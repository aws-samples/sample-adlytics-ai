# SPDX-License-Identifier: MIT-0
"""Harness token port: exchange the caller's verified Cognito token for a Harness token.

ADR-077 §2 makes the route the Harness's only caller: each turn exchanges the user's live
access token at the token service's route exchange and presents what comes back. Neither
token is a domain value, so both cross this port as opaque strings that never reach repr.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class HarnessToken:
    """One minted, single-turn Harness bearer token. Never cached, logged, or returned."""

    access_token: str = field(repr=False)
    expires_in: int

    def __post_init__(self) -> None:
        if not self.access_token:
            raise ValueError("A Harness token requires an access_token.")
        if isinstance(self.expires_in, bool) or self.expires_in < 1:
            raise ValueError("expires_in must be a positive number of seconds.")


@runtime_checkable
class HarnessTokenPort(Protocol):
    """Exchange one subject token for one Harness token.

    Raises IdentityError when the token service refuses the subject (`invalid_grant`), and
    DependencyError when the exchange could not be completed. Neither carries token content.
    """

    def exchange(self, subject_token: str) -> HarnessToken:
        """Return a Harness token bound to the subject token's verified `sub`."""
        ...
