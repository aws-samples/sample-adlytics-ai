# SPDX-License-Identifier: MIT-0
"""Read-only authorized analytical entity catalog boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol, runtime_checkable

from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EntityAlias,
    EntityContext,
    EntityKind,
    EntityReference,
)
from domain.shared_kernel.values import DateRange
from ports.outbound.query import BAKE_MAX_ROWS

MAX_ENTITY_CANDIDATES = 25
MAX_CAMPAIGN_PORTFOLIO_CANDIDATES = 1_000


class BrowsePurpose(StrEnum):
    """Who browses the campaign population, and so how many campaigns it may ask for."""

    INTERACTIVE = "interactive"
    """A request-time list: at most `MAX_CAMPAIGN_PORTFOLIO_CANDIDATES`."""
    BAKE = "bake"
    """A publish-time bake: at most `BAKE_MAX_ROWS`, the ceiling the Baker's memory certifies
    (ADR-152). Past it the bake refuses the page with `population_over_ceiling`."""


def campaign_candidate_limit(purpose: BrowsePurpose) -> int:
    """Return the most campaigns one browse of this purpose may ask for."""
    return BAKE_MAX_ROWS if purpose is BrowsePurpose.BAKE else MAX_CAMPAIGN_PORTFOLIO_CANDIDATES


MAX_PUBLIC_LABEL_ITEMS = 500
PUBLIC_LABEL_DIMENSIONS = frozenset(
    {
        "campaign_key",
        "amazon_asin",
        "marketplace_key",
        "channel_key",
        "platform_key",
        "ecosystem_key",
        "channel_group_key",
        "ad_product_key",
    }
)


class PublicEntityLabelOutcome(StrEnum):
    """Closed public-label resolution outcome with no protected identity material."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    UNAUTHORIZED_OR_HIDDEN = "unauthorized_or_hidden"
    INVALID_CONTEXT = "invalid_context"


@dataclass(frozen=True, slots=True)
class PublicEntityLabelItem:
    """One exact protected dimension tuple in anchor order."""

    dimensions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not self.dimensions:
            raise ValueError("public label item requires at least one protected dimension.")
        names = tuple(name for name, _ in self.dimensions)
        if len(set(names)) != len(names):
            raise ValueError("public label item repeats a protected dimension.")
        if any(name not in PUBLIC_LABEL_DIMENSIONS for name in names):
            raise ValueError("public label item contains an unsupported protected dimension.")
        if any(not name.strip() or not value.strip() for name, value in self.dimensions):
            raise ValueError("public label dimensions require exact nonblank names and values.")


@dataclass(frozen=True, slots=True)
class PublicEntityLabelRequest:
    """Bounded authorized positional projection of protected tuples to public labels."""

    scope: AuthorizedAnalyticalScope
    items: tuple[PublicEntityLabelItem, ...]
    context: EntityContext
    window: DateRange

    def __post_init__(self) -> None:
        if not 1 <= len(self.items) <= MAX_PUBLIC_LABEL_ITEMS:
            raise ValueError("public label batch must contain between 1 and 500 items.")
        if len(set(self.items)) != len(self.items):
            raise ValueError("public label batch repeats an exact protected dimension tuple.")


@dataclass(frozen=True, slots=True)
class PublicEntityLabelResolution:
    """One positional closed outcome and optional governed display label only."""

    outcome: PublicEntityLabelOutcome
    display_label: str | None = None

    def __post_init__(self) -> None:
        if (self.outcome is PublicEntityLabelOutcome.RESOLVED) != (self.display_label is not None):
            raise ValueError("only a resolved public label outcome may carry display text.")
        if self.display_label is not None and not self.display_label.strip():
            raise ValueError("resolved public display label cannot be blank.")


@dataclass(frozen=True, slots=True)
class PublicEntityLabelResult:
    """Positional public-only resolutions containing no protected identity material."""

    resolutions: tuple[PublicEntityLabelResolution, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.resolutions) <= MAX_PUBLIC_LABEL_ITEMS:
            raise ValueError("public label result must contain between 1 and 500 resolutions.")
        labels = tuple(
            item.display_label for item in self.resolutions if item.display_label is not None
        )
        if len(set(labels)) != len(labels):
            raise ValueError("public label result repeats a resolved display label.")


@dataclass(frozen=True, slots=True)
class EntityCatalogBrowseRequest:
    """Bounded authorized catalog projection without an untrusted text reference."""

    scope: AuthorizedAnalyticalScope
    kinds: frozenset[EntityKind]
    window: DateRange | None
    current_on: date | None
    context: EntityContext
    max_candidates: int
    purpose: BrowsePurpose = BrowsePurpose.INTERACTIVE
    """Whose browse this is: it sets the campaign-only limit (FEAT-008 step 1)."""

    def __post_init__(self) -> None:
        if (self.window is None) == (self.current_on is None):
            raise ValueError("entity catalog browse requires exactly one window or current day.")
        if self.purpose is BrowsePurpose.BAKE and self.kinds != frozenset({EntityKind.CAMPAIGN}):
            raise ValueError("a bake browse reads the campaign population only.")
        limit = (
            campaign_candidate_limit(self.purpose)
            if self.kinds == frozenset({EntityKind.CAMPAIGN})
            else MAX_ENTITY_CANDIDATES
        )
        if not 1 <= self.max_candidates <= limit:
            raise ValueError(f"entity catalog candidate limit must be between 1 and {limit}.")


@dataclass(frozen=True, slots=True)
class EntityCatalogBrowseResult:
    """Bounded authorized alias histories and the exact authorized candidate count."""

    aliases: tuple[EntityAlias, ...]
    truncated: bool = False
    authorized_count: int = 0

    def __post_init__(self) -> None:
        candidates = {(alias.kind, alias.key) for alias in self.aliases}
        # The widest purpose's limit; each request's own `max_candidates` bounds its result.
        if len(candidates) > max(campaign_candidate_limit(purpose) for purpose in BrowsePurpose):
            raise ValueError("entity catalog returned more candidates than the bounded contract.")
        if self.authorized_count == 0 and not self.truncated:
            object.__setattr__(self, "authorized_count", len(candidates))
        if self.authorized_count < len(candidates):
            raise ValueError("entity catalog authorized count is smaller than returned candidates.")
        if self.truncated != (self.authorized_count > len(candidates)):
            raise ValueError("entity catalog truncation contradicts the authorized count.")


@dataclass(frozen=True, slots=True)
class EntityCatalogLookupRequest:
    """Bounded search inputs applied after deployment scope and before disclosure."""

    scope: AuthorizedAnalyticalScope
    reference: EntityReference
    kinds: frozenset[EntityKind]
    window: DateRange | None
    current_on: date | None
    context: EntityContext
    max_candidates: int

    def __post_init__(self) -> None:
        if (self.window is None) == (self.current_on is None):
            raise ValueError("entity catalog lookup requires exactly one window or current day.")
        if not 1 <= self.max_candidates <= MAX_ENTITY_CANDIDATES:
            raise ValueError("entity catalog candidate limit must be between 1 and 25.")


@dataclass(frozen=True, slots=True)
class EntityCatalogLookupResult:
    """Authorized aliases only, plus a nondisclosing direct-reference hidden marker."""

    aliases: tuple[EntityAlias, ...]
    hidden_direct_match: bool = False
    truncated: bool = False

    def __post_init__(self) -> None:
        candidates = {(alias.kind, alias.key) for alias in self.aliases}
        if len(candidates) > MAX_ENTITY_CANDIDATES:
            raise ValueError("entity catalog returned more candidates than the bounded contract.")
        if self.hidden_direct_match and self.aliases:
            raise ValueError("hidden direct match cannot disclose authorized aliases.")


@runtime_checkable
class AnalyticalEntityCatalogPort(Protocol):
    """Search governed effective-dated dimensions without exposing unauthorized rows."""

    def browse(self, request: EntityCatalogBrowseRequest) -> EntityCatalogBrowseResult:
        """Return bounded authorized alias histories after effective filtering."""
        ...

    def lookup(self, request: EntityCatalogLookupRequest) -> EntityCatalogLookupResult:
        """Return bounded authorized aliases after applying every request selector."""
        ...

    def public_labels(self, request: PublicEntityLabelRequest) -> PublicEntityLabelResult:
        """Return positional closed outcomes and governed public display labels only."""
        ...
