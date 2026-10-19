# SPDX-License-Identifier: MIT-0
# ruff: noqa: S608 -- static probe SQL assembled from fixed parts; no value is interpolated
"""P-BO-3 (demographic segments) and P-BO-4 (segment-ID equality, ASI ID space), on STANDARD.

Source validation, not a governed producer. The queries and pass criteria are owned by the
business-outcomes design §8; the owner approved running them on the internal STANDARD demo
instance on 2026-10-09 (S-5), which makes the result validation-instance evidence, not customer
evidence. Controls are always on. Three deliberate departures from the design's literal text, each
for a recorded reason:

- The ASI table is the `_snapshot` view (`audience_segments_amer_<category>_snapshot`), the one
  packs 10 and 11 read and the one whose columns resolved live on 2026-10-08. The default view
  wasn't probed.
- `segment_marketplace_id = 1` is a literal: it is the US value AMC's off-Amazon conversions
  playbook filters on (docs/sources/amc.md "ASI segment marketplace", read 2026-10-08), and the
  governed `amc.segment_marketplace_id` alias. Step 0 doesn't run, because the docs state it.
  Step 1 adds one probe-only column, `asi_segments`, so an empty filter can't read as a mismatch.
- Step 1 joins the two segment IDs without `CAST`: both are `INTEGER` in the column registry, and
  the AMC SQL reference names no type for `CAST`. Step 2 groups ASI membership by its own
  `user_id_type`, the design's form when the table carries that column.

Segment names, IDs and counts stay in `findings/`. P-BO-3's regex candidates go to a findings
file for a reviewer; `evaluate` counts only candidates the reviewer confirmed in
`findings/bo-aud-pbo3-review.json` (`{"confirmed": {"<name>": "age|gender|income"}}`).

    set -a; source validation/.env.local; set +a
    uv run python validation/probe_bo_audience_segments.py run --query pbo3 \
      --from <YYYY-MM-01T00:00:00> --to <next YYYY-MM-01T00:00:00>
    uv run python validation/probe_bo_audience_segments.py evaluate \
      --month-from <YYYY-MM-01T00:00:00> --week-from <Monday>T00:00:00
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

import amc_recovery
from bo_probe_criteria import FAIL, INCONCLUSIVE, PASS, Parsed, number, parse_result, suppressed

FINDINGS = pathlib.Path(__file__).parent / "findings"
STEM_PREFIX = "bo-aud"
WORKFLOW_PREFIX = "probe-bo-aud"
RESULT_BYTE_LIMIT = 50_000_000
STANDARD = "STANDARD"
US_SEGMENT_MARKETPLACE_ID = 1
"""Documented, not observed: the off-Amazon conversions playbook (read 2026-10-08)."""
WINDOW_COLUMNS = ("time_window_start", "time_window_end")
_WINDOW = (
    "BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,\n"
    "       BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end"
)
MONTHS_PER_YEAR = 12
WEEK_DAYS = 7
THURSDAY = 3
RETENTION_DAYS = 395
AVAILABILITY_LAG_DAYS = 2
ID_MATCH_FLOOR = 10
ID_MATCH_SHARE = Decimal("0.5")
CSV_ESCAPE = "\\"
"""AMC escaped a double quote inside segment descriptions as a backslash-quote (2026-10-09)."""

PBO3_SQL = f"""
SELECT behavior_segment_name, behavior_segment_description,
       COUNT(DISTINCT user_id) AS shoppers,
       {_WINDOW}
FROM dsp_impressions_by_user_segments
GROUP BY behavior_segment_name, behavior_segment_description
"""  # nosec B608 # A-SQL-001
PBO3_COLUMNS = (
    "behavior_segment_name",
    "behavior_segment_description",
    "shoppers",
    *WINDOW_COLUMNS,
)


def _step1(table: str) -> str:
    return f"""
WITH dsp AS (
  SELECT behavior_segment_id AS segment_id, behavior_segment_name AS segment_name
  FROM dsp_impressions_by_matched_segments
  GROUP BY behavior_segment_id, behavior_segment_name
),
asi AS (
  SELECT segment_id, segment_name
  FROM {table}
  WHERE segment_marketplace_id = {US_SEGMENT_MARKETPLACE_ID}
  GROUP BY segment_id, segment_name
),
asi_count AS (SELECT COUNT(DISTINCT segment_id) AS asi_segments FROM asi)
SELECT
  COUNT(DISTINCT dsp.segment_id) AS dsp_segments,
  COUNT(DISTINCT CASE WHEN asi_id.segment_id IS NOT NULL THEN dsp.segment_id END) AS id_matches,
  COUNT(DISTINCT CASE WHEN asi_id.segment_name = dsp.segment_name THEN dsp.segment_id END)
    AS id_and_name_matches,
  COUNT(DISTINCT CASE WHEN asi_name.segment_id IS NOT NULL
                       AND asi_name.segment_id <> dsp.segment_id THEN dsp.segment_name END)
    AS name_matches_different_id,
  MAX(c.asi_segments) AS asi_segments,
  {_WINDOW}
FROM dsp
LEFT JOIN asi asi_id ON dsp.segment_id = asi_id.segment_id
LEFT JOIN asi asi_name ON dsp.segment_name = asi_name.segment_name
CROSS JOIN asi_count c
"""  # nosec B608 # A-SQL-001


STEP1_COLUMNS = (
    "dsp_segments",
    "id_matches",
    "id_and_name_matches",
    "name_matches_different_id",
    "asi_segments",
    *WINDOW_COLUMNS,
)
STEP2_SQL = f"""
SELECT user_id_type, COUNT(DISTINCT user_id) AS members,
       {_WINDOW}
FROM audience_segments_amer_inmarket_snapshot
WHERE segment_marketplace_id = {US_SEGMENT_MARKETPLACE_ID}
GROUP BY user_id_type
"""  # nosec B608 # A-SQL-001
STEP2_COLUMNS = ("user_id_type", "members", *WINDOW_COLUMNS)


@dataclass(frozen=True, slots=True)
class Probe:
    """One query: its SQL, expected header and window rule (`month` or `snapshot_week`)."""

    name: str
    sql: str
    columns: tuple[str, ...]
    window: str


PROBES = {
    probe.name: probe
    for probe in (
        Probe("pbo3", PBO3_SQL, PBO3_COLUMNS, "month"),
        Probe(
            "pbo4-step1-inmarket",
            _step1("audience_segments_amer_inmarket_snapshot"),
            STEP1_COLUMNS,
            "month",
        ),
        Probe(
            "pbo4-step1-lifestyle",
            _step1("audience_segments_amer_lifestyle_snapshot"),
            STEP1_COLUMNS,
            "month",
        ),
        Probe("pbo4-step2", STEP2_SQL, STEP2_COLUMNS, "snapshot_week"),
    )
}

CATEGORY_PATTERNS = {
    "age": re.compile(r"(?i)(\bage[ds]?\b|\b\d{2}\s*[-\u2013]\s*\d{2}\b|\b\d{2}\+)"),
    "gender": re.compile(r"(?i)(\bgender\b|\b(fe)?male\b|\b(wo)?men\b)"),
    "income": re.compile(r"(?i)(household income|\bhhi\b|\bincome\b)"),
}
"""Design §8 P-BO-3's one pattern, split into its three categories with the same alternatives."""
DESIGN_PATTERN = re.compile(
    r"(?i)(\bage[ds]?\b|\b\d{2}\s*[-\u2013]\s*\d{2}\b|\b\d{2}\+|\bgender\b|\b(fe)?male\b|\b(wo)?men\b"
    r"|household income|\bhhi\b|\bincome\b)"
)


def spec_for(probe: Probe) -> amc_recovery.ExecutionSpec:
    """Return the recovery identity for one probe (controls always on)."""
    return amc_recovery.ExecutionSpec(
        name=probe.name,
        sql=probe.sql,
        controls_off=False,
        workflow_prefix=WORKFLOW_PREFIX,
        stem_prefix=STEM_PREFIX,
    )


def _bound(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.UTC)
    except ValueError as error:
        raise SystemExit(f"bound {value!r} must be YYYY-MM-DDT00:00:00 (UTC)") from error
    if parsed.time() != dt.time(0):
        raise SystemExit(f"bound {value!r} must fall on a UTC midnight")
    return parsed


def _month_after(day: dt.datetime) -> dt.datetime:
    index = day.year * MONTHS_PER_YEAR + day.month
    return day.replace(year=index // MONTHS_PER_YEAR, month=index % MONTHS_PER_YEAR + 1, day=1)


def first_thursday(day: dt.date) -> dt.date:
    """Return the ASI snapshot date for the month containing `day`."""
    first = day.replace(day=1)
    return first + dt.timedelta(days=(THURSDAY - first.weekday()) % WEEK_DAYS)


def validate(probe: Probe, window_start: str, window_end: str, *, today: dt.date) -> None:
    """Refuse a window that breaks the probe's rule, before any discovery or AMC call."""
    start, end = _bound(window_start), _bound(window_end)
    if start.date() < today - dt.timedelta(days=RETENTION_DAYS):
        raise SystemExit("--from falls outside AMC's 13-month retention")
    if end.date() > today - dt.timedelta(days=AVAILABILITY_LAG_DAYS):
        raise SystemExit("--to must end at least 48 hours before today")
    if probe.window == "month":
        if start.day != 1 or end != _month_after(start):
            raise SystemExit(f"{probe.name} needs one calendar month: the 1st to the next 1st")
        return
    if start.weekday() != 0 or end - start != dt.timedelta(days=WEEK_DAYS):
        raise SystemExit(f"{probe.name} needs one ISO week: a Monday to the next Monday")
    snapshots = {first_thursday(start.date()), first_thursday(end.date())}
    if not any(start.date() <= day < end.date() for day in snapshots):
        raise SystemExit(f"{probe.name}'s week must include a first Thursday (the ASI snapshot)")


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
    """Recover or submit one probe on STANDARD and poll it to a terminal state."""
    probe = PROBES[str(args.query)]
    window = (str(args.window_start), str(args.window_end))
    validate(probe, *window, today=dt.datetime.now(tz=dt.UTC).date())
    api, instance_id = connector(STANDARD)
    since = str(
        args.recover_since
        or (dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    )
    print(f"instance: {STANDARD}  query: {probe.name}  window: {window[0]} .. {window[1]}")
    plan = amc_recovery.resolve_execution(
        amc_recovery.ExecutionRequest(
            api=api,
            instance_id=instance_id,
            instance_type=STANDARD,
            spec=spec_for(probe),
            window_start=window[0],
            window_end=window[1],
            recover_since=since,
            findings=findings,
        )
    )
    terminal = amc_recovery.collect_terminal(api, plan, byte_limit=RESULT_BYTE_LIMIT)
    summary: dict[str, object] = {
        "query": probe.name,
        "instance_id": plan.instance_id,
        "instance_type": STANDARD,
        "evidence_class": "validation_instance",
        "execution_id": plan.execution_id,
        "execution_source": plan.source,
        "sql_digest": amc_recovery.sql_digest(probe.sql),
        "window_start": window[0],
        "window_end": window[1],
        "terminal_status": terminal.status,
        "status_reason": terminal.reason,
    }
    if terminal.csv_text is not None:
        parsed = parse_result(
            terminal.csv_text,
            probe.columns,
            window=window,
            controls_off=False,
            escapechar=CSV_ESCAPE,
        )
        summary.update(
            row_count=len(parsed.rows),
            suppressed_count=parsed.suppressed_count,
            defects=list(parsed.defects),
        )
    (findings / f"{plan.stem}.summary.json").write_text(json.dumps(summary, indent=2))
    print(
        f"  {probe.name} status={terminal.status} source={plan.source} "
        f"defects={summary.get('defects', [])}"
    )
    return 0 if terminal.status == "SUCCEEDED" and not summary.get("defects") else 1


def _span(window: tuple[str, str]) -> str:
    return f"{window[0][:10].replace('-', '')}-{window[1][:10].replace('-', '')}"


def load(
    findings: pathlib.Path, name: str, window: tuple[str, str] | None
) -> tuple[str | None, Parsed | None]:
    """Return one probe's terminal status and parsed result, matched on the current digest."""
    if window is None:
        return None, None
    probe = PROBES[name]
    digest = amc_recovery.sql_digest(probe.sql)
    pattern = f"{STEM_PREFIX}-standard-{name}-*-{_span(window)}.summary.json"
    for path in sorted(findings.glob(pattern)):
        summary = cast("dict[str, object]", json.loads(path.read_text()))
        if summary.get("sql_digest") != digest:
            continue
        status = str(summary.get("terminal_status", ""))
        csv_path = path.with_name(path.name.replace(".summary.json", ".csv"))
        if status != "SUCCEEDED" or not csv_path.exists():
            return status, None
        return status, parse_result(
            csv_path.read_text(),
            probe.columns,
            window=window,
            controls_off=False,
            escapechar=CSV_ESCAPE,
        )
    return None, None


def pbo3_candidates(parsed: Parsed) -> dict[str, list[str]]:
    """Segment names whose name or description matches each category's alternatives."""
    found: dict[str, list[str]] = {category: [] for category in CATEGORY_PATTERNS}
    for row in parsed.rows:
        if suppressed(row):
            continue
        name = row.get("behavior_segment_name", "").strip()
        text = f"{name} {row.get('behavior_segment_description', '').strip()}"
        if not name or not DESIGN_PATTERN.search(text):
            continue
        for category, pattern in CATEGORY_PATTERNS.items():
            if pattern.search(text):
                found[category].append(name)
    return {category: sorted(set(names)) for category, names in found.items()}


def pbo3_outcome(
    candidates: Mapping[str, Sequence[str]], review: Mapping[str, str] | None
) -> dict[str, object]:
    """Presence per category from reviewer-confirmed candidates; pending without a review."""
    if review is None:
        return {
            "outcome": "pending_review",
            "candidates": {k: len(v) for k, v in candidates.items()},
        }
    named = {name for names in candidates.values() for name in names}
    stray = sorted(set(review) - named)
    confirmed = {
        category: sum(1 for name in names if review.get(name) == category)
        for category, names in candidates.items()
    }
    present = {category: count > 0 for category, count in confirmed.items()}
    return {
        "outcome": "present" if any(present.values()) else "absent",
        "present_by_category": present,
        "confirmed_by_category": confirmed,
        "candidates": {k: len(v) for k, v in candidates.items()},
        "review_names_not_candidates": len(stray),
    }


def step1_outcome(parsed: Parsed | None, status: str | None) -> dict[str, object]:
    """Design §8 P-BO-4 step 1 for one category table."""
    if parsed is None:
        return {"outcome": INCONCLUSIVE, "terminal_status": status or "not_run"}
    rows = [row for row in parsed.rows if not suppressed(row)]
    if len(rows) != 1:
        return {"outcome": INCONCLUSIVE, "reason": "no single unsuppressed row"}
    row = rows[0]
    values = {
        name: number(row, name) or Decimal(0)
        for name in ("dsp_segments", "id_matches", "id_and_name_matches",
                     "name_matches_different_id", "asi_segments")
    }  # fmt: skip
    if values["asi_segments"] == 0 or values["dsp_segments"] == 0:
        return {"outcome": INCONCLUSIVE, "reason": "an empty segment set"}
    enough = (
        values["id_matches"] >= ID_MATCH_FLOOR
        or values["id_matches"] >= ID_MATCH_SHARE * values["dsp_segments"]
    )
    checks = {
        "id_matches_enough": enough,
        "names_agree_on_matched_ids": values["id_and_name_matches"] == values["id_matches"],
        "no_name_under_a_different_id": values["name_matches_different_id"] == 0,
    }
    return {"outcome": PASS if all(checks.values()) else FAIL, "checks": checks}


def step2_outcome(parsed: Parsed | None, status: str | None) -> dict[str, object]:
    """Record the ID spaces present in ASI membership (no pass/fail)."""
    if parsed is None:
        return {"outcome": INCONCLUSIVE, "terminal_status": status or "not_run"}
    spaces = sorted(
        {
            row.get("user_id_type", "").strip() or "NULL"
            for row in parsed.rows
            if not suppressed(row)
        }
    )
    held = parsed.suppressed_count
    if not spaces:
        return {"outcome": INCONCLUSIVE, "reason": "no unsuppressed row", "suppressed_rows": held}
    only_account = spaces == ["adUserId"]
    return {
        "outcome": "recorded",
        "id_spaces": spaces,
        "suppressed_rows": held,
        "packs_10_11": (
            "adUserId restriction stays"
            if only_account
            else "lift the restriction in a new pack version"
        ),
    }


def evaluate(args: argparse.Namespace, *, findings: pathlib.Path = FINDINGS) -> int:
    """Classify every probe offline from local findings and write one verdict."""
    month_start = _bound(str(args.month_from))
    month = (str(args.month_from), _month_after(month_start).strftime("%Y-%m-%dT%H:%M:%S"))
    week = None
    if args.week_from:
        week_start = _bound(str(args.week_from))
        week = (
            str(args.week_from),
            (week_start + dt.timedelta(days=WEEK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S"),
        )
    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    verdict: dict[str, object] = {"evidence_class": "validation_instance", "window": month}
    status, pbo3 = load(findings, "pbo3", month)
    if pbo3 is None:
        verdict["pbo3"] = {"outcome": INCONCLUSIVE, "terminal_status": status or "not_run"}
    else:
        candidates = pbo3_candidates(pbo3)
        (findings / f"{STEM_PREFIX}-pbo3-candidates-{stamp}.json").write_text(
            json.dumps(candidates, indent=2)
        )
        review_path = findings / f"{STEM_PREFIX}-pbo3-review.json"
        review = (
            cast("dict[str, str]", json.loads(review_path.read_text()).get("confirmed", {}))
            if review_path.exists()
            else None
        )
        verdict["pbo3"] = pbo3_outcome(candidates, review)
    tables: dict[str, dict[str, object]] = {}
    for category in ("inmarket", "lifestyle"):
        status, parsed = load(findings, f"pbo4-step1-{category}", month)
        tables[category] = step1_outcome(parsed, status)
    outcomes = {value["outcome"] for value in tables.values()}
    step1 = PASS if outcomes == {PASS} else FAIL if FAIL in outcomes else INCONCLUSIVE
    verdict["pbo4_step1"] = {"outcome": step1, "tables": tables}
    status, parsed = load(findings, "pbo4-step2", week)
    verdict["pbo4_step2"] = step2_outcome(parsed, status)
    path = findings / f"{STEM_PREFIX}-verdict-{stamp}.json"
    path.write_text(json.dumps(verdict, indent=2, default=str))
    for key in ("pbo3", "pbo4_step1", "pbo4_step2"):
        print(f"{key}: {cast('dict[str, object]', verdict[key])['outcome']}")
    print(f"verdict: {path.name}")
    return 0


def parser() -> argparse.ArgumentParser:
    """Build the `run` and `evaluate` command line."""
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--query", choices=tuple(PROBES), required=True)
    run_parser.add_argument("--from", dest="window_start", required=True)
    run_parser.add_argument("--to", dest="window_end", required=True)
    run_parser.add_argument("--recover-since")
    evaluate_parser = commands.add_parser("evaluate")
    evaluate_parser.add_argument("--month-from", required=True)
    evaluate_parser.add_argument("--week-from")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one command."""
    args = parser().parse_args(argv)
    return run(args) if args.command == "run" else evaluate(args)


if __name__ == "__main__":
    sys.exit(main())
