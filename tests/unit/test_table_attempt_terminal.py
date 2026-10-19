# SPDX-License-Identifier: MIT-0
"""ADR-075 §5 terminal client: closed state vocabulary; observe and stop report, never raise."""

from __future__ import annotations

from typing import Any, cast

import pytest

import adapters.outbound.athena.table_attempt_terminal as terminal_module
from adapters.outbound.athena.table_attempt_terminal import Stage1AthenaTerminalClient
from adapters.outbound.telemetry.reporting import Report
from domain.operations.table_attempt import ProviderTerminalState
from ports.outbound.table_attempt_terminal import (
    ProviderNonterminal,
    ProviderTerminalObservation,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

# Every identity below is fictional fixture data; none is copied from a live system.
QUERY_ID = "query-exec-fixture-0001"


class _FailingAthenaClient:
    """Hand-rolled stub: every provider call raises the configured error."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[str] = []

    def get_query_execution(self, **_kwargs: object) -> object:
        self.calls.append("get_query_execution")
        raise self.error

    def stop_query_execution(self, **_kwargs: object) -> object:
        self.calls.append("stop_query_execution")
        raise self.error


def _reports(monkeypatch: pytest.MonkeyPatch) -> list[tuple[BaseException, str]]:
    reported: list[tuple[BaseException, str]] = []

    def capture(error: BaseException, report: Report) -> None:
        reported.append((error, report.operation))

    monkeypatch.setattr(terminal_module, "report_error", capture)
    return reported


def test_a_failed_observation_is_reported_and_stays_the_typed_nonterminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported = _reports(monkeypatch)
    error = RuntimeError("fixture transport defect")
    stub = _FailingAthenaClient(error)
    result = Stage1AthenaTerminalClient(cast("Any", stub)).observe(QUERY_ID)
    assert result == ProviderNonterminal(reason="observation_unavailable")
    assert stub.calls == ["get_query_execution"]
    assert reported == [(error, "table_attempt.terminal")]


def test_a_failed_stop_is_reported_and_never_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed stop leaves the query running until the caller's deadline; the report says why."""
    reported = _reports(monkeypatch)
    error = RuntimeError("fixture transport defect")
    stub = _FailingAthenaClient(error)
    Stage1AthenaTerminalClient(cast("Any", stub)).stop(QUERY_ID)
    assert stub.calls == ["stop_query_execution"]
    assert reported == [(error, "table_attempt.terminal")]


def test_a_blank_identity_is_malformed_without_a_call_or_a_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported = _reports(monkeypatch)
    stub = _FailingAthenaClient(RuntimeError("unused"))
    result = Stage1AthenaTerminalClient(cast("Any", stub)).observe("  ")
    assert result == ProviderNonterminal(reason="observation_malformed")
    assert stub.calls == []
    assert reported == []


class _Athena:
    """Returns one canned ``GetQueryExecution`` response."""

    def __init__(self, state: object) -> None:
        self._state = state

    def get_query_execution(self, **_: object) -> dict[str, object]:
        return {
            "QueryExecution": {
                "Status": {"State": self._state},
                "Statistics": {"DataScannedInBytes": 0, "EngineExecutionTimeInMillis": 4_100},
            }
        }


def _observe(state: object) -> object:
    client = Stage1AthenaTerminalClient(cast("Any", _Athena(state)))
    return client.observe(QUERY_ID)


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_each_documented_terminal_state_is_its_own_terminal_observation(state: str) -> None:
    observed = _observe(state)
    assert isinstance(observed, ProviderTerminalObservation)
    assert observed.state is ProviderTerminalState(state)
    assert observed.engine_ms == 4_100


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING"])
def test_each_documented_running_state_is_an_ordinary_nonterminal(state: str) -> None:
    assert _observe(state) == ProviderNonterminal(reason="nonterminal")


def test_a_state_outside_the_documented_vocabulary_is_named_never_read_as_running() -> None:
    """An unknown state is not terminality (§5), and it must not pass for a running query."""
    assert _observe("PAUSED") == ProviderNonterminal(reason="state_unrecognized")
    assert _observe(None) == ProviderNonterminal(reason="observation_malformed")
