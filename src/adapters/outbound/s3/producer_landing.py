# SPDX-License-Identifier: MIT-0
"""Version-bound S3 reads and governed writes for source registration finalization."""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.producer_landing import StoredProducerObject, VersionedProducerObject

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_s3.client import S3Client

PRODUCER_LANDING_UNREADABLE = ErrorCode("PRODUCER_LANDING", 1)
PRODUCER_LANDING_UNWRITABLE = ErrorCode("PRODUCER_LANDING", 2)
PRODUCER_LANDING_CONFLICT = ErrorCode("PRODUCER_LANDING", 3)


class S3ProducerLanding:
    """One retained raw bucket accessed only by explicit key and VersionId."""

    def __init__(self, bucket: str, client: S3Client | None = None) -> None:
        if not bucket.strip():
            raise ValueError("producer landing bucket cannot be blank.")
        self._bucket = bucket
        self._client = client or boto3.client("s3")

    def size(self, key: str, version_id: str) -> int:
        """Read exact-version size before allocating memory for the body."""
        if not key.strip() or not version_id.strip():
            raise ValueError("producer landing size check requires key and VersionId.")
        try:
            response = self._client.head_object(
                Bucket=self._bucket,
                Key=key,
                VersionId=version_id,
            )
            returned = str(response.get("VersionId", ""))
            if returned != version_id:
                raise InvariantError(
                    PRODUCER_LANDING_CONFLICT,
                    "S3 returned a different version than the registered source evidence.",
                    context={"key": key},
                )
            size = int(response["ContentLength"])
            if size < 0:
                raise InvariantError(
                    PRODUCER_LANDING_CONFLICT,
                    "S3 returned an invalid producer object size.",
                    context={"key": key, "size": size},
                )
            return size
        except InvariantError:
            raise
        except Exception as error:
            raise DependencyError(
                PRODUCER_LANDING_UNREADABLE,
                "Registered source evidence size could not be read.",
                context={"cause": type(error).__name__, "key": key},
            ) from None

    def body(self, key: str, version_id: str) -> VersionedProducerObject:
        """Read exact bytes and verify S3 returned the requested VersionId."""
        if not key.strip() or not version_id.strip():
            raise ValueError("producer landing read requires key and VersionId.")
        try:
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=key,
                VersionId=version_id,
            )
            returned = str(response.get("VersionId", ""))
            if returned != version_id:
                raise InvariantError(
                    PRODUCER_LANDING_CONFLICT,
                    "S3 returned a different version than the registered source evidence.",
                    context={"key": key},
                )
            return VersionedProducerObject(
                key=key,
                version_id=returned,
                body=response["Body"].read(),
            )
        except InvariantError:
            raise
        except ValueError:
            raise
        except Exception as error:
            raise DependencyError(
                PRODUCER_LANDING_UNREADABLE,
                "Registered source evidence could not be read.",
                context={"cause": type(error).__name__, "key": key},
            ) from None

    def put(self, key: str, body: bytes, content_type: str) -> StoredProducerObject:
        """Create once, or converge only when the existing immutable bytes are identical."""
        if not key.strip() or not body or not content_type.strip():
            raise ValueError(
                "producer landing write requires key, nonempty bytes, and content type."
            )
        try:
            response = self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                IfNoneMatch="*",
            )
            version_id = str(response.get("VersionId", ""))
            if not version_id:
                raise ValueError("versioned raw bucket returned no VersionId for producer write.")
            return StoredProducerObject(key=key, version_id=version_id)
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code not in {"PreconditionFailed", "412"}:
                raise DependencyError(
                    PRODUCER_LANDING_UNWRITABLE,
                    "Governed source evidence could not be written.",
                    context={"cause": code or type(error).__name__, "key": key},
                ) from None
        except ValueError:
            raise
        except Exception as error:
            raise DependencyError(
                PRODUCER_LANDING_UNWRITABLE,
                "Governed source evidence could not be written.",
                context={"cause": type(error).__name__, "key": key},
            ) from None

        try:
            existing = self._client.get_object(Bucket=self._bucket, Key=key)
            existing_body = existing["Body"].read()
            version_id = str(existing.get("VersionId", ""))
        except Exception as error:
            raise DependencyError(
                PRODUCER_LANDING_UNREADABLE,
                "Existing governed source evidence could not be verified.",
                context={"cause": type(error).__name__, "key": key},
            ) from None
        if existing_body != body or not version_id:
            raise InvariantError(
                PRODUCER_LANDING_CONFLICT,
                "An immutable governed source key already contains different evidence.",
                context={"key": key},
            )
        return StoredProducerObject(key=key, version_id=version_id)
