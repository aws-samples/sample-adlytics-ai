# SPDX-License-Identifier: MIT-0
"""Write one staged table unit to the staging bucket once, write-first (ADR-149 decision 3).

The key names the bytes (``<table>/<sha256>/rows.jsonl``), so a put with ``If-None-Match``
and the SHA-256 checksum either writes exactly those bytes or finds an object already there,
which a checksum-mode HEAD then confirms. The stager never HEADs a missing key and never
lists, so it needs no ``s3:ListBucket``.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from time import monotonic
from typing import TYPE_CHECKING, Final

from aws_lambda_powertools import Logger
from botocore.exceptions import BotoCoreError, ClientError

from adapters.outbound.telemetry.reporting import LogSeverity, OperationLog, report_operation
from domain.shared_kernel.errors import InvariantError
from ports.outbound.unit_staging import (
    STAGING_CONFLICT,
    STAGING_RENDER_DIVERGED,
    StagedUnit,
    StagingUnavailableError,
)

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_s3.client import S3Client

__all__ = ["S3UnitStager", "staging_object_key"]

_LOGGER: Final = Logger(service="adlytics-unit-staging")
_PRECONDITION: Final = frozenset({"PreconditionFailed", "412"})
_DIGEST: Final = frozenset({"BadDigest", "InvalidDigest"})
_CONTENT_TYPE: Final = "application/x-ndjson"


def staging_object_key(staged: StagedUnit) -> str:
    """Return the one object key a staged unit is filed under."""
    return f"{staged.table}/{staged.staging_key}/rows.jsonl"


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


class S3UnitStager:
    """The S3 ``UnitStager``: once-only put, then a checksum HEAD only when the key exists."""

    def __init__(self, bucket: str, client: S3Client) -> None:
        if not bucket.strip():
            raise ValueError("the unit stager requires a staging bucket name.")
        self._bucket = bucket
        self._client = client

    def ensure(self, staged: StagedUnit, render: Callable[[], bytes]) -> None:
        """Write the staged body once, or confirm the identical body is already present."""
        started = monotonic()
        key = staging_object_key(staged)
        checksum = base64.b64encode(bytes.fromhex(staged.staging_key)).decode("ascii")
        body = render()
        outcome = "written" if self._put(key, body, checksum) else "present"
        if outcome == "present":
            self._confirm(staged, key, checksum)
        report_operation(
            OperationLog(
                operation="semantic_unit_staged",
                component="unit_staging",
                phase="unit",
                outcome=outcome,
                severity=LogSeverity.INFO,
                details={
                    "table": staged.table,
                    "rows": staged.rows,
                    "bytes": staged.byte_length,
                    "elapsed_ms": round((monotonic() - started) * 1000),
                },
            ),
            _LOGGER,
        )

    def _put(self, key: str, body: bytes, checksum: str) -> bool:
        """Return ``True`` when this call wrote the object, ``False`` when it already existed."""
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                IfNoneMatch="*",
                ChecksumAlgorithm="SHA256",
                ChecksumSHA256=checksum,
                ContentType=_CONTENT_TYPE,
            )
        except ClientError as error:
            code = _code(error)
            if code in _PRECONDITION:
                return False
            if code in _DIGEST:
                raise InvariantError(
                    STAGING_RENDER_DIVERGED,
                    "the staging store rejected a body that differs from its checksum.",
                    context={"cause": code, "key": key},
                ) from None
            raise StagingUnavailableError(
                "a staged unit could not be written.",
                context={"cause": code or type(error).__name__, "key": key},
            ) from None
        except BotoCoreError as error:
            raise StagingUnavailableError(
                "a staged unit could not be written.",
                context={"cause": type(error).__name__, "key": key},
            ) from None
        return True

    def _confirm(self, staged: StagedUnit, key: str, checksum: str) -> None:
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as error:
            raise StagingUnavailableError(
                "an existing staged unit could not be verified.",
                context={"cause": _code(error) or type(error).__name__, "key": key},
            ) from None
        except BotoCoreError as error:
            raise StagingUnavailableError(
                "an existing staged unit could not be verified.",
                context={"cause": type(error).__name__, "key": key},
            ) from None
        if (
            head.get("ChecksumSHA256") != checksum
            or head.get("ContentLength") != staged.byte_length
        ):
            raise InvariantError(
                STAGING_CONFLICT,
                "an object at a staging key holds different bytes.",
                context={"key": key},
            )
