# SPDX-License-Identifier: MIT-0
"""Operator probes send their cookies only through the HTTPS boundary (H-VALIDATION-HTTPS-001).

Four probes used urllib, whose redirect handler copies a caller-set `Cookie` header onto an
https→http hop. They now send through `adapters.outbound.https.HttpsSession` with redirects off.
Each probe's module-level `_HTTP` is replaced by an `HttpsSession` that mounts a recording
adapter for both schemes, so the transport is replaced, never an AWS service. The adapter builds
real urllib3 responses through requests' own `HTTPAdapter.build_response`, so `Set-Cookie`
headers reach the probe exactly as they would from the network.

The last tests are the gate against recurrence: no tracked `.py` file under `validation/`, `src/`,
`scripts/` or `tests/` may use `urllib.request`, with no exception (O-TEST-TRANSPORT-001).
"Tracked" is the publication-boundary script's own enumeration, so both gates agree on what is
scanned.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from requests import PreparedRequest, Response
from requests.adapters import HTTPAdapter
from urllib3 import HTTPHeaderDict, HTTPResponse

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_operational_status as operational  # noqa: E402
from validation import probe_presentation_hydration as hydration  # noqa: E402
from validation import probe_prompt_registry as registry  # noqa: E402
from validation import ship_prompts as ship  # noqa: E402

from adapters.outbound.https import HttpsSession  # noqa: E402

pytestmark = pytest.mark.unit

ORIGIN = "https://review.example.test"
REFRESH_COOKIE = "adl_refresh=fictional-refresh; adl_csrf=fictional-csrf"
SESSION_COOKIE = "adl_session=fictional-session; Path=/"
ROTATED_CSRF_COOKIE = "adl_csrf=fictional-csrf-rotated; Path=/"
REGISTRY_REFRESH_REFUSED = "The refresh route returned no session cookie. Is ADL_REFRESH current?"


class _RecordingAdapter:
    """Answer every request with one scripted response, recording each URL and Cookie header."""

    def __init__(
        self,
        status: int,
        *,
        headers: list[tuple[str, str]] | None = None,
        body: bytes = b"",
    ) -> None:
        self.status = status
        self.headers = headers or []
        self.body = body
        self.sent: list[tuple[str, str | bytes | None]] = []

    def send(self, request: PreparedRequest, **kwargs: Any) -> Response:
        del kwargs
        self.sent.append((request.url or "", request.headers.get("Cookie")))
        raw = HTTPResponse(
            body=io.BytesIO(self.body),
            headers=HTTPHeaderDict(self.headers),
            status=self.status,
            preload_content=False,
            decode_content=False,
        )
        return HTTPAdapter().build_response(request, raw)

    def close(self) -> None:
        """No resources are opened by this adapter."""


def _install(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, adapter: _RecordingAdapter
) -> None:
    session = HttpsSession()
    session.mount("https://", cast("Any", adapter))
    session.mount("http://", cast("Any", adapter))
    monkeypatch.setattr(module, "_HTTP", session)


def _redirect_with_cookie() -> _RecordingAdapter:
    return _RecordingAdapter(
        302,
        headers=[("Location", "http://example.test/captured"), ("Set-Cookie", SESSION_COOKIE)],
    )


def _hydration_session() -> Any:
    return hydration.ApiSession(
        base_url=ORIGIN, refresh_token="fictional-refresh", csrf="fictional-csrf"
    )


# --- 1. Origin refusal, before any request ------------------------------------------------------


def _hydration_origin(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], str]:
    def from_env(raw: str) -> str:
        monkeypatch.setenv("ADL_BASE_URL", raw)
        monkeypatch.setenv("ADL_REFRESH", "fictional-refresh")
        monkeypatch.setenv("ADL_CSRF", "fictional-csrf")
        return hydration._session_from_env().base_url  # pyright: ignore[reportPrivateUsage]

    return from_env


_ORIGIN_CASES = {
    "operational_status": (
        operational,
        SystemExit,
        "ADL_BASE_URL must be an https URL with a host.",
    ),
    "presentation_hydration": (
        hydration,
        hydration.ProbeError,
        "ADL_BASE_URL must be an https URL",
    ),
    "prompt_registry": (registry, SystemExit, "ADL_BASE_URL must be https."),
    "ship_prompts": (
        ship,
        SystemExit,
        "Source validation/.env.local (ADL_BASE_URL https, ADL_REFRESH, ADL_CSRF).",
    ),
}


def _origin_entry(name: str, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], str]:
    entries: dict[str, Callable[[str], str]] = {
        "operational_status": operational._origin,  # pyright: ignore[reportPrivateUsage]
        "presentation_hydration": _hydration_origin(monkeypatch),
        "prompt_registry": registry._require_https,  # pyright: ignore[reportPrivateUsage]
        "ship_prompts": ship._origin,  # pyright: ignore[reportPrivateUsage]
    }
    return entries[name]


@pytest.mark.parametrize("raw", ["http://example.test", "https://"])
@pytest.mark.parametrize("name", sorted(_ORIGIN_CASES))
def test_a_non_https_origin_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, name: str, raw: str
) -> None:
    module, error_type, message = _ORIGIN_CASES[name]
    adapter = _RecordingAdapter(200)
    _install(monkeypatch, module, adapter)

    with pytest.raises(error_type) as excinfo:
        _origin_entry(name, monkeypatch)(raw)

    assert type(excinfo.value) is error_type
    assert str(excinfo.value) == message
    assert adapter.sent == []


@pytest.mark.parametrize("name", sorted(_ORIGIN_CASES))
def test_an_https_origin_is_accepted_without_its_trailing_slash(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    raw = f"{ORIGIN}/"
    # The registry probe's caller strips the slash before it validates (probe_prompt_registry.main).
    accepted = _origin_entry(name, monkeypatch)(
        raw.rstrip("/") if name == "prompt_registry" else raw
    )

    assert accepted == ORIGIN


class _StsReached(Exception):  # noqa: N818 - a control-flow sentinel, not an error
    pass


def _run_operational_main(monkeypatch: pytest.MonkeyPatch, origin: str) -> list[object]:
    sessions: list[object] = []

    def session(**kwargs: object) -> object:
        sessions.append(kwargs)
        raise _StsReached

    monkeypatch.setattr(operational, "_load_env", lambda: None)
    monkeypatch.setattr(operational.boto3, "Session", session)
    monkeypatch.setenv("ADL_BASE_URL", origin)
    monkeypatch.setenv("ADL_REFRESH", "fictional-refresh")
    monkeypatch.setenv("ADL_CSRF", "fictional-csrf")
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", "--profile", "fictional", "--env", "dev", "--expected-account", "111122223333"],
    )
    return sessions


def test_operational_status_checks_the_origin_before_any_aws_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _run_operational_main(monkeypatch, "http://example.test")

    with pytest.raises(SystemExit) as excinfo:
        operational.main()

    assert str(excinfo.value) == "ADL_BASE_URL must be an https URL with a host."
    assert sessions == []


def test_operational_status_reaches_aws_once_for_an_https_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _run_operational_main(monkeypatch, ORIGIN)

    with pytest.raises(_StsReached):
        operational.main()

    assert sessions == [{"profile_name": "fictional"}]


# --- 2. A redirect on refresh is refused before Set-Cookie is read --------------------------------


def _refresh_entry(name: str) -> Callable[[], object]:
    entries: dict[str, Callable[[], object]] = {
        "operational_status": lambda: operational._refreshed_cookie(  # pyright: ignore[reportPrivateUsage]
            ORIGIN, "fictional-refresh", "fictional-csrf"
        ),
        "presentation_hydration": lambda: hydration._refresh(  # pyright: ignore[reportPrivateUsage]
            _hydration_session()
        ),
        "prompt_registry": lambda: registry._refreshed_session(  # pyright: ignore[reportPrivateUsage]
            ORIGIN, "fictional-refresh", "fictional-csrf"
        ),
        "ship_prompts": lambda: ship._refreshed_session(  # pyright: ignore[reportPrivateUsage]
            ORIGIN, "fictional-refresh", "fictional-csrf"
        ),
    }
    return entries[name]


_REFRESH_REFUSALS = {
    "operational_status": (operational, SystemExit, "refresh did not issue a session cookie"),
    "presentation_hydration": (
        hydration,
        hydration.ProbeError,
        "session refresh was refused with status 302",
    ),
    "prompt_registry": (registry, SystemExit, REGISTRY_REFRESH_REFUSED),
    "ship_prompts": (ship, SystemExit, REGISTRY_REFRESH_REFUSED),
}


@pytest.mark.parametrize("name", sorted(_REFRESH_REFUSALS))
def test_a_refresh_redirect_carrying_a_session_cookie_is_refused(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    module, error_type, message = _REFRESH_REFUSALS[name]
    adapter = _redirect_with_cookie()
    _install(monkeypatch, module, adapter)

    with pytest.raises(error_type) as excinfo:
        _refresh_entry(name)()

    assert type(excinfo.value) is error_type
    assert str(excinfo.value) == message
    assert adapter.sent == [(f"{ORIGIN}/api/auth/refresh", REFRESH_COOKIE)]


# --- 3. Positive control: a 200 refresh issues the session as each probe expects ----------------


def _issued_cookies(*cookies: str) -> _RecordingAdapter:
    return _RecordingAdapter(200, headers=[("Set-Cookie", cookie) for cookie in cookies])


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("operational_status", "adl_session=fictional-session; adl_csrf=fictional-csrf"),
        ("prompt_registry", "fictional-session"),
        ("ship_prompts", "fictional-session"),
    ],
)
def test_a_200_refresh_returns_the_probes_session_shape(
    monkeypatch: pytest.MonkeyPatch, name: str, expected: str
) -> None:
    module, _error_type, _message = _REFRESH_REFUSALS[name]
    adapter = _issued_cookies(SESSION_COOKIE, ROTATED_CSRF_COOKIE)
    _install(monkeypatch, module, adapter)

    assert _refresh_entry(name)() == expected
    assert adapter.sent == [(f"{ORIGIN}/api/auth/refresh", REFRESH_COOKIE)]


@pytest.mark.parametrize(
    ("cookies", "csrf"),
    [
        ((SESSION_COOKIE, ROTATED_CSRF_COOKIE), "fictional-csrf-rotated"),
        ((SESSION_COOKIE,), "fictional-csrf"),
    ],
    ids=["rotated", "kept"],
)
def test_hydration_refresh_sets_the_session_and_keeps_the_csrf_rotation(
    monkeypatch: pytest.MonkeyPatch, cookies: tuple[str, ...], csrf: str
) -> None:
    adapter = _issued_cookies(*cookies)
    _install(monkeypatch, hydration, adapter)
    session = _hydration_session()

    hydration._refresh(session)  # pyright: ignore[reportPrivateUsage]

    assert (session.session_cookie, session.csrf) == ("fictional-session", csrf)
    assert adapter.sent == [(f"{ORIGIN}/api/auth/refresh", REFRESH_COOKIE)]


# --- 4. Data calls report a redirect instead of following it -------------------------------------


def test_a_data_call_redirect_is_returned_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _RecordingAdapter(302, headers=[("Location", "http://example.test/captured")])
    _install(monkeypatch, operational, adapter)

    observed = operational._get(ORIGIN, "adl_session=s", "/api/operations/data-flows")  # pyright: ignore[reportPrivateUsage]

    assert observed == (302, {})
    assert adapter.sent == [(f"{ORIGIN}/api/operations/data-flows", "adl_session=s")]


def test_a_data_call_returns_its_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _RecordingAdapter(
        200, headers=[("Content-Type", "application/json")], body=b'{"surfaces":[]}'
    )
    _install(monkeypatch, operational, adapter)

    observed = operational._get(ORIGIN, "adl_session=s", "/api/operations/data-flows")  # pyright: ignore[reportPrivateUsage]

    assert observed == (200, {"surfaces": []})


# --- 5. Gate against recurrence: no urllib.request under validation/, src/, scripts/ or tests/ ----

GATE_ROOTS = ("validation/", "src/", "scripts/", "tests/")
GATE_MESSAGE = "urllib.request is not the HTTPS boundary; use adapters.outbound.https.HttpsSession"


def _urllib_request_findings(path: str, source: str) -> list[str]:
    """Return one finding per import or attribute chain that reaches `urllib.request`."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source, filename=path)):
        if (
            (
                isinstance(node, ast.Import)
                and any(
                    alias.name == "urllib.request" or alias.name.startswith("urllib.request.")
                    for alias in node.names
                )
            )
            or (
                isinstance(node, ast.ImportFrom)
                and (
                    (node.module or "").startswith("urllib.request")
                    or (
                        node.module == "urllib"
                        and any(alias.name == "request" for alias in node.names)
                    )
                )
            )
            or (
                isinstance(node, ast.Attribute)
                and node.attr == "request"
                and isinstance(node.value, ast.Name)
                and node.value.id == "urllib"
            )
        ):
            lines.append(node.lineno)
    return [f"{path}:{line}: {GATE_MESSAGE}" for line in sorted(lines)]


@pytest.mark.parametrize("path", ["validation/x.py", "src/adapters/x.py", "tests/unit/x.py"])
@pytest.mark.parametrize(
    "source",
    [
        "import urllib.request",
        "from urllib.request import urlopen",
        "from urllib import request",
        "urllib.request.urlopen(target)",
    ],
    ids=["import", "from-module", "from-package", "attribute-chain"],
)
def test_each_urllib_request_spelling_is_refused(path: str, source: str) -> None:
    assert _urllib_request_findings(path, source) == [f"{path}:1: {GATE_MESSAGE}"]


@pytest.mark.parametrize("path", ["validation/x.py", "src/adapters/x.py", "tests/unit/x.py"])
def test_urllib_parse_is_allowed(path: str) -> None:
    assert _urllib_request_findings(path, "import urllib.parse") == []


def _tracked_python() -> list[str]:
    spec = importlib.util.spec_from_file_location(
        "check_publication_boundary", _ROOT / "scripts" / "check-publication-boundary.py"
    )
    assert spec is not None
    assert spec.loader is not None
    boundary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boundary)
    try:
        candidates = cast("list[str]", boundary._candidates())
    except OSError, subprocess.CalledProcessError:
        pytest.skip("git is unavailable, so tracked content is unknown")
    return [path for path in candidates if path.startswith(GATE_ROOTS) and path.endswith(".py")]


def test_no_tracked_source_under_the_gate_roots_uses_urllib_request() -> None:
    tracked = _tracked_python()

    # Non-vacuity: every root is enumerated, so an ignored-but-tracked root fails here.
    assert {
        "validation/ship_prompts.py",
        "validation/probe_operational_status.py",
        "src/adapters/outbound/https.py",
        "scripts/check-publication-boundary.py",
        "tests/integration/test_deployed_api.py",
    } <= set(tracked)
    findings = [
        finding
        for path in tracked
        for finding in _urllib_request_findings(path, (_ROOT / path).read_text())
    ]
    assert findings == []
