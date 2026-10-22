# SPDX-License-Identifier: MIT-0
"""Replicate immutable Semantic V2 evidence to S3 authority and Iceberg projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from adapters.outbound.athena.semantic_storage import (
    AthenaCanonicalMaterializer,
    AthenaSemanticTarget,
    AthenaSourceMaterializer,
    ProjectionClients,
)
from adapters.outbound.s3.semantic_storage import (
    S3CanonicalAdapter,
    S3IdentityEvidenceAdapter,
    S3SourceAlignedAdapter,
)
from adapters.outbound.s3.unit_staging import S3UnitStager
from adapters.outbound.v2_storage_catalog import V2StorageCatalog
from domain.semantics.canonical_compiler import CanonicalRow
from domain.semantics.publication import SourcePublicationRef, SuccessfulPublication
from domain.semantics.source_aligned import SourceAlignedPublication
from ports.outbound.canonical import CanonicalMaterializeRequest, CanonicalWriteResult
from ports.outbound.identity_evidence import IdentityEvidencePort
from ports.outbound.source_aligned import MaterializeSourceRowsetResult

if TYPE_CHECKING:
    from types_boto3_athena.client import AthenaClient
    from types_boto3_s3.client import S3Client

__all__ = [
    "ReplicatedCanonicalStorage",
    "ReplicatedSemanticStorage",
    "ReplicatedSemanticStorageRequest",
    "ReplicatedSourceAlignedStorage",
    "S3OnlySemanticStorage",
    "replicated_semantic_storage",
    "s3_only_semantic_storage",
]


class ReplicatedSourceAlignedStorage:
    """Require exact immutable S3 evidence and exact Iceberg source projection."""

    def __init__(
        self,
        s3: S3SourceAlignedAdapter,
        iceberg: AthenaSourceMaterializer,
    ) -> None:
        self._s3 = s3
        self._iceberg = iceberg

    def attest(self, publication: SourceAlignedPublication) -> MaterializeSourceRowsetResult:
        """Verify immutable source authority before its projection without mutation."""
        if self._s3.publication(publication.reference) != publication:
            return MaterializeSourceRowsetResult(
                accepted=False,
                created=False,
                reason="immutable S3 source publication differs.",
            )
        return self._iceberg.attest(publication)

    def materialize(self, publication: SourceAlignedPublication) -> MaterializeSourceRowsetResult:
        """Require both immutable S3 and exact Iceberg source materialization."""
        stored = self._s3.materialize(publication)
        if not stored.accepted:
            return stored
        reread = self._s3.publication(publication.reference)
        if reread != publication:
            return MaterializeSourceRowsetResult(
                accepted=False,
                created=False,
                reason="immutable S3 source readback differs from publication",
            )
        indexed = self._iceberg.materialize(publication)
        return MaterializeSourceRowsetResult(
            accepted=indexed.accepted,
            created=indexed.accepted and (stored.created or indexed.created),
            reason=indexed.reason,
        )

    def publication(self, reference: SourcePublicationRef) -> SourceAlignedPublication | None:
        """Resolve source authority from immutable S3, never from its analytical projection."""
        return self._s3.publication(reference)


class ReplicatedCanonicalStorage:
    """Require immutable S3 canonical evidence before exact Iceberg projection."""

    def __init__(
        self,
        s3: S3CanonicalAdapter,
        iceberg: AthenaCanonicalMaterializer,
    ) -> None:
        self._s3 = s3
        self._iceberg = iceberg

    def rows(self, publication: SuccessfulPublication) -> tuple[CanonicalRow, ...]:
        """Read exact successful rows from immutable S3 rather than the projection."""
        return self._s3.rows(publication)

    def attest(self, request: CanonicalMaterializeRequest) -> CanonicalWriteResult:
        """Verify immutable canonical bytes before their projection without mutation."""
        stored = self._s3.attest(request)
        if not stored.accepted:
            return stored
        return self._iceberg.attest(request)

    def materialize(self, request: CanonicalMaterializeRequest) -> CanonicalWriteResult:
        """Require both immutable S3 and exact Iceberg canonical materialization."""
        stored = self._s3.materialize(request)
        if not stored.accepted:
            return stored
        indexed = self._iceberg.materialize(request)
        return CanonicalWriteResult(
            accepted=indexed.accepted,
            created=indexed.accepted and (stored.created or indexed.created),
            row_count=indexed.row_count,
            content_checksum=indexed.content_checksum,
            reason=indexed.reason,
        )


@dataclass(frozen=True, slots=True)
class ReplicatedSemanticStorage:
    """Composed source authority/projection and canonical authority/projection stores."""

    source: ReplicatedSourceAlignedStorage
    canonical: ReplicatedCanonicalStorage
    identity_evidence: IdentityEvidencePort


@dataclass(frozen=True, slots=True)
class S3OnlySemanticStorage:
    """Composed immutable S3 authorities for the ingestion Lambda path."""

    source: S3SourceAlignedAdapter
    canonical: S3CanonicalAdapter
    identity_evidence: IdentityEvidencePort


def s3_only_semantic_storage(bucket: str, s3: S3Client) -> S3OnlySemanticStorage:
    """Compose existing source and canonical ports over immutable S3 only."""
    return S3OnlySemanticStorage(
        source=S3SourceAlignedAdapter(bucket, s3),
        canonical=S3CanonicalAdapter(bucket, s3),
        identity_evidence=S3IdentityEvidenceAdapter(bucket, s3),
    )


@dataclass(frozen=True, slots=True)
class ReplicatedSemanticStorageRequest:
    """Complete physical target and typed clients for Semantic V2 replication."""

    bucket: str
    staging_bucket: str
    """ADR-149: where the Iceberg projection stages each identity's unit before its MERGE."""
    target: AthenaSemanticTarget
    catalog: V2StorageCatalog
    s3: S3Client
    athena: AthenaClient

    def __post_init__(self) -> None:
        if not self.bucket.strip():
            raise ValueError("replicated Semantic V2 storage requires a bucket.")
        if not self.staging_bucket.strip():
            raise ValueError("replicated Semantic V2 storage requires a staging bucket.")


def replicated_semantic_storage(
    request: ReplicatedSemanticStorageRequest,
) -> ReplicatedSemanticStorage:
    """Compose immutable S3 authority before generated Iceberg projection."""
    clients = ProjectionClients(
        stager=S3UnitStager(request.staging_bucket, request.s3), athena=request.athena
    )
    return ReplicatedSemanticStorage(
        source=ReplicatedSourceAlignedStorage(
            S3SourceAlignedAdapter(request.bucket, request.s3),
            AthenaSourceMaterializer(request.target, request.catalog, clients),
        ),
        canonical=ReplicatedCanonicalStorage(
            S3CanonicalAdapter(request.bucket, request.s3),
            AthenaCanonicalMaterializer(request.target, request.catalog, clients),
        ),
        identity_evidence=S3IdentityEvidenceAdapter(request.bucket, request.s3),
    )
