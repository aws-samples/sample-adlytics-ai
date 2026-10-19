# SPDX-License-Identifier: MIT-0
"""Construct complete immutable SCD2 history candidates before publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from domain.semantics.canonical_compiler import (
    CanonicalRow,
    canonical_rows_checksum,
    canonical_scalar,
)

OPEN_ENDED = datetime(2999, 12, 31, tzinfo=UTC)
_HISTORY_COLUMNS = frozenset({"valid_from", "valid_to", "is_current"})


@dataclass(frozen=True, slots=True)
class DimensionHistoryRequest:
    """Previous complete history plus current observations at one deterministic boundary."""

    previous: tuple[CanonicalRow, ...]
    observations: tuple[CanonicalRow, ...]
    business_key: tuple[str, ...]
    tracked_attributes: tuple[str, ...]
    boundary: datetime

    def __post_init__(self) -> None:
        if self.boundary.tzinfo is None:
            raise ValueError("dimension history boundary must be timezone-aware.")
        if not self.business_key or not self.tracked_attributes:
            raise ValueError("dimension history requires business keys and tracked attributes.")
        overlap = set(self.business_key) & set(self.tracked_attributes)
        if overlap or set(self.business_key) & _HISTORY_COLUMNS:
            raise ValueError("dimension history keys and tracked attributes must be distinct.")


@dataclass(frozen=True, slots=True)
class DimensionHistoryResult:
    """Complete validated history and deterministic checksum for one candidate."""

    rows: tuple[CanonicalRow, ...]
    content_checksum: str


def build_dimension_history(request: DimensionHistoryRequest) -> DimensionHistoryResult:
    """Preserve omissions, close changed values, and open one successor atomically."""
    histories = [_row(item) for item in request.previous]
    _validate_history(histories, request.business_key)
    observed = _observations(request)
    for key, value in observed.items():
        current = _current(histories, request.business_key, key)
        if current is None:
            histories.append(_opened(value, request.boundary))
            continue
        if _attributes(current, request.tracked_attributes) == _attributes(
            value, request.tracked_attributes
        ):
            continue
        valid_from = _moment(current, "valid_from")
        if request.boundary <= valid_from:
            raise ValueError("a changed dimension observation must follow the current version.")
        current["valid_to"] = request.boundary
        current["is_current"] = False
        histories.append(_opened(value, request.boundary))
    _validate_history(histories, request.business_key)
    rows = tuple(
        tuple(
            sorted(
                ((name, canonical_scalar(value, column=name)) for name, value in item.items()),
                key=lambda pair: pair[0],
            )
        )
        for item in sorted(
            histories,
            key=lambda row: (
                *(_string(row, column) for column in request.business_key),
                _moment(row, "valid_from"),
            ),
        )
    )
    return DimensionHistoryResult(rows, canonical_rows_checksum(rows))


def _observations(request: DimensionHistoryRequest) -> dict[tuple[str, ...], dict[str, object]]:
    found: dict[tuple[str, ...], dict[str, object]] = {}
    for raw in request.observations:
        row = _row(raw)
        key = tuple(_string(row, column) for column in request.business_key)
        candidate = {name: value for name, value in row.items() if name not in _HISTORY_COLUMNS}
        prior = found.get(key)
        if prior is not None and _attributes(prior, request.tracked_attributes) != _attributes(
            candidate, request.tracked_attributes
        ):
            raise ValueError("one dimension boundary contains conflicting observations for a key.")
        found[key] = candidate
    return found


def _opened(value: dict[str, object], boundary: datetime) -> dict[str, object]:
    return {**value, "valid_from": boundary, "valid_to": OPEN_ENDED, "is_current": True}


def _current(
    rows: list[dict[str, object]], business_key: tuple[str, ...], key: tuple[str, ...]
) -> dict[str, object] | None:
    matches = [
        row
        for row in rows
        if tuple(_string(row, column) for column in business_key) == key
        and row.get("is_current") is True
    ]
    if len(matches) > 1:
        raise ValueError("dimension history has more than one current version for a key.")
    return matches[0] if matches else None


def _validate_history(rows: list[dict[str, object]], business_key: tuple[str, ...]) -> None:
    grouped: dict[tuple[str, ...], list[dict[str, object]]] = {}
    for row in rows:
        missing = _HISTORY_COLUMNS - set(row)
        if missing:
            raise ValueError(f"dimension history row is missing {sorted(missing)}.")
        key = tuple(_string(row, column) for column in business_key)
        grouped.setdefault(key, []).append(row)
    for versions in grouped.values():
        ordered = sorted(versions, key=lambda row: _moment(row, "valid_from"))
        if sum(item.get("is_current") is True for item in ordered) != 1:
            raise ValueError("dimension history requires exactly one current version per key.")
        for index, row in enumerate(ordered):
            start = _moment(row, "valid_from")
            end = _moment(row, "valid_to")
            if end <= start:
                raise ValueError("dimension history interval must be increasing.")
            if index and start < _moment(ordered[index - 1], "valid_to"):
                raise ValueError("dimension history intervals cannot overlap.")
            if row.get("is_current") is True and end != OPEN_ENDED:
                raise ValueError("current dimension version must have the open-ended bound.")


def _row(value: CanonicalRow) -> dict[str, object]:
    return dict(value)


def _attributes(row: dict[str, object], names: tuple[str, ...]) -> tuple[object, ...]:
    return tuple(row.get(name) for name in names)


def _string(row: dict[str, object], column: str) -> str:
    value = row.get(column)
    if not isinstance(value, str) or not value:
        raise ValueError(f"dimension history key {column!r} must be a non-empty string.")
    return value


def _moment(row: dict[str, object], column: str) -> datetime:
    value = row.get(column)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"dimension history column {column!r} must be timezone-aware datetime.")
    return value
