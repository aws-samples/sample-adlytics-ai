# SPDX-License-Identifier: MIT-0
"""ADR-078 origin-verify: the comparison, the cache rule, and both checks that make it first.

Every value here is fictional. The authorizer and the auth handler are imported with their
cold-start read stubbed, the way the other auth adapter tests import them.
"""

from __future__ import annotations

import importlib
import inspect
import json
import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest
from conftest import EDGE_ORIGIN_VALUE, stub_origin_verify

from adapters import config
from adapters.inbound.auth_handlers.origin_verify import (
    REFUSED_CATEGORY,
    OriginVerifier,
    OriginVerifyUnavailableError,
)
from domain.identity.origin_verify import (
    ORIGIN_VERIFY_HEADER,
    OriginVerifyValues,
    origin_matches,
    parse_origin_verify_values,
)
from domain.identity.session import IdentityError
from domain.shared_kernel.errors import InvariantError

if TYPE_CHECKING:
    from collections.abc import Generator

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-078")]

CURRENT = "fictional-current-value"
PREVIOUS = "fictional-previous-value"


def _document(current: str, previous: str | None = None) -> str:
    body = {"current": current} if previous is None else {"current": current, "previous": previous}
    return json.dumps(body)


class TestComparison:
    values = OriginVerifyValues(current=CURRENT, previous=PREVIOUS)

    @pytest.mark.parametrize("presented", [CURRENT, PREVIOUS])
    def test_accepts_current_and_previous(self, presented: str) -> None:
        assert origin_matches(presented, self.values)

    @pytest.mark.parametrize(
        "presented", ["", "fictional-current-valu", CURRENT + "x", CURRENT.upper(), "valeur-é"]
    )
    def test_refuses_anything_else(self, presented: str) -> None:
        assert not origin_matches(presented, self.values)

    def test_an_empty_previous_is_never_a_match_target(self) -> None:
        assert not origin_matches("", OriginVerifyValues(current=CURRENT))

    def test_never_renders_a_value(self) -> None:
        assert CURRENT not in repr(self.values)
        assert PREVIOUS not in repr(self.values)


class TestParse:
    def test_reads_current_and_optional_previous(self) -> None:
        assert parse_origin_verify_values(_document(CURRENT)) == OriginVerifyValues(CURRENT)
        parsed = parse_origin_verify_values(_document(CURRENT, PREVIOUS))
        assert (parsed.current, parsed.previous) == (CURRENT, PREVIOUS)

    @pytest.mark.parametrize(
        "document",
        [
            "not json",
            "[]",
            "{}",
            '{"current": ""}',
            '{"current": 7}',
            '{"current": "a", "previous": 1}',
        ],
    )
    def test_refuses_an_unusable_document_without_echoing_it(self, document: str) -> None:
        with pytest.raises(IdentityError) as raised:
            parse_origin_verify_values(document)
        assert document not in str(raised.value)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Secret:
    """A secret whose document the test rewrites, counting reads."""

    def __init__(self, document: str) -> None:
        self.document = document
        self.reads = 0
        self.fail = False

    def __call__(self, _secret_id: str) -> str:
        self.reads += 1
        if self.fail:
            raise RuntimeError("secrets manager unavailable")
        return self.document


def _verifier(secret: _Secret, clock: _Clock) -> OriginVerifier:
    settings = config.OriginVerifySettings("adlytics/dev/edge/origin-verify", 300, 60)
    return OriginVerifier(settings, secret, clock)


class TestCacheRule:
    def test_reads_once_at_cold_start_and_not_per_request(self) -> None:
        secret, clock = _Secret(_document(CURRENT)), _Clock()
        verifier = _verifier(secret, clock)
        for _ in range(5):
            assert verifier.verify(CURRENT)
        assert secret.reads == 1

    def test_meets_a_rotated_value_by_rereading_once(self) -> None:
        """Rotation steps 1-3: a warm container accepts the new value without refusing it."""
        secret, clock = _Secret(_document(CURRENT)), _Clock()
        verifier = _verifier(secret, clock)
        secret.document = _document("fictional-next-value", CURRENT)
        clock.now += 61
        assert verifier.verify("fictional-next-value")
        assert verifier.verify(CURRENT)
        assert secret.reads == 2

    def test_rereads_at_most_once_per_interval_however_many_wrong_values_arrive(self) -> None:
        secret, clock = _Secret(_document(CURRENT)), _Clock()
        verifier = _verifier(secret, clock)
        clock.now += 61
        for attempt in range(50):
            assert not verifier.verify(f"fictional-wrong-{attempt}")
        assert secret.reads == 2
        clock.now += 59
        assert not verifier.verify("fictional-wrong")
        assert secret.reads == 2
        clock.now += 1
        assert not verifier.verify("fictional-wrong")
        assert secret.reads == 3

    def test_a_dropped_previous_value_stops_being_accepted_by_the_maximum_age(self) -> None:
        """Rotation step 4: after the drop, the old value is refused once the cache has aged out."""
        secret, clock = _Secret(_document(CURRENT, PREVIOUS)), _Clock()
        verifier = _verifier(secret, clock)
        secret.document = _document(CURRENT)
        clock.now += 299
        assert verifier.verify(PREVIOUS)
        clock.now += 1
        assert not verifier.verify(PREVIOUS)
        assert verifier.verify(CURRENT)

    def test_a_failed_read_refuses_rather_than_serving_an_expired_value(self) -> None:
        secret, clock = _Secret(_document(CURRENT)), _Clock()
        verifier = _verifier(secret, clock)
        secret.fail = True
        clock.now += 300
        with pytest.raises(RuntimeError):
            verifier.verify(CURRENT)
        # Rate-limited even while failing: no read storm, and still refused, as unavailable rather
        # than as a wrong value the direct-caller alarm would count.
        with pytest.raises(OriginVerifyUnavailableError):
            verifier.verify(CURRENT)
        assert secret.reads == 2
        secret.fail = False
        clock.now += 60
        assert verifier.verify(CURRENT)

    def test_an_unreadable_secret_fails_the_cold_start(self) -> None:
        with pytest.raises(IdentityError):
            _verifier(_Secret("{}"), _Clock())


class TestSettings:
    source: ClassVar[dict[str, str]] = {
        "ADL_ORIGIN_VERIFY_SECRET_ID": "adlytics/dev/edge/origin-verify",
        "ADL_ORIGIN_VERIFY_MAX_CACHE_AGE_SECONDS": "300",
        "ADL_ORIGIN_VERIFY_REREAD_INTERVAL_SECONDS": "60",
    }

    def test_reads_a_complete_configuration(self) -> None:
        assert config.load_origin_verify_settings(self.source) == config.OriginVerifySettings(
            "adlytics/dev/edge/origin-verify", 300, 60
        )

    @pytest.mark.parametrize(
        "override",
        [
            {"ADL_ORIGIN_VERIFY_SECRET_ID": ""},
            {"ADL_ORIGIN_VERIFY_MAX_CACHE_AGE_SECONDS": ""},
            {"ADL_ORIGIN_VERIFY_REREAD_INTERVAL_SECONDS": "300"},
            {"ADL_ORIGIN_VERIFY_REREAD_INTERVAL_SECONDS": "0"},
            {"ADL_ORIGIN_VERIFY_MAX_CACHE_AGE_SECONDS": "five"},
        ],
    )
    def test_refuses_to_start_on_any_gap(self, override: dict[str, str]) -> None:
        with pytest.raises(InvariantError):
            config.load_origin_verify_settings({**self.source, **override})


class _Log:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def warning(self, _message: str, *, extra: dict[str, Any]) -> None:
        self.records.append(extra)


@dataclass
class _Harness:
    """Both auth modules, imported with the edge value stubbed, and their refusal log."""

    authorizer: ModuleType
    handlers: ModuleType
    monkeypatch: pytest.MonkeyPatch
    log: _Log


def _discard(*_args: object) -> None:
    return None


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Generator[_Harness]:
    settings = SimpleNamespace()
    monkeypatch.setattr(config, "settings", lambda: settings)
    monkeypatch.setattr(config, "settings_without_client_secret", lambda: settings)
    monkeypatch.setattr(config, "authorizer_public_client_id", lambda: "public-client-test")
    monkeypatch.setattr(config, "tracing_settings", lambda: SimpleNamespace(enabled=False))
    stub_origin_verify(monkeypatch)
    names = (
        "adapters.inbound.auth_handlers.authorizer",
        "adapters.inbound.auth_handlers.handlers",
    )
    for name in names:
        sys.modules.pop(name, None)
    authorizer, handlers = (importlib.import_module(name) for name in names)
    log = _Log()
    monkeypatch.setattr(authorizer, "LOGGER", log)
    monkeypatch.setattr(handlers, "LOGGER", log)
    monkeypatch.setattr(handlers, "report_operation", _discard)
    yield _Harness(authorizer, handlers, monkeypatch, log)
    for name in names:
        sys.modules.pop(name, None)


def _event(value: str | None, route: tuple[str, str] = ("GET", "/api/me")) -> dict[str, Any]:
    method, path = route
    headers = {} if value is None else {ORIGIN_VERIFY_HEADER: value}
    return {
        "routeKey": f"{method} {path}",
        "headers": headers,
        "requestContext": {
            "requestId": "fictional-api-request-1",
            "http": {"method": method, "path": path},
        },
    }


class _Errors:
    """Records what `report_error` was given."""

    def __init__(self) -> None:
        self.seen: list[BaseException] = []

    def record(self, error: BaseException, _report: object) -> None:
        self.seen.append(error)


class _Unavailable:
    """A verifier whose secret cannot be read."""

    def verify(self, _presented: str) -> bool:
        raise OriginVerifyUnavailableError


def _must_not_run(_event: object) -> dict[str, Any]:
    raise AssertionError("a check ran for a request not from the edge")


class TestAuthorizer:
    @pytest.mark.parametrize("value", [None, "", "fictional-wrong-value"])
    def test_refuses_before_any_other_check(self, harness: _Harness, value: str | None) -> None:
        harness.monkeypatch.setattr(harness.authorizer, "_to_request", _must_not_run)
        handler = inspect.unwrap(harness.authorizer.handler)
        assert handler(_event(value), object()) == {"isAuthorized": False}
        assert harness.log.records == [
            {
                "category": REFUSED_CATEGORY,
                "component": "authorizer",
                "api_request_id": "fictional-api-request-1",
            }
        ]
        assert "fictional-wrong-value" not in repr(harness.log.records)

    def test_an_unreadable_secret_denies_as_an_error_not_a_refusal(self, harness: _Harness) -> None:
        errors = _Errors()
        harness.monkeypatch.setattr(harness.authorizer, "ORIGIN_VERIFIER", _Unavailable())
        harness.monkeypatch.setattr(harness.authorizer, "_to_request", _must_not_run)
        harness.monkeypatch.setattr(harness.authorizer, "report_error", errors.record)
        handler = inspect.unwrap(harness.authorizer.handler)
        assert handler(_event(EDGE_ORIGIN_VALUE), object()) == {"isAuthorized": False}
        assert [type(error) for error in errors.seen] == [OriginVerifyUnavailableError]
        assert harness.log.records == []

    def test_passes_the_edge_value_on_to_the_credential_check(self, harness: _Harness) -> None:
        reached: list[object] = []

        def credential_check(event: object) -> object:
            reached.append(event)
            raise IdentityError("no credential")

        harness.monkeypatch.setattr(harness.authorizer, "_to_request", credential_check)
        harness.monkeypatch.setattr(harness.authorizer, "report_denial", _discard)
        handler = inspect.unwrap(harness.authorizer.handler)
        assert handler(_event(EDGE_ORIGIN_VALUE), object()) == {"isAuthorized": False}
        assert len(reached) == 1
        assert harness.log.records == []


AUTH_ROUTES = [
    ("GET", "/api/auth/login"),
    ("GET", "/api/auth/callback"),
    ("POST", "/api/auth/refresh"),
    ("POST", "/api/auth/logout"),
]


class TestAuthRoutes:
    @pytest.mark.parametrize("route", AUTH_ROUTES)
    @pytest.mark.parametrize("value", [None, "fictional-wrong-value"])
    def test_each_unauthenticated_route_refuses_without_the_edge_value(
        self, harness: _Harness, route: tuple[str, str], value: str | None
    ) -> None:
        for operation in harness.handlers.routing.UNAUTHENTICATED:
            harness.monkeypatch.setitem(harness.handlers._DISPATCH, operation, _must_not_run)
        response = inspect.unwrap(harness.handlers.handler)(_event(value, route), object())
        assert response["statusCode"] == 403
        assert json.loads(response["body"]) == {"error": "forbidden"}
        assert [record["category"] for record in harness.log.records] == [REFUSED_CATEGORY]

    @pytest.mark.parametrize("route", AUTH_ROUTES)
    def test_each_unauthenticated_route_dispatches_with_it(
        self, harness: _Harness, route: tuple[str, str]
    ) -> None:
        done: dict[str, Any] = {"statusCode": 204}

        def dispatch(_event: object) -> dict[str, Any]:
            return done

        for operation in harness.handlers.routing.UNAUTHENTICATED:
            harness.monkeypatch.setitem(harness.handlers._DISPATCH, operation, dispatch)
        handler = inspect.unwrap(harness.handlers.handler)
        assert handler(_event(EDGE_ORIGIN_VALUE, route), object()) is done

    @pytest.mark.parametrize("route", AUTH_ROUTES)
    def test_an_unreadable_secret_refuses_as_an_error_not_a_refusal(
        self, harness: _Harness, route: tuple[str, str]
    ) -> None:
        errors = _Errors()
        harness.monkeypatch.setattr(harness.handlers, "ORIGIN_VERIFIER", _Unavailable())
        harness.monkeypatch.setattr(harness.handlers, "report_error", errors.record)
        for operation in harness.handlers.routing.UNAUTHENTICATED:
            harness.monkeypatch.setitem(harness.handlers._DISPATCH, operation, _must_not_run)
        response = inspect.unwrap(harness.handlers.handler)(
            _event(EDGE_ORIGIN_VALUE, route), object()
        )
        assert response["statusCode"] == 403
        assert [type(error) for error in errors.seen] == [OriginVerifyUnavailableError]
        assert harness.log.records == []

    def test_the_authorized_session_route_is_left_to_the_authorizer(
        self, harness: _Harness
    ) -> None:
        done: dict[str, Any] = {"statusCode": 200}

        def dispatch(_event: object) -> dict[str, Any]:
            return done

        harness.monkeypatch.setitem(
            harness.handlers._DISPATCH, harness.handlers.routing.SESSION, dispatch
        )
        assert inspect.unwrap(harness.handlers.handler)(_event(None), object()) is done


def test_the_secret_read_makes_exactly_one_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside the authorizer's 5 s: botocore counts `max_attempts` as retries, so this is total."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    config.origin_verify_client.cache_clear()
    try:
        client = config.origin_verify_client()
        settings = cast("Any", client.meta.config)
        assert settings.retries["total_max_attempts"] == 1
        assert (settings.connect_timeout, settings.read_timeout) == (1, 1)
    finally:
        config.origin_verify_client.cache_clear()
