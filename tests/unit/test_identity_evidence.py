# SPDX-License-Identifier: MIT-0
"""Identity-mapping claim check (write-discipline §2.6): exact refusals, positive controls."""

from __future__ import annotations

import hashlib
import io
from datetime import date
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError

from adapters.outbound.s3.semantic_storage import (
    IDENTITY_EVIDENCE_PREFIX,
    S3IdentityEvidenceAdapter,
    identity_evidence_key,
)
from domain.semantics.canonical_compiler import IdentityMappingEvidence
from domain.semantics.identity_evidence import (
    IDENTITY_CLAIM_PREFIX,
    IdentityMappingsClaim,
    IdentityMappingsDocument,
    identity_mappings_document,
    parse_identity_claim,
    verify_identity_mappings,
)
from domain.shared_kernel.errors import DependencyError, InvariantError

pytestmark = [pytest.mark.unit]

ABSENT = "ADLY-IDENTITY_EVIDENCE-0002"
MISMATCH = "ADLY-IDENTITY_EVIDENCE-0003"
MALFORMED = "ADLY-IDENTITY_EVIDENCE-0001"


def _mappings(count: int) -> tuple[IdentityMappingEvidence, ...]:
    return tuple(
        IdentityMappingEvidence(
            source_namespace="amc.campaign_id_string",
            source_value=f"91{index:016d}",
            target_value=f"campaign:fixture-{index:03d}",
            mapping_version=1,
            valid_from=date(2000, 1, 1),
            valid_to=None,
        )
        for index in range(count)
    )


def _code(error: pytest.ExceptionInfo[InvariantError]) -> str:
    return str(error.value.code)


def test_document_is_content_addressed_and_the_claim_is_fixed_size() -> None:
    small = identity_mappings_document(_mappings(79))
    large = identity_mappings_document(_mappings(5_000))

    assert small.claim.count == 79
    assert small.claim.sha256 == hashlib.sha256(small.body).hexdigest()
    assert identity_mappings_document(_mappings(79)) == small  # deterministic bytes
    assert len(small.claim.detail) < 200
    assert len(large.claim.detail) - len(small.claim.detail) == len("5000") - len("79")
    assert identity_evidence_key(small.claim) == (
        f"{IDENTITY_EVIDENCE_PREFIX}{small.claim.sha256}.json"
    )


def test_verify_returns_the_full_list_for_matching_bytes() -> None:
    document = identity_mappings_document(_mappings(3))

    mappings = verify_identity_mappings(document.claim, document.body)

    assert [item["target"] for item in mappings] == [
        "campaign:fixture-000",
        "campaign:fixture-001",
        "campaign:fixture-002",
    ]


def test_verify_refuses_an_absent_document_with_its_exact_code() -> None:
    document = identity_mappings_document(_mappings(3))
    assert verify_identity_mappings(document.claim, document.body)  # positive control

    with pytest.raises(InvariantError) as refused:
        verify_identity_mappings(document.claim, None)

    assert _code(refused) == ABSENT


def test_verify_refuses_a_digest_mismatch_with_its_exact_code() -> None:
    document = identity_mappings_document(_mappings(3))
    tampered = identity_mappings_document(_mappings(4)).body
    assert verify_identity_mappings(document.claim, document.body)  # positive control

    with pytest.raises(InvariantError) as refused:
        verify_identity_mappings(document.claim, tampered)

    assert _code(refused) == MISMATCH


def test_verify_refuses_a_count_that_differs_from_the_claim() -> None:
    document = identity_mappings_document(_mappings(3))
    wrong = IdentityMappingsClaim(4, document.claim.sha256)
    assert len(verify_identity_mappings(document.claim, document.body)) == 3  # positive control

    with pytest.raises(InvariantError) as refused:
        verify_identity_mappings(wrong, document.body)

    assert _code(refused) == MALFORMED


def test_parse_reads_the_claim_and_refuses_the_superseded_inline_form() -> None:
    document = identity_mappings_document(_mappings(2))
    assert parse_identity_claim(document.claim.detail) == document.claim  # positive control

    inline = 'Governed identity mappings selected: [{"mapping_version":1}]'
    with pytest.raises(InvariantError) as refused:
        parse_identity_claim(inline)

    assert _code(refused) == MALFORMED


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '{"count":2,"schema":"other","sha256":"' + "a" * 64 + '"}',
        '{"count":-1,"schema":"adlytics.identity-mappings.v1","sha256":"' + "a" * 64 + '"}',
        '{"count":2,"schema":"adlytics.identity-mappings.v1","sha256":"ABC"}',
        '{"count":true,"schema":"adlytics.identity-mappings.v1","sha256":"' + "a" * 64 + '"}',
        '{"count": 2, "schema": "adlytics.identity-mappings.v1", "sha256": "' + "a" * 64 + '"}',
    ],
)
def test_parse_refuses_every_non_canonical_claim(payload: str) -> None:
    canonical = identity_mappings_document(_mappings(2)).claim
    assert parse_identity_claim(canonical.detail) == canonical  # positive control

    with pytest.raises(InvariantError) as refused:
        parse_identity_claim(IDENTITY_CLAIM_PREFIX + payload)

    assert _code(refused) == MALFORMED


def test_a_document_cannot_be_built_with_bytes_that_miss_its_claim() -> None:
    document = identity_mappings_document(_mappings(2))
    with pytest.raises(ValueError, match="do not match"):
        IdentityMappingsDocument(document.claim, document.body + b" ")


class _StubS3:
    """The two calls the adapter makes, with S3's conditional-create and missing-key errors."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.puts.append(kwargs)
        if kwargs.get("IfNoneMatch") == "*" and kwargs["Key"] in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        body = self.objects.get(kwargs["Key"])
        if body is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(body)}


def _adapter() -> tuple[S3IdentityEvidenceAdapter, _StubS3]:
    stub = _StubS3()
    return S3IdentityEvidenceAdapter("semantic-bucket", cast("Any", stub)), stub


def test_s3_put_is_conditional_under_the_content_addressed_key_and_replays_exactly() -> None:
    adapter, stub = _adapter()
    document = identity_mappings_document(_mappings(3))

    first = adapter.put(document)
    replay = adapter.put(document)

    assert (first.accepted, first.created) == (True, True)
    assert (replay.accepted, replay.created) == (True, False)
    assert {put["Key"] for put in stub.puts} == {identity_evidence_key(document.claim)}
    assert all(put["IfNoneMatch"] == "*" for put in stub.puts)
    assert stub.objects[identity_evidence_key(document.claim)] == document.body


def test_s3_put_never_overwrites_other_bytes_under_the_same_key() -> None:
    adapter, stub = _adapter()
    document = identity_mappings_document(_mappings(3))
    other = identity_mappings_document(_mappings(4))
    assert adapter.put(other).created  # positive control: a free key is created
    stub.objects[identity_evidence_key(document.claim)] = b"{}"

    refused = adapter.put(document)

    assert (refused.accepted, refused.created) == (False, False)
    assert refused.reason == "identity evidence key already holds other bytes."
    assert stub.objects[identity_evidence_key(document.claim)] == b"{}"


def test_s3_read_verifies_and_refuses_absence_and_mismatch_exactly() -> None:
    adapter, stub = _adapter()
    document = identity_mappings_document(_mappings(3))
    adapter.put(document)
    assert len(adapter.read(document.claim)) == 3  # positive control

    stub.objects[identity_evidence_key(document.claim)] = document.body + b" "
    with pytest.raises(InvariantError) as mismatch:
        adapter.read(document.claim)
    del stub.objects[identity_evidence_key(document.claim)]
    with pytest.raises(InvariantError) as absent:
        adapter.read(document.claim)

    assert _code(mismatch) == MISMATCH
    assert _code(absent) == ABSENT


def test_s3_read_reports_an_unreadable_store_as_a_dependency_failure_not_absence() -> None:
    adapter, stub = _adapter()
    document = identity_mappings_document(_mappings(1))

    def denied(**_kwargs: Any) -> dict[str, Any]:
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    adapter.put(document)
    assert adapter.read(document.claim)  # positive control
    stub.get_object = denied  # type: ignore[method-assign]
    with pytest.raises(DependencyError) as unreadable:
        adapter.read(document.claim)

    # In Lambda (no ListBucket on the prefix) an absent object reads this way too: fail closed.
    assert str(unreadable.value.code) == "ADLY-SEMANTIC_STORAGE-0001"
