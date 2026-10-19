# SPDX-License-Identifier: MIT-0
"""Conditional DynamoDB projection for operational data-flow observations."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast

import boto3
from botocore.exceptions import ClientError

from adapters.outbound.dynamodb.items import ItemError, read_text, write_int, write_text
from adapters.outbound.dynamodb.metric_store import DynamoMetricStore
from adapters.outbound.sqs.operational_status import (
    surface_observation_document,
    usage_contribution_document,
    usage_evidence_disposition_document,
)
from domain.operations.graph_projection import (
    GRAPH_ROOT_PREFIX,
    GraphProjectionConflictError,
    GraphProjectionRow,
    ProjectionKind,
)
from domain.operations.measurement import (
    Measurement,
    claim_measurement,
    node_measurement,
    receipt_measurement,
)
from domain.operations.stage1_operational_status import (
    ChildOutcomeV2,
    GenerationHeadDecisionKind,
    GenerationHeadDecisionRequest,
    LatestSelectorDecisionKind,
    LatestSelectorDecisionRequest,
    OperationalDataFlowUsageContribution,
    OperationalDataFlowUsageEvidenceDisposition,
    PlanReleaseDecisionKind,
    PlanReleaseRequest,
    Stage1Surface,
    decide_generation_head,
    decide_latest_selector,
    decide_plan_release,
)
from domain.operations.status import (
    BaselineWindow,
    CompletionOutcome,
    FlowSnapshot,
    FlowState,
    OperationalDataFlowEvent,
    OperationalStatusError,
    ReductionKind,
    event_document,
    event_from_document,
    reduce_event,
)
from ports.outbound.operational_status import (
    EventPage,
    OperationalDataFlowSurfaceObservation,
    OperationalStatusMessage,
    PendingPage,
    PlanTerminalProjectionRequest,
    ProjectionOutcome,
)

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import (
        AttributeValueTypeDef,
        PutTypeDef,
        TransactWriteItemTypeDef,
    )

APPLIED_EVENT_INDEX: Final = "applied-event"
PENDING_INDEX: Final = "pending"
SURFACE_INDEX: Final = "surface-sample-baseline"
MAX_TRANSACTION_ACTIONS: Final = 25
MAX_TRANSACTION_BYTES: Final = 350_000
MAX_DRAIN_EVENTS: Final = 5
MAX_QUERY_LIMIT: Final = 100
MAX_CURSOR_LENGTH: Final = 512
MAX_WINDOW_CONTRIBUTIONS: Final = 1000
MAX_BASELINE_WINDOWS: Final = 48
_CONDITIONAL = "ConditionalCheckFailedException"
_NEW_ITEM_CONDITION: Final = (
    "attribute_not_exists(partition_key) AND attribute_not_exists(sort_key)"
)
_TRANSACTION_CANCELLED = "TransactionCanceledException"
type Item = dict[str, "AttributeValueTypeDef"]


@dataclass(frozen=True, slots=True)
class _AwaitingPlanAuthority:
    request: PlanTerminalProjectionRequest
    receipt_key: Item
    awaiting_key: Item
    event_payload: str
    expectations_payload: str


class DynamoOperationalStatusStore:
    """Persist receipt plus applied/pending disposition atomically with no receive-only state."""

    def __init__(self, table: str, client: DynamoDBClient | None = None) -> None:
        if not table.strip():
            raise ValueError("operational status table is required")
        self._table = table
        self._client = client or boto3.client("dynamodb")

    def reduce(self, message: OperationalStatusMessage) -> ProjectionOutcome:
        """Dispatch the closed queue union through one active v1 projection path."""
        if isinstance(message, OperationalDataFlowEvent):
            return self._reduce_event(message)
        if isinstance(message, OperationalDataFlowSurfaceObservation):
            return self._reduce_surface_observation(message)
        if isinstance(message, OperationalDataFlowUsageContribution):
            return self._reduce_usage_contribution(message)
        return self._reduce_usage_disposition(message)

    def _reduce_event(self, event: OperationalDataFlowEvent) -> ProjectionOutcome:
        """Classify one lifecycle event by consistent readback and commit conditionally."""
        receipt = self._get(_event_key(event.event_id, "RECEIPT"))
        if receipt is not None:
            stored = event_from_document(_payload(receipt))
            if stored != event:
                raise OperationalStatusError("event id already names different immutable evidence")
            if self._get(_event_key(event.event_id, "APPLIED")) is not None:
                return ProjectionOutcome(ReductionKind.ALREADY_APPLIED)
        snapshot_event = self._snapshot_event(event.flow_id)
        decision = reduce_event(
            None if snapshot_event is None else FlowSnapshot.from_event(snapshot_event), event
        )
        if decision.kind is ReductionKind.ALREADY_APPLIED:
            return ProjectionOutcome(ReductionKind.ALREADY_APPLIED)
        if decision.kind is ReductionKind.CONFLICT:
            raise OperationalStatusError(decision.reason)
        if decision.kind is ReductionKind.PARK:
            if receipt is not None:
                return ProjectionOutcome(ReductionKind.PARK)
            self._park(event)
            return ProjectionOutcome(ReductionKind.PARK)
        return self._apply_contiguous(event, snapshot_event, receipt is not None)

    def _reduce_surface_observation(
        self, observation: OperationalDataFlowSurfaceObservation
    ) -> ProjectionOutcome:
        document = surface_observation_document(observation)
        payload = _json(document)
        receipt_key = _observation_key(observation.observation_id, "RECEIPT")
        receipt = self._get(receipt_key)
        if receipt is not None:
            if _payload(receipt) != document:
                raise OperationalStatusError(
                    "observation id already names different immutable evidence"
                )
            return ProjectionOutcome(ReductionKind.ALREADY_APPLIED)
        latest_key = _status_observation_key(observation.surface, "LATEST")
        latest = self._get(latest_key)
        latest_sequence = None if latest is None else _required_int(latest, "source_sequence")
        if latest_sequence is not None and observation.source_sequence <= latest_sequence:
            raise OperationalStatusError("surface observation source sequence did not advance")
        latest_put: dict[str, object] = {
            "TableName": self._table,
            "Item": {
                **latest_key,
                "source_sequence": write_int(observation.source_sequence),
                "observation_id": write_text(observation.observation_id),
                "payload": write_text(payload),
            },
        }
        if latest is None:
            latest_put["ConditionExpression"] = _NEW_ITEM_CONDITION
        else:
            latest_put["ConditionExpression"] = "source_sequence = :expected_sequence"
            latest_put["ExpressionAttributeValues"] = {
                ":expected_sequence": write_int(cast("int", latest_sequence))
            }
        transaction: list[TransactWriteItemTypeDef] = [
            {
                "Put": {
                    "TableName": self._table,
                    "Item": {**receipt_key, "payload": write_text(payload)},
                    "ConditionExpression": _NEW_ITEM_CONDITION,
                }
            },
            {"Put": cast("Any", latest_put)},
        ]
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as error:
            if _conditional(error):
                replay = self._get(receipt_key)
                if replay is not None and _payload(replay) == document:
                    return ProjectionOutcome(ReductionKind.ALREADY_APPLIED)
                raise OperationalStatusError("conditional surface-observation conflict") from None
            raise
        return ProjectionOutcome(ReductionKind.APPLY, applied_count=1)

    def _reduce_usage_contribution(
        self, contribution: OperationalDataFlowUsageContribution
    ) -> ProjectionOutcome:
        document = usage_contribution_document(contribution)
        return self._reduce_usage(
            identity=contribution.contribution_id,
            record_kind="CONTRIBUTION",
            flow_id=contribution.flow_id,
            category=contribution.category.value,
            query_count=contribution.usage.query_count,
            scanned_bytes=contribution.usage.scanned_bytes,
            engine_ms=contribution.usage.engine_ms,
            document=document,
        )

    def _reduce_usage_disposition(
        self, disposition: OperationalDataFlowUsageEvidenceDisposition
    ) -> ProjectionOutcome:
        document = usage_evidence_disposition_document(disposition)
        return self._reduce_usage(
            identity=disposition.disposition_id,
            record_kind="DISPOSITION",
            flow_id=disposition.flow_id,
            category=disposition.category.value,
            query_count=disposition.known_usage.query_count,
            scanned_bytes=disposition.known_usage.scanned_bytes,
            engine_ms=disposition.known_usage.engine_ms,
            document=document,
            missing_quantities=disposition.missing_quantities,
        )

    def _reduce_usage(  # noqa: PLR0913 - exact closed projection members
        self,
        *,
        identity: str,
        record_kind: str,
        flow_id: str,
        category: str,
        query_count: int,
        scanned_bytes: int | None,
        engine_ms: int | None,
        document: dict[str, object],
        missing_quantities: tuple[str, ...] = (),
    ) -> ProjectionOutcome:
        receipt_key = _usage_key(identity, record_kind)
        receipt = self._get(receipt_key)
        if receipt is not None:
            if _payload(receipt) != document:
                raise OperationalStatusError(
                    "usage identity already names different immutable evidence"
                )
            return ProjectionOutcome(ReductionKind.ALREADY_APPLIED)
        payload = _json(document)
        values = {
            ":partition": write_text(f"FLOW#{flow_id}"),
            ":sort": write_text("USAGE"),
            ":query_count": write_int(query_count),
            ":scanned_bytes": write_int(scanned_bytes or 0),
            ":engine_ms": write_int(engine_ms or 0),
        }
        aggregate_key = _flow_key(flow_id, "USAGE")
        transaction: list[TransactWriteItemTypeDef] = [
            {
                "Put": {
                    "TableName": self._table,
                    "Item": {**receipt_key, "payload": write_text(payload)},
                    "ConditionExpression": _NEW_ITEM_CONDITION,
                }
            },
            {
                "Update": {
                    "TableName": self._table,
                    "Key": aggregate_key,
                    "UpdateExpression": (
                        f"ADD {category}_athena_query_count :query_count, "
                        f"{category}_athena_scanned_bytes :scanned_bytes, "
                        f"{category}_athena_engine_ms :engine_ms"
                    ),
                    "ConditionExpression": (
                        "attribute_not_exists(partition_key) OR "
                        "(partition_key = :partition AND sort_key = :sort)"
                    ),
                    "ExpressionAttributeValues": values,
                }
            },
        ]
        if record_kind == "DISPOSITION":
            transaction.append(
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": {
                            **_usage_key(identity, "EVIDENCE"),
                            "category": write_text(category),
                            "evidence": write_text("unavailable"),
                            "missing_quantities": {
                                "L": [write_text(value) for value in missing_quantities]
                            },
                            "payload": write_text(payload),
                        },
                        "ConditionExpression": _NEW_ITEM_CONDITION,
                    }
                }
            )
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as error:
            if _conditional(error):
                replay = self._get(receipt_key)
                if replay is not None and _payload(replay) == document:
                    return ProjectionOutcome(ReductionKind.ALREADY_APPLIED)
                raise OperationalStatusError("conditional usage projection conflict") from None
            raise
        return ProjectionOutcome(ReductionKind.APPLY, applied_count=1)

    def project_child_outcome(self, outcome: ChildOutcomeV2) -> ProjectionOutcome:
        """Persist immutable history and fenced generation/latest heads with no v1 path."""
        history_key = _flow_key(outcome.parent_flow_id, outcome.history_sort_key)
        history = self._get(history_key)
        expected = _child_document(outcome)
        if history is not None:
            if _child_payload(history) != expected:
                raise OperationalStatusError("child outcome history slot is already owned")
            if self._child_projection_replay(outcome, expected):
                return ProjectionOutcome(ReductionKind.ALREADY_APPLIED)
            raise OperationalStatusError("child outcome replay is incomplete")

        head_key = _flow_key(outcome.parent_flow_id, outcome.generation_head_sort_key)
        selector_key = _flow_key(outcome.parent_flow_id, outcome.latest_selector_sort_key)
        current_head_item = self._get(head_key)
        current_selector_item = self._get(selector_key)
        current_head = None if current_head_item is None else _child_from_item(current_head_item)
        current_selector = (
            None if current_selector_item is None else _child_from_item(current_selector_item)
        )
        head_decision = decide_generation_head(
            GenerationHeadDecisionRequest(
                outcome,
                current_head,
                self._candidate_is_contiguous(outcome),
            )
        )
        selector_decision = decide_latest_selector(
            LatestSelectorDecisionRequest(outcome, current_selector)
        )
        if head_decision.kind is GenerationHeadDecisionKind.CONFLICT or (
            selector_decision.kind is LatestSelectorDecisionKind.CONFLICT
        ):
            raise OperationalStatusError("child outcome head conflicts with retained authority")

        history_item = {
            **history_key,
            **_child_item_fields(outcome),
            "payload": write_text(_json(expected)),
        }
        transaction: list[TransactWriteItemTypeDef] = [
            {
                "Put": {
                    "TableName": self._table,
                    "Item": history_item,
                    "ConditionExpression": _NEW_ITEM_CONDITION,
                }
            }
        ]
        transaction.append(
            {
                "Put": _child_head_put(
                    self._table,
                    head_key,
                    outcome,
                    current_head_item,
                    outcome.history_sort_key,
                    None,
                )
            }
        )
        if selector_decision.kind in {
            LatestSelectorDecisionKind.CREATE,
            LatestSelectorDecisionKind.ADVANCE,
        }:
            transaction.append(
                {
                    "Put": _child_head_put(
                        self._table,
                        selector_key,
                        outcome,
                        current_selector_item,
                        outcome.history_sort_key,
                        outcome.generation_head_sort_key,
                    )
                }
            )
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as error:
            if _conditional(error):
                if self._child_projection_replay(outcome, expected):
                    return ProjectionOutcome(ReductionKind.ALREADY_APPLIED)
                raise OperationalStatusError("conditional child-outcome conflict") from None
            raise
        return ProjectionOutcome(ReductionKind.APPLY, applied_count=1)

    def _candidate_is_contiguous(self, outcome: ChildOutcomeV2) -> bool:
        applied = self._get(_event_key(outcome.child_event_id, "APPLIED"))
        if applied is None:
            return False
        try:
            event = event_from_document(_payload(applied))
        except OperationalStatusError:
            return False
        return (
            event.surface == Stage1Surface.UNIT.value
            and event.flow_id == outcome.child_flow_id
            and event.parent_flow_id == outcome.parent_flow_id
            and event.event_id == outcome.child_event_id
            and event.transition_sequence == outcome.child_transition_sequence
            and event.execution_id == outcome.child_execution_id
            and event.state is outcome.lifecycle_state
            and event.outcome is outcome.completion_outcome
        )

    def _child_projection_replay(
        self, outcome: ChildOutcomeV2, expected: dict[str, object]
    ) -> bool:
        history = self._get(_flow_key(outcome.parent_flow_id, outcome.history_sort_key))
        head = self._get(_flow_key(outcome.parent_flow_id, outcome.generation_head_sort_key))
        if history is None or head is None:
            return False
        return _child_payload(history) == expected and _child_payload(head) == expected

    def reduce_plan_terminal(  # noqa: PLR0911, PLR0912 - explicit authority replay states
        self, request: PlanTerminalProjectionRequest
    ) -> ProjectionOutcome:
        """Park completed plans or apply other terminals through normal reduction."""
        event = request.event
        if event.state in {FlowState.BLOCKED, FlowState.FAILED, FlowState.SUPERSEDED}:
            return self._reduce_event(event)
        payload = _json(event_document(event))
        expectations_payload = _json(
            {
                "child_outcome_reduction_version": (
                    request.expectations.child_outcome_reduction_version
                ),
                "parent_flow_id": request.expectations.parent_flow_id,
                "required_child_expectations": [
                    {
                        "child_flow_id": expectation.child_flow_id,
                        "owner_generation": expectation.owner_generation,
                    }
                    for expectation in request.expectations.required_child_expectations
                ],
                "optional_child_flow_ids": list(request.expectations.optional_child_flow_ids),
            }
        )
        receipt_key = _event_key(event.event_id, "RECEIPT")
        awaiting_key = _flow_key(
            event.flow_id, f"AWAITING_CHILDREN#{event.transition_sequence:020d}"
        )
        authority = _AwaitingPlanAuthority(
            request,
            receipt_key,
            awaiting_key,
            payload,
            expectations_payload,
        )
        receipt = self._get(receipt_key)
        awaiting = self._get(awaiting_key)
        if receipt is not None:
            try:
                disposition = read_text(receipt, "disposition")
                exact_receipt = (
                    event_from_document(_payload(receipt)) == event
                    and read_text(receipt, "expectation_authority") == expectations_payload
                )
            except OperationalStatusError as error:
                raise OperationalStatusError("plan awaiting receipt is malformed") from error
            if not exact_receipt:
                raise OperationalStatusError("plan awaiting replay differs from exact authority")
            if disposition == "APPLIED":
                applied = self._get(_event_key(event.event_id, "APPLIED"))
                if (
                    awaiting is None
                    and applied is not None
                    and event_from_document(_payload(applied)) == event
                ):
                    return ProjectionOutcome(ReductionKind.ALREADY_APPLIED)
                raise OperationalStatusError("plan awaiting release replay is incomplete")
            if disposition != "AWAITING_CHILDREN" or not self._awaiting_item_matches(
                awaiting, authority
            ):
                raise OperationalStatusError("plan awaiting ownership is incomplete")
        elif awaiting is not None:
            raise OperationalStatusError("plan awaiting ownership has no receipt")

        heads = self._generation_heads(request)
        decision = decide_plan_release(PlanReleaseRequest(event.state, request.expectations, heads))
        if decision.kind is PlanReleaseDecisionKind.RELEASE and receipt is not None:
            return self._release_awaiting_plan(authority, heads)
        if decision.kind is PlanReleaseDecisionKind.AWAIT_CHILDREN and receipt is not None:
            return ProjectionOutcome(ReductionKind.PARK)
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table,
                            "Item": {
                                **receipt_key,
                                "disposition": write_text("AWAITING_CHILDREN"),
                                "expectation_authority": write_text(expectations_payload),
                                "payload": write_text(payload),
                            },
                            "ConditionExpression": _NEW_ITEM_CONDITION,
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table,
                            "Item": {
                                **awaiting_key,
                                "pending_state": write_text("AWAITING_CHILDREN"),
                                "pending_order": write_text(
                                    f"{event.flow_id}#{event.transition_sequence:020d}"
                                ),
                                "event_id": write_text(event.event_id),
                                "expectation_authority": write_text(expectations_payload),
                                "payload": write_text(payload),
                            },
                            "ConditionExpression": _NEW_ITEM_CONDITION,
                        }
                    },
                ]
            )
        except ClientError as error:
            if _conditional(error):
                replay_receipt = self._get(receipt_key)
                replay_awaiting = self._get(awaiting_key)
                if (
                    replay_receipt is not None
                    and read_text(replay_receipt, "disposition") == "AWAITING_CHILDREN"
                    and self._awaiting_item_matches(replay_awaiting, authority)
                    and event_from_document(_payload(replay_receipt)) == event
                    and read_text(replay_receipt, "expectation_authority") == expectations_payload
                ):
                    if decision.kind is PlanReleaseDecisionKind.RELEASE:
                        return self._release_awaiting_plan(authority, heads)
                    return ProjectionOutcome(ReductionKind.PARK)
                raise OperationalStatusError("conditional plan-awaiting conflict") from None
            raise
        if decision.kind is PlanReleaseDecisionKind.RELEASE:
            return self._release_awaiting_plan(authority, heads)
        return ProjectionOutcome(ReductionKind.PARK)

    def _generation_heads(
        self, request: PlanTerminalProjectionRequest
    ) -> tuple[ChildOutcomeV2, ...]:
        keys = [
            {"partition_key": write_text(partition), "sort_key": write_text(sort)}
            for partition, sort in request.expectations.generation_head_keys
        ]
        if not keys:
            return ()
        response = self._client.batch_get_item(
            RequestItems={self._table: {"Keys": keys, "ConsistentRead": True}}
        )
        raw_heads = response.get("Responses", {}).get(self._table, [])
        return tuple(_child_from_item(dict(item)) for item in raw_heads)

    @staticmethod
    def _awaiting_item_matches(item: Item | None, authority: _AwaitingPlanAuthority) -> bool:
        if item is None:
            return False
        event = authority.request.event
        try:
            return (
                read_text(item, "pending_state") == "AWAITING_CHILDREN"
                and read_text(item, "pending_order")
                == f"{event.flow_id}#{event.transition_sequence:020d}"
                and read_text(item, "event_id") == event.event_id
                and read_text(item, "expectation_authority") == authority.expectations_payload
                and read_text(item, "payload") == authority.event_payload
            )
        except ItemError, KeyError, ValueError:
            return False

    def _release_awaiting_plan(
        self,
        authority: _AwaitingPlanAuthority,
        heads: tuple[ChildOutcomeV2, ...],
    ) -> ProjectionOutcome:
        request = authority.request
        receipt_key = authority.receipt_key
        awaiting_key = authority.awaiting_key
        payload = authority.event_payload
        expectations_payload = authority.expectations_payload
        event = request.event
        prior_snapshot = self._snapshot_event(event.flow_id)
        reduction = reduce_event(
            None if prior_snapshot is None else FlowSnapshot.from_event(prior_snapshot), event
        )
        if reduction.kind is ReductionKind.PARK:
            return ProjectionOutcome(ReductionKind.PARK)
        if reduction.kind is not ReductionKind.APPLY:
            raise OperationalStatusError(reduction.reason)
        head_by_key = {
            (head.parent_flow_id, head.child_flow_id, head.owner_generation): head for head in heads
        }
        transaction: list[TransactWriteItemTypeDef] = []
        for expectation in request.expectations.required_child_expectations:
            head = head_by_key[
                (
                    request.expectations.parent_flow_id,
                    expectation.child_flow_id,
                    expectation.owner_generation,
                )
            ]
            transaction.append(
                {
                    "ConditionCheck": {
                        "TableName": self._table,
                        "Key": _flow_key(event.flow_id, head.generation_head_sort_key),
                        "ConditionExpression": "payload = :expected_payload",
                        "ExpressionAttributeValues": {
                            ":expected_payload": write_text(_json(_child_document(head)))
                        },
                    }
                }
            )
        transaction.extend(
            [
                {
                    "Update": {
                        "TableName": self._table,
                        "Key": receipt_key,
                        "UpdateExpression": "SET disposition = :applied",
                        "ConditionExpression": (
                            "disposition = :awaiting AND payload = :event_payload AND "
                            "expectation_authority = :expectations"
                        ),
                        "ExpressionAttributeValues": {
                            ":applied": write_text("APPLIED"),
                            ":awaiting": write_text("AWAITING_CHILDREN"),
                            ":event_payload": write_text(payload),
                            ":expectations": write_text(expectations_payload),
                        },
                    }
                },
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": {
                            **_event_key(event.event_id, "APPLIED"),
                            "payload": write_text(payload),
                        },
                        "ConditionExpression": _NEW_ITEM_CONDITION,
                    }
                },
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": {
                            **_flow_key(event.flow_id, _applied_sort(event.transition_sequence)),
                            "applied_flow_id": write_text(event.flow_id),
                            "applied_sequence": write_int(event.transition_sequence),
                            "payload": write_text(payload),
                        },
                        "ConditionExpression": _NEW_ITEM_CONDITION,
                    }
                },
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": {
                            "partition_key": write_text(f"SURFACE#{event.surface}"),
                            "sort_key": write_text(
                                f"CONTRIBUTION#{event.flow_id}#{event.transition_sequence:020d}"
                            ),
                            "surface_partition": write_text(event.surface),
                            "surface_sort": write_text(
                                f"{event.occurred_at.isoformat()}"
                                f"#{event.flow_id}#{event.transition_sequence:020d}"
                            ),
                            "payload": write_text(payload),
                        },
                        "ConditionExpression": _NEW_ITEM_CONDITION,
                    }
                },
                {
                    "Delete": {
                        "TableName": self._table,
                        "Key": awaiting_key,
                        "ConditionExpression": (
                            "pending_state = :awaiting AND pending_order = :pending_order AND "
                            "event_id = :event_id AND payload = :event_payload AND "
                            "expectation_authority = :expectations"
                        ),
                        "ExpressionAttributeValues": {
                            ":awaiting": write_text("AWAITING_CHILDREN"),
                            ":pending_order": write_text(
                                f"{event.flow_id}#{event.transition_sequence:020d}"
                            ),
                            ":event_id": write_text(event.event_id),
                            ":event_payload": write_text(payload),
                            ":expectations": write_text(expectations_payload),
                        },
                    }
                },
            ]
        )
        snapshot_put: dict[str, object] = {
            "TableName": self._table,
            "Item": {
                **_flow_key(event.flow_id, "SNAPSHOT"),
                "latest_event_id": write_text(event.event_id),
                "latest_sequence": write_int(event.transition_sequence),
                "payload": write_text(payload),
            },
        }
        if prior_snapshot is None:
            snapshot_put["ConditionExpression"] = _NEW_ITEM_CONDITION
        else:
            snapshot_put["ConditionExpression"] = (
                "latest_event_id = :prior_event AND latest_sequence = :prior_sequence"
            )
            snapshot_put["ExpressionAttributeValues"] = {
                ":prior_event": write_text(prior_snapshot.event_id),
                ":prior_sequence": write_int(prior_snapshot.transition_sequence),
            }
        transaction.append({"Put": cast("Any", snapshot_put)})
        if len(transaction) > MAX_TRANSACTION_ACTIONS or not _within_budget([(event, False)]):
            raise OperationalStatusError("plan awaiting release exceeds transaction bounds")
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as error:
            if _conditional(error):
                applied = self._get(_event_key(event.event_id, "APPLIED"))
                receipt = self._get(receipt_key)
                awaiting = self._get(awaiting_key)
                if (
                    applied is not None
                    and event_from_document(_payload(applied)) == event
                    and receipt is not None
                    and read_text(receipt, "disposition") == "APPLIED"
                    and read_text(receipt, "expectation_authority") == expectations_payload
                    and awaiting is None
                ):
                    return ProjectionOutcome(ReductionKind.ALREADY_APPLIED)
                raise OperationalStatusError("conditional plan-awaiting release conflict") from None
            raise
        return ProjectionOutcome(ReductionKind.APPLY, applied_count=1)

    def pending(self, limit: int) -> PendingPage:
        """Query only the live pending index; historical receipts cannot consume the bound."""
        bounded = _limit(limit)
        response = self._client.query(
            TableName=self._table,
            IndexName=PENDING_INDEX,
            KeyConditionExpression="pending_state = :pending",
            ExpressionAttributeValues={":pending": write_text("PENDING")},
            Limit=bounded,
            ScanIndexForward=True,
        )
        events = tuple(
            event_from_document(_payload(dict(item))) for item in response.get("Items", [])
        )
        return PendingPage(events)

    def flow(self, flow_id: str) -> dict[str, object] | None:
        """Read one exact snapshot consistently and return its closed event projection."""
        event = self._snapshot_event(flow_id)
        return None if event is None else event_document(event)

    def events(self, flow_id: str, cursor: str, limit: int) -> EventPage | None:
        """Query only APPLIED history and validate the opaque cursor is flow-bound."""
        after = decode_cursor(cursor, flow_id)
        if after is None:
            return None
        bounded = _limit(limit)
        response = self._client.query(
            TableName=self._table,
            IndexName=APPLIED_EVENT_INDEX,
            KeyConditionExpression="applied_flow_id = :flow AND applied_sequence > :after",
            ExpressionAttributeValues={
                ":flow": write_text(flow_id),
                ":after": write_int(after),
            },
            Limit=bounded,
            ScanIndexForward=True,
        )
        found = tuple(
            event_from_document(_payload(dict(item))) for item in response.get("Items", [])
        )
        next_cursor = (
            encode_cursor(flow_id, found[-1].transition_sequence) if len(found) == bounded else None
        )
        return EventPage(found, next_cursor)

    def surface(self, surface: str, limit: int) -> tuple[dict[str, object], ...]:
        """Query a bounded low-cardinality surface contribution lane; never scan."""
        response = self._client.query(
            TableName=self._table,
            IndexName=SURFACE_INDEX,
            KeyConditionExpression="surface_partition = :surface",
            ExpressionAttributeValues={":surface": write_text(surface)},
            Limit=_limit(limit),
            ScanIndexForward=False,
        )
        return tuple(
            event_document(event_from_document(_payload(dict(item))))
            for item in response.get("Items", [])
        )

    def window_contributions(
        self, surface: str, window_start: datetime, window_end: datetime
    ) -> tuple[OperationalDataFlowEvent, ...]:
        """Query one closed window's contributions in occurrence order through the GSI."""
        response = self._client.query(
            TableName=self._table,
            IndexName=SURFACE_INDEX,
            KeyConditionExpression=(
                "surface_partition = :surface AND surface_sort BETWEEN :start AND :end"
            ),
            ExpressionAttributeValues={
                ":surface": write_text(surface),
                ":start": write_text(window_start.isoformat()),
                ":end": write_text(f"{window_end.isoformat()}#\uffff"),
            },
            Limit=MAX_WINDOW_CONTRIBUTIONS,
            ScanIndexForward=True,
        )
        return tuple(
            event_from_document(_payload(dict(item))) for item in response.get("Items", [])
        )

    def baselines(self, surface: str, measurement_ref: str) -> tuple[BaselineWindow, ...]:
        """Read this surface's descriptive baseline windows, newest window first."""
        response = self._client.query(
            TableName=self._table,
            KeyConditionExpression="partition_key = :surface AND begins_with(sort_key, :prefix)",
            ExpressionAttributeValues={
                ":surface": write_text(f"SURFACE#{surface}"),
                ":prefix": write_text(f"BASELINE#{measurement_ref}#"),
            },
            Limit=MAX_BASELINE_WINDOWS,
            ScanIndexForward=False,
            ConsistentRead=True,
        )
        return tuple(_baseline_from(_payload(dict(item))) for item in response.get("Items", []))

    def record_baseline(self, surface: str, window: BaselineWindow) -> bool:
        """Create one immutable baseline revision; a lost create means it already exists."""
        try:
            self._client.put_item(
                TableName=self._table,
                Item={
                    "partition_key": write_text(f"SURFACE#{surface}"),
                    "sort_key": write_text(_baseline_sort(window)),
                    "payload": write_text(_json(_baseline_document(window))),
                },
                ConditionExpression=_NEW_ITEM_CONDITION,
            )
        except ClientError as error:
            if _conditional(error):
                return False
            raise
        return True

    def _snapshot_event(self, flow_id: str) -> OperationalDataFlowEvent | None:
        item = self._get(_flow_key(flow_id, "SNAPSHOT"))
        return None if item is None else event_from_document(_payload(item))

    def _get(self, key: Item) -> Item | None:
        response = self._client.get_item(
            TableName=self._table,
            Key=key,
            ConsistentRead=True,
        )
        item = response.get("Item")
        return None if item is None else dict(item)

    def _park(self, event: OperationalDataFlowEvent) -> None:
        payload = _json(event_document(event))
        pending_key = _flow_key(event.flow_id, _pending_sort(event.transition_sequence))
        transaction: list[TransactWriteItemTypeDef] = [
            {
                "Put": {
                    "TableName": self._table,
                    "Item": {
                        **_event_key(event.event_id, "RECEIPT"),
                        "disposition": write_text("PENDING"),
                        "payload": write_text(payload),
                    },
                    "ConditionExpression": _NEW_ITEM_CONDITION,
                }
            },
            {
                "Put": {
                    "TableName": self._table,
                    "Item": {
                        **pending_key,
                        "pending_state": write_text("PENDING"),
                        "pending_order": write_text(
                            f"{event.flow_id}#{event.transition_sequence:020d}"
                        ),
                        "event_id": write_text(event.event_id),
                        "payload": write_text(payload),
                    },
                    "ConditionExpression": _NEW_ITEM_CONDITION,
                }
            },
        ]
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as error:
            if _conditional(error):
                existing = self._get(_event_key(event.event_id, "RECEIPT"))
                if existing is not None and event_from_document(_payload(existing)) == event:
                    return
            raise

    def _apply_contiguous(
        self,
        first: OperationalDataFlowEvent,
        prior_snapshot: OperationalDataFlowEvent | None,
        first_was_pending: bool,
    ) -> ProjectionOutcome:
        candidates: list[tuple[OperationalDataFlowEvent, bool]] = [(first, first_was_pending)]
        pending_response = self._client.query(
            TableName=self._table,
            KeyConditionExpression="partition_key = :flow AND sort_key BETWEEN :first AND :last",
            ExpressionAttributeValues={
                ":flow": write_text(f"FLOW#{first.flow_id}"),
                ":first": write_text(_pending_sort(first.transition_sequence + 1)),
                ":last": write_text(_pending_sort(first.transition_sequence + MAX_DRAIN_EVENTS)),
            },
            ConsistentRead=True,
            Limit=MAX_DRAIN_EVENTS - 1,
            ScanIndexForward=True,
        )
        cursor = FlowSnapshot.from_event(first)
        for raw_item in pending_response.get("Items", []):
            pending_event = event_from_document(_payload(dict(raw_item)))
            decision = reduce_event(cursor, pending_event)
            if decision.kind is not ReductionKind.APPLY or decision.snapshot is None:
                break
            candidates.append((pending_event, True))
            cursor = decision.snapshot
        while len(candidates) > 1 and not _within_budget(candidates):
            candidates.pop()
        if not _within_budget(candidates):
            raise OperationalStatusError("one operational event exceeds transaction bounds")
        transaction: list[TransactWriteItemTypeDef] = []
        for event, was_pending in candidates:
            transaction.extend(self._candidate_actions(event, was_pending))
        final_event = candidates[-1][0]
        snapshot_put: dict[str, object] = {
            "TableName": self._table,
            "Item": {
                **_flow_key(first.flow_id, "SNAPSHOT"),
                "latest_event_id": write_text(final_event.event_id),
                "latest_sequence": write_int(final_event.transition_sequence),
                "payload": write_text(_json(event_document(final_event))),
            },
        }
        if prior_snapshot is None:
            snapshot_put["ConditionExpression"] = (
                "attribute_not_exists(partition_key) AND attribute_not_exists(sort_key)"
            )
        else:
            snapshot_put["ConditionExpression"] = (
                "latest_event_id = :prior_event AND latest_sequence = :prior_sequence"
            )
            snapshot_put["ExpressionAttributeValues"] = {
                ":prior_event": write_text(prior_snapshot.event_id),
                ":prior_sequence": write_int(prior_snapshot.transition_sequence),
            }
        transaction.append({"Put": cast("Any", snapshot_put)})
        if len(transaction) > MAX_TRANSACTION_ACTIONS:
            raise OperationalStatusError("operational reduction exceeds transaction action bound")
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as error:
            if _conditional(error):
                return self._conflict_outcome(first)
            raise
        return ProjectionOutcome(ReductionKind.APPLY, applied_count=len(candidates))

    def _candidate_actions(
        self, event: OperationalDataFlowEvent, was_pending: bool
    ) -> list[TransactWriteItemTypeDef]:
        payload = _json(event_document(event))
        actions: list[TransactWriteItemTypeDef] = []
        if not was_pending:
            actions.append(
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": {
                            **_event_key(event.event_id, "RECEIPT"),
                            "disposition": write_text("APPLIED"),
                            "payload": write_text(payload),
                        },
                        "ConditionExpression": _NEW_ITEM_CONDITION,
                    }
                }
            )
        actions.extend(
            [
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": {
                            **_event_key(event.event_id, "APPLIED"),
                            "payload": write_text(payload),
                        },
                        "ConditionExpression": _NEW_ITEM_CONDITION,
                    }
                },
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": {
                            **_flow_key(event.flow_id, _applied_sort(event.transition_sequence)),
                            "applied_flow_id": write_text(event.flow_id),
                            "applied_sequence": write_int(event.transition_sequence),
                            "payload": write_text(payload),
                        },
                        "ConditionExpression": _NEW_ITEM_CONDITION,
                    }
                },
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": {
                            "partition_key": write_text(f"SURFACE#{event.surface}"),
                            "sort_key": write_text(
                                f"CONTRIBUTION#{event.flow_id}#{event.transition_sequence:020d}"
                            ),
                            "surface_partition": write_text(event.surface),
                            "surface_sort": write_text(
                                f"{event.occurred_at.isoformat()}"
                                f"#{event.flow_id}#{event.transition_sequence:020d}"
                            ),
                            "payload": write_text(payload),
                        },
                        "ConditionExpression": _NEW_ITEM_CONDITION,
                    }
                },
            ]
        )
        if was_pending:
            actions.append(
                {
                    "Delete": {
                        "TableName": self._table,
                        "Key": _flow_key(event.flow_id, _pending_sort(event.transition_sequence)),
                        "ConditionExpression": "event_id = :event_id",
                        "ExpressionAttributeValues": {":event_id": write_text(event.event_id)},
                    }
                }
            )
        return actions

    def _conflict_outcome(self, first: OperationalDataFlowEvent) -> ProjectionOutcome:
        applied = self._get(_event_key(first.event_id, "APPLIED"))
        if applied is not None and event_from_document(_payload(applied)) == first:
            return ProjectionOutcome(ReductionKind.ALREADY_APPLIED)
        snapshot = self._snapshot_event(first.flow_id)
        decision = reduce_event(
            None if snapshot is None else FlowSnapshot.from_event(snapshot), first
        )
        if decision.kind is ReductionKind.PARK:
            return ProjectionOutcome(ReductionKind.PARK)
        raise OperationalStatusError("conditional projection conflict") from None


def _observation_key(observation_id: str, sort_key: str) -> Item:
    return {
        "partition_key": write_text(f"OBSERVATION#{observation_id}"),
        "sort_key": write_text(sort_key),
    }


def _status_observation_key(surface: str, sort_key: str) -> Item:
    return {
        "partition_key": write_text(f"STATUS_OBSERVATION#{surface}"),
        "sort_key": write_text(sort_key),
    }


def _usage_key(identity: str, sort_key: str) -> Item:
    return {
        "partition_key": write_text(f"USAGE#{identity}"),
        "sort_key": write_text(sort_key),
    }


def _required_int(item: Item, name: str) -> int:
    value = item.get(name, {}).get("N")
    if value is None:
        raise OperationalStatusError(f"stored operational {name} is absent")
    try:
        return int(value)
    except ValueError as error:
        raise OperationalStatusError(f"stored operational {name} is invalid") from error


def _child_document(outcome: ChildOutcomeV2) -> dict[str, object]:
    return {
        "child_outcome_reduction_version": outcome.child_outcome_reduction_version,
        "parent_flow_id": outcome.parent_flow_id,
        "child_flow_id": outcome.child_flow_id,
        "child_event_id": outcome.child_event_id,
        "child_transition_sequence": outcome.child_transition_sequence,
        "child_execution_id": outcome.child_execution_id,
        "owner_generation": outcome.owner_generation,
        "lifecycle_state": outcome.lifecycle_state.value,
        "completion_outcome": outcome.completion_outcome.value,
        "outcome_digest": outcome.outcome_digest,
    }


def _child_item_fields(outcome: ChildOutcomeV2) -> Item:
    return {
        "child_outcome_reduction_version": write_int(outcome.child_outcome_reduction_version),
        "parent_flow_id": write_text(outcome.parent_flow_id),
        "child_flow_id": write_text(outcome.child_flow_id),
        "child_event_id": write_text(outcome.child_event_id),
        "child_transition_sequence": write_int(outcome.child_transition_sequence),
        "child_execution_id": write_text(outcome.child_execution_id),
        "owner_generation": write_int(outcome.owner_generation),
        "lifecycle_state": write_text(outcome.lifecycle_state.value),
        "completion_outcome": write_text(outcome.completion_outcome.value),
        "outcome_digest": write_text(outcome.outcome_digest),
    }


def _child_payload(item: Item) -> dict[str, object]:
    value = _payload(item)
    if not isinstance(value, dict):
        raise OperationalStatusError("stored child outcome payload is invalid")
    return cast("dict[str, object]", value)


def _child_from_item(item: Item) -> ChildOutcomeV2:
    document = _child_payload(item)
    expected = frozenset(
        {
            "child_outcome_reduction_version",
            "parent_flow_id",
            "child_flow_id",
            "child_event_id",
            "child_transition_sequence",
            "child_execution_id",
            "owner_generation",
            "lifecycle_state",
            "completion_outcome",
            "outcome_digest",
        }
    )
    if frozenset(document) != expected:
        raise OperationalStatusError("stored child outcome fields differ from v2")
    outcome = ChildOutcomeV2(
        parent_flow_id=str(document["parent_flow_id"]),
        child_flow_id=str(document["child_flow_id"]),
        child_event_id=str(document["child_event_id"]),
        child_transition_sequence=_document_int(document["child_transition_sequence"]),
        child_execution_id=str(document["child_execution_id"]),
        owner_generation=_document_int(document["owner_generation"]),
        lifecycle_state=FlowState(str(document["lifecycle_state"])),
        completion_outcome=CompletionOutcome(str(document["completion_outcome"])),
        child_outcome_reduction_version=_document_int(document["child_outcome_reduction_version"]),
    )
    if outcome.outcome_digest != document["outcome_digest"]:
        raise OperationalStatusError("stored child outcome digest differs from authority")
    return outcome


def _document_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperationalStatusError("stored child outcome integer is invalid")
    return value


def _child_head_put(  # noqa: PLR0913, PLR0917 - private conditional builder
    table: str,
    key: Item,
    outcome: ChildOutcomeV2,
    current: Item | None,
    history_sort_key: str,
    generation_head_sort_key: str | None,
) -> PutTypeDef:
    item: Item = {
        **key,
        **_child_item_fields(outcome),
        "history_sort_key": write_text(history_sort_key),
        "payload": write_text(_json(_child_document(outcome))),
    }
    if generation_head_sort_key is not None:
        item["generation_head_sort_key"] = write_text(generation_head_sort_key)
    request: PutTypeDef = {"TableName": table, "Item": item}
    if current is None:
        request["ConditionExpression"] = _NEW_ITEM_CONDITION
        return request
    current_outcome = _child_from_item(current)
    request["ConditionExpression"] = (
        "child_outcome_reduction_version = :old_version AND "
        "parent_flow_id = :old_parent AND child_flow_id = :old_child AND "
        "child_event_id = :old_event AND child_transition_sequence = :old_sequence AND "
        "child_execution_id = :old_execution AND owner_generation = :old_generation AND "
        "lifecycle_state = :old_state AND completion_outcome = :old_outcome AND "
        "outcome_digest = :old_digest"
    )
    request["ExpressionAttributeValues"] = {
        ":old_version": write_int(current_outcome.child_outcome_reduction_version),
        ":old_parent": write_text(current_outcome.parent_flow_id),
        ":old_child": write_text(current_outcome.child_flow_id),
        ":old_event": write_text(current_outcome.child_event_id),
        ":old_sequence": write_int(current_outcome.child_transition_sequence),
        ":old_execution": write_text(current_outcome.child_execution_id),
        ":old_generation": write_int(current_outcome.owner_generation),
        ":old_state": write_text(current_outcome.lifecycle_state.value),
        ":old_outcome": write_text(current_outcome.completion_outcome.value),
        ":old_digest": write_text(current_outcome.outcome_digest),
    }
    return request


def encode_cursor(flow_id: str, after_sequence: int) -> str:
    """Issue a canonical URL-safe opaque continuation bound to one flow."""
    document = _json({"after": after_sequence, "flow": flow_id, "version": 1}).encode()
    return base64.urlsafe_b64encode(document).decode().rstrip("=")


def decode_cursor(cursor: str, flow_id: str) -> int | None:
    """Return a cursor position only for the exact canonical server-issued encoding."""
    if not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        value: object = json.loads(raw)
    except ValueError, UnicodeError, json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    document = cast("dict[str, object]", value)
    after = document.get("after")
    if (
        frozenset(document) != frozenset({"after", "flow", "version"})
        or document.get("flow") != flow_id
        or document.get("version") != 1
        or isinstance(after, bool)
        or not isinstance(after, int)
        or after < 0
    ):
        return None
    if encode_cursor(flow_id, after) != cursor:
        return None
    return after


def _flow_key(flow_id: str, sort_key: str) -> Item:
    return {
        "partition_key": write_text(f"FLOW#{flow_id}"),
        "sort_key": write_text(sort_key),
    }


def _event_key(event_id: str, sort_key: str) -> Item:
    return {
        "partition_key": write_text(f"EVENT#{event_id}"),
        "sort_key": write_text(sort_key),
    }


def _baseline_sort(window: BaselineWindow) -> str:
    return (
        f"BASELINE#{window.measurement_ref}#{window.window_start.isoformat()}"
        f"#R{window.revision:03d}"
    )


def _baseline_document(window: BaselineWindow) -> dict[str, object]:
    return {
        "window_id": window.window_id,
        "predecessor_window_id": window.predecessor_window_id,
        "revision": window.revision,
        "measurement_ref": window.measurement_ref,
        "measurement_version": window.measurement_version,
        "window_start": window.window_start.isoformat(),
        "window_end": window.window_end.isoformat(),
        "population_cutoff": window.population_cutoff.isoformat(),
        "finalized_at": window.finalized_at.isoformat(),
        "completeness": window.completeness,
        "late_sample_count": window.late_sample_count,
        "sample_count": window.sample_count,
    }


def _baseline_from(value: object) -> BaselineWindow:
    if not isinstance(value, dict):
        raise OperationalStatusError("stored baseline window is invalid")
    document = cast("dict[str, Any]", value)
    return BaselineWindow(
        window_id=str(document["window_id"]),
        predecessor_window_id=(
            None
            if document["predecessor_window_id"] is None
            else str(document["predecessor_window_id"])
        ),
        revision=int(document["revision"]),
        measurement_ref=str(document["measurement_ref"]),
        measurement_version=str(document["measurement_version"]),
        window_start=datetime.fromisoformat(str(document["window_start"])),
        window_end=datetime.fromisoformat(str(document["window_end"])),
        population_cutoff=datetime.fromisoformat(str(document["population_cutoff"])),
        finalized_at=datetime.fromisoformat(str(document["finalized_at"])),
        completeness=str(document["completeness"]),
        late_sample_count=int(document["late_sample_count"]),
        sample_count=int(document["sample_count"]),
    )


def _pending_sort(sequence: int) -> str:
    return f"PENDING#{sequence:020d}"


def _applied_sort(sequence: int) -> str:
    return f"APPLIED#{sequence:020d}"


def _payload(item: Item) -> object:
    try:
        value: object = json.loads(read_text(item, "payload"))
    except json.JSONDecodeError as error:
        raise OperationalStatusError("stored operational payload is invalid") from error
    return value


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _limit(value: int) -> int:
    if isinstance(value, bool) or not 1 <= value <= MAX_QUERY_LIMIT:
        raise OperationalStatusError(f"query limit must be between 1 and {MAX_QUERY_LIMIT}")
    return value


def _within_budget(events: list[tuple[OperationalDataFlowEvent, bool]]) -> bool:
    actions = 1 + sum(4 if pending else 3 for _, pending in events)
    encoded = sum(len(_json(event_document(event)).encode()) for event, _ in events)
    return actions <= MAX_TRANSACTION_ACTIONS and encoded <= MAX_TRANSACTION_BYTES


def _conditional(error: ClientError) -> bool:
    code = error.response.get("Error", {}).get("Code")
    if code == _CONDITIONAL:
        return True
    if code != _TRANSACTION_CANCELLED:
        return False
    return any(
        reason.get("Code") == "ConditionalCheckFailed"
        for reason in error.response.get("CancellationReasons", [])
    )


# Status v2 (S2): execution-graph events projected under their root flow. Rows are immutable
# copies of graph authority fields (`domain.operations.graph_projection`); the explanation
# re-reads the authority they name. This store never touches the execution-graph table.
_V2_SCHEMA: Final = "status-v2"
_V2_ITEM_KIND: Final = "execution_projection"
_V2_MAX_QUERY_PAGES: Final = 8
_EXECUTION_PREFIXES: Final = ("NODE#", "CLAIM#", "SERVING#")


def decode_attribute(value: AttributeValueTypeDef) -> object:  # noqa: PLR0911 -- one return per type
    """Decode one DynamoDB attribute into plain text, integers, booleans and lists.

    A string set becomes a sorted list of text: DynamoDB returns set members in no fixed order,
    and the bake's claim progress (`bake_done`, `bake_unavailable`) is one.
    """
    attribute = cast("dict[str, Any]", value)
    if "S" in attribute:
        return str(attribute["S"])
    if "SS" in attribute:
        return sorted(str(entry) for entry in attribute["SS"])
    if "N" in attribute:
        return int(str(attribute["N"]))
    if "BOOL" in attribute:
        return bool(attribute["BOOL"])
    if "L" in attribute:
        return [decode_attribute(entry) for entry in attribute["L"]]
    if "M" in attribute:
        mapping = cast("dict[str, AttributeValueTypeDef]", attribute["M"])
        return {name: decode_attribute(entry) for name, entry in mapping.items()}
    if "NULL" in attribute:
        return None
    raise OperationalStatusError("a graph item carries an attribute type S2 does not project")


def decode_item(item: Item) -> dict[str, object]:
    """Decode a whole DynamoDB item (a stream image or a read) into plain values."""
    return {name: decode_attribute(value) for name, value in item.items()}


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _node_contribution(row: GraphProjectionRow) -> Measurement | None:
    """S6 §2.10: one `FlowTransitionLatency` per terminal node, in the NODE row's commit."""
    document = row.document
    return node_measurement(
        authority_key=f"{row.authority_pk}/{row.authority_sk}",
        authority_epoch=row.authority_epoch,
        node=str(document["node"]),
        state=str(document["state"]),
        armed_at=_optional_text(document.get("armed_at")),
        settled_at=_optional_text(document.get("settled_at")),
    )


def _claim_contribution(row: GraphProjectionRow) -> Measurement | None:
    """S6 §2.10: one `ActivityAdmissionElapsed` per admission, in the CLAIM row's commit."""
    document = row.document
    node, admission = document.get("node"), document.get("admission")
    if not isinstance(node, str) or not isinstance(admission, int):
        return None
    return claim_measurement(
        authority_key=f"{row.authority_pk}/{row.authority_sk}",
        authority_epoch=row.authority_epoch,
        node=node,
        admission=admission,
        admitted_at=_optional_text(document.get("admitted_at")),
        queue_enqueued_at=_optional_text(document.get("queue_enqueued_at")),
    )


def _receipt_contribution(row: GraphProjectionRow) -> Measurement | None:
    """S5: one `FlowRootElapsed` per receipt that carries its root clock."""
    document = row.document
    clock = document.get("root_clock_started_at")
    decision, recorded_at = document.get("decision"), document.get("recorded_at")
    if not isinstance(clock, str) or not isinstance(decision, str):
        return None
    if not isinstance(recorded_at, str):
        return None
    return receipt_measurement(
        authority_key=f"{row.authority_pk}/{row.authority_sk}",
        authority_epoch=row.authority_epoch,
        decision=decision,
        recorded_at=recorded_at,
        root_clock_started_at=clock,
    )


type _Producer = Callable[[GraphProjectionRow], Measurement | None]
_CONTRIBUTIONS: Final[Mapping[ProjectionKind, _Producer]] = {
    ProjectionKind.RECEIPT: _receipt_contribution,
    ProjectionKind.NODE: _node_contribution,
    ProjectionKind.CLAIM: _claim_contribution,
}
"""The measured projection kinds (FR-3); every other kind's row measures nothing."""


def row_measurement(row: GraphProjectionRow) -> Measurement | None:
    """Return the contribution one projection row commits with it, or None (FR-3, S6 §2.10).

    The S6 gate probe derives the expected measurement identities from projected rows with it.
    """
    producer = _CONTRIBUTIONS.get(row.kind)
    return None if producer is None else producer(row)


def _v2_row(item: Item) -> GraphProjectionRow:
    document = _payload(item)
    if not isinstance(document, dict):
        raise OperationalStatusError("stored v2 projection payload is not an object")
    row = GraphProjectionRow(
        root_flow_id=read_text(item, "root_flow_id"),
        sort_key=read_text(item, "sort_key"),
        kind=ProjectionKind(read_text(item, "projection_kind")),
        authority_epoch=int(str(cast("dict[str, Any]", item["authority_epoch"])["N"])),
        authority_pk=read_text(item, "authority_pk"),
        authority_sk=read_text(item, "authority_sk"),
        document=cast("dict[str, object]", document),
    )
    if row.digest != read_text(item, "authority_digest"):
        raise OperationalStatusError("stored v2 projection row does not match its digest")
    return row


class DynamoGraphProjectionStore:
    """Status v2 rows under `XROOT#<root_flow_id>`: graphs and receipts of one root flow.

    S5 (FR-3): given a measurement store, a receipt row that carries its root clock is written in
    one transaction with its immutable `FlowRootElapsed` contribution and that contribution's
    `PENDING` acceptance, so a projector crash after the commit always leaves recoverable work and
    a replay of the row adds no second contribution. S6 part 2 (§2.10) does the same for a
    terminal NODE row (`FlowTransitionLatency`) and each CLAIM admission row
    (`ActivityAdmissionElapsed`).
    """

    def __init__(
        self,
        table: str,
        client: DynamoDBClient | None = None,
        metrics: DynamoMetricStore | None = None,
    ) -> None:
        if not table.strip():
            raise ValueError("operational status table is required")
        self._table = table
        self._client = client or boto3.client("dynamodb")
        self._metrics = metrics

    def _contribution(self, row: GraphProjectionRow) -> Measurement | None:
        return None if self._metrics is None else row_measurement(row)

    def apply(self, row: GraphProjectionRow) -> bool:
        """Put one row once; an exact replay is a no-op, other content is a conflict."""
        item: Item = {
            "partition_key": write_text(row.partition_key),
            "sort_key": write_text(row.sort_key),
            "item_kind": write_text(_V2_ITEM_KIND),
            "schema": write_text(_V2_SCHEMA),
            "projection_kind": write_text(row.kind.value),
            "root_flow_id": write_text(row.root_flow_id),
            "authority_epoch": write_int(row.authority_epoch),
            "authority_pk": write_text(row.authority_pk),
            "authority_sk": write_text(row.authority_sk),
            "authority_digest": write_text(row.digest),
            "payload": write_text(_json(dict(row.document))),
        }
        contribution = self._contribution(row)
        try:
            if contribution is None or self._metrics is None:
                self._client.put_item(
                    TableName=self._table, Item=item, ConditionExpression=_NEW_ITEM_CONDITION
                )
            else:
                self._client.transact_write_items(
                    TransactItems=cast(
                        "Any",
                        [
                            {
                                "Put": {
                                    "TableName": self._table,
                                    "Item": item,
                                    "ConditionExpression": _NEW_ITEM_CONDITION,
                                }
                            },
                            *self._metrics.contribution_members(contribution),
                        ],
                    )
                )
        except ClientError as error:
            if not _conditional(error):
                raise
            existing = self._client.get_item(
                TableName=self._table,
                Key={"partition_key": item["partition_key"], "sort_key": item["sort_key"]},
                ConsistentRead=True,
            ).get("Item")
            if existing is None or read_text(dict(existing), "authority_digest") != row.digest:
                raise GraphProjectionConflictError(
                    "a v2 projection row exists with other authority content"
                ) from None
            return False
        return True

    def _query(  # noqa: PLR0913, PLR0917 -- one private bounded query builder
        self, root_flow_id: str, prefix: str, after: str | None, limit: int
    ) -> tuple[list[Item], bool]:
        request: dict[str, Any] = {
            "TableName": self._table,
            "KeyConditionExpression": "partition_key = :pk AND begins_with(sort_key, :prefix)",
            "ExpressionAttributeValues": {
                ":pk": write_text(f"{GRAPH_ROOT_PREFIX}{root_flow_id}"),
                ":prefix": write_text(prefix),
            },
            "ConsistentRead": True,
            "Limit": limit + 1,
        }
        if after is not None:
            if not after.startswith(prefix):
                raise OperationalStatusError("a v2 continuation names another collection")
            request["ExclusiveStartKey"] = {
                "partition_key": write_text(f"{GRAPH_ROOT_PREFIX}{root_flow_id}"),
                "sort_key": write_text(after),
            }
        found: list[Item] = []
        for _ in range(_V2_MAX_QUERY_PAGES):
            response = self._client.query(**request)
            found.extend(dict(item) for item in response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if len(found) > limit or last is None:
                break
            request["ExclusiveStartKey"] = last
            request["Limit"] = limit + 1 - len(found)
        return found[:limit], len(found) > limit

    def graphs(self, root_flow_id: str) -> tuple[GraphProjectionRow, ...]:
        """Return every projected graph row of the root, in key order."""
        items, _ = self._query(root_flow_id, "GRAPH#", None, MAX_QUERY_LIMIT)
        return tuple(_v2_row(item) for item in items)

    def receipts(
        self, root_flow_id: str, after: str | None, limit: int
    ) -> tuple[tuple[GraphProjectionRow, ...], bool]:
        """Return up to `limit` receipt rows after `after`, and whether more exist."""
        items, more = self._query(root_flow_id, "RECEIPT#", after, _limit(limit))
        return tuple(_v2_row(item) for item in items), more

    def executions(self, root_flow_id: str) -> tuple[tuple[GraphProjectionRow, ...], bool]:
        """Return the root's NODE, CLAIM and SERVING rows (S6 §2.10), and whether any was cut.

        Each collection is read up to `MAX_QUERY_LIMIT` rows, in key order; `True` means at
        least one had more, so the explanation can name the gap instead of under-reporting.
        """
        rows: list[GraphProjectionRow] = []
        truncated = False
        for prefix in _EXECUTION_PREFIXES:
            items, more = self._query(root_flow_id, prefix, None, MAX_QUERY_LIMIT)
            rows.extend(_v2_row(item) for item in items)
            truncated = truncated or more
        return tuple(rows), truncated
