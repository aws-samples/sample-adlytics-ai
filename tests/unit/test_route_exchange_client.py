# SPDX-License-Identifier: MIT-0
"""Route exchange client response decoding (ADR-077 §2): exact, fail-closed, token-free errors.

Only the pure decoding is unit tested. The Lambda Invoke itself is covered against the deployed
function in tests/integration/test_token_service.py; mocking the SDK here would test the mock.
"""

from __future__ import annotations

import json

import pytest
from fictional_tokens import fictional_jwt

from adapters.outbound.token_service.route_exchange_client import (
    ROUTE_EXCHANGE_UNAVAILABLE,
    LambdaRouteTokenExchange,
    harness_token_from,
)
from domain.identity.session import IdentityError
from domain.shared_kernel.errors import DependencyError
from ports.outbound.harness_token import HarnessToken, HarnessTokenPort

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-077")]

_MINTED = fictional_jwt()
_SUCCESS = {"access_token": _MINTED, "token_type": "Bearer", "expires_in": 240}


def _raw(document: object) -> bytes:
    return json.dumps(document).encode()


def test_the_exact_success_document_decodes_to_the_minted_token() -> None:
    token = harness_token_from(_raw(_SUCCESS))

    assert token == HarnessToken(access_token=_MINTED, expires_in=240)
    assert _MINTED not in repr(token)


def test_invalid_grant_is_the_identity_refusal_and_names_no_token() -> None:
    assert harness_token_from(_raw(_SUCCESS)).access_token == _MINTED

    with pytest.raises(IdentityError) as caught:
        harness_token_from(_raw({"error": "invalid_grant"}))

    assert not isinstance(caught.value, DependencyError)
    assert _MINTED not in str(caught.value)


@pytest.mark.parametrize(
    "document",
    [
        {**_SUCCESS, "refresh_token": "never-issued"},
        {**_SUCCESS, "token_type": "bearer"},
        {**_SUCCESS, "expires_in": 0},
        {**_SUCCESS, "expires_in": True},
        {**_SUCCESS, "expires_in": "240"},
        {**_SUCCESS, "access_token": ""},
        {k: v for k, v in _SUCCESS.items() if k != "expires_in"},
        {"error": "invalid_grant", "error_description": "a reason"},
        {"error": "server_error"},
        [_SUCCESS],
        None,
    ],
)
def test_any_other_shape_is_a_dependency_failure_never_a_token_or_a_refusal(
    document: object,
) -> None:
    """A drifted contract must not become a bearer, nor send a healthy session to refresh."""
    assert harness_token_from(_raw(_SUCCESS)).expires_in == 240

    with pytest.raises(DependencyError) as caught:
        harness_token_from(_raw(document))

    assert caught.value.code == ROUTE_EXCHANGE_UNAVAILABLE
    assert _MINTED not in str(caught.value) and _MINTED not in str(caught.value.context)


def test_a_body_that_is_not_json_is_a_dependency_failure() -> None:
    with pytest.raises(DependencyError) as caught:
        harness_token_from(b"\xff not json")

    assert caught.value.code == ROUTE_EXCHANGE_UNAVAILABLE


def test_an_empty_subject_is_refused_before_anything_is_invoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built with a real client and no credentials: reaching Invoke would fail differently."""
    for name in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/aws-config")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/aws-credentials")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    client = LambdaRouteTokenExchange(
        "arn:aws:lambda:eu-west-1:111122223333:function:adlytics-dev-TokenRouteExchange",
        region="eu-west-1",
    )

    assert isinstance(client, HarnessTokenPort)
    with pytest.raises(IdentityError, match="No subject token"):
        client.exchange("")


def test_the_client_cannot_be_built_without_a_function() -> None:
    with pytest.raises(ValueError, match="function ARN"):
        LambdaRouteTokenExchange(" ", region="eu-west-1")
