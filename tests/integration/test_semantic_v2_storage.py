# SPDX-License-Identifier: MIT-0
"""Real S3 validation for immutable Semantic V2 source publication storage."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import boto3
import pytest
from probe_identity import PROBE_PREFIX

from adapters.outbound.s3.semantic_storage import S3SourceAlignedAdapter
from domain.semantics.identity import (
    DeliveryScope,
    ExtractionProfileKey,
    SourceDatasetKey,
    SourceIdentifier,
    SourceSystemKey,
)
from domain.semantics.publication import SourcePublicationRef
from domain.semantics.revision import RevisionSchemeKey, SourceRevision
from domain.semantics.source_aligned import (
    LandedDeliveryRef,
    SourceAlignedField,
    SourceAlignedPublication,
    SourceAlignedRow,
    SourceAlignedRowset,
    SourceInterpreterKey,
    SourceOption,
)
from domain.semantics.time import (
    BusinessCalendarKey,
    ExtractionCoverage,
    MeasurementBucket,
    TimeGrain,
)
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-014", "ADR-044")]


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


def _publication(reference: str) -> SourceAlignedPublication:
    day = date(2026, 8, 25)
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    calendar = BusinessCalendarKey("iso_gregorian", week_starts_on=0)
    return SourceAlignedPublication(
        reference=SourcePublicationRef(reference),
        landed_delivery=LandedDeliveryRef(
            f"{PROBE_PREFIX}/manifest.json",
            "manifest-version-probe",
            "sha256:manifest-probe",
            f"{PROBE_PREFIX}/rows.csv",
            "object-version-probe",
            "sha256:object-probe",
        ),
        rowset=SourceAlignedRowset(
            delivery_scope=DeliveryScope(
                SourceSystemKey("integration_probe"),
                SourceDatasetKey("semantic_storage"),
                ExtractionProfileKey("integration_probe", 1),
                SourceIdentifier("probe.scope", PROBE_PREFIX),
                "probe-options",
            ),
            extraction_coverage=ExtractionCoverage(DateRange(day, day)),
            interpreter=SourceInterpreterKey("integration.probe", 1),
            source_options=(SourceOption("probe", "true"),),
            rows=(
                SourceAlignedRow(
                    fields=(SourceAlignedField("amount", Decimal("1.2500")),),
                    source_identifiers=(SourceIdentifier("probe.id", PROBE_PREFIX),),
                    measurement=MeasurementBucket(
                        DateRange(day, day),
                        TimeGrain.DAY,
                        calendar,
                        "UTC",
                    ),
                ),
            ),
        ),
        source_revision=SourceRevision.instant(
            scheme=RevisionSchemeKey("integration.probe"),
            source_id=reference,
            observed_at=now,
        ),
        published_at=now,
    )


def test_exact_replay_is_accepted_and_conflicting_reference_is_refused() -> None:
    node_id = (
        f"{__file__}::{test_exact_replay_is_accepted_and_conflicting_reference_is_refused.__name__}"
    )
    digest = hashlib.sha256(node_id.encode()).hexdigest()[:24]
    reference = f"probe-{digest}"
    publication = _publication(reference)
    bucket = _bucket()
    client = boto3.client("s3")
    adapter = S3SourceAlignedAdapter(bucket, client)
    key = f"immutable/source-aligned/{reference[:2]}/{reference}.json"
    try:
        created = adapter.materialize(publication)
        replay = adapter.materialize(publication)
        conflicting = adapter.materialize(
            replace(publication, published_at=publication.published_at + timedelta(seconds=1))
        )

        assert created.accepted and created.created
        assert replay.accepted and not replay.created
        assert not conflicting.accepted and not conflicting.created
        assert adapter.publication(publication.reference) == publication
    finally:
        client.delete_object(Bucket=bucket, Key=key)
