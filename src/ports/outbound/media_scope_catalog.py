# SPDX-License-Identifier: MIT-0
"""Read-only authorized media-scope catalog boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast, runtime_checkable

from domain.presentation.artifacts import MediaScope, SafeMediaScopeDisplaySignature
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.shared_kernel.values import DateRange

__all__ = [
    "MAX_MEDIA_SCOPE_CANDIDATES",
    "MediaScopeCatalogCandidate",
    "MediaScopeCatalogCoverageRequest",
    "MediaScopeCatalogCoverageResult",
    "MediaScopeCatalogListRequest",
    "MediaScopeCatalogListResult",
    "MediaScopeCatalogPort",
    "MediaScopeCatalogRevalidationOutcome",
    "MediaScopeCatalogRevalidationRequest",
    "MediaScopeCatalogRevalidationResult",
]

MAX_MEDIA_SCOPE_CANDIDATES = 25


@dataclass(frozen=True, slots=True)
class MediaScopeCatalogCoverageRequest:
    """Authorized request for windows supported by declared Media catalog evidence."""

    authorized_scope: AuthorizedAnalyticalScope

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.authorized_scope), AuthorizedAnalyticalScope):
            raise TypeError("media scope coverage requires an authorized analytical scope.")


@dataclass(frozen=True, slots=True)
class MediaScopeCatalogCoverageResult:
    """Exact coalesced windows with at least one qualified Media tuple."""

    intervals: tuple[DateRange, ...]

    def __post_init__(self) -> None:
        if type(self.intervals) is not tuple or any(
            not isinstance(cast("object", interval), DateRange) for interval in self.intervals
        ):
            raise TypeError("media scope coverage intervals must be a DateRange tuple.")
        if any(
            (right.start - left.end).days <= 1
            for left, right in zip(self.intervals, self.intervals[1:], strict=False)
        ):
            raise ValueError("media scope coverage intervals must be sorted and disjoint.")


@dataclass(frozen=True, slots=True)
class MediaScopeCatalogListRequest:
    """Bounded authorized request for server-only media scope candidates."""

    authorized_scope: AuthorizedAnalyticalScope
    window: DateRange
    max_candidates: int

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.authorized_scope), AuthorizedAnalyticalScope):
            raise TypeError("media scope catalog requires an authorized analytical scope.")
        if not isinstance(cast("object", self.window), DateRange):
            raise TypeError("media scope catalog requires a governed date range.")
        if (
            type(self.max_candidates) is not int
            or not 1 <= self.max_candidates <= MAX_MEDIA_SCOPE_CANDIDATES
        ):
            raise ValueError("media scope catalog candidate limit must be between 1 and 25.")


@dataclass(frozen=True, slots=True)
class MediaScopeCatalogCandidate:
    """One server-only canonical scope with its safe signature and stable identity."""

    scope: MediaScope
    safe_signature: SafeMediaScopeDisplaySignature
    catalog_identity: str

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.scope), MediaScope):
            raise TypeError("media scope catalog candidate requires a canonical media scope.")
        if not isinstance(cast("object", self.safe_signature), SafeMediaScopeDisplaySignature):
            raise TypeError("media scope catalog candidate requires a safe display signature.")
        if type(self.catalog_identity) is not str or not self.catalog_identity.strip():
            raise ValueError("media scope catalog candidate requires a stable nonblank identity.")


@dataclass(frozen=True, slots=True)
class MediaScopeCatalogListResult:
    """Bounded server-only candidates; browser projection is owned outside this port."""

    candidates: tuple[MediaScopeCatalogCandidate, ...]
    truncated: bool = False

    def __post_init__(self) -> None:
        if type(self.candidates) is not tuple:
            raise TypeError("media scope catalog candidates must be a tuple.")
        if len(self.candidates) > MAX_MEDIA_SCOPE_CANDIDATES:
            raise ValueError(
                "media scope catalog returned more candidates than the bounded contract."
            )
        if not all(
            isinstance(cast("object", candidate), MediaScopeCatalogCandidate)
            for candidate in self.candidates
        ):
            raise TypeError("media scope catalog result requires media scope catalog candidates.")
        scopes = tuple(candidate.scope for candidate in self.candidates)
        labels = tuple(candidate.safe_signature.label for candidate in self.candidates)
        identities = tuple(candidate.catalog_identity for candidate in self.candidates)
        if len(set(scopes)) != len(scopes):
            raise ValueError("media scope catalog result repeats a canonical scope.")
        if len(set(labels)) != len(labels):
            raise ValueError("media scope catalog result repeats a safe display signature.")
        if len(set(identities)) != len(identities):
            raise ValueError("media scope catalog result repeats a stable candidate identity.")
        if type(self.truncated) is not bool:
            raise TypeError("media scope catalog truncation marker must be a bool.")


class MediaScopeCatalogRevalidationOutcome(StrEnum):
    """Closed nondisclosing outcome for current catalog identity revalidation."""

    CURRENT = "current"
    STALE_OR_UNAVAILABLE = "stale_or_unavailable"


@dataclass(frozen=True, slots=True)
class MediaScopeCatalogRevalidationRequest:
    """Server-only canonical scope and stable identity to revalidate before use."""

    authorized_scope: AuthorizedAnalyticalScope
    scope: MediaScope
    catalog_identity: str
    window: DateRange

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.authorized_scope), AuthorizedAnalyticalScope):
            raise TypeError("media scope revalidation requires an authorized analytical scope.")
        if not isinstance(cast("object", self.scope), MediaScope):
            raise TypeError("media scope revalidation requires a canonical media scope.")
        if not isinstance(cast("object", self.window), DateRange):
            raise TypeError("media scope revalidation requires a governed date range.")
        if type(self.catalog_identity) is not str or not self.catalog_identity.strip():
            raise ValueError("media scope revalidation requires a stable nonblank identity.")
        if not self.authorized_scope.allows(self.scope.commerce_scope):
            raise ValueError("media scope revalidation scope is outside the authorized scope.")


@dataclass(frozen=True, slots=True)
class MediaScopeCatalogRevalidationResult:
    """One public nondisclosing catalog-current outcome without selector material."""

    outcome: MediaScopeCatalogRevalidationOutcome

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.outcome), MediaScopeCatalogRevalidationOutcome):
            raise TypeError("media scope catalog revalidation requires a closed outcome.")


@runtime_checkable
class MediaScopeCatalogPort(Protocol):
    """Resolve qualified Media coverage, choices, and current catalog identities."""

    def coverage(
        self, request: MediaScopeCatalogCoverageRequest
    ) -> MediaScopeCatalogCoverageResult:
        """Return only windows backed by the qualified literal tuple/publication intersection."""
        ...

    def list_scopes(self, request: MediaScopeCatalogListRequest) -> MediaScopeCatalogListResult:
        """Return bounded server-only canonical candidates after authorization."""
        ...

    def revalidate(
        self, request: MediaScopeCatalogRevalidationRequest
    ) -> MediaScopeCatalogRevalidationResult:
        """Return one nondisclosing catalog-current outcome for a stored candidate."""
        ...
