# SPDX-License-Identifier: MIT-0
"""Athena statement lifecycle behavior at the timeout boundary."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any, cast

import boto3
import pytest
from botocore.stub import Stubber

from adapters.outbound.athena.execution import (
    QUERY_FAILED,
    QUERY_TIMED_OUT,
    StartedStatement,
    StatementOutcome,
    StatementRequest,
    await_completion,
    start_counted,
)
from adapters.outbound.athena.query import (
    BAKE_INVOCATION_WAIT,
    INTERACTIVE_WAIT,
    THROTTLED_START_MAX_WAIT_SECONDS,
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


# --- ADR-144: the throttled-start retry ------------------------------------------------------


class _Clock:
    """A monotonic clock that only moves when the retry sleeps."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _stubbed(throttles: int, *, then: str | None = "query-started") -> tuple[Any, Stubber]:
    client = boto3.client("athena", region_name="us-east-1")
    stubber = Stubber(client)
    for _ in range(throttles):
        stubber.add_client_error(
            "start_query_execution",
            service_error_code="TooManyRequestsException",
            service_message="Rate exceeded",
            http_status_code=400,
        )
    if then is not None:
        stubber.add_response("start_query_execution", {"QueryExecutionId": then})
    stubber.activate()
    return client, stubber


_REQUEST = StatementRequest("SELECT 1", (), "fictional-bake", "fictional-current", 0)


def _jitter() -> random.Random:
    return random.Random(7)  # noqa: S311 -- a seeded test jitter, not a secret


def _start(client: Any, clock: _Clock, until: float | None) -> StartedStatement:
    return start_counted(
        client,
        _REQUEST,
        retry_throttled_until=until,
        random_source=_jitter(),
        clock=clock,
        sleep=clock.sleep,
    )


def test_three_throttles_then_success_returns_the_id_after_full_jitter_sleeps() -> None:
    client, stubber = _stubbed(3)
    clock = _Clock()
    started = _start(client, clock, clock.now + THROTTLED_START_MAX_WAIT_SECONDS)
    assert started == StartedStatement("query-started", 3)
    expected = _jitter()
    caps = [0.5, 1.0, 2.0]
    assert clock.slept == [expected.uniform(0.0, cap) for cap in caps]
    assert all(0.0 <= pause <= cap for pause, cap in zip(clock.slept, caps, strict=True))
    stubber.assert_no_pending_responses()


def test_the_backoff_cap_is_eight_seconds() -> None:
    client, _ = _stubbed(8)
    clock = _Clock()
    _start(client, clock, clock.now + 1_000.0)
    assert len(clock.slept) == 8 and max(clock.slept) <= 8.0
    expected = _jitter()
    caps = [min(8.0, 0.5 * 2**attempt) for attempt in range(8)]
    assert clock.slept == [expected.uniform(0.0, cap) for cap in caps]


@pytest.mark.parametrize("window", [THROTTLED_START_MAX_WAIT_SECONDS, 3.0])
def test_a_throttle_outlasting_its_window_raises_with_its_attempts(window: float) -> None:
    """Past 60 s, and past a deadline that comes sooner (the bake's min(deadline, 60 s))."""
    client, _ = _stubbed(40, then=None)
    clock = _Clock()
    until = clock.now + window
    with pytest.raises(DependencyError) as raised:
        _start(client, clock, until)
    assert raised.value.code == QUERY_FAILED
    context = raised.value.context
    assert context["aws_code"] == "TooManyRequestsException"
    attempts = context["throttled_attempts"]
    assert isinstance(attempts, int) and attempts == len(clock.slept) + 1
    assert clock.now < until  # it never slept past the window


def test_another_client_error_raises_at_once() -> None:
    client = boto3.client("athena", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_client_error(
        "start_query_execution", service_error_code="InvalidRequestException", http_status_code=400
    )
    stubber.activate()
    clock = _Clock()
    with pytest.raises(DependencyError) as raised:
        _start(client, clock, clock.now + THROTTLED_START_MAX_WAIT_SECONDS)
    assert raised.value.context["aws_code"] == "InvalidRequestException"
    assert "throttled_attempts" not in raised.value.context and clock.slept == []


def test_no_window_never_retries_a_throttle() -> None:
    client, _ = _stubbed(1)
    clock = _Clock()
    with pytest.raises(DependencyError) as raised:
        _start(client, clock, None)
    assert raised.value.context["aws_code"] == "TooManyRequestsException"
    assert "throttled_attempts" not in raised.value.context and clock.slept == []


def test_the_interactive_window_retries_for_ten_seconds_and_the_bake_for_sixty() -> None:
    """A7 option (1): chat retries a throttled start for 10 s; the bake for 60 s."""
    from adapters.outbound.athena.deadline import (  # noqa: PLC0415
        reset_bake_statement_deadline,
        set_bake_statement_deadline,
    )

    assert INTERACTIVE_WAIT.throttled_start_until(100.0) == 110.0
    token = set_bake_statement_deadline(500.0)
    try:
        assert BAKE_INVOCATION_WAIT.throttled_start_until(100.0) == 160.0
        # The deadline binds first when it is sooner.
        assert BAKE_INVOCATION_WAIT.throttled_start_until(480.0) == 500.0
    finally:
        reset_bake_statement_deadline(token)
    client, _ = _stubbed(2)
    clock = _Clock()
    assert (
        _start(client, clock, INTERACTIVE_WAIT.throttled_start_until(clock.now)).throttled_attempts
        == 2
    )
    # Paired: a throttle outlasting chat's 10 s fails, as a sustained overload should.
    client, _ = _stubbed(40, then=None)
    clock = _Clock()
    with pytest.raises(DependencyError):
        _start(client, clock, INTERACTIVE_WAIT.throttled_start_until(clock.now))
    assert clock.now < 1_010.0


def test_a_named_wait_policy_timeout_carries_its_policy_waited_and_limit() -> None:
    client = _RunningAthena()
    with pytest.raises(DependencyError) as raised:
        await_completion(
            cast("AthenaClient", client), "query-timeout", limit=0, wait_policy="bake_invocation"
        )
    context = raised.value.context
    assert raised.value.code == QUERY_TIMED_OUT
    assert context["wait_policy"] == "bake_invocation" and context["wait_limit_seconds"] == 0
    assert isinstance(context["waited_seconds"], float)
    # Control: an unnamed caller's timeout context is unchanged.
    with pytest.raises(DependencyError) as unnamed:
        await_completion(cast("AthenaClient", client), "query-timeout", limit=0)
    assert "wait_policy" not in unnamed.value.context
    assert unnamed.value.context["waited_seconds"] == 0
