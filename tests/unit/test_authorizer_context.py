# SPDX-License-Identifier: MIT-0
"""The authorizer adapter: event reshaping, the review-levels context, and one JWKS refresh.

The decision itself is covered in test_route_authorization; these pin what the adapter feeds it
(route key, both credential slots, the public client) and what it hands the review function.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from conftest import CLIENT_ID, ISSUER, NOW, SUBJECT, TokenMinter, stub_origin_verify

from adapters import config
from domain.identity.tokens import JsonWebKeySet

if TYPE_CHECKING:
    from collections.abc import Generator

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-078", "ADR-081")]

PUBLIC_CLIENT_ID = "2publicexample345678"
APP_ORIGIN = "https://app.customer.com"


@dataclass
class _Keys:
    """Serves a stale key set until refreshed, and counts refreshes."""

    stale: JsonWebKeySet
    fresh: JsonWebKeySet
    refreshes: int = 0

    def signing_keys(self) -> JsonWebKeySet:
        return self.stale

    def refresh_signing_keys(self) -> JsonWebKeySet:
        self.refreshes += 1
        return self.fresh


@pytest.fixture
def authorizer(monkeypatch: pytest.MonkeyPatch) -> Generator[ModuleType]:
    settings = SimpleNamespace(
        cognito_issuer=ISSUER, cognito_client_id=CLIENT_ID, app_base_url=APP_ORIGIN
    )
    monkeypatch.setattr(config, "settings_without_client_secret", lambda: settings)
    monkeypatch.setattr(config, "authorizer_public_client_id", lambda: PUBLIC_CLIENT_ID)
    monkeypatch.setattr(config, "tracing_settings", lambda: SimpleNamespace(enabled=False))
    stub_origin_verify(monkeypatch)
    name = "adapters.inbound.auth_handlers.authorizer"
    sys.modules.pop(name, None)
    module = importlib.import_module(name)
    monkeypatch.setattr(module, "utc_now", lambda: NOW)
    yield module
    sys.modules.pop(name, None)


def _event(route_key: str, *, cookies: list[str], authorization: str | None) -> dict[str, Any]:
    headers = {"sec-fetch-site": "same-origin", "x-adl-csrf": "csrf"}
    if authorization is not None:
        headers["Authorization"] = authorization
    return {
        "routeKey": route_key,
        # A raw path that differs from the route key must change nothing about the entitlement.
        "rawPath": "/API/Quality-Review/Turn/",
        "cookies": cookies,
        "headers": headers,
        "requestContext": {"http": {"method": "POST", "path": "/API/Quality-Review/Turn/"}},
    }


def _decide(module: ModuleType, event: dict[str, Any]) -> dict[str, str]:
    decision = module._decide(module._to_request(event))
    return module._context(decision, "123e4567-e89b-42d3-a456-426614174000")


def test_a_bearer_on_a_review_route_carries_sorted_review_levels(
    authorizer: ModuleType, minter: TokenMinter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(authorizer, "PROVIDER", _Keys(minter.jwks(), minter.jwks()))
    token = minter.token(
        client_id=PUBLIC_CLIENT_ID, quality_review_levels=["quality_reviewer", "audit_attribution"]
    )
    context = _decide(
        authorizer,
        _event("POST /api/quality-review/turns", cookies=[], authorization=f"Bearer {token}"),
    )
    assert context == {
        "subject": SUBJECT,
        "correlation_id": "123e4567-e89b-42d3-a456-426614174000",
        "can_manage_prompts": "false",
        "can_read_operational_status": "false",
        "can_request_runs": "false",
        "review_levels": "audit_attribution,quality_reviewer",
        "reviewer": SUBJECT,
        "attribution": "true",
    }


def test_a_reviewer_without_attribution_is_logged_without_the_flag(
    authorizer: ModuleType, minter: TokenMinter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-081 §4: the access log's flag is true only for a caller in both review groups.
    monkeypatch.setattr(authorizer, "PROVIDER", _Keys(minter.jwks(), minter.jwks()))
    token = minter.token(client_id=PUBLIC_CLIENT_ID, quality_review_levels=["quality_reviewer"])
    context = _decide(
        authorizer,
        _event("POST /api/quality-review/turn", cookies=[], authorization=f"Bearer {token}"),
    )
    assert (context["reviewer"], context["attribution"]) == (SUBJECT, "false")


def test_a_non_review_route_never_receives_review_levels(
    authorizer: ModuleType, minter: TokenMinter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(authorizer, "PROVIDER", _Keys(minter.jwks(), minter.jwks()))
    token = minter.token(quality_review_levels=["quality_reviewer", "prompt_admin"])
    context = _decide(
        authorizer,
        _event(
            "POST /api/prompt-registry/activate",
            cookies=[f"adl_session={token}", "adl_csrf=csrf"],
            authorization=None,
        ),
    )
    assert "review_levels" not in context
    # The access log must never record an end user: no reviewer key off the review routes.
    assert "reviewer" not in context
    assert "attribution" not in context
    assert context["can_manage_prompts"] == "true"


def test_an_unknown_kid_refreshes_the_key_set_exactly_once(
    authorizer: ModuleType, minter: TokenMinter, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = _Keys(minter.jwks(kid="retired-key"), minter.jwks())
    monkeypatch.setattr(authorizer, "PROVIDER", keys)
    token = minter.token(client_id=PUBLIC_CLIENT_ID, quality_review_levels=["quality_reviewer"])
    context = _decide(
        authorizer,
        _event("POST /api/quality-review/turn", cookies=[], authorization=f"Bearer {token}"),
    )
    assert context["subject"] == SUBJECT
    assert keys.refreshes == 1


def test_the_bearer_is_verified_against_the_public_client_not_the_bff_client(
    authorizer: ModuleType, minter: TokenMinter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(authorizer, "PROVIDER", _Keys(minter.jwks(), minter.jwks()))
    request = authorizer._to_request(
        _event("POST /api/quality-review/turn", cookies=[], authorization="Bearer a.b.c")
    )
    assert (request.bearer_client_id, request.session_client_id) == (PUBLIC_CLIENT_ID, CLIENT_ID)
    assert request.route_key == "POST /api/quality-review/turn"
    assert request.authorization_header == "Bearer a.b.c"


def test_an_event_without_a_route_key_is_refused_before_any_decision(
    authorizer: ModuleType, minter: TokenMinter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(authorizer, "PROVIDER", _Keys(minter.jwks(), minter.jwks()))
    event = _event("POST /api/quality-review/turn", cookies=[], authorization=None)
    del event["routeKey"]
    with pytest.raises(authorizer.IdentityError, match="route key"):
        authorizer._to_request(event)
