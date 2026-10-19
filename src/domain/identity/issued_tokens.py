# SPDX-License-Identifier: MIT-0
"""Verification of a token this repository's own issuer minted, presented back to it.

ADR-077 S4's agent exchange (`POST /token`, RFC 8693) accepts exactly one kind of subject: a
Harness-audience token the route exchange minted for the conversation route. Every Gateway tool
verifies its caller by the same rules against the Gateway audience (§5, `domain.identity.caller`).
Pure computation,
like `domain.identity.tokens`, whose header, signature, expiry, and claim rules it reuses: the
key set arrives as an argument because reading it from the key store is an adapter's job.

Every failure is an `IdentityError`, which the exchange answers `invalid_grant`. Messages are for
our logs and never reach a caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from domain.identity.session import IdentityError
from domain.identity.tokens import (
    JsonWebKeySet,
    check_claim_equals,
    check_not_expired,
    require_str_claim,
    signing_key_id,
    verified_claims,
)

__all__ = [
    "IssuedTokenSubject",
    "VerifyIssuedTokenRequest",
    "verify_issued_token",
]


@dataclass(frozen=True, slots=True)
class VerifyIssuedTokenRequest:
    """Everything needed to decide whether a presented token is one this issuer minted.

    `now` is injected so the inclusive expiry rule is deterministic under test.
    """

    token: str = field(repr=False)
    keys: JsonWebKeySet
    expected_issuer: str
    expected_key_id: str
    expected_audience: str
    expected_client_id: str
    now: datetime

    def __post_init__(self) -> None:
        for label, value in (
            ("expected_issuer", self.expected_issuer),
            ("expected_key_id", self.expected_key_id),
            ("expected_audience", self.expected_audience),
            ("expected_client_id", self.expected_client_id),
        ):
            if not value.strip():
                raise ValueError(f"{label} is required; never verify without one.")
        if self.now.tzinfo is None:
            raise ValueError("now must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class IssuedTokenSubject:
    """Who a verified issued token names, which client acts for them, and when it expires."""

    subject: str
    expires_at: int
    """Whole seconds since the epoch: the subject token's own `exp`."""
    client_id: str
    """The verified client the token was minted for; its RFC 8693 `act.sub` names the same one."""

    def remaining_lifetime_seconds(self, issued_at: datetime) -> int:
        """Return the lifetime that makes a token issued at `issued_at` expire exactly here.

        `unsigned_token` floors `issued_at` to whole seconds and sets `exp = iat + lifetime`,
        so this lifetime yields `exp == expires_at` to the second: an exchanged token never
        outlives its subject. Fewer than one second left is a refusal, not a zero lifetime.
        """
        if issued_at.tzinfo is None:
            raise ValueError("issued_at must be timezone-aware.")
        remaining = self.expires_at - int(issued_at.timestamp())
        if remaining < 1:
            raise IdentityError("The subject token has no whole second of life left.")
        return remaining


def verify_issued_token(request: VerifyIssuedTokenRequest) -> IssuedTokenSubject:
    """Verify header, signature, then claims, and return the subject, expiry, and client.

    The header must name RS256 and exactly the issuer's signing key, so a token that merely
    looks right (a Cognito token, a token naming another `kid`) is refused before any key is
    selected. Nothing in the payload is trusted before the signature verifies. The issuer sets
    `act.sub` to the client it mints for, so a token whose actor names anyone else is refused.
    """
    if not request.token:
        raise IdentityError("No subject token presented.")

    kid = signing_key_id(request.token)
    if kid != request.expected_key_id:
        raise IdentityError("The subject token does not name the issuer's signing key.")
    jwk = request.keys.key_for(kid)
    if jwk is None:
        raise IdentityError("The issuer's signing key is not in the key set.")

    claims = verified_claims(request.token, jwk)

    expires_at = claims.get("exp")
    # Whole seconds only: the issuer never mints a fractional `exp`, and the exchanged token's
    # expiry is copied from this one exactly.
    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        raise IdentityError("The subject token has no whole-second exp claim.")
    check_not_expired(claims, request.now)
    check_claim_equals(claims, "iss", request.expected_issuer)
    check_claim_equals(claims, "aud", request.expected_audience)
    check_claim_equals(claims, "client_id", request.expected_client_id)
    _check_actor(claims, request.expected_client_id)

    return IssuedTokenSubject(
        subject=require_str_claim(claims, "sub"),
        expires_at=expires_at,
        client_id=request.expected_client_id,
    )


def _check_actor(claims: dict[str, Any], client_id: str) -> None:
    """Require the RFC 8693 actor claim to name exactly the verified client."""
    actor = claims.get("act")
    if not isinstance(actor, dict):
        raise IdentityError("The token has no act claim.")
    check_claim_equals(cast("dict[str, Any]", actor), "sub", client_id)
