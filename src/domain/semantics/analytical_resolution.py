# SPDX-License-Identifier: MIT-0
"""Deterministic authorized analytical entity resolution before measurement."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import cast

from domain.semantics.identity import (
    AdProductKey,
    AdvertiserKey,
    BrandKey,
    CampaignKey,
    CanonicalKey,
    ChannelGroupKey,
    ChannelKey,
    CommerceScopeKey,
    EcosystemKey,
    MarketplaceKey,
    PlatformKey,
    ProductKey,
    SourceIdentifier,
    SourceSystemKey,
)
from domain.shared_kernel.values import DateRange

_MIN_AMBIGUOUS_CANDIDATES = 2


class EntityKind(StrEnum):
    """Governed analytical entity kinds visible to semantic resolution."""

    MARKETPLACE = "marketplace"
    ADVERTISER = "advertiser"
    CAMPAIGN = "campaign"
    BRAND = "brand"
    PRODUCT = "product"
    ECOSYSTEM = "ecosystem"
    PLATFORM = "platform"
    CHANNEL_GROUP = "channel_group"
    CHANNEL = "channel"
    AD_PRODUCT = "ad_product"


class FieldOrigin(StrEnum):
    """Authoritative origin recorded for every resolved field."""

    EXPLICIT_CUSTOMER_TEXT = "explicit_customer_text"
    SERVER_VALIDATED_PAGE_CONTEXT = "server_validated_page_context"
    GOVERNED_DEFAULT = "governed_default"
    DETERMINISTIC_RESOLVER = "deterministic_resolver"
    CUSTOMER_CLARIFICATION = "customer_clarification"


@dataclass(frozen=True, slots=True)
class ResolutionProvenance:
    """Sanitized origin and canonical public value for one resolved field.

    Its closed three-scalar shape cannot carry source identifiers or scope, mapping references,
    owner/session/nonce values, SQL, or storage columns into a public response.
    """

    field: str
    origin: FieldOrigin
    value: str

    def __post_init__(self) -> None:
        if not self.field.strip() or not self.value.strip():
            raise ValueError("resolution provenance requires a field and public value.")


class ResolutionOutcome(StrEnum):
    """Typed terminal state of one deterministic resolution field."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    UNAUTHORIZED_OR_HIDDEN = "unauthorized_or_hidden"
    INVALID_CONTEXT = "invalid_context"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    SEMANTICALLY_INCOMPATIBLE = "semantically_incompatible"


@dataclass(frozen=True, slots=True)
class AuthorizedAnalyticalScope:
    """Canonical commerce populations visible to every user in one deployment."""

    commerce_scopes: frozenset[CommerceScopeKey]

    def __post_init__(self) -> None:
        if not self.commerce_scopes:
            raise ValueError("authorized analytical scope cannot be empty.")
        if any(
            not isinstance(cast("object", item), CommerceScopeKey) for item in self.commerce_scopes
        ):
            raise TypeError("authorized analytical scope requires CommerceScopeKey values.")

    def allows(self, commerce_scope: CommerceScopeKey) -> bool:
        """Whether one canonical commerce population is visible."""
        return commerce_scope in self.commerce_scopes


@dataclass(frozen=True, slots=True)
class EffectiveLabel:
    """Untrusted display text with the interval in which the source used it."""

    value: str
    valid_from: date
    valid_to: date | None = None

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("effective label cannot be blank.")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("effective label interval must be increasing.")

    @property
    def normalized(self) -> str:
        """Exact normalized comparison value; never a fuzzy or partial match."""
        return normalize_exact_text(self.value)

    def overlaps(self, window: DateRange) -> bool:
        """Whether this half-open alias interval overlaps an inclusive request window."""
        return self.valid_from <= window.end and (
            self.valid_to is None or window.start < self.valid_to
        )

    def contains(self, on: date) -> bool:
        """Whether this label is active on one business date."""
        return self.valid_from <= on and (self.valid_to is None or on < self.valid_to)


@dataclass(frozen=True, slots=True)
class MappingEvidence:
    """Versioned source evidence behind one canonical entity alias."""

    reference: str
    mapping_version: int

    def __post_init__(self) -> None:
        if not self.reference.strip() or self.mapping_version < 1:
            raise ValueError("mapping evidence requires reference and positive version.")


@dataclass(frozen=True, slots=True)
class EntityAlias:
    """One authorized-searchable effective alias for one canonical analytical entity."""

    kind: EntityKind
    key: CanonicalKey
    commerce_scope: CommerceScopeKey
    labels: tuple[EffectiveLabel, ...]
    source_identifiers: tuple[SourceIdentifier, ...]
    evidence: MappingEvidence
    valid_from: date
    valid_to: date | None = None
    source_system: SourceSystemKey | None = None
    source_scope: SourceIdentifier | None = None
    ecosystem: EcosystemKey | None = None
    platform: PlatformKey | None = None
    channel_group: ChannelGroupKey | None = None
    channel: ChannelKey | None = None
    ad_product: AdProductKey | None = None
    marketplace: MarketplaceKey | None = None

    def __post_init__(self) -> None:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("entity alias interval must be increasing.")
        if not self.labels and not self.source_identifiers:
            raise ValueError("entity alias requires a display label or source identifier.")
        if len(set(self.source_identifiers)) != len(self.source_identifiers):
            raise ValueError("entity alias repeats a source identifier.")
        if (self.source_system is None) != (self.source_scope is None):
            raise ValueError("entity alias source system and scope must be supplied together.")
        _require_key_kind(self.kind, self.key)
        qualified_scope = getattr(self.key, "commerce_scope", self.commerce_scope)
        if qualified_scope != self.commerce_scope:
            raise ValueError("entity alias commerce scope contradicts its canonical key.")


@dataclass(frozen=True, slots=True)
class EntityContext:
    """Optional explicit differentiators that may narrow but never invent identity."""

    platform: PlatformKey | None = None
    channel: ChannelKey | None = None
    marketplace: MarketplaceKey | None = None

    def matches(self, alias: EntityAlias) -> bool:
        """Whether every supplied differentiator agrees with this authorized alias."""
        return all(
            expected is None or actual == expected
            for expected, actual in (
                (self.platform, alias.platform),
                (self.channel, alias.channel),
                (self.marketplace, alias.marketplace),
            )
        )


@dataclass(frozen=True, slots=True)
class EntityReference:
    """Exactly one text, canonical-key, or fully qualified source reference."""

    text: str | None = None
    canonical_key: CanonicalKey | None = None
    source_identifier: SourceIdentifier | None = None
    source_system: SourceSystemKey | None = None
    source_scope: SourceIdentifier | None = None

    def __post_init__(self) -> None:
        supplied = sum(
            (
                bool(self.text and self.text.strip()),
                self.canonical_key is not None,
                self.source_identifier is not None,
            )
        )
        if supplied != 1:
            raise ValueError("entity reference requires exactly one reference mode.")
        has_qualifier = self.source_system is not None or self.source_scope is not None
        if self.source_identifier is None and has_qualifier:
            raise ValueError("only a source reference may carry source-system scope.")
        if self.source_identifier is not None and (
            (self.source_system is None) != (self.source_scope is None)
        ):
            raise ValueError("source reference system and scope must be supplied together.")


@dataclass(frozen=True, slots=True)
class ResolveEntityRequest:
    """Authorized entity request with effective date/window and explicit context."""

    scope: AuthorizedAnalyticalScope
    reference: EntityReference
    origin: FieldOrigin
    aliases: tuple[EntityAlias, ...]
    kinds: frozenset[EntityKind] = frozenset()
    window: DateRange | None = None
    current_on: date | None = None
    context: EntityContext = EntityContext()

    def __post_init__(self) -> None:
        if self.window is None and self.current_on is None:
            raise ValueError("entity resolution requires a requested window or current day.")


@dataclass(frozen=True, slots=True)
class ResolutionCandidate:
    """One canonical entity with every effective label retained for clarification."""

    kind: EntityKind
    key: CanonicalKey
    labels: tuple[EffectiveLabel, ...]
    platform: PlatformKey | None
    channel: ChannelKey | None
    marketplace: MarketplaceKey | None
    evidence: tuple[MappingEvidence, ...]


@dataclass(frozen=True, slots=True)
class ResolutionRecord[Value]:
    """One field's typed outcome, value, candidates, origin, and mapping evidence."""

    field: str
    origin: FieldOrigin
    outcome: ResolutionOutcome
    value: Value | None = None
    candidates: tuple[ResolutionCandidate, ...] = ()
    evidence: tuple[MappingEvidence, ...] = ()

    def __post_init__(self) -> None:
        if not self.field.strip():
            raise ValueError("resolution record field cannot be blank.")
        if self.outcome is ResolutionOutcome.RESOLVED:
            if self.value is None or not self.evidence or len(self.candidates) != 1:
                raise ValueError("resolved record requires one value, candidate, and evidence.")
        elif self.value is not None:
            raise ValueError("only a resolved record may carry a value.")
        if (
            self.outcome is ResolutionOutcome.AMBIGUOUS
            and len(self.candidates) < _MIN_AMBIGUOUS_CANDIDATES
        ):
            raise ValueError("ambiguous record requires at least two candidates.")
        if (
            self.outcome
            in {
                ResolutionOutcome.NOT_FOUND,
                ResolutionOutcome.UNAUTHORIZED_OR_HIDDEN,
            }
            and self.candidates
        ):
            raise ValueError("not-found and hidden outcomes cannot disclose candidates.")


def normalize_exact_text(value: str) -> str:
    """Normalize case, compatibility forms, punctuation, separators, and whitespace only."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    separated = "".join(
        " " if unicodedata.category(character)[0] in {"P", "S", "Z"} else character
        for character in normalized
    )
    return " ".join(separated.split())


def resolve_entity(request: ResolveEntityRequest) -> ResolutionRecord[CanonicalKey]:
    """Resolve one authorized canonical entity or return a typed non-query outcome."""
    authorized = tuple(
        alias
        for alias in request.aliases
        if request.scope.allows(alias.commerce_scope)
        and (not request.kinds or alias.kind in request.kinds)
    )
    lexical, hidden = _lexical_matches(request, authorized)
    effective_matches = tuple(alias for alias in lexical if _effective(alias, request))
    matched_keys = {(alias.kind, alias.key) for alias in effective_matches}
    effective = tuple(
        alias
        for alias in authorized
        if (alias.kind, alias.key) in matched_keys and _effective(alias, request)
    )
    contextual = tuple(alias for alias in effective if request.context.matches(alias))
    if not lexical:
        outcome = (
            ResolutionOutcome.UNAUTHORIZED_OR_HIDDEN if hidden else ResolutionOutcome.NOT_FOUND
        )
        return ResolutionRecord("entity", request.origin, outcome)
    if not effective or not contextual:
        candidates = _candidates(effective or lexical)
        return ResolutionRecord(
            "entity",
            request.origin,
            ResolutionOutcome.INVALID_CONTEXT,
            candidates=candidates,
        )
    candidates = _candidates(contextual)
    if len(candidates) > 1:
        return ResolutionRecord(
            "entity",
            request.origin,
            ResolutionOutcome.AMBIGUOUS,
            candidates=candidates,
        )
    candidate = next(iter(candidates))
    return ResolutionRecord(
        "entity",
        request.origin,
        ResolutionOutcome.RESOLVED,
        value=candidate.key,
        candidates=candidates,
        evidence=candidate.evidence,
    )


def _lexical_matches(
    request: ResolveEntityRequest,
    authorized: tuple[EntityAlias, ...],
) -> tuple[tuple[EntityAlias, ...], bool]:
    reference = request.reference
    if reference.text is not None:
        expected = normalize_exact_text(reference.text)
        return (
            tuple(
                alias
                for alias in authorized
                if any(label.normalized == expected for label in alias.labels)
            ),
            False,
        )
    if reference.canonical_key is not None:
        matches = tuple(alias for alias in authorized if alias.key == reference.canonical_key)
        hidden = not matches and any(
            alias.key == reference.canonical_key for alias in request.aliases
        )
        return matches, hidden
    source = cast("SourceIdentifier", reference.source_identifier)
    matches = tuple(
        alias
        for alias in authorized
        if source in alias.source_identifiers and _source_scope_matches(alias, reference)
    )
    hidden = not matches and any(
        source in alias.source_identifiers and _source_scope_matches(alias, reference)
        for alias in request.aliases
    )
    return matches, hidden


def _source_scope_matches(alias: EntityAlias, reference: EntityReference) -> bool:
    if alias.source_system is None:
        return reference.source_system is None and reference.source_scope is None
    return (
        reference.source_system == alias.source_system
        and reference.source_scope == alias.source_scope
    )


def _effective(alias: EntityAlias, request: ResolveEntityRequest) -> bool:
    if request.window is not None:
        return alias.valid_from <= request.window.end and (
            alias.valid_to is None or request.window.start < alias.valid_to
        )
    current = cast("date", request.current_on)
    return alias.valid_from <= current and (alias.valid_to is None or current < alias.valid_to)


def _candidates(aliases: tuple[EntityAlias, ...]) -> tuple[ResolutionCandidate, ...]:
    grouped: dict[tuple[EntityKind, CanonicalKey], list[EntityAlias]] = {}
    for alias in aliases:
        grouped.setdefault((alias.kind, alias.key), []).append(alias)
    return tuple(
        ResolutionCandidate(
            kind=kind,
            key=key,
            labels=tuple(
                sorted(
                    {label for alias in values for label in alias.labels},
                    key=lambda item: (item.valid_from, item.value),
                )
            ),
            platform=values[0].platform,
            channel=values[0].channel,
            marketplace=values[0].marketplace,
            evidence=tuple(
                sorted(
                    {alias.evidence for alias in values},
                    key=lambda item: (item.mapping_version, item.reference),
                )
            ),
        )
        for (kind, key), values in sorted(
            grouped.items(), key=lambda item: (item[0][0].value, item[0][1].value)
        )
    )


def _require_key_kind(kind: EntityKind, key: CanonicalKey) -> None:
    expected: dict[EntityKind, type[CanonicalKey]] = {
        EntityKind.MARKETPLACE: MarketplaceKey,
        EntityKind.ADVERTISER: AdvertiserKey,
        EntityKind.CAMPAIGN: CampaignKey,
        EntityKind.BRAND: BrandKey,
        EntityKind.PRODUCT: ProductKey,
        EntityKind.ECOSYSTEM: EcosystemKey,
        EntityKind.PLATFORM: PlatformKey,
        EntityKind.CHANNEL_GROUP: ChannelGroupKey,
        EntityKind.CHANNEL: ChannelKey,
        EntityKind.AD_PRODUCT: AdProductKey,
    }
    if not isinstance(key, expected[kind]):
        raise TypeError(f"{kind.value} alias requires {expected[kind].__name__}.")
