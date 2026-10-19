# SPDX-License-Identifier: MIT-0
"""Paid-feature availability derived from publication evidence, resolved per request (ADR-107).

Business-outcomes slice S0.7. The four decision-1 rows of design §3e each get a case, plus the
one that motivates resolving per request at all: an artifact baked inside the lag and served
after `expected_after` must render `missing`, never the stale `pending`.
"""

from __future__ import annotations

from datetime import date

import pytest

from domain.capabilities.paid_features import (
    DerivePaidFeatureStateRequest,
    PaidFeatureDisplay,
    PaidFeatureState,
    PaidFeatureStateError,
    PeriodState,
    ResolvePeriodStateRequest,
    derive_paid_feature_state,
    resolve_period_state,
)
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-107")]

JULY = DateRange(date(2026, 7, 1), date(2026, 7, 31))
AUGUST = DateRange(date(2026, 8, 1), date(2026, 8, 31))
SEPTEMBER = DateRange(date(2026, 9, 1), date(2026, 9, 30))
JUNE = DateRange(date(2026, 6, 1), date(2026, 6, 30))
MONTHS = (JUNE, JULY, AUGUST, SEPTEMBER)
LAG_DAYS = 2


def _derive(published: frozenset[DateRange], requested: DateRange = SEPTEMBER) -> PaidFeatureState:
    """Derive for a contract that declares June to September, its latest declared period."""
    return derive_paid_feature_state(
        DerivePaidFeatureStateRequest(
            requested_period=requested,
            declared_periods=MONTHS,
            published_periods=published,
            cadence_lag_days=LAG_DAYS,
        )
    )


def _resolve(state: PaidFeatureState, as_of: date) -> tuple[PeriodState, PaidFeatureDisplay]:
    result = resolve_period_state(ResolvePeriodStateRequest(state, as_of))
    return result.state, result.display


class TestDerivation:
    def test_expected_after_is_period_end_plus_the_cadence_lag(self) -> None:
        state = _derive(frozenset())

        assert state.expected_after == date(2026, 10, 2)

    def test_a_publication_in_the_last_three_periods_observes_the_feature(self) -> None:
        state = _derive(frozenset({JULY}))

        assert state.feature_observed is True
        assert state.period_published is False
        assert state.latest_available_period == JULY

    def test_a_publication_older_than_three_periods_does_not_observe_the_feature(self) -> None:
        """June is the fourth period back from September, so it no longer counts."""
        state = _derive(frozenset({JUNE}))

        assert state.feature_observed is False
        assert state.latest_available_period == JUNE

    def test_the_requested_period_published_is_both_observed_and_latest(self) -> None:
        state = _derive(frozenset({AUGUST, SEPTEMBER}))

        assert (state.feature_observed, state.period_published) == (True, True)
        assert state.latest_available_period == SEPTEMBER

    def test_declared_periods_must_contain_the_requested_period(self) -> None:
        with pytest.raises(PaidFeatureStateError, match="must contain the requested period"):
            derive_paid_feature_state(
                DerivePaidFeatureStateRequest(
                    requested_period=SEPTEMBER,
                    declared_periods=(JULY, AUGUST),
                    published_periods=frozenset(),
                    cadence_lag_days=LAG_DAYS,
                )
            )

    def test_an_older_requested_period_uses_the_deployments_latest_three_periods(self) -> None:
        """Review A-1: requesting June while September is published observes the feature."""
        state = _derive(frozenset({SEPTEMBER}), requested=JUNE)

        assert state.feature_observed is True
        assert state.period_published is False
        assert state.expected_after == date(2026, 7, 2)
        assert state.latest_available_period == SEPTEMBER
        assert _resolve(state, date(2026, 10, 8)) == (
            PeriodState.MISSING,
            PaidFeatureDisplay.MISSING,
        )

    def test_feature_observed_is_the_same_whichever_period_is_requested(self) -> None:
        """Deployment-level, so the catalog may read a state derived for any declared period."""
        observed = {_derive(frozenset({AUGUST}), requested=m).feature_observed for m in MONTHS}
        unobserved = {_derive(frozenset({JUNE}), requested=m).feature_observed for m in MONTHS}

        assert (observed, unobserved) == ({True}, {False})

    def test_overlapping_declared_periods_are_refused(self) -> None:
        with pytest.raises(PaidFeatureStateError, match="ascending and must not overlap"):
            derive_paid_feature_state(
                DerivePaidFeatureStateRequest(
                    requested_period=SEPTEMBER,
                    declared_periods=(
                        AUGUST,
                        DateRange(date(2026, 8, 31), date(2026, 9, 6)),
                        SEPTEMBER,
                    ),
                    published_periods=frozenset(),
                    cadence_lag_days=LAG_DAYS,
                )
            )

    def test_a_published_period_without_a_latest_period_cannot_be_constructed(self) -> None:
        with pytest.raises(PaidFeatureStateError, match="reflected in latest_available_period"):
            PaidFeatureState(
                feature_observed=True,
                period_published=True,
                expected_after=date(2026, 10, 2),
                latest_available_period=None,
            )

    def test_a_published_period_older_than_the_window_renders_its_figures(self) -> None:
        """June is published but outside the latest three periods: the figures still render."""
        state = _derive(frozenset({JUNE}), requested=JUNE)

        assert (state.feature_observed, state.period_published) == (False, True)
        assert _resolve(state, date(2026, 10, 8)) == (
            PeriodState.PUBLISHED,
            PaidFeatureDisplay.PUBLISHED,
        )


class TestTheFourRenderRows:
    """One case per row of the design §3e table, with an explicit `as_of`."""

    def test_never_observed_renders_needs_the_paid_feature_whatever_the_date(self) -> None:
        state = _derive(frozenset())

        assert _resolve(state, date(2026, 10, 1)) == (
            PeriodState.PENDING,
            PaidFeatureDisplay.NEEDS_PAID_FEATURE,
        )
        assert _resolve(state, date(2026, 10, 20)) == (
            PeriodState.MISSING,
            PaidFeatureDisplay.NEEDS_PAID_FEATURE,
        )

    def test_observed_and_inside_the_lag_renders_pending_through_expected_after(self) -> None:
        state = _derive(frozenset({AUGUST}))

        assert _resolve(state, date(2026, 10, 2)) == (
            PeriodState.PENDING,
            PaidFeatureDisplay.PENDING,
        )

    def test_observed_and_past_the_lag_renders_missing_from_the_next_day(self) -> None:
        state = _derive(frozenset({AUGUST}))

        result = resolve_period_state(ResolvePeriodStateRequest(state, date(2026, 10, 3)))

        assert (result.state, result.display) == (PeriodState.MISSING, PaidFeatureDisplay.MISSING)
        assert result.latest_available_period == AUGUST

    def test_published_renders_the_figures_whatever_the_date(self) -> None:
        state = _derive(frozenset({SEPTEMBER}))

        assert _resolve(state, date(2026, 12, 31)) == (
            PeriodState.PUBLISHED,
            PaidFeatureDisplay.PUBLISHED,
        )


def test_an_artifact_baked_inside_the_lag_reads_missing_once_served_after_expected_after() -> None:
    """The reason the text is resolved per request: a baked "pending" would go stale."""
    baked_on_october_first = _derive(frozenset({AUGUST}))

    served_the_same_day = _resolve(baked_on_october_first, date(2026, 10, 1))
    served_a_week_later = _resolve(baked_on_october_first, date(2026, 10, 8))

    assert served_the_same_day == (PeriodState.PENDING, PaidFeatureDisplay.PENDING)
    assert served_a_week_later == (PeriodState.MISSING, PaidFeatureDisplay.MISSING)
