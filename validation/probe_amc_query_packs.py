# SPDX-License-Identifier: MIT-0
"""Recover or execute governed AMC query packs and persist terminal conformance evidence.

Run only after sourcing validation/.env.local. Every execution ID is journaled immediately after
submission; rerunning recovers the journal or workflow history before it can submit again.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from dataclasses import dataclass
from typing import cast

from amc_client import AdsApi, AmcError, credentials_from_environment, fetch_csv
from probe_amc_grain import discover

from adapters.producer.packs import load_query_packs
from adapters.producer.results import parse_csv_result
from domain.producer.models import AccessTier, GovernedQueryPack
from domain.producer.validation import validate_result

ROOT = pathlib.Path(__file__).resolve().parents[1]
FINDINGS = pathlib.Path(__file__).parent / "findings"
TERMINAL_WAIT_SECONDS = 21_600


def parser() -> argparse.ArgumentParser:
    """Build the durable pack-validation command line."""
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--instance-type", choices=("STANDARD", "SANDBOX"), default="STANDARD")
    result.add_argument("--pack", action="append", dest="packs")
    result.add_argument("--window-start", "--from", dest="window_start", required=True)
    result.add_argument("--window-end", "--to", dest="window_end", required=True)
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
class ExecutionPlan:
    """One journaled execution awaiting terminal evidence."""

    pack: GovernedQueryPack
    instance_id: str
    workflow_id: str
    execution_id: str
    stem: str


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    """Exact workflow history lookup performed before any submission."""

    api: AdsApi
    instance_id: str
    workflow_id: str
    since: str


def _recover(request: RecoveryRequest) -> str | None:
    matches = [
        execution
        for execution in request.api.executions_since(
            instance_id=request.instance_id,
            min_creation_time=request.since,
        )
        if _workflow_id(execution) == request.workflow_id and _execution_id(execution)
    ]
    if len(matches) > 1:
        ids = sorted(_execution_id(item) for item in matches)
        raise SystemExit(f"more than one execution matches {request.workflow_id}: {ids}")
    return None if not matches else _execution_id(matches[0])


@dataclass(frozen=True, slots=True)
class ResolveExecutionRequest:
    """Inputs for recovering or submitting one governed pack execution."""

    api: AdsApi
    instance_id: str
    instance_type: str
    pack: GovernedQueryPack
    window_start: str
    window_end: str
    since: str


def _resolve_execution(request: ResolveExecutionRequest) -> ExecutionPlan:
    api = request.api
    instance_id = request.instance_id
    instance_type = request.instance_type
    pack = request.pack
    window_start = request.window_start
    window_end = request.window_end
    since = request.since
    start_day = window_start.partition("T")[0].replace("-", "")
    end_day = window_end.partition("T")[0].replace("-", "")
    identity = pack.digest.removeprefix("sha256:")[:10]
    workflow_id = f"probe-pack-{pack.pack_id}-{identity}-{start_day}-{end_day}"
    stem = f"query-pack-{instance_type.lower()}-{pack.pack_id}-{identity}-{start_day}-{end_day}"
    journal = FINDINGS / f"{stem}.submission.json"
    execution_id: str | None = None
    if journal.exists():
        recorded = json.loads(journal.read_text())
        execution_id = str(recorded.get("execution_id", "")) or None
    if execution_id is None:
        execution_id = _recover(RecoveryRequest(api, instance_id, workflow_id, since))
    if execution_id is None:
        sandbox = instance_type == "SANDBOX"
        options = dict(pack.execution_options)
        execution_id = api.execute_adhoc(
            instance_id=instance_id,
            workflow_id=workflow_id,
            sql=pack.sql,
            window_start=window_start,
            window_end=window_end,
            require_synthetic_data=True if sandbox else None,
            disable_aggregation_controls=False if sandbox else None,
            suppression_columns=(
                options["filteredMetricsDiscriminatorColumn"],
                options["filteredReasonColumn"],
                options["distinctUserCountColumn"],
            ),
        )
        journal.write_text(
            json.dumps(
                {
                    "instance_id": instance_id,
                    "instance_type": instance_type,
                    "workflow_id": workflow_id,
                    "execution_id": execution_id,
                    "pack_id": pack.pack_id,
                    "pack_digest": pack.digest,
                    "query_digest": pack.query_digest,
                    "execution_options": dict(pack.execution_options),
                    "max_result_bytes": pack.max_result_bytes,
                    "window_start": window_start,
                    "window_end": window_end,
                },
                indent=2,
            )
        )
    print(f"  {pack.pack_id:28s} execution={execution_id}")
    return ExecutionPlan(pack, instance_id, workflow_id, execution_id, stem)


def _terminal(api: AdsApi, plan: ExecutionPlan) -> tuple[str, bool]:
    execution = api.wait_for_execution(
        instance_id=plan.instance_id,
        execution_id=plan.execution_id,
        timeout_seconds=TERMINAL_WAIT_SECONDS,
    )
    (FINDINGS / f"{plan.stem}.terminal.json").write_text(json.dumps(execution, indent=2))
    status = str(execution.get("status", "")).upper()
    summary: dict[str, object] = {
        "pack_id": plan.pack.pack_id,
        "pack_digest": plan.pack.digest,
        "query_digest": plan.pack.query_digest,
        "workflow_id": plan.workflow_id,
        "execution_id": plan.execution_id,
        "terminal_status": status,
        "status_reason": str(
            execution.get("statusReason") or execution.get("statusMessage") or ""
        ),
    }
    if status != "SUCCEEDED":
        capability_blocked = plan.pack.access_tier is AccessTier.PAID_FEATURE
        summary["outcome"] = "CAPABILITY_BLOCKED" if capability_blocked else "NOT_PLAUSIBLE"
        (FINDINGS / f"{plan.stem}.summary.json").write_text(json.dumps(summary, indent=2))
        print(f"  {plan.pack.pack_id:28s} status={status} outcome={summary['outcome']}")
        return str(summary["outcome"]), capability_blocked

    urls = api.download_urls(instance_id=plan.instance_id, execution_id=plan.execution_id)
    if not urls:
        raise AmcError(200, "successful execution returned no download URL", plan.execution_id)
    csv_text = fetch_csv(urls[0], max_bytes=20_000_000)
    (FINDINGS / f"{plan.stem}.csv").write_text(csv_text)
    result, _evidence = parse_csv_result(csv_text.encode())
    report = validate_result(plan.pack, result)
    privacy_blocked = (
        not report.valid
        and {finding.code for finding in report.findings} == {"RESULT_SUPPRESSION_REFUSED"}
    )
    summary.update(
        {
            "outcome": (
                "PLAUSIBLE"
                if report.valid
                else "PRIVACY_BLOCKED"
                if privacy_blocked
                else "NONCONFORMING_RESULT"
            ),
            "row_count": result.row_count,
            "suppressed_row_count": result.suppressed_row_count,
            "header": list(result.columns),
            "findings": [
                {"code": finding.code, "detail": finding.detail}
                for finding in report.findings
            ],
            "safety_blockers": list(plan.pack.safety_blockers),
            "reject_suppressed_rows": plan.pack.reject_suppressed_rows,
        }
    )
    (FINDINGS / f"{plan.stem}.summary.json").write_text(json.dumps(summary, indent=2))
    print(
        f"  {plan.pack.pack_id:28s} status={status} rows={result.row_count} "
        f"outcome={summary['outcome']}"
    )
    return str(summary["outcome"]), report.valid or privacy_blocked


def main(argv: list[str] | None = None) -> int:
    """Recover, submit, poll, download, and persist all selected pack evidence."""
    args = parser().parse_args(argv)
    FINDINGS.mkdir(exist_ok=True)
    packs = {pack.pack_id: pack for pack in load_query_packs(ROOT)}
    selected_ids = tuple(args.packs or sorted(packs))
    unknown = sorted(set(selected_ids) - set(packs))
    if unknown:
        raise SystemExit(f"unknown packs: {unknown}")

    api = AdsApi(credentials_from_environment())
    instance_type = str(args.instance_type)
    instance_id = discover(api, instance_type)
    since = args.recover_since or (
        dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=7)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"instance: {instance_id} ({instance_type})")
    print(f"window  : {args.window_start} .. {args.window_end}")
    print("submissions/recoveries:")
    plans = tuple(
        _resolve_execution(
            ResolveExecutionRequest(
                api=api,
                instance_id=instance_id,
                instance_type=instance_type,
                pack=packs[pack_id],
                window_start=str(args.window_start),
                window_end=str(args.window_end),
                since=since,
            )
        )
        for pack_id in selected_ids
    )

    print("terminal results:")
    required_failed = False
    for plan in plans:
        outcome, accepted = _terminal(api, plan)
        if not accepted and plan.pack.access_tier is AccessTier.STANDARD:
            required_failed = True
        if outcome == "NONCONFORMING_RESULT":
            required_failed = True
    return 1 if required_failed else 0


if __name__ == "__main__":
    sys.exit(main())
