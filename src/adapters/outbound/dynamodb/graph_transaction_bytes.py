# SPDX-License-Identifier: MIT-0
"""The aggregate-size bound of one graph transaction, measured by the shared byte oracle.

Slice 7 (1b; FEAT-009 review F3). DynamoDB refuses a `TransactWriteItems` whose items exceed a
4 MB aggregate [verified: AWS DynamoDB API Reference, TransactWriteItems, read 2026-10-10]. The
unit batch writes (`arm_unit_batch`, `settle_unit_set`) touch up to 24 node items, each holding
an envelope of up to 256 KiB and, once settled, an effect record. So the action bound alone
can't keep them within the limit, and each one is bounded in bytes before it is built.

There is one measure: `Stage1DynamoByteOracle.conservative_service_item_bytes`, the oracle every
Stage-1 builder already uses. It is greater than or equal to the service-counted item size
(`byte_oracle.py`, §3.2). A target counts the larger of its pre-state and its result, as the
oracle's `TargetMeasurement` does:

- a `Put` counts its item exactly, and a pre-state only when its condition admits one;
- an `Update` counts its pre-state plus its expression values and its expression text, which
  covers every attribute name and value it can add or grow;
- a `Delete` or `ConditionCheck` counts its pre-state.

A pre-state comes from the item as read (`item_bytes`, recorded on the parsed record). A target
the writer did not read in this transaction (a claim, a `VERIFY#`, an `ARM#`, the counted
lock) is one of the fixed-shape items this adapter writes, keys, identities, timestamps and
small integers. It counts `UNREAD_ITEM_BYTES`, which a unit test pins above the largest such
item the whole chain writes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from boto3.dynamodb.types import TypeDeserializer

from adapters.outbound.dynamodb.byte_oracle import (
    MAX_AGGREGATE_TARGET_UPPER_BOUND_BYTES,
    Stage1DynamoByteOracle,
)

if TYPE_CHECKING:
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

    from adapters.outbound.dynamodb.graph_transactions import Member

__all__ = [
    "MAX_TRANSACTION_BYTES",
    "UNREAD_ITEM_BYTES",
    "ItemKey",
    "item_bytes",
    "item_key",
    "record_bytes",
    "transaction_bytes",
]

type Item = dict[str, AttributeValueTypeDef]
type ItemKey = tuple[str, str]

MAX_TRANSACTION_BYTES: Final = MAX_AGGREGATE_TARGET_UPPER_BOUND_BYTES
"""4,000,000: the oracle's admitted aggregate, below the service's 4 MB."""

UNREAD_ITEM_BYTES: Final = 8_192
"""The bound for a fixed-shape item not read in this transaction (pinned by a unit test)."""

_ORACLE: Final = Stage1DynamoByteOracle()
_DESERIALIZER: Final = TypeDeserializer()
_CREATE_ONLY: Final = "attribute_not_exists(pk)"


def item_bytes(item: Mapping[str, AttributeValueTypeDef]) -> int:
    """Measure one low-level item with the shared oracle."""
    plain = {name: _DESERIALIZER.deserialize(cast("Any", value)) for name, value in item.items()}
    return _ORACLE.conservative_service_item_bytes(plain)


def record_bytes(record: object) -> int:
    """Measure a parsed record that was not read from the table (its fields as attributes).

    Used only when a record carries no measured `item_bytes` (one built in memory), so a
    caller that never read the item still counts every field it holds.
    """
    if not is_dataclass(record) or isinstance(record, type):
        raise TypeError("a record measure takes a dataclass instance.")
    fields = {
        name: _plain_field(value)
        for name, value in asdict(record).items()
        if value is not None and value not in {(), frozenset()}
    }
    return _ORACLE.conservative_service_item_bytes(fields)


def _plain_field(value: object) -> object:
    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, tuple | list):
        return [str(entry) for entry in cast("Sequence[object]", value)]
    if isinstance(value, frozenset | set):
        return sorted(str(entry) for entry in cast("frozenset[object]", value))
    return str(value)


def item_key(body: Mapping[str, Any]) -> ItemKey:
    """Return a transaction member's `(pk, sk)`."""
    key = cast("Mapping[str, Mapping[str, str]]", body.get("Item") or body["Key"])
    return (str(key["pk"]["S"]), str(key["sk"]["S"]))


def transaction_bytes(members: Sequence[Member], read: Mapping[ItemKey, int]) -> int:
    """Return the transaction's aggregate upper bound: the sum of each target's maximum.

    `read` maps a target the writer read to its measured size; any other existing target
    counts `UNREAD_ITEM_BYTES`.
    """
    total = 0
    for member in members:
        ((kind, body),) = cast("dict[str, dict[str, Any]]", member.item).items()
        key = item_key(body)
        if kind == "Put":
            result = item_bytes(cast("Item", body["Item"]))
            replaces = body.get("ConditionExpression") != _CREATE_ONLY
            pre = read.get(key, UNREAD_ITEM_BYTES) if replaces else 0
            total += max(pre, result)
            continue
        pre = read.get(key, UNREAD_ITEM_BYTES)
        if kind == "Update":
            values = cast("Mapping[str, object]", body.get("ExpressionAttributeValues", {}))
            grown = _ORACLE.wire_json_bytes(dict(values)) + len(
                str(body["UpdateExpression"]).encode("utf-8")
            )
            total += pre + grown
            continue
        total += pre
    return total
