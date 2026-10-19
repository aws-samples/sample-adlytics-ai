# SPDX-License-Identifier: MIT-0
"""Turn a parsed source document into canonical rows, as one contract declares.

The half of reading that is a calculation rather than I/O. An adapter owns bytes, encodings
and the `csv`/`json` modules; this owns what a `source_path` *means* — which is where every
subtlety lives, and why it is testable with no file on disk.

Produces plain dicts keyed by canonical column, which the caller then validates against that
contract's generated Pydantic model. Two steps rather than one, because the model reports
every bad column at once with its name, and a mapper that validated as it went would fail on
the first.

**Additive tolerance is a rule here, not a convenience.** Both sources add fields without
notice, so an unrecognised key in the delivered document is ignored rather than rejected
(`data-contract-definition` steering). The reverse is not symmetric: a *declared* field that
is absent becomes `None`, and whether that is legitimate is already answered by the
contract — a nullable column says absence is expected, and a required one lets the row model
reject it by name.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, cast

from domain.data_contracts.contract import DataContract, FieldMapping, SourceFormat
from domain.data_contracts.schema import conformed_provenance_values
from domain.shared_kernel.values import FOUR_PLACES

__all__ = [
    "MappingError",
    "MappingRequest",
    "SourceDocument",
    "map_rows",
    "options_hash",
    "scaled",
    "suppression_flag",
]

MANIFEST_PREFIX = "manifest."

HASH_LENGTH = 16
"""Hex characters kept from an option-set digest.

`report_options_hash` is a partition key, so it becomes a directory name — a full 64-character
digest is unwieldy in a path and in a query predicate. 64 bits distinguishes a handful of
option combinations with room to spare.
"""


class MappingError(Exception):
    """A contract cannot be applied to the document it was given."""


def suppression_flag(value: object) -> bool:
    """Normalize the governed AMC discriminator without accepting unknown truthy text."""
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"", "false"}:
        return False
    if normalized in {"true", "values_removed"}:
        return True
    raise MappingError("suppression discriminator has an unknown value.")


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """What a reader produced from one delivered file, plus its manifest.

    `rows` for a flat format and `document` for a nested one — never both. The shape mirrors
    the fixture generator's `Fixture`, which is deliberate: the fixtures are what these
    readers read.
    """

    manifest: dict[str, Any] = field(default_factory=dict[str, Any])
    rows: tuple[dict[str, Any], ...] = ()
    document: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MappingRequest:
    """One document to be mapped through one contract."""

    contract: DataContract
    source: SourceDocument


def scaled(raw: object, divisor: int) -> Decimal:
    """Apply a monetary column's divisor, in decimal arithmetic throughout.

    `Decimal(str(raw))` rather than `Decimal(raw)` so an `int`, a `str` and a `Decimal` all
    work, and never `float(raw)` — a binary float cannot hold `11.37` exactly, and the error
    survives every later sum.

    **Rounded to four places with a stated mode.** A millicent value of `1234567` divides to
    `12.34567`, which is five places and more than `decimal(38,4)` holds, so something must
    round. Doing it here with `ROUND_HALF_EVEN` rather than letting the store truncate means
    the rule is visible and consistent: half-even avoids the systematic upward drift
    half-up would introduce across the thousands of rows these figures are summed over, and
    spend biased upward deflates every ROAS built on it.
    """
    if raw is None:
        raise MappingError("a monetary value cannot be scaled from nothing.")
    return (Decimal(str(raw)) / Decimal(divisor)).quantize(FOUR_PLACES, rounding=ROUND_HALF_EVEN)


def options_hash(value: object) -> str:
    """Digest an option set so one pull cannot be confused with another.

    `reportOptions` is dataset identity, not a runtime flag: the same `reportType` yields
    genuinely different effective schemas across pulls, and a MANUFACTURING pull summed with a
    SOURCING pull double-counts. This is what makes them distinguishable in a partition.

    `sort_keys` because a digest that depends on key order would change without the options
    changing. `default=str` because the reader parses JSON numbers as `Decimal`, which
    `json.dumps` cannot serialise.
    """
    canonical = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:HASH_LENGTH]


def delivered_name(mapping: FieldMapping) -> str:
    """Give the column name the delivered file uses (`FieldMapping.delivered_name` owns it)."""
    return mapping.delivered_name


def _walk(current: object, segment: str) -> object:
    """Follow one path segment, including an indexed one like `marketplaceIds[0]`."""
    if segment.endswith("]") and "[" in segment:
        key, _, raw_index = segment[:-1].partition("[")
        if not isinstance(current, dict):
            return None
        holder = cast("dict[str, object]", current).get(key)
        if not isinstance(holder, list):
            return None
        items = cast("list[object]", holder)
        index = int(raw_index)
        return items[index] if index < len(items) else None
    if isinstance(current, dict):
        return cast("dict[str, object]", current).get(segment)
    return None


def resolve(source: object, path: str) -> object:
    """Read a dotted path out of a parsed document, or None where it does not exist.

    None rather than raising, because absence is a legitimate outcome the contract already
    describes — `orderedRevenue` really is missing under a SOURCING pull.
    """
    current = source
    for segment in path.split("."):
        current = _walk(current, segment)
        if current is None:
            return None
    return current


def _object_valued_paths(contract: DataContract) -> frozenset[str]:
    """Find the paths that name a whole object rather than a scalar.

    A path that is a strict prefix of another mapped path is an object whose siblings are
    mapped individually: `reportSpecification.reportOptions` becomes `report_options_hash`
    while `reportSpecification.reportOptions.reportPeriod` becomes its own column. Reading a
    scalar at the prefix would find a dict and store its repr.
    """
    paths = {mapping.source_path for mapping in contract.fields if mapping.source_path}
    return frozenset(path for path in paths if any(other.startswith(f"{path}.") for other in paths))


def _array_name(contract: DataContract) -> str:
    """Name the single repeating array a nested contract reads.

    Exactly one, because a contract covers one source dataset at one grain. A document
    carrying two grains is two contracts, so two arrays here would mean the contract is
    wrong rather than the document.
    """
    names = {
        mapping.source_path.split("[]", maxsplit=1)[0]
        for mapping in contract.fields
        if "[]" in mapping.source_path
    }
    if len(names) != 1:
        raise MappingError(
            f"{contract.contract_id}: expected exactly one repeating array, found "
            f"{sorted(names) or 'none'}. A document with two grains is two contracts."
        )
    return next(iter(names))


def _value_for(mapping: FieldMapping, context: _RowContext) -> object:
    """Resolve one field's value for one row, before scaling."""
    if mapping.is_literal:
        # Stamped, never read. `channel_key` on a DSP contract is knowledge we hold, because
        # AMC's convention is that a NULL ad_product_type *means* DSP.
        return mapping.literal_value

    path = mapping.source_path
    if path.startswith(MANIFEST_PREFIX):
        return resolve(context.manifest, path[len(MANIFEST_PREFIX) :])

    if path in context.object_valued:
        return options_hash(resolve(context.document, path))

    if "[]" in path:
        _, _, remainder = path.partition("[].")
        return resolve(context.element, remainder)

    if context.document is not None:
        # Document-level, so the same value on every row. The vendor steering requires the
        # whole reportSpecification to be stamped onto each row: without it a MANUFACTURING
        # pull and a SOURCING pull are indistinguishable and double-count.
        return resolve(context.document, path)

    return context.element.get(delivered_name(mapping))


@dataclass(frozen=True, slots=True)
class _RowContext:
    """Everything resolving one row needs, so adding one costs a field not a parameter."""

    element: dict[str, Any]
    manifest: dict[str, Any]
    object_valued: frozenset[str]
    document: dict[str, Any] | None = None


def _map_one(contract: DataContract, context: _RowContext) -> dict[str, object]:
    """Map one source element into one canonical row."""
    row: dict[str, object] = {}
    for mapping in contract.fields:
        if mapping.assigned_by_load:
            # The merge computes it by comparing this load against the current version, so
            # the mapper cannot know it and must not invent it. Absent here, supplied there.
            continue
        value = _value_for(mapping, context)
        if (
            contract.suppression is not None
            and mapping.target_column == contract.suppression.discriminator_column
        ):
            value = suppression_flag(value)
        if mapping.monetary and value is not None:
            value = scaled(value, mapping.scale_divisor or 1)
        row[mapping.target_column] = value

    # Stamped last, from contract-level metadata rather than from any field. No source states
    # its own measurement basis or the day boundary it aggregated into, which is exactly why
    # these cannot be mapped and must be stamped. Empty for a dimension.
    row.update(conformed_provenance_values(contract))
    return row


def map_rows(request: MappingRequest) -> tuple[dict[str, object], ...]:
    """Map a whole document into canonical rows, one per source element."""
    contract = request.contract
    source = request.source
    object_valued = _object_valued_paths(contract)

    if contract.source_format is SourceFormat.JSON_DUAL_ARRAY:
        if source.document is None:
            raise MappingError(
                f"{contract.contract_id} reads a nested document, but the reader produced "
                "flat rows. The contract's source_format and the delivered file disagree."
            )
        holder = cast("object", source.document.get(_array_name(contract)))
        if not isinstance(holder, list):
            # An absent array is not an empty one. A report whose array is missing entirely
            # is a different event from a report with nothing in it, and only the second is
            # a legitimate quiet day.
            raise MappingError(
                f"{contract.contract_id}: the document has no `{_array_name(contract)}` array."
            )
        elements: list[dict[str, Any]] = [
            cast("dict[str, Any]", e) for e in cast("list[object]", holder) if isinstance(e, dict)
        ]
        return tuple(
            _map_one(
                contract,
                _RowContext(
                    element=element,
                    manifest=source.manifest,
                    object_valued=object_valued,
                    document=source.document,
                ),
            )
            for element in elements
        )

    return tuple(
        _map_one(
            contract,
            _RowContext(element=row, manifest=source.manifest, object_valued=object_valued),
        )
        for row in source.rows
    )
