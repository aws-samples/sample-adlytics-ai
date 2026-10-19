# SPDX-License-Identifier: MIT-0
"""Real Athena validation for generated Semantic V2 immutable Iceberg projections."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from probe_identity import PROBE_PREFIX
from types_boto3_athena.client import AthenaClient

from adapters.outbound.athena.execution import StatementRequest, await_completion, start
from adapters.outbound.athena.literals import quoted
from adapters.outbound.athena.semantic_storage import (
    AthenaCanonicalMaterializer,
    AthenaSemanticTarget,
    AthenaSourceMaterializer,
    AthenaTableUnitProjector,
    UnitWorkload,
    canonical_records,
)
from adapters.outbound.v2_storage_catalog import V2StorageCatalog, load_v2_storage_catalog
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

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-014", "ADR-044", "ADR-049")]

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
CALENDAR = BusinessCalendarKey("iso_gregorian", week_starts_on=0)


def _target() -> AthenaSemanticTarget:
    paths = {
        # The key is the `AthenaSemanticTarget.batch_workgroup` field; its value is BAKE.
        "batch_workgroup": "/adlytics/dev/data/v2/athena-workgroup-bake",
        "source_database": "/adlytics/dev/data/v2/source-aligned-glue-database",
        "candidate_database": "/adlytics/dev/data/v2/canonical-candidate-glue-database",
        "current_database": "/adlytics/dev/data/v2/canonical-current-glue-database",
    }
    try:
        client = boto3.client("ssm")
        values = {
            key: str(client.get_parameter(Name=path)["Parameter"].get("Value", ""))
            for key, path in paths.items()
        }
    except Exception as error:  # noqa: BLE001 — pytest.skip raises: no deployed target
        pytest.skip(f"deployed Semantic V2 Athena target is unavailable: {type(error).__name__}")
    if any(not value for value in values.values()):
        pytest.skip("deployed Semantic V2 Athena target parameter resolved to nothing")
    return AthenaSemanticTarget(**values)


def _identity(test_name: str, suffix: str) -> str:
    node_id = f"{__file__}::{test_name}::{suffix}"
    return f"{PROBE_PREFIX}-{hashlib.sha256(node_id.encode()).hexdigest()[:20]}"


def _source(reference: str, *, with_row: bool) -> SourceAlignedPublication:
    measurement = MeasurementBucket(DateRange(DAY, DAY), TimeGrain.DAY, CALENDAR, "UTC")
    rows = (
        (
            SourceAlignedRow(
                fields=(SourceAlignedField("probe_value", Decimal("1.2500")),),
                source_identifiers=(SourceIdentifier("probe.id", reference),),
                measurement=measurement,
            ),
        )
        if with_row
        else ()
    )
    return SourceAlignedPublication(
        reference=SourcePublicationRef(reference),
        landed_delivery=LandedDeliveryRef(
            f"{PROBE_PREFIX}/manifest.json",
            f"manifest-version-{reference}",
            f"sha256:manifest-{reference}",
            f"{PROBE_PREFIX}/rows.csv",
            f"object-version-{reference}",
            f"sha256:object-{reference}",
        ),
        rowset=SourceAlignedRowset(
            delivery_scope=DeliveryScope(
                SourceSystemKey("integration_probe"),
                SourceDatasetKey("semantic_storage"),
                ExtractionProfileKey("integration_probe", 1),
                SourceIdentifier("probe.scope", reference),
                "probe-options",
            ),
            extraction_coverage=ExtractionCoverage(DateRange(DAY, DAY)),
            interpreter=SourceInterpreterKey("source_pack.amc_attributed_conversion", 1),
            source_options=(SourceOption("probe", "true"),),
            rows=rows,
        ),
        source_revision=SourceRevision.instant(
            scheme=RevisionSchemeKey("integration.probe"),
            source_id=reference,
            observed_at=NOW,
        ),
        published_at=NOW,
    )


def _canonical(reference: str) -> CanonicalMaterializeRequest:
    rows: tuple[CanonicalRow, ...] = (
        (
            ("basis_key", "shipped"),
            ("date_key", DAY),
            ("marketplace_key", "integration_probe"),
            ("cogs_shipped", Decimal("4.2500")),
            ("currency_code", "USD"),
            ("customer_returns", None),
            ("distributor_view", "SOURCING"),
            ("period_end", DAY),
            ("period_start", DAY),
            ("report_options_hash", reference),
            ("report_period", "DAY"),
            ("report_type", "integration_probe"),
            ("retrieved_at", NOW),
            ("revenue_ordered", None),
            ("revenue_shipped", Decimal("9.5000")),
            ("selling_program", "integration_probe"),
            ("source_timezone", "UTC"),
            ("source_watermark", DAY),
            ("units_ordered", None),
            ("units_shipped", 2),
        ),
    )
    checksum = canonical_rows_checksum(rows)
    partition = PartitionIdentity(
        PublicationScope(
            CanonicalDatasetKey("fct_retail_sales_reported_total"),
            CommerceScopeKey("integration_probe"),
            "integration-probe",
            PublicationLensKey("integration-probe"),
            FactTimeClass.DAILY_ADDITIVE,
            CALENDAR,
            "UTC",
        ),
        MeasurementBucket(DateRange(DAY, DAY), TimeGrain.DAY, CALENDAR, "UTC"),
    )
    revision = SourceRevision.instant(
        scheme=RevisionSchemeKey("integration.probe"),
        source_id=reference,
        observed_at=NOW,
    )
    source = SourcePublicationRef(f"source-{reference}")
    subject = PublicationEvidenceSubject(
        publisher="canonical-curation",
        partition=partition.identity,
        source_publications=(source.value,),
        source_revision=f"integration.probe|instant|{NOW.isoformat()}|{reference}",
        content_checksum=checksum,
    )
    results = tuple(
        PrepublicationRuleResult(
            family,
            f"integration.{family.value}.v1",
            RuleOutcome.PASSED,
            (
                'Governed identity mappings selected: [{"mapping_version":7}]'
                if family is PrepublicationRuleFamily.IDENTITY
                else "Integration probe passed."
            ),
        )
        for family in PrepublicationRuleFamily
    )
    candidate = PublicationCandidate(
        publication_id=PublicationId(reference),
        publisher=CanonicalPublisherKey("canonical-curation"),
        partition=partition,
        attempt_id=AttemptId(f"attempt-{reference}"),
        holder=LeaseHolderKey(f"holder-{reference}"),
        fencing_token=1,
        source_inputs=(source,),
        source_revision=revision,
        content_checksum=checksum,
        evidence=PublicationEvidence("semantic-v2.1", subject, results),
        published_at=NOW,
    )
    return CanonicalMaterializeRequest(
        CanonicalDestination.CANDIDATE,
        candidate,
        rows,
        "semantic-v2.1",
    )


def _successful(request: CanonicalMaterializeRequest) -> CanonicalMaterializeRequest:
    candidate = request.publication
    assert isinstance(candidate, PublicationCandidate)
    publication = SuccessfulPublication(
        publication_id=candidate.publication_id,
        publisher=candidate.publisher,
        partition=candidate.partition,
        attempt_id=candidate.attempt_id,
        source_inputs=candidate.source_inputs,
        source_revision=candidate.source_revision,
        content_checksum=candidate.content_checksum,
        evidence=candidate.evidence,
        fencing_token=candidate.fencing_token,
        published_at=candidate.published_at,
    )
    return CanonicalMaterializeRequest(
        CanonicalDestination.SUCCESSFUL,
        publication,
        request.rows,
        request.rule_set_version,
    )


@dataclass(frozen=True, slots=True)
class _DeleteRequest:
    client: AthenaClient
    target: AthenaSemanticTarget
    database: str
    table: str
    column: str
    identity: str


def _delete(request: _DeleteRequest) -> None:
    execution = start(
        request.client,
        StatementRequest(
            sql=(
                # A-SQL-001: quoted names, value binds as ?; see docs/security-dispositions.md
                # nosemgrep: python.django.security.injection.tainted-sql-string
                f"DELETE FROM {quoted(request.table)} "  # noqa: S608 - generated identifiers only  # nosec B608
                f"WHERE {quoted(request.column)} = CAST(? AS varchar)"
            ),
            parameters=(request.identity,),
            workgroup=request.target.batch_workgroup,
            database=request.database,
            reuse_minutes=0,
        ),
    )
    await_completion(request.client, execution, limit=180)


def test_source_empty_nonempty_exact_replay_and_conflict_round_trip() -> None:
    target = _target()
    catalog = load_v2_storage_catalog(ROOT / "contracts/generated/v2-tables.json")
    client = boto3.client("athena")
    adapter = AthenaSourceMaterializer(target, catalog, client)
    table = catalog.source_for_interpreter("source_pack.amc_attributed_conversion")
    references = (
        _identity(
            test_source_empty_nonempty_exact_replay_and_conflict_round_trip.__name__, "empty"
        ),
        _identity(
            test_source_empty_nonempty_exact_replay_and_conflict_round_trip.__name__, "nonempty"
        ),
    )
    for reference in references:
        _delete(
            _DeleteRequest(
                client,
                target,
                target.source_database,
                table.name,
                "source_publication_id",
                reference,
            )
        )
    try:
        empty = _source(references[0], with_row=False)
        nonempty = _source(references[1], with_row=True)
        assert adapter.materialize(empty).accepted
        absent = adapter.attest(nonempty)
        created = adapter.materialize(nonempty)
        attested = adapter.attest(nonempty)
        replay = adapter.materialize(nonempty)
        conflicting_publication = replace(
            nonempty,
            published_at=nonempty.published_at + timedelta(seconds=1),
        )
        conflict_attestation = adapter.attest(conflicting_publication)
        conflict = adapter.materialize(conflicting_publication)

        assert not absent.accepted and not absent.created
        assert created.accepted and created.created
        assert attested.accepted and not attested.created
        assert replay.accepted and not replay.created
        assert not conflict_attestation.accepted and not conflict_attestation.created
        assert not conflict.accepted and not conflict.created
    finally:
        for reference in references:
            _delete(
                _DeleteRequest(
                    client,
                    target,
                    target.source_database,
                    table.name,
                    "source_publication_id",
                    reference,
                )
            )


@pytest.mark.covers("ADR-075")
def test_table_unit_projector_merges_verifies_and_retry_skips_complete_identity() -> None:
    target = _target()
    catalog = load_v2_storage_catalog(ROOT / "contracts/generated/v2-tables.json")
    client = boto3.client("athena")
    projector = AthenaTableUnitProjector(target, client)
    reference = _identity(
        test_table_unit_projector_merges_verifies_and_retry_skips_complete_identity.__name__,
        "publication",
    )
    request = _canonical(reference)
    table = catalog.canonical("fct_retail_sales_reported_total", successful=False)
    workload = UnitWorkload(
        table=table,
        database=target.candidate_database,
        records_by_identity=((reference, canonical_records(request, table)),),
    )
    delete = _DeleteRequest(
        client,
        target,
        target.candidate_database,
        table.name,
        "publication_id",
        reference,
    )
    _delete(delete)
    try:
        initial = projector.project(workload, retry=False)
        retry = projector.project(workload, retry=True)

        assert initial.accepted and initial.statement_count == 2
        assert initial.row_count == 1 and initial.athena_seconds >= 0
        assert retry.accepted and retry.statement_count == 2
        assert retry.row_count == 1 and retry.athena_seconds >= 0
    finally:
        _delete(delete)


def test_candidate_and_successful_history_are_isolated_typed_and_insert_only() -> None:
    target = _target()
    catalog: V2StorageCatalog = load_v2_storage_catalog(ROOT / "contracts/generated/v2-tables.json")
    client = boto3.client("athena")
    adapter = AthenaCanonicalMaterializer(target, catalog, client)
    reference = _identity(
        test_candidate_and_successful_history_are_isolated_typed_and_insert_only.__name__,
        "publication",
    )
    table = catalog.canonical("fct_retail_sales_reported_total", successful=False)
    request = _canonical(reference)
    for database in (target.candidate_database, target.current_database):
        _delete(_DeleteRequest(client, target, database, table.name, "publication_id", reference))
    try:
        absent = adapter.attest(request)
        candidate = adapter.materialize(request)
        attested_candidate = adapter.attest(request)
        replay = adapter.materialize(request)
        conflicting_request = replace(
            request,
            publication=replace(request.publication, fencing_token=2),
        )
        conflict_attestation = adapter.attest(conflicting_request)
        conflict = adapter.materialize(conflicting_request)
        successful_request = _successful(request)
        absent_successful = adapter.attest(successful_request)
        successful = adapter.materialize(successful_request)
        attested_successful = adapter.attest(successful_request)

        assert not absent.accepted and not absent.created
        assert candidate.accepted and candidate.created and candidate.row_count == 1
        assert attested_candidate.accepted and not attested_candidate.created
        assert replay.accepted and not replay.created and replay.row_count == 1
        assert not conflict_attestation.accepted and not conflict_attestation.created
        assert not conflict.accepted and not conflict.created
        assert not absent_successful.accepted and not absent_successful.created
        assert successful.accepted and successful.created and successful.row_count == 1
        assert attested_successful.accepted and not attested_successful.created
        assert successful.content_checksum == request.publication.content_checksum
    finally:
        for database in (target.candidate_database, target.current_database):
            _delete(
                _DeleteRequest(client, target, database, table.name, "publication_id", reference)
            )
