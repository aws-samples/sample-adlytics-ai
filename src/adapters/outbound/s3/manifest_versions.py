# SPDX-License-Identifier: MIT-0
"""Exact-version reads of the raw manifest zone for the execution explanation (S2).

`LastModified` of the exact manifest version is the root clock's only authority (FR-2), read
with `HeadObject` on that version. The key-only arm lists the key's versions and keeps only
entries whose key equals the key exactly, because a prefix listing also returns longer keys.
Both reads are read-only; any provider failure is a recoverable dependency failure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

from botocore.exceptions import BotoCoreError, ClientError

from domain.shared_kernel.errors import DependencyError, ErrorCode

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

__all__ = ["S3ManifestVersions"]

_READ_FAILED: Final = ErrorCode("EXEC_EXPLAIN", 21)
# A version ID S3 cannot parse names no version, so it reads as absent, not as a failure.
_ABSENT: Final = frozenset(
    {"400", "404", "InvalidArgument", "NoSuchKey", "NoSuchVersion", "NotFound"}
)
# More exact versions than this is already ambiguous; the listing stops reading there.
_MAX_VERSIONS: Final = 16


class S3ManifestVersions:
    """The raw bucket's manifest versions."""

    def __init__(self, bucket: str, client: S3Client) -> None:
        self._bucket = bucket
        self._client = client

    def _failure(self, error: Exception) -> DependencyError:
        return DependencyError(
            _READ_FAILED,
            "the raw manifest version could not be read.",
            context={"cause": type(error).__name__},
        )

    def version_ids(self, key: str) -> tuple[str, ...]:
        """Return the version IDs of exactly this key, up to the ambiguity bound."""
        found: list[str] = []
        try:
            for page in self._client.get_paginator("list_object_versions").paginate(
                Bucket=self._bucket, Prefix=key
            ):
                for item in page.get("Versions", []):
                    version = item.get("VersionId")
                    if item.get("Key") == key and version:
                        found.append(str(version))
                if len(found) > _MAX_VERSIONS:
                    break
        except (BotoCoreError, ClientError) as error:
            raise self._failure(error) from None
        return tuple(found)

    def last_modified(self, key: str, version_id: str) -> datetime | None:
        """Return the exact version's `LastModified` in UTC, or None if it does not exist."""
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=key, VersionId=version_id)
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code in _ABSENT:
                return None
            raise self._failure(error) from None
        except BotoCoreError as error:
            raise self._failure(error) from None
        moment = cast("datetime | None", head.get("LastModified"))
        if moment is None or moment.tzinfo is None:
            raise DependencyError(_READ_FAILED, "the manifest version has no LastModified.")
        return moment.astimezone(UTC)
