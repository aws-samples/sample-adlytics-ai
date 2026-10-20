# SPDX-License-Identifier: MIT-0
"""The run recorder writes immutable, verifiable records and never lets a narrowed run pass.

`scripts/run_record.py` is the evidence behind every "the tests pass" claim and the input the
publication gate matches against the pushed tree (docs/test-run-records.md). A failure here means a
record could be overwritten or edited without detection, a delta could hide a newly failing test,
a runner report could be misread, or a narrowed run could be recorded as the canonical suite.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
import run_record_report
from gate_script_support import SCRIPTS, commit, new_repo, run

pytestmark = pytest.mark.unit

TREE = "ab" * 20
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 19, 12, 5, tzinfo=UTC)


@pytest.fixture(scope="module")
def recorder() -> ModuleType:
    """Load the recorder script as a module so its functions are called directly."""
    spec = importlib.util.spec_from_file_location("run_record", SCRIPTS / "run_record.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: the script's dataclasses resolve their own module while they are created.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_writable(root: Path) -> None:
    for current, dirs, files in os.walk(root):
        Path(current).chmod(0o755)
        for name in dirs:
            (Path(current) / name).chmod(0o755)
        for name in files:
            (Path(current) / name).chmod(0o644)


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    """Yield a record root whose read-only records are made writable again for tmp cleanup."""
    records = tmp_path / "runs"
    records.mkdir()
    yield records
    _make_writable(records)


def _test(node_id: str, outcome: str, duration: float = 0.1) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "file": "tests/unit/test_fixture.py",
        "summary": node_id,
        "decision_ids": [],
        "outcome": outcome,
        "duration_s": duration,
        "retries": 0,
    }


def _write(  # noqa: PLR0913 — `now` is keyword-only; the rest are the fixtures of every caller
    recorder: ModuleType, root: Path, tests: list[dict[str, Any]], *, now: datetime = NOW
) -> Path:
    run = {"canonical": True, "tree": TREE, "exit_code": 0, "report_missing": False}
    return recorder.write_record(
        "pytest-unit", run, tests=tests, files={"console.log": b"ok\n"}, root=root, now=now
    )


def test_an_existing_record_directory_is_refused_and_left_untouched(
    recorder: ModuleType, root: Path
) -> None:
    first = _write(recorder, root, [_test("t::a", "passed")])
    before = {p: p.read_bytes() for p in first.rglob("*") if p.is_file()}

    with pytest.raises(recorder.RecordExistsError):
        _write(recorder, root, [_test("t::a", "failed")])

    assert {p: p.read_bytes() for p in first.rglob("*") if p.is_file()} == before
    assert recorder.verify_record(first) == []


def test_the_manifest_lists_every_other_file_with_its_sha256(
    recorder: ModuleType, root: Path
) -> None:
    record = _write(recorder, root, [_test("t::a", "passed")])
    manifest = json.loads((record / "manifest.json").read_text())
    on_disk = {
        p.relative_to(record).as_posix()
        for p in record.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }

    assert manifest["schema"] == 1
    assert (
        set(manifest["files"])
        == on_disk
        == {"run.json", "tests.json", "console.log", "status.json"}
    )
    for rel, entry in manifest["files"].items():
        data = (record / rel).read_bytes()
        assert entry == {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    assert recorder.verify_record(record) == []


def _modify(record: Path) -> None:
    (record / "console.log").write_bytes(b"edited\n")


def _add(record: Path) -> None:
    (record / "extra.txt").write_text("unlisted\n")


def _remove(record: Path) -> None:
    (record / "console.log").unlink()


@pytest.mark.parametrize("tamper", [_modify, _add, _remove], ids=["modified", "added", "removed"])
def test_a_tampered_record_does_not_verify(recorder: ModuleType, root: Path, tamper: Any) -> None:
    record = _write(recorder, root, [_test("t::a", "passed")])
    _make_writable(record)

    tamper(record)

    assert recorder.verify_record(record) != []
    assert recorder.latest_record(root, "pytest-unit") is None


def test_files_are_read_only_and_directories_unwritable_after_writing(
    recorder: ModuleType, root: Path
) -> None:
    record = recorder.write_record(
        "pytest-unit",
        {"tree": TREE, "exit_code": 0},
        tests=[_test("t::a", "passed")],
        files={"console.log": b"", "raw/junit.xml": b"<testsuites/>"},
        root=root,
        now=NOW,
    )

    modes = {
        p.relative_to(record).as_posix(): p.stat().st_mode & 0o777
        for p in [record, *record.rglob("*")]
    }
    assert modes == {
        ".": 0o555,
        "raw": 0o555,
        "raw/junit.xml": 0o444,
        "console.log": 0o444,
        "run.json": 0o444,
        "status.json": 0o444,
        "tests.json": 0o444,
        "manifest.json": 0o444,
    }


def test_the_delta_names_newly_failing_added_and_removed_tests(
    recorder: ModuleType, root: Path
) -> None:
    previous = _write(
        recorder,
        root,
        [_test("t::a", "passed"), _test("t::b", "passed"), _test("t::c", "failed")],
    )
    current = _write(
        recorder,
        root,
        [_test("t::a", "failed"), _test("t::b", "passed"), _test("t::d", "passed", 0.4)],
        now=LATER,
    )

    run = json.loads((current / "run.json").read_text())
    delta = run["delta"]
    assert delta["previous"] == f"pytest-unit/{previous.name}"
    assert delta["previous_tree"] == TREE
    assert delta["newly_failing"] == ["t::a"]
    assert delta["newly_passing"] == []
    assert (delta["added"], delta["removed"]) == (["t::d"], ["t::c"])
    assert delta["totals_change"] == {
        "tests": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
    }
    assert delta["p95_change_s"] == pytest.approx(0.3)
    assert run["outcome"] == "fail"
    assert json.loads((previous / "run.json").read_text())["delta"] is None


def test_only_record_shaped_names_count_as_records(recorder: ModuleType, root: Path) -> None:
    record = _write(recorder, root, [_test("t::a", "passed")])
    (root / "pytest-unit" / "coverage-map.json").write_text("{}\n")
    (root / "pytest-unit" / "worksheets").mkdir()

    assert recorder.record_dirs(root, "pytest-unit") == [record]


def test_a_jest_or_vitest_report_becomes_tests_json_entries(
    recorder: ModuleType, tmp_path: Path
) -> None:
    report = {
        "testResults": [
            {
                "name": str(tmp_path / "test" / "format.test.ts"),
                "status": "failed",
                "assertionResults": [
                    {
                        "ancestorTitles": ["format", "currency"],
                        "title": "keeps two decimals",
                        "status": "passed",
                        "duration": 12,
                    },
                    {
                        "ancestorTitles": [],
                        "title": "rejects NaN",
                        "status": "failed",
                        "duration": 3,
                        "retryReasons": ["first", "second"],
                    },
                    {"ancestorTitles": ["format"], "title": "later", "status": "todo"},
                ],
            },
            {"name": str(tmp_path / "test" / "broken.test.ts"), "status": "failed"},
        ]
    }

    tests = recorder.parse_js_report(report, tmp_path)

    assert [(t["node_id"], t["outcome"], t["duration_s"], t["retries"]) for t in tests] == [
        ("test/format.test.ts::format > currency::keeps two decimals", "passed", 0.012, 0),
        ("test/format.test.ts::rejects NaN", "failed", 0.003, 2),
        ("test/format.test.ts::format::later", "skipped", None, 0),
        ("test/broken.test.ts", "error", None, 0),
    ]
    assert {t["file"] for t in tests} == {"test/format.test.ts", "test/broken.test.ts"}
    assert all(t["decision_ids"] == [] for t in tests)


GATE = [sys.executable, str(SCRIPTS / "run_record.py"), "gate"]
_FAKE_RUNNER = """
import json, sys
json.dump({"testResults": [{"name": sys.argv[2], "status": "passed", "assertionResults": [
    {"ancestorTitles": [], "title": "one", "status": "passed", "duration": 5}]}]},
    open(sys.argv[1], "w"))
print("fake runner output")
sys.exit(int(sys.argv[3]))
"""


@pytest.fixture
def repo(tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a clean throwaway repository whose recorder writes into `root`."""
    monkeypatch.setenv("RUN_RECORD_ROOT", str(root))
    monkeypatch.delenv("RUN_RECORD_SOURCE_COMMIT", raising=False)
    monkeypatch.delenv("RUN_RECORD_PARENT", raising=False)
    return new_repo(tmp_path)


@pytest.mark.parametrize("case", [(0, "pass"), (1, "fail")], ids=["passing", "failing"])
def test_a_run_with_its_own_argv_is_recorded_non_canonical(
    repo: Path, root: Path, case: tuple[int, str]
) -> None:
    child_exit, outcome = case
    argv = [
        sys.executable,
        str(SCRIPTS / "run_record.py"),
        "run",
        "--kind",
        "pytest-unit",
        "--format",
        "jest-json",
        "--",
        sys.executable,
        "-c",
        _FAKE_RUNNER,
        "{staging}/jest.json",
        str(repo / "a.test.ts"),
        str(child_exit),
    ]

    result = run(argv, repo)

    assert result.returncode == child_exit, result.stderr
    assert "fake runner output" in result.stdout
    (record,) = (root / "pytest-unit").iterdir()
    assert f"run-record: {record}" in result.stdout
    run_json = json.loads((record / "run.json").read_text())
    assert run_json["canonical"] is False
    assert run_json["argv"][-3] == "{staging}/jest.json"
    assert (run_json["context"], run_json["outcome"], run_json["exit_code"]) == (
        "checkout",
        outcome,
        child_exit,
    )
    assert run_json["clean"] is True
    assert run_json["report_missing"] is False
    assert json.loads((record / "tests.json").read_text())[0]["node_id"] == "a.test.ts::one"
    assert (record / "raw" / "jest.json").is_file()
    assert b"fake runner output" in (record / "console.log").read_bytes()
    assert (
        run(
            [sys.executable, str(SCRIPTS / "run_record.py"), "verify", str(record)], repo
        ).returncode
        == 0
    )


def test_a_gate_stops_at_the_first_failing_step_and_names_its_child_records(
    repo: Path, root: Path
) -> None:
    child = (
        f"{sys.executable} {SCRIPTS / 'run_record.py'} run --kind scratch-child --format jest-json"
        f" -- {sys.executable} fake_runner.py {{staging}}/jest.json {repo / 'a.test.ts'} 0"
    )
    commit(repo, "Makefile", f"ok:\n\t@{child}\nfail:\n\t@exit 4\nnever:\n\t@echo never\n")
    commit(repo, "fake_runner.py", _FAKE_RUNNER)

    result = run(
        [*GATE, "--kind", "scratch-gate", "--", "ok", "fail", "never"],
        repo,
    )

    # make exits 2 when a recipe fails; the gate passes the failing step's code through.
    assert result.returncode == 2, result.stderr
    assert "never\n" not in result.stdout
    (gate,) = (root / "scratch-gate").iterdir()
    (child_record,) = (root / "scratch-child").iterdir()
    steps = json.loads((gate / "steps.json").read_text())
    assert [(s["name"], s["exit_code"]) for s in steps] == [("ok", 0), ("fail", 2)]
    assert steps[0]["child_records"] == [f"scratch-child/{child_record.name}"]
    gate_run = json.loads((gate / "run.json").read_text())
    assert (gate_run["canonical"], gate_run["outcome"], gate_run["totals"]) == (False, "fail", None)
    assert gate_run["child_records"] == [f"scratch-child/{child_record.name}"]
    # Each child names the gate it ran under; the gate itself ran under none.
    assert gate_run["parent"] is None
    child_run = json.loads((child_record / "run.json").read_text())
    assert child_run["parent"] == f"scratch-gate/{gate.name}"


def test_a_kind_without_a_canonical_command_needs_an_argv(repo: Path, root: Path) -> None:
    result = run(
        [sys.executable, str(SCRIPTS / "run_record.py"), "run", "--kind", "scratch-x"], repo
    )

    assert result.returncode == 2
    assert not (root / "scratch-x").exists()


# --------------------------------------------------------------------------------------------
# Live status: running, sealed, abandoned, never accepted
# --------------------------------------------------------------------------------------------

# Runs as the recorded child: waits until its first line reached console.log, then writes what
# the in-progress record held at that moment to argv[2], and a passing jest report to argv[1].
_OBSERVER = """
import json, os, pathlib, sys, time
print("first line, streamed", flush=True)
kind_dir = pathlib.Path(os.environ["RUN_RECORD_ROOT"]) / "scratch-live"
(record,) = [p for p in kind_dir.iterdir() if p.is_dir()]
deadline = time.monotonic() + 20
while "first line" not in (record / "console.log").read_text() and time.monotonic() < deadline:
    time.sleep(0.02)
seen = {
    "run": json.loads((record / "run.json").read_text()),
    "status": json.loads((record / "status.json").read_text()),
    "console": (record / "console.log").read_text(),
    "manifest": (record / "manifest.json").exists(),
    "recorder_pid": os.getppid(),
}
pathlib.Path(sys.argv[2]).write_text(json.dumps(seen))
json.dump({"testResults": [{"name": "a.test.ts", "status": "passed", "assertionResults": [
    {"ancestorTitles": [], "title": "one", "status": "passed", "duration": 5}]}]},
    open(sys.argv[1], "w"))
print("PASS a.test.ts", flush=True)
"""
# Runs as the recorded child: once its line is on disk, it kills the recorder with SIGKILL, so
# nothing can finalise the record.
_KILLER = """
import os, pathlib, signal, time
print("about to stop the recorder", flush=True)
kind_dir = pathlib.Path(os.environ["RUN_RECORD_ROOT"]) / "scratch-killed"
(record,) = [p for p in kind_dir.iterdir() if p.is_dir()]
deadline = time.monotonic() + 20
while "about to stop" not in (record / "console.log").read_text() and time.monotonic() < deadline:
    time.sleep(0.02)
os.kill(os.getppid(), signal.SIGKILL)
"""


def _record_run(kind: str, script: str, *args: str) -> list[str]:
    return [
        sys.executable,
        str(SCRIPTS / "run_record.py"),
        "run",
        "--kind",
        kind,
        "--format",
        "jest-json",
        "--",
        sys.executable,
        "-c",
        script,
        *args,
    ]


def test_an_in_progress_run_is_on_disk_and_sealed_only_when_it_ends(  # noqa: PLR0913, PLR0917 - fixtures
    recorder: ModuleType, repo: Path, root: Path, tmp_path: Path
) -> None:
    seen_path = tmp_path / "seen.json"

    result = run(
        _record_run("scratch-live", _OBSERVER, "{staging}/jest.json", str(seen_path)), repo
    )

    assert result.returncode == 0, result.stderr
    seen = json.loads(seen_path.read_text())
    # While the child ran: run.json said running, the heartbeat existed, the console was streamed
    # to disk, and there was no manifest, so nothing could verify or accept the record.
    assert seen["run"]["status"] == "running"
    assert seen["run"]["pid"] == seen["recorder_pid"]
    assert seen["run"]["kind"] == "scratch-live"
    assert seen["status"]["status"] == "running"
    assert seen["status"]["progress"] == {"files_passed": 0, "files_failed": 0}
    assert "first line, streamed" in seen["console"]
    assert seen["manifest"] is False
    (record,) = (root / "scratch-live").iterdir()
    assert f"run-record: running {record}" in result.stdout
    # After it: sealed, read-only, final status everywhere, the whole console kept.
    assert recorder.verify_record(record) == []
    assert recorder.record_state(record) == "passed"
    run_json = json.loads((record / "run.json").read_text())
    assert (run_json["status"], run_json["outcome"]) == ("passed", "pass")
    status = json.loads((record / "status.json").read_text())
    assert status["status"] == "passed"
    assert status["progress"] == {"files_passed": 1, "files_failed": 0}
    console = (record / "console.log").read_text()
    assert console.index("first line, streamed") < console.index("PASS a.test.ts")
    assert {p.stat().st_mode & 0o777 for p in record.rglob("*") if p.is_file()} == {0o444}
    assert {p.stat().st_mode & 0o777 for p in [record, *record.rglob("*")] if p.is_dir()} == {0o555}
    assert recorder.find_records(root, "scratch-live", passing=True) == [record]


def test_a_killed_run_is_abandoned_and_never_accepted(
    recorder: ModuleType, repo: Path, root: Path
) -> None:
    result = run(_record_run("scratch-killed", _KILLER), repo)

    assert result.returncode == -9
    (record,) = (root / "scratch-killed").iterdir()
    assert json.loads((record / "run.json").read_text())["status"] == "running"
    assert "about to stop the recorder" in (record / "console.log").read_text()
    assert not (record / "manifest.json").exists()
    # Fresh, it still looks running; once the heartbeat is stale and the process gone, abandoned.
    assert recorder.record_state(record) == "running"
    stale = time.time() + recorder.STALE_AFTER_S + 1
    assert recorder.record_state(record, now=stale) == "abandoned"
    assert recorder.verify_record(record) != []
    assert recorder.find_records(root, "scratch-killed") == []
    assert recorder.latest_record(root, "scratch-killed") is None


def test_gate_status_lists_running_and_abandoned_records_with_their_last_lines(  # noqa: PLR0913, PLR0917 - fixtures
    recorder: ModuleType, repo: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(_record_run("scratch-killed", _KILLER), repo)
    _write(recorder, root, [_test("t::a", "passed")])

    recorder.command_status(root, recent=5, lines=3)
    fresh = capsys.readouterr().out
    recorder.command_status(root, recent=5, lines=3, now=time.time() + recorder.STALE_AFTER_S + 1)
    stale = capsys.readouterr().out

    assert "run-record: 1 in progress" in fresh
    assert "RUNNING   scratch-killed/" in fresh
    assert "heartbeat " in fresh
    assert "| about to stop the recorder" in fresh
    assert "PASSED    pytest-unit/" in fresh
    assert "run-record: 0 in progress" in stale
    assert "abandoned (never accepted):" in stale
    assert "ABANDONED scratch-killed/" in stale


def test_the_heartbeat_rewrites_status_until_the_record_is_sealed(
    recorder: ModuleType, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recorder, "HEARTBEAT_INTERVAL_S", 0.02)
    polls: list[int] = []

    def progress() -> dict[str, int]:
        polls.append(1)
        return {"done": len(polls)}

    live = recorder.LiveRecord.begin("scratch-beat", {"tree": TREE}, root=root, progress=progress)
    first = json.loads((live.directory / "status.json").read_text())
    deadline = time.monotonic() + 10
    while len(polls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    live.set_step("lint")
    beating = json.loads((live.directory / "status.json").read_text())
    record = live.finalise({"exit_code": 0, "report_missing": False}, steps=[])
    sealed = (record / "status.json").read_bytes()
    time.sleep(0.1)

    assert first["status"] == "running"
    assert beating["updated_epoch"] > first["updated_epoch"]
    assert beating["step"] == "lint"
    assert beating["progress"]["done"] >= 3
    assert (record / "status.json").read_bytes() == sealed
    assert json.loads(sealed)["status"] == "passed"
    assert recorder.verify_record(record) == []


def test_a_sealed_record_that_says_running_does_not_verify(
    recorder: ModuleType, root: Path
) -> None:
    record = _write(recorder, root, [_test("t::a", "passed")])
    _make_writable(record)
    run_json = json.loads((record / "run.json").read_text())
    run_json["status"] = "running"
    (record / "run.json").write_text(json.dumps(run_json))
    manifest = json.loads((record / "manifest.json").read_text())
    data = (record / "run.json").read_bytes()
    manifest["files"]["run.json"] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    (record / "manifest.json").write_text(json.dumps(manifest))

    assert recorder.verify_record(record) == ["run.json: status 'running' is not final"]
    assert recorder.find_records(root, "pytest-unit") == []


def test_js_progress_counts_finished_files_across_chunk_boundaries(recorder: ModuleType) -> None:
    progress = recorder.JsProgress()
    stream = (
        b"\x1b[1mPASS\x1b[22m test/a.test.ts (2.1 s)\nFA",
        b"IL test/b.test.ts\n \xe2\x9c\x93 test/c.test.ts (16 tests) 20ms\n",
        b"   \xe2\x9c\x93 a slow test inside c  310ms\n",
        b" \xe2\x9d\xaf test/d.test.ts (5 tests | 2 failed) 9ms\n \xe2\x9c\x93 test/e.ts (1 test)",
    )
    for chunk in stream:
        progress.feed(chunk)

    # The last line has no newline yet, so it is not counted until it completes.
    assert progress.counts() == {
        "files_passed": 2,
        "files_failed": 2,
        "tests_done": 21,
        "tests_failed": 2,
    }


def test_the_pytest_plugin_keeps_a_progress_file_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    progress_path = tmp_path / "progress.json"
    monkeypatch.setenv("RUN_RECORD_PYTEST_REPORT", str(tmp_path / "report.json"))
    monkeypatch.setenv("RUN_RECORD_PYTEST_PROGRESS", str(progress_path))
    registered: list[Any] = []

    def register(plugin: object, _name: str) -> None:
        registered.append(plugin)

    manager = SimpleNamespace(register=register)
    run_record_report.register(
        cast("Any", SimpleNamespace(rootpath=tmp_path, pluginmanager=manager))
    )
    (plugin,) = registered
    # A pytest run started by a test never sees either path.
    assert "RUN_RECORD_PYTEST_REPORT" not in os.environ
    assert "RUN_RECORD_PYTEST_PROGRESS" not in os.environ
    plugin.pytest_collection_finish(cast("Any", SimpleNamespace(items=[1, 2, 3])))
    started = json.loads(progress_path.read_text())

    def report(node: str, when: str, outcome: str) -> Any:
        return SimpleNamespace(
            nodeid=node,
            when=when,
            outcome=outcome,
            failed=outcome == "failed",
            skipped=outcome == "skipped",
            duration=0.01,
        )

    for node, outcome in (("t::a", "passed"), ("t::b", "failed")):
        for when in ("setup", "call", "teardown"):
            plugin.pytest_runtest_logreport(
                report(node, when, outcome if when == "call" else "passed")
            )
    plugin.pytest_sessionfinish()

    assert started == {
        "runner": "pytest",
        "collected": 3,
        "done": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
    }
    assert json.loads(progress_path.read_text()) == {
        "runner": "pytest",
        "collected": 3,
        "done": 2,
        "passed": 1,
        "failed": 1,
        "skipped": 0,
        "errors": 0,
    }


def test_an_xdist_worker_never_registers_the_report_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUN_RECORD_PYTEST_REPORT", str(tmp_path / "report.json"))
    monkeypatch.setenv("RUN_RECORD_PYTEST_PROGRESS", str(tmp_path / "progress.json"))
    registered: list[tuple[object, str]] = []

    def register(plugin: object, name: str) -> None:
        registered.append((plugin, name))

    config = SimpleNamespace(
        rootpath=tmp_path,
        pluginmanager=SimpleNamespace(register=register),
        workerinput={run_record_report.WORKER_META: True},
        workeroutput={},
    )
    run_record_report.register(cast("Any", config))

    assert [name for _, name in registered] == ["run-record-meta"]
    assert config.workeroutput == {run_record_report.WORKER_META: {}}
    assert "RUN_RECORD_PYTEST_REPORT" not in os.environ
    assert "RUN_RECORD_PYTEST_PROGRESS" not in os.environ


# --------------------------------------------------------------------------------------------
# Parallel unit runs: the bounded worker count and the xdist report
# --------------------------------------------------------------------------------------------


def test_the_canonical_unit_command_runs_bounded_workers_one_module_per_worker(
    recorder: ModuleType,
) -> None:
    argv = list(recorder.CANONICAL["pytest-unit"].argv)

    assert argv[argv.index("-n") + 1] == recorder.WORKERS
    assert argv[argv.index("--dist") + 1] == "loadfile"
    assert "auto" not in argv


def test_the_canonical_unit_command_names_the_one_directory_every_unit_test_lives_in(
    recorder: ModuleType,
) -> None:
    """The named path loads tests/unit/conftest.py in the xdist controller and drops no test."""
    tests = SCRIPTS.parent / "tests"
    elsewhere = [
        path.relative_to(tests).as_posix()
        for path in sorted(tests.rglob("*.py"))
        if not path.is_relative_to(SCRIPTS.parent / recorder.UNIT_TESTS)
        and re.search(r"\bmark\.unit\b", path.read_text(encoding="utf-8"))
    ]

    assert recorder.CANONICAL["pytest-unit"].argv[-1] == recorder.UNIT_TESTS == "tests/unit"
    assert elsewhere == []


@pytest.mark.parametrize(
    ("cores", "expected"), [(1, 1), (2, 1), (3, 1), (4, 2), (6, 4), (10, 4), (128, 4)]
)
def test_the_default_worker_count_leaves_two_cores_free_and_is_capped(
    recorder: ModuleType, cores: int, expected: int
) -> None:
    assert recorder.pytest_workers({}, lambda: cores) == expected


@pytest.mark.parametrize("value", ["0", "1", "4", " 2 "])
def test_pytest_workers_overrides_the_default_up_to_the_cap(
    recorder: ModuleType, value: str
) -> None:
    assert recorder.pytest_workers({"PYTEST_WORKERS": value}, lambda: 128) == int(value)


@pytest.mark.parametrize("value", ["5", "64", "-1", "auto", "2.5", "\u00b2"])
def test_a_worker_count_above_the_cap_or_malformed_is_refused(
    recorder: ModuleType, value: str
) -> None:
    with pytest.raises(ValueError, match="whole number from 0 to 4"):
        recorder.pytest_workers({"PYTEST_WORKERS": value}, lambda: 128)


def test_a_canonical_run_with_a_refused_worker_count_writes_no_record(
    repo: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_WORKERS", "9")

    result = run(
        [sys.executable, str(SCRIPTS / "run_record.py"), "run", "--kind", "pytest-unit"], repo
    )

    assert result.returncode == 2
    assert "PYTEST_WORKERS must be a whole number from 0 to 4" in result.stderr
    assert not (root / "pytest-unit").exists()


def test_linux_physical_cores_counts_cores_not_hyperthreads(recorder: ModuleType) -> None:
    cpuinfo = "\n\n".join(
        f"processor\t: {n}\nphysical id\t: {socket}\ncore id\t: {core}"
        for n, (socket, core) in enumerate([(0, 0), (0, 0), (0, 1), (0, 1), (1, 0), (1, 0)])
    )

    assert recorder.linux_physical_cores(cpuinfo) == 3
    assert recorder.linux_physical_cores("processor\t: 0\n") is None


_XDIST_SUITE = {
    "pytest.ini": "[pytest]\nmarkers =\n    covers(*decision_ids): decisions\n",
    "conftest.py": (
        "import sys\n"
        f"sys.path.insert(0, {str(Path(run_record_report.__file__).parent)!r})\n"
        "from run_record_report import register\n\n\n"
        "def pytest_configure(config):\n"
        "    register(config)\n"
    ),
    "test_one.py": (
        "import pytest\n\n\n"
        '@pytest.mark.covers("ADR-001")\n'
        "def test_a():\n"
        '    """Adds up."""\n\n\n'
        "def test_b():\n"
        '    pytest.skip("not here")\n'
    ),
    "test_two.py": "def test_c():\n    pass\n\n\ndef test_d():\n    assert 1 == 2\n",
}


def test_an_xdist_run_records_the_same_tests_totals_and_metadata_as_one_process(
    repo: Path, root: Path, tmp_path: Path
) -> None:
    del repo  # sets the record root
    suite = tmp_path / "suite"
    suite.mkdir()
    for name, text in _XDIST_SUITE.items():
        (suite / name).write_text(text)
    argv = [
        sys.executable,
        str(SCRIPTS / "run_record.py"),
        "run",
        "--kind",
        "scratch-xdist",
        "--format",
        "pytest",
        "--",
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-n",
        "2",
        "--dist",
        "loadfile",
        "--junitxml={staging}/junit.xml",
    ]

    result = run(argv, suite)

    assert result.returncode == 1, result.stdout + result.stderr
    (record,) = (root / "scratch-xdist").iterdir()
    run_json = json.loads((record / "run.json").read_text())
    assert run_json["totals"] == {"tests": 4, "passed": 2, "failed": 1, "skipped": 1, "errors": 0}
    tests = {t["node_id"]: t for t in json.loads((record / "tests.json").read_text())}
    assert set(tests) == {
        "test_one.py::test_a",
        "test_one.py::test_b",
        "test_two.py::test_c",
        "test_two.py::test_d",
    }
    assert (tests["test_one.py::test_a"]["summary"], tests["test_one.py::test_a"]["file"]) == (
        "Adds up.",
        "test_one.py",
    )
    assert tests["test_one.py::test_a"]["decision_ids"] == ["ADR-001"]
    assert tests["test_two.py::test_d"]["summary"] == "test_d"
    progress = json.loads((record / "status.json").read_text())["progress"]
    assert (progress["collected"], progress["done"], progress["failed"]) == (4, 4, 1)
    assert 'tests="4"' in (record / "raw" / "junit.xml").read_text()


_UNIT = "import pytest\n\npytestmark = pytest.mark.unit\n\n\n"

# The repository's layout, so the pytest-xdist controller loads (or fails to load) the report plugin
# exactly as it does for `make test`: testpaths names tests/, the plugin is registered by
# tests/unit/conftest.py from a copy of the real run_record_report.py, and a test module defines
# its own warning category, as tests/unit/test_fixture_conformance.py does. The suite covers every
# outcome the report folds (pass, fail, skip at setup and in the call, setup and teardown errors),
# a module skipped at import, and a test the unit marker deselects.
_EQUIVALENCE_SUITE = {
    "pytest.ini": (
        "[pytest]\ntestpaths = tests\naddopts = --strict-markers\n"
        "markers =\n    unit: unit\n    covers(*decision_ids): decisions\n"
    ),
    "tests/unit/conftest.py": (
        "from run_record_report import register\n\n\n"
        "def pytest_configure(config):\n"
        "    register(config)\n"
    ),
    "tests/unit/test_one.py": (
        f"{_UNIT}"
        '@pytest.mark.covers("ADR-001")\n'
        "def test_a():\n"
        '    """Adds up."""\n\n\n'
        "def test_b():\n"
        '    pytest.skip("not here")\n'
    ),
    "tests/unit/test_two.py": (
        f"{_UNIT}def test_c():\n    pass\n\n\ndef test_d():\n    assert 1 == 2\n"
    ),
    "tests/unit/test_three.py": (
        f"{_UNIT}"
        "@pytest.fixture\n"
        "def broken_setup():\n"
        '    raise RuntimeError("setup")\n\n\n'
        "@pytest.fixture\n"
        "def broken_teardown():\n"
        "    yield\n"
        '    raise RuntimeError("teardown")\n\n\n'
        "def test_e(broken_setup):\n    pass\n\n\n"
        "def test_f(broken_teardown):\n    pass\n\n\n"
        '@pytest.mark.skip(reason="marked")\n'
        "def test_g():\n    pass\n"
    ),
    "tests/unit/test_four.py": (
        f'{_UNIT}pytest.skip("module", allow_module_level=True)\n\n\ndef test_h():\n    pass\n'
    ),
    "tests/unit/test_five.py": "def test_i():\n    pass\n",
    "tests/unit/test_six.py": (
        f"import warnings\n\n{_UNIT}"
        "class SuiteWarning(UserWarning):\n"
        '    """A warning category only this test module defines."""\n\n\n'
        "def test_j():\n"
        '    warnings.warn("shown", SuiteWarning, stacklevel=1)\n'
    ),
}


def _record_suite(recorder: ModuleType, suite: Path, workers: int) -> Path:
    """Record the canonical unit command over `suite` on `workers` workers (0: one process)."""
    kind = f"scratch-workers-{workers}"
    canonical = [
        str(workers) if arg == recorder.WORKERS else arg
        for arg in recorder.CANONICAL["pytest-unit"].argv
    ]
    argv = [
        sys.executable,
        str(SCRIPTS / "run_record.py"),
        "run",
        "--kind",
        kind,
        "--format",
        "pytest",
        "--",
        *canonical,
        "-p",
        "no:cacheprovider",
    ]
    result = run(argv, suite)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "INTERNALERROR" not in result.stdout + result.stderr
    (record,) = (Path(os.environ["RUN_RECORD_ROOT"]) / kind).iterdir()
    return record


def _record_view(record: Path) -> dict[str, object]:
    """Return what a record says about its tests, without timings, hosts or run order."""

    def entries(tests: list[dict[str, Any]]) -> list[tuple[object, ...]]:
        keys = ("node_id", "file", "summary", "decision_ids", "outcome")
        return sorted(tuple(json.dumps(test[key]) for key in keys) for test in tests)

    run_json = json.loads((record / "run.json").read_text())
    raw_report = json.loads((record / "raw" / "pytest.json").read_text())
    suite_element = re.search(r"<testsuite [^>]*>", (record / "raw" / "junit.xml").read_text())
    assert suite_element is not None
    suite_attributes = dict(re.findall(r' ([a-z]+)="([^"]*)"', suite_element.group(0)))
    return {
        "outcome": run_json["outcome"],
        "report_missing": run_json["report_missing"],
        "totals": run_json["totals"],
        "tests.json": entries(json.loads((record / "tests.json").read_text())),
        "raw/pytest.json": entries(raw_report["tests"]),
        "progress": json.loads((record / "status.json").read_text())["progress"],
        "junit": {
            name: suite_attributes[name] for name in ("tests", "failures", "errors", "skipped")
        },
        "files": sorted(path.name for path in (record / "raw").iterdir()),
    }


def test_a_parallel_record_equals_a_one_process_record_of_the_same_suite(
    recorder: ModuleType, repo: Path, tmp_path: Path
) -> None:
    del repo  # sets the record root
    suite = tmp_path / "suite"
    files = {
        **_EQUIVALENCE_SUITE,
        "tests/unit/run_record_report.py": Path(run_record_report.__file__).read_text(),
    }
    for name, text in files.items():
        (suite / name).parent.mkdir(parents=True, exist_ok=True)
        (suite / name).write_text(text)

    serial = _record_view(_record_suite(recorder, suite, 0))
    parallel = _record_view(_record_suite(recorder, suite, 3))

    assert parallel == serial
    assert serial["report_missing"] is False
    assert serial["totals"] == {"tests": 8, "passed": 3, "failed": 1, "skipped": 2, "errors": 2}
    assert serial["progress"] == {
        "runner": "pytest",
        "collected": 8,
        "done": 8,
        "passed": 3,
        "failed": 1,
        "skipped": 2,
        "errors": 2,
    }


# --------------------------------------------------------------------------------------------
# Exit-code runs
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", [(0, "pass"), (3, "fail")], ids=["passing", "failing"])
def test_an_exit_format_run_is_judged_by_its_exit_code_alone(
    repo: Path, root: Path, case: tuple[int, str]
) -> None:
    code, outcome = case
    argv = [
        sys.executable,
        str(SCRIPTS / "run_record.py"),
        "run",
        "--kind",
        "accept-fixture",
        "--format",
        "exit",
        "--",
        sys.executable,
        "-c",
        f"print('probe output'); raise SystemExit({code})",
    ]

    result = run(argv, repo)

    assert result.returncode == code, result.stderr
    (record,) = (root / "accept-fixture").iterdir()
    run_json = json.loads((record / "run.json").read_text())
    assert (run_json["outcome"], run_json["report_missing"], run_json["exit_code"]) == (
        outcome,
        False,
        code,
    )
    assert json.loads((record / "tests.json").read_text()) == []
    assert "probe output" in (record / "console.log").read_text()
