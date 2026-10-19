# SPDX-License-Identifier: MIT-0
"""HTTPS-only transport boundary for outbound adapter requests.

Callers validate before adding credentials or signing. ``HttpsSession`` repeats
that assertion on every prepared request, including each redirect hop, so a
server cannot downgrade an accepted HTTPS URL to plaintext or another scheme.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import requests
from requests import PreparedRequest, Response

CONTROL_CHARACTER_LIMIT = 32


class InsecureUrlError(ValueError):
    """An outbound URL is not a well-formed HTTPS URL with a host."""


def require_https(url: str) -> str:
    """Return a well-formed HTTPS URL, refusing every other scheme."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        # Accessing port also rejects malformed values instead of leaving them
        # for the transport to interpret differently.
        _ = parsed.port
    except ValueError as exc:
        raise InsecureUrlError("Refusing to call a malformed or non-HTTPS URL.") from exc

    if (
        parsed.scheme != "https"
        or not hostname
        or any(
            character.isspace() or ord(character) < CONTROL_CHARACTER_LIMIT
            for character in hostname
        )
    ):
        raise InsecureUrlError("Refusing to call a malformed or non-HTTPS URL.")
    return url


class HttpsSession(requests.Session):
    """A Requests session that validates the initial URL and every redirect."""

    def send(self, request: PreparedRequest, **kwargs: Any) -> Response:
        """Validate immediately before Requests performs network I/O."""
        require_https(request.url or "")
        return super().send(request, **kwargs)
