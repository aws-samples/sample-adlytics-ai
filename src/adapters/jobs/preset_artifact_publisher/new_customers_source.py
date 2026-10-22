# SPDX-License-Identifier: MIT-0
"""Bake `NewCustomersArtifact` v2 per declared preset (docs/ui/new-customers, ADR-075 pattern).

Per preset and per governed marketplace, every figure is planned through the measurement layer:
step 1 at ad-product grain (with the equal prior on a rolling preset), the step-2 periodic
figures for the resolved calendar period at grain set `platform` for the tiles and at grain set
`channel` for the per-channel table, read against the ad-product roster (plan D4,
`domain.measurement.rosters`), and the gateway at ad product x product with its ad-product
totals. The per-channel delivery lands on its own, so it has its own pending and missing states
and never holds back the tiles. Nothing is divided here; the domain builds the share of channel
from the two planned answers. The gateway is planned only over a window the pack-v2 coverage
gate covers (`domain.measurement.pack_coverage`); otherwise every channel reads the coverage text.

Registered with the publisher, and fails closed: the source composes only once the v2 page entry
`new-customers` exists (the UI workstream adds it), so today the page stays unavailable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, cast

from aws_lambda_powertools import Logger

from adapters.campaign_insight_requests import FindingSource, predicate_findings
from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.inbound.tool_handlers.measurement import (
    MeasurementExecutionTooling,
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    attach_pack_coverage,
    attach_roster,
    execute_presentation_batch,
    prepare_batch,
)
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher.campaign_source import (
    SUPERSEDED_HISTORY_RETENTION,
)
from adapters.jobs.preset_artifact_publisher.isolated_refusals import capped_preset_artifact_json
from adapters.jobs.preset_artifact_publisher.retail_source import (
    CanonicalRetailProductHierarchy,
    RetailProductHierarchySource,
    RetailProductIdentity,
)
from adapters.jobs.preset_artifact_publisher.scorecard_source import (
    MARKETPLACE_SELECTOR,
    GroupedAnswer,
    grouped_answer_complete,
    parse_grouped_answer,
)
from adapters.outbound.bake_sources import source_closed
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
from adapters.outbound.telemetry.reporting import Report, report_error
from adapters.producer.packs import load_query_packs
from domain.measurement.pack_coverage import (
    PackCoverageVerdict,
    pack_coverage_verdict,
    pack_versioned_columns,
)
from domain.measurement.rosters import Roster, apply_roster, coarser_grain_set
from domain.narrative.shaped_response import UnitKind
from domain.presentation.compared_metrics import new_customers_change_semantics
from domain.presentation.insights import (
    InsightContract,
    InsightEvidenceInput,
    InsightFindingKind,
    InsightSlotId,
    OperandBasis,
    finding_is_material,
    horizon_role_for,
)
from domain.presentation.new_customers import (
    NEW_BUYERS_DECLARED_PERIODS,
    NEW_CUSTOMERS_ARTIFACT_TYPE,
    NEW_CUSTOMERS_ARTIFACT_VERSION,
    NEW_CUSTOMERS_COVERAGE_METRICS,
    NEW_CUSTOMERS_PAGE_ID,
    GatewayEntry,
    NewBuyers,
    NewBuyersAwaitingRequest,
    NewBuyersFiguresRequest,
    NewCustomersArtifact,
    NewCustomersLabels,
    NewCustomersMarket,
    NewCustomersOutcome,
    NtbShareMover,
    PerChannel,
    PerChannelAwaitingRequest,
    PerChannelFiguresRequest,
    PerChannelInput,
    PortfolioAcquisitionSlotRequest,
    StepOneInput,
    StepOneRow,
    gateway_entry,
    gateway_not_covered,
    new_buyers_awaiting,
    new_buyers_figures,
    new_buyers_outside_declared_periods,
    new_customers_artifact_payload,
    new_customers_calendar_period,
    ntb_share_movers,
    per_channel_awaiting,
    per_channel_figures,
    portfolio_acquisition_cards,
    portfolio_acquisition_slot,
    short_window_label,
    step_one_row,
    window_label,
)
from domain.presentation.page_schemas import (
    TemporalPresetId,
    calendar_preset_grain,
    is_calendar_preset,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactPage,
    PresetArtifactPointerTemplate,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactVariant,
    SourceUnchanged,
)
from domain.presentation.scorecard import scorecard_marketplace_label
from domain.presentation.temporal import (
    TemporalComparisonStatus,
    TemporalResolutionAvailable,
    TemporalResolutionRequest,
    TemporalSelection,
    TemporalSelectionKind,
    resolve_temporal,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.identity import AdProductKey, ChannelGroupKey
from domain.semantics.time import TimeGrain
from domain.shared_kernel.values import DateRange
from ports.outbound.pack_coverage import PackCoverageReadRequest
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionPlan,
    PresentationEvidenceInspectionRequest,
)
from ports.outbound.query import BAKE_MAX_ROWS, RowCeiling
from ports.outbound.rosters import RosterRequest
from ports.outbound.semantic_state import scope_horizon_policy
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest
from ports.outbound.source_coverage import (
    SourceCoverageRequest,
    SourceCoverageRequirement,
    metric_coverage_requirement,
)

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from adapters.config import PresetArtifactBakeSettings
    from adapters.inbound.tool_handlers.measurement import PresentationMeasurementAnswerSlot
    from adapters.outbound.bake_sources import BakeSourceReads
    from domain.catalog.capability import Catalog
    from domain.data_contracts.contract import DataContract
    from domain.presentation.change import ComparedMetric
    from domain.presentation.page_schemas import (
        PresentationPageSchema,
        PresentationPageSchemaSet,
        PresentationTemporalPolicy,
    )
    from ports.outbound.preset_artifacts import PresetArtifactPointerStorePort
    from ports.outbound.publication_selection import PresentationEvidenceInspectionPort
    from ports.outbound.query import PeriodSeriesQueryPort
    from ports.outbound.rosters import RosterPort
    from ports.outbound.semantic_state import CurrentPublicationInspectionPort
    from ports.outbound.source_coverage import SourceCoveragePort

__all__ = [
    "NEW_CUSTOMERS_PRESET_RENDER_FORMAT",
    "NewCustomersArtifactRenderer",
    "NewCustomersCompiler",
    "NewCustomersRenderInput",
    "NewCustomersRenderResult",
    "NewCustomersStaticLabels",
    "RetainedNewCustomersPresetArtifactSource",
    "new_customers_bake_plan_count",
    "new_customers_coverage_requirements",
    "new_customers_labels",
    "new_customers_policy",
    "retained_new_customers_preset_artifact_source",
]

NEW_CUSTOMERS_PRESET_RENDER_FORMAT = 2
"""The New customers renderer's output generation (ADR-070 render format).

2: the share change is ADR-121's toned block, `evidence_status` is ADR-119's coverage block,
and with two marketplaces each market names itself (UI slice 9, plan D3 and D4)."""

_SPEND = "ad_spend"
_NTB_PURCHASES = "purchases_new_to_brand_12mo"
_NTB_REVENUE = "revenue_new_to_brand_12mo"
_ATTRIBUTED = "attributed_revenue_14d"
_SHARE = "ntb_revenue_share"
_COST_PER_NTB = "cost_per_ntb"
_GATEWAY = "purchases_new_to_brand_incl_halo_12mo"
_BUYERS = "buyers_new_to_brand_12mo"
_COST_PER_BUYER = "cost_per_new_to_brand_buyer_12mo"
_BUYERS_CONTRACT = "amc-new-to-brand-buyers-platform"
_CHANNEL_CONTRACT = "amc-new-to-brand-buyers-channel"
_BUYERS_FACT = "fct_new_to_brand_buyers"
_STEP_ONE_GRAINS = ("ecosystem", "platform", "channel", "ad_product", "marketplace")
_GATEWAY_PRODUCT_GRAINS = ("channel", "ad_product", "product")
_GATEWAY_TOTAL_GRAINS = ("channel", "ad_product")
_STEP_TWO_GRAINS = ("marketplace",)
_PER_CHANNEL_GRAINS = ("channel", "ad_product")
_STEP_ONE_METRICS = (_SPEND, _NTB_PURCHASES, _NTB_REVENUE, _ATTRIBUTED, _COST_PER_NTB)
_COVERAGE_METRICS = NEW_CUSTOMERS_COVERAGE_METRICS
"""Delivery and attributed conversions gate a bake; the gateway and new buyers degrade."""
_ECOSYSTEM = "amazon"
_PLATFORM = "amazon_ads"
_PERIOD_GRAIN_SELECTOR = "time_grain"
_GRAIN_SET_SELECTOR = "grain_set"
_PLATFORM_GRAIN_SET = "platform"
_CHANNEL_GRAIN_SET = "channel"
_ASIN_PREFIX = "amazon_asin:"
_ARTIFACT_ID = "new_customers"
_STEP_TWO_SECTION = "new-to-brand-step-2"
_BAKED_SECTIONS = frozenset(
    {"new-to-brand", "new-customers-insights", _STEP_TWO_SECTION, "gateway-products"}
)
"""The page sections this bake fills (docs/ui/new-customers/low-level-design.md §6)."""
_OPERATION = "new_customers_artifact_render"
_CORRELATION_ID = "new-customers-artifact"
LOGGER = Logger(service="adlytics-preset-artifact-baker")

type _Label = tuple[str, str, str, str]
"""(marketplace, section, metric, role) for one prepared plan."""


@dataclass(frozen=True, slots=True)
class NewCustomersStaticLabels:
    """Governed names, halo scopes and windows read from the taxonomy and the dictionary."""

    ad_products: tuple[tuple[str, str, str, str], ...]
    """(ad product key, channel key, label, channel group label) of the Amazon Ads network, in
    taxonomy order."""
    marketplaces: tuple[str, ...]
    labels: NewCustomersLabels
    buyers_period_grains: tuple[TimeGrain, ...]
    buyers_caveat: str
    share_semantics: ComparedMetric
    """`ntb_revenue_share`'s governed change semantics (ADR-121), from the dictionary."""
    marketplace_labels: Mapping[str, str] = field(default_factory=dict[str, str])
    """Each marketplace's short or governed name, which names its sections with two shown."""

    def __post_init__(self) -> None:
        if not self.marketplaces or tuple(sorted(set(self.marketplaces))) != self.marketplaces:
            raise ValueError("New customers measures each governed marketplace once, in order.")


def new_customers_labels(root: Path, catalog: Catalog) -> NewCustomersStaticLabels:
    """Read ad products from the taxonomy and halo scope and windows from the dictionary."""
    taxonomy = load_media_taxonomy(root)
    groups = {
        term.key.value: term.name
        for term in taxonomy.terms
        if isinstance(term.key, ChannelGroupKey)
    }
    ad_products = tuple(
        (term.key.value, term.channel.value, term.name, groups[term.channel_group.value])
        for term in taxonomy.terms
        if isinstance(term.key, AdProductKey)
        and term.platform is not None
        and term.platform.value == _PLATFORM
        and term.channel_group is not None
        and term.channel is not None
    )
    step_one, gateway = catalog.metric(_NTB_PURCHASES), catalog.metric(_GATEWAY)
    revenue, buyers = catalog.metric(_ATTRIBUTED), catalog.metric(_BUYERS)
    if step_one is None or gateway is None or revenue is None or buyers is None:
        raise RuntimeError("the New customers metric cohort is undeclared.")
    if revenue.attribution_window_days is None or step_one.lookback_months is None:
        raise RuntimeError("New customers labels need the attribution window and lookback.")
    bridge = load_commerce_identity_bridge(root)
    governed = {item.marketplace.value: item.value for item in bridge.marketplace_display_names}
    marketplaces = tuple(sorted({item.target.value for item in bridge.marketplace_mappings}))
    return NewCustomersStaticLabels(
        ad_products=ad_products,
        marketplaces=marketplaces,
        share_semantics=new_customers_change_semantics(catalog).for_metric("ntb_revenue_share"),
        # The Scorecard's short name ("US"), else the governed display name, else the key.
        marketplace_labels={
            key: scorecard_marketplace_label(key, governed.get(key, key)) for key in marketplaces
        },
        labels=NewCustomersLabels(
            network_label="Amazon",
            attribution_window_days=revenue.attribution_window_days,
            lookback_months=step_one.lookback_months,
            step_one_includes_brand_halo=bool(step_one.includes_brand_halo),
            gateway_includes_brand_halo=bool(gateway.includes_brand_halo),
        ),
        buyers_period_grains=buyers.period_grains,
        buyers_caveat=buyers.customer_caveat,
    )


@dataclass(frozen=True, slots=True)
class NewCustomersRenderInput:
    """One resolved preset: its window, its equal prior (rolling), or its period (calendar)."""

    preset: TemporalPresetId
    period: DateRange
    prior_period: DateRange | None = None
    calendar_period: CalendarPeriod | None = None

    def __post_init__(self) -> None:
        if (self.calendar_period is None) == (self.prior_period is None):
            raise ValueError("a rolling preset compares with its prior; a calendar one doesn't.")
        if self.calendar_period is not None and self.calendar_period.window != self.period:
            raise ValueError("a calendar preset's window is its period.")


@dataclass(frozen=True, slots=True)
class NewCustomersRenderResult:
    """A reduced artifact and the exact publication identities used."""

    artifact: NewCustomersArtifact | None
    publication_identities: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.artifact is not None and not self.publication_identities:
            raise ValueError("a rendered New customers artifact must name its publications.")


class NewCustomersCompiler(Protocol):
    """Measure and reduce one preset into a New customers artifact."""

    def render_new_customers(self, request: NewCustomersRenderInput) -> NewCustomersRenderResult:
        """Return the reduced artifact and the exact publication identities consumed."""
        ...


@dataclass(frozen=True, slots=True)
class _MarketPlan:
    """What one marketplace measures, decided from publication state before any query."""

    marketplace: str
    gateway: PackCoverageVerdict
    buyers_published: bool = False
    """The resolved period's new-buyers cell is CURRENT (calendar presets only)."""
    buyers_lag_days: int = 0
    """The new-buyers pack's declared lag after period end (ADR-110)."""
    buyers_latest: DateRange | None = None
    """The latest CURRENT new-buyers period of the same grain, for the missing text."""
    channel_published: bool = False
    """The resolved period's per-channel delivery is CURRENT (calendar presets only)."""
    channel_lag_days: int = 0
    channel_latest: DateRange | None = None
    """The latest CURRENT per-channel period of the same grain, for the missing text."""


@dataclass(frozen=True, slots=True)
class _Reduction:
    """One render's resolved preset, per-marketplace plans and parsed answers."""

    request: NewCustomersRenderInput
    plans: tuple[_MarketPlan, ...]
    answers: dict[_Label, GroupedAnswer]
    rosters: dict[_Label, Roster] = field(default_factory=dict[_Label, Roster])
    """The ad-product roster each per-channel plan was planned against (plan D4)."""

    def rows(self, label: _Label) -> GroupedAnswer:
        """Return one parsed answer by (marketplace, section, metric, role)."""
        return self.answers[label]


@dataclass(frozen=True, slots=True)
class _RostersOnce:
    """One bake's roster reads: each (kind, period, marketplace, platform) is read once."""

    port: RosterPort
    read: dict[RosterRequest, Roster] = field(default_factory=dict[RosterRequest, Roster])

    def roster(self, request: RosterRequest) -> Roster:
        """Return the period's roster, reading it from the port the first time it's asked for."""
        if request not in self.read:
            self.read[request] = self.port.roster(request)
        return self.read[request]


@dataclass(frozen=True, slots=True)
class _BatchBuilder:
    """Build one preset's measurement batch and its slot labels, with no I/O."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    clock: Callable[[], datetime]

    def request(  # noqa: PLR0913 — one plan's coordinates, by keyword
        self,
        metric: str,
        grains: tuple[str, ...],
        window: DateRange,
        *,
        filters: Mapping[str, str],
        prior: DateRange | None = None,
    ) -> MeasurementToolRequest:
        return MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN
            if prior is None
            else MeasurementTool.COMPARE_WINDOWS,
            catalog=self.catalog,
            contracts=self.contracts,
            metric_name=metric,
            grains=grains,
            window=window,
            prior_window=prior,
            computed_at=self.clock(),
            correlation_id=_CORRELATION_ID,
            filters=filters,
            max_rows=BAKE_MAX_ROWS,
            row_ceiling=RowCeiling.BAKE,
        )

    def batch(
        self, request: NewCustomersRenderInput, plans: tuple[_MarketPlan, ...]
    ) -> tuple[tuple[MeasurementToolRequest, ...], tuple[_Label, ...]]:
        requests: list[MeasurementToolRequest] = []
        labels: list[_Label] = []
        window, prior = request.period, request.prior_period
        for plan in plans:
            market = plan.marketplace
            pinned = {MARKETPLACE_SELECTOR: market}
            for metric in _STEP_ONE_METRICS:
                requests.append(self.request(metric, _STEP_ONE_GRAINS, window, filters=pinned))
                labels.append((market, "step1", metric, "current"))
            requests.append(
                self.request(_SHARE, _STEP_ONE_GRAINS, window, filters=pinned, prior=prior)
            )
            labels.append((market, "step1", _SHARE, "current"))
            if prior is not None:
                labels.append((market, "step1", _SHARE, "prior"))
            if plan.gateway.covered:
                for section, grains in (
                    ("gateway_products", _GATEWAY_PRODUCT_GRAINS),
                    ("gateway_total", _GATEWAY_TOTAL_GRAINS),
                ):
                    requests.append(self.request(_GATEWAY, grains, window, filters=pinned))
                    labels.append((market, section, _GATEWAY, "current"))
            period = request.calendar_period
            if period is not None and plan.buyers_published:
                periodic = {
                    **pinned,
                    _PERIOD_GRAIN_SELECTOR: period.grain.value,
                    _GRAIN_SET_SELECTOR: _PLATFORM_GRAIN_SET,
                }
                for metric in (_BUYERS, _COST_PER_BUYER):
                    requests.append(
                        self.request(metric, _STEP_TWO_GRAINS, window, filters=periodic)
                    )
                    labels.append((market, "step2", metric, "current"))
                requests.append(self.request(_SPEND, _STEP_TWO_GRAINS, window, filters=pinned))
                labels.append((market, "step2", _SPEND, "current"))
                if plan.channel_published:
                    # Read at grain set channel, never derived from the platform row (D-1).
                    per_channel = {**periodic, _GRAIN_SET_SELECTOR: _CHANNEL_GRAIN_SET}
                    for metric in (_BUYERS, _COST_PER_BUYER):
                        requests.append(
                            self.request(metric, _PER_CHANNEL_GRAINS, window, filters=per_channel)
                        )
                        labels.append((market, "step2_channel", metric, "current"))
        return tuple(requests), tuple(labels)


@dataclass(frozen=True, slots=True)
class NewCustomersArtifactRenderer:
    """Execute and reduce governed New customers evidence without page or session custody."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    evidence: PresentationEvidenceInspectionPort
    tooling: MeasurementTooling
    labels: NewCustomersStaticLabels
    insight_contract: InsightContract
    clock: Callable[[], datetime]
    titles: RetailProductHierarchySource | None = None

    def __post_init__(self) -> None:
        if self.insight_contract.id is not InsightSlotId.PORTFOLIO_ACQUISITION:
            raise ValueError("New customers renders the portfolio-acquisition insight contract.")
        if (
            self.tooling.pack_coverage is None
            or self.tooling.periods is None
            or self.tooling.rosters is None
        ):
            raise ValueError(
                "New customers needs the pack-coverage, served-period and roster readers."
            )

    def _market_plans(self, request: NewCustomersRenderInput) -> tuple[_MarketPlan, ...]:
        """Read gateway coverage and new-buyers publication per marketplace (state only)."""
        port, periods = self.tooling.pack_coverage, self.tooling.periods
        if port is None or periods is None:  # pragma: no cover — __post_init__ refuses it
            raise ValueError("New customers needs its publication-state readers.")
        columns = tuple(
            column for column in pack_versioned_columns(self.contracts) if column.column == _GATEWAY
        )
        versioned = tuple(
            contract
            for contract in self.contracts
            if contract.contract_id in {column.contract_id for column in columns}
        )
        buyers = tuple(
            item
            for item in self.contracts
            if item.contract_id in {_BUYERS_CONTRACT, _CHANNEL_CONTRACT}
        )
        plans: list[_MarketPlan] = []
        for market in self.labels.marketplaces:
            filters = {MARKETPLACE_SELECTOR: market}
            (coverage,) = port.pack_coverage(
                PackCoverageReadRequest(versioned, filters, (request.period,))
            )
            (column,) = columns
            verdict = pack_coverage_verdict(request.period, column.from_version, coverage.versions)
            period = request.calendar_period
            if period is None:
                plans.append(_MarketPlan(market, verdict))
                continue
            served = periods.served_periods(
                ServedPeriodsRequest(contracts=buyers, filters=filters, now=self.clock())
            )
            plans.append(
                _MarketPlan(
                    market,
                    verdict,
                    buyers_published=(_BUYERS_CONTRACT, period) in served.publications,
                    buyers_lag_days=served.cadence_lag_days[_BUYERS_CONTRACT],
                    buyers_latest=_latest(served, _BUYERS_CONTRACT, period.grain),
                    channel_published=(_CHANNEL_CONTRACT, period) in served.publications,
                    channel_lag_days=served.cadence_lag_days[_CHANNEL_CONTRACT],
                    channel_latest=_latest(served, _CHANNEL_CONTRACT, period.grain),
                )
            )
        return tuple(plans)

    def render_new_customers(self, request: NewCustomersRenderInput) -> NewCustomersRenderResult:
        """Measure every marketplace, reduce it, and bake the deterministic insight."""
        plans = self._market_plans(request)
        requests, labels = _BatchBuilder(self.catalog, self.contracts, self.clock).batch(
            request, plans
        )
        # Buyers and cost per buyer read one roster per marketplace: read it once per bake.
        rosters_once = None if self.tooling.rosters is None else _RostersOnce(self.tooling.rosters)
        prepared = prepare_batch(
            tuple(
                attach_roster(attach_pack_coverage(item, self.tooling.pack_coverage), rosters_once)
                for item in requests
            )
        )
        if len(prepared.plans) != len(labels):
            raise RuntimeError("the New customers batch and its slot labels must align.")
        rosters = {
            label: plan.logical.roster
            for label, plan in zip(labels, prepared.plans, strict=True)
            if plan.logical.roster is not None
        }
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
            return NewCustomersRenderResult(None, ())
        answers = self._parsed(request, executed.slots, labels)
        if answers is None:
            return NewCustomersRenderResult(None, ())
        artifact = self._artifact(_Reduction(request, plans, answers, rosters), measured)
        return NewCustomersRenderResult(artifact, measured if artifact is not None else ())

    def _parsed(
        self,
        request: NewCustomersRenderInput,
        slots: tuple[PresentationMeasurementAnswerSlot, ...],
        labels: tuple[_Label, ...],
    ) -> dict[_Label, GroupedAnswer] | None:
        """Parse every answer; a partial, truncated or mis-pinned one bakes nothing."""
        grains = {
            "step1": _STEP_ONE_GRAINS,
            "gateway_products": _GATEWAY_PRODUCT_GRAINS,
            "gateway_total": _GATEWAY_TOTAL_GRAINS,
            "step2": _STEP_TWO_GRAINS,
            "step2_channel": _PER_CHANNEL_GRAINS,
        }
        parsed: dict[_Label, GroupedAnswer] = {}
        for answer, label in zip(slots, labels, strict=True):
            market, section, _metric, role = label
            window = request.period if role == "current" else request.prior_period
            item = parse_grouped_answer(answer, cast("DateRange", window), grains[section])
            if item is None or not grouped_answer_complete(item, cast("DateRange", window)):
                # Every section the batch planned is over covered days, so anything short of a
                # complete answer is missing evidence: no bake, never an undercount.
                return None
            if section in {"step1", "step2"} and any(key[-1] != market for key in item.rows):
                return None
            parsed[label] = item
        return parsed

    def _artifact(
        self, reduction: _Reduction, measured: tuple[str, ...]
    ) -> NewCustomersArtifact | None:
        request = reduction.request
        revision = PresetArtifactRevision.from_publication_identities(
            measured, render_format=NEW_CUSTOMERS_PRESET_RENDER_FORMAT
        )
        period_label = (
            window_label(request.period)
            if request.calendar_period is None
            else request.calendar_period.label
        )
        windows = (
            None
            if request.prior_period is None
            else (short_window_label(request.period), short_window_label(request.prior_period))
        )
        try:
            markets = tuple(
                market
                for plan in reduction.plans
                if (market := self._market(reduction, plan)) is not None
            )
            movers = ntb_share_movers(markets)
            material = tuple(
                mover for mover in movers if self._material(mover, request.period.days)
            )
            slot_request = PortfolioAcquisitionSlotRequest(
                material,
                artifact_revision=revision.value,
                silence_reason=self.insight_contract.silence_condition,
                windows=windows,
            )
            return NewCustomersArtifact(
                outcome=NewCustomersOutcome.COMPLETE if markets else NewCustomersOutcome.EMPTY,
                preset=request.preset,
                period=request.period,
                prior_period=request.prior_period,
                period_label=period_label,
                labels=self.labels.labels,
                markets=markets,
                insights=(portfolio_acquisition_slot(slot_request),),
                insight_cards=portfolio_acquisition_cards(slot_request),
            )
        except ValueError as error:
            # An artifact invariant refused what the reduction built: report it, then bake
            # nothing, so the break alarms instead of reading as "nothing to bake".
            report_error(
                error,
                Report(operation=_OPERATION, correlation_id=_CORRELATION_ID, logger=LOGGER),
            )
            return None

    def _material(self, mover: NtbShareMover, days: int) -> bool:
        (bound,) = predicate_findings(
            (mover.finding,),
            InsightFindingKind.ABSOLUTE_CHANGE,
            InsightEvidenceInput.NETWORK_CHANNEL_COMPARISONS,
            source=FindingSource(
                InsightSlotId.PORTFOLIO_ACQUISITION, OperandBasis.CHANNEL_WINDOW, _SHARE
            ),
            priors={mover.finding.name: mover.prior},
        )
        return finding_is_material(
            self.insight_contract.predicate.materiality, bound, horizon_role_for(days)
        )

    def _market(self, reduction: _Reduction, plan: _MarketPlan) -> NewCustomersMarket | None:
        """One marketplace's sections in its own currency, or none when it has no delivery."""
        request, market = reduction.request, plan.marketplace

        def rows(section: str, metric: str, role: str = "current") -> GroupedAnswer:
            return reduction.rows((market, section, metric, role))

        spend = rows("step1", _SPEND)
        if not spend.rows:
            return None
        units = spend.units
        if units is None or units.kind is not UnitKind.CURRENCY or units.currency_code is None:
            raise ValueError("New customers spend carries one currency per marketplace.")
        currency = units.currency_code
        compared = request.prior_period is not None
        step_one: list[StepOneRow] = []
        for ad_product, channel, label, group in self.labels.ad_products:
            key = (_ECOSYSTEM, _PLATFORM, channel, ad_product, market)
            if key not in spend.rows:
                continue
            step_one.append(
                step_one_row(
                    StepOneInput(
                        ad_product_key=ad_product,
                        ad_product_label=label,
                        channel_group_label=group,
                        currency_code=currency,
                        ntb_purchases=rows("step1", _NTB_PURCHASES).rows.get(key),
                        ntb_revenue=rows("step1", _NTB_REVENUE).rows.get(key),
                        attributed_revenue=rows("step1", _ATTRIBUTED).rows.get(key),
                        share=rows("step1", _SHARE).rows.get(key),
                        cost_per_ntb=rows("step1", _COST_PER_NTB).rows.get(key),
                        compared=compared,
                        prior_share=(
                            rows("step1", _SHARE, "prior").rows.get(key) if compared else None
                        ),
                        window_text=window_label(request.period),
                        prior_text=(
                            "the prior window"
                            if request.prior_period is None
                            else window_label(request.prior_period)
                        ),
                        share_semantics=self.labels.share_semantics,
                    )
                )
            )
        return NewCustomersMarket(
            marketplace_key=market,
            currency_code=currency,
            new_to_brand=tuple(step_one),
            new_buyers=self._new_buyers(reduction, plan, currency),
            gateway=self._gateway(reduction, plan, tuple(step_one)),
            marketplace_label=self.labels.marketplace_labels.get(market, market),
        )

    def _new_buyers(self, reduction: _Reduction, plan: _MarketPlan, currency: str) -> NewBuyers:
        period = reduction.request.calendar_period
        if period is None:
            return new_buyers_outside_declared_periods(self.labels.buyers_period_grains)
        if not plan.buyers_published:
            return new_buyers_awaiting(
                NewBuyersAwaitingRequest(
                    period,
                    expected_after=period.end + timedelta(days=plan.buyers_lag_days),
                    latest_available_period=plan.buyers_latest,
                    as_of=self.clock().date(),
                )
            )
        key = (plan.marketplace,)
        step_two = (plan.marketplace, "step2")
        return new_buyers_figures(
            NewBuyersFiguresRequest(
                period,
                buyers=reduction.rows((*step_two, _BUYERS, "current")).rows.get(key),
                cost=reduction.rows((*step_two, _COST_PER_BUYER, "current")).rows.get(key),
                spend=reduction.rows((*step_two, _SPEND, "current")).rows.get(key),
                currency_code=currency,
                caveat=self.labels.buyers_caveat,
                per_channel=self._per_channel(reduction, plan, currency),
            )
        )

    def _per_channel(self, reduction: _Reduction, plan: _MarketPlan, currency: str) -> PerChannel:
        """Build the per-channel table for P: the roster applied to the channel grain set's rows."""
        period = cast("CalendarPeriod", reduction.request.calendar_period)
        if not plan.channel_published:
            return per_channel_awaiting(
                PerChannelAwaitingRequest(
                    period,
                    expected_after=period.end + timedelta(days=plan.channel_lag_days),
                    latest_available_period=plan.channel_latest,
                    as_of=self.clock().date(),
                )
            )
        label = (plan.marketplace, "step2_channel", _BUYERS, "current")
        buyers = reduction.rows(label).rows
        cost_answer = reduction.rows(
            (plan.marketplace, "step2_channel", _COST_PER_BUYER, "current")
        )
        costs = cost_answer.rows
        # Each cost is labelled with its own answer's currency, never the step-1 spend's, so
        # the market's one-currency invariant checks something real.
        if any(value is not None for value in costs.values()):
            units = cost_answer.units
            if units is None or units.kind is not UnitKind.CURRENCY or units.currency_code is None:
                raise ValueError("New customers cost per new buyer per channel carries a currency.")
            currency = units.currency_code
        by_ad_product = {key[-1]: value for key, value in buyers.items() if value is not None}
        cost_of = {key[-1]: value for key, value in costs.items()}
        # The same domain rule the answer applies: rostered keys with no row are absent, and
        # the loss is graded, never withdrawn under 20 entries (design §4.4).
        outcome = apply_roster(
            reduction.rosters[label],
            tuple(by_ad_product),
            coarser_grain_set(_BUYERS_FACT, _CHANNEL_GRAIN_SET),
        )
        rostered = set(outcome.roster.keys)
        rows = tuple(
            PerChannelInput(
                ad_product_key=ad_product,
                ad_product_label=name,
                channel_group_label=group,
                buyers=by_ad_product.get(ad_product),
                cost=cost_of.get(ad_product),
                on_roster=ad_product in rostered,
            )
            for ad_product, _channel, name, group in self.labels.ad_products
            if ad_product in rostered or ad_product in by_ad_product
        )
        listed = {item.ad_product_key for item in rows}
        if set(by_ad_product) - listed or rostered - listed:
            raise ValueError("every per-channel key is a governed Amazon Ads ad product.")
        return per_channel_figures(
            PerChannelFiguresRequest(
                period,
                currency_code=currency,
                rows=rows,
                note=outcome.note,
                caveat=self.labels.buyers_caveat,
            )
        )

    def _gateway(
        self, reduction: _Reduction, plan: _MarketPlan, step_one: tuple[StepOneRow, ...]
    ) -> tuple[GatewayEntry, ...]:
        channel_of = {item[0]: item[1] for item in self.labels.ad_products}
        channels = tuple((row.ad_product_key, row.ad_product_label) for row in step_one)
        if not plan.gateway.covered:
            return tuple(gateway_not_covered(channel, plan.gateway) for channel in channels)
        products = reduction.rows((plan.marketplace, "gateway_products", _GATEWAY, "current")).rows
        totals = reduction.rows((plan.marketplace, "gateway_total", _GATEWAY, "current")).rows
        identities = (
            None if self.titles is None else self.titles.hierarchy(reduction.request.period)
        )
        titles: Mapping[str, RetailProductIdentity] = (
            {} if identities is None else identities.by_asin
        )
        entries: list[GatewayEntry] = []
        for ad_product, label in channels:
            listed: dict[str, tuple[str, int]] = {}
            pair = (channel_of[ad_product], ad_product)
            for (channel, product, asin), value in products.items():
                if (channel, product) != pair or value is None:
                    continue
                identity = titles.get(asin)
                if identity is None:
                    # No effective title is governed for it: the product shows its ASIN.
                    shown = asin.removeprefix(_ASIN_PREFIX)
                    listed[shown] = (shown, int(value))
                else:
                    listed[identity.child_asin] = (identity.title, int(value))
            total = totals.get(pair)
            entries.append(
                gateway_entry((ad_product, label), listed, None if total is None else int(total))
            )
        return tuple(entries)


def _latest(served: ServedPeriods, contract_id: str, grain: TimeGrain) -> DateRange | None:
    """Return the latest CURRENT period of one contract and grain, for a missing text."""
    return max(
        (
            item.window
            for published, item in served.publications
            if published == contract_id and item.grain is grain
        ),
        key=lambda window: window.end,
        default=None,
    )


def new_customers_coverage_requirements(catalog: Catalog) -> tuple[SourceCoverageRequirement, ...]:
    """Spend and attributed conversions gate a bake; gateway and new buyers degrade."""
    requirements: list[SourceCoverageRequirement] = []
    for name in _COVERAGE_METRICS:
        declared = catalog.metric(name)
        if declared is None:
            raise RuntimeError(f"New customers metric {name} is unavailable.")
        requirement = metric_coverage_requirement(catalog, declared)
        if requirement not in requirements:
            requirements.append(requirement)
    return tuple(requirements)


@dataclass(frozen=True, slots=True)
class RetainedNewCustomersPresetArtifactSource:
    """Compile one declared New customers preset variant from retained truth only."""

    coverage: SourceCoveragePort
    coverage_requirements: tuple[SourceCoverageRequirement, ...]
    temporal_policy: PresentationTemporalPolicy
    compiler: NewCustomersCompiler
    pointers: PresetArtifactPointerStorePort
    clock: Callable[[], datetime]
    page: PresetArtifactPage = field(
        default_factory=lambda: PresetArtifactPage(NEW_CUSTOMERS_PAGE_ID)
    )

    def __post_init__(self) -> None:
        if not self.coverage_requirements:
            raise ValueError("New customers preset baking requires declared source coverage.")

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return a complete retained-materializer publication or no bakeable artifact."""
        resolved = self._resolved(variant)
        if resolved is None:
            return None
        rendered = self.compiler.render_new_customers(resolved)
        if rendered.artifact is None:
            return None
        revision = PresetArtifactRevision.from_publication_identities(
            rendered.publication_identities, render_format=NEW_CUSTOMERS_PRESET_RENDER_FORMAT
        )
        current = self.pointers.current(self.page, variant)
        if current is not None and current.revision == revision:
            return SourceUnchanged(revision)
        body = PresetArtifactBody(
            self.page,
            variant,
            revision,
            capped_preset_artifact_json(new_customers_artifact_payload(rendered.artifact)),
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
                # The window this body serves and its prior (ADR-116 decision 4), as every other
                # baker writes them, so the date control names each preset's dates.
                resolved_window=resolved.period,
                comparison_window=resolved.prior_period,
            ),
        )

    def _resolved(self, variant: PresetArtifactVariant) -> NewCustomersRenderInput | None:
        """Map a bake variant onto a declared preset, then resolve it."""
        presets = self.temporal_policy.candidate_presets
        preset = next((item for item in presets if item.value == variant.value), None)
        return None if preset is None else self.resolve(preset)

    def resolve(self, preset: TemporalPresetId) -> NewCustomersRenderInput | None:
        """Resolve a declared preset: rolling with its equal prior, or a calendar period.

        A bake variant is the preset id itself (`PresetArtifactVariant`), so a calendar variant
        such as `last_complete_week` reaches here unmapped.
        """
        if preset not in self.temporal_policy.candidate_presets:
            return None
        intervals = self.coverage.source_coverage(
            SourceCoverageRequest(self.coverage_requirements)
        ).bounds.intervals
        if is_calendar_preset(preset):
            return self._calendar(preset, intervals)
        return self._rolling(preset, intervals)

    def _calendar(
        self, preset: TemporalPresetId, intervals: tuple[DateRange, ...]
    ) -> NewCustomersRenderInput | None:
        """Resolve P on daily coverage alone; the step-2 section isn't an anchor."""
        # The serve-time miss path resolves through the same domain rule (LLD "Time policy").
        result = new_customers_calendar_period(preset, self.clock().date(), intervals)
        if result.period is None or result.grain is not calendar_preset_grain(preset):
            return None
        return NewCustomersRenderInput(preset, result.period.window, calendar_period=result.period)

    def _rolling(
        self, preset: TemporalPresetId, intervals: tuple[DateRange, ...]
    ) -> NewCustomersRenderInput | None:
        """Resolve a rolling preset and its equal prior, as on the other tabs."""
        try:
            resolved = resolve_temporal(
                TemporalResolutionRequest(
                    policy=self.temporal_policy,
                    coverage_intervals=intervals,
                    selection=TemporalSelection(
                        kind=TemporalSelectionKind.PRESET, preset_id=preset
                    ),
                    comparison_requested=True,
                )
            )
        except ValueError:
            return None
        if not isinstance(resolved, TemporalResolutionAvailable):
            return None
        control = resolved.control
        if control.comparison.status is not TemporalComparisonStatus.AVAILABLE:
            return None
        window = control.resolved.window
        prior = DateRange(
            window.start - timedelta(days=window.days), window.start - timedelta(days=1)
        )
        return NewCustomersRenderInput(preset, window, prior_period=prior)


def new_customers_bake_plan_count(root: Path) -> int:
    """Count the largest New customers bake's plans, for the bake budget (ADR-070 point 8).

    A calendar preset with gateway products covered and new buyers and new buyers per channel
    published plans every section; a rolling preset plans step 1 twice and no new buyers, so
    the calendar batch bounds every preset. The renderer refuses a prepared batch whose plans
    don't match these labels one for one, so the labels count the plans.
    """
    catalog = load_catalog(root)
    labels = new_customers_labels(root, catalog)
    period = CalendarPeriod(date(2026, 1, 5), date(2026, 1, 11), TimeGrain.WEEK)
    covered = PackCoverageVerdict(period.window, True, period.start, True)
    builder = _BatchBuilder(
        catalog,
        tuple(contract for _, contract in load_contracts(root)),
        lambda: datetime(2026, 1, 15, tzinfo=UTC),
    )
    _requests, slots = builder.batch(
        NewCustomersRenderInput(
            TemporalPresetId.LAST_COMPLETE_WEEK, period.window, calendar_period=period
        ),
        tuple(
            _MarketPlan(market, covered, buyers_published=True, channel_published=True)
            for market in labels.marketplaces
        ),
    )
    return len(slots)


def new_customers_policy(schemas: PresentationPageSchemaSet) -> PresentationTemporalPolicy | None:
    """Return the `new-customers` v2 page's temporal policy, or None until it declares this bake.

    The page id alone doesn't open the source: the entry must declare exactly the artifact this
    renderer writes (`new_customers`, `NewCustomersArtifact` v2, its three outcomes) and every
    section the bake fills, with the step-2 section bound to the declared calendar periods and
    not anchoring the page. Any other declaration keeps the page unavailable rather than baking a
    payload its decoder doesn't read.
    """
    page = next((item for item in schemas.pages if item.page_id == NEW_CUSTOMERS_PAGE_ID), None)
    if page is None or not _declares_this_bake(page):
        return None
    return page.temporal_policy


def _declares_this_bake(page: PresentationPageSchema) -> bool:
    """Whether a `new-customers` entry declares the v2 artifact and the sections this bake fills."""
    if len(page.artifacts) != 1:
        return False
    (artifact,) = page.artifacts
    if (
        artifact.artifact_id != _ARTIFACT_ID
        or artifact.artifact_type != NEW_CUSTOMERS_ARTIFACT_TYPE
        or artifact.version != NEW_CUSTOMERS_ARTIFACT_VERSION
        or len(artifact.outcomes) != len(NewCustomersOutcome)
        or set(artifact.outcomes) != {item.value for item in NewCustomersOutcome}
    ):
        return False
    sections = {section.section_id: section for section in page.sections}
    if not set(sections) >= _BAKED_SECTIONS:
        return False
    step_two = sections[_STEP_TWO_SECTION]
    return (
        step_two.declared_periods == NEW_BUYERS_DECLARED_PERIODS and step_two.period_anchor is False
    )


def _portfolio_acquisition_contract(root: Path) -> InsightContract:
    for _, contract in load_insight_contracts(root):
        if contract.id is InsightSlotId.PORTFOLIO_ACQUISITION:
            return contract
    raise RuntimeError("the portfolio-acquisition insight contract is undeclared.")


def retained_new_customers_preset_artifact_source(  # noqa: PLR0913, PLR0917 -- the closed signature
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort,
    current: BakeSourceReads,
) -> RetainedNewCustomersPresetArtifactSource | None:
    """Compose the retained New customers source, or none while its page or configuration is absent.

    The closed factory signature (A5.4): the invocation's admitted, captured `queries` and its
    one pinnable CURRENT reader, whose clock every renderer reads. Fails closed, logged once,
    without the retained-truth configuration or while the v2 page entry isn't declared.
    """
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
        return source_closed(NEW_CUSTOMERS_PAGE_ID, "settings_incomplete")
    root = contracts_root()
    policy = new_customers_policy(load_v2_page_schemas(root))
    if policy is None:
        return source_closed(NEW_CUSTOMERS_PAGE_ID, "page_entry_absent")
    contracts = tuple(contract for _, contract in load_contracts(root))
    source_packs = load_source_packs(root)
    bridge = load_commerce_identity_bridge(root)
    horizons = scope_horizon_policy(current)
    reader = cast("CurrentPublicationInspectionPort", current)
    metric_catalog = load_catalog(root)
    selector = PublicationSelector(
        reader,
        source_packs,
        bridge,
        cadence_lag_days={
            pack.contract_id: pack.effective_cadence_lag_days for pack in load_query_packs(root)
        },
        horizons=horizons,
    )
    # Chat's resolver over the same governed catalog, so a campaign roster names each
    # campaign as chat does; without it SpendRosterReader refuses the roster (PLAN-0008).
    names = DimensionLabelResolver(
        CanonicalEntityCatalog(
            EntityCatalogTooling(
                current=reader,
                canonical=S3CanonicalAdapter(settings.semantic_v2_bucket),
                packs=source_packs,
                contracts=contracts,
                bridge=bridge,
                taxonomy=load_media_taxonomy(root),
            )
        ),
        AuthorizedAnalyticalScope(frozenset({bridge.commerce_scope})),
    )
    compiler = NewCustomersArtifactRenderer(
        catalog=metric_catalog,
        contracts=contracts,
        evidence=PerPackPresentationEvidenceInspection(reader, source_packs, bridge),
        tooling=MeasurementTooling(
            queries=queries,
            publications=selector,
            query_batch_max_concurrency=settings.athena_query_batch_max_concurrency,
            periods=selector,
            pack_coverage=selector,
            rosters=SpendRosterReader(
                catalog=metric_catalog,
                contracts=contracts,
                publications=selector,
                queries=queries,
                labels=ad_product_labels(load_media_taxonomy(root)),
                clock=current.clock,
                names=BakePinnedNames(names),
            ),
        ),
        labels=new_customers_labels(root, metric_catalog),
        insight_contract=_portfolio_acquisition_contract(root),
        clock=current.clock,
        titles=CanonicalRetailProductHierarchy(
            reader,
            S3CanonicalAdapter(settings.semantic_v2_bucket),
            source_packs,
            contracts,
            bridge,
        ),
    )
    return RetainedNewCustomersPresetArtifactSource(
        coverage=PageWindowCatalog(reader, source_packs, contracts, bridge, horizons),
        coverage_requirements=new_customers_coverage_requirements(metric_catalog),
        temporal_policy=policy,
        compiler=compiler,
        pointers=pointers,
        clock=current.clock,
    )
