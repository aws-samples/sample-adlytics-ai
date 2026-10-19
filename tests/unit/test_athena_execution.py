# SPDX-License-Identifier: MIT-0
"""Athena statement lifecycle behavior at the timeout boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from adapters.outbound.athena.execution import (
    QUERY_TIMED_OUT,
    StatementOutcome,
    await_completion,
)
from domain.shared_kernel.errors import DependencyError

if TYPE_CHECKING:
    from types_boto3_athena.client import AthenaClient

pytestmark = pytest.mark.unit


class _RunningAthena:
    def __init__(self, *, cancellation_error: Exception | None = None) -> None:
        self.cancellation_error = cancellation_error
        self.cancelled: list[str] = []

    def get_query_execution(self, **arguments: str) -> dict[str, object]:
        del arguments
        return {"QueryExecution": {"Status": {"State": "RUNNING"}}}

    def stop_query_execution(self, **arguments: str) -> dict[str, object]:
        execution_id = arguments["QueryExecutionId"]
        self.cancelled.append(execution_id)
        if self.cancellation_error is not None:
            raise self.cancellation_error
        return {}


def test_timeout_requests_cancellation_before_raising() -> None:
    client = _RunningAthena()

    with pytest.raises(DependencyError) as raised:
        await_completion(cast("AthenaClient", client), "query-timeout", limit=0)

    assert raised.value.code == QUERY_TIMED_OUT
    assert raised.value.context["cancellation_requested"] is True
    assert client.cancelled == ["query-timeout"]


def test_cancellation_failure_remains_a_typed_timeout() -> None:
    client = _RunningAthena(cancellation_error=RuntimeError("unavailable"))

    with pytest.raises(DependencyError) as raised:
        await_completion(cast("AthenaClient", client), "query-timeout", limit=0)

    assert raised.value.code == QUERY_TIMED_OUT
    assert raised.value.context["cancellation_requested"] is False
    assert raised.value.context["cancellation_cause"] == "RuntimeError"
    assert client.cancelled == ["query-timeout"]


class _SucceededAthena:
    def get_query_execution(self, **arguments: str) -> dict[str, object]:
        del arguments
        return {
            "QueryExecution": {
                "Status": {"State": "SUCCEEDED"},
                "Statistics": {
                    "DataScannedInBytes": 4096,
                    "EngineExecutionTimeInMillis": 31,
                    "QueryPlanningTimeInMillis": 7,
                    "QueryQueueTimeInMillis": 5,
                    "TotalExecutionTimeInMillis": 47,
                    "ResultReuseInformation": {"ReusedPreviousResult": True},
                },
            }
        }


def test_completed_statement_reports_total_execution_millis() -> None:
    outcome = await_completion(cast("AthenaClient", _SucceededAthena()), "query-complete", limit=0)

    assert outcome == StatementOutcome(
        bytes_scanned=4096,
        reused_previous_result=True,
        engine_millis=31,
        planning_millis=7,
        queue_millis=5,
        total_millis=47,
    )
