# SPDX-License-Identifier: MIT-0
"""Typed source-aligned rowsets preserve interpreted evidence before canonicalization.

ADR-044. The landed object remains immutable raw evidence; a source-aligned publication records the
source vocabulary, types, row measurement buckets, extraction coverage, option set, interpreter
version, and exact landed object that a canonical publisher consumed. Nothing here promotes a source
identifier into a canonical identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from domain.semantics.identity import DeliveryScope, SourceIdentifier
from domain.semantics.publication import SourcePublicationRef
from domain.semantics.revision import SourceRevision
from domain.semantics.time import ExtractionCoverage, MeasurementBucket

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Iterable

__all__ = [
    "LandedDeliveryRef",
    "SourceAlignedField",
    "SourceAlignedPublication",
    "SourceAlignedRow",
    "SourceAlignedRowset",
    "SourceAlignedStateError",
    "SourceInterpreterKey",
    "SourceOption",
    "SourceScalar",
    "resolve_source_inputs",
]

_INTERPRETER = re.compile(r"^[a-z][a-z0-9_.:-]*$")

type SourceScalar = str | int | float | Decimal | bool | date | datetime | None


class SourceAlignedStateError(ValueError):
    """Source-aligned evidence is incomplete, ambiguous, or internally contradictory."""


@dataclass(frozen=True, slots=True)
class LandedDeliveryRef:
    """Reversible reference to one immutable object and the manifest that names it."""

    manifest_key: str
    manifest_version: str
    manifest_checksum: str
    object_key: str
    object_version: str
    object_checksum: str
    first_observed_at: datetime | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("manifest key", self.manifest_key),
            ("manifest version", self.manifest_version),
            ("manifest checksum", self.manifest_checksum),
            ("object key", self.object_key),
            ("object version", self.object_version),
            ("object checksum", self.object_checksum),
        ):
            if not value.strip():
                raise SourceAlignedStateError(f"landed delivery requires its {label}.")
        if self.first_observed_at is not None and self.first_observed_at.tzinfo is None:
            raise SourceAlignedStateError(
                "landed delivery first observation must be timezone-aware."
            )


@dataclass(frozen=True, slots=True)
class SourceInterpreterKey:
    """Versioned source-row interpretation, independent of canonical mapping versions."""

    value: str
    version: int

    def __post_init__(self) -> None:
        normalized = self.value.strip()
        if not _INTERPRETER.fullmatch(normalized):
            raise SourceAlignedStateError(f"source interpreter key is invalid: {self.value!r}.")
        if self.version < 1:
            raise SourceAlignedStateError("source interpreter version starts at 1.")
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True, slots=True)
class SourceOption:
    """One source-owned extraction option retained in full beside its routing hash."""

    name: str
    value: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.value.strip():
            raise SourceAlignedStateError("source option requires a non-empty name and value.")


@dataclass(frozen=True, slots=True)
class SourceAlignedField:
    """One typed field under the source's own label and meaning."""

    name: str
    value: SourceScalar

    def __post_init__(self) -> None:
        normalized = self.name.strip()
        if not normalized:
            raise SourceAlignedStateError("source-aligned field name cannot be empty.")
        value = cast("object", self.value)
        if value is not None and not isinstance(
            value, (str, int, float, Decimal, bool, date, datetime)
        ):
            raise SourceAlignedStateError(
                f"source-aligned field {normalized!r} is not a supported scalar."
            )
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise SourceAlignedStateError(
                f"source-aligned float field {normalized!r} must be finite."
            )
        if isinstance(self.value, Decimal) and not self.value.is_finite():
            raise SourceAlignedStateError(
                f"source-aligned decimal field {normalized!r} must be finite."
            )
        if isinstance(self.value, datetime) and self.value.tzinfo is None:
            raise SourceAlignedStateError(
                f"source-aligned datetime field {normalized!r} must be timezone-aware."
            )
        object.__setattr__(self, "name", normalized)


@dataclass(frozen=True, slots=True)
class SourceAlignedRow:
    """One interpreted source row, including qualified identifiers and measurement time."""

    fields: tuple[SourceAlignedField, ...]
    source_identifiers: tuple[SourceIdentifier, ...]
    measurement: MeasurementBucket

    def __post_init__(self) -> None:
        names = tuple(field.name for field in self.fields)
        if len(set(names)) != len(names):
            raise SourceAlignedStateError("source-aligned row repeats a source field name.")
        if len(set(self.source_identifiers)) != len(self.source_identifiers):
            raise SourceAlignedStateError("source-aligned row repeats a qualified identifier.")


@dataclass(frozen=True, slots=True)
class SourceAlignedRowset:
    """Typed interpretation of one delivery under source vocabulary, before canonical mapping."""

    delivery_scope: DeliveryScope
    extraction_coverage: ExtractionCoverage
    interpreter: SourceInterpreterKey
    source_options: tuple[SourceOption, ...]
    rows: tuple[SourceAlignedRow, ...]

    def __post_init__(self) -> None:
        option_names = tuple(option.name for option in self.source_options)
        if len(set(option_names)) != len(option_names):
            raise SourceAlignedStateError("source-aligned rowset repeats a source option.")
        for row in self.rows:
            self.extraction_coverage.require_contains(row.measurement)

    @property
    def content_checksum(self) -> str:
        """Deterministic digest of source semantics and typed row content."""
        payload = {
            "delivery_scope": {
                "source_system": self.delivery_scope.source_system.value,
                "source_dataset": self.delivery_scope.source_dataset.value,
                "extraction_profile": self.delivery_scope.extraction_profile.value,
                "extraction_profile_version": self.delivery_scope.extraction_profile.version,
                "source_scope_namespace": self.delivery_scope.source_scope.namespace,
                "source_scope_value": self.delivery_scope.source_scope.value,
                "option_set_hash": self.delivery_scope.option_set_hash,
            },
            "coverage": {
                "start": self.extraction_coverage.window.start.isoformat(),
                "end": self.extraction_coverage.window.end.isoformat(),
            },
            "interpreter": {
                "key": self.interpreter.value,
                "version": self.interpreter.version,
            },
            "source_options": [
                {"name": option.name, "value": option.value}
                for option in sorted(self.source_options, key=lambda item: item.name)
            ],
            "rows": sorted(
                (_row_data(row) for row in self.rows),
                key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
            ),
        }
        rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(rendered.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class SourceAlignedPublication:
    """Immutable materialized source rowset and the evidence needed to reproduce it."""

    reference: SourcePublicationRef
    landed_delivery: LandedDeliveryRef
    rowset: SourceAlignedRowset
    source_revision: SourceRevision
    published_at: datetime

    def __post_init__(self) -> None:
        if self.published_at.tzinfo is None:
            raise SourceAlignedStateError("source-aligned publication instant must be aware.")

    @property
    def content_checksum(self) -> str:
        """Deterministic digest of the interpreted source rowset."""
        return self.rowset.content_checksum

    @property
    def row_count(self) -> int:
        """Number of interpreted source rows, including a valid empty publication."""
        return len(self.rowset.rows)


def resolve_source_inputs(
    references: tuple[SourcePublicationRef, ...],
    available: Iterable[SourceAlignedPublication],
) -> tuple[SourceAlignedPublication, ...]:
    """Resolve every requested source input exactly once and preserve request order."""
    if not references:
        raise SourceAlignedStateError("canonical publication requires a source-aligned input.")
    if len(set(references)) != len(references):
        raise SourceAlignedStateError("canonical publication repeats a source-aligned reference.")

    by_reference: dict[SourcePublicationRef, list[SourceAlignedPublication]] = {}
    for publication in available:
        by_reference.setdefault(publication.reference, []).append(publication)

    resolved: list[SourceAlignedPublication] = []
    for reference in references:
        matches = by_reference.get(reference, [])
        if not matches:
            raise SourceAlignedStateError(f"source publication {reference.value!r} is missing.")
        if len(matches) != 1:
            raise SourceAlignedStateError(
                f"source publication {reference.value!r} resolves ambiguously."
            )
        resolved.append(matches[0])
    return tuple(resolved)


def _row_data(row: SourceAlignedRow) -> dict[str, object]:
    return {
        "fields": [
            {"name": field.name, "value": _scalar_data(field.value)}
            for field in sorted(row.fields, key=lambda item: item.name)
        ],
        "source_identifiers": [
            {"namespace": item.namespace, "value": item.value}
            for item in sorted(
                row.source_identifiers,
                key=lambda item: (item.namespace, item.value),
            )
        ],
        "measurement": {
            "start": row.measurement.window.start.isoformat(),
            "end": row.measurement.window.end.isoformat(),
            "grain": row.measurement.grain.value,
            "calendar": row.measurement.calendar.value,
            "week_starts_on": row.measurement.calendar.week_starts_on,
            "source_timezone": row.measurement.source_timezone,
            "observation_basis": row.measurement.observation_basis.value,
        },
    }


def _scalar_data(value: SourceScalar) -> object:
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.astimezone(UTC).isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if value is None:
        return {"type": "null", "value": None}
    return {"type": type(value).__name__, "value": value}
