# SPDX-License-Identifier: MIT-0
"""Supplementary P-BO-1 checks: fence structural variant and pack 14 daily grain (fictional)."""

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
from validation import bo_probe_sql as sql  # noqa: E402
from validation import probe_bo_supplementary as supp  # noqa: E402

pytestmark = pytest.mark.unit

TAIL = "distinct_user_count,suppression_reason,rows_suppressed"


def _never(kind: str) -> tuple[FakeApi, str]:
    raise AssertionError(f"discovery reached for {kind}")


def _cohort_window() -> tuple[str, str, str]:
    """Return a 12-month cohort window ending at least 48 hours ago, and its as-of month."""
    today = dt.datetime.now(tz=dt.UTC).date()
    end = today.replace(day=1)
    if (today - end).days < 2:  # the 48-hour availability lag
        end = (end - dt.timedelta(days=1)).replace(day=1)
    start = end.replace(year=end.year - 1)
    as_of = (end - dt.timedelta(days=1)).replace(day=1)
    return f"{start}T00:00:00", f"{end}T00:00:00", str(as_of)


def test_structural_fence_drops_only_the_country_filter() -> None:
    exact, structural = sql.fence().split(), sql.structural_fence().split()
    removed = [token for token in exact if token not in structural]
    assert removed == ["campaign_country", "CUSTOM_PARAMETER('dsp_campaign_country')"]
    assert len(exact) - len(structural) == 4  # AND campaign_country = CUSTOM_PARAMETER(...)
    for name in ("Q3s", "Q4s", "Q4ms"):
        query = sql.SUPPLEMENTARY[name]
        assert "campaign_country" not in query.sql
        assert query.parameters == (sql.MARKETPLACE,)
    assert sql.SUPPLEMENTARY["Q4s"].sql == sql.SUPPLEMENTARY["Q4ms"].sql
    for label in sql.PACK5_DIAG_LABELS:
        assert f"'{label}'" in sql.SUPPLEMENTARY["Q4s"].sql


def test_daily_pack14_adds_only_the_acquisition_date_key() -> None:
    daily = " ".join(sql.SUPPLEMENTARY["Q7d"].sql.split())
    for fragment in (
        " f.acq_date AS acquisition_date,",
        " ch.acquisition_date,",
        " x.acquisition_date,",
        ", x.acquisition_date",
    ):
        assert daily.count(fragment) == 1
        daily = daily.replace(fragment, "")
    assert daily == " ".join(sql.render_pack(14).split())
    assert sql.SUPPLEMENTARY["Q7d"].sql == sql.SUPPLEMENTARY["Q8d"].sql
    assert sql.SUPPLEMENTARY["Q8d"].controls_off
    assert not sql.SUPPLEMENTARY["Q7d"].controls_off
    assert sql.PACK14_DAILY_COLUMNS[3] == sql.ACQUISITION_DATE


def test_fence_query_needs_its_extension_before_discovery(tmp_path: Path) -> None:
    today = dt.datetime.now(tz=dt.UTC).date()
    # Two weeks back, so the week ends at least 48 hours before today on every weekday.
    monday = today - dt.timedelta(days=today.weekday() + 14)
    args = supp.parser().parse_args(
        [
            "run",
            *("--query", "Q3s", "--marketplace-name", "MARKETPLACE-FIXTURE"),
            *("--from", f"{monday}T00:00:00"),
            *("--to", f"{monday + dt.timedelta(days=7)}T00:00:00"),
        ]
    )
    with pytest.raises(SystemExit, match="extension"):
        supp.run(args, findings=tmp_path, connector=_never)


def test_criterion_2_restated_allows_derived_labels_only() -> None:
    rows = (
        {"label": "a", "campaign": "", "rows_suppressed": "true"},
        {"label": "b", "campaign": "", "rows_suppressed": "false"},
    )
    result = criteria.criterion_2_restated(
        rows, source_keys=("campaign",), derived_keys=("label",), label="2"
    )
    assert (result.outcome, result.detail["derived_labels_surviving"]) == ("pass", 1)
    leaking = ({"label": "a", "campaign": "c-1", "rows_suppressed": "true"},)
    assert (
        criteria.criterion_2_restated(
            leaking, source_keys=("campaign",), derived_keys=("label",), label="2"
        ).outcome
        == "fail"
    )
    assert (
        criteria.criterion_2_restated(
            rows[1:], source_keys=("campaign",), derived_keys=("label",), label="2"
        ).outcome
        == "inconclusive"
    )


def _daily_csv(window: tuple[str, str], *, controls: str) -> str:
    span = [window[0].replace("T", " "), window[1].replace("T", " ")]
    kept = ["2026-01-01", "1", "dsp", "2026-01-03", "3", "3", "1", "1", "1", "1", "1", "2", "9.5"]
    withheld = ["2026-01-01", "1", "dsp", "2026-01-04", "1", "1", "0", "0", "0", "0", "0", "", ""]
    rows = [[*kept, "USD", "USD", *span], [*withheld, "", "", *span]]
    header = list(sql.PACK14_DAILY_COLUMNS)
    if controls == "on":
        header += TAIL.split(",")
        reason = "1 sandbox.amazon_attributed_events_by_conversion_time users < 2 required"
        rows = [[*rows[0], "2", "", "false"], [*rows[1], "1", reason, "true"]]
    return "\n".join(",".join(row) for row in (header, *rows)) + "\n"


def test_daily_pair_exercises_0c_and_passes_5c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, end, as_of = _cohort_window()
    window = (start, end)
    payloads = iter((_daily_csv(window, controls="on"), _daily_csv(window, controls="off")))

    def fetch(_url: str, *, max_bytes: int) -> str:
        del max_bytes
        return next(payloads)

    monkeypatch.setattr(supp.amc_recovery, "fetch_csv", fetch)
    api = FakeApi()
    for name in ("Q7d", "Q8d"):
        args = supp.parser().parse_args(
            [
                "run",
                *("--query", name, "--from", start, "--to", end),
                *("--marketplace-name", "MARKETPLACE-FIXTURE", "--as-of-month", as_of),
            ]
        )
        assert supp.run(args, findings=tmp_path, connector=lambda _k: (api, "sandbox-fixture")) == 0
    assert [item["disable"] for item in api.submissions] == [False, True]
    assert all(item["synthetic"] is True for item in api.submissions)
    args = supp.parser().parse_args(
        [
            "evaluate",
            *("--week-from", "2026-01-05T00:00:00"),
            *("--cohort-from", start, "--cohort-to", end),
        ]
    )
    assert supp.evaluate(args, findings=tmp_path) == 0
    (path,) = tmp_path.glob("bo-supp-verdict-*.json")
    verdict = json.loads(path.read_text())
    daily = verdict["retention_daily"]
    outcomes = {key: value["outcome"] for key, value in daily["criteria"].items()}
    assert outcomes["0c"] == "pass"
    assert outcomes["5c"] == "pass"
    assert outcomes["3"] == "pass"
    assert outcomes["11.Q7d"] == "pass"
    assert daily["packs_14_15_policy"] == "discard_suppressed_rows (monthly cohorts)"
    assert daily["shape"]["labels"]["labels_name_a_withheld_cell"] == 1
    fence = verdict["fence_structural"]
    assert fence["label"] == sql.STRUCTURAL_LABEL
    assert fence["counts_as_ac_e15_pass"] is False
    assert fence["criteria"]["8"]["outcome"] == "inconclusive"


def test_sign_cross_check_reads_the_recorded_convention() -> None:
    header = ",".join(sql.PACK5_COLUMNS)
    blank = "," * (len(sql.PACK5_COLUMNS) - 3)
    rows = "\n".join(
        f"{label},all,{value}{blank}"
        for label, value in (
            ("diag_multi_hour_shoppers", 4),
            ("diag_sign_check", 4),
            ("diag_sign_check_mirror", 0),
        )
    )
    parsed = criteria.parse_result(
        f"{header}\n{rows}\n", sql.PACK5_COLUMNS, window=("", ""), controls_off=True
    )
    assert supp.sign_cross_check(parsed).outcome == "pass"
    assert supp.sign_cross_check(parsed, "t1_minus_t2").outcome == "fail"
