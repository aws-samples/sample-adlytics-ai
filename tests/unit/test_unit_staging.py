# SPDX-License-Identifier: MIT-0
"""ADR-149 S3 unit stager: once-only write, checksum-confirmed presence, and typed failures."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from typing import TYPE_CHECKING

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber

from adapters.outbound.s3.unit_staging import S3UnitStager, staging_object_key
from domain.shared_kernel.errors import DependencyError, InvariantError
from ports.outbound.unit_staging import (
    STAGING_CONFLICT,
    STAGING_RENDER_DIVERGED,
    STAGING_UNAVAILABLE,
    StagedUnit,
    StagingUnavailableError,
)

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-149")]

# A fictional bucket and body; nothing here reaches AWS.
BUCKET = "fixture-staging-bucket"
BODY = b'{"publication_id":"pub-a","stg_ordinal":"0"}\n'
STAGED = StagedUnit(
    table="fact_fixture_rows",
    staging_key=hashlib.sha256(BODY).hexdigest(),
    byte_length=len(BODY),
    row_counts=(("pub-a", 1),),
)
KEY = f"fact_fixture_rows/{STAGED.staging_key}/rows.jsonl"
CHECKSUM = base64.b64encode(hashlib.sha256(BODY).digest()).decode("ascii")
PUT = {
    "Bucket": BUCKET,
    "Key": KEY,
    "Body": BODY,
    "IfNoneMatch": "*",
    "ChecksumAlgorithm": "SHA256",
    "ChecksumSHA256": CHECKSUM,
    "ContentType": "application/x-ndjson",
}
HEAD = {"Bucket": BUCKET, "Key": KEY, "ChecksumMode": "ENABLED"}


class _Render:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> bytes:
        self.calls += 1
        return BODY


@pytest.fixture
def s3() -> Iterator[tuple[S3Client, Stubber]]:
    client: S3Client = boto3.client(
        "s3",
        region_name="us-east-1",
        config=Config(signature_version=UNSIGNED),
    )
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def test_the_key_names_table_and_content_address() -> None:
    assert staging_object_key(STAGED) == KEY


def test_t7_a_successful_put_writes_once_and_never_heads(s3: tuple[S3Client, Stubber]) -> None:
    client, stubber = s3
    stubber.add_response("put_object", {}, PUT)
    render = _Render()
    S3UnitStager(BUCKET, client).ensure(STAGED, render)
    assert render.calls == 1


def test_t7_an_existing_identical_object_is_present_without_a_second_put(
    s3: tuple[S3Client, Stubber],
) -> None:
    client, stubber = s3
    stubber.add_client_error("put_object", "PreconditionFailed", http_status_code=412)
    stubber.add_response(
        "head_object", {"ChecksumSHA256": CHECKSUM, "ContentLength": len(BODY)}, HEAD
    )
    render = _Render()
    S3UnitStager(BUCKET, client).ensure(STAGED, render)
    assert render.calls == 1


@pytest.mark.parametrize(
    "head",
    [
        {"ChecksumSHA256": base64.b64encode(b"\x00" * 32).decode("ascii"), "ContentLength": 46},
        {"ChecksumSHA256": CHECKSUM, "ContentLength": len(BODY) + 1},
        {"ContentLength": len(BODY)},
    ],
)
def test_t7_an_existing_object_with_other_bytes_is_a_conflict(
    s3: tuple[S3Client, Stubber], head: dict[str, object]
) -> None:
    client, stubber = s3
    stubber.add_client_error("put_object", "PreconditionFailed", http_status_code=412)
    stubber.add_response("head_object", head, HEAD)
    with pytest.raises(InvariantError) as refused:
        S3UnitStager(BUCKET, client).ensure(STAGED, _Render())
    assert refused.value.code == STAGING_CONFLICT


def test_t7_a_rejected_checksum_is_a_render_divergence(s3: tuple[S3Client, Stubber]) -> None:
    client, stubber = s3
    stubber.add_client_error("put_object", "BadDigest", http_status_code=400)
    with pytest.raises(InvariantError) as refused:
        S3UnitStager(BUCKET, client).ensure(STAGED, _Render())
    assert refused.value.code == STAGING_RENDER_DIVERGED


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("SlowDown", 503),
        ("InternalError", 500),
        ("ConditionalRequestConflict", 409),
        ("AccessDenied", 403),
    ],
)
def test_t7_throttling_5xx_409_and_403_are_staging_unavailable(
    s3: tuple[S3Client, Stubber], code: str, status: int
) -> None:
    client, stubber = s3
    stubber.add_client_error("put_object", code, http_status_code=status)
    with pytest.raises(StagingUnavailableError) as refused:
        S3UnitStager(BUCKET, client).ensure(STAGED, _Render())
    assert isinstance(refused.value, DependencyError)
    assert refused.value.code == STAGING_UNAVAILABLE


def test_t7_a_failed_head_after_412_is_staging_unavailable(s3: tuple[S3Client, Stubber]) -> None:
    client, stubber = s3
    stubber.add_client_error("put_object", "PreconditionFailed", http_status_code=412)
    stubber.add_client_error("head_object", "SlowDown", http_status_code=503)
    with pytest.raises(StagingUnavailableError):
        S3UnitStager(BUCKET, client).ensure(STAGED, _Render())


def test_t7_a_blank_bucket_is_refused_at_construction(s3: tuple[S3Client, Stubber]) -> None:
    client, _ = s3
    with pytest.raises(ValueError, match="staging bucket"):
        S3UnitStager(" ", client)


@pytest.mark.parametrize(
    "changes",
    [
        {"staging_key": "A" * 64},
        {"staging_key": "a" * 63},
        {"byte_length": 0},
        {"row_counts": ()},
        {"row_counts": (("pub-a", 0),)},
        {"row_counts": (("pub-a", 1), ("pub-a", 1))},
        {"table": "Not-A-Table"},
    ],
)
def test_a_malformed_staged_unit_is_an_invariant(changes: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "table": STAGED.table,
        "staging_key": STAGED.staging_key,
        "byte_length": STAGED.byte_length,
        "row_counts": STAGED.row_counts,
        **changes,
    }
    with pytest.raises(InvariantError):
        StagedUnit(**fields)  # pyright: ignore[reportArgumentType]
