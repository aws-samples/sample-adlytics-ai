# SPDX-License-Identifier: MIT-0
"""Load the governed media taxonomy without inferring source or analytical axes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

import yaml

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
from domain.semantics.media_taxonomy import (
    ExposureSourceSetMapping,
    MediaTaxonomy,
    MediaTaxonomyMapping,
    MediaTaxonomyResolution,
    MediaTaxonomyTerm,
)

TAXONOMY_PATH = Path("contracts/shared/media-taxonomy.yaml")
TAXONOMY_VERSION = 2
type RawMapping = dict[str, object]


def load_media_taxonomy(root: Path) -> MediaTaxonomy:
    """Load every named axis and exact effective-dated source mapping."""
    path = root / TAXONOMY_PATH
    raw = _mapping(cast("object", yaml.safe_load(path.read_text())), str(path))
    # Version 2 added `exposure_source_sets` (ADR-101, business-outcomes C-16). The key set is
    # exact, so a version-1 loader would reject a version-2 file either way; the version names
    # that break rather than leaving it to a key-set error.
    required = {
        "version",
        "ecosystems",
        "platforms",
        "channel_groups",
        "channels",
        "ad_products",
        "mappings",
        "exposure_source_sets",
    }
    if set(raw) != required or raw["version"] != TAXONOMY_VERSION:
        raise ValueError(
            f"{path}: media taxonomy requires exactly {sorted(required)} at version "
            f"{TAXONOMY_VERSION}."
        )
    definitions = _definitions(raw)
    mappings = tuple(
        _mapping_value(item, definitions.keys) for item in _list(raw["mappings"], "mappings")
    )
    source_sets = tuple(
        _source_set_value(item, definitions.keys)
        for item in _list(raw["exposure_source_sets"], "exposure_source_sets")
    )
    return MediaTaxonomy(
        version=TAXONOMY_VERSION,
        mappings=mappings,
        terms=definitions.terms,
        exposure_source_sets=source_sets,
    )


@dataclass(frozen=True, slots=True)
class _TaxonomyKeys:
    ecosystems: dict[str, EcosystemKey]
    platforms: dict[str, PlatformKey]
    groups: dict[str, ChannelGroupKey]
    channels: dict[str, ChannelKey]
    products: dict[str, AdProductKey]


@dataclass(frozen=True, slots=True)
class _TaxonomyDefinitions:
    keys: _TaxonomyKeys
    terms: tuple[MediaTaxonomyTerm, ...]


def _definitions(raw: RawMapping) -> _TaxonomyDefinitions:
    ecosystems: dict[str, EcosystemKey] = {}
    terms: list[MediaTaxonomyTerm] = []
    for value in _list(raw["ecosystems"], "ecosystems"):
        item = _exact(value, "ecosystems[]", {"key", "name"})
        key = _add(ecosystems, item["key"], EcosystemKey)
        terms.append(MediaTaxonomyTerm(key=key, name=_text(item["name"]), ecosystem=key))

    platforms: dict[str, PlatformKey] = {}
    platform_ecosystems: dict[PlatformKey, EcosystemKey] = {}
    for value in _list(raw["platforms"], "platforms"):
        item = _exact(value, "platforms[]", {"key", "ecosystem", "name"})
        key = _add(platforms, item["key"], PlatformKey)
        ecosystem = ecosystems[_text(item["ecosystem"])]
        platform_ecosystems[key] = ecosystem
        terms.append(
            MediaTaxonomyTerm(
                key=key,
                name=_text(item["name"]),
                ecosystem=ecosystem,
                platform=key,
            )
        )

    groups: dict[str, ChannelGroupKey] = {}
    for value in _list(raw["channel_groups"], "channel_groups"):
        item = _exact(value, "channel_groups[]", {"key", "name"})
        key = _add(groups, item["key"], ChannelGroupKey)
        terms.append(MediaTaxonomyTerm(key=key, name=_text(item["name"]), channel_group=key))

    channels: dict[str, ChannelKey] = {}
    channel_terms: dict[ChannelKey, MediaTaxonomyTerm] = {}
    for value in _list(raw["channels"], "channels"):
        item = _exact(
            value,
            "channels[]",
            {"key", "platform", "channel_group", "name"},
        )
        key = _add(channels, item["key"], ChannelKey)
        platform = platforms[_text(item["platform"])]
        group = groups[_text(item["channel_group"])]
        term = MediaTaxonomyTerm(
            key=key,
            name=_text(item["name"]),
            ecosystem=platform_ecosystems[platform],
            platform=platform,
            channel_group=group,
            channel=key,
        )
        channel_terms[key] = term
        terms.append(term)

    products: dict[str, AdProductKey] = {}
    for value in _list(raw["ad_products"], "ad_products"):
        item = _exact(
            value,
            "ad_products[]",
            {"key", "platform", "channel", "name"},
        )
        key = _add(products, item["key"], AdProductKey)
        platform = platforms[_text(item["platform"])]
        channel = channels[_text(item["channel"])]
        channel_term = channel_terms[channel]
        if channel_term.platform != platform:
            raise ValueError("ad product platform contradicts its governed channel.")
        terms.append(
            MediaTaxonomyTerm(
                key=key,
                name=_text(item["name"]),
                ecosystem=platform_ecosystems[platform],
                platform=platform,
                channel_group=channel_term.channel_group,
                channel=channel,
                ad_product=key,
            )
        )

    return _TaxonomyDefinitions(
        keys=_TaxonomyKeys(ecosystems, platforms, groups, channels, products),
        terms=tuple(terms),
    )


def _mapping_value(raw: object, keys: _TaxonomyKeys) -> MediaTaxonomyMapping:
    item = _mapping(raw, "mappings[]")
    expected = {
        "source",
        "ecosystem",
        "platform",
        "channel_group",
        "channel",
        "ad_product",
        "mapping_version",
        "valid_from",
        "evidence",
    }
    if set(item) - (expected | {"valid_to"}) or expected - set(item):
        raise ValueError("media taxonomy mapping has unknown or missing keys.")
    source = _exact(item["source"], "mapping source", {"namespace", "value"})
    return MediaTaxonomyMapping(
        MediaTaxonomyResolution(
            source=SourceIdentifier(_text(source["namespace"]), _text(source["value"])),
            ecosystem=keys.ecosystems[_text(item["ecosystem"])],
            platform=keys.platforms[_text(item["platform"])],
            channel_group=_optional(item["channel_group"], keys.groups),
            channel=_optional(item["channel"], keys.channels),
            ad_product=_optional(item["ad_product"], keys.products),
            mapping_version=_integer(item["mapping_version"], "mapping_version"),
            valid_from=_day(item["valid_from"], "valid_from"),
            valid_to=(None if item.get("valid_to") is None else _day(item["valid_to"], "valid_to")),
            evidence=_text(item["evidence"]),
        )
    )


def _source_set_value(raw: object, keys: _TaxonomyKeys) -> ExposureSourceSetMapping:
    item = _mapping(raw, "exposure_source_sets[]")
    expected = {
        "platform",
        "source_set",
        "exposure_group",
        "acquisition_channel_group",
        "mapping_version",
        "valid_from",
        "evidence",
    }
    if set(item) - (expected | {"valid_to"}) or expected - set(item):
        raise ValueError("exposure source set mapping has unknown or missing keys.")
    platform = _text(item["platform"])
    if platform not in keys.platforms:
        raise ValueError(f"exposure source set names undeclared platform {platform!r}.")
    return ExposureSourceSetMapping(
        platform=keys.platforms[platform],
        source_set=_text(item["source_set"]),
        exposure_group=ExposureGroupKey(_text(item["exposure_group"])),
        acquisition_channel_group=AcquisitionChannelGroupKey(
            _text(item["acquisition_channel_group"])
        ),
        mapping_version=_integer(item["mapping_version"], "mapping_version"),
        valid_from=_day(item["valid_from"], "valid_from"),
        valid_to=None if item.get("valid_to") is None else _day(item["valid_to"], "valid_to"),
        evidence=_text(item["evidence"]),
    )


def _add[Key: CanonicalKey](values: dict[str, Key], value: object, factory: type[Key]) -> Key:
    raw = _text(value)
    if raw in values:
        raise ValueError(f"{factory.__name__} repeats {raw!r}.")
    key = factory(raw)
    values[raw] = key
    return key


def _optional[Key: CanonicalKey](value: object, values: dict[str, Key]) -> Key | None:
    return None if value is None else values[_text(value)]


def _exact(value: object, label: str, expected: set[str]) -> RawMapping:
    item = _mapping(value, label)
    if set(item) != expected:
        raise ValueError(f"{label} requires exactly {sorted(expected)}.")
    return item


def _mapping(value: object, label: str) -> RawMapping:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping.")
    return cast("RawMapping", value)


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    return cast("list[object]", value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("media taxonomy value must be a non-empty string.")
    return value.strip()


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _day(value: object, label: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(_text(value))
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO date.") from error
