# SPDX-License-Identifier: MIT-0
"""Read one immutable, version-bound landed delivery for Semantic V2 ingestion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from adapters.jobs.curation.manifests import checksum_of, read_manifest
from domain.curation.landing import LandingError, misplacement
from domain.semantics.source_aligned import LandedDeliveryRef

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from types_boto3_s3.client import S3Client

    from domain.curation.landing import Manifest

__all__ = ["LoadedDelivery", "ManifestInput", "fetch_v2_delivery", "v2_operation_id"]


@dataclass(frozen=True, slots=True)
class ManifestInput:
    """One exact manifest object selected by an immutable event."""

    key: str
    version_id: str

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("manifest input requires a nonblank key.")
        if not self.version_id.strip():
            raise ValueError("manifest input requires a nonblank VersionId.")


@dataclass(frozen=True, slots=True)
class LoadedDelivery:
    """A verified delivery with exact versioned landed evidence."""

    manifest: Manifest
    manifest_key: str
    text: str
    landed_ref: LandedDeliveryRef


def v2_operation_id(delivery: LoadedDelivery) -> str:
    """Derive one cross-entry-point identity from exact immutable landed evidence."""
    landed = delivery.landed_ref
    payload = json.dumps(
        (
            landed.manifest_key,
            landed.manifest_version,
            landed.manifest_checksum,
            landed.object_key,
            landed.object_version,
            landed.object_checksum,
        ),
        separators=(",", ":"),
    )
    return f"v2-runtime-{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


@dataclass(frozen=True, slots=True)
class _BodyRequest:
    client: S3Client
    bucket: str
    key: str
    version_id: str


@dataclass(frozen=True, slots=True)
class _VersionedBody:
    data: bytes
    version_id: str
    last_modified: datetime


def fetch_v2_delivery(
    client: S3Client,
    bucket: str,
    manifest_input: ManifestInput,
) -> LoadedDelivery:
    """Read one exact event- or operator-selected manifest and its exact data object."""
    manifest_key = manifest_input.key
    manifest_body = _body(_BodyRequest(client, bucket, manifest_key, manifest_input.version_id))
    manifest = read_manifest(manifest_body.data.decode())

    wrong_place = misplacement(manifest, manifest_key)
    if wrong_place is not None:
        raise LandingError(wrong_place)

    if len(manifest.objects) != 1:
        raise LandingError(
            f"{manifest_key}: this delivery names {len(manifest.objects)} data objects. Joining "
            "parts needs a per-format rule nobody has decided — a CSV's repeated header must be "
            "dropped, JSONL lines concatenate, and two JSON documents cannot be joined at all — "
            "so a multi-part delivery is refused rather than guessed at."
        )

    landed = manifest.objects[0]
    if landed.version_id is None:
        raise LandingError(f"{manifest_key}: V2 manifest does not bind its data object VersionId.")

    object_key = f"{manifest.prefix}{landed.name}"
    object_body = _body(_BodyRequest(client, bucket, object_key, landed.version_id))
    if object_body.version_id != landed.version_id:
        raise LandingError(
            f"{manifest_key}: manifest binds object VersionId {landed.version_id!r}, but S3 "
            f"returned {object_body.version_id!r}."
        )
    if len(object_body.data) != landed.byte_count:
        raise LandingError(
            f"{landed.name}: the manifest declares {landed.byte_count} bytes and the object is "
            f"{len(object_body.data)}. A short object is a truncated upload, not a delivery with "
            "fewer rows."
        )
    if checksum_of(object_body.data) != landed.checksum:
        raise LandingError(
            f"{landed.name}: the object's digest does not match the manifest's, so the bytes "
            "stored are not the bytes the producer described."
        )

    return LoadedDelivery(
        manifest=manifest,
        manifest_key=manifest_key,
        text=object_body.data.decode(),
        landed_ref=LandedDeliveryRef(
            manifest_key=manifest_key,
            manifest_version=manifest_body.version_id,
            manifest_checksum=checksum_of(manifest_body.data),
            object_key=object_key,
            object_version=object_body.version_id,
            object_checksum=landed.checksum,
            first_observed_at=manifest_body.last_modified,
        ),
    )


def _body(request: _BodyRequest) -> _VersionedBody:
    """Read one selected object and verify S3 returned that exact VersionId."""
    client = request.client
    bucket = request.bucket
    key = request.key
    version_id = request.version_id
    try:
        response = client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
        returned_version = str(response.get("VersionId", ""))
        if not returned_version:
            raise LandingError(
                # A-S3-DIAGNOSTIC-001: error text only; see docs/security-dispositions.md
                # nosemgrep: python.django.security.injection.tainted-url-host
                f"s3://{bucket}/{key} returned no VersionId from the versioned raw bucket."
            )
        if returned_version != version_id:
            raise LandingError(
                # A-S3-DIAGNOSTIC-001: error text only; see docs/security-dispositions.md
                # nosemgrep: python.django.security.injection.tainted-url-host
                f"s3://{bucket}/{key} requested VersionId {version_id!r}, but S3 returned "
                f"{returned_version!r}."
            )
        return _VersionedBody(response["Body"].read(), returned_version, response["LastModified"])
    except LandingError:
        raise
    except Exception as exc:
        raise LandingError(
            # A-S3-DIAGNOSTIC-001: error text only; see docs/security-dispositions.md
            # nosemgrep: python.django.security.injection.tainted-url-host
            f"s3://{bucket}/{key} could not be read ({type(exc).__name__})."
        ) from None
