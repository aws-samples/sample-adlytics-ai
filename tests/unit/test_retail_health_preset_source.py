# SPDX-License-Identifier: MIT-0
"""Retained Retail Health preset baking: reduction, reconciliation, revision, and bounds."""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from publication_support import PublicationSpec, StubPublications, successful_publication

from adapters.config import PresetArtifactBakeSettings
from adapters.inbound.tool_handlers.measurement import DEFAULT_MAX_ROWS, MeasurementTooling
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher import retail_source as retail_source_module
from adapters.jobs.preset_artifact_publisher.baker import (
    BakePublication,
    RetailHealthPresetArtifactBaker,
)
from adapters.jobs.preset_artifact_publisher.insight_fill import PresetInsightFillChild
from adapters.jobs.preset_artifact_publisher.retail_source import (
    RETAIL_HEALTH_BOUNDARY,
    RETAIL_HEALTH_PRESET_RENDER_FORMAT,
    CanonicalRetailProductHierarchy,
    CanonicalRetailReportedTotals,
    RetailHealthArtifactRenderer,
    RetailHealthCompiler,
    RetailHealthRenderInput,
    RetailHealthRenderResult,
    RetailProductHierarchyResult,
    RetailProductHierarchySource,
    RetailProductIdentity,
    RetailReportedTotals,
    RetailReportedTotalsSource,
    RetainedRetailHealthPresetArtifactSource,
    retail_health_coverage_requirements,
    retained_retail_health_preset_artifact_source,
)
from adapters.jobs.preset_artifact_publisher.transport import BakeIntent
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.source_pack import load_source_packs
from adapters.retail_insight_requests import (
    RetailInsightRequestDerivationInput,
    retail_insight_request_derivation,
    retail_payload_without_ephemera,
)
from domain.measurement.planning import measure_alias
from domain.presentation.compared_metrics import presentation_labels
from domain.presentation.currency_display import without_display_values
from domain.presentation.evidence_status import CoverageState, write_evidence_status
from domain.presentation.insights import (
    InsightContinuationRequest,
    InsightEvidenceInput,
    InsightModelRoute,
    InsightPromptActivation,
    InsightPromptTier,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    unavailable_insight_slots,
)
from domain.presentation.preset_artifacts import (
    MAX_PRESET_ARTIFACT_BYTES,
    RETAIL_BODY_CEILING,
    PresetArtifactBody,
    PresetArtifactBodyCeilingError,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactPointerAdvanceResult,
    PresetArtifactProductChildIdentity,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactVariant,
    SourceUnchanged,
    canonical_preset_artifact_json,
)
from domain.presentation.retail_health import (
    RETAIL_HEALTH_INSIGHT_IDS,
    RETAIL_HEALTH_MAX_ROWS,
    RETAIL_HEALTH_METRIC_UNITS,
    RETAIL_HEALTH_ROW_METRICS,
    RETAIL_HEALTH_TOTAL_METRICS,
    RETAIL_HEALTH_TREND_METRICS,
    RetailComparison,
    RetailDistributorView,
    RetailHealthArtifact,
    RetailHealthOutcome,
    retail_health_artifact_from_payload,
    retail_health_artifact_payload,
    retail_health_coverage_facts,
)
from domain.presentation.retail_health_custody import (
    RetailProductChildDigests,
    retail_health_custody_manifest_from_payload,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from ports.outbound.canonical import CanonicalDatasetReadPort
from ports.outbound.preset_artifacts import PresetArtifactPointerStorePort
from ports.outbound.publication_selection import PresentationEvidenceInspectionPort
from ports.outbound.query import QueryRequest, QueryResult, QueryRow
from ports.outbound.semantic_state import CurrentPublicationPort
from ports.outbound.source_coverage import (
    SourceCoverageBounds,
    SourceCoverageRequest,
    SourceCoverageRequirement,
    SourceCoverageResult,
)

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
METRIC_CATALOG = load_catalog(ROOT)
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
INSIGHT_CONTRACTS = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
SOURCE_PACKS = load_source_packs(ROOT)
BRIDGE = load_commerce_identity_bridge(ROOT)
V2_SCHEMAS = load_v2_page_schemas(ROOT)
RETAIL_POLICY = next(
    schema for schema in V2_SCHEMAS.pages if schema.page_id == "retail-health"
).temporal_policy

PAGE = PresetArtifactPage("retail-health")
VARIANT = PresetArtifactVariant("7d")
NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
PERIOD = DateRange(date(2026, 8, 13), date(2026, 8, 19))
PRIOR = DateRange(date(2026, 8, 6), date(2026, 8, 12))
MARKETPLACE = "amazon_us"
COVERAGE = (DateRange(date(2026, 8, 1), date(2026, 8, 21)),)
REQUIREMENT = SourceCoverageRequirement((("vendor_central", "GET_VENDOR_SALES_REPORT"),))
CURRENT_MULTIPLIER = {
    "retail_revenue_shipped": Decimal(10),
    "units_shipped": Decimal(2),
    "customer_returns": Decimal(1),
    "glance_views": Decimal(5),
    # The per-product margin operand: current margin per product is base x (10 - 4) > 0.
    "cogs_shipped": Decimal(4),
    # Product detail's ordered operands (ADR-125): ordered revenue and units per product.
    "revenue_ordered": Decimal(11),
    "units_ordered": Decimal(3),
    # Ad-attributed purchases and revenue per product and channel (Amazon Marketing Cloud).
    "purchases": Decimal(2),
    "attributed_revenue_14d": Decimal(6),
}


CHANNEL_LABELS = {
    "amazon_dsp": "Amazon DSP",
    "amazon_sponsored_products": "Sponsored Products",
}


def _canonical_asin(index: int) -> str:
    # Canonical product keys are lowercase semantic keys; the display ASIN is the hierarchy's.
    return f"amazon_asin:fict{index:05d}"


def _products(count: int) -> tuple[str, ...]:
    return tuple(_canonical_asin(index + 1) for index in range(count))


def _hierarchy_result(products: tuple[str, ...]) -> RetailProductHierarchyResult:
    return RetailProductHierarchyResult(
        {
            asin: RetailProductIdentity(
                asin.removeprefix("amazon_asin:").upper(),
                f"PARENT{index:05d}",
                f"Fictional Product {index:05d}",
            )
            for index, asin in enumerate(products, start=1)
        },
        ("dim-product-publication-1",),
    )


def _expected_product_revenue(count: int) -> Decimal:
    multiplier = CURRENT_MULTIPLIER["retail_revenue_shipped"]
    return sum((Decimal(index) * multiplier for index in range(1, count + 1)), Decimal(0))


def _expected_product_units(count: int) -> Decimal:
    return sum(
        (Decimal(index) * CURRENT_MULTIPLIER["units_shipped"] for index in range(1, count + 1)),
        Decimal(0),
    )


@dataclass
class FakeHierarchy:
    result: RetailProductHierarchyResult | None
    windows: list[DateRange] = field(default_factory=list[DateRange])

    def hierarchy(self, window: DateRange) -> RetailProductHierarchyResult | None:
        self.windows.append(window)
        return self.result


@dataclass
class FakeReported:
    result: RetailReportedTotals | None
    windows: list[DateRange] = field(default_factory=list[DateRange])

    def reported_totals(self, window: DateRange) -> RetailReportedTotals | None:
        self.windows.append(window)
        return self.result


@dataclass
class RetailQueries:
    """Deterministic grouped rows per governed plan shape; values are fictional."""

    products: tuple[str, ...]
    period: DateRange
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        plan = request.plan.logical
        group = plan.group_by
        is_current = plan.window == self.period
        product_axis: tuple[str | None, ...] = self.products if "amazon_asin" in group else (None,)
        date_axis: tuple[date | None, ...] = (
            tuple(plan.window.start + timedelta(days=offset) for offset in range(plan.window.days))
            if "date_key" in group
            else (None,)
        )
        # Product detail's one ad-attributed query groups by channel too (ADR-125).
        channel_axis: tuple[str | None, ...] = (
            tuple(CHANNEL_LABELS) if "channel_key" in group else (None,)
        )
        rows: list[QueryRow] = []
        for asin, day, channel in (
            (asin, day, channel)
            for asin in product_axis
            for day in date_axis
            for channel in channel_axis
        ):
            row: dict[str, str | int | Decimal | None] = {"marketplace_key": MARKETPLACE}
            if asin is not None:
                row["amazon_asin"] = asin
            if day is not None:
                row["date_key"] = day.isoformat()
            if channel is not None:
                row["channel_key"] = channel
            for column in plan.carried_columns:
                row[column] = "USD"
            if plan.join is not None:
                row[measure_alias(0)] = Decimal(20)
                row[measure_alias(1)] = Decimal(1000)
            else:
                row[measure_alias(0)] = self._value(
                    plan.metric.name, (asin, day), is_current=is_current
                )
            rows.append(row)
        truncated = len(rows) > request.max_rows
        return QueryResult(
            rows=tuple(rows[: request.max_rows]),
            bytes_scanned=1_024,
            truncated=truncated,
            suppression=Suppression(False),
        )

    def _value(
        self, metric: str, key: tuple[str | None, date | None], *, is_current: bool
    ) -> Decimal:
        asin, day = key
        if asin is None and day is None:
            # A window total over the single marketplace.
            return Decimal(100) if is_current else Decimal(40)
        if asin is None:
            # A daily trend value over the single marketplace.
            return Decimal(2) if is_current else Decimal(1)
        base = Decimal(self.products.index(asin) + 1)
        if day is not None:
            # One shipped-revenue spark point per product per day.
            return base
        multiplier = CURRENT_MULTIPLIER[metric]
        return base * multiplier if is_current else base


def _renderer(
    queries: RetailQueries,
    hierarchy: FakeHierarchy,
    reported: FakeReported,
) -> RetailHealthArtifactRenderer:
    publications = StubPublications()
    return RetailHealthArtifactRenderer(
        catalog=METRIC_CATALOG,
        contracts=CONTRACTS,
        evidence=cast("PresentationEvidenceInspectionPort", publications),
        tooling=MeasurementTooling(
            queries=queries,
            publications=publications,
        ),
        hierarchy=cast("RetailProductHierarchySource", hierarchy),
        reported=cast("RetailReportedTotalsSource", reported),
        clock=lambda: NOW,
        movers_silence_reason=(
            "No supplied product movement exceeds the declared materiality threshold."
        ),
        commerce_scope=BRIDGE.commerce_scope,
        channel_labels=CHANNEL_LABELS,
    )


def _rendered(
    count: int,
    reported: RetailReportedTotals | None = None,
) -> RetailHealthRenderResult:
    products = _products(count)
    queries = RetailQueries(products, PERIOD)
    hierarchy = FakeHierarchy(_hierarchy_result(products))
    reported_totals = FakeReported(
        reported
        if reported is not None
        else RetailReportedTotals(
            _expected_product_revenue(count),
            _expected_product_units(count),
            ("reported-total-publication-1",),
        )
    )
    renderer = _renderer(queries, hierarchy, reported_totals)
    return renderer.render_retail_health(RetailHealthRenderInput(PERIOD, PRIOR))


@pytest.mark.covers("ADR-070")
def test_renderer_builds_a_complete_reconciled_artifact_from_retained_truth() -> None:
    """Matching by-product sums and reported control totals reconcile without figures."""
    rendered = _rendered(2)

    artifact = rendered.artifact
    assert artifact is not None
    assert artifact.outcome is RetailHealthOutcome.COMPLETE
    assert artifact.distributor_view is RetailDistributorView.MANUFACTURING
    assert artifact.boundary == RETAIL_HEALTH_BOUNDARY
    assert tuple(item.metric for item in artifact.totals) == RETAIL_HEALTH_TOTAL_METRICS
    for comparison in artifact.totals:
        assert comparison.current.units.kind is RETAIL_HEALTH_METRIC_UNITS[comparison.metric]
        assert comparison.current.grain.dimensions == ("period", "marketplace")
        assert comparison.current.period == PERIOD
        assert comparison.prior.period == PRIOR
    assert artifact.absences == ()
    reconciliation = artifact.reconciliation
    assert reconciliation is not None
    assert reconciliation.outcome == "reconciled"
    assert reconciliation.as_of == PERIOD.end
    assert reconciliation.product_revenue is None
    # Rows are server-ordered by current shipped revenue, largest first.
    assert tuple(row.child_asin for row in artifact.rows) == ("FICT00002", "FICT00001")
    assert artifact.rows[0].parent_asin == "PARENT00002"
    assert artifact.rows[0].title == "Fictional Product 00002"
    for row in artifact.rows:
        assert tuple(item.metric for item in row.comparisons) == RETAIL_HEALTH_ROW_METRICS
        assert all(
            item.current.grain.dimensions == ("period", "product", "marketplace")
            for item in row.comparisons
        )
        assert len(row.shipped_revenue_spark) == PERIOD.days
    assert artifact.total_count == 2
    assert artifact.truncation is None
    assert tuple(point.day for point in artifact.trend) == tuple(
        PERIOD.start + timedelta(days=offset) for offset in range(PERIOD.days)
    )
    assert all(
        len(point.current) == len(RETAIL_HEALTH_TREND_METRICS)
        and len(point.prior) == len(RETAIL_HEALTH_TREND_METRICS)
        and all(value.grain.dimensions == ("date", "marketplace") for value in point.current)
        for point in artifact.trend
    )
    assert tuple(slot.id for slot in artifact.insights) == RETAIL_HEALTH_INSIGHT_IDS
    assert (
        tuple(slot.state for slot in artifact.insights[:3]) == (InsightSlotState.UNAVAILABLE,) * 3
    )
    movers = artifact.insights[3]
    assert movers.state is InsightSlotState.FILLED
    assert movers.prompt_version == "deterministic-v1"
    assert movers.text is not None
    assert "Fictional Product 00002 (FICT00002)" in movers.text
    revision = PresetArtifactRevision.from_publication_identities(
        rendered.publication_identities,
        render_format=RETAIL_HEALTH_PRESET_RENDER_FORMAT,
    )
    assert all(slot.artifact_revision == revision.value for slot in artifact.insights)
    assert "dim-product-publication-1" in rendered.publication_identities
    assert "reported-total-publication-1" in rendered.publication_identities


@pytest.mark.covers("ADR-070")
def test_fourteen_day_product_series_is_chunked_below_its_query_row_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paired: 2,520 grouped rows render; every one-day query keeps the 500-row ceiling."""
    products = _products(45)
    period = DateRange(date(2026, 8, 6), date(2026, 8, 19))
    prior = DateRange(date(2026, 7, 23), date(2026, 8, 5))
    queries = RetailQueries(products, period)
    renderer = _renderer(
        queries,
        FakeHierarchy(_hierarchy_result(products)),
        FakeReported(
            RetailReportedTotals(
                _expected_product_revenue(45),
                _expected_product_units(45),
                ("reported-total-publication-1",),
            )
        ),
    )

    rendered = renderer.render_retail_health(RetailHealthRenderInput(period, prior))

    assert rendered.artifact is not None and len(rendered.artifact.rows) == 45
    spark = [
        request
        for request in queries.seen
        if {"date_key", "amazon_asin"} <= set(request.plan.logical.group_by)
    ]
    assert len(spark) == 2 * 2 * period.days
    assert sum(request.plan.logical.window.days for request in spark) * len(products) > 500
    assert all(
        request.plan.logical.window.days == 1 and request.max_rows == DEFAULT_MAX_ROWS
        for request in spark
    )

    # Paired failing control: one unchunked 14-day request truncates at the refusing
    # measurement-answer layer, so the renderer returns no partial artifact.
    def _single_window(window: DateRange) -> tuple[DateRange, ...]:
        return (window,)

    monkeypatch.setattr(retail_source_module, "_daily_windows", _single_window)
    control_queries = RetailQueries(products, period)
    refused = _renderer(
        control_queries,
        FakeHierarchy(_hierarchy_result(products)),
        FakeReported(
            RetailReportedTotals(
                _expected_product_revenue(45),
                _expected_product_units(45),
                ("reported-total-publication-1",),
            )
        ),
    ).render_retail_health(RetailHealthRenderInput(period, prior))
    assert refused.artifact is None
    assert any(
        request.plan.logical.window.days == period.days and request.max_rows == DEFAULT_MAX_ROWS
        for request in control_queries.seen
    )


@pytest.mark.covers("ADR-119")
def test_a_bake_serves_every_tile_complete_in_the_coverage_block() -> None:
    """A bake reads only answers covered end to end, so every tile's coverage is complete."""
    artifact = _rendered(2).artifact
    assert artifact is not None
    status = artifact.evidence_status
    assert status is not None

    assert status.window == PERIOD
    assert tuple(entry.slot for entry in status.entries) == tuple(
        item.metric for item in artifact.totals
    )
    assert all(entry.state is CoverageState.COMPLETE for entry in status.entries)
    assert status.entry("customer_returns").label == "Customer returns (units)"
    assert status.entry("customer_returns").role_label == "Headline and product rows"
    assert status.entry("revenue_ordered").role_label == "Headline"
    payload = retail_health_artifact_payload(artifact)
    assert retail_health_artifact_payload(retail_health_artifact_from_payload(payload)) == payload


def _withheld(comparison: RetailComparison, metrics: frozenset[str]) -> RetailComparison:
    if comparison.metric not in metrics:
        return comparison
    return dataclasses.replace(
        comparison, absolute_change=None, percentage_change=None, change=None, withheld=True
    )


@pytest.mark.covers("ADR-119", "ADR-118")
def test_a_partial_returns_day_withholds_those_changes_on_tiles_and_rows() -> None:
    """Fixture: the source sent the last day's returns for one product only (unit-proven)."""
    artifact = _rendered(2).artifact
    assert artifact is not None
    partial = frozenset({"customer_returns", "return_rate", "returns_value_estimate_retail"})
    facts = tuple(
        dataclasses.replace(
            fact, partial_dates=(PERIOD.end,), partial_entities=("Fictional Product 00001",)
        )
        if fact.slot in partial
        else fact
        for fact in retail_health_coverage_facts(
            tuple(item.metric for item in artifact.totals), presentation_labels(METRIC_CATALOG)
        )
    )
    variant = dataclasses.replace(
        artifact,
        totals=tuple(_withheld(item, partial) for item in artifact.totals),
        rows=tuple(
            dataclasses.replace(
                row, comparisons=tuple(_withheld(item, partial) for item in row.comparisons)
            )
            for row in artifact.rows
        ),
        evidence_status=write_evidence_status(PERIOD, facts),
    )

    payload = retail_health_artifact_payload(variant)
    decoded = retail_health_artifact_from_payload(payload)
    assert decoded.evidence_status == variant.evidence_status
    assert retail_health_artifact_payload(decoded) == payload
    status = variant.evidence_status
    assert status is not None
    assert status.entry("customer_returns").absence_text == "1 partial day."
    assert status.entry("customer_returns").coverage_text == (
        "Aug 19 counts the products that reported"
    )
    assert status.entry("return_rate").coverage_text == "Aug 13\u201318, 6 of 7 days"
    assert status.entry("customer_returns").missing_text == "Aug 19 (Fictional Product 00001)"
    returns = next(item for item in payload["totals"] if item["metric"] == "customer_returns")  # type: ignore[union-attr]
    assert returns["change"] is None and returns["absolute_change"] is None
    with pytest.raises(ValueError, match="withheld exactly"):
        dataclasses.replace(variant, evidence_status=artifact.evidence_status)


def test_renderer_reconstructs_the_derived_returns_and_margin_slots_from_operands() -> None:
    """Every derived figure is reduced from measured operands, exact at both grains."""
    rendered = _rendered(2)
    artifact = rendered.artifact
    assert artifact is not None
    # Per-product columns: for FICT00002 current returns 2 / units 4 = 0.5, and
    # margin 20 - 8 = 12 currency at the row grain.
    largest = artifact.rows[0]
    by_metric = {item.metric: item for item in largest.comparisons}
    row_return_rate = by_metric["return_rate"]
    assert row_return_rate.current.units.kind.value == "ratio"
    assert row_return_rate.current.grain.dimensions == ("period", "product", "marketplace")
    assert Decimal(row_return_rate.current.value) == Decimal("0.5000")
    row_margin = by_metric["amazon_margin_proxy"]
    assert row_margin.current.units.kind.value == "currency"
    assert row_margin.current.units.currency_code == "USD"
    assert Decimal(row_margin.current.value) == Decimal(12)
    # Totals reconstruct from measured operand totals (all 100 current in this fixture):
    # return rate 100/100 = 1, margin 100 - 100 = 0, fulfilment 100/100 = 1, and the
    # returns estimate 100 x (100/100) = 100, all at the total grain.
    totals_by_metric = {item.metric: item for item in artifact.totals}
    for metric in ("return_rate", "fulfilment_rate"):
        assert totals_by_metric[metric].current.units.kind.value == "ratio"
        assert totals_by_metric[metric].current.grain.dimensions == ("period", "marketplace")
        assert Decimal(totals_by_metric[metric].current.value) == Decimal("1.0000")
    assert Decimal(totals_by_metric["amazon_margin_proxy"].current.value) == Decimal(0)
    assert totals_by_metric["amazon_margin_proxy"].current.units.currency_code == "USD"
    estimate = totals_by_metric["returns_value_estimate_retail"]
    assert Decimal(estimate.current.value) == Decimal("100.0000")
    assert estimate.current.units.kind.value == "currency"
    assert estimate.current.units.currency_code == "USD"


def test_renderer_reports_a_dated_discrepancy_when_control_totals_differ() -> None:
    """A mismatch never blends silently; both figures render with the window date."""
    rendered = _rendered(
        2,
        reported=RetailReportedTotals(
            _expected_product_revenue(2) + Decimal(1),
            _expected_product_units(2),
            ("reported-total-publication-1",),
        ),
    )

    artifact = rendered.artifact
    assert artifact is not None
    reconciliation = artifact.reconciliation
    assert reconciliation is not None
    assert reconciliation.outcome == "discrepancy"
    assert reconciliation.as_of == PERIOD.end
    assert reconciliation.product_revenue == _expected_product_revenue(2)
    assert reconciliation.reported_revenue == _expected_product_revenue(2) + Decimal(1)
    assert reconciliation.product_units == _expected_product_units(2)
    assert reconciliation.reported_units == _expected_product_units(2)


@pytest.mark.covers("ADR-070")
def test_renderer_bounds_displayed_rows_at_fifty_with_a_boundless_total() -> None:
    """Totals and reconciliation cover the population; only display rows are bounded."""
    population = RETAIL_HEALTH_MAX_ROWS + 11
    rendered = _rendered(population)

    artifact = rendered.artifact
    assert artifact is not None
    assert len(artifact.rows) == RETAIL_HEALTH_MAX_ROWS
    assert artifact.truncation == RETAIL_HEALTH_MAX_ROWS
    assert artifact.total_count == population
    assert artifact.rows[0].child_asin == f"FICT{population:05d}"
    reconciliation = artifact.reconciliation
    assert reconciliation is not None
    assert reconciliation.outcome == "reconciled"


def test_renderer_refuses_a_population_without_complete_product_identity() -> None:
    products = _products(2)
    queries = RetailQueries(products, PERIOD)
    hierarchy = FakeHierarchy(_hierarchy_result(products[:1]))
    reported = FakeReported(
        RetailReportedTotals(
            _expected_product_revenue(2), _expected_product_units(2), ("reported-1",)
        )
    )

    rendered = _renderer(queries, hierarchy, reported).render_retail_health(
        RetailHealthRenderInput(PERIOD, PRIOR)
    )

    assert rendered.artifact is None


def test_renderer_refuses_when_reported_control_totals_are_unavailable() -> None:
    products = _products(1)
    queries = RetailQueries(products, PERIOD)

    rendered = _renderer(
        queries, FakeHierarchy(_hierarchy_result(products)), FakeReported(None)
    ).render_retail_health(RetailHealthRenderInput(PERIOD, PRIOR))

    assert rendered.artifact is None


@dataclass
class Coverage:
    intervals: tuple[DateRange, ...] = COVERAGE
    calls: list[SourceCoverageRequest] = field(default_factory=list[SourceCoverageRequest])

    def source_coverage(self, request: SourceCoverageRequest) -> SourceCoverageResult:
        self.calls.append(request)
        return SourceCoverageResult(
            contributors=(),
            bounds=SourceCoverageBounds(
                intervals=self.intervals,
                earliest_start=None if not self.intervals else self.intervals[0].start,
                latest_end=None if not self.intervals else self.intervals[-1].end,
            ),
            latest_loaded_at=None,
        )


@dataclass
class Pointers:
    pointer: PresetArtifactPointer | None = None
    reads: list[tuple[PresetArtifactPage, PresetArtifactVariant]] = field(
        default_factory=list[tuple[PresetArtifactPage, PresetArtifactVariant]]
    )

    def current(
        self, page: PresetArtifactPage, variant: PresetArtifactVariant
    ) -> PresetArtifactPointer | None:
        self.reads.append((page, variant))
        return self.pointer

    def advance(
        self, request: PresetArtifactPointerAdvanceRequest
    ) -> PresetArtifactPointerAdvanceResult:
        del request
        raise AssertionError("the retail source must never advance a pointer itself")


@dataclass
class Compiler:
    identities: tuple[str, ...] = ("publication-b", "publication-a")
    unavailable: bool = False
    received: list[RetailHealthRenderInput] = field(default_factory=list[RetailHealthRenderInput])

    def render_retail_health(self, request: RetailHealthRenderInput) -> RetailHealthRenderResult:
        self.received.append(request)
        if self.unavailable:
            return RetailHealthRenderResult(None, ())
        return RetailHealthRenderResult(
            RetailHealthArtifact(
                outcome=RetailHealthOutcome.EMPTY,
                period=request.period,
                prior_period=request.prior_period,
                distributor_view=RetailDistributorView.MANUFACTURING,
                totals=(),
                absences=(),
                insights=unavailable_insight_slots(RETAIL_HEALTH_INSIGHT_IDS),
                trend=(),
                rows=(),
                total_count=0,
                truncation=None,
                reconciliation=None,
                boundary=RETAIL_HEALTH_BOUNDARY,
            ),
            self.identities,
        )


def _source(
    *,
    compiler: Compiler | None = None,
    pointers: Pointers | None = None,
) -> tuple[RetainedRetailHealthPresetArtifactSource, Compiler, Pointers]:
    resolved_compiler = compiler or Compiler()
    resolved_pointers = pointers or Pointers()
    source = RetainedRetailHealthPresetArtifactSource(
        coverage=Coverage(),
        coverage_requirements=(REQUIREMENT,),
        temporal_policy=RETAIL_POLICY,
        compiler=cast("RetailHealthCompiler", resolved_compiler),
        pointers=cast("PresetArtifactPointerStorePort", resolved_pointers),
        clock=lambda: NOW,
    )
    return source, resolved_compiler, resolved_pointers


@pytest.mark.covers("ADR-070")
def test_revision_derives_from_the_exact_publication_identities_used() -> None:
    source, _, _ = _source(compiler=Compiler(identities=("publication-b", "publication-a")))

    request = source.publication_for(VARIANT)

    assert isinstance(request, PresetArtifactPublicationRequest)
    pointer = request.pointer_advance.pointer
    assert pointer.revision == PresetArtifactRevision.from_publication_identities(
        ("publication-a", "publication-b"),
        render_format=RETAIL_HEALTH_PRESET_RENDER_FORMAT,
    )
    assert pointer.forward_token == 1
    assert pointer.supersedes is None
    # ADR-116 decision 4: the pointer names the window its body serves and its equal prior.
    body_payload = json.loads(request.body.payload_json)
    assert pointer.resolved_window is not None
    assert pointer.resolved_window.start.isoformat() == body_payload["period"]["start"]
    assert pointer.resolved_window.end.isoformat() == body_payload["period"]["end"]
    assert pointer.comparison_window is not None
    assert pointer.comparison_window.end < pointer.resolved_window.start
    assert pointer.custody_digest is None
    assert request.custody is None
    assert (request.body.page, request.body.variant) == (PAGE, VARIANT)
    decoded = retail_health_artifact_from_payload(json.loads(request.body.payload_json))
    assert decoded.outcome is RetailHealthOutcome.EMPTY


def test_an_unchanged_revision_is_not_republished() -> None:
    source, _, _ = _source()
    first = source.publication_for(VARIANT)
    assert isinstance(first, PresetArtifactPublicationRequest)

    source_again, compiler, _ = _source(pointers=Pointers(pointer=first.pointer_advance.pointer))

    # S6 §2.8 (D26): unchanged is typed, never None, so a bake can tell it from unavailable.
    assert source_again.publication_for(VARIANT) == SourceUnchanged(
        first.pointer_advance.pointer.revision
    )
    assert len(compiler.received) == 1


def test_a_new_revision_supersedes_the_current_pointer_with_a_history_expiry() -> None:
    source, _, _ = _source()
    first = source.publication_for(VARIANT)
    assert isinstance(first, PresetArtifactPublicationRequest)
    current = first.pointer_advance.pointer

    source_two, _, _ = _source(
        compiler=Compiler(identities=("publication-c",)),
        pointers=Pointers(pointer=current),
    )
    request = source_two.publication_for(VARIANT)

    assert isinstance(request, PresetArtifactPublicationRequest)
    pointer = request.pointer_advance.pointer
    assert pointer.supersedes == current.revision
    assert pointer.forward_token == current.forward_token + 1
    assert request.pointer_advance.superseded_history_expires_at is not None
    assert request.pointer_advance.superseded_history_expires_at > NOW


def test_an_undeclared_variant_and_an_unavailable_render_bake_nothing() -> None:
    source, compiler, _ = _source()
    assert source.publication_for(PresetArtifactVariant("undeclared")) is None
    assert compiler.received == []

    unavailable, _, _ = _source(compiler=Compiler(unavailable=True))
    assert unavailable.publication_for(VARIANT) is None


def test_retail_baker_publishes_only_a_retained_source_request() -> None:
    source, _, _ = _source()
    baker = RetailHealthPresetArtifactBaker(source)

    result = baker.bake(BakeIntent("retail-health", "7d"))

    assert isinstance(result, BakePublication)
    assert result.request.body.page == PAGE


class _RecordingQueries:
    """A required `queries` port that records every request; an unconfigured source sends none."""

    def __init__(self) -> None:
        self.seen: list[QueryRequest] = []

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        raise AssertionError("an unconfigured source must never query")


def test_retail_source_composition_fails_closed_without_retained_configuration() -> None:
    settings = PresetArtifactBakeSettings(artifact_bucket="bucket", pointer_table="table")
    queries = _RecordingQueries()

    source = retained_retail_health_preset_artifact_source(
        settings,
        cast("PresetArtifactPointerStorePort", Pointers()),
        cast("Any", queries),
        cast("Any", object()),
    )

    assert source is None
    assert queries.seen == []


def test_coverage_requirements_derive_from_the_declared_page_metrics() -> None:
    requirements = retail_health_coverage_requirements(V2_SCHEMAS, METRIC_CATALOG)

    # Each requirement names the one fact its metrics' definitions read: revenue, units and
    # returns sum `fct_retail_sales`, glance views sum `fct_product_page_views`. So the
    # `*_reported_total` writers of the same datasets never decide this page's coverage.
    assert requirements == (
        SourceCoverageRequirement(
            (("vendor_central", "GET_VENDOR_SALES_REPORT"),), facts=("fct_retail_sales",)
        ),
        SourceCoverageRequirement(
            (("vendor_central", "GET_VENDOR_TRAFFIC_REPORT"),), facts=("fct_product_page_views",)
        ),
    )


@dataclass
class FakeCurrent:
    publications: tuple[object, ...]

    def current_for_scope(self, scope: object, horizon: object = None) -> tuple[object, ...]:
        del scope, horizon
        return self.publications


@dataclass
class FakeCanonical:
    rows_by_publication: dict[str, tuple[tuple[tuple[str, object], ...], ...]]

    def rows(self, publication: object) -> tuple[tuple[tuple[str, object], ...], ...]:
        identity = cast("str", getattr(publication, "publication_id").value)  # noqa: B009
        return self.rows_by_publication.get(identity, ())


def _reported_row(day: date, revenue: Decimal, units: int) -> tuple[tuple[str, object], ...]:
    row: dict[str, object] = {
        "date_key": day,
        "marketplace_key": "amazon_us",
        "report_type": "GET_VENDOR_SALES_REPORT",
        "report_period": "DAY",
        "period_start": day,
        "period_end": day,
        "distributor_view": "MANUFACTURING",
        "selling_program": "RETAIL",
        "report_options_hash": "fictional-options-hash",
        "source_watermark": day,
        "retrieved_at": datetime(2026, 8, 20, tzinfo=UTC),
        "revenue_shipped": revenue,
        "currency_code": "USD",
        "cogs_shipped": Decimal("1.0"),
        "revenue_ordered": None,
        "units_shipped": units,
        "units_ordered": None,
        "customer_returns": None,
        "basis_key": "retail_actual",
        "source_timezone": "PST",
        "ecosystem_key": "amazon",
        "platform_key": "amazon_retail",
    }
    return tuple(sorted(row.items()))


def _reported_adapter(
    days: tuple[date, ...], *, duplicate: bool = False
) -> CanonicalRetailReportedTotals:
    publications = tuple(
        successful_publication(
            "fct_retail_sales_reported_total",
            day,
            PublicationSpec(f"reported-{day.isoformat()}", NOW),
        )
        for day in days
    )
    rows = {
        f"reported-{day.isoformat()}": (
            (_reported_row(day, Decimal("10.0"), 3),) * (2 if duplicate else 1)
        )
        for day in days
    }
    return CanonicalRetailReportedTotals(
        cast("CurrentPublicationPort", FakeCurrent(publications)),
        cast("CanonicalDatasetReadPort", FakeCanonical(rows)),
        SOURCE_PACKS,
        CONTRACTS,
        BRIDGE,
    )


def test_reported_totals_sum_exactly_over_a_fully_covered_window() -> None:
    window = DateRange(date(2026, 8, 13), date(2026, 8, 15))
    days = tuple(window.start + timedelta(days=offset) for offset in range(window.days))

    totals = _reported_adapter(days).reported_totals(window)

    assert totals is not None
    assert totals.revenue_shipped == Decimal("30.0")
    assert totals.units_shipped == Decimal(9)
    assert totals.publication_identities == tuple(
        sorted(f"reported-{day.isoformat()}" for day in days)
    )


def test_reported_totals_refuse_partial_window_coverage_and_duplicate_days() -> None:
    """A partial control sum would reconcile against the wrong window and must refuse."""
    window = DateRange(date(2026, 8, 13), date(2026, 8, 15))
    days = tuple(window.start + timedelta(days=offset) for offset in range(window.days))

    assert _reported_adapter(days[:-1]).reported_totals(window) is None
    assert _reported_adapter(days, duplicate=True).reported_totals(window) is None


def test_product_hierarchy_reads_effective_identities_with_parent_fallback() -> None:
    window = DateRange(date(2026, 8, 13), date(2026, 8, 19))
    publication = successful_publication(
        "dim_product", date(2026, 8, 19), PublicationSpec("dim-product-1", NOW)
    )
    valid_from = datetime(2026, 1, 1, tzinfo=UTC)
    valid_to = datetime(2099, 1, 1, tzinfo=UTC)
    rows: dict[str, tuple[tuple[tuple[str, object], ...], ...]] = {
        "dim-product-1": (
            tuple(
                sorted(
                    {
                        "amazon_asin": "amazon_asin:FICT00001",
                        "amazon_parent_asin": "amazon_asin:FICTPARENT",
                        "marketplace_key": "amazon_us",
                        "product_title": "Fictional Product One",
                        "brand": None,
                        "brand_code": None,
                        "valid_from": valid_from,
                        "valid_to": valid_to,
                        "is_current": True,
                    }.items()
                )
            ),
            tuple(
                sorted(
                    {
                        "amazon_asin": "amazon_asin:FICT00002",
                        "amazon_parent_asin": None,
                        "marketplace_key": "amazon_us",
                        "product_title": None,
                        "brand": None,
                        "brand_code": None,
                        "valid_from": valid_from,
                        "valid_to": valid_to,
                        "is_current": True,
                    }.items()
                )
            ),
        )
    }
    adapter = CanonicalRetailProductHierarchy(
        cast("CurrentPublicationPort", FakeCurrent((publication,))),
        cast("CanonicalDatasetReadPort", FakeCanonical(rows)),
        SOURCE_PACKS,
        CONTRACTS,
        BRIDGE,
    )

    result = adapter.hierarchy(window)

    assert result is not None
    assert result.publication_identities == ("dim-product-1",)
    assert result.by_asin["amazon_asin:FICT00001"] == RetailProductIdentity(
        "FICT00001", "FICTPARENT", "Fictional Product One"
    )
    # A standalone product is its own parent, and its ASIN stands in for a missing title.
    assert result.by_asin["amazon_asin:FICT00002"] == RetailProductIdentity(
        "FICT00002", "FICT00002", "FICT00002"
    )


@pytest.mark.covers("ADR-068")
def test_retail_insight_derivation_admits_prompted_slots_from_the_baked_artifact() -> None:
    """The serve overlay and the fill child share this derivation; its identity must be exact."""
    rendered = _rendered(2)
    artifact = rendered.artifact
    assert artifact is not None

    derivation = retail_insight_request_derivation(
        RetailInsightRequestDerivationInput(
            artifact=artifact,
            scope=AuthorizedAnalyticalScope(frozenset({BRIDGE.commerce_scope})),
            contracts=INSIGHT_CONTRACTS,
            catalog=METRIC_CATALOG,
        )
    )

    prompted = {
        InsightSlotId.DEMAND_SHAPE,
        InsightSlotId.RETURNS_PRESSURE,
        InsightSlotId.ORDERED_VS_SHIPPED,
    }
    assert derivation.context.subject == "retail-health"
    assert derivation.context.artifact_revision.startswith("sha256:")
    assert derivation.context.window == PERIOD
    assert set(derivation.findings_by_slot) == prompted
    # The fake serves every window total as 100 current versus 40 prior, so demand and
    # returns carry material changes and both ordered and shipped revenue are displayed.
    assert derivation.findings_by_slot[InsightSlotId.DEMAND_SHAPE]
    assert derivation.findings_by_slot[InsightSlotId.RETURNS_PRESSURE]
    assert derivation.findings_by_slot[InsightSlotId.ORDERED_VS_SHIPPED]
    assert derivation.requests, "material changes must admit at least one declared slot"
    for assembly in derivation.requests:
        assert assembly.key.slot_id in prompted
        assert assembly.key.subject == "retail-health"
        assert assembly.key.artifact_revision == derivation.context.artifact_revision
        assert assembly.key.window_start == PERIOD.start.isoformat()
        assert assembly.key.window_end == PERIOD.end.isoformat()
        # The model echoes what it is shown: no context entry may carry a wire enum
        # token. A narration that received the raw distributor view served it back to
        # the customer verbatim (found live, 2026-09-24).
        for entry in assembly.context:
            assert "MANUFACTURING" not in entry.value
            assert "SOURCING" not in entry.value
        assert any(entry.value == "Manufacturing view" for entry in assembly.context)


@pytest.mark.covers("ADR-068")
def test_distributor_view_customer_words_never_expose_the_wire_token() -> None:
    assert RetailDistributorView.MANUFACTURING.customer_words == "Manufacturing view"
    assert RetailDistributorView.SOURCING.customer_words == "Sourcing view"


@pytest.mark.covers("ADR-068")
def test_retail_insight_revision_ignores_insight_delivery_state() -> None:
    """One baked revision must map to one cache identity however its slots were delivered."""
    rendered = _rendered(2)
    artifact = rendered.artifact
    assert artifact is not None
    silent_first = dataclasses.replace(
        artifact,
        insights=(
            InsightSlot(
                id=artifact.insights[0].id,
                output_class=artifact.insights[0].output_class,
                state=InsightSlotState.SILENT,
                text=None,
                evidence_line=None,
                artifact_revision=artifact.insights[0].artifact_revision,
                prompt_version="deterministic-gate1",
            ),
            *artifact.insights[1:],
        ),
    )

    assert retail_payload_without_ephemera(artifact) == retail_payload_without_ephemera(
        silent_first
    )


@dataclass
class _FillPrompts:
    version: str

    def active(self, slot_id: InsightSlotId, tier: InsightPromptTier) -> InsightPromptActivation:
        del tier
        return InsightPromptActivation(
            slot_id, self.version, "A reviewed fictional prompt.", InsightModelRoute.QUICK
        )


@dataclass
class _FillContinuation:
    requests: list[InsightContinuationRequest] = field(
        default_factory=list[InsightContinuationRequest]
    )

    def enqueue(self, request: InsightContinuationRequest) -> str:
        self.requests.append(request)
        return f"message-{len(self.requests)}"


@pytest.mark.covers("ADR-074", "ADR-068")
def test_retail_fill_child_warms_exactly_the_cache_keys_the_serve_overlay_derives() -> None:
    """§5.3's claim for the retail page: warmed keys are byte-identical to serve-time first-fill.

    Runs the real child against a real baked retail body with the real shared derivation —
    nothing monkeypatched — and compares against a serve-style derivation of the same
    decoded artifact with the same active prompt replacement.
    """
    rendered = _rendered(2)
    artifact = rendered.artifact
    assert artifact is not None
    scope = AuthorizedAnalyticalScope(frozenset({BRIDGE.commerce_scope}))
    revision = PresetArtifactRevision.from_publication_identities(("publication-fictional",))
    body = PresetArtifactBody(
        PAGE,
        VARIANT,
        revision,
        canonical_preset_artifact_json(retail_health_artifact_payload(artifact)),
    )
    continuation = _FillContinuation()
    child = PresetInsightFillChild(
        prompts=cast("Any", _FillPrompts("v9")),
        continuation=cast("Any", continuation),
        scope=scope,
        contracts=INSIGHT_CONTRACTS,
        catalog=METRIC_CATALOG,
    )

    child.warm(
        PresetArtifactPublicationRequest(
            body,
            PresetArtifactPointerAdvanceRequest(
                PresetArtifactPointer(body.page, body.variant, revision, body.digest, 1)
            ),
        )
    )

    serve_derivation = retail_insight_request_derivation(
        RetailInsightRequestDerivationInput(
            artifact=retail_health_artifact_from_payload(json.loads(body.payload_json)),
            scope=scope,
            contracts=INSIGHT_CONTRACTS,
            catalog=METRIC_CATALOG,
        )
    )
    expected_keys = [
        dataclasses.replace(assembly.key, prompt_version="v9")
        for assembly in serve_derivation.requests
    ]
    warmed_keys = [item.generation.key for item in continuation.requests]
    assert warmed_keys, "the fictional retail artifact must admit at least one declared slot"
    assert warmed_keys == expected_keys
    assert all(key.subject == "retail-health" for key in warmed_keys)


@pytest.mark.covers("ADR-068")
def test_a_closed_retail_artifact_derives_no_requests_so_every_prompted_slot_is_silent() -> None:
    """Classify never-evaluated absence-state slots as ineligible, never evaluated-quiet."""
    closed = RetailHealthArtifact(
        RetailHealthOutcome.EMPTY,
        PERIOD,
        PRIOR,
        RetailDistributorView.MANUFACTURING,
        (),
        (),
        unavailable_insight_slots(RETAIL_HEALTH_INSIGHT_IDS),
        (),
        (),
        0,
        None,
        None,
        "Retail actuals are not campaign outcomes.",
    )

    derivation = retail_insight_request_derivation(
        RetailInsightRequestDerivationInput(
            artifact=closed,
            scope=AuthorizedAnalyticalScope(frozenset({BRIDGE.commerce_scope})),
            contracts=INSIGHT_CONTRACTS,
            catalog=METRIC_CATALOG,
        )
    )

    assert derivation.requests == ()
    assert all(not findings for findings in derivation.findings_by_slot.values())
    assert not derivation.silence_reasons
    assert derivation.missing_evidence_by_slot == {
        InsightSlotId.DEMAND_SHAPE: (InsightEvidenceInput.RETAIL_TOTALS,),
        InsightSlotId.RETURNS_PRESSURE: (InsightEvidenceInput.RETAIL_TOTALS,),
        InsightSlotId.ORDERED_VS_SHIPPED: (
            InsightEvidenceInput.RETAIL_TOTALS,
            InsightEvidenceInput.RETAIL_RECONCILIATION,
        ),
    }


def _complete_retail_payload() -> dict[str, object]:
    artifact = _rendered(2).artifact
    assert artifact is not None
    return retail_health_artifact_payload(artifact)


def _comparison_values(payload: dict[str, object]) -> list[dict[str, object]]:
    comparisons = list(cast("list[dict[str, object]]", payload["totals"]))
    for row in cast("list[dict[str, object]]", payload["rows"]):
        comparisons.extend(cast("list[dict[str, object]]", row["comparisons"]))
    return [
        cast("dict[str, object]", comparison[side])
        for comparison in comparisons
        for side in ("current", "prior")
    ]


def _point_values(payload: dict[str, object]) -> list[dict[str, object]]:
    points = [
        value
        for point in cast("list[dict[str, object]]", payload["trend"])
        for side in ("current", "prior")
        for value in cast("list[dict[str, object]]", point[side])
    ]
    spark = [
        value
        for row in cast("list[dict[str, object]]", payload["rows"])
        for value in cast("list[dict[str, object]]", row["shipped_revenue_spark"])
    ]
    return points + spark


@pytest.mark.covers("ADR-084")
def test_retail_comparisons_display_currency_at_two_places_and_points_stay_five_fields() -> None:
    """Shipped revenue is USD in dollars at four places; units are counts with null."""
    payload = _complete_retail_payload()

    for value in _comparison_values(payload):
        units = cast("dict[str, object]", value["units"])
        if units["kind"] == "currency":
            assert value["display_value"] == f"{Decimal(str(value['value'])):.2f}"
        else:
            assert value["display_value"] is None
    points = _point_values(payload)
    assert points
    assert all(set(value) == {"basis", "grain", "period", "units", "value"} for value in points)


@pytest.mark.covers("ADR-084")
def test_a_retained_pre_display_retail_body_decodes_and_keeps_five_field_points() -> None:
    payload = _complete_retail_payload()
    retained = cast("dict[str, object]", without_display_values(payload))
    assert all("display_value" not in value for value in _comparison_values(retained))

    decoded = retail_health_artifact_from_payload(retained)

    re_encoded = retail_health_artifact_payload(decoded)
    assert re_encoded == payload
    assert all(len(value) == 5 for value in _point_values(retained))
    assert all(len(value) == 5 for value in _point_values(re_encoded))


@pytest.mark.covers("ADR-084")
def test_the_retail_decoder_refuses_a_wrong_count_mixed_or_plotted_display_value() -> None:
    payload = _complete_retail_payload()
    wrong = json.loads(json.dumps(payload))
    currency = next(
        value
        for value in _comparison_values(wrong)
        if cast("dict[str, object]", value["units"])["kind"] == "currency"
    )
    currency["display_value"] = "0.01" if currency["display_value"] != "0.01" else "0.02"
    on_count = json.loads(json.dumps(payload))
    count = next(
        value
        for value in _comparison_values(on_count)
        if cast("dict[str, object]", value["units"])["kind"] == "count"
    )
    count["display_value"] = "1.00"
    mixed = json.loads(json.dumps(payload))
    del _comparison_values(mixed)[0]["display_value"]
    plotted = json.loads(json.dumps(payload))
    _point_values(plotted)[0]["display_value"] = None

    for body in (wrong, on_count):
        with pytest.raises(ValueError, match="display value is not canonical"):
            retail_health_artifact_from_payload(body)
    with pytest.raises(ValueError, match="mixes display values"):
        retail_health_artifact_from_payload(mixed)
    with pytest.raises(ValueError, match="fields are not exact"):
        retail_health_artifact_from_payload(plotted)


@pytest.mark.covers("ADR-084", "ADR-070")
def test_retail_movers_are_baked_at_two_places_in_the_shipped_revenue_currency() -> None:
    """Format 4 added display values and two-place movers; formats 5 to 8 keep them."""
    artifact = _rendered(2).artifact
    assert artifact is not None
    movers = artifact.insights[3]

    assert RETAIL_HEALTH_PRESET_RENDER_FORMAT == 9
    assert movers.text is not None
    assert re.search(r"changed by -?[\d,]+\.\d{2} USD", movers.text)


@dataclass
class RenderedCompiler:
    """Return one real renderer result, so the source publishes exactly what a bake reduced."""

    result: RetailHealthRenderResult

    def render_retail_health(self, request: RetailHealthRenderInput) -> RetailHealthRenderResult:
        del request
        return self.result


@pytest.mark.covers("ADR-125")
def test_a_bake_reduces_one_product_detail_child_per_displayed_row() -> None:
    """Each child carries every tile at product grain; its additive figures are its row's."""
    rendered = _rendered(2)
    artifact = rendered.artifact
    assert artifact is not None
    assert len(rendered.children) == len(artifact.rows) == 2
    for row, child in zip(artifact.rows, rendered.children, strict=True):
        detail = child.artifact
        assert detail.product.child_asin == row.child_asin
        assert (detail.period, detail.prior_period) == (artifact.period, artifact.prior_period)
        assert tuple(item.metric for item in detail.totals) == RETAIL_HEALTH_TOTAL_METRICS
        by_metric = {item.metric: item for item in detail.totals}
        for comparison in row.comparisons:
            assert by_metric[comparison.metric].current.value == comparison.current.value
            assert by_metric[comparison.metric].prior.value == comparison.prior.value
        assert [point.current.value for point in detail.sales_daily] == [
            value.value for value in row.shipped_revenue_spark
        ]
        # The fake answers both declared channels, so each product has two rows, never totalled.
        ads = detail.ad_attributed
        assert ads is not None
        assert [item.label for item in ads.rows] == list(CHANNEL_LABELS.values())
        assert child.product.value == _canonical_asin(int(row.child_asin.removeprefix("FICT")))


@pytest.mark.covers("ADR-125")
def test_the_publication_binds_a_closed_child_set_in_its_custody_before_the_pointer() -> None:
    rendered = _rendered(2)
    source, _, _ = _source(compiler=cast("Compiler", RenderedCompiler(rendered)))

    request = source.publication_for(VARIANT)

    assert isinstance(request, PresetArtifactPublicationRequest)
    assert request.custody is not None
    pointer = request.pointer_advance.pointer
    assert pointer.custody_digest == request.custody.digest
    manifest = retail_health_custody_manifest_from_payload(json.loads(request.custody.payload_json))
    assert manifest.body_digest == request.body.digest
    children = dict(manifest.product_children)
    assert [body.identity for body in request.sibling_bodies] == [
        PresetArtifactProductChildIdentity(child.product_ref) for child in rendered.children
    ]
    for public, private in zip(request.sibling_bodies, request.sibling_custody, strict=True):
        ref = cast("PresetArtifactProductChildIdentity", public.identity).product_ref
        assert public.key.endswith(f"/{pointer.revision.value}/products/{ref}.json")
        assert children[ref] == RetailProductChildDigests(public.digest, private.digest)
        # The public child names the product by display identity only.
        assert "amazon_asin:" not in public.payload_json
        assert ref not in public.payload_json


@dataclass
class _RecordingPersistence:
    """Counts every persistence call; D18 requires zero for an over-cap body."""

    calls: list[PresetArtifactPublicationRequest] = field(
        default_factory=list[PresetArtifactPublicationRequest]
    )

    def publish(self, request: PresetArtifactPublicationRequest) -> None:
        self.calls.append(request)


def _bake_and_persist(products: int) -> tuple[_RecordingPersistence, int]:
    """Render `products` real rows, bake, and persist only what the bake returned."""
    source, _, _ = _source(compiler=cast("Compiler", RenderedCompiler(_rendered(products))))
    persistence = _RecordingPersistence()
    result = RetailHealthPresetArtifactBaker(source).bake(BakeIntent("retail-health", "7d"))
    assert isinstance(result, BakePublication)
    persistence.publish(result.request)
    return persistence, len(result.request.body.payload_json.encode())


@pytest.mark.covers("ADR-125")
def test_d18_an_over_cap_retail_parent_fails_closed_before_any_persistence_call() -> None:
    """Paired: 45 products render under the cap and persist; 50 raise the typed D18 error.

    The parent body is constructed while the source renders, before the baker returns, so an
    over-cap body never reaches persistence and no content is dropped.
    """
    written, observed = _bake_and_persist(45)
    assert len(written.calls) == 1
    assert observed == 254_046 <= MAX_PRESET_ARTIFACT_BYTES  # the audit's 45-product census

    with pytest.raises(PresetArtifactBodyCeilingError) as refused:
        _bake_and_persist(RETAIL_HEALTH_MAX_ROWS)
    assert refused.value.reason == RETAIL_BODY_CEILING
    assert refused.value.observed_bytes == 279_577 > MAX_PRESET_ARTIFACT_BYTES  # 50 products


@pytest.mark.covers("ADR-125")
def test_plan_4_3_every_current_read_precedes_the_first_query() -> None:
    """Make every CURRENT read precede the first query, with an unchanged artifact.

    The bind pass needs the whole read set before any query: both inspections, the hierarchy
    and the reported totals happen first, and the render still yields its two rows.
    """
    events: list[str] = []
    products = _products(2)
    queries = RetailQueries(products, PERIOD)
    hierarchy = FakeHierarchy(_hierarchy_result(products))
    reported = FakeReported(
        RetailReportedTotals(
            _expected_product_revenue(2), _expected_product_units(2), ("reported-1",)
        )
    )
    renderer = _renderer(queries, hierarchy, reported)

    def logged(name: str, call: Any) -> Any:
        def run(*args: Any, **kwargs: Any) -> Any:
            events.append(name)
            return call(*args, **kwargs)

        return run

    object.__setattr__(
        renderer, "evidence", SimpleNamespace(inspect=logged("read", renderer.evidence.inspect))
    )
    object.__setattr__(
        renderer, "hierarchy", SimpleNamespace(hierarchy=logged("read", hierarchy.hierarchy))
    )
    object.__setattr__(
        renderer,
        "reported",
        SimpleNamespace(reported_totals=logged("read", reported.reported_totals)),
    )
    object.__setattr__(
        renderer,
        "tooling",
        replace(renderer.tooling, queries=SimpleNamespace(run=logged("query", queries.run))),
    )

    rendered = renderer.render_retail_health(RetailHealthRenderInput(PERIOD, PRIOR))

    assert rendered.artifact is not None and len(rendered.artifact.rows) == 2
    first_query = events.index("query")
    assert events[:first_query] == ["read"] * 4
    assert "read" not in events[first_query:]
