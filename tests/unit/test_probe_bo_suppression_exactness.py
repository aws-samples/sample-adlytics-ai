# SPDX-License-Identifier: MIT-0
"""P-BO-1 CLI: run guards, controls per query, and offline evaluate (fictional values)."""

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
from validation import amc_client  # noqa: E402
from validation import probe_bo_suppression_exactness as probe  # noqa: E402

pytestmark = pytest.mark.unit

WEEK_FROM, WEEK_TO = "2026-01-05T00:00:00", "2026-01-12T00:00:00"
TODAY = dt.date(2026, 3, 1)


def _args(query: str, *extra: str) -> list[str]:
    return [
        "run", "--instance-type", "SANDBOX", "--query", query,
        "--from", WEEK_FROM, "--to", WEEK_TO, *extra,
    ]  # fmt: skip


@pytest.fixture
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Clock(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> _Clock:
            return cls(2026, 3, 1, tzinfo=tz)

    monkeypatch.setattr(probe.dt, "datetime", _Clock)


def _run(tmp_path: Path, argv: list[str], api: FakeApi) -> int:
    args = probe.parser().parse_args(argv)
    return probe.run(args, findings=tmp_path, connect=lambda _kind: (api, "instance-fixture"))


@pytest.mark.usefixtures("frozen_today")
def test_q2_submits_controls_off_and_q1_controls_on(tmp_path: Path) -> None:
    api = FakeApi()
    api.terminal = {"status": "REJECTED"}
    _run(tmp_path, _args("Q1"), api)
    _run(tmp_path, _args("Q2"), api)
    assert [item["disable"] for item in api.submissions] == [False, True]
    assert all(item["synthetic"] is True for item in api.submissions)
    assert api.submissions[0]["sql"] == api.submissions[1]["sql"]
    assert api.submissions[0]["workflow_id"] != api.submissions[1]["workflow_id"]


@pytest.mark.usefixtures("frozen_today")
def test_wrong_window_shape_is_refused_before_discovery(tmp_path: Path) -> None:
    api = FakeApi()
    called: list[bool] = []

    def connect(_kind: str) -> tuple[FakeApi, str]:
        called.append(True)
        return api, "instance-fixture"

    argv = ["run", "--instance-type", "SANDBOX", "--query", "Q1",
            "--from", "2026-01-06T00:00:00", "--to", "2026-01-13T00:00:00"]  # fmt: skip
    with pytest.raises(SystemExit, match="ISO week"):
        probe.run(probe.parser().parse_args(argv), findings=tmp_path, connect=connect)
    assert not called


@pytest.mark.usefixtures("frozen_today")
def test_missing_marketplace_for_q3_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="explicit values"):
        _run(tmp_path, _args("Q3", "--dsp-campaign-country", "ZZ"), FakeApi())


def test_window_rules() -> None:
    q3 = probe.QUERIES["Q3"]
    with pytest.raises(SystemExit, match="extension"):
        probe.validate_window(q3, "2026-02-16T00:00:00", "2026-02-23T00:00:00", today=TODAY)
    with pytest.raises(SystemExit, match="retention"):
        probe.validate_window(q3, "2024-01-01T00:00:00", "2024-01-08T00:00:00", today=TODAY)
    probe.validate_window(
        probe.QUERIES["Q3m"], "2026-01-01T00:00:00", "2026-02-01T00:00:00", today=TODAY
    )
    with pytest.raises(SystemExit, match="1st of a month"):
        probe.validate_window(
            probe.QUERIES["Q7"], "2025-03-15T00:00:00", "2026-02-01T00:00:00", today=TODAY
        )
    probe.validate_window(
        probe.QUERIES["Q7"], "2025-03-01T00:00:00", "2026-02-01T00:00:00", today=TODAY
    )


def _write(  # noqa: PLR0913 -- fixture builder
    tmp_path: Path,
    query: str,
    *,
    csv_text: str,
    status: str = "SUCCEEDED",
) -> None:
    sql_digest = probe.amc_recovery.sql_digest(probe.QUERIES[query].sql)
    stem = f"bo-p1-sandbox-{query.lower()}-fixture00-20260105-20260112"
    summary = {"query": query, "instance_id": "instance-fixture", "sql_digest": sql_digest,
               "terminal_status": status}  # fmt: skip
    (tmp_path / f"{stem}.summary.json").write_text(json.dumps(summary))
    (tmp_path / f"{stem}.csv").write_text(csv_text)


def test_evaluate_reads_only_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("evaluate must not construct AdsApi")

    monkeypatch.setattr(amc_client, "AdsApi", refuse)
    sup = "rows_suppressed,suppression_reason,distinct_user_count"
    analytic = "campaign_id_string,event_date_utc,reach_users,impressions"
    header = f"{analytic},time_window_start,time_window_end,{sup}"
    q1 = "\n".join([header, f"cmp-fixture-a,2026-01-05,4,10,{WEEK_FROM},{WEEK_TO},false,,2",
                    f",,1,1,{WEEK_FROM},{WEEK_TO},true,fixture,1"])  # fmt: skip
    q2 = "\n".join([header, f"cmp-fixture-a,2026-01-05,4,10,{WEEK_FROM},{WEEK_TO},false,,4",
                    f"cmp-fixture-b,2026-01-05,1,1,{WEEK_FROM},{WEEK_TO},false,,1"])  # fmt: skip
    q0_header = f"earlier_later,later_earlier,gap,users,time_window_start,time_window_end,{sup}"
    q0 = f"{q0_header}\n3600,-3600,3600,9,{WEEK_FROM},{WEEK_TO},false,,9"
    _write(tmp_path, "Q0", csv_text=q0)
    _write(tmp_path, "Q1", csv_text=q1)
    _write(tmp_path, "Q2", csv_text=q2)
    code = probe.evaluate(
        probe.parser().parse_args(["evaluate", "--week-from", WEEK_FROM]), findings=tmp_path
    )
    verdicts = list(tmp_path.glob("bo-p1-verdict-*.json"))
    assert len(verdicts) == 1
    verdict = json.loads(verdicts[0].read_text())
    assert verdict["gates"]["source"] == "pass"
    assert verdict["criteria"]["8b"]["outcome"] == "pass"
    assert verdict["criteria"]["8b"]["detail"]["convention"] == "t2_minus_t1"
    assert verdict["packs"]["1"]["policy"] == "discard_suppressed_rows"
    assert code == 2  # derived criteria are inconclusive without Q3-Q9
    assert "cmp-fixture" not in verdicts[0].read_text()


@pytest.mark.usefixtures("frozen_today")
def test_standard_only_for_controls_on_fence_queries(tmp_path: Path) -> None:
    api = FakeApi()
    api.terminal = {"status": "REJECTED"}
    std = ["--dsp-campaign-country", "ZZ", "--marketplace-name", "FIXTURE.MKT"]
    q4 = ["run", "--instance-type", "STANDARD", "--query", "Q4",
          "--from", "2025-12-01T00:00:00", "--to", "2025-12-08T00:00:00", *std]  # fmt: skip
    with pytest.raises(SystemExit, match="SANDBOX"):
        probe.run(probe.parser().parse_args(q4), findings=tmp_path, connect=lambda _k: (api, "i"))
    q3 = [*q4[:4], "Q3", *q4[5:]]
    probe.run(probe.parser().parse_args(q3), findings=tmp_path, connect=lambda _k: (api, "i"))
    assert api.submissions[0]["synthetic"] is None
    assert api.submissions[0]["parameters"] == {
        "dsp_campaign_country": "ZZ",
        "sponsored_marketplace_name": "FIXTURE.MKT",
    }
