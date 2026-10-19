# SPDX-License-Identifier: MIT-0
"""Production-safe exact-version S3 reader scoped to one explicit raw bucket."""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3

from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.versioned_raw import RawVersion, VersionedBody

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_s3.client import S3Client

__all__ = ["S3VersionedRawReader"]

RAW_VERSION_UNAVAILABLE = ErrorCode("RAW_VERSION", 1)
RAW_VERSION_INCONSISTENT = ErrorCode("RAW_VERSION", 2)


class S3VersionedRawReader:
    """Read retained versions without account-wide or bucket-root enumeration."""

    def __init__(self, bucket: str, client: S3Client | None = None) -> None:
        if not bucket.strip():
            raise ValueError("versioned raw reader requires an explicit bucket.")
        self._bucket = bucket
        self._client = client or boto3.client("s3")

    def versions(self, prefix: str) -> tuple[RawVersion, ...]:
        """Paginate ListObjectVersions only under a validated non-empty dataset prefix."""
        if not prefix.strip() or prefix.startswith("/") or prefix == "/" or ".." in prefix:
            raise ValueError("version listing requires a safe non-root source prefix.")
        found: list[RawVersion] = []
        key_marker: str | None = None
        version_marker: str | None = None
        while True:
            try:
                page = (
                    self._client.list_object_versions(Bucket=self._bucket, Prefix=prefix)
                    if key_marker is None
                    else self._client.list_object_versions(
                        Bucket=self._bucket,
                        Prefix=prefix,
                        KeyMarker=key_marker,
                        VersionIdMarker=version_marker or "",
                    )
                )
            except Exception as error:
                raise DependencyError(
                    RAW_VERSION_UNAVAILABLE,
                    "Retained raw versions could not be listed.",
                    context={"cause": type(error).__name__, "prefix": prefix},
                ) from None
            for item in page.get("Versions", []):
                modified_at = item.get("LastModified")
                if modified_at is None:
                    raise InvariantError(
                        RAW_VERSION_INCONSISTENT,
                        "A retained object version has no modification instant.",
                        context={"prefix": prefix},
                    )
                found.append(
                    RawVersion(
                        key=str(item.get("Key", "")),
                        version_id=str(item.get("VersionId", "")),
                        byte_count=int(item.get("Size", 0)),
                        modified_at=modified_at,
                        delete_marker=False,
                    )
                )
            for item in page.get("DeleteMarkers", []):
                modified_at = item.get("LastModified")
                if modified_at is None:
                    raise InvariantError(
                        RAW_VERSION_INCONSISTENT,
                        "A retained delete marker has no modification instant.",
                        context={"prefix": prefix},
                    )
                found.append(
                    RawVersion(
                        key=str(item.get("Key", "")),
                        version_id=str(item.get("VersionId", "")),
                        byte_count=0,
                        modified_at=modified_at,
                        delete_marker=True,
                    )
                )
            if not page.get("IsTruncated", False):
                return tuple(found)
            key_marker = str(page.get("NextKeyMarker", ""))
            version_marker = str(page.get("NextVersionIdMarker", ""))
            if not key_marker or not version_marker:
                raise InvariantError(
                    RAW_VERSION_INCONSISTENT,
                    "A truncated retained-version listing carried no continuation markers.",
                    context={"prefix": prefix},
                )

    def body(self, key: str, version_id: str) -> VersionedBody:
        """Read one explicitly selected S3 VersionId and verify the response binding."""
        if not key.strip() or not version_id.strip():
            raise ValueError("versioned raw read requires key and VersionId.")
        try:
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=key,
                VersionId=version_id,
            )
            returned_version = str(response.get("VersionId", ""))
            body = response["Body"].read()
        except Exception as error:
            raise DependencyError(
                RAW_VERSION_UNAVAILABLE,
                "A retained raw object version could not be read.",
                context={"cause": type(error).__name__, "key": key, "version": version_id},
            ) from None
        if returned_version != version_id:
            raise InvariantError(
                RAW_VERSION_INCONSISTENT,
                "A retained raw read returned another object version.",
                context={"key": key, "requested": version_id, "returned": returned_version},
            )
        return VersionedBody(key=key, version_id=version_id, body=body)
