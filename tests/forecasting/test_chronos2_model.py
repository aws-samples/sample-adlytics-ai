# SPDX-License-Identifier: MIT-0
"""The Chronos-2 adapter on fixed series with NaN context weeks (OQ-18).

A library mismatch here would fail every forecast job or publish non-finite or crossed bands.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
from pathlib import Path

import pytest

from adapters.jobs.demand_forecast.chronos2_model import Chronos2ForecastModel, model_input
from adapters.jobs.demand_forecast.job_log import JobLog
from adapters.jobs.demand_forecast.self_check import (
    SelfCheckRequest,
    _watched_codes,  # pyright: ignore[reportPrivateUsage]
    run_self_check,
    synthetic_series,
    watching,
)
from domain.forecasting.models import ForecastBatchRequest

pytestmark = pytest.mark.forecasting


def test_model_input_passes_unknown_weeks_as_nan() -> None:
    first = synthetic_series()[0]
    item = model_input(first)
    assert math.isnan(float(item["target"][40]))
    assert math.isnan(float(item["past_covariates"]["glance_views"][60]))
    assert len(item["target"]) == 104


def test_forecasts_are_finite_ordered_and_shaped_with_nan_context(forecast_model_dir: Path) -> None:
    model = Chronos2ForecastModel(forecast_model_dir, batch_size=256)
    result = model.forecast(ForecastBatchRequest(synthetic_series(), 13, cross_learning=True))
    assert len(result.forecasts) == 3
    for forecast in result.forecasts:
        for band in (forecast.p10, forecast.p50, forecast.p90):
            assert len(band) == 13
            assert all(math.isfinite(value) for value in band)
        assert all(
            low <= mid <= high
            for low, mid, high in zip(forecast.p10, forecast.p50, forecast.p90, strict=True)
        )


def test_the_self_check_passes_locally_and_calls_no_watched_function(
    forecast_model_dir: Path, tmp_path: Path
) -> None:
    stream = io.StringIO()
    report = run_self_check(
        SelfCheckRequest(forecast_model_dir, tmp_path, batch_size=256, cross_learning=True),
        JobLog("test", stream),
    )
    written = json.loads((tmp_path / "self-check.json").read_text())
    assert report.is_passed, written["failures"]
    assert written["passes_identical"] is True
    assert written["execute_path"]["called"] == []
    assert written["execute_path"]["unavailable"] == []


def test_the_watcher_records_a_call_of_a_watched_function() -> None:
    """Positive control: an empty `called` list only means something if a call would be seen."""
    from accelerate.utils import modeling  # noqa: PLC0415 - the module under watch

    watch = _watched_codes()
    with watching(watch), contextlib.suppress(Exception):
        modeling.load_checkpoint_in_model(None, "/nonexistent")  # pyright: ignore[reportArgumentType, reportUnknownMemberType]
    assert watch.called == {"accelerate.utils.modeling.load_checkpoint_in_model"}
