# SPDX-License-Identifier: MIT-0
"""The one digest algorithm for the model artifact, shared by the operator command and the job.

`roster_digest(entries)` = SHA-256 of the canonical JSON of the sorted (relative path, byte
length, SHA-256 of contents) of every file under the artifact prefix (design §4b, R1-16).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RosterEntry:
    """One artifact file: its path relative to the prefix, its size and its content digest."""

    path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if not self.path or self.path.startswith("/") or ".." in self.path.split("/"):
            raise ValueError(f"artifact path must be relative and inside the prefix: {self.path!r}")
        if self.size < 0:
            raise ValueError("artifact size must not be negative")
        if _HEX64.fullmatch(self.sha256) is None:
            raise ValueError("artifact sha256 must be 64 lowercase hex characters")


def roster_digest(entries: Iterable[RosterEntry]) -> str:
    """Return the artifact digest; two entries with one path are refused."""
    ordered = sorted(entries, key=lambda entry: entry.path)
    paths = [entry.path for entry in ordered]
    if len(paths) != len(set(paths)):
        raise ValueError("an artifact path appears twice")
    if not ordered:
        raise ValueError("an artifact needs at least one file")
    body = [[entry.path, entry.size, entry.sha256] for entry in ordered]
    canonical = json.dumps(body, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
