# SPDX-License-Identifier: MIT-0
"""Read the raw manifest version a run request names (S1; V-7).

Three reads, all against the one raw bucket: the exact version's body, the key's version
listing filtered to entries whose key equals the key exactly (a prefix listing also returns
longer keys), and the window's retrieval count, bounded by the generated limit. Any failure is a
recoverable dependency failure, so admission writes no receipt (V-7d).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from botocore.exceptions import BotoCoreError, ClientError

from adapters.jobs.curation.manifests import read_manifest
from domain.curation.landing import LandingError
from domain.operations.generated_execution_graphs import BOUNDS
from domain.operations.ingestion_root import RootRefusedError, exact_version_count, window_identity
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.execution_graph import ManifestRead

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

__all__ = ["TIMESTAMP_INVALID", "S3RunRequestManifests"]

_READ_FAILED: Final = ErrorCode("RUN_REQUEST", 14)
_MAX_MANIFEST_BYTES: Final = 262_144
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
TIMESTAMP_INVALID: Final = "manifest_authority_timestamp_invalid"


def _root_clock(modified: object) -> datetime:
    """S5-V-5: refuse a missing, naive or pre-epoch `LastModified` as a dependency failure.

    The root clock is required on the admission path (FR-2), so admission never stamps an
    obligation without one. The refusal matches the S2 manifest-version reader's.
    """
    if not isinstance(modified, datetime) or modified.tzinfo is None or modified < _EPOCH:
        raise DependencyError(
            _READ_FAILED,
            "the raw manifest version carries no valid LastModified.",
            context={"reason": TIMESTAMP_INVALID},
        )
    return modified.astimezone(UTC)


class S3RunRequestManifests:
    """The manifest reader admission uses."""

    def __init__(self, bucket: str, client: S3Client) -> None:
        self._bucket = bucket
        self._client = client

    def read(self, key: str, version_id: str) -> ManifestRead:
        """Read one exact manifest version and the listings admission decides over."""
        identity = window_identity(key)
        try:
            listed: list[str] = []
            for page in self._client.get_paginator("list_object_versions").paginate(
                Bucket=self._bucket, Prefix=key
            ):
                listed.extend(str(item.get("Key", "")) for item in page.get("Versions", []))
            body = self._client.get_object(Bucket=self._bucket, Key=key, VersionId=version_id)
            text = body["Body"].read(_MAX_MANIFEST_BYTES + 1)
            # S5: the same exact-version read carries the root clock (FR-2), so admission stamps
            # every obligation it writes with it and no extra request is made.
            modified = body.get("LastModified")
            retrievals = 0
            bound = int(BOUNDS["max_window_retrievals"])
            for page in self._client.get_paginator("list_objects_v2").paginate(
                Bucket=self._bucket, Prefix=f"{identity.window}retrieved=", Delimiter="/"
            ):
                retrievals += len(page.get("CommonPrefixes", []))
                if retrievals > bound:
                    break
        except (BotoCoreError, ClientError) as error:
            raise DependencyError(
                _READ_FAILED,
                "the raw manifest version could not be read.",
                context={"cause": type(error).__name__},
            ) from None
        if len(text) > _MAX_MANIFEST_BYTES:
            raise RootRefusedError("SCHEMA_INVALID", "the manifest body exceeds the read bound.")
        try:
            manifest = read_manifest(text.decode("utf-8"))
        except LandingError, UnicodeDecodeError:
            raise RootRefusedError(
                "SCHEMA_INVALID", "the manifest body is not a valid manifest."
            ) from None
        return ManifestRead(
            retrieved_at=manifest.retrieved_at,
            source_system=manifest.source_system,
            source_dataset=manifest.source_dataset,
            period_end=manifest.period_end.isoformat(),
            exact_versions=exact_version_count(key, listed),
            window_retrievals=retrievals,
            last_modified=_root_clock(modified),
        )
