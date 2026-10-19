# SPDX-License-Identifier: MIT-0
"""Outbound boundary for ADR-075 §5 provider-terminal observation of one attempt.

The writer and the reconciler may only progress a submitted attempt after
observing the provider terminal (``SUCCEEDED | FAILED | CANCELLED``) for its
durable valid query execution identity. This port owns exactly that observation,
the write-once ``StopQueryExecution`` companion the cancellation step issues, and
the verification readback rows. One observation is one provider call; the client
never raises for a provider, transport, or decoding failure — an unobservable
state is returned as the typed nonterminal result and the caller's monotonic
deadlines own all retry behaviour. Readback rows may raise a typed dependency
error because a failed readback is a retryable unit outcome, not an ambiguity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from domain.operations.table_attempt import ProviderTerminalState

__all__ = [
    "ObserveTableAttemptResult",
    "ProviderNonterminal",
    "ProviderTerminalObservation",
    "TableAttemptTerminalClient",
]


@dataclass(frozen=True, slots=True)
class ProviderTerminalObservation:
    """One provider-terminal observation with its exact known/missing quantities.

    ``query_count`` is exactly one for every accepted terminal (§6); the two
    Athena quantities come only from this terminal ``GetQueryExecution`` result
    and are ``None`` exactly when the provider omitted them — no adapter supplies
    zero, elapsed wall time, or a local estimate.
    """

    state: ProviderTerminalState
    query_count: int
    scanned_bytes: int | None
    engine_ms: int | None

    def __post_init__(self) -> None:
        if self.query_count != 1:
            raise ValueError("one accepted provider terminal records exactly one query.")
        for name in ("scanned_bytes", "engine_ms"):
            value: int | None = getattr(self, name)
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer exactly when known.")


@dataclass(frozen=True, slots=True)
class ProviderNonterminal:
    """The provider state is not yet terminal, or could not be observed this call."""

    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("a nonterminal observation names its closed reason.")


type ObserveTableAttemptResult = ProviderTerminalObservation | ProviderNonterminal


@runtime_checkable
class TableAttemptTerminalClient(Protocol):
    """One provider observation, one write-once stop, and the verification readback."""

    def observe(self, query_execution_id: str) -> ObserveTableAttemptResult:
        """Issue exactly one ``GetQueryExecution``; never raise past this boundary."""
        ...

    def stop(self, query_execution_id: str) -> None:
        """Issue exactly one ``StopQueryExecution``; success is not terminality."""
        ...

    def readback_rows(self, query_execution_id: str) -> tuple[tuple[str | None, ...], ...]:
        """Fetch the verification statement's complete result rows, header dropped."""
        ...
