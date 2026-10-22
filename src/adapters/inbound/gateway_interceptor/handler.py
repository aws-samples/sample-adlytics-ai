# SPDX-License-Identifier: MIT-0
"""The Gateway REQUEST interceptor: carry the caller's bearer to the tool (ADR-077 §5, probe P3).

The Gateway's `CUSTOM_JWT` authorizer has already verified the caller's token before this runs.
The interceptor makes no trust decision of its own beyond failing closed when a `tools/call`
carries no bearer at all, which can only happen when the authorizer is detached or misconfigured.

For every request it echoes `transformedGatewayRequest` with the request body. For `tools/call`:

- the bearer must be `Authorization: Bearer <credential>`, matched on a case-insensitive header
  name; otherwise the Gateway is told to answer immediately with HTTP 401 and a JSON-RPC error;
- every `params.arguments` key starting with `_adl_` is removed, because that prefix is reserved
  for server-set values and the model must never supply one;
- the output headers are exactly `{"Authorization": <the same header value>}`. AWS documents that
  an interceptor-set `Authorization` is forwarded to the target; [verified 2026-09-26, probe P3]
  a Lambda target receives it in its client context under `bedrockAgentCorePropagatedHeaders`.

Every other method passes through unchanged with no headers set. The transformation is a pure
function of its input, so a retried or repeated interception produces the same output.

`require_bearer_handler` is the external MCP gateway's entry point (ADR-150): the same rules, but
every JSON-RPC method needs the bearer and is forwarded with it, so an anonymous `initialize` or
`tools/list` is refused here too if the gateway's own authorizer ever admits one.

Nothing logged or traced here carries a header value, the bearer, or an argument value: the one
terminal record names only the JSON-RPC method, whether a bearer was present, and the outcome.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, cast

from aws_lambda_powertools import Logger

from adapters.config import tracing_settings
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_denial,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import (
    GATEWAY_INTERCEPTOR,
    GATEWAY_INTERCEPTOR_REQUIRE_BEARER,
    Recording,
    span,
    trace_boundary,
)
from domain.shared_kernel.errors import AdlyticsError, ErrorCode, ValidationError

__all__ = [
    "BAD_INTERCEPTOR_INPUT",
    "Interception",
    "InterceptionOutcome",
    "handler",
    "intercept",
    "require_bearer_handler",
]

_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
LOGGER = Logger(service="adlytics-gateway-interceptor")
OPERATION: Final = "gateway.interceptor"
_COMPONENT: Final = "gateway_interceptor"

INPUT_VERSION: Final = "1.0"
OUTPUT_VERSION: Final = "1.0"
TOOLS_CALL: Final = "tools/call"
RESERVED_ARGUMENT_PREFIX: Final = "_adl_"
UNAUTHORIZED_STATUS: Final = 401
UNAUTHORIZED_RPC_CODE: Final = -32001
"""A JSON-RPC implementation-defined server error (-32000 to -32099): the caller is unauthorized."""

BAD_INTERCEPTOR_INPUT = ErrorCode(area="INTERCEPTOR", number=1)
"""The Gateway sent an interceptor input this adapter does not support. A `ValidationError`: the
input is outside the documented version 1.0 contract, and the Gateway fails the request."""

_AUTHORIZATION: Final = "authorization"
_BEARER: Final = re.compile(r"bearer [!-~]+", re.IGNORECASE)
"""RFC 6750 §2.1: the scheme is case-insensitive, one space, then a non-empty visible credential."""

_METHOD_LABEL: Final = re.compile(r"[A-Za-z0-9_./-]{1,64}")
_UNRECOGNISED_METHOD: Final = "unrecognised"
_ABSENT_METHOD: Final = "absent"
_UNREADABLE_METHOD: Final = "unreadable"

report_operation(
    OperationLog(
        operation=OPERATION,
        component=_COMPONENT,
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


class InterceptionOutcome(StrEnum):
    """How one interception ended; the closed vocabulary of the terminal record."""

    FORWARDED = "forwarded"
    PASSED_THROUGH = "passed_through"
    UNAUTHORIZED = "unauthorized"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Interception:
    """The interceptor output and the non-secret facts its terminal record carries."""

    output: dict[str, Any]
    jsonrpc_method: str
    bearer_present: bool
    outcome: InterceptionOutcome


def intercept(event: object, *, require_bearer_for_every_method: bool = False) -> Interception:
    """Transform one Gateway REQUEST interceptor input, or raise for an unsupported shape.

    By default only `tools/call` needs a bearer and every other method passes through. In strict
    mode, the external MCP gateway's (ADR-150), every method needs one and is forwarded with it,
    so the terminal record states whether a bearer arrived for `initialize` and `tools/list` too.
    """
    body, headers = _gateway_request(event)
    method = body.get("method")
    label = _method_label(method)
    if method != TOOLS_CALL and not require_bearer_for_every_method:
        return Interception(
            output=_output({"body": body}),
            jsonrpc_method=label,
            bearer_present=False,
            outcome=InterceptionOutcome.PASSED_THROUGH,
        )

    sanitized = _without_reserved_arguments(body)
    bearer = _bearer(headers)
    if bearer is None:
        refusal = {
            "statusCode": UNAUTHORIZED_STATUS,
            "body": {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": {"code": UNAUTHORIZED_RPC_CODE, "message": "unauthorized"},
            },
        }
        return Interception(
            output=_output({"body": sanitized}, response=refusal),
            jsonrpc_method=label,
            bearer_present=False,
            outcome=InterceptionOutcome.UNAUTHORIZED,
        )
    return Interception(
        output=_output({"headers": {"Authorization": bearer}, "body": sanitized}),
        jsonrpc_method=label,
        bearer_present=True,
        outcome=InterceptionOutcome.FORWARDED,
    )


def _gateway_request(event: object) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a private copy of the request body and the request headers, or refuse the input."""
    if not isinstance(event, dict):
        raise _bad_input("event")
    document = cast("dict[str, Any]", event)
    if document.get("interceptorInputVersion") != INPUT_VERSION:
        raise _bad_input("interceptorInputVersion")
    mcp = document.get("mcp")
    if not isinstance(mcp, dict):
        raise _bad_input("mcp")
    request = cast("dict[str, Any]", mcp).get("gatewayRequest")
    if not isinstance(request, dict):
        raise _bad_input("gatewayRequest")
    gateway_request = cast("dict[str, Any]", request)
    body = gateway_request.get("body")
    if not isinstance(body, dict):
        raise _bad_input("body")
    # Absent when `passRequestHeaders` is off; a `tools/call` then has no bearer and is refused.
    headers = gateway_request.get("headers", {})
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        raise _bad_input("headers")
    # Copied so the output never aliases the input: the transformation stays a pure function.
    return copy.deepcopy(cast("dict[str, Any]", body)), cast("dict[str, Any]", headers)


def _bad_input(field: str) -> ValidationError:
    return ValidationError(
        code=BAD_INTERCEPTOR_INPUT,
        description="The Gateway interceptor input is not the supported version 1.0 shape.",
        context={"field": field},
    )


def _bearer(headers: dict[str, Any]) -> str | None:
    """Return the one `Authorization` value when it is a well-formed bearer, else `None`.

    Two headers differing only in case are ambiguous, so they are refused rather than resolved.
    """
    # JSON object keys are always strings, so every name has `lower()`.
    values = [value for name, value in headers.items() if name.lower() == _AUTHORIZATION]
    if len(values) != 1:
        return None
    value = values[0]
    if not isinstance(value, str) or _BEARER.fullmatch(value) is None:
        return None
    return value


def _without_reserved_arguments(body: dict[str, Any]) -> dict[str, Any]:
    """Remove every model-supplied `_adl_` argument; any other shape is left for the Gateway."""
    params = body.get("params")
    if not isinstance(params, dict):
        return body
    arguments = cast("dict[str, Any]", params).get("arguments")
    if not isinstance(arguments, dict):
        return body
    kept = {
        name: value
        for name, value in cast("dict[str, Any]", arguments).items()
        if not name.startswith(RESERVED_ARGUMENT_PREFIX)
    }
    return {**body, "params": {**cast("dict[str, Any]", params), "arguments": kept}}


def _output(request: dict[str, Any], *, response: dict[str, Any] | None = None) -> dict[str, Any]:
    mcp: dict[str, Any] = {"transformedGatewayRequest": request}
    if response is not None:
        mcp["transformedGatewayResponse"] = response
    return {"interceptorOutputVersion": OUTPUT_VERSION, "mcp": mcp}


def _method_label(method: object) -> str:
    """Name the JSON-RPC method in a bounded, log-safe form; never echo an odd raw value."""
    if method is None:
        return _ABSENT_METHOD
    if isinstance(method, str) and _METHOD_LABEL.fullmatch(method):
        return method
    return _UNRECOGNISED_METHOD


@trace_boundary(GATEWAY_INTERCEPTOR)
@flushes_traces
@flushes_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    """Intercept one Gateway request; refuse an unsupported input by raising."""
    with span(GATEWAY_INTERCEPTOR, operation=OPERATION) as process_span:
        result = _serve(event, process_span, strict=False)
        process_span.record(
            outcome=result.outcome.value,
            jsonrpc_method=result.jsonrpc_method,
            bearer_present=result.bearer_present,
        )
        _report_terminal(result)
        return result.output


@trace_boundary(GATEWAY_INTERCEPTOR_REQUIRE_BEARER)
@flushes_traces
@flushes_metrics
@LOGGER.inject_lambda_context
def require_bearer_handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    """Intercept one external MCP gateway request, requiring a bearer on every method (ADR-150)."""
    with span(GATEWAY_INTERCEPTOR_REQUIRE_BEARER, operation=OPERATION) as process_span:
        result = _serve(event, process_span, strict=True)
        process_span.record(
            outcome=result.outcome.value,
            jsonrpc_method=result.jsonrpc_method,
            bearer_present=result.bearer_present,
        )
        _report_terminal(result)
        return result.output


def _serve(event: dict[str, Any], process_span: Recording, *, strict: bool) -> Interception:
    """Intercept, reporting a failure or a denial once; the caller records the terminal result."""
    report = Report(operation=OPERATION, correlation_id="", logger=LOGGER)
    try:
        result = intercept(event, require_bearer_for_every_method=strict)
    except Exception as exc:
        _report_failure(exc, report, process_span)
        _report_terminal(
            Interception(
                output={},
                jsonrpc_method=_UNREADABLE_METHOD,
                bearer_present=False,
                outcome=InterceptionOutcome.FAILED,
            )
        )
        raise
    if result.outcome is InterceptionOutcome.UNAUTHORIZED:
        report_denial(
            "A request carried no bearer credential."
            if strict
            else "A tools/call request carried no bearer credential.",
            report,
        )
    return result


def _report_terminal(result: Interception) -> None:
    settled = result.outcome in {InterceptionOutcome.FORWARDED, InterceptionOutcome.PASSED_THROUGH}
    report_operation(
        OperationLog(
            operation=OPERATION,
            component=_COMPONENT,
            phase="terminal",
            outcome=result.outcome.value,
            severity=LogSeverity.INFO if settled else LogSeverity.WARNING,
            details={
                "jsonrpc_method": result.jsonrpc_method,
                "bearer_present": result.bearer_present,
            },
        ),
        LOGGER,
    )


def _report_failure(exc: Exception, report: Report, process_span: Recording) -> None:
    if isinstance(exc, AdlyticsError):
        process_span.note_error(exc)
    else:
        process_span.note_failure(exc)
    report_error(exc, report)
    process_span.record(outcome=InterceptionOutcome.FAILED.value)
