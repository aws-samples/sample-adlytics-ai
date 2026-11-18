# SPDX-License-Identifier: MIT-0
"""The state-space ETS adapter on the job's locked statsmodels and pandas (design §5a, R-41).

A library mismatch here would fail every ETS comparator, or let an unconverged fit be scored and
shown, or publish a band that is not statsmodels' 80% interval.
"""

from __future__ import annotations

import math
from importlib.metadata import version
from pathlib import Path

import pytest

from adapters.jobs.demand_forecast.ets_statsmodels import StatsmodelsStateSpaceEts
from adapters.jobs.demand_forecast.self_check import synthetic_series
from domain.forecasting.comparators import EtsRequest
from domain.forecasting.models import SeriesHistory, SkipReason

pytestmark = pytest.mark.forecasting

APPROVED = Path(__file__).resolve().parents[2] / (
    "src/adapters/jobs/demand_forecast/approved-packages.txt"
)


def _approved(name: str) -> str:
    for line in APPROVED.read_text().splitlines():
        package, _, pinned = line.partition("==")
        if package.strip().lower() == name:
            return pinned.strip()
    raise AssertionError(f"{name} is not in approved-packages.txt")


@pytest.mark.parametrize("package", ["statsmodels", "pandas"])
def test_the_environment_runs_the_approved_library_version(package: str) -> None:
    assert version(package) == _approved(package)


def test_series_with_unknown_weeks_fit_to_finite_ordered_13_week_bands() -> None:
    model = StatsmodelsStateSpaceEts()
    assert any(None in series.values for series in synthetic_series())
    for series in synthetic_series():
        result = model.fit_forecast(EtsRequest(series, 13))
        assert result.failure is None
        assert result.forecast is not None
        forecast = result.forecast
        assert (forecast.canonical_asin, forecast.target) == (series.canonical_asin, series.target)
        for band in (forecast.p10, forecast.p50, forecast.p90):
            assert len(band) == 13
            assert all(math.isfinite(value) for value in band)
        assert all(
            low < mid < high
            for low, mid, high in zip(forecast.p10, forecast.p50, forecast.p90, strict=True)
        )


def test_a_constant_series_is_refused_as_an_unconverged_fit() -> None:
    """The warning rule (R3-23): a flat line with no tested accuracy is never shown."""
    values = tuple(5.0 for _ in range(40))
    series = SeriesHistory("amazon_asin:b0fixtur01", "units_ordered", values, values)
    result = StatsmodelsStateSpaceEts().fit_forecast(EtsRequest(series, 13))
    assert (result.forecast, result.failure) == (None, SkipReason.ETS_FIT_FAILED)
