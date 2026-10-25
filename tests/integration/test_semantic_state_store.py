# SPDX-License-Identifier: MIT-0
"""Semantic V2 attempts, fenced leases, and publications against real DynamoDB.

These tests establish the service behavior no in-process substitute can: conditional updates,
transaction cancellation reasons, composite keys, strong pointer reads, and eventual GSI discovery.
Every invented hash key starts with the shared probe prefix so session cleanup can remove it safely.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import boto3
import pytest
from probe_identity import probe_key

from adapters.config import Settings
from adapters.outbound.dynamodb.semantic_state import (
    CURRENT_RECORD,
    PARTITION_KEY,
    PAYLOAD,
    PUBLICATION_RECORD_KEY,
    PUBLICATION_UNREADABLE,
    DynamoAttemptStore,
    DynamoCurrentPublications,
    DynamoLeaseStore,
    DynamoPublicationStore,
    DynamoPublicationStoreAccess,
    FlipOutcome,
    FlipRequest,
    FoldOutcome,
    GrantStaging,
    PublishCounts,
    PublishTarget,
    ServableProjection,
    StageClass,
    staged_attribute,
)
from domain.semantics.curation import CanonicalCurationPlan
from domain.semantics.identity import CommerceScopeKey
from domain.semantics.publication import (
    AttemptEvent,
    AttemptEventKind,
    AttemptId,
    CanonicalDatasetKey,
    CanonicalPublisherAuthority,
    CanonicalPublisherBinding,
    CanonicalPublisherKey,
    LeaseGrant,
    LeaseHolderKey,
    LeaseRequest,
    PartitionIdentity,
    PublicationCandidate,
    PublicationEvidence,
    PublicationGraph,
    PublicationId,
    PublicationLensKey,
    PublicationScope,
    PublishCandidateRequest,
    SourcePublicationRef,
    SuccessfulPublication,
    publish_candidate,
)
from domain.semantics.revision import RevisionSchemeKey, SourceRevision
from domain.semantics.source_aligned import SourceAlignedPublication
from domain.semantics.time import (
    BusinessCalendarKey,
    FactTimeClass,
    MeasurementBucket,
    TimeGrain,
)
from domain.semantics.validation import (
    PrepublicationRuleFamily,
    PrepublicationRuleResult,
    PublicationEvidenceSubject,
    RuleOutcome,
)
from domain.shared_kernel.errors import DependencyError
from domain.shared_kernel.values import DateRange
from ports.outbound.semantic_state import (
    AttemptStorePort,
    CommitPublicationGroupRequest,
    CommitPublicationRequest,
    LeaseStorePort,
    PublicationStorePort,
)

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-047")]

CALENDAR = BusinessCalendarKey("iso_gregorian", week_starts_on=0)
PUBLISHER = CanonicalPublisherKey("canonical-curation")


@pytest.fixture
def probe(request: pytest.FixtureRequest) -> str:
    return probe_key(str(request.node.name))  # type: ignore[reportUnknownArgumentType]


@pytest.fixture
def attempts(settings: Settings) -> DynamoAttemptStore:
    return DynamoAttemptStore(settings.semantic_attempts_table)


@pytest.fixture
def leases(settings: Settings) -> DynamoLeaseStore:
    return DynamoLeaseStore(settings.semantic_leases_table)


@pytest.fixture
def publications(settings: Settings) -> DynamoPublicationStore:
    return DynamoPublicationStore(
        settings.semantic_publications_table,
        settings.semantic_leases_table,
        DynamoPublicationStoreAccess(attempts_table=settings.semantic_attempts_table),
    )


def _partition(probe: str, bucket: date = date(2026, 8, 25)) -> PartitionIdentity:
    return PartitionIdentity(
        scope=PublicationScope(
            dataset=CanonicalDatasetKey(probe),
            commerce_scope=CommerceScopeKey(probe),
            population_key=probe,
            lens=PublicationLensKey(f"{probe}-lens"),
            time_class=FactTimeClass.DAILY_ADDITIVE,
            business_calendar=CALENDAR,
            source_timezone="America/Los_Angeles",
        ),
        bucket=MeasurementBucket(
            window=DateRange(bucket, bucket),
            grain=TimeGrain.DAY,
            calendar=CALENDAR,
            source_timezone="America/Los_Angeles",
        ),
    )


def _lease_request(
    probe: str,
    partition: PartitionIdentity,
    requested_at: datetime,
) -> LeaseRequest:
    return LeaseRequest(
        partition=partition,
        holder=LeaseHolderKey(f"{probe}-holder"),
        requested_at=requested_at,
        duration=timedelta(minutes=5),
    )


@dataclass(frozen=True, slots=True)
class _ResolvedSource:
    reference: SourcePublicationRef


@dataclass(frozen=True, slots=True)
class _PublicationSpec:
    published_at: datetime
    suffix: str = "one"
    current: SuccessfulPublication | None = None
    failed_family: PrepublicationRuleFamily | None = None


def _graph(partition: PartitionIdentity) -> PublicationGraph:
    dataset = partition.scope.dataset
    return PublicationGraph(
        expected_datasets=frozenset({dataset}),
        bindings=(CanonicalPublisherBinding(dataset, PUBLISHER),),
    )


def _authority(partition: PartitionIdentity) -> CanonicalPublisherAuthority:
    return _graph(partition).authorize(partition.scope.dataset, PUBLISHER)


def _publication(
    probe: str,
    grant: LeaseGrant,
    spec: _PublicationSpec,
) -> SuccessfulPublication:
    source = SourcePublicationRef(f"{probe}-source-{spec.suffix}")
    revision = SourceRevision.instant(
        scheme=RevisionSchemeKey("integration.semantic_state"),
        source_id=f"{probe}-revision-{spec.suffix}",
        observed_at=spec.published_at,
    )
    checksum = f"sha256:{probe}:{spec.suffix}"
    subject = PublicationEvidenceSubject(
        publisher=PUBLISHER.value,
        partition=grant.state.partition.identity,
        source_publications=(source.value,),
        source_revision="|".join(
            (
                revision.scheme.value,
                revision.precision.value,
                revision.observed_at.isoformat(),
                revision.source_id,
            )
        ),
        content_checksum=checksum,
    )
    candidate = PublicationCandidate(
        publication_id=PublicationId(f"{probe}-publication-{spec.suffix}"),
        publisher=PUBLISHER,
        partition=grant.state.partition,
        attempt_id=AttemptId(f"{probe}-attempt-{spec.suffix}"),
        holder=grant.state.holder,
        fencing_token=grant.state.fencing_token,
        source_inputs=(source,),
        source_revision=revision,
        content_checksum=checksum,
        evidence=PublicationEvidence(
            rule_set_version="v2-integration",
            subject=subject,
            results=tuple(
                PrepublicationRuleResult(
                    family=family,
                    rule_id=f"integration:{family.value}",
                    outcome=(
                        RuleOutcome.FAIL if family is spec.failed_family else RuleOutcome.PASSED
                    ),
                    detail=(
                        "injected integration failure"
                        if family is spec.failed_family
                        else "integration candidate check passed"
                    ),
                )
                for family in PrepublicationRuleFamily
            ),
        ),
        published_at=spec.published_at,
    )
    sources = cast(
        "tuple[SourceAlignedPublication, ...]",
        tuple(_ResolvedSource(reference) for reference in candidate.source_inputs),
    )
    plan = CanonicalCurationPlan(
        partition=candidate.partition,
        authority=_authority(candidate.partition),
        source_publications=sources,
    )
    return publish_candidate(
        PublishCandidateRequest(
            candidate=candidate,
            plan=plan,
            lease=grant,
            current=spec.current,
        )
    )


def test_adapters_implement_separate_authority_ports(
    attempts: DynamoAttemptStore,
    leases: DynamoLeaseStore,
    publications: DynamoPublicationStore,
) -> None:
    assert isinstance(attempts, AttemptStorePort)
    assert isinstance(leases, LeaseStorePort)
    assert isinstance(publications, PublicationStorePort)


def test_attempt_history_is_ordered_idempotent_and_success_is_commit_owned(
    probe: str,
    attempts: DynamoAttemptStore,
) -> None:
    partition = _partition(probe)
    now = datetime.now(UTC).replace(microsecond=0)
    started = AttemptEvent(
        attempt_id=AttemptId(f"{probe}-attempt"),
        partition=partition,
        kind=AttemptEventKind.STARTED,
        occurred_at=now,
        sequence=0,
    )
    succeeded = AttemptEvent(
        attempt_id=started.attempt_id,
        partition=partition,
        kind=AttemptEventKind.SUCCEEDED,
        occurred_at=now + timedelta(seconds=1),
        sequence=1,
    )

    success_refusal = attempts.append(succeeded)
    assert not success_refusal.accepted
    assert "atomically with CURRENT" in success_refusal.reason
    assert attempts.append(started).accepted
    assert not attempts.append(succeeded).accepted
    assert attempts.append(started).accepted

    failed = AttemptEvent(
        attempt_id=started.attempt_id,
        partition=partition,
        kind=AttemptEventKind.FAILED,
        occurred_at=now + timedelta(seconds=2),
        sequence=1,
        reason="terminal failure",
    )
    assert attempts.append(failed).accepted
    assert attempts.append(failed).accepted

    changed = AttemptEvent(
        attempt_id=started.attempt_id,
        partition=partition,
        kind=AttemptEventKind.STARTED,
        occurred_at=now + timedelta(seconds=2),
        sequence=0,
    )
    refusal = attempts.append(changed)
    assert not refusal.accepted
    assert "different evidence" in refusal.reason
    assert attempts.events_for(started.attempt_id) == (started, failed)


def test_lease_contention_renewal_release_and_fencing_are_atomic(
    probe: str,
    leases: DynamoLeaseStore,
) -> None:
    partition = _partition(probe)
    now = datetime.now(UTC).replace(microsecond=0)
    first = leases.acquire(_lease_request(probe, partition, now)).grant
    assert first is not None
    assert first.state.fencing_token == 1

    contender = leases.acquire(
        LeaseRequest(
            partition=partition,
            holder=LeaseHolderKey(f"{probe}-other"),
            requested_at=now + timedelta(seconds=1),
            duration=timedelta(minutes=5),
        )
    )
    assert contender.grant is None
    assert "live holder" in contender.reason

    renewed = leases.acquire(_lease_request(probe, partition, now + timedelta(seconds=2))).grant
    assert renewed is not None
    assert renewed.state.fencing_token == 2
    assert not leases.release(first, at=now + timedelta(seconds=3)).accepted
    assert leases.release(renewed, at=now + timedelta(seconds=3)).accepted

    replacement = leases.acquire(
        LeaseRequest(
            partition=partition,
            holder=LeaseHolderKey(f"{probe}-other"),
            requested_at=now + timedelta(seconds=3),
            duration=timedelta(minutes=5),
        )
    ).grant
    assert replacement is not None
    assert replacement.state.fencing_token == 3


def test_publication_commit_is_fenced_idempotent_and_superseding(
    probe: str,
    leases: DynamoLeaseStore,
    publications: DynamoPublicationStore,
) -> None:
    partition = _partition(probe)
    now = datetime.now(UTC).replace(microsecond=0)
    first_grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert first_grant is not None
    first = _publication(
        probe,
        first_grant,
        _PublicationSpec(published_at=now + timedelta(seconds=1)),
    )

    assert publications.commit(
        CommitPublicationRequest(first, first_grant, _authority(first.partition))
    ).accepted
    assert publications.commit(
        CommitPublicationRequest(first, first_grant, _authority(first.partition))
    ).accepted
    assert publications.current(partition) == first

    renewed = leases.acquire(_lease_request(probe, partition, now + timedelta(seconds=2))).grant
    assert renewed is not None
    stale = _publication(
        probe,
        first_grant,
        _PublicationSpec(
            published_at=now + timedelta(seconds=1),
            suffix="stale",
        ),
    )
    refusal = publications.commit(
        CommitPublicationRequest(stale, first_grant, _authority(stale.partition))
    )
    assert not refusal.accepted
    assert publications.current(partition) == first

    replacement = _publication(
        probe,
        renewed,
        _PublicationSpec(
            published_at=now + timedelta(seconds=3),
            suffix="two",
            current=first,
        ),
    )
    assert publications.commit(
        CommitPublicationRequest(replacement, renewed, _authority(replacement.partition))
    ).accepted
    assert publications.current(partition) == replacement
    assert replacement.supersedes == first.publication_id

    # A delayed retry must not report success after another publication became CURRENT.
    delayed = publications.commit(
        CommitPublicationRequest(first, first_grant, _authority(first.partition))
    )
    assert not delayed.accepted
    assert publications.current(partition) == replacement

    latest = leases.acquire(_lease_request(probe, partition, now + timedelta(seconds=4))).grant
    assert latest is not None
    omitted = _publication(
        probe,
        latest,
        _PublicationSpec(
            published_at=now + timedelta(seconds=5),
            suffix="omitted-predecessor",
        ),
    )
    assert not publications.commit(
        CommitPublicationRequest(omitted, latest, _authority(omitted.partition))
    ).accepted

    wrong = _publication(
        probe,
        latest,
        _PublicationSpec(
            published_at=now + timedelta(seconds=5),
            suffix="wrong-predecessor",
            current=first,
        ),
    )
    assert not publications.commit(
        CommitPublicationRequest(wrong, latest, _authority(wrong.partition))
    ).accepted
    assert publications.current(partition) == replacement


@pytest.mark.covers("ADR-047", "ADR-049")
def test_group_commit_is_all_or_none_when_one_member_loses_its_fence(
    probe: str,
    leases: DynamoLeaseStore,
    publications: DynamoPublicationStore,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    partitions = (_partition(probe), _partition(probe, date(2026, 8, 26)))
    first_grants = tuple(
        leases.acquire(_lease_request(probe, partition, now)).grant for partition in partitions
    )
    assert all(grant is not None for grant in first_grants)
    first = tuple(
        _publication(
            probe,
            cast("LeaseGrant", grant),
            _PublicationSpec(published_at=now + timedelta(seconds=1), suffix=f"first-{index}"),
        )
        for index, grant in enumerate(first_grants)
    )
    initial = CommitPublicationGroupRequest(
        tuple(
            CommitPublicationRequest(item, cast("LeaseGrant", grant), _authority(item.partition))
            for item, grant in zip(first, first_grants, strict=True)
        )
    )
    with pytest.raises(ValueError, match="20"):
        CommitPublicationGroupRequest(initial.members * 11)
    oversized_read = tuple(
        _partition(probe, date(2026, 1, 1) + timedelta(days=index)) for index in range(101)
    )
    with pytest.raises(ValueError, match="1-100"):
        publications.current_exact(oversized_read)
    assert publications.commit_group(initial).accepted

    replacement_grants = tuple(
        leases.acquire(_lease_request(probe, partition, now + timedelta(seconds=2))).grant
        for partition in partitions
    )
    assert all(grant is not None for grant in replacement_grants)
    replacements = tuple(
        _publication(
            probe,
            cast("LeaseGrant", grant),
            _PublicationSpec(
                published_at=now + timedelta(seconds=3),
                suffix=f"replacement-{index}",
                current=first[index],
            ),
        )
        for index, grant in enumerate(replacement_grants)
    )
    replaced_second = leases.acquire(
        _lease_request(probe, partitions[1], now + timedelta(seconds=3))
    ).grant
    assert replaced_second is not None

    stale_group = CommitPublicationGroupRequest(
        tuple(
            CommitPublicationRequest(item, cast("LeaseGrant", grant), _authority(item.partition))
            for item, grant in zip(replacements, replacement_grants, strict=True)
        )
    )
    outcome = publications.commit_group(stale_group)
    assert not outcome.accepted
    assert tuple(publications.current(partition) for partition in partitions) == first
    assert publications.current_exact(partitions) == first
    missing = _partition(probe, date(2026, 8, 27))
    inspected = publications.inspect_exact((missing, partitions[0]))
    assert inspected.publications == (None, first[0])
    assert publications.current_exact((partitions[0], missing)) is None


@pytest.mark.covers("ADR-047", "ADR-049")
def test_inspect_exact_refuses_current_that_appears_during_absence_attestation(
    probe: str,
    settings: Settings,
    leases: DynamoLeaseStore,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    partition = _partition(probe)
    grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert grant is not None
    publication = _publication(
        probe,
        grant,
        _PublicationSpec(published_at=now + timedelta(seconds=1), suffix="appeared"),
    )
    request = CommitPublicationRequest(publication, grant, _authority(partition))
    writer = DynamoPublicationStore(
        settings.semantic_publications_table,
        settings.semantic_leases_table,
        DynamoPublicationStoreAccess(attempts_table=settings.semantic_attempts_table),
    )
    reader_client = boto3.client("dynamodb")
    transaction_phases: list[str] = []
    commit_outcomes: list[bool] = []

    def publish_after_discovery(**_event: object) -> None:
        phase = "discovery" if not transaction_phases else "verify"
        transaction_phases.append(phase)
        if phase == "discovery":
            commit_outcomes.append(writer.commit(request).accepted)

    reader_client.meta.events.register(
        "after-call.dynamodb.TransactGetItems",
        publish_after_discovery,
    )
    reader = DynamoPublicationStore(
        settings.semantic_publications_table,
        settings.semantic_leases_table,
        DynamoPublicationStoreAccess(
            attempts_table=settings.semantic_attempts_table,
            client=reader_client,
        ),
    )

    with pytest.raises(DependencyError, match="changed"):
        reader.inspect_exact((partition,))

    assert commit_outcomes == [True]
    assert transaction_phases == ["discovery", "verify"]


@pytest.mark.covers("ADR-047", "ADR-049")
def test_group_commit_samples_one_pre_expiry_instant_for_every_member(
    probe: str,
    settings: Settings,
    leases: DynamoLeaseStore,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    sampled: list[datetime] = []

    def clock() -> datetime:
        sampled.append(now + timedelta(minutes=4))
        return sampled[-1]

    publications = DynamoPublicationStore(
        settings.semantic_publications_table,
        settings.semantic_leases_table,
        DynamoPublicationStoreAccess(attempts_table=settings.semantic_attempts_table, clock=clock),
    )
    partitions = (_partition(probe), _partition(probe, date(2026, 8, 26)))
    grants = tuple(
        leases.acquire(_lease_request(probe, partition, now)).grant for partition in partitions
    )
    assert all(grant is not None for grant in grants)
    successes = tuple(
        _publication(
            probe,
            cast("LeaseGrant", grant),
            _PublicationSpec(published_at=now + timedelta(seconds=1), suffix=f"clock-{index}"),
        )
        for index, grant in enumerate(grants)
    )
    request = CommitPublicationGroupRequest(
        tuple(
            CommitPublicationRequest(item, cast("LeaseGrant", grant), _authority(item.partition))
            for item, grant in zip(successes, grants, strict=True)
        )
    )

    assert publications.commit_group(request).accepted
    assert sampled == [now + timedelta(minutes=4)]
    assert publications.current_exact(partitions) == successes


@pytest.mark.covers("ADR-047", "ADR-049")
def test_oversized_publication_record_is_refused_before_transaction(
    probe: str,
    leases: DynamoLeaseStore,
    publications: DynamoPublicationStore,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    partition = _partition(probe)
    grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert grant is not None
    publication = _publication(
        probe,
        grant,
        _PublicationSpec(published_at=now + timedelta(seconds=1), suffix="oversized"),
    )
    first_result = publication.evidence.results[0]
    oversized = replace(
        publication,
        evidence=replace(
            publication.evidence,
            results=(
                replace(first_result, detail="x" * 40_000),
                *publication.evidence.results[1:],
            ),
        ),
    )

    with pytest.raises(ValueError, match="transaction-size"):
        publications.commit(CommitPublicationRequest(oversized, grant, _authority(partition)))

    assert publications.current(partition) is None


@pytest.mark.covers("ADR-047", "ADR-049")
def test_commit_at_expiry_equality_is_refused_without_partial_state(
    probe: str,
    settings: Settings,
    leases: DynamoLeaseStore,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    partition = _partition(probe)
    grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert grant is not None
    publication = _publication(
        probe,
        grant,
        _PublicationSpec(published_at=now + timedelta(seconds=1), suffix="expiry-equality"),
    )
    publications = DynamoPublicationStore(
        settings.semantic_publications_table,
        settings.semantic_leases_table,
        DynamoPublicationStoreAccess(
            attempts_table=settings.semantic_attempts_table, clock=lambda: grant.state.expires_at
        ),
    )

    outcome = publications.commit(
        CommitPublicationRequest(publication, grant, _authority(partition))
    )

    assert not outcome.accepted
    assert "expired" in outcome.reason
    assert publications.current(partition) is None


@pytest.mark.covers("ADR-047", "ADR-049")
def test_exact_committed_replay_converges_after_lease_expiry(
    probe: str,
    settings: Settings,
    leases: DynamoLeaseStore,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    partition = _partition(probe)
    grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert grant is not None
    publication = _publication(
        probe,
        grant,
        _PublicationSpec(published_at=now + timedelta(seconds=1), suffix="expired-replay"),
    )
    commit_at = [now + timedelta(minutes=4)]
    publications = DynamoPublicationStore(
        settings.semantic_publications_table,
        settings.semantic_leases_table,
        DynamoPublicationStoreAccess(
            attempts_table=settings.semantic_attempts_table, clock=lambda: commit_at[0]
        ),
    )
    request = CommitPublicationRequest(publication, grant, _authority(partition))
    assert publications.commit(request).accepted

    commit_at[0] = grant.state.expires_at

    assert publications.commit(request).accepted
    assert publications.current(partition) == publication


@pytest.mark.covers("ADR-047", "ADR-049")
def test_current_exact_refuses_missing_immutable_publication(
    probe: str,
    settings: Settings,
    leases: DynamoLeaseStore,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    partition = _partition(probe)
    grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert grant is not None
    publication = _publication(
        probe,
        grant,
        _PublicationSpec(published_at=now + timedelta(seconds=1), suffix="missing-attestation"),
    )
    publications = DynamoPublicationStore(
        settings.semantic_publications_table,
        settings.semantic_leases_table,
        DynamoPublicationStoreAccess(
            attempts_table=settings.semantic_attempts_table,
            clock=lambda: now + timedelta(seconds=2),
        ),
    )
    assert publications.commit(
        CommitPublicationRequest(publication, grant, _authority(partition))
    ).accepted
    boto3.client("dynamodb").delete_item(
        TableName=settings.semantic_publications_table,
        Key={
            PARTITION_KEY: {"S": partition.identity},
            PUBLICATION_RECORD_KEY: {"S": f"PUBLICATION#{publication.publication_id.value}"},
        },
    )

    with pytest.raises(DependencyError, match="immutable"):
        publications.current_exact((partition,))


@pytest.mark.covers("ADR-047", "ADR-049")
def test_current_exact_refuses_divergent_immutable_publication(
    probe: str,
    settings: Settings,
    leases: DynamoLeaseStore,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    partition = _partition(probe)
    grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert grant is not None
    publication = _publication(
        probe,
        grant,
        _PublicationSpec(published_at=now + timedelta(seconds=1), suffix="divergent-attestation"),
    )
    publications = DynamoPublicationStore(
        settings.semantic_publications_table,
        settings.semantic_leases_table,
        DynamoPublicationStoreAccess(
            attempts_table=settings.semantic_attempts_table,
            clock=lambda: now + timedelta(seconds=2),
        ),
    )
    assert publications.commit(
        CommitPublicationRequest(publication, grant, _authority(partition))
    ).accepted
    client = boto3.client("dynamodb")
    key = {
        PARTITION_KEY: {"S": partition.identity},
        PUBLICATION_RECORD_KEY: {"S": f"PUBLICATION#{publication.publication_id.value}"},
    }
    response = client.get_item(
        TableName=settings.semantic_publications_table,
        Key=key,
        ConsistentRead=True,
    )
    item = response.get("Item")
    assert item is not None
    encoded_payload = item[PAYLOAD].get("S")
    assert encoded_payload is not None
    payload = json.loads(encoded_payload)
    payload["published_at"] = (publication.published_at + timedelta(seconds=1)).isoformat()
    item[PAYLOAD] = {"S": json.dumps(payload, sort_keys=True, separators=(",", ":"))}
    client.put_item(TableName=settings.semantic_publications_table, Item=item)

    with pytest.raises(DependencyError, match="immutable"):
        publications.current_exact((partition,))


@pytest.mark.covers("ADR-047", "ADR-049")
def test_full_current_envelope_is_required_for_reads_and_replay(
    probe: str,
    settings: Settings,
    leases: DynamoLeaseStore,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    partition = _partition(probe)
    grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert grant is not None
    publication = _publication(
        probe,
        grant,
        _PublicationSpec(published_at=now + timedelta(seconds=1), suffix="envelope"),
    )
    publications = DynamoPublicationStore(
        settings.semantic_publications_table,
        settings.semantic_leases_table,
        DynamoPublicationStoreAccess(
            attempts_table=settings.semantic_attempts_table,
            clock=lambda: now + timedelta(seconds=2),
        ),
    )
    request = CommitPublicationRequest(publication, grant, _authority(partition))
    assert publications.commit(request).accepted

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if publications.current_for_scope(partition.scope) == (publication,):
            break
        time.sleep(0.5)
    assert publications.current_for_scope(partition.scope) == (publication,)

    client = boto3.client("dynamodb")
    key = {
        PARTITION_KEY: {"S": partition.identity},
        PUBLICATION_RECORD_KEY: {"S": CURRENT_RECORD},
    }
    response = client.get_item(
        TableName=settings.semantic_publications_table,
        Key=key,
        ConsistentRead=True,
    )
    item = response.get("Item")
    assert item is not None
    item["unexpected_attribute"] = {"S": "must be refused"}
    client.put_item(TableName=settings.semantic_publications_table, Item=item)

    with pytest.raises(DependencyError, match="head vector"):
        publications.current(partition)
    with pytest.raises(DependencyError, match="head vector"):
        publications.current_exact((partition,))
    with pytest.raises(DependencyError, match="diverges"):
        publications.current_for_scope(partition.scope)
    assert not publications.commit(request).accepted


@pytest.mark.covers("ADR-049")
@pytest.mark.parametrize("family", list(PrepublicationRuleFamily))
def test_failed_prepublication_family_never_advances_real_current_or_scope_coverage(
    family: PrepublicationRuleFamily,
    probe: str,
    settings: Settings,
) -> None:
    leases = DynamoLeaseStore(settings.semantic_leases_table)
    publications = DynamoPublicationStore(
        settings.semantic_publications_table,
        settings.semantic_leases_table,
        DynamoPublicationStoreAccess(attempts_table=settings.semantic_attempts_table),
    )
    probe = f"{probe.split('[', maxsplit=1)[0]}-{family.value}"
    partition = _partition(probe)
    now = datetime.now(UTC).replace(microsecond=0)
    first_grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert first_grant is not None
    current = _publication(
        probe,
        first_grant,
        _PublicationSpec(published_at=now + timedelta(seconds=1)),
    )
    assert publications.commit(
        CommitPublicationRequest(current, first_grant, _authority(partition))
    ).accepted

    deadline = time.monotonic() + 20
    before: tuple[SuccessfulPublication, ...] = ()
    while time.monotonic() < deadline:
        before = publications.current_for_scope(partition.scope)
        if before == (current,):
            break
        time.sleep(0.5)
    assert before == (current,)

    renewed = leases.acquire(_lease_request(probe, partition, now + timedelta(seconds=2))).grant
    assert renewed is not None
    with pytest.raises(ValueError, match=family.value):
        _publication(
            probe,
            renewed,
            _PublicationSpec(
                published_at=now + timedelta(seconds=3),
                suffix="refused",
                current=current,
                failed_family=family,
            ),
        )

    assert publications.current(partition) == current
    assert publications.current_for_scope(partition.scope) == before


def test_non_success_attempt_outcomes_leave_current_publication_unchanged(
    probe: str,
    settings: Settings,
) -> None:
    attempts = DynamoAttemptStore(settings.semantic_attempts_table)
    leases = DynamoLeaseStore(settings.semantic_leases_table)
    publications = DynamoPublicationStore(
        settings.semantic_publications_table,
        settings.semantic_leases_table,
        DynamoPublicationStoreAccess(attempts_table=settings.semantic_attempts_table),
    )
    partition = _partition(probe)
    now = datetime.now(UTC).replace(microsecond=0)
    grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert grant is not None
    current = _publication(
        probe,
        grant,
        _PublicationSpec(published_at=now + timedelta(seconds=1)),
    )
    assert publications.commit(
        CommitPublicationRequest(current, grant, _authority(current.partition))
    ).accepted

    for sequence, kind in enumerate(
        (
            AttemptEventKind.SKIPPED,
            AttemptEventKind.REJECTED,
            AttemptEventKind.QUARANTINED,
            AttemptEventKind.FAILED,
        ),
        start=1,
    ):
        attempt_id = AttemptId(f"{probe}-attempt-outcome-{sequence}")
        started = AttemptEvent(
            attempt_id=attempt_id,
            partition=partition,
            kind=AttemptEventKind.STARTED,
            occurred_at=now + timedelta(seconds=sequence + 1),
            sequence=0,
        )
        terminal = AttemptEvent(
            attempt_id=attempt_id,
            partition=partition,
            kind=kind,
            occurred_at=now + timedelta(seconds=sequence + 2),
            sequence=1,
            reason=(None if kind is AttemptEventKind.SKIPPED else "probe outcome"),
        )
        assert attempts.append(started).accepted
        assert attempts.append(terminal).accepted
        assert publications.current(partition) == current


def test_scope_discovery_returns_strong_current_pointers_for_exact_buckets(
    probe: str,
    leases: DynamoLeaseStore,
    publications: DynamoPublicationStore,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    expected: list[SuccessfulPublication] = []
    for offset in (0, 2):
        partition = _partition(probe, date(2026, 8, 25) + timedelta(days=offset))
        grant = leases.acquire(_lease_request(probe, partition, now)).grant
        assert grant is not None
        publication = _publication(
            probe,
            grant,
            _PublicationSpec(
                published_at=now + timedelta(seconds=1),
                suffix=f"bucket-{offset}",
            ),
        )
        assert publications.commit(
            CommitPublicationRequest(publication, grant, _authority(publication.partition))
        ).accepted
        expected.append(publication)

    deadline = time.monotonic() + 20
    found: tuple[SuccessfulPublication, ...] = ()
    while time.monotonic() < deadline:
        found = publications.current_for_scope(expected[0].partition.scope)
        if found == tuple(expected):
            break
        time.sleep(0.5)

    assert found == tuple(expected)
    assert [item.partition.bucket.window for item in found] == [
        DateRange(date(2026, 8, 25), date(2026, 8, 25)),
        DateRange(date(2026, 8, 27), date(2026, 8, 27)),
    ]


@pytest.mark.covers("ADR-070")
def test_a_quarter_sized_exact_read_attests_184_partitions_in_chunked_passes(
    probe: str,
    publications: DynamoPublicationStore,
    settings: Settings,
) -> None:
    """The Reach quarter's 184 day partitions, over the former 100, read as one verified vector."""
    now = datetime.now(UTC).replace(microsecond=0)
    leases = DynamoLeaseStore(settings.semantic_leases_table)
    expected: list[SuccessfulPublication] = []
    for offset in range(184):
        partition = _partition(probe, date(2026, 4, 1) + timedelta(days=offset))
        grant = leases.acquire(_lease_request(probe, partition, now)).grant
        assert grant is not None
        publication = _publication(
            probe,
            grant,
            _PublicationSpec(published_at=now + timedelta(seconds=1), suffix=f"day-{offset}"),
        )
        assert publications.commit(
            CommitPublicationRequest(publication, grant, _authority(partition))
        ).accepted
        expected.append(publication)
    partitions = tuple(publication.partition for publication in expected)

    assert publications.inspect_exact(partitions).publications == tuple(expected)
    serving = DynamoCurrentPublications(settings.semantic_publications_table)
    assert serving.inspect_exact(partitions).publications == tuple(expected)
    # Paired: one partition never published is a stable absence in its own slot, not a refusal.
    absent = _partition(probe, date(2026, 4, 1) + timedelta(days=184))
    inspected = serving.inspect_exact((*partitions, absent)).publications
    assert inspected[:184] == tuple(expected) and inspected[184] is None


@pytest.mark.covers("ADR-075")
def test_first_publication_seeds_its_own_servable_pointer_under_inline_projection(
    probe: str,
    publications: DynamoPublicationStore,
    settings: Settings,
) -> None:
    """The deployed composition projects before commit, so commit means servable."""
    now = datetime.now(tz=UTC)
    leases = DynamoLeaseStore(settings.semantic_leases_table)
    partition = _partition(probe)
    grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert grant is not None
    publication = _publication(probe, grant, _PublicationSpec(published_at=now))
    assert publications.commit(
        CommitPublicationRequest(publication, grant, _authority(partition))
    ).accepted

    raw = publications.raw_current(partition.identity)
    assert raw is not None
    assert raw["projected_publication_id"] == {"S": publication.publication_id.value}
    assert raw["projected_fencing_token"] == {"N": str(publication.fencing_token)}
    servable = publications.servable_by_key(partition.identity)
    assert servable == publication
    readers = DynamoCurrentPublications(settings.semantic_publications_table)
    assert readers.current(partition) == publication
    assert (
        readers.current_for_trigger(partition.identity, publication.publication_id.value)
        == publication
    )


@pytest.mark.covers("ADR-075")
def test_servable_pointer_trails_authority_and_advances_forward_only(
    probe: str,
    publications: DynamoPublicationStore,
    settings: Settings,
) -> None:
    """An S3-only composition seeds NONE; readers see nothing until the advance lands."""
    now = datetime.now(tz=UTC)
    leases = DynamoLeaseStore(settings.semantic_leases_table)
    partition = _partition(probe)
    deferred = DynamoPublicationStore(
        settings.semantic_publications_table,
        settings.semantic_leases_table,
        DynamoPublicationStoreAccess(
            attempts_table=settings.semantic_attempts_table,
            inline_projection=False,
        ),
    )
    grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert grant is not None
    first = _publication(probe, grant, _PublicationSpec(published_at=now))
    assert deferred.commit(CommitPublicationRequest(first, grant, _authority(partition))).accepted

    readers = DynamoCurrentPublications(settings.semantic_publications_table)
    raw = deferred.raw_current(partition.identity)
    assert raw is not None
    assert raw["projected_publication_id"] == {"S": "NONE"}
    assert deferred.servable_by_key(partition.identity) is None
    assert readers.current(partition) is None
    assert publications.current(partition) == first  # authority is unaffected

    assert (
        deferred.advance_servable_projection(
            ServableProjection(
                partition_key=partition.identity,
                publication_id=first.publication_id.value,
                fencing_token=first.fencing_token,
                projected_at=now + timedelta(seconds=1),
            )
        )
        is True
    )
    assert readers.current(partition) == first
    assert (
        deferred.advance_servable_projection(
            ServableProjection(
                partition_key=partition.identity,
                publication_id=first.publication_id.value,
                fencing_token=first.fencing_token,
                projected_at=now + timedelta(seconds=1),
            )
        )
        is False
    )  # equal token: already servable

    second_grant = leases.acquire(
        _lease_request(probe, partition, now + timedelta(seconds=2))
    ).grant
    assert second_grant is not None
    second = _publication(
        probe,
        second_grant,
        _PublicationSpec(published_at=now + timedelta(seconds=3), suffix="two", current=first),
    )
    assert deferred.commit(
        CommitPublicationRequest(second, second_grant, _authority(partition))
    ).accepted
    # Authority moved; the servable pointer still names the fully projected predecessor.
    assert publications.current(partition) == second
    assert readers.current(partition) == first
    assert readers.inspect_exact((partition,)).publications == (first,)
    assert readers.current_for_trigger(partition.identity, second.publication_id.value) is None

    assert deferred.advance_servable(second) is True
    assert readers.current(partition) == second
    assert deferred.advance_servable(first) is False  # older token never regresses


@pytest.mark.covers("ADR-075")
def test_a_legacy_current_item_is_unreadable_to_serving_reads_until_stamped(
    probe: str,
    publications: DynamoPublicationStore,
    settings: Settings,
) -> None:
    """Pre-migration items lack the pointer; serving reads refuse them until the stamp (D25)."""
    now = datetime.now(tz=UTC)
    leases = DynamoLeaseStore(settings.semantic_leases_table)
    partition = _partition(probe)
    grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert grant is not None
    publication = _publication(probe, grant, _PublicationSpec(published_at=now))
    assert publications.commit(
        CommitPublicationRequest(publication, grant, _authority(partition))
    ).accepted
    # Rewrite the CURRENT item in the deployed legacy shape (whole-item Put, no pointer).
    client = boto3.client("dynamodb")
    legacy = _publication_item_for_test(publication)
    client.put_item(TableName=settings.semantic_publications_table, Item=cast("Any", legacy))

    readers = DynamoCurrentPublications(settings.semantic_publications_table)
    raw = publications.raw_current(partition.identity)
    assert raw is not None
    assert "projected_publication_id" not in raw
    with pytest.raises(DependencyError, match="head vector") as refused:
        readers.current(partition)
    assert refused.value.code == PUBLICATION_UNREADABLE
    # Only the retiring trigger rule keeps resolving authority for an unstamped item.
    assert publications.servable_by_key(partition.identity) == publication

    # The attribute_not_exists disjunct stamps the legacy partition on its first advance.
    assert publications.advance_servable(publication) is True
    stamped = publications.raw_current(partition.identity)
    assert stamped is not None
    assert stamped["projected_publication_id"] == {"S": publication.publication_id.value}
    assert readers.current(partition) == publication


def _publication_item_for_test(
    publication: SuccessfulPublication,
) -> dict[str, object]:
    from adapters.outbound.dynamodb.semantic_state import (  # noqa: PLC0415
        _publication_item,  # pyright: ignore[reportPrivateUsage]
    )

    return cast("dict[str, object]", _publication_item(publication, current=True))


@pytest.mark.covers("ADR-070", "ADR-075")
def test_servable_exact_inspection_retries_a_projection_advance_between_head_passes(
    probe: str,
    settings: Settings,
) -> None:
    """A/C equality refuses the mixed attempt; the bounded retry then reads the new head."""
    now = datetime.now(tz=UTC).replace(microsecond=0)
    leases = DynamoLeaseStore(settings.semantic_leases_table)
    partition = _partition(probe)
    deferred = DynamoPublicationStore(
        settings.semantic_publications_table,
        settings.semantic_leases_table,
        DynamoPublicationStoreAccess(
            attempts_table=settings.semantic_attempts_table,
            inline_projection=False,
        ),
    )
    first_grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert first_grant is not None
    first = _publication(probe, first_grant, _PublicationSpec(published_at=now))
    assert deferred.commit(
        CommitPublicationRequest(first, first_grant, _authority(partition))
    ).accepted
    assert deferred.advance_servable(first)
    second_grant = leases.acquire(
        _lease_request(probe, partition, now + timedelta(seconds=2))
    ).grant
    assert second_grant is not None
    second = _publication(
        probe,
        second_grant,
        _PublicationSpec(
            published_at=now + timedelta(seconds=3),
            suffix="second",
            current=first,
        ),
    )
    assert deferred.commit(
        CommitPublicationRequest(second, second_grant, _authority(partition))
    ).accepted

    reader_client = boto3.client("dynamodb")
    phases: list[str] = []

    def advance_after_discovery(**_event: object) -> None:
        phase = ("discovery", "immutable", "verify")[len(phases) % 3]
        phases.append(phase)
        if len(phases) == 1:
            assert deferred.advance_servable_projection(
                ServableProjection(
                    partition.identity,
                    second.publication_id.value,
                    second.fencing_token,
                    now + timedelta(seconds=4),
                )
            )

    reader_client.meta.events.register(
        "after-call.dynamodb.TransactGetItems",
        advance_after_discovery,
    )
    reader = DynamoCurrentPublications(
        settings.semantic_publications_table,
        client=reader_client,
    )

    assert reader.inspect_exact((partition,)).publications == (second,)

    assert phases == ["discovery", "immutable", "verify"] * 2


@pytest.mark.covers("ADR-070", "ADR-075")
@pytest.mark.parametrize("corruption", ("naive_projected_at", "none_with_positive_fence"))
def test_exact_head_decoder_refuses_complete_but_invalid_projection_metadata(
    settings: Settings,
    publications: DynamoPublicationStore,
    corruption: str,
) -> None:
    """Both authority and serving modes fail closed on a malformed complete pointer tuple."""
    probe = probe_key(f"exact-head-{corruption}")
    now = datetime.now(tz=UTC).replace(microsecond=0)
    leases = DynamoLeaseStore(settings.semantic_leases_table)
    partition = _partition(probe)
    grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert grant is not None
    publication = _publication(probe, grant, _PublicationSpec(published_at=now))
    assert publications.commit(
        CommitPublicationRequest(publication, grant, _authority(partition))
    ).accepted

    client = boto3.client("dynamodb")
    key = {
        PARTITION_KEY: {"S": partition.identity},
        PUBLICATION_RECORD_KEY: {"S": CURRENT_RECORD},
    }
    response = client.get_item(
        TableName=settings.semantic_publications_table,
        Key=key,
        ConsistentRead=True,
    )
    item = response.get("Item")
    assert item is not None
    if corruption == "naive_projected_at":
        item["projected_at"] = {"S": "2026-09-22"}
    else:
        item["projected_publication_id"] = {"S": "NONE"}
        item["projected_fencing_token"] = {"N": "1"}
    client.put_item(TableName=settings.semantic_publications_table, Item=item)

    with pytest.raises(DependencyError, match="head vector"):
        publications.inspect_exact((partition,))
    with pytest.raises(DependencyError, match="head vector"):
        DynamoCurrentPublications(settings.semantic_publications_table).inspect_exact((partition,))


@pytest.mark.covers("ADR-075")
def test_graph_publish_stages_flips_and_folds_one_probe_window(
    probe: str,
    publications: DynamoPublicationStore,
    settings: Settings,
) -> None:
    """S6 §2.6 over the live publications table: staged is invisible until the window flips."""
    now = datetime.now(tz=UTC)
    leases = DynamoLeaseStore(settings.semantic_leases_table)
    partition = _partition(probe)
    grant = leases.acquire(_lease_request(probe, partition, now)).grant
    assert grant is not None
    publication = _publication(probe, grant, _PublicationSpec(published_at=now))
    authority_only = DynamoPublicationStore(
        settings.semantic_publications_table,
        settings.semantic_leases_table,
        DynamoPublicationStoreAccess(
            attempts_table=settings.semantic_attempts_table, inline_projection=False
        ),
    )
    assert authority_only.commit(
        CommitPublicationRequest(publication, grant, _authority(partition))
    ).accepted
    readers = DynamoCurrentPublications(settings.semantic_publications_table)
    assert readers.current(partition) is None  # NONE base: a GRAPH commit advances nothing
    grant_id = f"{probe}-grant"
    target = PublishTarget(
        partition.identity, publication.publication_id.value, publication.fencing_token
    )
    # The conflict unit is probe-prefixed, so the sweep removes GENERATION#<probe> (AGG4-19).
    staging = GrantStaging(grant_id, 1, probe, now)
    staged = publications.stage_for_grant(staging, target, max_redecide=8)
    assert staged.classification is StageClass.STAGED
    replayed = publications.stage_for_grant(staging, target, max_redecide=8)
    assert replayed.classification is StageClass.STAGED
    assert readers.current(partition) is None  # staged but unflipped is invisible
    request = FlipRequest(
        conflict_unit=probe,
        grant_id=grant_id,
        grant_ordinal=1,
        run_id=f"{probe}-run",
        root_flow_id=f"{probe}-root",
        authority_epoch=1,
        retrieved_at=now.isoformat(),
        revision_epoch=int(now.timestamp()),
        counts=PublishCounts.classify((staged,)),
        target_count=1,
        target_digest="d" * 64,
        flipped_at=now,
        fences=(),
    )
    assert publications.flip_generation(request) is FlipOutcome.FLIPPED
    assert publications.flip_generation(request) is FlipOutcome.ALREADY_FLIPPED
    assert readers.current(partition) == publication  # flipped is visible
    assert publications.fold_for_grant(grant_id, partition.identity) is FoldOutcome.ADVANCED
    assert publications.fold_for_grant(grant_id, partition.identity) is FoldOutcome.ABSENT
    raw = publications.raw_current(partition.identity)
    assert raw is not None
    assert raw["projected_publication_id"] == {"S": publication.publication_id.value}
    assert staged_attribute(grant_id) not in raw
    assert readers.current(partition) == publication
