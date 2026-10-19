# SPDX-License-Identifier: MIT-0
"""S5 (FR-3): measurement identities, statistics, gauges and the projection's contribution.

The status projection writes each contribution atomically with the receipt row it measures.
Fictional data only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from execution_graph_support import FakeDynamo

from adapters.jobs.operational_status.graph_projection_rebuild import projection_store, rebuild
from adapters.outbound.dynamodb.metric_store import DynamoMetricStore, metric_shard
from adapters.outbound.dynamodb.operational_status import DynamoGraphProjectionStore
from adapters.outbound.telemetry.measurement_sink import PutMetricDataSink
from domain.operations.graph_projection import GraphProjectionConflictError, project_graph_item
from domain.operations.measurement import (
    BUCKETS_MS,
    MEASURED_TRANSITIONS,
    Gauge,
    MeasurementCorruptError,
    Statistics,
    aggregate_id,
    confirmed,
    gauges,
    measurement,
    receipt_measurement,
    snapshot_digest,
    window_of,
)
from domain.shared_kernel.errors import InvariantError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

AT = datetime(2026, 10, 8, 12, 34, 56, tzinfo=UTC)
DIMS = {
    "Protocol": "v1",
    "Surface": "graph",
    "Stage": "run_request_decided",
    "Outcome": "ADMITTED",
    "AuthorityScope": "probe",
}


def test_a_replayed_event_reproduces_the_same_measurement_identity() -> None:
    first = measurement("RECEIPT#r1/RECEIPT", "FlowRootElapsed", DIMS, 1200, AT)
    again = measurement("RECEIPT#r1/RECEIPT", "FlowRootElapsed", DIMS, 1200, AT)
    other = measurement("RECEIPT#r2/RECEIPT", "FlowRootElapsed", DIMS, 1200, AT)
    assert first.measurement_id == again.measurement_id != other.measurement_id
    assert first.aggregate_id == other.aggregate_id


def test_the_aggregate_is_keyed_by_family_window_and_closed_dimensions_only() -> None:
    start, end = window_of(AT)
    assert (start, end) == (
        AT.replace(minute=0, second=0),
        AT.replace(minute=0, second=0) + timedelta(hours=1),
    )
    a = aggregate_id("FlowRootElapsed", start, end, tuple(DIMS.items()))
    b = aggregate_id("FlowRootElapsed", end, end + timedelta(hours=1), tuple(DIMS.items()))
    assert a != b


@pytest.mark.parametrize(
    "dims",
    [
        {**DIMS, "RequestId": "r1"},
        {name: value for name, value in DIMS.items() if name != "Stage"},
        {**DIMS, "Outcome": ""},
    ],
)
def test_an_identifier_or_a_missing_or_blank_dimension_is_refused(dims: dict[str, str]) -> None:
    with pytest.raises(MeasurementCorruptError):
        measurement("RECEIPT#r1/RECEIPT", "FlowRootElapsed", dims, 1, AT)


def test_an_undeclared_family_is_refused() -> None:
    with pytest.raises(MeasurementCorruptError):
        measurement("RECEIPT#r1/RECEIPT", "Latency", DIMS, 1, AT)


def test_statistics_are_exact_cumulative_and_monotonic() -> None:
    stats = Statistics().add(500).add(5_000).add(9_000_000)
    assert (stats.count, stats.sum_ms, stats.max_ms) == (3, 9_005_500, 9_000_000)
    assert len(stats.buckets) == len(BUCKETS_MS) + 1
    assert stats.buckets[0] == 1  # <= 1000
    assert stats.buckets[1] == 2  # <= 5000 (cumulative)
    assert stats.buckets[-2] == 2  # <= 7200000
    assert stats.buckets[-1] == 3  # +Inf counts every sample
    assert list(stats.buckets) == sorted(stats.buckets)
    with pytest.raises(MeasurementCorruptError):
        stats.add(-1)


def test_a_snapshot_publishes_count_sum_max_and_every_bucket_as_gauges() -> None:
    stats = Statistics().add(2_000)
    published = gauges("FlowRootElapsed", tuple(DIMS.items()), stats)
    names = [gauge.name for gauge in published]
    assert names[:3] == ["FlowRootElapsedCount", "FlowRootElapsedSumMs", "FlowRootElapsedMaxMs"]
    buckets = [gauge for gauge in published if gauge.name == "FlowRootElapsedBucket"]
    assert [dict(gauge.dimensions)["BucketLe"] for gauge in buckets][-1] == "+Inf"
    assert len(buckets) == len(BUCKETS_MS) + 1
    assert all(set(dict(g.dimensions)) - {"BucketLe"} == set(DIMS) for g in published)
    assert snapshot_digest("a", 1, stats) == snapshot_digest("a", 1, Statistics().add(2_000))


def test_confirmation_needs_every_gauge_at_least_at_its_value() -> None:
    expected = gauges("FlowRootElapsed", tuple(DIMS.items()), Statistics().add(2_000))
    full: dict[Gauge, float | None] = {gauge: float(gauge.value) for gauge in expected}
    assert confirmed(expected, full)
    higher = {gauge: float(gauge.value + 1) for gauge in expected}
    assert confirmed(expected, cast("dict[Gauge, float | None]", higher))
    missing = dict(full)
    missing[expected[4]] = None
    assert not confirmed(expected, missing)
    lower = dict(full)
    lower[expected[0]] = 0.0
    assert not confirmed(expected, lower)


def test_a_receipt_measures_root_to_decision_and_names_its_authority() -> None:
    shadow = receipt_measurement(
        authority_key="RECEIPT#r1/RECEIPT",
        authority_epoch=0,
        decision="JOINED",
        recorded_at="2026-10-08T12:00:03+00:00",
        root_clock_started_at="2026-10-08T12:00:00+00:00",
    )
    assert shadow is not None
    assert shadow.value_ms == 3_000
    assert dict(shadow.dimensions)["Surface"] == "shadow"
    assert dict(shadow.dimensions)["AuthorityScope"] == "live"
    assert (
        receipt_measurement(
            authority_key="RECEIPT#r1/RECEIPT",
            authority_epoch=1,
            decision="REFUSED",
            recorded_at="2026-10-08T12:00:03+00:00",
            root_clock_started_at=None,
        )
        is None
    )


# The contribution is written in the projection row's own transaction.

STATUS = "status-fixture"


def _status() -> tuple[FakeDynamo, DynamoGraphProjectionStore]:
    fake = FakeDynamo(key_names=("partition_key", "sort_key"))
    metrics = DynamoMetricStore(STATUS, cast("Any", fake))
    return fake, DynamoGraphProjectionStore(STATUS, cast("Any", fake), metrics)


def _receipt_item(clock: str | None = "2026-10-08T11:59:58+00:00") -> dict[str, object]:
    item: dict[str, object] = {
        "pk": "RECEIPT#request-m1",
        "sk": "RECEIPT",
        "item_kind": "receipt",
        "request_id": "request-m1",
        "decision": "ADMITTED",
        "authority_epoch": 0,
        "mode": "SHADOW",
        "conflict_unit": "u" * 64,
        "input_revision": "2026-10-06T00:00:00+00:00",
        "fingerprint": "f" * 64,
        "recorded_at": "2026-10-08T12:00:00+00:00",
        "protocol_version": "v1",
        "contract_revision": 1,
        "root_flow_id": "root-m1",
    }
    if clock is not None:
        item["root_clock_started_at"] = clock
    return item


def _families(fake: FakeDynamo) -> list[str]:
    return sorted(pk.split("#", 1)[0] for pk, _sk in fake.items)


def test_a_receipt_row_and_its_contribution_commit_in_one_transaction() -> None:
    fake, store = _status()
    row = project_graph_item(_receipt_item())
    assert row is not None
    assert store.apply(row) is True
    assert len(fake.transactions) == 1
    assert _families(fake) == ["MEASUREMENT", "METRIC_ACCEPT", "METRIC_DUE", "XROOT"]
    accept = next(item for (pk, _), item in fake.items.items() if pk.startswith("METRIC_ACCEPT#"))
    assert accept["state"]["S"] == "PENDING"
    measurement_id = accept["partition_key"]["S"].removeprefix("METRIC_ACCEPT#")
    assert (metric_shard(measurement_id), f"ACCEPT#{measurement_id}") in fake.items


def test_a_replayed_row_adds_no_second_contribution() -> None:
    fake, store = _status()
    row = project_graph_item(_receipt_item())
    assert row is not None
    store.apply(row)
    before = dict(fake.items)
    assert store.apply(row) is False
    assert fake.items == before


def test_a_failed_projection_commit_leaves_neither_row_nor_contribution() -> None:
    fake, store = _status()
    row = project_graph_item(_receipt_item())
    assert row is not None
    fake.fail_next = True
    with pytest.raises(ClientError):
        store.apply(row)
    assert fake.items == {}


def test_a_changed_row_is_still_a_conflict() -> None:
    fake, store = _status()
    row = project_graph_item(_receipt_item())
    assert row is not None
    store.apply(row)
    changed = project_graph_item({**_receipt_item(), "decision": "JOINED"})
    assert changed is not None
    with pytest.raises(GraphProjectionConflictError):
        store.apply(changed)
    del fake


def test_a_rebuilt_receipt_yields_exactly_one_contribution_and_none_on_replay() -> None:
    fake = FakeDynamo(key_names=("partition_key", "sort_key"))
    store = projection_store(STATUS, cast("Any", fake))
    first = rebuild([_receipt_item()], store, dry_run=False)
    assert (first["rows_written"], first["rows_replayed"]) == (1, 0)
    assert _families(fake) == ["MEASUREMENT", "METRIC_ACCEPT", "METRIC_DUE", "XROOT"]
    before = dict(fake.items)
    again = rebuild([_receipt_item()], store, dry_run=False)
    assert (again["rows_written"], again["rows_replayed"]) == (0, 1)
    assert fake.items == before


def test_a_receipt_without_a_root_clock_and_a_graph_row_measure_nothing() -> None:
    fake, store = _status()
    row = project_graph_item(_receipt_item(clock=None))
    assert row is not None
    store.apply(row)
    graph = project_graph_item(
        {
            "pk": "GRAPH#g1",
            "sk": "GRAPH",
            "item_kind": "graph",
            "state": "SHADOW_RECORDED",
            "authority_epoch": 0,
            "root_flow_id": "root-m1",
        }
    )
    assert graph is not None
    store.apply(graph)
    assert _families(fake) == ["XROOT", "XROOT"]


class _SpyCloudWatch:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_metric_data(self, **request: Any) -> None:
        self.puts.append(request)


def test_the_probe_sink_refuses_any_gauge_outside_probe_scope_and_puts_nothing() -> None:
    spy = _SpyCloudWatch()
    sink = PutMetricDataSink(cast("Any", spy))
    probe = Gauge("FlowRootElapsedCount", tuple({**DIMS}.items()), 1, "Count")
    live = Gauge(
        "FlowRootElapsedCount", tuple({**DIMS, "AuthorityScope": "live"}.items()), 0, "Count"
    )
    with pytest.raises(InvariantError):
        sink.emit([probe, live], AT)
    assert spy.puts == []
    sink.emit([probe], AT)
    assert len(spy.puts) == 1


# S6 part 2 (design §2.10, carried item 0a): node and claim measurements, in their rows' commits.

RUN = "run-m1"
ARMED = "2026-10-09T12:00:00+00:00"
SETTLED = "2026-10-09T12:00:07.250000+00:00"
ENQUEUED = "2026-10-09T12:00:01+00:00"


def _node_item(node: str = "publish", state: str = "SUCCEEDED", **extra: object) -> dict[str, Any]:
    return {
        "pk": f"RUN#{RUN}",
        "sk": f"NODE#{node}",
        "item_kind": "node",
        "root_flow_id": "root-m1",
        "authority_epoch": 1,
        "node": node,
        "node_id": f"id-{node}",
        "state": state,
        "phase": "POST_GRANT",
        "dispatch_id": "d1",
        "armed_at": ARMED,
        "settled_at": SETTLED,
        **extra,
    }


def _claim_item(
    node: str = "write_unit:campaigns", admission: int = 1, **extra: object
) -> dict[str, Any]:
    return {
        "pk": f"EXEC_CLAIM#id-{node}",
        "sk": "CLAIM",
        "item_kind": "claim",
        "root_flow_id": "root-m1",
        "authority_epoch": 1,
        "run_id": RUN,
        "node": node,
        "dispatch_id": "d1",
        "lease_version": admission,
        "lease_expires_at": "2026-10-09T12:16:00+00:00",
        "claimed_at": "2026-10-09T12:00:03+00:00",
        "queue_enqueued_at": ENQUEUED,
        "first_received_at": "2026-10-09T12:00:02+00:00",
        "admission": admission,
        "admitted_at": "2026-10-09T12:00:03+00:00",
        **extra,
    }


def _measurements(fake: FakeDynamo) -> list[dict[str, Any]]:
    return [item for (pk, _), item in fake.items.items() if pk.startswith("MEASUREMENT#")]


def _apply(store: DynamoGraphProjectionStore, item: dict[str, Any]) -> bool:
    row = project_graph_item(item)
    assert row is not None
    return store.apply(row)


def test_a_terminal_node_row_commits_one_flow_transition_latency_with_it() -> None:
    fake, store = _status()
    assert _apply(store, _node_item()) is True
    assert len(fake.transactions) == 1
    (contribution,) = _measurements(fake)
    assert contribution["family"]["S"] == "FlowTransitionLatency"
    assert int(contribution["value_ms"]["N"]) == 7_250
    assert dict(json.loads(contribution["dimensions"]["S"])) == {
        "Protocol": "v1",
        "Surface": "graph",
        "Stage": "publish",
        "Outcome": "SUCCEEDED",
        "AuthorityScope": "live",
    }
    assert contribution["source"]["S"] == f"RUN#{RUN}/NODE#publish"
    # A replayed terminal image adds no second contribution.
    assert _apply(store, _node_item()) is False
    assert len(_measurements(fake)) == 1


def test_every_terminal_node_measures_once_under_its_kind_and_a_live_node_measures_nothing() -> (
    None
):
    fake, store = _status()
    for node, state in (
        ("materialize_raw", "SUCCEEDED"),
        ("write_unit:campaigns", "SUCCEEDED"),
        ("write_unit:retail", "FAILED"),
        ("grant_apply", "SUCCEEDED"),
        ("precompute", "SUCCEEDED"),
        ("bake", "FAILED"),
    ):
        _apply(store, _node_item(node, state))
    assert project_graph_item(_node_item("publish", "CLAIMED")) is None
    stages = sorted(
        (
            dict(json.loads(item["dimensions"]["S"]))["Stage"],
            dict(json.loads(item["dimensions"]["S"]))["Outcome"],
        )
        for item in _measurements(fake)
    )
    assert stages == [
        ("bake", "FAILED"),
        ("grant_apply", "SUCCEEDED"),
        ("materialize_raw", "SUCCEEDED"),
        ("precompute", "SUCCEEDED"),
        ("write_unit", "FAILED"),
        ("write_unit", "SUCCEEDED"),
    ]
    assert all(("FlowTransitionLatency", stage) in MEASURED_TRANSITIONS for stage, _ in stages)


def test_a_never_armed_terminal_node_projects_but_measures_nothing() -> None:
    fake, store = _status()
    unarmed = {
        k: v for k, v in _node_item("bake", "SUPERSEDED_UNCLAIMED").items() if k != "armed_at"
    }
    assert _apply(store, unarmed) is True
    assert _measurements(fake) == []
    assert _families(fake) == ["XROOT"]


def test_each_claim_admission_commits_one_activity_admission_elapsed() -> None:
    fake, store = _status()
    assert _apply(store, _claim_item()) is True
    (first,) = _measurements(fake)
    assert first["family"]["S"] == "ActivityAdmissionElapsed"
    assert int(first["value_ms"]["N"]) == 2_000
    dims = dict(json.loads(first["dimensions"]["S"]))
    assert (dims["Stage"], dims["Outcome"]) == ("write_unit", "claim")
    assert first["source"]["S"] == "EXEC_CLAIM#id-write_unit:campaigns/CLAIM#1"


def test_a_reclaim_row_includes_the_retry_wait_and_a_lease_bump_writes_no_row() -> None:
    fake, store = _status()
    _apply(store, _claim_item())
    rows_before = _families(fake).count("XROOT")
    # Control: a lease bump changes only lease fields, so the row and its contribution replay.
    bumped = _claim_item(lease_version=2, lease_expires_at="2026-10-09T12:30:00+00:00")
    assert _apply(store, bumped) is False
    assert _families(fake).count("XROOT") == rows_before
    assert len(_measurements(fake)) == 1
    # A RECLAIM is admission 2 at a later instant, against the first claim's enqueue time.
    reclaim = _claim_item(admission=2, lease_version=3, admitted_at="2026-10-09T12:17:01+00:00")
    assert _apply(store, reclaim) is True
    second = next(item for item in _measurements(fake) if item["source"]["S"].endswith("#2"))
    assert int(second["value_ms"]["N"]) == 17 * 60_000
    assert dict(json.loads(second["dimensions"]["S"]))["Outcome"] == "reclaim"
    assert len(_measurements(fake)) == 2


def test_a_claim_without_its_enqueue_instant_or_node_measures_nothing() -> None:
    fake, store = _status()
    no_enqueue = {k: v for k, v in _claim_item().items() if k != "queue_enqueued_at"}
    assert _apply(store, no_enqueue) is True
    no_node = {k: v for k, v in _claim_item("bake").items() if k != "node"}
    assert _apply(store, no_node) is True
    assert _measurements(fake) == []


def test_an_unknown_node_kind_is_refused_as_corrupt_and_writes_nothing() -> None:
    fake, store = _status()
    with pytest.raises(MeasurementCorruptError):
        _apply(store, _node_item("unknown_kind"))
    assert fake.items == {}
    # Control: a template kind under the same shape measures.
    assert _apply(store, _node_item("precompute")) is True


def test_the_audit_set_is_the_receipt_plus_every_node_kind_and_each_routed_claim() -> None:
    assert ("FlowRootElapsed", "run_request_decided") in MEASURED_TRANSITIONS
    assert {
        stage for family, stage in MEASURED_TRANSITIONS if family == "FlowTransitionLatency"
    } == {
        "materialize_raw",
        "write_unit",
        "grant_apply",
        "publish",
        "precompute",
        "bake",
        "bake_task",
        "bake_finalize",
    }
    assert {
        stage for family, stage in MEASURED_TRANSITIONS if family == "ActivityAdmissionElapsed"
    } == {
        "materialize_raw",
        "write_unit",
        "publish",
        "precompute",
        "bake",
        "bake_task",
        "bake_finalize",
    }
    assert len(MEASURED_TRANSITIONS) == 16
