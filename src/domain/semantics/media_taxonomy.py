# SPDX-License-Identifier: MIT-0
"""Governed ecosystem, platform, channel-group, channel, and ad-product resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from domain.semantics.identity import (
    AcquisitionChannelGroupKey,
    AdProductKey,
    CanonicalKey,
    ChannelGroupKey,
    ChannelKey,
    EcosystemKey,
    ExposureGroupKey,
    PlatformKey,
    SourceIdentifier,
)

type MediaTaxonomyKey = EcosystemKey | PlatformKey | ChannelGroupKey | ChannelKey | AdProductKey


@dataclass(frozen=True, slots=True)
class MediaTaxonomyTerm:
    """One named canonical taxonomy value with its governed hierarchy qualifiers."""

    key: MediaTaxonomyKey
    name: str
    ecosystem: EcosystemKey | None = None
    platform: PlatformKey | None = None
    channel_group: ChannelGroupKey | None = None
    channel: ChannelKey | None = None
    ad_product: AdProductKey | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("media taxonomy term requires a display name.")
        expected = {
            EcosystemKey: self.ecosystem,
            PlatformKey: self.platform,
            ChannelGroupKey: self.channel_group,
            ChannelKey: self.channel,
            AdProductKey: self.ad_product,
        }
        if expected[type(self.key)] != self.key:
            raise ValueError("media taxonomy term must carry its key on the matching axis.")
        if isinstance(self.key, PlatformKey) and self.ecosystem is None:
            raise ValueError("platform term requires its ecosystem.")
        if isinstance(self.key, ChannelKey) and any(
            value is None for value in (self.ecosystem, self.platform, self.channel_group)
        ):
            raise ValueError("channel term requires ecosystem, platform, and channel group.")
        if isinstance(self.key, AdProductKey) and any(
            value is None
            for value in (
                self.ecosystem,
                self.platform,
                self.channel_group,
                self.channel,
            )
        ):
            raise ValueError("ad-product term requires its complete media hierarchy.")


@dataclass(frozen=True, slots=True)
class MediaTaxonomyResolution:
    """One source media identity resolved to all applicable canonical axes."""

    source: SourceIdentifier
    ecosystem: EcosystemKey
    platform: PlatformKey
    channel_group: ChannelGroupKey | None
    channel: ChannelKey | None
    ad_product: AdProductKey | None
    mapping_version: int
    valid_from: date
    valid_to: date | None
    evidence: str

    def __post_init__(self) -> None:
        if self.mapping_version < 1 or not self.evidence.strip():
            raise ValueError("media taxonomy resolution requires versioned evidence.")
        advertising = (self.channel_group, self.channel, self.ad_product)
        if any(value is None for value in advertising) and any(
            value is not None for value in advertising
        ):
            raise ValueError("advertising media axes are present together or absent together.")


@dataclass(frozen=True, slots=True)
class MediaTaxonomyMapping:
    """One effective-dated source-to-canonical media taxonomy mapping."""

    resolution: MediaTaxonomyResolution

    def applies(self, source: SourceIdentifier, on: date) -> bool:
        """Whether this mapping governs the source identity on the supplied day."""
        value = self.resolution
        return (
            value.source == source
            and value.valid_from <= on
            and (value.valid_to is None or on < value.valid_to)
        )


@dataclass(frozen=True, slots=True)
class ResolveExposureSourceSetRequest:
    """One platform's source-set label as a pack emitted it, and the row's business day."""

    platform: PlatformKey
    source_set: str
    on: date


@dataclass(frozen=True, slots=True)
class ExposureSourceSetMapping:
    """One platform's source-set label resolved to its canonical grouping keys (ADR-101).

    A shopper-aggregate pack emits a source fact, such as `dsp+sponsored` ("touched in
    `dsp_impressions` and in `sponsored_ads_traffic`"), never a canonical key (AC-BR.9). This
    mapping is where that label becomes `exposure_group` and `acquisition_channel_group`, owned
    by the one taxonomy like every other media axis (ADR-052, ADR-044).
    """

    platform: PlatformKey
    source_set: str
    exposure_group: ExposureGroupKey
    acquisition_channel_group: AcquisitionChannelGroupKey
    mapping_version: int
    valid_from: date
    valid_to: date | None
    evidence: str

    def __post_init__(self) -> None:
        if not self.source_set.strip() or self.source_set != self.source_set.strip():
            raise ValueError("exposure source set requires an exact, non-blank source label.")
        if self.mapping_version < 1 or not self.evidence.strip():
            raise ValueError("exposure source set mapping requires versioned evidence.")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("exposure source set mapping must end after it begins.")

    def applies(self, platform: PlatformKey, source_set: str, on: date) -> bool:
        """Whether this mapping governs the platform's source set on the supplied day."""
        return (
            self.platform == platform
            and self.source_set == source_set
            and self.valid_from <= on
            and (self.valid_to is None or on < self.valid_to)
        )


@dataclass(frozen=True, slots=True)
class MediaTaxonomy:
    """Complete governed media definitions and effective-date mappings."""

    version: int
    mappings: tuple[MediaTaxonomyMapping, ...]
    terms: tuple[MediaTaxonomyTerm, ...]
    exposure_source_sets: tuple[ExposureSourceSetMapping, ...]

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("media taxonomy version starts at 1.")
        if not self.mappings or not self.terms:
            raise ValueError("media taxonomy requires mappings and named terms.")
        identities = tuple((type(term.key), term.key) for term in self.terms)
        if len(set(identities)) != len(identities):
            raise ValueError("media taxonomy repeats a named canonical term.")
        terms = {term.key: term for term in self.terms}
        for mapping in self.mappings:
            value = mapping.resolution
            selected: tuple[CanonicalKey | None, ...] = (
                value.ecosystem,
                value.platform,
                value.channel_group,
                value.channel,
                value.ad_product,
            )
            if any(key is not None and key not in terms for key in selected):
                raise ValueError("media taxonomy mapping references an unnamed term.")
        for index, left in enumerate(self.mappings):
            for right in self.mappings[index + 1 :]:
                if left.resolution.source == right.resolution.source and _overlaps(left, right):
                    raise ValueError("media taxonomy contains overlapping source mappings.")
        self._validate_exposure_source_sets()

    def _validate_exposure_source_sets(self) -> None:
        """Refuse a source-set table that could put one shopper in two groups.

        Each axis must stay disjoint (ADR-101 D-3): within one platform and overlapping dates, a
        source set maps once, and two source sets never share a group key on either axis.
        """
        platforms = {term.key for term in self.terms if isinstance(term.key, PlatformKey)}
        for item in self.exposure_source_sets:
            if item.platform not in platforms:
                raise ValueError(
                    f"exposure source set {item.source_set!r} names unnamed platform "
                    f"{item.platform}."
                )
        sets = self.exposure_source_sets
        for index, left in enumerate(sets):
            for right in sets[index + 1 :]:
                if left.platform != right.platform or not _dates_overlap(left, right):
                    continue
                if left.source_set == right.source_set:
                    raise ValueError(
                        f"{left.platform}: exposure source set {left.source_set!r} maps twice."
                    )
                if left.exposure_group == right.exposure_group:
                    raise ValueError(
                        f"{left.platform}: exposure group {left.exposure_group} is shared by "
                        f"{left.source_set!r} and {right.source_set!r}."
                    )
                if left.acquisition_channel_group == right.acquisition_channel_group:
                    raise ValueError(
                        f"{left.platform}: acquisition channel group "
                        f"{left.acquisition_channel_group} is shared by {left.source_set!r} and "
                        f"{right.source_set!r}."
                    )

    def resolve_exposure_source_set(
        self, request: ResolveExposureSourceSetRequest
    ) -> ExposureSourceSetMapping:
        """Resolve exactly one source-set mapping, or refuse absence and ambiguity."""
        matches = tuple(
            item
            for item in self.exposure_source_sets
            if item.applies(request.platform, request.source_set, request.on)
        )
        if len(matches) != 1:
            state = "no" if not matches else "ambiguous"
            raise ValueError(
                f"{request.platform}:{request.source_set} has {state} exposure source set "
                f"mapping on {request.on}."
            )
        return matches[0]

    def resolve(self, source: SourceIdentifier, on: date) -> MediaTaxonomyResolution:
        """Resolve exactly one effective media mapping or refuse ambiguity/absence."""
        matches = tuple(item.resolution for item in self.mappings if item.applies(source, on))
        if len(matches) != 1:
            state = "no" if not matches else "ambiguous"
            raise ValueError(
                f"{source.namespace}:{source.value} has {state} media mapping on {on}."
            )
        return matches[0]

    def term_for(self, key: MediaTaxonomyKey) -> MediaTaxonomyTerm:
        """Return the one governed named definition for a canonical media key."""
        matches = tuple(term for term in self.terms if term.key == key)
        if len(matches) != 1:
            raise ValueError(f"{key} has no unique named media taxonomy term.")
        return matches[0]


def _dates_overlap(left: ExposureSourceSetMapping, right: ExposureSourceSetMapping) -> bool:
    return (left.valid_to is None or right.valid_from < left.valid_to) and (
        right.valid_to is None or left.valid_from < right.valid_to
    )


def _overlaps(left: MediaTaxonomyMapping, right: MediaTaxonomyMapping) -> bool:
    left_value = left.resolution
    right_value = right.resolution
    return (left_value.valid_to is None or right_value.valid_from < left_value.valid_to) and (
        right_value.valid_to is None or left_value.valid_from < right_value.valid_to
    )
