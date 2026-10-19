# SPDX-License-Identifier: MIT-0
"""Coverage adapter for active Semantic V2 CURRENT publications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING

from adapters.outbound.publication_selection import (
    PublicationScopeRequest,
    coverage_by_population,
    publication_scopes_for,
)
from domain.catalog.capability import Catalog, Metric
from domain.catalog.freshness import Coverage, common_intervals, latest_complete_window
from domain.semantics.commerce_identity import CommerceIdentityBridge
from domain.semantics.source_pack import PackReadiness, SourcePack, SourcePackRole
from domain.shared_kernel.provenance import FreshnessStatus
from domain.shared_kernel.values import DateRange
from ports.outbound.semantic_state import CurrentPublicationPort, ScopeHorizonPolicy
from ports.outbound.source_coverage import (
    SourceCoverageBounds,
    SourceCoverageContributor,
    SourceCoverageRequest,
    SourceCoverageRequirement,
    SourceCoverageResult,
    metric_coverage_requirement,
)

if TYPE_CHECKING:  # pragma: no cover — kept out of the Lambda import path
    from collections.abc import Callable, Sequence

    from domain.data_contracts.contract import DataContract
    from domain.semantics.publication import PublicationScope, SuccessfulPublication

__all__ = [
    "MetricWindowDefaults",
    "PageDefaultWindowCompleteness",
    "PageDefaultWindowRequest",
    "PageDefaultWindowResult",
    "PageWindowCatalog",
    "PageWindowRequirement",
    "PublicationCatalog",
]


@dataclass(frozen=True, slots=True)
class _PublicationContributor:
    contract: DataContract
    intervals: tuple[DateRange, ...]
    loaded_at: datetime | None


PageWindowRequirement = SourceCoverageRequirement
"""Backward-compatible name for the presentation source-coverage requirement."""


class PageDefaultWindowCompleteness(StrEnum):
    """Whether default resolution met the page's governed preferred duration."""

    PREFERRED = "preferred"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class PageDefaultWindowRequest:
    """Exact page requirements and its governed preferred duration."""

    requirements: tuple[PageWindowRequirement, ...]
    preferred_duration_days: int

    def __post_init__(self) -> None:
        if not self.requirements:
            raise ValueError("page default window needs at least one requirement.")
        if isinstance(self.preferred_duration_days, bool) or self.preferred_duration_days < 1:
            raise ValueError("page preferred duration must be a positive integer.")


@dataclass(frozen=True, slots=True)
class PageDefaultWindowResult:
    """One exact common interval and whether it met the preferred duration."""

    window: DateRange
    completeness: PageDefaultWindowCompleteness


@dataclass(frozen=True, slots=True)
class PageWindowCatalog:
    """Resolve the latest complete page window from exact CURRENT source scopes."""

    current: CurrentPublicationPort
    packs: tuple[SourcePack, ...]
    contracts: tuple[DataContract, ...]
    bridge: CommerceIdentityBridge
    horizons: ScopeHorizonPolicy | None = None
    """A bake's declared source horizon (A5.1 I9); None reads each scope whole, as today."""

    def _scope_reader(
        self, pack: SourcePack
    ) -> Callable[[PublicationScope], tuple[SuccessfulPublication, ...]]:
        """One pack's whole-scope reader, bounded by the bake's horizon when one is declared."""
        horizon = (
            None
            if self.horizons is None
            else self.horizons.window_horizon(pack.time.longest_bucket_days)
        )
        if horizon is None:
            return self.current.current_for_scope
        return partial(self.current.current_for_scope, horizon=horizon)

    def source_coverage(self, request: SourceCoverageRequest) -> SourceCoverageResult:
        """Read exact CURRENT contributor intervals without choosing a presentation window."""
        contracts = {contract.contract_id: contract for contract in self.contracts}
        contributors: list[SourceCoverageContributor] = []
        missing_required_scope = False
        for requirement in request.requirements:
            filters = dict(requirement.selectors)
            for source_system, source_dataset in requirement.sources:
                matched = tuple(
                    pack
                    for pack in self.packs
                    if pack.role is SourcePackRole.FACT
                    and pack.readiness is PackReadiness.READY
                    and (contract := contracts.get(pack.contract_id)) is not None
                    and contract.contributes_to_coverage
                    and contract.source_system == source_system
                    and contract.source_dataset == source_dataset
                    # Only the writers of the facts the metric reads decide its coverage.
                    and (not requirement.facts or contract.target_table in requirement.facts)
                )
                if not matched:
                    missing_required_scope = True
                    continue
                scopes_found = False
                for pack in matched:
                    contract = contracts[pack.contract_id]
                    for covered in coverage_by_population(
                        publication_scopes_for(
                            PublicationScopeRequest(pack, contract, self.bridge, filters)
                        ),
                        self._scope_reader(pack),
                    ):
                        publications = covered.publications
                        scopes_found = True
                        contributors.append(
                            SourceCoverageContributor(
                                requirement=requirement,
                                source_system=source_system,
                                source_dataset=source_dataset,
                                contributor_id=covered.scope.identity,
                                intervals=covered.intervals,
                                loaded_at=(
                                    None
                                    if not publications
                                    else max(item.published_at for item in publications)
                                ),
                            )
                        )
                if not scopes_found:
                    missing_required_scope = True
        ordered = tuple(
            sorted(
                contributors,
                key=lambda item: (
                    item.requirement.sources,
                    item.requirement.selectors,
                    item.requirement.facts,
                    item.source_system,
                    item.source_dataset,
                    item.contributor_id,
                ),
            )
        )
        intervals = (
            ()
            if missing_required_scope
            else common_intervals(tuple(item.intervals for item in ordered))
        )
        bounds = SourceCoverageBounds(
            intervals=intervals,
            earliest_start=None if not intervals else intervals[0].start,
            latest_end=None if not intervals else intervals[-1].end,
        )
        loaded = tuple(item.loaded_at for item in ordered if item.loaded_at is not None)
        return SourceCoverageResult(
            contributors=ordered,
            bounds=bounds,
            latest_loaded_at=max(loaded) if loaded else None,
        )

    def resolve_default(
        self,
        request: PageDefaultWindowRequest,
    ) -> PageDefaultWindowResult | None:
        """Project exact source coverage onto the legacy default-window contract."""
        coverage = self.source_coverage(SourceCoverageRequest(request.requirements))
        intervals = coverage.bounds.intervals
        preferred = latest_complete_window((intervals,), request.preferred_duration_days)
        if preferred is not None:
            return PageDefaultWindowResult(
                preferred,
                PageDefaultWindowCompleteness.PREFERRED,
            )
        if not intervals:
            return None
        return PageDefaultWindowResult(
            max(intervals, key=lambda item: (item.end, item.start)),
            PageDefaultWindowCompleteness.PARTIAL,
        )


@dataclass(frozen=True, slots=True)
class MetricWindowDefaults:
    """Resolve the latest published window for one metric, for governed window defaulting.

    A question that names no reporting window is answered for the newest coverage the metric's
    own sources have published, at most `preferred_days` long, rather than refused. The choice
    is recorded as a governed default upstream, which is what forces the released answer to
    disclose it (ADR-062).
    """

    window_catalog: PageWindowCatalog
    catalog: Catalog
    preferred_days: int = 7

    def default_window(self, metric: Metric) -> DateRange | None:
        """Return the newest fully covered window, or None when nothing is published."""
        # A metric with no source or no fact has no coverage: no default, as before the scoping.
        if not self.catalog.sources_for(metric) or not self.catalog.facts_read(metric):
            return None
        coverage = self.window_catalog.source_coverage(
            SourceCoverageRequest((metric_coverage_requirement(self.catalog, metric),))
        )
        intervals = coverage.bounds.intervals
        if not intervals:
            return None
        preferred = latest_complete_window((intervals,), self.preferred_days)
        if preferred is not None:
            return preferred
        latest = max(intervals, key=lambda item: (item.end, item.start))
        start = max(latest.start, latest.end - timedelta(days=self.preferred_days - 1))
        return DateRange(start, latest.end)


@dataclass(frozen=True, slots=True)
class PublicationCatalog:
    """Exact capability coverage from declared Semantic V2 CURRENT publication scopes."""

    current: CurrentPublicationPort
    packs: tuple[SourcePack, ...]
    contracts: tuple[DataContract, ...]
    bridge: CommerceIdentityBridge

    def coverage(self, now: datetime) -> tuple[Coverage, ...]:
        """Read every declared fact scope and intersect exact contributor intervals."""
        if now.tzinfo is None:
            raise ValueError("capability coverage time must be timezone-aware.")
        contracts = {contract.contract_id: contract for contract in self.contracts}
        per_fact: dict[str, list[_PublicationContributor]] = {}
        for pack in self.packs:
            contract = contracts.get(pack.contract_id)
            if (
                contract is None
                or not contract.contributes_to_coverage
                or pack.role is not SourcePackRole.FACT
                or pack.readiness is not PackReadiness.READY
            ):
                continue
            for covered in coverage_by_population(
                publication_scopes_for(
                    PublicationScopeRequest(
                        pack=pack,
                        contract=contract,
                        bridge=self.bridge,
                        filters={},
                    )
                ),
                self.current.current_for_scope,
            ):
                publications = covered.publications
                per_fact.setdefault(pack.dataset.value, []).append(
                    _PublicationContributor(
                        contract=contract,
                        intervals=covered.intervals,
                        loaded_at=(
                            None
                            if not publications
                            else max(item.published_at for item in publications)
                        ),
                    )
                )
        return tuple(
            self._coverage_of(fact, contributors, now)
            for fact, contributors in sorted(per_fact.items())
        )

    def coverage_for(self, fact_name: str, now: datetime) -> Coverage | None:
        """Give one declared V2 fact's exact CURRENT coverage."""
        return next(
            (item for item in self.coverage(now) if item.fact_name == fact_name),
            None,
        )

    @staticmethod
    def _coverage_of(
        fact: str,
        contributors: Sequence[_PublicationContributor],
        now: datetime,
    ) -> Coverage:
        first = contributors[0]
        missing = any(not item.intervals for item in contributors)
        intervals = (
            () if missing else common_intervals(tuple(item.intervals for item in contributors))
        )
        loaded = tuple(item.loaded_at for item in contributors if item.loaded_at is not None)
        stale = any(
            item.loaded_at is not None
            and now - item.loaded_at > timedelta(hours=item.contract.staleness_threshold_hours)
            for item in contributors
        )
        freshness = (
            FreshnessStatus.NEVER_LOADED
            if missing
            else FreshnessStatus.STALE
            if stale
            else FreshnessStatus.FRESH
        )
        grains = tuple(
            grain
            for grain in first.contract.grain
            if all(grain in item.contract.grain for item in contributors)
        )
        return Coverage(
            fact_name=fact,
            window=intervals[0] if len(intervals) == 1 else None,
            freshness=freshness,
            measurement_basis=first.contract.measurement_basis,
            valid_grains=grains or first.contract.grain,
            loaded_at=min(loaded) if loaded else None,
            intervals=intervals,
        )
