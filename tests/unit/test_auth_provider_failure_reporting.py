# SPDX-License-Identifier: MIT-0
"""Auth endpoint answers: the session issued, the CSRF pair enforced, the outages reported.

Callback and refresh are the only endpoints that issue a session, and refresh and logout sit
outside the authorizer, so their own CSRF pair is all that stops a cross-site request (ADR-007).
A session answer with the wrong status, a cacheable one, or an accepted cross-site request is the
security failure prevented here; each answer is asserted whole, against literals.

A rejected credential and a provider outage share one response on the callback, refresh and logout
endpoints. Only the outage is ours, so only it reaches `report_error`; the response for each is
compared with the rejected-credential response, which this change does not touch.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import urllib.parse
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from conftest import stub_origin_verify

from adapters import config
from adapters.outbound.telemetry.reporting import Report
from domain.identity.cookies import CookiePolicy, CookieScope
from domain.identity.session import IdentityError
from domain.identity.tokens import TokenSet
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


# --- The answers themselves: real cookie policy, real CSRF pair, a provider that succeeds -------

_APP = "https://app.example.test"
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_JSON_NO_STORE = {"Content-Type": "application/json", **_NO_STORE}
_FORBIDDEN = {"statusCode": 403, "headers": _JSON_NO_STORE, "body": '{"error": "forbidden"}'}
_ROTATED_CSRF = "fictional-rotated-csrf"
_VERIFIER = "v" * 43  # RFC 7636's 43-character floor
_SESSION_COOKIES = ["adl_csrf=fictional-csrf", "adl_refresh=fictional-refresh"]
_SESSION = "adl_session={}; Path=/; Max-Age=3600; SameSite=Lax; Secure; HttpOnly"
_CSRF = f"adl_csrf={_ROTATED_CSRF}; Path=/; Max-Age=2592000; SameSite=Lax; Secure"
_REFRESH = "adl_refresh=fictional-refresh; Path=/; Max-Age=2592000; SameSite=Lax; Secure; HttpOnly"
_TXN_CLEARED = "adl_txn=; Path=/; Max-Age=0; SameSite=Lax; Secure; HttpOnly"


class _IssuingProvider:
    """Answers every provider call the way Cognito does when the credential is good."""

    def exchange_code(self, _request: object) -> TokenSet:
        return TokenSet(
            access_token="fictional-access", expires_in=3600, refresh_token="fictional-refresh"
        )

    def refresh(self, _token: str) -> TokenSet:
        # Cognito does not reissue a refresh token on refresh.
        return TokenSet(access_token="fictional-access-2", expires_in=3600)

    def revoke(self, _token: str) -> None:
        return None


@dataclass(frozen=True)
class _Issuing:
    module: ModuleType
    reported: list[str]


@pytest.fixture
def issuing(monkeypatch: pytest.MonkeyPatch) -> Generator[_Issuing]:
    settings = SimpleNamespace(
        app_base_url=_APP,
        callback_redirect_uri=f"{_APP}/api/auth/callback",
        cookie_policy=CookiePolicy(CookieScope.HOST_ONLY),
        refresh_cookie_max_age_seconds=2_592_000,
        cognito_client_id="fictional-client",
        cognito_authorize_url="https://auth.example.test/oauth2/authorize",
    )
    monkeypatch.setattr(config, "settings", lambda: settings)
    monkeypatch.setattr(config, "tracing_settings", lambda: SimpleNamespace(enabled=False))
    stub_origin_verify(monkeypatch)
    sys.modules.pop(_NAME, None)
    module = importlib.import_module(_NAME)
    reported: list[str] = []

    def capture(_caught: BaseException, report: Report) -> None:
        reported.append(report.operation)

    def discard(*_args: object) -> None:
        return None

    def rotated() -> str:
        return _ROTATED_CSRF

    monkeypatch.setattr(module, "PROVIDER", _IssuingProvider())
    monkeypatch.setattr(module, "new_csrf_token", rotated)
    monkeypatch.setattr(module, "report_error", capture)
    monkeypatch.setattr(module, "report_operation", discard)
    yield _Issuing(module, reported)
    sys.modules.pop(_NAME, None)


def _post(path: str, headers: dict[str, str], cookies: list[str]) -> dict[str, Any]:
    return {
        "requestContext": {"http": {"method": "POST", "path": path}},
        "headers": headers,
        "cookies": cookies,
    }


def _callback(cookies: list[str]) -> dict[str, Any]:
    return {
        "requestContext": {"http": {"method": "GET", "path": "/api/auth/callback"}},
        "queryStringParameters": {"state": "fictional-state", "code": "fictional-code"},
        "cookies": cookies,
    }


def _refresh(
    issuing: _Issuing, headers: dict[str, str], cookies: list[str] | None = None
) -> dict[str, Any]:
    sent = _SESSION_COOKIES if cookies is None else cookies
    event = _post("/api/auth/refresh", headers, sent)
    answer: dict[str, Any] = issuing.module.refresh_handler(event)
    return answer


def test_a_login_redirects_uncached_to_managed_login_with_a_transaction_cookie(
    issuing: _Issuing,
) -> None:
    event = {"requestContext": {"http": {"method": "GET", "path": "/api/auth/login"}}}
    answer: dict[str, Any] = issuing.module.login_handler(event)
    location: str = answer["headers"]["Location"]
    target = urllib.parse.urlsplit(location)
    query = urllib.parse.parse_qs(target.query)
    assert answer["statusCode"] == 302
    assert {name: answer["headers"][name] for name in _NO_STORE} == _NO_STORE
    assert f"{target.scheme}://{target.netloc}{target.path}" == (
        "https://auth.example.test/oauth2/authorize"
    )
    assert (query["client_id"], query["response_type"], query["code_challenge_method"]) == (
        ["fictional-client"],
        ["code"],
        ["S256"],
    )
    (cookie,) = answer["cookies"]
    assert cookie.startswith("adl_txn=")
    assert cookie.endswith("; Path=/; Max-Age=300; SameSite=Lax; Secure; HttpOnly")


def test_a_redeemed_callback_redirects_home_with_the_session_and_ends_the_transaction(
    issuing: _Issuing,
) -> None:
    event = _callback([f"adl_txn=fictional-state.{_VERIFIER}"])
    assert issuing.module.callback_handler(event) == {
        "statusCode": 302,
        "headers": {**_NO_STORE, "Location": _APP},
        "cookies": [_SESSION.format("fictional-access"), _CSRF, _REFRESH, _TXN_CLEARED],
    }


def test_a_refresh_rotates_the_session_and_csrf_and_keeps_the_refresh_cookie(
    issuing: _Issuing,
) -> None:
    answer = _refresh(issuing, {"x-adl-csrf": "fictional-csrf", "sec-fetch-site": "same-origin"})
    assert answer == {
        "statusCode": 200,
        "headers": _JSON_NO_STORE,
        # No adl_refresh: Cognito reissued none, so the valid one is left in place.
        "cookies": [_SESSION.format("fictional-access-2"), _CSRF],
        "body": "{}",
    }


def test_a_csrf_token_mismatch_is_refused_even_from_a_same_site_fetch(issuing: _Issuing) -> None:
    """Sec-Fetch-Site is the weaker check: it must never short-circuit the double-submit token."""
    answer = _refresh(issuing, {"x-adl-csrf": "fictional-other", "sec-fetch-site": "same-site"})
    assert answer == _FORBIDDEN


def test_a_refresh_with_neither_sec_fetch_site_nor_origin_is_refused(issuing: _Issuing) -> None:
    """An absent Origin is unknown provenance, never same-origin."""
    assert _refresh(issuing, {"x-adl-csrf": "fictional-csrf"}) == _FORBIDDEN


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("http://app.example.test", id="scheme-downgrade"),
        pytest.param("https://app.example.test.evil.example", id="suffixed-host"),
    ],
)
def test_a_refresh_from_another_origin_is_refused(issuing: _Issuing, origin: str) -> None:
    assert _refresh(issuing, {"x-adl-csrf": "fictional-csrf", "origin": origin}) == _FORBIDDEN


def test_a_refresh_from_the_app_origin_without_sec_fetch_site_is_accepted(
    issuing: _Issuing,
) -> None:
    # Built at run time: an Origin header arrives as a fresh string, never the interned literal
    # the settings hold, so the comparison must be equality rather than identity.
    origin = "".join(["https://app.", "example.test"])
    answer = _refresh(issuing, {"x-adl-csrf": "fictional-csrf", "origin": origin})
    assert answer["statusCode"] == 200


def test_a_refresh_without_a_refresh_cookie_is_unauthenticated(issuing: _Issuing) -> None:
    answer = _refresh(
        issuing,
        {"x-adl-csrf": "fictional-csrf", "sec-fetch-site": "same-origin"},
        ["adl_csrf=fictional-csrf"],
    )
    assert answer == {
        "statusCode": 401,
        "headers": _JSON_NO_STORE,
        "body": '{"error": "unauthenticated"}',
    }


def test_a_logout_with_a_csrf_token_mismatch_is_forbidden(issuing: _Issuing) -> None:
    event = _post(
        "/api/auth/logout",
        {"x-adl-csrf": "fictional-other", "sec-fetch-site": "same-origin"},
        ["adl_csrf=fictional-csrf", "adl_refresh=fictional-refresh"],
    )
    assert issuing.module.logout_handler(event) == _FORBIDDEN


def test_a_callback_without_an_in_flight_login_redirects_to_the_failure_marker(
    issuing: _Issuing,
) -> None:
    assert issuing.module.callback_handler(_callback([])) == {
        "statusCode": 302,
        "headers": {"Location": f"{_APP}/?auth=failed", **_NO_STORE},
        "cookies": [_TXN_CLEARED],
    }


def test_an_unknown_auth_route_is_not_found(issuing: _Issuing) -> None:
    event = {"requestContext": {"http": {"method": "GET", "path": "/api/auth/anything-else"}}}
    answer = inspect.unwrap(issuing.module.handler)(event, object())
    not_found = {"statusCode": 404, "headers": _JSON_NO_STORE, "body": '{"error": "not_found"}'}
    assert answer == not_found


def test_an_endpoint_that_raises_answers_internal_and_is_reported(
    issuing: _Issuing, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_event: object) -> dict[str, Any]:
        raise RuntimeError("fictional handler fault")

    module = issuing.module
    monkeypatch.setitem(module._DISPATCH, module.routing.SESSION, broken)
    event = {"requestContext": {"http": {"method": "GET", "path": "/api/me"}}}
    answer = inspect.unwrap(module.handler)(event, object())
    assert answer == {"statusCode": 500, "headers": _JSON_NO_STORE, "body": '{"error": "internal"}'}
    assert issuing.reported == ["auth.session"]
