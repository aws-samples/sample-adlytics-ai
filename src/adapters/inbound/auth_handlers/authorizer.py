# SPDX-License-Identifier: MIT-0
"""REQUEST-type Lambda authorizer for the HTTP API.

Thin by design: it reshapes the event, asks the domain for a decision, and
translates the answer. Every rule about what makes a request authorised lives in
domain/identity/authorization.py, so the ordering is tested once rather than
re-implemented here.

The built-in JWT authorizer cannot be used. It reads a bearer token from a header
or query parameter and has no cookie support, which is the whole premise of this
design — see ADR-007 (docs/decisions/adr-007-auth-cookie-bff.md) and
docs/concepts/authentication.md.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from aws_lambda_powertools import Logger

from adapters.config import (
    authorizer_public_client_id,
    settings_without_client_secret,
    tracing_settings,
)
from adapters.inbound.auth_handlers import events, origin_verify
from adapters.outbound.cognito.identity_provider import CognitoIdentityProvider
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    METRICS,
    LogSeverity,
    OperationLog,
    Report,
    normalize_correlation_id,
    report_denial,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import AUTH_AUTHORIZE, span, trace_boundary
from domain.identity.authorization import (
    CSRF_HEADER,
    RouteAuthorization,
    RouteAuthorizationRequest,
    authorize_route,
)
from domain.identity.session import (
    IdentityError,
    ReviewAccessLevel,
    may_receive_attribution,
    utc_now,
)
from domain.identity.tokens import UnknownSigningKeyError
from domain.shared_kernel.errors import AdlyticsError

SETTINGS = settings_without_client_secret()
PUBLIC_CLIENT_ID = authorizer_public_client_id()
"""The public PKCE client a bearer must carry; the cookie keeps the BFF client (ADR-078 §2)."""
TRACING = configure_tracing(tracing_settings())
"""Whether spans leave this container, decided once at init.

Installed here beside `settings_without_client_secret()` for the same reason: a container that
cannot resolve its telemetry should fail at start rather than on a request. Returns `False` when
tracing is off, which is every test process and any deployment where Transaction Search has not
been enabled — the spans are still authored, they simply stay in-process.
"""

"""Resolved at import without the BFF client secret, which authorization never uses, so a
container with unusable nonsecret configuration never becomes healthy. See the configuration
steering."""

ORIGIN_VERIFIER = origin_verify.verifier_from_config()
"""ADR-078: read at cold start, so a container that cannot read the secret never becomes healthy."""
PROVIDER = CognitoIdentityProvider(SETTINGS)
"""One instance per container, so the JWKS cache survives across invocations."""

LOGGER = Logger(service="adlytics-authorizer")

OPERATION = "auth.authorize"

DENY: dict[str, Any] = {"isAuthorized": False}
"""No reason, ever.

A denial that explains itself tells an attacker which check they failed, turning
one blind attempt into a guided one. The reason goes to the log.
"""


@trace_boundary(AUTH_AUTHORIZE)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    """Authorise one request, or deny without explanation.

    Never raises. An exception escaping an authorizer becomes a 500, and a 500 on
    every request is indistinguishable from the platform being down.

    Every exit path is counted. A denial that is not counted cannot be alarmed on,
    and a spike in denials is either an attack or a broken deploy — which look
    identical from outside if nothing is recorded.
    """
    with span(AUTH_AUTHORIZE, operation=OPERATION) as process_span:
        # ADR-081 §2: minted here, the first server hop of every authorized route, and passed to
        # the handler in the context below; a client-supplied value is never read.
        correlation_id = normalize_correlation_id(None)
        report = Report(
            operation=OPERATION,
            correlation_id=correlation_id,
            logger=LOGGER,
        )

        # ADR-078: before any other check, and before `_to_request` fetches the signing keys, so a
        # request that did not come through the edge costs no outbound call.
        refusal = _refuse_unless_from_edge(event, report)
        if refusal is not None:
            process_span.record(outcome=refusal)
            return DENY
        try:
            request = _to_request(event)
        except IdentityError as exc:
            report_denial(str(exc), report)
            process_span.record(outcome="denied")
            return DENY
        except Exception as exc:
            # An authorizer must never propagate: a 500 on every request looks like an outage.
            if isinstance(exc, AdlyticsError):
                process_span.note_error(exc)
            else:
                process_span.note_failure(exc)
            report_error(exc, report)
            process_span.record(outcome="failed")
            return DENY

        try:
            decision = _decide(request)
        except IdentityError as exc:
            # Includes an unknown signing key that survived one refresh.
            report_denial(str(exc), report)
            process_span.record(outcome="denied")
            return DENY
        except Exception as exc:
            if isinstance(exc, AdlyticsError):
                process_span.note_error(exc)
            report_error(exc, report)
            process_span.record(outcome="failed")
            return DENY

        report_operation(
            OperationLog(
                operation=OPERATION,
                component="authorizer",
                phase="terminal",
                outcome="authorized",
                severity=LogSeverity.INFO,
                details={"correlation_id": report.correlation_id},
            ),
            LOGGER,
        )
        process_span.record(outcome="authorized")
        return {"isAuthorized": True, "context": _context(decision, correlation_id)}


def _refuse_unless_from_edge(event: dict[str, Any], report: Report) -> str | None:
    """Return the span outcome that refuses a request not from the edge, or `None` to continue.

    A failed secret read is reported as an error and refuses, the same as a wrong value.
    """
    try:
        from_edge = ORIGIN_VERIFIER.verify(origin_verify.presented_value(event))
    except Exception as exc:
        report_error(exc, report)
        return "failed"
    if from_edge:
        return None
    origin_verify.report_refusal(LOGGER, component="authorizer", event=event)
    return "denied"


def _decide(request: RouteAuthorizationRequest) -> RouteAuthorization:
    """Authorise, retrying once if the signing keys look stale.

    A correct issuer with an unrecognised `kid` almost always means the pool
    rotated. Refreshing once and retrying turns a rotation from a platform-wide
    outage into an extra fetch. The retry is bounded at one so an attacker cannot
    use unknown key IDs to drive outbound requests.
    """
    try:
        return authorize_route(request)
    except UnknownSigningKeyError:
        refreshed = replace(request, keys=PROVIDER.refresh_signing_keys())
        return authorize_route(refreshed)


def _to_request(event: dict[str, Any]) -> RouteAuthorizationRequest:
    """Reshape a payload-2.0 authorizer event into the domain's request."""
    headers = events.headers(event)
    return RouteAuthorizationRequest(
        route_key=events.route_key(event),
        method=events.method(event),
        cookie_header=events.cookie_header(event),
        authorization_header=events.authorization_header(event),
        keys=PROVIDER.signing_keys(),
        expected_issuer=SETTINGS.cognito_issuer,
        session_client_id=SETTINGS.cognito_client_id,
        bearer_client_id=PUBLIC_CLIENT_ID,
        now=utc_now(),
        csrf_header=headers.get(CSRF_HEADER, ""),
        allowed_origin=SETTINGS.app_base_url,
        origin=headers.get("origin", ""),
        sec_fetch_site=headers.get("sec-fetch-site", ""),
    )


def _context(decision: RouteAuthorization, correlation_id: str) -> dict[str, str]:
    """Return verified identity, the request support key, and review levels on review routes.

    The review levels travel only to the two review routes, whose function rechecks every
    section against them; no other route receives an entitlement it does not use. The same two
    routes alone carry the reviewer access log's keys (ADR-081 §4), so the stage access log
    records a reviewer and never an end user.
    """
    levels = decision.identity.review_levels
    context = {
        "subject": decision.identity.subject,
        "correlation_id": correlation_id,
        "can_manage_prompts": str(ReviewAccessLevel.PROMPT_ADMIN in levels).lower(),
        "can_read_operational_status": str(
            ReviewAccessLevel.OPERATIONS_STATUS_READ in levels
        ).lower(),
        "can_request_runs": str(ReviewAccessLevel.OPERATIONS_RUN_REQUEST in levels).lower(),
    }
    if decision.review_route:
        context[events.REVIEW_LEVELS_CONTEXT] = ",".join(sorted(level.value for level in levels))
        context[events.REVIEWER_CONTEXT] = decision.identity.subject
        context[events.ATTRIBUTION_CONTEXT] = str(may_receive_attribution(levels)).lower()
    return context
