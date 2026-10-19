# SPDX-License-Identifier: MIT-0
"""The execution-explanation cursor MAC: one KMS HMAC-SHA-256 key (S2, BF-5).

The key never leaves KMS, so no signing secret exists in configuration, in memory or in a
store, and no cursor state is kept anywhere. HMAC is deterministic, so a retried page returns
byte-identical cursor bytes. The key's alias is derived from the execution-graph table name
(`alias/<table>-cursor-mac`), a value the function is already configured with, so the cursor
key needs no configuration key of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from botocore.exceptions import BotoCoreError, ClientError

from domain.shared_kernel.errors import DependencyError, ErrorCode

if TYPE_CHECKING:
    from types_boto3_kms.client import KMSClient

__all__ = ["KmsCursorMac", "cursor_mac_alias"]

_MAC_FAILED: Final = ErrorCode("EXEC_CURSOR", 20)
_ALGORITHM: Final = "HMAC_SHA_256"
_INVALID_MAC: Final = "KMSInvalidMacException"


def cursor_mac_alias(execution_graph_table: str) -> str:
    """Derive the cursor key's alias from the execution-graph table it pages over."""
    return f"alias/{execution_graph_table}-cursor-mac"


class KmsCursorMac:
    """Generate and verify cursor MACs with the KMS HMAC key."""

    def __init__(self, key_id: str, client: KMSClient) -> None:
        self._key_id = key_id
        self._client = client

    def mac(self, message: bytes) -> bytes:
        """Return the deterministic HMAC-SHA-256 of `message`."""
        try:
            response = self._client.generate_mac(
                KeyId=self._key_id, Message=message, MacAlgorithm=_ALGORITHM
            )
        except (BotoCoreError, ClientError) as error:
            raise DependencyError(
                _MAC_FAILED,
                "the cursor MAC could not be computed.",
                context={"cause": type(error).__name__},
            ) from None
        return bytes(response["Mac"])

    def verify(self, message: bytes, mac: bytes) -> bool:
        """Return whether `mac` is this key's MAC of `message`."""
        try:
            response = self._client.verify_mac(
                KeyId=self._key_id, Message=message, Mac=mac, MacAlgorithm=_ALGORITHM
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == _INVALID_MAC:
                return False
            raise DependencyError(
                _MAC_FAILED,
                "the cursor MAC could not be verified.",
                context={"cause": type(error).__name__},
            ) from None
        except BotoCoreError as error:
            raise DependencyError(
                _MAC_FAILED,
                "the cursor MAC could not be verified.",
                context={"cause": type(error).__name__},
            ) from None
        return bool(response.get("MacValid", False))
