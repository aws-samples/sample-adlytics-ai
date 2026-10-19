# SPDX-License-Identifier: MIT-0
"""Recovery-first AMC execution shared by the business-outcome probes.

Generalises `probe_deduplicated_attribution.resolve_execution` and `collect_terminal`: journal,
then filtered history, then submit and journal immediately, before any poll. The execution
identity covers the normalized SQL, the aggregation-controls flag and the parameter values, so
the same SQL with controls on and off can never adopt each other's execution.

Only the new probes use this module; the older probes keep their own copies unchanged.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass
from typing import Protocol, cast

from amc_client import AmcError, fetch_csv
from probe_amc_grain import SUPPRESSION

SANDBOX = "SANDBOX"
TERMINAL_WAIT_SECONDS = 21_600


class ProbeApi(Protocol):
    """The AMC execution surface recovery-first orchestration needs."""

    def executions_since(
        self, *, instance_id: str, min_creation_time: str
    ) -> list[dict[str, object]]:
        """List executions created since an exact AMC timestamp."""
        ...

    def execute_adhoc(
        self,
        *,
        instance_id: str,
        workflow_id: str,
        sql: str,
        window_start: str,
        window_end: str,
        require_synthetic_data: bool | None,
        disable_aggregation_controls: bool | None,
        suppression_columns: tuple[str, str, str],
        parameter_values: dict[str, str] | None = None,
    ) -> str:
        """Submit one exact ad hoc workflow."""
        ...

    def wait_for_execution(
        self, *, instance_id: str, execution_id: str, timeout_seconds: int
    ) -> dict[str, object]:
        """Wait for one execution to reach a terminal state."""
        ...

    def download_urls(self, *, instance_id: str, execution_id: str) -> list[str]:
        """Return presigned downloads for one successful execution."""
        ...


@dataclass(frozen=True, slots=True)
class ExecutionSpec:
    """One exact query to recover or submit.

    A spec with `parameter_values` is submitted only when the request sets `allow_parameters`,
    which a caller does once the ad hoc `inputParameters` form is confirmed (or, for the shape
    probe itself, to test it).
    """

    name: str
    sql: str
    controls_off: bool
    workflow_prefix: str
    stem_prefix: str
    parameter_values: tuple[tuple[str, str], ...] = ()


def normalized_sql(sql: str) -> str:
    """Render exactly what AdsApi submits."""
    return " ".join(sql.split())


def identity_digest(spec: ExecutionSpec) -> str:
    """Digest of SQL, controls and parameters: the execution's identity."""
    material = "\n".join(
        (
            normalized_sql(spec.sql),
            "controls_off" if spec.controls_off else "controls_on",
            json.dumps(sorted(spec.parameter_values)),
        )
    )
    return f"sha256:{hashlib.sha256(material.encode()).hexdigest()}"


def sql_digest(sql: str) -> str:
    """Digest of the normalized SQL alone, which a later slice cites to ship identical text."""
    return f"sha256:{hashlib.sha256(normalized_sql(sql).encode()).hexdigest()}"


def _day(value: str) -> str:
    return value.partition("T")[0].replace("-", "")


def names(
    spec: ExecutionSpec, instance_type: str, *, window_start: str, window_end: str
) -> tuple[str, str]:
    """Return the workflow ID and the findings stem for one execution."""
    identity = identity_digest(spec).removeprefix("sha256:")[:10]
    query = spec.name.lower()
    span = f"{_day(window_start)}-{_day(window_end)}"
    workflow_id = f"{spec.workflow_prefix}-{query}-{identity}-{span}"
    stem = f"{spec.stem_prefix}-{instance_type.lower()}-{query}-{identity}-{span}"
    return workflow_id, stem


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Everything needed to recover or submit one execution."""

    api: ProbeApi
    instance_id: str
    instance_type: str
    spec: ExecutionSpec
    window_start: str
    window_end: str
    recover_since: str
    findings: pathlib.Path
    allow_parameters: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """One journaled or recovered server-side execution."""

    instance_id: str
    instance_type: str
    spec: ExecutionSpec
    workflow_id: str
    execution_id: str
    digest: str
    stem: str
    findings: pathlib.Path
    source: str


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


def _recover(request: ExecutionRequest, workflow_id: str) -> str | None:
    matches = [
        execution
        for execution in request.api.executions_since(
            instance_id=request.instance_id, min_creation_time=request.recover_since
        )
        if _workflow_id(execution) == workflow_id and _execution_id(execution)
    ]
    if len(matches) > 1:
        ids = sorted(_execution_id(item) for item in matches)
        raise SystemExit(f"more than one execution matches {workflow_id}: {ids}")
    return None if not matches else _execution_id(matches[0])


def _controls(instance_type: str, controls_off: bool) -> tuple[bool | None, bool | None]:
    """Return (requireSyntheticData, disableAggregationControls) for the target type."""
    if instance_type == SANDBOX:
        return True, controls_off
    if controls_off:
        raise SystemExit("aggregation controls can be turned off only on a SANDBOX instance")
    return None, None


def resolve_execution(request: ExecutionRequest) -> ExecutionPlan:
    """Adopt a journaled or historical execution before submitting an exact new one."""
    spec = request.spec
    synthetic, disable = _controls(request.instance_type, spec.controls_off)
    if spec.parameter_values and not request.allow_parameters:
        raise SystemExit(
            f"{spec.name} needs CUSTOM_PARAMETER values, and the ad hoc parameter request shape "
            "is unconfirmed; refusing to guess it (see validation/README.md)."
        )
    digest = identity_digest(spec)
    workflow_id, stem = names(
        spec,
        request.instance_type,
        window_start=request.window_start,
        window_end=request.window_end,
    )
    request.findings.mkdir(parents=True, exist_ok=True)
    journal = request.findings / f"{stem}.submission.json"
    execution_id: str | None = None
    source = "submission"
    if journal.exists():
        recorded = cast("dict[str, object]", json.loads(journal.read_text()))
        if str(recorded.get("instance_id", "")) != request.instance_id:
            raise SystemExit(f"{journal.name} names another instance; the target changed")
        if str(recorded.get("identity_digest", "")) != digest:
            raise SystemExit(f"{journal.name} records a different identity digest")
        execution_id = str(recorded.get("execution_id", "")) or None
        source = "journal"
    if execution_id is None:
        execution_id = _recover(request, workflow_id)
        source = "history" if execution_id is not None else source
    if execution_id is None:
        execution_id = request.api.execute_adhoc(
            instance_id=request.instance_id,
            workflow_id=workflow_id,
            sql=spec.sql,
            window_start=request.window_start,
            window_end=request.window_end,
            require_synthetic_data=synthetic,
            disable_aggregation_controls=disable,
            suppression_columns=SUPPRESSION,
            parameter_values=dict(spec.parameter_values) or None,
        )
    if source != "journal":
        journal.write_text(
            json.dumps(
                {
                    "instance_id": request.instance_id,
                    "instance_type": request.instance_type,
                    "workflow_id": workflow_id,
                    "execution_id": execution_id,
                    "query": spec.name,
                    "identity_digest": digest,
                    "sql_digest": sql_digest(spec.sql),
                    "sql": normalized_sql(spec.sql),
                    "controls_off": spec.controls_off,
                    "parameter_values": dict(spec.parameter_values),
                    "window_start": request.window_start,
                    "window_end": request.window_end,
                    "require_synthetic_data": synthetic,
                    "disable_aggregation_controls": disable,
                    "suppression_columns": list(SUPPRESSION),
                    "recovered_from": source,
                },
                indent=2,
            )
        )
    print(f"  {spec.name:8s} execution={execution_id} source={source}")
    return ExecutionPlan(
        request.instance_id,
        request.instance_type,
        spec,
        workflow_id,
        execution_id,
        digest,
        stem,
        request.findings,
        source,
    )


@dataclass(frozen=True, slots=True)
class TerminalResult:
    """Terminal status and, on success, the downloaded CSV."""

    status: str
    reason: str
    csv_text: str | None


def collect_terminal(
    api: ProbeApi,
    plan: ExecutionPlan,
    *,
    byte_limit: int,
    timeout_seconds: int = TERMINAL_WAIT_SECONDS,
) -> TerminalResult:
    """Poll to a terminal state and persist the terminal record and, on success, the CSV."""
    terminal = api.wait_for_execution(
        instance_id=plan.instance_id,
        execution_id=plan.execution_id,
        timeout_seconds=timeout_seconds,
    )
    (plan.findings / f"{plan.stem}.terminal.json").write_text(json.dumps(terminal, indent=2))
    status = str(terminal.get("status", "")).upper()
    reason = str(terminal.get("statusReason") or terminal.get("statusMessage") or "")
    if status != "SUCCEEDED":
        return TerminalResult(status, reason, None)
    urls = api.download_urls(instance_id=plan.instance_id, execution_id=plan.execution_id)
    if not urls:
        raise AmcError(200, "successful execution returned no download URL", plan.execution_id)
    csv_text = fetch_csv(urls[0], max_bytes=byte_limit)
    (plan.findings / f"{plan.stem}.csv").write_text(csv_text)
    return TerminalResult(status, reason, csv_text)
