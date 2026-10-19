# SPDX-License-Identifier: MIT-0
"""Immutable, content-addressed store for identity-mapping claim-check documents.

The port exposes no raw read: `read` returns the mapping list only after the adapter has
recomputed the digest through `verify_identity_mappings`, so every reader fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from domain.semantics.identity_evidence import IdentityMappingsClaim, IdentityMappingsDocument

__all__ = ["IdentityEvidencePort", "IdentityEvidenceWriteResult"]


@dataclass(frozen=True, slots=True)
class IdentityEvidenceWriteResult:
    """Whether the document was created, or already existed with exactly these bytes."""

    accepted: bool
    created: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.accepted and not self.reason:
            raise ValueError("a refused identity-evidence write must state why.")
        if self.created and not self.accepted:
            raise ValueError("a refused identity-evidence write cannot be created.")


@runtime_checkable
class IdentityEvidencePort(Protocol):
    """Write once under the document's digest; read only through digest verification."""

    def put(self, document: IdentityMappingsDocument) -> IdentityEvidenceWriteResult:
        """Create the document, or accept a byte-identical existing one; never overwrite."""
        ...

    def read(self, claim: IdentityMappingsClaim) -> tuple[dict[str, object], ...]:
        """Return the verified full mapping list, raising the exact refusal otherwise."""
        ...
