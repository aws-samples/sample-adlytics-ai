# SPDX-License-Identifier: MIT-0
"""Whether a registration's period is exactly one declared calendar period (ADR-110)."""

from __future__ import annotations

from datetime import date

from domain.producer.models import PeriodGrain
from domain.semantics.calendar_periods import is_complete_period
from domain.semantics.time import TimeGrain


def is_aligned(grain: PeriodGrain, period: tuple[date, date]) -> bool:
    """Whether `[start, end]` (inclusive UTC days) is one whole period of `grain`.

    An ISO week runs Monday to Sunday; a month runs from its 1st to its last day; a quarter starts
    on 1 January, April, July or October and ends on the last day of its third month. The
    arithmetic has one home, `domain.semantics.calendar_periods` (ADR-100), which serving uses too.
    """
    start, end = period
    return is_complete_period(TimeGrain(grain.value), start, end)
