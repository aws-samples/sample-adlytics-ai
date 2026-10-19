# SPDX-License-Identifier: MIT-0
"""Shared fixtures for the unit tier.

`TokenMinter` exists because a real user pool cannot issue the tokens that matter
most to test — a wrong audience, a substituted algorithm, an unknown signing key.
Holding the private key is what makes those cases constructible. See ADR-014.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from git_history_support import MARKER, marker_revision, missing_history_reason
from jwt.algorithms import RSAAlgorithm

from domain.identity.tokens import JsonWebKey, JsonWebKeySet

ISSUER = "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_example"
CLIENT_ID = "1example23456789"
SUBJECT = "5fdbe1d5-7901-4fee-9d1d-518103789c94"
KID = "primary-signing-key"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class TokenMinter:
    """Signs tokens with a keypair the test controls."""

    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def _jwk(self, key: rsa.RSAPrivateKey, kid: str) -> JsonWebKey:
        jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        return {**{k: str(v) for k, v in jwk.items() if k != "key_ops"}, "kid": kid, "alg": "RS256"}

    def jwks(self, *, kid: str = KID) -> JsonWebKeySet:
        """Return the key set an authorizer would have fetched from the pool."""
        return JsonWebKeySet(keys=(self._jwk(self.private_key, kid),))

    def claims(self, **overrides: Any) -> dict[str, Any]:
        """Build a claim set that verifies cleanly, before any override.

        An override of None removes the claim rather than setting it to null,
        because "absent" and "present but null" are different attacks and it is
        absence that a skipped check lets through.
        """
        claims = {
            "sub": SUBJECT,
            "iss": ISSUER,
            "client_id": CLIENT_ID,
            "token_use": "access",
            "exp": int((NOW + timedelta(minutes=15)).timestamp()),
            **overrides,
        }
        return {name: value for name, value in claims.items() if value is not None}

    def token(
        self,
        *,
        kid: str = KID,
        algorithm: str = "RS256",
        wrong_key: bool = False,
        **claim_overrides: Any,
    ) -> str:
        """Sign a token, optionally in a way Cognito never would."""
        key = self.other_key if wrong_key else self.private_key
        if algorithm == "HS256":
            return jwt.encode(
                self.claims(**claim_overrides),
                key="a" * 32,
                algorithm="HS256",
                headers={"kid": kid},
            )
        return jwt.encode(
            self.claims(**claim_overrides),
            key=key,  # type: ignore[arg-type]
            algorithm=algorithm,
            headers={"kid": kid},
        )

    def unsigned_token(self, **claim_overrides: Any) -> str:
        """Build a token declaring `alg: none` with an empty signature.

        Built by hand because the point is to produce something a compliant
        library would refuse to emit.
        """
        segments: list[dict[str, Any]] = [
            {"alg": "none", "typ": "JWT", "kid": KID},
            self.claims(**claim_overrides),
        ]
        encoded = [
            base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()
            for part in segments
        ]
        return ".".join([*encoded, ""])


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip a `requires_git_history(revision)` test whose revision this checkout does not hold.

    The reason names the revision, so a clean export reports the check it could not make rather
    than failing on `git` (git_history_support.py).
    """
    revisions = [marker_revision(mark) for mark in item.iter_markers(name=MARKER)]
    reason = missing_history_reason(revisions)
    if reason is not None:
        pytest.skip(reason)


@pytest.fixture(scope="session")
def minter() -> TokenMinter:
    """Session-scoped: RSA keygen is slow and the keys never need to differ."""
    return TokenMinter()


EDGE_ORIGIN_VALUE = "fictional-edge-origin-value-0123456789"
"""A fictional origin-verify value; never a deployed one."""


def stub_origin_verify(monkeypatch: pytest.MonkeyPatch, secret: str | None = None) -> str:
    """Make the auth modules' cold-start origin-verify read return a fictional secret.

    Patched on `adapters.config`, which the modules read at import, so call this before importing
    the authorizer or the auth handler. Returns the accepted value.
    """
    from adapters import config  # noqa: PLC0415 - imported where the patch is applied

    monkeypatch.setattr(
        config,
        "origin_verify_settings",
        lambda: config.OriginVerifySettings("adlytics/dev/edge/origin-verify", 300, 60),
    )
    document = json.dumps({"current": EDGE_ORIGIN_VALUE}) if secret is None else secret

    def read(_secret_id: str) -> str:
        return document

    monkeypatch.setattr(config, "read_origin_verify_secret", read)
    return EDGE_ORIGIN_VALUE
