# SPDX-License-Identifier: MIT-0
"""The ETS comparator port: one state-space fit and forecast per series (design §4a, §5a).

Callers may rely on: unknown weeks reach the fit as missing, never filled; an unconverged fit
returns `ETS_FIT_FAILED`, never a forecast; the forecast is uncensored (the caller applies
`censor_at_zero`). Any other library failure raises `EtsModelError`; the job reports it, never a
partial forecast.
"""

from __future__ import annotations

from typing import Protocol

from domain.forecasting.comparators import EtsRequest, EtsResult


class EtsModelError(Exception):
    """The ETS library failed on a series; the message carries no series values."""


class EtsModelPort(Protocol):
    """A no-trend, no-season exponential smoothing model fitted on the whole context."""

    def fit_forecast(self, request: EtsRequest) -> EtsResult:
        """Fit the context and forecast `horizon_weeks`; raise `EtsModelError` on a failure."""
        ...
