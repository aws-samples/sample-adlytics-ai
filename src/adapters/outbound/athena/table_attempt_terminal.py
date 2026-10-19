# SPDX-License-Identifier: MIT-0
"""The dedicated ADR-075 §5 provider-terminal observation client for one attempt.

One ``GetQueryExecution`` per ``observe`` call and one ``StopQueryExecution`` per
``stop`` call: the default client carries the same one-internal-attempt botocore
configuration as the dedicated start client, so the SDK never silently repeats a
provider call whose outcome the caller's deadlines must own. ``observe`` and
``stop`` never raise past this boundary — any exception or malformed response is
the typed nonterminal result, because absence of an observation is never evidence
of terminality (§5); every such exception is reported through ``report_error``.
``scanned_bytes`` comes only from ``DataScannedInBytes`` and ``engine_ms`` only
from ``EngineExecutionTimeInMillis`` on the terminal result; a missing quantity
stays missing (§6). The verification readback delegates to the
existing paged result reader and may raise, because a failed readback is a
retryable unit outcome rather than a provider ambiguity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import boto3
from aws_lambda_powertools import Logger

from adapters.outbound.athena.execution import fetch_rows
from adapters.outbound.athena.table_attempt_start import START_RETRY_CONFIG
from adapters.outbound.telemetry.reporting import Report, report_error
from domain.operations.table_attempt import ProviderTerminalState
from ports.outbound.table_attempt_terminal import (
    ObserveTableAttemptResult,
    ProviderNonterminal,
    ProviderTerminalObservation,
)

if TYPE_CHECKING:  # pragma: no cover — kept out of the Lambda import path
    from types_boto3_athena.client import AthenaClient

__all__ = [
    "Stage1AthenaTerminalClient",
    "dedicated_terminal_athena_client",
]

_TERMINAL_STATES: Final = frozenset(member.value for member in ProviderTerminalState)
_NONTERMINAL_STATES: Final = frozenset({"QUEUED", "RUNNING"})
_QUERY_COUNT: Final = 1
# The query execution id is provider-issued, so it is not a correlation id; the provider request
# id and operation reach the log through report_error's provider failure context.
_REPORT: Final = Report(
    operation="table_attempt.terminal",
    correlation_id="",
    logger=Logger(service="adlytics-table-attempt"),
)


def dedicated_terminal_athena_client() -> AthenaClient:
    """Build the one-attempt Athena client the dedicated observation path requires."""
    return boto3.client("athena", config=START_RETRY_CONFIG)


def _member(container: object, name: str) -> object:
    if not isinstance(container, dict):
        return None
    return cast("dict[object, object]", container).get(name)


def _known_quantity(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _classified(response: object) -> ObserveTableAttemptResult:
    """Classify Athena's closed state vocabulary, each member by name.

    ``QUEUED | RUNNING | SUCCEEDED | FAILED | CANCELLED`` is the documented
    ``QueryExecutionStatus.State`` set. A state outside it is never read as terminal
    (absence of a known terminal is not terminality, §5); it is named
    ``state_unrecognized`` so it cannot pass for an ordinary running query, and the
    caller's Q/S deadlines still bound the wait.
    """
    execution = _member(response, "QueryExecution")
    state = _member(_member(execution, "Status"), "State")
    if not isinstance(state, str):
        return ProviderNonterminal(reason="observation_malformed")
    if state in _NONTERMINAL_STATES:
        return ProviderNonterminal(reason="nonterminal")
    if state not in _TERMINAL_STATES:
        return ProviderNonterminal(reason="state_unrecognized")
    statistics = _member(execution, "Statistics")
    return ProviderTerminalObservation(
        state=ProviderTerminalState(state),
        query_count=_QUERY_COUNT,
        scanned_bytes=_known_quantity(_member(statistics, "DataScannedInBytes")),
        engine_ms=_known_quantity(_member(statistics, "EngineExecutionTimeInMillis")),
    )


class Stage1AthenaTerminalClient:
    """§5 provider-terminal observation, write-once stop, and verification readback.

    An injected client must already carry the one-internal-attempt configuration;
    the default construction pins it. The composing entry points use the default.
    """

    def __init__(self, client: AthenaClient | None = None) -> None:
        self._client: AthenaClient = (
            client if client is not None else dedicated_terminal_athena_client()
        )

    def observe(self, query_execution_id: str) -> ObserveTableAttemptResult:
        """Issue exactly one ``GetQueryExecution`` and classify its closed result."""
        if not query_execution_id.strip():
            return ProviderNonterminal(reason="observation_malformed")
        try:
            response = self._client.get_query_execution(QueryExecutionId=query_execution_id)
        except Exception as error:
            report_error(error, _REPORT)
            return ProviderNonterminal(reason="observation_unavailable")
        return _classified(response)

    def stop(self, query_execution_id: str) -> None:
        """Issue exactly one ``StopQueryExecution``; success is not terminality."""
        # Cancellation persistence is already durable; the caller's deadlines own retry, so a
        # failed stop is reported and never raised.
        try:
            self._client.stop_query_execution(QueryExecutionId=query_execution_id)
        except Exception as error:
            report_error(error, _REPORT)

    def readback_rows(self, query_execution_id: str) -> tuple[tuple[str | None, ...], ...]:
        """Fetch the verification statement's complete result rows, header dropped."""
        return tuple(tuple(row) for row in fetch_rows(self._client, query_execution_id))
