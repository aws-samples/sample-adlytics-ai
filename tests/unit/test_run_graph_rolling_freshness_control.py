# SPDX-License-Identifier: MIT-0
"""The D7 single-root plus A/B control driver: inputs, real-parser children, and its verdict.

Every child command is checked with the real `land_fixtures.__main__._parse_args`; subprocesses
and AWS reads are faked, so nothing here lands, sends, or reads anything. Fictional data only.
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from botocore.exceptions import EndpointConnectionError

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import run_graph_rolling_freshness_control as control  # noqa: E402

from adapters.jobs.land_fixtures.__main__ import (  # noqa: E402
    _parse_args,  # pyright: ignore[reportPrivateUsage]
)

pytestmark = pytest.mark.unit

_MANIFEST = _ROOT / "validation" / "fixtures" / "graph-rolling-freshness-control-v1.json"
_WINDOW = "amc/fictional/window={label}/"


def _inputs(tmp_path: pathlib.Path, **overrides: Any) -> control.Inputs:
    values: dict[str, Any] = {
        "profile": "fictional-profile",
        "environment": "dev",
        "region": "us-east-1",
        "expected_account": "111122223333",
        "data_stack": "Adlytics-DataV2-dev",
        "bucket": "fictional-raw",
        "graph_store_stack": "Adlytics-ControlPlaneAuthorityStore-dev",
        "manifest": _MANIFEST,
        "acceptance_execution_id": "exec-1",
        "evidence_dir": tmp_path / "evidence" / "run-1",
    }
    values.update(overrides)
    return control.Inputs(**values)


def _argv(tmp_path: pathlib.Path, **overrides: str) -> list[str]:
    values = {
        "--profile": "fictional-profile",
        "--environment": "dev",
        "--region": "us-east-1",
        "--expected-account": "111122223333",
        "--data-stack": "Adlytics-DataV2-dev",
        "--bucket": "fictional-raw",
        "--graph-store-stack": "Adlytics-ControlPlaneAuthorityStore-dev",
        "--manifest": str(_MANIFEST),
        "--acceptance-execution-id": "exec-1",
        "--evidence-dir": str(tmp_path / "evidence" / "run-1"),
        **overrides,
    }
    return [item for pair in values.items() for item in pair]


def test_inputs_are_validated_before_any_aws_access(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    parsed = control.parse_inputs(_argv(tmp_path), root)
    assert parsed.environment == "dev" and not parsed.synthetic_full_plan
    for overrides, message in (
        ({"--environment": "prod"}, "only against dev"),
        ({"--profile": "default"}, "nondefault profile"),
        ({"--expected-account": "12345"}, "12-digit"),
        ({"--manifest": str(tmp_path / "missing.json")}, "unreadable"),
        ({"--evidence-dir": str(tmp_path / "outside")}, "under validation/evidence"),
        ({"--bucket": "  "}, "must not be blank"),
    ):
        with pytest.raises(control.ControlError, match=message):
            control.parse_inputs(_argv(tmp_path, **overrides), root)
    existing = root / "existing"
    existing.mkdir()
    with pytest.raises(control.ControlError, match="new directory"):
        control.parse_inputs(_argv(tmp_path, **{"--evidence-dir": str(existing)}), root)


def test_a_reused_acceptance_execution_id_is_refused(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "evidence"
    (root / "earlier").mkdir(parents=True)
    (root / "earlier" / control.RECEIPT_NAME).write_text(
        json.dumps({"acceptance_execution_id": "exec-1"})
    )
    with pytest.raises(control.ControlError, match="already used"):
        control.parse_inputs(_argv(tmp_path), root)
    assert control.parse_inputs(_argv(tmp_path, **{"--acceptance-execution-id": "exec-2"}), root)


def test_the_committed_manifest_has_the_four_rosters_and_one_single_root() -> None:
    digest, entries = control.load_manifest(_MANIFEST)
    assert len(digest) == 64
    rosters = {roster: [e for e in entries if e.roster == roster] for roster in control.ROSTERS}
    assert len(rosters["single_root"]) == 1
    assert rosters["wave_a"] and rosters["wave_b"] and rosters["synthetic_full_plan"]
    sources = [e.source for e in entries]
    assert len(set(sources)) == len(sources)
    assert len(rosters["synthetic_full_plan"]) == 98


@pytest.mark.parametrize(
    ("rosters", "message"),
    [
        (
            {"single_root": {"a": "s1"}, "wave_a": {"b": "s2"}, "wave_b": {"c": "s3"}},
            "four rosters",
        ),
        (
            {
                "single_root": {"a": "s1", "x": "s9"},
                "wave_a": {"b": "s2"},
                "wave_b": {"c": "s3"},
                "synthetic_full_plan": {"d": "s4"},
            },
            "exactly one root",
        ),
        (
            {
                "single_root": {"a": "s1"},
                "wave_a": {"a": "s2"},
                "wave_b": {"c": "s3"},
                "synthetic_full_plan": {"d": "s4"},
            },
            "label repeats",
        ),
        (
            {
                "single_root": {"a": "s1"},
                "wave_a": {"b": "s1"},
                "wave_b": {"c": "s3"},
                "synthetic_full_plan": {"d": "s4"},
            },
            "source repeats",
        ),
        (
            {
                "single_root": {"a": "s1"},
                "wave_a": {},
                "wave_b": {"c": "s3"},
                "synthetic_full_plan": {"d": "s4"},
            },
            "nonempty map",
        ),
    ],
)
def test_a_malformed_manifest_is_refused_before_any_child(
    tmp_path: pathlib.Path, rosters: dict[str, object], message: str
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": control.MANIFEST_SCHEMA, "rosters": rosters}))
    with pytest.raises(control.ControlError, match=message):
        control.load_manifest(path)


def test_operation_ids_are_deterministic_lower_case_and_bind_the_label() -> None:
    entry = control.Entry("wave_a", "Label-A", "source")
    first = control.operation_id("Exec-1", entry, "launch")
    assert first == control.operation_id("Exec-1", entry, "launch") == first.lower()
    assert first.startswith("graph-roll-") and "-wave-a-" in first and first.endswith("-launch")
    other = control.operation_id("Exec-1", control.Entry("wave_a", "label-a", "source"), "launch")
    assert other != first


def test_every_child_command_is_accepted_by_the_real_land_fixtures_parser(
    tmp_path: pathlib.Path,
) -> None:
    inputs = _inputs(tmp_path)
    entry = control.Entry("wave_a", "a1", "generated/fictional.csv#daily-run=1#chunk=1")
    plan = control.land_fixtures_argv(inputs, entry, "plan1")
    reserve = control.land_fixtures_argv(inputs, entry, "reserve", "amc/x/retrieved=1/m.json")
    launch = control.land_fixtures_argv(inputs, entry, "launch", "amc/x/retrieved=1/m.json")
    for argv in (plan, reserve, launch):
        assert argv[:3] == (sys.executable, "-m", "adapters.jobs.land_fixtures")
        assert all(isinstance(item, str) for item in argv)
        control.validate_with_real_parser(argv)
    assert _parse_args(plan[3:]).dry_run is True
    reserved = _parse_args(reserve[3:])
    assert (reserved.reserved_probe_key, reserved.no_twin, reserved.twin_only) == (
        "amc/x/retrieved=1/m.json",
        True,
        False,
    )
    twin = _parse_args(launch[3:])
    assert (twin.twin_only, twin.no_twin, twin.graph_store_stack) == (
        True,
        False,
        "Adlytics-ControlPlaneAuthorityStore-dev",
    )
    # The real parser refuses the forbidden shapes the driver must never build.
    for forbidden in (
        [*launch[3:], "--no-twin"],
        [*plan[3:], "--reserved-probe-key", "k", "--twin-only"],
        [
            item
            for item in launch[3:]
            if item not in {"--reserved-probe-key", "amc/x/retrieved=1/m.json"}
        ],
    ):
        with pytest.raises(SystemExit):
            _parse_args(tuple(forbidden))


@dataclass
class _Children:
    """A fake child runner: dry plans, reservations and launches by phase and label."""

    keys: dict[str, str] = field(default_factory=dict[str, str])
    windows: dict[str, str] = field(default_factory=dict[str, str])
    drift: set[str] = field(default_factory=set[str])
    fail: dict[str, control.ChildResult] = field(default_factory=dict[str, control.ChildResult])
    twins: dict[str, str] = field(default_factory=dict[str, str])
    calls: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])
    plans: dict[str, int] = field(default_factory=dict[str, int])

    def __call__(self, argv: tuple[str, ...], timeout: float) -> control.ChildResult:
        del timeout
        self.calls.append(argv)
        source = argv[argv.index("--probe-retrieval") + 1]
        operation = argv[argv.index("--operation-id") + 1]
        phase = operation.rsplit("-", 1)[1]
        label = source.removeprefix("src-")
        if f"{label}:{phase}" in self.fail:
            return self.fail[f"{label}:{phase}"]
        window = self.windows.get(label, _WINDOW.format(label=label))
        key = self.keys.get(label, f"{window}retrieved=20261006T000001Z/manifest.json")
        if phase.startswith("plan"):
            self.plans[label] = self.plans.get(label, 0) + 1
            if label in self.drift and self.plans[label] == 2:
                key = key.replace("000001Z", "000002Z")
            report = {"key": key, "window": window, "r": "x", "historical": False}
            return control.ChildResult(0, f"noise\nPROBE_RETRIEVAL {json.dumps(report)}\n")
        if phase == "reserve" or "--no-twin" in argv:
            report = {"key": argv[argv.index("--reserved-probe-key") + 1], "no_twin": True}
            return control.ChildResult(0, f"PROBE_RETRIEVAL {json.dumps(report)}\n")
        reserved = argv[argv.index("--reserved-probe-key") + 1]
        twin = {
            "mode": "GRAPH",
            "sent": True,
            "decision": "ADMITTED",
            "version_id": "v-1",
            "receipt": {
                "run_id": self.twins.get(label, f"run-{label}"),
                "manifest_key": reserved,
                "version_id": "v-1",
            },
        }
        return control.ChildResult(0, f"PROBE_TWIN {json.dumps(twin)}\n")


def _entries() -> list[control.Entry]:
    return [
        control.Entry("single_root", "s", "src-s"),
        control.Entry("wave_a", "a1", "src-a1"),
        control.Entry("wave_a", "a2", "src-a2"),
        control.Entry("wave_b", "b1", "src-b1"),
    ]


def test_dry_plan_runs_every_entry_twice_and_reserves_in_stable_order(
    tmp_path: pathlib.Path,
) -> None:
    children = _Children()
    planned = control.dry_plan(_inputs(tmp_path), _entries(), children)
    assert [item.entry.label for item in planned] == ["s", "a1", "a2", "b1"]
    assert all(count == 2 for count in children.plans.values())
    children.calls.clear()
    control.reserve(_inputs(tmp_path), planned, children)
    reserved = [call[call.index("--reserved-probe-key") + 1] for call in children.calls]
    assert reserved == sorted(reserved)
    assert all("--no-twin" in call and "--dry-run" not in call for call in children.calls)


@pytest.mark.parametrize(
    ("children", "message"),
    [
        (_Children(drift={"a1"}), "two dry plans differ"),
        (_Children(windows={"a2": _WINDOW.format(label="a1")}), "same window"),
        (
            _Children(fail={"b1:plan1": control.ChildResult(1, "probe retrieval refused")}),
            "exited 1",
        ),
        (
            _Children(
                fail={"b1:plan1": control.ChildResult(1, "", stderr="x\nProbeError: slot taken\n")}
            ),
            "exited 1: x | ProbeError: slot taken",
        ),
        (
            _Children(
                fail={
                    "b1:plan1": control.ChildResult(
                        1, '{"level":"INFO"}\n{"level":"ERROR","message":"operation failed"}\n'
                    )
                }
            ),
            'exited 1: {"level":"ERROR","message":"operation failed"}',
        ),
        (_Children(fail={"b1:plan1": control.ChildResult(None, "", timed_out=True)}), "timed out"),
        (_Children(fail={"s:plan2": control.ChildResult(0, "no report\n")}), "0 PROBE_RETRIEVAL"),
        (_Children(keys={"a1": "elsewhere/retrieved=1/m.json"}), "fresh probe slot"),
    ],
)
def test_a_drifted_duplicate_exhausted_or_malformed_dry_plan_stops_before_any_write(
    tmp_path: pathlib.Path, children: _Children, message: str
) -> None:
    with pytest.raises(control.ControlError, match=message):
        control.dry_plan(_inputs(tmp_path), _entries(), children)
    assert all("--dry-run" in call for call in children.calls)


def test_a_reservation_that_lands_another_key_stops_before_the_barrier(
    tmp_path: pathlib.Path,
) -> None:
    planned = control.dry_plan(_inputs(tmp_path), _entries(), _Children())

    def wrong(argv: tuple[str, ...], timeout: float) -> control.ChildResult:
        del argv, timeout
        return control.ChildResult(0, 'PROBE_RETRIEVAL {"key": "other", "no_twin": true}\n')

    with pytest.raises(control.ControlError, match="landed another key"):
        control.reserve(_inputs(tmp_path), planned, wrong)


def _twin_line(**changes: Any) -> str:
    twin: dict[str, Any] = {
        "mode": "GRAPH",
        "sent": True,
        "decision": "ADMITTED",
        "version_id": "v-1",
        "receipt": {"run_id": "run-1", "manifest_key": "k", "version_id": "v-1"},
    }
    receipt = {**twin["receipt"], **changes.pop("receipt", {})}
    twin.update(changes, receipt=receipt)
    return f"PROBE_TWIN {json.dumps(twin)}\n"


def test_exactly_one_valid_probe_twin_line_is_accepted() -> None:
    assert control.parse_twin(_twin_line(), "k")[0] == "run-1"
    for stdout, message in (
        ("", "0 PROBE_TWIN"),
        (_twin_line() + _twin_line(), "2 PROBE_TWIN"),
        ("PROBE_TWIN {not json\n", "malformed"),
        (_twin_line(decision="REFUSED"), "not admitted"),
        (_twin_line(mode="SHADOW"), "GRAPH mode"),
        (_twin_line(sent=False), "GRAPH mode"),
        (_twin_line(receipt={"run_id": ""}), "names no run"),
        (_twin_line(receipt={"manifest_key": "other"}), "another key"),
        (_twin_line(receipt={"version_id": "v-2"}), "another key"),
    ):
        with pytest.raises(control.ControlError, match=message):
            control.parse_twin(stdout, "k")


@dataclass
class _Reads:
    """Fake strong reads scripted for one control run."""

    runs: dict[str, dict[str, object]]
    heads: list[dict[str, object]]
    markers: dict[str, dict[str, object]]
    transitions: dict[str, dict[str, object]]
    reads: int = 0
    deliveries_pending: int = 0
    delivery_reads: list[str] = field(default_factory=list[str])

    def run(self, run_id: str) -> dict[str, object] | None:
        return self.runs.get(run_id)

    def head(self) -> dict[str, object] | None:
        self.reads += 1
        return self.heads[min(self.reads - 1, len(self.heads) - 1)]

    def marker(self, run_id: str) -> dict[str, object] | None:
        return self.markers.get(run_id)

    def transition(self, cohort_revision: str) -> dict[str, object] | None:
        return self.transitions.get(cohort_revision)

    def delivery_receipt(self, manifest_key: str) -> dict[str, object] | None:
        self.delivery_reads.append(manifest_key)
        if self.deliveries_pending > 0:
            self.deliveries_pending -= 1
            return None
        return {"request_id": f"delivery-{manifest_key}", "decision": "QUEUED_LATEST"}

    def held(self) -> bool:
        return True


_A_HEAD: dict[str, object] = {
    "state": "ACTIVE",
    "cohort_revision": "cohort-a",
    "elected_run_ids": ["run-a1", "run-a2"],
    "winner_run_id": "run-a1",
    "elected_candidate_digest": "d-1",
    "elected_frontier_digest": "f-1",
    "serving_input_revision": "in-1",
    "activated_at": "2026-10-15T10:04:00+00:00",
    "direct_binding_id": "bind-1",
    "version": 4,
}
_B_HEAD: dict[str, object] = {
    "state": "ACTIVE",
    "cohort_revision": "cohort-b",
    "elected_run_ids": ["run-b1"],
    "version": 6,
}


def _terminal_run(minutes: int = 20) -> dict[str, object]:
    return {
        "state": "SUCCEEDED",
        "root_clock_started_at": "2026-10-15T10:00:00+00:00",
        "terminal_at": f"2026-10-15T{10 + minutes // 60:02d}:{minutes % 60:02d}:00+00:00",
    }


def _world(**changes: Any) -> _Reads:
    runs = {f"run-{label}": _terminal_run() for label in ("s", "a1", "a2", "b1")}
    runs.update(changes.pop("runs", {}))
    return _Reads(
        runs=runs,
        heads=changes.pop("heads", [_A_HEAD, _A_HEAD, _B_HEAD]),
        markers=changes.pop(
            "markers",
            {
                "run-b1": {
                    "state": "SETTLED_ELIGIBLE",
                    "flipped_at": "2026-10-15T10:05:00+00:00",
                    "settled_at": "2026-10-15T10:05:01+00:00",
                }
            },
        ),
        transitions=changes.pop(
            "transitions",
            {
                "cohort-a": {
                    "drained_at": "2026-10-15T10:10:00+00:00",
                    "elected_cohort_revision": "cohort-b",
                }
            },
        ),
    )


def _run(tmp_path: pathlib.Path, reads: _Reads) -> dict[str, object]:
    inputs = _inputs(tmp_path)
    children = _Children()
    planned = control.dry_plan(inputs, _entries(), children)
    clock = control.Clock(sleep=lambda _seconds: None, monotonic=lambda: 0.0)
    collection = control.run_control(inputs, planned, children, reads, clock)
    by_roster = {
        roster: [collection.run_ids[p.entry.label] for p in planned if p.entry.roster == roster]
        for roster in control.MANDATORY_ROSTERS
    }
    return control.rolling_freshness_control_verdict(collection, by_roster)


def test_the_single_root_and_a_b_control_passes_with_b_elected_by_a_final_transaction(
    tmp_path: pathlib.Path,
) -> None:
    verdict = _run(tmp_path, _world())
    assert verdict["passed"] is True, verdict["reasons"]
    assert verdict["disposition"] == "PASS"


def test_no_twin_launches_before_every_reserved_delivery_is_recorded(
    tmp_path: pathlib.Path,
) -> None:
    inputs = _inputs(tmp_path)
    children = _Children()
    planned = control.dry_plan(inputs, _entries(), children)
    reads = _world()
    reads.deliveries_pending = 2  # the last reservation's delivery is still committing
    launched_before: list[int] = []
    original = children.__call__

    def runner(argv: tuple[str, ...], timeout: float) -> control.ChildResult:
        if "--twin-only" in argv:
            launched_before.append(reads.deliveries_pending)
        return original(argv, timeout)

    clock = control.Clock(sleep=lambda _seconds: None, monotonic=lambda: 0.0)
    collection = control.run_control(inputs, planned, runner, reads, clock)

    assert launched_before and set(launched_before) == {0}
    assert set(reads.delivery_reads) == {item.key for item in planned}
    recorded = collection.observations["deliveries_recorded"]
    assert set(cast("dict[str, object]", recorded)) == {item.entry.label for item in planned}


@dataclass
class _UnheldReads(_Reads):
    """Without the hold, each landed key's own delivery is admitted to the scripted run."""

    admitted: dict[str, str] = field(default_factory=dict[str, str])
    decision: str = "ADMITTED"

    def delivery_receipt(self, manifest_key: str) -> dict[str, object] | None:
        self.delivery_reads.append(manifest_key)
        run_id = self.admitted.get(manifest_key)
        return None if run_id is None else {"decision": self.decision, "run_id": run_id}


def _unheld(tmp_path: pathlib.Path, decision: str = "ADMITTED") -> tuple[Any, ...]:
    inputs = _inputs(tmp_path)
    children = _Children()
    planned = control.dry_plan(inputs, _entries(), children)
    world = _world()
    reads = _UnheldReads(
        world.runs,
        world.heads,
        world.markers,
        world.transitions,
        admitted={item.key: f"run-{item.entry.label}" for item in planned},
        decision=decision,
    )
    return inputs, children, planned, reads


def test_without_the_hold_each_key_lands_at_its_launch_and_its_delivery_names_the_run(
    tmp_path: pathlib.Path,
) -> None:
    inputs, children, planned, reads = _unheld(tmp_path)
    hooked: list[tuple[str, list[str]]] = []
    clock = control.Clock(sleep=lambda _seconds: None, monotonic=lambda: 0.0)

    collection = control.run_control(
        inputs,
        planned,
        children,
        reads,
        clock,
        held=False,
        on_launch=lambda roster, run_ids: hooked.append((roster, list(run_ids))),
    )

    by_roster = {
        roster: [collection.run_ids[p.entry.label] for p in planned if p.entry.roster == roster]
        for roster in control.MANDATORY_ROSTERS
    }
    verdict = control.rolling_freshness_control_verdict(collection, by_roster)
    assert verdict["passed"] is True, verdict["reasons"]
    launches = [argv for argv in children.calls if "--reserved-probe-key" in argv]
    assert launches and all("--no-twin" in argv for argv in launches)
    assert not [argv for argv in children.calls if "--twin-only" in argv]
    labels = [argv[argv.index("--probe-retrieval") + 1] for argv in launches]
    assert labels[0] == "src-s" and labels[-1] == "src-b1"
    assert set(labels[1:3]) == {"src-a1", "src-a2"}
    assert all(argv[argv.index("--operation-id") + 1].endswith("-launch") for argv in launches)
    assert hooked == [
        ("single_root", ["run-s"]),
        ("wave_a", ["run-a1", "run-a2"]),
        ("wave_b", ["run-b1"]),
    ]
    assert collection.observations["admission"] == "unheld_delivery"
    assert "deliveries_recorded" not in collection.observations


def test_without_the_hold_a_delivery_that_joined_another_run_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    # Paired control: the same unheld launch, but the delivery joined an existing run.
    inputs, children, planned, reads = _unheld(tmp_path, decision="JOINED")
    clock = control.Clock(sleep=lambda _seconds: None, monotonic=lambda: 0.0)

    collection = control.Collection()
    with pytest.raises(control.ControlError, match="s: the delivery was not admitted"):
        control.run_control(inputs, planned, children, reads, clock, collection, held=False)
    assert len([argv for argv in children.calls if "--reserved-probe-key" in argv]) == 1
    # The refused receipt stays on the launch, so the NOT_PROVEN receipt names its decision.
    refused = cast("dict[str, object]", collection.launches["s"]["delivery_receipt"])
    assert refused["decision"] == "JOINED"


def test_without_the_hold_an_admitted_receipt_naming_no_run_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    inputs, children, planned, reads = _unheld(tmp_path)
    reads.admitted = dict.fromkeys(reads.admitted, "")
    clock = control.Clock(sleep=lambda _seconds: None, monotonic=lambda: 0.0)

    with pytest.raises(control.ControlError, match="s: the delivery was not admitted"):
        control.run_control(inputs, planned, children, reads, clock, held=False)


def test_a_transient_endpoint_failure_is_a_missed_poll_not_a_failure() -> None:
    calls: list[int] = []

    def read() -> str | None:
        calls.append(1)
        if len(calls) == 1:
            raise EndpointConnectionError(endpoint_url="https://dynamodb")
        return "found"

    assert control._poll(read, timeout=60, sleep=lambda _s: None, clock=lambda: 0.0) == "found"  # pyright: ignore[reportPrivateUsage]
    # Paired control: a failure that never clears still ends at the deadline.
    ticks = iter(range(0, 10**6, 30))

    def always() -> str | None:
        raise EndpointConnectionError(endpoint_url="https://dynamodb")

    with pytest.raises(control.ControlError, match="strong poll timed out"):
        control._poll(always, timeout=60, sleep=lambda _s: None, clock=lambda: float(next(ticks)))  # pyright: ignore[reportPrivateUsage]


def test_a_delivery_never_recorded_stops_before_any_launch(tmp_path: pathlib.Path) -> None:
    inputs = _inputs(tmp_path)
    children = _Children()
    planned = control.dry_plan(inputs, _entries(), children)
    reads = _world()
    reads.deliveries_pending = 10**6
    ticks = iter(range(0, 10**6, 600))
    clock = control.Clock(sleep=lambda _seconds: None, monotonic=lambda: float(next(ticks)))

    with pytest.raises(control.ControlError, match="strong poll timed out"):
        control.run_control(inputs, planned, children, reads, clock)
    assert not any("--twin-only" in call for call in children.calls)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        *(
            ({"heads": [_A_HEAD, {**_A_HEAD, name: value}, _B_HEAD]}, reason)
            for name, value, reason in (
                ("elected_run_ids", ["run-a1"], "a_snapshot_changed_while_b_published"),
                ("winner_run_id", "run-a2", "a_snapshot_changed_while_b_published"),
                ("elected_candidate_digest", "d-2", "a_snapshot_changed_while_b_published"),
                ("elected_frontier_digest", "f-2", "a_snapshot_changed_while_b_published"),
                ("cohort_revision", "cohort-x", "a_snapshot_changed_while_b_published"),
                ("state", "IDLE", "a_not_active_when_b_settled"),
            )
        ),
        (
            {
                "transitions": {
                    "cohort-a": {
                        "drained_at": "2026-10-15T10:05:00.500000+00:00",
                        "elected_cohort_revision": "cohort-b",
                    }
                }
            },
            "b_settled_at_not_before_a_final_alias",
        ),
        (
            {
                "transitions": {
                    "cohort-a": {
                        "drained_at": "2026-10-15T10:05:00+00:00",
                        "elected_cohort_revision": "cohort-b",
                    }
                }
            },
            "b_flipped_at_not_before_a_final_alias",
        ),
        (
            {
                "transitions": {
                    "cohort-a": {
                        "drained_at": "2026-10-15T10:10:00+00:00",
                        "elected_cohort_revision": "cohort-c",
                    }
                }
            },
            "b_not_elected_by_a_final_transaction",
        ),
        ({"runs": {"run-s": {**_terminal_run(), "state": "FAILED"}}}, "single_root_not_succeeded"),
        ({"runs": {"run-a2": _terminal_run(121)}}, "root_over_7200_seconds"),
    ],
)
def test_each_rolling_control_violation_is_not_proven(
    tmp_path: pathlib.Path, changes: dict[str, Any], reason: str
) -> None:
    verdict = _run(tmp_path, _world(**changes))
    assert verdict["passed"] is False
    assert reason in cast("list[str]", verdict["reasons"])


def test_a_lifecycle_only_head_change_while_b_settles_passes(tmp_path: pathlib.Path) -> None:
    # x8: A's direct finalize moved the head ACTIVE -> BOUND_DRAINING, bumped its version and
    # rebound it 0.83 s after B settled, before the control's head read; A itself was unchanged.
    draining = {**_A_HEAD, "state": "BOUND_DRAINING", "version": 5, "direct_binding_id": "bind-2"}
    verdict = _run(tmp_path, _world(heads=[_A_HEAD, draining, _B_HEAD]))
    assert verdict["passed"] is True, verdict["reasons"]


def test_the_7200_second_root_boundary_is_inclusive(tmp_path: pathlib.Path) -> None:
    assert _run(tmp_path, _world(runs={"run-a2": _terminal_run(120)}))["passed"] is True


def test_b_joining_a_is_not_proven(tmp_path: pathlib.Path) -> None:
    joined = {**_A_HEAD, "elected_run_ids": ["run-a1", "run-a2", "run-b1"]}
    inputs = _inputs(tmp_path)
    children = _Children()
    planned = control.dry_plan(inputs, _entries(), children)
    # A is never ACTIVE with exactly its own runs, so the control stops at once: NOT_PROVEN.
    ticks = iter(range(0, 100_000, 10_000))
    clock = control.Clock(sleep=lambda _seconds: None, monotonic=lambda: float(next(ticks)))
    with pytest.raises(control.ControlError, match="joined other runs"):
        control.run_control(inputs, planned, children, _world(heads=[joined]), clock)


def test_a_wave_split_across_cohorts_fails_fast_not_at_the_timeout(
    tmp_path: pathlib.Path,
) -> None:
    # x4: a-1 was elected alone; an immutable snapshot never gains a-2, so waiting is futile.
    split = {**_A_HEAD, "elected_run_ids": ["run-a1"]}
    inputs = _inputs(tmp_path)
    children = _Children()
    planned = control.dry_plan(inputs, _entries(), children)
    clock = control.Clock(sleep=lambda _seconds: None, monotonic=lambda: 0.0)
    with pytest.raises(control.ControlError, match="split across cohorts"):
        control.run_control(inputs, planned, children, _world(heads=[split]), clock)


@dataclass
class _SingleRootLifecycle(_Reads):
    """The single root is OPEN for `open_reads` reads, GRANTED until wave A launches, then done."""

    open_reads: int = 2
    wave_a_launched: bool = False
    single_reads: int = 0
    last_single_state: object = None

    def run(self, run_id: str) -> dict[str, object] | None:
        if run_id != "run-s":
            return self.runs.get(run_id)
        self.single_reads += 1
        if self.single_reads <= self.open_reads:
            found: dict[str, object] = {"state": "OPEN"}
        else:
            found = _terminal_run() if self.wave_a_launched else {"state": "GRANTED"}
        self.last_single_state = found["state"]
        return found


def _sequenced(
    tmp_path: pathlib.Path, reads: _SingleRootLifecycle
) -> tuple[control.Collection, list[object]]:
    inputs = _inputs(tmp_path)
    children = _Children()
    planned = control.dry_plan(inputs, _entries(), children)
    single_state_at_a_launch: list[object] = []

    def runner(argv: tuple[str, ...], timeout: float) -> control.ChildResult:
        operation = argv[argv.index("--operation-id") + 1]
        if "--twin-only" in argv and "-wave-a-" in operation:
            # The last single-root state the control read before this launch.
            single_state_at_a_launch.append(reads.last_single_state)
            reads.wave_a_launched = True
        return children(argv, timeout)

    clock = control.Clock(sleep=lambda _seconds: None, monotonic=lambda: 0.0)
    return control.run_control(inputs, planned, runner, reads, clock), single_state_at_a_launch


def _lifecycle(**changes: Any) -> _SingleRootLifecycle:
    world = _world(**changes)
    return _SingleRootLifecycle(world.runs, world.heads, world.markers, world.transitions)


def test_wave_a_launches_once_the_single_root_holds_its_grant_not_after_its_terminal(
    tmp_path: pathlib.Path,
) -> None:
    # x4 and x7: A launched after the single root's terminal reached an IDLE head one release
    # pass apart and split; launched behind the single root's busy cohort, both wait for it.
    reads = _lifecycle()
    collection, at_launch = _sequenced(tmp_path, reads)
    # Both twins launched after a GRANTED read (never after an OPEN one) and before terminal.
    assert at_launch == ["GRANTED", "GRANTED"]
    granted = cast("dict[str, Any]", collection.observations["single_root_granted"])
    assert granted["states"] == {"run-s": "GRANTED"}
    # Criterion 3 still waits for the single root's own terminal state.
    terminal = cast("dict[str, Any]", collection.observations["single_root_terminal"])
    assert terminal["run-s"]["state"] == "SUCCEEDED"


_SETTLED_A: dict[str, dict[str, object]] = {
    "run-s": {"state": "FINALIZED", "finalized_at": "2026-10-15T10:07:00+00:00"},
    "run-a1": {"state": "SETTLED_ELIGIBLE", "settled_at": "2026-10-15T10:04:30+00:00"},
}


def test_each_wave_a_twin_records_its_settle_margin_before_the_single_finalize(
    tmp_path: pathlib.Path,
) -> None:
    markers = {
        **_SETTLED_A,
        "run-a2": {"state": "SETTLED_ELIGIBLE", "settled_at": "2026-10-15T10:05:00+00:00"},
        "run-b1": _world().markers["run-b1"],
    }
    collection, _ = _sequenced(tmp_path, _lifecycle(markers=markers))
    margin = cast("dict[str, Any]", collection.observations["wave_a_margin"])
    assert margin["single_finalized_at"] == "2026-10-15T10:07:00+00:00"
    assert {k: v["margin_seconds"] for k, v in margin["twins"].items()} == {
        "run-a1": 150.0,
        "run-a2": 120.0,
    }


def test_a_split_wave_keeps_its_margin_in_the_refused_receipt(tmp_path: pathlib.Path) -> None:
    split = {**_A_HEAD, "elected_run_ids": ["run-a1"]}
    collection = control.Collection()
    inputs = _inputs(tmp_path)
    children = _Children()
    planned = control.dry_plan(inputs, _entries(), children)
    clock = control.Clock(sleep=lambda _seconds: None, monotonic=lambda: 0.0)
    reads = _world(heads=[split], markers=dict(_SETTLED_A))
    with pytest.raises(control.ControlError, match="split across cohorts"):
        control.run_control(inputs, planned, children, reads, clock, collection)
    margin = cast("dict[str, Any]", collection.observations["wave_a_margin"])
    # a-2 had not settled when a-1 was elected alone: the near miss is in the evidence.
    assert margin["twins"] == {
        "run-a1": {"settled_at": "2026-10-15T10:04:30+00:00", "margin_seconds": 150.0},
        "run-a2": {"settled_at": None, "margin_seconds": None},
    }


def test_a_wave_launches_its_twins_concurrently(tmp_path: pathlib.Path) -> None:
    inputs = _inputs(tmp_path)
    children = _Children()
    planned = control.dry_plan(inputs, _entries(), children)
    wave = [item for item in planned if item.entry.roster == "wave_a"]
    barrier = threading.Barrier(len(wave), timeout=5)

    def runner(argv: tuple[str, ...], timeout: float) -> control.ChildResult:
        barrier.wait()  # deadlocks (BrokenBarrierError) unless every twin is in flight at once
        return children(argv, timeout)

    run_ids = control.launch(inputs, wave, runner, control.Collection())
    assert len(set(run_ids)) == len(wave)


def test_a_duplicate_run_id_stops_further_launches(tmp_path: pathlib.Path) -> None:
    inputs = _inputs(tmp_path)
    children = _Children(twins={"a1": "run-same", "a2": "run-same"})
    planned = control.dry_plan(inputs, _entries(), children)
    clock = control.Clock(sleep=lambda _seconds: None, monotonic=lambda: 0.0)
    reads = _world()
    reads.runs["run-same"] = _terminal_run()
    with pytest.raises(control.ControlError, match="same run id"):
        control.run_control(inputs, planned, children, reads, clock)
    launched = [c for c in children.calls if "--twin-only" in c]
    assert len(launched) == 3  # single root, a1, then the refused a2; b1 is never launched


def test_the_receipt_is_written_atomically_and_refused_inputs_write_none(
    tmp_path: pathlib.Path,
) -> None:
    path = control.write_receipt(tmp_path / "out", {"disposition": "NOT_PROVEN"})
    assert json.loads(path.read_text()) == {"disposition": "NOT_PROVEN"}
    assert not list((tmp_path / "out").glob(".*.tmp"))
    assert control.main(_argv(tmp_path, **{"--environment": "prod"})) == 2
    assert not (tmp_path / "evidence").exists()
