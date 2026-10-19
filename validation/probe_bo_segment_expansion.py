# SPDX-License-Identifier: MIT-0
# ruff: noqa: S608 -- static probe SQL; no value is interpolated
"""P-BO-2: test whether AMC SQL expands `dsp_impressions.matched_behavior_segment_ids`.

Source validation, not a governed producer. Step 0 (doc read) found `EXPLODE(array)` in the AMC
SQL function reference and no `UNNEST`, so only Form B runs; Form A is refused. Controls are
always on. One case per run over one complete calendar month, recovery-first, then an offline
`evaluate` applies the design §8 criteria (1)-(4). Segment IDs never leave `findings/`.

    set -a; source validation/.env.local; set +a
    uv run python validation/probe_bo_segment_expansion.py run \
      --instance-type SANDBOX --case expansion --form B \
      --from <YYYY-MM-01T00:00:00> --to <next YYYY-MM-01T00:00:00>
    uv run python validation/probe_bo_segment_expansion.py evaluate \
      --from <YYYY-MM-01T00:00:00> --to <next YYYY-MM-01T00:00:00>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import cast

import amc_recovery
from bo_probe_criteria import FAIL, INCONCLUSIVE, PASS, Parsed, number, parse_result, suppressed

FINDINGS = pathlib.Path(__file__).parent / "findings"
STEM_PREFIX = "bo-p2"
WORKFLOW_PREFIX = "probe-bo2"
RESULT_BYTE_LIMIT = 20_000_000
DOCUMENTED_FORM = "B"
"""`EXPLODE` is documented under Array functions [verified 2026-10-08, AMC SQL functions page];
`UNNEST` is not on that page or in the AMC SQL grammar."""
TYPES = ("SANDBOX", "STANDARD")
COLUMNS = ("segment_id", "shoppers", "impressions", "time_window_start", "time_window_end")
_WINDOW = (
    "BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start, "
    "BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end"
)
EXPANSION_SQL = f"""
SELECT segment_id, COUNT(DISTINCT user_id) AS shoppers, SUM(impressions) AS impressions,
       {_WINDOW}
FROM (SELECT user_id, impressions, EXPLODE(matched_behavior_segment_ids) AS segment_id
      FROM dsp_impressions)
GROUP BY segment_id
"""  # nosec B608 # A-SQL-001
CROSS_CHECK_SQL = f"""
SELECT behavior_segment_id AS segment_id, COUNT(DISTINCT user_id) AS shoppers,
       SUM(impressions) AS impressions,
       {_WINDOW}
FROM dsp_impressions_by_matched_segments
GROUP BY behavior_segment_id
"""  # nosec B608 # A-SQL-001
CASES = {"expansion": EXPANSION_SQL, "cross-check": CROSS_CHECK_SQL}
EVIDENCE = {"SANDBOX": "sandbox_provisional", "STANDARD": "validation_instance_admissibility"}
MONTHS_PER_YEAR = 12
AVAILABILITY_LAG_DAYS = 2
RETENTION_DAYS = 395


def spec_for(case: str) -> amc_recovery.ExecutionSpec:
    """Return the recovery identity for one case (controls always on)."""
    return amc_recovery.ExecutionSpec(
        name=case,
        sql=CASES[case],
        controls_off=False,
        workflow_prefix=WORKFLOW_PREFIX,
        stem_prefix=STEM_PREFIX,
    )


def _bound(value: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.UTC)
    except ValueError as error:
        raise SystemExit(f"bound {value!r} must be YYYY-MM-DDT00:00:00 (UTC)") from error


def validate_month(window_start: str, window_end: str, *, today: dt.date) -> None:
    """Require one complete calendar month inside retention and 48 hours old."""
    start, end = _bound(window_start), _bound(window_end)
    index = start.year * MONTHS_PER_YEAR + start.month
    following = start.replace(
        year=index // MONTHS_PER_YEAR, month=index % MONTHS_PER_YEAR + 1, day=1
    )
    if start.day != 1 or start.time() != dt.time(0) or end != following:
        raise SystemExit("P-BO-2 needs one calendar month: the 1st to the next 1st, UTC")
    if start.date() < today - dt.timedelta(days=RETENTION_DAYS):
        raise SystemExit("--from falls outside AMC's 13-month retention")
    if end.date() > today - dt.timedelta(days=AVAILABILITY_LAG_DAYS):
        raise SystemExit("--to must end at least 48 hours before today")


def connect(instance_type: str) -> tuple[amc_recovery.ProbeApi, str]:
    """Authenticate from the sourced environment and re-discover an instance of the type."""
    from amc_client import AdsApi, credentials_from_environment
    from probe_amc_grain import discover

    api = AdsApi(credentials_from_environment())
    return cast("amc_recovery.ProbeApi", api), discover(api, instance_type)


def run(
    args: argparse.Namespace,
    *,
    findings: pathlib.Path = FINDINGS,
    connector: Callable[[str], tuple[amc_recovery.ProbeApi, str]] = connect,
) -> int:
    """Recover or submit one case and poll it to a terminal state."""
    if args.case == "expansion" and args.form != DOCUMENTED_FORM:
        raise SystemExit(f"only the documented Form {DOCUMENTED_FORM} (EXPLODE) may run")
    window = (str(args.window_start), str(args.window_end))
    validate_month(*window, today=dt.datetime.now(tz=dt.UTC).date())
    instance_type = str(args.instance_type)
    api, instance_id = connector(instance_type)
    since = str(
        args.recover_since
        or (dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    )
    print(f"instance: {instance_type}  case: {args.case}  window: {window[0]} .. {window[1]}")
    plan = amc_recovery.resolve_execution(
        amc_recovery.ExecutionRequest(
            api=api,
            instance_id=instance_id,
            instance_type=instance_type,
            spec=spec_for(str(args.case)),
            window_start=window[0],
            window_end=window[1],
            recover_since=since,
            findings=findings,
        )
    )
    terminal = amc_recovery.collect_terminal(api, plan, byte_limit=RESULT_BYTE_LIMIT)
    summary: dict[str, object] = {
        "case": args.case,
        "instance_id": plan.instance_id,
        "instance_type": instance_type,
        "execution_id": plan.execution_id,
        "sql_digest": amc_recovery.sql_digest(CASES[str(args.case)]),
        "window_start": window[0],
        "window_end": window[1],
        "terminal_status": terminal.status,
        "status_reason": terminal.reason,
    }
    if terminal.csv_text is not None:
        parsed = parse_result(terminal.csv_text, COLUMNS, window=window, controls_off=False)
        summary.update(
            row_count=len(parsed.rows),
            suppressed_count=parsed.suppressed_count,
            defects=list(parsed.defects),
        )
    (findings / f"{plan.stem}.summary.json").write_text(json.dumps(summary, indent=2))
    print(f"  {args.case} status={terminal.status} rows={summary.get('row_count', '-')}")
    return 0 if terminal.status == "SUCCEEDED" else 1


@dataclass(frozen=True, slots=True)
class Verdict:
    """The P-BO-2 outcome on one instance type."""

    instance_type: str
    evidence_class: str
    criteria: Mapping[str, str]
    detail: Mapping[str, object]
    route: str


def _unsuppressed(parsed: Parsed) -> dict[str, object]:
    return {
        row["segment_id"].strip(): number(row, "impressions")
        for row in parsed.rows
        if not suppressed(row) and row.get("segment_id", "").strip()
    }


def classify(
    instance_type: str,
    *,
    expansion_status: str | None,
    expansion: Parsed | None,
    cross_check: Parsed | None,
    standard_admissible: bool | None = None,
) -> Verdict:
    """Apply criteria (1)-(4); a route of C1-b needs criterion (1) on a STANDARD instance."""
    criteria: dict[str, str] = {}
    detail: dict[str, object] = {}
    if expansion_status is None:
        criteria = dict.fromkeys(("1", "2", "3", "4"), INCONCLUSIVE)
    else:
        criteria["1"] = PASS if expansion_status == "SUCCEEDED" else FAIL
    if criteria.get("1") == FAIL:
        criteria.update(dict.fromkeys(("2", "3", "4"), FAIL))
    elif expansion is not None:
        exp = _unsuppressed(expansion)
        criteria["2"] = PASS if exp else FAIL
        detail["expansion_unsuppressed_segments"] = len(exp)
        if cross_check is None:
            criteria["3"] = criteria["4"] = INCONCLUSIVE
        else:
            ref = _unsuppressed(cross_check)
            both = set(exp) & set(ref)
            mismatched = sum(exp[key] != ref[key] for key in both)
            detail.update(
                cross_check_unsuppressed_segments=len(ref),
                compared=len(both),
                impressions_mismatched=mismatched,
                only_in_expansion=len(set(exp) - set(ref)),
                only_in_cross_check=len(set(ref) - set(exp)),
            )
            if not exp and not ref:
                criteria["3"] = criteria["4"] = INCONCLUSIVE
            else:
                criteria["3"] = (
                    PASS if both and not mismatched else FAIL if mismatched else INCONCLUSIVE
                )
                criteria["4"] = PASS if set(exp) == set(ref) else FAIL
    if FAIL in criteria.values():
        route = "C1-a"
    elif instance_type == "STANDARD" and criteria.get("1") == PASS:
        route = (
            "C1-b" if all(value == PASS for value in criteria.values()) else "C1-a (provisional)"
        )
    elif all(value == PASS for value in criteria.values()):
        route = (
            "C1-b"
            if standard_admissible
            else "C1-a (provisional: Standard criterion 1 not yet run)"
        )
    else:
        route = "C1-a (provisional)"
    return Verdict(instance_type, EVIDENCE[instance_type], criteria, detail, route)


def _load(
    findings: pathlib.Path, instance_type: str, *, case: str, span: str
) -> tuple[str | None, Parsed | None]:
    digest = amc_recovery.sql_digest(CASES[case])
    pattern = f"{STEM_PREFIX}-{instance_type.lower()}-{case}-*-{span}.summary.json"
    for path in sorted(findings.glob(pattern)):
        summary = cast("dict[str, object]", json.loads(path.read_text()))
        if summary.get("sql_digest") != digest:
            continue
        status = str(summary.get("terminal_status", ""))
        csv_path = path.with_name(path.name.replace(".summary.json", ".csv"))
        if status != "SUCCEEDED" or not csv_path.exists():
            return status, None
        window = (str(summary["window_start"]), str(summary["window_end"]))
        return status, parse_result(
            csv_path.read_text(), COLUMNS, window=window, controls_off=False
        )
    return None, None


def evaluate(args: argparse.Namespace, *, findings: pathlib.Path = FINDINGS) -> int:
    """Classify each instance type's results offline and write one verdict."""
    start, end = (
        str(value)[:10].replace("-", "") for value in (args.window_start, args.window_end)
    )
    span = f"{start}-{end}"
    standard_status, _ = _load(findings, "STANDARD", case="expansion", span=span)
    verdicts: list[Verdict] = []
    for instance_type in TYPES:
        status, expansion = _load(findings, instance_type, case="expansion", span=span)
        _, cross = _load(findings, instance_type, case="cross-check", span=span)
        verdicts.append(
            classify(
                instance_type,
                expansion_status=status,
                expansion=expansion,
                cross_check=cross,
                standard_admissible=standard_status == "SUCCEEDED" if standard_status else None,
            )
        )
    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = findings / f"{STEM_PREFIX}-verdict-{stamp}.json"
    path.write_text(json.dumps([asdict(verdict) for verdict in verdicts], indent=2))
    for verdict in verdicts:
        print(f"{verdict.instance_type}: {dict(verdict.criteria)} route={verdict.route}")
    print(f"verdict: {path.name}")
    return 0


def parser() -> argparse.ArgumentParser:
    """Build the `run` and `evaluate` command line."""
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--instance-type", choices=TYPES, required=True)
    run_parser.add_argument("--case", choices=tuple(CASES), required=True)
    run_parser.add_argument("--form", choices=("A", "B"), default=DOCUMENTED_FORM)
    run_parser.add_argument("--from", dest="window_start", required=True)
    run_parser.add_argument("--to", dest="window_end", required=True)
    run_parser.add_argument("--recover-since")
    evaluate_parser = commands.add_parser("evaluate")
    evaluate_parser.add_argument("--from", dest="window_start", required=True)
    evaluate_parser.add_argument("--to", dest="window_end", required=True)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one command."""
    args = parser().parse_args(argv)
    return run(args) if args.command == "run" else evaluate(args)


if __name__ == "__main__":
    sys.exit(main())
