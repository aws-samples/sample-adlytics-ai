# SPDX-License-Identifier: MIT-0
"""Run the whole plan-item-13 acceptance as one deterministic step (dev only, hold kept set).

    uv run python validation/run_graph_acceptance.py --profile <profile> --environment dev \
        --region <region> --expected-account <account> --data-stack <data-stack> \
        --bucket <raw-bucket> --graph-store-stack <store-stack> \
        --manifest validation/fixtures/graph-rolling-freshness-control-v1.json \
        --acceptance-execution-id <id> --evidence-dir <evidence-root>/acceptance/<id> \
        --accepted-generation <generation> --generation-unit <unit> \
        --env-file <ignored .env> --lock-file <dev deploy lock> \
        [--p6e-args <json argv file>] [--census-baseline <predeploy census>] \
        [--matrix-evidence HIST-<id>=<path> ...] [--gap-seconds 10]

Steps, in this fixed order, each logged under `<evidence-dir>/logs/`; any failure stops the run,
and the receipt `graph-acceptance-run-v1.json` records every step's exit and instants:

1. `make dev-state` (read-only). Its successful end is `preflight_completed_at`.
   Then the one governed outside-list selection (`probe_turn_latency.py
   --select-outside-list-item`, a read-only CHAT-workgroup query) writes this execution's
   `chat-outside-list-selection-v3.json`; both chat samples and the verdict bind to it.
2. The paired no-bake chat baseline (`probe_turn_latency.py --phase paired-no-bake`), which
   finishes before step 3 starts, so it lies after preflight and before the first root launch.
3. `run_graph_rolling_freshness_control.execute`, which blocks. A watcher observes the control's
   wave-A launches, strongly reads the serving head until A's immutable snapshot is ACTIVE with
   exactly those runs, and only then starts the during-bake sample with the same persona,
   manifest, prompts, sample count and gap; that probe strongly re-reads the head before every
   row, so each row launches on an ACTIVE read of A.
4. After the span lag, the trace artifacts are collected once and retained, the bake interval
   (A's `bake.run` start to its final successful `bake.finalize` end) is derived from them, and
   both chat drafts are bound to it. Then the HIST-8 persona footnote readback
   (`probe_footnote_readback.py`), read no earlier than that interval's end.
5. Optional P6(e) (`--p6e-args`, the control's run IDs appended, and the span window from the
   preflight's end to the control's end unless the file names one), the postdeploy legacy census
   (`--census-baseline`), `make check-alarm-window` since preflight, and a terminal
   `make dev-state`.
6. The run-generated `matrix-evidence-manifest-v1.json` (every matrix source's path and digest),
   then `probe_ingestion_traceability_gate.py --acceptance-verdicts`, one receipt per verdict.

`--resume-after-control <digest file>` continues an existing execution whose control passed and
whose later legs did not: it verifies every reused artifact (`RESUME_REUSED`) against the digests
recorded beforehand, refuses anything the original left NOT_PROVEN, then runs step 5 and step 6
again in `-resume` directories, writing `graph-acceptance-resume-v1.json` and a manifest that
names both windows. The original receipt stays as written.

Nothing here clears the GRAPH hold; that is a separate, recorded owner-authorized mutation.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, cast

from botocore.exceptions import BotoCoreError

_ROOT: Final = Path(__file__).resolve().parents[1]
for _path in (_ROOT / "src", _ROOT / "validation"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import run_graph_rolling_freshness_control as control

RECEIPT_NAME: Final = "graph-acceptance-run-v1.json"
RECEIPT_SCHEMA: Final = "graph-acceptance-run-v1"
CHAT_MANIFEST: Final = _ROOT / "validation" / "fixtures" / "chat-bake-acceptance-v2.json"
BASELINE_DRAFT: Final = "chat-paired-no-bake.draft.json"
DURING_DRAFT: Final = "chat-during-bake.draft.json"
OUTSIDE_SELECTION_RECEIPT: Final = "chat-outside-list-selection-v3.json"
"""`probe_turn_latency.OUTSIDE_SELECTION_RECEIPT`, written once inside the execution."""
FOOTNOTE_RECEIPT: Final = "footnote-readback-verdict.json"
OBSERVED_COHORT_RECEIPT: Final = "observed-cohort-verdict.json"
"""`MATRIX_COMMAND_RECEIPTS["observed_cohort_verdict"]`, from this execution's P6(e) evidence."""
RESUME_RECEIPT: Final = "graph-acceptance-resume-v1.json"
RESUME_SCHEMA: Final = "graph-acceptance-resume-v1"
RESUME_REUSED: Final = (
    "rolling-freshness-control-v1.json",
    "chat-latency-paired-no-bake.json",
    "chat-latency-during-bake.json",
    "chat-paired-no-bake.draft.json",
    "chat-during-bake.draft.json",
    "chat-outside-list-selection-v3.json",
    "trace-artifacts-v1.json",
    FOOTNOTE_RECEIPT,
    RECEIPT_NAME,
)
"""Every original-window artifact a resume reads; each must match its recorded digest."""
"""HIST-8's execution-bound receipt (`probe_footnote_readback.py`)."""
SPAN_LAG_SECONDS: Final = 660
"""The span retrieval's 600 s `aws/spans` lag bound plus one minute, waited before collection."""
DURING_PROBE_TIMEOUT_SECONDS: Final = 1_800
P6_MATRIX_ROWS: Final = (
    "HIST-5",
    "HIST-6",
    "HIST-7",
    "HIST-9",
    "HIST-10b",
    "HIST-12",
    "HIST-12b",
    "HIST-16",
    "HIST-19",
)
"""The retained historical rows whose command is the P6(e) step."""


class AcceptanceError(RuntimeError):
    """The acceptance cannot be proven; the receipt is `NOT_PROVEN` and the hold stays set."""


@dataclass(frozen=True, slots=True)
class AcceptanceInputs:
    """The control's validated inputs plus every chat, lock and evidence input."""

    control: control.Inputs
    accepted_generation: str
    generation_unit: str
    env_file: Path
    lock_file: Path
    gap_seconds: int
    p6e_args: Path | None
    census_baseline: Path | None
    matrix_evidence: tuple[str, ...]
    recover_minutes: int
    expected_alarms: str
    dispositions: Path | None = None
    resume_digests: Path | None = None
    """`--resume-after-control <digest file>`: resume this existing execution after its control."""


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
        "--accepted-generation",
        "--generation-unit",
        "--env-file",
        "--lock-file",
    ):
        parser.add_argument(name, required=True)
    parser.add_argument("--gap-seconds", type=int, default=10)
    parser.add_argument("--p6e-args", type=Path)
    parser.add_argument("--census-baseline", type=Path)
    parser.add_argument("--matrix-evidence", action="append", default=[])
    parser.add_argument("--recover-minutes", type=int, default=15)
    parser.add_argument("--expected-alarms", default="none")
    parser.add_argument("--dispositions", type=Path)
    parser.add_argument(
        "--resume-after-control",
        type=Path,
        dest="resume_digests",
        help="a `shasum -a 256` listing of the execution's reused artifacts, recorded beforehand",
    )
    return parser


_CONTROL_FLAGS: Final = (
    "profile",
    "environment",
    "region",
    "expected_account",
    "data_stack",
    "bucket",
    "graph_store_stack",
    "manifest",
    "acceptance_execution_id",
    "evidence_dir",
)


def parse_acceptance(argv: Sequence[str]) -> AcceptanceInputs:
    """Validate every input before AWS access; the control's own rules apply unchanged.

    The control's evidence directory is this execution's directory, which must be new; the
    root its reuse and placement checks run against is that directory's parent.
    """
    arguments = _parser().parse_args(list(argv))
    evidence = Path(arguments.evidence_dir).resolve()
    control_argv: list[str] = []
    for name in _CONTROL_FLAGS:
        control_argv += [f"--{name.replace('_', '-')}", str(getattr(arguments, name))]
    if arguments.resume_digests is not None:
        # A resume continues one existing execution; the control's new-directory and unused-ID
        # rules are checked against a scratch name, then the real directory is restored.
        if not evidence.is_dir():
            raise AcceptanceError("--resume-after-control needs the existing execution directory")
        index = control_argv.index("--evidence-dir") + 1
        control_argv[index] = str(evidence.parent / f".resume-check-{evidence.name}")
        index = control_argv.index("--acceptance-execution-id") + 1
        execution = control_argv[index]
        control_argv[index] = f"{execution}-resume-check"
        checked = control.parse_inputs(control_argv, evidence_root=evidence.parent)
        inputs = replace(checked, evidence_dir=evidence, acceptance_execution_id=execution)
    else:
        inputs = control.parse_inputs(control_argv, evidence_root=evidence.parent)
    if not Path(arguments.env_file).is_file():
        raise AcceptanceError("--env-file is unreadable")
    if not 0 <= arguments.gap_seconds <= 120:
        raise AcceptanceError("--gap-seconds must be between 0 and 120")
    for path in (
        arguments.p6e_args,
        arguments.census_baseline,
        arguments.dispositions,
        arguments.resume_digests,
    ):
        if path is not None and not path.is_file():
            raise AcceptanceError(f"{path} is unreadable")
    return AcceptanceInputs(
        control=inputs,
        accepted_generation=str(arguments.accepted_generation),
        generation_unit=str(arguments.generation_unit),
        env_file=Path(arguments.env_file).resolve(),
        lock_file=Path(arguments.lock_file).resolve(),
        gap_seconds=int(arguments.gap_seconds),
        p6e_args=arguments.p6e_args,
        census_baseline=arguments.census_baseline,
        matrix_evidence=tuple(arguments.matrix_evidence),
        recover_minutes=int(arguments.recover_minutes),
        expected_alarms=str(arguments.expected_alarms),
        dispositions=arguments.dispositions,
        resume_digests=arguments.resume_digests,
    )


# --- Commands ------------------------------------------------------------------------------


def _target(inputs: AcceptanceInputs) -> list[str]:
    c = inputs.control
    return ["--profile", c.profile, "--region", c.region, "--expected-account", c.expected_account]


def make_argv(
    inputs: AcceptanceInputs, target: str, directory: Path, **extra: str
) -> tuple[str, ...]:
    """One read-only operator `make` target with the explicit dev identity."""
    c = inputs.control
    values = {
        "PROFILE": c.profile,
        "ENV": c.environment,
        "REGION": c.region,
        "ACCOUNT": c.expected_account,
        "EVIDENCE_DIR": str(directory),
        **extra,
    }
    return ("make", target, *(f"{name}={value}" for name, value in values.items()))


def dev_state_argv(inputs: AcceptanceInputs, directory: Path) -> tuple[str, ...]:
    """Read-only `make dev-state` with the lock file and the recorded dispositions, if any."""
    extra = {"LOCK_FILE": str(inputs.lock_file)}
    if inputs.dispositions is not None:
        extra["DISPOSITIONS"] = str(inputs.dispositions)
    return make_argv(inputs, "dev-state", directory, **extra)


def chat_argv(
    inputs: AcceptanceInputs, phase: str, output: Path, preflight_at: str, cohort: str | None
) -> tuple[str, ...]:
    """The governed chat probe for one phase; both phases share every identity input."""
    c = inputs.control
    argv = [
        sys.executable,
        str(_ROOT / "validation" / "probe_turn_latency.py"),
        *_target(inputs),
        "--environment",
        c.environment,
        "--manifest",
        str(CHAT_MANIFEST),
        "--phase",
        phase,
        "--output",
        str(output),
        "--acceptance-execution-id",
        c.acceptance_execution_id,
        "--accepted-generation",
        inputs.accepted_generation,
        "--generation-unit",
        inputs.generation_unit,
        "--preflight-completed-at",
        preflight_at,
        "--outside-selection-receipt",
        str(c.evidence_dir / OUTSIDE_SELECTION_RECEIPT),
        "--gap-seconds",
        str(inputs.gap_seconds),
        "--telemetry-dir",
        str(c.evidence_dir),
        "--graph-store-stack",
        c.graph_store_stack,
    ]
    if cohort is not None:
        argv += ["--target-cohort-revision", cohort]
    return tuple(argv)


def selection_argv(inputs: AcceptanceInputs) -> tuple[str, ...]:
    """Return the read-only governed selection; its receipt lands in this execution's directory."""
    c = inputs.control
    return (
        sys.executable,
        str(_ROOT / "validation" / "probe_turn_latency.py"),
        *_target(inputs),
        "--environment",
        c.environment,
        "--manifest",
        str(CHAT_MANIFEST),
        "--select-outside-list-item",
        "--acceptance-execution-id",
        c.acceptance_execution_id,
        "--accepted-generation",
        inputs.accepted_generation,
        "--generation-unit",
        inputs.generation_unit,
        "--graph-store-stack",
        c.graph_store_stack,
        "--output",
        str(c.evidence_dir / OUTSIDE_SELECTION_RECEIPT),
    )


def footnote_argv(inputs: AcceptanceInputs, output: Path, not_before: str) -> tuple[str, ...]:
    """HIST-8: the persona footnote readback, read no earlier than the bake's finalize end."""
    c = inputs.control
    return (
        sys.executable,
        str(_ROOT / "validation" / "probe_footnote_readback.py"),
        *_target(inputs),
        "--environment",
        c.environment,
        "--acceptance-execution-id",
        c.acceptance_execution_id,
        "--not-before",
        not_before,
        "--output",
        str(output),
    )


def gate_argv(inputs: AcceptanceInputs, *mode: str) -> tuple[str, ...]:
    """The governed gate in one mode, with the explicit dev identity and this execution's dir."""
    c = inputs.control
    return (
        sys.executable,
        str(_ROOT / "validation" / "probe_ingestion_traceability_gate.py"),
        *mode,
        *_target(inputs),
        "--env",
        c.environment,
        "--evidence-dir",
        str(c.evidence_dir),
        "--store-stack",
        c.graph_store_stack,
    )


def verdicts_argv(inputs: AcceptanceInputs, matrix: Sequence[str]) -> tuple[str, ...]:
    """`--acceptance-verdicts` over this execution, with every explicit matrix source."""
    argv = list(gate_argv(inputs, "--acceptance-verdicts"))
    argv += [
        "--acceptance-execution-id",
        inputs.control.acceptance_execution_id,
        "--accepted-generation",
        inputs.accepted_generation,
        "--env-file",
        str(inputs.env_file),
        "--evidence-root",
        str(inputs.control.evidence_dir.parent),
    ]
    for pair in matrix:
        argv += ["--matrix-evidence", pair]
    return tuple(argv)


# --- Ports ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepResult:
    """One finished step: its exit status and instants."""

    name: str
    returncode: int | None
    started_at: str
    ended_at: str


class Ports(Protocol):
    """Everything the orchestration touches outside this process; `LivePorts` is the real one."""

    def run_step(self, name: str, argv: tuple[str, ...], timeout: float) -> StepResult:
        """Run one child to completion with its output logged."""
        ...

    def start(self, name: str, argv: tuple[str, ...]) -> Callable[[float], StepResult]:
        """Start one child; return the call that waits for it."""
        ...

    def execute_control(
        self, runner: control.Runner, clock: control.Clock
    ) -> tuple[Path, dict[str, object]]:
        """Run the blocking control with the observing runner and the aborting clock."""
        ...

    def head(self) -> Mapping[str, object] | None:
        """Strongly read the serving-domain head."""
        ...

    def collect_trace(self, control_document: Mapping[str, object]) -> Mapping[str, object]:
        """Collect the execution's trace artifacts (read-only)."""
        ...

    def sleep(self, seconds: float) -> None:
        """Wait."""
        ...

    def now(self) -> datetime:
        """The current aware UTC instant."""
        ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class LaunchObserver:
    """Wrap the control's runner and record every wave-A launch's run ID as it returns."""

    inner: control.Runner
    wave_a_expected: int
    wave_a: list[str] = field(default_factory=list[str])
    first_launch_at: str | None = None
    complete: threading.Event = field(default_factory=threading.Event)

    def __call__(self, argv: tuple[str, ...], timeout: float) -> control.ChildResult:
        """Run the child; on a wave-A launch, parse its one `PROBE_TWIN` line.

        An unheld launch lands its key (`--no-twin`, a `-launch` operation ID) and prints no
        twin; its wave-A run IDs arrive through `launched` instead.
        """
        operation = argv[argv.index("--operation-id") + 1] if "--operation-id" in argv else ""
        launch = "--twin-only" in argv or operation.endswith("-launch")
        if launch and self.first_launch_at is None:
            self.first_launch_at = _now()
        result = self.inner(argv, timeout)
        if "--twin-only" in argv and "-wave-a-" in operation and result.returncode == 0:
            key = argv[argv.index("--reserved-probe-key") + 1]
            try:
                run_id, _ = control.parse_twin(result.stdout, key)
            except control.ControlError:
                return result  # the control refuses it itself and stops
            self.wave_a.append(run_id)
            if len(self.wave_a) == self.wave_a_expected:
                self.complete.set()
        return result

    def launched(self, roster: str, run_ids: Sequence[str]) -> None:
        """Record an unheld wave A's admitted run IDs once the control knows all of them."""
        if roster != "wave_a" or self.complete.is_set():
            return
        self.wave_a[:] = list(run_ids)
        if len(self.wave_a) == self.wave_a_expected:
            self.complete.set()


@dataclass(slots=True)
class DuringBake:
    """The during-bake watcher's outcome."""

    active_read: dict[str, object] | None = None
    result: StepResult | None = None
    failure: str | None = None
    transient_read_failures: int = 0


def watch_during_bake_guarded(
    ports: Ports,
    observer: LaunchObserver,
    launch: Callable[[str], tuple[str, ...]],
    stop: threading.Event,
    outcome: DuringBake,
    abort: threading.Event,
) -> None:
    """Run the watcher; any unexpected exception is a named failure that aborts the control.

    x4's watcher died on an unhandled exception while the control kept polling for over an
    hour; now the failure is recorded and the control's next wait raises.
    """
    try:
        watch_during_bake(ports, observer, launch, stop, outcome)
    except Exception as error:
        outcome.failure = f"the during-bake watcher failed: {type(error).__name__}: {error}"
    if outcome.failure is not None:
        abort.set()


def aborting_clock(abort: threading.Event, sleep: Callable[[float], None]) -> control.Clock:
    """Return the control's clock, whose every wait refuses once the watcher has failed."""

    def wait(seconds: float) -> None:
        if abort.is_set():
            raise control.ControlError("aborted: the during-bake watcher failed")
        sleep(seconds)

    return control.Clock(sleep=wait)


def watch_during_bake(
    ports: Ports,
    observer: LaunchObserver,
    launch: Callable[[str], tuple[str, ...]],
    stop: threading.Event,
    outcome: DuringBake,
    *,
    poll_seconds: float = 5.0,
) -> None:
    """Wait for every wave-A launch, then for A ACTIVE with exactly those runs; then sample."""
    while not observer.complete.is_set():
        if stop.is_set():
            outcome.failure = "the control ended before every wave-A launch returned"
            return
        observer.complete.wait(poll_seconds)
    while True:
        try:
            head = ports.head()
        except BotoCoreError:
            # A transient endpoint failure (x4 lost its watcher to one DNS miss) is a missed
            # poll, not a verdict; the control's end still bounds the wait.
            outcome.transient_read_failures += 1
            head = None
        if head is not None and head.get("state") == "ACTIVE":
            elected = set(cast("Sequence[str]", head.get("elected_run_ids") or ()))
            if elected == set(observer.wave_a):
                break
            if elected & set(observer.wave_a):
                outcome.failure = "A's ACTIVE snapshot is not exactly the wave-A runs"
                return
        if stop.is_set():
            outcome.failure = "the control ended before A was observed ACTIVE"
            return
        ports.sleep(poll_seconds)
    cohort = str(head.get("cohort_revision") or "")
    outcome.active_read = {**dict(head), "read_at": _now()}
    wait = ports.start("chat-during-bake", launch(cohort))
    outcome.result = wait(DURING_PROBE_TIMEOUT_SECONDS)


def _require(result: StepResult) -> StepResult:
    if result.returncode != 0:
        raise AcceptanceError(f"step {result.name} exited {result.returncode}")
    return result


def _bind(draft_path: Path, output: Path, binding: Mapping[str, str]) -> Path:
    from probe_turn_latency import bind_chat_sample, write_atomic

    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    write_atomic(output, bind_chat_sample(draft, binding))
    return output


def out_of_scope_alarms(record: Path | None) -> list[dict[str, object]]:
    """List the owned alarms one `dev_operations.py` record recorded, each with its state."""
    if record is None:
        return []
    document = cast("dict[str, object]", json.loads(record.read_text(encoding="utf-8")))
    report = cast("dict[str, object]", document.get("report") or {})
    items = cast("list[dict[str, object]]", report.get("out_of_scope") or [])
    return [dict(item) for item in items]


def alarm_verdict(  # noqa: PLR0917 -- one execution's two alarm reads
    execution_id: str,
    since: str,
    window: tuple[StepResult, Path | None],
    terminal: tuple[StepResult, Path | None],
    directory: Path,
) -> Path:
    """Write HIST-17's verdict: the alarm window and the terminal state, both passing.

    Neither record names a verdict field; each passing exit is its verdict. Every owned alarm
    either read records is listed under `out_of_graph_scope` with its state, reason and owner, so
    the matrix row names it rather than reporting it as clean.
    """
    from probe_ingestion_traceability_acceptance import write_json_atomic

    (window_step, window_record), (terminal_step, terminal_record) = window, terminal
    return write_json_atomic(
        directory / "alarm-window-verdict.json",
        {
            "acceptance_execution_id": execution_id,
            "passed": window_step.returncode == 0
            and terminal_step.returncode == 0
            and terminal_record is not None,
            "record": str(window_record),
            "terminal_record": None if terminal_record is None else str(terminal_record),
            "since": since,
            "out_of_graph_scope": {
                "alarm_window": out_of_scope_alarms(window_record),
                "terminal_state": out_of_scope_alarms(terminal_record),
            },
        },
    )


def _newest_json(directory: Path, after: float) -> Path | None:
    found = [path for path in directory.glob("*.json") if path.stat().st_mtime >= after]
    return max(found, key=lambda path: path.stat().st_mtime) if found else None


def run_acceptance(inputs: AcceptanceInputs, ports: Ports) -> dict[str, object]:
    """Run every step in order; return the receipt document (never raises past a step)."""
    from probe_ingestion_traceability_acceptance import (
        CHAT_BASELINE,
        CHAT_DURING,
        TRACE_ARTIFACTS,
        TRACE_WINDOW_PAD,
        bake_interval,
        write_json_atomic,
    )

    c = inputs.control
    directory = c.evidence_dir
    directory.mkdir(parents=True, exist_ok=False)
    steps: list[StepResult] = []
    document: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA,
        "acceptance_execution_id": c.acceptance_execution_id,
        "started_at": _now(),
        "steps": steps,
        "disposition": "NOT_PROVEN",
        "hold": "unchanged (set); clearing is a separate owner-authorized mutation",
    }
    for name in ("dev-state-preflight", "alarm-window", "dev-state-terminal"):
        (directory / name).mkdir()  # `dev_operations.py` writes only into an existing directory
    try:
        preflight = _require(
            ports.run_step(
                "dev-state-preflight",
                dev_state_argv(inputs, directory / "dev-state-preflight"),
                900,
            )
        )
        steps.append(preflight)
        preflight_at = preflight.ended_at
        steps.append(
            _require(ports.run_step("select-outside-list-item", selection_argv(inputs), 300))
        )
        steps.append(
            _require(
                ports.run_step(
                    "chat-paired-no-bake",
                    chat_argv(
                        inputs, "paired-no-bake", directory / BASELINE_DRAFT, preflight_at, None
                    ),
                    DURING_PROBE_TIMEOUT_SECONDS,
                )
            )
        )
        _, entries = control.load_manifest(c.manifest)
        observer = LaunchObserver(
            lambda argv, timeout: control.run_child(argv, timeout),
            sum(1 for entry in entries if entry.roster == "wave_a"),
        )
        stop = threading.Event()
        abort = threading.Event()
        during = DuringBake()
        watcher = threading.Thread(
            target=watch_during_bake_guarded,
            args=(
                ports,
                observer,
                lambda cohort: chat_argv(
                    inputs, "during-bake", directory / DURING_DRAFT, preflight_at, cohort
                ),
                stop,
                during,
                abort,
            ),
            daemon=True,
        )
        watcher.start()
        try:
            control_path, control_document = ports.execute_control(
                observer, aborting_clock(abort, ports.sleep)
            )
        finally:
            stop.set()
            watcher.join(DURING_PROBE_TIMEOUT_SECONDS + 60)
        document["control_receipt"] = str(control_path)
        document["control_disposition"] = control_document.get("disposition")
        document["first_root_launch_at"] = observer.first_launch_at
        document["a_active_read"] = during.active_read
        document["watcher_transient_read_failures"] = during.transient_read_failures
        if during.result is not None:
            steps.append(during.result)
        if control_document.get("disposition") != "PASS":
            watcher = f"; {during.failure}" if during.failure else ""
            raise AcceptanceError(
                "the rolling-freshness control is NOT_PROVEN: "
                f"{control_document.get('failure_reason') or 'verdict failed'}{watcher}"
            )
        if during.failure is not None or during.result is None:
            raise AcceptanceError(during.failure or "the during-bake sample never ran")
        _require(during.result)
        if observer.first_launch_at is None or not preflight_at < observer.first_launch_at:
            raise AcceptanceError("no root launch followed the preflight")
        # The trace window ends TRACE_WINDOW_PAD after the control; wait the lag after that.
        ended = datetime.fromisoformat(str(control_document["ended_at"])) + TRACE_WINDOW_PAD
        wait = SPAN_LAG_SECONDS - (ports.now() - ended).total_seconds()
        if wait > 0:
            ports.sleep(wait)
        trace_path = write_json_atomic(
            directory / TRACE_ARTIFACTS, ports.collect_trace(control_document)
        )
        artifacts = json.loads(trace_path.read_text(encoding="utf-8"))
        observations = cast("Mapping[str, Any]", control_document["observations"])
        winner = str(observations["a_snapshot_before"]["winner_run_id"])
        interval = bake_interval(artifacts, winner)
        binding = {
            "first_root_launch_at": observer.first_launch_at,
            "target_bake_run_id": winner,
            "target_cohort_revision": str(observations["a_snapshot_before"]["cohort_revision"]),
            "bake_run_started_at": interval[0],
            "bake_finalize_ended_at": interval[1],
        }
        document["chat_binding"] = binding
        _bind(directory / BASELINE_DRAFT, directory / CHAT_BASELINE, binding)
        _bind(directory / DURING_DRAFT, directory / CHAT_DURING, binding)
        matrix = list(inputs.matrix_evidence)
        named = {pair.partition("=")[0] for pair in matrix}
        if "HIST-8" not in named:
            # Post-bake persona readback; its receipt's verdict, not this exit, decides HIST-8,
            # so a failing readback still lets every other source be collected.
            footnote = directory / FOOTNOTE_RECEIPT
            steps.append(
                ports.run_step(
                    "footnote-readback", footnote_argv(inputs, footnote, interval[1]), 300
                )
            )
            matrix.append(f"HIST-8={footnote}")
        _evidence_and_verdicts(
            inputs,
            ports,
            steps,
            document,
            Collected(
                preflight_started_at=preflight.started_at,
                preflight_at=preflight_at,
                control_document=control_document,
                matrix=matrix,
                named=named,
            ),
        )
    except Exception as error:
        document["failure"] = type(error).__name__
        document["failure_reason"] = str(error)
    document["ended_at"] = _now()
    document["steps"] = [
        {
            "name": s.name,
            "returncode": s.returncode,
            "started_at": s.started_at,
            "ended_at": s.ended_at,
        }
        for s in steps
    ]
    write_json_atomic(directory / RECEIPT_NAME, document)
    return document


@dataclass(frozen=True, slots=True)
class Collected:
    """What the evidence legs after the control need from the run that observed it."""

    preflight_started_at: str
    preflight_at: str
    control_document: Mapping[str, object]
    matrix: list[str]
    named: set[str]
    suffix: str = ""
    """Appended to each leg's directory, so a resumed leg never writes into the original's."""
    legs: Mapping[str, object] = field(default_factory=dict[str, object])
    """Extra matrix-manifest fields (a resume's per-leg windows)."""


def _evidence_and_verdicts(
    inputs: AcceptanceInputs,
    ports: Ports,
    steps: list[StepResult],
    document: dict[str, object],
    collected: Collected,
) -> None:
    """P6(e), the census, the alarm window, terminal state, the manifest, then the verdicts."""
    from probe_ingestion_traceability_acceptance import (
        MATRIX_MANIFEST,
        matrix_manifest,
        write_json_atomic,
    )

    c = inputs.control
    directory = c.evidence_dir
    if inputs.p6e_args is not None:
        started = time.time()
        p6_dir = directory / f"p6e{collected.suffix}"
        p6_dir.mkdir()
        extra = [str(item) for item in json.loads(inputs.p6e_args.read_text(encoding="utf-8"))]
        # The span window is this run's own: from the preflight's end (before the first
        # root launch) to the control's end, which the span-lag wait has already aged past
        # the `aws/spans` lag. A static file cannot know either instant in advance.
        if "--window-start" not in extra:
            extra += ["--window-start", collected.preflight_at]
        if "--window-end" not in extra:
            extra += ["--window-end", str(collected.control_document["ended_at"])]
        runs = [
            argument
            for run in cast("Mapping[str, str]", collected.control_document["run_ids"]).values()
            for argument in ("--run-id", run)
        ]
        argv = (
            sys.executable,
            str(_ROOT / "validation" / "probe_ingestion_traceability_gate.py"),
            *extra,
            *_target(inputs),
            "--env",
            c.environment,
            "--evidence-dir",
            str(p6_dir),
            *runs,
        )
        steps.append(_require(ports.run_step("p6e", argv, 3_600)))
        p6_evidence = _newest_json(p6_dir, started)
        if p6_evidence is None:
            raise AcceptanceError("P6(e) wrote no evidence")
        collected.matrix.extend(
            f"{row}={p6_evidence}" for row in P6_MATRIX_ROWS if row not in collected.named
        )
        if "HIST-1" not in collected.named:
            # Criteria 3-5's `observed_cohort_verdict` runs inside P6(e); its result is written
            # as this execution's own receipt, which HIST-1/2/3/10a/11 read by name.
            observed = json.loads(p6_evidence.read_text(encoding="utf-8"))["checks"][
                "observed_cohort"
            ]
            write_json_atomic(
                directory / OBSERVED_COHORT_RECEIPT,
                {
                    **observed,
                    "acceptance_execution_id": c.acceptance_execution_id,
                    "source": str(p6_evidence),
                },
            )
    if inputs.census_baseline is not None:
        census_argv = (
            *gate_argv(inputs, "--legacy-census"),
            "--census-baseline",
            str(inputs.census_baseline),
        )
        steps.append(_require(ports.run_step("legacy-census-preservation", census_argv, 1_800)))
    alarm_dir = directory / f"alarm-window{collected.suffix}"
    alarm_dir.mkdir(exist_ok=True)
    started = time.time()
    scope = {} if inputs.dispositions is None else {"DISPOSITIONS": str(inputs.dispositions)}
    alarms = ports.run_step(
        "check-alarm-window",
        make_argv(
            inputs,
            "check-alarm-window",
            alarm_dir,
            SINCE=collected.preflight_started_at,
            RECOVER_MINUTES=str(inputs.recover_minutes),
            EXPECTED=inputs.expected_alarms,
            **scope,
        ),
        900,
    )
    steps.append(_require(alarms))
    alarm_record = _newest_json(alarm_dir, started)
    terminal_dir = directory / f"dev-state-terminal{collected.suffix}"
    terminal_dir.mkdir(exist_ok=True)
    terminal_started = time.time()
    terminal = ports.run_step("dev-state-terminal", dev_state_argv(inputs, terminal_dir), 900)
    steps.append(_require(terminal))
    terminal_record = _newest_json(terminal_dir, terminal_started)
    if "HIST-17" not in collected.named and alarm_record is not None:
        verdict = alarm_verdict(
            c.acceptance_execution_id,
            collected.preflight_started_at,
            (alarms, alarm_record),
            (terminal, terminal_record),
            directory,
        )
        collected.matrix.append(f"HIST-17={verdict}")
    if "HIST-15" not in collected.named:
        collected.matrix.append(f"HIST-15={directory / 'trace-completeness-verdict.json'}")
    document["matrix_evidence"] = collected.matrix
    # The run-generated manifest is the only way a source naming no execution binds to this
    # one; written once, after every evidence-producing step and before the verdicts.
    manifest_path = write_json_atomic(
        directory / MATRIX_MANIFEST,
        {
            **matrix_manifest(c.acceptance_execution_id, collected.matrix, collected.named),
            **collected.legs,
        },
    )
    document["matrix_manifest"] = str(manifest_path)
    verdicts = ports.run_step("acceptance-verdicts", verdicts_argv(inputs, collected.matrix), 3_600)
    steps.append(verdicts)
    document["disposition"] = "PASS" if verdicts.returncode == 0 else "NOT_PROVEN"


def verify_reuse(directory: Path, digests: Path) -> dict[str, str]:
    """Refuse unless every reused artifact exists with exactly its recorded sha256."""
    import hashlib

    recorded: dict[str, str] = {}
    for line in digests.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.strip().partition("  ")
        if digest and name:
            recorded[name] = digest
    for name in RESUME_REUSED:
        path = directory / name
        if name not in recorded:
            raise AcceptanceError(f"no recorded digest for reused {name}")
        if not path.is_file():
            raise AcceptanceError(f"the reused {name} is missing")
        if hashlib.sha256(path.read_bytes()).hexdigest() != recorded[name]:
            raise AcceptanceError(f"the reused {name} changed since its digest was recorded")
    return {name: recorded[name] for name in RESUME_REUSED}


def _original_window(directory: Path) -> tuple[dict[str, object], dict[str, object]]:
    """The original run's PASS legs; anything the original itself left NOT_PROVEN is refused."""
    control_document = json.loads((directory / control.RECEIPT_NAME).read_text(encoding="utf-8"))
    if control_document.get("disposition") != "PASS":
        raise AcceptanceError("the original control is not PASS; it cannot be reused")
    footnote = json.loads((directory / FOOTNOTE_RECEIPT).read_text(encoding="utf-8"))
    if footnote.get("passed") is not True:
        raise AcceptanceError("the original footnote readback is not PASS; it cannot be reused")
    run = json.loads((directory / RECEIPT_NAME).read_text(encoding="utf-8"))
    steps = {str(step["name"]): step for step in cast("list[dict[str, object]]", run["steps"])}
    for name in ("dev-state-preflight", "select-outside-list-item", "chat-paired-no-bake"):
        if steps.get(name, {}).get("returncode") != 0:
            raise AcceptanceError(f"the original {name} step did not pass")
    for name in ("chat-during-bake", "footnote-readback"):
        if steps.get(name, {}).get("returncode") != 0:
            raise AcceptanceError(f"the original {name} step did not pass")
    if not run.get("chat_binding"):
        raise AcceptanceError("the original run never bound its chat samples")
    return control_document, run


def resume_acceptance(inputs: AcceptanceInputs, ports: Ports) -> dict[str, object]:
    """Resume one execution after its PASS control: only the legs the original never finished.

    The original window's control, chat samples, selection, trace artifacts and footnote are
    reused only if each still has its recorded digest; the original run receipt is read for its
    step instants and stays as written. Its NOT_PROVEN P6(e) leg is never reused: P6(e), the
    census, the alarm window, terminal state, the manifest and the verdicts all run again here,
    each leg in its own `-resume` directory, and the manifest names both windows.
    """
    c = inputs.control
    directory = c.evidence_dir
    steps: list[StepResult] = []
    document: dict[str, object] = {
        "schema_version": RESUME_SCHEMA,
        "acceptance_execution_id": c.acceptance_execution_id,
        "started_at": _now(),
        "disposition": "NOT_PROVEN",
        "hold": "unchanged (set); clearing is a separate owner-authorized mutation",
    }
    # One resume per execution: its outputs are write-once, so a second resume would fail
    # midway on the first receipt it cannot overwrite (found by the x5 rehearsal). Refused
    # before anything is written; the earlier resume's receipt stays as it is.
    if (directory / RESUME_RECEIPT).exists() or (directory / OBSERVED_COHORT_RECEIPT).exists():
        return {
            **document,
            "failure": "AcceptanceError",
            "failure_reason": "this execution was already resumed; start a new execution",
        }
    try:
        digests = verify_reuse(directory, cast("Path", inputs.resume_digests))
        document["reused_sha256"] = digests
        control_document, run = _original_window(directory)
        original_steps = cast("list[dict[str, object]]", run["steps"])
        preflight = next(s for s in original_steps if s["name"] == "dev-state-preflight")
        document["legs"] = legs = {
            "original_window": {
                "started_at": run["started_at"],
                "ended_at": run.get("ended_at"),
                "probe_commit": _head_of(directory / "dev-state-preflight"),
                "legs": [
                    *original_steps,
                    {
                        "name": "rolling-freshness-control",
                        "started_at": control_document["started_at"],
                        "ended_at": control_document["ended_at"],
                        "disposition": control_document["disposition"],
                    },
                ],
                "not_reused": "the original P6(e) leg (NOT_PROVEN) and its p6e/ directory",
            },
            "resume_window": {"started_at": document["started_at"], "probe_commit": _head()},
        }
        _evidence_and_verdicts(
            inputs,
            ports,
            steps,
            document,
            Collected(
                preflight_started_at=str(preflight["started_at"]),
                preflight_at=str(preflight["ended_at"]),
                control_document=control_document,
                matrix=[*inputs.matrix_evidence, f"HIST-8={directory / FOOTNOTE_RECEIPT}"],
                named={pair.partition("=")[0] for pair in inputs.matrix_evidence},
                suffix="-resume",
                legs={"legs": legs},
            ),
        )
    except Exception as error:
        document["failure"] = type(error).__name__
        document["failure_reason"] = str(error)
    document["ended_at"] = _now()
    document["steps"] = [
        {
            "name": s.name,
            "returncode": s.returncode,
            "started_at": s.started_at,
            "ended_at": s.ended_at,
        }
        for s in steps
    ]
    from probe_ingestion_traceability_acceptance import write_json_atomic

    write_json_atomic(directory / RESUME_RECEIPT, document)
    return document


def _head() -> str:
    return subprocess.run(
        ["git", "-C", str(_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _head_of(state_dir: Path) -> str | None:
    """The HEAD a `make dev-state` record stamped, if one is retained."""
    for path in sorted(state_dir.glob("state-*.json")):
        head = json.loads(path.read_text(encoding="utf-8")).get("head")
        if isinstance(head, str):
            return head
    return None


# --- Live ports ----------------------------------------------------------------------------


@dataclass(slots=True)
class LivePorts:
    """The real subprocesses, control, strong reads and trace collection."""

    inputs: AcceptanceInputs
    session: Any
    graph_table: str

    def _log(self, name: str) -> Path:
        path = self.inputs.control.evidence_dir / "logs" / f"{name}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def run_step(self, name: str, argv: tuple[str, ...], timeout: float) -> StepResult:
        """Run one child to completion, its output in `logs/<name>.log`."""
        return self.start(name, argv)(timeout)

    def start(self, name: str, argv: tuple[str, ...]) -> Callable[[float], StepResult]:
        """Start one child (an argument tuple, never a shell) from the worktree root."""
        started = _now()
        log = self._log(name).open("w", encoding="utf-8")
        process = subprocess.Popen(argv, cwd=_ROOT, stdout=log, stderr=subprocess.STDOUT, text=True)

        def wait(timeout: float) -> StepResult:
            try:
                returncode: int | None = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                returncode = None
            log.close()
            return StepResult(name, returncode, started, _now())

        return wait

    def execute_control(
        self, runner: control.Runner, clock: control.Clock
    ) -> tuple[Path, dict[str, object]]:
        """Run the blocking control in this process with the observing runner."""
        return control.execute(
            self.inputs.control, runner, clock, on_launch=getattr(runner, "launched", None)
        )

    def head(self) -> Mapping[str, object] | None:
        """Strongly read the serving-domain head."""
        from domain.operations.execution_graph_release import INGESTION_RELEASE_DOMAIN

        reads = control.BotoControlReads(
            self.session.client("dynamodb"), self.graph_table, INGESTION_RELEASE_DOMAIN
        )
        return reads.head()

    def collect_trace(self, control_document: Mapping[str, object]) -> Mapping[str, object]:
        """Collect the execution's trace artifacts through the gate's gatherer."""
        from probe_ingestion_traceability_acceptance import collect_trace_artifacts

        return collect_trace_artifacts(
            self.session, self.inputs.control.environment, self.graph_table, control_document
        )

    def sleep(self, seconds: float) -> None:
        """Wait."""
        time.sleep(seconds)

    def now(self) -> datetime:
        """The current aware UTC instant."""
        return datetime.now(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    """Validate, then run every acceptance step; nonzero unless every step and verdict passed."""
    try:
        inputs = parse_acceptance(sys.argv[1:] if argv is None else argv)
    except (AcceptanceError, control.ControlError) as error:
        print(f"NOT_PROVEN: {error}", file=sys.stderr)
        return 2
    # Bounded SDK retries for transient connection and DNS errors, inherited by every child.
    os.environ.setdefault("AWS_RETRY_MODE", "standard")
    os.environ.setdefault("AWS_MAX_ATTEMPTS", "8")
    import boto3

    from adapters.jobs.execution_graph.resolution import resolve_graph_table

    c = inputs.control
    session = boto3.Session(profile_name=c.profile, region_name=c.region)
    if session.client("sts").get_caller_identity()["Account"] != c.expected_account:
        print("NOT_PROVEN: the explicit profile is not the expected account", file=sys.stderr)
        return 2
    table = resolve_graph_table(session, c.environment, c.graph_store_stack)
    if inputs.resume_digests is not None:
        document = resume_acceptance(inputs, LivePorts(inputs, session, table))
        name = RESUME_RECEIPT
    else:
        document = run_acceptance(inputs, LivePorts(inputs, session, table))
        name = RECEIPT_NAME
    print(f"receipt: {c.evidence_dir / name} disposition={document['disposition']}")
    return 0 if document["disposition"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
