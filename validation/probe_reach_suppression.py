# SPDX-License-Identifier: MIT-0
"""Determine whether suppression re-aggregation sums non-additive distinct reach.

Runs the same high-geography reach query without and with the discriminator. Default mode preserves
separate privacy-nulled rows; discriminator mode merges identical nulled rows. Equality between a
merged reach value and the sum of several default-mode reach values proves SUM re-aggregation.
Every execution is recovered before submission and journaled immediately.
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
from decimal import Decimal
from typing import cast

from amc_client import AdsApi, AmcError, credentials_from_environment, fetch_csv
from probe_amc_grain import SUPPRESSION, discover

FINDINGS = pathlib.Path(__file__).parent / "findings"
TERMINAL_WAIT_SECONDS = 21_600
MIN_MERGED_ROWS = 2
GEO_COLUMN = "postal_code"
DATE_COLUMN = "conversion_event_date_utc"
DETAIL_SQL = (
    "SELECT conversion_event_date_utc, campaign_id_string, postal_code, "
    "COUNT(DISTINCT user_id) AS reach_users "
    "FROM amazon_attributed_events_by_conversion_time "
    "GROUP BY conversion_event_date_utc, campaign_id_string, postal_code"
)
DIRECT_SQL = (
    "SELECT conversion_event_date_utc, campaign_id_string, "
    "COUNT(DISTINCT user_id) AS reach_users "
    "FROM amazon_attributed_events_by_conversion_time "
    "GROUP BY conversion_event_date_utc, campaign_id_string"
)


def parser() -> argparse.ArgumentParser:
    """Build the durable suppression probe command line."""
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--from", dest="window_start", required=True)
    result.add_argument("--to", dest="window_end", required=True)
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
class ProbeCase:
    """One query and suppression mode needed by the comparison."""

    name: str
    sql: str
    suppression: bool


CASES = (
    ProbeCase("detail_plain_attributed", DETAIL_SQL, False),
    ProbeCase("detail_discriminator_attributed", DETAIL_SQL, True),
    ProbeCase("direct_reach_attributed", DIRECT_SQL, True),
)


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    """Exact workflow history lookup performed before submission."""

    api: AdsApi
    instance_id: str
    workflow_id: str
    since: str


def _recover(request: RecoveryRequest) -> str | None:
    matches = [
        item
        for item in request.api.executions_since(
            instance_id=request.instance_id,
            min_creation_time=request.since,
        )
        if _workflow_id(item) == request.workflow_id and _execution_id(item)
    ]
    if len(matches) > 1:
        raise SystemExit(
            f"more than one execution matches {request.workflow_id}: "
            f"{sorted(_execution_id(item) for item in matches)}"
        )
    return None if not matches else _execution_id(matches[0])


@dataclass(frozen=True, slots=True)
class SubmitRequest:
    """Inputs for recovering or submitting one probe case."""

    api: AdsApi
    instance_id: str
    case: ProbeCase
    window_start: str
    window_end: str
    since: str


def _execution(request: SubmitRequest) -> tuple[str, str]:
    start = request.window_start.partition("T")[0].replace("-", "")
    end = request.window_end.partition("T")[0].replace("-", "")
    workflow_id = f"probe-reach-suppression-{request.case.name}-{start}-{end}"
    stem = f"reach-suppression-{request.case.name}-{start}-{end}"
    journal = FINDINGS / f"{stem}.submission.json"
    execution_id: str | None = None
    if journal.exists():
        execution_id = str(json.loads(journal.read_text()).get("execution_id", "")) or None
    if execution_id is None:
        execution_id = _recover(
            RecoveryRequest(request.api, request.instance_id, workflow_id, request.since)
        )
    if execution_id is None:
        execution_id = request.api.execute_adhoc(
            instance_id=request.instance_id,
            workflow_id=workflow_id,
            sql=request.case.sql,
            window_start=request.window_start,
            window_end=request.window_end,
            require_synthetic_data=True,
            disable_aggregation_controls=False,
            suppression_columns=SUPPRESSION if request.case.suppression else None,
        )
        journal.write_text(
            json.dumps(
                {
                    "instance_id": request.instance_id,
                    "workflow_id": workflow_id,
                    "execution_id": execution_id,
                    "case": request.case.name,
                    "window_start": request.window_start,
                    "window_end": request.window_end,
                    "sql": request.case.sql,
                    "suppression": request.case.suppression,
                },
                indent=2,
            )
        )
    print(f"  {request.case.name:24s} execution={execution_id}")
    return execution_id, stem


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """Exact terminal execution whose CSV evidence must be persisted."""

    api: AdsApi
    instance_id: str
    execution_id: str
    stem: str


def _download(request: DownloadRequest) -> str:
    api = request.api
    instance_id = request.instance_id
    execution_id = request.execution_id
    stem = request.stem
    execution = api.wait_for_execution(
        instance_id=instance_id,
        execution_id=execution_id,
        timeout_seconds=TERMINAL_WAIT_SECONDS,
    )
    (FINDINGS / f"{stem}.terminal.json").write_text(json.dumps(execution, indent=2))
    status = str(execution.get("status", "")).upper()
    print(f"  {stem:48s} status={status}")
    if status != "SUCCEEDED":
        raise AmcError(
            200,
            str(execution.get("statusReason") or execution.get("statusMessage") or status),
            execution_id,
        )
    urls = api.download_urls(instance_id=instance_id, execution_id=execution_id)
    if not urls:
        raise AmcError(200, "successful execution returned no download URL", execution_id)
    text = fetch_csv(urls[0], max_bytes=20_000_000)
    (FINDINGS / f"{stem}.csv").write_text(text)
    return text


def _key(row: dict[str, str]) -> tuple[str, str]:
    return (row.get(DATE_COLUMN, ""), row.get("campaign_id_string", ""))


def _number(row: dict[str, str]) -> Decimal:
    return Decimal(row.get("reach_users", "0") or "0")


def _analyse(plain_text: str, flagged_text: str, direct_text: str) -> dict[str, object]:
    plain = tuple(csv.DictReader(io.StringIO(plain_text)))
    flagged = tuple(csv.DictReader(io.StringIO(flagged_text)))
    direct = {_key(row): _number(row) for row in csv.DictReader(io.StringIO(direct_text))}
    plain_null: dict[tuple[str, str], list[Decimal]] = {}
    flagged_null: dict[tuple[str, str], list[Decimal]] = {}
    for row in plain:
        if not (row.get(GEO_COLUMN) or "").strip():
            plain_null.setdefault(_key(row), []).append(_number(row))
    for row in flagged:
        if (row.get(GEO_COLUMN) or "").strip():
            continue
        if (row.get("rows_suppressed") or "").strip().lower() != "true":
            continue
        flagged_null.setdefault(_key(row), []).append(_number(row))

    comparisons: list[dict[str, object]] = []
    for key, flagged_values in flagged_null.items():
        plain_values = plain_null.get(key, [])
        if len(plain_values) < MIN_MERGED_ROWS and len(flagged_values) < MIN_MERGED_ROWS:
            continue
        plain_sum = sum(plain_values, Decimal(0))
        comparisons.append(
            {
                "date": key[0],
                "campaign": key[1],
                "plain_null_rows": len(plain_values),
                "plain_reach_sum": str(plain_sum),
                "flagged_null_rows": len(flagged_values),
                "flagged_reach_values": sorted({str(value) for value in flagged_values}),
                "direct_reach": str(direct.get(key, "")),
                "single_merged_value_equals_plain_sum": (
                    len(flagged_values) == 1 and flagged_values[0] == plain_sum
                ),
            }
        )
    duplicate_groups = sum(len(values) > 1 for values in flagged_null.values())
    proves_sum = any(
        bool(item["single_merged_value_equals_plain_sum"])
        for item in comparisons
    )
    suppressed_rows = sum(len(values) for values in flagged_null.values())
    return {
        "plain_rows": len(plain),
        "flagged_rows": len(flagged),
        "direct_rows": len(direct),
        "suppressed_flagged_rows": suppressed_rows,
        "duplicate_suppressed_grain_groups": duplicate_groups,
        "discriminator_reduced_row_count": len(flagged) < len(plain),
        "proves_sum_reaggregation": proves_sum,
        "preserves_duplicate_nulled_grain_rows": duplicate_groups > 0,
        "requires_non_additive_suppression_refusal": suppressed_rows > 0,
        "comparison_count": len(comparisons),
        "comparisons": comparisons[:50],
    }


def main(argv: list[str] | None = None) -> int:
    """Recover, submit, poll, compare, and persist the non-additive reach finding."""
    args = parser().parse_args(argv)
    FINDINGS.mkdir(exist_ok=True)
    api = AdsApi(credentials_from_environment())
    instance_id = discover(api, "SANDBOX")
    since = args.recover_since or (
        dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=7)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"instance: {instance_id} (SANDBOX)")
    print(f"window  : {args.window_start} .. {args.window_end}")
    planned = tuple(
        (
            case,
            *_execution(
                SubmitRequest(
                    api,
                    instance_id,
                    case,
                    str(args.window_start),
                    str(args.window_end),
                    since,
                )
            ),
        )
        for case in CASES
    )
    results = {
        case.name: _download(DownloadRequest(api, instance_id, execution_id, stem))
        for case, execution_id, stem in planned
    }
    summary = _analyse(
        results["detail_plain_attributed"],
        results["detail_discriminator_attributed"],
        results["direct_reach_attributed"],
    )
    stem = (
        f"reach-suppression-summary-{str(args.window_start)[:10].replace('-', '')}-"
        f"{str(args.window_end)[:10].replace('-', '')}"
    )
    (FINDINGS / f"{stem}.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0 if summary["requires_non_additive_suppression_refusal"] else 1


if __name__ == "__main__":
    sys.exit(main())
