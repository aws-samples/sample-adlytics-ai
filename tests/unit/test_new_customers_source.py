# SPDX-License-Identifier: MIT-0
"""The New customers materializer: one batch per marketplace, fail-closed registration (ADR-075).

Every figure is fictional and synthetic. The renderer runs the real measurement batch, planner,
pack-coverage attachment and shaping against an in-memory query port, as the Scorecard tests do.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from publication_support import StubPublications

from adapters.config import PresetArtifactBakeSettings
from adapters.inbound.tool_handlers.measurement import MeasurementTooling
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher import baker as baker_module
from adapters.jobs.preset_artifact_publisher import new_customers_source
from adapters.jobs.preset_artifact_publisher.bake_results import (
    BakePublication,
    BakeUnavailable,
)
from adapters.jobs.preset_artifact_publisher.baker import (
    CampaignPresetArtifactBaker,
    NewCustomersPresetArtifactBaker,
    PresetArtifactBakeApplication,
    RegisteredPresetArtifactBaker,
    UnavailableCampaignPresetArtifactSource,
    UnavailableNewCustomersPresetArtifactSource,
    UnavailablePresetArtifactBaker,
)
from adapters.jobs.preset_artifact_publisher.baker import apply_records as apply_bake_records
from adapters.jobs.preset_artifact_publisher.new_customers_source import (
    NewCustomersArtifactRenderer,
    NewCustomersRenderInput,
    NewCustomersStaticLabels,
    RetainedNewCustomersPresetArtifactSource,
    new_customers_bake_plan_count,
    new_customers_coverage_requirements,
    new_customers_labels,
    new_customers_policy,
    retained_new_customers_preset_artifact_source,
)
from adapters.jobs.preset_artifact_publisher.transport import (
    BakeIntent,
    DeclaredBakeIntents,
    load_declared_bake_intents,
)
from adapters.outbound.bake_sources import BakeSourceReads
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.telemetry import tracing
from adapters.outbound.telemetry.generated_spans import PRESET_ARTIFACT_DELIVERY
from adapters.outbound.telemetry.reporting import OperationLog, Report
from domain.measurement.pack_coverage import PackDayVersions
from domain.measurement.planning import measure_alias
from domain.measurement.rosters import Roster, RosterEntry
from domain.presentation.insights import InsightSlotId, InsightSlotState
from domain.presentation.new_customers import (
    NEW_CUSTOMERS_PAGE_ID,
    GatewayState,
    NewBuyersState,
    NewCustomersAbsence,
    NewCustomersAbsenceReason,
    NewCustomersArtifact,
    PerChannel,
    PerChannelState,
    ShareChange,
    new_customers_artifact_payload,
)
from domain.presentation.page_schemas import (
    PageTime,
    PresentationPageSchemaSet,
    PresentationTemporalPolicy,
    TemporalComparisonPolicy,
    TemporalPresetId,
)
from domain.presentation.preset_artifacts import (
    ArtifactBodyWriteResult,
    PresetArtifactPage,
    PresetArtifactPointerAdvanceOutcome,
    PresetArtifactPointerAdvanceResult,
    PresetArtifactPublicationRequest,
    PresetArtifactPublicationResult,
    PresetArtifactVariant,
)
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.time import TimeGrain
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from ports.outbound.pack_coverage import PackCoverageReadRequest
from ports.outbound.preset_artifacts import (
    PresetArtifactPersistencePort,
    PresetArtifactPointerStorePort,
)
from ports.outbound.publication_selection import PresentationEvidenceInspectionPort
from ports.outbound.query import QueryRequest, QueryResult, QueryRow
from ports.outbound.rosters import RosterRequest
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest
from ports.outbound.source_coverage import (
    SourceCoverageBounds,
    SourceCoverageRequest,
    SourceCoverageResult,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-100", "ADR-106")]


def _reads() -> BakeSourceReads:
    """Return a fictional pinnable reader: composition builds over it and never reads."""
    return BakeSourceReads(cast("Any", None), cast("Any", None))


ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT)
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
INSIGHTS = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
LABELS = new_customers_labels(ROOT, CATALOG)
NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)
PERIOD = DateRange(date(2026, 9, 14), date(2026, 9, 20))
PRIOR = DateRange(date(2026, 9, 7), date(2026, 9, 13))
WEEK = CalendarPeriod(PERIOD.start, PERIOD.end, TimeGrain.WEEK)
US = "amazon_us"
UK = "amazon_uk"
CURRENCIES = {US: "USD", UK: "GBP"}
BUYERS = "amc-new-to-brand-buyers-platform"
CHANNEL = "amc-new-to-brand-buyers-channel"
# (channel key, ad product key) of the two delivering ad products.
SP = ("amazon_sponsored_products", "sponsored_products")
SB = ("amazon_sponsored_brands", "sponsored_brands")
DSP = ("amazon_dsp", "amazon_dsp")

# Fictional (current, prior) values. SB delivers but has no attributed-conversion row at all.
_DELIVERY: dict[str, dict[tuple[str, str], tuple[Decimal, Decimal]]] = {
    "ad_spend": {SP: (Decimal(600), Decimal(500)), SB: (Decimal(400), Decimal(400))},
}
_CONVERSION: dict[str, dict[tuple[str, str], tuple[Decimal, Decimal]]] = {
    "purchases_new_to_brand_12mo": {SP: (Decimal(40), Decimal(30))},
    "revenue_new_to_brand_12mo": {SP: (Decimal(1200), Decimal(850))},
    "revenue_attributed": {SP: (Decimal(3000), Decimal(2500))},
}
_GATEWAY_PRODUCTS = {(SP, "amazon_asin:B0GATE0001"): 5, (SP, "amazon_asin:B0GATE0002"): 3}
_BUYERS_COUNT = Decimal(4400)
_WEEK_SPEND = Decimal(5500)
_CHANNEL_BUYERS: dict[tuple[str, str], Decimal] = {SP: Decimal(300), SB: Decimal(120)}
"""Fictional per-ad-product new buyers at grain set channel; never added to the platform tile."""
_CHANNEL_SPEND: dict[tuple[str, str], Decimal] = {
    SP: Decimal(3000),
    SB: Decimal(1200),
    DSP: Decimal(900),
}
"""Fictional per-ad-product spend over the week, the D-6 numerator of each row's cost."""


@dataclass
class NewCustomersQueries:
    """Deterministic grouped rows per governed plan; every figure is fictional."""

    period: DateRange
    marketplaces: tuple[str, ...] = (US,)
    buyers: Decimal | None = _BUYERS_COUNT
    """The platform buyers cell; None is the stored `not_returned` cell."""
    channel_buyers: dict[tuple[str, str], Decimal] = field(
        default_factory=lambda: dict(_CHANNEL_BUYERS)
    )
    """The per-ad-product rows the channel grain set returned; a missing key came back no row."""
    channel_currency: str | None = None
    """The currency the per-channel cost answer carries; None is the marketplace's own."""
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def _channel_rows(self, request: QueryRequest, market: str) -> list[QueryRow]:
        """Rows of the channel grain set: one per returned ad product, never a total."""
        plan = request.plan.logical
        rows: list[QueryRow] = []
        for media, buyers in self.channel_buyers.items():
            row: dict[str, str | int | Decimal | None] = {
                "channel_key": media[0],
                "ad_product_key": media[1],
            }
            for column in plan.carried_columns:
                row[column] = self.channel_currency or CURRENCIES[market]
            if plan.join is not None:
                row[measure_alias(0)] = _CHANNEL_SPEND[media]
                row[measure_alias(1)] = buyers
            else:
                row[measure_alias(0)] = buyers
            rows.append(row)
        return rows

    @staticmethod
    def _column(name: str, media: tuple[str, str], current: bool) -> Decimal | None:
        index = 0 if current else 1
        for table in (_DELIVERY, _CONVERSION):
            if name in table:
                value = table[name].get(media)
                return None if value is None else value[index]
        aliases = {"spend": "ad_spend", "attributed_revenue_14d": "revenue_attributed"}
        if name in aliases:
            return NewCustomersQueries._column(aliases[name], media, current)
        raise AssertionError(f"no fictional value for {name}")

    def _media_rows(self, request: QueryRequest, market: str) -> list[QueryRow]:
        plan = request.plan.logical
        current = plan.window == self.period
        delivered = plan.join is not None or plan.metric.name == "ad_spend"
        roster = tuple(_DELIVERY["ad_spend"]) if delivered else (SP,)
        rows: list[QueryRow] = []
        for media in roster:
            row: dict[str, str | int | Decimal | None] = {
                "ecosystem_key": "amazon",
                "platform_key": "amazon_ads",
                "channel_key": media[0],
                "ad_product_key": media[1],
                "marketplace_key": market,
            }
            for column in plan.carried_columns:
                row[column] = CURRENCIES[market]
            if plan.join is not None:
                row[measure_alias(0)] = self._column(plan.metric.numerator, media, current)
                row[measure_alias(1)] = self._column(plan.metric.denominator, media, current)
            else:
                for index, column in enumerate(plan.measures):
                    row[measure_alias(index)] = self._column(column, media, current)
            rows.append({key: row[key] for key in (*plan.group_by, *row) if key in row})
        return rows

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        plan = request.plan.logical
        market = plan.filters["marketplace_key"]
        rows: list[QueryRow]
        periodic = plan.join.filters if plan.join is not None else plan.filters
        if periodic.get("grain_set") == "channel":
            rows = self._channel_rows(request, market)
        elif plan.metric.name == "purchases_new_to_brand_incl_halo_12mo":
            by_product = "amazon_asin" in plan.group_by
            totals: dict[tuple[object, ...], int] = {}
            for (media, asin), count in _GATEWAY_PRODUCTS.items():
                key = (*media, asin) if by_product else media
                totals[key] = totals.get(key, 0) + count
            rows = [
                cast(
                    "QueryRow",
                    {
                        **dict(zip(plan.group_by, key, strict=True)),
                        measure_alias(0): Decimal(count),
                    },
                )
                for key, count in totals.items()
            ]
        elif plan.group_by == ("marketplace_key",):
            row: dict[str, str | int | Decimal | None] = {"marketplace_key": market}
            for column in plan.carried_columns:
                row[column] = CURRENCIES[market]
            if plan.join is not None:
                row[measure_alias(0)] = _WEEK_SPEND
                row[measure_alias(1)] = self.buyers
            else:
                row[measure_alias(0)] = (
                    _WEEK_SPEND if plan.metric.name == "ad_spend" else self.buyers
                )
            rows = [row]
        else:
            rows = self._media_rows(request, market)
        return QueryResult(
            rows=tuple(rows), bytes_scanned=1_024, truncated=False, suppression=Suppression(False)
        )


@dataclass
class _Coverage:
    """Every day of every window at one pack version, or with chosen days overridden."""

    version: int = 2
    overrides: dict[date, int | None] = field(default_factory=dict[date, int | None])

    def pack_coverage(self, request: PackCoverageReadRequest) -> tuple[PackDayVersions, ...]:
        days = {
            window.start + timedelta(days=offset): self.version
            for window in request.windows
            for offset in range(window.days)
        }
        return tuple(
            PackDayVersions(item.contract_id, {**days, **self.overrides})
            for item in request.contracts
        )


@dataclass
class _Served:
    periods: frozenset[CalendarPeriod] = frozenset({WEEK})
    channel_periods: frozenset[CalendarPeriod] | None = None
    """The per-channel pack's published periods; None publishes the platform pack's."""

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        del request
        channel = self.periods if self.channel_periods is None else self.channel_periods
        return ServedPeriods(
            frozenset(
                {
                    *((BUYERS, period) for period in self.periods),
                    *((CHANNEL, period) for period in channel),
                }
            ),
            {BUYERS: 2, CHANNEL: 2},
        )


_ROSTER_LABELS = {SP[1]: "Sponsored Products", SB[1]: "Sponsored Brands", DSP[1]: "Amazon DSP"}


@dataclass
class _Rosters:
    """The ad products with spend in the period; every request reads the same roster."""

    keys: tuple[str, ...] = (SP[1], SB[1])
    seen: list[RosterRequest] = field(default_factory=list[RosterRequest])

    def roster(self, request: RosterRequest) -> Roster:
        self.seen.append(request)
        return Roster(
            request.kind,
            request.period,
            request.marketplace_key,
            request.platform_key,
            tuple(RosterEntry(key, _ROSTER_LABELS[key]) for key in self.keys),
        )


def _renderer(  # noqa: PLR0913 — the query port and six keyword stand-ins
    queries: NewCustomersQueries,
    *,
    coverage: _Coverage | None = None,
    served: _Served | None = None,
    labels: NewCustomersStaticLabels = LABELS,
    publications: StubPublications | None = None,
    rosters: _Rosters | None = None,
    now: datetime = NOW,
) -> NewCustomersArtifactRenderer:
    stub = publications or StubPublications()
    return NewCustomersArtifactRenderer(
        catalog=CATALOG,
        contracts=CONTRACTS,
        evidence=cast("PresentationEvidenceInspectionPort", stub),
        tooling=MeasurementTooling(
            queries=queries,
            publications=stub,
            periods=served or _Served(),
            pack_coverage=coverage or _Coverage(),
            rosters=rosters or _Rosters(),
        ),
        labels=labels,
        insight_contract=INSIGHTS[InsightSlotId.PORTFOLIO_ACQUISITION],
        clock=lambda: now,
    )


ROLLING = NewCustomersRenderInput(TemporalPresetId.SEVEN_DAYS, PERIOD, prior_period=PRIOR)
CALENDAR = NewCustomersRenderInput(
    TemporalPresetId.LAST_COMPLETE_WEEK, PERIOD, calendar_period=WEEK
)


def _rendered(request: NewCustomersRenderInput = ROLLING, **kwargs: object) -> NewCustomersArtifact:
    result = _renderer(NewCustomersQueries(PERIOD), **kwargs).render_new_customers(request)  # type: ignore[arg-type]
    assert result.artifact is not None
    assert result.publication_identities
    return result.artifact


def test_the_bake_budget_counts_the_largest_batch_a_bake_executes() -> None:
    # A calendar bake with every section published executes exactly the counted plans.
    calendar = NewCustomersQueries(PERIOD)
    _renderer(calendar).render_new_customers(CALENDAR)
    assert (
        _rendered(CALENDAR).markets[0].new_buyers.per_channel is not None
    )  # the per-channel table was planned
    assert len(calendar.seen) == new_customers_bake_plan_count(ROOT)
    # Control: a rolling bake plans no new buyers, so it executes fewer than the bound.
    rolling = NewCustomersQueries(PERIOD)
    _renderer(rolling).render_new_customers(ROLLING)
    assert 0 < len(rolling.seen) < new_customers_bake_plan_count(ROOT)


def test_a_rolling_bake_carries_the_planned_step_1_figures_and_their_absences() -> None:
    artifact = _rendered()
    (market,) = artifact.markets
    assert (market.marketplace_key, market.currency_code) == (US, "USD")
    sp, sb = market.new_to_brand
    assert (sp.ad_product_key, sp.ad_product_label, sp.channel_group_label) == (
        "sponsored_products",
        "Sponsored Products",
        "Search",
    )
    assert (sp.ntb_purchases.value, sp.ntb_revenue.value) == (Decimal(40), Decimal(1200))
    assert sp.ntb_revenue_share.value == Decimal("0.4")
    assert sp.cost_per_ntb_purchase.value == Decimal(15)
    change = sp.ntb_revenue_share_change
    assert isinstance(change, ShareChange)
    assert change.points == Decimal("6.0")
    # SB delivered with no conversion row: true zeros and the two zero texts.
    assert (sb.ntb_purchases.value, sb.ntb_revenue.value) == (Decimal(0), Decimal(0))
    assert sb.ntb_revenue_share.absence is not None
    assert sb.ntb_revenue_share.absence.reason is NewCustomersAbsenceReason.NO_ATTRIBUTED_REVENUE
    assert sb.cost_per_ntb_purchase.absence is not None
    assert sb.cost_per_ntb_purchase.absence.reason is NewCustomersAbsenceReason.NO_NTB_PURCHASES
    # Rolling preset: the new-buyers callout, no tiles.
    assert market.new_buyers.state is NewBuyersState.OUTSIDE_DECLARED_PERIODS


def test_gateway_products_come_from_the_halo_metric_per_channel_never_across() -> None:
    (market,) = _rendered().markets
    sp, sb = market.gateway
    assert sp.state is GatewayState.ROWS
    assert [(row.asin, row.ntb_purchases, row.share_of_channel) for row in sp.rows] == [
        ("B0GATE0001", 5, Decimal(5) / Decimal(8)),
        ("B0GATE0002", 3, Decimal(3) / Decimal(8)),
    ]
    # No effective title is governed in this fixture, so the product shows its ASIN.
    assert [row.title for row in sp.rows] == ["B0GATE0001", "B0GATE0002"]
    assert (sb.state, sb.text) == (
        GatewayState.NO_NTB_PURCHASES,
        "No new-to-brand purchases were credited to Sponsored Brands in this window.",
    )


def test_a_material_share_mover_fills_portfolio_acquisition_on_a_rolling_preset() -> None:
    (slot,) = _rendered().insights
    assert slot.state is InsightSlotState.FILLED
    assert slot.text == (
        "Sponsored Products new-to-brand share rose 6 points. Inside Sponsored Products only, "
        "Sep 14\u201320 compared with Sep 7\u201313."
    )


def test_a_rolling_bake_names_its_window_as_the_date_control_does_and_a_calendar_its_period() -> (
    None
):
    # ADR-120: one calendar form for a reporting window, the date control's.
    rolling = new_customers_artifact_payload(_rendered())
    assert (rolling["period_label"], rolling["scope_text"], rolling["page_context_label"]) == (
        "Sep 14\u201320, 2026",
        "Showing Sep 14\u201320, 2026 (7 days) compared with Sep 7\u201313. "
        "Data through Sep 20, 2026.",
        "New customers \u00b7 Sep 14\u201320, 2026",
    )
    assert rolling["starter_questions"] == [
        "Which channel had the highest new-to-brand share in Sep 14\u201320, 2026?"
    ]
    # Control: a calendar preset reads its server period label and asks no comparison.
    calendar = new_customers_artifact_payload(_rendered(CALENDAR))
    assert (calendar["period_label"], calendar["scope_text"]) == (
        "Week of 14\u201320 Sep 2026",
        "Showing Week of 14\u201320 Sep 2026 (Latest measured week). "
        "No comparison for calendar periods.",
    )


def test_a_calendar_bake_shows_new_buyers_and_no_comparison() -> None:
    artifact = _rendered(CALENDAR)
    (market,) = artifact.markets
    section = market.new_buyers
    assert section.state is NewBuyersState.FIGURES
    assert section.new_buyers is not None and section.cost_per_new_buyer is not None
    assert (section.new_buyers.value, section.cost_per_new_buyer.value) == (
        _BUYERS_COUNT,
        _WEEK_SPEND / _BUYERS_COUNT,
    )
    for row in market.new_to_brand:
        change = row.ntb_revenue_share_change
        assert not isinstance(change, ShareChange)
        assert change.reason is NewCustomersAbsenceReason.CALENDAR_PERIOD_SELECTED
    (slot,) = artifact.insights
    assert slot.state is InsightSlotState.SILENT
    assert artifact.period_label == "Week of 14\u201320 Sep 2026"


def test_a_not_returned_buyers_cell_bakes_both_tiles_as_too_few_shoppers() -> None:
    queries = NewCustomersQueries(PERIOD, buyers=None)
    result = _renderer(queries).render_new_customers(CALENDAR)
    assert result.artifact is not None
    (market,) = result.artifact.markets
    section = market.new_buyers
    assert section.state is NewBuyersState.FIGURES
    assert section.new_buyers is not None and section.cost_per_new_buyer is not None
    assert [
        (tile.metric, tile.value, tile.absence)
        for tile in (section.new_buyers, section.cost_per_new_buyer)
    ] == [
        (
            "buyers_new_to_brand_12mo",
            None,
            NewCustomersAbsence(
                NewCustomersAbsenceReason.NOT_RETURNED, "too few shoppers, or none"
            ),
        ),
        (
            "cost_per_new_to_brand_buyer_12mo",
            None,
            NewCustomersAbsence(
                NewCustomersAbsenceReason.NOT_RETURNED, "too few shoppers, or none"
            ),
        ),
    ]
    # Control: the returned cell bakes both figures through the same path.
    returned = _rendered(CALENDAR).markets[0].new_buyers
    assert returned.new_buyers is not None and returned.cost_per_new_buyer is not None
    assert (returned.new_buyers.value, returned.cost_per_new_buyer.value) == (
        _BUYERS_COUNT,
        _WEEK_SPEND / _BUYERS_COUNT,
    )


def test_a_late_new_buyers_delivery_shows_its_pending_text_and_holds_nothing_else_back() -> None:
    queries = NewCustomersQueries(PERIOD)
    result = _renderer(queries, served=_Served(frozenset())).render_new_customers(CALENDAR)
    assert result.artifact is not None
    (market,) = result.artifact.markets
    assert (market.new_buyers.state, market.new_buyers.text) == (
        NewBuyersState.MISSING,
        "Figures for Week of 14\u201320 Sep 2026 didn't arrive.",
    )
    assert market.new_buyers.expected_after == date(2026, 9, 22)
    assert {request.plan.logical.metric.name for request in queries.seen}.isdisjoint(
        {"buyers_new_to_brand_12mo", "cost_per_new_to_brand_buyer_12mo"}
    )
    assert len(market.new_to_brand) == 2


def test_an_uncovered_window_bakes_every_channel_s_coverage_text_and_plans_no_gateway() -> None:
    queries = NewCustomersQueries(PERIOD)
    coverage = _Coverage(overrides={PERIOD.start: 1})
    result = _renderer(queries, coverage=coverage).render_new_customers(ROLLING)
    assert result.artifact is not None
    (market,) = result.artifact.markets
    assert {entry.state for entry in market.gateway} == {GatewayState.NOT_COVERED}
    assert {entry.first_covered_date for entry in market.gateway} == {date(2026, 9, 15)}
    assert "purchases_new_to_brand_incl_halo_12mo" not in {
        request.plan.logical.metric.name for request in queries.seen
    }
    # Positive control: the covered window plans the gateway's two answers.
    covered = NewCustomersQueries(PERIOD)
    _renderer(covered).render_new_customers(ROLLING)
    assert [
        request.plan.logical.group_by
        for request in covered.seen
        if request.plan.logical.metric.name == "purchases_new_to_brand_incl_halo_12mo"
    ] == [("channel_key", "ad_product_key", "amazon_asin"), ("channel_key", "ad_product_key")]


def test_each_marketplace_is_its_own_batch_and_section_in_its_own_currency() -> None:
    labels = replace(LABELS, marketplaces=(UK, US))
    queries = NewCustomersQueries(PERIOD, marketplaces=(UK, US))
    result = _renderer(queries, labels=labels).render_new_customers(ROLLING)
    assert result.artifact is not None
    assert [(item.marketplace_key, item.currency_code) for item in result.artifact.markets] == [
        (UK, "GBP"),
        (US, "USD"),
    ]
    assert {request.plan.logical.filters["marketplace_key"] for request in queries.seen} == {UK, US}


def test_a_missing_step_1_publication_bakes_nothing() -> None:
    publications = StubPublications(inspection_absent_indices=frozenset({0}))
    result = _renderer(NewCustomersQueries(PERIOD), publications=publications).render_new_customers(
        ROLLING
    )
    assert result.artifact is None


def test_an_artifact_invariant_break_is_reported_and_bakes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported: list[tuple[BaseException, Report]] = []

    def record(error: BaseException, report: Report) -> None:
        reported.append((error, report))

    monkeypatch.setattr(new_customers_source, "report_error", record)
    # Positive control: an ordinary bake reports nothing.
    assert _rendered() is not None
    assert reported == []

    def contradicting(_item: object) -> object:
        raise ValueError("a new-to-brand share is planned exactly when attributed revenue isn't 0.")

    monkeypatch.setattr(new_customers_source, "step_one_row", contradicting)
    result = _renderer(NewCustomersQueries(PERIOD)).render_new_customers(ROLLING)
    assert result.artifact is None
    ((error, report),) = reported
    assert isinstance(error, ValueError)
    assert (report.operation, report.correlation_id) == (
        "new_customers_artifact_render",
        "new-customers-artifact",
    )


# --- Per-channel new buyers (v2, manifest revision 2026-10-09.2) ----------------------------------

_NOT_RETURNED = NewCustomersAbsence(
    NewCustomersAbsenceReason.NOT_RETURNED, "too few shoppers, or none"
)


def _per_channel(
    queries: NewCustomersQueries | None = None, **kwargs: object
) -> tuple[PerChannel, NewCustomersQueries]:
    queries = queries or NewCustomersQueries(PERIOD)
    result = _renderer(queries, **kwargs).render_new_customers(CALENDAR)  # type: ignore[arg-type]
    assert result.artifact is not None
    (market,) = result.artifact.markets
    assert market.new_buyers.state is NewBuyersState.FIGURES
    table = market.new_buyers.per_channel
    assert table is not None
    return table, queries


def _cells(table: PerChannel) -> list[tuple[str, object, object]]:
    return [
        (
            row.ad_product_key,
            row.new_buyers.value if row.new_buyers.absence is None else row.new_buyers.absence,
            row.cost_per_new_buyer.value
            if row.cost_per_new_buyer.absence is None
            else row.cost_per_new_buyer.absence,
        )
        for row in table.rows
    ]


def test_per_channel_rows_equal_the_planned_answers_at_grain_set_channel_and_carry_no_total() -> (
    None
):
    table, queries = _per_channel()
    assert table.state is PerChannelState.ROWS
    assert _cells(table) == [
        ("sponsored_products", Decimal(300), Decimal(3000) / Decimal(300)),
        ("sponsored_brands", Decimal(120), Decimal(1200) / Decimal(120)),
    ]
    assert [(row.ad_product_label, row.channel_group_label) for row in table.rows] == [
        ("Sponsored Products", "Search"),
        ("Sponsored Brands", "Search"),
    ]
    assert table.note is None
    # Read at grain set channel, grouped by ad product; never derived from the platform row.
    channel_plans = [
        request.plan.logical
        for request in queries.seen
        if (request.plan.logical.join or request.plan.logical).filters.get("grain_set") == "channel"
    ]
    assert [(plan.metric.name, plan.group_by) for plan in channel_plans] == [
        ("buyers_new_to_brand_12mo", ("channel_key", "ad_product_key")),
        ("cost_per_new_to_brand_buyer_12mo", ("channel_key", "ad_product_key")),
    ]
    # The platform tile is its own answer: 300 + 120 is not 4,400 and nothing adds them.
    (market,) = _rendered(CALENDAR).markets
    assert market.new_buyers.new_buyers is not None
    assert market.new_buyers.new_buyers.value == _BUYERS_COUNT
    payload = new_customers_artifact_payload(_rendered(CALENDAR))
    assert "total" not in json.dumps(payload["markets"][0]["new_buyers"]["per_channel"])  # type: ignore[index]


def test_a_rostered_ad_product_with_no_row_bakes_not_available_and_a_returned_one_its_figures() -> (
    None
):
    queries = NewCustomersQueries(PERIOD, channel_buyers={SP: Decimal(300)})
    table, _ = _per_channel(queries)
    assert _cells(table) == [
        ("sponsored_products", Decimal(300), Decimal(10)),
        ("sponsored_brands", _NOT_RETURNED, _NOT_RETURNED),
    ]
    # One of two roster entries missing is 50% loss: graded degraded, never withdrawn.
    assert table.note == "Some breakdowns had too few shoppers and aren't shown."
    # Control: every rostered ad product returned is not degraded.
    assert _per_channel()[0].note is None


def _sp_spend_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only Sponsored Products spent in the period, so step 1 lists it alone (NC-GATE-103)."""
    monkeypatch.setitem(_DELIVERY, "ad_spend", {SP: _DELIVERY["ad_spend"][SP]})


def test_a_returned_ad_product_off_the_roster_is_kept_with_the_no_spend_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Off the roster, Sponsored Brands had no ad spend in the period: step 1 doesn't list it.
    _sp_spend_only(monkeypatch)
    table, _ = _per_channel(rosters=_Rosters(keys=(SP[1],)))
    assert _cells(table) == [
        ("sponsored_products", Decimal(300), Decimal(10)),
        (
            "sponsored_brands",
            Decimal(120),
            NewCustomersAbsence(
                NewCustomersAbsenceReason.NO_AD_SPEND,
                "No ad spend for Sponsored Brands in Week of 14\u201320 Sep 2026.",
            ),
        ),
    ]
    assert table.note is None


def test_an_empty_roster_with_no_row_reads_no_channels_and_one_returned_row_reads_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = NewCustomersQueries(PERIOD, channel_buyers={})
    table, _ = _per_channel(queries, rosters=_Rosters(keys=()))
    assert (table.state, table.text, table.rows, table.note) == (
        PerChannelState.NO_CHANNELS,
        "No channel had ad spend in Week of 14\u201320 Sep 2026.",
        (),
        None,
    )
    # Paired: an empty roster and one returned row lists that row. With no ad spend, Sponsored
    # Brands isn't in step 1 either.
    _sp_spend_only(monkeypatch)
    one = NewCustomersQueries(PERIOD, channel_buyers={SB: Decimal(120)})
    listed, _ = _per_channel(one, rosters=_Rosters(keys=()))
    assert listed.state is PerChannelState.ROWS
    assert [row.ad_product_key for row in listed.rows] == ["sponsored_brands"]


def test_a_per_channel_cost_in_another_currency_is_reported_and_bakes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported: list[BaseException] = []

    def record(error: BaseException, _report: Report) -> None:
        reported.append(error)

    monkeypatch.setattr(new_customers_source, "report_error", record)
    # Positive control: the cost answer in the marketplace's currency bakes and reports nothing.
    table, _ = _per_channel(NewCustomersQueries(PERIOD, channel_currency="USD"))
    assert [
        None if (units := row.cost_per_new_buyer.units) is None else units.currency_code
        for row in table.rows
    ] == ["USD", "USD"]
    assert reported == []
    # The cost answer's own currency labels it, so a mismatch reaches the one-currency rule.
    result = _renderer(NewCustomersQueries(PERIOD, channel_currency="EUR")).render_new_customers(
        CALENDAR
    )
    assert result.artifact is None
    (error,) = reported
    assert isinstance(error, ValueError)
    assert str(error) == "a marketplace section is in one currency."


def test_a_rostered_per_channel_row_step_1_does_not_list_is_reported_and_bakes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NC-GATE-103: the bake refuses the row the browser's decoder refuses, and alarms."""
    reported: list[BaseException] = []

    def record(error: BaseException, _report: Report) -> None:
        reported.append(error)

    monkeypatch.setattr(new_customers_source, "report_error", record)
    # Positive control: Sponsored Brands spent and is rostered, so step 1 lists its row.
    _per_channel()
    assert reported == []
    # Rostered with a row back, while step 1, read from ad spend, doesn't list it.
    _sp_spend_only(monkeypatch)
    assert _renderer(NewCustomersQueries(PERIOD)).render_new_customers(CALENDAR).artifact is None
    (error,) = reported
    assert isinstance(error, ValueError)
    assert str(error) == "a per-channel row is a step-1 ad product, or one with no ad spend."


_NO_NEW_BUYERS = NewCustomersAbsence(
    NewCustomersAbsenceReason.NO_NEW_BUYERS,
    "No new buyers for Sponsored Brands in Week of 14\u201320 Sep 2026.",
)


def test_a_returned_ad_product_with_spend_and_0_new_buyers_bakes_no_new_buyers() -> None:
    """Its planned cost divides by zero: the cost cell reads why, and the rest still bakes."""
    queries = NewCustomersQueries(PERIOD, channel_buyers={SP: Decimal(300), SB: Decimal(0)})
    result = _renderer(queries).render_new_customers(CALENDAR)
    assert result.artifact is not None
    (market,) = result.artifact.markets
    table = market.new_buyers.per_channel
    assert table is not None
    assert _cells(table) == [
        # Paired: the row with new buyers keeps its numeric cost.
        ("sponsored_products", Decimal(300), Decimal(10)),
        ("sponsored_brands", Decimal(0), _NO_NEW_BUYERS),
    ]
    assert table.note is None
    # Nothing else is held back: the tiles, step 1 and the gateway bake as without the zero.
    assert market.new_buyers.new_buyers is not None
    assert market.new_buyers.new_buyers.value == _BUYERS_COUNT
    assert len(market.new_to_brand) == 2
    assert market.gateway == _rendered(CALENDAR).markets[0].gateway
    # Paired: a rostered ad product that genuinely didn't come back still reads not_returned.
    missing, _ = _per_channel(NewCustomersQueries(PERIOD, channel_buyers={SP: Decimal(300)}))
    assert _cells(missing)[1] == ("sponsored_brands", _NOT_RETURNED, _NOT_RETURNED)


def test_platform_tiles_with_spend_and_0_new_buyers_bake_no_new_buyers() -> None:
    result = _renderer(NewCustomersQueries(PERIOD, buyers=Decimal(0))).render_new_customers(
        CALENDAR
    )
    assert result.artifact is not None
    (market,) = result.artifact.markets
    section = market.new_buyers
    assert section.new_buyers is not None and section.cost_per_new_buyer is not None
    assert [
        (tile.value, tile.absence) for tile in (section.new_buyers, section.cost_per_new_buyer)
    ] == [
        (Decimal(0), None),
        (
            None,
            NewCustomersAbsence(
                NewCustomersAbsenceReason.NO_NEW_BUYERS,
                "No new buyers in Week of 14\u201320 Sep 2026.",
            ),
        ),
    ]
    # The per-channel table under the tiles still bakes its own rows.
    assert section.per_channel is not None
    assert section.per_channel.state is PerChannelState.ROWS
    # Paired: new buyers above 0 give a numeric cost; a cell not returned reads not_returned.
    returned = _rendered(CALENDAR).markets[0].new_buyers.cost_per_new_buyer
    assert returned is not None and returned.value == _WEEK_SPEND / _BUYERS_COUNT
    absent = _renderer(NewCustomersQueries(PERIOD, buyers=None)).render_new_customers(CALENDAR)
    assert absent.artifact is not None
    tile = absent.artifact.markets[0].new_buyers.cost_per_new_buyer
    assert tile is not None and tile.absence == _NOT_RETURNED


def test_a_per_channel_delivery_not_landed_shows_its_own_text_while_the_tiles_render() -> None:
    late = _Served(channel_periods=frozenset())
    table, queries = _per_channel(served=late)
    # NOW is 23 Sep, past 22 Sep, so the week's per-channel delivery is missing.
    assert (table.state, table.text, table.expected_after) == (
        PerChannelState.MISSING,
        "Figures per channel for Week of 14\u201320 Sep 2026 didn't arrive.",
        date(2026, 9, 22),
    )
    assert not any(
        (request.plan.logical.join or request.plan.logical).filters.get("grain_set") == "channel"
        for request in queries.seen
    )
    # Pending through its expected date, even with an empty roster: never no_channels before it
    # lands.
    pending = _renderer(
        NewCustomersQueries(PERIOD, channel_buyers={}),
        served=late,
        rosters=_Rosters(keys=()),
        now=datetime(2026, 9, 22, 12, tzinfo=UTC),
    ).render_new_customers(CALENDAR)
    assert pending.artifact is not None
    early = pending.artifact.markets[0].new_buyers
    assert early.per_channel is not None
    assert (early.per_channel.state, early.per_channel.text) == (
        PerChannelState.PENDING,
        "Figures per channel for Week of 14\u201320 Sep 2026 are expected after 22 Sep 2026.",
    )
    assert early.new_buyers is not None and early.new_buyers.value == _BUYERS_COUNT


def test_the_per_channel_plans_carry_the_roster_read_for_the_period_and_marketplace() -> None:
    rosters = _Rosters()
    _per_channel(rosters=rosters)
    # Buyers and cost per buyer share the period's roster: it is read once per marketplace.
    assert [(item.kind.value, item.period, item.marketplace_key) for item in rosters.seen] == [
        ("ad_product", PERIOD, US)
    ]
    two = _Rosters()
    _renderer(
        NewCustomersQueries(PERIOD, marketplaces=(UK, US)),
        labels=replace(LABELS, marketplaces=(UK, US)),
        rosters=two,
    ).render_new_customers(CALENDAR)
    assert sorted(item.marketplace_key for item in two.seen) == sorted((UK, US))
    # Control: a rolling bake plans no per-channel figure, so it reads no roster.
    idle = _Rosters()
    _renderer(NewCustomersQueries(PERIOD), rosters=idle).render_new_customers(ROLLING)
    assert idle.seen == []


# --- Registration: fails closed until the UI adds the page entry ---------------------------------

POLICY = PresentationTemporalPolicy(
    candidate_presets=(
        TemporalPresetId.SEVEN_DAYS,
        TemporalPresetId.THIRTY_DAYS,
        TemporalPresetId.LAST_COMPLETE_WEEK,
        TemporalPresetId.LAST_COMPLETE_MONTH,
    ),
    # OD-UX-6 (2026-10-22): New customers opens on Last full week.
    default_preset=TemporalPresetId.LAST_COMPLETE_WEEK,
    custom_enabled=False,
    custom_min_duration_days=1,
    custom_max_duration_days=90,
    comparison_policy=TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR,
    display_grains=(PageTime.PERIOD,),
)
"""The `new-customers` v2 entry's time policy, as the hand-off spec gives it to the UI."""


@dataclass
class _SourceCoverage:
    intervals: tuple[DateRange, ...]

    def source_coverage(self, request: SourceCoverageRequest) -> SourceCoverageResult:
        del request
        return SourceCoverageResult(
            (),
            SourceCoverageBounds(
                self.intervals,
                self.intervals[0].start if self.intervals else None,
                self.intervals[-1].end if self.intervals else None,
            ),
            None,
        )


@dataclass
class _Pointers:
    def current(self, page: PresetArtifactPage, variant: PresetArtifactVariant) -> None:
        del page, variant


def _settings() -> PresetArtifactBakeSettings:
    return PresetArtifactBakeSettings(
        artifact_bucket="fictional-artifacts",
        pointer_table="fictional-pointers",
        semantic_publications_table="fictional-table",
        semantic_v2_bucket="fictional-bucket",
        athena_workgroup="fictional-workgroup",
        semantic_v2_current_database="fictional-database",
        athena_result_reuse_max_age_minutes=1,
        athena_query_batch_max_concurrency=1,
    )


def test_the_materializer_is_registered_but_fails_closed_without_the_page_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The composition root builds real clients, which need a region of their own; never one
    # an earlier test happened to leave behind (the suite runs in random order).
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    committed = load_v2_page_schemas(ROOT)
    # Positive control: the committed entry (UI slice 9, item B3) opens the source.
    assert new_customers_policy(committed) == POLICY
    schemas = replace(
        committed,
        pages=tuple(page for page in committed.pages if page.page_id != NEW_CUSTOMERS_PAGE_ID),
    )
    assert new_customers_policy(schemas) is None
    # The composition root reads the committed entry, so it composes the source.
    composed = retained_new_customers_preset_artifact_source(
        _settings(), cast("Any", None), cast("Any", None), _reads()
    )
    assert isinstance(composed, RetainedNewCustomersPresetArtifactSource)
    baker = RegisteredPresetArtifactBaker(
        bakers={
            "campaigns": CampaignPresetArtifactBaker(UnavailableCampaignPresetArtifactSource()),
            "new-customers": NewCustomersPresetArtifactBaker(
                UnavailableNewCustomersPresetArtifactSource()
            ),
        },
        fallback=UnavailablePresetArtifactBaker(),
        roster_pages=("campaigns", "new-customers"),
    )
    result = baker.bake(BakeIntent(NEW_CUSTOMERS_PAGE_ID, "7d"))
    assert isinstance(result, BakeUnavailable)
    assert result.reason == "new_customers_materialization_unavailable"
    # The page id alone doesn't open it: a `new-customers` entry declaring another page's
    # artifact (here Retail health's, copied) keeps the source closed.
    page = next(item for item in schemas.pages if item.page_id == "retail-health")
    # The entry's calendar presets need temporal control version 2, which the UI raises with it.
    injected = replace(
        schemas,
        temporal_control_version=2,
        pages=(
            *schemas.pages,
            replace(
                page,
                page_id=NEW_CUSTOMERS_PAGE_ID,
                display_name="New customers",
                route="/new-customers/",
                time_policy=POLICY,
            ),
        ),
    )
    assert new_customers_policy(injected) is None


def test_only_an_entry_declaring_new_customers_v2_and_its_baked_sections_opens_the_source(
    tmp_path: pathlib.Path,
) -> None:
    schemas = load_v2_page_schemas(_declared_root(tmp_path))
    page = next(item for item in schemas.pages if item.page_id == NEW_CUSTOMERS_PAGE_ID)
    (artifact,) = page.artifacts
    assert (artifact.artifact_id, artifact.artifact_type, artifact.version) == (
        "new_customers",
        "NewCustomersArtifact",
        2,
    )

    def policy(**changes: object) -> PresentationTemporalPolicy | None:
        changed = replace(page, **changes)  # type: ignore[arg-type]
        others = tuple(item for item in schemas.pages if item.page_id != NEW_CUSTOMERS_PAGE_ID)
        return new_customers_policy(replace(schemas, pages=(*others, changed)))

    # Positive control: the hand-off entry opens the source.
    assert policy() == POLICY
    step_two = next(item for item in page.sections if item.section_id == "new-to-brand-step-2")
    refused = {
        "wrong type": {"artifacts": (replace(artifact, artifact_type="RetailHealthArtifact"),)},
        "wrong version": {"artifacts": (replace(artifact, version=1),)},
        "wrong id": {"artifacts": (replace(artifact, artifact_id="retail_health"),)},
        "an outcome missing": {"artifacts": (replace(artifact, outcomes=("complete", "empty")),)},
        "a second artifact": {
            "artifacts": (artifact, replace(artifact, artifact_id="new_customers_extra"))
        },
        "a baked section missing": {
            "sections": tuple(
                item for item in page.sections if item.section_id != "gateway-products"
            )
        },
        "step 2 anchoring the page": {
            "sections": tuple(
                replace(item, period_anchor=True) if item is step_two else item
                for item in page.sections
            )
        },
    }
    assert {name: policy(**changes) for name, changes in refused.items()} == dict.fromkeys(refused)


def test_the_composition_root_opens_once_the_page_entry_is_declared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    root = _declared_root(tmp_path)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    def declared(_root: pathlib.Path) -> PresentationPageSchemaSet:
        return load_v2_page_schemas(root)

    monkeypatch.setattr(new_customers_source, "load_v2_page_schemas", declared)
    source = retained_new_customers_preset_artifact_source(
        _settings(),
        cast("PresetArtifactPointerStorePort", _Pointers()),
        cast("Any", None),
        _reads(),
    )
    assert isinstance(source, RetainedNewCustomersPresetArtifactSource)
    assert source.temporal_policy == POLICY
    # Control: the same declaration without the retained-truth configuration stays closed.
    unconfigured = replace(_settings(), semantic_publications_table="")
    assert (
        retained_new_customers_preset_artifact_source(
            unconfigured,
            cast("PresetArtifactPointerStorePort", _Pointers()),
            cast("Any", None),
            _reads(),
        )
        is None
    )


def _source(coverage_end: date = PERIOD.end) -> RetainedNewCustomersPresetArtifactSource:
    return RetainedNewCustomersPresetArtifactSource(
        coverage=_SourceCoverage((DateRange(date(2026, 8, 1), coverage_end),)),
        coverage_requirements=new_customers_coverage_requirements(CATALOG),
        temporal_policy=POLICY,
        compiler=_renderer(NewCustomersQueries(PERIOD)),
        pointers=cast("object", _Pointers()),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


def test_an_injected_page_entry_bakes_a_publication_whose_figures_equal_the_planned_answers() -> (
    None
):
    baker = RegisteredPresetArtifactBaker(
        bakers={
            "campaigns": CampaignPresetArtifactBaker(UnavailableCampaignPresetArtifactSource()),
            "new-customers": NewCustomersPresetArtifactBaker(_source()),
        },
        fallback=UnavailablePresetArtifactBaker(),
        roster_pages=("campaigns", "new-customers"),
    )
    result = baker.bake(BakeIntent(NEW_CUSTOMERS_PAGE_ID, "7d"))
    assert isinstance(result, BakePublication)
    body = json.loads(result.request.body.payload_json)
    assert (body["type"], body["version"], body["preset"]) == ("NewCustomersArtifact", 2, "7d")
    assert body["period"] == {"start": "2026-09-14", "end": "2026-09-20"}
    (market,) = body["markets"]
    sp = market["new_to_brand"]["rows"][0]
    assert (sp["ntb_purchases"]["value"], sp["ntb_revenue"]["value"]) == ("40", "1200")
    assert sp["ntb_revenue"]["display_value"] == "1200.00"
    assert sp["ntb_revenue_share"]["value"] == "0.4000"
    assert sp["ntb_revenue_share"]["display_text"] == "40.00%"
    assert sp["cost_per_ntb_purchase"]["value"] == "15.0000"
    assert sp["cost_per_ntb_purchase"]["display_value"] == "15.00"
    assert [row["ntb_purchases"] for row in market["gateway"][0]["rows"]] == [5, 3]
    # A calendar preset resolves P on daily coverage alone.
    assert _source().resolve(TemporalPresetId.LAST_COMPLETE_WEEK) == CALENDAR


def test_a_last_complete_week_variant_bakes_the_step_2_tiles() -> None:
    baker = RegisteredPresetArtifactBaker(
        bakers={
            "campaigns": CampaignPresetArtifactBaker(UnavailableCampaignPresetArtifactSource()),
            "new-customers": NewCustomersPresetArtifactBaker(_source()),
        },
        fallback=UnavailablePresetArtifactBaker(),
        roster_pages=("campaigns", "new-customers"),
    )
    result = baker.bake(BakeIntent(NEW_CUSTOMERS_PAGE_ID, "last_complete_week"))
    assert isinstance(result, BakePublication)
    assert result.request.body.variant == PresetArtifactVariant("last_complete_week")
    body = json.loads(result.request.body.payload_json)
    assert (body["preset"], body["period"], body["prior_period"]) == (
        "last_complete_week",
        {"start": "2026-09-14", "end": "2026-09-20"},
        None,
    )
    (market,) = body["markets"]
    section = market["new_buyers"]
    assert (section["state"], section["text"]) == ("figures", None)
    assert (section["new_buyers"]["value"], section["cost_per_new_buyer"]["value"]) == (
        "4400",
        "1.2500",
    )
    # Control: the rolling variant of the same page shows the callout instead of the tiles.
    rolling = baker.bake(BakeIntent(NEW_CUSTOMERS_PAGE_ID, "7d"))
    assert isinstance(rolling, BakePublication)
    rolled = json.loads(rolling.request.body.payload_json)
    assert rolled["markets"][0]["new_buyers"]["state"] == "outside_declared_periods"


HANDOFF_ENTRY = """\
  - id: new-customers
    display_name: New customers
    route: /new-customers/
    purpose: Placeholder page purpose; the UI sub-slice writes the page copy.
    primary_job: Placeholder primary job; the UI sub-slice writes the page copy.
    mode: selectable
    boundary_required: true
    compatibility_routes: []
    temporal_policy:
      candidate_presets: [7d, 30d, last_complete_week, last_complete_month]
      default_preset: last_complete_week
      custom: {enabled: false, min_duration_days: 1, max_duration_days: 90}
      comparison_policy: optional_equal_prior
      display_grains: [period]
    artifacts:
      - id: new_customers
        type: NewCustomersArtifact
        version: 2
        outcomes: [complete, empty, insufficient]
    visuals: []
    sections:
      - {id: workspace-scope, cardinality: exactly_one}
      - {id: new-to-brand, cardinality: exactly_one}
      - {id: new-customers-insights, cardinality: exactly_one}
      - id: new-to-brand-step-2
        cardinality: exactly_one
        declared_periods: [last_complete_week, last_complete_month]
        period_anchor: false
      - {id: gateway-products, cardinality: exactly_one}
      - {id: pointers, cardinality: exactly_one}
      - {id: evidence-trace, cardinality: exactly_one}
      - {id: boundary, cardinality: exactly_one}
    actions: []
"""
"""The v2 entry as `docs/ui/new-customers/low-level-design.md` declares it, minus the ADR-115
`navigation` block this branch's loader doesn't read yet."""

COMMITTED_INTENTS = (
    *(("campaigns", preset) for preset in ("1d", "7d", "14d", "30d")),
    *(("channel-performance", preset) for preset in ("1d", "7d", "14d", "30d")),
    *(("workspace", preset) for preset in ("1d", "7d", "14d", "30d")),
    *(("data-health", preset) for preset in ("1d", "7d", "14d", "30d")),
    *(("media-efficiency", preset) for preset in ("1d", "7d", "14d", "30d")),
    *(("retail-health", preset) for preset in ("1d", "7d", "14d")),
    *(
        ("scorecard", preset)
        for preset in ("7d", "30d", "last_complete_week", "last_complete_month")
    ),
    *(
        ("reach", preset)
        for preset in ("last_complete_week", "last_complete_month", "last_complete_quarter")
    ),
    ("retention", "latest_with_current_publication"),
    ("audiences", "last_complete_month"),
)


def _declared_root(_tmp_path: pathlib.Path) -> pathlib.Path:
    """Return the committed declaration: it carries the hand-off entry since UI slice 9 (B3)."""
    return ROOT


def test_the_committed_entry_is_the_hand_off_entry_plus_its_navigation_and_starter() -> None:
    page = next(
        item for item in load_v2_page_schemas(ROOT).pages if item.page_id == NEW_CUSTOMERS_PAGE_ID
    )
    assert (page.route, page.temporal_policy) == ("/new-customers/", POLICY)
    assert [section.section_id for section in page.sections] == [
        "workspace-scope",
        "new-to-brand",
        "new-customers-insights",
        "new-to-brand-step-2",
        "gateway-products",
        "pointers",
        "evidence-trace",
        "boundary",
    ]
    navigation = page.navigation
    assert navigation is not None
    assert (navigation.hub_id, navigation.tab_label, navigation.order) == (
        "customers",
        "New customers",
        1,
    )
    assert navigation.tab_description == (
        "Shoppers buying from your brand for the first time in 12 months"
    )


@dataclass
class _Persistence:
    published: list[PresetArtifactPublicationRequest] = field(
        default_factory=list[PresetArtifactPublicationRequest]
    )

    def publish(self, request: PresetArtifactPublicationRequest) -> PresetArtifactPublicationResult:
        self.published.append(request)
        body = request.body
        return PresetArtifactPublicationResult(
            ArtifactBodyWriteResult(
                body.page, body.variant, body.revision, body.key, body.digest, None
            ),
            PresetArtifactPointerAdvanceResult(PresetArtifactPointerAdvanceOutcome.ADVANCED),
        )


def test_the_hand_off_entry_composes_the_application_and_its_calendar_variants_bake(
    tmp_path: pathlib.Path,
) -> None:
    root = _declared_root(tmp_path)
    declared = load_declared_bake_intents(root)
    # Every other page's intents are unchanged, in order, beside the four New customers adds.
    assert [
        (item.page, item.variant) for item in declared.intents if item.page != NEW_CUSTOMERS_PAGE_ID
    ] == list(COMMITTED_INTENTS)
    assert [item.variant for item in declared.intents if item.page == NEW_CUSTOMERS_PAGE_ID] == [
        "7d",
        "30d",
        "last_complete_week",
        "last_complete_month",
    ]
    assert new_customers_policy(load_v2_page_schemas(root)) == POLICY
    persistence = _Persistence()
    application = PresetArtifactBakeApplication(
        declared,
        RegisteredPresetArtifactBaker(
            bakers={
                "campaigns": CampaignPresetArtifactBaker(UnavailableCampaignPresetArtifactSource()),
                "new-customers": NewCustomersPresetArtifactBaker(_source()),
            },
            fallback=UnavailablePresetArtifactBaker(),
            roster_pages=("campaigns", "new-customers"),
        ),
        cast("PresetArtifactPersistencePort", persistence),
    )
    week = BakeIntent(NEW_CUSTOMERS_PAGE_ID, "last_complete_week")
    assert application.apply(week.serialize()) is not None
    ((request,),) = (persistence.published,)
    assert (request.body.page.value, request.body.variant.value) == (
        NEW_CUSTOMERS_PAGE_ID,
        "last_complete_week",
    )
    # Control: an undeclared calendar variant of the same page is acknowledged and bakes nothing.
    assert (
        application.apply(BakeIntent(NEW_CUSTOMERS_PAGE_ID, "last_complete_quarter").serialize())
        is None
    )
    assert len(persistence.published) == 1


def test_a_preset_the_page_doesn_t_declare_bakes_nothing() -> None:
    assert _source().publication_for(PresetArtifactVariant("14d")) is None
    # Positive control: a declared preset over covered days bakes.
    assert _source().publication_for(PresetArtifactVariant("7d")) is not None


def test_a_new_customers_bake_is_found_by_page_through_its_log_line_and_its_own_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Page key -> terminal operation log -> (execution_id, trace_id, span_id) -> delivery span.

    The publish spans declare no page attribute, so the page key reaches a span only through the
    terminal log line, which the baker emits inside that record's delivery span.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "TRACER", provider.get_tracer("test"))
    lines: list[dict[str, object]] = []

    def record(event: OperationLog, _logger: object) -> None:
        lines.append({**event.details, "outcome": event.outcome, **tracing.current_trace_ids()})

    monkeypatch.setattr(baker_module, "report_operation", record)
    ours = BakeIntent(NEW_CUSTOMERS_PAGE_ID, "7d")
    other = BakeIntent("retail-health", "7d")
    application = PresetArtifactBakeApplication(
        DeclaredBakeIntents((ours, other)),
        RegisteredPresetArtifactBaker(
            bakers={
                "campaigns": CampaignPresetArtifactBaker(UnavailableCampaignPresetArtifactSource()),
                "new-customers": NewCustomersPresetArtifactBaker(_source()),
            },
            fallback=UnavailablePresetArtifactBaker(),
            roster_pages=("campaigns", "new-customers"),
        ),
        cast("PresetArtifactPersistencePort", _Persistence()),
    )
    failures = apply_bake_records(
        {
            "Records": [
                {"messageId": "one", "body": ours.serialize()},
                {"messageId": "two", "body": other.serialize()},
            ]
        },
        application,
    )
    assert failures == ()
    found = [line for line in lines if line["page"] == NEW_CUSTOMERS_PAGE_ID]
    (line,) = found
    assert (line["variant"], line["outcome"]) == ("7d", "published")
    deliveries = {
        f"{item.context.span_id:016x}": (f"{item.context.trace_id:032x}", item)
        for item in exporter.get_finished_spans()
        if item.name == PRESET_ARTIFACT_DELIVERY and item.context is not None
    }
    trace_id, span = deliveries[cast("str", line["span_id"])]
    assert trace_id == line["trace_id"]
    assert (span.attributes or {})["execution_id"] == line["execution_id"]
    # Control: the batch's other page shares the execution_id but resolves to its own span, and
    # the page query never returns its line.
    (theirs,) = [item for item in lines if item["page"] == "retail-health"]
    assert theirs["execution_id"] == line["execution_id"]
    assert theirs["span_id"] != line["span_id"]
    assert theirs["span_id"] in deliveries
    assert theirs not in found
