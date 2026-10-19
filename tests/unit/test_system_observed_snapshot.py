# SPDX-License-Identifier: MIT-0
"""System-first-observed hierarchy snapshots never masquerade as source business time."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.curation.manifests import (
    BuildManifestRequest,
    build_manifest,
    checksum_of,
    declared_columns_of,
)
from adapters.jobs.curation.readers import DeliveredFile, read
from adapters.outbound.source_pack import load_source_packs
from domain.curation.landing import LandedObject
from domain.semantics.revision import RevisionPrecision
from domain.semantics.source_aligned import LandedDeliveryRef
from domain.semantics.source_compiler import CompileSourceRequest, compile_source_publication
from domain.semantics.source_pack import TimeField
from domain.semantics.time import FactTimeClass, ObservationTimeBasis, TimeGrain

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-020", "ADR-044", "ADR-046")]
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/generated"


def _request() -> CompileSourceRequest:
    contract = next(
        contract
        for _, contract in load_contracts(ROOT)
        if contract.contract_id == "vendor-product-hierarchy"
    )
    pack = next(
        item for item in load_source_packs(ROOT) if item.contract_id == contract.contract_id
    )
    text = (FIXTURES / "vendor-product-hierarchy.jsonl").read_text()
    declaration_text = (FIXTURES / "vendor-product-hierarchy.manifest.json").read_text()
    declaration = json.loads(declaration_text)
    manifest = build_manifest(
        BuildManifestRequest(
            declaration=declaration,
            objects=(
                LandedObject("data-0001.jsonl", len(text.encode()), checksum_of(text.encode())),
            ),
            declared_columns=declared_columns_of(text, source_format=contract.source_format.value),
        )
    )
    document = read(DeliveredFile(text, contract.source_format, declaration_text))
    first_observed = datetime(
        2026,
        8,
        20,
        1,
        30,
        tzinfo=timezone(timedelta(hours=2)),
    )
    return CompileSourceRequest(
        pack=pack,
        contract=contract,
        manifest=manifest,
        document=document,
        landed_delivery=LandedDeliveryRef(
            manifest_key=manifest.manifest_key,
            manifest_version="manifest-version-v1",
            manifest_checksum="sha256:manifest",
            object_key=f"{manifest.prefix}{manifest.objects[0].name}",
            object_version="object-version-v1",
            object_checksum=manifest.objects[0].checksum,
            first_observed_at=first_observed,
        ),
        published_at=datetime(2026, 8, 24, tzinfo=UTC),
    )


def test_hierarchy_uses_immutable_technical_observation_not_manifest_business_dates() -> None:
    request = _request()
    publication = compile_source_publication(request)

    assert request.manifest.source_watermark is None
    assert publication.source_revision.scheme.value == "adlytics.raw_manifest_first_observed"
    assert publication.source_revision.precision is RevisionPrecision.INSTANT
    assert publication.source_revision.observed_at == request.landed_delivery.first_observed_at
    assert publication.source_revision.source_id == publication.reference.value
    assert publication.rowset.rows
    for row in publication.rowset.rows:
        assert row.measurement.observation_basis is ObservationTimeBasis.SYSTEM_FIRST_OBSERVED
        assert row.measurement.calendar.value == "technical_utc"
        assert row.measurement.source_timezone == "UTC"
        assert row.measurement.grain is TimeGrain.DAY
        assert row.measurement.window.start == datetime(2026, 8, 19, 23, 30, tzinfo=UTC).date()
        assert row.measurement.window.end == row.measurement.window.start


def test_system_observed_snapshot_requires_immutable_landing_time() -> None:
    request = _request()
    with pytest.raises(ValueError, match="lacks S3 observation time"):
        compile_source_publication(
            replace(
                request,
                landed_delivery=replace(request.landed_delivery, first_observed_at=None),
            )
        )


@pytest.mark.parametrize(
    "time",
    [
        TimeField(
            FactTimeClass.SNAPSHOT,
            "businessDate",
            "businessDate",
            TimeGrain.DAY,
            ObservationTimeBasis.SOURCE_BUSINESS_DATE,
        ),
        TimeField(
            FactTimeClass.DAILY_ADDITIVE,
            "businessDate",
            "businessDate",
            TimeGrain.DAY,
            ObservationTimeBasis.SOURCE_BUSINESS_DATE,
        ),
    ],
)
def test_source_business_date_time_requires_explicit_bounds(time: TimeField) -> None:
    assert time.start_path and time.end_path


def test_invalid_system_observed_shapes_are_refused() -> None:
    with pytest.raises(ValueError, match="SNAPSHOT, DAY"):
        TimeField(
            FactTimeClass.DAILY_ADDITIVE,
            None,
            None,
            TimeGrain.DAY,
            ObservationTimeBasis.SYSTEM_FIRST_OBSERVED,
        )
    with pytest.raises(ValueError, match="source-business-date"):
        TimeField(
            FactTimeClass.SNAPSHOT,
            None,
            None,
            TimeGrain.DAY,
            ObservationTimeBasis.SOURCE_BUSINESS_DATE,
        )
