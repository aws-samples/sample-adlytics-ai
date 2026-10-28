# SPDX-License-Identifier: MIT-0
"""Typed transaction members for the execution-graph table (S3; S1A-10).

Every member carries the domain signal its condition stands for. When DynamoDB cancels a
transaction only for condition failures, the first failed member's signal is raised as a
`GraphGuardError`, so a race surfaces as its exact typed guard rather than a generic
"state changed" (S1A-10). A member may also ask for the item's old image on failure and
resolve the signal from it: a `RUN#` update that failed because the run carries a `grant_id`
is `RUN_GRANT_PENDING_APPLY`, not a stale revision. Two hooks refine a condition cancellation:
a member's `resolve` refines its one signal, and a `TransactRequest`'s `refine` classifies the
whole failed set (every failed member with its request item and old image), which `_guard`
cannot, since it stops at the first failed member. A `TransactionConflict` (a concurrent
transaction on one of the items) is raised as `GraphTransactionConflictError`, which the claim
path retries within its bound and counts (R2-7). Anything else is `GraphStoreUnavailableError`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from botocore.exceptions import BotoCoreError, ClientError

from domain.operations.execution_graph_dispatch import GraphGuardError
from ports.outbound.execution_graph_dispatch import (
    GraphStoreUnavailableError,
    GraphTransactionConflictError,
)

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef, TransactWriteItemTypeDef

__all__ = [
    "FailedMember",
    "Member",
    "MemberSpec",
    "TransactRequest",
    "check",
    "delete",
    "put",
    "transact",
    "update",
]

type Item = dict[str, AttributeValueTypeDef]
type Values = dict[str, AttributeValueTypeDef]

_CANCELLED: Final = "TransactionCanceledException"
_CONDITION: Final = "ConditionalCheckFailed"
_CONFLICT: Final = "TransactionConflict"
_NAMES: Final = {
    "#state": "state",
    "#mode": "mode",
    "#hold": "hold",
    "#scope": "scope",
    "#permitted": "permitted",
    # `domain` is a DynamoDB reserved word; a bare use rejects the whole transaction.
    "#domain": "domain",
}


@dataclass(frozen=True, slots=True)
class Member:
    """One transaction member and the typed signal its failed condition maps to."""

    item: TransactWriteItemTypeDef
    signal: str
    resolve: Callable[[Item | None], str] | None = None
    """Map the member's old image (returned on failure) to a finer signal."""


@dataclass(frozen=True, slots=True)
class MemberSpec:
    """The parts every member shares: its table, key, condition and failure signal."""

    table: str
    key: Item
    signal: str
    condition: str | None = None
    values: Values | None = None
    resolve: Callable[[Item | None], str] | None = None


@dataclass(frozen=True, slots=True)
class FailedMember:
    """One member whose condition failed in a cancelled transaction."""

    index: int
    item: TransactWriteItemTypeDef
    """`members[index].item`, exactly as sent."""
    old: Item | None
    """The item's old image (`CancellationReason.Item`), when the member asked for it."""


@dataclass(frozen=True, slots=True)
class TransactRequest:
    """One atomic graph transaction: its members, their bound, and an opt-in refinement."""

    members: list[Member]
    bound: int
    refine: Callable[[tuple[FailedMember, ...]], GraphGuardError | None] | None = None
    """Classify the whole failed set of an all-condition cancellation; None keeps `_guard`."""

    def __post_init__(self) -> None:
        if not self.members or len(self.members) > self.bound:
            raise GraphStoreUnavailableError("a graph transaction is empty or exceeds its bound.")


def _body(spec: MemberSpec, text: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"TableName": spec.table}
    expressions = " ".join(part for part in (text, spec.condition) if part)
    if spec.condition is not None:
        body["ConditionExpression"] = spec.condition
    if spec.values:
        body["ExpressionAttributeValues"] = dict(spec.values)
    names = {name: value for name, value in _NAMES.items() if name in expressions}
    if names:
        body["ExpressionAttributeNames"] = names
    if spec.resolve is not None:
        body["ReturnValuesOnConditionCheckFailure"] = "ALL_OLD"
    return body


def put(spec: MemberSpec, item: Item) -> Member:
    """Build a conditioned put; the key is taken from the item."""
    body = _body(spec, None)
    body["Item"] = item
    return Member(cast("TransactWriteItemTypeDef", {"Put": body}), spec.signal, spec.resolve)


def update(spec: MemberSpec, expression: str) -> Member:
    """Build a conditioned update."""
    body = _body(spec, expression)
    body["Key"] = spec.key
    body["UpdateExpression"] = expression
    return Member(cast("TransactWriteItemTypeDef", {"Update": body}), spec.signal, spec.resolve)


def delete(spec: MemberSpec) -> Member:
    """Build a conditioned (or unconditional) delete."""
    body = _body(spec, None)
    body["Key"] = spec.key
    return Member(cast("TransactWriteItemTypeDef", {"Delete": body}), spec.signal, spec.resolve)


def check(spec: MemberSpec) -> Member:
    """Build a ConditionCheck; it must carry a condition."""
    if spec.condition is None:
        raise ValueError("a ConditionCheck needs a condition.")
    body = _body(spec, None)
    body["Key"] = spec.key
    return Member(
        cast("TransactWriteItemTypeDef", {"ConditionCheck": body}), spec.signal, spec.resolve
    )


def _guard(members: list[Member], reasons: list[dict[str, Any]]) -> GraphGuardError:
    for member, reason in zip(members, reasons, strict=False):
        if str(reason.get("Code", "None")) != _CONDITION:
            continue
        if member.resolve is not None:
            old = reason.get("Item")
            return GraphGuardError(member.resolve(None if old is None else dict(old)))
        return GraphGuardError(member.signal)
    return GraphGuardError(members[0].signal)


def _refused(request: TransactRequest, reasons: list[dict[str, Any]]) -> GraphGuardError:
    """Return an all-condition cancellation's guard: the request's refinement when it has one.

    The refinement sees every failed member only when DynamoDB returned one reason per member;
    otherwise, or when it returns None, the first failed member's signal is raised as before.
    """
    if request.refine is not None and len(reasons) == len(request.members):
        failed = tuple(
            FailedMember(
                index,
                request.members[index].item,
                None if reason.get("Item") is None else dict(reason["Item"]),
            )
            for index, reason in enumerate(reasons)
            if str(reason.get("Code", "None")) == _CONDITION
        )
        refined = request.refine(failed)
        if refined is not None:
            return refined
    return _guard(request.members, reasons)


def transact(client: DynamoDBClient, request: TransactRequest) -> None:
    """Commit the members atomically, or raise the typed guard, conflict or failure."""
    members = request.members
    try:
        client.transact_write_items(TransactItems=[member.item for member in members])
    except ClientError as error:
        response = cast("dict[str, Any]", error.response)
        if response.get("Error", {}).get("Code") == _CANCELLED:
            reasons = [dict(item) for item in response.get("CancellationReasons", [])]
            codes = {str(reason.get("Code", "None")) for reason in reasons} - {"None", ""}
            if _CONFLICT in codes:
                raise GraphTransactionConflictError(_CONFLICT) from None
            if codes == {_CONDITION}:
                raise _refused(request, reasons) from None
        raise GraphStoreUnavailableError(
            f"a graph transaction failed: {response.get('Error', {}).get('Code')}"
        ) from None
    except BotoCoreError as error:
        raise GraphStoreUnavailableError(
            f"a graph transaction failed: {type(error).__name__}"
        ) from None
