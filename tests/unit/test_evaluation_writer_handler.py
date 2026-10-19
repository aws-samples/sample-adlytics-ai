# SPDX-License-Identifier: MIT-0
"""Evaluation writer Lambda tracing and content-free failure reporting."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from adapters.outbound.telemetry import tracing
from adapters.outbound.telemetry.reporting import OperationLog
from adapters.outbound.telemetry.tracing import EVALUATION_WRITER
from domain.evaluation.linkage import EvaluationWriterReceiptV1
from domain.shared_kernel.errors import InvariantError

_HANDLER_ENVIRONMENT = {
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "ADL_ENVIRONMENT": "dev",
    "ADL_AWS_REGION": "us-east-1",
    "ADL_TRACING_ENABLED": "false",
    "ADL_TRACE_SAMPLE_RATIO": "1",
    "ADL_EVALUATION_INPUT_LOG_GROUP": "evaluation-input-test",
    "ADL_EVALUATION_INPUT_SERVICE_NAME": "evaluation-test",
    "ADL_EVALUATION_WRITER_RECEIPTS_TABLE": "evaluation-writer-receipts-test",
    "ADL_RECEIPT_RETENTION_DAYS": "90",
}
_PREVIOUS_ENVIRONMENT = {key: os.environ.get(key) for key in _HANDLER_ENVIRONMENT}
os.environ.update(_HANDLER_ENVIRONMENT)

from adapters.jobs.evaluation_writer import handler as writer  # noqa: E402

for _key, _value in _PREVIOUS_ENVIRONMENT.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value

pytestmark = pytest.mark.unit


class _Context:
    function_name = "evaluation-writer"
    function_version = "$LATEST"
    invoked_function_arn = "arn:aws:lambda:us-east-1:111122223333:function:evaluation-writer"
    memory_limit_in_mb = 512
    aws_request_id = "request-1"


_RECEIPT_CREATED_AT = "2026-09-02T12:00:00+00:00"


def _record(**overrides: object) -> dict[str, object]:
    """One queue record exactly as Lambda renders it, with the receipt time the sink attaches."""
    return {
        "messageId": "message-fixture",
        "body": "opaque",
        "messageAttributes": {
            "receipt_created_at": {
                "stringValue": _RECEIPT_CREATED_AT,
                "stringListValues": [],
                "binaryListValues": [],
                "dataType": "String",
            }
        },
        **overrides,
    }


class _FailingLogs:
    def put_log_events(self, **_kwargs: object) -> None:
        raise RuntimeError("provider unavailable")


def _exported_spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "TRACER", provider.get_tracer("test"))
    return exporter


def test_provider_failure_is_reported_without_error_description_and_traced_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = _exported_spans(monkeypatch)
    errors: list[BaseException] = []
    operations: list[OperationLog] = []
    envelope = SimpleNamespace(
        audit_id="audit-fixture",
        evaluation_admission_ref="admission-fixture",
        correlation_id="correlation-fixture",
    )

    def decode(_body: object) -> object:
        return envelope

    def identifiers(_envelope: object) -> tuple[str, str]:
        return "1" * 32, "2" * 16

    def render(_projection: object) -> str:
        return "content-free-span"

    def capture_error(error: BaseException, _report: object) -> None:
        errors.append(error)

    def capture_operation(operation: OperationLog, _logger: object) -> None:
        operations.append(operation)

    monkeypatch.setattr(writer, "_ensure_stream", lambda: None)
    monkeypatch.setattr(writer, "decode_envelope", decode)
    monkeypatch.setattr(writer, "released_span_identifiers", identifiers)
    monkeypatch.setattr(writer, "released_span_json", render)
    monkeypatch.setattr(writer, "_LOGS", _FailingLogs())
    monkeypatch.setattr(writer, "_WRITER_RECEIPTS", _AuditStore())
    monkeypatch.setattr(writer, "report_error", capture_error)
    monkeypatch.setattr(writer, "report_operation", capture_operation)

    result = writer.handler(
        {"Records": [_record()]},
        _Context(),
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "message-fixture"}]}
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    retry = next(operation for operation in operations if operation.outcome == "retry")
    assert retry.description == ""
    assert retry.details == {}
    (root,) = [item for item in exporter.get_finished_spans() if item.name == EVALUATION_WRITER]
    assert dict(root.attributes or {}) == {
        "operation": EVALUATION_WRITER,
        "error_type": "RuntimeError",
        "outcome": "partial_retry",
        "record_count": 1,
        "persisted_count": 0,
        "failed_count": 1,
    }


def test_stream_setup_failure_is_reported_and_re_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    error = RuntimeError("stream unavailable")
    reported: list[BaseException] = []

    def fail_stream() -> None:
        raise error

    def capture_error(caught: BaseException, _report: object) -> None:
        reported.append(caught)

    monkeypatch.setattr(writer, "_ensure_stream", fail_stream)
    monkeypatch.setattr(writer, "report_error", capture_error)

    with pytest.raises(RuntimeError, match="stream unavailable"):
        writer.handler({"Records": []}, _Context())

    assert reported == [error]


class _SuccessfulLogs:
    def put_log_events(self, **_kwargs: object) -> None:
        return None


class _AuditStore:
    """The writer receipt port the handler holds, keeping what it was given."""

    def __init__(self) -> None:
        self.receipts: list[EvaluationWriterReceiptV1] = []
        self.receipt_times: list[str] = []

    def write_evaluation_writer_receipt(
        self, receipt: EvaluationWriterReceiptV1, receipt_created_at: str
    ) -> None:
        self.receipts.append(receipt)
        self.receipt_times.append(receipt_created_at)

    def get_evaluation_writer_receipt(
        self, audit_id: str, admission_ref: str
    ) -> EvaluationWriterReceiptV1 | None:
        return next(
            (
                receipt
                for receipt in self.receipts
                if receipt.audit_id == audit_id and receipt.admission_ref == admission_ref
            ),
            None,
        )


def test_persisted_lifecycle_contains_counts_without_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[OperationLog] = []
    audit_store = _AuditStore()
    envelope = SimpleNamespace(
        audit_id="audit-fixture",
        evaluation_admission_ref="admission-fixture",
        correlation_id="correlation-fixture",
    )

    def decode(_body: object) -> object:
        return envelope

    def identifiers(_envelope: object) -> tuple[str, str]:
        return "1" * 32, "2" * 16

    def render(_projection: object) -> str:
        return "content-free-span"

    def capture_operation(operation: OperationLog, _logger: object) -> None:
        operations.append(operation)

    monkeypatch.setattr(writer, "_ensure_stream", lambda: None)
    monkeypatch.setattr(writer, "decode_envelope", decode)
    monkeypatch.setattr(writer, "released_span_identifiers", identifiers)
    monkeypatch.setattr(writer, "released_span_json", render)
    monkeypatch.setattr(writer, "_LOGS", _SuccessfulLogs())
    monkeypatch.setattr(writer, "_WRITER_RECEIPTS", audit_store)
    monkeypatch.setattr(writer, "report_operation", capture_operation)

    result = writer.handler(
        {"Records": [_record()]},
        _Context(),
    )

    assert result == {"batchItemFailures": []}
    assert len(audit_store.receipts) == 1
    # The writer receipt expires with its turn's receipt, whose time rides on the queue message.
    assert audit_store.receipt_times == [_RECEIPT_CREATED_AT]
    persisted = next(operation for operation in operations if operation.outcome == "persisted")
    assert persisted.details == {}
    terminal = next(operation for operation in operations if operation.phase == "terminal")
    assert terminal.details == {"record_count": 1, "failed_count": 0}


class _RecordingLogs:
    def __init__(self) -> None:
        self.calls = 0

    def put_log_events(self, **_kwargs: object) -> None:
        self.calls += 1


def _any_envelope(_body: object) -> object:
    return SimpleNamespace()


def _ignore_error(_error: BaseException, _report: object) -> None:
    return None


@pytest.mark.covers("ADR-081")
def test_a_message_without_its_receipt_time_emits_no_span_and_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The time is read before the span is emitted, so a refusal leaves no orphaned span."""
    audit_store = _AuditStore()
    logs = _RecordingLogs()
    monkeypatch.setattr(writer, "_ensure_stream", lambda: None)
    monkeypatch.setattr(writer, "decode_envelope", _any_envelope)
    monkeypatch.setattr(writer, "_LOGS", logs)
    monkeypatch.setattr(writer, "_WRITER_RECEIPTS", audit_store)
    monkeypatch.setattr(writer, "report_error", _ignore_error)

    result = writer.handler({"Records": [_record(messageAttributes={})]}, _Context())

    assert result == {"batchItemFailures": [{"itemIdentifier": "message-fixture"}]}
    assert logs.calls == 0
    assert audit_store.receipts == []


_TRACE_ID = "1" * 32
_SPAN_ID = "2" * 16


def _wire_success(
    monkeypatch: pytest.MonkeyPatch, audit_store: _AuditStore, logs: _RecordingLogs
) -> list[OperationLog]:
    operations: list[OperationLog] = []
    envelope = SimpleNamespace(
        audit_id="audit-fixture",
        evaluation_admission_ref="admission-fixture",
        correlation_id="correlation-fixture",
    )

    def decode(_body: object) -> object:
        return envelope

    def identifiers(_envelope: object) -> tuple[str, str]:
        return _TRACE_ID, _SPAN_ID

    def render(_projection: object) -> str:
        return "content-free-span"

    def capture_operation(operation: OperationLog, _logger: object) -> None:
        operations.append(operation)

    monkeypatch.setattr(writer, "_ensure_stream", lambda: None)
    monkeypatch.setattr(writer, "decode_envelope", decode)
    monkeypatch.setattr(writer, "released_span_identifiers", identifiers)
    monkeypatch.setattr(writer, "released_span_json", render)
    monkeypatch.setattr(writer, "_LOGS", logs)
    monkeypatch.setattr(writer, "_WRITER_RECEIPTS", audit_store)
    monkeypatch.setattr(writer, "report_operation", capture_operation)
    return operations


@pytest.mark.covers("ADR-080")
def test_a_redelivered_message_after_a_successful_attempt_neither_re_emits_nor_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Lambda timeout after the write returns the message.

    A second span would be a second evaluation input for one admission, and a receipt conflict
    would dead-letter the message.
    """
    audit_store = _AuditStore()
    logs = _RecordingLogs()
    operations = _wire_success(monkeypatch, audit_store, logs)

    first = writer.handler({"Records": [_record()]}, _Context())
    redelivered = writer.handler({"Records": [_record()]}, _Context())

    assert first == {"batchItemFailures": []}
    assert redelivered == {"batchItemFailures": []}
    assert logs.calls == 1
    assert len(audit_store.receipts) == 1
    outcomes = [operation.outcome for operation in operations if operation.phase == "record"]
    assert outcomes == ["persisted", "replayed"]


@pytest.mark.covers("ADR-080")
def test_a_stored_receipt_naming_another_span_is_a_conflict_and_emits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_store = _AuditStore()
    audit_store.receipts.append(
        EvaluationWriterReceiptV1(
            audit_id="audit-fixture",
            admission_ref="admission-fixture",
            trace_id=_TRACE_ID,
            span_id="3" * 16,
            emitted_at="2026-09-02T12:00:01+00:00",
        )
    )
    logs = _RecordingLogs()
    _wire_success(monkeypatch, audit_store, logs)
    errors: list[BaseException] = []

    def capture_error(error: BaseException, _report: object) -> None:
        errors.append(error)

    monkeypatch.setattr(writer, "report_error", capture_error)

    result = writer.handler({"Records": [_record()]}, _Context())

    assert result == {"batchItemFailures": [{"itemIdentifier": "message-fixture"}]}
    assert logs.calls == 0
    assert len(audit_store.receipts) == 1
    assert [type(error) for error in errors] == [InvariantError]
