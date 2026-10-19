# SPDX-License-Identifier: MIT-0
"""Supplementary P-BO-1 checks the owner approved on 2026-10-09, on a SANDBOX only.

Two checks, each a controls-on query and its controls-off reference on the same instance:

- **Packs 5-7 fence: structural check, not the exact pack text.** Q3s/Q4s are pack 5 and Q4 with
  the fence's one `campaign_country` line removed, because the Sandbox schema lacks that column.
  Q4ms is Q4s over a calendar month, run only if criterion 8 isn't exercised over the week. Every
  result is labelled `bo_probe_sql.STRUCTURAL_LABEL`, and it never counts as the AC-E1.5 pass:
  AC-E1.5 stays "pending Step B".
- **S-6 Retention: pack 14 at acquisition-date grain.** Q7d/Q8d are pack 14 with one added grain
  key, the shopper's acquisition date, so cells are sparse enough to suppress. They exercise
  criteria 0c and 5c; a pass lets monthly cohorts ship under `discard_suppressed_rows`.

`run` re-discovers the Sandbox and adopts a journaled or historical execution before it can submit
(amc_recovery). `evaluate` reads only `findings/` and writes one count-only verdict. The criteria
are owned by the business-outcomes design §8, with criterion 2 as the owner restated it on
2026-10-08: source keys NULL, derived labels may survive, `rows_suppressed` identifies the row.

    set -a; source validation/.env.local; set +a
    uv run python validation/probe_bo_supplementary.py run --query Q3s \
      --from <Monday>T00:00:00 --to <next Monday>T00:00:00 --marketplace-name <name>
    uv run python validation/probe_bo_supplementary.py evaluate --week-from <Monday>T00:00:00 \
      --cohort-from <YYYY-MM-01T00:00:00> --cohort-to <YYYY-MM-01T00:00:00>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from decimal import Decimal
from typing import cast

import amc_recovery
from bo_probe_criteria import (
    INCONCLUSIVE,
    PACK5_COUNTS,
    PACK5_KEYS,
    PACK14_COUNTS,
    PACK14_KEYS,
    PASS,
    CriterionResult,
    Inputs,
    Parsed,
    bar_of,
    combine,
    counted,
    criterion_2_restated,
    criterion_3,
    criterion_6,
    criterion_7,
    criterion_8,
    criterion_11,
    exactness,
    group_rows,
    parse_result,
    suppressed,
    withheld_labels_matched,
)
from bo_probe_sql import (
    ACQUISITION_DATE,
    PACK5_DIAG_LABELS,
    STRUCTURAL_LABEL,
    SUPPLEMENTARY,
    Query,
)
from probe_bo_suppression_exactness import (
    AVAILABILITY_LAG_DAYS,
    EXTENSION_DAYS,
    WEEK_DAYS,
    parameter_values,
    validate_window,
)

FINDINGS = pathlib.Path(__file__).parent / "findings"
STEM_PREFIX = "bo-supp"
WORKFLOW_PREFIX = "probe-bo-supp"
RESULT_BYTE_LIMIT = 20_000_000
SANDBOX = "SANDBOX"
FENCE_QUERIES = frozenset({"Q3s", "Q4s", "Q4ms"})
RECORDED_CONVENTION = "t2_minus_t1"
"""Criterion 8b's reading, from Q0 on the Sandbox (validation/RESULTS.md, 2026-10-08)."""
PACK14_DAILY_KEYS = (*PACK14_KEYS, ACQUISITION_DATE)
MONTHS_PER_YEAR = 12
SIGN_EXPECTED = {"t2_minus_t1": (1, 0), "t1_minus_t2": (0, 1), "absolute": (0, 0)}


def _bound(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.UTC)


def _month_after(day: dt.datetime) -> dt.datetime:
    index = day.year * MONTHS_PER_YEAR + day.month
    return day.replace(year=index // MONTHS_PER_YEAR, month=index % MONTHS_PER_YEAR + 1, day=1)


def spec_for(query: Query, values: tuple[tuple[str, str], ...]) -> amc_recovery.ExecutionSpec:
    """Return the recovery identity for one supplementary query."""
    return amc_recovery.ExecutionSpec(
        name=query.name,
        sql=query.sql,
        controls_off=query.controls_off,
        workflow_prefix=WORKFLOW_PREFIX,
        stem_prefix=STEM_PREFIX,
        parameter_values=values,
    )


def validate(query: Query, window_start: str, window_end: str, *, today: dt.date) -> None:
    """Apply the step A window rule, plus the fence's complete 14-day extension."""
    validate_window(query, window_start, window_end, today=today)
    if query.name in FENCE_QUERIES:
        extended = _bound(window_end).date() + dt.timedelta(days=EXTENSION_DAYS)
        if extended > today - dt.timedelta(days=AVAILABILITY_LAG_DAYS):
            raise SystemExit("the 14-day window extension after --to is not yet complete")


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
    """Recover or submit one query on the Sandbox and poll it to a terminal state."""
    query = SUPPLEMENTARY[str(args.query)]
    window = (str(args.window_start), str(args.window_end))
    validate(query, *window, today=dt.datetime.now(tz=dt.UTC).date())
    values = parameter_values(query, args)
    api, instance_id = connector(SANDBOX)
    since = str(
        args.recover_since
        or (dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    )
    print(f"instance: {SANDBOX}  query: {query.name}  window: {window[0]} .. {window[1]}")
    plan = amc_recovery.resolve_execution(
        amc_recovery.ExecutionRequest(
            api=api,
            instance_id=instance_id,
            instance_type=SANDBOX,
            spec=spec_for(query, values),
            window_start=window[0],
            window_end=window[1],
            recover_since=since,
            findings=findings,
            allow_parameters=True,
        )
    )
    terminal = amc_recovery.collect_terminal(api, plan, byte_limit=RESULT_BYTE_LIMIT)
    summary: dict[str, object] = {
        "query": query.name,
        "label": STRUCTURAL_LABEL
        if query.name in FENCE_QUERIES
        else "pack 14 at acquisition-date grain",
        "instance_id": plan.instance_id,
        "instance_type": SANDBOX,
        "execution_id": plan.execution_id,
        "execution_source": plan.source,
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
            row_count=len(parsed.rows),
            suppressed_count=parsed.suppressed_count,
            suppression_columns=parsed.suppression_columns,
            defects=list(parsed.defects),
        )
    (findings / f"{plan.stem}.summary.json").write_text(json.dumps(summary, indent=2))
    print(
        f"  {query.name:5s} status={terminal.status} source={plan.source} "
        f"suppressed={'yes' if summary.get('suppressed_count') else 'no'} "
        f"defects={summary.get('defects', [])}"
    )
    return 0 if terminal.status == "SUCCEEDED" and not summary.get("defects") else 1


def _span(window: tuple[str, str]) -> str:
    return f"{window[0][:10].replace('-', '')}-{window[1][:10].replace('-', '')}"


def load(
    findings: pathlib.Path, name: str, window: tuple[str, str] | None
) -> tuple[Parsed | None, str]:
    """Return one query's parsed result and instance ID, matched on the current SQL digest."""
    if window is None:
        return None, ""
    query = SUPPLEMENTARY[name]
    digest = amc_recovery.sql_digest(query.sql)
    pattern = f"{STEM_PREFIX}-sandbox-{name.lower()}-*-{_span(window)}.summary.json"
    for path in sorted(findings.glob(pattern)):
        summary = cast("dict[str, object]", json.loads(path.read_text()))
        if summary.get("sql_digest") != digest or summary.get("terminal_status") != "SUCCEEDED":
            continue
        csv_text = path.with_name(path.name.replace(".summary.json", ".csv")).read_text()
        parsed = parse_result(
            csv_text, query.columns, window=window, controls_off=query.controls_off
        )
        return parsed, str(summary.get("instance_id", ""))
    return None, ""


def _not_run(criterion: str) -> CriterionResult:
    return CriterionResult(criterion, INCONCLUSIVE, exercised=False, detail={"not_run": True})


def sign_cross_check(q4: Parsed | None, convention: str = RECORDED_CONVENTION) -> CriterionResult:
    """Q4's two sign branches against the convention criterion 8b recorded from Q0."""
    if q4 is None:
        return _not_run("8b.cross_check")
    diag = {
        row.get("exposure_source_set", "").strip(): row.get("shoppers_reached", "").strip()
        for row in q4.rows
        if row.get("exposure_source_set", "").strip() in PACK5_DIAG_LABELS
    }

    def value(name: str) -> Decimal:
        text = diag.get(name, "")
        return Decimal(text) if text else Decimal(0)

    multi = value("diag_multi_hour_shoppers")
    want = SIGN_EXPECTED[convention]
    agrees = (value("diag_sign_check"), value("diag_sign_check_mirror")) == (
        want[0] * multi,
        want[1] * multi,
    )
    return counted(
        "8b.cross_check", exercised=multi >= 1, violations=int(multi >= 1 and not agrees)
    )


def _paired(
    on: tuple[Parsed | None, str], off: tuple[Parsed | None, str]
) -> tuple[Parsed, Parsed] | None:
    if on[0] is None or off[0] is None or on[1] != off[1]:
        return None
    return on[0], off[0]


def fence_structural(
    findings: pathlib.Path, week: tuple[str, str], month: tuple[str, str] | None
) -> dict[str, object]:
    """Criteria 5, 6, 8, 11, the 8b cross-check and 7's pack 5 half, on the structural text."""
    on, off = load(findings, "Q3s", week), load(findings, "Q4s", week)
    monthly = load(findings, "Q4ms", month)
    pair = _paired(on, off)
    criteria: list[CriterionResult] = []
    held = on[0].suppressed_count if on[0] is not None else 0
    exercise = counted("5.exercised", exercised=held > 0, violations=0, suppressed=held)
    if pair is None:
        criteria.append(_not_run("5"))
    else:
        groups = group_rows(pair[1], "exposure_source_set", PACK5_DIAG_LABELS)
        exact = exactness(pair[0].rows, groups, PACK5_KEYS, counts=PACK5_COUNTS)
        one = counted("5.1", exercised=exact.checked > 0, violations=exact.mismatched)
        four = counted("5.4", exercised=exact.checked > 0, violations=exact.absent)
        two = criterion_2_restated(
            pair[0].rows, source_keys=(), derived_keys=PACK5_KEYS, label="5.2"
        )
        criteria.extend(combine("5", (one, two, four), exercise))
    eight = criterion_8(off[0])
    if eight.outcome == INCONCLUSIVE and monthly[0] is not None:
        rerun = criterion_8(monthly[0])
        eight = CriterionResult(
            "8", rerun.outcome, rerun.exercised, {**rerun.detail, "calendar_month_rerun": True}
        )
    bar = criterion_11(on[0], label="Q3s")
    seven = criterion_7(
        Inputs(
            {"Q3": on[0], "Q4": off[0]},
            instances={"Q3": on[1], "Q4": off[1]},
        ),
        bar_pack5=bar_of(bar),
        bar_pack14=bar_of(bar),
    )
    criteria.extend((exercise, criterion_6(off[0]), eight, sign_cross_check(off[0]), bar, seven))
    return {
        "label": STRUCTURAL_LABEL,
        "counts_as_ac_e15_pass": False,
        "ac_e15": "pending Step B",
        "criteria": {item.id: asdict(item) for item in criteria},
    }


def _state(rows: Sequence[dict[str, str]] | Sequence[object], column: str) -> str:
    values = [cast("dict[str, str]", row).get(column, "").strip() for row in rows]
    if not values:
        return "no_rows"
    filled = sum(1 for value in values if value)
    return "all_null" if filled == 0 else "all_present" if filled == len(values) else "mixed"


def retention_daily(findings: pathlib.Path, cohort: tuple[str, str]) -> dict[str, object]:
    """Criteria 0c and 5c, plus 3, 11 and 7's pack 14 half, on pack 14 at acquisition-date grain."""
    on, off = load(findings, "Q7d", cohort), load(findings, "Q8d", cohort)
    held = on[0].suppressed_count if on[0] is not None else 0
    zero_c = counted("0c", exercised=held > 0, violations=0, suppressed=held)
    pair = _paired(on, off)
    shape: dict[str, object] = {}
    criteria: list[CriterionResult] = [zero_c]
    if pair is None:
        criteria.append(_not_run("5c"))
    else:
        exact = exactness(pair[0].rows, pair[1].rows, PACK14_DAILY_KEYS, counts=PACK14_COUNTS)
        one = counted(
            "5c.1",
            exercised=exact.checked > 0,
            violations=exact.mismatched,
            checked=exact.checked,
            duplicate_reference_keys=exact.duplicate_reference_keys,
        )
        four = counted("5c.4", exercised=exact.checked > 0, violations=exact.absent)
        two = criterion_2_restated(
            pair[0].rows, source_keys=(), derived_keys=PACK14_DAILY_KEYS, label="5c.2"
        )
        criteria.extend(combine("5c", (one, two, four), zero_c))
        criteria.append(
            criterion_3(*pair, PACK14_DAILY_KEYS, reach="cohort_buyers_new_to_brand_12mo")
        )
        rows = [row for row in pair[0].rows if suppressed(row)]
        shape = {
            "suppressed_keys": {key: _state(rows, key) for key in PACK14_DAILY_KEYS},
            "suppressed_counts": {name: _state(rows, name) for name in PACK14_COUNTS},
            "labels": withheld_labels_matched(pair[0], pair[1].rows, PACK14_DAILY_KEYS),
        }
    bar = criterion_11(on[0], label="Q7d")
    seven = criterion_7(
        Inputs({"Q7": on[0], "Q8": off[0]}, instances={"Q7": on[1], "Q8": off[1]}),
        bar_pack5=bar_of(bar),
        bar_pack14=bar_of(bar),
        pack14_keys=PACK14_DAILY_KEYS,
    )
    criteria.extend((bar, seven))
    by_id = {item.id: item for item in criteria}
    passed = by_id["0c"].outcome == PASS and by_id["5c"].outcome == PASS
    return {
        "label": "pack 14 at acquisition-date grain (one added grain key)",
        "criteria": {key: asdict(value) for key, value in by_id.items()},
        "shape": shape,
        "sub_gate_2": PASS if passed else by_id["5c"].outcome,
        "packs_14_15_policy": (
            "discard_suppressed_rows (monthly cohorts)" if passed else "unchanged"
        ),
    }


def evaluate(args: argparse.Namespace, *, findings: pathlib.Path = FINDINGS) -> int:
    """Evaluate both checks offline from local findings and write one verdict."""
    start = _bound(str(args.week_from))
    week = (
        str(args.week_from),
        (start + dt.timedelta(days=WEEK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S"),
    )
    month = None
    if args.month_from:
        month_start = _bound(str(args.month_from))
        month = (str(args.month_from), _month_after(month_start).strftime("%Y-%m-%dT%H:%M:%S"))
    verdict: dict[str, object] = {
        "evaluated_at": dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "evidence_class": "sandbox",
        "fence_structural": fence_structural(findings, week, month),
    }
    if args.cohort_from and args.cohort_to:
        verdict["retention_daily"] = retention_daily(
            findings, (str(args.cohort_from), str(args.cohort_to))
        )
    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    findings.mkdir(parents=True, exist_ok=True)
    path = findings / f"{STEM_PREFIX}-verdict-{stamp}.json"
    path.write_text(json.dumps(verdict, indent=2, default=str))
    for check in ("fence_structural", "retention_daily"):
        block = cast("dict[str, object]", verdict.get(check, {}))
        criteria = cast("dict[str, dict[str, object]]", block.get("criteria", {}))
        print(f"{check} ({block.get('label', '-')}):")
        for key, value in criteria.items():
            print(f"  criterion {key:16s} {value['outcome']}")
    print(f"verdict: {path.name}")
    return 0


def parser() -> argparse.ArgumentParser:
    """Build the `run` and `evaluate` command line."""
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--query", choices=tuple(SUPPLEMENTARY), required=True)
    run_parser.add_argument("--from", dest="window_start", required=True)
    run_parser.add_argument("--to", dest="window_end", required=True)
    run_parser.add_argument("--marketplace-name")
    run_parser.add_argument("--as-of-month")
    run_parser.add_argument("--recover-since")
    run_parser.set_defaults(dsp_campaign_country=None)
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
