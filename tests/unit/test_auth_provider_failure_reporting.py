# SPDX-License-Identifier: MIT-0
"""Auth endpoints report a provider outage they absorb, and answer exactly as before.

A rejected credential and a provider outage share one response on the callback, refresh and logout
endpoints. Only the outage is ours, so only it reaches `report_error`; the response for each is
compared with the rejected-credential response, which this change does not touch.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from conftest import stub_origin_verify

from adapters import config
from adapters.outbound.telemetry.reporting import Report
from domain.identity.session import IdentityError
from domain.shared_kernel.errors import DependencyError, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Generator

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-007")]

_NAME = "adapters.inbound.auth_handlers.handlers"
_OUTAGE = DependencyError(ErrorCode("AUTH", 99), "fixture provider outage.")
_EVENT: dict[str, Any] = {
    "queryStringParameters": {"state": "fixture-state", "code": "fixture-code"},
    "cookies": [],
}


class _Provider:
    """Raises one configured error from every provider call."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def exchange_code(self, _request: object) -> object:
        raise self.error

    def refresh(self, _token: str) -> object:
        raise self.error

    def revoke(self, _token: str) -> None:
        raise self.error


@dataclass(frozen=True)
class _Auth:
    module: ModuleType
    monkeypatch: pytest.MonkeyPatch


def _cleared_transaction(_policy: object) -> str:
    return "txn=; Max-Age=0"


def _cleared_session(_policy: object) -> tuple[str, ...]:
    return ("session=; Max-Age=0",)


def _transaction(_event: object) -> SimpleNamespace:
    return SimpleNamespace(code_verifier="fictional-verifier")


def _always(*_args: object) -> bool:
    return True


@pytest.fixture
def handlers(monkeypatch: pytest.MonkeyPatch) -> Generator[_Auth]:
    settings = SimpleNamespace(
        app_base_url="https://app.example.test",
        callback_redirect_uri="https://app.example.test/callback",
        cookie_policy=object(),
    )
    monkeypatch.setattr(config, "settings", lambda: settings)
    monkeypatch.setattr(config, "tracing_settings", lambda: SimpleNamespace(enabled=False))
    stub_origin_verify(monkeypatch)
    sys.modules.pop(_NAME, None)
    module = importlib.import_module(_NAME)
    refresh_cookie: str = module.REFRESH_COOKIE

    def cookies(_header: str) -> dict[str, str]:
        return {refresh_cookie: "fictional"}

    monkeypatch.setattr(module, "clear_transaction_cookie", _cleared_transaction)
    monkeypatch.setattr(module, "build_cleared_cookies", _cleared_session)
    monkeypatch.setattr(module, "_in_flight_transaction", _transaction)
    monkeypatch.setattr(module, "state_matches", _always)
    monkeypatch.setattr(module, "_csrf_is_valid", _always)
    monkeypatch.setattr(module, "parse_cookie_header", cookies)
    yield _Auth(module, monkeypatch)
    sys.modules.pop(_NAME, None)


def _answer(
    auth: _Auth, endpoint: str, error: Exception
) -> tuple[dict[str, Any], list[tuple[BaseException, str]]]:
    """Answer one endpoint with a provider that raises `error`."""
    reported: list[tuple[BaseException, str]] = []

    def capture(caught: BaseException, report: Report) -> None:
        reported.append((caught, report.operation))

    auth.monkeypatch.setattr(auth.module, "PROVIDER", _Provider(error))
    auth.monkeypatch.setattr(auth.module, "report_error", capture)
    answer: dict[str, Any] = getattr(auth.module, f"{endpoint}_handler")(_EVENT)
    return answer, reported


@pytest.mark.parametrize("endpoint", ["callback", "refresh"])
def test_a_provider_outage_is_reported_and_answered_as_a_rejected_credential(
    handlers: _Auth, endpoint: str
) -> None:
    rejected, rejected_reports = _answer(handlers, endpoint, IdentityError("fixture rejection."))
    outage, outage_reports = _answer(handlers, endpoint, _OUTAGE)
    assert outage == rejected
    assert rejected_reports == []
    assert outage_reports == [(_OUTAGE, f"auth.{endpoint}")]


def test_a_failed_revocation_is_reported_and_logout_still_clears_the_session(
    handlers: _Auth,
) -> None:
    response, reported = _answer(handlers, "logout", _OUTAGE)
    assert response["statusCode"] == 200
    assert response["body"] == '{"revoked": false}'
    assert response["cookies"] == ["session=; Max-Age=0"]
    assert reported == [(_OUTAGE, "auth.logout")]
