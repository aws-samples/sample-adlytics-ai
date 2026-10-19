# SPDX-License-Identifier: MIT-0
"""The cells a cohort vintage serves, and whether two vintages share one cohort grain (ADR-104).

A cohort vintage (`TimeGrain.CUSTOM`) is the 13 whole calendar months ending on the last day of
its `as_of_month` *a* (`domain.semantics.calendar_periods.VINTAGE_MONTHS`). Its cells form a
triangle, enumerated here from the vintage alone, so the load, the bake and the page agree:

- **Monthly** (pack version 1): every cohort month *m* in the vintage and every follow-up month
  *k* = 1 to 12 with *m* + *k* <= *a* (design §6.6, "Closed enumeration").
- **Quarterly** (the `quarterly` variant, Retention LLD "Quarterly cohorts"): every calendar
  cohort quarter *q* whose first month is inside the vintage, and every follow-up quarter *j* that
  has ended by the end of *a*. A cohort quarter plus three follow-up quarters spans the 12 months
  the window can serve, so Quarter 4 is never a cell.

Pure: dates in, cell values out. A cohort value is its first day (`YYYY-MM-DD`); an offset is
its follow-up count as decimal text, as the pack's rows carry them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from domain.data_contracts.contract import CohortGrain, CohortTriangle
from domain.semantics.calendar_periods import shift_months

__all__ = [
    "COHORT_GRAIN_CHANGED",
    "QUARTER_MONTHS",
    "TriangleCohort",
    "cohort_grain_changed_text",
    "months_between",
    "triangle_cohorts",
]

QUARTER_MONTHS = 3
COHORT_GRAIN_CHANGED = "Cohort grain changed in {period}."
"""D-8 (a)'s refusal across a monthly and a quarterly vintage (Retention LLD, design states)."""


def cohort_grain_changed_text(period_label: str) -> str:
    """Return the refusal naming the as-of month of the first quarterly vintage."""
    return COHORT_GRAIN_CHANGED.format(period=period_label)


@dataclass(frozen=True, slots=True)
class TriangleCohort:
    """One cohort of a vintage's triangle and the follow-up offsets it serves."""

    start: date
    offsets: tuple[int, ...]

    @property
    def value(self) -> str:
        """The cohort's cell value: its first day."""
        return self.start.isoformat()


def months_between(earlier: date, later: date) -> int:
    """Whole calendar months from one month start to a later one."""
    return (later.year - earlier.year) * 12 + later.month - earlier.month


def _monthly(triangle: CohortTriangle, start: date, as_of: date) -> tuple[TriangleCohort, ...]:
    cohorts: list[TriangleCohort] = []
    month = start
    while month < as_of:
        reach = min(triangle.follow_ups, months_between(month, as_of))
        cohorts.append(TriangleCohort(month, tuple(range(1, reach + 1))))
        month = shift_months(month, 1)
    return tuple(cohorts)


def _quarterly(triangle: CohortTriangle, start: date, as_of: date) -> tuple[TriangleCohort, ...]:
    cohorts: list[TriangleCohort] = []
    month = start
    while month <= as_of:
        if (month.month - 1) % QUARTER_MONTHS == 0:
            # Follow-up quarter j ends 3j + 2 months after the cohort quarter's first month.
            gap = months_between(month, as_of)
            offsets = tuple(
                j
                for j in range(1, triangle.follow_ups + 1)
                if QUARTER_MONTHS * j + QUARTER_MONTHS - 1 <= gap
            )
            if offsets:
                cohorts.append(TriangleCohort(month, offsets))
        month = shift_months(month, 1)
    return tuple(cohorts)


def triangle_cohorts(
    triangle: CohortTriangle, vintage: tuple[date, date]
) -> tuple[TriangleCohort, ...]:
    """Return every cohort the vintage serves and its offsets, oldest cohort first.

    `vintage` is the inclusive period: the first day of its first month to the last day of its
    `as_of_month`. A cohort with no servable offset (the as-of month itself, or a cohort quarter
    whose first follow-up quarter hasn't ended) isn't a cohort of the triangle.
    """
    start, end = vintage
    as_of = end.replace(day=1)
    if triangle.grain is CohortGrain.MONTH:
        return _monthly(triangle, start, as_of)
    return _quarterly(triangle, start, as_of)
