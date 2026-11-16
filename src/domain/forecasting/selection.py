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
from datetime import date, datetime, timedelta
from enum import StrEnum

from domain.forecasting.policy import WEEKDAYS
from domain.forecasting.publish import GENERATION_DIGEST_CHARS, CurrentPointer, Disposition
from domain.shared_kernel.values import DateRange

WINDOW_DAYS = 728
"""104 weeks of days ending `input_end`."""
SELECTION_DAYS = 1000
"""The selection read: the 1,000 days ending today (the exact-read bound)."""
SATURDAY = 5


class HeldCategory(StrEnum):
    """Why `select_input()` holds instead of running (design §6d steps 1 and 4)."""

    NO_COMPLETE_WEEK = "no_complete_week"
    INPUT_END_TOO_OLD = "input_end_too_old"
    WEEKLY_SLOT_NOT_DUE = "weekly_slot_not_due"


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


def previous_slot_date(check_date: date, admission_weekday: str) -> date:
    """Return the newest `admission_weekday` strictly before `check_date` (1 to 7 days back)."""
    if admission_weekday not in WEEKDAYS:
        raise ValueError(f"admission_weekday must be one of {list(WEEKDAYS)}")
    back = (check_date.weekday() - WEEKDAYS.index(admission_weekday)) % 7 or 7
    return check_date - timedelta(days=back)


def scheduled_run_id(set_id: str, check_date: date) -> str:
    """Return a scheduled check's run ID, `<set hash 12>-<UTC date>` (design §6d "Trigger").

    The execution itself is named by Scheduler (OQ-S2-3), so the run ID, and with it every
    receipt key of the run, comes from the scheduled time's UTC date.
    """
    if not set_id or "#" in set_id:
        raise ValueError("set_id is non-empty and has no '#'")
    return f"{set_id}-{check_date.isoformat()}"


def pointer_consumes_slot(current: CurrentPointer | None, slot_run_id: str) -> bool:
    """Whether `CURRENT` shows the slot run left a known fingerprint (OQ-S2-1 cases b and c).

    Either CURRENT's generation was published by the slot run (a `generation_id` is
    `<run_id>-<12 hex>`), or `last_attempt` is the slot run's with a non-null fingerprint.
    """
    if current is None:
        return False
    generation = current.generation_id
    cut = GENERATION_DIGEST_CHARS + 1
    if generation is not None and generation[:-cut] == slot_run_id and generation[-cut] == "-":
        return True
    last = current.last_attempt
    return last is not None and last.run_id == slot_run_id and last.run_fingerprint is not None


class Trigger(StrEnum):
    """Who started the execution: the daily schedule or an operator (design §6d)."""

    SCHEDULED = "scheduled"
    OPERATOR = "operator"


class SelectOutcome(StrEnum):
    """`SelectInput`'s decision."""

    RUN = "run"
    NO_OP_SAME_INPUT = "no_op_same_input"
    HELD_INPUT_NOT_READY = "held_input_not_ready"


class SelectBasis(StrEnum):
    """Which §6d step decided, for the log line and the tests."""

    SAME_AS_CURRENT = "same_as_current"
    """Step 2: the fingerprint is CURRENT's."""
    LAST_ATTEMPT_SUPPRESSED = "last_attempt_suppressed"
    """Step 3: `last_attempt` has this fingerprint with `suppress`."""
    LAST_ATTEMPT_RETRY = "last_attempt_retry"
    """Step 3: `last_attempt` has this fingerprint with `retry`; a retry runs on any day."""
    OPERATOR_START = "operator_start"
    """Step 4 skipped: an operator start runs a new fingerprint on any day."""
    WEEKLY_SLOT = "weekly_slot"
    """Step 4: the scheduled check falls on the slot weekday."""
    SLOT_MISSED = "slot_missed"
    """Step 4, R7-M2: the latest slot run neither recorded a select receipt nor a fingerprint."""
    SLOT_NOT_DUE = "slot_not_due"
    """Step 4: the latest slot is consumed and today is not the slot weekday."""


@dataclass(frozen=True)
class SelectDecisionRequest:
    """Steps 2 to 5 of §6d for one check whose window holds a complete week.

    `slot_consumed` is required exactly when a scheduled check falls off the slot weekday: the
    latest slot run has a `#select` receipt, or `pointer_consumes_slot()` holds (OQ-S2-1).
    """

    run_fingerprint: str
    current: CurrentPointer | None
    trigger: Trigger
    check_date: date
    admission_weekday: str
    retry_failed: bool = False
    slot_consumed: bool | None = None

    def __post_init__(self) -> None:
        if not self.run_fingerprint:
            raise ValueError("a decision needs the run fingerprint")
        if self.retry_failed and self.trigger is not Trigger.OPERATOR:
            raise ValueError("only an operator start carries RETRY_FAILED")
        off_slot = self.trigger is Trigger.SCHEDULED and not admits_new_input(
            self.check_date, self.admission_weekday
        )
        if off_slot != (self.slot_consumed is not None):
            raise ValueError("slot_consumed is given exactly for a scheduled check off the slot")


@dataclass(frozen=True)
class SelectDecision:
    """The outcome, the held category (only `weekly_slot_not_due` here) and the deciding step."""

    outcome: SelectOutcome
    category: HeldCategory | None
    basis: SelectBasis


def select_decision(request: SelectDecisionRequest) -> SelectDecision:
    """Apply §6d steps 2 to 5 in order; the first that applies wins."""
    current = request.current
    if current is not None and current.run_fingerprint == request.run_fingerprint:
        return SelectDecision(SelectOutcome.NO_OP_SAME_INPUT, None, SelectBasis.SAME_AS_CURRENT)
    last = None if current is None else current.last_attempt
    if (
        last is not None
        and last.run_fingerprint == request.run_fingerprint
        and not request.retry_failed
    ):
        if last.retry_disposition is Disposition.SUPPRESS:
            return SelectDecision(
                SelectOutcome.NO_OP_SAME_INPUT, None, SelectBasis.LAST_ATTEMPT_SUPPRESSED
            )
        return SelectDecision(SelectOutcome.RUN, None, SelectBasis.LAST_ATTEMPT_RETRY)
    return _slot_decision(request)


def _slot_decision(request: SelectDecisionRequest) -> SelectDecision:
    """Step 4: an operator start skips it; a scheduled check admits on the slot or a missed slot."""
    if request.trigger is Trigger.OPERATOR:
        return SelectDecision(SelectOutcome.RUN, None, SelectBasis.OPERATOR_START)
    if request.slot_consumed is None:
        return SelectDecision(SelectOutcome.RUN, None, SelectBasis.WEEKLY_SLOT)
    if request.slot_consumed:
        return SelectDecision(
            SelectOutcome.HELD_INPUT_NOT_READY,
            HeldCategory.WEEKLY_SLOT_NOT_DUE,
            SelectBasis.SLOT_NOT_DUE,
        )
    return SelectDecision(SelectOutcome.RUN, None, SelectBasis.SLOT_MISSED)


def current_age_days(published_at: datetime, now: datetime) -> int:
    """Whole days since CURRENT's generation was published (`ForecastCurrentAgeDays`, §9)."""
    if published_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("both instants are timezone-aware")
    return max(0, (now - published_at) // timedelta(days=1))


def unknown_sales_days(pins: WindowPins, check_date: date, grace_days: int) -> int:
    """Count the window's unknown sales days (`UnknownSalesDays`, round 7 definition, R5C2-13).

    Over `[window_start, input_end]` only: a day not covered for sales, more than `grace_days`
    before `check_date`, and on or after the window's first day with a sales pin, so a backfill
    in progress doesn't count. A check with no window (held at step 1) counts none.
    """
    if grace_days < 0:
        raise ValueError("grace_days is never negative")
    if pins.window is None or pins.input_end is None or not pins.sales:
        return 0
    first = max(pins.window.start, min(pin.bucket.start for pin in pins.sales))
    covered = _covered_days(pins.covered_sales)
    cutoff = check_date - timedelta(days=grace_days)
    span = (pins.input_end - first).days + 1
    days = (first + timedelta(days=n) for n in range(span))
    return sum(1 for day in days if day not in covered and day < cutoff)


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
