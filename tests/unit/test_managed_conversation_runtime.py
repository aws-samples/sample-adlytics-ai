# SPDX-License-Identifier: MIT-0
"""The streamed-answer runtime: event projection, receipts, evaluation handoff, and delivery."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import cast

import pytest
from conversation_publication_fixture import custody, shaped_response
from managed_conversation_support import (
    CORRELATION_ID,
    NOW,
    PRINCIPAL,
    PROMPT_DIGEST,
    RecordingAudit,
    RecordingHandoff,
    ScriptedHarness,
    managed_request,
    managed_runtime,
)

from adapters.inbound.conversation import block_runtime
from adapters.inbound.conversation.block_runtime import (
    ManagedConversationControls,
)
from adapters.outbound.telemetry.reporting import ConversationTerminalLog
from adapters.outbound.telemetry.turn_timing import TurnMilestone, turn_timer
from domain.conversation.answer_stream import (
    ConversationActivityEventV4,
    ConversationDoneEventV4,
    ConversationRefusedEventV4,
    ConversationStreamEventV4,
    ConversationTextEventV4,
    ConversationToolEventV4,
    PublicActivityStageV4,
    PublicToolCallStatusV4,
    StreamRefusalReasonCodeV4,
    validate_conversation_stream,
)
from domain.conversation.audit import (
    AuditOperationCategoryV2,
    AuditOperationOutcomeV2,
    AuditPublicationOutcomeV2,
    AuditTerminalOutcomeV2,
    AuditToolStepDispositionV3,
    AuditToolStepV3,
    ConversationAuditRecordV6,
    TurnDeliveryDispositionV1,
    TurnDeliveryRecordV1,
)
from domain.conversation.grounded_evaluation import grounded_evaluation_context
from domain.conversation.managed import (
    ManagedHarnessActivityV1,
    ManagedHarnessEventV1,
    ManagedHarnessLifecycleStageV1,
    ManagedHarnessTextDeltaV1,
    ManagedHarnessToolActivityV1,
    ManagedHarnessToolCallStatusV1,
    ManagedHarnessToolStepV1,
)
from domain.conversation.tool_custody import (
    AgentToolCustodyV1,
    HarnessStepDispositionV1,
    HarnessToolStepV1,
    ToolCustodyDispositionV1,
    ValidatedToolResultV1,
    evaluation_grounding_sets,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, ValidationError
from ports.outbound.audit import AgentQualityAuditStorePort, AuditWriteResult
from ports.outbound.evaluation_outbox import (
    EvaluationOutboxWriteDisposition,
)

pytestmark = pytest.mark.unit

_REFUSED_RESULT_REF = "7c1e9a2b-4d3f-4a8e-8b2c-1f0e9d8c7b6a"
_DELTAS = ("Fictional count ", "was **7** ", "last week.")
_ANSWER = "".join(_DELTAS)


class FailingCreateAudit(RecordingAudit):
    def create(self, record: ConversationAuditRecordV6) -> AuditWriteResult:
        raise RuntimeError(f"fictional receipt-store outage for {record.audit_id}")


class FailingDeliveryAudit(RecordingAudit):
    def record_delivery(
        self,
        record: TurnDeliveryRecordV1,
        receipt_created_at: str,  # noqa: ARG002 - the port's signature
    ) -> AuditWriteResult:
        raise RuntimeError(f"fictional delivery-store outage for {record.audit_id}")


def _run(
    harness: ScriptedHarness, audit: RecordingAudit, handoff: RecordingHandoff | None = None
) -> tuple[ConversationStreamEventV4, ...]:
    return tuple(managed_runtime(harness, audit, handoff).run(managed_request()))


def _texts(*deltas: str) -> tuple[ManagedHarnessTextDeltaV1, ...]:
    return tuple(ManagedHarnessTextDeltaV1(text=delta) for delta in deltas)


def _step() -> ManagedHarnessToolStepV1:
    result = custody().results[0]
    return ManagedHarnessToolStepV1(
        step=HarnessToolStepV1(
            ordinal=1,
            tool_name=result.tool_name,
            disposition=HarnessStepDispositionV1.SUCCEEDED,
            step_ref=result.tool_result_ref,
        ),
        result=result,
    )


def _refused_result(ordinal: int = 1) -> ValidatedToolResultV1:
    return ValidatedToolResultV1(
        tool_use_ref=f"tool-use-refused-{ordinal}",
        tool_name="measurement_metric_by_grain",
        ordinal=ordinal,
        disposition=ToolCustodyDispositionV1.REFUSED,
        tool_result_ref=_REFUSED_RESULT_REF,
        grounding_set=shaped_response().metadata.grounding,
        refusal_reason="Fictional window is outside the governed coverage.",
    )


def _refused_step(ordinal: int = 1) -> ManagedHarnessToolStepV1:
    return ManagedHarnessToolStepV1(
        step=HarnessToolStepV1(
            ordinal=ordinal,
            tool_name="measurement_metric_by_grain",
            disposition=HarnessStepDispositionV1.REFUSED,
            step_ref=_REFUSED_RESULT_REF,
        ),
        result=_refused_result(),
    )


def _errored_step(ordinal: int) -> ManagedHarnessToolStepV1:
    return ManagedHarnessToolStepV1(
        step=HarnessToolStepV1(
            ordinal=ordinal,
            tool_name="catalog_describe_metric",
            disposition=HarnessStepDispositionV1.ERRORED,
            step_ref=None,
        )
    )


def _tool(status: ManagedHarnessToolCallStatusV1) -> ManagedHarnessToolActivityV1:
    return ManagedHarnessToolActivityV1(tool_name="catalog_list_metrics", status=status)


def _requested_data() -> ManagedHarnessActivityV1:
    return ManagedHarnessActivityV1(stage=ManagedHarnessLifecycleStageV1.GOVERNED_DATA_REQUESTED)


def _governed_answer() -> tuple[ManagedHarnessEventV1, ...]:
    """One governed call, then the streamed answer."""
    return (
        _requested_data(),
        _tool(ManagedHarnessToolCallStatusV1.REQUESTED),
        _tool(ManagedHarnessToolCallStatusV1.COMPLETED),
        _step(),
        *_texts(*_DELTAS),
    )


def _outage(service_code: str = "ThrottlingException") -> DependencyError:
    return DependencyError(
        ErrorCode("HARNESS", 1),
        "The conversation service is temporarily unavailable.",
        context={"service_code": service_code},
    )


def _capture_terminals(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Record the projection of every terminal line the runtime reports."""
    lines: list[dict[str, str]] = []

    def record(event: object, _logger: object) -> None:
        if isinstance(event, ConversationTerminalLog):
            lines.append(event.projection)

    monkeypatch.setattr(block_runtime, "report_lifecycle", record)
    return lines


def _receipt(audit: RecordingAudit) -> ConversationAuditRecordV6:
    [record] = audit.records
    return record


# --- Event projection -------------------------------------------------------------------------


@pytest.mark.covers("ADR-083")
def test_harness_events_project_to_public_v4_events_in_order() -> None:
    harness = ScriptedHarness(
        (
            _tool(ManagedHarnessToolCallStatusV1.REQUESTED),
            _requested_data(),
            ManagedHarnessActivityV1(stage=ManagedHarnessLifecycleStageV1.GOVERNED_DATA_READY),
            _requested_data(),
            _tool(ManagedHarnessToolCallStatusV1.COMPLETED),
            _step(),
            *_texts(*_DELTAS),
        )
    )
    audit = RecordingAudit()

    events = _run(harness, audit)

    assert events == (
        ConversationActivityEventV4(stage=PublicActivityStageV4.UNDERSTANDING_REQUEST),
        ConversationToolEventV4(
            tool_name="catalog_list_metrics", status=PublicToolCallStatusV4.REQUESTED
        ),
        ConversationActivityEventV4(stage=PublicActivityStageV4.RETRIEVING_GOVERNED_DATA),
        ConversationToolEventV4(
            tool_name="catalog_list_metrics", status=PublicToolCallStatusV4.COMPLETED
        ),
        ConversationTextEventV4(sequence=1, text=_DELTAS[0]),
        ConversationTextEventV4(sequence=2, text=_DELTAS[1]),
        ConversationTextEventV4(sequence=3, text=_DELTAS[2]),
        ConversationDoneEventV4(audit_id=_receipt(audit).audit_id),
    )
    assert validate_conversation_stream(events, closed=True) == _ANSWER
    assert harness.calls[0].question == "How did ROAS change last week?"


@pytest.mark.covers("ADR-083")
def test_understanding_is_announced_before_the_harness_is_invoked() -> None:
    harness = ScriptedHarness(_texts(*_DELTAS))
    stream = managed_runtime(harness, RecordingAudit()).run(managed_request())

    first = next(stream)

    assert first == ConversationActivityEventV4(stage=PublicActivityStageV4.UNDERSTANDING_REQUEST)
    assert harness.calls == []
    stream.close()


@pytest.mark.covers("ADR-083")
def test_each_text_delta_is_released_before_the_harness_produces_its_next_event() -> None:
    harness = ScriptedHarness(_texts(*_DELTAS))
    audit = RecordingAudit()
    stream = managed_runtime(harness, audit).run(managed_request())

    next(stream)
    first = next(stream)

    assert first == ConversationTextEventV4(sequence=1, text=_DELTAS[0])
    assert harness.produced == [f"text:{_DELTAS[0]}"]
    assert audit.records == []

    second = next(stream)

    assert second == ConversationTextEventV4(sequence=2, text=_DELTAS[1])
    assert harness.produced == [f"text:{_DELTAS[0]}", f"text:{_DELTAS[1]}"]
    remaining = tuple(stream)
    assert isinstance(remaining[-1], ConversationDoneEventV4)
    assert harness.produced[-1] == "end"


@pytest.mark.covers("ADR-083")
def test_the_receipt_is_written_after_the_harness_turn_ends_and_before_done() -> None:
    harness = ScriptedHarness(_texts(*_DELTAS))
    audit = RecordingAudit()
    stream = managed_runtime(harness, audit).run(managed_request())

    for _ in range(1 + len(_DELTAS)):
        next(stream)
    assert audit.records == []

    done = next(stream)

    assert harness.produced[-1] == "end"
    assert done == ConversationDoneEventV4(audit_id=_receipt(audit).audit_id)
    with pytest.raises(StopIteration):
        next(stream)


# --- Receipts per terminal class --------------------------------------------------------------


@pytest.mark.covers("ADR-083")
@pytest.mark.covers("ADR-062")
def test_a_complete_turn_writes_one_streamed_receipt() -> None:
    audit = RecordingAudit()

    events = _run(ScriptedHarness(_governed_answer()), audit)

    record = _receipt(audit)
    assert isinstance(events[-1], ConversationDoneEventV4)
    assert events[-1].audit_id == record.audit_id == "ref-01"
    assert record.schema_version == "ConversationAuditRecord.v6"
    assert record.correlation_id == CORRELATION_ID
    assert record.trace_id is None  # no recording span in the unit loop
    assert record.operation_category is AuditOperationCategoryV2.COMPOSITION
    assert record.operation_outcome is AuditOperationOutcomeV2.SUCCEEDED
    assert record.publication_outcome is AuditPublicationOutcomeV2.PUBLISHED
    assert record.terminal_outcome is AuditTerminalOutcomeV2.COMPLETE
    assert record.runtime_version == "managed-harness.v3"
    assert record.response_contract_version == "conversation-stream.v4"
    assert record.page_schema_version == "answer.markdown.v1"
    assert record.streamed is True
    assert record.prompt_catalog_digest == PROMPT_DIGEST
    assert record.guardrail_version == "7"
    assert record.created_at == NOW.isoformat()
    assert record.principal == PRINCIPAL
    assert record.released_character_count == len(_ANSWER)
    assert record.trajectory == (
        AuditToolStepV3(
            ordinal=1,
            tool_name="catalog_list_metrics",
            disposition=AuditToolStepDispositionV3.SUCCEEDED,
            step_ref=custody().results[0].tool_result_ref,
        ),
    )
    assert record.tool_custody_digest == custody().digest
    assert (record.presentation_plan_digest, record.public_block_kinds) == (None, ())
    assert record.public_block_count == 0
    assert (record.failure_error_code, record.failure_service_code) == (None, None)
    assert record.evaluation_admission_ref is None


@pytest.mark.covers("ADR-083")
def test_a_turn_without_tool_calls_records_an_empty_trajectory_and_empty_custody() -> None:
    audit = RecordingAudit()

    _run(ScriptedHarness(_texts("Fictional greeting.")), audit)

    record = _receipt(audit)
    assert record.trajectory == ()
    assert record.tool_custody_digest == AgentToolCustodyV1.from_results(()).digest
    assert record.operation_outcome is AuditOperationOutcomeV2.SUCCEEDED


@pytest.mark.covers("ADR-083")
def test_a_turn_whose_every_governed_call_refused_is_a_refused_operation() -> None:
    audit = RecordingAudit()

    _run(ScriptedHarness((_refused_step(), *_texts("Fictional explanation."))), audit)

    record = _receipt(audit)
    assert record.terminal_outcome is AuditTerminalOutcomeV2.COMPLETE
    assert record.operation_outcome is AuditOperationOutcomeV2.REFUSED
    assert record.trajectory[0].disposition is AuditToolStepDispositionV3.REFUSED
    assert record.trajectory[0].step_ref == _REFUSED_RESULT_REF


@pytest.mark.covers("ADR-083")
def test_an_errored_call_is_kept_in_the_trajectory_without_a_reference_or_custody() -> None:
    audit = RecordingAudit()

    _run(ScriptedHarness((_errored_step(1), *_texts("Fictional answer."))), audit)

    record = _receipt(audit)
    assert record.trajectory == (
        AuditToolStepV3(
            ordinal=1,
            tool_name="catalog_describe_metric",
            disposition=AuditToolStepDispositionV3.ERRORED,
            step_ref=None,
        ),
    )
    assert record.tool_custody_digest == AgentToolCustodyV1.from_results(()).digest


@pytest.mark.covers("ADR-083")
def test_a_turn_that_ends_without_text_is_a_governed_no_answer_refusal() -> None:
    audit = RecordingAudit()

    events = _run(ScriptedHarness((_requested_data(), _step())), audit)

    record = _receipt(audit)
    assert events[-1] == ConversationRefusedEventV4(
        reason_code=StreamRefusalReasonCodeV4.NO_ANSWER,
        reason="I could not produce an answer to that. Please rephrase your question.",
        audit_id=record.audit_id,
    )
    assert validate_conversation_stream(events, closed=True) is None
    assert record.terminal_outcome is AuditTerminalOutcomeV2.GOVERNED_REFUSAL
    assert record.publication_outcome is AuditPublicationOutcomeV2.REFUSED
    assert record.operation_outcome is AuditOperationOutcomeV2.SUCCEEDED
    assert (record.failure_error_code, record.failure_service_code) == (None, None)
    assert record.released_character_count == 0
    assert len(record.trajectory) == 1
    assert record.principal == PRINCIPAL
    assert audit.deliveries == []


@pytest.mark.covers("ADR-083")
@pytest.mark.covers("ADR-062")
def test_a_harness_failure_before_any_text_is_a_dependency_refusal_with_its_codes() -> None:
    audit = RecordingAudit()

    events = _run(ScriptedHarness((), failure=_outage()), audit)

    record = _receipt(audit)
    assert isinstance(events[-1], ConversationRefusedEventV4)
    assert events[-1].reason_code is StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE
    assert events[-1].audit_id == record.audit_id
    assert record.terminal_outcome is AuditTerminalOutcomeV2.DEPENDENCY_FAILURE
    assert record.publication_outcome is AuditPublicationOutcomeV2.NOT_APPLICABLE
    assert record.operation_outcome is AuditOperationOutcomeV2.FAILED
    assert record.failure_error_code == "ADLY-HARNESS-0001"
    assert record.failure_service_code == "ThrottlingException"
    assert record.trajectory == ()
    assert record.released_character_count == 0
    assert audit.deliveries == []


@pytest.mark.covers("ADR-083")
def test_a_mid_turn_harness_failure_keeps_the_trajectory_and_the_text_already_sent() -> None:
    """Text on screen stays; the receipt records what ran and what was sent before the failure."""
    audit = RecordingAudit()
    harness = ScriptedHarness(
        (_step(), *_texts(_DELTAS[0])), failure=RuntimeError("fictional stream reset")
    )

    events = _run(harness, audit)

    record = _receipt(audit)
    assert [event.kind for event in events] == ["activity", "text", "refused"]
    assert isinstance(events[-1], ConversationRefusedEventV4)
    assert events[-1].reason_code is StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE
    assert record.terminal_outcome is AuditTerminalOutcomeV2.DEPENDENCY_FAILURE
    assert record.publication_outcome is AuditPublicationOutcomeV2.PUBLISHED
    assert record.failure_error_code == "ADLY-MANAGED_CONVERSATION-0001"
    assert record.failure_service_code is None
    assert [step.step_ref for step in record.trajectory] == [custody().results[0].tool_result_ref]
    assert record.tool_custody_digest == custody().digest
    assert record.released_character_count == len(_DELTAS[0])
    [delivery] = audit.deliveries
    # The consumer read every event, but a refused turn is never recorded as delivered.
    assert (delivery.disposition, delivery.emitted_block_count) == (
        TurnDeliveryDispositionV1.TRUNCATED,
        1,
    )


@pytest.mark.covers("ADR-083")
@pytest.mark.parametrize(
    ("events", "publication", "released"),
    [
        ((), AuditPublicationOutcomeV2.NOT_APPLICABLE, 0),
        (_texts(_DELTAS[0]), AuditPublicationOutcomeV2.PUBLISHED, len(_DELTAS[0])),
    ],
    ids=["before-text", "after-text"],
)
def test_a_harness_stream_that_closes_without_ending_the_turn_is_a_dependency_failure(
    events: tuple[ManagedHarnessEventV1, ...],
    publication: AuditPublicationOutcomeV2,
    released: int,
) -> None:
    audit = RecordingAudit()

    public = _run(ScriptedHarness(events, ends_turn=False), audit)

    record = _receipt(audit)
    assert isinstance(public[-1], ConversationRefusedEventV4)
    assert public[-1].reason_code is StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE
    assert record.terminal_outcome is AuditTerminalOutcomeV2.DEPENDENCY_FAILURE
    assert record.failure_error_code == "ADLY-MANAGED_CONVERSATION-0001"
    assert record.publication_outcome is publication
    assert record.released_character_count == released


@pytest.mark.covers("ADR-083")
def test_a_delta_the_public_contract_refuses_is_never_counted_as_sent() -> None:
    """An oversized Harness delta fails the turn; the receipt records nothing released."""
    audit = RecordingAudit()
    harness = ScriptedHarness(_texts("x" * (8 * 1024 + 1), "Fictional tail."))

    events = _run(harness, audit)

    record = _receipt(audit)
    assert [event.kind for event in events] == ["activity", "refused"]
    assert record.terminal_outcome is AuditTerminalOutcomeV2.DEPENDENCY_FAILURE
    assert record.publication_outcome is AuditPublicationOutcomeV2.NOT_APPLICABLE
    assert record.released_character_count == 0
    assert harness.interrupted is True
    assert audit.deliveries == []


# --- Evaluation handoff -----------------------------------------------------------------------


@pytest.mark.covers("ADR-083")
def test_a_complete_turn_hands_the_exact_released_answer_and_grounded_context_to_evaluation() -> (
    None
):
    audit = RecordingAudit()
    handoff = RecordingHandoff()

    events = _run(ScriptedHarness(_governed_answer()), audit, handoff)

    assert audit.records == []  # the receipt travels in the transactional handoff instead
    [request] = handoff.requests
    record = request.audit
    envelope = request.outbox.envelope.envelope
    assert events[-1] == ConversationDoneEventV4(audit_id=record.audit_id)
    assert envelope.released_answer == _ANSWER == validate_conversation_stream(events, closed=True)
    assert envelope.question == "How did ROAS change last week?"
    assert (envelope.correlation_id, envelope.audit_id) == (CORRELATION_ID, record.audit_id)
    assert envelope.grounded_context == grounded_evaluation_context(
        evaluation_grounding_sets(custody())
    )
    context = json.loads(envelope.grounded_context)
    grounding = shaped_response().metadata.grounding
    assert grounding is not None
    assert context["schema_version"] == "GroundedEvaluationContext.v2"
    assert context["evidence"][0]["grounding_id"] == grounding.grounding_id
    admission_ref = record.evaluation_admission_ref
    assert admission_ref is not None
    assert admission_ref != record.audit_id
    assert envelope.evaluation_admission_ref == admission_ref
    assert request.admission.audit_id == record.audit_id
    assert request.admission.admission_ref == admission_ref
    # The dispatcher and the writer never read receipts: each computes its record's expiry from
    # `admitted_at`, so the route must admit the row at exactly the receipt's creation time.
    assert request.outbox.envelope.admitted_at == record.created_at
    assert request.admission.selected_at == record.created_at
    assert request.outbox.state.state.value == "pending"
    assert request.outbox.state.attempt_count == 0
    assert record.terminal_outcome is AuditTerminalOutcomeV2.COMPLETE


@pytest.mark.covers("ADR-083")
@pytest.mark.parametrize(
    ("events", "failure", "terminal"),
    [
        ((_step(),), None, AuditTerminalOutcomeV2.GOVERNED_REFUSAL),
        (_texts(_DELTAS[0]), _outage(), AuditTerminalOutcomeV2.DEPENDENCY_FAILURE),
    ],
    ids=["no-answer", "dependency-failure"],
)
def test_only_a_complete_turn_is_admitted_to_evaluation(
    events: tuple[ManagedHarnessEventV1, ...],
    failure: Exception | None,
    terminal: AuditTerminalOutcomeV2,
) -> None:
    audit = RecordingAudit()
    handoff = RecordingHandoff()

    _run(ScriptedHarness(events, failure=failure), audit, handoff)

    assert handoff.requests == []
    record = _receipt(audit)
    assert record.terminal_outcome is terminal
    assert record.evaluation_admission_ref is None


@pytest.mark.covers("ADR-083")
def test_a_disconnected_turn_is_never_admitted_to_evaluation() -> None:
    audit = RecordingAudit()
    handoff = RecordingHandoff()
    stream = managed_runtime(ScriptedHarness(_texts(*_DELTAS)), audit, handoff).run(
        managed_request()
    )
    next(stream)
    next(stream)
    stream.close()

    assert handoff.requests == []
    assert _receipt(audit).terminal_outcome is AuditTerminalOutcomeV2.CLIENT_DISCONNECTED
    assert _receipt(audit).evaluation_admission_ref is None


@pytest.mark.covers("ADR-083")
def test_without_an_evaluation_handoff_a_complete_turn_writes_only_its_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminals = _capture_terminals(monkeypatch)
    audit = RecordingAudit()

    _run(ScriptedHarness(_texts(*_DELTAS)), audit)

    assert _receipt(audit).evaluation_admission_ref is None
    [line] = terminals
    assert line["evaluation_state"] == "not_selected"


@pytest.mark.parametrize(
    ("handoff", "counted"),
    [
        (RecordingHandoff(), 1),
        (RecordingHandoff(EvaluationOutboxWriteDisposition.EXACT_REPLAY), 0),
        (None, 0),
    ],
)
def test_runtime_counts_each_new_evaluation_admission_once(
    monkeypatch: pytest.MonkeyPatch, handoff: RecordingHandoff | None, counted: int
) -> None:
    """ADR-079 §3: the no-dispatch alarm's denominator counts new admissions only."""
    admissions: list[None] = []
    monkeypatch.setattr(
        block_runtime, "report_evaluation_admission", lambda: admissions.append(None)
    )

    _run(ScriptedHarness(_texts(*_DELTAS)), RecordingAudit(), handoff)

    assert len(admissions) == counted


# --- Client disconnect and delivery -----------------------------------------------------------


@pytest.mark.covers("ADR-083")
@pytest.mark.covers("ADR-063")
def test_a_client_that_leaves_mid_answer_leaves_a_disconnected_receipt_and_truncated_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminals = _capture_terminals(monkeypatch)
    audit = RecordingAudit()
    harness = ScriptedHarness(_texts(*_DELTAS))
    stream = managed_runtime(harness, audit).run(managed_request())

    next(stream)
    assert isinstance(next(stream), ConversationTextEventV4)
    assert isinstance(next(stream), ConversationTextEventV4)
    stream.close()

    record = _receipt(audit)
    assert record.terminal_outcome is AuditTerminalOutcomeV2.CLIENT_DISCONNECTED
    assert record.publication_outcome is AuditPublicationOutcomeV2.PUBLISHED
    # Counted once the consumer returned for more: the second delta's write never completed.
    assert record.released_character_count == len(_DELTAS[0])
    assert (record.failure_error_code, record.failure_service_code) == (None, None)
    assert record.principal == PRINCIPAL
    [delivery] = audit.deliveries
    # Server-observed lower bound: the consumer returned for the first text event only.
    assert (delivery.disposition, delivery.emitted_block_count) == (
        TurnDeliveryDispositionV1.TRUNCATED,
        1,
    )
    assert delivery.audit_id == record.audit_id
    assert audit.delivery_receipt_times == [record.created_at]
    assert harness.interrupted is True
    [line] = terminals
    assert (line["managed_terminal"], line["investigation_next"], line["audit_state"]) == (
        "client_disconnected",
        # The user left: the turn's own receipt is where to look, not an application failure.
        "inspect_terminal_audit",
        "persisted",
    )


@pytest.mark.covers("ADR-063")
def test_a_client_that_leaves_at_the_first_text_event_was_sent_nothing() -> None:
    """The consumer never returned for more, so the first delta never reached it (ADR-063)."""
    audit = RecordingAudit()
    harness = ScriptedHarness(_texts(*_DELTAS))
    stream = managed_runtime(harness, audit).run(managed_request())

    next(stream)
    assert isinstance(next(stream), ConversationTextEventV4)
    stream.close()

    record = _receipt(audit)
    assert record.terminal_outcome is AuditTerminalOutcomeV2.CLIENT_DISCONNECTED
    assert record.publication_outcome is AuditPublicationOutcomeV2.NOT_APPLICABLE
    assert record.released_character_count == 0
    assert audit.deliveries == []
    assert harness.interrupted is True


@pytest.mark.covers("ADR-083")
def test_a_client_that_leaves_before_any_text_records_no_delivery() -> None:
    """Delivery is recorded only for a turn that released text (ADR-063)."""
    audit = RecordingAudit()
    harness = ScriptedHarness((_tool(ManagedHarnessToolCallStatusV1.REQUESTED), _step()))
    stream = managed_runtime(harness, audit).run(managed_request())

    next(stream)
    assert isinstance(next(stream), ConversationToolEventV4)
    stream.close()

    record = _receipt(audit)
    assert record.terminal_outcome is AuditTerminalOutcomeV2.CLIENT_DISCONNECTED
    assert record.publication_outcome is AuditPublicationOutcomeV2.NOT_APPLICABLE
    assert record.operation_outcome is AuditOperationOutcomeV2.SUCCEEDED
    assert record.trajectory == ()  # the step event was never pulled
    assert audit.deliveries == []
    assert harness.interrupted is True


@pytest.mark.covers("ADR-083")
def test_a_client_that_leaves_before_the_harness_starts_still_leaves_a_receipt() -> None:
    audit = RecordingAudit()
    harness = ScriptedHarness(_texts(*_DELTAS))
    stream = managed_runtime(harness, audit).run(managed_request())

    next(stream)
    stream.close()

    assert harness.calls == []
    record = _receipt(audit)
    assert record.terminal_outcome is AuditTerminalOutcomeV2.CLIENT_DISCONNECTED
    assert record.publication_outcome is AuditPublicationOutcomeV2.NOT_APPLICABLE
    assert audit.deliveries == []


@pytest.mark.covers("ADR-063")
def test_a_fully_consumed_turn_records_a_delivered_disposition() -> None:
    audit = RecordingAudit()

    events = _run(ScriptedHarness(_texts(*_DELTAS)), audit)

    record = _receipt(audit)
    assert isinstance(events[-1], ConversationDoneEventV4)
    [delivery] = audit.deliveries
    assert delivery.disposition is TurnDeliveryDispositionV1.DELIVERED
    assert delivery.emitted_block_count == len(_DELTAS)
    assert delivery.audit_id == record.audit_id
    # ADR-081 §5: the companion expires with its receipt, not from its own later write time.
    assert audit.delivery_receipt_times == [record.created_at]


@pytest.mark.covers("ADR-063")
def test_a_consumer_that_closes_at_done_still_records_delivered() -> None:
    audit = RecordingAudit()
    stream = managed_runtime(ScriptedHarness(_texts(*_DELTAS)), audit).run(managed_request())

    event = next(stream)
    while not isinstance(event, ConversationDoneEventV4):
        event = next(stream)
    stream.close()

    assert _receipt(audit).terminal_outcome is AuditTerminalOutcomeV2.COMPLETE
    [delivery] = audit.deliveries
    assert (delivery.disposition, delivery.emitted_block_count) == (
        TurnDeliveryDispositionV1.DELIVERED,
        len(_DELTAS),
    )


@pytest.mark.covers("ADR-063")
def test_a_failed_delivery_write_never_fails_a_delivered_answer() -> None:
    audit = FailingDeliveryAudit()

    events = _run(ScriptedHarness(_texts(*_DELTAS)), audit)

    assert events[-1] == ConversationDoneEventV4(audit_id=_receipt(audit).audit_id)
    assert audit.deliveries == []


@pytest.mark.covers("ADR-063")
def test_a_failed_delivery_write_never_fails_a_disconnect() -> None:
    audit = FailingDeliveryAudit()
    stream = managed_runtime(ScriptedHarness(_texts(*_DELTAS)), audit).run(managed_request())
    next(stream)
    next(stream)
    next(stream)

    stream.close()

    assert _receipt(audit).terminal_outcome is AuditTerminalOutcomeV2.CLIENT_DISCONNECTED


# --- Receipt write failure --------------------------------------------------------------------


@pytest.mark.covers("ADR-081")
@pytest.mark.covers("ADR-083")
def test_a_complete_turn_whose_receipt_failed_still_ends_done_without_a_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminals = _capture_terminals(monkeypatch)
    audit = FailingCreateAudit()

    events = _run(ScriptedHarness(_texts(*_DELTAS)), audit)

    assert events[-1] == ConversationDoneEventV4(audit_id=None)
    assert validate_conversation_stream(events, closed=True) == _ANSWER
    [line] = terminals
    assert {key: line[key] for key in ("audit_id", "audit_state", "managed_terminal")} == {
        "audit_id": "",
        "audit_state": "not_persisted",
        "managed_terminal": "complete",
    }
    assert line["correlation_id"] == CORRELATION_ID
    assert line["publication_outcome"] == "published"
    assert audit.deliveries == []  # no receipt, so nothing for the companion to key on


@pytest.mark.covers("ADR-081")
def test_a_refusal_whose_receipt_failed_has_no_reference_and_logs_not_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminals = _capture_terminals(monkeypatch)

    events = _run(ScriptedHarness((), failure=_outage()), FailingCreateAudit())

    assert isinstance(events[-1], ConversationRefusedEventV4)
    assert events[-1].reason_code is StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE
    assert events[-1].audit_id is None
    [line] = terminals
    assert (line["audit_id"], line["audit_state"], line["managed_terminal"]) == (
        "",
        "not_persisted",
        "dependency_failure",
    )


@pytest.mark.covers("ADR-083")
def test_a_failed_evaluation_handoff_is_a_failed_receipt_and_still_ends_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminals = _capture_terminals(monkeypatch)
    handoff = RecordingHandoff(failure=RuntimeError("fictional outbox outage"))

    events = _run(ScriptedHarness(_texts(*_DELTAS)), RecordingAudit(), handoff)

    assert events[-1] == ConversationDoneEventV4(audit_id=None)
    [line] = terminals
    assert (line["audit_state"], line["evaluation_state"]) == ("not_persisted", "not_selected")


@pytest.mark.covers("ADR-083")
def test_a_disconnect_whose_receipt_failed_logs_not_persisted_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminals = _capture_terminals(monkeypatch)
    audit = FailingCreateAudit()
    stream = managed_runtime(ScriptedHarness(_texts(*_DELTAS)), audit).run(managed_request())
    next(stream)
    next(stream)

    stream.close()

    [line] = terminals
    assert (line["managed_terminal"], line["audit_state"]) == (
        "client_disconnected",
        "not_persisted",
    )
    assert audit.deliveries == []


# --- Harness lifecycle and principal ----------------------------------------------------------


@pytest.mark.covers("ADR-083")
@pytest.mark.parametrize(
    "harness",
    [
        ScriptedHarness(_texts(*_DELTAS)),
        ScriptedHarness(()),
        ScriptedHarness(_texts(_DELTAS[0]), failure=_outage()),
        ScriptedHarness(_texts(_DELTAS[0]), ends_turn=False),
        ScriptedHarness(_texts("x" * (8 * 1024 + 1))),
    ],
    ids=["complete", "no-answer", "failure", "unended", "refused-delta"],
)
def test_the_harness_stream_is_finalized_on_every_runtime_path(harness: ScriptedHarness) -> None:
    _run(harness, RecordingAudit())

    assert harness.finished is True


@pytest.mark.covers("ADR-083")
def test_a_disconnect_closes_the_suspended_harness_stream_at_once() -> None:
    harness = ScriptedHarness(_texts(*_DELTAS))
    stream = managed_runtime(harness, RecordingAudit()).run(managed_request())
    next(stream)
    next(stream)

    stream.close()

    assert (harness.interrupted, harness.finished) == (True, True)
    assert harness.produced == [f"text:{_DELTAS[0]}"]


@pytest.mark.covers("ADR-062")
@pytest.mark.covers("ADR-077")
@pytest.mark.parametrize(
    "audit", [RecordingAudit(), FailingCreateAudit()], ids=["persisted", "not-persisted"]
)
def test_the_principal_reaches_the_receipt_and_never_the_harness_or_the_terminal_log(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    audit: RecordingAudit,
) -> None:
    terminals = _capture_terminals(monkeypatch)
    harness = ScriptedHarness(_governed_answer())

    _run(harness, audit)

    if audit.records:
        assert _receipt(audit).principal == PRINCIPAL
        assert PRINCIPAL not in repr(_receipt(audit))
    assert [asdict(turn) for turn in harness.calls] == [asdict(managed_request().turn)]
    assert PRINCIPAL not in repr(harness.calls)
    assert PRINCIPAL not in repr(managed_request())
    [line] = terminals
    assert PRINCIPAL not in json.dumps(line)
    assert PRINCIPAL not in capsys.readouterr().out


# --- Pre-Harness terminals (ADR-081 §1) -------------------------------------------------------


def _controls() -> ManagedConversationControls:
    return ManagedConversationControls(
        issue_reference=lambda: "audit-02",
        now=lambda: NOW,
        prompt_digest=PROMPT_DIGEST,
        guardrail_version="7",
    )


@pytest.mark.covers("ADR-081")
@pytest.mark.covers("ADR-083")
@pytest.mark.parametrize(
    ("outcome", "managed_terminal"),
    [
        (AuditTerminalOutcomeV2.INPUT_POLICY_INTERVENTION, "input_policy_intervention"),
        (AuditTerminalOutcomeV2.EXCHANGE_REFUSED, "exchange_refused"),
    ],
)
def test_a_pre_harness_terminal_writes_a_receipt_with_nothing_run_or_published(
    outcome: AuditTerminalOutcomeV2,
    managed_terminal: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminals = _capture_terminals(monkeypatch)
    audit = RecordingAudit()
    record = block_runtime.record_pre_harness_terminal(
        block_runtime.PreHarnessTerminalRequest(
            request=managed_request(),
            terminal_outcome=outcome,
            audit_store=cast(AgentQualityAuditStorePort, audit),
            controls=_controls(),
        )
    )

    assert record is not None
    assert audit.records == [record]
    assert (record.audit_id, record.correlation_id, record.principal) == (
        "audit-02",
        CORRELATION_ID,
        PRINCIPAL,
    )
    assert record.terminal_outcome is outcome
    assert record.operation_category is AuditOperationCategoryV2.NONE
    assert record.operation_outcome is AuditOperationOutcomeV2.NOT_ATTEMPTED
    assert record.publication_outcome is AuditPublicationOutcomeV2.NOT_APPLICABLE
    assert (record.response_contract_version, record.page_schema_version) == (
        "conversation-stream.v4",
        "answer.markdown.v1",
    )
    assert (record.prompt_catalog_digest, record.guardrail_version) == (PROMPT_DIGEST, "7")
    assert record.trajectory == ()
    assert record.tool_custody_digest is None
    assert record.released_character_count == 0
    assert record.evaluation_admission_ref is None
    [line] = terminals
    assert {key: line[key] for key in ("audit_id", "audit_state", "managed_terminal")} == {
        "audit_id": "audit-02",
        "audit_state": "persisted",
        "managed_terminal": managed_terminal,
    }
    assert PRINCIPAL not in json.dumps(line)


@pytest.mark.covers("ADR-081")
def test_a_failed_pre_harness_receipt_returns_none_and_logs_not_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminals = _capture_terminals(monkeypatch)
    record = block_runtime.record_pre_harness_terminal(
        block_runtime.PreHarnessTerminalRequest(
            request=managed_request(),
            terminal_outcome=AuditTerminalOutcomeV2.EXCHANGE_REFUSED,
            audit_store=cast(AgentQualityAuditStorePort, FailingCreateAudit()),
            controls=_controls(),
        )
    )

    assert record is None
    [line] = terminals
    assert (line["audit_id"], line["audit_state"], line["managed_terminal"]) == (
        "",
        "not_persisted",
        "exchange_refused",
    )
    assert PRINCIPAL not in json.dumps(line)


def test_only_the_two_pre_harness_outcomes_can_be_recorded_before_the_harness() -> None:
    with pytest.raises(ValueError, match="pre-Harness"):
        block_runtime.PreHarnessTerminalRequest(
            request=managed_request(),
            terminal_outcome=AuditTerminalOutcomeV2.COMPLETE,
            audit_store=cast(AgentQualityAuditStorePort, RecordingAudit()),
            controls=_controls(),
        )


@pytest.mark.covers("ADR-083")
def test_an_answer_evaluation_cannot_admit_still_leaves_its_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text already shown keeps its receipt when the evaluation envelope refuses the pair.

    The refusal reads as a lost evaluation under its own code, never as an unprovisioned plane
    or a dependency outage.
    """
    terminals = _capture_terminals(monkeypatch)
    errors: list[BaseException] = []

    def capture(error: BaseException, _report: object) -> None:
        errors.append(error)

    monkeypatch.setattr(block_runtime, "report_error", capture)
    audit = RecordingAudit()
    handoff = RecordingHandoff()

    events = _run(ScriptedHarness(_texts("   ")), audit, handoff)

    record = _receipt(audit)
    assert handoff.requests == []
    assert record.terminal_outcome is AuditTerminalOutcomeV2.COMPLETE
    assert record.evaluation_admission_ref is None
    assert events[-1] == ConversationDoneEventV4(audit_id=record.audit_id)
    [line] = terminals
    assert (line["audit_state"], line["evaluation_state"]) == ("persisted", "failed")
    assert line["managed_terminal"] == "complete"
    [error] = errors
    assert isinstance(error, ValidationError)
    assert str(error.code) == "ADLY-MANAGED_CONVERSATION-0002"
    assert not error.alarms


def test_the_receipt_write_is_marked_on_the_turn_timer_with_its_audit_id() -> None:
    audit = RecordingAudit()
    with turn_timer() as timer:
        _run(ScriptedHarness(_texts("Hello.")), audit)

    assert TurnMilestone.RECEIPT_WRITTEN in timer.marks
    assert timer.audit_id == _receipt(audit).audit_id
