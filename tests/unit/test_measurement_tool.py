# SPDX-License-Identifier: MIT-0
"""The measurement tool, and everything the answer has to say about its own figures.

Built against the **real** metric dictionary and the **real** contract, because the decisions
under test are about what those declare: `retail_revenue_shipped`'s notes carry the mandatory
net-of-returns caveat, and `vendor-sales-by-asin` declares the 45-day restatement window and the
PST business day. A hand-built metric would let those pass while the shipped declarations said
something else.

The query port is a stub rather than a mock, and it is a stub of the *port* — the SQL and the
engine are covered where they belong, in `test_athena_composition.py` and the integration tier.
What is being tested here is what the tool does with rows once it has them.

Four things carry most of the weight:

- **Every default the tool pins is stated in the narrative.** A population the caller did not
  choose and cannot see is the failure ADR-024 exists to prevent, one layer up.
- **`marketplace_key` is not defaulted**, so a question without one is refused rather than
  answered about a country nobody named.
- **An empty result carries no units and says so is not a zero.** A denominated zero would claim
  a product sold nothing.
- **A figure is never asserted without its units and its grain**, per the testing rules — a
  correct number at the wrong grain is the failure mode this product must not have.
"""

from __future__ import annotations

import dataclasses
import pathlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from publication_support import StubPublications
from test_publication_selection import RecordingCurrent
from test_tool_caller_gate import measurement as measurement_entrypoint

from adapters.inbound.tool_handlers.measurement import (
    DEFAULT_MAX_ROWS,
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    handle,
)
from adapters.inbound.tool_handlers.schemas import FORBIDDEN_IDENTITY_FIELDS
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.source_pack import load_source_packs
from domain.catalog.capability import GRAIN_NOT_SUPPORTED_CODE
from domain.catalog.freshness import Coverage
from domain.data_contracts.contract import DataContract, FieldMapping
from domain.measurement.answers import LENS_DEFAULTS, defaults_for
from domain.measurement.pack_coverage import CONTRACT_COVERAGE_REFUSED, PackDayVersions
from domain.measurement.planning import FactShape, FactShapes, measure_alias
from domain.narrative.publication import QualifierKind, RenderingKind
from domain.narrative.shaped_response import (
    FreshnessStatus,
    NonAdditivity,
    ResponseData,
    ShapedResponse,
    UnitKind,
)
from domain.semantics.identity import EcosystemKey
from domain.semantics.media_taxonomy import MediaTaxonomyTerm
from domain.shared_kernel.errors import DependencyError, DomainError, ErrorCode, ValidationError
from domain.shared_kernel.provenance import MeasurementBasis, Suppression
from domain.shared_kernel.values import DateRange
from ports.outbound.pack_coverage import PackCoverageReadRequest
from ports.outbound.query import QueryRequest, QueryResult

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-006", "ADR-024", "ADR-027", "ADR-010")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
WEEK = DateRange(date(2026, 8, 10), date(2026, 8, 16))
US = "ATVPDKIKX0DER"

Row = dict[str, str | int | Decimal | None]
type Contracts = tuple[DataContract, ...]
"""Every contract in the repository. A fact is described by all of those writing it."""


def _rows(*pairs: tuple[str, str]) -> tuple[Row, ...]:
    """Build query rows in the shape the Athena adapter returns them.

    The aggregate is keyed `value_0`, the alias the composed statement gives it. `value` is the
    *output* key a combined figure is emitted under, and the two are deliberately different so
    a plan gaining a second aggregate does not change the response shape.
    """
    return tuple(
        {"amazon_asin": asin, "currency_code": "USD", measure_alias(0): Decimal(amount)}
        for asin, amount in pairs
    )


@dataclass
class StubQueries:
    """A stub of `QueryPort`, recording what it was asked.

    A stub rather than a mock: the assertions are about the request the tool built and the answer
    it produced, and a mock would let a test pass while asserting only that a method was called.
    """

    rows: tuple[Row, ...] = ()
    truncated: bool = False
    suppression: Suppression = field(default_factory=lambda: Suppression(rows_suppressed=False))
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        return QueryResult(
            rows=self.rows,
            bytes_scanned=1_024,
            truncated=self.truncated,
            suppression=self.suppression,
        )


@pytest.fixture(scope="module")
def contracts() -> tuple[DataContract, ...]:
    """Every contract, because a fact is described by all of those writing it.

    `fct_ad_exposure` has four, each supplying different measures, so a shape from one is a
    partial view of the table and cannot plan a ratio over columns two of them contribute.
    """
    return tuple(c for _, c in load_contracts(ROOT))


def _request(contracts: Contracts, **overrides: object) -> MeasurementToolRequest:
    fields: dict[str, object] = {
        "tool": MeasurementTool.METRIC_BY_GRAIN,
        "catalog": load_catalog(ROOT),
        "contracts": contracts,
        "metric_name": "retail_revenue_shipped",
        "grains": ("product",),
        "window": WEEK,
        "computed_at": NOW,
        "correlation_id": "corr-1",
        "filters": {"marketplace_key": US},
    }
    fields.update(overrides)
    return MeasurementToolRequest(**fields)  # type: ignore[arg-type]


def _qualifier_kinds(response: ShapedResponse) -> set[QualifierKind]:
    return {item.kind for item in response.metadata.qualifiers}


def _answer(contracts: Contracts, **overrides: object) -> ShapedResponse:
    queries = StubQueries(rows=overrides.pop("rows", _rows(("B0AAA", "40.0000"))))  # type: ignore[arg-type]
    queries.truncated = bool(overrides.pop("truncated", False))
    suppression = overrides.pop("suppression", None)
    if suppression is not None:
        queries.suppression = suppression  # type: ignore[assignment]
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


class TestTheTargetQuestion:
    """Shipped revenue by ASIN for last week — build-order Phase 2's target."""

    def test_it_answers_with_figures_units_and_grain_together(self, contracts: Contracts) -> None:
        """Never a bare number.

        The testing rules require units and grain beside any figure, because a correct figure at
        the wrong grain is the failure mode this product must not have.
        """
        response = _answer(contracts, rows=_rows(("B0AAA", "40.0000"), ("B0BBB", "35.0000")))

        assert response.data.grain.dimensions == ("amazon_asin",)
        units = response.data.units
        assert units is not None
        assert units.kind is UnitKind.CURRENCY
        assert units.currency_code == "USD"
        assert [row["value"] for row in response.data.values] == [
            Decimal("40.0000"),
            Decimal("35.0000"),
        ]

    def test_the_scale_is_the_stored_four_places(self, contracts: Contracts) -> None:
        # A wrong divisor is a silent order-of-magnitude error, so the scale is asserted rather
        # than assumed from the value looking about right.
        response = _answer(contracts, rows=_rows(("B0AAA", "40.0000")))

        assert str(response.data.values[0]["value"]) == "40.0000"

    def test_the_row_count_matches_the_values(self, contracts: Contracts) -> None:
        response = _answer(contracts, rows=_rows(("B0AAA", "1.0000"), ("B0BBB", "2.0000")))

        assert response.metadata.row_count == len(response.data.values) == 2

    def test_it_names_the_fact_and_the_basis_it_read(self, contracts: Contracts) -> None:
        response = _answer(contracts)

        assert response.metadata.facts_read == ("fct_retail_sales",)
        assert response.metadata.measurement_basis == (MeasurementBasis.RETAIL_ACTUAL,)
        assert response.metadata.mixes_basis is False


class TestTheDefaultsAreStated:
    """A default the caller cannot see is a population they did not choose.

    ADR-024 makes the double-count structurally impossible at the plan level. This is the same
    argument one layer up: the tool has to pin two option columns for the question to be
    answerable at all, and an answer that did not say so would be about a population nobody
    selected.
    """

    def test_the_lens_columns_are_pinned_for_the_caller(self, contracts: Contracts) -> None:
        response = _answer(contracts)
        request = response  # readability: the assertion is on the narrative

        assert any("distributor_view = MANUFACTURING" in s for s in request.narrative)
        assert any("selling_program = RETAIL" in s for s in request.narrative)
        assert any("report_period = DAY" in s for s in request.narrative)

    def test_the_narrative_says_why_a_total_across_them_would_be_wrong(
        self, contracts: Contracts
    ) -> None:
        # Stating the pin without the consequence tells a reader a fact they cannot act on.
        response = _answer(contracts)

        assert any("double-count" in sentence for sentence in response.narrative)

    def test_a_caller_supplied_value_is_not_overridden(self, contracts: Contracts) -> None:
        """A default that overrode an explicit choice would answer a different question.

        Silently, because the figures would still be internally consistent.
        """
        queries = StubQueries(rows=_rows(("B0AAA", "9.0000")))
        handle(
            _request(
                contracts,
                filters={"marketplace_key": US, "distributor_view": "SOURCING"},
            ),
            MeasurementTooling(queries=queries, publications=StubPublications()),
        )

        assert queries.seen[0].plan.logical.filters["distributor_view"] == "SOURCING"

    def test_direct_callers_remain_empty_and_computed_defaults_are_disclosed(
        self, contracts: Contracts
    ) -> None:
        request = _request(contracts)

        response = handle(
            request,
            MeasurementTooling(
                queries=StubQueries(rows=_rows(("B0AAA", "9.0000"))),
                publications=StubPublications(),
            ),
        )

        assert request.defaulted_filters == {}
        assert request.resolution_provenance == ()
        assert response.metadata.resolution_provenance == ()
        assert any("distributor_view = MANUFACTURING" in line for line in response.narrative)
        assert any("selling_program = RETAIL" in line for line in response.narrative)
        assert any("report_period = DAY" in line for line in response.narrative)

    def test_resolver_supplied_defaults_stay_separate_and_are_disclosed(
        self, contracts: Contracts
    ) -> None:
        request = _request(
            contracts,
            defaulted_filters={"distributor_view": "MANUFACTURING"},
        )

        response = handle(
            request,
            MeasurementTooling(
                queries=StubQueries(rows=_rows(("B0AAA", "9.0000"))),
                publications=StubPublications(),
            ),
        )

        assert "distributor_view" not in request.filters
        assert request.defaulted_filters == {"distributor_view": "MANUFACTURING"}
        assert any("distributor_view = MANUFACTURING" in line for line in response.narrative)

    def test_only_a_lens_column_is_pinned_at_all(self) -> None:
        """The map is the boundary, and it holds only reporting views.

        A column selecting a different population must never acquire a default, so the check is
        on the declared map rather than on one call's behaviour.
        """
        assert set(LENS_DEFAULTS) == {"distributor_view", "selling_program", "report_period"}
        assert "marketplace_key" not in LENS_DEFAULTS
        assert "channel_key" not in LENS_DEFAULTS

    def test_a_column_with_no_declared_default_is_left_unaccounted(self) -> None:
        # So ADR-024 still refuses the plan and names it, rather than a silent default appearing
        # the day somebody finds the refusal inconvenient.
        assert defaults_for(("marketplace_key", "distributor_view", "report_period")) == {
            "distributor_view": "MANUFACTURING",
            "report_period": "DAY",
        }


class TestTheMarketplaceIsNotDefaulted:
    def test_a_question_without_one_is_refused(self, contracts: Contracts) -> None:
        """The refusal ADR-024 produces, reached through the tool.

        A marketplace selects a different population *and* a different currency, so defaulting it
        would answer about a country a European brand never asked about, denominated wrongly.
        """
        with pytest.raises(DomainError) as raised:
            handle(
                _request(contracts, filters={}),
                MeasurementTooling(queries=StubQueries(), publications=StubPublications()),
            )

        assert "marketplace_key" in raised.value.user_visible_description

    def test_the_refusal_spends_no_query(self, contracts: Contracts) -> None:
        # A refusal that had already queried would have cost money to say no.
        queries = StubQueries()
        with pytest.raises(DomainError):
            handle(
                _request(contracts, filters={}),
                MeasurementTooling(queries=queries, publications=StubPublications()),
            )

        assert queries.seen == []

    def test_it_does_not_alarm(self, contracts: Contracts) -> None:
        # An under-specified question is a request outside its contract, not a platform fault.
        with pytest.raises(DomainError) as raised:
            handle(
                _request(contracts, filters={}),
                MeasurementTooling(queries=StubQueries(), publications=StubPublications()),
            )

        assert raised.value.alarms is False


class TestAnEmptyResult:
    """Zero rows is an answer, and it is not a zero."""

    def test_it_returns_a_response_rather_than_raising(self, contracts: Contracts) -> None:
        response = _answer(contracts, rows=())

        assert response.metadata.row_count == 0
        assert response.data.values == ()

    def test_it_carries_no_units(self, contracts: Contracts) -> None:
        """The case the envelope was changed to express.

        A monetary figure's currency comes from the rows, so an empty monetary result has none.
        Denominating it would state that the absent rows would have been in that currency.
        """
        response = _answer(contracts, rows=())

        assert response.data.units is None

    def test_it_still_states_its_grain(self, contracts: Contracts) -> None:
        # "Nothing sold, by ASIN" and "nothing sold, in total" are different answers.
        response = _answer(contracts, rows=())

        assert response.data.grain.dimensions == ("amazon_asin",)

    def test_the_narrative_says_it_is_an_absence_not_a_zero(self, contracts: Contracts) -> None:
        response = _answer(contracts, rows=())

        assert any("rather than a figure of zero" in s for s in response.narrative)


class TestTheCaveatsThatAreNotOptional:
    def test_the_net_of_returns_note_is_carried_from_the_dictionary(
        self, contracts: Contracts
    ) -> None:
        """answerable-questions §5 makes this mandatory, and the dictionary declares it.

        Carried rather than restated, so the sentence cannot drift from the declaration.
        """
        response = _answer(contracts)

        assert any("Net of returns" in sentence for sentence in response.narrative)

    def test_the_restatement_window_is_stated_with_its_duration(self, contracts: Contracts) -> None:
        # Derived from the contract's declared 45 days, so it says how long rather than "recently".
        response = _answer(contracts)

        assert any("45" in sentence for sentence in response.narrative)

    def test_the_source_business_day_is_named(self, contracts: Contracts) -> None:
        """The dates in an answer are the *source's* days, not UTC days.

        A pre-aggregated day cannot be re-bucketed (ADR-004), so presenting vendor dates beside a
        UTC coverage bound without saying so implies a boundary the data never used.
        """
        response = _answer(contracts)

        assert any("PST" in sentence for sentence in response.narrative)

    def test_suppression_is_stated_when_rows_were_withheld(self, contracts: Contracts) -> None:
        response = _answer(
            contracts,
            suppression=Suppression(rows_suppressed=True, reason="below the privacy threshold"),
        )

        assert response.metadata.suppression.rows_suppressed is True
        assert QualifierKind.SUPPRESSED in _qualifier_kinds(response)
        assert any("will not reconcile" in sentence for sentence in response.narrative)

    def test_qualified_publication_dq_is_typed_and_mandatory(self, contracts: Contracts) -> None:
        caveat = "Sparse optional columns passed under the declared qualification."
        response = _answer(contracts, qualified_dq=(caveat,))

        assert response.metadata.qualified_dq == (caveat,)
        assert QualifierKind.QUALIFIED_DQ in _qualifier_kinds(response)
        grounding = response.metadata.grounding
        assert grounding is not None
        dq_ids = {
            item.rendering_id
            for item in grounding.renderings
            if item.qualifier is QualifierKind.QUALIFIED_DQ
        }
        assert dq_ids <= set(grounding.mandatory_rendering_ids)
        assert dq_ids

    def test_a_truncated_answer_says_so(self, contracts: Contracts) -> None:
        """Both disclosures, asserted separately, because two sentences now contain the word.

        The envelope refuses a truncated response whose narrative is silent, and it looks for the
        substring `truncat`. Since the total sentence acquired its own reason for a page, that
        substring is present whether or not the sentence about the *rows* is — so the invariant and
        a substring assertion would both pass with the page disclosure deleted. Found by mutation:
        inverting `if qualifiers.truncated` in `_closing_lines` survived.
        """
        response = _answer(contracts, truncated=True)

        assert response.metadata.truncated is True
        assert QualifierKind.TRUNCATED in _qualifier_kinds(response)
        assert "This result was truncated, so it is a page rather than the whole answer." in (
            response.narrative
        ), "the rows being partial is a separate fact from the total being unstatable"
        assert any("truncat" in sentence.lower() for sentence in response.narrative)

    def test_an_untruncated_answer_never_claims_it_was(self, contracts: Contracts) -> None:
        # A caveat that is not true is as misleading as one that is missing, and it reads as
        # authoritative because the platform said it.
        response = _answer(contracts)

        assert response.metadata.truncated is False
        assert not any("truncat" in sentence.lower() for sentence in response.narrative)


def _stated_total(response: ShapedResponse) -> str | None:
    """Give the sentence that states a total across the rows, or the one refusing to."""
    return next((s for s in response.narrative if s.startswith(("Across all", "No total"))), None)


@pytest.mark.covers("G-45")
class TestABreakdownSuppliesItsOwnTotal:
    """G-45: a breakdown carried five rows and no total, and the model added them up itself.

    The sums were arithmetically correct, which is why nothing looked wrong — and the same
    behaviour over a page, a withholding or a non-additive metric produces a figure that is
    silently short. So every one of those cases has to say that no total is stated *and why*,
    because an unexplained absence is indistinguishable from one that needs no explanation.
    """

    def test_the_total_is_carried_structurally_and_stated_in_words(
        self, contracts: Contracts
    ) -> None:
        """Both, not either. A figure the model can only read in prose can be paraphrased away.

        Asserted against the same object rather than against two literals, because two literals
        agreeing today is what lets them disagree later.
        """
        response = _answer(
            contracts,
            rows=_rows(("B0AAA", "40.0000"), ("B0BBB", "35.0000"), ("B0CCC", "25.0000")),
        )

        total = response.data.population_total
        assert total == Decimal("100.0000")
        units = response.data.units
        assert units is not None
        assert units.kind is UnitKind.CURRENCY
        assert units.currency_code == "USD"
        assert response.data.grain.dimensions == ("amazon_asin",)
        stated = _stated_total(response)
        assert stated is not None
        assert str(total) in stated, "the sentence must state the figure the envelope carries"
        assert "100.0000 USD" in stated, "a monetary total is never separable from its currency"

    def test_the_total_keeps_the_stored_four_places(self, contracts: Contracts) -> None:
        # A wrong divisor is a silent order-of-magnitude error, and a rounded total is how a
        # reconciliation against the customer's own report starts failing by cents.
        response = _answer(contracts, rows=_rows(("B0AAA", "0.1000"), ("B0BBB", "0.2000")))

        assert str(response.data.population_total) == "0.3000"

    def test_a_count_total_carries_no_currency(self, contracts: Contracts) -> None:
        response = _answer(
            contracts,
            metric_name="impressions",
            grains=_DSP_GRAINS,
            rows=_dsp_rows(("c1", "500"), ("c2", "300")),
        )

        units = response.data.units
        assert units is not None
        assert units.kind is UnitKind.COUNT
        assert response.data.population_total == 800
        stated = _stated_total(response)
        assert stated is not None
        assert "the total is 800." in stated
        assert "USD" not in stated, "a count has no currency, and inventing one is a claim"

    def test_the_total_is_absent_and_explained_when_the_result_is_a_page(
        self, contracts: Contracts
    ) -> None:
        """The case the measured defect would have made worse rather than better.

        A total over a page is short by an unknown amount, so supplying one here would have put
        the platform's name on exactly the figure G-45 is about.
        """
        response = _answer(contracts, truncated=True)

        assert response.data.population_total is None
        assert _stated_total(response) == (
            "No total is stated across these rows: this result is truncated, so any total over "
            "it would be short by an unknown amount."
        )

    def test_the_total_is_absent_and_explained_when_amazon_withheld_rows(
        self, contracts: Contracts
    ) -> None:
        """A figure excludes the withheld part by construction, and adding it back would not fix it.

        Every measure folds attributable and withheld separately, and what was withheld is a floor
        rather than the gap — AMC deletes sub-threshold rows rather than flagging them, so a total
        plus a floor is still not a total.
        """
        response = _answer(
            contracts,
            suppression=Suppression(rows_suppressed=True, reason="below the privacy threshold"),
        )

        assert response.data.population_total is None
        stated = _stated_total(response)
        assert stated is not None
        assert "Amazon withheld part of this population" in stated

    def test_the_total_is_absent_and_explained_for_a_distinct_count(
        self, contracts: Contracts
    ) -> None:
        """`reach_users` is a COUNT, so the units gate lets it through and additivity stops it."""
        response = _answer(
            contracts,
            metric_name="reach_users",
            grains=_DSP_GRAINS,
            rows=_dsp_rows(("c1", "500"), ("c2", "300")),
        )

        assert response.data.population_total is None
        stated = _stated_total(response)
        assert stated is not None
        assert "counts distinct people" in stated

    def test_only_a_non_additive_result_carries_the_additivity_marker(
        self, contracts: Contracts
    ) -> None:
        """The marker is the dictionary's rule against a sum, so an additive figure has none."""
        rows = _dsp_rows(("c1", "500"), ("c2", "300"))
        additive = _answer(contracts, metric_name="impressions", grains=_DSP_GRAINS, rows=rows)
        distinct = _answer(contracts, metric_name="reach_users", grains=_DSP_GRAINS, rows=rows)

        assert additive.data.additivity is None
        assert additive.data.population_total == 800
        # Only the column the two rows differ on: date, channel and ad product are shared.
        assert distinct.data.additivity == NonAdditivity(
            across=("campaign_key",),
            instruction=(
                "These figures are not additive across campaign. Don't add them together, and "
                "don't state a total or a share of a total built from them. A total is stated "
                "only when a governed tool returns one."
            ),
        )
        # Paired control: one distinct-count row is its own figure, so there is nothing to add.
        single = _answer(
            contracts, metric_name="reach_users", grains=_DSP_GRAINS, rows=_dsp_rows(("c1", "500"))
        )
        assert single.data.additivity is None

    def test_compared_ratio_rows_carry_the_marker_and_one_key_carries_none(
        self, contracts: Contracts
    ) -> None:
        """A ratio has no aggregation policy, so `additive` alone marks its compared rows.

        Review r2 #1 and #3: the segment control can't tell, since its exact-only axis gives
        the same marker either way. The one-product comparison has nothing to add.
        """
        compared = {
            "tool": MeasurementTool.COMPARE_WINDOWS,
            "metric_name": "retail_asp_purchased",
            "grains": ("product",),
            "prior_window": DateRange(date(2026, 8, 3), date(2026, 8, 9)),
        }
        two = _answer(
            contracts,
            **compared,
            rows=_ratio_rows(("B0AAA", "100.0000", "10.0000"), ("B0BBB", "40.0000", "4.0000")),
        )
        marker = NonAdditivity(
            across=("amazon_asin",),
            instruction=(
                "These figures are not additive across product. Don't add them together, and "
                "don't state a total or a share of a total built from them. A total is stated "
                "only when a governed tool returns one."
            ),
        )
        assert two.data.additivity == marker
        grounding = two.metadata.grounding
        assert grounding is not None
        assert [
            (item.kind, item.text, item.source_paths)
            for item in grounding.renderings
            if item.rendering_id == f"{grounding.grounding_id}:additivity"
        ] == [(RenderingKind.CONTEXT, marker.instruction, ("data.additivity", "data.grain"))]
        one = _answer(contracts, **compared, rows=_ratio_rows(("B0AAA", "100.0000", "10.0000")))
        assert one.data.additivity is None

    def test_the_agent_prompt_carries_the_non_additive_and_total_rules(self) -> None:
        """Pin the prompt rule the marker acts through, and the field it names.

        Owner rule: the agent is governed by prompts and evaluation, never a filter on model text.
        """
        prompt = (ROOT / "contracts/prompts/v1/managed-conversation.md").read_text()
        rule = (
            "Figures a tool result marks non-additive, in data.additivity, are never added "
            "together or turned into shares of their sum. State a total only when a governed "
            "tool returned it."
        )
        assert prompt.count(rule) == 1
        assert "additivity" in {field.name for field in dataclasses.fields(ResponseData)}

    def test_an_empty_answer_states_no_total_at_all(self, contracts: Contracts) -> None:
        """Not even a refusal to state one.

        There are no rows for a total to be absent from, and the headline already says the window
        held nothing — a sentence declining a total nobody could have expected would read as
        though one were owed.
        """
        response = _answer(contracts, rows=())

        assert response.data.population_total is None
        assert _stated_total(response) is None

    @pytest.mark.covers("G-46")
    def test_a_ratio_is_called_a_ratio_rather_than_a_distinct_count(
        self, contracts: Contracts
    ) -> None:
        """G-46, reached from the breakdown path.

        `retail_asp_purchased` is a quotient declared in **currency**, so it passes the unit-kind
        gate. Before the metric's own kind was tested first, it was summed — and where it was not,
        it was explained as counting distinct people, which an average selling price does not do.
        """
        response = _answer(
            contracts,
            metric_name="retail_asp_purchased",
            grains=("product",),
            rows=_ratio_rows(("B0AAA", "100.0000", "10.0000"), ("B0BBB", "40.0000", "4.0000")),
        )

        assert response.data.population_total is None
        stated = _stated_total(response)
        assert stated is not None
        assert "is a ratio, and a sum of ratios is not one" in stated
        assert "distinct people" not in stated
        assert response.metadata.semantic_qualifiers
        assert all(
            qualifier.left != qualifier.right for qualifier in response.metadata.semantic_qualifiers
        )


_DSP_GRAINS = ("date", "campaign", "channel", "ad_product")
"""Every grain column `fct_ad_exposure` makes a row distinct by.

Named because a shorter grain is refused rather than answered: summing across an
extraction-option column is a claim that it is addition, and `reach_users` additionally cannot be
aggregated across dates at all.
"""


def _dsp_rows(*pairs: tuple[str, str]) -> tuple[Row, ...]:
    """Build single-aggregate rows at the full DSP grain."""
    return tuple(
        {
            "date_key": "2026-08-10",
            "campaign_key": campaign,
            "channel_key": "amazon_dsp",
            "ad_product_key": "dsp",
            measure_alias(0): Decimal(value),
        }
        for campaign, value in pairs
    )


def _ratio_rows(*triples: tuple[str, str, str]) -> tuple[Row, ...]:
    """Build two-aggregate rows for a same-fact ratio, carrying the currency its result needs."""
    return tuple(
        {
            "amazon_asin": asin,
            "currency_code": "USD",
            measure_alias(0): Decimal(numerator),
            measure_alias(1): Decimal(denominator),
        }
        for asin, numerator, denominator in triples
    )


class TestCoverageReachesTheAnswer:
    def test_current_publications_report_fresh(self, contracts: Contracts) -> None:
        response = _answer(contracts)

        assert response.metadata.freshness_status is FreshnessStatus.FRESH

    def test_missing_current_publication_fails_before_querying(self, contracts: Contracts) -> None:
        queries = StubQueries(rows=())
        publications = StubPublications(failure=_selection_failure())

        with pytest.raises(DependencyError, match="CURRENT coverage is incomplete"):
            handle(
                _request(contracts),
                MeasurementTooling(queries=queries, publications=publications),
            )

        assert queries.seen == []
        assert len(publications.seen) == 1

    @pytest.mark.covers("ADR-036")
    def test_unbound_rows_cannot_be_returned_when_publication_selection_fails(
        self, contracts: Contracts
    ) -> None:
        queries = StubQueries(rows=_rows(("B0AAA", "40.0000")))

        with pytest.raises(DependencyError):
            handle(
                _request(contracts),
                MeasurementTooling(
                    queries=queries,
                    publications=StubPublications(failure=_selection_failure()),
                ),
            )

        assert queries.seen == []

    def test_stale_selected_publications_say_how_old_they_are(self, contracts: Contracts) -> None:
        coverage = Coverage(
            fact_name="fct_retail_sales",
            window=WEEK,
            freshness=FreshnessStatus.STALE,
            measurement_basis=MeasurementBasis.RETAIL_ACTUAL,
            valid_grains=("product",),
            loaded_at=NOW - timedelta(days=14),
            intervals=(WEEK,),
        )
        response = _answer(contracts, coverage=coverage)

        assert response.metadata.freshness_status is FreshnessStatus.STALE
        assert QualifierKind.STALE in _qualifier_kinds(response)
        assert any("staleness threshold" in sentence for sentence in response.narrative)

    def test_incomplete_current_window_fails_before_querying(self, contracts: Contracts) -> None:
        queries = StubQueries()

        with pytest.raises(DependencyError, match="CURRENT coverage is incomplete"):
            handle(
                _request(contracts),
                MeasurementTooling(
                    queries=queries,
                    publications=StubPublications(failure=_selection_failure()),
                ),
            )

        assert queries.seen == []

    def test_a_fully_selected_window_says_nothing_about_coverage(
        self, contracts: Contracts
    ) -> None:
        response = _answer(contracts)

        assert not any("does not span" in sentence for sentence in response.narrative)

    def test_a_failed_candidate_cannot_supply_last_known_good_figures(
        self, contracts: Contracts
    ) -> None:
        queries = StubQueries()

        with pytest.raises(DependencyError):
            handle(
                _request(contracts),
                MeasurementTooling(
                    queries=queries,
                    publications=StubPublications(failure=_selection_failure()),
                ),
            )

        assert queries.seen == []


class TestTheCoverageWindow:
    def test_it_is_the_window_asked_about(self, contracts: Contracts) -> None:
        """Not the loaded window, which would answer a different question.

        Whether the data reaches across it is carried by the freshness status and a narrative
        sentence, so the bounds can mean one thing rather than two.
        """
        response = _answer(contracts)

        assert response.metadata.coverage_start.date() == WEEK.start
        assert response.metadata.coverage_end.date() == WEEK.end

    def test_the_bounds_are_timezone_aware(self, contracts: Contracts) -> None:
        response = _answer(contracts)

        assert response.metadata.coverage_start.tzinfo is not None
        assert response.metadata.coverage_end.tzinfo is not None

    def test_the_end_covers_the_whole_last_day(self, contracts: Contracts) -> None:
        # `DateRange` is inclusive at both ends, so a bound at midnight would describe a window
        # one day shorter than the one that was queried.
        response = _answer(contracts)

        assert response.metadata.coverage_end.hour == 23


class TestTheRequestShape:
    def test_it_accepts_no_identity_argument(self) -> None:
        """Asserted on the field names, so adding one is a test failure rather than a review miss.

        If identity is an argument the model fills in, the model can impersonate — and an
        impersonated call looks like an ordinary tool call in every log.
        """
        fields = MeasurementToolRequest.__dataclass_fields__

        for forbidden in FORBIDDEN_IDENTITY_FIELDS:
            assert forbidden not in fields

    def test_the_page_size_is_capped_by_default(self) -> None:
        # Server-side, so the model does not decide how much it receives.
        assert DEFAULT_MAX_ROWS <= 500

    def test_the_max_rows_reaches_the_query(self, contracts: Contracts) -> None:
        queries = StubQueries(rows=_rows(("B0AAA", "1.0000")))
        handle(
            _request(contracts, max_rows=25),
            MeasurementTooling(queries=queries, publications=StubPublications()),
        )

        assert queries.seen[0].max_rows == 25

    def test_the_plan_asks_for_the_currency_column(self, contracts: Contracts) -> None:
        # Carried, not grouped: it labels the figure rather than slicing it, and without it the
        # tool would be labelling an amount by assuming a marketplace's currency.
        queries = StubQueries(rows=_rows(("B0AAA", "1.0000")))
        handle(
            _request(contracts),
            MeasurementTooling(queries=queries, publications=StubPublications()),
        )

        logical = queries.seen[0].plan.logical
        assert logical.carried_columns == ("currency_code",)
        assert "currency_code" not in logical.grain.dimensions


@pytest.mark.covers("ADR-032")
class TestTheToolResolvesItsOwnFactShapes:
    """Which facts a question reads is derived from the dictionary, never supplied.

    The tool takes every contract rather than one, for two reasons that arrived together. A fact
    is described by **every** contract writing it — `fct_ad_exposure` has four, each contributing
    different measures, so `ctr` cannot be planned from one. And a ratio across two facts needs
    both described, which no single contract can do.

    A caller choosing would be choosing something the metric's own declaration already states.
    """

    def test_a_single_fact_metric_resolves_to_one_shape(self, contracts: Contracts) -> None:
        shape = _request(contracts).shape
        assert isinstance(shape, FactShape)
        assert shape.name == "fct_retail_sales"

    def test_a_same_fact_ratio_still_resolves_to_one_shape(self, contracts: Contracts) -> None:
        """`ctr` needs the merged shape: no one contract declares both clicks and impressions."""
        shape = _request(contracts, metric_name="ctr", grains=("campaign",)).shape
        assert isinstance(shape, FactShape)
        assert {"clicks", "impressions"} <= set(shape.columns)

    def test_a_cross_fact_ratio_resolves_to_both_shapes(self, contracts: Contracts) -> None:
        shape = _request(contracts, metric_name="roas_attributed_14d", grains=("campaign",)).shape
        assert isinstance(shape, FactShapes)
        assert (shape.numerator.name, shape.denominator.name) == (
            "fct_attributed_conversion",
            "fct_ad_exposure",
        )

    def test_a_vendor_cross_fact_ratio_resolves_across_two_reports(
        self, contracts: Contracts
    ) -> None:
        """`conversion_rate_retail` reads sales and traffic, which Vendor Central splits."""
        shape = _request(contracts, metric_name="conversion_rate_retail").shape
        assert isinstance(shape, FactShapes)
        assert (shape.numerator.name, shape.denominator.name) == (
            "fct_retail_sales",
            "fct_product_page_views",
        )

    def test_an_undeclared_metric_is_refused_before_a_shape_is_built(
        self, contracts: Contracts
    ) -> None:
        with pytest.raises(ValidationError, match="not a metric this platform declares"):
            _ = _request(contracts, metric_name="revenue_shiped").shape


@pytest.mark.covers("G-37")
class TestTheAnswerNamesTheContractsBehindIt:
    """G-37: the envelope declares `contract_ids` and every answer used to report it empty.

    A declared provenance field that is always empty is worse than an absent one — a consumer
    reading it concludes no contract was involved. It is also what makes a figure traceable back to
    a delivery, which the fact name alone cannot do.

    Most of these read the derived property rather than a whole answer, matching the class above:
    the resolution is what has the logic, and a drill-across answer would need fabricated row
    shapes to assert the same thing through a query.
    """

    def test_the_envelope_carries_them(self, contracts: Contracts) -> None:
        """The wiring, asserted through a real answer rather than the property alone."""
        response = _answer(contracts)
        assert response.metadata.contract_ids == ("vendor-sales-by-asin",)

    def test_a_drill_across_names_both_sides(self, contracts: Contracts) -> None:
        """The case a naive fix under-reports.

        `roas_attributed_14d` divides `fct_attributed_conversion` by `fct_ad_exposure`, and the
        denominator alone has five writers. Taking the contract whose freshness was checked, or the
        numerator's only, would report one of six and look plausible.
        """
        request = _request(contracts, metric_name="roas_attributed_14d", grains=("campaign",))
        assert tuple(c.contract_id for c in request.contributing_contracts) != ()
        assert tuple(sorted(c.contract_id for c in request.contributing_contracts)) == (
            "amc-attributed-conversion",
            "amc-dsp-clicks",
            "amc-dsp-impressions",
            "amc-dsp-reach",
            "amc-dsp-views",
            "amc-reach-frequency",
        )

    def test_a_same_fact_ratio_names_every_writer_of_that_fact(self, contracts: Contracts) -> None:
        """`ctr` reads one fact, and no single contract declares both of its columns."""
        request = _request(contracts, metric_name="ctr", grains=("campaign",))
        assert tuple(sorted(c.contract_id for c in request.contributing_contracts)) == (
            "amc-dsp-clicks",
            "amc-dsp-impressions",
            "amc-dsp-reach",
            "amc-dsp-views",
            "amc-reach-frequency",
        )

    def test_a_dimension_contract_is_never_named(self, contracts: Contracts) -> None:
        """A product hierarchy is joined against, not measured.

        Listing it as provenance for a figure would misstate where the figure came from, and it
        would appear on every product-grain answer.
        """
        request = _request(contracts)
        named = {c.contract_id for c in request.contributing_contracts}
        assert "vendor-product-hierarchy" not in named

    def test_a_cross_report_ratio_names_one_writer_per_side(self, contracts: Contracts) -> None:
        request = _request(contracts, metric_name="conversion_rate_retail")
        assert tuple(sorted(c.contract_id for c in request.contributing_contracts)) == (
            "vendor-sales-by-asin",
            "vendor-traffic-by-asin",
        )

    def test_what_reaches_the_envelope_is_sorted_and_deduplicated(
        self, contracts: Contracts
    ) -> None:
        """`Metadata` refuses anything else, so this asserts the tool satisfies it.

        Duplicating a contract in the fixture is the realistic way this breaks — the same contract
        loaded twice from two paths — and the answer must still be constructible.
        """
        doubled = (*contracts, *contracts)
        response = _answer(doubled)
        ids = response.metadata.contract_ids
        assert ids == ("vendor-sales-by-asin",)
        assert list(ids) == sorted(set(ids))


@dataclass
class _DspReachCoverage:
    """A stub of `PackCoveragePort`: the days `amc-dsp-reach` has a CURRENT publication."""

    covered: frozenset[date]
    seen: list[PackCoverageReadRequest] = field(default_factory=list[PackCoverageReadRequest])

    def pack_coverage(self, request: PackCoverageReadRequest) -> tuple[PackDayVersions, ...]:
        self.seen.append(request)
        days = {
            window.start + timedelta(days=offset)
            for window in request.windows
            for offset in range(window.days)
        }
        # As the selector does: the first published day is read only when a day isn't covered.
        first = min(self.covered, default=None) if days - self.covered else None
        return tuple(
            PackDayVersions(
                item.contract_id, {day: 1 if day in self.covered else None for day in days}, first
            )
            for item in request.contracts
        )


_DSP_CAMPAIGN = {
    "marketplace_key": "amazon_us",
    "channel_key": "amazon_dsp",
    "ad_product_key": "amazon_dsp",
    "campaign_key": "campaign:northstar-dsp",
}


@pytest.mark.covers("ADR-109")
class TestDspReachInChat:
    """'DSP reach for campaign X on 2026-09-14': pack 4, chat only (owner S-4, plan D6)."""

    DAY = date(2026, 9, 14)

    def _ask(  # noqa: PLR0913, PLR0917 — the contracts, the window, its coverage and the scope
        self,
        contracts: Contracts,
        window: DateRange,
        covered: frozenset[date],
        filters: dict[str, str] | None = None,
    ) -> tuple[ShapedResponse | ValidationError, _DspReachCoverage, StubQueries]:
        coverage = _DspReachCoverage(covered)
        filters = _DSP_CAMPAIGN if filters is None else filters
        queries = StubQueries(
            rows=(
                {
                    "date_key": "2026-09-14",
                    "campaign_key": filters["campaign_key"],
                    measure_alias(0): 41,
                },
            )
        )
        request = _request(
            contracts,
            metric_name="reach_users",
            grains=("date", "campaign"),
            window=window,
            filters=filters,
        )
        tooling = MeasurementTooling(
            queries=queries, publications=StubPublications(), pack_coverage=coverage
        )
        try:
            return handle(request, tooling), coverage, queries
        except ValidationError as error:
            return error, coverage, queries

    def test_a_covered_campaign_day_plans_to_the_exposure_fact_at_campaign_by_day(
        self, contracts: Contracts
    ) -> None:
        answered, coverage, queries = self._ask(
            contracts, DateRange(self.DAY, self.DAY), frozenset({self.DAY})
        )
        assert isinstance(answered, ShapedResponse)
        (query,) = queries.seen
        logical = query.plan.logical
        assert (logical.fact, logical.measures) == ("fct_ad_exposure", ("reach_users",))
        assert logical.group_by == ("date_key", "campaign_key")
        (read,) = coverage.seen
        assert tuple(item.contract_id for item in read.contracts) == ("amc-dsp-reach",)
        assert "amc-dsp-reach" in answered.metadata.contract_ids

    def test_an_uncovered_day_reads_the_gate_s_reason_and_runs_no_query(
        self, contracts: Contracts
    ) -> None:
        refused, _coverage, queries = self._ask(
            contracts, DateRange(self.DAY, self.DAY), frozenset()
        )
        assert isinstance(refused, ValidationError)
        assert refused.code == CONTRACT_COVERAGE_REFUSED
        assert refused.description == (
            "Reach (distinct users) for this selection isn't measured yet, so it isn't answered."
        )
        assert queries.seen == []

    def test_a_day_before_pack_4_names_the_first_measured_day(self, contracts: Contracts) -> None:
        """14 Sep 2026 asked, DSP reach published from 4 Oct 2026: the refusal names 4 Oct."""
        first = date(2026, 10, 4)
        refused, _coverage, queries = self._ask(
            contracts, DateRange(self.DAY, self.DAY), frozenset({first})
        )
        assert isinstance(refused, ValidationError)
        assert refused.code == CONTRACT_COVERAGE_REFUSED
        assert refused.description == (
            "Reach (distinct users) for this selection is measured from 4 Oct 2026. This window "
            "is earlier, so it isn't answered. Ask for days from 4 Oct 2026 on."
        )
        assert queries.seen == []
        # Positive control: the named day answers.
        answered, _coverage, _queries = self._ask(
            contracts, DateRange(first, first), frozenset({first})
        )
        assert isinstance(answered, ShapedResponse)

    def test_a_seven_day_request_is_refused_by_the_per_day_rule(self, contracts: Contracts) -> None:
        """Every day covered, so only the distinct-count rule (`ADLY-CATALOG-0003`) refuses."""
        week = DateRange(self.DAY, self.DAY + timedelta(days=6))
        every_day = frozenset(week.start + timedelta(days=n) for n in range(7))

        def tooling(queries: StubQueries) -> MeasurementTooling:
            return MeasurementTooling(
                queries=queries,
                publications=StubPublications(),
                pack_coverage=_DspReachCoverage(every_day),
            )

        refused_queries = StubQueries()
        with pytest.raises(DomainError) as refused:
            handle(
                _request(
                    contracts,
                    metric_name="reach_users",
                    grains=("campaign",),
                    window=week,
                    filters=_DSP_CAMPAIGN,
                ),
                tooling(refused_queries),
            )
        assert type(refused.value) is DomainError
        assert refused.value.code == GRAIN_NOT_SUPPORTED_CODE
        assert refused.value.context == {
            "requested": "reach_users",
            "reason": "grain_not_supported",
        }
        assert refused.value.description == (
            "reach_users counts distinct users, so it cannot be aggregated across 7 days of "
            "date_key. Two periods' distinct shoppers are not the sum of each period's, because "
            "the same shopper may appear in both, and the source publishes no sketch that could "
            "be merged to find out. A coarser figure needs a new extraction at that grain rather "
            "than an aggregation of this one. Available instead: date, ecosystem, platform, "
            "channel_group, channel, campaign, ad_product, marketplace."
        )
        assert refused_queries.seen == []
        # Positive control: the same campaign request for one covered day is answered.
        answered_queries = StubQueries(
            rows=({"campaign_key": _DSP_CAMPAIGN["campaign_key"], measure_alias(0): 41},)
        )
        answered = handle(
            _request(
                contracts,
                metric_name="reach_users",
                grains=("campaign",),
                window=DateRange(self.DAY, self.DAY),
                filters=_DSP_CAMPAIGN,
            ),
            tooling(answered_queries),
        )
        assert isinstance(answered, ShapedResponse)
        (query,) = answered_queries.seen
        assert (query.plan.logical.fact, query.plan.logical.group_by) == (
            "fct_ad_exposure",
            ("campaign_key",),
        )

    def test_sponsored_reach_never_consults_dsp_reach_coverage(self, contracts: Contracts) -> None:
        """Paired control: amc-reach-frequency's reach is unchanged by pack 4."""
        sponsored = {
            "marketplace_key": "amazon_us",
            "channel_key": "amazon_sponsored_products",
            "ad_product_key": "sponsored_products",
            "campaign_key": "campaign:northstar-sponsored-03",
        }
        answered, coverage, _queries = self._ask(
            contracts, DateRange(self.DAY, self.DAY), frozenset(), sponsored
        )
        assert isinstance(answered, ShapedResponse)
        assert coverage.seen == []


_SPONSORED_VIEWS = {"marketplace_key": "amazon_us", "channel_key": "amazon_sponsored_products"}
_VIEWS_REFUSAL = (
    "The Viewable impressions metric is measured only for Amazon DSP, not Sponsored Products. "
    "Ask about Amazon DSP, or ask without a channel."
)


def _views_request(contracts: Contracts, channel: str) -> MeasurementToolRequest:
    return _request(
        contracts,
        metric_name="viewable_impressions",
        grains=("channel",),
        window=DateRange(date(2026, 8, 18), date(2026, 8, 18)),
        filters={**_SPONSORED_VIEWS, "channel_key": channel},
    )


class TestADspOnlyMeasureUnderASponsoredPin:
    """'Viewable impressions for Sponsored Products': a grounded refusal, not a broken tool.

    campaign-detail-500 review finding 1. The real selector over the committed packs and
    contracts; only the CURRENT port is a double, and the refusal is raised before any read.
    """

    def _tooling(self, queries: StubQueries) -> MeasurementTooling:
        return MeasurementTooling(
            queries=queries,
            publications=PublicationSelector(
                RecordingCurrent(),
                load_source_packs(ROOT),
                load_commerce_identity_bridge(ROOT),
                taxonomy=load_media_taxonomy(ROOT),
            ),
        )

    def test_handle_refuses_naming_the_one_channel_that_measures_it(
        self, contracts: Contracts
    ) -> None:
        queries = StubQueries()
        with pytest.raises(DomainError) as refused:
            handle(_views_request(contracts, "amazon_sponsored_products"), self._tooling(queries))

        assert type(refused.value) is DomainError
        assert str(refused.value.code) == "ADLY-PUBLICATION_SELECTION-0004"
        assert refused.value.user_visible_description == _VIEWS_REFUSAL
        assert queries.seen == []

    def test_the_same_measure_pinned_to_amazon_dsp_is_answered(self, contracts: Contracts) -> None:
        """Paired control: the pin, not the measure, is what is refused."""
        queries = StubQueries(rows=({"channel_key": "amazon_dsp", measure_alias(0): 7},))
        answered = handle(_views_request(contracts, "amazon_dsp"), self._tooling(queries))

        assert isinstance(answered, ShapedResponse)
        (query,) = queries.seen
        assert dict(query.plan.logical.filters)["channel_key"] == "amazon_dsp"

    def test_the_deployed_measurement_selector_refuses_it_too(self, contracts: Contracts) -> None:
        """The entry point's own composition carries the taxonomy, so chat gets the refusal.

        Imported under fictional configuration; the refusal is raised in binding, before the
        selector's CURRENT port (DynamoDB) or the query port (Athena) is reached.
        """
        queries = StubQueries()
        tooling = dataclasses.replace(measurement_entrypoint.TOOLING, queries=queries)
        with pytest.raises(DomainError) as refused:
            handle(_views_request(contracts, "amazon_sponsored_products"), tooling)

        assert str(refused.value.code) == "ADLY-PUBLICATION_SELECTION-0004"
        assert refused.value.user_visible_description == _VIEWS_REFUSAL
        assert queries.seen == []

    def test_sponsored_products_named_as_channel_and_ad_product_is_refused_too(
        self, contracts: Contracts
    ) -> None:
        """Two media pins the one writer contradicts: the same refusal, never a broken tool.

        campaign-detail-500 review 2 finding 1. Resolution keeps one entity per kind and the
        tool schema asks for every kind the customer names, so planning passes both pins to
        binding; before this fix the entry point's own selector raised the bare `ValueError`.
        """
        queries = StubQueries()
        tooling = dataclasses.replace(measurement_entrypoint.TOOLING, queries=queries)
        request = dataclasses.replace(
            _views_request(contracts, "amazon_sponsored_products"),
            filters={**_SPONSORED_VIEWS, "ad_product_key": "sponsored_products"},
        )
        with pytest.raises(DomainError) as refused:
            handle(request, tooling)

        assert type(refused.value) is DomainError
        assert str(refused.value.code) == "ADLY-PUBLICATION_SELECTION-0004"
        assert refused.value.user_visible_description == (
            "The Viewable impressions metric is measured only for Amazon DSP, not Sponsored "
            "Products. Ask about Amazon DSP, or ask without a channel and an ad product."
        )
        assert not refused.value.alarms
        assert queries.seen == []

    # No committed contract stamps `ecosystem_key` or `platform_key` as a column literal, and none
    # has two views writers, so these synthesize the views writer through `handle()` itself
    # (campaign-detail-500 review 3 LOW 3).
    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(
                (
                    "ecosystem_key",
                    "amazon",
                    "walmart",
                    "The Viewable impressions metric is measured only for Amazon, not Walmart. "
                    "Ask about Amazon, or ask without an ecosystem.",
                ),
                id="ecosystem",
            ),
            pytest.param(
                (
                    "platform_key",
                    "amazon_ads",
                    "amazon_retail",
                    "The Viewable impressions metric is measured only for Amazon Ads, not "
                    "Amazon Retail. Ask about Amazon Ads, or ask without a platform.",
                ),
                id="platform",
            ),
        ],
    )
    def test_an_ecosystem_or_platform_pin_the_writer_stamps_otherwise_is_refused(
        self, contracts: Contracts, case: tuple[str, str, str, str]
    ) -> None:
        column, stamped, pinned, reason = case
        views = _dsp_views(contracts)
        writer = dataclasses.replace(
            views,
            fields=(
                *(m for m in views.fields if m.target_column != column),
                FieldMapping(target_column=column, target_type="string", literal_value=stamped),
            ),
        )
        base = load_media_taxonomy(ROOT)
        taxonomy = dataclasses.replace(
            base,
            terms=(
                *base.terms,
                MediaTaxonomyTerm(EcosystemKey("walmart"), "Walmart", EcosystemKey("walmart")),
            ),
        )
        queries = StubQueries()
        tooling = MeasurementTooling(
            queries=queries,
            publications=PublicationSelector(
                RecordingCurrent(),
                load_source_packs(ROOT),
                load_commerce_identity_bridge(ROOT),
                taxonomy=taxonomy,
            ),
        )
        request = dataclasses.replace(
            _views_request(tuple(writer if c is views else c for c in contracts), "amazon_dsp"),
            filters={"marketplace_key": "amazon_us", column: pinned},
        )

        with pytest.raises(DomainError) as refused:
            handle(request, tooling)

        assert type(refused.value) is DomainError
        assert str(refused.value.code) == "ADLY-PUBLICATION_SELECTION-0004"
        assert refused.value.user_visible_description == reason
        assert not refused.value.alarms
        assert queries.seen == []

    def test_writers_contradicting_different_columns_stay_an_invariant(
        self, contracts: Contracts
    ) -> None:
        """One views writer stamps only the channel, another only the ad product.

        No single "measured only for" is true of both, so the customer's two pins are the bare
        no-pack `ValueError` the entry point alarms on, not a refusal.
        """
        views = _dsp_views(contracts)
        channel_only = _read_from_a_path(views, "ad_product_key")
        ad_product_only = dataclasses.replace(
            _read_from_a_path(views, "channel_key"), contract_id="unit-dsp-views-ad-product"
        )
        packs = load_source_packs(ROOT)
        views_pack = next(p for p in packs if p.contract_id == "amc-dsp-views")
        queries = StubQueries()
        tooling = MeasurementTooling(
            queries=queries,
            publications=PublicationSelector(
                RecordingCurrent(),
                (*packs, dataclasses.replace(views_pack, contract_id=ad_product_only.contract_id)),
                load_commerce_identity_bridge(ROOT),
                taxonomy=load_media_taxonomy(ROOT),
            ),
        )
        writers = (*(channel_only if c is views else c for c in contracts), ad_product_only)
        request = dataclasses.replace(
            _views_request(writers, "amazon_sponsored_products"),
            filters={**_SPONSORED_VIEWS, "ad_product_key": "sponsored_products"},
        )

        with pytest.raises(
            ValueError, match=r"^fct_ad_exposure: no source pack matches \('dsp_views',\)\.$"
        ) as raised:
            handle(request, tooling)

        assert type(raised.value) is ValueError
        assert queries.seen == []


def _dsp_views(contracts: Contracts) -> DataContract:
    return next(c for c in contracts if c.contract_id == "amc-dsp-views")


def _read_from_a_path(contract: DataContract, column: str) -> DataContract:
    """Return the contract with `column` read from a source path, not stamped as a literal."""
    return dataclasses.replace(
        contract,
        fields=tuple(
            dataclasses.replace(m, source_path="unit.media", literal_value=None)
            if m.target_column == column
            else m
            for m in contract.fields
        ),
    )
