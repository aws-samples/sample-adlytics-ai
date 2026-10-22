# SPDX-License-Identifier: MIT-0
# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false
"""Declared-only stream, FIFO, and deploy-backfill preset-artifact transport."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from bake_application_support import apply_with_deadline, legacy_application, remaining_ms
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from publication_support import PublicationSpec, successful_publication

from adapters.config import (
    PresetArtifactBackfillSettings,
    PresetArtifactBakeSettings,
    PresetArtifactEnqueueSettings,
    preset_artifact_backfill_settings,
    preset_artifact_bake_settings,
    preset_artifact_enqueue_settings,
)
from adapters.jobs.execution_graph.cutover_gate import CutoverGate
from adapters.jobs.preset_artifact_publisher import backfill as backfill_module
from adapters.jobs.preset_artifact_publisher import baker as baker_module
from adapters.jobs.preset_artifact_publisher import enqueue as enqueue_module
from adapters.jobs.preset_artifact_publisher.backfill import (
    BackfillApplication,
    physical_resource_id,
)
from adapters.jobs.preset_artifact_publisher.bake_results import (
    BakePublication,
    BakeResult,
    BakeUnavailable,
    BakeUnchanged,
)
from adapters.jobs.preset_artifact_publisher.baker import (
    AthenaBatchAdmission,
    CampaignPresetArtifactBaker,
    CampaignPresetArtifactSource,
    PresetArtifactBaker,
    RegisteredPresetArtifactBaker,
    RetailHealthPresetArtifactBaker,
    UnavailableRetailHealthPresetArtifactSource,
)
from adapters.jobs.preset_artifact_publisher.enqueue import (
    PresetArtifactEnqueueApplication,
    PublicationIntentPlanner,
    PublicationTriggerReader,
)
from adapters.jobs.preset_artifact_publisher.enqueue import apply_records as apply_enqueue_records
from adapters.jobs.preset_artifact_publisher.transport import (
    BakeIntent,
    BakeIntentSender,
    DeclaredBakeIntents,
)
from adapters.outbound.bake_sources import BakeQueryCapture
from adapters.outbound.telemetry import tracing
from adapters.outbound.telemetry.reporting import OperationLog
from adapters.outbound.telemetry.tracing import (
    PRESET_ARTIFACT_BACKFILL,
    PRESET_ARTIFACT_ENQUEUE_DELIVERY,
)
from domain.operations.bake_refusals import (
    IsolatedRefusal,
    IsolatedRefusalReason,
    RefusalScope,
)
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_release import BakeRoster
from domain.presentation.preset_artifacts import (
    ArtifactBodyWriteResult,
    PresetArtifactBody,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceOutcome,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactPointerAdvanceResult,
    PresetArtifactPublicationRequest,
    PresetArtifactPublicationResult,
    PresetArtifactRevision,
    PresetArtifactVariant,
    SourceUnchanged,
    canonical_preset_artifact_json,
)
from domain.shared_kernel.errors import InvariantError
from ports.outbound.preset_artifacts import (
    LegacyPointerAfterCutoverError,
    PresetArtifactPersistencePort,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.unit

PAGE = PresetArtifactPage("campaigns")
PERSISTED_VARIANT = PresetArtifactVariant("seven-days")
INTENT = BakeIntent("campaigns", "7d")
DECLARED = DeclaredBakeIntents((INTENT,))
GENERATION = "a" * 64


@dataclass
class Reader:
    publication: object | None
    received: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def current_for_trigger(self, partition_key: str, publication_id: str) -> object | None:
        self.received.append((partition_key, publication_id))
        return self.publication


@dataclass
class Planner:
    intents: tuple[BakeIntent, ...] = ()
    received: list[object] = field(default_factory=list[object])
    fail: bool = False

    def intents_for(self, publication: object) -> tuple[BakeIntent, ...]:
        self.received.append(publication)
        if self.fail:
            raise RuntimeError("retained read unavailable")
        return self.intents


@dataclass
class Sender:
    sent: list[BakeIntent] = field(default_factory=list[BakeIntent])
    fail: bool = False

    def send(self, intent: BakeIntent) -> None:
        if self.fail:
            raise RuntimeError("queue unavailable")
        self.sent.append(intent)


@dataclass
class Baker:
    result: BakeResult
    received: list[BakeIntent] = field(default_factory=list[BakeIntent])
    fail: bool = False

    def bake(self, intent: BakeIntent) -> BakeResult:
        self.received.append(intent)
        if self.fail:
            raise RuntimeError("baker unavailable")
        return self.result


@dataclass
class CampaignSource:
    request: PresetArtifactPublicationRequest | None
    received: list[PresetArtifactVariant] = field(default_factory=list[PresetArtifactVariant])

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | None:
        self.received.append(variant)
        return self.request


@dataclass
class Persistence:
    results: list[PresetArtifactPublicationRequest] = field(
        default_factory=list[PresetArtifactPublicationRequest]
    )
    fail: bool = False

    def publish(
        self, request: PresetArtifactPublicationRequest, **_deadline: object
    ) -> PresetArtifactPublicationResult:
        if self.fail:
            raise RuntimeError("persistence unavailable")
        self.results.append(request)
        body = request.body
        return PresetArtifactPublicationResult(
            ArtifactBodyWriteResult(
                body.page,
                body.variant,
                body.revision,
                body.key,
                body.digest,
                None,
            ),
            PresetArtifactPointerAdvanceResult(PresetArtifactPointerAdvanceOutcome.ADVANCED),
        )


def _stream_record(
    *,
    event_id: str = "event-1",
    sequence_number: str = "sequence-1",
    payload: str = '{"publication_id":"p-1"}',
) -> dict[str, object]:
    return {
        "eventID": event_id,
        "eventName": "MODIFY",
        "dynamodb": {
            "SequenceNumber": sequence_number,
            "NewImage": {
                "partition_key": {"S": "retained-partition"},
                "record_key": {"S": "CURRENT"},
                "payload": {"S": payload},
                "page": {"S": "attacker-controlled"},
                "variant": {"S": "30d"},
            },
        },
    }


def _request() -> PresetArtifactPublicationRequest:
    revision = PresetArtifactRevision.from_publication_identities(("publication-1",))
    body = PresetArtifactBody(
        PAGE,
        PERSISTED_VARIANT,
        revision,
        canonical_preset_artifact_json({"kind": "real"}),
    )
    pointer = PresetArtifactPointer(PAGE, PERSISTED_VARIANT, revision, body.digest, 1)
    return PresetArtifactPublicationRequest(body, PresetArtifactPointerAdvanceRequest(pointer))


def test_campaign_baker_publishes_only_a_retained_source_request_for_the_declared_variant() -> None:
    request = _request()
    source = CampaignSource(request)
    baker = CampaignPresetArtifactBaker(cast("CampaignPresetArtifactSource", source))

    result = baker.bake(BakeIntent("campaigns", "seven-days"))

    assert result == BakePublication(request)
    assert source.received == [PresetArtifactVariant("seven-days")]


def _enqueue_application(
    reader: Reader,
    planner: Planner,
    sender: Sender,
) -> PresetArtifactEnqueueApplication:
    return PresetArtifactEnqueueApplication(
        cast("PublicationTriggerReader", reader),
        cast("PublicationIntentPlanner", planner),
        cast("BakeIntentSender", sender),
        DECLARED,
    )


def _current_publication() -> object:
    """Build a retained current publication; its published_at drives the companion clock."""
    return successful_publication(
        "fact_unit_test",
        date(2026, 8, 10),
        PublicationSpec("p-1", datetime(2026, 8, 19, 12, tzinfo=UTC)),
    )


def test_enqueue_re_attests_current_and_sends_only_planner_declared_intents() -> None:
    publication = _current_publication()
    reader, planner, sender = Reader(publication), Planner((INTENT,)), Sender()

    failures = apply_enqueue_records(
        {"Records": [_stream_record()]},
        _enqueue_application(reader, planner, sender),
    )

    assert failures == ()
    assert reader.received == [("retained-partition", "p-1")]
    assert planner.received == [publication]
    assert sender.sent == [INTENT]


def test_enqueue_acknowledges_malformed_current_and_retries_only_its_failed_delivery() -> None:
    reader, planner, sender = Reader(object()), Planner((INTENT,)), Sender(fail=True)

    failures = apply_enqueue_records(
        {
            "Records": [
                _stream_record(
                    event_id="ignored",
                    sequence_number="ignored-sequence",
                    payload="bad",
                ),
                _stream_record(event_id="retry-event", sequence_number="retry-sequence"),
            ]
        },
        _enqueue_application(reader, planner, sender),
    )

    assert failures == ("retry-sequence",)
    assert reader.received == [("retained-partition", "p-1")]


def test_fifo_intent_is_closed_and_stably_serialized_for_grouped_deduplication() -> None:
    assert INTENT.serialize() == '{"page":"campaigns","variant":"7d"}'
    assert INTENT.message_group_id == "campaigns#7d"
    assert BakeIntent.parse(INTENT.serialize()) == INTENT
    assert BakeIntent.parse('{"page":"campaigns","variant":"7d","extra":true}') is None


def test_o1_every_d2_group_id_fits_sqs_and_a_129_character_one_is_refused() -> None:
    """A5.2 item 4: `<page>#<variant>` is at most SQS's 128-character MessageGroupId."""
    from domain.operations.execution_graph_release import BakeRoster  # noqa: PLC0415

    for row in BakeRoster.from_contract().rows:
        intent = BakeIntent(row.page, row.variant)
        assert intent.message_group_id == row.key
        assert len(intent.message_group_id) <= 128
    assert len(BakeIntent("p" * 47, "v" * 80).message_group_id) == 128
    with pytest.raises(ValueError, match="128"):
        BakeIntent("p" * 48, "v" * 80)
    with pytest.raises(ValueError, match="lowercase"):
        BakeIntent("campaigns", "Last-month")


def test_o1_node_lock_and_source_key_shapes_fit_dynamodb_at_the_grammar_maxima() -> None:
    """A5.2 item 2: the V3 key shapes at an 80+80 row and the longest D2 row.

    DynamoDB caps a sort key at 1,024 bytes (Developer Guide, "Constraints", "Sort key
    length", read 2026-10-15); option (b) adds no `BAKE_BARRIER#` shape.
    """
    from adapters.jobs.preset_artifact_publisher.graph_bake import (  # noqa: PLC0415
        task_node_name,
    )

    for key in ("p" * 80 + "#" + "v_" * 40, "retention#latest_with_current_publication"):
        node = task_node_name(key)
        assert node == f"bake_task:{key}"
        shapes = (node, f"POST_GRANT#{node}#{'u' * 128}", f"BAKE_SOURCE#{key}")
        assert all(len(shape.encode()) <= 1024 for shape in shapes)
    assert len(task_node_name("retention#latest_with_current_publication")) == 51


def test_baker_acknowledges_malformed_intent_and_returns_unavailable_without_persistence() -> None:
    unavailable = BakeUnavailable(INTENT, "page_baker_unavailable")
    baker, persistence = Baker(unavailable), Persistence()
    application = legacy_application(
        DECLARED,
        cast("PresetArtifactBaker", baker),
        cast("PresetArtifactPersistencePort", persistence),
    )

    failures = apply_with_deadline(
        {
            "Records": [
                {"messageId": "malformed", "body": "{}"},
                {"messageId": "one", "body": INTENT.serialize()},
            ]
        },
        application,
    )

    assert failures == ()
    assert baker.received == [INTENT]
    assert persistence.results == []


def test_baker_persists_real_bake_request_and_retries_only_its_message_on_failure() -> None:
    persisted_intent = BakeIntent("campaigns", "seven-days")
    declared = DeclaredBakeIntents((persisted_intent,))
    publication = BakePublication(_request())
    baker, persistence = Baker(publication), Persistence(fail=True)
    application = legacy_application(
        declared,
        cast("PresetArtifactBaker", baker),
        cast("PresetArtifactPersistencePort", persistence),
    )

    failures = apply_with_deadline(
        {"Records": [{"messageId": "retry", "body": persisted_intent.serialize()}]},
        application,
    )

    assert failures == ("retry",)
    assert baker.received == [persisted_intent]
    assert persistence.results == []


def test_backfill_sends_every_declared_target_on_create_and_never_on_delete() -> None:
    second = BakeIntent("media-efficiency", "30d")
    sender = Sender()
    application = BackfillApplication(DeclaredBakeIntents((INTENT, second)), sender)
    event = {
        "RequestType": "Create",
        # CloudFormation injects ServiceToken into every custom resource's properties.
        "ResourceProperties": {
            "ServiceToken": "arn:aws:lambda:us-east-1:000000000000:function:fictional",
            "generation": GENERATION,
        },
    }

    created = application.apply(event)
    deleted = application.apply({**event, "RequestType": "Delete"})

    assert created == {"PhysicalResourceId": physical_resource_id(GENERATION)}
    assert deleted == created
    assert sender.sent == [INTENT, second]


def test_registered_retail_baker_returns_typed_unavailable_without_materialization() -> None:
    """Retail registration cannot fabricate an artifact before retained materialization exists."""
    baker = RegisteredPresetArtifactBaker(
        bakers={
            "campaigns": CampaignPresetArtifactBaker(CampaignSource(None)),
            "retail-health": RetailHealthPresetArtifactBaker(
                UnavailableRetailHealthPresetArtifactSource()
            ),
        },
        fallback=cast("PresetArtifactBaker", Baker(BakeUnavailable(INTENT, "fallback"))),
        roster_pages=("campaigns", "retail-health"),
    )

    result = baker.bake(BakeIntent("retail-health", "7d"))

    # F12: the reason is generated from the normalized page id, never a source literal.
    assert result == BakeUnavailable(
        BakeIntent("retail-health", "7d"), "retail_health_materialization_unavailable"
    )


_PUBLISHER_ENV = (
    "ADL_SEMANTIC_PUBLICATIONS_TABLE",
    "ADL_PRESET_ARTIFACT_BAKE_QUEUE_URL",
    "ADL_PRESET_ARTIFACT_BUCKET",
    "ADL_PRESET_ARTIFACT_POINTER_TABLE",
    "ADL_SEMANTIC_V2_BUCKET",
    "ADL_SEMANTIC_V2_ATHENA_BAKE_WORKGROUP",
    "ADL_SEMANTIC_V2_CURRENT_DATABASE",
    "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES",
    "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY",
)


def _clear_publisher_settings_caches() -> None:
    preset_artifact_enqueue_settings.cache_clear()
    preset_artifact_bake_settings.cache_clear()
    preset_artifact_backfill_settings.cache_clear()


@pytest.fixture
def publisher_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    for name in _PUBLISHER_ENV:
        monkeypatch.delenv(name, raising=False)
    _clear_publisher_settings_caches()
    yield monkeypatch
    _clear_publisher_settings_caches()


def test_publisher_settings_report_every_missing_key_at_once(
    publisher_settings_env: pytest.MonkeyPatch,
) -> None:
    """One deploy with several unset values must surface one complete finding, not a chain."""
    del publisher_settings_env
    with pytest.raises(InvariantError) as enqueue_error:
        preset_artifact_enqueue_settings()
    with pytest.raises(InvariantError) as bake_error:
        preset_artifact_bake_settings()
    with pytest.raises(InvariantError) as backfill_error:
        preset_artifact_backfill_settings()

    missing = {
        "semantic_publications_table",
        "bake_queue_url",
        "artifact_bucket",
        "pointer_table",
    }
    assert enqueue_error.value.context == {
        "missing": ["semantic_publications_table", "bake_queue_url"]
    }
    assert bake_error.value.context == {"missing": ["artifact_bucket", "pointer_table"]}
    assert backfill_error.value.context == {"missing": ["bake_queue_url"]}
    for error in (enqueue_error, bake_error, backfill_error):
        assert not any(name in error.value.description for name in missing)


def test_publisher_settings_resolve_only_each_worker_declared_targets(
    publisher_settings_env: pytest.MonkeyPatch,
) -> None:
    publisher_settings_env.setenv("ADL_SEMANTIC_PUBLICATIONS_TABLE", " publications-table ")
    publisher_settings_env.setenv(
        "ADL_PRESET_ARTIFACT_BAKE_QUEUE_URL", "https://sqs.example/bake.fifo"
    )
    publisher_settings_env.setenv("ADL_PRESET_ARTIFACT_BUCKET", "artifact-bucket")
    publisher_settings_env.setenv("ADL_PRESET_ARTIFACT_POINTER_TABLE", "pointer-table")

    assert preset_artifact_enqueue_settings() == PresetArtifactEnqueueSettings(
        semantic_publications_table="publications-table",
        bake_queue_url="https://sqs.example/bake.fifo",
    )
    assert preset_artifact_bake_settings() == PresetArtifactBakeSettings(
        artifact_bucket="artifact-bucket",
        pointer_table="pointer-table",
        semantic_publications_table="publications-table",
    )
    assert preset_artifact_backfill_settings() == PresetArtifactBackfillSettings(
        bake_queue_url="https://sqs.example/bake.fifo"
    )


def test_bake_settings_resolve_the_campaign_source_fields_when_declared(
    publisher_settings_env: pytest.MonkeyPatch,
) -> None:
    """The optional retained-truth fields resolve without gating the persistence targets."""
    publisher_settings_env.setenv("ADL_PRESET_ARTIFACT_BUCKET", "artifact-bucket")
    publisher_settings_env.setenv("ADL_PRESET_ARTIFACT_POINTER_TABLE", "pointer-table")
    publisher_settings_env.setenv("ADL_SEMANTIC_PUBLICATIONS_TABLE", "publications-table")
    publisher_settings_env.setenv("ADL_SEMANTIC_V2_BUCKET", "semantic-bucket")
    publisher_settings_env.setenv("ADL_SEMANTIC_V2_ATHENA_BAKE_WORKGROUP", "bake-workgroup")
    publisher_settings_env.setenv("ADL_SEMANTIC_V2_CURRENT_DATABASE", "semantic-current")
    publisher_settings_env.setenv("ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES", "60")
    publisher_settings_env.setenv("ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY", "8")

    assert preset_artifact_bake_settings() == PresetArtifactBakeSettings(
        artifact_bucket="artifact-bucket",
        pointer_table="pointer-table",
        semantic_publications_table="publications-table",
        semantic_v2_bucket="semantic-bucket",
        athena_workgroup="bake-workgroup",
        semantic_v2_current_database="semantic-current",
        athena_result_reuse_max_age_minutes=60,
        athena_query_batch_max_concurrency=8,
    )


def test_bake_companion_terminal_reuses_the_message_sent_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The terminal instant is identity-bearing.

    A wall clock here forked immutable event identities across duplicate emissions
    and dead-lettered them (2026-09-22).
    """
    emitted: list[tuple[object, object]] = []

    class _Emitter:
        def emit(self, start: object, terminal: object) -> None:
            emitted.append((start, terminal))

    def _companion_emitter(_operation: str) -> _Emitter:
        return _Emitter()

    monkeypatch.setattr(baker_module, "companion_emitter", _companion_emitter)
    record: dict[str, object] = {
        "messageId": "message-fictional-0001",
        "attributes": {"SentTimestamp": "1790108000000", "ApproximateReceiveCount": "1"},
    }
    emit = baker_module._emit_bake_companion  # pyright: ignore[reportPrivateUsage]
    emit(record, BakeUnavailable(INTENT, "page_baker_unavailable"))
    emit(record, BakeUnavailable(INTENT, "page_baker_unavailable"))

    assert len(emitted) == 2
    first_start, first_terminal = emitted[0]
    second_start, second_terminal = emitted[1]
    sent = datetime.fromtimestamp(1790108000000 / 1_000, tz=UTC)
    assert getattr(first_terminal, "occurred_at", None) == sent
    # Redelivery determinism: byte-identical events, never a fresh wall-clock identity.
    assert first_start == second_start
    assert first_terminal == second_terminal


def test_baker_routes_completion_through_shared_operation_reporting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[OperationLog] = []

    def record_operation(event: OperationLog, _logger: object) -> None:
        operations.append(event)

    monkeypatch.setattr(baker_module, "report_operation", record_operation)
    application = legacy_application(
        DECLARED,
        cast("PresetArtifactBaker", Baker(BakeUnavailable(INTENT, "unavailable"))),
        cast("PresetArtifactPersistencePort", Persistence()),
    )

    result = application.apply(INTENT.serialize(), remaining_ms=remaining_ms, final=False)

    assert isinstance(result, BakeUnavailable)
    assert len(operations) == 1
    assert operations[0].operation == "preset_artifact.publish"
    assert operations[0].phase == "terminal"
    assert operations[0].outcome == "unavailable"


def test_baker_routes_failure_and_retry_through_shared_reporting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[OperationLog] = []
    errors: list[BaseException] = []

    def record_operation(event: OperationLog, _logger: object) -> None:
        operations.append(event)

    def record_error(error: BaseException, _report: object) -> None:
        errors.append(error)

    monkeypatch.setattr(baker_module, "report_operation", record_operation)
    monkeypatch.setattr(baker_module, "report_error", record_error)
    application = legacy_application(
        DECLARED,
        cast("PresetArtifactBaker", Baker(BakeUnavailable(INTENT, "unavailable"), fail=True)),
        cast("PresetArtifactPersistencePort", Persistence()),
    )

    failures = apply_with_deadline(
        {"Records": [{"messageId": "retry", "body": INTENT.serialize()}]},
        application,
    )

    assert failures == ("retry",)
    assert len(errors) == 1
    assert len(operations) == 1
    assert operations[0].outcome == "retry"
    assert operations[0].severity.value == "warning"


def _exported_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[InMemorySpanExporter, TracerProvider]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "TRACER", provider.get_tracer("test"))
    return exporter, provider


_LINEAGE = {"operation_id", "execution_id", "plan_digest", "stage", "completed", "total"}


def test_enqueue_batch_and_delivery_spans_carry_lineage_and_count_only_admitted_triggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream batch must be traceable per trigger with a digest of identities, not images."""
    exporter, _provider = _exported_spans(monkeypatch)
    reader, planner, sender = Reader(_current_publication()), Planner((INTENT,)), Sender()
    event = {
        "Records": [
            _stream_record(event_id="skip", sequence_number="skip", payload="bad"),
            _stream_record(event_id="one", sequence_number="one"),
            _stream_record(event_id="two", sequence_number="two"),
        ]
    }
    telemetry = enqueue_module.batch_telemetry(event, "exec-1")

    failures = apply_enqueue_records(
        event, _enqueue_application(reader, planner, sender), telemetry
    )

    assert failures == ()
    assert telemetry.total == 2
    assert telemetry.plan_digest.startswith("sha256:")
    deliveries = [
        s for s in exporter.get_finished_spans() if s.name == PRESET_ARTIFACT_ENQUEUE_DELIVERY
    ]
    assert len(deliveries) == 2
    for delivery in deliveries:
        attributes = dict(delivery.attributes or {})
        assert set(attributes) >= _LINEAGE
        assert attributes["execution_id"] == "exec-1"
        assert attributes["plan_digest"] == telemetry.plan_digest
        assert attributes["total"] == 2
    assert [dict(d.attributes or {})["completed"] for d in deliveries] == [1, 2]


def test_backfill_run_counts_sent_intents_and_sends_nothing_on_delete() -> None:
    second = BakeIntent("media-efficiency", "30d")
    sender = Sender()
    application = BackfillApplication(DeclaredBakeIntents((INTENT, second)), sender)
    event = {
        "RequestType": "Update",
        "ResourceProperties": {"ServiceToken": "arn:fictional", "generation": GENERATION},
    }

    updated = application.run(event)
    deleted = application.run({**event, "RequestType": "Delete"})

    assert (updated.sent, updated.total, updated.request_type) == (2, 2, "Update")
    assert (deleted.sent, deleted.total, deleted.request_type) == (0, 0, "Delete")
    assert updated.generation == GENERATION
    assert updated.response() == {"PhysicalResourceId": physical_resource_id(GENERATION)}


def test_r5_1_an_update_with_an_unchanged_declared_input_sends_nothing() -> None:
    """R5-1: only a changed declared input re-fires the deploy backfill."""
    sender = Sender()
    application = BackfillApplication(DeclaredBakeIntents((INTENT,)), sender)
    unchanged = {
        "RequestType": "Update",
        "ResourceProperties": {"ServiceToken": "arn:fictional", "generation": GENERATION},
        "OldResourceProperties": {"ServiceToken": "arn:fictional-old", "generation": GENERATION},
    }

    quiet = application.run(unchanged)
    changed = application.run({**unchanged, "OldResourceProperties": {"generation": "b" * 64}})

    assert (quiet.sent, quiet.total, quiet.request_type) == (0, 0, "Unchanged")
    assert quiet.response() == {"PhysicalResourceId": physical_resource_id(GENERATION)}
    assert (changed.sent, changed.request_type) == (1, "Update")
    assert sender.sent == [INTENT]


def test_backfill_handler_reports_failure_through_shared_seam_and_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CloudFormation must see the failure, and so must the operator's structured log."""
    exporter, _provider = _exported_spans(monkeypatch)
    operations: list[OperationLog] = []
    errors: list[BaseException] = []

    def record_operation(event: OperationLog, _logger: object) -> None:
        operations.append(event)

    def record_error(error: BaseException, _report: object) -> None:
        errors.append(error)

    monkeypatch.setattr(backfill_module, "report_operation", record_operation)
    monkeypatch.setattr(backfill_module, "report_error", record_error)
    monkeypatch.setattr(
        backfill_module,
        "application",
        lambda: BackfillApplication(DECLARED, cast("BakeIntentSender", Sender(fail=True))),
    )
    event = {
        "RequestType": "Create",
        "ResourceProperties": {"ServiceToken": "arn:fictional", "generation": GENERATION},
    }

    with pytest.raises(RuntimeError, match="queue unavailable"):
        backfill_module.handler(event, _LambdaContext())

    assert len(errors) == 1
    assert [op.outcome for op in operations] == ["failed"]
    assert operations[0].details["plan_digest"] == f"sha256:{GENERATION}"
    (root,) = [s for s in exporter.get_finished_spans() if s.name == PRESET_ARTIFACT_BACKFILL]
    assert set(dict(root.attributes or {})) >= _LINEAGE
    assert root.status.status_code.name == "ERROR"


@dataclass
class _LambdaContext:
    aws_request_id: str = "req-1"
    function_name: str = "fictional"
    memory_limit_in_mb: int = 128
    invoked_function_arn: str = "arn:fictional"


# --- S6 slice 5i: D26's legacy report label, the registered plan, graph envelopes -------------


def test_d26_the_legacy_apply_reports_unchanged_as_unchanged_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[OperationLog] = []

    def record_operation(event: OperationLog, _logger: object) -> None:
        operations.append(event)

    monkeypatch.setattr(baker_module, "report_operation", record_operation)
    persistence = Persistence()
    revision = PresetArtifactRevision.from_publication_identities(("publication-1",))
    application = legacy_application(
        DECLARED,
        cast("PresetArtifactBaker", Baker(BakeUnchanged(INTENT, revision))),
        cast("PresetArtifactPersistencePort", persistence),
    )

    result = application.apply(INTENT.serialize(), remaining_ms=remaining_ms, final=False)

    assert result == BakeUnchanged(INTENT, revision)
    assert persistence.results == []
    assert [operation.outcome for operation in operations] == ["unchanged"]


def test_d26_control_the_legacy_apply_still_reports_unavailable_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[OperationLog] = []

    def record_operation(event: OperationLog, _logger: object) -> None:
        operations.append(event)

    monkeypatch.setattr(baker_module, "report_operation", record_operation)
    application = legacy_application(
        DECLARED,
        cast("PresetArtifactBaker", Baker(BakeUnavailable(INTENT, "page_baker_unavailable"))),
        cast("PresetArtifactPersistencePort", Persistence()),
    )

    application.apply(INTENT.serialize(), remaining_ms=remaining_ms, final=False)

    assert [operation.outcome for operation in operations] == ["unavailable"]


def test_d26_the_bake_companion_reports_unchanged_as_no_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[object] = []

    class _Emitter:
        def emit(self, start: object, terminal: object) -> None:
            del start
            emitted.append(terminal)

    def emitter(_operation: str) -> _Emitter:
        return _Emitter()

    monkeypatch.setattr(baker_module, "companion_emitter", emitter)
    record: dict[str, object] = {
        "messageId": "message-fictional-0002",
        "attributes": {"SentTimestamp": "1790108000000", "ApproximateReceiveCount": "1"},
    }
    revision = PresetArtifactRevision.from_publication_identities(("publication-1",))
    emit = baker_module._emit_bake_companion  # pyright: ignore[reportPrivateUsage]

    emit(record, BakeUnchanged(INTENT, revision))
    emit(record, BakeUnavailable(INTENT, "page_baker_unavailable"))

    assert [getattr(item, "reason_code", None) for item in emitted] == [
        "source_unchanged",
        "page_baker_unavailable",
    ]


def test_agg4_13_registered_pages_are_exactly_the_roster_pages() -> None:
    """A1.2/F7 (A5.3): registration derives from the 20-row roster's seven pages."""
    composed = baker_module.PAGE_BAKERS
    assert (
        tuple(composed)
        == BakeRoster.from_contract().pages
        == (
            "campaigns",
            "retail-health",
            "scorecard",
            "new-customers",
            "reach",
            "retention",
            "audiences",
        )
    )


def test_a_campaign_source_unchanged_maps_to_bake_unchanged() -> None:
    revision = PresetArtifactRevision.from_publication_identities(("publication-1",))

    @dataclass
    class _Unchanged:
        def publication_for(self, variant: PresetArtifactVariant) -> SourceUnchanged:
            del variant
            return SourceUnchanged(revision)

    baker = CampaignPresetArtifactBaker(cast("CampaignPresetArtifactSource", _Unchanged()))

    assert baker.bake(INTENT) == BakeUnchanged(INTENT, revision)
    # Control: no artifact is still unavailable, never unchanged.
    none_baker = CampaignPresetArtifactBaker(
        cast("CampaignPresetArtifactSource", CampaignSource(None))
    )
    assert none_baker.bake(INTENT) == BakeUnavailable(
        INTENT, "campaigns_materialization_unavailable"
    )


_GRAPH_BODY = (
    '{"conflict_unit":"u","dispatch_id":"d","grant_id":"g","granted_run":"r",'
    '"node_id":"n","post_grant":"bake","root_flow_id":"f","run_id":"r",'
    '"schema_version":"1","served_revision":"2026-10-07T08:00:00+00:00"}'
)


@dataclass
class _GraphConsumer:
    consumed: list[object] = field(default_factory=list[object])

    def consume(self, envelope: object, remaining_ms: object) -> object:
        del remaining_ms
        self.consumed.append(envelope)
        return type("Result", (), {"acknowledge": True})()


def _graph_record() -> dict[str, object]:
    return {
        "messageId": "graph-1",
        "body": _GRAPH_BODY,
        "attributes": {"ApproximateReceiveCount": "1", "SentTimestamp": "1790108000000"},
    }


def test_a_graph_bake_envelope_outside_graph_is_refused_with_zero_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[BaseException] = []

    def record_error(error: BaseException, _report: object) -> None:
        errors.append(error)

    monkeypatch.setattr(baker_module, "report_error", record_error)
    graph = _GraphConsumer()
    baker = Baker(BakeUnavailable(INTENT, "unavailable"))
    application = legacy_application(
        DECLARED,
        cast("PresetArtifactBaker", baker),
        cast("PresetArtifactPersistencePort", Persistence()),
        graph=cast("baker_module.GraphBakeConsumer", graph),
        mode=lambda: AuthorityMode.SHADOW,
    )

    failures = apply_with_deadline({"Records": [_graph_record()]}, application)

    assert failures == ("graph-1",)
    assert (graph.consumed, baker.received) == ([], [])
    assert isinstance(errors[0], InvariantError)
    assert errors[0].context["signal"] == "GRAPH_DISPATCH_NOT_ACTIVE"


def test_control_a_graph_bake_envelope_in_graph_reaches_the_graph_bake() -> None:
    graph = _GraphConsumer()
    baker = Baker(BakeUnavailable(INTENT, "unavailable"))
    application = legacy_application(
        DECLARED,
        cast("PresetArtifactBaker", baker),
        cast("PresetArtifactPersistencePort", Persistence()),
        graph=cast("baker_module.GraphBakeConsumer", graph),
        mode=lambda: AuthorityMode.GRAPH,
    )

    failures = apply_with_deadline({"Records": [_graph_record()]}, application)

    assert failures == ()
    assert len(graph.consumed) == 1 and baker.received == []


# --- S6 §2.9 (D6): the stream, baker-legacy and backfill gates -------------------------------


@dataclass
class _Gate:
    """A cutover gate over a scripted mode that records each drop and each mode read."""

    mode_value: AuthorityMode
    drops: list[tuple[str, int]] = field(default_factory=list[tuple[str, int]])
    reads: int = 0

    def mode(self) -> AuthorityMode:
        self.reads += 1
        return self.mode_value

    def gate(self) -> CutoverGate:
        return CutoverGate(self.mode, lambda signal, count: self.drops.append((signal, count)))


def test_d6_the_enqueue_stream_gate_drops_every_record_in_graph() -> None:
    reader, planner, sender = Reader(_current_publication()), Planner((INTENT,)), Sender()
    scripted = _Gate(AuthorityMode.GRAPH)
    application = replace(_enqueue_application(reader, planner, sender), gate=scripted.gate())

    failures = apply_enqueue_records(
        {"Records": [_stream_record(), _stream_record(event_id="two")]}, application
    )

    assert failures == ()
    assert (reader.received, sender.sent) == ([], [])
    assert scripted.drops == [("STREAM_TRIGGER_AFTER_CUTOVER", 2)]
    assert scripted.reads == 1  # once per batch, before any record is parsed


def test_d6_control_the_enqueue_stream_proceeds_in_shadow() -> None:
    reader, planner, sender = Reader(_current_publication()), Planner((INTENT,)), Sender()
    scripted = _Gate(AuthorityMode.SHADOW)
    application = replace(_enqueue_application(reader, planner, sender), gate=scripted.gate())

    apply_enqueue_records({"Records": [_stream_record()]}, application)

    assert sender.sent == [INTENT]
    assert scripted.drops == []


def test_d6_the_baker_drops_a_legacy_intent_in_graph_and_acknowledges_it() -> None:
    scripted = _Gate(AuthorityMode.GRAPH)
    baker, persistence = Baker(BakePublication(_request())), Persistence()
    application = legacy_application(
        DECLARED,
        cast("PresetArtifactBaker", baker),
        cast("PresetArtifactPersistencePort", persistence),
        mode=scripted.mode,
        cutover_drop=lambda signal, count: scripted.drops.append((signal, count)),
    )

    failures = apply_with_deadline(
        {"Records": [{"messageId": "legacy", "body": INTENT.serialize()}]}, application
    )

    assert failures == ()
    assert (baker.received, persistence.results) == ([], [])
    assert scripted.drops == [("LEGACY_BAKE_AFTER_CUTOVER", 1)]


def test_d6_control_the_baker_bakes_a_legacy_intent_in_shadow() -> None:
    scripted = _Gate(AuthorityMode.SHADOW)
    baker = Baker(BakeUnavailable(INTENT, "page_baker_unavailable"))
    application = legacy_application(
        DECLARED,
        cast("PresetArtifactBaker", baker),
        cast("PresetArtifactPersistencePort", Persistence()),
        mode=scripted.mode,
        cutover_drop=lambda signal, count: scripted.drops.append((signal, count)),
    )

    apply_with_deadline(
        {"Records": [{"messageId": "legacy", "body": INTENT.serialize()}]}, application
    )

    assert baker.received == [INTENT]
    assert scripted.drops == []


def test_agg3_6_a_legacy_write_refused_across_the_cas_is_acknowledged_and_counted() -> None:
    scripted = _Gate(AuthorityMode.LEGACY_DRAINING)

    @dataclass
    class _Refusing(Persistence):
        def publish(
            self, request: PresetArtifactPublicationRequest, **_deadline: object
        ) -> PresetArtifactPublicationResult:
            raise LegacyPointerAfterCutoverError(request.pointer_advance.pointer.partition_key)

    persisted = BakeIntent("campaigns", "seven-days")
    application = legacy_application(
        DeclaredBakeIntents((persisted,)),
        cast("PresetArtifactBaker", Baker(BakePublication(_request()))),
        cast("PresetArtifactPersistencePort", _Refusing()),
        mode=scripted.mode,
        cutover_drop=lambda signal, count: scripted.drops.append((signal, count)),
    )

    failures = apply_with_deadline(
        {"Records": [{"messageId": "drain", "body": persisted.serialize()}]}, application
    )

    assert failures == ()  # zero effect, acknowledged
    assert scripted.drops == [("LEGACY_BAKE_AFTER_CUTOVER", 1)]


def test_backfill_in_graph_sends_nothing_and_counts_it() -> None:
    scripted = _Gate(AuthorityMode.GRAPH)
    sender = Sender()
    application = BackfillApplication(DECLARED, sender, scripted.gate())
    event = {
        "RequestType": "Create",
        "ResourceProperties": {"ServiceToken": "arn:fictional", "generation": GENERATION},
    }

    outcome = application.run(event)

    assert (outcome.sent, sender.sent) == (0, [])
    assert outcome.response() == {"PhysicalResourceId": physical_resource_id(GENERATION)}
    assert scripted.drops == [("BACKFILL_AFTER_CUTOVER", 1)]


def test_backfill_control_in_shadow_sends_every_declared_intent() -> None:
    scripted = _Gate(AuthorityMode.SHADOW)
    sender = Sender()
    application = BackfillApplication(DECLARED, sender, scripted.gate())
    event = {
        "RequestType": "Create",
        "ResourceProperties": {"ServiceToken": "arn:fictional", "generation": GENERATION},
    }

    assert application.run(event).sent == 1
    assert sender.sent == [INTENT]
    assert scripted.drops == []


def test_no_rebake_command_exists_while_d11_is_held() -> None:
    from adapters.jobs.execution_graph import operator  # noqa: PLC0415

    source = Path(operator.__file__).read_text(encoding="utf-8")
    assert "rebake" not in source


# --- plan 3.4 / A1.3: Q from config into the one invocation-shared admission ------------------


class _GatedQueries:
    """A query port that holds every call until `release_at` are in flight at once."""

    def __init__(self, release_at: int) -> None:
        self._lock = Lock()
        self._reached = Event()
        self._active = 0
        self._release_at = release_at
        self.calls = 0

    def run(self, request: object) -> object:
        with self._lock:
            self.calls += 1
            self._active += 1
            if self._active >= self._release_at:
                self._reached.set()
        self._reached.wait(timeout=5)
        with self._lock:
            self._active -= 1
        return request


_COMPOSED_SETTINGS = PresetArtifactBakeSettings(
    artifact_bucket="fictional-artifacts",
    pointer_table="fictional-pointers",
    semantic_publications_table="fictional-publications",
    semantic_v2_bucket="fictional-semantic",
    athena_workgroup="adlytics-dev-v2-bake",
    semantic_v2_current_database="adlytics_dev_v2_current",
    athena_result_reuse_max_age_minutes=60,
    # The Baker env's ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY = bakerStartCap (Q = 10).
    athena_query_batch_max_concurrency=10,
)


def _compose_baker(
    monkeypatch: pytest.MonkeyPatch, settings: PresetArtifactBakeSettings, queries: object
) -> tuple[baker_module.PresetArtifactBakeApplication, list[tuple[str, object]]]:
    """Run `baker.application` with fakes, recording each source factory's `queries`."""
    factories: list[tuple[str, object]] = []
    module = cast("Any", baker_module)

    def factory(name: str) -> object:
        def record(_settings: object, _pointers: object, ports: object, *_current: object) -> None:
            factories.append((name, ports))

        return record

    monkeypatch.setattr(module, "preset_artifact_bake_settings", lambda: settings)
    monkeypatch.setattr(
        module,
        "graph_consumer_settings",
        lambda: SimpleNamespace(execution_graph_table=""),
    )
    monkeypatch.setattr(module, "DynamoPresetArtifactPointerStore", lambda *_a, **_k: object())
    monkeypatch.setattr(module, "S3PresetArtifactBodyStore", lambda _bucket: object())
    monkeypatch.setattr(module, "S3PresetArtifactCustodyStore", lambda _bucket: object())
    monkeypatch.setattr(module, "AthenaQueryAdapter", lambda _target: queries)
    monkeypatch.setattr(module, "DynamoCurrentPublications", lambda _table: object())
    monkeypatch.setattr(module, "retained_campaign_preset_artifact_source", factory("campaign"))
    monkeypatch.setattr(
        module, "retained_retail_health_preset_artifact_source", factory("retail-health")
    )
    declared = DeclaredBakeIntents((BakeIntent("campaigns", "7d"),))
    monkeypatch.setattr(module, "load_declared_bake_intents", lambda _root: declared)
    monkeypatch.setattr(module, "_insight_fill_child", lambda _settings: None)
    baker_module.application.cache_clear()
    try:
        return baker_module.application(), factories
    finally:
        baker_module.application.cache_clear()


def _occupancy(admission: AthenaBatchAdmission, queries: int) -> int:
    with ThreadPoolExecutor(max_workers=queries) as pool:
        list(pool.map(admission.run, [cast("Any", object())] * queries))
    return admission.maximum_occupancy


def test_baker_composition_feeds_q_10_into_the_one_admission_every_source_shares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = _GatedQueries(release_at=10)
    composed, factories = _compose_baker(monkeypatch, _COMPOSED_SETTINGS, queries)
    admission = cast("RegisteredPresetArtifactBaker", composed.parts.baker).admission
    assert isinstance(admission, AthenaBatchAdmission)
    # Each roster page's factory receives the one capturable port over the shared admission,
    # never an adapter of its own (plan 4.3: the capture is the source's bind step).
    assert [name for name, _ in factories] == ["campaign", "retail-health"]
    port = factories[0][1]
    assert all(shared is port for _, shared in factories)
    assert isinstance(port, BakeQueryCapture) and port.inner is admission
    # A 12-query fixture never exceeds the configured capacity of 10.
    assert _occupancy(admission, 12) == 10
    assert queries.calls == 12


def test_control_a_cap_of_3_observes_exactly_3_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = _GatedQueries(release_at=3)
    settings = replace(_COMPOSED_SETTINGS, athena_query_batch_max_concurrency=3)
    composed, _ = _compose_baker(monkeypatch, settings, queries)
    admission = cast("RegisteredPresetArtifactBaker", composed.parts.baker).admission
    assert isinstance(admission, AthenaBatchAdmission)
    assert _occupancy(admission, 12) == 3


def test_an_absent_admission_setting_composes_every_source_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = _GatedQueries(release_at=1)
    settings = replace(_COMPOSED_SETTINGS, athena_workgroup="")
    composed, factories = _compose_baker(monkeypatch, settings, queries)
    assert cast("RegisteredPresetArtifactBaker", composed.parts.baker).admission is None
    # No factory is called without the admission, so none can build an unbounded adapter.
    assert factories == []
    assert queries.calls == 0


# --- the stage's runtime write deadline (ADR-145, design 04 §4 step 1, 02 §3.3 step 5) -------


def _deadline_after(pairs: int, calls: list[int]) -> Any:
    """Return a `remaining_ms` with time for `pairs` submissions, then below the tail."""

    def remaining() -> int:
        calls.append(1)
        return 600_000 if len(calls) <= pairs else 29_999

    return remaining


def test_the_write_deadline_stops_submission_drains_in_flight_and_writes_no_parent() -> None:
    from test_preset_artifacts import (  # noqa: PLC0415 -- the sibling-store fixtures
        _persistence,  # pyright: ignore[reportPrivateUsage]
        _sibling_request,  # pyright: ignore[reportPrivateUsage]
        _SiblingStores,  # pyright: ignore[reportPrivateUsage]
    )

    stores = _SiblingStores(hold_seconds=0)
    persistence, _ = _persistence(stores)
    with pytest.raises(IsolatedRefusal) as refused:
        persistence.stage(
            _sibling_request(20), remaining_ms=_deadline_after(3, []), tail_seconds=30.0
        )
    assert (refused.value.reason, refused.value.scope) == (
        IsolatedRefusalReason.BODIES_OVER_INVOCATION,
        RefusalScope.PAGE,
    )
    # The three submitted pairs were written and observed; nothing after; no parent, custody
    # or pointer, so no custody names the pairs written.
    assert dict(refused.value.refusal_context) == {"bodies": 20, "written": 3}
    assert len([event for event in stores.events if event.startswith("custody:")]) == 3
    assert stores.active == 0
    assert not {"parent-body", "parent-custody", "pointer"} & set(stores.events)
    # Paired: with time for every pair, the stage writes them all and the parent.
    complete = _SiblingStores(hold_seconds=0)
    staged, _ = _persistence(complete)
    staged.stage(_sibling_request(20), remaining_ms=_deadline_after(20, []), tail_seconds=30.0)
    assert complete.events[-2:] == ["parent-body", "parent-custody"]


def test_the_write_deadline_through_publish_moves_no_legacy_pointer() -> None:
    from test_preset_artifacts import (  # noqa: PLC0415 -- the sibling-store fixtures
        _persistence,  # pyright: ignore[reportPrivateUsage]
        _sibling_request,  # pyright: ignore[reportPrivateUsage]
        _SiblingStores,  # pyright: ignore[reportPrivateUsage]
    )

    stores = _SiblingStores(hold_seconds=0)
    persistence, _ = _persistence(stores)
    with pytest.raises(IsolatedRefusal):
        persistence.publish(
            _sibling_request(9), remaining_ms=_deadline_after(0, []), tail_seconds=30.0
        )
    assert stores.events == []  # nothing submitted, no parent, no pointer
    # Paired: the same request with time publishes and advances the pointer last.
    published = _SiblingStores(hold_seconds=0)
    publishing, _ = _persistence(published)
    publishing.publish(_sibling_request(9), remaining_ms=lambda: 600_000, tail_seconds=30.0)
    assert published.events[-1] == "pointer"


def test_stage_and_publish_require_the_deadline_keywords() -> None:
    from test_preset_artifacts import (  # noqa: PLC0415 -- the sibling-store fixtures
        _persistence,  # pyright: ignore[reportPrivateUsage]
        _sibling_request,  # pyright: ignore[reportPrivateUsage]
        _SiblingStores,  # pyright: ignore[reportPrivateUsage]
    )

    persistence, _ = _persistence(_SiblingStores(hold_seconds=0))
    with pytest.raises(TypeError):
        cast("Any", persistence).stage(_sibling_request(1))
    with pytest.raises(TypeError):
        cast("Any", persistence).publish(_sibling_request(1), remaining_ms=lambda: 1)
