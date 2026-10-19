# SPDX-License-Identifier: MIT-0
"""Plan item 13: the one acceptance entrypoint's ordering and its paired-chat coordination.

Every child, the control, the strong head reads and the trace collection are faked; nothing
here lands, sends, or reads anything. Fictional data only.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from botocore.exceptions import EndpointConnectionError

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from test_probe_ingestion_traceability_acceptance import (  # noqa: E402
    _BAKE_END,  # pyright: ignore[reportPrivateUsage]
    _BAKE_START,  # pyright: ignore[reportPrivateUsage]
    _artifacts,  # pyright: ignore[reportPrivateUsage]
)
from validation import probe_footnote_readback as footnote  # noqa: E402
from validation import probe_turn_latency as latency  # noqa: E402
from validation import run_graph_acceptance as acceptance_run  # noqa: E402

control = acceptance_run.control  # the one control module the orchestrator drives

pytestmark = pytest.mark.unit

_MANIFEST = _ROOT / "validation" / "fixtures" / "graph-rolling-freshness-control-v1.json"
_EXECUTION = "exec-orchestrated-1"


def _argv(tmp_path: pathlib.Path, **overrides: str) -> list[str]:
    (tmp_path / ".env").write_text("{}", encoding="utf-8")
    values = {
        "--profile": "fictional-profile",
        "--environment": "dev",
        "--region": "us-east-1",
        "--expected-account": "111122223333",
        "--data-stack": "Adlytics-DataV2-dev",
        "--bucket": "fictional-raw",
        "--graph-store-stack": "Adlytics-ControlPlaneAuthorityStore-dev",
        "--manifest": str(_MANIFEST),
        "--acceptance-execution-id": _EXECUTION,
        "--evidence-dir": str(tmp_path / "acceptance" / _EXECUTION),
        "--accepted-generation": "gen-7",
        "--generation-unit": "unit-1",
        "--env-file": str(tmp_path / ".env"),
        "--lock-file": str(tmp_path / "dev-deploy.lock"),
        **overrides,
    }
    return [item for pair in values.items() for item in pair]


def _wave_a_count() -> int:
    _, entries = control.load_manifest(_MANIFEST)
    return sum(1 for entry in entries if entry.roster == "wave_a")


def _draft(phase: str, start: datetime, preflight: str) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    begin = start
    for prompt in ("p1", "p2", "p3", "p4", "p5", "outside_bounded_campaign"):
        row: dict[str, object] = {
            "prompt_id": prompt,
            "request_started_at": begin.isoformat(),
            "request_ended_at": (begin + timedelta(seconds=1)).isoformat(),
            "latency_ms": 1000,
        }
        if phase == "during_bake":
            row["head_active_receipt"] = {"state": "ACTIVE", "cohort_revision": "c-a"}
        rows.append(row)
        begin += timedelta(seconds=10)
    return {
        "schema_version": latency.CHAT_DRAFT_SCHEMA,
        "phase": phase,
        "preflight_completed_at": preflight,
        "sample_started_at": start.isoformat(),
        "sample_ended_at": rows[-1]["request_ended_at"],
        "rows": rows,
    }


_SCOPED: dict[str, object] = {
    "alarm": "adlytics-test-evaluation-worst-label",
    "state": "ALARM",
    "owner": "D2",
}


@dataclass
class _Ports:
    """Faked ports; the control drives the observing runner like the real one."""

    head_state: dict[str, object] = field(default_factory=dict[str, object])
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    control_disposition: str = "PASS"
    during_started: threading.Event = field(default_factory=threading.Event)
    clock: list[datetime] = field(
        default_factory=lambda: [datetime(2026, 10, 15, 9, 0, tzinfo=UTC)]
    )

    def _tick(self) -> str:
        self.clock[0] += timedelta(minutes=1)
        return self.clock[0].isoformat()

    def run_step(
        self, name: str, argv: tuple[str, ...], timeout: float
    ) -> acceptance_run.StepResult:
        return self.start(name, argv)(timeout)

    def start(
        self, name: str, argv: tuple[str, ...]
    ) -> Callable[[float], acceptance_run.StepResult]:
        started = self._tick()
        self.calls.append(("start", name))
        if name == "select-outside-list-item":
            output = pathlib.Path(argv[argv.index("--output") + 1])
            assert argv[argv.index("--acceptance-execution-id") + 1] == _EXECUTION
            assert argv[argv.index("--generation-unit") + 1] == "unit-1"
            assert "--graph-store-stack" in argv and "--select-outside-list-item" in argv
            output.write_text("{}", encoding="utf-8")
        if name.startswith("chat-"):
            receipt = pathlib.Path(argv[argv.index("--outside-selection-receipt") + 1])
            assert receipt.is_file()
            assert receipt.parent == pathlib.Path(argv[argv.index("--telemetry-dir") + 1])
        if name == "footnote-readback":
            assert argv[argv.index("--acceptance-execution-id") + 1] == _EXECUTION
            assert argv[argv.index("--not-before") + 1] == _BAKE_END.isoformat()
            output = pathlib.Path(argv[argv.index("--output") + 1])
            output.write_text(
                json.dumps({"acceptance_execution_id": _EXECUTION, "passed": True}),
                encoding="utf-8",
            )
        if name in {"check-alarm-window", "dev-state-terminal"}:
            directory = next(a for a in argv if a.startswith("EVIDENCE_DIR=")).split("=", 1)[1]
            record = {"report": {"out_of_scope": [_SCOPED]}}
            (pathlib.Path(directory) / f"{name}-x.json").write_text(
                json.dumps(record), encoding="utf-8"
            )
        if name.startswith("chat-"):
            output = pathlib.Path(argv[argv.index("--output") + 1])
            preflight = argv[argv.index("--preflight-completed-at") + 1]
            phase = "during_bake" if name == "chat-during-bake" else "paired_no_bake"
            begin = datetime(2026, 10, 15, 10, 15 if phase == "during_bake" else 9, tzinfo=UTC)
            output.write_text(json.dumps(_draft(phase, begin, preflight)), encoding="utf-8")
            if phase == "during_bake":
                assert argv[argv.index("--target-cohort-revision") + 1] == "c-a"
                self.during_started.set()
        return lambda _timeout: acceptance_run.StepResult(name, 0, started, self._tick())

    def execute_control(
        self, runner: control.Runner, clock: control.Clock
    ) -> tuple[pathlib.Path, dict[str, object]]:
        del clock
        self.calls.append(("control", "start"))
        wave_a: list[str] = []
        for index in range(_wave_a_count()):
            run_id = f"run-a{index}"
            key = f"amc/window={index}/retrieved=x"
            argv = (
                "python",
                "--operation-id",
                f"graph-roll-x-wave-a-{index}-launch",
                "--reserved-probe-key",
                key,
                "--twin-only",
            )
            twin = {
                "mode": "GRAPH",
                "sent": True,
                "decision": "ADMITTED",
                "version_id": "v1",
                "receipt": {"run_id": run_id, "manifest_key": key, "version_id": "v1"},
            }
            control_stdout = f"PROBE_TWIN {json.dumps(twin)}\n"
            acceptance_run.control.run_child = lambda _a, _t, s=control_stdout: control.ChildResult(
                0, s
            )  # type: ignore[assignment]
            runner(argv, 1)
            wave_a.append(run_id)
        self.head_state = {
            "state": "ACTIVE",
            "elected_run_ids": wave_a,
            "cohort_revision": "c-a",
            "winner_run_id": "run-a",
        }
        assert self.during_started.wait(5), "the during-bake sample never started"
        self.calls.append(("control", "end"))
        document: dict[str, object] = {
            "acceptance_execution_id": _EXECUTION,
            "disposition": self.control_disposition,
            "ended_at": (_BAKE_END - timedelta(hours=1)).isoformat(),
            "run_ids": {"single": "run-s", **{f"a{i}": r for i, r in enumerate(wave_a)}},
            "observations": {
                "a_snapshot_before": {"winner_run_id": "run-a", "cohort_revision": "c-a"}
            },
        }
        return pathlib.Path("control.json"), document

    def head(self) -> Mapping[str, object] | None:
        return dict(self.head_state) or None

    def collect_trace(self, control_document: Mapping[str, object]) -> Mapping[str, object]:
        assert control_document["acceptance_execution_id"] == _EXECUTION
        self.calls.append(("collect", "trace"))
        return _artifacts()

    def sleep(self, seconds: float) -> None:
        del seconds

    def now(self) -> datetime:
        return datetime.now(UTC)


@pytest.fixture(autouse=True)
def _restore_run_child() -> Any:  # pyright: ignore[reportUnusedFunction] -- an autouse fixture
    original = control.run_child
    yield
    control.run_child = original  # type: ignore[assignment]


def test_the_steps_run_in_the_fixed_order_and_bind_both_samples(tmp_path: pathlib.Path) -> None:
    inputs = acceptance_run.parse_acceptance(_argv(tmp_path))
    ports = _Ports()

    document = acceptance_run.run_acceptance(inputs, ports)

    assert document["disposition"] == "PASS", document
    names = [name for kind, name in ports.calls if kind == "start"]
    verdicts = acceptance_run.verdicts_argv(inputs, [])
    assert "--outside-list-item" not in verdicts  # the gate reads the retained receipt itself
    assert names[:3] == ["dev-state-preflight", "select-outside-list-item", "chat-paired-no-bake"]
    assert names[3:] == [
        "chat-during-bake",
        "footnote-readback",
        "check-alarm-window",
        "dev-state-terminal",
        "acceptance-verdicts",
    ]
    # The baseline finished before the control (and so before the first root launch) began.
    assert ports.calls.index(("start", "chat-paired-no-bake")) < ports.calls.index(
        ("control", "start")
    )
    assert ports.calls.index(("control", "end")) < ports.calls.index(("collect", "trace"))
    directory = inputs.control.evidence_dir
    for name in ("chat-latency-paired-no-bake.json", "chat-latency-during-bake.json"):
        sample = json.loads((directory / name).read_text())
        assert sample["schema_version"] == "chat-latency-sample-v1"
        assert sample["bake_run_started_at"] == _BAKE_START.isoformat()
        assert sample["bake_finalize_ended_at"] == _BAKE_END.isoformat()
        assert sample["target_cohort_revision"] == "c-a"
    receipt = json.loads((directory / acceptance_run.RECEIPT_NAME).read_text())
    assert receipt["a_active_read"]["state"] == "ACTIVE"
    matrix = receipt["matrix_evidence"]
    assert any(item.startswith("HIST-15=") for item in matrix)
    assert any(item.startswith("HIST-17=") for item in matrix)
    hist17 = json.loads((directory / "alarm-window-verdict.json").read_text())
    assert hist17["passed"] is True
    # The owned evaluation alarm is named in the HIST-17 row, never folded into a clean pass.
    assert hist17["out_of_graph_scope"] == {
        "alarm_window": [_SCOPED],
        "terminal_state": [_SCOPED],
    }
    assert hist17["terminal_record"].endswith("dev-state-terminal-x.json")
    assert f"HIST-8={directory / acceptance_run.FOOTNOTE_RECEIPT}" in matrix
    # The run-generated manifest binds each source to this execution by path and digest; the
    # receipt the verdict mode writes later records no digest and binds by its own ID only.
    manifest = json.loads((directory / "matrix-evidence-manifest-v1.json").read_text())
    assert manifest["acceptance_execution_id"] == _EXECUTION
    assert receipt["matrix_manifest"] == str(directory / "matrix-evidence-manifest-v1.json")
    entries = manifest["entries"]
    assert set(entries) == {item.partition("=")[0] for item in matrix}
    assert len(entries["HIST-17"]["sha256"]) == 64
    assert entries["HIST-17"]["origin"] == "run"
    assert entries["HIST-15"]["sha256"] is None


def test_p6e_reads_this_runs_own_span_window_and_every_run(tmp_path: pathlib.Path) -> None:
    p6e_args = tmp_path / "p6e-args.json"
    p6e_args.write_text(json.dumps(["--probe", "P6", "--p6-step", "e"]), encoding="utf-8")
    inputs = acceptance_run.parse_acceptance(_argv(tmp_path, **{"--p6e-args": str(p6e_args)}))
    seen: dict[str, tuple[str, ...]] = {}

    class _P6Ports(_Ports):
        def start(
            self, name: str, argv: tuple[str, ...]
        ) -> Callable[[float], acceptance_run.StepResult]:
            if name == "p6e":
                seen[name] = argv
                evidence = pathlib.Path(argv[argv.index("--evidence-dir") + 1])
                (evidence / "P6-e-x.json").write_text(
                    json.dumps({"checks": {"observed_cohort": {"passed": True}}}),
                    encoding="utf-8",
                )
            return super().start(name, argv)

    document = acceptance_run.run_acceptance(inputs, _P6Ports())

    assert document["disposition"] == "PASS", document
    argv = seen["p6e"]
    preflight = next(s for s in document["steps"] if s["name"] == "dev-state-preflight")  # type: ignore[union-attr]
    assert argv[argv.index("--window-start") + 1] == preflight["ended_at"]
    assert argv[argv.index("--window-end") + 1] == (_BAKE_END - timedelta(hours=1)).isoformat()
    assert argv.count("--run-id") == 1 + _wave_a_count()
    observed = json.loads(
        (inputs.control.evidence_dir / "observed-cohort-verdict.json").read_text()
    )
    assert observed["acceptance_execution_id"] == _EXECUTION and observed["passed"] is True


def test_the_footnote_receipt_binds_this_execution_and_its_verdict() -> None:
    readback: dict[str, object] = {
        "generation": {"built_at": "2026-10-14T05:25:34Z"},
        "window_end_iso": "2026-10-06",
        "read_at": "2026-10-15T20:10:00.000Z",
    }
    page: dict[str, object] = {
        "page_text": "Data as of Oct 6, 2026, built 2026-10-14 05:25:34 UTC.",
        "asset_texts": [],
    }
    passed = footnote.receipt(
        readback, page, execution_id=_EXECUTION, not_before="2026-10-15T20:05:00+00:00"
    )
    assert passed["acceptance_execution_id"] == _EXECUTION
    assert passed["passed"] is True
    assert not {"page_text", "asset_texts"} & set(passed)  # the full texts are never retained
    stale = footnote.receipt(
        readback,
        {**page, "page_text": "Data as of Oct 6, 2026."},
        execution_id=_EXECUTION,
        not_before="2026-10-15T20:05:00+00:00",
    )
    assert stale["passed"] is False


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (subprocess.CalledProcessError(1, ["node"]), {"kind": "exit", "returncode": 1}),
        (subprocess.TimeoutExpired(["node"], 180), {"kind": "timeout", "timeout_seconds": 180}),
    ],
)
def test_a_failed_render_still_writes_a_not_proven_footnote_receipt(
    error: subprocess.CalledProcessError | subprocess.TimeoutExpired, failure: dict[str, object]
) -> None:
    readback = footnote.render_failed(error)
    document = footnote.receipt(
        readback, {}, execution_id=_EXECUTION, not_before="2000-01-01T00:00:00+00:00"
    )
    assert cast("dict[str, object]", document["readback"])["render_failed"] == failure
    assert "render_failed" in cast("list[str]", document["reasons"])
    assert document["disposition"] == "NOT_PROVEN"


def test_a_not_proven_control_stops_before_any_collection(tmp_path: pathlib.Path) -> None:
    inputs = acceptance_run.parse_acceptance(_argv(tmp_path))
    ports = _Ports(control_disposition="NOT_PROVEN")

    document = acceptance_run.run_acceptance(inputs, ports)

    assert document["disposition"] == "NOT_PROVEN"
    assert ("collect", "trace") not in ports.calls
    assert "acceptance-verdicts" not in [name for _, name in ports.calls]


def test_the_watcher_survives_a_transient_endpoint_failure_and_still_samples() -> None:
    observer = acceptance_run.LaunchObserver(control.run_child, 1)
    observer.wave_a.append("run-a0")
    observer.complete.set()
    active = {"state": "ACTIVE", "elected_run_ids": ["run-a0"], "cohort_revision": "c-a"}
    failures = [EndpointConnectionError(endpoint_url="https://dynamodb")]

    class _FlakyPorts(_Ports):
        def head(self) -> Mapping[str, object] | None:
            if failures:
                raise failures.pop()
            return active

        def start(
            self, name: str, argv: tuple[str, ...]
        ) -> Callable[[float], acceptance_run.StepResult]:
            del argv
            return lambda _timeout: acceptance_run.StepResult(name, 0, "s", "e")

    ports = _FlakyPorts()
    outcome = acceptance_run.DuringBake()

    acceptance_run.watch_during_bake(
        ports, observer, lambda _cohort: ("x",), threading.Event(), outcome, poll_seconds=0
    )

    assert outcome.failure is None
    assert outcome.transient_read_failures == 1
    assert outcome.result is not None and outcome.result.name == "chat-during-bake"


def test_a_crashed_watcher_aborts_the_control_with_a_named_not_proven_receipt(
    tmp_path: pathlib.Path,
) -> None:
    # x4: the watcher thread died unhandled and the control polled on for over an hour.
    inputs = acceptance_run.parse_acceptance(_argv(tmp_path))

    class _CrashPorts(_Ports):
        def head(self) -> Mapping[str, object] | None:
            raise RuntimeError("unexpected")

        def execute_control(
            self, runner: control.Runner, clock: control.Clock
        ) -> tuple[pathlib.Path, dict[str, object]]:
            del runner
            for _ in range(200):  # the control's waits, until the abort refuses one
                try:
                    clock.sleep(0.01)
                except control.ControlError as error:
                    return pathlib.Path("c.json"), {
                        "disposition": "NOT_PROVEN",
                        "failure_reason": str(error),
                    }
                if not self.calls or self.calls[-1] != ("control", "waiting"):
                    self.calls.append(("control", "waiting"))
                    acceptance_run.control.run_child = lambda _a, _t: control.ChildResult(0, "")  # type: ignore[assignment]
            return pathlib.Path("c.json"), {"disposition": "PASS"}

    ports = _CrashPorts()
    original = acceptance_run.LaunchObserver.__init__

    def observed(self: acceptance_run.LaunchObserver, inner: Any, expected: int) -> None:
        original(self, inner, expected)
        self.complete.set()  # every wave-A launch returned; the watcher now reads the head

    acceptance_run.LaunchObserver.__init__ = observed  # type: ignore[method-assign]
    try:
        document = acceptance_run.run_acceptance(inputs, ports)
    finally:
        acceptance_run.LaunchObserver.__init__ = original  # type: ignore[method-assign]

    assert document["disposition"] == "NOT_PROVEN"
    reason = str(document["failure_reason"])
    assert "aborted: the during-bake watcher failed" in reason
    assert "RuntimeError: unexpected" in reason


def test_the_watcher_refuses_an_active_snapshot_that_is_not_exactly_wave_a() -> None:
    observer = acceptance_run.LaunchObserver(control.run_child, 1)
    observer.wave_a.append("run-a0")
    observer.complete.set()
    ports = _Ports(head_state={"state": "ACTIVE", "elected_run_ids": ["run-a0", "run-b0"]})
    outcome = acceptance_run.DuringBake()

    acceptance_run.watch_during_bake(
        ports, observer, lambda _cohort: ("x",), threading.Event(), outcome, poll_seconds=0
    )

    assert outcome.failure == "A's ACTIVE snapshot is not exactly the wave-A runs"
    assert outcome.result is None


def test_the_watcher_never_samples_when_the_control_stops_first() -> None:
    observer = acceptance_run.LaunchObserver(control.run_child, 2)
    stop = threading.Event()
    stop.set()
    outcome = acceptance_run.DuringBake()

    acceptance_run.watch_during_bake(
        _Ports(), observer, lambda _c: ("x",), stop, outcome, poll_seconds=0
    )

    assert outcome.failure == "the control ended before every wave-A launch returned"


def test_an_unheld_launch_stamps_the_first_launch_and_its_hook_names_wave_a() -> None:
    observer = acceptance_run.LaunchObserver(lambda _argv, _timeout: control.ChildResult(0, ""), 2)
    observer(("x", "--operation-id", "graph-roll-1-single-root-2-reserve", "--no-twin"), 1.0)
    assert observer.first_launch_at is None  # a held reservation is not a launch
    observer(("x", "--operation-id", "graph-roll-1-single-root-2-launch", "--no-twin"), 1.0)
    assert observer.first_launch_at is not None

    observer.launched("single_root", ["run-s"])
    assert not observer.complete.is_set()
    observer.launched("wave_a", ["run-a1", "run-a2"])
    assert observer.complete.is_set()
    assert observer.wave_a == ["run-a1", "run-a2"]


def test_inputs_follow_the_controls_own_refusals(tmp_path: pathlib.Path) -> None:
    with pytest.raises(control.ControlError, match="only against dev"):
        acceptance_run.parse_acceptance(_argv(tmp_path, **{"--environment": "prod"}))
    existing = tmp_path / "acceptance" / "exists"
    existing.mkdir(parents=True)
    with pytest.raises(control.ControlError, match="new directory"):
        acceptance_run.parse_acceptance(_argv(tmp_path, **{"--evidence-dir": str(existing)}))
    with pytest.raises(acceptance_run.AcceptanceError, match="gap-seconds"):
        acceptance_run.parse_acceptance(_argv(tmp_path, **{"--gap-seconds": "500"}))


def test_both_chat_phases_share_every_identity_input(tmp_path: pathlib.Path) -> None:
    inputs = acceptance_run.parse_acceptance(_argv(tmp_path))
    baseline = acceptance_run.chat_argv(inputs, "paired-no-bake", tmp_path / "a", "t0", None)
    during = acceptance_run.chat_argv(inputs, "during-bake", tmp_path / "b", "t0", "c-a")

    def identity(argv: tuple[str, ...]) -> list[str]:
        skip = {"--phase", "--output", "--target-cohort-revision"}
        kept: list[str] = []
        items = iter(argv)
        for item in items:
            if item in skip:
                next(items)
                continue
            kept.append(item)
        return kept

    assert identity(baseline) == identity(during)
    parser = latency._parser()  # pyright: ignore[reportPrivateUsage] -- the real probe parser
    parser.parse_args(list(baseline[2:]))
    parser.parse_args(list(during[2:]))


def _resumable(tmp_path: pathlib.Path) -> tuple[acceptance_run.AcceptanceInputs, pathlib.Path]:
    """Run one fake acceptance whose P6(e) fails, then digest its reused artifacts."""
    p6e_args = tmp_path / "p6e-args.json"
    p6e_args.write_text(json.dumps(["--probe", "P6", "--p6-step", "e"]), encoding="utf-8")
    inputs = acceptance_run.parse_acceptance(_argv(tmp_path, **{"--p6e-args": str(p6e_args)}))

    class _FailingP6(_Ports):
        def start(
            self, name: str, argv: tuple[str, ...]
        ) -> Callable[[float], acceptance_run.StepResult]:
            waiting = super().start(name, argv)
            if name == "p6e":
                return lambda _t: acceptance_run.StepResult(name, 1, "s", "e")
            if name == "footnote-readback":
                output = pathlib.Path(argv[argv.index("--output") + 1])
                output.write_text(json.dumps({"passed": True}), encoding="utf-8")
            return waiting

    first = acceptance_run.run_acceptance(inputs, _FailingP6())
    assert first["disposition"] == "NOT_PROVEN" and "p6e" in str(first["failure_reason"])
    directory = inputs.control.evidence_dir
    # The real control writes its receipt into the execution directory; the fake does not.
    (directory / "rolling-freshness-control-v1.json").write_text(
        json.dumps(
            {
                "acceptance_execution_id": _EXECUTION,
                "disposition": "PASS",
                "started_at": "2026-10-15T09:05:00+00:00",
                "ended_at": (_BAKE_END - timedelta(hours=1)).isoformat(),
                "run_ids": {"single": "run-s", "a0": "run-a0"},
                "observations": {
                    "a_snapshot_before": {"winner_run_id": "run-a", "cohort_revision": "c-a"}
                },
            }
        ),
        encoding="utf-8",
    )
    digests = tmp_path / "digests.txt"
    digests.write_text(
        "".join(
            f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}\n"
            for name in acceptance_run.RESUME_REUSED
        ),
        encoding="utf-8",
    )
    resumed = acceptance_run.parse_acceptance(
        _argv(tmp_path, **{"--p6e-args": str(p6e_args), "--resume-after-control": str(digests)})
    )
    return resumed, digests


def test_a_resume_runs_only_the_legs_after_the_control_in_their_own_directories(
    tmp_path: pathlib.Path,
) -> None:
    inputs, _ = _resumable(tmp_path)

    class _P6Ports(_Ports):
        def start(
            self, name: str, argv: tuple[str, ...]
        ) -> Callable[[float], acceptance_run.StepResult]:
            if name == "p6e":
                evidence = pathlib.Path(argv[argv.index("--evidence-dir") + 1])
                (evidence / "P6-e-x.json").write_text(
                    json.dumps({"checks": {"observed_cohort": {"passed": True}}}),
                    encoding="utf-8",
                )
            return super().start(name, argv)

    ports = _P6Ports()
    document = acceptance_run.resume_acceptance(inputs, ports)

    assert document["disposition"] == "PASS", document
    names = [name for kind, name in ports.calls if kind == "start"]
    assert names == ["p6e", "check-alarm-window", "dev-state-terminal", "acceptance-verdicts"]
    assert ("control", "start") not in ports.calls
    directory = inputs.control.evidence_dir
    assert (directory / "p6e-resume" / "P6-e-x.json").is_file()
    manifest = json.loads((directory / "matrix-evidence-manifest-v1.json").read_text())
    legs = manifest["legs"]
    assert set(legs) == {"original_window", "resume_window"}
    assert any(
        leg["name"] == "rolling-freshness-control" for leg in legs["original_window"]["legs"]
    )
    assert "HIST-8" in manifest["entries"]
    assert (directory / acceptance_run.RESUME_RECEIPT).is_file()
    assert json.loads((directory / acceptance_run.RECEIPT_NAME).read_text())["disposition"] == (
        "NOT_PROVEN"
    )  # the original receipt stays as written


@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_a_resume_refuses_a_changed_or_missing_reused_artifact(
    tmp_path: pathlib.Path, damage: str
) -> None:
    inputs, _ = _resumable(tmp_path)
    during = inputs.control.evidence_dir / "chat-latency-during-bake.json"
    if damage == "changed":
        during.write_text(during.read_text() + " ", encoding="utf-8")
    else:
        during.unlink()
    document = acceptance_run.resume_acceptance(inputs, _Ports())
    assert document["disposition"] == "NOT_PROVEN"
    expected = "changed since" if damage == "changed" else "is missing"
    assert f"chat-latency-during-bake.json {expected}" in str(document["failure_reason"])


def test_a_resume_refuses_an_original_control_that_was_not_proven(
    tmp_path: pathlib.Path,
) -> None:
    inputs, digests = _resumable(tmp_path)
    directory = inputs.control.evidence_dir
    control_path = directory / "rolling-freshness-control-v1.json"
    control_path.write_text(json.dumps({"disposition": "NOT_PROVEN"}), encoding="utf-8")
    lines = [
        line
        for line in digests.read_text().splitlines()
        if not line.endswith("rolling-freshness-control-v1.json")
    ]
    digest = hashlib.sha256(control_path.read_bytes()).hexdigest()
    lines.append(f"{digest}  rolling-freshness-control-v1.json")
    digests.write_text("\n".join(lines) + "\n", encoding="utf-8")
    document = acceptance_run.resume_acceptance(inputs, _Ports())
    assert document["disposition"] == "NOT_PROVEN"
    assert "original control is not PASS" in str(document["failure_reason"])


def test_a_second_resume_of_one_execution_is_refused_before_any_write(
    tmp_path: pathlib.Path,
) -> None:
    inputs, _ = _resumable(tmp_path)

    class _P6Ports(_Ports):
        def start(
            self, name: str, argv: tuple[str, ...]
        ) -> Callable[[float], acceptance_run.StepResult]:
            if name == "p6e":
                evidence = pathlib.Path(argv[argv.index("--evidence-dir") + 1])
                (evidence / "P6-e-x.json").write_text(
                    json.dumps({"checks": {"observed_cohort": {"passed": True}}}),
                    encoding="utf-8",
                )
            return super().start(name, argv)

    assert acceptance_run.resume_acceptance(inputs, _P6Ports())["disposition"] == "PASS"
    receipt = inputs.control.evidence_dir / acceptance_run.RESUME_RECEIPT
    before = receipt.read_bytes()
    ports = _Ports()
    again = acceptance_run.resume_acceptance(inputs, ports)
    assert again["disposition"] == "NOT_PROVEN"
    assert "already resumed" in str(again["failure_reason"])
    assert ports.calls == [] and receipt.read_bytes() == before


def test_the_trace_is_collected_only_after_the_lag_past_the_windows_end(
    tmp_path: pathlib.Path,
) -> None:
    # x5: waiting the lag after the control's end left the window's 5-minute tail inside the
    # lag, so the promoter retrieval read `pending`. The wait now runs from the window's end.
    inputs = acceptance_run.parse_acceptance(_argv(tmp_path))
    ended = _BAKE_END - timedelta(hours=1)
    slept: list[float] = []

    class _ClockPorts(_Ports):
        def now(self) -> datetime:
            return ended + timedelta(seconds=60)

        def sleep(self, seconds: float) -> None:
            slept.append(seconds)

    acceptance_run.run_acceptance(inputs, _ClockPorts())
    lag = acceptance_run.SPAN_LAG_SECONDS
    assert max(slept) == lag + 300 - 60  # the old wait would have been lag - 60


@pytest.mark.parametrize(
    "case",
    [
        (0, 0, True, True),
        # Paired controls: either read failing, or no terminal record, fails HIST-17.
        (3, 0, True, False),
        (0, 3, True, False),
        (0, 0, False, False),
    ],
)
def test_hist17_needs_a_clean_window_and_a_clean_terminal_state(
    tmp_path: pathlib.Path, case: tuple[int, int, bool, bool]
) -> None:
    window_code, terminal_code, terminal_written, passed = case
    window = tmp_path / "alarm-window-x.json"
    window.write_text(json.dumps({"report": {"out_of_scope": [_SCOPED]}}), encoding="utf-8")
    terminal = tmp_path / "state-x.json"
    terminal.write_text(json.dumps({"report": {"out_of_scope": []}}), encoding="utf-8")

    path = acceptance_run.alarm_verdict(
        _EXECUTION,
        "2026-10-15T09:00:00+00:00",
        (acceptance_run.StepResult("check-alarm-window", window_code, "a", "b"), window),
        (
            acceptance_run.StepResult("dev-state-terminal", terminal_code, "c", "d"),
            terminal if terminal_written else None,
        ),
        tmp_path,
    )

    verdict = json.loads(path.read_text())
    assert verdict["passed"] is passed
    assert verdict["out_of_graph_scope"]["alarm_window"] == [_SCOPED]
