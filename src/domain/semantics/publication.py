# SPDX-License-Identifier: MIT-0
"""Attempts, leases, successful publications and exact coverage are separate concepts.

ADR-047. An attempt records work; a lease grants temporary authority; a successful publication says
validated data became current; coverage is derived from current publications. A failed attempt can
therefore never erase last-good coverage, and an expired worker cannot publish after a replacement
holder acquires a higher fencing token.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from domain.semantics.identity import CanonicalKey, CommerceScopeKey
from domain.semantics.revision import SourceRevision
from domain.semantics.time import (
    BusinessCalendarKey,
    FactTimeClass,
    MeasurementBucket,
    ObservationTimeBasis,
    TimeGrain,
)
from domain.semantics.validation import PublicationEvidence, PublicationEvidenceSubject
from domain.shared_kernel.values import DateRange

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Iterable

    from domain.semantics.curation import CanonicalCurationPlan

__all__ = [
    "AMC_LENS_OPTIONS",
    "PACK_VARIANT_OPTION",
    "PERIOD_GRAIN_OPTION",
    "AttemptEvent",
    "AttemptEventKind",
    "AttemptId",
    "AuthorityCheck",
    "CanonicalDatasetKey",
    "CanonicalPublisherAuthority",
    "CanonicalPublisherBinding",
    "CanonicalPublisherKey",
    "ExactCoverage",
    "LeaseGrant",
    "LeaseHolderKey",
    "LeaseRequest",
    "LeaseState",
    "LeaseUnavailableError",
    "PartitionIdentity",
    "PublicationCandidate",
    "PublicationEvidence",
    "PublicationGraph",
    "PublicationId",
    "PublicationLensKey",
    "PublicationOwnershipError",
    "PublicationScope",
    "PublicationStateError",
    "PublishCandidateRequest",
    "SourcePublicationRef",
    "SuccessfulPublication",
    "acquire_lease",
    "coverage_from",
    "lens_source_options",
    "publication_lens_for",
    "publish_candidate",
    "successful_matches_candidate",
]

_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")


class PublicationStateError(ValueError):
    """A publication-state value cannot preserve the V2 invariants."""


class LeaseUnavailableError(PublicationStateError):
    """A live holder already has authority over the canonical partition."""


class PublicationOwnershipError(PublicationStateError):
    """The canonical publication graph has no unique authorized owner."""


def _identity(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not _ID.fullmatch(normalized):
        raise PublicationStateError(f"{label} is not a stable identifier: {value!r}.")
    return normalized


@dataclass(frozen=True, slots=True)
class AttemptId:
    """Stable identity of one execution attempt, including retries as new ids."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _identity(self.value, label="attempt id"))


@dataclass(frozen=True, slots=True)
class PublicationId:
    """Stable identity of one immutable successful canonical publication."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _identity(self.value, label="publication id"))


@dataclass(frozen=True, slots=True)
class LeaseHolderKey:
    """Identity of the worker currently authorized to publish."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _identity(self.value, label="lease holder"))


@dataclass(frozen=True, slots=True)
class CanonicalDatasetKey(CanonicalKey):
    """Canonical dataset with one publication owner under ADR-044."""


@dataclass(frozen=True, slots=True)
class CanonicalPublisherKey:
    """Stable identity of a component authorized to make canonical versions current."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _identity(self.value, label="canonical publisher"))


@dataclass(frozen=True, slots=True)
class CanonicalPublisherBinding:
    """One canonical dataset and its sole publishing component."""

    dataset: CanonicalDatasetKey
    publisher: CanonicalPublisherKey


_AUTHORITY_PROOF = object()


class CanonicalPublisherAuthority:
    """Opaque non-retargetable owner authorization issued only by a validated graph."""

    _dataset: CanonicalDatasetKey
    _publisher: CanonicalPublisherKey
    __slots__ = ("_dataset", "_publisher")

    def __init__(
        self,
        dataset: CanonicalDatasetKey,
        publisher: CanonicalPublisherKey,
        proof: object,
    ) -> None:
        if proof is not _AUTHORITY_PROOF:
            raise PublicationOwnershipError("publisher authority was not issued by the graph.")
        object.__setattr__(self, "_dataset", dataset)
        object.__setattr__(self, "_publisher", publisher)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"publisher authority is immutable; cannot set {name!r}.")

    @property
    def dataset(self) -> CanonicalDatasetKey:
        """Dataset this capability authorizes."""
        return self._dataset

    @property
    def publisher(self) -> CanonicalPublisherKey:
        """Sole component this capability authorizes."""
        return self._publisher


@dataclass(frozen=True, slots=True)
class PublicationGraph:
    """Complete one-owner authorization graph for an expected canonical dataset set."""

    expected_datasets: frozenset[CanonicalDatasetKey]
    bindings: tuple[CanonicalPublisherBinding, ...]

    def __post_init__(self) -> None:
        if not self.expected_datasets:
            raise PublicationOwnershipError("publication graph requires an expected dataset set.")
        owners: dict[CanonicalDatasetKey, CanonicalPublisherKey] = {}
        for binding in self.bindings:
            if binding.dataset in owners:
                raise PublicationOwnershipError(
                    f"canonical dataset {binding.dataset.value!r} has more than one owner binding."
                )
            owners[binding.dataset] = binding.publisher
        missing = self.expected_datasets - owners.keys()
        extra = owners.keys() - self.expected_datasets
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append("missing " + ", ".join(sorted(dataset.value for dataset in missing)))
            if extra:
                details.append("extra " + ", ".join(sorted(dataset.value for dataset in extra)))
            raise PublicationOwnershipError(
                "publication graph does not exactly cover canonical datasets: "
                + "; ".join(details)
                + "."
            )

    def owner_for(self, dataset: CanonicalDatasetKey) -> CanonicalPublisherKey:
        """Return the sole declared owner, refusing a dataset outside the complete graph."""
        for binding in self.bindings:
            if binding.dataset == dataset:
                return binding.publisher
        raise PublicationOwnershipError(
            f"canonical dataset {dataset.value!r} has no publishing owner."
        )

    def require_owner(
        self,
        dataset: CanonicalDatasetKey,
        publisher: CanonicalPublisherKey,
    ) -> None:
        """Refuse a component that is not the dataset's sole declared owner."""
        expected = self.owner_for(dataset)
        if expected != publisher:
            raise PublicationOwnershipError(
                f"canonical dataset {dataset.value!r} is owned by {expected.value!r}, not "
                f"{publisher.value!r}."
            )

    def authorize(
        self,
        dataset: CanonicalDatasetKey,
        publisher: CanonicalPublisherKey,
    ) -> CanonicalPublisherAuthority:
        """Issue non-forgeable authority after validating the graph's sole owner."""
        self.require_owner(dataset, publisher)
        return CanonicalPublisherAuthority(dataset, publisher, _AUTHORITY_PROOF)


@dataclass(frozen=True, slots=True)
class PublicationLensKey:
    """Opaque identity of the option/lens semantics selecting one publication family."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _identity(self.value, label="publication lens"))


PERIOD_GRAIN_OPTION: Final = "period_grain"
"""The registration option naming a periodic delivery's grain (ADR-110 point 1)."""

PACK_VARIANT_OPTION: Final = "pack_variant"
"""The registration option naming a declared pack variant's delivery (plan D9). Absent on a
delivery of the base pack, so every existing series keeps its lens."""

AMC_LENS_OPTIONS: Final = frozenset({PERIOD_GRAIN_OPTION, PACK_VARIANT_OPTION})
"""The AMC registration options that name a publication family, so its lens hashes them.

A governed AMC registration also carries the pack's execution options, the window bounds and the
pack's input parameters (`domain.producer.planning.plan_landing`). None of them names a family:
the execution options are fixed by the pack and the Finalizer refuses any other value; the
window is the delivery's own period, which its bucket already carries; and each parameter is
the governed value for the delivery's marketplace and period, which the Finalizer re-resolves
and refuses on a mismatch (ADR-110 point 2), so it adds nothing beyond the population. Hashing
them made every producer-landed delivery a family of its own, which serving could not rebuild
(wave-2 risk A1-7). The period grain does name one: a week and a month are separate series. So
does a pack variant (plan D9): pack 5's touch-only rows are a different series from its combined
rows, and must never be read as one.
"""


def lens_source_options(
    source_system: str, options: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, str], ...]:
    """Select the source options a publication lens hashes, on the publisher and serving alike.

    Every option of another source is its family identity (a Vendor `reportOptions` value), so it
    passes through. An AMC delivery keeps only `AMC_LENS_OPTIONS`.
    """
    if source_system != "amc":
        return options
    return tuple(item for item in options if item[0] in AMC_LENS_OPTIONS)


def publication_lens_for(
    profile: str,
    source_options: tuple[tuple[str, str], ...],
    profile_version: int,
) -> PublicationLensKey:
    """Derive one publisher-owned lens identity from profile, options, and version."""
    if not profile.strip():
        raise PublicationStateError("publication lens requires its source profile identity.")
    if profile_version < 1:
        raise PublicationStateError("publication lens profile version starts at 1.")
    if any(not name.strip() or not value.strip() for name, value in source_options):
        raise PublicationStateError("publication lens options require non-empty names and values.")
    names = tuple(name for name, _ in source_options)
    if len(set(names)) != len(names):
        raise PublicationStateError("publication lens repeats a source option name.")
    payload = json.dumps(
        {
            "profile": profile,
            "source_options": sorted(source_options),
            "profile_version": profile_version,
        },
        separators=(",", ":"),
    )
    return PublicationLensKey(f"lens-{hashlib.sha256(payload.encode()).hexdigest()[:24]}")


@dataclass(frozen=True, slots=True)
class PublicationScope:
    """Family whose current successful buckets combine into exact coverage."""

    dataset: CanonicalDatasetKey
    commerce_scope: CommerceScopeKey
    population_key: str
    lens: PublicationLensKey
    time_class: FactTimeClass
    business_calendar: BusinessCalendarKey
    source_timezone: str
    observation_basis: ObservationTimeBasis = ObservationTimeBasis.SOURCE_BUSINESS_DATE

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "population_key",
            _identity(self.population_key, label="publication population"),
        )
        if not self.source_timezone.strip():
            raise PublicationStateError("publication scope requires its source timezone.")

    @property
    def identity(self) -> str:
        """Stable physical key for state storage, not an analytical row key."""
        return "|".join(
            (
                self.dataset.value,
                self.commerce_scope.value,
                self.population_key,
                self.lens.value,
                self.time_class.value,
                self.business_calendar.value,
                str(self.business_calendar.week_starts_on),
                self.source_timezone,
                self.observation_basis.value,
            )
        )


@dataclass(frozen=True, slots=True)
class PartitionIdentity:
    """One canonical publication scope and exact measurement bucket."""

    scope: PublicationScope
    bucket: MeasurementBucket

    def __post_init__(self) -> None:
        if self.bucket.calendar != self.scope.business_calendar:
            raise PublicationStateError(
                "publication bucket calendar must match its coverage scope calendar."
            )
        if self.bucket.source_timezone != self.scope.source_timezone:
            raise PublicationStateError(
                "publication bucket timezone must match its coverage scope timezone."
            )
        if self.bucket.observation_basis is not self.scope.observation_basis:
            raise PublicationStateError(
                "publication bucket observation basis must match its coverage scope."
            )
        if self.scope.time_class is FactTimeClass.DAILY_ADDITIVE and (
            self.bucket.grain is not TimeGrain.DAY
        ):
            raise PublicationStateError("daily additive publication accepts only DAY buckets.")
        if self.scope.time_class is FactTimeClass.PERIODIC and self.bucket.grain is TimeGrain.DAY:
            raise PublicationStateError("periodic publication cannot use a DAY bucket.")

    @property
    def identity(self) -> str:
        """Stable storage identity whose scope prefix remains queryable."""
        return "|".join(
            (
                self.scope.identity,
                self.bucket.grain.value,
                self.bucket.window.start.isoformat(),
                self.bucket.window.end.isoformat(),
            )
        )


class AttemptEventKind(StrEnum):
    """Immutable event in one attempt's operational history."""

    STARTED = "started"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True, slots=True)
class AttemptEvent:
    """One append-only event in an attempt's history."""

    attempt_id: AttemptId
    partition: PartitionIdentity
    kind: AttemptEventKind
    occurred_at: datetime
    sequence: int
    predecessor: AttemptId | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            raise PublicationStateError("attempt event instant must be timezone-aware.")
        if self.sequence < 0:
            raise PublicationStateError("attempt event sequence cannot be negative.")
        if self.kind is AttemptEventKind.STARTED and self.sequence != 0:
            raise PublicationStateError("STARTED is sequence zero for every attempt.")
        if self.kind is not AttemptEventKind.STARTED and self.sequence != 1:
            raise PublicationStateError("a terminal attempt event is sequence one after STARTED.")
        if (
            self.kind
            in {
                AttemptEventKind.REJECTED,
                AttemptEventKind.QUARANTINED,
                AttemptEventKind.FAILED,
            }
            and not self.reason
        ):
            raise PublicationStateError(f"{self.kind.value} attempt event requires a reason.")

    @property
    def event_key(self) -> str:
        """One immutable slot per sequence; terminal kinds cannot become two truths."""
        return f"{self.sequence:08d}"


@dataclass(frozen=True, slots=True)
class LeaseState:
    """Current fenced authority over one canonical partition."""

    partition: PartitionIdentity
    holder: LeaseHolderKey
    fencing_token: int
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.fencing_token < 1:
            raise PublicationStateError("fencing token starts at 1.")
        if self.expires_at.tzinfo is None:
            raise PublicationStateError("lease expiry must be timezone-aware.")

    def active_at(self, instant: datetime) -> bool:
        """Whether the holder still has authority at the given aware instant."""
        if instant.tzinfo is None:
            raise PublicationStateError("lease comparison instant must be timezone-aware.")
        return instant < self.expires_at


@dataclass(frozen=True, slots=True)
class LeaseRequest:
    """Request to acquire or renew publication authority."""

    partition: PartitionIdentity
    holder: LeaseHolderKey
    requested_at: datetime
    duration: timedelta

    def __post_init__(self) -> None:
        if self.requested_at.tzinfo is None:
            raise PublicationStateError("lease request instant must be timezone-aware.")
        if self.duration <= timedelta(0):
            raise PublicationStateError("lease duration must be positive.")


@dataclass(frozen=True, slots=True)
class AuthorityCheck:
    """Publication authority a candidate asks a lease grant to prove."""

    partition: PartitionIdentity
    holder: LeaseHolderKey
    fencing_token: int
    at: datetime


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    """Authority returned by a successful lease acquisition."""

    state: LeaseState

    def require_authority(self, check: AuthorityCheck) -> None:
        """Refuse stale, foreign, expired or wrong-scope publication authority."""
        if self.state.partition != check.partition:
            raise PublicationStateError("lease grant is for a different canonical partition.")
        if self.state.holder != check.holder:
            raise PublicationStateError("lease grant belongs to another holder.")
        if self.state.fencing_token != check.fencing_token:
            raise PublicationStateError("publication carries a stale fencing token.")
        if not self.state.active_at(check.at):
            raise PublicationStateError("publication lease expired before publication.")


def acquire_lease(current: LeaseState | None, request: LeaseRequest) -> LeaseGrant:
    """Decide a lease acquisition, incrementing the fence on every grant or renewal.

    A renewal invalidates an earlier token held by the same worker. That is deliberate: any task
    still carrying the old token is stale once renewal succeeds, exactly like a replaced worker.
    """
    if current is not None:
        if current.partition != request.partition:
            raise PublicationStateError("current lease is for a different canonical partition.")
        if current.active_at(request.requested_at) and current.holder != request.holder:
            raise LeaseUnavailableError(
                f"partition is leased by {current.holder.value} until "
                f"{current.expires_at.isoformat()}."
            )
        token = current.fencing_token + 1
    else:
        token = 1
    return LeaseGrant(
        LeaseState(
            partition=request.partition,
            holder=request.holder,
            fencing_token=token,
            expires_at=request.requested_at + request.duration,
        )
    )


@dataclass(frozen=True, slots=True)
class SourcePublicationRef:
    """Immutable source-aligned publication consumed by a canonical publisher."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _identity(self.value, label="source publication"))


@dataclass(frozen=True, slots=True)
class PublicationCandidate:
    """Validated candidate asking to become current under a lease."""

    publication_id: PublicationId
    publisher: CanonicalPublisherKey
    partition: PartitionIdentity
    attempt_id: AttemptId
    holder: LeaseHolderKey
    fencing_token: int
    source_inputs: tuple[SourcePublicationRef, ...]
    source_revision: SourceRevision
    content_checksum: str
    evidence: PublicationEvidence
    published_at: datetime

    def __post_init__(self) -> None:
        if self.fencing_token < 1:
            raise PublicationStateError("publication fencing token starts at 1.")
        if not self.source_inputs:
            raise PublicationStateError("canonical publication requires source-aligned inputs.")
        if len(set(self.source_inputs)) != len(self.source_inputs):
            raise PublicationStateError("canonical publication repeats a source input.")
        if not self.content_checksum.strip():
            raise PublicationStateError("canonical publication requires a content checksum.")
        if self.published_at.tzinfo is None:
            raise PublicationStateError("publication instant must be timezone-aware.")
        if self.evidence.subject != self.evidence_subject:
            raise PublicationStateError(
                "prepublication evidence was evaluated for another candidate subject."
            )

    @property
    def evidence_subject(self) -> PublicationEvidenceSubject:
        """Exact semantics and bytes validation evidence must have evaluated."""
        return _publication_evidence_subject(self)


@dataclass(frozen=True, slots=True)
class SuccessfulPublication:
    """Immutable validated canonical partition version."""

    publication_id: PublicationId
    publisher: CanonicalPublisherKey
    partition: PartitionIdentity
    attempt_id: AttemptId
    source_inputs: tuple[SourcePublicationRef, ...]
    source_revision: SourceRevision
    content_checksum: str
    evidence: PublicationEvidence
    fencing_token: int
    published_at: datetime
    supersedes: PublicationId | None = None

    def __post_init__(self) -> None:
        if not self.source_inputs:
            raise PublicationStateError("successful publication requires source-aligned inputs.")
        if len(set(self.source_inputs)) != len(self.source_inputs):
            raise PublicationStateError("successful publication repeats a source input.")
        if not self.content_checksum.strip():
            raise PublicationStateError("successful publication requires a content checksum.")
        if not (self.evidence.publishable or self.evidence.readable_historical_success):
            raise PublicationStateError("failed validation evidence cannot describe a success.")
        if self.evidence.subject is not None and self.evidence.subject != self.evidence_subject:
            raise PublicationStateError(
                "prepublication evidence was evaluated for another successful subject."
            )
        if self.fencing_token < 1:
            raise PublicationStateError("successful publication fencing token starts at 1.")
        if self.published_at.tzinfo is None:
            raise PublicationStateError("successful publication instant must be timezone-aware.")
        if self.supersedes == self.publication_id:
            raise PublicationStateError("a publication cannot supersede itself.")

    @property
    def evidence_subject(self) -> PublicationEvidenceSubject:
        """Exact semantics and bytes validation evidence must have evaluated."""
        return _publication_evidence_subject(self)


def _publication_evidence_subject(
    publication: PublicationCandidate | SuccessfulPublication,
) -> PublicationEvidenceSubject:
    revision = publication.source_revision
    return PublicationEvidenceSubject(
        publisher=publication.publisher.value,
        partition=publication.partition.identity,
        source_publications=tuple(reference.value for reference in publication.source_inputs),
        source_revision="|".join(
            (
                revision.scheme.value,
                revision.precision.value,
                revision.observed_at.isoformat(),
                revision.source_id,
            )
        ),
        content_checksum=publication.content_checksum,
    )


def successful_matches_candidate(
    candidate: PublicationCandidate,
    successful: SuccessfulPublication,
) -> bool:
    """Whether a success is the exact immutable transition of one candidate."""
    return (
        successful.publication_id == candidate.publication_id
        and successful.publisher == candidate.publisher
        and successful.partition == candidate.partition
        and successful.attempt_id == candidate.attempt_id
        and successful.fencing_token == candidate.fencing_token
        and successful.source_inputs == candidate.source_inputs
        and successful.source_revision == candidate.source_revision
        and successful.content_checksum == candidate.content_checksum
        and successful.evidence == candidate.evidence
        and successful.published_at == candidate.published_at
    )


@dataclass(frozen=True, slots=True)
class PublishCandidateRequest:
    """Candidate, owner graph, lease authority, and observed current predecessor."""

    candidate: PublicationCandidate
    plan: CanonicalCurationPlan
    lease: LeaseGrant
    current: SuccessfulPublication | None


def publish_candidate(request: PublishCandidateRequest) -> SuccessfulPublication:
    """Create an immutable success only when owner, validation, and lease authority hold."""
    candidate = request.candidate
    if request.plan.partition != candidate.partition:
        raise PublicationOwnershipError("canonical plan is for another publication partition.")
    if request.plan.publisher != candidate.publisher:
        raise PublicationOwnershipError("canonical plan was authorized for another publisher.")
    if request.plan.source_references != candidate.source_inputs:
        raise PublicationStateError(
            "publication source references differ from the resolved canonical plan."
        )
    if not candidate.evidence.publishable:
        raise PublicationStateError(
            f"candidate failed prepublication rules: {', '.join(candidate.evidence.failed_rules)}."
        )
    request.lease.require_authority(
        AuthorityCheck(
            partition=candidate.partition,
            holder=candidate.holder,
            fencing_token=candidate.fencing_token,
            at=candidate.published_at,
        )
    )
    if request.current is not None and request.current.partition != candidate.partition:
        raise PublicationStateError("current publication is for another canonical partition.")
    return SuccessfulPublication(
        publication_id=candidate.publication_id,
        publisher=candidate.publisher,
        partition=candidate.partition,
        attempt_id=candidate.attempt_id,
        source_inputs=candidate.source_inputs,
        source_revision=candidate.source_revision,
        content_checksum=candidate.content_checksum,
        evidence=candidate.evidence,
        fencing_token=candidate.fencing_token,
        published_at=candidate.published_at,
        supersedes=None if request.current is None else request.current.publication_id,
    )


@dataclass(frozen=True, slots=True)
class ExactCoverage:
    """Normalized exact interval set; adjacent intervals coalesce and holes remain holes."""

    intervals: tuple[DateRange, ...]

    def __post_init__(self) -> None:
        normalized = _normalize(self.intervals)
        if normalized != self.intervals:
            raise PublicationStateError(
                "exact coverage intervals must be sorted, disjoint and coalesced."
            )

    def covers(self, requested: DateRange) -> bool:
        """Whether one coalesced interval contains the complete request."""
        return any(interval.covers(requested) for interval in self.intervals)


def coverage_from(publications: Iterable[SuccessfulPublication]) -> ExactCoverage:
    """Derive coverage only from current successes in one exact publication scope."""
    selected = tuple(publications)
    if len({item.partition.scope for item in selected}) > 1:
        raise PublicationStateError("exact coverage cannot combine different publication scopes.")
    return ExactCoverage(_normalize(tuple(item.partition.bucket.window for item in selected)))


def _normalize(intervals: tuple[DateRange, ...]) -> tuple[DateRange, ...]:
    if not intervals:
        return ()
    ordered = sorted(intervals, key=lambda interval: (interval.start, interval.end))
    merged: list[DateRange] = [ordered[0]]
    for following in ordered[1:]:
        previous = merged[-1]
        if following.start <= previous.end + timedelta(days=1):
            merged[-1] = DateRange(previous.start, max(previous.end, following.end))
        else:
            merged.append(following)
    return tuple(merged)
