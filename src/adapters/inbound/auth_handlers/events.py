# SPDX-License-Identifier: MIT-0
"""Reading an API Gateway payload-2.0 event.

Shared by the authorizer and the auth endpoints so there is one interpretation of
the event shape. Two copies would eventually disagree about something small — a
header case, a cookie value containing an equals sign — and disagreement between
the authorizer and the handler it guards is the worst place for it.

Nothing here defaults a missing value into a permissive one. `method` raises
rather than assuming GET, because assuming GET would exempt a request from CSRF on
the strength of a malformed event.
"""

from __future__ import annotations

from typing import Any, cast

from adapters.outbound.telemetry.reporting import normalize_correlation_id
from domain.identity.session import IdentityError, ReviewAccessLevel


def headers(event: dict[str, Any]) -> dict[str, str]:
    """Header names lowercased. API Gateway does this itself on most paths, not all."""
    return {name.lower(): value for name, value in _string_map(event.get("headers")).items()}


def query(event: dict[str, Any]) -> dict[str, str]:
    """Query-string parameters, with names left as sent.

    Not lowercased, unlike headers: query-parameter names are case-sensitive, and
    folding them would silently match a parameter the provider did not send.
    """
    return _string_map(event.get("queryStringParameters"))


def cookie_header(event: dict[str, Any]) -> str:
    """Rebuild a Cookie header from the event's cookies array.

    Payload 2.0 pre-splits cookies into a list. Rejoining rather than parsing that
    list keeps a single cookie parser in the domain, instead of two that can
    disagree about a value containing an equals sign.
    """
    cookies = event.get("cookies")
    if isinstance(cookies, list):
        return "; ".join(str(cookie) for cookie in cast("list[object]", cookies))
    return headers(event).get("cookie", "")


def method(event: dict[str, Any]) -> str:
    """Return the HTTP method, which decides whether CSRF applies."""
    return _http_field(event, "method")


def path(event: dict[str, Any]) -> str:
    """Return the request path, used to resolve the route."""
    return _http_field(event, "path")


def route_key(event: dict[str, Any]) -> str:
    """Return the route API Gateway matched, such as `POST /api/quality-review/turn`.

    The only input an entitlement is chosen from. The raw path is not used: API Gateway has
    already matched it to exactly this route, and a second interpretation could disagree.
    """
    value = event.get("routeKey")
    if isinstance(value, str) and value:
        return value
    raise IdentityError("The event carried no route key.")


def authorization_header(event: dict[str, Any]) -> str | None:
    """Return the Authorization header as sent, or `None` when the request carried none.

    Present-but-empty is returned as it is, never folded into absence: any Authorization
    header is a credential the decision must parse or refuse.
    """
    return headers(event).get("authorization")


def api_request_id(event: dict[str, Any]) -> str:
    """Return API Gateway's own request id, or empty; an opaque locator, never a credential."""
    request_context = event.get("requestContext")
    if isinstance(request_context, dict):
        found = cast("dict[str, object]", request_context).get("requestId")
        if isinstance(found, str):
            return found
    return ""


REVIEW_LEVELS_CONTEXT = "review_levels"
"""The authorizer context key carrying the comma-joined verified levels on review routes."""

REVIEWER_CONTEXT = "reviewer"
"""The verified subject on review routes only, for the reviewer access log (ADR-081 §4)."""

ATTRIBUTION_CONTEXT = "attribution"
"""`true` or `false` on review routes only: whether the caller may receive the principal."""


def authorizer_review_levels(event: dict[str, Any]) -> frozenset[ReviewAccessLevel]:
    """Return the verified review levels the authorizer projected for a review route.

    Absent means the authorizer did not attach them, which on a review route is a denial; an
    unknown level is refused rather than ignored.
    """
    request_context = event.get("requestContext")
    if isinstance(request_context, dict):
        authorizer = cast("dict[str, object]", request_context).get("authorizer")
        if isinstance(authorizer, dict):
            lambda_context = cast("dict[str, object]", authorizer).get("lambda")
            if isinstance(lambda_context, dict):
                raw = cast("dict[str, object]", lambda_context).get(REVIEW_LEVELS_CONTEXT)
                if isinstance(raw, str):
                    try:
                        return frozenset(
                            ReviewAccessLevel(value) for value in raw.split(",") if value
                        )
                    except ValueError:
                        raise IdentityError("The authorizer review levels are malformed.") from None
    raise IdentityError("The event carried no authorizer review levels.")


def authorizer_subject(event: dict[str, Any]) -> str:
    """Return the subject the authorizer verified.

    The only trustworthy identity on an authorized route. Reading the cookie again
    here would be a second identity source that can disagree with the first.
    """
    request_context = event.get("requestContext")
    if isinstance(request_context, dict):
        authorizer = cast("dict[str, object]", request_context).get("authorizer")
        if isinstance(authorizer, dict):
            lambda_context = cast("dict[str, object]", authorizer).get("lambda")
            if isinstance(lambda_context, dict):
                subject = cast("dict[str, object]", lambda_context).get("subject")
                if isinstance(subject, str) and subject:
                    return subject
    raise IdentityError("The event carried no authorizer subject.")


def authorizer_correlation_id(event: dict[str, Any]) -> str:
    """Return the canonical support key normalized by the uncached authorizer."""
    request_context = event.get("requestContext")
    if isinstance(request_context, dict):
        authorizer = cast("dict[str, object]", request_context).get("authorizer")
        if isinstance(authorizer, dict):
            lambda_context = cast("dict[str, object]", authorizer).get("lambda")
            if isinstance(lambda_context, dict):
                correlation_id = cast("dict[str, object]", lambda_context).get("correlation_id")
                if (
                    isinstance(correlation_id, str)
                    and normalize_correlation_id(correlation_id) == correlation_id
                ):
                    return correlation_id
    raise IdentityError("The event carried no canonical authorizer correlation id.")


def _http_field(event: dict[str, Any], name: str) -> str:
    """Read one field from requestContext.http, refusing to default it.

    An absent method must not become GET, which would exempt a request from CSRF
    on the strength of a malformed event.
    """
    request_context = event.get("requestContext")
    if isinstance(request_context, dict):
        http = cast("dict[str, object]", request_context).get("http")
        if isinstance(http, dict):
            found = cast("dict[str, object]", http).get(name)
            if isinstance(found, str) and found:
                return found
    raise IdentityError(f"The event carried no HTTP {name}.")


def _string_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    fields = cast("dict[str, object]", raw)
    return {str(name): str(value) for name, value in fields.items()}


def authorizer_can_manage_prompts(event: dict[str, Any]) -> bool:
    """Return only the verified prompt-admin entitlement projected by the authorizer."""
    request_context = event.get("requestContext")
    if isinstance(request_context, dict):
        authorizer = cast("dict[str, object]", request_context).get("authorizer")
        if isinstance(authorizer, dict):
            lambda_context = cast("dict[str, object]", authorizer).get("lambda")
            if isinstance(lambda_context, dict):
                return cast("dict[str, object]", lambda_context).get("can_manage_prompts") == "true"
    raise IdentityError("The event carried no prompt-admin entitlement.")


def authorizer_can_read_operational_status(event: dict[str, Any]) -> bool:
    """Return only the verified dedicated operational-status entitlement."""
    request_context = event.get("requestContext")
    if isinstance(request_context, dict):
        authorizer = cast("dict[str, object]", request_context).get("authorizer")
        if isinstance(authorizer, dict):
            lambda_context = cast("dict[str, object]", authorizer).get("lambda")
            if isinstance(lambda_context, dict):
                value = cast("dict[str, object]", lambda_context).get("can_read_operational_status")
                return value == "true"
    raise IdentityError("The event carried no operational-status entitlement.")
