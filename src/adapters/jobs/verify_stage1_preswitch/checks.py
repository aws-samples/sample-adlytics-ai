# SPDX-License-Identifier: MIT-0
"""Pure verification over one complete all-page read of the retained publications table.

Every predicate here consumes plain DynamoDB items already read to completion by the caller and
the owner decoders of ``adapters.outbound.dynamodb.table_attempt``; it never touches a client, so
each rule is testable from dict fixtures and the live run reduces to "read every page, then decide".
Recomputation is provenance (write discipline §2.5): a reserved-namespace row that fails exact
recomputation cannot have been produced by an amended builder, and there is no repair mode.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast

from adapters.outbound.dynamodb.table_attempt import (
    attempt_observation_from_item,
    fence_observation_from_item,
    inline_run_from_item,
    protected_case_from_item,
)
from domain.operations.table_attempt import (
    ATTEMPT_RECORD_PREFIX,
    FENCE_RECORD_KEY,
    PROTECTED_CASE_PREFIX,
    PROTECTED_CASE_RECORD_KEY,
    RECONCILIATION_FIELD_NAMES,
    RUN_RECORD_PREFIX,
    TABLE_ATTEMPT_PREFIX,
    USAGE_RECORD_PREFIX,
    ClearFence,
    CoreValidityRequest,
    InlineRunOwnerObservation,
    InlineRunState,
    PlanUnitOwnerObservation,
    ReconcileBlockIdentityRequest,
    RunIdentityRequest,
    TableAttemptError,
    WriterKind,
    core_valid,
    parse_attempt,
    parse_fence,
    recompute_attempt_identity,
    reconcile_block_identity,
    reconcile_recovery_reference,
    reconciliation_valid,
    run_identity,
)
from domain.operations.write_plan import (
    META_RECORD_KEY,
    READY_STATUS_VALUE,
    UNIT_RECORD_PREFIX,
    UNIT_SUPERSESSION_EVIDENCE_FIELD,
    WRITE_PLAN_PREFIX,
    IntentState,
    PlanStatus,
    PlanUnitStatus,
    is_canonical_digest,
)

if TYPE_CHECKING:
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

    Item = dict[str, AttributeValueTypeDef]

__all__ = [
    "STAGE1_INDEXES",
    "Check",
    "ExecutionEvidence",
    "IndexExpectation",
    "canonical_namespace_checks",
    "edge_checks",
    "execution_evidence",
    "gsi_schema_check",
    "ready_field_check",
    "stray_reconciliation_check",
]

_INTENT_SEGMENT: Final = "#INTENT#"
_READY_ORDER: Final = re.compile(r"\A[0-9]{20}#([0-9a-f]{64})\Z")
_PLAN_META_TERMINAL: Final = frozenset(
    {PlanStatus.COMPLETED.value, PlanStatus.SUPERSEDED.value, PlanStatus.ADMISSION_FAILED.value}
)
_UNIT_TERMINAL: Final = frozenset({PlanUnitStatus.ATTESTED.value, PlanUnitStatus.FAILED.value})
_RUN_TERMINAL: Final = frozenset({InlineRunState.TERMINAL.value})


@dataclass(frozen=True, slots=True)
class IndexExpectation:
    """The pinned key schema and projection of one Stage-1 sparse GSI."""

    name: str
    partition_key: str
    sort_key: str
    projection: str = "KEYS_ONLY"


STAGE1_INDEXES: Final = (
    IndexExpectation("table-attempt-reconcile", "reconcile_status", "reconcile_after_or_block"),
    IndexExpectation("stage1-plan-intent-ready", "ready_status", "ready_order"),
    IndexExpectation("stage1-outbox-pending", "outbox_status", "outbox_order"),
)


@dataclass(frozen=True, slots=True)
class Check:
    """One per-check verdict: its name, pass/fail, and every violating key."""

    name: str
    passed: bool
    violating_keys: tuple[str, ...] = ()
    detail: Mapping[str, object] = field(default_factory=lambda: cast("Mapping[str, object]", {}))

    def document(self) -> dict[str, object]:
        """Render the report member."""
        return {
            "name": self.name,
            "passed": self.passed,
            "violating_keys": list(self.violating_keys),
            "detail": dict(self.detail),
        }


def _text(item: Item, name: str) -> str | None:
    value = item.get(name)
    if value is None:
        return None
    text = value.get("S")
    return text if isinstance(text, str) else None


def _key(item: Item) -> str:
    return f"{_text(item, 'partition_key') or '?'}/{_text(item, 'record_key') or '?'}"


def _failed(name: str, keys: Iterable[str], **detail: object) -> Check:
    ordered = tuple(sorted(set(keys)))
    return Check(name, not ordered, ordered, detail)


# --- DescribeTable: GSI proofs --------------------------------------------------------------------


def gsi_schema_check(table: Mapping[str, object]) -> Check:
    """Prove each Stage-1 GSI ``ACTIVE`` with the exact pinned key schema and projection."""
    raw_indexes = table.get("GlobalSecondaryIndexes")
    indexes = (
        {
            str(cast("Mapping[str, object]", entry).get("IndexName")): cast(
                "Mapping[str, object]", entry
            )
            for entry in cast("Sequence[object]", raw_indexes)
        }
        if isinstance(raw_indexes, list)
        else {}
    )
    violations: list[str] = []
    for expected in STAGE1_INDEXES:
        entry = indexes.get(expected.name)
        if entry is None:
            violations.append(f"{expected.name}: missing")
            continue
        schema = {
            str(cast("Mapping[str, object]", member).get("KeyType")): str(
                cast("Mapping[str, object]", member).get("AttributeName")
            )
            for member in cast("Sequence[object]", entry.get("KeySchema", []))
        }
        projection = cast("Mapping[str, object]", entry.get("Projection", {}))
        if entry.get("IndexStatus") != "ACTIVE":
            violations.append(f"{expected.name}: status {entry.get('IndexStatus')}")
        if schema.get("HASH") != expected.partition_key or schema.get("RANGE") != expected.sort_key:
            violations.append(f"{expected.name}: key schema")
        if projection.get("ProjectionType") != expected.projection:
            violations.append(f"{expected.name}: projection")
    return _failed("gsi_schema", violations, indexes_checked=len(STAGE1_INDEXES))


# --- Ready fields ---------------------------------------------------------------------------------


def _is_intent_meta(item: Item) -> bool:
    partition = _text(item, "partition_key") or ""
    return (
        partition.startswith(WRITE_PLAN_PREFIX)
        and _INTENT_SEGMENT in partition
        and _text(item, "record_key") == META_RECORD_KEY
    )


def ready_field_check(items: Sequence[Item]) -> Check:
    """Only pending intent ``META`` rows carry both ready fields, in their exact forms."""
    violations: list[str] = []
    for item in items:
        status, order = _text(item, "ready_status"), _text(item, "ready_order")
        has_ready = "ready_status" in item or "ready_order" in item
        if not _is_intent_meta(item):
            if has_ready:
                violations.append(_key(item))
            continue
        pending = _text(item, "state") == IntentState.PENDING.value
        if not pending:
            if has_ready:
                violations.append(_key(item))
            continue
        intent_id = (_text(item, "partition_key") or "").rsplit(_INTENT_SEGMENT, 1)[-1]
        match = _READY_ORDER.match(order or "")
        if status != READY_STATUS_VALUE or match is None or match.group(1) != intent_id:
            violations.append(_key(item))
    return _failed("ready_fields", violations, rows=len(items))


# --- Reserved namespaces --------------------------------------------------------------------------


def _partitioned(items: Sequence[Item], prefix: str) -> dict[str, dict[str, Item]]:
    grouped: dict[str, dict[str, Item]] = {}
    for item in items:
        partition = _text(item, "partition_key") or ""
        if partition.startswith(prefix):
            grouped.setdefault(partition, {})[_text(item, "record_key") or ""] = item
    return grouped


def _run_owner(run_item: Item | None, run_id: str) -> InlineRunOwnerObservation | None:
    if run_item is None:
        return None

    def number(name: str) -> int | None:
        raw = run_item.get(name, {}).get("N")
        return int(raw) if isinstance(raw, str) and raw.lstrip("-").isdigit() else None

    return InlineRunOwnerObservation(
        run_partition_key=_text(run_item, "partition_key") or "",
        run_record_key=_text(run_item, "record_key") or "",
        run_id=run_id,
        state=_text(run_item, "state"),
        writer_execution_id=_text(run_item, "execution_id"),
        ordinal=number("next_ordinal"),
        current_attempt_id=_text(run_item, "current_attempt_id"),
        lease_holder=_text(run_item, "lease_holder"),
        fencing_token=number("fencing_token"),
        reservation_count=number("accepted_attempt_reservations"),
        reserved_slot_identity=_text(run_item, "reserved_slot_identity"),
    )


def _unit_owner(
    all_items: Mapping[str, Mapping[str, Item]], plan_key: str, table_id: str
) -> PlanUnitOwnerObservation | None:
    plan_rows = all_items.get(plan_key)
    if plan_rows is None:
        return None
    unit = plan_rows.get(f"{UNIT_RECORD_PREFIX}{table_id}")
    if unit is None:
        return None
    meta = plan_rows.get(META_RECORD_KEY)

    def number(source: Item | None, name: str) -> int | None:
        if source is None:
            return None
        raw = source.get(name, {}).get("N")
        return int(raw) if isinstance(raw, str) and raw.lstrip("-").isdigit() else None

    intent_admitted = False
    if meta is not None:
        digest, intent_id = _text(meta, "plan_digest"), _text(meta, "origin_intent_id")
        if digest is not None and intent_id is not None:
            intent_meta = all_items.get(f"{WRITE_PLAN_PREFIX}{digest}{_INTENT_SEGMENT}{intent_id}")
            if intent_meta is not None:
                state = _text(intent_meta.get(META_RECORD_KEY, {}), "state")
                intent_admitted = state == IntentState.ADMITTED.value
    return PlanUnitOwnerObservation(
        plan_partition_key=plan_key,
        unit_record_key=f"{UNIT_RECORD_PREFIX}{table_id}",
        plan_meta_exists=meta is not None,
        origin_intent_admitted=intent_admitted,
        unit_status=_text(unit, "status"),
        writer_execution_id=_text(unit, "writer_execution_id"),
        ordinal=number(unit, "ordinal"),
        current_attempt_id=_text(unit, "current_attempt_id"),
        lease_holder=_text(unit, "lease_holder"),
        fencing_token=number(unit, "fencing_token"),
        unit_reservation_count=number(unit, "unit_reservation_count"),
        plan_reservation_count=number(meta, "plan_reservation_count"),
        reserved_slot_identity=_text(unit, "reserved_slot_identity"),
    )


@dataclass(frozen=True, slots=True)
class _Namespace:
    """One ``TABLE_ATTEMPT#<table>`` partition and the whole-table view it is judged against."""

    table_id: str
    rows: Mapping[str, Item]
    everything: Mapping[str, Mapping[str, Item]]

    def key(self, record: str) -> str:
        return f"{TABLE_ATTEMPT_PREFIX}{self.table_id}/{record}"


def _fence_violations(namespace: _Namespace) -> list[str]:
    fence_item = namespace.rows.get(FENCE_RECORD_KEY)
    if fence_item is None:
        return []
    observation = fence_observation_from_item(fence_item)
    key = namespace.key(FENCE_RECORD_KEY)
    if observation.malformed_field_names or observation.unrecognized_field_names:
        return [key]
    try:
        fence = parse_fence(observation)
    except TableAttemptError:
        return [key]
    if isinstance(fence, ClearFence):
        stray = any(name in fence_item for name in RECONCILIATION_FIELD_NAMES)
        return [key] if stray else []
    attempt_item = namespace.rows.get(f"{ATTEMPT_RECORD_PREFIX}{fence.attempt_id}")
    attempt = None if attempt_item is None else attempt_observation_from_item(attempt_item)
    owner_run = owner_unit = None
    if attempt is not None:
        if attempt.inline_run_owner_ref is not None:
            run_item = namespace.rows.get(f"{RUN_RECORD_PREFIX}{attempt.inline_run_owner_ref}")
            owner_run = _run_owner(run_item, attempt.inline_run_owner_ref)
        elif attempt.plan_unit_owner_ref is not None:
            owner_unit = _unit_owner(
                namespace.everything, attempt.plan_unit_owner_ref, namespace.table_id
            )
    request = CoreValidityRequest(
        table_id=namespace.table_id,
        fence=observation,
        attempt=attempt,
        recompute_identity=recompute_attempt_identity,
        plan_unit_owner=owner_unit,
        inline_run_owner=owner_run,
    )
    if not core_valid(request) or not reconciliation_valid(observation):
        return [key]
    return []


def _attempt_violations(namespace: _Namespace) -> list[str]:
    violations: list[str] = []
    for record, item in namespace.rows.items():
        if not record.startswith(ATTEMPT_RECORD_PREFIX):
            continue
        observation = attempt_observation_from_item(item)
        if observation.malformed_field_names or observation.unrecognized_field_names:
            violations.append(namespace.key(record))
            continue
        try:
            attempt = parse_attempt(observation)
        except TableAttemptError:
            violations.append(namespace.key(record))
            continue
        core = attempt.core
        if record != f"{ATTEMPT_RECORD_PREFIX}{core.attempt_id}":
            violations.append(namespace.key(record))
            continue
        # Every attempt, terminal or not, must reproduce its three identities from its immutable
        # core: the paired nonterminal check recomputes the same bytes, so this also catches an
        # unpaired nonterminal attempt, which no amended builder ever leaves behind.
        identity = recompute_attempt_identity(core)
        if (identity.attempt_id, identity.client_request_token, identity.envelope_digest) != (
            core.attempt_id,
            core.client_request_token,
            core.envelope_digest,
        ):
            violations.append(namespace.key(record))
    return violations


def _run_violations(namespace: _Namespace) -> list[str]:
    violations: list[str] = []
    for record, item in namespace.rows.items():
        if not record.startswith(RUN_RECORD_PREFIX):
            continue
        try:
            run = inline_run_from_item(item)
        except TableAttemptError, ValueError:
            violations.append(namespace.key(record))
            continue
        expected = run_identity(
            RunIdentityRequest(
                writer_kind=run.writer_kind,
                operation_id=run.operation_id,
                execution_id=run.execution_id,
                physical_table_id=run.physical_table_id,
            )
        )
        if (
            run.run_id != expected
            or record != f"{RUN_RECORD_PREFIX}{run.run_id}"
            or run.physical_table_id != namespace.table_id
        ):
            violations.append(namespace.key(record))
    return violations


def _usage_violations(namespace: _Namespace) -> list[str]:
    violations: list[str] = []
    for record in namespace.rows:
        if not record.startswith(USAGE_RECORD_PREFIX):
            continue
        attempt_id = record.removeprefix(USAGE_RECORD_PREFIX)
        sibling = namespace.rows.get(f"{ATTEMPT_RECORD_PREFIX}{attempt_id}")
        settled = sibling is not None and _text(sibling, "athena_usage_state") == "settled"
        terminal = sibling is not None and _text(sibling, "terminal_at") is not None
        if not (settled and terminal):
            violations.append(namespace.key(record))
    return violations


def _protected_case_violations(cases: Mapping[str, Mapping[str, Item]]) -> list[str]:
    violations: list[str] = []
    for partition, rows in cases.items():
        recovery_ref = partition.removeprefix(PROTECTED_CASE_PREFIX)
        for record, item in rows.items():
            key = f"{partition}/{record}"
            if record != PROTECTED_CASE_RECORD_KEY:
                violations.append(key)
                continue
            try:
                case = protected_case_from_item(item, recovery_ref)
            except TableAttemptError, ValueError:
                violations.append(key)
                continue
            if case.observation_digest is None:
                violations.append(key)
                continue
            block_id = reconcile_block_identity(
                ReconcileBlockIdentityRequest(
                    binding_digest=case.binding_digest,
                    reason=case.reason,
                    observation_digest=case.observation_digest,
                    blocked_at=case.blocked_at,
                )
            )
            if block_id != case.reconcile_block_id or (
                reconcile_recovery_reference(block_id) != recovery_ref
            ):
                violations.append(key)
    return violations


def canonical_namespace_checks(items: Sequence[Item]) -> tuple[Check, ...]:
    """Recompute every ``TABLE_ATTEMPT#`` and ``PROVIDER_RECONCILE_BLOCK#`` row as builder bytes."""
    everything: dict[str, dict[str, Item]] = {}
    for item in items:
        partition = _text(item, "partition_key") or ""
        everything.setdefault(partition, {})[_text(item, "record_key") or ""] = item
    namespaces = [
        _Namespace(partition.removeprefix(TABLE_ATTEMPT_PREFIX), rows, everything)
        for partition, rows in _partitioned(items, TABLE_ATTEMPT_PREFIX).items()
    ]
    fences = [key for namespace in namespaces for key in _fence_violations(namespace)]
    attempts = [key for namespace in namespaces for key in _attempt_violations(namespace)]
    runs = [key for namespace in namespaces for key in _run_violations(namespace)]
    usage = [key for namespace in namespaces for key in _usage_violations(namespace)]
    cases = _partitioned(items, PROTECTED_CASE_PREFIX)
    unknown = [
        f"{TABLE_ATTEMPT_PREFIX}{namespace.table_id}/{record}"
        for namespace in namespaces
        for record in namespace.rows
        if record != FENCE_RECORD_KEY
        and not record.startswith((ATTEMPT_RECORD_PREFIX, RUN_RECORD_PREFIX, USAGE_RECORD_PREFIX))
    ]
    return (
        _failed("fence_canonical", fences, partitions=len(namespaces)),
        _failed("attempt_canonical", attempts, partitions=len(namespaces)),
        _failed("run_canonical", runs, partitions=len(namespaces)),
        _failed("usage_beside_settled_attempt", usage, partitions=len(namespaces)),
        _failed("protected_case_canonical", _protected_case_violations(cases), cases=len(cases)),
        _failed("reserved_record_kinds", unknown, partitions=len(namespaces)),
    )


def stray_reconciliation_check(items: Sequence[Item]) -> Check:
    """No §3.1 reconciliation attribute exists on any row other than a ``FENCE``."""
    violations = [
        _key(item)
        for item in items
        if any(name in item for name in RECONCILIATION_FIELD_NAMES)
        and not (
            (_text(item, "partition_key") or "").startswith(TABLE_ATTEMPT_PREFIX)
            and _text(item, "record_key") == FENCE_RECORD_KEY
        )
    ]
    return _failed("stray_reconciliation_attributes", violations, rows=len(items))


# --- Execution evidence (§2.5 "quiet executions") -------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    """The durable-authority half of execution evidence, keyed for the report."""

    nonterminal_plans: tuple[str, ...]
    nonterminal_units: tuple[str, ...]
    nonterminal_runs: tuple[str, ...]
    nonterminal_attempts: tuple[str, ...]
    nonclear_fences: tuple[str, ...]
    pending_fences: tuple[str, ...]
    blocked_fences: tuple[str, ...]

    @property
    def quiet(self) -> bool:
        """Whether no durable nonterminal authority or nonclear fence exists."""
        return not any(
            (
                self.nonterminal_plans,
                self.nonterminal_units,
                self.nonterminal_runs,
                self.nonterminal_attempts,
                self.nonclear_fences,
                self.pending_fences,
                self.blocked_fences,
            )
        )

    def document(self) -> dict[str, object]:
        """Render the report member."""
        return {
            "nonterminal_plans": list(self.nonterminal_plans),
            "nonterminal_units": list(self.nonterminal_units),
            "nonterminal_runs": list(self.nonterminal_runs),
            "nonterminal_attempts": list(self.nonterminal_attempts),
            "nonclear_fences": list(self.nonclear_fences),
            "pending_fences": list(self.pending_fences),
            "blocked_fences": list(self.blocked_fences),
            "quiet": self.quiet,
        }


def _is_plan_meta(item: Item) -> bool:
    partition = _text(item, "partition_key") or ""
    return (
        partition.startswith(WRITE_PLAN_PREFIX)
        and _INTENT_SEGMENT not in partition
        and _text(item, "record_key") == META_RECORD_KEY
    )


def _unit_terminal(item: Item) -> bool:
    """Whether a plan unit is terminal; a `superseded` one only with its recorded evidence.

    TD-50: the reviewed orphan disposition writes `superseded` together with its evidence digest.
    A `superseded` unit without that digest was not written by it and stays nonterminal.
    """
    status = _text(item, "status")
    if status == PlanUnitStatus.SUPERSEDED.value:
        return is_canonical_digest(_text(item, UNIT_SUPERSESSION_EVIDENCE_FIELD) or "")
    return status in _UNIT_TERMINAL


def execution_evidence(items: Sequence[Item]) -> ExecutionEvidence:
    """Collect every nonterminal owner row and nonclear fence from the complete read."""
    plans: list[str] = []
    units: list[str] = []
    runs: list[str] = []
    attempts: list[str] = []
    nonclear: list[str] = []
    pending: list[str] = []
    blocked: list[str] = []
    for item in items:
        partition = _text(item, "partition_key") or ""
        record = _text(item, "record_key") or ""
        state = _text(item, "state")
        if _is_plan_meta(item) and _text(item, "status") not in _PLAN_META_TERMINAL:
            plans.append(_key(item))
        elif (
            partition.startswith(WRITE_PLAN_PREFIX)
            and _INTENT_SEGMENT not in partition
            and record.startswith(UNIT_RECORD_PREFIX)
            and not _unit_terminal(item)
        ):
            units.append(_key(item))
        elif partition.startswith(TABLE_ATTEMPT_PREFIX):
            if record.startswith(RUN_RECORD_PREFIX) and state not in _RUN_TERMINAL:
                runs.append(_key(item))
            elif record.startswith(ATTEMPT_RECORD_PREFIX) and _text(item, "terminal_at") is None:
                attempts.append(_key(item))
            elif record == FENCE_RECORD_KEY and state != "clear":
                nonclear.append(_key(item))
                status = _text(item, "reconcile_status")
                if status == "pending":
                    pending.append(_key(item))
                elif status == "blocked":
                    blocked.append(_key(item))
    return ExecutionEvidence(
        tuple(sorted(plans)),
        tuple(sorted(units)),
        tuple(sorted(runs)),
        tuple(sorted(attempts)),
        tuple(sorted(nonclear)),
        tuple(sorted(pending)),
        tuple(sorted(blocked)),
    )


# --- Forward-edge evidence ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EdgeObservation:
    """Everything one ``verify-edge`` run has read, before any predicate."""

    target: str
    items: Sequence[Item]
    delivery_reserved_concurrency: int | None
    queue_counts: Mapping[str, int]
    dlq_counts: Mapping[str, int]
    handoff_state: Item | None


_EDGE_TARGETS: Final = frozenset({"INLINE_QUIESCED", "QUEUED_ACCUMULATE", "QUEUED_ACTIVE"})


def _transitional_ledger_violations(items: Sequence[Item], commit_instant: str) -> list[str]:
    """§2.5 statement-ledger proof: no delivery-composition statement postdates the commit."""
    violations: list[str] = []
    transitional_runs: set[tuple[str, str]] = set()
    for item in items:
        partition = _text(item, "partition_key") or ""
        record = _text(item, "record_key") or ""
        if not partition.startswith(TABLE_ATTEMPT_PREFIX):
            continue
        if (
            record.startswith(RUN_RECORD_PREFIX)
            and _text(item, "writer_kind") == WriterKind.TRANSITIONAL_DELIVERY.value
        ):
            transitional_runs.add((partition, record.removeprefix(RUN_RECORD_PREFIX)))
            created = _text(item, "created_at") or ""
            if created > commit_instant:
                violations.append(_key(item))
    for item in items:
        partition = _text(item, "partition_key") or ""
        record = _text(item, "record_key") or ""
        if not (
            partition.startswith(TABLE_ATTEMPT_PREFIX) and record.startswith(ATTEMPT_RECORD_PREFIX)
        ):
            continue
        owner = _text(item, "inline_run_owner_ref")
        if owner is None or (partition, owner) not in transitional_runs:
            continue
        submitted = _text(item, "submitted_at")
        if submitted is not None and submitted > commit_instant:
            violations.append(_key(item))
    return violations


def edge_checks(observation: EdgeObservation) -> tuple[Check, ...]:
    """Evaluate the closed forward-edge evidence for one target state."""
    if observation.target not in _EDGE_TARGETS:
        raise ValueError("verify-edge target is outside the closed vocabulary.")
    evidence = execution_evidence(observation.items)
    concurrency = observation.delivery_reserved_concurrency
    checks: list[Check] = []
    if observation.target == "INLINE_QUIESCED":
        checks.append(
            Check(
                "delivery_concurrency_zero",
                concurrency == 0,
                () if concurrency == 0 else ("delivery_function",),
                {"reserved_concurrency": concurrency},
            )
        )
        checks.append(
            _failed(
                "automatic_writer_attempts_terminal_fences_clear",
                (
                    *evidence.nonterminal_attempts,
                    *evidence.nonclear_fences,
                    *evidence.nonterminal_runs,
                ),
            )
        )
        return tuple(checks)
    state = observation.handoff_state
    commit_instant = None if state is None else _text(state, "last_transition_at")
    if state is None or commit_instant is None or _text(state, "disposition") != "ACTIVE":
        checks.append(Check("handoff_state_active", False, ("PROJECTION_HANDOFF STATE",)))
        return tuple(checks)
    checks.append(Check("handoff_state_active", True, (), {"state": _text(state, "state")}))
    if observation.target == "QUEUED_ACCUMULATE":
        # §2.5: only the ACCUMULATE edge demands quiet executions — nothing queued-composition
        # has run yet, so any nonterminal owner row or queue member is old-version evidence.
        # The ACTIVE edge re-proves only the statement-ledger absence: accumulated nonterminal
        # plans, queued units, and visible queue messages are that state's designed content.
        quiet_keys = (
            *evidence.nonterminal_plans,
            *evidence.nonterminal_units,
            *evidence.nonterminal_runs,
            *evidence.nonterminal_attempts,
            *evidence.nonclear_fences,
        )
        in_flight = sum(observation.queue_counts.values()) + sum(observation.dlq_counts.values())
        checks.append(
            _failed(
                "no_old_version_delivery_execution",
                (*quiet_keys, *(("semantic_write_queue",) if in_flight else ())),
                queue=dict(observation.queue_counts),
                dlq=dict(observation.dlq_counts),
            )
        )
    checks.append(
        _failed(
            "no_delivery_composition_statements_after_commit",
            _transitional_ledger_violations(observation.items, commit_instant),
            commit_instant=commit_instant,
        )
    )
    return tuple(checks)
