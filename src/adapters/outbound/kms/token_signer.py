# SPDX-License-Identifier: MIT-0
"""KMS implementation of TokenSigningPort (ADR-077 §3).

The key is an asymmetric RSA `SIGN_VERIFY` key. Its key policy denies `kms:Sign` to every
principal but the route-exchange role, so this adapter signs only inside that function; the
public issuer holds `kms:GetPublicKey` alone. The message is signed as `RAW`, which makes KMS
hash it with SHA-256 itself: the JWS signing input is what is signed, exactly as RS256
requires, and no digest is computed here that could disagree with the algorithm named.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from adapters.outbound.aws_errors import aws_failure_context
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.token_signing import TokenSigningPort

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_kms.client import KMSClient

__all__ = [
    "PUBLIC_KEY_UNAVAILABLE",
    "SIGNING_ALGORITHM",
    "SIGNING_UNAVAILABLE",
    "KmsTokenSigner",
]

SIGNING_UNAVAILABLE = ErrorCode("TOKENSVC", 1)
PUBLIC_KEY_UNAVAILABLE = ErrorCode("TOKENSVC", 2)

SIGNING_ALGORITHM = "RSASSA_PKCS1_V1_5_SHA_256"
"""KMS's name for RS256 (RFC 7518 §3.3)."""

_KEY_USAGE = "SIGN_VERIFY"
_UNAVAILABLE = "The token service is temporarily unavailable."


class KmsTokenSigner(TokenSigningPort):
    """Sign with, and publish the public half of, one KMS key. One client per process."""

    def __init__(self, key_id: str, *, region: str, client: KMSClient | None = None) -> None:
        if not key_id.strip() or not region.strip():
            raise ValueError("A KMS signing key id and region are required.")
        self._key_id = key_id
        self._client: KMSClient = (
            client
            if client is not None
            else boto3.client(
                "kms",
                region_name=region,
                config=Config(
                    connect_timeout=2,
                    read_timeout=5,
                    retries={"mode": "standard", "total_max_attempts": 3},
                ),
            )
        )

    def sign(self, message: bytes) -> bytes:
        """Return the RS256 signature KMS computes over `message`."""
        if not message:
            raise ValueError("Refusing to sign an empty message.")
        try:
            response = self._client.sign(
                KeyId=self._key_id,
                Message=message,
                MessageType="RAW",
                SigningAlgorithm=SIGNING_ALGORITHM,
            )
        except (BotoCoreError, ClientError) as error:
            raise DependencyError(
                SIGNING_UNAVAILABLE, _UNAVAILABLE, context=aws_failure_context(error)
            ) from error
        signature = response.get("Signature", b"")
        if not signature or response.get("SigningAlgorithm") != SIGNING_ALGORITHM:
            raise DependencyError(
                SIGNING_UNAVAILABLE, _UNAVAILABLE, context={"cause": "unexpected_sign_response"}
            )
        return signature

    def public_key_der(self) -> bytes:
        """Return the key's DER SubjectPublicKeyInfo, refusing a key that cannot sign RS256."""
        try:
            response = self._client.get_public_key(KeyId=self._key_id)
        except (BotoCoreError, ClientError) as error:
            raise DependencyError(
                PUBLIC_KEY_UNAVAILABLE, _UNAVAILABLE, context=aws_failure_context(error)
            ) from error
        public_key = response.get("PublicKey", b"")
        if (
            not public_key
            or response.get("KeyUsage") != _KEY_USAGE
            or SIGNING_ALGORITHM not in response.get("SigningAlgorithms", [])
        ):
            raise DependencyError(
                PUBLIC_KEY_UNAVAILABLE,
                _UNAVAILABLE,
                context={"cause": "key_cannot_sign_rs256"},
            )
        return public_key
