# SPDX-License-Identifier: MIT-0
"""`EtsModelPort` on statsmodels' state-space `ExponentialSmoothing` (design §5a, R3-23).

The model has additive errors, no trend and no season, initialised by estimation, and is
fitted on the whole context with unknown weeks as NaN (the state-space framework estimates
with missing observations, §3d). p50 is the `mean` column of
`get_forecast(horizon).summary_frame(alpha=0.2)`; p10 and p90 are `mean_ci_lower` and
`mean_ci_upper`, its 80% interval. A `ConvergenceWarning` during the fit or the forecast returns
`ETS_FIT_FAILED`, so an unconverged fit is never scored or shown. Any exception becomes
`EtsModelError`. The fit prints nothing (`disp=False`), so the job's JSON-lines log stays valid.
"""

from __future__ import annotations

import math
import warnings
from typing import Any

import numpy as np
from statsmodels.tools.sm_exceptions import ConvergenceWarning
from statsmodels.tsa.statespace.exponential_smoothing import ExponentialSmoothing

from domain.forecasting.comparators import EtsRequest, EtsResult
from domain.forecasting.models import QuantileForecast, SkipReason
from ports.outbound.ets_model import EtsModelError

INTERVAL_ALPHA = 0.2
"""`summary_frame`'s alpha for the 80% interval, whose bounds are p10 and p90."""

_MODEL: Any = ExponentialSmoothing


class StatsmodelsStateSpaceEts:
    """The ETS comparator; stateless, so one instance serves every series of a job."""

    def fit_forecast(self, request: EtsRequest) -> EtsResult:
        """Fit the series' context and forecast `horizon_weeks` weeks after it."""
        series = request.series
        endog = np.asarray(
            [math.nan if value is None else value for value in series.values], dtype=np.float64
        )
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                model: Any = _MODEL(
                    endog, trend=False, seasonal=None, initialization_method="estimated"
                )
                fitted: Any = model.fit(disp=False)
                frame: Any = fitted.get_forecast(request.horizon_weeks).summary_frame(
                    alpha=INTERVAL_ALPHA
                )
        except Exception as error:
            raise EtsModelError(type(error).__name__) from error
        if any(issubclass(warning.category, ConvergenceWarning) for warning in caught):
            return EtsResult(None, SkipReason.ETS_FIT_FAILED)

        def column(name: str) -> tuple[float, ...]:
            return tuple(float(value) for value in frame[name].tolist())

        return EtsResult(
            QuantileForecast(
                series.canonical_asin,
                series.target,
                column("mean_ci_lower"),
                column("mean"),
                column("mean_ci_upper"),
            ),
            None,
        )
