# SPDX-License-Identifier: MIT-0
"""No-network tests for the outbound HTTPS-only transport boundary."""

from typing import Any, cast

import pytest
from requests import PreparedRequest, Response
from requests.adapters import BaseAdapter

from adapters.outbound.https import HttpsSession, InsecureUrlError, require_https

pytestmark = pytest.mark.unit


class _RedirectingAdapter:
    """Return one scripted response while recording whether a redirect was sent."""

    def __init__(self, *, location: str | None = None) -> None:
        self.location = location
        self.requests: list[str] = []

    def send(self, request: PreparedRequest, **kwargs: Any) -> Response:
        del kwargs
        self.requests.append(request.url or "")
        response = Response()
        response.status_code = 302 if self.location is not None else 200
        response.url = request.url or ""
        response.request = request
        if self.location is not None:
            response.headers["Location"] = self.location
        return response

    def close(self) -> None:
        """No resources are opened by this adapter."""


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/path",
        "file:///etc/passwd",
        "ftp://example.test/path",
        "//example.test/path",
        "https://",
        "https://[malformed",
        "not a URL",
    ],
)
def test_non_https_or_malformed_urls_are_rejected(url: str) -> None:
    with pytest.raises(InsecureUrlError, match="malformed or non-HTTPS"):
        require_https(url)


def test_https_url_is_accepted() -> None:
    assert require_https("https://example.test/path?value=1") == (
        "https://example.test/path?value=1"
    )


def test_https_session_accepts_https_without_network() -> None:
    adapter = _RedirectingAdapter()
    session = HttpsSession()
    session.mount("https://", cast("BaseAdapter", adapter))

    response = session.get("https://example.test/path")

    assert response.status_code == 200
    assert adapter.requests == ["https://example.test/path"]


def test_redirect_downgrade_is_rejected_before_second_send() -> None:
    adapter = _RedirectingAdapter(location="http://example.test/downgraded")
    session = HttpsSession()
    session.mount("https://", cast("BaseAdapter", adapter))
    session.mount("http://", cast("BaseAdapter", adapter))

    with pytest.raises(InsecureUrlError, match="non-HTTPS"):
        session.get("https://example.test/start")

    assert adapter.requests == ["https://example.test/start"]
