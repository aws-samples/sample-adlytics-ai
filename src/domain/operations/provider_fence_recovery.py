# SPDX-License-Identifier: MIT-0
"""The closed ``ProviderFenceRecoveryInvocation.v1``/``Result.v1`` wire forms (write discipline §3).

The provider-fence recovery broker lists canonical provider-reconcile blocks and requeues exactly
one canonical block output by its opaque recovery reference. Its wire schemas are versioned and
closed: extra, missing, retyped, blank, or unknown-mode fields reject before any client exists,
and every failure carries exactly one member of the five-value error vocabulary with no selector,
key, or raw provider content. Nothing here reads a table or calls a service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from domain.operations.table_attempt import is_canonical_instant
from domain.shared_kernel.errors import ErrorCode, ValidationError

__all__ = [
    "MAX_LIST_LIMIT",
    "PROVIDER_FENCE_RECOVERY_SCHEMA_VERSION",
    "BlockedCaseSummary",
    "ListBlockedCasesInvocation",
    "ListBlockedCasesResult",
    "ProviderFenceRecoveryError",
    "ProviderFenceRecoveryErrorCode",
    "ProviderFenceRecoveryInvocation",
    "ProviderFenceRecoveryResult",
    "RequeueBlockedCaseInvocation",
    "RequeueBlockedCaseResult",
    "RequeueOutcome",
    "parse_invocation",
    "result_document",
]

PROVIDER_FENCE_RECOVERY_SCHEMA_VERSION: Final = "1"
MAX_LIST_LIMIT: Final = 100
_MAX_OPERATION_ID_LENGTH: Final = 128
_MAX_REASON_LENGTH: Final = 512
_MAX_CURSOR_LENGTH: Final = 64
_LIST_FIELDS: Final = frozenset({"schema_version", "mode", "limit"})
_LIST_OPTIONAL: Final = frozenset({"cursor"})
_REQUEUE_FIELDS: Final = frozenset(
    {"schema_version", "mode", "recovery_ref", "operation_id", "resolution_reason"}
)
_UUID4_TEXT: Final = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_INVALID: Final = ErrorCode("PROVIDER_FENCE_RECOVERY", 1)


class ProviderFenceRecoveryErrorCode(StrEnum):
    """The five closed failure codes; no other error vocabulary exists."""

    INVALID_INVOCATION = "invalid_invocation"
    CASE_NOT_FOUND = "case_not_found"
    STALE_CASE = "stale_case"
    CONFLICT = "conflict"
    DEPENDENCY_FAILURE = "dependency_failure"


class ProviderFenceRecoveryError(ValidationError):
    """One closed failure; the message is exactly the code and nothing else."""

    def __init__(self, code: ProviderFenceRecoveryErrorCode) -> None:
        super().__init__(_INVALID, code.value)
        self.recovery_code = code


def _invalid() -> ProviderFenceRecoveryError:
    return ProviderFenceRecoveryError(ProviderFenceRecoveryErrorCode.INVALID_INVOCATION)


def _require_text(value: object, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise _invalid()
    return value


@dataclass(frozen=True, slots=True)
class ListBlockedCasesInvocation:
    """``mode="list"``: a bounded page, optionally after an opaque cursor."""

    limit: int
    cursor: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not 1 <= self.limit <= MAX_LIST_LIMIT:
            raise _invalid()
        if self.cursor is not None and not _UUID4_TEXT.match(self.cursor):
            raise _invalid()


@dataclass(frozen=True, slots=True)
class RequeueBlockedCaseInvocation:
    """``mode="requeue"``: exactly one canonical block output by opaque reference."""

    recovery_ref: str
    operation_id: str
    resolution_reason: str

    def __post_init__(self) -> None:
        if not _UUID4_TEXT.match(self.recovery_ref):
            raise _invalid()
        _require_text(self.operation_id, max_length=_MAX_OPERATION_ID_LENGTH)
        _require_text(self.resolution_reason, max_length=_MAX_REASON_LENGTH)


type ProviderFenceRecoveryInvocation = ListBlockedCasesInvocation | RequeueBlockedCaseInvocation


def parse_invocation(value: object) -> ProviderFenceRecoveryInvocation:
    """Accept exactly one closed variant; anything else is ``invalid_invocation``."""
    if not isinstance(value, dict):
        raise _invalid()
    document = cast("dict[object, object]", value)
    if any(not isinstance(key, str) for key in document):
        raise _invalid()
    typed = cast("dict[str, object]", document)
    if typed.get("schema_version") != PROVIDER_FENCE_RECOVERY_SCHEMA_VERSION:
        raise _invalid()
    mode = typed.get("mode")
    keys = frozenset(typed)
    if mode == "list":
        if not (_LIST_FIELDS <= keys <= _LIST_FIELDS | _LIST_OPTIONAL):
            raise _invalid()
        limit = typed["limit"]
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise _invalid()
        cursor = typed.get("cursor")
        if cursor is not None:
            cursor = _require_text(cursor, max_length=_MAX_CURSOR_LENGTH)
        return ListBlockedCasesInvocation(limit=limit, cursor=cursor)
    if mode == "requeue":
        if keys != _REQUEUE_FIELDS:
            raise _invalid()
        return RequeueBlockedCaseInvocation(
            recovery_ref=_require_text(typed["recovery_ref"], max_length=_MAX_CURSOR_LENGTH),
            operation_id=_require_text(typed["operation_id"], max_length=_MAX_OPERATION_ID_LENGTH),
            resolution_reason=_require_text(
                typed["resolution_reason"], max_length=_MAX_REASON_LENGTH
            ),
        )
    raise _invalid()


@dataclass(frozen=True, slots=True)
class BlockedCaseSummary:
    """One listed case: only its opaque reference, safe closed reason, and block instant."""

    recovery_ref: str
    safe_reason: str
    blocked_at: str

    def __post_init__(self) -> None:
        if not _UUID4_TEXT.match(self.recovery_ref):
            raise ValueError("a blocked case summary carries a canonical recovery reference.")
        if not self.safe_reason.strip():
            raise ValueError("a blocked case summary carries its closed reason.")
        if not is_canonical_instant(self.blocked_at):
            raise ValueError("a blocked case summary carries its 20-digit block instant.")


@dataclass(frozen=True, slots=True)
class ListBlockedCasesResult:
    """``mode="list"`` result: summaries plus the optional opaque continuation."""

    cases: tuple[BlockedCaseSummary, ...]
    next_cursor: str | None


class RequeueOutcome(StrEnum):
    """Closed requeue outcomes."""

    REQUEUED = "requeued"
    ALREADY_REQUEUED = "already_requeued"


@dataclass(frozen=True, slots=True)
class RequeueBlockedCaseResult:
    """``mode="requeue"`` result: the same opaque reference and the closed outcome."""

    recovery_ref: str
    outcome: RequeueOutcome


type ProviderFenceRecoveryResult = ListBlockedCasesResult | RequeueBlockedCaseResult


def result_document(result: ProviderFenceRecoveryResult) -> dict[str, object]:
    """Render the closed ``Result.v1`` wire form; no selector or provider content exists."""
    if isinstance(result, ListBlockedCasesResult):
        document: dict[str, object] = {
            "schema_version": PROVIDER_FENCE_RECOVERY_SCHEMA_VERSION,
            "mode": "list",
            "cases": [
                {
                    "recovery_ref": case.recovery_ref,
                    "safe_reason": case.safe_reason,
                    "blocked_at": case.blocked_at,
                }
                for case in result.cases
            ],
        }
        if result.next_cursor is not None:
            document["next_cursor"] = result.next_cursor
        return document
    return {
        "schema_version": PROVIDER_FENCE_RECOVERY_SCHEMA_VERSION,
        "mode": "requeue",
        "recovery_ref": result.recovery_ref,
        "outcome": result.outcome.value,
    }
