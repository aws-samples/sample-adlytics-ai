# SPDX-License-Identifier: MIT-0
"""The closed isolated-refusal table (ADR-142, design 02 §3.1, §4, §7).

Every case pairs a refusal that constructs, classifies or groups with a control that fails on
the defect it guards: an unknown key, an out-of-range figure, a code outside the taxonomy, a
scope the page doesn't declare.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from adapters.outbound.publication_selection import (
    PACK_SNAPSHOT_TOO_LARGE,
    SELECTION_FAILED,
    SELECTION_TOO_LARGE,
)
from adapters.outbound.telemetry.graph_metrics import (
    BAKE_ISOLATED_REFUSAL_DATA_METRIC,
    BAKE_ISOLATED_REFUSAL_LIMIT_METRIC,
    isolated_refusal_metric,
)
from domain.operations.bake_refusals import (
    BAKE_REFUSAL,
    PARTITION_BOUND_CODES,
    REFUSAL_CLASSES,
    REFUSAL_CONTEXT,
    REFUSAL_SCOPE_INVALID,
    IsolatedRefusal,
    IsolatedRefusalReason,
    RefusalClass,
    RefusalGroup,
    RefusalScope,
    check_refusal_scope,
    classify_refusal,
    group_refusals,
)
from domain.shared_kernel.errors import (
    DomainError,
    ErrorCode,
    InvariantError,
    ValidationError,
)

pytestmark = pytest.mark.unit

R = IsolatedRefusalReason
PAGE, SECTION, ITEM = RefusalScope.PAGE, RefusalScope.SECTION, RefusalScope.ITEM


def test_the_class_table_is_closed_over_every_reason_and_its_allowlists() -> None:
    assert set(REFUSAL_CLASSES) == set(R) == set(REFUSAL_CONTEXT)
    by_class = {
        klass: {reason.value for reason, value in REFUSAL_CLASSES.items() if value is klass}
        for klass in RefusalClass
    }
    assert by_class == {
        RefusalClass.LIMIT: {
            "rows_over_ceiling",
            "body_over_cap",
            "bodies_over_invocation",
            "partitions_over_bound",
            "statement_time_over_invocation",
            "population_over_ceiling",
        },
        RefusalClass.DEPENDENCY: {"dependency_unavailable"},
        RefusalClass.DATA: {"source_refused", "history_gap", "evidence_incomplete"},
    }


def test_a_refusal_is_a_domain_error_with_the_bake_refusal_code_and_no_free_text() -> None:
    refusal = IsolatedRefusal(R.ROWS_OVER_CEILING, PAGE, rows_read=10_001, max_rows=10_000)
    assert isinstance(refusal, DomainError) and refusal.code == BAKE_REFUSAL
    assert (refusal.reason, refusal.scope, refusal.count) == (R.ROWS_OVER_CEILING, PAGE, 1)
    assert dict(refusal.refusal_context) == {"max_rows": 10_000, "rows_read": 10_001}
    assert refusal.refusal_class is RefusalClass.LIMIT


def test_an_unknown_context_key_raises_at_construction_and_an_allowed_one_constructs() -> None:
    with pytest.raises(ValueError, match="does not carry context key 'observed'"):
        IsolatedRefusal(R.BODIES_OVER_INVOCATION, PAGE, observed=3)
    # Paired: the reason's own keys construct, static and runtime forms alike.
    IsolatedRefusal(R.BODIES_OVER_INVOCATION, PAGE, bodies=900, max_bodies=800)
    IsolatedRefusal(R.BODIES_OVER_INVOCATION, PAGE, written=4, bodies=9)
    # A reason with no context takes none.
    with pytest.raises(ValueError, match="does not carry"):
        IsolatedRefusal(R.HISTORY_GAP, ITEM, 3, section="campaign-audiences", rows_read=1)


@pytest.mark.parametrize("value", [-1, 2**31, True, 1.5, "7"])
def test_a_figure_outside_0_to_2_31_or_not_an_integer_raises(value: object) -> None:
    with pytest.raises(ValueError, match="integer in 0 <= value < 2\\*\\*31"):
        IsolatedRefusal(R.ROWS_OVER_CEILING, PAGE, rows_read=cast("Any", value))
    # Paired: the bounds themselves construct.
    IsolatedRefusal(R.ROWS_OVER_CEILING, PAGE, rows_read=0, max_rows=2**31 - 1)


@pytest.mark.parametrize("code", ["ADLY-fictional-0001", "ATHENA-2", "ADLY-ATHENA-2", 7])
def test_an_error_code_outside_the_taxonomy_raises(code: object) -> None:
    with pytest.raises(ValueError, match="ADLY-\\* taxonomy code"):
        IsolatedRefusal(R.SOURCE_REFUSED, PAGE, error_code=cast("Any", code))
    # Paired: a taxonomy code, including an area with a digit, constructs.
    IsolatedRefusal(R.SOURCE_REFUSED, PAGE, error_code="ADLY-GRAPH_BAKE_V3-0010")


@pytest.mark.parametrize("count", [0, -2, True])
def test_a_refusal_refuses_at_least_one_item(count: object) -> None:
    with pytest.raises(ValueError, match="at least one item"):
        IsolatedRefusal(R.HISTORY_GAP, ITEM, cast("Any", count), section="campaign-audiences")
    assert IsolatedRefusal(R.HISTORY_GAP, ITEM, 200, section="campaign-audiences").count == 200


def test_a_page_refusal_names_no_section_and_a_closed_reason_and_scope_are_required() -> None:
    with pytest.raises(ValueError, match="names no section"):
        IsolatedRefusal(R.HISTORY_GAP, PAGE, section="campaign-audiences")
    with pytest.raises(ValueError, match="closed IsolatedRefusalReason"):
        IsolatedRefusal(cast("Any", "history_gap"), PAGE)
    with pytest.raises(ValueError, match="closed RefusalScope"):
        IsolatedRefusal(R.HISTORY_GAP, cast("Any", "page"))


# --- classify_refusal ----------------------------------------------------------------------


def test_the_partition_bound_codes_are_the_selectors_own() -> None:
    assert frozenset({SELECTION_TOO_LARGE, PACK_SNAPSHOT_TOO_LARGE}) == PARTITION_BOUND_CODES


@pytest.mark.parametrize("code", [SELECTION_TOO_LARGE, PACK_SNAPSHOT_TOO_LARGE])
def test_a_partition_bound_is_partitions_over_bound_with_its_deciding_figures(
    code: ErrorCode,
) -> None:
    error = ValidationError(
        code,
        "too many",
        context={"scope": "pack-6", "required_partitions": 184, "maximum_partitions": 100},
    )
    refusal = classify_refusal(error)
    assert (refusal.reason, refusal.scope) == (R.PARTITIONS_OVER_BOUND, PAGE)
    # The figures that decided it, and nothing else (the pack scope is dropped).
    assert dict(refusal.refusal_context) == {"maximum_partitions": 100, "required_partitions": 184}


def test_any_other_source_refusal_is_source_refused_with_its_code() -> None:
    error = DomainError(SELECTION_FAILED, "no window", context={"required_partitions": 3})
    refusal = classify_refusal(error)
    assert (refusal.reason, refusal.scope) == (R.SOURCE_REFUSED, PAGE)
    assert dict(refusal.refusal_context) == {"error_code": "ADLY-PUBLICATION_SELECTION-0001"}
    roster = ValidationError(ErrorCode("PLAN", 8), "Campaign names can't be read")
    assert dict(classify_refusal(roster).refusal_context) == {"error_code": "ADLY-PLAN-0008"}


def test_a_typed_refusal_passes_through_classification() -> None:
    typed = IsolatedRefusal(R.ROWS_OVER_CEILING, PAGE, rows_read=5, max_rows=4)
    assert classify_refusal(typed) is typed


# --- the scope check -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("page", "section"),
    [
        ("reach", "frequency"),
        ("new-customers", "new-to-brand-step-2"),
        ("audiences", "campaign-audiences"),
    ],
)
def test_a_section_claim_on_a_non_anchor_section_is_accepted(page: str, section: str) -> None:
    refusal = IsolatedRefusal(R.ROWS_OVER_CEILING, SECTION, section=section, rows_read=2)
    assert check_refusal_scope(refusal, page) is refusal


@pytest.mark.parametrize(
    ("page", "section"),
    [
        ("reach", "overlap"),  # declares period_anchor: true
        ("new-customers", "gateway-products"),  # declares none, so it is an anchor
        ("scorecard", "frequency"),  # another page's section
    ],
)
def test_a_section_claim_on_an_anchor_or_undeclared_section_is_an_invariant(
    page: str, section: str
) -> None:
    refusal = IsolatedRefusal(R.ROWS_OVER_CEILING, SECTION, section=section)
    with pytest.raises(InvariantError) as raised:
        check_refusal_scope(refusal, page)
    assert raised.value.code == REFUSAL_SCOPE_INVALID


def test_a_profile_path_item_inside_the_anchor_section_is_accepted() -> None:
    path = IsolatedRefusal(R.HISTORY_GAP, ITEM, 2, section="segment-ranking")
    assert check_refusal_scope(path, "audiences") is path
    # Paired: an item claim on a page that holds no items is an invariant.
    with pytest.raises(InvariantError):
        check_refusal_scope(IsolatedRefusal(R.HISTORY_GAP, ITEM, section="frequency"), "reach")
    # And an item or section claim that names no section is one too.
    with pytest.raises(InvariantError):
        check_refusal_scope(IsolatedRefusal(R.HISTORY_GAP, ITEM), "audiences")


# --- groups, their stored form and their metric -------------------------------------------


def test_refusals_group_by_scope_and_reason_summing_counts() -> None:
    groups = group_refusals(
        (
            IsolatedRefusal(R.HISTORY_GAP, ITEM, 3, section="campaign-audiences"),
            IsolatedRefusal(R.HISTORY_GAP, ITEM, 1, section="segment-ranking"),
            IsolatedRefusal(
                R.ROWS_OVER_CEILING, SECTION, section="campaign-audiences", rows_read=7, max_rows=6
            ),
            IsolatedRefusal(
                R.ROWS_OVER_CEILING, SECTION, section="campaign-audiences", rows_read=9, max_rows=6
            ),
        )
    )
    assert [(g.scope, g.reason, g.count, dict(g.context)) for g in groups] == [
        (ITEM, R.HISTORY_GAP, 4, {}),
        # The larger figure of the two reads that decided the group.
        (SECTION, R.ROWS_OVER_CEILING, 2, {"max_rows": 6, "rows_read": 9}),
    ]
    assert group_refusals(()) == ()


def test_a_group_round_trips_through_its_stored_form_and_a_tampered_one_is_refused() -> None:
    (group,) = group_refusals(
        (IsolatedRefusal(R.SOURCE_REFUSED, PAGE, error_code="ADLY-PLAN-0008"),)
    )
    record = group.to_record()
    assert record == {
        "scope": "page",
        "reason": "source_refused",
        "refusal_class": "data",
        "count": 1,
        "context": {"error_code": "ADLY-PLAN-0008"},
    }
    assert RefusalGroup.from_record(record) == group
    for tampered in (
        {**record, "context": {"error_code": "ADLY-PLAN-0008", "campaign_id": "x"}},
        {**record, "refusal_class": "limit"},
        {**record, "count": 0},
        {**record, "reason": "fictional"},
        {**record, "context": {"count": 2}},
        {key: value for key, value in record.items() if key != "count"},
        ["not", "an", "object"],
    ):
        with pytest.raises(ValueError):
            RefusalGroup.from_record(tampered)


def test_a_limit_refusal_goes_to_the_limit_metric() -> None:
    over = IsolatedRefusal(
        R.PARTITIONS_OVER_BOUND, PAGE, required_partitions=1_001, maximum_partitions=1_000
    )
    assert over.refusal_class is RefusalClass.LIMIT
    assert isolated_refusal_metric(over.refusal_class.value) == BAKE_ISOLATED_REFUSAL_LIMIT_METRIC
    # Paired: source_refused is data, counted on the data metric, not alarmed.
    refused = IsolatedRefusal(R.SOURCE_REFUSED, PAGE, error_code="ADLY-PLAN-0008")
    assert isolated_refusal_metric(refused.refusal_class.value) == BAKE_ISOLATED_REFUSAL_DATA_METRIC
    assert isolated_refusal_metric("dependency") == BAKE_ISOLATED_REFUSAL_LIMIT_METRIC
    with pytest.raises(ValueError, match="limit, dependency or data"):
        isolated_refusal_metric("defect")


@pytest.mark.covers("ADR-152")
def test_every_limit_reason_names_its_operator_action_and_no_other_reason_does() -> None:
    from domain.operations.bake_refusals import MEMORY_SIZED, OPERATOR_ACTIONS  # noqa: PLC0415

    limits = {reason for reason, kind in REFUSAL_CLASSES.items() if kind is RefusalClass.LIMIT}
    assert set(OPERATOR_ACTIONS) == limits
    assert all(text.strip() for text in OPERATOR_ACTIONS.values())
    assert MEMORY_SIZED == {R.ROWS_OVER_CEILING, R.POPULATION_OVER_CEILING} <= limits
    assert all(
        OPERATOR_ACTIONS[reason].startswith("raise the Baker memory") for reason in MEMORY_SIZED
    )
