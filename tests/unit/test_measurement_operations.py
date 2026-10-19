# SPDX-License-Identifier: MIT-0
"""The two operations beyond a breakdown — `measurement_compare_windows` and `measurement_top_n`.

Driven through `handle` against the **real** metric dictionary and the **real** contracts, for the
reason `test_measurement_tool.py` gives: the decisions under test are about what those declare.
`vendor-sales-by-asin` declares a 45-day restatement window, and the comparison caveat this tool
exists for is derived from that number rather than written out — a hand-built contract would let the
sentence pass while the shipped declaration said something else.

The query port stub answers **each window differently**, which is the point. A stub returning one
set of rows for both periods would make every key comparable by construction and hide the rule that
matters most: a key present in only one period reports no change, because an absent period is either
one with no rows or one Amazon withheld.

Four things carry most of the weight:

- **Truncation is a refusal, not a caveat.** Both operations are claims about the whole population,
  and neither query orders its own output, so a page of rows gives an arbitrary ranking and a grand
  total short by an unstatable amount.
- **The restatement asymmetry is stated with a duration.** Comparing an unsettled period against a
  settled one is the caveat `compare_windows` earns its own tool on.
- **A ranking carries the grand total, and says why when it cannot.** An unexplained absence invites
  the model to supply the sum itself.
- **Overlapping periods are refused**, because two windows sharing days double-count them.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from threading import Event, Lock

import pytest
from publication_support import StubPublications

from adapters.inbound.tool_handlers import measurement as measurement_module
from adapters.inbound.tool_handlers.gateway import to_json_safe
from adapters.inbound.tool_handlers.measurement import (
    QUERY_FAN_IN_COLLISION,
    MeasurementExecutionTooling,
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    bind_batch,
    distinct_query_count,
    execute_batch,
    execute_presentation_batch,
    execute_presentation_query_batch,
    handle,
    handle_batch,
    prepare_batch,
    query_request_digest,
)
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.freshness import Coverage
from domain.data_contracts.contract import DataContract
from domain.measurement.comparison import (
    CHANGE_ALIAS,
    CHANGE_PERCENT_ALIAS,
    CURRENT_ALIAS,
    PRIOR_ALIAS,
    RANK_ALIAS,
    RankOrder,
)
from domain.measurement.planning import QueryPlan, measure_alias
from domain.narrative.publication import QualifierKind, RenderingKind
from domain.narrative.shaped_response import ResolutionProvenance, ShapedResponse, UnitKind
from domain.semantics.analytical_resolution import FieldOrigin
from domain.shared_kernel.errors import (
    DependencyError,
    DomainError,
    ErrorCode,
    InvariantError,
    ValidationError,
)
from domain.shared_kernel.provenance import FreshnessStatus, MeasurementBasis, Suppression
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionResult,
    PresentationEvidenceInspectionSlot,
    PresentationEvidenceRole,
    PublicationSelection,
    PublicationSelectionRequest,
)
from ports.outbound.query import QueryRequest, QueryResult

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-032", "ADR-010", "ADR-024")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
US = "ATVPDKIKX0DER"

LATER = DateRange(date(2026, 8, 10), date(2026, 8, 16))
"""Seven days, ending three days before `NOW` — inside the 45-day restatement window."""

EARLIER = DateRange(date(2026, 8, 3), date(2026, 8, 9))
"""The seven days before `LATER`, also still restating."""

SETTLED = DateRange(date(2026, 6, 1), date(2026, 6, 7))
"""Seventy-three days before `NOW`, so outside the restatement window and no longer moving."""

SHORTER = DateRange(date(2026, 8, 1), date(2026, 8, 2))
"""Two days, so a comparison against `LATER` is comparing periods of different lengths."""

Row = dict[str, str | int | Decimal | None]
type Contracts = tuple[DataContract, ...]


def _money(*pairs: tuple[str, str]) -> tuple[Row, ...]:
    """Build monetary rows at ASIN grain, in the shape the Athena adapter returns them."""
    return tuple(
        {"amazon_asin": asin, "currency_code": "USD", measure_alias(0): Decimal(amount)}
        for asin, amount in pairs
    )


def _ratio(*triples: tuple[str, str, str]) -> tuple[Row, ...]:
    """Build two-aggregate rows for a same-fact ratio: numerator then denominator."""
    return tuple(
        {
            "amazon_asin": asin,
            measure_alias(0): Decimal(numerator),
            measure_alias(1): Decimal(denominator),
        }
        for asin, numerator, denominator in triples
    )


def _money_ratio(*triples: tuple[str, str, str]) -> tuple[Row, ...]:
    """Build the same, for a ratio whose *result* is money and so needs a currency in the row.

    `asp_shipped` and `revenue_per_glance_view` are the cases: a quotient denominated in currency,
    which is what G-46 turns on.
    """
    return tuple(
        {
            "amazon_asin": asin,
            "currency_code": "USD",
            measure_alias(0): Decimal(numerator),
            measure_alias(1): Decimal(denominator),
        }
        for asin, numerator, denominator in triples
    )


@dataclass
class WindowedQueries:
    """A `QueryPort` stub that answers each window with its own rows.

    Keyed on the plan's window because that is the only thing differing between a comparison's two
    reads — same metric, same grain, same filters — so it is also the only key a stub can honestly
    dispatch on.
    """

    by_window: dict[DateRange, tuple[Row, ...]] = field(
        default_factory=dict[DateRange, tuple[Row, ...]]
    )
    truncated: bool = False
    suppressed: dict[DateRange, Suppression] = field(default_factory=dict[DateRange, Suppression])
    failure: Exception | None = None
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        if self.failure is not None:
            raise self.failure
        window = request.plan.logical.window
        return QueryResult(
            rows=self.by_window.get(window, ()),
            bytes_scanned=1_024,
            truncated=self.truncated,
            suppression=self.suppressed.get(window, Suppression(rows_suppressed=False)),
        )


@dataclass
class ConcurrentWindowedQueries:
    """A thread-safe stub that makes a comparator complete before its current period."""

    current_started: Event = field(default_factory=Event)
    comparator_completed: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)
    active: int = 0
    max_active: int = 0
    completed: list[DateRange] = field(default_factory=list[DateRange])

    def run(self, request: QueryRequest) -> QueryResult:
        window = request.plan.logical.window
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if window == LATER:
                self.current_started.set()
                assert self.comparator_completed.wait(timeout=1)
            else:
                assert self.current_started.wait(timeout=1)
                self.comparator_completed.set()
            with self.lock:
                self.completed.append(window)
            return QueryResult(
                rows=(),
                bytes_scanned=1_024,
                truncated=False,
                suppression=Suppression(rows_suppressed=False),
            )
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture(scope="module")
def contracts() -> Contracts:
    return tuple(contract for _, contract in load_contracts(ROOT))


def _request(contracts: Contracts, **overrides: object) -> MeasurementToolRequest:
    fields: dict[str, object] = {
        "tool": MeasurementTool.COMPARE_WINDOWS,
        "catalog": load_catalog(ROOT),
        "contracts": contracts,
        "metric_name": "retail_revenue_shipped",
        "grains": ("product",),
        "window": LATER,
        "prior_window": EARLIER,
        "computed_at": NOW,
        "correlation_id": "corr-1",
        "filters": {"marketplace_key": US},
    }
    fields.update(overrides)
    return MeasurementToolRequest(**fields)  # type: ignore[arg-type]


def _answer(contracts: Contracts, **overrides: object) -> ShapedResponse:
    """Run one invocation, with per-window rows the caller supplies."""
    queries = WindowedQueries(
        by_window=overrides.pop("by_window", {}),  # type: ignore[arg-type]
        truncated=bool(overrides.pop("truncated", False)),
        suppressed=overrides.pop("suppressed", {}),  # type: ignore[arg-type]
    )
    publications = StubPublications(
        coverage=overrides.pop("coverage", None),  # type: ignore[arg-type]
        qualified_dq=overrides.pop("qualified_dq", ()),  # type: ignore[arg-type]
    )
    return handle(
        _request(contracts, **overrides),
        MeasurementTooling(queries=queries, publications=publications),
    )


def _selection_failure() -> DependencyError:
    return DependencyError(
        ErrorCode("publication-selection", 1),
        "Required Semantic V2 CURRENT coverage is incomplete.",
    )


def _inspected_slot(
    role: PresentationEvidenceRole,
    logical: QueryPlan,
    contracts: Contracts,
) -> PresentationEvidenceInspectionSlot:
    selection = StubPublications().select(PublicationSelectionRequest((logical,), contracts, NOW))
    return PresentationEvidenceInspectionSlot(
        role,
        selection.plans[0],
        selection.coverage,
        selection.qualified_dq,
    )


def _says(answer: ShapedResponse, fragment: str) -> bool:
    """Whether any narrative sentence carries the fragment."""
    return any(fragment in sentence for sentence in answer.narrative)


def _row(answer: ShapedResponse, asin: str) -> dict[str, object]:
    for candidate in answer.data.values:
        if candidate.get("amazon_asin") == asin:
            return dict(candidate)
    raise AssertionError(f"{asin} is not in the answer: {answer.data.values}")


@pytest.mark.parametrize(
    "overrides",
    (
        {
            "by_window": {
                LATER: _money(("B0AAA", "2.0000")),
                EARLIER: _money(("B0AAA", "1.0000")),
            }
        },
        {
            "tool": MeasurementTool.TOP_N,
            "prior_window": None,
            "n": 1,
            "by_window": {LATER: _money(("B0AAA", "2.0000"))},
        },
    ),
)
def test_resolution_provenance_crosses_comparison_and_ranking_answers(
    overrides: dict[str, object],
    contracts: Contracts,
) -> None:
    provenance = (
        ResolutionProvenance(
            "metric", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, "retail_revenue_shipped"
        ),
    )

    answer = _answer(contracts, resolution_provenance=provenance, **overrides)

    assert answer.metadata.resolution_provenance == provenance
    encoded = to_json_safe(answer)
    assert encoded["metadata"]["resolution_provenance"] == [
        {
            "field": "metric",
            "origin": "explicit_customer_text",
            "value": "retail_revenue_shipped",
        }
    ]


class TestComparingTwoPeriods:
    def test_each_row_carries_both_figures_the_change_and_the_percentage(
        self, contracts: Contracts
    ) -> None:
        """Asserted with units and grain, never as bare numbers."""
        answer = _answer(
            contracts,
            by_window={
                LATER: _money(("B0AAA", "120.0000")),
                EARLIER: _money(("B0AAA", "100.0000")),
            },
        )

        assert answer.data.units is not None
        assert answer.data.units.kind is UnitKind.CURRENCY
        assert answer.data.units.currency_code == "USD"
        assert answer.data.grain.dimensions == ("amazon_asin",)
        assert _row(answer, "B0AAA") == {
            "amazon_asin": "B0AAA",
            CURRENT_ALIAS: Decimal("120.0000"),
            PRIOR_ALIAS: Decimal("100.0000"),
            CHANGE_ALIAS: Decimal("20.0000"),
            CHANGE_PERCENT_ALIAS: "+20.00%",
        }
        grounding = answer.metadata.grounding
        assert grounding is not None
        claims = {
            item.rendering_id: item.text
            for item in grounding.renderings
            if item.kind is RenderingKind.CLAIM
        }
        assert any(
            "Current Retail revenue (shipped) was 120.0000 USD " in text for text in claims.values()
        )
        assert any(
            "Percentage change in Retail revenue (shipped) was +20.00% " in text
            for text in claims.values()
        )
        assert all("Rank was" not in text for text in claims.values())

    def test_it_reads_both_windows_and_nothing_else(self, contracts: Contracts) -> None:
        """Two queries, identical but for the period, so the change is the period's."""
        queries = WindowedQueries(
            by_window={LATER: _money(("B0AAA", "1.0000")), EARLIER: _money(("B0AAA", "1.0000"))}
        )
        publications = StubPublications()
        handle(
            _request(contracts),
            MeasurementTooling(queries=queries, publications=publications),
        )

        assert len(publications.seen) == 1
        assert [plan.window for plan in publications.seen[0].plans] == [LATER, EARLIER]
        assert [seen.plan.logical.window for seen in queries.seen] == [LATER, EARLIER]
        assert {seen.plan.logical.metric.name for seen in queries.seen} == {
            "retail_revenue_shipped"
        }
        assert {tuple(sorted(seen.plan.logical.filters)) for seen in queries.seen} == {
            tuple(sorted(queries.seen[0].plan.logical.filters))
        }

    def test_the_metadata_span_reaches_back_to_the_earlier_period(
        self, contracts: Contracts
    ) -> None:
        """A comparison whose earlier period predates the loaded window is what coverage catches.

        Reporting only the recent bound would hide it.
        """
        answer = _answer(
            contracts,
            prior_window=SETTLED,
            by_window={
                LATER: _money(("B0AAA", "120.0000")),
                SETTLED: _money(("B0AAA", "100.0000")),
            },
        )
        assert answer.metadata.coverage_start.date() == SETTLED.start
        assert answer.metadata.coverage_end.date() == LATER.end
        assert answer.metadata.requested_coverage_intervals == (SETTLED, LATER)
        assert answer.metadata.actual_coverage_intervals == (SETTLED, LATER)
        assert QualifierKind.PARTIAL not in {
            qualifier.kind for qualifier in answer.metadata.qualifiers
        }

    def test_exact_holes_reach_metadata_grounding_and_json(self, contracts: Contracts) -> None:
        coverage = Coverage(
            fact_name="fct_retail_sales",
            window=LATER,
            freshness=FreshnessStatus.FRESH,
            measurement_basis=MeasurementBasis.RETAIL_ACTUAL,
            valid_grains=("product",),
            loaded_at=NOW,
            intervals=(LATER,),
            holes=(SETTLED,),
        )
        answer = _answer(
            contracts,
            prior_window=SETTLED,
            coverage=coverage,
            by_window={
                LATER: _money(("B0AAA", "120.0000")),
                SETTLED: _money(("B0AAA", "100.0000")),
            },
        )

        assert answer.metadata.actual_coverage_intervals == (LATER,)
        assert answer.metadata.coverage_holes == (SETTLED,)
        partial = next(
            qualifier
            for qualifier in answer.metadata.qualifiers
            if qualifier.kind is QualifierKind.PARTIAL
        )
        assert partial.source_paths == (
            "metadata.actual_coverage_intervals",
            "metadata.coverage_holes",
        )
        grounding = answer.metadata.grounding
        assert grounding is not None
        rendering = next(
            item for item in grounding.renderings if item.qualifier is QualifierKind.PARTIAL
        )
        assert rendering.source_paths == partial.source_paths
        encoded = to_json_safe(answer)
        metadata = encoded["metadata"]
        assert metadata["actual_coverage_intervals"] == [
            {"start": LATER.start.isoformat(), "end": LATER.end.isoformat()}
        ]
        assert metadata["coverage_holes"] == [
            {"start": SETTLED.start.isoformat(), "end": SETTLED.end.isoformat()}
        ]

    def test_the_row_count_matches_the_keys_either_period_reported(
        self, contracts: Contracts
    ) -> None:
        answer = _answer(
            contracts,
            by_window={
                LATER: _money(("B0AAA", "1.0000"), ("B0NEW", "2.0000")),
                EARLIER: _money(("B0AAA", "1.0000"), ("B0GONE", "3.0000")),
            },
        )
        assert answer.metadata.row_count == 3
        assert len(answer.data.values) == 3


class TestTheCaveatAComparisonEarnsItsOwnToolOn:
    def test_an_unsettled_period_against_a_settled_one_is_stated_with_its_duration(
        self, contracts: Contracts
    ) -> None:
        """Derived from the contract's declared 45 days, so it cannot drift from the declaration."""
        answer = _answer(
            contracts,
            prior_window=SETTLED,
            by_window={
                LATER: _money(("B0AAA", "120.0000")),
                SETTLED: _money(("B0AAA", "100.0000")),
            },
        )
        assert _says(answer, "up to 45 days after publication")
        assert _says(answer, "unsettled figure against a settled one")

    def test_two_unsettled_periods_are_called_provisional(self, contracts: Contracts) -> None:
        answer = _answer(
            contracts,
            by_window={
                LATER: _money(("B0AAA", "120.0000")),
                EARLIER: _money(("B0AAA", "100.0000")),
            },
        )
        assert _says(answer, "45-day restatement window")
        assert _says(answer, "the change between them is provisional")
        assert not _says(answer, "unsettled figure against a settled one")

    def test_two_settled_periods_say_neither_will_move(self, contracts: Contracts) -> None:
        old = DateRange(date(2026, 1, 5), date(2026, 1, 11))
        older = DateRange(date(2025, 12, 1), date(2025, 12, 7))
        answer = _answer(
            contracts,
            window=old,
            prior_window=older,
            by_window={old: _money(("B0AAA", "120.0000")), older: _money(("B0AAA", "100.0000"))},
        )
        assert _says(answer, "neither figure is expected to move further")

    def test_the_generic_breakdown_caveat_is_not_used_for_a_comparison(
        self, contracts: Contracts
    ) -> None:
        """A recent period will still move says nothing about the asymmetry between two."""
        answer = _answer(
            contracts,
            by_window={
                LATER: _money(("B0AAA", "120.0000")),
                EARLIER: _money(("B0AAA", "100.0000")),
            },
        )
        assert not _says(answer, "so a recent period will still move")


class TestWhatAComparisonMustSayAboutItself:
    def test_periods_of_different_lengths_are_named_as_such(self, contracts: Contracts) -> None:
        """The most common way a comparison misleads, and it is invisible in the figures."""
        answer = _answer(
            contracts,
            prior_window=SHORTER,
            by_window={
                LATER: _money(("B0AAA", "120.0000")),
                SHORTER: _money(("B0AAA", "100.0000")),
            },
        )
        assert _says(answer, "7 day(s) against 2")
        assert _says(answer, "as well as any change in performance")

    def test_equal_lengths_say_nothing_about_length(self, contracts: Contracts) -> None:
        answer = _answer(
            contracts,
            by_window={
                LATER: _money(("B0AAA", "120.0000")),
                EARLIER: _money(("B0AAA", "100.0000")),
            },
        )
        assert not _says(answer, "different lengths")

    def test_a_key_in_only_one_period_is_counted_and_explained(self, contracts: Contracts) -> None:
        """Both readings of the absence are named, because the count cannot tell them apart."""
        answer = _answer(
            contracts,
            by_window={
                LATER: _money(("B0AAA", "120.0000"), ("B0NEW", "5.0000")),
                EARLIER: _money(("B0AAA", "100.0000")),
            },
        )
        assert _says(answer, "1 of 2 row(s) appear in only one of the two periods")
        assert _says(answer, "Amazon withheld")
        assert _row(answer, "B0NEW")[CHANGE_ALIAS] is None

    def test_a_change_from_zero_explains_its_missing_percentage(self, contracts: Contracts) -> None:
        answer = _answer(
            contracts,
            by_window={
                LATER: _money(("B0AAA", "40.0000")),
                EARLIER: _money(("B0AAA", "0.0000")),
            },
        )
        assert _says(answer, "Growth from nothing has no percentage")
        assert _row(answer, "B0AAA")[CHANGE_ALIAS] == Decimal("40.0000")
        assert _row(answer, "B0AAA")[CHANGE_PERCENT_ALIAS] is None

    def test_two_empty_periods_are_an_absence_rather_than_a_zero(
        self, contracts: Contracts
    ) -> None:
        answer = _answer(contracts, by_window={})
        assert answer.data.values == ()
        assert answer.data.units is None
        assert _says(answer, "absence of data rather than a figure of zero")

    def test_a_withholding_in_the_earlier_period_reaches_the_answer(
        self, contracts: Contracts
    ) -> None:
        """A key whose earlier figure was withheld has an unknown change.

        However clean the later period is. Reporting no suppression because the recent side was
        complete would present that change as measured.
        """
        answer = _answer(
            contracts,
            by_window={
                LATER: _money(("B0AAA", "120.0000")),
                EARLIER: _money(("B0AAA", "100.0000")),
            },
            suppressed={
                EARLIER: Suppression(
                    rows_suppressed=True, reason="Below the AMC privacy threshold."
                )
            },
        )
        assert answer.metadata.suppression.rows_suppressed
        assert _says(answer, "In the earlier period")


class TestQualifiedDqAcrossOperations:
    def test_comparison_preserves_current_publication_caveat(self, contracts: Contracts) -> None:
        caveat = "Comparison input passed under qualified DQ."
        answer = _answer(contracts, qualified_dq=(caveat,))

        assert answer.metadata.qualified_dq == (caveat,)
        assert QualifierKind.QUALIFIED_DQ in {item.kind for item in answer.metadata.qualifiers}

    def test_ranking_preserves_current_publication_caveat(self, contracts: Contracts) -> None:
        caveat = "Ranking input passed under qualified DQ."
        answer = _answer(
            contracts,
            tool=MeasurementTool.TOP_N,
            prior_window=None,
            n=1,
            by_window={LATER: _money(("B0AAA", "10.0000"))},
            qualified_dq=(caveat,),
        )

        assert answer.metadata.qualified_dq == (caveat,)
        assert QualifierKind.QUALIFIED_DQ in {item.kind for item in answer.metadata.qualifiers}


class TestWhatTheseOperationsRefuse:
    def test_missing_publication_for_either_window_fails_before_both_queries(
        self, contracts: Contracts
    ) -> None:
        queries = WindowedQueries()
        publications = StubPublications(failure=_selection_failure())

        with pytest.raises(DependencyError, match="CURRENT coverage is incomplete"):
            handle(
                _request(contracts),
                MeasurementTooling(queries=queries, publications=publications),
            )

        assert queries.seen == []
        assert len(publications.seen) == 1
        assert [plan.window for plan in publications.seen[0].plans] == [LATER, EARLIER]

    def test_a_comparison_without_a_prior_window_is_refused(self, contracts: Contracts) -> None:
        with pytest.raises(ValidationError, match="prior_window_start"):
            _request(contracts, prior_window=None)

    def test_overlapping_periods_are_refused(self, contracts: Contracts) -> None:
        """Two windows sharing days double-count them, so a period is compared with itself."""
        with pytest.raises(ValidationError, match="double-count"):
            _request(contracts, prior_window=DateRange(date(2026, 8, 5), date(2026, 8, 12)))

    def test_a_prior_period_that_starts_after_the_later_one_is_refused(
        self, contracts: Contracts
    ) -> None:
        with pytest.raises(ValidationError, match="end before"):
            _request(contracts, prior_window=DateRange(date(2026, 9, 1), date(2026, 9, 7)))

    def test_a_truncated_comparison_is_refused_rather_than_caveated(
        self, contracts: Contracts
    ) -> None:
        """A page of two result sets pairs arbitrary subsets.

        A key present in one page and absent from the other reads as a period with no figure, when
        it is really a period this answer did not look at.
        """
        with pytest.raises(DomainError, match="whole population"):
            _answer(
                contracts,
                truncated=True,
                by_window={
                    LATER: _money(("B0AAA", "120.0000")),
                    EARLIER: _money(("B0AAA", "100.0000")),
                },
            )

    def test_a_truncated_ranking_is_refused_rather_than_caveated(
        self, contracts: Contracts
    ) -> None:
        """The top ten of an arbitrary page is not the top ten, and no caveat repairs that."""
        with pytest.raises(DomainError, match="whole population"):
            _answer(
                contracts,
                tool=MeasurementTool.TOP_N,
                prior_window=None,
                n=2,
                truncated=True,
                by_window={LATER: _money(("B0AAA", "120.0000"))},
            )

    def test_a_row_ceiling_above_what_the_port_accepts_is_refused_here(
        self, contracts: Contracts
    ) -> None:
        """Refused as a request, not left to raise a bare ValueError out of the port.

        `max_rows` is a declared tool argument, so any caller can reach `QueryRequest`'s own
        ceiling. That refusal is a `ValueError`, which is not an `AdlyticsError` — so it escaped
        the entry point and Gateway reported a broken tool rather than a declined question.
        `max_rows: 999999` returned `FunctionError: Unhandled` on deployed dev.
        """
        with pytest.raises(ValidationError, match="max_rows must be between"):
            _request(
                contracts,
                tool=MeasurementTool.METRIC_BY_GRAIN,
                prior_window=None,
                max_rows=999_999,
            )

    def test_a_row_ceiling_below_one_is_refused(self, contracts: Contracts) -> None:
        with pytest.raises(ValidationError, match="max_rows must be between"):
            _request(contracts, tool=MeasurementTool.METRIC_BY_GRAIN, prior_window=None, max_rows=0)

    def test_a_ranking_needs_a_size(self, contracts: Contracts) -> None:
        with pytest.raises(ValidationError, match="at least 1"):
            _request(contracts, tool=MeasurementTool.TOP_N, prior_window=None)

    def test_ranking_by_change_needs_a_prior_period(self, contracts: Contracts) -> None:
        with pytest.raises(ValidationError, match="prior_window_start"):
            _request(
                contracts,
                tool=MeasurementTool.TOP_N,
                prior_window=None,
                n=5,
                order_by=RankOrder.CHANGE,
            )

    def test_ranking_by_value_with_a_prior_period_is_refused(self, contracts: Contracts) -> None:
        """A caller who sent a prior window meant to rank by change.

        Ordering by value anyway would answer a different question with every argument they
        supplied looking honoured.
        """
        with pytest.raises(ValidationError, match="ordered by VALUE"):
            _request(contracts, tool=MeasurementTool.TOP_N, n=5, order_by=RankOrder.VALUE)


class TestARankingCarriesItsGrandTotal:
    def test_the_rows_are_numbered_and_ordered_largest_first(self, contracts: Contracts) -> None:
        answer = _answer(
            contracts,
            tool=MeasurementTool.TOP_N,
            prior_window=None,
            n=2,
            by_window={
                LATER: _money(("B0AAA", "10.0000"), ("B0BBB", "300.0000"), ("B0CCC", "200.0000"))
            },
        )
        assert [row[RANK_ALIAS] for row in answer.data.values] == [1, 2]
        assert [row["amazon_asin"] for row in answer.data.values] == ["B0BBB", "B0CCC"]
        assert answer.metadata.row_count == 2
        grounding = answer.metadata.grounding
        assert grounding is not None
        claims = tuple(
            item.text for item in grounding.renderings if item.kind is RenderingKind.CLAIM
        )
        assert any(
            text == "Rank was 1 for amazon asin B0BBB, at the amazon asin grain." for text in claims
        )
        assert any(
            "Retail revenue (shipped) was 300.0000 USD for amazon asin B0BBB" in text
            for text in claims
        )

    def test_the_total_and_the_subtotal_are_both_pre_computed_and_denominated(
        self, contracts: Contracts
    ) -> None:
        """Ten campaigns of five hundred with no denominator invites "so that is most of my spend".

        Both ends arrive already added, because the reader who would otherwise sum them is the
        model — which never sums, divides or infers a figure.
        """
        answer = _answer(
            contracts,
            tool=MeasurementTool.TOP_N,
            prior_window=None,
            n=2,
            by_window={
                LATER: _money(("B0AAA", "10.0000"), ("B0BBB", "300.0000"), ("B0CCC", "200.0000"))
            },
        )
        assert _says(answer, "account for 500.0000 USD of 510.0000 USD across all 3")
        assert _says(answer, "1 row(s) are not shown")

    def test_a_ratio_states_why_it_has_no_total(self, contracts: Contracts) -> None:
        """A sum of ratios is not a ratio.

        An unexplained absence invites the model to supply the sum itself.
        """
        answer = _answer(
            contracts,
            tool=MeasurementTool.TOP_N,
            metric_name="return_rate",
            prior_window=None,
            n=1,
            by_window={
                LATER: _ratio(("B0AAA", "2.0000", "10.0000"), ("B0BBB", "1.0000", "4.0000"))
            },
        )
        assert answer.data.units is not None
        assert answer.data.units.kind is UnitKind.RATIO
        assert answer.data.population_total is None
        assert _says(answer, "No total is stated across these rows")
        assert _says(answer, "a sum of ratios is not one")

    def test_a_ranking_never_claims_its_result_was_truncated(self, contracts: Contracts) -> None:
        """Truncation is a refusal on this path, so the caveat is unreachable and must stay so.

        `_ranking_narrative` hard-codes `truncated=False` into its qualifiers because the tool
        raises `RESULT_TOO_WIDE` before reaching here. Nothing asserted the consequence, so
        flipping that literal emitted a page disclosure over a whole-population answer — a caveat
        that is false, arriving with the platform's authority. Found by mutation.
        """
        answer = _answer(
            contracts,
            tool=MeasurementTool.TOP_N,
            prior_window=None,
            n=2,
            by_window={LATER: _money(("B0AAA", "10.0000"), ("B0BBB", "300.0000"))},
        )

        assert answer.metadata.truncated is False
        assert not _says(answer, "truncat")

    @pytest.mark.covers("G-45")
    def test_the_total_it_states_is_also_carried_structurally(self, contracts: Contracts) -> None:
        """The same figure in the envelope and in the sentence, so a reader of either agrees.

        Prose alone made the ranking's total the one figure a check over the narration could not
        verify against an envelope, which is the exception that stops such a check being written.
        """
        answer = _answer(
            contracts,
            tool=MeasurementTool.TOP_N,
            prior_window=None,
            n=2,
            by_window={
                LATER: _money(("B0AAA", "10.0000"), ("B0BBB", "300.0000"), ("B0CCC", "200.0000"))
            },
        )
        total = answer.data.population_total
        assert total == Decimal("510.0000")
        assert _says(answer, f"of {total} USD across all 3")

    @pytest.mark.covers("G-47")
    def test_no_total_is_claimed_over_a_population_amazon_withheld_part_of(
        self, contracts: Contracts
    ) -> None:
        """G-47. The figure was right and the sentence around it was not.

        Every measure folds to what could be attributed and what Amazon withheld separately
        (ADR-035), so `Ranking.total` sums the attributable side and excludes the withheld part by
        construction. It was nonetheless stated as covering "the whole population, including them"
        — two sentences after the disclosure saying these figures will not reconcile to a total.
        """
        answer = _answer(
            contracts,
            tool=MeasurementTool.TOP_N,
            prior_window=None,
            n=2,
            by_window={
                LATER: _money(("B0AAA", "10.0000"), ("B0BBB", "300.0000"), ("B0CCC", "200.0000"))
            },
            suppressed={
                LATER: Suppression(
                    rows_suppressed=True,
                    reason="Amazon withheld 12.0000 revenue across 1 of 3 amazon_asin value(s).",
                )
            },
        )

        assert answer.data.population_total is None
        assert not _says(answer, "covers the whole population"), (
            "the withheld amount is outside the total, so completeness cannot be claimed"
        )
        assert not _says(answer, "account for"), "no subtotal-of-total sentence without a total"
        assert _says(answer, "Amazon withheld part of this population")
        assert _says(answer, "1 of 3 row(s) are not shown"), (
            "the omission still has to be quantified — refusing the total is not refusing the count"
        )

    @pytest.mark.covers("G-46")
    def test_a_ratio_denominated_in_currency_is_not_summed(self, contracts: Contracts) -> None:
        """G-47's sibling, and the one that was producing a figure rather than a wrong sentence.

        `asp_shipped` is revenue over units — a quotient — and it declares **currency**, so it
        passed the unit-kind gate `total_of` uses. A top-N over two ASINs each averaging 10.00
        reported a grand total of 20.0000 USD "covering the whole population": a sum of average
        selling prices, computed by the platform and labelled as a population figure.
        """
        answer = _answer(
            contracts,
            tool=MeasurementTool.TOP_N,
            metric_name="asp_shipped",
            grains=("product",),
            prior_window=None,
            n=1,
            by_window={
                LATER: _money_ratio(
                    ("B0AAA", "100.0000", "10.0000"), ("B0BBB", "40.0000", "4.0000")
                )
            },
        )

        assert answer.data.units is not None
        assert answer.data.units.kind is UnitKind.CURRENCY, (
            "the point of the case: currency units on something that is not summable"
        )
        assert answer.data.population_total is None
        assert not _says(answer, "20.0000 USD")
        assert _says(answer, "is a ratio, and a sum of ratios is not one")
        assert not _says(answer, "distinct people")

    def test_an_empty_population_says_there_is_nothing_to_rank(self, contracts: Contracts) -> None:
        answer = _answer(
            contracts,
            tool=MeasurementTool.TOP_N,
            prior_window=None,
            n=5,
            by_window={},
        )
        assert answer.data.values == ()
        assert answer.data.units is None
        assert _says(answer, "nothing to rank")
        assert _says(answer, "absence of data rather than a figure of zero")


class TestARankingByChange:
    def test_it_ranks_the_movers_and_excludes_what_cannot_move(self, contracts: Contracts) -> None:
        answer = _answer(
            contracts,
            tool=MeasurementTool.TOP_N,
            n=2,
            order_by=RankOrder.CHANGE,
            by_window={
                LATER: _money(("B0AAA", "110.0000"), ("B0BBB", "300.0000"), ("B0NEW", "900.0000")),
                EARLIER: _money(("B0AAA", "100.0000"), ("B0BBB", "100.0000")),
            },
        )

        assert [row["amazon_asin"] for row in answer.data.values] == ["B0BBB", "B0AAA"]
        assert [row[CHANGE_ALIAS] for row in answer.data.values] == [
            Decimal("200.0000"),
            Decimal("10.0000"),
        ]
        assert _says(answer, "appear in only one of the two periods and are excluded")

    def test_it_carries_the_comparison_restatement_caveat(self, contracts: Contracts) -> None:
        """A ranking by change is ranking a comparison, so it inherits the asymmetry."""
        answer = _answer(
            contracts,
            tool=MeasurementTool.TOP_N,
            prior_window=SETTLED,
            n=1,
            order_by=RankOrder.CHANGE,
            by_window={
                LATER: _money(("B0AAA", "110.0000")),
                SETTLED: _money(("B0AAA", "100.0000")),
            },
        )
        assert _says(answer, "unsettled figure against a settled one")

    def test_nothing_comparable_says_so_rather_than_reporting_an_empty_window(
        self, contracts: Contracts
    ) -> None:
        """Two different absences. The window held rows; none of them has a change."""
        answer = _answer(
            contracts,
            tool=MeasurementTool.TOP_N,
            n=5,
            order_by=RankOrder.CHANGE,
            by_window={LATER: _money(("B0NEW", "900.0000")), EARLIER: ()},
        )
        assert answer.data.values == ()
        assert answer.data.units is None
        assert _says(answer, "appears in only one of the two periods")
        assert not _says(answer, "absence of data rather than a figure of zero")


@dataclass
class ReorderedPublications:
    """Return valid bound plans in the wrong order to exercise the exact-plan guard."""

    seen: list[PublicationSelectionRequest] = field(
        default_factory=list[PublicationSelectionRequest]
    )

    def select(self, request: PublicationSelectionRequest) -> PublicationSelection:
        self.seen.append(request)
        selected = StubPublications().select(request)
        return PublicationSelection(
            plans=tuple(reversed(selected.plans)),
            coverage=selected.coverage,
            qualified_dq=selected.qualified_dq,
        )


class TestPageMeasurementBatch:
    def test_typed_seams_prepare_then_bind_then_execute(self, contracts: Contracts) -> None:
        queries = WindowedQueries()
        publications = StubPublications()

        prepared = prepare_batch((_request(contracts),))

        assert tuple(plan.role for plan in prepared.plans) == (
            PresentationEvidenceRole.CURRENT,
            PresentationEvidenceRole.COMPARATOR,
        )
        assert publications.seen == []
        assert queries.seen == []

        bound = bind_batch(prepared, publications)

        assert len(publications.seen) == 1
        assert queries.seen == []

        answers = execute_batch(bound, MeasurementExecutionTooling(queries=queries))

        assert len(answers) == 1
        assert tuple(request.plan.logical.window for request in queries.seen) == (
            LATER,
            EARLIER,
        )

    def test_ordinary_batch_runs_current_and_comparator_queries_concurrently(
        self, contracts: Contracts
    ) -> None:
        queries = ConcurrentWindowedQueries()
        bound = bind_batch(prepare_batch((_request(contracts),)), StubPublications())

        answers = execute_batch(
            bound,
            MeasurementExecutionTooling(queries=queries, query_batch_max_concurrency=2),
        )

        assert answers
        assert queries.max_active == 2
        assert queries.completed == [EARLIER, LATER]

    def test_binding_refuses_reordered_logical_plans_before_querying(
        self, contracts: Contracts
    ) -> None:
        queries = WindowedQueries()
        publications = ReorderedPublications()
        prepared = prepare_batch((_request(contracts),))

        with pytest.raises(InvariantError, match="logical plan sequence"):
            bind_batch(prepared, publications)

        assert len(publications.seen) == 1
        assert queries.seen == []

    def test_shared_raw_presentation_seam_preserves_absent_slots_without_queries(
        self, contracts: Contracts
    ) -> None:
        prepared = prepare_batch((_request(contracts),))
        current, comparator = prepared.plans
        queries = WindowedQueries()
        inspection = PresentationEvidenceInspectionResult(
            (
                PresentationEvidenceInspectionSlot(
                    PresentationEvidenceRole.CURRENT,
                    None,
                ),
                PresentationEvidenceInspectionSlot(
                    PresentationEvidenceRole.COMPARATOR,
                    None,
                ),
            )
        )

        executed = execute_presentation_query_batch(
            prepared,
            inspection,
            MeasurementExecutionTooling(queries=queries),
        )

        assert tuple(slot.logical for slot in executed.slots) == (
            current.logical,
            comparator.logical,
        )
        assert tuple(slot.role for slot in executed.slots) == (
            PresentationEvidenceRole.CURRENT,
            PresentationEvidenceRole.COMPARATOR,
        )
        assert all(not slot.present for slot in executed.slots)
        assert queries.seen == []

    def test_presentation_executes_only_present_prebound_plans_in_role_order(
        self, contracts: Contracts
    ) -> None:
        prepared = prepare_batch((_request(contracts),))
        current, comparator = prepared.plans
        queries = WindowedQueries()
        inspection = PresentationEvidenceInspectionResult(
            (
                PresentationEvidenceInspectionSlot(
                    PresentationEvidenceRole.CURRENT,
                    None,
                ),
                _inspected_slot(
                    PresentationEvidenceRole.COMPARATOR,
                    comparator.logical,
                    contracts,
                ),
            )
        )

        executed = execute_presentation_batch(
            prepared,
            inspection,
            MeasurementExecutionTooling(queries=queries),
        )

        assert tuple(slot.role for slot in executed.slots) == (
            PresentationEvidenceRole.CURRENT,
            PresentationEvidenceRole.COMPARATOR,
        )
        assert executed.slots[0].logical == current.logical
        assert not executed.slots[0].present
        assert executed.slots[0].answer is None
        assert executed.slots[1].logical == comparator.logical
        assert executed.slots[1].present
        assert executed.slots[1].answer is not None
        assert tuple(request.plan.logical for request in queries.seen) == (comparator.logical,)

    def test_presentation_runs_independent_queries_concurrently_without_reordering_slots(
        self, contracts: Contracts
    ) -> None:
        prepared = prepare_batch((_request(contracts),))
        current, comparator = prepared.plans
        queries = ConcurrentWindowedQueries()
        inspection = PresentationEvidenceInspectionResult(
            (
                _inspected_slot(
                    PresentationEvidenceRole.CURRENT,
                    current.logical,
                    contracts,
                ),
                _inspected_slot(
                    PresentationEvidenceRole.COMPARATOR,
                    comparator.logical,
                    contracts,
                ),
            )
        )

        executed = execute_presentation_query_batch(
            prepared,
            inspection,
            MeasurementExecutionTooling(queries=queries, query_batch_max_concurrency=2),
        )

        assert queries.max_active == 2
        assert queries.completed == [EARLIER, LATER]
        assert tuple(slot.logical for slot in executed.slots) == (
            current.logical,
            comparator.logical,
        )

    def test_presentation_validates_every_bound_logical_plan_before_querying(
        self, contracts: Contracts
    ) -> None:
        prepared = prepare_batch((_request(contracts),))
        current, comparator = prepared.plans
        queries = WindowedQueries()
        inspection = PresentationEvidenceInspectionResult(
            (
                _inspected_slot(
                    PresentationEvidenceRole.CURRENT,
                    current.logical,
                    contracts,
                ),
                _inspected_slot(
                    PresentationEvidenceRole.COMPARATOR,
                    current.logical,
                    contracts,
                ),
            )
        )

        with pytest.raises(InvariantError, match="changed a prepared plan"):
            execute_presentation_batch(
                prepared,
                inspection,
                MeasurementExecutionTooling(queries=queries),
            )

        assert comparator.logical != current.logical
        assert queries.seen == []

    def test_presentation_refuses_role_misalignment_before_querying(
        self, contracts: Contracts
    ) -> None:
        prepared = prepare_batch((_request(contracts),))
        current, comparator = prepared.plans
        queries = WindowedQueries()
        inspection = PresentationEvidenceInspectionResult(
            (
                _inspected_slot(
                    PresentationEvidenceRole.COMPARATOR,
                    current.logical,
                    contracts,
                ),
                _inspected_slot(
                    PresentationEvidenceRole.CURRENT,
                    comparator.logical,
                    contracts,
                ),
            )
        )

        with pytest.raises(InvariantError, match="prepared plan or role"):
            execute_presentation_batch(
                prepared,
                inspection,
                MeasurementExecutionTooling(queries=queries),
            )

        assert queries.seen == []

    @pytest.mark.parametrize("failure_kind", ("dependency", "invariant"))
    def test_presentation_propagates_query_failures(
        self,
        failure_kind: str,
        contracts: Contracts,
    ) -> None:
        prepared = prepare_batch(
            (
                _request(
                    contracts,
                    tool=MeasurementTool.METRIC_BY_GRAIN,
                    prior_window=None,
                ),
            )
        )
        expected = prepared.plans[0]
        failure = (
            _selection_failure()
            if failure_kind == "dependency"
            else InvariantError(
                ErrorCode("MEASUREMENT_QUERY", 1),
                "The query result violated its governed plan.",
            )
        )
        queries = WindowedQueries(failure=failure)
        inspection = PresentationEvidenceInspectionResult(
            (
                _inspected_slot(
                    PresentationEvidenceRole.CURRENT,
                    expected.logical,
                    contracts,
                ),
            )
        )

        with pytest.raises((DependencyError, InvariantError)) as raised:
            execute_presentation_batch(
                prepared,
                inspection,
                MeasurementExecutionTooling(queries=queries),
            )

        assert raised.value is failure

    def test_every_plan_is_bound_once_before_any_query(self, contracts: Contracts) -> None:
        queries = WindowedQueries()
        publications = StubPublications()
        requests = tuple(
            _request(
                contracts,
                tool=MeasurementTool.METRIC_BY_GRAIN,
                metric_name=metric,
                prior_window=None,
            )
            for metric in ("retail_revenue_shipped", "units_shipped")
        )

        answers = handle_batch(
            requests,
            MeasurementTooling(queries=queries, publications=publications),
        )

        assert len(answers) == 2
        assert len(publications.seen) == 1
        assert [plan.metric.name for plan in publications.seen[0].plans] == [
            "retail_revenue_shipped",
            "units_shipped",
        ]
        assert [request.plan.logical.metric.name for request in queries.seen] == [
            "retail_revenue_shipped",
            "units_shipped",
        ]

    def test_a_later_planning_failure_runs_no_selection_or_query(
        self, contracts: Contracts
    ) -> None:
        queries = WindowedQueries()
        publications = StubPublications()
        requests = (
            _request(
                contracts,
                tool=MeasurementTool.METRIC_BY_GRAIN,
                prior_window=None,
            ),
            _request(
                contracts,
                tool=MeasurementTool.METRIC_BY_GRAIN,
                metric_name="not_a_declared_metric",
                prior_window=None,
            ),
        )

        with pytest.raises(ValidationError):
            handle_batch(
                requests,
                MeasurementTooling(queries=queries, publications=publications),
            )

        assert publications.seen == []
        assert queries.seen == []

    def test_attribution_models_are_three_independent_exact_plans(
        self, contracts: Contracts
    ) -> None:
        queries = WindowedQueries()
        publications = StubPublications()
        models = ("first_touch_v1", "last_touch_v1", "linear_v1")
        requests = tuple(
            _request(
                contracts,
                tool=MeasurementTool.METRIC_BY_GRAIN,
                metric_name="roas_deduplicated_incl_halo_14d",
                grains=("channel", "marketplace"),
                prior_window=None,
                filters={
                    "marketplace_key": US,
                    "attribution_model_key": model,
                    "currency_code": "USD",
                },
            )
            for model in models
        )

        handle_batch(
            requests,
            MeasurementTooling(queries=queries, publications=publications),
        )

        assert len(publications.seen) == 1
        plans = publications.seen[0].plans
        assert len(plans) == 3
        assert tuple(dict(plan.filters)["attribution_model_key"] for plan in plans) == models
        assert plans[0] != plans[1]
        assert plans[0] != plans[2]
        assert plans[1] != plans[2]


def _bound_request(contracts: Contracts, *, comparator: bool = False) -> QueryRequest:
    """Return one complete post-bind request: the current (LATER) or comparator (EARLIER) plan."""
    prepared = prepare_batch((_request(contracts),)).plans[1 if comparator else 0]
    role = PresentationEvidenceRole.COMPARATOR if comparator else PresentationEvidenceRole.CURRENT
    bound = _inspected_slot(role, prepared.logical, contracts).bound
    assert bound is not None
    return QueryRequest(plan=bound, max_rows=prepared.max_rows)


@dataclass
class _CountingQueries:
    """Counts executions per window and fails the windows it is told to."""

    failing: frozenset[DateRange] = frozenset()
    lock: Lock = field(default_factory=Lock)
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        with self.lock:
            self.seen.append(request)
        window = request.plan.logical.window
        if window in self.failing:
            raise DependencyError(ErrorCode("FICTIONAL", window.start.day), "query failed")
        return QueryResult(
            rows=(),
            bytes_scanned=window.start.day,
            truncated=False,
            suppression=Suppression(rows_suppressed=False),
        )


class TestCompleteBoundFanIn:
    """Plan 4.4 (DB20): exactly equal complete requests share one execution, nothing else."""

    def test_equal_complete_requests_share_one_execution_copied_into_every_slot(
        self, contracts: Contracts
    ) -> None:
        later = _bound_request(contracts)
        earlier = _bound_request(contracts, comparator=True)
        queries = _CountingQueries()

        results = measurement_module._execute_queries(  # pyright: ignore[reportPrivateUsage]
            (later, None, earlier, later),
            MeasurementExecutionTooling(queries=queries, query_batch_max_concurrency=2),
        )

        assert len(queries.seen) == 2
        assert results[1] is None
        assert results[0] is results[3]
        assert results[0] is not None and results[2] is not None
        assert (results[0].bytes_scanned, results[2].bytes_scanned) == (LATER.start.day, 3)
        assert distinct_query_count((later, None, earlier, later)) == 2

    def test_digest_is_stable_when_unordered_fields_have_an_alternate_order(
        self, contracts: Contracts
    ) -> None:
        """Canonical sorting preserves equality; the former repr digest is the failing control."""
        base = _bound_request(contracts)
        first_logical = replace(base.plan.logical, filters={"z_key": "z", "a_key": "a"})
        second_logical = replace(base.plan.logical, filters={"a_key": "a", "z_key": "z"})
        filters = (("a_key", "a"), ("z_key", "z"))
        first = replace(
            base, plan=replace(base.plan, logical=first_logical, primary_filters=filters)
        )
        second = replace(
            base, plan=replace(base.plan, logical=second_logical, primary_filters=filters)
        )

        assert first == second
        assert repr(first) != repr(second)  # paired control: repr is insertion-order dependent
        assert query_request_digest(first) == query_request_digest(second)

    @pytest.mark.parametrize("field_name", ["max_rows", "window", "filters", "publications"])
    def test_a_near_collision_in_any_complete_field_is_never_shared(
        self, contracts: Contracts, field_name: str
    ) -> None:
        """Paired: the equal request shares (1 execution), the near-collision does not (2)."""
        base = _bound_request(contracts)
        plan = base.plan
        if field_name == "max_rows":
            near = replace(base, max_rows=base.max_rows - 1)
        elif field_name == "window":
            near = _bound_request(contracts, comparator=True)
        elif field_name == "filters":
            (column, value), *rest = plan.primary_filters
            near = replace(
                base,
                plan=replace(plan, primary_filters=((column, f"{value}-other"), *rest)),
            )
        else:
            other = replace(plan.primary[0], publication_id="publication-fictional-other")
            near = replace(base, plan=replace(plan, primary=(other, *plan.primary[1:])))
        assert near != base

        assert distinct_query_count((base, base)) == 1
        assert distinct_query_count((base, near)) == 2

    def test_an_equal_digest_with_an_unequal_value_is_fatal_before_any_query(
        self, contracts: Contracts, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        later = _bound_request(contracts)
        earlier = _bound_request(contracts, comparator=True)

        def same_digest(request: QueryRequest) -> str:
            del request
            return "same"

        monkeypatch.setattr(measurement_module, "query_request_digest", same_digest)
        queries = _CountingQueries()

        # Control: an exactly equal pair under the same constant digest still shares one slot.
        assert distinct_query_count((later, later)) == 1
        with pytest.raises(InvariantError) as refused:
            measurement_module._execute_queries(  # pyright: ignore[reportPrivateUsage]
                (later, earlier), MeasurementExecutionTooling(queries=queries)
            )
        assert refused.value.code == QUERY_FAN_IN_COLLISION
        assert queries.seen == []

    def test_every_distinct_execution_is_observed_and_the_first_slot_error_is_raised(
        self, contracts: Contracts
    ) -> None:
        later = _bound_request(contracts)
        earlier = _bound_request(contracts, comparator=True)
        queries = _CountingQueries(failing=frozenset({LATER, EARLIER}))

        with pytest.raises(DependencyError) as failed:
            measurement_module._execute_queries(  # pyright: ignore[reportPrivateUsage]
                (earlier, later, earlier),
                MeasurementExecutionTooling(queries=queries, query_batch_max_concurrency=2),
            )

        assert failed.value.code == ErrorCode("FICTIONAL", EARLIER.start.day)
        assert sorted(request.plan.logical.window.start for request in queries.seen) == [
            EARLIER.start,
            LATER.start,
        ]
