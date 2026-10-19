# SPDX-License-Identifier: MIT-0
"""The thin HTTPS client behind `flow explain` (S2, FR-8).

It sends one explanation request to the deployed API over HTTPS only, follows no redirect and
no diagnostic fallback, and returns the server's status and JSON body unchanged. It imports no
AWS SDK, log or trace client: the API is the only authority it talks to, and everything it
prints is a server-owned row.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, cast

from adapters.outbound.https import HttpsSession, require_https

__all__ = ["EXPLANATIONS_PATH", "ExplainResponse", "SessionCredential", "explain", "reference"]

EXPLANATIONS_PATH: Final = "/api/operations/execution-explanations"
_TIMEOUT_SECONDS: Final = 30
_UUID_LENGTH: Final = 36


@dataclass(frozen=True, slots=True)
class SessionCredential:
    """One `adl_session` value; never printed."""

    session: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ExplainResponse:
    """The server's status and body, as returned."""

    status: int
    body: Mapping[str, object]


def reference(  # noqa: PLR0913 -- the CLI's four optional selectors, keyword-only
    target: str,
    *,
    version_id: str | None = None,
    graph_id: str | None = None,
    node: str | None = None,
) -> dict[str, object]:
    """Build one arm from the CLI's target: a graph, a node, a flow ID or a manifest key."""
    if graph_id is not None:
        if node is not None:
            return {"node": {"graph_id": graph_id, "node": node}}
        return {"plan": {"graph_id": graph_id}}
    if len(target) == _UUID_LENGTH and target.count("-") == 4 and "/" not in target:  # noqa: PLR2004
        return {"flow_id": target}
    manifest: dict[str, str] = {"key": target}
    if version_id is not None:
        manifest["version_id"] = version_id
    return {"manifest": manifest}


def explain(  # noqa: PLR0913 -- origin, credential and body, plus an injectable session
    origin: str,
    credential: SessionCredential,
    body: Mapping[str, object],
    *,
    http: HttpsSession | None = None,
) -> ExplainResponse:
    """POST one explanation request with a fresh double-submit CSRF value."""
    csrf = secrets.token_urlsafe(24)
    url = require_https(f"{origin.rstrip('/')}{EXPLANATIONS_PATH}")
    session = http or HttpsSession()
    with session.post(
        url,
        data=json.dumps(dict(body), sort_keys=True).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Cookie": f"adl_session={credential.session}; adl_csrf={csrf}",
            "x-adl-csrf": csrf,
            "Sec-Fetch-Site": "same-origin",
        },
        timeout=_TIMEOUT_SECONDS,
        allow_redirects=False,
    ) as response:
        try:
            decoded: object = response.json() if response.content else {}
        except ValueError:
            decoded = {}
        document = cast("dict[str, object]", decoded) if isinstance(decoded, dict) else {}
        return ExplainResponse(status=response.status_code, body=document)
