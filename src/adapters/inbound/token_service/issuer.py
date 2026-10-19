# SPDX-License-Identifier: MIT-0
"""Public issuer: discovery, the key set, and the agent exchange (ADR-077 §3, S4).

Served by a regional REST API (Lambda proxy integration) with AWS WAF on its stage. Discovery
and the key set are public by nature and unauthenticated, like every OAuth issuer's; the key
set is read from KMS once per container, so a request flood costs Lambda invocations, not KMS
calls.

`POST /token` is the agent exchange (RFC 8693): AgentCore Identity's credential provider
presents the Harness token the route exchange minted and receives a Gateway-audience token that
expires with it. The API method is unauthenticated because the client authenticates in the form
body (`client_secret_post`), and that authentication is the first thing checked: a caller that
is not the registered client never reaches the key set or the signer. The fixed order is parse,
authenticate the client, check the grant, verify the subject, mint.

Every `/token` request ends in one terminal record carrying non-secret diagnostics of what the
caller sent (ADR-077 S4 P2): form parameter names as fixed labels, the subject type as a label,
and presence or match booleans. No value of a secret, a token, or a target is ever recorded.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
import urllib.parse
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any, Final, cast

from aws_lambda_powertools import Logger

from adapters.config import token_service_settings, tracing_settings
from adapters.outbound.kms.token_signer import KmsTokenSigner
from adapters.outbound.secretsmanager.client_secret import SecretsManagerClientSecret
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    METRICS,
    LogSeverity,
    OperationLog,
    Report,
    report_denial,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import ISSUER_REQUEST, span, trace_boundary
from domain.identity.issuance import (
    TOKEN_EXCHANGE_GRANT_TYPE,
    DiscoveryDocument,
    IssueTokenRequest,
    compact_jws,
    jwk_from_der_public_key,
    unsigned_token,
)
from domain.identity.issued_tokens import (
    IssuedTokenSubject,
    VerifyIssuedTokenRequest,
    verify_issued_token,
)
from domain.identity.session import IdentityError, utc_now
from domain.identity.tokens import JsonWebKey, JsonWebKeySet
from domain.shared_kernel.errors import AdlyticsError
from ports.outbound.client_secret import ClientSecretPort
from ports.outbound.token_signing import TokenSigningPort

__all__ = ["DISCOVERY_PATH", "JWKS_PATH", "TOKEN_PATH", "handler"]

SETTINGS = token_service_settings()
TRACING = configure_tracing(tracing_settings())
SIGNER: TokenSigningPort = KmsTokenSigner(SETTINGS.token_signing_key_id, region=SETTINGS.aws_region)
CLIENT_SECRET: ClientSecretPort = SecretsManagerClientSecret(
    SETTINGS.agent_client_secret_id, region=SETTINGS.aws_region
)
"""One per container, so the secret is read once and cached across invocations."""

DISCOVERY = DiscoveryDocument(SETTINGS.token_issuer)
"""Built at import, so an unusable issuer fails the container before it serves anything."""

LOGGER = Logger(service="adlytics-token-issuer")
OPERATION = "token.issuer"
DISCOVERY_PATH = "/.well-known/openid-configuration"
JWKS_PATH = "/jwks.json"
TOKEN_PATH = "/token"  # noqa: S105 — a resource path, not a credential
_DOCUMENT_HEADERS = {"Content-Type": "application/json", "Cache-Control": "public, max-age=300"}
_NOT_FOUND_HEADERS = {"Content-Type": "application/json"}
_EXCHANGE_HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}
"""RFC 6749 §5.1: a token response is never cached; refusals carry the same headers."""

_FORM_MEDIA_TYPE: Final = "application/x-www-form-urlencoded"
_MAX_FORM_CHARACTERS: Final = 16_384
"""Far above two RS256 tokens and the fixed parameters; anything larger is not an exchange."""
_MAX_FORM_FIELDS: Final = 32

_ISSUED_TOKEN_TYPE: Final = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105
_TOKEN_TYPE: Final = "Bearer"  # noqa: S105 — an OAuth token_type value, not a credential
_JWT_TYPE: Final = "urn:ietf:params:oauth:token-type:jwt"
_SUBJECT_TOKEN_TYPES: Final = frozenset({_JWT_TYPE, _ISSUED_TOKEN_TYPE})
_TARGET_PARAMETERS: Final = ("audience", "resource")
_ACTOR_PARAMETERS: Final = ("actor_token", "actor_token_type")

_INVALID_REQUEST: Final = "invalid_request"
_INVALID_CLIENT: Final = "invalid_client"
_UNSUPPORTED_GRANT_TYPE: Final = "unsupported_grant_type"
_INVALID_TARGET: Final = "invalid_target"
_INVALID_GRANT: Final = "invalid_grant"
_ISSUED: Final = "issued"
_BAD_REQUEST: Final = 400
_UNAUTHORIZED: Final = 401

_FORM_KEY_LABELS: Final[Mapping[str, str]] = {
    "grant_type": "grant_type",
    "client_id": "client_id",
    # pragma: allowlist nextline secret (A-PROTOCOL-CONSTANT-001)
    "client_secret": "client_auth",
    "subject_token": "subject",
    "subject_token_type": "subject_type",
    "actor_token": "actor",
    "actor_token_type": "actor_type",
    "requested_token_type": "requested_type",
    "audience": "audience",
    "resource": "resource",
    "scope": "scope",
}
"""The fixed diagnostic label for each RFC 6749 / RFC 8693 parameter name.

A label rather than the name because the shared reporting seam redacts any text or key containing
`token` or `secret` (`adapters.outbound.aws_errors.redact_diagnostic_text`), which would erase
exactly the names P2 must observe. The mapping is one-to-one, so the label set is the name set.
"""
_UNLISTED_KEY: Final = re.compile(r"[a-z0-9_.-]{1,64}")
_SUBJECT_TYPE_LABELS: Final[Mapping[str, str]] = {_JWT_TYPE: "jwt", _ISSUED_TOKEN_TYPE: "access"}


class _Refused(Exception):  # noqa: N818 — control flow for one RFC 6749 §5.2 error response
    """One OAuth error response; the reason is for our logs and never reaches the caller."""

    def __init__(self, error: str, reason: str, status: int = _BAD_REQUEST) -> None:
        super().__init__(reason)
        self.error = error
        self.reason = reason
        self.status = status


@dataclass(slots=True)
class _ExchangeDiagnostics:
    """What one `/token` request carried, reduced to non-secret labels and booleans."""

    form_keys: tuple[str, ...] = ()
    subject_type: str = "absent"
    audience_present: bool = False
    resource_present: bool = False
    scope_present: bool = False
    target_matches: bool | None = None
    """None when neither target parameter was sent; never the value sent."""
    outcome: str = "failed"
    reason: str = ""

    def observe(self, pairs: list[tuple[str, str]]) -> None:
        names = {name for name, _ in pairs}
        self.form_keys = tuple(sorted({_form_key_label(name) for name in names}))
        subject_types = [value for name, value in pairs if name == "subject_token_type"]
        if subject_types:
            self.subject_type = _SUBJECT_TYPE_LABELS.get(subject_types[0], "other")
        self.audience_present = "audience" in names
        self.resource_present = "resource" in names
        self.scope_present = "scope" in names
        targets = [value for name, value in pairs if name in _TARGET_PARAMETERS]
        if targets:
            self.target_matches = all(value == SETTINGS.gateway_token_audience for value in targets)

    def details(self) -> dict[str, object]:
        return {
            "form_keys": list(self.form_keys),
            "subject_type": self.subject_type,
            "audience_present": self.audience_present,
            "resource_present": self.resource_present,
            "scope_present": self.scope_present,
            "target_matches": self.target_matches,
        }


@trace_boundary(ISSUER_REQUEST)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: object) -> dict[str, object]:  # noqa: ARG001
    """Answer GET on discovery and the key set, POST on the exchange; everything else is 404."""
    with span(ISSUER_REQUEST, operation=OPERATION) as process_span:
        report = Report(operation=OPERATION, correlation_id="", logger=LOGGER)
        # `resource` is the API's declared resource template, not caller-chosen path text.
        method = event.get("httpMethod")
        resource = event.get("resource")
        exchange: _ExchangeDiagnostics | None = None
        response: dict[str, object]
        try:
            if method == "GET" and resource == DISCOVERY_PATH:
                response = _document(DISCOVERY.as_json())
            elif method == "GET" and resource == JWKS_PATH:
                response = _document(_key_set())
            elif method == "POST" and resource == TOKEN_PATH:
                exchange = _ExchangeDiagnostics()
                response = _token_exchange(event, exchange, report)
            else:
                response = {
                    "statusCode": 404,
                    "headers": dict(_NOT_FOUND_HEADERS),
                    "body": json.dumps({"error": "not_found"}),
                }
        except Exception as exc:
            if isinstance(exc, AdlyticsError):
                process_span.note_error(exc)
            else:
                process_span.note_failure(exc)
            report_error(exc, report)
            if exchange is not None:
                _report_exchange(exchange, status=None)
            process_span.record(outcome="failed")
            raise

        status = response["statusCode"]
        if exchange is not None:
            _report_exchange(exchange, status=cast("int", status))
            process_span.record(outcome=exchange.outcome, status=status)
            return response
        found = status == 200  # noqa: PLR2004 — the one success status
        report_operation(
            OperationLog(
                operation=OPERATION,
                component="token_service",
                phase="terminal",
                outcome="served" if found else "not_found",
                severity=LogSeverity.INFO if found else LogSeverity.WARNING,
                details={"status": status},
            ),
            LOGGER,
        )
        process_span.record(outcome="served" if found else "not_found", status=status)
        return response


def _token_exchange(
    event: Mapping[str, object], exchange: _ExchangeDiagnostics, report: Report
) -> dict[str, object]:
    """Run the exchange in its load-bearing order and answer RFC 8693 success or error JSON."""
    now = utc_now()
    try:
        pairs = _form_pairs(event)
        exchange.observe(pairs)
        form = _unique(pairs)
        _authenticate(form)
        subject_token = _exchange_subject_token(form)
        subject = _verified_subject(subject_token, now)
        lifetime = subject.remaining_lifetime_seconds(now)
    except _Refused as refusal:
        exchange.outcome = refusal.error
        exchange.reason = refusal.reason
        if refusal.error == _INVALID_CLIENT:
            report_denial(refusal.reason, report)
        return _exchange_response(refusal.status, {"error": refusal.error})
    except IdentityError as exc:
        # Includes a subject with no whole second left: refused, never minted for zero seconds.
        exchange.outcome = _INVALID_GRANT
        exchange.reason = str(exc)
        report_denial(str(exc), report)
        return _exchange_response(_BAD_REQUEST, {"error": _INVALID_GRANT})

    minted = _mint(subject, now, lifetime)
    exchange.outcome = _ISSUED
    return _exchange_response(
        200,
        {
            "access_token": minted,
            "issued_token_type": _ISSUED_TOKEN_TYPE,
            "token_type": _TOKEN_TYPE,
            "expires_in": lifetime,
        },
    )


def _form_pairs(event: Mapping[str, object]) -> list[tuple[str, str]]:
    """Decode the form body, base64 first when the proxy says so; any defect is invalid_request."""
    if _media_type(event.get("headers")) != _FORM_MEDIA_TYPE:
        raise _Refused(_INVALID_REQUEST, "The request body is not form-encoded.")
    body = event.get("body")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise _Refused(_INVALID_REQUEST, "The request body is not text.")
    text = body
    if event.get("isBase64Encoded") is True:
        try:
            text = base64.b64decode(body, validate=True).decode("utf-8")
        except binascii.Error, UnicodeDecodeError:
            raise _Refused(
                _INVALID_REQUEST, "The request body is not valid base64 UTF-8."
            ) from None
    if len(text) > _MAX_FORM_CHARACTERS:
        raise _Refused(_INVALID_REQUEST, "The request body is too large.")
    try:
        return urllib.parse.parse_qsl(
            text,
            keep_blank_values=True,
            strict_parsing=True,
            errors="strict",
            max_num_fields=_MAX_FORM_FIELDS,
        )
    except ValueError, UnicodeDecodeError:
        raise _Refused(_INVALID_REQUEST, "The request body is not a valid form.") from None


def _media_type(headers: object) -> str:
    """Return the lower-cased media type of Content-Type, matched case-insensitively."""
    if not isinstance(headers, dict):
        return ""
    entries = cast("dict[object, object]", headers)
    for name, value in entries.items():
        if isinstance(name, str) and name.lower() == "content-type" and isinstance(value, str):
            return value.split(";", 1)[0].strip().lower()
    return ""


def _unique(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """RFC 6749 §3.2: a request parameter must not be included more than once."""
    form: dict[str, str] = {}
    for name, value in pairs:
        if name in form:
            raise _Refused(_INVALID_REQUEST, "A form parameter is repeated.")
        form[name] = value
    return form


def _authenticate(form: Mapping[str, str]) -> None:
    """Authenticate the registered client before any key or signing work.

    A wrong `client_id` never reads the secret store. A mismatched secret reads it once more,
    which is what lets a rotated secret take effect without a new container.
    """
    client_id = form.get("client_id", "")
    presented = form.get("client_secret", "")
    if not presented or not hmac.compare_digest(
        client_id.encode("utf-8"), SETTINGS.agent_client_id.encode("utf-8")
    ):
        raise _Refused(
            _INVALID_CLIENT, "The client is not the registered agent client.", _UNAUTHORIZED
        )
    if CLIENT_SECRET.current().matches(presented):
        return
    if CLIENT_SECRET.refreshed().matches(presented):
        return
    raise _Refused(
        _INVALID_CLIENT, "The presented client authentication does not match.", _UNAUTHORIZED
    )


def _exchange_subject_token(form: Mapping[str, str]) -> str:
    """Check the grant, the subject type, the actor, and the target; return the subject token."""
    if form.get("grant_type") != TOKEN_EXCHANGE_GRANT_TYPE:
        raise _Refused(_UNSUPPORTED_GRANT_TYPE, "The grant is not token exchange.")
    if form.get("subject_token_type") not in _SUBJECT_TOKEN_TYPES:
        raise _Refused(_INVALID_REQUEST, "The subject type is not accepted.")
    if any(name in form for name in _ACTOR_PARAMETERS):
        raise _Refused(_INVALID_REQUEST, "An actor is not accepted.")
    for name in _TARGET_PARAMETERS:
        if name in form and form[name] != SETTINGS.gateway_token_audience:
            raise _Refused(_INVALID_TARGET, f"The {name} is not the Gateway audience.")
    subject_token = form.get("subject_token", "")
    if not subject_token:
        raise _Refused(_INVALID_REQUEST, "No subject was presented.")
    return subject_token


def _verified_subject(subject_token: str, now: datetime) -> IssuedTokenSubject:
    """Verify the subject as a Harness token this issuer minted for the conversation route."""
    return verify_issued_token(
        VerifyIssuedTokenRequest(
            token=subject_token,
            keys=JsonWebKeySet(keys=(_published_jwk(),)),
            expected_issuer=SETTINGS.token_issuer,
            expected_key_id=SETTINGS.token_signing_key_id,
            expected_audience=SETTINGS.harness_token_audience,
            expected_client_id=SETTINGS.route_client_id,
            now=now,
        )
    )


def _mint(subject: IssuedTokenSubject, now: datetime, lifetime: int) -> str:
    """Sign a Gateway-audience token for the subject; the agent client is its actor."""
    unsigned = unsigned_token(
        IssueTokenRequest(
            issuer=SETTINGS.token_issuer,
            subject=subject.subject,
            audience=SETTINGS.gateway_token_audience,
            client_id=SETTINGS.agent_client_id,
            key_id=SETTINGS.token_signing_key_id,
            issued_at=now,
            lifetime_seconds=lifetime,
            token_id=uuid.uuid4().hex,
        )
    )
    return compact_jws(unsigned, SIGNER.sign(unsigned.signing_input))


def _form_key_label(name: str) -> str:
    """Label a parameter name for diagnostics; an unlisted name is kept only when bounded."""
    label = _FORM_KEY_LABELS.get(name)
    if label is not None:
        return label
    return f"other:{name}" if _UNLISTED_KEY.fullmatch(name) else "other"


def _report_exchange(exchange: _ExchangeDiagnostics, *, status: int | None) -> None:
    """Emit the one terminal `/token` record with its P2 diagnostics."""
    issued = exchange.outcome == _ISSUED
    report_operation(
        OperationLog(
            operation=OPERATION,
            component="token_service",
            phase="terminal",
            outcome=exchange.outcome,
            severity=LogSeverity.INFO if issued else LogSeverity.WARNING,
            description=exchange.reason,
            details={"status": status, **exchange.details()},
        ),
        LOGGER,
    )


@lru_cache(maxsize=1)
def _published_jwk() -> JsonWebKey:
    """Return the published verification key, cached per container once KMS has answered.

    A failure is not cached, so the next request retries KMS rather than serving nothing.
    """
    return jwk_from_der_public_key(SIGNER.public_key_der(), SETTINGS.token_signing_key_id)


def _key_set() -> Mapping[str, object]:
    return {"keys": [_published_jwk()]}


def _exchange_response(status: int, document: Mapping[str, object]) -> dict[str, object]:
    return {
        "statusCode": status,
        "headers": dict(_EXCHANGE_HEADERS),
        "body": json.dumps(document, separators=(",", ":")),
    }


def _document(document: Mapping[str, object]) -> dict[str, object]:
    return {
        "statusCode": 200,
        "headers": dict(_DOCUMENT_HEADERS),
        "body": json.dumps(document, separators=(",", ":")),
    }
