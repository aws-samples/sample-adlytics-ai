# SPDX-License-Identifier: MIT-0
"""Client secret port: the agent exchange's one registered client credential (ADR-077 S4).

The token service authenticates AgentCore Identity's credential provider by `client_secret_post`.
The secret is generated at deploy time and held in a secret store; this port reads it without
naming the store. The value crosses as `ClientSecret`, which never reaches repr and is compared
only in constant time.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import SecretStr

__all__ = ["ClientSecret", "ClientSecretPort"]


@dataclass(frozen=True, slots=True)
class ClientSecret:
    """One client secret value, masked in every representation."""

    value: SecretStr

    def __post_init__(self) -> None:
        if not self.value.get_secret_value():
            raise ValueError("A client secret cannot be empty.")

    def matches(self, presented: str) -> bool:
        """Compare in constant time, over UTF-8 bytes so any presented text is comparable."""
        return hmac.compare_digest(
            self.value.get_secret_value().encode("utf-8"), presented.encode("utf-8")
        )


@runtime_checkable
class ClientSecretPort(Protocol):
    """Read the registered client secret, cached per process.

    Every method raises DependencyError when the store fails, refuses, or holds a malformed
    value, and never leaks an SDK exception type or any part of the value.
    """

    def current(self) -> ClientSecret:
        """Return the cached secret, reading the store only on first use."""
        ...

    def refreshed(self) -> ClientSecret:
        """Re-read the store unless it was read within the adapter's refresh interval.

        Replaces the cache when it reads, and returns what the cache holds after.
        """
        ...
