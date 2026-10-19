# SPDX-License-Identifier: MIT-0
"""Scheduled evaluation-outbox dispatch: the ADR-079 rule per row, tracing, and retry reporting."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from adapters.outbound.telemetry import tracing
from adapters.outbound.telemetry.reporting import EvaluationDispatchOutcome, OperationLog
from adapters.outbound.telemetry.tracing import EVALUATION_OUTBOX_DISPATCH
from domain.conversation.audit import TurnDeliveryDispositionV1, TurnDeliveryRecordV1
from domain.evaluation.linkage import EvaluationNotDispatchedReasonV1
from domain.evaluation.release import (
    EvaluationFamilyV1,
    EvaluationReleaseEvaluatorV1,
    EvaluationReleaseV1,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.evaluation_outbox import (
    EvaluationOutboxDeliverRequest,
    EvaluationOutboxReleaseRequest,
    EvaluationOutboxWithholdRequest,
)

_HANDLER_ENVIRONMENT = {
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "ADL_ENVIRONMENT": "dev",
    "ADL_AWS_REGION": "us-east-1",
    "ADL_TRACING_ENABLED": "false",
    "ADL_TRACE_SAMPLE_RATIO": "1",
    "ADL_EVALUATION_OUTBOX_TABLE": "evaluation-outbox-test",
    "ADL_EVALUATION_DELIVERIES_TABLE": "evaluation-deliveries-test",
    "ADL_EVALUATION_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/111122223333/evaluation",
    "ADL_EVALUATION_OUTBOX_RETENTION_DAYS": "14",
    "ADL_RECEIPT_RETENTION_DAYS": "90",
    "ADL_EVALUATION_RELEASE_PARAMETERS": "/adlytics/dev/evaluation/release-v4",
    "ADL_AGENT_QUALITY_AUDIT_TABLE": "agent-quality-audit-test",
    "ADL_EVALUATION_DELIVERY_WAIT_SECONDS": "420",
}
_PREVIOUS_ENVIRONMENT = {key: os.environ.get(key) for key in _HANDLER_ENVIRONMENT}
os.environ.update(_HANDLER_ENVIRONMENT)

from adapters.jobs.evaluation_outbox_dispatcher import handler as dispatcher  # noqa: E402

for _key, _value in _PREVIOUS_ENVIRONMENT.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-079")]

_LONG_AGO = "2026-09-02T12:00:00+00:00"
_EVALUATOR = EvaluationReleaseEvaluatorV1(
    evaluator_id="fictional-helpful-01",
    evaluator_name="fictional_helpful",
    revision="r1",
    model_id="fictional.judge-model-v1:0",
    prompt_digest="e" * 64,
)
_ENABLED = (
    EvaluationReleaseV1(
        family=EvaluationFamilyV1.V4,
        enabled=True,
        config_id="fictional_released_v4-0001",
        evaluators=(_EVALUATOR,),
        decided_at="2026-09-01T00:00:00+00:00",
    ),
)
_DISABLED: tuple[EvaluationReleaseV1 | None, ...] = (None,)


class _Context:
    function_name = "evaluation-outbox-dispatcher"
    function_version = "$LATEST"
    invoked_function_arn = "arn:aws:lambda:us-east-1:111122223333:function:outbox-dispatch"
    memory_limit_in_mb = 512
    aws_request_id = "request-1"


@dataclass
class _Store:
    admitted_at: str = _LONG_AGO
    due_error: Exception | None = None
    calls: list[str] = field(default_factory=list[str])
    releases: list[EvaluationOutboxReleaseRequest] = field(
        default_factory=list[EvaluationOutboxReleaseRequest]
    )
    delivered: list[EvaluationOutboxDeliverRequest] = field(
        default_factory=list[EvaluationOutboxDeliverRequest]
    )
    withheld: list[EvaluationOutboxWithholdRequest] = field(
        default_factory=list[EvaluationOutboxWithholdRequest]
    )

    def due(self, _request: object) -> tuple[str, ...]:
        self.calls.append("due")
        if self.due_error is not None:
            raise self.due_error
        return ("admission-ref",)

    def claim(self, _request: object) -> Any:
        self.calls.append("claim")
        return SimpleNamespace(
            envelope=SimpleNamespace(
                envelope="opaque-envelope",
                audit_id="audit-01",
                admission_ref="admission-ref",
                admitted_at=self.admitted_at,
            ),
            state=SimpleNamespace(attempt_count=1),
        )

    def release(self, request: EvaluationOutboxReleaseRequest) -> None:
        self.releases.append(request)

    def deliver(self, request: EvaluationOutboxDeliverRequest) -> None:
        self.delivered.append(request)

    def withhold(self, request: EvaluationOutboxWithholdRequest) -> None:
        self.withheld.append(request)


@dataclass
class _Sink:
    failure: Exception | None = None
    receipt_times: list[str] = field(default_factory=list[str])

    def enqueue(self, _envelope: object, receipt_created_at: str) -> str:
        self.receipt_times.append(receipt_created_at)
        if self.failure is not None:
            raise self.failure
        return "delivery-ref"


@dataclass
class _Releases:
    releases: tuple[EvaluationReleaseV1 | None, ...] = _ENABLED
    failure: Exception | None = None

    def read(self) -> tuple[EvaluationReleaseV1 | None, ...]:
        if self.failure is not None:
            raise self.failure
        return self.releases


@dataclass
class _TurnDeliveries:
    disposition: TurnDeliveryDispositionV1 | None = TurnDeliveryDispositionV1.DELIVERED
    reads: list[str] = field(default_factory=list[str])

    def get_delivery(self, audit_id: str) -> TurnDeliveryRecordV1 | None:
        self.reads.append(audit_id)
        if self.disposition is None:
            return None
        return TurnDeliveryRecordV1(
            audit_id=audit_id,
            disposition=self.disposition,
            emitted_block_count=0 if self.disposition is TurnDeliveryDispositionV1.ABORTED else 1,
            recorded_at="2026-09-02T12:00:05+00:00",
        )


@dataclass
class _Run:
    store: _Store
    sink: _Sink
    deliveries: _TurnDeliveries
    outcomes: list[EvaluationDispatchOutcome]
    operations: list[OperationLog]
    errors: list[BaseException]
    exporter: InMemorySpanExporter


def _install(  # noqa: PLR0913 - one seam per collaborator the handler reads
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: _Store | None = None,
    sink: _Sink | None = None,
    releases: _Releases | None = None,
    deliveries: _TurnDeliveries | None = None,
) -> _Run:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "TRACER", provider.get_tracer("test"))
    run = _Run(
        store=store or _Store(),
        sink=sink or _Sink(),
        deliveries=deliveries or _TurnDeliveries(),
        outcomes=[],
        operations=[],
        errors=[],
        exporter=exporter,
    )
    monkeypatch.setattr(dispatcher, "_STORE", run.store)
    monkeypatch.setattr(dispatcher, "_SINK", run.sink)
    monkeypatch.setattr(dispatcher, "_RELEASES", releases or _Releases())
    monkeypatch.setattr(dispatcher, "_TURN_DELIVERIES", run.deliveries)

    def capture_operation(operation: OperationLog, _logger: object) -> None:
        run.operations.append(operation)

    def capture_error(error: BaseException, _report: object) -> None:
        run.errors.append(error)

    monkeypatch.setattr(dispatcher, "report_evaluation_dispatch", run.outcomes.append)
    monkeypatch.setattr(dispatcher, "report_operation", capture_operation)
    monkeypatch.setattr(dispatcher, "report_error", capture_error)
    return run


def _root_attributes(run: _Run) -> dict[str, object]:
    (root,) = [
        item
        for item in run.exporter.get_finished_spans()
        if item.name == EVALUATION_OUTBOX_DISPATCH
    ]
    return dict(root.attributes or {})


def test_enqueue_failure_is_reported_released_and_traced_as_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _install(monkeypatch, sink=_Sink(failure=RuntimeError("queue unavailable")))

    result = dispatcher.handler({}, _Context())

    assert result == {"due": 1, "delivered": 0, "not_dispatched": 0, "waiting": 0, "retried": 1}
    # The route admits a row at its receipt's creation time; that instant rides to the writer.
    assert run.sink.receipt_times == [_LONG_AGO]
    assert len(run.store.releases) == 1
    assert run.store.releases[0].count_attempt  # a failed enqueue is an attempt
    assert run.store.delivered == []
    assert [type(error) for error in run.errors] == [RuntimeError]
    assert run.outcomes == [EvaluationDispatchOutcome.RETRY]
    assert [operation.outcome for operation in run.operations] == ["retry"]
    assert _root_attributes(run) == {
        "operation": EVALUATION_OUTBOX_DISPATCH,
        "error_type": "RuntimeError",
        "outcome": "retry",
        "due_count": 1,
        "delivered_count": 0,
        "not_dispatched_count": 0,
        "waiting_count": 0,
        "retry_count": 1,
        "skipped_count": 0,
    }


def test_due_failure_is_reported_and_re_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    error = RuntimeError("store unavailable")
    run = _install(monkeypatch, store=_Store(due_error=error))

    with pytest.raises(RuntimeError, match="store unavailable"):
        dispatcher.handler({}, _Context())

    assert run.errors == [error]


def test_an_unreadable_release_record_fails_the_run_before_anything_is_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decision the dispatcher could not see must neither send nor withhold a row."""
    failure = DependencyError(ErrorCode("EVALUATION", 6), "Evaluation release records unreadable.")
    run = _install(monkeypatch, releases=_Releases(failure=failure))

    with pytest.raises(DependencyError):
        dispatcher.handler({}, _Context())

    assert run.store.calls == []
    assert (run.store.delivered, run.store.withheld, run.store.releases) == ([], [], [])
    assert run.outcomes == []
    assert run.errors == [failure]


def test_a_delivered_answer_is_sent_then_settled_with_the_enabled_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _install(monkeypatch)
    before = datetime.now(UTC)

    result = dispatcher.handler({}, _Context())

    (delivered,) = run.store.delivered
    assert [(family.family, family.config_id) for family in delivered.families] == [
        (EvaluationFamilyV1.V4, "fictional_released_v4-0001")
    ]
    assert delivered.families[0].evaluators == (_EVALUATOR,)
    decided = datetime.fromisoformat(delivered.decided_at)
    assert decided >= before
    # The delivered-row expiry is the decision time plus 14 days, in whole epoch seconds.
    assert delivered.expires_at_epoch == int((decided + timedelta(days=14)).timestamp())
    assert run.sink.receipt_times == [_LONG_AGO]
    assert run.outcomes == [EvaluationDispatchOutcome.DISPATCHED]
    assert result["delivered"] == 1


def test_with_no_enabled_family_a_row_is_withheld_without_reading_its_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _install(monkeypatch, releases=_Releases(releases=_DISABLED))

    result = dispatcher.handler({}, _Context())

    (withheld,) = run.store.withheld
    assert withheld.reason is EvaluationNotDispatchedReasonV1.EVALUATION_NOT_ENABLED
    assert withheld.lease_ref
    decided = datetime.fromisoformat(withheld.decided_at)
    # A withheld row keeps its content no longer than a delivered row: 14 days, epoch seconds.
    assert withheld.expires_at_epoch == int((decided + timedelta(days=14)).timestamp())
    assert run.deliveries.reads == []
    assert run.sink.receipt_times == []
    assert run.outcomes == [EvaluationDispatchOutcome.EVALUATION_NOT_ENABLED]
    assert result == {"due": 1, "delivered": 0, "not_dispatched": 1, "waiting": 0, "retried": 0}


@pytest.mark.parametrize(
    ("disposition", "reason"),
    [
        (TurnDeliveryDispositionV1.TRUNCATED, "answer_not_received"),
        (TurnDeliveryDispositionV1.ABORTED, "answer_not_received"),
        (None, "delivery_unknown"),
    ],
    ids=["truncated", "aborted", "absent-past-wait"],
)
def test_a_row_whose_answer_was_not_received_or_is_unknown_is_withheld_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
    disposition: TurnDeliveryDispositionV1 | None,
    reason: str,
) -> None:
    """Each row was admitted long before the 420-second wait, so absence is final."""
    run = _install(
        monkeypatch,
        store=_Store(admitted_at=_LONG_AGO),
        deliveries=_TurnDeliveries(disposition=disposition),
    )

    dispatcher.handler({}, _Context())

    (withheld,) = run.store.withheld
    assert withheld.reason.value == reason
    assert run.deliveries.reads == ["audit-01"]
    assert run.sink.receipt_times == []
    assert run.outcomes == [EvaluationDispatchOutcome(reason)]
    attributes = _root_attributes(run)
    assert (attributes["not_dispatched_count"], attributes["delivered_count"]) == (1, 0)


def test_an_absent_delivery_inside_the_wait_releases_the_row_to_poll_in_thirty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _install(
        monkeypatch,
        store=_Store(admitted_at=datetime.now(UTC).isoformat()),
        deliveries=_TurnDeliveries(disposition=None),
    )
    before = datetime.now(UTC)

    result = dispatcher.handler({}, _Context())

    (released,) = run.store.releases
    delay = datetime.fromisoformat(released.available_at) - before
    assert timedelta(seconds=30) <= delay < timedelta(seconds=35)
    # Waiting is not an attempt, so it never lengthens a later enqueue failure's backoff.
    assert released.count_attempt is False
    assert (run.store.withheld, run.store.delivered) == ([], [])
    assert run.outcomes == [EvaluationDispatchOutcome.WAITING]
    assert result["waiting"] == 1
    attributes = _root_attributes(run)
    assert (attributes["waiting_count"], attributes["outcome"]) == (1, "completed")
