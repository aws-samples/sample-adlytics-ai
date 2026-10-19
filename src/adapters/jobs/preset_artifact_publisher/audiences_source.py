# SPDX-License-Identifier: MIT-0
"""Bake `AudienceProfileArtifact` v1 for the latest complete month (docs/ui/audiences, ADR-075).

Per governed marketplace, every figure is planned through the measurement layer, the path chat
reads too: for each profile path with a CURRENT delivery for the month (packs 10, 11 and 12),
its segment rows' buyers, reached shoppers and new-to-brand buyers by segment, graded against the
path's segment roster (the segments it returned the month before, read through the roster port),
and its `profiled_total` row's two bases; for pack 8, each DSP campaign's segment impressions,
spend and attributed sales, each campaign graded against its own segment roster. Nothing is
planned across segments, paths or campaigns. The domain computes every share, index, CPM and
ROAS (`domain.presentation.audiences`), so a missing total reads "Not available" rather than a
share of a partial base.

A bake reads in four steps, each one batch across every marketplace, run at most the configured
query bound at once and joined by identity: the served months (one read per marketplace), the
DSP campaign lists (one planned batch), every profile path's and campaign's roster, then the
measurement batch. No read waits on a sibling in its own step, and no step is per campaign.

Paid states (ADR-107) come from publication evidence over each paid pack's latest three months:
packs 10-11 unobserved leave the paid view's callout and default the page to the DSP view; pack
13 unobserved leaves ASI segments named by ID with "name not available yet".

Registered with the publisher, and fails closed: the source composes only once the v2 page entry
`audiences` declares the LLD's whole server-relevant entry (`audiences_policy_declared`: mode,
boundary, temporal policy, `AudienceProfileArtifact` v1 and the seven sections with their periods
and anchor, as `docs/ui/audiences/low-level-design.md` declares them; the UI workstream adds
the entry).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, is_dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from adapters.campaign_insight_requests import FindingSource, predicate_findings
from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.inbound.tool_handlers.measurement import (
    DEFAULT_MAX_ROWS,
    MeasurementExecutionTooling,
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    execute_presentation_batch,
    prepare_batch,
    run_bounded,
)
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher.campaign_source import (
    SUPERSEDED_HISTORY_RETENTION,
)
from adapters.jobs.preset_artifact_publisher.scorecard_source import (
    MARKETPLACE_SELECTOR,
    GroupedAnswer,
    grouped_answer_complete,
    parse_grouped_answer,
)
from adapters.outbound.bake_sources import source_closed
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from domain.capabilities.paid_features import (
    DerivePaidFeatureStateRequest,
    PaidFeatureState,
    derive_paid_feature_state,
)
from domain.measurement.periodic import PROFILE_PATH_COLUMN, ROW_KIND_COLUMN
from domain.measurement.rosters import (
    Roster,
    RosterKind,
    apply_roster,
    roster_filters,
)
from domain.presentation.audiences import (
    AUDIENCES_ARTIFACT_TYPE,
    AUDIENCES_ARTIFACT_VERSION,
    AUDIENCES_BODY_OVER_BOUND,
    AUDIENCES_PAGE_ID,
    DELIVERY_PATHS,
    DSP_VIEW,
    NO_DELIVERY_TEXT,
    PAID_VIEW,
    VIEW_NAMESPACES,
    AudienceMarket,
    AudienceProfileArtifact,
    CampaignAudiences,
    CampaignRowRequest,
    CampaignSegment,
    DeliveryState,
    OverIndexFinding,
    OverIndexSlotRequest,
    PaidState,
    ProfileDelivery,
    ProfileSegment,
    ProfileView,
    ViewRequest,
    audiences_artifact_payload,
    audiences_unavailable_artifact_payload,
    build_view,
    campaign_segment,
    over_index_findings,
    segment_over_index_slot,
)
from domain.presentation.insights import (
    DeterministicFinding,
    InsightContract,
    InsightEvidenceInput,
    InsightFindingKind,
    InsightSlotId,
    OperandBasis,
    OverIndexPolicy,
    finding_is_material,
    horizon_role_for,
)
from domain.presentation.page_schemas import (
    Cardinality,
    PageSection,
    PageTime,
    PresentationPageMode,
    PresentationTemporalPolicy,
    TemporalComparisonPolicy,
    TemporalPresetId,
)
from domain.presentation.preset_artifacts import (
    MAX_PRESET_ARTIFACT_BYTES,
    PresetArtifactBody,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactVariant,
    SourceUnchanged,
    canonical_preset_artifact_json,
)
from domain.producer.models import DEFAULT_CADENCE_LAG_DAYS
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.audience_segments import (
    ASI_NEEDS_PAID_FEATURE_TEXT,
    SEGMENT_NAMESPACE_COLUMN,
    SegmentNamespace,
)
from domain.semantics.calendar_periods import CalendarPeriod, period_containing
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import DomainError, ValidationError
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionPlan,
    PresentationEvidenceInspectionRequest,
)
from ports.outbound.query import QueryResult
from ports.outbound.rosters import RosterRequest
from ports.outbound.segment_labels import SegmentLabelRequest
from ports.outbound.semantic_state import scope_horizon_policy
from ports.outbound.served_periods import ServedPeriodsRequest

if TYPE_CHECKING:  # pragma: no cover
    from adapters.config import PresetArtifactBakeSettings
    from adapters.inbound.tool_handlers.measurement import PresentationMeasurementAnswerSlot
    from adapters.outbound.bake_sources import BakeSourceReads
    from domain.catalog.capability import Catalog
    from domain.data_contracts.contract import DataContract
    from domain.presentation.page_schemas import PresentationPageSchema, PresentationPageSchemaSet
    from domain.semantics.audience_segments import SegmentLabel
    from ports.outbound.preset_artifacts import PresetArtifactPointerStorePort
    from ports.outbound.publication_selection import PresentationEvidenceInspectionPort
    from ports.outbound.query import PeriodSeriesQueryPort
    from ports.outbound.rosters import RosterPort
    from ports.outbound.segment_labels import SegmentLabelPort
    from ports.outbound.semantic_state import CurrentPublicationInspectionPort

__all__ = [
    "AUDIENCES_BODY_OVER_BOUND",
    "AUDIENCES_ENVELOPE_MAX_BYTES",
    "AUDIENCES_MATERIALIZATION_UNAVAILABLE",
    "AUDIENCES_RENDER_FORMAT",
    "AUDIENCES_ROW_MAX_BYTES",
    "AUDIENCES_SECTIONS",
    "AUDIENCES_TEMPORAL_POLICY",
    "AudiencesArtifactRenderer",
    "AudiencesRenderInput",
    "AudiencesRenderResult",
    "RetainedAudiencesPresetArtifactSource",
    "audiences_bake_phase_plan_counts",
    "audiences_bake_plan_count",
    "audiences_marketplace_count",
    "audiences_policy_declared",
    "governed_campaign_names",
    "retained_audiences_preset_artifact_source",
]

AUDIENCES_RENDER_FORMAT = 3
"""Generation 3: the campaigns' `shown_text`. Generation 2 added wire fields, the pointer's
`resolved_window` and the render-inputs identity; an older pointer is rebuilt, never kept."""
PROFILE_CONTRACTS = {
    "asi_in_market": "amc-segment-buyer-profile-inmarket",
    "asi_lifestyle": "amc-segment-buyer-profile-lifestyle",
    "dsp_exposed_only": "amc-segment-buyer-profile-dsp-exposed",
}
"""Each profile path's one contract (packs 10, 11, 12)."""
METADATA_CONTRACT = "amc-segment-metadata"
DELIVERY_CONTRACT = "amc-audience-performance"
SEGMENT_METRICS = ("segment_exposed_shoppers", "segment_buyers", "segment_buyers_new_to_brand")
TOTAL_METRICS = ("profiled_exposed_total", "profiled_buyers_total")
CAMPAIGN_METRICS = ("segment_impressions", "segment_spend", "segment_attributed_revenue")
SEGMENT_GRAINS = ("marketplace", "audience_segment")
TOTAL_GRAINS = ("marketplace",)
CAMPAIGN_GRAINS = ("marketplace", "campaign", "audience_segment")
LATEST_MONTH_VARIANT = "last_complete_month"
"""The one variant this page bakes: its temporal policy's default preset."""
OBSERVATION_MONTHS = 3
"""ADR-107 point 1: the latest three declared months decide whether a paid pack was observed."""
_ARTIFACT_ID = "audiences"
_CORRELATION_ID = "audiences-artifact"
_PERIOD_GRAIN_SELECTOR = "time_grain"
_GRAIN_SET_SELECTOR = "grain_set"
_CAMPAIGN_COLUMN = "campaign_key"
_SILENT = "No segment holds enough buyers with a share clearly above its share of reached shoppers."

type _Label = tuple[str, str, str]
"""(marketplace, scope, metric): scope is a profile path, or `campaign` for pack 8."""


@dataclass(frozen=True, slots=True)
class AudiencesRenderInput:
    """One resolved complete calendar month."""

    period: CalendarPeriod


@dataclass(frozen=True, slots=True)
class AudiencesRenderResult:
    """The baked artifact, or None, and the publication identities it read."""

    artifact: AudienceProfileArtifact | None
    publication_identities: tuple[str, ...]


_EMPTY_RESULT = QueryResult(
    rows=(), bytes_scanned=0, truncated=False, suppression=Suppression(rows_suppressed=False)
)
_PREFETCH_CAMPAIGN = "campaign:prefetch-0001"
"""The prefetch walker's synthetic campaign key: fictional, never rendered or stored."""


@dataclass(frozen=True, slots=True)
class _EmptyAnswers:
    """The prefetch walker's in-process query answers: every read is empty, none executes."""

    def run(self, request: object) -> QueryResult:
        del request
        return _EMPTY_RESULT

    def run_series(self, request: object) -> QueryResult:
        del request
        return _EMPTY_RESULT


class AudiencesCompiler(Protocol):
    """Render one month from retained truth only."""

    def render_audiences(self, request: AudiencesRenderInput) -> AudiencesRenderResult:
        """Return the artifact for the month, or None when it can't be baked whole."""
        ...


@dataclass
class _Batch:
    plans: list[MeasurementToolRequest] = field(default_factory=list[MeasurementToolRequest])
    labels: list[_Label] = field(default_factory=list[_Label])


@dataclass(frozen=True, slots=True)
class _Served:
    """One marketplace's published months per contract and the paid states derived from them."""

    published: dict[str, frozenset[DateRange]]
    paid: dict[str, PaidFeatureState]


@dataclass(frozen=True, slots=True)
class _Listed:
    """One marketplace's ranked shown campaigns and how many pack 8 listed."""

    shown: tuple[str, ...]
    total: int


@dataclass(frozen=True, slots=True)
class _Scope:
    """One graded delivery: a marketplace's profile path, or one of its DSP campaigns."""

    market: str
    name: str
    """The profile path, or `campaign:{campaign_key}`."""
    fact: str
    filters: dict[str, str]


@dataclass(frozen=True, slots=True)
class _Named:
    """One marketplace's delivery states and every non-measurement read its body renders."""

    states: dict[str, tuple[DeliveryState, ...]]
    """Each view's delivery states, by view."""
    labels: dict[str, dict[str, SegmentLabel]]
    """Each view's segment labels, by view."""
    campaign_names: dict[str, str]
    campaign_labels: dict[str, SegmentLabel]
    """The DSP segment labels the campaign rows read."""
    campaigns: CampaignAudiences


def governed_campaign_names(
    resolver: DimensionLabelResolver,
) -> Callable[[tuple[str, ...], DateRange], dict[str, str]]:
    """Name DSP campaigns exactly as chat does: the governed label, else its numbered placeholder.

    The resolver numbers "Unnamed campaign N" over the sorted keys it's given, so one bake's
    campaigns always number the same way.
    """

    def names(campaigns: tuple[str, ...], window: DateRange) -> dict[str, str]:
        labels = resolver.resolve(
            tuple({_CAMPAIGN_COLUMN: campaign} for campaign in campaigns),
            (_CAMPAIGN_COLUMN,),
            window,
        )
        return {
            entry.value: entry.label for entry in labels.entries if entry.column == _CAMPAIGN_COLUMN
        }

    return names


def _months_ending(period: CalendarPeriod, count: int) -> tuple[DateRange, ...]:
    months: list[DateRange] = []
    day = period.start
    for _ in range(count):
        month = period_containing(TimeGrain.MONTH, day)
        months.append(DateRange(month.start, month.end))
        day = month.start - timedelta(days=1)
    return tuple(reversed(months))


@dataclass(frozen=True, slots=True)
class AudiencesArtifactRenderer:
    """Plan packs 10-12 and 8 per marketplace and reduce them in the domain."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    evidence: PresentationEvidenceInspectionPort
    tooling: MeasurementTooling
    rosters: RosterPort
    segment_labels: SegmentLabelPort
    marketplaces: tuple[str, ...]
    insight_contract: InsightContract
    """`segment-over-index` v1."""
    clock: Callable[[], datetime]
    campaign_labels: Callable[[tuple[str, ...], DateRange], dict[str, str]]
    """Each DSP campaign's governed public label, or chat's placeholder (`governed_campaign_names`).

    No default: a campaign key is a protected identifier, so it never names itself."""
    campaign_bound: int
    """The most DSP campaigns per marketplace the page measures.

    `audiences_runtime_campaign_bound`, a known limit (TD-D2-AUDIENCES-CAMPAIGN-LIMIT).

    A presentation bound only: chat reads every campaign through its own governed access."""
    cadence_lag_days: dict[str, int] = field(default_factory=dict[str, int])
    """Each paid pack's declared lag (ADR-110), for its ADR-107 `expected_after`."""
    prefetch_campaign: str | None = None
    """Set only on the prefetch walker: one synthetic listed campaign per marketplace."""

    def __post_init__(self) -> None:
        if self.insight_contract.id is not InsightSlotId.SEGMENT_OVER_INDEX:
            raise ValueError("Audiences renders segment-over-index.")
        if len(self.marketplaces) != 1:
            raise ValueError("Audiences bakes exactly one governed marketplace.")
        if self.campaign_bound < 1:
            raise ValueError("Audiences shows at least one campaign.")

    @property
    def _policy(self) -> OverIndexPolicy:
        return cast("OverIndexPolicy", self.insight_contract.over_index_policy)

    def _contract(self, contract_id: str) -> DataContract:
        return next(item for item in self.contracts if item.contract_id == contract_id)

    def _served(self, market: str, period: CalendarPeriod) -> _Served:
        reader = self.tooling.periods
        if reader is None:
            raise ValueError("Audiences needs the served-period reader.")
        ids = (*PROFILE_CONTRACTS.values(), METADATA_CONTRACT, DELIVERY_CONTRACT)
        # One read for all five contracts: each publication names its own contract.
        served = reader.served_periods(
            ServedPeriodsRequest(
                tuple(self._contract(contract_id) for contract_id in ids),
                {MARKETPLACE_SELECTOR: market},
                self.clock(),
            )
        )
        published = {
            contract_id: frozenset(
                DateRange(item.start, item.end)
                for held, item in served.publications
                if held == contract_id and item.grain is TimeGrain.MONTH
            )
            for contract_id in ids
        }
        months = _months_ending(period, OBSERVATION_MONTHS)
        paid = {
            contract_id: derive_paid_feature_state(
                DerivePaidFeatureStateRequest(
                    period.window,
                    months,
                    published[contract_id],
                    cadence_lag_days=self.cadence_lag_days.get(
                        contract_id, DEFAULT_CADENCE_LAG_DAYS
                    ),
                )
            )
            for contract_id in (
                PROFILE_CONTRACTS["asi_in_market"],
                PROFILE_CONTRACTS["asi_lifestyle"],
                METADATA_CONTRACT,
            )
        }
        return _Served(published, paid)

    def _request(  # noqa: PLR0913, PLR0917 -- one metric: marketplace, grains, filters, roster
        self,
        market: str,
        metric: str,
        grains: tuple[str, ...],
        read: tuple[CalendarPeriod, dict[str, str]],
        roster: Roster | None = None,
    ) -> MeasurementToolRequest:
        period, filters = read
        return MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=self.catalog,
            contracts=self.contracts,
            metric_name=metric,
            grains=grains,
            window=period.window,
            computed_at=self.clock(),
            correlation_id=_CORRELATION_ID,
            filters={
                MARKETPLACE_SELECTOR: market,
                _PERIOD_GRAIN_SELECTOR: TimeGrain.MONTH.value,
                **filters,
            },
            max_rows=DEFAULT_MAX_ROWS,
            rosters=() if roster is None else (roster,),
        )

    def _roster(self, period: CalendarPeriod, scope: _Scope) -> Roster:
        """Read one roster once per bake: a profile path's, or one campaign's, segments."""
        contract = next(item for item in self.contracts if item.target_table == scope.fact)
        return self.rosters.roster(
            RosterRequest(
                kind=RosterKind.SEGMENT,
                period=period.window,
                marketplace_key=scope.market,
                platform_key=contract.platform_key,
                filters=roster_filters(RosterKind.SEGMENT, scope.filters),
                fact=scope.fact,
            )
        )

    def prefetch_audiences(self, request: AudiencesRenderInput) -> None:
        """Make every CURRENT read a real render of the month can reach, executing no query.

        Plan A5.1 prefetch, under the bake's RECORD reader: the walker lists one synthetic
        campaign per marketplace and answers every query empty in process, so each roster's
        served periods and month selections, each campaign scope's plans, the segment-label and
        campaign-name scopes are read as a populated render reads them. No query reaches the
        bake's port, which forbids them during prefetch. A selection refusal is the render's
        own, reached again from the replayed reads.
        """
        empty = _EmptyAnswers()
        rosters = self.rosters
        if is_dataclass(rosters) and hasattr(rosters, "queries"):
            rosters = cast("RosterPort", replace(cast("Any", rosters), queries=empty))
        walker = replace(
            self,
            tooling=replace(self.tooling, queries=empty),
            rosters=rosters,
            prefetch_campaign=_PREFETCH_CAMPAIGN,
        )
        try:
            walker.render_audiences(request)
        except DomainError, ValidationError:
            return

    def render_audiences(self, request: AudiencesRenderInput) -> AudiencesRenderResult:
        """Measure every marketplace's profile paths and DSP campaigns for the month."""
        period = request.period
        bound = self.tooling.query_batch_max_concurrency
        served = dict(
            zip(
                self.marketplaces,
                run_bounded(
                    tuple(partial(self._served, market, period) for market in self.marketplaces),
                    bound,
                ),
                strict=True,
            )
        )
        listed = self._campaigns(
            tuple(
                market
                for market in self.marketplaces
                if period.window in served[market].published[DELIVERY_CONTRACT]
            ),
            period,
        )
        scopes = tuple(
            scope
            for market in self.marketplaces
            for scope in self._scopes(
                market, period, (served[market], listed.get(market, _Listed((), 0)).shown)
            )
        )
        read = run_bounded(tuple(partial(self._roster, period, scope) for scope in scopes), bound)
        rosters = {
            (scope.market, scope.name): roster for scope, roster in zip(scopes, read, strict=True)
        }
        batch = _Batch()
        for scope, roster in zip(scopes, read, strict=True):
            self._plan(scope, (period, roster), batch)
        if not batch.plans:
            return AudiencesRenderResult(None, ())
        measured = self._measured(batch, period)
        if measured is None:
            return AudiencesRenderResult(None, ())
        answers, measured_identities = measured
        named = {
            market: self._named(
                market, period, (served[market], answers, rosters), listed.get(market)
            )
            for market in self.marketplaces
        }
        # Labels, campaign names, rosters, paid states and campaign publication change the body
        # without changing a measured publication, so the revision names what they read too.
        identities = tuple(
            sorted({*measured_identities, _render_inputs_identity(rosters, named, served)})
        )
        revision = PresetArtifactRevision.from_publication_identities(
            identities, render_format=AUDIENCES_RENDER_FORMAT
        ).value
        markets = tuple(
            self._market(market, period, (served[market], rosters, named[market], revision))
            for market in self.marketplaces
        )
        artifact = AudienceProfileArtifact(period, markets, self._policy.buyers_min)
        return AudiencesRenderResult(artifact, identities)

    def _scopes(
        self,
        market: str,
        period: CalendarPeriod,
        found: tuple[_Served, tuple[str, ...]],
    ) -> tuple[_Scope, ...]:
        """Return a marketplace's graded deliveries: each published path, then each campaign."""
        served, campaigns = found
        profiles = tuple(
            _Scope(
                market,
                path,
                "fct_segment_profile",
                {
                    _GRAIN_SET_SELECTOR: "platform",
                    PROFILE_PATH_COLUMN: path,
                    SEGMENT_NAMESPACE_COLUMN: self._namespace(path).value,
                },
            )
            for path, contract_id in PROFILE_CONTRACTS.items()
            if period.window in served.published[contract_id]
        )
        delivered = tuple(
            _Scope(
                market,
                f"campaign:{campaign}",
                "fct_segment_delivery",
                {**_campaign_base(), "campaign_key": campaign},
            )
            for campaign in campaigns
        )
        return (*profiles, *delivered)

    def _plan(self, scope: _Scope, graded: tuple[CalendarPeriod, Roster], batch: _Batch) -> None:
        """Plan one delivery's figures, its segment figures graded against its own roster.

        A campaign plans pack 8's three figures by segment; a profile path its segment rows'
        three figures and its `profiled_total` row's two bases, which no roster grades.
        """
        period, roster = graded
        market = scope.market
        if scope.fact == "fct_segment_delivery":
            for metric in CAMPAIGN_METRICS:
                batch.plans.append(
                    self._request(market, metric, SEGMENT_GRAINS, (period, scope.filters), roster)
                )
                batch.labels.append((market, scope.name, metric))
            return
        for metric in SEGMENT_METRICS:
            filters = {**scope.filters, ROW_KIND_COLUMN: "segment"}
            batch.plans.append(
                self._request(market, metric, SEGMENT_GRAINS, (period, filters), roster)
            )
            batch.labels.append((market, scope.name, metric))
        for metric in TOTAL_METRICS:
            filters = {**scope.filters, ROW_KIND_COLUMN: "profiled_total"}
            batch.plans.append(self._request(market, metric, TOTAL_GRAINS, (period, filters)))
            batch.labels.append((market, scope.name, metric))

    def _campaigns(self, markets: tuple[str, ...], period: CalendarPeriod) -> dict[str, _Listed]:
        """List each marketplace's DSP campaigns pack 8 returned and the top ones the page shows.

        One planned batch, ungraded. Campaigns rank by their largest matched-segment impressions,
        descending, then by campaign key, the order that numbers their opaque references. Segment
        figures never add up (impressions repeat per matched segment), so the rank never sums
        them.
        """
        if not markets:
            return {}
        if self.prefetch_campaign is not None:
            # The prefetch walker (A5.1): pack 8's list is the one result-dependent input, and a
            # campaign key never selects a partition or a scope, so one synthetic campaign per
            # marketplace reaches every per-campaign read family.
            return {market: _Listed((self.prefetch_campaign,), 1) for market in markets}
        platform = self._contract(DELIVERY_CONTRACT).platform_key
        requests = tuple(
            self._request(
                market,
                "segment_impressions",
                CAMPAIGN_GRAINS,
                (period, _campaign_base()),
                Roster(RosterKind.SEGMENT, period.window, market, platform, ()),
            )
            for market in markets
        )
        answers = self._run(requests, period, CAMPAIGN_GRAINS)
        if answers is None:
            raise RuntimeError("pack 8's campaign list for a published month can't be read.")
        if any(
            key[0] != market
            for market, answer in zip(markets, answers, strict=True)
            for key in answer.rows
        ):
            # As `_parsed` does: a list answered for another marketplace is never attributed.
            raise RuntimeError("pack 8's campaign list carries another marketplace's campaigns.")
        return {
            market: self._ranked(answer) for market, answer in zip(markets, answers, strict=True)
        }

    def _ranked(self, answer: GroupedAnswer) -> _Listed:
        largest: dict[str, Decimal] = {}
        for key, value in answer.rows.items():
            campaign = str(key[1])
            figure = Decimal(0) if value is None else Decimal(value)
            largest[campaign] = max(largest.get(campaign, figure), figure)
        ranked = sorted(largest, key=lambda campaign: (-largest[campaign], campaign))
        return _Listed(tuple(ranked[: self.campaign_bound]), len(ranked))

    def _run(
        self,
        requests: tuple[MeasurementToolRequest, ...],
        period: CalendarPeriod,
        grains: tuple[str, ...],
    ) -> tuple[GroupedAnswer, ...] | None:
        prepared = prepare_batch(requests)
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
        parsed = tuple(parse_grouped_answer(slot, period.window, grains) for slot in executed.slots)
        if any(item is None or not grouped_answer_complete(item, period.window) for item in parsed):
            return None
        return cast("tuple[GroupedAnswer, ...]", parsed)

    def _measured(
        self, batch: _Batch, period: CalendarPeriod
    ) -> tuple[dict[_Label, GroupedAnswer], tuple[str, ...]] | None:
        prepared = prepare_batch(tuple(batch.plans))
        if len(prepared.plans) != len(batch.labels):
            raise RuntimeError("the Audiences batch and its labels must align.")
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
        answers = _parsed(executed.slots, batch, period)
        if answers is None:
            return None
        identities = tuple(
            sorted(
                {
                    publication.publication_id.value
                    for slot in inspection.slots
                    if slot.bound is not None
                    for publication in slot.bound.all_publications
                }
            )
        )
        return answers, identities

    @staticmethod
    def _namespace(path: str) -> SegmentNamespace:
        view = PAID_VIEW if path in DELIVERY_PATHS[PAID_VIEW] else DSP_VIEW
        return VIEW_NAMESPACES[view]

    def _labels(  # noqa: PLR0913, PLR0917 -- one namespace's segments in one marketplace and month
        self, market: str, namespace: SegmentNamespace, keys: set[str], period: CalendarPeriod
    ) -> dict[str, SegmentLabel]:
        # No early return on no keys: a Baker's pinning port reads the label scopes then.
        return dict(
            self.segment_labels.labels(
                SegmentLabelRequest(namespace, market, tuple(sorted(keys)), period.end)
            )
        )

    def _named(  # noqa: PLR0913, PLR0917 -- a marketplace, its month, evidence and campaigns
        self,
        market: str,
        period: CalendarPeriod,
        measured: tuple[_Served, dict[_Label, GroupedAnswer], dict[tuple[str, str], Roster]],
        listed: _Listed | None,
    ) -> _Named:
        """Read a marketplace's segment labels and campaign names before its revision exists."""
        _served, answers, rosters = measured
        states: dict[str, tuple[DeliveryState, ...]] = {}
        labels: dict[str, dict[str, SegmentLabel]] = {}
        for view in (PAID_VIEW, DSP_VIEW):
            states[view] = tuple(
                self._delivery_state(market, path, measured, period)
                for path in DELIVERY_PATHS[view]
            )
            keys = {
                *(
                    segment.segment_id
                    for item in states[view]
                    if item.delivery
                    for segment in item.delivery.segments
                ),
                *(
                    key
                    for path in DELIVERY_PATHS[view]
                    for key in getattr(rosters.get((market, path)), "keys", ())
                ),
            }
            labels[view] = self._labels(market, VIEW_NAMESPACES[view], keys, period)
        if listed is None:
            return _Named(states, labels, {}, {}, CampaignAudiences(published=False, rows=()))
        # In rank order, so the opaque references number the shown campaigns by rank.
        campaigns = listed.shown
        segments = {
            str(key[1])
            for campaign in campaigns
            for metric in CAMPAIGN_METRICS
            for key in answers[(market, f"campaign:{campaign}", metric)].rows
        }
        # Sorted keys, so chat's placeholders number the same campaigns the same way.
        names = self.campaign_labels(tuple(sorted(campaigns)), period.window) if campaigns else {}
        segment_labels = self._labels(market, SegmentNamespace.AMC_DSP_BEHAVIOR, segments, period)
        rows = replace(
            _campaign_rows(market, campaigns, answers, (names, segment_labels)),
            shown=len(campaigns),
            listed=listed.total,
        )
        return _Named(states, labels, names, segment_labels, rows)

    def _market(
        self,
        market: str,
        period: CalendarPeriod,
        measured: tuple[_Served, dict[tuple[str, str], Roster], _Named, str],
    ) -> AudienceMarket:
        served, rosters, named, revision = measured
        views: list[ProfileView] = []
        for view in (PAID_VIEW, DSP_VIEW):
            states = named.states[view]
            labels = named.labels[view]
            paid = (
                PaidState.STANDARD
                if view == DSP_VIEW
                else PaidState.OBSERVED
                if any(
                    served.paid[PROFILE_CONTRACTS[path]].feature_observed
                    for path in DELIVERY_PATHS[PAID_VIEW]
                )
                else PaidState.NOT_OBSERVED
            )
            # One card per delivery: each names its own path's segments against its own base.
            insights = tuple(
                segment_over_index_slot(
                    OverIndexSlotRequest(
                        view,
                        item.path,
                        period.start,
                        item.delivery,
                        ()
                        if item.delivery is None
                        else tuple(
                            finding
                            for finding in over_index_findings(item.delivery, self._policy)
                            if self._material(finding, period)
                        ),
                        labels,
                        self._policy,
                        revision,
                        _SILENT,
                        # An unobserved paid profile is the paid callout, never a monthly gap.
                        no_delivery_text=ASI_NEEDS_PAID_FEATURE_TEXT
                        if paid is PaidState.NOT_OBSERVED
                        else NO_DELIVERY_TEXT,
                    )
                )
                for item in states
            )
            views.append(
                build_view(
                    ViewRequest(
                        view,
                        paid,
                        states,
                        {
                            path: rosters[(market, path)].keys
                            for path in DELIVERY_PATHS[view]
                            if (market, path) in rosters
                        },
                        labels,
                        self._policy,
                        insights,
                    )
                )
            )
        paid_view, standard_view = views
        return AudienceMarket(market, paid_view, standard_view, named.campaigns)

    def _delivery_state(  # noqa: PLR0913, PLR0917 -- a marketplace, a path, its evidence, its month
        self,
        market: str,
        path: str,
        measured: tuple[_Served, dict[_Label, GroupedAnswer], dict[tuple[str, str], Roster]],
        period: CalendarPeriod,
    ) -> DeliveryState:
        served, answers, rosters = measured
        contract_id = PROFILE_CONTRACTS[path]
        paid_state = served.paid.get(contract_id)
        if period.window not in served.published[contract_id]:
            return DeliveryState(path, None, paid_state)
        figures = tuple(answers[(market, path, metric)].rows for metric in SEGMENT_METRICS)
        keys = sorted({key for rows in figures for key in rows}, key=lambda key: str(key[1]))
        # A segment one figure carries and another lacks had no measured zero (figures.py drops
        # a not_returned cell): it's listed not available, never read as 0.
        whole = [key for key in keys if all(rows.get(key) is not None for rows in figures)]
        partial = tuple(str(key[1]) for key in keys if key not in whole)
        segments = tuple(
            ProfileSegment(str(key[1]), *(int(cast("Decimal", rows[key])) for rows in figures))
            for key in whole
        )
        totals = tuple(
            next(iter(answers[(market, path, metric)].rows.values()), None)
            for metric in TOTAL_METRICS
        )
        exposed_total, buyers_total = (None if value is None else int(value) for value in totals)
        if exposed_total is None or buyers_total is None:
            exposed_total = buyers_total = None
        roster = rosters[(market, path)]
        outcome = apply_roster(roster, (item.segment_id for item in segments), None)
        delivery = ProfileDelivery(
            path, self._namespace(path), segments, exposed_total, buyers_total
        )
        return DeliveryState(
            path,
            delivery,
            paid_state,
            first_month=roster.first_period,
            roster_note=outcome.note,
            partial=partial,
        )

    def _material(self, finding: OverIndexFinding, period: CalendarPeriod) -> bool:
        """Whether a named segment clears `segment-over-index`'s materiality (rate points)."""
        difference = (finding.buyer_share - finding.exposed_share).quantize(Decimal("0.0001"))
        (bound,) = predicate_findings(
            (DeterministicFinding("Buyer share minus reached share", difference),),
            InsightFindingKind.ABSOLUTE_CHANGE,
            InsightEvidenceInput.SEGMENT_PROFILE_ROWS,
            source=FindingSource(
                InsightSlotId.SEGMENT_OVER_INDEX,
                OperandBasis.SEGMENT_DELIVERY,
                "segment_buyer_share",
            ),
            priors={"Buyer share minus reached share": finding.exposed_share},
        )
        return finding_is_material(
            self.insight_contract.predicate.materiality,
            bound,
            horizon_role_for(period.window.days),
        )


def _campaign_base() -> dict[str, str]:
    """Pack 8's campaign-grain selection: DSP behaviour segments only."""
    return {
        _GRAIN_SET_SELECTOR: "campaign",
        SEGMENT_NAMESPACE_COLUMN: SegmentNamespace.AMC_DSP_BEHAVIOR.value,
    }


def _currency(answer: GroupedAnswer) -> str | None:
    return None if answer.units is None else answer.units.currency_code


def _campaign_rows(  # noqa: PLR0913, PLR0917 -- a marketplace, its campaigns, answers, names
    market: str,
    campaigns: tuple[str, ...],
    answers: dict[_Label, GroupedAnswer],
    names: tuple[dict[str, str], dict[str, SegmentLabel]],
) -> CampaignAudiences:
    """Reduce a published month's campaigns to their segment rows, under the names read."""
    campaign_names, labels = names
    if any(campaign not in campaign_names for campaign in campaigns):
        # The resolver names every protected value, so a gap is a broken port, never a key.
        raise RuntimeError("every Audiences campaign needs its governed name or placeholder.")
    rows: list[CampaignSegment] = []
    for campaign in campaigns:
        impressions, spend, revenue = (
            answers[(market, f"campaign:{campaign}", metric)] for metric in CAMPAIGN_METRICS
        )
        # A segment any of the three answers returns is a row: a figure another answer
        # lacks is withheld with its own reason (`campaign_segment`), never dropped.
        returned = {*impressions.rows, *spend.rows, *revenue.rows}
        for key in sorted(returned, key=lambda item: str(item[1])):
            segment = str(key[1])
            rows.append(
                campaign_segment(
                    CampaignRowRequest(
                        (campaign, campaign_names[campaign]),
                        segment,
                        labels.get(segment),
                        impressions.rows.get(key),
                        (spend.rows.get(key), _currency(spend)),
                        (revenue.rows.get(key), _currency(revenue)),
                    )
                )
            )
    return CampaignAudiences(published=True, rows=tuple(rows))


_RENDER_INPUTS_IDENTITY_PREFIX = "audiences-render-inputs.v2:"
"""Names the digest of every non-measurement read an Audiences body renders.

v2 adds each paid contract's derived state and the campaign publication flag."""


def _label_payload(labels: dict[str, SegmentLabel]) -> dict[str, list[str | None]]:
    return {
        key: [
            label.name,
            None if label.provider is None else label.provider.value,
            label.category_level_1,
            label.category_path,
        ]
        for key, label in sorted(labels.items())
    }


def _paid_payload(paid: dict[str, PaidFeatureState]) -> dict[str, list[object]]:
    return {
        contract_id: [
            state.feature_observed,
            state.period_published,
            state.expected_after.isoformat(),
            None
            if state.latest_available_period is None
            else [
                state.latest_available_period.start.isoformat(),
                state.latest_available_period.end.isoformat(),
            ],
        ]
        for contract_id, state in sorted(paid.items())
    }


def _render_inputs_identity(
    rosters: dict[tuple[str, str], Roster],
    named: dict[str, _Named],
    served: dict[str, _Served],
) -> str:
    """Digest the rosters, labels, names, paid states and campaign publication a bake rendered.

    None of them is a measured publication, yet each changes the body, so a change to any of
    them must advance the revision rather than leave a stale body current. Paid states come from
    publication history outside the month's plans, and a month pack 8 published with no
    campaign plans nothing, so neither reaches the measured identities.
    """
    evidence = {
        "rosters": [
            [market, name, list(roster.keys), roster.first_period]
            for (market, name), roster in sorted(rosters.items())
        ],
        "markets": {
            market: {
                "labels": {view: _label_payload(labels) for view, labels in item.labels.items()},
                "campaign_names": dict(sorted(item.campaign_names.items())),
                "campaign_labels": _label_payload(item.campaign_labels),
                "campaigns_published": item.campaigns.published,
                "campaigns_listed": item.campaigns.listed,
                "paid": _paid_payload(served[market].paid),
            }
            for market, item in sorted(named.items())
        },
    }
    digest = hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return f"{_RENDER_INPUTS_IDENTITY_PREFIX}{digest}"


def _parsed(
    slots: tuple[PresentationMeasurementAnswerSlot, ...], batch: _Batch, period: CalendarPeriod
) -> dict[_Label, GroupedAnswer] | None:
    """Parse every answer; a partial, truncated or mis-pinned one bakes nothing."""
    parsed: dict[_Label, GroupedAnswer] = {}
    for answer, label in zip(slots, batch.labels, strict=True):
        grains = TOTAL_GRAINS if label[2] in TOTAL_METRICS else SEGMENT_GRAINS
        item = parse_grouped_answer(answer, period.window, grains)
        if item is None or not grouped_answer_complete(item, period.window):
            # Every month planned has a CURRENT publication, so anything short of a complete
            # answer is missing evidence: no bake, never an undercount.
            return None
        if any(key[0] != label[0] for key in item.rows):
            return None
        parsed[label] = item
    return parsed


AUDIENCES_ROW_MAX_BYTES = 800
"""A ceiling on one profile or campaign row's encoded bytes; a test holds wide rows within it.

A row past it (a segment name of several hundred characters) is still refused by name: the
source also checks the encoded body against the artifact cap."""
AUDIENCES_ENVELOPE_MAX_BYTES = 16_000
"""A ceiling on one marketplace's encoded bytes outside its rows; a test holds it."""


def _body_rows(payload: dict[str, object]) -> int:
    """Count every profile row and campaign row a body carries."""
    markets = cast("list[dict[str, Any]]", payload["markets"])
    return sum(len(view["rows"]) for market in markets for view in market["views"]) + sum(
        len(market["campaign_audiences"]["rows"]) for market in markets
    )


@dataclass(frozen=True, slots=True)
class RetainedAudiencesPresetArtifactSource:
    """Compile the latest complete month's Audiences artifact from retained truth only."""

    compiler: AudiencesCompiler
    served: Callable[[], frozenset[tuple[str, CalendarPeriod]]]
    """Every (contract, month) packs 10-12 have a CURRENT publication for, any marketplace."""
    pointers: PresetArtifactPointerStorePort
    clock: Callable[[], datetime]
    row_budget: int
    """The most profile and campaign rows one body carries (`audiences_row_budget`)."""
    page: PresetArtifactPage = field(default_factory=lambda: PresetArtifactPage(AUDIENCES_PAGE_ID))

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return a complete body or a typed unavailable body for the newest target month."""
        if variant.value != LATEST_MONTH_VARIANT:
            return None
        resolved = self.resolve()
        if resolved is None:
            return None
        rendered = self.compiler.render_audiences(resolved)
        if rendered.artifact is None:
            return None
        payload = audiences_artifact_payload(rendered.artifact)
        encoded = canonical_preset_artifact_json(payload)
        over_bound = (
            _body_rows(payload) > self.row_budget
            or len(encoded.encode()) > MAX_PRESET_ARTIFACT_BYTES
        )
        identities = rendered.publication_identities
        if over_bound:
            window = rendered.artifact.month.window
            identities = (
                *identities,
                f"{AUDIENCES_BODY_OVER_BOUND}:{window.start.isoformat()}:{window.end.isoformat()}",
            )
            payload = audiences_unavailable_artifact_payload(rendered.artifact.month)
            encoded = canonical_preset_artifact_json(payload)
        revision = PresetArtifactRevision.from_publication_identities(
            identities, render_format=AUDIENCES_RENDER_FORMAT
        )
        current = self.pointers.current(self.page, variant)
        if current is not None and current.revision == revision:
            return SourceUnchanged(revision)
        body = PresetArtifactBody(self.page, variant, revision, encoded)
        pointer = PresetArtifactPointer(
            self.page,
            variant,
            revision,
            body.digest,
            1 if current is None else current.forward_token + 1,
            None if current is None else current.revision,
            None,
            resolved_window=rendered.artifact.month.window,
        )
        return PresetArtifactPublicationRequest(
            body,
            PresetArtifactPointerAdvanceRequest(
                pointer,
                None if current is None else self.clock() + SUPERSEDED_HISTORY_RETENTION,
            ),
            None,
        )

    def prefetch(self, variant: PresetArtifactVariant) -> None:
        """Prefetch the newest target month's CURRENT reads (plan A5.1); renders nothing."""
        if variant.value != LATEST_MONTH_VARIANT:
            return
        resolved = self.resolve()
        prefetch = getattr(self.compiler, "prefetch_audiences", None)
        if resolved is not None and prefetch is not None:
            prefetch(resolved)

    def resolve(self) -> AudiencesRenderInput | None:
        """Return the latest complete month any buyer-profile path has published."""
        months = sorted(
            period
            for contract_id, period in self.served()
            if contract_id in PROFILE_CONTRACTS.values() and period.grain is TimeGrain.MONTH
        )
        return AudiencesRenderInput(months[-1]) if months else None


AUDIENCES_TEMPORAL_POLICY = PresentationTemporalPolicy(
    candidate_presets=(TemporalPresetId.LAST_COMPLETE_MONTH,),
    default_preset=TemporalPresetId.LAST_COMPLETE_MONTH,
    custom_enabled=False,
    custom_min_duration_days=1,
    custom_max_duration_days=90,
    comparison_policy=TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD,
    display_grains=(PageTime.DECLARED_PERIOD,),
)
"""The LLD's `temporal_policy` (docs/ui/audiences §6 "Page declarations"), whole."""
_MONTH_PERIODS = (TemporalPresetId.LAST_COMPLETE_MONTH,)
AUDIENCES_SECTIONS = (
    PageSection("workspace-scope", Cardinality.EXACTLY_ONE),
    PageSection("profile-path", Cardinality.EXACTLY_ONE),
    PageSection("segment-ranking", Cardinality.EXACTLY_ONE, _MONTH_PERIODS, period_anchor=True),
    PageSection("audience-insights", Cardinality.EXACTLY_ONE),
    PageSection("campaign-audiences", Cardinality.EXACTLY_ONE, _MONTH_PERIODS, period_anchor=False),
    PageSection("evidence-trace", Cardinality.EXACTLY_ONE),
    PageSection("boundary", Cardinality.EXACTLY_ONE),
)
"""The LLD's sections in order: `segment-ranking` the only anchor, both bound to the month."""


def audiences_marketplace_count(root: Path) -> int:
    """Count the governed marketplaces the Audiences bake renders."""
    return len(
        {
            mapping.target.value
            for mapping in load_commerce_identity_bridge(root).marketplace_mappings
        }
    )


def audiences_bake_phase_plan_counts(marketplaces: int, campaigns: int) -> tuple[int, int, int]:
    """Return list, roster and measurement query counts in their execution-barrier order."""
    profile_rosters = len(PROFILE_CONTRACTS)
    profile_measurements = len(PROFILE_CONTRACTS) * (len(SEGMENT_METRICS) + len(TOTAL_METRICS))
    return (
        marketplaces,
        marketplaces * (profile_rosters + campaigns),
        marketplaces * (profile_measurements + campaigns * len(CAMPAIGN_METRICS)),
    )


def audiences_bake_plan_count(marketplaces: int, campaigns: int) -> int:
    """Count one bake's queries with `campaigns` shown per marketplace."""
    return sum(audiences_bake_phase_plan_counts(marketplaces, campaigns))


def audiences_policy_declared(schemas: PresentationPageSchemaSet) -> bool:
    """Whether the `audiences` v2 page entry declares exactly this bake.

    The page id alone doesn't open the source: the entry must declare every server-relevant
    part of the LLD's v2 entry (docs/ui/audiences §6): `mode: selectable` with the boundary
    required, exactly `AUDIENCES_TEMPORAL_POLICY`, exactly the artifact this renderer writes
    (`audiences`, `AudienceProfileArtifact` v1, its three outcomes) and exactly
    `AUDIENCES_SECTIONS` with their cardinalities, declared periods and the one anchor. The
    page copy (purpose, primary job) and the navigation block are the UI's. Any other
    declaration keeps the page unavailable.
    """
    page = next((item for item in schemas.pages if item.page_id == AUDIENCES_PAGE_ID), None)
    return page is not None and _declares_this_bake(page)


def _declares_this_bake(page: PresentationPageSchema) -> bool:
    if (
        page.mode is not PresentationPageMode.SELECTABLE
        or page.boundary_required is not True
        or page.time_policy != AUDIENCES_TEMPORAL_POLICY
        or page.sections != AUDIENCES_SECTIONS
        or len(page.artifacts) != 1
    ):
        return False
    (artifact,) = page.artifacts
    return (
        artifact.artifact_id == _ARTIFACT_ID
        and artifact.artifact_type == AUDIENCES_ARTIFACT_TYPE
        and artifact.version == AUDIENCES_ARTIFACT_VERSION
        and tuple(sorted(artifact.outcomes)) == ("complete", "empty", "insufficient")
    )


def _contract(slot: InsightSlotId) -> InsightContract:
    for _, contract in load_insight_contracts(contracts_root()):
        if contract.id is slot:
            return contract
    raise RuntimeError(f"the {slot.value} insight contract is undeclared.")


AUDIENCES_MATERIALIZATION_UNAVAILABLE = "audiences_materialization_unavailable"
"""The generated reason while the retained-truth configuration or the v2 page entry is absent."""


def retained_audiences_preset_artifact_source(  # noqa: PLR0913, PLR0917 -- the closed signature
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort,
    current: BakeSourceReads,
) -> RetainedAudiencesPresetArtifactSource | None:
    """Compose the retained Audiences source, or none, logging the closure condition once.

    The closed factory signature (A5.4): the invocation's admitted, captured `queries` and its
    one pinnable CURRENT reader, whose clock every renderer reads. Fails closed without the
    retained-truth configuration, while the v2 page entry `audiences` isn't declared, and when
    configuration can't satisfy the served one-market contract. The baker maps None to the
    generated `audiences_materialization_unavailable`.
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
        return source_closed(AUDIENCES_PAGE_ID, "settings_incomplete")
    root = contracts_root()
    if not audiences_policy_declared(load_v2_page_schemas(root)):
        return source_closed(AUDIENCES_PAGE_ID, "page_entry_absent")
    if audiences_marketplace_count(root) != 1:
        return source_closed(AUDIENCES_PAGE_ID, "audiences_marketplace_cardinality_unavailable")
    return _composed(settings, pointers, queries, current)


def _composed(  # noqa: PLR0913, PLR0917 -- the closed factory inputs
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort,
    current: BakeSourceReads,
) -> RetainedAudiencesPresetArtifactSource:
    """Wire the renderer to the deployed stores; reached only once the page entry exists."""
    from adapters.jobs.preset_artifact_publisher.bakeability import (  # noqa: PLC0415
        audiences_row_budget,
        audiences_runtime_campaign_bound,
    )
    from adapters.outbound.entity_catalog import (  # noqa: PLC0415
        CanonicalEntityCatalog,
        EntityCatalogTooling,
    )
    from adapters.outbound.media_taxonomy import load_media_taxonomy  # noqa: PLC0415
    from adapters.outbound.metric_dictionary import load_catalog  # noqa: PLC0415
    from adapters.outbound.publication_selection import (  # noqa: PLC0415
        PerPackPresentationEvidenceInspection,
        PublicationSelector,
    )
    from adapters.outbound.s3.semantic_storage import S3CanonicalAdapter  # noqa: PLC0415
    from adapters.outbound.segment_labels import (  # noqa: PLC0415
        BakePinnedSegmentLabels,
        CanonicalSegmentLabels,
    )
    from adapters.outbound.segment_roster import SegmentRosterReader  # noqa: PLC0415
    from adapters.outbound.source_pack import load_source_packs  # noqa: PLC0415
    from adapters.producer.packs import load_query_packs  # noqa: PLC0415

    root = contracts_root()
    contracts = tuple(contract for _, contract in load_contracts(root))
    source_packs = load_source_packs(root)
    bridge = load_commerce_identity_bridge(root)
    horizons = scope_horizon_policy(current)
    reader = cast("CurrentPublicationInspectionPort", current)
    lags = {pack.contract_id: pack.effective_cadence_lag_days for pack in load_query_packs(root)}
    selector = PublicationSelector(
        reader, source_packs, bridge, cadence_lag_days=lags, horizons=horizons
    )
    marketplaces = tuple(sorted({mapping.target.value for mapping in bridge.marketplace_mappings}))
    catalog = load_catalog(root)
    canonical = S3CanonicalAdapter(settings.semantic_v2_bucket)
    # Pinned on the empty-answer pass too, so the task's render reads only pinned scopes.
    labels = BakePinnedSegmentLabels(
        CanonicalSegmentLabels(reader, canonical, source_packs, contracts, bridge)
    )
    # Chat's resolver over the same governed catalog, so a campaign reads by its governed name
    # or chat's "Unnamed campaign N", never by its key.
    campaign_names = governed_campaign_names(
        DimensionLabelResolver(
            CanonicalEntityCatalog(
                EntityCatalogTooling(
                    current=reader,
                    canonical=canonical,
                    packs=source_packs,
                    contracts=contracts,
                    bridge=bridge,
                    taxonomy=load_media_taxonomy(root),
                )
            ),
            AuthorizedAnalyticalScope(frozenset({bridge.commerce_scope})),
        )
    )
    anchoring = tuple(item for item in contracts if item.contract_id in PROFILE_CONTRACTS.values())

    def served() -> frozenset[tuple[str, CalendarPeriod]]:
        return frozenset(
            item
            for market in marketplaces
            for item in selector.served_periods(
                ServedPeriodsRequest(anchoring, {MARKETPLACE_SELECTOR: market}, current.clock())
            ).publications
        )

    compiler = AudiencesArtifactRenderer(
        catalog=catalog,
        contracts=contracts,
        evidence=PerPackPresentationEvidenceInspection(reader, source_packs, bridge),
        tooling=MeasurementTooling(
            queries=queries,
            publications=selector,
            query_batch_max_concurrency=settings.athena_query_batch_max_concurrency,
            periods=selector,
        ),
        rosters=SegmentRosterReader(
            catalog=catalog,
            contracts=contracts,
            publications=selector,
            periods=selector,
            queries=queries,
            labels=labels,
            clock=current.clock,
        ),
        segment_labels=labels,
        marketplaces=marketplaces,
        insight_contract=_contract(InsightSlotId.SEGMENT_OVER_INDEX),
        clock=current.clock,
        campaign_labels=campaign_names,
        # One source with the certifier, so the runtime never outruns the certified count.
        campaign_bound=audiences_runtime_campaign_bound(len(marketplaces)),
        cadence_lag_days=lags,
    )
    return RetainedAudiencesPresetArtifactSource(
        compiler=compiler,
        served=served,
        pointers=pointers,
        clock=current.clock,
        row_budget=audiences_row_budget(len(marketplaces)),
    )
