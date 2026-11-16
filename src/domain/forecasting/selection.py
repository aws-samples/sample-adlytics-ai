# SPDX-License-Identifier: MIT-0
"""Pick `input_end` and keep the pins of the 104 weeks ending it (design §3c, R4-02, R5-01).

The selection reads the 1,000 days ending today; the window `[input_end - 727, input_end]` is
anchored on `input_end`, never on today, so the passing of days alone never changes the input
fingerprint. Product pins are in neither fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from domain.forecasting.policy import WEEKDAYS
from domain.shared_kernel.values import DateRange

WINDOW_DAYS = 728
"""104 weeks of days ending `input_end`."""
SELECTION_DAYS = 1000
"""The selection read: the 1,000 days ending today (the exact-read bound)."""
SATURDAY = 5


class HeldCategory(StrEnum):
    """Why `select_input()` holds instead of running (design §6d step 1)."""

    NO_COMPLETE_WEEK = "no_complete_week"
    INPUT_END_TOO_OLD = "input_end_too_old"


@dataclass(frozen=True)
class WindowPin:
    """One pinned CURRENT publication and the day range its bucket covers."""

    partition: str
    publication_id: str
    fencing_token: int
    digest: str
    bucket: DateRange


@dataclass(frozen=True)
class SelectionWindowRequest:
    """The selection's sales and traffic pins, their covered days, and the selection window."""

    sales: tuple[WindowPin, ...]
    traffic: tuple[WindowPin, ...]
    covered_sales: tuple[DateRange, ...]
    covered_traffic: tuple[DateRange, ...]
    selection_window: DateRange

    def __post_init__(self) -> None:
        if self.selection_window.days > SELECTION_DAYS:
            raise ValueError(f"the selection window is at most {SELECTION_DAYS} days")


@dataclass(frozen=True)
class WindowPins:
    """The run's `input_end`, window, pins and input fingerprint, or why the run is held."""

    held: HeldCategory | None
    input_end: date | None
    window: DateRange | None
    sales: tuple[WindowPin, ...]
    traffic: tuple[WindowPin, ...]
    covered_sales: tuple[DateRange, ...]
    covered_traffic: tuple[DateRange, ...]
    input_fingerprint: str | None


def _held(category: HeldCategory, input_end: date | None) -> WindowPins:
    return WindowPins(category, input_end, None, (), (), (), (), None)


def admits_new_input(check_date: date, admission_weekday: str) -> bool:
    """Whether a scheduled check on UTC `check_date` is the weekly slot (design §6d step 4).

    Only the slot admits a new run fingerprint; the caller passes the date it already reads for
    the selection window, so the domain reads no clock.
    """
    if admission_weekday not in WEEKDAYS:
        raise ValueError(f"admission_weekday must be one of {list(WEEKDAYS)}")
    return WEEKDAYS[check_date.weekday()] == admission_weekday


def _covered_days(intervals: tuple[DateRange, ...]) -> set[date]:
    days: set[date] = set()
    for interval in intervals:
        days.update(interval.start + timedelta(days=n) for n in range(interval.days))
    return days


def newest_complete_saturday(covered: tuple[DateRange, ...]) -> date | None:
    """Return the newest Saturday whose Sunday-to-Saturday week is wholly covered, or None."""
    days = _covered_days(covered)
    saturdays = sorted((d for d in days if d.weekday() == SATURDAY), reverse=True)
    for saturday in saturdays:
        if all(saturday - timedelta(days=n) in days for n in range(7)):
            return saturday
    return None


def _clip(intervals: tuple[DateRange, ...], window: DateRange) -> tuple[DateRange, ...]:
    clipped: list[DateRange] = []
    for interval in intervals:
        if interval.overlaps(window):
            clipped.append(
                DateRange(max(interval.start, window.start), min(interval.end, window.end))
            )
    return tuple(clipped)


def _inside(pins: tuple[WindowPin, ...], window: DateRange) -> tuple[WindowPin, ...]:
    kept = [pin for pin in pins if window.covers(pin.bucket)]
    return tuple(sorted(kept, key=lambda pin: (pin.bucket.start, pin.partition)))


def _pin_json(pin: WindowPin) -> list[object]:
    return [
        pin.partition,
        pin.publication_id,
        pin.fencing_token,
        pin.digest,
        pin.bucket.start.isoformat(),
        pin.bucket.end.isoformat(),
    ]


def input_fingerprint(pins: WindowPins) -> str:
    """SHA-256 of the canonical JSON of the window pins and their covered intervals, in order."""
    body = {
        "sales": [_pin_json(pin) for pin in pins.sales],
        "traffic": [_pin_json(pin) for pin in pins.traffic],
        "covered_sales": [[r.start.isoformat(), r.end.isoformat()] for r in pins.covered_sales],
        "covered_traffic": [[r.start.isoformat(), r.end.isoformat()] for r in pins.covered_traffic],
        "input_end": pins.input_end.isoformat() if pins.input_end else None,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def window_pins(request: SelectionWindowRequest) -> WindowPins:
    """Apply design §3c steps 2 to 5: `input_end`, the age check, the window and the fingerprint."""
    input_end = newest_complete_saturday(request.covered_sales)
    if input_end is None:
        return _held(HeldCategory.NO_COMPLETE_WEEK, None)
    window = DateRange(input_end - timedelta(days=WINDOW_DAYS - 1), input_end)
    if window.start < request.selection_window.start:
        return _held(HeldCategory.INPUT_END_TOO_OLD, input_end)
    unfingerprinted = WindowPins(
        held=None,
        input_end=input_end,
        window=window,
        sales=_inside(request.sales, window),
        traffic=_inside(request.traffic, window),
        covered_sales=_clip(request.covered_sales, window),
        covered_traffic=_clip(request.covered_traffic, window),
        input_fingerprint=None,
    )
    return WindowPins(
        held=None,
        input_end=input_end,
        window=window,
        sales=unfingerprinted.sales,
        traffic=unfingerprinted.traffic,
        covered_sales=unfingerprinted.covered_sales,
        covered_traffic=unfingerprinted.covered_traffic,
        input_fingerprint=input_fingerprint(unfingerprinted),
    )
