# SPDX-License-Identifier: MIT-0
"""The probe retrieval's `on_demand` twin request (S6, V-19; design.md, "Ingress by mode").

After `land_fixtures --probe-retrieval` puts its one new key, it sends one
`on_demand`/`bounded_validation` request for that exact manifest version, unless `--no-twin` is
given. The mode decides what the request does:

- `LEGACY`: no request (fixture landing takes the HEAD path);
- `SHADOW`: one epoch-0 decision;
- `LEGACY_DRAINING`: a `REFUSED(RUN_ADMISSION_NOT_ACTIVE)` receipt (row 3);
- `GRAPH`: admission.

The request ID derives from the exact (key, version), so a repeated twin returns its receipt.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from adapters.inbound.run_requests.service import (
    AdmissionDependencies,
    RunRequest,
    admit_run_request,
)
from adapters.jobs.curation.manifests import write_manifest
from adapters.jobs.execution_graph.resolution import resolve_graph_table
from adapters.jobs.reprocess_semantic_v2.resources import resolve_named_semantic_resources
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore
from adapters.outbound.dynamodb.stage1_writer_authority import Stage1WriterAuthorityReader
from adapters.outbound.legacy_head import S3CoordinatorLegacyHead
from adapters.outbound.s3.run_request_manifests import S3RunRequestManifests
from domain.operations.execution_graph import AuthorityMode
from domain.operations.generated_execution_graphs import PROTOCOL_VERSION

if TYPE_CHECKING:
    import boto3

    from adapters.jobs.land_fixtures.bundle import LandedDelivery
    from domain.curation.landing import LandedObject

__all__ = [
    "ProbeTwinRequest",
    "exact_manifest_version",
    "send_probe_twin",
    "twin_request",
    "verify_reserved_probe",
]


@dataclass(frozen=True, slots=True)
class ProbeTwinRequest:
    """Where the twin is decided and for which probe key."""

    environment: str
    store_stack: str
    bucket: str
    manifest_key: str
    principal: str


def twin_request(manifest_key: str, version_id: str, principal: str) -> RunRequest:
    """Build the deterministic `on_demand`/`bounded_validation` twin request."""
    digest = hashlib.sha256(f"{manifest_key}\n{version_id}".encode()).hexdigest()
    return RunRequest(
        request_id=f"twin-{digest[:48]}",
        protocol_version=PROTOCOL_VERSION,
        caller_kind="on_demand",
        reason="bounded_validation",
        manifest_key=manifest_key,
        version_id=version_id,
        requester=principal,
        requester_is_iam=True,
    )


def exact_manifest_version(session: boto3.Session, bucket: str, key: str) -> str:
    """Return the one version of the immutable probe key (an exact-key listing)."""
    versions: list[str] = []
    for page in (
        session.client("s3")
        .get_paginator("list_object_versions")
        .paginate(Bucket=bucket, Prefix=key)
    ):
        versions.extend(
            str(item.get("VersionId"))
            for item in page.get("Versions", [])
            if item.get("Key") == key
        )
    if len(versions) != 1:
        raise PermissionError(f"the probe key has {len(versions)} versions; exactly one expected.")
    return versions[0]


def verify_reserved_probe(session: boto3.Session, bucket: str, delivery: LandedDelivery) -> str:
    """Strongly verify a reserved probe delivery: one immutable version of each planned object.

    The reservation's manifest is written bound to its data objects' S3 versions
    (`apply_immutable`), so the expected manifest bytes are the plan's manifest bound to the one
    version of each planned data object, whose bytes must also be the planned ones. Returns the
    manifest's version. A missing, multi-version, or different-bytes object is refused, so a
    launch never sends a twin for a slot another landing took.
    """
    s3 = session.client("s3")
    bound: list[LandedObject] = []
    for planned, landed in zip(delivery.data, delivery.manifest.objects, strict=True):
        data_version = exact_manifest_version(session, bucket, planned.key)
        stored = s3.get_object(Bucket=bucket, Key=planned.key, VersionId=data_version)
        if stored["Body"].read() != planned.body:
            raise PermissionError(
                "a reserved probe data object holds different bytes than planned."
            )
        bound.append(replace(landed, version_id=data_version))
    expected = write_manifest(replace(delivery.manifest, objects=tuple(bound))).encode()
    marker = delivery.marker
    version = exact_manifest_version(session, bucket, marker.key)
    stored = s3.get_object(Bucket=bucket, Key=marker.key, VersionId=version)
    if stored["Body"].read() != expected:
        raise PermissionError("the reserved probe key holds different bytes than planned.")
    return version


def send_probe_twin(session: boto3.Session, request: ProbeTwinRequest) -> dict[str, object]:
    """Read the mode once and send the twin where the mode admits a request."""
    table = resolve_graph_table(session, request.environment, request.store_stack)
    dynamodb = session.client("dynamodb")
    store = DynamoExecutionGraphStore(table, dynamodb)
    mode = store.read_mode().mode
    version = exact_manifest_version(session, request.bucket, request.manifest_key)
    if mode is AuthorityMode.LEGACY:
        return {"mode": mode.value, "sent": False, "version_id": version}
    s3 = session.client("s3")
    resources = resolve_named_semantic_resources(
        request.environment, session.client("cloudformation"), session.client("ssm")
    )
    reader = Stage1WriterAuthorityReader(resources["publications"], resources["leases"], dynamodb)
    receipt = admit_run_request(
        twin_request(request.manifest_key, version, request.principal),
        AdmissionDependencies(
            store=store,
            manifests=S3RunRequestManifests(request.bucket, s3),
            now=lambda: datetime.now(UTC),
            legacy=S3CoordinatorLegacyHead(request.bucket, s3, reader),
        ),
    )
    return {
        "mode": mode.value,
        "sent": True,
        "version_id": version,
        "decision": receipt.decision,
        "receipt": asdict(receipt),
    }
