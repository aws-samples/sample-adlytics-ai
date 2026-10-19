# SPDX-License-Identifier: MIT-0
"""Secrets Manager implementation of ClientSecretPort (ADR-077 S4).

The agent client secret is generated at deploy time as a JSON object whose `client_secret` key
holds the value; the same key is what the AgentCore OAuth2 credential provider reads. It is
fetched once per container and cached, so a request flood costs Lambda invocations, not
Secrets Manager calls; `refreshed` bypasses the cache when a presented secret does not match,
at most once per `REFRESH_INTERVAL_SECONDS`, which tolerates a rotation without letting a
flood of wrong secrets reach the store.

No part of the value ever enters an exception, a log field, or a repr: shape failures name
the defect only, and the JSON parser's own error is dropped rather than chained.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import SecretStr

from adapters.outbound.aws_errors import aws_failure_context
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.client_secret import ClientSecret, ClientSecretPort

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_secretsmanager.client import SecretsManagerClient

__all__ = [
    "CLIENT_SECRET_KEY",
    "CLIENT_SECRET_UNAVAILABLE",
    "REFRESH_INTERVAL_SECONDS",
    "RefreshGate",
    "SecretsManagerClientSecret",
    "client_secret_from",
]

CLIENT_SECRET_UNAVAILABLE = ErrorCode("TOKENSVC", 4)

# pragma: allowlist nextline secret (A-PROTOCOL-CONSTANT-001)
CLIENT_SECRET_KEY = "client_secret"  # noqa: S105 — the JSON key name, not a value
"""The JSON key the deploy-time generated secret and the credential provider both use."""

_UNAVAILABLE = "The token service is temporarily unavailable."


def client_secret_from(secret_string: str) -> ClientSecret:
    """Parse the stored `{"client_secret": "<non-empty string>"}` document.

    Raises DependencyError naming only the defect. The parser error is suppressed because its
    message can quote the document it failed on.
    """
    try:
        document: object = json.loads(secret_string)
    except ValueError:
        raise DependencyError(
            CLIENT_SECRET_UNAVAILABLE, _UNAVAILABLE, context={"cause": "secret_not_json"}
        ) from None
    if not isinstance(document, dict):
        raise DependencyError(
            CLIENT_SECRET_UNAVAILABLE, _UNAVAILABLE, context={"cause": "secret_not_object"}
        )
    value = cast("dict[str, object]", document).get(CLIENT_SECRET_KEY)
    if not isinstance(value, str) or not value:
        raise DependencyError(
            CLIENT_SECRET_UNAVAILABLE, _UNAVAILABLE, context={"cause": "secret_key_absent"}
        )
    return ClientSecret(SecretStr(value))


REFRESH_INTERVAL_SECONDS: Final = 60.0
"""The shortest gap between two store reads a mismatched secret can cause, per container.

The client id is public, so any caller can present it with a wrong secret. Without this bound
every such request would read Secrets Manager; with it, a flood costs one read a minute, and a
rotated secret is still picked up within a minute of the first request that presents it.
"""


@dataclass(slots=True)
class RefreshGate:
    """Admit at most one store read per interval, measured on a monotonic clock."""

    interval_seconds: float
    clock: Callable[[], float] = time.monotonic
    _last_read: float | None = None

    def ready(self) -> bool:
        """Return whether the interval since the last recorded read has passed."""
        last = self._last_read
        return last is None or self.clock() - last >= self.interval_seconds

    def record(self) -> None:
        """Record that a read went to the store now, whether or not it succeeded."""
        self._last_read = self.clock()


class SecretsManagerClientSecret(ClientSecretPort):
    """Read one secret by ARN and cache its client secret. One client per process."""

    def __init__(
        self, secret_id: str, *, region: str, client: SecretsManagerClient | None = None
    ) -> None:
        if not secret_id.strip() or not region.strip():
            raise ValueError("A secret id and region are required.")
        self._secret_id = secret_id
        self._cached: ClientSecret | None = None
        self._gate = RefreshGate(REFRESH_INTERVAL_SECONDS)
        self._client: SecretsManagerClient = (
            client
            if client is not None
            else boto3.client(
                "secretsmanager",
                region_name=region,
                config=Config(
                    connect_timeout=2,
                    read_timeout=5,
                    retries={"mode": "standard", "total_max_attempts": 3},
                ),
            )
        )

    def current(self) -> ClientSecret:
        """Return the cached secret, reading the store only on first use."""
        cached = self._cached
        return cached if cached is not None else self._read()

    def refreshed(self) -> ClientSecret:
        """Read the store again unless it was read within the refresh interval.

        Within the interval the cache is returned unchanged, so a flood of wrong secrets under
        the public client id costs no store reads. With nothing cached yet it always reads.
        """
        cached = self._cached
        if cached is not None and not self._gate.ready():
            return cached
        return self._read()

    def _read(self) -> ClientSecret:
        """Read the store, replace the cache, and return the value it holds now.

        A failure leaves the previous cache in place and is not cached itself, so the next
        request retries rather than serving nothing.
        """
        self._gate.record()
        try:
            response = self._client.get_secret_value(SecretId=self._secret_id)
        except (BotoCoreError, ClientError) as error:
            raise DependencyError(
                CLIENT_SECRET_UNAVAILABLE, _UNAVAILABLE, context=aws_failure_context(error)
            ) from error
        secret_string = response.get("SecretString")
        if not secret_string:
            raise DependencyError(
                CLIENT_SECRET_UNAVAILABLE, _UNAVAILABLE, context={"cause": "secret_not_text"}
            )
        secret = client_secret_from(secret_string)
        self._cached = secret
        return secret
