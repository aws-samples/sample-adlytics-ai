# SPDX-License-Identifier: MIT-0
"""ADR-149 staged objects: deterministic bytes and key, the record bound, and the value golden."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from adapters.outbound.athena.semantic_storage import PhysicalRecord, PhysicalValue, UnitWorkload
from adapters.outbound.athena.staging import (
    _rendered,  # pyright: ignore[reportPrivateUsage]
    iter_staged_lines,
    render_staged_body,
    stage_unit,
)
from adapters.outbound.v2_storage_catalog import V2Column, V2Table, load_v2_storage_catalog
from domain.shared_kernel.errors import InvariantError
from ports.outbound.unit_staging import (
    STAGING_EMPTY_IDENTITY,
    STAGING_RENDER_DIVERGED,
    StagedUnit,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-149")]

GOLDEN = Path(__file__).parent / "golden"
BASE_RENDERER_BLOB = "d5346b39ac87b94a276ec0bbb9d2ffa4a9127c76"
BOUND = 200_000

# Every identity and value below is fictional fixture data.
TABLE = V2Table(
    name="fact_fixture_rows",
    columns=(
        V2Column(name="publication_id", type="string", nullable=False),
        V2Column(name="row_key", type="bigint", nullable=False),
        V2Column(name="label", type="string", nullable=True),
        V2Column(name="amount", type="decimal(38,4)", nullable=True),
        V2Column(name="observed_at", type="timestamp", nullable=True),
    ),
    grain=("publication_id", "row_key"),
    partition_keys=("publication_id",),
)
AT = datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)


def _record(identity: str, row_key: int, label: str | None = "x") -> PhysicalRecord:
    return {
        "publication_id": identity,
        "row_key": row_key,
        "label": label,
        "amount": Decimal("1.5000"),
        "observed_at": AT,
    }


def _workload(*objects: tuple[str, tuple[PhysicalRecord, ...]]) -> UnitWorkload:
    return UnitWorkload(table=TABLE, database="db_fixture", records_by_identity=objects)


FIXTURE = _workload(
    ("pub-a", (_record("pub-a", 2, 'say "hi"\n'), _record("pub-a", 1, None))),
    ("pub-b", (_record("pub-b", 1),)),
)


def test_t3_the_staged_body_and_its_summary_are_exact() -> None:
    body = b"".join(iter_staged_lines(FIXTURE))
    assert body == (
        b'{"publication_id":"pub-a","row_key":"1","label":null,"amount":"1.5000",'
        b'"observed_at":"2024-01-02 03:04:05.123456","stg_ordinal":"0"}\n'
        b'{"publication_id":"pub-a","row_key":"2","label":"say \\"hi\\"\\n","amount":"1.5000",'
        b'"observed_at":"2024-01-02 03:04:05.123456","stg_ordinal":"1"}\n'
        b'{"publication_id":"pub-b","row_key":"1","label":"x","amount":"1.5000",'
        b'"observed_at":"2024-01-02 03:04:05.123456","stg_ordinal":"2"}\n'
    )
    staged = stage_unit(FIXTURE, max_record_bytes=BOUND)
    assert staged == StagedUnit(
        table="fact_fixture_rows",
        staging_key=hashlib.sha256(body).hexdigest(),
        byte_length=len(body),
        row_counts=(("pub-a", 2), ("pub-b", 1)),
    )
    assert staged.rows == 3
    assert render_staged_body(FIXTURE, staged) == body
    # A raw line terminator never appears inside a line: JSON escapes CR and LF.
    assert body.count(b"\n") == 3


def test_t3_the_key_is_deterministic_and_moves_with_any_change() -> None:
    key = stage_unit(FIXTURE, max_record_bytes=BOUND).staging_key
    shuffled = _workload(
        ("pub-a", (_record("pub-a", 1, None), _record("pub-a", 2, 'say "hi"\n'))),
        ("pub-b", (_record("pub-b", 1),)),
    )
    # Records are staged in grain order, so their input order does not move the key.
    assert stage_unit(shuffled, max_record_bytes=BOUND).staging_key == key
    changed_value = _workload(
        ("pub-a", (_record("pub-a", 2, 'say "hi"\n'), _record("pub-a", 1, "y"))),
        ("pub-b", (_record("pub-b", 1),)),
    )
    changed_null = _workload(
        ("pub-a", (_record("pub-a", 2, None), _record("pub-a", 1, None))),
        ("pub-b", (_record("pub-b", 1),)),
    )
    reordered = _workload(
        ("pub-b", (_record("pub-b", 1),)),
        ("pub-a", (_record("pub-a", 2, 'say "hi"\n'), _record("pub-a", 1, None))),
    )
    others = {
        stage_unit(workload, max_record_bytes=BOUND).staging_key
        for workload in (changed_value, changed_null, reordered)
    }
    assert key not in others
    assert len(others) == 3


def test_t3_a_staged_unit_holds_no_body() -> None:
    staged = stage_unit(FIXTURE, max_record_bytes=BOUND)
    assert [field.name for field in dataclasses.fields(staged)] == [
        "table",
        "staging_key",
        "byte_length",
        "row_counts",
    ]
    assert not hasattr(staged, "body")


def test_t3_an_identity_without_records_is_an_invariant_and_one_record_stages() -> None:
    empty = _workload(("pub-a", ()), ("pub-b", (_record("pub-b", 1),)))
    with pytest.raises(InvariantError) as refused:
        stage_unit(empty, max_record_bytes=BOUND)
    assert refused.value.code == STAGING_EMPTY_IDENTITY
    # Control: the same identity with one record stages.
    one = _workload(("pub-a", (_record("pub-a", 1),)), ("pub-b", (_record("pub-b", 1),)))
    assert stage_unit(one, max_record_bytes=BOUND).row_counts == (("pub-a", 1), ("pub-b", 1))


def test_t3_a_line_at_the_record_bound_stages_and_one_byte_more_is_refused() -> None:
    record = _workload(("pub-a", (_record("pub-a", 1, "z" * 1_000),)))
    (line,) = tuple(iter_staged_lines(record))
    assert stage_unit(record, max_record_bytes=len(line)).byte_length == len(line)
    with pytest.raises(ValueError, match="staged line exceeds"):
        stage_unit(record, max_record_bytes=len(line) - 1)


def test_t3_a_divergent_render_is_an_invariant() -> None:
    staged = stage_unit(FIXTURE, max_record_bytes=BOUND)
    other = _workload(("pub-a", (_record("pub-a", 1),)))
    with pytest.raises(InvariantError) as refused:
        render_staged_body(other, staged)
    assert refused.value.code == STAGING_RENDER_DIVERGED


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ({**_record("pub-a", 1), "row_key": None}, "is null"),
        ({**_record("pub-a", 1), "observed_at": datetime(2024, 1, 2)}, "timezone-aware"),  # noqa: DTZ001
        ({**_record("pub-a", 1), "label": "lone \ud800"}, "surrogate"),
    ],
)
def test_t3_an_unrenderable_record_raises_value_error(record: PhysicalRecord, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        stage_unit(_workload(("pub-a", (record,))), max_record_bytes=BOUND)


def test_t3_a_repeated_grain_raises_value_error() -> None:
    repeated = _workload(("pub-a", (_record("pub-a", 1), _record("pub-a", 1, "y"))))
    with pytest.raises(ValueError, match="repeat"):
        stage_unit(repeated, max_record_bytes=BOUND)


def _decoded(tagged: dict[str, Any]) -> PhysicalValue:
    tag = tagged["t"]
    if tag == "null":
        return None
    decoders: dict[str, Callable[[Any], PhysicalValue]] = {
        "str": str,
        "int": int,
        "decimal": Decimal,
        "bool": bool,
        "date": date.fromisoformat,
        "datetime": datetime.fromisoformat,
    }
    return decoders[tag](tagged["v"])


def _cases() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    inputs = json.loads((GOLDEN / "staged_value_edge_inputs.json").read_text(encoding="utf-8"))
    golden = json.loads((GOLDEN / "staged_value_text.json").read_text(encoding="utf-8"))
    entries = cast("list[dict[str, Any]]", inputs["entries"])
    texts = cast("list[dict[str, Any]]", golden["texts"])
    assert [entry["name"] for entry in entries] == [text["name"] for text in texts]
    return list(zip(entries, texts, strict=True))


def _render_or_error(
    render: Callable[[PhysicalValue, V2Column], str | None], entry: dict[str, Any]
) -> dict[str, Any]:
    column = V2Column(entry["name"], entry["type"], entry["nullable"])
    try:
        return {"name": entry["name"], "text": render(_decoded(entry["value"]), column)}
    except ValueError as error:
        return {"name": entry["name"], "error": type(error).__name__}


def test_t4_every_staged_value_equals_the_base_renderers_golden_text() -> None:
    r"""Hold every staged value to the text the base renderer sent.

    The golden was generated once, in slice S1, by:

    uv run python scripts/generate-staged-value-golden.py \
        tests/unit/golden/staged_value_edge_inputs.json > tests/unit/golden/staged_value_text.json
    """
    golden = json.loads((GOLDEN / "staged_value_text.json").read_text(encoding="utf-8"))
    assert golden["renderer_blob"] == BASE_RENDERER_BLOB
    cases = _cases()
    assert len(cases) >= 30
    for entry, text in cases:
        assert _render_or_error(_rendered, entry) == text


def test_t4_control_an_isoformat_timestamp_renderer_fails_the_golden() -> None:
    def isoformat(value: PhysicalValue, column: V2Column) -> str | None:
        if isinstance(value, datetime):
            return value.isoformat()
        return _rendered(value, column)

    differing = [
        entry["name"] for entry, text in _cases() if _render_or_error(isoformat, entry) != text
    ]
    assert differing
    assert all(name.startswith("timestamp_") for name in differing)


def test_reserved_staging_column_names_refuse_at_catalog_load(tmp_path: Path) -> None:
    generated = Path(__file__).resolve().parents[2] / "contracts" / "generated" / "v2-tables.json"
    catalog = json.loads(generated.read_text(encoding="utf-8"))
    # Control: the shipped catalog uses neither name and loads.
    assert load_v2_storage_catalog(generated)
    for reserved in ("stg_ordinal", "staging_key"):
        altered = json.loads(json.dumps(catalog))
        altered["sourceAlignedTables"][0]["columns"][0]["name"] = reserved
        path = tmp_path / f"{reserved}.json"
        path.write_text(json.dumps(altered), encoding="utf-8")
        with pytest.raises(ValueError, match="reserved for staging"):
            load_v2_storage_catalog(path)
