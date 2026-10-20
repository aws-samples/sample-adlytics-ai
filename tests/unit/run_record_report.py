# SPDX-License-Identifier: MIT-0
"""Per-test metadata for the run recorder, written only when scripts/run_record.py asks for it.

The recorder sets RUN_RECORD_PYTEST_REPORT to a file in its staging directory, and
`tests/unit/conftest.py` then registers this plugin in `pytest_configure`. It writes one entry per
test (node ID, file, summary, covers-marker decision IDs, outcome, call duration) for the record's
tests.json, so a recorded pass names exactly which tests ran and how long each took. When the
recorder also sets RUN_RECORD_PYTEST_PROGRESS, the plugin keeps that file current while the run is
in progress (collected, done and per-outcome counts, at most once per PROGRESS_EVERY_S), and the
recorder's heartbeat copies it into status.json. Both variables are removed from the environment
first, so a pytest run started by a test never overwrites the outer files. Without them nothing is
registered: plain pytest and cosmic-ray runs are unaffected. docs/test-run-records.md owns the
record format.

Under pytest-xdist the plugin runs in the controller, which receives every worker's test reports
but collects nothing itself. It asks each worker, through `workerinput`, to send back the metadata
of the tests it collected in `workeroutput`, and takes the collected count from xdist's
node-collection hook. A worker never writes either file.
"""

from __future__ import annotations

import json
import os
import platform
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPORT_VARIABLE = "RUN_RECORD_PYTEST_REPORT"
PROGRESS_VARIABLE = "RUN_RECORD_PYTEST_PROGRESS"
# The progress file is rewritten at most this often; the recorder's heartbeat reads it.
PROGRESS_EVERY_S = 1.0
# The workerinput flag and workeroutput key that carry test metadata from xdist workers.
WORKER_META = "run_record_meta"


@dataclass
class _Result:
    """The outcome and call duration of one test node."""

    outcome: str = "passed"
    duration_s: float = 0.0


@dataclass(frozen=True)
class _Meta:
    """What a test is: its file, its one-line summary and the decisions it covers."""

    file: str
    summary: str
    decision_ids: list[str]


@dataclass
class _Progress:
    """Counts of the tests finished so far, rewritten whole so a reader never sees half a file."""

    target: Path
    collected: int = 0
    outcomes: Counter[str] = field(default_factory=Counter[str])
    written: float = float("-inf")

    def write(self, *, force: bool = False) -> None:
        """Rewrite the file, unless it was rewritten less than PROGRESS_EVERY_S ago."""
        now = time.monotonic()
        if not force and now - self.written < PROGRESS_EVERY_S:
            return
        self.written = now
        document = {
            "runner": "pytest",
            "collected": self.collected,
            "done": sum(self.outcomes.values()),
            "passed": self.outcomes["passed"],
            "failed": self.outcomes["failed"],
            "skipped": self.outcomes["skipped"],
            "errors": self.outcomes["error"],
        }
        temporary = self.target.with_name(f".{self.target.name}.tmp")
        temporary.write_text(json.dumps(document) + "\n")
        temporary.replace(self.target)


@dataclass
class _RunRecordReport:
    """A pytest plugin collecting per-test metadata and outcomes into one JSON report."""

    target: Path
    root: Path
    progress: _Progress | None = None
    meta: dict[str, _Meta] = field(default_factory=dict[str, _Meta])
    results: dict[str, _Result] = field(default_factory=dict[str, _Result])

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        """Remember each collected test's file, summary and covers-marker decision IDs."""
        for item in items:
            self.meta[item.nodeid] = _item_meta(item, self.root)

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        """Start the progress file with the number of tests selected to run."""
        if self.progress is not None:
            self.progress.collected = len(session.items)
            self.progress.write(force=True)

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node: Any) -> None:
        """xdist: ask each worker to send back the metadata of the tests it collects."""
        node.workerinput[WORKER_META] = True

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node: Any, ids: list[str]) -> None:  # noqa: ARG002 - the hook's signature
        """xdist: every worker collects the same tests; the controller collects none."""
        if self.progress is not None:
            self.progress.collected = len(ids)
            self.progress.write(force=True)

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any, error: object) -> None:  # noqa: ARG002 - the hook's signature
        """xdist: merge the metadata a finished worker sent back."""
        output: dict[str, Any] = getattr(node, "workeroutput", None) or {}
        for node_id, (file, summary, decision_ids) in output.get(WORKER_META, {}).items():
            self.meta[node_id] = _Meta(file=file, summary=summary, decision_ids=decision_ids)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        """Record a module that failed to collect as an error entry."""
        if report.failed:
            self.results[report.nodeid] = _Result(outcome="error")
            if self.progress is not None:
                self.progress.outcomes["error"] += 1

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Fold the setup, call and teardown reports of one test into its outcome."""
        result = self.results.setdefault(report.nodeid, _Result())
        if report.when == "call":
            result.duration_s = round(report.duration, 6)
            if result.outcome != "error":
                result.outcome = report.outcome
        elif report.failed:
            result.outcome = "error"
        elif report.skipped and report.when == "setup":
            result.outcome = "skipped"
        if report.when == "teardown" and self.progress is not None:
            self.progress.outcomes[result.outcome] += 1
            self.progress.write()

    def pytest_sessionfinish(self) -> None:
        """Write the report the recorder parses into tests.json."""
        tests: list[dict[str, object]] = []
        for node_id, result in self.results.items():
            meta = self.meta.get(node_id) or _Meta(
                file=node_id.split("::", 1)[0], summary="failed to collect", decision_ids=[]
            )
            tests.append(
                {
                    "node_id": node_id,
                    "file": meta.file,
                    "summary": meta.summary,
                    "decision_ids": meta.decision_ids,
                    "outcome": result.outcome,
                    "duration_s": result.duration_s,
                    "retries": 0,
                }
            )
        document = {
            "python": platform.python_version(),
            "pytest": pytest.__version__,
            "tests": tests,
        }
        self.target.write_text(json.dumps(document, indent=2) + "\n")
        if self.progress is not None:
            self.progress.write(force=True)


@dataclass
class _WorkerMeta:
    """An xdist worker's half: the metadata of each collected test, sent in workeroutput."""

    root: Path
    output: dict[str, list[object]]

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        """Store each collected test's metadata for the controller."""
        for item in items:
            meta = _item_meta(item, self.root)
            self.output[item.nodeid] = [meta.file, meta.summary, meta.decision_ids]


def _item_meta(item: pytest.Item, root: Path) -> _Meta:
    """Return a test's repository-relative file, one-line summary and covers-marker IDs."""
    doc = getattr(item.obj, "__doc__", None) if isinstance(item, pytest.Function) else None
    first = doc.strip().splitlines()[0].strip() if isinstance(doc, str) and doc.strip() else ""
    try:
        file = item.path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        file = item.path.as_posix()
    return _Meta(
        file=file,
        summary=first or item.name,
        decision_ids=[str(arg) for marker in item.iter_markers("covers") for arg in marker.args],
    )


def register(config: pytest.Config) -> None:
    """Register the report plugin when the recorder asked for one, and hide the request.

    An xdist worker registers only the metadata half, and only when its controller asked for it.
    """
    worker_input: dict[str, Any] | None = getattr(config, "workerinput", None)
    if worker_input is not None:
        os.environ.pop(REPORT_VARIABLE, None)
        os.environ.pop(PROGRESS_VARIABLE, None)
        if worker_input.get(WORKER_META):
            output: dict[str, list[object]] = {}
            config.workeroutput[WORKER_META] = output  # type: ignore[attr-defined]
            config.pluginmanager.register(_WorkerMeta(config.rootpath, output), "run-record-meta")
        return
    target = os.environ.pop(REPORT_VARIABLE, None)
    progress = os.environ.pop(PROGRESS_VARIABLE, None)
    if target:
        plugin = _RunRecordReport(
            Path(target), config.rootpath, _Progress(Path(progress)) if progress else None
        )
        config.pluginmanager.register(plugin, "run-record")
