# SPDX-License-Identifier: MIT-0
"""`ForecastModelPort` on Chronos-2, loaded from the mounted JumpStart artifact on local disk.

Adapted from the source engine's adapter. Callers may rely on: the model loads from local files
only (`HF_HUB_OFFLINE=1`; the job runs network-isolated), on CPU, with no `device_map` (so no
accelerate dispatch path); glance views go in as a past covariate only; an unknown week is NaN,
from which Chronos-2 builds its context mask; `cross_learning` forecasts the request jointly. A
prediction that raises becomes `ForecastModelError`. Loading errors propagate to the caller.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
from chronos import Chronos2Pipeline  # pyright: ignore[reportMissingTypeStubs]

from domain.forecasting.models import (
    CHRONOS2_MODEL_ID,
    ForecastBatchRequest,
    ForecastBatchResult,
    QuantileForecast,
    SeriesHistory,
)
from domain.forecasting.policy import QUANTILES
from ports.outbound.forecast_model import ForecastModelError

COVARIATE = "glance_views"


def _array(values: tuple[float | None, ...]) -> Any:
    return np.asarray([math.nan if v is None else v for v in values], dtype=np.float32)


def model_input(series: SeriesHistory) -> dict[str, Any]:
    """One series as Chronos-2's dict input: `target` and the glance-views past covariate."""
    return {
        "target": _array(series.values),
        "past_covariates": {COVARIATE: _array(series.covariate)},
    }


class Chronos2ForecastModel:
    """Chronos-2 on CPU from a directory holding the JumpStart `config.json` and weights."""

    def __init__(self, model_directory: Path, batch_size: int) -> None:
        self._pipeline: Any = Chronos2Pipeline.from_pretrained(  # pyright: ignore[reportUnknownMemberType]
            str(model_directory), dtype="float32"
        )
        self._batch_size = batch_size

    @property
    def model_id(self) -> str:
        """The model identity recorded in the run report."""
        return CHRONOS2_MODEL_ID

    def forecast(self, request: ForecastBatchRequest) -> ForecastBatchResult:
        """Forecast the batch in one call; any library error becomes `ForecastModelError`."""
        try:
            quantiles, _mean = self._pipeline.predict_quantiles(
                [model_input(series) for series in request.series],
                prediction_length=request.horizon_weeks,
                quantile_levels=list(QUANTILES),
                batch_size=self._batch_size,
                cross_learning=request.cross_learning,
            )
        except Exception as error:
            raise ForecastModelError(type(error).__name__) from error
        forecasts: list[QuantileForecast] = []
        for series, item in zip(request.series, quantiles, strict=True):
            # A univariate target yields shape (1, horizon, quantile levels).
            bands = [
                tuple(float(v) for v in item[0, :, index].tolist())
                for index in range(len(QUANTILES))
            ]
            forecasts.append(
                QuantileForecast(series.canonical_asin, series.target, bands[0], bands[1], bands[2])
            )
        return ForecastBatchResult(tuple(forecasts))
