# SPDX-License-Identifier: MIT-0
"""Real S3 validation for immutable canonical candidates and bound evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import boto3
import pytest

from adapters.outbound.s3.semantic_storage import (
    S3CanonicalAdapter,
    S3ProjectionObjectSource,
)
from domain.semantics.canonical_compiler import CanonicalRow, canonical_rows_checksum
from domain.semantics.identity import CommerceScopeKey
from domain.semantics.publication import (
    AttemptId,
    CanonicalDatasetKey,
    CanonicalPublisherKey,
    LeaseHolderKey,
    PartitionIdentity,
    PublicationCandidate,
    PublicationId,
    PublicationLensKey,
    PublicationScope,
    SourcePublicationRef,
)
from domain.semantics.revision import RevisionSchemeKey, SourceRevision
from domain.semantics.time import BusinessCalendarKey, FactTimeClass, MeasurementBucket, TimeGrain
from domain.semantics.validation import (
    PrepublicationRuleFamily,
    PrepublicationRuleResult,
    PublicationEvidence,
    PublicationEvidenceSubject,
    RuleOutcome,
)
from domain.shared_kernel.values import DateRange
from ports.outbound.canonical import CanonicalDestination, CanonicalMaterializeRequest

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-014", "ADR-044", "ADR-049")]


def _bucket() -> str:
    try:
        parameter = boto3.client("ssm").get_parameter(Name="/adlytics/dev/data/v2/bucket")[
            "Parameter"
        ]
        value = parameter.get("Value")
        if not isinstance(value, str) or not value:
            pytest.skip("deployed Semantic V2 bucket parameter resolved to nothing")
        return value
    except Exception as error:  # noqa: BLE001 — pytest.skip raises: no deployed bucket
        pytest.skip(f"deployed Semantic V2 bucket is unavailable: {type(error).__name__}")


def _request(reference: str) -> CanonicalMaterializeRequest:
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    day = date(2026, 8, 25)
    rows: tuple[CanonicalRow, ...] = (
        (
            ("identity", reference),
            ("marketplace_key", "integration_probe"),
        ),
    )
    checksum = canonical_rows_checksum(rows)
    calendar = BusinessCalendarKey("iso_gregorian", 0)
    partition = PartitionIdentity(
        PublicationScope(
            CanonicalDatasetKey("integration_probe_fact"),
            CommerceScopeKey("integration_probe"),
            "integration-probe",
            PublicationLensKey("integration-probe"),
            FactTimeClass.DAILY_ADDITIVE,
            calendar,
            "UTC",
        ),
        MeasurementBucket(DateRange(day, day), TimeGrain.DAY, calendar, "UTC"),
    )
    revision = SourceRevision.instant(
        scheme=RevisionSchemeKey("integration.probe"),
        source_id=reference,
        observed_at=now,
    )
    source = SourcePublicationRef(f"source-{reference}")
    subject = PublicationEvidenceSubject(
        publisher="canonical-curation",
        partition=partition.identity,
        source_publications=(source.value,),
        source_revision="integration.probe|instant|2026-08-25T12:00:00+00:00|" + reference,
        content_checksum=checksum,
    )
    results = tuple(
        PrepublicationRuleResult(
            family,
            f"integration.{family.value}.v1",
            RuleOutcome.PASSED,
            (
                'Governed identity mappings selected: [{"mapping_version":7}]'
                if family is PrepublicationRuleFamily.IDENTITY
                else "Integration probe passed."
            ),
        )
        for family in PrepublicationRuleFamily
    )
    candidate = PublicationCandidate(
        PublicationId(reference),
        CanonicalPublisherKey("canonical-curation"),
        partition,
        AttemptId(f"attempt-{reference}"),
        LeaseHolderKey(f"holder-{reference}"),
        1,
        (source,),
        revision,
        checksum,
        PublicationEvidence("semantic-v2.1", subject, results),
        now,
    )
    return CanonicalMaterializeRequest(
        CanonicalDestination.CANDIDATE,
        candidate,
        rows,
        "semantic-v2.1",
    )


def test_candidate_create_replay_conflict_and_mapping_evidence_round_trip() -> None:
    test_name = test_candidate_create_replay_conflict_and_mapping_evidence_round_trip.__name__
    node_id = f"{__file__}::{test_name}"
    reference = "integration-probe-" + hashlib.sha256(node_id.encode()).hexdigest()[:16]
    request = _request(reference)
    bucket = _bucket()
    client = boto3.client("s3")
    adapter = S3CanonicalAdapter(bucket, client)
    key = f"immutable/canonical/candidate/integration_probe_fact/{reference}.json"
    try:
        absent = adapter.attest(request)
        created = adapter.materialize(request)
        attested = adapter.attest(request)
        replay = adapter.materialize(request)
        conflicting_request = replace(
            request,
            publication=replace(
                request.publication,
                published_at=request.publication.published_at + timedelta(seconds=1),
            ),
        )
        conflicting_attestation = adapter.attest(conflicting_request)
        conflicting = adapter.materialize(conflicting_request)
        stored_bytes = S3ProjectionObjectSource(bucket, client).read(key)
        stored = json.loads(stored_bytes)

        assert not absent.accepted and not absent.created
        assert created.accepted and created.created
        assert attested.accepted and not attested.created
        assert replay.accepted and not replay.created
        assert not conflicting_attestation.accepted and not conflicting_attestation.created
        assert not conflicting.accepted and not conflicting.created
        identity = next(
            item for item in stored["evidence"]["results"] if item["family"] == "identity"
        )
        assert '"mapping_version":7' in identity["detail"]
        assert stored["evidence"]["subject"]["content_checksum"] == stored["content_checksum"]
    finally:
        client.delete_object(Bucket=bucket, Key=key)
