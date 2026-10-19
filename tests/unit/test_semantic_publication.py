# SPDX-License-Identifier: MIT-0
"""Semantic V2 attempts, fenced leases, successful publications and exact coverage.

SA-06 through SA-08. These are domain acceptance tests: failed attempts create no publication,
stale holders cannot publish, and coverage coalesces adjacent success while preserving gaps.
Persistence adapters are covered separately against real DynamoDB.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest

from domain.semantics.curation import CanonicalCurationPlan
from domain.semantics.identity import CommerceScopeKey
from domain.semantics.publication import (
    AttemptEvent,
    AttemptEventKind,
    AttemptId,
    AuthorityCheck,
    CanonicalDatasetKey,
    CanonicalPublisherBinding,
    CanonicalPublisherKey,
    ExactCoverage,
    LeaseGrant,
    LeaseHolderKey,
    LeaseRequest,
    LeaseUnavailableError,
    PartitionIdentity,
    PublicationCandidate,
    PublicationEvidence,
    PublicationGraph,
    PublicationId,
    PublicationLensKey,
    PublicationOwnershipError,
    PublicationScope,
    PublicationStateError,
    PublishCandidateRequest,
    SourcePublicationRef,
    SuccessfulPublication,
    acquire_lease,
    coverage_from,
    publish_candidate,
    successful_matches_candidate,
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
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-047")]

NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
HOLDER = LeaseHolderKey("worker-1")
CALENDAR = BusinessCalendarKey("iso_gregorian", week_starts_on=0)
DATASET = CanonicalDatasetKey("retail_sales_daily")
PUBLISHER = CanonicalPublisherKey("canonical-curation")
GRAPH = PublicationGraph(
    expected_datasets=frozenset({DATASET}),
    bindings=(CanonicalPublisherBinding(DATASET, PUBLISHER),),
)


def _evidence(
    subject: PublicationEvidenceSubject,
    failed: PrepublicationRuleFamily | None = None,
) -> PublicationEvidence:
    return PublicationEvidence(
        rule_set_version="v2",
        subject=subject,
        results=tuple(
            PrepublicationRuleResult(
                family=family,
                rule_id=family.value,
                outcome=RuleOutcome.FAIL if family is failed else RuleOutcome.PASSED,
                detail="injected failure" if family is failed else "check passed",
            )
            for family in PrepublicationRuleFamily
        ),
    )


def _partition(start: date = date(2026, 8, 25)) -> PartitionIdentity:
    return PartitionIdentity(
        scope=PublicationScope(
            dataset=DATASET,
            commerce_scope=CommerceScopeKey("amazon_us"),
            population_key="manufacturing_retail",
            lens=PublicationLensKey("default"),
            time_class=FactTimeClass.DAILY_ADDITIVE,
            business_calendar=CALENDAR,
            source_timezone="PST",
        ),
        bucket=MeasurementBucket(
            window=DateRange(start, start),
            grain=TimeGrain.DAY,
            calendar=CALENDAR,
            source_timezone="PST",
        ),
    )


def _subject(
    publisher: CanonicalPublisherKey,
    partition: PartitionIdentity,
    revision: SourceRevision,
) -> PublicationEvidenceSubject:
    return PublicationEvidenceSubject(
        publisher=publisher.value,
        partition=partition.identity,
        source_publications=("source-publication-1",),
        source_revision="|".join(
            (
                revision.scheme.value,
                revision.precision.value,
                revision.observed_at.isoformat(),
                revision.source_id,
            )
        ),
        content_checksum="sha256:example",
    )


@dataclass(frozen=True, slots=True)
class _CandidateSpec:
    """Overrides for one candidate fixture, without a six-argument helper."""

    partition: PartitionIdentity | None = None
    token: int = 1
    holder: LeaseHolderKey = HOLDER
    publisher: CanonicalPublisherKey = PUBLISHER
    evidence: PublicationEvidence | None = None
    publication_id: str = "publication-1"
    published_at: datetime = NOW + timedelta(minutes=1)


def _grant(
    partition: PartitionIdentity | None = None,
    *,
    holder: LeaseHolderKey = HOLDER,
    now: datetime = NOW,
) -> LeaseGrant:
    return acquire_lease(
        None,
        LeaseRequest(
            partition=partition or _partition(),
            holder=holder,
            requested_at=now,
            duration=timedelta(minutes=5),
        ),
    )


def _candidate(spec: _CandidateSpec | None = None) -> PublicationCandidate:
    chosen = spec or _CandidateSpec()
    partition = chosen.partition or _partition()
    revision = SourceRevision.instant(
        scheme=RevisionSchemeKey("test.source_revision"),
        source_id="revision-1",
        observed_at=NOW,
    )
    return PublicationCandidate(
        publication_id=PublicationId(chosen.publication_id),
        publisher=chosen.publisher,
        partition=partition,
        attempt_id=AttemptId("attempt-1"),
        holder=chosen.holder,
        fencing_token=chosen.token,
        source_inputs=(SourcePublicationRef("source-publication-1"),),
        source_revision=revision,
        content_checksum="sha256:example",
        evidence=chosen.evidence or _evidence(_subject(chosen.publisher, partition, revision)),
        published_at=chosen.published_at,
    )


@dataclass(frozen=True, slots=True)
class _ResolvedSource:
    reference: SourcePublicationRef


def _publish(
    candidate: PublicationCandidate,
    *,
    lease: LeaseGrant,
    current: SuccessfulPublication | None,
) -> SuccessfulPublication:
    sources = cast(
        "tuple[SourceAlignedPublication, ...]",
        tuple(_ResolvedSource(reference) for reference in candidate.source_inputs),
    )
    plan = CanonicalCurationPlan(
        partition=candidate.partition,
        authority=GRAPH.authorize(candidate.partition.scope.dataset, PUBLISHER),
        source_publications=sources,
    )
    return publish_candidate(
        PublishCandidateRequest(
            candidate=candidate,
            plan=plan,
            lease=lease,
            current=current,
        )
    )


def _success(start: date, *, publication_id: str) -> SuccessfulPublication:
    partition = _partition(start)
    return _publish(
        _candidate(_CandidateSpec(partition=partition, publication_id=publication_id)),
        lease=_grant(partition),
        current=None,
    )


class TestPartitionIdentity:
    def test_option_lenses_have_distinct_scope_and_partition_identities(self) -> None:
        first = _partition()
        second = PartitionIdentity(
            scope=replace(first.scope, lens=PublicationLensKey("another_lens")),
            bucket=first.bucket,
        )
        assert first.scope.identity != second.scope.identity
        assert first.identity != second.identity

    def test_daily_additive_scope_refuses_a_month_bucket(self) -> None:
        daily = _partition()
        monthly = MeasurementBucket(
            window=DateRange(date(2026, 8, 1), date(2026, 8, 31)),
            grain=TimeGrain.MONTH,
            calendar=CALENDAR,
            source_timezone="PST",
        )
        with pytest.raises(PublicationStateError, match="only DAY"):
            PartitionIdentity(scope=daily.scope, bucket=monthly)

    @pytest.mark.parametrize(
        "bucket",
        [
            MeasurementBucket(
                window=DateRange(date(2026, 8, 25), date(2026, 8, 25)),
                grain=TimeGrain.DAY,
                calendar=BusinessCalendarKey("retail_445", week_starts_on=6),
                source_timezone="PST",
            ),
            MeasurementBucket(
                window=DateRange(date(2026, 8, 25), date(2026, 8, 25)),
                grain=TimeGrain.DAY,
                calendar=CALENDAR,
                source_timezone="UTC",
            ),
        ],
    )
    def test_bucket_calendar_and_timezone_must_match_scope(
        self,
        bucket: MeasurementBucket,
    ) -> None:
        with pytest.raises(PublicationStateError, match=r"calendar|timezone"):
            PartitionIdentity(scope=_partition().scope, bucket=bucket)


class TestAttemptHistory:
    def test_started_event_is_sequence_zero(self) -> None:
        event = AttemptEvent(
            attempt_id=AttemptId("attempt-1"),
            partition=_partition(),
            kind=AttemptEventKind.STARTED,
            occurred_at=NOW,
            sequence=0,
        )
        assert event.event_key == "00000000"

    def test_terminal_event_follows_started(self) -> None:
        event = AttemptEvent(
            attempt_id=AttemptId("attempt-1"),
            partition=_partition(),
            kind=AttemptEventKind.SUCCEEDED,
            occurred_at=NOW + timedelta(seconds=1),
            sequence=1,
        )
        assert event.event_key == "00000001"

    def test_failed_attempt_requires_a_reason(self) -> None:
        with pytest.raises(PublicationStateError, match="requires a reason"):
            AttemptEvent(
                attempt_id=AttemptId("attempt-1"),
                partition=_partition(),
                kind=AttemptEventKind.FAILED,
                occurred_at=NOW,
                sequence=1,
            )

    def test_retry_links_to_predecessor_without_rewriting_it(self) -> None:
        retry = AttemptEvent(
            attempt_id=AttemptId("attempt-2"),
            predecessor=AttemptId("attempt-1"),
            partition=_partition(),
            kind=AttemptEventKind.STARTED,
            occurred_at=NOW,
            sequence=0,
        )
        assert retry.predecessor == AttemptId("attempt-1")


class TestFencedLease:
    def test_first_grant_starts_at_token_one(self) -> None:
        grant = _grant()
        assert grant.state.fencing_token == 1

    def test_live_other_holder_is_refused(self) -> None:
        current = _grant().state
        with pytest.raises(LeaseUnavailableError, match="worker-1"):
            acquire_lease(
                current,
                LeaseRequest(
                    partition=_partition(),
                    holder=LeaseHolderKey("worker-2"),
                    requested_at=NOW + timedelta(minutes=1),
                    duration=timedelta(minutes=5),
                ),
            )

    def test_expired_lease_can_be_replaced_with_a_higher_fence(self) -> None:
        current = _grant().state
        following = acquire_lease(
            current,
            LeaseRequest(
                partition=_partition(),
                holder=LeaseHolderKey("worker-2"),
                requested_at=NOW + timedelta(minutes=6),
                duration=timedelta(minutes=5),
            ),
        )
        assert following.state.fencing_token == 2
        assert following.state.holder == LeaseHolderKey("worker-2")

    def test_renewal_invalidates_the_holders_prior_token(self) -> None:
        first = _grant()
        renewed = acquire_lease(
            first.state,
            LeaseRequest(
                partition=_partition(),
                holder=LeaseHolderKey("worker-1"),
                requested_at=NOW + timedelta(minutes=1),
                duration=timedelta(minutes=5),
            ),
        )
        assert renewed.state.fencing_token == 2
        with pytest.raises(PublicationStateError, match="stale fencing token"):
            renewed.require_authority(
                AuthorityCheck(
                    partition=_partition(),
                    holder=HOLDER,
                    fencing_token=1,
                    at=NOW + timedelta(minutes=2),
                )
            )


class TestSuccessfulPublication:
    @pytest.mark.covers("ADR-044")
    def test_wrong_canonical_publisher_cannot_create_a_success(self) -> None:
        candidate = _candidate(_CandidateSpec(publisher=CanonicalPublisherKey("source-contract")))
        with pytest.raises(PublicationOwnershipError, match="another publisher"):
            _publish(candidate, lease=_grant(), current=None)

    def test_success_type_itself_refuses_failed_evidence(self) -> None:
        success = _success(date(2026, 8, 1), publication_id="publication-1")
        subject = success.evidence.subject
        assert subject is not None
        failed = _evidence(subject, PrepublicationRuleFamily.TABLE_DQ)
        with pytest.raises(PublicationStateError, match="cannot describe a success"):
            replace(success, evidence=failed)

    def test_validation_failure_creates_no_success(self) -> None:
        candidate = _candidate()
        subject = candidate.evidence.subject
        assert subject is not None
        evidence = _evidence(subject, PrepublicationRuleFamily.CONTRACT_ASSERTIONS)
        with pytest.raises(PublicationStateError, match="contract_assertions"):
            _publish(
                replace(candidate, evidence=evidence),
                lease=_grant(),
                current=None,
            )

    @pytest.mark.covers("ADR-049")
    @pytest.mark.parametrize("family", list(PrepublicationRuleFamily))
    def test_every_prepublication_family_failure_preserves_last_good_current(
        self,
        family: PrepublicationRuleFamily,
    ) -> None:
        current = _success(date(2026, 8, 25), publication_id="publication-current")
        before = coverage_from((current,))
        candidate = _candidate(_CandidateSpec(publication_id="publication-refused"))
        subject = candidate.evidence.subject
        assert subject is not None
        candidate = replace(candidate, evidence=_evidence(subject, family))

        with pytest.raises(PublicationStateError, match=family.value):
            _publish(candidate, lease=_grant(), current=current)

        assert current.publication_id == PublicationId("publication-current")
        assert coverage_from((current,)) == before

    def test_evidence_for_other_candidate_bytes_cannot_be_constructed(self) -> None:
        candidate = _candidate()
        with pytest.raises(PublicationStateError, match="another candidate subject"):
            replace(candidate, content_checksum="sha256:substituted")

    def test_stale_worker_cannot_publish(self) -> None:
        first = _grant()
        current = first.state
        replacement = acquire_lease(
            current,
            LeaseRequest(
                partition=_partition(),
                holder=LeaseHolderKey("worker-2"),
                requested_at=NOW + timedelta(minutes=6),
                duration=timedelta(minutes=5),
            ),
        )
        stale = _candidate(_CandidateSpec(token=1, published_at=NOW + timedelta(minutes=7)))
        with pytest.raises(PublicationStateError, match="another holder"):
            _publish(stale, lease=replacement, current=None)

    def test_expired_lease_cannot_publish(self) -> None:
        with pytest.raises(PublicationStateError, match="expired"):
            _publish(
                _candidate(_CandidateSpec(published_at=NOW + timedelta(minutes=6))),
                lease=_grant(),
                current=None,
            )

    def test_success_references_validation_and_source_evidence(self) -> None:
        candidate = _candidate()
        success = _publish(
            candidate,
            lease=_grant(),  # type: ignore[arg-type]
            current=None,
        )
        assert success.evidence.rule_set_version == "v2"
        assert success.source_inputs == (SourcePublicationRef("source-publication-1"),)
        assert success.supersedes is None
        assert successful_matches_candidate(candidate, success)

    def test_success_refuses_evidence_bound_to_another_envelope(self) -> None:
        success = _publish(_candidate(), lease=_grant(), current=None)

        with pytest.raises(PublicationStateError, match="another successful subject"):
            replace(success, content_checksum="sha256:substituted")

    def test_restatement_supersedes_current_publication_without_rewriting_it(self) -> None:
        current = _publish(
            _candidate(),
            lease=_grant(),  # type: ignore[arg-type]
            current=None,
        )
        replacement = _publish(
            _candidate(_CandidateSpec(publication_id="publication-2")),
            lease=_grant(),  # type: ignore[arg-type]
            current=current,
        )
        assert replacement.supersedes == current.publication_id
        assert current.supersedes is None


class TestExactCoverage:
    def test_different_option_lenses_cannot_share_one_coverage_conclusion(self) -> None:
        first = _success(date(2026, 8, 1), publication_id="publication-1")
        other_scope = replace(first.partition.scope, lens=PublicationLensKey("another_lens"))
        other_partition = PartitionIdentity(
            scope=other_scope,
            bucket=first.partition.bucket,
        )
        second = _publish(
            _candidate(
                _CandidateSpec(
                    partition=other_partition,
                    publication_id="publication-2",
                )
            ),
            lease=_grant(other_partition),
            current=None,
        )
        with pytest.raises(PublicationStateError, match="different publication scopes"):
            coverage_from((first, second))

    def test_adjacent_successful_days_coalesce(self) -> None:
        coverage = coverage_from(
            (
                _success(date(2026, 8, 1), publication_id="publication-1"),
                _success(date(2026, 8, 2), publication_id="publication-2"),
            )
        )
        assert coverage.intervals == (DateRange(date(2026, 8, 1), date(2026, 8, 2)),)

    def test_gap_remains_a_gap(self) -> None:
        coverage = coverage_from(
            (
                _success(date(2026, 8, 1), publication_id="publication-1"),
                _success(date(2026, 8, 3), publication_id="publication-3"),
            )
        )
        assert coverage.intervals == (
            DateRange(date(2026, 8, 1), date(2026, 8, 1)),
            DateRange(date(2026, 8, 3), date(2026, 8, 3)),
        )
        assert coverage.covers(DateRange(date(2026, 8, 1), date(2026, 8, 3))) is False

    def test_each_side_of_a_gap_is_covered_independently(self) -> None:
        coverage = ExactCoverage(
            (
                DateRange(date(2026, 8, 1), date(2026, 8, 1)),
                DateRange(date(2026, 8, 3), date(2026, 8, 3)),
            )
        )
        assert coverage.covers(DateRange(date(2026, 8, 1), date(2026, 8, 1))) is True
        assert coverage.covers(DateRange(date(2026, 8, 3), date(2026, 8, 3))) is True

    def test_min_max_envelope_is_refused_as_nonexact_coverage(self) -> None:
        with pytest.raises(PublicationStateError, match="sorted, disjoint and coalesced"):
            ExactCoverage(
                (
                    DateRange(date(2026, 8, 3), date(2026, 8, 3)),
                    DateRange(date(2026, 8, 1), date(2026, 8, 1)),
                )
            )
