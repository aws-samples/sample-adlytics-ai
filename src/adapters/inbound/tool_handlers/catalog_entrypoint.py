# SPDX-License-Identifier: MIT-0
"""The Lambda entry point for the catalog domain's tools.

**A separate function from measurement, and the reason is IAM rather than tidiness.** The
collocation rule is one Lambda per domain with several Gateway targets routing to it, and its
exception is that tools whose permissions differ materially must not share a function, because the
function holds the union. This one reads no business data: the metric dictionary and contracts
ship inside its deployment artefact, and both answers and classified refusals carry their canonical
grounding directly in the returned tool result.

The coverage half of this family — `catalog_coverage`, build-order 2.6b — is still absent. It needs
`CatalogPort` wired to the freshness registry, and wiring that here would make this function need
DynamoDB, which is the trade to weigh when it is built rather than now.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aws_lambda_powertools import Logger

from adapters.config import tool_caller_settings, tracing_settings
from adapters.inbound.tool_handlers import gateway
from adapters.inbound.tool_handlers.caller import CallerVerifier, kms_caller_verifier
from adapters.inbound.tool_handlers.catalog import CatalogTool, CatalogToolRequest, handle
from adapters.outbound.metric_dictionary import load_catalog
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
    CATALOG_GATEWAY,
    span,
    tool_span,
    trace_boundary,
)
from domain.narrative.publication import grounded_refusal
from domain.shared_kernel.errors import AdlyticsError, ValidationError

__all__ = ["handler"]

ROOT = gateway.contracts_root()
CATALOG = load_catalog(ROOT)
LOGGER = Logger(service="adlytics-catalog")

# Catalog remains declaration-only and has no business-data or answer-grounding dependency.
# `tracing_settings()` remains tolerant because telemetry absence must not become availability loss.
TRACING = configure_tracing(tracing_settings())
CALLER: CallerVerifier = kms_caller_verifier(tool_caller_settings())
"""Verifies every call's caller before anything else is read (ADR-077 §5)."""


def _handle(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Answer one catalog Gateway tool call or return its deterministic refusal.

    The caller is verified first, before the invocation is read: an unverified call is refused
    with no argument, tool name, or declaration consulted.
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
        report_lifecycle(
            GatewayTerminalLog(
                operation=UNVERIFIED_CALLER,
                outcome=Outcome.REFUSED,
                result_class=GatewayResultClass.CALLER_UNVERIFIED,
                investigation_next=GatewayInvestigationNext.TOOL_BOUNDARY,
                gateway_message_ref=message_ref,
                channel=channel,
                caller_verified=False,
            ),
            LOGGER,
        )
        return _refusal_response(admission.refusal, normalize_correlation_id(None))
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
        report_invocation(report, Outcome.FAILED)
        report_lifecycle(
            GatewayTerminalLog(
                operation=UNIDENTIFIED_TOOL,
                outcome=Outcome.FAILED,
                result_class=GatewayResultClass.INVALID_INVOCATION,
                investigation_next=GatewayInvestigationNext.TOOL_BOUNDARY,
                gateway_message_ref=message_ref,
                channel=channel,
                caller_verified=True,
            ),
            LOGGER,
        )
        raise

    response_correlation_id = normalize_correlation_id(None)
    try:
        selected_tool = _tool(invocation.tool_name)
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
            report_lifecycle(
                GatewayTerminalLog(
                    operation=INVALID_TOOL,
                    outcome=Outcome.REFUSED,
                    result_class=GatewayResultClass.INVALID_TOOL,
                    investigation_next=GatewayInvestigationNext.TOOL_BOUNDARY,
                    gateway_message_ref=invocation.gateway_message_ref,
                    channel=channel,
                    caller_verified=True,
                ),
                LOGGER,
            )
            return _refusal_response(error, response_correlation_id)

    report = Report(
        operation=selected_tool.value,
        correlation_id="",
        logger=LOGGER,
        gateway_message_ref=invocation.gateway_message_ref,
    )
    with span(tool_span(selected_tool.value)) as recording:
        # Both identifiers let the audit trajectory reach this Gateway-isolated trace: the
        # result reference travels inside the tool result, the message reference is Gateway's.
        recording.record(
            tool_result_ref=response_correlation_id,
            gateway_message_ref=invocation.gateway_message_ref,
        )
        try:
            request = _request_from(invocation, selected_tool, response_correlation_id)
            answer = handle(request)
        except AdlyticsError as error:
            report_error(error, report)
            report_invocation(report, Outcome.REFUSED)
            recording.note_error(error)
            report_lifecycle(
                GatewayTerminalLog(
                    operation=selected_tool.value,
                    outcome=Outcome.REFUSED,
                    result_class=GatewayResultClass.CLASSIFIED_REFUSAL,
                    investigation_next=GatewayInvestigationNext.TOOL_BOUNDARY,
                    gateway_message_ref=invocation.gateway_message_ref,
                    channel=channel,
                    caller_verified=True,
                ),
                LOGGER,
            )
            return _refusal_response(error, response_correlation_id)
        except Exception as error:
            report_error(error, report)
            report_invocation(report, Outcome.FAILED)
            report_lifecycle(
                GatewayTerminalLog(
                    operation=selected_tool.value,
                    outcome=Outcome.FAILED,
                    result_class=GatewayResultClass.UNEXPECTED_FAILURE,
                    investigation_next=GatewayInvestigationNext.TOOL_BOUNDARY,
                    gateway_message_ref=invocation.gateway_message_ref,
                    channel=channel,
                    caller_verified=True,
                ),
                LOGGER,
            )
            raise

        recording.record(rows_returned=answer.metadata.row_count)
        report_invocation(report, Outcome.ANSWERED)
        report_lifecycle(
            GatewayTerminalLog(
                operation=selected_tool.value,
                outcome=Outcome.ANSWERED,
                result_class=GatewayResultClass.ANSWER,
                investigation_next=GatewayInvestigationNext.MANAGED_TOOL_TRACE,
                gateway_message_ref=invocation.gateway_message_ref,
                channel=channel,
                caller_verified=True,
            ),
            LOGGER,
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


def _request_from(
    invocation: gateway.GatewayInvocation,
    tool: CatalogTool,
    response_correlation_id: str,
) -> CatalogToolRequest:
    """Turn the wire arguments into the request the tool validates.

    `metric_name` and `grain` are optional on the request object because only one of the two tools
    uses them, and which is which is the tool's own business rather than this adapter's.
    """
    arguments = invocation.arguments
    return CatalogToolRequest(
        tool=tool,
        catalog=CATALOG,
        computed_at=datetime.now(UTC),
        correlation_id=response_correlation_id,
        metric_name=str(arguments.get("metric_name") or ""),
        grain=str(arguments.get("grain") or ""),
        source_reference=str(arguments.get("source_reference") or ""),
    )


def _tool(name: str) -> CatalogTool:
    """Resolve the tool name, or refuse naming the ones that exist."""
    try:
        return CatalogTool(name)
    except ValueError:
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=(
                "The requested tool is not implemented by this catalog target. Available tools: "
                + ", ".join(sorted(member.value for member in CatalogTool))
                + "."
            ),
            context={"available": sorted(member.value for member in CatalogTool)},
        ) from None


@trace_boundary(CATALOG_GATEWAY)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Bound invocation parsing and the selected governed catalog-tool span."""
    with span(CATALOG_GATEWAY, operation=CATALOG_GATEWAY) as process_span:
        result = _handle(event, context)
        process_span.record(outcome="completed")
        return result
