# SPDX-License-Identifier: MIT-0
"""`GET /api/demand-forecast` and `GET /api/demand-forecast/status` (ADR-163).

Behind the existing session authorizer. Any verified session may read, the same audience as every
page route; neither route is under `/api/operations/`. The function reads the projected subject
first, then parses the query before any store read, then serves one read plan.

Every path returns a coded body: 200 with a designed `outcome`; 400 for a malformed query; 403
without a subject; 404 for a route this function doesn't serve; 503 when the store is unavailable
or incomplete; 500 `FORECAST_READ_FAILED` for anything else, logging only the error's class name.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import boto3
from aws_lambda_powertools import Logger

from adapters.config import demand_forecast_reader, settings_without_client_secret, tracing_settings
from adapters.inbound.auth_handlers import events
from adapters.inbound.correlation import current_correlation_id, returns_correlation
from adapters.inbound.forecast_read.service import ForecastReadService, ReadContext
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.outbound.demand_forecast_policy import load_policy
from adapters.outbound.dynamodb.demand_forecast_store import (
    READER_CLIENT_CONFIG,
    DynamoDbForecastStore,
)
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import DEMAND_FORECAST_READ, span, trace_boundary
from domain.forecasting import read_api
from domain.identity.session import IdentityError
from domain.shared_kernel.errors import (
    AdlyticsError,
    ErrorClass,
    InvariantError,
    ValidationError,
)

__all__ = ["handler"]

LOGGER: Final = Logger(service="adlytics-demand-forecast-read")
_OPERATION: Final = DEMAND_FORECAST_READ
_COMPONENT: Final = "forecast_read.handler"
_LIST_PARAMETERS: Final = frozenset({"asin", "status", "page"})
_STATUS_BY_CLASS: Final = {
    ErrorClass.VALIDATION: 400,
    ErrorClass.DOMAIN: 400,
    ErrorClass.DEPENDENCY: 503,
}


class _Clock(Protocol):
    def get_remaining_time_in_millis(self) -> int: ...


def _service() -> ForecastReadService:
    values = demand_forecast_reader(settings_without_client_secret())
    policy = load_policy(contracts_root() / "contracts/forecasting/demand-forecast.yaml").policy
    client = boto3.client("dynamodb", config=READER_CLIENT_CONFIG)
    store = DynamoDbForecastStore(values.forecast_table, policy.serving.item_max_bytes, client)
    return ForecastReadService(store, values, policy)


_TRACING: Final = configure_tracing(tracing_settings())
SERVICE: Final = _service()


def _response(status: int, body: dict[str, object]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, sort_keys=True),
    }


def _refusal(error: AdlyticsError) -> dict[str, Any]:
    if error.code == read_api.ROUTE_UNKNOWN:
        status = 404
    elif error.code == read_api.IDENTITY_REQUIRED:
        status = 403
    else:
        status = _STATUS_BY_CLASS.get(error.error_class, 500)
    if error.code == read_api.FORECAST_STORE_INCOMPLETE:
        status = 503
    return _response(status, read_api.refusal_body(error).model_dump(mode="json"))


def _query_invalid() -> ValidationError:
    return ValidationError(
        read_api.QUERY_INVALID,
        "Use asin alone, or status and page, and no other query parameter.",
    )


def _route(event: dict[str, Any]) -> str:
    try:
        return events.route_key(event)
    except IdentityError:
        raise InvariantError(read_api.ROUTE_UNKNOWN, "The event carried no route key.") from None


def _dispatch(
    event: dict[str, Any], context: ReadContext, route: str
) -> tuple[dict[str, object], int, str]:
    """Serve the route; return the body, the items read and the freshness state."""
    query = events.query(event)
    if route == read_api.STATUS_ROUTE:
        if query:
            raise _query_invalid()
        status = SERVICE.status(context)
        body = status.response
    elif route == read_api.LIST_ROUTE:
        if set(query) - _LIST_PARAMETERS or ("asin" in query and len(query) > 1):
            raise _query_invalid()
        if "asin" in query:
            status = SERVICE.asin_detail(read_api.parse_asin(query["asin"]), context)
        else:
            request = read_api.list_request(query.get("status"), query.get("page"))
            status = SERVICE.list_page(request, context)
        body = status.response
    else:
        raise InvariantError(read_api.ROUTE_UNKNOWN, f"The route {route!r} is not served here.")
    return body.model_dump(mode="json"), status.items_read, body.freshness.state


def _report(error: BaseException) -> None:
    report_error(
        error,
        Report(operation=_OPERATION, correlation_id=current_correlation_id(), logger=LOGGER),
    )


@returns_correlation(LOGGER)
@trace_boundary(DEMAND_FORECAST_READ)
@flushes_traces
@flushes_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: _Clock) -> dict[str, Any]:
    """Serve one demand forecast read route; every failure returns its coded refusal."""
    with span(DEMAND_FORECAST_READ, operation=_OPERATION) as process_span:
        items_read = 0
        route: str | None = None
        try:
            route = _route(event)
            process_span.record(route=route)
            try:
                events.authorizer_subject(event)
            except IdentityError:
                raise ValidationError(
                    read_api.IDENTITY_REQUIRED, "A signed-in session is required."
                ) from None
            read = ReadContext.for_invocation(
                datetime.now(UTC), context.get_remaining_time_in_millis()
            )
            body, items_read, freshness = _dispatch(event, read, route)
            result = _response(200, body)
            process_span.record(outcome="completed", items_read=items_read, freshness=freshness)
            outcome = "completed"
        except AdlyticsError as error:
            process_span.note_failure(error)
            _report(error)
            result = _refusal(error)
            outcome = "refused"
            process_span.record(outcome=outcome)
        except Exception as error:
            failed = InvariantError(
                read_api.FORECAST_READ_FAILED,
                "The demand forecast read failed.",
                context={"error_type": type(error).__name__},
            )
            process_span.note_failure(failed)
            _report(failed)
            result = _refusal(failed)
            outcome = "failed"
            process_span.record(outcome=outcome)
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="terminal",
                outcome=outcome,
                severity=LogSeverity.INFO,
                details={
                    "route": route,
                    "status": int(result["statusCode"]),
                    "items_read": items_read,
                },
            ),
            LOGGER,
        )
        return result
