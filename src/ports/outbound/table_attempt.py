# SPDX-License-Identifier: MIT-0
"""Outbound boundary for the ADR-075 §3.1 table-scoped provider-attempt authority.

This slice owns only what the live path needs: strongly consistent reads of the
fence, attempt, run, and protected case, plus ``RecordSubmittedAttemptRequest`` —
the sole direct (non-transactional) mutation in the §3.2 matrix. The coordinator
transactions (Begin, Recover, Park, Complete, Block, ...) belong to a later slice
and are deliberately absent here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from domain.operations.table_attempt import (
    FenceRecord,
    InlineRun,
    ProtectedReconcileCase,
    ProviderErrorClass,
    SanitizedStartEvidence,
    TableAttempt,
    is_canonical_instant,
    is_canonical_table_id,
    valid_query_execution_id,
)

__all__ = [
    "RecordSubmittedAttemptRequest",
    "RecordSubmittedAttemptResult",
    "TableAttemptStore",
]


@dataclass(frozen=True, slots=True)
class RecordSubmittedAttemptRequest:
    """One live ``Accepted`` outcome to record as the conditional ``intent→submitted`` write.

    The envelope digest and client request token are the immutable stored values the
    condition must match; the query identity, submission instant, evidence form, and
    state are written together, write-once.
    """

    physical_table_id: str
    attempt_id: str
    envelope_digest: str
    client_request_token: str
    query_execution_id: str
    submitted_at: str
    sanitized_start_evidence: SanitizedStartEvidence

    def __post_init__(self) -> None:
        if not is_canonical_table_id(self.physical_table_id):
            raise ValueError("recording requires a canonical database-qualified table id.")
        if not self.attempt_id.strip():
            raise ValueError("recording requires the exact attempt id.")
        if not self.envelope_digest.strip():
            raise ValueError("recording requires the immutable envelope digest.")
        if not self.client_request_token.strip():
            raise ValueError("recording requires the immutable client request token.")
        if not valid_query_execution_id(self.query_execution_id):
            raise ValueError("recording requires a valid query execution id.")
        if not is_canonical_instant(self.submitted_at):
            raise ValueError("submitted_at must be a 20-digit epoch-microseconds instant.")
        if self.sanitized_start_evidence.provider_error_class is not ProviderErrorClass.NONE:
            raise ValueError("only an Accepted outcome, whose error class is none, is recordable.")


class RecordSubmittedAttemptResult(StrEnum):
    """Recorded now, or proven a byte-identical already-durable replay (no-op)."""

    RECORDED = "recorded"
    REPLAYED = "replayed"


@runtime_checkable
class TableAttemptStore(Protocol):
    """Strongly consistent table-attempt authority reads plus the one direct mutation."""

    def read_fence(self, table_id: str) -> FenceRecord | None:
        """Strongly read one ``FENCE``; absent is ``None``; malformed is a typed error."""
        ...

    def read_attempt(self, table_id: str, attempt_id: str) -> TableAttempt | None:
        """Strongly read one ``ATTEMPT#``; absent is ``None``; malformed is a typed error."""
        ...

    def read_run(self, table_id: str, run_id: str) -> InlineRun | None:
        """Strongly read one ``RUN#``; absent is ``None``; malformed is a typed error."""
        ...

    def read_protected_case(self, recovery_ref: str) -> ProtectedReconcileCase | None:
        """Strongly read one protected case; absent is ``None``; malformed is a typed error."""
        ...

    def record_submitted_attempt(
        self, request: RecordSubmittedAttemptRequest
    ) -> RecordSubmittedAttemptResult:
        """Conditionally record ``intent→submitted``; exact replay is a no-op.

        A changed id, time, digest, token, or evidence byte is an invariant collision
        raised as a typed error, never retried.
        """
        ...
