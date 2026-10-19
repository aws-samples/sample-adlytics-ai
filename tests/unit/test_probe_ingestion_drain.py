# SPDX-License-Identifier: MIT-0
"""The drain probe's span figures, read through the 5a span-retrieval module (plan B6)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_ingestion_drain as probe  # noqa: E402

pytestmark = pytest.mark.unit

_STARTED = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)
_ENDED = _STARTED + timedelta(minutes=10)


def _message(name: str, index: int, **fields: object) -> list[dict[str, str]]:
    span = {
        "traceId": f"{index:032x}",
        "spanId": f"{index:016x}",
        "name": name,
        "resource": {"attributes": {"deployment.environment": "dev"}},
        **fields,
    }
    return [{"field": "@message", "value": json.dumps(span)}]


class _FakeLogs:
    def __init__(self, rows: list[list[dict[str, str]]], status: str = "Complete") -> None:
        self.rows, self.status, self.queries = rows, status, list[str]()

    def start_query(self, **request: Any) -> dict[str, str]:
        self.queries.append(str(request["queryString"]))
        return {"queryId": "query-1"}

    def get_query_results(self, **_request: Any) -> dict[str, object]:
        return {"status": self.status, "results": self.rows}


def test_business_span_latency_is_computed_per_name_from_the_retrieved_spans() -> None:
    rows = [
        _message("ingest.fetch", 1, durationNano=str(100_000_000)),
        _message("ingest.fetch", 2, durationNano=str(300_000_000)),
        _message("ingest.parse", 3, durationNano=50_000_000),
    ]
    logs = _FakeLogs(rows)
    report = probe._span_latency(cast("Any", logs), _STARTED, _ENDED)  # pyright: ignore[reportPrivateUsage]
    assert report["status"] == "complete"
    assert report["by_name"] == {
        "ingest.fetch": {"spans": 2, "p50_ms": 100.0, "p95_ms": 300.0, "max_ms": 300.0},
        "ingest.parse": {"spans": 1, "p50_ms": 50.0, "p95_ms": 50.0, "max_ms": 50.0},
    }
    assert "name = 'ingest.fetch'" in logs.queries[0]


def test_athena_statement_latency_sums_the_declared_timings() -> None:
    timings = {"total_millis": 40, "queue_millis": 5, "planning_millis": 10, "engine_millis": 25}
    logs = _FakeLogs([_message("athena.statement", 1, attributes=timings)])
    report = probe._athena_latency(cast("Any", logs), _STARTED, _ENDED)  # pyright: ignore[reportPrivateUsage]
    assert (report["statements"], report["p95_total_ms"]) == (1, 40.0)
    assert (report["queue_ms"], report["planning_ms"], report["engine_ms"]) == (5.0, 10.0, 25.0)


def test_a_failed_span_query_is_a_probe_error_and_a_completed_one_is_not() -> None:
    with pytest.raises(probe.ProbeError, match="performance span retrieval failed"):
        probe._span_latency(cast("Any", _FakeLogs([], "Failed")), _STARTED, _ENDED)  # pyright: ignore[reportPrivateUsage]
    # Control: the same read completing returns its (empty) figures.
    empty = probe._span_latency(cast("Any", _FakeLogs([])), _STARTED, _ENDED)  # pyright: ignore[reportPrivateUsage]
    assert empty["by_name"] == {}
