# SPDX-License-Identifier: MIT-0
"""Tests for the ShapedResponse envelope.

Each test here pins an invariant that exists to make a dishonest response
impossible to construct, rather than merely discouraged.
"""

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from domain.narrative.publication import AnswerQualifier, QualifierKind, RenderingKind
from domain.narrative.shaped_response import (
    ClaimField,
    FreshnessStatus,
    Grain,
    MeasurementBasis,
    Metadata,
    Recommendation,
    ResolutionProvenance,
    ResponseData,
    ShapedResponse,
    ShapeError,
    Suppression,
    UnitKind,
    Units,
)
from domain.semantics.analytical_resolution import FieldOrigin

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-006", "ADR-010")]

T0 = datetime(2026, 8, 1, tzinfo=UTC)
T1 = datetime(2026, 8, 18, tzinfo=UTC)


def make_metadata(**overrides: object) -> Metadata:
    defaults: dict[str, object] = {
        "facts_read": ("fct_ad_exposure",),
        "measurement_basis": (MeasurementBasis.AD_ATTRIBUTED,),
        "coverage_start": T0,
        "coverage_end": T1,
        "freshness_status": FreshnessStatus.FRESH,
        "suppression": Suppression(rows_suppressed=False),
        "row_count": 1,
        "computed_at": T1,
        "correlation_id": "corr-1",
    }
    return Metadata(**(defaults | overrides))  # type: ignore[arg-type]


def make_data(rows: int = 1) -> ResponseData:
    """Build response data, with units present exactly when values are.

    Units are optional only for an empty result, so a helper that always supplied them could
    not build the zero-row case at all — which is the case an empty monetary answer needs.
    """
    row: dict[str, str | int | Decimal | None] = {"spend": Decimal("100.00")}
    values = tuple(dict(row) for _ in range(rows))
    units = Units(kind=UnitKind.CURRENCY, currency_code="USD") if values else None
    return ResponseData(
        values=values,
        units=units,
        grain=Grain(dimensions=("date", "campaign")),
        claim_fields=() if units is None else (ClaimField("spend", units),),
    )


class TestUnits:
    def test_currency_requires_a_code(self) -> None:
        # canonical-model forbids a bare monetary amount, so the type does too.
        with pytest.raises(ShapeError, match="currency_code"):
            Units(kind=UnitKind.CURRENCY)

    @pytest.mark.parametrize(
        "kind", [UnitKind.COUNT, UnitKind.RATIO, UnitKind.PERCENT, UnitKind.DURATION_SECONDS]
    )
    def test_non_currency_rejects_a_code(self, kind: UnitKind) -> None:
        """Every non-currency kind, because `UnitKind` is a `StrEnum` and one of them was special.

        Found by mutation: `is not UnitKind.CURRENCY` relaxed to `< UnitKind.CURRENCY` survived,
        because a `StrEnum` compares as its value and `"count"` is the only kind that sorts below
        `"currency"`. The single case the test used was the one case that could not distinguish the
        two, so a ratio carrying a currency code would have been accepted.
        """
        with pytest.raises(ShapeError, match="meaningless"):
            Units(kind=kind, currency_code="USD")

    def test_currency_with_a_code_is_valid(self) -> None:
        assert Units(kind=UnitKind.CURRENCY, currency_code="GBP").currency_code == "GBP"


class TestGrain:
    def test_grain_cannot_be_empty(self) -> None:
        """A correct figure at an unstated grain is the failure this product must not have.

        `ValueError` rather than `ShapeError`: `Grain` is a shared-kernel value object now,
        not an envelope invariant, and it raises what its neighbours raise. The guarantee is
        unchanged — the grain refuses itself before a response can be built around it.
        """
        with pytest.raises(ValueError, match="at least one dimension"):
            Grain(dimensions=())

    def test_renders_readably(self) -> None:
        assert str(Grain(dimensions=("date", "campaign"))) == "date x campaign"


class TestMixesBasis:
    def test_single_basis_does_not_mix(self) -> None:
        assert make_metadata().mixes_basis is False

    def test_two_bases_mix(self) -> None:
        meta = make_metadata(
            measurement_basis=(MeasurementBasis.AD_ATTRIBUTED, MeasurementBasis.RETAIL_ACTUAL)
        )
        assert meta.mixes_basis is True

    def test_repeated_basis_does_not_mix(self) -> None:
        # Derived from the distinct set, so listing one basis twice is not mixing.
        meta = make_metadata(
            measurement_basis=(MeasurementBasis.AD_ATTRIBUTED, MeasurementBasis.AD_ATTRIBUTED)
        )
        assert meta.mixes_basis is False

    def test_surfaced_on_the_response(self) -> None:
        response = ShapedResponse(
            data=make_data(),
            narrative=("Spend was 100.00 USD.",),
            metadata=make_metadata(
                measurement_basis=(MeasurementBasis.AD_ATTRIBUTED, MeasurementBasis.MODELLED)
            ),
        )
        assert response.requires_basis_warning is True


class TestResolutionProvenance:
    def test_public_type_is_frozen_and_has_only_sanitized_scalar_fields(self) -> None:
        provenance = ResolutionProvenance(
            field="campaign",
            origin=FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            value="campaign:fictional",
        )

        assert tuple(item.name for item in fields(provenance)) == ("field", "origin", "value")
        assert not hasattr(provenance, "__dict__")
        with pytest.raises(FrozenInstanceError):
            provenance.value = "changed"  # type: ignore[misc]


class TestMetadataInvariants:
    def test_must_name_the_facts_read(self) -> None:
        with pytest.raises(ShapeError, match="facts it read"):
            make_metadata(facts_read=())

    def test_must_declare_a_basis(self) -> None:
        with pytest.raises(ShapeError, match="measurement basis"):
            make_metadata(measurement_basis=())

    def test_rejects_naive_timestamps(self) -> None:
        with pytest.raises(ShapeError, match="timezone-aware"):
            make_metadata(coverage_start=datetime(2026, 8, 1))  # noqa: DTZ001

    def test_rejects_inverted_coverage(self) -> None:
        with pytest.raises(ShapeError, match="cannot precede"):
            make_metadata(coverage_start=T1, coverage_end=T0)

    def test_coverage_bounds_may_be_the_same_instant(self) -> None:
        """Inverted is refused; identical is not, and only inversion was pinned.

        Found by mutation: `<` relaxed to `<=` survived. A window describing a single instant is a
        legitimate shape, and refusing it would reject an answer rather than a bad bound.
        """
        assert make_metadata(coverage_start=T1, coverage_end=T1).coverage_end == T1

    def test_rejects_a_negative_row_count(self) -> None:
        """Nothing exercised the guard at all, so any weaker bound passed.

        Found by mutation: `row_count < 0` relaxed to `row_count < -1` survived.
        """
        with pytest.raises(ShapeError, match="cannot be negative"):
            make_metadata(row_count=-1)

    def test_requires_a_correlation_id(self) -> None:
        with pytest.raises(ShapeError, match="correlation_id"):
            make_metadata(correlation_id="")

    def test_rejects_duplicate_contract_ids(self) -> None:
        """A contract listed twice reads as one that contributed twice.

        Refused rather than deduplicated, because the duplicate means a caller assembled the list
        wrongly and silently fixing it hides that.
        """
        with pytest.raises(ShapeError, match="contract_ids"):
            make_metadata(contract_ids=("vendor-sales-by-asin", "vendor-sales-by-asin"))

    def test_rejects_a_duplicate_followed_by_a_later_id(self) -> None:
        """The duplicate that sorts *below* what follows it, which the pair above does not cover.

        Found by mutation: `!=` relaxed to `>` survived, and this is its counterexample. A list
        that repeats its first element compares as *less* than its own deduplicated form — the
        second element is the smaller one — so a comparison rather than an inequality accepts it.
        Both existing cases happen to compare greater, so neither could distinguish the two.
        """
        with pytest.raises(ShapeError, match="contract_ids"):
            make_metadata(
                contract_ids=(
                    "vendor-sales-by-asin",
                    "vendor-sales-by-asin",
                    "vendor-traffic-by-asin",
                )
            )

    def test_rejects_unsorted_contract_ids(self) -> None:
        """Load order carries no meaning, so two identical answers must not differ by it."""
        with pytest.raises(ShapeError, match="contract_ids"):
            make_metadata(contract_ids=("vendor-traffic-by-asin", "vendor-sales-by-asin"))

    def test_accepts_sorted_unique_contract_ids(self) -> None:
        metadata = make_metadata(contract_ids=("vendor-sales-by-asin", "vendor-traffic-by-asin"))
        assert metadata.contract_ids == ("vendor-sales-by-asin", "vendor-traffic-by-asin")

    def test_no_contract_ids_is_still_constructible(self) -> None:
        """The catalog path supplies none, and G-37 records why that half is still open."""
        assert make_metadata().contract_ids == ()

    def test_facts_read_is_exempt_from_the_ordering_rule(self) -> None:
        """Its order is the metric's numerator-then-denominator order, which is information.

        A drill-across names its numerator fact first. Sorting that would discard which side of the
        ratio each fact is, so the rule applies to `contract_ids` alone.
        """
        metadata = make_metadata(facts_read=("fct_retail_sales", "fct_ad_exposure"))
        assert metadata.facts_read == ("fct_retail_sales", "fct_ad_exposure")


class TestResponseInvariants:
    def test_row_count_must_match_values(self) -> None:
        # A tool cannot report a count it did not produce.
        with pytest.raises(ShapeError, match="does not match"):
            ShapedResponse(
                data=make_data(rows=2),
                narrative=("x",),
                metadata=make_metadata(row_count=5),
            )

    def test_truncation_must_be_stated_in_the_narrative(self) -> None:
        # A silently truncated result read as complete is a correctness bug.
        with pytest.raises(ShapeError, match="must say so"):
            ShapedResponse(
                data=make_data(),
                narrative=("Spend was 100.00 USD.",),
                metadata=make_metadata(truncated=True),
            )

    def test_truncation_stated_is_accepted(self) -> None:
        response = ShapedResponse(
            data=make_data(),
            narrative=("Spend was 100.00 USD.", "Results were truncated at 1 row."),
            metadata=make_metadata(truncated=True),
        )
        assert response.metadata.truncated is True

    def test_narrative_is_required(self) -> None:
        with pytest.raises(ShapeError, match="narrative"):
            ShapedResponse(data=make_data(), narrative=(), metadata=make_metadata())

    def test_a_row_count_below_the_values_carried_is_refused(self) -> None:
        """The other direction, and the one no test exercised.

        Reporting fewer rows than were carried is the more dangerous of the two: a reader who
        trusts the count reconciles against a population smaller than the one they were shown.
        Found by mutation — `!=` relaxed to `>` passed every existing assertion, because all of
        them over-reported.
        """
        with pytest.raises(ShapeError, match="does not match"):
            ShapedResponse(
                data=make_data(rows=3),
                narrative=("x",),
                metadata=make_metadata(row_count=1),
            )

    def test_a_count_matching_beyond_the_small_integer_cache_is_accepted(self) -> None:
        """Compared by value and never by identity, which only diverges past 256.

        CPython interns small integers, so `is not` agrees with `!=` for every count a test
        normally uses and disagrees for a real breakdown. `domain/curation/watermarks` carried
        exactly this defect against a value built from DynamoDB, so it is a shape rather than a
        curiosity.
        """
        wide = 300
        response = ShapedResponse(
            data=make_data(rows=wide),
            narrative=("x",),
            metadata=make_metadata(row_count=wide),
        )
        assert response.metadata.row_count == wide

    def test_many_unique_ranks_beyond_the_small_integer_cache_are_accepted(self) -> None:
        # The same identity-versus-value trap on the recommendation ranks.
        ranked = tuple(
            Recommendation(statement=f"s{n}", rationale="r", rank=n) for n in range(1, 301)
        )
        response = ShapedResponse(
            data=make_data(),
            narrative=("x",),
            metadata=make_metadata(),
            recommendations=ranked,
        )
        assert len(response.recommendations) == 300

    def test_zero_rows_is_a_valid_response(self) -> None:
        # "No data for that window" is an answer, not a failure — ADR-010.
        response = ShapedResponse(
            data=make_data(rows=0),
            narrative=("No data for that window.",),
            metadata=make_metadata(row_count=0),
        )
        assert response.metadata.row_count == 0


class TestTheEnvelopeCannotBeEditedPastItsOwnChecks:
    """Every invariant here runs at construction, so mutability would be a way round all of them.

    The population total is the sharpest case: `ShapedResponse.__post_init__` reads
    `data.population_total` to pair it with a sentence, so a `ResponseData` that could be assigned
    to afterwards would let a figure appear in the envelope with nothing stating it — which is the
    exact condition the invariant exists to make unconstructible.

    Found by mutation: `frozen=True` flipped to `False` on all five types and every test still
    passed, because none of them tried to assign.
    """

    def test_response_data_cannot_be_assigned_to(self) -> None:
        data = make_data()
        with pytest.raises(FrozenInstanceError):
            data.population_total = Decimal("999.99")  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("subject", "attribute"),
        [
            (Units(kind=UnitKind.COUNT), "kind"),
            (Recommendation(statement="s", rationale="r", rank=1), "rank"),
            (make_data(), "values"),
            (make_metadata(), "row_count"),
        ],
    )
    def test_every_envelope_type_is_frozen(self, subject: object, attribute: str) -> None:
        with pytest.raises(FrozenInstanceError):
            setattr(subject, attribute, None)

    def test_the_response_itself_is_frozen(self) -> None:
        response = ShapedResponse(data=make_data(), narrative=("x",), metadata=make_metadata())
        with pytest.raises(FrozenInstanceError):
            response.narrative = ()  # type: ignore[misc]


@pytest.mark.covers("G-45")
class TestThePopulationTotalCannotDriftFromItsSentence:
    """G-45: the model summed five rows because nothing else had.

    Supplying the total removes the motive. These pin the two ways supplying it could still go
    wrong — a figure no sentence states, and a figure over nothing.
    """

    def test_a_total_no_narrative_line_states_is_refused(self) -> None:
        with pytest.raises(ShapeError, match="no narrative line states it"):
            ShapedResponse(
                data=_data_with_total(Decimal("100.00")),
                narrative=("Spend was reported by campaign.",),
                metadata=make_metadata(),
            )

    def test_a_total_stated_verbatim_is_accepted(self) -> None:
        response = ShapedResponse(
            data=_data_with_total(Decimal("100.00")),
            narrative=("Across all 1 row(s) above the total is 100.00 USD.",),
            metadata=make_metadata(),
        )
        assert response.data.population_total == Decimal("100.00")

    def test_a_total_restated_at_a_different_scale_is_refused(self) -> None:
        """The failure a keyword check would pass and this one catches.

        A sentence rebuilt with `100.0` describes a different figure from `Decimal("100.00")`, and
        for money the trailing place is where a cent goes missing. The envelope carries the scale
        the tool computed, so the sentence has to carry it too.
        """
        with pytest.raises(ShapeError, match="no narrative line states it"):
            ShapedResponse(
                data=_data_with_total(Decimal("100.00")),
                narrative=("Across all 1 row(s) above the total is 100.0 USD.",),
                metadata=make_metadata(),
            )

    def test_a_total_over_no_values_is_refused(self) -> None:
        """Zero rows is an answer; a total over them is a claim about a population never read."""
        with pytest.raises(ShapeError, match="carrying no values"):
            ResponseData(
                values=(),
                units=None,
                grain=Grain(dimensions=("campaign",)),
                population_total=Decimal("0.00"),
            )

    def test_no_total_is_the_default_so_every_existing_caller_still_builds(self) -> None:
        """The catalog path answers from files and has nothing to total."""
        assert make_data().population_total is None


def _data_with_total(total: Decimal | int) -> ResponseData:
    """Response data carrying a population total, denominated by the rows' own units."""
    return ResponseData(
        values=({"spend": Decimal("100.00")},),
        units=Units(kind=UnitKind.CURRENCY, currency_code="USD"),
        grain=Grain(dimensions=("campaign",)),
        population_total=total,
    )


class TestRecommendations:
    def test_rationale_is_required(self) -> None:
        # The model presents recommendations; it never invents them.
        with pytest.raises(ShapeError, match="rationale"):
            Recommendation(statement="Raise the bid.", rationale="", rank=1)

    def test_rank_starts_at_one(self) -> None:
        with pytest.raises(ShapeError, match="rank starts at 1"):
            Recommendation(statement="x", rationale="y", rank=0)

    def test_a_rank_beyond_the_first_is_accepted(self) -> None:
        """A floor, not an equality — and nothing pinned that.

        Found by mutation: `rank < 1` relaxed to `rank != 1` survived, because every valid rank in
        this module's tests was 1. A second and third recommendation would have been refused.
        """
        assert Recommendation(statement="x", rationale="y", rank=3).rank == 3

    def test_ranks_must_be_unique(self) -> None:
        with pytest.raises(ShapeError, match="unique"):
            ShapedResponse(
                data=make_data(),
                narrative=("x",),
                metadata=make_metadata(),
                recommendations=(
                    Recommendation(statement="a", rationale="r", rank=1),
                    Recommendation(statement="b", rationale="r", rank=1),
                ),
            )


class TestUnitsAreTiedToValues:
    """Absent exactly when the figures are, and the tie is enforced both ways.

    Units used to be mandatory, and that made one honest answer unconstructible: a monetary
    figure's currency comes from the rows, so an empty monetary result has no currency to
    state — and zero rows is an answer rather than a refusal. Making units optional *only*
    when there are no values keeps the canonical model's ban on a bare amount intact.
    """

    def test_an_empty_result_may_carry_no_units(self) -> None:
        data = ResponseData(values=(), units=None, grain=Grain(dimensions=("amazon_asin",)))

        assert data.units is None

    def test_values_without_units_are_refused(self) -> None:
        # A figure without its units is the bare monetary amount the canonical model forbids.
        with pytest.raises(ShapeError, match="no units to state them in"):
            one: dict[str, str | int | Decimal | None] = {"value": Decimal("1.0000")}
            ResponseData(
                values=(one,),
                units=None,
                grain=Grain(dimensions=("amazon_asin",)),
            )

    def test_units_without_values_are_refused(self) -> None:
        """The direction that is easy to miss, and it is a claim about absent data.

        Denominating nothing states that the absent rows would have been in that currency,
        which is exactly the assumption reading a marketplace default was rejected for.
        """
        with pytest.raises(ShapeError, match="nothing to denominate"):
            ResponseData(
                values=(),
                units=Units(kind=UnitKind.CURRENCY, currency_code="USD"),
                grain=Grain(dimensions=("amazon_asin",)),
            )

    def test_the_grain_stays_mandatory_when_empty(self) -> None:
        # "Nothing sold, by ASIN" and "nothing sold, in total" are different answers, and only
        # the grain distinguishes them.
        with pytest.raises(ValueError, match="dimension"):
            ResponseData(values=(), units=None, grain=Grain(dimensions=()))


class TestGroundingEnvelope:
    def test_response_derives_candidate_bound_claim_renderings(self) -> None:
        response = ShapedResponse(
            data=make_data(),
            narrative=("Spend was measured for the requested period.",),
            metadata=make_metadata(),
        )

        grounding = response.metadata.grounding
        assert grounding is not None
        claims = tuple(item for item in grounding.renderings if item.kind is RenderingKind.CLAIM)
        assert len(claims) == 1
        assert claims[0].text == ("Spend was 100.00 USD, at the date and campaign grain.")

    def test_governed_default_metric_origin_becomes_fixed_mandatory_context(self) -> None:
        canonical_metric = "private-canonical-metric"
        response = ShapedResponse(
            data=make_data(),
            narrative=("Spend was measured.",),
            metadata=make_metadata(
                resolution_provenance=(
                    ResolutionProvenance(
                        "metric",
                        FieldOrigin.GOVERNED_DEFAULT,
                        canonical_metric,
                    ),
                )
            ),
        )

        grounding = response.metadata.grounding
        assert grounding is not None
        disclosures = tuple(
            item
            for item in grounding.renderings
            if item.source_paths == ("metadata.resolution_provenance[0].origin",)
        )
        assert len(disclosures) == 1
        disclosure = disclosures[0]
        assert disclosure.text == "Metric selection used the governed default."
        assert disclosure.kind is RenderingKind.CONTEXT
        assert disclosure.rendering_id in grounding.mandatory_rendering_ids
        assert canonical_metric not in repr(grounding)

    def test_governed_default_window_origin_disclosure_names_the_window(self) -> None:
        """A defaulted period the user cannot see is a population they did not choose."""
        response = ShapedResponse(
            data=make_data(),
            narrative=("Spend was measured.",),
            metadata=make_metadata(
                resolution_provenance=(
                    ResolutionProvenance(
                        "current_period",
                        FieldOrigin.GOVERNED_DEFAULT,
                        "2026-08-10..2026-08-16",
                    ),
                )
            ),
        )
        grounding = response.metadata.grounding
        assert grounding is not None
        disclosure = next(
            item
            for item in grounding.renderings
            if item.source_paths == ("metadata.resolution_provenance[0].origin",)
        )
        assert disclosure.text == (
            "No reporting window was specified, so this answer uses "
            "2026-08-10..2026-08-16, the latest published coverage."
        )
        assert disclosure.kind is RenderingKind.CONTEXT
        assert disclosure.rendering_id in grounding.mandatory_rendering_ids

    def test_other_provenance_never_becomes_generic_grounded_text(self) -> None:
        # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
        private_key = "campaign:private"
        response = ShapedResponse(
            data=make_data(),
            narrative=("Spend was measured.",),
            metadata=make_metadata(
                resolution_provenance=(
                    ResolutionProvenance(
                        "metric",
                        FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
                        "impressions",
                    ),
                    ResolutionProvenance(
                        "campaign",
                        FieldOrigin.GOVERNED_DEFAULT,
                        private_key,
                    ),
                )
            ),
        )

        grounding = response.metadata.grounding
        assert grounding is not None
        assert all(
            not path.startswith("metadata.resolution_provenance")
            for item in grounding.renderings
            for path in item.source_paths
        )
        assert private_key not in repr(grounding)

    def test_typed_mandatory_qualifier_must_exist_verbatim_in_narrative(self) -> None:
        qualifier = AnswerQualifier(
            kind=QualifierKind.SUPPRESSED,
            rendering="Some rows were withheld by privacy thresholds.",
            source_paths=("metadata.suppression",),
        )
        with pytest.raises(ShapeError, match="suppressed qualifier"):
            ShapedResponse(
                data=make_data(),
                narrative=("Spend was 100.00 USD.",),
                metadata=make_metadata(qualifiers=(qualifier,)),
            )

    def test_typed_qualifier_becomes_a_mandatory_grounding_rendering(self) -> None:
        line = "Some rows were withheld by privacy thresholds."
        qualifier = AnswerQualifier(
            kind=QualifierKind.SUPPRESSED,
            rendering=line,
            source_paths=("metadata.suppression",),
        )
        response = ShapedResponse(
            data=make_data(),
            narrative=("Spend was measured.", line),
            metadata=make_metadata(qualifiers=(qualifier,)),
        )

        grounding = response.metadata.grounding
        assert grounding is not None
        required = {
            item.rendering_id
            for item in grounding.renderings
            if item.qualifier is QualifierKind.SUPPRESSED
        }
        assert required
        assert required <= set(grounding.mandatory_rendering_ids)


class TestQualifierEvidencePaths:
    def test_unknown_source_path_is_refused_before_grounding(self) -> None:
        qualifier = AnswerQualifier(
            kind=QualifierKind.PARTIAL,
            rendering="Loaded data is partial.",
            source_paths=("metadata.nonexistent",),
        )
        with pytest.raises(ShapeError, match="unknown typed paths"):
            make_metadata(qualifiers=(qualifier,))


class TestNumericalNarrativeSeparation:
    def test_untyped_numerical_scaffolding_is_not_releasable_context(self) -> None:
        line = "Compared 2 periods over 7 days."
        response = ShapedResponse(
            data=make_data(),
            narrative=(line,),
            metadata=make_metadata(),
        )

        grounding = response.metadata.grounding
        assert grounding is not None
        assert all(item.text != line for item in grounding.renderings)
