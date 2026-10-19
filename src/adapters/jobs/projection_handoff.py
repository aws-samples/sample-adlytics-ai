# SPDX-License-Identifier: MIT-0
"""Private CloudFormation provider for the §2.5 Stage-1 projection-handoff authority.

One Lambda serves the three closed in-stack custom resources
(``ProjectionHandoffPrepare``, ``ProjectionHandoffCommit``,
``ProjectionHandoffDeletionGuard``) against the retained publications table at
``partition_key=PROJECTION_HANDOFF#<environment>``. Every ``STATE`` write is
conditional — creates assert absence, replacements assert the exact prior row —
and audits are created only by conditional ``UpdateItem`` that an occupied key
must match byte-for-byte. The provider always delivers exactly one
CloudFormation response, success or failure, by HTTP ``PUT`` to ``ResponseURL``.

This component is span-free by design (write-discipline §10): structured logs
and metrics flow through the shared reporting seam only.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import urlparse

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from adapters.config import projection_handoff_provider_settings
from adapters.outbound.cloudformation_response import HttpsCustomResourceResponder
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from domain.operations.projection_handoff import (
    STATE_RECORD_KEY,
    ActiveRow,
    AuditPhase,
    BuildActiveRowRequest,
    BuildDecommissionedRowRequest,
    BuildDecommissioningRowRequest,
    BuildPreparedRowRequest,
    BuildRollingBackRowRequest,
    CallbackEnvelope,
    CommitDecisionRequest,
    CommitProperties,
    CommitReplayDecision,
    CommittedAuditRequest,
    DecommissionedRow,
    DecommissioningRow,
    DecommissionTokenRequest,
    ParseCallbackRequest,
    ParsePhysicalIdRequest,
    PhysicalId,
    PrepareDecisionRequest,
    PreparedRow,
    PrepareMode,
    PrepareProperties,
    PrepareReplayDecision,
    ProjectionHandoffError,
    ResourceKind,
    RollbackOwner,
    RollingBackRow,
    RowValue,
    StateRow,
    audit_record_key,
    build_active_row,
    build_decommissioned_row,
    build_decommissioning_row,
    build_prepared_row,
    build_rolling_back_row,
    committed_audit_payload,
    decide_commit,
    decide_prepare,
    decommission_token,
    decommissioned_audit_payload,
    decommissioning_audit_payload,
    failed_create_physical_id,
    failed_create_request_id,
    parse_callback,
    parse_physical_id,
    parse_snapshot,
    parse_state_row,
    partition_key,
    physical_id_text,
    prepared_audit_payload,
    prepared_row_from_audit,
    rollback_facts_from_audit,
    rollback_facts_from_row,
    rolled_back_audit_payload,
    rolling_back_audit_payload,
    stack_identity_hash,
    state_row_map,
)
from domain.shared_kernel.errors import (
    DependencyError,
    ErrorCode,
    InvariantError,
    ValidationError,
)
from ports.outbound.custom_resource_response import (
    CustomResourceResponderPort,
    CustomResourceResponse,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

__all__ = [
    "OPERATION",
    "CallbackResult",
    "HandoffStore",
    "ProviderRuntime",
    "handler",
    "process",
    "run",
]

_LOGGER = Logger(service="adlytics-projection-handoff")
OPERATION = "projection_handoff.callback"
_COMPONENT = "projection_handoff"

HANDOFF_INVALID: Final = ErrorCode("HANDOFF", 1)
HANDOFF_CONFLICT: Final = ErrorCode("HANDOFF", 2)
HANDOFF_AUDIT_MISMATCH: Final = ErrorCode("HANDOFF", 3)
HANDOFF_INCONSISTENT: Final = ErrorCode("HANDOFF", 4)
HANDOFF_DEPENDENCY: Final = ErrorCode("HANDOFF", 5)
HANDOFF_UNANSWERABLE: Final = ErrorCode("HANDOFF", 6)

_CONDITIONAL: Final = "ConditionalCheckFailedException"
_AUDIT_PAYLOAD_ATTRIBUTE: Final = "payload"


def _now_instant() -> str:
    """Return the 20-digit UTC epoch-microsecond instant captured once before a write."""
    return f"{time.time_ns() // 1_000:020d}"


def _attribute_value(value: RowValue) -> AttributeValueTypeDef:
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    return {"S": value}


def _plain_value(value: AttributeValueTypeDef) -> RowValue:
    if "BOOL" in value:
        return value["BOOL"]
    if "N" in value:
        return int(value["N"])
    if "S" in value:
        return value["S"]
    raise InvariantError(
        HANDOFF_INCONSISTENT, "retained handoff attribute is outside the closed value forms"
    )


class HandoffStore:
    """Own the ``PROJECTION_HANDOFF#<environment>`` key space in the retained table.

    Item shapes: the single ``record_key=STATE`` row in one of the five closed
    disposition forms, and immutable ``record_key=AUDIT#<token>#<phase>`` records
    carrying exactly one ``payload`` attribute of canonical bytes.
    """

    def __init__(self, table: str, environment: str, client: DynamoDBClient) -> None:
        if not table.strip():
            raise ValueError("projection handoff store requires the retained table name")
        self._table = table
        self._partition = partition_key(environment)
        self._client = client

    def _key(self, record_key: str) -> dict[str, AttributeValueTypeDef]:
        return {"partition_key": {"S": self._partition}, "record_key": {"S": record_key}}

    def read_state(self) -> dict[str, RowValue] | None:
        """Strongly read the single ``STATE`` row as plain closed-form values."""
        response = self._call(
            lambda: self._client.get_item(
                TableName=self._table, Key=self._key(STATE_RECORD_KEY), ConsistentRead=True
            )
        )
        item = response.get("Item")
        if item is None:
            return None
        attributes = cast("dict[str, AttributeValueTypeDef]", item)
        return {
            name: _plain_value(value)
            for name, value in attributes.items()
            if name not in {"partition_key", "record_key"}
        }

    def read_audit(self, token: str, phase: AuditPhase) -> bytes | None:
        """Strongly read one immutable audit payload's exact bytes."""
        response = self._call(
            lambda: self._client.get_item(
                TableName=self._table,
                Key=self._key(audit_record_key(token, phase)),
                ConsistentRead=True,
            )
        )
        item = response.get("Item")
        if item is None:
            return None
        attributes = cast("dict[str, AttributeValueTypeDef]", item)
        payload = attributes.get(_AUDIT_PAYLOAD_ATTRIBUTE, {})
        text = payload.get("S")
        if not isinstance(text, str):
            raise InvariantError(HANDOFF_INCONSISTENT, "retained audit record has no payload")
        return text.encode("utf-8")

    def write_state(
        self, prior: Mapping[str, RowValue] | None, target: Mapping[str, RowValue]
    ) -> None:
        """Replace the ``STATE`` row conditioning on absence or the exact prior form."""
        names: dict[str, str] = {}
        values: dict[str, AttributeValueTypeDef] = {}
        assignments: list[str] = []
        for index, name in enumerate(sorted(target)):
            names[f"#s{index}"] = name
            values[f":s{index}"] = _attribute_value(target[name])
            assignments.append(f"#s{index} = :s{index}")
        update = "SET " + ", ".join(assignments)
        removed = sorted(frozenset(prior or {}) - frozenset(target))
        for index, name in enumerate(removed):
            names[f"#r{index}"] = name
        if removed:
            update += " REMOVE " + ", ".join(f"#r{index}" for index in range(len(removed)))
        if prior is None:
            condition = "attribute_not_exists(partition_key)"
        else:
            comparisons: list[str] = []
            for index, name in enumerate(sorted(prior)):
                names[f"#c{index}"] = name
                values[f":c{index}"] = _attribute_value(prior[name])
                comparisons.append(f"#c{index} = :c{index}")
            condition = " AND ".join(comparisons)
        self._mutate(
            lambda: self._client.update_item(
                TableName=self._table,
                Key=self._key(STATE_RECORD_KEY),
                UpdateExpression=update,
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            ),
            "retained handoff STATE row differs from the conditioned prior form",
        )

    def delete_state(self, prior: Mapping[str, RowValue]) -> None:
        """First-bootstrap rollback alone: conditionally delete the exact token-bound row."""
        if prior.get("mode") != PrepareMode.BOOTSTRAP.value or prior.get("prior_snapshot") != (
            '"ABSENT"'
        ):
            raise InvariantError(
                HANDOFF_INCONSISTENT,
                "DeleteItem requires the complete fromDisposition=ABSENT bootstrap snapshot",
            )
        names: dict[str, str] = {}
        values: dict[str, AttributeValueTypeDef] = {}
        comparisons: list[str] = []
        for index, name in enumerate(sorted(prior)):
            names[f"#c{index}"] = name
            values[f":c{index}"] = _attribute_value(prior[name])
            comparisons.append(f"#c{index} = :c{index}")
        self._mutate(
            lambda: self._client.delete_item(
                TableName=self._table,
                Key=self._key(STATE_RECORD_KEY),
                ConditionExpression=" AND ".join(comparisons),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            ),
            "retained handoff STATE row differs from the token-bound bootstrap rollback",
        )

    def ensure_audit(self, request: EnsureAuditRequest) -> None:
        """Create the absent audit by conditional ``UpdateItem``; occupied must match bytes."""
        payload_text = request.payload.decode("utf-8")
        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._key(audit_record_key(request.token, request.phase)),
                UpdateExpression="SET #p = :p",
                ConditionExpression="attribute_not_exists(partition_key) OR #p = :p",
                ExpressionAttributeNames={"#p": _AUDIT_PAYLOAD_ATTRIBUTE},
                ExpressionAttributeValues={":p": {"S": payload_text}},
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == _CONDITIONAL:
                raise InvariantError(
                    HANDOFF_AUDIT_MISMATCH,
                    "occupied audit record differs from the required canonical bytes",
                    context={"record_kind": request.phase.value},
                ) from error
            raise DependencyError(
                HANDOFF_DEPENDENCY, "audit record write failed", context={}
            ) from error

    def _mutate(self, call: Callable[[], object], conflict: str) -> None:
        try:
            call()
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == _CONDITIONAL:
                raise InvariantError(HANDOFF_CONFLICT, conflict) from error
            raise DependencyError(
                HANDOFF_DEPENDENCY, "retained handoff state write failed"
            ) from error

    def _call(self, call: Callable[[], Mapping[str, object]]) -> Mapping[str, object]:
        try:
            return call()
        except ClientError as error:
            raise DependencyError(
                HANDOFF_DEPENDENCY, "retained handoff state read failed"
            ) from error


@dataclass(frozen=True, slots=True)
class EnsureAuditRequest:
    """One immutable audit record and the exact canonical bytes it must hold."""

    token: str
    phase: AuditPhase
    payload: bytes


@dataclass(frozen=True, slots=True)
class ProviderRuntime:
    """The provider's injectable boundaries: identity, retained store, and clock."""

    environment: str
    store: HandoffStore
    clock: Callable[[], str]


@dataclass(frozen=True, slots=True)
class CallbackResult:
    """One successful callback outcome; Delete responses carry no ``Data``."""

    physical_resource_id: str
    data: Mapping[str, object] | None


def _read_row(runtime: ProviderRuntime) -> tuple[dict[str, RowValue] | None, StateRow | None]:
    raw = runtime.store.read_state()
    if raw is None:
        return None, None
    try:
        return raw, parse_state_row(raw)
    except ProjectionHandoffError as error:
        raise InvariantError(
            HANDOFF_INCONSISTENT, "retained projection-handoff STATE row is malformed"
        ) from error


class _DecidedCreateError(Exception):
    """A Create failed after deciding its token-specific physical ID; it carries that ID.

    The rollback Delete of a failed Create receives whatever physical ID the failure named. Naming
    the decided ID lets the ordinary token-bound Delete restore a row the Create had already
    written (the Harness provider's ``_ProvisionFailedError`` pattern).
    """

    def __init__(self, physical_resource_id: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.physical_resource_id = physical_resource_id
        self.cause = cause


@contextmanager
def _create_failure_names(envelope: CallbackEnvelope, physical_resource_id: str) -> Generator[None]:
    """Within a decided Create, re-raise any failure carrying the decided physical ID."""
    try:
        yield
    except Exception as error:
        if envelope.request_type != "Create":
            raise
        cause = (
            ValidationError(HANDOFF_INVALID, str(error))
            if isinstance(error, ProjectionHandoffError)
            else error
        )
        raise _DecidedCreateError(physical_resource_id, cause) from error


def _prepare_apply(envelope: CallbackEnvelope, runtime: ProviderRuntime) -> CallbackResult:
    """Prepare Create/Update: strongly read, select exactly one mode, CAS, then audit."""
    raw, row = _read_row(runtime)
    decision = decide_prepare(PrepareDecisionRequest(envelope, row))
    if isinstance(decision, PrepareReplayDecision):
        return CallbackResult(decision.physical_resource_id, decision.data.document())
    properties = cast("PrepareProperties", envelope.properties)
    # A failed write may still have landed (a timeout), so the decided ID is named from here on;
    # the token-bound Delete restores a written row and is a stale no-op otherwise.
    with _create_failure_names(envelope, decision.physical_resource_id):
        if decision.already_prepared:
            prepared = cast("PreparedRow", row)
        else:
            prepared = build_prepared_row(
                BuildPreparedRowRequest(decision, properties, runtime.clock())
            )
            runtime.store.write_state(raw, state_row_map(prepared))
        runtime.store.ensure_audit(
            EnsureAuditRequest(
                decision.token,
                AuditPhase.PREPARED,
                prepared_audit_payload(runtime.environment, prepared),
            )
        )
    return CallbackResult(decision.physical_resource_id, decision.data.document())


def _retained_prepared(runtime: ProviderRuntime, active: ActiveRow) -> PreparedRow:
    prepared_payload = runtime.store.read_audit(active.last_transition_id, AuditPhase.PREPARED)
    if prepared_payload is None:
        raise InvariantError(
            HANDOFF_INCONSISTENT, "committed transition has no retained PREPARED audit"
        )
    return prepared_row_from_audit(prepared_payload)


def _commit_apply(envelope: CallbackEnvelope, runtime: ProviderRuntime) -> CallbackResult:
    """Commit Create/Update: consume only the prepared ``Fn::GetAtt`` propagation."""
    raw, row = _read_row(runtime)
    decision = decide_commit(CommitDecisionRequest(envelope, row))
    if isinstance(decision, CommitReplayDecision):
        return CallbackResult(decision.physical_resource_id, decision.data.document())
    if decision.already_committed:
        active = cast("ActiveRow", row)
        with _create_failure_names(envelope, decision.physical_resource_id):
            prepared = _retained_prepared(runtime, active)
    else:
        prepared = decision.prepared
        active = build_active_row(
            BuildActiveRowRequest(prepared, decision.state, decision.revision, runtime.clock())
        )
        # Unlike prepare, a failed commit write keeps the undecided marker: the commit-ID Delete
        # treats its own token still PREPARED as an inconsistency, and the marker's Delete
        # handles both a landed and an unlanded write (``_failed_create_delete``).
        runtime.store.write_state(raw, state_row_map(active))
    # ACTIVE now carries the token, so the commit-ID Delete owns its rollback.
    with _create_failure_names(envelope, decision.physical_resource_id):
        runtime.store.ensure_audit(
            EnsureAuditRequest(
                prepared.token,
                AuditPhase.COMMITTED,
                committed_audit_payload(
                    CommittedAuditRequest(runtime.environment, prepared, active)
                ),
            )
        )
    return CallbackResult(decision.physical_resource_id, decision.data.document())


def _establish_rolling_back(
    context: _RollbackContext, prepared: PreparedRow, owner: RollbackOwner
) -> RollingBackRow:
    rolling = build_rolling_back_row(
        BuildRollingBackRowRequest(prepared, owner, context.runtime.clock())
    )
    context.runtime.store.write_state(context.raw, state_row_map(rolling))
    _ensure_rolling_back_audit(context.runtime, rolling)
    return rolling


def _ensure_rolling_back_audit(runtime: ProviderRuntime, rolling: RollingBackRow) -> None:
    runtime.store.ensure_audit(
        EnsureAuditRequest(
            rolling.token,
            AuditPhase.ROLLING_BACK,
            rolling_back_audit_payload(runtime.environment, rolling),
        )
    )


@dataclass(frozen=True, slots=True)
class _RollbackContext:
    runtime: ProviderRuntime
    raw: dict[str, RowValue] | None
    token: str


def _commit_delete(envelope: CallbackEnvelope, runtime: ProviderRuntime) -> CallbackResult:
    """Commit Delete: token-bound rollback ownership, otherwise closed no-ops."""
    physical = parse_physical_id(
        ParsePhysicalIdRequest(
            envelope.physical_resource_id, runtime.environment, ResourceKind.COMMIT
        )
    )
    raw, row = _read_row(runtime)
    return _commit_rollback(envelope, _RollbackContext(runtime, raw, physical.token), row)


def _commit_rollback(
    envelope: CallbackEnvelope, context: _RollbackContext, row: StateRow | None
) -> CallbackResult:
    """Apply the token-bound commit Delete behavior to one strongly read ``STATE`` row."""
    runtime, raw, token = context.runtime, context.raw, context.token
    result = CallbackResult(envelope.physical_resource_id, None)
    if isinstance(row, ActiveRow):
        stale = row.stack_identity != envelope.stack_id or row.last_transition_id != token
        if stale:
            return result
        prepared_payload = runtime.store.read_audit(token, AuditPhase.PREPARED)
        if prepared_payload is None:
            raise InvariantError(
                HANDOFF_INCONSISTENT, "current-token rollback has no retained PREPARED audit"
            )
        prepared = prepared_row_from_audit(prepared_payload)
        if prepared.token != token or prepared.stack_identity != envelope.stack_id:
            raise InvariantError(
                HANDOFF_INCONSISTENT, "retained PREPARED audit contradicts the rollback token"
            )
        _establish_rolling_back(
            _RollbackContext(runtime, raw, token), prepared, RollbackOwner.COMMIT
        )
        return result
    if isinstance(row, RollingBackRow):
        if row.token == token:
            _ensure_rolling_back_audit(runtime, row)
        return result
    if isinstance(row, PreparedRow) and row.token == token:
        raise InvariantError(
            HANDOFF_INCONSISTENT, "commit Delete found its current token still PREPARED"
        )
    return result


def _restore_prior_snapshot(runtime: ProviderRuntime, rolling: RollingBackRow) -> None:
    """Restore the byte-exact prior snapshot, then ensure ``ROLLED_BACK``."""
    snapshot = parse_snapshot(rolling.prior_snapshot)
    if snapshot is None:
        runtime.store.delete_state(state_row_map(rolling))
    else:
        runtime.store.write_state(state_row_map(rolling), state_row_map(snapshot))
    runtime.store.ensure_audit(
        EnsureAuditRequest(
            rolling.token,
            AuditPhase.ROLLED_BACK,
            rolled_back_audit_payload(runtime.environment, rollback_facts_from_row(rolling)),
        )
    )


def _complete_decommission(context: _RollbackContext, row: DecommissioningRow) -> None:
    """Prepare Delete in decommission: condition snapshot and token, write terminal row."""
    runtime = context.runtime
    prior = parse_snapshot(row.prior_snapshot)
    expected = decommission_token(
        DecommissionTokenRequest(
            environment=runtime.environment,
            stack_identity=row.stack_identity,
            state=row.state,
            revision=row.revision,
            last_transition_id=row.last_transition_id,
        )
    )
    if not isinstance(prior, ActiveRow) or expected != row.decommission_token:
        raise InvariantError(
            HANDOFF_INCONSISTENT, "DECOMMISSIONING row contradicts its deterministic token"
        )
    terminal = build_decommissioned_row(BuildDecommissionedRowRequest(row, runtime.clock()))
    runtime.store.write_state(context.raw, state_row_map(terminal))
    runtime.store.ensure_audit(
        EnsureAuditRequest(
            terminal.last_transition_id,
            AuditPhase.DECOMMISSIONED,
            decommissioned_audit_payload(runtime.environment, terminal),
        )
    )


def _prepare_delete_fallback(context: _RollbackContext) -> None:
    """Distinguish a completed-restore retry from a stale zero-write no-op."""
    runtime, token = context.runtime, context.token
    if runtime.store.read_audit(token, AuditPhase.ROLLED_BACK) is not None:
        return
    rolling_payload = runtime.store.read_audit(token, AuditPhase.ROLLING_BACK)
    if rolling_payload is None:
        return
    facts = rollback_facts_from_audit(rolling_payload)
    runtime.store.ensure_audit(
        EnsureAuditRequest(
            token,
            AuditPhase.ROLLED_BACK,
            rolled_back_audit_payload(runtime.environment, facts),
        )
    )


def _prepare_delete(envelope: CallbackEnvelope, runtime: ProviderRuntime) -> CallbackResult:
    """Prepare Delete: establish ``ROLLING_BACK`` then restore, or the closed no-ops."""
    physical = parse_physical_id(
        ParsePhysicalIdRequest(
            envelope.physical_resource_id, runtime.environment, ResourceKind.PREPARE
        )
    )
    raw, row = _read_row(runtime)
    result = CallbackResult(envelope.physical_resource_id, None)
    token = physical.token
    context = _RollbackContext(runtime, raw, token)
    if isinstance(row, PreparedRow) and row.token == token:
        rolling = _establish_rolling_back(context, row, RollbackOwner.PREPARE)
        _restore_prior_snapshot(runtime, rolling)
        return result
    if isinstance(row, RollingBackRow) and row.token == token:
        _ensure_rolling_back_audit(runtime, row)
        _restore_prior_snapshot(runtime, row)
        return result
    if isinstance(row, DecommissioningRow):
        prior = parse_snapshot(row.prior_snapshot)
        owns = isinstance(prior, ActiveRow) and prior.last_transition_id == token
        if owns:
            if row.stack_identity != envelope.stack_id:
                raise InvariantError(
                    HANDOFF_INCONSISTENT, "decommission completion crossed stack identities"
                )
            _complete_decommission(context, row)
        return result
    if isinstance(row, DecommissionedRow):
        prior = parse_snapshot(row.prior_snapshot)
        if isinstance(prior, ActiveRow) and prior.last_transition_id == token:
            runtime.store.ensure_audit(
                EnsureAuditRequest(
                    row.last_transition_id,
                    AuditPhase.DECOMMISSIONED,
                    decommissioned_audit_payload(runtime.environment, row),
                )
            )
        return result
    current = (
        isinstance(row, ActiveRow)
        and row.last_transition_id == token
        and row.stack_identity == envelope.stack_id
    )
    if current:
        raise InvariantError(
            HANDOFF_INCONSISTENT, "prepare Delete found its current token still ACTIVE"
        )
    _prepare_delete_fallback(context)
    return result


def _guard_physical_id(envelope: CallbackEnvelope, environment: str) -> str:
    return physical_id_text(
        PhysicalId(environment, ResourceKind.DECOMMISSION, stack_identity_hash(envelope.stack_id))
    )


def _guard_apply(envelope: CallbackEnvelope, runtime: ProviderRuntime) -> CallbackResult:
    """Guard Create validates only; Update requires byte-identical properties."""
    expected = _guard_physical_id(envelope, runtime.environment)
    if envelope.request_type == "Create":
        return CallbackResult(expected, None)
    if envelope.physical_resource_id != expected:
        raise ValidationError(
            HANDOFF_INVALID, "deletion-guard physical resource ID is outside the closed form"
        )
    if envelope.raw_old_properties != envelope.raw_properties:
        raise ValidationError(
            HANDOFF_INVALID, "deletion-guard update requires byte-identical properties"
        )
    return CallbackResult(envelope.physical_resource_id, None)


def _guard_delete(envelope: CallbackEnvelope, runtime: ProviderRuntime) -> CallbackResult:
    """Guard Delete: CAS only the complete same-stack ``ACTIVE`` row to decommissioning."""
    expected = _guard_physical_id(envelope, runtime.environment)
    if envelope.physical_resource_id != expected:
        raise ValidationError(
            HANDOFF_INVALID, "deletion-guard physical resource ID is outside the closed form"
        )
    raw, row = _read_row(runtime)
    result = CallbackResult(envelope.physical_resource_id, None)
    if row is None:
        return result
    same_stack = row.stack_identity == envelope.stack_id
    if not same_stack:
        return result
    if isinstance(row, ActiveRow):
        token = decommission_token(
            DecommissionTokenRequest(
                environment=runtime.environment,
                stack_identity=row.stack_identity,
                state=row.state,
                revision=row.revision,
                last_transition_id=row.last_transition_id,
            )
        )
        target = build_decommissioning_row(
            BuildDecommissioningRowRequest(row, token, runtime.clock())
        )
        runtime.store.write_state(raw, state_row_map(target))
        runtime.store.ensure_audit(
            EnsureAuditRequest(
                token,
                AuditPhase.DECOMMISSIONING,
                decommissioning_audit_payload(runtime.environment, target),
            )
        )
        return result
    if isinstance(row, DecommissioningRow):
        expected_token = decommission_token(
            DecommissionTokenRequest(
                environment=runtime.environment,
                stack_identity=row.stack_identity,
                state=row.state,
                revision=row.revision,
                last_transition_id=row.last_transition_id,
            )
        )
        if expected_token != row.decommission_token:
            raise InvariantError(
                HANDOFF_INCONSISTENT, "DECOMMISSIONING row contradicts its deterministic token"
            )
        runtime.store.ensure_audit(
            EnsureAuditRequest(
                row.decommission_token,
                AuditPhase.DECOMMISSIONING,
                decommissioning_audit_payload(runtime.environment, row),
            )
        )
        return result
    if isinstance(row, DecommissionedRow):
        return result
    raise InvariantError(
        HANDOFF_INCONSISTENT, "deletion guard found the same-stack row mid-transition"
    )


def _failed_create_delete(envelope: CallbackEnvelope, runtime: ProviderRuntime) -> CallbackResult:
    """Delete of an undecided failed Create's marker: undo only what that Create can have left.

    CloudFormation sends this Delete when it rolls back the failed Create. It never sends an
    Update for a resource whose Create failed, so the marker is accepted on Delete alone. Every
    branch keeps the documented delete order: guard first, commit next, prepare last.
    """
    result = CallbackResult(envelope.physical_resource_id, None)
    if envelope.resource_kind is ResourceKind.DECOMMISSION:
        # Guard Create only validates, so a failed one wrote nothing.
        return result
    raw, row = _read_row(runtime)
    if envelope.resource_kind is ResourceKind.COMMIT:
        # The commit's properties carry the prepared token through ``Fn::GetAtt``.
        token = cast("CommitProperties", envelope.properties).transition_token
        if isinstance(row, PreparedRow) and row.token == token:
            # The ACTIVE write never landed; prepare's token-bound Delete, which runs after every
            # application resource is gone, restores the prior snapshot.
            return result
        # The ACTIVE write may have landed before the failure (a timed-out write).
        return _commit_rollback(envelope, _RollbackContext(runtime, raw, token), row)
    same_stack = isinstance(row, PreparedRow | RollingBackRow)
    same_stack = same_stack and cast("PreparedRow | RollingBackRow", row).stack_identity == (
        envelope.stack_id
    )
    if not same_stack:
        return result
    # A same-stack PREPARED or ROLLING_BACK row while this stack's only prepare failed to create
    # is an orphan of that Create (an earlier provider named no token for it). Prepare is deleted
    # last, so the restore runs after every application resource is gone, as for a token Delete.
    if isinstance(row, PreparedRow):
        rolling = _establish_rolling_back(
            _RollbackContext(runtime, raw, row.token), row, RollbackOwner.PREPARE
        )
    else:
        rolling = cast("RollingBackRow", row)
        _ensure_rolling_back_audit(runtime, rolling)
    _restore_prior_snapshot(runtime, rolling)
    return result


def _delete(envelope: CallbackEnvelope, runtime: ProviderRuntime) -> CallbackResult:
    if failed_create_request_id(envelope.physical_resource_id) is not None:
        return _failed_create_delete(envelope, runtime)
    if envelope.resource_kind is ResourceKind.PREPARE:
        return _prepare_delete(envelope, runtime)
    if envelope.resource_kind is ResourceKind.COMMIT:
        return _commit_delete(envelope, runtime)
    return _guard_delete(envelope, runtime)


def process(event: object, runtime: ProviderRuntime) -> CallbackResult:
    """Validate one callback and execute its closed §2.5 behavior."""
    try:
        envelope = parse_callback(ParseCallbackRequest(event, runtime.environment))
        if envelope.request_type == "Delete":
            return _delete(envelope, runtime)
        if envelope.resource_kind is ResourceKind.PREPARE:
            return _prepare_apply(envelope, runtime)
        if envelope.resource_kind is ResourceKind.COMMIT:
            return _commit_apply(envelope, runtime)
        return _guard_apply(envelope, runtime)
    except ProjectionHandoffError as error:
        raise ValidationError(HANDOFF_INVALID, str(error)) from error


# --- CloudFormation response delivery -------------------------------------------------------------


def _event_mapping(event: object) -> Mapping[str, object]:
    if isinstance(event, Mapping):
        return cast("Mapping[str, object]", event)
    return {}


def _defensive_text(event: Mapping[str, object], name: str) -> str:
    value = event.get(name)
    return value if isinstance(value, str) else ""


def _sanitized_reason(event: Mapping[str, object], error: Exception) -> str:
    """Name only the failure category and the caller's own RequestId; never values."""
    category = "unexpected provider failure"
    if isinstance(error, ValidationError):
        category = "callback validation failed"
    elif isinstance(error, InvariantError):
        category = "state authority conflict"
    elif isinstance(error, DependencyError):
        category = "dependency failure"
    request_id = _defensive_text(event, "RequestId") or "unknown"
    return f"Projection handoff {category}; see provider logs for RequestId {request_id}."


def _failure_physical_id(event: Mapping[str, object], error: Exception) -> str:
    if isinstance(error, _DecidedCreateError):
        return error.physical_resource_id
    incoming = _defensive_text(event, "PhysicalResourceId")
    if incoming.strip():
        return incoming
    return failed_create_physical_id(_defensive_text(event, "RequestId"))


@dataclass(frozen=True, slots=True)
class _ResponseParts:
    status: str
    reason: str
    physical_resource_id: str
    data: Mapping[str, object] | None


def _response_body(event: Mapping[str, object], parts: _ResponseParts) -> bytes:
    document: dict[str, object] = {
        "Status": parts.status,
        "Reason": parts.reason,
        "PhysicalResourceId": parts.physical_resource_id,
        "StackId": _defensive_text(event, "StackId"),
        "RequestId": _defensive_text(event, "RequestId"),
        "LogicalResourceId": _defensive_text(event, "LogicalResourceId"),
    }
    if parts.data is not None:
        document["Data"] = parts.data
    return json.dumps(document).encode("utf-8")


def _deliverer(responder: CustomResourceResponderPort) -> Callable[[str, bytes], None]:
    """Deliver through the shared HTTPS-only responder, keeping this provider's error code."""

    def deliver(url: str, body: bytes) -> None:
        try:
            responder.send(CustomResourceResponse(response_url=url, body=body))
        except DependencyError as error:
            refused = "status" in error.context
            raise DependencyError(
                HANDOFF_DEPENDENCY,
                "response delivery was refused" if refused else "response delivery failed",
                context=dict(error.context),
            ) from None

    return deliver


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One raw callback with the runtime and response delivery boundary to use."""

    event: object
    runtime: ProviderRuntime
    deliver: Callable[[str, bytes], None]


def run(request: RunRequest) -> str:
    """Process one callback and always deliver exactly one response."""
    report = Report(operation=OPERATION, correlation_id="", logger=_LOGGER)
    try:
        event = _event_mapping(request.event)
    except Exception as error:
        report_error(error, report)
        raise
    try:
        result = process(request.event, request.runtime)
        parts = _ResponseParts("SUCCESS", "", result.physical_resource_id, result.data)
        outcome = "completed"
    except Exception as error:
        cause = error.cause if isinstance(error, _DecidedCreateError) else error
        report_error(cause, report)
        parts = _ResponseParts(
            "FAILED", _sanitized_reason(event, cause), _failure_physical_id(event, error), None
        )
        outcome = "failed"
    try:
        url = _defensive_text(event, "ResponseURL")
        if urlparse(url).scheme != "https":
            raise InvariantError(
                HANDOFF_UNANSWERABLE, "callback carries no https ResponseURL to answer"
            )
        request.deliver(url, _response_body(event, parts))
    except Exception as error:
        report_error(error, report)
        raise
    report_operation(
        OperationLog(
            operation=OPERATION,
            component=_COMPONENT,
            phase="terminal",
            outcome=outcome,
            severity=LogSeverity.INFO if outcome == "completed" else LogSeverity.WARNING,
            details={
                "request_id": _defensive_text(event, "RequestId"),
                "record_kind": _defensive_text(event, "ResourceType"),
            },
        ),
        _LOGGER,
    )
    return outcome


@lru_cache(maxsize=1)
def _runtime() -> ProviderRuntime:
    settings = projection_handoff_provider_settings()
    store = HandoffStore(
        settings.publications_table, settings.environment, boto3.client("dynamodb")
    )
    return ProviderRuntime(settings.environment, store, _now_instant)


@flushes_metrics
@_LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, str]:
    """Serve one closed projection-handoff callback and answer CloudFormation."""
    outcome = run(
        RunRequest(
            event=event,
            runtime=_runtime(),
            deliver=_deliverer(HttpsCustomResourceResponder()),
        )
    )
    return {"outcome": outcome}
