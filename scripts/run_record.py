#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Write, verify and find immutable test-run records under the gitignored `.runs/` directory.

docs/test-run-records.md owns the record contract: the layout, every field, which records a gate
accepts. This module owns the canonical command of every test kind (`CANONICAL`) and the steps of
every gate kind (`CANONICAL_GATES`), so a narrowed run can never pass for the canonical one.

    run    --kind K [--format F --cwd DIR -- argv...]   run one test command and record it
    gate   --kind K [-- target...]                      run `make <target>` per step and record it
    verify DIR                                          exit 0 iff DIR matches its manifest
    find   --kind K [--tree T] [--passing] [--canonical] [--clean]
    status [--recent N] [--lines N]                     list in-progress and recent records

A record directory is created with os.mkdir when the run starts, so an existing one is refused
(exit 3), never overwritten or merged. While the run is in progress run.json says `running`,
console.log grows as the child writes, and status.json is rewritten every HEARTBEAT_INTERVAL_S.
At the end every file is listed with its sha256 in manifest.json, then every file is made 0444 and
every directory 0555. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import IO, Any, cast

SCHEMA = 1
ROOT_VARIABLE = "RUN_RECORD_ROOT"
SOURCE_VARIABLE = "RUN_RECORD_SOURCE_COMMIT"
PYTEST_REPORT_VARIABLE = "RUN_RECORD_PYTEST_REPORT"
PYTEST_PROGRESS_VARIABLE = "RUN_RECORD_PYTEST_PROGRESS"
# A gate sets this for its children, so each child record names the gate record it ran under.
PARENT_VARIABLE = "RUN_RECORD_PARENT"
STAGING = "{staging}"
# The pytest progress file in the staging directory; a heartbeat input, never copied into raw/.
PYTEST_PROGRESS_FILE = "pytest-progress.json"
# Live status (docs/test-run-records.md#live-status). An in-progress record's status.json is
# rewritten at least every HEARTBEAT_INTERVAL_S seconds. A record still `running` whose heartbeat
# is older than STALE_AFTER_S is `abandoned` when its process is gone, `stalled` when it is not.
HEARTBEAT_INTERVAL_S = 10.0
STALE_AFTER_S = 120.0
RUNNING = "running"
FINAL_STATUSES = frozenset({"passed", "failed"})
IN_PROGRESS_STATES = frozenset({RUNNING, "stalled"})
# A record directory: UTC start of the run to the microsecond, then the first 12 hex of the tree.
RECORD_NAME = re.compile(r"^\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{12}$")
KIND_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
UNKNOWN_TREE = "0" * 40
RESERVED = frozenset(
    {"run.json", "tests.json", "steps.json", "manifest.json", "status.json", "console.log"}
)
NODE_PACKAGES = ("vitest", "jest", "ts-jest")
SLOWEST = 20
EXIT_USAGE = 2
EXIT_RECORD_EXISTS = 3
EXIT_RECORD_FAILED = 70
# The report file each format reads from the staging directory; an argv writes it there.
REPORT_FILES = {"pytest": "pytest.json", "vitest-json": "vitest.json", "jest-json": "jest.json"}
# A command with no runner report (an acceptance probe, a report-only scan): its exit code alone.
EXIT_FORMAT = "exit"
FORMATS = (*sorted(REPORT_FILES), EXIT_FORMAT)
FAILING = frozenset({"failed", "error"})


@dataclass(frozen=True)
class Canonical:
    """The one command a test kind is recorded canonical for, relative to the repository root."""

    argv: tuple[str, ...]
    cwd: str
    format: str


# Parallel unit runs (docs/test-run-records.md#parallel-unit-runs). The pytest-unit command runs
# under pytest-xdist with `--dist loadfile`, so each test module stays in one worker and its module
# and session fixtures behave as they do in one process. `{workers}` is replaced at run time by
# pytest_workers(): $PYTEST_WORKERS when set (0, one process with no workers, up to the cap), else
# min(PYTEST_WORKERS_CAP, physical cores - PYTEST_WORKERS_RESERVED), at least 1.
# The command names UNIT_TESTS rather than relying on testpaths (tests/): pytest loads a conftest
# at startup only under a named path or a test* directory, and the xdist controller collects
# nothing, so without the path it never loads tests/unit/conftest.py. It would then register no
# report plugin and have no tests/unit on sys.path to rebuild a worker's warning whose category a
# test module defines. tests/unit/test_run_record.py checks every unit-marked test is under it.
UNIT_TESTS = "tests/unit"
WORKERS = "{workers}"
WORKERS_VARIABLE = "PYTEST_WORKERS"
PYTEST_WORKERS_CAP = 4
PYTEST_WORKERS_RESERVED = 2
_WORKER_COUNT = re.compile(r"[0-9]+")


CANONICAL: dict[str, Canonical] = {
    "pytest-unit": Canonical(
        (
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "unit",
            "-n",
            WORKERS,
            "--dist",
            "loadfile",
            f"--junitxml={STAGING}/junit.xml",
            UNIT_TESTS,
        ),
        ".",
        "pytest",
    ),
    "frontend-test": Canonical(
        (
            "npm",
            "test",
            "--",
            "--reporter=default",
            "--reporter=json",
            f"--outputFile.json={STAGING}/vitest.json",
        ),
        "frontend",
        "vitest-json",
    ),
    "infra-test": Canonical(
        ("npm", "test", "--", "--json", f"--outputFile={STAGING}/jest.json"),
        "infra",
        "jest-json",
    ),
}
# Gate kinds run these make targets in order and stop at the first failure.
CANONICAL_GATES: dict[str, tuple[str, ...]] = {
    "verify": ("lint", "typecheck", "lint-boundaries", "validate-contracts", "test"),
    "verify-clean": ("verify-clean-run",),
}


class RunRecordError(Exception):
    """The record could not be written or read as the contract requires."""


class RecordExistsError(RunRecordError):
    """The record directory already exists; a record is never overwritten or merged."""


# --------------------------------------------------------------------------------------------
# Git and environment
# --------------------------------------------------------------------------------------------


def _capture(argv: list[str], cwd: Path | None = None) -> str | None:
    """Return the stripped stdout of a fixed argv, or None when it fails or is absent."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed tool argv, no shell
            argv, cwd=cwd, capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def linux_physical_cores(cpuinfo: str) -> int | None:
    """Return the distinct (physical id, core id) pairs in /proc/cpuinfo text, or None."""
    cores: set[tuple[str, str]] = set()
    for block in cpuinfo.split("\n\n"):
        fields = {
            key.strip(): value.strip()
            for key, sep, value in (line.partition(":") for line in block.splitlines())
            if sep
        }
        if "core id" in fields:
            cores.add((fields.get("physical id", "0"), fields["core id"]))
    return len(cores) or None


def physical_cores() -> int:
    """Return the machine's physical core count, else its logical count, else 1."""
    if sys.platform == "darwin":
        found = _capture(["/usr/sbin/sysctl", "-n", "hw.physicalcpu"])
        if found and _WORKER_COUNT.fullmatch(found) and int(found) > 0:
            return int(found)
    elif sys.platform.startswith("linux"):
        try:
            cores = linux_physical_cores(Path("/proc/cpuinfo").read_text(encoding="utf-8"))
        except OSError:
            cores = None
        if cores:
            return cores
    return os.cpu_count() or 1


def pytest_workers(env: dict[str, str], cores: Callable[[], int] = physical_cores) -> int:
    """Return the pytest-unit worker count; raise ValueError for a malformed $PYTEST_WORKERS."""
    configured = env.get(WORKERS_VARIABLE, "").strip()
    if not configured:
        return max(1, min(PYTEST_WORKERS_CAP, cores() - PYTEST_WORKERS_RESERVED))
    if not _WORKER_COUNT.fullmatch(configured) or int(configured) > PYTEST_WORKERS_CAP:
        raise ValueError(
            f"{WORKERS_VARIABLE} must be a whole number from 0 to {PYTEST_WORKERS_CAP}, "
            f"not {configured!r}"
        )
    return int(configured)


def toplevel(cwd: Path) -> Path | None:
    """Return the repository root containing `cwd`, or None outside a repository."""
    found = _capture(["git", "rev-parse", "--show-toplevel"], cwd)
    return Path(found) if found else None


def record_root(root: Path | None = None, cwd: Path | None = None) -> Path:
    """Return the record root: `root`, else $RUN_RECORD_ROOT, else <toplevel>/.runs."""
    if root is not None:
        return root
    configured = os.environ.get(ROOT_VARIABLE)
    if configured:
        return Path(configured)
    top = toplevel(cwd or Path.cwd())
    if top is None:
        raise RunRecordError(f"not in a git repository and {ROOT_VARIABLE} is unset")
    return top / ".runs"


def porcelain_count(repo: Path | None) -> int | None:
    """Return the number of `git status --porcelain` lines, or None without a repository."""
    if repo is None:
        return None
    out = _capture(["git", "status", "--porcelain"], repo)
    if out is None:
        return None
    return len([line for line in out.splitlines() if line.strip()])


def git_state(repo: Path | None) -> dict[str, Any]:
    """Return head, tree and branch of `repo` (None values outside a repository)."""
    if repo is None:
        return {"head": None, "tree": UNKNOWN_TREE, "branch": None}
    head = _capture(["git", "rev-parse", "HEAD"], repo)
    tree = _capture(["git", "rev-parse", "HEAD^{tree}"], repo) if head else None
    branch = _capture(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo) if head else None
    return {"head": head, "tree": tree or UNKNOWN_TREE, "branch": branch}


def provenance(repo: Path | None) -> dict[str, Any]:
    """Return head, tree, branch, source_commit and context: the run.json provenance of `repo`."""
    state = git_state(repo)
    source = os.environ.get(SOURCE_VARIABLE)
    return {
        **state,
        "source_commit": source or state["head"],
        "context": "clean-export" if source else "checkout",
    }


def _version_of(argv: list[str]) -> str | None:
    out = _capture(argv)
    if not out:
        return None
    match = re.search(r"\d+\.\d+(?:\.\d+)?", out.splitlines()[0])
    return match.group(0) if match else out.splitlines()[0]


def tool_versions(cwd: Path, pytest_version: str | None) -> dict[str, str | None]:
    """Return the versions of the interpreter, the runners and the tools a record depends on."""
    versions: dict[str, str | None] = {"python": platform.python_version()}
    if pytest_version is not None:
        versions["pytest"] = pytest_version
    versions["node"] = _version_of(["node", "--version"])
    for package in NODE_PACKAGES:
        manifest = cwd / "node_modules" / package / "package.json"
        if manifest.is_file():
            try:
                version = json.loads(manifest.read_text()).get("version")
            except OSError, ValueError:
                version = None
            versions[package] = version if isinstance(version, str) else None
    versions["uv"] = _version_of(["uv", "--version"])
    versions["git"] = _version_of(["git", "--version"])
    versions["platform"] = platform.platform()
    return versions


# --------------------------------------------------------------------------------------------
# Derived fields
# --------------------------------------------------------------------------------------------


def nearest_rank(values: list[float], quantile: float) -> float | None:
    """Return the nearest-rank quantile of `values`, or None when there are none."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _entries(
    tests: list[dict[str, Any]] | None, steps: list[dict[str, Any]] | None
) -> list[tuple[str, str, float | None]]:
    """Return (id, outcome, duration) per test, or per step for a gate record."""
    if tests is not None:
        return [(t["node_id"], t["outcome"], t.get("duration_s")) for t in tests]
    return [
        (s["name"], "passed" if s["exit_code"] == 0 else "failed", s.get("duration_s"))
        for s in steps or []
    ]


def totals(tests: list[dict[str, Any]]) -> dict[str, int]:
    """Return the test, passed, failed, skipped and error counts."""
    outcomes = [t["outcome"] for t in tests]
    return {
        "tests": len(outcomes),
        "passed": outcomes.count("passed"),
        "failed": outcomes.count("failed"),
        "skipped": outcomes.count("skipped"),
        "errors": outcomes.count("error"),
    }


def durations(entries: list[tuple[str, str, float | None]]) -> dict[str, float | None]:
    """Return the nearest-rank p50 and p95 and the maximum of the entry durations."""
    values = [d for _, _, d in entries if d is not None]
    return {
        "p50": nearest_rank(values, 0.50),
        "p95": nearest_rank(values, 0.95),
        "max": max(values) if values else None,
    }


def slowest(entries: list[tuple[str, str, float | None]]) -> list[dict[str, Any]]:
    """Return the `SLOWEST` longest entries, longest first."""
    timed = [(d, i) for i, _, d in entries if d is not None]
    timed.sort(key=lambda pair: (-pair[0], pair[1]))
    return [{"node_id": i, "duration_s": d} for d, i in timed[:SLOWEST]]


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _obj(value: object) -> dict[str, Any]:
    """Return `value` as a JSON object, or an empty one when it is not an object."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _seq(value: object) -> list[Any]:
    """Return `value` as a JSON array, or an empty one when it is not an array."""
    return cast("list[Any]", value) if isinstance(value, list) else []


def compute_delta(
    previous: Path | None,
    run: dict[str, Any],
    entries: list[tuple[str, str, float | None]],
) -> dict[str, Any] | None:
    """Return the change against `previous` (the latest earlier record of the same kind)."""
    if previous is None:
        return None
    prev_run = _obj(_load_json(previous / "run.json"))
    if (previous / "tests.json").is_file():
        prev_entries = _entries(_load_json(previous / "tests.json"), None)
    else:
        prev_entries = _entries(None, _load_json(previous / "steps.json"))
    before = {i: o for i, o, _ in prev_entries}
    now = {i: o for i, o, _ in entries}
    cur_totals, prev_totals = _obj(run.get("totals")), _obj(prev_run.get("totals"))
    totals_change = (
        {k: int(v) - int(prev_totals.get(k, 0)) for k, v in cur_totals.items()}
        if cur_totals and prev_totals
        else None
    )
    cur_p95 = _obj(run.get("durations")).get("p95")
    prev_p95 = _obj(prev_run.get("durations")).get("p95")
    return {
        "previous": f"{previous.parent.name}/{previous.name}",
        "previous_tree": prev_run.get("tree"),
        "totals_change": totals_change,
        "newly_failing": sorted(
            i for i, o in now.items() if o in FAILING and before.get(i) in {"passed", "skipped"}
        ),
        "newly_passing": sorted(
            i for i, o in now.items() if o == "passed" and before.get(i) in FAILING
        ),
        "added": sorted(set(now) - set(before)),
        "removed": sorted(set(before) - set(now)),
        "p95_change_s": (
            round(cur_p95 - prev_p95, 6) if cur_p95 is not None and prev_p95 is not None else None
        ),
    }


# --------------------------------------------------------------------------------------------
# Writing, verifying and finding records
# --------------------------------------------------------------------------------------------


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_files(directory: Path) -> tuple[dict[str, Path], list[str]]:
    """Return every file under `directory` except manifest.json, plus non-regular entries."""
    files: dict[str, Path] = {}
    irregular: list[str] = []
    for path in sorted(directory.rglob("*")):
        rel = path.relative_to(directory).as_posix()
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            irregular.append(rel)
        elif path.is_file() and rel != "manifest.json":
            files[rel] = path
    return files, irregular


def _check_relpath(rel: str) -> None:
    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or ".." in parts or rel in RESERVED:
        raise RunRecordError(f"invalid record file name: {rel!r}")


def _derive(
    record: dict[str, Any],
    tests: list[dict[str, Any]] | None,
    entries: list[tuple[str, str, float | None]],
) -> None:
    """Fill totals, durations, slowest and outcome where the caller did not set them."""
    record.setdefault("totals", totals(tests) if tests is not None else None)
    record.setdefault("durations", durations(entries))
    record.setdefault("slowest", slowest(entries))
    if "outcome" not in record:
        counts = _obj(record["totals"])
        bad = int(counts.get("failed", 0)) + int(counts.get("errors", 0))
        ok = record.get("exit_code") == 0 and not record.get("report_missing") and bad == 0
        record["outcome"] = "pass" if ok else "fail"


def _replace(path: Path, data: bytes) -> None:
    """Write `path` whole through a sibling temporary file, so a reader never sees half of it."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


Progress = Callable[[], dict[str, Any] | None]


class LiveRecord:
    """One record from its start to its seal (docs/test-run-records.md#live-status).

    `begin` creates the directory with run.json `running`, an empty append-only console.log and a
    first status.json; a daemon thread then rewrites status.json every HEARTBEAT_INTERVAL_S.
    `finalise` writes the outcome, the reports and the manifest and makes the record read-only.
    A process that dies before `finalise` leaves the record `running`, which `record_state`
    reports as `abandoned` once the heartbeat is stale; no gate ever accepts it.
    """

    def __init__(  # noqa: PLR0913, PLR0917 - the parts begin() resolves
        self,
        kind: str,
        root: Path,
        directory: Path,
        start: dict[str, Any],
        progress: Progress | None,
    ) -> None:
        """Hold an already created record directory; use `begin` to create one."""
        self.kind = kind
        self.root = root
        self.directory = directory
        self.start = start
        self.progress = progress
        self.step: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._bytes = 0
        fd = os.open(
            directory / "console.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o644
        )
        self._console: IO[bytes] = os.fdopen(fd, "ab", buffering=0)

    @classmethod
    def begin(  # noqa: PLR0913 - the record's identity, then keyword-only options
        cls,
        kind: str,
        run: dict[str, Any],
        *,
        root: Path | None = None,
        now: datetime | None = None,
        progress: Progress | None = None,
        heartbeat: bool = True,
    ) -> LiveRecord:
        """Create the record directory and start its heartbeat; RecordExistsError if it exists."""
        if not KIND_NAME.match(kind):
            raise RunRecordError(f"invalid kind: {kind!r}")
        root = record_root(root)
        tree = str(run.get("tree") or UNKNOWN_TREE)
        if not re.fullmatch(r"[0-9a-f]{12,}", tree):
            raise RunRecordError(f"invalid tree: {tree!r}")
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        kind_dir = root / kind
        kind_dir.mkdir(parents=True, exist_ok=True)
        directory = kind_dir / f"{moment.strftime('%Y%m%dT%H%M%S.%fZ')}-{tree[:12]}"
        try:
            directory.mkdir()
        except FileExistsError as error:
            raise RecordExistsError(f"record already exists: {directory}") from error
        start: dict[str, Any] = {
            "schema": SCHEMA,
            "kind": kind,
            "parent": os.environ.get(PARENT_VARIABLE) or None,
            "utc_start": moment.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            **run,
        }
        start.update(schema=SCHEMA, kind=kind, tree=tree, status=RUNNING, pid=os.getpid())
        _replace(directory / "run.json", _json_bytes(start))
        live = cls(kind, root, directory, start, progress)
        live.beat()
        if heartbeat:
            live._thread = threading.Thread(target=live._heartbeat, daemon=True)
            live._thread.start()
        return live

    @property
    def name(self) -> str:
        """The record as `<kind>/<dir>`, the form child_records and parent use."""
        return f"{self.kind}/{self.directory.name}"

    def write(self, chunk: bytes) -> None:
        """Append `chunk` to console.log and flush it to disk."""
        with self._lock:
            self._console.write(chunk)
            self._bytes += len(chunk)

    def line(self, text: str) -> None:
        """Append one line of text to console.log."""
        self.write(f"{text}\n".encode())

    def set_step(self, step: str | None) -> None:
        """Name the gate step now running and write the heartbeat at once."""
        self.step = step
        self.beat()

    def _status(self, status: str) -> dict[str, Any]:
        try:
            progress = self.progress() if self.progress is not None else None
        except OSError, ValueError:
            progress = None
        return {
            "schema": SCHEMA,
            "kind": self.kind,
            "status": status,
            "pid": self.start["pid"],
            "utc_start": self.start.get("utc_start"),
            "updated_utc": _utc(),
            "updated_epoch": time.time(),
            "step": self.step,
            "progress": progress,
            "console_bytes": self._bytes,
        }

    def beat(self) -> None:
        """Rewrite status.json with the current time, step and progress."""
        with self._lock:
            _replace(self.directory / "status.json", _json_bytes(self._status(RUNNING)))

    def _heartbeat(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            try:
                self.beat()
            except OSError:
                # A full disk or a removed directory must not kill the run it describes.
                continue

    def finalise(  # noqa: PLR0913 - the outcome, then keyword-only contents
        self,
        run: dict[str, Any],
        *,
        tests: list[dict[str, Any]] | None = None,
        steps: list[dict[str, Any]] | None = None,
        files: dict[str, bytes] | None = None,
    ) -> Path:
        """Seal the record: outcome, reports, manifest, read-only. It never changes again.

        `run` carries the caller's fields; schema, kind, totals, durations, slowest, outcome,
        status and delta are filled in when absent. Exactly one of `tests` or `steps`.
        """
        if (tests is None) == (steps is None):
            raise RunRecordError("a record holds exactly one of tests or steps")
        for rel in files or {}:
            _check_relpath(rel)
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self._lock:
            self._console.close()

        entries = _entries(tests, steps)
        record: dict[str, Any] = {**self.start, **run}
        record.update(schema=SCHEMA, kind=self.kind, tree=self.start["tree"], pid=self.start["pid"])
        _derive(record, tests, entries)
        record["status"] = "passed" if record["outcome"] == "pass" else "failed"
        if "delta" not in record:
            previous = latest_record(self.root, self.kind, exclude=self.directory)
            record["delta"] = compute_delta(previous, record, entries)

        contents: dict[str, bytes] = {}
        if tests is not None:
            contents["tests.json"] = _json_bytes(tests)
        else:
            contents["steps.json"] = _json_bytes(steps)
        contents.update(files or {})
        for rel, data in contents.items():
            target = self.directory / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(data)
        _replace(self.directory / "status.json", _json_bytes(self._status(record["status"])))
        _replace(self.directory / "run.json", _json_bytes(record))
        written, _ = _record_files(self.directory)
        manifest = {
            "schema": SCHEMA,
            "files": {
                rel: {"sha256": _sha256(path), "bytes": path.stat().st_size}
                for rel, path in written.items()
            },
        }
        with (self.directory / "manifest.json").open("xb") as handle:
            handle.write(_json_bytes(manifest))
        for current, _dirs, names in os.walk(self.directory, topdown=False):
            for name in names:
                Path(current, name).chmod(0o444)
            Path(current).chmod(0o555)
        return self.directory


def write_record(  # noqa: PLR0913 - the contract's fields, passed by keyword
    kind: str,
    run: dict[str, Any],
    *,
    tests: list[dict[str, Any]] | None = None,
    steps: list[dict[str, Any]] | None = None,
    files: dict[str, bytes] | None = None,
    root: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Write one immutable record at once and return its directory.

    For a run that is already over: begin and finalise in one call, with `files["console.log"]`
    (when given) as the whole console. Raises RecordExistsError when the directory exists.
    """
    if (tests is None) == (steps is None):
        raise RunRecordError("a record holds exactly one of tests or steps")
    rest = dict(files or {})
    console = rest.pop("console.log", b"")
    for rel in rest:
        _check_relpath(rel)
    live = LiveRecord.begin(kind, run, root=root, now=now, heartbeat=False)
    live.write(console)
    return live.finalise(run, tests=tests, steps=steps, files=rest)


def verify_record(path: Path) -> list[str]:
    """Return every way `path` differs from its manifest; an empty list means it verifies."""
    if not path.is_dir():
        return [f"{path}: not a record directory"]
    manifest_path = path / "manifest.json"
    try:
        manifest = _load_json(manifest_path)
    except (OSError, ValueError) as error:
        return [f"{manifest_path}: unreadable manifest ({error})"]
    if _obj(manifest).get("schema") != SCHEMA:
        return [f"{manifest_path}: not a schema {SCHEMA} manifest"]
    listed = _obj(_obj(manifest).get("files"))
    if not listed:
        return [f"{manifest_path}: no files map"]
    actual, irregular = _record_files(path)
    problems = [f"{rel}: not a regular file" for rel in irregular]
    problems += [f"{rel}: missing" for rel in sorted(set(listed) - set(actual))]
    problems += [f"{rel}: not in the manifest" for rel in sorted(set(actual) - set(listed))]
    for rel in sorted(set(listed) & set(actual)):
        expected = _obj(listed[rel])
        file_path = actual[rel]
        if expected.get("bytes") != file_path.stat().st_size:
            problems.append(f"{rel}: size differs from the manifest")
        if expected.get("sha256") != _sha256(file_path):
            problems.append(f"{rel}: sha256 differs from the manifest")
    if "run.json" not in listed:
        problems.append("run.json: missing")
    elif "run.json" in actual and not problems:
        status = _read_object(actual["run.json"]).get("status")
        # Records sealed before live status carry no status; a sealed one never says running.
        if status is not None and status not in FINAL_STATUSES:
            problems.append(f"run.json: status {status!r} is not final")
    return problems


def _alive(pid: object) -> bool:
    """Return whether a process with this pid exists on this machine."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_object(path: Path) -> dict[str, Any]:
    try:
        return _obj(_load_json(path))
    except OSError, ValueError:
        return {}


def record_state(directory: Path, *, now: float | None = None) -> str:
    """Return passed, failed, invalid (sealed); running, stalled or abandoned (not sealed).

    Only a sealed record that verifies is passed or failed. An unsealed one is running while its
    heartbeat is at most STALE_AFTER_S old; after that it is stalled while its process exists
    and abandoned once it is gone.
    """
    if (directory / "manifest.json").exists():
        if verify_record(directory):
            return "invalid"
        run = _read_object(directory / "run.json")
        status = run.get("status")
        if status in FINAL_STATUSES:
            return str(status)
        return "passed" if run.get("outcome") == "pass" else "failed"
    status = _read_object(directory / "status.json")
    updated = status.get("updated_epoch")
    if not isinstance(updated, (int, float)):
        try:
            updated = directory.stat().st_mtime
        except OSError:
            return "abandoned"
    age = (time.time() if now is None else now) - float(updated)
    if age <= STALE_AFTER_S:
        return RUNNING
    pid = status.get("pid", _read_object(directory / "run.json").get("pid"))
    return "stalled" if _alive(pid) else "abandoned"


def record_dirs(root: Path, kind: str | None = None) -> list[Path]:
    """Return every record directory under `root` (of `kind` when given), oldest first.

    Only names matching RECORD_NAME count, so other files under a kind directory (a coverage map,
    a worksheets/ directory) are never mistaken for records.
    """
    if not root.is_dir():
        return []
    kinds = [root / kind] if kind else [p for p in root.iterdir() if p.is_dir()]
    found: list[Path] = []
    for kind_dir in kinds:
        if kind_dir.is_dir():
            found += [p for p in kind_dir.iterdir() if p.is_dir() and RECORD_NAME.match(p.name)]
    return sorted(found, key=lambda p: (p.name, p.parent.name))


def find_records(  # noqa: PLR0913 - independent optional filters
    root: Path,
    kind: str,
    *,
    tree: str | None = None,
    passing: bool | None = None,
    canonical: bool | None = None,
    clean: bool | None = None,
) -> list[Path]:
    """Return the verified records of `kind` matching every given filter, newest first."""
    matches: list[Path] = []
    for directory in reversed(record_dirs(root, kind)):
        if verify_record(directory):
            continue
        run = _load_json(directory / "run.json")
        if tree is not None and run.get("tree") != tree:
            continue
        if passing is not None and (run.get("outcome") == "pass") != passing:
            continue
        if canonical is not None and bool(run.get("canonical")) != canonical:
            continue
        if clean is not None and bool(run.get("clean")) != clean:
            continue
        matches.append(directory)
    return matches


def latest_record(root: Path, kind: str, exclude: Path | None = None) -> Path | None:
    """Return the newest verified record of `kind`, skipping `exclude`."""
    for directory in reversed(record_dirs(root, kind)):
        if exclude is not None and directory.resolve() == exclude.resolve():
            continue
        if not verify_record(directory):
            return directory
    return None


# --------------------------------------------------------------------------------------------
# Report parsing
# --------------------------------------------------------------------------------------------


def _relative(name: str, repo: Path | None) -> str:
    path = Path(name)
    if repo is not None and path.is_absolute():
        try:
            return path.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix()


_JS_OUTCOMES = {
    "passed": "passed",
    "failed": "failed",
    "pending": "skipped",
    "skipped": "skipped",
    "todo": "skipped",
    "disabled": "skipped",
    "focused": "passed",
}


def parse_js_report(data: dict[str, Any], repo: Path | None) -> list[dict[str, Any]]:
    """Return tests.json entries from a jest `--json` or vitest json reporter document."""
    tests: list[dict[str, Any]] = []
    for result in map(_obj, _seq(data.get("testResults"))):
        relpath = _relative(str(result.get("name", "")), repo)
        assertions = [_obj(a) for a in _seq(result.get("assertionResults"))]
        if not assertions and result.get("status") == "failed":
            # The file failed before any test ran (an import or compile error).
            tests.append(
                {
                    "node_id": relpath,
                    "file": relpath,
                    "summary": "test file failed to run",
                    "decision_ids": [],
                    "outcome": "error",
                    "duration_s": None,
                    "retries": 0,
                }
            )
            continue
        for assertion in assertions:
            ancestors = " > ".join(str(a) for a in _seq(assertion.get("ancestorTitles")))
            title = str(assertion.get("title", ""))
            node_id = "::".join(p for p in (relpath, ancestors, title) if p)
            duration = assertion.get("duration")
            invocations = assertion.get("invocations")
            if "retryReasons" in assertion:
                retries = len(_seq(assertion["retryReasons"]))
            elif isinstance(invocations, int) and invocations > 1:
                retries = invocations - 1
            else:
                retries = 0
            tests.append(
                {
                    "node_id": node_id,
                    "file": relpath,
                    "summary": title,
                    "decision_ids": [],
                    "outcome": _JS_OUTCOMES.get(str(assertion.get("status")), "error"),
                    "duration_s": (
                        round(duration / 1000, 6) if isinstance(duration, (int, float)) else None
                    ),
                    "retries": retries,
                }
            )
    return tests


def parse_report(
    fmt: str, staging: Path, repo: Path | None
) -> tuple[list[dict[str, Any]], str | None]:
    """Return (tests, pytest version) from the staged report; raise ValueError when absent."""
    if fmt == EXIT_FORMAT:
        return [], None
    report = staging / REPORT_FILES[fmt]
    document = _obj(_load_json(report))
    if not document:
        raise ValueError(f"{report.name} is not a report object")
    if fmt == "pytest":
        if not isinstance(document.get("tests"), list):
            raise ValueError(f"{report.name} has no tests list")
        version = document.get("pytest")
        return [_obj(t) for t in _seq(document["tests"])], str(version) if version else None
    return parse_js_report(document, repo), None


# --------------------------------------------------------------------------------------------
# Running children
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Tee:
    """Copies a child's stream to the terminal unchanged and into one shared console log."""

    captured: list[bytes]
    lock: threading.Lock
    sink: Callable[[bytes], None] | None = None

    def pump(self, source: IO[bytes], terminal: IO[bytes]) -> None:
        """Forward every chunk of `source` to the terminal and to the sink (else the log)."""
        fd = source.fileno()
        while chunk := os.read(fd, 1 << 16):
            terminal.write(chunk)
            terminal.flush()
            with self.lock:
                if self.sink is not None:
                    self.sink(chunk)
                else:
                    self.captured.append(chunk)


def run_child(  # noqa: PLR0913 - argv, cwd and env, then the keyword-only sink
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    sink: Callable[[bytes], None] | None = None,
) -> tuple[int, bytes]:
    """Run `argv`, streaming stdout and stderr unchanged while teeing both; return (code, log).

    With a `sink`, every chunk goes to it as it arrives and the returned log is empty.
    """
    captured: list[bytes] = []
    lock = threading.Lock()
    try:
        process = subprocess.Popen(  # noqa: S603 - argv from CANONICAL or the operator
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as error:
        message = f"run-record: cannot start {argv[0]}: {error}\n".encode()
        sys.stderr.buffer.write(message)
        if sink is not None:
            sink(message)
            return 127, b""
        return 127, message
    if process.stdout is None or process.stderr is None:
        raise RunRecordError("the child's output pipes were not opened")
    tee = _Tee(captured, lock, sink)
    pumps = [
        threading.Thread(target=tee.pump, args=(process.stdout, sys.stdout.buffer)),
        threading.Thread(target=tee.pump, args=(process.stderr, sys.stderr.buffer)),
    ]
    for pump in pumps:
        pump.start()
    try:
        code = process.wait()
    except KeyboardInterrupt:
        code = process.wait()
    for pump in pumps:
        pump.join()
    return (128 - code if code < 0 else code), b"".join(captured)


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# One finished test file: jest prints `PASS <file>` or `FAIL <file>`; vitest's default reporter
# prints a check mark (U+2713), or a chevron (U+276F) or cross (U+00D7) with `| K failed`, then
# `<file> (N tests)`.
VITEST_PASSED = "\u2713"
_JEST_FILE = re.compile(r"^\s*(PASS|FAIL)\s+\S")
_VITEST_FILE = re.compile(
    r"^\s*([\u2713\u276f\u00d7])\s+\S+\s+\((\d+) tests?(?:\s*\|\s*(\d+) failed)?"
)


@dataclass
class JsProgress:
    """Counts the test files a jest or vitest run reports finished, from its console stream."""

    files_passed: int = 0
    files_failed: int = 0
    tests_done: int = 0
    tests_failed: int = 0
    _pending: bytes = b""

    def feed(self, chunk: bytes) -> None:
        """Read the complete lines of `chunk`; a partial last line waits for the next chunk."""
        data = self._pending + chunk
        *lines, self._pending = data.split(b"\n")
        for raw in lines:
            text = _ANSI.sub("", raw.decode("utf-8", "replace")).rsplit("\r", 1)[-1]
            if jest := _JEST_FILE.match(text):
                if jest.group(1) == "PASS":
                    self.files_passed += 1
                else:
                    self.files_failed += 1
            elif vitest := _VITEST_FILE.match(text):
                failed = int(vitest.group(3) or 0)
                if vitest.group(1) == VITEST_PASSED and not failed:
                    self.files_passed += 1
                else:
                    self.files_failed += 1
                self.tests_done += int(vitest.group(2))
                self.tests_failed += failed

    def counts(self) -> dict[str, Any]:
        """Return the counts so far, for the heartbeat."""
        counts: dict[str, Any] = {
            "files_passed": self.files_passed,
            "files_failed": self.files_failed,
        }
        if self.tests_done:
            counts.update(tests_done=self.tests_done, tests_failed=self.tests_failed)
        return counts


def pytest_progress(path: Path) -> Progress:
    """Return a heartbeat reader of the progress file tests/unit/run_record_report.py writes."""

    def read() -> dict[str, Any] | None:
        if not path.is_file():
            return None
        return _obj(_load_json(path)) or None

    return read


@dataclass
class _Recording:
    """One recorded invocation: the repository it ran in and the run.json fields so far."""

    kind: str
    canonical: bool
    argv: list[str]
    cwd: Path
    repo: Path | None
    run: dict[str, Any] = field(default_factory=dict[str, Any])
    started: float = 0.0

    def begin(self) -> None:
        """Capture the git state and the porcelain count before the command starts."""
        self.run = {
            "schema": SCHEMA,
            "kind": self.kind,
            "canonical": self.canonical,
            "argv": self.argv,
            "cwd": self._display_cwd(),
            **provenance(self.repo),
            "porcelain_before": porcelain_count(self.repo),
            "utc_start": _utc(),
        }
        self.started = time.monotonic()

    def start(self, root: Path, progress: Progress | None) -> LiveRecord | int:
        """Create the running record, or return the exit code when it cannot be created."""
        try:
            live = LiveRecord.begin(self.kind, self.run, root=root, progress=progress)
        except RecordExistsError as error:
            sys.stderr.write(f"run-record: refused: {error}\n")
            return EXIT_RECORD_EXISTS
        except (RunRecordError, OSError) as error:
            sys.stderr.write(f"run-record: the record could not be started: {error}\n")
            return EXIT_RECORD_FAILED
        sys.stdout.write(f"run-record: running {live.directory}\n")
        sys.stdout.flush()
        return live

    def finish(self, exit_code: int) -> None:
        """Capture the end time, the porcelain count after, and the exit code."""
        run = self.run
        run["utc_end"] = _utc()
        run["duration_s"] = round(time.monotonic() - self.started, 6)
        run["porcelain_after"] = porcelain_count(self.repo)
        run["clean"] = run["porcelain_before"] == 0 and run["porcelain_after"] == 0
        run["exit_code"] = exit_code

    def seal(self, live: LiveRecord, exit_code: int, **record: Any) -> int:
        """Finalise the record; return the exit code the command must end with."""
        try:
            directory = live.finalise(self.run, **record)
        except (RunRecordError, OSError) as error:
            sys.stderr.write(f"run-record: the record could not be written: {error}\n")
            return exit_code or EXIT_RECORD_FAILED
        sys.stdout.write(f"run-record: {directory}\n")
        sys.stdout.flush()
        return exit_code

    def _display_cwd(self) -> str:
        if self.repo is not None:
            try:
                return self.cwd.resolve().relative_to(self.repo.resolve()).as_posix() or "."
            except ValueError:
                pass
        return str(self.cwd)


def _usage(message: str) -> int:
    sys.stderr.write(f"run-record: {message}\n")
    return EXIT_USAGE


def _pytest_version(fmt: str) -> str | None:
    if fmt != "pytest":
        return None
    try:
        return metadata.version("pytest")
    except metadata.PackageNotFoundError:
        return None


def _progress_source(
    fmt: str, staging: Path, env: dict[str, str]
) -> tuple[JsProgress | None, Progress | None]:
    """Return the console counter (JS runners) and the heartbeat's progress reader for `fmt`."""
    if fmt == "pytest":
        env[PYTEST_REPORT_VARIABLE] = str(staging / REPORT_FILES["pytest"])
        env[PYTEST_PROGRESS_VARIABLE] = str(staging / PYTEST_PROGRESS_FILE)
        return None, pytest_progress(staging / PYTEST_PROGRESS_FILE)
    if fmt in {"vitest-json", "jest-json"}:
        js = JsProgress()
        return js, js.counts
    return None, None


def _raw_files(staging: Path) -> dict[str, bytes]:
    """Return the runner's reports in the staging directory, as the record's raw/ files."""
    return {
        f"raw/{path.relative_to(staging).as_posix()}": path.read_bytes()
        for path in sorted(staging.rglob("*"))
        if path.is_file() and path.name != PYTEST_PROGRESS_FILE
    }


@dataclass(frozen=True)
class _Command:
    """The command a run executes: its template, format, working directory and worker count."""

    template: list[str]
    format: str
    cwd: Path
    workers: int | None = None


def _command(  # noqa: PLR0913, PLR0917 - the parsed args and argv, then where the run starts
    args: argparse.Namespace, argv: list[str], here: Path, repo: Path | None
) -> _Command:
    """Return the canonical command of the kind (argv empty) or the given one; ValueError if not."""
    kind: str = args.kind
    if argv:
        if args.format is None:
            raise ValueError("a run with an argv needs --format")
        return _Command(argv, args.format, Path(args.cwd).resolve() if args.cwd else here)
    spec = CANONICAL.get(kind)
    if spec is None:
        raise ValueError(f"kind {kind!r} has no canonical command; give --format and -- argv")
    if args.format is not None or args.cwd is not None:
        raise ValueError("a canonical run takes no --format or --cwd")
    workers = pytest_workers(dict(os.environ)) if WORKERS in spec.argv else None
    return _Command(list(spec.argv), spec.format, (repo or here) / spec.cwd, workers)


def command_run(args: argparse.Namespace, argv: list[str]) -> int:
    """Run one test command (canonical when argv is empty) and record it as it runs."""
    kind: str = args.kind
    here = Path.cwd()
    repo = toplevel(here)
    canonical = not argv
    try:
        command = _command(args, argv, here, repo)
        root = record_root(cwd=here)
    except (ValueError, RunRecordError) as error:
        return _usage(str(error))
    template, fmt, cwd, workers = command.template, command.format, command.cwd, command.workers

    recording = _Recording(kind, canonical, template, cwd, repo)
    staging = Path(tempfile.mkdtemp(prefix="run-record-"))
    try:
        child_argv = [
            str(workers)
            if arg == WORKERS and workers is not None
            else arg.replace(STAGING, str(staging))
            for arg in template
        ]
        env = dict(os.environ)
        js, progress = _progress_source(fmt, staging, env)
        recording.begin()
        if workers is not None:
            recording.run["workers"] = workers
        live = recording.start(root, progress)
        if isinstance(live, int):
            return live

        def sink(chunk: bytes) -> None:
            live.write(chunk)
            if js is not None:
                js.feed(chunk)

        exit_code, _ = run_child(child_argv, cwd, env, sink=sink)
        recording.finish(exit_code)
        try:
            tests, pytest_version = parse_report(fmt, staging, repo)
            recording.run["report_missing"] = False
        except OSError, ValueError, KeyError, TypeError:
            tests, pytest_version = [], None
            recording.run["report_missing"] = True
        recording.run["tool_versions"] = tool_versions(cwd, pytest_version or _pytest_version(fmt))
        return recording.seal(live, exit_code, tests=tests, files=_raw_files(staging))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def command_gate(kind: str, targets: list[str]) -> int:
    """Run `make <target>` per step, stop at the first failure, and record the gate as it runs."""
    here = Path.cwd()
    canonical_targets = CANONICAL_GATES.get(kind)
    if not targets:
        if canonical_targets is None:
            return _usage(f"gate kind {kind!r} has no canonical steps; give -- targets")
        targets = list(canonical_targets)
    try:
        root = record_root(cwd=here)
    except RunRecordError as error:
        return _usage(str(error))
    canonical = canonical_targets is not None and tuple(targets) == canonical_targets
    recording = _Recording(kind, canonical, ["make", *targets], here, toplevel(here))
    steps: list[dict[str, Any]] = []
    recording.begin()
    live = recording.start(root, lambda: {"steps_done": len(steps), "steps_total": len(targets)})
    if isinstance(live, int):
        return live
    env = {**os.environ, PARENT_VARIABLE: live.name}
    exit_code = 0
    for target in targets:
        argv = ["make", "--no-print-directory", target]
        before = set(record_dirs(root))
        step_start = time.monotonic()
        live.set_step(target)
        code, _ = run_child(argv, here, env, sink=live.write)
        children = sorted(p.relative_to(root).as_posix() for p in set(record_dirs(root)) - before)
        steps.append(
            {
                "name": target,
                "argv": argv,
                "exit_code": code,
                "duration_s": round(time.monotonic() - step_start, 6),
                "child_records": children,
            }
        )
        if code != 0:
            exit_code = code
            break
    live.set_step(None)
    recording.finish(exit_code)
    recording.run["report_missing"] = False
    recording.run["tool_versions"] = tool_versions(here, None)
    recording.run["child_records"] = [c for step in steps for c in step["child_records"]]
    return recording.seal(live, exit_code, steps=steps)


def command_verify(directory: Path) -> int:
    """Print every manifest problem of `directory`; exit 0 only when there is none."""
    problems = verify_record(directory)
    for problem in problems:
        sys.stderr.write(f"run-record: {directory}: {problem}\n")
    if problems:
        return 1
    sys.stdout.write(f"run-record: verified {directory}\n")
    return 0


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    whole = max(0, round(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s" if hours else f"{minutes}m{secs:02d}s"


def _epoch(utc: object) -> float | None:
    if not isinstance(utc, str):
        return None
    try:
        return datetime.fromisoformat(utc.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def last_lines(console: Path, count: int) -> list[str]:
    """Return the last `count` non-blank lines of a console log, without terminal escapes."""
    try:
        with console.open("rb") as handle:
            handle.seek(max(0, console.stat().st_size - (1 << 15)))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = [_ANSI.sub("", line).rsplit("\r", 1)[-1].rstrip() for line in tail.splitlines()]
    return [line for line in lines if line.strip()][-count:] if count > 0 else []


def describe(directory: Path, state: str, now: float) -> str:
    """Return one status line: state, record, elapsed time, heartbeat age, step and progress."""
    run = _read_object(directory / "run.json")
    status = _read_object(directory / "status.json")
    started = _epoch(run.get("utc_start"))
    parts = [f"{state.upper():<9} {directory.parent.name}/{directory.name}"]
    if state in FINAL_STATUSES or state == "invalid":
        duration = run.get("duration_s")
        parts.append(f"took {_duration(duration if isinstance(duration, (int, float)) else None)}")
        return "  ".join(parts)
    updated = status.get("updated_epoch")
    beat = float(updated) if isinstance(updated, (int, float)) else None
    end = now if state in IN_PROGRESS_STATES else beat
    parts.append(f"elapsed {_duration(end - started if end and started else None)}")
    parts.append(f"heartbeat {_duration(now - beat if beat else None)} ago")
    if status.get("step"):
        parts.append(f"step {status['step']}")
    progress = _obj(status.get("progress"))
    if progress:
        parts.append("progress " + ", ".join(f"{k} {v}" for k, v in progress.items()))
    return "  ".join(parts)


def command_status(  # noqa: PLR0913 - the root, then keyword-only options
    root: Path, *, recent: int, lines: int, now: float | None = None
) -> int:
    """Print every in-progress record and the most recent abandoned and finished ones."""
    moment = time.time() if now is None else now
    states = [(d, record_state(d, now=moment)) for d in record_dirs(root)]
    states.sort(key=lambda pair: pair[0].name)
    live = [(d, s) for d, s in states if s in IN_PROGRESS_STATES]
    abandoned = [(d, s) for d, s in states if s == "abandoned"][-recent:] if recent > 0 else []
    done = [(d, s) for d, s in states if s not in IN_PROGRESS_STATES | {"abandoned"}]
    done = done[-recent:] if recent > 0 else []
    out = sys.stdout
    out.write(f"run-record: {len(live)} in progress under {root}\n")
    for title, group, with_log in (
        ("in progress", live, True),
        ("abandoned (never accepted)", abandoned, True),
        ("recent", done, False),
    ):
        if not group:
            continue
        out.write(f"{title}:\n")
        for directory, state in group:
            out.write(f"  {describe(directory, state, moment)}\n")
            if with_log:
                for text in last_lines(directory / "console.log", lines):
                    out.write(f"    | {text}\n")
    return 0


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911 - one return per command
    """Parse the command line and dispatch; see the module docstring."""
    args_in = list(sys.argv[1:] if argv is None else argv)
    rest: list[str] = []
    if "--" in args_in:
        split = args_in.index("--")
        args_in, rest = args_in[:split], args_in[split + 1 :]
    parser = argparse.ArgumentParser(
        prog="run_record.py", description=(__doc__ or "").splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="run one test command and record it")
    run_parser.add_argument("--kind", required=True)
    run_parser.add_argument("--format", choices=FORMATS)
    run_parser.add_argument("--cwd")
    gate_parser = sub.add_parser("gate", help="run make targets in order and record the gate")
    gate_parser.add_argument("--kind", required=True)
    verify_parser = sub.add_parser("verify", help="check a record against its manifest")
    verify_parser.add_argument("directory", type=Path)
    find_parser = sub.add_parser("find", help="list verified records, newest first")
    find_parser.add_argument("--kind", required=True)
    find_parser.add_argument("--tree")
    find_parser.add_argument("--passing", action="store_true", default=None)
    find_parser.add_argument("--canonical", action="store_true", default=None)
    find_parser.add_argument("--clean", action="store_true", default=None)
    status_parser = sub.add_parser("status", help="list in-progress and recent records")
    status_parser.add_argument("--recent", type=int, default=5)
    status_parser.add_argument("--lines", type=int, default=5)
    args = parser.parse_args(args_in)

    if args.command in {"run", "gate"} and not KIND_NAME.match(args.kind):
        sys.stderr.write(f"run-record: invalid kind {args.kind!r}\n")
        return EXIT_USAGE
    if args.command == "run":
        return command_run(args, rest)
    if args.command == "gate":
        return command_gate(args.kind, rest)
    if rest:
        parser.error("only run and gate take arguments after --")
    if args.command == "verify":
        return command_verify(args.directory)
    try:
        root = record_root()
    except RunRecordError as error:
        sys.stderr.write(f"run-record: {error}\n")
        return EXIT_USAGE
    if args.command == "status":
        return command_status(root, recent=args.recent, lines=args.lines)
    found = find_records(
        root,
        args.kind,
        tree=args.tree,
        passing=args.passing,
        canonical=args.canonical,
        clean=args.clean,
    )
    for directory in found:
        sys.stdout.write(f"{directory}\n")
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
