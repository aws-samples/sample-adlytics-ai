# SPDX-License-Identifier: MIT-0
"""N1 root identity, the conflict unit `U` and the input revision `r` of one manifest (V-7).

- `U` is the dataset window: the manifest key with its last `retrieved=<instant>/` segment and
  file name removed. Two retrievals of one option set are versions of one thing (ADR-030), so
  the window is the unit with newer and older inputs. Windows that differ only in `options=`
  are different units.
- `r` is the retrieval instant in the key's `retrieved=` segment, at one-second resolution. It
  is derived from the key and never stored as a position, so noncurrent-version expiry cannot
  move it. The exact-version manifest body must declare the same instant.
- The rewrite check keeps only listing entries whose key equals the key exactly, because a
  prefix listing also returns longer keys. More than one exact version is a rewrite.
- The root flow is the manifest's exact version: one root per (key, VersionId).

Pure: no I/O, no clock, no AWS.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from domain.operations.status import canonical_authority_digest
from domain.shared_kernel.errors import ErrorCode, ValidationError

__all__ = [
    "MANIFEST_KEY_BODY_MISMATCH",
    "RETRIEVED_FORMAT",
    "SCHEMA_INVALID",
    "ManifestVersion",
    "RootRefusedError",
    "WindowIdentity",
    "exact_version_count",
    "manifest_fingerprint",
    "require_body_agrees",
    "root_flow_id",
    "window_identity",
]

RETRIEVED_FORMAT: Final = "%Y%m%dT%H%M%SZ"
SCHEMA_INVALID: Final = "SCHEMA_INVALID"
MANIFEST_KEY_BODY_MISMATCH: Final = "MANIFEST_KEY_BODY_MISMATCH"
_ROOT_NAMESPACE: Final = uuid.UUID("5b0f39f2-4a59-5b1f-9f42-6a1d2f7c3e10")
_RETRIEVED: Final = re.compile(r"^(?P<window>.+/)retrieved=(?P<instant>\d{8}T\d{6}Z)/[^/]+$")


class RootRefusedError(ValidationError):
    """A pre-domain refusal of the manifest's identity: no receipt, zero graph writes."""

    def __init__(self, signal: str, description: str) -> None:
        code = 1 if signal == SCHEMA_INVALID else 2
        super().__init__(ErrorCode("RUN_ROOT", code), description)
        self.signal = signal


@dataclass(frozen=True, slots=True)
class WindowIdentity:
    """`U` and `r` for one manifest key."""

    key: str
    window: str
    retrieved_at: datetime

    @property
    def conflict_unit(self) -> str:
        """The stable digest of `U`, used as the slot, waiter and head key."""
        return canonical_authority_digest(["conflict-unit/v1", self.window])


@dataclass(frozen=True, slots=True)
class ManifestVersion:
    """One exact manifest version, the authority every ingestion request names."""

    key: str
    version_id: str


def window_identity(key: str) -> WindowIdentity:
    """Derive `U` and `r` from the key, or refuse a key without one `retrieved=` segment."""
    match = _RETRIEVED.fullmatch(key)
    if match is None or "retrieved=" in match.group("window"):
        raise RootRefusedError(
            SCHEMA_INVALID, "a manifest key carries exactly one retrieved= segment."
        )
    try:
        instant = datetime.strptime(match.group("instant"), RETRIEVED_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise RootRefusedError(SCHEMA_INVALID, "the retrieved= instant is invalid.") from None
    return WindowIdentity(key=key, window=match.group("window"), retrieved_at=instant)


def require_body_agrees(identity: WindowIdentity, body_retrieved_at: datetime) -> None:
    """Refuse a manifest whose body declares another retrieval instant than its key (V-7c)."""
    if body_retrieved_at.tzinfo is None or (
        body_retrieved_at.astimezone(UTC).replace(microsecond=0) != identity.retrieved_at
        or body_retrieved_at.microsecond
    ):
        raise RootRefusedError(
            MANIFEST_KEY_BODY_MISMATCH, "the manifest body disagrees with its key."
        )


def exact_version_count(key: str, listed_keys: Iterable[str]) -> int:
    """Count listing entries for exactly this key; longer keys under the prefix are ignored."""
    return sum(1 for listed in listed_keys if listed == key)


def root_flow_id(version: ManifestVersion) -> str:
    """Derive the root flow of one exact manifest version (FR-1, N1)."""
    return str(uuid.uuid5(_ROOT_NAMESPACE, f"{version.key}\x1f{version.version_id}"))


def manifest_fingerprint(version: ManifestVersion, template: str, contract_revision: int) -> str:
    """`F`: the manifest authority digest plus template and contract revisions.

    The contract enters by its reviewed `contract_revision` only, so an edit to the contract
    file that is not a new revision leaves every `F` unchanged (rows 7-9 keep joining).
    """
    return canonical_authority_digest(
        [
            "run-request-fingerprint/v1",
            version.key,
            version.version_id,
            template,
            f"contract_revision={contract_revision}",
        ]
    )
