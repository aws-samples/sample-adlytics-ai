# SPDX-License-Identifier: MIT-0
"""Publish measurement snapshots and read them back from CloudWatch (FR-3, S5).

Two emitters share one rendering: EMF from the deployed reconciler (`EmfMeasurementSink`), and
`PutMetricData` from the probe runner, which has no Lambda log stream (`PutMetricDataSink`).
Both write each gauge at the window's deterministic timestamp, so a replay lands on the same
datapoint and `Maximum` can never count a contribution twice.

The reader (`CloudWatchMeasurementReader`) asks `GetMetricData` for exactly the expected gauges:
the `Adlytics` namespace, the family's names, the closed dimensions, that timestamp's one-minute
period and `Maximum`. A gauge the sink did not return reads as None, never as zero.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from aws_lambda_powertools.metrics import MetricUnit, single_metric
from botocore.exceptions import BotoCoreError, ClientError

from domain.operations.measurement import Gauge
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError

if TYPE_CHECKING:
    from types_boto3_cloudwatch.client import CloudWatchClient

__all__ = [
    "NAMESPACE",
    "PERIOD",
    "CloudWatchMeasurementReader",
    "EmfMeasurementSink",
    "MeasurementReader",
    "MeasurementSink",
    "PutMetricDataSink",
]

NAMESPACE: Final = "Adlytics"
PERIOD: Final = timedelta(minutes=1)
_SINK_FAILED: Final = ErrorCode("MEASUREMENT", 21)
_NOT_PROBE_SCOPE: Final = ErrorCode("MEASUREMENT", 23)
_PROBE_SCOPE: Final = ("AuthorityScope", "probe")


class MeasurementSink(Protocol):
    """Write one snapshot's gauges at their window timestamp."""

    def emit(self, gauges: Sequence[Gauge], timestamp: datetime) -> None:
        """Write and flush every gauge, or raise."""
        ...


class MeasurementReader(Protocol):
    """Read back the `Maximum` the sink holds for each expected gauge."""

    def read(self, gauges: Sequence[Gauge], timestamp: datetime) -> Mapping[Gauge, float | None]:
        """Return each gauge's observed value, or None when the sink returned none."""
        ...


class EmfMeasurementSink:
    """EMF records written to the function's log stream, one isolated record per gauge."""

    def emit(self, gauges: Sequence[Gauge], timestamp: datetime) -> None:
        """Write each gauge as its own EMF record at the window timestamp, flushed at emission."""
        for gauge in gauges:
            with single_metric(
                name=gauge.name,
                unit=MetricUnit(gauge.unit),
                value=float(gauge.value),
                namespace=NAMESPACE,
            ) as emitted:
                emitted.set_timestamp(timestamp)
                for name, value in gauge.dimensions:
                    emitted.add_dimension(name=name, value=value)


class PutMetricDataSink:
    """The probe runner's emitter: the same gauges through `PutMetricData`, probe scope only.

    IAM can condition `PutMetricData` on the namespace only, and live and probe gauges share
    `Adlytics`. So this emitter refuses, before any put, a gauge that is not `AuthorityScope=probe`:
    a probe-written live gauge could confirm a live snapshot it never measured.
    """

    def __init__(self, client: CloudWatchClient) -> None:
        self._client = client

    def emit(self, gauges: Sequence[Gauge], timestamp: datetime) -> None:
        """Put every gauge at the window timestamp, in bounded batches."""
        if any(_PROBE_SCOPE not in gauge.dimensions for gauge in gauges):
            raise InvariantError(
                _NOT_PROBE_SCOPE, "the probe emitter refuses a gauge outside AuthorityScope=probe."
            )
        data = [
            {
                "MetricName": gauge.name,
                "Dimensions": [{"Name": name, "Value": value} for name, value in gauge.dimensions],
                "Timestamp": timestamp,
                "Value": float(gauge.value),
                "Unit": gauge.unit,
            }
            for gauge in gauges
        ]
        try:
            for start in range(0, len(data), 20):
                self._client.put_metric_data(
                    Namespace=NAMESPACE, MetricData=cast("Any", data[start : start + 20])
                )
        except (BotoCoreError, ClientError) as error:
            raise DependencyError(
                _SINK_FAILED,
                "a measurement gauge could not be put.",
                context={"cause": type(error).__name__},
            ) from None


class CloudWatchMeasurementReader:
    """Read each expected gauge's `Maximum` at the window timestamp with `GetMetricData`."""

    def __init__(self, client: CloudWatchClient) -> None:
        self._client = client

    def read(self, gauges: Sequence[Gauge], timestamp: datetime) -> Mapping[Gauge, float | None]:
        """Return the observed `Maximum` per gauge for exactly that one-minute period."""
        start = timestamp.replace(second=0, microsecond=0)
        queries = [
            {
                "Id": f"g{index}",
                "MetricStat": {
                    "Metric": {
                        "Namespace": NAMESPACE,
                        "MetricName": gauge.name,
                        "Dimensions": [
                            {"Name": name, "Value": value} for name, value in gauge.dimensions
                        ],
                    },
                    "Period": int(PERIOD.total_seconds()),
                    "Stat": "Maximum",
                },
                "ReturnData": True,
            }
            for index, gauge in enumerate(gauges)
        ]
        observed: dict[Gauge, float | None] = dict.fromkeys(gauges)
        try:
            token: str | None = None
            while True:
                arguments: dict[str, Any] = {
                    "MetricDataQueries": queries,
                    "StartTime": start,
                    "EndTime": start + PERIOD,
                    "ScanBy": "TimestampAscending",
                }
                if token is not None:
                    arguments["NextToken"] = token
                page = self._client.get_metric_data(**arguments)
                for result in page.get("MetricDataResults", []):
                    values = list(result.get("Values", []))
                    if values:
                        gauge = gauges[int(str(result.get("Id", "g0"))[1:])]
                        observed[gauge] = max(float(value) for value in values)
                token = page.get("NextToken")
                if not token:
                    return observed
        except (BotoCoreError, ClientError) as error:
            raise DependencyError(
                _SINK_FAILED,
                "the measurement sink could not be read.",
                context={"cause": type(error).__name__},
            ) from None
