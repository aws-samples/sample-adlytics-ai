# SPDX-License-Identifier: MIT-0
"""S5: the stuck-flow sweep and the `PROMOTE#` age (FR-7 kept rule, V-20, V-26, V-9; fictional)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words
from execution_graph_dispatch_support import NOW as BAKE_NOW
from execution_graph_dispatch_support import ROOT_CLOCK as GRANTED_CLOCK
from execution_graph_dispatch_support import (
    SHARD,
    TABLE,
    TEMPLATE,
    UNIT,
    instantiate,
    now_text,
    planner,
    seed_run,
    set_mode,
    store,
    write_bool,
    write_int,
    write_text,
)
from execution_graph_release_support import granted_run, release_store, settle_publish
from execution_graph_support import FakeDynamo
from graph_bake_v3_support import Bake, roster
from graph_consumer_support import precompute_contributed

from adapters.jobs.execution_graph import stuck_flow as detector_module
from adapters.jobs.execution_graph.instantiate import InstantiationOutcome
from adapters.jobs.execution_graph.promoter import PromoterContext, promote_shard
from adapters.jobs.execution_graph.stuck_flow import DetectorContext, detect, summary
from adapters.jobs.preset_artifact_publisher.graph_bake import task_node_name
from adapters.outbound.dynamodb.due_shards import DynamoDueShardReader
from adapters.outbound.dynamodb.execution_graph import build_admission_transaction, receipt_item
from adapters.outbound.telemetry.graph_metrics import (
    AuthorityScope,
    put_probe_stuck_flow_reading,
    stuck_flow_datapoints,
)
from domain.operations.bake_scheduling import PRECOMPUTE_READY_SECONDS
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_dispatch import FenceReason, NodeState
from domain.operations.generated_execution_graphs import BAKE_TASKS
from domain.operations.run_request import Waiter
from domain.operations.stuck_flow import (
    FINALIZER_BLOCKED_AFTER,
    FLOW_OBLIGATION_KINDS,
    PRECOMPUTE_BLOCKED_AFTER,
    PROTOCOL_CEILING,
    ContributionObservation,
    DueObservation,
    HoldState,
    SweepIncompleteError,
    SweepReading,
    bake_contribution_candidates,
    promotion_age_seconds,
    sweep,
    unarmed_contribution_candidates,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.execution_graph import (
    AdmissionCommit,
    EffectKind,
    NewRun,
    ReceiptRecord,
    ReplacedRun,
)
from ports.outbound.execution_graph_dispatch import FenceRunRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
SHARDS = tuple(f"DUE#{index}" for index in range(16))
CLEAR = HoldState(set=False)


def due(shard: str, sk: str, root_age: timedelta | None = None, **extra: object) -> DueObservation:
    root = None if root_age is None else NOW - root_age
    return DueObservation(shard, sk, root, **extra)  # pyright: ignore[reportArgumentType]


def full(**overrides: list[DueObservation] | None) -> dict[str, list[DueObservation] | None]:
    shards: dict[str, list[DueObservation] | None] = {shard: [] for shard in SHARDS}
    for name, items in overrides.items():
        shards[name.replace("_", "#")] = items
    return shards


def test_the_v1_ceiling_comes_from_the_generated_contract() -> None:
    assert timedelta(seconds=7200) == PROTOCOL_CEILING


def test_an_empty_complete_sweep_reads_zero() -> None:
    reading = sweep(full(), SHARDS, CLEAR, NOW)
    assert (reading.past_ceiling_count, reading.without_root_clock_count) == (0, 0)
    assert reading.shards == 16
    assert reading.open_flows == 0
    assert reading.promotion_age_seconds == 0.0


@pytest.mark.parametrize("kind", sorted(FLOW_OBLIGATION_KINDS))
def test_every_flow_obligation_kind_counts_once_its_root_clock_passes_the_ceiling(
    kind: str,
) -> None:
    """V-26 (`INSTANTIATE#`), V-20 (`WAITER#`), V-9 (`VERIFY#`) and the run's other work items."""
    # A recovery item always carries its closed reason (plan 6.1); the writer stamps one.
    extra: dict[str, Any] = {"recovery_reason": "fatal"} if kind == "BAKE_RECOVERY" else {}
    # ADR-140: a COMPLETE# always names its grant; the sweep reads its bake member.
    extra |= {"grant_id": "grant-x"} if kind == "COMPLETE" else {}
    at = due("DUE#3", f"{kind}#x", PROTOCOL_CEILING, **extra)
    before = due("DUE#4", f"{kind}#y", PROTOCOL_CEILING - timedelta(seconds=1), **extra)
    unarmed = {"grant-x": (None, None)} if kind == "COMPLETE" else None
    reading = sweep(full(DUE_3=[at], DUE_4=[before]), SHARDS, CLEAR, NOW, None, unarmed)
    assert reading.past_ceiling == (at,)
    assert reading.open_flows == 2
    assert reading.per_shard()["DUE#3"]["past_ceiling"] == [f"{kind}#x"]


def test_the_due_time_is_the_root_clock_plus_the_ceiling_never_the_item_write_time() -> None:
    # An obligation written just now for a root received three hours ago is already past due.
    old_root = due("DUE#0", "OUTBOX#n1", timedelta(hours=3))
    reading = sweep(full(DUE_0=[old_root]), SHARDS, CLEAR, NOW)
    assert reading.past_ceiling_count == 1


def test_a_flow_obligation_without_a_root_clock_is_counted_apart_never_guessed() -> None:
    unclocked = due("DUE#5", "FENCE#r1")
    reading = sweep(full(DUE_5=[unclocked]), SHARDS, CLEAR, NOW)
    assert reading.past_ceiling_count == 0
    assert reading.without_root_clock == (unclocked,)
    assert reading.per_shard() == {"DUE#5": {"without_root_clock": ["FENCE#r1"]}}


def test_promote_items_are_never_counted_as_flows() -> None:
    wake = due("DUE#1", "PROMOTE#u1", timedelta(hours=9), woken_at=NOW - timedelta(minutes=2))
    reading = sweep(full(DUE_1=[wake]), SHARDS, CLEAR, NOW)
    assert reading.open_flows == 0
    assert reading.past_ceiling_count == 0
    assert reading.promotion_age_seconds == 120.0


@pytest.mark.parametrize("failed", ["DUE#0", "DUE#15"])
def test_no_value_at_all_unless_every_shard_was_read(failed: str) -> None:
    """No zero before the sweep is complete (FR-7 kept rule)."""
    shards = full()
    shards[failed] = None
    with pytest.raises(SweepIncompleteError):
        sweep(shards, SHARDS, CLEAR, NOW)
    del shards[failed]
    with pytest.raises(SweepIncompleteError):
        sweep(shards, SHARDS, CLEAR, NOW)


def test_an_undeclared_item_kind_or_an_extra_shard_yields_no_value() -> None:
    with pytest.raises(SweepIncompleteError):
        sweep(full(DUE_2=[due("DUE#2", "MYSTERY#1", timedelta(0))]), SHARDS, CLEAR, NOW)
    shards = full()
    shards["DUE#16"] = []
    with pytest.raises(SweepIncompleteError):
        sweep(shards, SHARDS, CLEAR, NOW)


def test_v20_age_skips_held_items_while_the_hold_is_set() -> None:
    held = due("DUE#0", "PROMOTE#u1", woken_at=NOW - timedelta(hours=5), held=True)
    fresh = due("DUE#0", "PROMOTE#u2", woken_at=NOW - timedelta(seconds=30))
    age, measured, skipped = promotion_age_seconds([held, fresh], HoldState(set=True), NOW)
    assert (age, measured, skipped) == (30.0, 1, 1)


def test_v20_age_after_the_hold_clears_runs_from_the_later_of_wakeup_and_clear() -> None:
    held = due("DUE#0", "PROMOTE#u1", woken_at=NOW - timedelta(hours=5), held=True)
    cleared = HoldState(set=False, cleared_at=NOW - timedelta(seconds=40))
    assert promotion_age_seconds([held], cleared, NOW) == (40.0, 1, 0)
    # An item woken after the clear is measured from its own wakeup.
    later = due("DUE#0", "PROMOTE#u3", woken_at=NOW - timedelta(seconds=10))
    assert promotion_age_seconds([later], cleared, NOW) == (10.0, 1, 0)
    # With no clear recorded, the wakeup alone sets the age.
    assert promotion_age_seconds([held], CLEAR, NOW) == (5 * 3600.0, 1, 0)


def test_a_promote_item_without_a_wakeup_yields_no_value() -> None:
    with pytest.raises(SweepIncompleteError):
        promotion_age_seconds([due("DUE#0", "PROMOTE#u1")], CLEAR, NOW)


# Every obligation writer stamps the root clock it is due from (FR-7 kept rule).

ROOT_CLOCK = "2026-10-07T07:30:00+00:00"


def _receipt(decision: str, row: int, **ids: str | None) -> ReceiptRecord:
    return ReceiptRecord(
        request_id="request-root-clock",
        request_digest="d" * 64,
        decision=decision,
        reason=None,
        row=row,
        authority_epoch=1,
        mode=AuthorityMode.GRAPH,
        conflict_unit=UNIT,
        input_revision=NOW.isoformat(),
        fingerprint="7" * 64,
        recorded_at=now_text(),
        protocol_version="v1",
        contract_revision=1,
        root_flow_id="root-fictional",
        root_clock_started_at=ROOT_CLOCK,
        **ids,  # pyright: ignore[reportArgumentType]
    )


def _new_run() -> NewRun:
    return NewRun(
        run_id="run-new",
        graph_id="graph-new",
        fingerprint="7" * 64,
        retrieved_at=NOW.isoformat(),
        root_flow_id="root-fictional",
        generation=0,
        template=TEMPLATE,
        root_clock_started_at=ROOT_CLOCK,
    )


def _stamped(members: list[Any]) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for member in members:
        ((_kind, body),) = cast("dict[str, dict[str, Any]]", member).items()
        item = body.get("Item")
        if item is None:
            continue
        clock = item.get("root_clock_started_at")
        found[str(item["sk"]["S"]).split("#", 1)[0] or str(item["pk"]["S"])] = (
            None if clock is None else str(clock["S"])
        )
    return found


def test_admission_stamps_the_run_its_instantiation_and_its_receipt() -> None:
    commit = AdmissionCommit(
        _receipt("ADMITTED", 20, run_id="run-new", graph_id="graph-new"),
        EffectKind.ADMIT_RUN,
        UNIT,
        0,
        new_run=_new_run(),
    )
    stamped = _stamped(build_admission_transaction(TABLE, commit))
    assert stamped["RUN"] == stamped["INSTANTIATE"] == stamped["RECEIPT"] == ROOT_CLOCK


def test_a_replacement_stamps_the_replaced_runs_fence_with_that_runs_own_clock() -> None:
    commit = AdmissionCommit(
        _receipt("REPLACED_BEFORE_COMPUTE", 17, run_id="run-new", graph_id="graph-new"),
        EffectKind.REPLACE_RUN,
        UNIT,
        1,
        new_run=_new_run(),
        replaced=ReplacedRun("run-old", invalidating=False, root_clock_started_at="old-clock"),
    )
    stamped = _stamped(build_admission_transaction(TABLE, commit))
    assert stamped["FENCE"] == "old-clock"
    assert stamped["INSTANTIATE"] == ROOT_CLOCK


def test_a_queued_waiter_carries_its_root_clock() -> None:
    waiter = Waiter("request-root-clock", "7" * 64, NOW, False, 1, None, ROOT_CLOCK)
    commit = AdmissionCommit(
        _receipt("QUEUED_LATEST", 19), EffectKind.QUEUE_WAITER, UNIT, 1, new_waiter=waiter
    )
    assert _stamped(build_admission_transaction(TABLE, commit))["WAITER"] == ROOT_CLOCK


def _clocked_run(fake: FakeDynamo) -> str:
    set_mode(fake, AuthorityMode.GRAPH)
    seeded = seed_run(fake)
    name = seeded.record.run_id
    fake.items[(f"RUN#{name}", "RUN")]["root_clock_started_at"] = write_text(ROOT_CLOCK)
    return name


def _clock_of(fake: FakeDynamo, prefix: str) -> list[str | None]:
    found: list[str | None] = []
    for (_pk, sk), item in fake.items.items():
        if sk.startswith(prefix):
            clock = item.get("root_clock_started_at")
            found.append(None if clock is None else str(clock["S"]))
    return found


def test_instantiation_and_a_fence_stamp_their_outbox_and_fence_items() -> None:
    fake = FakeDynamo()
    run = _clocked_run(fake)
    assert instantiate(fake, run, "write_a", "write_b") is InstantiationOutcome.ARMED
    assert _clock_of(fake, "OUTBOX#") == [ROOT_CLOCK, ROOT_CLOCK]
    current = store(fake).read_run(run)
    assert current is not None
    assert current.root_clock_started_at == ROOT_CLOCK
    store(fake).fence_run(FenceRunRequest(current, FenceReason.CANCELLED, now_text()))
    assert _clock_of(fake, "FENCE#") == [ROOT_CLOCK]


def test_a_promoted_waiter_keeps_its_root_clock_on_the_run_and_its_instantiation() -> None:
    fake = FakeDynamo()
    run = _clocked_run(fake)
    receipt = _receipt("QUEUED_LATEST", 19)
    fake.items[("RECEIPT#wait-clock", "RECEIPT")] = cast(
        "Any", receipt_item(replace(receipt, request_id="wait-clock"))
    )
    fake.items[(SHARD, f"WAITER#{UNIT}")] = {
        "pk": write_text(SHARD),
        "sk": write_text(f"WAITER#{UNIT}"),
        "item_kind": write_text("waiter"),
        "authority_epoch": write_int(1),
        "request_id": write_text("wait-clock"),
        "fingerprint": write_text("8" * 64),
        "retrieved_at": write_text(NOW.isoformat()),
        "held": write_bool(False),
        "revision": write_int(1),
        "due_at": write_text(now_text()),
        "root_clock_started_at": write_text("waiter-clock"),
    }
    current = store(fake).read_run(run)
    assert current is not None
    store(fake).fence_run(FenceRunRequest(current, FenceReason.CANCELLED, now_text()))
    counts = promote_shard(
        PromoterContext(release_store(fake), planner("write_a"), lambda: NOW), SHARD
    )
    assert counts.branches == {"promote_waiter": 1}
    promoted = [
        item
        for (pk, _sk), item in fake.items.items()
        if pk.startswith("RUN#") and pk != f"RUN#{run}" and _sk == "RUN"
    ]
    assert [str(item["root_clock_started_at"]["S"]) for item in promoted] == ["waiter-clock"]
    assert "waiter-clock" in _clock_of(fake, "OUTBOX#")


def test_s6_arm_and_complete_are_flow_obligations_stamped_with_the_runs_root_clock() -> None:
    assert {"ARM", "COMPLETE"} <= FLOW_OBLIGATION_KINDS
    fake = FakeDynamo()
    run = granted_run(fake)  # apply puts COMPLETE#; the run's clock is two hours old
    assert _clock_of(fake, "ARM#") == []  # control: no arming obligation before `publish` settles
    settle_publish(fake, run)  # the `publish` settlement puts ARM# (scope serving)
    assert _clock_of(fake, "COMPLETE#") == _clock_of(fake, "ARM#") == [GRANTED_CLOCK]

    emitted, failures, reading = _detect(fake)

    assert failures == [] and len(emitted) == 1
    # AGG3-7: a granted run that never completes, and its waiting arming, age past the ceiling.
    assert {item.kind for item in reading.past_ceiling} == {"ARM", "COMPLETE"}


# The detector adapter: one strongly consistent sweep over the real reader, then its gauges.


def _obligation(  # noqa: PLR0913, PLR0917 -- one fixture writer
    fake: FakeDynamo, shard: str, sk: str, root: datetime | None
) -> None:
    item: dict[str, Any] = {"pk": write_text(shard), "sk": write_text(sk)}
    if root is not None:
        item["root_clock_started_at"] = write_text(root.isoformat())
    fake.items[(shard, sk)] = item


def _detect(
    fake: FakeDynamo, scope: AuthorityScope = AuthorityScope.LIVE
) -> tuple[list[tuple[SweepReading, AuthorityScope]], list[BaseException], Any]:
    emitted: list[tuple[SweepReading, AuthorityScope]] = []
    failures: list[BaseException] = []
    context = DetectorContext(
        DynamoDueShardReader(TABLE, cast("Any", fake)),
        lambda reading, at: emitted.append((reading, at)),
        lambda: NOW,
        failures.append,
        scope,
    )
    try:
        result: Any = detect(context)
    except SweepIncompleteError as error:
        result = error
    return emitted, failures, result


def test_the_detector_reads_every_shard_strongly_and_emits_once_per_sweep() -> None:
    fake = FakeDynamo()
    _obligation(fake, "DUE#2", "INSTANTIATE#run-a", NOW - timedelta(hours=3))
    _obligation(fake, "DUE#9", "WAITER#unit-b", NOW - timedelta(minutes=5))
    _obligation(fake, "DUE#9", "VERIFY#node-c", None)
    emitted, failures, reading = _detect(fake)
    assert failures == []
    assert len(emitted) == 1
    assert emitted[0][1] is AuthorityScope.LIVE
    assert reading.past_ceiling_count == 1
    assert reading.without_root_clock_count == 1
    assert reading.open_flows == 3
    assert summary(reading)["per_shard"] == {
        "DUE#2": {"past_ceiling": ["INSTANTIATE#run-a"]},
        "DUE#9": {"without_root_clock": ["VERIFY#node-c"]},
    }


def test_a_failed_shard_is_reported_once_and_no_gauge_is_emitted() -> None:
    fake = FakeDynamo(fail_query="'DUE#7'")
    emitted, failures, result = _detect(fake)
    assert emitted == []
    assert len(failures) == 1
    assert isinstance(result, SweepIncompleteError)


def test_the_hold_comes_from_the_mode_item_and_skips_held_promotions() -> None:
    fake = FakeDynamo()
    set_mode(fake, AuthorityMode.GRAPH, hold=True)
    fake.items[("DUE#0", "PROMOTE#u")] = {
        "pk": write_text("DUE#0"),
        "sk": write_text("PROMOTE#u"),
        "held": write_bool(True),
        "woken_at": write_text((NOW - timedelta(hours=1)).isoformat()),
    }
    _emitted, _failures, reading = _detect(fake)
    assert (reading.promotions_measured, reading.promotions_skipped_held) == (0, 1)


def test_live_and_probe_gauges_carry_only_closed_dimensions() -> None:
    fake = FakeDynamo()
    _obligation(fake, "DUE#1", "OUTBOX#n", NOW - timedelta(hours=4))
    _emitted, _failures, reading = _detect(fake, AuthorityScope.PROBE)
    points = stuck_flow_datapoints(reading, AuthorityScope.PROBE)
    assert [(p.name, p.value) for p in points] == [
        ("OpenFlowsPastProtocolCeiling", 1.0),
        ("OpenFlowsWithoutRootClock", 0.0),
        ("PromoteObligationAgeSeconds", 0.0),
        ("GraphBakeTaskFailed", 0.0),
        ("GraphBakeRetryExhausted", 0.0),
        ("GraphBakePrecomputeBlocked", 0.0),
        ("GraphBakeFinalizerBlocked", 0.0),
    ]
    assert {p.dimensions for p in points} == {(("Protocol", "v1"), ("AuthorityScope", "probe"))}


def test_the_handler_reports_an_incomplete_sweep_and_returns_no_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDynamo(fail_query="'DUE#3'")
    reports: list[BaseException] = []
    emitted: list[object] = []
    monkeypatch.setattr(
        detector_module, "_live_reader", lambda: DynamoDueShardReader(TABLE, cast("Any", fake))
    )

    def record(reading: SweepReading, scope: AuthorityScope) -> None:
        emitted.append((reading, scope))

    monkeypatch.setattr(detector_module, "emit_stuck_flow_reading", record)
    monkeypatch.setattr(detector_module, "_report", reports.append)
    assert detector_module.handler({}, None) == {"complete": False}
    assert emitted == []
    assert len(reports) == 1
    fake.fail_query = None
    result = detector_module.handler({}, None)
    assert result["complete"] is True
    assert result["past_ceiling"] == 0
    assert len(emitted) == 1


class _SpyCloudWatch:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_metric_data(self, **request: Any) -> None:
        self.puts.append(request)


def test_the_probe_emitter_renders_probe_scope_only() -> None:
    spy = _SpyCloudWatch()
    reading = SweepReading(16, 1, (), (), 0.0, 0, 0)
    put_probe_stuck_flow_reading(cast("Any", spy), reading)
    (request,) = spy.puts
    assert request["Namespace"] == "Adlytics"
    scopes = {
        dimension["Value"]
        for datum in request["MetricData"]
        for dimension in datum["Dimensions"]
        if dimension["Name"] == "AuthorityScope"
    }
    assert scopes == {"probe"}
    assert len(request["MetricData"]) == 7


def test_f9_a_clocked_bake_recovery_item_is_one_open_flow() -> None:
    """Plan 4.1 (F9): `BAKE_RECOVERY#<task_id>` is a declared DUE kind with the root clock."""
    reading = sweep(
        full(
            DUE_5=[
                due("DUE#5", "BAKE_RECOVERY#task-a", timedelta(seconds=60), recovery_reason="fatal")
            ]
        ),
        SHARDS,
        CLEAR,
        NOW,
    )

    assert "BAKE_RECOVERY" in FLOW_OBLIGATION_KINDS
    assert reading.open_flows == 1
    assert reading.without_root_clock_count == 0


def test_f9_control_the_same_shard_without_the_declared_kind_yields_no_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain.operations import stuck_flow  # noqa: PLC0415

    monkeypatch.setattr(
        stuck_flow, "FLOW_OBLIGATION_KINDS", FLOW_OBLIGATION_KINDS - {"BAKE_RECOVERY"}
    )
    item = due("DUE#5", "BAKE_RECOVERY#task-a", timedelta(seconds=60))
    with pytest.raises(SweepIncompleteError):
        sweep(full(DUE_5=[item]), SHARDS, CLEAR, NOW)


# --- plan 6.1 (F9): the four bake gauges, each from its one named read ------------------------

GRANT = "grant-fictional"
DIGEST = "e" * 64


def stamped(age: timedelta, **extra: object) -> DueObservation:
    """Return a `COMPLETE#` carrying the 5.1 readiness stamp `age` ago."""
    fields: dict[str, Any] = {
        "grant_id": GRANT,
        "bake_readiness_at": NOW - age,
        "input_effect_roster_digest": DIGEST,
        **extra,
    }
    return DueObservation("DUE#4", "COMPLETE#run-a", NOW - timedelta(minutes=5), **fields)


def contribution(settled_age: timedelta, digest: str = DIGEST) -> ContributionObservation:
    return ContributionObservation(GRANT, NOW - settled_age, digest)


def bake_counts(reading: SweepReading) -> tuple[int, int, int, int]:
    return (
        len(reading.bake_tasks_failed),
        len(reading.bake_retry_exhausted),
        len(reading.bake_precompute_blocked),
        len(reading.bake_finalizer_blocked),
    )


def swept(
    items: list[DueObservation], contributions: Mapping[str, ContributionObservation | None]
) -> SweepReading:
    return sweep(full(DUE_4=items), SHARDS, CLEAR, NOW, contributions)


def test_f9_the_age_gates_derive_from_the_contract_readiness_timeline() -> None:
    """TD-279: both thresholds move with the schedule, pass, lease, redelivery and claim."""
    from adapters.jobs.insight_precompute.graph_precompute import (  # noqa: PLC0415
        PRECOMPUTE_CLAIM_LEASE,
    )
    from adapters.jobs.preset_artifact_publisher.graph_bake import (  # noqa: PLC0415
        FINALIZE_ARMED_REDELIVERY_SECONDS,
    )

    period = int(BAKE_TASKS["worker_schedule_seconds"])
    ready = period + int(BAKE_TASKS["worker_pass_seconds"]) + PRECOMPUTE_CLAIM_LEASE.seconds
    assert ready == PRECOMPUTE_READY_SECONDS == FINALIZE_ARMED_REDELIVERY_SECONDS == 180
    assert timedelta(seconds=ready + period) == PRECOMPUTE_BLOCKED_AFTER == timedelta(seconds=240)
    claim = int(BAKE_TASKS["control_claim_seconds"])
    assert timedelta(seconds=ready + claim + period) == FINALIZER_BLOCKED_AFTER
    assert timedelta(seconds=270) == FINALIZER_BLOCKED_AFTER


@pytest.mark.parametrize(
    ("reason", "expected"), [("fatal", (1, 0, 0, 0)), ("retry_exhausted", (0, 1, 0, 0))]
)
def test_f9_a_recovery_item_counts_in_exactly_its_reasons_gauge(
    reason: str, expected: tuple[int, int, int, int]
) -> None:
    item = due("DUE#4", "BAKE_RECOVERY#task-a", timedelta(seconds=1), recovery_reason=reason)
    assert bake_counts(swept([item], {})) == expected
    # Control: the healthy shard, no recovery item, counts nothing.
    assert bake_counts(swept([], {})) == (0, 0, 0, 0)


@pytest.mark.parametrize("reason", ["unknown", None])
def test_f9_control_a_recovery_item_without_a_closed_reason_yields_no_value(
    reason: str | None,
) -> None:
    item = due("DUE#4", "BAKE_RECOVERY#task-a", timedelta(seconds=1), recovery_reason=reason)
    with pytest.raises(SweepIncompleteError, match="closed reason"):
        swept([item], {})


def test_f9_precompute_blocked_flips_exactly_past_its_derived_threshold() -> None:
    at = stamped(PRECOMPUTE_BLOCKED_AFTER)
    past = stamped(PRECOMPUTE_BLOCKED_AFTER + timedelta(seconds=1))
    assert bake_counts(swept([at], {GRANT: None})) == (0, 0, 0, 0)
    assert bake_counts(swept([past], {GRANT: None})) == (0, 0, 1, 0)
    # Control: the healthy row, the exact contribution settled just now, counts nothing.
    assert bake_counts(swept([past], {GRANT: contribution(timedelta(seconds=1))})) == (0, 0, 0, 0)


def test_f9_a_mismatched_digest_contribution_is_precompute_blocked_not_finalizer_blocked() -> None:
    other = contribution(timedelta(minutes=10), digest="f" * 64)
    past = stamped(PRECOMPUTE_BLOCKED_AFTER + timedelta(seconds=1))
    assert bake_counts(swept([past], {GRANT: other})) == (0, 0, 1, 0)


def test_f9_finalizer_blocked_flips_exactly_past_its_derived_threshold_since_settlement() -> None:
    item = stamped(timedelta(minutes=10))
    at = {GRANT: contribution(FINALIZER_BLOCKED_AFTER)}
    past = {GRANT: contribution(FINALIZER_BLOCKED_AFTER + timedelta(seconds=1))}
    assert bake_counts(swept([item], at)) == (0, 0, 0, 0)
    assert bake_counts(swept([item], past)) == (0, 0, 0, 1)
    assert swept([item], past).per_shard()["DUE#4"]["bake_finalizer_blocked"] == ["COMPLETE#run-a"]


def test_f9_only_a_stamp_older_than_the_younger_threshold_is_a_contribution_candidate() -> None:
    gate = min(PRECOMPUTE_BLOCKED_AFTER, FINALIZER_BLOCKED_AFTER)
    young, old = stamped(gate), stamped(gate + timedelta(seconds=1))
    unstamped = due("DUE#4", "COMPLETE#run-b", timedelta(minutes=1), grant_id="grant-b")
    assert bake_contribution_candidates([young, unstamped], NOW) == ()
    assert bake_contribution_candidates([old, old], NOW) == (GRANT,)
    # A young stamp counts nothing and needs no read.
    assert bake_counts(swept([young], {})) == (0, 0, 0, 0)


def test_f9_control_a_candidate_whose_contribution_was_not_read_yields_no_value() -> None:
    with pytest.raises(SweepIncompleteError, match="not read"):
        swept([stamped(PRECOMPUTE_BLOCKED_AFTER + timedelta(seconds=1))], {})


def test_f9_control_a_partial_readiness_stamp_yields_no_value() -> None:
    partial = stamped(
        PRECOMPUTE_BLOCKED_AFTER + timedelta(seconds=1), input_effect_roster_digest=None
    )
    with pytest.raises(SweepIncompleteError, match="partial"):
        bake_contribution_candidates([partial], NOW)
    with pytest.raises(SweepIncompleteError, match="partial"):
        swept([partial], {GRANT: None})


# TD-279: the recorded healthy timeline of dev run 69e71e5d (2026-10-14, benchmark-readout.md).
_READY = datetime(2026, 10, 14, 4, 1, 51, 67000, tzinfo=UTC)
"""The finalizer's first claim armed precompute (`bake_readiness_at`)."""
_SETTLED = datetime(2026, 10, 14, 4, 3, 3, 459000, tzinfo=UTC)
"""`graph_precompute_succeeded`: the exact precompute contribution settled."""
_FINALIZED = datetime(2026, 10, 14, 4, 4, 51, 883000, tzinfo=UTC)
"""The final transaction committed and deleted `COMPLETE#`."""
_DETECTOR_PASSES = tuple(
    datetime(2026, 10, 14, 4, minute, second, millis * 1000, tzinfo=UTC)
    for minute, second, millis in ((1, 52, 62), (2, 51, 962), (3, 52, 67))
)
"""The three detector passes (their terminal lines) that read the stamped `COMPLETE#` live; the
04:02:51 pass paged precompute-blocked and the 04:03:52 pass finalizer-blocked before TD-279."""


def _healthy_sweep(now: datetime, ready: datetime, settled: datetime | None) -> SweepReading:
    item = DueObservation(
        "DUE#3",
        "COMPLETE#run-69e71e5d",
        datetime(2026, 10, 14, 1, 39, 12, tzinfo=UTC),
        grant_id=GRANT,
        bake_readiness_at=ready,
        input_effect_roster_digest=DIGEST,
    )
    found = None if settled is None else ContributionObservation(GRANT, settled, DIGEST)
    candidates = bake_contribution_candidates([item], now)
    return sweep(full(DUE_3=[item]), SHARDS, CLEAR, now, dict.fromkeys(candidates, found))


def test_td279_the_recorded_healthy_timeline_pages_neither_bake_gauge() -> None:
    for now in _DETECTOR_PASSES:
        settled = _SETTLED if now >= _SETTLED else None
        assert (now, bake_counts(_healthy_sweep(now, _READY, settled))) == (now, (0, 0, 0, 0))
    # Control (sensitivity): the same timeline crossed the pre-TD-279 60 s and 30 s literals,
    # which is why it paged live; the derived thresholds sit above it, not beside it.
    assert _DETECTOR_PASSES[1] - _READY > timedelta(seconds=60)
    assert _DETECTOR_PASSES[2] - _SETTLED > timedelta(seconds=30)
    assert (
        _FINALIZED - _SETTLED < FINALIZER_BLOCKED_AFTER
        and _SETTLED - _READY < PRECOMPUTE_BLOCKED_AFTER
    )


def test_td279_control_a_precompute_stuck_past_its_threshold_pages() -> None:
    late = _READY + PRECOMPUTE_BLOCKED_AFTER + timedelta(seconds=1)
    assert bake_counts(_healthy_sweep(late, _READY, None)) == (0, 0, 1, 0)


def test_td279_control_a_finalizer_stuck_past_its_threshold_pages() -> None:
    late = _SETTLED + FINALIZER_BLOCKED_AFTER + timedelta(seconds=1)
    assert bake_counts(_healthy_sweep(late, _READY, _SETTLED)) == (0, 0, 0, 1)


def test_td279_a_clocked_coalesce_item_is_covered_by_the_ceiling_not_the_unclocked_gauge() -> None:
    clocked = due("DUE#8", "COALESCE#cohort-a", timedelta(minutes=5))
    assert len(sweep(full(DUE_8=[clocked]), SHARDS, CLEAR, NOW).without_root_clock) == 0
    old = due("DUE#8", "COALESCE#cohort-a", PROTOCOL_CEILING)
    assert len(sweep(full(DUE_8=[old]), SHARDS, CLEAR, NOW).past_ceiling) == 1


def test_td279_control_a_coalesce_item_without_a_clock_still_pages() -> None:
    unclocked = due("DUE#8", "COALESCE#cohort-a")
    reading = sweep(full(DUE_8=[unclocked]), SHARDS, CLEAR, NOW)
    assert [item.sk for item in reading.without_root_clock] == ["COALESCE#cohort-a"]


# The detector adapter over the real writers: the V3 bake's recovery item and readiness stamp.


@guard_reserved_words
@dataclass
class _ContributionSpy(FakeDynamo):
    """Record every `GetItem` key; fail the contribution read when asked to."""

    gets: list[tuple[str, str, bool]] = field(default_factory=list[tuple[str, str, bool]])
    fail_contribution: bool = False

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        pk, sk = str(key["pk"]["S"]), str(key["sk"]["S"])
        self.gets.append((pk, sk, bool(kwargs.get("ConsistentRead"))))
        if self.fail_contribution and pk.startswith("COHORT#"):
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "GetItem")
        return super().get_item(**kwargs)


def _sweep_at(fake: FakeDynamo, now: datetime) -> tuple[list[object], list[BaseException], Any]:
    emitted: list[object] = []
    failures: list[BaseException] = []
    context = DetectorContext(
        DynamoDueShardReader(TABLE, cast("Any", fake)),
        lambda reading, scope: emitted.append((reading, scope)),
        lambda: now,
        failures.append,
    )
    try:
        result: Any = detect(context)
    except SweepIncompleteError as error:
        result = error
    return emitted, failures, result


def test_f9_a_real_bake_recovery_item_counts_through_the_shard_query() -> None:
    bake = Bake(roster=roster(2))
    run = bake.granted()
    bake.fan_out(run)
    bake.dispatch(run)
    first, second = bake.roster.keys
    bake.sources.extra_on_task.add(first)  # fatal: a bound count differing from the pin
    bake.queries.fail[second] = DependencyError(ErrorCode("FICTIONAL", 1), "throttled")
    bake.task(run, first)
    bake.task(run, second, receive_count=1)
    claim = (f"EXEC_CLAIM#{bake.node(run, task_node_name(second)).node_id}", "CLAIM")
    bake.tables.items[claim]["lease_expires_at"] = {"S": "2000-01-01T00:00:00+00:00"}
    bake.task(run, second, receive_count=3)

    _emitted, failures, reading = _sweep_at(bake.tables, BAKE_NOW)

    assert failures == []
    assert bake_counts(reading) == (1, 1, 0, 0)


def test_f9_the_real_readiness_stamp_counts_precompute_then_finalizer_blocked_then_clears() -> None:
    bake = Bake(roster=roster(1))
    bake.seed_predecessors()
    run = bake.granted()
    bake.fan_out(run)
    bake.all_tasks(run)
    bake.arm()
    bake.dispatch(run)
    assert bake.deliver(run, "bake_finalize").outcome == "graph_bake_precompute_armed"
    spy = _ContributionSpy(items=bake.tables.items)
    later = BAKE_NOW + PRECOMPUTE_BLOCKED_AFTER + timedelta(seconds=1)

    emitted, failures, reading = _sweep_at(spy, later)
    assert (failures, len(emitted)) == ([], 1)
    assert bake_counts(reading) == (0, 0, 1, 0)
    grant = bake.run_item(run)["grant_id"]["S"]
    assert [get for get in spy.gets if get[0].startswith("COHORT#")] == [
        (f"COHORT#{grant}", "CONTRIBUTION#precompute", True)
    ]

    stored = release_store(bake.tables).read_precompute_input(run)
    assert stored is not None
    precompute_contributed(bake.tables, run, stored.digest, bake.roster.keys)  # settled BAKE_NOW
    stuck = BAKE_NOW + FINALIZER_BLOCKED_AFTER + timedelta(seconds=1)
    assert bake_counts(_sweep_at(spy, stuck)[2]) == (0, 0, 0, 1)
    assert bake_counts(_sweep_at(spy, later)[2]) == (0, 0, 0, 0)
    assert bake_counts(_sweep_at(spy, BAKE_NOW + FINALIZER_BLOCKED_AFTER)[2]) == (0, 0, 0, 0)

    # Control: an unreadable contribution GetItem yields no value for every gauge.
    spy.fail_contribution = True
    emitted, failures, result = _sweep_at(spy, later)
    assert (emitted, len(failures)) == ([], 1)
    assert isinstance(result, SweepIncompleteError)
    spy.fail_contribution = False

    # The final transaction deletes COMPLETE#, so a finalized winner counts nothing.
    assert bake.deliver(run, "bake_finalize", 2).node_state is NodeState.SUCCEEDED
    assert bake_counts(_sweep_at(spy, later)[2]) == (0, 0, 0, 0)


def test_f9_control_an_unparseable_readiness_stamp_makes_its_shard_unreadable() -> None:
    fake = FakeDynamo()
    fake.items[("DUE#4", "COMPLETE#run-a")] = {
        "pk": write_text("DUE#4"),
        "sk": write_text("COMPLETE#run-a"),
        "grant_id": write_text(GRANT),
        "bake_readiness_at": write_text("not-a-time"),
        "input_effect_roster_digest": write_text(DIGEST),
    }
    emitted, failures, result = _sweep_at(fake, NOW)
    assert (emitted, len(failures)) == ([], 1)
    assert isinstance(result, SweepIncompleteError)


# --- ADR-140: an unstamped COMPLETE# is anchored on the stored bake contribution ---------------

# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
LIVE_RUN = "f4e952df062c846accf065af6290e46dbcd24fda89f0345270f03e633685a7ad"
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
LIVE_GRANT = "15433587b53ec6c3f50025d974a697ff45cc63d2f86f95f0a995cc026c4fcc64"
LIVE_BAKE_SETTLED = datetime(2026, 10, 16, 23, 21, 50, 759789, tzinfo=UTC)
"""The live wedge: consumer step 3 settled the winner's bake `NO_ELIGIBLE` with no finalizer."""
LIVE_NOW = datetime(2026, 10, 17, 2, 36, 52, tzinfo=UTC)


def _unstamped(grant: str = LIVE_GRANT, run: str = LIVE_RUN) -> DueObservation:
    return DueObservation(
        "DUE#12", f"COMPLETE#{run}", LIVE_BAKE_SETTLED - timedelta(minutes=20), grant_id=grant
    )


def _unarmed_sweep(
    now: datetime, bake: datetime | None, precompute: datetime | None = None
) -> SweepReading:
    item = _unstamped()
    found = (
        None if bake is None else ContributionObservation(LIVE_GRANT, bake, None),
        None if precompute is None else ContributionObservation(LIVE_GRANT, precompute, None),
    )
    assert unarmed_contribution_candidates([item]) == (LIVE_GRANT,)
    return sweep(full(DUE_12=[item]), SHARDS, CLEAR, now, {}, {LIVE_GRANT: found})


@pytest.mark.covers("ADR-140")
def test_adr140_g1_the_live_wedge_counts_precompute_blocked() -> None:
    reading = _unarmed_sweep(LIVE_NOW, LIVE_BAKE_SETTLED)

    assert [item.sk for item in reading.bake_precompute_blocked] == [f"COMPLETE#{LIVE_RUN}"]
    assert bake_counts(reading) == (0, 0, 1, 0)


@pytest.mark.covers("ADR-140")
def test_adr140_g2_both_unstamped_windows_are_the_contract_constants() -> None:
    at, past = PRECOMPUTE_BLOCKED_AFTER, PRECOMPUTE_BLOCKED_AFTER + timedelta(seconds=1)
    assert bake_counts(_unarmed_sweep(LIVE_BAKE_SETTLED + at, LIVE_BAKE_SETTLED)) == (0, 0, 0, 0)
    assert bake_counts(_unarmed_sweep(LIVE_BAKE_SETTLED + past, LIVE_BAKE_SETTLED)) == (0, 0, 1, 0)
    # Bound but not completed: both members stored, counted from the later settlement.
    later = LIVE_BAKE_SETTLED + timedelta(seconds=5)
    at, past = FINALIZER_BLOCKED_AFTER, FINALIZER_BLOCKED_AFTER + timedelta(seconds=1)
    assert bake_counts(_unarmed_sweep(later + at, LIVE_BAKE_SETTLED, later)) == (0, 0, 0, 0)
    assert bake_counts(_unarmed_sweep(later + past, LIVE_BAKE_SETTLED, later)) == (0, 0, 0, 1)
    # Control: no bake member (fan-out pending, or a loser) counts nothing, however old.
    assert bake_counts(_unarmed_sweep(LIVE_NOW, None)) == (0, 0, 0, 0)


@pytest.mark.covers("ADR-140")
def test_adr140_g3_an_unread_or_grantless_complete_yields_no_value() -> None:
    with pytest.raises(SweepIncompleteError, match="not read"):
        sweep(full(DUE_12=[_unstamped()]), SHARDS, CLEAR, LIVE_NOW, {}, {})
    grantless = DueObservation("DUE#12", f"COMPLETE#{LIVE_RUN}", LIVE_BAKE_SETTLED)
    with pytest.raises(SweepIncompleteError, match="names no grant"):
        unarmed_contribution_candidates([grantless])
    with pytest.raises(SweepIncompleteError, match="names no grant"):
        sweep(full(DUE_12=[grantless]), SHARDS, CLEAR, LIVE_NOW, {}, {})


def _complete_item(fake: FakeDynamo, run: str, grant: str) -> None:
    fake.items[("DUE#12", f"COMPLETE#{run}")] = {
        "pk": write_text("DUE#12"),
        "sk": write_text(f"COMPLETE#{run}"),
        "run_id": write_text(run),
        "grant_id": write_text(grant),
        "root_clock_started_at": write_text((LIVE_NOW - timedelta(minutes=30)).isoformat()),
    }


@pytest.mark.covers("ADR-140")
def test_adr140_g3_a_loser_reads_only_its_bake_member_and_an_unreadable_one_emits_nothing() -> None:
    spy = _ContributionSpy()
    _complete_item(spy, "run-loser", "grant-loser")

    emitted, failures, reading = _sweep_at(spy, LIVE_NOW)

    assert (failures, len(emitted)) == ([], 1)
    assert bake_counts(reading) == (0, 0, 0, 0)
    assert [get for get in spy.gets if get[0].startswith("COHORT#")] == [
        ("COHORT#grant-loser", "CONTRIBUTION#bake", True)
    ]
    # The live wedge over the real reader: the stored bake member, no precompute, counts.
    spy.items[(f"COHORT#{LIVE_GRANT}", "CONTRIBUTION#bake")] = {
        "pk": write_text(f"COHORT#{LIVE_GRANT}"),
        "sk": write_text("CONTRIBUTION#bake"),
        "outcome": write_text("no_eligible"),
        "settled_at": write_text(LIVE_BAKE_SETTLED.isoformat()),
        "root_flow_id": write_text("root-live"),
    }
    _complete_item(spy, LIVE_RUN, LIVE_GRANT)
    _emitted, _failures, live = _sweep_at(spy, LIVE_NOW)
    assert [item.sk for item in live.bake_precompute_blocked] == [f"COMPLETE#{LIVE_RUN}"]
    # Control: an unreadable contribution GetItem yields no value for every gauge.
    spy.fail_contribution = True
    emitted, failures, result = _sweep_at(spy, LIVE_NOW)
    assert emitted == [] and len(failures) == 2
    assert isinstance(result, SweepIncompleteError)
