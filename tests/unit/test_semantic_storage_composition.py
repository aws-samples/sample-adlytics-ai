# SPDX-License-Identifier: MIT-0
"""Semantic storage composition for the S3-only ingestion path."""

from __future__ import annotations

from typing import Any, cast

import pytest
from unit_staging_support import RecordingStager

from adapters.jobs.reprocess_semantic_v2.resources import resolve_named_semantic_resources
from adapters.jobs.stage1_operator_bootstrap import parameter_path
from adapters.outbound.athena import semantic_storage as semantic_module
from adapters.outbound.athena.execution import StatementOutcome, StatementRequest
from adapters.outbound.athena.semantic_storage import AthenaSemanticTarget, ProjectionClients
from adapters.outbound.s3.semantic_storage import S3CanonicalAdapter, S3SourceAlignedAdapter
from adapters.outbound.semantic_storage import (
    ReplicatedSemanticStorageRequest,
    replicated_semantic_storage,
    s3_only_semantic_storage,
)
from adapters.outbound.v2_storage_catalog import V2Column, V2Table
from ports.outbound.canonical import CanonicalDatasetReadPort, CanonicalDatasetWritePort
from ports.outbound.source_aligned import SourceAlignedReadPort, SourceAlignedWritePort

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]


def test_s3_only_storage_returns_existing_port_compatible_adapters() -> None:
    storage = s3_only_semantic_storage("semantic-bucket", cast("Any", object()))

    assert type(storage.source) is S3SourceAlignedAdapter
    assert isinstance(storage.source, SourceAlignedWritePort)
    assert isinstance(storage.source, SourceAlignedReadPort)
    assert type(storage.canonical) is S3CanonicalAdapter
    assert isinstance(storage.canonical, CanonicalDatasetWritePort)
    assert isinstance(storage.canonical, CanonicalDatasetReadPort)


def test_s3_only_storage_rejects_a_blank_bucket() -> None:
    with pytest.raises(ValueError, match="semantic storage bucket is required"):
        s3_only_semantic_storage("  ", cast("Any", object()))


# --- ADR-149 T-17: the operator path stages, verifies first and merges only what is missing --


_TABLE = V2Table(
    name="fct_operator_rows",
    columns=(
        V2Column(name="publication_id", type="string", nullable=False),
        V2Column(name="label", type="string", nullable=False),
    ),
    grain=("publication_id", "label"),
    partition_keys=("publication_id",),
)
_IDENTITY = "publication-fictional-0001"
_REQUEST = semantic_module._ProjectionRequest(  # pyright: ignore[reportPrivateUsage]
    table=_TABLE,
    database="db_candidate",
    records=(
        {"publication_id": _IDENTITY, "label": "a"},
        {"publication_id": _IDENTITY, "label": "b"},
    ),
    identity=_IDENTITY,
)


def _counts(target: int, matched: int) -> list[list[str | None]]:
    return [
        ["target", _IDENTITY, str(target)],
        ["staged", _IDENTITY, "2"],
        ["matched_target", _IDENTITY, str(matched)],
        ["matched_staged", _IDENTITY, str(matched)],
    ]


def _projection(
    monkeypatch: pytest.MonkeyPatch, verifications: list[list[list[str | None]]]
) -> tuple[Any, list[str], RecordingStager]:
    order: list[str] = []
    stager = RecordingStager(calls=order)
    statements: list[str] = []

    def start(_client: object, request: StatementRequest) -> str:
        order.append("merge" if request.sql.startswith("MERGE") else "verification")
        statements.append(request.sql)
        return f"query-{len(statements)}"

    def complete(_client: object, _execution: str, *, limit: float) -> StatementOutcome:
        del limit
        return StatementOutcome(
            bytes_scanned=1,
            reused_previous_result=False,
            engine_millis=1,
            planning_millis=1,
            queue_millis=1,
            total_millis=1,
        )

    def rows(_client: object, _execution: str) -> list[list[str | None]]:
        return verifications.pop(0)

    monkeypatch.setattr(semantic_module, "start", start)
    monkeypatch.setattr(semantic_module, "await_completion", complete)
    monkeypatch.setattr(semantic_module, "fetch_rows", rows)
    projection = semantic_module._AthenaProjection(  # pyright: ignore[reportPrivateUsage]
        AthenaSemanticTarget("bake", "db_source", "db_candidate", "db_current", "db_staging"),
        cast("Any", object()),
        ProjectionClients(stager=stager, athena=cast("Any", object())),
    )
    return projection, order, stager


@pytest.mark.covers("ADR-149")
def test_t17_an_absent_identity_stages_merges_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    projection, order, stager = _projection(monkeypatch, [_counts(0, 0), _counts(2, 2)])

    assert projection.materialize(_REQUEST) == (True, True, "")

    assert order == ["stage", "verification", "merge", "verification"]
    (staged,) = stager.ensured
    assert staged.row_counts == ((_IDENTITY, 2),)


@pytest.mark.covers("ADR-149")
def test_t17_an_exact_identity_is_accepted_without_a_write(monkeypatch: pytest.MonkeyPatch) -> None:
    projection, order, _ = _projection(monkeypatch, [_counts(2, 2)])
    assert projection.materialize(_REQUEST) == (True, False, "")
    assert order == ["stage", "verification"]
    # attest runs the verification only.
    attesting, attested, _ = _projection(monkeypatch, [_counts(2, 2)])
    assert attesting.attest(_REQUEST) is True
    assert attested == ["stage", "verification"]


@pytest.mark.covers("ADR-149")
def test_t17_a_conflicting_identity_refuses_without_a_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One target row has no staged twin (target 2, matched 1): a MERGE would leave it there.
    projection, order, _ = _projection(monkeypatch, [_counts(2, 1)])
    assert projection.materialize(_REQUEST) == (
        False,
        False,
        "identity has different Iceberg records",
    )
    assert "merge" not in order
    # Control: a partly present identity (every target row matched) merges the rest.
    partial, partial_order, _ = _projection(monkeypatch, [_counts(1, 1), _counts(2, 2)])
    assert partial.materialize(_REQUEST) == (True, True, "")
    assert partial_order.count("merge") == 1


@pytest.mark.covers("ADR-149")
def test_t17_a_verification_still_differing_after_the_merge_names_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection, _, _ = _projection(monkeypatch, [_counts(0, 0), _counts(1, 1)])
    assert projection.materialize(_REQUEST) == (
        False,
        False,
        "Iceberg verification differs from the staged unit for fct_operator_rows.",
    )
    # Control: the same fake with matching counts after the MERGE accepts.
    control, _, _ = _projection(monkeypatch, [_counts(0, 0), _counts(2, 2)])
    assert control.materialize(_REQUEST) == (True, True, "")


@pytest.mark.covers("ADR-149")
def test_t17_the_jobs_resolve_the_contract_staging_paths_and_compose_one_stager() -> None:
    resolved: list[str] = []

    class _Ssm:
        def get_parameter(self, *, Name: str) -> dict[str, Any]:  # noqa: N803 - boto3 shape
            resolved.append(Name)
            return {"Parameter": {"Value": Name}}

    with pytest.raises(ValueError, match="does not own"):
        resolve_named_semantic_resources("dev", cast("Any", _CloudFormation()), cast("Any", _Ssm()))
    assert parameter_path("dev", "semanticV2StagingDatabase") in resolved
    assert parameter_path("dev", "semanticV2StagingBucketName") in resolved
    storage = replicated_semantic_storage(
        ReplicatedSemanticStorageRequest(
            bucket="semantic-bucket",
            staging_bucket="staging-bucket",
            target=AthenaSemanticTarget(
                "bake", "db_source", "db_candidate", "db_current", "db_staging"
            ),
            catalog=cast("Any", object()),
            s3=cast("Any", object()),
            athena=cast("Any", object()),
        )
    )
    assert storage.source is not None
    # Control: a blank staging bucket refuses at construction.
    with pytest.raises(ValueError, match="staging bucket"):
        ReplicatedSemanticStorageRequest(
            bucket="semantic-bucket",
            staging_bucket=" ",
            target=AthenaSemanticTarget(
                "bake", "db_source", "db_candidate", "db_current", "db_staging"
            ),
            catalog=cast("Any", object()),
            s3=cast("Any", object()),
            athena=cast("Any", object()),
        )


class _CloudFormation:
    """A paginator-shaped stack owning nothing, so the resolver refuses after reading paths."""

    def get_paginator(self, _name: str) -> Any:
        class _Pages:
            def paginate(self, **_kwargs: object) -> list[dict[str, Any]]:
                return [{"StackResourceSummaries": []}]

        return _Pages()
