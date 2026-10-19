# SPDX-License-Identifier: MIT-0
"""Route exchange: a Cognito access token in, a single-turn Harness token out (ADR-077 §2, §3).

Invoked directly by the Conversation Function and by nothing else: the function has no route,
and its resource policy denies every other principal. The subject token passes exactly the
verification the route itself applies (`verify_access_token` against the Cognito issuer and the
BFF client), so this function can never mint for a token the route would have refused.

The minted token's lifetime is configuration, not the subject's remaining life. A refused
subject answers `{"error": "invalid_grant"}` and nothing else; an internal failure raises, so
Lambda reports a function error rather than a refusal a caller could mistake for a bad session.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from aws_lambda_powertools import Logger

from adapters.config import token_service_settings, tracing_settings
from adapters.outbound.cognito.identity_provider import CognitoSigningKeys
from adapters.outbound.kms.token_signer import KmsTokenSigner
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
from adapters.outbound.telemetry.tracing import (
    ROUTE_EXCHANGE_PROCESS,
    Recording,
    span,
    trace_boundary,
)
from domain.identity.issuance import IssueTokenRequest, compact_jws, unsigned_token
from domain.identity.session import Identity, IdentityError, utc_now
from domain.identity.tokens import (
    UnknownSigningKeyError,
    VerifyAccessTokenRequest,
    verify_access_token,
)
from domain.shared_kernel.errors import AdlyticsError
from ports.outbound.token_signing import TokenSigningPort

__all__ = ["INVALID_GRANT", "handler"]

SETTINGS = token_service_settings()
TRACING = configure_tracing(tracing_settings())
KEYS = CognitoSigningKeys(SETTINGS.cognito_jwks_url)
"""One per container, so the Cognito key set cache survives across invocations."""

SIGNER: TokenSigningPort = KmsTokenSigner(SETTINGS.token_signing_key_id, region=SETTINGS.aws_region)
LOGGER = Logger(service="adlytics-token-route-exchange")
OPERATION = "token.route_exchange"
TOKEN_TYPE = "Bearer"  # noqa: S105 — an OAuth token_type value, not a credential

INVALID_GRANT: dict[str, str] = {"error": "invalid_grant"}
"""The only refusal. It never carries a reason: a reason tells a caller which check failed."""

_REQUEST_KEYS = frozenset({"subject_token"})
_TRACE_CARRIER_KEY = "headers"
"""The one extra key a request may carry, and it is never read here.

The caller's botocore OpenTelemetry instrumentation injects W3C trace context into every Lambda
invoke payload as a `headers` object (opentelemetry-instrumentation-botocore, lmbd.py). It carries
no identity, so it is tolerated when it is a flat string map and ignored; any other key refuses.
"""


@trace_boundary(ROUTE_EXCHANGE_PROCESS)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: object) -> dict[str, object]:  # noqa: ARG001
    """Mint one Harness token for a verified subject, or answer `invalid_grant`."""
    with span(ROUTE_EXCHANGE_PROCESS, operation=OPERATION) as process_span:
        report = Report(operation=OPERATION, correlation_id="", logger=LOGGER)
        try:
            identity = _verified_subject(event)
        except IdentityError as exc:
            # Includes an unknown signing key that survived one refresh.
            report_denial(str(exc), report)
            process_span.record(outcome="refused")
            return dict(INVALID_GRANT)
        except Exception as exc:
            _report_failure(exc, report, process_span)
            raise

        try:
            minted = _mint(identity)
        except Exception as exc:
            _report_failure(exc, report, process_span)
            raise

        report_operation(
            OperationLog(
                operation=OPERATION,
                component="token_service",
                phase="terminal",
                outcome="exchanged",
                severity=LogSeverity.INFO,
            ),
            LOGGER,
        )
        process_span.record(outcome="exchanged")
        return {
            "access_token": minted,
            "token_type": TOKEN_TYPE,
            "expires_in": SETTINGS.harness_token_lifetime_seconds,
        }


def _verified_subject(event: object) -> Identity:
    """Verify the subject token exactly as the route does, refreshing keys once on rotation."""
    subject_token = _subject_token(event)
    request = VerifyAccessTokenRequest(
        token=subject_token,
        keys=KEYS.signing_keys(),
        expected_issuer=SETTINGS.cognito_issuer,
        expected_client_id=SETTINGS.cognito_client_id,
        now=utc_now(),
    )
    try:
        return verify_access_token(request)
    except UnknownSigningKeyError:
        return verify_access_token(replace(request, keys=KEYS.refresh_signing_keys()))


def _subject_token(event: object) -> str:
    """Accept `{"subject_token": "<non-empty string>"}`; anything else is refused.

    The one tolerated addition is a `headers` trace carrier, which must be a flat map of strings
    (what the SDK's trace injection adds to the payload); any other member or shape is refused.
    """
    if not isinstance(event, dict):
        raise IdentityError("The exchange request is not a JSON object.")
    request: dict[object, object] = event  # pyright: ignore[reportUnknownVariableType]
    carrier = request.get(_TRACE_CARRIER_KEY)
    keys = frozenset(request) - {_TRACE_CARRIER_KEY}
    if keys != _REQUEST_KEYS or not _is_trace_carrier(carrier):
        raise IdentityError("The exchange request does not carry exactly a subject token.")
    token = request["subject_token"]
    if not isinstance(token, str) or not token:
        raise IdentityError("The exchange request carries no usable subject token.")
    return token


def _is_trace_carrier(value: object) -> bool:
    """Absent, or a flat map of strings: the only shape the SDK's trace injection produces."""
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    entries: dict[object, object] = value  # pyright: ignore[reportUnknownVariableType]
    return all(isinstance(k, str) and isinstance(v, str) for k, v in entries.items())


def _mint(identity: Identity) -> str:
    """Sign a Harness-audience token for the verified subject; the route is its only client."""
    unsigned = unsigned_token(
        IssueTokenRequest(
            issuer=SETTINGS.token_issuer,
            subject=identity.subject,
            audience=SETTINGS.harness_token_audience,
            client_id=SETTINGS.route_client_id,
            key_id=SETTINGS.token_signing_key_id,
            issued_at=utc_now(),
            lifetime_seconds=SETTINGS.harness_token_lifetime_seconds,
            token_id=uuid.uuid4().hex,
        )
    )
    return compact_jws(unsigned, SIGNER.sign(unsigned.signing_input))


def _report_failure(exc: Exception, report: Report, process_span: Recording) -> None:
    if isinstance(exc, AdlyticsError):
        process_span.note_error(exc)
    else:
        process_span.note_failure(exc)
    report_error(exc, report)
    process_span.record(outcome="failed")
