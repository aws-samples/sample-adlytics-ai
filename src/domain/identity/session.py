# SPDX-License-Identifier: MIT-0
"""Identity and session rules.

Pure domain: no AWS, no Cognito, no HTTP. Token exchange and JWKS verification
are adapter concerns (adapters/outbound/cognito); what a *valid session* means
lives here so it can be tested without a network.

Specified in ADR-007 (docs/decisions/adr-007-auth-cookie-bff.md), including the
measured denial semantics its 2026-09-25 amendment carries.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class ReviewAccessLevel(StrEnum):
    """Closed independently grantable route access levels (ADR-078 §3).

    `AUDIT_ATTRIBUTION` grants nothing alone: it adds the attribution section only for a caller
    who also holds `QUALITY_REVIEWER`. The claim is closed, so a token naming any other level,
    such as one of the three retired per-level values, is refused on every route.
    """

    QUALITY_REVIEWER = "quality_reviewer"
    AUDIT_ATTRIBUTION = "audit_attribution"
    PROMPT_ADMIN = "prompt_admin"
    OPERATIONS_STATUS_READ = "operations_status_read"
    OPERATIONS_RUN_REQUEST = "operations_run_request"
    """May request ingestion runs on demand (owner-gate item 5, 2026-10-07)."""


def may_receive_attribution(levels: Collection[ReviewAccessLevel]) -> bool:
    """Whether verified levels entitle the caller to a turn's principal (ADR-081 §3).

    Both `QUALITY_REVIEWER` and `AUDIT_ATTRIBUTION`: the second alone grants nothing.
    """
    return (
        ReviewAccessLevel.QUALITY_REVIEWER in levels
        and ReviewAccessLevel.AUDIT_ATTRIBUTION in levels
    )


class IdentityError(Exception):
    """Base for identity rule violations.

    Deliberately not one of the four classes in shared_kernel.errors: those carry
    user-facing descriptions, and an auth failure must never explain itself to
    the caller in detail.
    """


class SessionAbsentError(IdentityError):
    """The request carries no session cookie at all.

    Distinct from every other identity failure because only this one means there is no
    session to refresh: ADR-007 answers it 401 (sign in), and every other denial 403
    (refresh once, then retry).
    """


@dataclass(frozen=True, slots=True)
class Identity:
    """Who is asking.

    Built only from a verified token. Never from a request body, query parameter
    or a value the model produced — see ADR-007 and the tool contract steering.
    """

    subject: str
    """The Cognito `sub`. The canonical user identifier platform-wide, and the
    `actor_id` for AgentCore Memory."""

    issuer: str
    """Token `iss`. Checked so a token from another pool cannot be replayed."""

    review_levels: tuple[ReviewAccessLevel, ...] = ()
    """Closed route levels derived only from a verified Cognito access-token claim."""

    def __post_init__(self) -> None:
        if not self.subject:
            raise IdentityError("Identity requires a subject.")
        if not self.issuer:
            raise IdentityError("Identity requires an issuer.")


@dataclass(frozen=True, slots=True)
class Session:
    """A session's temporal validity.

    Expiry is compared against an injected `now` rather than read from the clock,
    because a domain object that calls the clock cannot be tested deterministically
    and the clock is an infrastructure concern.
    """

    identity: Identity
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for label, value in (("issued_at", self.issued_at), ("expires_at", self.expires_at)):
            if value.tzinfo is None:
                raise IdentityError(f"{label} must be timezone-aware.")
        if self.expires_at <= self.issued_at:
            raise IdentityError("expires_at must be after issued_at.")

    def is_expired(self, now: datetime) -> bool:
        """Whether the session is no longer valid at `now`.

        Expiry is inclusive: a session valid at its own expiry is a replay window.
        """
        if now.tzinfo is None:
            raise IdentityError("now must be timezone-aware.")
        return now >= self.expires_at

    def expires_within(self, now: datetime, window: timedelta) -> bool:
        """Whether the session should be refreshed pre-emptively.

        Refreshing slightly early avoids a 401-then-retry on every request that
        happens to land at the boundary.
        """
        if now.tzinfo is None:
            raise IdentityError("now must be timezone-aware.")
        return now + window >= self.expires_at


def csrf_token_matches(cookie_value: str, header_value: str) -> bool:
    """Double-submit CSRF comparison.

    Constant-time, because a short-circuiting comparison leaks the token one byte
    at a time. Absent or empty values are a failure, never a pass — an attacker
    who can omit the header must not thereby succeed.
    """
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)


def new_csrf_token() -> str:
    """Mint a fresh double-submit token.

    From a cryptographic source, because a guessable CSRF token is no CSRF token.
    Issued once per session and rotated whenever the session is.
    """
    return secrets.token_urlsafe(32)


def utc_now() -> datetime:
    """Timezone-aware now.

    Provided so domain tests inject a clock rather than reaching for one, and so
    a naive datetime never enters the model. ADR-004 depends on that discipline.
    """
    return datetime.now(UTC)
