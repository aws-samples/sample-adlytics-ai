# SPDX-License-Identifier: MIT-0
"""Token signing port: the token service's one asymmetric key, expressed without naming KMS.

The private key never leaves its key store, so this port signs bytes rather than handing
out key material. The public key is returned as DER SubjectPublicKeyInfo, which the domain
converts to the published JWK (`domain.identity.issuance.jwk_from_der_public_key`).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenSigningPort(Protocol):
    """Sign JWS signing inputs with RS256 and publish the matching public key.

    Every method raises DependencyError when the key store fails or refuses, and never leaks
    an SDK exception type.
    """

    def sign(self, message: bytes) -> bytes:
        """Return the RSASSA-PKCS1-v1_5 SHA-256 signature over `message` itself."""
        ...

    def public_key_der(self) -> bytes:
        """Return the signing key's public key as DER SubjectPublicKeyInfo."""
        ...
