# SPDX-License-Identifier: MIT-0
"""Tests for reading a payload-2.0 event and resolving its route.

Both modules are deliberately free of configuration imports, so they are testable
without a deployed environment. The handlers they feed are not — those resolve
settings at import and are verified in the integration tier, per ADR-014.

The recurring theme: nothing here defaults a missing value into a permissive one.
A malformed event must not become a GET, because GET is exempt from CSRF. An entitlement
reader grants only on its own claim reading exactly "true", so a wrong or absent claim can
never grant prompt administration or operational status.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest

from adapters.inbound.auth_handlers import events, routing
from domain.identity.session import IdentityError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-007")]


def _event(method: str = "GET", path: str = "/api/me", **extra: Any) -> dict[str, Any]:
    return {"requestContext": {"http": {"method": method, "path": path}}, **extra}


class TestHeaders:
    def test_names_are_lowercased(self) -> None:
        # API Gateway lowercases on most paths but not all, and a header read with
        # the wrong case reads as absent — which for CSRF means a skipped check.
        assert events.headers(_event(headers={"X-Adl-Csrf": "t"}))["x-adl-csrf"] == "t"

    def test_absent_headers_yield_empty(self) -> None:
        assert events.headers(_event()) == {}

    def test_a_non_dict_headers_field_yields_empty(self) -> None:
        assert events.headers(_event(headers="broken")) == {}


class TestQuery:
    def test_names_keep_their_case(self) -> None:
        # Query-parameter names are case-sensitive. Folding them would match a
        # parameter the provider never sent.
        assert events.query(_event(queryStringParameters={"State": "x"})) == {"State": "x"}

    def test_absent_query_yields_empty(self) -> None:
        assert events.query(_event()) == {}


class TestCookieHeader:
    def test_the_cookies_array_is_rejoined(self) -> None:
        rebuilt = events.cookie_header(_event(cookies=["adl_session=a", "adl_csrf=b"]))
        assert rebuilt == "adl_session=a; adl_csrf=b"

    def test_it_falls_back_to_the_raw_header(self) -> None:
        assert events.cookie_header(_event(headers={"Cookie": "adl_session=a"})) == "adl_session=a"

    def test_no_cookies_at_all_yields_empty(self) -> None:
        assert events.cookie_header(_event()) == ""


class TestMethodAndPath:
    def test_the_method_is_read(self) -> None:
        assert events.method(_event(method="POST")) == "POST"

    def test_an_absent_method_raises_rather_than_defaulting(self) -> None:
        # Defaulting to GET would exempt the request from CSRF on the strength of
        # a malformed event.
        with pytest.raises(IdentityError, match="method"):
            events.method({"requestContext": {"http": {"path": "/api/me"}}})

    def test_an_absent_path_raises(self) -> None:
        with pytest.raises(IdentityError, match="path"):
            events.path({"requestContext": {"http": {"method": "GET"}}})

    def test_a_missing_request_context_raises(self) -> None:
        with pytest.raises(IdentityError):
            events.method({})


class TestAuthorizerSubject:
    def test_the_verified_subject_is_read(self) -> None:
        event = _event(
            requestContext={
                "http": {"method": "GET", "path": "/api/me"},
                "authorizer": {"lambda": {"subject": "sub-123"}},
            }
        )
        assert events.authorizer_subject(event) == "sub-123"

    def test_an_absent_subject_raises(self) -> None:
        # The authorizer context is the only trustworthy identity on an authorized
        # route. Its absence means the route was reached without one.
        with pytest.raises(IdentityError, match="subject"):
            events.authorizer_subject(_event())

    def test_an_empty_subject_raises(self) -> None:
        event = _event(
            requestContext={
                "http": {"method": "GET", "path": "/api/me"},
                "authorizer": {"lambda": {"subject": ""}},
            }
        )
        with pytest.raises(IdentityError, match="subject"):
            events.authorizer_subject(event)


_PROMPTS = "can_manage_prompts"
_STATUS = "can_read_operational_status"
_READERS = [
    pytest.param(events.authorizer_can_manage_prompts, _PROMPTS, _STATUS, id="prompt-admin"),
    pytest.param(
        events.authorizer_can_read_operational_status, _STATUS, _PROMPTS, id="operational-status"
    ),
]
_Reader = Callable[[dict[str, Any]], bool]


def _authorized(context_json: str) -> dict[str, Any]:
    """Build an event whose authorizer context arrives JSON-decoded, as API Gateway sends it.

    Decoded strings are fresh objects, never the interned literal, so an identity comparison
    against "true" cannot pass for the equality the readers must make.
    """
    return _event(
        requestContext={
            "http": {"method": "GET", "path": "/api/me"},
            "authorizer": {"lambda": json.loads(context_json)},
        }
    )


class TestAuthorizerEntitlements:
    @pytest.mark.parametrize(("reader", "claim", "_other"), _READERS)
    def test_its_own_claim_true_grants(self, reader: _Reader, claim: str, _other: str) -> None:
        assert reader(_authorized(f'{{"subject": "sub-123", "{claim}": "true"}}')) is True

    @pytest.mark.parametrize(("reader", "claim", "_other"), _READERS)
    def test_its_own_claim_false_does_not_grant(
        self, reader: _Reader, claim: str, _other: str
    ) -> None:
        assert reader(_authorized(f'{{"subject": "sub-123", "{claim}": "false"}}')) is False

    @pytest.mark.parametrize(("reader", "_claim", "_other"), _READERS)
    def test_an_absent_claim_does_not_grant(
        self, reader: _Reader, _claim: str, _other: str
    ) -> None:
        assert reader(_authorized('{"subject": "sub-123"}')) is False

    @pytest.mark.parametrize(("reader", "_claim", "other"), _READERS)
    def test_the_other_entitlements_claim_does_not_grant(
        self, reader: _Reader, _claim: str, other: str
    ) -> None:
        # Holding one entitlement must never grant the other.
        assert reader(_authorized(f'{{"subject": "sub-123", "{other}": "true"}}')) is False

    @pytest.mark.parametrize(("reader", "_claim", "_other"), _READERS)
    def test_an_event_without_an_authorizer_context_raises(
        self, reader: _Reader, _claim: str, _other: str
    ) -> None:
        # A malformed event is refused, never read as a plain "not entitled".
        with pytest.raises(IdentityError, match="entitlement"):
            reader(_event())


class TestRouting:
    @pytest.mark.parametrize(
        ("method", "path", "expected"),
        [
            ("GET", "/api/auth/login", routing.LOGIN),
            ("GET", "/api/auth/callback", routing.CALLBACK),
            ("POST", "/api/auth/refresh", routing.REFRESH),
            ("POST", "/api/auth/logout", routing.LOGOUT),
            ("GET", "/api/me", routing.SESSION),
        ],
    )
    def test_each_route_resolves(self, method: str, path: str, expected: str) -> None:
        assert routing.resolve(_event(method=method, path=path)) == expected

    def test_an_unknown_path_is_not_guessed_at(self) -> None:
        with pytest.raises(IdentityError, match="No route"):
            routing.resolve(_event(path="/api/auth/anything-else"))

    def test_the_method_is_part_of_the_key(self) -> None:
        # POST /auth/login must not reach the login handler. A GET-only endpoint
        # answering a POST would bypass the CSRF rules written for mutations.
        with pytest.raises(IdentityError, match="No route"):
            routing.resolve(_event(method="POST", path="/api/auth/login"))

    def test_a_prefix_does_not_match(self) -> None:
        with pytest.raises(IdentityError, match="No route"):
            routing.resolve(_event(path="/api/auth/login/extra"))


class TestUnauthenticatedSet:
    def test_it_is_exactly_the_four_session_lifecycle_routes(self) -> None:
        """Pinned by value, because this set decides which routes are unguarded.

        Adding an operation here silently removes the authorizer from it. `/me` is
        the one route that must stay out.
        """
        assert (
            frozenset({routing.LOGIN, routing.CALLBACK, routing.REFRESH, routing.LOGOUT})
            == routing.UNAUTHENTICATED
        )
        assert routing.SESSION not in routing.UNAUTHENTICATED

    def test_every_unauthenticated_operation_is_a_real_route(self) -> None:
        assert set(routing.ROUTES.values()) >= routing.UNAUTHENTICATED
