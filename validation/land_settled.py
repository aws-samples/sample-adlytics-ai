# SPDX-License-Identifier: MIT-0
"""Land one phase or seal selection behind a fresh settled report (owner, 2026-10-06).

Runs `probe_pipeline_settled.py` (signal-driven, so an idle pipeline settles in about a minute)
immediately before `adapters.jobs.land_fixtures`, and hands the new report to it. A report that
`SETTLED_REPORT_MAX_AGE` has expired therefore never forces a manual re-run, and the landing
job's own age check is unchanged. If the pipeline does not settle, nothing is landed.

    uv run python validation/land_settled.py --baseline <pre-phase baseline> \\
        --report <path> -- <land_fixtures arguments without --settled-report>
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import probe_pipeline_settled as settled

from adapters.jobs.land_fixtures import __main__ as land_cli


def _probe_target(land_arguments: Sequence[str]) -> list[str]:
    """The settle probe's identity, read from the landing's own explicit arguments."""
    values = list(land_arguments)
    target: list[str] = []
    for flag, probe_flag in (
        ("--profile", "--profile"),
        ("--region", "--region"),
        ("--expected-account", "--expected-account"),
        ("--env", "--environment"),
    ):
        if flag not in values or values.index(flag) + 1 >= len(values):
            raise SystemExit(f"land_settled.py needs the landing's {flag}.")
        target += [probe_flag, values[values.index(flag) + 1]]
    return target


def land_after_settle(
    baseline: Path,
    report: Path,
    land_arguments: Sequence[str],
    *,
    settle: Callable[[list[str]], int] = settled.run,
    land: Callable[[tuple[str, ...]], int] = land_cli.main,
) -> int:
    """Take a fresh settled report, then land with it; return the first nonzero exit code."""
    if "--settled-report" in land_arguments:
        raise SystemExit("land_settled.py supplies --settled-report itself.")
    code = settle(
        [
            *_probe_target(land_arguments),
            "--baseline",
            str(baseline),
            "--settled-report",
            str(report),
        ]
    )
    if code != 0:
        print("NOT SETTLED: nothing was landed", file=sys.stderr)
        return code
    return land((*land_arguments, "--settled-report", str(report)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("land_arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    rest = list(args.land_arguments)
    if rest[:1] == ["--"]:
        rest = rest[1:]
    return land_after_settle(args.baseline, args.report, rest)


if __name__ == "__main__":
    sys.exit(main())
