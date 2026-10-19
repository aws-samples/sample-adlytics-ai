# SPDX-License-Identifier: MIT-0
"""`/me` entitlements: each one is the authorizer's verified projection (ADR-115 decisions 7, 8)."""

from __future__ import annotations

import importlib
import json
import sys
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from conftest import stub_origin_verify

from adapters import config
from domain.identity.session import IdentityError

if TYPE_CHECKING:
    from collections.abc import Generator

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-115")]

_NAME = "adapters.inbound.auth_handlers.handlers"


@pytest.fixture
def handlers(monkeypatch: pytest.MonkeyPatch) -> Generator[ModuleType]:
    settings = SimpleNamespace(operational_status_provisioned=False)
    monkeypatch.setattr(config, "settings", lambda: settings)
    monkeypatch.setattr(config, "tracing_settings", lambda: SimpleNamespace(enabled=False))
    stub_origin_verify(monkeypatch)
    sys.modules.pop(_NAME, None)
    yield importlib.import_module(_NAME)
    sys.modules.pop(_NAME, None)


def _event(**context: str) -> dict[str, Any]:
    return {
        "requestContext": {
            "http": {"method": "GET", "path": "/api/me"},
            "authorizer": {"lambda": {"subject": "subject-fictional", **context}},
        }
    }


def _me(handlers: ModuleType, *, provisioned: bool, member: bool) -> dict[str, object]:
    handlers.SETTINGS.operational_status_provisioned = provisioned
    response = handlers.session_handler(
        _event(can_manage_prompts="false", can_read_operational_status=str(member).lower())
    )
    assert response["statusCode"] == 200
    return json.loads(response["body"])


def _body(*, granted: bool, provisioned: bool) -> dict[str, object]:
    return {
        "subject": "subject-fictional",
        "can_manage_prompts": False,
        "can_read_operational_status": granted,
        "operational_status_provisioned": provisioned,
    }


def test_not_provisioned_grants_nothing_even_to_a_member(handlers: ModuleType) -> None:
    assert _me(handlers, provisioned=False, member=True) == _body(granted=False, provisioned=False)
    assert _me(handlers, provisioned=False, member=False) == _body(granted=False, provisioned=False)
    # Positive control: the same member, provisioned, is granted.
    assert _me(handlers, provisioned=True, member=True) == _body(granted=True, provisioned=True)


def test_provisioned_without_membership_has_no_access(handlers: ModuleType) -> None:
    assert _me(handlers, provisioned=True, member=False) == _body(granted=False, provisioned=True)
    # Positive control: membership is what grants it here.
    assert _me(handlers, provisioned=True, member=True)["can_read_operational_status"] is True


def test_provisioned_with_membership_is_granted(handlers: ModuleType) -> None:
    assert _me(handlers, provisioned=True, member=True) == _body(granted=True, provisioned=True)
    # Positive control: provisioning is what grants it to the same member.
    assert _me(handlers, provisioned=False, member=True)["can_read_operational_status"] is False


def test_prompt_administration_is_independent_of_operational_status(handlers: ModuleType) -> None:
    handlers.SETTINGS.operational_status_provisioned = True
    response = handlers.session_handler(
        _event(can_manage_prompts="true", can_read_operational_status="false")
    )
    body = json.loads(response["body"])
    assert body["can_manage_prompts"] is True
    assert body["can_read_operational_status"] is False


def test_an_event_without_the_authorizer_context_is_refused(handlers: ModuleType) -> None:
    with pytest.raises(IdentityError):
        handlers.session_handler({"requestContext": {"http": {"method": "GET", "path": "/api/me"}}})


def test_provisioning_defaults_to_absent() -> None:
    assert config.Settings.model_fields["operational_status_provisioned"].default is False
