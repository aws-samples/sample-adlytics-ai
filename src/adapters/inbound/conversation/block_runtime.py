# SPDX-License-Identifier: MIT-0
"""The streamed-answer runtime: relay the agent's text, then record the turn (ADR-083).

Assistant text leaves the moment the Harness delivers it. When the Harness turn ends the runtime
writes one immutable receipt, and for a completed answer the evaluation admission and outbox,
carrying the exact concatenated text the user was sent; only then does it emit `done` with the
receipt key as the support reference (ADR-081 §2). Nothing checks the text before it is shown:
evaluation and audit measure what was released.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypedDict

from aws_lambda_powertools import Logger

from adapters.inbound.conversation.sse import RelayRefused
from adapters.outbound.telemetry.reporting import (
    ConversationInvestigationNext,
    ConversationTerminalLog,
    EvaluationState,
    ManagedTerminal,
    PublicationOutcome,
    Report,
    report_error,
    report_evaluation_admission,
    report_lifecycle,
)
from adapters.outbound.telemetry.tracing import current_trace_ids
from adapters.outbound.telemetry.turn_timing import TurnMilestone, current_turn_timer
from domain.conversation.answer_stream import (
    ConversationActivityEventV4,
    ConversationContextEventV4,
    ConversationDoneEventV4,
    ConversationRefusedEventV4,
    ConversationStreamEventV4,
    ConversationTextEventV4,
    ConversationToolEventV4,
    PublicActivityStageV4,
    PublicToolCallStatusV4,
    StreamRefusalReasonCodeV4,
)
from domain.conversation.audit import (
    ANSWER_STREAM_CONTRACT,
    MANAGED_HARNESS_RUNTIME_V3,
    AuditContextReasonV1,
    AuditContextStatusV1,
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
    ManagedConversationRequestV1,
    ManagedHarnessActivityV1,
    ManagedHarnessEventV1,
    ManagedHarnessLifecycleStageV1,
    ManagedHarnessTextDeltaV1,
    ManagedHarnessToolActivityV1,
    ManagedHarnessToolStepV1,
)
from domain.conversation.tool_custody import (
    AgentToolCustodyV1,
    HarnessToolStepV1,
    ToolCustodyDispositionV1,
    ValidatedToolResultV1,
    evaluation_grounding_sets,
    validate_harness_trajectory,
)
from domain.evaluation.envelope import EvaluationEnvelope
from domain.evaluation.linkage import EvaluationAdmissionV1
from domain.evaluation.outbox import (
    EvaluationOutboxDeliveryStateV1,
    EvaluationOutboxEnvelopeV1,
    EvaluationOutboxInitialRecordV1,
    EvaluationOutboxStateV1,
    evaluation_delivery_ref,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, ValidationError
from ports.outbound.audit import AgentQualityAuditStorePort
from ports.outbound.evaluation_outbox import EvaluationHandoffPort, EvaluationHandoffRequest
from ports.outbound.harness import ManagedHarnessPort

MANAGED_CONVERSATION_UNAVAILABLE = ErrorCode("MANAGED_CONVERSATION", 1)
# A complete turn whose released pair the evaluation envelope refused: the answer was delivered
# and its receipt written, but the turn was not admitted to evaluation.
EVALUATION_ADMISSION_REFUSED = ErrorCode("MANAGED_CONVERSATION", 2)
# The route refused to relay the runtime's stream (an illegal event, or a relay failure), so the
# receipt's failure code tells a route refusal apart from a Harness failure.
MANAGED_CONVERSATION_STREAM_REFUSED = ErrorCode("MANAGED_CONVERSATION", 3)
ANSWER_FORMAT = "answer.markdown.v1"
"""The released answer's format, recorded on every receipt this runtime writes."""
_LOGGER = Logger(service="adlytics-conversation")
_OPERATION = "conversation.turn"
_REASONS = {
    StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE: (
        "The assistant is temporarily unavailable. Please try again shortly."
    ),
    StreamRefusalReasonCodeV4.NO_ANSWER: (
        "I could not produce an answer to that. Please rephrase your question."
    ),
}


def _dependency_error(error: Exception) -> DependencyError:
    """Map a failure that ended a turn to the dependency error its receipt and report carry."""
    if isinstance(error, DependencyError):
        return error
    if isinstance(error, RelayRefused):
        return DependencyError(
            MANAGED_CONVERSATION_STREAM_REFUSED,
            "The conversation stream was refused by the route.",
            context={
                "cause": "RelayRefused",
                "relay_refusal": error.cause.value,
                "relay_error_type": error.error_type,
            },
        )
    return DependencyError(
        MANAGED_CONVERSATION_UNAVAILABLE,
        "The conversation service is temporarily unavailable.",
        context={"cause": type(error).__name__},
    )


def _issue_reference() -> str:
    return secrets.token_urlsafe(32)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedConversationDependencies:
    """Outbound boundaries the streamed-answer runtime needs."""

    harness: ManagedHarnessPort
    audit_store: AgentQualityAuditStorePort
    evaluation_handoff: EvaluationHandoffPort | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedConversationControls:
    """Adapter-owned side-effect controls supplied by production or focused tests."""

    issue_reference: Callable[[], str] = _issue_reference
    now: Callable[[], datetime] = _utc_now
    prompt_digest: str | None = None
    guardrail_version: str | None = None


_DEFAULT_CONTROLS = ManagedConversationControls()


@dataclass(frozen=True, slots=True)
class _Terminal:
    """How one turn ended, as its receipt and its terminal event record it."""

    terminal_outcome: AuditTerminalOutcomeV2
    publication_outcome: AuditPublicationOutcomeV2
    reason_code: StreamRefusalReasonCodeV4 | None = None
    failure_error_code: str | None = None
    failure_service_code: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class _EnvelopeRequest:
    """What evaluation admission needs from one completed turn."""

    request: ManagedConversationRequestV1
    answer: str
    custody: AgentToolCustodyV1
    audit_id: str
    admission_ref: str


@dataclass(slots=True)
class _TurnProgress:
    """What one turn has sent and observed so far."""

    parts: list[str] = field(default_factory=lambda: [])
    emitted: int = 0
    steps: list[HarnessToolStepV1] = field(default_factory=lambda: [])
    results: list[ValidatedToolResultV1] = field(default_factory=lambda: [])
    retrieving: bool = False
    ended: bool = False
    recorded: bool = False
    record: ConversationAuditRecordV6 | None = None
    evaluation_refused: bool = False
    delivered: bool = False

    @property
    def text(self) -> str:
        return "".join(self.parts)


class ManagedConversationRuntime:
    """Relay the streamed answer, then write the turn's receipt before its terminal event."""

    def __init__(
        self,
        dependencies: ManagedConversationDependencies,
        controls: ManagedConversationControls = _DEFAULT_CONTROLS,
    ) -> None:
        self._harness = dependencies.harness
        self._audit_store = dependencies.audit_store
        self._evaluation_handoff = dependencies.evaluation_handoff
        self._issue_reference = controls.issue_reference
        self._now = controls.now
        self._prompt_digest = controls.prompt_digest
        self._guardrail_version = controls.guardrail_version

    def run(self, request: ManagedConversationRequestV1) -> Generator[ConversationStreamEventV4]:
        """Stream progress and text as they arrive, then one receipt-backed terminal.

        A client that goes away mid-stream arrives as `GeneratorExit` at a yield; the `finally`
        then records the turn as `client_disconnected` with what it was sent, and every turn
        that sent text records its delivery disposition (ADR-063). A route that refuses the
        stream throws `RelayRefused` in at a yield, and the turn records `dependency_failure`
        with a receipt-backed refused event; an exception escaping the body does the same, so
        only a genuine close reaches the `finally` unrecorded.
        """
        turn = _TurnProgress()
        try:
            yield from self._turn(request, turn)
        except RelayRefused as refusal:
            if turn.recorded:
                # The relay refused the terminal itself. The immutable receipt stands; the
                # delivery record says the answer was not delivered.
                turn.delivered = False
                return
            yield self._refused(request, turn, self._dependency_failure(refusal, request, turn))
        except Exception as error:
            if turn.recorded:
                raise
            yield self._refused(request, turn, self._dependency_failure(error, request, turn))
        finally:
            if not turn.recorded:
                self._record(
                    request,
                    turn,
                    _Terminal(
                        terminal_outcome=AuditTerminalOutcomeV2.CLIENT_DISCONNECTED,
                        publication_outcome=_publication(turn),
                    ),
                )
            if turn.record is not None and (turn.parts or turn.delivered):
                self._record_delivery(turn)

    def _turn(
        self, request: ManagedConversationRequestV1, turn: _TurnProgress
    ) -> Generator[ConversationStreamEventV4]:
        """Relay the Harness turn, then record it and yield its receipt-backed terminal."""
        # Emitted before the first Harness byte, so the browser never faces a silent
        # connection while the model thinks.
        yield ConversationActivityEventV4(stage=PublicActivityStageV4.UNDERSTANDING_REQUEST)
        # ADR-120 decision 5: only a turn that was sent a reference says whether it used it.
        if request.context_status is not None:
            yield ConversationContextEventV4.of(request.context_status)
        failure: _Terminal | None = None
        events = self._harness.stream(request.turn)
        try:
            for event in events:
                public = _project(turn, event)
                if public is not None:
                    yield public
                    if isinstance(public, ConversationTextEventV4):
                        turn.parts.append(public.text)
                        turn.emitted = public.sequence
        except Exception as error:
            failure = self._dependency_failure(error, request, turn)
        finally:
            events.close()
        terminal = failure or _ended(turn)
        record = self._record(request, turn, terminal)
        if terminal.reason_code is None:
            turn.delivered = True
            yield ConversationDoneEventV4(audit_id=None if record is None else record.audit_id)
        else:
            yield ConversationRefusedEventV4(
                reason_code=terminal.reason_code,
                reason=_REASONS[terminal.reason_code],
                audit_id=None if record is None else record.audit_id,
            )

    def _refused(
        self, request: ManagedConversationRequestV1, turn: _TurnProgress, terminal: _Terminal
    ) -> ConversationRefusedEventV4:
        """Record an unrecorded turn's failure and return its receipt-backed refused event."""
        record = self._record(request, turn, terminal)
        return ConversationRefusedEventV4(
            reason_code=StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE,
            reason=_REASONS[StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE],
            audit_id=None if record is None else record.audit_id,
        )

    def _dependency_failure(
        self, error: Exception, request: ManagedConversationRequestV1, turn: _TurnProgress
    ) -> _Terminal:
        dependency = _dependency_error(error)
        report_error(
            dependency,
            Report(
                operation=_OPERATION, correlation_id=request.turn.correlation_id, logger=_LOGGER
            ),
        )
        service_code = dependency.context.get("service_code")
        return _Terminal(
            terminal_outcome=AuditTerminalOutcomeV2.DEPENDENCY_FAILURE,
            publication_outcome=_publication(turn),
            reason_code=StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE,
            failure_error_code=str(dependency.code),
            failure_service_code=service_code if isinstance(service_code, str) else None,
        )

    def _record(
        self, request: ManagedConversationRequestV1, turn: _TurnProgress, terminal: _Terminal
    ) -> ConversationAuditRecordV6 | None:
        """Write the turn's receipt once; a failed write is reported and never raised."""
        turn.recorded = True
        try:
            turn.record = self._persist(request, turn, terminal)
            timer = current_turn_timer()
            if timer is not None:
                timer.mark(TurnMilestone.RECEIPT_WRITTEN)
                timer.audit_id = turn.record.audit_id
        except Exception as error:
            report_error(
                error
                if isinstance(error, DependencyError)
                else DependencyError(
                    MANAGED_CONVERSATION_UNAVAILABLE,
                    "The conversation receipt could not be persisted.",
                    context={"cause": type(error).__name__},
                ),
                Report(
                    operation=_OPERATION,
                    correlation_id=request.turn.correlation_id,
                    logger=_LOGGER,
                ),
            )
            report_lifecycle(
                _unpersisted_terminal_log(
                    request.turn.correlation_id,
                    terminal.terminal_outcome,
                    terminal.publication_outcome,
                ),
                _LOGGER,
            )
            return None
        report_lifecycle(
            _terminal_log(turn.record, evaluation_refused=turn.evaluation_refused), _LOGGER
        )
        return turn.record

    def _persist(
        self, request: ManagedConversationRequestV1, turn: _TurnProgress, terminal: _Terminal
    ) -> ConversationAuditRecordV6:
        steps = tuple(turn.steps)
        validate_harness_trajectory(steps)
        custody = AgentToolCustodyV1.from_results(tuple(turn.results))
        answer = turn.text
        admitted = (
            self._evaluation_handoff is not None
            and terminal.terminal_outcome is AuditTerminalOutcomeV2.COMPLETE
        )
        admission_ref = self._issue_reference() if admitted else None
        audit_id = self._issue_reference()
        envelope = (
            None
            if admission_ref is None
            else self._evaluation_envelope(
                _EnvelopeRequest(
                    request=request,
                    answer=answer,
                    custody=custody,
                    audit_id=audit_id,
                    admission_ref=admission_ref,
                )
            )
        )
        evaluation_admission_ref = None if envelope is None else admission_ref
        turn.evaluation_refused = admission_ref is not None and envelope is None
        created_at = self._now().isoformat()
        record = ConversationAuditRecordV6(
            audit_id=audit_id,
            correlation_id=request.turn.correlation_id,
            trace_id=current_trace_ids().get("trace_id"),
            operation_category=AuditOperationCategoryV2.COMPOSITION,
            operation_outcome=_operation_outcome(terminal, custody),
            publication_outcome=terminal.publication_outcome,
            terminal_outcome=terminal.terminal_outcome,
            runtime_version=MANAGED_HARNESS_RUNTIME_V3,
            prompt_catalog_digest=self._prompt_digest,
            page_schema_version=ANSWER_FORMAT,
            response_contract_version=ANSWER_STREAM_CONTRACT,
            evaluation_admission_ref=evaluation_admission_ref,
            created_at=created_at,
            failure_error_code=terminal.failure_error_code,
            failure_service_code=terminal.failure_service_code,
            trajectory=tuple(
                AuditToolStepV3(
                    ordinal=step.ordinal,
                    tool_name=step.tool_name,
                    disposition=AuditToolStepDispositionV3(step.disposition.value),
                    step_ref=step.step_ref,
                )
                for step in steps
            ),
            tool_custody_digest=custody.digest,
            guardrail_version=self._guardrail_version,
            principal=request.principal,
            released_character_count=len(answer),
            **_audit_context(request),
        )
        if evaluation_admission_ref is None or envelope is None or self._evaluation_handoff is None:
            written = self._audit_store.create(record)
        else:
            delivery_ref = evaluation_delivery_ref(record.audit_id, evaluation_admission_ref)
            written = self._evaluation_handoff.persist(
                EvaluationHandoffRequest(
                    audit=record,
                    admission=EvaluationAdmissionV1(
                        audit_id=record.audit_id,
                        admission_ref=evaluation_admission_ref,
                        selected_at=created_at,
                    ),
                    outbox=EvaluationOutboxInitialRecordV1(
                        envelope=EvaluationOutboxEnvelopeV1(
                            audit_id=record.audit_id,
                            admission_ref=evaluation_admission_ref,
                            delivery_ref=delivery_ref,
                            envelope=envelope,
                            admitted_at=created_at,
                        ),
                        state=EvaluationOutboxStateV1(
                            audit_id=record.audit_id,
                            admission_ref=evaluation_admission_ref,
                            delivery_ref=delivery_ref,
                            state=EvaluationOutboxDeliveryStateV1.PENDING,
                            available_at=created_at,
                            attempt_count=0,
                        ),
                    ),
                )
            )
        if written.disposition.value not in {"applied", "exact_replay"}:
            raise DependencyError(
                MANAGED_CONVERSATION_UNAVAILABLE,
                "The conversation receipt could not be persisted.",
                context={"cause": "invalid_write_disposition"},
            )
        if evaluation_admission_ref is not None and written.disposition.value == "applied":
            report_evaluation_admission()
        return record

    @staticmethod
    def _evaluation_envelope(envelope: _EnvelopeRequest) -> EvaluationEnvelope | None:
        """Build the exact released pair for evaluation, or `None` when it cannot be admitted.

        The text is already on the user's screen, so a pair the evaluation contract refuses
        (an answer of whitespace only, or one past the envelope's size bound) must never cost
        the turn its receipt: the receipt is written without admission and the refusal reported.
        """
        request = envelope.request
        try:
            return EvaluationEnvelope(
                question=request.turn.question,
                released_answer=envelope.answer,
                correlation_id=request.turn.correlation_id,
                audit_id=envelope.audit_id,
                evaluation_admission_ref=envelope.admission_ref,
                grounded_context=grounded_evaluation_context(
                    evaluation_grounding_sets(envelope.custody)
                ),
            )
        except ValueError as error:
            report_error(
                ValidationError(
                    EVALUATION_ADMISSION_REFUSED,
                    "The released answer could not be admitted to evaluation.",
                    context={"cause": type(error).__name__, "detail": str(error)[:200]},
                ),
                Report(
                    operation=_OPERATION,
                    correlation_id=request.turn.correlation_id,
                    logger=_LOGGER,
                ),
            )
            return None

    def _record_delivery(self, turn: _TurnProgress) -> None:
        """Best-effort ADR-063 disposition; a failed write never fails a delivered answer.

        The count is of text events the route wrote; through CloudFront it can exceed what the
        viewer saw.
        """
        record = turn.record
        if record is None:  # pragma: no cover - guarded by the caller
            return
        if turn.delivered:
            disposition = TurnDeliveryDispositionV1.DELIVERED
        elif turn.emitted:
            disposition = TurnDeliveryDispositionV1.TRUNCATED
        else:
            disposition = TurnDeliveryDispositionV1.ABORTED
        try:
            self._audit_store.record_delivery(
                TurnDeliveryRecordV1(
                    audit_id=record.audit_id,
                    disposition=disposition,
                    emitted_block_count=turn.emitted,
                    recorded_at=self._now().isoformat(),
                ),
                # The companion expires with its receipt, never from its own write time.
                record.created_at,
            )
        except Exception as error:
            report_error(
                error,
                Report(operation=_OPERATION, correlation_id=record.correlation_id, logger=_LOGGER),
            )


def _project(turn: _TurnProgress, event: ManagedHarnessEventV1) -> ConversationStreamEventV4 | None:
    """Turn one private Harness projection into its public event, recording what it shows."""
    if isinstance(event, ManagedHarnessTextDeltaV1):
        # Counted by the caller once the consumer returns for more, never before: a delta the
        # contract refuses or the client never took was not sent.
        return ConversationTextEventV4(sequence=len(turn.parts) + 1, text=event.text)
    if isinstance(event, ManagedHarnessToolActivityV1):
        return ConversationToolEventV4(
            tool_name=event.tool_name, status=PublicToolCallStatusV4(event.status.value)
        )
    if isinstance(event, ManagedHarnessActivityV1):
        if (
            event.stage is ManagedHarnessLifecycleStageV1.GOVERNED_DATA_REQUESTED
            and not turn.retrieving
        ):
            turn.retrieving = True
            return ConversationActivityEventV4(stage=PublicActivityStageV4.RETRIEVING_GOVERNED_DATA)
        return None
    if isinstance(event, ManagedHarnessToolStepV1):
        _observe_step(turn, event)
        return None
    turn.ended = True
    return None


def _observe_step(turn: _TurnProgress, event: ManagedHarnessToolStepV1) -> None:
    """Keep the trajectory as each call completes, so a later failure still records it."""
    turn.steps.append(event.step)
    if event.result is not None:
        turn.results.append(event.result)


def _publication(turn: _TurnProgress) -> AuditPublicationOutcomeV2:
    return (
        AuditPublicationOutcomeV2.PUBLISHED
        if turn.parts
        else AuditPublicationOutcomeV2.NOT_APPLICABLE
    )


def _ended(turn: _TurnProgress) -> _Terminal:
    """Classify a turn whose Harness stream closed without an error."""
    if not turn.ended:
        return _Terminal(
            terminal_outcome=AuditTerminalOutcomeV2.DEPENDENCY_FAILURE,
            publication_outcome=_publication(turn),
            reason_code=StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE,
            failure_error_code=str(MANAGED_CONVERSATION_UNAVAILABLE),
        )
    if not turn.parts:
        # The model ended its turn without writing anything for the user: a governed refusal
        # with a natural explanation, recorded so the feedback loop sees it.
        return _Terminal(
            terminal_outcome=AuditTerminalOutcomeV2.GOVERNED_REFUSAL,
            publication_outcome=AuditPublicationOutcomeV2.REFUSED,
            reason_code=StreamRefusalReasonCodeV4.NO_ANSWER,
        )
    return _Terminal(
        terminal_outcome=AuditTerminalOutcomeV2.COMPLETE,
        publication_outcome=AuditPublicationOutcomeV2.PUBLISHED,
    )


def _operation_outcome(terminal: _Terminal, custody: AgentToolCustodyV1) -> AuditOperationOutcomeV2:
    if terminal.terminal_outcome is AuditTerminalOutcomeV2.DEPENDENCY_FAILURE:
        return AuditOperationOutcomeV2.FAILED
    if custody.results and all(
        item.disposition is ToolCustodyDispositionV1.REFUSED for item in custody.results
    ):
        return AuditOperationOutcomeV2.REFUSED
    return AuditOperationOutcomeV2.SUCCEEDED


_PRE_HARNESS_TERMINALS = frozenset(
    {AuditTerminalOutcomeV2.INPUT_POLICY_INTERVENTION, AuditTerminalOutcomeV2.EXCHANGE_REFUSED}
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PreHarnessTerminalRequest:
    """One turn that ended before the Harness ran: its request, outcome, and where to record it.

    ADR-081 §1: an inbound Guardrail intervention or a refused token exchange. The verified
    principal and the server's correlation are already known, so the receipt names both.
    """

    request: ManagedConversationRequestV1
    terminal_outcome: AuditTerminalOutcomeV2
    audit_store: AgentQualityAuditStorePort
    controls: ManagedConversationControls

    def __post_init__(self) -> None:
        if self.terminal_outcome not in _PRE_HARNESS_TERMINALS:
            raise ValueError("only an inbound intervention or a refused exchange ends pre-Harness.")


def record_pre_harness_terminal(
    terminal: PreHarnessTerminalRequest,
) -> ConversationAuditRecordV6 | None:
    """Write the receipt and terminal log line of a turn that ended before the Harness ran.

    Nothing ran and nothing was published, so the receipt has no trajectory, no custody or plan
    digest, and no evaluation admission. A receipt that cannot be built or written is reported
    and logged as not persisted and returns `None`: the refusal the caller answers with never
    depends on the receipt.
    """
    request = terminal.request
    try:
        record = _pre_harness_record(terminal)
        written = terminal.audit_store.create(record)
        if written.disposition.value not in {"applied", "exact_replay"}:
            raise ValueError("invalid_write_disposition")
    except Exception as error:
        report_error(
            DependencyError(
                MANAGED_CONVERSATION_UNAVAILABLE,
                "The conversation receipt could not be persisted.",
                context={"cause": type(error).__name__},
            ),
            Report(
                operation=_OPERATION, correlation_id=request.turn.correlation_id, logger=_LOGGER
            ),
        )
        report_lifecycle(
            _unpersisted_terminal_log(
                request.turn.correlation_id,
                terminal.terminal_outcome,
                AuditPublicationOutcomeV2.NOT_APPLICABLE,
            ),
            _LOGGER,
        )
        return None
    report_lifecycle(_terminal_log(record), _LOGGER)
    return record


def _pre_harness_record(terminal: PreHarnessTerminalRequest) -> ConversationAuditRecordV6:
    request = terminal.request
    controls = terminal.controls
    return ConversationAuditRecordV6(
        audit_id=controls.issue_reference(),
        correlation_id=request.turn.correlation_id,
        trace_id=current_trace_ids().get("trace_id"),
        operation_category=AuditOperationCategoryV2.NONE,
        operation_outcome=AuditOperationOutcomeV2.NOT_ATTEMPTED,
        publication_outcome=AuditPublicationOutcomeV2.NOT_APPLICABLE,
        terminal_outcome=terminal.terminal_outcome,
        runtime_version=MANAGED_HARNESS_RUNTIME_V3,
        prompt_catalog_digest=controls.prompt_digest,
        page_schema_version=ANSWER_FORMAT,
        response_contract_version=ANSWER_STREAM_CONTRACT,
        evaluation_admission_ref=None,
        created_at=controls.now().isoformat(),
        guardrail_version=controls.guardrail_version,
        principal=request.principal,
        **_audit_context(request),
    )


class _AuditContext(TypedDict):
    page_context_reference: str | None
    context_status: AuditContextStatusV1 | None


def _audit_context(request: ManagedConversationRequestV1) -> _AuditContext:
    """Return the receipt's page fields: the sent reference and its status, never labels."""
    status = request.context_status
    reference = request.page_context_reference
    return {
        "page_context_reference": None if reference is None else reference.value,
        "context_status": (
            None
            if status is None
            else AuditContextStatusV1(
                used=status.used,
                reason=None if status.reason is None else AuditContextReasonV1(status.reason.value),
            )
        ),
    }


_MANAGED_TERMINALS = {
    AuditTerminalOutcomeV2.COMPLETE: ManagedTerminal.COMPLETE,
    AuditTerminalOutcomeV2.GOVERNED_REFUSAL: ManagedTerminal.GOVERNED_REFUSAL,
    AuditTerminalOutcomeV2.OUTPUT_POLICY_INTERVENTION: ManagedTerminal.OUTPUT_POLICY_INTERVENTION,
    AuditTerminalOutcomeV2.DEPENDENCY_FAILURE: ManagedTerminal.DEPENDENCY_FAILURE,
    AuditTerminalOutcomeV2.INPUT_POLICY_INTERVENTION: ManagedTerminal.INPUT_POLICY_INTERVENTION,
    AuditTerminalOutcomeV2.EXCHANGE_REFUSED: ManagedTerminal.EXCHANGE_REFUSED,
    AuditTerminalOutcomeV2.CLIENT_DISCONNECTED: ManagedTerminal.CLIENT_DISCONNECTED,
}
_PUBLICATION_OUTCOMES = {
    AuditPublicationOutcomeV2.PUBLISHED: PublicationOutcome.PUBLISHED,
    AuditPublicationOutcomeV2.REFUSED: PublicationOutcome.REFUSED,
    AuditPublicationOutcomeV2.NOT_APPLICABLE: PublicationOutcome.NOT_APPLICABLE,
}
_APPLICATION_FAILURES = frozenset({ManagedTerminal.DEPENDENCY_FAILURE})


def _unpersisted_terminal_log(
    correlation_id: str,
    terminal_outcome: AuditTerminalOutcomeV2,
    publication_outcome: AuditPublicationOutcomeV2,
) -> ConversationTerminalLog:
    """Build the terminal line of a turn whose receipt write failed: the outcome, no audit ID."""
    return ConversationTerminalLog(
        correlation_id=correlation_id,
        managed_terminal=_MANAGED_TERMINALS[terminal_outcome],
        publication_outcome=_PUBLICATION_OUTCOMES[publication_outcome],
        audit_id="",
        evaluation_state=EvaluationState.NOT_SELECTED,
        investigation_next=ConversationInvestigationNext.APPLICATION_FAILURE,
        persisted=False,
    )


def _terminal_log(
    record: ConversationAuditRecordV6, *, evaluation_refused: bool = False
) -> ConversationTerminalLog:
    """Build a persisted turn's terminal line.

    A turn selected for evaluation whose pair the envelope refused reads as `failed`, never as
    `not_selected`, so the terminal line separates a lost evaluation from an unprovisioned one.
    """
    if record.evaluation_admission_ref is not None:
        evaluation_state = EvaluationState.PENDING
    elif evaluation_refused:
        evaluation_state = EvaluationState.FAILED
    else:
        evaluation_state = EvaluationState.NOT_SELECTED
    managed_terminal = _MANAGED_TERMINALS[record.terminal_outcome]
    return ConversationTerminalLog(
        correlation_id=record.correlation_id,
        managed_terminal=managed_terminal,
        publication_outcome=_PUBLICATION_OUTCOMES[record.publication_outcome],
        audit_id=record.audit_id,
        evaluation_state=evaluation_state,
        investigation_next=(
            ConversationInvestigationNext.APPLICATION_FAILURE
            if managed_terminal in _APPLICATION_FAILURES
            else ConversationInvestigationNext.TERMINAL_AUDIT
        ),
    )
