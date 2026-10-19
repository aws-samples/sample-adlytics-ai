# SPDX-License-Identifier: MIT-0
"""P-BO-1 step A: reach under suppression, one query per run, on a SANDBOX only.

Source validation, not a governed producer. Each `run` re-discovers the SANDBOX, recovers a
journaled or historical execution before it can submit, journals immediately, and writes the
terminal, CSV and a count-only summary to `findings/`. `evaluate` reads only `findings/`, makes
no AMC call, and writes one verdict. The pass criteria are owned by the business-outcomes design
§8; `bo_probe_criteria.py` implements them.

    set -a; source validation/.env.local; set +a
    uv run python validation/probe_bo_suppression_exactness.py run \
      --instance-type SANDBOX --query Q0 \
      --from <Monday>T00:00:00 --to <Monday + 7 days>T00:00:00
    uv run python validation/probe_bo_suppression_exactness.py evaluate \
      --week-from <Monday>T00:00:00 --cohort-from <YYYY-MM-01T00:00:00> --cohort-to <...>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from typing import cast

import amc_recovery
from bo_probe_criteria import (
    FAIL,
    INCONCLUSIVE,
    Inputs,
    Parsed,
    decide_packs,
    evaluate_gates,
    parse_result,
)
from bo_probe_sql import AS_OF, COUNTRY, MARKETPLACE, QUERIES, Query

FINDINGS = pathlib.Path(__file__).parent / "findings"
STEM_PREFIX = "bo-p1"
WORKFLOW_PREFIX = "probe-bo1"
RESULT_BYTE_LIMIT = 20_000_000
RETENTION_DAYS = 395
"""AMC keeps 13 months rolling (docs/sources/amc.md §7); 395 days is the conservative floor."""
AVAILABILITY_LAG_DAYS = 2
EXTENSION_DAYS = 14
WEEK_DAYS = 7
MAX_COHORT_MONTHS = 13
WINDOW_PACK_QUERIES = frozenset({"Q3", "Q4", "Q5", "Q6", "Q5r", "Q6r"})
FENCE_QUERIES = frozenset({"Q3", "Q4", "Q3m", "Q4m", "Q5", "Q6", "Q5r", "Q6r"})
"""Queries whose exposure fence filters `dsp_impressions.campaign_country`, a column
`probe_bo_campaign_country_schema.py` found absent from the Sandbox schema (2026-10-08). Their
controls-on halves may run on STANDARD (the internal demo instance: validation-instance
evidence); their controls-off halves cannot run there."""
PARAMETER_FORM_CONFIRMED = True
"""`workflow.inputParameters` plus execution `parameterValues`, confirmed for ad hoc executions by
`probe_bo_parameter_shape.py` on the Sandbox, 2026-10-08 (the supplied value came back)."""
MONTHS_PER_YEAR = 12


def _bound(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.UTC)
    except ValueError as error:
        raise SystemExit(f"bound {value!r} must be YYYY-MM-DDT00:00:00 (UTC)") from error
    if parsed.time() != dt.time(0):
        raise SystemExit(f"bound {value!r} must fall on a UTC midnight")
    return parsed


def _month_after(day: dt.datetime, months: int = 1) -> dt.datetime:
    index = day.year * MONTHS_PER_YEAR + day.month - 1 + months
    return day.replace(year=index // MONTHS_PER_YEAR, month=index % MONTHS_PER_YEAR + 1, day=1)


def _months_between(start: dt.datetime, end: dt.datetime) -> int:
    return (end.year - start.year) * MONTHS_PER_YEAR + end.month - start.month


def validate_window(query: Query, window_start: str, window_end: str, *, today: dt.date) -> None:
    """Refuse a window that breaks the query's rule, before any discovery or AMC call."""
    start, end = _bound(window_start), _bound(window_end)
    if end <= start:
        raise SystemExit("the window must be half-open with --to after --from")
    if start.date() < today - dt.timedelta(days=RETENTION_DAYS):
        raise SystemExit("--from falls outside AMC's 13-month retention")
    if end.date() > today - dt.timedelta(days=AVAILABILITY_LAG_DAYS):
        raise SystemExit("--to must end at least 48 hours before today")
    if query.window == "week":
        if start.weekday() != 0 or end - start != dt.timedelta(days=WEEK_DAYS):
            raise SystemExit(f"{query.name} needs one ISO week: a Monday to the next Monday")
    elif query.window == "month":
        if start.day != 1 or end != _month_after(start):
            raise SystemExit(f"{query.name} needs one calendar month: the 1st to the next 1st")
    elif start.day != 1 or end.day != 1 or _months_between(start, end) > MAX_COHORT_MONTHS:
        raise SystemExit(f"{query.name} needs the 1st of a month to the 1st, at most 13 months")
    if query.name in WINDOW_PACK_QUERIES:
        extended = end.date() + dt.timedelta(days=EXTENSION_DAYS)
        if extended > today - dt.timedelta(days=AVAILABILITY_LAG_DAYS):
            raise SystemExit("the 14-day window extension after --to is not yet complete")


def parameter_values(query: Query, args: argparse.Namespace) -> tuple[tuple[str, str], ...]:
    """Collect the query's CUSTOM_PARAMETER values from explicit arguments only."""
    supplied = {
        COUNTRY: args.dsp_campaign_country,
        MARKETPLACE: args.marketplace_name,
        AS_OF: args.as_of_month,
    }
    missing = [name for name in query.parameters if not supplied.get(name)]
    if missing:
        raise SystemExit(f"{query.name} needs explicit values for {missing}")
    if AS_OF in query.parameters:
        expected = (_month_after(_bound(args.window_end), -1)).strftime("%Y-%m-%d")
        if args.as_of_month != expected:
            raise SystemExit(f"--as-of-month must be {expected}, the month before --to")
        supplied[AS_OF] = f"{args.as_of_month} 00:00:00"
    return tuple((name, str(supplied[name])) for name in query.parameters)


def spec_for(query: Query, values: tuple[tuple[str, str], ...]) -> amc_recovery.ExecutionSpec:
    """Return the recovery identity for one query."""
    return amc_recovery.ExecutionSpec(
        name=query.name,
        sql=query.sql,
        controls_off=query.controls_off,
        workflow_prefix=WORKFLOW_PREFIX,
        stem_prefix=STEM_PREFIX,
        parameter_values=values,
    )


def summarize(
    plan: amc_recovery.ExecutionPlan,
    terminal: amc_recovery.TerminalResult,
    query: Query,
    *,
    window: tuple[str, str],
) -> dict[str, object]:
    """Write the count-only summary for one terminal execution."""
    summary: dict[str, object] = {
        "query": query.name,
        "instance_id": plan.instance_id,
        "instance_type": plan.instance_type,
        "execution_id": plan.execution_id,
        "workflow_id": plan.workflow_id,
        "identity_digest": plan.digest,
        "sql_digest": amc_recovery.sql_digest(query.sql),
        "controls_off": query.controls_off,
        "window_start": window[0],
        "window_end": window[1],
        "terminal_status": terminal.status,
        "status_reason": terminal.reason,
    }
    if terminal.csv_text is not None:
        parsed = parse_result(
            terminal.csv_text, query.columns, window=window, controls_off=query.controls_off
        )
        summary.update(
            {
                "row_count": len(parsed.rows),
                "suppressed_count": parsed.suppressed_count,
                "suppression_columns": parsed.suppression_columns,
                "defects": list(parsed.defects),
            }
        )
    (plan.findings / f"{plan.stem}.summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def connect_instance(instance_type: str) -> tuple[amc_recovery.ProbeApi, str]:
    """Authenticate from the sourced environment and re-discover an instance of the type."""
    from amc_client import AdsApi, credentials_from_environment
    from probe_amc_grain import discover

    api = AdsApi(credentials_from_environment())
    return cast("amc_recovery.ProbeApi", api), discover(api, instance_type)


def run(
    args: argparse.Namespace,
    *,
    findings: pathlib.Path = FINDINGS,
    connect: Callable[[str], tuple[amc_recovery.ProbeApi, str]] = connect_instance,
) -> int:
    """Recover or submit one query, then poll it to a terminal state."""
    query = QUERIES[str(args.query)]
    window = (str(args.window_start), str(args.window_end))
    validate_window(query, *window, today=dt.datetime.now(tz=dt.UTC).date())
    values = parameter_values(query, args)
    spec = spec_for(query, values)
    instance_type = str(args.instance_type)
    if instance_type != "SANDBOX" and (query.controls_off or query.name not in FENCE_QUERIES):
        raise SystemExit(
            f"{query.name} runs on SANDBOX; STANDARD is only for controls-on fence queries"
        )
    if values and not PARAMETER_FORM_CONFIRMED:
        raise SystemExit(f"{query.name} needs a confirmed ad hoc parameter form")
    api, instance_id = connect(instance_type)
    recover_since = str(
        args.recover_since
        or (dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    )
    print(f"instance: {instance_type}  query: {query.name}  window: {window[0]} .. {window[1]}")
    plan = amc_recovery.resolve_execution(
        amc_recovery.ExecutionRequest(
            api=api,
            instance_id=instance_id,
            instance_type=instance_type,
            spec=spec,
            window_start=window[0],
            window_end=window[1],
            recover_since=recover_since,
            findings=findings,
            allow_parameters=PARAMETER_FORM_CONFIRMED,
        )
    )
    terminal = amc_recovery.collect_terminal(api, plan, byte_limit=RESULT_BYTE_LIMIT)
    summary = summarize(plan, terminal, query, window=window)
    print(
        f"  {query.name:8s} status={terminal.status} rows={summary.get('row_count', '-')} "
        f"suppressed={summary.get('suppressed_count', '-')} defects={summary.get('defects', [])}"
    )
    return 0 if terminal.status == "SUCCEEDED" and not summary.get("defects") else 1


def _span(window: tuple[str, str]) -> str:
    return f"{window[0][:10].replace('-', '')}-{window[1][:10].replace('-', '')}"


def load_inputs(
    findings: pathlib.Path, windows: Mapping[str, tuple[str, str]]
) -> tuple[Inputs, dict[str, dict[str, object]]]:
    """Load the summaries and CSVs matching the current query text for each window kind."""
    results: dict[str, Parsed | None] = {}
    rejected: set[str] = set()
    statuses: dict[str, dict[str, object]] = {}
    instances: dict[str, set[str]] = {}
    sources: dict[str, str] = {}
    for name, query in QUERIES.items():
        window = windows.get(query.window)
        if window is None:
            continue
        pattern = f"{STEM_PREFIX}-*-{name.lower()}-*-{_span(window)}.summary.json"
        digest = amc_recovery.sql_digest(query.sql)
        matches = [
            (path, summary)
            for path in sorted(findings.glob(pattern))
            if (summary := cast("dict[str, object]", json.loads(path.read_text())))
            and summary.get("sql_digest") == digest
        ]
        if not matches:
            continue
        kinds = [str(summary.get("instance_type", "")) for _, summary in matches]
        if len(set(kinds)) != len(kinds):
            raise SystemExit(f"more than one {name} summary per instance type matches")
        succeeded = [m for m in matches if m[1].get("terminal_status") == "SUCCEEDED"]
        for _, other in matches:
            if other not in [m[1] for m in succeeded[:1]]:
                statuses[f"{name}@{other.get('instance_type')}"] = {
                    "terminal_status": other.get("terminal_status"),
                    "status_reason": other.get("status_reason"),
                }
        path, summary = (succeeded or matches)[0]
        kind = str(summary.get("instance_type", "SANDBOX"))
        instance_id = str(summary.get("instance_id", ""))
        instances.setdefault(kind, set()).add(instance_id)
        sources[name] = f"{kind}:{instance_id}"
        status = str(summary.get("terminal_status", ""))
        statuses[name] = {
            "instance_type": kind,
            "evidence_class": "sandbox" if kind == "SANDBOX" else "validation_instance",
            "terminal_status": status,
            "sql_digest": digest,
            "row_count": summary.get("row_count"),
            "defects": summary.get("defects", []),
        }
        if status == "REJECTED":
            rejected.add(name)
        if status != "SUCCEEDED":
            continue
        csv_path = path.with_name(path.name.replace(".summary.json", ".csv"))
        results[name] = parse_result(
            csv_path.read_text(), query.columns, window=window, controls_off=query.controls_off
        )
    if any(len(ids) > 1 for ids in instances.values()):
        raise SystemExit("summaries come from more than one instance of a type; refusing to mix")
    return Inputs(results, frozenset(rejected), sources), statuses


def evaluate(args: argparse.Namespace, *, findings: pathlib.Path = FINDINGS) -> int:
    """Classify every criterion offline from local findings and write one verdict."""
    windows: dict[str, tuple[str, str]] = {}
    week_from = _bound(str(args.week_from))
    windows["week"] = (
        str(args.week_from),
        (week_from + dt.timedelta(days=WEEK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S"),
    )
    if args.month_from:
        month_from = _bound(str(args.month_from))
        windows["month"] = (
            str(args.month_from),
            _month_after(month_from).strftime("%Y-%m-%dT%H:%M:%S"),
        )
    if args.cohort_from and args.cohort_to:
        windows["cohort"] = (str(args.cohort_from), str(args.cohort_to))
    inputs, statuses = load_inputs(findings, windows)
    gates = evaluate_gates(inputs)
    decisions = decide_packs(gates, pack7_bar_from="Q5r" if inputs.get("Q5r") else "Q5")
    verdict = {
        "evaluated_at": dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "evidence_class": "per query (sandbox, or validation_instance for STANDARD)",
        "windows": windows,
        "queries": statuses,
        "gates": {
            "source": gates.source,
            "sub_gate_1": gates.sub_gate_1,
            "sub_gate_2": gates.sub_gate_2,
        },
        "criteria": {key: asdict(value) for key, value in gates.criteria.items()},
        "packs": {str(pack): asdict(decision) for pack, decision in decisions.items()},
    }
    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = findings / f"{STEM_PREFIX}-verdict-{stamp}.json"
    findings.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verdict, indent=2, default=str))
    print(
        f"gates: source={gates.source} sub_gate_1={gates.sub_gate_1} sub_gate_2={gates.sub_gate_2}"
    )
    for key, value in gates.criteria.items():
        print(f"  criterion {key:10s} {value.outcome}")
    for pack, decision in decisions.items():
        print(
            f"  pack {pack:2d} {decision.policy} bar={decision.expected_bar} "
            f"blockers={list(decision.blockers)}"
        )
    print(f"verdict: {path.name}")
    outcomes = [value.outcome for value in gates.criteria.values()]
    defects = any(status.get("defects") for status in statuses.values())
    if defects or FAIL in outcomes:
        return 1
    return 2 if INCONCLUSIVE in outcomes else 0


def parser() -> argparse.ArgumentParser:
    """Build the `run` and `evaluate` command line."""
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--instance-type", choices=("SANDBOX", "STANDARD"), required=True)
    run_parser.add_argument("--query", choices=tuple(QUERIES), required=True)
    run_parser.add_argument("--from", dest="window_start", required=True)
    run_parser.add_argument("--to", dest="window_end", required=True)
    run_parser.add_argument("--as-of-month")
    run_parser.add_argument("--marketplace-name")
    run_parser.add_argument("--dsp-campaign-country")
    run_parser.add_argument("--recover-since")
    evaluate_parser = commands.add_parser("evaluate")
    evaluate_parser.add_argument("--week-from", required=True)
    evaluate_parser.add_argument("--month-from")
    evaluate_parser.add_argument("--cohort-from")
    evaluate_parser.add_argument("--cohort-to")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one command."""
    args = parser().parse_args(argv)
    return run(args) if args.command == "run" else evaluate(args)


if __name__ == "__main__":
    sys.exit(main())
