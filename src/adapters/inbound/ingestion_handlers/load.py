# SPDX-License-Identifier: MIT-0
"""Load one immutable Semantic V2 delivery from an S3 event (write-discipline phase 1).

A landing failure propagates so SQS retries and eventually preserves the message in the dead-letter
queue. A typed Semantic V2 decision returns success and is reported with bounded outcome metrics;
retry identity is derived only from the exact versioned evidence in the landing.

Phase 1 proves feasibility before the first authority mutation, publishes authority exactly as
before (S3 objects, attempts, publications, ``CURRENT``), and then either submits the one plan
intent for the queued composition or drives every physical table inline for the transitional
inline composition. It never sends a semantic-write queue message: ``PlanIntentReconciler`` is the
sole post-persistence actor.

S6 §2.4: a record body carrying the graph dispatch binding is a `materialize_raw` graph envelope.
It runs only in `GRAPH` (`graph_materialize`): claimed, materialized with nothing servable
advanced and no plan intent, and settled on the execution graph.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Protocol, cast

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.metrics import MetricUnit, single_metric

from adapters.config import (
    projection_handoff_settings,
    settings_without_client_secret,
    stage1_delivery_settings,
    tracing_settings,
)
from adapters.graph_authority import provisioned_graph_table
from adapters.inbound.ingestion_handlers.delivery_plan import (
    POST_AUTHORITY_INFEASIBLE,
    DeliveryRoster,
    FeasibilityCheck,
    PreparedRosterRequest,
    ProjectionComposition,
    PublishedRosterRequest,
    prepared_roster,
    projection_composition,
    published_roster,
    require_feasible,
)
from adapters.inbound.ingestion_handlers.graph_materialize import (
    DELIVERY_CLAIM_LEASE,
    GRAPH_MAX_UNITS,
    GraphEnvelopeDelivery,
    GraphEnvelopeRefusedError,
    GraphMaterialization,
    MaterializeRequest,
    MaterializeRuntime,
    ZeroRowTarget,
    graph_envelope_delivery,
    is_graph_envelope,
    materialize_graph_envelope,
    unit_envelope,
)
from adapters.inbound.ingestion_handlers.ingress import (
    DeliveryIngress,
    DeliveryRef,
    IngressPath,
    drain_ended,
    drain_wait,
    graph_admit,
    ingress_path,
    queue_url_from_arn,
    shadow_record,
)
from adapters.inbound.ingestion_handlers.inline_run import (
    InlineAuthorityReads,
    InlineRunOutcome,
    InlineRunRequest,
    InlineRunResult,
    InlineRunRuntime,
    run_inline_table,
)
from adapters.inbound.ingestion_handlers.table_attempt_protocol import instant
from adapters.inbound.run_requests.service import (
    AdmissionDependencies,
    RunRequest,
    admit_run_request,
)
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.ingest_delivery.landing import (
    ManifestInput,
    fetch_v2_delivery,
    v2_operation_id,
)
from adapters.jobs.ingest_delivery.semantic_ingestion import (
    SemanticIngestionRequest,
    ingest_semantic_delivery,
)
from adapters.jobs.ingest_delivery.semantic_sidecar import (
    PreparedProjectionPlan,
    SemanticSidecarProgress,
    SemanticSidecarTooling,
)
from adapters.jobs.schema_generation.publisher_graph import load_publication_graph
from adapters.outbound.athena.semantic_storage import AthenaSemanticTarget
from adapters.outbound.athena.table_attempt_start import Stage1AthenaStartClient
from adapters.outbound.athena.table_attempt_terminal import Stage1AthenaTerminalClient
from adapters.outbound.audience_segment_providers import load_segment_providers
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore
from adapters.outbound.dynamodb.execution_graph_dispatch import DynamoGraphDispatchStore
from adapters.outbound.dynamodb.projection_coordinator import DynamoProjectionCoordinator
from adapters.outbound.dynamodb.semantic_state import (
    DynamoAttemptStore,
    DynamoLeaseStore,
    DynamoPublicationStore,
    DynamoPublicationStoreAccess,
    bounded_lease_client,
)
from adapters.outbound.dynamodb.stage1_lifecycle_head import DynamoStage1LifecycleHeadStore
from adapters.outbound.dynamodb.stage1_writer_authority import Stage1WriterAuthorityReader
from adapters.outbound.dynamodb.table_attempt import DynamoTableAttemptStore
from adapters.outbound.governed_materialization import GovernedMaterializationPort
from adapters.outbound.legacy_head import S3CoordinatorLegacyHead
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.s3.run_request_manifests import S3RunRequestManifests
from adapters.outbound.semantic_storage import s3_only_semantic_storage
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.table_dq import load_runtime_table_rules
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
    INGEST_FETCH,
    INGEST_HANDLER_PROCESS,
    span,
    trace_boundary,
)
from adapters.outbound.v2_storage_catalog import V2StorageCatalog, load_v2_storage_catalog
from adapters.producer.packs import discard_policies, installed_governance, load_query_packs
from domain.curation.ingestion import IngestionError
from domain.curation.landing import MANIFEST_NAME
from domain.operations.execution_graph import AuthorityMode, ModeState
from domain.operations.execution_graph_dispatch import GRAPH_DISPATCH_NOT_ACTIVE
from domain.operations.source_delivery import (
    ProvedSourceOutcome,
    SourceDeliveryObservation,
    classify_source_delivery,
)
from domain.operations.stage1_operational_status import PlanIdentityRequest, derive_plan_identity
from domain.operations.statement_protocol import StatementStartBounds
from domain.operations.table_attempt import WriterKind
from domain.operations.write_plan import (
    IntentMeta,
    IntentOrigin,
    IntentOriginKind,
    PlanMeta,
    SuccessorOwner,
    intent_partition_key,
    plan_digest_for_source_delivery,
    source_delivery_identity,
)
from domain.semantics.publication import CanonicalDatasetKey
from domain.shared_kernel.errors import (
    AdlyticsError,
    DependencyError,
    ErrorCode,
    InvariantError,
)
from ports.outbound.execution_graph import ReceiptRecord
from ports.outbound.projection_coordinator import (
    AuthorityModeGuard,
    GraphAuthorityRefusedError,
    ModeGuardKind,
    ProjectionCoordinatorPort,
    SubmitPlanIntentRequest,
    SubmitPlanIntentResult,
)

if TYPE_CHECKING:  # pragma: no cover — kept out of the Lambda import path
    from collections.abc import Callable

    from adapters.config import Stage1DeliverySettings
    from adapters.jobs.ingest_delivery.landing import LoadedDelivery
    from adapters.jobs.ingest_delivery.semantic_sidecar import SemanticSidecarResult
    from adapters.outbound.governed_materialization import MaterializationClaim
    from domain.operations.stage1_lifecycle_head import Stage1LifecycleHeadTuple
    from domain.semantics.publication import SuccessfulPublication
    from ports.outbound.execution_graph_dispatch import RunRecord

__all__ = [
    "IngestionMessage",
    "delivery_messages",
    "handler",
    "ingestion_messages",
    "manifest_keys",
]

LOGGER = Logger(service="adlytics-ingest-handler")

PARTITION_METRIC = "IngestPartitions"
"""One count per partition a delivery touched, dimensioned on the action **only**.

Not on the contract as well, and the reason is CloudWatch rather than taste: a metric published with
two dimensions is not retrievable by one of them, so `Action=quarantined` and
`Action=quarantined,Contract=x` are different series. An alarm on "any quarantine" would need one
alarm per contract, and would silently miss the twelfth contract somebody adds.

The contract is on the `ingest.partition` span and in the log line, where cardinality costs nothing
and both are queryable.
"""

DELIVERY_METRIC = "IngestDeliveries"
"""One count per message this function accepted, whatever happened to its partitions.

A denominator, and therefore deliberately undimensioned. A quarantine count with no total cannot
express a rate, and it is the rate that says whether one bad producer arrived or the whole feed
changed shape.
"""

METRICS_NAMESPACE = "Adlytics"
"""Stated here because `single_metric` opens its own EMF blob and takes its own namespace.

The same namespace `reporting.METRICS` uses. Two spellings of it would put half this function's
metrics somewhere nothing is alarmed on, which is the failure a shared constant prevents.
"""

NO_PLAN_KEY = "none"
"""Receipt correlation for an empty unit set: authority published, no plan or intent exists."""

INLINE_PLAN_KEY = "inline"
"""Receipt correlation for the transitional inline composition (§9): no plan key is invented."""

AUTHORITY_MODE_DRAINING = "AUTHORITY_MODE_DRAINING"
"""V-13(b): the coordinator's refusal of a new root after the drain CAS."""
INLINE_RUN_RETRYABLE = ErrorCode("STAGE1_DELIVERY", 3)
INLINE_RUN_FAILED = ErrorCode("STAGE1_DELIVERY", 4)

# Resolved at import, so a container that cannot resolve its configuration never becomes healthy. A
# request-time failure here would be a partial outage presenting as a defect in whichever delivery
# happened to arrive first.
#
# `settings_without_client_secret` rather than `settings`: this is not the BFF. Reaching Secrets
# Manager at init would make a load fail to start for a credential it must never hold, and would
# need `secretsmanager:GetSecretValue` on this role to write curated rows.
SETTINGS = settings_without_client_secret()

DELIVERY_SETTINGS = stage1_delivery_settings()
GRAPH_TABLE = provisioned_graph_table()
"""S3 (V-13(b)): the execution-graph table whose mode item a new-root submission checks; None
where the graph authority is not provisioned (`adapters.graph_authority`), which reads LEGACY."""
"""The §11 frozen accessor phase 1 consumes: bounds for feasibility and the inline lease."""

COMPOSITION = projection_composition(projection_handoff_settings().state)
"""The §2.5 resolver's projection composition for this deployment: inline writer or queued."""

TRACING = configure_tracing(tracing_settings())
"""Whether spans leave this container. Recorded at init because a function that believes it is
exporting and is not looks identical to one that is, and produces no error to find."""

ROOT = contracts_root()
"""Where `contracts/` sits relative to this module inside the bundle.

Shared with the tool handlers rather than searched for again. Two upward searches would be two
answers the moment the packaging layout changes, and the failure would be a load that cannot find
the contracts it is supposed to enforce.
"""

COLUMNS = load_registry(ROOT)
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
INSTALLED_PACKS = load_query_packs(ROOT)
INSTALLED_GOVERNANCE = installed_governance(INSTALLED_PACKS)
INSTALLED_DISCARD_POLICIES = discard_policies(INSTALLED_PACKS)
"""Read once per container. All are pure functions of files in this artifact, so re-reading them
per invocation would spend cold-start work on every warm one."""

S3 = boto3.client("s3")
DYNAMODB = boto3.client("dynamodb")
SQS = boto3.client("sqs")


class _AuthorityReads(InlineAuthorityReads, Protocol):
    """The strong reads phase 1 needs: the inline run's plus the §5.3 classifier's."""

    def owner(self, plan_digest: str) -> SuccessorOwner | None: ...
    def intent(self, plan_digest: str, intent_id: str) -> IntentMeta | None: ...
    def plan_meta(self, plan_key: str) -> PlanMeta | None: ...


@dataclass(frozen=True, slots=True)
class _V2Runtime:
    """Typed phase-one dependencies and their exact immutable landing bucket.

    ``inline`` is composed only for the inline composition; the queued composition constructs no
    Athena client and runs no statement.
    """

    bucket: str
    tooling: SemanticSidecarTooling
    target: AthenaSemanticTarget
    catalog: V2StorageCatalog
    publications: DynamoPublicationStore
    coordinator: ProjectionCoordinatorPort
    reads: _AuthorityReads
    settings: Stage1DeliverySettings
    composition: ProjectionComposition
    inline: InlineRunRuntime | None
    mode_item: Callable[[], dict[str, object] | None] | None
    """S3: one strong read of the execution-graph mode item, for the V-13(b) guard; None where
    the graph authority is not provisioned."""
    ingress: DeliveryIngress
    """S6 (V-19): the per-message mode read and the `delivery_event` requester."""
    materialization: GovernedMaterializationPort
    """S6 (hard deny (d)): the one governed raw fetch; refuses a historical manifest first."""

    def __post_init__(self) -> None:
        if (self.inline is None) != (self.composition is ProjectionComposition.QUEUED):
            raise ValueError("an inline runtime exists exactly for the inline composition.")


def _raw_bucket() -> str:
    """Validate the immutable landing boundary."""
    if not SETTINGS.raw_bucket.strip():
        raise IngestionError(
            "a load needs a raw bucket to read from, and none was configured. In a deployed "
            "function that is ADL_RAW_BUCKET."
        )
    return SETTINGS.raw_bucket


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _sleep_ms(milliseconds: int) -> None:
    time.sleep(milliseconds / 1_000)


def _inline_runtime(
    coordinator: ProjectionCoordinatorPort, reads: InlineAuthorityReads
) -> InlineRunRuntime:
    """Compose the transitional inline writer's runtime from the §11 delivery accessor."""
    return InlineRunRuntime(
        coordinator=coordinator,
        attempts=DynamoTableAttemptStore(SETTINGS.semantic_publications_table, DYNAMODB),
        start=Stage1AthenaStartClient(),
        terminals=Stage1AthenaTerminalClient(),
        reads=reads,
        bounds=StatementStartBounds(
            max_statement_bytes=DELIVERY_SETTINGS.athena_max_statement_bytes,
            max_encoded_identity_bytes=DELIVERY_SETTINGS.max_encoded_identity_bytes,
        ),
        lease_duration_seconds=DELIVERY_SETTINGS.table_lease_duration_seconds,
        wall=_utc_now,
        monotonic_ms=_monotonic_ms,
        sleep_ms=_sleep_ms,
        logger=LOGGER,
    )


def _runtime() -> _V2Runtime:
    """Compose phase-one publication, plan-intent submission, and the composed projection path."""
    target_values = {
        "bucket": SETTINGS.semantic_v2_bucket,
        "bake workgroup": SETTINGS.semantic_v2_athena_bake_workgroup,
        "source database": SETTINGS.semantic_v2_source_database,
        "candidate database": SETTINGS.semantic_v2_candidate_database,
        "current database": SETTINGS.semantic_v2_current_database,
    }
    missing = tuple(label for label, value in target_values.items() if not value.strip())
    if missing:
        raise ValueError(
            "Semantic V2 is enabled but its generated storage target is incomplete: "
            + ", ".join(missing)
            + "."
        )
    rules = load_runtime_table_rules(ROOT / "contracts/generated/dq-rulesets.json")
    graph = load_publication_graph(
        ROOT,
        frozenset(CanonicalDatasetKey(table) for table in rules),
    )
    target = AthenaSemanticTarget(
        batch_workgroup=SETTINGS.semantic_v2_athena_bake_workgroup,
        source_database=SETTINGS.semantic_v2_source_database,
        candidate_database=SETTINGS.semantic_v2_candidate_database,
        current_database=SETTINGS.semantic_v2_current_database,
    )
    catalog = load_v2_storage_catalog(ROOT / "contracts/generated/v2-tables.json")
    storage = s3_only_semantic_storage(SETTINGS.semantic_v2_bucket, S3)
    publications = DynamoPublicationStore(
        SETTINGS.semantic_publications_table,
        SETTINGS.semantic_leases_table,
        DynamoPublicationStoreAccess(
            attempts_table=SETTINGS.semantic_attempts_table,
            inline_projection=False,
        ),
    )
    tooling = SemanticSidecarTooling(
        source_writer=storage.source,
        source_reader=storage.source,
        canonical_writer=storage.canonical,
        attempts=DynamoAttemptStore(SETTINGS.semantic_attempts_table),
        # TD-61: each lease call is bounded, so a degraded DynamoDB cannot hang the delivery.
        leases=DynamoLeaseStore(SETTINGS.semantic_leases_table, bounded_lease_client()),
        publications=publications,
        packs=load_source_packs(ROOT),
        bridge=load_commerce_identity_bridge(ROOT),
        graph=graph,
        table_rules=rules,
        columns=COLUMNS,
        catalog=load_catalog(ROOT),
        identity_evidence=storage.identity_evidence,
        taxonomy=load_media_taxonomy(ROOT),
        segment_providers=load_segment_providers(ROOT),
        canonical_reader=storage.canonical,
    )
    head_store = DynamoStage1LifecycleHeadStore(SETTINGS.semantic_publications_table, DYNAMODB)
    coordinator = DynamoProjectionCoordinator(
        SETTINGS.semantic_publications_table,
        SETTINGS.semantic_leases_table,
        head_store,
        DYNAMODB,
        graph_table=GRAPH_TABLE,
    )
    reads = Stage1WriterAuthorityReader(
        SETTINGS.semantic_publications_table,
        SETTINGS.semantic_leases_table,
        DYNAMODB,
    )
    return _V2Runtime(
        bucket=_raw_bucket(),
        tooling=tooling,
        target=target,
        catalog=catalog,
        publications=publications,
        coordinator=coordinator,
        reads=reads,
        settings=DELIVERY_SETTINGS,
        composition=COMPOSITION,
        inline=(
            _inline_runtime(coordinator, reads)
            if COMPOSITION is ProjectionComposition.INLINE
            else None
        ),
        mode_item=(
            None
            if GRAPH_TABLE is None
            else DynamoGraphDispatchStore(GRAPH_TABLE, DYNAMODB).read_mode_item
        ),
        ingress=_delivery_ingress(reads),
        materialization=GovernedMaterializationPort(_raw_bucket(), S3, fetch=_fetch_delivery),
    )


def _fetch_delivery(client: Any, bucket: str, manifest: ManifestInput) -> LoadedDelivery:
    """Fetch the exact delivery through the landing module, resolved at call time."""
    return fetch_v2_delivery(client, bucket, manifest)


@lru_cache(maxsize=1)
def _own_role_arn() -> str:
    """Return the handler's own role session ARN, read once per container (no grant needed)."""
    return str(boto3.client("sts").get_caller_identity()["Arn"])


def _set_visibility(queue_url: str, receipt_handle: str, seconds: int) -> None:
    SQS.change_message_visibility(
        QueueUrl=queue_url, ReceiptHandle=receipt_handle, VisibilityTimeout=seconds
    )


def _delivery_ingress(reads: Stage1WriterAuthorityReader) -> DeliveryIngress:
    """Compose the per-message mode read and the admission call (S6, V-19)."""
    if GRAPH_TABLE is None:
        return DeliveryIngress(
            mode=lambda: ModeState(AuthorityMode.LEGACY, 0),
            admit=None,
            visibility=_set_visibility,
            requester=_own_role_arn,
            logger=LOGGER,
        )
    store = DynamoExecutionGraphStore(GRAPH_TABLE, DYNAMODB)
    deps = AdmissionDependencies(
        store=store,
        manifests=S3RunRequestManifests(_raw_bucket(), S3),
        now=_utc_now,
        legacy=S3CoordinatorLegacyHead(_raw_bucket(), S3, reads),
    )

    def admit(request: RunRequest) -> ReceiptRecord:
        return admit_run_request(request, deps)

    return DeliveryIngress(
        mode=store.read_mode,
        admit=admit,
        visibility=_set_visibility,
        requester=_own_role_arn,
        logger=LOGGER,
    )


RUNTIME = _runtime()


def _remaining_budget(context: object) -> Callable[[], int]:
    """Bind the Lambda remaining-time budget lazily; only an inline run ever consumes it."""

    def remaining() -> int:
        method = getattr(context, "get_remaining_time_in_millis", None)
        if not callable(method):
            raise InvariantError(
                INLINE_RUN_FAILED,
                "The inline delivery composition requires the Lambda remaining-time budget.",
            )
        return int(cast("Callable[[], int]", method)())

    return remaining


@trace_boundary(INGEST_HANDLER_PROCESS)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: object) -> dict[str, list[dict[str, str]]]:
    """Load every delivery this message names.

    S6: returns the partial-batch response. The batch size is still one; the response exists
    so a delivery the drain leaves waiting (`LEGACY_DRAINING`, V-19) stays unacknowledged
    without failing the invocation: its visibility is already set to the drain-wait expiry.

    Raises on anything it could not decide, which is what puts the message back on the queue. The
    §5.3 classifier acknowledges a delivery only by returning; every unproved outcome raises.
    """
    budget = _remaining_budget(context)
    with span(INGEST_HANDLER_PROCESS, operation="ingest.handler") as process_span:
        boundary_report = Report(
            operation="ingest.handler",
            correlation_id="",
            logger=LOGGER,
        )
        try:
            messages = delivery_messages(event)
        except Exception as error:
            report_error(error, boundary_report)
            raise
        process_span.record(record_count=len(messages))
        waiting = [
            {"itemIdentifier": message.message_id}
            for message in messages
            if not _route(message, budget)
        ]
        process_span.record(outcome="completed")
    return {"batchItemFailures": waiting}


@dataclass(frozen=True, slots=True)
class IngestionMessage:
    """Exact bucket and marker selection plus one SQS delivery attempt's lineage."""

    bucket: str
    manifest: ManifestInput
    execution_id: str
    predecessor_execution_id: str | None
    message_id: str = ""
    receipt_handle: str = ""
    receive_count: int = 1
    queue_url: str = ""
    """S6: where the drain-wait visibility change goes (from the record's eventSourceARN)."""

    def ref(self) -> DeliveryRef:
        """Return the ingress view of this message."""
        return DeliveryRef(
            manifest_key=self.manifest.key,
            version_id=self.manifest.version_id,
            receipt_handle=self.receipt_handle,
            receive_count=self.receive_count,
            message_id=self.message_id,
            queue_url=self.queue_url,
        )


def _execution_id(message_id: str, receive_count: int) -> str:
    payload = f"{message_id}|{receive_count}"
    return f"sqs-{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def _required_mapping(value: object, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(message)
    return cast("dict[str, Any]", value)


def _required_text(value: object, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    return value


def _receive_count(record: dict[str, Any]) -> int:
    attributes = _required_mapping(
        record.get("attributes"),
        "A manifest marker requires a positive integer ApproximateReceiveCount.",
    )
    receive = _required_text(
        attributes.get("ApproximateReceiveCount"),
        "A manifest marker requires a positive integer ApproximateReceiveCount.",
    )
    if not receive.isdecimal() or int(receive) < 1:
        raise ValueError("A manifest marker requires a positive integer ApproximateReceiveCount.")
    return int(receive)


def _queue_url(record: dict[str, Any]) -> str:
    if not record.get("eventSourceARN"):
        return ""
    return queue_url_from_arn(str(record["eventSourceARN"]))


def _graph_delivery(
    record: dict[str, Any], body: str, envelope: dict[str, Any]
) -> GraphEnvelopeDelivery:
    """S6 §2.4: one graph envelope on the delivery route (a body with the binding fields)."""
    attributes = _required_mapping(
        record.get("attributes"),
        "A graph envelope requires a positive integer ApproximateReceiveCount.",
    )
    return graph_envelope_delivery(
        body,
        envelope,
        {str(name): str(value) for name, value in attributes.items()},
        message_id=_required_text(
            record.get("messageId"), "A graph envelope requires a nonblank SQS messageId."
        ),
        receipt_handle=str(record.get("receiptHandle", "")),
        receive_count=_receive_count(record),
        queue_url=_queue_url(record),
    )


def delivery_messages(
    event: dict[str, Any],
) -> tuple[IngestionMessage | GraphEnvelopeDelivery, ...]:
    """Parse every record: an S3 manifest marker, or (S6 §2.4) a graph envelope.

    A body carrying the three dispatch-binding fields is a graph envelope; anything else is an
    S3 event, read exactly as before and failing closed on malformed marker correlation.
    """
    records_value = event.get("Records", [])
    if not isinstance(records_value, list):
        raise ValueError("An ingestion event requires a Records list.")
    records = cast("list[object]", records_value)
    found: list[IngestionMessage | GraphEnvelopeDelivery] = []
    for value in records:
        record = _required_mapping(value, "An ingestion record must be an object.")
        body = _required_text(
            record.get("body"),
            "An ingestion record body must be a JSON string.",
        )
        envelope = _required_mapping(
            json.loads(body),
            "An ingestion record body must contain an EventBridge object.",
        )
        if is_graph_envelope(envelope):
            found.append(_graph_delivery(record, body, envelope))
            continue
        detail = _required_mapping(
            envelope.get("detail"),
            "An ingestion message carried no S3 object detail.",
        )
        object_detail = _required_mapping(
            detail.get("object"),
            "An ingestion message carried no S3 object.",
        )
        key = _required_text(
            object_detail.get("key"),
            "An ingestion message carried no S3 object key.",
        )
        bucket_detail = _required_mapping(
            detail.get("bucket"),
            "An ingestion message carried no S3 bucket name.",
        )
        bucket = _required_text(
            bucket_detail.get("name"),
            "An ingestion message carried no S3 bucket name.",
        )
        if not key.endswith(f"/{MANIFEST_NAME}"):
            report_operation(
                OperationLog(
                    operation="ingest.handler",
                    component="ingest_handler",
                    phase="filtering",
                    outcome="ignored",
                    severity=LogSeverity.WARNING,
                    description="Skipped an object that is not a delivery marker.",
                    details={"expected_suffix": MANIFEST_NAME},
                ),
                LOGGER,
            )
            continue
        version_id = _required_text(
            object_detail.get("version-id"),
            "A manifest marker requires a nonblank object VersionId.",
        )
        message_id = _required_text(
            record.get("messageId"),
            "A manifest marker requires a nonblank SQS messageId.",
        )
        receive_count = _receive_count(record)
        found.append(
            IngestionMessage(
                bucket=bucket,
                manifest=ManifestInput(key, version_id),
                execution_id=_execution_id(message_id, receive_count),
                predecessor_execution_id=(
                    None if receive_count == 1 else _execution_id(message_id, receive_count - 1)
                ),
                message_id=message_id,
                receipt_handle=str(record.get("receiptHandle", "")),
                receive_count=receive_count,
                queue_url=_queue_url(record),
            )
        )
    return tuple(found)


def ingestion_messages(event: dict[str, Any]) -> tuple[IngestionMessage, ...]:
    """Parse exact manifest markers (graph envelopes are routed separately, S6 §2.4)."""
    return tuple(
        message for message in delivery_messages(event) if isinstance(message, IngestionMessage)
    )


def manifest_keys(event: dict[str, Any]) -> tuple[str, ...]:
    """Compatibility projection of the exact typed ingestion messages."""
    return tuple(message.manifest.key for message in ingestion_messages(event))


@dataclass(frozen=True, slots=True)
class _V2Outcome:
    """Typed V2 reporting payload."""

    operation_id: str
    semantic_operation_id: str
    plan_digest: str
    results: tuple[SemanticSidecarResult, ...]
    write_plan_key: str
    intent_id: str | None = None
    acknowledgment: ProvedSourceOutcome | None = None
    # The owner-persisted public (flow_id, execution_id) pair of every inline run this
    # invocation advanced; the structured terminal carries them so the §13 reporter's
    # transport family joins by exact identity (never a timestamp or heuristic).
    run_flow_bindings: tuple[tuple[str, str, str | None], ...] = ()


def _message_operation_id(message: IngestionMessage) -> str:
    payload = json.dumps(
        (message.bucket, message.manifest.key, message.manifest.version_id),
        separators=(",", ":"),
    )
    return f"operation-{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


@dataclass(frozen=True, slots=True)
class _DeliveryIdentity:
    """The §3 source-delivery identity pair derived from the exact manifest (key, version)."""

    origin_source_identity: str
    plan_digest: str


def _delivery_identity(message: IngestionMessage) -> _DeliveryIdentity:
    """Derive ``origin_source_identity`` and ``plan_digest`` through the sole §3 constructors."""
    origin = source_delivery_identity(((message.manifest.key, message.manifest.version_id),))
    return _DeliveryIdentity(origin, plan_digest_for_source_delivery(origin))


def _manifest_ref(delivery: LoadedDelivery) -> str:
    """Render one canonical reference to the exact versioned manifest evidence."""
    landed = delivery.landed_ref
    return json.dumps(
        {
            "checksum": landed.manifest_checksum,
            "key": landed.manifest_key,
            "version": landed.manifest_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _with_write_plan_key(
    results: tuple[SemanticSidecarResult, ...], plan_key: str
) -> tuple[SemanticSidecarResult, ...]:
    """Correlate every returned receipt once its projection disposition is durable."""
    return tuple(
        replace(
            result,
            receipts=tuple(
                replace(receipt, write_plan_key=plan_key) for receipt in result.receipts
            ),
        )
        for result in results
    )


@dataclass(frozen=True, slots=True)
class _ZeroRowServabilityRequest:
    """Lifecycle identities for direct servability of rowless publications."""

    results: tuple[SemanticSidecarResult, ...]
    now: datetime
    operation_id: str
    execution_id: str
    predecessor_execution_id: str | None
    plan_digest: str
    manifest_ref: str


def _advance_zero_row_publications(
    runtime: _V2Runtime,
    request: _ZeroRowServabilityRequest,
) -> None:
    """Make committed withheld-only publications servable without projection work.

    Transitional: §2.1 rule 4 seeds zero-row servability inside the ``commit_group``
    transaction itself, which the publication store does not yet do; until it does, this
    monotonic follow-up keeps rowless current publications servable.
    """
    for result in request.results:
        for receipt in result.receipts:
            successful = receipt.successful
            if successful is None or successful.rows:
                continue
            publication = cast("SuccessfulPublication", successful.publication)
            advanced = runtime.publications.advance_servable(publication, at=request.now)
            report_operation(
                OperationLog(
                    operation="semantic_projection_servable",
                    component="ingest_handler",
                    phase="servable",
                    outcome="completed" if advanced else "projection_superseded",
                    severity=(LogSeverity.INFO if advanced else LogSeverity.WARNING),
                    details={
                        "flow_id": request.operation_id,
                        "causation_id": request.manifest_ref,
                        "operation_id": request.operation_id,
                        "execution_id": request.execution_id,
                        "predecessor_execution_id": request.predecessor_execution_id or "none",
                        "plan_digest": request.plan_digest,
                        "plan_key": NO_PLAN_KEY,
                        "unit_key": INLINE_PLAN_KEY,
                        "partition": publication.partition.identity,
                        "publication_id": publication.publication_id.value,
                        "projected_at": request.now.isoformat(),
                    },
                ),
                LOGGER,
            )


# --- Plan intent (queued composition) -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _IntentSubmission:
    """Everything one ``SubmitPlanIntentRequest`` derives from besides the owner snapshot."""

    identity: _DeliveryIdentity
    roster: DeliveryRoster
    now: datetime


def _submit_plan_intent(
    runtime: _V2Runtime, submission: _IntentSubmission
) -> SubmitPlanIntentResult:
    """Strongly read the owner snapshot and submit the sole canonical intent (§2.1 rule 6)."""
    plan_digest = submission.identity.plan_digest
    owner = runtime.reads.owner(plan_digest)
    request = SubmitPlanIntentRequest(
        plan_digest=plan_digest,
        root_plan_key=None if owner is None else owner.root_plan_key,
        source_plan_key=None,
        origin=IntentOrigin(
            IntentOriginKind.SOURCE_DELIVERY, submission.identity.origin_source_identity
        ),
        units=submission.roster.entries,
        operation_instant=instant(submission.now),
        owner=owner,
        # V-13(b): a new root commits only while the mode is LEGACY or SHADOW, so the drain CAS
        # cannot slip in between this handler's mode read and its submission.
        mode_guard=(
            None
            if runtime.mode_item is None
            else AuthorityModeGuard(ModeGuardKind.NEW_ROOT_ADMITTING, runtime.mode_item())
        ),
    )
    return runtime.coordinator.submit_plan_intent(request)


def _plan_head(
    runtime: _V2Runtime, plan_digest: str, plan: PlanMeta | None
) -> Stage1LifecycleHeadTuple | None:
    if plan is None:
        return None
    identity = derive_plan_identity(PlanIdentityRequest(plan_digest, plan.internal_execution_id))
    return runtime.reads.head(identity.flow_id)


def _acknowledge_source_delivery(
    runtime: _V2Runtime, plan_digest: str, intent_id: str
) -> ProvedSourceOutcome:
    """Perform the closed §5.3 strong read and prove exactly one acknowledgment outcome."""
    intent = runtime.reads.intent(plan_digest, intent_id)
    owner = runtime.reads.owner(plan_digest)
    active_plan: PlanMeta | None = None
    last_plan: PlanMeta | None = None
    if owner is not None and owner.active_plan_key is not None:
        active_plan = runtime.reads.plan_meta(owner.active_plan_key)
    elif owner is not None and owner.last_plan_key is not None:
        last_plan = runtime.reads.plan_meta(owner.last_plan_key)
    return classify_source_delivery(
        SourceDeliveryObservation(
            intent_id=intent_id,
            intent=intent,
            owner=owner,
            active_plan=active_plan,
            active_head=_plan_head(runtime, plan_digest, active_plan),
            last_plan=last_plan,
            last_head=_plan_head(runtime, plan_digest, last_plan),
        )
    )


# --- Inline runs (transitional inline composition) ------------------------------------------------


@dataclass(frozen=True, slots=True)
class _InlineDelivery:
    """The identities and budget every inline table run of one delivery shares."""

    roster: DeliveryRoster
    operation_id: str
    execution_id: str
    remaining_budget_ms: Callable[[], int]


def _run_inline_tables(
    runtime: _V2Runtime, delivery: _InlineDelivery
) -> tuple[InlineRunResult, ...]:
    """Drive every physical table inline, one at a time in lexical order (§9)."""
    inline = runtime.inline
    if inline is None:
        raise InvariantError(INLINE_RUN_FAILED, "The queued composition never runs a table inline.")
    results: list[InlineRunResult] = []
    for unit in delivery.roster.units:
        result = run_inline_table(
            inline,
            InlineRunRequest(
                writer_kind=WriterKind.TRANSITIONAL_DELIVERY,
                operation_id=delivery.operation_id,
                execution_id=delivery.execution_id,
                physical_table_id=unit.entry.unit_key,
                table_kind=unit.entry.table_kind,
                workload=unit.workload,
                composition=unit.composition,
                remaining_budget_ms=delivery.remaining_budget_ms,
                servable=unit.servable,
            ),
        )
        if result.retryable:
            raise DependencyError(
                INLINE_RUN_RETRYABLE,
                "An inline table run returned a typed retryable outcome; the delivery retries.",
                context={"table": unit.entry.unit_key, "outcome": result.outcome},
            )
        if result.outcome != InlineRunOutcome.FINALIZED:
            raise InvariantError(
                INLINE_RUN_FAILED,
                "An inline table run failed after authority was published.",
                context={"table": unit.entry.unit_key, "outcome": result.outcome},
            )
        results.append(result)
    return tuple(results)


# --- One delivery ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Correlation:
    """The safe identities every progress and terminal record of one delivery carries."""

    operation_id: str
    execution_id: str
    predecessor_execution_id: str | None
    plan_digest: str

    def details(self, **extra: object) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "execution_id": self.execution_id,
            "predecessor_execution_id": self.predecessor_execution_id or "none",
            "plan_digest": self.plan_digest,
            **extra,
        }


def _report_progress(correlation: _Correlation, event: SemanticSidecarProgress) -> None:
    report_operation(
        OperationLog(
            operation="ingest_delivery",
            component="ingest_handler",
            phase="progress",
            outcome="completed",
            severity=LogSeverity.INFO,
            details=correlation.details(
                stage=event.stage,
                completed=event.completed,
                total=event.total,
                contract_ids=event.contract_ids,
            ),
        ),
        LOGGER,
    )


def _feasibility(  # noqa: PLR0913, PLR0917 -- the §2.1 check's inputs plus the GRAPH run
    runtime: _V2Runtime,
    roster: DeliveryRoster,
    groups: tuple[tuple[tuple[str, str], ...], ...],
    run: RunRecord | None,
) -> FeasibilityCheck:
    """Build the §2.1 check; in GRAPH (a run is given) with the 11-unit and envelope bounds."""
    if run is None:
        return FeasibilityCheck(roster=roster, groups=groups, settings=runtime.settings)
    return FeasibilityCheck(
        roster=roster,
        groups=groups,
        settings=runtime.settings,
        max_units=GRAPH_MAX_UNITS,
        envelope=unit_envelope(run),
    )


def _prove_feasible(  # noqa: PLR0913, PLR0917 -- the §2.1 callback plus the GRAPH run
    runtime: _V2Runtime,
    now: datetime,
    plan: PreparedProjectionPlan,
    run: RunRecord | None = None,
) -> None:
    """Refuse before mutation whenever the prepared roster is infeasible (the §2.1 callback)."""
    roster = prepared_roster(
        PreparedRosterRequest(
            plan=plan,
            target=runtime.target,
            catalog=runtime.catalog,
            now=now,
            max_statement_bytes=runtime.settings.athena_max_statement_bytes,
        )
    )
    require_feasible(_feasibility(runtime, roster, plan.groups, run))


def _published_units(
    runtime: _V2Runtime,
    results: tuple[SemanticSidecarResult, ...],
    run: RunRecord | None = None,
) -> DeliveryRoster:
    """Build the exact published roster and re-prove it; nothing outside the proof may persist."""
    roster = published_roster(
        PublishedRosterRequest(
            results=results,
            target=runtime.target,
            catalog=runtime.catalog,
            max_statement_bytes=runtime.settings.athena_max_statement_bytes,
        )
    )
    try:
        require_feasible(_feasibility(runtime, roster, (), run))
    except AdlyticsError as error:
        raise InvariantError(
            POST_AUTHORITY_INFEASIBLE,
            "The published roster encodes outside the pre-authority feasibility proof.",
            context={"cause": str(error.code)},
        ) from error
    return roster


@dataclass(frozen=True, slots=True)
class _IntentReport:
    """The §10 phase-1 telemetry: intent identity, unit count, ordinal, acknowledgment digest."""

    correlation: _Correlation
    submitted: SubmitPlanIntentResult
    acknowledgment: ProvedSourceOutcome
    unit_count: int


def _report_intent(report: _IntentReport) -> None:
    report_operation(
        OperationLog(
            operation="semantic_plan_intent_submitted",
            component="ingest_handler",
            phase="intent",
            outcome=report.submitted.outcome.value,
            severity=LogSeverity.INFO,
            details=report.correlation.details(
                intent_id=report.submitted.intent_id,
                roster_digest=report.submitted.roster_digest,
                unit_count=report.unit_count,
                proved_outcome_ordinal=report.acknowledgment.ordinal,
                owner_revision=report.acknowledgment.owner_revision,
                source_acknowledgment_digest=report.acknowledgment.acknowledgment_digest,
            ),
        ),
        LOGGER,
    )


@trace_boundary(INGEST_FETCH)
def _fetch(
    message: IngestionMessage,
    correlation: _Correlation,
    claim: MaterializationClaim | None = None,
) -> LoadedDelivery:
    """Fetch the exact delivery under its `INGEST_FETCH` span (legacy, or nested in GRAPH)."""
    with span(
        INGEST_FETCH,
        operation_id=correlation.operation_id,
        execution_id=message.execution_id,
        predecessor_execution_id=message.predecessor_execution_id or "none",
        plan_digest=correlation.plan_digest,
        stage="fetch",
        completed=0,
        total=1,
    ) as fetch_span:
        # S6 (hard deny (d)): the governed port refuses a historical manifest before any
        # data-object read, in every mode; a GRAPH caller's claim is re-attested there too.
        delivery = (
            RUNTIME.materialization.fetch(message.manifest)
            if claim is None
            else RUNTIME.materialization.fetch(message.manifest, claim)
        )
        fetch_span.record(
            completed=1,
            outcome="completed",
        )
    return delivery


def _correlation(message: IngestionMessage) -> tuple[_DeliveryIdentity, _Correlation]:
    identity = _delivery_identity(message)
    return identity, _Correlation(
        operation_id=_message_operation_id(message),
        execution_id=message.execution_id,
        predecessor_execution_id=message.predecessor_execution_id,
        plan_digest=identity.plan_digest,
    )


def _execute(
    message: IngestionMessage, now: datetime, remaining_budget_ms: Callable[[], int]
) -> _V2Outcome:
    """Fetch exact immutable evidence, publish it through Semantic V2, and hand off projection."""
    if message.bucket != RUNTIME.bucket:
        raise ValueError("An ingestion marker names a bucket outside this runtime.")
    identity, correlation = _correlation(message)
    operation_id = correlation.operation_id
    delivery = _fetch(message, correlation)
    semantic_operation_id = v2_operation_id(delivery)

    results = ingest_semantic_delivery(
        SemanticIngestionRequest(
            delivery=delivery,
            contracts=CONTRACTS,
            columns=COLUMNS,
            now=now,
            operation_id=semantic_operation_id,
            execution_id=message.execution_id,
            plan_digest=identity.plan_digest,
            predecessor_execution_id=message.predecessor_execution_id,
            telemetry_operation_id=operation_id,
            installed_governance=INSTALLED_GOVERNANCE,
            discard_policies=INSTALLED_DISCARD_POLICIES,
            progress=lambda event: _report_progress(correlation, event),
            feasibility=lambda plan: _prove_feasible(RUNTIME, now, plan),
        ),
        RUNTIME.tooling,
    )
    _advance_zero_row_publications(
        RUNTIME,
        _ZeroRowServabilityRequest(
            results=results,
            now=now,
            operation_id=operation_id,
            execution_id=message.execution_id,
            predecessor_execution_id=message.predecessor_execution_id,
            plan_digest=identity.plan_digest,
            manifest_ref=_manifest_ref(delivery),
        ),
    )
    roster = _published_units(RUNTIME, results)
    outcome = _V2Outcome(
        operation_id=operation_id,
        semantic_operation_id=semantic_operation_id,
        plan_digest=identity.plan_digest,
        results=results,
        write_plan_key=NO_PLAN_KEY,
    )
    if not roster.units:
        return replace(outcome, results=_with_write_plan_key(results, NO_PLAN_KEY))
    if RUNTIME.composition is ProjectionComposition.INLINE:
        inline_results = _run_inline_tables(
            RUNTIME,
            _InlineDelivery(
                roster=roster,
                operation_id=semantic_operation_id,
                execution_id=message.execution_id,
                remaining_budget_ms=remaining_budget_ms,
            ),
        )
        return replace(
            outcome,
            results=_with_write_plan_key(results, INLINE_PLAN_KEY),
            write_plan_key=INLINE_PLAN_KEY,
            run_flow_bindings=tuple(
                (result.flow_id, result.execution_id, result.predecessor_execution_id)
                for result in inline_results
            ),
        )
    submitted = _submit_plan_intent(RUNTIME, _IntentSubmission(identity, roster, now))
    acknowledgment = _acknowledge_source_delivery(
        RUNTIME, identity.plan_digest, submitted.intent_id
    )
    _report_intent(_IntentReport(correlation, submitted, acknowledgment, len(roster.units)))
    intent_key = intent_partition_key(identity.plan_digest, submitted.intent_id)
    return replace(
        outcome,
        results=_with_write_plan_key(results, intent_key),
        write_plan_key=intent_key,
        intent_id=submitted.intent_id,
        acknowledgment=acknowledgment,
    )


# --- GRAPH: the materialize_raw node (S6 §2.4) ----------------------------------------------------


def _zero_row_targets(results: tuple[SemanticSidecarResult, ...]) -> tuple[ZeroRowTarget, ...]:
    """Return the committed rowless publications `publish` stages; GRAPH never advances them."""
    targets: list[ZeroRowTarget] = []
    for result in results:
        for receipt in result.receipts:
            successful = receipt.successful
            if successful is None or successful.rows:
                continue
            publication = cast("SuccessfulPublication", successful.publication)
            targets.append(
                ZeroRowTarget(
                    partition=publication.partition.identity,
                    publication_id=publication.publication_id.value,
                    fencing_token=publication.fencing_token,
                )
            )
    return tuple(targets)


def _materialize_graph(request: MaterializeRequest) -> GraphMaterialization:
    """Fetch under the re-attested claim and publish authority with nothing servable advanced.

    The publication store is composed `inline_projection=False`, so a commit advances nothing
    servable; `_advance_zero_row_publications` and `_submit_plan_intent` are not called (their
    targets go to the effect record, and `publish` stages them). The INLINE branch is
    unreachable: this path never reads the projection composition.
    """
    predecessor = (
        None
        if request.receive_count == 1
        else _execution_id(request.message_id, request.receive_count - 1)
    )
    message = IngestionMessage(
        bucket=RUNTIME.bucket,
        manifest=request.manifest,
        execution_id=_execution_id(request.message_id, request.receive_count),
        predecessor_execution_id=predecessor,
        message_id=request.message_id,
        receive_count=request.receive_count,
    )
    identity, correlation = _correlation(message)
    delivery = _fetch(message, correlation, request.claim)
    semantic_operation_id = v2_operation_id(delivery)
    results = ingest_semantic_delivery(
        SemanticIngestionRequest(
            delivery=delivery,
            contracts=CONTRACTS,
            columns=COLUMNS,
            now=request.now,
            operation_id=semantic_operation_id,
            execution_id=message.execution_id,
            plan_digest=identity.plan_digest,
            predecessor_execution_id=predecessor,
            telemetry_operation_id=correlation.operation_id,
            installed_governance=INSTALLED_GOVERNANCE,
            # The same installed packs as the queued path: without them every `records_cell_state`
            # contract is refused CLOSED_ENUMERATION and no automatic variant is read.
            discard_policies=INSTALLED_DISCARD_POLICIES,
            progress=lambda event: _report_progress(correlation, event),
            feasibility=lambda plan: _prove_feasible(RUNTIME, request.now, plan, request.run),
        ),
        RUNTIME.tooling,
    )
    roster = _published_units(RUNTIME, results, request.run)
    outcome = _V2Outcome(
        operation_id=correlation.operation_id,
        semantic_operation_id=semantic_operation_id,
        plan_digest=identity.plan_digest,
        results=_with_write_plan_key(results, request.node_id),
        write_plan_key=request.node_id,
    )

    def report_terminal(elapsed_ms: float) -> None:
        _report_v2(
            request.manifest.key,
            outcome,
            elapsed_ms=elapsed_ms,
            receive_count=request.receive_count,
        )

    return GraphMaterialization(
        units=roster.entries,
        zero_row_targets=_zero_row_targets(results),
        report_terminal=report_terminal,
    )


def _count_delivery() -> None:
    METRICS.add_metric(name=DELIVERY_METRIC, unit=MetricUnit.Count, value=1)


def _graph_runtime() -> MaterializeRuntime | None:
    """Compose the GRAPH materialize path; None where the graph authority is not provisioned."""
    if GRAPH_TABLE is None:
        return None
    store = DynamoGraphDispatchStore(GRAPH_TABLE, DYNAMODB)

    def mode() -> AuthorityMode:
        return store.read_mode().mode

    return MaterializeRuntime(
        store=store,
        mode=mode,
        materialize=_materialize_graph,
        visibility=_set_visibility,
        lease=DELIVERY_CLAIM_LEASE,
        logger=LOGGER,
        now=_utc_now,
        count_delivery=_count_delivery,
    )


GRAPH = _graph_runtime()


def _load_graph(delivery: GraphEnvelopeDelivery) -> bool:
    """Run one graph envelope; without a graph authority it is refused with zero effect."""
    graph = GRAPH
    if graph is None:
        report_error(
            GraphEnvelopeRefusedError(GRAPH_DISPATCH_NOT_ACTIVE),
            Report(operation="ingest_delivery", correlation_id=delivery.node_id, logger=LOGGER),
        )
        return False
    return materialize_graph_envelope(graph, delivery)


def _route(
    message: IngestionMessage | GraphEnvelopeDelivery, remaining_budget_ms: Callable[[], int]
) -> bool:
    """Route a graph envelope to the GRAPH materialize path and an S3 event to ingress."""
    if isinstance(message, GraphEnvelopeDelivery):
        return _load_graph(message)
    return _load(message, remaining_budget_ms)


def _load(message: IngestionMessage, remaining_budget_ms: Callable[[], int]) -> bool:
    """Route one message by one strong mode read (V-19); return whether it is acknowledged."""
    ingress = RUNTIME.ingress
    routed = ingress.mode()
    mode = routed.mode
    # V-17b: this handler takes no legacy path after a LEGACY_DRAINING read, so it never
    # counts; the call keeps every legacy writer's per-invocation read explicit.
    RUNTIME.coordinator.count_drain(mode)
    path = ingress_path(mode)
    if path is IngressPath.DRAIN_WAIT:
        drain_wait(ingress, message.ref(), routed)
        return False
    if path is IngressPath.GRAPH:
        graph_admit(ingress, message.ref())
        return True
    try:
        _load_legacy(message, remaining_budget_ms)
    except GraphAuthorityRefusedError as refused:
        if refused.signal != AUTHORITY_MODE_DRAINING:
            raise
        # V-19(d): the drain CAS committed after this message read LEGACY or SHADOW. One more
        # read supplies `D`; if the drain has already ended, the next receive re-routes it.
        current = ingress.mode()
        if current.mode is AuthorityMode.LEGACY_DRAINING and current.drain_deadline is not None:
            drain_wait(ingress, message.ref(), current)
        else:
            drain_ended(ingress, message.ref(), current)
        return False
    if path is IngressPath.LEGACY_THEN_SHADOW:
        shadow_record(ingress, message.ref())
    return True


def _load_legacy(message: IngestionMessage, remaining_budget_ms: Callable[[], int]) -> None:
    """Load one exact event-selected delivery through the V2 write plane."""
    manifest_key = message.manifest.key
    report = Report(
        operation="ingest_delivery",
        correlation_id=message.execution_id,
        logger=LOGGER,
    )
    METRICS.add_metric(name=DELIVERY_METRIC, unit=MetricUnit.Count, value=1)

    started = time.monotonic()
    try:
        outcome = _execute(message, datetime.now(tz=UTC), remaining_budget_ms)
    except GraphAuthorityRefusedError as refused:
        if refused.signal == AUTHORITY_MODE_DRAINING:
            # V-19(d): not a failure; `_load` takes the drain-wait path without a report.
            raise
        report_error(refused, report)
        raise
    except AdlyticsError as error:
        # Classified, so it is counted with its class and code before it propagates. It still
        # propagates: unlike a tool call there is no caller to hand a refusal to, and the queue's
        # retry is the recovery.
        report_error(error, report)
        raise
    except Exception as error:
        report_error(error, report)
        raise

    _report_v2(
        manifest_key,
        outcome,
        elapsed_ms=(time.monotonic() - started) * 1000.0,
        receive_count=message.receive_count,
    )


def _report_v2(  # noqa: PLR0913 -- the terminal record's four independent facts
    manifest_key: str, outcome: _V2Outcome, *, elapsed_ms: float, receive_count: int
) -> None:
    """Count and log typed Semantic V2 decisions.

    The terminal record carries the SQS receive that completed it, so a drain-wait readback
    reads the receive count instead of inferring it (S6-8).
    """
    for result in outcome.results:
        with single_metric(
            name=PARTITION_METRIC,
            unit=MetricUnit.Count,
            value=1,
            namespace=METRICS_NAMESPACE,
        ) as metric:
            metric.add_dimension(name="Action", value=result.outcome.value)
    acknowledgment = outcome.acknowledgment
    report_operation(
        OperationLog(
            operation="ingest_delivery",
            component="ingest_handler",
            phase="terminal",
            outcome="completed",
            severity=LogSeverity.INFO,
            description="Loaded a Semantic V2 delivery.",
            details={
                "manifest_key": manifest_key,
                "receive_count": receive_count,
                "operation_id": outcome.operation_id,
                "semantic_operation_id": outcome.semantic_operation_id,
                "plan_digest": outcome.plan_digest,
                "write_plan_key": outcome.write_plan_key,
                "intent_id": outcome.intent_id or "none",
                "proved_outcome_ordinal": (
                    acknowledgment.ordinal if acknowledgment is not None else 0
                ),
                "source_acknowledgment_digest": (
                    acknowledgment.acknowledgment_digest if acknowledgment is not None else "none"
                ),
                "contracts": len(outcome.results),
                "outcomes": sorted({result.outcome.value for result in outcome.results}),
                "publications": sum(len(result.publications) for result in outcome.results),
                # §13 transport family: this invocation's own duration plus the owner-persisted
                # public identity of every inline run it advanced, joined by exact identity only.
                "elapsed_ms": round(elapsed_ms, 3),
                "run_flows": [
                    {
                        "flow_id": flow_id,
                        "execution_id": execution_id,
                        "predecessor_execution_id": predecessor_execution_id,
                    }
                    for flow_id, execution_id, predecessor_execution_id in outcome.run_flow_bindings
                ],
            },
        ),
        LOGGER,
    )
