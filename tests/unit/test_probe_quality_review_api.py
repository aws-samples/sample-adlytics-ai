# SPDX-License-Identifier: MIT-0
"""The review API acceptance matrix: credential slots, refusal semantics, and target refusal."""

from __future__ import annotations

import json
import ssl
import sys
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_quality_review_api as probe  # noqa: E402

from ports.outbound.audit import MAX_TURNS_WINDOW  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-078")]


def test_a_cookie_credential_carries_only_the_session_and_csrf_pair() -> None:
    headers = probe._Credential("cookie", cookie="session-value").headers()  # pyright: ignore[reportPrivateUsage]
    assert "Authorization" not in headers
    assert headers["Cookie"] == "adl_session=session-value; adl_csrf=quality-review-validation"
    assert headers["x-adl-csrf"] == "quality-review-validation"


def test_a_bearer_with_an_unrelated_cookie_carries_no_session_cookie() -> None:
    headers = probe._Credential(  # pyright: ignore[reportPrivateUsage]
        "bearer+unrelated", bearer="bearer-value", extra_cookie="theme=validation"
    ).headers()
    assert headers["Authorization"] == "Bearer bearer-value"
    assert headers["Cookie"] == "theme=validation"
    assert "adl_session" not in headers["Cookie"]


def test_neither_credential_sends_no_cookie_and_no_authorization() -> None:
    headers = probe._Credential("none").headers()  # pyright: ignore[reportPrivateUsage]
    assert "Cookie" not in headers
    assert "Authorization" not in headers


@pytest.mark.parametrize(
    ("observed", "passed"), [(401, True), (403, True), (404, False), (200, False)]
)
def test_a_refusal_is_either_the_absent_identity_or_the_authorizer_denial(
    observed: int, passed: bool
) -> None:
    matrix = probe._Matrix(checks=[])  # pyright: ignore[reportPrivateUsage]
    matrix.expect("case", route="turns", credential="none", expected="refused", observed=observed)
    assert matrix.passed is passed
    assert set(matrix.checks[0]) == {
        "check",
        "route",
        "credential",
        "expected",
        "observed",
        "passed",
    }


def test_main_refuses_a_non_dev_target_before_any_aws_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = probe.main(
        [
            "--profile",
            "fictional",
            "--environment",
            "prod",
            "--region",
            "us-east-1",
            "--expected-account",
            "000000000000",
        ]
    )
    assert code == 2
    assert "approved dev region" in capsys.readouterr().out


@pytest.mark.parametrize("route", ["/api/quality-review/turn", "/api/quality-review/turns"])
def test_every_path_variant_differs_from_the_canonical_route(route: str) -> None:
    variants = probe._path_variants(route)  # pyright: ignore[reportPrivateUsage]

    names = {name for name, _, _ in variants}
    assert {
        "trailing_slash",
        "prefix_parent_segment",
        "upper_leaf",
        "double_slash_prefix",
        "dot_segment",
        "parent_segment",
        "encoded_slash",
        "wrong_method",
    } <= names
    assert all((method, path) != ("POST", route) for _, method, path in variants), (
        "a variant equal to the canonical route would measure nothing"
    )


class _Response:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text

    def read(self, _limit: int) -> bytes:
        return self._text.encode()


_SENT: list[tuple[str, str]] = []
_CONNECTS: list[tuple[tuple[object, ...], dict[str, object]]] = []


class _Connection:
    def __init__(self, response: _Response) -> None:
        self._response = response

    def request(self, method: str, path: str, **_kwargs: object) -> None:
        _SENT.append((method, path))

    def getresponse(self) -> _Response:
        return self._response

    def close(self) -> None:
        return None


_CASES = (
    (200, '{"schema_version":"QualityTurnReview.v1"}', True),
    (200, '{"schema_version":"QualityTurnsPage.v1","turns":[]}', True),
    (200, "<!doctype html><html></html>", False),
    (200, '{"schema_version":"SomethingElse"}', False),
    (403, '{"message":"Forbidden"}', False),
    (404, "", False),
)


@pytest.mark.parametrize("case", _CASES)
def test_only_a_review_document_counts_as_reaching_the_review_function(
    monkeypatch: pytest.MonkeyPatch, case: tuple[int, str, bool]
) -> None:
    """A static-site fallback that answers 200 with the app shell is not access."""
    status, text, reached = case

    observed = _reach_review(monkeypatch, status, text)

    assert observed == (status, reached)
    assert _SENT == [("POST", "/api/quality-review/./turn")]
    assert [(args, kwargs["timeout"]) for args, kwargs in _CONNECTS] == [
        (("review.example.test", 443), 30)
    ]


def _reach_review(monkeypatch: pytest.MonkeyPatch, status: int, text: str) -> tuple[int, bool]:
    """Send one raw variant through a recording HTTPSConnection fake."""
    _SENT.clear()
    _CONNECTS.clear()

    def connect(*args: object, **kwargs: object) -> _Connection:
        _CONNECTS.append((args, kwargs))
        return _Connection(_Response(status, text))

    monkeypatch.setattr(probe.http.client, "HTTPSConnection", connect)
    return probe._raw_reached_review(  # pyright: ignore[reportPrivateUsage]
        "https://review.example.test",
        method="POST",
        path="/api/quality-review/./turn",
        credential=probe._Credential("bearer", bearer="bearer-value"),  # pyright: ignore[reportPrivateUsage]
        body={"selector": {"audit_id": "fictional"}},
    )


def test_the_raw_connection_states_certificate_and_hostname_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TLS context is explicit in code, so removing it fails here (A-HTTPS-CONNECTION-001)."""
    _reach_review(monkeypatch, 200, '{"schema_version":"QualityTurnReview.v1"}')

    assert len(_CONNECTS) == 1
    _args, kwargs = _CONNECTS[0]
    assert "context" in kwargs
    context = kwargs["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def _record(route_key: str, **fields: str) -> str:
    """Render one access-log record; `fields` override the empty review keys."""
    record = {
        "requestId": "r",
        "routeKey": route_key,
        "status": "200",
        "reviewer": "-",
        "attribution": "-",
        "authorizerError": "-",
        "responseLatency": "12",
        "integrationLatency": "10",
    }
    return json.dumps(record | fields)


def _sent(name: str, route_key: str, expected: tuple[str, str] | None = None) -> probe._Sent:  # pyright: ignore[reportPrivateUsage]
    """Build a request whose record must carry `expected` (reviewer, attribution), else empties."""
    reviewer, attribution = expected or (None, None)
    return probe._Sent(  # pyright: ignore[reportPrivateUsage]
        name, route_key, "200", 200, f"{name}-id", reviewer, attribution
    )


_TURNS_KEY = "POST /api/quality-review/turns"


def _failed(checks: list[dict[str, object]]) -> set[object]:
    return {check["check"] for check in checks if not check["passed"]}


@pytest.mark.covers("ADR-081")
def test_access_log_checks_pass_when_each_record_matches_its_request() -> None:
    sent = [
        _sent("attributor", _TURNS_KEY, ("sub-a", "true")),
        _sent("me", "GET /api/me"),
    ]
    records = {
        "attributor-id": _record(_TURNS_KEY, reviewer="sub-a", attribution="true"),
        "me-id": _record("GET /api/me"),
    }
    checks = probe.access_log_checks(sent, records, ("marker",))
    assert _failed(checks) == set()
    assert {"check": "access_log_empty_rendering", "passed": True, "dash_values": 2} in checks


@pytest.mark.covers("ADR-081")
def test_access_log_checks_refuse_an_end_user_on_a_non_review_record() -> None:
    sent = [_sent("me", "GET /api/me")]
    records = {"me-id": _record("GET /api/me", reviewer="sub-a", attribution="false")}
    assert _failed(probe.access_log_checks(sent, records, ())) == {"access_log_me"}


@pytest.mark.covers("ADR-081")
def test_access_log_checks_refuse_a_wrong_flag_or_reviewer() -> None:
    sent = [_sent("reviewer", _TURNS_KEY, ("sub-r", "false"))]
    for reviewer, attribution in (("sub-r", "true"), ("sub-x", "false"), ("-", "-")):
        records = {"reviewer-id": _record(_TURNS_KEY, reviewer=reviewer, attribution=attribution)}
        assert _failed(probe.access_log_checks(sent, records, ())) == {"access_log_reviewer"}


@pytest.mark.covers("ADR-081")
def test_access_log_checks_refuse_extra_keys_markers_and_missing_records() -> None:
    sent = [_sent("me", "GET /api/me"), _sent("gone", _TURNS_KEY)]
    extra = json.loads(_record("GET /api/me", requestId="marker-value"))
    extra["subject"] = "sub-a"
    records = {"me-id": json.dumps(extra)}
    assert _failed(probe.access_log_checks(sent, records, ("marker-value",))) == {
        "access_log_records_found",
        "access_log_record_keys_exactly_the_format",
        "access_log_holds_no_selector_correlation_or_content",
        "access_log_gone",
    }


def test_the_walk_window_is_exactly_the_longest_the_api_accepts() -> None:
    """A window one second past 31 days is a 400; the probe once sent exactly that."""
    window = probe._window()  # pyright: ignore[reportPrivateUsage]
    span = datetime.fromisoformat(window["to"]) - datetime.fromisoformat(window["from"])
    assert span == MAX_TURNS_WINDOW


def _walk(*rows: tuple[str, str]) -> probe._Walk:  # pyright: ignore[reportPrivateUsage]
    counts = {"pages": 1, "rows": len(rows), "short_pages_with_cursor": 0, "principal_fields": 0}
    return probe._Walk(counts, rows)  # pyright: ignore[reportPrivateUsage]


_ALL = _walk(("a", "complete"), ("b", "refused"), ("c", "complete"))


@pytest.mark.covers("ADR-078")
def test_a_filtered_walk_passes_only_with_every_match_exactly_once() -> None:
    good = probe.filtered_walk_check(_ALL, _walk(("c", "complete"), ("a", "complete")), "complete")
    assert good["passed"] is True
    assert (good["matches"], good["rows"]) == (2, 2)
    for bad in (
        _walk(("a", "complete")),
        _walk(("a", "complete"), ("c", "complete"), ("a", "complete")),
        _walk(("a", "complete"), ("c", "complete"), ("b", "refused")),
    ):
        assert probe.filtered_walk_check(_ALL, bad, "complete")["passed"] is False
    # No match in the window proves nothing, so it fails rather than passing vacuously.
    vacuous = probe.filtered_walk_check(_walk(("b", "refused")), _walk(), "complete")
    assert vacuous["passed"] is False


@pytest.mark.covers("ADR-078")
def test_the_public_client_read_back_requires_code_openid_and_only_the_dev_flows() -> None:
    good = {
        "AllowedOAuthFlows": ["code"],
        "AllowedOAuthScopes": ["openid"],
        "ExplicitAuthFlows": ["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
    }
    assert probe.public_client_check(good)["passed"] is True
    for bad in (
        good | {"AllowedOAuthScopes": ["openid", "aws.cognito.signin.user.admin"]},
        good | {"ExplicitAuthFlows": [*good["ExplicitAuthFlows"], "ALLOW_USER_PASSWORD_AUTH"]},
        # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
        good | {"ClientSecret": "present"},
        good | {"AllowedOAuthFlows": ["code", "implicit"]},
    ):
        assert probe.public_client_check(bad)["passed"] is False
