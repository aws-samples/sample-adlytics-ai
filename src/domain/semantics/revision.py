# SPDX-License-Identifier: MIT-0
"""Source-neutral revision tokens preserve a producer's ordering precision.

A revision scheme belongs to the adapter or later source-pack compiler. The shared domain accepts a
qualified scheme and either date- or instant-precision evidence; it never names AMC, Vendor Central,
or any future producer. This prevents every new source from requiring a shared-domain edit while
still refusing comparisons across unrelated source clocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum

__all__ = [
    "RevisionPrecision",
    "RevisionSchemeKey",
    "SourceRevision",
    "SourceRevisionError",
]

_SCHEME = re.compile(r"^[a-z][a-z0-9_.:-]*$")


class SourceRevisionError(ValueError):
    """Two source revisions cannot be compared or one is malformed."""


class RevisionPrecision(StrEnum):
    """Precision the source actually supplies, never precision invented by the platform."""

    DATE = "date"
    INSTANT = "instant"


@dataclass(frozen=True, slots=True)
class RevisionSchemeKey:
    """Qualified source-owned revision vocabulary loaded by an adapter."""

    value: str

    def __post_init__(self) -> None:
        normalized = self.value.strip()
        if not _SCHEME.fullmatch(normalized):
            raise SourceRevisionError(f"source revision scheme is invalid: {self.value!r}.")
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True, slots=True)
class SourceRevision:
    """A source-owned revision with enough precision to order restatements.

    `source_id` is identity and audit evidence, never the ordering itself. Ordering is the
    normalized instant. Date-precision values normalize to UTC midnight solely to share one typed
    storage shape; only revisions under the same qualified scheme may be compared.
    """

    scheme: RevisionSchemeKey
    precision: RevisionPrecision
    observed_at: datetime
    source_id: str

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise SourceRevisionError("source revision instant must be timezone-aware.")
        if not self.source_id.strip():
            raise SourceRevisionError("source revision requires a source-owned identifier.")
        normalized = self.observed_at.astimezone(UTC)
        if self.precision is RevisionPrecision.DATE and normalized.time() != time.min:
            raise SourceRevisionError("a date-precision revision must normalize to midnight UTC.")
        object.__setattr__(self, "observed_at", normalized)

    @classmethod
    def instant(
        cls,
        *,
        scheme: RevisionSchemeKey,
        source_id: str,
        observed_at: datetime,
    ) -> SourceRevision:
        """Build full-precision revision evidence without interpreting the source identifier."""
        return cls(
            scheme=scheme,
            precision=RevisionPrecision.INSTANT,
            observed_at=observed_at,
            source_id=source_id,
        )

    @classmethod
    def dated(
        cls,
        *,
        scheme: RevisionSchemeKey,
        source_id: str,
        observed_on: date,
    ) -> SourceRevision:
        """Build date-precision revision evidence supplied by a source."""
        return cls(
            scheme=scheme,
            precision=RevisionPrecision.DATE,
            observed_at=datetime.combine(observed_on, time.min, tzinfo=UTC),
            source_id=source_id,
        )

    def compare(self, other: SourceRevision) -> int:
        """Return -1, 0 or 1, refusing revisions from different schemes or precisions."""
        if self.scheme != other.scheme:
            raise SourceRevisionError(
                f"cannot order {self.scheme.value} against {other.scheme.value}; the sources own "
                "different revision vocabularies."
            )
        if self.precision is not other.precision:
            raise SourceRevisionError(
                f"{self.scheme.value}: cannot order {self.precision.value} against "
                f"{other.precision.value} revision evidence."
            )
        return (self.observed_at > other.observed_at) - (self.observed_at < other.observed_at)

    def is_newer_than(self, other: SourceRevision) -> bool:
        """Whether this revision succeeds another revision under the same source scheme."""
        return self.compare(other) > 0
