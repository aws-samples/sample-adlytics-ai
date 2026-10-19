# SPDX-License-Identifier: MIT-0
"""O-SEMANTIC-WRITE-ACK-001, read-only: judge one UTC window's semantic-write acknowledgements.

Was every unit message acknowledged, with nothing dead-lettered and nothing left in flight?
    uv run --frozen python validation/probe_semantic_write_ack.py --profile <profile> \
        --region <region> --expected-account <account> --environment dev \
        --start <iso> --end <iso> --output <file>.json
Run it under `uv run --frozen python`: the system `python3` may predate `datetime.UTC`.
Reads CloudWatch and SQS only. Over the window it reads the semantic-write FIFO's sent,
deduplicated, received and deleted sums, both queues' approximate depths (start, maximum), the
TableWriter's invocations and errors, and every `SemanticUnitReceives` series by its `Outcome` and
`FinalReceive` dimensions. Then it reads both queues' live depths. Results join their queries
by `Id` across every `NextToken` page, never by position.
The DLQ's `NumberOfMessagesSent` does not count a message moved there by its redrive policy
(SQS troubleshooting guide), so DLQ growth is read from its depth series and live depth.
Verdict: PASS, FAIL (loss evidence) or NOT_PROVEN (the evidence can't decide). Exit 0 only
on PASS.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

SCHEMA: Final = "semantic-write-ack-v2"
PERIOD_SECONDS: Final = 60
UNIT_METRIC: Final = "SemanticUnitReceives"
UNIT_NAMESPACE: Final = "Adlytics"
SUCCESS_OUTCOMES: Final = frozenset(
    {
        "attested",
        "attested_replay",
        "graph_already_settled",
        "graph_publish_succeeded",
        "graph_unit_batch_settled",
        "graph_unit_succeeded",
        "legacy_replay_acknowledged",
        "plan_completed",
    }
)
"""Outcomes that settle the unit as written. A final receive with any other outcome is a
non-success: either it is returned and goes to the DLQ, or it acknowledges a failed unit."""
DEPTHS: Final = (
    "ApproximateNumberOfMessagesVisible",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
)
FLOWS: Final = (
    "NumberOfMessagesSent",
    "NumberOfMessagesReceived",
    "NumberOfMessagesDeleted",
    "NumberOfDeduplicatedSentMessages",
)
"""`NumberOfMessagesSent` counts a FIFO send SQS then deduplicates (dev, 2026-10-17 02:09Z:
sent 2, deduplicated 1, from one two-send Dispatcher pass), so enqueued = sent - deduplicated."""


@dataclass(frozen=True, slots=True)
class Series:
    """One CloudWatch query: its `Id`, metric, dimensions and statistic."""

    id: str
    namespace: str
    metric: str
    dimensions: Mapping[str, str]
    stat: str

    def query(self) -> dict[str, Any]:
        """Return the `MetricDataQueries` entry for this series."""
        return {
            "Id": self.id,
            "ReturnData": True,
            "MetricStat": {
                "Metric": {
                    "Namespace": self.namespace,
                    "MetricName": self.metric,
                    "Dimensions": [{"Name": k, "Value": v} for k, v in self.dimensions.items()],
                },
                "Period": PERIOD_SECONDS,
                "Stat": self.stat,
            },
        }


@dataclass(slots=True)
class Points:
    """Every datapoint one series returned, across pages, and its last page's status."""

    values: list[tuple[datetime, float]] = field(default_factory=list[tuple[datetime, float]])
    status: str = "Absent"

    def total(self) -> float:
        """Return the sum of every datapoint."""
        return sum(value for _, value in self.values)

    def peak(self) -> float | None:
        """Return the largest datapoint's value, or None when the series returned none."""
        return max(value for _, value in self.values) if self.values else None

    def last_active(self) -> str | None:
        """Return the latest timestamp with a non-zero value."""
        active = [moment for moment, value in self.values if value]
        return max(active).astimezone(UTC).isoformat() if active else None


def names(environment: str) -> dict[str, str]:
    """Return the exact resource names the ingestion stack derives for one environment."""
    return {
        "queue": f"adlytics-{environment}-semantic-write.fifo",
        "dlq": f"adlytics-{environment}-semantic-write-dlq.fifo",
        "writer": f"adlytics-{environment}-TableWriter",
    }


def plan_series(environment: str, unit_dimensions: Iterable[Mapping[str, str]]) -> list[Series]:
    """Every series the verdict reads; `unit_dimensions` are the listed unit-receive series."""
    resource = names(environment)
    series: list[Series] = []

    def add(namespace: str, metric: str, dimensions: Mapping[str, str], *, stat: str) -> None:
        series.append(Series(f"m{len(series)}", namespace, metric, dict(dimensions), stat))

    for role in ("queue", "dlq"):
        for metric in FLOWS:
            add("AWS/SQS", metric, {"QueueName": resource[role]}, stat="Sum")
        for metric in DEPTHS:
            add("AWS/SQS", metric, {"QueueName": resource[role]}, stat="Maximum")
    for metric in ("Invocations", "Errors"):
        add("AWS/Lambda", metric, {"FunctionName": resource["writer"]}, stat="Sum")
    for dimensions in unit_dimensions:
        add(UNIT_NAMESPACE, UNIT_METRIC, dimensions, stat="Sum")
    return series


def collect(
    fetch: Callable[..., Mapping[str, Any]],
    series: Sequence[Series],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Points]:
    """Read every page of `GetMetricData` and join each result to its query by `Id`."""
    points = {item.id: Points() for item in series}
    request: dict[str, Any] = {
        "MetricDataQueries": [item.query() for item in series],
        "StartTime": start,
        "EndTime": end,
        "ScanBy": "TimestampAscending",
    }
    while True:
        page = fetch(**request)
        for result in page.get("MetricDataResults", []):
            target = points.get(str(result["Id"]))
            if target is None:
                raise ValueError(f"GetMetricData returned an unknown Id {result['Id']!r}")
            target.values.extend(
                zip(result.get("Timestamps", []), result.get("Values", []), strict=True)
            )
            target.status = str(result.get("StatusCode", "Absent"))
        token = page.get("NextToken")
        if not token:
            return points
        request["NextToken"] = token


@dataclass(frozen=True, slots=True)
class LiveDepth:
    """One queue's three live approximate depths."""

    visible: int
    in_flight: int
    delayed: int

    @property
    def total(self) -> int:
        """Messages in any state."""
        return self.visible + self.in_flight + self.delayed


def _pick(series: Sequence[Series], points: Mapping[str, Points], **match: str) -> Points:
    for item in series:
        values = {"metric": item.metric, **item.dimensions}
        if all(values.get(key) == value for key, value in match.items()):
            return points[item.id]
    return Points()


def judge(  # noqa: PLR0912, PLR0915 -- one flat list of named checks reads best as one function
    environment: str,
    series: Sequence[Series],
    *,
    points: Mapping[str, Points],
    opening: Mapping[str, Points],
    live: Mapping[str, LiveDepth],
) -> dict[str, Any]:
    """Return the counts and the verdict with every reason that kept it from PASS.

    `opening` holds the depth series over the two periods that straddle the window's start; its
    peak is the opening depth, so a backlog that drains early still reads as non-empty.
    """
    resource = names(environment)
    queue, dlq = resource["queue"], resource["dlq"]

    def flow(name: str, metric: str) -> Points:
        return _pick(series, points, metric=metric, QueueName=name)

    sent = flow(queue, "NumberOfMessagesSent").total()
    deduplicated = flow(queue, "NumberOfDeduplicatedSentMessages").total()
    enqueued = sent - deduplicated
    received = flow(queue, "NumberOfMessagesReceived").total()
    deleted = flow(queue, "NumberOfMessagesDeleted").total()
    start_depth = [_pick(series, opening, metric=m, QueueName=queue).peak() for m in DEPTHS]
    dlq_start = [_pick(series, opening, metric=m, QueueName=dlq).peak() for m in DEPTHS]
    dlq_peak = [flow(dlq, metric).peak() for metric in DEPTHS]
    invocations = _pick(series, points, metric="Invocations").total()
    errors = _pick(series, points, metric="Errors").total()
    receives: list[dict[str, Any]] = []
    for item in series:
        if item.metric == UNIT_METRIC and points[item.id].total():
            receives.append({**item.dimensions, "sum": points[item.id].total()})
    outcome_sum = sum(float(row["sum"]) for row in receives)
    fail: list[str] = []
    unproven: list[str] = []
    incomplete = sorted(
        {
            p.status
            for p in (*points.values(), *opening.values())
            if p.status not in {"Complete", "Absent"}
        }
    )
    if incomplete:
        unproven.append(f"metric results not complete: {incomplete}")
    for row in receives:
        if row.get("FinalReceive") == "true" and row.get("Outcome") not in SUCCESS_OUTCOMES:
            fail.append(
                f"final receive with non-success outcome {row.get('Outcome')}: {row['sum']:g}"
            )
        elif row.get("Outcome") == "unclassified":
            fail.append(f"unclassified outcome: {row['sum']:g}")
    if None in start_depth:
        unproven.append("main queue start depth not reported")
    elif sum(v or 0 for v in start_depth):
        unproven.append(
            f"main queue held {sum(v or 0 for v in start_depth):g} messages at window start"
        )
    if live[queue].total:
        unproven.append(f"main queue holds {live[queue].total} messages now (in flight, not loss)")
    if None in dlq_start or None in dlq_peak:
        unproven.append("DLQ depth series not reported")
    else:
        baseline = sum(v or 0 for v in dlq_start)
        peak = sum(v or 0 for v in dlq_peak)
        if live[dlq].total > baseline:
            fail.append(f"DLQ grew: baseline {baseline:g}, now {live[dlq].total}")
        elif peak > baseline:
            # Dead-lettered inside the window, then removed: the unit's fate is unknown.
            unproven.append(f"DLQ peaked at {peak:g} above baseline {baseline:g}, now back")
    if enqueued != deleted:
        closed = (
            None not in start_depth
            and not sum(v or 0 for v in start_depth)
            and not live[queue].total
        )
        target = fail if closed else unproven
        target.append(f"enqueued {enqueued:g} != deleted {deleted:g}")
    if received > enqueued:
        unproven.append(
            f"received {received:g} > enqueued {enqueued:g} (redelivery; needs per-message join)"
        )
    elif received < enqueued:
        unproven.append(
            f"received {received:g} < enqueued {enqueued:g} (not every unit was received)"
        )
    if not sent and not outcome_sum:
        unproven.append("no semantic-write traffic in the window")
    if outcome_sum != invocations or outcome_sum != received:
        unproven.append(
            f"unit outcomes {outcome_sum:g}, invocations {invocations:g}, "
            f"received {received:g} differ"
        )
    verdict = "FAIL" if fail else "NOT_PROVEN" if unproven else "PASS"
    return {
        "verdict": verdict,
        "reasons": fail + unproven,
        "queue": {
            "sent": sent,
            "deduplicated": deduplicated,
            "enqueued": enqueued,
            "received": received,
            "deleted": deleted,
            "depth_at_start": dict(zip(DEPTHS, start_depth, strict=True)),
            "last_sent_at": flow(queue, "NumberOfMessagesSent").last_active(),
            "last_deleted_at": flow(queue, "NumberOfMessagesDeleted").last_active(),
            "depth_now": vars_of(live[queue]),
        },
        "dlq": {
            "baseline_at_start": dict(zip(DEPTHS, dlq_start, strict=True)),
            "window_peak": dict(zip(DEPTHS, dlq_peak, strict=True)),
            "depth_now": vars_of(live[dlq]),
            "sent": flow(dlq, "NumberOfMessagesSent").total(),
            "received": flow(dlq, "NumberOfMessagesReceived").total(),
            "deleted": flow(dlq, "NumberOfMessagesDeleted").total(),
        },
        "writer": {"invocations": invocations, "errors": errors},
        "unit_receives": sorted(
            receives, key=lambda row: (row.get("FinalReceive", ""), row.get("Outcome", ""))
        ),
        "unit_receive_sum": outcome_sum,
    }


def vars_of(depth: LiveDepth) -> dict[str, int]:
    """Return a live depth as a plain mapping."""
    return {"visible": depth.visible, "in_flight": depth.in_flight, "delayed": depth.delayed}


LIVE_ATTRIBUTES: Final = (
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
)
"""The queue attributes behind the three depths; SQS names the visible one without `Visible`."""


def live_depth(sqs: Any, queue_name: str) -> LiveDepth:
    """Read one queue's three live approximate depths."""
    url = sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
    attributes = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=list(LIVE_ATTRIBUTES))
    visible, in_flight, delayed = (int(attributes["Attributes"][n]) for n in LIVE_ATTRIBUTES)
    return LiveDepth(visible, in_flight, delayed)


def _instant(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{text!r} names no UTC offset")
    return moment.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    """Read the window, judge it and write the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("--profile", "--region", "--expected-account", "--environment", "--output"):
        parser.add_argument(name, required=True)
    parser.add_argument("--start", required=True, type=_instant)
    parser.add_argument("--end", required=True, type=_instant)
    arguments = parser.parse_args(argv)
    import boto3

    session = boto3.Session(profile_name=arguments.profile, region_name=arguments.region)
    if session.client("sts").get_caller_identity()["Account"] != arguments.expected_account:
        print("NOT_PROVEN: the explicit profile is not the expected account", file=sys.stderr)
        return 2
    cloudwatch = session.client("cloudwatch")
    unit_dimensions = [
        {d["Name"]: d["Value"] for d in metric.get("Dimensions", [])}
        for page in cloudwatch.get_paginator("list_metrics").paginate(
            Namespace=UNIT_NAMESPACE, MetricName=UNIT_METRIC
        )
        for metric in page["Metrics"]
    ]
    series = plan_series(arguments.environment, unit_dimensions)
    points = collect(cloudwatch.get_metric_data, series, start=arguments.start, end=arguments.end)
    period = timedelta(seconds=PERIOD_SECONDS)
    depths = [item for item in series if item.metric in DEPTHS]
    opening = collect(
        cloudwatch.get_metric_data,
        depths,
        start=arguments.start - period,
        end=arguments.start + period,
    )
    sqs = session.client("sqs")
    resource = names(arguments.environment)
    live = {resource[role]: live_depth(sqs, resource[role]) for role in ("queue", "dlq")}
    report = {
        "schema_version": SCHEMA,
        "window": [arguments.start.isoformat(), arguments.end.isoformat()],
        "read_at": datetime.now(UTC).isoformat(),
        **judge(arguments.environment, series, points=points, opening=opening, live=live),
    }
    Path(arguments.output).write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(f"{report['verdict']}: {report['reasons']}")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
