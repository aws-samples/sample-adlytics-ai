# SPDX-License-Identifier: MIT-0
"""The shopper-aggregate foundation (ADR-099, ADR-101; design §4.3 to §4.5, slice S0.3).

Every delivery, fact and metric here is synthetic and built in memory; no pack, contract or fact
of the family exists yet. Each refusal asserts its exact signal and the layer that raised it,
beside a paired positive control.
"""

from __future__ import annotations

# `_validate_semantic_axes` is the planner's axis check, exercised directly with a synthetic
# shopper policy so the D-9 refusal is proven where every consumer plans.
# pyright: reportPrivateUsage=false
import json
import re
from dataclasses import replace
from datetime import date
from decimal import Decimal
from itertools import product
from pathlib import Path

import pytest

from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.curation.manifests import (
    BuildManifestRequest,
    build_manifest,
    checksum_of,
    declared_columns_of,
)
from adapters.jobs.curation.readers import DeliveredFile, read
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import GRAIN_NOT_SUPPORTED_CODE, Catalog, Metric
from domain.curation.assertions import control_total_measures, measure_columns
from domain.curation.ingestion import split_withheld
from domain.curation.landing import LandedObject, Manifest
from domain.curation.mapping import MappingRequest, map_rows
from domain.data_contracts.contract import DataContract, FieldMapping, NullMeaning
from domain.measurement.planning import PlanRequest, _validate_semantic_axes
from domain.semantics.compatibility import (
    DISJOINT_AXES,
    AggregationKind,
    AggregationOperation,
    AggregationPolicy,
    AxisBehavior,
    AxisPolicy,
    SemanticAxis,
    SemanticCompatibilityError,
)
from domain.shared_kernel.errors import DomainError
from domain.shared_kernel.values import DateRange
from domain.shopper_aggregates import (
    CELL_STATE_COLUMN,
    DEGRADED_NOTE,
    BarRequest,
    BarVerdict,
    CellState,
    ClosedEnumeration,
    DisjointCell,
    DisjointSumRefusalError,
    DisjointSumRequest,
    EnumerationAxis,
    EnumerationRequest,
    GrainSet,
    LossGrade,
    LossRequest,
    LossScope,
    RepeatedTotal,
    RepeatedTotalRequest,
    ServedGrainSets,
    ShopperAggregateContractError,
    ShopperPolicyRequest,
    check_bar,
    check_repeated_totals,
    fill_closed_enumeration,
    grade_loss,
    shopper_aggregate_policy,
    sum_disjoint,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-099", "ADR-101", "ADR-087")]

ROOT = Path(__file__).resolve().parents[2]

IDENTITY = EnumerationAxis("identity_space", ("account", "device_or_browser"))
SOURCE_SET = EnumerationAxis("exposure_source_set", ("dsp", "sponsored", "dsp+sponsored"))
BUCKETS = EnumerationAxis(
    "frequency_bucket", ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10_plus")
)
PACK_5 = ClosedEnumeration(cells=(SOURCE_SET,), slices=(IDENTITY,))
PACK_7 = ClosedEnumeration(cells=(BUCKETS,), slices=(IDENTITY,), open_slices=("ad_product_type",))
MEASURES = ("shoppers", "purchases")

NTB = ServedGrainSets(
    "fct_new_to_brand_buyers",
    (
        GrainSet("platform", "the Amazon Ads total"),
        GrainSet("ad_product", "the ad product view"),
        GrainSet("campaign", "the campaign view"),
    ),
)


def _row(**values: object) -> dict[str, object]:
    return {"shoppers": 40, "purchases": 3, **values}


def _pack_5_rows(*, without: tuple[str, str] | None = None) -> tuple[dict[str, object], ...]:
    return tuple(
        _row(identity_space=space, exposure_source_set=group)
        for space, group in product(IDENTITY.values, SOURCE_SET.values)
        if (space, group) != without
    )


# --- not_returned: the null meaning is absence, never zero (C-10) -----------------------------


def test_not_returned_is_a_null_meaning_and_the_data_layer_page_lists_every_meaning() -> None:
    assert NullMeaning.NOT_RETURNED.value == "not_returned"
    page = (ROOT / "docs/capabilities/data-layer/README.md").read_text()
    section = page.split("## 4. Null meanings", 1)[1].split("\n## ", 1)[0]
    listed = set(re.findall(r"^\| `([a-z_]+)` \|", section, flags=re.MULTILINE))
    assert listed == {meaning.value for meaning in NullMeaning}


def test_a_nullable_column_may_declare_not_returned() -> None:
    mapping = FieldMapping(
        source_path="shoppers",
        target_column="shoppers",
        nullable=True,
        null_meaning=NullMeaning.NOT_RETURNED,
        non_additive=True,
    )
    assert mapping.null_meaning is NullMeaning.NOT_RETURNED


def test_a_missing_cell_is_written_not_returned_with_null_measures_never_zero() -> None:
    outcome = fill_closed_enumeration(
        EnumerationRequest(PACK_5, _pack_5_rows(without=("account", "dsp")), MEASURES)
    )
    [gap] = outcome.not_returned
    assert gap == {
        "identity_space": "account",
        "exposure_source_set": "dsp",
        CELL_STATE_COLUMN: "not_returned",
        "shoppers": None,
        "purchases": None,
    }
    returned = [row for row in outcome.rows if row[CELL_STATE_COLUMN] == "returned"]
    assert len(returned) == 5
    assert all(row["shoppers"] == 40 for row in returned)
    slices = {
        dict(item.key)["identity_space"]: (item.expected, item.missing) for item in outcome.slices
    }
    assert slices == {"account": (3, 1), "device_or_browser": (3, 0)}
    # The absent cell is a gap the sum refuses (the meaning is absence, not zero) ...
    cells = tuple(
        DisjointCell(
            "pack5:2026-09:amazon_us:platform",
            str(row["exposure_source_set"]),
            CellState(str(row[CELL_STATE_COLUMN])),
            None if row["shoppers"] is None else Decimal(str(row["shoppers"])),
        )
        for row in outcome.rows
        if row["identity_space"] == "account"
    )
    request = DisjointSumRequest(SemanticAxis.EXPOSURE_GROUP, frozenset(SOURCE_SET.values), cells)
    with pytest.raises(DisjointSumRefusalError) as refused:
        sum_disjoint(request)
    assert refused.value.reason == "not_returned"
    assert (
        str(refused.value) == "exposure_group dsp didn't come back, so the total isn't available."
    )
    # ... and the loss counts it missing, not measured.
    assert grade_loss(LossRequest(3, 1, LossScope.SLICE)).grade is LossGrade.DEGRADED


def test_a_fully_returned_delivery_writes_no_gap() -> None:
    outcome = fill_closed_enumeration(EnumerationRequest(PACK_5, _pack_5_rows(), MEASURES))
    assert outcome.not_returned == ()
    assert {(item.expected, item.missing) for item in outcome.slices} == {(3, 0)}


# --- Closed enumerations: an undeclared or NULL value rejects the delivery ---------------------


def test_an_undeclared_bucket_rejects_the_delivery() -> None:
    rows = (
        _row(identity_space="account", frequency_bucket="11", ad_product_type="sponsored_products"),
    )
    with pytest.raises(ShopperAggregateContractError) as rejected:
        fill_closed_enumeration(EnumerationRequest(PACK_7, rows, MEASURES))
    assert [(f.code, f.detail) for f in rejected.value.findings] == [
        (
            "CELL_VALUE_UNDECLARED",
            "row 0: frequency_bucket = '11' is not one of 1, 2, 3, 4, 5, 6, 7, 8, 9, 10_plus.",
        )
    ]
    # Positive control: the declared top bucket loads.
    accepted = replace_bucket(rows, "10_plus")
    assert fill_closed_enumeration(EnumerationRequest(PACK_7, accepted, MEASURES)).rows


def replace_bucket(
    rows: tuple[dict[str, object], ...], bucket: str
) -> tuple[dict[str, object], ...]:
    return tuple({**row, "frequency_bucket": bucket} for row in rows)


def test_a_null_bucket_on_an_unsuppressed_row_rejects_the_delivery() -> None:
    """Review DS3-2: the bucket CASE has no ELSE, so NULL means the fence leaked."""
    rows = (
        _row(identity_space="account", frequency_bucket=None, ad_product_type="sponsored_products"),
    )
    with pytest.raises(ShopperAggregateContractError) as rejected:
        fill_closed_enumeration(EnumerationRequest(PACK_7, rows, MEASURES))
    assert [(f.code, f.detail) for f in rejected.value.findings] == [
        ("FREQUENCY_BUCKET_NULL", "row 0: frequency_bucket is NULL on an unsuppressed row.")
    ]
    # Paired control: a NULL in another declared column keeps the generic code.
    other = (_row(identity_space=None, frequency_bucket="1", ad_product_type="sponsored_products"),)
    with pytest.raises(ShopperAggregateContractError) as generic:
        fill_closed_enumeration(EnumerationRequest(PACK_7, other, MEASURES))
    assert [f.code for f in generic.value.findings] == ["CELL_VALUE_NULL"]


def test_a_repeated_cell_rejects_the_delivery() -> None:
    rows = (*_pack_5_rows(), _row(identity_space="account", exposure_source_set="dsp"))
    with pytest.raises(ShopperAggregateContractError) as rejected:
        fill_closed_enumeration(EnumerationRequest(PACK_5, rows, MEASURES))
    assert [f.code for f in rejected.value.findings] == ["CELL_DUPLICATE"]


def test_pack_7_applies_the_enumeration_to_each_ad_product_that_returned_a_row() -> None:
    rows = tuple(
        _row(
            identity_space="account", frequency_bucket=bucket, ad_product_type="sponsored_products"
        )
        for bucket in BUCKETS.values
    )
    outcome = fill_closed_enumeration(EnumerationRequest(PACK_7, rows, MEASURES))
    slices = {
        (dict(item.key)["identity_space"], dict(item.key)["ad_product_type"]): item.missing
        for item in outcome.slices
    }
    # The device slice of the returned ad product is expected in full; an ad product with no
    # row at all is the roster's job at bake, so it has no slice here.
    assert slices == {
        ("account", "sponsored_products"): 0,
        ("device_or_browser", "sponsored_products"): 10,
    }


# --- Loss grading: the four §4.4 unit cases -----------------------------------------------------


def test_case_1_a_pack_14_delivery_with_only_sponsored_rows_is_not_degraded() -> None:
    """The dsp and dsp+sponsored cohort-groups are absent cohorts, outside the denominator."""
    pack_14 = ClosedEnumeration(
        cells=(EnumerationAxis("months_since_acquisition", tuple(str(k) for k in range(1, 13))),),
        slices=(EnumerationAxis("acquisition_source_set", ("dsp", "sponsored", "dsp+sponsored")),),
        cohorts=(EnumerationAxis("cohort_month", ("2025-09-01", "2025-10-01")),),
    )
    rows = tuple(
        _row(
            acquisition_source_set="sponsored", cohort_month=month, months_since_acquisition=str(k)
        )
        for month in ("2025-09-01", "2025-10-01")
        for k in range(1, 13)
    )
    outcome = fill_closed_enumeration(EnumerationRequest(pack_14, rows, MEASURES))
    grades = {
        dict(item.key)["acquisition_source_set"]: (
            item.expected,
            len(item.absent_cohorts),
            grade_loss(LossRequest(item.expected, item.missing, LossScope.SLICE)).grade,
        )
        for item in outcome.slices
    }
    assert grades == {
        "dsp": (0, 2, LossGrade.NOT_DEGRADED),
        "sponsored": (24, 0, LossGrade.NOT_DEGRADED),
        "dsp+sponsored": (0, 2, LossGrade.NOT_DEGRADED),
    }
    assert outcome.not_returned == ()


def test_case_2_a_segment_roster_with_30_percent_churn_is_graded_not_withdrawn() -> None:
    loss = grade_loss(LossRequest(expected=40, missing=12, scope=LossScope.ROSTER))
    assert (loss.grade, loss.pointer, loss.note) == (LossGrade.DEGRADED, None, DEGRADED_NOTE)


def test_case_3_a_24_campaign_roster_at_25_percent_loss_is_withdrawn_and_points_to_pack_2() -> None:
    coarser = NTB.coarser_served("campaign")
    loss = grade_loss(LossRequest(24, 6, LossScope.ROSTER, coarser))
    assert loss.grade is LossGrade.WITHDRAWN
    assert loss.pointer == GrainSet("ad_product", "the ad product view")
    assert loss.note == "too few shoppers in this breakdown; see the ad product view"
    # Paired control: the coarsest grain set has nowhere to point, so it is only graded.
    assert NTB.coarser_served("platform") is None
    assert grade_loss(LossRequest(24, 6, LossScope.ROSTER, None)).grade is LossGrade.DEGRADED


def test_case_4_a_5_entry_ad_product_roster_missing_one_is_graded_not_withdrawn() -> None:
    """AC-B1.5, review DS6-3: 20% loss on fewer than 20 cells never withdraws."""
    loss = grade_loss(LossRequest(5, 1, LossScope.ROSTER, NTB.coarser_served("ad_product")))
    assert loss.grade is LossGrade.DEGRADED
    assert loss.pointer is None


@pytest.mark.parametrize(
    ("expected", "missing", "grade"),
    [
        (20, 5, LossGrade.WITHDRAWN),  # 25% over 20 cells
        (20, 4, LossGrade.DEGRADED),  # exactly 20% doesn't exceed the threshold
        (20, 1, LossGrade.NOT_DEGRADED),  # exactly 5% doesn't exceed it either
        (19, 19, LossGrade.DEGRADED),  # under 20 cells is graded whatever the loss
    ],
)
def test_the_thresholds_are_exceeded_not_met(expected: int, missing: int, grade: LossGrade) -> None:
    coarser = NTB.coarser_served("campaign")
    assert grade_loss(LossRequest(expected, missing, LossScope.DELIVERY, coarser)).grade is grade


def test_a_slice_is_never_withdrawn_even_with_a_coarser_grain_set() -> None:
    """Packs 5 to 7: a 6-cell slice with two missing cells keeps the rest (§13)."""
    coarser = NTB.coarser_served("campaign")
    assert grade_loss(LossRequest(6, 2, LossScope.SLICE, coarser)).grade is LossGrade.DEGRADED
    assert grade_loss(LossRequest(40, 40, LossScope.SLICE, coarser)).grade is LossGrade.DEGRADED


# --- Bar check -----------------------------------------------------------------------------


def _bar_rows(*counts: int, suppressed: int = 0) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = [
        {"rows_suppressed": "false", "distinct_user_count": str(count)} for count in counts
    ]
    rows.extend(
        {"rows_suppressed": "true", "distinct_user_count": "100"} for _ in range(suppressed)
    )
    return tuple(rows)


def test_a_delivery_within_its_bar_is_accepted_and_suppressed_rows_are_not_read() -> None:
    outcome = check_bar(BarRequest(_bar_rows(1, 2, 2, suppressed=3), expected_bar=2))
    assert (outcome.verdict, outcome.highest, outcome.finding) == (BarVerdict.ACCEPTED, 2, None)


def test_a_row_above_the_bar_rejects_the_delivery() -> None:
    outcome = check_bar(BarRequest(_bar_rows(2, 5), expected_bar=2))
    assert outcome.verdict is BarVerdict.REJECTED
    assert outcome.finding is not None
    assert (outcome.finding.code, outcome.finding.detail) == (
        "BAR_EXCEEDED",
        "1 unsuppressed row(s) read distinct_user_count above expectedBar 2 (highest 5).",
    )


def test_every_row_at_100_under_a_bar_of_2_is_an_escalation_and_bar_100_accepts_it() -> None:
    escalated = check_bar(BarRequest(_bar_rows(100, 100, 100), expected_bar=2))
    assert escalated.verdict is BarVerdict.ESCALATION
    assert escalated.finding is not None
    assert escalated.finding.code == "BAR_ESCALATION"
    # Paired control: the escalated pack version declares expectedBar 100.
    assert check_bar(BarRequest(_bar_rows(100, 100, 100), 100)).verdict is BarVerdict.ACCEPTED
    # A mix of 100 and 2 is a slipped column, not an escalation.
    assert check_bar(BarRequest(_bar_rows(100, 2), 2)).verdict is BarVerdict.REJECTED


def test_an_unreadable_row_is_refused() -> None:
    outcome = check_bar(BarRequest(({"rows_suppressed": "false", "distinct_user_count": ""},), 2))
    assert outcome.verdict is BarVerdict.REJECTED
    assert outcome.finding is not None
    assert outcome.finding.code == "BAR_UNREADABLE"


# --- Repeated totals ---------------------------------------------------------------------------


COHORT_SIZE = RepeatedTotal("cohort_buyers", ("acquisition_source_set", "cohort_month"))


def test_copies_of_a_repeated_total_must_agree() -> None:
    rows = tuple(
        {
            "acquisition_source_set": "sponsored",
            "cohort_month": "2025-09-01",
            "cohort_buyers": 80,
            "k": k,
        }
        for k in range(1, 4)
    )
    assert check_repeated_totals(RepeatedTotalRequest(rows, (COHORT_SIZE,))) == ()
    gap = {
        "acquisition_source_set": "sponsored",
        "cohort_month": "2025-09-01",
        "cohort_buyers": None,
    }
    assert check_repeated_totals(RepeatedTotalRequest((*rows, gap), (COHORT_SIZE,))) == ()
    differing = (*rows, {**rows[0], "cohort_buyers": 81})
    [finding] = check_repeated_totals(RepeatedTotalRequest(differing, (COHORT_SIZE,)))
    assert (finding.code, finding.detail) == (
        "REPEATED_TOTAL_MISMATCH",
        "cohort_buyers differs across copies at acquisition_source_set=sponsored, "
        "cohort_month=2025-09-01: 80, 81.",
    )


# --- D-9 and D-3: sums only across a disjoint axis, inside one delivery --------------------------


PACK_7_AXES = frozenset(
    {
        SemanticAxis.TIME,
        SemanticAxis.MARKETPLACE,
        SemanticAxis.ECOSYSTEM,
        SemanticAxis.PLATFORM,
        SemanticAxis.AD_PRODUCT,
        SemanticAxis.FREQUENCY_BUCKET,
        SemanticAxis.IDENTITY_SPACE,
    }
)
PACK_5_AXES = (PACK_7_AXES - {SemanticAxis.AD_PRODUCT, SemanticAxis.FREQUENCY_BUCKET}) | {
    SemanticAxis.EXPOSURE_GROUP
}
ONE_DAY = DateRange(date(2026, 9, 1), date(2026, 9, 1))
ONE_MARKETPLACE = {"marketplace_key": "ATVPDKIKX0DER"}


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(ROOT)


def _with_policy(catalog: Catalog, name: str, policy: AggregationPolicy) -> tuple[Catalog, Metric]:
    """Give one real metric a synthetic shopper-aggregate axis policy, in memory."""
    found = catalog.metric(name)
    assert found is not None
    metric = replace(found, aggregation_policy=policy)
    metrics = tuple(metric if item.name == name else item for item in catalog.metrics)
    return (
        Catalog(
            metrics=metrics,
            deferred=catalog.deferred,
            inventory_categories=catalog.inventory_categories,
        ),
        metric,
    )


def test_the_planner_refuses_pack_7_purchases_summed_across_ad_products(catalog: Catalog) -> None:
    """AC-BR.8b, design D-9: one purchase sits in every ad-product window the shopper had."""
    policy = shopper_aggregate_policy(ShopperPolicyRequest(AggregationKind.SUM, PACK_7_AXES))
    shopper_catalog, metric = _with_policy(catalog, "purchases", policy)
    # Positive control: one ad product at a time, summed across buckets and identity spaces.
    per_ad_product = PlanRequest(
        "purchases", ("date", "marketplace", "ad_product"), ONE_DAY, ONE_MARKETPLACE
    )
    _validate_semantic_axes(shopper_catalog, metric, per_ad_product)
    across = PlanRequest("purchases", ("date", "marketplace"), ONE_DAY, ONE_MARKETPLACE)
    with pytest.raises(DomainError) as refused:
        _validate_semantic_axes(shopper_catalog, metric, across)
    assert refused.value.code == GRAIN_NOT_SUPPORTED_CODE
    assert refused.value.context["reason"] == "grain_not_supported"
    assert refused.value.description.startswith(
        "sum: ad_product is exact_only, so it cannot use sum."
    )


def test_pack_5_purchases_sum_across_exposure_groups_and_never_across_periods() -> None:
    policy = shopper_aggregate_policy(ShopperPolicyRequest(AggregationKind.SUM, PACK_5_AXES))
    policy.require_rollup(
        frozenset({SemanticAxis.EXPOSURE_GROUP}), operation=AggregationOperation.SUM
    )
    with pytest.raises(
        SemanticCompatibilityError, match=r"^sum: time is exact_only, so it cannot use sum\.$"
    ):
        policy.require_rollup(frozenset({SemanticAxis.TIME}), operation=AggregationOperation.SUM)
    cells = tuple(
        DisjointCell("pack5:2026-09:amazon_us:platform", group, CellState.RETURNED, Decimal(n))
        for group, n in zip(SOURCE_SET.values, (3, 5, 2), strict=True)
    )
    request = DisjointSumRequest(SemanticAxis.EXPOSURE_GROUP, frozenset(SOURCE_SET.values), cells)
    assert sum_disjoint(request) == Decimal(10)


def test_a_disjoint_sum_never_crosses_deliveries_or_skips_a_value() -> None:
    cells = (
        DisjointCell("pack5:2026-09", "dsp", CellState.RETURNED, Decimal(3)),
        DisjointCell("pack5:2026-10", "sponsored", CellState.RETURNED, Decimal(5)),
        DisjointCell("pack5:2026-09", "dsp+sponsored", CellState.RETURNED, Decimal(2)),
    )
    values = frozenset(SOURCE_SET.values)
    with pytest.raises(DisjointSumRefusalError) as refused:
        sum_disjoint(DisjointSumRequest(SemanticAxis.EXPOSURE_GROUP, values, cells))
    assert refused.value.reason == "not_one_delivery"
    with pytest.raises(DisjointSumRefusalError) as incomplete:
        sum_disjoint(DisjointSumRequest(SemanticAxis.EXPOSURE_GROUP, values, cells[:1]))
    assert incomplete.value.reason == "axis_incomplete"
    with pytest.raises(DisjointSumRefusalError) as not_disjoint:
        sum_disjoint(DisjointSumRequest(SemanticAxis.CAMPAIGN, values, cells))
    assert not_disjoint.value.reason == "axis_not_disjoint"


def test_d3_a_distinct_count_sums_across_identity_spaces_only() -> None:
    """AC-BR.8: shoppers across both identity spaces of one delivery; never across campaigns."""
    axes = PACK_5_AXES | {SemanticAxis.CAMPAIGN}
    policy = shopper_aggregate_policy(ShopperPolicyRequest(AggregationKind.EXACT_DISTINCT, axes))
    policy.require_rollup(
        frozenset({SemanticAxis.IDENTITY_SPACE}), operation=AggregationOperation.SUM
    )
    with pytest.raises(
        SemanticCompatibilityError, match=r"^exact_distinct cannot be rolled up with sum\.$"
    ):
        policy.require_rollup(
            frozenset({SemanticAxis.IDENTITY_SPACE, SemanticAxis.CAMPAIGN}),
            operation=AggregationOperation.SUM,
        )


def test_an_existing_distinct_count_still_refuses_every_roll_up(catalog: Catalog) -> None:
    reach = catalog.metric("reach_users")
    assert reach is not None
    assert reach.aggregation_policy is not None
    assert not DISJOINT_AXES & reach.aggregation_policy.available_axes
    with pytest.raises(
        SemanticCompatibilityError, match=r"^exact_distinct cannot be rolled up with sum\.$"
    ):
        reach.aggregation_policy.require_rollup(
            frozenset({SemanticAxis.CAMPAIGN}), operation=AggregationOperation.SUM
        )


def _axis(behavior: AxisBehavior) -> AxisPolicy:
    return AxisPolicy(aggregation=behavior, groupable=True, filterable=True, alignable=True)


@pytest.mark.parametrize(
    ("axes", "message"),
    [
        (
            {SemanticAxis.CAMPAIGN: AxisBehavior.DISJOINT_WITHIN_EXTRACTION},
            "disjoint_within_extraction is only for disjoint shopper axes, not campaign.",
        ),
        (
            {SemanticAxis.EXPOSURE_GROUP: AxisBehavior.ADDITIVE},
            "exposure_group must be disjoint_within_extraction or exact_only.",
        ),
        (
            {
                SemanticAxis.EXPOSURE_GROUP: AxisBehavior.DISJOINT_WITHIN_EXTRACTION,
                SemanticAxis.CHANNEL: AxisBehavior.ADDITIVE,
            },
            "a sum within one extraction must be exact_only on every other axis; channel is not.",
        ),
    ],
)
def test_the_policy_refuses_a_disjoint_rule_that_could_leave_one_delivery(
    axes: dict[SemanticAxis, AxisBehavior], message: str
) -> None:
    with pytest.raises(SemanticCompatibilityError) as refused:
        AggregationPolicy(
            kind=AggregationKind.SUM,
            available_axes=frozenset(axes),
            axes={axis: _axis(behavior) for axis, behavior in axes.items()},
        )
    assert str(refused.value) == message
    # Paired control: the same axes, with the open one exact_only, are accepted.
    fixed = {
        axis: (
            AxisBehavior.EXACT_ONLY
            if axis not in DISJOINT_AXES
            else AxisBehavior.DISJOINT_WITHIN_EXTRACTION
        )
        for axis in axes
    }
    AggregationPolicy(
        kind=AggregationKind.SUM,
        available_axes=frozenset(fixed),
        axes={axis: _axis(behavior) for axis, behavior in fixed.items()},
    )


# --- Residual exclusion and the control-total skip (ADR-099 point 12) ---------------------------


@pytest.fixture(scope="module")
def contracts() -> dict[str, DataContract]:
    return {contract.contract_id: contract for _, contract in load_contracts(ROOT)}


def test_the_control_total_skips_a_distinct_count(contracts: dict[str, DataContract]) -> None:
    reach = contracts["amc-reach-frequency"]
    registry = load_registry(ROOT)
    assert "reach_users" in measure_columns(reach, registry)
    checked = control_total_measures(reach, registry)
    assert "reach_users" not in checked
    assert checked == measure_columns(reach, registry) - {"reach_users"}
    # Positive control: a contract with no distinct count checks every measure, as before.
    impressions = contracts["amc-dsp-impressions"]
    assert control_total_measures(impressions, registry) == measure_columns(impressions, registry)


TRAPS = ROOT / "tests/fixtures/traps"
RESIDUAL_TRAP = "amc-withheld-residual"


def _delivery(contract: DataContract) -> tuple[Manifest, tuple[dict[str, object], ...]]:
    """Map the committed withheld-residual trap: one named row and one fully nulled row."""
    text = (TRAPS / f"{RESIDUAL_TRAP}.csv").read_text()
    declaration = (TRAPS / f"{RESIDUAL_TRAP}.manifest.json").read_text()
    body = text.encode()
    manifest = build_manifest(
        BuildManifestRequest(
            declaration=json.loads(declaration),
            objects=(
                LandedObject(
                    name="data-0001.csv", byte_count=len(body), checksum=checksum_of(body)
                ),
            ),
            declared_columns=declared_columns_of(text, source_format="csv"),
        )
    )
    source = read(
        DeliveredFile(text=text, source_format=contract.source_format, manifest_text=declaration)
    )
    return manifest, map_rows(MappingRequest(contract=contract, source=source))


def test_the_residual_excludes_a_distinct_count(contracts: dict[str, DataContract]) -> None:
    """Keep a distinct count out of the residual while additive measures still join it.

    Synthetic: the contract marks `impressions` as a distinct count, in memory.
    """
    contract = contracts["amc-dsp-impressions"]
    manifest, rows = _delivery(contract)
    before = split_withheld(contract, manifest, rows).residual
    assert before is not None
    assert str(before.measures["impressions"]) == "457"
    distinct = replace(
        contract,
        fields=tuple(
            replace(field, non_additive=True) if field.target_column == "impressions" else field
            for field in contract.fields
        ),
    )
    # The trap's withheld row has no currency (live shape), so give it one here: an additive
    # money measure joins the residual only beside its currency (AMC-V decision 5).
    priced = tuple({**row, "currency_code": "USD"} for row in rows)
    after = split_withheld(distinct, manifest, priced).residual
    assert after is not None
    assert "impressions" not in after.measures
    assert str(after.measures["spend"]) == "1.3940"


# --- ADR-087, widened: an exact distinct count is typed absent too -----------------------------


def test_ratios_and_exact_distinct_counts_are_typed_absent_additive_metrics_are_not(
    catalog: Catalog,
) -> None:
    def typed(name: str) -> bool:
        metric = catalog.metric(name)
        assert metric is not None
        return metric.absence_is_typed

    assert typed("roas_attributed_14d")
    assert typed("ctr")
    assert typed("reach_users")
    assert not typed("ad_spend")
    assert not typed("purchases")
    assert not typed("impressions")
