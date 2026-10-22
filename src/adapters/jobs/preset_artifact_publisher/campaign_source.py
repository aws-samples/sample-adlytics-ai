# SPDX-License-Identifier: MIT-0
"""Retained-truth compilation of Campaign preset bodies and their custody manifests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, cast

from adapters.campaign_artifact_renderer import (
    CampaignArtifactRenderer,
    CampaignArtifactRenderInput,
    CampaignPortfolioRenderResult,
    retained_campaign_selections,
)
from adapters.campaign_measurement import (
    CampaignArtifactMeasurementService,
    CampaignMeasurementDependencies,
    CampaignMeasurementSelection,
    CampaignPortfolioMeasurementInput,
)
from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.inbound.tool_handlers.measurement import MeasurementTooling
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.insight_precompute.planning import campaign_coverage_requirements
from adapters.jobs.presentation_validation.loader import load_page_schemas, load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher.isolated_refusals import capped_preset_artifact_json
from adapters.outbound.catalog import PageWindowCatalog
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.entity_catalog import CanonicalEntityCatalog, EntityCatalogTooling
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import (
    PerPackPresentationEvidenceInspection,
    PublicationSelector,
)
from adapters.outbound.s3.semantic_storage import S3CanonicalAdapter
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.spend_roster import (
    BakePinnedNames,
    SpendRosterReader,
    ad_product_labels,
)
from domain.presentation.analytical_pages import (
    PresentationDimensionLabel,
    campaign_platform_labels,
    presentation_dimension_labels,
)
from domain.presentation.campaign_attribution import (
    CampaignSelectionCapability,
    SafeCampaignDisplaySignature,
)
from domain.presentation.campaign_custody import (
    CampaignCustodyManifest,
    CampaignCustodyRow,
    campaign_custody_manifest_payload,
)
from domain.presentation.campaign_portfolio import (
    ALL_CHANNELS_FILTER_ID,
    CampaignPortfolioArtifact,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
    campaign_portfolio_artifact_payload,
    campaign_portfolio_segment,
    campaign_portfolio_segment_payload,
)
from domain.presentation.campaigns import EffectiveNameStatus
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactCustodyHeader,
    PresetArtifactFilterIdentity,
    PresetArtifactPage,
    PresetArtifactPointerTemplate,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingIdentity,
    PresetArtifactSiblingStream,
    PresetArtifactVariant,
    SourceUnchanged,
)
from domain.presentation.temporal import (
    TemporalComparisonStatus,
    TemporalResolutionAvailable,
    TemporalResolutionRequest,
    TemporalSelection,
    TemporalSelectionKind,
    resolve_temporal,
)
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EntityContext,
    EntityKind,
)
from domain.semantics.identity import CampaignKey, ChannelKey, MarketplaceKey, PlatformKey
from domain.shared_kernel.values import DateRange
from ports.outbound.entity_catalog import (
    MAX_CAMPAIGN_PORTFOLIO_CANDIDATES,
    AnalyticalEntityCatalogPort,
    EntityCatalogBrowseRequest,
)
from ports.outbound.preset_artifacts import PresetArtifactPointerStorePort
from ports.outbound.semantic_state import scope_horizon_policy
from ports.outbound.source_coverage import (
    SourceCoveragePort,
    SourceCoverageRequest,
    SourceCoverageRequirement,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from adapters.config import PresetArtifactBakeSettings
    from adapters.outbound.bake_sources import BakeSourceReads
    from domain.presentation.page_schemas import PresentationTemporalPolicy
    from domain.presentation.preset_artifacts import PresetArtifactSiblingPair
    from domain.semantics.commerce_identity import CommerceIdentityBridge
    from ports.outbound.query import PeriodSeriesQueryPort
    from ports.outbound.semantic_state import CurrentPublicationInspectionPort

__all__ = [
    "CAMPAIGN_PRESET_POPULATION_BOUND",
    "CampaignPortfolioCompiler",
    "RetainedCampaignPresetArtifactSource",
    "campaign_portfolio_bake_plan_count",
    "representative_windows",
    "retained_campaign_preset_artifact_source",
]

# The declared measurable population for one bake — the catalog port's own campaign browse
# bound. Grouped measurement scans do not grow with it; it caps only the in-memory row
# reduction before top-N display selection. Beyond it the bake refuses rather than silently
# measure a partial population (rank, don't refuse, applies up to this bound; past it is the
# deferred per-campaign decomposition slice).
CAMPAIGN_PRESET_POPULATION_BOUND = MAX_CAMPAIGN_PORTFOLIO_CANDIDATES

# Superseded revisions stay readable for in-flight serves, then expire from history.
SUPERSEDED_HISTORY_RETENTION = timedelta(days=30)

# The campaign renderer's output generation for identical sources (ADR-070, 2026-09-16
# render-format amendment). Bump whenever rendering changes bytes for the same publications:
# format 2 projects governed display labels into row and custody signatures; format 3 carries
# the ADR-071 pagination record and estate roster in place of the truncation record; format 5
# carries the currency `display_value` beside every monetary value (ADR-084); format 6 serves
# a ratio over a zero denominator as typed absent (null), never 0 (ADR-087); format 7 carries
# the server-declared `change` block on every comparison (ADR-121); format 8 carries the
# window's `evidence_status` coverage block (ADR-119); format 9 moves card next steps to
# `NextStep.v2` (ADR-122), so every revision changes and every cached insight slot re-fills
# before a v2 decoder reads it. The body bytes are format 8's. Format 10 moves with Retail
# health format 8, whose three new tiles changed the compared-metric roster (ADR-125); the
# body bytes are format 9's. Format 11 bakes the channel filter siblings under one pointer, each
# with its own segments keyed by filter, and the multi-channel By channel and Delivery per
# channel content (ADR-127). Format 12 writes every segment and filter sibling through the
# streamed stage and finds each through the parent custody's sharded sibling index (ADR-145):
# the parent and filter custody carry only their own rows; the body bytes are format 11's.
CAMPAIGN_PRESET_RENDER_FORMAT = 12


_COUNTER_INSTANT = datetime(2026, 3, 2, tzinfo=UTC)
"""The fixed stamp a certification counter's requests carry: no counter reads a wall clock."""


def _counter_clock() -> datetime:
    return _COUNTER_INSTANT


class CampaignPortfolioCompiler(Protocol):
    """Measure and reduce one full authorized cohort into a portfolio artifact."""

    def render_portfolio(
        self, request: CampaignArtifactRenderInput
    ) -> CampaignPortfolioRenderResult:
        """Return the reduced portfolio and the exact publication identities consumed."""
        ...


@dataclass(frozen=True, slots=True)
class _CampaignFilterPublication:
    """One channel filter's canonical first segment, its custody rows and its population."""

    filter_id: str
    payload_json: str
    custody_rows: tuple[CampaignCustodyRow, ...]
    population: CampaignPortfolioArtifact
    """The filter's whole ordered estate, from which its segments render as the stage draws them."""

    @property
    def segment_count(self) -> int:
        """Every ordering's segments, less the canonical first one (the filter's own parent)."""
        pagination = self.population.pagination
        return 0 if pagination is None else _ORDERING_COUNT * pagination.segment_count - 1


_ORDERING_COUNT = 2 * len(CampaignPortfolioSortField)
"""Each sort field ascending and descending: the orderings a filter's segments are baked in."""


@dataclass(frozen=True, slots=True)
class _CampaignSiblingPairs:
    """The Campaigns sibling stream (ADR-145): every filter's segments, then each filter sibling.

    Each segment renders, with its custody of only its rows, as the stage draws it, so a bake
    holds the pairs in flight rather than every body. A filter sibling's custody carries only its
    own rows: every sibling is found through the parent custody's index.
    """

    page: PresetArtifactPage
    variant: PresetArtifactVariant
    revision: PresetArtifactRevision
    filters: tuple[_CampaignFilterPublication, ...]
    by_signature: dict[SafeCampaignDisplaySignature, CampaignMeasurementSelection]

    def __call__(self) -> Iterator[PresetArtifactSiblingPair]:
        for publication in self.filters:
            yield from self._segments(publication.population)
        for publication in self.filters[1:]:
            identity = PresetArtifactFilterIdentity(publication.filter_id)
            yield self._pair(identity, publication.payload_json, publication.custody_rows)

    def stream(self) -> PresetArtifactSiblingStream:
        """Return the stream with its declared count."""
        count = sum(item.segment_count for item in self.filters) + len(self.filters) - 1
        return PresetArtifactSiblingStream(self, count)

    def _segments(
        self, population: CampaignPortfolioArtifact
    ) -> Iterator[PresetArtifactSiblingPair]:
        """Render every noncanonical ordering segment and each canonical tail segment."""
        if population.pagination is None:
            return
        for sort_field in CampaignPortfolioSortField:
            for direction in ("asc", "desc"):
                sort = CampaignPortfolioSort(sort_field, direction)
                for segment_index in range(population.pagination.segment_count):
                    if (
                        sort_field is CampaignPortfolioSortField.SPEND
                        and direction == "desc"
                        and segment_index == 0
                    ):
                        continue
                    artifact = campaign_portfolio_segment(population, sort, segment_index)
                    if artifact is None:
                        raise ValueError("declared campaign segment is unavailable.")
                    rows = _custody_rows(artifact, self.by_signature)
                    if rows is None:
                        raise ValueError("campaign segment custody is unavailable.")
                    yield self._pair(
                        PresetArtifactSiblingIdentity(
                            f"{sort_field.value}-{direction}", segment_index, population.filter_id
                        ),
                        capped_preset_artifact_json(campaign_portfolio_segment_payload(artifact)),
                        rows,
                    )

    def _pair(
        self,
        identity: PresetArtifactSiblingIdentity | PresetArtifactFilterIdentity,
        payload_json: str,
        rows: tuple[CampaignCustodyRow, ...],
    ) -> PresetArtifactSiblingPair:
        body = PresetArtifactSiblingBody(
            self.page, self.variant, self.revision, identity, payload_json
        )
        custody = PresetArtifactSiblingCustody(
            self.page,
            self.variant,
            self.revision,
            identity,
            capped_preset_artifact_json(
                campaign_custody_manifest_payload(CampaignCustodyManifest(body.digest, rows))
            ),
        )
        return body, custody


@dataclass(frozen=True, slots=True)
class RetainedCampaignPresetArtifactSource:
    """Compile one declared Campaign preset variant from retained truth only.

    The source measures the full authorized population grouped, lets the domain order the
    estate with its pagination record and display roster (ADR-071), derives the
    artifact revision from the exact publication identities used, and binds a private custody
    manifest carrying the canonical selectors the public payload deliberately omits.
    """

    coverage: SourceCoveragePort
    coverage_requirements: tuple[SourceCoverageRequirement, ...]
    temporal_policy: PresentationTemporalPolicy
    catalog: AnalyticalEntityCatalogPort
    scope: AuthorizedAnalyticalScope
    compiler: CampaignPortfolioCompiler
    pointers: PresetArtifactPointerStorePort
    clock: Callable[[], datetime]
    labels: tuple[PresentationDimensionLabel, ...]
    page: PresetArtifactPage = field(default_factory=lambda: PresetArtifactPage("campaigns"))
    channel_order: tuple[str, ...] = ()
    """Governed channel keys in taxonomy order, the order channel filters are offered in."""

    def __post_init__(self) -> None:
        if not self.coverage_requirements:
            raise ValueError("campaign preset baking requires declared source coverage.")

    def publication_for(  # noqa: PLR0911
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return a complete retained-materializer publication or no currently bakeable artifact.

        `SourceUnchanged` when the computed revision is the one the current pointer already
        carries (S6 §2.8, D26), so a bake can tell an unchanged page from an unavailable one.
        """
        windows = self._resolved_windows(variant)
        if windows is None:
            return None
        window, prior = windows
        selections = self._authorized_selections(window)
        if not selections:
            return None
        rendered = self.compiler.render_portfolio(
            CampaignArtifactRenderInput(
                selections=selections,
                period=window,
                prior_period=prior,
                sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
                capabilities=_baked_placeholder_capabilities(len(selections)),
                channel_order=self.channel_order,
            )
        )
        if rendered.portfolio is None:
            return None
        revision = PresetArtifactRevision.from_publication_identities(
            rendered.publication_identities,
            render_format=CAMPAIGN_PRESET_RENDER_FORMAT,
        )
        current = self.pointers.current(self.page, variant)
        if current is not None and current.revision == revision:
            return SourceUnchanged(revision)
        by_signature = {selection.signature: selection for selection in selections}
        if len(by_signature) != len(selections):
            return None
        # Every channel filter is a render-only sibling of this one batch (ADR-127 decision 2):
        # All channels is the pointer's body; each other filter is a sibling the index binds.
        filters: list[_CampaignFilterPublication] = []
        for artifact in (rendered.portfolio, *rendered.siblings):
            publication = self._filter_publication(artifact, by_signature)
            if publication is None:
                return None
            filters.append(publication)
        everything = filters[0]
        if everything.filter_id != ALL_CHANNELS_FILTER_ID:
            return None
        body = PresetArtifactBody(self.page, variant, revision, everything.payload_json)
        # The parent custody's header is All channels' own rows; the stage adds the sibling
        # index, through which every segment and filter sibling is found (ADR-145).
        header = PresetArtifactCustodyHeader(
            capped_preset_artifact_json(
                campaign_custody_manifest_payload(
                    CampaignCustodyManifest(body.digest, everything.custody_rows)
                )
            )
        )
        return PresetArtifactPublicationRequest(
            body,
            PresetArtifactPointerTemplate(
                self.page,
                variant,
                revision,
                body.digest,
                1 if current is None else current.forward_token + 1,
                None if current is None else current.revision,
                None if current is None else self.clock() + SUPERSEDED_HISTORY_RETENTION,
                resolved_window=window,
                comparison_window=prior,
            ),
            header,
            _CampaignSiblingPairs(
                self.page, variant, revision, tuple(filters), by_signature
            ).stream(),
        )

    @staticmethod
    def _filter_publication(
        population: CampaignPortfolioArtifact,
        by_signature: dict[SafeCampaignDisplaySignature, CampaignMeasurementSelection],
    ) -> _CampaignFilterPublication | None:
        """Bake one filter's canonical first segment and its custody rows; keep its estate."""
        canonical_sort = CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc")
        parent_artifact = campaign_portfolio_segment(population, canonical_sort, 0)
        if parent_artifact is None:
            return None
        custody_rows = _custody_rows(parent_artifact, by_signature)
        if custody_rows is None:
            return None
        return _CampaignFilterPublication(
            population.filter_id,
            capped_preset_artifact_json(campaign_portfolio_artifact_payload(parent_artifact)),
            custody_rows,
            population,
        )

    def _resolved_windows(
        self, variant: PresetArtifactVariant
    ) -> tuple[DateRange, DateRange] | None:
        """Resolve the declared preset window and its comparator without clocks or gap filling."""
        preset_id = next(
            (
                candidate
                for candidate in self.temporal_policy.candidate_presets
                if candidate.value == variant.value
            ),
            None,
        )
        if preset_id is None:
            return None
        source_coverage = self.coverage.source_coverage(
            SourceCoverageRequest(self.coverage_requirements)
        )
        resolved = resolve_temporal(
            TemporalResolutionRequest(
                policy=self.temporal_policy,
                coverage_intervals=source_coverage.bounds.intervals,
                selection=TemporalSelection(
                    kind=TemporalSelectionKind.PRESET,
                    preset_id=preset_id,
                ),
                comparison_requested=False,
            )
        )
        if not isinstance(resolved, TemporalResolutionAvailable):
            return None
        control = resolved.control
        if control.comparison.status is not TemporalComparisonStatus.AVAILABLE:
            return None
        window = control.resolved.window
        prior = DateRange(
            window.start - timedelta(days=window.days),
            window.start - timedelta(days=1),
        )
        return window, prior

    def _authorized_selections(self, window: DateRange) -> tuple[CampaignMeasurementSelection, ...]:
        """Browse the full authorized population; refuse only beyond the declared bound."""
        resolved = self.catalog.browse(
            EntityCatalogBrowseRequest(
                scope=self.scope,
                kinds=frozenset({EntityKind.CAMPAIGN}),
                window=window,
                current_on=None,
                context=EntityContext(),
                max_candidates=CAMPAIGN_PRESET_POPULATION_BOUND,
            )
        )
        if resolved.truncated:
            return ()
        try:
            return retained_campaign_selections(tuple(resolved.aliases), window, self.labels)
        except TypeError, ValueError:
            return ()


def _custody_rows(
    portfolio: CampaignPortfolioArtifact,
    by_signature: dict[SafeCampaignDisplaySignature, CampaignMeasurementSelection],
) -> tuple[CampaignCustodyRow, ...] | None:
    """Bind one public segment's row order to its canonical selectors."""
    rows: list[CampaignCustodyRow] = []
    for row in portfolio.rows:
        selection = by_signature.get(row.signature)
        if selection is None:
            return None
        rows.append(
            CampaignCustodyRow(
                selection.campaign,
                selection.channel,
                selection.marketplace,
                row.signature,
            )
        )
    return tuple(rows)


def _baked_placeholder_capabilities(count: int) -> tuple[CampaignSelectionCapability, ...]:
    """Produce unique, non-redeemable, canonical-free placeholders the serve path replaces.

    The public baked payload must never carry a canonical selector, so the placeholder is a
    positional marker rather than the retained ``retained:{campaign}`` form.
    """
    expiry = datetime.max.replace(tzinfo=UTC)
    return tuple(
        CampaignSelectionCapability(f"baked:{index:05d}", expiry) for index in range(count)
    )


# The portfolio cohort is grouped, but its daily trend is chunked by days under the query row
# ceiling, so the plan count grows with the window: each roster variant is counted at its own
# window and equal prior, ending on this fixed day (plan 4.2: 26/26/30/34 for 1/7/14/30 days).
_REPRESENTATIVE_END = date(2026, 3, 1)


def representative_windows(days: int) -> tuple[DateRange, DateRange]:
    """Return a fixed `days`-long window and its equal prior, for counting one variant's plans."""
    if days < 1:
        raise ValueError("a representative window needs at least one day.")
    start = _REPRESENTATIVE_END - timedelta(days=days - 1)
    prior_end = start - timedelta(days=1)
    return (
        DateRange(start, _REPRESENTATIVE_END),
        DateRange(prior_end - timedelta(days=days - 1), prior_end),
    )


def campaign_portfolio_bake_plan_count(root: Path, days: int) -> int:
    """Count the real portfolio bake batch of one `days` window at the campaign estate bound.

    The build-time bakeability check budgets the exact grouped and window-chunked batch
    ``CampaignArtifactRenderer.render_portfolio`` executes at the largest accepted population.
    """
    period, prior = representative_windows(days)
    bridge = load_commerce_identity_bridge(root)
    service = CampaignArtifactMeasurementService(
        CampaignMeasurementDependencies(
            scope=AuthorizedAnalyticalScope(frozenset({bridge.commerce_scope})),
            catalog=load_catalog(root),
            contracts=tuple(contract for _, contract in load_contracts(root)),
            clock=_counter_clock,
        )
    )
    batch = service.portfolio_batch(
        CampaignPortfolioMeasurementInput(
            selections=_representative_selections(bridge),
            period=period,
            prior_period=prior,
            sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        )
    )
    return len(batch.prepared.plans)


def _representative_selections(
    bridge: CommerceIdentityBridge,
) -> tuple[CampaignMeasurementSelection, ...]:
    """Build a fictional cohort at the displayed-row bound; never a live identifier."""
    marketplace = MarketplaceKey(
        min(mapping.target.value for mapping in bridge.marketplace_mappings)
    )
    platform = PlatformKey("amazon_ads")
    channel = ChannelKey("amazon_dsp")
    return tuple(
        CampaignMeasurementSelection(
            campaign=CampaignKey(
                f"campaign:bakeability-{index:03d}",
                commerce_scope=bridge.commerce_scope,
                platform=platform,
            ),
            channel=channel,
            marketplace=marketplace,
            signature=SafeCampaignDisplaySignature(
                effective_name=None,
                channel=channel.value,
                marketplace=marketplace.value,
                platform=platform.value,
                effective_name_status=EffectiveNameStatus.MISSING,
            ),
        )
        for index in range(CAMPAIGN_PRESET_POPULATION_BOUND)
    )


def retained_campaign_preset_artifact_source(  # noqa: PLR0913, PLR0917 -- composition inputs
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort,
    current: BakeSourceReads,
) -> RetainedCampaignPresetArtifactSource | None:
    """Compose the retained campaign source, or none while its configuration is absent."""
    if not all(
        (
            settings.semantic_publications_table,
            settings.semantic_v2_bucket,
            settings.athena_workgroup,
            settings.semantic_v2_current_database,
            settings.athena_result_reuse_max_age_minutes > 0,
            settings.athena_query_batch_max_concurrency > 0,
        )
    ):
        return None
    root = contracts_root()
    contracts = tuple(contract for _, contract in load_contracts(root))
    source_packs = load_source_packs(root)
    bridge = load_commerce_identity_bridge(root)
    taxonomy = load_media_taxonomy(root)
    scope = AuthorizedAnalyticalScope(frozenset({bridge.commerce_scope}))
    reader = cast("CurrentPublicationInspectionPort", current)
    catalog = CanonicalEntityCatalog(
        EntityCatalogTooling(
            current=reader,
            canonical=S3CanonicalAdapter(settings.semantic_v2_bucket),
            packs=source_packs,
            contracts=contracts,
            bridge=bridge,
            taxonomy=taxonomy,
        )
    )
    metric_catalog = load_catalog(root)
    campaign_schema = next(
        (schema for schema in load_v2_page_schemas(root).pages if schema.page_id == "campaigns"),
        None,
    )
    if campaign_schema is None:
        raise RuntimeError("campaign presentation schema is unavailable for preset baking.")
    horizons = scope_horizon_policy(current)
    selector = PublicationSelector(reader, source_packs, bridge, horizons=horizons)
    compiler = CampaignArtifactRenderer(
        measurements=CampaignArtifactMeasurementService(
            CampaignMeasurementDependencies(
                scope=scope,
                catalog=metric_catalog,
                contracts=contracts,
                clock=current.clock,
                # A roster-graded figure reads its rostered keys as in chat (plan D4).
                rosters=SpendRosterReader(
                    catalog=metric_catalog,
                    contracts=contracts,
                    publications=selector,
                    queries=queries,
                    labels=ad_product_labels(taxonomy),
                    clock=current.clock,
                    # A campaign roster names each campaign as chat does, never by its key.
                    names=BakePinnedNames(DimensionLabelResolver(catalog, scope)),
                ),
            )
        ),
        # Bake-time evidence snapshots are per source pack (ADR-070 point 5); the
        # request-serving path keeps the strict single-transaction selector.
        evidence=PerPackPresentationEvidenceInspection(reader, source_packs, bridge),
        tooling=MeasurementTooling(
            queries=queries,
            publications=selector,
            query_batch_max_concurrency=settings.athena_query_batch_max_concurrency,
        ),
    )
    return RetainedCampaignPresetArtifactSource(
        coverage=PageWindowCatalog(reader, source_packs, contracts, bridge, horizons),
        coverage_requirements=campaign_coverage_requirements(
            load_page_schemas(root), metric_catalog
        ),
        temporal_policy=campaign_schema.temporal_policy,
        catalog=catalog,
        scope=scope,
        compiler=compiler,
        pointers=pointers,
        clock=current.clock,
        labels=(
            *presentation_dimension_labels(taxonomy, bridge),
            *campaign_platform_labels(taxonomy),
        ),
        channel_order=tuple(
            term.key.value for term in taxonomy.terms if isinstance(term.key, ChannelKey)
        ),
    )
