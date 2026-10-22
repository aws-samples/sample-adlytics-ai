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
    StagedSource,
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
    staged_merge_statement,
    verification_matches,
    verification_statement,
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
from adapters.outbound.v2_storage_catalog import V2Column, V2Table, load_v2_storage_catalog
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
from ports.outbound.unit_staging import StagedUnit

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
        AthenaSemanticTarget("batch", "source", "candidate", "current", "staging"),
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


# --- ADR-149: the staged MERGE, the set-based verification and its acceptance rule ---------------

_DIGEST = "0123456789abcdef" * 4
_SOURCE = StagedSource("db_staging", _DIGEST)
_PIN_TABLE = V2Table(
    name="fct_pin_rows",
    columns=(
        V2Column(name="publication_id", type="string", nullable=False),
        V2Column(name="segment_id", type="string", nullable=True),
        V2Column(name="amount", type="decimal(38,4)", nullable=True),
        V2Column(name="label", type="string", nullable=False),
    ),
    grain=("publication_id", "segment_id"),
    partition_keys=("publication_id",),
)
_PIN_MERGE = (
    'MERGE INTO "fct_pin_rows" AS target USING (SELECT '
    'CAST("publication_id" AS varchar) AS "publication_id", '
    'CAST("segment_id" AS varchar) AS "segment_id", '
    'CAST("amount" AS decimal(38,4)) AS "amount", '
    'CAST("label" AS varchar) AS "label" '
    'FROM "db_staging"."fct_pin_rows" WHERE "staging_key" = '
    "'0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef') AS incoming "
    'ON target."publication_id" = incoming."publication_id" AND '
    '(target."segment_id" = incoming."segment_id" OR '
    '(target."segment_id" IS NULL AND incoming."segment_id" IS NULL)) '
    'WHEN NOT MATCHED THEN INSERT ("publication_id", "segment_id", "amount", "label") '
    'VALUES (incoming."publication_id", incoming."segment_id", incoming."amount", '
    'incoming."label")'
)
_PIN_VERIFICATION = (
    'WITH s AS (SELECT CAST("stg_ordinal" AS bigint) AS "stg_ordinal", '
    'CAST("publication_id" AS varchar) AS "publication_id", '
    'CAST("segment_id" AS varchar) AS "segment_id", '
    'CAST("amount" AS decimal(38,4)) AS "amount", '
    'CAST("label" AS varchar) AS "label" '
    'FROM "db_staging"."fct_pin_rows" WHERE "staging_key" = '
    "'0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'), "
    't AS (SELECT "publication_id", "segment_id", "amount", "label" FROM "fct_pin_rows" '
    'WHERE "publication_id" IN (CAST(? AS varchar), CAST(? AS varchar))), '
    'm AS (SELECT t."publication_id" AS identity, s."stg_ordinal" AS ordinal FROM t JOIN s '
    'ON t."publication_id" = s."publication_id" AND '
    '(t."segment_id" = s."segment_id" OR (t."segment_id" IS NULL AND s."segment_id" IS NULL)) '
    'WHERE (t."amount" = s."amount" OR (t."amount" IS NULL AND s."amount" IS NULL)) '
    'AND t."label" = s."label") '
    "SELECT 'target' AS side, \"publication_id\" AS identity, count(*) AS n FROM t GROUP BY 2 "
    "UNION ALL SELECT 'staged', \"publication_id\", count(*) FROM s GROUP BY 2 "
    "UNION ALL SELECT 'matched_target', identity, count(*) FROM m GROUP BY 2 "
    "UNION ALL SELECT 'matched_staged', identity, count(DISTINCT ordinal) FROM m GROUP BY 2"
)


@pytest.mark.covers("ADR-149")
def test_t6_the_staged_merge_and_verification_are_pinned_exactly() -> None:
    assert staged_merge_statement(_PIN_TABLE, _SOURCE) == _PIN_MERGE
    assert verification_statement(_PIN_TABLE, _SOURCE, 2) == _PIN_VERIFICATION
    # Control: rendering the non-nullable `label` null-safe fails the pin.
    loosened = V2Table(
        name=_PIN_TABLE.name,
        columns=(*_PIN_TABLE.columns[:3], V2Column(name="label", type="string", nullable=True)),
        grain=_PIN_TABLE.grain,
        partition_keys=_PIN_TABLE.partition_keys,
    )
    assert verification_statement(loosened, _SOURCE, 2) != _PIN_VERIFICATION
    assert staged_merge_statement(loosened, _SOURCE) == _PIN_MERGE  # label not grain


def _null_safe(left: str, right: str, name: str) -> str:
    equal = f'{left}."{name}" = {right}."{name}"'
    return f'({equal} OR ({left}."{name}" IS NULL AND {right}."{name}" IS NULL))'


@pytest.mark.covers("ADR-149")
@pytest.mark.parametrize("name", ["fct_segment_profile", "fct_retail_sales"])
def test_t6_catalog_tables_compare_nullable_columns_equal_or_both_null(name: str) -> None:
    table = next(table for table in CATALOG.candidate_by_dataset.values() if table.name == name)
    merge = staged_merge_statement(table, _SOURCE)
    verification = verification_statement(table, _SOURCE, 3)
    columns = {column.name: column for column in table.columns}
    for column_name in table.grain:
        if columns[column_name].nullable:
            assert _null_safe("target", "incoming", column_name) in merge
            assert _null_safe("t", "s", column_name) in verification
        else:
            assert f'target."{column_name}" = incoming."{column_name}"' in merge
            assert f'(target."{column_name}" = incoming."{column_name}" OR' not in merge
            assert f'(t."{column_name}" = s."{column_name}" OR' not in verification
    for column in table.columns:
        if column.name in table.grain:
            continue
        if column.nullable:
            assert _null_safe("t", "s", column.name) in verification
        else:
            assert f't."{column.name}" = s."{column.name}"' in verification
            assert f'(t."{column.name}" = s."{column.name}" OR' not in verification
    if name == "fct_segment_profile":
        assert columns["segment_id"].nullable  # the nullable grain this case exists for
    # Control: no statement uses IS NOT DISTINCT FROM (ING-VERIFY-NULLSAFE).
    assert "IS NOT DISTINCT FROM" not in merge
    assert "IS NOT DISTINCT FROM" not in verification
    assert "DELETE" not in merge
    assert "UPDATE" not in merge


@pytest.mark.covers("ADR-149")
def test_t6_no_catalog_statement_uses_is_not_distinct_from() -> None:
    tables = (
        *CATALOG.source_by_contract.values(),
        *CATALOG.candidate_by_dataset.values(),
        *CATALOG.current_by_dataset.values(),
    )
    for table in tables:
        for sql in (
            staged_merge_statement(table, _SOURCE),
            verification_statement(table, _SOURCE, 1),
        ):
            assert "IS NOT DISTINCT FROM" not in sql


@pytest.mark.covers("ADR-149")
@pytest.mark.parametrize(
    ("database", "key"),
    [("db-staging", _DIGEST), ("db_staging", _DIGEST.upper()), ("db_staging", "x' OR 1=1 --")],
)
def test_staged_statements_refuse_an_unsafe_database_or_key(database: str, key: str) -> None:
    with pytest.raises(ValueError, match=r"unsafe staging database|64 lowercase hex"):
        StagedSource(database, key)


def _staged(*counts: tuple[str, int]) -> StagedUnit:
    return StagedUnit(table="fct_pin_rows", staging_key=_DIGEST, byte_length=10, row_counts=counts)


def _rows(counts: dict[str, int], **sides: dict[str, int]) -> tuple[tuple[str | None, ...], ...]:
    names = ("target", "staged", "matched_target", "matched_staged")
    return tuple(
        (side, identity, str(count))
        for side in names
        for identity, count in sides.get(side, counts).items()
    )


@pytest.mark.covers("ADR-149")
def test_t5_verification_accepts_exactly_the_staged_counts() -> None:
    staged = _staged(("pub-a", 2), ("pub-b", 1))
    exact = {"pub-a": 2, "pub-b": 1}
    assert verification_matches(staged, _rows(exact)) is True
    # Row order does not matter.
    assert verification_matches(staged, tuple(reversed(_rows(exact)))) is True


@pytest.mark.covers("ADR-149")
@pytest.mark.parametrize(
    "sides",
    [
        # A missing target row: the target and matched counts fall short.
        {"target": {"pub-a": 1, "pub-b": 1}, "matched_target": {"pub-a": 1, "pub-b": 1}},
        # An extra target row with no staged twin.
        {"target": {"pub-a": 3, "pub-b": 1}},
        # A duplicate target row matches the same staged row twice.
        {"target": {"pub-a": 3, "pub-b": 1}, "matched_target": {"pub-a": 3, "pub-b": 1}},
        # An altered value: the row is counted but matches nothing.
        {"matched_target": {"pub-a": 1, "pub-b": 1}, "matched_staged": {"pub-a": 1, "pub-b": 1}},
        # A row under the wrong identity.
        {"target": {"pub-a": 1, "pub-b": 2}, "matched_target": {"pub-a": 1, "pub-b": 1}},
        # An empty or missing staged object: no staged or matched side at all.
        {"staged": {}, "matched_target": {}, "matched_staged": {}},
    ],
)
def test_t5_verification_refuses_every_differing_count(sides: dict[str, dict[str, int]]) -> None:
    staged = _staged(("pub-a", 2), ("pub-b", 1))
    assert verification_matches(staged, _rows({"pub-a": 2, "pub-b": 1}, **sides)) is False


@pytest.mark.covers("ADR-149")
@pytest.mark.parametrize(
    "extra",
    [
        ("target", "pub-a"),
        ("target", "pub-a", "2", "x"),
        ("unknown", "pub-a", "2"),
        ("target", "pub-z", "2"),
        ("target", "pub-a", None),
        ("target", "pub-a", "-1"),
        ("target", "pub-a", "2.0"),
        ("target", "pub-a", "2"),
    ],
)
def test_t5_verification_refuses_a_malformed_or_unknown_row(extra: tuple[str | None, ...]) -> None:
    staged = _staged(("pub-a", 2))
    rows = _rows({"pub-a": 2})
    assert verification_matches(staged, rows) is True
    assert verification_matches(staged, (*rows, extra)) is False
