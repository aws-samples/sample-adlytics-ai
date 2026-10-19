# SPDX-License-Identifier: MIT-0
"""P-BO-3 and P-BO-4 probes on STANDARD (fictional segment names and counts)."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "validation"))
sys.path.insert(0, str(ROOT))

from tests.unit.test_amc_recovery import FakeApi  # noqa: E402
from validation import bo_probe_criteria as criteria  # noqa: E402
from validation import probe_bo_audience_segments as aud  # noqa: E402

pytestmark = pytest.mark.unit

TAIL = "distinct_user_count,suppression_reason,rows_suppressed"
TODAY = dt.date(2026, 10, 9)


def _never(kind: str) -> tuple[FakeApi, str]:
    raise AssertionError(f"discovery reached for {kind}")


def _parsed(columns: tuple[str, ...], rows: list[list[str]]) -> criteria.Parsed:
    header = ",".join((*columns, *TAIL.split(",")))
    body = "\n".join(",".join(row) for row in rows)
    return criteria.parse_result(
        f"{header}\n{body}\n", columns, window=("", ""), controls_off=False
    )


def test_sql_keeps_identifiers_out_and_reads_one_category_table() -> None:
    for probe in aud.PROBES.values():
        select = probe.sql.split("FROM")[0]
        assert "user_id," not in select.replace("user_id_type", "")
        assert "CAST(" not in probe.sql
    for name in ("pbo4-step1-inmarket", "pbo4-step1-lifestyle", "pbo4-step2"):
        sql = aud.PROBES[name].sql
        assert "segment_marketplace_id = 1" in sql
        tables = [t for t in ("inmarket_snapshot", "lifestyle_snapshot") if t in sql]
        assert len(tables) == 1


@pytest.mark.parametrize(
    ("text", "categories"),
    [
        ("Adults 25\u201334", {"age"}),
        ("Ages 65+ shoppers", {"age"}),
        ("Female shoppers", {"gender"}),
        ("Women's shoes", {"gender"}),
        ("Household income $100k+", {"income"}),
        ("Pet owners", set[str]()),
    ],
)
def test_category_patterns_split_the_design_pattern(text: str, categories: set[str]) -> None:
    found = {name for name, pattern in aud.CATEGORY_PATTERNS.items() if pattern.search(text)}
    assert found == categories
    assert bool(aud.DESIGN_PATTERN.search(text)) == bool(categories)


def test_pbo3_counts_only_reviewer_confirmed_candidates() -> None:
    parsed = _parsed(
        aud.PBO3_COLUMNS,
        [
            ["Women's shoes", "Product interest", "40", "", "", "2", "", "false"],
            ["Adults 25-34", "Age band", "40", "", "", "2", "", "false"],
            ["", "", "1", "", "", "1", "1 users < 2 required", "true"],
        ],
    )
    candidates = aud.pbo3_candidates(parsed)
    assert candidates == {"age": ["Adults 25-34"], "gender": ["Women's shoes"], "income": []}
    assert aud.pbo3_outcome(candidates, None)["outcome"] == "pending_review"
    outcome = aud.pbo3_outcome(candidates, {"Adults 25-34": "age"})
    assert outcome["outcome"] == "present"
    assert outcome["present_by_category"] == {"age": True, "gender": False, "income": False}
    assert aud.pbo3_outcome(candidates, {})["outcome"] == "absent"


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["40", "12", "12", "0", "300"], "pass"),
        (["40", "30", "30", "0", "300"], "pass"),
        (["40", "3", "3", "0", "300"], "fail"),
        (["40", "12", "11", "0", "300"], "fail"),
        (["40", "12", "12", "1", "300"], "fail"),
        (["40", "0", "0", "0", "0"], "inconclusive"),
    ],
)
def test_step1_criteria(values: list[str], expected: str) -> None:
    parsed = _parsed(aud.STEP1_COLUMNS, [[*values, "", "", "2", "", "false"]])
    assert aud.step1_outcome(parsed, "SUCCEEDED")["outcome"] == expected


def test_step1_without_a_result_is_inconclusive() -> None:
    assert aud.step1_outcome(None, "REJECTED")["outcome"] == "inconclusive"


def test_step2_records_id_spaces() -> None:
    parsed = _parsed(
        aud.STEP2_COLUMNS,
        [["adUserId", "40", "", "", "2", "", "false"], ["", "1", "", "", "1", "r", "true"]],
    )
    outcome = aud.step2_outcome(parsed, "SUCCEEDED")
    assert outcome["id_spaces"] == ["adUserId"]
    assert outcome["packs_10_11"] == "adUserId restriction stays"
    wider = _parsed(
        aud.STEP2_COLUMNS,
        [
            ["adUserId", "40", "", "", "2", "", "false"],
            ["sisDeviceId", "9", "", "", "2", "", "false"],
        ],
    )
    assert str(aud.step2_outcome(wider, "SUCCEEDED")["packs_10_11"]).startswith("lift")


def test_windows_are_checked_before_discovery(tmp_path: Path) -> None:
    probe = aud.PROBES["pbo4-step2"]
    aud.validate(probe, "2026-08-31T00:00:00", "2026-09-07T00:00:00", today=TODAY)
    with pytest.raises(SystemExit, match="first Thursday"):
        aud.validate(probe, "2026-09-14T00:00:00", "2026-09-21T00:00:00", today=TODAY)
    with pytest.raises(SystemExit, match="calendar month"):
        aud.validate(aud.PROBES["pbo3"], "2026-09-01T00:00:00", "2026-09-08T00:00:00", today=TODAY)
    args = aud.parser().parse_args(
        ["run", "--query", "pbo3", "--from", "2026-09-01T00:00:00", "--to", "2026-09-08T00:00:00"]
    )
    with pytest.raises(SystemExit, match="calendar month"):
        aud.run(args, findings=tmp_path, connector=_never)


def test_run_on_standard_sends_no_sandbox_controls_and_adopts_its_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = dt.datetime.now(tz=dt.UTC).date()
    end = today.replace(day=1)
    if (today - end).days < 2:  # the 48-hour availability lag
        end = (end - dt.timedelta(days=1)).replace(day=1)
    start = (end - dt.timedelta(days=1)).replace(day=1)
    window = (f"{start}T00:00:00", f"{end}T00:00:00")
    span = f"{window[0].replace('T', ' ')},{window[1].replace('T', ' ')}"

    def fetch(_url: str, *, max_bytes: int) -> str:
        del max_bytes
        header = ",".join((*aud.STEP1_COLUMNS, *TAIL.split(",")))
        return f"{header}\n40,12,12,0,300,{span},2,,false\n"

    monkeypatch.setattr(aud.amc_recovery, "fetch_csv", fetch)
    api = FakeApi()
    argv = ["run", "--query", "pbo4-step1-inmarket", "--from", window[0], "--to", window[1]]
    for _ in range(2):
        args = aud.parser().parse_args(argv)
        assert aud.run(args, findings=tmp_path, connector=lambda _k: (api, "standard-fixture")) == 0
    assert len(api.submissions) == 1
    assert api.submissions[0]["synthetic"] is None
    assert api.submissions[0]["disable"] is None
    args = aud.parser().parse_args(["evaluate", "--month-from", window[0]])
    assert aud.evaluate(args, findings=tmp_path) == 0
    (path,) = tmp_path.glob("bo-aud-verdict-*.json")
    verdict = json.loads(path.read_text())
    assert verdict["pbo4_step1"]["tables"]["inmarket"]["outcome"] == "pass"
    assert verdict["pbo4_step1"]["outcome"] == "inconclusive"  # lifestyle not run
    assert verdict["pbo3"]["outcome"] == "inconclusive"
