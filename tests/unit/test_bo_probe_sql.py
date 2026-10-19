# SPDX-License-Identifier: MIT-0
"""Rendering of the P-BO-1 step A queries and candidate pack text (pure)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "validation"))
sys.path.insert(0, str(ROOT))

from validation import bo_probe_sql as sql  # noqa: E402

pytestmark = pytest.mark.unit


def _part(name: str) -> str:
    return sql.expand((sql.SQL_DIR / name).read_text().strip())


def test_q3_q5_q5r_q7_are_exactly_the_candidate_packs() -> None:
    assert sql.QUERIES["Q3"].sql == sql.render_pack(5)
    assert sql.QUERIES["Q5"].sql == sql.render_pack(6)
    assert sql.QUERIES["Q5r"].sql == sql.render_pack(7)
    assert sql.QUERIES["Q7"].sql == sql.render_pack(14)
    assert sql.QUERIES["Q3m"].sql == sql.QUERIES["Q3"].sql


def test_q4_contains_pack5_ctes_and_select_verbatim() -> None:
    q4 = sql.QUERIES["Q4"].sql
    pack5 = sql.render_pack(5)
    ctes = pack5.split("\nSELECT g.exposure_source_set")[0]
    assert q4.startswith(ctes)
    assert _part("pack05.select.sql") in q4
    assert sql.QUERIES["Q4m"].sql == q4


def test_every_q4_branch_has_pack5_column_count() -> None:
    branches = sql.QUERIES["Q4"].sql.split("UNION ALL\nSELECT ")[1:]
    assert len(branches) == len(sql.PACK5_DIAG_LABELS)
    width = len(sql.PACK5_COLUMNS)
    for branch in branches:
        select_list = branch.split("\nFROM ")[0]
        depth, commas = 0, 0
        for char in select_list:
            depth += char == "("
            depth -= char == ")"
            commas += char == "," and depth == 0
        assert commas + 1 == width


def test_q8_and_q9_reuse_pack14() -> None:
    pack14 = sql.render_pack(14)
    assert sql.QUERIES["Q8"].sql.startswith(pack14)
    assert "'cohort_total'" in sql.QUERIES["Q8"].sql
    cohort = _part("pack14.ctes.sql")
    assert sql.QUERIES["Q9"].sql.startswith(f"WITH {cohort}")
    round_form = sql.expand("MONTH_INDEX(a, b)")
    assert round_form.startswith("ROUND(") and round_form in sql.expand("MONTH_INDEX(a, b)")
    assert "YEAR(" in sql.QUERIES["Q9alt"].sql
    assert "YEAR(" not in sql.QUERIES["Q9"].sql


def test_q1_and_q2_share_sql_and_differ_only_in_controls() -> None:
    q1, q2 = sql.QUERIES["Q1"], sql.QUERIES["Q2"]
    assert q1.sql == q2.sql
    assert (q1.controls_off, q2.controls_off) == (False, True)
    assert "event_dt_hour_utc" in sql.QUERIES["Q1r"].sql
    assert "event_dt_hour_utc" not in q1.sql


def test_fence_is_identical_in_packs_5_6_7() -> None:
    fence = sql.expand(sql.fence())
    for pack in (5, 6, 7):
        assert fence in sql.render_pack(pack)


def test_no_bare_seconds_between_bound_in_packs() -> None:
    bare = re.compile(r"SECONDS_BETWEEN\([^()]*\)\s*(<|>=|BETWEEN)")
    for pack in (5, 6, 7, 14):
        text = sql.render_pack(pack)
        for match in bare.finditer(text):
            prefix = text[max(0, match.start() - 4) : match.start()]
            assert prefix.endswith("ABS("), f"bare bound in pack {pack}"


def test_gap_seconds_is_the_documented_abs_form_without_unary_minus() -> None:
    assert sql.gap_seconds("a", "b") == "ABS(SECONDS_BETWEEN(a, b))"
    for query in sql.QUERIES.values():
        assert "-SECONDS_BETWEEN" not in query.sql
    assert "GAP_SECONDS" not in "".join(query.sql for query in sql.QUERIES.values())
    assert "MONTH_INDEX" not in "".join(query.sql for query in sql.QUERIES.values())


def test_no_line_comment_select_star_or_query_order_by() -> None:
    for query in sql.QUERIES.values():
        assert "--" not in query.sql
        assert not re.search(r"SELECT\s+\*", query.sql)
        for match in re.finditer(r"ORDER BY", query.sql):
            assert "OVER (" in query.sql[max(0, match.start() - 80) : match.start()]


def test_no_sensitive_output_alias() -> None:
    sensitive = ("user_id", "conversion_id", "request_tag", "purchase_id")
    for query in sql.QUERIES.values():
        assert not set(query.columns) & set(sensitive)
        for alias in re.findall(r"\bAS\s+(\w+)", query.sql):
            assert alias not in sensitive


def test_parameters_are_declared_per_query() -> None:
    assert sql.QUERIES["Q0"].parameters == ()
    assert sql.QUERIES["Q1"].parameters == ()
    assert set(sql.QUERIES["Q3"].parameters) == {sql.COUNTRY, sql.MARKETPLACE}
    assert set(sql.QUERIES["Q7"].parameters) == {sql.MARKETPLACE, sql.AS_OF}
