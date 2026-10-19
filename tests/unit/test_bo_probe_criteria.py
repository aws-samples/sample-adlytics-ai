# SPDX-License-Identifier: MIT-0
"""P-BO-1 step A criteria and pack decisions on fictional inline results."""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "validation"))
sys.path.insert(0, str(ROOT))

from validation import bo_probe_criteria as c  # noqa: E402
from validation import bo_probe_sql as sql  # noqa: E402

pytestmark = pytest.mark.unit

WEEK = ("2026-01-05T00:00:00", "2026-01-12T00:00:00")
SUP = ("rows_suppressed", "suppression_reason", "distinct_user_count")


def table(
    columns: Sequence[str], rows: Sequence[Mapping[str, object]], *, window: tuple[str, str] = WEEK
) -> c.Parsed:
    """Build a parsed result from fictional rows; unspecified cells are empty."""
    header = (*columns, *SUP)
    lines = [",".join(header)]
    for row in rows:
        cells = {"time_window_start": window[0], "time_window_end": window[1], **row}
        cells.setdefault("rows_suppressed", "false")
        lines.append(",".join(str(cells.get(name, "")) for name in header))
    return c.parse_result("\n".join(lines) + "\n", columns, window=window, controls_off=False)


Q1C = sql.QUERIES["Q1"].columns


def q1(*rows: tuple[str, str, int] | None, bar: int = 2) -> c.Parsed:
    out: list[dict[str, object]] = []
    for row in rows:
        if row is None:
            out.append({"rows_suppressed": "true", "reach_users": 1, "distinct_user_count": 1})
        else:
            out.append(
                {"campaign_id_string": row[0], "event_date_utc": row[1], "reach_users": row[2],
                 "distinct_user_count": bar}
            )  # fmt: skip
    return table(Q1C, out)


def q2(*rows: tuple[str, str, int]) -> c.Parsed:
    return table(
        Q1C,
        [{"campaign_id_string": a, "event_date_utc": d, "reach_users": n} for a, d, n in rows],
    )


def outcome(results: Sequence[c.CriterionResult], criterion: str) -> str:
    return {item.id: item.outcome for item in results}[criterion]


# --- parse and sanity ---------------------------------------------------------------------------


def test_sanity_flags_header_window_and_sensitive_columns() -> None:
    good = q2(("cmp-fixture-a", "2026-01-05", 3))
    assert good.defects == ()
    bad_window = table(Q1C, [{"reach_users": 3}], window=("2026-01-06T00:00:00", WEEK[1]))
    reparsed = c.parse_result(
        "campaign_id_string,user_id\nx,y\n", Q1C, window=WEEK, controls_off=False
    )
    assert any("time_window_start" in d for d in bad_window.defects) is False
    assert any("sensitive" in d for d in reparsed.defects)
    assert any("header" in d for d in reparsed.defects)


# --- source-column half -------------------------------------------------------------------------


def test_source_half_passes_when_exercised_and_exact() -> None:
    on = q1(("cmp-fixture-a", "2026-01-05", 4), None)
    off = q2(("cmp-fixture-a", "2026-01-05", 4), ("cmp-fixture-b", "2026-01-05", 1))
    gate, results = c.source_half(c.Inputs({"Q1": on, "Q2": off}))
    assert gate == c.PASS
    assert [outcome(results, k) for k in ("0", "1", "2", "4", "3")] == [c.PASS] * 5


def test_source_half_fails_on_inexact_count_and_named_suppressed_row() -> None:
    on = table(
        Q1C,
        [
            {
                "campaign_id_string": "cmp-fixture-a",
                "event_date_utc": "2026-01-05",
                "reach_users": 5,
            },
            {"campaign_id_string": "cmp-fixture-b", "rows_suppressed": "true"},
        ],
    )
    off = q2(("cmp-fixture-a", "2026-01-05", 4), ("cmp-fixture-b", "2026-01-05", 1))
    gate, results = c.source_half(c.Inputs({"Q1": on, "Q2": off}))
    assert gate == c.FAIL
    assert outcome(results, "1") == c.FAIL
    assert outcome(results, "2") == c.FAIL


def test_source_half_absent_grain_fails_criterion_4() -> None:
    on = q1(("cmp-fixture-z", "2026-01-05", 3), None)
    off = q2(("cmp-fixture-a", "2026-01-05", 1))
    _, results = c.source_half(c.Inputs({"Q1": on, "Q2": off}))
    assert outcome(results, "4") == c.FAIL


def test_criterion_0_escalates_to_the_hourly_rerun() -> None:
    on = q1(("cmp-fixture-a", "2026-01-05", 4))
    off = q2(("cmp-fixture-a", "2026-01-05", 4))
    gate, results = c.source_half(c.Inputs({"Q1": on, "Q2": off}))
    assert gate == c.INCONCLUSIVE
    assert outcome(results, "0") == c.INCONCLUSIVE
    cols = sql.QUERIES["Q1r"].columns
    q1r = table(
        cols,
        [
            {"campaign_id_string": "cmp-fixture-a", "event_date_utc": "2026-01-05",
             "event_dt_hour_utc": "2026-01-05 03:00:00", "reach_users": 3},
            {"rows_suppressed": "true", "reach_users": 1},
        ],
    )  # fmt: skip
    q2r = table(
        cols,
        [
            {"campaign_id_string": "cmp-fixture-a", "event_date_utc": "2026-01-05",
             "event_dt_hour_utc": "2026-01-05 03:00:00", "reach_users": 3},
            {"campaign_id_string": "cmp-fixture-a", "event_date_utc": "2026-01-05",
             "event_dt_hour_utc": "2026-01-05 04:00:00", "reach_users": 1},
        ],
    )  # fmt: skip
    gate, results = c.source_half(c.Inputs({"Q1": on, "Q2": off, "Q1r": q1r, "Q2r": q2r}))
    assert gate == c.PASS
    zero = next(item for item in results if item.id == "0")
    assert zero.detail["rerun"] == 1


def test_criterion_3_fails_when_a_two_user_cell_is_withheld() -> None:
    on = q1(None)
    off = q2(("cmp-fixture-a", "2026-01-05", 2))
    assert c.criterion_3(on, off, c.Q1_KEYS).outcome == c.FAIL


# --- derived half -------------------------------------------------------------------------------


P5 = sql.PACK5_COLUMNS


def pack5_row(  # noqa: PLR0913 -- fixture builder
    group: str, space: str, reached: int, *, purchasers: int = 1, **extra: object
) -> dict[str, object]:
    return {
        "exposure_source_set": group, "identity_space": space, "shoppers_reached": reached,
        "branded_search_shoppers": 0, "detail_page_view_shoppers": 0, "purchasers": purchasers,
        "ntb_purchasers": 0, "distinct_user_count": 2, **extra,
    }  # fmt: skip


def diag(label: str, value: int) -> dict[str, object]:
    return {"exposure_source_set": label, "identity_space": "all", "shoppers_reached": value}


GROUPS = [
    pack5_row("dsp", "account", 5),
    pack5_row("dsp", "device_or_browser", 3),
    pack5_row("sponsored", "account", 7),
    pack5_row("sponsored", "device_or_browser", 2),
    pack5_row("dsp+sponsored", "account", 4),
    pack5_row("dsp+sponsored", "device_or_browser", 1),
]


def q4(**diagnostics: int) -> c.Parsed:
    values = {
        "fence_total": 22, "diag_pre_only_shoppers": 2, "diag_pre_only_counted": 0,
        "diag_late_dsp_shoppers": 1, "diag_late_dsp_in_dsp_groups": 0,
        "diag_late_dsp_in_sponsored": 1, "diag_multi_hour_shoppers": 6,
        "diag_sign_check": 0, "diag_sign_check_mirror": 0, **diagnostics,
    }  # fmt: skip
    return table(P5, [*GROUPS, *(diag(k, v) for k, v in values.items())])


def q3() -> c.Parsed:
    return table(P5, [*GROUPS[:5], {"rows_suppressed": "true", "distinct_user_count": 1}])


P6 = sql.PACK6_COLUMNS


def q5_suppressed() -> c.Parsed:
    return table(P6, [{"rows_suppressed": "true", "distinct_user_count": 1}])


def test_criterion_5_matches_q3_against_q4_group_rows_only() -> None:
    gate, results = c.derived_sub_gate_1(c.Inputs({"Q3": q3(), "Q4": q4(), "Q5": q5_suppressed()}))
    assert outcome(results, "0b") == c.PASS
    assert outcome(results, "5") == c.PASS
    assert gate == c.INCONCLUSIVE  # 5b not run without Q6


def test_criterion_5_is_inconclusive_without_the_0b_exercise() -> None:
    gate, results = c.derived_sub_gate_1(c.Inputs({"Q3": q3(), "Q4": q4()}))
    five = next(item for item in results if item.id == "5")
    assert five.outcome == c.INCONCLUSIVE
    assert five.detail["unsuppressed_rows_matched"] is True
    assert gate == c.INCONCLUSIVE


def test_source_half_unexercised_matches_are_not_a_pass() -> None:
    on = q1(("cmp-fixture-a", "2026-01-05", 4))
    off = q2(("cmp-fixture-a", "2026-01-05", 4))
    _, results = c.source_half(c.Inputs({"Q1": on, "Q2": off}))
    by_id = {item.id: item for item in results}
    assert by_id["1"].outcome == by_id["4"].outcome == c.INCONCLUSIVE
    assert by_id["1"].detail["unsuppressed_rows_matched"] is True


def test_pairs_from_different_instances_are_not_compared() -> None:
    inputs = c.Inputs(
        {"Q3": q3(), "Q4": q4(), "Q5": q5_suppressed()},
        instances={"Q3": "STANDARD:instance-fixture-b", "Q4": "SANDBOX:instance-fixture-a",
                   "Q5": "STANDARD:instance-fixture-b"},
    )  # fmt: skip
    _, results = c.derived_sub_gate_1(inputs)
    five = next(item for item in results if item.id == "5")
    assert five.outcome == c.INCONCLUSIVE
    assert five.detail == {"not_run": True, "instance_mismatch": True}
    seven = c.criterion_7(inputs, bar_pack5=2, bar_pack14=2)
    assert seven.detail["pack5"] == "instance_mismatch"
    same = c.Inputs(
        {"Q3": q3(), "Q4": q4()},
        instances={"Q3": "SANDBOX:instance-fixture-a", "Q4": "SANDBOX:instance-fixture-a"},
    )
    assert same.pair("Q3", "Q4") is not None


def test_criterion_6_sums_groups_to_fence_total() -> None:
    assert c.criterion_6(q4()).outcome == c.PASS
    assert c.criterion_6(q4(fence_total=23)).outcome == c.FAIL


def test_criterion_8_ac_e15() -> None:
    assert c.criterion_8(q4()).outcome == c.PASS
    assert c.criterion_8(q4(diag_pre_only_counted=1)).outcome == c.FAIL
    assert c.criterion_8(q4(diag_late_dsp_shoppers=0, diag_late_dsp_in_sponsored=0)).outcome == (
        c.INCONCLUSIVE
    )


def q0(first: int, second: int, gap: int) -> c.Parsed:
    return table(
        sql.Q0_COLUMNS, [{"earlier_later": first, "later_earlier": second, "gap": gap, "users": 9}]
    )


@pytest.mark.parametrize(
    ("pair", "convention", "signs"),
    [
        ((3600, -3600), "t2_minus_t1", (6, 0)),
        ((-3600, 3600), "t1_minus_t2", (0, 6)),
        ((3600, 3600), "absolute", (0, 0)),
    ],
)
def test_criterion_8b_reads_each_convention(
    pair: tuple[int, int], convention: str, signs: tuple[int, int]
) -> None:
    result = c.criterion_8b(
        q0(*pair, 3600), q4(diag_sign_check=signs[0], diag_sign_check_mirror=signs[1])
    )
    assert result.outcome == c.PASS
    assert result.detail["convention"] == convention
    assert result.detail["cross_check"] == "agree"


def test_criterion_8b_bad_gap_and_cross_check_disagreement() -> None:
    assert c.criterion_8b(q0(3600, -3600, 0), None).outcome == c.FAIL
    assert c.criterion_8b(q0(60, -60, 60), None).outcome == c.FAIL
    disagree = c.criterion_8b(
        q0(3600, -3600, 3600), q4(diag_sign_check=0, diag_sign_check_mirror=6)
    )
    assert disagree.outcome == c.INCONCLUSIVE
    assert disagree.detail["defect"] is True
    assert c.criterion_8b(None, None).outcome == c.INCONCLUSIVE


def test_criterion_8_pending_only_after_8b_passed() -> None:
    quiet = q4(diag_late_dsp_shoppers=0, diag_late_dsp_in_sponsored=0, diag_sign_check=6)
    passed = c.evaluate_gates(c.Inputs({"Q0": q0(3600, -3600, 3600), "Q4": quiet, "Q4m": quiet}))
    assert passed.outcome("8") == c.PENDING
    failed = c.evaluate_gates(c.Inputs({"Q0": q0(1, 1, 1), "Q4": quiet, "Q4m": quiet}))
    assert failed.outcome("8") == c.INCONCLUSIVE


@pytest.mark.parametrize(("bar", "expected"), [(2, 2), (100, 100), (37, None)])
def test_criterion_11_bars(bar: int, expected: int | None) -> None:
    parsed = table(P5, [pack5_row("dsp", "account", 5, distinct_user_count=bar)])
    result = c.criterion_11(parsed, label="Q3")
    assert result.detail["bar"] == expected
    assert result.outcome == (c.PASS if expected else c.INCONCLUSIVE)


def test_criterion_11_without_unsuppressed_rows_is_inconclusive() -> None:
    parsed = table(P5, [{"rows_suppressed": "true", "distinct_user_count": 1}])
    result = c.criterion_11(parsed, label="Q3")
    assert result.outcome == c.INCONCLUSIVE
    assert c.bar_of(result) == c.ESCALATED_BAR


def test_criterion_7_reads_the_escalated_bar() -> None:
    small = table(P5, [pack5_row("dsp", "account", 50, purchasers=0)])
    withheld = table(P5, [{"rows_suppressed": "true"}])
    inputs = c.Inputs({"Q3": withheld, "Q4": small})
    assert c.criterion_7(inputs, bar_pack5=2, bar_pack14=2).outcome == c.FAIL
    assert c.criterion_7(inputs, bar_pack5=100, bar_pack14=2).outcome == c.INCONCLUSIVE


P14 = sql.PACK14_COLUMNS


def cohort_row(  # noqa: PLR0913 -- fixture builder
    month: str,
    k: int,
    group: str,
    *,
    size: int,
    **extra: object,
) -> dict[str, object]:
    return {"cohort_month": month, "months_since_acquisition": k, "acquisition_source_set": group,
            "cohort_buyers_new_to_brand_12mo": size, **extra}  # fmt: skip


def test_criterion_7_never_passes_on_one_half() -> None:
    cell = cohort_row("2026-01-01", 1, "dsp", size=5, repeat_buyers=1, distinct_user_count=2)
    inputs = c.Inputs({"Q7": table(P14, [cell]), "Q8": table(P14, [cell])})
    seven = c.criterion_7(inputs, bar_pack5=2, bar_pack14=2)
    assert seven.outcome == c.INCONCLUSIVE
    assert seven.detail["pack14"] == {"cells": 1, "violations": 0, "bar": 2}
    assert seven.detail["pack5"] == "not_run"
    withheld = c.Inputs({"Q7": table(P14, [{"rows_suppressed": "true"}]), "Q8": table(P14, [cell])})
    assert c.criterion_7(withheld, bar_pack5=2, bar_pack14=2).outcome == c.FAIL
    both = c.Inputs({**inputs.results, "Q3": q3(), "Q4": q4()})
    assert c.criterion_7(both, bar_pack5=100, bar_pack14=2).outcome == c.PASS


def test_criterion_5c_is_inconclusive_until_0c_is_exercised() -> None:
    cell = cohort_row("2026-01-01", 1, "dsp", size=5, repeat_buyers=1)
    total = cohort_row("2026-01-01", 0, "cohort_total", size=5)
    reference = table(P14, [cell, total])
    gate, results = c.derived_sub_gate_2(c.Inputs({"Q7": table(P14, [cell]), "Q8": reference}))
    assert outcome(results, "0c") == c.INCONCLUSIVE
    five_c = next(item for item in results if item.id == "5c")
    assert five_c.outcome == c.INCONCLUSIVE
    assert five_c.detail["unsuppressed_rows_matched"] is True
    assert gate == c.INCONCLUSIVE
    exercised = table(P14, [cell, {"rows_suppressed": "true"}])
    gate, results = c.derived_sub_gate_2(c.Inputs({"Q7": exercised, "Q8": reference}))
    assert outcome(results, "5c") == c.PASS
    assert gate == c.PASS


def test_criterion_9_repeated_total_and_cohort_total() -> None:
    rows = [
        cohort_row("2026-01-01", 1, "dsp", size=3),
        cohort_row("2026-01-01", 2, "dsp", size=3),
        cohort_row("2026-01-01", 1, "sponsored", size=5),
        cohort_row("2026-01-01", 0, "cohort_total", size=8),
    ]
    assert c.criterion_9(table(P14, rows)).outcome == c.PASS
    missing_group = [*rows[:3], cohort_row("2026-01-01", 0, "cohort_total", size=9)]
    assert c.criterion_9(table(P14, missing_group)).outcome == c.FAIL
    repeated = [cohort_row("2026-01-01", 2, "dsp", size=4), *rows[:1], *rows[2:]]
    assert c.criterion_9(table(P14, repeated)).outcome == c.FAIL


Q9C = sql.Q9_COLUMNS


def q9_rows(cohort: str, months: Sequence[tuple[str, int]]) -> c.Parsed:
    return table(
        Q9C, [{"cohort_month": cohort, "event_month": e, "k": k, "shoppers": 2} for e, k in months]
    )


def test_criterion_10_ignores_pre_cohort_rows_and_needs_a_year_crossing() -> None:
    crossing = [(f"2025-{m:02d}-01", m - 9) for m in range(10, 13)]
    crossing += [(f"2026-{m:02d}-01", m + 3) for m in range(1, 4)]
    crossing += [("2025-07-01", 99)]  # pre-cohort: ignored under decision 6
    result = c.criterion_10(q9_rows("2025-09-01", crossing))
    assert result.outcome == c.PASS
    assert result.detail["pre_cohort"] == 1
    same_year = [(f"2025-{m:02d}-01", m - 1) for m in range(2, 9)]
    assert c.criterion_10(q9_rows("2025-01-01", same_year)).outcome == c.INCONCLUSIVE
    wrong = [*crossing[:-1], ("2026-04-01", 6)]
    assert c.criterion_10(q9_rows("2025-09-01", wrong)).outcome == c.FAIL


P7 = sql.PACK7_COLUMNS


def credit(model: str, value: str) -> dict[str, object]:
    return {"row_kind": "credit", "attribution_model_key": model, "credited_purchases": value}


def test_criterion_6b_tolerance_and_double_count() -> None:
    total = {"row_kind": "credit_total", "credited_purchases": "3"}
    good = [
        credit("first_touch_v1", "3"),
        credit("last_touch_v1", "3"),
        credit("linear_v1", "2.9999995"),
    ]
    assert c.criterion_6b(table(P7, [*good, total])).outcome == c.PASS
    over = [credit("first_touch_v1", "4"), credit("last_touch_v1", "4"), credit("linear_v1", "4")]
    assert c.criterion_6b(table(P7, [*over, total])).outcome == c.FAIL
    assert c.criterion_6b(table(P7, good)).outcome == c.INCONCLUSIVE


# --- pack decisions -----------------------------------------------------------------------------


def gates(source: str = c.PASS, one: str = c.PASS, two: str = c.PASS, **outcomes: str) -> c.Gates:
    defaults = {
        "8b": c.PASS,
        "8": c.PASS,
        "6": c.PASS,
        "6b": c.PASS,
        "9": c.PASS,
        "10": c.PASS,
        "7": c.PASS,
    }
    criteria = {
        key.replace("_", "."): c.CriterionResult(key.replace("_", "."), value, exercised=True)
        for key, value in {**defaults, **outcomes}.items()
    }
    for label in ("Q3", "Q5", "Q7"):
        criteria[f"11.{label}"] = c.CriterionResult(f"11.{label}", c.PASS, True, {"bar": 2})
    return c.Gates(source, one, two, criteria)


def test_decide_packs_as_designed() -> None:
    decisions = c.decide_packs(gates())
    assert all(decision.policy == c.DISCARD for decision in decisions.values())
    assert decisions[5].expected_bar == "2"
    assert decisions[1].expected_bar == "first_live_run"
    assert decisions[14].blockers == ()


def test_decide_packs_source_passes_derived_inconclusive() -> None:
    decisions = c.decide_packs(gates(one=c.INCONCLUSIVE, two=c.INCONCLUSIVE))
    assert decisions[1].policy == c.DISCARD
    assert decisions[5].policy == c.REJECT
    assert decisions[14].policy == c.REJECT


def test_decide_packs_sql_blockers_and_fallbacks() -> None:
    decisions = c.decide_packs(
        gates(**{"8b": c.FAIL, "6": c.FAIL, "6b": c.INCONCLUSIVE, "10": c.FAIL})
    )
    assert "8b" in decisions[5].blockers and "6" in decisions[5].blockers
    assert "8b" in decisions[14].blockers and "10" in decisions[14].blockers
    assert any("Not available per channel" in note for note in decisions[7].notes)
    pending = c.decide_packs(gates(**{"8": c.PENDING}))
    assert pending[5].blockers == ()
    assert any("pending" in note for note in pending[5].notes)
    # Without the Q4m re-run, an inconclusive AC-E1.5 stays a blocker even after 8b passed.
    unrun = c.decide_packs(gates(**{"8": c.INCONCLUSIVE}))
    assert all(unrun[pack].blockers == ("8",) for pack in (5, 6, 7))
    assert not any("pending" in note for note in unrun[6].notes)
    per_source = c.decide_packs(gates(**{"7": c.FAIL}))
    assert per_source[5].policy == c.DISCARD
    assert any("quarterly" in note for note in per_source[14].notes)


def test_decide_packs_fail_and_escalated_bar() -> None:
    decisions = c.decide_packs(gates(source=c.FAIL, one=c.FAIL))
    assert decisions[1].policy == c.REJECT and decisions[5].policy == c.REJECT
    escalated = gates()
    escalated_criteria = dict(escalated.criteria)
    escalated_criteria["11.Q3"] = c.CriterionResult("11.Q3", c.PASS, True, {"bar": 100})
    result = c.decide_packs(c.Gates(c.PASS, c.PASS, c.PASS, escalated_criteria))
    assert result[5].expected_bar == "100"
    assert result[10].expected_bar == "100"


def test_widened_integer_grain_still_matches() -> None:
    on = table(P14, [cohort_row("2026-01-01", 1, "dsp", size=3)])
    off = table(
        P14,
        [
            {
                **cohort_row("2026-01-01", 1, "dsp", size=3),
                "months_since_acquisition": "1.0000000000",
            }
        ],
    )
    result = c.exactness(on.rows, off.rows, c.PACK14_KEYS, counts=c.PACK14_COUNTS)
    assert (result.checked, result.absent) == (1, 0)
