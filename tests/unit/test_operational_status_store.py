# SPDX-License-Identifier: MIT-0
"""Conditional DynamoDB projection request shapes, cursors, and budgets, without AWS."""

from __future__ import annotations

# The transaction budget is a private composition seam exercised directly.
# pyright: reportPrivateUsage=false
import base64
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words
from operational_status_support import NOW, flow_event, uuid_text

from adapters.outbound.dynamodb.operational_status import (
    DynamoOperationalStatusStore,
    _within_budget,
    decode_cursor,
    encode_cursor,
)
from domain.operations.status import (
    BaselineFoldRequest,
    OperationalDataFlowEvent,
    OperationalStatusError,
    ReductionKind,
    event_document,
    initial_baseline,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-073", "ADR-074")]

FLOW_ID = uuid_text(2)
NEW_ITEM = "attribute_not_exists(partition_key) AND attribute_not_exists(sort_key)"


@guard_reserved_words
@dataclass
class _RecordingDynamo:
    """Dict-backed recording client that preserves exact low-level request shapes."""

    items: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict[tuple[str, str], dict[str, Any]]
    )
    puts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    queries: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    transactions: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def get_item(self, **request: Any) -> dict[str, Any]:
        key = cast("dict[str, dict[str, str]]", request["Key"])
        item = self.items.get((key["partition_key"]["S"], key["sort_key"]["S"]))
        return {} if item is None else {"Item": item}

    def put_item(self, **request: Any) -> dict[str, Any]:
        self.puts.append(request)
        item = cast("dict[str, dict[str, str]]", request["Item"])
        key = (item["partition_key"]["S"], item["sort_key"]["S"])
        if key in self.items and "ConditionExpression" in request:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        self.items[key] = dict(item)
        return {}

    def query(self, **request: Any) -> dict[str, Any]:
        self.queries.append(request)
        if "begins_with" not in request["KeyConditionExpression"]:
            return {"Items": []}
        values = cast("dict[str, dict[str, str]]", request["ExpressionAttributeValues"])
        partition_key = values[":surface"]["S"]
        prefix = values[":prefix"]["S"]
        found = [
            item
            for (stored_partition, sort_key), item in sorted(self.items.items())
            if stored_partition == partition_key and sort_key.startswith(prefix)
        ]
        if not request.get("ScanIndexForward", True):
            found.reverse()
        return {"Items": found}

    def transact_write_items(self, **request: Any) -> dict[str, Any]:
        self.transactions.append(request)
        for action in cast("list[dict[str, Any]]", request["TransactItems"]):
            if "Put" in action:
                item = cast("dict[str, dict[str, str]]", action["Put"]["Item"])
                self.items[(item["partition_key"]["S"], item["sort_key"]["S"])] = dict(item)
        return {}


def _store(client: _RecordingDynamo) -> DynamoOperationalStatusStore:
    return DynamoOperationalStatusStore("operational-status-table", cast("Any", client))


class TestCursor:
    def test_a_cursor_round_trips_for_its_own_flow(self) -> None:
        assert decode_cursor(encode_cursor(FLOW_ID, 3), FLOW_ID) == 3

    def test_a_cursor_is_bound_to_one_flow(self) -> None:
        assert decode_cursor(encode_cursor(FLOW_ID, 3), uuid_text(9)) is None

    def test_a_non_canonical_reencode_is_rejected(self) -> None:
        """A hand-built cursor with equal content but different bytes is not server-issued."""
        loose = json.dumps({"after": 3, "flow": FLOW_ID, "version": 1})
        forged = base64.urlsafe_b64encode(loose.encode()).decode().rstrip("=")

        assert decode_cursor(forged, FLOW_ID) is None

    def test_tampered_or_malformed_cursors_are_rejected(self) -> None:
        assert decode_cursor("", FLOW_ID) is None
        assert decode_cursor("!!!not-base64", FLOW_ID) is None
        assert decode_cursor("x" * 513, FLOW_ID) is None
        assert decode_cursor(_forge({"after": -1, "flow": FLOW_ID, "version": 1}), FLOW_ID) is None
        assert (
            decode_cursor(_forge({"after": True, "flow": FLOW_ID, "version": 1}), FLOW_ID) is None
        )
        assert decode_cursor(_forge({"after": 3, "flow": FLOW_ID, "version": 2}), FLOW_ID) is None
        assert decode_cursor(_forge({"after": 3, "flow": FLOW_ID}), FLOW_ID) is None
        assert decode_cursor(_forge([1, 2, 3]), FLOW_ID) is None


def _forge(value: object) -> str:
    document = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(document).decode().rstrip("=")


class TestTransactionBudget:
    def test_the_action_budget_boundary_for_pending_events(self) -> None:
        pending = [(flow_event(), True)]

        assert _within_budget(pending * 6) is True
        assert _within_budget(pending * 7) is False

    def test_the_action_budget_boundary_for_new_events(self) -> None:
        arrived = [(flow_event(), False)]

        assert _within_budget(arrived * 8) is True
        assert _within_budget(arrived * 9) is False

    def test_the_byte_budget_refuses_one_oversized_event(self) -> None:
        oversized = flow_event(reason_code="x" * 400_000)

        assert _within_budget([(oversized, False)]) is False


WINDOW_START = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _baseline(sample_count: int) -> Any:
    return initial_baseline(
        BaselineFoldRequest(
            measurement_ref="semantic_candidate_decision_latency",
            measurement_version="1",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            finalized_at=WINDOW_END,
            sample_count=sample_count,
        )
    )


class TestBaselinePersistence:
    def test_record_baseline_creates_one_immutable_conditional_revision(self) -> None:
        client = _RecordingDynamo()
        window = _baseline(3)

        assert _store(client).record_baseline("semantic_candidate_publication", window) is True

        (request,) = client.puts
        assert request["ConditionExpression"] == NEW_ITEM
        item = cast("dict[str, dict[str, str]]", request["Item"])
        assert item["partition_key"] == {"S": "SURFACE#semantic_candidate_publication"}
        assert item["sort_key"] == {
            "S": (f"BASELINE#semantic_candidate_decision_latency#{WINDOW_START.isoformat()}#R001")
        }

    def test_a_lost_create_reports_the_existing_revision(self) -> None:
        client = _RecordingDynamo()
        store = _store(client)
        window = _baseline(3)

        assert store.record_baseline("semantic_candidate_publication", window) is True
        assert store.record_baseline("semantic_candidate_publication", window) is False

    def test_baselines_read_back_what_record_baseline_stored_newest_first(self) -> None:
        client = _RecordingDynamo()
        store = _store(client)
        first = _baseline(3)
        corrected = replace(
            first,
            window_id=uuid_text(9),
            predecessor_window_id=first.window_id,
            revision=2,
            sample_count=5,
            late_sample_count=2,
        )
        store.record_baseline("semantic_candidate_publication", first)
        store.record_baseline("semantic_candidate_publication", corrected)

        found = store.baselines(
            "semantic_candidate_publication", "semantic_candidate_decision_latency"
        )

        assert found == (corrected, first)


class TestReduce:
    def test_a_receipt_with_different_immutable_evidence_is_refused(self) -> None:
        client = _RecordingDynamo()
        event = flow_event()
        rival = flow_event(stage="candidate_retried")
        client.items[(f"EVENT#{event.event_id}", "RECEIPT")] = {"payload": {"S": _payload(rival)}}

        with pytest.raises(OperationalStatusError, match="different immutable evidence"):
            _store(client).reduce(event)

    def test_an_orphan_successor_parks_its_receipt_and_pending_slot(self) -> None:
        client = _RecordingDynamo()
        orphan = flow_event(transition_sequence=2, predecessor_event_id=uuid_text(9))

        outcome = _store(client).reduce(orphan)

        assert outcome.kind is ReductionKind.PARK
        (transaction,) = client.transactions
        receipt, pending = [
            cast("dict[str, Any]", action["Put"])
            for action in cast("list[dict[str, Any]]", transaction["TransactItems"])
        ]
        assert receipt["ConditionExpression"] == NEW_ITEM
        assert receipt["Item"]["partition_key"] == {"S": f"EVENT#{orphan.event_id}"}
        assert receipt["Item"]["sort_key"] == {"S": "RECEIPT"}
        assert receipt["Item"]["disposition"] == {"S": "PENDING"}
        assert pending["ConditionExpression"] == NEW_ITEM
        assert pending["Item"]["partition_key"] == {"S": f"FLOW#{orphan.flow_id}"}
        assert pending["Item"]["sort_key"] == {"S": f"PENDING#{2:020d}"}
        assert pending["Item"]["pending_state"] == {"S": "PENDING"}
        assert pending["Item"]["event_id"] == {"S": orphan.event_id}

    def test_a_parked_event_arriving_again_parks_without_a_second_write(self) -> None:
        client = _RecordingDynamo()
        store = _store(client)
        orphan = flow_event(transition_sequence=2, predecessor_event_id=uuid_text(9))

        assert store.reduce(orphan).kind is ReductionKind.PARK
        assert store.reduce(orphan).kind is ReductionKind.PARK
        assert len(client.transactions) == 1

    def test_a_root_event_applies_in_one_conditional_transaction(self) -> None:
        client = _RecordingDynamo()
        event = flow_event()

        outcome = _store(client).reduce(event)

        assert outcome.kind is ReductionKind.APPLY
        assert outcome.applied_count == 1
        (transaction,) = client.transactions
        actions = [
            cast("dict[str, Any]", action["Put"])
            for action in cast("list[dict[str, Any]]", transaction["TransactItems"])
        ]
        receipt, applied, history, contribution, snapshot = actions
        assert receipt["Item"]["sort_key"] == {"S": "RECEIPT"}
        assert receipt["Item"]["disposition"] == {"S": "APPLIED"}
        assert applied["Item"]["partition_key"] == {"S": f"EVENT#{event.event_id}"}
        assert applied["Item"]["sort_key"] == {"S": "APPLIED"}
        assert history["Item"]["sort_key"] == {"S": f"APPLIED#{1:020d}"}
        assert history["Item"]["applied_flow_id"] == {"S": event.flow_id}
        assert history["Item"]["applied_sequence"] == {"N": "1"}
        assert contribution["Item"]["surface_partition"] == {"S": event.surface}
        assert contribution["Item"]["surface_sort"] == {
            "S": f"{NOW.isoformat()}#{event.flow_id}#{1:020d}"
        }
        for action in (receipt, applied, history, contribution):
            assert action["ConditionExpression"] == NEW_ITEM
        assert snapshot["Item"]["partition_key"] == {"S": f"FLOW#{event.flow_id}"}
        assert snapshot["Item"]["sort_key"] == {"S": "SNAPSHOT"}
        assert snapshot["ConditionExpression"] == NEW_ITEM

    def test_an_applied_event_arriving_again_is_already_applied(self) -> None:
        client = _RecordingDynamo()
        store = _store(client)
        event = flow_event()

        assert store.reduce(event).kind is ReductionKind.APPLY
        again = store.reduce(event)

        assert again.kind is ReductionKind.ALREADY_APPLIED
        assert len(client.transactions) == 1


def _payload(event: OperationalDataFlowEvent) -> str:
    return json.dumps(event_document(event), sort_keys=True, separators=(",", ":"))
