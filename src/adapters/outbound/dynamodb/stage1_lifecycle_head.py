# SPDX-License-Identifier: MIT-0
"""Conditional ADR-075 Stage-1 lifecycle head and event-authority persistence.

Owns the ``STATUS_FLOW#``/``HEAD`` item shape and the retained lifecycle ``EVENT#`` write side of
the schema whose read side is ``_retained_event_request`` in
``adapters.outbound.dynamodb.stage1_operational_status``; that reader is imported here and in the
unit suite so writer and reader can never drift apart silently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import boto3
from botocore.exceptions import ClientError

from adapters.outbound.dynamodb.items import (
    ItemError,
    read_int,
    read_text,
    write_int,
    write_text,
)
from adapters.outbound.dynamodb.stage1_operational_status import (
    _conditional,  # pyright: ignore[reportPrivateUsage] -- shared conditional-failure decode
    _retained_event_request,  # pyright: ignore[reportPrivateUsage] -- reader half of this schema
)
from domain.operations.stage1_lifecycle_head import (
    EVENT_RECORD_PREFIX,
    Stage1LifecycleEventFragment,
    Stage1LifecycleHeadInvariantError,
    Stage1LifecycleHeadTuple,
    Stage1LifecycleProof,
    Stage1PersistedEventObservation,
    Stage1ReplayClassification,
    Stage1ReplayObservation,
    classify_stage1_replay,
)
from domain.operations.stage1_operational_status import (
    InlineIdentityRequest,
    PlanIdentityRequest,
    PointerIdentityRequest,
    Stage1AuthorityKind,
    Stage1Surface,
    UnitIdentityRequest,
    VerifiedStage1EventAuthority,
    reattest_stage1_event,
)
from domain.operations.status import FlowState, OperationalStatusError
from ports.outbound.stage1_lifecycle_head import (
    Stage1LifecycleApplyKind,
    Stage1LifecycleApplyResult,
    Stage1LifecycleHeadStorePort,
    Stage1LifecycleTransactionItems,
)

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef, TransactWriteItemTypeDef

type Item = dict[str, "AttributeValueTypeDef"]

_EVENT_REPLAY_CONDITION: Final = (
    "attribute_not_exists(partition_key) OR "
    "(payload = :expected_payload AND immutable_payload_digest = :expected_digest)"
)
_NEW_HEAD_CONDITION: Final = "attribute_not_exists(partition_key)"


def _head_text_fields(head: Stage1LifecycleHeadTuple) -> tuple[tuple[str, str], ...]:
    return (
        ("surface", head.surface.value),
        ("flow_id", head.flow_id),
        ("current_execution_id", head.current_execution_id),
        ("current_internal_execution_id", head.current_internal_execution_id),
        ("latest_event_id", head.latest_event_id),
        ("latest_authority_digest", head.latest_authority_digest),
        ("latest_state", head.latest_state.value),
    )


def _head_optional_values(head: Stage1LifecycleHeadTuple) -> tuple[tuple[str, str | None], ...]:
    return (
        ("predecessor_execution_id", head.predecessor_execution_id),
        ("parent_flow_id", head.parent_flow_id),
        ("causation_event_id", head.causation_event_id),
    )


class DynamoStage1LifecycleHeadStore(Stage1LifecycleHeadStorePort):
    """Persist one proof's event authority and head atomically; classify races, never adopt."""

    def __init__(self, publications_table: str, client: DynamoDBClient | None = None) -> None:
        if not publications_table.strip():
            raise ValueError("Stage-1 lifecycle head store requires the publications table")
        self._publications = publications_table
        self._client = client or boto3.client("dynamodb")

    def transaction_items(self, proof: Stage1LifecycleProof) -> Stage1LifecycleTransactionItems:
        """Return the conditioned fragments a coordinator embeds in its own transaction."""
        return Stage1LifecycleTransactionItems(
            event_puts=tuple(self._event_put(proof, fragment) for fragment in proof.events),
            head_write=self._head_write(proof),
        )

    def apply(self, proof: Stage1LifecycleProof) -> Stage1LifecycleApplyResult:
        """Run exactly the proof's fragments as one transaction for a single-member owner."""
        preread = tuple(
            self._get(proof.partition_key, fragment.record_key) for fragment in proof.events
        )
        if any(item is not None for item in preread):
            return self._classified(proof)
        try:
            self._client.transact_write_items(TransactItems=self._transaction(proof))
        except ClientError as error:
            if not _conditional(error):
                raise
            return self._classified(proof)
        return Stage1LifecycleApplyResult(Stage1LifecycleApplyKind.APPLIED)

    def _transaction(self, proof: Stage1LifecycleProof) -> list[TransactWriteItemTypeDef]:
        members = [self._event_put(proof, fragment) for fragment in proof.events]
        head = self._head_write(proof)
        if head is not None:
            members.append(head)
        return members

    def _event_put(
        self, proof: Stage1LifecycleProof, fragment: Stage1LifecycleEventFragment
    ) -> TransactWriteItemTypeDef:
        return {
            "Put": {
                "TableName": self._publications,
                "Item": _event_item(proof.partition_key, fragment),
                "ConditionExpression": _EVENT_REPLAY_CONDITION,
                "ExpressionAttributeValues": {
                    ":expected_payload": write_text(fragment.payload),
                    ":expected_digest": write_text(fragment.outbox.immutable_payload_digest),
                },
            }
        }

    def _head_write(self, proof: Stage1LifecycleProof) -> TransactWriteItemTypeDef | None:
        if proof.head_record_key is None or proof.resulting_head is None:
            return None
        item = _head_item(proof.partition_key, proof.head_record_key, proof.resulting_head)
        if proof.prior_head is None:
            return {
                "Put": {
                    "TableName": self._publications,
                    "Item": item,
                    "ConditionExpression": _NEW_HEAD_CONDITION,
                }
            }
        condition, values = _prior_head_condition(proof.prior_head)
        return {
            "Put": {
                "TableName": self._publications,
                "Item": item,
                "ConditionExpression": condition,
                "ExpressionAttributeValues": values,
            }
        }

    def _classified(self, proof: Stage1LifecycleProof) -> Stage1LifecycleApplyResult:
        classification = classify_stage1_replay(proof, self._observation(proof))
        if classification is Stage1ReplayClassification.BYTE_IDENTICAL:
            return Stage1LifecycleApplyResult(Stage1LifecycleApplyKind.REPLAYED_BYTE_IDENTICAL)
        if classification is Stage1ReplayClassification.ADVANCED_THROUGH_CONTIGUOUS_CHAIN:
            return Stage1LifecycleApplyResult(Stage1LifecycleApplyKind.REPLAYED_AFTER_ADVANCE)
        raise Stage1LifecycleHeadInvariantError(
            "Stage-1 lifecycle head transaction lost to a conflicting committed winner"
        )

    def _observation(self, proof: Stage1LifecycleProof) -> Stage1ReplayObservation:
        events: list[Stage1PersistedEventObservation | None] = []
        for fragment in proof.events:
            item = self._get(proof.partition_key, fragment.record_key)
            events.append(None if item is None else _event_observation(item))
        head: Stage1LifecycleHeadTuple | None = None
        if proof.head_record_key is not None:
            head_item = self._get(proof.partition_key, proof.head_record_key)
            head = None if head_item is None else _head_from_item(head_item)
        later: tuple[VerifiedStage1EventAuthority, ...] = ()
        if (
            head is not None
            and proof.resulting_head is not None
            and head.latest_transition_sequence > proof.resulting_head.latest_transition_sequence
        ):
            later = self._chain(
                proof.partition_key,
                proof.resulting_head.latest_transition_sequence,
                head,
            )
        return Stage1ReplayObservation(tuple(events), head, later)

    def _chain(
        self, partition_key: str, first: int, head: Stage1LifecycleHeadTuple
    ) -> tuple[VerifiedStage1EventAuthority, ...]:
        verified: list[VerifiedStage1EventAuthority] = []
        for item in self._event_items(partition_key):
            try:
                authority = reattest_stage1_event(_retained_event_request(item))
            except OperationalStatusError as error:
                raise Stage1LifecycleHeadInvariantError(
                    "retained Stage-1 lifecycle chain event is invalid"
                ) from error
            if first < authority.transition_sequence <= head.latest_transition_sequence:
                verified.append(authority)
        return tuple(sorted(verified, key=lambda link: link.transition_sequence))

    def _event_items(self, partition_key: str) -> list[Item]:
        items: list[Item] = []
        start_key: Item | None = None
        while True:
            if start_key is None:
                response = self._client.query(
                    TableName=self._publications,
                    KeyConditionExpression=(
                        "partition_key = :partition AND begins_with(record_key, :prefix)"
                    ),
                    ExpressionAttributeValues={
                        ":partition": write_text(partition_key),
                        ":prefix": write_text(EVENT_RECORD_PREFIX),
                    },
                    ConsistentRead=True,
                )
            else:
                response = self._client.query(
                    TableName=self._publications,
                    KeyConditionExpression=(
                        "partition_key = :partition AND begins_with(record_key, :prefix)"
                    ),
                    ExpressionAttributeValues={
                        ":partition": write_text(partition_key),
                        ":prefix": write_text(EVENT_RECORD_PREFIX),
                    },
                    ConsistentRead=True,
                    ExclusiveStartKey=start_key,
                )
            items.extend(dict(found) for found in response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                return items
            start_key = dict(response["LastEvaluatedKey"])

    def _get(self, partition_key: str, record_key: str) -> Item | None:
        response = self._client.get_item(
            TableName=self._publications,
            Key={"partition_key": write_text(partition_key), "record_key": write_text(record_key)},
            ConsistentRead=True,
        )
        found = response.get("Item")
        return None if found is None else dict(found)


def _event_observation(item: Item) -> Stage1PersistedEventObservation:
    try:
        return Stage1PersistedEventObservation(
            reattest=_retained_event_request(item),
            payload=read_text(item, "payload"),
            immutable_payload_digest=read_text(item, "immutable_payload_digest"),
        )
    except (ItemError, OperationalStatusError) as error:
        raise Stage1LifecycleHeadInvariantError(
            "retained Stage-1 lifecycle event is not this writer's shape"
        ) from error


def _event_item(partition_key: str, fragment: Stage1LifecycleEventFragment) -> Item:
    reattest = fragment.reattest
    authority = reattest.authority
    proof = reattest.proof
    outbox = fragment.outbox
    if outbox.outbox_order is None:
        raise Stage1LifecycleHeadInvariantError("pending lifecycle authority has no outbox order")
    item: Item = {
        "partition_key": write_text(partition_key),
        "record_key": write_text(fragment.record_key),
        "surface": write_text(authority.surface.value),
        "internal_key": {
            "L": [
                write_text(member) if isinstance(member, str) else write_int(member)
                for member in authority.internal_key
            ]
        },
        "transition_id": write_text(authority.transition_id),
        "lifecycle_state": write_text(authority.lifecycle_state.value),
        "authority_time": write_text(authority.authority_time),
        "flow_id": write_text(proof.flow_id),
        "execution_id": write_text(proof.execution_id),
        "event_id": write_text(proof.event_id),
        "transition_sequence": write_int(proof.transition_sequence),
        "immutable_payload_digest": write_text(outbox.immutable_payload_digest),
        "payload": write_text(fragment.payload),
        "authority_kind": write_text(Stage1AuthorityKind.LIFECYCLE_EVENT.value),
        "outbox_state": write_text(outbox.outbox_state.value),
        "outbox_status": write_text(outbox.outbox_state.value),
        "outbox_order": write_text(outbox.outbox_order),
        "consecutive_failed_delivery_attempts": write_int(
            outbox.consecutive_failed_delivery_attempts
        ),
    }
    if authority.completion_outcome is not None:
        item["completion_outcome"] = write_text(authority.completion_outcome.value)
    if authority.typed_reason is not None:
        item["typed_reason"] = write_text(authority.typed_reason)
    for name in (
        "predecessor_event_id",
        "predecessor_execution_id",
        "parent_flow_id",
        "causation_event_id",
    ):
        value: str | None = getattr(proof, name)
        if value is not None:
            item[name] = write_text(value)
    _add_identity_attributes(item, reattest.identity)
    if reattest.owner_generation is not None:
        item["owner_generation"] = write_int(reattest.owner_generation)
    expectations = reattest.required_child_expectations
    if expectations is not None:
        item["child_outcome_reduction_version"] = write_int(
            expectations.child_outcome_reduction_version
        )
        item["required_child_expectations"] = {
            "L": [
                {
                    "M": {
                        "child_flow_id": write_text(expectation.child_flow_id),
                        "owner_generation": write_int(expectation.owner_generation),
                    }
                }
                for expectation in expectations.required_child_expectations
            ]
        }
    return item


def _add_identity_attributes(
    item: Item,
    identity: PlanIdentityRequest
    | UnitIdentityRequest
    | InlineIdentityRequest
    | PointerIdentityRequest,
) -> None:
    if isinstance(identity, PlanIdentityRequest):
        item["plan_digest"] = write_text(identity.plan_digest)
        item["internal_execution_id"] = write_text(identity.internal_delivery_execution_id)
    elif isinstance(identity, UnitIdentityRequest):
        item["plan_digest"] = write_text(identity.plan_digest)
        item["internal_execution_id"] = write_text(identity.writer_execution_id)
    elif isinstance(identity, InlineIdentityRequest):
        item["internal_execution_id"] = write_text(identity.internal_execution_id)
    else:
        item["internal_pointer_execution_source_kind"] = write_text(
            identity.execution_source.kind.value
        )
        item["internal_pointer_execution_id"] = write_text(
            identity.execution_source.internal_pointer_execution_id
        )


def _head_item(partition_key: str, record_key: str, head: Stage1LifecycleHeadTuple) -> Item:
    item: Item = {
        "partition_key": write_text(partition_key),
        "record_key": write_text(record_key),
        "head_contract_version": write_int(head.head_contract_version),
        "latest_transition_sequence": write_int(head.latest_transition_sequence),
    }
    for name, value in _head_text_fields(head):
        item[name] = write_text(value)
    for name, optional in _head_optional_values(head):
        if optional is not None:
            item[name] = write_text(optional)
    return item


def _prior_head_condition(prior: Stage1LifecycleHeadTuple) -> tuple[str, Item]:
    terms = [
        "head_contract_version = :prior_head_contract_version",
        "latest_transition_sequence = :prior_latest_transition_sequence",
    ]
    values: Item = {
        ":prior_head_contract_version": write_int(prior.head_contract_version),
        ":prior_latest_transition_sequence": write_int(prior.latest_transition_sequence),
    }
    for name, text in _head_text_fields(prior):
        terms.append(f"{name} = :prior_{name}")
        values[f":prior_{name}"] = write_text(text)
    for name, value in _head_optional_values(prior):
        if value is None:
            terms.append(f"attribute_not_exists({name})")
        else:
            terms.append(f"{name} = :prior_{name}")
            values[f":prior_{name}"] = write_text(value)
    return " AND ".join(terms), values


def _head_from_item(item: Item) -> Stage1LifecycleHeadTuple:
    try:
        return Stage1LifecycleHeadTuple(
            surface=Stage1Surface(read_text(item, "surface")),
            flow_id=read_text(item, "flow_id"),
            current_execution_id=read_text(item, "current_execution_id"),
            current_internal_execution_id=read_text(item, "current_internal_execution_id"),
            predecessor_execution_id=_optional_text(item, "predecessor_execution_id"),
            latest_event_id=read_text(item, "latest_event_id"),
            latest_authority_digest=read_text(item, "latest_authority_digest"),
            latest_transition_sequence=_required_int(item, "latest_transition_sequence"),
            latest_state=FlowState(read_text(item, "latest_state")),
            parent_flow_id=_optional_text(item, "parent_flow_id"),
            causation_event_id=_optional_text(item, "causation_event_id"),
            head_contract_version=_required_int(item, "head_contract_version"),
        )
    except (ItemError, ValueError) as error:
        raise Stage1LifecycleHeadInvariantError(
            "retained Stage-1 lifecycle head is not the closed 12-field item"
        ) from error


def _optional_text(item: Item, name: str) -> str | None:
    return read_text(item, name) if name in item else None


def _required_int(item: Item, name: str) -> int:
    if name not in item:
        raise ItemError(f"{name} is missing or not a number.")
    return read_int(item, name)
