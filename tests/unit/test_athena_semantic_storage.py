# SPDX-License-Identifier: MIT-0
"""Pure V2 Iceberg record projection and insert-only Athena composition."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pytest

from adapters.outbound.athena import semantic_storage as semantic_module
from adapters.outbound.athena.execution import StatementOutcome
from adapters.outbound.athena.parameters import sql_literal
from adapters.outbound.athena.semantic_storage import (
    AthenaCanonicalMaterializer,
    AthenaSemanticTarget,
    AthenaSourceMaterializer,
    PhysicalRecord,
    UnitWorkload,
    canonical_records,
    canonical_unit_records,
    merge_batches,
    merge_statement,
    multi_select_statement,
    ordered_records,
    pack_object_batches,
    select_statement,
    source_records,
)
from adapters.outbound.athena.start_envelope import envelope_fits
from adapters.outbound.s3.semantic_storage import (
    S3CanonicalAdapter,
    S3SourceAlignedAdapter,
    canonical_object_bytes,
    projection_source_publication,
    projection_unit_source,
    source_publication_payload,
)
from adapters.outbound.semantic_storage import (
    ReplicatedCanonicalStorage,
    ReplicatedSourceAlignedStorage,
)
from adapters.outbound.v2_storage_catalog import V2Table, load_v2_storage_catalog
from domain.operations.statement_protocol import MAX_STATEMENT_BYTES
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
    WITHHELD_ONLY_RULE_ID,
    PrepublicationRuleFamily,
    PrepublicationRuleResult,
    PublicationEvidence,
    PublicationEvidenceSubject,
    RuleOutcome,
)
from domain.shared_kernel.values import DateRange
from ports.outbound.canonical import (
    CanonicalDestination,
    CanonicalMaterializeRequest,
    CanonicalWriteResult,
)
from ports.outbound.source_aligned import MaterializeSourceRowsetResult

if TYPE_CHECKING:
    from types_boto3_athena.client import AthenaClient

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-044", "ADR-049")]
ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = load_v2_storage_catalog(ROOT / "contracts/generated/v2-tables.json")
_FITS = envelope_fits("wg-fixture-batch", "candidate", MAX_STATEMENT_BYTES)
DAY = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
CALENDAR = BusinessCalendarKey("iso_gregorian", 0)
BUCKET = MeasurementBucket(DateRange(DAY, DAY), TimeGrain.DAY, CALENDAR, "UTC")


def _source(*, rows: tuple[SourceAlignedRow, ...]) -> SourceAlignedPublication:
    return SourceAlignedPublication(
        SourcePublicationRef("source-probe-001"),
        LandedDeliveryRef(
            "raw/probe/manifest.json",
            "manifest-version",
            "sha256:manifest",
            "raw/probe/data.csv",
            "object-version",
            "sha256:object",
        ),
        SourceAlignedRowset(
            DeliveryScope(
                SourceSystemKey("amc"),
                SourceDatasetKey("amazon_retail_purchases"),
                ExtractionProfileKey("amc.retail_purchases", 1),
                SourceIdentifier("amc.entity", "fictional-athena-entity"),
                "options-hash",
            ),
            ExtractionCoverage(DateRange(DAY, DAY)),
            SourceInterpreterKey("source_pack.amc_retail_purchases", 1),
            (SourceOption("probe", "true"),),
            rows,
        ),
        SourceRevision.instant(
            scheme=RevisionSchemeKey("amc.execution"),
            source_id="execution-probe",
            observed_at=NOW,
        ),
        NOW,
    )


def _canonical(*, withheld_only: bool = False) -> CanonicalMaterializeRequest:
    rows: tuple[CanonicalRow, ...] = (
        ()
        if withheld_only
        else (
            (
                ("amazon_asin", "amazon_asin:b0probe001"),
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
            ),
        )
    )
    checksum = canonical_rows_checksum(rows)
    partition = PartitionIdentity(
        PublicationScope(
            CanonicalDatasetKey("fct_retail_purchase_amc"),
            CommerceScopeKey("amazon_retail_us"),
            "amazon_us",
            PublicationLensKey("integration-probe"),
            FactTimeClass.DAILY_ADDITIVE,
            CALENDAR,
            "UTC",
        ),
        BUCKET,
    )
    revision = SourceRevision.instant(
        scheme=RevisionSchemeKey("amc.execution"),
        source_id="execution-probe",
        observed_at=NOW,
    )
    source = SourcePublicationRef("source-probe-001")
    subject = PublicationEvidenceSubject(
        "canonical-curation",
        partition.identity,
        (source.value,),
        "amc.execution|instant|2026-08-25T12:00:00+00:00|execution-probe",
        checksum,
    )
    results = tuple(
        PrepublicationRuleResult(
            family,
            (
                WITHHELD_ONLY_RULE_ID
                if withheld_only and family is PrepublicationRuleFamily.SUPPRESSION_COVERAGE
                else f"probe.{family.value}.v1"
            ),
            RuleOutcome.PASSED,
            "Probe passed.",
        )
        for family in PrepublicationRuleFamily
    )
    candidate = PublicationCandidate(
        PublicationId("publication-probe-001"),
        CanonicalPublisherKey("canonical-curation"),
        partition,
        AttemptId("attempt-probe-001"),
        LeaseHolderKey("holder-probe-001"),
        7,
        (source,),
        revision,
        checksum,
        PublicationEvidence("semantic-v2.1", subject, results),
        NOW,
    )
    return CanonicalMaterializeRequest(
        CanonicalDestination.CANDIDATE,
        candidate,
        rows,
        "semantic-v2.1",
    )


def test_generated_catalog_has_complete_equal_candidate_and_current_schemas() -> None:
    # Pack 1 (amc-new-to-brand-buyers-platform) adds one source-aligned table and one fact;
    # pack 4 (amc-dsp-reach) adds one source-aligned table and a writer of an existing fact;
    # pack 2 (amc-new-to-brand-buyers-channel) adds one source-aligned table and a grain set;
    # pack 3 (amc-new-to-brand-buyers-campaign) adds one source-aligned table and a grain set;
    # pack 5 (amc-exposure-groups) adds one source-aligned table, one fact and its residual;
    # packs 6 and 7 (amc-exposure-frequency-*) add two source-aligned tables, one fact and its
    # residual; packs 9, 13 and 8 add three source-aligned tables, dim_audience_segment, one
    # fact and its residual; packs 14 and 15 add two source-aligned tables,
    # fct_shopper_acquisition and its residual; packs 10-12 add three source-aligned tables and
    # fct_segment_profile, which has no residual (no additive measure).
    assert len(CATALOG.source_by_contract) == 29
    assert len(CATALOG.candidate_by_dataset) == 25
    assert CATALOG.candidate_by_dataset.keys() == CATALOG.current_by_dataset.keys()


def test_empty_source_projects_one_envelope_without_inventing_a_row() -> None:
    publication = _source(rows=())
    table = CATALOG.source_for_interpreter(publication.rowset.interpreter.value)
    records = source_records(publication, table)
    assert len(records) == 1
    assert records[0]["record_kind"] == "envelope"
    assert records[0]["source_row_ordinal"] == -1
    assert records[0]["source_payload_json"] is None
    scope = json.loads(str(records[0]["delivery_scope_json"]))
    assert scope["source_scope_namespace"] == "amc.entity"
    assert scope["source_scope_value"] == "fictional-athena-entity"
    payload = source_publication_payload(publication)
    assert payload["version"] == 2
    assert payload["delivery_scope"] == scope


def test_source_projection_preserves_typed_payload_and_stable_ordinal() -> None:
    publication = _source(
        rows=(
            SourceAlignedRow(
                fields=(SourceAlignedField("amount", Decimal("1.2500")),),
                source_identifiers=(SourceIdentifier("amc.asin", "B0PROBE001"),),
                measurement=BUCKET,
            ),
        )
    )
    table = CATALOG.source_for_interpreter(publication.rowset.interpreter.value)
    records = source_records(publication, table)
    assert len(records) == 2
    assert records[1]["source_row_ordinal"] == 0
    assert '"type":"decimal"' in str(records[1]["source_payload_json"])


def test_expected_projection_orders_numeric_grain_numerically_beyond_nine_rows() -> None:
    """A rendered-string sort put ordinal 10 before 2.

    The exact-partition SELECT orders the bigint numerically, so the expected
    projection diverged and every replay of a partition past nine rows was refused.
    """
    publication = _source(
        rows=tuple(
            SourceAlignedRow(
                fields=(SourceAlignedField("amount", Decimal(f"{index + 1}.2500")),),
                source_identifiers=(SourceIdentifier("amc.asin", f"B0PROBE{index:03d}"),),
                measurement=BUCKET,
            )
            for index in range(12)
        )
    )
    table = CATALOG.source_for_interpreter(publication.rowset.interpreter.value)
    records = ordered_records(table, source_records(publication, table))
    ordinals = [record["source_row_ordinal"] for record in records]
    assert ordinals == [-1, *range(12)]


def test_canonical_projection_fills_nullable_union_columns_and_preserves_types() -> None:
    request = _canonical()
    table = CATALOG.canonical("fct_retail_purchase_amc", successful=False)
    records = canonical_records(request, table)
    assert len(records) == 1
    assert records[0]["publication_id"] == "publication-probe-001"
    assert records[0]["revenue_purchased"] == Decimal("12.3400")
    assert records[0]["rows_suppressed"] is False


@pytest.mark.covers("ADR-075")
def test_projection_decoders_preserve_strict_source_and_flat_canonical_authority() -> None:
    source = _source(rows=())
    source_bytes = (json.dumps(source_publication_payload(source)) + "\n").encode()
    assert projection_source_publication(source_bytes) == source

    request = _canonical()
    decoded = projection_unit_source(canonical_object_bytes(request))
    table = CATALOG.canonical("fct_retail_purchase_amc", successful=False)

    assert decoded.destination == CanonicalDestination.CANDIDATE.value
    assert decoded.publication_id == request.publication.publication_id.value
    assert decoded.partition_key == request.publication.partition.identity
    assert decoded.fencing_token == request.publication.fencing_token
    assert decoded.content_checksum == request.publication.content_checksum
    assert canonical_unit_records(decoded, table) == canonical_records(request, table)

    malformed = json.loads(canonical_object_bytes(request))
    malformed["fencing_token"] = True
    with pytest.raises(ValueError, match="integer fencing_token"):
        projection_unit_source(json.dumps(malformed).encode())


@pytest.mark.covers("ADR-075")
def test_unit_workload_rejects_duplicate_identity_and_packs_whole_objects() -> None:
    request = _canonical()
    table = CATALOG.canonical("fct_retail_purchase_amc", successful=False)
    records = canonical_records(request, table)
    identity = request.publication.publication_id.value

    with pytest.raises(ValueError, match="repeat an identity"):
        UnitWorkload(
            table,
            "candidate",
            ((identity, records), (identity, records)),
        )

    packed = pack_object_batches(table, (records, records), _FITS)
    readback = multi_select_statement(table, 2)
    assert packed == (records + records,)
    assert readback.count("CAST(? AS varchar)") == 2
    assert 'ORDER BY "publication_id"' in readback


def test_insert_only_merge_and_exact_partition_select_never_delete_or_update() -> None:
    request = _canonical()
    table = CATALOG.canonical("fct_retail_purchase_amc", successful=False)
    records = canonical_records(request, table)
    sql, parameters = merge_statement(table, records)
    select = select_statement(table)
    assert "MERGE INTO" in sql
    assert "WHEN NOT MATCHED THEN INSERT" in sql
    assert "UPDATE" not in sql
    assert "DELETE" not in sql
    assert select.startswith("SELECT ")
    assert 'WHERE "publication_id" = CAST(? AS varchar)' in select
    assert select.count("?") == 1
    assert "ORDER BY" in select
    assert "MERGE" not in select
    assert "INSERT" not in select
    assert "UPDATE" not in select
    assert "DELETE" not in select
    assert parameters


def test_large_values_are_safely_inlined_beyond_execution_parameter_member_limit() -> None:
    publication = _source(
        rows=(
            SourceAlignedRow(
                fields=(SourceAlignedField("long_value", "x" * 1_100),),
                source_identifiers=(SourceIdentifier("amc.asin", "B0PROBE001"),),
                measurement=BUCKET,
            ),
        )
    )
    table = CATALOG.source_for_interpreter(publication.rowset.interpreter.value)
    sql, parameters = merge_statement(table, source_records(publication, table))

    assert "x" * 1_100 in sql
    assert all(len(sql_literal(value).encode()) <= 1_024 for value in parameters)


def _sixty_row_partition() -> tuple[V2Table, tuple[PhysicalRecord, ...]]:
    publication = _source(
        rows=tuple(
            SourceAlignedRow(
                fields=(SourceAlignedField("amount", Decimal(index)),),
                source_identifiers=(SourceIdentifier("amc.asin", f"B0PROBE{index:03}"),),
                measurement=BUCKET,
            )
            for index in range(60)
        )
    )
    table = CATALOG.source_for_interpreter(publication.rowset.interpreter.value)
    return table, source_records(publication, table)


def test_a_partition_past_the_old_parameter_cap_is_one_merge_under_the_byte_bound() -> None:
    """61 records carried well over 900 parameters, which used to force several commits."""
    table, records = _sixty_row_partition()
    _, parameters = merge_statement(table, records)
    assert len(parameters) > 900
    assert merge_batches(table, records, _FITS) == (records,)


def test_large_partitions_split_under_the_byte_bound_without_losing_rows() -> None:
    table, records = _sixty_row_partition()
    one_record = merge_statement(table, records[:1])
    single_bytes = len(
        json.dumps({"QueryString": one_record[0], "ExecutionParameters": one_record[1]}).encode()
    )
    fits = envelope_fits("wg", "source", 4 * single_bytes)  # a few records per merge
    batches = merge_batches(table, records, fits)

    assert len(batches) > 1
    assert tuple(record for batch in batches for record in batch) == records
    for batch in batches:
        assert fits(*merge_statement(table, batch))


def test_one_record_beyond_the_byte_bound_raises_rather_than_truncating() -> None:
    table, records = _sixty_row_partition()
    with pytest.raises(ValueError, match="statement byte bound"):
        merge_batches(table, records[:1], envelope_fits("wg", "source", 64))


def test_empty_canonical_materialization_requires_withheld_only_evidence() -> None:
    request = _canonical()
    with pytest.raises(ValueError, match="requires withheld-only"):
        CanonicalMaterializeRequest(
            request.destination,
            request.publication,
            (),
            request.rule_set_version,
        )


def test_withheld_only_canonical_materialization_projects_no_business_row() -> None:
    request = _canonical(withheld_only=True)
    table = CATALOG.canonical("fct_retail_purchase_amc", successful=False)

    assert request.rows == ()
    assert request.publication.evidence.withheld_only
    assert request.publication.content_checksum == canonical_rows_checksum(())
    assert canonical_records(request, table) == ()


def test_withheld_only_evidence_refuses_nonempty_canonical_rows() -> None:
    empty = _canonical(withheld_only=True)
    ordinary = _canonical()
    with pytest.raises(ValueError, match="requires empty canonical rows"):
        CanonicalMaterializeRequest(
            empty.destination,
            empty.publication,
            ordinary.rows,
            empty.rule_set_version,
        )


def test_canonical_materialization_binds_the_evidence_rule_set() -> None:
    request = _canonical()
    with pytest.raises(ValueError, match="rule set differs"):
        CanonicalMaterializeRequest(
            request.destination,
            request.publication,
            request.rows,
            "semantic-v2.other",
        )


def test_canonical_materialization_binds_rows_to_the_publication_checksum() -> None:
    request = _canonical()
    with pytest.raises(ValueError, match="rows differ"):
        CanonicalMaterializeRequest(
            request.destination,
            request.publication,
            request.rows + request.rows,
            request.rule_set_version,
        )


def test_replicated_attestation_uses_read_only_authority_then_projection() -> None:
    source = _source(rows=())
    canonical = _canonical()

    class SourceAuthority:
        def publication(self, reference: SourcePublicationRef) -> SourceAlignedPublication | None:
            return source if reference == source.reference else None

    class SourceProjection:
        def attest(self, publication: SourceAlignedPublication) -> MaterializeSourceRowsetResult:
            return MaterializeSourceRowsetResult(publication == source, False)

    class CanonicalAuthority:
        def attest(self, request: CanonicalMaterializeRequest) -> CanonicalWriteResult:
            return CanonicalWriteResult(
                request == canonical,
                False,
                len(request.rows),
                request.publication.content_checksum,
                "" if request == canonical else "canonical mismatch",
            )

    class CanonicalProjection(CanonicalAuthority):
        pass

    source_storage = ReplicatedSourceAlignedStorage(
        cast("S3SourceAlignedAdapter", SourceAuthority()),
        cast("AthenaSourceMaterializer", SourceProjection()),
    )
    canonical_storage = ReplicatedCanonicalStorage(
        cast("S3CanonicalAdapter", CanonicalAuthority()),
        cast("AthenaCanonicalMaterializer", CanonicalProjection()),
    )

    source_result = source_storage.attest(source)
    canonical_result = canonical_storage.attest(canonical)

    assert source_result.accepted and not source_result.created
    assert canonical_result.accepted and not canonical_result.created


@dataclass
class _RecordedStatementSpan:
    name: str
    attributes: dict[str, object]

    def record(self, **attributes: object) -> None:
        self.attributes.update(attributes)


def test_projection_statement_spans_record_cost_without_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[_RecordedStatementSpan] = []
    executions: list[str] = []

    @contextmanager
    def record_span(name: str, **attributes: object) -> Generator[_RecordedStatementSpan]:
        opened = _RecordedStatementSpan(name, dict(attributes))
        recorded.append(opened)
        yield opened

    def start_statement(_client: object, _request: object) -> str:
        execution = f"query-{len(executions) + 1}"
        executions.append(execution)
        return execution

    def await_statement(_client: object, _execution: str, *, limit: float) -> StatementOutcome:
        assert limit > 0
        return StatementOutcome(
            bytes_scanned=2048,
            reused_previous_result=False,
            engine_millis=11,
            planning_millis=3,
            queue_millis=2,
            total_millis=19,
        )

    def no_rows(_client: object, _execution: str) -> list[list[str | None]]:
        return []

    monkeypatch.setattr(semantic_module, "span", record_span)
    monkeypatch.setattr(semantic_module, "start", start_statement)
    monkeypatch.setattr(semantic_module, "await_completion", await_statement)
    monkeypatch.setattr(semantic_module, "fetch_rows", no_rows)

    materializer = AthenaSourceMaterializer(
        AthenaSemanticTarget("batch", "source", "candidate", "current"),
        CATALOG,
        client=cast("AthenaClient", object()),
    )
    result = materializer.materialize(_source(rows=()))

    assert result.accepted is False
    assert [item.attributes["operation_kind"] for item in recorded] == [
        "select",
        "merge",
        "select",
    ]
    assert [item.attributes["query_id"] for item in recorded] == executions
    for item in recorded:
        assert item.name == "athena.statement"
        assert item.attributes["logical_table"] == "sa_amc_retail_purchases"
        assert item.attributes["bytes_scanned"] == 2048
        assert item.attributes["total_millis"] == 19
        assert "sql" not in item.attributes
        assert "parameters" not in item.attributes
