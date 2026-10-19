# SPDX-License-Identifier: MIT-0
"""Access-token verification: signature first, then claims, in a fixed order.

Pure computation. The JWKS arrives as an argument because *fetching* it is a
network concern owned by adapters/outbound/cognito, while *verifying against* it
is not. That split is what lets the hostile cases below be unit tested — a real
user pool will never issue a token with a wrong audience or a substituted
algorithm, so those inputs have to be constructed. See ADR-014.

`adl_session` carries the Cognito **access** token, so the audience claim is
`client_id` and there is no `aud` claim to check. ADR-007's 2026-09-25 amendment records
why, and what breaks if `aud` is checked instead.

The header, signature, expiry, and claim helpers are public because
`domain.identity.issued_tokens` verifies the token service's own tokens by exactly these
rules; one home for the inclusive expiry and the fixed algorithm keeps the two from drifting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import jwt

from domain.identity.session import Identity, IdentityError, ReviewAccessLevel

QUALITY_REVIEW_LEVELS_CLAIM = "quality_review_levels"

REQUIRED_ALGORITHM = "RS256"
"""The only algorithm accepted. Cognito signs with RS256 and nothing else.

Passed explicitly to the decoder rather than read from the token header. A token
states its own algorithm, so honouring that claim is how algorithm-confusion
attacks work — `alg: none` and an HS256 token signed with the public key both
depend on the verifier believing the attacker.
"""

REQUIRED_TOKEN_USE = "access"  # noqa: S105 — a claim value to compare, not a credential
"""Rejects an ID token presented where an access token is required.

Both are signed by the same pool with valid signatures, so the signature check
cannot tell them apart. Only this claim can.
"""


class UnknownSigningKeyError(IdentityError):
    """The token names a `kid` absent from the key set.

    Distinct from other failures because it is the one that may be recoverable:
    a user pool rotates signing keys, so a correct issuer with an unrecognised
    `kid` most likely means our cached JWKS is stale. The caller refreshes once
    and retries — see the auth handler steering.
    """


type JsonWebKey = dict[str, str]


@dataclass(frozen=True, slots=True)
class JsonWebKeySet:
    """A user pool's public signing keys.

    A pool signs access tokens and ID tokens with two *different* key pairs, so
    there is no single "the pool key" to pin. Selection is always by `kid`.
    """

    keys: tuple[JsonWebKey, ...]

    def key_for(self, kid: str) -> JsonWebKey | None:
        """Return the key with this `kid`, or None. Never guess when `kid` is absent."""
        if not kid:
            return None
        return next((k for k in self.keys if k.get("kid") == kid), None)


@dataclass(frozen=True, slots=True)
class TokenSet:
    """What the identity provider returns from a token exchange or a refresh.

    `refresh_token` is empty on a refresh response: Cognito does not reissue one,
    and pretending otherwise would overwrite a good cookie with an empty value.
    """

    access_token: str
    expires_in: int
    refresh_token: str = ""

    def __post_init__(self) -> None:
        if not self.access_token:
            raise IdentityError("TokenSet requires an access_token.")
        if self.expires_in < 1:
            raise IdentityError("expires_in must be a positive number of seconds.")


@dataclass(frozen=True, slots=True)
class VerifyAccessTokenRequest:
    """Everything needed to decide whether one token is trustworthy.

    `now` is injected rather than read from the clock so expiry is deterministic
    under test, matching the discipline `Session` already follows.
    """

    token: str
    keys: JsonWebKeySet
    expected_issuer: str
    expected_client_id: str
    now: datetime

    def __post_init__(self) -> None:
        if not self.expected_issuer:
            raise IdentityError("expected_issuer is required; never verify without one.")
        if not self.expected_client_id:
            raise IdentityError("expected_client_id is required; never verify without one.")
        if self.now.tzinfo is None:
            raise IdentityError("now must be timezone-aware.")


def verify_access_token(request: VerifyAccessTokenRequest) -> Identity:
    """Verify signature then claims, and return who is asking.

    Raises IdentityError on any failure, and UnknownSigningKeyError for the one
    case a caller should retry. Messages describe the failure for our logs; never
    return one to a caller, because it tells an attacker which check they failed.
    Order is deliberate and load-bearing: nothing about the payload is trusted
    before the signature is verified.
    """
    if not request.token:
        raise IdentityError("No token presented.")

    kid = signing_key_id(request.token)
    jwk = request.keys.key_for(kid)
    if jwk is None:
        raise UnknownSigningKeyError(f"No signing key for kid {kid!r}.")

    claims = verified_claims(request.token, jwk)

    check_not_expired(claims, request.now)
    check_claim_equals(claims, "client_id", request.expected_client_id)
    check_claim_equals(claims, "iss", request.expected_issuer)
    check_claim_equals(claims, "token_use", REQUIRED_TOKEN_USE)

    return Identity(
        subject=require_str_claim(claims, "sub"),
        issuer=request.expected_issuer,
        review_levels=_review_levels(claims),
    )


def _review_levels(claims: dict[str, Any]) -> tuple[ReviewAccessLevel, ...]:
    raw = claims.get(QUALITY_REVIEW_LEVELS_CLAIM)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise IdentityError("Quality review levels claim is malformed.")
    values = cast("list[object]", raw)
    if not all(isinstance(value, str) for value in values):
        raise IdentityError("Quality review levels claim is malformed.")
    try:
        levels = tuple(ReviewAccessLevel(cast("str", value)) for value in values)
    except ValueError:
        raise IdentityError("Quality review levels claim is malformed.") from None
    if len(levels) != len(set(levels)):
        raise IdentityError("Quality review levels claim is malformed.")
    return levels


def signing_key_id(token: str) -> str:
    """Read the `kid` from the unverified header, rejecting any other algorithm.

    Reading the header before verifying is unavoidable — `kid` is what selects
    the key. Nothing here is trusted beyond choosing which key to check against.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise IdentityError("Token header is not decodable.") from exc

    if header.get("alg") != REQUIRED_ALGORITHM:
        raise IdentityError(f"Algorithm {header.get('alg')!r} is not accepted.")

    kid = header.get("kid")
    return kid if isinstance(kid, str) else ""


def verified_claims(token: str, jwk: JsonWebKey) -> dict[str, Any]:
    """Claims from a signature-verified token, with our own checks left to us.

    Claim checks are all disabled here and performed explicitly afterwards, so
    there is one place that decides what a claim failure means. `verify_exp`
    would read the wall clock instead of the injected `now`, and `verify_aud`
    would look for an `aud` claim that an access token does not carry.
    `verify_iat` is off for the same wall-clock reason: with zero leeway it refused a
    token whose floored `iat` was a moment ahead of this container's clock, which is
    ordinary skew between two functions, and no rule here depends on `iat`.

    The two failure paths are kept apart because a log line saying "signature"
    when the real problem was a malformed claim sends the next person to the
    wrong place.
    """
    try:
        key = jwt.PyJWK.from_dict(jwk, algorithm=REQUIRED_ALGORITHM)
        return jwt.decode(
            token,
            key=key.key,
            algorithms=[REQUIRED_ALGORITHM],
            options={
                "verify_exp": False,
                "verify_iat": False,
                "verify_aud": False,
                "verify_iss": False,
                "verify_sub": False,
            },
        )
    except jwt.InvalidSignatureError as exc:
        raise IdentityError("Token signature is not valid.") from exc
    except jwt.PyJWTError as exc:
        raise IdentityError("Token is not verifiable.") from exc


def check_not_expired(claims: dict[str, Any], now: datetime) -> None:
    """Reject at the expiry instant, not after it.

    A token valid at exactly its own `exp` is a replay window, so the comparison
    is inclusive — the same rule `Session.is_expired` follows.
    """
    raw = claims.get("exp")
    if not isinstance(raw, int | float):
        raise IdentityError("Token has no usable exp claim.")
    if now >= datetime.fromtimestamp(float(raw), UTC):
        raise IdentityError("Token has expired.")


def check_claim_equals(claims: dict[str, Any], name: str, expected: str) -> None:
    """Require a claim to be present and exactly equal to `expected`."""
    if require_str_claim(claims, name) != expected:
        raise IdentityError(f"Claim {name} does not match the expected value.")


def require_str_claim(claims: dict[str, Any], name: str) -> str:
    """Return a non-empty string claim, or fail.

    A missing claim is a failure rather than a skipped check. Treating absence as
    "nothing to verify" is how an audience check silently stops running.
    """
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise IdentityError(f"Claim {name} is missing or not a string.")
    return value
