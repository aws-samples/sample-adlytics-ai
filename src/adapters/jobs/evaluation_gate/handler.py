# SPDX-License-Identifier: MIT-0
"""Lambda entry point for `Custom::AgentCoreOnlineEvaluation`, the evaluation gate (ADR-079).

Composition and the business span only; the lifecycle is `provider.run`. The function's timeout
is 15 minutes and no poll or Evaluate call starts later than `_RESPONSE_MARGIN_SECONDS` before
it, so a configuration still converging, or a calibration still running, yields a FAILED response
naming where it stopped instead of a silent timeout that leaves CloudFormation waiting for the
custom resource's service timeout. The margin covers one in-flight Evaluate at its bounded read
timeout and retries, one response PUT, and the terminal log.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from typing import Final, cast

import boto3
from botocore.config import Config

from adapters.config import tracing_settings
from adapters.jobs.evaluation_gate.provider import LOGGER, ProviderRuntime, RunRequest, run
from adapters.outbound.agentcore.online_evaluation_control import (
    AgentCoreOnlineEvaluationControl,
)
from adapters.outbound.cloudformation_response import HttpsCustomResourceResponder
from adapters.outbound.logs.evaluation_result_group import CloudWatchResultGroupReadback
from adapters.outbound.ssm.evaluation_release_store import SsmEvaluationReleaseStore
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    flushes_metrics,
    report_operation,
)
from adapters.outbound.telemetry.tracing import EVALUATION_GATE, span, trace_boundary
from domain.shared_kernel.errors import AdlyticsError

__all__ = ["handler", "remaining_seconds"]

_RESPONSE_MARGIN_SECONDS: Final = 240
"""About 11 of 15 minutes for work; one Evaluate may run up to two 60 s attempts past it."""

_CONTROL_BOUNDS: Final = Config(
    connect_timeout=10,
    read_timeout=20,
    retries={"mode": "standard", "total_max_attempts": 3},
)
"""Control-plane, Logs, and Parameter Store calls: one call stays well inside the margin."""
_EVALUATE_BOUNDS: Final = Config(
    connect_timeout=10,
    read_timeout=60,
    retries={"mode": "standard", "total_max_attempts": 2},
)

_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
_OPERATION: Final = EVALUATION_GATE

report_operation(
    OperationLog(
        operation=_OPERATION,
        component="evaluation_gate",
        phase="start",
        outcome="active" if _TRACING else "disabled",
        severity=LogSeverity.INFO,
        details={
            "stage": "startup",
            "tracing": _TRACING,
            "sample_ratio": _TRACING_SETTINGS.sample_ratio,
        },
    ),
    LOGGER,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@lru_cache(maxsize=1)
def _runtime() -> ProviderRuntime:
    return ProviderRuntime(
        control=AgentCoreOnlineEvaluationControl(
            boto3.client("bedrock-agentcore-control", config=_CONTROL_BOUNDS),
            boto3.client("bedrock-agentcore", config=_EVALUATE_BOUNDS),
        ),
        readback=CloudWatchResultGroupReadback(boto3.client("logs", config=_CONTROL_BOUNDS)),
        releases=SsmEvaluationReleaseStore(boto3.client("ssm", config=_CONTROL_BOUNDS)),
        responder=HttpsCustomResourceResponder(),
        clock=time.monotonic,
        sleep=time.sleep,
        now=_utc_now,
    )


def remaining_seconds(context: object) -> float:
    """Return the Lambda's remaining time in seconds, or zero when no budget is exposed."""
    method = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(method):
        return 0.0  # no budget: any wait fails at once with a named Reason, and one response goes
    return int(cast("Callable[[], int]", method)()) / 1000


@trace_boundary(EVALUATION_GATE)
@flushes_traces
@flushes_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, object], context: object) -> dict[str, str]:
    """Serve one custom-resource callback and answer CloudFormation."""
    with span(EVALUATION_GATE, operation=_OPERATION) as process_span:
        runtime = _runtime()
        budget = max(0.0, remaining_seconds(context) - _RESPONSE_MARGIN_SECONDS)
        result = run(RunRequest(event=event, runtime=runtime, deadline=runtime.clock() + budget))
        process_span.record(
            outcome=result.status.lower(),
            request_type=result.request_type or None,
            family=result.family or None,
            requested=result.requested,
            execution_status=result.execution_status or None,
            failed_assertion_count=result.failed_assertion_count,
            calibration_case_count=result.calibration_case_count,
            calibration_failure_count=result.calibration_failure_count,
        )
        if isinstance(result.error, AdlyticsError):
            process_span.note_error(result.error)
        elif result.error is not None:
            process_span.note_failure(result.error)
        return {"status": result.status}
