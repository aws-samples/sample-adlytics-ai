# SPDX-License-Identifier: MIT-0
"""What the token service issues, computed without a key, a clock, or a network.

ADR-077 §3 makes the repository its own minimal issuer, and the conversational-runtime
low-level design owns the claim contract. This module owns the shapes only: the claim set,
the JWS signing input, the compact serialization once a signature exists, the public key
set entry, and the discovery document. Signing is a port (`ports.outbound.token_signing`),
because the private key lives in KMS and never reaches this process.

Invalid inputs raise `ValueError`, never `IdentityError`. An `IdentityError` means "this
caller's subject token was refused" and is answered with `invalid_grant`; a malformed
issuance request is a composition fault, and answering it as a refused user would hide it.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_der_public_key

from domain.identity.tokens import REQUIRED_ALGORITHM, JsonWebKey

__all__ = [
    "CLIENT_SECRET_POST",
    "MINIMUM_RSA_KEY_BITS",
    "TOKEN_EXCHANGE_GRANT_TYPE",
    "DiscoveryDocument",
    "IssueTokenRequest",
    "TokenClaims",
    "UnsignedToken",
    "compact_jws",
    "jwk_from_der_public_key",
    "unsigned_token",
]

MINIMUM_RSA_KEY_BITS = 2048
"""The smallest RSA modulus a published verification key may have (RS256 per RFC 7518 §3.3)."""

TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105
"""RFC 8693 §2.1: the one grant the agent exchange serves."""

# pragma: allowlist nextline secret (A-PROTOCOL-CONSTANT-001)
CLIENT_SECRET_POST = "client_secret_post"  # noqa: S105 — an RFC 8414 method name, not a secret
"""RFC 8414 §2 / OpenID Connect: client credentials in the form body."""

_JWS_TYPE = "JWT"
_JWKS_PATH = "/jwks.json"
_TOKEN_PATH = "/token"  # noqa: S105 — a URL path, not a credential


@dataclass(frozen=True, slots=True)
class IssueTokenRequest:
    """Everything one minted token is a function of.

    `issued_at` and `token_id` are injected rather than read here so a test can pin the
    exact token, and so this module never reaches for a clock or a random source. The
    lifetime is always the caller's explicit choice and no subject expiry is a field: the
    route exchange passes its configured single-turn lifetime (ADR-077 §2), and the agent
    exchange passes `IssuedTokenSubject.remaining_lifetime_seconds`, so its token's `exp`
    equals its subject's and never exceeds it.
    """

    issuer: str
    subject: str
    audience: str
    client_id: str
    key_id: str
    issued_at: datetime
    lifetime_seconds: int
    token_id: str

    def __post_init__(self) -> None:
        if not self.issuer.startswith("https://") or self.issuer != self.issuer.rstrip("/"):
            raise ValueError("issuer must be an https URL without a trailing slash.")
        for label, value in (
            ("subject", self.subject),
            ("audience", self.audience),
            ("client_id", self.client_id),
            ("key_id", self.key_id),
            ("token_id", self.token_id),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"{label} must be a nonblank string without padding.")
        if self.issued_at.tzinfo is None:
            raise ValueError("issued_at must be timezone-aware.")
        if (
            isinstance(self.lifetime_seconds, bool)
            or not isinstance(cast("object", self.lifetime_seconds), int)
            or self.lifetime_seconds < 1
        ):
            raise ValueError("lifetime_seconds must be a positive integer.")


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The exact claim set of one minted token, in its serialized order."""

    iss: str
    sub: str
    aud: str
    client_id: str
    act_sub: str
    iat: int
    exp: int
    jti: str

    def as_json(self) -> dict[str, object]:
        """Return the JSON claim object: `act` is RFC 8693's actor claim, naming the client."""
        return {
            "iss": self.iss,
            "sub": self.sub,
            "aud": self.aud,
            "client_id": self.client_id,
            "act": {"sub": self.act_sub},
            "iat": self.iat,
            "exp": self.exp,
            "jti": self.jti,
        }


@dataclass(frozen=True, slots=True)
class UnsignedToken:
    """A token's header and claims, and the exact bytes a signature must cover."""

    key_id: str
    claims: TokenClaims
    signing_input: bytes


def unsigned_token(request: IssueTokenRequest) -> UnsignedToken:
    """Build the claims and the JWS signing input `b64url(header).b64url(claims)`.

    `iat` is whole seconds, and `exp` is exactly `iat + lifetime_seconds`, so the lifetime a
    consumer observes is the configured one to the second.
    """
    issued_at = int(request.issued_at.timestamp())
    claims = TokenClaims(
        iss=request.issuer,
        sub=request.subject,
        aud=request.audience,
        client_id=request.client_id,
        act_sub=request.client_id,
        iat=issued_at,
        exp=issued_at + request.lifetime_seconds,
        jti=request.token_id,
    )
    header = {"alg": REQUIRED_ALGORITHM, "typ": _JWS_TYPE, "kid": request.key_id}
    signing_input = f"{_b64url_json(header)}.{_b64url_json(claims.as_json())}".encode("ascii")
    return UnsignedToken(key_id=request.key_id, claims=claims, signing_input=signing_input)


def compact_jws(token: UnsignedToken, signature: bytes) -> str:
    """Append the signature to the signing input as the JWS compact serialization."""
    if not signature:
        raise ValueError("A token cannot be assembled without a signature.")
    return f"{token.signing_input.decode('ascii')}.{_b64url(signature)}"


def jwk_from_der_public_key(der: bytes, key_id: str) -> JsonWebKey:
    """Convert a DER SubjectPublicKeyInfo RSA key to its RS256 verification JWK (RFC 7517).

    Refuses anything that is not an RSA key of at least `MINIMUM_RSA_KEY_BITS`, because a key
    set that published it would let every consumer accept tokens it should not.
    """
    if not key_id.strip():
        raise ValueError("A published key needs a kid.")
    try:
        key = load_der_public_key(der)
    except ValueError:
        raise ValueError("The public key is not a DER SubjectPublicKeyInfo.") from None
    if not isinstance(key, RSAPublicKey):
        raise ValueError("The public key is not an RSA key.")
    if key.key_size < MINIMUM_RSA_KEY_BITS:
        raise ValueError("The RSA public key is smaller than the RS256 minimum.")
    numbers = key.public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": REQUIRED_ALGORITHM,
        "kid": key_id,
        "n": _b64url(_unsigned_big_endian(numbers.n)),
        "e": _b64url(_unsigned_big_endian(numbers.e)),
    }


@dataclass(frozen=True, slots=True)
class DiscoveryDocument:
    """The issuer's OpenID discovery document, which Harness and Gateway authorizers read."""

    issuer: str

    def __post_init__(self) -> None:
        if not self.issuer.startswith("https://") or self.issuer != self.issuer.rstrip("/"):
            raise ValueError("issuer must be an https URL without a trailing slash.")

    def as_json(self) -> dict[str, object]:
        """Return the document; both endpoint URLs derive from the issuer so none can drift.

        The token endpoint and its grant and client-authentication method are what AgentCore
        Identity's custom OAuth2 discovery reads to reach the agent exchange (ADR-077 S4).
        """
        return {
            "issuer": self.issuer,
            "jwks_uri": f"{self.issuer}{_JWKS_PATH}",
            "token_endpoint": f"{self.issuer}{_TOKEN_PATH}",
            "grant_types_supported": [TOKEN_EXCHANGE_GRANT_TYPE],
            "token_endpoint_auth_methods_supported": [CLIENT_SECRET_POST],
            "response_types_supported": ["token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": [REQUIRED_ALGORITHM],
        }


def _unsigned_big_endian(value: int) -> bytes:
    """Minimal big-endian octets, as RFC 7518 §6.3.1 requires for `n` and `e`."""
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_json(value: dict[str, object] | dict[str, str]) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode("utf-8"))
