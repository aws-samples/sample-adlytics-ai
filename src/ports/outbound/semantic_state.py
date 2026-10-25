# SPDX-License-Identifier: MIT-0
"""Persistence boundaries for Semantic Architecture V2 publication state.

Separate protocols because their authority differs. An operational writer appends attempt events; a
publication owner may acquire a lease and commit a successful publication; analytical readers may
read successful current publications and exact coverage and must never acquire a lease or inspect
attempt internals merely because the strings sit in the same configuration object.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, cast, runtime_checkable

from domain.semantics.publication import (
    AttemptEvent,
    AttemptEventKind,
    AttemptId,
    CanonicalPublisherAuthority,
    LeaseGrant,
    LeaseRequest,
    PartitionIdentity,
    PublicationScope,
    SuccessfulPublication,
)

__all__ = [
    "MAX_COMMIT_GROUP_MEMBERS",
    "MAX_EXACT_CURRENT_PARTITIONS",
    "AttemptStorePort",
    "CommitPublicationGroupRequest",
    "CommitPublicationRequest",
    "CurrentPublicationInspectionPort",
    "CurrentPublicationPort",
    "InspectExactResult",
    "LeaseAcquireOutcome",
    "LeaseStorePort",
    "PublicationStorePort",
    "ScopeHorizon",
    "ScopeHorizonPolicy",
    "StateWriteOutcome",
    "scope_horizon_policy",
]


@dataclass(frozen=True, slots=True)
class StateWriteOutcome:
    """Whether a conditional state mutation was accepted, and why if not."""

    accepted: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.accepted and not self.reason:
            raise ValueError("a refused semantic-state write must state why.")


@dataclass(frozen=True, slots=True)
class LeaseAcquireOutcome:
    """A lease grant or a stated contention outcome."""

    grant: LeaseGrant | None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.grant is None and not self.reason:
            raise ValueError("a refused lease acquisition must state why.")
        if self.grant is not None and self.reason:
            raise ValueError("a granted lease cannot also carry a refusal reason.")


@dataclass(frozen=True, slots=True)
class CommitPublicationRequest:
    """One immutable success, terminal event, and grant for one atomic CURRENT transition."""

    publication: SuccessfulPublication
    lease: LeaseGrant
    authority: CanonicalPublisherAuthority
    success_event: AttemptEvent | None = None

    def __post_init__(self) -> None:
        event = self.success_event
        if event is None:
            event = AttemptEvent(
                attempt_id=self.publication.attempt_id,
                partition=self.publication.partition,
                kind=AttemptEventKind.SUCCEEDED,
                occurred_at=self.publication.published_at,
                sequence=1,
            )
            object.__setattr__(self, "success_event", event)
        if (
            event.kind is not AttemptEventKind.SUCCEEDED
            or event.sequence != 1
            or event.attempt_id != self.publication.attempt_id
            or event.partition != self.publication.partition
            or event.reason is not None
            or event.occurred_at != self.publication.published_at
        ):
            raise ValueError(
                "publication commit requires its exact successful terminal attempt event."
            )
        if not self.publication.evidence.publishable:
            raise ValueError("only modern publishable evidence may advance CURRENT.")
        if self.authority.dataset != self.publication.partition.scope.dataset:
            raise ValueError("publisher authority is for another canonical dataset.")
        if self.authority.publisher != self.publication.publisher:
            raise ValueError("publisher authority belongs to another component.")


MAX_COMMIT_GROUP_MEMBERS = 20
MAX_EXACT_CURRENT_PARTITIONS = 4_800
"""The most partitions one verified exact read attests: a cost bound, not a consistency one.

The read is ADR-070's chunked two-pass snapshot: heads, immutables and heads again, each in
transactions of at most 100 items, accepted only when both head passes agree. Forward-only heads
make that agreement one instant at any size; DynamoDB calls grow by one per 100 partitions per pass.

Derived (ADR-070, 2026-10-25): the largest N whose worst serving read, 3 attempts x 3 x
ceil(N / 100) transactions, fits the tightest caller budget, chat's quick turn (120 s,
`agent.quickChat.timeoutSeconds`), at the slowest per-transaction latency measured on dev,
276.0 ms: floor(120,000 / (9 x 276.0)) = 48 transactions per pass, so N = 4,800. A slower
measurement or a tighter turn budget re-derives it.
"""


@dataclass(frozen=True, slots=True)
class ScopeHorizon:
    """The bounded part of a scope a reader declares it needs (plan A5.1 I9).

    Exactly one of `from_bucket_start` (an inclusive lower bound on `bucket_start`) or `newest`
    (a newest-first cap; `newest=1` is the scope's latest publication). A whole-scope read with
    no horizon is unbounded and is never pinned by a bake.
    """

    from_bucket_start: date | None = None
    newest: int | None = None

    def __post_init__(self) -> None:
        if (self.from_bucket_start is None) == (self.newest is None):
            raise ValueError("a scope horizon declares exactly one of from_bucket_start, newest.")
        if self.newest is not None and (isinstance(self.newest, bool) or self.newest < 1):
            raise ValueError("a newest-first horizon reads at least one publication.")

    @property
    def token(self) -> str:
        """The canonical key element a bake pin stores beside the scope identity."""
        if self.newest is not None:
            return f"newest={self.newest}"
        return f"from={cast('date', self.from_bucket_start).isoformat()}"


@runtime_checkable
class ScopeHorizonPolicy(Protocol):
    """Who knows a bake row's declared source horizon (the bake's CURRENT reader).

    Outside a bake it returns None, so a whole-scope read keeps today's unbounded answer.
    """

    def window_horizon(self, longest_bucket_days: int) -> ScopeHorizon | None:
        """Return the row's windowed horizon, shifted back by the pack's longest bucket."""
        ...


def scope_horizon_policy(reader: object) -> ScopeHorizonPolicy | None:
    """Return a CURRENT reader that declares bake horizons, or None for a plain reader."""
    return reader if isinstance(reader, ScopeHorizonPolicy) else None


@dataclass(frozen=True, slots=True)
class InspectExactResult:
    """One attested publication-or-absence slot per requested partition, in request order."""

    publications: tuple[SuccessfulPublication | None, ...]

    def __post_init__(self) -> None:
        if not self.publications or len(self.publications) > MAX_EXACT_CURRENT_PARTITIONS:
            raise ValueError(
                f"exact CURRENT inspection requires 1-{MAX_EXACT_CURRENT_PARTITIONS} slots."
            )


@dataclass(frozen=True, slots=True)
class CommitPublicationGroupRequest:
    """One all-or-none set of at most 20 exact publication and attempt transitions."""

    members: tuple[CommitPublicationRequest, ...]

    def __post_init__(self) -> None:
        if not self.members:
            raise ValueError("publication group requires at least one member.")
        if len(self.members) > MAX_COMMIT_GROUP_MEMBERS:
            raise ValueError(f"publication group cannot exceed {MAX_COMMIT_GROUP_MEMBERS} members.")
        partitions = tuple(member.publication.partition for member in self.members)
        if len(set(partitions)) != len(partitions):
            raise ValueError("publication group repeats a canonical partition.")
        if tuple(sorted(partition.identity for partition in partitions)) != tuple(
            partition.identity for partition in partitions
        ):
            raise ValueError("publication group members must be sorted by partition identity.")


@runtime_checkable
class AttemptStorePort(Protocol):
    """Append-only operational history, isolated from analytical readers."""

    def append(self, event: AttemptEvent) -> StateWriteOutcome:
        """Append one immutable attempt event, idempotently refusing a different duplicate."""
        ...

    def events_for(self, attempt_id: AttemptId) -> tuple[AttemptEvent, ...]:
        """Read one attempt's events in sequence order."""
        ...


@runtime_checkable
class LeaseStorePort(Protocol):
    """Expiring fenced authority to publish one canonical partition."""

    def acquire(self, request: LeaseRequest) -> LeaseAcquireOutcome:
        """Acquire or renew authority atomically, returning a monotonically higher fence."""
        ...

    def release(self, grant: LeaseGrant, *, at: datetime) -> StateWriteOutcome:
        """Expire the lease only when holder and fence still match the grant."""
        ...


@runtime_checkable
class CurrentPublicationPort(Protocol):
    """Read-only authority over strongly selected successful CURRENT publications."""

    def current(self, partition: PartitionIdentity) -> SuccessfulPublication | None:
        """Attest one exact CURRENT pointer against its immutable publication."""
        ...

    def current_exact(
        self,
        partitions: tuple[PartitionIdentity, ...],
    ) -> tuple[SuccessfulPublication, ...] | None:
        """Attest exact pointer pairs; any absent CURRENT returns None, corruption refuses."""
        ...

    def current_for_scope(
        self, scope: PublicationScope, horizon: ScopeHorizon | None = None
    ) -> tuple[SuccessfulPublication, ...]:
        """Discover scope keys and attest every CURRENT/immutable publication pair.

        `horizon` bounds the read (plan A5.1 I9): `from_bucket_start` keeps the buckets starting
        on or after it, `newest` the newest ones. None is today's unbounded read, which every
        non-bake caller keeps; a bake never passes None.
        """
        ...


@runtime_checkable
class CurrentPublicationInspectionPort(CurrentPublicationPort, Protocol):
    """Read-only authority that preserves independently attested exact absence slots."""

    def inspect_exact(
        self,
        partitions: tuple[PartitionIdentity, ...],
    ) -> InspectExactResult:
        """Return one ordered attested publication-or-stable-absence slot per partition."""
        ...

    def earliest_current_for_scope(self, scope: PublicationScope) -> SuccessfulPublication | None:
        """Attest only the scope's earliest-bucket CURRENT publication, or None when it has none.

        Reads the scope in bucket-start order and stops at the first attested publication, under a
        fixed total bound on reads, so the cost doesn't grow with the scope's retained history
        (`current_for_scope` reads it all). None also when no attested publication lies within that
        bound: a caller names no first day rather than reading further.
        """
        ...


@runtime_checkable
class PublicationStorePort(CurrentPublicationInspectionPort, Protocol):
    """Immutable successes plus strongly selected current partition versions."""

    def validate_group(self, request: CommitPublicationGroupRequest) -> None:
        """Reject a group whose exact persisted representation cannot transact safely."""
        ...

    def commit(self, request: CommitPublicationRequest) -> StateWriteOutcome:
        """Atomically record publication, CURRENT, and exact SUCCEEDED attempt evidence."""
        ...

    def commit_group(self, request: CommitPublicationGroupRequest) -> StateWriteOutcome:
        """Atomically record/advance all members and their exact terminal attempt evidence."""
        ...
