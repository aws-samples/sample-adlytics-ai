# SPDX-License-Identifier: MIT-0
"""Operator directory port: resolve prompt authors' opaque subjects to email addresses.

The versions read is the only caller (ADR-069). Resolutions are returned to prompt-admin
operators only and are never stored or logged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from domain.identity.operator_directory import OperatorEmailResolution


class OperatorDirectoryPort(Protocol):
    """Look up operator email addresses by Cognito subject."""

    def email_addresses(self, subjects: frozenset[str]) -> Mapping[str, OperatorEmailResolution]:
        """Return one resolution per requested subject; never raises for a lookup failure."""
        ...
