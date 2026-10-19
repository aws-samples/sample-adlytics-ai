# SPDX-License-Identifier: MIT-0
"""One server-minted correlation identifier per HTTP API request (ADR-081 §2).

The authorizer mints it on an authorized route and passes it in its context; a route without an
authorizer mints its own. `returns_correlation` wraps an API Gateway handler so the handler reads
that one value through `current_correlation_id()` and every response it returns carries it in
the `x-correlation-id` header. No handler ever reads a client-supplied value.
"""

from __future__ import annotations

import functools
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol, cast

from adapters.inbound.auth_handlers import events
from adapters.outbound.telemetry.reporting import CORRELATION_HEADER, normalize_correlation_id
from domain.identity.session import IdentityError

if TYPE_CHECKING:
    from collections.abc import Callable

_CURRENT: ContextVar[str] = ContextVar("adlytics_request_correlation", default="")


def current_correlation_id() -> str:
    """Return this request's correlation identifier, or empty outside a wrapped handler."""
    return _CURRENT.get()


def request_correlation(event: dict[str, Any]) -> str:
    """Return the authorizer's value when the route has one, otherwise a freshly minted one.

    `requestContext.authorizer` is set only by API Gateway, never by the caller, so on a route
    without an authorizer this always mints.
    """
    try:
        return events.authorizer_correlation_id(event)
    except IdentityError:
        return normalize_correlation_id(None)


def with_correlation_header(response: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Return `response` with the correlation header added, leaving every other header as is."""
    headers = dict(cast("dict[str, str]", response.get("headers") or {}))
    headers[CORRELATION_HEADER] = correlation_id
    return {**response, "headers": headers}


class _CorrelatedLogger(Protocol):
    """The one Logger method the decorator uses, so tests can pass a recorder."""

    def set_correlation_id(self, value: str | None) -> None: ...


def returns_correlation[H: Callable[..., dict[str, Any]]](
    logger: _CorrelatedLogger,
) -> Callable[[H], H]:
    """Resolve the request's correlation once, stamp it on every log line, and return it.

    Every line `logger` writes while the handler runs carries `correlation_id`, so the handler's
    own start and terminal lines join the authorizer's line for the same request.
    """

    def decorate(handler: H) -> H:
        @functools.wraps(handler)
        def wrapper(event: dict[str, Any], context: object) -> dict[str, Any]:
            correlation_id = request_correlation(event)
            token = _CURRENT.set(correlation_id)
            logger.set_correlation_id(correlation_id)
            try:
                return with_correlation_header(handler(event, context), correlation_id)
            finally:
                logger.set_correlation_id(None)
                _CURRENT.reset(token)

        return cast("H", wrapper)

    return decorate
