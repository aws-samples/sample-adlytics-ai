# SPDX-License-Identifier: MIT-0
"""Fictional JWT-shaped strings, spelled as readable parts so no encoded literal is committed.

Nothing here is signed by a real key or accepted by any verifier. The header and claims are
written as JSON so a reader sees exactly what the token says. A plain fictional secret is spelled
as words (for example "Fictional-Agent-Secret-For-Tests"), never as random-looking characters.
"""

import base64
import json
from collections.abc import Mapping

FICTIONAL_SIGNATURE = "fictional-signature"


def b64url(raw: bytes) -> str:
    """Base64url-encode bytes without padding (RFC 7515 §2)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64url_json(value: Mapping[str, object]) -> str:
    """Base64url-encode compact JSON without padding."""
    return b64url(json.dumps(value, separators=(",", ":")).encode())


def fictional_jwt(
    claims: Mapping[str, object] | str | None = None,
    *,
    signature: str = FICTIONAL_SIGNATURE,
) -> str:
    """Return `header.claims.signature`; a str `claims` is used verbatim as a marker segment."""
    if claims is None:
        claims = {"sub": "fictional"}
    payload = claims if isinstance(claims, str) else b64url_json(claims)
    return f"{b64url_json({'alg': 'RS256'})}.{payload}.{signature}"
