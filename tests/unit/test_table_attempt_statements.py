# SPDX-License-Identifier: MIT-0
"""ADR-075 §5/§6 canonical start envelopes and per-unit statement composition."""

from __future__ import annotations

import pytest

from adapters.outbound.athena.semantic_storage import (
    PhysicalRecord,
    UnitWorkload,
    merge_statement,
    multi_select_statement,
    ordered_records,
    pack_object_batches,
)
from adapters.outbound.athena.start_envelope import envelope_fits
from adapters.outbound.athena.table_attempt_statements import (
    ENVELOPE_MALFORMED,
    STATEMENT_UNPACKABLE,
    ComposedStatement,
    ComposeStartEnvelopeRequest,
    ComposeUnitStatementsRequest,
    compose_start_envelope,
    compose_unit_statements,
    parse_start_envelope,
)
from adapters.outbound.v2_storage_catalog import V2Column, V2Table
from domain.operations.statement_protocol import (
    MAX_STATEMENT_BYTES,
    AcceptedStatementStart,
    LocalStatementRefusal,
    StatementKind,
    StatementRefusalReason,
    StatementStartBounds,
    ValidateStatementStartRequest,
    validate_statement_start,
)
from domain.shared_kernel.errors import ValidationError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

# Every identity below is fictional fixture data; none is copied from a live system.
WORKGROUP = "wg-fixture-batch"
DATABASE = "db_fixture"

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


# --- §6 unit statement composition ----------------------------------------------------------------


def _workload() -> UnitWorkload:
    return UnitWorkload(
        table=TABLE,
        database=DATABASE,
        records_by_identity=(
            ("pub-1", (_record("pub-1", "r1", 5), _record("pub-1", "r2", None))),
            ("pub-2", (_record("pub-2", "r1", 9),)),
        ),
    )


def test_a_unit_composes_its_packed_merges_followed_by_exactly_one_verification() -> None:
    composition = compose_unit_statements(
        ComposeUnitStatementsRequest(workload=_workload(), workgroup=WORKGROUP)
    )
    kinds = tuple(statement.kind for statement in composition.statements)
    assert kinds[:-1] == (StatementKind.MERGE,) * composition.merge_count
    assert kinds[-1] is StatementKind.VERIFICATION
    assert composition.merge_count >= 1
    assert composition.verification is composition.statements[-1]


def test_each_merge_matches_the_production_packer_and_renderer_byte_for_byte() -> None:
    """Feasibility and runtime must count the same statements, so no second packer may exist."""
    workload = _workload()
    composition = compose_unit_statements(
        ComposeUnitStatementsRequest(workload=workload, workgroup=WORKGROUP)
    )
    objects = tuple(ordered_records(TABLE, records) for _, records in workload.records_by_identity)
    expected_batches = pack_object_batches(
        TABLE, objects, envelope_fits(WORKGROUP, DATABASE, MAX_STATEMENT_BYTES)
    )
    assert composition.merge_count == len(expected_batches)
    for statement, batch in zip(composition.merges, expected_batches, strict=True):
        expected_sql, expected_parameters = merge_statement(TABLE, batch)
        assert statement.sql.encode("utf-8") == expected_sql.encode("utf-8")
        assert statement.parameters == expected_parameters


def test_the_verification_reads_back_every_declared_identity_in_one_statement() -> None:
    composition = compose_unit_statements(
        ComposeUnitStatementsRequest(workload=_workload(), workgroup=WORKGROUP)
    )
    verification = composition.verification
    expected_sql = multi_select_statement(TABLE, 2)
    assert verification.sql.encode("utf-8") == expected_sql.encode("utf-8")
    assert verification.parameters == ("pub-1", "pub-2")


def test_every_composed_statement_carries_its_own_canonical_envelope() -> None:
    composition = compose_unit_statements(
        ComposeUnitStatementsRequest(workload=_workload(), workgroup=WORKGROUP)
    )
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


def test_a_single_record_beyond_the_statement_ceilings_refuses_before_authority() -> None:
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
    oversized: PhysicalRecord = {
        "publication_id": "pub-1",
        "row_key": "r1",
        "payload": "x" * 250_000,
    }
    workload = UnitWorkload(
        table=wide_table,
        database=DATABASE,
        records_by_identity=(("pub-1", (oversized,)),),
    )
    with pytest.raises(ValidationError) as caught:
        compose_unit_statements(
            ComposeUnitStatementsRequest(workload=workload, workgroup=WORKGROUP)
        )
    assert caught.value.code == STATEMENT_UNPACKABLE


def test_composition_requires_a_nonblank_workgroup() -> None:
    with pytest.raises(ValidationError):
        ComposeUnitStatementsRequest(workload=_workload(), workgroup="  ")


@pytest.mark.parametrize("bound", [0, -1, True])
def test_composition_requires_a_positive_integer_byte_bound(bound: object) -> None:
    with pytest.raises(ValidationError) as caught:
        ComposeUnitStatementsRequest(
            workload=_workload(),
            workgroup=WORKGROUP,
            max_statement_bytes=bound,  # type: ignore[arg-type]
        )
    assert caught.value.code == ENVELOPE_MALFORMED


# --- §11 2026-10-10: one byte bound, no parameter-count split -------------------------------------


def _wide_unit(identities: int, rows_per_identity: int) -> UnitWorkload:
    """Build a unit far past the removed 900-parameter cap (3 parameters per record)."""
    return UnitWorkload(
        table=TABLE,
        database=DATABASE,
        records_by_identity=tuple(
            (
                f"pub-{identity}",
                tuple(
                    _record(f"pub-{identity}", f"r{row:04}", row)
                    for row in range(rows_per_identity)
                ),
            )
            for identity in range(identities)
        ),
    )


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


def test_a_unit_past_the_old_900_parameter_cap_composes_exactly_one_merge() -> None:
    """Four 100-record objects are 1200 parameters: the old cap made this two Iceberg commits."""
    composition = compose_unit_statements(
        ComposeUnitStatementsRequest(workload=_wide_unit(4, 100), workgroup=WORKGROUP)
    )
    assert composition.merge_count == 1
    (merge,) = composition.merges
    assert len(merge.parameters) == 1_200
    assert len(merge.start_envelope.encode("utf-8")) <= MAX_STATEMENT_BYTES
    # The shared loop's validator accepts exactly what the packer composed.
    assert isinstance(_validated(merge, MAX_STATEMENT_BYTES), AcceptedStatementStart)


def test_the_byte_bound_is_exact_on_the_canonical_envelope() -> None:
    """At the one-merge envelope's exact size one merge fits; one byte less splits it."""
    workload = _wide_unit(4, 100)
    single = compose_unit_statements(
        ComposeUnitStatementsRequest(workload=workload, workgroup=WORKGROUP)
    )
    exact = len(single.merges[0].start_envelope.encode("utf-8"))
    at_bound = compose_unit_statements(
        ComposeUnitStatementsRequest(
            workload=workload, workgroup=WORKGROUP, max_statement_bytes=exact
        )
    )
    assert at_bound.merge_count == 1
    below = compose_unit_statements(
        ComposeUnitStatementsRequest(
            workload=workload, workgroup=WORKGROUP, max_statement_bytes=exact - 1
        )
    )
    assert below.merge_count == 2
    # Whole objects only: the split falls on an object boundary, and no record is lost.
    assert sum(len(merge.parameters) for merge in below.merges) == 1_200
    for merge in below.merges:
        assert len(merge.start_envelope.encode("utf-8")) <= exact - 1
        assert isinstance(_validated(merge, exact - 1), AcceptedStatementStart)
    # The validator applies the same measure: the one-merge envelope refuses one byte below.
    refused = _validated(single.merges[0], exact - 1)
    assert isinstance(refused, LocalStatementRefusal)
    assert refused.reason is StatementRefusalReason.START_ENVELOPE


def test_parameter_bytes_count_toward_the_bound_and_refuse_loudly() -> None:
    """SQL alone would fit; the encoded parameters push the one record's merge past the bound."""
    record = _record("pub-1", "k" * 400, 1)
    workload = UnitWorkload(
        table=TABLE, database=DATABASE, records_by_identity=(("pub-1", (record,)),)
    )
    sql, _ = merge_statement(TABLE, (record,))
    bound = len(sql.encode("utf-8")) + 200
    with pytest.raises(ValidationError) as caught:
        compose_unit_statements(
            ComposeUnitStatementsRequest(
                workload=workload, workgroup=WORKGROUP, max_statement_bytes=bound
            )
        )
    assert caught.value.code == STATEMENT_UNPACKABLE
    assert caught.value.context["max_statement_bytes"] == bound
    # Positive control: the same record composes once the bound covers its whole envelope.
    roomy = compose_unit_statements(
        ComposeUnitStatementsRequest(
            workload=workload, workgroup=WORKGROUP, max_statement_bytes=bound + 1_000
        )
    )
    assert roomy.merge_count == 1


# --- Slice 7 (M4, AR5-2): each composed MERGE's rows and partitions ---------------------------


def test_slice7_merge_shapes_are_the_composers_own_batches() -> None:
    from adapters.outbound.athena.merge_shapes import (  # noqa: PLC0415
        MergeShape,
        merge_batches_of,
        merge_shapes,
    )

    request = ComposeUnitStatementsRequest(workload=_workload(), workgroup=WORKGROUP)
    composition = compose_unit_statements(request)
    batches = merge_batches_of(request)
    # Each re-rendered batch is the composer's MERGE, so no second packer exists.
    for statement, batch in zip(composition.merges, batches, strict=True):
        assert merge_statement(TABLE, batch) == (statement.sql, statement.parameters)
    # The two-member union packs into one MERGE over both identities' partitions.
    assert merge_shapes(request) == (MergeShape(rows=3, partitions=2),)


def test_slice7_a_one_object_member_reports_one_partition_and_no_verification() -> None:
    from adapters.outbound.athena.merge_shapes import MergeShape, merge_shapes  # noqa: PLC0415

    workload = UnitWorkload(
        table=TABLE,
        database=DATABASE,
        records_by_identity=(("pub-1", (_record("pub-1", "r1", 5),)),),
    )
    request = ComposeUnitStatementsRequest(workload=workload, workgroup=WORKGROUP)
    shapes = merge_shapes(request)
    assert shapes == (MergeShape(rows=1, partitions=1),)
    assert len(shapes) == compose_unit_statements(request).merge_count  # verification uncounted
