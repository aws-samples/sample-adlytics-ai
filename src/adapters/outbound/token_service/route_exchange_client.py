# SPDX-License-Identifier: MIT-0
"""Route exchange client: the Conversation Function's one call to the token service.

Implements HarnessTokenPort by invoking the route-exchange function directly
(`RequestResponse`). The function has no route and a resource-policy Deny for every
principal but the Conversation Function role (ADR-077 §3), so this is the only caller that
can reach it. The client is built once per process; the tokens it carries are per call and
are never cached, logged, or placed in an exception.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from adapters.outbound.aws_errors import aws_failure_context
from adapters.outbound.telemetry.tracing import CONVERSATION_ROUTE_EXCHANGE, span, trace_boundary
from domain.identity.session import IdentityError
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.harness_token import HarnessToken, HarnessTokenPort

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_lambda.client import LambdaClient

__all__ = [
    "INVALID_GRANT",
    "ROUTE_EXCHANGE_UNAVAILABLE",
    "LambdaRouteTokenExchange",
    "harness_token_from",
]

ROUTE_EXCHANGE_UNAVAILABLE = ErrorCode("TOKENSVC", 3)

INVALID_GRANT = {"error": "invalid_grant"}
"""The route exchange's only refusal document; it never carries a reason."""

_TOKEN_TYPE = "Bearer"  # noqa: S105 — an OAuth token_type value, not a credential
_SUCCESS_KEYS = frozenset({"access_token", "token_type", "expires_in"})
_OPERATION = "conversation.token_exchange"
_UNAVAILABLE = "The conversation service is temporarily unavailable."


class LambdaRouteTokenExchange(HarnessTokenPort):
    """Exchange a verified Cognito access token for a Harness token through Lambda Invoke."""

    def __init__(
        self, function_arn: str, *, region: str, client: LambdaClient | None = None
    ) -> None:
        if not function_arn.strip():
            raise ValueError("The route-exchange function ARN is required.")
        self._function_arn = function_arn
        self._client: LambdaClient = (
            client
            if client is not None
            else boto3.client(
                "lambda",
                region_name=region,
                config=Config(
                    connect_timeout=2,
                    read_timeout=10,
                    retries={"mode": "standard", "total_max_attempts": 2},
                ),
            )
        )

    @trace_boundary(CONVERSATION_ROUTE_EXCHANGE)
    def exchange(self, subject_token: str) -> HarnessToken:
        """Return the minted Harness token, or raise IdentityError / DependencyError."""
        with span(CONVERSATION_ROUTE_EXCHANGE, operation=_OPERATION) as recording:
            if not subject_token:
                raise IdentityError("No subject token to exchange.")
            try:
                response = self._client.invoke(
                    FunctionName=self._function_arn,
                    InvocationType="RequestResponse",
                    Payload=json.dumps({"subject_token": subject_token}).encode(),
                )
                raw = response["Payload"].read()
            except (BotoCoreError, ClientError) as error:
                raise DependencyError(
                    ROUTE_EXCHANGE_UNAVAILABLE, _UNAVAILABLE, context=aws_failure_context(error)
                ) from error
            if response.get("FunctionError"):
                # The payload of a function error is the function's own error report. It is not
                # read further: the token service logs its own failure under its own span.
                raise DependencyError(
                    ROUTE_EXCHANGE_UNAVAILABLE,
                    _UNAVAILABLE,
                    context={"cause": "function_error"},
                )
            try:
                token = harness_token_from(raw)
            except IdentityError:
                recording.record(outcome="refused")
                raise
            recording.record(outcome="exchanged")
            return token


def harness_token_from(raw: bytes) -> HarnessToken:
    """Decode the route exchange's response document, exactly and fail-closed.

    `{"error": "invalid_grant"}` is the only refusal and raises IdentityError. Every other
    shape that is not exactly the success document is a DependencyError: a contract drift must
    never be read as a token, and must never be answered to the user as a refused session.
    """
    try:
        parsed: object = json.loads(raw)
    except UnicodeDecodeError, json.JSONDecodeError:
        raise DependencyError(
            ROUTE_EXCHANGE_UNAVAILABLE, _UNAVAILABLE, context={"cause": "unparseable_response"}
        ) from None
    if parsed == INVALID_GRANT:
        raise IdentityError("The token service refused the subject token.")
    if not isinstance(parsed, dict):
        raise _malformed()
    document = cast("dict[str, object]", parsed)
    access_token = document.get("access_token")
    expires_in = document.get("expires_in")
    if (
        frozenset(document) != _SUCCESS_KEYS
        or document.get("token_type") != _TOKEN_TYPE
        or not isinstance(access_token, str)
        or not access_token
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or expires_in < 1
    ):
        raise _malformed()
    return HarnessToken(access_token=access_token, expires_in=expires_in)


def _malformed() -> DependencyError:
    return DependencyError(
        ROUTE_EXCHANGE_UNAVAILABLE, _UNAVAILABLE, context={"cause": "unexpected_response_shape"}
    )
