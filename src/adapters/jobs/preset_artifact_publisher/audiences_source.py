# SPDX-License-Identifier: MIT-0
"""Bake `AudienceProfileArtifact` v2 and its parts for the latest complete month (ADR-075, ADR-141).

Per governed marketplace, every figure is planned through the measurement layer, the path chat
reads too: for each profile path with a CURRENT delivery for the month (packs 10, 11 and 12),
its segment rows' buyers, reached shoppers and new-to-brand buyers by segment, graded against the
path's segment roster (the segments it returned the month before, read through the roster port),
and its `profiled_total` row's two bases; for pack 8, every DSP campaign's segment impressions,
spend and attributed sales, read once for all campaigns grouped by campaign and streamed one
campaign at a time (`audiences_campaigns`), each campaign graded against its own segments of the
month before. Nothing is planned across segments, paths or campaigns. The domain computes every
share, index, CPM and ROAS (`domain.presentation.audiences`), so a missing total reads "Not
available" rather than a share of a partial base.

A bake reads in two phases, each run at most the configured query bound at once: each profile
path's roster with the campaigns' count pass, then the profile measurement with the history walk
for campaigns that returned no segment the month before. The statement count doesn't grow with
campaigns (24 per marketplace). The parent carries each path's first 50 rows and the first
campaign index shard; every other page is a part, and the campaign pages are rendered as the
stage writes them (the page pass), so a bake holds one campaign's rows at a time.

Paid states (ADR-107) come from publication evidence over each paid pack's latest three months:
packs 10-11 unobserved leave the paid view's callout and default the page to the DSP view; pack
13 unobserved leaves ASI segments named by ID with "name not available yet".

Registered with the publisher, and fails closed: the source composes only once the v2 page entry
`audiences` declares the LLD's whole server-relevant entry (`audiences_policy_declared`: mode,
boundary, temporal policy, `AudienceProfileArtifact` v2 and the seven sections with their periods
and anchor, as `docs/ui/audiences/low-level-design.md` declares them).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, is_dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from adapters.campaign_insight_requests import FindingSource, predicate_findings
from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.inbound.tool_handlers.measurement import (
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
from adapters.jobs.preset_artifact_publisher.audiences_campaigns import (
    CAMPAIGN_KEY_COLUMNS,
    CampaignCount,
    CampaignCountScope,
    CampaignPages,
    CampaignTally,
    count_campaigns,
)
from adapters.jobs.preset_artifact_publisher.campaign_source import (
    SUPERSEDED_HISTORY_RETENTION,
)
from adapters.jobs.preset_artifact_publisher.group_stream import (
    GroupRead,
    GroupStreams,
    SinkSlot,
    Unphased,
)
from adapters.jobs.preset_artifact_publisher.isolated_refusals import (
    capped_preset_artifact_json,
    note_isolated_refusal,
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
    ROSTER_REFUSED,
    Roster,
    RosterKind,
    apply_roster,
    roster_filters,
)
from domain.operations.bake_refusals import IsolatedRefusal, IsolatedRefusalReason, RefusalScope
from domain.presentation.audiences import (
    AUDIENCES_ARTIFACT_TYPE,
    AUDIENCES_ARTIFACT_VERSION,
    AUDIENCES_PAGE_ID,
    CAMPAIGN_HISTORY_GAP_TEXT,
    CAMPAIGN_INCOMPLETE_TEXT,
    DELIVERY_PATHS,
    DSP_VIEW,
    NO_DELIVERY_TEXT,
    PAID_VIEW,
    PROFILE_PATH_REFUSED_TEXTS,
    VIEW_NAMESPACES,
    AudienceMarket,
    AudienceProfileArtifact,
    CampaignAudiences,
    CampaignEntryState,
    CampaignIndexEntry,
    DeliveryState,
    OverIndexFinding,
    OverIndexSlotRequest,
    PaidState,
    ProfileDelivery,
    ProfileSegment,
    ProfileView,
    ViewRequest,
    audiences_artifact_payload,
    audiences_campaign_index_payload,
    audiences_campaign_page_payload,
    audiences_profile_page_payload,
    build_view,
    over_index_findings,
    profile_page_count,
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
    PresetArtifactBody,
    PresetArtifactCustodyHeader,
    PresetArtifactPage,
    PresetArtifactPointerTemplate,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingIdentity,
    PresetArtifactSiblingPair,
    PresetArtifactSiblingStream,
    PresetArtifactVariant,
    SourceUnchanged,
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
from ports.outbound.query import (
    BAKE_MAX_ROWS,
    BAKE_MAX_STREAMED_ROWS,
    FoldRead,
    QueryResult,
    RowCeiling,
)
from ports.outbound.rosters import (
    CampaignHistories,
    CampaignHistoriesRequest,
    CampaignHistory,
    CampaignHistoryPort,
    RosterRequest,
)
from ports.outbound.segment_labels import SegmentLabelRequest
from ports.outbound.semantic_state import scope_horizon_policy
from ports.outbound.served_periods import ServedPeriodsRequest

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

    from adapters.config import PresetArtifactBakeSettings
    from adapters.inbound.tool_handlers.measurement import PresentationMeasurementAnswerSlot
    from adapters.outbound.bake_sources import BakeSourceReads
    from domain.catalog.capability import Catalog
    from domain.data_contracts.contract import DataContract
    from domain.presentation.page_schemas import PresentationPageSchema, PresentationPageSchemaSet
    from domain.semantics.audience_segments import SegmentLabel
    from ports.outbound.preset_artifacts import PresetArtifactPointerStorePort
    from ports.outbound.publication_selection import PresentationEvidenceInspectionPort
    from ports.outbound.query import FoldingQueryPort, PeriodSeriesQueryPort
    from ports.outbound.rosters import RosterPort
    from ports.outbound.segment_labels import SegmentLabelPort
    from ports.outbound.semantic_state import CurrentPublicationInspectionPort

__all__ = [
    "AUDIENCES_ENVELOPE_MAX_BYTES",
    "AUDIENCES_MATERIALIZATION_UNAVAILABLE",
    "AUDIENCES_RENDER_FORMAT",
    "AUDIENCES_ROW_MAX_BYTES",
    "AUDIENCES_SECTIONS",
    "AUDIENCES_TEMPORAL_POLICY",
    "AudiencesArtifactRenderer",
    "AudiencesRenderInput",
    "AudiencesRenderResult",
    "CampaignPagePass",
    "RetainedAudiencesPresetArtifactSource",
    "audiences_marketplace_count",
    "audiences_policy_declared",
    "governed_campaign_names",
    "retained_audiences_preset_artifact_source",
]

AUDIENCES_RENDER_FORMAT = 4
"""Generation 4: v2, every DSP campaign in paged parts (ADR-141). Generation 3 added the
campaigns' `shown_text`, generation 2 wire fields, the pointer's `resolved_window` and the
render-inputs identity; an older pointer is rebuilt, never kept."""
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
CAMPAIGN_ROSTER_METRIC = "segment_reach"
"""R1: every returned segment row carries its reach, so its keys are the month's segments."""
CAMPAIGN_STREAMED_READS = len(CAMPAIGN_METRICS) + 1
"""The count pass merges the three figures and R1 at once (`AudiencesArtifactRenderer`)."""
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
_PROFILE_SECTION = "segment-ranking"
_CAMPAIGN_SECTION = "campaign-audiences"
_INDEX_FILTER = "campaign-index"
_INDEX_ORDERING = "rank"
_PROFILE_ORDERING = "index-desc"
_CAMPAIGN_ORDERING = "impressions-desc"

type _Label = tuple[str, str, str]
"""(marketplace, profile path, metric)."""


@dataclass(frozen=True, slots=True)
class AudiencesRenderInput:
    """One resolved complete calendar month."""

    period: CalendarPeriod


@dataclass(frozen=True, slots=True)
class AudiencesRenderResult:
    """The baked parent, or None; the publication identities it read; its campaign pages."""

    artifact: AudienceProfileArtifact | None
    publication_identities: tuple[str, ...]
    campaigns: CampaignPagePass | None = None
    """The page pass over every listed campaign, started only when the publication is staged."""


_EMPTY_RESULT = QueryResult(
    rows=(), bytes_scanned=0, truncated=False, suppression=Suppression(rows_suppressed=False)
)
_EMPTY_FOLD = FoldRead(
    rows_read=0, truncated=False, bytes_scanned=0, suppression=Suppression(rows_suppressed=False)
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

    def fold(self, request: object, sink: object) -> FoldRead:
        del request, sink
        return _EMPTY_FOLD

    def fold_series(self, request: object, sink: object) -> FoldRead:
        del request, sink
        return _EMPTY_FOLD


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
class _Scope:
    """One graded profile path of a marketplace."""

    market: str
    name: str
    """The profile path."""
    fact: str
    filters: dict[str, str]


@dataclass(frozen=True, slots=True)
class _PathRefusal:
    """A profile path refused for its own reason (an item of `segment-ranking`, 01 §3.3)."""

    reason: IsolatedRefusalReason


_PATH_REFUSALS: Final = {
    "history_gap": IsolatedRefusalReason.HISTORY_GAP,
    "unreadable": IsolatedRefusalReason.EVIDENCE_INCOMPLETE,
}
"""A profile roster's `roster_refusal` that refuses only its path; any other refuses the page."""


@dataclass(frozen=True, slots=True)
class _Counted:
    """One marketplace's campaign count pass: its reads, their tallies, their publications."""

    reads: tuple[GroupRead, ...]
    count: CampaignCount
    identities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Named:
    """One marketplace's delivery states and every non-measurement read its body renders."""

    states: dict[str, tuple[DeliveryState, ...]]
    """Each view's delivery states, by view."""
    labels: dict[str, dict[str, SegmentLabel]]
    """Each view's segment labels, by view; the DSP view's name the campaign rows' too."""
    campaign_names: dict[str, str]
    campaigns: CampaignAudiences
    keys: dict[str, str]
    """Each index entry's campaign key, by its reference."""


@dataclass(frozen=True, slots=True)
class CampaignPagePass:
    """What the page pass reads and renders: the count pass's figure reads and index."""

    reads: tuple[GroupRead, ...]
    """Impressions, spend and attributed sales, as the count pass prepared them."""
    tooling: MeasurementExecutionTooling
    entries: dict[str, CampaignIndexEntry]
    count: CampaignCount
    labels: dict[str, SegmentLabel]

    def start(self) -> CampaignPages:
        """Start the page pass and return once each read's statement has executed.

        Called inside the render, so every statement runs under the render's deadline and
        certified bound; the stage then drains the rows.
        """
        streams = GroupStreams(self.reads, self.tooling, CAMPAIGN_KEY_COLUMNS)
        streams.start()
        streams.wait_started()
        return CampaignPages(streams, self.entries, self.count, self.labels.get)


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


def _prior_month(period: CalendarPeriod) -> CalendarPeriod:
    return period_containing(TimeGrain.MONTH, period.start - timedelta(days=1))


@dataclass(frozen=True, slots=True)
class AudiencesArtifactRenderer:
    """Plan packs 10-12 and 8 per marketplace and reduce them in the domain."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    evidence: PresentationEvidenceInspectionPort
    tooling: MeasurementTooling
    rosters: RosterPort
    histories: CampaignHistoryPort
    segment_labels: SegmentLabelPort
    marketplaces: tuple[str, ...]
    insight_contract: InsightContract
    """`segment-over-index` v1."""
    clock: Callable[[], datetime]
    campaign_labels: Callable[[tuple[str, ...], DateRange], dict[str, str]]
    """Each DSP campaign's governed public label, or chat's placeholder (`governed_campaign_names`).

    No default: a campaign key is a protected identifier, so it never names itself."""
    cadence_lag_days: dict[str, int] = field(default_factory=dict[str, int])
    """Each paid pack's declared lag (ADR-110), for its ADR-107 `expected_after`."""
    prefetch_campaign: str | None = None
    """Set only on the prefetch walker: one synthetic listed campaign per marketplace."""

    def __post_init__(self) -> None:
        if self.insight_contract.id is not InsightSlotId.SEGMENT_OVER_INDEX:
            raise ValueError("Audiences renders segment-over-index.")
        if len(self.marketplaces) != 1:
            raise ValueError("Audiences bakes exactly one governed marketplace.")
        if self.tooling.query_batch_max_concurrency < CAMPAIGN_STREAMED_READS:
            # Each campaign read holds its admission slot while it streams, and the merge needs
            # all of them at once (group_stream).
            raise ValueError("Audiences streams its campaign reads at once: raise the query bound.")

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
            max_rows=BAKE_MAX_ROWS,
            row_ceiling=RowCeiling.BAKE,
            rosters=() if roster is None else (roster,),
        )

    def campaign_requests(
        self, market: str, period: CalendarPeriod, prior: CalendarPeriod | None
    ) -> tuple[MeasurementToolRequest, ...]:
        """Plan the campaign reads, each for every campaign at `CAMPAIGN_GRAINS` (01 §3.1).

        Pack 8's three figures for the month, then R1 (the segments each campaign returned the
        month before) when `prior` is published. Each is ungraded (an empty key-read roster) and
        streamed: its reduction is a fresh `SinkSlot`, which each pass fills with its own sink.
        """
        platform = self._contract(DELIVERY_CONTRACT).platform_key
        reads = [(metric, period) for metric in CAMPAIGN_METRICS]
        if prior is not None:
            reads.append((CAMPAIGN_ROSTER_METRIC, prior))
        return tuple(
            replace(
                self._request(
                    market,
                    metric,
                    CAMPAIGN_GRAINS,
                    (month, _campaign_base()),
                    Roster(RosterKind.SEGMENT, month.window, market, platform, ()),
                ),
                max_rows=BAKE_MAX_STREAMED_ROWS,
                row_ceiling=RowCeiling.BAKE_STREAMED,
                reduction=SinkSlot(BAKE_MAX_ROWS),
            )
            for metric, month in reads
        )

    def _roster(self, period: CalendarPeriod, scope: _Scope) -> Roster | _PathRefusal:
        """Read one profile path's roster once per bake; its own refusal refuses only the path."""
        contract = next(item for item in self.contracts if item.target_table == scope.fact)
        try:
            return self.rosters.roster(
                RosterRequest(
                    kind=RosterKind.SEGMENT,
                    period=period.window,
                    marketplace_key=scope.market,
                    platform_key=contract.platform_key,
                    filters=roster_filters(RosterKind.SEGMENT, scope.filters),
                    fact=scope.fact,
                    ceiling=RowCeiling.BAKE,
                )
            )
        except DomainError as error:
            reason = _PATH_REFUSALS.get(str(error.context.get("roster_refusal")))
            if error.code != ROSTER_REFUSED or reason is None:
                raise
            return _PathRefusal(reason)

    def _read(self, request: MeasurementToolRequest) -> tuple[GroupRead, tuple[str, ...]]:
        """Prepare and inspect one streamed read; return it and the publications it binds."""
        prepared = prepare_batch((request,))
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
        identities = tuple(
            publication.publication_id.value
            for slot in inspection.slots
            if slot.bound is not None
            for publication in slot.bound.all_publications
        )
        return GroupRead(prepared, inspection, cast("SinkSlot", request.reduction)), identities

    def _count(self, market: str, period: CalendarPeriod, prior: CalendarPeriod | None) -> _Counted:
        """Run the count pass: every campaign's tally from the three figures and R1."""
        reads, identities = zip(
            *(self._read(request) for request in self.campaign_requests(market, period, prior)),
            strict=True,
        )
        streams = GroupStreams(
            reads,
            MeasurementExecutionTooling(
                queries=self.tooling.queries,
                query_batch_max_concurrency=self.tooling.query_batch_max_concurrency,
            ),
            CAMPAIGN_KEY_COLUMNS,
        )
        platform = self._contract(DELIVERY_CONTRACT).platform_key
        count = count_campaigns(
            streams,
            CampaignCountScope(
                market, platform, (period.window, None if prior is None else prior.window)
            ),
        )
        if self.prefetch_campaign is not None:
            # The prefetch walker (A5.1): every answer is empty, so one synthetic campaign is
            # listed to reach each per-campaign read family (history, names, labels).
            count = replace(
                count,
                tallies={self.prefetch_campaign: CampaignTally(1, Decimal(0), False, None)},
                complete=True,
            )
        return _Counted(reads, count, tuple(sorted({item for ids in identities for item in ids})))

    def prefetch_audiences(self, request: AudiencesRenderInput) -> None:
        """Make every CURRENT read a real render of the month can reach, executing no query.

        Plan A5.1 prefetch, under the bake's RECORD reader: the walker lists one synthetic
        campaign per marketplace and answers every query empty in process, so each roster's
        served periods and month selections, each campaign read's plans, the history walk, the
        segment-label and campaign-name scopes are read as a populated render reads them. No
        query reaches the bake's port, which forbids them during prefetch. A selection refusal is
        the render's own, reached again from the replayed reads.
        """
        empty = _EmptyAnswers()

        def emptied[T](reader: T) -> T:
            if is_dataclass(reader) and hasattr(reader, "queries"):
                return cast("T", replace(cast("Any", reader), queries=empty))
            return reader

        rosters = emptied(self.rosters)
        histories = (
            cast("CampaignHistoryPort", rosters)
            if cast("object", self.histories) is cast("object", self.rosters)
            else emptied(self.histories)
        )
        walker = replace(
            self,
            tooling=replace(self.tooling, queries=empty),
            rosters=rosters,
            histories=histories,
            prefetch_campaign=_PREFETCH_CAMPAIGN,
        )
        try:
            walker.render_audiences(request)
        except DomainError, ValidationError:
            return

    def render_audiences(self, request: AudiencesRenderInput) -> AudiencesRenderResult:
        """Measure the marketplace's profile paths and every DSP campaign for the month.

        Phase 1: each profile path's roster and the campaigns' count pass. Phase 2: the profile
        measurement and the history walk for campaigns with no segments the month before. The
        page pass over the campaigns starts only when the publication is staged.
        """
        period = request.period
        bound = self.tooling.query_batch_max_concurrency
        (market,) = self.marketplaces
        served = self._served(market, period)
        delivered = period.window in served.published[DELIVERY_CONTRACT]
        prior = _prior_month(period)
        rostered = prior if prior.window in served.published[DELIVERY_CONTRACT] else None
        scopes = self._scopes(market, period, served)
        first = run_bounded(
            (
                *(partial(self._roster, period, scope) for scope in scopes),
                *((partial(self._count, market, period, rostered),) if delivered else ()),
            ),
            bound,
        )
        counted = cast("_Counted", first[-1]) if delivered else None
        rosters: dict[tuple[str, str], Roster] = {}
        refused: dict[str, IsolatedRefusalReason] = {}
        for scope, item in zip(scopes, first, strict=False):
            if isinstance(item, _PathRefusal):
                refused[scope.name] = item.reason
            else:
                rosters[(market, scope.name)] = cast("Roster", item)
        batch = _Batch()
        for scope in scopes:
            if scope.name not in refused:
                self._plan(scope, (period, rosters[(market, scope.name)]), batch)
        second = run_bounded(
            (
                *((partial(self._measured, batch, period),) if batch.plans else ()),
                *((partial(self._histories, market, period, counted),) if counted else ()),
            ),
            bound,
        )
        answers: dict[_Label, GroupedAnswer] = {}
        measured_identities: tuple[str, ...] = ()
        if batch.plans:
            answers, measured_identities, incomplete = cast(
                "tuple[dict[_Label, GroupedAnswer], tuple[str, ...], frozenset[str]]", second[0]
            )
            refused.update(dict.fromkeys(incomplete, IsolatedRefusalReason.EVIDENCE_INCOMPLETE))
        histories = cast("CampaignHistories", second[-1]) if counted else None
        self._refuse_paths(scopes, refused)
        named = self._named(
            market, period, (served, answers, rosters, refused), (counted, histories)
        )
        campaign_identities = () if counted is None else counted.identities
        # Labels, campaign names, rosters, paid states, histories and refusals change the body
        # without changing a measured publication, so the revision names what they read too.
        identities = tuple(
            sorted(
                {
                    *measured_identities,
                    *campaign_identities,
                    _render_inputs_identity(rosters, (market, named), served, refused),
                }
            )
        )
        revision = PresetArtifactRevision.from_publication_identities(
            identities, render_format=AUDIENCES_RENDER_FORMAT
        ).value
        markets = (self._market(market, period, (served, rosters, named, revision)),)
        artifact = AudienceProfileArtifact(period, markets, self._policy.buyers_min, revision)
        pages = None
        if counted is not None and named.campaigns.index:
            pages = CampaignPagePass(
                counted.reads[: len(CAMPAIGN_METRICS)],
                MeasurementExecutionTooling(
                    queries=Unphased(cast("FoldingQueryPort", self.tooling.queries)),
                    query_batch_max_concurrency=self.tooling.query_batch_max_concurrency,
                ),
                {named.keys[entry.ref]: entry for entry in named.campaigns.index},
                counted.count,
                named.labels[DSP_VIEW],
            )
        return AudiencesRenderResult(artifact, identities, pages)

    @staticmethod
    def _refuse_paths(
        scopes: tuple[_Scope, ...], refused: dict[str, IsolatedRefusalReason]
    ) -> None:
        """Note each refused path; every served path refused refuses the page (01 §3.3)."""
        gaps = sum(1 for item in refused.values() if item is IsolatedRefusalReason.HISTORY_GAP)
        incomplete = len(refused) - gaps
        if scopes and all(scope.name in refused for scope in scopes):
            if refused[scopes[0].name] is IsolatedRefusalReason.HISTORY_GAP:
                raise IsolatedRefusal(IsolatedRefusalReason.HISTORY_GAP, RefusalScope.PAGE)
            raise IsolatedRefusal(IsolatedRefusalReason.EVIDENCE_INCOMPLETE, RefusalScope.PAGE)
        if gaps:
            note_isolated_refusal(
                IsolatedRefusalReason.HISTORY_GAP, RefusalScope.ITEM, gaps, section=_PROFILE_SECTION
            )
        if incomplete:
            note_isolated_refusal(
                IsolatedRefusalReason.EVIDENCE_INCOMPLETE,
                RefusalScope.ITEM,
                incomplete,
                section=_PROFILE_SECTION,
            )

    def _histories(
        self, market: str, period: CalendarPeriod, counted: _Counted
    ) -> CampaignHistories:
        """Grade every listed campaign R1 didn't grade, in one grouped walk (01 §3.2)."""
        return self.histories.campaign_histories(
            CampaignHistoriesRequest(
                period.window,
                market,
                self._contract(DELIVERY_CONTRACT).platform_key,
                tuple(
                    sorted(
                        campaign
                        for campaign, tally in counted.count.tallies.items()
                        if not tally.rostered
                    )
                )
                if counted.count.complete
                else (),
            )
        )

    def _scopes(self, market: str, period: CalendarPeriod, served: _Served) -> tuple[_Scope, ...]:
        """Return a marketplace's graded profile paths: each one published for the month."""
        return tuple(
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

    def _plan(self, scope: _Scope, graded: tuple[CalendarPeriod, Roster], batch: _Batch) -> None:
        """Plan one profile path's figures, its segment figures graded against its own roster.

        A profile path plans its segment rows' three figures and its `profiled_total` row's two
        bases, which no roster grades.
        """
        period, roster = graded
        market = scope.market
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

    def _measured(
        self, batch: _Batch, period: CalendarPeriod
    ) -> tuple[dict[_Label, GroupedAnswer], tuple[str, ...], frozenset[str]]:
        """Run the profile measurement; return its answers, publications and incomplete paths."""
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
        answers, incomplete = _parsed(executed.slots, batch, period)
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
        return answers, identities, incomplete

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
        measured: tuple[
            _Served,
            dict[_Label, GroupedAnswer],
            dict[tuple[str, str], Roster],
            dict[str, IsolatedRefusalReason],
        ],
        campaigns: tuple[_Counted | None, CampaignHistories | None],
    ) -> _Named:
        """Read a marketplace's segment labels and campaign names before its revision exists.

        One label read per namespace: the DSP view's names every campaign segment too, since
        both read the DSP behaviour namespace (01 §3.4).
        """
        _served, _answers, rosters, _refused = measured
        counted, histories = campaigns
        states: dict[str, tuple[DeliveryState, ...]] = {}
        labels: dict[str, dict[str, SegmentLabel]] = {}
        listed = () if counted is None or not counted.count.complete else counted.count.tallies
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
            if view == DSP_VIEW and counted is not None and listed:
                keys |= counted.count.segments
            labels[view] = self._labels(market, VIEW_NAMESPACES[view], keys, period)
        if counted is None:
            return _Named(states, labels, {}, CampaignAudiences(published=False), {})
        if not counted.count.complete:
            note_isolated_refusal(
                IsolatedRefusalReason.EVIDENCE_INCOMPLETE,
                RefusalScope.SECTION,
                section=_CAMPAIGN_SECTION,
            )
            section = CampaignAudiences(
                published=True, refused_text=CAMPAIGN_INCOMPLETE_TEXT.format(month=period.label)
            )
            return _Named(states, labels, {}, section, {})
        # Sorted keys, so chat's placeholders number the same campaigns the same way.
        keys = tuple(sorted(listed))
        names = self.campaign_labels(keys, period.window) if keys else {}
        if any(campaign not in names for campaign in keys):
            # The resolver names every protected value, so a gap is a broken port, never a key.
            raise RuntimeError("every Audiences campaign needs its governed name or placeholder.")
        section, refs = _campaign_index(counted.count, (names, histories), period.label)
        return _Named(states, labels, names, section, refs)

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
                        # An unobserved paid profile is the paid callout, never a monthly gap;
                        # a refused path reads its own sentence.
                        no_delivery_text=item.refused_text
                        or (
                            ASI_NEEDS_PAID_FEATURE_TEXT
                            if paid is PaidState.NOT_OBSERVED
                            else NO_DELIVERY_TEXT
                        ),
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
        measured: tuple[
            _Served,
            dict[_Label, GroupedAnswer],
            dict[tuple[str, str], Roster],
            dict[str, IsolatedRefusalReason],
        ],
        period: CalendarPeriod,
    ) -> DeliveryState:
        served, answers, rosters, refused = measured
        contract_id = PROFILE_CONTRACTS[path]
        paid_state = served.paid.get(contract_id)
        if period.window not in served.published[contract_id]:
            return DeliveryState(path, None, paid_state)
        if path in refused:
            text = PROFILE_PATH_REFUSED_TEXTS[refused[path].value].format(month=period.label)
            return DeliveryState(path, None, paid_state, refused_text=text)
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


def _campaign_index(
    count: CampaignCount,
    named: tuple[dict[str, str], CampaignHistories | None],
    month: str,
) -> tuple[CampaignAudiences, dict[str, str]]:
    """Rank every listed campaign and grade it: available, withdrawn, or its own history gap.

    Campaigns rank by their largest matched-segment impressions, descending, then by campaign
    key, the order that numbers their opaque references. Segment figures never add up
    (impressions repeat per matched segment), so the rank never sums them.
    """
    names, histories = named
    graded: Mapping[str, CampaignHistory] = {} if histories is None else histories.histories
    ranked = sorted(count.tallies, key=lambda key: (-count.tallies[key].largest, key))
    entries: list[CampaignIndexEntry] = []
    refs: dict[str, str] = {}
    gaps = 0
    for rank, campaign in enumerate(ranked, start=1):
        tally = count.tallies[campaign]
        ref = f"campaign-{rank}"
        refs[ref] = campaign
        label = names[campaign]
        if not tally.rostered and graded.get(campaign) is CampaignHistory.HISTORY_GAP:
            gaps += 1
            text = CAMPAIGN_HISTORY_GAP_TEXT.format(campaign=label, month=month)
            entries.append(
                CampaignIndexEntry(ref, label, tally.rows, CampaignEntryState.REFUSED, text)
            )
        elif tally.withdrawn_note is not None:
            entries.append(
                CampaignIndexEntry(
                    ref, label, tally.rows, CampaignEntryState.WITHDRAWN, tally.withdrawn_note
                )
            )
        else:
            entries.append(CampaignIndexEntry(ref, label, tally.rows, CampaignEntryState.AVAILABLE))
    if gaps:
        note_isolated_refusal(
            IsolatedRefusalReason.HISTORY_GAP, RefusalScope.ITEM, gaps, section=_CAMPAIGN_SECTION
        )
    return CampaignAudiences(published=True, index=tuple(entries)), refs


_RENDER_INPUTS_IDENTITY_PREFIX = "audiences-render-inputs.v3:"
"""Names the digest of every non-measurement read an Audiences body renders.

v3 adds the campaign index (its names, history grades and states) and the refused paths."""


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


def _render_inputs_identity(  # noqa: PLR0913, PLR0917 -- every non-measurement read
    rosters: dict[tuple[str, str], Roster],
    named: tuple[str, _Named],
    served: _Served,
    refused: dict[str, IsolatedRefusalReason],
) -> str:
    """Digest the rosters, labels, names, paid states, index and refusals a bake rendered.

    None of them is a measured publication, yet each changes the body, so a change to any of
    them must advance the revision rather than leave a stale body current. Paid states come from
    publication history outside the month's plans; the campaign index reads the history walk
    and the governed names; a refusal changes what a path or campaign shows.
    """
    market, item = named
    evidence = {
        "rosters": [
            [roster_market, name, list(roster.keys), roster.first_period]
            for (roster_market, name), roster in sorted(rosters.items())
        ],
        "markets": {
            market: {
                "labels": {view: _label_payload(labels) for view, labels in item.labels.items()},
                "campaign_names": dict(sorted(item.campaign_names.items())),
                "campaigns_published": item.campaigns.published,
                "campaigns_refused": item.campaigns.refused_text,
                "campaign_index": [
                    [entry.ref, entry.label, entry.segment_count, entry.state.value, entry.text]
                    for entry in item.campaigns.index
                ],
                "paid": _paid_payload(served.paid),
                "refused_paths": {path: reason.value for path, reason in sorted(refused.items())},
            }
        },
    }
    digest = hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return f"{_RENDER_INPUTS_IDENTITY_PREFIX}{digest}"


def _parsed(
    slots: tuple[PresentationMeasurementAnswerSlot, ...], batch: _Batch, period: CalendarPeriod
) -> tuple[dict[_Label, GroupedAnswer], frozenset[str]]:
    """Parse every profile answer; return them and the paths whose answer isn't complete.

    Every month planned has a CURRENT publication, so anything short of a complete answer is
    missing evidence: that path's own `evidence_incomplete`, never an undercount. A profile read
    is bounded by its segments, so one cut short is a defect (`parse_grouped_answer`).
    """
    parsed: dict[_Label, GroupedAnswer] = {}
    incomplete: set[str] = set()
    for answer, label in zip(slots, batch.labels, strict=True):
        grains = TOTAL_GRAINS if label[2] in TOTAL_METRICS else SEGMENT_GRAINS
        item = parse_grouped_answer(answer, period.window, grains)
        if item is None or any(key[0] != label[0] for key in item.rows):
            raise RuntimeError("an Audiences profile answer is incoherent for its marketplace.")
        if not grouped_answer_complete(item, period.window):
            incomplete.add(label[1])
        parsed[label] = item
    return parsed, frozenset(incomplete)


AUDIENCES_ROW_MAX_BYTES = 800
"""A ceiling on one profile or campaign row's encoded bytes; a test holds wide rows within it."""
AUDIENCES_ENVELOPE_MAX_BYTES = 16_000
"""A ceiling on one body's encoded bytes outside its rows; a test holds it."""
_CUSTODY_TYPE: Final = "AudiencesCustody"
_CUSTODY_VERSION: Final = 1


def _custody(body_digest: str) -> str:
    """Audiences custody binds each body by digest; it carries no private selector."""
    return capped_preset_artifact_json(
        {"type": _CUSTODY_TYPE, "version": _CUSTODY_VERSION, "body_digest": body_digest}
    )


@dataclass(frozen=True, slots=True)
class _AudiencesSiblingPairs:
    """The Audiences sibling stream (ADR-145): index shards, profile pages, campaign pages.

    The index shards and profile pages render from the parent the render already holds; the
    campaign pages render from the page pass as the stage draws them, one campaign at a time.
    """

    page: PresetArtifactPage
    variant: PresetArtifactVariant
    revision: PresetArtifactRevision
    artifact: AudienceProfileArtifact
    campaigns: CampaignPages | None

    def stream(self) -> PresetArtifactSiblingStream:
        """Return the stream with its declared count, known before it is drawn."""
        (market,) = self.artifact.markets
        count = (
            market.campaigns.index_shards
            - 1
            + sum(
                max(profile_page_count(view, item.path) - 1, 0)
                for view in (market.paid, market.standard)
                for item in view.deliveries
            )
            + (0 if self.campaigns is None else self.campaigns.page_count)
        )
        return PresetArtifactSiblingStream(self, count)

    def __call__(self) -> Iterator[PresetArtifactSiblingPair]:
        (market,) = self.artifact.markets
        revision = self.revision.value
        for shard in range(1, market.campaigns.index_shards):
            yield self._pair(
                PresetArtifactSiblingIdentity(_INDEX_ORDERING, shard, _INDEX_FILTER),
                audiences_campaign_index_payload(market.campaigns, shard, revision),
            )
        for view in (market.paid, market.standard):
            for item in view.deliveries:
                for number in range(1, profile_page_count(view, item.path)):
                    yield self._pair(
                        PresetArtifactSiblingIdentity(
                            _PROFILE_ORDERING, number, f"profile-{item.path}"
                        ),
                        audiences_profile_page_payload(
                            view, item.path, number, self.artifact.display_minimum, revision
                        ),
                    )
        if self.campaigns is not None:
            for campaign_page in self.campaigns.pages():
                yield self._pair(
                    PresetArtifactSiblingIdentity(
                        _CAMPAIGN_ORDERING, campaign_page.page, campaign_page.entry.ref
                    ),
                    audiences_campaign_page_payload(campaign_page, revision),
                )

    def _pair(
        self, identity: PresetArtifactSiblingIdentity, payload: dict[str, object]
    ) -> PresetArtifactSiblingPair:
        body = PresetArtifactSiblingBody(
            self.page, self.variant, self.revision, identity, capped_preset_artifact_json(payload)
        )
        custody = PresetArtifactSiblingCustody(
            self.page, self.variant, self.revision, identity, _custody(body.digest)
        )
        return body, custody


@dataclass(frozen=True, slots=True)
class RetainedAudiencesPresetArtifactSource:
    """Compile the latest complete month's Audiences artifact from retained truth only."""

    compiler: AudiencesCompiler
    served: Callable[[], frozenset[tuple[str, CalendarPeriod]]]
    """Every (contract, month) packs 10-12 have a CURRENT publication for, any marketplace."""
    pointers: PresetArtifactPointerStorePort
    clock: Callable[[], datetime]
    page: PresetArtifactPage = field(default_factory=lambda: PresetArtifactPage(AUDIENCES_PAGE_ID))

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return the parent and its parts' stream for the newest target month, or None.

        The campaign page pass starts here, inside the render, only once the revision differs
        from the pointer's, and the stage drains it.
        """
        if variant.value != LATEST_MONTH_VARIANT:
            return None
        resolved = self.resolve()
        if resolved is None:
            return None
        rendered = self.compiler.render_audiences(resolved)
        artifact = rendered.artifact
        if artifact is None:
            return None
        revision = PresetArtifactRevision.from_publication_identities(
            rendered.publication_identities, render_format=AUDIENCES_RENDER_FORMAT
        )
        if revision.value != artifact.revision:
            raise RuntimeError("the Audiences parent names another revision than its own.")
        current = self.pointers.current(self.page, variant)
        if current is not None and current.revision == revision:
            return SourceUnchanged(revision)
        body = PresetArtifactBody(
            self.page,
            variant,
            revision,
            capped_preset_artifact_json(audiences_artifact_payload(artifact)),
        )
        pages = None if rendered.campaigns is None else rendered.campaigns.start()
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
                resolved_window=artifact.month.window,
            ),
            PresetArtifactCustodyHeader(_custody(body.digest)),
            _AudiencesSiblingPairs(self.page, variant, revision, artifact, pages).stream(),
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


def audiences_policy_declared(schemas: PresentationPageSchemaSet) -> bool:
    """Whether the `audiences` v2 page entry declares exactly this bake.

    The page id alone doesn't open the source: the entry must declare every server-relevant
    part of the LLD's v2 entry (docs/ui/audiences §6): `mode: selectable` with the boundary
    required, exactly `AUDIENCES_TEMPORAL_POLICY`, exactly the artifact this renderer writes
    (`audiences`, `AudienceProfileArtifact` v2, its three outcomes) and exactly
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

    roster_reader = SegmentRosterReader(
        catalog=catalog,
        contracts=contracts,
        publications=selector,
        periods=selector,
        queries=queries,
        labels=labels,
        clock=current.clock,
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
        rosters=roster_reader,
        histories=roster_reader,
        segment_labels=labels,
        marketplaces=marketplaces,
        insight_contract=_contract(InsightSlotId.SEGMENT_OVER_INDEX),
        clock=current.clock,
        campaign_labels=campaign_names,
        cadence_lag_days=lags,
    )
    return RetainedAudiencesPresetArtifactSource(
        compiler=compiler, served=served, pointers=pointers, clock=current.clock
    )
