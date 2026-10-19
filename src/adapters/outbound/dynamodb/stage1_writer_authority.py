# SPDX-License-Identifier: MIT-0
"""Strongly consistent ADR-075 §2.2/§4 authority reads for the queued table writer.

The coordinator owns every write to the ``WRITE_PLAN#``, ``STATUS_FLOW#``,
``TABLE_ATTEMPT#``, and ``TABLE#`` key families; the writer owns no mutation
outside its typed coordinator requests. Before claim and before every
condition-carrying request, the writer strongly reads the exact owner, origin
intent, plan ``META``, unit, plan/unit ``HEAD``, lease, fence, attempt, and
``CURRENT`` snapshots this module returns. Raw snapshots are returned as plain
items — the exact byte forms the coordinator's builders re-serialize into
condition expressions — and the closed typed forms are parsed through their
domain constructors so a malformed stored item fails as a typed invariant
rather than flowing into a condition.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final, cast

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from domain.operations.stage1_lifecycle_head import (
    EVENT_RECORD_PREFIX,
    HEAD_RECORD_KEY,
    STATUS_FLOW_PREFIX,
    Stage1LifecycleHeadTuple,
)
from domain.operations.stage1_operational_status import FlowState, Stage1Surface
from domain.operations.status import event_from_document
from domain.operations.table_attempt import (
    USAGE_RECORD_PREFIX,
    AttemptObservation,
    FenceObservation,
    ReconciliationObservation,
    attempt_record_key,
    parse_sanitized_evidence,
    run_record_key,
    table_attempt_partition_key,
)
from domain.operations.table_lease import TableLeaseRecord, table_lease_partition_key
from domain.operations.write_plan import (
    META_RECORD_KEY,
    SUCCESSOR_OWNER_RECORD_KEY,
    UNIT_RECORD_PREFIX,
    CanonicalObjectEntry,
    CanonicalUnitEntry,
    IntentMeta,
    IntentOrigin,
    IntentOriginKind,
    IntentState,
    PlanMeta,
    PlanStatus,
    RequiredChildExpectation,
    SettledAttemptReference,
    SuccessorOwner,
    intent_partition_key,
    owner_partition_key,
    unit_record_key,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.projection_coordinator import (
    InlineRunAttemptOwner,
    QueuedUnitAttemptOwner,
    SettledAttemptObservation,
    SettledUsageAuthorityObservation,
    SourcePlanReplacementEvidence,
    TableAttemptOwnerSnapshot,
)

if TYPE_CHECKING:  # pragma: no cover — kept out of the Lambda import path
    from collections.abc import Mapping
    from datetime import datetime

    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

__all__ = [
    "Stage1WriterAuthorityReader",
    "plan_meta_from_item",
    "unit_entry_from_item",
]

_UNREADABLE: Final = ErrorCode("STAGE1_WRITER_READS", 1)
_MALFORMED: Final = ErrorCode("STAGE1_WRITER_READS", 2)

_RECONCILIATION_FIELDS: Final = (
    "reconcile_status",
    "reconcile_after_or_block",
    "reconcile_attempt_count",
    "requeued_from_case_ref",
    "requeued_at",
    "reconcile_block_id",
    "reconcile_block_reason",
    "reconcile_blocked_at",
    "reconcile_observation_digest",
    "reconcile_recovery_ref",
)
_ATTEMPT_TEXT_FIELDS: Final = (
    "state",
    "physical_table_id",
    "attempt_id",
    "plan_unit_owner_ref",
    "inline_run_owner_ref",
    "statement_kind",
    "lease_holder",
    "writer_execution_id",
    "start_envelope",
    "client_request_token",
    "envelope_digest",
    "reservation_identity",
    "query_execution_id",
    "submitted_at",
    "cancel_requested_at",
    "parked_at",
    "terminal_at",
    "athena_usage_state",
    "owner_disposition",
    "usage_category",
    "usage_authority_digest",
)
_ATTEMPT_INT_FIELDS: Final = (
    "ordinal",
    "fencing_token",
    "query_count",
    "scanned_bytes",
    "engine_ms",
)
_OBJECT_MEMBER_ARITY: Final = 6


def _malformed(description: str, **context: object) -> InvariantError:
    return InvariantError(_MALFORMED, description, context=dict(context))


def _plain_value(low: Mapping[str, object]) -> object:
    if len(low) != 1:
        raise _malformed("a stored attribute value carries exactly one type tag.")
    tag, value = next(iter(low.items()))
    if tag == "S":
        return value
    if tag == "N":
        return int(cast("str", value))
    if tag == "BOOL":
        return bool(value)
    if tag == "NULL":
        return None
    if tag == "L":
        return [
            _plain_value(cast("Mapping[str, object]", member))
            for member in cast("list[object]", value)
        ]
    if tag == "M":
        return {
            str(name): _plain_value(cast("Mapping[str, object]", member))
            for name, member in cast("dict[object, object]", value).items()
        }
    raise _malformed("a stored attribute value uses a type outside the writer's key families.")


def _plain_item(low: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
    return {name: _plain_value(value) for name, value in low.items()}


def _text(item: Mapping[str, object], name: str) -> str | None:
    value = item.get(name)
    return value if isinstance(value, str) else None


def _required_text(item: Mapping[str, object], name: str) -> str:
    value = _text(item, name)
    if value is None:
        raise _malformed(f"stored item is missing required text field {name}.")
    return value


def _number(item: Mapping[str, object], name: str) -> int | None:
    value = item.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _required_number(item: Mapping[str, object], name: str) -> int:
    value = _number(item, name)
    if value is None:
        raise _malformed(f"stored item is missing required integer field {name}.")
    return value


def unit_entry_from_item(item: Mapping[str, object]) -> CanonicalUnitEntry:
    """Rebuild the closed canonical unit entry retained on one plan ``UNIT#`` item."""
    raw_objects = item.get("objects")
    if not isinstance(raw_objects, list):
        raise _malformed("a plan unit item retains its canonical object roster.")
    entries: list[CanonicalObjectEntry] = []
    for member in cast("list[object]", raw_objects):
        if not isinstance(member, list):
            raise _malformed("a canonical object member is the exact six-member list.")
        if len(cast("list[object]", member)) != _OBJECT_MEMBER_ARITY:
            raise _malformed("a canonical object member is the exact six-member list.")
        kind, reference, checksum, publication_id, partition_key, fencing_token = cast(
            "list[object]", member
        )
        if (
            not isinstance(kind, str)
            or not isinstance(reference, str)
            or not isinstance(checksum, str)
            or not isinstance(publication_id, str)
            or not (partition_key is None or isinstance(partition_key, str))
            or isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
        ):
            raise _malformed("a canonical object member carries typed drift.")
        entries.append(
            CanonicalObjectEntry(
                kind=kind,
                reference=reference,
                content_checksum=checksum,
                publication_id=publication_id,
                partition_key=partition_key,
                fencing_token=fencing_token,
            )
        )
    return CanonicalUnitEntry(
        unit_key=_required_text(item, "unit_key"),
        table_kind=_required_text(item, "table_kind"),
        merge_count=_required_number(item, "merge_count"),
        objects=tuple(entries),
    )


class Stage1WriterAuthorityReader:
    """Strongly consistent typed reads over the retained publications and leases tables."""

    def __init__(
        self,
        publications_table: str,
        leases_table: str,
        client: DynamoDBClient | None = None,
    ) -> None:
        if not publications_table.strip() or not leases_table.strip():
            raise ValueError("the reader requires both retained table names.")
        if publications_table == leases_table:
            raise ValueError("the publications and leases tables are distinct.")
        self._publications = publications_table
        self._leases = leases_table
        self._client: DynamoDBClient = client if client is not None else boto3.client("dynamodb")

    # -- low-level strongly consistent reads ------------------------------------------------------

    def _get(self, table: str, key: dict[str, AttributeValueTypeDef]) -> dict[str, object] | None:
        try:
            response = self._client.get_item(TableName=table, Key=key, ConsistentRead=True)
        except (BotoCoreError, ClientError) as error:
            raise DependencyError(
                _UNREADABLE,
                "A strongly consistent writer authority read failed.",
                context={"cause": type(error).__name__},
            ) from error
        item = response.get("Item")
        if item is None:
            return None
        return _plain_item(cast("Mapping[str, Mapping[str, object]]", item))

    def _get_record(self, partition_key: str, record_key: str) -> dict[str, object] | None:
        return self._get(
            self._publications,
            {"partition_key": {"S": partition_key}, "record_key": {"S": record_key}},
        )

    # -- plan-side typed reads ---------------------------------------------------------------------

    def owner(self, plan_digest: str) -> SuccessorOwner | None:
        """Strongly read the one ``SUCCESSOR_OWNER`` record for this plan digest."""
        item = self._get_record(owner_partition_key(plan_digest), SUCCESSOR_OWNER_RECORD_KEY)
        if item is None:
            return None
        return SuccessorOwner(
            revision=_required_number(item, "revision"),
            root_plan_key=_text(item, "root_plan_key"),
            last_plan_key=_text(item, "last_plan_key"),
            last_generation=_required_number(item, "last_generation"),
            active_plan_key=_text(item, "active_plan_key"),
            active_intent_id=_text(item, "active_intent_id"),
            reserved_intent_id=_text(item, "reserved_intent_id"),
            reserved_intent_digest=_text(item, "reserved_intent_digest"),
            reserved_generation=_number(item, "reserved_generation"),
            latest_internal_execution_id=_text(item, "latest_internal_execution_id"),
        )

    def intent(self, plan_digest: str, intent_id: str) -> IntentMeta | None:
        """Strongly read one origin intent ``META``."""
        item = self._get_record(intent_partition_key(plan_digest, intent_id), META_RECORD_KEY)
        if item is None:
            return None
        return IntentMeta(
            root_plan_key=_text(item, "root_plan_key"),
            source_plan_key=_text(item, "source_plan_key"),
            plan_digest=_required_text(item, "plan_digest"),
            intent_id=_required_text(item, "intent_id"),
            roster_digest=_required_text(item, "roster_digest"),
            origin=IntentOrigin(
                IntentOriginKind(_required_text(item, "origin_kind")),
                _required_text(item, "origin_member"),
            ),
            state=IntentState(_required_text(item, "state")),
            claimed_generation=_number(item, "claimed_generation"),
            retry_count=_required_number(item, "retry_count"),
            due_at=_required_text(item, "due_at"),
            ready_status=_text(item, "ready_status"),
            ready_order=_text(item, "ready_order"),
            created_at=_required_text(item, "created_at"),
            last_transition_at=_required_text(item, "last_transition_at"),
        )

    def plan_meta_item(self, plan_key: str) -> dict[str, object] | None:
        """Strongly read one plan ``META`` as its raw plain item."""
        return self._get_record(plan_key, META_RECORD_KEY)

    def plan_meta(self, plan_key: str) -> PlanMeta | None:
        """Strongly read and parse one plan ``META`` into its closed typed form."""
        item = self.plan_meta_item(plan_key)
        if item is None:
            return None
        return plan_meta_from_item(item)

    def unit_item(self, plan_key: str, unit_key: str) -> dict[str, object] | None:
        """Strongly read one plan ``UNIT#`` as its raw plain item."""
        return self._get_record(plan_key, unit_record_key(unit_key))

    def plan_unit_items(self, plan_key: str) -> tuple[dict[str, object], ...]:
        """Strongly read every ``UNIT#`` item under one plan partition."""
        items: list[dict[str, object]] = []
        start_key: dict[str, AttributeValueTypeDef] | None = None
        while True:
            try:
                request: dict[str, object] = {
                    "TableName": self._publications,
                    "KeyConditionExpression": (
                        "partition_key = :plan AND begins_with(record_key, :unit)"
                    ),
                    "ExpressionAttributeValues": {
                        ":plan": {"S": plan_key},
                        ":unit": {"S": UNIT_RECORD_PREFIX},
                    },
                    "ConsistentRead": True,
                }
                if start_key is not None:
                    request["ExclusiveStartKey"] = start_key
                response = self._client.query(**request)  # pyright: ignore[reportArgumentType]
            except (BotoCoreError, ClientError) as error:
                raise DependencyError(
                    _UNREADABLE,
                    "A strongly consistent plan-unit query failed.",
                    context={"cause": type(error).__name__},
                ) from error
            for member in response.get("Items", []):
                items.append(_plain_item(cast("Mapping[str, Mapping[str, object]]", member)))
            start_key = response.get("LastEvaluatedKey")
            if start_key is None:
                return tuple(items)

    def flow_root_clock(self, flow_id: str) -> datetime:
        """Strongly read the flow's retained root event and return its exact clock instant.

        The root ``EVENT#`` payload is the durable owner timestamp every successor
        transition must repeat as ``clock_started_at``: the status reducer rejects a
        successor whose clock differs from the applied root's, so the clock is read
        back from retained authority rather than recomputed from any current wall.
        """
        partition = f"{STATUS_FLOW_PREFIX}{flow_id}"
        roots: list[dict[str, object]] = []
        start_key: dict[str, AttributeValueTypeDef] | None = None
        while True:
            try:
                request: dict[str, object] = {
                    "TableName": self._publications,
                    "KeyConditionExpression": (
                        "partition_key = :flow AND begins_with(record_key, :event)"
                    ),
                    "ExpressionAttributeValues": {
                        ":flow": {"S": partition},
                        ":event": {"S": EVENT_RECORD_PREFIX},
                    },
                    "ConsistentRead": True,
                }
                if start_key is not None:
                    request["ExclusiveStartKey"] = start_key
                response = self._client.query(**request)  # pyright: ignore[reportArgumentType]
            except (BotoCoreError, ClientError) as error:
                raise DependencyError(
                    _UNREADABLE,
                    "A strongly consistent flow root-event query failed.",
                    context={"cause": type(error).__name__},
                ) from error
            for member in response.get("Items", []):
                item = _plain_item(cast("Mapping[str, Mapping[str, object]]", member))
                if _number(item, "transition_sequence") == 1:
                    roots.append(item)
            start_key = response.get("LastEvaluatedKey")
            if start_key is None:
                break
        if len(roots) != 1:
            raise InvariantError(
                _MALFORMED,
                "A lifecycle flow does not retain exactly one root event.",
                context={"flow_id": flow_id, "root_count": len(roots)},
            )
        payload = roots[0].get("payload")
        if not isinstance(payload, str):
            raise InvariantError(
                _MALFORMED,
                "The retained root event carries no payload text.",
                context={"flow_id": flow_id},
            )
        event = event_from_document(json.loads(payload))
        clock = event.clock_started_at
        if clock is None:
            raise InvariantError(
                _MALFORMED,
                "The retained root event carries no flow clock.",
                context={"flow_id": flow_id},
            )
        return clock

    def head(self, flow_id: str) -> Stage1LifecycleHeadTuple | None:
        """Strongly read one lifecycle ``HEAD`` at its status flow partition."""
        item = self._get_record(f"{STATUS_FLOW_PREFIX}{flow_id}", HEAD_RECORD_KEY)
        if item is None:
            return None
        return Stage1LifecycleHeadTuple(
            surface=Stage1Surface(_required_text(item, "surface")),
            flow_id=_required_text(item, "flow_id"),
            current_execution_id=_required_text(item, "current_execution_id"),
            current_internal_execution_id=_required_text(item, "current_internal_execution_id"),
            predecessor_execution_id=_text(item, "predecessor_execution_id"),
            latest_event_id=_required_text(item, "latest_event_id"),
            latest_authority_digest=_required_text(item, "latest_authority_digest"),
            latest_transition_sequence=_required_number(item, "latest_transition_sequence"),
            latest_state=FlowState(_required_text(item, "latest_state")),
            parent_flow_id=_text(item, "parent_flow_id"),
            causation_event_id=_text(item, "causation_event_id"),
            head_contract_version=_required_number(item, "head_contract_version"),
        )

    def current_item(self, partition_identity: str) -> dict[str, object] | None:
        """Strongly read one canonical-partition ``CURRENT`` pointer as a plain item."""
        return self._get_record(partition_identity, "CURRENT")

    # -- lease/fence/attempt raw observations ------------------------------------------------------

    def lease(self, table_id: str) -> TableLeaseRecord | None:
        """Strongly read one ``TABLE#`` lease record from the leases table."""
        item = self._get(
            self._leases, {"partition_key": {"S": table_lease_partition_key(table_id)}}
        )
        if item is None:
            return None
        return TableLeaseRecord(
            physical_table_id=table_id,
            holder=_required_text(item, "holder"),
            fencing_token=_required_number(item, "fencing_token"),
            expires_at=_required_text(item, "expires_at"),
            parked_at=_text(item, "parked_at"),
        )

    def fence(self, table_id: str) -> FenceObservation | None:
        """Strongly read one ``FENCE`` as the raw observation the coordinator conditions."""
        partition = table_attempt_partition_key(table_id)
        item = self._get_record(partition, "FENCE")
        if item is None:
            return None
        raw: dict[str, str | int | bool | None] = {}
        for name in _RECONCILIATION_FIELDS:
            value = item.get(name)
            raw[name] = value if isinstance(value, str | int | bool) else None
        return FenceObservation(
            partition_key=_required_text(item, "partition_key"),
            record_key=_required_text(item, "record_key"),
            state=_text(item, "state"),
            physical_table_id=_text(item, "physical_table_id"),
            attempt_id=_text(item, "attempt_id"),
            lease_holder=_text(item, "lease_holder"),
            fencing_token=_number(item, "fencing_token"),
            parked_at=_text(item, "parked_at"),
            reconciliation=ReconciliationObservation(
                reconcile_status=raw["reconcile_status"],
                reconcile_after_or_block=raw["reconcile_after_or_block"],
                reconcile_attempt_count=raw["reconcile_attempt_count"],
                requeued_from_case_ref=raw["requeued_from_case_ref"],
                requeued_at=raw["requeued_at"],
                reconcile_block_id=raw["reconcile_block_id"],
                reconcile_block_reason=raw["reconcile_block_reason"],
                reconcile_blocked_at=raw["reconcile_blocked_at"],
                reconcile_observation_digest=raw["reconcile_observation_digest"],
                reconcile_recovery_ref=raw["reconcile_recovery_ref"],
            ),
        )

    def run_item(self, table_id: str, run_id: str) -> dict[str, object] | None:
        """Strongly read one inline ``TABLE_ATTEMPT#<table>/RUN#<run id>`` as its raw plain item.

        Unconditioned: the inline driver alone decides replay versus creation from the bytes.
        """
        return self._get_record(table_attempt_partition_key(table_id), run_record_key(run_id))

    def attempt_item(self, table_id: str, attempt_id: str) -> dict[str, object] | None:
        """Strongly read one ``ATTEMPT#`` as its raw plain item."""
        partition = table_attempt_partition_key(table_id)
        return self._get_record(partition, attempt_record_key(attempt_id))

    def settled_references(self, plan_key: str) -> tuple[SettledAttemptReference, ...]:
        """Strongly read every settled attempt reference this plan's units carry, in §5 order.

        The order is ascending unsigned UTF-8 bytes of ``(physical_table_id, attempt_id)``: the
        same order the coordinator's settled-attempt digest consumes.
        """
        references: list[SettledAttemptReference] = []
        for item in self.plan_unit_items(plan_key):
            unit_key = _text(item, "unit_key")
            raw = item.get("attempt_references")
            if unit_key is None or not isinstance(raw, list):
                continue
            for attempt_id in cast("list[object]", raw):
                if not isinstance(attempt_id, str):
                    continue
                attempt_item = self.attempt_item(unit_key, attempt_id)
                if attempt_item is None or attempt_item.get("athena_usage_state") != "settled":
                    continue
                references.append(
                    SettledAttemptReference(
                        unit_key, attempt_id, _required_text(attempt_item, "usage_authority_digest")
                    )
                )
        return tuple(
            sorted(
                references,
                key=lambda ref: (
                    ref.physical_table_id.encode("utf-8"),
                    ref.attempt_id.encode("utf-8"),
                ),
            )
        )

    def replacement_evidence(self, plan_key: str) -> SourcePlanReplacementEvidence:
        """Strongly read the complete source evidence one planned-plan replacement conditions.

        Every source unit item, every settled accepted attempt, and each attempt's byte-identical
        ``USAGE#`` authority are read whole; the request type rejects any aggregate mismatch.
        """
        meta = self.plan_meta(plan_key)
        if meta is None:
            raise InvariantError(
                _MALFORMED,
                "The owner-active source plan META is absent.",
                context={"plan_key": plan_key},
            )
        attempts: list[SettledAttemptObservation] = []
        usage: list[SettledUsageAuthorityObservation] = []
        for reference in self.settled_references(plan_key):
            attempt_item = self.attempt_item(reference.physical_table_id, reference.attempt_id)
            usage_item = self.usage_item(reference.physical_table_id, reference.attempt_id)
            if attempt_item is None or usage_item is None:
                raise InvariantError(
                    _MALFORMED,
                    "A settled source attempt lost its ATTEMPT# or USAGE# authority.",
                    context={"plan_key": plan_key},
                )
            attempts.append(
                SettledAttemptObservation(
                    reference.physical_table_id,
                    reference.attempt_id,
                    _required_text(attempt_item, "state"),
                    reference.usage_authority_digest,
                    attempt_item,
                )
            )
            usage.append(
                SettledUsageAuthorityObservation(
                    reference.physical_table_id,
                    reference.attempt_id,
                    _required_text(usage_item, "payload"),
                    _required_text(usage_item, "immutable_payload_digest"),
                    usage_item,
                )
            )
        return SourcePlanReplacementEvidence(
            plan_key=plan_key,
            meta=meta,
            unit_items=self.plan_unit_items(plan_key),
            attempts=tuple(attempts),
            usage_authorities=tuple(usage),
        )

    def attempt_owner(self, attempt: AttemptObservation) -> TableAttemptOwnerSnapshot | None:
        """Strongly read the one owner item an attempt's immutable owner reference names.

        A queued attempt names its plan key (the unit is ``UNIT#<table>`` under it); an inline
        attempt names its run id. Absence is ``None``; the caller decides what absence means.
        """
        table_id = attempt.physical_table_id
        if table_id is None:
            return None
        if attempt.plan_unit_owner_ref is not None:
            item = self.unit_item(attempt.plan_unit_owner_ref, table_id)
            if item is None:
                return None
            return QueuedUnitAttemptOwner(attempt.plan_unit_owner_ref, table_id, item)
        if attempt.inline_run_owner_ref is not None:
            item = self.run_item(table_id, attempt.inline_run_owner_ref)
            if item is None:
                return None
            return InlineRunAttemptOwner(table_id, attempt.inline_run_owner_ref, item)
        return None

    def usage_item(self, table_id: str, attempt_id: str) -> dict[str, object] | None:
        """Strongly read one settled attempt's immutable ``USAGE#`` authority as a plain item.

        Planned-plan replacement conditions every source attempt's byte-identical
        settlement authority; the coordinator owns the ``USAGE#`` key form.
        """
        partition = table_attempt_partition_key(table_id)
        return self._get_record(partition, f"{USAGE_RECORD_PREFIX}{attempt_id}")

    def attempt(self, table_id: str, attempt_id: str) -> AttemptObservation | None:
        """Strongly read one ``ATTEMPT#`` as the raw observation the coordinator conditions."""
        item = self.attempt_item(table_id, attempt_id)
        if item is None:
            return None
        fields: dict[str, object] = {
            "partition_key": _required_text(item, "partition_key"),
            "record_key": _required_text(item, "record_key"),
        }
        for name in _ATTEMPT_TEXT_FIELDS:
            fields[name] = _text(item, name)
        for name in _ATTEMPT_INT_FIELDS:
            fields[name] = _number(item, name)
        evidence = item.get("sanitized_start_evidence")
        if isinstance(evidence, dict):
            fields["sanitized_start_evidence"] = parse_sanitized_evidence(
                cast("Mapping[str, object]", evidence)
            )
        else:
            fields["sanitized_start_evidence"] = None
        return AttemptObservation(**cast("dict[str, object]", fields))  # type: ignore[arg-type]


def plan_meta_from_item(item: Mapping[str, object]) -> PlanMeta:
    """Rebuild the closed plan ``META`` typed form from one strongly read plain item."""
    raw_expectations = item.get("required_child_expectations")
    if not isinstance(raw_expectations, list):
        raise _malformed("plan META retains its required-child expectation members.")
    members: list[RequiredChildExpectation] = []
    for member in cast("list[object]", raw_expectations):
        if not isinstance(member, dict):
            raise _malformed("a required-child expectation member is a two-field map.")
        mapped = cast("Mapping[str, object]", member)
        members.append(
            RequiredChildExpectation(
                child_flow_id=_required_text(mapped, "child_flow_id"),
                owner_generation=_required_number(mapped, "owner_generation"),
            )
        )
    return PlanMeta(
        plan_digest=_required_text(item, "plan_digest"),
        internal_execution_id=_required_text(item, "internal_execution_id"),
        root_plan_key=_required_text(item, "root_plan_key"),
        generation=_required_number(item, "generation"),
        source_plan_key=_text(item, "source_plan_key"),
        origin_intent_id=_required_text(item, "origin_intent_id"),
        roster_digest=_required_text(item, "roster_digest"),
        origin=IntentOrigin(
            IntentOriginKind(_required_text(item, "origin_kind")),
            _required_text(item, "origin_member"),
        ),
        status=PlanStatus(_required_text(item, "status")),
        unit_count=_required_number(item, "unit_count"),
        admitted_count=_required_number(item, "admitted_count"),
        attested_count=_required_number(item, "attested_count"),
        failed_count=_required_number(item, "failed_count"),
        child_outcome_reduction_version=_required_number(item, "child_outcome_reduction_version"),
        required_child_expectations=tuple(members),
        settled_attempt_count=_required_number(item, "settled_attempt_count"),
        settled_attempt_digest=_text(item, "settled_attempt_digest"),
        terminal_at=_text(item, "terminal_at"),
        created_at=_required_text(item, "created_at"),
        plan_reservation_count=_number(item, "plan_reservation_count") or 0,
    )
