# SPDX-License-Identifier: MIT-0
"""S3 storage for immutable, capability-free preset artifact bodies."""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from domain.presentation.preset_artifacts import (
    ArtifactBodyWriteResult,
    CustodyManifestWriteResult,
    PresetArtifactBody,
    PresetArtifactCustodyManifest,
    PresetArtifactPointer,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingCustodyWriteResult,
    PresetArtifactSiblingReadRequest,
    PresetArtifactSiblingWriteResult,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.preset_artifacts import (
    PresetArtifactBodyStorePort,
    PresetArtifactCustodyStorePort,
)

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_s3.client import S3Client

__all__ = ["S3PresetArtifactBodyStore", "S3PresetArtifactCustodyStore"]

PRESET_ARTIFACT_BODY_UNREADABLE = ErrorCode("PRESET_ARTIFACT_BODY", 1)
PRESET_ARTIFACT_BODY_UNWRITABLE = ErrorCode("PRESET_ARTIFACT_BODY", 2)
PRESET_ARTIFACT_BODY_CONFLICT = ErrorCode("PRESET_ARTIFACT_BODY", 3)


class S3PresetArtifactBodyStore(PresetArtifactBodyStorePort):
    """Create-once revision bodies and attest their bytes before returning them."""

    def __init__(self, bucket: str, client: S3Client | None = None) -> None:
        if not bucket.strip():
            raise ValueError("preset artifact bucket cannot be blank.")
        self._bucket = bucket
        self._client = client or boto3.client("s3")

    def write(self, body: PresetArtifactBody) -> ArtifactBodyWriteResult:
        """Create the revision key once, accepting only exact immutable replay bytes."""
        encoded = body.payload_json.encode()
        try:
            response = self._client.put_object(
                Bucket=self._bucket,
                Key=body.key,
                Body=encoded,
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code not in {"PreconditionFailed", "412"}:
                raise DependencyError(
                    PRESET_ARTIFACT_BODY_UNWRITABLE,
                    "Preset artifact body could not be written.",
                    context={"cause": code or type(error).__name__},
                ) from None
            existing = self._read_bytes(body.key)
            if existing != encoded:
                raise InvariantError(
                    PRESET_ARTIFACT_BODY_CONFLICT,
                    "An immutable preset artifact key already contains different bytes.",
                    context={"key": body.key},
                ) from None
            return self._result(body, None)
        except Exception as error:
            raise DependencyError(
                PRESET_ARTIFACT_BODY_UNWRITABLE,
                "Preset artifact body could not be written.",
                context={"cause": type(error).__name__},
            ) from None
        return self._result(body, response.get("VersionId") or None)

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactBody:
        """Read a pointer-named body and reject a substituted or malformed payload."""
        try:
            payload_json = self._read_bytes(pointer.body_key).decode()
            body = PresetArtifactBody(
                pointer.page,
                pointer.variant,
                pointer.revision,
                payload_json,
            )
        except InvariantError:
            raise
        except (UnicodeDecodeError, ValueError) as error:
            raise InvariantError(
                PRESET_ARTIFACT_BODY_CONFLICT,
                "Persisted preset artifact bytes are malformed or do not match their pointer.",
                context={"cause": type(error).__name__, "key": pointer.body_key},
            ) from None
        if body.digest != pointer.body_digest:
            raise InvariantError(
                PRESET_ARTIFACT_BODY_CONFLICT,
                "Persisted preset artifact bytes do not match their pointer checksum.",
                context={"key": pointer.body_key},
            )
        return body

    def write_sibling(self, body: PresetArtifactSiblingBody) -> PresetArtifactSiblingWriteResult:
        """Create one immutable ordered sibling, accepting only exact replay bytes."""
        encoded = body.payload_json.encode()
        version_id: str | None = None
        try:
            response = self._client.put_object(
                Bucket=self._bucket,
                Key=body.key,
                Body=encoded,
                ContentType="application/json",
                IfNoneMatch="*",
            )
            version_id = response.get("VersionId")
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code not in {"PreconditionFailed", "412"}:
                raise DependencyError(
                    PRESET_ARTIFACT_BODY_UNWRITABLE,
                    "Preset artifact sibling body could not be written.",
                    context={"cause": code or type(error).__name__},
                ) from None
            if self._read_bytes(body.key) != encoded:
                raise InvariantError(
                    PRESET_ARTIFACT_BODY_CONFLICT,
                    "An immutable preset artifact sibling key contains different bytes.",
                    context={"key": body.key},
                ) from None
        return PresetArtifactSiblingWriteResult(
            body.page,
            body.variant,
            body.revision,
            body.identity,
            body.key,
            body.digest,
            version_id,
        )

    def read_sibling(self, request: PresetArtifactSiblingReadRequest) -> PresetArtifactSiblingBody:
        """Read and attest exactly one public sibling selected by the parent index."""
        key = (
            f"pages/{request.page.value}/{request.variant.value}/{request.revision.value}/"
            f"{request.descriptor.identity.path}.json"
        )
        try:
            candidate = PresetArtifactSiblingBody(
                request.page,
                request.variant,
                request.revision,
                request.descriptor.identity,
                self._read_bytes(key).decode(),
            )
        except UnicodeDecodeError, ValueError:
            raise InvariantError(
                PRESET_ARTIFACT_BODY_CONFLICT,
                "Persisted sibling body is malformed.",
                context={"key": key},
            ) from None
        if candidate.digest != request.descriptor.body_digest:
            raise InvariantError(
                PRESET_ARTIFACT_BODY_CONFLICT,
                "Persisted sibling body does not match the parent custody checksum.",
                context={"key": key},
            )
        return candidate

    def _read_bytes(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return response["Body"].read()
        except Exception as error:
            raise DependencyError(
                PRESET_ARTIFACT_BODY_UNREADABLE,
                "Preset artifact body could not be read.",
                context={"cause": type(error).__name__},
            ) from None

    @staticmethod
    def _result(body: PresetArtifactBody, version_id: str | None) -> ArtifactBodyWriteResult:
        return ArtifactBodyWriteResult(
            body.page,
            body.variant,
            body.revision,
            body.key,
            body.digest,
            version_id,
        )


class S3PresetArtifactCustodyStore(PresetArtifactCustodyStorePort):
    """Create-once private custody manifests and attest their bytes before returning them."""

    def __init__(self, bucket: str, client: S3Client | None = None) -> None:
        if not bucket.strip():
            raise ValueError("preset artifact custody bucket cannot be blank.")
        self._bucket = bucket
        self._client = client or boto3.client("s3")

    def write(self, manifest: PresetArtifactCustodyManifest) -> CustodyManifestWriteResult:
        """Create the custody key once, accepting only exact immutable replay bytes."""
        encoded = manifest.payload_json.encode()
        try:
            response = self._client.put_object(
                Bucket=self._bucket,
                Key=manifest.key,
                Body=encoded,
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code not in {"PreconditionFailed", "412"}:
                raise DependencyError(
                    PRESET_ARTIFACT_BODY_UNWRITABLE,
                    "Preset artifact custody manifest could not be written.",
                    context={"cause": code or type(error).__name__},
                ) from None
            existing = self._read_bytes(manifest.key)
            if existing != encoded:
                raise InvariantError(
                    PRESET_ARTIFACT_BODY_CONFLICT,
                    "An immutable custody manifest key already contains different bytes.",
                    context={"key": manifest.key},
                ) from None
            return self._result(manifest, None)
        except Exception as error:
            raise DependencyError(
                PRESET_ARTIFACT_BODY_UNWRITABLE,
                "Preset artifact custody manifest could not be written.",
                context={"cause": type(error).__name__},
            ) from None
        return self._result(manifest, response.get("VersionId") or None)

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactCustodyManifest:
        """Read a pointer-named custody manifest and reject substituted or malformed bytes."""
        if pointer.custody_digest is None:
            raise InvariantError(
                PRESET_ARTIFACT_BODY_CONFLICT,
                "A custody-free pointer cannot name a custody manifest.",
                context={"key": pointer.custody_key},
            )
        try:
            payload_json = self._read_bytes(pointer.custody_key).decode()
            manifest = PresetArtifactCustodyManifest(
                pointer.page,
                pointer.variant,
                pointer.revision,
                payload_json,
            )
        except InvariantError:
            raise
        except (UnicodeDecodeError, ValueError) as error:
            raise InvariantError(
                PRESET_ARTIFACT_BODY_CONFLICT,
                "Persisted custody manifest bytes are malformed or do not match their pointer.",
                context={"cause": type(error).__name__, "key": pointer.custody_key},
            ) from None
        if manifest.digest != pointer.custody_digest:
            raise InvariantError(
                PRESET_ARTIFACT_BODY_CONFLICT,
                "Persisted custody manifest bytes do not match their pointer checksum.",
                context={"key": pointer.custody_key},
            )
        return manifest

    def write_sibling(
        self, manifest: PresetArtifactSiblingCustody
    ) -> PresetArtifactSiblingCustodyWriteResult:
        """Create one immutable sibling custody body, accepting only exact replay bytes."""
        encoded = manifest.payload_json.encode()
        version_id: str | None = None
        try:
            response = self._client.put_object(
                Bucket=self._bucket,
                Key=manifest.key,
                Body=encoded,
                ContentType="application/json",
                IfNoneMatch="*",
            )
            version_id = response.get("VersionId")
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code not in {"PreconditionFailed", "412"}:
                raise DependencyError(
                    PRESET_ARTIFACT_BODY_UNWRITABLE,
                    "Preset artifact sibling custody could not be written.",
                    context={"cause": code or type(error).__name__},
                ) from None
            if self._read_bytes(manifest.key) != encoded:
                raise InvariantError(
                    PRESET_ARTIFACT_BODY_CONFLICT,
                    "An immutable sibling custody key contains different bytes.",
                    context={"key": manifest.key},
                ) from None
        return PresetArtifactSiblingCustodyWriteResult(
            manifest.page,
            manifest.variant,
            manifest.revision,
            manifest.identity,
            manifest.key,
            manifest.digest,
            version_id,
        )

    def read_sibling(
        self, request: PresetArtifactSiblingReadRequest
    ) -> PresetArtifactSiblingCustody:
        """Read and attest exactly one custody sibling selected by the parent index."""
        key = (
            f"custody/{request.page.value}/{request.variant.value}/{request.revision.value}/"
            f"{request.descriptor.identity.path}.json"
        )
        try:
            candidate = PresetArtifactSiblingCustody(
                request.page,
                request.variant,
                request.revision,
                request.descriptor.identity,
                self._read_bytes(key).decode(),
            )
        except UnicodeDecodeError, ValueError:
            raise InvariantError(
                PRESET_ARTIFACT_BODY_CONFLICT,
                "Persisted sibling custody is malformed.",
                context={"key": key},
            ) from None
        if candidate.digest != request.descriptor.custody_digest:
            raise InvariantError(
                PRESET_ARTIFACT_BODY_CONFLICT,
                "Persisted sibling custody does not match the parent custody checksum.",
                context={"key": key},
            )
        return candidate

    def _read_bytes(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return response["Body"].read()
        except Exception as error:
            raise DependencyError(
                PRESET_ARTIFACT_BODY_UNREADABLE,
                "Preset artifact custody manifest could not be read.",
                context={"cause": type(error).__name__},
            ) from None

    @staticmethod
    def _result(
        manifest: PresetArtifactCustodyManifest, version_id: str | None
    ) -> CustodyManifestWriteResult:
        return CustodyManifestWriteResult(
            manifest.page,
            manifest.variant,
            manifest.revision,
            manifest.key,
            manifest.digest,
            version_id,
        )
