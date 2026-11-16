# SPDX-License-Identifier: MIT-0
"""The forecast model port: one call per planned batch (adapted from the source engine's port).

Callers may rely on: one forecast per request series, in request order; with
`cross_learning`, the series of one request are forecast jointly. A model failure raises
`ForecastModelError`; the job reports it, never a partial batch.
"""

from __future__ import annotations

from typing import Protocol

from domain.forecasting.models import ForecastBatchRequest, ForecastBatchResult


class ForecastModelError(Exception):
    """The model could not forecast a batch; the message carries no series values."""


class ForecastModelPort(Protocol):
    """A quantile forecaster over weekly series."""

    @property
    def model_id(self) -> str:
        """The model identity recorded in the run report and fingerprint."""
        ...

    def forecast(self, request: ForecastBatchRequest) -> ForecastBatchResult:
        """Forecast every series of the batch; raise `ForecastModelError` on any failure."""
        ...
