# SPDX-License-Identifier: MIT-0
"""``ProviderFenceRecoveryFunction``: list canonical provider blocks and requeue one (§3, §5).

The sole provider-block discovery and requeue broker. ``list`` queries only the ``blocked``
partition of ``table-attempt-reconcile`` and resolves every returned base ``FENCE`` to its exact
protected case server-side; the result carries only opaque references, safe reasons, block instants,
and an opaque cursor. ``requeue`` strongly reads the protected case and its canonical blocked
``FENCE``/``ATTEMPT``/owner pair and passes that block output whole to the coordinator's private
requeue row; it repairs nothing, calls no provider, and clears no fence. Every failure is exactly
one member of the closed five-value vocabulary. Span-free by design (write discipline §10):
structured operation logs through the shared reporting seam only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger

from adapters.config import provider_fence_recovery_settings
from adapters.outbound.dynamodb.projection_coordinator import DynamoProjectionCoordinator
from adapters.outbound.dynamodb.stage1_discovery import DynamoStage1Discovery
from adapters.outbound.dynamodb.stage1_lifecycle_head import DynamoStage1LifecycleHeadStore
from adapters.outbound.dynamodb.stage1_writer_authority import Stage1WriterAuthorityReader
from adapters.outbound.dynamodb.table_attempt import DynamoTableAttemptStore
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from domain.operations.provider_fence_recovery import (
    BlockedCaseSummary,
    ListBlockedCasesInvocation,
    ListBlockedCasesResult,
    ProviderFenceRecoveryError,
    ProviderFenceRecoveryErrorCode,
    ProviderFenceRecoveryResult,
    RequeueBlockedCaseInvocation,
    RequeueBlockedCaseResult,
    RequeueOutcome,
    parse_invocation,
    result_document,
)
from domain.operations.table_attempt import (
    TABLE_ATTEMPT_PREFIX,
    ProtectedCaseState,
    ProtectedReconcileCase,
    reconciliation_valid,
)
from domain.shared_kernel.errors import DependencyError, DomainError, InvariantError
from ports.outbound.projection_coordinator import (
    RequeueBlockedTableAttemptReconcileOutcome,
    RequeueBlockedTableAttemptReconcileRequest,
)
from ports.outbound.stage1_discovery import BlockedFenceQuery

if TYPE_CHECKING:
    from collections.abc import Callable

    from domain.operations.table_attempt import FenceObservation
    from ports.outbound.stage1_discovery import Stage1DiscoveryPort
    from ports.outbound.table_attempt import TableAttemptStore

__all__ = ["ProviderFenceRecovery", "handler", "recover"]

_LOGGER = Logger(service="adlytics-provider-fence-recovery")
_OPERATION = "stage1_provider_fence.recovery"
_COMPONENT = "provider_fence_recovery"
_LEASES_TABLE_UNUSED = "unused-by-provider-fence-recovery"


def _failure(code: ProviderFenceRecoveryErrorCode) -> ProviderFenceRecoveryError:
    return ProviderFenceRecoveryError(code)


def _now_instant() -> str:
    return f"{int(datetime.now(UTC).timestamp() * 1_000_000):020d}"


class ProviderFenceRecovery:
    """The broker over its strongly consistent reads and the private requeue row."""

    def __init__(  # noqa: PLR0913, PLR0917 -- four injected collaborators plus a clock
        self,
        discovery: Stage1DiscoveryPort,
        reader: Stage1WriterAuthorityReader,
        attempts: TableAttemptStore,
        coordinator: DynamoProjectionCoordinator,
        now: Callable[[], str] = _now_instant,
    ) -> None:
        self._discovery = discovery
        self._reader = reader
        self._attempts = attempts
        self._coordinator = coordinator
        self._now = now

    def invoke(self, value: object) -> ProviderFenceRecoveryResult:
        """Parse one closed invocation and perform exactly its variant."""
        invocation = parse_invocation(value)
        if isinstance(invocation, ListBlockedCasesInvocation):
            return self._list(invocation)
        return self._requeue(invocation)

    # -- list --------------------------------------------------------------------------------------

    def _list(self, invocation: ListBlockedCasesInvocation) -> ListBlockedCasesResult:
        try:
            keys = self._discovery.blocked_fences(
                BlockedFenceQuery(invocation.limit, invocation.cursor)
            )
        except DependencyError as error:
            raise _failure(ProviderFenceRecoveryErrorCode.DEPENDENCY_FAILURE) from error
        summaries: list[BlockedCaseSummary] = []
        for key in keys:
            if not key.partition_key.startswith(TABLE_ATTEMPT_PREFIX) or key.record_key != "FENCE":
                raise _failure(ProviderFenceRecoveryErrorCode.STALE_CASE)
            table_id = key.partition_key[len(TABLE_ATTEMPT_PREFIX) :]
            fence = self._read_fence(table_id)
            rec = fence.reconciliation
            if rec.reconcile_status != "blocked" or not reconciliation_valid(fence):
                # The index is eventually consistent: a requeue may have raced this page.
                continue
            ref = rec.reconcile_recovery_ref
            if not isinstance(ref, str):  # pragma: no cover -- excluded by reconciliation_valid
                raise _failure(ProviderFenceRecoveryErrorCode.STALE_CASE)
            case = self._read_case(ref)
            if case is None or not _case_matches_fence(case, fence, table_id):
                raise _failure(ProviderFenceRecoveryErrorCode.STALE_CASE)
            summaries.append(
                BlockedCaseSummary(
                    recovery_ref=case.recovery_ref,
                    safe_reason=case.reason.value,
                    blocked_at=case.blocked_at,
                )
            )
        next_cursor = (
            keys[-1].sort_value[len("BLOCK#") :] if len(keys) == invocation.limit else None
        )
        return ListBlockedCasesResult(tuple(summaries), next_cursor)

    # -- requeue -----------------------------------------------------------------------------------

    def _requeue(self, invocation: RequeueBlockedCaseInvocation) -> RequeueBlockedCaseResult:
        case = self._read_case(invocation.recovery_ref)
        if case is None:
            raise _failure(ProviderFenceRecoveryErrorCode.CASE_NOT_FOUND)
        if case.state is ProtectedCaseState.REQUEUED:
            # Only the exact prior consumption replays as already_requeued; a different
            # operation against a consumed case is stale, never a second requeue or a repair.
            same_operation = (
                case.requeue_operation_id == invocation.operation_id
                and case.resolution_reason == invocation.resolution_reason
            )
            if not same_operation:
                raise _failure(ProviderFenceRecoveryErrorCode.STALE_CASE)
            return RequeueBlockedCaseResult(case.recovery_ref, RequeueOutcome.ALREADY_REQUEUED)
        if case.state is not ProtectedCaseState.BLOCKED:  # pragma: no cover -- closed enum
            raise _failure(ProviderFenceRecoveryErrorCode.STALE_CASE)
        fence = self._read_fence(case.physical_table_id)
        attempt = self._read(lambda: self._reader.attempt(case.physical_table_id, case.attempt_id))
        if attempt is None:
            raise _failure(ProviderFenceRecoveryErrorCode.STALE_CASE)
        owner = self._read(lambda: self._reader.attempt_owner(attempt))
        if owner is None:
            raise _failure(ProviderFenceRecoveryErrorCode.STALE_CASE)
        try:
            request = RequeueBlockedTableAttemptReconcileRequest(
                physical_table_id=case.physical_table_id,
                case=case,
                fence=fence,
                attempt=attempt,
                owner=owner,
                operation_id=invocation.operation_id,
                resolution_reason=invocation.resolution_reason,
                requeue_instant=self._now(),
            )
        except ValueError as error:
            raise _failure(ProviderFenceRecoveryErrorCode.STALE_CASE) from error
        try:
            result = self._coordinator.requeue_blocked_table_attempt_reconcile(request)
        except DomainError as error:
            raise _failure(ProviderFenceRecoveryErrorCode.CONFLICT) from error
        except DependencyError as error:
            raise _failure(ProviderFenceRecoveryErrorCode.DEPENDENCY_FAILURE) from error
        outcome = (
            RequeueOutcome.REQUEUED
            if result.outcome is RequeueBlockedTableAttemptReconcileOutcome.REQUEUED
            else RequeueOutcome.ALREADY_REQUEUED
        )
        return RequeueBlockedCaseResult(case.recovery_ref, outcome)

    # -- strongly consistent reads -----------------------------------------------------------------

    def _read[T](self, read: Callable[[], T]) -> T:
        try:
            return read()
        except DependencyError as error:
            raise _failure(ProviderFenceRecoveryErrorCode.DEPENDENCY_FAILURE) from error
        except InvariantError as error:
            raise _failure(ProviderFenceRecoveryErrorCode.STALE_CASE) from error

    def _read_fence(self, table_id: str) -> FenceObservation:
        fence = self._read(lambda: self._reader.fence(table_id))
        if fence is None:
            raise _failure(ProviderFenceRecoveryErrorCode.STALE_CASE)
        return fence

    def _read_case(self, recovery_ref: str) -> ProtectedReconcileCase | None:
        return self._read(lambda: self._attempts.read_protected_case(recovery_ref))


def _case_matches_fence(
    case: ProtectedReconcileCase, fence: FenceObservation, table_id: str
) -> bool:
    rec = fence.reconciliation
    return (
        case.state is ProtectedCaseState.BLOCKED
        and case.physical_table_id == table_id
        and case.attempt_id == fence.attempt_id
        and rec.reconcile_recovery_ref == case.recovery_ref
        and rec.reconcile_block_id == case.reconcile_block_id
        and rec.reconcile_block_reason == case.reason.value
        and rec.reconcile_blocked_at == case.blocked_at
        and rec.reconcile_observation_digest == case.observation_digest
    )


def recover(value: object, broker: ProviderFenceRecovery) -> dict[str, object]:
    """Parse, invoke, and shape without accepting or returning internal selectors."""
    return result_document(broker.invoke(value))


@lru_cache(maxsize=1)
def _broker() -> ProviderFenceRecovery:
    settings = provider_fence_recovery_settings()
    client = boto3.client("dynamodb")
    publications = settings.publications_table
    return ProviderFenceRecovery(
        discovery=DynamoStage1Discovery(publications, client),
        reader=Stage1WriterAuthorityReader(publications, _LEASES_TABLE_UNUSED, client),
        attempts=DynamoTableAttemptStore(publications, client),
        coordinator=DynamoProjectionCoordinator(
            publications,
            _LEASES_TABLE_UNUSED,
            DynamoStage1LifecycleHeadStore(publications, client),
            client,
        ),
    )


def _reportable_error(error: BaseException) -> BaseException:
    """Keep the closed recovery error while alarming on its dependency cause."""
    if (
        isinstance(error, ProviderFenceRecoveryError)
        and error.recovery_code is ProviderFenceRecoveryErrorCode.DEPENDENCY_FAILURE
        and isinstance(error.__cause__, DependencyError)
    ):
        return error.__cause__
    return error


@flushes_metrics
@_LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, object]:
    """Perform one separately authorized list or requeue."""
    try:
        result = recover(event, _broker())
    except Exception as error:
        report_error(
            _reportable_error(error),
            Report(operation=_OPERATION, correlation_id="", logger=_LOGGER),
        )
        raise
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase="terminal",
            outcome=str(result.get("outcome", result["mode"])),
            severity=LogSeverity.INFO,
            details={"mode": result["mode"]},
        ),
        _LOGGER,
    )
    return result
