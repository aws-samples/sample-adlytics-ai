# SPDX-License-Identifier: MIT-0
"""ADR-075 §5/§6 canonical start envelopes and ADR-149 per-unit statement composition."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from adapters.outbound.athena.merge_shapes import MergeShape, merge_shapes
from adapters.outbound.athena.semantic_storage import (
    PhysicalRecord,
    PhysicalValue,
    StagedSource,
    UnitWorkload,
    staged_merge_statement,
    verification_statement,
)
from adapters.outbound.athena.staging import iter_staged_lines, render_staged_body, stage_unit
from adapters.outbound.athena.start_envelope import (
    ComposeStartEnvelopeRequest,
    compose_start_envelope,
)
from adapters.outbound.athena.table_attempt_statements import (
    ENVELOPE_MALFORMED,
    STATEMENT_UNPACKABLE,
    ComposedStatement,
    ComposeUnitStatementsRequest,
    UnitStatementComposition,
    compose_unit_statements,
    parse_start_envelope,
)
from adapters.outbound.v2_storage_catalog import V2Column, V2Table, load_v2_storage_catalog
from domain.operations.projection_feasibility import (
    FeasibleProjection,
    ProjectionFeasibilityRequest,
    UnitMergeMeasurement,
    validate_projection_feasibility,
)
from domain.operations.statement_protocol import (
    MAX_STATEMENT_BYTES,
    AcceptedStatementStart,
    LocalStatementRefusal,
    StatementKind,
    StatementRefusalReason,
    StatementStartBounds,
    ValidateStatementStartRequest,
    unit_statement_kinds,
    validate_statement_start,
)
from domain.operations.write_plan import CanonicalObjectEntry, CanonicalUnitEntry
from domain.shared_kernel.errors import InvariantError, ValidationError
from ports.outbound.unit_staging import STAGING_COMPOSITION_SHAPE

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075", "ADR-149")]

ROOT = Path(__file__).resolve().parents[2]

# Every identity below is fictional fixture data; none is copied from a live system.
WORKGROUP = "wg-fixture-batch"
DATABASE = "db_fixture"
STAGING_DATABASE = "db_fixture_staging"

TABLE = V2Table(
    name="fact_semantic_rows",
    columns=(
        V2Column(name="publication_id", type="string", nullable=False),
        V2Column(name="row_key", type="string", nullable=False),
        V2Column(name="amount", type="int", nullable=True),
    ),
    grain=("publication_id", "row_key"),
    partition_keys=("publication_id",),
)


def _record(identity: str, row_key: str, amount: int | None) -> PhysicalRecord:
    return {"publication_id": identity, "row_key": row_key, "amount": amount}


# --- Canonical start envelope ---------------------------------------------------------------------


def test_the_parameterized_envelope_reproduces_its_pinned_golden_bytes() -> None:
    """The envelope digest hashes these exact UTF-8 bytes, so the rendering is load-bearing."""
    envelope = compose_start_envelope(
        ComposeStartEnvelopeRequest(
            workgroup=WORKGROUP,
            database=DATABASE,
            sql="SELECT 1",
            parameters=("a b", "it's"),
        )
    )
    assert envelope == (
        '{"Database":"db_fixture",'
        "\"ExecutionParameters\":[\"'a b'\",\"'it''s'\"],"
        '"QueryString":"SELECT 1",'
        '"WorkGroup":"wg-fixture-batch"}'
    )


def test_a_parameterless_envelope_omits_the_execution_parameters_key_entirely() -> None:
    """Athena rejects an empty ExecutionParameters list rather than treating it as none."""
    envelope = compose_start_envelope(
        ComposeStartEnvelopeRequest(
            workgroup=WORKGROUP, database=DATABASE, sql="SELECT 1", parameters=()
        )
    )
    assert envelope == (
        '{"Database":"db_fixture","QueryString":"SELECT 1","WorkGroup":"wg-fixture-batch"}'
    )


def test_parse_round_trips_the_composed_envelope_byte_identically() -> None:
    envelope = compose_start_envelope(
        ComposeStartEnvelopeRequest(
            workgroup=WORKGROUP, database=DATABASE, sql="SELECT 1", parameters=("a",)
        )
    )
    parsed = parse_start_envelope(envelope)
    assert parsed.workgroup == WORKGROUP
    assert parsed.database == DATABASE
    assert parsed.query_string == "SELECT 1"
    assert parsed.execution_parameters == ("'a'",)


@pytest.mark.parametrize(
    "text",
    [
        "not-json",
        "[]",
        '{"Database":"d","QueryString":"q"}',
        '{"Database":"d","QueryString":"q","WorkGroup":"w","Extra":"x"}',
        '{"Database":"","QueryString":"q","WorkGroup":"w"}',
        '{"Database":"d","ExecutionParameters":[],"QueryString":"q","WorkGroup":"w"}',
        '{"Database":"d","ExecutionParameters":[7],"QueryString":"q","WorkGroup":"w"}',
        '{"Database": "d","QueryString":"q","WorkGroup":"w"}',
        '{"WorkGroup":"w","Database":"d","QueryString":"q"}',
    ],
)
def test_every_envelope_outside_the_exact_canonical_form_is_refused(text: str) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_start_envelope(text)
    assert caught.value.code == ENVELOPE_MALFORMED


def test_compose_refuses_a_blank_workgroup_database_or_sql() -> None:
    with pytest.raises(ValidationError):
        ComposeStartEnvelopeRequest(workgroup=" ", database=DATABASE, sql="q", parameters=())
    with pytest.raises(ValidationError):
        ComposeStartEnvelopeRequest(workgroup=WORKGROUP, database=DATABASE, sql=" ", parameters=())


# --- §6 unit statement composition (ADR-149: one staged MERGE, one verification) ----------------


def _workload() -> UnitWorkload:
    return UnitWorkload(
        table=TABLE,
        database=DATABASE,
        records_by_identity=(
            ("pub-1", (_record("pub-1", "r1", 5), _record("pub-1", "r2", None))),
            ("pub-2", (_record("pub-2", "r1", 9),)),
        ),
    )


def _request(workload: UnitWorkload, **bound: int) -> ComposeUnitStatementsRequest:
    return ComposeUnitStatementsRequest(
        workload=workload, workgroup=WORKGROUP, staging_database=STAGING_DATABASE, **bound
    )


def test_a_unit_composes_its_packed_merges_followed_by_exactly_one_verification() -> None:
    composition = compose_unit_statements(_request(_workload()))
    kinds = tuple(statement.kind for statement in composition.statements)
    assert kinds[:-1] == (StatementKind.MERGE,) * composition.merge_count
    assert kinds[-1] is StatementKind.VERIFICATION
    assert composition.merge_count == 1
    assert composition.verification is composition.statements[-1]


def test_each_merge_matches_the_production_packer_and_renderer_byte_for_byte() -> None:
    """Feasibility and runtime compose the same statements: the MERGE is the staged renderer's."""
    workload = _workload()
    composition = compose_unit_statements(_request(workload))
    (merge,) = composition.merges
    staged = stage_unit(workload, max_record_bytes=MAX_STATEMENT_BYTES)
    expected_sql = staged_merge_statement(TABLE, StagedSource(STAGING_DATABASE, staged.staging_key))
    assert merge.sql.encode("utf-8") == expected_sql.encode("utf-8")
    assert merge.parameters == ()
    assert merge.staged == staged
    assert merge.render is not None
    assert merge.render() == render_staged_body(workload, staged)


def test_the_verification_reads_back_every_declared_identity_in_one_statement() -> None:
    workload = _workload()
    composition = compose_unit_statements(_request(workload))
    verification = composition.verification
    key = stage_unit(workload, max_record_bytes=MAX_STATEMENT_BYTES).staging_key
    expected_sql = verification_statement(TABLE, StagedSource(STAGING_DATABASE, key), 2)
    assert verification.sql.encode("utf-8") == expected_sql.encode("utf-8")
    assert verification.parameters == ("pub-1", "pub-2")
    assert verification.staged is None
    assert verification.render is None


def test_every_composed_statement_carries_its_own_canonical_envelope() -> None:
    composition = compose_unit_statements(_request(_workload()))
    for statement in composition.statements:
        assert isinstance(statement, ComposedStatement)
        expected = compose_start_envelope(
            ComposeStartEnvelopeRequest(
                workgroup=WORKGROUP,
                database=DATABASE,
                sql=statement.sql,
                parameters=statement.parameters,
            )
        )
        assert statement.start_envelope == expected
        parsed = parse_start_envelope(statement.start_envelope)
        assert parsed.query_string == statement.sql
    # The parameterless MERGE envelope omits ExecutionParameters entirely.
    assert "ExecutionParameters" not in composition.merges[0].start_envelope


def _payload_workload(payload: str) -> tuple[V2Table, UnitWorkload]:
    wide_table = V2Table(
        name="fact_semantic_rows",
        columns=(
            V2Column(name="publication_id", type="string", nullable=False),
            V2Column(name="row_key", type="string", nullable=False),
            V2Column(name="payload", type="string", nullable=True),
        ),
        grain=("publication_id", "row_key"),
        partition_keys=("publication_id",),
    )
    record: PhysicalRecord = {"publication_id": "pub-1", "row_key": "r1", "payload": payload}
    return wide_table, UnitWorkload(
        table=wide_table, database=DATABASE, records_by_identity=(("pub-1", (record,)),)
    )


def test_a_single_record_beyond_the_statement_ceilings_refuses_before_authority() -> None:
    _, workload = _payload_workload("x" * 250_000)
    with pytest.raises(ValidationError) as caught:
        compose_unit_statements(_request(workload))
    assert caught.value.code == STATEMENT_UNPACKABLE
    assert caught.value.context["max_statement_bytes"] == 200_000
    # Boundary control: a bound equal to the record's staged line composes both statements.
    (line,) = tuple(iter_staged_lines(workload))
    at_bound = compose_unit_statements(_request(workload, max_statement_bytes=len(line)))
    assert tuple(statement.kind for statement in at_bound.statements) == (
        StatementKind.MERGE,
        StatementKind.VERIFICATION,
    )
    for statement in at_bound.statements:
        assert len(statement.start_envelope.encode("utf-8")) < len(line)
    # Second control: a small record with a null in a required column can't be staged either.
    unstageable = UnitWorkload(
        table=TABLE,
        database=DATABASE,
        records_by_identity=(("pub-1", ({**_record("pub-1", "r1", 1), "row_key": None},)),),
    )
    with pytest.raises(ValidationError) as null_refusal:
        compose_unit_statements(_request(unstageable))
    assert null_refusal.value.code == STATEMENT_UNPACKABLE
    assert "one record can't be staged" in null_refusal.value.description


def test_composition_requires_a_nonblank_workgroup() -> None:
    with pytest.raises(ValidationError):
        ComposeUnitStatementsRequest(
            workload=_workload(), workgroup="  ", staging_database=STAGING_DATABASE
        )


@pytest.mark.parametrize("database", ["", "  ", "Staging", "db-staging", "1db"])
def test_composition_requires_a_generated_staging_database(database: str) -> None:
    with pytest.raises(ValidationError) as caught:
        ComposeUnitStatementsRequest(
            workload=_workload(), workgroup=WORKGROUP, staging_database=database
        )
    assert caught.value.code == ENVELOPE_MALFORMED


@pytest.mark.parametrize("bound", [0, -1, True])
def test_composition_requires_a_positive_integer_byte_bound(bound: object) -> None:
    with pytest.raises(ValidationError) as caught:
        ComposeUnitStatementsRequest(
            workload=_workload(),
            workgroup=WORKGROUP,
            staging_database=STAGING_DATABASE,
            max_statement_bytes=bound,  # type: ignore[arg-type]
        )
    assert caught.value.code == ENVELOPE_MALFORMED


def test_a_composition_of_any_other_shape_is_an_invariant() -> None:
    merge, verification = compose_unit_statements(_request(_workload())).statements
    bare_merge = dataclasses.replace(merge, staged=None, render=None)
    staged_verification = dataclasses.replace(verification, staged=merge.staged)
    for statements in (
        (merge,),
        (merge, merge, verification),
        (verification, merge),
        (bare_merge, verification),
        (merge, staged_verification),
    ):
        with pytest.raises(InvariantError) as caught:
            UnitStatementComposition(statements=statements)
        assert caught.value.code == STAGING_COMPOSITION_SHAPE
    # Control: the composer's own shape constructs.
    assert UnitStatementComposition(statements=(merge, verification)).merge_count == 1


# --- §11 the step-2 validator owns the envelope bound ------------------------------------------


def _validated(statement: ComposedStatement, bound: int) -> object:
    """Run the §5 step-2 validator over one composed statement, as the shared loop does."""
    return validate_statement_start(
        ValidateStatementStartRequest(
            statement_kind=statement.kind.value,
            owner_kind="queued_unit",
            owner_ref="WRITE_PLAN#" + "b" * 64 + "#" + "c" * 64,
            physical_table_id="adlytics_dev.fact_semantic_rows",
            lease_holder="ab01" * 16,
            fencing_token=7,
            ordinal=1,
            start_envelope=statement.start_envelope,
            rendered_sql=statement.sql,
            parameters=statement.parameters,
            bounds=StatementStartBounds(max_statement_bytes=bound),
        )
    )


def test_parameter_bytes_count_toward_the_bound_and_refuse_loudly() -> None:
    """The verification's identity parameter counts; the validator, not the composer, refuses."""
    record = _record("pub-1", "k" * 400, 1)
    workload = UnitWorkload(
        table=TABLE, database=DATABASE, records_by_identity=(("pub-1", (record,)),)
    )
    composition = compose_unit_statements(_request(workload))
    verification = composition.verification
    n = len(verification.start_envelope.encode("utf-8"))
    assert isinstance(_validated(verification, n), AcceptedStatementStart)
    refused = _validated(verification, n - 1)
    assert isinstance(refused, LocalStatementRefusal)
    assert refused.reason is StatementRefusalReason.START_ENVELOPE
    # Control: the composer measures no envelope. At a bound one byte under the verification,
    # still above the record's staged line, it composes both statements and raises nothing.
    (line,) = tuple(iter_staged_lines(workload))
    assert len(line) < n - 1
    tight = compose_unit_statements(_request(workload, max_statement_bytes=n - 1))
    assert len(tight.statements) == 2
    assert isinstance(_validated(tight.verification, n - 1), LocalStatementRefusal)


# --- T-1: two statements at any admitted size (the LARGE tier) ---------------------------------

CATALOG = load_v2_storage_catalog(ROOT / "contracts" / "generated" / "v2-tables.json")
_START = date(2000, 1, 1)
_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _value(column: V2Column, row: int) -> PhysicalValue:
    """Return a fictional, row-unique value of the column's catalog type."""
    values: dict[str, PhysicalValue] = {
        "string": f"{column.name[:8]}-{row:07d}",
        "bigint": row,
        "int": row % 100_000,
        "date": _START + timedelta(days=row),
        "timestamp": _AT + timedelta(seconds=row),
        "boolean": row % 2 == 0,
    }
    if column.type.startswith("decimal"):
        return Decimal(row).scaleb(-4)
    return values[column.type]


def _synthetic(table: V2Table, identities: tuple[str, ...], rows: int) -> UnitWorkload:
    partition = table.partition_keys[0]
    return UnitWorkload(
        table=table,
        database=DATABASE,
        records_by_identity=tuple(
            (
                identity,
                tuple(
                    {
                        **{column.name: _value(column, row) for column in table.columns},
                        partition: identity,
                    }
                    for row in range(rows)
                ),
            )
            for identity in identities
        ),
    )


def _table(name: str) -> V2Table:
    tables = (
        *CATALOG.source_by_contract.values(),
        *CATALOG.candidate_by_dataset.values(),
        *CATALOG.current_by_dataset.values(),
    )
    return next(table for table in tables if table.name == name)


def _entry(unit_key: str, table_kind: str, identities: tuple[str, ...]) -> CanonicalUnitEntry:
    return CanonicalUnitEntry(
        unit_key=unit_key,
        table_kind=table_kind,
        merge_count=1,
        objects=tuple(
            CanonicalObjectEntry(
                kind="canonical",
                reference=f"object-{identity}",
                content_checksum="a" * 64,
                publication_id=identity,
                partition_key=None,
                fencing_token=1,
            )
            for identity in sorted(identities)
        ),
    )


def _without_key(sql: str, key: str) -> str:
    assert sql.count(key) == 1
    return sql.replace(key, "<key>")


def test_t1_a_large_tier_delivery_composes_two_statements_per_unit_at_any_row_count() -> None:
    """3,000 active ASINs a day, 30-day chunk: about 270,000 rows across three units (§9).

    Each unit composes exactly one MERGE and one verification (M(u) = 1), and neither SQL text
    nor parameters depend on rows: a one-record unit over the same identities composes the
    same statements apart from the staging key. So the 200,000-byte statement bound and the
    10-per-unit and 32-per-plan attempt caps no longer move with the delivery's rows.
    """
    days = tuple(f"pub-day-{day:02d}" for day in range(30))
    source = _synthetic(_table("sa_vendor_sales_by_asin"), ("src-pub-0001",), 90_001)
    retail = _table("fct_retail_sales")
    candidate = _synthetic(retail, days, 3_000)
    current = _synthetic(retail, days, 3_000)
    units = (
        ("a-source", "source", source),
        ("b-candidate", "candidate", candidate),
        ("c-current", "current", current),
    )
    assert sum(len(r) for _, _, w in units for _, r in w.records_by_identity) == 270_001
    entries: list[CanonicalUnitEntry] = []
    for unit_key, table_kind, workload in units:
        composition = compose_unit_statements(_request(workload))
        assert composition.merge_count == 1
        assert tuple(statement.kind for statement in composition.statements) == (
            StatementKind.MERGE,
            StatementKind.VERIFICATION,
        )
        assert unit_statement_kinds(composition.merge_count) == (
            StatementKind.MERGE,
            StatementKind.VERIFICATION,
        )
        assert merge_shapes(_request(workload)) == (
            MergeShape(
                rows=sum(len(records) for _, records in workload.records_by_identity),
                partitions=len(workload.records_by_identity),
            ),
        )
        for statement in composition.statements:
            assert len(statement.start_envelope.encode("utf-8")) < MAX_STATEMENT_BYTES
            assert isinstance(_validated(statement, MAX_STATEMENT_BYTES), AcceptedStatementStart)
        # Control: one record per identity composes the same SQL apart from the key.
        identities = tuple(identity for identity, _ in workload.records_by_identity)
        small = compose_unit_statements(_request(_synthetic(workload.table, identities, 1)))
        big_key = composition.merges[0].staged
        small_key = small.merges[0].staged
        assert big_key is not None
        assert small_key is not None
        assert big_key.staging_key != small_key.staging_key
        for large, one in zip(composition.statements, small.statements, strict=True):
            assert _without_key(large.sql, big_key.staging_key) == _without_key(
                one.sql, small_key.staging_key
            )
            assert large.parameters == one.parameters
        entries.append(_entry(unit_key, table_kind, identities))
    result = validate_projection_feasibility(
        ProjectionFeasibilityRequest(
            units=tuple(entries),
            measurements=tuple(UnitMergeMeasurement(entry.unit_key, 1) for entry in entries),
        )
    )
    assert isinstance(result, FeasibleProjection)
    assert result.clean_attempts == 6


# --- T-2: envelopes stay under the bound for every catalog table -------------------------------

_LONGEST_ENV = "e" * 24
_LONGEST_WORKGROUP = f"adlytics-{_LONGEST_ENV}-v2-bake"
_LONGEST_DATABASE = f"adlytics_{_LONGEST_ENV}_v2_canonical_candidate"
_LONGEST_STAGING = f"adlytics_{_LONGEST_ENV}_v2_staging"


def _envelope_bytes(sql: str, parameters: tuple[str, ...]) -> int:
    envelope = compose_start_envelope(
        ComposeStartEnvelopeRequest(
            workgroup=_LONGEST_WORKGROUP,
            database=_LONGEST_DATABASE,
            sql=sql,
            parameters=parameters,
        )
    )
    return len(envelope.encode("utf-8"))


def test_t2_both_envelopes_stay_under_the_bound_for_every_catalog_table() -> None:
    source = StagedSource(_LONGEST_STAGING, "f" * 64)
    tables = {
        table.name: table
        for table in (
            *CATALOG.source_by_contract.values(),
            *CATALOG.candidate_by_dataset.values(),
            *CATALOG.current_by_dataset.values(),
        )
    }
    assert len(tables) == 54
    unit = tuple(f"{index:03d}" + "i" * 125 for index in range(37))
    union = tuple(f"{index:03d}" + "u" * 125 for index in range(888))
    assert {len(identity.encode("utf-8")) for identity in (*unit, *union)} == {128}
    largest = 0
    for table in tables.values():
        merge = staged_merge_statement(table, source)
        assert _envelope_bytes(merge, ()) < MAX_STATEMENT_BYTES
        verification = verification_statement(table, source, len(unit))
        assert _envelope_bytes(verification, unit) < MAX_STATEMENT_BYTES
        widest = verification_statement(table, source, len(union))
        largest = max(largest, _envelope_bytes(widest, union))
    assert largest < MAX_STATEMENT_BYTES

    # Control: a 200,001-byte envelope is refused by the step-2 validator.
    def statement(width: int) -> ComposedStatement:
        parameters = ("x" * width,)
        return ComposedStatement(
            kind=StatementKind.VERIFICATION,
            sql="SELECT 1",
            parameters=parameters,
            start_envelope=compose_start_envelope(
                ComposeStartEnvelopeRequest(
                    workgroup=WORKGROUP, database=DATABASE, sql="SELECT 1", parameters=parameters
                )
            ),
        )

    probe = len(statement(199_000).start_envelope.encode("utf-8"))
    oversized = statement(199_000 + MAX_STATEMENT_BYTES + 1 - probe)
    assert len(oversized.start_envelope.encode("utf-8")) == MAX_STATEMENT_BYTES + 1
    refused = _validated(oversized, MAX_STATEMENT_BYTES)
    assert isinstance(refused, LocalStatementRefusal)
    assert refused.reason is StatementRefusalReason.START_ENVELOPE


# --- Slice 7 (M4, AR5-2): each composed MERGE's rows and partitions ---------------------------


def test_slice7_a_one_object_member_reports_one_partition_and_no_verification() -> None:
    workload = UnitWorkload(
        table=TABLE,
        database=DATABASE,
        records_by_identity=(("pub-1", (_record("pub-1", "r1", 5),)),),
    )
    request = _request(workload)
    shapes = merge_shapes(request)
    assert shapes == (MergeShape(rows=1, partitions=1),)
    assert len(shapes) == compose_unit_statements(request).merge_count  # verification uncounted
