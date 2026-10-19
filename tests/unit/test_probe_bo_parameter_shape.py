# SPDX-License-Identifier: MIT-0
"""P-BO parameter shape probe: one execution, classified on the returned marker."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "validation"))
sys.path.insert(0, str(ROOT))

from tests.unit.test_amc_recovery import FakeApi  # noqa: E402
from validation import probe_bo_parameter_shape as probe  # noqa: E402

pytestmark = pytest.mark.unit

HEADER = (
    "marker,users,time_window_start,time_window_end,"
    "rows_suppressed,suppression_reason,distinct_user_count"
)


def test_confirmed_only_when_the_supplied_value_returns() -> None:
    good = f"{HEADER}\n{probe.MARKER_VALUE},9,a,b,false,,9\n"
    assert probe.classify("SUCCEEDED", good)["outcome"] == "confirmed"
    other = f"{HEADER}\nsomething-else,9,a,b,false,,9\n"
    assert probe.classify("SUCCEEDED", other)["outcome"] == "not_confirmed"
    assert probe.classify("REJECTED", None)["outcome"] == "not_confirmed"


def test_one_submission_carries_the_declared_parameter(tmp_path: Path) -> None:
    api = FakeApi()
    api.terminal = {"status": "REJECTED"}
    argv = ["--from", "2026-01-05T00:00:00", "--to", "2026-01-12T00:00:00"]
    assert probe.main(argv, findings=tmp_path, connector=lambda: (api, "instance-fixture")) == 1
    assert probe.main(argv, findings=tmp_path, connector=lambda: (api, "instance-fixture")) == 1
    assert len(api.submissions) == 1
    assert api.submissions[0]["parameters"] == {probe.MARKER_NAME: probe.MARKER_VALUE}
    assert api.submissions[0]["disable"] is False
