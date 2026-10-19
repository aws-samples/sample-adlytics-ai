# SPDX-License-Identifier: MIT-0
"""Status v2 store shapes: graph events projected under their root flow (S2).

The graph items come from the real S1 admission service and adapter (`seeded_graph_store`), and
the projector path is the deployed stream handler's own function.
"""

from __future__ import annotations

import importlib
from typing import Any, Final, cast

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words
from execution_explanation_support import FakeStatusTable, seeded_graph_store

from adapters.config import operational_projector_settings, tracing_settings
from adapters.outbound.dynamodb.operational_status import (
    DynamoGraphProjectionStore,
    decode_item,
)
from domain.operations.graph_projection import (
    GRAPH_ROOT_PREFIX,
    GraphProjectionConflictError,
    ProjectionKind,
    epoch_label,
    project_graph_item,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-073")]


def _stream_record(item: dict[str, Any], sequence: str, event: str = "INSERT") -> dict[str, Any]:
    return {"eventName": event, "dynamodb": {"NewImage": item, "SequenceNumber": sequence}}


@pytest.fixture
def projector(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import the projector module under fictional settings."""
    monkeypatch.setenv("ADL_OPERATIONAL_STATUS_TABLE", "unit-test-operational-status")
    monkeypatch.setenv("ADL_SEMANTIC_PUBLICATIONS_TABLE", "unit-test-publications")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    operational_projector_settings.cache_clear()
    tracing_settings.cache_clear()
    return importlib.import_module("adapters.jobs.operational_status.projector")


def test_graph_and_receipt_items_map_to_rows_under_their_root_and_others_to_nothing() -> None:
    seeded = seeded_graph_store()
    first, second = seeded.receipts
    rows = {
        (pk, sk): project_graph_item(decode_item(cast("Any", item)))
        for (pk, sk), item in seeded.graph.items.items()
    }

    assert rows[("MODE#ingestion", "MODE")] is None
    graph = rows[(f"GRAPH#{first.graph_id}", "GRAPH")]
    assert graph is not None and graph.kind is ProjectionKind.GRAPH
    assert graph.partition_key == f"{GRAPH_ROOT_PREFIX}{first.root_flow_id}"
    assert graph.sort_key == f"GRAPH#0#{first.graph_id}"
    assert graph.document["state"] == "SHADOW_RECORDED"
    receipt = rows[(f"RECEIPT#{second.request_id}", "RECEIPT")]
    assert receipt is not None and receipt.kind is ProjectionKind.RECEIPT
    assert receipt.sort_key == f"RECEIPT#{second.recorded_at}#p1-second#RECEIPT"
    assert receipt.document["decision"] == "JOINED"
    assert receipt.authority_epoch == 0
    assert epoch_label(receipt.authority_epoch) == "shadow"
    assert epoch_label(1) == "graph"
    # Closed fields only: the requester digest and request digest are not copied.
    assert "requester" not in receipt.document
    assert "request_digest" not in receipt.document


def test_an_item_without_a_root_or_epoch_is_not_projected() -> None:
    assert (
        project_graph_item({"pk": "RECEIPT#x", "sk": "SUPERSEDED", "item_kind": "receipt"}) is None
    )
    assert project_graph_item({"pk": "GRAPH#x", "sk": "GRAPH", "item_kind": "graph"}) is None


def test_apply_is_idempotent_and_a_changed_row_is_a_conflict() -> None:
    seeded = seeded_graph_store()

    assert seeded.project_all() == 3
    assert seeded.project_all() == 0
    puts = seeded.status.puts

    (key, item) = next(
        (key, item) for key, item in seeded.graph.items.items() if key[1] == "RECEIPT"
    )
    changed = decode_item(cast("Any", item)) | {"reason": "a different authority value"}
    row = project_graph_item(changed)
    assert row is not None
    with pytest.raises(GraphProjectionConflictError):
        seeded.projection.apply(row)
    assert seeded.status.puts == puts
    del key


def test_receipts_page_in_recorded_order_with_no_duplicate_or_omission() -> None:
    seeded = seeded_graph_store(("p1-req-a", "p1-req-b", "p1-req-c", "p1-req-d", "p1-req-e"))
    seeded.project_all()
    root = str(seeded.receipts[0].root_flow_id)

    pages: list[list[str]] = []
    after: str | None = None
    while True:
        rows, more = seeded.projection.receipts(root, after, 2)
        pages.append([str(row.document["request_id"]) for row in rows])
        if not more:
            break
        after = rows[-1].sort_key

    assert pages == [["p1-req-a", "p1-req-b"], ["p1-req-c", "p1-req-d"], ["p1-req-e"]]
    assert all(query["ConsistentRead"] is True for query in seeded.status.queries)
    whole, more = seeded.projection.receipts(root, None, 25)
    assert [row.document["request_id"] for row in whole] == [
        name for page in pages for name in page
    ]
    assert more is False
    (graph,) = seeded.projection.graphs(root)
    assert graph.document["graph_id"] == seeded.receipts[0].graph_id


def test_a_continuation_into_another_collection_is_refused() -> None:
    seeded = seeded_graph_store()
    seeded.project_all()
    from domain.operations.status import OperationalStatusError  # noqa: PLC0415

    with pytest.raises(OperationalStatusError):
        seeded.projection.receipts(str(seeded.receipts[0].root_flow_id), "GRAPH#0#x", 2)


def test_the_stream_handler_projects_inserts_and_skips_other_events(projector: Any) -> None:
    seeded = seeded_graph_store()
    status = FakeStatusTable()
    store = DynamoGraphProjectionStore("status", status)  # type: ignore[arg-type]
    records = [
        _stream_record(item, str(index)) for index, item in enumerate(seeded.graph.items.values())
    ]
    records.append({"eventName": "REMOVE", "dynamodb": {"SequenceNumber": "99"}})

    failures = projector.apply_graph_records({"Records": records}, store)
    again = projector.apply_graph_records({"Records": records}, store)

    assert failures == () and again == ()
    assert status.puts == 3
    assert {sk.split("#", 1)[0] for _, sk in status.items} == {"GRAPH", "RECEIPT"}


def test_the_stream_handler_stops_at_the_first_failure_and_reports_it_once(
    projector: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = seeded_graph_store()
    reports: list[BaseException] = []

    def _record(error: BaseException, _report: object) -> None:
        reports.append(error)

    monkeypatch.setattr(projector, "report_error", _record)

    @guard_reserved_words
    class _Failing(FakeStatusTable):
        def put_item(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
                "PutItem",
            )

    store = DynamoGraphProjectionStore("status", _Failing())  # type: ignore[arg-type]
    graph_items = [item for (_, sk), item in seeded.graph.items.items() if sk != "MODE"]
    records = [_stream_record(item, f"{index:03d}") for index, item in enumerate(graph_items)]

    failures = projector.apply_graph_records({"Records": records}, store)

    assert failures == ("000",)
    assert len(reports) == 1


def test_the_projector_never_touches_the_graph_store(projector: Any) -> None:
    seeded = seeded_graph_store()
    before = len(seeded.graph.transactions)
    records = [_stream_record(item, "1") for item in seeded.graph.items.values()]

    projector.apply_graph_records(
        {"Records": records},
        DynamoGraphProjectionStore("status", FakeStatusTable()),  # type: ignore[arg-type]
    )

    assert len(seeded.graph.transactions) == before
    assert projector.graph_handler.__name__ == "graph_handler"


# --- S6 part 2 (design §2.10): NODE, CLAIM and SERVING rows ----------------------------------
# The source fields are fixture-only until slices 2 and 5 write them on the graph items.

_ROOT_FLOW = "00000000-0000-5000-8000-0000000000a1"


def _node_item(state: str, **extra: object) -> dict[str, object]:
    return {
        "pk": "RUN#run-0001",
        "sk": "NODE#materialize_raw",
        "item_kind": "node",
        "authority_epoch": 1,
        "root_flow_id": _ROOT_FLOW,
        "run_id": "run-0001",
        "node": "materialize_raw",
        "node_id": "n" * 64,
        "state": state,
        "revision": 4,
        "phase": "PRE_GRANT",
        "after": [],
        "lock_key": "MATERIALIZE#u",
        "dispatch_id": "d" * 64,
        "put_at": "2026-10-09T00:00:00+00:00",
        "armed_at": "2026-10-09T00:00:01+00:00",
        **extra,
    }


def _claim_item(admission: int, **extra: object) -> dict[str, object]:
    return {
        "pk": f"EXEC_CLAIM#{'n' * 64}",
        "sk": "CLAIM",
        "item_kind": "claim",
        "authority_epoch": 1,
        "root_flow_id": _ROOT_FLOW,
        "run_id": "run-0001",
        "dispatch_id": "d" * 64,
        "admission": admission,
        "admitted_at": f"2026-10-09T00:0{admission}:00+00:00",
        "claimed_at": "2026-10-09T00:01:00+00:00",
        "queue_enqueued_at": "2026-10-09T00:00:30+00:00",
        "lease_version": admission,
        "lease_expires_at": "2026-10-09T00:20:00+00:00",
        **extra,
    }


def _serving_item() -> dict[str, object]:
    return {
        "pk": "SERVING#grant-0001",
        "sk": "BINDING",
        "item_kind": "serving_binding",
        "authority_epoch": 1,
        "root_flow_id": _ROOT_FLOW,
        "binding_id": "b" * 64,
        "conflict_unit": "u" * 64,
        "served_revision": "2026-10-06T00:00:00+00:00",
        "contributor_roots": [_ROOT_FLOW],
        "bound_at": "2026-10-09T00:30:00+00:00",
        "cohort_revision": "cohort-1",
        "serving_input_revision": "input-1",
        "winner_run_id": "run-0001",
        "binding_kind": "direct",
    }


def test_a_terminal_node_projects_and_a_non_terminal_node_maps_to_nothing() -> None:
    row = project_graph_item(
        _node_item("SUCCEEDED", settled_at="2026-10-09T00:05:00+00:00", effect_digest="e" * 64)
    )
    assert row is not None and row.kind is ProjectionKind.NODE
    assert row.sort_key == "NODE#1#run-0001#materialize_raw"
    assert row.partition_key == f"{GRAPH_ROOT_PREFIX}{_ROOT_FLOW}"
    assert (row.authority_pk, row.authority_sk) == ("RUN#run-0001", "NODE#materialize_raw")
    assert row.document == {
        "run_id": "run-0001",
        "node": "materialize_raw",
        "node_id": "n" * 64,
        "state": "SUCCEEDED",
        "phase": "PRE_GRANT",
        "dispatch_id": "d" * 64,
        "put_at": "2026-10-09T00:00:00+00:00",
        "armed_at": "2026-10-09T00:00:01+00:00",
        "settled_at": "2026-10-09T00:05:00+00:00",
        "effect_digest": "e" * 64,
    }
    # Control: the same node in any non-terminal state maps to nothing.
    for state in ("PENDING", "READY", "PERMITTED", "DISPATCHED", "CLAIMED"):
        assert project_graph_item(_node_item(state)) is None
    for state in ("FAILED", "FENCED_UNSENT", "SUPERSEDED_UNCLAIMED", "COALESCED"):
        assert project_graph_item(_node_item(state)) is not None


def test_a_changed_terminal_node_image_is_a_projection_conflict() -> None:
    projection = DynamoGraphProjectionStore("status", cast("Any", FakeStatusTable()))
    settled = _node_item("SUCCEEDED", settled_at="2026-10-09T00:05:00+00:00")
    row = project_graph_item(settled)
    assert row is not None and projection.apply(row) is True
    # Control: the exact terminal image replays as a no-op, and a revision-only bump too.
    assert projection.apply(row) is False
    bumped = project_graph_item({**settled, "revision": 5})
    assert bumped is not None and projection.apply(bumped) is False
    changed = project_graph_item({**settled, "settled_at": "2026-10-09T00:06:00+00:00"})
    assert changed is not None
    with pytest.raises(GraphProjectionConflictError) as conflict:
        projection.apply(changed)
    assert str(conflict.value.code) == "ADLY-EXEC_PROJECTION-0001"


def test_each_claim_admission_projects_its_own_row_and_a_lease_bump_replays() -> None:
    first = project_graph_item(_claim_item(1))
    assert first is not None and first.kind is ProjectionKind.CLAIM
    assert first.sort_key == f"CLAIM#1#{'n' * 64}#1"
    assert "lease_version" not in first.document and "lease_expires_at" not in first.document
    assert first.document["admission"] == 1
    second = project_graph_item(_claim_item(2))
    assert second is not None and second.sort_key == f"CLAIM#1#{'n' * 64}#2"
    # A lease bump changes only lease fields, so it maps to the identical row.
    bumped = project_graph_item(
        _claim_item(1, lease_version=7, lease_expires_at="2026-10-09T01:00:00+00:00")
    )
    assert bumped is not None and bumped.digest == first.digest
    # Control: a claim with no admission counter (written before slice 2) maps to nothing.
    assert project_graph_item({**_claim_item(1), "admission": None}) is None


def test_a_serving_binding_projects_under_its_grant() -> None:
    row = project_graph_item(_serving_item())
    assert row is not None and row.kind is ProjectionKind.SERVING
    assert row.sort_key == "SERVING#1#grant-0001"
    assert row.document["contributor_roots"] == [_ROOT_FLOW]
    assert row.document["grant_id"] == "grant-0001"
    assert row.document["cohort_revision"] == "cohort-1"
    assert row.document["serving_input_revision"] == "input-1"
    assert row.document["winner_run_id"] == "run-0001"
    assert row.document["binding_kind"] == "direct"
    # Control: today's binding, without root_flow_id and authority_epoch, maps to nothing.
    legacy = {
        k: v for k, v in _serving_item().items() if k not in {"root_flow_id", "authority_epoch"}
    }
    assert project_graph_item(legacy) is None


_RETIRED_SERVING_FIELDS: Final = {
    "serving_day": "2026-10-09",
    "reservation_seconds": 1980,
    "capacity_state": "ROLLED_OVER",
    "capacity_reason": "serving_execution_budget_exhausted",
    "rollover_count": 1,
    "refusal_reason": "serving_execution_budget_exhausted",
}


def test_retired_day_and_wait_items_and_fields_never_project() -> None:
    """Plan item 4: retained historical day/wait rows and fields are inert to the status plane."""
    rooted = {"authority_epoch": 1, "root_flow_id": _ROOT_FLOW}
    for item in (
        {"pk": "SERVING_DAY#2026-10-09", "sk": "RESERVATION", "item_kind": "serving_day"},
        {"pk": "SHARD#00", "sk": "CAPACITY_WAIT#cohort-1", "item_kind": "capacity_wait"},
        {"pk": "SHARD#00", "sk": "PUBLISH_WAIT#run-0001", "item_kind": "publish_wait"},
    ):
        assert project_graph_item({**item, **rooted}) is None
    # Control: the same retired fields on a live binding and a terminal node are dropped.
    serving = project_graph_item({**_serving_item(), **_RETIRED_SERVING_FIELDS})
    node = project_graph_item({**_node_item("COALESCED"), **_RETIRED_SERVING_FIELDS})
    assert serving is not None and node is not None
    for row in (serving, node):
        assert set(_RETIRED_SERVING_FIELDS).isdisjoint(row.document)
    current = project_graph_item(_serving_item())
    assert current is not None and serving.digest == current.digest


def test_the_rebuild_scans_the_new_kinds_and_maps_them_through_the_same_function() -> None:
    from adapters.jobs.operational_status.graph_projection_rebuild import rebuild  # noqa: PLC0415

    items = [
        _node_item("SUCCEEDED", settled_at="2026-10-09T00:05:00+00:00"),
        _node_item("READY") | {"sk": "NODE#grant_apply", "node": "grant_apply"},
        _claim_item(1),
        _serving_item(),
    ]
    projection = DynamoGraphProjectionStore("status", cast("Any", FakeStatusTable()))
    result = rebuild(items, projection, dry_run=False)
    assert (result["items_read"], result["rows_mapped"], result["rows_written"]) == (4, 3, 3)
    sort_keys = sorted(str(row).split("|")[1] for row in cast("list[str]", result["rows"]))
    assert sort_keys == [
        f"CLAIM#1#{'n' * 64}#1",
        "NODE#1#run-0001#materialize_raw",
        "SERVING#1#grant-0001",
    ]


# --- P6(e) fix forward: a bake claim after `record_bake_progress` ------------------------------
# The dev bake claim of run e79dc41c as the graph table holds it (raw scan, 2026-10-11T17:24Z):
# `ADD bake_done` left a DynamoDB string set on the claim, so its stream MODIFY image and the
# rebuild scan both carry one.

_LIVE_BAKE_CLAIM: Final[dict[str, Any]] = {
    "run_id": {"S": "e79dc41c58e43771737caf4d66f05a0b502b7e2bbd0431c3b2b7233dc0f3140f"},
    "queue_enqueued_at": {"S": "2026-10-11T15:25:50.595000+00:00"},
    "node": {"S": "bake"},
    "admission": {"N": "1"},
    "dispatch_id": {"S": "16d0bf596af98c55d84adbed62309f658ec2dddbd02b6b55b6a6c0e18d4e991a"},
    "authority_epoch": {"N": "1"},
    "bake_done": {
        "SS": [
            "campaigns#14d",
            "campaigns#1d",
            "campaigns#30d",
            "campaigns#7d",
            "retail-health#14d",
            "retail-health#1d",
            "retail-health#7d",
        ]
    },
    "lease_expires_at": {"S": "2026-10-11T15:27:44.462005+00:00"},
    "claimed_at": {"S": "2026-10-11T15:25:58.913945+00:00"},
    "lease_version": {"N": "2"},
    "admitted_at": {"S": "2026-10-11T15:25:58.913945+00:00"},
    "sk": {"S": "CLAIM"},
    "item_kind": {"S": "claim"},
    "pk": {"S": "EXEC_CLAIM#8ec5b1004e3ad567b7916f7fb0d145d1086a11a6375b86d413a166f2aba892f3"},
    "first_received_at": {"S": "2026-10-11T15:25:50.595000+00:00"},
    "root_flow_id": {"S": "c6b31759-a536-50a4-8483-f1dcdbcb61f6"},
}


def _bake_claim_images() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return the claim at admission, after progress (live), and with an unavailable intent."""
    admitted = {name: value for name, value in _LIVE_BAKE_CLAIM.items() if name != "bake_done"}
    unavailable = _LIVE_BAKE_CLAIM | {
        "bake_done": {"SS": ["retail-health#1d", "campaigns#7d"]},
        "bake_unavailable": {"SS": ["retail-health#1d|no_rows"]},
    }
    return admitted, dict(_LIVE_BAKE_CLAIM), unavailable


def test_a_string_set_decodes_to_sorted_text_and_other_set_types_are_still_refused() -> None:
    decoded = decode_item({"bake_done": {"SS": ["b#7d", "a#1d", "a#14d"]}})
    assert decoded == {"bake_done": ["a#14d", "a#1d", "b#7d"]}
    # Control: number and binary sets stay unprojected types, as before.
    from domain.operations.status import OperationalStatusError  # noqa: PLC0415

    for refused in ({"NS": ["1"]}, {"BS": [b"x"]}, {"B": b"x"}):
        with pytest.raises(OperationalStatusError):
            decode_item(cast("Any", {"value": refused}))


def test_a_post_progress_bake_claim_replays_its_admission_row_on_the_stream(
    projector: Any,
) -> None:
    status = FakeStatusTable()
    store = DynamoGraphProjectionStore("status", status)  # type: ignore[arg-type]
    admitted, progressed, unavailable = _bake_claim_images()
    records = [
        _stream_record(admitted, "001"),
        _stream_record(progressed, "002", "MODIFY"),
        _stream_record(unavailable, "003", "MODIFY"),
    ]

    failures = projector.apply_graph_records({"Records": records}, store)

    # One CLAIM row from the admission; both progress images replay it exactly.
    assert failures == ()
    assert status.puts == 1
    ((_, sort_key),) = status.items
    assert sort_key == f"CLAIM#1#{_LIVE_BAKE_CLAIM['pk']['S'].removeprefix('EXEC_CLAIM#')}#1"
    rows = [project_graph_item(decode_item(cast("Any", image))) for image in _bake_claim_images()]
    assert len({row.digest for row in rows if row is not None}) == 1
    assert all(row is not None and "bake_done" not in row.document for row in rows)


def test_the_rebuild_scan_decodes_a_post_progress_bake_claim() -> None:
    from adapters.jobs.operational_status import graph_projection_rebuild  # noqa: PLC0415

    _, progressed, unavailable = _bake_claim_images()

    @guard_reserved_words
    class _Graph:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []

        def scan(self, **request: Any) -> dict[str, Any]:
            self.requests.append(request)
            if "ExclusiveStartKey" not in request:
                return {
                    "Items": [progressed],
                    "ScannedCount": 11_062,
                    "LastEvaluatedKey": {"pk": progressed["pk"], "sk": progressed["sk"]},
                }
            return {"Items": [unavailable], "ScannedCount": 1}

    class _Session:
        def __init__(self) -> None:
            self.graph = _Graph()

        def client(self, name: str) -> _Graph:
            assert name == "dynamodb"
            return self.graph

    session = _Session()
    scan = graph_projection_rebuild._scan  # pyright: ignore[reportPrivateUsage]
    items = scan(cast("Any", session), "graph")

    assert [item["bake_done"] for item in items] == [
        sorted(progressed["bake_done"]["SS"]),
        ["campaigns#7d", "retail-health#1d"],
    ]
    assert all(request["ConsistentRead"] is True for request in session.graph.requests)
    projection = DynamoGraphProjectionStore("status", cast("Any", FakeStatusTable()))
    result = graph_projection_rebuild.rebuild(items, projection, dry_run=False)
    assert (result["items_read"], result["rows_mapped"], result["rows_written"]) == (2, 2, 1)
    assert result["rows_replayed"] == 1


def test_the_rebuild_scan_still_stops_past_its_bound() -> None:
    from adapters.jobs.operational_status import graph_projection_rebuild  # noqa: PLC0415

    bound = graph_projection_rebuild.MAX_SCANNED_ITEMS
    scan = graph_projection_rebuild._scan  # pyright: ignore[reportPrivateUsage]

    @guard_reserved_words
    class _Graph:
        def scan(self, **request: Any) -> dict[str, Any]:
            del request
            return {"Items": [], "ScannedCount": bound + 1}

    class _Session:
        def client(self, name: str) -> _Graph:
            del name
            return _Graph()

    with pytest.raises(RuntimeError, match="rebuild scan bound"):
        scan(cast("Any", _Session()), "graph")
