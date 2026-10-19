# SPDX-License-Identifier: MIT-0
"""Translate between domain values and DynamoDB's typed attribute shape.

The low-level client takes `{"S": "text"}` rather than `"text"`, and every value comes back the
same way. Kept in one pure module so the mapping is assertable without a table, and so there is
one answer to "how is a date stored" rather than one per adapter.

Three choices worth stating, because each has a wrong-looking alternative:

- **A date and a timestamp are stored as ISO-8601 strings, not numbers.** DynamoDB has no
  temporal type, so the choice is between a string that sorts correctly and an epoch number that
  also sorts correctly. The string wins because a human reading the item can see what it means,
  and because a date and a timestamp then look different in the store rather than both being
  integers.
- **A timestamp is stored in UTC with its offset.** `datetime.fromisoformat` round-trips it
  exactly, and a naive value read back would compare unequal to an aware one — Python raises
  rather than guessing, so a dropped offset surfaces as a crash on the next comparison rather
  than as a wrong answer.
- **A count is stored as a number and read back as an `int`.** DynamoDB returns every number as
  a string, so reading one without converting gives `"200"`, which compares wrongly and
  concatenates instead of adding.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — kept out of the Lambda import path
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

__all__ = [
    "ItemError",
    "read_bool",
    "read_date",
    "read_int",
    "read_moment",
    "read_text",
    "read_text_map",
    "write_bool",
    "write_date",
    "write_int",
    "write_moment",
    "write_text",
    "write_text_map",
]


class ItemError(Exception):
    """A stored item is not the shape this adapter writes."""


def write_text(value: str) -> AttributeValueTypeDef:
    """Store a string."""
    return {"S": value}


def write_int(value: int) -> AttributeValueTypeDef:
    """Store a whole number. DynamoDB carries every number as text on the wire."""
    return {"N": str(value)}


def write_bool(value: bool) -> AttributeValueTypeDef:
    """Store a boolean as `BOOL` rather than as a string, so a condition can compare it."""
    return {"BOOL": value}


def write_date(value: date) -> AttributeValueTypeDef:
    """Store a calendar date as its ISO form, which sorts correctly as a string."""
    return {"S": value.isoformat()}


def write_moment(value: datetime) -> AttributeValueTypeDef:
    """Store an instant in UTC, offset included.

    Normalised rather than stored as delivered, so two items written from different machines are
    comparable as strings — which is what lets the store, not just the caller, order them.
    """
    if value.tzinfo is None:
        raise ItemError("a stored instant must be timezone-aware.")
    return {"S": value.astimezone(UTC).isoformat()}


def write_text_map(values: Mapping[str, str]) -> AttributeValueTypeDef:
    """Store a flat string-to-string mapping as `M`.

    A map rather than a list of pairs, because every use of this is a lookup by key and a list
    would have to be scanned. And `M` rather than a JSON string, because a JSON string is a second
    encoding inside the store: it would need parsing on read, it cannot be projected, and a
    malformed one fails somewhere other than at the attribute that holds it.

    Flat by design. A nested map is a shape this module would have to describe recursively, and
    every value it has been asked for so far is a single string — an objective kind, a channel key.
    A second level is a signal that the thing being stored wants its own item.
    """
    return {"M": {key: {"S": value} for key, value in values.items()}}


Item = dict[str, "AttributeValueTypeDef"]


def read_text(item: Item, name: str) -> str:
    """Read a required string, failing by name rather than returning an empty one."""
    found = item.get(name, {}).get("S")
    if found is None:
        raise ItemError(f"{name} is missing or not a string.")
    return found


def read_int(item: Item, name: str, *, default: int = 0) -> int:
    """Read a number as an `int`.

    Defaulted because a count added to the schema later is legitimately absent from an item
    written before it existed — and zero is the honest reading of "this load did not record one",
    which is different from a null the caller has to branch on.
    """
    found = item.get(name, {}).get("N")
    return default if found is None else int(found)


def read_bool(item: Item, name: str) -> bool:
    """Read a boolean, treating absence as false."""
    return bool(item.get(name, {}).get("BOOL", False))


def read_date(item: Item, name: str) -> date:
    """Read a calendar date back from its ISO form."""
    return date.fromisoformat(read_text(item, name))


def read_text_map(item: Item, name: str) -> dict[str, str]:
    """Read a flat string-to-string mapping, treating absence as empty.

    Absence is empty rather than an error because the attribute means "the exceptions", and having
    none is the ordinary case — an account that configures no channel exception is the one ADR-011
    expects. An entry whose value is not a string fails by name instead, because that is a shape
    this module did not write.
    """
    found = item.get(name, {}).get("M")
    if found is None:
        return {}
    read: dict[str, str] = {}
    for key, value in found.items():
        text = value.get("S")
        if text is None:
            raise ItemError(f"{name}[{key}] is not a string.")
        read[key] = text
    return read


def read_moment(item: Item, name: str) -> datetime:
    """Read an instant back as timezone-aware.

    A naive value here would compare unequal to every aware one in the domain, and Python raises
    on that comparison rather than guessing — so this is the boundary where the offset has to be
    restored or the failure moves somewhere unrelated.
    """
    moment = datetime.fromisoformat(read_text(item, name))
    if moment.tzinfo is None:
        raise ItemError(f"{name} was stored without a timezone offset.")
    return moment.astimezone(UTC)
