# SPDX-License-Identifier: MIT-0
"""Retained-truth compilation of capability-free Retail Health preset bodies."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING, Protocol, cast

from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.inbound.tool_handlers.measurement import (
    MeasurementExecutionTooling,
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    execute_presentation_batch,
    prepare_batch,
)
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher.campaign_source import (
    SUPERSEDED_HISTORY_RETENTION,
    representative_windows,
)
from adapters.jobs.preset_artifact_publisher.isolated_refusals import capped_preset_artifact_json
from adapters.outbound.catalog import PageWindowCatalog
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.entity_catalog import LATEST_ONLY
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import (
    PerPackPresentationEvidenceInspection,
    PublicationScopeRequest,
    PublicationSelector,
    publication_scopes_for,
)
from adapters.outbound.s3.semantic_storage import S3CanonicalAdapter
from adapters.outbound.source_pack import load_source_packs
from domain.catalog.freshness import FreshnessStatus
from domain.data_contracts.generated_rows import (
    VendorProductHierarchyRow,
    VendorSalesReportedTotalRow,
)
from domain.measurement.lenses import LENS_DEFAULTS
from domain.measurement.planning import MEASURE_ALIAS
from domain.measurement.ranking import (
    RANKED_INVARIANT,
    RankedProjection,
    RankedRestriction,
    RankedValue,
)
from domain.narrative.shaped_response import UnitKind, Units
from domain.operations.bake_refusals import IsolatedRefusal, IsolatedRefusalReason, RefusalScope
from domain.presentation.change import ChangeSemantics
from domain.presentation.compared_metrics import presentation_change_semantics, presentation_labels
from domain.presentation.currency_display import CurrencyTextStyle, currency_display_text
from domain.presentation.insights import (
    DeterministicFinding,
    DeterministicFindingRequest,
    DeterministicMoversSlotRequest,
    InsightAbsenceClass,
    InsightFindingKind,
    InsightFindingOperand,
    InsightOutputClass,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    calculate_deterministic_findings,
    deterministic_movers_text,
    nonzero_movers,
    unavailable_insight_slots,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactCustodyHeader,
    PresetArtifactPage,
    PresetArtifactPointerTemplate,
    PresetArtifactProductChildIdentity,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingStream,
    PresetArtifactVariant,
    SourceUnchanged,
)
from domain.presentation.retail_health import (
    RETAIL_HEALTH_INSIGHT_IDS,
    RETAIL_HEALTH_MAX_ROWS,
    RETAIL_HEALTH_METRIC_UNITS,
    RETAIL_HEALTH_ROW_GRAIN,
    RETAIL_HEALTH_ROW_METRICS,
    RETAIL_HEALTH_TOTAL_GRAIN,
    RETAIL_HEALTH_TOTAL_METRICS,
    RETAIL_HEALTH_TREND_GRAIN,
    RETAIL_HEALTH_TREND_METRICS,
    RetailComparison,
    RetailDistributorView,
    RetailHealthArtifact,
    RetailHealthOutcome,
    RetailMetricValue,
    RetailProductRow,
    RetailReconciliation,
    RetailTrendPoint,
    retail_comparison,
    retail_health_artifact_payload,
    retail_health_evidence_status,
)
from domain.presentation.retail_health_custody import (
    RetailHealthCustodyManifest,
    RetailHealthCustodyProduct,
    RetailProductChildCustody,
    product_ref_for,
    retail_health_custody_manifest_payload,
    retail_product_child_custody_payload,
)
from domain.presentation.retail_product_detail import (
    RETAIL_PRODUCT_AD_METRICS,
    RETAIL_PRODUCT_INSIGHT_PROVENANCE,
    RETAIL_PRODUCT_SERIES_GRAIN,
    RetailAdAttributed,
    RetailAdComparison,
    RetailAdValue,
    RetailDetailReason,
    RetailProductDetailArtifact,
    RetailProductDetailOutcome,
    RetailProductInsight,
    RetailSalesPoint,
    RetailTrafficPoint,
    ad_values_by_channel,
    product_insight_stats,
    retail_ad_comparison,
    retail_product_detail_payload,
)
from domain.presentation.retail_product_detail import (
    RetailProductIdentity as ProductDetailIdentity,
)
from domain.presentation.starter_questions import window_label
from domain.presentation.temporal import (
    TemporalComparisonStatus,
    TemporalResolutionAvailable,
    TemporalResolutionRequest,
    TemporalSelection,
    TemporalSelectionKind,
    resolve_temporal,
)
from domain.semantics.identity import ChannelKey, CommerceScopeKey, MarketplaceKey, ProductKey
from domain.semantics.media_taxonomy import MediaTaxonomy
from domain.semantics.source_pack import PackReadiness
from domain.shared_kernel.errors import InvariantError
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange, Grain
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionPlan,
    PresentationEvidenceInspectionRequest,
    PresentationEvidenceInspectionResult,
)
from ports.outbound.query import BAKE_LONGEST_WINDOW_DAYS, BAKE_MAX_ROWS, RowCeiling
from ports.outbound.semantic_state import ScopeHorizon, scope_horizon_policy
from ports.outbound.source_coverage import (
    SourceCoverageRequest,
    SourceCoverageRequirement,
    metric_coverage_requirement,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from adapters.config import PresetArtifactBakeSettings
    from adapters.inbound.tool_handlers.measurement import (
        PreparedMeasurementBatch,
        PresentationMeasurementAnswerSlot,
    )
    from adapters.outbound.bake_sources import BakeSourceReads
    from domain.catalog.capability import Catalog
    from domain.data_contracts.contract import DataContract
    from domain.presentation.page_schemas import (
        PresentationPageSchemaSet,
        PresentationTemporalPolicy,
    )
    from domain.semantics.commerce_identity import CommerceIdentityBridge
    from domain.semantics.source_pack import SourcePack
    from ports.outbound.canonical import CanonicalDatasetReadPort
    from ports.outbound.preset_artifacts import PresetArtifactPointerStorePort
    from ports.outbound.publication_selection import PresentationEvidenceInspectionPort
    from ports.outbound.query import PeriodSeriesQueryPort
    from ports.outbound.semantic_state import (
        CurrentPublicationInspectionPort,
        CurrentPublicationPort,
    )
    from ports.outbound.source_coverage import SourceCoveragePort

__all__ = [
    "RETAIL_HEALTH_BOUNDARY",
    "CanonicalRetailProductHierarchy",
    "CanonicalRetailReportedTotals",
    "RetailHealthArtifactRenderer",
    "RetailHealthCompiler",
    "RetailHealthMeasurementBatch",
    "RetailHealthMeasurementBatchRequest",
    "RetailHealthRenderInput",
    "RetailHealthRenderResult",
    "RetailProductHierarchyResult",
    "RetailProductHierarchySource",
    "RetailProductIdentity",
    "RetailReportedTotals",
    "RetailReportedTotalsSource",
    "RetainedRetailHealthPresetArtifactSource",
    "retail_health_bake_plan_count",
    "retail_health_coverage_requirements",
    "retail_health_measurement_batch",
    "retained_retail_health_preset_artifact_source",
]

# The retail renderer's output generation for identical sources (ADR-070, 2026-09-16
# render-format amendment). Bump whenever rendering changes bytes for the same publications.
# 2 — derived returns-and-margin compare slots and per-product return-rate/margin columns
#     (RetailHealthArtifact V2, design revision 2026-09-17.2).
# 3 — insight slots carry the declared absence pair (ADR-068 amendment 2026-09-23): the
#     bake-rendered retail-movers slot states its silence condition when quiet, and the
#     prompted placeholders declare generation-pending. Campaign formats are unchanged
#     because the campaign serve path rebuilds every insight slot live.
# 4 — comparison values carry the currency `display_value`, and the baked retail-movers text
#     writes two-place amounts with their currency code (ADR-084).
# 5 — every comparison carries the server-declared `change` block (ADR-121).
# 6 — a complete body carries the window's `evidence_status` coverage block (ADR-119).
# 7 — card next steps are `NextStep.v2` (ADR-122): the revision changes so every cached
#     insight slot re-fills before a v2 decoder reads it. The body bytes are format 6's.
# 8 — four tile groups shared with product detail (ordered units, average selling price and
#     revenue per glance view), the return-reasons and paid-feature statuses, a private
#     custody manifest and one baked `RetailProductDetail` child per served row (ADR-125).
# 9 — moves with Campaigns format 11, whose Delivery per channel adds the viewable rate to the
#     compared-metric roster (ADR-127); the body bytes are format 8's.
# 10 — the children's digests move from the parent custody's inline child set to the
#     stage-built sibling index, and the manifest kind is versioned (ADR-145); the body bytes
#     are format 9's.
RETAIL_HEALTH_PRESET_RENDER_FORMAT = 10

RETAIL_HEALTH_BOUNDARY = (
    "Retail actuals only. No campaign identifier, ROAS, or advertising "
    "attribution appears on this page."
)

_SPARK_GRAIN = Grain(("date", "product", "marketplace"))
_PROMPTED_INSIGHT_IDS = tuple(
    slot_id for slot_id in RETAIL_HEALTH_INSIGHT_IDS if slot_id is not InsightSlotId.RETAIL_MOVERS
)
_GROUPS = {
    "total": ("marketplace_key",),
    "row": ("amazon_asin", "marketplace_key"),
    "ranked": ("amazon_asin", "marketplace_key"),
    "spark": ("date_key", "amazon_asin", "marketplace_key"),
    "trend": ("date_key", "marketplace_key"),
}
_RANKED_SECTIONS = frozenset({"ranked", "spark"})
"""The reads whose bound is semantic (the top 50, ADR-143): over it is a defect, not a refusal."""
_SERIES_MAX_ROWS = RETAIL_HEALTH_MAX_ROWS * BAKE_LONGEST_WINDOW_DAYS
"""A ranked series read's bound: the 50 shown times the longest window's days (derived)."""
_SHIPPED_REVENUE = "retail_revenue_shipped"
_ASIN_NAMESPACE_PREFIX = "amazon_asin:"

# What the baker MEASURES is narrower than what the artifact CARRIES: the four derived
# returns-and-margin slots (return_rate, returns_value_estimate_retail, fulfilment_rate,
# amazon_margin_proxy) are reduced from these measured operands at render time — the same
# publish-time reconstruction the campaign baker uses for portfolio ROAS — so one authority
# derives all four rather than the engine computing some and this module the others.
# units_ordered is measured only as the fulfilment-rate denominator and carries no tile.
_MEASURED_TOTAL_METRICS = (
    "retail_revenue_shipped",
    "units_shipped",
    "customer_returns",
    "glance_views",
    "revenue_ordered",
    "cogs_shipped",
    "conversion_rate_retail",
    "units_ordered",
)
# Each child carries every tile at product grain (ADR-125 decision 4): the ordered operands are
# measured per product too, and every product ratio is reduced from its operands here.
_MEASURED_ROW_METRICS = (
    *RETAIL_HEALTH_TREND_METRICS,
    "cogs_shipped",
    "revenue_ordered",
    "units_ordered",
)
_DERIVED_TOTAL_METRICS = (
    "return_rate",
    "returns_value_estimate_retail",
    "fulfilment_rate",
    "amazon_margin_proxy",
    "asp_shipped",
    "revenue_per_glance_view",
)
_GLANCE_VIEWS = "glance_views"
_MEASURED_UNIT_KINDS: dict[str, UnitKind] = {
    **RETAIL_HEALTH_METRIC_UNITS,
    "units_ordered": UnitKind.COUNT,
}
_RATIO_PRECISION = Decimal("0.0001")
"""The governed presentation precision for derived quotients, matching portfolio ROAS."""

_SlotLabel = tuple[str, str, str, DateRange]
_ParsedAnswer = tuple[dict[tuple[str, ...], Decimal | int], Units, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class RetailProductIdentity:
    """The safe display identity dim_product declares for one child ASIN."""

    child_asin: str
    parent_asin: str
    title: str


@dataclass(frozen=True, slots=True)
class RetailProductHierarchyResult:
    """Effective product identities keyed by canonical ASIN, with their evidence."""

    by_asin: Mapping[str, RetailProductIdentity]
    publication_identities: tuple[str, ...]


class RetailProductHierarchySource(Protocol):
    """Resolve dim_product display identities effective for one reporting window."""

    def hierarchy(self, window: DateRange) -> RetailProductHierarchyResult | None:
        """Return every effective product identity, or none while evidence is absent."""
        ...


@dataclass(frozen=True, slots=True)
class RetailReportedTotals:
    """Window sums of the vendor-reported control totals under the serving lens."""

    revenue_shipped: Decimal
    units_shipped: Decimal
    publication_identities: tuple[str, ...]


class RetailReportedTotalsSource(Protocol):
    """Read the *_reported_total control figures for one exact reporting window."""

    def reported_totals(self, window: DateRange) -> RetailReportedTotals | None:
        """Return complete window control totals, or none while coverage is partial."""
        ...


@dataclass(frozen=True, slots=True)
class RetailHealthRenderInput:
    """The exact resolved window pair one Retail Health bake covers."""

    period: DateRange
    prior_period: DateRange


@dataclass(frozen=True, slots=True)
class RetailProductChild:
    """One displayed row's canonical product and its baked detail body."""

    product: ProductKey
    artifact: RetailProductDetailArtifact

    @property
    def product_ref(self) -> str:
        """The server-only reference that keys the child and the parent custody."""
        return product_ref_for(self.product)


@dataclass(frozen=True, slots=True)
class RetailHealthRenderResult:
    """A reduced Retail Health artifact and the exact publication identities used."""

    artifact: RetailHealthArtifact | None
    publication_identities: tuple[str, ...]
    children: tuple[RetailProductChild, ...] = ()
    """One baked product-detail child per displayed row, in row order (ADR-125 decision 2)."""
    movers_product_ref: str | None = None
    """The product the Largest movers card names first, for its ``open_product_detail``."""

    def __post_init__(self) -> None:
        if self.artifact is not None and not self.publication_identities:
            raise ValueError("a rendered Retail Health artifact must name its publications.")
        if self.children and (
            self.artifact is None or len(self.children) != len(self.artifact.rows)
        ):
            raise ValueError("a Retail Health bake serves one child per displayed row.")


class RetailHealthCompiler(Protocol):
    """Measure and reduce one full retail window into a Retail Health artifact."""

    def render_retail_health(self, request: RetailHealthRenderInput) -> RetailHealthRenderResult:
        """Return the reduced artifact and the exact publication identities consumed."""
        ...


@dataclass(frozen=True, slots=True)
class _ReducedRows:
    """Displayed rows plus the population figures the bounded table cannot carry."""

    displayed: tuple[RetailProductRow, ...]
    asins: tuple[str, ...]
    """The canonical ASIN of each displayed row, in row order (server-only)."""
    movers_asins: tuple[str, ...]
    """The canonical ASIN of each ranked mover, in the movers' ranking order."""
    total_count: int
    truncation: int | None
    product_revenue: Decimal
    product_units: Decimal
    movers: tuple[DeterministicFinding, ...]


@dataclass(frozen=True, slots=True)
class RetailHealthArtifactRenderer:
    """Execute and reduce governed retail evidence without page/session custody."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    evidence: PresentationEvidenceInspectionPort
    tooling: MeasurementTooling
    hierarchy: RetailProductHierarchySource
    reported: RetailReportedTotalsSource
    clock: Callable[[], datetime]
    # The retail-movers contract's governed silence condition: the bake-rendered
    # deterministic slot is served as-is, so its quiet state must name this reason.
    movers_silence_reason: str
    commerce_scope: CommerceScopeKey
    """The workspace's canonical commerce scope, which qualifies each child's product key."""
    channel_labels: Mapping[str, str]
    """Governed channel display names in taxonomy order, for ad-attributed rows."""

    def render_retail_health(  # noqa: PLR0911
        self, request: RetailHealthRenderInput
    ) -> RetailHealthRenderResult:
        """Measure totals, rows, trend, and reconciliation from retained truth only."""
        period, prior = request.period, request.prior_period
        prepared, slots = self._batch(period, prior)
        inspection = self.evidence.inspect(
            PresentationEvidenceInspectionRequest(
                plans=tuple(
                    PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                    for plan in prepared.plans
                ),
                contracts=prepared.contracts,
                now=prepared.now,
            )
        )
        # Plan 4.3 (DB21): every CURRENT read happens before the first query, so the V3 bake's
        # fan-out pins all of them and the render's own capture pass is its pure bind step.
        # The decisions below still read these results where they always did.
        hierarchy = self.hierarchy.hierarchy(period)
        reported = self.reported.reported_totals(period)
        ad_inspected = self._ad_inspection(period, prior)
        executed = execute_presentation_batch(
            prepared,
            inspection,
            MeasurementExecutionTooling(
                queries=self.tooling.queries,
                query_batch_max_concurrency=self.tooling.query_batch_max_concurrency,
            ),
        )
        measured = tuple(
            sorted(
                {
                    publication.publication_id.value
                    for slot in inspection.slots
                    if slot.bound is not None
                    for publication in slot.bound.all_publications
                }
            )
        )
        if not measured:
            return RetailHealthRenderResult(None, ())
        if all(
            _covered_empty(answer, label[3])
            for answer, label in zip(executed.slots, slots, strict=True)
        ):
            revision = PresetArtifactRevision.from_publication_identities(
                measured, render_format=RETAIL_HEALTH_PRESET_RENDER_FORMAT
            )
            return RetailHealthRenderResult(_empty_artifact(period, prior, revision), measured)
        answers: dict[tuple[str, str, str], _ParsedAnswer] = {}
        for answer, (section, metric, role, expected) in zip(executed.slots, slots, strict=True):
            if section in _RANKED_SECTIONS:
                _require_within_bound(answer)
            parsed = _answer_rows(answer, expected, _GROUPS[section], _MEASURED_UNIT_KINDS[metric])
            if parsed is None:
                return RetailHealthRenderResult(None, ())
            answers[(section, metric, role)] = parsed
        marketplaces = {key[-1] for rows, _, _ in answers.values() for key in rows}
        if len(marketplaces) != 1:
            return RetailHealthRenderResult(None, ())
        marketplace = next(iter(marketplaces))
        if hierarchy is None or reported is None:
            return RetailHealthRenderResult(None, ())
        ads = self._ad_evidence(ad_inspected, period, marketplace)
        identities = tuple(
            sorted(
                {
                    *measured,
                    *hierarchy.publication_identities,
                    *reported.publication_identities,
                    *ads.publications,
                }
            )
        )
        revision = PresetArtifactRevision.from_publication_identities(
            identities, render_format=RETAIL_HEALTH_PRESET_RENDER_FORMAT
        )
        context = _ReductionContext(
            answers, marketplace, period, prior, presentation_change_semantics(self.catalog)
        )
        totals = _totals(context)
        reduced = _rows(context, hierarchy)
        trend = _trend(context)
        if totals is None or reduced is None or trend is None:
            return RetailHealthRenderResult(None, ())
        matches = (
            reduced.product_revenue == reported.revenue_shipped
            and reduced.product_units == reported.units_shipped
        )
        reconciliation = (
            RetailReconciliation("reconciled", period.end)
            if matches
            else RetailReconciliation(
                "discrepancy",
                period.end,
                reduced.product_revenue,
                reported.revenue_shipped,
                reduced.product_units,
                reported.units_shipped,
            )
        )
        # Bake-time placeholders only: the serve path assembles the prompted slots live
        # and replaces these, so their truthful at-rest class is generation-pending.
        insights = (
            *unavailable_insight_slots(
                _PROMPTED_INSIGHT_IDS,
                artifact_revision=revision.value,
                absence_class=InsightAbsenceClass.GENERATION_PENDING,
            ),
            _retail_movers_slot(
                DeterministicMoversSlotRequest(
                    artifact_revision=revision.value,
                    findings=reduced.movers,
                    silence_reason=self.movers_silence_reason,
                    currency_code=_shipped_revenue_currency(totals),
                )
            ),
        )
        try:
            artifact = RetailHealthArtifact(
                outcome=RetailHealthOutcome.COMPLETE,
                period=period,
                prior_period=prior,
                distributor_view=RetailDistributorView(LENS_DEFAULTS["distributor_view"]),
                totals=totals,
                absences=(),
                insights=insights,
                trend=trend,
                rows=reduced.displayed,
                total_count=reduced.total_count,
                truncation=reduced.truncation,
                reconciliation=reconciliation,
                boundary=RETAIL_HEALTH_BOUNDARY,
                # Every answer above covers its window end to end (holes are refused), so
                # each tile's coverage is complete (ADR-119).
                evidence_status=retail_health_evidence_status(
                    period,
                    tuple(item.metric for item in totals),
                    presentation_labels(self.catalog),
                ),
            )
        except ValueError:
            return RetailHealthRenderResult(None, ())
        try:
            children = tuple(
                _product_child(
                    _ChildInput(
                        context=context,
                        artifact=artifact,
                        asin=asin,
                        row=row,
                        product=ProductKey(asin, self.commerce_scope, MarketplaceKey(marketplace)),
                        reduced=reduced,
                        ads=ads,
                        labels=presentation_labels(self.catalog),
                        channel_labels=self.channel_labels,
                    )
                )
                for asin, row in zip(reduced.asins, reduced.displayed, strict=True)
            )
        except ValueError:
            return RetailHealthRenderResult(None, ())
        movers_ref = _movers_product_ref(artifact, reduced, children)
        return RetailHealthRenderResult(artifact, identities, children, movers_ref)

    def _ad_inspection(
        self, period: DateRange, prior: DateRange
    ) -> tuple[RetailHealthMeasurementBatch, PresentationEvidenceInspectionResult] | None:
        """Prepare and inspect the product ad batch, or None when it cannot be (no query)."""
        try:
            batch = retail_product_ad_batch(
                RetailHealthMeasurementBatchRequest(
                    catalog=self.catalog,
                    contracts=self.contracts,
                    period=period,
                    prior_period=prior,
                    computed_at=self.clock(),
                )
            )
            inspection = self.evidence.inspect(
                PresentationEvidenceInspectionRequest(
                    plans=tuple(
                        PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                        for plan in batch.prepared.plans
                    ),
                    contracts=batch.prepared.contracts,
                    now=batch.prepared.now,
                )
            )
        except RuntimeError, ValueError:
            return None
        return batch, inspection

    def _ad_evidence(
        self,
        inspected: tuple[RetailHealthMeasurementBatch, PresentationEvidenceInspectionResult] | None,
        period: DateRange,
        marketplace: str,
    ) -> _AdEvidence:
        """Measure ad-attributed purchases and revenue per product and channel, or say why not.

        One Amazon Marketing Cloud query per preset (ADR-125 consequences). It never blocks the
        retail bake: an incomplete answer becomes each child's typed absence.
        """
        window = window_label(period)
        if inspected is None:
            return _ad_missing(window)
        batch, inspection = inspected
        try:
            executed = execute_presentation_batch(
                batch.prepared,
                inspection,
                MeasurementExecutionTooling(
                    queries=self.tooling.queries,
                    query_batch_max_concurrency=self.tooling.query_batch_max_concurrency,
                ),
            )
        except RuntimeError, ValueError:
            return _ad_missing(window)
        publications = tuple(
            sorted(
                {
                    publication.publication_id.value
                    for slot in inspection.slots
                    if slot.bound is not None
                    for publication in slot.bound.all_publications
                }
            )
        )
        answers: dict[tuple[str, str], _ParsedAnswer] = {}
        for answer, (_, metric, role, expected) in zip(executed.slots, batch.slots, strict=True):
            parsed = _answer_rows(
                answer,
                expected,
                _AD_GROUP,
                UnitKind.COUNT if metric == "purchases" else UnitKind.CURRENCY,
                basis=MeasurementBasis.AD_ATTRIBUTED,
            )
            if parsed is None:
                return _ad_missing(window)
            answers[(metric, role)] = parsed
        if any(key[-1] != marketplace for rows, _, _ in answers.values() for key in rows):
            return _ad_missing(window)
        return _AdEvidence(answers, publications, window)

    def _batch(
        self, period: DateRange, prior: DateRange
    ) -> tuple[PreparedMeasurementBatch, tuple[_SlotLabel, ...]]:
        """Prepare the fixed governed Retail Health cohort in stable slot order."""
        batch = retail_health_measurement_batch(
            RetailHealthMeasurementBatchRequest(
                catalog=self.catalog,
                contracts=self.contracts,
                period=period,
                prior_period=prior,
                computed_at=self.clock(),
            )
        )
        return batch.prepared, batch.slots


@dataclass(frozen=True, slots=True)
class RetailHealthMeasurementBatchRequest:
    """Inputs the fixed governed Retail Health measurement cohort is prepared from."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    period: DateRange
    prior_period: DateRange
    computed_at: datetime


@dataclass(frozen=True, slots=True)
class RetailHealthMeasurementBatch:
    """The prepared Retail Health cohort plus stable slot labels for reduction."""

    prepared: PreparedMeasurementBatch
    slots: tuple[_SlotLabel, ...]


def retail_health_measurement_batch(
    request: RetailHealthMeasurementBatchRequest,
) -> RetailHealthMeasurementBatch:
    """Build the one real Retail Health bake batch in stable slot order.

    The renderer executes exactly this batch and the build-time bakeability check
    counts exactly its plans, so the budget model can never drift from the baker.
    """
    period, prior = request.period, request.prior_period
    measurements: list[MeasurementToolRequest] = []
    slots: list[_SlotLabel] = []
    for metric in _MEASURED_TOTAL_METRICS:
        measurements.append(_compare_request(request, metric, ("marketplace",)))
        slots.extend((("total", metric, "current", period), ("total", metric, "prior", prior)))
    for metric in _MEASURED_ROW_METRICS:
        measurements.append(_compare_request(request, metric, ("product", "marketplace")))
        slots.extend((("row", metric, "current", period), ("row", metric, "prior", prior)))
    # ADR-143: the top 50 are ranked inside the query, over the compares' own population, and
    # the product-detail series are read for those 50 only: one read per (metric, window role),
    # so the rows returned and the statements run are constant in the catalog.
    measurements.append(
        _daily_request(
            request,
            _SHIPPED_REVENUE,
            ("product", "marketplace"),
            period,
            max_rows=RETAIL_HEALTH_MAX_ROWS,
            ranked=retail_ranked_restriction(period, prior, RankedProjection.RANKED_KEYS),
        )
    )
    slots.append(("ranked", _SHIPPED_REVENUE, "current", period))
    for metric in (_SHIPPED_REVENUE, _GLANCE_VIEWS):
        for role, window in (("current", period), ("prior", prior)):
            measurements.append(
                _daily_request(
                    request,
                    metric,
                    ("date", "product", "marketplace"),
                    window,
                    max_rows=_SERIES_MAX_ROWS,
                    ranked=retail_ranked_restriction(period, prior, RankedProjection.SEMI_JOIN),
                )
            )
            slots.append(("spark", metric, role, window))
    for metric in RETAIL_HEALTH_TREND_METRICS:
        measurements.append(_daily_request(request, metric, ("date", "marketplace"), period))
        slots.append(("trend", metric, "current", period))
        measurements.append(_daily_request(request, metric, ("date", "marketplace"), prior))
        slots.append(("trend", metric, "prior", prior))
    try:
        prepared = prepare_batch(tuple(measurements))
    except (KeyError, ValueError) as error:
        raise RuntimeError("Retail Health requires a declared metric cohort.") from error
    return RetailHealthMeasurementBatch(prepared, tuple(slots))


def _compare_request(
    request: RetailHealthMeasurementBatchRequest,
    metric: str,
    grains: tuple[str, ...],
) -> MeasurementToolRequest:
    return MeasurementToolRequest(
        tool=MeasurementTool.COMPARE_WINDOWS,
        catalog=request.catalog,
        contracts=request.contracts,
        metric_name=metric,
        grains=grains,
        window=request.period,
        prior_window=request.prior_period,
        computed_at=request.computed_at,
        correlation_id="retail-health-artifact",
        filters={},
        max_rows=BAKE_MAX_ROWS,
        row_ceiling=RowCeiling.BAKE,
    )


def retail_ranked_restriction(
    period: DateRange, prior: DateRange, projection: RankedProjection
) -> RankedRestriction:
    """Retail's one rank rule: current shipped revenue over the trend metrics' population.

    The population declaration (`RETAIL_HEALTH_TREND_METRICS` over current and prior) is the one
    `_rows` reads its stated total from, so the ranked set and the total can't drift apart.
    """
    return RankedRestriction(
        key="product",
        rank_metric=_SHIPPED_REVENUE,
        rank_window=period,
        population_metrics=RETAIL_HEALTH_TREND_METRICS,
        population_windows=(period, prior),
        n=RETAIL_HEALTH_MAX_ROWS,
        projection=projection,
    )


def _daily_request(  # noqa: PLR0913, PLR0917
    request: RetailHealthMeasurementBatchRequest,
    metric: str,
    grains: tuple[str, ...],
    period: DateRange,
    *,
    max_rows: int = BAKE_MAX_ROWS,
    ranked: RankedRestriction | None = None,
) -> MeasurementToolRequest:
    return MeasurementToolRequest(
        tool=MeasurementTool.METRIC_BY_GRAIN,
        catalog=request.catalog,
        contracts=request.contracts,
        metric_name=metric,
        grains=grains,
        window=period,
        computed_at=request.computed_at,
        correlation_id="retail-health-artifact",
        filters={},
        max_rows=max_rows,
        row_ceiling=RowCeiling.BAKE,
        ranked=ranked,
    )


_COUNTER_INSTANT = datetime(2026, 3, 2, tzinfo=UTC)
"""The fixed stamp a certification counter's requests carry: no counter reads a wall clock."""


def _counter_clock() -> datetime:
    return _COUNTER_INSTANT


def retail_health_bake_plan_count(root: Path, days: int) -> int:
    """Count the real Retail Health bake plans: compares, trends, the ranked keys, four series."""
    period, prior = representative_windows(days)
    request = RetailHealthMeasurementBatchRequest(
        catalog=load_catalog(root),
        contracts=tuple(contract for _, contract in load_contracts(root)),
        period=period,
        prior_period=prior,
        computed_at=_counter_clock(),
    )
    return len(retail_health_measurement_batch(request).prepared.plans) + len(
        retail_product_ad_batch(request).prepared.plans
    )


@dataclass(frozen=True, slots=True)
class _ReductionContext:
    """Parsed slot answers plus the single-marketplace window pair they cover."""

    answers: dict[tuple[str, str, str], _ParsedAnswer]
    marketplace: str
    period: DateRange
    prior_period: DateRange
    semantics: ChangeSemantics


def _quotient(numerator: Decimal | int, denominator: Decimal | int) -> Decimal:
    """One governed presentation quotient; a zero denominator yields the page's zero.

    The zero matches the engine's own behavior on this page — a ratio answer with no rows
    reduces through ``rows.get(key, 0)`` — so a derived rate and a measured rate degrade
    the same way.
    """
    if not denominator:
        return Decimal(0)
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        _RATIO_PRECISION, rounding=ROUND_HALF_EVEN
    )


_OPERANDS = (
    "retail_revenue_shipped",
    "units_shipped",
    "customer_returns",
    "glance_views",
    "cogs_shipped",
    "units_ordered",
)


def _derived_operand_totals(
    context: _ReductionContext, role: str
) -> dict[str, Decimal | int] | None:
    """Collect one role's measured operand totals for the derived compare slots."""
    key = (context.marketplace,)
    values: dict[str, Decimal | int] = {}
    for metric in _OPERANDS:
        rows, _units, _ = context.answers[("total", metric, role)]
        if len(rows) > 1:
            return None
        values[metric] = rows.get(key, 0)
    return values


def _derived_total_value(metric: str, operands: Mapping[str, Decimal | int]) -> Decimal:  # noqa: PLR0911
    """Reconstruct one derived figure from its measured operands (totals and products alike)."""
    if metric == "return_rate":
        return _quotient(operands["customer_returns"], operands["units_shipped"])
    if metric == "fulfilment_rate":
        return _quotient(operands["units_shipped"], operands["units_ordered"])
    if metric == "amazon_margin_proxy":
        return Decimal(operands["retail_revenue_shipped"]) - Decimal(operands["cogs_shipped"])
    if metric == "asp_shipped":
        return _quotient(operands["retail_revenue_shipped"], operands["units_shipped"])
    if metric == "revenue_per_glance_view":
        return _quotient(operands["retail_revenue_shipped"], operands["glance_views"])
    if metric == "conversion_rate_retail":
        return _quotient(operands["units_ordered"], operands["glance_views"])
    if metric == "returns_value_estimate_retail":
        asp = _quotient(operands["retail_revenue_shipped"], operands["units_shipped"])
        return (Decimal(operands["customer_returns"]) * asp).quantize(
            _RATIO_PRECISION, rounding=ROUND_HALF_EVEN
        )
    raise ValueError(f"undeclared derived retail metric {metric!r}.")


def _derived_units(metric: str, currency_units: Units) -> Units:
    kind = RETAIL_HEALTH_METRIC_UNITS[metric]
    return currency_units if kind is UnitKind.CURRENCY else Units(kind)


@dataclass(frozen=True, slots=True)
class _ComparisonSides:
    """One figure's two window values with the semantics they share."""

    metric: str
    current: Decimal | int
    prior: Decimal | int
    units: Units
    grain: Grain


def _compared(context: _ReductionContext, sides: _ComparisonSides) -> RetailComparison:
    signed = sides.metric == "amazon_margin_proxy"
    return retail_comparison(
        sides.metric,
        RetailMetricValue(sides.current, sides.units, context.period, sides.grain, signed=signed),
        RetailMetricValue(
            sides.prior, sides.units, context.prior_period, sides.grain, signed=signed
        ),
        context.semantics,
    )


def _totals(context: _ReductionContext) -> tuple[RetailComparison, ...] | None:
    """Reduce the declared total roster, or none when any answer is incoherent.

    The measured display slots come straight from their answers; the derived slots are
    reconstructed from measured operand totals, so every figure on the page is
    server-computed against one custody.
    """
    by_metric: dict[str, RetailComparison] = {}
    key = (context.marketplace,)
    currency_units: Units | None = None
    for metric in _MEASURED_TOTAL_METRICS:
        current_rows, current_units, _ = context.answers[("total", metric, "current")]
        prior_rows, prior_units, _ = context.answers[("total", metric, "prior")]
        if current_units != prior_units or len(current_rows) > 1 or len(prior_rows) > 1:
            return None
        if metric == "retail_revenue_shipped":
            currency_units = current_units
        by_metric[metric] = _compared(
            context,
            _ComparisonSides(
                metric,
                current_rows.get(key, 0),
                prior_rows.get(key, 0),
                current_units,
                RETAIL_HEALTH_TOTAL_GRAIN,
            ),
        )
    current_operands = _derived_operand_totals(context, "current")
    prior_operands = _derived_operand_totals(context, "prior")
    if current_operands is None or prior_operands is None or currency_units is None:
        return None
    for metric in _DERIVED_TOTAL_METRICS:
        by_metric[metric] = _compared(
            context,
            _ComparisonSides(
                metric,
                _derived_total_value(metric, current_operands),
                _derived_total_value(metric, prior_operands),
                _derived_units(metric, currency_units),
                RETAIL_HEALTH_TOTAL_GRAIN,
            ),
        )
    return tuple(by_metric[metric] for metric in RETAIL_HEALTH_TOTAL_METRICS)


def _rows(
    context: _ReductionContext,
    hierarchy: RetailProductHierarchyResult,
) -> _ReducedRows | None:
    """Reduce the full grouped product population into bounded display rows."""
    answers = context.answers
    population = sorted(
        {
            key[0]
            for metric in RETAIL_HEALTH_TREND_METRICS
            for role in ("current", "prior")
            for key in answers[("row", metric, role)][0]
        }
    )
    if not population:
        return None
    shown = _ranked_keys(context, frozenset(population))
    spark_rows, spark_units, spark_qualifiers = answers[("spark", _SHIPPED_REVENUE, "current")]
    qualifiers = tuple(
        sorted(
            {
                *spark_qualifiers,
                *(
                    qualifier
                    for metric in _MEASURED_ROW_METRICS
                    for role in ("current", "prior")
                    for qualifier in answers[("row", metric, role)][2]
                ),
            }
        )
    )
    days = tuple(
        context.period.start + timedelta(days=offset) for offset in range(context.period.days)
    )
    rows_by_asin: dict[str, RetailProductRow] = {}
    operands: list[InsightFindingOperand] = []
    asin_by_name: dict[str, str] = {}
    product_revenue = Decimal(0)
    product_units = Decimal(0)
    for asin in population:
        identity = hierarchy.by_asin.get(asin)
        if identity is None:
            return None
        row_key = (asin, context.marketplace)
        comparisons: list[RetailComparison] = []
        measured_row: dict[tuple[str, str], Decimal | int] = {}
        for metric in _MEASURED_ROW_METRICS:
            current_rows, current_units, _ = answers[("row", metric, "current")]
            prior_rows, prior_units, _ = answers[("row", metric, "prior")]
            if current_units != prior_units:
                return None
            current_value = current_rows.get(row_key, 0)
            prior_value = prior_rows.get(row_key, 0)
            measured_row[(metric, "current")] = current_value
            measured_row[(metric, "prior")] = prior_value
            if metric not in RETAIL_HEALTH_ROW_METRICS:
                # Measured as a product-detail operand (COGS, ordered figures); no column.
                continue
            comparisons.append(
                retail_comparison(
                    metric,
                    RetailMetricValue(
                        current_value, current_units, context.period, RETAIL_HEALTH_ROW_GRAIN
                    ),
                    RetailMetricValue(
                        prior_value, current_units, context.prior_period, RETAIL_HEALTH_ROW_GRAIN
                    ),
                    context.semantics,
                )
            )
        currency_units = comparisons[0].current.units
        for metric in ("return_rate", "amazon_margin_proxy"):
            signed = metric == "amazon_margin_proxy"
            units = Units(UnitKind.RATIO) if metric == "return_rate" else currency_units
            values: dict[str, Decimal] = {}
            for role in ("current", "prior"):
                if metric == "return_rate":
                    values[role] = _quotient(
                        measured_row[("customer_returns", role)],
                        measured_row[("units_shipped", role)],
                    )
                else:
                    values[role] = Decimal(
                        measured_row[("retail_revenue_shipped", role)]
                    ) - Decimal(measured_row[("cogs_shipped", role)])
            comparisons.append(
                retail_comparison(
                    metric,
                    RetailMetricValue(
                        values["current"],
                        units,
                        context.period,
                        RETAIL_HEALTH_ROW_GRAIN,
                        signed=signed,
                    ),
                    RetailMetricValue(
                        values["prior"],
                        units,
                        context.prior_period,
                        RETAIL_HEALTH_ROW_GRAIN,
                        signed=signed,
                    ),
                    context.semantics,
                )
            )
        shipped_current = Decimal(comparisons[0].current.value)
        shipped_prior = Decimal(comparisons[0].prior.value)
        product_revenue += shipped_current
        product_units += Decimal(comparisons[1].current.value)
        spark = tuple(
            RetailMetricValue(
                spark_rows.get((day.isoformat(), asin, context.marketplace), 0),
                spark_units,
                DateRange(day, day),
                _SPARK_GRAIN,
            )
            for day in days
        )
        rows_by_asin[asin] = RetailProductRow(
            identity.child_asin,
            identity.parent_asin,
            identity.title,
            tuple(comparisons),
            spark,
            qualifiers,
        )
        asin_by_name[f"{identity.title} ({identity.child_asin})"] = asin
        operands.append(
            InsightFindingOperand(
                name=f"{identity.title} ({identity.child_asin})",
                current=shipped_current,
                prior=shipped_prior,
            )
        )
    # The display order is the rank rule's own (ADR-143); the whole population still feeds the
    # stated total, the product sums and the movers above.
    total_count = len(population)
    displayed = tuple(rows_by_asin[asin] for asin in shown)
    displayed_asins = shown
    movers = nonzero_movers(
        calculate_deterministic_findings(
            DeterministicFindingRequest(
                kind=InsightFindingKind.RANKED_ABSOLUTE_CHANGE,
                operands=tuple(operands),
            )
        )
    )
    return _ReducedRows(
        displayed=displayed,
        asins=displayed_asins,
        movers_asins=tuple(asin_by_name.get(item.name, "") for item in movers),
        total_count=total_count,
        truncation=None if total_count <= RETAIL_HEALTH_MAX_ROWS else RETAIL_HEALTH_MAX_ROWS,
        product_revenue=product_revenue,
        product_units=product_units,
        movers=movers,
    )


def _ranked_invariant(problem: str) -> InvariantError:
    return InvariantError(
        RANKED_INVARIANT,
        "Retail health's ranked reads disagree with its compares' population.",
        context={"problem": problem},
    )


def _require_within_bound(answer: PresentationMeasurementAnswerSlot) -> None:
    """Refuse a ranked read past its semantic bound: a defect, never a `rows_over_ceiling`."""
    shaped = answer.answer
    if shaped is not None and shaped.metadata.truncated:
        raise _ranked_invariant("a ranked read returned more than its bound")


def _ranked_keys(context: _ReductionContext, population: frozenset[str]) -> tuple[str, ...]:
    """Return the ranked keys in display order, after checking them against the population.

    Cheap checks, not a second ranking: the ranked keys number min(50, |P|) and lie in P; each
    one with a current shipped-revenue row ranks at exactly that row's value; every series key is
    a ranked key. A violation means the SQL and the declared population disagree.
    """
    answers, marketplace = context.answers, context.marketplace
    ranked_rows = answers[("ranked", _SHIPPED_REVENUE, "current")][0]
    ranked = RankedRestriction.order(
        RankedValue(key[0], value) for key, value in ranked_rows.items()
    )
    keys = tuple(item.key for item in ranked)
    if len(keys) != min(RETAIL_HEALTH_MAX_ROWS, len(population)):
        raise _ranked_invariant("the ranked keys are not min(50, population)")
    if not set(keys) <= population:
        raise _ranked_invariant("a ranked key is outside the population")
    compared = answers[("row", _SHIPPED_REVENUE, "current")][0]
    for item in ranked:
        value = compared.get((item.key, marketplace))
        if value is not None and Decimal(value) != Decimal(item.rank_value):
            raise _ranked_invariant("a rank value differs from the compare's current value")
    shown = set(keys)
    for metric in (_SHIPPED_REVENUE, _GLANCE_VIEWS):
        for role in ("current", "prior"):
            if any(key[1] not in shown for key in answers[("spark", metric, role)][0]):
                raise _ranked_invariant("a series key is outside the ranked keys")
    return keys


def _trend(context: _ReductionContext) -> tuple[RetailTrendPoint, ...] | None:
    """Densify the aligned daily trend for every chart-switchable metric."""
    points: list[RetailTrendPoint] = []
    for offset in range(context.period.days):
        day = context.period.start + timedelta(days=offset)
        prior_day = context.prior_period.start + timedelta(days=offset)
        current_values: list[RetailMetricValue] = []
        prior_values: list[RetailMetricValue] = []
        for metric in RETAIL_HEALTH_TREND_METRICS:
            current_rows, current_units, _ = context.answers[("trend", metric, "current")]
            prior_rows, prior_units, _ = context.answers[("trend", metric, "prior")]
            current_values.append(
                RetailMetricValue(
                    current_rows.get((day.isoformat(), context.marketplace), 0),
                    current_units,
                    DateRange(day, day),
                    RETAIL_HEALTH_TREND_GRAIN,
                )
            )
            prior_values.append(
                RetailMetricValue(
                    prior_rows.get((prior_day.isoformat(), context.marketplace), 0),
                    prior_units,
                    DateRange(prior_day, prior_day),
                    RETAIL_HEALTH_TREND_GRAIN,
                )
            )
        points.append(RetailTrendPoint(day, tuple(current_values), tuple(prior_values)))
    return tuple(points)


_AD_GROUP = ("amazon_asin", "channel_key", "marketplace_key")


def retail_product_ad_batch(
    request: RetailHealthMeasurementBatchRequest,
) -> RetailHealthMeasurementBatch:
    """Build the one Amazon Marketing Cloud product query a preset bakes (ADR-125)."""
    measurements: list[MeasurementToolRequest] = []
    slots: list[_SlotLabel] = []
    for metric in RETAIL_PRODUCT_AD_METRICS:
        measurements.append(
            _compare_request(request, metric, ("product", "channel", "marketplace"))
        )
        slots.extend(
            (
                ("ad", metric, "current", request.period),
                ("ad", metric, "prior", request.prior_period),
            )
        )
    try:
        prepared = prepare_batch(tuple(measurements))
    except (KeyError, ValueError) as error:
        raise RuntimeError("product detail requires its declared ad-attributed cohort.") from error
    return RetailHealthMeasurementBatch(prepared, tuple(slots))


@dataclass(frozen=True, slots=True)
class _AdEvidence:
    """Ad-attributed answers keyed by (metric, role), or none with the window they miss."""

    answers: Mapping[tuple[str, str], _ParsedAnswer] | None
    publications: tuple[str, ...]
    window: str


def _ad_missing(window: str) -> _AdEvidence:
    return _AdEvidence(None, (), window)


def _ad_section(ads: _AdEvidence, asin: str, item: _ChildInput) -> RetailAdAttributed:
    """One product's channels from the shared answers, or its typed absence (ia §7.3a item 4)."""
    if ads.answers is None:
        return RetailAdAttributed(
            (),
            RetailDetailReason.MISSING_CURRENT_PUBLICATION,
            f"Amazon Marketing Cloud hasn't sent purchases for this product for {ads.window}. "
            "This section fills in by itself when they arrive. The Vendor Central figures "
            "above are complete.",
        )
    marketplace = item.context.marketplace
    by_channel: dict[str, tuple[RetailAdComparison, RetailAdComparison]] = {}
    for channel in item.channel_labels:
        key = (asin, channel, marketplace)
        sides: list[RetailAdComparison] = []
        present = False
        for metric in RETAIL_PRODUCT_AD_METRICS:
            current_rows, units, _ = ads.answers[(metric, "current")]
            prior_rows, prior_units, _ = ads.answers[(metric, "prior")]
            present = present or key in current_rows or key in prior_rows
            chosen = units if units.currency_code is not None else prior_units
            sides.append(
                retail_ad_comparison(
                    metric,
                    RetailAdValue(current_rows.get(key, 0), chosen, item.context.period),
                    RetailAdValue(prior_rows.get(key, 0), chosen, item.context.prior_period),
                    item.context.semantics,
                )
            )
        if present:
            by_channel[channel] = (sides[0], sides[1])
    if not by_channel:
        return RetailAdAttributed(
            (),
            RetailDetailReason.EMPTY_COVERED,
            f"Amazon Marketing Cloud credits no purchases of this product to ads in {ads.window}.",
        )
    return RetailAdAttributed(ad_values_by_channel(by_channel, item.channel_labels))


@dataclass(frozen=True, slots=True)
class _ChildInput:
    """Everything one product-detail child is reduced from, all from the parent's bake."""

    context: _ReductionContext
    artifact: RetailHealthArtifact
    asin: str
    row: RetailProductRow
    product: ProductKey
    reduced: _ReducedRows
    ads: _AdEvidence
    labels: Mapping[str, str]
    channel_labels: Mapping[str, str]


def _product_comparisons(item: _ChildInput) -> tuple[RetailComparison, ...]:
    """Every Retail health tile for one product, measured or reduced from its operands."""
    key = (item.asin, item.context.marketplace)
    operands: dict[str, dict[str, Decimal | int]] = {"current": {}, "prior": {}}
    units: dict[str, Units] = {}
    for metric in _MEASURED_ROW_METRICS:
        for role in ("current", "prior"):
            rows, metric_units, _ = item.context.answers[("row", metric, role)]
            operands[role][metric] = rows.get(key, 0)
            units[metric] = metric_units
    currency = units[_SHIPPED_REVENUE]
    by_metric: dict[str, RetailComparison] = {}
    for metric in RETAIL_HEALTH_TOTAL_METRICS:
        measured = metric in _MEASURED_ROW_METRICS
        by_metric[metric] = _compared(
            item.context,
            _ComparisonSides(
                metric,
                operands["current"][metric]
                if measured
                else _derived_total_value(metric, operands["current"]),
                operands["prior"][metric]
                if measured
                else _derived_total_value(metric, operands["prior"]),
                units[metric] if measured else _derived_units(metric, currency),
                RETAIL_HEALTH_ROW_GRAIN,
            ),
        )
    return tuple(by_metric[metric] for metric in RETAIL_HEALTH_TOTAL_METRICS)


def _product_series[P: (RetailSalesPoint, RetailTrafficPoint)](
    item: _ChildInput, metric: str, point: type[P]
) -> tuple[P, ...]:
    """One product's daily figure beside the same offset in the prior window."""
    context = item.context
    current_rows, units, _ = context.answers[("spark", metric, "current")]
    prior_rows, prior_units, _ = context.answers[("spark", metric, "prior")]
    if units != prior_units:
        raise ValueError("product series units differ between windows.")
    points: list[P] = []
    for offset in range(context.period.days):
        day = context.period.start + timedelta(days=offset)
        prior_day = context.prior_period.start + timedelta(days=offset)
        points.append(
            point(
                day,
                RetailMetricValue(
                    current_rows.get((day.isoformat(), item.asin, context.marketplace), 0),
                    units,
                    DateRange(day, day),
                    RETAIL_PRODUCT_SERIES_GRAIN,
                ),
                RetailMetricValue(
                    prior_rows.get((prior_day.isoformat(), item.asin, context.marketplace), 0),
                    units,
                    DateRange(prior_day, prior_day),
                    RETAIL_PRODUCT_SERIES_GRAIN,
                ),
            )
        )
    return tuple(points)


def _product_insights(
    item: _ChildInput, totals: tuple[RetailComparison, ...]
) -> tuple[RetailProductInsight, ...]:
    """Return the parent's baked movers card when it names this product, with its stats."""
    movers = next(slot for slot in item.artifact.insights if slot.id is InsightSlotId.RETAIL_MOVERS)
    named = item.reduced.movers_asins[:_MOVERS_NAMED]
    if movers.state is not InsightSlotState.FILLED or item.asin not in named:
        return ()
    rank = item.reduced.movers_asins.index(item.asin) + 1
    finding = item.reduced.movers[rank - 1]
    revenue = next(total for total in totals if total.metric == _SHIPPED_REVENUE)
    code = revenue.current.units.currency_code
    change = f"{currency_display_text(finding.value, None, CurrencyTextStyle.SIGNED_BARE)} {code}"
    return (
        RetailProductInsight(
            movers,
            product_insight_stats(item.row.title, revenue, change, rank, item.reduced.total_count),
            RETAIL_PRODUCT_INSIGHT_PROVENANCE,
        ),
    )


_MOVERS_NAMED = 3
"""The deterministic movers text names the top three nonzero movers."""


def _product_child(item: _ChildInput) -> RetailProductChild:
    """Reduce one displayed row's product-detail child from the parent's own answers."""
    totals = _product_comparisons(item)
    insights = _product_insights(item, totals)
    period = item.context.period
    detail = RetailProductDetailArtifact(
        outcome=RetailProductDetailOutcome.SELECTED,
        period=period,
        prior_period=item.context.prior_period,
        product=ProductDetailIdentity(item.row.title, item.row.child_asin, item.row.parent_asin),
        distributor_view=item.artifact.distributor_view,
        totals=totals,
        absences=(),
        sales_daily=_product_series(item, _SHIPPED_REVENUE, RetailSalesPoint),
        traffic_daily=_product_series(item, _GLANCE_VIEWS, RetailTrafficPoint),
        ad_attributed=_ad_section(item.ads, item.asin, item),
        insights=insights,
        no_insight_line=None
        if insights
        else f"No Retail health insight names this product for {window_label(period)}.",
        evidence_status=retail_health_evidence_status(
            period, tuple(total.metric for total in totals), item.labels
        ),
    )
    return RetailProductChild(item.product, detail)


def _movers_product_ref(
    artifact: RetailHealthArtifact,
    reduced: _ReducedRows,
    children: tuple[RetailProductChild, ...],
) -> str | None:
    """Name the product a filled movers card names first, when it is a displayed row."""
    movers = next(slot for slot in artifact.insights if slot.id is InsightSlotId.RETAIL_MOVERS)
    if movers.state is not InsightSlotState.FILLED or not reduced.movers_asins:
        return None
    first = reduced.movers_asins[0]
    return next(
        (
            child.product_ref
            for asin, child in zip(reduced.asins, children, strict=True)
            if asin == first
        ),
        None,
    )


def _empty_artifact(
    period: DateRange, prior: DateRange, revision: PresetArtifactRevision
) -> RetailHealthArtifact:
    """Declare a covered window with no retail movement, without fabricating figures."""
    return RetailHealthArtifact(
        outcome=RetailHealthOutcome.EMPTY,
        period=period,
        prior_period=prior,
        distributor_view=RetailDistributorView(LENS_DEFAULTS["distributor_view"]),
        totals=(),
        absences=(),
        insights=unavailable_insight_slots(
            RETAIL_HEALTH_INSIGHT_IDS, artifact_revision=revision.value
        ),
        trend=(),
        rows=(),
        total_count=0,
        truncation=None,
        reconciliation=None,
        boundary=RETAIL_HEALTH_BOUNDARY,
    )


def _retail_movers_silence_condition(root: Path) -> str:
    """Read the retail-movers contract's governed silence condition; contracts own the text."""
    for _, contract in load_insight_contracts(root):
        if contract.id is InsightSlotId.RETAIL_MOVERS:
            return contract.silence_condition
    raise RuntimeError("the retail-movers insight contract is undeclared.")


def _shipped_revenue_currency(totals: tuple[RetailComparison, ...]) -> str | None:
    """Return the currency the retail movers move: the shipped-revenue total's code."""
    revenue_total = next((total for total in totals if total.metric == _SHIPPED_REVENUE), None)
    return None if revenue_total is None else revenue_total.current.units.currency_code


def _retail_movers_slot(request: DeterministicMoversSlotRequest) -> InsightSlot:
    """Render the deterministic retail movers slot without prompt or model access.

    No nonzero mover is the retail-movers contract's declared silence (W12).
    """
    if not nonzero_movers(request.findings) or request.currency_code is None:
        return InsightSlot(
            InsightSlotId.RETAIL_MOVERS,
            InsightOutputClass.OBSERVED,
            InsightSlotState.SILENT,
            None,
            None,
            request.artifact_revision,
            "deterministic-v1",
            absence_class=InsightAbsenceClass.EVALUATED_QUIET,
            absence_reason=request.silence_reason,
        )
    rendered = deterministic_movers_text(request.findings, request.currency_code)
    return InsightSlot(
        InsightSlotId.RETAIL_MOVERS,
        InsightOutputClass.OBSERVED,
        InsightSlotState.FILLED,
        f"{rendered}.",
        "Server-ranked absolute shipped-revenue changes.",
        request.artifact_revision,
        "deterministic-v1",
    )


def _covered_empty(answer: PresentationMeasurementAnswerSlot, period: DateRange) -> bool:
    """Whether an answer is complete, unsuppressed, untruncated, and simply has no rows."""
    shaped = answer.answer
    if shaped is None:
        return False
    metadata = shaped.metadata
    return (
        not shaped.data.values
        and metadata.row_count == 0
        and metadata.requested_coverage_intervals == (period,)
        and metadata.actual_coverage_intervals == (period,)
        and not metadata.coverage_holes
        and not metadata.suppression.rows_suppressed
        and not metadata.truncated
        and metadata.freshness_status is not FreshnessStatus.NEVER_LOADED
    )


def _answer_rows(  # noqa: PLR0913, PLR0917
    answer: PresentationMeasurementAnswerSlot,
    period: DateRange,
    group: tuple[str, ...],
    kind: UnitKind,
    *,
    basis: MeasurementBasis = MeasurementBasis.RETAIL_ACTUAL,
) -> _ParsedAnswer | None:
    """Parse one complete grouped answer of one basis, or none when incoherent.

    A read cut at its declared width refuses the page (`rows_over_ceiling`, ADR-142): Retail
    declares no section a refusal may own, so the page is the read's smallest grain.
    """
    shaped = answer.answer
    if shaped is None:
        return None
    if shaped.metadata.truncated:
        raise IsolatedRefusal(
            IsolatedRefusalReason.ROWS_OVER_CEILING,
            RefusalScope.PAGE,
            max_rows=shaped.metadata.row_count,
        )
    if basis is not MeasurementBasis.RETAIL_ACTUAL and _covered_empty(answer, period):
        # A covered window in which Amazon credits nothing to ads is a legitimate empty set.
        return {}, Units(kind), ()
    metadata, data = shaped.metadata, shaped.data
    units = data.units
    # A complete, unsuppressed answer with no rows is a legitimate zero for a
    # non-monetary additive metric; the envelope states no units for it, so the
    # declared unit stands in. An empty monetary answer stays refused.
    if units is None and not data.values and kind is not UnitKind.CURRENCY:
        units = Units(kind)
    if (
        metadata.requested_coverage_intervals != (period,)
        or metadata.actual_coverage_intervals != (period,)
        or metadata.coverage_holes
        or metadata.suppression.rows_suppressed
        or metadata.truncated
        or metadata.freshness_status is FreshnessStatus.NEVER_LOADED
        or data.grain.dimensions != group
        or metadata.measurement_basis != (basis,)
        or units is None
        or units.kind is not kind
        or metadata.row_count != len(data.values)
    ):
        return None
    qualifiers = tuple(
        sorted(
            {
                *metadata.qualified_dq,
                *(item.rendering for item in metadata.qualifiers),
                *(
                    ()
                    if metadata.freshness_status is FreshnessStatus.FRESH
                    else (metadata.freshness_status.value,)
                ),
            }
        )
    )
    rows: dict[tuple[str, ...], Decimal | int] = {}
    for raw_row in data.values:
        raw = cast("dict[str, object]", raw_row)
        key = tuple(raw.get(item) for item in group)
        value = raw.get(MEASURE_ALIAS)
        if (
            set(raw) != {*group, MEASURE_ALIAS}
            or any(not isinstance(item, str) or not item.strip() for item in key)
            or isinstance(value, bool)
            or not isinstance(value, (int, Decimal))
            or value < 0
            or key in rows
        ):
            return None
        rows[cast("tuple[str, ...]", key)] = value
    return rows, units, qualifiers


@dataclass(frozen=True, slots=True)
class RetainedRetailHealthPresetArtifactSource:
    """Compile one declared Retail Health preset variant from retained truth only.

    The source measures the full grouped population, lets the domain contract bound
    the displayed rows at fifty with a boundless total, derives the artifact revision
    from the exact publication identities used, and publishes one product-detail child
    per displayed row with the private custody that names each row's product (ADR-125).
    """

    coverage: SourceCoveragePort
    coverage_requirements: tuple[SourceCoverageRequirement, ...]
    temporal_policy: PresentationTemporalPolicy
    compiler: RetailHealthCompiler
    pointers: PresetArtifactPointerStorePort
    clock: Callable[[], datetime]
    page: PresetArtifactPage = field(default_factory=lambda: PresetArtifactPage("retail-health"))

    def __post_init__(self) -> None:
        if not self.coverage_requirements:
            raise ValueError("retail health preset baking requires declared source coverage.")

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return a complete retained-materializer publication or no bakeable artifact.

        `SourceUnchanged` when the computed revision is the current pointer's (S6 §2.8, D26).
        """
        windows = self._resolved_windows(variant)
        if windows is None:
            return None
        window, prior = windows
        rendered = self.compiler.render_retail_health(RetailHealthRenderInput(window, prior))
        if rendered.artifact is None:
            return None
        revision = PresetArtifactRevision.from_publication_identities(
            rendered.publication_identities,
            render_format=RETAIL_HEALTH_PRESET_RENDER_FORMAT,
        )
        current = self.pointers.current(self.page, variant)
        if current is not None and current.revision == revision:
            return SourceUnchanged(revision)
        body = PresetArtifactBody(
            self.page,
            variant,
            revision,
            capped_preset_artifact_json(retail_health_artifact_payload(rendered.artifact)),
        )
        children = _child_bodies(self.page, variant, revision, rendered.children)
        # The manifest is the custody header; the stage adds the sibling index, the children's
        # one digest home (ADR-145), and refuses children that aren't exactly the served rows.
        header = (
            None
            if not rendered.children
            else PresetArtifactCustodyHeader(
                capped_preset_artifact_json(
                    retail_health_custody_manifest_payload(
                        RetailHealthCustodyManifest(
                            body.digest,
                            tuple(
                                RetailHealthCustodyProduct(child.product_ref, child.product)
                                for child in rendered.children
                            ),
                            rendered.movers_product_ref,
                        )
                    )
                )
            )
        )
        # Every child and its custody are written and attested before the parent, and the
        # pointer advances last, so a served revision always has its whole closed child set.
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
            PresetArtifactSiblingStream.of(children),
            None
            if header is None
            else frozenset(
                PresetArtifactProductChildIdentity(child.product_ref) for child in rendered.children
            ),
        )

    def _resolved_windows(
        self, variant: PresetArtifactVariant
    ) -> tuple[DateRange, DateRange] | None:
        """Resolve the declared preset window and its comparator without clocks."""
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


def _child_bodies(  # noqa: PLR0913, PLR0917 -- one parent identity and its children
    page: PresetArtifactPage,
    variant: PresetArtifactVariant,
    revision: PresetArtifactRevision,
    children: tuple[RetailProductChild, ...],
) -> tuple[tuple[PresetArtifactSiblingBody, PresetArtifactSiblingCustody], ...]:
    """Encode each child's public body and private custody under the parent's revision."""
    encoded: list[tuple[PresetArtifactSiblingBody, PresetArtifactSiblingCustody]] = []
    for child in children:
        identity = PresetArtifactProductChildIdentity(child.product_ref)
        public = PresetArtifactSiblingBody(
            page,
            variant,
            revision,
            identity,
            capped_preset_artifact_json(retail_product_detail_payload(child.artifact)),
        )
        private = PresetArtifactSiblingCustody(
            page,
            variant,
            revision,
            identity,
            capped_preset_artifact_json(
                retail_product_child_custody_payload(
                    RetailProductChildCustody(child.product_ref, child.product, public.digest)
                )
            ),
        )
        encoded.append((public, private))
    return tuple(encoded)


def retail_channel_labels(taxonomy: MediaTaxonomy) -> dict[str, str]:
    """Name every governed channel in taxonomy order, for ad-attributed product rows."""
    return {
        term.key.value: term.name for term in taxonomy.terms if isinstance(term.key, ChannelKey)
    }


def _display_asin(canonical: str) -> str:
    return canonical.removeprefix(_ASIN_NAMESPACE_PREFIX)


@dataclass(frozen=True, slots=True)
class CanonicalRetailProductHierarchy:
    """Read effective dim_product identities from retained canonical truth."""

    current: CurrentPublicationPort
    canonical: CanonicalDatasetReadPort
    packs: tuple[SourcePack, ...]
    contracts: tuple[DataContract, ...]
    bridge: CommerceIdentityBridge

    def hierarchy(self, window: DateRange) -> RetailProductHierarchyResult | None:
        """Return the latest effective product identities, or none without evidence."""
        located = _ready_pack(self.packs, self.contracts, "dim_product")
        if located is None:
            return None
        pack, contract = located
        by_asin: dict[str, RetailProductIdentity] = {}
        publications: set[str] = set()
        try:
            for scope in publication_scopes_for(
                PublicationScopeRequest(pack, contract, self.bridge, {})
            ):
                candidates = self.current.current_for_scope(scope, horizon=LATEST_ONLY)
                if not candidates:
                    continue
                publication = max(
                    candidates,
                    key=lambda item: (
                        item.partition.bucket.window.end,
                        item.published_at,
                        item.publication_id.value,
                    ),
                )
                publications.add(publication.publication_id.value)
                for raw in self.canonical.rows(publication):
                    row = VendorProductHierarchyRow.model_validate(dict(raw))
                    valid_from = row.valid_from.date()
                    valid_to = None if row.is_current else row.valid_to.date()
                    if valid_from > window.end or (valid_to is not None and valid_to <= window.end):
                        continue
                    child = _display_asin(row.amazon_asin)
                    parent = (
                        child
                        if row.amazon_parent_asin is None
                        else _display_asin(row.amazon_parent_asin)
                    )
                    title = (
                        row.product_title
                        if row.product_title is not None and row.product_title.strip()
                        else child
                    )
                    by_asin[row.amazon_asin] = RetailProductIdentity(child, parent, title)
        except ValueError:
            return None
        if not by_asin:
            return None
        return RetailProductHierarchyResult(by_asin, tuple(sorted(publications)))


@dataclass(frozen=True, slots=True)
class CanonicalRetailReportedTotals:
    """Sum the vendor-reported control totals from retained canonical truth."""

    current: CurrentPublicationPort
    canonical: CanonicalDatasetReadPort
    packs: tuple[SourcePack, ...]
    contracts: tuple[DataContract, ...]
    bridge: CommerceIdentityBridge

    def reported_totals(self, window: DateRange) -> RetailReportedTotals | None:
        """Return complete window sums under the serving lens, or none when partial."""
        located = _ready_pack(self.packs, self.contracts, "fct_retail_sales_reported_total")
        if located is None:
            return None
        pack, contract = located
        # Only buckets intersecting the window are read below, so the read starts one longest
        # bucket before it (the overlap rule, A5.1 I9).
        horizon = ScopeHorizon(
            from_bucket_start=window.start - timedelta(days=pack.time.longest_bucket_days)
        )
        revenue = Decimal(0)
        units = Decimal(0)
        seen: set[tuple[str, str]] = set()
        marketplaces: set[str] = set()
        publications: set[str] = set()
        try:
            for scope in publication_scopes_for(
                PublicationScopeRequest(pack, contract, self.bridge, {})
            ):
                for publication in self.current.current_for_scope(scope, horizon=horizon):
                    bucket = publication.partition.bucket.window
                    if bucket.start > window.end or bucket.end < window.start:
                        continue
                    contributed = False
                    for raw in self.canonical.rows(publication):
                        row = VendorSalesReportedTotalRow.model_validate(dict(raw))
                        if not window.start <= row.date_key <= window.end:
                            continue
                        key = (row.date_key.isoformat(), row.marketplace_key)
                        if key in seen:
                            return None
                        seen.add(key)
                        marketplaces.add(row.marketplace_key)
                        revenue += row.revenue_shipped
                        units += Decimal(row.units_shipped)
                        contributed = True
                    if contributed:
                        publications.add(publication.publication_id.value)
        except ValueError:
            return None
        if len(marketplaces) != 1:
            return None
        expected = {
            (window.start + timedelta(days=offset)).isoformat() for offset in range(window.days)
        }
        if {day for day, _ in seen} != expected:
            return None
        return RetailReportedTotals(revenue, units, tuple(sorted(publications)))


def _ready_pack(
    packs: tuple[SourcePack, ...],
    contracts: tuple[DataContract, ...],
    dataset: str,
) -> tuple[SourcePack, DataContract] | None:
    pack = next(
        (
            item
            for item in packs
            if item.dataset.value == dataset and item.readiness is PackReadiness.READY
        ),
        None,
    )
    if pack is None:
        return None
    contract = next((item for item in contracts if item.contract_id == pack.contract_id), None)
    if contract is None:
        return None
    return pack, contract


def retail_health_coverage_requirements(
    schemas: PresentationPageSchemaSet, catalog: Catalog
) -> tuple[SourceCoverageRequirement, ...]:
    """Derive the retail cohort's source coverage from its declared page metrics."""
    page = next((item for item in schemas.pages if item.page_id == "retail-health"), None)
    if page is None:
        raise RuntimeError("retail health presentation schema is unavailable for preset baking.")
    requirements: list[SourceCoverageRequirement] = []
    for visual in page.visuals:
        for name in visual.metric_cohort:
            declared = catalog.metric(name)
            if declared is None:
                raise RuntimeError(f"retail health preset metric {name} is unavailable.")
            requirement = metric_coverage_requirement(catalog, declared)
            if requirement not in requirements:
                requirements.append(requirement)
    return tuple(requirements)


def retained_retail_health_preset_artifact_source(  # noqa: PLR0913, PLR0917 -- composition inputs
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort,
    current: BakeSourceReads,
) -> RetainedRetailHealthPresetArtifactSource | None:
    """Compose the retained retail source, or none while its configuration is absent."""
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
    canonical = S3CanonicalAdapter(settings.semantic_v2_bucket)
    metric_catalog = load_catalog(root)
    schemas = load_v2_page_schemas(root)
    retail_schema = next(
        (schema for schema in schemas.pages if schema.page_id == "retail-health"),
        None,
    )
    if retail_schema is None:
        raise RuntimeError("retail health presentation schema is unavailable for preset baking.")
    horizons = scope_horizon_policy(current)
    reader = cast("CurrentPublicationInspectionPort", current)
    selector = PublicationSelector(reader, source_packs, bridge, horizons=horizons)
    compiler = RetailHealthArtifactRenderer(
        catalog=metric_catalog,
        contracts=contracts,
        # Bake-time evidence snapshots are per source pack (ADR-070 point 5); the
        # request-serving path keeps the strict single-transaction selector.
        evidence=PerPackPresentationEvidenceInspection(reader, source_packs, bridge),
        tooling=MeasurementTooling(
            queries=queries,
            publications=selector,
            query_batch_max_concurrency=settings.athena_query_batch_max_concurrency,
        ),
        hierarchy=CanonicalRetailProductHierarchy(
            reader, canonical, source_packs, contracts, bridge
        ),
        reported=CanonicalRetailReportedTotals(reader, canonical, source_packs, contracts, bridge),
        clock=current.clock,
        movers_silence_reason=_retail_movers_silence_condition(root),
        commerce_scope=bridge.commerce_scope,
        channel_labels=retail_channel_labels(load_media_taxonomy(root)),
    )
    return RetainedRetailHealthPresetArtifactSource(
        coverage=PageWindowCatalog(reader, source_packs, contracts, bridge, horizons),
        coverage_requirements=retail_health_coverage_requirements(schemas, metric_catalog),
        temporal_policy=retail_schema.temporal_policy,
        compiler=compiler,
        pointers=pointers,
        clock=current.clock,
    )
