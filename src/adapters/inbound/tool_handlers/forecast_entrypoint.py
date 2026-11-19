# SPDX-License-Identifier: MIT-0
"""The Lambda entry point for the four demand forecast tools (ADR-163).

A separate function from catalog and measurement because its permissions differ: it may only
`GetItem` the forecast table's `SET#` and `GEN#` keys. It mirrors `catalog_entrypoint.py` step for
step, with one change: every failure returns a typed refusal, never a re-raise, so a malformed
invocation, an unknown tool and an unexpected error each reach the agent with a code (R20).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final, Protocol

import boto3
from aws_lambda_powertools import Logger

from adapters.config import (
    demand_forecast_reader,
    settings_without_client_secret,
    tool_caller_settings,
    tracing_settings,
)
from adapters.inbound.forecast_read.service import ForecastReadService, ReadContext
from adapters.inbound.tool_handlers import gateway
from adapters.inbound.tool_handlers.caller import CallerVerifier, kms_caller_verifier
from adapters.inbound.tool_handlers.forecast import ForecastTool, ForecastToolRequest, handle
from adapters.outbound.demand_forecast_policy import load_policy
from adapters.outbound.dynamodb.demand_forecast_store import (
    READER_CLIENT_CONFIG,
    DynamoDbForecastStore,
)
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    INVALID_TOOL,
    METRICS,
    UNIDENTIFIED_TOOL,
    UNVERIFIED_CALLER,
    GatewayInvestigationNext,
    GatewayResultClass,
    GatewayTerminalLog,
    Outcome,
    Report,
    normalize_correlation_id,
    report_error,
    report_invocation,
    report_lifecycle,
)
from adapters.outbound.telemetry.tracing import (
    FORECAST_GATEWAY,
    Recording,
    span,
    tool_span,
    trace_boundary,
)
from domain.forecasting import read_api
from domain.narrative.publication import grounded_refusal
from domain.shared_kernel.errors import AdlyticsError, InvariantError, ValidationError

__all__ = ["handler"]

LOGGER: Final = Logger(service="adlytics-forecast-tool")
TRACING = configure_tracing(tracing_settings())
CALLER: CallerVerifier = kms_caller_verifier(tool_caller_settings())
"""Verifies every call's caller before anything else is read (ADR-077 §5)."""


def _service() -> ForecastReadService:
    values = demand_forecast_reader(settings_without_client_secret())
    root = gateway.contracts_root()
    policy = load_policy(root / "contracts/forecasting/demand-forecast.yaml").policy
    client = boto3.client("dynamodb", config=READER_CLIENT_CONFIG)
    store = DynamoDbForecastStore(values.forecast_table, policy.serving.item_max_bytes, client)
    return ForecastReadService(store, values, policy)


SERVICE: Final = _service()


class _Clock(Protocol):
    def get_remaining_time_in_millis(self) -> int: ...


def _terminal(
    operation: str, outcome: Outcome, result_class: GatewayResultClass, **where: Any
) -> None:
    report_lifecycle(
        GatewayTerminalLog(
            operation=operation,
            outcome=outcome,
            result_class=result_class,
            investigation_next=where.pop(
                "investigation_next", GatewayInvestigationNext.TOOL_BOUNDARY
            ),
            gateway_message_ref=where["gateway_message_ref"],
            channel=where["channel"],
            caller_verified=where["caller_verified"],
        ),
        LOGGER,
    )


def _handle(event: dict[str, Any], context: Any, process_span: Recording) -> dict[str, Any]:
    """Answer one forecast tool call, or return its typed refusal; the caller is verified first.

    An answer records its plan's `items_read` on the process span (`tool.forecast_gateway`).
    """
    message_ref = gateway.gateway_message_ref(context)
    channel = gateway.channel_keys(event, context)
    caller_report = Report(
        operation=UNVERIFIED_CALLER,
        correlation_id="",
        logger=LOGGER,
        gateway_message_ref=message_ref,
    )
    admission = CALLER.admit(context, caller_report)
    if admission.refusal is not None:
        report_invocation(caller_report, Outcome.REFUSED)
        _terminal(
            UNVERIFIED_CALLER,
            Outcome.REFUSED,
            GatewayResultClass.CALLER_UNVERIFIED,
            gateway_message_ref=message_ref,
            channel=channel,
            caller_verified=False,
        )
        return _refusal_response(admission.refusal, normalize_correlation_id(None))
    response_correlation_id = normalize_correlation_id(None)
    try:
        invocation = gateway.GatewayInvocation.read(event, context)
    except AdlyticsError as error:
        report = Report(
            operation=UNIDENTIFIED_TOOL,
            correlation_id="",
            logger=LOGGER,
            gateway_message_ref=message_ref,
        )
        report_error(error, report)
        report_invocation(report, Outcome.REFUSED)
        _terminal(
            UNIDENTIFIED_TOOL,
            Outcome.REFUSED,
            GatewayResultClass.INVALID_INVOCATION,
            gateway_message_ref=message_ref,
            channel=channel,
            caller_verified=True,
        )
        return _refusal_response(error, response_correlation_id)
    try:
        selected = _tool(invocation.tool_name)
    except ValidationError as error:
        report = Report(
            operation=INVALID_TOOL,
            correlation_id="",
            logger=LOGGER,
            gateway_message_ref=invocation.gateway_message_ref,
        )
        with span(tool_span(INVALID_TOOL)) as recording:
            report_error(error, report)
            report_invocation(report, Outcome.REFUSED)
            recording.note_error(error)
            _terminal(
                INVALID_TOOL,
                Outcome.REFUSED,
                GatewayResultClass.INVALID_TOOL,
                gateway_message_ref=invocation.gateway_message_ref,
                channel=channel,
                caller_verified=True,
            )
            return _refusal_response(error, response_correlation_id)
    report = Report(
        operation=selected.value,
        correlation_id="",
        logger=LOGGER,
        gateway_message_ref=invocation.gateway_message_ref,
    )
    where = {
        "gateway_message_ref": invocation.gateway_message_ref,
        "channel": channel,
        "caller_verified": True,
    }
    with span(tool_span(selected.value)) as recording:
        recording.record(
            tool_result_ref=response_correlation_id,
            gateway_message_ref=invocation.gateway_message_ref,
        )
        try:
            read = ReadContext.for_invocation(
                datetime.now(UTC), context.get_remaining_time_in_millis()
            )
            request = ForecastToolRequest(
                tool=selected,
                arguments=invocation.arguments,
                computed_at=read.now,
                correlation_id=response_correlation_id,
            )
            result = handle(request, SERVICE, read)
        except AdlyticsError as error:
            report_error(error, report)
            report_invocation(report, Outcome.REFUSED)
            recording.note_error(error)
            _terminal(
                selected.value, Outcome.REFUSED, GatewayResultClass.CLASSIFIED_REFUSAL, **where
            )
            return _refusal_response(error, response_correlation_id)
        except Exception as error:
            failed = InvariantError(
                read_api.FORECAST_READ_FAILED,
                "The demand forecast read failed.",
                context={"error_type": type(error).__name__},
            )
            report_error(failed, report)
            report_invocation(report, Outcome.FAILED)
            recording.note_error(failed)
            _terminal(
                selected.value, Outcome.FAILED, GatewayResultClass.UNEXPECTED_FAILURE, **where
            )
            return _refusal_response(failed, response_correlation_id)
        answer = result.response
        recording.record(rows_returned=answer.metadata.row_count)
        process_span.record(items_read=result.items_read)
        report_invocation(report, Outcome.ANSWERED)
        _terminal(
            selected.value,
            Outcome.ANSWERED,
            GatewayResultClass.ANSWER,
            investigation_next=GatewayInvestigationNext.MANAGED_TOOL_TRACE,
            **where,
        )
        return gateway.to_json_safe(answer)


def _refusal_response(error: AdlyticsError, response_correlation_id: str) -> dict[str, Any]:
    """Return one deterministic safe refusal with canonical grounding metadata."""
    grounding = grounded_refusal(
        error.user_visible_description,
        blocks_answer=True,
        identity_salt=response_correlation_id,
    )
    return gateway.to_json_safe(
        {
            "refused": True,
            "code": str(error.code),
            "reason": error.user_visible_description,
            "correlation_id": response_correlation_id,
            "metadata": {"grounding": grounding},
        }
    )


def _tool(name: str) -> ForecastTool:
    """Resolve the tool name, or refuse naming the ones that exist."""
    try:
        return ForecastTool(name)
    except ValueError:
        available = sorted(member.value for member in ForecastTool)
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=(
                "The requested tool is not implemented by this forecast target. Available "
                "tools: " + ", ".join(available) + "."
            ),
            context={"available": available},
        ) from None


@trace_boundary(FORECAST_GATEWAY)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: _Clock) -> dict[str, Any]:
    """Bound invocation parsing and the selected forecast tool's span."""
    with span(FORECAST_GATEWAY, operation=FORECAST_GATEWAY) as process_span:
        result = _handle(event, context, process_span)
        process_span.record(outcome="completed")
        return result
