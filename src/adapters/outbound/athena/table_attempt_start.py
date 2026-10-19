# SPDX-License-Identifier: MIT-0
"""The dedicated ADR-075 §5 step-3 Stage-1 Athena start client.

One SDK internal attempt and one ``StartQueryExecution`` call per invocation:
the default client is built with botocore ``retries={"total_max_attempts": 1}``,
so the SDK never silently retries a Start whose acceptance is unknown. The
client never raises past its boundary — every exception after Begin
(``ClientError``, transport, timeout, unknown) and every malformed SDK result,
missing field, wrong type, or blank query execution identity returns
``AcceptanceAmbiguous``. Absence of a valid identity is never evidence of
rejection; both outcomes leave every Begin authority untouched. A ``ClientError``
or unknown exception is reported through ``report_error`` before it becomes that
result, so the closed class is never the only trace of the failure.

The closed ``provider_error_class`` mapping recognizes throttling,
service-unavailable, and network-timeout signals plus a malformed provider
response; everything unmapped is exactly ``unknown``. ``sanitized_evidence`` is
the closed two-field form — an integer-or-null provider status code and the
closed class — with no free text, message, request ID, or provider payload.
No SDK or service-error classification claim exists beyond this mapping.

Result reuse is constantly disabled (§6: the design does not depend on it), so
the persisted envelope plus the client request token fully determine the request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

from adapters.outbound.athena.table_attempt_statements import (
    ParsedStartEnvelope,
    parse_start_envelope,
)
from adapters.outbound.telemetry.reporting import Report, report_error
from domain.operations.table_attempt import (
    ProviderErrorClass,
    SanitizedStartEvidence,
    valid_query_execution_id,
)
from ports.outbound.table_attempt_start import (
    AcceptanceAmbiguous,
    Accepted,
    StartTableAttemptRequest,
    StartTableAttemptResult,
)

if TYPE_CHECKING:  # pragma: no cover — kept out of the Lambda import path
    from types_boto3_athena.client import AthenaClient

__all__ = [
    "START_RETRY_CONFIG",
    "Stage1AthenaStartClient",
    "dedicated_start_athena_client",
]

START_RETRY_CONFIG: Final = Config(retries={"total_max_attempts": 1})
"""Exactly one SDK internal attempt: no silent retry may follow an ambiguous Start."""

_THROTTLING_CODES: Final = frozenset(
    {"ThrottlingException", "Throttling", "TooManyRequestsException"}
)
_SERVICE_UNAVAILABLE_CODES: Final = frozenset({"ServiceUnavailableException", "ServiceUnavailable"})
_THROTTLING_STATUS: Final = 429
_SERVICE_UNAVAILABLE_STATUS: Final = 503
_LOGGER: Final = Logger(service="adlytics-table-attempt")
_OPERATION: Final = "table_attempt.start"


def dedicated_start_athena_client() -> AthenaClient:
    """Build the one-attempt Athena client the dedicated start path requires."""
    return boto3.client("athena", config=START_RETRY_CONFIG)


def _evidence(status_code: int | None, error_class: ProviderErrorClass) -> SanitizedStartEvidence:
    return SanitizedStartEvidence(
        provider_status_code=status_code, provider_error_class=error_class
    )


def _ambiguous(error_class: ProviderErrorClass, status_code: int | None) -> AcceptanceAmbiguous:
    return AcceptanceAmbiguous(
        reason=error_class, sanitized_evidence=_evidence(status_code, error_class)
    )


def _plain_status(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _member(container: object, name: str) -> object:
    if not isinstance(container, dict):
        return None
    return cast("dict[object, object]", container).get(name)


def _response_status(response: object) -> int | None:
    return _plain_status(_member(_member(response, "ResponseMetadata"), "HTTPStatusCode"))


def _client_error_result(error: ClientError) -> AcceptanceAmbiguous:
    response: object = getattr(error, "response", None)
    status = _response_status(response)
    raw_code = _member(_member(response, "Error"), "Code")
    code = raw_code if isinstance(raw_code, str) else ""
    if code in _THROTTLING_CODES or status == _THROTTLING_STATUS:
        return _ambiguous(ProviderErrorClass.THROTTLING, status)
    if code in _SERVICE_UNAVAILABLE_CODES or status == _SERVICE_UNAVAILABLE_STATUS:
        return _ambiguous(ProviderErrorClass.SERVICE_UNAVAILABLE, status)
    return _ambiguous(ProviderErrorClass.UNKNOWN, status)


def _classified_response(response: object) -> StartTableAttemptResult:
    status = _response_status(response)
    identity = _member(response, "QueryExecutionId")
    if not isinstance(identity, str) or not valid_query_execution_id(identity):
        return _ambiguous(ProviderErrorClass.RESPONSE_MALFORMED, status)
    return Accepted(
        query_execution_id=identity,
        sanitized_evidence=_evidence(status, ProviderErrorClass.NONE),
    )


class Stage1AthenaStartClient:
    """The dedicated start client: exactly one ``StartQueryExecution`` per invocation.

    An injected client must already carry ``START_RETRY_CONFIG``; the default
    construction pins it. The composing entry points use the default.
    """

    def __init__(self, client: AthenaClient | None = None) -> None:
        self._client: AthenaClient = (
            client if client is not None else dedicated_start_athena_client()
        )

    def start_table_attempt(self, request: StartTableAttemptRequest) -> StartTableAttemptResult:
        """Replay the persisted envelope once and return the closed §5 step-3 result."""
        report = Report(
            operation=_OPERATION, correlation_id=request.client_request_token, logger=_LOGGER
        )
        try:
            envelope = parse_start_envelope(request.start_envelope)
        except Exception as error:
            report_error(error, report)
            return _ambiguous(ProviderErrorClass.UNKNOWN, None)
        try:
            response = self._start_once(envelope, request.client_request_token)
        except ClientError as error:
            report_error(error, report)
            return _client_error_result(error)
        except ConnectTimeoutError, ReadTimeoutError:
            return _ambiguous(ProviderErrorClass.NETWORK_TIMEOUT, None)
        except Exception as error:
            report_error(error, report)
            return _ambiguous(ProviderErrorClass.UNKNOWN, None)
        return _classified_response(response)

    def _start_once(self, envelope: ParsedStartEnvelope, token: str) -> object:
        """Issue the exact stored request; the envelope entries pass through verbatim.

        The reuse literal is inlined at each call site so pyright checks it against
        ``ResultReuseConfigurationTypeDef`` from context, as ``execution.start`` does.
        """
        if envelope.execution_parameters:
            return self._client.start_query_execution(
                QueryString=envelope.query_string,
                WorkGroup=envelope.workgroup,
                QueryExecutionContext={"Database": envelope.database},
                ClientRequestToken=token,
                ExecutionParameters=list(envelope.execution_parameters),
                ResultReuseConfiguration={"ResultReuseByAgeConfiguration": {"Enabled": False}},
            )
        return self._client.start_query_execution(
            QueryString=envelope.query_string,
            WorkGroup=envelope.workgroup,
            QueryExecutionContext={"Database": envelope.database},
            ClientRequestToken=token,
            ResultReuseConfiguration={"ResultReuseByAgeConfiguration": {"Enabled": False}},
        )
