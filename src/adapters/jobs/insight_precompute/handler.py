# SPDX-License-Identifier: MIT-0
# ruff: noqa: E501, PLR0911
"""Bounded, retained-state-driven publication-stream insight precomputation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Protocol, cast

import boto3
from aws_lambda_powertools import Logger

from adapters.campaign_measurement import (
    CampaignArtifactMeasurementService,
    CampaignMeasurementDependencies,
)
from adapters.config import graph_consumer_settings, insight_precompute_settings, tracing_settings
from adapters.inbound.presentation_handlers.insights import (
    InsightAssemblyRequest,
    InsightSlotAssembler,
)
from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.inbound.tool_handlers.measurement import MeasurementTooling
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.execution_graph.cutover_gate import (
    CutoverGate,
    provisioned_cutover_gate,
)
from adapters.jobs.execution_graph.post_grant_consumer import (
    ConsumeResult,
    PostGrantEnvelope,
    PostGrantEnvelopeError,
    PostGrantRuntime,
    post_grant_envelope,
)
from adapters.jobs.insight_precompute.graph_precompute import GraphPrecomputeWork
from adapters.jobs.insight_precompute.materializer import (
    RetainedCampaignArtifactMaterializer,
    StagedArtifactInsightPlanner,
)
from adapters.jobs.insight_precompute.planning import (
    RetainedPublicationPlanner,
    campaign_coverage_requirements,
)
from adapters.jobs.insight_precompute.requests import CampaignRetainedPublicationRequestBuilder
from adapters.jobs.presentation_validation.loader import load_page_schemas, load_v2_page_schemas
from adapters.outbound.athena.query import AthenaQueryAdapter, AthenaQueryTarget
from adapters.outbound.bedrock.insights import BedrockInsightGenerator, InsightModelRouteModels
from adapters.outbound.catalog import PageWindowCatalog
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.execution_graph_release import DynamoGraphReleaseStore
from adapters.outbound.dynamodb.insight_cache import DynamoInsightCache
from adapters.outbound.dynamodb.insight_prompt_registry import DynamoInsightPromptRegistry
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.entity_catalog import CanonicalEntityCatalog, EntityCatalogTooling
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.operational_companions import companion_emitter
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.s3.preset_artifacts import (
    S3PresetArtifactBodyStore,
    S3PresetArtifactCustodyStore,
)
from adapters.outbound.s3.semantic_storage import S3CanonicalAdapter
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.spend_roster import SpendRosterReader, ad_product_labels
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import INSIGHT_PRECOMPUTE, span, trace_boundary
from domain.operations.companions import (
    CompanionFlowRequest,
    CompanionTerminal,
    companion_flow,
)
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_dispatch import (
    GRAPH_DISPATCH_NOT_ACTIVE,
    STREAM_TRIGGER_AFTER_CUTOVER,
)
from domain.operations.execution_graph_release import BakeRoster, CohortMember
from domain.operations.status import CompletionOutcome, FlowState, Progress
from domain.presentation.analytical_pages import (
    campaign_platform_labels,
    presentation_dimension_labels,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.publication import SuccessfulPublication
from domain.shared_kernel.errors import ErrorCode, InvariantError

if TYPE_CHECKING:
    from collections.abc import Callable

_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
LOGGER = Logger(service="adlytics-insight-precompute")
_OPERATION = INSIGHT_PRECOMPUTE
_COMPONENT = "insight_precompute"

report_operation(
    OperationLog(
        operation=_OPERATION,
        component=_COMPONENT,
        phase="start",
        outcome="active" if _TRACING else "disabled",
        severity=LogSeverity.INFO,
        details={
            "stage": "startup",
            "tracing": _TRACING,
            "sample_ratio": _TRACING_SETTINGS.sample_ratio,
        },
    ),
    LOGGER,
)


@dataclass(frozen=True, slots=True)
class PublicationTrigger:
    """Validated current-publication identity only; it carries no caller authority or windows."""

    partition_key: str
    publication_id: str


class PublicationTriggerReader(Protocol):
    """Attest a stream record against current, immutable governed publication state."""

    def current_for_trigger(
        self, partition_key: str, publication_id: str
    ) -> SuccessfulPublication | None:
        """Return the retained attested publication, or none when this delivery is superseded."""
        ...


class PublicationInsightPlanner(Protocol):
    """Derive finite, declared assembly requests only from a retained publication."""

    def requests_for(
        self, publication: SuccessfulPublication
    ) -> tuple[InsightAssemblyRequest, ...]:
        """Return bounded, ordered requests whose owner, subject, window, and revision are server-owned."""
        ...


class PrecomputeApplication(Protocol):
    """Repository-owned resolver that derives declared subjects/windows from retained publications."""

    def apply(self, trigger: PublicationTrigger) -> None:
        """Precompute the bounded declared slot roster for one validated publication."""
        ...


@dataclass(frozen=True, slots=True)
class PublicationInsightPrecomputeApplication:
    """Read-attest-plan-assemble application preserving stream partial-failure semantics."""

    reader: PublicationTriggerReader
    planner: PublicationInsightPlanner
    assembler: InsightSlotAssembler
    max_requests_per_publication: int
    graph_planner: StagedArtifactInsightPlanner | None = None
    gate: CutoverGate = field(default_factory=CutoverGate)
    """S6 §2.9 (D6): in GRAPH every stream record is dropped with zero effect and counted."""

    def __post_init__(self) -> None:
        if self.max_requests_per_publication < 1:
            raise ValueError("precompute request fan-out must be positive.")

    def apply(self, trigger: PublicationTrigger) -> None:
        """Ignore stale records; only infrastructure/state failures escape for record retry."""
        publication = self.reader.current_for_trigger(trigger.partition_key, trigger.publication_id)
        if publication is None:
            return
        requests = self.planner.requests_for(publication)
        if len(requests) > self.max_requests_per_publication:
            raise ValueError("precompute planner exceeded the configured fan-out bound.")
        if requests:
            self.assembler.many(requests)
        _emit_precompute_companion(trigger, publication, len(requests))


def _emit_precompute_companion(
    trigger: PublicationTrigger, publication: SuccessfulPublication, settled: int
) -> None:
    """Report the attested settlement decision as a deterministic two-event companion flow.

    Identity and instants derive from the re-attested publication, so a stream
    redelivery reproduces byte-identical events that deduplicate at the projection.
    """
    flow = companion_flow(
        CompanionFlowRequest(
            surface="insight_precompute",
            authority_kind="current_publication",
            identity={
                "partition": trigger.partition_key,
                "publication_id": trigger.publication_id,
            },
            started_at=publication.published_at,
            start_state=FlowState.RUNNING,
            start_stage="current_transition_observed",
        )
    )
    terminal = flow.terminal(
        CompanionTerminal(
            state=FlowState.COMPLETED,
            outcome=(
                CompletionOutcome.SUCCEEDED if settled else CompletionOutcome.COMPLETED_NO_CHANGE
            ),
            stage="cache_settlement_attested",
            occurred_at=publication.published_at,
            authority_digest_value={
                "partition": trigger.partition_key,
                "publication_id": trigger.publication_id,
                "settled": str(settled),
            },
            reason_code=None if settled else "no_eligible_slots",
            progress=Progress(settled, max(settled, 1), "slots") if settled else None,
        )
    )
    companion_emitter(_OPERATION).emit(flow.start, terminal)


def _servable_transition(image: dict[str, object], publication_id: str) -> bool:
    """Apply the servable trigger rule owned by generation coordination §3.

    A record whose ``projected_publication_id`` equals the authority publication id is the
    applied (servable) transition. An absent attribute is a legacy authority advance from
    the pre-migration window and is processed. Anything else — an authority-only advance
    or a superseded servable move — is acknowledged without work.
    """
    projected = image.get("projected_publication_id")
    if projected is None:
        return True
    if not isinstance(projected, dict):
        return False
    return cast("dict[str, object]", projected).get("S") == publication_id


def parse_record(record: object) -> PublicationTrigger | None:
    """Accept only a DynamoDB INSERT/MODIFY NEW_IMAGE CURRENT publication record."""
    if not isinstance(record, dict):
        return None
    event = cast("dict[str, object]", record)
    if event.get("eventName") not in {"INSERT", "MODIFY"}:
        return None
    raw_dynamodb = event.get("dynamodb")
    if not isinstance(raw_dynamodb, dict):
        return None
    dynamodb = cast("dict[str, object]", raw_dynamodb)
    raw_image = dynamodb.get("NewImage")
    if not isinstance(raw_image, dict):
        return None
    image = cast("dict[str, object]", raw_image)
    raw_partition, raw_record_key, raw_payload = (
        image.get("partition_key"),
        image.get("record_key"),
        image.get("payload"),
    )
    if not all(isinstance(item, dict) for item in (raw_partition, raw_record_key, raw_payload)):
        return None
    partition = cast("dict[str, object]", raw_partition)
    record_key = cast("dict[str, object]", raw_record_key)
    payload = cast("dict[str, object]", raw_payload)
    partition_text, record_text, payload_text = (
        partition.get("S"),
        record_key.get("S"),
        payload.get("S"),
    )
    if (
        not isinstance(partition_text, str)
        or not partition_text
        or record_text != "CURRENT"
        or not isinstance(payload_text, str)
    ):
        return None
    try:
        decoded: object = json.loads(payload_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    publication_id = cast("dict[str, object]", decoded).get("publication_id")
    if not isinstance(publication_id, str) or not publication_id:
        return None
    if not _servable_transition(image, publication_id):
        return None
    return PublicationTrigger(partition_text, publication_id)


def _report_failure(error: BaseException) -> None:
    report_error(error, Report(operation=_OPERATION, correlation_id="", logger=LOGGER))


def apply_records(
    event: object,
    application: PrecomputeApplication,
    *,
    after_cutover: bool | None = None,
) -> tuple[str, ...]:
    """Apply accepted records in delivery order; return failed event ids for partial-batch retry.

    S6 §2.9 (D6): the mode is read once per batch, before any record is parsed (the handler
    passes its read as `after_cutover`); in GRAPH the batch has zero effect and is counted.
    """
    if not isinstance(event, dict):
        return ()
    records = cast("dict[str, object]", event).get("Records")
    if not isinstance(records, list):
        return ()
    if after_cutover is None:
        gate = getattr(application, "gate", None)
        after_cutover = isinstance(gate, CutoverGate) and gate.closed(
            STREAM_TRIGGER_AFTER_CUTOVER, len(cast("list[object]", records))
        )
    if after_cutover:
        return ()
    failures: list[str] = []
    for record in cast("list[object]", records):
        trigger = parse_record(record)
        if trigger is None:
            continue
        try:
            application.apply(trigger)
        except Exception as error:
            _report_failure(error)
            if isinstance(record, dict):
                event_id = cast("dict[str, object]", record).get("eventID")
                if isinstance(event_id, str):
                    failures.append(event_id)
    return tuple(failures)


# --- S6 §2.8: the graph precompute FIFO ------------------------------------------------------

GRAPH_PRECOMPUTE_OUTSIDE_GRAPH = ErrorCode("GRAPH_POST_GRANT", 7)
"""A graph precompute envelope received outside `GRAPH`: zero effect, left for the DLQ."""


class GraphPrecomputeConsumer(Protocol):
    """The graph precompute (a `PostGrantRuntime`), as the FIFO consumer sees it."""

    def consume(
        self, envelope: PostGrantEnvelope, remaining_ms: Callable[[], int]
    ) -> ConsumeResult:
        """Consume one precompute envelope; `acknowledge` decides the batch item."""
        ...


@dataclass(frozen=True, slots=True)
class GraphPrecomputeBatch:
    """One FIFO invocation: the composed consumer (None where not provisioned) and its clocks."""

    consumer: GraphPrecomputeConsumer | None
    mode: Callable[[], AuthorityMode]
    remaining_ms: Callable[[], int]


def _graph_message_id(record: object) -> str:
    if not isinstance(record, dict):
        return ""
    message_id = cast("dict[str, object]", record).get("messageId")
    return message_id if isinstance(message_id, str) else ""


def is_graph_batch(event: object) -> bool:
    """Whether the invocation came from the precompute FIFO (SQS), not the publication stream."""
    if not isinstance(event, dict):
        return False
    records = cast("dict[str, object]", event).get("Records")
    if not isinstance(records, list) or not records:
        return False
    first = cast("list[object]", records)[0]
    return isinstance(first, dict) and cast("dict[str, object]", first).get("eventSource") == (
        "aws:sqs"
    )


def apply_graph_records(event: object, batch: GraphPrecomputeBatch) -> tuple[str, ...]:
    """Consume each graph precompute envelope; return the message ids left unacknowledged.

    The mode is read once per batch, before any record is parsed. A graph envelope outside
    `GRAPH`, or with no graph consumer composed, is refused with zero effect and left for the
    DLQ; a malformed one likewise.
    """
    if not isinstance(event, dict):
        return ()
    records = cast("dict[str, object]", event).get("Records")
    if not isinstance(records, list):
        return ()
    mode = batch.mode()
    failures: list[str] = []
    for record in cast("list[object]", records):
        message_id = _graph_message_id(record)
        try:
            envelope = post_grant_envelope(
                cast("dict[str, object]", record) if isinstance(record, dict) else {},
                CohortMember.PRECOMPUTE,
            )
        except PostGrantEnvelopeError as malformed:
            _report_failure(malformed)
            failures.append(message_id)
            continue
        if envelope is None:
            continue
        if mode is not AuthorityMode.GRAPH or batch.consumer is None:
            _report_failure(
                InvariantError(
                    GRAPH_PRECOMPUTE_OUTSIDE_GRAPH,
                    "A graph precompute envelope arrived outside GRAPH.",
                    context={"signal": GRAPH_DISPATCH_NOT_ACTIVE},
                )
            )
            failures.append(message_id)
            continue
        if not batch.consumer.consume(envelope, batch.remaining_ms).acknowledge:
            failures.append(message_id)
    return tuple(failures)


def _unbounded_ms() -> int:
    return 1_000_000_000


def _legacy_mode() -> AuthorityMode:
    return AuthorityMode.LEGACY


@lru_cache(maxsize=1)
def graph_consumer() -> tuple[PostGrantRuntime | None, Callable[[], AuthorityMode]]:
    """Compose the graph precompute over the stream worker's reader, planner and assembler.

    Only where the graph authority is provisioned; elsewhere the mode reads `LEGACY`.
    """
    table = graph_consumer_settings().execution_graph_table
    if not table:
        return None, _legacy_mode
    composed = application()
    if composed.graph_planner is None:
        raise InvariantError(
            ErrorCode("GRAPH_PRECOMPUTE", 5),
            "Graph precompute artifact planning is not configured.",
        )
    store = DynamoGraphReleaseStore(table, boto3.client("dynamodb"))
    runtime = PostGrantRuntime(
        store,
        GraphPrecomputeWork(
            BakeRoster.from_contract(),
            composed.graph_planner,
            composed.assembler,
            composed.max_requests_per_publication,
        ),
        lambda: datetime.now(UTC),
        LOGGER,
    )
    return runtime, lambda: store.read_mode().mode


@lru_cache(maxsize=1)
def application() -> PublicationInsightPrecomputeApplication:
    """Compose retained publication planning with no page, session, or owner capability."""
    settings = insight_precompute_settings()
    if (
        settings.insight_precompute_max_requests,
        settings.insight_generation_max_concurrency,
        settings.insight_generation_timeout_seconds,
    ) != (40, 8, 4):
        # The Athena cap is the start budget's `insight_precompute` row (S2), set by the stack
        # and refused below one by settings; the eight here is model-call concurrency.
        raise InvariantError(
            ErrorCode("GRAPH_PRECOMPUTE", 3),
            "Graph precompute capacity and concurrency do not match the reviewed plan.",
        )
    root = contracts_root()
    contracts = tuple(contract for _, contract in load_contracts(root))
    source_packs = load_source_packs(root)
    bridge = load_commerce_identity_bridge(root)
    taxonomy = load_media_taxonomy(root)
    scope = AuthorizedAnalyticalScope(frozenset({bridge.commerce_scope}))
    current = DynamoCurrentPublications(settings.semantic_publications_table)
    catalog = CanonicalEntityCatalog(
        EntityCatalogTooling(
            current=current,
            canonical=S3CanonicalAdapter(settings.semantic_v2_bucket),
            packs=source_packs,
            contracts=contracts,
            bridge=bridge,
            taxonomy=taxonomy,
        )
    )
    metric_catalog = load_catalog(root)
    page_schemas = load_page_schemas(root)
    presentation_schemas = load_v2_page_schemas(root)
    campaign_schema = next(
        (schema for schema in presentation_schemas.pages if schema.page_id == "campaigns"),
        None,
    )
    if campaign_schema is None:
        raise RuntimeError("campaign presentation schema is unavailable for retained precompute.")
    insight_contracts = {contract.id: contract for _, contract in load_insight_contracts(root)}
    queries = AthenaQueryAdapter(
        AthenaQueryTarget(
            workgroup=settings.athena_workgroup,
            database=settings.semantic_v2_current_database,
            reuse_minutes=settings.athena_result_reuse_max_age_minutes,
        )
    )
    publications = PublicationSelector(current, source_packs, bridge)
    # The ad products with spend in a period, so a roster-graded figure reads each rostered key
    # with no row "not available" here as in chat (plan D4): the measurement service prepares
    # every request through `attach_roster` with it.
    rosters = SpendRosterReader(
        catalog=metric_catalog,
        contracts=contracts,
        publications=publications,
        queries=queries,
        labels=ad_product_labels(taxonomy),
        clock=lambda: datetime.now(UTC),
        # The campaign measurement service may ask a CAMPAIGN roster; name it as chat does.
        names=DimensionLabelResolver(catalog, scope),
    )
    planner = RetainedPublicationPlanner(
        coverage=PageWindowCatalog(current, source_packs, contracts, bridge),
        coverage_requirements=campaign_coverage_requirements(page_schemas, metric_catalog),
        temporal_policy=campaign_schema.temporal_policy,
        catalog=catalog,
        scope=scope,
        request_builder=CampaignRetainedPublicationRequestBuilder(
            artifacts=RetainedCampaignArtifactMaterializer(
                catalog=catalog,
                scope=scope,
                measurements=CampaignArtifactMeasurementService(
                    CampaignMeasurementDependencies(
                        scope=scope,
                        catalog=metric_catalog,
                        contracts=contracts,
                        clock=lambda: datetime.now(UTC),
                        rosters=rosters,
                    )
                ),
                evidence=PublicationSelector(current, source_packs, bridge),
                tooling=MeasurementTooling(
                    queries=queries,
                    publications=publications,
                    query_batch_max_concurrency=settings.athena_query_batch_max_concurrency,
                ),
                labels=(
                    *presentation_dimension_labels(taxonomy, bridge),
                    *campaign_platform_labels(taxonomy),
                ),
            ),
            scope=scope,
            contracts=insight_contracts,
        ),
        max_requests=settings.insight_precompute_max_requests,
        logger=LOGGER,
    )
    return PublicationInsightPrecomputeApplication(
        reader=current,
        planner=planner,
        graph_planner=StagedArtifactInsightPlanner(
            bodies=S3PresetArtifactBodyStore(settings.preset_artifact_bucket),
            custody=S3PresetArtifactCustodyStore(settings.preset_artifact_bucket),
            scope=scope,
            contracts=insight_contracts,
            catalog=metric_catalog,
        ),
        assembler=InsightSlotAssembler(
            cache=DynamoInsightCache(settings.insight_cache_table),
            prompts=DynamoInsightPromptRegistry(settings.insight_prompt_registry_table),
            generator=BedrockInsightGenerator(
                InsightModelRouteModels(
                    settings.insight_quick_model_id,
                    settings.insight_deep_model_id,
                ),
                settings.insight_generation_timeout_seconds,
            ),
            max_concurrency=settings.insight_generation_max_concurrency,
        ),
        max_requests_per_publication=settings.insight_precompute_max_requests,
        gate=provisioned_cutover_gate(),
    )


@trace_boundary(INSIGHT_PRECOMPUTE)
@flushes_traces
@flushes_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, list[dict[str, str]]]:
    """Lambda partial-batch interface; only retained state determines worker authority."""
    with span(INSIGHT_PRECOMPUTE, operation=_OPERATION) as process_span:
        raw_records: object = event.get("Records", [])
        record_count = (
            len(cast("list[object]", raw_records)) if isinstance(raw_records, list) else 0
        )
        try:
            if is_graph_batch(event):
                consumer, mode = graph_consumer()
                remaining: object = getattr(_context, "get_remaining_time_in_millis", None)
                failures = apply_graph_records(
                    event,
                    GraphPrecomputeBatch(
                        consumer,
                        mode,
                        cast("Callable[[], int]", remaining)
                        if callable(remaining)
                        else _unbounded_ms,
                    ),
                )
            else:
                composed = application()
                closed = composed.gate.closed(STREAM_TRIGGER_AFTER_CUTOVER, record_count)
                failures = apply_records(event, composed, after_cutover=closed)
        except Exception as error:
            process_span.note_failure(error)
            process_span.record(
                outcome="failed",
                record_count=record_count,
                acknowledged_count=0,
                failed_count=record_count,
            )
            _report_failure(error)
            raise
        failed_count = len(failures)
        if failures:
            process_span.note_failure(RuntimeError("partial batch retry"))
        acknowledged_count = record_count - failed_count
        outcome = "completed" if not failures else "partial_retry"
        process_span.record(
            outcome=outcome,
            record_count=record_count,
            acknowledged_count=acknowledged_count,
            failed_count=failed_count,
        )
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="terminal",
                outcome=outcome,
                severity=LogSeverity.INFO if not failures else LogSeverity.WARNING,
                details={
                    "record_count": record_count,
                    "acknowledged_count": acknowledged_count,
                    "failed_count": failed_count,
                },
            ),
            LOGGER,
        )
        return {"batchItemFailures": [{"itemIdentifier": item} for item in failures]}
