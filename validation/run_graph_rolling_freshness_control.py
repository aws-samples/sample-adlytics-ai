# SPDX-License-Identifier: MIT-0
"""Run the mandatory D7 single-root plus two-wave A/B rolling-freshness control (dev only).

    uv run python validation/run_graph_rolling_freshness_control.py --profile <profile> \
        --environment dev --region <region> --expected-account <account> \
        --data-stack <data-stack> --bucket <raw-bucket> --graph-store-stack <store-stack> \
        --manifest validation/fixtures/graph-rolling-freshness-control-v1.json \
        --acceptance-execution-id <id> --evidence-dir validation/evidence/<new-dir> \
        [--synthetic-full-plan]

Every child is the real `python -m adapters.jobs.land_fixtures` command as an argument tuple,
never a shell string, and every argv is first parsed by the real `_parse_args`. The protocol:

1. Parse the whole manifest, then dry-plan every entry twice; duplicate labels, sources,
   windows or keys, an exhausted slot, or a plan that differs between the two dry plans stops
   before any write.
2. Reserve each exact dry-planned key serially, in stable `(window, key, label)` order, with
   `--reserved-probe-key <key> --no-twin`.
3. Launch the single root with `--twin-only` and wait for it to hold its grant. Then launch
   wave A, so its twins become ready behind the single root's busy cohort; wait for the single
   root to reach a terminal state, strongly observe A's immutable snapshot `ACTIVE` (recording
   each twin's settle margin before the single cohort's finalize), and launch wave B while A
   stays active. Collect B's flips and settlements, A's before/after snapshot, A's final
   transition, and B's activation.

Each child must print exactly one `PROBE_TWIN <json>` line: mode GRAPH, sent, decision
`ADMITTED`, a nonblank run ID unique across the execution, and the reserved key. Anything else
stops further launches and atomically writes a sanitized `NOT_PROVEN` receipt. The optional
`synthetic_full_plan` runs only with `--synthetic-full-plan`. It is labelled synthetic load
characterization and never clears or blocks the hold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, cast

from botocore.exceptions import BotoCoreError, ClientError

_ROOT: Final = Path(__file__).resolve().parents[1]
_EVIDENCE_ROOT: Final = _ROOT / "validation" / "evidence"
MANIFEST_SCHEMA: Final = "graph-rolling-freshness-control-v1"
RECEIPT_SCHEMA: Final = "rolling-freshness-control-v1"
RECEIPT_NAME: Final = "rolling-freshness-control-v1.json"
MANDATORY_ROSTERS: Final = ("single_root", "wave_a", "wave_b")
SYNTHETIC_ROSTER: Final = "synthetic_full_plan"
ROSTERS: Final = (*MANDATORY_ROSTERS, SYNTHETIC_ROSTER)
CHILD_TIMEOUT_SECONDS: Final = 900
TERMINAL_TIMEOUT_SECONDS: Final = 7_200
DELIVERY_TIMEOUT_SECONDS: Final = 900
"""The bound on a reservation's own S3 delivery reaching its committed admission receipt."""
POLL_SECONDS: Final = 15.0
_ACCOUNT: Final = re.compile(r"^\d{12}$")
_PROBE_TWIN: Final = "PROBE_TWIN "
_PROBE_RETRIEVAL: Final = "PROBE_RETRIEVAL "
_TERMINAL_RUN_STATES: Final = frozenset({"SUCCEEDED", "FAILED", "SUPERSEDED", "CANCELLED"})
A_SNAPSHOT_FIELDS: Final = (
    "cohort_revision",
    "elected_run_ids",
    "winner_run_id",
    "elected_candidate_digest",
    "elected_frontier_digest",
    "elected_frontier_revisions",
    "elected_frontier_visible_counts",
    "elected_frontier_eligible_counts",
    "serving_input_revision",
    "activated_at",
)
"""A's immutable elected snapshot: contributors (`elected_run_ids`), the direct `winner_run_id`
and so its aliases, the elected digests and vectors, and activation. Criterion 4 requires these
unchanged while B publishes. `state`, `version` and the direct binding are lifecycle: A's own
direct finalize moves the head ACTIVE -> BOUND_DRAINING and rebinds it without touching A (x8)."""
A_HOLDS_HEAD_STATES: Final = frozenset({"ACTIVE", "BOUND_DRAINING"})
"""Criterion 4's "A remains active" (clarified 2026-10-16): A's cohort holds the head in one
of these states and its final-alias drain has not committed (B's instants precede it)."""


class ControlError(RuntimeError):
    """The control cannot be proven; the receipt is `NOT_PROVEN`."""


@dataclass(frozen=True, slots=True)
class Inputs:
    """The validated explicit dev target and control inputs, checked before any AWS access."""

    profile: str
    environment: str
    region: str
    expected_account: str
    data_stack: str
    bucket: str
    graph_store_stack: str
    manifest: Path
    acceptance_execution_id: str
    evidence_dir: Path
    synthetic_full_plan: bool = False


@dataclass(frozen=True, slots=True)
class Entry:
    """One roster label and the `land_fixtures --probe-retrieval` source it names."""

    roster: str
    label: str
    source: str


@dataclass(frozen=True, slots=True)
class PlannedEntry:
    """One entry's deterministic dry plan."""

    entry: Entry
    window: str
    key: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "--profile",
        "--environment",
        "--region",
        "--expected-account",
        "--data-stack",
        "--bucket",
        "--graph-store-stack",
        "--manifest",
        "--acceptance-execution-id",
        "--evidence-dir",
    ):
        parser.add_argument(name, required=True)
    parser.add_argument("--synthetic-full-plan", action="store_true")
    return parser


def parse_inputs(argv: Sequence[str], evidence_root: Path = _EVIDENCE_ROOT) -> Inputs:
    """Validate every input before AWS access; any refusal is a `ControlError`."""
    arguments = _parser().parse_args(list(argv))
    values = {name: str(getattr(arguments, name)).strip() for name in vars(arguments)}
    for name, value in values.items():
        if name != "synthetic_full_plan" and not value:
            raise ControlError(f"--{name.replace('_', '-')} must not be blank")
    if values["environment"] != "dev":
        raise ControlError("the control runs only against dev")
    if values["profile"].casefold() == "default":
        raise ControlError("an explicit nondefault profile is required")
    if _ACCOUNT.fullmatch(values["expected_account"]) is None:
        raise ControlError("--expected-account must be a 12-digit account id")
    manifest = Path(values["manifest"])
    if not manifest.is_file():
        raise ControlError("the manifest is unreadable")
    evidence = Path(values["evidence_dir"]).resolve()
    root = evidence_root.resolve()
    if evidence.exists() or root not in evidence.parents:
        raise ControlError("--evidence-dir must be a new directory under validation/evidence/")
    execution = values["acceptance_execution_id"]
    if execution_id_used(execution, root):
        raise ControlError("the acceptance execution id was already used")
    return Inputs(
        profile=values["profile"],
        environment="dev",
        region=values["region"],
        expected_account=values["expected_account"],
        data_stack=values["data_stack"],
        bucket=values["bucket"],
        graph_store_stack=values["graph_store_stack"],
        manifest=manifest,
        acceptance_execution_id=execution,
        evidence_dir=evidence,
        synthetic_full_plan=bool(arguments.synthetic_full_plan),
    )


def execution_id_used(execution_id: str, evidence_root: Path) -> bool:
    """Whether any retained control receipt already names this acceptance execution id."""
    if not evidence_root.is_dir():
        return False
    for path in evidence_root.rglob(RECEIPT_NAME):
        try:
            document = json.loads(path.read_text())
        except OSError, json.JSONDecodeError:
            continue
        if isinstance(document, dict) and document.get("acceptance_execution_id") == execution_id:
            return True
    return False


def load_manifest(path: Path) -> tuple[str, tuple[Entry, ...]]:
    """Parse the whole four-roster manifest; return its SHA-256 and every entry."""
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ControlError("the manifest is not JSON") from error
    if not isinstance(document, dict) or document.get("schema_version") != MANIFEST_SCHEMA:
        raise ControlError("the manifest has the wrong schema")
    rosters = cast("dict[str, object]", document).get("rosters")
    if not isinstance(rosters, dict) or set(cast("dict[str, object]", rosters)) != set(ROSTERS):
        raise ControlError("the manifest must hold exactly the four rosters")
    entries: list[Entry] = []
    for roster in ROSTERS:
        mapping = cast("dict[str, object]", rosters)[roster]
        if not isinstance(mapping, dict) or not mapping:
            raise ControlError(f"roster {roster} must be a nonempty map")
        for label, source in cast("dict[str, object]", mapping).items():
            if not label.strip() or not isinstance(source, str) or not source.strip():
                raise ControlError(f"roster {roster} has a blank label or source")
            entries.append(Entry(roster, label, source))
    singles = [entry for entry in entries if entry.roster == "single_root"]
    if len(singles) != 1:
        raise ControlError("single_root must name exactly one root")
    labels = [entry.label for entry in entries]
    if len(set(labels)) != len(labels):
        raise ControlError("a label repeats across rosters")
    sources = [entry.source for entry in entries]
    if len(set(sources)) != len(sources):
        raise ControlError("a source repeats across the rosters")
    return hashlib.sha256(raw).hexdigest(), tuple(entries)


def _sha12(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def operation_id(execution_id: str, entry: Entry, phase: str) -> str:
    """`graph-roll-<exec-sha12>-<roster>-<label-sha12>-<phase>`, lower-case ASCII."""
    return (
        f"graph-roll-{_sha12(execution_id)}-{entry.roster.replace('_', '-')}-"
        f"{_sha12(entry.label)}-{phase}"
    ).lower()


def land_fixtures_argv(
    inputs: Inputs, entry: Entry, phase: str, reserved_key: str | None = None
) -> tuple[str, ...]:
    """Build the exact `land_fixtures` argument tuple for one dry plan, reservation, or launch."""
    argv = [
        sys.executable,
        "-m",
        "adapters.jobs.land_fixtures",
        "--profile",
        inputs.profile,
        "--region",
        inputs.region,
        "--expected-account",
        inputs.expected_account,
        "--env",
        inputs.environment,
        "--operation-id",
        operation_id(inputs.acceptance_execution_id, entry, "launch" if phase == "land" else phase),
        "--stack",
        inputs.data_stack,
        "--bucket",
        inputs.bucket,
        "--probe-retrieval",
        entry.source,
        "--graph-store-stack",
        inputs.graph_store_stack,
    ]
    if phase.startswith("plan"):
        argv.append("--dry-run")
    elif phase in {"reserve", "land"}:
        # `land` is the unheld launch: the reservation itself, whose S3 delivery the live
        # automatic admission admits, so its operation ID is the entry's launch ID.
        argv += ["--reserved-probe-key", cast("str", reserved_key), "--no-twin"]
    elif phase == "launch":
        argv += ["--reserved-probe-key", cast("str", reserved_key), "--twin-only"]
    else:
        raise ControlError(f"unknown child phase {phase}")
    return tuple(argv)


def validate_with_real_parser(argv: Sequence[str]) -> None:
    """Parse one child argv with the real `land_fixtures._parse_args`, in process."""
    from adapters.jobs.land_fixtures.__main__ import (
        _parse_args,  # pyright: ignore[reportPrivateUsage]
    )

    try:
        _parse_args(tuple(argv[3:]))
    except SystemExit as error:
        raise ControlError("the real land_fixtures parser refused a child command") from error


@dataclass(frozen=True, slots=True)
class ChildResult:
    """One child's exit status and captured output."""

    returncode: int | None
    stdout: str
    timed_out: bool = False
    stderr: str = ""
    """Kept so a failing child's own refusal reaches the receipt (x2 lost it)."""


type Runner = Callable[[tuple[str, ...], float], ChildResult]


def run_child(argv: tuple[str, ...], timeout: float) -> ChildResult:
    """Run one child as an argument tuple (never a shell), from `src/`."""
    try:
        completed = subprocess.run(  # noqa: S603 -- an argument tuple built above, no shell
            argv,
            cwd=_ROOT / "src",
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ChildResult(None, "", timed_out=True)
    return ChildResult(completed.returncode, completed.stdout, stderr=completed.stderr)


def _one_line(stdout: str, prefix: str) -> dict[str, object]:
    lines = [line for line in stdout.splitlines() if line.startswith(prefix)]
    if len(lines) != 1:
        raise ControlError(f"the child printed {len(lines)} {prefix.strip()} lines, not one")
    try:
        document = json.loads(lines[0].removeprefix(prefix))
    except json.JSONDecodeError as error:
        raise ControlError(f"the child's {prefix.strip()} line is malformed") from error
    if not isinstance(document, dict):
        raise ControlError(f"the child's {prefix.strip()} line is malformed")
    return cast("dict[str, object]", document)


def _completed(result: ChildResult, what: str) -> ChildResult:
    if result.timed_out:
        raise ControlError(f"the {what} child timed out")
    if result.returncode != 0:
        # `land_fixtures` reports its error through the Powertools logger, which writes JSON
        # to stdout, not stderr (x3 lost it); keep its error lines and the stderr tail.
        errors = [
            line
            for line in result.stdout.splitlines()
            if '"level":"ERROR"' in line or '"level":"WARNING"' in line
        ]
        lines = errors[-2:] + [line for line in result.stderr.splitlines() if line.strip()][-2:]
        tail = f": {' | '.join(lines)[-1200:]}" if lines else ""
        raise ControlError(f"the {what} child exited {result.returncode}{tail}")
    return result


def dry_plan(inputs: Inputs, entries: Sequence[Entry], runner: Runner) -> tuple[PlannedEntry, ...]:
    """Dry-plan every entry twice and refuse duplicates, exhaustion, and plan drift."""
    planned: list[PlannedEntry] = []
    for entry in entries:
        reports: list[dict[str, object]] = []
        for phase in ("plan1", "plan2"):
            argv = land_fixtures_argv(inputs, entry, phase)
            validate_with_real_parser(argv)
            result = _completed(runner(argv, CHILD_TIMEOUT_SECONDS), f"{entry.label} dry plan")
            reports.append(_one_line(result.stdout, _PROBE_RETRIEVAL))
        first, second = reports
        if {k: first.get(k) for k in ("key", "window", "r")} != {
            k: second.get(k) for k in ("key", "window", "r")
        }:
            raise ControlError(f"{entry.label}: the two dry plans differ")
        window, key = first.get("window"), first.get("key")
        if not isinstance(window, str) or not window.endswith("/") or not isinstance(key, str):
            raise ControlError(f"{entry.label}: the dry plan has a malformed window or key")
        if not key.startswith(f"{window}retrieved=") or first.get("historical") is not False:
            raise ControlError(f"{entry.label}: the dry plan is not a fresh probe slot")
        planned.append(PlannedEntry(entry, window, key))
    windows = [item.window for item in planned]
    keys = [item.key for item in planned]
    if len(set(windows)) != len(windows):
        raise ControlError("two entries plan the same window")
    if len(set(keys)) != len(keys):
        raise ControlError("two entries plan the same probe key")
    return tuple(planned)


def reserve(inputs: Inputs, planned: Sequence[PlannedEntry], runner: Runner) -> None:
    """Land each exact dry-planned key serially, in stable `(window, key, label)` order."""
    for item in sorted(planned, key=lambda p: (p.window, p.key, p.entry.label)):
        argv = land_fixtures_argv(inputs, item.entry, "reserve", item.key)
        validate_with_real_parser(argv)
        result = _completed(runner(argv, CHILD_TIMEOUT_SECONDS), f"{item.entry.label} reserve")
        report = _one_line(result.stdout, _PROBE_RETRIEVAL)
        if report.get("key") != item.key or report.get("no_twin") is not True:
            raise ControlError(f"{item.entry.label}: the reservation landed another key")


def parse_twin(stdout: str, reserved_key: str) -> tuple[str, dict[str, object]]:
    """Validate one launch child's `PROBE_TWIN` line; return its run id and the twin."""
    twin = _one_line(stdout, _PROBE_TWIN)
    receipt = twin.get("receipt")
    if twin.get("mode") != "GRAPH" or twin.get("sent") is not True:
        raise ControlError("the twin was not sent in GRAPH mode")
    if twin.get("decision") != "ADMITTED" or not isinstance(receipt, dict):
        raise ControlError("the twin was not admitted")
    typed = cast("dict[str, object]", receipt)
    run_id = typed.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ControlError("the twin receipt names no run")
    version = twin.get("version_id")
    if (
        typed.get("manifest_key") != reserved_key
        or not version
        or typed.get("version_id") != version
    ):
        raise ControlError("the twin receipt is for another key or version")
    return run_id, twin


class ControlReads(Protocol):
    """The strong reads the control polls; `BotoControlReads` is the live one."""

    def run(self, run_id: str) -> Mapping[str, object] | None:
        """Return the decoded `RUN#<run_id>/RUN` item."""
        ...

    def head(self) -> Mapping[str, object] | None:
        """Return the decoded serving-domain head."""
        ...

    def marker(self, run_id: str) -> Mapping[str, object] | None:
        """Return the decoded `SERVING_PUBLICATION#<run_id>/MARKER`."""
        ...

    def transition(self, cohort_revision: str) -> Mapping[str, object] | None:
        """Return the decoded transition that drained this cohort."""
        ...

    def delivery_receipt(self, manifest_key: str) -> Mapping[str, object] | None:
        """Return the decoded `delivery_event` receipt of the reserved key, once recorded."""
        ...

    def held(self) -> bool:
        """Return whether the mode item holds automatic admission (the GRAPH hold is set)."""
        ...


LaunchHook = Callable[[str, Sequence[str]], None]
"""Called with a roster and its run IDs once every launch of that roster is admitted."""


@dataclass(slots=True)
class Collection:
    """Everything one control run observed, for the receipt."""

    launches: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    run_ids: dict[str, str] = field(default_factory=dict[str, str])
    observations: dict[str, object] = field(default_factory=dict[str, object])


def _now() -> str:
    return datetime.now(UTC).isoformat()


def launch(
    inputs: Inputs,
    planned: Sequence[PlannedEntry],
    runner: Runner,
    collection: Collection,
) -> list[str]:
    """Launch every reserved key of one roster together; refuse the first invalid result.

    A wave's twins start concurrently: launched serially (about 37 s each), x4's wave A was
    admitted 37 s apart and elected in two serving elections, so A never formed one snapshot.
    Results are then checked in planned order.
    """
    run_ids: list[str] = []
    argvs = [land_fixtures_argv(inputs, item.entry, "launch", item.key) for item in planned]
    for argv in argvs:
        validate_with_real_parser(argv)
    launched_at = _now()

    def child(argv: tuple[str, ...]) -> ChildResult:
        return runner(argv, CHILD_TIMEOUT_SECONDS)

    with ThreadPoolExecutor(max_workers=max(1, len(argvs))) as pool:
        results = list(pool.map(child, argvs))
    for item, result in zip(planned, results, strict=True):
        collection.launches[item.entry.label] = {
            "launched_at": launched_at,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
        }
        _completed(result, f"{item.entry.label} launch")
        run_id, twin = parse_twin(result.stdout, item.key)
        if run_id in collection.run_ids.values():
            raise ControlError("two launches returned the same run id")
        collection.run_ids[item.entry.label] = run_id
        collection.launches[item.entry.label]["version_id"] = twin.get("version_id")
        run_ids.append(run_id)
    return run_ids


def launch_unheld(
    inputs: Inputs,
    planned: Sequence[PlannedEntry],
    runner: Runner,
    *,
    collection: Collection,
    reads: ControlReads,
    clock: Clock,
) -> list[str]:
    """Land every key of one roster together; its live automatic admission is the launch.

    With the GRAPH hold cleared, a reservation's own S3 delivery is admitted at once, so a
    twin sent after it can only join that run (run 1 of D2 acceptance, 2026-10-16: "the twin
    was not admitted"). Each key is therefore landed at its launch point, concurrently within
    its roster, and its run is the one its committed `delivery_event` receipt admitted.
    """
    argvs = [land_fixtures_argv(inputs, item.entry, "land", item.key) for item in planned]
    for argv in argvs:
        validate_with_real_parser(argv)
    launched_at = _now()

    def child(argv: tuple[str, ...]) -> ChildResult:
        return runner(argv, CHILD_TIMEOUT_SECONDS)

    with ThreadPoolExecutor(max_workers=max(1, len(argvs))) as pool:
        results = list(pool.map(child, argvs))
    for item, result in zip(planned, results, strict=True):
        collection.launches[item.entry.label] = {
            "launched_at": launched_at,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "admission": "delivery_event",
        }
        report = _one_line(_completed(result, f"{item.entry.label} land").stdout, _PROBE_RETRIEVAL)
        collection.launches[item.entry.label]["land_report"] = dict(report)
        if report.get("key") != item.key or report.get("no_twin") is not True:
            raise ControlError(f"{item.entry.label}: the launch landed another key")
        if report.get("historical") is True:
            raise ControlError(f"{item.entry.label}: the launch landed a historical slot")
    run_ids: list[str] = []
    for item in planned:
        receipt = _poll(
            lambda key=item.key: reads.delivery_receipt(key),
            timeout=DELIVERY_TIMEOUT_SECONDS,
            sleep=clock.sleep,
            clock=clock.monotonic,
        )
        run_id = receipt.get("run_id")
        # The receipt is committed once, with its decision, in the admission transaction, so
        # the first strong read is final; it is kept on the launch even when it is refused.
        collection.launches[item.entry.label]["delivery_receipt"] = {
            name: receipt.get(name)
            for name in ("request_id", "decision", "reason", "run_id", "recorded_at")
        }
        if receipt.get("decision") != "ADMITTED" or not isinstance(run_id, str) or not run_id:
            raise ControlError(f"{item.entry.label}: the delivery was not admitted")
        if run_id in collection.run_ids.values():
            raise ControlError("two launches returned the same run id")
        collection.run_ids[item.entry.label] = run_id
        run_ids.append(run_id)
    return run_ids


def _poll[T](
    read: Callable[[], T | None],
    *,
    timeout: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> T:
    deadline = clock() + timeout
    while True:
        try:
            found = read()
        except BotoCoreError:
            # A transient endpoint failure is a missed poll; the deadline still bounds it.
            found = None
        if found is not None:
            return found
        if clock() >= deadline:
            raise ControlError("a strong poll timed out")
        sleep(POLL_SECONDS)


@dataclass(frozen=True, slots=True)
class Clock:
    """The control's injected waits, so unit tests run without sleeping."""

    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


def _deliveries_recorded(
    reads: ControlReads, planned: Sequence[PlannedEntry], clock: Clock
) -> dict[str, object]:
    """Wait until every reserved key's own `delivery_event` admission has committed.

    A reservation's S3 delivery is admitted asynchronously (under the hold, a held waiter). A
    twin launched while that admission commits races it on the same conflict unit, and x3's
    single-root twin failed so, one second after the last reservation. With every delivery
    recorded first, each twin decides against the committed waiter and supersedes it.
    """

    def read() -> dict[str, object] | None:
        found: dict[str, object] = {}
        for item in planned:
            receipt = reads.delivery_receipt(item.key)
            if receipt is None:
                return None
            found[item.entry.label] = {
                "request_id": receipt.get("request_id"),
                "decision": receipt.get("decision"),
                "reason": receipt.get("reason"),
                "recorded_at": receipt.get("recorded_at"),
            }
        return found

    return _poll(read, timeout=DELIVERY_TIMEOUT_SECONDS, sleep=clock.sleep, clock=clock.monotonic)


def _terminal(reads: ControlReads, run_ids: Sequence[str], clock: Clock) -> dict[str, object]:
    def read() -> dict[str, object] | None:
        runs = {run_id: reads.run(run_id) for run_id in run_ids}
        if all(
            run is not None and run.get("state") in _TERMINAL_RUN_STATES and run.get("terminal_at")
            for run in runs.values()
        ):
            return {run_id: dict(cast("Mapping[str, object]", run)) for run_id, run in runs.items()}
        return None

    return _poll(read, timeout=TERMINAL_TIMEOUT_SECONDS, sleep=clock.sleep, clock=clock.monotonic)


def _granted(reads: ControlReads, run_ids: Sequence[str], clock: Clock) -> dict[str, object]:
    """Wait until every run holds its grant (`GRANTED`) or is already terminal.

    Wave A launches here, while the single root's cohort is still to activate or ACTIVE, so
    both twins become ready behind one busy head and the promoter elects them together once
    it is IDLE. Launched after the single root's terminal state instead, x4's and x7's twins
    reached an IDLE head one release pass apart and were elected in two cohorts.
    """

    def read() -> dict[str, object] | None:
        runs = {run_id: reads.run(run_id) for run_id in run_ids}
        states = {run_id: None if run is None else run.get("state") for run_id, run in runs.items()}
        if all(state == "GRANTED" or state in _TERMINAL_RUN_STATES for state in states.values()):
            return {"observed_at": _now(), "states": states}
        return None

    return _poll(read, timeout=TERMINAL_TIMEOUT_SECONDS, sleep=clock.sleep, clock=clock.monotonic)


def _wave_margin(
    reads: ControlReads, single: Sequence[str], wave: Sequence[str]
) -> dict[str, object]:
    """Record how long before the single cohort's finalize each wave twin settled.

    A positive margin means the twin was settled while the single root's cohort was ACTIVE, so
    a near miss is visible in the evidence; a missing or negative one is the race.
    """
    try:
        finals = [(reads.marker(run_id) or {}).get("finalized_at") for run_id in single]
        finalized = max(datetime.fromisoformat(str(at)) for at in finals) if all(finals) else None
        twins: dict[str, object] = {}
        for run_id in wave:
            settled = (reads.marker(run_id) or {}).get("settled_at")
            margin = (
                (finalized - datetime.fromisoformat(str(settled))).total_seconds()
                if finalized is not None and settled
                else None
            )
            twins[run_id] = {"settled_at": settled, "margin_seconds": margin}
    except (BotoCoreError, ClientError, ValueError) as error:
        # An observation only: it never masks the wait it follows, which may be refusing.
        return {"observed_at": _now(), "unavailable": type(error).__name__}
    return {
        "observed_at": _now(),
        "basis": "single cohort marker finalized_at minus twin marker settled_at",
        "single_finalized_at": None if finalized is None else finalized.isoformat(),
        "twins": twins,
    }


def _active_with(reads: ControlReads, run_ids: Sequence[str], clock: Clock) -> dict[str, object]:
    def read() -> dict[str, object] | None:
        head = reads.head()
        if head is None or head.get("state") != "ACTIVE":
            return None
        elected = set(cast("Sequence[str]", head.get("elected_run_ids") or ()))
        wave = set(run_ids)
        if elected & wave and elected != wave:
            # An immutable snapshot never changes members, so this wave can never be exactly
            # one ACTIVE snapshot; fail now rather than poll to the 7,200 s timeout (x4).
            shape = (
                f"split across cohorts ({len(elected & wave)} of its {len(wave)} runs)"
                if elected < wave or not wave <= elected
                else f"joined other runs ({len(elected - wave)} not in the wave)"
            )
            raise ControlError(f"the wave {shape} in cohort {head.get('cohort_revision')}")
        return dict(head) if elected == set(run_ids) else None

    return _poll(read, timeout=TERMINAL_TIMEOUT_SECONDS, sleep=clock.sleep, clock=clock.monotonic)


def _settled(reads: ControlReads, run_ids: Sequence[str], clock: Clock) -> dict[str, object]:
    def read() -> dict[str, object] | None:
        markers = {run_id: reads.marker(run_id) for run_id in run_ids}
        if all(
            marker is not None and marker.get("state") == "SETTLED_ELIGIBLE"
            for marker in markers.values()
        ):
            return {
                "observed_at": _now(),
                "head": dict(reads.head() or {}),
                "markers": {
                    key: dict(cast("Mapping[str, object]", v)) for key, v in markers.items()
                },
            }
        return None

    return _poll(read, timeout=TERMINAL_TIMEOUT_SECONDS, sleep=clock.sleep, clock=clock.monotonic)


def _drained(reads: ControlReads, cohort: str, clock: Clock) -> dict[str, object]:
    def read() -> dict[str, object] | None:
        found = reads.transition(cohort)
        return None if found is None else dict(found)

    return _poll(read, timeout=TERMINAL_TIMEOUT_SECONDS, sleep=clock.sleep, clock=clock.monotonic)


def _entries(planned: Sequence[PlannedEntry], roster: str) -> list[PlannedEntry]:
    return [item for item in planned if item.entry.roster == roster]


def run_control(
    inputs: Inputs,
    planned: Sequence[PlannedEntry],
    runner: Runner,
    reads: ControlReads,
    clock: Clock | None = None,
    collection: Collection | None = None,
    *,
    held: bool = True,
    on_launch: LaunchHook | None = None,
) -> Collection:
    """Single root granted, then A; single root terminal, A ACTIVE, B while A is active.

    A's drain then elects B. `collection` is the caller's, so a refusal keeps what was observed.
    Under the hold every key was reserved beforehand and launches by twin; without it each key
    is landed at its launch point (`launch_unheld`). The sequence is the same either way.
    """
    clock = clock or Clock()
    collection = Collection() if collection is None else collection
    observed = collection.observations
    observed["admission"] = "held_twin" if held else "unheld_delivery"

    def start(entries: Sequence[PlannedEntry], roster: str) -> list[str]:
        run_ids = (
            launch(inputs, entries, runner, collection)
            if held
            else launch_unheld(
                inputs, entries, runner, collection=collection, reads=reads, clock=clock
            )
        )
        if on_launch is not None:
            on_launch(roster, run_ids)
        return run_ids

    if held:
        observed["deliveries_recorded"] = _deliveries_recorded(reads, planned, clock)
    single = start(_entries(planned, "single_root"), "single_root")
    observed["single_root_granted"] = _granted(reads, single, clock)
    wave_a = start(_entries(planned, "wave_a"), "wave_a")
    observed["single_root_terminal"] = _terminal(reads, single, clock)
    try:
        a_active = _active_with(reads, wave_a, clock)
    finally:
        observed["wave_a_margin"] = _wave_margin(reads, single, wave_a)
    observed["a_active_at"] = _now()
    observed["a_snapshot_before"] = a_active
    wave_b = start(_entries(planned, "wave_b"), "wave_b")
    observed["b_settled"] = _settled(reads, wave_b, clock)
    cohort = a_active.get("cohort_revision")
    if not isinstance(cohort, str) or not cohort:
        raise ControlError("A's active snapshot names no cohort")
    observed["a_transition"] = _drained(reads, cohort, clock)
    observed["b_active"] = _active_with(reads, wave_b, clock)
    observed["waves_terminal"] = _terminal(reads, [*wave_a, *wave_b], clock)
    if inputs.synthetic_full_plan:
        synthetic = start(_entries(planned, SYNTHETIC_ROSTER), SYNTHETIC_ROSTER)
        observed["synthetic_full_plan"] = {
            "label": "synthetic load characterization; never a hold-clear gate",
            "terminal": _terminal(reads, synthetic, clock),
        }
    return collection


def _instant(value: object, what: str) -> datetime:
    if not isinstance(value, str):
        raise ControlError(f"{what} is not an instant")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ControlError(f"{what} is not timezone-aware")
    return parsed


def _seconds(runs: Mapping[str, object]) -> tuple[float, float]:
    """Return (max root seconds, cycle seconds) from `root_clock_started_at` to `terminal_at`."""
    typed = cast("Mapping[str, Mapping[str, object]]", runs)
    roots = [_instant(run.get("root_clock_started_at"), "root clock") for run in typed.values()]
    terminals = [_instant(run.get("terminal_at"), "terminal_at") for run in typed.values()]
    durations = [(t - r).total_seconds() for r, t in zip(roots, terminals, strict=True)]
    if any(value < 0 for value in durations):
        raise ControlError("a run is terminal before its root clock")
    return max(durations), (max(terminals) - min(roots)).total_seconds()


def rolling_freshness_control_verdict(
    collection: Collection, run_ids: Mapping[str, Sequence[str]]
) -> dict[str, object]:
    """Judge the mandatory single-root and A/B control; the synthetic plan never gates it."""
    reasons: list[str] = []
    observed = collection.observations
    try:
        single = cast("Mapping[str, Mapping[str, object]]", observed["single_root_terminal"])
        if any(run.get("state") != "SUCCEEDED" for run in single.values()):
            reasons.append("single_root_not_succeeded")
        single_root, single_cycle = _seconds(single)
        before = cast("Mapping[str, object]", observed["a_snapshot_before"])
        settled = cast("Mapping[str, object]", observed["b_settled"])
        transition = cast("Mapping[str, object]", observed["a_transition"])
        b_active = cast("Mapping[str, object]", observed["b_active"])
        waves = cast("Mapping[str, Mapping[str, object]]", observed["waves_terminal"])
        a_ids, b_ids = set(run_ids["wave_a"]), set(run_ids["wave_b"])
        head_at_settle = cast("Mapping[str, object]", settled.get("head") or {})
        if any(head_at_settle.get(name) != before.get(name) for name in A_SNAPSHOT_FIELDS):
            reasons.append("a_snapshot_changed_while_b_published")
        if head_at_settle.get("state") not in A_HOLDS_HEAD_STATES:
            reasons.append("a_not_active_when_b_settled")
        if set(cast("Sequence[str]", before.get("elected_run_ids") or ())) & b_ids:
            reasons.append("b_joined_a")
        drained_at = _instant(transition.get("drained_at"), "A drained_at")
        markers = cast("Mapping[str, Mapping[str, object]]", settled.get("markers") or {})
        if set(markers) != b_ids:
            reasons.append("b_settlement_incomplete")
        for marker in markers.values():
            for name in ("flipped_at", "settled_at"):
                instant = marker.get(name)
                if instant is None or _instant(instant, name) >= drained_at:
                    reasons.append(f"b_{name}_not_before_a_final_alias")
        if _instant(settled.get("observed_at"), "B settled") < _instant(
            observed.get("a_active_at"), "A active"
        ):
            reasons.append("b_settled_before_a_active")
        if transition.get("elected_cohort_revision") != b_active.get("cohort_revision"):
            reasons.append("b_not_elected_by_a_final_transaction")
        if any(run.get("state") != "SUCCEEDED" for run in waves.values()):
            reasons.append("wave_run_not_succeeded")
        if set(waves) != a_ids | b_ids:
            reasons.append("wave_terminal_incomplete")
        wave_root, wave_cycle = _seconds(waves)
    except (KeyError, ControlError, ValueError) as error:
        return {"reasons": [f"invalid:{error}"], "passed": False, "disposition": "NOT_PROVEN"}
    if max(single_root, wave_root) > TERMINAL_TIMEOUT_SECONDS:
        reasons.append("root_over_7200_seconds")
    if single_cycle > TERMINAL_TIMEOUT_SECONDS or wave_cycle > TERMINAL_TIMEOUT_SECONDS:
        reasons.append("cycle_over_7200_seconds")
    return {
        "single_root": {"max_root_seconds": single_root, "cycle_seconds": single_cycle},
        "waves": {"max_root_seconds": wave_root, "cycle_seconds": wave_cycle},
        "reasons": reasons,
        "passed": not reasons,
        "disposition": "PASS" if not reasons else "NOT_PROVEN",
    }


def write_receipt(evidence_dir: Path, document: Mapping[str, object]) -> Path:
    """Atomically write the receipt: a temporary file, then one rename."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / RECEIPT_NAME
    temporary = evidence_dir / f".{RECEIPT_NAME}.tmp"
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
    return path


@dataclass(frozen=True, slots=True)
class BotoControlReads:
    """The control's live strong reads of the execution-graph table."""

    dynamodb: Any
    table: str
    domain: str
    session: Any = None
    """The session `delivery_receipt` lists the raw bucket's exact key versions with."""
    bucket: str = ""

    def _get(self, pk: str, sk: str) -> Mapping[str, object] | None:
        from adapters.outbound.dynamodb.operational_status import decode_item

        item = self.dynamodb.get_item(
            TableName=self.table, Key={"pk": {"S": pk}, "sk": {"S": sk}}, ConsistentRead=True
        ).get("Item")
        return None if item is None else decode_item(item)

    def run(self, run_id: str) -> Mapping[str, object] | None:
        """Read `RUN#<run_id>/RUN` strongly."""
        return self._get(f"RUN#{run_id}", "RUN")

    def head(self) -> Mapping[str, object] | None:
        """Read the serving-domain head strongly."""
        return self._get(f"SERVING_DOMAIN#{self.domain}", "HEAD")

    def marker(self, run_id: str) -> Mapping[str, object] | None:
        """Read one publication marker strongly."""
        return self._get(f"SERVING_PUBLICATION#{run_id}", "MARKER")

    def transition(self, cohort_revision: str) -> Mapping[str, object] | None:
        """Read the transition that drained one cohort strongly."""
        return self._get(f"SERVING_TRANSITION#{self.domain}", f"COHORT#{cohort_revision}")

    def delivery_receipt(self, manifest_key: str) -> Mapping[str, object] | None:
        """Read the reserved key's own `delivery_event` receipt strongly, once it exists.

        The request ID is `ingress.delivery_request`'s, from the key's one exact version.
        """
        if self.session is None:
            raise ControlError("the live reads need the session that reads the raw bucket")
        from adapters.inbound.ingestion_handlers.ingress import DeliveryRef, delivery_request
        from adapters.jobs.land_fixtures.twin import exact_manifest_version

        version = exact_manifest_version(self.session, self.bucket, manifest_key)
        # Only the key and version name the request; the receive fields are unused here.
        ref = DeliveryRef(manifest_key, version, "", 0, "", "")
        request = delivery_request(ref, "control")
        return self._get(f"RECEIPT#{request.request_id}", "RECEIPT")

    def held(self) -> bool:
        """Strongly read the mode item with the runtime's own decoder; GRAPH is required."""
        from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore
        from domain.operations.execution_graph import AuthorityMode

        mode = DynamoExecutionGraphStore(self.table, self.dynamodb).read_mode()
        if mode.mode is not AuthorityMode.GRAPH:
            raise ControlError("the control runs only in GRAPH mode")
        return bool(mode.hold)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the control end to end; any failure writes a sanitized `NOT_PROVEN` receipt."""
    try:
        inputs = parse_inputs(sys.argv[1:] if argv is None else argv)
    except ControlError as error:
        print(f"NOT_PROVEN: {error}", file=sys.stderr)
        return 2
    path, document = execute(inputs)
    print(f"receipt: {path}")
    return 0 if document["disposition"] == "PASS" else 1


def execute(
    inputs: Inputs,
    runner: Runner | None = None,
    clock: Clock | None = None,
    *,
    on_launch: LaunchHook | None = None,
) -> tuple[Path, dict[str, object]]:
    """Run the validated control with `runner` for every child; write and return the receipt.

    `main` runs it with `run_child`; the acceptance orchestrator wraps `run_child` to observe
    launches while the control blocks, and changes nothing else. The mode's hold, read once
    before any write, selects reservation-then-twin (held) or land-at-launch (unheld).
    """
    runner = runner or run_child
    collection = Collection()
    document: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA,
        "acceptance_execution_id": inputs.acceptance_execution_id,
        "started_at": _now(),
        "disposition": "NOT_PROVEN",
    }
    try:
        manifest_sha, entries = load_manifest(inputs.manifest)
        document["manifest_sha256"] = manifest_sha
        mandatory = [
            e for e in entries if inputs.synthetic_full_plan or e.roster != SYNTHETIC_ROSTER
        ]
        import boto3

        from adapters.jobs.execution_graph.resolution import resolve_graph_table
        from domain.operations.execution_graph_release import INGESTION_RELEASE_DOMAIN

        session = boto3.Session(profile_name=inputs.profile, region_name=inputs.region)
        if session.client("sts").get_caller_identity()["Account"] != inputs.expected_account:
            raise ControlError("the explicit profile is not the expected account")
        planned = dry_plan(inputs, mandatory, runner)
        document["planned"] = [
            {
                "roster": item.entry.roster,
                "label": item.entry.label,
                "key": item.key,
                "operation_id": operation_id(inputs.acceptance_execution_id, item.entry, "launch"),
            }
            for item in planned
        ]
        reads = BotoControlReads(
            session.client("dynamodb"),
            resolve_graph_table(session, inputs.environment, inputs.graph_store_stack),
            INGESTION_RELEASE_DOMAIN,
            session=session,
            bucket=inputs.bucket,
        )
        held = reads.held()
        if held:
            reserve(inputs, planned, runner)
        run_control(
            inputs, planned, runner, reads, clock, collection, held=held, on_launch=on_launch
        )
        by_roster = {
            roster: [collection.run_ids[item.entry.label] for item in _entries(planned, roster)]
            for roster in MANDATORY_ROSTERS
        }
        verdict = rolling_freshness_control_verdict(collection, by_roster)
        document.update(verdict=verdict, disposition=verdict["disposition"])
    except Exception as error:  # noqa: BLE001 -- every failure is one sanitized NOT_PROVEN receipt
        document["failure"] = type(error).__name__
        document["failure_reason"] = str(error) if isinstance(error, ControlError) else ""
    # The control's own strong reads, kept on a refusal too (a split's margin is the evidence).
    document.update(
        launches=collection.launches,
        run_ids=collection.run_ids,
        observations=collection.observations,
    )
    document["ended_at"] = _now()
    return write_receipt(inputs.evidence_dir, document), document


if __name__ == "__main__":
    sys.exit(main())
