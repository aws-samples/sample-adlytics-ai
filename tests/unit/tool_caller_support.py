# SPDX-License-Identifier: MIT-0
"""Test support for Gateway tool caller verification (ADR-077 §5).

Every Gateway tool resolves `ToolCallerSettings` at import and verifies each call's bearer before
anything else. This module lets a unit test do both without AWS: it imports an entry point under a
fictional caller configuration, then replaces the module's verifier with one whose key set comes
from an RSA key generated here instead of from KMS. The verifier itself is the real one, so every
hostile bearer is refused by the code under test, never by a stub of it (ADR-014).
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from adapters.config import (
    read_tool_caller_settings,
    settings_without_client_secret,
    tool_caller_settings,
    tracing_settings,
)
from adapters.inbound.tool_handlers.caller import CallerVerifier
from domain.identity.issuance import jwk_from_der_public_key
from domain.identity.tokens import JsonWebKeySet

# A-TEST-LITERALS-001: a fictional issuer URL; see docs/security-dispositions.md
TOKEN_ISSUER = "https://abc123.execute-api.eu-west-1.amazonaws.com/v1"  # nosec B105
SIGNING_KEY_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"
SIGNING_KEY_ARN = f"arn:aws:kms:eu-west-1:111122223333:key/{SIGNING_KEY_ID}"
GATEWAY_AUDIENCE = "adlytics-dev-gateway"
HARNESS_AUDIENCE = "adlytics-dev-harness"
AGENT_CLIENT = "adlytics-dev-agent"
ROUTE_CLIENT = "adlytics-dev-conversation-route"
SUBJECT = "5fdbe1d5-7901-4fee-9d1d-518103789c94"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
EXP = int(NOW.timestamp()) + 240
"""Epoch seconds: the Gateway token's expiry, 240 s after NOW."""

CALLER_ENV: Mapping[str, str] = {
    "ADL_TOKEN_ISSUER": TOKEN_ISSUER,
    "ADL_TOKEN_SIGNING_KEY_ARN": SIGNING_KEY_ARN,
    "ADL_GATEWAY_TOKEN_AUDIENCE": GATEWAY_AUDIENCE,
    "ADL_AGENT_CLIENT_ID": AGENT_CLIENT,
}

PROPAGATED_HEADERS = "bedrockAgentCorePropagatedHeaders"

CALLER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
"""The token service's signing key, as far as every tool test in this session is concerned."""

FOREIGN_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
"""A key that is not the issuer's, presented under the issuer's `kid`."""


@dataclass
class LocalPublicKey:
    """`PublicKeySource` over a key held in the test: what KMS `GetPublicKey` returns."""

    key: rsa.RSAPrivateKey
    reads: int = 0
    failure: Exception | None = None

    def public_key_der(self) -> bytes:
        self.reads += 1
        if self.failure is not None:
            raise self.failure
        return self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )


def local_verifier(source: LocalPublicKey | None = None) -> CallerVerifier:
    """Return the real verifier, configured as the tools are, over the test's key and clock."""
    return CallerVerifier(
        read_tool_caller_settings(CALLER_ENV),
        source if source is not None else LocalPublicKey(CALLER_KEY),
        now=lambda: NOW,
    )


def gateway_claims(**overrides: Any) -> dict[str, Any]:
    """Build the Gateway token claim set that verifies; an override of None removes the claim."""
    claims = {
        "iss": TOKEN_ISSUER,
        "sub": SUBJECT,
        "aud": GATEWAY_AUDIENCE,
        "client_id": AGENT_CLIENT,
        "act": {"sub": AGENT_CLIENT},
        "iat": int(NOW.timestamp()) - 10,
        "exp": EXP,
        "jti": "0" * 31 + "1",
        **overrides,
    }
    return {name: value for name, value in claims.items() if value is not None}


def gateway_token(
    key: rsa.RSAPrivateKey = CALLER_KEY, *, kid: str = SIGNING_KEY_ID, **overrides: Any
) -> str:
    """Sign a Gateway-audience token as the token service would, or vary one attribute of it."""
    return jwt.encode(gateway_claims(**overrides), key=key, algorithm="RS256", headers={"kid": kid})


def with_bearer(custom: Mapping[str, Any], token: str | None = None) -> dict[str, Any]:
    """Return a client context carrying the propagated `Authorization` the Gateway forwards."""
    bearer = gateway_token() if token is None else token
    return {**custom, PROPAGATED_HEADERS: {"Authorization": f"Bearer {bearer}"}}


# ADR-150: the external MCP gateway's credential class, a Cognito access token for one public
# client. Every value is fictional; the pool id follows Cognito's `<region>_<id>` shape.
MCP_CLIENT = "3mcpclientexample4567890"
POOL_ID = "eu-west-1_example"
POOL_REGION = "eu-west-1"
COGNITO_ISSUER = f"https://cognito-idp.{POOL_REGION}.amazonaws.com/{POOL_ID}"
COGNITO_KID = "cognito-access-signing-key-1"
COGNITO_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
"""The user pool's access-token signing key, as far as these tests are concerned."""

MCP_CALLER_ENV: Mapping[str, str] = {
    **CALLER_ENV,
    "ADL_MCP_CLIENT_ID": MCP_CLIENT,
    "ADL_COGNITO_USER_POOL_ID": POOL_ID,
    "ADL_AWS_REGION": POOL_REGION,
}


def _public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return jwk_from_der_public_key(der, kid)


@dataclass
class LocalPoolKeys:
    """`SigningKeySource` over keys held in the test: what the pool's JWKS endpoint publishes.

    `published` is what a refresh returns; `cached` is what `signing_keys` returns until then, so a
    test can stage a rotation the cache has not seen yet.
    """

    published: tuple[tuple[str, rsa.RSAPrivateKey], ...] = ((COGNITO_KID, COGNITO_KEY),)
    cached: tuple[tuple[str, rsa.RSAPrivateKey], ...] | None = None
    reads: int = 0
    refreshes: int = 0
    failure: Exception | None = None

    def signing_keys(self) -> JsonWebKeySet:
        self.reads += 1
        if self.failure is not None:
            raise self.failure
        keys = self.published if self.cached is None else self.cached
        return JsonWebKeySet(keys=tuple(_public_jwk(key, kid) for kid, key in keys))

    def refresh_signing_keys(self) -> JsonWebKeySet:
        self.refreshes += 1
        if self.failure is not None:
            raise self.failure
        self.cached = None
        return JsonWebKeySet(keys=tuple(_public_jwk(key, kid) for kid, key in self.published))


def mcp_verifier(keys: LocalPoolKeys | None = None) -> CallerVerifier:
    """Return the real verifier with the external MCP client configured, over test keys."""
    return CallerVerifier(
        read_tool_caller_settings(MCP_CALLER_ENV),
        LocalPublicKey(CALLER_KEY),
        cognito_keys=keys if keys is not None else LocalPoolKeys(),
        now=lambda: NOW,
    )


def cognito_claims(**overrides: Any) -> dict[str, Any]:
    """Build an entitled MCP-client access token's claims; an override of None removes one."""
    claims = {
        "iss": COGNITO_ISSUER,
        "sub": SUBJECT,
        "client_id": MCP_CLIENT,
        "token_use": "access",
        "scope": "openid",
        "quality_review_levels": ["mcp_tool_use"],
        "iat": int(NOW.timestamp()) - 10,
        "exp": int(NOW.timestamp()) + 900,
        "jti": "0" * 31 + "2",
        **overrides,
    }
    return {name: value for name, value in claims.items() if value is not None}


def cognito_token(
    key: rsa.RSAPrivateKey = COGNITO_KEY, *, kid: str = COGNITO_KID, **overrides: Any
) -> str:
    """Sign an MCP-client access token as the user pool would, or vary one attribute of it."""
    return jwt.encode(cognito_claims(**overrides), key=key, algorithm="RS256", headers={"kid": kid})


def import_tool_entrypoint(name: str, extra_env: Mapping[str, str] | None = None) -> ModuleType:
    """Import a tool entry point under the caller configuration, then give it the local verifier.

    The environment is set only for the import and restored afterwards, because the entry point
    resolves its configuration at module scope and keeps it; nothing later in the session sees
    these variables. An already-imported module is reused so every importer shares one instance.
    """
    module = sys.modules.get(name)
    if module is None:
        env = {**CALLER_ENV, **(extra_env or {})}
        saved = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        _clear_settings_caches()
        try:
            module = importlib.import_module(name)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            _clear_settings_caches()
    module.CALLER = local_verifier()  # type: ignore[attr-defined]
    return module


def _clear_settings_caches() -> None:
    tool_caller_settings.cache_clear()
    settings_without_client_secret.cache_clear()
    tracing_settings.cache_clear()
