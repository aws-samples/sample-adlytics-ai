# SPDX-License-Identifier: MIT-0
"""Every landing takes a fresh settled report first; an expired one never forces a re-run."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_TARGET = [
    "--profile",
    "fictional-profile",
    "--region",
    "us-east-1",
    "--expected-account",
    "000000000000",
    "--env",
    "dev",
]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import land_settled  # noqa: E402
from validation import probe_pipeline_settled as probe  # noqa: E402

from adapters.jobs.land_fixtures import job as land_job  # noqa: E402
from domain.curation.landing import LandingError  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]


def test_a_fresh_report_is_taken_before_each_landing_and_passed_to_it(tmp_path: Path) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    def settle(argv: list[str]) -> int:
        calls.append(("settle", tuple(argv)))
        return 0

    def land(argv: tuple[str, ...]) -> int:
        calls.append(("land", argv))
        return 0

    report = tmp_path / "settled.json"
    for _ in range(2):  # two phases in a row: each gets its own settle
        assert (
            land_settled.land_after_settle(
                tmp_path / "baseline.json",
                report,
                [*_TARGET, "--classes", "seal"],
                settle=settle,
                land=land,
            )
            == 0
        )
    assert [kind for kind, _ in calls] == ["settle", "land", "settle", "land"]
    assert calls[1][1] == (*_TARGET, "--classes", "seal", "--settled-report", str(report))
    assert calls[0][1][:8] == (
        "--profile",
        "fictional-profile",
        "--region",
        "us-east-1",
        "--expected-account",
        "000000000000",
        "--environment",
        "dev",
    )
    assert ("--settled-report", str(report)) == calls[0][1][-2:]


def test_an_unsettled_pipeline_lands_nothing(tmp_path: Path) -> None:
    landed: list[tuple[str, ...]] = []
    code = land_settled.land_after_settle(
        tmp_path / "b.json",
        tmp_path / "r.json",
        [*_TARGET, "--classes", "cadence"],
        settle=lambda _argv: 1,
        land=lambda argv: landed.append(argv) or 0,
    )
    assert (code, landed) == (1, [])


def test_the_landing_job_s_age_check_is_unchanged() -> None:
    # Positive control: the report the wrapper replaces would have been refused once expired.
    checked = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    report = probe.settled_report(
        environment="dev", account="000000000000", checked_at=checked, findings=()
    )
    with pytest.raises(LandingError, match="15 minutes"):
        land_job.check_settled_report(
            report, environment="dev", account="000000000000", now=checked + timedelta(minutes=16)
        )
    assert timedelta(minutes=15) == land_job.SETTLED_REPORT_MAX_AGE


def test_a_caller_cannot_pass_its_own_report(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="supplies --settled-report"):
        land_settled.land_after_settle(
            tmp_path / "b.json", tmp_path / "r.json", ["--settled-report", "old.json"]
        )


def test_the_settle_identity_comes_from_the_landing_arguments(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="needs the landing's --expected-account"):
        land_settled.land_after_settle(
            tmp_path / "b.json", tmp_path / "r.json", ["--profile", "p", "--region", "r"]
        )
