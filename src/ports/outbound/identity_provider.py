# SPDX-License-Identifier: MIT-0
"""Identity provider port.

What the auth handlers need from Cognito, expressed without naming it. The
domain-side types — TokenSet, JsonWebKeySet — are owned by domain/identity, so
this interface never mentions an SDK type or an HTTP shape.

There is deliberately no method that authenticates a user with a password. Every
sign-in goes through the provider's own hosted flow and arrives here as an
authorization code, which is what keeps federated SSO possible — see ADR-007.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from domain.identity.tokens import JsonWebKeySet, TokenSet


@dataclass(frozen=True, slots=True)
class ExchangeCodeRequest:
    """A one-time authorization code being redeemed for tokens."""

    code: str
    redirect_uri: str
    code_verifier: str
    """PKCE verifier matching the challenge sent when the flow started.

    Required, not optional. An authorization code interception is only survivable
    if the code alone is insufficient, and that is exactly what PKCE provides.
    """

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("code is required.")
        if not self.redirect_uri:
            raise ValueError("redirect_uri is required.")
        if not self.code_verifier:
            raise ValueError("code_verifier is required; never exchange a code without PKCE.")


@runtime_checkable
class IdentityProviderPort(Protocol):
    """Token exchange, refresh, revocation and public signing keys.

    Implemented by a Cognito adapter. Every method raises DependencyError once
    retries are exhausted, and never leaks an SDK exception type.
    """

    def exchange_code(self, request: ExchangeCodeRequest) -> TokenSet:
        """Redeem an authorization code at the token endpoint."""
        ...

    def refresh(self, refresh_token: str) -> TokenSet:
        """Exchange a refresh token for a new access token.

        The returned TokenSet carries an empty refresh_token when the provider
        did not reissue one, so a caller never overwrites a good cookie.
        """
        ...

    def revoke(self, refresh_token: str) -> None:
        """Invalidate a refresh token at the provider.

        Called on logout in addition to clearing cookies. Clearing alone leaves a
        copied token working until it expires.
        """
        ...

    def signing_keys(self) -> JsonWebKeySet:
        """Return the pool's public keys, for signature verification.

        Cached by the implementation. A pool has two key pairs and rotates them,
        so this may return a different set over time.
        """
        ...

    def refresh_signing_keys(self) -> JsonWebKeySet:
        """Return the pool's public keys, bypassing any cache.

        A separate method rather than a flag on `signing_keys`, because the two
        have very different costs and the caller must choose deliberately. Called
        only after a token names an unrecognised `kid` — never per request, or an
        unauthenticated caller could drive an outbound fetch on every attempt.
        """
        ...
