# SPDX-License-Identifier: MIT-0
"""Probe observed campaign_country representation on the three STANDARD DSP datasets.

The query is intentionally aggregate-only and emits no campaign identifier. Results establish
structural value shape in the configured internal demo instance; they do not establish customer
volume or universal population.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import pathlib
import sys
from dataclasses import dataclass
from typing import cast

from amc_client import AdsApi, AmcError, credentials_from_environment, fetch_csv
from probe_amc_grain import SUPPRESSION, discover

FINDINGS = pathlib.Path(__file__).parent / "findings"
TABLES = {
    "dsp_clicks": """
        SELECT BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
               BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end,
               campaign_country,
               COUNT(DISTINCT campaign_id_string) AS campaign_count,
               SUM(clicks) AS activity_count
        FROM dsp_clicks
        GROUP BY 1, 2, 3
    """,
    "dsp_impressions": """
        SELECT BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
               BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end,
               campaign_country,
               COUNT(DISTINCT campaign_id_string) AS campaign_count,
               SUM(impressions) AS activity_count
        FROM dsp_impressions
        GROUP BY 1, 2, 3
    """,
    "dsp_views": """
        SELECT BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
               BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end,
               campaign_country,
               COUNT(DISTINCT campaign_id_string) AS campaign_count,
               SUM(events) AS activity_count
        FROM dsp_views
        GROUP BY 1, 2, 3
    """,
}
DAYS_BACK = 3
TERMINAL_WAIT_SECONDS = 21_600


def parser() -> argparse.ArgumentParser:
    """Build the guarded STANDARD-only probe CLI."""
    result = argparse.ArgumentParser()
    result.add_argument("--standard", action="store_true", required=True)
    result.add_argument("--table", choices=tuple(TABLES), required=True)
    result.add_argument("--window-days", type=int, default=14)
    result.add_argument("--recover-execution-id")
    result.add_argument("--recover-since")
    return result


def _execution_id(execution: dict[str, object]) -> str:
    value = execution.get("workflowExecutionId")
    return value if isinstance(value, str) else ""


def _workflow_id(execution: dict[str, object]) -> str:
    direct = execution.get("workflowId")
    if isinstance(direct, str):
        return direct
    workflow = execution.get("workflow")
    if isinstance(workflow, dict):
        value = cast("dict[str, object]", workflow).get("workflowId")
        return value if isinstance(value, str) else ""
    return ""


@dataclass(frozen=True, slots=True)
class _RecoveryRequest:
    api: AdsApi
    instance_id: str
    workflow_id: str
    since: str


def _recover(request: _RecoveryRequest) -> str | None:
    matches = [
        item
        for item in request.api.executions_since(
            instance_id=request.instance_id,
            min_creation_time=request.since,
        )
        if _workflow_id(item) == request.workflow_id and _execution_id(item)
    ]
    if len(matches) > 1:
        ids = sorted(_execution_id(item) for item in matches)
        raise SystemExit(f"more than one existing execution matches {request.workflow_id}: {ids}")
    return None if not matches else _execution_id(matches[0])


def _summary(csv_text: str) -> dict[str, object]:
    rows = tuple(csv.DictReader(io.StringIO(csv_text)))
    visible = sorted(
        {
            str(row.get("campaign_country", "")).strip()
            for row in rows
            if str(row.get("campaign_country", "")).strip()
        }
    )
    null_rows = tuple(row for row in rows if not str(row.get("campaign_country", "")).strip())
    genuine_nulls = sum(
        str(row.get("rows_suppressed", "")).strip().lower() == "false" for row in null_rows
    )
    suppressed_nulls = sum(
        str(row.get("rows_suppressed", "")).strip().lower() == "true" for row in null_rows
    )
    return {
        "row_count": len(rows),
        "visible_campaign_country_values": visible,
        "null_rows": len(null_rows),
        "genuine_null_rows": genuine_nulls,
        "suppressed_null_rows": suppressed_nulls,
    }


def main(argv: list[str] | None = None) -> int:
    """Recover or submit one narrow STANDARD execution, then persist its terminal evidence."""
    args = parser().parse_args(argv)
    if args.window_days < 1:
        raise SystemExit("--window-days must be positive")
    FINDINGS.mkdir(exist_ok=True)
    api = AdsApi(credentials_from_environment())
    instance_id = discover(api, "STANDARD")
    end = dt.datetime.now(tz=dt.UTC).date() - dt.timedelta(days=DAYS_BACK)
    start = end - dt.timedelta(days=args.window_days - 1)
    workflow_id = (
        f"probe-campaign-country-{args.table.replace('_', '-')}-{start:%Y%m%d}-{end:%Y%m%d}"
    )
    stem = f"campaign-country-{args.table}-{start:%Y%m%d}-{end:%Y%m%d}"
    journal = FINDINGS / f"{stem}.submission.json"
    execution_id = args.recover_execution_id
    if execution_id is None and journal.exists():
        recorded = json.loads(journal.read_text())
        execution_id = str(recorded.get("execution_id", "")) or None
    since = (
        args.recover_since
        or f"{dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=1):%Y-%m-%dT%H:%M:%S}"
    )
    if execution_id is None:
        execution_id = _recover(_RecoveryRequest(api, instance_id, workflow_id, since))
    if execution_id is None:
        execution_id = api.execute_adhoc(
            instance_id=instance_id,
            workflow_id=workflow_id,
            sql=TABLES[args.table],
            window_start=f"{start.isoformat()}T00:00:00",
            window_end=f"{(end + dt.timedelta(days=1)).isoformat()}T00:00:00",
            require_synthetic_data=None,
            disable_aggregation_controls=None,
            suppression_columns=SUPPRESSION,
        )
        journal.write_text(
            json.dumps(
                {
                    "instance_id": instance_id,
                    "workflow_id": workflow_id,
                    "execution_id": execution_id,
                    "table": args.table,
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "sql": " ".join(TABLES[args.table].split()),
                },
                indent=2,
            )
        )
    print(f"instance : {instance_id}")
    print(f"workflow : {workflow_id}")
    print(f"execution: {execution_id}")
    execution = api.wait_for_execution(
        instance_id=instance_id,
        execution_id=execution_id,
        timeout_seconds=TERMINAL_WAIT_SECONDS,
    )
    (FINDINGS / f"{stem}.terminal.json").write_text(json.dumps(execution, indent=2))
    status = str(execution.get("status", "")).upper()
    print(f"status   : {status}")
    if status != "SUCCEEDED":
        print(f"reason   : {execution.get('statusReason') or '(none given)'}")
        return 1
    urls = api.download_urls(instance_id=instance_id, execution_id=execution_id)
    if not urls:
        raise AmcError(200, "successful execution returned no download URL", execution_id)
    csv_text = fetch_csv(urls[0])
    (FINDINGS / f"{stem}.csv").write_text(csv_text)
    summary = _summary(csv_text)
    (FINDINGS / f"{stem}.summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
