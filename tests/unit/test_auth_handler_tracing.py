# SPDX-License-Identifier: MIT-0
"""Root tracing boundaries for authorization and authentication request handlers."""

from __future__ import annotations

import importlib
import inspect
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from conftest import EDGE_ORIGIN_VALUE, stub_origin_verify

from adapters import config
from adapters.outbound.telemetry.reporting import OperationLog
from domain.identity.authorization import CredentialKind, RouteAuthorization
from domain.identity.session import Identity

if TYPE_CHECKING:
    from collections.abc import Generator

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-007")]


@dataclass
class _Recording:
    attributes: dict[str, object]

    def record(self, **attributes: object) -> None:
        self.attributes.update(attributes)

    def note_error(self, error: object) -> None:
        self.attributes["error_type"] = type(error).__name__


@pytest.fixture
def auth_modules(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[ModuleType, ModuleType]]:
    settings = SimpleNamespace()
    tracing = SimpleNamespace(enabled=False)
    monkeypatch.setattr(config, "settings", lambda: settings)
    monkeypatch.setattr(config, "settings_without_client_secret", lambda: settings)
    monkeypatch.setattr(config, "authorizer_public_client_id", lambda: "public-client-test")
    monkeypatch.setattr(config, "tracing_settings", lambda: tracing)
    stub_origin_verify(monkeypatch)
    names = (
        "adapters.inbound.auth_handlers.authorizer",
        "adapters.inbound.auth_handlers.handlers",
    )
    for name in names:
        sys.modules.pop(name, None)
    authorizer = importlib.import_module(names[0])
    handlers = importlib.import_module(names[1])
    yield authorizer, handlers
    for name in names:
        sys.modules.pop(name, None)


def test_authorizer_root_span_records_only_the_safe_terminal_decision(
    auth_modules: tuple[ModuleType, ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    authorizer, _handlers = auth_modules
    captured: list[tuple[str, _Recording]] = []

    @contextmanager
    def capture(name: str, **attributes: object) -> Generator[_Recording]:
        initial: dict[str, object] = dict(attributes)
        recording = _Recording(initial)
        captured.append((name, recording))
        yield recording

    def request_from_event(_event: object) -> object:
        return object()

    identity = Identity("customer-subject-must-not-span", "https://issuer.test")

    def decide(_request: object) -> RouteAuthorization:
        return RouteAuthorization(
            identity=identity, credential=CredentialKind.SESSION_COOKIE, review_route=False
        )

    def discard_operation(_event: object, _logger: object) -> None:
        return None

    monkeypatch.setattr(authorizer, "span", capture)
    monkeypatch.setattr(authorizer, "_to_request", request_from_event)
    monkeypatch.setattr(authorizer, "_decide", decide)
    monkeypatch.setattr(authorizer, "report_operation", discard_operation)

    response = inspect.unwrap(authorizer.handler)(
        {
            "cookies": ["adl_session=secret-token"],
            "headers": {
                "x-correlation-id": "123e4567-e89b-42d3-a456-426614174000",
                "x-adl-origin-verify": EDGE_ORIGIN_VALUE,
            },
        },
        object(),
    )

    assert response["isAuthorized"] is True
    assert vars(authorizer.handler)["__trace_boundaries__"] == (authorizer.AUTH_AUTHORIZE,)
    assert len(captured) == 1
    name, recording = captured[0]
    assert name == authorizer.AUTH_AUTHORIZE
    assert recording.attributes == {"operation": "auth.authorize", "outcome": "authorized"}
    assert "secret-token" not in repr(recording.attributes)
    assert identity.subject not in repr(recording.attributes)


def test_auth_request_root_span_records_status_without_cookie_or_response_content(
    auth_modules: tuple[ModuleType, ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    _authorizer, handlers = auth_modules
    captured: list[tuple[str, _Recording]] = []

    @contextmanager
    def capture(name: str, **attributes: object) -> Generator[_Recording]:
        initial: dict[str, object] = dict(attributes)
        recording = _Recording(initial)
        captured.append((name, recording))
        yield recording

    response: dict[str, object] = {
        "statusCode": 200,
        "cookies": ["adl_session=secret-token"],
        "body": '{"subject":"customer-subject-must-not-span"}',
    }

    def resolve(_event: object) -> str:
        return handlers.routing.LOGIN

    def dispatch(_event: object) -> dict[str, object]:
        return response

    def discard_operation(_event: object, _logger: object) -> None:
        return None

    monkeypatch.setattr(handlers, "span", capture)
    monkeypatch.setattr(handlers.routing, "resolve", resolve)
    monkeypatch.setitem(handlers._DISPATCH, handlers.routing.LOGIN, dispatch)
    monkeypatch.setattr(handlers, "report_operation", discard_operation)

    returned = inspect.unwrap(handlers.handler)(
        {"headers": {"x-adl-origin-verify": EDGE_ORIGIN_VALUE}}, object()
    )

    assert returned is response
    assert vars(handlers.handler)["__trace_boundaries__"] == (handlers.AUTH_REQUEST,)
    assert len(captured) == 1
    name, recording = captured[0]
    assert name == handlers.AUTH_REQUEST
    assert recording.attributes == {
        "operation": "auth.request",
        "outcome": "completed",
        "status": 200,
    }
    assert "secret-token" not in repr(recording.attributes)
    assert "customer-subject" not in repr(recording.attributes)


@pytest.mark.covers("ADR-081")
def test_the_authorizer_mints_the_correlation_and_ignores_a_client_value(
    auth_modules: tuple[ModuleType, ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-081 §2: the one identifier a request carries is minted by the first server hop. A
    # valid client value must not become the support key, or a caller could choose a value that
    # collides with another turn's receipt and hide it from the review lookup.
    authorizer, _handlers = auth_modules
    reported: list[object] = []
    client_value = "123e4567-e89b-42d3-a456-426614174000"

    def request_from_event(_event: object) -> object:
        return object()

    def decide(_request: object) -> RouteAuthorization:
        return RouteAuthorization(
            identity=Identity("customer-subject", "https://issuer.test"),
            credential=CredentialKind.SESSION_COOKIE,
            review_route=False,
        )

    def record_operation(event: object, _logger: object) -> None:
        reported.append(event)

    monkeypatch.setattr(authorizer, "_to_request", request_from_event)
    monkeypatch.setattr(authorizer, "_decide", decide)
    monkeypatch.setattr(authorizer, "report_operation", record_operation)
    event = {
        "cookies": ["adl_session=secret-token"],
        "headers": {"x-correlation-id": client_value, "x-adl-origin-verify": EDGE_ORIGIN_VALUE},
    }

    first = inspect.unwrap(authorizer.handler)(event, object())
    second = inspect.unwrap(authorizer.handler)(event, object())

    minted = [first["context"]["correlation_id"], second["context"]["correlation_id"]]
    assert client_value not in minted
    assert minted[0] != minted[1]
    assert [cast("OperationLog", line).details["correlation_id"] for line in reported] == minted
