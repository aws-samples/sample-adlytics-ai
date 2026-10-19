# SPDX-License-Identifier: MIT-0
"""Pure ADR-075 §5 table-lease authority: the closed ``TABLE#`` lease-record form.

Table leases live in the separate leases table under the single-attribute key
``partition_key = TABLE#<physical table id>``; there is no record key. The item
carries exactly ``holder``, the monotonic ``fencing_token``, ``expires_at`` as
canonical ISO-8601 UTC text (compared lexically by the store), and, only while
the fenced attempt is parked, one canonical 20-digit ``parked_at`` instant. The
coordinator owns every acquire, release, and park mutation; this module owns the
closed record form, the canonical key, the pinned released-expiry instant, and
the exact expiry-to-epoch-microseconds conversion Begin uses for the initial
reconcile due instant. No clock, network, or AWS type enters here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from domain.operations.projection_handoff import canonical_json
from domain.operations.table_attempt import (
    RunIdentityRequest,
    is_canonical_instant,
    is_canonical_table_id,
)

__all__ = [
    "RELEASED_LEASE_EXPIRY",
    "TABLE_LEASE_PREFIX",
    "TableLeaseError",
    "TableLeaseRecord",
    "canonical_lease_instant",
    "inline_lease_holder",
    "is_canonical_lease_expiry",
    "lease_expiry_epoch_micros",
    "queued_lease_holder",
    "table_lease_partition_key",
]

TABLE_LEASE_PREFIX: Final = "TABLE#"
# The canonical released instant: the epoch, rendered exactly as the store writes
# UTC moments, so a released lease sorts lexically before every live expiry.
RELEASED_LEASE_EXPIRY: Final = "1970-01-01T00:00:00+00:00"

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND: Final = timedelta(microseconds=1)
_MICROS_DIGITS: Final = 20


class TableLeaseError(ValueError):
    """A table-lease record, key, or instant violates the closed §5 contract."""


def table_lease_partition_key(table_id: str) -> str:
    """Return ``TABLE#<physical table id>`` for a canonical table id."""
    if not is_canonical_table_id(table_id):
        raise TableLeaseError("physical table id must be a canonical database-qualified id.")
    return f"{TABLE_LEASE_PREFIX}{table_id}"


def queued_lease_holder(plan_key: str, unit_key: str, writer_execution_id: str) -> str:
    """Derive the §5 queued holder ``sha256(canonical_json([plan, unit, writer]))``."""
    if not plan_key.strip() or not unit_key.strip() or not writer_execution_id.strip():
        raise TableLeaseError("a queued holder derives from three nonblank identities.")
    return hashlib.sha256(canonical_json([plan_key, unit_key, writer_execution_id])).hexdigest()


def inline_lease_holder(run: RunIdentityRequest) -> str:
    """Derive the §5 inline holder ``sha256(canonical_json(["inline", kind, op, exec, table]))``.

    The holder shares the validated immutable ``run_id`` tuple, so the same request
    names both the run and its lease holder.
    """
    return hashlib.sha256(
        canonical_json(
            [
                "inline",
                run.writer_kind.value,
                run.operation_id,
                run.execution_id,
                run.physical_table_id,
            ]
        )
    ).hexdigest()


def is_canonical_lease_expiry(value: str) -> bool:
    """Whether text is a stored lease expiry: UTC ISO-8601 with offset, byte-canonical."""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return False
    if moment.tzinfo is None:
        return False
    return moment.astimezone(UTC).isoformat() == value


def lease_expiry_epoch_micros(value: str) -> int:
    """Convert a canonical lease expiry to UTC epoch microseconds by integer arithmetic."""
    if not is_canonical_lease_expiry(value):
        raise TableLeaseError("lease expiry must be canonical UTC ISO-8601 text.")
    micros = (datetime.fromisoformat(value) - _EPOCH) // _MICROSECOND
    if micros < 0 or len(f"{micros:020d}") != _MICROS_DIGITS:
        raise TableLeaseError("lease expiry is outside the 20-digit epoch-microsecond range.")
    return micros


def canonical_lease_instant(epoch_micros_text: str) -> str:
    """Render a canonical 20-digit epoch-microseconds instant as canonical lease-expiry text.

    The exact inverse of :func:`lease_expiry_epoch_micros` by integer arithmetic, so
    the store's lexical ``expires_at <= <instant>`` comparison against a rendered
    operation instant orders exactly as the underlying moments do.
    """
    if not is_canonical_instant(epoch_micros_text):
        raise TableLeaseError("lease instant must be a 20-digit epoch-microseconds instant.")
    return (_EPOCH + int(epoch_micros_text) * _MICROSECOND).isoformat()


@dataclass(frozen=True, slots=True)
class TableLeaseRecord:
    """One strongly read ``TABLE#`` lease record in its closed §5 form."""

    physical_table_id: str
    holder: str
    fencing_token: int
    expires_at: str
    parked_at: str | None = None

    def __post_init__(self) -> None:
        if not is_canonical_table_id(self.physical_table_id):
            raise TableLeaseError("physical table id must be a canonical database-qualified id.")
        if not self.holder.strip():
            raise TableLeaseError("lease holder must be nonblank text.")
        if isinstance(self.fencing_token, bool) or self.fencing_token < 1:
            raise TableLeaseError("fencing token must be a positive integer.")
        if not is_canonical_lease_expiry(self.expires_at):
            raise TableLeaseError("lease expiry must be canonical UTC ISO-8601 text.")
        if self.parked_at is not None and not is_canonical_instant(self.parked_at):
            raise TableLeaseError("parked_at must be a 20-digit epoch-microseconds instant.")

    @property
    def partition_key(self) -> str:
        """The canonical single-attribute leases-table key."""
        return table_lease_partition_key(self.physical_table_id)

    def expiry_epoch_micros(self) -> int:
        """Return the lease expiry as UTC epoch micros (Begin's initial reconcile due)."""
        return lease_expiry_epoch_micros(self.expires_at)
