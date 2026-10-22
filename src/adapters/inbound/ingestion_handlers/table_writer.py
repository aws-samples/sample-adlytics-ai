# SPDX-License-Identifier: MIT-0
"""Phase-2 ADR-075 table writer: project and atomically finalize one table unit.

One SQS FIFO delivery carries the six-field internal owner body (§4). The writer
strongly reads the successor owner, origin intent, plan ``META``, unit, and plan
``HEAD``; requires byte-exact body reproduction; claims ``queued|failed →
writing`` with its receive-derived writer execution identity; fences the table
(§5); composes the unit's merges and single verification (§6); drives every
statement through the §5 protocol — monotonic deadlines, the closed step-2
validator, ``BeginTableAttemptRequest``, the dedicated one-call start client,
``RecordSubmittedAttemptRequest`` on ``Accepted``, bounded terminal polling with
cancellation at ``Q`` and parking at ``S``, and ``CompleteTableAttemptRequest``
on a provider terminal — verifies the readback once, finalizes the unit with its
pre-resolved pointer outcomes and atomic settlement (§2.2 step 7), releases the
fence, and conditionally completes the plan (step 9). ``TABLE_BUSY`` and
``TABLE_ATTEMPT_PENDING`` are recorded through ``RecordStage1RefusalRequest``,
receive one exact visibility override, and stay retryable. Every write flows
through the coordinator's conditioned transactions; this adapter owns time,
never the conditions.

S6 §2.5: in ``GRAPH`` a graph envelope carries exactly the owner fields of a ``write_unit``
body or a ``publish`` body, plus its dispatch binding; any other graph shape is an invariant
(zero effect, unacknowledged, reported once). A unit runs through the shared inline run
(``WriterKind.GRAPH_UNIT``, no pointers) under its graph claim and settles its node itself
(D14, ``graph_unit``); ``publish`` stages, flips and folds its window (``graph_publish``). Each
graph envelope opens ``TABLE_WRITER_GRAPH_UNIT`` or ``GRAPH_PUBLISH`` as its root span; the
six-field legacy form keeps ``TABLE_WRITER_PROCESS``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.metrics import MetricUnit, single_metric
from botocore.exceptions import BotoCoreError, ClientError

from adapters.config import (
    settings_without_client_secret,
    table_writer_settings,
    tracing_settings,
)
from adapters.graph_authority import provisioned_graph_table
from adapters.inbound.ingestion_handlers import graph_publish, graph_unit
from adapters.inbound.ingestion_handlers.graph_claim import (
    ClaimDisposition,
    GraphClaimInput,
    claim_graph_envelope,
)
from adapters.inbound.ingestion_handlers.graph_materialize import classify_failure
from adapters.inbound.ingestion_handlers.inline_run import (
    InlineRunOutcome,
    InlineRunRequest,
    InlineRunResult,
    InlineRunRuntime,
    run_inline_table,
)
from adapters.inbound.ingestion_handlers.table_attempt_protocol import (
    PROJECTION_INVALID,
    LocalStatementRefusedError,
    StatementParked,
    StatementProtocolRuntime,
    StatementResult,
    StatementRun,
    epoch_micros,
    instant,
    run_statement,
    verification_holds,
)
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.jobs.execution_graph.node_verifier import (
    ClaimAttemptFacts,
    NodeFacts,
    UnreachablePostGrantFacts,
)
from adapters.outbound.athena.merge_shapes import MergeShape, merge_shapes
from adapters.outbound.athena.semantic_storage import (
    AthenaSemanticTarget,
    PhysicalRecord,
    UnitWorkload,
    canonical_unit_records,
    source_records,
)
from adapters.outbound.athena.table_attempt_start import Stage1AthenaStartClient
from adapters.outbound.athena.table_attempt_statements import (
    ComposedStatement,
    ComposeUnitStatementsRequest,
    UnitStatementComposition,
    compose_unit_statements,
)
from adapters.outbound.athena.table_attempt_terminal import Stage1AthenaTerminalClient
from adapters.outbound.aws_errors import merged_aws_failure_context
from adapters.outbound.dynamodb.execution_graph_dispatch import (
    BATCH_NODE,
    DynamoGraphDispatchStore,
    batch_dispatch_id,
)
from adapters.outbound.dynamodb.projection_coordinator import DynamoProjectionCoordinator
from adapters.outbound.dynamodb.semantic_state import (
    DynamoPublicationStore,
    DynamoPublicationStoreAccess,
)
from adapters.outbound.dynamodb.stage1_lifecycle_head import DynamoStage1LifecycleHeadStore
from adapters.outbound.dynamodb.stage1_writer_authority import (
    Stage1WriterAuthorityReader,
    unit_entry_from_item,
)
from adapters.outbound.dynamodb.table_attempt import DynamoTableAttemptStore
from adapters.outbound.s3.semantic_storage import (
    S3ProjectionObjectSource,
    projection_source_publication,
    projection_unit_source,
    source_object_key,
)
from adapters.outbound.s3.unit_staging import S3UnitStager
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    METRICS,
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import (
    GRAPH_PUBLISH,
    TABLE_WRITER_GRAPH_UNIT,
    TABLE_WRITER_GRAPH_UNIT_BATCH,
    TABLE_WRITER_PLAN_TERMINAL,
    TABLE_WRITER_PROCESS,
    TABLE_WRITER_UNIT,
    span,
    trace_boundary,
)
from adapters.outbound.v2_storage_catalog import V2StorageCatalog, V2Table, load_v2_storage_catalog
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_dispatch import (
    GRAPH_BINDING_FIELDS,
    GRAPH_UNIT_CLAIM_REFUSED_LIVE,
    GRAPH_UNIT_SET_COMPOSITION,
    MAX_BATCH_MEMBERS,
    TABLE_LOCK_PREFIX,
    Acceptance,
    EnvelopeKind,
    NodeState,
    ReturnReason,
    execution_set_id_for,
    table_writer_acceptance,
    unit_may_return,
)
from domain.operations.generated_execution_graphs import BOUNDS, WRITE_UNIT_BATCHING
from domain.operations.queue_clocks import queue_attributes
from domain.operations.stage1_lifecycle_head import (
    Stage1LifecycleHeadTuple,
    Stage1LifecycleProof,
    Stage1LifecycleProofRequest,
    Stage1LifecycleTransition,
    derive_stage1_lifecycle_proof,
    stage1_head_authority_document,
)
from domain.operations.stage1_operational_status import (
    OwnerDisposition as StatusOwnerDisposition,
)
from domain.operations.stage1_operational_status import (
    PlanAdmissionSource,
    PlanIdentityRequest,
    PlanRequiredChildExpectations,
    PointerExecutionSource,
    PointerExecutionSourceKind,
    PointerIdentityRequest,
    PriorOwnerEventSource,
    SameTransactionTerminalSource,
    SettleStage1AthenaUsageRequest,
    Stage1IdentityResult,
    Stage1OwnerTransition,
    Stage1TopologyRequest,
    UnitIdentityRequest,
    derive_plan_identity,
    derive_unit_identity,
)
from domain.operations.stage1_operational_status import (
    ProviderTerminalState as StatusProviderTerminalState,
)
from domain.operations.stage1_operational_status import (
    RequiredChildExpectation as StatusChildExpectation,
)
from domain.operations.statement_protocol import (
    MIN_EXCLUSIVE_BUDGET_MS,
    InsufficientStatementBudget,
    StatementStartBounds,
    unit_statement_kinds,
)
from domain.operations.status import (
    MEASUREMENT_REFS,
    CompletionOutcome,
    EvaluationBinding,
    FlowState,
    canonical_authority_digest,
)
from domain.operations.table_attempt import (
    AttemptObservation,
    FenceObservation,
    ProviderTerminalState,
    WriterKind,
)
from domain.operations.table_lease import (
    TableLeaseRecord,
    lease_expiry_epoch_micros,
    queued_lease_holder,
)
from domain.operations.write_plan import (
    CanonicalObjectEntry,
    CanonicalUnitEntry,
    IntentMeta,
    IntentState,
    PlanMeta,
    PlanStatus,
    SettledAttemptReference,
    SuccessorOwner,
    plan_partition_key,
)
from domain.semantics.canonical_compiler import canonical_checksum_digest
from domain.shared_kernel.errors import (
    GRAPH_UNIT_BATCH_ACCOUNTING_CODE,
    GRAPH_UNIT_CLAIM_REFUSED_LIVE_CODE,
    GRAPH_UNIT_SET_COMPOSITION_CODE,
    WRITE_UNIT_BATCHING_PENDING_PROBE_CODE,
    AdlyticsError,
    DependencyError,
    DomainError,
    ErrorCode,
    InvariantError,
)
from ports.outbound.canonical import CanonicalDestination
from ports.outbound.execution_graph_dispatch import ClaimRecord, GraphDispatchStore, NodeRecord
from ports.outbound.projection_coordinator import (
    RUN_RESERVATION_CAP,
    AcquireTableFenceOutcome,
    AcquireTableFenceRequest,
    AthenaAttemptSettlement,
    AttemptBudgetExhaustion,
    AttestedUnitObservation,
    AuthorityModeGuard,
    BeginQueuedUnitOwner,
    ClaimUnitOutcome,
    ClaimUnitRequest,
    CompletePlanRequest,
    CurrentPointerOutcome,
    FailUnitRequest,
    FinalizeUnitRequest,
    GraphAuthorityRefusedError,
    GraphClaimFence,
    ModeGuardKind,
    PlanHeadObservation,
    PointerOutcomeKind,
    ProjectionCoordinatorPort,
    QueuedUnitAttemptOwner,
    RecordStage1RefusalRequest,
    RefusedUnitObservation,
    ReleaseTableFenceRequest,
    SettledAttemptObservation,
    Stage1RefusalReason,
    Stage1RefusalTag,
    Stage1SettlementExpansion,
    UnitFailureReason,
)
from ports.outbound.table_attempt import (
    TableAttemptStore,
)
from ports.outbound.table_attempt_start import (
    TableAttemptStartClient,
)
from ports.outbound.table_attempt_terminal import (
    TableAttemptTerminalClient,
)
from ports.outbound.unit_staging import StagingUnavailableError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Mapping, Sequence

    from types_boto3_sqs import SQSClient
    from types_boto3_sqs.type_defs import ChangeMessageVisibilityRequestTypeDef

    from ports.outbound.unit_staging import UnitStager

__all__ = [
    "GraphDispatchBinding",
    "GraphEnvelope",
    "GraphEnvelopeKind",
    "GraphEnvelopeShapeError",
    "SqsMessageVisibility",
    "WriteUnitDelivery",
    "graph_envelope_from_body",
    "handler",
    "refusal_visibility_seconds",
    "table_writer_delivery",
    "visibility_change_request",
    "write_unit_delivery",
    "writer_execution_id",
]

LOGGER = Logger(service="adlytics-table-writer")
_PROJECTION_INVALID = PROJECTION_INVALID
_VISIBILITY_UNAVAILABLE = ErrorCode("TABLE_WRITER", 3)
_SQS_MAX_VISIBILITY_SECONDS: Final = 43_200
_CANONICAL_KEY_PARTS: Final = 5
_PLAN_KEY_PARTS: Final = 3
_SCHEMA_VERSION: Final = "1"
WRITER_EXECUTION_NAMESPACE: Final = "stage1-writer-execution/v1"
_POINTER_REBUILD_LIMIT: Final = 3
_INSTANT_DIGITS: Final = 20
_MESSAGE_FIELDS: Final = frozenset(
    {
        "schema_version",
        "plan_key",
        "plan_digest",
        "origin_intent_id",
        "generation",
        "unit_key",
    }
)
_GRAPH_FIELDS: Final = GRAPH_BINDING_FIELDS
"""A body carrying the dispatch binding is a graph envelope (V-12)."""
_BATCH_FIELDS: Final = frozenset({"batch_id", "lock_key", "members"})
"""Slice 7 (P2): a body carrying any batch field is a graph envelope too, so a malformed batch
is refused `GRAPH_ENVELOPE_SHAPE_INVALID`, never read as legacy."""
_BATCH_OWNER_FIELDS: Final = frozenset({"schema_version", "node", *_BATCH_FIELDS})
"""Slice 7 (D31): the semantic-write message for units, a list of member references."""
_BATCH_MEMBER_FIELDS: Final = frozenset({"run_id", "node_id", "dispatch_id"})
_PUBLISH_OWNER_FIELDS: Final = frozenset(
    {
        "schema_version",
        "post_grant",
        "grant_id",
        "granted_run",
        "conflict_unit",
        "served_revision",
        "root_flow_id",
    }
)
"""S6 §2.5: the `publish` node's owner body (`post_grant_body`)."""
_PUBLISH_NODE: Final = "publish"
_UNIT_MEMBER_ARITY: Final = 4
GRAPH_ENVELOPE_SHAPE_INVALID: Final = "GRAPH_ENVELOPE_SHAPE_INVALID"
_UNIT_MEASUREMENT: Final = "semantic_projection_unit"

# Configuration and tracing are fixed before any SDK client is constructed. A writer
# that cannot resolve its exact deployed §11 values never becomes healthy.
SETTINGS = settings_without_client_secret()
WRITER_SETTINGS = table_writer_settings()
# S3: the execution-graph table, for the per-batch strong mode read (V-12). None where the graph
# authority is not provisioned, which reads LEGACY (`adapters.graph_authority`).
GRAPH_TABLE = provisioned_graph_table()
TRACING = configure_tracing(tracing_settings())
ROOT = contracts_root()
S3 = boto3.client("s3")
SQS = boto3.client("sqs")
DYNAMODB = boto3.client("dynamodb")


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _sleep_ms(duration_ms: int) -> None:
    time.sleep(max(0, duration_ms) / 1_000)


def writer_execution_id(message_id: str, receive_count: int) -> str:
    """Derive the sole §4 ``stage1-writer-execution/v1`` receive identity."""
    if not message_id.strip():
        raise ValueError("writer execution identity requires the exact received MessageId.")
    if isinstance(receive_count, bool) or receive_count < 1:
        raise ValueError("writer execution identity requires a positive receive count.")
    return canonical_authority_digest([WRITER_EXECUTION_NAMESPACE, message_id, receive_count])


# --- §4 delivery parsing --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphDispatchBinding:
    """The dispatch binding a graph envelope carries beside its owner body."""

    dispatch_id: str
    run_id: str
    node_id: str


@dataclass(frozen=True, slots=True)
class WriteUnitDelivery:
    """One exact SQS receive and the closed six-field internal owner body it carries."""

    plan_key: str
    plan_digest: str
    origin_intent_id: str
    generation: int
    unit_key: str
    message_id: str
    receipt_handle: str
    receive_count: int
    sent_timestamp: str | None = None
    first_receive_timestamp: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.plan_key,
            self.plan_digest,
            self.origin_intent_id,
            self.unit_key,
            self.message_id,
            self.receipt_handle,
        )
        if not all(value.strip() for value in required):
            raise ValueError("write-unit delivery identities cannot be blank.")
        if isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("write-unit generation must be a nonnegative integer.")
        if self.receive_count < 1:
            raise ValueError("write-unit receive count must be positive.")
        expected = plan_partition_key(self.plan_digest, self.internal_delivery_execution_id)
        if self.plan_key != expected:
            raise ValueError(
                "write-unit plan key does not reproduce from its digest and execution."
            )

    @property
    def internal_delivery_execution_id(self) -> str:
        """The internal execution segment bound into the plan key."""
        parts = self.plan_key.split("#")
        if len(parts) != _PLAN_KEY_PARTS:
            raise ValueError("write-unit plan key does not carry its execution segment.")
        return parts[2]

    @property
    def writer_execution_id(self) -> str:
        """This exact receive's identity: the sole §4 constructor output, supplied whole."""
        return writer_execution_id(self.message_id, self.receive_count)


def _mapping(value: object, message: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(message)
    return {str(key): item for key, item in cast("dict[object, object]", value).items()}


def _text(value: object, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    return value


def _plain_int(value: object, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(message)
    return value


@dataclass(frozen=True, slots=True)
class _SqsRecord:
    """The one SQS record of a batch-of-one and its parsed JSON body."""

    body_text: str
    body: dict[str, object]
    message_id: str
    receipt_handle: str
    receive_count: int
    sent_timestamp: str | None
    first_receive_timestamp: str | None


def _sqs_record(event: Mapping[str, object]) -> _SqsRecord:
    records_value = event.get("Records")
    if not isinstance(records_value, list):
        raise ValueError("table writer requires an SQS batch containing exactly one record.")
    records = cast("list[object]", records_value)
    if len(records) != 1:
        raise ValueError("table writer requires an SQS batch containing exactly one record.")
    record = _mapping(records[0], "table-writer SQS record must be an object.")
    body_text = _text(record.get("body"), "table-writer SQS body must be a JSON string.")
    body = _mapping(json.loads(body_text), "table-writer SQS body must be an object.")
    attributes = _mapping(
        record.get("attributes"),
        "table-writer SQS record requires message attributes.",
    )
    receive = _text(
        attributes.get("ApproximateReceiveCount"),
        "table-writer SQS record requires ApproximateReceiveCount.",
    )
    if not receive.isdecimal() or int(receive) < 1:
        raise ValueError("table-writer ApproximateReceiveCount must be a positive integer.")
    return _SqsRecord(
        body_text=body_text,
        body=body,
        message_id=_text(record.get("messageId"), "table-writer messageId is required."),
        receipt_handle=_text(
            record.get("receiptHandle"), "table-writer receiptHandle is required."
        ),
        receive_count=int(receive),
        sent_timestamp=_optional_text(attributes.get("SentTimestamp")),
        first_receive_timestamp=_optional_text(attributes.get("ApproximateFirstReceiveTimestamp")),
    )


def _legacy_delivery(sqs: _SqsRecord) -> WriteUnitDelivery:
    body = sqs.body
    if frozenset(body) != _MESSAGE_FIELDS:
        raise ValueError("table-writer SQS body must contain exactly the six owner fields.")
    if body.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("table-writer SQS body must carry schema_version 1.")
    return WriteUnitDelivery(
        plan_key=_text(body.get("plan_key"), "write-unit plan key is required."),
        plan_digest=_text(body.get("plan_digest"), "write-unit plan digest is required."),
        origin_intent_id=_text(
            body.get("origin_intent_id"), "write-unit origin intent identity is required."
        ),
        generation=_plain_int(
            body.get("generation"), "write-unit generation must be a JSON integer."
        ),
        unit_key=_text(body.get("unit_key"), "write-unit unit key is required."),
        message_id=sqs.message_id,
        receipt_handle=sqs.receipt_handle,
        receive_count=sqs.receive_count,
        sent_timestamp=sqs.sent_timestamp,
        first_receive_timestamp=sqs.first_receive_timestamp,
    )


def write_unit_delivery(event: Mapping[str, object]) -> WriteUnitDelivery:
    """Parse exactly one SQS record carrying exactly the six §4 owner fields."""
    return _legacy_delivery(_sqs_record(event))


class GraphEnvelopeKind(StrEnum):
    """The two graph envelopes the semantic-write route carries (S6 §2.5).

    Slice 7 (D31): a unit is always a batch of member references; a single unit is a batch of
    one. The pre-1b single-unit `write_unit` body is refused as any other shape.
    """

    WRITE_UNIT_BATCH = "write_unit_batch"
    PUBLISH = "publish"


class GraphEnvelopeShapeError(InvariantError):
    """A graph envelope whose owner body is neither exact shape: zero effect, unacknowledged."""

    def __init__(self, sqs_message: tuple[str, int], detail: str) -> None:
        super().__init__(
            ErrorCode("TABLE_WRITER", 4),
            "a graph envelope does not carry exactly a write_unit or publish owner body.",
            context={"signal": GRAPH_ENVELOPE_SHAPE_INVALID, "detail": detail},
        )
        self.message_id, self.receive_count = sqs_message
        self.signal = GRAPH_ENVELOPE_SHAPE_INVALID


@dataclass(frozen=True, slots=True)
class GraphBatchMember:
    """One member reference of a unit batch envelope (D31): the unit's bytes stay on its node."""

    run_id: str
    node_id: str
    dispatch_id: str


@dataclass(frozen=True, slots=True)
class GraphEnvelope:
    """One received graph envelope on the semantic-write route, and its SQS receive."""

    kind: GraphEnvelopeKind
    body_text: str
    """The exact received body; the claim recomputes the envelope digest from it."""
    message_id: str
    receipt_handle: str
    receive_count: int
    sent_timestamp: str | None = None
    first_receive_timestamp: str | None = None
    binding: GraphDispatchBinding | None = None
    """The `publish` dispatch binding; a unit batch carries none (§2.5 "Envelope")."""
    conflict_unit: str = ""
    root_flow_id: str = ""
    grant_id: str | None = None
    """The `publish` grant; None for a unit batch."""
    granted_run: str | None = None
    served_revision: str | None = None
    batch_id: str | None = None
    """Slice 7: a unit batch's identity; None for `publish`."""
    lock_key: str | None = None
    members: tuple[GraphBatchMember, ...] = ()

    @property
    def node(self) -> str:
        """The owner body's node name, as a span names it."""
        return self.kind.value

    def required_binding(self) -> GraphDispatchBinding:
        """Return the `publish` binding; a batch reaching a binding reader is an invariant."""
        if self.binding is None:
            raise _invariant("A graph envelope without a dispatch binding reached its claim.")
        return self.binding


def _batch_envelope(sqs: _SqsRecord) -> GraphEnvelope:
    """Parse a unit batch envelope, refusing every malformed shape (§2.5 "Envelope", test 10).

    Refused: other owner fields, no members, a repeated `node_id`, more than
    `MAX_BATCH_MEMBERS`, and a `lock_key` outside `TABLE#<k>`.
    """
    body = sqs.body
    refused = (sqs.message_id, sqs.receive_count)
    if frozenset(body) != _BATCH_OWNER_FIELDS or body.get("node") != BATCH_NODE:
        raise GraphEnvelopeShapeError(refused, "owner fields")
    try:
        if body.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("a graph owner body carries schema_version 1.")
        lock_key = _text(body.get("lock_key"), "lock_key is required.")
        if not lock_key.startswith(TABLE_LOCK_PREFIX) or lock_key == TABLE_LOCK_PREFIX:
            raise ValueError("a unit batch holds a TABLE# lock.")
        raw = body.get("members")
        if (
            not isinstance(raw, list)
            or not raw
            or len(cast("list[object]", raw)) > MAX_BATCH_MEMBERS
        ):
            raise ValueError(f"a unit batch lists 1 to {MAX_BATCH_MEMBERS} members.")
        members: list[GraphBatchMember] = []
        for entry in cast("list[object]", raw):
            if not isinstance(entry, dict):
                raise ValueError("a batch member is an object.")
            fields = cast("dict[str, object]", entry)
            if frozenset(fields) != _BATCH_MEMBER_FIELDS:
                raise ValueError("a batch member carries exactly run_id, node_id, dispatch_id.")
            members.append(
                GraphBatchMember(
                    run_id=_text(fields.get("run_id"), "member run_id is required."),
                    node_id=_text(fields.get("node_id"), "member node_id is required."),
                    dispatch_id=_text(fields.get("dispatch_id"), "member dispatch_id is required."),
                )
            )
        if len({member.node_id for member in members}) != len(members):
            raise ValueError("a unit batch lists each node once.")
        return GraphEnvelope(
            kind=GraphEnvelopeKind.WRITE_UNIT_BATCH,
            body_text=sqs.body_text,
            message_id=sqs.message_id,
            receipt_handle=sqs.receipt_handle,
            receive_count=sqs.receive_count,
            sent_timestamp=sqs.sent_timestamp,
            first_receive_timestamp=sqs.first_receive_timestamp,
            batch_id=_text(body.get("batch_id"), "batch_id is required."),
            lock_key=lock_key,
            members=tuple(members),
        )
    except (AdlyticsError, ValueError) as malformed:
        raise GraphEnvelopeShapeError(refused, type(malformed).__name__) from None


def _unit_entry(value: object) -> CanonicalUnitEntry:
    if not isinstance(value, list) or len(cast("list[object]", value)) != _UNIT_MEMBER_ARITY:
        raise ValueError("a write_unit body carries the four-member canonical unit entry.")
    unit_key, table_kind, merge_count, objects = cast("list[object]", value)
    return unit_entry_from_item(
        {
            "unit_key": unit_key,
            "table_kind": table_kind,
            "merge_count": merge_count,
            "objects": objects,
        }
    )


def graph_envelope_from_body(sqs: _SqsRecord) -> GraphEnvelope:
    """Parse one graph envelope: exactly the `write_unit_batch` or `publish` owner fields."""
    body = sqs.body
    if frozenset(body) & _BATCH_FIELDS or body.get("node") == BATCH_NODE:
        return _batch_envelope(sqs)
    owner = frozenset(body) - _GRAPH_FIELDS
    refused = (sqs.message_id, sqs.receive_count)
    try:
        binding = GraphDispatchBinding(
            dispatch_id=_text(body.get("dispatch_id"), "graph dispatch_id is required."),
            run_id=_text(body.get("run_id"), "graph run_id is required."),
            node_id=_text(body.get("node_id"), "graph node_id is required."),
        )
        if body.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("a graph owner body carries schema_version 1.")
        common = {
            "binding": binding,
            "conflict_unit": _text(body.get("conflict_unit"), "conflict_unit is required."),
            "root_flow_id": _text(body.get("root_flow_id"), "root_flow_id is required."),
            "body_text": sqs.body_text,
            "message_id": sqs.message_id,
            "receipt_handle": sqs.receipt_handle,
            "receive_count": sqs.receive_count,
            "sent_timestamp": sqs.sent_timestamp,
            "first_receive_timestamp": sqs.first_receive_timestamp,
        }
        if owner == _PUBLISH_OWNER_FIELDS and body.get("post_grant") == _PUBLISH_NODE:
            return GraphEnvelope(
                kind=GraphEnvelopeKind.PUBLISH,
                grant_id=_text(body.get("grant_id"), "grant_id is required."),
                granted_run=_text(body.get("granted_run"), "granted_run is required."),
                served_revision=_text(body.get("served_revision"), "served_revision is required."),
                **common,  # type: ignore[arg-type]
            )
    except (AdlyticsError, ValueError) as malformed:
        raise GraphEnvelopeShapeError(refused, type(malformed).__name__) from None
    raise GraphEnvelopeShapeError(refused, "owner fields")


def table_writer_delivery(event: Mapping[str, object]) -> WriteUnitDelivery | GraphEnvelope:
    """Parse one SQS record: a body carrying a binding or batch field is a graph envelope."""
    sqs = _sqs_record(event)
    if frozenset(sqs.body) & (_GRAPH_FIELDS | _BATCH_FIELDS):
        return graph_envelope_from_body(sqs)
    return _legacy_delivery(sqs)


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


# --- Visibility -----------------------------------------------------------------------------------


def refusal_visibility_seconds(target_micros: int | None, now_micros: int) -> int:
    """Derive the one §5 refusal visibility override from a target instant.

    ``target_micros=None`` is the valid-blocked-authority case, which uses the
    ``lease+margin`` cap directly; otherwise the exact signed-microsecond rule
    ``min(cap, max(1, ceil(remaining) + margin))`` applies.
    """
    margin = WRITER_SETTINGS.semantic_write_visibility_margin_seconds
    cap = WRITER_SETTINGS.table_lease_duration_seconds + margin
    if target_micros is None:
        return cap
    difference = target_micros - now_micros
    remaining = 0 if difference <= 0 else -(-difference // 1_000_000)
    return min(cap, max(1, remaining + margin))


ADMISSION_RETRY_SECONDS: Final = 30
"""The redelivery step for a unit read before its admission landed (2026-10-06).

The plan-intent reconciler sends each unit's message, then admits it, so a writer that receives
the message first classifies it `admission_incomplete`. Measured over all 10 occurrences on
2026-10-06, admission completed at most 0.125 s after that read (p95 0.125 s, median 0.045 s).
Without an override the unit waited the queue's 960 s visibility. 30 s is the floor, and receive
`n` waits `30 x n` s, so the two waits before the third (last) receive sum to 90 s: past one
missed reconciler tick (60 s), and far past 10 x the measured maximum.
"""


def admission_retry_seconds(receive_count: int) -> int:
    """Return the redelivery delay for an `admission_incomplete` unit on receive `receive_count`."""
    return ADMISSION_RETRY_SECONDS * max(1, receive_count)


def visibility_change_request(
    queue_url: str,
    receipt_handle: str,
    timeout_seconds: int,
) -> ChangeMessageVisibilityRequestTypeDef:
    """Shape the exact configured-queue visibility override request."""
    if not queue_url.strip() or not receipt_handle.strip():
        raise ValueError("visibility override requires queue URL and receipt handle.")
    if not 0 <= timeout_seconds <= _SQS_MAX_VISIBILITY_SECONDS:
        raise ValueError("visibility override seconds are outside the SQS bound.")
    return {
        "QueueUrl": queue_url,
        "ReceiptHandle": receipt_handle,
        "VisibilityTimeout": timeout_seconds,
    }


class SqsMessageVisibility:
    """Apply the one refusal visibility override on the configured semantic write queue."""

    def __init__(self, queue_url: str, client: SQSClient) -> None:
        if not queue_url.strip():
            raise ValueError("semantic write queue URL is required.")
        self._queue_url = queue_url
        self._client = client

    def change(self, receipt_handle: str, timeout_seconds: int) -> None:
        """Apply one visibility override; a failure propagates unhandled as TABLE_WRITER/3."""
        try:
            self._client.change_message_visibility(
                **visibility_change_request(self._queue_url, receipt_handle, timeout_seconds)
            )
        except (BotoCoreError, ClientError) as error:
            raise DependencyError(
                _VISIBILITY_UNAVAILABLE,
                "Semantic write-unit visibility could not be changed.",
                context=merged_aws_failure_context(error),
            ) from error
        except Exception as error:
            raise DependencyError(
                _VISIBILITY_UNAVAILABLE,
                "Semantic write-unit visibility could not be changed.",
                context={"cause": type(error).__name__},
            ) from error


# --- Runtime seams --------------------------------------------------------------------------------


class _AuthorityReads(Protocol):
    def owner(self, plan_digest: str) -> SuccessorOwner | None: ...

    def intent(self, plan_digest: str, intent_id: str) -> IntentMeta | None: ...

    def plan_meta(self, plan_key: str) -> PlanMeta | None: ...

    def plan_meta_item(self, plan_key: str) -> dict[str, object] | None: ...

    def unit_item(self, plan_key: str, unit_key: str) -> dict[str, object] | None: ...

    def plan_unit_items(self, plan_key: str) -> tuple[dict[str, object], ...]: ...

    def head(self, flow_id: str) -> Stage1LifecycleHeadTuple | None: ...

    def flow_root_clock(self, flow_id: str) -> datetime: ...

    def current_item(self, partition_identity: str) -> dict[str, object] | None: ...

    def lease(self, table_id: str) -> TableLeaseRecord | None: ...

    def fence(self, table_id: str) -> FenceObservation | None: ...

    def attempt(self, table_id: str, attempt_id: str) -> AttemptObservation | None: ...

    def attempt_item(self, table_id: str, attempt_id: str) -> dict[str, object] | None: ...

    def run_item(self, table_id: str, run_id: str) -> dict[str, object] | None: ...


class _ProjectionObjects(Protocol):
    def read(self, key: str) -> bytes: ...


class _MessageVisibility(Protocol):
    def change(self, receipt_handle: str, timeout_seconds: int) -> None: ...


class _GraphAuthority(GraphDispatchStore, Protocol):
    """The execution-graph authority TableWriter reads the mode from and claims through."""

    def read_mode_item(self) -> dict[str, object] | None:
        """Read the exact mode item (plain attributes) for a cross-table ConditionCheck."""
        ...

    def read_claim_item(self, node_id: str) -> dict[str, object] | None:
        """S6 §2.5: read the exact `EXEC_CLAIM#` item (plain attributes) for a Begin fence."""
        ...


@dataclass(frozen=True, slots=True)
class _Runtime:
    reader: _AuthorityReads
    coordinator: ProjectionCoordinatorPort
    attempts: TableAttemptStore
    start: TableAttemptStartClient
    terminals: TableAttemptTerminalClient
    objects: _ProjectionObjects
    stager: UnitStager
    """ADR-149: writes each unit's staged object just before its MERGE begins."""
    catalog: V2StorageCatalog
    target: AthenaSemanticTarget
    visibility: _MessageVisibility
    wall: Callable[[], datetime]
    monotonic_ms: Callable[[], int]
    sleep_ms: Callable[[int], None]
    graph: _GraphAuthority | None
    """None where the graph authority is not provisioned: every batch then reads LEGACY."""
    graph_facts: NodeFacts
    inline_run: Callable[[InlineRunRuntime, InlineRunRequest], InlineRunResult] = run_inline_table
    """S6 §2.5: the shared inline run a graph unit executes through."""
    publish: graph_publish.PublishPorts | None = None
    """S6 §2.6: the publications store and graph table `publish` flips through."""


def _runtime() -> _Runtime:
    configured = {
        "semantic bucket": SETTINGS.semantic_v2_bucket,
        "bake workgroup": SETTINGS.semantic_v2_athena_bake_workgroup,
        "source database": SETTINGS.semantic_v2_source_database,
        "candidate database": SETTINGS.semantic_v2_candidate_database,
        "current database": SETTINGS.semantic_v2_current_database,
        "staging database": SETTINGS.semantic_v2_staging_database,
        "staging bucket": SETTINGS.semantic_v2_staging_bucket,
    }
    missing = tuple(label for label, value in configured.items() if not value.strip())
    if missing:
        raise ValueError("Semantic table writer configuration is incomplete: " + ", ".join(missing))
    head_store = DynamoStage1LifecycleHeadStore(SETTINGS.semantic_publications_table, DYNAMODB)
    reader = Stage1WriterAuthorityReader(
        SETTINGS.semantic_publications_table,
        SETTINGS.semantic_leases_table,
        DYNAMODB,
    )
    return _Runtime(
        reader=reader,
        coordinator=DynamoProjectionCoordinator(
            SETTINGS.semantic_publications_table,
            SETTINGS.semantic_leases_table,
            head_store,
            DYNAMODB,
            graph_table=GRAPH_TABLE,
        ),
        attempts=DynamoTableAttemptStore(SETTINGS.semantic_publications_table, DYNAMODB),
        start=Stage1AthenaStartClient(),
        terminals=Stage1AthenaTerminalClient(),
        objects=S3ProjectionObjectSource(SETTINGS.semantic_v2_bucket, S3),
        stager=S3UnitStager(SETTINGS.semantic_v2_staging_bucket, S3),
        catalog=load_v2_storage_catalog(ROOT / "contracts/generated/v2-tables.json"),
        target=AthenaSemanticTarget(
            batch_workgroup=SETTINGS.semantic_v2_athena_bake_workgroup,
            source_database=SETTINGS.semantic_v2_source_database,
            candidate_database=SETTINGS.semantic_v2_candidate_database,
            current_database=SETTINGS.semantic_v2_current_database,
            staging_database=SETTINGS.semantic_v2_staging_database,
        ),
        visibility=SqsMessageVisibility(WRITER_SETTINGS.semantic_write_queue_url, SQS),
        wall=_utc_now,
        monotonic_ms=_monotonic_ms,
        sleep_ms=_sleep_ms,
        graph=None if GRAPH_TABLE is None else DynamoGraphDispatchStore(GRAPH_TABLE, DYNAMODB),
        graph_facts=ClaimAttemptFacts(reader),
        publish=None
        if GRAPH_TABLE is None
        else graph_publish.PublishPorts(
            publications=DynamoPublicationStore(
                SETTINGS.semantic_publications_table,
                SETTINGS.semantic_leases_table,
                DynamoPublicationStoreAccess(client=DYNAMODB),
            ),
            graph_table=GRAPH_TABLE,
        ),
    )


# --- Typed unit outcomes and internal signals -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class _UnitResult:
    """One receive's acknowledgement decision and its closed outcome label."""

    acknowledge: bool
    outcome: str


@dataclass(frozen=True, slots=True)
class _Acknowledged:
    outcome: str


@dataclass(frozen=True, slots=True)
class _RetryableUnit:
    reason: str


@dataclass(frozen=True, slots=True)
class _UnitAuthority:
    """The §4 strong-read snapshot the writer classified before claim."""

    owner: SuccessorOwner
    intent: IntentMeta
    plan_meta: PlanMeta
    plan_meta_item: Mapping[str, object]
    plan_head: PlanHeadObservation
    unit_item: Mapping[str, object]
    unit_entry: CanonicalUnitEntry
    unit_head: Stage1LifecycleHeadTuple | None


type _Classification = _UnitAuthority | _Acknowledged | _RetryableUnit


class _StatementFailedError(Exception):
    """One statement reached a closed failure disposition (§8)."""

    def __init__(self, reason: UnitFailureReason, description: str) -> None:
        super().__init__(description)
        self.reason = reason


class _AttemptBudgetExhaustedError(Exception):
    """Queued Begin would request plan attempt 33; no Athena call may occur (§5)."""


class _ChecksumMismatchError(Exception):
    """An immutable object diverged from its plan reference (§8: fail only the unit)."""


@dataclass(slots=True)
class _UnitContext:
    """One receive's mutable unit-processing state while the lease is held."""

    runtime: _Runtime
    delivery: WriteUnitDelivery
    authority: _UnitAuthority
    identity: Stage1IdentityResult
    head: Stage1LifecycleHeadTuple
    lease: TableLeaseRecord | None
    remaining_budget_ms: Callable[[], int]
    clock_started_at: datetime | None = None
    mode_guard: AuthorityModeGuard | None = None
    """V-13(a): set only when this batch's strong mode read returned LEGACY_DRAINING."""

    @property
    def table_id(self) -> str:
        return self.delivery.unit_key

    @property
    def plan_key(self) -> str:
        return self.delivery.plan_key


def _invariant(description: str, **context: object) -> InvariantError:
    return InvariantError(_PROJECTION_INVALID, description, context=dict(context))


def _report(  # noqa: PLR0913, PLR0917 - one private reporting seam
    delivery: WriteUnitDelivery,
    operation: str,
    outcome: str,
    severity: LogSeverity,
    **details: object,
) -> None:
    report_operation(
        OperationLog(
            operation=operation,
            component="table_writer",
            phase="unit",
            outcome=outcome,
            severity=severity,
            details={
                "internal_plan_key": delivery.plan_key,
                # The status plane's join keys (ADR-073 §14): this unit's flow and its plan's.
                "flow_id": derive_unit_identity(_unit_identity_request(delivery)).flow_id,
                "parent_flow_id": derive_plan_identity(
                    PlanIdentityRequest(
                        delivery.plan_digest, delivery.internal_delivery_execution_id
                    )
                ).flow_id,
                "unit_key": delivery.unit_key,
                "writer_execution_id": delivery.writer_execution_id,
                "receive_count": delivery.receive_count,
                **details,
            },
        ),
        LOGGER,
    )


UNIT_RECEIVES_METRIC: Final = "SemanticUnitReceives"
METRICS_NAMESPACE: Final = "Adlytics"
"""`single_metric` opens its own EMF blob and takes its own namespace (as in `load.py`)."""
UNIT_RECEIVE_OUTCOMES: Final = frozenset(
    {
        # acknowledged
        "attested",
        "attested_replay",
        "attempt_budget_successor_created",
        "plan_replaced",
        "intent_replaced",
        "stale_generation",
        "stale_plan_authority",
        "plan_completed",
        "plan_superseded",
        "plan_admission_failed",
        # S3 graph authority (V-9, V-12, V-23)
        "execution_superseded",
        "graph_awaiting_provider_terminal",
        "graph_already_settled",
        "legacy_replay_acknowledged",
        # S6 §2.5 graph units and envelopes
        "graph_unit_succeeded",
        "graph_unit_failed",
        "graph_claim_lost",
        "graph_unit_retryable",
        "graph_redecide_exhausted",
        "graph_envelope_invalid",
        "graph_publish_succeeded",
        "graph_publish_retryable",
        "graph_publish_invariant",
        # slice 7 (1b, M3): a returned member, a stale member, and the per-invocation outcome
        "graph_unit_returned",
        "graph_batch_member_stale",
        "graph_unit_batch_settled",
        # retried
        # Rolling publish (D7): a bounded flip or settle frontier race released the claim.
        "frontier_conflict",
        "graph_unit_claim_refused_live",
        "graph_unit_batch_unresolved",
        "graph_claim_live",
        "graph_dispatch_not_active",
        "legacy_envelope_after_cutover",
        "admission_incomplete",
        "attempt_parked",
        "foreign_attempt_pending",
        "insufficient_budget",
        "owner_unreadable",
        "plan_head_unreadable",
        "plan_meta_unreadable",
        "prior_writer_live",
        "refused_table_attempt_pending",
        "refused_table_busy",
        "unit_unreadable",
        "statement_retryable",
        "unit_failed",
        # an unclassified exception
        "raised",
    }
)
"""The closed `Outcome` dimension: every `_UnitResult.outcome` the writer returns."""


def unit_receive_dimensions(outcome: str, receive_count: int) -> dict[str, str]:
    """Return the closed dimensions of one `SemanticUnitReceives` count.

    An attested replay carries its plan-terminal suffix, which is folded into one value; any
    outcome outside the closed vocabulary counts as `unclassified`, never as itself.
    """
    folded = "attested_replay" if outcome.startswith("attested_replay") else outcome
    final = receive_count >= WRITER_SETTINGS.semantic_write_max_receive_count
    return {
        "Outcome": folded if folded in UNIT_RECEIVE_OUTCOMES else "unclassified",
        "FinalReceive": "true" if final else "false",
    }


def _count_receive(outcome: str, receive_count: int) -> None:
    with single_metric(
        name=UNIT_RECEIVES_METRIC, unit=MetricUnit.Count, value=1, namespace=METRICS_NAMESPACE
    ) as metric:
        for name, value in unit_receive_dimensions(outcome, receive_count).items():
            metric.add_dimension(name=name, value=value)


# --- §4 strong-read classification ----------------------------------------------------------------


def _classify_authority(  # noqa: PLR0911, PLR0912 - one typed return per closed §4 classification
    runtime: _Runtime, delivery: WriteUnitDelivery
) -> _Classification:
    """Strongly read owner/intent/plan/unit/HEAD and apply the §4 classification."""
    owner = runtime.reader.owner(delivery.plan_digest)
    if owner is None:
        return _RetryableUnit("owner_unreadable")
    if owner.active_plan_key != delivery.plan_key:
        return _Acknowledged("plan_replaced")
    if owner.active_intent_id != delivery.origin_intent_id:
        return _Acknowledged("intent_replaced")
    intent = runtime.reader.intent(delivery.plan_digest, delivery.origin_intent_id)
    if intent is None or intent.state is not IntentState.ADMITTED:
        return _RetryableUnit("admission_incomplete")
    if intent.claimed_generation != delivery.generation:
        return _Acknowledged("stale_generation")
    plan_meta = runtime.reader.plan_meta(delivery.plan_key)
    if plan_meta is None:
        return _RetryableUnit("plan_meta_unreadable")
    if plan_meta.status is not PlanStatus.PLANNED:
        return _Acknowledged(f"plan_{plan_meta.status.value}")
    if (
        plan_meta.generation != delivery.generation
        or plan_meta.origin_intent_id != delivery.origin_intent_id
        or plan_meta.plan_digest != delivery.plan_digest
    ):
        return _Acknowledged("stale_plan_authority")
    if plan_meta.admitted_count != plan_meta.unit_count:
        return _RetryableUnit("admission_incomplete")
    plan_meta_item = runtime.reader.plan_meta_item(delivery.plan_key)
    unit_item = runtime.reader.unit_item(delivery.plan_key, delivery.unit_key)
    if plan_meta_item is None or unit_item is None:
        return _RetryableUnit("unit_unreadable")
    if unit_item.get("admission") != "admitted":
        # §4: admission is never inferred from message presence; the unit's own
        # durable admission status must prove it beside the plan counters.
        return _RetryableUnit("admission_incomplete")
    unit_entry = unit_entry_from_item(unit_item)
    if unit_entry.unit_key != delivery.unit_key:
        raise _invariant("The retained unit roster names another physical table.")
    plan_flow = derive_plan_identity(
        PlanIdentityRequest(delivery.plan_digest, delivery.internal_delivery_execution_id)
    ).flow_id
    plan_head = runtime.reader.head(plan_flow)
    if plan_head is None:
        return _RetryableUnit("plan_head_unreadable")
    unit_flow = unit_item.get("flow_id")
    if not isinstance(unit_flow, str) or not unit_flow.strip():
        raise _invariant("The retained unit carries no persisted public flow identity.")
    return _UnitAuthority(
        owner=owner,
        intent=intent,
        plan_meta=plan_meta,
        plan_meta_item=plan_meta_item,
        plan_head=PlanHeadObservation(f"STATUS_FLOW#{plan_flow}", "HEAD", plan_head),
        unit_item=unit_item,
        unit_entry=unit_entry,
        unit_head=runtime.reader.head(unit_flow),
    )


# --- Lifecycle proofs -----------------------------------------------------------------------------


def _unit_identity_request(delivery: WriteUnitDelivery) -> UnitIdentityRequest:
    return UnitIdentityRequest(
        delivery.plan_digest, delivery.unit_key, delivery.writer_execution_id
    )


def _claim_proof(  # noqa: PLR0913, PLR0917 - one private proof constructor
    authority: _UnitAuthority,
    delivery: WriteUnitDelivery,
    now: datetime,
    clock_started_at: datetime | None,
) -> Stage1LifecycleProof:
    head = authority.unit_head
    if head is None:
        topology = Stage1TopologyRequest(
            Stage1OwnerTransition.FIRST_UNIT_STARTED,
            PlanAdmissionSource(
                plan_flow_id=authority.plan_head.head.flow_id,
                admitted_event_id=authority.plan_head.head.latest_event_id,
                plan_meta_authority=dict(authority.plan_meta_item),
                complete_plan_head_authority=stage1_head_authority_document(
                    authority.plan_head.head
                ),
                admitted_event_authority=[
                    authority.plan_head.head.latest_event_id,
                    authority.plan_head.head.latest_authority_digest,
                ],
            ),
        )
        prior = None
    else:
        topology = Stage1TopologyRequest(
            Stage1OwnerTransition.LATER_UNIT_EVENT,
            PriorOwnerEventSource(
                head.parent_flow_id, head.latest_event_id, stage1_head_authority_document(head)
            ),
        )
        prior = head
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=_unit_identity_request(delivery),
            internal_key=(delivery.plan_digest, delivery.unit_key),
            topology=topology,
            transition=Stage1LifecycleTransition(
                "semantic_unit_started", FlowState.RUNNING, None, None
            ),
            stage="projection_unit_claimed",
            authority_at=now,
            prior_head=prior,
            clock_started_at=clock_started_at,
            owner_generation=delivery.generation,
        )
    )


def _unit_event_proof(  # noqa: PLR0913, PLR0917 - one private proof constructor
    delivery: WriteUnitDelivery,
    head: Stage1LifecycleHeadTuple,
    transition_id: str,
    state: FlowState,
    outcome: CompletionOutcome | None,
    typed_reason: str | None,
    now: datetime,
    clock_started_at: datetime,
) -> Stage1LifecycleProof:
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=_unit_identity_request(delivery),
            internal_key=(delivery.plan_digest, delivery.unit_key),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.LATER_UNIT_EVENT,
                PriorOwnerEventSource(
                    head.parent_flow_id,
                    head.latest_event_id,
                    stage1_head_authority_document(head),
                ),
            ),
            transition=Stage1LifecycleTransition(transition_id, state, outcome, typed_reason),
            stage="projection_unit_disposition",
            authority_at=now,
            prior_head=head,
            clock_started_at=clock_started_at,
            owner_generation=delivery.generation,
        )
    )


def _pointer_proof(  # noqa: PLR0913, PLR0917 - one private proof constructor
    delivery: WriteUnitDelivery,
    partition_identity: str,
    publication_id: str,
    fencing_token: int,
    attested: Stage1LifecycleProof,
    now: datetime,
) -> Stage1LifecycleProof:
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=PointerIdentityRequest(
                partition_identity,
                publication_id,
                fencing_token,
                PointerExecutionSource(
                    PointerExecutionSourceKind.QUEUED_UNIT_WRITER_EXECUTION,
                    delivery.writer_execution_id,
                ),
            ),
            internal_key=(partition_identity, publication_id, fencing_token),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.QUEUED_NONZERO_POINTER,
                SameTransactionTerminalSource(attested.flow_id, attested.event_id),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_projection_servable",
                FlowState.COMPLETED,
                CompletionOutcome.SUCCEEDED,
                None,
            ),
            stage="projection_servable",
            authority_at=now,
            prior_head=None,
            publication_partition_key=partition_identity,
        )
    )


def _plan_terminal_proof(  # noqa: PLR0913, PLR0917 - one private proof constructor
    delivery: WriteUnitDelivery,
    plan_meta: PlanMeta,
    plan_head: Stage1LifecycleHeadTuple,
    now: datetime,
    clock_started_at: datetime,
) -> Stage1LifecycleProof:
    expectations = PlanRequiredChildExpectations(
        parent_flow_id=plan_head.flow_id,
        required_child_expectations=tuple(
            StatusChildExpectation(member.child_flow_id, member.owner_generation)
            for member in plan_meta.required_child_expectations
        ),
    )
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=PlanIdentityRequest(
                delivery.plan_digest, delivery.internal_delivery_execution_id
            ),
            internal_key=(delivery.plan_key,),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.LATER_PLAN_EVENT,
                PriorOwnerEventSource(
                    plan_head.parent_flow_id,
                    plan_head.latest_event_id,
                    stage1_head_authority_document(plan_head),
                ),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_plan_terminal", FlowState.COMPLETED, CompletionOutcome.SUCCEEDED, None
            ),
            stage="projection_plan_terminal",
            authority_at=now,
            prior_head=plan_head,
            clock_started_at=clock_started_at,
            required_child_expectations=expectations,
        )
    )


# --- §2.2 step 3: workload assembly from the immutable authority ----------------------------------


@dataclass(frozen=True, slots=True)
class _ServableIdentity:
    partition_key: str
    publication_id: str
    fencing_token: int


@dataclass(frozen=True, slots=True)
class _ShapedUnit:
    workload: UnitWorkload
    content_checksums: tuple[str, ...]
    servable: tuple[_ServableIdentity, ...]


def _expected_database(target: AthenaSemanticTarget, table_kind: str) -> str:
    if table_kind == "source_aligned":
        return target.source_database
    if table_kind == "candidate":
        return target.candidate_database
    if table_kind == "current":
        return target.current_database
    raise _invariant("Write unit table kind is outside the closed vocabulary.", kind=table_kind)


def _canonical_reference(reference: str) -> tuple[str, str, str]:
    parts = reference.split("/")
    if len(parts) != _CANONICAL_KEY_PARTS or parts[:2] != ["immutable", "canonical"]:
        raise _invariant("Canonical projection reference has an invalid immutable key.")
    destination, dataset, filename = parts[2:]
    if not destination or not dataset or not filename.endswith(".json"):
        raise _invariant("Canonical projection reference has an invalid immutable key.")
    publication_id = filename.removesuffix(".json")
    if not publication_id:
        raise _invariant("Canonical projection reference has no publication identity.")
    return destination, dataset, publication_id


def _require_checksum_match(entry: CanonicalObjectEntry, content_checksum: str) -> None:
    """Compare digest bytes only: the publication renders ``sha256:<hex>``, the roster the hex.

    A malformed publication checksum can never match the roster digest, so it is
    the same §8 unit-only ``CHECKSUM_MISMATCH`` disposition, never repaired from Iceberg.
    """
    try:
        digest = canonical_checksum_digest(content_checksum)
    except ValueError as malformed:
        raise _ChecksumMismatchError(entry.reference) from malformed
    if digest != entry.content_checksum:
        raise _ChecksumMismatchError(entry.reference)


def _decode_source_object(
    runtime: _Runtime, entry: CanonicalObjectEntry
) -> tuple[V2Table, str, tuple[PhysicalRecord, ...]]:
    publication = projection_source_publication(
        runtime.objects.read(source_object_key(entry.reference))
    )
    if publication.reference.value != entry.reference:
        raise _invariant("Stored source projection identity differs from its plan reference.")
    _require_checksum_match(entry, publication.content_checksum)
    table = runtime.catalog.source_for_interpreter(publication.rowset.interpreter.value)
    return table, publication.reference.value, source_records(publication, table)


def _decode_canonical_object(
    runtime: _Runtime, table_kind: str, entry: CanonicalObjectEntry
) -> tuple[V2Table, str, tuple[PhysicalRecord, ...], _ServableIdentity | None]:
    key_destination, dataset, key_publication = _canonical_reference(entry.reference)
    source = projection_unit_source(runtime.objects.read(entry.reference))
    expected_destination = (
        CanonicalDestination.SUCCESSFUL
        if table_kind == "current"
        else CanonicalDestination.CANDIDATE
    )
    _require_checksum_match(entry, source.content_checksum)
    if (
        key_destination != expected_destination.value
        or source.destination != expected_destination.value
        or source.publication_id != key_publication
        or source.publication_id != entry.publication_id
    ):
        raise _invariant(
            "Stored canonical identity or destination differs from its plan reference.",
            reference=entry.reference,
            table_kind=table_kind,
        )
    servable: _ServableIdentity | None = None
    if table_kind == "current":
        if (
            source.partition_key != entry.partition_key
            or source.fencing_token != entry.fencing_token
        ):
            raise _invariant(
                "Stored current publication partition or fence differs from its plan reference.",
                reference=entry.reference,
            )
        servable = _ServableIdentity(
            source.partition_key, source.publication_id, source.fencing_token
        )
    table = runtime.catalog.canonical(dataset, successful=table_kind == "current")
    return table, source.publication_id, canonical_unit_records(source, table), servable


def _shape_unit(runtime: _Runtime, entry: CanonicalUnitEntry) -> _ShapedUnit:  # noqa: PLR0912
    """Fetch and verify every immutable object and decode the unit workload (§2.2 step 3)."""
    database, separator, table_name = entry.unit_key.partition(".")
    if not separator or database != _expected_database(runtime.target, entry.table_kind):
        raise _invariant(
            "Write unit database differs from its retained table kind.",
            unit_key=entry.unit_key,
            table_kind=entry.table_kind,
        )
    records_by_identity: list[tuple[str, tuple[PhysicalRecord, ...]]] = []
    seen: dict[str, tuple[PhysicalRecord, ...]] = {}
    checksums: list[str] = []
    servable: list[_ServableIdentity] = []
    selected_table: V2Table | None = None
    try:
        for reference in entry.objects:
            if reference.kind == "source":
                if entry.table_kind != "source_aligned":
                    raise _invariant(
                        "Source projection reference differs from its retained table kind."
                    )
                table, identity, records = _decode_source_object(runtime, reference)
                advance = None
            else:
                if entry.table_kind == "source_aligned":
                    raise _invariant(
                        "Canonical projection reference differs from its retained table kind."
                    )
                table, identity, records, advance = _decode_canonical_object(
                    runtime, entry.table_kind, reference
                )
            if table.name != table_name:
                raise _invariant(
                    "Immutable projection object resolves to a different generated table.",
                    unit_key=entry.unit_key,
                    logical_table=table.name,
                )
            if selected_table is not None and selected_table != table:
                raise _invariant("Write unit objects resolve to conflicting generated schemas.")
            selected_table = table
            prior = seen.get(identity)
            if prior is not None:
                qualifier = "duplicate" if prior == records else "conflicting"
                raise _invariant(
                    f"Write unit contains {qualifier} immutable publication identities.",
                    publication_id=identity,
                )
            seen[identity] = records
            records_by_identity.append((identity, records))
            checksums.append(reference.content_checksum)
            if advance is not None:
                servable.append(advance)
    except AdlyticsError, _ChecksumMismatchError:
        raise
    except Exception as error:
        raise _invariant(
            "Immutable projection authority is malformed.",
            cause=type(error).__name__,
            unit_key=entry.unit_key,
        ) from error
    if selected_table is None:
        raise _invariant("Write unit has no generated physical table.")
    return _ShapedUnit(
        workload=UnitWorkload(
            table=selected_table,
            database=database,
            records_by_identity=tuple(records_by_identity),
        ),
        content_checksums=tuple(checksums),
        servable=tuple(servable),
    )


# --- §5 statement protocol ------------------------------------------------------------------------


def _fresh_unit_item(ctx: _UnitContext) -> dict[str, object]:
    unit_item = ctx.runtime.reader.unit_item(ctx.plan_key, ctx.delivery.unit_key)
    if unit_item is None:
        raise _invariant("The claimed unit disappeared during processing.")
    return unit_item


def _fresh_meta_item(ctx: _UnitContext) -> dict[str, object]:
    meta_item = ctx.runtime.reader.plan_meta_item(ctx.plan_key)
    if meta_item is None:
        raise _invariant("The plan META disappeared during processing.")
    return meta_item


def _snapshot_count(item: Mapping[str, object], name: str) -> int:
    value = item.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invariant(f"stored counter {name} is not an integer.")
    return value


def _accepted_attempts(ctx: _UnitContext) -> int | None:
    """Read the attested unit's accepted reservations once, for telemetry only (§4.8).

    The unit is already finalized and its lease released, so a failed read is reported once
    and the attested line carries ``None``; it never turns a finalized unit into a retry.
    """
    try:
        return _snapshot_count(_fresh_unit_item(ctx), "unit_reservation_count")
    except Exception as error:
        report_error(
            error,
            Report(operation="semantic_unit_attested", correlation_id=ctx.plan_key, logger=LOGGER),
        )
        return None


def _required_lease(ctx: _UnitContext) -> TableLeaseRecord:
    if ctx.lease is None:
        raise _invariant("Statement processing requires the held table lease.")
    return ctx.lease


class _QueuedStatementOwner:
    """The queued unit's §5 owner seam: fresh snapshots, cap preflight, and reporting."""

    __slots__ = ("_ctx",)

    def __init__(self, ctx: _UnitContext) -> None:
        self._ctx = ctx

    def begin_owner(self) -> BeginQueuedUnitOwner:
        """Re-read the owner, apply the queued caps, and shape the Begin owner."""
        ctx = self._ctx
        unit_item = _fresh_unit_item(ctx)
        meta_item = _fresh_meta_item(ctx)
        plan_count = _snapshot_count(meta_item, "plan_reservation_count")
        unit_count = _snapshot_count(unit_item, "unit_reservation_count")
        if plan_count >= WRITER_SETTINGS.max_accepted_attempts_per_plan:
            raise _AttemptBudgetExhaustedError
        if unit_count >= WRITER_SETTINGS.max_accepted_attempts_per_unit:
            raise _StatementFailedError(
                UnitFailureReason.STATEMENT_RETRIES_EXHAUSTED,
                "the unit reached its permanent 10-reservation cap.",
            )
        return BeginQueuedUnitOwner(
            QueuedUnitAttemptOwner(ctx.plan_key, ctx.table_id, unit_item), meta_item
        )

    def attempt_owner(self) -> QueuedUnitAttemptOwner:
        """Re-read the exact unit snapshot completion and cancellation condition on."""
        ctx = self._ctx
        return QueuedUnitAttemptOwner(ctx.plan_key, ctx.table_id, _fresh_unit_item(ctx))

    def report(
        self, operation: str, outcome: str, severity: LogSeverity, **details: object
    ) -> None:
        """Emit through the writer's one private reporting seam."""
        _report(self._ctx.delivery, operation, outcome, severity, **details)


def _statement_run(ctx: _UnitContext) -> StatementRun:
    runtime = ctx.runtime
    return StatementRun(
        runtime=StatementProtocolRuntime(
            coordinator=runtime.coordinator,
            attempts=runtime.attempts,
            start=runtime.start,
            terminals=runtime.terminals,
            reads=runtime.reader,
            stager=runtime.stager,
            wall=runtime.wall,
            monotonic_ms=runtime.monotonic_ms,
            sleep_ms=runtime.sleep_ms,
        ),
        owner=_QueuedStatementOwner(ctx),
        physical_table_id=ctx.table_id,
        lease=_required_lease(ctx),
        writer_execution_id=ctx.delivery.writer_execution_id,
        bounds=StatementStartBounds(
            max_statement_bytes=WRITER_SETTINGS.athena_max_statement_bytes,
            max_encoded_identity_bytes=WRITER_SETTINGS.max_encoded_identity_bytes,
        ),
        remaining_budget_ms=ctx.remaining_budget_ms,
    )


def _run_statement(ctx: _UnitContext, statement: ComposedStatement) -> StatementResult:
    """Drive one composed statement through the shared §5 loop as the queued owner."""
    try:
        return run_statement(_statement_run(ctx), statement)
    except LocalStatementRefusedError as refused:
        raise _StatementFailedError(
            UnitFailureReason.STATEMENT_RETRIES_EXHAUSTED, str(refused)
        ) from refused
    except StagingUnavailableError as unavailable:
        # ADR-149 §10: raised before Begin, so nothing is reserved; the unit retries like an
        # Athena outage while receives remain, then fails STATEMENT_RETRIES_EXHAUSTED.
        raise _StatementFailedError(
            UnitFailureReason.STATEMENT_RETRIES_EXHAUSTED, "staging unavailable"
        ) from unavailable


# --- Refusals, settlement, dispositions, finalization ---------------------------------------------


def _record_refusal(
    ctx: _UnitContext, reason: Stage1RefusalReason, target_micros: int | None
) -> None:
    """Record one §5 retryable refusal, then apply the one visibility override."""
    runtime = ctx.runtime
    now = runtime.wall()
    proof = _unit_event_proof(
        ctx.delivery,
        ctx.head,
        "semantic_unit_refused",
        FlowState.RETRYING,
        None,
        reason.value.lower(),
        now,
        _unit_flow_clock(ctx),
    )
    runtime.coordinator.record_stage1_refusal(
        RecordStage1RefusalRequest(
            tag=Stage1RefusalTag.UNIT,
            reason=reason,
            refusal_instant=instant(now),
            proof=proof,
            unit=RefusedUnitObservation(
                plan_digest=ctx.delivery.plan_digest,
                plan_key=ctx.plan_key,
                intent=ctx.authority.intent,
                owner=ctx.authority.owner,
                writer_execution_id=ctx.delivery.writer_execution_id,
                unit_item=_fresh_unit_item(ctx),
                meta_item=ctx.authority.plan_meta_item,
            ),
        )
    )
    resulting = proof.resulting_head
    if resulting is not None:
        ctx.head = resulting
    seconds = refusal_visibility_seconds(target_micros, epoch_micros(now))
    _report(
        ctx.delivery,
        "semantic_unit_refused",
        reason.value,
        LogSeverity.WARNING,
        visibility_timeout_seconds=seconds,
    )
    runtime.visibility.change(ctx.delivery.receipt_handle, seconds)


def _pending_target_micros(ctx: _UnitContext, fence: FenceObservation) -> int | None:
    """Derive the §5 TABLE_ATTEMPT_PENDING visibility target from the observed fence."""
    reconciliation = fence.reconciliation
    if reconciliation.reconcile_status == "blocked":
        return None
    due: int | None = None
    key = reconciliation.reconcile_after_or_block
    if isinstance(key, str) and len(key) >= _INSTANT_DIGITS and key[:_INSTANT_DIGITS].isdecimal():
        due = int(key[:_INSTANT_DIGITS])
    if fence.state == "parked":
        return due
    lease = ctx.runtime.reader.lease(ctx.table_id)
    expiry = lease_expiry_epoch_micros(lease.expires_at) if lease is not None else None
    candidates = [value for value in (due, expiry) if value is not None]
    return max(candidates) if candidates else None


def _settlement_expansion(
    ctx: _UnitContext, disposition: StatusOwnerDisposition, observed_at: datetime
) -> Stage1SettlementExpansion:
    """Expand this unit's unsettled accepted provider-terminal attempts for settlement."""
    unit_item = _fresh_unit_item(ctx)
    references = unit_item.get("attempt_references")
    attempt_ids = (
        [value for value in cast("list[object]", references) if isinstance(value, str)]
        if isinstance(references, list)
        else []
    )
    members: list[AthenaAttemptSettlement] = []
    for attempt_id in attempt_ids:
        observation = ctx.runtime.reader.attempt(ctx.table_id, attempt_id)
        if observation is None:
            raise _invariant("A referenced table attempt is unreadable.", attempt_id=attempt_id)
        if observation.athena_usage_state != "unsettled":
            continue
        state = observation.state
        if state is None:
            raise _invariant("A referenced table attempt has no terminal state.")
        members.append(
            AthenaAttemptSettlement(
                attempt=observation,
                settlement=SettleStage1AthenaUsageRequest(
                    attempt_id=attempt_id,
                    flow_id=ctx.identity.flow_id,
                    execution_id=ctx.identity.execution_id,
                    evaluation_binding=EvaluationBinding(MEASUREMENT_REFS[_UNIT_MEASUREMENT]),
                    owner_disposition=disposition,
                    observed_at=observed_at,
                    provider_terminal_state=StatusProviderTerminalState(state),
                    scanned_bytes=observation.scanned_bytes,
                    engine_ms=observation.engine_ms,
                ),
            )
        )
    return Stage1SettlementExpansion(owner_disposition=disposition, attempts=tuple(members))


def _prior_settled_attempts(
    ctx: _UnitContext, plan_meta: PlanMeta
) -> tuple[SettledAttemptReference, ...]:
    """Reconstruct the plan's complete already-settled attempt references."""
    if plan_meta.settled_attempt_count == 0:
        return ()
    references: list[SettledAttemptReference] = []
    for item in ctx.runtime.reader.plan_unit_items(ctx.plan_key):
        raw = item.get("attempt_references")
        if not isinstance(raw, list):
            continue
        table_id = str(item.get("unit_key", ""))
        for attempt_id in cast("list[object]", raw):
            if not isinstance(attempt_id, str):
                continue
            observation = ctx.runtime.reader.attempt(table_id, attempt_id)
            if observation is None or observation.athena_usage_state != "settled":
                continue
            digest = _usage_authority_digest(ctx, table_id, attempt_id)
            references.append(
                SettledAttemptReference(
                    physical_table_id=table_id,
                    attempt_id=attempt_id,
                    usage_authority_digest=digest,
                )
            )
    ordered = tuple(
        sorted(
            references,
            key=lambda ref: (ref.physical_table_id.encode("utf-8"), ref.attempt_id.encode("utf-8")),
        )
    )
    if len(ordered) != plan_meta.settled_attempt_count:
        raise _invariant(
            "Settled-attempt reconstruction differs from the plan's settled count.",
            reconstructed=len(ordered),
            expected=plan_meta.settled_attempt_count,
        )
    return ordered


def _usage_authority_digest(ctx: _UnitContext, table_id: str, attempt_id: str) -> str:
    item = ctx.runtime.reader.attempt_item(table_id, attempt_id)
    if item is None:
        raise _invariant("A settled table attempt is unreadable.", attempt_id=attempt_id)
    digest = item.get("usage_authority_digest")
    if not isinstance(digest, str) or not digest.strip():
        raise _invariant("A settled attempt carries no usage authority digest.")
    return digest


def _fail_unit(ctx: _UnitContext, reason: UnitFailureReason) -> None:
    """Record the closed §8 ordinary unit failure disposition atomically."""
    runtime = ctx.runtime
    now = runtime.wall()
    proof = _unit_event_proof(
        ctx.delivery,
        ctx.head,
        "semantic_unit_failed",
        FlowState.RETRYING,
        None,
        reason.value,
        now,
        _unit_flow_clock(ctx),
    )
    runtime.coordinator.fail_unit(
        FailUnitRequest(
            plan_digest=ctx.delivery.plan_digest,
            plan_key=ctx.plan_key,
            physical_table_id=ctx.table_id,
            writer_execution_id=ctx.delivery.writer_execution_id,
            owner=ctx.authority.owner,
            plan_meta=_required_plan_meta(ctx),
            lease=_required_lease(ctx),
            observed_fence=runtime.reader.fence(ctx.table_id),
            reason=reason,
            settlement=_settlement_expansion(ctx, StatusOwnerDisposition.RETRYABLE_FAILURE, now),
            prior_settled_attempts=_prior_settled_attempts(ctx, _required_plan_meta(ctx)),
            operation_instant=instant(now),
            proof=proof,
            unit_item=_fresh_unit_item(ctx),
        )
    )
    _report(ctx.delivery, "semantic_unit_failed", reason.value, LogSeverity.WARNING)


def _fail_unit_attempt_budget(ctx: _UnitContext) -> str | None:
    """Record the attempt-33 disposition: terminalize the plan and create the successor."""
    runtime = ctx.runtime
    now = runtime.wall()
    plan_meta = _required_plan_meta(ctx)
    successor_units = tuple(
        unit_entry_from_item(item) for item in runtime.reader.plan_unit_items(ctx.plan_key)
    )
    proof = _unit_event_proof(
        ctx.delivery,
        ctx.head,
        "semantic_unit_failed",
        FlowState.RETRYING,
        None,
        UnitFailureReason.PLAN_ATTEMPT_BUDGET_EXHAUSTED.value,
        now,
        _unit_flow_clock(ctx),
    )
    result = runtime.coordinator.fail_unit(
        FailUnitRequest(
            plan_digest=ctx.delivery.plan_digest,
            plan_key=ctx.plan_key,
            physical_table_id=ctx.table_id,
            writer_execution_id=ctx.delivery.writer_execution_id,
            owner=ctx.authority.owner,
            plan_meta=plan_meta,
            lease=_required_lease(ctx),
            observed_fence=runtime.reader.fence(ctx.table_id),
            reason=UnitFailureReason.PLAN_ATTEMPT_BUDGET_EXHAUSTED,
            settlement=_settlement_expansion(
                ctx, StatusOwnerDisposition.EXHAUSTED_OR_UNAVAILABLE, now
            ),
            prior_settled_attempts=_prior_settled_attempts(ctx, plan_meta),
            operation_instant=instant(now),
            proof=proof,
            unit_item=None,
            budget_exhaustion=AttemptBudgetExhaustion(
                successor_units=successor_units,
                permanent_reservation_count=WRITER_SETTINGS.max_accepted_attempts_per_plan,
                rejected_ordinal=WRITER_SETTINGS.max_accepted_attempts_per_plan + 1,
            ),
        )
    )
    _report(
        ctx.delivery,
        "semantic_unit_failed",
        UnitFailureReason.PLAN_ATTEMPT_BUDGET_EXHAUSTED.value,
        LogSeverity.WARNING,
        successor_intent_id=result.successor_intent_id or "",
    )
    return result.successor_intent_id


def _required_plan_meta(ctx: _UnitContext) -> PlanMeta:
    plan_meta = ctx.runtime.reader.plan_meta(ctx.plan_key)
    if plan_meta is None:
        raise _invariant("The plan META disappeared during processing.")
    return plan_meta


def _pointer_outcomes(  # noqa: PLR0913, PLR0917 - one private pointer resolver
    ctx: _UnitContext, shaped: _ShapedUnit, attested: Stage1LifecycleProof, now: datetime
) -> tuple[CurrentPointerOutcome, ...]:
    """Pre-resolve every named current pointer while the lease remains held (§2.2 step 6)."""
    outcomes: list[CurrentPointerOutcome] = []
    for member in shaped.servable:
        current = ctx.runtime.reader.current_item(member.partition_key)
        if current is None or current.get("publication_id") is None:
            raise _invariant(
                "A named current pointer has no authority publication.",
                partition=member.partition_key,
            )
        projected = current.get("projected_fencing_token")
        superseded = (
            isinstance(projected, int)
            and not isinstance(projected, bool)
            and projected >= member.fencing_token
            and current.get("projected_publication_id") not in (None, "NONE")
        )
        proof = _pointer_proof(
            ctx.delivery,
            member.partition_key,
            member.publication_id,
            member.fencing_token,
            attested,
            now,
        )
        outcomes.append(
            CurrentPointerOutcome(
                kind=(
                    PointerOutcomeKind.PROJECTION_SUPERSEDED
                    if superseded
                    else PointerOutcomeKind.ADVANCED
                ),
                partition_identity=member.partition_key,
                projected_publication_id=member.publication_id,
                projected_fencing_token=member.fencing_token,
                proof=proof,
                observed_current=None if superseded else current,
            )
        )
    return tuple(outcomes)


def _finalize_unit(ctx: _UnitContext, shaped: _ShapedUnit) -> None:
    """Execute the one §2.2 step-7 finalization transaction, rebuilding on pointer races."""
    for _ in range(_POINTER_REBUILD_LIMIT):
        now = ctx.runtime.wall()
        attested = _unit_event_proof(
            ctx.delivery,
            ctx.head,
            "semantic_unit_attested",
            FlowState.COMPLETED,
            CompletionOutcome.SUCCEEDED,
            None,
            now,
            _unit_flow_clock(ctx),
        )
        request = FinalizeUnitRequest(
            plan_digest=ctx.delivery.plan_digest,
            plan_key=ctx.plan_key,
            physical_table_id=ctx.table_id,
            writer_execution_id=ctx.delivery.writer_execution_id,
            owner=ctx.authority.owner,
            plan_meta=_required_plan_meta(ctx),
            unit_item=_fresh_unit_item(ctx),
            lease=_required_lease(ctx),
            observed_fence=ctx.runtime.reader.fence(ctx.table_id),
            pointers=_pointer_outcomes(ctx, shaped, attested, now),
            settlement=_settlement_expansion(ctx, StatusOwnerDisposition.SUCCESSFUL, now),
            prior_settled_attempts=_prior_settled_attempts(ctx, _required_plan_meta(ctx)),
            verified_checksums=shaped.content_checksums,
            operation_instant=instant(now),
            proof=attested,
            mode_guard=ctx.mode_guard,
        )
        try:
            ctx.runtime.coordinator.finalize_unit(request)
        except GraphAuthorityRefusedError as refused:
            # V-13(a): the GRAPH CAS committed after this batch read LEGACY_DRAINING. Nothing
            # committed; the drain's F4 proof says this cannot happen, so it is an invariant.
            raise _invariant(
                "Legacy finalize lost its mode check.", signal=refused.signal
            ) from None
        except DomainError:
            # §2.2 step 6: a pointer conditional conflict rebuilds the whole
            # transaction after strong re-read; it never fails the unit.
            continue
        return
    raise DomainError(
        _PROJECTION_INVALID,
        "Unit finalization kept losing its conditions; the redelivery retries from claim.",
    )


@trace_boundary(TABLE_WRITER_PLAN_TERMINAL)
def _plan_terminal(runtime: _Runtime, delivery: WriteUnitDelivery) -> str:
    """Run the sole §2.2 step-9 conditional plan completion after re-reading authority."""
    plan_meta = runtime.reader.plan_meta(delivery.plan_key)
    owner = runtime.reader.owner(delivery.plan_digest)
    plan_flow = derive_plan_identity(
        PlanIdentityRequest(delivery.plan_digest, delivery.internal_delivery_execution_id)
    ).flow_id
    plan_head = runtime.reader.head(plan_flow)
    if plan_meta is None or owner is None or plan_head is None:
        return "authority_unreadable"
    with span(
        TABLE_WRITER_PLAN_TERMINAL,
        flow_id=plan_head.flow_id,
        execution_id=plan_head.current_execution_id,
        internal_plan_key=delivery.plan_key,
        stage="plan_terminal",
        completed=0,
        total=1,
    ) as terminal_span:
        if plan_meta.status is not PlanStatus.PLANNED:
            terminal_span.record(completed=1, outcome=plan_meta.status.value)
            return plan_meta.status.value
        complete = (
            plan_meta.attested_count == plan_meta.unit_count
            and plan_meta.admitted_count == plan_meta.unit_count
            and plan_meta.failed_count == 0
        )
        if not complete:
            terminal_span.record(completed=1, outcome="incomplete")
            return "incomplete"
        request = _complete_plan_request(runtime, delivery, plan_meta, owner, plan_head)
        try:
            runtime.coordinator.complete_plan(request)
        except DomainError:
            terminal_span.record(completed=1, outcome="conditional_noop")
            return "conditional_noop"
        terminal_span.record(completed=1, outcome="completed")
        return "completed"


def _complete_plan_request(  # noqa: PLR0913, PLR0917 - one private request assembler
    runtime: _Runtime,
    delivery: WriteUnitDelivery,
    plan_meta: PlanMeta,
    owner: SuccessorOwner,
    plan_head: Stage1LifecycleHeadTuple,
) -> CompletePlanRequest:
    units: list[AttestedUnitObservation] = []
    attempts: list[SettledAttemptObservation] = []
    for item in runtime.reader.plan_unit_items(delivery.plan_key):
        unit_key = str(item.get("unit_key", ""))
        flow_id = str(item.get("flow_id", ""))
        units.append(
            AttestedUnitObservation(
                unit_key=unit_key,
                flow_id=flow_id,
                owner_generation=plan_meta.generation,
                item=item,
            )
        )
        raw = item.get("attempt_references")
        if not isinstance(raw, list):
            continue
        for attempt_id in cast("list[object]", raw):
            if not isinstance(attempt_id, str):
                continue
            attempt_item = runtime.reader.attempt_item(unit_key, attempt_id)
            if attempt_item is None or attempt_item.get("athena_usage_state") != "settled":
                continue
            attempts.append(
                SettledAttemptObservation(
                    physical_table_id=unit_key,
                    attempt_id=attempt_id,
                    provider_state=str(attempt_item.get("state", "")),
                    usage_authority_digest=str(attempt_item.get("usage_authority_digest", "")),
                    item=attempt_item,
                )
            )
    return CompletePlanRequest(
        plan_digest=delivery.plan_digest,
        plan_key=delivery.plan_key,
        owner=owner,
        plan_meta=plan_meta,
        units=tuple(units),
        attempts=tuple(attempts),
        proof=_plan_terminal_proof(
            delivery,
            plan_meta,
            plan_head,
            runtime.wall(),
            runtime.reader.flow_root_clock(plan_head.flow_id),
        ),
        operation_instant=instant(runtime.wall()),
    )


# --- The unit lifecycle ---------------------------------------------------------------------------


def _step_one_claim(ctx: _UnitContext, status: object) -> _RetryableUnit | None:
    """Dispatch the §2.2 step-1 claim by the strongly read unit status.

    ``queued|failed`` claim under this receive; a foreign ``writing`` unit takes
    the §8 reclaim path; this receive's own ``writing`` unit is already claimed.
    """
    prior_writer = ctx.authority.unit_item.get("writer_execution_id")
    if status == "writing" and prior_writer != ctx.delivery.writer_execution_id:
        return _reclaim_foreign_writing_unit(ctx, prior_writer)
    if status in ("queued", "failed"):
        return _claim(ctx)
    return None


def _claim(
    ctx: _UnitContext, prior_writer_execution_id: str | None = None
) -> _RetryableUnit | None:
    """Run the §2.2 step-1 conditional claim and advance the tracked unit head.

    ``prior_writer_execution_id`` selects the §8 ``writing→writing`` reclaim of a
    dead writer's unit; a live prior holder returns the retryable
    ``prior_writer_live`` outcome after the one ``TABLE_BUSY``-shaped visibility
    override and records no lifecycle event.
    """
    now = ctx.runtime.wall()
    proof = _claim_proof(
        ctx.authority,
        ctx.delivery,
        now,
        None if ctx.authority.unit_head is None else _unit_flow_clock(ctx),
    )
    prior_holder = (
        None
        if prior_writer_execution_id is None
        else queued_lease_holder(ctx.plan_key, ctx.table_id, prior_writer_execution_id)
    )
    result = ctx.runtime.coordinator.claim_unit(
        ClaimUnitRequest(
            plan_digest=ctx.delivery.plan_digest,
            plan_key=ctx.plan_key,
            physical_table_id=ctx.table_id,
            intent=ctx.authority.intent,
            owner=ctx.authority.owner,
            plan_meta=ctx.authority.plan_meta,
            unit_item=ctx.authority.unit_item,
            writer_execution_id=ctx.delivery.writer_execution_id,
            operation_instant=instant(now),
            plan_head=ctx.authority.plan_head,
            proof=proof,
            prior_writer_execution_id=prior_writer_execution_id,
            prior_lease_holder=prior_holder,
        )
    )
    if result.outcome is ClaimUnitOutcome.PRIOR_WRITER_LIVE:
        target = (
            lease_expiry_epoch_micros(result.held_until) if result.held_until is not None else None
        )
        _defer_visibility(ctx, "prior_writer_live", target)
        return _RetryableUnit("prior_writer_live")
    resulting = proof.resulting_head
    if resulting is None:  # pragma: no cover - unit proofs always advance the head
        raise _invariant("The claim proof carries no resulting unit head.")
    ctx.head = resulting
    _report(ctx.delivery, "semantic_unit_started", "claimed", LogSeverity.DEBUG)
    return None


def _unit_flow_clock(ctx: _UnitContext) -> datetime:
    """Read back the unit flow's durable root clock once and repeat it on every successor."""
    if ctx.clock_started_at is None:
        ctx.clock_started_at = ctx.runtime.reader.flow_root_clock(ctx.identity.flow_id)
    return ctx.clock_started_at


def _defer_visibility(ctx: _UnitContext, reason: str, target_micros: int | None) -> None:
    """Apply the one §5 visibility override without a lifecycle event.

    Used where the design pins "retryable ... and no lifecycle event": a foreign
    ``writing`` unit still bound to a nonterminal attempt, or a live prior holder
    refusing a reclaim. The unit's durable claim state is another writer's, so no
    refusal event may be recorded against it.
    """
    seconds = refusal_visibility_seconds(target_micros, epoch_micros(ctx.runtime.wall()))
    _report(
        ctx.delivery,
        "semantic_unit_deferred",
        reason,
        LogSeverity.WARNING,
        visibility_timeout_seconds=seconds,
    )
    ctx.runtime.visibility.change(ctx.delivery.receipt_handle, seconds)


def _reclaim_foreign_writing_unit(ctx: _UnitContext, prior_writer: object) -> _RetryableUnit | None:
    """Run the §2.2 step-1 ``writing→writing`` reclaim of another writer's unit (§8).

    A unit still bound to a nonterminal attempt is retryable with the
    ``TABLE_ATTEMPT_PENDING`` visibility and no lifecycle event until the
    reconciler completes that attempt; otherwise the lease-conditioned reclaim
    decides between resuming and ``prior_writer_live``.
    """
    if not isinstance(prior_writer, str) or not prior_writer.strip():
        raise _invariant("A writing unit carries no durable prior writer execution identity.")
    if ctx.authority.unit_item.get("current_attempt_id") is not None:
        fence = ctx.runtime.reader.fence(ctx.table_id)
        target = _pending_target_micros(ctx, fence) if fence is not None else None
        _defer_visibility(ctx, "foreign_attempt_pending", target)
        return _RetryableUnit("foreign_attempt_pending")
    return _claim(ctx, prior_writer_execution_id=prior_writer)


def _acquire_lease(ctx: _UnitContext) -> _RetryableUnit | None:
    """Run the §2.2 step-2 fence check and lease acquisition; refusals stay retryable."""
    runtime = ctx.runtime
    fence = runtime.reader.fence(ctx.table_id)
    if fence is not None and fence.state in ("active", "parked"):
        _record_refusal(
            ctx,
            Stage1RefusalReason.TABLE_ATTEMPT_PENDING,
            _pending_target_micros(ctx, fence),
        )
        return _RetryableUnit("refused_table_attempt_pending")
    now = runtime.wall()
    holder = queued_lease_holder(ctx.plan_key, ctx.table_id, ctx.delivery.writer_execution_id)
    expires = (
        now.replace(microsecond=0) + timedelta(seconds=WRITER_SETTINGS.table_lease_duration_seconds)
    ).isoformat()
    observed_lease = runtime.reader.lease(ctx.table_id)
    result = runtime.coordinator.acquire_table_fence(
        AcquireTableFenceRequest(
            physical_table_id=ctx.table_id,
            holder=holder,
            operation_instant=instant(now),
            new_expires_at=expires,
            observed_lease=observed_lease,
            observed_fence=fence,
        )
    )
    if result.outcome is AcquireTableFenceOutcome.TABLE_BUSY:
        target = (
            lease_expiry_epoch_micros(result.held_until) if result.held_until is not None else None
        )
        _record_refusal(ctx, Stage1RefusalReason.TABLE_BUSY, target)
        return _RetryableUnit("refused_table_busy")
    if result.outcome is AcquireTableFenceOutcome.TABLE_ATTEMPT_PENDING:
        fresh = runtime.reader.fence(ctx.table_id)
        target = _pending_target_micros(ctx, fresh) if fresh is not None else None
        _record_refusal(ctx, Stage1RefusalReason.TABLE_ATTEMPT_PENDING, target)
        return _RetryableUnit("refused_table_attempt_pending")
    if result.fencing_token is None or result.expires_at is None:  # pragma: no cover
        raise _invariant("An acquired table fence carries its grant fields.")
    ctx.lease = TableLeaseRecord(
        physical_table_id=ctx.table_id,
        holder=holder,
        fencing_token=result.fencing_token,
        expires_at=result.expires_at,
    )
    return None


def _release_lease(ctx: _UnitContext) -> None:
    """Release the held lease against an absent-or-clear fence (§2.2 step 8)."""
    lease = ctx.lease
    if lease is None:
        return
    ctx.lease = None
    fence = ctx.runtime.reader.fence(ctx.table_id)
    if fence is not None and fence.state in ("active", "parked"):
        # Only provider-terminal completion clears the fence; leave the parked pair.
        return
    try:
        ctx.runtime.coordinator.release_table_fence(
            ReleaseTableFenceRequest(
                physical_table_id=ctx.table_id, lease=lease, observed_fence=fence
            )
        )
    except DomainError:
        # A lost release condition is availability-only: the lease reaches expiry.
        _report(ctx.delivery, "semantic_table_lease_release", "lost", LogSeverity.WARNING)


def _execute_statements(
    ctx: _UnitContext, composition: UnitStatementComposition
) -> _RetryableUnit | None:
    """Drive every composed statement through §5 and check the verification counts once."""
    for statement in composition.statements:
        result = _run_statement(ctx, statement)
        if isinstance(result, InsufficientStatementBudget):
            _report(
                ctx.delivery,
                "semantic_statement_budget",
                "insufficient",
                LogSeverity.WARNING,
                remaining_budget_ms=result.remaining_budget_ms,
            )
            return _RetryableUnit("insufficient_budget")
        if isinstance(result, StatementParked):
            ctx.lease = None  # parked: only the reconciler may clear the pair
            _report(
                ctx.delivery,
                "semantic_attempt_parked",
                "parked",
                LogSeverity.WARNING,
                attempt_id=result.attempt_id,
            )
            return _RetryableUnit("attempt_parked")
        if result.state is not ProviderTerminalState.SUCCEEDED:
            raise _StatementFailedError(
                UnitFailureReason.STATEMENT_RETRIES_EXHAUSTED,
                f"the provider terminal is {result.state.value}.",
            )
        if statement is composition.verification and not verification_holds(
            composition, ctx.runtime.terminals.readback_rows(result.query_execution_id)
        ):
            raise _StatementFailedError(
                UnitFailureReason.VERIFICATION_MISMATCH,
                "the verification counts differ from the staged unit.",
            )
    return None


def _statement_failure_disposition(
    ctx: _UnitContext, failure: _StatementFailedError
) -> _UnitResult:
    """Apply §8: retry while receives remain, else record the closed unit failure."""
    exhausted = ctx.delivery.receive_count >= WRITER_SETTINGS.semantic_write_max_receive_count
    if not exhausted and failure.reason is not UnitFailureReason.CHECKSUM_MISMATCH:
        _report(
            ctx.delivery,
            "semantic_statement_failed",
            failure.reason.value,
            LogSeverity.WARNING,
            retryable=True,
            description=str(failure),
        )
        return _UnitResult(acknowledge=False, outcome="statement_retryable")
    _fail_unit(ctx, failure.reason)
    return _UnitResult(acknowledge=False, outcome="unit_failed")


def _process_unit(
    runtime: _Runtime,
    delivery: WriteUnitDelivery,
    remaining_budget_ms: Callable[[], int],
) -> _UnitResult:
    """Run the §2.2 steps 1-9 lifecycle for one delivered unit and classify its receive."""
    gated = _mode_gate(runtime, delivery)
    if isinstance(gated, _UnitResult):
        return gated
    return _process_legacy_unit(runtime, delivery, (remaining_budget_ms, gated))


def _legacy_terminal(runtime: _Runtime, delivery: WriteUnitDelivery) -> bool:
    """V-23: the exact legacy unit already reached its terminal authority (attested)."""
    item = runtime.reader.unit_item(delivery.plan_key, delivery.unit_key)
    return item is not None and item.get("status") == "attested"


def _read_mode(runtime: _Runtime) -> AuthorityMode:
    """One strong mode read per batch; S6 (V-17b) turns drain counting on after a draining read."""
    graph = runtime.graph
    mode = AuthorityMode.LEGACY if graph is None else graph.read_mode().mode
    runtime.coordinator.count_drain(mode)
    return mode


def _refuse_by_mode(signal: str, correlation_id: str) -> _UnitResult:
    """Zero effect, left unacknowledged for the DLQ, and the invariant counted once."""
    report_error(
        _invariant("An envelope arrived that its mode does not accept.", signal=signal),
        Report(operation="semantic_table_writer", correlation_id=correlation_id, logger=LOGGER),
    )
    return _UnitResult(acknowledge=False, outcome=signal.lower())


def _mode_gate(
    runtime: _Runtime, delivery: WriteUnitDelivery
) -> _UnitResult | AuthorityModeGuard | None:
    """Apply the mode-by-envelope table to a legacy envelope (V-12, V-13).

    Returns a final result, or the finalize mode guard the legacy protocol carries (None
    outside LEGACY_DRAINING).
    """
    graph = runtime.graph
    mode = _read_mode(runtime)
    terminal = mode is AuthorityMode.GRAPH and _legacy_terminal(runtime, delivery)
    acceptance = table_writer_acceptance(EnvelopeKind.LEGACY, mode, legacy_terminal=terminal)
    if acceptance is Acceptance.LEGACY_PROTOCOL:
        return None
    if acceptance is Acceptance.LEGACY_PROTOCOL_MODE_CHECKED and graph is not None:
        return AuthorityModeGuard(ModeGuardKind.FINALIZE_NOT_GRAPH, graph.read_mode_item())
    if acceptance is Acceptance.LEGACY_REPLAY_ACKNOWLEDGED:
        _report(
            delivery, "semantic_unit_classified", "legacy_replay_acknowledged", LogSeverity.INFO
        )
        return _UnitResult(acknowledge=True, outcome="legacy_replay_acknowledged")
    # LEGACY_ENVELOPE_AFTER_CUTOVER.
    return _refuse_by_mode(acceptance.value, delivery.plan_key)


# --- S6 §2.5: graph envelopes ---------------------------------------------------------------------


def _graph_report(correlation_id: str) -> Report:
    return Report(
        operation="semantic_table_writer",
        correlation_id=correlation_id,
        logger=LOGGER,
    )


def _claim_lease() -> timedelta:
    return timedelta(
        seconds=WRITER_SETTINGS.table_writer_timeout_seconds
        + WRITER_SETTINGS.semantic_write_visibility_margin_seconds
    )


def _graph_claim(runtime: _Runtime, envelope: GraphEnvelope) -> ClaimDisposition:
    """Gate on one strong mode read, then take the graph execution claim (V-9, V-10, V-12)."""
    graph = runtime.graph
    binding = envelope.required_binding()
    mode = _read_mode(runtime)
    acceptance = table_writer_acceptance(EnvelopeKind.GRAPH, mode, legacy_terminal=False)
    if acceptance is not Acceptance.GRAPH_CLAIM or graph is None:
        refused = _refuse_by_mode(acceptance.value, binding.node_id)
        return ClaimDisposition(refused.acknowledge, refused.outcome)
    enqueued, received = queue_attributes(
        {
            name: value
            for name, value in (
                ("SentTimestamp", envelope.sent_timestamp),
                ("ApproximateFirstReceiveTimestamp", envelope.first_receive_timestamp),
            )
            if value is not None
        }
    )
    disposition = claim_graph_envelope(
        graph,
        UnreachablePostGrantFacts(),
        GraphClaimInput(
            run_id=binding.run_id,
            node_id=binding.node_id,
            dispatch_id=binding.dispatch_id,
            body=envelope.body_text,
            now=runtime.wall,
            lease=_claim_lease(),
            queue_enqueued_at=None if enqueued is None else enqueued.isoformat(),
            first_received_at=None if received is None else received.isoformat(),
            sleep_ms=runtime.sleep_ms,
        ),
    )
    report_operation(
        OperationLog(
            operation="semantic_table_writer",
            component="table_writer",
            phase=envelope.node,
            outcome=disposition.outcome,
            severity=LogSeverity.INFO,
            details={
                "run_id": binding.run_id,
                "node_id": binding.node_id,
                "dispatch_id": binding.dispatch_id,
                "receive_count": envelope.receive_count,
            },
        ),
        LOGGER,
    )
    return disposition


def _inline_runtime(runtime: _Runtime) -> InlineRunRuntime:
    return InlineRunRuntime(
        coordinator=runtime.coordinator,
        attempts=runtime.attempts,
        start=runtime.start,
        terminals=runtime.terminals,
        reads=runtime.reader,
        stager=runtime.stager,
        bounds=StatementStartBounds(
            max_statement_bytes=WRITER_SETTINGS.athena_max_statement_bytes,
            max_encoded_identity_bytes=WRITER_SETTINGS.max_encoded_identity_bytes,
        ),
        lease_duration_seconds=WRITER_SETTINGS.table_lease_duration_seconds,
        wall=runtime.wall,
        monotonic_ms=runtime.monotonic_ms,
        sleep_ms=runtime.sleep_ms,
        logger=LOGGER,
    )


def _claim_fence(
    graph: _GraphAuthority, claims: Sequence[ClaimRecord]
) -> Callable[[], tuple[GraphClaimFence, ...]]:
    """Strongly re-read each member's exact claim item for each Begin, in member order.

    Each pre-state moves per attempt, so the thunk reads again for every Begin (V-9).
    """
    held = tuple(claims)

    def fences() -> tuple[GraphClaimFence, ...]:
        observed: list[GraphClaimFence] = []
        for claim in held:
            item = graph.read_claim_item(claim.node_id)
            if item is None:
                raise _invariant("The graph claim disappeared while its unit was executing.")
            observed.append(GraphClaimFence(claim.node_id, claim.lease_version, item))
        return tuple(observed)

    return fences


# --- Slice 7 (1b): the unit batch executor (§2.5 "Batch execution", steps 1-8) ---------------

_MAX_REDECIDE: Final = int(BOUNDS["max_redecide"])
GRAPH_UNIT_BATCH_SIZE_METRIC: Final = "GraphUnitBatchSize"
GRAPH_UNIT_BATCH_COMMIT_MS_METRIC: Final = "GraphUnitBatchCommitMs"
GRAPH_UNIT_BATCH_SPLITS_METRIC: Final = "GraphUnitBatchSplits"
_BUDGET_OUTCOMES: Final = frozenset(
    {InlineRunOutcome.INSUFFICIENT_BUDGET, InlineRunOutcome.ATTEMPT_PARKED}
)
_TABLE_OUTCOMES: Final = frozenset(
    {InlineRunOutcome.REFUSED_TABLE_BUSY, InlineRunOutcome.REFUSED_TABLE_ATTEMPT_PENDING}
)
_MEMBER_OUTCOMES: Final = frozenset(
    {
        InlineRunOutcome.STATEMENT_FAILED,
        InlineRunOutcome.VERIFICATION_MISMATCH,
        InlineRunOutcome.ATTEMPT_BUDGET_EXHAUSTED,
    }
)


class _StopKind(StrEnum):
    """Why an invocation stopped executing: the disposition every later member takes."""

    BUDGET = "budget"
    """A time-caused outcome in a batch of more than one: unresolved, or `final_unisolated`."""
    TABLE = "table"
    """The table can't take a commit now: unresolved, or `FAILED` with the outcome when final."""
    SYSTEMIC = "systemic"
    """Both halves of a first split failed: unresolved, or `final_unisolated` when final."""
    REFUSED_LIVE = "refused_live"
    """Begin was refused while every re-read claim was live: unresolved (AR2)."""


@dataclass(frozen=True, slots=True)
class _Stop:
    kind: _StopKind
    reason: str
    cause: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _MemberSummary:
    """D10: one shaped member, body-free; formation and settlement read only this."""

    rows: int
    identities: frozenset[str]
    content_checksums: tuple[str, ...]
    servable: tuple[_ServableIdentity, ...]


def _summarize(shaped: _ShapedUnit) -> _MemberSummary:
    records = shaped.workload.records_by_identity
    return _MemberSummary(
        rows=sum(len(rows) for _, rows in records),
        identities=frozenset(identity for identity, _ in records),
        content_checksums=shaped.content_checksums,
        servable=shaped.servable,
    )


@dataclass(slots=True)
class _BatchMember:
    """One listed member and, once it has one, the class it ends the invocation in."""

    ref: GraphBatchMember
    node: NodeRecord | None = None
    unit: graph_unit.UnitClaim | None = None
    entry: CanonicalUnitEntry | None = None
    summary: _MemberSummary | None = None
    """D10: kept after step 2 instead of the shaped workload, which is re-read just in time."""
    execution_set_id: str | None = None
    settled: graph_unit.UnitSettled | None = None
    member_class: graph_unit.MemberClass | None = None

    def claimed(self) -> graph_unit.UnitClaim:
        if self.unit is None:
            raise _invariant("A batch member executed without its claim.")
        return self.unit

    def summarized(self) -> _MemberSummary:
        if self.summary is None:
            raise _invariant("A batch member reached its execution set unshaped.")
        return self.summary


@dataclass(frozen=True, slots=True)
class _ExecutionSet:
    members: tuple[_BatchMember, ...]
    workload: UnitWorkload
    composition: UnitStatementComposition
    shapes: tuple[MergeShape, ...]


class _SetStatus(StrEnum):
    DONE = "done"
    """Every member of the set has its class."""
    MEMBER_FAILED = "member_failed"
    """A member-attributable outcome on a set of more than one: the caller splits it."""
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class _Executed:
    """D10: one group's execution result; `members` are the members it ran or stopped."""

    status: _SetStatus
    outcome: str | None
    members: tuple[_BatchMember, ...]


def _entry_of(node: NodeRecord) -> CanonicalUnitEntry:
    """Return the roster entry the node's stored owner body carries (the unit's one home)."""
    try:
        body: object = json.loads(node.body)
        if not isinstance(body, dict):
            raise TypeError("a write_unit node body is a JSON object.")
        return _unit_entry(cast("dict[str, object]", body).get("unit"))
    except (AdlyticsError, ValueError, TypeError) as malformed:
        raise _invariant(
            "A write_unit node's stored body carries no canonical unit entry.",
            node_id=node.node_id,
            cause=type(malformed).__name__,
        ) from None


def _compose(workload: UnitWorkload, target: AthenaSemanticTarget) -> ComposeUnitStatementsRequest:
    return ComposeUnitStatementsRequest(
        workload=workload,
        workgroup=target.batch_workgroup,
        staging_database=target.staging_database,
        max_statement_bytes=WRITER_SETTINGS.athena_max_statement_bytes,
    )


def _union(workloads: Sequence[UnitWorkload]) -> UnitWorkload:
    first = workloads[0]
    return UnitWorkload(
        table=first.table,
        database=first.database,
        records_by_identity=tuple(
            pair for workload in workloads for pair in workload.records_by_identity
        ),
    )


def _identities(member: _BatchMember) -> frozenset[str]:
    return member.summarized().identities


def _group_members(ready: Sequence[_BatchMember]) -> list[tuple[_BatchMember, ...]]:
    """Step 3 (D10): member groups from summaries alone, in member order; composes nothing.

    A group ends before a member whose identities overlap it. State-blind, so the same members
    always form the same groups.
    """
    groups: list[tuple[_BatchMember, ...]] = []
    current: list[_BatchMember] = []
    identities: set[str] = set()
    for member in ready:
        own = _identities(member)
        if current and own & identities:
            groups.append(tuple(current))
            current, identities = [], set()
        current.append(member)
        identities |= own
    if current:
        groups.append(tuple(current))
    return groups


def _composition_defect(batch_id: str, description: str, **context: object) -> InvariantError:
    return InvariantError(
        GRAPH_UNIT_SET_COMPOSITION_CODE,
        description,
        context={"signal": GRAPH_UNIT_SET_COMPOSITION, "batch_id": batch_id, **context},
    )


@dataclass(slots=True)
class _Batch:
    """One unit batch invocation's state: its members, counters and stop (steps 1-8)."""

    runtime: _Runtime
    envelope: GraphEnvelope
    remaining_budget_ms: Callable[[], int]
    members: list[_BatchMember]
    claimed: int = 0
    sets: int = 0
    splits: int = 0
    commit_ms: int = 0
    budget_outcomes: int = 0
    redecides: int = 0
    stop: _Stop | None = None
    cause_reported: bool = False
    last_execution_ms: int = 0

    @property
    def graph(self) -> _GraphAuthority:
        graph = self.runtime.graph
        if graph is None:
            raise _invariant("A unit batch needs its graph authority.")
        return graph

    @property
    def batch_id(self) -> str:
        return self.envelope.batch_id or ""

    @property
    def lock_key(self) -> str:
        return self.envelope.lock_key or ""

    # --- classification ------------------------------------------------------------------------

    def settle(
        self, member: _BatchMember, action: Callable[[], graph_unit.UnitSettled]
    ) -> graph_unit.UnitSettled:
        """Give one member its one class, under its own unit span (D37)."""
        if member.member_class is not None:
            raise InvariantError(
                GRAPH_UNIT_BATCH_ACCOUNTING_CODE,
                "a batch member was classified twice.",
                context={"node_id": member.ref.node_id, "batch_id": self.batch_id},
            )
        settled = _member_span(self, member, action)
        member.settled = settled
        member.member_class = graph_unit.member_class(settled)
        return settled

    def _stop_member(  # noqa: PLR0911 -- one return per stop disposition
        self, member: _BatchMember, stop: _Stop
    ) -> graph_unit.UnitSettled:
        unit = member.claimed()
        if stop.kind is _StopKind.REFUSED_LIVE:
            graph_unit.retry(unit, stop.reason)
            return graph_unit.UnitSettled(
                False, "graph_unit_claim_refused_live", NodeState.CLAIMED, stop.reason
            )
        if stop.kind is _StopKind.TABLE:
            if stop.cause is not None:
                retryable, reason = classify_failure(stop.cause, final=unit.final)
                if retryable:
                    # The run's exception is reported once per receive, as built.
                    cause = None if self.cause_reported else stop.cause
                    self.cause_reported = True
                    return graph_unit.retry(unit, reason, cause)
                return graph_unit.settle_failed(unit, reason, stop.cause)
            if unit.final:
                return graph_unit.settle_failed(unit, stop.reason, None)
            return graph_unit.retry(unit, stop.reason)
        # Budget and systemic: never a failure in a batch of more than one (D43).
        if unit.final and unit_may_return(final_return=unit.final_return):
            return graph_unit.return_member(unit, ReturnReason.FINAL_UNISOLATED, stop.reason)
        if unit.final:
            return graph_unit.settle_failed(unit, stop.reason, stop.cause)
        return graph_unit.retry(unit, stop.reason)

    def apply_stop(self, members: Sequence[_BatchMember]) -> None:
        """Give every unclassified member the stop's disposition."""
        stop = self.stop
        if stop is None:
            raise _invariant("A batch applied a stop it does not hold.")
        for member in members:
            if member.member_class is None:
                self.settle(member, lambda member=member: self._stop_member(member, stop))

    def halt(self, members: Sequence[_BatchMember], stop: _Stop) -> _SetStatus:
        if self.stop is None:
            self.stop = stop
        self.apply_stop(members)
        return _SetStatus.STOPPED

    # --- steps 1 and 2 -------------------------------------------------------------------------

    def read_members(self) -> None:
        """Step 1's reads, before any write: a missing run or node is a defect, not a skip."""
        graph = self.graph
        for member in self.members:
            run = graph.read_run(member.ref.run_id)
            node = next(
                (n for n in graph.read_nodes(member.ref.run_id) if n.node_id == member.ref.node_id),
                None,
            )
            if run is None or node is None:
                raise InvariantError(
                    ErrorCode("EXEC_GRAPH", 23),
                    "a graph envelope names a run or node that does not exist.",
                )
            member.node = node

    def claim_members(self) -> None:
        """Step 1: skip stale members, then claim each other member with this batch's ID."""
        for member in self.members:
            node = member.node
            if node is None:
                raise _invariant("A batch member was not read before its claim.")
            stale = (
                node.batch_id != self.batch_id
                or node.dispatch_id != member.ref.dispatch_id
                or node.lock_key != self.lock_key
            )
            if stale:
                self.settle(member, lambda member=member: _stale(self, member))
                continue
            disposition = _claim_member(self.runtime, self.envelope, member.ref, node)
            if disposition.claim is None:
                settled = graph_unit.UnitSettled(disposition.acknowledge, disposition.outcome)
                self.settle(member, lambda settled=settled: settled)
                continue
            member.unit = graph_unit.UnitClaim(
                store=self.graph,
                claim=disposition.claim,
                dispatch_id=member.ref.dispatch_id,
                receive_count=self.envelope.receive_count,
                now=self.runtime.wall,
                report=_graph_report(member.ref.node_id),
                final_return=node.final_return,
                batch_id=self.batch_id,
            )
            member.entry = _entry_of(node)
        self.claimed = sum(member.unit is not None for member in self.members)

    def shape_members(self) -> None:
        """Step 2: shape and compose each claimed member alone; a refusal is that member's.

        D10: only the member's body-free summary is kept, so step 2 holds one member's records
        at a time.
        """
        for member in self.members:
            if member.unit is None or member.member_class is not None or member.entry is None:
                continue
            unit, entry = member.unit, member.entry
            try:
                summary = self._shape_alone(entry)
            except _ChecksumMismatchError as mismatch:
                self.settle(
                    member,
                    lambda unit=unit, mismatch=mismatch: graph_unit.settle_failed(
                        unit, graph_unit.CHECKSUM_MISMATCH, mismatch
                    ),
                )
                continue
            except Exception as error:
                self.settle(
                    member,
                    lambda unit=unit, error=error: graph_unit.settle_shaping_error(unit, error),
                )
                continue
            member.summary = summary

    def _shape_alone(self, entry: CanonicalUnitEntry) -> _MemberSummary:
        """Shape and compose one member alone; its workload is dropped when this returns."""
        shaped = _shape_unit(self.runtime, entry)
        alone = compose_unit_statements(_compose(shaped.workload, self.runtime.target))
        unit_statement_kinds(alone.merge_count)  # AR5-3: the built refusal, kept
        return _summarize(shaped)

    # --- step 3 --------------------------------------------------------------------------------

    def compose_set(self, group: Sequence[_BatchMember]) -> _ExecutionSet | _SetStatus:
        """Compose one group just in time (D10): re-shape, check, then compose the union.

        Phase 1 re-shapes each working (unclassified) member from S3; a checksum mismatch or
        another shaping error settles that member and the rest are composed, and a
        `DependencyError` halts the whole group TABLE (`STOPPED`). Phase 2 requires each
        re-shape to equal the member's summary. Phase 3 composes the union; any failure there,
        or a union `_set_fits` refuses, is `GRAPH_UNIT_SET_COMPOSITION` (M3, M4).
        """
        working = [member for member in group if member.member_class is None]
        workloads: list[UnitWorkload] = []
        composed: list[_BatchMember] = []
        for member in working:
            unit, entry = member.claimed(), member.entry
            if entry is None:
                raise _invariant("A batch member reached its execution set without its entry.")
            try:
                shaped = _shape_unit(self.runtime, entry)
            except _ChecksumMismatchError as mismatch:
                self.settle(
                    member,
                    lambda unit=unit, mismatch=mismatch: graph_unit.settle_failed(
                        unit, graph_unit.CHECKSUM_MISMATCH, mismatch
                    ),
                )
                continue
            except DependencyError as error:
                return self.halt(working, _Stop(_StopKind.TABLE, "raised", error))
            except Exception as error:
                self.settle(
                    member,
                    lambda unit=unit, error=error: graph_unit.settle_shaping_error(unit, error),
                )
                continue
            summary = member.summarized()
            again = _summarize(shaped)
            if (again.content_checksums, again.identities) != (
                summary.content_checksums,
                summary.identities,
            ):
                raise _composition_defect(
                    self.batch_id,
                    "a re-shaped batch member differs from its step-2 summary.",
                    node_id=member.ref.node_id,
                )
            workloads.append(shaped.workload)
            composed.append(member)
        if not composed:
            return _SetStatus.DONE
        try:
            workload = _union(workloads)
            request = _compose(workload, self.runtime.target)
            composition = compose_unit_statements(request)
            shapes = merge_shapes(request)
        except Exception as error:
            raise _composition_defect(
                self.batch_id,
                "composing a write_unit execution set's union raised; a defect.",
                cause=type(error).__name__,
            ) from error
        candidate = _ExecutionSet(tuple(composed), workload, composition, shapes)
        if len(shapes) != composition.merge_count or not _set_fits(candidate):
            raise _composition_defect(
                self.batch_id, "the composed execution set disagrees with its bounds."
            )
        return candidate

    def form_sets(self) -> list[tuple[_BatchMember, ...]]:
        """Step 3 (D10): the member groups, in member order; nothing is composed here.

        An empty result for shaped members is a defect (N1).
        """
        ready = [m for m in self.members if m.summary is not None and m.member_class is None]
        groups = _group_members(ready)
        if ready and not groups:
            raise _composition_defect(
                self.batch_id, "execution-set formation yielded no set for shaped members."
            )
        return groups

    # --- steps 4 to 8 --------------------------------------------------------------------------

    def _time_admits(self, statements: int) -> bool:
        """Step 4 (D43): only a batch of more than one claimed member runs the time rule."""
        if self.claimed <= 1:
            return True
        allowance = WRITE_UNIT_BATCHING["statement_allowance_ms"]
        if allowance is None:
            raise InvariantError(
                WRITE_UNIT_BATCHING_PENDING_PROBE_CODE,
                "the write_unit batching allowance is null; the deploy gate refuses it.",
            )
        needed = MIN_EXCLUSIVE_BUDGET_MS + (statements + 1) * int(allowance)
        return self.remaining_budget_ms() >= needed

    def execute(  # noqa: PLR0911, PLR0912 -- one return per classified end of a group
        self, group: Sequence[_BatchMember], *, isolate_single: bool
    ) -> _Executed:
        """Compose one group, run its set (step 5) and classify the outcome (steps 6-8).

        D10: `current` is the only name that binds a composed set, and a refused set is
        dropped before `remaining` is re-shaped, so at most one set's records are live.
        `isolate_single` is False for split halves, so a failed half of one member is reported
        back and the first split can judge whether both halves failed (systemic stop).
        """
        if self.stop is not None:
            self.apply_stop(group)
            return _Executed(_SetStatus.STOPPED, None, tuple(group))
        current = self.compose_set(group)
        if isinstance(current, _SetStatus):
            return _Executed(current, None, tuple(group))
        while True:
            if self.stop is not None:
                self.apply_stop(current.members)
                return _Executed(_SetStatus.STOPPED, None, current.members)
            statements = len(current.composition.statements)
            if not self._time_admits(statements):
                self.budget_outcomes += 1
                status = self.halt(current.members, _Stop(_StopKind.BUDGET, "time_rule"))
                return _Executed(status, None, current.members)
            set_id = execution_set_id_for(self.batch_id, [m.ref.node_id for m in current.members])
            for member in current.members:
                member.execution_set_id = set_id
            started = self.runtime.monotonic_ms()
            result: InlineRunResult | None = None
            try:
                result = self.runtime.inline_run(
                    _inline_runtime(self.runtime),
                    InlineRunRequest(
                        writer_kind=WriterKind.GRAPH_UNIT,
                        operation_id=self.batch_id,
                        execution_id=set_id,
                        physical_table_id=self.lock_key.removeprefix(TABLE_LOCK_PREFIX),
                        table_kind=_table_kind(current),
                        workload=current.workload,
                        composition=current.composition,
                        remaining_budget_ms=self.remaining_budget_ms,
                        servable=(),
                        graph_claims=_claim_fence(
                            self.graph, [m.claimed().claim for m in current.members]
                        ),
                    ),
                )
            except GraphAuthorityRefusedError:
                # Only record here: the active exception's traceback still reaches the refused
                # set, so the re-decision runs after the `try` statement has exited.
                self._record_set(
                    current, set_id, "graph_claim_refused", started=started, result=None
                )
            except Exception as error:
                self._record_set(current, set_id, "raised", started=started, result=None)
                return _Executed(self._run_raised(current, error), None, current.members)
            if result is not None:
                self._record_set(current, set_id, result.outcome, started=started, result=result)
                status = self._classify(current, result, isolate_single=isolate_single)
                return _Executed(status, result.outcome, current.members)
            members = current.members
            decision = self._refused_begin(members)
            if decision is None:
                return _Executed(_SetStatus.DONE, None, members)
            if isinstance(decision, _SetStatus):
                return _Executed(decision, None, members)
            remaining, lost = decision
            if lost:
                del current
                current = self.compose_set(remaining)
                if isinstance(current, _SetStatus):
                    return _Executed(current, None, remaining)

    def _run_raised(self, current: _ExecutionSet, error: BaseException) -> _SetStatus:
        """Stop at a run `DependencyError` or unclassified exception: table-level (step 8)."""
        return self.halt(current.members, _Stop(_StopKind.TABLE, "raised", error))

    def _refused_begin(
        self, members: tuple[_BatchMember, ...]
    ) -> tuple[tuple[_BatchMember, ...], bool] | _SetStatus | None:
        """Step 8, AR2/M1: re-read every claim; drop lost members, re-decide in one counter.

        Returns the remaining members and whether any was lost, None when every member was
        lost, or STOPPED once the shared `max_redecide` counter (N2) is spent with every claim
        still live.
        """
        lost: list[_BatchMember] = []
        for member in members:
            unit = member.claimed()
            claim = self.graph.read_claim(unit.node_id)
            if claim is None or claim.lease_version != unit.claim.lease_version:
                lost.append(member)
        for member in lost:
            unit = member.claimed()
            self.settle(member, lambda unit=unit: graph_unit.claim_lost(unit))
        remaining = tuple(m for m in members if m.member_class is None)
        if not remaining:
            return None
        if self.redecides >= _MAX_REDECIDE:
            report_error(
                InvariantError(
                    GRAPH_UNIT_CLAIM_REFUSED_LIVE_CODE,
                    "Begin refused a write_unit set while every re-read claim was live.",
                    context={
                        "signal": GRAPH_UNIT_CLAIM_REFUSED_LIVE,
                        "batch_id": self.batch_id,
                        "node_ids": [m.ref.node_id for m in remaining],
                    },
                ),
                _graph_report(self.batch_id),
            )
            return self.halt(remaining, _Stop(_StopKind.REFUSED_LIVE, "claim_refused_live"))
        self.redecides += 1
        return remaining, bool(lost)

    def _classify(
        self, current: _ExecutionSet, result: InlineRunResult, *, isolate_single: bool
    ) -> _SetStatus:
        outcome = result.outcome
        if outcome == InlineRunOutcome.FINALIZED:
            self.commit_ms += self.last_execution_ms
            self._succeed(current, result)
            return _SetStatus.DONE
        if self.claimed <= 1:
            (member,) = current.members
            self._decide_alone(member, outcome)
            return _SetStatus.DONE
        deadline = outcome == InlineRunOutcome.STATEMENT_FAILED and result.deadline_cancelled
        if outcome in _BUDGET_OUTCOMES or deadline:
            self.budget_outcomes += 1
            return self.halt(current.members, _Stop(_StopKind.BUDGET, outcome))
        if outcome in _TABLE_OUTCOMES:
            return self.halt(current.members, _Stop(_StopKind.TABLE, outcome))
        if outcome in _MEMBER_OUTCOMES:
            if len(current.members) == 1 and isolate_single:
                self._isolate(current.members[0], outcome)
                return _SetStatus.DONE
            return _SetStatus.MEMBER_FAILED
        graph_unit.decide(outcome, final=False)  # raises the closed-table invariant
        raise _invariant("An inline outcome escaped the graph-unit table.", outcome=outcome)

    def _decide_alone(self, member: _BatchMember, outcome: str) -> None:
        """Apply the built rows to a batch of one, with difference 4 (§2.5 step 8, D43)."""
        unit = member.claimed()
        action = graph_unit.decide(outcome, final=unit.final)
        if action is graph_unit.UnitAction.RETRY:
            self.settle(member, lambda: graph_unit.retry(unit, outcome))
        elif action is graph_unit.UnitAction.RETURN:
            self.settle(
                member,
                lambda: graph_unit.return_member(unit, ReturnReason.MEMBER_FAILED, outcome),
            )
        else:
            self.settle(member, lambda: graph_unit.settle_failed(unit, outcome, None))

    def _isolate(self, member: _BatchMember, outcome: str) -> None:
        """Settle or return a member that fails alone in a batch of more than one (step 7)."""
        unit = member.claimed()
        if outcome == InlineRunOutcome.STATEMENT_FAILED and not unit.final:
            self.settle(
                member,
                lambda: graph_unit.return_member(unit, ReturnReason.MEMBER_FAILED, outcome),
            )
            return
        self.settle(member, lambda: graph_unit.settle_failed(unit, outcome, None))

    def _succeed(self, current: _ExecutionSet, result: InlineRunResult) -> None:
        """Step 6 (D34): each member's own effect record, one transaction per chunk."""
        successes: list[graph_unit.UnitSuccess] = []
        for member in current.members:
            unit, entry = member.claimed(), member.entry
            if entry is None:
                raise _invariant("A finalized batch member has no roster entry.")
            record, digest = graph_unit.unit_effect_record(
                entry.unit_key,
                result.flow_id,
                tuple(
                    graph_unit.UnitTarget(
                        item.partition_key, item.publication_id, item.fencing_token
                    )
                    for item in member.summarized().servable
                ),
            )
            successes.append(graph_unit.UnitSuccess(unit, record, digest))
        settled = graph_unit.settle_set_succeeded(
            successes, lock_key=self.lock_key, batch_id=self.batch_id
        )
        for member in current.members:
            outcome = settled[member.ref.node_id]
            self.settle(member, lambda outcome=outcome: outcome)

    def _record_set(  # noqa: PLR0913 -- the set, its identity, outcome, start and result
        self,
        current: _ExecutionSet,
        set_id: str,
        outcome: str,
        *,
        started: int,
        result: InlineRunResult | None,
    ) -> None:
        """One `graph_unit_set_executed` record per executed set (§2.10, M4), at INFO."""
        elapsed = self.runtime.monotonic_ms() - started
        self.last_execution_ms = elapsed
        self.sets += 1
        report_operation(
            OperationLog(
                operation="semantic_table_writer",
                component="table_writer",
                phase="graph_unit_set_executed",
                outcome=outcome,
                severity=LogSeverity.INFO,
                details={
                    "batch_id": self.batch_id,
                    "execution_set_id": set_id,
                    "table": self.lock_key.removeprefix(TABLE_LOCK_PREFIX),
                    "statements": len(current.composition.statements),
                    "rows": sum(shape.rows for shape in current.shapes),
                    "execution_ms": elapsed,
                    "deadline_cancelled": False if result is None else result.deadline_cancelled,
                    "max_partitions_per_merge": max(
                        (shape.partitions for shape in current.shapes), default=0
                    ),
                    "members": len(current.members),
                },
            ),
            LOGGER,
        )

    def run_formed(self, group: tuple[_BatchMember, ...]) -> None:
        """Run one formed group, splitting a member-attributable failure in halves (step 7).

        D10: no composed set is bound here, so the parent set is unreachable once `execute`
        returns.
        """
        executed = self.execute(group, isolate_single=True)
        if executed.status is _SetStatus.MEMBER_FAILED and executed.outcome is not None:
            self._split(executed.members, executed.outcome, first=True)

    def _split(self, members: tuple[_BatchMember, ...], outcome: str, *, first: bool) -> None:
        """Halve a failed set in member order, run both halves, then recurse into failures.

        Each half is composed inside its own `execute`, so the second half is composed only
        after the first half's set is dropped, and every half yields exactly one `_Executed`.
        If both halves of the first split fail and no member is on its final attempt, the split
        stops (systemic) and every later set's members are unresolved. On a final attempt the
        split continues to single members.
        """
        if len(members) == 1:
            self._isolate(members[0], outcome)
            return
        middle = (len(members) + 1) // 2
        results: list[_Executed] = []
        for half in (members[:middle], members[middle:]):
            self.splits += 1
            results.append(self.execute(half, isolate_single=False))
        if any(entry.status is _SetStatus.STOPPED for entry in results):
            for entry in results:
                if entry.status is _SetStatus.MEMBER_FAILED:
                    self.apply_stop(entry.members)
            return
        both_failed = all(entry.status is _SetStatus.MEMBER_FAILED for entry in results)
        final = any(member.claimed().final for member in members)
        if first and both_failed and not final:
            self.halt(members, _Stop(_StopKind.SYSTEMIC, "systemic_stop"))
            return
        for entry in results:
            if entry.status is not _SetStatus.MEMBER_FAILED or entry.outcome is None:
                continue
            if self.stop is not None:
                self.apply_stop(entry.members)
                continue
            self._split(entry.members, entry.outcome, first=False)

    # --- the end of the invocation -------------------------------------------------------------

    def release_unclassified(self) -> None:
        """D-b: before a message-level invariant, release every unclassified member's lease."""
        for member in self.members:
            if member.unit is not None and member.member_class is None:
                graph_unit.retry(member.unit, "batch_invariant")

    def account(self) -> graph_unit.UnitBatchSettled:
        """Step 8: every listed member in exactly one class, then the acknowledgement."""
        missing = [m.ref.node_id for m in self.members if m.member_class is None]
        if missing:
            self.release_unclassified()
            raise InvariantError(
                GRAPH_UNIT_BATCH_ACCOUNTING_CODE,
                "a write_unit batch left a listed member in no class.",
                context={"batch_id": self.batch_id, "node_ids": missing},
            )
        settled = tuple(
            graph_unit.MemberSettled(
                m.ref.node_id,
                cast("graph_unit.MemberClass", m.member_class),
                cast("graph_unit.UnitSettled", m.settled),
            )
            for m in self.members
        )
        blocking = {graph_unit.MemberClass.UNRESOLVED, graph_unit.MemberClass.LIVE_DUPLICATE}
        acknowledge = not any(item.member_class in blocking for item in settled)
        if len(settled) == 1:
            outcome = settled[0].settled.outcome
        else:
            outcome = "graph_unit_batch_settled" if acknowledge else "graph_unit_batch_unresolved"
        return graph_unit.UnitBatchSettled(acknowledge, outcome, settled)


def _set_fits(candidate: _ExecutionSet) -> bool:
    """Step 3's per-set bounds: at most 10 statements, and a verification within the bound."""
    composition = candidate.composition
    verification = composition.verification.start_envelope.encode("utf-8")
    return (
        len(composition.statements) <= RUN_RESERVATION_CAP
        and len(verification) <= WRITER_SETTINGS.athena_max_statement_bytes
    )


def _table_kind(chosen: _ExecutionSet) -> str:
    kinds = {member.entry.table_kind for member in chosen.members if member.entry is not None}
    if len(kinds) != 1:
        raise _invariant("A write_unit execution set spans table kinds.")
    return next(iter(kinds))


def _stale(batch: _Batch, member: _BatchMember) -> graph_unit.UnitSettled:
    """Step 1: a member whose node no longer carries this batch is skipped (§2.12 redrive)."""
    report_operation(
        OperationLog(
            operation="semantic_table_writer",
            component="table_writer",
            phase="graph_unit",
            outcome="graph_batch_member_stale",
            severity=LogSeverity.WARNING,
            details={
                "batch_id": batch.batch_id,
                "run_id": member.ref.run_id,
                "node_id": member.ref.node_id,
                "dispatch_id": member.ref.dispatch_id,
                "receive_count": batch.envelope.receive_count,
            },
        ),
        LOGGER,
    )
    return graph_unit.UnitSettled(True, "graph_batch_member_stale")


def _claim_member(  # noqa: PLR0913, PLR0917 -- the runtime, the batch and one member's reads
    runtime: _Runtime, envelope: GraphEnvelope, ref: GraphBatchMember, node: NodeRecord
) -> ClaimDisposition:
    """Claim one batch member through the one graph claim, with the node's stored body (N1)."""
    graph = runtime.graph
    if graph is None:
        raise _invariant("A unit batch needs its graph authority.")
    enqueued, received = queue_attributes(
        {
            name: value
            for name, value in (
                ("SentTimestamp", envelope.sent_timestamp),
                ("ApproximateFirstReceiveTimestamp", envelope.first_receive_timestamp),
            )
            if value is not None
        }
    )
    disposition = claim_graph_envelope(
        graph,
        runtime.graph_facts,
        GraphClaimInput(
            run_id=ref.run_id,
            node_id=ref.node_id,
            dispatch_id=ref.dispatch_id,
            body=node.body,
            now=runtime.wall,
            lease=_claim_lease(),
            queue_enqueued_at=None if enqueued is None else enqueued.isoformat(),
            first_received_at=None if received is None else received.isoformat(),
            sleep_ms=runtime.sleep_ms,
            batch_id=envelope.batch_id,
        ),
    )
    report_operation(
        OperationLog(
            operation="semantic_table_writer",
            component="table_writer",
            phase="write_unit",
            outcome=disposition.outcome,
            severity=LogSeverity.INFO,
            details={
                "batch_id": envelope.batch_id,
                "run_id": ref.run_id,
                "node_id": ref.node_id,
                "dispatch_id": ref.dispatch_id,
                "receive_count": envelope.receive_count,
            },
        ),
        LOGGER,
    )
    return disposition


@trace_boundary(TABLE_WRITER_GRAPH_UNIT)
def _member_span(
    batch: _Batch, member: _BatchMember, action: Callable[[], graph_unit.UnitSettled]
) -> graph_unit.UnitSettled:
    """One `TABLE_WRITER_GRAPH_UNIT` span per member, under the batch span (§2.10, D37)."""
    node = member.node
    with span(
        TABLE_WRITER_GRAPH_UNIT,
        dispatch_id=member.ref.dispatch_id,
        run_id=member.ref.run_id,
        node_id=member.ref.node_id,
        conflict_unit=_conflict_unit(node),
        receive_count=batch.envelope.receive_count,
        node="write_unit" if node is None else node.name,
        batch_id=batch.batch_id,
        execution_set_id=member.execution_set_id,
        admission=None if member.unit is None else member.unit.admission,
        stage="unit",
        completed=0,
        total=1,
    ) as recording:
        settled = action()
        recording.record(
            completed=1,
            outcome=settled.outcome,
            node_state="none" if settled.node_state is None else settled.node_state.value,
            failure_reason=settled.failure_reason or "none",
        )
    return settled


def _conflict_unit(node: NodeRecord | None) -> str:
    if node is None:
        return "unknown"
    try:
        body: object = json.loads(node.body)
    except ValueError:
        return "unknown"
    if isinstance(body, dict):
        value = cast("dict[str, object]", body).get("conflict_unit")
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _batch_metrics(batch: _Batch) -> None:
    """D37: the three batch metrics, once per invocation, with no table or run dimension.

    Each carries `batch_id` as EMF metadata, a log property and never a dimension, so P6(e)
    attributes every sample to its own batch (FEAT-009 review F6).
    """
    for name, value in (
        (GRAPH_UNIT_BATCH_SIZE_METRIC, batch.claimed),
        (GRAPH_UNIT_BATCH_COMMIT_MS_METRIC, batch.commit_ms),
        (GRAPH_UNIT_BATCH_SPLITS_METRIC, batch.splits),
    ):
        unit = (
            MetricUnit.Milliseconds
            if name == GRAPH_UNIT_BATCH_COMMIT_MS_METRIC
            else MetricUnit.Count
        )
        with single_metric(
            name=name, unit=unit, value=value, namespace=METRICS_NAMESPACE
        ) as metric:
            metric.add_metadata(key="batch_id", value=batch.envelope.batch_id or "")


def _process_unit_batch(
    runtime: _Runtime, envelope: GraphEnvelope, remaining_budget_ms: Callable[[], int]
) -> tuple[graph_unit.UnitBatchSettled, _Batch | None]:
    """Execute one unit batch: steps 1-8 of design §2.5 "Batch execution".

    A batch of one is this path with one member. Every listed member ends in exactly one class,
    or the batch raises a named invariant after releasing every unclassified lease.
    """
    mode = _read_mode(runtime)
    acceptance = table_writer_acceptance(EnvelopeKind.GRAPH, mode, legacy_terminal=False)
    if acceptance is not Acceptance.GRAPH_CLAIM or runtime.graph is None:
        refused = _refuse_by_mode(acceptance.value, envelope.batch_id or "")
        return graph_unit.UnitBatchSettled(refused.acknowledge, refused.outcome, ()), None
    batch = _Batch(
        runtime=runtime,
        envelope=envelope,
        remaining_budget_ms=remaining_budget_ms,
        members=[_BatchMember(ref) for ref in envelope.members],
    )
    try:
        batch.read_members()
        try:
            batch.claim_members()
            batch.shape_members()
            for group in batch.form_sets():
                batch.run_formed(group)
        except BaseException:
            batch.release_unclassified()
            raise
        return batch.account(), batch
    finally:
        # D37 (FEAT-009 review F5): every invocation that formed a batch emits each metric
        # exactly once, also when an invariant or a store failure raises out of it.
        _batch_metrics(batch)


@trace_boundary(TABLE_WRITER_GRAPH_UNIT_BATCH)
def _graph_unit_batch_envelope(
    runtime: _Runtime, envelope: GraphEnvelope, remaining_budget_ms: Callable[[], int]
) -> _UnitResult:
    """Run one unit batch envelope under its one root `TABLE_WRITER_GRAPH_UNIT_BATCH` span."""
    batch_id = envelope.batch_id or ""
    message_dispatch = batch_dispatch_id(batch_id, envelope.body_text)
    with span(
        TABLE_WRITER_GRAPH_UNIT_BATCH,
        dispatch_id=message_dispatch,
        batch_id=batch_id,
        lock_key=envelope.lock_key,
        member_count=len(envelope.members),
        receive_count=envelope.receive_count,
        stage="batch",
        completed=0,
        total=len(envelope.members),
    ) as recording:
        result, batch = _process_unit_batch(runtime, envelope, remaining_budget_ms)
        counts = dict.fromkeys(graph_unit.MemberClass, 0)
        for member in result.members:
            counts[member.member_class] += 1
        recording.record(
            completed=len(result.members),
            outcome=result.outcome,
            set_count=0 if batch is None else batch.sets,
            split_count=0 if batch is None else batch.splits,
            succeeded_count=counts[graph_unit.MemberClass.SUCCEEDED],
            failed_count=counts[graph_unit.MemberClass.FAILED],
            returned_count=counts[graph_unit.MemberClass.RETURNED],
            skipped_count=counts[graph_unit.MemberClass.SKIPPED],
            live_duplicate_count=counts[graph_unit.MemberClass.LIVE_DUPLICATE],
            unresolved_count=counts[graph_unit.MemberClass.UNRESOLVED],
            commit_ms=0 if batch is None else batch.commit_ms,
            budget_outcome_count=0 if batch is None else batch.budget_outcomes,
        )
    return _UnitResult(acknowledge=result.acknowledge, outcome=result.outcome)


def _graph_publish(runtime: _Runtime, envelope: GraphEnvelope) -> graph_publish.PublishResult:
    disposition = _graph_claim(runtime, envelope)
    claim = disposition.claim
    if claim is None:
        return graph_publish.PublishResult(disposition.acknowledge, disposition.outcome)
    graph, ports = runtime.graph, runtime.publish
    if graph is None or ports is None or envelope.grant_id is None:
        raise _invariant("A publish needs its graph authority, publications and grant.")
    binding = envelope.required_binding()
    return graph_publish.publish_claimed(
        graph,
        ports,
        graph_publish.PublishEnvelope(
            run_id=binding.run_id,
            node_id=binding.node_id,
            dispatch_id=binding.dispatch_id,
            grant_id=envelope.grant_id,
            granted_run=envelope.granted_run or "",
            conflict_unit=envelope.conflict_unit,
            receive_count=envelope.receive_count,
        ),
        claim,
        runtime.wall,
        LOGGER,
    )


@trace_boundary(GRAPH_PUBLISH)
def _graph_publish_envelope(runtime: _Runtime, envelope: GraphEnvelope) -> _UnitResult:
    """Run one `publish` graph envelope under its one root `GRAPH_PUBLISH` span (§2.6)."""
    binding = envelope.required_binding()
    with span(
        GRAPH_PUBLISH,
        dispatch_id=binding.dispatch_id,
        run_id=binding.run_id,
        node_id=binding.node_id,
        conflict_unit=envelope.conflict_unit,
        receive_count=envelope.receive_count,
        stage="publish",
        completed=0,
        total=1,
        grant_id=envelope.grant_id,
    ) as recording:
        result = _graph_publish(runtime, envelope)
        counts = result.counts
        recording.record(
            completed=1,
            outcome=result.outcome,
            node_state="none" if result.node_state is None else result.node_state.value,
            target_count=result.target_count,
            advanced_count=None if counts is None else counts.advanced,
            superseded_count=None if counts is None else counts.superseded,
        )
    return _UnitResult(acknowledge=result.acknowledge, outcome=result.outcome)


def _process_graph_envelope(
    runtime: _Runtime, envelope: GraphEnvelope, remaining_budget_ms: Callable[[], int]
) -> _UnitResult:
    """Route one graph envelope to its root span owner."""
    if envelope.kind is GraphEnvelopeKind.WRITE_UNIT_BATCH:
        return _graph_unit_batch_envelope(runtime, envelope, remaining_budget_ms)
    return _graph_publish_envelope(runtime, envelope)


@trace_boundary(TABLE_WRITER_UNIT)
def _process_legacy_unit(  # noqa: PLR0911 - one explicit ordered §2.2 unit lifecycle
    runtime: _Runtime,
    delivery: WriteUnitDelivery,
    gate: tuple[Callable[[], int], AuthorityModeGuard | None],
) -> _UnitResult:
    """Run the legacy protocol unchanged, with the finalize mode guard when the batch drains."""
    remaining_budget_ms, mode_guard = gate
    classified = _classify_authority(runtime, delivery)
    if isinstance(classified, _Acknowledged):
        _report(delivery, "semantic_unit_classified", classified.outcome, LogSeverity.INFO)
        return _UnitResult(acknowledge=True, outcome=classified.outcome)
    if isinstance(classified, _RetryableUnit):
        _report(delivery, "semantic_unit_classified", classified.reason, LogSeverity.WARNING)
        if classified.reason == "admission_incomplete":
            # Only this classification gets a short redelivery; every other retryable outcome
            # keeps the queue's visibility timeout.
            runtime.visibility.change(
                delivery.receipt_handle, admission_retry_seconds(delivery.receive_count)
            )
        return _UnitResult(acknowledge=False, outcome=classified.reason)
    authority = classified
    identity = derive_unit_identity(_unit_identity_request(delivery))
    head = authority.unit_head
    plan_head = authority.plan_head.head
    with span(
        TABLE_WRITER_UNIT,
        flow_id=identity.flow_id,
        parent_flow_id=plan_head.flow_id,
        causation_event_id=(
            head.causation_event_id
            if head is not None and head.causation_event_id is not None
            else plan_head.latest_event_id
        ),
        execution_id=identity.execution_id,
        internal_plan_key=delivery.plan_key,
        writer_execution_id=delivery.writer_execution_id,
        receive_count=delivery.receive_count,
        table_kind=authority.unit_entry.table_kind,
        stage="unit",
        completed=0,
        total=1,
    ) as unit_span:
        status = authority.unit_item.get("status")
        if status == "attested":
            outcome = _plan_terminal(runtime, delivery)
            unit_span.record(completed=1, outcome="attested_replay")
            return _UnitResult(acknowledge=True, outcome=f"attested_replay_{outcome}")
        ctx = _UnitContext(
            runtime=runtime,
            delivery=delivery,
            authority=authority,
            identity=identity,
            head=head if head is not None else plan_head,
            lease=None,
            remaining_budget_ms=remaining_budget_ms,
            mode_guard=mode_guard,
        )
        refused = _step_one_claim(ctx, status)
        if refused is not None:
            unit_span.record(completed=1, outcome=refused.reason)
            return _UnitResult(acknowledge=False, outcome=refused.reason)
        refused = _acquire_lease(ctx)
        if refused is not None:
            unit_span.record(completed=1, outcome=refused.reason)
            return _UnitResult(acknowledge=False, outcome=refused.reason)
        try:
            shaped = _shape_unit(runtime, authority.unit_entry)
            composition = compose_unit_statements(_compose(shaped.workload, runtime.target))
            unit_statement_kinds(composition.merge_count)
            retryable = _execute_statements(ctx, composition)
            if retryable is not None:
                unit_span.record(completed=1, outcome=retryable.reason)
                return _UnitResult(acknowledge=False, outcome=retryable.reason)
            _finalize_unit(ctx, shaped)
        except _ChecksumMismatchError as mismatch:
            _report(
                delivery,
                "semantic_unit_failed",
                UnitFailureReason.CHECKSUM_MISMATCH.value,
                LogSeverity.WARNING,
                reference=str(mismatch),
            )
            _fail_unit(ctx, UnitFailureReason.CHECKSUM_MISMATCH)
            unit_span.record(completed=1, outcome="checksum_mismatch")
            return _UnitResult(acknowledge=False, outcome="unit_failed")
        except _AttemptBudgetExhaustedError:
            _fail_unit_attempt_budget(ctx)
            unit_span.record(completed=1, outcome="attempt_budget_exhausted")
            return _UnitResult(acknowledge=True, outcome="attempt_budget_successor_created")
        except _StatementFailedError as failure:
            result = _statement_failure_disposition(ctx, failure)
            unit_span.record(completed=1, outcome=result.outcome)
            return result
        finally:
            _release_lease(ctx)
        _report(
            delivery,
            "semantic_unit_attested",
            "attested",
            LogSeverity.INFO,
            accepted_attempts=_accepted_attempts(ctx),
            merge_count=composition.merge_count,
            rows=sum(len(records) for _, records in shaped.workload.records_by_identity),
        )
        _plan_terminal(runtime, delivery)
        unit_span.record(completed=1, outcome="attested")
        return _UnitResult(acknowledge=True, outcome="attested")


RUNTIME = _runtime()


def _remaining_budget(context: object) -> Callable[[], int]:
    method = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(method):
        raise _invariant("The table writer requires the Lambda remaining-time budget.")
    bound = cast("Callable[[], int]", method)
    return lambda: int(bound())


def _correlation_id(delivery: WriteUnitDelivery | GraphEnvelope | None) -> str:
    if delivery is None:
        return ""
    if isinstance(delivery, GraphEnvelope):
        if delivery.binding is not None:
            return delivery.binding.node_id
        return delivery.batch_id or ""
    return delivery.plan_key


@trace_boundary(TABLE_WRITER_PROCESS)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: object) -> dict[str, list[dict[str, str]]]:
    """Process one SQS write unit and report its §4 acknowledgement decision.

    A retryable typed outcome returns the exact received ``messageId`` in
    ``batchItemFailures``; acknowledgment happens only after the pinned §4
    conditions. An unclassified failure raises, leaving the whole batch-of-one
    unacknowledged.
    """
    budget = _remaining_budget(context)
    delivery: WriteUnitDelivery | GraphEnvelope | None = None
    try:
        delivery = table_writer_delivery(event)
        if isinstance(delivery, GraphEnvelope):
            # S6 §2.5: a graph envelope's root span is its own; no legacy identity is opened.
            result = _process_graph_envelope(RUNTIME, delivery, budget)
        else:
            identity = derive_unit_identity(_unit_identity_request(delivery))
            parent = derive_plan_identity(
                PlanIdentityRequest(delivery.plan_digest, delivery.internal_delivery_execution_id)
            )
            with span(
                TABLE_WRITER_PROCESS,
                flow_id=identity.flow_id,
                parent_flow_id=parent.flow_id,
                causation_event_id="unresolved",
                execution_id=identity.execution_id,
                internal_plan_key=delivery.plan_key,
                internal_delivery_execution_id=delivery.internal_delivery_execution_id,
                writer_execution_id=delivery.writer_execution_id,
                receive_count=delivery.receive_count,
                stage="writer",
                completed=0,
                total=1,
            ) as process_span:
                result = _process_unit(RUNTIME, delivery, budget)
                process_span.record(completed=1, outcome=result.outcome)
        _count_receive(result.outcome, delivery.receive_count)
    except GraphEnvelopeShapeError as refused:
        # Zero effect, unacknowledged for the DLQ, and the invariant reported once.
        _count_receive("graph_envelope_invalid", refused.receive_count)
        report_error(
            refused,
            Report(
                operation="semantic_table_writer",
                correlation_id=refused.message_id,
                logger=LOGGER,
            ),
        )
        return {"batchItemFailures": [{"itemIdentifier": refused.message_id}]}
    except Exception as error:
        if delivery is not None:
            _count_receive("raised", delivery.receive_count)
        report_error(
            error,
            Report(
                operation="semantic_table_writer",
                correlation_id=_correlation_id(delivery),
                logger=LOGGER,
            ),
        )
        raise
    if result.acknowledge:
        return {"batchItemFailures": []}
    return {"batchItemFailures": [{"itemIdentifier": delivery.message_id}]}
