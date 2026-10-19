# SPDX-License-Identifier: MIT-0
"""Cookie construction and parsing.

Every attribute here is security-load-bearing. The three cookies, their
attributes and the SameSite analysis are specified in ADR-007
(docs/decisions/adr-007-auth-cookie-bff.md); this module is the single
implementation of that specification.

Building all three headers in one function is deliberate. "Issue cookies in
exactly one place" is a rule in the auth handler steering, and one function that
returns the whole set is how that rule stops being something to remember.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from domain.identity.session import IdentityError
from domain.identity.tokens import TokenSet

SESSION_COOKIE = "adl_session"
REFRESH_COOKIE = "adl_refresh"
CSRF_COOKIE = "adl_csrf"

TRANSACTION_COOKIE = "adl_txn"
"""Carries the in-flight login's state and PKCE verifier across the redirect.

Not part of a session — it exists only between /auth/login and /auth/callback and
is cleared as soon as the code is redeemed. HttpOnly, so no script can read the
verifier, and SameSite=Lax so it still arrives on the top-level navigation back
from the provider.
"""

TRANSACTION_MAX_AGE = 300
"""Five minutes. Long enough to type a password and clear an MFA prompt, short
enough that an abandoned login does not leave a usable verifier lying around."""

SAME_SITE = "Lax"
"""Never Strict.

Strict is stricter than this architecture needs and breaks a user arriving from
an external link who expects to still be signed in. Lax keeps the protection
that matters, which is on state-changing requests.
"""


PUBLIC_SUFFIXES = (".cloudfront.net", ".amazonaws.com", ".on.aws")
"""AWS-provided suffixes that a browser refuses to accept a cookie for.

These are on the Public Suffix List, which exists precisely so one tenant cannot
set a cookie readable by every other tenant sharing the hostname. A `Set-Cookie`
naming one of these is dropped silently: the login appears to succeed and every
request afterwards is denied, which is the least diagnosable failure in this whole
design. Host-only is the correct scope on an AWS-provided hostname.
"""


class CookieScope(StrEnum):
    """How widely a cookie is scoped, stated rather than inferred.

    An empty domain string could mean either "host-only, deliberately" or
    "configuration did not resolve". Naming the intent removes that ambiguity, so
    a missing value cannot quietly become the permissive reading.
    """

    HOST_ONLY = "host_only"
    """No Domain attribute. The cookie goes back only to the exact host that set
    it, which is both the stricter scope and the only legal one on a public
    suffix."""

    PARENT_DOMAIN = "parent_domain"
    """Scoped to a registrable parent, so an app host and an API host under it
    share the session. Requires a domain we actually control."""


@dataclass(frozen=True, slots=True)
class CookiePolicy:
    """Where cookies are scoped.

    Only the scope is configurable. HttpOnly, Secure, SameSite and Path are fixed
    by the security design — making any of them a parameter would invite a caller
    to weaken it.
    """

    scope: CookieScope
    domain: str = ""
    """The registrable parent, leading dot included: `.customer.com`.

    Required for PARENT_DOMAIN, and must be empty for HOST_ONLY.
    """

    def __post_init__(self) -> None:
        if self.scope is CookieScope.HOST_ONLY:
            if self.domain:
                raise IdentityError(
                    f"Host-only cookies take no domain, but {self.domain!r} was given. "
                    "Use PARENT_DOMAIN to scope across hosts."
                )
            return

        if not self.domain:
            raise IdentityError("PARENT_DOMAIN scope requires a domain; there is no default.")
        if not self.domain.startswith("."):
            raise IdentityError(
                f"Cookie domain {self.domain!r} must be the registrable parent "
                "with a leading dot, such as '.customer.com'."
            )
        if "/" in self.domain or ":" in self.domain:
            raise IdentityError(
                f"Cookie domain {self.domain!r} looks like a URL. It must be a bare domain."
            )
        if self.domain.rstrip(".").endswith("localhost"):
            raise IdentityError(
                "Cookie domain resolved to localhost. These cookies are Secure and "
                "scoped to a registrable parent, so a localhost value means "
                "configuration did not resolve — fail rather than emit it."
            )
        for suffix in PUBLIC_SUFFIXES:
            if self.domain == suffix or self.domain.endswith(suffix):
                raise IdentityError(
                    f"Cookie domain {self.domain!r} is a public suffix, which a browser "
                    "refuses to set a cookie for. Use HOST_ONLY on an AWS-provided "
                    "hostname."
                )


@dataclass(frozen=True, slots=True)
class SessionCookieRequest:
    """A freshly authenticated or refreshed session, ready to become headers."""

    policy: CookiePolicy
    tokens: TokenSet
    csrf_token: str
    refresh_max_age: int
    """Lifetime for the refresh and CSRF cookies, in seconds.

    Comes from configuration rather than the token response, because Cognito does
    not report the refresh token's lifetime.
    """

    def __post_init__(self) -> None:
        if not self.csrf_token:
            raise IdentityError("A session must carry a CSRF token.")
        if self.refresh_max_age < 1:
            raise IdentityError("refresh_max_age must be a positive number of seconds.")


@dataclass(frozen=True, slots=True)
class _CookieToSet:
    """The parts of a cookie that differ between the three.

    Everything else — Secure, Path, SameSite — is applied uniformly by
    `_set_cookie` so no caller can vary it.
    """

    name: str
    value: str
    max_age: int
    http_only: bool


def build_session_cookies(request: SessionCookieRequest) -> tuple[str, ...]:
    """Build every Set-Cookie header for a session.

    The only place in the platform that issues session cookies. A refresh omits
    the refresh cookie when the provider did not reissue a refresh token, so a
    rotation never overwrites a valid one with an empty value.
    """
    cookies = [
        _CookieToSet(
            name=SESSION_COOKIE,
            value=request.tokens.access_token,
            max_age=request.tokens.expires_in,
            http_only=True,
        ),
        _CookieToSet(
            name=CSRF_COOKIE,
            value=request.csrf_token,
            max_age=request.refresh_max_age,
            http_only=False,
        ),
    ]
    if request.tokens.refresh_token:
        cookies.append(
            _CookieToSet(
                name=REFRESH_COOKIE,
                value=request.tokens.refresh_token,
                max_age=request.refresh_max_age,
                http_only=True,
            )
        )
    return tuple(_set_cookie(cookie, request.policy) for cookie in cookies)


def build_cleared_cookies(policy: CookiePolicy) -> tuple[str, ...]:
    """Clear all three cookies, for logout.

    Clearing alone is not logout. The refresh token must also be revoked at the
    provider, or a copy taken before logout still works — see the auth handler
    steering.
    """
    return tuple(
        _set_cookie(_CookieToSet(name=name, value="", max_age=0, http_only=http_only), policy)
        for name, http_only in (
            (SESSION_COOKIE, True),
            (REFRESH_COOKIE, True),
            (CSRF_COOKIE, False),
        )
    )


def build_transaction_cookie(policy: CookiePolicy, encoded: str) -> str:
    """Set the short-lived login transaction cookie."""
    if not encoded:
        raise IdentityError("Refusing to set an empty login transaction cookie.")
    return _set_cookie(
        _CookieToSet(
            name=TRANSACTION_COOKIE,
            value=encoded,
            max_age=TRANSACTION_MAX_AGE,
            http_only=True,
        ),
        policy,
    )


def clear_transaction_cookie(policy: CookiePolicy) -> str:
    """Remove the transaction cookie once the code has been redeemed.

    Cleared on both the success and failure paths. A verifier that outlives its
    single use is a credential with no remaining purpose.
    """
    return _set_cookie(
        _CookieToSet(name=TRANSACTION_COOKIE, value="", max_age=0, http_only=True),
        policy,
    )


def parse_cookie_header(header: str) -> dict[str, str]:
    """Parse a request `Cookie` header into names and values.

    Splits each pair on its first `=` only, because a token value can itself
    contain padding characters. Malformed pairs are dropped rather than raising:
    an unrelated third-party cookie must not be able to fail our authorizer.
    """
    cookies: dict[str, str] = {}
    for pair in header.split(";"):
        name, separator, value = pair.strip().partition("=")
        if separator and name:
            cookies[name.strip()] = value.strip()
    return cookies


def _set_cookie(cookie: _CookieToSet, policy: CookiePolicy) -> str:
    """Render one Set-Cookie header. Secure, Path=/ and SameSite are not negotiable.

    A host-only cookie omits Domain entirely rather than sending an empty one. An
    empty Domain attribute is not the same thing and browsers vary on it.
    """
    parts = [f"{cookie.name}={cookie.value}"]
    if policy.scope is CookieScope.PARENT_DOMAIN:
        parts.append(f"Domain={policy.domain}")
    parts += [
        "Path=/",
        f"Max-Age={cookie.max_age}",
        f"SameSite={SAME_SITE}",
        "Secure",
    ]
    if cookie.http_only:
        parts.append("HttpOnly")
    return "; ".join(parts)
