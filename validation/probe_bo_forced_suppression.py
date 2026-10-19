# SPDX-License-Identifier: MIT-0
# ruff: noqa: S608 -- static probe SQL assembled from fixed parts; no value is interpolated
"""P-BO-1 follow-up: force suppression with a deliberately sparse grain, then read its shape.

No step A query suppressed a row (Q1/Q2, Q1r/Q2r, Q5, Q5r, Q7), so exactness under suppression was
unproven. These variants group `dsp_impressions` so finely that one-user cells must occur:

- `sparse_source`: campaign x day x hour x `device_type` x `operating_system` x `site`, all LOW
  source columns. It stands where Q1/Q2 and Q1r/Q2r stood (the source-column half).
- `sparse_derived`: a per-shopper label derived in a CTE (impressions per shopper per campaign-hour,
  bucketed), grouped by campaign x hour x bucket. It asks the derived-grouping question of design
  §8 on probe text; it is not pack 6, 7 or 14 text, so it cannot settle 0b or 0c.

Each variant runs controls on and, on the SANDBOX only, controls off as the same-instance reference.
`run` re-discovers the instance and adopts a journaled or historical execution before it can
submit (amc_recovery). `evaluate` reads only `findings/`, applies criteria 0, 1, 2, 3 and 4 with
`bo_probe_criteria` to each same-instance pair, records the suppression output shape, and writes one
verdict whose detail holds counts only. The criteria are owned by the business-outcomes design §8.

    set -a; source validation/.env.local; set +a
    uv run python validation/probe_bo_forced_suppression.py run \
      --variant sparse_source --controls on --instance-type SANDBOX \
      --from 2026-09-14T00:00:00 --to 2026-09-15T00:00:00
    uv run python validation/probe_bo_forced_suppression.py evaluate \
      --from 2026-09-14T00:00:00 --to 2026-09-15T00:00:00
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import cast

import amc_recovery
from bo_probe_criteria import (
    PASS,
    Parsed,
    criterion_0,
    criterion_3,
    exactness,
    exactness_criteria,
    number,
    parse_result,
    suppressed,
)
from probe_amc_grain import SUPPRESSION

FINDINGS = pathlib.Path(__file__).parent / "findings"
STEM_PREFIX = "bo-force"
WORKFLOW_PREFIX = "probe-bo-force"
RESULT_BYTE_LIMIT = 20_000_000
_WINDOW = (
    "BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,\n"
    "       BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end"
)
WINDOW_COLUMNS = ("time_window_start", "time_window_end")
COUNTS = ("reach_users",)
MEASURES = ("reach_users", "impressions")


@dataclass(frozen=True, slots=True)
class Variant:
    """One sparse query: its SQL, grain keys and output columns."""

    name: str
    sql: str
    keys: tuple[str, ...]

    @property
    def columns(self) -> tuple[str, ...]:
        """The analytic header, in order."""
        return (*self.keys, *MEASURES, *WINDOW_COLUMNS)


_SOURCE_KEYS = (
    "campaign_id_string",
    "impression_date_utc",
    "impression_dt_hour_utc",
    "device_type",
    "operating_system",
    "site",
)
_SOURCE_SQL = f"""
SELECT {", ".join(_SOURCE_KEYS)},
       COUNT(DISTINCT user_id) AS reach_users, SUM(impressions) AS impressions,
       {_WINDOW}
FROM dsp_impressions
GROUP BY {", ".join(_SOURCE_KEYS)}
"""  # nosec B608 # A-SQL-001
_DERIVED_KEYS = ("campaign_id_string", "impression_dt_hour_utc", "frequency_bucket")
_DERIVED_SQL = f"""
WITH per_shopper AS (
  SELECT user_id, campaign_id_string, impression_dt_hour_utc,
         SUM(impressions) AS shopper_impressions
  FROM dsp_impressions
  GROUP BY user_id, campaign_id_string, impression_dt_hour_utc
),
labelled AS (
  SELECT user_id, campaign_id_string, impression_dt_hour_utc, shopper_impressions,
         CASE WHEN shopper_impressions = 1 THEN '1'
              WHEN shopper_impressions = 2 THEN '2'
              WHEN shopper_impressions <= 4 THEN '3_4'
              ELSE '5_plus' END AS frequency_bucket
  FROM per_shopper
)
SELECT campaign_id_string, impression_dt_hour_utc, frequency_bucket,
       COUNT(DISTINCT user_id) AS reach_users, SUM(shopper_impressions) AS impressions,
       {_WINDOW}
FROM labelled
GROUP BY campaign_id_string, impression_dt_hour_utc, frequency_bucket
"""  # nosec B608 # A-SQL-001
VARIANTS = {
    variant.name: variant
    for variant in (
        Variant("sparse_source", _SOURCE_SQL, _SOURCE_KEYS),
        Variant("sparse_derived", _DERIVED_SQL, _DERIVED_KEYS),
    )
}


def spec(variant: Variant, *, controls_off: bool) -> amc_recovery.ExecutionSpec:
    """Recovery identity for one variant and controls setting."""
    return amc_recovery.ExecutionSpec(
        name=f"{variant.name.replace('_', '-')}-{'off' if controls_off else 'on'}",
        sql=variant.sql,
        controls_off=controls_off,
        workflow_prefix=WORKFLOW_PREFIX,
        stem_prefix=STEM_PREFIX,
    )


def connect(instance_type: str) -> tuple[amc_recovery.ProbeApi, str]:
    """Authenticate from the sourced environment and re-discover an instance of the type."""
    from amc_client import AdsApi, credentials_from_environment
    from probe_amc_grain import discover

    api = AdsApi(credentials_from_environment())
    return cast("amc_recovery.ProbeApi", api), discover(api, instance_type)


def shape(parsed: Parsed, variant: Variant, header: Sequence[str]) -> dict[str, object]:
    """Describe the suppression output: columns, markers, and what a suppressed row keeps."""
    rows = [row for row in parsed.rows if suppressed(row)]
    kept = [row for row in parsed.rows if not suppressed(row)]

    def state(group: Sequence[dict[str, str]] | Sequence[object], column: str) -> str:
        values = [cast("dict[str, str]", row).get(column, "").strip() for row in group]
        if not values:
            return "no_rows"
        filled = sum(1 for value in values if value)
        return "all_null" if filled == 0 else "all_present" if filled == len(values) else "mixed"

    users = [number(row, SUPPRESSION[2]) for row in kept]
    return {
        "header": list(header),
        "suppression_columns_present": parsed.suppression_columns,
        "rows": len(parsed.rows),
        "suppressed_rows": len(rows),
        "flag_values": sorted({row.get(SUPPRESSION[0], "").strip() for row in parsed.rows}),
        "reason_on_suppressed": state(rows, SUPPRESSION[1]),
        "reason_on_unsuppressed": state(kept, SUPPRESSION[1]),
        "reason_texts": sorted({row.get(SUPPRESSION[1], "").strip() for row in rows}),
        "distinct_user_count_on_suppressed": state(rows, SUPPRESSION[2]),
        "min_distinct_user_count_unsuppressed": str(
            min((u for u in users if u is not None), default="")
        ),
        "max_distinct_user_count_unsuppressed": str(
            max((u for u in users if u is not None), default="")
        ),
        "suppressed_keys": {key: state(rows, key) for key in variant.keys},
        "suppressed_measures": {name: state(rows, name) for name in MEASURES},
        "suppressed_windows": {name: state(rows, name) for name in WINDOW_COLUMNS},
    }


def run(
    args: argparse.Namespace,
    *,
    findings: pathlib.Path = FINDINGS,
    connector: Callable[[str], tuple[amc_recovery.ProbeApi, str]] = connect,
) -> int:
    """Recover or submit one variant, poll it to a terminal state, and summarise it."""
    variant = VARIANTS[str(args.variant)]
    controls_off = str(args.controls) == "off"
    instance_type = str(args.instance_type)
    if controls_off and instance_type != "SANDBOX":
        raise SystemExit("aggregation controls can be turned off only on a SANDBOX instance")
    api, instance_id = connector(instance_type)
    since = str(
        args.recover_since
        or (dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    )
    window = (str(args.window_start), str(args.window_end))
    plan = amc_recovery.resolve_execution(
        amc_recovery.ExecutionRequest(
            api=api,
            instance_id=instance_id,
            instance_type=instance_type,
            spec=spec(variant, controls_off=controls_off),
            window_start=window[0],
            window_end=window[1],
            recover_since=since,
            findings=findings,
        )
    )
    terminal = amc_recovery.collect_terminal(api, plan, byte_limit=RESULT_BYTE_LIMIT)
    summary: dict[str, object] = {
        "variant": variant.name,
        "controls_off": controls_off,
        "instance_id": instance_id,
        "instance_type": instance_type,
        "execution_source": plan.source,
        "terminal_status": terminal.status,
        "status_reason": terminal.reason,
        "sql_digest": amc_recovery.sql_digest(variant.sql),
        "window_start": window[0],
        "window_end": window[1],
    }
    if terminal.csv_text is not None:
        parsed = parse_result(
            terminal.csv_text, variant.columns, window=window, controls_off=controls_off
        )
        header = terminal.csv_text.splitlines()[0].split(",") if terminal.csv_text else []
        summary["defects"] = list(parsed.defects)
        summary["shape"] = shape(parsed, variant, header)
    (plan.findings / f"{plan.stem}.summary.json").write_text(json.dumps(summary, indent=2))
    shaped = cast("dict[str, object]", summary.get("shape", {}))
    print(
        f"{variant.name} controls={'off' if controls_off else 'on'} {instance_type}: "
        f"status={terminal.status} source={plan.source} "
        f"suppressed_rows={'yes' if shaped.get('suppressed_rows') else 'no'} "
        f"defects={summary.get('defects', [])}"
    )
    return 0 if terminal.status == "SUCCEEDED" and not summary.get("defects") else 1


def _load(
    findings: pathlib.Path, variant: Variant, *, instance_type: str, controls: str, span: str
) -> tuple[dict[str, object], Parsed] | None:
    stem = f"{STEM_PREFIX}-{instance_type.lower()}-{variant.name.replace('_', '-')}-{controls}-"
    digest = amc_recovery.sql_digest(variant.sql)
    for path in sorted(findings.glob(f"{stem}*-{span}.summary.json")):
        summary = cast("dict[str, object]", json.loads(path.read_text()))
        if summary.get("sql_digest") != digest or summary.get("terminal_status") != "SUCCEEDED":
            continue
        window = (str(summary["window_start"]), str(summary["window_end"]))
        csv_text = path.with_name(path.name.replace(".summary.json", ".csv")).read_text()
        return summary, parse_result(
            csv_text, variant.columns, window=window, controls_off=controls == "off"
        )
    return None


def evaluate(args: argparse.Namespace, *, findings: pathlib.Path = FINDINGS) -> int:
    """Apply criteria 0-4 to each same-instance pair and write one count-only verdict."""
    start, end = (
        str(value)[:10].replace("-", "") for value in (args.window_start, args.window_end)
    )
    span = f"{start}-{end}"
    verdict: dict[str, object] = {"window": [args.window_start, args.window_end], "variants": {}}
    for variant in VARIANTS.values():
        entry: dict[str, object] = {}
        for instance_type in ("SANDBOX", "STANDARD"):
            on = _load(findings, variant, instance_type=instance_type, controls="on", span=span)
            off = _load(findings, variant, instance_type=instance_type, controls="off", span=span)
            if on is None:
                continue
            item: dict[str, object] = {"controls_on_shape": on[0].get("shape")}
            if off is not None and on[0].get("instance_id") == off[0].get("instance_id"):
                item["controls_off_shape"] = off[0].get("shape")
                zero = criterion_0(on[1], off[1])
                exact = exactness(on[1].rows, off[1].rows, variant.keys, counts=COUNTS)
                one, two, four = exactness_criteria("", exact)
                item["criteria"] = {
                    result.id: asdict(result)
                    for result in (zero, one, two, four, criterion_3(on[1], off[1], variant.keys))
                }
                item["exercised"] = zero.outcome == PASS
            entry[instance_type] = item
        cast("dict[str, object]", verdict["variants"])[variant.name] = entry
    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = findings / f"{STEM_PREFIX}-verdict-{stamp}.json"
    path.write_text(json.dumps(verdict, indent=2, default=str))
    for name, entry in cast("dict[str, dict[str, dict[str, object]]]", verdict["variants"]).items():
        for instance_type, item in entry.items():
            criteria = cast("dict[str, dict[str, object]]", item.get("criteria", {}))
            outcomes = {key: value["outcome"] for key, value in criteria.items()}
            print(f"{name} {instance_type}: {outcomes or 'controls-on only'}")
    print(f"verdict: {path.name}")
    return 0


def parser() -> argparse.ArgumentParser:
    """Build the `run` and `evaluate` command line."""
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--variant", choices=tuple(VARIANTS), required=True)
    run_parser.add_argument("--controls", choices=("on", "off"), required=True)
    run_parser.add_argument("--instance-type", choices=("SANDBOX", "STANDARD"), required=True)
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
