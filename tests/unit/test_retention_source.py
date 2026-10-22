# SPDX-License-Identifier: MIT-0
"""The Retention materializer: planned per-cell answers, fail-closed registration (ADR-075).

Every figure is fictional and synthetic. The renderer runs the real measurement batch, planner and
shaping against an in-memory query port, as the Reach tests do, so the bake is exactly what the
planned answers say. Pack 15's own cohort count is read from its bound publications' load
evidence, which the stub supplies.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest
from publication_support import StubPublications

from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    handle,
    handle_batch,
    require_answered,
)
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher.bake_results import (
    BakePublication,
    BakeUnavailable,
)
from adapters.jobs.preset_artifact_publisher.baker import (
    CampaignPresetArtifactBaker,
    RegisteredPresetArtifactBaker,
    RetentionPresetArtifactBaker,
    UnavailableCampaignPresetArtifactSource,
    UnavailablePresetArtifactBaker,
    UnavailableRetentionPresetArtifactSource,
)
from adapters.jobs.preset_artifact_publisher.retention_source import (
    LATEST_VINTAGE_VARIANT,
    RETENTION_MARKET_MAX_ENCODED_BYTES,
    RETENTION_RENDER_FORMAT,
    RetainedRetentionPresetArtifactSource,
    RetentionArtifactRenderer,
    RetentionRenderInput,
    RetentionRenderResult,
    retention_policy,
)
from adapters.jobs.preset_artifact_publisher.transport import (
    BakeIntent,
    load_declared_bake_intents,
)
from adapters.outbound.metric_dictionary import load_catalog
from domain.data_contracts.contract import CohortGrain, CohortTriangle
from domain.measurement.planning import measure_alias
from domain.measurement.publications import BoundQueryPlan
from domain.narrative.shaped_response import ShapedResponse
from domain.presentation.insights import InsightSlotId
from domain.presentation.page_schemas import PageSchemaError, PresentationVintagePolicy
from domain.presentation.preset_artifacts import (
    MAX_PRESET_ARTIFACT_BYTES,
    PresetArtifactPage,
    PresetArtifactRevision,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
)
from domain.presentation.retention import RETENTION_PAGE_ID, retention_artifact_payload
from domain.semantics.calendar_periods import CalendarPeriod, vintage_ending
from domain.semantics.time import TimeGrain
from domain.semantics.validation import (
    PrepublicationRuleFamily,
    PrepublicationRuleResult,
    PublicationEvidence,
    RuleOutcome,
)
from domain.shared_kernel.errors import DomainError, ValidationError
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from domain.shopper_aggregates.cohorts import TriangleCohort, triangle_cohorts
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionRequest,
    PresentationEvidenceInspectionResult,
    PublicationSelection,
    PublicationSelectionRequest,
)
from ports.outbound.query import QueryRequest, QueryResult, QueryRow
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075", "ADR-100", "ADR-104")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT)
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
INSIGHTS = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)
VINTAGE = vintage_ending(date(2026, 9, 1))
STANDARD = "amc-acquisition-cohorts"
ARP = "amc-acquisition-cohorts-arp"
US = "amazon_us"
UK = "amazon_uk"
GROUPS = ("dsp_only", "sponsored_only", "dsp_and_sponsored")
MONTHLY = CohortTriangle("cohort_month", "months_since_acquisition", CohortGrain.MONTH, 12)


def _cohorts() -> tuple[TriangleCohort, ...]:
    return triangle_cohorts(MONTHLY, (VINTAGE.start, VINTAGE.end))


_VALUES: dict[str | tuple[str, str], Callable[[int], int]] = {
    "cohort_buyers_new_to_brand_12mo": lambda _k: 400,
    "cohort_buyers_account_ids": lambda _k: 300,
    ("standard", "repeat_buyers"): lambda _k: 40,
    "cumulative_repeat_buyers_account_ids": lambda k: 20 + k,
    ("arp", "repeat_buyers"): lambda _k: 60,
    "cumulative_repeat_buyers": lambda k: 60 + k,
}


@dataclass
class CohortQueries:
    """Grouped rows per governed plan: one per (marketplace, cohort, k, acquisition group)."""

    not_returned: tuple[tuple[str, date, int, str], ...] = ()
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        plan = request.plan.logical
        (measure,) = plan.measures
        path = plan.filters["cohort_path"]
        value = _VALUES.get(measure) or _VALUES[(path, measure)]
        market = plan.filters["marketplace_key"]
        rows: list[QueryRow] = []
        for cohort in _cohorts():
            for k in cohort.offsets:
                for group in GROUPS:
                    row: dict[str, str | int | Decimal | None] = {
                        "marketplace_key": market,
                        "cohort_month": cohort.start.isoformat(),
                        "months_since_acquisition": str(k),
                        "acquisition_channel_group": group,
                    }
                    returned = (path, cohort.start, k, group) not in self.not_returned
                    row[measure_alias(0)] = Decimal(value(k)) if returned else None
                    rows.append(row)
        return QueryResult(
            rows=tuple(rows), bytes_scanned=1_024, truncated=False, suppression=Suppression(False)
        )


@dataclass
class _Served:
    publications: frozenset[tuple[str, CalendarPeriod]] = frozenset(
        {(STANDARD, VINTAGE), (ARP, VINTAGE)}
    )
    series: dict[tuple[str, CalendarPeriod], tuple[str | None, ...]] = field(
        default_factory=dict[tuple[str, CalendarPeriod], tuple[str | None, ...]]
    )

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        ids = {contract.contract_id for contract in request.contracts}
        return ServedPeriods(
            frozenset(item for item in self.publications if item[0] in ids),
            {STANDARD: 2, ARP: 2},
            {key: held for key, held in self.series.items() if key[0] in ids},
        )


def _record(check: int, gap: tuple[str, str] | None = None) -> str:
    """Pack 15's own count of every cohort and group; `gap` is one counted a shopper short."""
    totals = {
        f"{cohort.start.isoformat()}|{group}": str(
            check - 1 if (cohort.start.isoformat(), group) == gap else check
        )
        for cohort in _cohorts()
        for group in GROUPS
    }
    return json.dumps(
        {"grade": "not_degraded", "repeated_totals": {"cohort_buyers_account_ids_check": totals}},
        sort_keys=True,
    )


@dataclass
class _Evidence(StubPublications):
    """The stub selector and inspector, binding each paid plan to pack 15's record."""

    check: int = 300
    gap: tuple[str, str] | None = None

    def _with_record(self, bound: BoundQueryPlan) -> BoundQueryPlan:
        if bound.logical.filters.get("cohort_path") != "arp":
            return bound
        first = bound.primary[0]
        results = (
            *(
                PrepublicationRuleResult(family, f"unit:{family.value}", RuleOutcome.PASSED, "ok")
                for family in PrepublicationRuleFamily
            ),
            PrepublicationRuleResult(
                PrepublicationRuleFamily.SUPPRESSION_COVERAGE,
                f"suppression:{ARP}.cell-loss.v1",
                RuleOutcome.PASSED,
                _record(self.check, self.gap),
            ),
        )
        evidence = PublicationEvidence("unit", first.evidence_subject, results)
        return replace(bound, primary=(replace(first, evidence=evidence), *bound.primary[1:]))

    def select(self, request: PublicationSelectionRequest) -> PublicationSelection:
        selected = super().select(request)
        return replace(selected, plans=tuple(self._with_record(item) for item in selected.plans))

    def inspect(
        self, request: PresentationEvidenceInspectionRequest
    ) -> PresentationEvidenceInspectionResult:
        inspected = super().inspect(request)
        return PresentationEvidenceInspectionResult(
            tuple(
                slot if slot.bound is None else replace(slot, bound=self._with_record(slot.bound))
                for slot in inspected.slots
            )
        )


def _renderer(
    queries: CohortQueries, served: _Served | None = None, check: int = 300
) -> RetentionArtifactRenderer:
    stub = _Evidence(check=check)
    periods = served or _Served()
    return RetentionArtifactRenderer(
        catalog=CATALOG,
        contracts=CONTRACTS,
        evidence=stub,
        tooling=MeasurementTooling(queries=queries, publications=stub, periods=periods),
        marketplaces=(US,),
        insight_contract=INSIGHTS[InsightSlotId.RETENTION_SHIFT],
        clock=lambda: NOW,
        first_quarterly=lambda _market: _first_quarterly(periods),
    )


def _first_quarterly(served: _Served) -> date | None:
    """Return the fake's first standard vintage a quarterly series holds (its earliest CURRENT)."""
    return min(
        (
            period.as_of_month
            for contract_id, period in served.publications
            if contract_id == STANDARD
            and period.grain is TimeGrain.CUSTOM
            and "quarterly" in served.series.get((contract_id, period), ())
        ),
        default=None,
    )


def _baked(
    queries: CohortQueries | None = None, served: _Served | None = None, check: int = 300
) -> dict[str, object]:
    result = _renderer(queries or CohortQueries(), served, check).render_retention(
        RetentionRenderInput(VINTAGE)
    )
    assert result.artifact is not None
    assert result.publication_identities
    return retention_artifact_payload(result.artifact)


def _cell(  # noqa: PLR0913, PLR0917 -- one baked cell: its path, group, cohort and offset
    body: dict[str, object], path: str, group: str, cohort: str, k: int
) -> dict[str, object]:
    (market,) = cast("list[dict[str, object]]", body["markets"])
    triangle = cast("dict[str, list[dict[str, object]]]", market["triangle"])
    grid = next(item for item in triangle[path] if item["group"] == group)
    row = next(
        item for item in cast("list[dict[str, object]]", grid["rows"]) if item["cohort"] == cohort
    )
    return next(
        item for item in cast("list[dict[str, object]]", row["cells"]) if item["offset"] == k
    )


def test_the_bake_equals_the_planned_per_cell_answers() -> None:
    queries = CohortQueries()
    body = _baked(queries)
    # 40 repeat buyers of a 400-shopper cohort on the standard path; 60 of pack 14's 300 account
    # IDs on the paid path.
    standard = _cell(body, "standard", "sponsored_only", "2025-09-01", 1)
    assert standard["rate"] == {"value": "0.1000", "text": "10.0%"}
    assert standard["cohort_size"] == {"value": "400", "text": "400 new customers"}
    paid = _cell(body, "paid", "sponsored_only", "2025-09-01", 1)
    assert paid["rate"] == {"value": "0.2000", "text": "20.0%"}
    (market,) = cast("list[dict[str, object]]", body["markets"])
    curve = cast("dict[str, object]", market["curve"])
    assert curve["metric"] == "cumulative_repeat_rate_account_ids"
    first = cast("list[dict[str, object]]", curve["lines"])[0]["points"][0]  # type: ignore[index]
    assert first["rate"] == {"value": "0.0700", "text": "7.0%"}  # 21 / 300 per cohort
    # Every plan reads one metric per cell, grouped by cohort, k and group, on one path and the
    # vintage, with the series' cohort grain pinned.
    metrics = sorted(
        {(r.plan.logical.filters["cohort_path"], r.plan.logical.metric.name) for r in queries.seen}
    )
    assert metrics == [
        ("arp", "cohort_repeat_buyers"),
        ("arp", "cohort_repeat_buyers_cumulative"),
        ("standard", "cohort_buyers_account_ids"),
        ("standard", "cohort_buyers_new_to_brand_12mo"),
        ("standard", "cohort_repeat_buyers"),
        ("standard", "cohort_repeat_buyers_cumulative_account_ids"),
    ]
    for request in queries.seen:
        plan = request.plan.logical
        assert plan.group_by == (
            "marketplace_key",
            "cohort_month",
            "months_since_acquisition",
            "acquisition_channel_group",
        )
        assert (plan.filters["time_grain"], plan.filters["as_of_month"]) == ("custom", "2026-09-01")
        assert plan.filters["cohort_grain"] == "month"
        assert plan.period == VINTAGE


def test_a_not_returned_cell_bakes_not_available_and_its_curve_point_too() -> None:
    gap = ("standard", date(2025, 12, 1), 3, "dsp_only")
    body = _baked(CohortQueries(not_returned=(gap,)))
    cell = _cell(body, "standard", "dsp_only", "2025-12-01", 3)
    assert (cell["state"], cell["text"], cell["rate"]) == ("not_returned", "Not available", None)
    # Control: the next month of the same cohort came back.
    assert _cell(body, "standard", "dsp_only", "2025-12-01", 4)["state"] == "value"


def test_the_arp_rate_is_withheld_when_pack_15s_own_count_differs() -> None:
    body = _baked(check=299)
    cell = _cell(body, "paid", "dsp_only", "2025-09-01", 1)
    assert (cell["state"], cell["text"]) == ("withheld", "cohort sizes changed between runs")
    # Control: the standard path still renders.
    assert _cell(body, "standard", "dsp_only", "2025-09-01", 1)["state"] == "value"


def test_a_paid_path_with_no_delivery_reads_the_callout_and_plans_nothing_paid() -> None:
    queries = CohortQueries()
    body = _baked(queries, _Served(frozenset({(STANDARD, VINTAGE)})))
    (market,) = cast("list[dict[str, object]]", body["markets"])
    path = cast("dict[str, dict[str, object]]", market["path"])
    assert path["paid"]["state"] == "not_observed"
    assert cast("dict[str, object]", market["triangle"])["paid"] == []
    assert {r.plan.logical.filters["cohort_path"] for r in queries.seen} == {"standard"}


def test_a_vintage_two_series_hold_bakes_nothing() -> None:
    served = _Served(series={(STANDARD, VINTAGE): (None, "quarterly")})
    result = _renderer(CohortQueries(), served).render_retention(RetentionRenderInput(VINTAGE))
    assert result == RetentionRenderResult(None, ())


def test_a_vintage_with_no_standard_publication_bakes_nothing() -> None:
    result = _renderer(CohortQueries(), _Served(frozenset())).render_retention(
        RetentionRenderInput(VINTAGE)
    )
    assert result == RetentionRenderResult(None, ())


@dataclass
class _WideQueries(CohortQueries):
    """Every figure scaled to eight digits, so each cell's text is as wide as a real one."""

    def run(self, request: QueryRequest) -> QueryResult:
        result = super().run(request)
        alias = measure_alias(0)
        return replace(
            result,
            rows=tuple(
                row if row[alias] is None else {**row, alias: cast("Decimal", row[alias]) * 10_000}
                for row in result.rows
            ),
        )


def test_each_marketplace_s_widest_body_fits_the_registered_per_market_ceiling() -> None:
    def encoded(markets: tuple[str, ...]) -> int:
        renderer = replace(_renderer(_WideQueries(), check=3_000_000), marketplaces=markets)
        result = renderer.render_retention(RetentionRenderInput(VINTAGE))
        assert result.artifact is not None
        payload = canonical_preset_artifact_json(retention_artifact_payload(result.artifact))
        return len(payload.encode())

    one, two = encoded((US,)), encoded((US, UK))
    # One marketplace with the envelope, and each one added, stay within the ceiling, so
    # `retention_bake_body_bytes` bounds every governed roster.
    assert one <= RETENTION_MARKET_MAX_ENCODED_BYTES
    assert two - one <= RETENTION_MARKET_MAX_ENCODED_BYTES
    # Control: two marketplaces already exceed the artifact cap, which the gate now reports.
    assert two > MAX_PRESET_ARTIFACT_BYTES


def _measured_only(identities: tuple[str, ...]) -> set[str]:
    return {item for item in identities if not item.startswith("retention-render-inputs.v1:")}


def test_a_label_or_grain_history_only_change_advances_the_retention_revision() -> None:
    def render(renderer: RetentionArtifactRenderer) -> RetentionRenderResult:
        result = renderer.render_retention(RetentionRenderInput(VINTAGE))
        assert result.artifact is not None
        return result

    def unrevised(result: RetentionRenderResult) -> dict[str, object]:
        """Return the body without its revision, which names the identities under test."""
        assert result.artifact is not None
        return json.loads(
            json.dumps(retention_artifact_payload(result.artifact)).replace(
                PresetArtifactRevision.from_publication_identities(
                    result.publication_identities, render_format=RETENTION_RENDER_FORMAT
                ).value,
                "",
            )
        )

    two = replace(_renderer(CohortQueries()), marketplaces=(US, UK))
    # The paid series switched to quarterly cohorts: its cells name the first quarterly vintage.
    paid_quarterly: dict[tuple[str, CalendarPeriod], tuple[str | None, ...]] = {
        (ARP, VINTAGE): ("quarterly",)
    }
    earlier = vintage_ending(date(2026, 6, 1))
    pairs = {
        "marketplace label": (two, replace(two, marketplace_labels={US: "United States"})),
        "first quarterly vintage": (
            _renderer(CohortQueries(), _Served(series=paid_quarterly)),
            _renderer(
                CohortQueries(),
                _Served(
                    frozenset({(STANDARD, VINTAGE), (ARP, VINTAGE), (STANDARD, earlier)}),
                    {**paid_quarterly, (STANDARD, earlier): ("quarterly",)},
                ),
            ),
        ),
    }
    for before_renderer, after_renderer in pairs.values():
        before, after = render(before_renderer), render(after_renderer)
        # Control: the same inputs derive the same identities.
        assert render(before_renderer).publication_identities == before.publication_identities
        assert unrevised(before) != unrevised(after)
        # The measured publications are the same, so only the render-inputs identity differs.
        assert _measured_only(before.publication_identities) == _measured_only(
            after.publication_identities
        )
        assert before.publication_identities != after.publication_identities


# --- Chat answers the ARP rates the bake computes (owner decision 2026-10-12) ---------------------

_CHAT_GRAINS = ("cohort", "months_since_acquisition", "acquisition_channel_group")


@dataclass(frozen=True)
class _Ask:
    filters: dict[str, str] = field(default_factory=dict[str, str])
    served: _Served = field(default_factory=_Served)
    check: int = 300
    gap: tuple[str, str] | None = None
    window: DateRange = VINTAGE.window


def _ask(metric: str, ask: _Ask) -> MeasurementToolRequest:
    return MeasurementToolRequest(
        tool=MeasurementTool.METRIC_BY_GRAIN,
        catalog=CATALOG,
        contracts=CONTRACTS,
        metric_name=metric,
        grains=_CHAT_GRAINS,
        window=ask.window,
        computed_at=NOW,
        correlation_id="corr-arp",
        filters={"marketplace_key": US, **ask.filters},
    )


def _tooling(ask: _Ask, queries: CohortQueries | None = None) -> MeasurementTooling:
    stub = _Evidence(check=ask.check, gap=ask.gap)
    return MeasurementTooling(
        queries=queries or CohortQueries(), publications=stub, periods=ask.served
    )


def _chat(metric: str, ask: _Ask, queries: CohortQueries | None = None) -> ShapedResponse:
    return handle(_ask(metric, ask), _tooling(ask, queries))


def _row(answer: ShapedResponse, cohort: str, cell: tuple[int, str]) -> dict[str, object]:
    k, group = cell
    return next(
        dict(row)
        for row in answer.data.values
        if (row["cohort_month"], row["months_since_acquisition"], row["acquisition_channel_group"])
        == (cohort, str(k), group)
    )


def test_chat_answers_the_arp_rate_with_the_bakes_rate_and_basis() -> None:
    queries = CohortQueries()
    answer = _chat("repeat_buyer_rate_arp", _Ask(), queries)
    # The same inputs the bake reads: 60 paid-path repeat buyers over pack 14's 300 account IDs.
    chat = _row(answer, "2025-09-01", (1, "sponsored_only"))
    paid = _cell(_baked(), "paid", "sponsored_only", "2025-09-01", 1)
    assert (chat["value"], chat["withheld_reason"]) == (Decimal("0.2000"), None)
    assert paid["rate"] == {"value": str(chat["value"]), "text": "20.0%"}
    metric = CATALOG.metric("repeat_buyer_rate_arp")
    assert metric is not None
    assert answer.metadata.measurement_basis == metric.measurement_basis
    assert {basis.value for basis in metric.measurement_basis} == {"ad_attributed", "retail_actual"}
    assert answer.metadata.contract_ids == (STANDARD, ARP)
    # Each operand on its own path, for one vintage and its cohort grain.
    assert sorted(
        (r.plan.logical.filters["cohort_path"], r.plan.logical.metric.name) for r in queries.seen
    ) == [("arp", "cohort_repeat_buyers"), ("standard", "cohort_buyers_account_ids")]
    for request in queries.seen:
        assert request.plan.logical.filters["as_of_month"] == "2026-09-01"
        assert request.plan.logical.filters["cohort_grain"] == "month"
    # Every served cell answers: 78 cohort cells per group in a 13-month vintage.
    assert len(answer.data.values) == sum(len(c.offsets) for c in _cohorts()) * len(GROUPS)
    # The cumulative ARP rate reads pack 15's cumulative repeat buyers: (60 + 1) / 300.
    cumulative = _chat("cumulative_repeat_rate_arp", _Ask())
    assert _row(cumulative, "2025-09-01", (1, "dsp_only"))["value"] == Decimal("0.2033")


def test_without_a_paid_delivery_only_the_arp_rate_is_refused() -> None:
    standard_only = _Served(frozenset({(STANDARD, VINTAGE)}))
    with pytest.raises(DomainError) as refused:
        _chat("repeat_buyer_rate_arp", _Ask(served=standard_only))
    assert refused.value.context["reason"] == "not_observed_in_deployment"
    assert "no delivery of any cohort vintage" in refused.value.description
    # Paired control: the standard path's rate answers on the same deployment, binding pack 14
    # alone.
    queries = CohortQueries()
    standard = _chat(
        "cohort_repeat_buyers",
        _Ask(filters={"cohort_path": "standard"}, served=standard_only),
        queries,
    )
    assert _row(standard, "2025-09-01", (1, "sponsored_only"))["value"] == 40
    assert {r.plan.logical.filters["cohort_path"] for r in queries.seen} == {"standard"}
    # A named vintage the paid path hasn't delivered names the one it has.
    earlier = vintage_ending(date(2026, 8, 1))
    behind = _Served(frozenset({(STANDARD, VINTAGE), (STANDARD, earlier), (ARP, earlier)}))
    with pytest.raises(DomainError) as named:
        _chat("repeat_buyer_rate_arp", _Ask(filters={"as_of_month": "2026-09"}, served=behind))
    assert named.value.context["reason"] == "not_observed_in_deployment"
    assert (
        "The latest vintage it has delivered ends August 2026; name it as as_of_month."
        in named.value.description
    )
    # Asked without a vintage, the latest vintage both paths hold answers.
    month = DateRange(date(2026, 9, 1), date(2026, 9, 30))
    latest = _chat(
        "repeat_buyer_rate_arp", _Ask(served=behind, window=month), queries := CohortQueries()
    )
    assert latest.data.values
    assert {r.plan.logical.filters["as_of_month"] for r in queries.seen} == {"2026-08-01"}


def test_in_one_batch_a_missing_paid_delivery_refuses_only_the_arp_slot() -> None:
    standard_only = _Ask(served=_Served(frozenset({(STANDARD, VINTAGE)})))
    standard_ask = _Ask(filters={"cohort_path": "standard"}, served=standard_only.served)
    batch = (
        _ask("cohort_repeat_buyers", standard_ask),
        _ask("repeat_buyer_rate_arp", standard_only),
        _ask("cohort_buyers_account_ids", standard_ask),
    )
    queries = CohortQueries()
    first, refused, last = handle_batch(batch, _tooling(standard_only, queries))
    # The standard answers come back beside the refusal, read from pack 14 alone.
    assert isinstance(first, ShapedResponse)
    assert isinstance(last, ShapedResponse)
    assert _row(first, "2025-09-01", (1, "sponsored_only"))["value"] == 40
    assert _row(last, "2025-09-01", (1, "sponsored_only"))["value"] == 300
    assert {r.plan.logical.filters["cohort_path"] for r in queries.seen} == {"standard"}
    # Only the ARP slot carries the typed refusal.
    assert isinstance(refused, DomainError)
    assert refused.context["reason"] == "not_observed_in_deployment"
    assert "no delivery of any cohort vintage" in refused.description
    # A caller served whole (one tool call, a page) raises that same refusal.
    with pytest.raises(DomainError) as whole:
        require_answered((first, refused, last))
    assert whole.value is refused
    # Paired control: with pack 15 delivered every slot of the same batch answers.
    answered = require_answered(handle_batch(batch, _tooling(_Ask())))
    assert _row(answered[1], "2025-09-01", (1, "sponsored_only"))["value"] == Decimal("0.2000")
    assert _row(answered[0], "2025-09-01", (1, "sponsored_only"))["value"] == 40


def test_a_failed_cohort_size_check_withholds_only_its_cells() -> None:
    answer = _chat("repeat_buyer_rate_arp", _Ask(gap=("2025-10-01", "dsp_only")))
    withheld = _row(answer, "2025-10-01", (1, "dsp_only"))
    assert (withheld["value"], withheld["withheld_reason"]) == (
        None,
        "cohort sizes changed between runs",
    )
    cells = sum(1 for c in _cohorts() if c.start == date(2025, 10, 1) for _ in c.offsets)
    assert sum(1 for row in answer.data.values if row["withheld_reason"]) == cells
    assert answer.narrative[1] == (
        f"{CATALOG.metric('repeat_buyer_rate_arp').label} is withheld for {cells} cell(s): "  # type: ignore[union-attr]
        "cohort sizes changed between runs."
    )
    # Paired control: the same cohort's other group, and the next cohort, still answer.
    assert _row(answer, "2025-10-01", (1, "sponsored_only"))["value"] == Decimal("0.2000")
    assert _row(answer, "2025-11-01", (1, "dsp_only"))["value"] == Decimal("0.2000")
    # The bake withholds the same cell with the same reason.
    baked = _cell(_baked(check=299), "paid", "dsp_only", "2025-10-01", 1)
    assert (baked["state"], baked["text"]) == ("withheld", withheld["withheld_reason"])


def test_operands_on_two_cohort_grains_withhold_every_arp_cell_with_the_grain_reason() -> None:
    served = _Served(series={(ARP, VINTAGE): ("quarterly",)})
    answer = _chat("repeat_buyer_rate_arp", _Ask(served=served))
    reasons = {row["withheld_reason"] for row in answer.data.values}
    assert reasons == {"Cohort grain changed in September 2026."}
    # Paired control: on one grain the same cells answer.
    assert all(
        row["value"] is not None for row in _chat("repeat_buyer_rate_arp", _Ask()).data.values
    )


def test_chat_refuses_a_cohort_path_or_a_missing_marketplace_on_an_arp_rate() -> None:
    with pytest.raises(ValidationError, match="takes no cohort_path"):
        _chat("repeat_buyer_rate_arp", _Ask(filters={"cohort_path": "arp"}))
    stub = _Evidence()
    with pytest.raises(ValidationError, match="name its marketplace"):
        handle(
            MeasurementToolRequest(
                tool=MeasurementTool.METRIC_BY_GRAIN,
                catalog=CATALOG,
                contracts=CONTRACTS,
                metric_name="repeat_buyer_rate_arp",
                grains=_CHAT_GRAINS,
                window=VINTAGE.window,
                computed_at=NOW,
                correlation_id="corr-arp",
            ),
            MeasurementTooling(queries=CohortQueries(), publications=stub, periods=_Served()),
        )


HANDOFF_ENTRY = """\
  - id: retention
    display_name: Retention
    route: /retention/
    purpose: Placeholder page purpose; the UI sub-slice writes the page copy.
    primary_job: Placeholder primary job; the UI sub-slice writes the page copy.
    mode: selectable
    boundary_required: true
    compatibility_routes: []
    vintage_policy:
      selector: as_of_month
      default: latest_with_current_publication
      comparison: none
    artifacts:
      - id: retention
        type: RetentionCohortArtifact
        version: 1
        outcomes: [complete, empty, insufficient]
    visuals: []
    sections:
      - {id: workspace-scope, cardinality: exactly_one}
      - {id: path, cardinality: exactly_one}
      - {id: triangle, cardinality: exactly_one, metrics: [repeat_buyer_rate]}
      - id: retention-curve
        cardinality: exactly_one
        metrics: [cumulative_repeat_rate_account_ids]
      - {id: retention-insights, cardinality: exactly_one}
      - {id: evidence-trace, cardinality: exactly_one}
      - {id: boundary, cardinality: exactly_one}
    actions: []
"""
"""The v2 entry as `docs/ui/retention/low-level-design.md` declares it, minus the ADR-115
`navigation` block this branch's loader doesn't read yet."""


def _require_temporal_control_v2(pages: str) -> None:
    """Refuse a root the vintage hand-off entry can't join: it needs temporal control v2."""
    assert "\ntemporal_control_version: 2\n" in pages, "pages.yaml must declare temporal control v2"


def _declared_root(tmp_path: pathlib.Path) -> pathlib.Path:
    committed = (ROOT / "presentation/pages/v2/pages.yaml").read_text()
    _require_temporal_control_v2(committed)
    target = tmp_path / "presentation/pages/v2/pages.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(committed)
    return tmp_path


def test_the_hand_off_root_must_declare_temporal_control_v2() -> None:
    committed = (ROOT / "presentation/pages/v2/pages.yaml").read_text()
    _require_temporal_control_v2(committed)
    # Paired control: a root still declaring v1 fails the check.
    lowered = committed.replace("temporal_control_version: 2\n", "temporal_control_version: 1\n", 1)
    assert lowered != committed
    with pytest.raises(AssertionError, match="temporal control v2"):
        _require_temporal_control_v2(lowered)


@dataclass
class _Pointers:
    def current(self, page: PresetArtifactPage, variant: PresetArtifactVariant) -> None:
        del page, variant


def test_vintage_declaration_rejects_an_extra_display_grains_key(tmp_path: pathlib.Path) -> None:
    root = _declared_root(tmp_path)
    path = root / "presentation/pages/v2/pages.yaml"
    committed = path.read_text()
    altered = committed.replace(
        "      comparison: none\n    artifacts:\n",
        "      comparison: none\n      display_grains: [as_of_vintage]\n    artifacts:\n",
        1,
    )
    assert altered != committed
    path.write_text(altered)
    with pytest.raises(PageSchemaError, match="vintage_policy"):
        load_v2_page_schemas(root)


def test_the_committed_page_entry_opens_the_registered_materializer() -> None:
    schemas = load_v2_page_schemas(ROOT)
    assert RETENTION_PAGE_ID in {page.page_id for page in schemas.pages}
    assert retention_policy(schemas) == PresentationVintagePolicy()
    # Paired control: a registry without the Retention baker still fails closed by name.
    baker = RegisteredPresetArtifactBaker(
        bakers={
            "campaigns": CampaignPresetArtifactBaker(UnavailableCampaignPresetArtifactSource()),
            "retention": RetentionPresetArtifactBaker(UnavailableRetentionPresetArtifactSource()),
        },
        fallback=UnavailablePresetArtifactBaker(),
        roster_pages=("campaigns", "retention"),
    )
    result = baker.bake(BakeIntent(RETENTION_PAGE_ID, LATEST_VINTAGE_VARIANT))
    assert isinstance(result, BakeUnavailable)
    assert result.reason == "retention_materialization_unavailable"


def test_only_an_entry_declaring_retention_v1_and_its_sections_opens_the_source(
    tmp_path: pathlib.Path,
) -> None:
    root = _declared_root(tmp_path)
    schemas = load_v2_page_schemas(root)
    page = next(item for item in schemas.pages if item.page_id == RETENTION_PAGE_ID)
    (artifact,) = page.artifacts

    def policy(**changes: object) -> PresentationVintagePolicy | None:
        changed = replace(page, **changes)  # type: ignore[arg-type]
        others = tuple(item for item in schemas.pages if item.page_id != RETENTION_PAGE_ID)
        return retention_policy(replace(schemas, pages=(*others, changed)))

    # Positive control: the hand-off entry opens the source, and its vintage page bakes the
    # policy's default variant.
    assert policy() == PresentationVintagePolicy()
    intents = load_declared_bake_intents(root).intents
    assert BakeIntent(RETENTION_PAGE_ID, LATEST_VINTAGE_VARIANT) in intents
    refused = {
        "wrong type": {"artifacts": (replace(artifact, artifact_type="ReachAndFunnelArtifact"),)},
        "wrong version": {"artifacts": (replace(artifact, version=2),)},
        "an outcome missing": {"artifacts": (replace(artifact, outcomes=("complete", "empty")),)},
        "curve missing": {
            "sections": tuple(
                item for item in page.sections if item.section_id != "retention-curve"
            )
        },
    }
    assert {name: policy(**changes) for name, changes in refused.items()} == dict.fromkeys(refused)


@dataclass
class _Compiler:
    renderer: RetentionArtifactRenderer

    def render_retention(self, request: RetentionRenderInput) -> RetentionRenderResult:
        return self.renderer.render_retention(request)


def test_an_injected_page_entry_bakes_a_publication_equal_to_the_planned_answers() -> None:
    earlier = VINTAGE.previous()
    source = RetainedRetentionPresetArtifactSource(
        vintage_policy=PresentationVintagePolicy(),
        compiler=_Compiler(_renderer(CohortQueries())),
        served=lambda: frozenset({(STANDARD, VINTAGE), (STANDARD, earlier)}),
        pointers=cast("object", _Pointers()),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    # The default vintage is the latest with a current pack 14 publication.
    assert source.resolve() == RetentionRenderInput(VINTAGE)
    baker = RegisteredPresetArtifactBaker(
        bakers={
            "campaigns": CampaignPresetArtifactBaker(UnavailableCampaignPresetArtifactSource()),
            "retention": RetentionPresetArtifactBaker(source),
        },
        fallback=UnavailablePresetArtifactBaker(),
        roster_pages=("campaigns", "retention"),
    )
    result = baker.bake(BakeIntent(RETENTION_PAGE_ID, LATEST_VINTAGE_VARIANT))
    assert isinstance(result, BakePublication)
    body = json.loads(result.request.body.payload_json)
    assert result.request.pointer_template.complete(None).pointer.resolved_window == VINTAGE.window
    assert body == _baked()
    assert (body["type"], body["version"], body["outcome"]) == (
        "RetentionCohortArtifact",
        1,
        "complete",
    )
    # Control: a variant other than the policy default bakes nothing.
    other = baker.bake(BakeIntent(RETENTION_PAGE_ID, "last_complete_month"))
    assert isinstance(other, BakeUnavailable)
