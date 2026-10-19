# SPDX-License-Identifier: MIT-0
"""Which method and path belong to which operation.

Separate from handlers.py deliberately. That module resolves configuration at
import, so importing it requires a deployed environment — and the routing table is
pure logic that should be testable without one.

An exact table rather than prefix matching. A path that is not listed is a 404, so
adding an endpoint is a deliberate edit here rather than a side effect of a path
that happens to match a prefix.
"""

from __future__ import annotations

from typing import Any

from adapters.inbound.auth_handlers import events
from domain.identity.session import IdentityError

LOGIN = "login"
CALLBACK = "callback"
REFRESH = "refresh"
LOGOUT = "logout"
SESSION = "session"

API_PREFIX = "/api"
"""Every API path carries it.

CloudFront serves the frontend and the API from one distribution so the browser
sees a single origin — which is what makes the session cookies same-origin rather
than merely same-site, and what removes the need for a custom domain. A single
`/api/*` cache behaviour selects the API origin, and CloudFront prepends rather
than strips, so the prefix belongs in the route itself. Carrying it here rather
than rewriting at the edge keeps one fewer moving part in the request path.
"""

ROUTES: dict[tuple[str, str], str] = {
    ("GET", f"{API_PREFIX}/auth/login"): LOGIN,
    ("GET", f"{API_PREFIX}/auth/callback"): CALLBACK,
    ("POST", f"{API_PREFIX}/auth/refresh"): REFRESH,
    ("POST", f"{API_PREFIX}/auth/logout"): LOGOUT,
    ("GET", f"{API_PREFIX}/me"): SESSION,
}

UNAUTHENTICATED = frozenset({LOGIN, CALLBACK, REFRESH, LOGOUT})
"""Operations that cannot sit behind the session authorizer.

Login and callback happen before a session exists. Refresh runs precisely when the
access token has expired, so the authorizer would deny it. Logout is tearing the
session down. All four therefore defend themselves, and this set is what the CDK
route wiring is checked against — see ADR-007
(docs/decisions/adr-007-auth-cookie-bff.md), 2026-09-25 amendment.
"""


def resolve(event: dict[str, Any]) -> str:
    """Return the operation for this request, or raise.

    Never guesses. An unmapped route raising here becomes a 404 rather than
    falling through to a handler that was not written for it.
    """
    method = events.method(event)
    path = events.path(event)
    operation = ROUTES.get((method, path))
    if operation is None:
        raise IdentityError(f"No route for {method} {path!r}.")
    return operation
