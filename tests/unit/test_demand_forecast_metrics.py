# SPDX-License-Identifier: MIT-0
"""The demand-forecast emitters (design §9): names, units and dimension values.

An emitter that drifts from the dictionary's `operational_metrics` section puts a datapoint no
alarm reads, so a failed or held run, or a slow publication, goes unseen.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adapters.outbound.telemetry.demand_forecast_metrics import (
    NAMESPACE,
    Datapoint,
    landing_to_publish,
    metric_data,
    run_outcome,
)
from domain.forecasting.publish import RunOutcome
from domain.operations.generated_operational_metrics import (
    FORECAST_RUN_OUTCOME,
    PRODUCER_OUTCOME,
)

pytestmark = pytest.mark.unit

AT = datetime(2026, 1, 1, 13, tzinfo=UTC)


def test_every_run_outcome_is_a_declared_dimension_value() -> None:
    declared = dict(FORECAST_RUN_OUTCOME.dimensions)["outcome"]
    assert set(declared) == {outcome.value for outcome in RunOutcome}


def test_a_run_outcome_renders_as_one_count_in_the_demand_forecast_namespace() -> None:
    assert NAMESPACE == "Adlytics/DemandForecast"
    assert metric_data([run_outcome("failed")], AT) == [
        {
            "MetricName": "ForecastRunOutcome",
            "Dimensions": [{"Name": "outcome", "Value": "failed"}],
            "Timestamp": AT,
            "Value": 1.0,
            "Unit": "Count",
        }
    ]


def test_landing_to_publish_renders_in_seconds_with_no_dimension() -> None:
    assert metric_data([landing_to_publish(5400)], AT) == [
        {
            "MetricName": "LandingToPublishSeconds",
            "Dimensions": [],
            "Timestamp": AT,
            "Value": 5400,
            "Unit": "Seconds",
        }
    ]


@pytest.mark.parametrize(
    ("dimensions", "value"),
    [
        ((("outcome", "exploded"),), 1.0),
        ((), 1.0),
        ((("outcome", "failed"), ("shard", "1")), 1.0),
        ((("outcome", "failed"),), float("nan")),
    ],
    ids=["undeclared-value", "missing-dimension", "extra-dimension", "non-finite"],
)
def test_a_datapoint_outside_the_declaration_is_refused_when_built(
    dimensions: tuple[tuple[str, str], ...], value: float
) -> None:
    with pytest.raises(ValueError, match="ForecastRunOutcome"):
        Datapoint(FORECAST_RUN_OUTCOME, value, dimensions)


def test_a_metric_from_the_producer_namespace_is_refused() -> None:
    with pytest.raises(ValueError, match="not in Adlytics/DemandForecast"):
        Datapoint(PRODUCER_OUTCOME, 1.0, (("class", "landed"),))


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        metric_data([run_outcome("published")], datetime(2026, 1, 1, 13))  # noqa: DTZ001
