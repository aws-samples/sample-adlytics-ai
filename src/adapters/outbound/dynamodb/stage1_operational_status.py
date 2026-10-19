# SPDX-License-Identifier: MIT-0
"""Conditional retained-authority delivery and recovery for ADR-075 Stage 1."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

import boto3
from botocore.exceptions import ClientError

from adapters.outbound.dynamodb.items import (
    ItemError,
    read_int,
    read_moment,
    read_text,
    write_int,
    write_moment,
    write_text,
)
from adapters.outbound.sqs.operational_status import parse_message
from domain.operations.stage1_operational_status import (
    BlockStage1OutboxRequest,
    InlineIdentityRequest,
    LifecycleEventAuthority,
    MarkStage1OutboxDeliveredRequest,
    OperationalDataFlowUsageContribution,
    OperationalDataFlowUsageEvidenceDisposition,
    OutboxState,
    PlanIdentityRequest,
    PlanRequiredChildExpectations,
    PointerExecutionSource,
    PointerExecutionSourceKind,
    PointerIdentityRequest,
    ProtectedStage1OutboxCase,
    ReattestStage1EventRequest,
    RecordStage1AuthorityObservationRequest,
    RecordStage1OutboxDeliveryFailureRequest,
    RecoveryOutcome,
    RequeuedFromBlock,
    RequeueStage1OutboxRequest,
    RequiredChildExpectation,
    SettledUsageContributionAuthority,
    SettledUsageEvidenceDispositionAuthority,
    Stage1AuthorityKind,
    Stage1AuthorityMember,
    Stage1EventAuthorityRequest,
    Stage1EventIdentityMember,
    Stage1OutboxAuthoritySnapshot,
    Stage1OutboxOrderRequest,
    Stage1OutboxRecoveryInvocation,
    Stage1OutboxRecoveryResult,
    Stage1RetainedEventProof,
    Stage1Surface,
    UnitIdentityRequest,
    VerifiedStage1EventAuthority,
    block_stage1_outbox,
    reattest_stage1_event,
    stage1_outbox_order,
    verify_stage1_wire_member,
)
from domain.operations.status import (
    SCHEMA_VERSION,
    AuthorityObservation,
    CompletionOutcome,
    FlowState,
    OperationalDataFlowEvent,
    OperationalStatusError,
    canonical_authority_digest,
)
from ports.outbound.stage1_operational_status import (
    Stage1OutboxDeliveryStatePort,
    Stage1OutboxKey,
    Stage1OutboxRecoveryPort,
    Stage1PendingPage,
    Stage1RetainedAuthority,
    Stage1RetainedEventAuthorityPort,
)

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef, TransactWriteItemTypeDef

OUTBOX_PENDING_INDEX: Final = "stage1-outbox-pending"
MAX_PENDING_LIMIT: Final = 100
AGGREGATE_RECORD: Final = "STAGE1_OUTBOX_BLOCKS"
OPEN_CASES_PARTITION: Final = "STAGE1_OUTBOX_OPEN_CASES"
CASE_RECORD: Final = "STAGE1_OUTBOX"
_CONDITIONAL: Final = "ConditionalCheckFailedException"
_TRANSACTION_CANCELLED: Final = "TransactionCanceledException"
_TRANSACTION_CONDITIONAL: Final = "ConditionalCheckFailed"
_NEW_ITEM: Final = "attribute_not_exists(partition_key) AND attribute_not_exists(sort_key)"
type Item = dict[str, "AttributeValueTypeDef"]


class DynamoStage1OperationalStatus(Stage1OutboxDeliveryStatePort, Stage1OutboxRecoveryPort):
    """Own Stage-1 outbox writes without exposing expressions or transaction members."""

    def __init__(  # noqa: PLR0913, PLR0917 - two tables plus injectable boundaries
        self,
        publications_table: str,
        status_table: str,
        client: DynamoDBClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not publications_table.strip() or not status_table.strip():
            raise ValueError("Stage-1 status adapter requires both retained table names")
        self._publications = publications_table
        self._status = status_table
        self._client = client or boto3.client("dynamodb")
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    def snapshot(self, partition_key: str, record_key: str) -> Stage1OutboxAuthoritySnapshot | None:
        """Strongly read state even when the retained payload is invalid."""
        item = self._get_publication(partition_key, record_key)
        return None if item is None else _snapshot_from_item(item)

    def authority(self, partition_key: str, record_key: str) -> Stage1RetainedAuthority | None:
        """Strongly read one exact base item and validate its immutable queue member."""
        item = self._get_publication(partition_key, record_key)
        return None if item is None else self._authority_from_item(item)

    def pending(self, limit: int) -> Stage1PendingPage:
        """Query the fixed KEYS_ONLY GSI once, from the first key, with no cursor."""
        if isinstance(limit, bool) or not 1 <= limit <= MAX_PENDING_LIMIT:
            raise OperationalStatusError("Stage-1 pending limit must be between 1 and 100")
        response = self._client.query(
            TableName=self._publications,
            IndexName=OUTBOX_PENDING_INDEX,
            KeyConditionExpression="outbox_status = :pending",
            ExpressionAttributeValues={":pending": write_text(OutboxState.PENDING.value)},
            Limit=limit,
            ScanIndexForward=True,
        )
        keys = tuple(
            Stage1OutboxKey(
                read_text(dict(item), "partition_key"), read_text(dict(item), "record_key")
            )
            for item in response.get("Items", [])
        )
        return Stage1PendingPage(keys)

    def record_observation(
        self, request: RecordStage1AuthorityObservationRequest
    ) -> Stage1OutboxAuthoritySnapshot:
        """Write the first lifecycle observation instant or classify an exact replay."""
        authority = request.authority
        try:
            self._client.update_item(
                TableName=self._publications,
                Key=_publication_key(authority.partition_key, authority.record_key),
                UpdateExpression="SET authority_observed_at = :observed",
                ConditionExpression=(
                    _pending_condition() + " AND attribute_not_exists(authority_observed_at)"
                ),
                ExpressionAttributeValues={
                    **_authority_values(authority),
                    ":observed": write_moment(request.observed_at),
                },
            )
        except ClientError as error:
            if not _conditional(error):
                raise
            item = self._get_publication(authority.partition_key, authority.record_key)
            if item is None or not _same_authority(item, authority):
                raise OperationalStatusError(
                    "lifecycle observation lost a changed authority"
                ) from None
            try:
                read_moment(item, "authority_observed_at")
            except (ItemError, ValueError) as parse_error:
                raise OperationalStatusError(
                    "lifecycle observation conflict is incomplete"
                ) from parse_error
            # Set-once replay: the persisted instant is the immutable observation authority.
            # A later delivery attempt's fresh candidate never competes with it — the LLD pins
            # that replay "may update attempts/timestamps but never this field" — so the same
            # authority with an already-persisted instant returns that instant unchanged.
        return self._strong_snapshot(authority.partition_key, authority.record_key)

    def mark_delivered(
        self, request: MarkStage1OutboxDeliveredRequest
    ) -> Stage1OutboxAuthoritySnapshot:
        """Conditionally mark after send and consume any exact requeue marker once."""
        authority = request.authority
        marker = authority.requeued_from_block
        try:
            if marker is None:
                self._client.update_item(
                    TableName=self._publications,
                    Key=_publication_key(authority.partition_key, authority.record_key),
                    UpdateExpression=(
                        "SET outbox_state = :delivered, delivered_at = :delivered_at "
                        "REMOVE outbox_status, outbox_order"
                    ),
                    ConditionExpression=(
                        _pending_condition() + " AND attribute_not_exists(requeued_from_block)"
                    ),
                    ExpressionAttributeValues={
                        **_authority_values(authority),
                        ":delivered": write_text(OutboxState.DELIVERED.value),
                        ":delivered_at": write_moment(request.delivered_at),
                    },
                )
            else:
                aggregates = self._aggregates(marker.affected_surfaces)
                transaction: list[TransactWriteItemTypeDef] = [
                    {
                        "Update": {
                            "TableName": self._publications,
                            "Key": _publication_key(authority.partition_key, authority.record_key),
                            "UpdateExpression": (
                                "SET outbox_state = :delivered, delivered_at = :delivered_at "
                                "REMOVE outbox_status, outbox_order, requeued_from_block"
                            ),
                            "ConditionExpression": (
                                _pending_condition() + " AND requeued_from_block = :expected_marker"
                            ),
                            "ExpressionAttributeValues": {
                                **_authority_values(authority),
                                ":delivered": write_text(OutboxState.DELIVERED.value),
                                ":delivered_at": write_moment(request.delivered_at),
                                ":expected_marker": _write_marker(marker),
                            },
                        }
                    },
                    *(
                        _aggregate_update(
                            self._status,
                            surface,
                            aggregate,
                            request.delivered_at,
                            add_dependency=0,
                            add_invalid=0,
                            add_requeued=-1,
                            reason="stage1_outbox_redelivered",
                        )
                        for surface, aggregate in zip(
                            marker.affected_surfaces, aggregates, strict=True
                        )
                    ),
                ]
                self._client.transact_write_items(TransactItems=transaction)
        except ClientError as error:
            if not _conditional(error):
                raise
            replay = self._strong_snapshot(authority.partition_key, authority.record_key)
            if (
                replay.outbox_state is OutboxState.DELIVERED
                and replay.immutable_payload_digest == authority.immutable_payload_digest
                and replay.authority_kind is authority.authority_kind
            ):
                return replay
            raise OperationalStatusError("delivery mark lost a stale authority race") from None
        return self._strong_snapshot(authority.partition_key, authority.record_key)

    def record_failure(
        self, request: RecordStage1OutboxDeliveryFailureRequest
    ) -> Stage1OutboxAuthoritySnapshot:
        """Advance exactly one below-threshold dependency failure count."""
        authority = request.authority
        next_count = authority.consecutive_failed_delivery_attempts + 1
        try:
            self._client.update_item(
                TableName=self._publications,
                Key=_publication_key(authority.partition_key, authority.record_key),
                UpdateExpression=(
                    "SET consecutive_failed_delivery_attempts = :next_count, "
                    "last_delivery_failed_at = :failed_at, "
                    "last_dependency_error_class = :error_class"
                ),
                ConditionExpression=_pending_condition() + _marker_condition(authority),
                ExpressionAttributeValues={
                    **_authority_values(authority),
                    **_marker_values(authority),
                    ":next_count": write_int(next_count),
                    ":failed_at": write_moment(request.failed_at),
                    ":error_class": write_text(request.dependency_error_class.value),
                },
            )
        except ClientError as error:
            if not _conditional(error):
                raise
            replay = self._strong_snapshot(authority.partition_key, authority.record_key)
            if (
                replay.outbox_state is OutboxState.PENDING
                and replay.immutable_payload_digest == authority.immutable_payload_digest
                and replay.authority_kind is authority.authority_kind
                and replay.consecutive_failed_delivery_attempts == next_count
                and replay.requeued_from_block == authority.requeued_from_block
            ):
                return replay
            raise OperationalStatusError("delivery failure lost a stale authority race") from None
        return self._strong_snapshot(authority.partition_key, authority.record_key)

    def block(self, request: BlockStage1OutboxRequest) -> ProtectedStage1OutboxCase:
        """Atomically block, create protected/safe case records, and account all surfaces."""
        disposition = block_stage1_outbox(request)
        authority = request.authority
        aggregates = self._aggregates(disposition.affected_surfaces)
        blocked_snapshot = Stage1OutboxAuthoritySnapshot(
            authority.partition_key,
            authority.record_key,
            authority.immutable_payload_digest,
            authority.authority_kind,
            OutboxState.BLOCKED,
            disposition.final_failed_delivery_attempts,
        )
        protected = ProtectedStage1OutboxCase(
            request.case_ref,
            blocked_snapshot,
            disposition.affected_surfaces,
            request.blocked_at,
            disposition.reason_code,
        )
        case_item = _case_item(protected, request.block_error_class.value)
        membership_item = _membership_item(protected)
        marker = authority.requeued_from_block
        transaction: list[TransactWriteItemTypeDef] = [
            {
                "Update": {
                    "TableName": self._publications,
                    "Key": _publication_key(authority.partition_key, authority.record_key),
                    "UpdateExpression": (
                        "SET outbox_state = :blocked, blocked_at = :blocked_at, "
                        "block_error_class = :error_class, reason_code = :reason, "
                        "consecutive_failed_delivery_attempts = :final_count "
                        "REMOVE outbox_status, outbox_order"
                        + (", requeued_from_block" if marker is not None else "")
                    ),
                    "ConditionExpression": _pending_condition() + _marker_condition(authority),
                    "ExpressionAttributeValues": {
                        **_authority_values(authority),
                        **_marker_values(authority),
                        ":blocked": write_text(OutboxState.BLOCKED.value),
                        ":blocked_at": write_moment(request.blocked_at),
                        ":error_class": write_text(request.block_error_class.value),
                        ":reason": write_text(disposition.reason_code),
                        ":final_count": write_int(disposition.final_failed_delivery_attempts),
                    },
                }
            },
            {
                "Put": {
                    "TableName": self._status,
                    "Item": case_item,
                    "ConditionExpression": _NEW_ITEM,
                }
            },
            {
                "Put": {
                    "TableName": self._status,
                    "Item": membership_item,
                    "ConditionExpression": _NEW_ITEM,
                }
            },
        ]
        for surface, aggregate in zip(disposition.affected_surfaces, aggregates, strict=True):
            transaction.append(
                _aggregate_update(
                    self._status,
                    surface,
                    aggregate,
                    request.blocked_at,
                    add_dependency=int(disposition.reason_code.endswith("dependency")),
                    add_invalid=int(disposition.reason_code.endswith("invalid")),
                    add_requeued=-1 if marker is not None else 0,
                    reason=disposition.reason_code,
                )
            )
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as error:
            if not _conditional(error):
                raise
            replay = self._read_protected_case(request.case_ref)
            if replay is not None and replay == protected and self._block_replay_matches(replay):
                return replay
            raise OperationalStatusError("outbox block lost a stale authority race") from None
        return protected

    def requeue(self, request: RequeueStage1OutboxRequest) -> Stage1OutboxAuthoritySnapshot:
        """Atomically requeue the exact protected generation and close safe membership."""
        protected = request.protected_case
        authority = protected.authority
        marker = request.marker
        aggregates = self._aggregates(protected.affected_surfaces)
        order = stage1_outbox_order(
            Stage1OutboxOrderRequest(
                authority.partition_key,
                authority.record_key,
                authority.immutable_payload_digest,
                request.requeued_at,
            )
        )
        membership_key = _membership_key(protected.blocked_at, protected.case_ref)
        transaction: list[TransactWriteItemTypeDef] = [
            {
                "Update": {
                    "TableName": self._publications,
                    "Key": _publication_key(authority.partition_key, authority.record_key),
                    "UpdateExpression": (
                        "SET outbox_state = :pending, outbox_status = :pending, "
                        "outbox_order = :order, consecutive_failed_delivery_attempts = :zero, "
                        "requeued_from_block = :marker "
                        "REMOVE blocked_at, block_error_class, reason_code"
                    ),
                    "ConditionExpression": (
                        "immutable_payload_digest = :digest AND authority_kind = :kind AND "
                        "outbox_state = :blocked AND "
                        "consecutive_failed_delivery_attempts = :failed_count AND "
                        "attribute_not_exists(requeued_from_block)"
                    ),
                    "ExpressionAttributeValues": {
                        ":digest": write_text(authority.immutable_payload_digest),
                        ":kind": write_text(authority.authority_kind.value),
                        ":blocked": write_text(OutboxState.BLOCKED.value),
                        ":failed_count": write_int(authority.consecutive_failed_delivery_attempts),
                        ":pending": write_text(OutboxState.PENDING.value),
                        ":order": write_text(order),
                        ":zero": write_int(0),
                        ":marker": _write_marker(marker),
                    },
                }
            },
            {
                "Update": {
                    "TableName": self._status,
                    "Key": _case_key(protected.case_ref),
                    "UpdateExpression": (
                        "SET #state = :requeued, requeued_at = :at, "
                        "resolution_reason = :resolution, "
                        "requeue_audit = list_append(requeue_audit, :audit)"
                    ),
                    "ConditionExpression": (
                        "#state = :blocked AND blocked_partition_key = :partition AND "
                        "blocked_record_key = :record AND immutable_payload_digest = :digest AND "
                        "authority_kind = :kind AND blocked_at = :blocked_at AND "
                        "affected_surfaces = :surfaces"
                    ),
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ExpressionAttributeValues": {
                        ":requeued": write_text("requeued"),
                        ":blocked": write_text("blocked"),
                        ":at": write_moment(request.requeued_at),
                        ":resolution": write_text(request.resolution_reason),
                        ":partition": write_text(authority.partition_key),
                        ":record": write_text(authority.record_key),
                        ":digest": write_text(authority.immutable_payload_digest),
                        ":kind": write_text(authority.authority_kind.value),
                        ":blocked_at": write_moment(protected.blocked_at),
                        ":surfaces": _write_surfaces(protected.affected_surfaces),
                        ":audit": {
                            "L": [
                                {
                                    "M": {
                                        "state": write_text("requeued"),
                                        "at": write_moment(request.requeued_at),
                                        "reason": write_text(request.resolution_reason),
                                    }
                                }
                            ]
                        },
                    },
                }
            },
            {
                "Delete": {
                    "TableName": self._status,
                    "Key": membership_key,
                    "ConditionExpression": (
                        "case_ref = :case_ref AND #state = :blocked AND "
                        "blocked_at = :blocked_at AND "
                        "affected_surfaces = :surfaces AND reason_code = :reason"
                    ),
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ExpressionAttributeValues": {
                        ":case_ref": write_text(protected.case_ref),
                        ":blocked": write_text("blocked"),
                        ":blocked_at": write_moment(protected.blocked_at),
                        ":surfaces": _write_surfaces(protected.affected_surfaces),
                        ":reason": write_text(protected.reason_code),
                    },
                }
            },
        ]
        for surface, aggregate in zip(protected.affected_surfaces, aggregates, strict=True):
            decrement_dependency = int(protected.reason_code.endswith("dependency"))
            decrement_invalid = int(protected.reason_code.endswith("invalid"))
            transaction.append(
                _aggregate_update(
                    self._status,
                    surface,
                    aggregate,
                    request.requeued_at,
                    add_dependency=-decrement_dependency,
                    add_invalid=-decrement_invalid,
                    add_requeued=1,
                    reason="stage1_outbox_requeued",
                )
            )
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as error:
            if not _conditional(error):
                raise
            replay = self.authority(authority.partition_key, authority.record_key)
            if (
                replay is not None
                and replay.snapshot.outbox_state is OutboxState.PENDING
                and replay.snapshot.requeued_from_block == marker
                and replay.snapshot.consecutive_failed_delivery_attempts == 0
                and self._requeue_replay_matches(protected, request.resolution_reason)
            ):
                return replay.snapshot
            raise OperationalStatusError("outbox requeue lost a stale authority race") from None
        return self._strong_snapshot(authority.partition_key, authority.record_key)

    def invoke(self, invocation: Stage1OutboxRecoveryInvocation) -> Stage1OutboxRecoveryResult:
        """Resolve all protected selectors server-side and return only the closed result."""
        case = self._get_status(f"CASE#{invocation.case_ref}", CASE_RECORD)
        if case is None:
            raise OperationalStatusError("protected Stage-1 outbox case is unavailable")
        protected = _protected_case_from_item(case)
        if _case_requeue_audit_matches(case, protected, invocation.resolution_reason):
            if self._pending_requeue_matches(protected):
                return Stage1OutboxRecoveryResult(
                    invocation.case_ref, RecoveryOutcome.ALREADY_REQUEUED
                )
            raise OperationalStatusError("protected Stage-1 recovery is no longer pending")
        try:
            state = read_text(case, "state")
        except ItemError as error:
            raise OperationalStatusError("protected Stage-1 case is malformed") from error
        if state != "blocked":
            raise OperationalStatusError("protected Stage-1 recovery audit differs")
        request = RequeueStage1OutboxRequest(
            protected,
            self._clock().astimezone(UTC),
            invocation.resolution_reason,
        )
        try:
            self.requeue(request)
        except OperationalStatusError:
            replay = self._get_status(f"CASE#{invocation.case_ref}", CASE_RECORD)
            if (
                replay is not None
                and _case_requeue_audit_matches(replay, protected, invocation.resolution_reason)
                and self._pending_requeue_matches(protected)
            ):
                return Stage1OutboxRecoveryResult(
                    invocation.case_ref, RecoveryOutcome.ALREADY_REQUEUED
                )
            raise
        return Stage1OutboxRecoveryResult(invocation.case_ref, RecoveryOutcome.REQUEUED)

    def _block_replay_matches(self, protected: ProtectedStage1OutboxCase) -> bool:
        snapshot = self.snapshot(protected.authority.partition_key, protected.authority.record_key)
        membership = self._get_status(
            OPEN_CASES_PARTITION,
            read_text(_membership_key(protected.blocked_at, protected.case_ref), "sort_key"),
        )
        if snapshot != protected.authority or not _membership_matches(membership, protected):
            return False
        return all(
            aggregate is not None for aggregate in self._aggregates(protected.affected_surfaces)
        )

    def _requeue_replay_matches(
        self, protected: ProtectedStage1OutboxCase, resolution_reason: str
    ) -> bool:
        case = self._get_status(f"CASE#{protected.case_ref}", CASE_RECORD)
        return (
            case is not None
            and _case_requeue_audit_matches(case, protected, resolution_reason)
            and self._pending_requeue_matches(protected)
        )

    def _pending_requeue_matches(self, protected: ProtectedStage1OutboxCase) -> bool:
        authority = protected.authority
        snapshot = self.snapshot(authority.partition_key, authority.record_key)
        return (
            snapshot is not None
            and snapshot.immutable_payload_digest == authority.immutable_payload_digest
            and snapshot.authority_kind is authority.authority_kind
            and snapshot.outbox_state is OutboxState.PENDING
            and snapshot.consecutive_failed_delivery_attempts == 0
            and snapshot.requeued_from_block
            == RequeuedFromBlock(protected.case_ref, protected.affected_surfaces)
        )

    def _authority_from_item(self, item: Item) -> Stage1RetainedAuthority:
        snapshot = _snapshot_from_item(item)
        try:
            payload_text = read_text(item, "payload")
            decoded: object = json.loads(payload_text)
        except (ItemError, json.JSONDecodeError) as error:
            raise OperationalStatusError("retained Stage-1 payload is invalid") from error
        if canonical_authority_digest(decoded) != snapshot.immutable_payload_digest:
            raise OperationalStatusError("retained Stage-1 immutable digest differs")
        tag = {
            Stage1AuthorityKind.LIFECYCLE_EVENT: "event",
            Stage1AuthorityKind.USAGE_CONTRIBUTION: "usage_contribution",
            Stage1AuthorityKind.USAGE_EVIDENCE_DISPOSITION: "usage_evidence_disposition",
        }[snapshot.authority_kind]
        body = json.dumps(
            {"schema_version": SCHEMA_VERSION, tag: decoded},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        parsed = parse_message(body)
        member: Stage1AuthorityMember
        if isinstance(parsed, OperationalDataFlowEvent):
            if snapshot.authority_kind is not Stage1AuthorityKind.LIFECYCLE_EVENT:
                raise OperationalStatusError("retained authority prefix/kind/member mismatch")
            verified = reattest_stage1_event(_retained_event_request(item))
            observed_at = (
                snapshot.authority_observed_at
                if snapshot.authority_observed_at is not None
                else parsed.authority.observed_at
            )
            parsed = replace(
                parsed,
                authority=AuthorityObservation(
                    parsed.authority.kind,
                    verified.authority_digest,
                    observed_at,
                ),
            )
            verify_stage1_wire_member(parsed, verified)
            member = LifecycleEventAuthority(parsed)
        elif isinstance(parsed, OperationalDataFlowUsageContribution):
            _settled_usage(item)
            if snapshot.authority_kind is not Stage1AuthorityKind.USAGE_CONTRIBUTION:
                raise OperationalStatusError("retained authority prefix/kind/member mismatch")
            member = SettledUsageContributionAuthority(parsed)
        elif isinstance(parsed, OperationalDataFlowUsageEvidenceDisposition):
            _settled_usage(item)
            if snapshot.authority_kind is not Stage1AuthorityKind.USAGE_EVIDENCE_DISPOSITION:
                raise OperationalStatusError("retained authority prefix/kind/member mismatch")
            member = SettledUsageEvidenceDispositionAuthority(parsed)
        else:
            raise OperationalStatusError("retained authority cannot be a surface observation")
        return Stage1RetainedAuthority(snapshot, member)

    def _strong_snapshot(
        self, partition_key: str, record_key: str
    ) -> Stage1OutboxAuthoritySnapshot:
        item = self._get_publication(partition_key, record_key)
        if item is None:
            raise OperationalStatusError("Stage-1 authority disappeared after mutation")
        return _snapshot_from_item(item)

    def _get_publication(self, partition_key: str, record_key: str) -> Item | None:
        response = self._client.get_item(
            TableName=self._publications,
            Key=_publication_key(partition_key, record_key),
            ConsistentRead=True,
        )
        found = response.get("Item")
        return None if found is None else dict(found)

    def _get_status(self, partition_key: str, sort_key: str) -> Item | None:
        response = self._client.get_item(
            TableName=self._status,
            Key={"partition_key": write_text(partition_key), "sort_key": write_text(sort_key)},
            ConsistentRead=True,
        )
        found = response.get("Item")
        return None if found is None else dict(found)

    def _aggregates(self, surfaces: tuple[Stage1Surface, ...]) -> tuple[Item | None, ...]:
        return tuple(
            self._get_status(f"STATUS_OBSERVATION#{surface.value}", AGGREGATE_RECORD)
            for surface in surfaces
        )

    def _read_protected_case(self, case_ref: str) -> ProtectedStage1OutboxCase | None:
        item = self._get_status(f"CASE#{case_ref}", CASE_RECORD)
        if item is None:
            return None
        return _protected_case_from_item(item)


class DynamoStage1RetainedEventAuthorityReader(Stage1RetainedEventAuthorityPort):
    """Strong wire-derived reads of retained ``STATUS_FLOW#``/``EVENT#`` lifecycle authority."""

    def __init__(self, publications_table: str, client: DynamoDBClient | None = None) -> None:
        if not publications_table.strip():
            raise ValueError("Stage-1 retained authority reader requires the publications table")
        self._publications = publications_table
        self._client = client or boto3.client("dynamodb")

    def retained_event_authority(
        self, flow_id: str, opaque_digest: str
    ) -> VerifiedStage1EventAuthority | None:
        """Derive the retained key from the received member alone and re-attest the item."""
        response = self._client.get_item(
            TableName=self._publications,
            Key={
                "partition_key": write_text(f"STATUS_FLOW#{flow_id}"),
                "record_key": write_text(f"EVENT#{opaque_digest}"),
            },
            ConsistentRead=True,
        )
        found = response.get("Item")
        if found is None:
            return None
        return reattest_stage1_event(_retained_event_request(dict(found)))


def _retained_event_request(item: Item) -> ReattestStage1EventRequest:
    try:
        surface = Stage1Surface(read_text(item, "surface"))
        authority = Stage1EventAuthorityRequest(
            surface=surface,
            internal_key=_read_internal_key(item),
            transition_id=read_text(item, "transition_id"),
            lifecycle_state=FlowState(read_text(item, "lifecycle_state")),
            completion_outcome=(
                CompletionOutcome(read_text(item, "completion_outcome"))
                if "completion_outcome" in item
                else None
            ),
            typed_reason=(read_text(item, "typed_reason") if "typed_reason" in item else None),
            authority_time=read_text(item, "authority_time"),
        )
        proof = Stage1RetainedEventProof(
            flow_id=read_text(item, "flow_id"),
            execution_id=read_text(item, "execution_id"),
            event_id=read_text(item, "event_id"),
            transition_sequence=_read_required_int(item, "transition_sequence"),
            predecessor_event_id=_optional_text_attribute(item, "predecessor_event_id"),
            predecessor_execution_id=_optional_text_attribute(item, "predecessor_execution_id"),
            parent_flow_id=_optional_text_attribute(item, "parent_flow_id"),
            causation_event_id=_optional_text_attribute(item, "causation_event_id"),
        )
        identity = _read_event_identity(item, authority)
        owner_generation = (
            _read_required_int(item, "owner_generation") if surface is Stage1Surface.UNIT else None
        )
        expectations = (
            _read_plan_expectations(item, proof.flow_id) if surface is Stage1Surface.PLAN else None
        )
    except OperationalStatusError:
        raise
    except (ItemError, ValueError) as error:
        raise OperationalStatusError(
            "retained Stage-1 event authority fields are incomplete"
        ) from error
    return ReattestStage1EventRequest(authority, identity, proof, owner_generation, expectations)


def _read_internal_key(item: Item) -> tuple[str | int, ...]:
    raw = item.get("internal_key", {}).get("L")
    if raw is None:
        raise ItemError("internal_key is missing or not a list.")
    members: list[str | int] = []
    for member in raw:
        if "S" in member:
            members.append(cast("str", member["S"]))
        elif "N" in member:
            members.append(int(cast("str", member["N"])))
        else:
            raise ItemError("internal_key member is not text or a number.")
    return tuple(members)


def _read_event_identity(
    item: Item, authority: Stage1EventAuthorityRequest
) -> Stage1EventIdentityMember:
    surface = authority.surface
    if surface is Stage1Surface.PLAN:
        return PlanIdentityRequest(
            read_text(item, "plan_digest"), read_text(item, "internal_execution_id")
        )
    if surface is Stage1Surface.UNIT:
        return UnitIdentityRequest(
            read_text(item, "plan_digest"),
            _text_key_member(authority.internal_key, 1),
            read_text(item, "internal_execution_id"),
        )
    if surface is Stage1Surface.INLINE_RUN:
        return InlineIdentityRequest(
            _text_key_member(authority.internal_key, 0),
            _text_key_member(authority.internal_key, 1),
            _text_key_member(authority.internal_key, 2),
            read_text(item, "internal_execution_id"),
        )
    fencing_token = authority.internal_key[2]
    if isinstance(fencing_token, str):
        raise ItemError("pointer fencing token is not a number.")
    return PointerIdentityRequest(
        _text_key_member(authority.internal_key, 0),
        _text_key_member(authority.internal_key, 1),
        fencing_token,
        PointerExecutionSource(
            PointerExecutionSourceKind(read_text(item, "internal_pointer_execution_source_kind")),
            read_text(item, "internal_pointer_execution_id"),
        ),
    )


def _read_plan_expectations(item: Item, parent_flow_id: str) -> PlanRequiredChildExpectations:
    version = _read_required_int(item, "child_outcome_reduction_version")
    raw = item.get("required_child_expectations", {}).get("L")
    if raw is None:
        raise ItemError("required_child_expectations is missing or not a list.")
    expectations: list[RequiredChildExpectation] = []
    for member in raw:
        entry = member.get("M")
        if entry is None:
            raise ItemError("required child expectation is malformed.")
        entry_item = cast("Item", dict(entry))
        expectations.append(
            RequiredChildExpectation(
                read_text(entry_item, "child_flow_id"),
                _read_required_int(entry_item, "owner_generation"),
            )
        )
    return PlanRequiredChildExpectations(
        parent_flow_id,
        tuple(expectations),
        child_outcome_reduction_version=version,
    )


def _text_key_member(members: tuple[str | int, ...], index: int) -> str:
    member = members[index]
    if not isinstance(member, str):
        raise ItemError("internal_key member is not text.")
    return member


def _optional_text_attribute(item: Item, name: str) -> str | None:
    return read_text(item, name) if name in item else None


def _read_required_int(item: Item, name: str) -> int:
    if name not in item:
        raise ItemError(f"{name} is missing or not a number.")
    return read_int(item, name)


def _publication_key(partition_key: str, record_key: str) -> Item:
    if not partition_key.strip() or not record_key.startswith(("EVENT#", "USAGE#")):
        raise OperationalStatusError("Stage-1 authority key is outside the closed prefixes")
    return {"partition_key": write_text(partition_key), "record_key": write_text(record_key)}


def _snapshot_from_item(item: Item) -> Stage1OutboxAuthoritySnapshot:
    try:
        state = OutboxState(read_text(item, "outbox_state"))
        marker = _read_marker(item)
        return Stage1OutboxAuthoritySnapshot(
            partition_key=read_text(item, "partition_key"),
            record_key=read_text(item, "record_key"),
            immutable_payload_digest=read_text(item, "immutable_payload_digest"),
            authority_kind=Stage1AuthorityKind(read_text(item, "authority_kind")),
            outbox_state=state,
            consecutive_failed_delivery_attempts=read_int(
                item, "consecutive_failed_delivery_attempts"
            ),
            outbox_order=(
                read_text(item, "outbox_order") if state is OutboxState.PENDING else None
            ),
            requeued_from_block=marker,
            authority_observed_at=(
                read_moment(item, "authority_observed_at")
                if "authority_observed_at" in item
                else None
            ),
        )
    except (ItemError, ValueError) as error:
        raise OperationalStatusError("retained Stage-1 authority state is malformed") from error


def _settled_usage(item: Item) -> None:
    try:
        settled = read_text(item, "athena_usage_state")
        delta_attribute = item["is_delta"]
    except (ItemError, KeyError) as error:
        raise OperationalStatusError("retained Stage-1 usage is not owner-settled") from error
    if (
        settled != "settled"
        or frozenset(delta_attribute) != frozenset({"BOOL"})
        or delta_attribute.get("BOOL") is not False
    ):
        raise OperationalStatusError("retained Stage-1 usage is unsettled or a delta")


def _pending_condition() -> str:
    return (
        "immutable_payload_digest = :digest AND authority_kind = :kind AND "
        "outbox_state = :pending AND outbox_status = :pending AND "
        "outbox_order = :order AND consecutive_failed_delivery_attempts = :failed_count"
    )


def _authority_values(authority: Stage1OutboxAuthoritySnapshot) -> Item:
    if authority.outbox_order is None:
        raise OperationalStatusError("pending authority has no outbox order")
    return {
        ":digest": write_text(authority.immutable_payload_digest),
        ":kind": write_text(authority.authority_kind.value),
        ":pending": write_text(OutboxState.PENDING.value),
        ":order": write_text(authority.outbox_order),
        ":failed_count": write_int(authority.consecutive_failed_delivery_attempts),
    }


def _same_authority(item: Item, authority: Stage1OutboxAuthoritySnapshot) -> bool:
    try:
        snapshot = _snapshot_from_item(item)
    except OperationalStatusError:
        return False
    return (
        snapshot.partition_key == authority.partition_key
        and snapshot.record_key == authority.record_key
        and snapshot.immutable_payload_digest == authority.immutable_payload_digest
        and snapshot.authority_kind is authority.authority_kind
    )


def _marker_condition(authority: Stage1OutboxAuthoritySnapshot) -> str:
    return (
        " AND attribute_not_exists(requeued_from_block)"
        if authority.requeued_from_block is None
        else " AND requeued_from_block = :expected_marker"
    )


def _marker_values(authority: Stage1OutboxAuthoritySnapshot) -> Item:
    marker = authority.requeued_from_block
    return {} if marker is None else {":expected_marker": _write_marker(marker)}


def _write_marker(marker: RequeuedFromBlock) -> AttributeValueTypeDef:
    return {
        "M": {
            "case_ref": write_text(marker.case_ref),
            "affected_surfaces": _write_surfaces(marker.affected_surfaces),
        }
    }


def _read_marker(item: Item) -> RequeuedFromBlock | None:
    raw = item.get("requeued_from_block", {}).get("M")
    if raw is None:
        return None
    try:
        surfaces = _read_surfaces(raw["affected_surfaces"])
        case_ref = cast("str", raw["case_ref"]["S"])
    except (KeyError, TypeError) as error:
        raise OperationalStatusError("retained requeue marker is malformed") from error
    return RequeuedFromBlock(case_ref, surfaces)


def _write_surfaces(surfaces: tuple[Stage1Surface, ...]) -> AttributeValueTypeDef:
    return {"L": [write_text(surface.value) for surface in surfaces]}


def _read_surfaces(value: AttributeValueTypeDef) -> tuple[Stage1Surface, ...]:
    raw = value.get("L")
    if raw is None:
        raise OperationalStatusError("affected surfaces are malformed")
    try:
        return tuple(Stage1Surface(cast("str", item["S"])) for item in raw)
    except (KeyError, ValueError) as error:
        raise OperationalStatusError("affected surfaces are malformed") from error


def _case_key(case_ref: str) -> Item:
    return {
        "partition_key": write_text(f"CASE#{case_ref}"),
        "sort_key": write_text(CASE_RECORD),
    }


def _membership_key(blocked_at: datetime, case_ref: str) -> Item:
    micros = int(blocked_at.astimezone(UTC).timestamp() * 1_000_000)
    return {
        "partition_key": write_text(OPEN_CASES_PARTITION),
        "sort_key": write_text(f"OPEN#{micros:020d}#{case_ref}"),
    }


def _case_item(case: ProtectedStage1OutboxCase, error_class: str) -> Item:
    return {
        **_case_key(case.case_ref),
        "state": write_text("blocked"),
        "affected_surfaces": _write_surfaces(case.affected_surfaces),
        "blocked_partition_key": write_text(case.authority.partition_key),
        "blocked_record_key": write_text(case.authority.record_key),
        "immutable_payload_digest": write_text(case.authority.immutable_payload_digest),
        "authority_kind": write_text(case.authority.authority_kind.value),
        "failed_delivery_attempts": write_int(case.authority.consecutive_failed_delivery_attempts),
        "created_at": write_moment(case.blocked_at),
        "blocked_at": write_moment(case.blocked_at),
        "reason_code": write_text(case.reason_code),
        "block_error_class": write_text(error_class),
        "block_audit": {
            "L": [
                {
                    "M": {
                        "state": write_text("blocked"),
                        "at": write_moment(case.blocked_at),
                        "reason_code": write_text(case.reason_code),
                    }
                }
            ]
        },
        "requeue_audit": {"L": []},
    }


def _membership_item(case: ProtectedStage1OutboxCase) -> Item:
    return {
        **_membership_key(case.blocked_at, case.case_ref),
        "case_ref": write_text(case.case_ref),
        "affected_surfaces": _write_surfaces(case.affected_surfaces),
        "state": write_text("blocked"),
        "blocked_at": write_moment(case.blocked_at),
        "reason_code": write_text(case.reason_code),
        "reading": write_text("Stage-1 status delivery requires recovery."),
        "next_action": write_text("requeue_stage1_outbox"),
    }


def _membership_matches(item: Item | None, protected: ProtectedStage1OutboxCase) -> bool:
    if item is None:
        return False
    try:
        return (
            read_text(item, "case_ref") == protected.case_ref
            and read_text(item, "state") == "blocked"
            and read_moment(item, "blocked_at") == protected.blocked_at
            and _read_surfaces(item["affected_surfaces"]) == protected.affected_surfaces
            and read_text(item, "reason_code") == protected.reason_code
        )
    except ItemError, KeyError, OperationalStatusError, ValueError:
        return False


def _protected_case_from_item(item: Item) -> ProtectedStage1OutboxCase:
    try:
        case_ref = read_text(item, "partition_key").removeprefix("CASE#")
        uuid.UUID(case_ref)
        surfaces = _read_surfaces(item["affected_surfaces"])
        blocked_at = read_moment(item, "blocked_at")
        authority = Stage1OutboxAuthoritySnapshot(
            read_text(item, "blocked_partition_key"),
            read_text(item, "blocked_record_key"),
            read_text(item, "immutable_payload_digest"),
            Stage1AuthorityKind(read_text(item, "authority_kind")),
            OutboxState.BLOCKED,
            read_int(item, "failed_delivery_attempts"),
        )
        if read_text(item, "state") not in {"blocked", "requeued"}:
            raise OperationalStatusError("protected case state is invalid")
        return ProtectedStage1OutboxCase(
            case_ref,
            authority,
            surfaces,
            blocked_at,
            read_text(item, "reason_code"),
        )
    except (ItemError, KeyError, ValueError) as error:
        raise OperationalStatusError("protected Stage-1 case is malformed") from error


def _case_requeue_audit_matches(
    item: Item,
    protected: ProtectedStage1OutboxCase,
    resolution_reason: str,
) -> bool:
    audit = item.get("requeue_audit", {}).get("L")
    if not isinstance(audit, list) or len(audit) != 1:
        return False
    raw_entry = cast("dict[str, object]", audit[0]).get("M")
    if not isinstance(raw_entry, dict):
        return False
    entry = cast("Item", raw_entry)
    if frozenset(entry) != frozenset({"state", "at", "reason"}):
        return False
    try:
        requeued_at = read_moment(item, "requeued_at")
        return (
            read_text(item, "partition_key") == f"CASE#{protected.case_ref}"
            and read_text(item, "sort_key") == CASE_RECORD
            and read_text(item, "state") == "requeued"
            and read_text(item, "resolution_reason") == resolution_reason
            and read_text(entry, "state") == "requeued"
            and read_text(entry, "reason") == resolution_reason
            and read_moment(entry, "at") == requeued_at
            and requeued_at >= protected.blocked_at
        )
    except ItemError, ValueError:
        return False


def _aggregate_update(  # noqa: PLR0913, PLR0917 - private transaction builder
    table: str,
    surface: Stage1Surface,
    current: Item | None,
    observed_at: datetime,
    *,
    add_dependency: int,
    add_invalid: int,
    add_requeued: int,
    reason: str,
) -> TransactWriteItemTypeDef:
    dependency = 0 if current is None else read_int(current, "blocked_dependency")
    invalid = 0 if current is None else read_int(current, "blocked_invalid")
    requeued = 0 if current is None else read_int(current, "requeued_pending")
    next_values = (
        dependency + add_dependency,
        invalid + add_invalid,
        requeued + add_requeued,
    )
    if min(next_values) < 0:
        raise OperationalStatusError("Stage-1 aggregate accounting would underflow")
    sequence = 0 if current is None else read_int(current, "source_sequence")
    key = {
        "partition_key": write_text(f"STATUS_OBSERVATION#{surface.value}"),
        "sort_key": write_text(AGGREGATE_RECORD),
    }
    values: Item = {
        ":dependency": write_int(next_values[0]),
        ":invalid": write_int(next_values[1]),
        ":requeued": write_int(next_values[2]),
        ":next_sequence": write_int(sequence + 1),
        ":observed": write_moment(observed_at),
        ":evidence": write_text("complete" if sum(next_values) == 0 else "incomplete"),
        ":reason": write_text(reason),
        ":pending": write_text("pending"),
        ":report_id": write_text(str(uuid.uuid4())),
    }
    if current is None:
        condition = _NEW_ITEM
    else:
        condition = (
            "blocked_dependency = :old_dependency AND blocked_invalid = :old_invalid AND "
            "requeued_pending = :old_requeued AND source_sequence = :old_sequence"
        )
        values.update(
            {
                ":old_dependency": write_int(dependency),
                ":old_invalid": write_int(invalid),
                ":old_requeued": write_int(requeued),
                ":old_sequence": write_int(sequence),
            }
        )
    return {
        "Update": {
            "TableName": table,
            "Key": key,
            "UpdateExpression": (
                "SET blocked_dependency = :dependency, blocked_invalid = :invalid, "
                "requeued_pending = :requeued, source_sequence = :next_sequence, "
                "observed_at = :observed, evidence = :evidence, latest_reason = :reason, "
                "report_state = :pending, report_id = :report_id"
            ),
            "ConditionExpression": condition,
            "ExpressionAttributeValues": values,
        }
    }


def _conditional(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    if code == _CONDITIONAL:
        return True
    if code != _TRANSACTION_CANCELLED:
        return False
    reasons = cast("list[dict[str, object]]", error.response.get("CancellationReasons") or [])
    return any(reason.get("Code") == _TRANSACTION_CONDITIONAL for reason in reasons)
