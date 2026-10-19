# SPDX-License-Identifier: MIT-0
"""The authorization decision, composed in one place.

The step order in the auth handler steering — extract exactly one credential, verify the
signature, validate claims and the credential's client and route binding, then CSRF — is
implemented here rather than in the authorizer adapter, so there is exactly one tested ordering.
An adapter that composed these itself could get the order wrong in a way that still returns
allow.

Two entry points share that ordering. `authorize_route` is the API authorizer's decision: the
route entitlement comes from the matched route key, and each credential is bound to one app
client and a fixed route set (ADR-078 §2, §3). `authorize_session` is the cookie-only decision
the conversation route makes for itself before it exchanges the user's token onward (ADR-077 §2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from domain.identity.cookies import CSRF_COOKIE, SESSION_COOKIE, parse_cookie_header
from domain.identity.session import (
    Identity,
    IdentityError,
    ReviewAccessLevel,
    SessionAbsentError,
    csrf_token_matches,
)
from domain.identity.tokens import JsonWebKeySet, VerifyAccessTokenRequest, verify_access_token

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""Methods exempt from CSRF.

Defined as the allow-list rather than listing the mutating methods, so a method
nobody anticipated requires CSRF by default instead of bypassing it.
"""

CSRF_HEADER = "x-adl-csrf"
"""Where the browser echoes the adl_csrf cookie. CSRF design: ADR-007."""

SAME_SITE_FETCH_VALUES = frozenset({"same-origin", "same-site"})
"""Sec-Fetch-Site values that mean the request came from our own application.

`cross-site` and `none` both mean it did not. `none` covers a request the user
initiated directly, such as pasting a URL, which is not a legitimate way to reach
a mutating endpoint.
"""

QUALITY_REVIEW_TURN_ROUTE = "POST /api/quality-review/turn"
QUALITY_REVIEW_TURNS_ROUTE = "POST /api/quality-review/turns"
REVIEW_ROUTE_KEYS = frozenset({QUALITY_REVIEW_TURN_ROUTE, QUALITY_REVIEW_TURNS_ROUTE})
"""The only routes a bearer credential is valid on (ADR-078 §2)."""

ROUTE_ENTITLEMENTS: dict[str, ReviewAccessLevel] = {
    QUALITY_REVIEW_TURN_ROUTE: ReviewAccessLevel.QUALITY_REVIEWER,
    QUALITY_REVIEW_TURNS_ROUTE: ReviewAccessLevel.QUALITY_REVIEWER,
    "POST /api/prompt-registry/slots": ReviewAccessLevel.PROMPT_ADMIN,
    "POST /api/prompt-registry/versions": ReviewAccessLevel.PROMPT_ADMIN,
    "POST /api/prompt-registry/create-version": ReviewAccessLevel.PROMPT_ADMIN,
    "POST /api/prompt-registry/activate": ReviewAccessLevel.PROMPT_ADMIN,
    "POST /api/prompt-registry/retire": ReviewAccessLevel.PROMPT_ADMIN,
    "POST /api/prompt-registry/extensions/calibrate": ReviewAccessLevel.PROMPT_ADMIN,
    "GET /api/operations/data-flow-definitions": ReviewAccessLevel.OPERATIONS_STATUS_READ,
    "GET /api/operations/data-flows": ReviewAccessLevel.OPERATIONS_STATUS_READ,
    "GET /api/operations/data-flows/{flow_id}": ReviewAccessLevel.OPERATIONS_STATUS_READ,
    "GET /api/operations/data-flows/{flow_id}/events": ReviewAccessLevel.OPERATIONS_STATUS_READ,
    "POST /api/operations/execution-explanations": ReviewAccessLevel.OPERATIONS_STATUS_READ,
    "POST /api/operations/run-requests": ReviewAccessLevel.OPERATIONS_RUN_REQUEST,
    "GET /api/operations/run-requests/{request_id}": ReviewAccessLevel.OPERATIONS_RUN_REQUEST,
}
"""The entitlement each protected route key requires, keyed by the route API Gateway matched.

Keyed by route key rather than raw path, so a path variant that API Gateway still matched to a
route can never select a different entitlement than the route it reached.
"""

PROTECTED_PREFIXES = ("/api/quality-review/", "/api/prompt-registry/", "/api/operations/")
"""Route-key paths under which an unmapped route is denied rather than left unentitled."""

_BEARER = re.compile(
    r"\A(?i:bearer) +([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\Z",
)
"""Exactly one compact JWS after a case-insensitive `Bearer` scheme and at least one space.

A non-empty signature segment is required, so an `alg: none` token is refused here already, and
two tokens joined by a proxy's comma never match.
"""


def requires_csrf(method: str) -> bool:
    """Whether this method needs a CSRF token.

    Applied on every mutating request, not only when SameSite is None. The
    double-submit check is cheap and it is the only defence left if the cookie
    ever has to be reissued as SameSite=None.
    """
    return method.strip().upper() not in SAFE_METHODS


def required_route_level(route_key: str) -> ReviewAccessLevel | None:
    """Return the entitlement a route key requires, `None` for an unentitled route, or raise.

    A route key under a protected prefix with no mapping is denied, so a route added there
    without an entitlement fails closed instead of being open to every signed-in caller.
    """
    method, separator, path = route_key.partition(" ")
    if not separator or not method or not path.startswith("/"):
        raise IdentityError("The route key is malformed.")
    required = ROUTE_ENTITLEMENTS.get(route_key)
    if required is not None:
        return required
    if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in PROTECTED_PREFIXES):
        raise IdentityError("The protected route has no entitlement mapping.")
    return None


def bearer_token(authorization_header: str) -> str:
    """Return the one bearer token in an Authorization header, or raise."""
    match = _BEARER.fullmatch(authorization_header.strip())
    if match is None:
        raise IdentityError("The Authorization header is not exactly one bearer token.")
    return match.group(1)


@dataclass(frozen=True, slots=True, kw_only=True)
class _CsrfEvidence:
    method: str
    csrf_cookie: str
    csrf_header: str
    allowed_origin: str
    origin: str
    sec_fetch_site: str


def _require_same_application(evidence: _CsrfEvidence) -> None:
    """Apply both CSRF defences to a mutating cookie-credentialed request."""
    if not requires_csrf(evidence.method):
        return
    if not csrf_token_matches(evidence.csrf_cookie, evidence.csrf_header):
        raise IdentityError("CSRF double-submit validation failed.")
    if not _came_from_our_application(evidence):
        raise IdentityError("The request did not originate from this application.")


def _came_from_our_application(evidence: _CsrfEvidence) -> bool:
    """Second CSRF defence, independent of the double-submit token.

    Sec-Fetch-Site is preferred because the browser sets it and a page cannot
    forge it. Origin is the fallback for a browser that omits it. Neither present
    means this is not a browser request from our application, which for a mutating
    endpoint is a denial rather than a benefit of the doubt.
    """
    if evidence.sec_fetch_site:
        return evidence.sec_fetch_site.strip().lower() in SAME_SITE_FETCH_VALUES
    return bool(evidence.origin) and evidence.origin == evidence.allowed_origin


def _require_origin_for_mutation(method: str, allowed_origin: str) -> None:
    if not method:
        raise IdentityError("method is required to decide whether CSRF applies.")
    if requires_csrf(method) and not allowed_origin:
        raise IdentityError(
            "allowed_origin is required for a mutating request. Without it the "
            "origin check would pass vacuously."
        )


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """One cookie-credentialed request, reduced to what the session decision depends on.

    The cookie header is the only identity source. Nothing here accepts an
    identity from a body, query parameter or custom header — see ADR-007.
    """

    cookie_header: str
    method: str
    keys: JsonWebKeySet
    expected_issuer: str
    expected_client_id: str
    now: datetime
    csrf_header: str = ""
    allowed_origin: str = ""
    """The application's own origin. Required for a mutating request."""
    origin: str = ""
    sec_fetch_site: str = ""

    def __post_init__(self) -> None:
        _require_origin_for_mutation(self.method, self.allowed_origin)


@dataclass(frozen=True, slots=True)
class AuthorizedSession:
    """An authorized caller together with the verified cookie token.

    Returned only to a caller that exchanges the user's own token onward (ADR-077
    §2). `access_token` is excluded from repr so it never reaches a log line or a
    traceback through this object.
    """

    identity: Identity
    access_token: str = field(repr=False)


def authorize_session(request: AuthorizationRequest) -> AuthorizedSession:
    """Authorize one cookie-credentialed request and return the verified token, or raise.

    Raises SessionAbsentError when no session cookie is present, IdentityError for any other
    failure, and UnknownSigningKeyError when the caller should refresh the JWKS and retry once.
    There is no boolean return and no partial success. The token returned is the one whose
    signature and claims were just verified, never a second read of the cookie.
    """
    cookies = parse_cookie_header(request.cookie_header)
    token = cookies.get(SESSION_COOKIE, "")
    identity = _verify(
        token,
        request=VerificationTarget(
            keys=request.keys,
            expected_issuer=request.expected_issuer,
            expected_client_id=request.expected_client_id,
            now=request.now,
        ),
    )
    _require_same_application(
        _CsrfEvidence(
            method=request.method,
            csrf_cookie=cookies.get(CSRF_COOKIE, ""),
            csrf_header=request.csrf_header,
            allowed_origin=request.allowed_origin,
            origin=request.origin,
            sec_fetch_site=request.sec_fetch_site,
        )
    )
    return AuthorizedSession(identity=identity, access_token=token)


@dataclass(frozen=True, slots=True, kw_only=True)
class VerificationTarget:
    """The key set, issuer, client, and instant one token is verified against."""

    keys: JsonWebKeySet
    expected_issuer: str
    expected_client_id: str
    now: datetime


def _verify(token: str, *, request: VerificationTarget) -> Identity:
    if not token:
        raise SessionAbsentError(f"Request carries no {SESSION_COOKIE} cookie.")
    return verify_access_token(
        VerifyAccessTokenRequest(
            token=token,
            keys=request.keys,
            expected_issuer=request.expected_issuer,
            expected_client_id=request.expected_client_id,
            now=request.now,
        )
    )


class CredentialKind(StrEnum):
    """Which one credential a request carried."""

    SESSION_COOKIE = "session_cookie"
    BEARER = "bearer"


@dataclass(frozen=True, slots=True)
class RouteAuthorizationRequest:
    """One API request as the authorizer sees it: its matched route and both credential slots.

    `authorization_header` is `None` when the request carried no Authorization header; any
    present value, even an empty one, is a credential that must parse as exactly one bearer.
    """

    route_key: str
    method: str
    cookie_header: str
    authorization_header: str | None
    keys: JsonWebKeySet
    expected_issuer: str
    session_client_id: str
    """The confidential BFF client: the only client an `adl_session` cookie is valid with."""
    bearer_client_id: str
    """The public PKCE client: the only client a bearer is valid with."""
    now: datetime
    csrf_header: str = ""
    allowed_origin: str = ""
    origin: str = ""
    sec_fetch_site: str = ""

    def __post_init__(self) -> None:
        if not self.route_key:
            raise IdentityError("route_key is required; entitlement is chosen from it.")
        if not self.session_client_id or not self.bearer_client_id:
            raise IdentityError("Both client ids are required; never verify without one.")
        _require_origin_for_mutation(self.method, self.allowed_origin)


@dataclass(frozen=True, slots=True)
class RouteAuthorization:
    """An authorized caller, the credential it used, and whether the route is a review route."""

    identity: Identity
    credential: CredentialKind
    review_route: bool


def authorize_route(request: RouteAuthorizationRequest) -> RouteAuthorization:
    """Return the authorized caller of one API route, or raise.

    Order, every step failing closed: the route's required entitlement from its route key;
    exactly one credential; for a bearer, the route binding before any cryptography, so a
    bearer on another route cannot drive a key refresh; signature, then claims with the
    credential's own client; the entitlement; then CSRF for the cookie credential only.
    """
    required = required_route_level(request.route_key)
    cookies = parse_cookie_header(request.cookie_header)
    has_cookie = SESSION_COOKIE in cookies
    has_bearer = request.authorization_header is not None
    if has_cookie and has_bearer:
        raise IdentityError("Request carries both a session cookie and a bearer.")
    review_route = request.route_key in REVIEW_ROUTE_KEYS
    if request.authorization_header is not None:
        if not review_route:
            raise IdentityError("A bearer is not valid on this route.")
        identity = _verify(
            bearer_token(request.authorization_header),
            request=VerificationTarget(
                keys=request.keys,
                expected_issuer=request.expected_issuer,
                expected_client_id=request.bearer_client_id,
                now=request.now,
            ),
        )
        credential = CredentialKind.BEARER
    else:
        identity = _verify(
            cookies.get(SESSION_COOKIE, ""),
            request=VerificationTarget(
                keys=request.keys,
                expected_issuer=request.expected_issuer,
                expected_client_id=request.session_client_id,
                now=request.now,
            ),
        )
        credential = CredentialKind.SESSION_COOKIE
    if required is not None and required not in identity.review_levels:
        raise IdentityError("Route entitlement is absent.")
    if credential is CredentialKind.SESSION_COOKIE:
        _require_same_application(
            _CsrfEvidence(
                method=request.method,
                csrf_cookie=cookies.get(CSRF_COOKIE, ""),
                csrf_header=request.csrf_header,
                allowed_origin=request.allowed_origin,
                origin=request.origin,
                sec_fetch_site=request.sec_fetch_site,
            )
        )
    return RouteAuthorization(identity=identity, credential=credential, review_route=review_route)
