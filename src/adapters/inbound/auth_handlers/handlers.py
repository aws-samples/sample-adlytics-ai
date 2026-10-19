# SPDX-License-Identifier: MIT-0
"""The four auth endpoints, collocated in one Lambda.

One function for the whole auth domain rather than four, per the compute steering:
four cold starts and four deployment artefacts for endpoints that share all of
their configuration and most of their imports is operational cost without benefit.

Every response carries cookies and a status. No response carries a token in its
body, its URL or a redirect fragment — that property is the reason this design
exists, and `_session_response` is the only function that puts a token anywhere.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from aws_lambda_powertools import Logger

from adapters.config import settings, tracing_settings
from adapters.inbound.auth_handlers import events, origin_verify, routing
from adapters.inbound.correlation import current_correlation_id, returns_correlation
from adapters.outbound.cognito.identity_provider import CognitoIdentityProvider
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    METRICS,
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import AUTH_REQUEST, span, trace_boundary
from domain.identity.authorization import (
    CSRF_HEADER,
    SAME_SITE_FETCH_VALUES,
)
from domain.identity.cookies import (
    CSRF_COOKIE,
    REFRESH_COOKIE,
    TRANSACTION_COOKIE,
    SessionCookieRequest,
    build_cleared_cookies,
    build_session_cookies,
    build_transaction_cookie,
    clear_transaction_cookie,
    parse_cookie_header,
)
from domain.identity.login_flow import (
    CHALLENGE_METHOD,
    LoginTransaction,
    new_login_transaction,
    state_matches,
)
from domain.identity.session import IdentityError, csrf_token_matches, new_csrf_token
from domain.identity.tokens import TokenSet
from domain.shared_kernel.errors import AdlyticsError, DependencyError
from ports.outbound.identity_provider import ExchangeCodeRequest

SETTINGS = settings()
TRACING = configure_tracing(tracing_settings())
"""Whether spans leave this container, decided once at init.

Installed here beside `settings()` for the same reason: a container that cannot resolve its
telemetry should fail at start rather than on a request. Returns `False` when tracing is off, which
is every test process and any deployment where Transaction Search has not been enabled — the spans
are still authored, they simply stay in-process.
"""

PROVIDER = CognitoIdentityProvider(SETTINGS)
ORIGIN_VERIFIER = origin_verify.verifier_from_config()
"""ADR-078: the four routes without an authorizer make the authorizer's first check themselves."""
LOGGER = Logger(service="adlytics-auth")


REQUESTED_SCOPES = "openid email profile"
HTTP_ERROR_STATUS = 400
"""A design constant, not configuration. Changing it changes what the access token
authorises, which is a decision rather than an environment difference."""


@returns_correlation(LOGGER)
@trace_boundary(AUTH_REQUEST)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    """Route one request to the endpoint that owns it.

    One Lambda for the whole auth domain: five routes that share all of their
    configuration and most of their imports do not justify five cold starts and
    five deployment artefacts.

    Catches everything. An unhandled exception here would surface as a 502 in the
    middle of a login, which reads as an outage rather than a rejected credential.
    """
    with span(AUTH_REQUEST, operation="auth.request") as process_span:
        try:
            operation = routing.resolve(event)
        except IdentityError as error:
            report_operation(
                OperationLog(
                    operation="auth.route",
                    component="auth_api",
                    phase="routing",
                    outcome="not_found",
                    severity=LogSeverity.WARNING,
                    description=str(error),
                    details={"status": 404},
                ),
                LOGGER,
            )
            process_span.record(outcome="not_found", status=404)
            return _json(404, {"error": "not_found"})

        correlation_id = current_correlation_id()
        from_edge = operation not in routing.UNAUTHENTICATED or _from_edge(
            event, operation, correlation_id
        )
        if not from_edge:
            process_span.record(outcome="refused", status=403)
            return _json(403, {"error": "forbidden"})
        report_operation(
            OperationLog(
                operation=f"auth.{operation}",
                component="auth_api",
                phase="start",
                outcome="started",
                severity=LogSeverity.DEBUG,
            ),
            LOGGER,
        )
        try:
            response = _DISPATCH[operation](event)
        except Exception as exc:
            # An inbound adapter absorbs everything: an unhandled error here would be a
            # 502 in the middle of a login.
            if isinstance(exc, AdlyticsError):
                process_span.note_error(exc)
            else:
                process_span.note_failure(exc)
            report_error(
                exc,
                Report(
                    operation=f"auth.{operation}",
                    correlation_id=correlation_id,
                    logger=LOGGER,
                ),
            )
            process_span.record(outcome="failed", status=500)
            return _json(500, {"error": "internal"})

        status = int(response.get("statusCode", 500))
        refused = status >= HTTP_ERROR_STATUS
        report_operation(
            OperationLog(
                operation=f"auth.{operation}",
                component="auth_api",
                phase="terminal",
                outcome="refused" if refused else "completed",
                severity=LogSeverity.WARNING if refused else LogSeverity.INFO,
                details={"status": status},
            ),
            LOGGER,
        )
        process_span.record(outcome="refused" if refused else "completed", status=status)
        return response


def login_handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:  # noqa: ARG001
    """Begin a login. Redirects to managed login with PKCE and state.

    The frontend navigates to this endpoint rather than constructing the provider
    URL itself, so swapping managed login for a UI of our own is a change here and
    nowhere else.
    """
    transaction = new_login_transaction()
    query = urllib.parse.urlencode(
        {
            "client_id": SETTINGS.cognito_client_id,
            "response_type": "code",
            "scope": REQUESTED_SCOPES,
            "redirect_uri": SETTINGS.callback_redirect_uri,
            "state": transaction.state,
            "code_challenge": transaction.code_challenge,
            "code_challenge_method": CHALLENGE_METHOD,
        }
    )
    return {
        "statusCode": 302,
        "headers": {"Location": f"{SETTINGS.cognito_authorize_url}?{query}", **_no_store()},
        "cookies": [build_transaction_cookie(SETTINGS.cookie_policy, transaction.encode())],
    }


def callback_handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:  # noqa: ARG001
    """Redeem the authorization code and establish the session.

    On any failure the browser is sent back to the application with a generic
    marker. The reason stays server-side: a callback error message is rendered in
    a page and is the easiest place to leak why a token was rejected.
    """
    try:
        transaction = _in_flight_transaction(event)
        returned_state = events.query(event).get("state", "")
        if not state_matches(returned_state, transaction):
            raise IdentityError("Callback state does not match the in-flight login.")

        code = events.query(event).get("code", "")
        if not code:
            raise IdentityError("Callback carried no authorization code.")

        tokens = PROVIDER.exchange_code(
            ExchangeCodeRequest(
                code=code,
                redirect_uri=SETTINGS.callback_redirect_uri,
                code_verifier=transaction.code_verifier,
            )
        )
    except IdentityError:
        return _failed_login()
    except DependencyError as error:
        _report_provider_failure(error, routing.CALLBACK)
        return _failed_login()

    return _session_response(tokens, location=SETTINGS.app_base_url, end_transaction=True)


def refresh_handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:  # noqa: ARG001
    """Rotate an expired session using the refresh cookie.

    Deliberately not behind the session authorizer: the access token has expired
    by the time this is called, so the authorizer would deny it. That makes this
    endpoint responsible for its own CSRF defence, which is why the checks are
    repeated here rather than assumed.
    """
    cookies = parse_cookie_header(events.cookie_header(event))
    if not _csrf_is_valid(event, cookies):
        return _json(403, {"error": "forbidden"})

    refresh_token = cookies.get(REFRESH_COOKIE, "")
    if not refresh_token:
        return _json(401, {"error": "unauthenticated"})

    try:
        tokens = PROVIDER.refresh(refresh_token)
    except IdentityError:
        # One attempt, never a loop. A refresh that fails means re-authenticating.
        return _json(401, {"error": "unauthenticated"})
    except DependencyError as error:
        _report_provider_failure(error, routing.REFRESH)
        return _json(401, {"error": "unauthenticated"})

    return _session_response(tokens, location=None)


def logout_handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:  # noqa: ARG001
    """Revoke at the provider and clear the cookies.

    Clearing alone is not logout: a refresh token copied beforehand keeps working
    until it expires. Cookies are cleared even when revocation fails, so a
    provider outage cannot leave the browser holding a live session.
    """
    cookies = parse_cookie_header(events.cookie_header(event))
    if not _csrf_is_valid(event, cookies):
        return _json(403, {"error": "forbidden"})

    refresh_token = cookies.get(REFRESH_COOKIE, "")
    revoked = True
    if refresh_token:
        try:
            PROVIDER.revoke(refresh_token)
        except DependencyError as error:
            _report_provider_failure(error, routing.LOGOUT)
            revoked = False

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", **_no_store()},
        "cookies": list(build_cleared_cookies(SETTINGS.cookie_policy)),
        "body": '{"revoked": true}' if revoked else '{"revoked": false}',
    }


def _session_response(
    tokens: TokenSet, *, location: str | None, end_transaction: bool = False
) -> dict[str, Any]:
    """Establish a session. The only place that does.

    Both the managed-login callback and the refresh path end here, so a future
    login route inherits identical cookie attributes rather than a second copy
    that drifts. A new CSRF token is minted on every session, which is what makes
    refresh a rotation rather than an extension.
    """
    cookies = list(
        build_session_cookies(
            SessionCookieRequest(
                policy=SETTINGS.cookie_policy,
                tokens=tokens,
                csrf_token=new_csrf_token(),
                refresh_max_age=SETTINGS.refresh_cookie_max_age_seconds,
            )
        )
    )
    if end_transaction:
        cookies.append(clear_transaction_cookie(SETTINGS.cookie_policy))

    headers: dict[str, str] = {**_no_store()}
    if location is not None:
        headers["Location"] = location
        return {"statusCode": 302, "headers": headers, "cookies": cookies}

    headers["Content-Type"] = "application/json"
    return {"statusCode": 200, "headers": headers, "cookies": cookies, "body": "{}"}


def _in_flight_transaction(event: dict[str, Any]) -> LoginTransaction:
    raw = parse_cookie_header(events.cookie_header(event)).get(TRANSACTION_COOKIE, "")
    if not raw:
        raise IdentityError("Callback arrived with no in-flight login.")
    return LoginTransaction.decode(raw)


def _failed_login() -> dict[str, Any]:
    """Send the browser home, and end the transaction either way.

    A verifier that outlives its single use is a credential with no purpose left.
    """
    return {
        "statusCode": 302,
        "headers": {"Location": f"{SETTINGS.app_base_url}/?auth=failed", **_no_store()},
        "cookies": [clear_transaction_cookie(SETTINGS.cookie_policy)],
    }


def _report_provider_failure(error: DependencyError, operation: str) -> None:
    """Report a provider failure the endpoint answers with its unchanged response.

    A rejected credential (`IdentityError`) is the user's answer; a provider outage behind the
    same response is ours, so only the outage reaches the error metric.
    """
    report_error(
        error,
        Report(
            operation=f"auth.{operation}", correlation_id=current_correlation_id(), logger=LOGGER
        ),
    )


def _csrf_is_valid(event: dict[str, Any], cookies: dict[str, str]) -> bool:
    """Double-submit plus an origin check, the same pair the authorizer applies.

    Repeated here because these two routes cannot sit behind the authorizer — one
    runs with an expired session, the other is tearing the session down.
    """
    headers = events.headers(event)
    if not csrf_token_matches(cookies.get(CSRF_COOKIE, ""), headers.get(CSRF_HEADER, "")):
        return False

    fetch_site = headers.get("sec-fetch-site", "")
    if fetch_site:
        return fetch_site.strip().lower() in SAME_SITE_FETCH_VALUES
    origin = headers.get("origin", "")
    return bool(origin) and origin == SETTINGS.app_base_url


def session_handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:  # noqa: ARG001
    """Return who the caller is, according to the authorizer.

    The smallest possible authenticated endpoint, and the only proof that a
    session actually authorises anything. Identity comes from the authorizer
    context and never from the cookie: re-parsing the cookie here would be a
    second identity source that can disagree with the one that granted access.

    `can_read_operational_status` is group membership AND provisioned in this deployment
    (ADR-115 decision 8), so the shell never lists a page whose API has no route here.
    `operational_status_provisioned` says whether this deployment has that API at all, so a
    direct visit can tell "isn't set up here" from "no access". It's a deployment fact, the same
    for every caller, and grants nothing: the operational status API enforces membership on its
    own.
    """
    provisioned = SETTINGS.operational_status_provisioned
    return _json(
        200,
        {
            "subject": events.authorizer_subject(event),
            "can_manage_prompts": events.authorizer_can_manage_prompts(event),
            "can_read_operational_status": events.authorizer_can_read_operational_status(event)
            and provisioned,
            "operational_status_provisioned": provisioned,
        },
    )


def _from_edge(event: dict[str, Any], operation: str, correlation_id: str) -> bool:
    """Whether the request carries an accepted origin-verify value (ADR-078).

    A failed secret read refuses, the same as a wrong value.
    """
    try:
        verified = ORIGIN_VERIFIER.verify(origin_verify.presented_value(event))
    except Exception as exc:
        report_error(
            exc,
            Report(operation=f"auth.{operation}", correlation_id=correlation_id, logger=LOGGER),
        )
        return False
    if not verified:
        origin_verify.report_refusal(LOGGER, component="auth_api", event=event)
    return verified


def _json(status: int, body: dict[str, object]) -> dict[str, Any]:
    """Build a JSON response. Bodies here never carry a token or a failure reason."""
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **_no_store()},
        "body": json.dumps(body),
    }


_DISPATCH: dict[str, Any] = {
    routing.LOGIN: login_handler,
    routing.CALLBACK: callback_handler,
    routing.REFRESH: refresh_handler,
    routing.LOGOUT: logout_handler,
    routing.SESSION: session_handler,
}
"""Declared after the handlers so the table cannot reference one that moved."""


def _no_store() -> dict[str, str]:
    """Return cache directives that keep a session-setting response out of caches."""
    return {"Cache-Control": "no-store", "Pragma": "no-cache"}
