# SPDX-License-Identifier: MIT-0
"""P-BO-1 campaign_country schema probe: one column, plain form first (fictional values)."""

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
from validation import probe_bo_campaign_country_schema as probe  # noqa: E402

pytestmark = pytest.mark.unit

ARGV = ["--from", "2026-01-05T00:00:00", "--to", "2026-01-12T00:00:00"]
TODAY = dt.date(2026, 3, 1)
NOT_FOUND = "The SQL query was invalid: Column 'campaign_country' not found in any table"


def _main(api: FakeApi, tmp_path: Path, argv: list[str] = ARGV) -> int:
    return probe.main(
        argv, findings=tmp_path, connector=lambda: (api, "instance-fixture"), today=TODAY
    )


def _verdict(tmp_path: Path) -> dict[str, object]:
    (path,) = tmp_path.glob("bo-cc-verdict-*.json")
    return json.loads(path.read_text())


def test_classify_reads_only_a_rejection_naming_the_column() -> None:
    assert probe.classify("SUCCEEDED", "") == probe.RESOLVES
    assert probe.classify("REJECTED", NOT_FOUND) == probe.NOT_FOUND
    assert probe.classify("REJECTED", "Column 'other' not found in any table") == probe.INCONCLUSIVE
    assert probe.classify("REJECTED", "") == probe.INCONCLUSIVE
    assert probe.classify("FAILED", NOT_FOUND) == probe.INCONCLUSIVE


@pytest.mark.parametrize(
    ("plain", "extended", "conclusion"),
    [
        (probe.NOT_FOUND, None, "absent_from_sandbox_schema"),
        (probe.INCONCLUSIVE, None, probe.INCONCLUSIVE),
        (probe.RESOLVES, probe.NOT_FOUND, "not_resolvable_through_extend_time_window"),
        (probe.RESOLVES, probe.RESOLVES, "resolvable_both_forms"),
        (probe.RESOLVES, probe.INCONCLUSIVE, "present_on_plain_table"),
    ],
)
def test_conclude(plain: str, extended: str | None, conclusion: str) -> None:
    assert probe.conclude(plain, extended)["conclusion"] == conclusion


def test_forms_read_one_column_and_differ_only_in_the_table() -> None:
    plain, extended = probe.FORMS["plain"], probe.FORMS["extended"]
    assert plain.count("(campaign_country)") == extended.count("(campaign_country)") == 1
    assert "EXTEND_TIME_WINDOW" not in plain
    assert plain.replace("FROM dsp_impressions", "") == extended.replace(
        "FROM TABLE(EXTEND_TIME_WINDOW('dsp_impressions', 'P0D', 'P14D'))", ""
    )


def test_rejected_plain_form_never_submits_the_extended_form(tmp_path: Path) -> None:
    api = FakeApi()
    api.terminal = {"status": "REJECTED", "statusReason": NOT_FOUND}
    assert _main(api, tmp_path) == 0
    assert len(api.submissions) == 1
    assert api.submissions[0]["disable"] is False
    assert api.submissions[0]["synthetic"] is True
    assert "EXTEND_TIME_WINDOW" not in str(api.submissions[0]["sql"])
    assert _verdict(tmp_path)["conclusion"] == "absent_from_sandbox_schema"
    assert _main(api, tmp_path) == 0
    assert len(api.submissions) == 1  # the re-run adopts the journal


def test_resolved_plain_form_submits_the_extended_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fetch(_url: str, *, max_bytes: int) -> str:
        del max_bytes
        return "n\n1\n"

    monkeypatch.setattr(probe.amc_recovery, "fetch_csv", fetch)
    api = FakeApi()
    assert _main(api, tmp_path) == 0
    assert len(api.submissions) == 2
    assert "EXTEND_TIME_WINDOW" in str(api.submissions[1]["sql"])
    assert _verdict(tmp_path)["conclusion"] == "resolvable_both_forms"


def test_incomplete_extension_is_refused_before_discovery(tmp_path: Path) -> None:
    called: list[bool] = []

    def connector() -> tuple[FakeApi, str]:
        called.append(True)
        return FakeApi(), "instance-fixture"

    late = ["--from", "2026-02-16T00:00:00", "--to", "2026-02-23T00:00:00"]
    with pytest.raises(SystemExit, match="extension"):
        probe.main(late, findings=tmp_path, connector=connector, today=TODAY)
    assert not called
