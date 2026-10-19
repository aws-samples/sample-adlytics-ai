# SPDX-License-Identifier: MIT-0
"""The deployed-API test helper sends exactly the cookies its caller names.

Several tests in `tests/integration/test_deployed_api.py` prove behaviour when no
Cookie header is sent at all. A session shared across calls would store the
`adl_txn` cookie from `/auth/login` and replay it on every later call, so those
tests would stop testing their precondition while still passing. These check
`call` across calls, which a check of independent calls cannot see.

`HttpsSession` in the loaded module is replaced by a subclass that mounts a
recording adapter, so only the transport is replaced, never an AWS service. The
adapter builds real urllib3 responses through requests' own
`HTTPAdapter.build_response`, so `Set-Cookie` reaches the session exactly as it
would from the network.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from http.client import HTTPMessage
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
import requests
from requests import PreparedRequest, Response
from requests.adapters import HTTPAdapter
from urllib3 import HTTPHeaderDict, HTTPResponse

from adapters.outbound.https import HttpsSession, InsecureUrlError

pytestmark = pytest.mark.unit

_MODULE = Path(__file__).resolve().parents[1] / "integration" / "test_deployed_api.py"
ORIGIN = "https://app.example.test"
TXN_COOKIE = "adl_txn=fictional-txn; Path=/; Secure; HttpOnly; SameSite=Lax"


class _RecordingAdapter:
    """Answer every request with a 302 that sets `adl_txn`, recording each Cookie header."""

    def __init__(self) -> None:
        self.sent: list[str | bytes | None] = []

    def send(self, request: PreparedRequest, **kwargs: Any) -> Response:
        del kwargs
        self.sent.append(request.headers.get("Cookie"))
        headers = [("Location", "https://auth.example.test/authorize"), ("Set-Cookie", TXN_COOKIE)]
        raw = HTTPResponse(
            body=io.BytesIO(b""),
            headers=HTTPHeaderDict(headers),
            status=302,
            preload_content=False,
            decode_content=False,
        )
        # requests fills a session's cookie jar only from the http.client response
        # urllib3 keeps on a real connection; without it no jar would ever fill.
        message = HTTPMessage()
        for name, value in headers:
            message[name] = value
        original = SimpleNamespace(msg=message, isclosed=lambda: True)
        raw._original_response = cast("Any", original)  # pyright: ignore[reportPrivateUsage]
        return HTTPAdapter().build_response(request, raw)

    def close(self) -> None:
        """No resources are opened by this adapter."""


class _ScriptedAdapter:
    """Answer each URL from a script of (status, headers, body), recording what was sent."""

    def __init__(self, script: dict[str, tuple[int, list[tuple[str, str]], bytes]]) -> None:
        self.script = script
        self.sent: list[tuple[str, str | bytes | None]] = []

    def send(self, request: PreparedRequest, **kwargs: Any) -> Response:
        del kwargs
        url = request.url or ""
        self.sent.append((url, request.headers.get("Authorization")))
        status, headers, body = self.script[url]
        raw = HTTPResponse(
            body=io.BytesIO(body),
            headers=HTTPHeaderDict(headers),
            status=status,
            preload_content=False,
            decode_content=False,
        )
        return HTTPAdapter().build_response(request, raw)

    def close(self) -> None:
        """No resources are opened by this adapter."""


def _load(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    name = "deployed_api_for_transport_test"
    spec = importlib.util.spec_from_file_location(name, _MODULE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Its slotted dataclass resolves its own module through sys.modules.
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, _RecordingAdapter]:
    module = _load(monkeypatch)
    adapter = _RecordingAdapter()

    class _Recorded(HttpsSession):
        def __init__(self) -> None:
            super().__init__()
            self.mount("https://", cast("Any", adapter))

    monkeypatch.setattr(module, "HttpsSession", _Recorded)
    return module, adapter


def test_a_call_with_no_cookie_sends_none_after_a_call_that_received_one(
    recorded: tuple[ModuleType, _RecordingAdapter],
) -> None:
    module, adapter = recorded
    login = module.call(f"{ORIGIN}/api/auth/login")
    assert login.cookie("adl_txn")
    module.call(f"{ORIGIN}/api/me")
    module.call(f"{ORIGIN}/api/auth/refresh", method="POST")
    assert adapter.sent == [None, None, None]


def test_an_explicit_cookie_is_sent_as_named_and_not_carried_forward(
    recorded: tuple[ModuleType, _RecordingAdapter],
) -> None:
    module, adapter = recorded
    module.call(f"{ORIGIN}/api/me", headers=module.cookie_header(adl_session="fictional"))
    module.call(f"{ORIGIN}/api/me")
    assert adapter.sent == ["adl_session=fictional", None]


# --- read_body: HttpsSession, raise_for_status, and no environment trust (O-TEST-TRANSPORT-001) ---

_NEXT = f"{ORIGIN}/next"


def _scripted(
    monkeypatch: pytest.MonkeyPatch, script: dict[str, tuple[int, list[tuple[str, str]], bytes]]
) -> tuple[ModuleType, _ScriptedAdapter, list[HttpsSession]]:
    module = _load(monkeypatch)
    adapter = _ScriptedAdapter(script)
    sessions: list[HttpsSession] = []

    class _Scripted(HttpsSession):
        def __init__(self) -> None:
            super().__init__()
            self.mount("https://", cast("Any", adapter))
            self.mount("http://", cast("Any", adapter))
            sessions.append(self)

    monkeypatch.setattr(module, "HttpsSession", _Scripted)
    return module, adapter, sessions


def test_read_body_follows_an_https_redirect_and_returns_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, adapter, _ = _scripted(
        monkeypatch,
        {f"{ORIGIN}/": (302, [("Location", _NEXT)], b""), _NEXT: (200, [], b"ok")},
    )

    assert module.read_body(f"{ORIGIN}/") == "ok"
    assert [url for url, _ in adapter.sent] == [f"{ORIGIN}/", _NEXT]


def test_read_body_refuses_a_plaintext_redirect_before_sending_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext = "http://app.example.test/next"
    module, adapter, _ = _scripted(
        monkeypatch,
        {f"{ORIGIN}/": (302, [("Location", plaintext)], b""), plaintext: (200, [], b"captured")},
    )

    with pytest.raises(InsecureUrlError):
        module.read_body(f"{ORIGIN}/")
    assert [url for url, _ in adapter.sent] == [f"{ORIGIN}/"]


def test_read_body_raises_on_an_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    module, _, _ = _scripted(monkeypatch, {f"{ORIGIN}/": (404, [], b"missing")})

    with pytest.raises(requests.HTTPError):
        module.read_body(f"{ORIGIN}/")


def _netrc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    netrc = tmp_path / "netrc"
    netrc.write_text("machine app.example.test login fictional-login password fictional-word\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))


def test_read_body_sends_no_netrc_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _netrc(monkeypatch, tmp_path)
    module, adapter, sessions = _scripted(monkeypatch, {f"{ORIGIN}/": (200, [], b"ok")})

    assert module.read_body(f"{ORIGIN}/") == "ok"
    assert adapter.sent == [(f"{ORIGIN}/", None)]
    assert [session.trust_env for session in sessions] == [False]


def test_a_session_that_trusts_the_environment_would_send_the_netrc_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Positive control: the netrc above is read when `trust_env` is left on."""
    _netrc(monkeypatch, tmp_path)
    adapter = _ScriptedAdapter({f"{ORIGIN}/": (200, [], b"ok")})
    with HttpsSession() as http:
        http.mount("https://", cast("Any", adapter))
        http.get(f"{ORIGIN}/", timeout=1).close()

    [(_, authorization)] = adapter.sent
    assert isinstance(authorization, str)
    assert authorization.startswith("Basic ")
