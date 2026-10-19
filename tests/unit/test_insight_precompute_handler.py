# SPDX-License-Identifier: MIT-0
# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownVariableType=false
"""Stream admission and retained-state custody for bounded insight precomputation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from publication_support import PublicationSpec, successful_publication

import adapters.jobs.insight_precompute.handler as worker
from adapters.inbound.presentation_handlers.insights import InsightSlotAssembler
from adapters.jobs.insight_precompute.handler import (
    PublicationInsightPlanner,
    PublicationInsightPrecomputeApplication,
    PublicationTrigger,
    apply_records,
)
from adapters.jobs.insight_precompute.materializer import RetainedCampaignArtifactMaterializer
from adapters.jobs.insight_precompute.planning import RetainedPublicationPlanner
from adapters.jobs.insight_precompute.requests import CampaignRetainedPublicationRequestBuilder
from adapters.outbound.spend_roster import SpendRosterReader
from adapters.outbound.telemetry import tracing
from adapters.outbound.telemetry.tracing import INSIGHT_PRECOMPUTE
from domain.semantics.identity import CommerceScopeKey
from domain.semantics.publication import SuccessfulPublication
from domain.shared_kernel.errors import InvariantError

pytestmark = pytest.mark.unit


@dataclass
class Reader:
    publication: object | None
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def current_for_trigger(
        self, partition_key: str, publication_id: str
    ) -> SuccessfulPublication | None:
        self.calls.append((partition_key, publication_id))
        return cast("SuccessfulPublication | None", self.publication)


@dataclass
class Planner:
    requests: tuple[object, ...] = ()
    received: list[object] = field(default_factory=list[object])
    error: Exception | None = None

    def requests_for(self, publication: SuccessfulPublication) -> tuple[object, ...]:
        self.received.append(publication)
        if self.error is not None:
            raise self.error
        return self.requests


@dataclass
class Assembler:
    received: list[tuple[object, ...]] = field(default_factory=list[tuple[object, ...]])

    def many(self, requests: tuple[object, ...]) -> tuple[object, ...]:
        self.received.append(requests)
        return ()


def _record(
    *, event_id: str = "event-1", payload: str = '{"publication_id":"publication-1"}'
) -> dict[str, object]:
    return {
        "eventID": event_id,
        "eventName": "MODIFY",
        "dynamodb": {
            "NewImage": {
                "partition_key": {"S": "partition-retained"},
                "record_key": {"S": "CURRENT"},
                "payload": {"S": payload},
                "subject": {"S": "browser-controlled-and-ignored"},
                "window": {"S": "2099-01-01"},
            }
        },
    }


def _application(
    reader: Reader, planner: Planner, assembler: Assembler
) -> PublicationInsightPrecomputeApplication:
    return PublicationInsightPrecomputeApplication(
        reader=reader,
        planner=cast("PublicationInsightPlanner", planner),
        assembler=cast("InsightSlotAssembler", assembler),
        max_requests_per_publication=2,
    )


def _publication() -> SuccessfulPublication:
    """Build a retained current publication; the companion clock reads its published_at."""
    return successful_publication(
        "fact_unit_test",
        date(2026, 8, 10),
        PublicationSpec("publication-1", datetime(2026, 8, 19, 12, tzinfo=UTC)),
    )


def test_worker_re_reads_retained_publication_and_ignores_event_subject_and_window() -> None:
    publication = _publication()
    reader, planner, assembler = Reader(publication), Planner(), Assembler()

    failures = apply_records({"Records": [_record()]}, _application(reader, planner, assembler))

    assert failures == ()
    assert reader.calls == [("partition-retained", "publication-1")]
    assert planner.received == [publication]
    assert assembler.received == []


def test_worker_assembles_only_planner_derived_requests_and_bounds_fan_out() -> None:
    publication = _publication()
    planned = (object(), object())
    reader, planner, assembler = Reader(publication), Planner(planned), Assembler()

    _application(reader, planner, assembler).apply(
        PublicationTrigger("partition-retained", "publication-1")
    )

    assert planner.received == [publication]
    assert assembler.received == [planned]


def test_worker_ignores_stale_current_trigger_without_planning_or_generation() -> None:
    reader, planner, assembler = Reader(None), Planner((object(),)), Assembler()

    _application(reader, planner, assembler).apply(
        PublicationTrigger("partition-retained", "stale")
    )

    assert reader.calls == [("partition-retained", "stale")]
    assert planner.received == []
    assert assembler.received == []


def test_worker_returns_only_failing_valid_record_identifiers_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, planner, assembler = (
        Reader(object()),
        Planner(error=RuntimeError("read failed")),
        Assembler(),
    )
    invalid = _record(event_id="ignored", payload="not-json")
    reported: list[BaseException] = []
    monkeypatch.setattr(worker, "report_error", lambda error, _report: reported.append(error))

    failures = apply_records(
        {"Records": [invalid, _record(event_id="retry-me")]},
        _application(reader, planner, assembler),
    )

    assert failures == ("retry-me",)
    assert reader.calls == [("partition-retained", "publication-1")]
    assert len(reported) == 1
    assert isinstance(reported[0], RuntimeError)


def test_worker_rejects_non_current_or_missing_publication_records_before_reader_access() -> None:
    reader, planner, assembler = Reader(object()), Planner(), Assembler()
    deleted = _record()
    deleted["eventName"] = "REMOVE"
    not_current = _record()
    cast("dict[str, object]", cast("dict[str, object]", not_current["dynamodb"])["NewImage"])[
        "record_key"
    ] = {"S": "PUBLICATION#publication-1"}

    failures = apply_records(
        {"Records": [deleted, not_current]}, _application(reader, planner, assembler)
    )

    assert failures == ()
    assert reader.calls == []


@pytest.mark.covers("ADR-068")
def test_application_composes_the_real_retained_planner_without_page_capabilities(  # noqa: PLR0915 -- one composition, every assertion on it
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stream worker must compose governed retained dependencies, never a safe-looking no-op."""
    settings = SimpleNamespace(
        semantic_publications_table="publications",
        semantic_v2_bucket="semantic",
        preset_artifact_bucket="preset-artifacts",
        insight_cache_table="cache",
        insight_prompt_registry_table="prompts",
        insight_quick_model_id="quick-model",
        insight_deep_model_id="deep-model",
        insight_generation_timeout_seconds=4,
        insight_generation_max_concurrency=8,
        insight_precompute_max_requests=40,
        athena_workgroup="adlytics-dev-v2-batch",
        semantic_v2_current_database="adlytics_dev_v2_current",
        athena_result_reuse_max_age_minutes=60,
        # S2: the start budget's 2 x 4 row; no longer compared with the model-call eight.
        athena_query_batch_max_concurrency=4,
    )
    root = object()
    v2_schemas = SimpleNamespace(
        pages=(SimpleNamespace(page_id="campaigns", temporal_policy=object()),)
    )
    bridge = SimpleNamespace(commerce_scope=CommerceScopeKey("commerce-fictional"))
    module = cast("Any", worker)
    monkeypatch.setattr(module, "insight_precompute_settings", lambda: settings)
    monkeypatch.setattr(module, "contracts_root", lambda: root)
    monkeypatch.setattr(module, "load_contracts", cast("Any", lambda _root: ()))
    monkeypatch.setattr(module, "load_source_packs", cast("Any", lambda _root: ()))
    monkeypatch.setattr(module, "load_commerce_identity_bridge", cast("Any", lambda _root: bridge))
    monkeypatch.setattr(module, "load_media_taxonomy", cast("Any", lambda _root: object()))
    monkeypatch.setattr(module, "presentation_dimension_labels", cast("Any", lambda *_args: ()))
    monkeypatch.setattr(module, "campaign_platform_labels", cast("Any", lambda *_args: ()))
    monkeypatch.setattr(module, "DynamoCurrentPublications", cast("Any", lambda _table: object()))
    monkeypatch.setattr(module, "EntityCatalogTooling", cast("Any", lambda **_kwargs: object()))
    monkeypatch.setattr(module, "CanonicalEntityCatalog", cast("Any", lambda _tooling: object()))
    monkeypatch.setattr(module, "S3CanonicalAdapter", cast("Any", lambda _bucket: object()))
    monkeypatch.setattr(module, "S3PresetArtifactBodyStore", cast("Any", lambda _bucket: object()))
    monkeypatch.setattr(
        module, "S3PresetArtifactCustodyStore", cast("Any", lambda _bucket: object())
    )
    monkeypatch.setattr(module, "load_catalog", cast("Any", lambda _root: object()))
    monkeypatch.setattr(module, "load_page_schemas", cast("Any", lambda _root: object()))
    monkeypatch.setattr(
        module,
        "campaign_coverage_requirements",
        cast("Any", lambda *_args: (object(),)),
    )
    monkeypatch.setattr(module, "load_v2_page_schemas", cast("Any", lambda _root: v2_schemas))
    monkeypatch.setattr(module, "PageWindowCatalog", cast("Any", lambda *_args: object()))
    monkeypatch.setattr(module, "load_insight_contracts", cast("Any", lambda _root: ()))
    monkeypatch.setattr(module, "AthenaQueryAdapter", cast("Any", lambda *_args: object()))
    monkeypatch.setattr(module, "PublicationSelector", cast("Any", lambda *_args: object()))
    labels = {"sponsored_products": "Sponsored Products"}
    monkeypatch.setattr(module, "ad_product_labels", cast("Any", lambda _taxonomy: labels))
    monkeypatch.setattr(module, "DynamoInsightCache", cast("Any", lambda _table: object()))
    monkeypatch.setattr(module, "DynamoInsightPromptRegistry", cast("Any", lambda _table: object()))
    monkeypatch.setattr(module, "BedrockInsightGenerator", cast("Any", lambda *_args: object()))
    monkeypatch.setattr(module, "InsightSlotAssembler", cast("Any", lambda **_kwargs: object()))
    worker.application.cache_clear()

    composed = worker.application()

    assert isinstance(composed.planner, RetainedPublicationPlanner)
    assert isinstance(composed.graph_planner, worker.StagedArtifactInsightPlanner)
    assert isinstance(composed.planner.request_builder, CampaignRetainedPublicationRequestBuilder)
    assert isinstance(
        composed.planner.request_builder.artifacts,
        RetainedCampaignArtifactMaterializer,
    )
    assert composed.planner.max_requests == settings.insight_precompute_max_requests
    assert composed.planner.logger is worker.LOGGER
    # The roster reader sits on the measurement service, which prepares every request through
    # `attach_roster` (plan D4), bound to the query and publication readers the renderer runs
    # with, so a roster-graded figure reads spend from the publications it is bound to. The
    # renderer's tooling holds none: it only executes what the service prepared.
    artifacts = composed.planner.request_builder.artifacts
    tooling, rosters = artifacts.tooling, artifacts.measurements.dependencies.rosters
    assert isinstance(rosters, SpendRosterReader)
    assert rosters.queries is tooling.queries
    assert rosters.publications is tooling.publications
    assert rosters.labels == labels
    assert tooling.rosters is None
    # The Athena cap 4 reaches the fan-out width unchanged.
    assert tooling.query_batch_max_concurrency == 4
    worker.application.cache_clear()
    # Paired control: (40, 8, 5) is off the reviewed tuple and is refused by the guard.
    settings.insight_generation_timeout_seconds = 5
    with pytest.raises(InvariantError) as refused:
        worker.application()
    assert str(refused.value.code) == "ADLY-GRAPH_PRECOMPUTE-0003"
    worker.application.cache_clear()


class _Context:
    function_name = "insight-precompute"
    function_version = "$LATEST"
    invoked_function_arn = "arn:aws:lambda:us-east-1:111122223333:function:precompute"
    memory_limit_in_mb = 512
    aws_request_id = "request-1"


def test_handler_records_partial_batch_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "TRACER", provider.get_tracer("test"))
    application = _application(
        Reader(object()),
        Planner(error=RuntimeError("read failed")),
        Assembler(),
    )
    reported: list[BaseException] = []
    monkeypatch.setattr(worker, "application", lambda: application)
    monkeypatch.setattr(worker, "report_error", lambda error, _report: reported.append(error))

    result = worker.handler(
        {"Records": [_record(event_id="retry-me"), _record(payload="not-json")]},
        _Context(),
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "retry-me"}]}
    assert len(reported) == 1
    (root,) = [item for item in exporter.get_finished_spans() if item.name == INSIGHT_PRECOMPUTE]
    assert dict(root.attributes or {}) == {
        "operation": INSIGHT_PRECOMPUTE,
        "error_type": "RuntimeError",
        "outcome": "partial_retry",
        "record_count": 2,
        "acknowledged_count": 1,
        "failed_count": 1,
    }


# --- S6 §2.9 (D6): the precompute stream gate --------------------------------------------------


def _gated(  # noqa: PLR0913, PLR0917 -- one fixture builder
    reader: Reader, planner: Planner, mode: Any, drops: list[tuple[str, int]]
) -> PublicationInsightPrecomputeApplication:
    from dataclasses import replace  # noqa: PLC0415

    from adapters.jobs.execution_graph.cutover_gate import CutoverGate  # noqa: PLC0415

    gate = CutoverGate(lambda: mode, lambda signal, count: drops.append((signal, count)))
    return replace(_application(reader, planner, Assembler()), gate=gate)


def test_d6_the_precompute_stream_gate_drops_every_record_in_graph() -> None:
    from domain.operations.execution_graph import AuthorityMode  # noqa: PLC0415

    reader, planner, drops = Reader(_publication()), Planner(), []
    failures = apply_records(
        {"Records": [_record(), _record(event_id="two")]},
        _gated(reader, planner, AuthorityMode.GRAPH, drops),
    )

    assert failures == ()
    assert (reader.calls, planner.received) == ([], [])
    assert drops == [("STREAM_TRIGGER_AFTER_CUTOVER", 2)]


def test_d6_control_the_precompute_stream_proceeds_in_shadow() -> None:
    from domain.operations.execution_graph import AuthorityMode  # noqa: PLC0415

    reader, planner, drops = Reader(_publication()), Planner(), []
    apply_records({"Records": [_record()]}, _gated(reader, planner, AuthorityMode.SHADOW, drops))

    assert reader.calls == [("partition-retained", "publication-1")]
    assert drops == []


def test_a_stream_batch_is_not_a_graph_batch_and_an_sqs_batch_is() -> None:
    assert not worker.is_graph_batch({"Records": [_record()]})
    assert worker.is_graph_batch({"Records": [{"eventSource": "aws:sqs", "body": "{}"}]})
