# SPDX-License-Identifier: MIT-0
"""Derive the bounded Media scope catalog from governed declarations and CURRENT evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta

from adapters.outbound.publication_selection import PublicationScopeRequest, publication_scopes_for
from domain.data_contracts.contract import DataContract
from domain.presentation.artifacts import MediaScope, SafeMediaScopeDisplaySignature
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.commerce_identity import CommerceIdentityBridge
from domain.semantics.identity import MarketplaceKey, SourceIdentifier
from domain.semantics.media_taxonomy import MediaTaxonomy, MediaTaxonomyResolution
from domain.semantics.publication import PartitionIdentity, PublicationScope, SuccessfulPublication
from domain.semantics.source_pack import PackReadiness, SourcePack, SourcePackRole
from domain.semantics.time import FactTimeClass, MeasurementBucket, TimeGrain
from domain.shared_kernel.values import DateRange
from ports.outbound.media_scope_catalog import (
    MediaScopeCatalogCandidate,
    MediaScopeCatalogCoverageRequest,
    MediaScopeCatalogCoverageResult,
    MediaScopeCatalogListRequest,
    MediaScopeCatalogListResult,
    MediaScopeCatalogPort,
    MediaScopeCatalogRevalidationOutcome,
    MediaScopeCatalogRevalidationRequest,
    MediaScopeCatalogRevalidationResult,
)
from ports.outbound.semantic_state import CurrentPublicationPort

__all__ = ["GovernedMediaScopeCatalog"]


@dataclass(frozen=True, slots=True)
class _CatalogEvidence:
    """One declared, literal media profile with its exact CURRENT publication set."""

    pack: SourcePack
    contract: DataContract
    taxonomy: MediaTaxonomyResolution
    marketplace: MarketplaceKey
    mapping_versions: tuple[tuple[str, int], ...]
    publication_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MediaScopeCatalogDependencies:
    """Read-only deployment declarations and CURRENT evidence behind the catalog."""

    commerce_bridge: CommerceIdentityBridge
    taxonomy: MediaTaxonomy
    current: CurrentPublicationPort
    packs: tuple[SourcePack, ...]
    contracts: tuple[DataContract, ...]


class GovernedMediaScopeCatalog(MediaScopeCatalogPort):
    """Read-only catalog at the declared Media/profile/marketplace evidence intersection.

    A source pack with a dynamic media field never enumerates a tuple: its source values have no
    declaration-owned closed universe. This prevents a catalog from inferring Sponsored or any
    other profile from unrelated CURRENT publications.
    """

    def __init__(self, dependencies: MediaScopeCatalogDependencies) -> None:
        self._commerce_bridge = dependencies.commerce_bridge
        self._taxonomy = dependencies.taxonomy
        self._current = dependencies.current
        self._packs = dependencies.packs
        self._contracts = {contract.contract_id: contract for contract in dependencies.contracts}

    def coverage(
        self, request: MediaScopeCatalogCoverageRequest
    ) -> MediaScopeCatalogCoverageResult:
        """Derive only day windows with a qualified literal tuple/publication intersection."""
        days = sorted(self._publication_days())
        qualified = tuple(
            day for day in days if self._candidates(request.authorized_scope, DateRange(day, day))
        )
        return MediaScopeCatalogCoverageResult(_coalesce_days(qualified))

    def list_scopes(self, request: MediaScopeCatalogListRequest) -> MediaScopeCatalogListResult:
        """Return deterministic candidates from the complete declared evidence intersection."""
        candidates = self._candidates(request.authorized_scope, request.window)
        ordered = tuple(
            sorted(
                candidates,
                key=lambda candidate: (
                    candidate.safe_signature.label,
                    candidate.catalog_identity,
                ),
            )
        )
        return MediaScopeCatalogListResult(
            candidates=ordered[: request.max_candidates],
            truncated=len(ordered) > request.max_candidates,
        )

    def revalidate(
        self, request: MediaScopeCatalogRevalidationRequest
    ) -> MediaScopeCatalogRevalidationResult:
        """Re-derive without disclosing why a stored catalog entry is no longer current."""
        candidates = self._candidates(request.authorized_scope, request.window)
        current = any(
            candidate.scope == request.scope
            and candidate.catalog_identity == request.catalog_identity
            for candidate in candidates
        )
        return MediaScopeCatalogRevalidationResult(
            MediaScopeCatalogRevalidationOutcome.CURRENT
            if current
            else MediaScopeCatalogRevalidationOutcome.STALE_OR_UNAVAILABLE
        )

    def _candidates(
        self, authorized_scope: AuthorizedAnalyticalScope, window: DateRange
    ) -> tuple[MediaScopeCatalogCandidate, ...]:
        if not authorized_scope.allows(self._commerce_bridge.commerce_scope):
            return ()
        grouped: dict[MediaScope, tuple[_CatalogEvidence, ...]] = {}
        for taxonomy in self._literal_taxonomy_resolutions(window):
            for marketplace in self._marketplaces_for_all_packs(taxonomy, window):
                evidence = self._evidence_for(taxonomy, marketplace, window)
                if evidence is None:
                    continue
                channel = taxonomy.channel
                if channel is None:
                    raise RuntimeError("literal Media taxonomy resolution lacks a channel.")
                scope = MediaScope(
                    commerce_scope=self._commerce_bridge.commerce_scope,
                    platform=taxonomy.platform,
                    channel=channel,
                    marketplace=marketplace,
                )
                grouped[scope] = (*grouped.get(scope, ()), *evidence)
        return tuple(self._candidate(scope, evidence) for scope, evidence in grouped.items())

    def _publication_days(self) -> set[date]:
        """Read declared CURRENT partitions only to discover candidate day coverage."""
        days: set[date] = set()
        targets = {mapping.target for mapping in self._commerce_bridge.marketplace_mappings}
        for pack in self._eligible_packs():
            contract = self._contracts[pack.contract_id]
            for marketplace in targets:
                scopes = publication_scopes_for(
                    PublicationScopeRequest(
                        pack=pack,
                        contract=contract,
                        bridge=self._commerce_bridge,
                        filters={"marketplace_key": marketplace.value},
                    )
                )
                for scope in scopes:
                    for publication in self._current.current_for_scope(scope):
                        if not publication.evidence.publishable:
                            continue
                        bucket = publication.partition.bucket.window
                        days.update(
                            bucket.start + timedelta(days=offset) for offset in range(bucket.days)
                        )
        return days

    def _literal_taxonomy_resolutions(
        self, window: DateRange
    ) -> tuple[MediaTaxonomyResolution, ...]:
        resolutions: dict[SourceIdentifier, MediaTaxonomyResolution] = {}
        for pack in self._eligible_packs():
            media = pack.media
            if media is None or media.literal_value is None:
                continue
            source = SourceIdentifier(media.namespace, media.literal_value)
            resolved = self._consistent_taxonomy(source, window)
            if resolved is not None and resolved.channel is not None:
                resolutions[source] = resolved
        return tuple(resolutions.values())

    def _eligible_packs(self) -> tuple[SourcePack, ...]:
        return tuple(
            pack
            for pack in self._packs
            if pack.role is SourcePackRole.FACT
            and pack.readiness is PackReadiness.READY
            and pack.media is not None
            and pack.media.literal_value is not None
            and pack.contract_id in self._contracts
            # A publication-gated writer (DSP reach) is checked per request, never as media
            # coverage, so a refused reach delivery can't hide the DSP scope (plan D6).
            and self._contracts[pack.contract_id].contributes_to_coverage
            # A periodic shopper-aggregate pack measures whole weeks and months, never the
            # daily media coverage this catalog reports (ADR-100).
            and pack.time.time_class is not FactTimeClass.PERIODIC
        )

    def _marketplaces_for_all_packs(
        self,
        taxonomy: MediaTaxonomyResolution,
        window: DateRange,
    ) -> tuple[MarketplaceKey, ...]:
        contributing = tuple(
            pack
            for pack in self._eligible_packs()
            if pack.media is not None
            and pack.media.literal_value is not None
            and SourceIdentifier(pack.media.namespace, pack.media.literal_value) == taxonomy.source
        )
        targets = {mapping.target for mapping in self._commerce_bridge.marketplace_mappings}
        return tuple(
            marketplace
            for marketplace in sorted(targets, key=lambda item: item.value)
            if all(self._mapping_versions(pack, marketplace, window) for pack in contributing)
        )

    def _evidence_for(
        self,
        taxonomy: MediaTaxonomyResolution,
        marketplace: MarketplaceKey,
        window: DateRange,
    ) -> tuple[_CatalogEvidence, ...] | None:
        evidence: list[_CatalogEvidence] = []
        for pack in self._eligible_packs():
            media = pack.media
            if media is None or media.literal_value is None:
                continue
            if SourceIdentifier(media.namespace, media.literal_value) != taxonomy.source:
                continue
            mapping_versions = self._mapping_versions(pack, marketplace, window)
            if not mapping_versions:
                return None
            publications = self._current_publications(pack, marketplace, window)
            if publications is None:
                return None
            evidence.append(
                _CatalogEvidence(
                    pack=pack,
                    contract=self._contracts[pack.contract_id],
                    taxonomy=taxonomy,
                    marketplace=marketplace,
                    mapping_versions=mapping_versions,
                    publication_ids=tuple(
                        sorted(item.publication_id.value for item in publications)
                    ),
                )
            )
        return tuple(evidence) or None

    def _consistent_taxonomy(
        self, source: SourceIdentifier, window: DateRange
    ) -> MediaTaxonomyResolution | None:
        resolved: MediaTaxonomyResolution | None = None
        for day in _days(window):
            try:
                current = self._taxonomy.resolve(source, day)
            except ValueError:
                return None
            if resolved is None:
                resolved = current
            elif (
                current.platform != resolved.platform
                or current.channel != resolved.channel
                or current.mapping_version != resolved.mapping_version
                or current.evidence != resolved.evidence
            ):
                return None
        return resolved

    def _mapping_versions(
        self, pack: SourcePack, marketplace: MarketplaceKey, window: DateRange
    ) -> tuple[tuple[str, int], ...]:
        selected: set[tuple[str, int]] = set()
        for day in _days(window):
            matches = tuple(
                mapping
                for mapping in self._commerce_bridge.marketplace_mappings
                if mapping.source.namespace == pack.marketplace.namespace
                and mapping.target == marketplace
                and mapping.applies(mapping.source, on=day)
            )
            if not matches:
                return ()
            selected.update(
                (f"{mapping.source.namespace}:{mapping.source.value}", mapping.mapping_version)
                for mapping in matches
            )
        return tuple(sorted(selected))

    def _current_publications(
        self, pack: SourcePack, marketplace: MarketplaceKey, window: DateRange
    ) -> tuple[SuccessfulPublication, ...] | None:
        contract = self._contracts[pack.contract_id]
        scopes = publication_scopes_for(
            PublicationScopeRequest(
                pack=pack,
                contract=contract,
                bridge=self._commerce_bridge,
                filters={"marketplace_key": marketplace.value},
            )
        )
        required = tuple(
            partition for scope in scopes for partition in _partitions(pack, scope, window)
        )
        available = {
            item.partition: item
            for scope in scopes
            for item in self._current.current_for_scope(scope)
            if item.evidence.publishable
        }
        publications = tuple(available.get(partition) for partition in required)
        if any(item is None for item in publications):
            return None
        return tuple(item for item in publications if item is not None)

    def _candidate(
        self, scope: MediaScope, evidence: tuple[_CatalogEvidence, ...]
    ) -> MediaScopeCatalogCandidate:
        platform = self._taxonomy.term_for(scope.platform).name
        channel = self._taxonomy.term_for(scope.channel).name
        marketplace = next(
            (
                item.value
                for item in self._commerce_bridge.marketplace_display_names
                if item.marketplace == scope.marketplace
            ),
            None,
        )
        if marketplace is None:
            raise ValueError("media scope marketplace lacks a governed display name.")
        material = {
            "tuple": {
                "commerce_scope": scope.commerce_scope.value,
                "platform": scope.platform.value,
                "channel": scope.channel.value,
                "marketplace": scope.marketplace.value,
            },
            "taxonomy": {
                "version": self._taxonomy.version,
                "mappings": sorted(
                    {
                        (
                            f"{item.taxonomy.source.namespace}:{item.taxonomy.source.value}",
                            item.taxonomy.mapping_version,
                            item.taxonomy.evidence,
                        )
                        for item in evidence
                    }
                ),
            },
            "evidence": [
                {
                    "contract": item.contract.contract_id,
                    "contract_version": item.contract.contract_version,
                    "pack_registry_version": item.pack.registry_version,
                    "marketplace_mappings": item.mapping_versions,
                    "publications": item.publication_ids,
                }
                for item in sorted(evidence, key=lambda item: item.pack.contract_id)
            ],
        }
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return MediaScopeCatalogCandidate(
            scope=scope,
            safe_signature=SafeMediaScopeDisplaySignature(
                f"{platform} · {channel} · {marketplace}"
            ),
            catalog_identity=f"media-catalog-v1:{digest}",
        )


def _days(window: DateRange) -> tuple[date, ...]:
    return tuple(window.start + timedelta(days=offset) for offset in range(window.days))


def _partitions(
    pack: SourcePack, scope: PublicationScope, window: DateRange
) -> tuple[PartitionIdentity, ...]:
    windows = (
        tuple(DateRange(day, day) for day in _days(window))
        if pack.time.grain is TimeGrain.DAY
        else (window,)
    )
    return tuple(
        PartitionIdentity(
            scope,
            MeasurementBucket(
                window=selected,
                grain=pack.time.grain,
                calendar=scope.business_calendar,
                source_timezone=scope.source_timezone,
                observation_basis=scope.observation_basis,
            ),
        )
        for selected in windows
    )


def _coalesce_days(days: tuple[date, ...]) -> tuple[DateRange, ...]:
    """Return canonical disjoint intervals without filling uncovered days."""
    if not days:
        return ()
    intervals: list[DateRange] = []
    start = previous = days[0]
    for current in days[1:]:
        if current != previous + timedelta(days=1):
            intervals.append(DateRange(start, previous))
            start = current
        previous = current
    intervals.append(DateRange(start, previous))
    return tuple(intervals)
