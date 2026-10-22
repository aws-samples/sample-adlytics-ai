# SPDX-License-Identifier: MIT-0
"""Fictional phase-one delivery fixtures shared by the load handler and planning tests.

Every identity here is invented; the catalog is the repository's own generated one, so the
fixtures prove only the declared structure and the deterministic packer behaviour over it.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from adapters.config import Stage1DeliverySettings
from adapters.jobs.ingest_delivery.semantic_sidecar import (
    PreparedProjectionPartition,
    PreparedProjectionPlan,
    SemanticSidecarOutcome,
    SemanticSidecarReceipt,
    SemanticSidecarResult,
)
from adapters.outbound.athena.semantic_storage import AthenaSemanticTarget
from adapters.outbound.v2_storage_catalog import load_v2_storage_catalog
from domain.semantics.canonical_compiler import CanonicalRow, canonical_rows_checksum
from domain.semantics.identity import (
    CommerceScopeKey,
    DeliveryScope,
    ExtractionProfileKey,
    SourceDatasetKey,
    SourceIdentifier,
    SourceSystemKey,
)
from domain.semantics.publication import (
    AttemptId,
    CanonicalDatasetKey,
    CanonicalPublisherKey,
    LeaseHolderKey,
    PartitionIdentity,
    PublicationCandidate,
    PublicationId,
    PublicationLensKey,
    PublicationScope,
    SourcePublicationRef,
    SuccessfulPublication,
)
from domain.semantics.revision import RevisionSchemeKey, SourceRevision
from domain.semantics.source_aligned import (
    LandedDeliveryRef,
    SourceAlignedField,
    SourceAlignedPublication,
    SourceAlignedRow,
    SourceAlignedRowset,
    SourceInterpreterKey,
    SourceOption,
)
from domain.semantics.time import (
    BusinessCalendarKey,
    ExtractionCoverage,
    FactTimeClass,
    MeasurementBucket,
    TimeGrain,
)
from domain.semantics.validation import (
    PrepublicationRuleFamily,
    PrepublicationRuleResult,
    PublicationEvidence,
    PublicationEvidenceSubject,
    RuleOutcome,
)
from domain.shared_kernel.values import DateRange
from ports.outbound.canonical import CanonicalDestination, CanonicalMaterializeRequest

__all__ = [
    "CATALOG",
    "CONTRACT_ID",
    "DATASET",
    "DELIVERY_ENVIRONMENT",
    "FENCING_TOKEN",
    "INSTANT",
    "LANDED",
    "MANIFEST_KEY",
    "MANIFEST_VERSION",
    "NOW",
    "ORIGIN_SOURCE_IDENTITY",
    "PLAN_DIGEST",
    "PUBLICATION_ID",
    "SETTINGS",
    "TARGET",
    "candidate",
    "digest",
    "partition",
    "prepared",
    "published",
    "result",
    "rowless_prepared",
    "rows",
    "source",
    "successful",
]

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = load_v2_storage_catalog(ROOT / "contracts/generated/v2-tables.json")
NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)
DAY = date(2026, 8, 30)
CALENDAR = BusinessCalendarKey("iso_gregorian", 0)
BUCKET = MeasurementBucket(DateRange(DAY, DAY), TimeGrain.DAY, CALENDAR, "UTC")
DATASET = "fct_retail_purchase_amc"
CONTRACT_ID = "amc_retail_purchases"
MANIFEST_KEY = "fictional/source/window/manifest.json"
MANIFEST_VERSION = "manifest-version-fictional"
LANDED = LandedDeliveryRef(
    manifest_key=MANIFEST_KEY,
    manifest_version=MANIFEST_VERSION,
    manifest_checksum="sha256:manifest-fictional",
    object_key="fictional/source/window/data.csv",
    object_version="object-version-fictional",
    object_checksum="sha256:object-fictional",
    first_observed_at=NOW,
)
TARGET = AthenaSemanticTarget(
    batch_workgroup="fictional-batch",
    source_database="z_source",
    candidate_database="a_candidate",
    current_database="m_current",
    staging_database="s_staging",
)
SETTINGS = Stage1DeliverySettings(
    max_units=15,
    max_accepted_attempts_per_unit=10,
    max_accepted_attempts_per_plan=32,
    max_group_members=4,
    max_pointers_per_table=37,
    max_object_refs_per_unit=37,
    athena_max_statement_bytes=200_000,
    max_encoded_identity_bytes=128,
    max_partition_identity_bytes=256,
    table_lease_duration_seconds=930,
)
DELIVERY_ENVIRONMENT = {
    # The full write-discipline §11 object the delivery Lambda's strict accessor resolves.
    "ADL_PROJECTION_HANDOFF_STATE": "INLINE_ACTIVE",
    "ADL_STAGE1_MAX_UNITS": "15",
    "ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_UNIT": "10",
    "ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_PLAN": "32",
    "ADL_STAGE1_MAX_GROUP_MEMBERS": "4",
    "ADL_STAGE1_MAX_POINTERS_PER_TABLE": "37",
    "ADL_STAGE1_MAX_OBJECT_REFS_PER_UNIT": "37",
    "ADL_STAGE1_ATHENA_MAX_STATEMENT_BYTES": "200000",
    "ADL_STAGE1_MAX_ENCODED_IDENTITY_BYTES": "128",
    "ADL_STAGE1_MAX_PARTITION_IDENTITY_BYTES": "256",
    "ADL_TABLE_WRITER_TIMEOUT_SECONDS": "900",
    "ADL_TABLE_WRITER_CLEANUP_RESERVE_MS": "60000",
    "ADL_TABLE_WRITER_CONCURRENCY": "8",
    "ADL_TABLE_WRITER_MAX_RECEIVE_COUNT": "3",
    "ADL_TABLE_LEASE_DURATION_SECONDS": "930",
    "ADL_TABLE_WRITER_VISIBILITY_MARGIN_SECONDS": "60",
    "ADL_TABLE_ATTEMPT_RECONCILE_BATCH_SIZE": "10",
    "ADL_TABLE_ATTEMPT_RECONCILE_BASE_BACKOFF_SECONDS": "60",
    "ADL_TABLE_ATTEMPT_RECONCILE_MAX_BACKOFF_SECONDS": "900",
    "ADL_PLAN_INTENT_RECONCILE_BATCH_SIZE": "50",
    "ADL_PLAN_INTENT_RECONCILE_BASE_BACKOFF_SECONDS": "60",
    "ADL_PLAN_INTENT_RECONCILE_MAX_BACKOFF_SECONDS": "900",
    "ADL_PROJECTION_HANDOFF_DRAIN_OBSERVATION_SECONDS": "300",
}
INSTANT = "01787832000000000000"
PUBLICATION_ID = "publication-fictional-0001"
FENCING_TOKEN = 7

# Golden bytes over the fictional manifest pair, computed independently below in the tests.
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
ORIGIN_SOURCE_IDENTITY = "1d474a739b1ebe0018fb7057d7093cfe877281da22d1b7761c05ca5781fe315d"
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
PLAN_DIGEST = "bae66bd52d81ee9b25214183434baaf64cd3f4b53589ad02afad8b8b8fb0585b"


# --- Fictional fixtures ---------------------------------------------------------------------------


def source(reference: str = "source-fictional-0001", *, rows: int = 1) -> SourceAlignedPublication:
    return SourceAlignedPublication(
        SourcePublicationRef(reference),
        LANDED,
        SourceAlignedRowset(
            DeliveryScope(
                SourceSystemKey("amc"),
                SourceDatasetKey("amazon_retail_purchases"),
                ExtractionProfileKey("amc.retail_purchases", 1),
                SourceIdentifier("amc.entity", "fictional-entity"),
                "options-hash",
            ),
            ExtractionCoverage(DateRange(DAY, DAY)),
            SourceInterpreterKey(f"source_pack.{CONTRACT_ID}", 1),
            (SourceOption("fictional", "true"),),
            tuple(
                SourceAlignedRow(
                    fields=(SourceAlignedField("amount", Decimal("1.2500")),),
                    source_identifiers=(SourceIdentifier("amc.asin", f"B0FICTION{index:03d}"),),
                    measurement=BUCKET,
                )
                for index in range(rows)
            ),
        ),
        SourceRevision.instant(
            scheme=RevisionSchemeKey("amc.execution"),
            source_id="execution-fictional",
            observed_at=NOW,
        ),
        NOW,
    )


def rows(count: int = 1) -> tuple[CanonicalRow, ...]:
    return tuple(
        (
            ("amazon_asin", f"amazon_asin:b0fictional{index:03d}"),
            ("basis_key", "retail_actual"),
            ("currency_code", "USD"),
            ("date_key", DAY),
            ("distinct_user_count", 100),
            ("ecosystem_key", "amazon"),
            ("marketplace_key", "amazon_us"),
            ("platform_key", "amazon_retail"),
            ("revenue_purchased", Decimal("12.3400")),
            ("rows_suppressed", False),
            ("source_timezone", "UTC"),
            ("suppression_reason", None),
            ("units_purchased", 10),
        )
        for index in range(count)
    )


def partition(lens: str = "lens") -> PartitionIdentity:
    return PartitionIdentity(
        PublicationScope(
            CanonicalDatasetKey(DATASET),
            CommerceScopeKey("fx"),
            "us",
            PublicationLensKey(lens),
            FactTimeClass.DAILY_ADDITIVE,
            CALENDAR,
            "UTC",
        ),
        BUCKET,
    )


def _evidence(partition: PartitionIdentity, checksum: str) -> PublicationEvidence:
    subject = PublicationEvidenceSubject(
        "canonical-curation",
        partition.identity,
        ("source-fictional-0001",),
        "amc.execution|instant|2026-08-30T12:00:00+00:00|execution-fictional",
        checksum,
    )
    results = tuple(
        PrepublicationRuleResult(
            family, f"fictional.{family.value}.v1", RuleOutcome.PASSED, "Fictional rule passed."
        )
        for family in PrepublicationRuleFamily
    )
    return PublicationEvidence("semantic-v2.1", subject, results)


def _revision() -> SourceRevision:
    return SourceRevision.instant(
        scheme=RevisionSchemeKey("amc.execution"),
        source_id="execution-fictional",
        observed_at=NOW,
    )


def candidate(
    rows: tuple[CanonicalRow, ...], partition: PartitionIdentity
) -> CanonicalMaterializeRequest:
    checksum = canonical_rows_checksum(rows)
    candidate = PublicationCandidate(
        PublicationId(PUBLICATION_ID),
        CanonicalPublisherKey("canonical-curation"),
        partition,
        AttemptId("attempt-fictional-0001"),
        LeaseHolderKey("holder-fictional-0001"),
        FENCING_TOKEN,
        (SourcePublicationRef("source-fictional-0001"),),
        _revision(),
        checksum,
        _evidence(partition, checksum),
        NOW,
    )
    return CanonicalMaterializeRequest(
        CanonicalDestination.CANDIDATE, candidate, rows, "semantic-v2.1"
    )


def successful(
    rows: tuple[CanonicalRow, ...], partition: PartitionIdentity
) -> CanonicalMaterializeRequest:
    checksum = canonical_rows_checksum(rows)
    successful = SuccessfulPublication(
        publication_id=PublicationId(PUBLICATION_ID),
        publisher=CanonicalPublisherKey("canonical-curation"),
        partition=partition,
        attempt_id=AttemptId("attempt-fictional-0001"),
        source_inputs=(SourcePublicationRef("source-fictional-0001"),),
        source_revision=_revision(),
        content_checksum=checksum,
        evidence=_evidence(partition, checksum),
        fencing_token=FENCING_TOKEN,
        published_at=NOW,
    )
    return CanonicalMaterializeRequest(
        CanonicalDestination.SUCCESSFUL, successful, rows, "semantic-v2.1"
    )


def result(*receipts: SemanticSidecarReceipt) -> SemanticSidecarResult:
    return SemanticSidecarResult(
        contract_id=CONTRACT_ID,
        outcome=SemanticSidecarOutcome.PUBLISHED,
        detail="fictional publication",
        receipts=receipts,
    )


def published(row_count: int = 1) -> tuple[SemanticSidecarResult, ...]:
    source_publication = source()
    canonical_rows = rows(row_count)
    identity = partition()
    return (
        result(
            SemanticSidecarReceipt(
                landed_delivery=LANDED,
                source_publications=(source_publication,),
                candidate=candidate(canonical_rows, identity),
                successful=successful(canonical_rows, identity),
            )
        ),
    )


def prepared(row_count: int = 1) -> PreparedProjectionPlan:
    source_publication = source()
    identity = partition()
    canonical_rows = rows(row_count)
    return PreparedProjectionPlan(
        source_publications=(source_publication,),
        partitions=(
            PreparedProjectionPartition(
                contract_id=CONTRACT_ID,
                dataset=DATASET,
                partition_identity=identity.identity,
                source_publications=(source_publication,),
                canonical_rows=canonical_rows,
                content_checksum=canonical_rows_checksum(canonical_rows),
            ),
        ),
        groups=(((CONTRACT_ID, identity.identity),),),
    )


def digest(value: object) -> str:
    """Independent canonical-JSON SHA-256 for golden-byte assertions."""
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def rowless_prepared() -> PreparedProjectionPlan:
    """Build a prepared plan whose only partition compiled zero canonical rows."""
    empty = source(rows=0)
    return replace(
        prepared(),
        source_publications=(empty,),
        partitions=(
            PreparedProjectionPartition(
                contract_id=CONTRACT_ID,
                dataset=DATASET,
                partition_identity=partition().identity,
                source_publications=(empty,),
                canonical_rows=(),
                content_checksum=canonical_rows_checksum(()),
            ),
        ),
    )
