# SPDX-License-Identifier: MIT-0
"""DynamoDB persistence for Semantic Architecture V2 publication state.

Three tables and three authorities:

- attempts append immutable operational events;
- leases conditionally grant expiring fenced publication authority; and
- publications store immutable successes plus one current pointer per exact partition.

The publications GSI discovers current partition keys for a scope. A GSI is eventually
consistent, so every discovered key is read again from the base table with
`ConsistentRead=True`; lag can temporarily understate coverage and cannot make an old
pointer current. Missing a just-created bucket temporarily fails toward an absence rather
than a plausible complete answer.

Serving readers resolve each `CURRENT` item through its window generation pointers: the
candidates are the projected base plus every `staged#<grant_id>` entry whose window pointer
(`GENERATION#<conflict_unit>` / `POINTER`) holds a grant ordinal at least the entry's, and the
largest fencing token wins. A staged entry is invisible until its window flips, so one
conditional pointer write makes a whole window visible at once (design §2.6).
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from adapters.outbound.dynamodb.execution_graph_dispatch import (
    serving_authority_identity_from_item,
)
from domain.operations.execution_graph_dispatch import (
    ServingAuthorityIdentity,
    require_serving_authority_identity,
)
from domain.semantics.identity import CommerceScopeKey
from domain.semantics.publication import (
    AttemptEvent,
    AttemptEventKind,
    AttemptId,
    AuthorityCheck,
    CanonicalDatasetKey,
    CanonicalPublisherKey,
    LeaseGrant,
    LeaseHolderKey,
    LeaseRequest,
    LeaseState,
    PartitionIdentity,
    PublicationId,
    PublicationLensKey,
    PublicationScope,
    PublicationStateError,
    SourcePublicationRef,
    SuccessfulPublication,
)
from domain.semantics.revision import RevisionPrecision, RevisionSchemeKey, SourceRevision
from domain.semantics.time import (
    BusinessCalendarKey,
    FactTimeClass,
    MeasurementBucket,
    ObservationTimeBasis,
    TimeGrain,
)
from domain.semantics.validation import (
    LegacyPublicationRules,
    PrepublicationRuleFamily,
    PrepublicationRuleResult,
    PublicationEvidence,
    PublicationEvidenceSubject,
    RuleOutcome,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from domain.shared_kernel.values import DateRange
from ports.outbound.semantic_state import (
    MAX_EXACT_CURRENT_PARTITIONS,
    AttemptStorePort,
    CommitPublicationGroupRequest,
    CommitPublicationRequest,
    InspectExactResult,
    LeaseAcquireOutcome,
    LeaseStorePort,
    PublicationStorePort,
    ScopeHorizon,
    StateWriteOutcome,
)

if TYPE_CHECKING:  # pragma: no cover — kept out of the Lambda import path
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import (
        AttributeValueTypeDef,
        TransactGetItemTypeDef,
        TransactWriteItemTypeDef,
    )

__all__ = [
    "ATTEMPT_EVENT_KEY",
    "ATTEMPT_KEY",
    "CURATION_PUBLISHER",
    "CURRENT_RECORD",
    "EARLIEST_SCOPE_PAGE",
    "GENERATION_POINTER_NEWER",
    "GENERATION_POINTER_RECORD",
    "LEASE_CALL_BOUND_SECONDS",
    "LEASE_CLIENT_CONNECT_TIMEOUT_SECONDS",
    "LEASE_CLIENT_MAX_ATTEMPTS",
    "LEASE_CLIENT_READ_TIMEOUT_SECONDS",
    "MAX_PUBLICATION_ITEM_BYTES",
    "PARTITION_KEY",
    "PUBLICATION_RECORD_KEY",
    "SCOPE_CURRENT_INDEX",
    "DynamoAttemptStore",
    "DynamoCurrentPublications",
    "DynamoLeaseStore",
    "DynamoPublicationStore",
    "DynamoPublicationStoreAccess",
    "FlipOutcome",
    "FlipRequest",
    "FoldOutcome",
    "GenerationFlipBarrierError",
    "GenerationFlipLostError",
    "GenerationPointer",
    "GrantStaging",
    "GraphCurrentBase",
    "PublishCounts",
    "PublishTarget",
    "ServableProjection",
    "StageClass",
    "StagedEntry",
    "StagedTarget",
    "bounded_lease_client",
    "generation_pointer_partition",
    "graph_current_base",
    "publication_evidence_from_data",
    "publication_record_bytes",
    "staged_attribute",
]

ATTEMPT_KEY = "attempt_id"
ATTEMPT_EVENT_KEY = "event_key"
PARTITION_KEY = "partition_key"
PUBLICATION_RECORD_KEY = "record_key"
CURRENT_RECORD = "CURRENT"
PROJECTED_PUBLICATION_ID = "projected_publication_id"
# A-PROTOCOL-CONSTANT-001: a DynamoDB attribute name; see docs/security-dispositions.md
PROJECTED_FENCING_TOKEN = "projected_fencing_token"  # noqa: S105 -- attribute name  # nosec B105
PROJECTED_AT = "projected_at"
SERVABLE_NONE = "NONE"
_SERVABLE_ATTRIBUTES = frozenset({PROJECTED_PUBLICATION_ID, PROJECTED_FENCING_TOKEN, PROJECTED_AT})
SCOPE_CURRENT_INDEX = "scope-current"
COVERAGE_SCOPE = "coverage_scope"
BUCKET_START = "bucket_start"
EARLIEST_SCOPE_PAGE = 8
"""The most earliest buckets an earliest-bucket read examines: one index page, never a second."""
PUBLICATION_EVIDENCE_SCHEMA_VERSION = 2
MAX_TRANSACTION_ITEMS = 100
DYNAMODB_TRANSACTION_LIMIT_BYTES = 4_000_000
PUBLICATION_TRANSACTION_HEADROOM_BYTES = 800_000
# Derive the per-item guard from the grouped transaction envelope: payload bytes leave explicit
# room for keys and request framing. The 60-campaign attribution acceptance measured 30,049
# bytes for CURRENT; the former 30 KB guard refused it despite the transaction headroom.
MAX_PUBLICATION_ITEM_BYTES = (
    DYNAMODB_TRANSACTION_LIMIT_BYTES - PUBLICATION_TRANSACTION_HEADROOM_BYTES
) // MAX_TRANSACTION_ITEMS
PUBLICATION_PAIR_SIZE = 2
PAYLOAD = "payload"

LEASE_CLIENT_CONNECT_TIMEOUT_SECONDS = 2
LEASE_CLIENT_READ_TIMEOUT_SECONDS = 5
LEASE_CLIENT_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_CAP_SECONDS = 20  # botocore standard mode: rand(0, 1) x min(2**i, 20)
_RETRY_AFTER_EXTRA_SECONDS = 5  # botocore standard mode honours x-amz-retry-after up to +5 s
LEASE_CALL_BOUND_SECONDS = LEASE_CLIENT_MAX_ATTEMPTS * (
    LEASE_CLIENT_CONNECT_TIMEOUT_SECONDS + LEASE_CLIENT_READ_TIMEOUT_SECONDS
) + sum(
    min(2**retry, _RETRY_BACKOFF_CAP_SECONDS) + _RETRY_AFTER_EXTRA_SECONDS
    for retry in range(LEASE_CLIENT_MAX_ATTEMPTS - 1)
)
"""TD-61: the longest one lease call takes on `bounded_lease_client` (34 s), retries included.

Every attempt's connect and read timeouts, plus the largest backoff (throttling scale, before
jitter) and retry-after allowance between attempts (botocore 1.43 `retries/standard.py`). A
connect that walks several resolved addresses, or a slow DNS resolution, is outside it.
"""


def bounded_lease_client() -> DynamoDBClient:
    """TD-61: the delivery handler's lease client, with each call bounded by its config.

    Each lease call ends within `LEASE_CALL_BOUND_SECONDS`, so a degraded DynamoDB cannot hang
    the delivery invocation on a lease call. The default client retries in legacy mode with
    60 s timeouts.
    """
    return boto3.client(
        "dynamodb",
        config=Config(
            connect_timeout=LEASE_CLIENT_CONNECT_TIMEOUT_SECONDS,
            read_timeout=LEASE_CLIENT_READ_TIMEOUT_SECONDS,
            retries={"mode": "standard", "total_max_attempts": LEASE_CLIENT_MAX_ATTEMPTS},
        ),
    )


CONDITIONAL = "ConditionalCheckFailedException"
TRANSACTION_CONDITIONAL = "ConditionalCheckFailed"
TRANSACTION_CANCELLED = "TransactionCanceledException"

ATTEMPT_UNREADABLE = ErrorCode("SEMANTIC_STATE", 1)
ATTEMPT_UNWRITABLE = ErrorCode("SEMANTIC_STATE", 2)
LEASE_UNWRITABLE = ErrorCode("SEMANTIC_STATE", 3)
PUBLICATION_UNREADABLE = ErrorCode("SEMANTIC_STATE", 4)
PUBLICATION_UNWRITABLE = ErrorCode("SEMANTIC_STATE", 5)

_HEAD_ABSENT = "absent"
_HEAD_NONE = "none"
_HEAD_PUBLICATION = "publication"

# The window generation protocol (design §2.6). The staged-entry and pointer formats live here
# only, beside the reader resolution that interprets them (plan B3).
STAGED_PREFIX = "staged#"
_STAGED_ENTRY_FIELDS = frozenset(
    {"publication_id", "fencing_token", "conflict_unit", "grant_ordinal", "staged_at"}
)
GENERATION_PREFIX = "GENERATION#"
GENERATION_POINTER_RECORD = "POINTER"
GENERATION_POINTER_KIND = "generation_pointer"
GENERATION_POINTER_SCHEMA_VERSION = 1
# A refused A/P/B/C/P' comparison repeats the whole read up to this many attempts in all,
# with a jittered pause between attempts (design §2.6 "Bounded retry inside the adapter").
SERVABLE_READ_ATTEMPTS = 3
_RETRY_PAUSE_SECONDS = (0.05, 0.15)
_GRAPH_PARTICIPANT_START = 3


@dataclass(frozen=True, slots=True)
class _ExactPublicationHead:
    """One closed normalized authority or servable selector state."""

    kind: str
    publication_id: str | None = None
    fencing_token: int | None = None
    selected_payload: str | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.kind == _HEAD_PUBLICATION:
            if not self.publication_id or self.fencing_token is None or self.fencing_token < 1:
                raise ValueError("publication head requires a positive fenced identity.")
        elif self.kind not in {_HEAD_ABSENT, _HEAD_NONE} or any(
            value is not None
            for value in (self.publication_id, self.fencing_token, self.selected_payload)
        ):
            raise ValueError("absence heads cannot carry publication identity.")


@dataclass(frozen=True, slots=True)
class _VectorRead:
    """One verified vector read: ordered keys, their partitions when known, and the mode.

    Scope reads pass no partitions; each is decoded from its discovered item in A.
    """

    keys: tuple[str, ...]
    partitions: tuple[PartitionIdentity, ...] | None
    servable: bool


@dataclass(frozen=True, slots=True)
class _HeadVector:
    """One resolved head vector and the windows its staged entries named."""

    partitions: tuple[PartitionIdentity, ...]
    heads: tuple[_ExactPublicationHead, ...]
    windows: frozenset[str]


def _text(value: str) -> AttributeValueTypeDef:
    return {"S": value}


def _number(value: int) -> AttributeValueTypeDef:
    return {"N": str(value)}


def _read_text(item: dict[str, AttributeValueTypeDef], key: str) -> str:
    attribute = item.get(key)
    if not isinstance(attribute, dict):
        raise ValueError(f"stored semantic state has no string {key!r}.")
    value = attribute.get("S")
    if not isinstance(value, str):
        raise ValueError(f"stored semantic state has no string {key!r}.")
    return value


def _read_int(item: dict[str, AttributeValueTypeDef], key: str) -> int:
    attribute = item.get(key)
    if not isinstance(attribute, dict):
        raise ValueError(f"stored semantic state has no number {key!r}.")
    value = attribute.get("N")
    if not isinstance(value, str):
        raise ValueError(f"stored semantic state has no number {key!r}.")
    return int(value)


def _moment(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("semantic-state instant must be timezone-aware.")
    return value.astimezone(UTC).isoformat()


def _read_moment(item: dict[str, AttributeValueTypeDef], key: str) -> datetime:
    value = datetime.fromisoformat(_read_text(item, key))
    if value.tzinfo is None:
        raise ValueError(f"stored semantic-state instant {key!r} is naive.")
    return value


def _is_conditional(error: Exception) -> bool:
    return (
        isinstance(error, ClientError)
        and error.response.get("Error", {}).get("Code") == CONDITIONAL
    )


def _jittered_pause() -> None:
    time.sleep(random.uniform(*_RETRY_PAUSE_SECONDS))  # noqa: S311 -- retry jitter, not a secret


def staged_attribute(grant_id: str) -> str:
    """Name the `CURRENT` attribute that carries one grant's staged entry."""
    if not grant_id.strip():
        raise ValueError("a staged entry requires a nonblank grant id.")
    return f"{STAGED_PREFIX}{grant_id}"


def generation_pointer_partition(conflict_unit: str) -> str:
    """Name the publications-table partition of one window's generation pointer."""
    if not conflict_unit.strip():
        raise ValueError("a generation pointer requires a nonblank conflict unit.")
    return f"{GENERATION_PREFIX}{conflict_unit}"


def _is_non_authority(name: str) -> bool:
    """Servable pointer attributes and staged entries are not authority attributes."""
    return name in _SERVABLE_ATTRIBUTES or name.startswith(STAGED_PREFIX)


@dataclass(frozen=True, slots=True)
class StagedEntry:
    """One grant's staged servable candidate on a `CURRENT` item (`staged#<grant_id>`)."""

    grant_id: str
    publication_id: str
    fencing_token: int
    conflict_unit: str
    grant_ordinal: int
    staged_at: datetime


@dataclass(frozen=True, slots=True)
class GraphCurrentBase:
    """One `CURRENT` item classified for GRAPH staging and the fold.

    `projected_publication_id` is None for the `NONE` sentinel (token 0).
    """

    partition_key: str
    publication_id: str
    fencing_token: int
    projected_publication_id: str | None
    projected_fencing_token: int
    staged: tuple[StagedEntry, ...]


def _staged_entries(current: Mapping[str, AttributeValueTypeDef]) -> tuple[StagedEntry, ...]:
    """Decode every `staged#<grant_id>` map; any other shape is unreadable."""
    entries: list[StagedEntry] = []
    for name in sorted(current):
        if not name.startswith(STAGED_PREFIX):
            continue
        grant_id = name.removeprefix(STAGED_PREFIX)
        value = cast("object", current[name].get("M"))
        if not grant_id.strip() or not isinstance(value, dict):
            raise ValueError("CURRENT carries a malformed staged entry.")
        entry = cast("dict[str, AttributeValueTypeDef]", value)
        if frozenset(entry) != _STAGED_ENTRY_FIELDS:
            raise ValueError("CURRENT staged entry is incomplete or unexpected.")
        staged = StagedEntry(
            grant_id=grant_id,
            publication_id=_read_text(entry, "publication_id"),
            fencing_token=_read_int(entry, "fencing_token"),
            conflict_unit=_read_text(entry, "conflict_unit"),
            grant_ordinal=_read_int(entry, "grant_ordinal"),
            staged_at=_read_moment(entry, "staged_at"),
        )
        if (
            not staged.publication_id.strip()
            or not staged.conflict_unit.strip()
            or staged.fencing_token < 1
            or staged.grant_ordinal < 1
        ):
            raise ValueError("CURRENT staged entry identity is invalid.")
        entries.append(staged)
    return tuple(entries)


def _pointer_ordinal(
    conflict_unit: str, item: Mapping[str, AttributeValueTypeDef] | None
) -> int | None:
    """Decode one window generation pointer to its grant ordinal, or None when absent."""
    if item is None:
        return None
    if (
        item.get(PARTITION_KEY) != _text(generation_pointer_partition(conflict_unit))
        or item.get(PUBLICATION_RECORD_KEY) != _text(GENERATION_POINTER_RECORD)
        or item.get("item_kind") != _text(GENERATION_POINTER_KIND)
        or item.get("schema_version") != _number(GENERATION_POINTER_SCHEMA_VERSION)
    ):
        raise ValueError("window generation pointer is substituted or unexpected.")
    ordinal = _read_int(dict(item), "grant_ordinal")
    if ordinal < 1:
        raise ValueError("window generation pointer carries an invalid grant ordinal.")
    return ordinal


def graph_current_base(
    partition_key: str, current: Mapping[str, AttributeValueTypeDef] | None
) -> GraphCurrentBase:
    """Classify one raw `CURRENT` item for GRAPH staging and the fold (AGG4-3).

    GRAPH paths keep no pre-migration rule: an item without `projected_*`, an absent item, or
    a malformed servable pair or staged entry is an `InvariantError` `PUBLICATION_UNREADABLE`.
    Reader resolution refuses the same item as a `DependencyError` through its head-vector
    wrap (AGG4-17).
    """
    if current is None:
        raise InvariantError(
            PUBLICATION_UNREADABLE,
            "A GRAPH publish target has no CURRENT item.",
            context={"partition": partition_key},
        )
    if not _SERVABLE_ATTRIBUTES.issubset(current):
        raise InvariantError(
            PUBLICATION_UNREADABLE,
            "A GRAPH publish target CURRENT item has no servable projection.",
            context={"partition": partition_key},
        )
    try:
        item = dict(current)
        if item.get(PARTITION_KEY) != _text(partition_key):
            raise ValueError("CURRENT names a substituted partition.")
        authority_id = _read_text(item, "publication_id")
        authority_token = _read_int(item, "fencing_token")
        projected_id = _read_text(item, PROJECTED_PUBLICATION_ID)
        projected_token = _read_int(item, PROJECTED_FENCING_TOKEN)
        _read_moment(item, PROJECTED_AT)
        if projected_id == SERVABLE_NONE:
            if projected_token != 0:
                raise ValueError("CURRENT NONE sentinel carries a nonzero fence.")
        else:
            _require_within_authority(
                (projected_id, projected_token), (authority_id, authority_token)
            )
        staged = _staged_entries(item)
    except (KeyError, TypeError, ValueError) as error:
        raise InvariantError(
            PUBLICATION_UNREADABLE,
            "A GRAPH publish target CURRENT item is malformed.",
            context={"cause": type(error).__name__, "partition": partition_key},
        ) from None
    return GraphCurrentBase(
        partition_key=partition_key,
        publication_id=authority_id,
        fencing_token=authority_token,
        projected_publication_id=None if projected_id == SERVABLE_NONE else projected_id,
        projected_fencing_token=projected_token,
        staged=staged,
    )


def _require_within_authority(candidate: tuple[str, int], authority: tuple[str, int]) -> None:
    """Require a servable candidate to trail authority, naming its ID exactly at its token."""
    publication_id, fencing_token = candidate
    authority_id, authority_token = authority
    if not publication_id.strip() or fencing_token < 1 or fencing_token > authority_token:
        raise ValueError("CURRENT projected identity is invalid.")
    if (publication_id == authority_id) != (fencing_token == authority_token):
        raise ValueError("CURRENT projected identity and fence diverge from authority.")


# --- GRAPH publish formats (design §2.6; plan B3) -------------------------------------------------

CURATION_PUBLISHER = "canonical-curation"
"""The publisher whose advanced targets make bake and precompute eligible (§2.6 counts, §2.8)."""
GENERATION_POINTER_NEWER = ErrorCode("SEMANTIC_STATE", 6)
PUBLICATION_FLIP_ATOMICITY_INVARIANT = ErrorCode("SEMANTIC_STATE", 7)
"""A window pointer already names a larger grant ordinal than the flipping grant (invariant)."""


class StageClass(StrEnum):
    """How one publish target classified from stored `CURRENT` state."""

    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    STAGED = "staged"


@dataclass(frozen=True, slots=True)
class PublishTarget:
    """One effect-record target `publish` stages: a partition and its committed publication."""

    partition_key: str
    publication_id: str
    fencing_token: int

    def __post_init__(self) -> None:
        if not self.partition_key.strip() or not self.publication_id.strip():
            raise ValueError("a publish target names its partition and publication.")
        if isinstance(self.fencing_token, bool) or self.fencing_token < 1:
            raise ValueError("a publish target carries a positive fencing token.")


@dataclass(frozen=True, slots=True)
class GrantStaging:
    """The grant one staged entry is written under (AGG4-16: the ordinal is on every entry)."""

    grant_id: str
    grant_ordinal: int
    conflict_unit: str
    staged_at: datetime

    def __post_init__(self) -> None:
        staged_attribute(self.grant_id)
        generation_pointer_partition(self.conflict_unit)
        if isinstance(self.grant_ordinal, bool) or self.grant_ordinal < 1:
            raise ValueError("a staged entry carries a positive grant ordinal.")
        _moment(self.staged_at)


@dataclass(frozen=True, slots=True)
class StagedTarget:
    """One target's stored classification and whether its partition is canonical curation."""

    target: PublishTarget
    classification: StageClass
    curation: bool


@dataclass(frozen=True, slots=True)
class PublishCounts:
    """`advanced` = staged + published and `superseded`, each with its curation share."""

    advanced: int
    advanced_curation: int
    superseded: int
    superseded_curation: int

    @classmethod
    def classify(cls, staged: tuple[StagedTarget, ...]) -> PublishCounts:
        """Count classified targets; a replay of a partial stage yields the same counts."""
        advanced = [item for item in staged if item.classification is not StageClass.SUPERSEDED]
        superseded = [item for item in staged if item.classification is StageClass.SUPERSEDED]
        return cls(
            advanced=len(advanced),
            advanced_curation=sum(1 for item in advanced if item.curation),
            superseded=len(superseded),
            superseded_curation=sum(1 for item in superseded if item.curation),
        )

    def to_record(self) -> dict[str, int]:
        """Render the frozen counts under their stored attribute names."""
        return {
            "advanced_count": self.advanced,
            "advanced_curation_count": self.advanced_curation,
            "superseded_count": self.superseded,
            "superseded_curation_count": self.superseded_curation,
        }

    def to_item(self) -> dict[str, AttributeValueTypeDef]:
        """Render the counts as DynamoDB number attributes."""
        return {name: _number(value) for name, value in self.to_record().items()}

    @classmethod
    def from_item(cls, item: Mapping[str, AttributeValueTypeDef]) -> PublishCounts:
        """Read the counts the flip froze on a pointer."""
        stored = dict(item)
        return cls(
            advanced=_read_int(stored, "advanced_count"),
            advanced_curation=_read_int(stored, "advanced_curation_count"),
            superseded=_read_int(stored, "superseded_count"),
            superseded_curation=_read_int(stored, "superseded_curation_count"),
        )


@dataclass(frozen=True, slots=True)
class GenerationPointer:
    """One window's flipped generation pointer, with the counts frozen at its flip."""

    conflict_unit: str
    grant_id: str
    grant_ordinal: int
    counts: PublishCounts
    target_count: int


@dataclass(frozen=True, slots=True)
class FlipRequest:
    """The flip of one window to one grant (§2.6 step 2)."""

    conflict_unit: str
    grant_id: str
    grant_ordinal: int
    run_id: str
    root_flow_id: str
    authority_epoch: int
    retrieved_at: str
    revision_epoch: int
    counts: PublishCounts
    target_count: int
    target_digest: str
    flipped_at: datetime
    fences: tuple[TransactWriteItemTypeDef, ...]
    """The graph-table members (the `RUN#` grant fence, the `EXEC_CLAIM#` lease), bound."""
    graph_members: tuple[TransactWriteItemTypeDef, ...] = ()
    """Day/head fences, marker put, and one frontier update on the graph table."""
    marker_table: str | None = None
    marker_key: dict[str, AttributeValueTypeDef] | None = None
    marker_expected: dict[str, AttributeValueTypeDef] | None = None
    serving_identity: ServingAuthorityIdentity | None = None


def _generation_pointer_attributes(request: FlipRequest) -> dict[str, AttributeValueTypeDef]:
    """Render every operation-owned attribute of one generation flip."""
    return {
        "grant_id": _text(request.grant_id),
        "grant_ordinal": _number(request.grant_ordinal),
        "run_id": _text(request.run_id),
        "root_flow_id": _text(request.root_flow_id),
        "authority_epoch": _number(request.authority_epoch),
        "retrieved_at": _text(request.retrieved_at),
        "revision_epoch": _number(request.revision_epoch),
        **request.counts.to_item(),
        "target_count": _number(request.target_count),
        "target_digest": _text(request.target_digest),
        "flipped_at": _text(_moment(request.flipped_at)),
        "item_kind": _text(GENERATION_POINTER_KIND),
        "schema_version": _number(GENERATION_POINTER_SCHEMA_VERSION),
    }


def _generation_pointer_item(request: FlipRequest) -> dict[str, AttributeValueTypeDef]:
    return {
        PARTITION_KEY: _text(generation_pointer_partition(request.conflict_unit)),
        PUBLICATION_RECORD_KEY: _text(GENERATION_POINTER_RECORD),
        **_generation_pointer_attributes(request),
    }


class FlipOutcome(StrEnum):
    """What one flip call did."""

    FLIPPED = "flipped"
    ALREADY_FLIPPED = "already_flipped"


class FoldOutcome(StrEnum):
    """What the fold did to one target."""

    ADVANCED = "advanced"
    REMOVED = "removed"
    ABSENT = "absent"


class GenerationFlipBarrierError(Exception):
    """A serving day/head/frontier guard raced; the publisher retries then parks durably."""


class GenerationFlipLostError(Exception):
    """A flip fence (the `RUN#` grant or the `EXEC_CLAIM#` lease) was refused: zero effect."""

    def __init__(self, position: int) -> None:
        super().__init__(f"generation flip fence {position} was refused.")
        self.position = position


def _current_key(partition_key: str) -> dict[str, AttributeValueTypeDef]:
    return {PARTITION_KEY: _text(partition_key), PUBLICATION_RECORD_KEY: _text(CURRENT_RECORD)}


def _staged_value(grant: GrantStaging, target: PublishTarget) -> AttributeValueTypeDef:
    return {
        "M": {
            "publication_id": _text(target.publication_id),
            "fencing_token": _number(target.fencing_token),
            "conflict_unit": _text(grant.conflict_unit),
            "grant_ordinal": _number(grant.grant_ordinal),
            "staged_at": _text(_moment(grant.staged_at)),
        }
    }


def _unreadable_target(partition_key: str, detail: str) -> InvariantError:
    return InvariantError(
        PUBLICATION_UNREADABLE,
        f"A GRAPH publish target {detail}.",
        context={"partition": partition_key},
    )


def _classify_target(
    base: GraphCurrentBase, grant_id: str, target: PublishTarget
) -> StageClass | None:
    """Classify one target from stored state, or None when it still needs its entry."""
    try:
        _require_within_authority(
            (target.publication_id, target.fencing_token),
            (base.publication_id, base.fencing_token),
        )
    except ValueError:
        raise _unreadable_target(target.partition_key, "is outside its authority") from None
    projected = base.projected_publication_id
    if projected == target.publication_id:
        if base.projected_fencing_token != target.fencing_token:
            raise _unreadable_target(target.partition_key, "base diverges from its fence")
        return StageClass.PUBLISHED
    if projected is not None and base.projected_fencing_token >= target.fencing_token:
        if base.projected_fencing_token == target.fencing_token:
            raise _unreadable_target(target.partition_key, "base names another publication")
        return StageClass.SUPERSEDED
    entry = next((item for item in base.staged if item.grant_id == grant_id), None)
    if entry is None:
        return None
    if (entry.publication_id, entry.fencing_token) != (
        target.publication_id,
        target.fencing_token,
    ):
        raise _unreadable_target(target.partition_key, "entry names another publication")
    return StageClass.STAGED


def _authority_publisher(
    partition_key: str, current: Mapping[str, AttributeValueTypeDef] | None
) -> str:
    """Read the publisher of the partition's authority publication from `CURRENT`'s payload."""
    try:
        data = _object(json.loads(_read_text(dict(current or {}), PAYLOAD)))
        return _string(data, "publisher")
    except (KeyError, TypeError, ValueError) as error:
        raise InvariantError(
            PUBLICATION_UNREADABLE,
            "A GRAPH publish target CURRENT item has no readable publisher.",
            context={"cause": type(error).__name__, "partition": partition_key},
        ) from None


def _unwritable(error: Exception, subject: str, step: str) -> DependencyError:
    return DependencyError(
        PUBLICATION_UNWRITABLE,
        "A GRAPH publish write could not be committed.",
        context={"cause": type(error).__name__, "subject": subject, "step": step},
    )


def _pointer_newer(request: FlipRequest) -> InvariantError:
    return InvariantError(
        GENERATION_POINTER_NEWER,
        "The window generation pointer already names a later grant.",
        context={"conflict_unit": request.conflict_unit, "grant_ordinal": request.grant_ordinal},
    )


class DynamoAttemptStore(AttemptStorePort):
    """Append-only attempt events."""

    def __init__(self, table: str, client: DynamoDBClient | None = None) -> None:
        self._table = table
        self._client = client or boto3.client("dynamodb")

    def append(self, event: AttemptEvent) -> StateWriteOutcome:
        """Append STARTED or non-success terminal evidence; success commits with CURRENT."""
        if event.kind is AttemptEventKind.SUCCEEDED:
            return StateWriteOutcome(
                accepted=False,
                reason="SUCCEEDED attempt evidence must be committed atomically with CURRENT.",
            )
        if event.kind is not AttemptEventKind.STARTED:
            started = self._event(event.attempt_id, "00000000")
            if started is None:
                return StateWriteOutcome(
                    accepted=False,
                    reason="A terminal attempt event requires its STARTED event first.",
                )
            if (
                started.partition != event.partition
                or started.predecessor != event.predecessor
                or event.occurred_at < started.occurred_at
            ):
                return StateWriteOutcome(
                    accepted=False,
                    reason="A terminal attempt event contradicts its STARTED event.",
                )
        item = _attempt_item(event)
        try:
            self._client.put_item(
                TableName=self._table,
                Item=item,
                ConditionExpression=(
                    f"attribute_not_exists({ATTEMPT_KEY}) AND "
                    f"attribute_not_exists({ATTEMPT_EVENT_KEY})"
                ),
            )
        except Exception as error:
            if _is_conditional(error):
                existing = self._event(event.attempt_id, event.event_key)
                return (
                    StateWriteOutcome(accepted=True)
                    if existing == event
                    else StateWriteOutcome(
                        accepted=False,
                        reason=(
                            "An attempt event already exists at this sequence "
                            "with different evidence."
                        ),
                    )
                )
            raise DependencyError(
                ATTEMPT_UNWRITABLE,
                "The semantic attempt history could not be written.",
                context={"cause": type(error).__name__, "attempt_id": event.attempt_id.value},
            ) from None
        return StateWriteOutcome(accepted=True)

    def events_for(self, attempt_id: AttemptId) -> tuple[AttemptEvent, ...]:
        """Read all events for one attempt in sequence order."""
        try:
            response = self._client.query(
                TableName=self._table,
                KeyConditionExpression=f"{ATTEMPT_KEY} = :attempt",
                ExpressionAttributeValues={":attempt": _text(attempt_id.value)},
                ConsistentRead=True,
            )
        except Exception as error:
            raise DependencyError(
                ATTEMPT_UNREADABLE,
                "The semantic attempt history could not be read.",
                context={"cause": type(error).__name__, "attempt_id": attempt_id.value},
            ) from None
        return tuple(_attempt_from(dict(item)) for item in response.get("Items", []))

    def _event(self, attempt_id: AttemptId, event_key: str) -> AttemptEvent | None:
        try:
            response = self._client.get_item(
                TableName=self._table,
                Key={ATTEMPT_KEY: _text(attempt_id.value), ATTEMPT_EVENT_KEY: _text(event_key)},
                ConsistentRead=True,
            )
        except Exception as error:
            raise DependencyError(
                ATTEMPT_UNREADABLE,
                "The semantic attempt history could not be read.",
                context={"cause": type(error).__name__, "attempt_id": attempt_id.value},
            ) from None
        item = response.get("Item")
        return None if item is None else _attempt_from(dict(item))


class DynamoLeaseStore(LeaseStorePort):
    """Atomic expiring fenced lease per canonical partition."""

    def __init__(self, table: str, client: DynamoDBClient | None = None) -> None:
        self._table = table
        self._client = client or boto3.client("dynamodb")

    def acquire(self, request: LeaseRequest) -> LeaseAcquireOutcome:
        """Acquire or renew and atomically increment the fence."""
        expires_at = request.requested_at + request.duration
        try:
            response = self._client.update_item(
                TableName=self._table,
                Key={PARTITION_KEY: _text(request.partition.identity)},
                UpdateExpression=(
                    "SET #holder = :holder, #expires = :expires, #partition = :partition "
                    "ADD fencing_token :one"
                ),
                ConditionExpression=(
                    f"attribute_not_exists({PARTITION_KEY}) OR "
                    "#expires <= :now OR #holder = :holder"
                ),
                ExpressionAttributeNames={
                    "#holder": "holder",
                    "#expires": "expires_at",
                    "#partition": "partition_payload",
                },
                ExpressionAttributeValues={
                    ":holder": _text(request.holder.value),
                    ":expires": _text(_moment(expires_at)),
                    ":now": _text(_moment(request.requested_at)),
                    ":one": _number(1),
                    ":partition": _text(_json(_partition_data(request.partition))),
                },
                ReturnValues="ALL_NEW",
            )
        except Exception as error:
            if _is_conditional(error):
                return LeaseAcquireOutcome(
                    grant=None,
                    reason="Another live holder already controls this canonical partition.",
                )
            raise DependencyError(
                LEASE_UNWRITABLE,
                "The semantic publication lease could not be acquired.",
                context={"cause": type(error).__name__, "partition": request.partition.identity},
            ) from None
        attributes = dict(response.get("Attributes", {}))
        state = LeaseState(
            partition=request.partition,
            holder=LeaseHolderKey(_read_text(attributes, "holder")),
            fencing_token=_read_int(attributes, "fencing_token"),
            expires_at=_read_moment(attributes, "expires_at"),
        )
        return LeaseAcquireOutcome(grant=LeaseGrant(state))

    def release(self, grant: LeaseGrant, *, at: datetime) -> StateWriteOutcome:
        """Expire only the holder/fence pair represented by this grant."""
        state = grant.state
        try:
            self._client.update_item(
                TableName=self._table,
                Key={PARTITION_KEY: _text(state.partition.identity)},
                UpdateExpression="SET #expires = :at",
                ConditionExpression="#holder = :holder AND fencing_token = :token",
                ExpressionAttributeNames={"#holder": "holder", "#expires": "expires_at"},
                ExpressionAttributeValues={
                    ":holder": _text(state.holder.value),
                    ":token": _number(state.fencing_token),
                    ":at": _text(_moment(at)),
                },
            )
        except Exception as error:
            if _is_conditional(error):
                return StateWriteOutcome(
                    accepted=False,
                    reason="The lease was already replaced or belongs to another holder.",
                )
            raise DependencyError(
                LEASE_UNWRITABLE,
                "The semantic publication lease could not be released.",
                context={"cause": type(error).__name__, "partition": state.partition.identity},
            ) from None
        return StateWriteOutcome(accepted=True)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ServableProjection:
    """Flat current-table identity needed for one monotonic servability advance."""

    partition_key: str
    publication_id: str
    fencing_token: int
    projected_at: datetime

    def __post_init__(self) -> None:
        if not self.partition_key.strip() or not self.publication_id.strip():
            raise ValueError("servable projection identities cannot be blank.")
        if self.fencing_token <= 0:
            raise ValueError("servable projection fencing token must be positive.")
        if self.projected_at.tzinfo is None:
            raise ValueError("servable projection instant must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class DynamoPublicationStoreAccess:
    """SDK, clock, and atomic attempt-table collaborator for publication persistence.

    ``inline_projection`` states that this composition projects Iceberg before the commit
    (the deployed replicated path and the operator tools): a first publication seeds its own
    servable pointer and every commit advances servability immediately. The Stage 1 S3-only
    composition sets it False, seeds ``NONE``, and leaves the advance to the table writer.
    """

    attempts_table: str = ""
    client: DynamoDBClient | None = None
    clock: Callable[[], datetime] = _utc_now
    inline_projection: bool = True
    retry_pause: Callable[[], None] = _jittered_pause


class DynamoPublicationStore(PublicationStorePort):
    """Immutable publications and a fenced current pointer."""

    def __init__(
        self,
        publications_table: str,
        leases_table: str,
        access: DynamoPublicationStoreAccess | None = None,
    ) -> None:
        selected = access or DynamoPublicationStoreAccess()
        self._publications = publications_table
        self._leases = leases_table
        self._attempts = selected.attempts_table
        self._client = selected.client or boto3.client("dynamodb")
        self._clock = selected.clock
        self._inline_projection = selected.inline_projection
        self._retry_pause = selected.retry_pause

    def validate_group(self, request: CommitPublicationGroupRequest) -> None:
        """Bound every duplicated record so maximum-size transactions stay below the byte cap."""
        for member in request.members:
            if publication_record_bytes(member.publication) > MAX_PUBLICATION_ITEM_BYTES:
                raise ValueError(
                    "publication state record exceeds the safe transaction-size bound."
                )

    def commit(self, request: CommitPublicationRequest) -> StateWriteOutcome:
        """Commit one publication through the same all-or-none grouped transaction path."""
        return self.commit_group(CommitPublicationGroupRequest((request,)))

    def commit_group(self, request: CommitPublicationGroupRequest) -> StateWriteOutcome:
        """Transactionally advance every exact member, or leave every pointer unchanged."""
        self.validate_group(request)
        commit_at = self._clock()
        if commit_at.tzinfo is None:
            raise ValueError("publication commit clock must return an aware UTC instant.")
        items: list[TransactWriteItemTypeDef] = []
        try:
            for member in request.members:
                publication = member.publication
                member.lease.require_authority(
                    AuthorityCheck(
                        partition=publication.partition,
                        holder=member.lease.state.holder,
                        fencing_token=publication.fencing_token,
                        at=commit_at,
                    )
                )
                items.extend(self._transaction_items(member, commit_at))
        except PublicationStateError as error:
            if self._exact_replay(request):
                return StateWriteOutcome(accepted=True)
            return StateWriteOutcome(accepted=False, reason=str(error))
        if len(items) > MAX_TRANSACTION_ITEMS:
            raise ValueError("publication group exceeds the DynamoDB transaction item limit.")
        try:
            self._client.transact_write_items(TransactItems=items)
        except Exception as error:
            if isinstance(error, ClientError) and _transaction_is_conditional(error):
                return (
                    StateWriteOutcome(accepted=True)
                    if self._exact_replay(request)
                    else StateWriteOutcome(
                        accepted=False,
                        reason=(
                            "A grouped publication lease, predecessor, or fencing token is stale, "
                            "or an immutable publication id names different evidence."
                        ),
                    )
                )
            raise DependencyError(
                PUBLICATION_UNWRITABLE,
                "The canonical publication group could not be committed.",
                context={
                    "cause": type(error).__name__,
                    "service_code": (
                        str(error.response.get("Error", {}).get("Code", ""))
                        if isinstance(error, ClientError)
                        else ""
                    ),
                    "partitions": ",".join(
                        member.publication.partition.identity for member in request.members
                    ),
                },
            ) from None
        if self._inline_projection:
            # This composition projected Iceberg before the commit, so each committed
            # publication is servable the moment authority advances; a refusal only means
            # a newer publication already advanced servability past this one. The commit
            # instant is the projection instant, so the group's one sampled clock is shared.
            for member in request.members:
                self.advance_servable(member.publication, at=commit_at)
        return StateWriteOutcome(accepted=True)

    def advance_servable(
        self, publication: SuccessfulPublication, at: datetime | None = None
    ) -> bool:
        """Advance servability for existing domain callers through the flat identity rule."""
        return self.advance_servable_projection(
            ServableProjection(
                partition_key=publication.partition.identity,
                publication_id=publication.publication_id.value,
                fencing_token=publication.fencing_token,
                projected_at=at if at is not None else self._clock(),
            )
        )

    def advance_servable_projection(self, projection: ServableProjection) -> bool:
        """Advance a flat projected identity without reconstructing publication state.

        Returns False when a newer publication is already servable (projection superseded);
        the conditional update is the same monotonic rule used by existing domain callers.
        """
        try:
            self._client.update_item(
                TableName=self._publications,
                Key={
                    PARTITION_KEY: _text(projection.partition_key),
                    PUBLICATION_RECORD_KEY: _text(CURRENT_RECORD),
                },
                UpdateExpression=(
                    f"SET {PROJECTED_PUBLICATION_ID} = :projected, "
                    f"{PROJECTED_FENCING_TOKEN} = :token, {PROJECTED_AT} = :at"
                ),
                ConditionExpression=(
                    "attribute_exists(publication_id) AND "
                    f"(attribute_not_exists({PROJECTED_PUBLICATION_ID}) OR "
                    f"{PROJECTED_PUBLICATION_ID} = :none OR {PROJECTED_FENCING_TOKEN} < :token)"
                ),
                ExpressionAttributeValues={
                    ":projected": _text(projection.publication_id),
                    ":token": _number(projection.fencing_token),
                    ":at": _text(_moment(projection.projected_at)),
                    ":none": _text(SERVABLE_NONE),
                },
            )
        except ClientError as error:
            if str(error.response.get("Error", {}).get("Code", "")) == (
                "ConditionalCheckFailedException"
            ):
                return False
            raise DependencyError(
                PUBLICATION_UNWRITABLE,
                "The servable projection pointer could not be advanced.",
                context={
                    "cause": type(error).__name__,
                    "partition": projection.partition_key,
                },
            ) from error
        except Exception as error:
            raise DependencyError(
                PUBLICATION_UNWRITABLE,
                "The servable projection pointer could not be advanced.",
                context={
                    "cause": type(error).__name__,
                    "partition": projection.partition_key,
                },
            ) from error
        return True

    def raw_current(self, partition_key: str) -> dict[str, AttributeValueTypeDef] | None:
        """Strongly read one exact CURRENT item without interpretation."""
        try:
            discovery = self._client.transact_get_items(
                TransactItems=[_publication_get(self._publications, partition_key, CURRENT_RECORD)]
            )
            (item,) = _transaction_response_items(discovery, 1)
        except Exception as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "The canonical publication could not be read.",
                context={"cause": type(error).__name__, "partition": partition_key},
            ) from None
        return None if item is None else dict(item)

    def servable_by_key(self, partition_key: str) -> SuccessfulPublication | None:
        """Resolve the servable publication for one partition, or None for a valid absence.

        Before the Stage 1 migration completes, an absent servable attribute is a legacy
        partition whose projection ran inline, so the authority publication is servable.
        """
        current = self.raw_current(partition_key)
        if current is None:
            return None
        projected = current.get(PROJECTED_PUBLICATION_ID)
        if projected is None:
            return self._current_by_key(partition_key)
        projected_id = _read_text(current, PROJECTED_PUBLICATION_ID)
        if projected_id == SERVABLE_NONE:
            return None
        authority_id = _read_text(current, "publication_id")
        if projected_id == authority_id:
            return self._current_by_key(partition_key)
        return self._attest_servable(partition_key, projected_id, current)

    def _attest_servable(
        self,
        partition_key: str,
        projected_id: str,
        current: dict[str, AttributeValueTypeDef],
    ) -> SuccessfulPublication:
        try:
            attested = self._client.transact_get_items(
                TransactItems=[
                    _publication_get(
                        self._publications, partition_key, f"PUBLICATION#{projected_id}"
                    )
                ]
            )
            (immutable,) = _transaction_response_items(attested, 1)
        except Exception as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "The servable publication could not be read.",
                context={"cause": type(error).__name__, "partition": partition_key},
            ) from None
        if immutable is None:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "The servable pointer names an absent immutable publication.",
                context={"partition": partition_key},
            )
        publication = _publication_from(dict(immutable))
        if (
            publication.publication_id.value != projected_id
            or _number(publication.fencing_token) != current.get(PROJECTED_FENCING_TOKEN)
            or publication.partition.identity != partition_key
        ):
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "The servable pointer diverges from its immutable publication.",
                context={"partition": partition_key},
            )
        return publication

    def _exact_replay(self, request: CommitPublicationGroupRequest) -> bool:
        transactions = [
            item
            for member in request.members
            for item in (
                _publication_get(
                    self._publications,
                    member.publication.partition.identity,
                    CURRENT_RECORD,
                ),
                _publication_get(
                    self._publications,
                    member.publication.partition.identity,
                    f"PUBLICATION#{member.publication.publication_id.value}",
                ),
                _attempt_get(
                    self._attempts,
                    member.publication.attempt_id,
                    "00000000",
                ),
                _attempt_get(
                    self._attempts,
                    member.publication.attempt_id,
                    "00000001",
                ),
            )
        ]
        try:
            response = self._client.transact_get_items(TransactItems=transactions)
        except Exception as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "The exact current canonical publications could not be attested.",
                context={
                    "cause": type(error).__name__,
                    "service_code": (
                        str(error.response.get("Error", {}).get("Code", ""))
                        if isinstance(error, ClientError)
                        else ""
                    ),
                    "partitions": len(request.members),
                },
            ) from None
        try:
            response_items = _transaction_response_items(response, len(transactions))
        except ValueError as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "The exact current canonical attestation returned malformed evidence.",
                context={
                    "cause": type(error).__name__,
                    "partitions": len(request.members),
                },
            ) from None
        for index, member in enumerate(request.members):
            current = response_items[index * 4]
            immutable = response_items[index * 4 + 1]
            started = response_items[index * 4 + 2]
            succeeded = response_items[index * 4 + 3]
            success_event = cast("AttemptEvent", member.success_event)
            if current is None or immutable is None or started is None or succeeded is None:
                return False
            authority_current = {
                name: value for name, value in current.items() if not _is_non_authority(name)
            }
            if authority_current != _publication_item(member.publication, current=True):
                return False
            if immutable != _publication_item(member.publication, current=False):
                return False
            if started != _attempt_item(_started_for_success(success_event)):
                return False
            if succeeded != _attempt_item(success_event):
                return False
        return True

    def _transaction_items(
        self,
        request: CommitPublicationRequest,
        commit_at: datetime,
    ) -> tuple[TransactWriteItemTypeDef, ...]:
        publication = request.publication
        authority_sets = (
            "publication_id = :pid, fencing_token = :token, #payload = :payload, "
            f"{COVERAGE_SCOPE} = :scope, {BUCKET_START} = :bucket"
        )
        authority_values: dict[str, AttributeValueTypeDef] = {
            ":pid": _text(publication.publication_id.value),
            ":token": _number(publication.fencing_token),
            ":payload": _text(_json(_publication_data(publication))),
            ":scope": _text(publication.partition.scope.identity),
            ":bucket": _text(publication.partition.bucket.window.start.isoformat()),
        }
        current_key: dict[str, AttributeValueTypeDef] = {
            PARTITION_KEY: _text(publication.partition.identity),
            PUBLICATION_RECORD_KEY: _text(CURRENT_RECORD),
        }
        current_write: TransactWriteItemTypeDef
        if publication.supersedes is None:
            # A first publication also seeds the servable projection pointer; an
            # inline-projection composition truthfully names itself as projected, while the
            # S3-only composition seeds the explicit NONE sentinel for the table writer.
            seed_sets = (
                f"{PROJECTED_PUBLICATION_ID} = :projected, "
                f"{PROJECTED_FENCING_TOKEN} = :projected_token, {PROJECTED_AT} = :projected_at"
            )
            current_write = {
                "Update": {
                    "TableName": self._publications,
                    "Key": current_key,
                    "UpdateExpression": f"SET {authority_sets}, {seed_sets}",
                    "ConditionExpression": "attribute_not_exists(publication_id)",
                    "ExpressionAttributeNames": {"#payload": PAYLOAD},
                    "ExpressionAttributeValues": {
                        **authority_values,
                        ":projected": _text(
                            publication.publication_id.value
                            if self._inline_projection
                            else SERVABLE_NONE
                        ),
                        ":projected_token": _number(
                            publication.fencing_token if self._inline_projection else 0
                        ),
                        ":projected_at": _text(_moment(commit_at)),
                    },
                }
            }
        else:
            # A supersede advances authority only: the servable attributes are never named,
            # so a concurrent table-writer advance cannot be overwritten or regressed.
            current_write = {
                "Update": {
                    "TableName": self._publications,
                    "Key": current_key,
                    "UpdateExpression": f"SET {authority_sets}",
                    "ConditionExpression": (
                        "publication_id = :supersedes AND fencing_token < :token"
                    ),
                    "ExpressionAttributeNames": {"#payload": PAYLOAD},
                    "ExpressionAttributeValues": {
                        **authority_values,
                        ":supersedes": _text(publication.supersedes.value),
                    },
                }
            }
        success_event = cast("AttemptEvent", request.success_event)
        started_item = _attempt_item(_started_for_success(success_event))
        succeeded_item = _attempt_item(success_event)
        return (
            {
                "Put": {
                    "TableName": self._attempts,
                    "Item": started_item,
                    "ConditionExpression": (
                        f"(attribute_not_exists({ATTEMPT_KEY}) AND "
                        f"attribute_not_exists({ATTEMPT_EVENT_KEY})) OR #payload = :payload"
                    ),
                    "ExpressionAttributeNames": {"#payload": PAYLOAD},
                    "ExpressionAttributeValues": {":payload": started_item[PAYLOAD]},
                }
            },
            {
                "ConditionCheck": {
                    "TableName": self._leases,
                    "Key": {PARTITION_KEY: _text(publication.partition.identity)},
                    "ConditionExpression": (
                        "#holder = :holder AND fencing_token = :token AND #expires > :at"
                    ),
                    "ExpressionAttributeNames": {
                        "#holder": "holder",
                        "#expires": "expires_at",
                    },
                    "ExpressionAttributeValues": {
                        ":holder": _text(request.lease.state.holder.value),
                        ":token": _number(publication.fencing_token),
                        ":at": _text(_moment(commit_at)),
                    },
                }
            },
            {
                "Put": {
                    "TableName": self._publications,
                    "Item": _publication_item(publication, current=False),
                    "ConditionExpression": (
                        f"attribute_not_exists({PARTITION_KEY}) AND "
                        f"attribute_not_exists({PUBLICATION_RECORD_KEY})"
                    ),
                }
            },
            current_write,
            {
                "Put": {
                    "TableName": self._attempts,
                    "Item": succeeded_item,
                    "ConditionExpression": (
                        f"attribute_not_exists({ATTEMPT_KEY}) AND "
                        f"attribute_not_exists({ATTEMPT_EVENT_KEY})"
                    ),
                }
            },
        )

    # --- GRAPH publish: stage, flip, fold (design §2.6; plan B3) --------------------------------

    def stage_for_grant(
        self, grant: GrantStaging, target: PublishTarget, *, max_redecide: int
    ) -> StagedTarget:
        """Stage one target under its grant, classified from stored `CURRENT` state (AGG3-4).

        `published`: the projected base already names the target (an earlier attempt folded it).
        `superseded`: the base names a publication with a larger fencing token. `staged`: this
        grant's entry already exists. Otherwise one direct `UpdateItem` sets
        `staged#<grant_id>` conditioned on `attribute_exists(publication_id) AND
        attribute_not_exists(#s)`; a condition failure re-reads and re-classifies, at most
        `max_redecide` times. Nothing a reader resolves moves: the entry is uncovered until the
        window's pointer flips.
        """
        name = staged_attribute(grant.grant_id)
        for _ in range(max_redecide):
            current = self.raw_current(target.partition_key)
            base = graph_current_base(target.partition_key, current)
            curation = _authority_publisher(target.partition_key, current) == CURATION_PUBLISHER
            found = _classify_target(base, grant.grant_id, target)
            if found is not None:
                return StagedTarget(target, found, curation)
            try:
                self._client.update_item(
                    TableName=self._publications,
                    Key=_current_key(target.partition_key),
                    UpdateExpression="SET #s = :entry",
                    ConditionExpression=(
                        "attribute_exists(publication_id) AND attribute_not_exists(#s)"
                    ),
                    ExpressionAttributeNames={"#s": name},
                    ExpressionAttributeValues={":entry": _staged_value(grant, target)},
                )
            except Exception as error:
                if _is_conditional(error):
                    continue
                raise _unwritable(error, target.partition_key, "staged") from None
            return StagedTarget(target, StageClass.STAGED, curation)
        raise DependencyError(
            PUBLICATION_UNWRITABLE,
            "A GRAPH publish target kept changing while it was staged.",
            context={"partition": target.partition_key, "attempts": max_redecide},
        )

    def read_generation_pointer(self, conflict_unit: str) -> GenerationPointer | None:
        """Strongly read one window's generation pointer (the flip's idempotence pre-read)."""
        try:
            (item,) = self._transact_get(
                [
                    _publication_get(
                        self._publications,
                        generation_pointer_partition(conflict_unit),
                        GENERATION_POINTER_RECORD,
                    )
                ]
            )
        except Exception as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "The window generation pointer could not be read.",
                context={"cause": type(error).__name__, "conflict_unit": conflict_unit},
            ) from None
        try:
            ordinal = _pointer_ordinal(conflict_unit, item)
            if item is None or ordinal is None:
                return None
            return GenerationPointer(
                conflict_unit=conflict_unit,
                grant_id=_read_text(item, "grant_id"),
                grant_ordinal=ordinal,
                counts=PublishCounts.from_item(item),
                target_count=_read_int(item, "target_count"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise InvariantError(
                PUBLICATION_UNREADABLE,
                "The window generation pointer is malformed.",
                context={"cause": type(error).__name__, "conflict_unit": conflict_unit},
            ) from None

    def flip_generation(self, request: FlipRequest) -> FlipOutcome:
        """Make one window's staged targets visible at once (§2.6 step 2).

        One `TransactWriteItems`: the `GENERATION#<U>` / `POINTER` update conditioned on
        `attribute_not_exists(partition_key) OR grant_ordinal < :ordinal`, then the caller's
        graph-table fences (the `RUN#` grant fence and the `EXEC_CLAIM#` lease). A strong
        pre-read skips the write when the pointer already names this grant; a cancelled
        transaction re-reads the same way. A refused fence raises `GenerationFlipLostError`
        (zero effect); a pointer at a larger ordinal is an invariant.
        """
        if self._flipped(request):
            return FlipOutcome.ALREADY_FLIPPED
        pointer = _generation_pointer_attributes(request)
        names = sorted(pointer)
        update: TransactWriteItemTypeDef = {
            "Update": {
                "TableName": self._publications,
                "Key": {
                    PARTITION_KEY: _text(generation_pointer_partition(request.conflict_unit)),
                    PUBLICATION_RECORD_KEY: _text(GENERATION_POINTER_RECORD),
                },
                "UpdateExpression": "SET " + ", ".join(f"{name} = :{name}" for name in names),
                "ConditionExpression": (
                    f"attribute_not_exists({PARTITION_KEY}) OR grant_ordinal < :ordinal"
                ),
                "ExpressionAttributeValues": {
                    **{f":{name}": pointer[name] for name in names},
                    ":ordinal": _number(request.grant_ordinal),
                },
            }
        }
        items = [update, *request.fences, *request.graph_members]
        try:
            self._client.transact_write_items(TransactItems=items)
        except Exception as error:
            if (
                isinstance(error, ClientError)
                and error.response.get("Error", {}).get("Code") == TRANSACTION_CANCELLED
            ):
                reasons = cast(
                    "list[dict[str, Any]]", error.response.get("CancellationReasons") or []
                )
                if any(reason.get("Code") == "TransactionConflict" for reason in reasons):
                    raise GenerationFlipBarrierError from None
            if not (isinstance(error, ClientError) and _transaction_is_conditional(error)):
                if self._flipped(request):
                    return FlipOutcome.ALREADY_FLIPPED
                raise _unwritable(error, request.conflict_unit, "pointer") from None
            if self._flipped(request):
                return FlipOutcome.ALREADY_FLIPPED
            reasons = cast("dict[str, Any]", error.response).get("CancellationReasons", [])
            for position, reason in enumerate(reasons):
                if position in {1, 2} and reason.get("Code") == TRANSACTION_CONDITIONAL:
                    raise GenerationFlipLostError(position) from None
                if (
                    position >= _GRAPH_PARTICIPANT_START
                    and reason.get("Code") == TRANSACTION_CONDITIONAL
                ):
                    raise GenerationFlipBarrierError from None
            raise _pointer_newer(request) from None
        return FlipOutcome.FLIPPED

    def _flipped(self, request: FlipRequest) -> bool:  # noqa: PLR0912
        if (
            request.marker_table is None
            or request.marker_key is None
            or request.marker_expected is None
        ):
            current = self.read_generation_pointer(request.conflict_unit)
            if current is None:
                return False
            if current.grant_id == request.grant_id:
                return True
            if current.grant_ordinal >= request.grant_ordinal:
                raise _pointer_newer(request)
            return False
        if request.serving_identity is None:
            raise InvariantError(
                PUBLICATION_FLIP_ATOMICITY_INVARIANT,
                "The graph publication flip has no typed serving authority identity.",
                context={"conflict_unit": request.conflict_unit},
            )
        try:
            require_serving_authority_identity(
                request.serving_identity,
                serving_authority_identity_from_item(request.marker_expected),
            )
        except ValueError:
            raise InvariantError(
                PUBLICATION_FLIP_ATOMICITY_INVARIANT,
                "The graph publication marker has mismatched serving authority.",
                context={"conflict_unit": request.conflict_unit},
            ) from None
        try:
            pointer_item, marker_item = self._transact_get(
                [
                    _publication_get(
                        self._publications,
                        generation_pointer_partition(request.conflict_unit),
                        GENERATION_POINTER_RECORD,
                    ),
                    {
                        "Get": {
                            "TableName": request.marker_table,
                            "Key": request.marker_key,
                        }
                    },
                ]
            )
        except Exception as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "The atomic publication flip evidence could not be read.",
                context={"cause": type(error).__name__, "conflict_unit": request.conflict_unit},
            ) from None
        pointer_present = pointer_item is not None
        marker_present = marker_item is not None
        if not pointer_present:
            if marker_present:
                raise InvariantError(
                    PUBLICATION_FLIP_ATOMICITY_INVARIANT,
                    "The semantic pointer and graph publication marker are not atomic.",
                    context={"conflict_unit": request.conflict_unit},
                )
            return False
        ordinal = _pointer_ordinal(request.conflict_unit, pointer_item)
        if _read_text(pointer_item, "grant_id") != request.grant_id:
            if ordinal is not None and ordinal >= request.grant_ordinal:
                raise _pointer_newer(request)
            if marker_present:
                raise InvariantError(
                    PUBLICATION_FLIP_ATOMICITY_INVARIANT,
                    "The publication marker does not match the semantic pointer.",
                    context={"conflict_unit": request.conflict_unit},
                )
            return False
        if not marker_present:
            raise InvariantError(
                PUBLICATION_FLIP_ATOMICITY_INVARIANT,
                "The semantic pointer and graph publication marker are not atomic.",
                context={"conflict_unit": request.conflict_unit},
            )
        expected_marker = request.marker_expected
        if marker_item != expected_marker:
            raise InvariantError(
                PUBLICATION_FLIP_ATOMICITY_INVARIANT,
                "The graph publication marker payload is not the committed flip payload.",
                context={"conflict_unit": request.conflict_unit},
            )
        if pointer_item != _generation_pointer_item(request):
            raise InvariantError(
                PUBLICATION_FLIP_ATOMICITY_INVARIANT,
                "The semantic pointer payload is not the exact committed flip payload.",
                context={"conflict_unit": request.conflict_unit},
            )
        return True

    def fold_for_grant(self, grant_id: str, partition_key: str) -> FoldOutcome:
        """Fold one target's `staged#<grant_id>` entry into its base, never lowering it (step 3).

        A target without the entry is skipped. Otherwise one `UpdateItem` sets `projected_*` from
        the entry and removes it, conditioned on the entry, on `projected_publication_id`, and on
        the entry beating the base (`NONE` or a lower token); on a condition failure a second
        `UpdateItem` removes the entry alone, and its own failure means it is already folded.
        """
        name = staged_attribute(grant_id)
        base = graph_current_base(partition_key, self.raw_current(partition_key))
        entry = next((item for item in base.staged if item.grant_id == grant_id), None)
        if entry is None:
            return FoldOutcome.ABSENT
        try:
            self._client.update_item(
                TableName=self._publications,
                Key=_current_key(partition_key),
                UpdateExpression=(
                    f"SET {PROJECTED_PUBLICATION_ID} = :id, {PROJECTED_FENCING_TOKEN} = :token, "
                    f"{PROJECTED_AT} = :at REMOVE #s"
                ),
                ConditionExpression=(
                    f"attribute_exists(#s) AND attribute_exists({PROJECTED_PUBLICATION_ID}) AND "
                    f"({PROJECTED_PUBLICATION_ID} = :none OR {PROJECTED_FENCING_TOKEN} < :token)"
                ),
                ExpressionAttributeNames={"#s": name},
                ExpressionAttributeValues={
                    ":id": _text(entry.publication_id),
                    ":token": _number(entry.fencing_token),
                    ":at": _text(_moment(self._clock())),
                    ":none": _text(SERVABLE_NONE),
                },
            )
        except Exception as error:
            if not _is_conditional(error):
                raise _unwritable(error, partition_key, "fold") from None
        else:
            return FoldOutcome.ADVANCED
        try:
            self._client.update_item(
                TableName=self._publications,
                Key=_current_key(partition_key),
                UpdateExpression="REMOVE #s",
                ConditionExpression="attribute_exists(#s)",
                ExpressionAttributeNames={"#s": name},
            )
        except Exception as error:
            if not _is_conditional(error):
                raise _unwritable(error, partition_key, "fold") from None
            return FoldOutcome.ABSENT
        return FoldOutcome.REMOVED

    def current(self, partition: PartitionIdentity) -> SuccessfulPublication | None:
        """Attest one exact CURRENT pointer and its immutable publication."""
        selected = self.current_exact((partition,))
        return None if selected is None else selected[0]

    def current_exact(
        self,
        partitions: tuple[PartitionIdentity, ...],
    ) -> tuple[SuccessfulPublication, ...] | None:
        """Return every exact publication, or None when any attested slot is absent."""
        inspected = self.inspect_exact(partitions)
        if any(publication is None for publication in inspected.publications):
            return None
        return tuple(
            publication for publication in inspected.publications if publication is not None
        )

    def inspect_exact(
        self,
        partitions: tuple[PartitionIdentity, ...],
    ) -> InspectExactResult:
        """Read ordered CURRENT slots and attest both publications and stable absence."""
        _validate_exact_partitions(partitions)
        return self._verified_exact_publications(partitions, servable=False)

    def _transact_get(
        self, gets: list[TransactGetItemTypeDef]
    ) -> tuple[dict[str, AttributeValueTypeDef] | None, ...]:
        """Read every get in order, in transactional chunks of at most 100 items."""
        items: list[dict[str, AttributeValueTypeDef] | None] = []
        for start in range(0, len(gets), MAX_TRANSACTION_ITEMS):
            chunk = gets[start : start + MAX_TRANSACTION_ITEMS]
            response = self._client.transact_get_items(TransactItems=chunk)
            items.extend(_transaction_response_items(response, len(chunk)))
        return tuple(items)

    def _read_pointers(self, windows: frozenset[str]) -> dict[str, int | None]:
        """Strongly read each named window's generation pointer ordinal (P or P')."""
        ordered = sorted(windows)
        items = self._transact_get(
            [
                _publication_get(
                    self._publications,
                    generation_pointer_partition(window),
                    GENERATION_POINTER_RECORD,
                )
                for window in ordered
            ]
        )
        return {
            window: _pointer_ordinal(window, item)
            for window, item in zip(ordered, items, strict=True)
        }

    def _read_exact_heads(
        self,
        read: _VectorRead,
        *,
        phase: str,
        windows: frozenset[str] = frozenset(),
    ) -> _HeadVector:
        """Read, resolve and normalize one complete authority or servable head vector.

        A serving read follows the `CURRENT` vector with the pointer vector for every window
        named by its staged entries, plus `windows` (P' covers the windows named in A or C).
        """
        try:
            items = self._transact_get(
                [_publication_head_get(self._publications, key) for key in read.keys]
            )
            partitions = (
                read.partitions
                if read.partitions is not None
                else tuple(
                    _discovered_partition(key, item)
                    for key, item in zip(read.keys, items, strict=True)
                )
            )
            if not read.servable:
                return _HeadVector(
                    partitions,
                    tuple(
                        _normalize_exact_head(partition, item, servable=False)
                        for partition, item in zip(partitions, items, strict=True)
                    ),
                    frozenset(),
                )
            named = windows | frozenset(
                entry.conflict_unit
                for item in items
                if item is not None
                for entry in _staged_entries(item)
            )
            pointers = self._read_pointers(named) if named else {}
            heads = tuple(
                _resolve_servable_head(partition, item, pointers)
                for partition, item in zip(partitions, items, strict=True)
            )
            return _HeadVector(partitions, heads, named)
        except Exception as error:
            service_code = (
                str(error.response.get("Error", {}).get("Code", ""))
                if isinstance(error, ClientError)
                else ""
            )
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "Exact CURRENT head vector could not be verified.",
                context={
                    "cause": type(error).__name__,
                    "service_code": service_code,
                    "phase": phase,
                    "partitions": len(read.keys),
                },
            ) from None

    def _read_exact_immutables(
        self,
        partitions: tuple[PartitionIdentity, ...],
        heads: tuple[_ExactPublicationHead, ...],
    ) -> tuple[SuccessfulPublication | None, ...]:
        """Atomically attest every immutable publication named by one head vector."""
        selected = tuple(
            (index, partition, head)
            for index, (partition, head) in enumerate(zip(partitions, heads, strict=True))
            if head.kind == _HEAD_PUBLICATION
        )
        if not selected:
            return (None,) * len(partitions)
        try:
            items = self._transact_get(
                [
                    _publication_get(
                        self._publications,
                        partition.identity,
                        f"PUBLICATION#{cast('str', head.publication_id)}",
                    )
                    for _, partition, head in selected
                ]
            )
            found: list[SuccessfulPublication | None] = [None] * len(partitions)
            for (index, partition, head), item in zip(selected, items, strict=True):
                if item is None:
                    raise ValueError("selected immutable publication is absent.")
                found[index] = _attest_exact_immutable(partition, head, item)
            return tuple(found)
        except Exception as error:
            service_code = (
                str(error.response.get("Error", {}).get("Code", ""))
                if isinstance(error, ClientError)
                else ""
            )
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "Exact immutable publication vector could not be attested.",
                context={
                    "cause": type(error).__name__,
                    "service_code": service_code,
                    "phase": "immutable",
                    "partitions": len(partitions),
                },
            ) from None

    def _verified_exact_publications(
        self,
        partitions: tuple[PartitionIdentity, ...],
        *,
        servable: bool,
    ) -> InspectExactResult:
        """Return B only when the resolved head vectors R(A, P) and R(C, P') are equal."""
        return InspectExactResult(
            self._verified_vector(
                _VectorRead(
                    tuple(partition.identity for partition in partitions), partitions, servable
                )
            )
        )

    def _verified_vector(self, read: _VectorRead) -> tuple[SuccessfulPublication | None, ...]:
        """Read A, P, B, C and P', and accept B when the resolved vectors agree.

        Resolved heads compare by kind, publication ID and fencing token, so an authority-only
        commit, another window's staging write or a fold leaves the comparison equal; only a
        resolved move (for one window, a flip inside the read) refuses an attempt. A serving
        read repeats a refused attempt up to `SERVABLE_READ_ATTEMPTS` in all; an authority read
        makes one attempt, as before. Each window therefore resolves all old or all new at P,
        per adapter call (AGG4-7).
        """
        attempts = SERVABLE_READ_ATTEMPTS if read.servable else 1
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self._retry_pause()
            first = self._read_exact_heads(read, phase="discover")
            publications = self._read_exact_immutables(first.partitions, first.heads)
            verified = self._read_exact_heads(
                _VectorRead(read.keys, first.partitions, read.servable),
                phase="verify",
                windows=first.windows,
            )
            if first.heads == verified.heads:
                return publications
        raise DependencyError(
            PUBLICATION_UNREADABLE,
            "Exact CURRENT head vector changed during immutable attestation.",
            context={"phase": "verify", "partitions": len(read.keys), "attempts": attempts},
        )

    def immutable_exact(
        self, pins: tuple[tuple[str, str, int], ...]
    ) -> tuple[SuccessfulPublication, ...]:
        """Read and attest each named immutable `PUBLICATION#<id>` record; never CURRENT.

        Plan 4.3 (DB21): a pinned bake task re-resolves only the records its manifest names.
        A missing record, or one whose partition, id or token differs, is refused.
        """
        if not pins:
            return ()
        try:
            items = self._transact_get(
                [
                    _publication_get(self._publications, partition, f"PUBLICATION#{publication}")
                    for partition, publication, _token in pins
                ]
            )
        except Exception as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "A pinned immutable publication could not be read.",
                context={"cause": type(error).__name__, "pins": len(pins)},
            ) from None
        attested: list[SuccessfulPublication] = []
        for (partition, publication_id, token), item in zip(pins, items, strict=True):
            if item is None:
                raise InvariantError(
                    PUBLICATION_UNREADABLE,
                    "A pinned immutable publication is absent.",
                    context={"publication_id": publication_id},
                )
            publication = _publication_from(item)
            if (
                publication.partition.identity != partition
                or publication.publication_id.value != publication_id
                or publication.fencing_token != token
            ):
                raise InvariantError(
                    PUBLICATION_UNREADABLE,
                    "A pinned immutable publication diverges from its pin.",
                    context={"publication_id": publication_id},
                )
            attested.append(publication)
        return tuple(attested)

    def inspect_exact_servable(
        self,
        partitions: tuple[PartitionIdentity, ...],
    ) -> InspectExactResult:
        """Attest one stable exact servable vector for request-facing readers."""
        _validate_exact_partitions(partitions)
        return self._verified_exact_publications(partitions, servable=True)

    def servable_for_scope(
        self, scope: PublicationScope, horizon: ScopeHorizon | None = None
    ) -> tuple[SuccessfulPublication, ...]:
        """Resolve one scope's GSI-discovered keys as one verified servable vector.

        Keys are discovered once per call; `NONE` slots are omitted. A `newest=n` horizon reads
        one newest-first page of at least `EARLIEST_SCOPE_PAGE` keys and keeps the newest `n`
        servable ones, so a short projection backlog still answers the latest servable bucket.
        """
        page = None
        if horizon is not None and horizon.newest is not None:
            page = max(horizon.newest, EARLIEST_SCOPE_PAGE)
        keys = self.scope_partition_keys(scope, horizon, newest_page=page)
        if not keys:
            return ()
        found = tuple(
            publication
            for publication in self._verified_vector(_VectorRead(keys, None, servable=True))
            if publication is not None
        )
        if horizon is None or horizon.newest is None:
            return found
        return _newest(found, horizon.newest)

    def scope_partition_keys(
        self,
        scope: PublicationScope,
        horizon: ScopeHorizon | None = None,
        *,
        newest_page: int | None = None,
    ) -> tuple[str, ...]:
        """Discover current partition keys for one scope through the GSI, deduplicated.

        No horizon pages the whole scope. `from_bucket_start` adds a `bucket_start` key condition
        to the same query (the index sorts on it). `newest=n` reads one newest-first page of
        `newest_page or n` keys and never a second, as `earliest_scope_keys` reads the earliest.
        """
        condition = f"{COVERAGE_SCOPE} = :scope"
        values: dict[str, AttributeValueTypeDef] = {":scope": _text(scope.identity)}
        if horizon is not None and horizon.from_bucket_start is not None:
            condition += f" AND {BUCKET_START} >= :from"
            values[":from"] = _text(horizon.from_bucket_start.isoformat())
        if horizon is not None and horizon.newest is not None:
            return self._newest_scope_keys(scope, newest_page or horizon.newest)
        keys: list[str] = []
        start: dict[str, AttributeValueTypeDef] | None = None
        while True:
            try:
                response = (
                    self._client.query(
                        TableName=self._publications,
                        IndexName=SCOPE_CURRENT_INDEX,
                        KeyConditionExpression=condition,
                        ExpressionAttributeValues=values,
                        ProjectionExpression=PARTITION_KEY,
                    )
                    if start is None
                    else self._client.query(
                        TableName=self._publications,
                        IndexName=SCOPE_CURRENT_INDEX,
                        KeyConditionExpression=condition,
                        ExpressionAttributeValues=values,
                        ProjectionExpression=PARTITION_KEY,
                        ExclusiveStartKey=start,
                    )
                )
            except Exception as error:
                raise DependencyError(
                    PUBLICATION_UNREADABLE,
                    "Canonical publication coverage could not be discovered.",
                    context={"cause": type(error).__name__, "scope": scope.identity},
                ) from None
            try:
                keys.extend(
                    _read_text(dict(item), PARTITION_KEY) for item in response.get("Items", [])
                )
                start = (
                    dict(response["LastEvaluatedKey"]) if "LastEvaluatedKey" in response else None
                )
            except (KeyError, TypeError, ValueError) as error:
                raise DependencyError(
                    PUBLICATION_UNREADABLE,
                    "Canonical publication coverage returned malformed keys.",
                    context={"cause": type(error).__name__, "scope": scope.identity},
                ) from None
            if start is None:
                break

        return tuple(sorted(set(keys)))

    def _newest_scope_keys(self, scope: PublicationScope, limit: int) -> tuple[str, ...]:
        """Return a scope's newest `limit` CURRENT partition keys: one page, newest first."""
        try:
            response = self._client.query(
                TableName=self._publications,
                IndexName=SCOPE_CURRENT_INDEX,
                KeyConditionExpression=f"{COVERAGE_SCOPE} = :scope",
                ExpressionAttributeValues={":scope": _text(scope.identity)},
                ProjectionExpression=PARTITION_KEY,
                ScanIndexForward=False,
                Limit=limit,
            )
            keys = tuple(
                _read_text(dict(item), PARTITION_KEY) for item in response.get("Items", [])
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "Canonical publication coverage returned malformed keys.",
                context={"cause": type(error).__name__, "scope": scope.identity},
            ) from None
        except Exception as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "Canonical publication coverage could not be discovered.",
                context={"cause": type(error).__name__, "scope": scope.identity},
            ) from None
        return tuple(sorted(set(keys[:limit])))

    def earliest_scope_keys(self, scope: PublicationScope) -> tuple[str, ...]:
        """Return a scope's first `EARLIEST_SCOPE_PAGE` CURRENT partition keys by bucket start.

        One GSI query and never a second page: the index sorts on `bucket_start`, so the earliest
        buckets come first, and a reader that finds none of them usable stops there rather than
        discovering the scope's history.
        """
        try:
            response = self._client.query(
                TableName=self._publications,
                IndexName=SCOPE_CURRENT_INDEX,
                KeyConditionExpression=f"{COVERAGE_SCOPE} = :scope",
                ExpressionAttributeValues={":scope": _text(scope.identity)},
                ProjectionExpression=PARTITION_KEY,
                ScanIndexForward=True,
                Limit=EARLIEST_SCOPE_PAGE,
            )
            keys = tuple(
                _read_text(dict(item), PARTITION_KEY) for item in response.get("Items", [])
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "Canonical publication coverage returned malformed keys.",
                context={"cause": type(error).__name__, "scope": scope.identity},
            ) from None
        except Exception as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "Canonical publication coverage could not be discovered.",
                context={"cause": type(error).__name__, "scope": scope.identity},
            ) from None
        return keys[:EARLIEST_SCOPE_PAGE]

    def earliest_current_for_scope(self, scope: PublicationScope) -> SuccessfulPublication | None:
        """Strongly attest only the scope's earliest-bucket CURRENT pointer: one page, one pair."""
        keys = self.earliest_scope_keys(scope)
        return self._current_by_key(keys[0]) if keys else None

    def current_for_scope(
        self, scope: PublicationScope, horizon: ScopeHorizon | None = None
    ) -> tuple[SuccessfulPublication, ...]:
        """Discover current partition keys through the GSI and strongly reread each pointer."""
        keys = self.scope_partition_keys(scope, horizon)
        return _bucket_ordered(tuple(self._current_by_key(key) for key in keys))

    def current_for_trigger(
        self, partition_key: str, publication_id: str
    ) -> SuccessfulPublication | None:
        """Attest one stream-triggered pointer and ignore a superseded publication id."""
        if not partition_key.strip() or not publication_id.strip():
            raise ValueError("publication trigger identity must be nonblank.")
        publication = self._current_by_key(partition_key)
        return publication if publication.publication_id.value == publication_id else None

    def _current_by_key(self, key: str) -> SuccessfulPublication:
        try:
            discovery = self._client.transact_get_items(
                TransactItems=[_publication_get(self._publications, key, CURRENT_RECORD)]
            )
        except Exception as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "The canonical publication could not be read.",
                context={"cause": type(error).__name__, "partition": key},
            ) from None
        try:
            (discovered,) = _transaction_response_items(discovery, 1)
        except ValueError as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "Scope-discovered CURRENT returned malformed discovery evidence.",
                context={"cause": type(error).__name__, "partition": key},
            ) from None
        if discovered is None:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "A scope-discovered CURRENT publication is absent from the base table.",
                context={"partition": key},
            )
        try:
            publication_id = _read_text(discovered, "publication_id")
        except (TypeError, ValueError) as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "Canonical CURRENT contains a malformed publication identity.",
                context={"cause": type(error).__name__, "partition": key},
            ) from None
        try:
            attested = self._client.transact_get_items(
                TransactItems=[
                    _publication_get(self._publications, key, CURRENT_RECORD),
                    _publication_get(
                        self._publications,
                        key,
                        f"PUBLICATION#{publication_id}",
                    ),
                ]
            )
        except Exception as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "The canonical publication could not be attested.",
                context={"cause": type(error).__name__, "partition": key},
            ) from None
        try:
            current_item, immutable_item = _transaction_response_items(
                attested,
                PUBLICATION_PAIR_SIZE,
            )
        except ValueError as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "Scope-discovered CURRENT returned malformed attestation evidence.",
                context={"cause": type(error).__name__, "partition": key},
            ) from None
        if current_item is None:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "Canonical CURRENT disappeared during immutable attestation.",
                context={"partition": key},
            )
        if immutable_item is None:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "Canonical CURRENT lacks its immutable attestation.",
                context={"partition": key},
            )
        try:
            return _attest_publication_pair(
                key,
                dict(current_item),
                dict(immutable_item),
            )
        except (TypeError, ValueError) as error:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "Canonical CURRENT diverges from its immutable record.",
                context={"cause": type(error).__name__, "partition": key},
            ) from None


class DynamoCurrentPublications:
    """Read-only V2 servable-publication adapter with no lease or mutation surface.

    Every serving read resolves the projected base and each covered staged entry through
    one verified A/P/B/C/P' vector, so a reader never learns a publication whose Iceberg
    projection is not attested or whose window has not flipped. An item without the
    projected pointer is refused as `PUBLICATION_UNREADABLE` (D25): no pre-migration rule
    remains on these reads. Only `current_for_trigger`, which retires, keeps it.
    """

    def __init__(
        self,
        publications_table: str,
        client: DynamoDBClient | None = None,
        *,
        retry_pause: Callable[[], None] = _jittered_pause,
    ) -> None:
        self._store = DynamoPublicationStore(
            publications_table,
            "",
            DynamoPublicationStoreAccess(client=client, retry_pause=retry_pause),
        )

    def current(self, partition: PartitionIdentity) -> SuccessfulPublication | None:
        """Resolve one partition's servable publication."""
        (publication,) = self._store.inspect_exact_servable((partition,)).publications
        if publication is not None and publication.partition != partition:
            raise DependencyError(
                PUBLICATION_UNREADABLE,
                "Servable publication differs from the requested partition.",
                context={"partition": partition.identity},
            )
        return publication

    def inspect_exact(
        self,
        partitions: tuple[PartitionIdentity, ...],
    ) -> InspectExactResult:
        """Attest one stable ordered servable publication-or-absence vector."""
        return self._store.inspect_exact_servable(partitions)

    def current_exact(
        self,
        partitions: tuple[PartitionIdentity, ...],
    ) -> tuple[SuccessfulPublication, ...] | None:
        """Read all exact servable pointers or return no selection."""
        inspected = self.inspect_exact(partitions)
        if any(publication is None for publication in inspected.publications):
            return None
        return tuple(
            publication for publication in inspected.publications if publication is not None
        )

    def immutable_exact(
        self, pins: tuple[tuple[str, str, int], ...]
    ) -> tuple[SuccessfulPublication, ...]:
        """Attest each pinned immutable record; never a servable or CURRENT read (plan 4.3)."""
        return self._store.immutable_exact(pins)

    def current_for_scope(
        self, scope: PublicationScope, horizon: ScopeHorizon | None = None
    ) -> tuple[SuccessfulPublication, ...]:
        """Discover servable buckets for a declared capability scope, omitting NONE slots."""
        return _bucket_ordered(self._store.servable_for_scope(scope, horizon))

    def earliest_current_for_scope(self, scope: PublicationScope) -> SuccessfulPublication | None:
        """Resolve the scope's earliest-bucket servable publication, reading in bucket order.

        A bucket whose projection isn't attested yet (servable NONE) is skipped. The lookup is
        bounded as a whole: one index query and at most `EARLIEST_SCOPE_PAGE` pointer reads. When
        none of those buckets is servable, as in a projection backlog, it returns None and the
        refusal names no first day (fail closed) rather than reading further into the history.
        """
        for key in self._store.earliest_scope_keys(scope):
            publication = self._store.servable_by_key(key)
            if publication is not None:
                return publication
        return None

    def current_for_trigger(
        self, partition_key: str, publication_id: str
    ) -> SuccessfulPublication | None:
        """Attest a stream trigger against the servable publication pair."""
        if not partition_key.strip() or not publication_id.strip():
            raise ValueError("publication trigger identity must be nonblank.")
        publication = self._store.servable_by_key(partition_key)
        if publication is None or publication.publication_id.value != publication_id:
            return None
        return publication


def _transaction_is_conditional(error: ClientError) -> bool:
    if error.response.get("Error", {}).get("Code") != TRANSACTION_CANCELLED:
        return False
    reasons = cast("list[dict[str, Any]]", error.response.get("CancellationReasons") or [])
    return (
        bool(reasons)
        and all(reason.get("Code") in {"None", TRANSACTION_CONDITIONAL} for reason in reasons)
        and any(reason.get("Code") == TRANSACTION_CONDITIONAL for reason in reasons)
    )


def _started_for_success(event: AttemptEvent) -> AttemptEvent:
    return AttemptEvent(
        attempt_id=event.attempt_id,
        partition=event.partition,
        kind=AttemptEventKind.STARTED,
        occurred_at=event.occurred_at,
        sequence=0,
        predecessor=event.predecessor,
    )


def _attempt_item(event: AttemptEvent) -> dict[str, AttributeValueTypeDef]:
    return {
        ATTEMPT_KEY: _text(event.attempt_id.value),
        ATTEMPT_EVENT_KEY: _text(event.event_key),
        PAYLOAD: _text(_json(_attempt_data(event))),
    }


def _attempt_from(item: dict[str, AttributeValueTypeDef]) -> AttemptEvent:
    data = _object(json.loads(_read_text(item, PAYLOAD)))
    return AttemptEvent(
        attempt_id=AttemptId(_string(data, "attempt_id")),
        partition=_partition_from(_object(data["partition"])),
        kind=AttemptEventKind(_string(data, "kind")),
        occurred_at=datetime.fromisoformat(_string(data, "occurred_at")),
        sequence=_integer(data, "sequence"),
        predecessor=(
            None if data.get("predecessor") is None else AttemptId(str(data["predecessor"]))
        ),
        reason=None if data.get("reason") is None else str(data["reason"]),
    )


def _attempt_data(event: AttemptEvent) -> dict[str, object]:
    return {
        "attempt_id": event.attempt_id.value,
        "partition": _partition_data(event.partition),
        "kind": event.kind.value,
        "occurred_at": _moment(event.occurred_at),
        "sequence": event.sequence,
        "predecessor": None if event.predecessor is None else event.predecessor.value,
        "reason": event.reason,
    }


def _validate_exact_partitions(partitions: tuple[PartitionIdentity, ...]) -> None:
    if not partitions or len(partitions) > MAX_EXACT_CURRENT_PARTITIONS:
        raise ValueError(
            f"exact CURRENT read requires 1-{MAX_EXACT_CURRENT_PARTITIONS} partitions."
        )
    if len(set(partitions)) != len(partitions):
        raise ValueError("exact CURRENT read repeats a partition.")


def _transaction_response_items(
    response: object,
    expected: int,
) -> tuple[dict[str, AttributeValueTypeDef] | None, ...]:
    if not isinstance(response, dict):
        raise ValueError("transaction response is not an object.")
    response_map = cast("dict[str, object]", response)
    responses_value = response_map.get("Responses")
    if not isinstance(responses_value, list):
        raise ValueError("transaction response has no response list.")
    responses = cast("list[object]", responses_value)
    if len(responses) != expected:
        raise ValueError("transaction response has unexpected cardinality.")
    items: list[dict[str, AttributeValueTypeDef] | None] = []
    for member_value in responses:
        if not isinstance(member_value, dict):
            raise ValueError("transaction response member is not an object.")
        member = cast("dict[str, object]", member_value)
        item_value = member.get("Item")
        if item_value is not None and not isinstance(item_value, dict):
            raise ValueError("transaction response item is not an object.")
        item = (
            None
            if item_value is None
            else dict(cast("dict[str, AttributeValueTypeDef]", item_value))
        )
        items.append(item)
    return tuple(items)


def _attempt_get(
    table: str,
    attempt_id: AttemptId,
    event_key: str,
) -> TransactGetItemTypeDef:
    return {
        "Get": {
            "TableName": table,
            "Key": {
                ATTEMPT_KEY: _text(attempt_id.value),
                ATTEMPT_EVENT_KEY: _text(event_key),
            },
        }
    }


def _publication_head_get(
    table: str,
    partition_key: str,
) -> TransactGetItemTypeDef:
    """Read one complete size-guarded selector envelope for exact A/C verification."""
    return _publication_get(table, partition_key, CURRENT_RECORD)


def _publication_get(
    table: str,
    partition_key: str,
    record_key: str,
) -> TransactGetItemTypeDef:
    return {
        "Get": {
            "TableName": table,
            "Key": {
                PARTITION_KEY: _text(partition_key),
                PUBLICATION_RECORD_KEY: _text(record_key),
            },
        }
    }


def _discovered_partition(
    key: str, current: dict[str, AttributeValueTypeDef] | None
) -> PartitionIdentity:
    """Decode the partition a scope-discovered key names from its strongly read item."""
    if current is None:
        raise ValueError("A scope-discovered CURRENT publication is absent from the base table.")
    partition = _publication_from(current).partition
    if partition.identity != key:
        raise ValueError("A scope-discovered CURRENT payload names a substituted partition.")
    return partition


def _resolve_servable_head(
    partition: PartitionIdentity,
    current: dict[str, AttributeValueTypeDef] | None,
    pointers: Mapping[str, int | None],
) -> _ExactPublicationHead:
    """Resolve one `CURRENT` item to its servable head under one pointer vector.

    The candidates are the projected base and every staged entry whose window pointer
    (`pointers` maps a conflict unit to its pointer's grant ordinal) has an ordinal at least
    the entry's. The largest fencing token wins; `NONE` when the base is `NONE` and nothing
    is covered. Each covered entry is validated as the projected pair is.
    """
    base = _normalize_exact_head(partition, current, servable=True)
    if current is None:
        return base
    covered = [
        (entry.publication_id, entry.fencing_token)
        for entry in _staged_entries(current)
        if (ordinal := pointers.get(entry.conflict_unit)) is not None
        and ordinal >= entry.grant_ordinal
    ]
    if not covered:
        return base
    authority_id = _read_text(current, "publication_id")
    authority = (authority_id, _read_int(current, "fencing_token"))
    for candidate in covered:
        _require_within_authority(candidate, authority)
    if base.kind == _HEAD_PUBLICATION:
        covered.append((cast("str", base.publication_id), cast("int", base.fencing_token)))
    head_id, head_token = max(covered, key=lambda candidate: candidate[1])
    if any(token == head_token and pid != head_id for pid, token in covered):
        raise ValueError("CURRENT servable candidates share a fence with different identities.")
    return _ExactPublicationHead(
        _HEAD_PUBLICATION,
        head_id,
        head_token,
        _read_text(current, PAYLOAD) if head_id == authority_id else None,
    )


def _normalize_exact_head(
    partition: PartitionIdentity,
    current: dict[str, AttributeValueTypeDef] | None,
    *,
    servable: bool,
) -> _ExactPublicationHead:
    """Decode one projected CURRENT envelope into a closed monotonic head state.

    Staged entries are non-authority attributes here; `_resolve_servable_head` interprets them.
    """
    if current is None:
        return _ExactPublicationHead(_HEAD_ABSENT)
    base_fields = {
        PARTITION_KEY,
        PUBLICATION_RECORD_KEY,
        "publication_id",
        "fencing_token",
        PAYLOAD,
        COVERAGE_SCOPE,
        BUCKET_START,
    }
    fields = {name for name in current if not name.startswith(STAGED_PREFIX)}
    servable_fields = frozenset(fields & _SERVABLE_ATTRIBUTES)
    if fields - _SERVABLE_ATTRIBUTES != base_fields or servable_fields not in {
        frozenset(),
        _SERVABLE_ATTRIBUTES,
    }:
        raise ValueError("CURRENT selector envelope is incomplete or unexpected.")
    if (
        current[PARTITION_KEY] != _text(partition.identity)
        or current[PUBLICATION_RECORD_KEY] != _text(CURRENT_RECORD)
        or current[COVERAGE_SCOPE] != _text(partition.scope.identity)
        or current[BUCKET_START] != _text(partition.bucket.window.start.isoformat())
    ):
        raise ValueError("CURRENT selector names a substituted partition.")
    authority_id = _read_text(current, "publication_id")
    authority_token = _read_int(current, "fencing_token")
    authority_payload = _read_text(current, PAYLOAD)
    authority_publication = _publication_from(current)
    if (
        not authority_id.strip()
        or authority_token < 1
        or authority_publication.publication_id.value != authority_id
        or authority_publication.fencing_token != authority_token
        or authority_publication.partition != partition
    ):
        raise ValueError("CURRENT authority identity is invalid.")
    authority = _ExactPublicationHead(
        _HEAD_PUBLICATION,
        authority_id,
        authority_token,
        authority_payload,
    )
    if not servable_fields:
        if servable:
            # No pre-migration rule on serving reads (AGG4-3): resolving authority here would
            # make an unflipped GRAPH materialization reader-visible at commit.
            raise ValueError("CURRENT has no servable projection pointer.")
        return authority
    projected_id = _read_text(current, PROJECTED_PUBLICATION_ID)
    projected_token = _read_int(current, PROJECTED_FENCING_TOKEN)
    _read_moment(current, PROJECTED_AT)
    if projected_id == SERVABLE_NONE:
        if projected_token != 0:
            raise ValueError("CURRENT NONE sentinel carries a nonzero fence.")
        return authority if not servable else _ExactPublicationHead(_HEAD_NONE)
    _require_within_authority((projected_id, projected_token), (authority_id, authority_token))
    if not servable:
        return authority
    return _ExactPublicationHead(
        _HEAD_PUBLICATION,
        projected_id,
        projected_token,
        authority_payload if projected_id == authority_id else None,
    )


def _attest_exact_immutable(
    partition: PartitionIdentity,
    head: _ExactPublicationHead,
    immutable: dict[str, AttributeValueTypeDef],
) -> SuccessfulPublication:
    """Attest one immutable record against the exact normalized head that selected it."""
    if head.kind != _HEAD_PUBLICATION or head.publication_id is None:
        raise ValueError("an absence head cannot attest an immutable publication.")
    expected = {
        PARTITION_KEY,
        PUBLICATION_RECORD_KEY,
        "publication_id",
        "fencing_token",
        PAYLOAD,
    }
    if set(immutable) != expected:
        raise ValueError("immutable publication envelope is incomplete or unexpected.")
    publication = _publication_from(immutable)
    if (
        immutable[PARTITION_KEY] != _text(partition.identity)
        or immutable[PUBLICATION_RECORD_KEY] != _text(f"PUBLICATION#{head.publication_id}")
        or immutable["publication_id"] != _text(head.publication_id)
        or immutable["fencing_token"] != _number(cast("int", head.fencing_token))
        or (
            head.selected_payload is not None and immutable[PAYLOAD] != _text(head.selected_payload)
        )
        or publication.partition != partition
        or publication.publication_id.value != head.publication_id
        or publication.fencing_token != head.fencing_token
    ):
        raise ValueError("immutable publication diverges from its selected head.")
    return publication


def _require_envelope_fields(
    current: dict[str, AttributeValueTypeDef],
    immutable: dict[str, AttributeValueTypeDef],
) -> None:
    """Permit exactly the authority attributes plus the servable pointer and staged entries."""
    current_fields = {
        PARTITION_KEY,
        PUBLICATION_RECORD_KEY,
        "publication_id",
        "fencing_token",
        PAYLOAD,
        COVERAGE_SCOPE,
        BUCKET_START,
    }
    immutable_fields = {
        PARTITION_KEY,
        PUBLICATION_RECORD_KEY,
        "publication_id",
        "fencing_token",
        PAYLOAD,
    }
    if {name for name in current if not _is_non_authority(name)} != current_fields:
        raise ValueError("publication envelope has missing or unexpected attributes.")
    if set(immutable) != immutable_fields:
        raise ValueError("publication envelope has missing or unexpected attributes.")


def _attest_publication_pair(
    partition: PartitionIdentity | str,
    current: dict[str, AttributeValueTypeDef],
    immutable: dict[str, AttributeValueTypeDef],
) -> SuccessfulPublication:
    partition_key = partition if isinstance(partition, str) else partition.identity
    _require_envelope_fields(current, immutable)
    if current[PARTITION_KEY] != _text(partition_key):
        raise ValueError("CURRENT names a substituted partition.")
    if current[PUBLICATION_RECORD_KEY] != _text(CURRENT_RECORD):
        raise ValueError("CURRENT has a substituted record key.")
    for name in (PARTITION_KEY, "publication_id", "fencing_token", PAYLOAD):
        if current[name] != immutable[name]:
            raise ValueError(f"CURRENT and immutable {name} diverge.")
    publication = _publication_from(current)
    if immutable[PUBLICATION_RECORD_KEY] != _text(
        f"PUBLICATION#{publication.publication_id.value}"
    ):
        raise ValueError("immutable publication has a substituted record key.")
    if current["publication_id"] != _text(publication.publication_id.value):
        raise ValueError("publication id envelope diverges from payload.")
    if current["fencing_token"] != _number(publication.fencing_token):
        raise ValueError("fencing token envelope diverges from payload.")
    if publication.partition.identity != partition_key:
        raise ValueError("publication payload names a substituted partition.")
    if isinstance(partition, PartitionIdentity) and publication.partition != partition:
        raise ValueError("publication payload differs from the requested partition.")
    if current[COVERAGE_SCOPE] != _text(publication.partition.scope.identity):
        raise ValueError("CURRENT coverage scope diverges from payload.")
    if current[BUCKET_START] != _text(publication.partition.bucket.window.start.isoformat()):
        raise ValueError("CURRENT bucket start diverges from payload.")
    return publication


def publication_record_bytes(publication: SuccessfulPublication) -> int:
    """Return the encoded size of the larger of a publication's two transaction records."""
    return max(
        len(_json(_publication_item(publication, current=current)).encode())
        for current in (False, True)
    )


def _publication_item(
    publication: SuccessfulPublication, *, current: bool
) -> dict[str, AttributeValueTypeDef]:
    item: dict[str, AttributeValueTypeDef] = {
        PARTITION_KEY: _text(publication.partition.identity),
        PUBLICATION_RECORD_KEY: _text(
            CURRENT_RECORD if current else f"PUBLICATION#{publication.publication_id.value}"
        ),
        "publication_id": _text(publication.publication_id.value),
        "fencing_token": _number(publication.fencing_token),
        PAYLOAD: _text(_json(_publication_data(publication))),
    }
    if current:
        item[COVERAGE_SCOPE] = _text(publication.partition.scope.identity)
        item[BUCKET_START] = _text(publication.partition.bucket.window.start.isoformat())
    return item


def publication_evidence_from_data(evidence: dict[str, object]) -> PublicationEvidence:
    """Decode modern bound evidence and retain prior immutable records as historical."""
    if "results" not in evidence:
        return PublicationEvidence(
            rule_set_version=_string(evidence, "rule_set_version"),
            legacy=LegacyPublicationRules(
                passed_rules=tuple(str(value) for value in _list(evidence, "passed_rules")),
                qualified_warnings=tuple(
                    str(value) for value in _list(evidence, "qualified_warnings")
                ),
                failed_rules=tuple(str(value) for value in _list(evidence, "failed_rules")),
            ),
        )
    if evidence.get("schema_version") != PUBLICATION_EVIDENCE_SCHEMA_VERSION:
        raise ValueError("modern publication evidence requires schema version 2.")
    subject = _object(evidence["subject"])
    return PublicationEvidence(
        rule_set_version=_string(evidence, "rule_set_version"),
        subject=PublicationEvidenceSubject(
            publisher=_string(subject, "publisher"),
            partition=_string(subject, "partition"),
            source_publications=tuple(
                str(value) for value in _list(subject, "source_publications")
            ),
            source_revision=_string(subject, "source_revision"),
            content_checksum=_string(subject, "content_checksum"),
        ),
        results=tuple(
            PrepublicationRuleResult(
                family=PrepublicationRuleFamily(_string(result, "family")),
                rule_id=_string(result, "rule_id"),
                outcome=RuleOutcome(_string(result, "outcome")),
                detail=_string(result, "detail"),
                qualifier=None if result.get("qualifier") is None else str(result["qualifier"]),
            )
            for value in _list(evidence, "results")
            for result in (_object(value),)
        ),
    )


def _publication_from(item: dict[str, AttributeValueTypeDef]) -> SuccessfulPublication:
    data = _object(json.loads(_read_text(item, PAYLOAD)))
    revision = _object(data["source_revision"])
    evidence = _object(data["evidence"])
    return SuccessfulPublication(
        publication_id=PublicationId(_string(data, "publication_id")),
        publisher=CanonicalPublisherKey(_string(data, "publisher")),
        partition=_partition_from(_object(data["partition"])),
        attempt_id=AttemptId(_string(data, "attempt_id")),
        source_inputs=tuple(
            SourcePublicationRef(str(value)) for value in _list(data, "source_inputs")
        ),
        source_revision=SourceRevision(
            scheme=RevisionSchemeKey(_string(revision, "scheme")),
            precision=RevisionPrecision(_string(revision, "precision")),
            observed_at=datetime.fromisoformat(_string(revision, "observed_at")),
            source_id=_string(revision, "source_id"),
        ),
        content_checksum=_string(data, "content_checksum"),
        evidence=publication_evidence_from_data(evidence),
        fencing_token=_integer(data, "fencing_token"),
        published_at=datetime.fromisoformat(_string(data, "published_at")),
        supersedes=(
            None if data.get("supersedes") is None else PublicationId(str(data["supersedes"]))
        ),
    )


def _evidence_data(evidence: PublicationEvidence) -> dict[str, object]:
    """Persist modern schema explicitly; preserve historical evidence without upgrading it."""
    if evidence.legacy is not None:
        return {
            "rule_set_version": evidence.rule_set_version,
            "passed_rules": list(evidence.legacy.passed_rules),
            "qualified_warnings": list(evidence.legacy.qualified_warnings),
            "failed_rules": list(evidence.legacy.failed_rules),
        }
    subject = evidence.subject
    if subject is None:  # pragma: no cover - PublicationEvidence rejects this state
        raise ValueError("modern publication evidence has no subject.")
    return {
        "schema_version": PUBLICATION_EVIDENCE_SCHEMA_VERSION,
        "rule_set_version": evidence.rule_set_version,
        "subject": {
            "publisher": subject.publisher,
            "partition": subject.partition,
            "source_publications": list(subject.source_publications),
            "source_revision": subject.source_revision,
            "content_checksum": subject.content_checksum,
        },
        "results": [
            {
                "family": result.family.value,
                "rule_id": result.rule_id,
                "outcome": result.outcome.value,
                "detail": result.detail,
                "qualifier": result.qualifier,
            }
            for result in evidence.results
        ],
    }


def _publication_data(publication: SuccessfulPublication) -> dict[str, object]:
    return {
        "publication_id": publication.publication_id.value,
        "publisher": publication.publisher.value,
        "partition": _partition_data(publication.partition),
        "attempt_id": publication.attempt_id.value,
        "source_inputs": [item.value for item in publication.source_inputs],
        "source_revision": {
            "scheme": publication.source_revision.scheme.value,
            "precision": publication.source_revision.precision.value,
            "observed_at": _moment(publication.source_revision.observed_at),
            "source_id": publication.source_revision.source_id,
        },
        "content_checksum": publication.content_checksum,
        "evidence": _evidence_data(publication.evidence),
        "fencing_token": publication.fencing_token,
        "published_at": _moment(publication.published_at),
        "supersedes": None if publication.supersedes is None else publication.supersedes.value,
    }


def _partition_data(partition: PartitionIdentity) -> dict[str, object]:
    return {
        "scope": {
            "dataset": partition.scope.dataset.value,
            "commerce_scope": partition.scope.commerce_scope.value,
            "population_key": partition.scope.population_key,
            "lens": partition.scope.lens.value,
            "time_class": partition.scope.time_class.value,
            "calendar": partition.scope.business_calendar.value,
            "week_starts_on": partition.scope.business_calendar.week_starts_on,
            "source_timezone": partition.scope.source_timezone,
            "observation_basis": partition.scope.observation_basis.value,
        },
        "bucket": {
            "start": partition.bucket.window.start.isoformat(),
            "end": partition.bucket.window.end.isoformat(),
            "grain": partition.bucket.grain.value,
            "calendar": partition.bucket.calendar.value,
            "week_starts_on": partition.bucket.calendar.week_starts_on,
            "source_timezone": partition.bucket.source_timezone,
            "observation_basis": partition.bucket.observation_basis.value,
        },
    }


def _partition_from(data: dict[str, object]) -> PartitionIdentity:
    scope = _object(data["scope"])
    bucket = _object(data["bucket"])
    calendar_key = BusinessCalendarKey(
        _string(scope, "calendar"),
        week_starts_on=_optional_int(scope.get("week_starts_on")),
    )
    bucket_calendar = BusinessCalendarKey(
        _string(bucket, "calendar"),
        week_starts_on=_optional_int(bucket.get("week_starts_on")),
    )
    return PartitionIdentity(
        scope=PublicationScope(
            dataset=CanonicalDatasetKey(_string(scope, "dataset")),
            commerce_scope=CommerceScopeKey(_string(scope, "commerce_scope")),
            population_key=_string(scope, "population_key"),
            lens=PublicationLensKey(_string(scope, "lens")),
            time_class=FactTimeClass(_string(scope, "time_class")),
            business_calendar=calendar_key,
            source_timezone=_string(scope, "source_timezone"),
            observation_basis=ObservationTimeBasis(
                str(
                    scope.get(
                        "observation_basis",
                        ObservationTimeBasis.SOURCE_BUSINESS_DATE.value,
                    )
                )
            ),
        ),
        bucket=MeasurementBucket(
            window=DateRange(
                date.fromisoformat(_string(bucket, "start")),
                date.fromisoformat(_string(bucket, "end")),
            ),
            grain=TimeGrain(_string(bucket, "grain")),
            calendar=bucket_calendar,
            source_timezone=_string(bucket, "source_timezone"),
            observation_basis=ObservationTimeBasis(
                str(
                    bucket.get(
                        "observation_basis",
                        ObservationTimeBasis.SOURCE_BUSINESS_DATE.value,
                    )
                )
            ),
        ),
    )


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"stored semantic state expected an object, got {type(value).__name__}.")
    return cast("dict[str, object]", value)


def _list(data: dict[str, object], key: str) -> list[object]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValueError(f"stored semantic state expected a list at {key!r}.")
    return cast("list[object]", value)


def _string(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"stored semantic state expected a string at {key!r}.")
    return value


def _integer(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"stored semantic state expected an integer at {key!r}.")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("stored semantic state expected an optional integer.")
    return value


def _bucket_ordered(
    publications: tuple[SuccessfulPublication, ...],
) -> tuple[SuccessfulPublication, ...]:
    """Return a scope read's publications in bucket order, the order every reader receives."""
    return tuple(
        sorted(
            publications,
            key=lambda item: (
                item.partition.bucket.window.start,
                item.partition.bucket.window.end,
                item.publication_id.value,
            ),
        )
    )


def _newest(
    publications: tuple[SuccessfulPublication, ...], count: int
) -> tuple[SuccessfulPublication, ...]:
    """Return the `count` publications with the latest bucket start, in bucket order."""
    return _bucket_ordered(publications)[-count:]
