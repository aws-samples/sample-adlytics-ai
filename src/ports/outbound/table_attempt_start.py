# SPDX-License-Identifier: MIT-0
"""Outbound boundary for the dedicated ADR-075 §5 step-3 Athena start client.

The complete public result is exactly
``Accepted(query_execution_id, sanitized_evidence) |
AcceptanceAmbiguous(reason, sanitized_evidence)``. A valid query execution
identity is exactly a String containing at least one non-whitespace character.
Every exception after Begin — ``ClientError``, transport, timeout, and unknown
classes — plus every malformed SDK result, missing field, wrong type, or blank
identity is ``AcceptanceAmbiguous``; this contract makes no SDK/service-error
classification claim beyond the closed ``provider_error_class`` mapping.
``Accepted`` always carries ``provider_error_class=none`` and the ambiguous
``reason`` is exactly the selected ``provider_error_class`` value. The envelope
crosses this boundary as the opaque persisted canonical start bytes; composing
it is an adapter's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from domain.operations.table_attempt import (
    ProviderErrorClass,
    SanitizedStartEvidence,
    valid_query_execution_id,
)

__all__ = [
    "AcceptanceAmbiguous",
    "Accepted",
    "StartTableAttemptRequest",
    "StartTableAttemptResult",
    "TableAttemptStartClient",
]

_TOKEN_TEXT: Final = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class StartTableAttemptRequest:
    """The persisted canonical start envelope and the attempt's client request token."""

    start_envelope: str
    client_request_token: str

    def __post_init__(self) -> None:
        if not self.start_envelope.strip():
            raise ValueError("a start requires the persisted canonical start envelope.")
        if not _TOKEN_TEXT.match(self.client_request_token):
            raise ValueError(
                "the client request token is the derived attempt id: 64 lowercase hex."
            )


@dataclass(frozen=True, slots=True)
class Accepted:
    """Athena returned a valid query execution identity; evidence class is ``none``."""

    query_execution_id: str
    sanitized_evidence: SanitizedStartEvidence

    def __post_init__(self) -> None:
        if not valid_query_execution_id(self.query_execution_id):
            raise ValueError("Accepted requires at least one non-whitespace character.")
        if self.sanitized_evidence.provider_error_class is not ProviderErrorClass.NONE:
            raise ValueError("Accepted always carries provider_error_class=none.")


@dataclass(frozen=True, slots=True)
class AcceptanceAmbiguous:
    """No valid identity was proven; absence is never evidence of rejection."""

    reason: ProviderErrorClass
    sanitized_evidence: SanitizedStartEvidence

    def __post_init__(self) -> None:
        if self.reason is ProviderErrorClass.NONE:
            raise ValueError("an ambiguous outcome never carries the none error class.")
        if self.reason is not self.sanitized_evidence.provider_error_class:
            raise ValueError(
                "the ambiguous reason is exactly the selected provider_error_class value."
            )


type StartTableAttemptResult = Accepted | AcceptanceAmbiguous


@runtime_checkable
class TableAttemptStartClient(Protocol):
    """The dedicated Stage-1 start client: one SDK attempt, one call per invocation."""

    def start_table_attempt(self, request: StartTableAttemptRequest) -> StartTableAttemptResult:
        """Call ``StartQueryExecution`` exactly once and return the closed result.

        The implementation never raises for a provider, transport, or decoding
        failure — every post-Begin failure is an ``AcceptanceAmbiguous`` value.
        """
        ...
