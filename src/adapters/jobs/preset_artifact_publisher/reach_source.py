# SPDX-License-Identifier: MIT-0
"""Bake `ReachAndFunnelArtifact` v1 per declared preset (docs/ui/reach-and-funnel, ADR-075).

Per calendar preset and per governed marketplace, every per-cell figure is planned through the
measurement layer: shoppers reached, searching shoppers, purchasers and halo-inclusive sales of
pack 5 (`fct_exposure_group_outcome`), grouped by exposure group and identity space, for the
resolved period and the prior calendar period (D-8 (a)). Nothing is planned across cells; the
domain computes every share, rate and index inside one delivery (`domain.presentation.reach`), so
a `not_returned` cell reads "not available" rather than a share of the cells that came back.

Each period is read from the one pack variant series holding its CURRENT publication (plan D9):
the base pack's `combined` rows, or the touch-only variant's reach on `exposure` rows and its
outcomes on `outcome` rows. A period two series hold bakes nothing, and a prior period of another
variant is shown with no change (`PACK_VARIANT_MISMATCH`).

Registered with the publisher, and fails closed: the source composes only once the v2 page entry
`reach` declares `ReachAndFunnelArtifact` v1 and the sections this bake fills (the UI workstream
adds it, as `docs/ui/reach-and-funnel/low-level-design.md` declares it), and only while the
governed AMC instance declaration (`contracts/shared/amc-instance.yaml`) names a known kind. On
an instant-access instance the overlap and funnel read "Needs DSP and sponsored ads on one AMC
instance" for the latest complete period, with nothing planned.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, NamedTuple, Protocol, cast

from adapters.campaign_insight_requests import FindingSource, predicate_findings
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.inbound.tool_handlers.measurement import (
    DEFAULT_MAX_ROWS,
    MeasurementExecutionTooling,
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
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
from adapters.jobs.preset_artifact_publisher.scorecard_source import (
    MARKETPLACE_SELECTOR,
    GroupedAnswer,
    grouped_answer_complete,
    parse_grouped_answer,
)
from adapters.outbound.amc_instance import load_amc_instance
from adapters.outbound.bake_sources import source_closed
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import (
    PerPackPresentationEvidenceInspection,
    PublicationSelector,
)
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.spend_roster import SpendRosterReader, ad_product_labels
from adapters.producer.packs import load_query_packs
from domain.measurement.periodic import (
    BUCKET_SCHEME_COLUMN,
    ROW_KIND_COLUMN,
    VariantMismatchError,
    active_variant,
)
from domain.measurement.rosters import Roster, RosterKind
from domain.presentation.calendar_presets import (
    PeriodicContract,
    PresetSection,
    ResolveCalendarPresetRequest,
    resolve_calendar_preset,
)
from domain.presentation.insights import (
    DeterministicFinding,
    InsightContract,
    InsightEvidenceInput,
    InsightFindingKind,
    InsightHorizonRole,
    InsightSlotId,
    OperandBasis,
    finding_is_material,
    horizon_role_for,
)
from domain.presentation.page_schemas import TemporalPresetId, is_calendar_preset
from domain.presentation.preset_artifacts import (
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
from domain.presentation.reach import (
    ACCOUNT,
    BUCKET_SCHEMES,
    DEVICE_OR_BROWSER,
    EXPOSURE_GROUPS,
    FREQUENCY_DECLARED_PERIODS,
    OVERLAP_DECLARED_PERIODS,
    REACH_ARTIFACT_TYPE,
    REACH_ARTIFACT_VERSION,
    REACH_PAGE_ID,
    SPONSORED_ONLY,
    ExposureCell,
    FrequencyCell,
    FrequencyChannel,
    FrequencyDelivery,
    FrequencySlotRequest,
    ReachAbsence,
    ReachAndFunnelArtifact,
    ReachDelivery,
    ReachMarket,
    ReachSlotRequest,
    SectionsRequest,
    build_sections,
    frequency_saturation_slot,
    full_funnel_association_slot,
    funnel_rate,
    reach_artifact_payload,
    reach_overlap_slot,
    saturated_bucket,
    share_above,
)
from domain.semantics.amc_instance import AmcInstanceKind
from domain.semantics.calendar_periods import CalendarPeriod
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionPlan,
    PresentationEvidenceInspectionRequest,
)
from ports.outbound.rosters import RosterRequest
from ports.outbound.semantic_state import scope_horizon_policy
from ports.outbound.served_periods import ServedPeriodsRequest

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from adapters.config import PresetArtifactBakeSettings
    from adapters.inbound.tool_handlers.measurement import PresentationMeasurementAnswerSlot
    from adapters.outbound.bake_sources import BakeSourceReads
    from domain.catalog.capability import Catalog
    from domain.data_contracts.contract import DataContract
    from domain.presentation.insights import SaturationPolicy
    from domain.presentation.page_schemas import (
        PresentationPageSchema,
        PresentationPageSchemaSet,
        PresentationTemporalPolicy,
    )
    from ports.outbound.preset_artifacts import PresetArtifactPointerStorePort
    from ports.outbound.publication_selection import PresentationEvidenceInspectionPort
    from ports.outbound.query import PeriodSeriesQueryPort
    from ports.outbound.semantic_state import CurrentPublicationInspectionPort

__all__ = [
    "REACH_MATERIALIZATION_UNAVAILABLE",
    "REACH_PRESET_RENDER_FORMAT",
    "ReachArtifactRenderer",
    "ReachCompiler",
    "ReachRenderInput",
    "ReachRenderResult",
    "RetainedReachPresetArtifactSource",
    "reach_bake_plan_count",
    "reach_policy",
    "retained_reach_preset_artifact_source",
]

REACH_PRESET_RENDER_FORMAT = 1
"""The Reach renderer's output generation (ADR-070 render format)."""

EXPOSURE_CONTRACT = "amc-exposure-groups"
CELL_METRICS = (
    ("shoppers_reached", "shoppers_reached"),
    ("search_shoppers", "search_shoppers_in_window"),
    ("purchasers", "purchasers_in_window"),
    ("revenue_incl_halo", "revenue_incl_halo_in_window"),
)
"""Each `ExposureCell` field and the per-cell dictionary metric planned for it."""
CELL_GRAINS = ("marketplace", "exposure_group", "identity_space")
FREQUENCY_PLATFORM = "amc-exposure-frequency-platform"
FREQUENCY_CHANNEL = "amc-exposure-frequency-channel"
FREQUENCY_METRICS = (
    ("shoppers", "shoppers_in_frequency_bucket"),
    ("purchasers", "purchasers_in_frequency_bucket"),
    ("revenue_incl_halo", "revenue_incl_halo_in_frequency_bucket"),
    ("dsp_window_spend", "dsp_window_spend_in_frequency_bucket"),
    ("sponsored_window_spend", "sponsored_window_spend_in_frequency_bucket"),
)
"""Each `FrequencyCell` field and the per-bucket dictionary metric planned for it (pack 6)."""
FREQUENCY_GRAINS = ("marketplace", "frequency_bucket", "identity_space")
CHANNEL_GRAINS = ("marketplace", "ad_product", "frequency_bucket", "identity_space")
_CHANNEL_GRAIN_SET = "channel"
_AMAZON_ADS = "amazon_ads"
_PERIOD_GRAIN_SELECTOR = "time_grain"
_GRAIN_SET_SELECTOR = "grain_set"
_PLATFORM_GRAIN_SET = "platform"
_ARTIFACT_ID = "reach"
_ANCHORED_SECTIONS = ("overlap", "funnel")
_FREQUENCY_SECTION = "frequency"
_CORRELATION_ID = "reach-artifact"
_SILENT_OVERLAP = (
    "DSP-only shoppers on Amazon account IDs are below the declared share, or a cell isn't "
    "available."
)
_SILENT_FUNNEL = (
    "Shoppers who saw both and those who saw sponsored ads only don't differ in purchaser rate "
    "by the declared threshold, or a rate isn't available."
)
_SILENT_FREQUENCY = (
    "No frequency bucket on Amazon account IDs passed the two-step test with enough shoppers and "
    "purchasers, or a bucket isn't available."
)
_FINDING_QUANTUM = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class ReachRenderInput:
    """One resolved calendar preset: its complete period and the prior period of the same grain."""

    preset: TemporalPresetId
    period: CalendarPeriod

    @property
    def prior(self) -> CalendarPeriod:
        """The prior calendar period, the one comparison allowed (D-8 (a))."""
        return self.period.previous()


@dataclass(frozen=True, slots=True)
class ReachRenderResult:
    """The reduced artifact and the exact publication identities consumed."""

    artifact: ReachAndFunnelArtifact | None
    publication_identities: tuple[str, ...]


class ReachCompiler(Protocol):
    """Compile one resolved preset into a Reach artifact from governed evidence only."""

    def render_reach(self, request: ReachRenderInput) -> ReachRenderResult:
        """Return the reduced artifact and the exact publication identities consumed."""
        ...


type _Label = tuple[str, str, str]
"""(marketplace, cell field, role) for one planned answer."""


@dataclass(frozen=True, slots=True)
class _Read:
    """One marketplace's planned period: its role, the period, and the series it is read from."""

    market: str
    role: str
    period: CalendarPeriod
    variant: str | None


@dataclass(frozen=True, slots=True)
class _FrequencyRead:
    """One marketplace's frequency for the period: its scheme, channel roster and campaigns."""

    market: str
    period: CalendarPeriod
    scheme: str
    channel_roster: Roster | None
    active_campaigns: int


@dataclass(frozen=True, slots=True)
class _Reduced:
    """The batch's answers, the artifact revision, each (market, role)'s series, and refusal.

    `series` holds the pack variant each planned (marketplace, role) period was read from (None
    for the base pack); `refused` that this marketplace's prior period was held by two series;
    `frequency` the marketplace's pack 6 read, None when frequency isn't planned for it.
    """

    answers: dict[_Label, GroupedAnswer]
    revision: str
    series: dict[tuple[str, str], str | None]
    refused: bool
    frequency: _FrequencyRead | None = None


class _FrequencyPlan(NamedTuple):
    """One frequency figure's marketplace, period, grain set and bucket scheme."""

    market: str
    period: CalendarPeriod
    grain_set: str
    scheme: str


@dataclass
class _Batch:
    """The plans one bake runs, their labels and the grains each label's answer is grouped by."""

    plans: list[MeasurementToolRequest] = field(default_factory=list[MeasurementToolRequest])
    labels: list[_Label] = field(default_factory=list[_Label])
    grains: dict[_Label, tuple[str, ...]] = field(default_factory=dict[_Label, tuple[str, ...]])

    def add(self, plan: MeasurementToolRequest, label: _Label, grains: tuple[str, ...]) -> None:
        self.plans.append(plan)
        self.labels.append(label)
        self.grains[label] = grains


@dataclass(frozen=True, slots=True)
class ReachArtifactRenderer:
    """Plan pack 5's per-cell figures for every marketplace and reduce them in the domain."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    evidence: PresentationEvidenceInspectionPort
    tooling: MeasurementTooling
    marketplaces: tuple[str, ...]
    insight_contracts: tuple[InsightContract, InsightContract, InsightContract]
    """`reach-overlap`, `full-funnel-association` and `frequency-saturation`, in that order."""
    clock: Callable[[], datetime]
    instance: AmcInstanceKind
    """The governed AMC instance kind (`contracts/shared/amc-instance.yaml`), never defaulted."""
    ad_product_labels: Mapping[str, str] = field(default_factory=dict[str, str])
    """Each governed Amazon Ads ad product's label, for a per-channel row off the spend roster."""
    frequency_lag_days: int = 16
    """Packs 6 and 7's governed cadence lag, for frequency's pending date (design §4.6)."""

    def __post_init__(self) -> None:
        if tuple(item.id for item in self.insight_contracts) != (
            InsightSlotId.REACH_OVERLAP,
            InsightSlotId.FULL_FUNNEL_ASSOCIATION,
            InsightSlotId.FREQUENCY_SATURATION,
        ):
            raise ValueError(
                "Reach renders reach-overlap, full-funnel-association and frequency-saturation."
            )
        if not self.marketplaces:
            raise ValueError("Reach bakes at least one governed marketplace.")

    def _row_kind(self, variant: str | None, measure: str) -> str:
        """Return the `row_kind` whose rows of `variant` carry `measure` (the contract says)."""
        contract = next(item for item in self.contracts if item.contract_id == EXPOSURE_CONTRACT)
        cells = contract.expected_cells
        kind = None if cells is None else cells.value_for(variant, ROW_KIND_COLUMN, (measure,))
        if kind is None:
            raise ValueError(f"{EXPOSURE_CONTRACT} declares no rows carrying {measure}.")
        return kind

    def _request(self, cell: tuple[str, str], read: _Read) -> MeasurementToolRequest:
        """Plan one cell metric (`(cell field, metric)`) for one marketplace's period and series."""
        cell_field, metric = cell
        period, market = read.period, read.market
        row_kind = self._row_kind(read.variant, cell_field)
        return MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=self.catalog,
            contracts=self.contracts,
            metric_name=metric,
            grains=CELL_GRAINS,
            window=period.window,
            computed_at=self.clock(),
            correlation_id=_CORRELATION_ID,
            filters={
                MARKETPLACE_SELECTOR: market,
                _PERIOD_GRAIN_SELECTOR: period.grain.value,
                _GRAIN_SET_SELECTOR: _PLATFORM_GRAIN_SET,
                ROW_KIND_COLUMN: row_kind,
            },
            max_rows=DEFAULT_MAX_ROWS,
        )

    def _served(
        self, market: str, periods: tuple[CalendarPeriod, ...]
    ) -> dict[CalendarPeriod, str | VariantMismatchError | None]:
        """Each published period's one series, or the refusal of a period two series hold."""
        reader = self.tooling.periods
        if reader is None:
            raise ValueError("Reach needs the served-period reader.")
        contracts = tuple(item for item in self.contracts if item.contract_id == EXPOSURE_CONTRACT)
        served = reader.served_periods(
            ServedPeriodsRequest(
                contracts=contracts, filters={MARKETPLACE_SELECTOR: market}, now=self.clock()
            )
        )
        found: dict[CalendarPeriod, str | VariantMismatchError | None] = {}
        for period in periods:
            if (EXPOSURE_CONTRACT, period) not in served.publications:
                continue
            try:
                found[period] = active_variant(
                    served.series_of(EXPOSURE_CONTRACT, period), period.label
                )
            except VariantMismatchError as error:
                found[period] = error
        return found

    def render_reach(self, request: ReachRenderInput) -> ReachRenderResult:
        """Measure every marketplace's current and prior cells and frequency, then reduce them.

        Overlap and funnel read pack 5 on the week and month presets; frequency reads pack 6 (and
        pack 7 for its channel rows) on the month and quarter presets. A period two series hold
        bakes nothing. On an instant-access instance nothing of pack 5 is planned, and frequency
        (sponsored-side only there) still is.
        """
        batch = _Batch()
        series: dict[tuple[str, str], str | None] = {}
        refused: set[str] = set()
        dsp = self.instance.dsp_available
        anchored = request.preset in OVERLAP_DECLARED_PERIODS
        if anchored and dsp and not self._plan_exposure(request, batch, (series, refused)):
            # Two series hold the period: neither is served, so nothing bakes.
            return ReachRenderResult(None, ())
        frequency: dict[str, _FrequencyRead] = {}
        if request.preset in FREQUENCY_DECLARED_PERIODS:
            for market in self.marketplaces:
                try:
                    read_frequency = self._frequency(market, request.period, batch)
                except VariantMismatchError:
                    # Fine and coarse series both hold the period: neither is served.
                    return ReachRenderResult(None, ())
                if read_frequency is not None:
                    frequency[market] = read_frequency
        if not batch.plans:
            if not dsp and anchored:
                return self._needs_dsp(request)
            return ReachRenderResult(None, ())
        answers, measured = self._measured(batch, request)
        if answers is None or not measured:
            # A partial answer, or no marketplace's period landed: nothing to bake, and the page
            # reads its pending or missing state from the serve-time resolution.
            return ReachRenderResult(None, ())
        if not dsp:
            measured = tuple(sorted({self._instance_identity(request), *measured}))
        revision = PresetArtifactRevision.from_publication_identities(
            measured, render_format=REACH_PRESET_RENDER_FORMAT
        ).value
        markets = tuple(
            self._market(
                request,
                market,
                _Reduced(answers, revision, series, market in refused, frequency.get(market)),
            )
            for market in self.marketplaces
        )
        return ReachRenderResult(
            ReachAndFunnelArtifact(request.preset, request.period, markets), measured
        )

    def _plan_exposure(
        self,
        request: ReachRenderInput,
        batch: _Batch,
        found: tuple[dict[tuple[str, str], str | None], set[str]],
    ) -> bool:
        """Plan pack 5's cells for each marketplace's period and prior; False if two series hold it.

        `found` collects each planned (marketplace, role)'s series and the marketplaces whose
        prior period two series hold, which is shown with no change.
        """
        series, refused = found
        for market in self.marketplaces:
            served = self._served(market, (request.period, request.prior))
            for role, period in (("current", request.period), ("prior", request.prior)):
                if period not in served:
                    continue
                variant = served[period]
                if isinstance(variant, VariantMismatchError):
                    if role == "current":
                        return False
                    refused.add(market)
                    continue
                series[market, role] = variant
                read = _Read(market, role, period, variant)
                for cell in CELL_METRICS:
                    batch.add(self._request(cell, read), (market, cell[0], role), CELL_GRAINS)
        return True

    def _scheme(self, contract_id: str, market: str, period: CalendarPeriod) -> str | None:
        """Return the bucket scheme of the one series holding `period`, or None if unpublished.

        Raises `VariantMismatchError` when two series (fine and coarse) hold it.
        """
        reader = self.tooling.periods
        if reader is None:
            raise ValueError("Reach needs the served-period reader.")
        contract = next(item for item in self.contracts if item.contract_id == contract_id)
        served = reader.served_periods(
            ServedPeriodsRequest(
                contracts=(contract,), filters={MARKETPLACE_SELECTOR: market}, now=self.clock()
            )
        )
        if (contract_id, period) not in served.publications:
            return None
        variant = active_variant(served.series_of(contract_id, period), period.label)
        cells = contract.expected_cells
        scheme = None if cells is None else cells.value_for(variant, BUCKET_SCHEME_COLUMN, ())
        if scheme is None:
            raise ValueError(f"{contract_id} declares no bucket scheme for {variant!r}.")
        return scheme

    def _frequency_request(self, metric: str, plan: _FrequencyPlan) -> MeasurementToolRequest:
        """Plan one frequency metric for one marketplace's period, grain set and scheme."""
        market, period, grain_set, scheme = plan
        return MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=self.catalog,
            contracts=self.contracts,
            metric_name=metric,
            grains=FREQUENCY_GRAINS if grain_set == _PLATFORM_GRAIN_SET else CHANNEL_GRAINS,
            window=period.window,
            computed_at=self.clock(),
            correlation_id=_CORRELATION_ID,
            filters={
                MARKETPLACE_SELECTOR: market,
                _PERIOD_GRAIN_SELECTOR: period.grain.value,
                _GRAIN_SET_SELECTOR: grain_set,
                ROW_KIND_COLUMN: "window",
                BUCKET_SCHEME_COLUMN: scheme,
            },
            max_rows=DEFAULT_MAX_ROWS,
        )

    def _frequency(
        self, market: str, period: CalendarPeriod, batch: _Batch
    ) -> _FrequencyRead | None:
        """Plan one marketplace's pack 6 buckets and pack 7 channel rows, and count campaigns.

        None when pack 6 hasn't published the period: frequency reads pending. Pack 7 only names
        the per-channel table's rows (ad products it holds or the spend roster lists), every cell
        of which reads "Not available per channel" (plan D12).
        """
        scheme = self._scheme(FREQUENCY_PLATFORM, market, period)
        if scheme is None:
            return None
        rosters = self.tooling.rosters
        if rosters is None:
            raise ValueError("Reach frequency needs the roster reader.")
        for cell_field, metric in FREQUENCY_METRICS:
            batch.add(
                self._frequency_request(
                    metric, _FrequencyPlan(market, period, _PLATFORM_GRAIN_SET, scheme)
                ),
                (market, cell_field, "frequency"),
                FREQUENCY_GRAINS,
            )
        channel_roster = None
        channel_scheme = self._scheme(FREQUENCY_CHANNEL, market, period)
        if channel_scheme is not None:
            attached = attach_roster(
                self._frequency_request(
                    "shoppers_in_frequency_bucket",
                    _FrequencyPlan(market, period, _CHANNEL_GRAIN_SET, channel_scheme),
                ),
                rosters,
            )
            channel_roster = attached.rosters[0] if attached.rosters else None
            batch.add(attached, (market, "shoppers", "channels"), CHANNEL_GRAINS)
        campaigns = rosters.roster(
            RosterRequest(RosterKind.CAMPAIGN, period.window, market, _AMAZON_ADS)
        )
        return _FrequencyRead(market, period, scheme, channel_roster, len(campaigns.entries))

    def _instance_identity(self, request: ReachRenderInput) -> str:
        """Name the governed instance declaration and the period as one evidence identity."""
        return (
            f"amc-instance.v1:{self.instance.value}:{request.period.grain.value}:"
            f"{request.period.window.start.isoformat()}:{request.period.window.end.isoformat()}"
        )

    def _needs_dsp(self, request: ReachRenderInput) -> ReachRenderResult:
        """Bake the instant-access state: nothing is planned, overlap and funnel need DSP.

        The artifact's evidence is the governed declaration and the period, so its identity
        names both and changes when either does.
        """
        identity = self._instance_identity(request)
        revision = PresetArtifactRevision.from_publication_identities(
            (identity,), render_format=REACH_PRESET_RENDER_FORMAT
        ).value
        markets = tuple(
            self._market(request, market, _Reduced({}, revision, {}, refused=False))
            for market in self.marketplaces
        )
        return ReachRenderResult(
            ReachAndFunnelArtifact(request.preset, request.period, markets), (identity,)
        )

    def _measured(
        self, batch: _Batch, request: ReachRenderInput
    ) -> tuple[dict[_Label, GroupedAnswer] | None, tuple[str, ...]]:
        prepared = prepare_batch(tuple(batch.plans))
        if len(prepared.plans) != len(batch.labels):
            raise RuntimeError("the Reach batch and its labels must align.")
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
        return self._parsed(executed.slots, batch, request), measured

    @staticmethod
    def _parsed(
        slots: tuple[PresentationMeasurementAnswerSlot, ...],
        batch: _Batch,
        request: ReachRenderInput,
    ) -> dict[_Label, GroupedAnswer] | None:
        """Parse every answer; a partial, truncated or mis-pinned one bakes nothing."""
        parsed: dict[_Label, GroupedAnswer] = {}
        for answer, label in zip(slots, batch.labels, strict=True):
            market, _field, role = label
            window = request.prior.window if role == "prior" else request.period.window
            item = parse_grouped_answer(answer, window, batch.grains[label])
            if item is None or not grouped_answer_complete(item, window):
                # Every period planned has a CURRENT publication, so anything short of a
                # complete answer is missing evidence: no bake, never an undercount.
                return None
            if any(key[0] != market for key in item.rows):
                return None
            parsed[label] = item
        return parsed

    def _delivery(self, answers: dict[_Label, GroupedAnswer], read: _Read) -> ReachDelivery:
        """Build one planned period's 3 x 2 grid from its four answers.

        On the base pack every figure of a cell sits on one row. On the touch-only variant reach
        comes from `exposure` rows and outcomes from `outcome` rows, account IDs only, so a cell
        whose outcome row didn't come back keeps its reach and reads its outcomes as absent.
        """
        market, role, period, variant = read.market, read.role, read.period, read.variant
        by_field = {
            cell_field: answers.get((market, cell_field, role)) for cell_field, _m in CELL_METRICS
        }
        if any(item is None for item in by_field.values()):
            raise RuntimeError("every planned Reach period has its four answers.")
        currency = None
        revenue = cast("GroupedAnswer", by_field["revenue_incl_halo"])
        if revenue.units is not None:
            currency = revenue.units.currency_code
        cells: list[ExposureCell] = []
        for group, _label in EXPOSURE_GROUPS:
            for space in (ACCOUNT, DEVICE_OR_BROWSER):
                values = {
                    cell_field: cast("GroupedAnswer", answer).rows.get((market, group, space))
                    for cell_field, answer in by_field.items()
                }
                shoppers = values["shoppers_reached"]
                if shoppers is None:
                    # A not_returned cell: absent, never 0 (design §4.5).
                    cells.append(ExposureCell(group, space, returned=False))
                    continue
                outcomes = variant is None or (
                    space == ACCOUNT
                    and (market, group, space) in cast("GroupedAnswer", by_field["purchasers"]).rows
                )
                if not outcomes:
                    cells.append(
                        ExposureCell(
                            group,
                            space,
                            returned=True,
                            shoppers_reached=shoppers,
                            outcome_returned=False,
                        )
                    )
                    continue
                sales = values["revenue_incl_halo"]
                cells.append(
                    ExposureCell(
                        group,
                        space,
                        returned=True,
                        shoppers_reached=shoppers,
                        search_shoppers=values["search_shoppers"],
                        purchasers=values["purchasers"],
                        revenue_incl_halo=sales,
                        currency_code=None if sales is None else currency,
                    )
                )
        return ReachDelivery(period, tuple(cells), variant)

    def _market(
        self,
        request: ReachRenderInput,
        market: str,
        reduced: _Reduced,
    ) -> ReachMarket:
        """One marketplace's sections and insights from its answers, revision and series."""
        answers, revision = reduced.answers, reduced.revision
        delivery, prior = (
            self._delivery(answers, _Read(market, role, period, reduced.series[market, role]))
            if (market, role) in reduced.series
            else None
            for role, period in (("current", request.period), ("prior", request.prior))
        )
        read = reduced.frequency
        frequency = None if read is None else self._frequency_delivery(answers, read)
        sections = build_sections(
            SectionsRequest(
                request.preset,
                request.period,
                delivery,
                prior,
                dsp_available=self.instance.dsp_available,
                prior_refused=reduced.refused,
                frequency=frequency,
                channels=() if read is None else self._channels(answers, market, read),
                active_campaigns=None if read is None else read.active_campaigns,
                frequency_lag_days=self.frequency_lag_days,
            )
        )
        overlap_contract, funnel_contract, frequency_contract = self.insight_contracts
        role = horizon_role_for(request.period.window.days)
        return ReachMarket(
            market,
            sections,
            (
                reach_overlap_slot(
                    ReachSlotRequest(
                        delivery,
                        self._overlap_material(delivery, overlap_contract, role),
                        revision,
                        _SILENT_OVERLAP,
                    )
                ),
                full_funnel_association_slot(
                    ReachSlotRequest(
                        delivery,
                        self._funnel_material(delivery, funnel_contract, role),
                        revision,
                        _SILENT_FUNNEL,
                    )
                ),
                frequency_saturation_slot(
                    FrequencySlotRequest(
                        frequency,
                        cast("SaturationPolicy", frequency_contract.saturation_policy),
                        self._saturation_material(frequency, frequency_contract, role),
                        revision,
                        _SILENT_FREQUENCY,
                    )
                ),
            ),
        )

    @staticmethod
    def _frequency_delivery(
        answers: dict[_Label, GroupedAnswer], read: _FrequencyRead
    ) -> FrequencyDelivery:
        """Build one marketplace's pack 6 buckets from its five per-bucket answers.

        A bucket whose shoppers didn't come back is `not_returned`. Each amount is labelled with
        its own answer's currency; a returned bucket with no amount had none (no purchase, or no
        touch of that kind), never 0 labelled with another column's currency.
        """
        market = read.market
        found = {name: answers.get((market, name, "frequency")) for name, _m in FREQUENCY_METRICS}
        if any(item is None for item in found.values()):
            raise RuntimeError("every planned frequency period has its five answers.")
        answer = cast("dict[str, GroupedAnswer]", found)

        def labelled(name: str, value: Decimal | None) -> str | None:
            units = answer[name].units
            return None if value is None or units is None else units.currency_code

        cells: list[FrequencyCell] = []
        for bucket, _label in BUCKET_SCHEMES[read.scheme]:
            for space in (ACCOUNT, DEVICE_OR_BROWSER):
                key = (market, bucket, space)
                shoppers = answer["shoppers"].rows.get(key)
                if shoppers is None:
                    # A not_returned bucket: absent, never 0 (design §4.5).
                    cells.append(FrequencyCell(bucket, space, returned=False))
                    continue
                value = {name: answer[name].rows.get(key) for name in answer}
                cells.append(
                    FrequencyCell(
                        bucket,
                        space,
                        returned=True,
                        shoppers=shoppers,
                        purchasers=value["purchasers"],
                        revenue_incl_halo=value["revenue_incl_halo"],
                        currency_code=labelled("revenue_incl_halo", value["revenue_incl_halo"]),
                        dsp_window_spend=value["dsp_window_spend"],
                        dsp_currency=labelled("dsp_window_spend", value["dsp_window_spend"]),
                        sponsored_window_spend=value["sponsored_window_spend"],
                        sponsored_currency=labelled(
                            "sponsored_window_spend", value["sponsored_window_spend"]
                        ),
                    )
                )
        return FrequencyDelivery(read.period, read.scheme, tuple(cells))

    def _channels(
        self, answers: dict[_Label, GroupedAnswer], market: str, read: _FrequencyRead
    ) -> tuple[FrequencyChannel, ...]:
        """List the per-channel rows: rostered ad products, then any pack 7 holds off the roster."""
        held = answers.get((market, "shoppers", "channels"))
        returned = tuple(dict.fromkeys(str(key[1]) for key in (() if held is None else held.rows)))
        roster = read.channel_roster
        rows = [] if roster is None else [(entry.key, entry.label) for entry in roster.entries]
        listed = {key for key, _label in rows}
        for key in returned:
            if key in listed:
                continue
            label = self.ad_product_labels.get(key)
            if label is None:
                raise ValueError(f"{key} is not a governed Amazon Ads ad product.")
            rows.append((key, label))
        return tuple(FrequencyChannel(key, label) for key, label in rows)

    @staticmethod
    def _saturation_material(
        delivery: FrequencyDelivery | None, contract: InsightContract, role: InsightHorizonRole
    ) -> bool:
        """Whether the bucket the two-step rule names clears the contract's materiality."""
        policy = contract.saturation_policy
        if delivery is None or policy is None:
            return False
        bucket = saturated_bucket(delivery, policy)
        share = None if bucket is None else share_above(delivery, bucket)
        if share is None:
            return False
        (bound,) = predicate_findings(
            (DeterministicFinding("Shoppers who saw more ads than the named bucket", share),),
            InsightFindingKind.SHARE_OF_TOTAL,
            InsightEvidenceInput.FREQUENCY_BUCKET_CELLS,
            source=FindingSource(
                InsightSlotId.FREQUENCY_SATURATION,
                OperandBasis.BUCKET_DELIVERY,
                "shoppers_in_frequency_bucket",
            ),
        )
        return finding_is_material(contract.predicate.materiality, bound, role)

    @staticmethod
    def _overlap_material(
        delivery: ReachDelivery | None, contract: InsightContract, role: InsightHorizonRole
    ) -> bool:
        if delivery is None:
            return False
        cells = tuple(delivery.cell(group, ACCOUNT) for group, _label in EXPOSURE_GROUPS)
        if any(not cell.returned or cell.shoppers_reached is None for cell in cells):
            return False
        total = sum((cast("Decimal", cell.shoppers_reached) for cell in cells), Decimal(0))
        if total == 0:
            return False
        dsp = cast("Decimal", delivery.cell("dsp_only", ACCOUNT).shoppers_reached)
        share = (dsp / total * 100).quantize(_FINDING_QUANTUM)
        (bound,) = predicate_findings(
            (DeterministicFinding("DSP-only shoppers share", share),),
            InsightFindingKind.SHARE_OF_TOTAL,
            InsightEvidenceInput.EXPOSURE_GROUP_CELLS,
            source=FindingSource(
                InsightSlotId.REACH_OVERLAP, OperandBasis.GROUP_DELIVERY, "shoppers_reached"
            ),
        )
        return finding_is_material(contract.predicate.materiality, bound, role)

    @staticmethod
    def _funnel_material(
        delivery: ReachDelivery | None, contract: InsightContract, role: InsightHorizonRole
    ) -> bool:
        if delivery is None:
            return False
        both = funnel_rate(delivery.cell("dsp_and_sponsored", ACCOUNT), "purchaser_rate")
        sponsored = funnel_rate(delivery.cell(SPONSORED_ONLY, ACCOUNT), "purchaser_rate")
        if isinstance(both, ReachAbsence) or isinstance(sponsored, ReachAbsence):
            return False
        if sponsored.value == 0:
            return False
        change = ((both.value - sponsored.value) / sponsored.value * 100).quantize(_FINDING_QUANTUM)
        (bound,) = predicate_findings(
            (DeterministicFinding("Saw both purchaser rate against sponsored only", change),),
            InsightFindingKind.PERCENTAGE_CHANGE,
            InsightEvidenceInput.EXPOSURE_GROUP_CELLS,
            source=FindingSource(
                InsightSlotId.FULL_FUNNEL_ASSOCIATION,
                OperandBasis.GROUP_DELIVERY,
                "purchaser_rate",
            ),
            priors={"Saw both purchaser rate against sponsored only": sponsored.value},
        )
        return finding_is_material(contract.predicate.materiality, bound, role)


@dataclass(frozen=True, slots=True)
class RetainedReachPresetArtifactSource:
    """Compile one declared Reach preset variant from retained truth only."""

    temporal_policy: PresentationTemporalPolicy
    compiler: ReachCompiler
    served: Callable[[], frozenset[tuple[str, CalendarPeriod]]]
    """Every (contract, complete period) pack 5 has a CURRENT publication for, any marketplace."""
    cadence_lag_days: int
    pointers: PresetArtifactPointerStorePort
    clock: Callable[[], datetime]
    instance: AmcInstanceKind
    """The governed AMC instance kind; on instant access no pack 5 period anchors the page."""
    frequency_lag_days: int = 16
    """Pack 6's governed cadence lag; frequency anchors the quarter preset on it."""
    page: PresetArtifactPage = field(default_factory=lambda: PresetArtifactPage(REACH_PAGE_ID))

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return a complete retained-materializer publication or no bakeable artifact."""
        presets = self.temporal_policy.candidate_presets
        preset = next((item for item in presets if item.value == variant.value), None)
        resolved = None if preset is None else self.resolve(preset)
        if resolved is None:
            return None
        rendered = self.compiler.render_reach(resolved)
        if rendered.artifact is None:
            return None
        revision = PresetArtifactRevision.from_publication_identities(
            rendered.publication_identities, render_format=REACH_PRESET_RENDER_FORMAT
        )
        current = self.pointers.current(self.page, variant)
        if current is not None and current.revision == revision:
            return SourceUnchanged(revision)
        body = PresetArtifactBody(
            self.page,
            variant,
            revision,
            canonical_preset_artifact_json(reach_artifact_payload(rendered.artifact)),
        )
        pointer = PresetArtifactPointer(
            self.page,
            variant,
            revision,
            body.digest,
            1 if current is None else current.forward_token + 1,
            None if current is None else current.revision,
            None,
            # The served period (ADR-116 decision 4), so every preset button names its dates.
            resolved_window=rendered.artifact.period.window,
        )
        return PresetArtifactPublicationRequest(
            body,
            PresetArtifactPointerAdvanceRequest(
                pointer,
                None if current is None else self.clock() + SUPERSEDED_HISTORY_RETENTION,
            ),
            None,
        )

    def resolve(self, preset: TemporalPresetId) -> ReachRenderInput | None:
        """Resolve a declared calendar preset to the latest period every anchor has (ADR-100).

        Overlap and funnel anchor the week and month presets on pack 5; frequency reads pack 6
        there without anchoring, so a late pack 6 reads pending. At the quarter preset only
        frequency is bound, so it anchors on pack 6 (LLD "Page declarations"). On an
        instant-access instance pack 5 can't run, so the week and month presets resolve to the
        latest complete period on the calendar alone, where overlap and funnel need DSP; pack 6
        still runs there, sponsored-side only.
        """
        if preset not in self.temporal_policy.candidate_presets or not is_calendar_preset(preset):
            return None
        dsp = self.instance.dsp_available
        contracts = (PeriodicContract(EXPOSURE_CONTRACT, self.cadence_lag_days),) if dsp else ()
        sections = (
            tuple(PresetSection(item, True, contracts) for item in _ANCHORED_SECTIONS)
            if preset in OVERLAP_DECLARED_PERIODS
            else ()
        ) + (
            (
                PresetSection(
                    _FREQUENCY_SECTION,
                    False,
                    (PeriodicContract(FREQUENCY_PLATFORM, self.frequency_lag_days),),
                ),
            )
            if preset in FREQUENCY_DECLARED_PERIODS
            else ()
        )
        needs_dsp = not dsp and preset in OVERLAP_DECLARED_PERIODS
        if not needs_dsp and not any(section.periodic_contracts for section in sections):
            return None
        result = resolve_calendar_preset(
            ResolveCalendarPresetRequest(
                preset=preset,
                as_of=self.clock().date(),
                sections=sections,
                publications=self.served(),
            )
        )
        return None if result.period is None else ReachRenderInput(preset, result.period)


def reach_bake_plan_count(root: Path) -> int:
    """Count the largest Reach bake's plans for the declared bake budget."""
    marketplaces = {
        mapping.target.value for mapping in load_commerce_identity_bridge(root).marketplace_mappings
    }
    exposure = 2 * len(CELL_METRICS)
    frequency = len(FREQUENCY_METRICS) + 1
    return len(marketplaces) * (exposure + frequency)


def reach_policy(schemas: PresentationPageSchemaSet) -> PresentationTemporalPolicy | None:
    """Return the `reach` v2 page's temporal policy, or None until it declares this bake.

    The page id alone doesn't open the source: the entry must declare exactly the artifact this
    renderer writes (`reach`, `ReachAndFunnelArtifact` v1, its three outcomes), the overlap and
    funnel sections on the week and month presets as anchors, and frequency on the month and
    quarter presets, not anchoring. Any other declaration keeps the page unavailable.
    """
    page = next((item for item in schemas.pages if item.page_id == REACH_PAGE_ID), None)
    if page is None or not _declares_this_bake(page):
        return None
    return page.temporal_policy


def _declares_this_bake(page: PresentationPageSchema) -> bool:
    if len(page.artifacts) != 1:
        return False
    (artifact,) = page.artifacts
    if (
        artifact.artifact_id != _ARTIFACT_ID
        or artifact.artifact_type != REACH_ARTIFACT_TYPE
        or artifact.version != REACH_ARTIFACT_VERSION
        or set(artifact.outcomes) != {"complete", "empty", "insufficient"}
        or len(artifact.outcomes) != 3  # noqa: PLR2004 -- the three declared outcomes
    ):
        return False
    sections = {section.section_id: section for section in page.sections}
    if not {*_ANCHORED_SECTIONS, _FREQUENCY_SECTION} <= set(sections):
        return False
    anchored = all(
        sections[item].declared_periods == OVERLAP_DECLARED_PERIODS
        and sections[item].period_anchor is True
        for item in _ANCHORED_SECTIONS
    )
    frequency = sections[_FREQUENCY_SECTION]
    return (
        anchored
        and frequency.declared_periods == FREQUENCY_DECLARED_PERIODS
        and frequency.period_anchor is False
    )


def _contract(root: Path, slot: InsightSlotId) -> InsightContract:
    for _, contract in load_insight_contracts(root):
        if contract.id is slot:
            return contract
    raise RuntimeError(f"the {slot.value} insight contract is undeclared.")


REACH_MATERIALIZATION_UNAVAILABLE = "reach_materialization_unavailable"
"""The reason while the retained-truth configuration or the v2 page entry is absent."""


class _CampaignCountOnly:
    """Reach's campaign roster names: each key itself, reading nothing (review 2026-10-17 #3).

    Reach frequency only counts the campaigns with spend (`len(roster.entries)`); no campaign
    label reaches the page. So the roster neither refuses on an unnamed campaign nor reads a
    CURRENT label scope a GRAPH pin can't see.
    """

    def public_names(
        self, column: str, values: tuple[str, ...], window: DateRange
    ) -> dict[str, str]:
        """Name every key by itself."""
        del column, window
        return {value: value for value in values}


def retained_reach_preset_artifact_source(  # noqa: PLR0913, PLR0917 -- the closed signature
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort,
    current: BakeSourceReads,
) -> RetainedReachPresetArtifactSource | None:
    """Compose the retained Reach source, or none, logging the closure condition once.

    The closed factory signature (A5.4): the invocation's admitted, captured `queries` and its
    one pinnable CURRENT reader, whose clock every renderer reads. Fails closed without the
    retained-truth configuration, while the v2 page entry `reach` isn't declared, and while the
    governed AMC instance declaration is missing or names an unknown kind
    (`amc_instance_kind_missing`, `amc_instance_kind_unknown`): the kind is never defaulted.
    The baker maps None to the generated `reach_materialization_unavailable`.
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
        return source_closed(REACH_PAGE_ID, "settings_incomplete")
    root = contracts_root()
    policy = reach_policy(load_v2_page_schemas(root))
    if policy is None:
        return source_closed(REACH_PAGE_ID, "page_entry_absent")
    instance = load_amc_instance(root)
    if instance.kind is None:
        return source_closed(REACH_PAGE_ID, instance.reason or "amc_instance_kind_missing")
    contracts = tuple(contract for _, contract in load_contracts(root))
    source_packs = load_source_packs(root)
    bridge = load_commerce_identity_bridge(root)
    horizons = scope_horizon_policy(current)
    reader = cast("CurrentPublicationInspectionPort", current)
    lags = {pack.contract_id: pack.effective_cadence_lag_days for pack in load_query_packs(root)}
    selector = PublicationSelector(
        reader, source_packs, bridge, cadence_lag_days=lags, horizons=horizons
    )
    catalog = load_catalog(root)
    marketplaces = tuple(sorted({mapping.target.value for mapping in bridge.marketplace_mappings}))
    anchoring = tuple(
        item for item in contracts if item.contract_id in {EXPOSURE_CONTRACT, FREQUENCY_PLATFORM}
    )

    def served() -> frozenset[tuple[str, CalendarPeriod]]:
        return frozenset(
            item
            for market in marketplaces
            for item in selector.served_periods(
                ServedPeriodsRequest(anchoring, {MARKETPLACE_SELECTOR: market}, current.clock())
            ).publications
        )

    labels = ad_product_labels(load_media_taxonomy(root))

    compiler = ReachArtifactRenderer(
        catalog=catalog,
        contracts=contracts,
        evidence=PerPackPresentationEvidenceInspection(reader, source_packs, bridge),
        tooling=MeasurementTooling(
            queries=queries,
            publications=selector,
            query_batch_max_concurrency=settings.athena_query_batch_max_concurrency,
            periods=selector,
            rosters=SpendRosterReader(
                catalog=catalog,
                contracts=contracts,
                publications=selector,
                queries=queries,
                labels=labels,
                clock=current.clock,
                names=_CampaignCountOnly(),
            ),
        ),
        marketplaces=marketplaces,
        insight_contracts=(
            _contract(root, InsightSlotId.REACH_OVERLAP),
            _contract(root, InsightSlotId.FULL_FUNNEL_ASSOCIATION),
            _contract(root, InsightSlotId.FREQUENCY_SATURATION),
        ),
        clock=current.clock,
        instance=instance.kind,
        ad_product_labels=labels,
        frequency_lag_days=lags[FREQUENCY_PLATFORM],
    )
    return RetainedReachPresetArtifactSource(
        temporal_policy=policy,
        compiler=compiler,
        served=served,
        cadence_lag_days=lags[EXPOSURE_CONTRACT],
        pointers=pointers,
        clock=current.clock,
        instance=instance.kind,
        frequency_lag_days=lags[FREQUENCY_PLATFORM],
    )
