# SPDX-License-Identifier: MIT-0
"""Read-only exact acceptance for one applied Semantic V2 reprocess execution."""

from __future__ import annotations

from dataclasses import dataclass, field

from adapters.jobs.ingest_delivery.semantic_sidecar import (
    SemanticSidecarOutcome,
    SemanticSidecarReceipt,
    SemanticSidecarResult,
)
from adapters.outbound.semantic_storage import ReplicatedSemanticStorage
from domain.semantics.identity_evidence import parse_identity_claim
from domain.semantics.publication import (
    AttemptEventKind,
    PartitionIdentity,
    PublicationCandidate,
    SuccessfulPublication,
    successful_matches_candidate,
)
from domain.semantics.source_aligned import SourceAlignedPublication
from domain.semantics.validation import PrepublicationRuleFamily, PublicationEvidence
from domain.shared_kernel.errors import InvariantError
from ports.outbound.canonical import CanonicalDestination, CanonicalMaterializeRequest
from ports.outbound.semantic_state import AttemptStorePort, PublicationStorePort

__all__ = [
    "ReprocessAcceptanceCounts",
    "ReprocessAcceptanceRequest",
    "ReprocessAcceptanceResult",
    "attest_reprocess",
]

_EXPECTED_ATTEMPT_EVENTS = 2


@dataclass(frozen=True, slots=True)
class ReprocessAcceptanceCounts:
    """Accepted exact artifact counts by semantic lifecycle grain."""

    sources: int = 0
    candidates: int = 0
    successes: int = 0
    publications: int = 0
    attempts: int = 0

    def __post_init__(self) -> None:
        if (
            min(
                self.sources,
                self.candidates,
                self.successes,
                self.publications,
                self.attempts,
            )
            < 0
        ):
            raise ValueError("reprocess acceptance counts cannot be negative.")


@dataclass(frozen=True, slots=True)
class ReprocessAcceptanceResult:
    """Whether every receipt was exactly attested without mutation."""

    accepted: bool
    counts: ReprocessAcceptanceCounts
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.accepted and not self.reason:
            raise ValueError("refused reprocess acceptance requires a safe reason.")
        if self.accepted and self.reason:
            raise ValueError("accepted reprocess acceptance cannot carry a refusal reason.")


@dataclass(frozen=True, slots=True)
class ReprocessAcceptanceRequest:
    """Exact sidecar receipts and the read authorities that must attest them."""

    outcomes: tuple[SemanticSidecarResult, ...]
    storage: ReplicatedSemanticStorage
    attempts: AttemptStorePort
    publications: PublicationStorePort


@dataclass(slots=True)
class _CollectedReceipts:
    sources: dict[str, SourceAlignedPublication] = field(
        default_factory=dict[str, SourceAlignedPublication]
    )
    candidates: dict[str, CanonicalMaterializeRequest] = field(
        default_factory=dict[str, CanonicalMaterializeRequest]
    )
    successes: dict[str, CanonicalMaterializeRequest] = field(
        default_factory=dict[str, CanonicalMaterializeRequest]
    )
    attempts: dict[str, SemanticSidecarReceipt] = field(
        default_factory=dict[str, SemanticSidecarReceipt]
    )
    current: dict[PartitionIdentity, CanonicalMaterializeRequest] = field(
        default_factory=dict[PartitionIdentity, CanonicalMaterializeRequest]
    )


def _refused(reason: str) -> ReprocessAcceptanceResult:
    return ReprocessAcceptanceResult(False, ReprocessAcceptanceCounts(), reason)


def _source_only(receipt: SemanticSidecarReceipt) -> bool:
    return (
        receipt.candidate is None
        and receipt.successful is None
        and receipt.attempt_id is None
        and receipt.predecessor_attempt_id is None
        and receipt.terminal_event_kind is None
    )


def _published(receipt: SemanticSidecarReceipt) -> bool:
    successful = receipt.successful
    if (
        successful is None
        or not isinstance(successful.publication, SuccessfulPublication)
        or receipt.attempt_id is None
        or receipt.terminal_event_kind is not AttemptEventKind.SUCCEEDED
        or successful.destination is not CanonicalDestination.SUCCESSFUL
        or successful.publication.attempt_id != receipt.attempt_id
        or tuple(item.reference for item in receipt.source_publications)
        != successful.publication.source_inputs
    ):
        return False
    candidate = receipt.candidate
    return not (
        candidate is None
        or not isinstance(candidate.publication, PublicationCandidate)
        or candidate.destination is not CanonicalDestination.CANDIDATE
        or not successful_matches_candidate(candidate.publication, successful.publication)
        or candidate.rows != successful.rows
        or candidate.rule_set_version != successful.rule_set_version
    )


def _skipped(receipt: SemanticSidecarReceipt) -> bool:
    """Accept only an exact stale-watermark decision with no canonical materialization."""
    return (
        receipt.candidate is None
        and receipt.successful is None
        and receipt.attempt_id is not None
        and receipt.terminal_event_kind is AttemptEventKind.SKIPPED
    )


def _outcome_lifecycle(outcome: SemanticSidecarResult) -> bool:
    if not outcome.receipts:
        return False
    if outcome.outcome is SemanticSidecarOutcome.SOURCE_ONLY:
        return not outcome.publications and all(_source_only(item) for item in outcome.receipts)
    # A blocked contract has an undecided bucket by construction, so its lifecycle is refused.
    if outcome.outcome not in {
        SemanticSidecarOutcome.PUBLISHED,
        SemanticSidecarOutcome.SUPERSEDED,
    }:
        return False
    successful_ids = tuple(
        receipt.successful.publication.publication_id
        for receipt in outcome.receipts
        if receipt.successful is not None
    )
    decided = all(_published(item) or _skipped(item) for item in outcome.receipts)
    complete = (
        outcome.expected_partition_count > 0
        and len(outcome.receipts) == outcome.expected_partition_count
    )
    if not (complete and outcome.publications == successful_ids and decided):
        return False
    # Published: at least one bucket applied, the rest skipped as older than CURRENT.
    # Superseded: every bucket skipped.
    if outcome.outcome is SemanticSidecarOutcome.PUBLISHED:
        return any(_published(item) for item in outcome.receipts)
    return all(_skipped(item) for item in outcome.receipts)


def _attempt_is_exact(
    receipt: SemanticSidecarReceipt,
    attempts: AttemptStorePort,
) -> bool:
    attempt_id = receipt.attempt_id
    successful = receipt.successful
    if attempt_id is None:
        return False
    events = attempts.events_for(attempt_id)
    if len(events) != _EXPECTED_ATTEMPT_EVENTS:
        return False
    started, terminal = events
    expected_partition = (
        successful.publication.partition if successful is not None else started.partition
    )
    expected_terminal = (
        AttemptEventKind.SUCCEEDED if successful is not None else AttemptEventKind.SKIPPED
    )
    return (
        started.attempt_id == attempt_id
        and started.partition == expected_partition
        and started.kind is AttemptEventKind.STARTED
        and started.sequence == 0
        and started.predecessor == receipt.predecessor_attempt_id
        and terminal.attempt_id == attempt_id
        and terminal.partition == expected_partition
        and terminal.kind is expected_terminal
        and terminal.kind is receipt.terminal_event_kind
        and terminal.sequence == 1
        and terminal.predecessor == receipt.predecessor_attempt_id
    )


def _add_receipt(collected: _CollectedReceipts, receipt: SemanticSidecarReceipt) -> bool:
    valid = True
    for source in receipt.source_publications:
        existing = collected.sources.get(source.reference.value)
        valid = valid and (existing is None or existing == source)
        collected.sources[source.reference.value] = source
    if receipt.candidate is not None:
        publication_id = receipt.candidate.publication.publication_id.value
        existing_candidate = collected.candidates.get(publication_id)
        valid = valid and (existing_candidate is None or existing_candidate == receipt.candidate)
        collected.candidates[publication_id] = receipt.candidate
    if receipt.successful is not None:
        publication_id = receipt.successful.publication.publication_id.value
        existing_success = collected.successes.get(publication_id)
        valid = valid and (existing_success is None or existing_success == receipt.successful)
        collected.successes[publication_id] = receipt.successful
        collected.current[receipt.successful.publication.partition] = receipt.successful
    if receipt.attempt_id is not None:
        existing_attempt = collected.attempts.get(receipt.attempt_id.value)
        valid = valid and (existing_attempt is None or existing_attempt == receipt)
        collected.attempts[receipt.attempt_id.value] = receipt
    return valid


def _collect(
    outcomes: tuple[SemanticSidecarResult, ...],
) -> _CollectedReceipts | None:
    collected = _CollectedReceipts()
    valid = True
    for outcome in outcomes:
        valid = valid and _outcome_lifecycle(outcome)
        for receipt in outcome.receipts:
            valid = _add_receipt(collected, receipt) and valid
    return collected if valid else None


def _attest_sources(
    collected: _CollectedReceipts,
    storage: ReplicatedSemanticStorage,
) -> str:
    for source in collected.sources.values():
        result = storage.source.attest(source)
        if not result.accepted or result.created:
            return "source authority or projection differs from its receipt."
    return ""


def _attest_identity_evidence(
    evidence: PublicationEvidence,
    storage: ReplicatedSemanticStorage,
) -> bool:
    """Read every claim-check document the evidence names, through digest verification."""
    try:
        for result in evidence.results:
            if result.family is PrepublicationRuleFamily.IDENTITY:
                storage.identity_evidence.read(parse_identity_claim(result.detail))
    # error-propagation: a predicate; _attest_canonical refuses acceptance on False with a reason
    except InvariantError:
        return False
    return True


def _attest_canonical(
    collected: _CollectedReceipts,
    storage: ReplicatedSemanticStorage,
) -> str:
    for candidate in collected.candidates.values():
        result = storage.canonical.attest(candidate)
        if not result.accepted or result.created:
            return "candidate authority or projection differs from its receipt."
        if not _attest_identity_evidence(candidate.publication.evidence, storage):
            return "identity claim-check evidence is absent or differs from its claim."
    for successful in collected.successes.values():
        publication = successful.publication
        if not isinstance(publication, SuccessfulPublication):
            return "successful receipt has a malformed publication envelope."
        result = storage.canonical.attest(successful)
        if not result.accepted or result.created:
            return "successful authority or projection differs from its receipt."
        if storage.canonical.rows(publication) != successful.rows:
            return "successful S3 rows differ from their exact receipt."
    return ""


def _attest_state(
    collected: _CollectedReceipts,
    attempts: AttemptStorePort,
    publications: PublicationStorePort,
) -> str:
    for receipt in collected.attempts.values():
        if not _attempt_is_exact(receipt, attempts):
            return "attempt history differs from exact STARTED then declared terminal event."
    for partition, successful in collected.current.items():
        if publications.current(partition) != successful.publication:
            return "CURRENT and immutable publication evidence differ from the receipt."
    return ""


def attest_reprocess(request: ReprocessAcceptanceRequest) -> ReprocessAcceptanceResult:
    """Attest exact receipts against immutable authority, projection, and state history."""
    if not request.outcomes:
        return _refused("reprocess acceptance requires at least one outcome.")
    collected = _collect(request.outcomes)
    if collected is None:
        return _refused("reprocess receipts have a blocked, missing, or malformed lifecycle.")
    if not collected.sources:
        return _refused("reprocess receipts contain no exact source authority.")
    reason = (
        _attest_sources(collected, request.storage)
        or _attest_canonical(collected, request.storage)
        or _attest_state(collected, request.attempts, request.publications)
    )
    if reason:
        return _refused(reason)
    return ReprocessAcceptanceResult(
        accepted=True,
        counts=ReprocessAcceptanceCounts(
            sources=len(collected.sources),
            candidates=len(collected.candidates),
            successes=len(collected.successes),
            publications=len(collected.current),
            attempts=len(collected.attempts),
        ),
    )
