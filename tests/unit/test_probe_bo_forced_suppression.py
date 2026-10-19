# SPDX-License-Identifier: MIT-0
"""Forced-suppression probe and paid-feature presence probe (fictional values)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "validation"))
sys.path.insert(0, str(ROOT))

from tests.unit.test_amc_recovery import FakeApi  # noqa: E402
from validation import probe_amc_paid_feature_schema as paid  # noqa: E402
from validation import probe_bo_forced_suppression as force  # noqa: E402

pytestmark = pytest.mark.unit


def _never(kind: str) -> tuple[FakeApi, str]:
    raise AssertionError(f"discovery reached for {kind}")


DAY = ["--from", "2026-01-05T00:00:00", "--to", "2026-01-06T00:00:00"]
WINDOW = "2026-01-05 00:00:00,2026-01-06 00:00:00"
TAIL = "distinct_user_count,suppression_reason,rows_suppressed"
ON_CSV = (
    "campaign_id_string,impression_dt_hour_utc,frequency_bucket,reach_users,impressions,"
    f"time_window_start,time_window_end,{TAIL}\n"
    f"c-1,2026-01-05 01:00:00,1,3,3,{WINDOW},2,,false\n"
    f",,2,1,2,{WINDOW},1,1 sandbox.dsp_impressions users < 2 required,true\n"
)
OFF_CSV = (
    "campaign_id_string,impression_dt_hour_utc,frequency_bucket,reach_users,impressions,"
    "time_window_start,time_window_end\n"
    f"c-1,2026-01-05 01:00:00,1,3,3,{WINDOW}\n"
    f"c-2,2026-01-05 02:00:00,2,1,2,{WINDOW}\n"
)


def _run(api: FakeApi, tmp_path: Path, *, controls: str) -> int:
    args = force.parser().parse_args(
        [
            "run",
            "--variant",
            "sparse_derived",
            "--controls",
            controls,
            "--instance-type",
            "SANDBOX",
            *DAY,
        ]
    )
    return force.run(args, findings=tmp_path, connector=lambda _kind: (api, "instance-fixture"))


def test_controls_off_is_refused_on_standard_before_discovery(tmp_path: Path) -> None:
    args = force.parser().parse_args(
        [
            "run",
            *("--variant", "sparse_source", "--controls", "off", "--instance-type", "STANDARD"),
            *DAY,
        ]
    )
    with pytest.raises(SystemExit, match="SANDBOX"):
        force.run(args, findings=tmp_path, connector=_never)


def test_pair_records_shape_and_derived_label_survival(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = iter((ON_CSV, OFF_CSV, ON_CSV))

    def fetch(_url: str, *, max_bytes: int) -> str:
        del max_bytes
        return next(payloads)

    monkeypatch.setattr(force.amc_recovery, "fetch_csv", fetch)
    api = FakeApi()
    assert _run(api, tmp_path, controls="on") == 0
    assert _run(api, tmp_path, controls="off") == 0
    assert [item["disable"] for item in api.submissions] == [False, True]
    assert _run(api, tmp_path, controls="on") == 0
    assert len(api.submissions) == 2  # the re-run adopts the journal
    args = force.parser().parse_args(["evaluate", *DAY])
    assert force.evaluate(args, findings=tmp_path) == 0
    (path,) = tmp_path.glob("bo-force-verdict-*.json")
    item = json.loads(path.read_text())["variants"]["sparse_derived"]["SANDBOX"]
    shape = item["controls_on_shape"]
    assert shape["suppressed_keys"] == {
        "campaign_id_string": "all_null",
        "impression_dt_hour_utc": "all_null",
        "frequency_bucket": "all_present",
    }
    assert shape["suppressed_measures"] == {
        "reach_users": "all_present",
        "impressions": "all_present",
    }
    assert shape["max_distinct_user_count_unsuppressed"] == "2"
    outcomes = {key: value["outcome"] for key, value in item["criteria"].items()}
    assert outcomes == {"0": "pass", "1": "pass", "2": "fail", "4": "pass", "3": "pass"}


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        ("SUCCEEDED", "", {"outcome": paid.RESOLVES}),
        (
            "REJECTED",
            "Column 'asin_parent' not found in any table",
            {"outcome": paid.COLUMN_NOT_FOUND, "column": "asin_parent"},
        ),
        (
            "REJECTED",
            "Table amazon_retail_purchases is not available",
            {"outcome": paid.TABLE_UNAVAILABLE},
        ),
        ("REJECTED", "", {"outcome": paid.INCONCLUSIVE}),
        ("FAILED", "amazon_retail_purchases", {"outcome": paid.INCONCLUSIVE}),
    ],
)
def test_paid_classify(status: str, reason: str, expected: dict[str, str]) -> None:
    assert paid.classify(paid.PROBES["arp_table"], status, reason) == expected


def test_paid_sql_never_selects_an_identifier() -> None:
    for probe in paid.PROBES.values():
        assert "COUNT(DISTINCT user_id)" in probe.sql or "user_id" not in probe.sql.replace(
            "user_id_type", ""
        )
        assert "GROUP BY" not in probe.sql


def test_paid_columns_probe_waits_for_its_table(tmp_path: Path) -> None:
    argv = ["--probe", "arp_columns", "--instance-type", "STANDARD", *DAY]
    with pytest.raises(SystemExit, match="arp_table"):
        paid.main(argv, findings=tmp_path, connector=_never)


def test_paid_table_probe_on_standard_sends_no_sandbox_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fetch(_url: str, *, max_bytes: int) -> str:
        del max_bytes
        header = f"purchase_units_sold_rows,time_window_start,time_window_end,{TAIL}"
        return f"{header}\n0,{WINDOW},0,,false\n"

    monkeypatch.setattr(paid.amc_recovery, "fetch_csv", fetch)
    api = FakeApi()
    argv = ["--probe", "arp_table", "--instance-type", "STANDARD", *DAY]
    assert (
        paid.main(argv, findings=tmp_path, connector=lambda _kind: (api, "instance-fixture")) == 0
    )
    assert api.submissions[0]["synthetic"] is None
    assert api.submissions[0]["disable"] is None
    (path,) = tmp_path.glob("paid-schema-verdict-standard-arp_table-*.json")
    assert json.loads(path.read_text())["outcome"] == paid.RESOLVES
