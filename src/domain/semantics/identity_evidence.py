# SPDX-License-Identifier: MIT-0
"""Claim-check form of a candidate's selected identity mappings (write-discipline §2.6).

The `source.<contract>.identity.v1` rule result used to carry every selected mapping inline.
Its size grows with the campaigns and products in one bucket, and the result travels into the
DynamoDB publication record, whose transaction items have a fixed byte bound: a 57-campaign
conversion bucket (79 mappings) produced a 33,762-byte record against the 32,000-byte bound.

The rule result now carries only a claim: the mapping count, the SHA-256 of one canonical
document holding the full list, and the document schema. The document is an immutable,
content-addressed object the writer stores before any record that names it. Every reader goes
through `verify_identity_mappings`, which recomputes the digest and fails closed; a reader never
falls back to a partial or inline list. The superseded inline form is refused, not decoded.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from domain.shared_kernel.errors import ErrorCode, InvariantError

if TYPE_CHECKING:  # pragma: no cover
    from domain.semantics.canonical_compiler import IdentityMappingEvidence

__all__ = [
    "IDENTITY_CLAIM_PREFIX",
    "IDENTITY_EVIDENCE_ABSENT",
    "IDENTITY_EVIDENCE_DIGEST_MISMATCH",
    "IDENTITY_EVIDENCE_KEY_CONFLICT",
    "IDENTITY_EVIDENCE_MALFORMED",
    "IDENTITY_EVIDENCE_UNPREPARED",
    "IDENTITY_MAPPINGS_SCHEMA",
    "IdentityMappingsClaim",
    "IdentityMappingsDocument",
    "identity_mappings_document",
    "parse_identity_claim",
    "verify_identity_mappings",
]

IDENTITY_MAPPINGS_SCHEMA: Final = "adlytics.identity-mappings.v1"
IDENTITY_CLAIM_PREFIX: Final = "Governed identity mappings selected by claim check: "

IDENTITY_EVIDENCE_MALFORMED: Final = ErrorCode("IDENTITY_EVIDENCE", 1)
"""A claim or its document is not the claim-check form (including the superseded inline form)."""
IDENTITY_EVIDENCE_ABSENT: Final = ErrorCode("IDENTITY_EVIDENCE", 2)
"""The content-addressed document a claim names does not exist."""
IDENTITY_EVIDENCE_DIGEST_MISMATCH: Final = ErrorCode("IDENTITY_EVIDENCE", 3)
"""The stored document's SHA-256 is not the digest the claim names."""
IDENTITY_EVIDENCE_KEY_CONFLICT: Final = ErrorCode("IDENTITY_EVIDENCE", 4)
"""A write found the content-addressed key already holding other bytes; nothing is overwritten."""
IDENTITY_EVIDENCE_UNPREPARED: Final = ErrorCode("IDENTITY_EVIDENCE", 5)
"""A candidate's evidence names a claim whose document this delivery did not prepare."""

_DIGEST: Final = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class IdentityMappingsClaim:
    """What a publication record carries: the count and digest of the full mapping list."""

    count: int
    sha256: str

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("identity mapping claim count cannot be negative.")
        if not _DIGEST.fullmatch(self.sha256):
            raise ValueError("identity mapping claim digest must be 64 lowercase hex.")

    @property
    def detail(self) -> str:
        """The rule-result detail: a fixed-size claim, independent of the mapping count."""
        return IDENTITY_CLAIM_PREFIX + json.dumps(
            {"count": self.count, "schema": IDENTITY_MAPPINGS_SCHEMA, "sha256": self.sha256},
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class IdentityMappingsDocument:
    """The canonical bytes a claim addresses, with the claim derived from them."""

    claim: IdentityMappingsClaim
    body: bytes

    def __post_init__(self) -> None:
        if hashlib.sha256(self.body).hexdigest() != self.claim.sha256:
            raise ValueError("identity mapping document bytes do not match their claim.")


def _mapping_payload(item: IdentityMappingEvidence) -> dict[str, object]:
    return {
        "source": f"{item.source_namespace}:{item.source_value}",
        "source_system_key": item.source_system_key,
        "source_scope": (
            None
            if item.source_scope_namespace is None
            else {"namespace": item.source_scope_namespace, "value": item.source_scope_value}
        ),
        "source_identifier_kind": item.source_identifier_kind,
        "target": item.target_value,
        "mapping_version": item.mapping_version,
        "valid_from": item.valid_from.isoformat(),
        "valid_to": None if item.valid_to is None else item.valid_to.isoformat(),
    }


def identity_mappings_document(
    mappings: tuple[IdentityMappingEvidence, ...],
) -> IdentityMappingsDocument:
    """Serialize the mappings in their given (compiler-sorted) order as one canonical document."""
    body = json.dumps(
        {
            "mappings": [_mapping_payload(item) for item in mappings],
            "schema": IDENTITY_MAPPINGS_SCHEMA,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    claim = IdentityMappingsClaim(len(mappings), hashlib.sha256(body).hexdigest())
    return IdentityMappingsDocument(claim, body)


def _malformed(description: str) -> InvariantError:
    return InvariantError(IDENTITY_EVIDENCE_MALFORMED, description)


def parse_identity_claim(detail: str) -> IdentityMappingsClaim:
    """Read the claim out of one identity rule-result detail, refusing every other form."""
    if not detail.startswith(IDENTITY_CLAIM_PREFIX):
        raise _malformed("The identity rule result is not an identity-mapping claim check.")
    try:
        value: object = json.loads(detail.removeprefix(IDENTITY_CLAIM_PREFIX))
    except ValueError:
        raise _malformed("The identity-mapping claim is not valid JSON.") from None
    if not isinstance(value, dict):
        raise _malformed("The identity-mapping claim has an unexpected shape or schema.")
    fields = cast("dict[str, object]", value)
    count = fields.get("count")
    digest = fields.get("sha256")
    if (
        set(fields) != {"count", "schema", "sha256"}
        or fields.get("schema") != IDENTITY_MAPPINGS_SCHEMA
        or not isinstance(count, int)
        or isinstance(count, bool)
        or not isinstance(digest, str)
    ):
        raise _malformed("The identity-mapping claim has an unexpected shape or schema.")
    try:
        claim = IdentityMappingsClaim(count, digest)
    except ValueError:
        raise _malformed("The identity-mapping claim has an invalid count or digest.") from None
    if claim.detail != detail:
        raise _malformed("The identity-mapping claim is not in its canonical form.")
    return claim


def verify_identity_mappings(
    claim: IdentityMappingsClaim, body: bytes | None
) -> tuple[dict[str, object], ...]:
    """Return the full mapping list only when the stored bytes are exactly what the claim names."""
    if body is None:
        raise InvariantError(
            IDENTITY_EVIDENCE_ABSENT,
            "The identity-mapping claim-check document is absent.",
            context={"sha256": claim.sha256},
        )
    if hashlib.sha256(body).hexdigest() != claim.sha256:
        raise InvariantError(
            IDENTITY_EVIDENCE_DIGEST_MISMATCH,
            "The identity-mapping document's digest differs from its claim.",
            context={"sha256": claim.sha256},
        )
    try:
        value: object = json.loads(body)
    except ValueError:
        raise _malformed("The identity-mapping document is not valid JSON.") from None
    if not isinstance(value, dict):
        raise _malformed("The identity-mapping document has an unexpected shape or schema.")
    document = cast("dict[str, object]", value)
    listed = document.get("mappings")
    if (
        set(document) != {"mappings", "schema"}
        or document.get("schema") != IDENTITY_MAPPINGS_SCHEMA
        or not isinstance(listed, list)
    ):
        raise _malformed("The identity-mapping document has an unexpected shape or schema.")
    mappings = tuple(cast("list[object]", listed))
    if not all(isinstance(item, dict) for item in mappings):
        raise _malformed("The identity-mapping document has an unexpected shape or schema.")
    if len(mappings) != claim.count:
        raise _malformed("The identity-mapping document's count differs from its claim.")
    return cast("tuple[dict[str, object], ...]", mappings)
