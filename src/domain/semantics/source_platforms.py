# SPDX-License-Identifier: MIT-0
"""Derive the advertising platforms each simple metric's figures can come from (ADR-106).

The planner accounts the platform and ecosystem axes for an operand that can only come from one
platform: such a metric can't be summed across networks, because there is only one. So the set
must cover every platform whose rows the plan could read. That is why the walk uses **the same
match predicate as publication selection** (`PublicationSelector._selected_side`): a source pack
whose dataset is a fact the metric reads, and whose contract's `source_dataset` is one of the
metric's `source_datasets`, whatever the contract's `source_system`. A second network's contract
that reuses a dataset name is therefore counted the moment it could be read. If selection ever
checks `source_system` too, this predicate changes in the same commit; the coverage test in
`tests/unit/test_source_platforms.py` fails until it does.

Each matched pack's media identity resolves through the media taxonomy. A literal resolves to
its mappings' platforms. A path resolves to the union over **every** mapping in its namespace,
regardless of `valid_from`, so effective dating can never hide a platform that was, or will be,
mapped there.

The set comes from governed files, never from loaded data, so it can't change with what happens
to be published. A metric whose walk matches no pack, or reaches a pack whose media can't be
resolved, is refused at load rather than treated as single-platform.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from domain.catalog.capability import Catalog, CatalogError, MetricKind
from domain.semantics.identity import PlatformKey, SourceIdentifier

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Mapping

    from domain.data_contracts.contract import DataContract
    from domain.semantics.media_taxonomy import MediaTaxonomy
    from domain.semantics.source_pack import SourcePack

__all__ = [
    "SourcePlatformsRequest",
    "SourcePlatformsResult",
    "derive_source_platforms",
]


@dataclass(frozen=True, slots=True)
class SourcePlatformsRequest:
    """The four governed declarations the walk reads: metrics, contracts, packs, taxonomy."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    source_packs: tuple[SourcePack, ...]
    taxonomy: MediaTaxonomy


@dataclass(frozen=True, slots=True)
class SourcePlatformsResult:
    """Every simple metric's derived platform set, keyed by metric name."""

    platforms: Mapping[str, frozenset[PlatformKey]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "platforms", MappingProxyType(dict(self.platforms)))


def _pack_platforms(pack: SourcePack, taxonomy: MediaTaxonomy) -> frozenset[PlatformKey]:
    media = pack.media
    if media is None:
        raise CatalogError(
            f"{pack.contract_id}: the source pack declares no media identity, so the "
            "platforms its rows come from can't be derived."
        )
    if media.literal_value is not None:
        literal = SourceIdentifier(media.namespace, media.literal_value)
        resolutions = tuple(
            mapping.resolution
            for mapping in taxonomy.mappings
            if mapping.resolution.source == literal
        )
    else:
        resolutions = tuple(
            mapping.resolution
            for mapping in taxonomy.mappings
            if mapping.resolution.source.namespace == media.namespace
        )
    if not resolutions:
        shown = media.literal_value if media.literal_value is not None else media.source_path
        raise CatalogError(
            f"{pack.contract_id}: media {media.namespace}:{shown} has no media taxonomy "
            "mapping, so the platforms its rows come from can't be derived."
        )
    return frozenset(resolution.platform for resolution in resolutions)


def derive_source_platforms(request: SourcePlatformsRequest) -> SourcePlatformsResult:
    """Walk every simple metric to the packs selection can read, then to their platforms."""
    contracts = {contract.contract_id: contract for contract in request.contracts}
    platforms: dict[str, frozenset[PlatformKey]] = {}
    for metric in request.catalog.metrics:
        if metric.kind is not MetricKind.SIMPLE:
            continue
        facts = request.catalog.facts_read(metric)
        matched = tuple(
            pack
            for pack in request.source_packs
            if pack.dataset.value in facts
            and (contract := contracts.get(pack.contract_id)) is not None
            and contract.source_dataset in metric.source_datasets
        )
        if not matched:
            raise CatalogError(
                f"{metric.name}: no source pack writes {', '.join(facts)} from "
                f"{', '.join(metric.source_datasets)}, so the platforms its figures can come "
                "from can't be derived."
            )
        platforms[metric.name] = frozenset(
            platform for pack in matched for platform in _pack_platforms(pack, request.taxonomy)
        )
    return SourcePlatformsResult(platforms)
