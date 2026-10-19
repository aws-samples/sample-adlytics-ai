# SPDX-License-Identifier: MIT-0
"""A relay refusal is recorded by the real runtime as the turn's own terminal (ADR-081, ADR-063).

Every test composes `sse.relay` with the real `ManagedConversationRuntime` over scripted ports,
so the receipt, the frames, and the error reports are the ones production would produce.
"""

from __future__ import annotations

import json
from collections.abc import Generator

import pytest
from managed_conversation_support import (
    RecordingAudit,
    ScriptedHarness,
    managed_request,
    managed_runtime,
)

from adapters.inbound.conversation import block_runtime, sse
from domain.conversation.answer_stream import (
    ConversationDoneEventV4,
    ConversationStreamEventV4,
    ConversationStreamValidator,
)
from domain.conversation.audit import (
    AuditTerminalOutcomeV2,
    ConversationAuditRecordV6,
    TurnDeliveryDispositionV1,
)
from domain.conversation.managed import (
    HarnessTurnV2,
    ManagedHarnessEventV1,
    ManagedHarnessTextDeltaV1,
)
from domain.shared_kernel.errors import AdlyticsError, ErrorClass

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-081", "ADR-063")]

_DELTAS = ("Fictional count ", "was **7** ", "last week.")


def _texts() -> tuple[ManagedHarnessTextDeltaV1, ...]:
    return tuple(ManagedHarnessTextDeltaV1(text=delta) for delta in _DELTAS)


def _error_reports(monkeypatch: pytest.MonkeyPatch) -> list[BaseException]:
    """Capture every report the runtime makes that would be logged at ERROR."""
    reports: list[BaseException] = []

    def capture(error: BaseException, _report: object) -> None:
        if not (
            isinstance(error, AdlyticsError)
            and error.error_class in {ErrorClass.DOMAIN, ErrorClass.VALIDATION}
        ):
            reports.append(error)

    monkeypatch.setattr(block_runtime, "report_error", capture)
    return reports


def _reject(monkeypatch: pytest.MonkeyPatch, *, nth: int | None = None) -> None:
    """Make the relay's validator reject its `nth` event, or the first `done` when `nth` is None."""

    class Rejecting(ConversationStreamValidator):
        __slots__ = ("seen",)

        def __init__(self) -> None:
            super().__init__()
            self.seen = 0

        def accept(self, event: ConversationStreamEventV4) -> None:
            self.seen += 1
            if self.seen == nth or (nth is None and isinstance(event, ConversationDoneEventV4)):
                raise ValueError("fictional illegal event")
            super().accept(event)

    monkeypatch.setattr(sse, "ConversationStreamValidator", Rejecting)


def _relay(
    harness: ScriptedHarness, audit: RecordingAudit, emitted: list[bytes]
) -> sse.RelayResult:
    """Relay one real runtime turn the way the route does, closing the runtime afterwards."""
    events = managed_runtime(harness, audit).run(managed_request())
    try:
        return sse.relay(next(events), events, emitted.append)
    finally:
        events.close()


def _last_frame(emitted: list[bytes]) -> dict[str, object]:
    return json.loads(emitted[-1][len(b"data:") : -2])


def _receipt(audit: RecordingAudit) -> ConversationAuditRecordV6:
    [record] = audit.records
    return record


def test_an_illegal_event_leaves_a_dependency_failure_receipt_and_its_audit_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors = _error_reports(monkeypatch)
    _reject(monkeypatch, nth=3)
    audit = RecordingAudit()
    harness = ScriptedHarness(_texts())
    emitted: list[bytes] = []

    result = _relay(harness, audit, emitted)

    record = _receipt(audit)
    assert record.terminal_outcome is AuditTerminalOutcomeV2.DEPENDENCY_FAILURE
    assert record.failure_error_code == "ADLY-MANAGED_CONVERSATION-0003"
    frame = _last_frame(emitted)
    assert (frame["kind"], frame["audit_id"]) == ("refused", record.audit_id)
    assert (result.ending, result.receipt_terminal) == (sse.RelayEnding.RELAY_REFUSED, True)
    assert harness.interrupted is True
    [error] = errors
    assert isinstance(error, AdlyticsError)
    assert error.context["relay_refusal"] == "illegal_event"
    assert error.context["relay_error_type"] == "ValueError"


def test_a_client_that_has_gone_leaves_a_disconnected_receipt_counting_what_was_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors = _error_reports(monkeypatch)
    audit = RecordingAudit()
    harness = ScriptedHarness(_texts())
    emitted: list[bytes] = []
    events = managed_runtime(harness, audit).run(managed_request())

    def emit(frame: bytes) -> None:
        if emitted:
            raise BrokenPipeError("fictional closed socket")
        emitted.append(frame)

    try:
        result = sse.relay(next(events), events, emit)
    finally:
        events.close()

    assert result.ending is sse.RelayEnding.CLIENT_GONE
    assert len(emitted) == 1
    record = _receipt(audit)
    assert record.terminal_outcome is AuditTerminalOutcomeV2.CLIENT_DISCONNECTED
    # Only the activity frame was written; the first text frame's write failed.
    assert record.released_character_count == 0
    assert errors == []


class _RaisingHarness(ScriptedHarness):
    """A Harness whose `stream` call itself raises, before any generator exists."""

    def stream(self, turn: HarnessTurnV2) -> Generator[ManagedHarnessEventV1]:
        self.calls.append(turn)
        raise RuntimeError("fictional synchronous Harness failure")


def test_a_synchronous_harness_raise_is_the_runtimes_own_receipt_backed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors = _error_reports(monkeypatch)
    audit = RecordingAudit()
    emitted: list[bytes] = []

    result = _relay(_RaisingHarness(()), audit, emitted)

    record = _receipt(audit)
    assert record.terminal_outcome is AuditTerminalOutcomeV2.DEPENDENCY_FAILURE
    assert record.failure_error_code == "ADLY-MANAGED_CONVERSATION-0001"
    frame = _last_frame(emitted)
    assert (frame["kind"], frame["audit_id"]) == ("refused", record.audit_id)
    assert frame["reason_code"] == "dependency_unavailable"
    assert result.ending is sse.RelayEnding.TERMINAL
    assert len(errors) == 1


def test_a_refused_done_keeps_the_complete_receipt_and_records_it_not_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors = _error_reports(monkeypatch)
    _reject(monkeypatch)
    audit = RecordingAudit()
    emitted: list[bytes] = []

    result = _relay(ScriptedHarness(_texts()), audit, emitted)

    record = _receipt(audit)
    assert record.terminal_outcome is AuditTerminalOutcomeV2.COMPLETE
    [delivery] = audit.deliveries
    # Not delivered: every text frame was written, but the user saw a refusal, not `done`.
    assert delivery.disposition is TurnDeliveryDispositionV1.TRUNCATED
    assert delivery.emitted_block_count == len(_DELTAS)
    frame = _last_frame(emitted)
    assert (frame["kind"], frame["audit_id"]) == ("refused", None)
    assert (result.ending, result.receipt_terminal) == (sse.RelayEnding.RELAY_REFUSED, False)
    # The runtime had already recorded the turn, so it reports nothing; the route reports once.
    assert errors == []
