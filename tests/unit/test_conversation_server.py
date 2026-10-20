# SPDX-License-Identifier: MIT-0
"""Conversation HTTP boundary: authorization outcomes, admission, and stream lifecycle."""

from __future__ import annotations

import importlib
import io
import json
import sys
import uuid
from collections.abc import Generator, Iterable, Iterator
from dataclasses import dataclass, field, replace
from email.message import Message
from types import SimpleNamespace
from typing import Any, Never, cast

import pytest
from managed_conversation_support import (
    RecordingAudit,
    ScriptedHarness,
    managed_request,
    managed_runtime,
)

from adapters.config import Settings, settings_without_client_secret, tracing_settings
from adapters.inbound.conversation import block_runtime, turns
from adapters.outbound.telemetry.reporting import ConversationTurnTimingLog
from domain.conversation.answer_stream import (
    ConversationActivityEventV4,
    ConversationDoneEventV4,
    ConversationRefusedEventV4,
    ConversationTextEventV4,
    PublicActivityStageV4,
    StreamRefusalReasonCodeV4,
)
from domain.conversation.audit import AuditTerminalOutcomeV2
from domain.conversation.managed import ManagedConversationRequestV1, ManagedHarnessTextDeltaV1
from domain.conversation.page_context import (
    ConversationContextStatusV1,
    page_context_block,
    redeemed_page_context,
)
from domain.conversation.stream import ConversationTerminalV2
from domain.identity.session import IdentityError, SessionAbsentError
from domain.semantics.page_context import PageContextDisplayLabels, PageContextReference
from domain.shared_kernel.errors import (
    AdlyticsError,
    DependencyError,
    ErrorClass,
    ErrorCode,
    ValidationError,
)
from ports.outbound.guardrail import Assessment, AssessmentRequest
from ports.outbound.harness_token import HarnessToken

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-007", "ADR-041", "ADR-081", "ADR-063")]

_ENV = {
    "AWS_DEFAULT_REGION": "eu-west-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "ADL_ENVIRONMENT": "dev",
    "ADL_AWS_REGION": "eu-west-1",
    "ADL_COGNITO_USER_POOL_ID": "eu-west-1_example",
    "ADL_COGNITO_CLIENT_ID": "1example23456789",
    "ADL_COGNITO_LOGIN_DOMAIN": "auth.test.invalid",
    "ADL_APP_BASE_URL": "https://app.test.invalid",
    "ADL_API_BASE_URL": "https://api.test.invalid",
    "ADL_COOKIE_SCOPE": "host_only",
    "ADL_REFRESH_COOKIE_MAX_AGE_SECONDS": "2592000",
    "ADL_ATHENA_WORKGROUP": "unused-by-server-test",
    "ADL_SEMANTIC_ATTEMPTS_TABLE": "unused-by-server-test",
    "ADL_SEMANTIC_LEASES_TABLE": "unused-by-server-test",
    "ADL_SEMANTIC_PUBLICATIONS_TABLE": "unused-by-server-test",
    "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "0",
    "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "8",
    "ADL_HARNESS_ARN": "arn:aws:bedrock-agentcore:eu-west-1:111122223333:harness/test",
    "ADL_AGENT_QUALITY_AUDIT_TABLE": "unused-by-server-test",
    "ADL_GUARDRAILS_ENABLED": "false",
}
_CORRELATION_ID = "123e4567-e89b-42d3-a456-426614174000"
_UNAVAILABLE_RESPONSE = {"error": "The conversation service is temporarily unavailable."}
_DEPENDENCY_REFUSAL_SUFFIX = (
    b'"reason_code":"dependency_unavailable","schema_version":"ConversationStream.v4"}\n\n'
)


def _answer_text() -> ConversationTextEventV4:
    return ConversationTextEventV4(sequence=1, text="Fictional governed **answer**.")


def _answered() -> tuple[ConversationTextEventV4, ConversationDoneEventV4]:
    """Return the smallest legal V4 stream: one text delta, then done."""
    return (_answer_text(), ConversationDoneEventV4(audit_id="audit-01"))


@dataclass
class _RecordingRuntime:
    events: Iterable[object] = ()
    failure: Exception | None = None
    requests: list[ManagedConversationRequestV1] = field(
        default_factory=list[ManagedConversationRequestV1]
    )

    def run(self, request: ManagedConversationRequestV1) -> Iterator[object]:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        yield from self.events


@dataclass
class _Guardrail:
    assessment: Assessment = field(default_factory=lambda: Assessment(intervened=False, reason=""))
    failure: Exception | None = None
    requests: list[AssessmentRequest] = field(default_factory=list[AssessmentRequest])

    def assess(self, request: AssessmentRequest) -> Assessment:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return self.assessment


@dataclass
class _Exchange:
    """HarnessTokenPort fake: mints a distinct fictional token per call, or fails as told."""

    failure: Exception | None = None
    subjects: list[str] = field(default_factory=list[str])

    def exchange(self, subject_token: str) -> HarnessToken:
        self.subjects.append(subject_token)
        if self.failure is not None:
            raise self.failure
        return HarnessToken(access_token=f"minted-{len(self.subjects):04d}", expires_in=240)


@dataclass
class _Tooling:
    runtime: _RecordingRuntime
    guardrail: _Guardrail | None = None
    token_exchange: _Exchange = field(default_factory=_Exchange)
    bearers: list[turns.HarnessBearer] = field(default_factory=list[turns.HarnessBearer])
    pre_harness: list[str] = field(default_factory=list[str])
    receipt_written: bool = True

    def record_pre_harness(self, request: object, outcome: Any) -> SimpleNamespace | None:
        """Record which pre-Harness terminal was written; its receipt key is fictional."""
        del request
        self.pre_harness.append(outcome.value)
        return SimpleNamespace(audit_id="audit-pre-harness-01") if self.receipt_written else None

    def runtime_for(self, bearer: turns.HarnessBearer) -> _RecordingRuntime:
        self.bearers.append(bearer)
        return self.runtime


@dataclass
class _Provider:
    """Local identity-provider port fake; authorization itself is separately unit-owned."""

    keys: object = field(default_factory=object)

    def signing_keys(self) -> object:
        return self.keys


@dataclass
class _Recording:
    values: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    errors: list[Exception] = field(default_factory=list[Exception])

    def record(self, **values: object) -> None:
        self.values.append(dict(values))

    def note_error(self, error: Exception) -> None:
        self.errors.append(error)


class _LineRecorder:
    """A Powertools `Logger` stand-in keeping each line's structured fields, at any level."""

    def __init__(self) -> None:
        self.lines: list[dict[str, object]] = []

    def _record(self, _message: object, *_args: object, **kwargs: object) -> None:
        self.lines.append(dict(cast("dict[str, object]", kwargs.get("extra") or {})))

    debug = info = warning = error = exception = _record

    def append_keys(self, **_keys: object) -> None:
        """Accept persistent keys; the test reads per-line fields only."""


@dataclass
class _Response:
    json: list[tuple[int, dict[str, object]]] = field(
        default_factory=list[tuple[int, dict[str, object]]]
    )
    terminals: list[tuple[int, ConversationTerminalV2]] = field(
        default_factory=list[tuple[int, ConversationTerminalV2]]
    )
    chunks: list[bytes] = field(default_factory=list[bytes])
    began: bool = False
    ended: bool = False


@pytest.fixture
def server_module(monkeypatch: pytest.MonkeyPatch) -> Generator[Any]:
    """Import production composition under fictional startup settings and port fakes."""
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    settings_without_client_secret.cache_clear()
    tracing_settings.cache_clear()
    sys.modules.pop("adapters.inbound.conversation.server", None)
    bootstrap = _Tooling(_RecordingRuntime())

    def compose_tooling(_cls: type[turns.ConversationTooling], _settings: Settings) -> _Tooling:
        return bootstrap

    monkeypatch.setattr(turns.ConversationTooling, "of", classmethod(compose_tooling))
    module = cast("Any", importlib.import_module("adapters.inbound.conversation.server"))
    monkeypatch.setattr(module, "PROVIDER", _Provider())
    yield module
    sys.modules.pop("adapters.inbound.conversation.server", None)
    settings_without_client_secret.cache_clear()
    tracing_settings.cache_clear()


def _handler(module: Any, *, body: bytes = b"{}") -> tuple[Any, _Response]:
    response = _Response()
    handler = module.Handler.__new__(module.Handler)
    headers = Message()
    headers["Content-Length"] = str(len(body))
    handler.path = "/api/conversation"
    handler.headers = headers
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()

    def write_json(status: int, payload: dict[str, Any]) -> None:
        response.json.append((status, dict(payload)))

    def write_terminal(status: int, terminal: ConversationTerminalV2) -> None:
        response.terminals.append((status, terminal))

    def begin_stream() -> None:
        response.began = True

    def end_stream() -> None:
        response.ended = True

    handler._json = write_json
    handler._terminal = write_terminal
    handler._begin_stream = begin_stream
    handler._chunk = response.chunks.append
    handler._end_stream = end_stream
    return handler, response


# A-TEST-LITERALS-001: a fictional token; see docs/security-dispositions.md
_SUBJECT_TOKEN = "fictional-verified-cognito-access-token"  # nosec B105


def _admitted_turn(module: Any) -> turns.AdmittedTurn:
    return module.AdmittedTurn(
        turn=module.VerifiedTurn(
            question="What can this workspace report?",
            verified_owner_id="verified-owner",
            conversation_id="conversation-verified-owner-browser",
            custody_session_id="browser-session",
            correlation_id=_CORRELATION_ID,
        ),
        subject_token=_SUBJECT_TOKEN,
    )


def _answer(handler: Any, recording: _Recording) -> None:
    handler._answer(recording, {"x-correlation-id": _CORRELATION_ID}, _CORRELATION_ID)
    handler._finish_stream()


@dataclass(frozen=True)
class _Admission:
    module: Any
    runtime: _RecordingRuntime
    guardrail: _Guardrail | None = None
    exchange: _Exchange = field(default_factory=_Exchange)


def _admit(monkeypatch: pytest.MonkeyPatch, admission: _Admission) -> _Tooling:
    tooling = _Tooling(admission.runtime, admission.guardrail, admission.exchange)
    monkeypatch.setattr(admission.module, "TOOLING", tooling)

    def accepted_turn(*_args: object, **_kwargs: object) -> turns.AdmittedTurn:
        return _admitted_turn(admission.module)

    monkeypatch.setattr(admission.module, "turn_from", accepted_turn)
    return tooling


def test_health_and_unknown_get_are_plain_json(server_module: Any) -> None:
    health, health_response = _handler(server_module)
    health.path = "/health?probe=1"
    health.do_GET()
    unknown, unknown_response = _handler(server_module)
    unknown.path = "/other"
    unknown.do_GET()

    assert health_response.json == [(200, {"ok": True})]
    assert unknown_response.json == [(404, {"error": "not found"})]


@pytest.mark.covers("ADR-077")
def test_a_request_without_a_session_cookie_is_401_and_never_exchanges(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    """ADR-007: 401 means there is no session to refresh, so the browser signs in."""
    tooling = _admit(monkeypatch, _Admission(server_module, _RecordingRuntime()))
    handler, response = _handler(server_module)
    recording = _Recording()

    def absent(*_args: object, **_kwargs: object) -> Never:
        raise SessionAbsentError("fictional absent cookie")

    monkeypatch.setattr(server_module, "turn_from", absent)
    _answer(handler, recording)

    assert response.json == [(401, {"error": "unauthorized"})]
    assert response.began is False
    assert recording.values == [{"outcome": "denied"}]
    assert tooling.token_exchange.subjects == []


@pytest.mark.covers("ADR-077")
def test_every_other_identity_denial_is_403_so_the_browser_refreshes_once(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    """An expired, invalid, CSRF, or origin failure has a cookie to refresh; 401 would sign out."""
    runtime = _RecordingRuntime()
    tooling = _admit(monkeypatch, _Admission(server_module, runtime))
    handler, response = _handler(server_module)
    recording = _Recording()

    def deny_turn(*_args: object, **_kwargs: object) -> Never:
        raise IdentityError("fictional expired token")

    monkeypatch.setattr(server_module, "turn_from", deny_turn)
    _answer(handler, recording)

    assert response.json == [(403, {"error": "forbidden"})]
    assert response.began is False
    assert recording.values == [{"outcome": "denied"}]
    assert runtime.requests == []
    assert tooling.token_exchange.subjects == []


@pytest.mark.covers("ADR-077")
def test_a_refused_exchange_is_403_and_never_invokes_the_harness(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    runtime = _RecordingRuntime(events=_answered())
    exchange = _Exchange(failure=IdentityError("fictional invalid_grant"))
    tooling = _admit(monkeypatch, _Admission(server_module, runtime, exchange=exchange))
    handler, response = _handler(server_module)
    recording = _Recording()

    _answer(handler, recording)

    assert response.json == [(403, {"error": "forbidden"})]
    assert response.began is False
    assert recording.values == [{"outcome": "denied"}]
    assert exchange.subjects == [_SUBJECT_TOKEN]
    assert tooling.bearers == []
    assert runtime.requests == []
    # ADR-081 §1: the refused exchange leaves a receipt; the 403 body still names nothing.
    assert tooling.pre_harness == ["exchange_refused"]


@pytest.mark.covers("ADR-077")
def test_an_unavailable_exchange_is_a_preopen_503_that_never_invokes_the_harness(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    runtime = _RecordingRuntime(events=_answered())
    failure = DependencyError(ErrorCode("TOKENSVC", 3), "The conversation service is down.")
    tooling = _admit(
        monkeypatch, _Admission(server_module, runtime, exchange=_Exchange(failure=failure))
    )
    handler, response = _handler(server_module)

    _answer(handler, _Recording())

    assert response.json == [
        (503, {"error": failure.user_visible_description, "code": "ADLY-TOKENSVC-0003"})
    ]
    assert response.began is False
    assert tooling.bearers == []
    assert runtime.requests == []


@dataclass(frozen=True)
class _PreopenCase:
    error: ValidationError
    status: int


@pytest.mark.parametrize(
    "case",
    [
        _PreopenCase(ValidationError(ErrorCode("TEST", 1), "invalid request"), 400),
        _PreopenCase(ValidationError(ErrorCode("TEST", 2), "another invalid request"), 400),
    ],
)
def test_classified_preopen_error_maps_before_streaming(
    monkeypatch: pytest.MonkeyPatch,
    server_module: Any,
    case: _PreopenCase,
) -> None:
    handler, response = _handler(server_module)

    def reject_turn(*_args: object, **_kwargs: object) -> Never:
        raise case.error

    monkeypatch.setattr(server_module, "turn_from", reject_turn)
    _answer(handler, _Recording())

    assert response.json == [
        (
            case.status,
            {"error": case.error.user_visible_description, "code": str(case.error.code)},
        )
    ]
    assert response.began is False


def test_input_guardrail_refusal_does_not_invoke_runtime(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    runtime = _RecordingRuntime()
    guardrail = _Guardrail(Assessment(intervened=True, reason="Fictional input policy."))
    tooling = _admit(monkeypatch, _Admission(server_module, runtime, guardrail))
    handler, response = _handler(server_module)

    _answer(handler, _Recording())

    assert runtime.requests == []
    # The exchange is the last pre-open step: a declined question mints nothing.
    assert tooling.token_exchange.subjects == []
    assert response.terminals
    status, terminal = response.terminals[0]
    assert status == 400
    assert terminal.reason_code.value == "input_intervened"
    assert guardrail.requests[0].direction.value == "inbound"
    # ADR-081: the intervention leaves a receipt, and its key is the support reference.
    assert tooling.pre_harness == ["input_policy_intervention"]
    assert terminal.audit_id == "audit-pre-harness-01"


def test_input_guardrail_refusal_logs_which_checks_fired_but_shows_only_the_reason(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    signals = ("content:PROMPT_ATTACK:LOW", "pii:EMAIL")
    guardrail = _Guardrail(
        Assessment(intervened=True, reason="Fictional input policy.", signals=signals)
    )
    _admit(monkeypatch, _Admission(server_module, _RecordingRuntime(), guardrail))
    logger = _LineRecorder()
    # The real `report_operation` and its sanitizer run; only the sink is replaced.
    monkeypatch.setattr(server_module, "LOGGER", logger)
    handler, response = _handler(server_module)

    _answer(handler, _Recording())

    (refusal,) = [extra for extra in logger.lines if extra.get("phase") == "input_guardrail"]
    assert refusal["guardrail_signals"] == list(signals)
    assert refusal["description"] == "Fictional input policy."
    # The admitted turn's question (see `_admit`) appears in no line.
    assert "What can this workspace report?" not in json.dumps(logger.lines, default=str)
    _status, terminal = response.terminals[0]
    # The caller sees the safe reason; which filter fired stays in the operator log.
    assert terminal.reason == "Fictional input policy."
    assert "PROMPT_ATTACK" not in terminal.reason


def test_an_unrecorded_intervention_still_refuses_without_a_reference(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    guardrail = _Guardrail(Assessment(intervened=True, reason="Fictional input policy."))
    tooling = _admit(monkeypatch, _Admission(server_module, _RecordingRuntime(), guardrail))
    tooling.receipt_written = False
    handler, response = _handler(server_module)
    _answer(handler, _Recording())
    status, terminal = response.terminals[0]
    assert (status, terminal.reason_code.value, terminal.audit_id) == (
        400,
        "input_intervened",
        None,
    )


def test_empty_runtime_stream_is_a_preopen_dependency_response(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    _admit(monkeypatch, _Admission(server_module, _RecordingRuntime()))
    handler, response = _handler(server_module)

    _answer(handler, _Recording())

    assert response.json == [(503, _UNAVAILABLE_RESPONSE)]
    assert response.began is False


def test_invalid_first_runtime_event_is_a_preopen_dependency_response(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    runtime = _RecordingRuntime(events=(ConversationDoneEventV4(),))
    _admit(monkeypatch, _Admission(server_module, runtime))
    handler, response = _handler(server_module)

    _answer(handler, _Recording())

    assert response.json == [(503, _UNAVAILABLE_RESPONSE)]
    assert response.began is False


def test_valid_runtime_stream_is_chunked_and_records_answered(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    runtime = _RecordingRuntime(
        events=(
            ConversationActivityEventV4(stage=PublicActivityStageV4.RETRIEVING_GOVERNED_DATA),
            _answer_text(),
            ConversationDoneEventV4(),
        )
    )
    _admit(monkeypatch, _Admission(server_module, runtime))
    handler, response = _handler(server_module)
    recording = _Recording()

    _answer(handler, recording)

    assert response.began is True
    assert response.ended is True
    assert len(response.chunks) == 3
    assert recording.values[-1] == {"outcome": "answered"}
    assert runtime.requests[0].turn.question == "What can this workspace report?"


@pytest.mark.covers("ADR-077")
def test_each_turn_exchanges_its_subject_once_and_invokes_with_only_the_minted_bearer(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    runtime = _RecordingRuntime(
        events=(
            _answer_text(),
            ConversationDoneEventV4(),
        )
    )
    tooling = _admit(monkeypatch, _Admission(server_module, runtime))
    handler, response = _handler(server_module)
    recording = _Recording()

    _answer(handler, recording)

    assert recording.values[-1] == {"outcome": "answered"}
    assert response.began is True
    assert tooling.token_exchange.subjects == [_SUBJECT_TOKEN]
    # A-TEST-LITERALS-001: a fictional token; see docs/security-dispositions.md
    assert tooling.bearers == [turns.HarnessBearer(token="minted-0001")]  # nosec B106
    assert tooling.bearers[0].token != _SUBJECT_TOKEN
    assert len(runtime.requests) == 1
    assert runtime.requests[0].turn.actor_id == turns.actor_id_for("verified-owner")
    assert _SUBJECT_TOKEN not in repr(runtime.requests[0])
    assert "minted-0001" not in repr(runtime.requests[0])


@pytest.mark.covers("ADR-077")
def test_a_second_turn_mints_a_second_token_rather_than_reusing_the_first(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    """ADR-077 §2: each turn gets its own Harness token and nothing caches it."""
    tooling = _admit(monkeypatch, _Admission(server_module, _RecordingRuntime()))
    for _turn in range(2):
        tooling.runtime.events = (
            _answer_text(),
            ConversationDoneEventV4(),
        )
        handler, _response = _handler(server_module)
        _answer(handler, _Recording())

    assert tooling.token_exchange.subjects == [_SUBJECT_TOKEN, _SUBJECT_TOKEN]
    assert [bearer.token for bearer in tooling.bearers] == [
        "minted-0001",
        "minted-0002",
    ]


def test_started_stream_failure_terminalizes_and_records_dependency_failure(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    def events() -> Iterator[object]:
        yield ConversationActivityEventV4(stage=PublicActivityStageV4.RETRIEVING_GOVERNED_DATA)
        raise RuntimeError("fictional stream failure")

    runtime = _RecordingRuntime(events=events())
    _admit(monkeypatch, _Admission(server_module, runtime))
    handler, response = _handler(server_module)
    recording = _Recording()

    _answer(handler, recording)

    assert response.began is True
    assert response.ended is True
    assert response.chunks[-1].endswith(_DEPENDENCY_REFUSAL_SUFFIX)
    assert recording.values[-1] == {"outcome": "failed"}


@pytest.mark.covers("ADR-083")
def test_a_no_answer_refusal_opens_the_stream_and_records_refused(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    refusal = ConversationRefusedEventV4(
        reason_code=StreamRefusalReasonCodeV4.NO_ANSWER,
        reason="Fictional no-answer refusal.",
        audit_id="audit-01",
    )
    runtime = _RecordingRuntime(
        events=(
            ConversationActivityEventV4(stage=PublicActivityStageV4.UNDERSTANDING_REQUEST),
            refusal,
        )
    )
    _admit(monkeypatch, _Admission(server_module, runtime))
    handler, response = _handler(server_module)
    recording = _Recording()

    _answer(handler, recording)

    assert (response.began, response.ended) == (True, True)
    assert response.chunks[-1] == server_module.sse.encode(refusal)
    assert recording.values[-1] == {"outcome": "refused"}


@pytest.mark.covers("ADR-083")
def test_an_illegal_mid_stream_event_becomes_a_safe_dependency_refusal(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    runtime = _RecordingRuntime(
        events=(
            _answer_text(),
            ConversationTextEventV4(sequence=3, text="Fictional out-of-order delta."),
            ConversationDoneEventV4(),
        )
    )
    _admit(monkeypatch, _Admission(server_module, runtime))
    errors = _error_reports(monkeypatch, server_module)
    handler, response = _handler(server_module)
    recording = _Recording()

    _answer(handler, recording)

    assert len(response.chunks) == 2
    assert b"out-of-order" not in b"".join(response.chunks)
    assert response.chunks[-1].endswith(_DEPENDENCY_REFUSAL_SUFFIX)
    assert recording.values[-1] == {"outcome": "failed"}
    # A runtime that does not handle the thrown refusal leaves no receipt, so the route reports.
    assert len(errors) == 1


@pytest.mark.covers("ADR-063")
@pytest.mark.covers("ADR-083")
def test_the_route_closes_the_runtime_stream_after_its_terminal(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    """Closing the runtime generator runs its delivery write before Lambda can freeze."""
    closed: list[bool] = []

    def events() -> Iterator[object]:
        try:
            yield from _answered()
            yield ConversationTextEventV4(sequence=2, text="Never relayed.")
        finally:
            closed.append(True)

    _admit(monkeypatch, _Admission(server_module, _RecordingRuntime(events=events())))
    handler, response = _handler(server_module)

    _answer(handler, _Recording())

    assert closed == [True]
    assert response.chunks[-1] == server_module.sse.encode(
        ConversationDoneEventV4(audit_id="audit-01")
    )
    assert b"Never relayed." not in b"".join(response.chunks)


def _wire_handler(module: Any, path: str, headers: Message) -> Any:
    """Build a handler that writes its real HTTP response bytes, for header assertions."""
    handler = module.Handler.__new__(module.Handler)
    handler.path = path
    handler.headers = headers
    handler.rfile = io.BytesIO(b"{}")
    handler.wfile = io.BytesIO()
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"POST {path} HTTP/1.1"
    handler.command = "POST"
    handler.client_address = ("127.0.0.1", 0)
    handler.close_connection = False

    def quiet(*_args: object) -> None:
        return None

    handler.log_message = quiet
    return handler


def _response_headers(raw: bytes) -> list[Message]:
    """Parse the header block of every HTTP response written to one connection."""
    parsed: list[Message] = []
    for block in raw.split(b"HTTP/1.1 ")[1:]:
        head = block.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n", 1)[1]
        message = Message()
        for line in head.split("\r\n"):
            name, _, value = line.partition(": ")
            message[name] = value
        parsed.append(message)
    return parsed


@pytest.mark.covers("ADR-081")
def test_the_route_returns_its_own_correlation_and_never_a_previous_requests(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    # ADR-081 §2: one server-minted value per request. A client value is never echoed, and a
    # later request on the same kept-alive connection never receives the earlier one.
    _admit(
        monkeypatch,
        _Admission(server_module, _RecordingRuntime(events=_answered())),
    )
    request_headers = Message()
    request_headers["Content-Length"] = "2"
    request_headers["x-correlation-id"] = _CORRELATION_ID
    handler = _wire_handler(server_module, "/api/chat", request_headers)

    handler.do_POST()
    handler.path = "/health"
    handler.do_GET()

    raw = handler.wfile.getvalue()
    assert b'"kind":"text"' in raw
    assert b'"audit_id":"audit-01","kind":"done"' in raw
    stream, health = _response_headers(raw)
    assert stream["Content-Type"] == "text/event-stream"
    minted = stream["x-correlation-id"]
    assert minted is not None
    assert minted != _CORRELATION_ID
    assert str(uuid.UUID(minted)) == minted
    assert uuid.UUID(minted).version == 4
    assert health["x-correlation-id"] is None


def _timing_lines(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []

    def record(event: object, _logger: object) -> None:
        if isinstance(event, ConversationTurnTimingLog):
            lines.append(event.projection)

    monkeypatch.setattr(module, "report_lifecycle", record)
    return lines


def test_a_streamed_turn_writes_one_timing_line_in_milestone_order(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    lines = _timing_lines(monkeypatch, server_module)
    _admit(monkeypatch, _Admission(server_module, _RecordingRuntime(events=_answered())))
    handler, response = _handler(server_module)

    order: list[str] = []
    end_stream = handler._end_stream

    def ended() -> None:
        order.append("final chunk")
        end_stream()

    handler._end_stream = ended
    lines_before = lines.copy

    def flushed(*_args: object) -> None:
        order.append(f"flush after {len(lines_before())} timing lines")

    monkeypatch.setattr(server_module, "flush_traces", flushed)

    handler.do_POST()

    assert response.ended is True
    # Lambda freezes the environment once the body ends, so nothing may follow the final chunk.
    assert order == ["flush after 1 timing lines", "final chunk"]
    [line] = lines
    assert line["phase"] == "timing"
    assert line["correlation_id"] == handler._correlation_id
    milestones = [key for key in line if key.startswith("ms_")]
    assert milestones == [
        "ms_admitted",
        "ms_input_assessed",
        "ms_harness_token_exchanged",
        "ms_response_started",
        "ms_stream_closed",
    ]
    values = [cast("int", line[key]) for key in milestones]
    assert values == sorted(values)


def test_a_turn_refused_before_admission_writes_no_timing_line(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    lines = _timing_lines(monkeypatch, server_module)
    _admit(monkeypatch, _Admission(server_module, _RecordingRuntime()))

    def absent(*_args: object, **_kwargs: object) -> Never:
        raise SessionAbsentError("fictional absent cookie")

    monkeypatch.setattr(server_module, "turn_from", absent)
    handler, response = _handler(server_module)

    handler.do_POST()

    assert response.json == [(401, {"error": "unauthorized"})]
    assert lines == []


def test_a_failed_flush_still_ends_the_stream_that_carried_done(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    _admit(monkeypatch, _Admission(server_module, _RecordingRuntime(events=_answered())))
    handler, response = _handler(server_module)

    def failing(*_args: object) -> None:
        raise RuntimeError("fictional exporter failure")

    monkeypatch.setattr(server_module, "flush_traces", failing)

    with pytest.raises(RuntimeError, match="fictional exporter failure"):
        handler.do_POST()

    assert response.ended is True


def test_a_relay_failure_still_ends_the_stream_after_the_flush(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    def events() -> Iterator[object]:
        yield ConversationActivityEventV4(stage=PublicActivityStageV4.RETRIEVING_GOVERNED_DATA)
        raise RuntimeError("fictional stream failure")

    _admit(monkeypatch, _Admission(server_module, _RecordingRuntime(events=events())))
    handler, response = _handler(server_module)
    order: list[str] = []
    end_stream = handler._end_stream

    def ended() -> None:
        order.append("final chunk")
        end_stream()

    handler._end_stream = ended

    def flushed(*_args: object) -> None:
        order.append("flush")

    monkeypatch.setattr(server_module, "flush_traces", flushed)

    handler.do_POST()

    assert (response.began, response.ended) == (True, True)
    assert order == ["flush", "final chunk"]


def test_a_kept_alive_handler_never_ends_a_stream_it_did_not_open(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    _admit(monkeypatch, _Admission(server_module, _RecordingRuntime(events=_answered())))
    handler, response = _handler(server_module)
    handler.do_POST()
    endings: list[str] = []
    handler._end_stream = lambda: endings.append("final chunk")

    def absent(*_args: object, **_kwargs: object) -> Never:
        raise SessionAbsentError("fictional absent cookie")

    monkeypatch.setattr(server_module, "turn_from", absent)
    handler.do_POST()

    assert response.json[-1] == (401, {"error": "unauthorized"})
    assert endings == []


def _error_reports(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[BaseException]:
    """Capture every report the route or the runtime makes that would be logged at ERROR."""
    reports: list[BaseException] = []

    def capture(error: BaseException, _report: object) -> None:
        if not (
            isinstance(error, AdlyticsError)
            and error.error_class in {ErrorClass.DOMAIN, ErrorClass.VALIDATION}
        ):
            reports.append(error)

    monkeypatch.setattr(module, "report_error", capture)
    monkeypatch.setattr(block_runtime, "report_error", capture)
    return reports


def _harness() -> ScriptedHarness:
    return ScriptedHarness(
        tuple(ManagedHarnessTextDeltaV1(text=text) for text in ("Fictional ", "answer."))
    )


def test_a_client_that_leaves_mid_stream_is_client_disconnected_and_not_an_error(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    errors = _error_reports(monkeypatch, server_module)
    audit = RecordingAudit()
    events = managed_runtime(_harness(), audit).run(managed_request())
    first = next(events)
    handler, _response = _handler(server_module)
    written: list[bytes] = []

    def gone(frame: bytes) -> None:
        if written:
            raise BrokenPipeError("fictional closed socket")
        written.append(frame)

    handler._chunk = gone
    recording = _Recording()

    handler._relay(
        server_module._RelayRequest(
            first=first,
            remaining=events,
            released=server_module._ReleasedTurn(verified=_admitted_turn(server_module).turn),
        ),
        recording,
    )

    assert recording.values[-1] == {"outcome": "client_disconnected"}
    assert recording.errors == []
    assert errors == []
    [record] = audit.records
    assert record.terminal_outcome is AuditTerminalOutcomeV2.CLIENT_DISCONNECTED


def test_an_illegal_first_runtime_event_is_a_503_with_a_dependency_failure_receipt(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    """O1: the pre-stream refusal is the runtime's own receipt and its single error report."""
    audit = RecordingAudit()
    harness = _harness()
    _admit(monkeypatch, _Admission(server_module, cast("Any", managed_runtime(harness, audit))))
    errors = _error_reports(monkeypatch, server_module)

    def illegal(*_args: object, **_kwargs: object) -> Never:
        raise ValueError("fictional illegal first event")

    monkeypatch.setattr(server_module, "validate_conversation_stream", illegal)
    handler, response = _handler(server_module)
    recording = _Recording()

    _answer(handler, recording)

    assert response.json == [(503, _UNAVAILABLE_RESPONSE)]
    assert response.began is False
    [record] = audit.records
    assert record.terminal_outcome is AuditTerminalOutcomeV2.DEPENDENCY_FAILURE
    assert record.failure_error_code == "ADLY-MANAGED_CONVERSATION-0003"
    assert harness.calls == []
    assert len(errors) == 1
    assert recording.values[-1] == {"error_type": "ValueError", "outcome": "failed"}


# ADR-120: the route redeems before the guardrail and never fails a turn on redemption ---------

_PAGE_REFERENCE = PageContextReference("P" * 43)


def _admit_with_reference(
    monkeypatch: pytest.MonkeyPatch, admission: _Admission, redeem: Any
) -> _Tooling:
    tooling = _admit(monkeypatch, admission)
    object.__setattr__(tooling, "redeem", redeem)
    referenced = replace(
        _admitted_turn(admission.module).turn, page_context_reference=_PAGE_REFERENCE
    )

    def accepted_turn(*_args: object, **_kwargs: object) -> turns.AdmittedTurn:
        return turns.AdmittedTurn(turn=referenced, subject_token=_SUBJECT_TOKEN)

    monkeypatch.setattr(admission.module, "turn_from", accepted_turn)
    return tooling


@pytest.mark.covers("ADR-120")
def test_the_guardrail_assesses_the_question_and_the_page_context_block_together(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    runtime = _RecordingRuntime(events=_answered())
    guardrail = _Guardrail()
    context = redeemed_page_context(
        PageContextDisplayLabels(page="Campaigns", window="Sep 9\u201315, 2026")
    )

    def redeem(_verified: object) -> turns.RedeemedTurnContext:
        return turns.RedeemedTurnContext(
            status=ConversationContextStatusV1(used=True), page_context=context
        )

    _admit_with_reference(monkeypatch, _Admission(server_module, runtime, guardrail), redeem)
    handler, _ = _handler(server_module)

    _answer(handler, _Recording())

    (assessed,) = guardrail.requests
    assert assessed.text == f"What can this workspace report?\n\n{page_context_block(context)}"
    (request,) = runtime.requests
    assert request.turn.page_context == context
    assert request.page_context_reference == _PAGE_REFERENCE


@pytest.mark.covers("ADR-120")
def test_a_redemption_that_cannot_be_read_runs_the_turn_without_context(
    monkeypatch: pytest.MonkeyPatch, server_module: Any
) -> None:
    runtime = _RecordingRuntime(events=_answered())

    def redeem(_verified: object) -> Never:
        raise DependencyError(ErrorCode("PAGE_CONTEXT", 2), "Page context could not be read.")

    _admit_with_reference(monkeypatch, _Admission(server_module, runtime), redeem)
    handler, response = _handler(server_module)

    _answer(handler, _Recording())

    (request,) = runtime.requests
    assert request.turn.page_context is None
    assert request.context_status == turns.redemption_failed().status
    assert response.began is True
