# SPDX-License-Identifier: MIT-0
"""Shared immutable values and validation for structured conversation contracts."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, cast


def require_text(value: object, label: str, *, maximum: int | None = None) -> None:
    """Require bounded, nonblank text without changing server-authored material."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonblank text.")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters.")


def require_optional_text(value: object, label: str, *, maximum: int | None = None) -> None:
    """Require null or bounded nonblank text."""
    if value is not None:
        require_text(value, label, maximum=maximum)


def require_integer(value: object, label: str, *, minimum: int | None = None) -> None:
    """Reject booleans and enforce only an explicitly declared minimum."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer, not boolean.")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be an integer greater than or equal to {minimum}.")


def freeze_json_object(value: Mapping[str, object], label: str) -> Mapping[str, object]:
    """Copy and recursively freeze one JSON object without changing its wire shape."""

    def freeze(item: object, path: str) -> object:
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{path} contains a non-finite number.")
            return item
        if isinstance(item, (list, tuple)):
            return tuple(
                freeze(child, f"{path} item")
                for child in cast("list[object] | tuple[object, ...]", item)
            )
        if isinstance(item, Mapping):
            frozen: dict[str, object] = {}
            for key, child in cast("Mapping[object, object]", item).items():
                if not isinstance(key, str) or not key.strip():
                    raise ValueError(f"{path} object keys must be nonblank strings.")
                frozen[key] = freeze(child, f"{path}.{key}")
            return MappingProxyType(frozen)
        raise ValueError(f"{path} is not a JSON value.")

    return cast("Mapping[str, object]", freeze(value, label))


def require_boolean(value: object, label: str) -> None:
    """Require an actual boolean rather than a truthy scalar."""
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be boolean.")


def require_unique_text(values: tuple[str, ...], label: str, *, nonempty: bool = False) -> None:
    """Require nonblank, duplicate-free references."""
    if nonempty and not values:
        raise ValueError(f"{label} cannot be empty.")
    for value in values:
        require_text(value, label)
    if len(set(values)) != len(values):
        raise ValueError(f"{label} cannot contain duplicates.")


def require_iso_date(value: str, label: str) -> date:
    """Parse one canonical ISO-8601 calendar date."""
    require_text(value, label)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} must be an ISO date.") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must use canonical ISO date form.")
    return parsed


class DepthV1(StrEnum):
    """Server-budget depth requested by planning."""

    QUICK = "quick"
    DEEP = "deep"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportingWindowV1:
    """Exact reporting window shared by private and public contracts."""

    schema_version: Literal["ReportingWindow.v1"] = "ReportingWindow.v1"
    start_date: str
    end_date: str
    timezone: str
    date_basis: str
    coverage_label: str | None
    freshness_label: str | None

    def __post_init__(self) -> None:
        if require_iso_date(self.start_date, "reporting start") > require_iso_date(
            self.end_date, "reporting end"
        ):
            raise ValueError("reporting start cannot follow reporting end.")
        require_text(self.timezone, "reporting timezone")
        require_text(self.date_basis, "reporting date basis")
        require_optional_text(self.coverage_label, "coverage label")
        require_optional_text(self.freshness_label, "freshness label")


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalyticalSubjectV1:
    """Public analytical subject label."""

    kind: str
    label: str

    def __post_init__(self) -> None:
        require_text(self.kind, "subject kind")
        require_text(self.label, "subject label")


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalyticalContextV1:
    """Exact analytical context attached to evidence material."""

    schema_version: Literal["AnalyticalContext.v1"] = "AnalyticalContext.v1"
    subject: AnalyticalSubjectV1
    marketplace_label: str | None
    grain_label: str
    basis_label: str
    source_label: str
    currency: str | None
    reporting_window: ReportingWindowV1

    def __post_init__(self) -> None:
        require_optional_text(self.marketplace_label, "marketplace label")
        require_text(self.grain_label, "grain label")
        require_text(self.basis_label, "basis label")
        require_text(self.source_label, "source label")
        require_optional_text(self.currency, "currency")


def analytical_contexts_are_compatible(
    left: AnalyticalContextV1, right: AnalyticalContextV1
) -> bool:
    """Compare non-period semantics while allowing period-state metadata to differ."""
    return (
        left.subject == right.subject
        and left.marketplace_label == right.marketplace_label
        and left.grain_label == right.grain_label
        and left.basis_label == right.basis_label
        and left.source_label == right.source_label
        and left.currency == right.currency
        and left.reporting_window.timezone == right.reporting_window.timezone
        and left.reporting_window.date_basis == right.reporting_window.date_basis
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class CoverageIntervalV1:
    """Inclusive normalized coverage interval."""

    start_date: str
    end_date: str

    def __post_init__(self) -> None:
        if require_iso_date(self.start_date, "coverage start") > require_iso_date(
            self.end_date, "coverage end"
        ):
            raise ValueError("coverage start cannot follow coverage end.")


@dataclass(frozen=True, slots=True, kw_only=True)
class SubjectScopeV1:
    """Protected subject scope for validation and actions."""

    subject_kind: str
    subject_ref: str
    reporting_window: ReportingWindowV1 | None

    def __post_init__(self) -> None:
        require_text(self.subject_kind, "subject kind")
        require_text(self.subject_ref, "subject reference")
