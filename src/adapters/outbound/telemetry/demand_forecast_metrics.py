# SPDX-License-Identifier: MIT-0
"""Demand-forecast operational metrics through `PutMetricData` (design §9).

Callers may rely on:
- every name, unit, namespace and dimension value comes from the generated projection of the
  dictionary's `operational_metrics` section (`domain.operations.generated_operational_metrics`),
  so an emitter can't drift from the alarms that read it;
- a datapoint with an undeclared dimension name or value, or a non-finite value, is refused when
  it is built, before anything is put;
- a failed put raises `MetricEmitError` naming the AWS error code, so the function's `Errors`
  alarm fires rather than an outcome going uncounted.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast

from botocore.exceptions import BotoCoreError, ClientError

from domain.operations.generated_operational_metrics import (
    FORECAST_CURRENT_AGE_DAYS,
    FORECAST_RUN_OUTCOME,
    LANDING_TO_PUBLISH_SECONDS,
    UNKNOWN_SALES_DAYS,
    OperationalMetricSpec,
)

if TYPE_CHECKING:
    from types_boto3_cloudwatch.client import CloudWatchClient

NAMESPACE: Final = FORECAST_RUN_OUTCOME.namespace
"""`Adlytics/DemandForecast`, the only namespace the publisher's IAM grant allows."""
PUT_BATCH: Final = 20
"""Datapoints per `PutMetricData` call, as the repository's other emitter batches."""


class MetricEmitError(Exception):
    """`PutMetricData` failed; the message is the AWS error code or the exception's name."""


@dataclass(frozen=True)
class Datapoint:
    """One value of one declared metric, with exactly its declared dimensions."""

    spec: OperationalMetricSpec
    value: float
    dimensions: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.spec.namespace != NAMESPACE:
            raise ValueError(f"{self.spec.name} is not in {NAMESPACE}")
        if not math.isfinite(self.value):
            raise ValueError(f"{self.spec.name} needs a finite value")
        declared = dict(self.spec.dimensions)
        if [name for name, _ in self.dimensions] != list(declared):
            raise ValueError(f"{self.spec.name} carries exactly {list(declared)}")
        for name, value in self.dimensions:
            if value not in declared[name]:
                raise ValueError(f"{self.spec.name}: {name}={value!r} is not declared")


def run_outcome(outcome: str) -> Datapoint:
    """`ForecastRunOutcome{outcome}`: one count for one recorded outcome."""
    return Datapoint(FORECAST_RUN_OUTCOME, 1.0, (("outcome", outcome),))


def landing_to_publish(seconds: float) -> Datapoint:
    """`LandingToPublishSeconds`: from the newest selected publication to `published_at`."""
    return Datapoint(LANDING_TO_PUBLISH_SECONDS, seconds)


def current_age(days: int) -> Datapoint:
    """`ForecastCurrentAgeDays`: whole days since CURRENT's generation was published."""
    return Datapoint(FORECAST_CURRENT_AGE_DAYS, float(days))


def unknown_days(days: int) -> Datapoint:
    """`UnknownSalesDays`: the input window's unknown sales days past the grace days."""
    return Datapoint(UNKNOWN_SALES_DAYS, float(days))


def metric_data(points: Sequence[Datapoint], timestamp: datetime) -> list[dict[str, object]]:
    """Render datapoints as `PutMetricData` `MetricData` entries."""
    if timestamp.tzinfo is None:
        raise ValueError("a metric timestamp is timezone-aware")
    return [
        {
            "MetricName": point.spec.name,
            "Dimensions": [{"Name": name, "Value": value} for name, value in point.dimensions],
            "Timestamp": timestamp,
            "Value": point.value,
            "Unit": point.spec.unit,
        }
        for point in points
    ]


class CloudWatchDemandForecastMetrics:
    """Puts datapoints in `Adlytics/DemandForecast`, `PUT_BATCH` at a time."""

    def __init__(self, client: CloudWatchClient) -> None:
        self._client = client

    def emit(self, points: Sequence[Datapoint], timestamp: datetime) -> None:
        """Put every datapoint at `timestamp`; raise `MetricEmitError` on a failed put."""
        data = metric_data(points, timestamp)
        try:
            for start in range(0, len(data), PUT_BATCH):
                self._client.put_metric_data(
                    Namespace=NAMESPACE, MetricData=cast("Any", data[start : start + PUT_BATCH])
                )
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", "")) or "ClientError"
            raise MetricEmitError(code) from error
        except BotoCoreError as error:
            raise MetricEmitError(type(error).__name__) from error
