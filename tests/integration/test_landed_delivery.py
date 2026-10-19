# SPDX-License-Identifier: MIT-0
"""Immutable Semantic V2 landing integrity against a real versioned S3 bucket."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Protocol

import boto3
import pytest

from adapters.jobs.curation.manifests import checksum_of, declared_columns_of, write_manifest
from adapters.jobs.ingest_delivery import __main__ as ingest_cli
from adapters.jobs.ingest_delivery.landing import ManifestInput, fetch_v2_delivery
from domain.curation.landing import LandedObject, LandingError, Manifest
from domain.curation.mapping import options_hash
from domain.semantics.identity import SourceIdentifier

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_s3.type_defs import ObjectIdentifierTypeDef

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-030")]

MARKETPLACE = "ATVPDKIKX0DER"
WINDOW = date(2026, 9, 20)
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
MISPLACED_PREFIX = "vendor_central/GET_VENDOR_SALES_REPORT/moved-by-a-test/"
OPTIONS: dict[str, object] = {
    "reportPeriod": "DAY",
    "distributorView": "MANUFACTURING",
    "sellingProgram": "RETAIL",
}


@dataclass(frozen=True, slots=True)
class _Probe:
    manifest: Manifest
    body: bytes


@dataclass(frozen=True, slots=True)
class _Landed:
    manifest: Manifest
    manifest_version: str


class _RequestNode(Protocol):
    nodeid: str


class _FixtureRequest(Protocol):
    node: _RequestNode


def _document() -> bytes:
    payload = {
        "reportSpecification": {
            "reportType": "GET_VENDOR_SALES_REPORT",
            "marketplaceIds": [MARKETPLACE],
            "dataStartTime": WINDOW.isoformat(),
            "dataEndTime": WINDOW.isoformat(),
            "lastUpdatedDate": "2026-09-22T00:00:00Z",
            "reportOptions": OPTIONS,
        },
        "salesByAsin": [{"asin": "B0LANDPROBE", "startDate": WINDOW.isoformat()}],
    }
    return (json.dumps(payload, indent=2) + "\n").encode()


def _probe(*, retrieved: datetime) -> _Probe:
    body = _document()
    manifest = Manifest(
        source_system="vendor_central",
        source_dataset="GET_VENDOR_SALES_REPORT",
        source_scope=SourceIdentifier(
            "vendor_central.vendor_group", "fictional-integration-vendor-group"
        ),
        option_set=OPTIONS,
        option_set_hash=options_hash(OPTIONS),
        marketplace=MARKETPLACE,
        period_start=WINDOW,
        period_end=WINDOW,
        source_watermark=date(2026, 9, 22),
        retrieved_at=retrieved,
        declared_columns=declared_columns_of(body.decode(), source_format="json_dual_array"),
        objects=(
            LandedObject(
                name="data-0001.json",
                byte_count=len(body),
                checksum=checksum_of(body),
            ),
        ),
    )
    return _Probe(manifest, body)


@pytest.fixture(scope="module")
def raw_bucket(request: _FixtureRequest) -> Iterator[str]:
    try:
        account = str(boto3.client("sts").get_caller_identity()["Account"])
    except Exception as exc:  # noqa: BLE001 — pytest.skip raises  # pragma: no cover
        pytest.skip(f"integration tier needs AWS credentials: {type(exc).__name__}")
    digest = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:12]
    bucket = f"adlytics-dev-integration-landing-{account}-{digest}"
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket=bucket)
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    try:
        yield bucket
    finally:
        versions = s3.list_object_versions(Bucket=bucket)
        objects: list[ObjectIdentifierTypeDef] = [
            {"Key": str(item.get("Key", "")), "VersionId": str(item.get("VersionId", ""))}
            for group in (versions.get("Versions", []), versions.get("DeleteMarkers", []))
            for item in group
            if item.get("Key") and item.get("VersionId")
        ]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        s3.delete_bucket(Bucket=bucket)


def _land(bucket: str, probe: _Probe, *, body: bytes | None = None) -> _Landed:
    s3 = boto3.client("s3")
    landed = probe.manifest.objects[0]
    response = s3.put_object(
        Bucket=bucket,
        Key=f"{probe.manifest.prefix}{landed.name}",
        Body=probe.body if body is None else body,
        ContentType="application/json",
    )
    version = str(response.get("VersionId", ""))
    assert version
    manifest = replace(
        probe.manifest,
        objects=(replace(landed, version_id=version),),
    )
    manifest_response = s3.put_object(
        Bucket=bucket,
        Key=manifest.manifest_key,
        Body=write_manifest(manifest).encode(),
        ContentType="application/json",
    )
    manifest_version = str(manifest_response.get("VersionId", ""))
    assert manifest_version
    return _Landed(manifest, manifest_version)


def test_a_version_bound_delivery_round_trips_with_its_digest_intact(raw_bucket: str) -> None:
    probe = _probe(retrieved=datetime(2026, 9, 22, 6, 0, tzinfo=UTC))
    landed = _land(raw_bucket, probe)
    manifest = landed.manifest

    fetched = fetch_v2_delivery(
        boto3.client("s3"),
        raw_bucket,
        ManifestInput(manifest.manifest_key, landed.manifest_version),
    )

    assert fetched.manifest == manifest
    assert fetched.text.encode() == probe.body
    assert fetched.landed_ref.object_version == manifest.objects[0].version_id


def test_a_manifest_moved_to_another_prefix_is_refused(raw_bucket: str) -> None:
    probe = _probe(retrieved=datetime(2026, 9, 22, 7, 0, tzinfo=UTC))
    landed = _land(raw_bucket, probe)
    manifest = landed.manifest
    elsewhere = f"{MISPLACED_PREFIX}manifest.json"
    misplaced = boto3.client("s3").put_object(
        Bucket=raw_bucket,
        Key=elsewhere,
        Body=write_manifest(manifest).encode(),
        ContentType="application/json",
    )
    misplaced_version = str(misplaced.get("VersionId", ""))
    assert misplaced_version

    with pytest.raises(LandingError, match="disagree"):
        fetch_v2_delivery(
            boto3.client("s3"),
            raw_bucket,
            ManifestInput(elsewhere, misplaced_version),
        )


def test_a_truncated_object_is_refused_before_parsing(raw_bucket: str) -> None:
    probe = _probe(retrieved=datetime(2026, 9, 22, 8, 0, tzinfo=UTC))
    landed = _land(raw_bucket, probe, body=probe.body[: len(probe.body) // 2])
    manifest = landed.manifest

    with pytest.raises(LandingError, match="truncated upload"):
        fetch_v2_delivery(
            boto3.client("s3"),
            raw_bucket,
            ManifestInput(manifest.manifest_key, landed.manifest_version),
        )


def test_a_later_object_version_does_not_substitute_declared_evidence(raw_bucket: str) -> None:
    probe = _probe(retrieved=datetime(2026, 9, 22, 9, 0, tzinfo=UTC))
    landed = _land(raw_bucket, probe)
    manifest = landed.manifest
    declared_version = manifest.objects[0].version_id
    object_key = f"{manifest.prefix}{manifest.objects[0].name}"
    later = boto3.client("s3").put_object(
        Bucket=raw_bucket,
        Key=object_key,
        Body=b"different current bytes",
        ContentType="application/json",
    )
    assert str(later.get("VersionId", "")) != declared_version

    fetched = fetch_v2_delivery(
        boto3.client("s3"),
        raw_bucket,
        ManifestInput(manifest.manifest_key, landed.manifest_version),
    )

    assert fetched.text.encode() == probe.body
    assert fetched.landed_ref.object_version == declared_version


def test_an_event_selected_manifest_version_is_not_replaced_by_current(raw_bucket: str) -> None:
    probe = _probe(retrieved=datetime(2026, 9, 22, 10, 0, tzinfo=UTC))
    landed = _land(raw_bucket, probe)
    replacement = replace(
        landed.manifest,
        source_watermark=date(2026, 9, 23),
    )
    later = boto3.client("s3").put_object(
        Bucket=raw_bucket,
        Key=landed.manifest.manifest_key,
        Body=write_manifest(replacement).encode(),
        ContentType="application/json",
    )
    assert str(later.get("VersionId", "")) != landed.manifest_version

    fetched = fetch_v2_delivery(
        boto3.client("s3"),
        raw_bucket,
        ManifestInput(landed.manifest.manifest_key, landed.manifest_version),
    )

    assert fetched.manifest == landed.manifest
    assert fetched.landed_ref.manifest_version == landed.manifest_version


def test_manual_discovery_freezes_the_current_manifest_version(raw_bucket: str) -> None:
    probe = _probe(retrieved=datetime(2026, 9, 22, 11, 0, tzinfo=UTC))
    landed = _land(raw_bucket, probe)
    selected_manifest = replace(
        landed.manifest,
        source_watermark=date(2026, 9, 23),
    )
    selected_response = boto3.client("s3").put_object(
        Bucket=raw_bucket,
        Key=landed.manifest.manifest_key,
        Body=write_manifest(selected_manifest).encode(),
        ContentType="application/json",
    )
    selected_version = str(selected_response.get("VersionId", ""))
    assert selected_version

    discovered = ingest_cli._manifest_inputs(  # pyright: ignore[reportPrivateUsage]
        boto3.client("s3"),
        raw_bucket,
    )
    selected = next(item for item in discovered if item.key == landed.manifest.manifest_key)
    assert selected == ManifestInput(landed.manifest.manifest_key, selected_version)

    later_manifest = replace(selected_manifest, source_watermark=date(2026, 9, 24))
    later_response = boto3.client("s3").put_object(
        Bucket=raw_bucket,
        Key=landed.manifest.manifest_key,
        Body=write_manifest(later_manifest).encode(),
        ContentType="application/json",
    )
    assert str(later_response.get("VersionId", "")) != selected.version_id

    fetched = fetch_v2_delivery(boto3.client("s3"), raw_bucket, selected)

    assert fetched.manifest == selected_manifest
    assert fetched.landed_ref.manifest_version == selected_version
