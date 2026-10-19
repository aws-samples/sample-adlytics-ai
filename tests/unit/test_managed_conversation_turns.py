# SPDX-License-Identifier: MIT-0
"""Authenticated managed conversation admission and execution-request framing."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest
from conftest import NOW, SUBJECT, TokenMinter

from adapters.config import Settings, load_settings
from adapters.inbound.conversation import turns
from adapters.inbound.conversation.turns import (
    AdmittedTurn,
    ConversationTooling,
    TurnHttpRequest,
    VerifiedTurn,
    actor_id_for,
    managed_request_from,
    turn_from,
)
from adapters.outbound.token_service.route_exchange_client import LambdaRouteTokenExchange
from domain.conversation.managed import HarnessTurnV2, ManagedConversationRequestV1
from domain.identity.session import IdentityError, SessionAbsentError
from domain.shared_kernel.errors import ValidationError

pytestmark = pytest.mark.unit

_CORRELATION_ID = "123e4567-e89b-42d3-a456-426614174000"
_CSRF = "fictional-csrf-value"
_SETTINGS_SOURCE = {
    "ADL_ENVIRONMENT": "dev",
    "ADL_AWS_REGION": "eu-west-1",
    "ADL_COGNITO_USER_POOL_ID": "eu-west-1_example",
    "ADL_COGNITO_CLIENT_ID": "1example23456789",
    # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
    "ADL_COGNITO_CLIENT_SECRET": "fictional-client-secret",
    "ADL_COGNITO_LOGIN_DOMAIN": "auth.test.invalid",
    "ADL_APP_BASE_URL": "https://app.test.invalid",
    "ADL_API_BASE_URL": "https://api.test.invalid",
    "ADL_COOKIE_SCOPE": "host_only",
    "ADL_REFRESH_COOKIE_MAX_AGE_SECONDS": "2592000",
    "ADL_ATHENA_WORKGROUP": "unused-by-turn-test",
    "ADL_SEMANTIC_ATTEMPTS_TABLE": "unused-by-turn-test",
    "ADL_SEMANTIC_LEASES_TABLE": "unused-by-turn-test",
    "ADL_SEMANTIC_PUBLICATIONS_TABLE": "unused-by-turn-test",
    "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "0",
    "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "8",
    "ADL_QUICK_CHAT_TIMEOUT_SECONDS": "120",
    "ADL_DEEP_DIVE_TIMEOUT_SECONDS": "180",
    "ADL_HARNESS_ARN": "arn:aws:bedrock-agentcore:eu-west-1:111122223333:harness/test",
    "ADL_AGENT_QUALITY_AUDIT_TABLE": "unused-by-turn-test",
    "ADL_RECEIPT_RETENTION_DAYS": "90",
    "ADL_PAGE_CONTEXT_TABLE": "unused-by-turn-test",
    "ADL_GUARDRAILS_ENABLED": "false",
    "ADL_TOKEN_ROUTE_EXCHANGE_FUNCTION": (
        "arn:aws:lambda:eu-west-1:111122223333:function:adlytics-dev-TokenRouteExchange"
    ),
}


def _verified_turn(*, owner: str, session: str, question: str) -> VerifiedTurn:
    return VerifiedTurn(
        question=question,
        verified_owner_id=owner,
        conversation_id=f"conversation-{owner}-{session}",
        custody_session_id=session,
        correlation_id=_CORRELATION_ID,
    )


def test_managed_turn_actor_is_the_versioned_digest_of_the_subject_alone() -> None:
    request = managed_request_from(
        _verified_turn(
            owner="verified-subject",
            session="browser-session",
            question="Which metrics can this workspace report?",
        )
    )

    assert isinstance(request, ManagedConversationRequestV1)
    expected_actor = hashlib.sha256(b"user-memory-v1\0verified-subject").hexdigest()
    assert request.turn.actor_id == f"actor-{expected_actor}"
    assert request.turn.conversation_id == "conversation-verified-subject-browser-session"
    assert request.turn.question == "Which metrics can this workspace report?"


@pytest.mark.covers("ADR-077")
def test_actor_id_for_is_the_public_derivation_the_managed_turn_uses() -> None:
    expected = "actor-" + hashlib.sha256(b"user-memory-v1\0owner-a").hexdigest()

    assert actor_id_for("owner-a") == expected
    assert (
        managed_request_from(
            _verified_turn(owner="owner-a", session="browser-session", question="Question")
        ).turn.actor_id
        == expected
    )


@pytest.mark.covers("ADR-077")
def test_one_user_keeps_one_actor_across_browser_sessions_but_not_one_conversation() -> None:
    """Memory is per user and the Harness session per conversation (ADR-077 §6)."""
    first = managed_request_from(
        _verified_turn(owner="owner-a", session="browser-session-1", question="Question A")
    )
    second = managed_request_from(
        _verified_turn(owner="owner-a", session="browser-session-2", question="Question B")
    )

    assert first.turn.actor_id == second.turn.actor_id
    assert first.turn.conversation_id == "conversation-owner-a-browser-session-1"
    assert second.turn.conversation_id == "conversation-owner-a-browser-session-2"


def test_catalog_wording_does_not_select_a_chat_specific_runtime_route() -> None:
    inventory = managed_request_from(
        _verified_turn(
            owner="verified-subject",
            session="browser-session",
            question="What metrics can this workspace report?",
        )
    )
    analytical = managed_request_from(
        _verified_turn(
            owner="verified-subject",
            session="browser-session",
            question="How did ROAS change last week?",
        )
    )

    assert type(inventory) is type(analytical)
    assert inventory.schema_version == "ManagedConversationRequest.v1"


@pytest.mark.covers("ADR-077")
def test_managed_turn_owner_is_not_selected_by_the_browser_session_value() -> None:
    first = managed_request_from(
        _verified_turn(owner="owner-a", session="same-browser-session", question="Question A")
    )
    second = managed_request_from(
        _verified_turn(owner="owner-b", session="same-browser-session", question="Question B")
    )

    assert first.turn.actor_id == actor_id_for("owner-a")
    assert second.turn.actor_id == actor_id_for("owner-b")
    assert first.turn.actor_id != second.turn.actor_id
    assert first.turn.conversation_id != second.turn.conversation_id


def test_harness_turn_rejects_a_missing_actor_identity() -> None:
    with pytest.raises(ValueError, match="actor identity"):
        HarnessTurnV2(
            question="Fictional question",
            conversation_id="conversation-fictional",
            actor_id="",
            correlation_id=_CORRELATION_ID,
        )


@pytest.fixture
def settings() -> Settings:
    return load_settings(_SETTINGS_SOURCE)


@pytest.fixture
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin admission's clock so expiry boundaries are exact rather than racing wall time."""
    monkeypatch.setattr(turns, "utc_now", lambda: NOW)


def _body(session_id: str = "browser-session") -> bytes:
    return json.dumps(
        {
            "schema_version": "ConversationRequest.v3",
            "question": "Which metrics can this workspace report?",
            "session_id": session_id,
        }
    ).encode()


def _http(token: str, *, body: bytes | None = None, csrf: str = _CSRF) -> TurnHttpRequest:
    return TurnHttpRequest(
        method="POST",
        path="/api/chat",
        headers={
            "cookie": f"adl_session={token}; adl_csrf={_CSRF}",
            "x-adl-csrf": csrf,
            "sec-fetch-site": "same-origin",
        },
        body=_body() if body is None else body,
        correlation_id=_CORRELATION_ID,
    )


def _token_expiring_after(minter: TokenMinter, remaining: timedelta) -> str:
    return minter.token(exp=int((NOW + remaining).timestamp()))


@pytest.mark.covers("ADR-077")
@pytest.mark.usefixtures("fixed_clock")
def test_turn_from_admits_the_exact_turn_and_the_verified_cookie_token_as_subject(
    minter: TokenMinter, settings: Settings
) -> None:
    token = _token_expiring_after(minter, timedelta(minutes=10))

    admitted = turn_from(_http(token), keys=minter.jwks(), settings=settings)

    expected_conversation = (
        "conversation-" + hashlib.sha256(f"{SUBJECT}\0browser-session".encode()).hexdigest()
    )
    assert admitted == AdmittedTurn(
        turn=VerifiedTurn(
            question="Which metrics can this workspace report?",
            verified_owner_id=SUBJECT,
            conversation_id=expected_conversation,
            custody_session_id="browser-session",
            correlation_id=_CORRELATION_ID,
        ),
        subject_token=token,
    )


@pytest.mark.covers("ADR-077")
@pytest.mark.usefixtures("fixed_clock")
def test_a_token_with_one_second_left_is_admitted_because_there_is_no_freshness_gate(
    minter: TokenMinter, settings: Settings
) -> None:
    """ADR-077 §2: the user token only has to be unexpired; the Harness token's life is its own."""
    token = _token_expiring_after(minter, timedelta(seconds=1))

    assert turn_from(_http(token), keys=minter.jwks(), settings=settings).subject_token == token


@pytest.mark.covers("ADR-077", "ADR-007")
@pytest.mark.usefixtures("fixed_clock")
def test_an_expired_token_is_the_present_cookie_denial_not_the_absent_one(
    minter: TokenMinter, settings: Settings
) -> None:
    assert turn_from(
        _http(_token_expiring_after(minter, timedelta(seconds=1))),
        keys=minter.jwks(),
        settings=settings,
    )

    with pytest.raises(IdentityError, match="expired") as caught:
        turn_from(
            _http(_token_expiring_after(minter, timedelta(0))),
            keys=minter.jwks(),
            settings=settings,
        )
    assert not isinstance(caught.value, SessionAbsentError)


@pytest.mark.covers("ADR-077", "ADR-007")
@pytest.mark.usefixtures("fixed_clock")
def test_a_request_with_no_session_cookie_is_the_session_absent_denial(
    minter: TokenMinter, settings: Settings
) -> None:
    request = _http(minter.token())
    headers = {**request.headers, "cookie": f"adl_csrf={_CSRF}"}

    with pytest.raises(SessionAbsentError):
        turn_from(
            TurnHttpRequest(
                method=request.method,
                path=request.path,
                headers=headers,
                body=request.body,
                correlation_id=request.correlation_id,
            ),
            keys=minter.jwks(),
            settings=settings,
        )


@pytest.mark.covers("ADR-077")
@pytest.mark.usefixtures("fixed_clock")
def test_identity_is_refused_before_the_body_is_decoded(
    minter: TokenMinter, settings: Settings
) -> None:
    expired = _token_expiring_after(minter, timedelta(0))
    live = _token_expiring_after(minter, timedelta(minutes=5))

    with pytest.raises(IdentityError, match="expired"):
        turn_from(_http(expired, body=b"not-json"), keys=minter.jwks(), settings=settings)
    with pytest.raises(ValidationError, match=r"ConversationRequest\.v3"):
        turn_from(_http(live, body=b"not-json"), keys=minter.jwks(), settings=settings)


@pytest.mark.covers("ADR-077")
@pytest.mark.usefixtures("fixed_clock")
def test_a_csrf_failure_is_the_present_cookie_denial(
    minter: TokenMinter, settings: Settings
) -> None:
    token = _token_expiring_after(minter, timedelta(minutes=5))

    with pytest.raises(IdentityError, match="CSRF") as caught:
        turn_from(_http(token, csrf="not-the-token"), keys=minter.jwks(), settings=settings)
    assert type(caught.value) is IdentityError


@pytest.mark.covers("ADR-077")
@pytest.mark.usefixtures("fixed_clock")
def test_the_subject_token_never_appears_in_the_repr_of_the_admitted_turn(
    minter: TokenMinter, settings: Settings
) -> None:
    token = _token_expiring_after(minter, timedelta(minutes=5))

    admitted = turn_from(_http(token), keys=minter.jwks(), settings=settings)

    assert admitted.subject_token == token
    assert token not in repr(admitted)
    assert token not in repr(managed_request_from(admitted.turn))


@pytest.mark.covers("ADR-077")
def test_conversation_composition_fails_closed_without_the_route_exchange_function(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the exchange the route has no Harness credential at all (ADR-077 §2)."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    composed = ConversationTooling.of(settings)
    assert isinstance(composed.token_exchange, LambdaRouteTokenExchange)
    unconfigured = settings.model_copy(update={"token_route_exchange_function": ""})

    with pytest.raises(ValidationError) as caught:
        ConversationTooling.of(unconfigured)

    assert caught.value.context == {"missing": ["ADL_TOKEN_ROUTE_EXCHANGE_FUNCTION"]}


@pytest.mark.covers("ADR-081")
def test_conversation_composition_fails_closed_without_a_declared_receipt_retention(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route writes every receipt; without a retention it would write ones that never expire."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    unconfigured = settings.model_copy(update={"receipt_retention_days": None})

    with pytest.raises(ValidationError) as caught:
        ConversationTooling.of(unconfigured)

    assert caught.value.context == {"missing": ["ADL_RECEIPT_RETENTION_DAYS"]}
