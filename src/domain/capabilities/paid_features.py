# SPDX-License-Identifier: MIT-0
"""Paid-feature availability, derived from Publication State and resolved per request (ADR-107).

Adlytics can't ask AMC whether a customer subscribes to a paid feature, and a configuration
flag would drift from what AMC actually delivers. So availability is *derived*: from whether a
successful publication of the paid contract exists. A missing delivery can mean "not enrolled",
"not landed yet this period" or "the producer failed", and those need different texts.

Two halves, because artifacts are baked only when a publication changes (ADR-070, ADR-075):

- `derive_paid_feature_state` runs at bake and catalog time and returns the four values a
  materializer bakes. It never returns a resolved state text.
- `resolve_period_state` runs per request in the presentation adapter, with that adapter's own
  clock date as `as_of`. A delivery that never arrives therefore turns "pending" into "missing"
  without any rebake. Neither function reads a clock.

Pure domain. The publication evidence arrives as plain periods an adapter read through the
existing `CurrentPublicationPort`, so every rule is testable without a store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from itertools import pairwise

from domain.shared_kernel.values import DateRange

__all__ = [
    "FEATURE_OBSERVATION_PERIODS",
    "DerivePaidFeatureStateRequest",
    "PaidFeatureDisplay",
    "PaidFeatureState",
    "PaidFeatureStateError",
    "PeriodState",
    "ResolvePeriodStateRequest",
    "ResolvePeriodStateResult",
    "derive_paid_feature_state",
    "resolve_period_state",
]

FEATURE_OBSERVATION_PERIODS = 3
"""How many of the contract's latest declared periods can show the feature was observed.

Three, so a subscribed workspace doesn't flip to "needs the paid feature" on the first days of a
month or after one failed run (ADR-107 point 1). The window is the deployment's latest three,
never the three ending at the requested period, so an older requested period in a workspace
that receives the feature reads "missing", not "needs the paid feature".
"""


class PaidFeatureStateError(ValueError):
    """The publication evidence can't produce a coherent paid-feature state."""


class PeriodState(StrEnum):
    """Whether one requested period's figures exist, are still expected, or didn't arrive."""

    PUBLISHED = "published"
    """A current successful publication covers the requested period."""

    PENDING = "pending"
    """Not published, and `as_of` is on or before `expected_after`."""

    MISSING = "missing"
    """Not published, and `as_of` is after `expected_after`."""


class PaidFeatureDisplay(StrEnum):
    """Which of the four decision-1 rows a paid panel renders (design §3e).

    The texts themselves are built on the server by the presentation adapter, from the declared
    grain and period, never here and never hard-coded in the frontend.
    """

    NEEDS_PAID_FEATURE = "needs_paid_feature"
    """Nothing delivered in the last three periods. Never claims the customer isn't subscribed."""

    PENDING = "pending"
    """"Figures for {period} are expected after {date}"."""

    MISSING = "missing"
    """"Figures for {period} didn't arrive", naming the latest available period."""

    PUBLISHED = "published"
    """The figures."""


@dataclass(frozen=True, slots=True)
class PaidFeatureState:
    """The four values a materializer bakes for one paid section and requested period."""

    feature_observed: bool
    """A successful publication exists for one of the contract's last three declared periods.

    Deployment-level: it doesn't depend on which period was requested.
    """

    period_published: bool
    """A current successful publication covers the requested period."""

    expected_after: date
    """The requested period's end plus the pack's `cadenceLagDays`."""

    latest_available_period: DateRange | None
    """The latest period with a current publication, or None when there is none."""

    def __post_init__(self) -> None:
        # `period_published` without `feature_observed` is coherent: a requested period older
        # than the observation window can be published while nothing arrived lately. The
        # figures render (`resolve_period_state` checks `period_published` first).
        if self.period_published and self.latest_available_period is None:
            raise PaidFeatureStateError(
                "a published requested period must be reflected in latest_available_period."
            )


@dataclass(frozen=True, slots=True)
class DerivePaidFeatureStateRequest:
    """Publication evidence for one paid contract, as an adapter read it."""

    requested_period: DateRange
    declared_periods: tuple[DateRange, ...]
    """The contract's declared periods in ascending order, through its latest declared period.

    Must contain `requested_period`. The last three entries are the observation window, so the
    adapter passes them through the latest period the contract declares, whatever was requested.
    """

    published_periods: frozenset[DateRange]
    """Every period with a current successful publication of the paid contract."""

    cadence_lag_days: int
    """The pack's declared lag after period end (ADR-110)."""


def derive_paid_feature_state(request: DerivePaidFeatureStateRequest) -> PaidFeatureState:
    """Derive the bakeable state from publication evidence, refusing incoherent input."""
    periods = request.declared_periods
    if request.requested_period not in periods:
        raise PaidFeatureStateError(
            "declared_periods must contain the requested period; a period the contract doesn't "
            "declare has no paid-feature state."
        )
    if any(later.start <= earlier.end for earlier, later in pairwise(periods)):
        raise PaidFeatureStateError("declared_periods must be ascending and must not overlap.")
    if request.cadence_lag_days < 0:
        raise PaidFeatureStateError("cadence_lag_days cannot be negative.")

    window = periods[-FEATURE_OBSERVATION_PERIODS:]
    latest = max(request.published_periods, key=lambda period: period.end, default=None)
    return PaidFeatureState(
        feature_observed=any(period in request.published_periods for period in window),
        period_published=request.requested_period in request.published_periods,
        expected_after=request.requested_period.end + timedelta(days=request.cadence_lag_days),
        latest_available_period=latest,
    )


@dataclass(frozen=True, slots=True)
class ResolvePeriodStateRequest:
    """A baked state and the presentation adapter's own clock date."""

    baked_state: PaidFeatureState
    as_of: date


@dataclass(frozen=True, slots=True)
class ResolvePeriodStateResult:
    """The per-request period state and the decision-1 row it renders."""

    state: PeriodState
    display: PaidFeatureDisplay
    expected_after: date
    latest_available_period: DateRange | None


def resolve_period_state(request: ResolvePeriodStateRequest) -> ResolvePeriodStateResult:
    """Resolve published, pending or missing for one request, never reading a clock.

    `pending` holds on `expected_after` itself and `missing` begins the day after, so a delivery
    landing on the last expected day is never called late.
    """
    baked = request.baked_state
    if baked.period_published:
        state = PeriodState.PUBLISHED
    elif request.as_of <= baked.expected_after:
        state = PeriodState.PENDING
    else:
        state = PeriodState.MISSING

    if state is PeriodState.PUBLISHED:
        display = PaidFeatureDisplay.PUBLISHED
    elif not baked.feature_observed:
        display = PaidFeatureDisplay.NEEDS_PAID_FEATURE
    else:
        display = PaidFeatureDisplay(state.value)
    return ResolvePeriodStateResult(
        state=state,
        display=display,
        expected_after=baked.expected_after,
        latest_available_period=baked.latest_available_period,
    )
