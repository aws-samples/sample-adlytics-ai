# SPDX-License-Identifier: MIT-0
"""Tests for the error taxonomy and its structured failure reporting."""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import pytest
from aws_lambda_powertools.metrics import MetricUnit

from adapters.outbound.telemetry import reporting
from adapters.outbound.telemetry.reporting import (
    ConversationInvestigationNext,
    ConversationTerminalLog,
    EvaluationState,
    GatewayInvestigationNext,
    GatewayResultClass,
    GatewayTerminalLog,
    LogSeverity,
    ManagedTerminal,
    OperationLog,
    Outcome,
    PublicationOutcome,
    Report,
    flushes_metrics,
    normalize_correlation_id,
    report_denial,
    report_error,
    report_lifecycle,
    report_operation,
)
from domain.evaluation.quality import VerdictCount
from domain.presentation.insights import InsightCacheOutcome, InsightPromptTier
from domain.shared_kernel.errors import (
    DependencyError,
    DomainError,
    ErrorClass,
    ErrorCode,
    ErrorMetric,
    InvariantError,
    ValidationError,
)

if TYPE_CHECKING:
    from aws_lambda_powertools import Logger

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-010", "G-14")]


@dataclass
class _RecordingLogger:
    """Capture the structured error call without configuring a log sink."""

    debugs: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    errors: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    warnings: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    infos: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )

    def debug(self, message: str, *, extra: dict[str, Any]) -> None:
        self.debugs.append((message, extra))

    def info(self, message: str, *, extra: dict[str, Any]) -> None:
        self.infos.append((message, extra))

    def error(self, message: str, *, extra: dict[str, Any]) -> None:
        self.errors.append((message, extra))

    def warning(self, message: str, *, extra: dict[str, Any]) -> None:
        self.warnings.append((message, extra))


class TestCorrelationNormalization:
    def test_a_canonical_uuid_is_preserved(self) -> None:
        correlation_id = "123e4567-e89b-42d3-a456-426614174000"

        assert normalize_correlation_id(correlation_id) == correlation_id

    @pytest.mark.parametrize(
        "supplied",
        [
            None,
            "",
            "not-a-uuid",
            "person@example.com",
            "123e4567-e89b-42d3-a456-426614174000-extra",
            "123E4567-E89B-42D3-A456-426614174000",
            "123e4567-e89b-12d3-a456-426614174000",
        ],
    )
    def test_absent_malformed_overlong_and_pii_like_values_are_replaced(
        self, supplied: object
    ) -> None:
        normalized = normalize_correlation_id(supplied)

        assert normalized != supplied
        assert str(UUID(normalized)) == normalized
        assert UUID(normalized).version == 4


class TestClosedLifecycleReporting:
    def test_conversation_terminal_uses_only_the_closed_post_audit_projection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logger = _RecordingLogger()
        monkeypatch.setattr(
            reporting,
            "current_trace_ids",
            lambda: {"trace_id": "a" * 32, "span_id": "b" * 16},
        )

        report_lifecycle(
            ConversationTerminalLog(
                correlation_id="123e4567-e89b-42d3-a456-426614174000",
                managed_terminal=ManagedTerminal.COMPLETE,
                publication_outcome=PublicationOutcome.PUBLISHED,
                audit_id="audit-opaque",
                evaluation_state=EvaluationState.PENDING,
                investigation_next=ConversationInvestigationNext.TERMINAL_AUDIT,
            ),
            cast("Logger", logger),
        )

        assert logger.infos == [
            (
                "managed lifecycle",
                {
                    "operation": "conversation.turn",
                    "component": "conversation_api",
                    "phase": "terminal",
                    "outcome": "completed",
                    "correlation_id": "123e4567-e89b-42d3-a456-426614174000",
                    "managed_terminal": "complete",
                    "publication_outcome": "published",
                    "audit_id": "audit-opaque",
                    "audit_state": "persisted",
                    "evaluation_state": "pending",
                    "investigation_next": "inspect_terminal_audit",
                    "trace_id": "a" * 32,
                    "span_id": "b" * 16,
                },
            )
        ]

    def test_gateway_uses_only_its_local_reference_and_closed_result_class(self) -> None:
        event = GatewayTerminalLog(
            operation="catalog_list_metrics",
            outcome=Outcome.ANSWERED,
            result_class=GatewayResultClass.ANSWER,
            investigation_next=GatewayInvestigationNext.MANAGED_TOOL_TRACE,
            gateway_message_ref="mcp-local",
            caller_verified=True,
        )

        assert event.projection == {
            "operation": "catalog_list_metrics",
            "component": "gateway_target",
            "phase": "terminal",
            "outcome": "answered",
            "result_class": "answer",
            "investigation_next": "inspect_managed_tool_trace",
            "caller_verified": True,
            "gateway_message_ref": "mcp-local",
        }
        assert {"correlation_id", "traceparent", "tracestate", "baggage"}.isdisjoint(
            event.projection
        )

    def test_debug_operation_uses_the_shared_structured_projection(self) -> None:
        logger = _RecordingLogger()

        report_operation(
            OperationLog(
                operation="presentation.channel_performance",
                component="presentation_api",
                phase="start",
                outcome="started",
                severity=LogSeverity.DEBUG,
                description="authorization=credential-value",
                details={"resource": "channel-page", "status": 0},
            ),
            cast("Logger", logger),
        )

        assert logger.debugs == [
            (
                "operation lifecycle",
                {
                    "operation": "presentation.channel_performance",
                    "component": "presentation_api",
                    "phase": "start",
                    "outcome": "started",
                    "resource": "channel-page",
                    "status": 0,
                    "description": "authorization=[REDACTED]",
                },
            )
        ]


class TestErrorCode:
    def test_formats_with_zero_padding(self) -> None:
        assert str(ErrorCode("DATA", 104)) == "ADLY-DATA-0104"

    def test_uppercases_the_area(self) -> None:
        assert str(ErrorCode("data", 1)) == "ADLY-DATA-0001"


class TestAlarming:
    """Which classes page someone. The whole point of the taxonomy."""

    def test_domain_error_never_alarms(self) -> None:
        # "No data for that window" is an answer, not a failure. Alarming here
        # would drown the alarms that matter.
        error = DomainError(ErrorCode("DATA", 1), "No data for that window.")
        assert error.alarms is False

    def test_validation_error_never_alarms(self) -> None:
        assert ValidationError(ErrorCode("API", 1), "Bad range.").alarms is False

    def test_dependency_error_alarms(self) -> None:
        assert DependencyError(ErrorCode("ATHENA", 1), "Query failed.").alarms is True

    def test_invariant_error_alarms(self) -> None:
        assert InvariantError(ErrorCode("MODEL", 1), "Impossible grain.").alarms is True


class TestUserVisibility:
    def test_domain_reason_reaches_the_user(self) -> None:
        error = DomainError(
            ErrorCode("DATA", 1),
            "Only 6 weeks of history exist; 12 are needed to forecast.",
        )
        assert "6 weeks" in error.user_visible_description

    def test_dependency_detail_never_reaches_the_user(self) -> None:
        # The real description can name infrastructure. It must stay in the log.
        error = DependencyError(
            ErrorCode("ATHENA", 7),
            "Athena query abc-123 exceeded BytesScannedCutoffPerQuery.",
            context={"query_id": "abc-123"},
        )
        visible = error.user_visible_description
        assert "abc-123" not in visible
        assert "Athena" not in visible

    def test_invariant_detail_never_reaches_the_user(self) -> None:
        error = InvariantError(ErrorCode("MODEL", 2), "fct_ad_exposure lost its key.")
        assert "fct_ad_exposure" not in error.user_visible_description


class TestErrorMetric:
    def test_dimensions_are_low_cardinality(self) -> None:
        error = DomainError(ErrorCode("DATA", 1), "No data.")
        metric = ErrorMetric.from_error(error, operation="measurement.roas")

        assert metric.dimensions == {
            "ErrorClass": "domain",
            "ErrorCode": "ADLY-DATA-0001",
            "Operation": "measurement.roas",
        }

    def test_context_is_not_promoted_to_a_dimension(self) -> None:
        # Unbounded values must never become EMF dimensions.
        error = DependencyError(
            ErrorCode("DDB", 3),
            "Lookup failed.",
            context={"user_id": "sub-abc-123", "correlation_id": "corr-xyz"},
        )
        metric = ErrorMetric.from_error(error, operation="forecast.read")

        assert "user_id" not in metric.dimensions
        assert "correlation_id" not in metric.dimensions
        assert set(metric.dimensions) == {"ErrorClass", "ErrorCode", "Operation"}

    def test_carries_the_class_through(self) -> None:
        error = InvariantError(ErrorCode("MODEL", 9), "Bad state.")
        assert ErrorMetric.from_error(error, "x").error_class is ErrorClass.INVARIANT


class TestSafeErrorReporting:
    def test_only_allowlisted_bounded_context_reaches_the_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logger = _RecordingLogger()
        monkeypatch.setattr(
            reporting,
            "current_trace_ids",
            lambda: {"trace_id": "a" * 32, "span_id": "b" * 16},
        )
        error = DependencyError(
            ErrorCode("STORE", 8),
            "A safe description.",
            context={
                "publication_id": "publication-7",
                "source_publication_id": "source-publication-8",
                "scope": "scope-9",
                "query_id": "query-10",
                "service_code": "ProvisionedThroughputExceededException",
                "arguments": ["caller-argument-secret"],
                "unknown": ["caller-unknown-secret"],
                "candidates": ["candidate-1", object(), ["candidate-2"]],
                "actor_id": "actor-secret",
                "owner_id": "owner-secret",
                "user_id": "user-secret",
                "session_id": "session-secret",
                "page_ref": "page-secret",
                "selection_ref": "selection-secret",
                "nonce": "nonce-secret",
                "grounding_id": "grounding-secret",
                "fencing_token": "fencing-secret",
                "cookie": "cookie-secret",
                "credential": "credential-secret",
                "raw_payload": "payload-secret",
                "question": "question-secret",
                "model_output": "model-secret",
                "unknown_key": "unknown-secret",
            },
        )

        report_error(
            error,
            Report(
                operation="publication.read",
                correlation_id="123e4567-e89b-42d3-a456-426614174000",
                logger=cast("Logger", logger),
            ),
        )

        context = logger.errors[0][1]["error_context"]
        assert logger.errors[0][1]["trace_id"] == "a" * 32
        assert logger.errors[0][1]["span_id"] == "b" * 16
        assert context["publication_id"] == "publication-7"
        assert context["source_publication_id"] == "source-publication-8"
        assert context["scope"] == "scope-9"
        assert context["query_id"] == "query-10"
        assert context["service_code"] == "ProvisionedThroughputExceededException"
        assert context["candidates"] == ["candidate-1", ["candidate-2"]]
        for key in (
            "arguments",
            "unknown",
            "actor_id",
            "owner_id",
            "user_id",
            "session_id",
            "page_ref",
            "selection_ref",
            "nonce",
            "grounding_id",
            "question",
            "model_output",
            "unknown_key",
        ):
            expected = ["[REDACTED]"] if key in {"arguments", "unknown"} else "[REDACTED]"
            assert context[key] == expected
        assert {"fencing_token", "cookie", "credential", "raw_payload"}.isdisjoint(context)
        serialized = json.dumps(logger.errors[0][1], sort_keys=True)
        for secret in (
            "caller-argument-secret",
            "caller-unknown-secret",
            "actor-secret",
            "owner-secret",
            "user-secret",
            "session-secret",
            "page-secret",
            "selection-secret",
            "nonce-secret",
            "grounding-secret",
            "fencing-secret",
            "cookie-secret",
            "credential-secret",
            "payload-secret",
            "question-secret",
            "model-secret",
            "unknown-secret",
        ):
            assert secret not in serialized

    def test_an_unexpected_error_logs_only_type_and_stack_locations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logger = _RecordingLogger()
        monkeypatch.setattr(
            reporting,
            "current_trace_ids",
            lambda: {"trace_id": "c" * 32, "span_id": "d" * 16},
        )

        def fail() -> None:
            # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
            secret_local = "local-secret"  # nosec B105
            raise RuntimeError(f"message-secret {secret_local}")

        try:
            fail()
        except RuntimeError as error:
            report_error(
                error,
                Report(
                    operation="conversation.turn",
                    correlation_id="123e4567-e89b-42d3-a456-426614174000",
                    logger=cast("Logger", logger),
                ),
            )

        assert len(logger.errors) == 1
        message, extra = logger.errors[0]
        assert message == "unexpected failure"
        assert extra["error_type"] == "RuntimeError"
        assert extra["error_class"] == "invariant"
        assert extra["error_code"] == "ADLY-UNEXPECTED-0000"
        assert extra["alarms"] is True
        assert extra["trace_id"] == "c" * 32
        assert extra["span_id"] == "d" * 16
        assert extra["stack"]
        assert all(set(location) == {"filename", "function", "line"} for location in extra["stack"])
        serialized = json.dumps(logger.errors[0], sort_keys=True)
        assert "message-secret" not in serialized
        assert "local-secret" not in serialized


class TestSafeDenialReporting:
    def test_logs_a_fixed_category_and_preserves_the_denial_metric(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logger = _RecordingLogger()
        dimensions: list[tuple[str, str]] = []
        metrics: list[tuple[str, MetricUnit, float]] = []

        def record_dimension(*, name: str, value: str) -> None:
            dimensions.append((name, value))

        def record_metric(*, name: str, unit: MetricUnit, value: float) -> None:
            metrics.append((name, unit, value))

        monkeypatch.setattr(reporting.METRICS, "add_dimension", record_dimension)
        monkeypatch.setattr(reporting.METRICS, "add_metric", record_metric)
        monkeypatch.setattr(
            reporting,
            "current_trace_ids",
            lambda: {"trace_id": "e" * 32, "span_id": "f" * 16},
        )

        report_denial(
            "provider detail with customer-secret",
            Report(
                operation="conversation.turn",
                correlation_id="123e4567-e89b-42d3-a456-426614174000",
                gateway_message_ref="gateway-message-ref",
                logger=cast("Logger", logger),
            ),
        )

        assert logger.warnings == [
            (
                "authorization denied",
                {
                    "trace_id": "e" * 32,
                    "span_id": "f" * 16,
                    "correlation_id": "123e4567-e89b-42d3-a456-426614174000",
                    "gateway_message_ref": "gateway-message-ref",
                    "operation": "conversation.turn",
                    "category": "authorization_failed",
                    "description": "provider detail with [REDACTED]",
                },
            )
        ]
        assert "customer-secret" not in json.dumps(logger.warnings)
        assert dimensions == [("Operation", "conversation.turn")]
        assert metrics == [("AuthorizationDenied", MetricUnit.Count, 1)]


class TestFlushMetricsDecorator:
    def test_flushes_after_return(self, monkeypatch: pytest.MonkeyPatch) -> None:
        flushed: list[bool] = []
        monkeypatch.setattr(reporting, "flush_metrics", lambda: flushed.append(True))

        @flushes_metrics
        def handler() -> str:
            return "result"

        assert handler() == "result"
        assert flushed == [True]

    def test_flushes_after_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        flushed: list[bool] = []
        monkeypatch.setattr(reporting, "flush_metrics", lambda: flushed.append(True))

        @flushes_metrics
        def handler() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            handler()
        assert flushed == [True]


def test_each_error_metric_uses_an_isolated_dimension_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, str]] = []

    class Metric:
        def __init__(self) -> None:
            self.dimensions: dict[str, str] = {}

        def add_dimension(self, *, name: str, value: str) -> None:
            self.dimensions[name] = value

    @contextmanager
    def isolated_metric(**_kwargs: object) -> Generator[Metric]:
        metric = Metric()
        yield metric
        emitted.append(metric.dimensions)

    logger = _RecordingLogger()
    monkeypatch.setattr(reporting, "single_metric", isolated_metric)
    for error, operation in (
        (DependencyError(ErrorCode("STORE", 1), "dependency"), "first.operation"),
        (InvariantError(ErrorCode("MODEL", 2), "invariant"), "second.operation"),
    ):
        report_error(error, Report(operation, "", cast("Logger", logger)))

    assert emitted == [
        {
            "ErrorClass": "dependency",
            "ErrorCode": "ADLY-STORE-0001",
            "Operation": "first.operation",
        },
        {
            "ErrorClass": "invariant",
            "ErrorCode": "ADLY-MODEL-0002",
            "Operation": "second.operation",
        },
    ]


def test_insight_cache_outcome_metric_uses_only_the_closed_tier_and_outcome_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, str]] = []

    class Metric:
        def __init__(self) -> None:
            self.dimensions: dict[str, str] = {}

        def add_dimension(self, *, name: str, value: str) -> None:
            self.dimensions[name] = value

    @contextmanager
    def isolated_metric(**kwargs: object) -> Generator[Metric]:
        assert kwargs == {
            "name": "InsightCacheOutcome",
            "unit": MetricUnit.Count,
            "value": 1,
            "namespace": "Adlytics",
        }
        metric = Metric()
        yield metric
        emitted.append(metric.dimensions)

    monkeypatch.setattr(reporting, "single_metric", isolated_metric)

    reporting.report_cache_outcome(InsightPromptTier.DEEP, InsightCacheOutcome.HIT)

    assert emitted == [{"Tier": "deep", "Outcome": "hit"}]


def _capture_single_metrics(monkeypatch: pytest.MonkeyPatch, name: str) -> list[dict[str, str]]:
    """Record each isolated metric's dimensions, asserting its exact name and namespace."""
    emitted: list[dict[str, str]] = []

    class Metric:
        def __init__(self) -> None:
            self.dimensions: dict[str, str] = {}

        def add_dimension(self, *, name: str, value: str) -> None:
            self.dimensions[name] = value

    @contextmanager
    def isolated_metric(**kwargs: object) -> Generator[Metric]:
        assert kwargs == {
            "name": name,
            "unit": MetricUnit.Count,
            "value": 1,
            "namespace": "Adlytics",
        }
        metric = Metric()
        yield metric
        emitted.append(metric.dimensions)

    monkeypatch.setattr(reporting, "single_metric", isolated_metric)
    return emitted


@pytest.mark.covers("ADR-079")
def test_the_admission_count_is_the_no_dispatch_alarm_denominator_with_no_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`EvaluationQualityStack` alarms on exactly this name; a drift would read OK forever."""
    emitted = _capture_single_metrics(monkeypatch, "EvaluationAdmission")
    reporting.report_evaluation_admission()
    assert emitted == [{}]


@pytest.mark.covers("ADR-079")
def test_a_verdict_counts_by_dimension_and_label_and_by_dimension_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted = _capture_single_metrics(monkeypatch, "EvaluationVerdict")
    reporting.report_evaluation_verdict(VerdictCount(dimension="helpfulness", label="poor"))
    assert emitted == [{"Dimension": "helpfulness", "Label": "poor"}, {"Dimension": "helpfulness"}]
