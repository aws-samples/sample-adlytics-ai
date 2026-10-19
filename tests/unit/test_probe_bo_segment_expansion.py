# SPDX-License-Identifier: MIT-0
"""P-BO-2 criteria (1)-(4) and evidence classes on fictional segment IDs."""

from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "validation"))
sys.path.insert(0, str(ROOT))

from tests.unit.test_amc_recovery import FakeApi  # noqa: E402
from validation import probe_bo_segment_expansion as probe  # noqa: E402

pytestmark = pytest.mark.unit

MONTH = ("2026-01-01T00:00:00", "2026-02-01T00:00:00")
SUP = "rows_suppressed,suppression_reason,distinct_user_count"


def result(rows: Sequence[tuple[str, int, bool]]) -> probe.Parsed:
    header = ",".join(probe.COLUMNS) + "," + SUP
    lines = [header] + [
        f"{'' if hidden else seg},2,{imps},{MONTH[0]},{MONTH[1]},{str(hidden).lower()},,2"
        for seg, imps, hidden in rows
    ]
    return probe.parse_result("\n".join(lines), probe.COLUMNS, window=MONTH, controls_off=False)


MATCH = [("9001", 10, False), ("9002", 4, False), ("", 1, True)]


def test_sandbox_pass_is_provisional_until_standard_admissibility() -> None:
    verdict = probe.classify(
        "SANDBOX", expansion_status="SUCCEEDED", expansion=result(MATCH), cross_check=result(MATCH)
    )
    assert set(verdict.criteria.values()) == {probe.PASS}
    assert verdict.evidence_class == "sandbox_provisional"
    assert verdict.route.startswith("C1-a (provisional")
    admitted = probe.classify(
        "SANDBOX",
        expansion_status="SUCCEEDED",
        expansion=result(MATCH),
        cross_check=result(MATCH),
        standard_admissible=True,
    )
    assert admitted.route == "C1-b"


def test_standard_pass_reads_c1b() -> None:
    verdict = probe.classify(
        "STANDARD", expansion_status="SUCCEEDED", expansion=result(MATCH), cross_check=result(MATCH)
    )
    assert verdict.route == "C1-b"
    assert verdict.evidence_class == "validation_instance_admissibility"


def test_mismatch_and_set_difference_fail() -> None:
    other = [("9001", 11, False), ("9003", 4, False)]
    verdict = probe.classify(
        "SANDBOX", expansion_status="SUCCEEDED", expansion=result(MATCH), cross_check=result(other)
    )
    assert verdict.criteria["3"] == probe.FAIL
    assert verdict.criteria["4"] == probe.FAIL
    assert verdict.route == "C1-a"
    assert verdict.detail["impressions_mismatched"] == 1


def test_no_unsuppressed_rows_fails_2_and_leaves_3_4_inconclusive() -> None:
    hidden = [("", 1, True)]
    verdict = probe.classify(
        "STANDARD",
        expansion_status="SUCCEEDED",
        expansion=result(hidden),
        cross_check=result(hidden),
    )
    assert verdict.criteria["2"] == probe.FAIL
    assert verdict.criteria["3"] == verdict.criteria["4"] == probe.INCONCLUSIVE


@pytest.mark.parametrize("status", ["REJECTED", "FAILED", "CANCELLED"])
def test_non_succeeded_expansion_fails(status: str) -> None:
    verdict = probe.classify("SANDBOX", expansion_status=status, expansion=None, cross_check=None)
    assert verdict.criteria["1"] == probe.FAIL
    assert verdict.route == "C1-a"


def test_not_run_is_inconclusive() -> None:
    verdict = probe.classify("STANDARD", expansion_status=None, expansion=None, cross_check=None)
    assert set(verdict.criteria.values()) == {probe.INCONCLUSIVE}


def test_undocumented_form_is_refused_before_any_call(tmp_path: Path) -> None:
    api = FakeApi()
    argv = ["run", "--instance-type", "SANDBOX", "--case", "expansion", "--form", "A",
            "--from", MONTH[0], "--to", MONTH[1]]  # fmt: skip
    with pytest.raises(SystemExit, match="documented"):
        probe.run(
            probe.parser().parse_args(argv), findings=tmp_path, connector=lambda _: (api, "i")
        )
    assert api.calls == 0


def test_month_window_rule() -> None:
    with pytest.raises(SystemExit, match="calendar month"):
        probe.validate_month(
            "2026-01-02T00:00:00", "2026-02-01T00:00:00", today=dt.date(2026, 3, 1)
        )
    probe.validate_month(*MONTH, today=dt.date(2026, 3, 1))
