# SPDX-License-Identifier: MIT-0
"""Fictional grounded Evaluation V4 calibration-corpus contract."""

import json
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import forecast_read_support as forecast_support
import pytest
import test_audience_segments as segments
from evaluation_gate_support import evaluator_document, spec_document
from forecast_series_support import policy as forecast_policy
from publication_support import StubPublications
from test_dimension_labels import (
    SCOPE,
    _LabelCatalog,  # pyright: ignore[reportPrivateUsage]
)
from test_measurement_tool import StubQueries

from adapters.inbound.forecast_read.service import ForecastReadService, ReadContext
from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from adapters.inbound.tool_handlers.forecast import ForecastTool, ForecastToolRequest
from adapters.inbound.tool_handlers.forecast import handle as forecast_handle
from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    handle,
)
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.metric_dictionary import load_catalog
from domain.conversation.grounded_evaluation import grounded_evaluation_context
from domain.evaluation.gate import (
    REQUIRED_CALIBRATION_KINDS,
    CalibrationCaseKind,
    parse_gate_spec,
)
from domain.measurement.planning import measure_alias
from domain.narrative.publication import (
    CollectionCompletenessV1,
    GroundedCollectionV1,
    GroundedRendering,
    GroundingSet,
    RenderingKind,
    grounded_refusal,
)
from domain.narrative.shaped_response import ShapedResponse
from domain.semantics.audience_segments import SegmentLabel, SegmentNamespace
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[2]
_CORPORA = (
    (
        "validation/evaluation-v4-cases.json",
        {"grounded_correctness", "internal_consistency", "helpfulness"},
    ),
)


def _records(corpus: str) -> list[dict[str, object]]:
    document = cast("dict[str, object]", json.loads((_ROOT / corpus).read_text()))
    return cast("list[dict[str, object]]", document["cases"])


def test_v4_calibration_cases_are_balanced_and_pin_the_observed_failure() -> None:
    raw: object = json.loads((_ROOT / "validation/evaluation-v4-cases.json").read_text())
    assert isinstance(raw, dict)
    document = cast("dict[str, object]", raw)
    assert document["schema_version"] == "EvaluationV4Calibration.v1"
    cases = document["cases"]
    assert isinstance(cases, list)
    records = cast("list[dict[str, object]]", cases)
    assert {record["name"] for record in records} == {
        "canonical_metric_inventory",
        "category_counts_do_not_reconcile",
        "general_explanation_with_governed_limitation",
        "general_explanation_smuggles_platform_claim",
        "fluent_unsupported_regrouping",
        "self_contradictory_status",
        "fluent_generic_non_answer",
        "answers_a_different_question",
        "clarifies_an_unnamed_comparison",
        "derives_a_percentage_change",
        "miscomputes_a_percentage_change",
        "derives_across_incompatible_bases",
        "labels_a_projection",
        "omits_a_mandatory_qualifier",
        "presents_a_top_n_list_as_complete",
        "omits_a_required_collection",
        "sums_figures_marked_non_additive",
        "reports_purchases_as_customers",
        "states_a_remembered_figure_when_the_metric_has_no_data",
        "restates_a_remembered_figure_after_a_refusal",
        "withheld_forecast_reason_stated_as_returned",
        "withheld_forecast_reason_extended_to_availability",
        "shown_forecast_total_stated_as_returned",
    }
    expected = [cast("dict[str, object]", record["expected"]) for record in records]
    grounded: set[str] = set()
    for item in expected:
        value = item.get("grounded_correctness")
        if value is not None:
            grounded.update(cast("list[str]", value) if isinstance(value, list) else [str(value)])
    assert grounded == {"Grounded", "Unsupported", "Not Applicable", "Partially Grounded"}
    mismatch = next(
        record for record in records if record["name"] == "category_counts_do_not_reconcile"
    )
    assert "7, 19, 9, 8, 6, 2, and 4" in str(mismatch["candidate"])
    assert cast("dict[str, str]", mismatch["expected"]) == {
        "grounded_correctness": "Unsupported",
        "internal_consistency": "Contradictory",
    }


_MIN_CASES_PER_DIMENSION = 3


def test_every_calibrated_dimension_has_enough_cases_and_both_outcomes() -> None:
    """The gate refuses a family whose evaluator has fewer than three cases (ADR-079 §5)."""
    for corpus, dimensions in _CORPORA:
        records = _records(corpus)
        for dimension in dimensions:
            labels = [
                cast("dict[str, object]", record["expected"])[dimension]
                for record in records
                if dimension in cast("dict[str, object]", record["expected"])
            ]
            assert len(labels) >= _MIN_CASES_PER_DIMENSION, (corpus, dimension)
            assert len({str(label) for label in labels}) >= 2, (corpus, dimension)


@pytest.mark.parametrize(
    ("corpus", "kinds"),
    [
        (
            "validation/evaluation-v4-cases.json",
            {
                "canonical_metric_inventory": "known_correct",
                "category_counts_do_not_reconcile": "contradiction",
                "general_explanation_with_governed_limitation": "known_correct",
                "general_explanation_smuggles_platform_claim": "fluent_unsupported",
                "fluent_unsupported_regrouping": "fluent_unsupported",
                "self_contradictory_status": "contradiction",
                "fluent_generic_non_answer": "fluent_unsupported",
                "answers_a_different_question": "off_topic",
                "clarifies_an_unnamed_comparison": "clarification",
                "derives_a_percentage_change": "known_correct",
                "miscomputes_a_percentage_change": "contradiction",
                "derives_across_incompatible_bases": "fluent_unsupported",
                "labels_a_projection": "known_correct",
                "omits_a_mandatory_qualifier": "fluent_unsupported",
                "presents_a_top_n_list_as_complete": "fluent_unsupported",
                "omits_a_required_collection": "fluent_unsupported",
                "sums_figures_marked_non_additive": "fluent_unsupported",
                "reports_purchases_as_customers": "fluent_unsupported",
                "states_a_remembered_figure_when_the_metric_has_no_data": "contradiction",
                "restates_a_remembered_figure_after_a_refusal": "fluent_unsupported",
                "withheld_forecast_reason_stated_as_returned": "known_correct",
                "withheld_forecast_reason_extended_to_availability": "fluent_unsupported",
                "shown_forecast_total_stated_as_returned": "known_correct",
            },
        ),
    ],
    ids=["v4"],
)
def test_every_case_declares_its_closed_calibration_kind(
    corpus: str, kinds: dict[str, str]
) -> None:
    records = _records(corpus)
    assert {str(record["name"]): record["kind"] for record in records} == kinds
    assert {str(record["kind"]) for record in records} <= {
        str(kind) for kind in CalibrationCaseKind
    }


def test_every_dimension_is_calibrated_on_each_required_kind() -> None:
    """agent-evaluation-validity: known-correct, contradiction, and fluent-but-unsupported."""
    for corpus, dimensions in _CORPORA:
        records = _records(corpus)
        for dimension in dimensions:
            covered = {
                str(record["kind"])
                for record in records
                if dimension in cast("dict[str, object]", record["expected"])
            }
            missing = {str(kind) for kind in REQUIRED_CALIBRATION_KINDS} - covered
            assert not missing, (corpus, dimension, missing)


@pytest.mark.parametrize(("corpus", "dimensions"), _CORPORA, ids=["v4"])
def test_each_corpus_passes_the_gates_own_calibration_rule(
    corpus: str, dimensions: set[str]
) -> None:
    """Projected as the stack projects it, the corpus parses as a gate spec's calibration."""
    evaluators = [
        evaluator_document(dimension, f"{dimension}-AbCdE{index:05d}")
        for index, dimension in enumerate(sorted(dimensions))
    ]
    cases = [
        {
            "name": record["name"],
            "kind": record["kind"],
            "question": record["question"],
            "grounded_context": "\n".join(
                f"- {assertion}" for assertion in cast("list[str]", record["trusted_assertions"])
            ),
            "candidate": record["candidate"],
            "expected": record["expected"],
        }
        for record in _records(corpus)
    ]
    spec = parse_gate_spec(json.dumps(spec_document(evaluators=evaluators, calibration=cases)))
    assert len(spec.calibration) == len(cases)


@pytest.mark.covers("ADR-083")
def test_obligation_cases_carry_the_production_grounded_context() -> None:
    """Exercise the obligation checks with the exact document the route sends (ADR-083)."""
    records = {
        str(record["name"]): record for record in _records("validation/evaluation-v4-cases.json")
    }
    production = json.loads(grounded_evaluation_context((_ONE_OF_EACH,)))
    for name in (
        "omits_a_mandatory_qualifier",
        "presents_a_top_n_list_as_complete",
        "omits_a_required_collection",
        "sums_figures_marked_non_additive",
        "reports_purchases_as_customers",
    ):
        context = json.loads(str(records[name]["grounded_context"]))
        assert context["schema_version"] == production["schema_version"]
        assert _shape(context) <= _shape(production), name
        assert cast("dict[str, object]", records[name]["expected"])["grounded_correctness"] == [
            "Unsupported",
            "Partially Grounded",
        ]
    qualifier = json.loads(str(records["omits_a_mandatory_qualifier"]["grounded_context"]))
    assert qualifier["obligations"]["required_rendering_ids"]
    top_n = json.loads(str(records["presents_a_top_n_list_as_complete"]["grounded_context"]))
    assert top_n["evidence"][0]["collections"][0]["completeness"] == "top_n"
    collection = json.loads(str(records["omits_a_required_collection"]["grounded_context"]))
    assert collection["obligations"]["required_collection_ids"] == ["catalog:reach"]
    assert collection["evidence"][0]["collections"][0]["required"] is True


_FICTIONAL_GROUNDING = "grounding-fictional-1"
_SEGMENT_FIGURES = (
    ("51001", "Fictional Segment A", 61709),
    ("51002", "Fictional Segment B", 37025),
    ("51003", "Fictional Segment C", 24683),
    ("51004", "Fictional Segment D", 16455),
)


def _fictional(response: ShapedResponse) -> str:
    """Encode a real tool answer's grounding as the evaluator gets it, under a fictional id."""
    grounding = response.metadata.grounding
    assert grounding is not None
    encoded = grounded_evaluation_context((grounding,))
    return encoded.replace(grounding.grounding_id, _FICTIONAL_GROUNDING)


def refusal_context(text: str) -> str:
    """Encode a tool refusal exactly as the measurement dispatch builds it, under a fictional id."""
    grounding = grounded_refusal(text, blocks_answer=True, identity_salt="corr-fictional")
    encoded = grounded_evaluation_context((grounding,))
    return encoded.replace(grounding.grounding_id, _FICTIONAL_GROUNDING)


def segment_impressions_context() -> str:
    """Run 3's `outside_top_n_segments` read: four segments of one campaign, one month."""
    labels = {
        segment: SegmentLabel(SegmentNamespace.AMC_DSP_BEHAVIOR, segment, name, None)
        for segment, name, _value in _SEGMENT_FIGURES
    }
    ids = tuple(segment for segment, _name, _value in _SEGMENT_FIGURES)
    rows: tuple[dict[str, object], ...] = tuple(
        {"segment_id": segment, measure_alias(0): value}
        for segment, _name, value in _SEGMENT_FIGURES
    )
    answered, *_ = segments._c1_chat(  # pyright: ignore[reportPrivateUsage]
        segments._c1_request("segment_impressions"),  # pyright: ignore[reportPrivateUsage]
        rows,
        segments._segment_roster(segments.SEPTEMBER, *ids),  # pyright: ignore[reportPrivateUsage]
        known=labels,
    )
    return _fictional(cast("ShapedResponse", answered))


def ntb_purchases_context() -> str:
    """Run 3's `new_customers_lake` read: NTB purchases for September, one marketplace."""
    catalog = load_catalog(_ROOT)
    response = handle(
        MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=catalog,
            contracts=tuple(contract for _path, contract in load_contracts(_ROOT)),
            metric_name="purchases_new_to_brand_12mo",
            grains=("marketplace",),
            window=DateRange(date(2026, 9, 1), date(2026, 9, 30)),
            computed_at=datetime(2026, 10, 17, 5, 0, tzinfo=UTC),
            correlation_id="corr-fictional",
            filters={"marketplace_key": "amazon_us"},
        ),
        MeasurementTooling(
            queries=StubQueries(rows=({"marketplace_key": "amazon_us", measure_alias(0): 45524},)),
            publications=StubPublications(),
            labels=DimensionLabelResolver(
                _LabelCatalog({("marketplace_key", "amazon_us"): "United States"}), SCOPE
            ),
        ),
    )
    return _fictional(response)


def test_the_answer_defect_cases_carry_the_context_the_tools_return() -> None:
    """Run 3's two answer defects, each graded on the grounding its tool really returns.

    The context is rebuilt from the measurement tool, so the case can't assume evidence the
    evaluator never sees live (the segment marker is there; a purchases answer carries the
    metric's label only, no customer definition).
    """
    records = {
        str(record["name"]): record for record in _records("validation/evaluation-v4-cases.json")
    }
    assert records["sums_figures_marked_non_additive"] == {
        "name": "sums_figures_marked_non_additive",
        "kind": "fluent_unsupported",
        "question": (
            "How many impressions did fictional Campaign Cedar deliver to each audience segment "
            "in September 2026?"
        ),
        "trusted_assertions": [
            "Fictional Campaign Cedar's DSP impressions matched to each segment in September 2026 "
            "were 61,709 for Fictional Segment A, 37,025 for Fictional Segment B, 24,683 for "
            "Fictional Segment C, and 16,455 for Fictional Segment D.",
            "These figures are not additive across audience segment: one impression repeats once "
            "per matched segment. The tool states no total and no share of a total.",
        ],
        "grounded_context": segment_impressions_context(),
        "candidate": (
            "In September 2026, fictional Campaign Cedar delivered **139,872 total impressions** "
            "across four audience segments:\n\n| Audience segment | Impressions |\n|---|---|\n"
            "| Fictional Segment A | 61,709 |\n| Fictional Segment B | 37,025 |\n"
            "| Fictional Segment C | 24,683 |\n| Fictional Segment D | 16,455 |\n\n"
            "Fictional Segment A received the most impressions, accounting for 44% of the "
            "campaign's segment-matched impressions."
        ),
        "expected": {"grounded_correctness": ["Unsupported", "Partially Grounded"]},
    }
    # Independent of regeneration: the committed record itself carries the additivity rule, so
    # a regenerated case that lost the marker can't pass under the same name.
    committed = json.loads(str(records["sums_figures_marked_non_additive"]["grounded_context"]))
    assert [
        (item["kind"], item["source_paths"])
        for item in committed["evidence"][0]["renderings"]
        if item["rendering_id"] == f"{_FICTIONAL_GROUNDING}:additivity"
    ] == [("context", ["data.additivity", "data.grain"])]
    # The candidate's total and share are the run-3 sum and share of these very figures.
    figures = [value for _segment, _name, value in _SEGMENT_FIGURES]
    assert (sum(figures), round(100 * figures[0] / sum(figures))) == (139872, 44)
    assert records["reports_purchases_as_customers"] == {
        "name": "reports_purchases_as_customers",
        "kind": "fluent_unsupported",
        "question": "How many new-to-brand customers did we acquire in September 2026?",
        "trusted_assertions": [
            "New-to-brand purchases (12 month lookback) in September 2026 were 45,524 in the "
            "United States marketplace.",
            "That figure counts purchases, not people. No count of new-to-brand customers or "
            "buyers was returned.",
        ],
        "grounded_context": ntb_purchases_context(),
        "candidate": (
            "You acquired **45,524 new-to-brand customers** in September 2026 in the United "
            "States marketplace. These are shoppers who had not purchased from your brand in the "
            "previous twelve months."
        ),
        "expected": {"grounded_correctness": ["Unsupported", "Partially Grounded"]},
    }


_NTB_REFUSAL = (
    "New-to-brand buyers (12 month lookback) hasn't been measured for September 2026. "
    "No complete month has been measured yet."
)
_REACH_REFUSAL = (
    "Shoppers reached hasn't been measured for the week of 5\u201311 Oct 2026. "
    "The nearest measured week is 28 Sep\u20134 Oct 2026."
)


@pytest.mark.covers("ADR-147")
def test_the_remembered_figure_cases_carry_the_refusal_the_tool_returns() -> None:
    """Both live failure shapes: a figure recalled from memory beside the tool's own refusal.

    The trusted evidence is only the refusal the tool returned that turn, so a figure the answer
    adds can come from nowhere but memory, and the evaluator must grade it Unsupported.
    """
    records = {
        str(record["name"]): record for record in _records("validation/evaluation-v4-cases.json")
    }
    assert records["states_a_remembered_figure_when_the_metric_has_no_data"] == {
        "name": "states_a_remembered_figure_when_the_metric_has_no_data",
        "kind": "contradiction",
        "question": (
            "How many new-to-brand customers did fictional Brand Maple acquire in September 2026?"
        ),
        "trusted_assertions": [_NTB_REFUSAL],
        "grounded_context": refusal_context(_NTB_REFUSAL),
        "candidate": (
            "New-to-brand buyers (12 month lookback) hasn't been measured for September 2026, so "
            "I can't read that figure from your data yet. No complete month has been measured.\n\n"
            "However, based on your tracking records, you acquired **45,524 new-to-brand "
            "customers** in September 2026."
        ),
        "expected": {
            "grounded_correctness": "Unsupported",
            "internal_consistency": "Contradictory",
        },
    }
    assert records["restates_a_remembered_figure_after_a_refusal"] == {
        "name": "restates_a_remembered_figure_after_a_refusal",
        "kind": "fluent_unsupported",
        "question": (
            "How many shoppers did fictional Brand Maple reach in the week of 5\u201311 October "
            "2026, by exposure group and ID type?"
        ),
        "trusted_assertions": [_REACH_REFUSAL],
        "grounded_context": refusal_context(_REACH_REFUSAL),
        "candidate": (
            "Shoppers reached hasn't been measured for the week of 5\u201311 Oct 2026 yet. The "
            "nearest measured week is 28 Sep\u20134 Oct 2026.\n\n"
            "Based on your previous query, the week of 28 Sep\u20134 Oct showed:\n\n"
            "| Exposure group | ID type | Shoppers reached |\n|---|---|---|\n"
            "| Fictional Group A | Fictional ID type X | 146,325 |\n"
            "| Fictional Group A | Fictional ID type Y | 98,410 |\n"
            "| Fictional Group B | Fictional ID type X | 61,207 |\n"
            "| Fictional Group B | Fictional ID type Y | 52,880 |\n"
            "| Fictional Group C | Fictional ID type X | 23,916 |\n"
            "| Fictional Group C | Fictional ID type Y | 17,342 |"
        ),
        "expected": {"grounded_correctness": "Unsupported"},
    }
    for name in (
        "states_a_remembered_figure_when_the_metric_has_no_data",
        "restates_a_remembered_figure_after_a_refusal",
    ):
        context = json.loads(str(records[name]["grounded_context"]))
        renderings = context["evidence"][0]["renderings"]
        assert [(item["kind"], item["source_paths"]) for item in renderings] == [
            ("refusal", ["refusal.reason"])
        ]
        assert renderings[0]["rendering_id"] == f"{_FICTIONAL_GROUNDING}:reason"


_ONE_OF_EACH = GroundingSet(
    grounding_id="grounding-shape",
    renderings=(
        GroundedRendering(
            rendering_id="grounding-shape:claim",
            text="A fictional claim.",
            kind=RenderingKind.CLAIM,
            source_paths=("data.value",),
        ),
    ),
    mandatory_rendering_ids=(),
    requires_claim_selection=True,
    collections=(
        GroundedCollectionV1(
            collection_id="grounding-shape:items",
            heading="Fictional items",
            items=("Alpha",),
            completeness=CollectionCompletenessV1.COMPLETE,
            source_paths=("data.items",),
            required=True,
        ),
    ),
)


def _shape(context: dict[str, object]) -> set[tuple[str, frozenset[str]]]:
    """Key sets per nesting level the evaluator reads, so a fixture cannot drift from the encoder.

    A fixture with no collection has no collection level; every level it has must match.
    """
    shapes = {
        ("document", frozenset(context)),
        ("obligations", frozenset(cast("dict[str, object]", context["obligations"]))),
    }
    for evidence in cast("list[dict[str, object]]", context["evidence"]):
        shapes.add(("evidence", frozenset(evidence)))
        for level in ("renderings", "collections"):
            shapes.update(
                (level, frozenset(item))
                for item in cast("list[dict[str, object]]", evidence[level])
            )
    return shapes


_INACTIVE_REASON = (
    "B0FIXT0104 isn't forecast for demand (ordered units): no sales were reported for it in the "
    "13 weeks of data through 2026-11-07, so there is no recent pattern to forecast. That alone "
    "doesn't say whether it is still for sale or what its future demand will be."
)
_FORECAST_QUALIFIER = (
    "Forecasts here continue the observed pattern, aren't causal predictions and exclude "
    "promotions."
)


def forecast_lookup_context(asin: str) -> str:
    """One `forecast_list_asins` lookup by exact ASIN over two fictional products.

    B0FIXT0104 is withheld `ASIN_INACTIVE`; B0FIXT0105 is shown with a stored 13-week total.
    """
    inactive = forecast_support.item(104, shown=False, reason="ASIN_INACTIVE")
    shown = forecast_support.item(105, p50="42.0000")
    entries = forecast_support.ranked([shown, inactive])
    reader = forecast_support.FakeReader(
        current=forecast_support.pointer(),
        meta=forecast_support.generation(entries),
        stored_pages=forecast_support.pages(entries),
        items={view.candidate.display_asin: view for view in (inactive, shown)},
    )
    service = ForecastReadService(reader, forecast_support.VALUES, forecast_policy())
    now = forecast_support.NOW
    read = forecast_handle(
        ForecastToolRequest(ForecastTool.LIST, {"asin": asin}, now, "corr-fictional"),
        service,
        ReadContext(now, time.monotonic() + 60),
    )
    return _fictional(read.response)


@pytest.mark.covers("ADR-083", "ADR-163")
def test_the_forecast_reason_cases_carry_the_context_the_forecast_tool_returns() -> None:
    """Chat said a product withheld for no recent sales "isn't available for sale" (dev, Q4).

    The truthful answer and the false one differ only in the claim the stored reason doesn't
    support, so the pair grades that claim; the shown-product case is the positive control.
    """
    records = {
        str(record["name"]): record for record in _records("validation/evaluation-v4-cases.json")
    }
    question = "Why is there no demand forecast for fictional product B0FIXT0104?"
    assertions = [
        _INACTIVE_REASON,
        "The forecast qualifier: forecasts continue the observed pattern, aren't causal "
        "predictions and exclude promotions.",
    ]
    inactive = forecast_lookup_context("B0FIXT0104")
    assert records["withheld_forecast_reason_stated_as_returned"] == {
        "name": "withheld_forecast_reason_stated_as_returned",
        "kind": "known_correct",
        "question": question,
        "trusted_assertions": assertions,
        "grounded_context": inactive,
        "candidate": (
            "B0FIXT0104 (Fictional product 104) has no demand forecast because no sales were "
            "reported for it in the 13 weeks of data through 2026-11-07, so there is no recent "
            "pattern to forecast. That alone doesn't say whether it is still for sale or what its "
            f"future demand will be. {_FORECAST_QUALIFIER}"
        ),
        "expected": {"grounded_correctness": "Grounded", "internal_consistency": "Consistent"},
    }
    assert records["withheld_forecast_reason_extended_to_availability"] == {
        "name": "withheld_forecast_reason_extended_to_availability",
        "kind": "fluent_unsupported",
        "question": question,
        "trusted_assertions": assertions,
        "grounded_context": inactive,
        "candidate": (
            "B0FIXT0104 (Fictional product 104) has no demand forecast because the product is "
            "inactive: no sales were reported for it in the 13 weeks of data through 2026-11-07. "
            "Inactive products aren't currently available for sale, so they won't generate "
            f"future demand. {_FORECAST_QUALIFIER}"
        ),
        "expected": {"grounded_correctness": ["Unsupported", "Partially Grounded"]},
    }
    assert records["shown_forecast_total_stated_as_returned"] == {
        "name": "shown_forecast_total_stated_as_returned",
        "kind": "known_correct",
        "question": "What is the 13-week demand forecast for fictional product B0FIXT0105?",
        "trusted_assertions": [
            "The stored likely (p50) forecast total of units ordered for B0FIXT0105 over the "
            "next 13 weeks is 546.",
            "The forecast qualifier: forecasts continue the observed pattern, aren't causal "
            "predictions and exclude promotions.",
        ],
        "grounded_context": forecast_lookup_context("B0FIXT0105"),
        "candidate": (
            "The likely (p50) demand forecast for B0FIXT0105 (Fictional product 105) over the "
            "next 13 weeks is **546 units ordered**. This is a forecast, not observed sales: it "
            "continues the observed pattern, isn't a causal prediction and excludes promotions."
        ),
        "expected": {"grounded_correctness": "Grounded", "internal_consistency": "Consistent"},
    }
    # The committed context itself carries the reason as a claim, so a regenerated case that lost
    # it can't pass under the same name.
    committed = json.loads(inactive)
    assert [
        item["kind"]
        for item in committed["evidence"][0]["renderings"]
        if item["text"] == _INACTIVE_REASON
    ] == ["claim"]
