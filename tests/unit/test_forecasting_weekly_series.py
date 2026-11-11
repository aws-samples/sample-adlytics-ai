# SPDX-License-Identifier: MIT-0
"""The three-state missing-week rule and context trimming (design §3b, §3d).

A wrong state turns an unknown week into a zero the model learns from, or a gap into a restart
that blanks a product's history; both change a published forecast.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from forecast_series_support import (
    STARTS,
    asin,
    product_rows,
    series,
    target_row,
    traffic_rows,
    without_day,
)

from domain.forecasting.models import WeekState
from domain.forecasting.policy import TARGETS
from domain.forecasting.weekly_series import (
    DayCounts,
    DayStates,
    GapKind,
    classify_days,
    classify_gap_weeks,
    is_active,
    model_context,
    roll_up_weeks,
)

pytestmark = pytest.mark.unit

OBS, ZERO, UNK = WeekState.OBSERVED, WeekState.INFERRED_ZERO, WeekState.UNKNOWN


@pytest.mark.parametrize(
    ("rows", "nulls", "complete", "active", "expected"),
    [
        (7, 0, 7, True, DayStates(7, 0, 0)),  # every day delivered
        (3, 0, 7, True, DayStates(3, 4, 0)),  # no-row covered days of an active product
        (3, 0, 7, False, DayStates(3, 0, 4)),  # ... of an inactive product
        (3, 1, 7, True, DayStates(2, 4, 1)),  # a declared null is unknown, never zero
        (0, 0, 7, True, DayStates(0, 7, 0)),
        (0, 0, 6, True, DayStates(0, 6, 1)),  # an uncovered day is unknown
        (0, 0, 7, False, DayStates(0, 0, 7)),
    ],
)
def test_each_day_takes_the_first_state_of_the_design_table_that_applies(  # noqa: PLR0913, PLR0917 -- parametrized
    rows: int, nulls: int, complete: int, active: bool, expected: DayStates
) -> None:
    assert classify_days(DayCounts(rows, nulls, complete), active=active) == expected


def test_a_row_on_an_uncovered_day_is_refused() -> None:
    with pytest.raises(ValueError, match="complete_days"):
        DayCounts(days_with_row=7, days_with_null=0, complete_days=6)


@pytest.mark.parametrize(
    ("days", "state", "value"),
    [
        (DayStates(7, 0, 0), OBS, Decimal(5)),
        (DayStates(2, 5, 0), OBS, Decimal(5)),  # inferred-zero days add 0
        (DayStates(0, 7, 0), ZERO, Decimal(0)),
        (DayStates(6, 0, 1), UNK, None),  # any unknown day makes the week unknown
    ],
)
def test_weeks_roll_up_to_one_state(
    days: DayStates, state: WeekState, value: Decimal | None
) -> None:
    cell = roll_up_weeks(days, Decimal(5))
    assert (cell.state, cell.value) == (state, value)


def test_activity_is_a_trailing_13_week_window_ending_each_week() -> None:
    observed = [1] + [0] * 20
    assert is_active(observed, 12, 13) is True
    assert is_active(observed, 13, 13) is False


def test_a_set_gap_week_is_unknown_for_every_asin_but_keeps_every_context() -> None:
    """One unlanded day 60 weeks back makes its week unknown for both products, restarting none."""
    day = STARTS[44] + timedelta(days=2)
    rows = [
        row
        for product, value in ((asin(1), 5.0), (asin(2), 2.0))
        for row in product_rows(product, (w for w in range(104) if w != 44), value)
    ]
    # The unlanded day has no row: the gap week holds six delivered days.
    rows += [target_row(asin(n), 44, t, value=5.0, rows=6) for n in (1, 2) for t in TARGETS]
    result = series(rows, sales_coverage=without_day(day))
    assert result.set_gaps[44] is True
    assert sum(result.set_gaps) == 1
    for item in result.series:
        weeks = item.targets["units_ordered"]
        assert weeks.weeks[44].state is UNK
        assert weeks.gaps[44] is GapKind.SET_GAP
        assert weeks.context_start == 0


def test_a_no_row_week_of_an_active_product_is_inferred_zero_and_of_an_inactive_one_unknown() -> (
    None
):
    """Rows stop at week 70: weeks 71 to 82 stay active (inferred zero), week 83 on are unknown."""
    result = series(product_rows(asin(1), range(71), 3.0))
    weeks = result.series[0].targets["units_shipped"].weeks
    assert {weeks[w].state for w in range(71, 83)} == {ZERO}
    assert weeks[71].value == Decimal(0)
    assert {weeks[w].state for w in range(83, 104)} == {UNK}


def test_a_long_product_gap_at_the_end_leaves_an_empty_context() -> None:
    """A delisted product: 21 unknown weeks at the end exceed the 2-week run, so no context."""
    result = series(product_rows(asin(1), range(70), 3.0))
    weeks = result.series[0].targets["units_ordered"]
    assert weeks.context_start == 104


def test_declared_null_weeks_are_unknown_and_a_three_week_product_gap_restarts() -> None:
    rows = [r for r in product_rows(asin(1), range(104), 4.0) if r.week_start not in STARTS[30:33]]
    rows += [target_row(asin(1), w, t, value=None, nulls=7) for w in range(30, 33) for t in TARGETS]
    weeks = series(rows).series[0].targets["units_ordered"]
    assert [weeks.weeks[w].state for w in range(29, 34)] == [OBS, UNK, UNK, UNK, OBS]
    assert weeks.context_start == 33
    # Control: a two-week gap is within the tolerance and keeps the whole context.
    two = [r for r in rows if r.week_start != STARTS[32]]
    two += [target_row(asin(1), 32, t, value=4.0) for t in TARGETS]
    assert series(two).series[0].targets["units_ordered"].context_start == 0


def test_a_set_gap_week_inside_a_product_gap_neither_counts_nor_breaks_the_run() -> None:
    states = [OBS, UNK, UNK, UNK, UNK, OBS]
    run_with_set_gap = classify_gap_weeks(states, [False, False, True, False, False, False])
    assert model_context(states, run_with_set_gap, 2) == 5  # three product-gap weeks: restart
    two_and_set_gap = classify_gap_weeks(
        [OBS, UNK, UNK, UNK, OBS], [False, False, True, False, False]
    )
    # Two product-gap weeks around the set gap: within the tolerance.
    assert model_context([OBS, UNK, UNK, UNK, OBS], two_and_set_gap, 2) == 0


def test_mixed_currency_codes_mark_a_money_series_and_not_a_unit_series() -> None:
    rows = product_rows(asin(1), range(104), 1.0)
    rows += [target_row(asin(1), 0, "revenue_shipped", value=1.0, currency="EUR")]
    rows = [
        r
        for r in rows
        if not (
            r.week_start == STARTS[0] and r.target == "revenue_shipped" and r.min_currency == "USD"
        )
    ]
    item = series(rows).series[0]
    assert item.targets["revenue_shipped"].currency_mixed is True
    assert item.targets["units_ordered"].currency_mixed is False
    assert item.targets["cogs_shipped"].currency_code == "USD"


def test_glance_views_are_unknown_on_an_uncovered_traffic_week_and_known_elsewhere() -> None:
    day = STARTS[50]
    item = series(
        product_rows(asin(1), range(104), 1.0),
        traffic=traffic_rows(asin(1), range(104)),
        traffic_coverage=without_day(day),
    ).series[0]
    assert item.covariate[50] is None
    assert item.covariate[49] == 100.0
