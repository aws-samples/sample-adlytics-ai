# SPDX-License-Identifier: MIT-0
"""Physical evidence identity and analytical identity are different typed domains.

ADR-045. A string may look identical at both boundaries and still mean something different:
`ATVPDKIKX0DER` can route a delivery, identify an Amazon marketplace alias, and qualify an ASIN.
Treating those as one key is what made a fact DELETE by a manifest id and INSERT rows carrying
`AMAZON.COM`. The types here make substitution explicit and resolution evidence durable.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Sequence

__all__ = [
    "AcquisitionChannelGroupKey",
    "AdProductKey",
    "AdvertiserKey",
    "BrandKey",
    "CampaignKey",
    "CanonicalAdvertiserKeyRequest",
    "CanonicalCampaignKeyRequest",
    "CanonicalKey",
    "ChannelGroupKey",
    "ChannelKey",
    "CommerceScopeKey",
    "DeliveryScope",
    "EcosystemKey",
    "ExposureGroupKey",
    "ExtractionProfileKey",
    "IdentityMapping",
    "IdentityResolution",
    "IdentityResolutionError",
    "MarketplaceKey",
    "PlatformKey",
    "ProductKey",
    "SourceDatasetKey",
    "SourceIdentifier",
    "SourceSystemKey",
    "canonical_advertiser_key",
    "canonical_brand_key",
    "canonical_campaign_key",
    "resolve_identity",
]

_KEY = re.compile(r"^[a-z][a-z0-9_.:-]*$")
_SOURCE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_NAMESPACE = re.compile(r"^[a-z][a-z0-9_.:-]*$")


class IdentityResolutionError(ValueError):
    """A source identifier cannot be resolved to exactly one canonical identity."""


def _key(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not _KEY.fullmatch(normalized):
        raise ValueError(
            f"{label} is {value!r}; expected a lowercase semantic key beginning with a letter "
            "and containing only letters, digits, dot, underscore, colon or dash."
        )
    return normalized


def _source_key(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not _SOURCE_KEY.fullmatch(normalized):
        raise ValueError(
            f"{label} is {value!r}; expected a source-owned identifier beginning with a letter "
            "and containing only letters, digits, dot, underscore, colon or dash."
        )
    return normalized


@dataclass(frozen=True, slots=True)
class CanonicalKey:
    """Base type every analytical identity must extend to cross the resolution boundary."""

    value: str

    def __post_init__(self) -> None:
        if type(self) is CanonicalKey:
            raise TypeError(
                "CanonicalKey is an abstract identity boundary; use a concrete platform, channel, "
                "commerce-scope, marketplace or product key."
            )
        object.__setattr__(self, "value", _key(self.value, label=type(self).__name__))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SourceSystemKey:
    """Physical system that emitted evidence, such as `amc` or `vendor_central`."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _key(self.value, label="source system"))


@dataclass(frozen=True, slots=True)
class SourceDatasetKey:
    """Dataset inside a source system, never a business-facing channel."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _source_key(self.value, label="source dataset"))


@dataclass(frozen=True, slots=True)
class ExtractionProfileKey:
    """Stable semantic identity of an extraction definition, independent of SQL formatting."""

    value: str
    version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _key(self.value, label="extraction profile"))
        if self.version < 1:
            raise ValueError("an extraction profile version starts at 1.")


@dataclass(frozen=True, slots=True)
class EcosystemKey(CanonicalKey):
    """Broad business ecosystem, such as Amazon or Google."""


@dataclass(frozen=True, slots=True)
class PlatformKey(CanonicalKey):
    """Business service inside an ecosystem, such as Amazon Ads or Amazon Retail."""


@dataclass(frozen=True, slots=True)
class ChannelGroupKey(CanonicalKey):
    """Cross-platform media family, such as search, display, or video."""


@dataclass(frozen=True, slots=True)
class ChannelKey(CanonicalKey):
    """Governed advertising delivery channel, such as Amazon DSP."""


@dataclass(frozen=True, slots=True)
class AdProductKey(CanonicalKey):
    """Platform-specific advertising buying or serving product."""


@dataclass(frozen=True, slots=True)
class ExposureGroupKey(CanonicalKey):
    """Which ad sources one shopper saw within one platform, such as `dsp_and_sponsored`.

    ADR-101. A value is a combination of exposure sources inside one platform; another network's
    groups are its own combinations, never a shared vocabulary.
    """


@dataclass(frozen=True, slots=True)
class AcquisitionChannelGroupKey(CanonicalKey):
    """Which ad sources were credited with one shopper's new-to-brand purchase (ADR-101, ADR-104).

    Shares its keys with `ExposureGroupKey` so the two read alike, but it's a separate axis: one is
    about delivery, the other about credit.
    """


@dataclass(frozen=True, slots=True)
class CommerceScopeKey(CanonicalKey):
    """Canonical business scope whose activity a fact measures."""


@dataclass(frozen=True, slots=True)
class BrandKey(CanonicalKey):
    """Stable brand identity scoped by one canonical commerce population."""

    commerce_scope: CommerceScopeKey

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(cast("object", self.commerce_scope), CommerceScopeKey):
            raise TypeError("brand key requires a CommerceScopeKey qualifier.")


@dataclass(frozen=True, slots=True)
class AdvertiserKey(CanonicalKey):
    """Opaque advertiser identity scoped by commerce population and platform."""

    commerce_scope: CommerceScopeKey
    platform: PlatformKey

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(cast("object", self.commerce_scope), CommerceScopeKey):
            raise TypeError("advertiser key requires a CommerceScopeKey qualifier.")
        if not isinstance(cast("object", self.platform), PlatformKey):
            raise TypeError("advertiser key requires a PlatformKey qualifier.")


@dataclass(frozen=True, slots=True)
class CampaignKey(CanonicalKey):
    """Opaque campaign identity scoped by commerce population and platform."""

    commerce_scope: CommerceScopeKey
    platform: PlatformKey

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(cast("object", self.commerce_scope), CommerceScopeKey):
            raise TypeError("campaign key requires a CommerceScopeKey qualifier.")
        if not isinstance(cast("object", self.platform), PlatformKey):
            raise TypeError("campaign key requires a PlatformKey qualifier.")


@dataclass(frozen=True, slots=True)
class MarketplaceKey(CanonicalKey):
    """Canonical market population, resolved from names, ids or country aliases."""


@dataclass(frozen=True, slots=True)
class ProductKey(CanonicalKey):
    """Durable product entity resolved inside a canonical commerce scope.

    `commerce_scope` and `marketplace` prevent a source-local code from being promoted into an
    apparently global product key. A future governed global identity may be a different type; this
    one says exactly what has been established now.
    """

    commerce_scope: CommerceScopeKey
    marketplace: MarketplaceKey

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(cast("object", self.commerce_scope), CommerceScopeKey):
            raise TypeError("product key requires a CommerceScopeKey qualifier.")
        if not isinstance(cast("object", self.marketplace), MarketplaceKey):
            raise TypeError("product key requires a MarketplaceKey qualifier.")


@dataclass(frozen=True, slots=True)
class SourceIdentifier:
    """A value qualified by the namespace that gives it meaning.

    `AMAZON.COM` as an AMC marketplace name and `ATVPDKIKX0DER` as an Amazon marketplace id
    are two source identifiers. Neither is a `MarketplaceKey` until a governed mapping resolves it.
    """

    namespace: str
    value: str

    def __post_init__(self) -> None:
        namespace = self.namespace.strip()
        value = self.value.strip()
        if not _NAMESPACE.fullmatch(namespace):
            raise ValueError(f"identifier namespace is invalid: {self.namespace!r}.")
        if not value:
            raise ValueError(f"{namespace}: a source identifier value cannot be empty.")
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "value", value)


@dataclass(frozen=True, slots=True)
class DeliveryScope:
    """Physical acquisition scope used for routing, replay and provenance.

    None of these fields is an analytical join key. The option hash is physical dataset identity;
    profile id/version says which extraction semantics produced it; `source_scope` remains qualified
    because a credential or producer routing value does not prove a canonical commerce scope.
    """

    source_system: SourceSystemKey
    source_dataset: SourceDatasetKey
    extraction_profile: ExtractionProfileKey
    source_scope: SourceIdentifier
    option_set_hash: str

    def __post_init__(self) -> None:
        if not self.option_set_hash.strip():
            raise ValueError("delivery scope requires an option-set hash.")


@dataclass(frozen=True, slots=True)
class CanonicalCampaignKeyRequest:
    """Complete equivalence inputs for one source-native campaign identity."""

    commerce_scope: CommerceScopeKey
    platform: PlatformKey
    channel: ChannelKey
    source_system: SourceSystemKey
    source_scope: SourceIdentifier
    identifier_kind: str
    source_value: str

    def __post_init__(self) -> None:
        qualifiers = (
            (self.commerce_scope, CommerceScopeKey, "commerce scope"),
            (self.platform, PlatformKey, "platform"),
            (self.channel, ChannelKey, "channel"),
            (self.source_system, SourceSystemKey, "source system"),
            (self.source_scope, SourceIdentifier, "source scope"),
        )
        for value, expected, label in qualifiers:
            if not isinstance(cast("object", value), expected):
                raise TypeError(f"campaign key request requires a typed {label}.")
        object.__setattr__(
            self,
            "identifier_kind",
            _key(self.identifier_kind, label="campaign identifier kind"),
        )
        source_value = self.source_value.strip()
        if not source_value:
            raise ValueError("campaign source value cannot be empty.")
        object.__setattr__(self, "source_value", source_value)


@dataclass(frozen=True, slots=True)
class CanonicalAdvertiserKeyRequest:
    """Complete equivalence inputs for one account-qualified advertiser identity."""

    commerce_scope: CommerceScopeKey
    platform: PlatformKey
    source_system: SourceSystemKey
    source_scope: SourceIdentifier
    source: SourceIdentifier

    def __post_init__(self) -> None:
        qualifiers = (
            (self.commerce_scope, CommerceScopeKey, "commerce scope"),
            (self.platform, PlatformKey, "platform"),
            (self.source_system, SourceSystemKey, "source system"),
            (self.source_scope, SourceIdentifier, "source scope"),
            (self.source, SourceIdentifier, "source identifier"),
        )
        for value, expected, label in qualifiers:
            if not isinstance(cast("object", value), expected):
                raise TypeError(f"advertiser key request requires a typed {label}.")


def _require_canonical(value: object) -> None:
    """Enforce the runtime boundary even when an untyped adapter bypasses static checking."""
    if not isinstance(value, CanonicalKey):
        raise TypeError("identity mapping target must be a canonical key type.")


@dataclass(frozen=True, slots=True)
class IdentityMapping[Key: CanonicalKey]:
    """One versioned, effective-dated source-to-canonical resolution."""

    source: SourceIdentifier
    target: Key
    mapping_version: int
    valid_from: date
    valid_to: date | None = None

    def __post_init__(self) -> None:
        _require_canonical(self.target)
        if self.mapping_version < 1:
            raise ValueError("an identity mapping version starts at 1.")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("an identity mapping cannot end before it begins.")

    def applies(self, source: SourceIdentifier, *, on: date) -> bool:
        """Whether this mapping resolves the source identifier on the requested business day."""
        return (
            self.source == source
            and self.valid_from <= on
            and (self.valid_to is None or on < self.valid_to)
        )


@dataclass(frozen=True, slots=True)
class IdentityResolution[Key: CanonicalKey]:
    """Reproducible evidence of one successful canonical identity resolution."""

    source: SourceIdentifier
    target: Key
    mapping_version: int
    valid_from: date
    valid_to: date | None


def canonical_brand_key(
    commerce_scope: CommerceScopeKey,
    source: SourceIdentifier,
) -> BrandKey:
    """Derive one opaque scoped brand key from a stable qualified source alias."""
    digest = hashlib.sha256(
        f"{commerce_scope}|{source.namespace}|{source.value}".encode()
    ).hexdigest()[:24]
    return BrandKey(f"brand:{digest}", commerce_scope)


def canonical_campaign_key(request: CanonicalCampaignKeyRequest) -> CampaignKey:
    """Derive an opaque campaign key from canonical and source-account equivalence inputs."""
    payload = json.dumps(
        [
            "canonical-campaign-key:v2",
            request.commerce_scope.value,
            request.platform.value,
            request.channel.value,
            request.source_system.value,
            request.source_scope.namespace,
            request.source_scope.value,
            request.identifier_kind,
            request.source_value,
        ],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()[:24]
    return CampaignKey(
        f"campaign:{digest}",
        request.commerce_scope,
        request.platform,
    )


def canonical_advertiser_key(request: CanonicalAdvertiserKeyRequest) -> AdvertiserKey:
    """Derive an opaque advertiser key from a source-account-qualified native alias."""
    payload = json.dumps(
        [
            "canonical-advertiser-key:v2",
            request.commerce_scope.value,
            request.platform.value,
            request.source_system.value,
            request.source_scope.namespace,
            request.source_scope.value,
            request.source.namespace,
            request.source.value,
        ],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()[:24]
    return AdvertiserKey(
        f"advertiser:{digest}",
        request.commerce_scope,
        request.platform,
    )


def resolve_identity[Key: CanonicalKey](
    source: SourceIdentifier,
    *,
    on: date,
    mappings: Sequence[IdentityMapping[Key]],
) -> IdentityResolution[Key]:
    """Resolve one source identifier through exactly one active mapping.

    Zero matches is unresolved. Several active mappings are ambiguous even when they currently point
    at the same target: choosing one would discard which mapping version supplied the evidence and
    make a later audit depend on list order.
    """
    candidates = [mapping for mapping in mappings if mapping.applies(source, on=on)]
    if not candidates:
        raise IdentityResolutionError(
            f"{source.namespace}:{source.value} has no canonical mapping on {on}."
        )
    if len(candidates) != 1:
        rendered = ", ".join(
            sorted(f"v{mapping.mapping_version}->{mapping.target}" for mapping in candidates)
        )
        raise IdentityResolutionError(
            f"{source.namespace}:{source.value} resolves ambiguously on {on}: {rendered}."
        )
    selected = candidates[0]
    return IdentityResolution(
        source=source,
        target=selected.target,
        mapping_version=selected.mapping_version,
        valid_from=selected.valid_from,
        valid_to=selected.valid_to,
    )
