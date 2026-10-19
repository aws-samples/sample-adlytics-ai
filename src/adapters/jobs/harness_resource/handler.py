# SPDX-License-Identifier: MIT-0
"""Lambda entry point for `Custom::AgentCoreHarness` (ADR-077, amended 2026-09-26).

Composition only; the lifecycle is `provider.run`. The function's timeout is 15 minutes and
polling stops `_RESPONSE_MARGIN_SECONDS` before it, so a Harness that is still converging yields
a FAILED response naming its last status instead of a silent timeout that leaves CloudFormation
waiting for the custom resource's service timeout.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import lru_cache
from typing import Final, cast

import boto3
from botocore.config import Config

from adapters.jobs.harness_resource.provider import LOGGER, ProviderRuntime, RunRequest, run
from adapters.outbound.agentcore.harness_control import AgentCoreHarnessControl
from adapters.outbound.cloudformation_response import HttpsCustomResourceResponder
from adapters.outbound.logs.log_group_retention import CloudWatchLogRetention
from adapters.outbound.telemetry.reporting import flushes_metrics

__all__ = ["handler", "remaining_seconds"]

_RESPONSE_MARGIN_SECONDS: Final = 120
"""Room after the last poll for one response PUT and the terminal log (about 13 of 15 min)."""


@lru_cache(maxsize=1)
def _runtime() -> ProviderRuntime:
    retries = Config(retries={"mode": "standard", "total_max_attempts": 5})
    client = boto3.client("bedrock-agentcore-control", config=retries)
    return ProviderRuntime(
        control=AgentCoreHarnessControl(client),
        responder=HttpsCustomResourceResponder(),
        logs=CloudWatchLogRetention(boto3.client("logs", config=retries)),
        clock=time.monotonic,
        sleep=time.sleep,
    )


def remaining_seconds(context: object) -> float:
    """Return the Lambda's remaining time in seconds, or zero when no budget is exposed."""
    method = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(method):
        return 0.0  # no budget: any wait fails at once with a named Reason, and one response goes
    return int(cast("Callable[[], int]", method)()) / 1000


@flushes_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, object], context: object) -> dict[str, str]:
    """Serve one custom-resource callback and answer CloudFormation."""
    runtime = _runtime()
    budget = max(0.0, remaining_seconds(context) - _RESPONSE_MARGIN_SECONDS)
    status = run(RunRequest(event=event, runtime=runtime, deadline=runtime.clock() + budget))
    return {"status": status}
