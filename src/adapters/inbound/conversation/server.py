# SPDX-License-Identifier: MIT-0
"""Authenticated HTTP edge for one structured managed Harness conversation turn."""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any

from aws_lambda_powertools import Logger

from adapters.config import settings_without_client_secret, tracing_settings
from adapters.inbound.conversation import sse
from adapters.inbound.conversation.block_runtime import MANAGED_CONVERSATION_UNAVAILABLE
from adapters.inbound.conversation.contract_codec import contract_to_json
from adapters.inbound.conversation.sse import RelayRefusalCause
from adapters.inbound.conversation.turns import (
    AdmittedTurn,
    ConversationTooling,
    HarnessBearer,
    RedeemedTurnContext,
    TurnHttpRequest,
    VerifiedTurn,
    managed_request_from,
    redemption_failed,
    turn_from,
)
from adapters.outbound.cognito.identity_provider import CognitoIdentityProvider
from adapters.outbound.telemetry.bootstrap import (
    configure_tracing,
    flush_traces,
    otlp_traces_endpoint,
)
from adapters.outbound.telemetry.reporting import (
    CORRELATION_HEADER,
    ConversationTurnTimingLog,
    LogSeverity,
    OperationLog,
    Report,
    flush_metrics,
    normalize_correlation_id,
    report_denial,
    report_error,
    report_lifecycle,
    report_operation,
)
from adapters.outbound.telemetry.tracing import (
    CONVERSATION_TURN,
    Recording,
    span,
    trace_boundary,
)
from adapters.outbound.telemetry.turn_timing import (
    TurnMilestone,
    TurnTimer,
    current_turn_timer,
    turn_timer,
)
from domain.conversation.answer_stream import (
    ConversationDoneEventV4,
    ConversationRefusedEventV4,
    ConversationStreamEventV4,
    StreamRefusalReasonCodeV4,
    validate_conversation_stream,
)
from domain.conversation.audit import AuditTerminalOutcomeV2
from domain.conversation.managed import ManagedConversationRequestV1
from domain.conversation.page_context import page_context_block
from domain.conversation.stream import (
    ConversationTerminalV2,
    TerminalReasonCodeV2,
)
from domain.identity.session import IdentityError, SessionAbsentError
from domain.shared_kernel.errors import AdlyticsError, DependencyError, ErrorClass
from ports.outbound.guardrail import AssessmentRequest, Direction

__all__ = ["Handler", "serve"]

SETTINGS = settings_without_client_secret()
PROVIDER = CognitoIdentityProvider(SETTINGS)
TRACING = configure_tracing(tracing_settings())
TOOLING = ConversationTooling.of(SETTINGS)
LOGGER = Logger(service="adlytics-conversation")
OPERATION = "conversation.turn"
PORT = 8080
HEALTH_PATH = "/health"
HEARTBEAT_INTERVAL_SECONDS = 15.0
"""How long the runtime may stay silent before an SSE comment goes out.

CloudFront's origin read timeout on the conversation behaviour is an idle ceiling between bytes,
and the Harness can think for most of a deep dive without producing a public event (ADR-065). The
comment carries no data and the browser parser discards it; it exists so the idle gap never
reaches the edge's limit. Well under that limit rather than close to it, because the limit is
owned by `infra/lib/config` and a heartbeat tuned to its exact value breaks the day it moves.
"""


class _StreamWriter:
    """Serialize every write to one response stream and heartbeat when it goes quiet."""

    def __init__(self, write: Callable[[bytes], None], interval: float) -> None:
        self._write = write
        self._interval = interval
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._last_write = time.monotonic()
        self._thread = threading.Thread(target=self._pulse, name="sse-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def chunk(self, payload: bytes) -> None:
        with self._lock:
            self._write(payload)
            self._last_write = time.monotonic()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self._interval)

    def _pulse(self) -> None:
        while not self._stopped.wait(timeout=1.0):
            if time.monotonic() - self._last_write < self._interval:
                continue
            with self._lock:
                if self._stopped.is_set():
                    return
                try:
                    self._write(sse.HEARTBEAT)
                except OSError:
                    # The client is gone; the relay observes the same failure on its next write.
                    return
                self._last_write = time.monotonic()


_COLD_START = threading.Event()
_COLD_START.set()
"""Set until the first turn this process serves, so that turn's timing line says it was cold."""


def _mark(milestone: TurnMilestone) -> None:
    timer = current_turn_timer()
    if timer is not None:
        timer.mark(milestone)


def _report_timing(timer: TurnTimer, correlation_id: str, *, cold_start: bool) -> None:
    """Emit the turn's timing line once it ends; a turn refused before admission has none."""
    if TurnMilestone.ADMITTED not in timer.marks:
        return
    report_lifecycle(
        ConversationTurnTimingLog(
            correlation_id=correlation_id,
            audit_id=timer.audit_id,
            cold_start=cold_start,
            milestones_ms={
                milestone.value: timer.marks[milestone]
                for milestone in TurnMilestone
                if milestone in timer.marks
            },
            tools=tuple(item.projection for item in timer.tools),
            assistant_messages_ms=tuple(timer.assistant_messages_ms),
            text_deltas=timer.text_deltas,
            metadata_events=timer.metadata_events,
            usage=dict(timer.usage),
        ),
        LOGGER,
    )


@dataclass(frozen=True, slots=True)
class _Declined:
    """An inbound intervention: the reader's reason and the diagnostic labels of what fired."""

    reason: str
    signals: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ReleasedTurn:
    verified: VerifiedTurn


@dataclass(frozen=True, slots=True)
class _RelayRequest:
    first: ConversationStreamEventV4
    remaining: Generator[ConversationStreamEventV4]
    released: _ReleasedTurn


@dataclass(frozen=True, slots=True)
class _PreopenFailure:
    error: AdlyticsError
    correlation_id: str


class Handler(http.server.BaseHTTPRequestHandler):
    """Authorize, complete, invoke, and relay one managed conversation turn."""

    protocol_version = "HTTP/1.1"
    server_version = "adlytics"
    sys_version = ""
    _correlation_id: str = ""
    """This request's server-minted correlation. One instance serves every request on a kept-alive
    connection, so each request resets it before responding: no response may carry another's."""

    def do_GET(self) -> None:
        """Answer only the adapter readiness check."""
        self._correlation_id = ""
        if self.path.split("?")[0].rstrip("/") == HEALTH_PATH.rstrip("/"):
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    _stream_open: bool = False
    """Whether this request's chunked stream awaits its final chunk (see `do_POST`)."""

    def do_POST(self) -> None:
        """Run one turn, flush its telemetry, and only then end the response stream.

        With response streaming, Lambda ends the invocation when the body ends and freezes the
        environment. Anything written after the final chunk runs only when the next invocation
        thaws it, or never: on 2026-09-30 the turn's timing line was logged 45 s late, at the
        next request, and the root span with it. So the final chunk is the last thing written.
        """
        self._correlation_id = ""
        self._stream_open = False
        try:
            self._turn()
        finally:
            try:
                flush_metrics()
                flush_traces()
            finally:
                # The client already has `done`; a failed flush must not leave its body open.
                self._finish_stream()

    @trace_boundary(CONVERSATION_TURN)
    def _turn(self) -> None:
        headers = {name.lower(): value for name, value in self.headers.items()}
        # ADR-081 §2: minted here, the route's first server hop, and returned on every response.
        # A client-supplied value is never read.
        correlation_id = normalize_correlation_id(None)
        self._correlation_id = correlation_id
        cold_start = _COLD_START.is_set()
        _COLD_START.clear()
        with (
            turn_timer() as timer,
            span(
                CONVERSATION_TURN,
                operation=OPERATION,
            ) as recording,
        ):
            try:
                self._answer(recording, headers, correlation_id)
            finally:
                _report_timing(timer, correlation_id, cold_start=cold_start)

    def _admit(
        self, recording: Recording, headers: dict[str, str], correlation_id: str
    ) -> AdmittedTurn | None:
        """Admit one turn, or answer its pre-open refusal and return None."""
        try:
            return turn_from(
                TurnHttpRequest(
                    method="POST",
                    path=self.path,
                    headers=headers,
                    body=self.rfile.read(int(self.headers.get("Content-Length") or 0)),
                    correlation_id=correlation_id,
                ),
                keys=PROVIDER.signing_keys(),
                settings=SETTINGS,
            )
        except SessionAbsentError as exc:
            # ADR-007's 401: there is no session to refresh, so the browser goes to sign-in.
            report_denial(str(exc), self._report(correlation_id))
            recording.record(outcome="denied")
            self._json(401, {"error": "unauthorized"})
        except IdentityError as exc:
            # ADR-007's 403 for every denial of a present cookie (expired, invalid, CSRF,
            # origin): the browser refreshes once and retries rather than signing out.
            self._forbidden(exc, correlation_id, recording)
        except AdlyticsError as exc:
            self._preopen_error(_PreopenFailure(exc, correlation_id), recording)
        return None

    def _answer(  # noqa: PLR0911 - each pre-open refusal must end before streaming
        self, recording: Recording, headers: dict[str, str], correlation_id: str
    ) -> None:
        admitted = self._admit(recording, headers, correlation_id)
        if admitted is None:
            return
        _mark(TurnMilestone.ADMITTED)
        verified = admitted.turn
        managed_request = managed_request_from(verified, self._redeemed(verified))
        try:
            declined = self._declined(managed_request, recording)
        except AdlyticsError as exc:
            self._preopen_error(_PreopenFailure(exc, correlation_id), recording)
            return
        except Exception as exc:
            report_error(exc, self._report(correlation_id))
            recording.record(error_type=type(exc).__name__, outcome="failed")
            self._json(503, {"error": "The conversation Guardrail is temporarily unavailable."})
            return
        _mark(TurnMilestone.INPUT_ASSESSED)
        if declined is not None:
            recorded = TOOLING.record_pre_harness(
                managed_request, AuditTerminalOutcomeV2.INPUT_POLICY_INTERVENTION
            )
            report_operation(
                OperationLog(
                    operation="conversation.turn",
                    component="conversation_api",
                    phase="input_guardrail",
                    outcome="refused",
                    severity=LogSeverity.WARNING,
                    description=declined.reason,
                    details={
                        "correlation_id": correlation_id,
                        "reason_code": TerminalReasonCodeV2.INPUT_INTERVENED.value,
                        "status": 400,
                        # Which checks fired and how confident each was, never the matched text,
                        # so a false positive is diagnosable from the line alone.
                        "guardrail_signals": list(declined.signals),
                    },
                ),
                LOGGER,
            )
            recording.record(outcome="refused")
            self._terminal(
                400,
                ConversationTerminalV2(
                    reason_code=TerminalReasonCodeV2.INPUT_INTERVENED,
                    reason=declined.reason,
                    audit_id=None if recorded is None else recorded.audit_id,
                ),
            )
            return

        bearer = self._harness_bearer(admitted, managed_request, recording)
        if bearer is None:
            return
        _mark(TurnMilestone.HARNESS_TOKEN_EXCHANGED)

        try:
            events = TOOLING.runtime_for(bearer).run(managed_request)
            first = next(events)
        except StopIteration:
            error = RuntimeError("Managed conversation returned no terminal stream.")
            report_error(error, self._report(correlation_id))
            recording.record(error_type=type(error).__name__, outcome="failed")
            self._json(503, {"error": "The conversation service is temporarily unavailable."})
            return
        except AdlyticsError as exc:
            self._preopen_error(_PreopenFailure(exc, correlation_id), recording)
            return
        except Exception as exc:
            report_error(exc, self._report(correlation_id))
            recording.record(error_type=type(exc).__name__, outcome="failed")
            self._json(503, {"error": "The conversation service is temporarily unavailable."})
            return

        try:
            validate_conversation_stream(
                (first,),
                closed=isinstance(first, ConversationDoneEventV4 | ConversationRefusedEventV4),
            )
        except ValueError as exc:
            # O1: the runtime is suspended at its first yield, so the refusal it is told about
            # becomes its `dependency_failure` receipt and its own error report.
            ack = sse.refuse_runtime(events, RelayRefusalCause.ILLEGAL_EVENT, type(exc).__name__)
            events.close()
            if ack is None:
                report_error(exc, self._report(correlation_id))
            recording.record(error_type=type(exc).__name__, outcome="failed")
            self._json(503, {"error": "The conversation service is temporarily unavailable."})
            return

        self._begin_stream()
        _mark(TurnMilestone.RESPONSE_STARTED)
        self._relay(
            _RelayRequest(
                first=first,
                remaining=events,
                released=_ReleasedTurn(verified=verified),
            ),
            recording,
        )

    def _harness_bearer(
        self,
        admitted: AdmittedTurn,
        managed_request: ManagedConversationRequestV1,
        recording: Recording,
    ) -> HarnessBearer | None:
        """Exchange this turn's verified Cognito token for its Harness token (ADR-077 §2).

        One exchange per turn, immediately before the Harness is invoked, and never cached.
        A refused subject is ADR-007's 403 because the cookie was present; any other failure
        is answered before the stream opens and names no token.
        """
        correlation_id = admitted.turn.correlation_id
        try:
            minted = TOOLING.token_exchange.exchange(admitted.subject_token)
        except IdentityError as exc:
            # ADR-081 §1: a refused exchange is a turn's terminal, so it leaves a receipt; the
            # 403 itself stays ADR-007's and names nothing.
            TOOLING.record_pre_harness(managed_request, AuditTerminalOutcomeV2.EXCHANGE_REFUSED)
            self._forbidden(exc, correlation_id, recording)
            return None
        except AdlyticsError as exc:
            self._preopen_error(_PreopenFailure(exc, correlation_id), recording)
            return None
        except Exception as exc:
            report_error(exc, self._report(correlation_id))
            recording.record(error_type=type(exc).__name__, outcome="failed")
            self._json(503, {"error": "The conversation service is temporarily unavailable."})
            return None
        return HarnessBearer(token=minted.access_token)

    def _forbidden(self, exc: IdentityError, correlation_id: str, recording: Recording) -> None:
        report_denial(str(exc), self._report(correlation_id))
        recording.record(outcome="denied")
        self._json(403, {"error": "forbidden"})

    def _relay(self, request: _RelayRequest, recording: Recording) -> None:
        writer = _StreamWriter(self._chunk, HEARTBEAT_INTERVAL_SECONDS)
        writer.start()
        correlation_id = request.released.verified.correlation_id
        try:
            result = sse.relay(request.first, request.remaining, writer.chunk)
        except Exception as exc:
            # A relay bug: the runtime records it as its own refusal when it still can, and that
            # report (carrying `relay_error_type`) is then the turn's single error report.
            ack = sse.refuse_runtime(
                request.remaining, RelayRefusalCause.RELAY_FAILED, type(exc).__name__
            )
            if ack is None:
                report_error(exc, self._report(correlation_id))
            recording.record(error_type=type(exc).__name__, outcome="failed")
        else:
            self._record_relay(result, correlation_id, recording)
        finally:
            # The relay stops at the terminal event; closing the runtime generator here runs its
            # ADR-063 delivery write deterministically instead of leaving it to garbage collection,
            # which Lambda may never reach before it freezes the environment.
            request.remaining.close()
            writer.stop()
        # The final chunk is written by `do_POST` after telemetry is flushed.
        self._stream_open = True
        _mark(TurnMilestone.STREAM_CLOSED)

    def _record_relay(
        self, result: sse.RelayResult, correlation_id: str, recording: Recording
    ) -> None:
        """Map how the relay ended to the turn span's outcome and at most one error report."""
        if result.ending is sse.RelayEnding.CLIENT_GONE:
            # The client left; the runtime's receipt says `client_disconnected` and so does the
            # span. Not an error, so no error metric.
            recording.record(outcome="client_disconnected")
            return
        if result.refusal_reason_code is StreamRefusalReasonCodeV4.DEPENDENCY_UNAVAILABLE:
            dependency = DependencyError(
                MANAGED_CONVERSATION_UNAVAILABLE,
                "The conversation service is temporarily unavailable.",
            )
            recording.note_error(dependency)
            if result.ending is sse.RelayEnding.RELAY_REFUSED and not result.receipt_terminal:
                # The runtime could not record this refusal, so the route reports it once.
                report_error(dependency, self._report(correlation_id))
            recording.record(outcome="failed")
        elif result.refused:
            recording.record(outcome="refused")
        else:
            recording.record(outcome="answered")

    def _finish_stream(self) -> None:
        if self._stream_open:
            self._stream_open = False
            self._end_stream()

    def _redeemed(self, verified: VerifiedTurn) -> RedeemedTurnContext | None:
        """Redeem the page reference; a read that fails runs the turn without context."""
        if verified.page_context_reference is None:
            return None
        try:
            return TOOLING.redeem(verified)
        except Exception as exc:
            # ADR-120 decision 5: a failed redemption never fails the turn. It reads `invalid`,
            # the same non-disclosing reason as a mismatch, and is reported once here.
            report_error(exc, self._report(verified.correlation_id))
            return redemption_failed()

    def _declined(
        self, request: ManagedConversationRequestV1, recording: Recording
    ) -> _Declined | None:
        guardrail = TOOLING.guardrail
        if guardrail is None:
            return None
        turn = request.turn
        # ADR-120 decision 6: the route guardrail runs over the page context block as well as
        # the question, as one inbound text, exactly as the Harness will receive them.
        text = (
            turn.question
            if turn.page_context is None
            else f"{turn.question}\n\n{page_context_block(turn.page_context)}"
        )
        try:
            assessment = guardrail.assess(
                AssessmentRequest(
                    text=text,
                    direction=Direction.INBOUND,
                    correlation_id=turn.correlation_id,
                )
            )
        except AdlyticsError:
            recording.record(question_assessed=False)
            raise
        recording.record(question_assessed=True)
        if assessment.intervened:
            recording.record(outcome="declined")
            return _Declined(reason=assessment.reason, signals=assessment.signals)
        return None

    def _preopen_error(
        self,
        failure: _PreopenFailure,
        recording: Recording,
    ) -> None:
        exc = failure.error
        report_error(exc, self._report(failure.correlation_id))
        recording.note_error(exc)
        status = {
            ErrorClass.DOMAIN: 409,
            ErrorClass.VALIDATION: 400,
            ErrorClass.DEPENDENCY: 503,
            ErrorClass.INVARIANT: 500,
        }[exc.error_class]
        recording.record(outcome="failed" if exc.alarms else "refused")
        self._json(status, {"error": exc.user_visible_description, "code": str(exc.code)})

    def _report(self, correlation_id: str) -> Report:
        return Report(operation=OPERATION, correlation_id=correlation_id, logger=LOGGER)

    def _send_correlation(self) -> None:
        """Return the request's server-minted correlation identifier (ADR-081 §2)."""
        if self._correlation_id:
            self.send_header(CORRELATION_HEADER, self._correlation_id)

    def _begin_stream(self) -> None:
        self.send_response(200)
        self._send_correlation()
        self.send_header("Content-Type", sse.MEDIA_TYPE)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk(self, payload: bytes) -> None:
        """Write one chunked-transfer frame; callers serialize through `_StreamWriter`."""
        self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
        self.wfile.flush()

    def _end_stream(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _terminal(self, status: int, terminal: ConversationTerminalV2) -> None:
        self._encoded_json(status, contract_to_json(terminal).encode())

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._encoded_json(status, json.dumps(payload).encode())

    def _encoded_json(self, status: int, encoded: bytes) -> None:
        self.send_response(status)
        self._send_correlation()
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Suppress the standard library's unstructured access log."""


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Keep readiness responsive while a managed conversation is active."""

    daemon_threads = True


def serve() -> None:
    """Run until the Lambda container stops."""
    report_operation(
        OperationLog(
            operation=OPERATION,
            component="conversation_api",
            phase="start",
            outcome="started",
            severity=LogSeverity.DEBUG,
            details={
                "port": PORT,
                "tracing": TRACING,
                "traces_endpoint": (otlp_traces_endpoint(SETTINGS.aws_region) if TRACING else None),
                "sample_ratio": tracing_settings().sample_ratio,
            },
        ),
        LOGGER,
    )
    Server(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    serve()
