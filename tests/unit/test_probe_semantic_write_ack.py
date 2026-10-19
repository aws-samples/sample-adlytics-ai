# SPDX-License-Identifier: MIT-0
"""The O-SEMANTIC-WRITE-ACK-001 probe joins by Id across pages and judges its verdict.

Covers the D2 pre-flight audit's S2 (in-flight, DLQ and final non-success receives) and S3
(results joined by `Id` over every `NextToken` page).
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import probe_semantic_write_ack as probe  # noqa: E402

pytestmark = [pytest.mark.unit]
_ENV = "dev"
_T0 = datetime(2026, 10, 16, 20, 50, tzinfo=UTC)
_QUEUE = "adlytics-dev-semantic-write.fifo"
_DLQ = "adlytics-dev-semantic-write-dlq.fifo"
_OK = {"Outcome": "graph_unit_succeeded", "FinalReceive": "false"}


def _points(
    series: list[probe.Series], values: Mapping[tuple[str, str], list[float]]
) -> dict[str, probe.Points]:
    """Datapoints by (metric, queue-or-outcome); every other series reads one zero point."""
    points: dict[str, probe.Points] = {}
    for item in series:
        key = (item.metric, item.dimensions.get("QueueName") or item.dimensions.get("Outcome", ""))
        raw = values.get(key, [0.0])
        points[item.id] = probe.Points(
            [(_T0 + timedelta(minutes=i), v) for i, v in enumerate(raw)], "Complete"
        )
    return points


def _clean(n: float = 3.0) -> dict[tuple[str, str], list[float]]:
    return {
        ("NumberOfMessagesSent", _QUEUE): [n],
        ("NumberOfMessagesReceived", _QUEUE): [n],
        ("NumberOfMessagesDeleted", _QUEUE): [n],
        ("Invocations", ""): [n],
        (probe.UNIT_METRIC, "graph_unit_succeeded"): [n],
    }


def _quiet(
    series: list[probe.Series], values: Mapping[tuple[str, str], list[float]] | None = None
) -> dict[str, probe.Points]:
    """Return the opening read: the depth series over the two periods around the start."""
    depths = [item for item in series if item.metric in probe.DEPTHS]
    return _points(depths, values or {})


def _empty() -> dict[str, probe.LiveDepth]:
    return {_QUEUE: probe.LiveDepth(0, 0, 0), _DLQ: probe.LiveDepth(0, 0, 0)}


def test_collect_joins_by_id_across_pages() -> None:
    """S3: results arrive out of query order and over two pages; each joins its own Id."""
    series = probe.plan_series(_ENV, [_OK])
    sent, deleted = series[0].id, series[2].id
    pages: list[dict[str, Any]] = [
        {
            "MetricDataResults": [
                {"Id": deleted, "Timestamps": [_T0], "Values": [1.0], "StatusCode": "PartialData"},
                {"Id": sent, "Timestamps": [_T0], "Values": [5.0], "StatusCode": "PartialData"},
            ],
            "NextToken": "page-2",
        },
        {
            "MetricDataResults": [
                {
                    "Id": deleted,
                    "Timestamps": [_T0 + timedelta(minutes=1)],
                    "Values": [4.0],
                    "StatusCode": "Complete",
                },
            ]
        },
    ]
    requests: list[dict[str, Any]] = []

    def fetch(**request: Any) -> dict[str, Any]:
        requests.append(dict(request))
        return pages[len(requests) - 1]

    points = probe.collect(fetch, series, start=_T0, end=_T0 + timedelta(hours=1))
    assert points[sent].total() == 5.0
    assert points[deleted].total() == 5.0
    assert points[deleted].status == "Complete"
    assert [r.get("NextToken") for r in requests] == [None, "page-2"]


def test_clean_window_passes() -> None:
    series = probe.plan_series(_ENV, [_OK])
    report = probe.judge(
        _ENV, series, points=_points(series, _clean()), opening=_quiet(series), live=_empty()
    )
    assert report["verdict"] == "PASS", report["reasons"]


def test_deduplicated_resend_is_not_loss() -> None:
    """A byte-identical resend SQS deduplicates is counted as sent but never enqueued."""
    series = probe.plan_series(_ENV, [_OK])
    values = _clean() | {
        ("NumberOfMessagesSent", _QUEUE): [3.0, 1.0],
        ("NumberOfDeduplicatedSentMessages", _QUEUE): [0.0, 1.0],
    }
    report = probe.judge(
        _ENV, series, points=_points(series, values), opening=_quiet(series), live=_empty()
    )
    assert report["verdict"] == "PASS", report["reasons"]
    assert report["queue"]["enqueued"] == 3


def test_in_flight_at_window_end_is_not_proven_not_loss() -> None:
    """S2: a message still in flight at the end is neither PASS nor counted as loss."""
    series = probe.plan_series(_ENV, [_OK])
    values = _clean() | {("NumberOfMessagesDeleted", _QUEUE): [2.0]}
    live = _empty() | {_QUEUE: probe.LiveDepth(0, 1, 0)}
    report = probe.judge(
        _ENV, series, points=_points(series, values), opening=_quiet(series), live=live
    )
    assert report["verdict"] == "NOT_PROVEN"
    assert any("in flight" in reason for reason in report["reasons"])


def test_undeleted_message_with_empty_queues_fails() -> None:
    series = probe.plan_series(_ENV, [_OK])
    values = _clean() | {("NumberOfMessagesDeleted", _QUEUE): [2.0]}
    report = probe.judge(
        _ENV, series, points=_points(series, values), opening=_quiet(series), live=_empty()
    )
    assert report["verdict"] == "FAIL"


def test_dlq_growth_reads_depth_not_sent() -> None:
    """A redrive is not counted in the DLQ's NumberOfMessagesSent, so its depth decides."""
    series = probe.plan_series(_ENV, [_OK])
    values = _clean() | {("ApproximateNumberOfMessagesVisible", _DLQ): [0.0, 1.0]}
    live = _empty() | {_DLQ: probe.LiveDepth(1, 0, 0)}
    report = probe.judge(
        _ENV, series, points=_points(series, values), opening=_quiet(series), live=live
    )
    assert report["verdict"] == "FAIL"
    assert report["dlq"]["sent"] == 0


def test_dlq_peak_that_drained_is_not_proven_not_fail() -> None:
    """Review R1 HIGH: a dead-lettered message later removed has an unknown fate."""
    series = probe.plan_series(_ENV, [_OK])
    values = _clean() | {("ApproximateNumberOfMessagesVisible", _DLQ): [0.0, 1.0, 0.0]}
    report = probe.judge(
        _ENV, series, points=_points(series, values), opening=_quiet(series), live=_empty()
    )
    assert report["verdict"] == "NOT_PROVEN"


def test_backlog_at_open_that_drains_early_is_not_proven_not_fail() -> None:
    """Review R1 MEDIUM: the opening read straddles the start, so an early drain still counts."""
    series = probe.plan_series(_ENV, [_OK])
    values = _clean() | {("NumberOfMessagesDeleted", _QUEUE): [5.0]}
    opening = _quiet(series, {("ApproximateNumberOfMessagesVisible", _QUEUE): [2.0, 0.0]})
    report = probe.judge(
        _ENV, series, points=_points(series, values), opening=opening, live=_empty()
    )
    assert report["verdict"] == "NOT_PROVEN"
    assert any("at window start" in reason for reason in report["reasons"])


def test_window_without_traffic_is_not_proven() -> None:
    """Review R1 LOW: an empty window is no evidence of acknowledgement."""
    series = probe.plan_series(_ENV, [_OK])
    report = probe.judge(
        _ENV, series, points=_points(series, {}), opening=_quiet(series), live=_empty()
    )
    assert report["verdict"] == "NOT_PROVEN"


def test_final_receive_with_non_success_outcome_fails() -> None:
    final = {"Outcome": "statement_retryable", "FinalReceive": "true"}
    series = probe.plan_series(_ENV, [_OK, final])
    values = _clean() | {
        (probe.UNIT_METRIC, "statement_retryable"): [1.0],
        (probe.UNIT_METRIC, "graph_unit_succeeded"): [2.0],
    }
    report = probe.judge(
        _ENV, series, points=_points(series, values), opening=_quiet(series), live=_empty()
    )
    assert report["verdict"] == "FAIL"
    assert any("statement_retryable" in reason for reason in report["reasons"])


def test_redelivery_is_not_proven() -> None:
    """Received above sent means a redelivery: the window counts can't prove each unit's fate."""
    series = probe.plan_series(_ENV, [_OK])
    values = _clean() | {
        ("NumberOfMessagesReceived", _QUEUE): [4.0],
        ("Invocations", ""): [4.0],
        (probe.UNIT_METRIC, "graph_unit_succeeded"): [4.0],
    }
    report = probe.judge(
        _ENV, series, points=_points(series, values), opening=_quiet(series), live=_empty()
    )
    assert report["verdict"] == "NOT_PROVEN"


class _Sqs:
    """Answer only the attribute names `GetQueueAttributes` accepts."""

    known = frozenset(probe.LIVE_ATTRIBUTES)

    def get_queue_url(self, **_: Any) -> dict[str, str]:
        return {"QueueUrl": "https://sqs.invalid/q"}

    def get_queue_attributes(self, **request: Any) -> dict[str, Any]:
        names: list[str] = request["AttributeNames"]
        unknown = set(names) - self.known
        if unknown:
            raise ValueError(f"InvalidAttributeName: {sorted(unknown)}")
        values: dict[str, str] = dict(zip(probe.LIVE_ATTRIBUTES, ("2", "1", "0"), strict=True))
        return {"Attributes": {name: values[name] for name in names}}


def test_live_depth_requests_the_queue_attribute_names() -> None:
    """The visible depth is `ApproximateNumberOfMessages`, not the CloudWatch metric's name."""
    assert probe.live_depth(_Sqs(), _QUEUE) == probe.LiveDepth(2, 1, 0)
