# SPDX-License-Identifier: MIT-0
"""P6 (slice S6) part 1 for the traceability probe runner: ingress by mode and the abort rehearsal.

The reversible half of P6, run in steps so a long drain wait is never one process:

- `--p6-step a`: in `SHADOW`, one probe retrieval with its `on_demand` twin. Its `delivery_event`
  (the SQS handler's SHADOW recording, after the legacy outcome) and the twin share the one
  shadow binding with distinct receipts, both epoch 0, and the legacy path still plans the
  delivery. The `schedule` arm needs producer onboarding, which dev does not provision, so it is
  reported as not provisioned rather than passed.
- `--p6-step b0-start`: the abort rehearsal's first half. The expected alarms are written to
  their own evidence file first, the staffed-window notice (`--staffed-notice`) is recorded,
  nothing may be in flight on the delivery, semantic-write or bake queues, and the host clock's
  skew against AWS is read. Then the `LEGACY_DRAINING` CAS with the shortest legal `D` (drain
  CAS + `T_max` + margin + a small buffer), and one probe retrieval: the SQS handler makes zero
  admission calls and leaves the message waiting, and fixture landing's twin gets
  `REFUSED(RUN_ADMISSION_NOT_ACTIVE)`. Both the drain command receipt and the refusal carry
  epoch 0. Then the abort CAS `LEGACY_DRAINING -> LEGACY`.
- `--p6-step b0-finish --p6-start <b0-start.json>`: after `D` plus the margin, the waiting message
  is processed by the legacy path on its second receive (read from the handler's terminal
  record), the plan completes with every unit attested, there is no DLQ arrival and no epoch-1
  record of any family. Then the mode returns to `SHADOW`, which is where part 1 leaves it.

Every step records the deploy procedure's step-7 fields through the runner's `_preflight`: the
deployed `SHA`, each set stack's assets-file digest and `S3Key` comparison, and the import scan.

The real drain and the `GRAPH` CAS (P6(b)-(f)) are not part of this module: they wait for the
approved GRAPH execution chain design.
"""

# The P6 probe composes the runner's private helpers.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Final, cast

import probe_ingestion_traceability_gate as gate

from adapters.inbound.ingestion_handlers.ingress import DeliveryRef, delivery_request
from domain.operations.cutover import FENCE_MARGIN_SECONDS
from domain.operations.generated_execution_graphs import CUTOVER, T_MAX_SECONDS

__all__ = [
    "CLOCK_SKEW_BUDGET_SECONDS",
    "DRAIN_BUFFER_SECONDS",
    "EXPECTED_DRAIN_ALARMS",
    "clock_skew_seconds",
    "epoch_1_verdict",
    "handler_records",
    "in_flight_verdict",
    "plan_completed",
    "probe_p6",
    "settled_plan",
    "shadow_binding_verdict",
    "terminals_after",
]

DRAIN_BUFFER_SECONDS: Final = 120
"""Added to the shortest legal `D`, so the CAS's own clock never lands it below the floor."""
EXPECTED_DRAIN_ALARMS: Final = (
    "ingestion-oldest",
    "ingestion-alarming-errors-ingest-drain-last-receive",
    "ingestion-dead-lettered",
    "record-function-changes",
    "record-control-plane-changes",
)
"""design.md, "Expected alarms during the drain", plus the two deploy-audit alarms (S5-1).

Each entry is an alarm name without its `adlytics-<env>-` prefix, and
`infra/test/p6-expected-alarms.test.ts` requires every one to be synthesized (S6-1). The dead-letter
alarm has read `ALARM` since 2026-10-06, so a drain-time dead letter makes no transition: the DLQ's
visible count (`no_dlq_arrival`) is the only arrival signal, never this alarm.
"""
CONDITIONAL_DRAIN_ALARMS: Final = ("ingestion-alarming-errors-semantic-table-writer",)
"""Alarms expected in a drain window only under `legacy_race_verdict` (owner-gate Item 21).

Known legacy defect, retires with legacy: TableWriter's legacy `_finalize_unit` reads the plan
`META` twice, so a unit of the same plan settling between the reads makes request validation
raise (seen 2026-10-06 and 2026-10-09). The unit retries and the plan completes. The alarm is
expected only when every alarming TableWriter record in the window carries the race's structured
signature and every affected plan reaches `completed` inside the window. Anything else stops.
"""
LEGACY_FINALIZE_RACE: Final = {
    "error_code": "ADLY-UNEXPECTED-0000",
    "error_class": "invariant",
    "error_type": "ValueError",
    "frames": (
        ("table_writer.py", "_finalize_unit"),
        ("projection_coordinator.py", "_require_settled_history"),
    ),
}
"""The race's structured signature in the shared error record (`report_error`).

The defect raises an unclassified `ValueError`, so it has no dedicated error code; the code,
class and type together with the two structured stack frames identify it. No description text
is matched.
"""
CLOCK_SKEW_BUDGET_SECONDS: Final = 10
"""The host clock may differ from AWS by at most this before a drain CAS (S6-14).

`begin_drain` stamps `drain_entered_at` and checks `D`'s floor with the operator host's clock,
so the 60 s fence margin is also the skew budget; this keeps the skew a small part of it.
"""
NON_EPOCH_1_FAMILIES: Final = frozenset({"COMMAND", "GRAPH", "MODE", "RECEIPT"})
"""The only live-table families an epoch-0 world holds; any other family is graph execution."""
IN_FLIGHT_QUEUES: Final = ("ingestion", "semantic-write.fifo", "preset-bake.fifo")
"""The daily ingestion path's work queues; their DLQs are read separately (S6-12)."""
_POLL_SECONDS: Final = 20
_MARGIN: Final = int(CUTOVER["delivery_queue_visibility_margin_seconds"])
_LOG_WAIT_SECONDS: Final = 300
_PLANNED: Final = frozenset({"planned", "completed"})
_FINISHED: Final = frozenset({"completed", "superseded", "admission_failed"})
"""The plan `META` terminal states (`domain/operations/write_plan.py`, `PlanStatus`)."""


# --- Pure verdicts (unit-tested) -------------------------------------------------------------


def shadow_binding_verdict(
    twin: dict[str, object] | None, delivery: dict[str, object] | None
) -> dict[str, object]:
    """Both receipts epoch 0, distinct, and bound to the one shadow graph (V-5, V-19)."""
    if twin is None or delivery is None:
        return {"passed": False, "reason": "a receipt is missing"}
    decisions = {str(twin.get("decision")), str(delivery.get("decision"))}
    graphs = {twin.get("graph_id"), delivery.get("graph_id")}
    checks = {
        "distinct_request_ids": twin.get("request_id") != delivery.get("request_id"),
        "both_epoch_0": twin.get("authority_epoch") == 0 and delivery.get("authority_epoch") == 0,
        "same_fingerprint": twin.get("fingerprint") == delivery.get("fingerprint"),
        "one_shadow_graph": len(graphs) == 1 and None not in graphs,
        "admitted_then_joined": decisions == {"ADMITTED", "JOINED"},
        "caller_kinds": {twin.get("caller_kind"), delivery.get("caller_kind")}
        == {"on_demand", "delivery_event"},
    }
    return {"checks": checks, "passed": all(checks.values())}


def drain_deadline(now: datetime) -> datetime:
    """Return the shortest legal `D` plus the buffer (P6(b0); design.md step 5's floor)."""
    return now + timedelta(seconds=T_MAX_SECONDS + FENCE_MARGIN_SECONDS + DRAIN_BUFFER_SECONDS)


def plan_completed(plan: dict[str, object]) -> bool:
    """The legacy plan completed with every unit attested and none failed (S6-9)."""
    units = plan.get("unit_count")
    return (
        plan.get("status") == "completed"
        and isinstance(units, int)
        and units > 0
        and plan.get("attested_count") == units
        and not plan.get("failed_count")
    )


def _record_instant(row: dict[str, str]) -> datetime:
    stamp = row.get("@timestamp", "1970-01-01 00:00:00").replace(" ", "T")
    return datetime.fromisoformat(stamp).replace(tzinfo=UTC)


def terminals_after(records: list[dict[str, str]], instant: datetime) -> list[dict[str, str]]:
    """The handler's terminal records written at or after `instant` (`D` plus the margin)."""
    return [
        row
        for row in records
        if row.get("phase") == "terminal" and _record_instant(row) >= instant
    ]


def epoch_1_verdict(census: dict[str, int], epoch_1_keys: list[str]) -> dict[str, object]:
    """No epoch-1 record of any family: only epoch-0 families, and no item at epoch 1 (S6-9)."""
    families = sorted(set(census) - NON_EPOCH_1_FAMILIES)
    return {
        "graph_execution_families": families,
        "epoch_1_items": epoch_1_keys[:20],
        "passed": not families and not epoch_1_keys,
    }


def clock_skew_seconds(host: datetime, server_date: str) -> float:
    """Return host minus AWS time, from one response's HTTP `Date` header (1 s resolution)."""
    return (host - parsedate_to_datetime(server_date)).total_seconds()


def race_signature(record: dict[str, object]) -> bool:
    """Whether one structured error record is the known legacy finalize race."""
    stack = record.get("stack")
    entries = cast("list[object]", stack) if isinstance(stack, list) else []
    frames = {
        (str(entry.get("filename")), str(entry.get("function")))
        for entry in (cast("dict[str, object]", item) for item in entries if isinstance(item, dict))
    }
    scalars = ("error_code", "error_class", "error_type")
    return (
        all(record.get(name) == LEGACY_FINALIZE_RACE[name] for name in scalars)
        and set(cast("tuple[tuple[str, str], ...]", LEGACY_FINALIZE_RACE["frames"])) <= frames
    )


def _micros(value: object) -> datetime | None:
    """Read a coordinator `terminal_at` (zero-padded epoch microseconds)."""
    if not isinstance(value, str) or not value.isdigit():
        return None
    return datetime.fromtimestamp(int(value) / 1_000_000, UTC)


@dataclass(frozen=True, slots=True)
class AlarmWindow:
    """One drain window's alarm transitions and the reads the conditional alarms need."""

    start: datetime
    end: datetime
    transitions: list[tuple[datetime, str, str]]
    """(instant, alarm name, history summary), every `adlytics-<env>-` state update."""
    race_records: list[dict[str, object]]
    """Every alarming TableWriter error record in the window, parsed."""
    plans: dict[str, dict[str, object]]
    """Plan key -> its `META` status and `terminal_at`, read at the window's end."""


def alarm_window_verdict(
    window: AlarmWindow, expected: frozenset[str], conditional: frozenset[str]
) -> dict[str, object]:
    """Stop on any unlisted `ALARM`; admit a conditional one only under its mechanical rule."""
    into_alarm = [row for row in window.transitions if row[2].endswith("to ALARM")]
    unexpected = sorted({name for _, name, _ in into_alarm if name not in expected | conditional})
    fired = sorted({name for _, name, _ in into_alarm if name in conditional})
    # Item 18 condition 2, as in `s6-alarm-check.sh`: still in ALARM 15 minutes after entering it.
    stuck: list[str] = []
    for name in sorted({name for _, name, _ in into_alarm}):
        last = [row for row in window.transitions if row[1] == name][-1]
        if last[2].endswith("to ALARM") and window.end - last[0] > timedelta(minutes=15):
            stuck.append(name)
    race: dict[str, object] = {"fired": fired}
    race_ok = True
    if fired:
        records = window.race_records
        signed = bool(records) and all(race_signature(record) for record in records)
        affected = sorted({str(record.get("correlation_id")) for record in records})
        completed = {
            key: (
                window.plans.get(key, {}).get("status") == "completed"
                and (at := _micros(window.plans.get(key, {}).get("terminal_at"))) is not None
                and window.start <= at <= window.end
            )
            for key in affected
        }
        race_ok = signed and bool(completed) and all(completed.values())
        race |= {
            "records": len(records),
            "all_carry_the_race_signature": signed,
            "affected_plans_completed_in_window": completed,
            "classification": "known legacy defect, retires with legacy" if race_ok else "stop",
        }
    return {
        "window": [window.start.isoformat(), window.end.isoformat()],
        "transitions": [
            (at.isoformat(), name, summary) for at, name, summary in window.transitions
        ],
        "unexpected": unexpected,
        "not_recovered": stuck,
        "conditional": race,
        "passed": not unexpected and not stuck and race_ok,
    }


def in_flight_verdict(counts: dict[str, int]) -> dict[str, object]:
    """Nothing visible or in flight on the daily ingestion work queues (S6-12)."""
    busy = {name: count for name, count in counts.items() if count}
    return {"busy": busy, "passed": not busy}


# --- Live helpers ----------------------------------------------------------------------------


def _land_with_twin(
    target: gate.Target, arguments: argparse.Namespace
) -> tuple[int, dict[str, object], dict[str, object] | None]:
    argv = [
        "uv",
        "run",
        "python",
        "-m",
        "adapters.jobs.land_fixtures",
        "--profile",
        target.profile,
        "--region",
        target.region,
        "--expected-account",
        target.expected_account,
        "--env",
        target.environment,
        "--operation-id",
        str(arguments.operation_id),
        "--stack",
        str(arguments.raw_stack),
        "--bucket",
        str(arguments.bucket),
        "--probe-retrieval",
        gate.FIXED_PROBE_DELIVERY,
        "--graph-store-stack",
        str(arguments.store_stack),
    ]
    # A-SUBPROCESS-001: uv run of a repository module, no shell; see docs/security-dispositions.md
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    completed = subprocess.run(  # noqa: S603 -- fixed argv, explicit inputs only
        argv, cwd=Path(arguments.worktree).resolve(), capture_output=True, text=True, check=False
    )
    report = gate.parse_probe_report(completed.stdout)
    twin = None
    for line in completed.stdout.splitlines():
        if line.startswith("PROBE_TWIN "):
            twin = cast("dict[str, object]", json.loads(line.removeprefix("PROBE_TWIN ")))
    if completed.returncode not in {0, 3} or twin is None:
        raise gate.ProbeError(
            f"land_fixtures exited {completed.returncode}: {completed.stderr[-400:]}"
        )
    return completed.returncode, report, twin


def _receipt(session: Any, table: str, request_id: str) -> dict[str, object] | None:
    found = session.client("dynamodb").get_item(
        TableName=table,
        Key={"pk": {"S": f"RECEIPT#{request_id}"}, "sk": {"S": "RECEIPT"}},
        ConsistentRead=True,
    )
    item = found.get("Item")
    if item is None:
        return None
    plain: dict[str, object] = {}
    for name, value in item.items():
        if "S" in value:
            plain[name] = value["S"]
        elif "N" in value:
            plain[name] = int(value["N"])
    return plain


def _command(session: Any, table: str, command_id: str) -> dict[str, object] | None:
    found = session.client("dynamodb").get_item(
        TableName=table,
        Key={"pk": {"S": f"COMMAND#{command_id}"}, "sk": {"S": "COMMAND"}},
        ConsistentRead=True,
    )
    item = found.get("Item")
    if item is None:
        return None
    return {
        "command": item["command"]["S"],
        "authority_epoch": int(item["authority_epoch"]["N"]),
        "mode": item["mode"]["S"],
        "details": {key: value["S"] for key, value in item["details"]["M"].items()},
    }


def _delivery_request_id(key: str, version: str) -> str:
    ref = DeliveryRef(key, version, "", 1, "", "")
    return delivery_request(ref, "arn:aws:sts::000000000000:assumed-role/r/s").request_id


def _queue_counts(session: Any, environment: str) -> dict[str, int]:
    sqs = session.client("sqs")
    counts: dict[str, int] = {}
    for name in (f"adlytics-{environment}-ingestion", f"adlytics-{environment}-ingestion-dlq"):
        url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
        attributes = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        counts[f"{name}:visible"] = int(attributes["ApproximateNumberOfMessages"])
        counts[f"{name}:not_visible"] = int(attributes["ApproximateNumberOfMessagesNotVisible"])
    return counts


def _work_queue_counts(session: Any, environment: str) -> dict[str, int]:
    """Visible plus in-flight messages on each daily ingestion work queue (not the DLQs)."""
    sqs = session.client("sqs")
    counts: dict[str, int] = {}
    for suffix in IN_FLIGHT_QUEUES:
        name = f"adlytics-{environment}-{suffix}"
        url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
        attributes = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        counts[name] = int(attributes["ApproximateNumberOfMessages"]) + int(
            attributes["ApproximateNumberOfMessagesNotVisible"]
        )
    return counts


def _clock_skew(session: Any) -> dict[str, object]:
    """Read the host clock against one AWS response's `Date` header (S6-14)."""
    host = datetime.now(UTC)
    response = session.client("sts").get_caller_identity()
    server = str(response["ResponseMetadata"]["HTTPHeaders"]["date"])
    skew = clock_skew_seconds(host, server)
    return {
        "host_utc": host.isoformat(),
        "aws_date": server,
        "skew_seconds": skew,
        "budget_seconds": CLOCK_SKEW_BUDGET_SECONDS,
        "passed": abs(skew) <= CLOCK_SKEW_BUDGET_SECONDS,
    }


def _epoch_1_items(session: Any, table: str) -> list[str]:
    """Every live-table item stamped `authority_epoch = 1` (a consistent scan; read-only)."""
    found: list[str] = []
    pages = (
        session.client("dynamodb")
        .get_paginator("scan")
        .paginate(
            TableName=table,
            ConsistentRead=True,
            ProjectionExpression="pk, sk",
            FilterExpression="authority_epoch = :one",
            ExpressionAttributeValues={":one": {"N": "1"}},
        )
    )
    for page in pages:
        found.extend(f"{item['pk']['S']}/{item['sk']['S']}" for item in page.get("Items", []))
    return found


def _write_expected_alarms(
    target: gate.Target, step: str, names: list[str], window: str
) -> dict[str, object]:
    """Write the expected-alarm list to its own evidence file before any CAS (S6-11)."""
    target.evidence_dir.mkdir(parents=True, exist_ok=True)
    path = target.evidence_dir / f"P6-{step}-expected-alarms-{gate._utc_stamp()}.json"
    conditional = [
        f"adlytics-{target.environment}-{suffix}" for suffix in CONDITIONAL_DRAIN_ALARMS
    ]
    body = {
        "step": step,
        "window": window,
        "expected_alarms": names,
        "conditional_alarms": {
            name: {
                "classification": "known legacy defect, retires with legacy",
                "expected_only_if": [
                    "every alarming TableWriter record in the window carries the signature",
                    "every affected plan reads completed with terminal_at inside the window",
                ],
                "signature": LEGACY_FINALIZE_RACE,
            }
            for name in conditional
        },
    }
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "written_at": datetime.now(UTC).isoformat(),
    }


def _staffed_notice(path: str) -> dict[str, object]:
    """Record the staffed-window notice the operator sent before the drain (S6-12)."""
    notice = Path(path)
    if not notice.is_absolute() or not notice.is_file():
        raise gate.ProbeError("--staffed-notice must be an absolute path to the notice record")
    return {
        "path": str(notice),
        "sha256": hashlib.sha256(notice.read_bytes()).hexdigest(),
        "modified_at": datetime.fromtimestamp(notice.stat().st_mtime, UTC).isoformat(),
    }


def _alarm_transitions(
    session: Any, environment: str, start: datetime, end: datetime
) -> list[tuple[datetime, str, str]]:
    cloudwatch = session.client("cloudwatch")
    rows: list[tuple[datetime, str, str]] = []
    for page in cloudwatch.get_paginator("describe_alarm_history").paginate(
        HistoryItemType="StateUpdate", StartDate=start, EndDate=end
    ):
        for item in page.get("AlarmHistoryItems", []):
            name = str(item["AlarmName"])
            if name.startswith(f"adlytics-{environment}-"):
                stamp = cast("datetime", item["Timestamp"]).astimezone(UTC)
                rows.append((stamp, name, str(item["HistorySummary"])))
    return sorted(rows)


def _table_writer_alarming_records(
    session: Any, environment: str, start: datetime, end: datetime
) -> list[dict[str, object]]:
    """Every invariant or dependency error record TableWriter wrote in the window, parsed."""
    logs = session.client("logs")
    started = logs.start_query(
        logGroupName=f"/aws/lambda/adlytics-{environment}-TableWriter",
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()) + 60,
        queryString=(
            "fields @message | filter operation = 'semantic_table_writer' "
            "and error_class in ['invariant', 'dependency'] | limit 1000"
        ),
    )
    for _ in range(60):
        result = logs.get_query_results(queryId=started["queryId"])
        if result["status"] in {"Complete", "Failed", "Cancelled"}:
            if result["status"] != "Complete":
                raise gate.ProbeError("the TableWriter error query did not complete")
            records: list[dict[str, object]] = []
            for row in result.get("results", []):
                message = next(
                    (str(f["value"]) for f in row if f["field"] == "@message"), ""
                )
                start_at = message.find("{")
                records.append(
                    cast("dict[str, object]", json.loads(message[start_at:]))
                    if start_at >= 0
                    else {"unparsed": message[:200]}
                )
            return records
        time.sleep(2)
    raise gate.ProbeError("the TableWriter error query did not complete")


def _plan_meta(session: Any, environment: str, plan_key: str) -> dict[str, object]:
    name = f"/adlytics/{environment}/data/v2/semantic-publications-table"
    table = str(session.client("ssm").get_parameter(Name=name)["Parameter"]["Value"])
    item = (
        session.client("dynamodb")
        .get_item(
            TableName=table,
            Key={"partition_key": {"S": plan_key}, "record_key": {"S": "META"}},
            ConsistentRead=True,
        )
        .get("Item", {})
    )
    return {
        "status": item.get("status", {}).get("S"),
        "terminal_at": item.get("terminal_at", {}).get("S") or item.get("terminal_at", {}).get("N"),
    }


def read_alarm_window(
    session: Any, environment: str, start: datetime, end: datetime
) -> AlarmWindow:
    """Read the window's transitions, and the race's records and plans when its alarm fired."""
    transitions = _alarm_transitions(session, environment, start, end)
    conditional = {f"adlytics-{environment}-{suffix}" for suffix in CONDITIONAL_DRAIN_ALARMS}
    if not any(name in conditional and s.endswith("to ALARM") for _, name, s in transitions):
        return AlarmWindow(start, end, transitions, [], {})
    # The alarm's 5-minute period can count records written just before the window opened.
    records = _table_writer_alarming_records(
        session, environment, start - timedelta(minutes=5), end
    )
    plans = {
        key: _plan_meta(session, environment, key)
        for key in {str(record.get("correlation_id")) for record in records}
        if key.startswith("WRITE_PLAN#")
    }
    return AlarmWindow(start, end, transitions, records, plans)


def _expected_names(environment: str) -> tuple[frozenset[str], frozenset[str]]:
    return (
        frozenset(f"adlytics-{environment}-{suffix}" for suffix in EXPECTED_DRAIN_ALARMS),
        frozenset(f"adlytics-{environment}-{suffix}" for suffix in CONDITIONAL_DRAIN_ALARMS),
    )


def _query_handler_records(
    session: Any, environment: str, key: str, since: datetime
) -> list[dict[str, str]]:
    group = f"/aws/lambda/adlytics-{environment}-IngestionFunction"
    logs = session.client("logs")
    query = (
        "fields @timestamp, phase, outcome, receive_count, admission_calls "
        f"| filter @message like '{key}' "
        "| filter phase in ['drain_wait', 'terminal', 'admission'] "
        "| sort @timestamp asc | limit 50"
    )
    started = logs.start_query(
        logGroupName=group,
        startTime=int(since.timestamp()),
        endTime=int(datetime.now(UTC).timestamp()) + 60,
        queryString=query,
    )
    for _ in range(30):
        result = logs.get_query_results(queryId=started["queryId"])
        if result["status"] in {"Complete", "Failed", "Cancelled"}:
            return [
                {str(field["field"]): str(field["value"]) for field in row}
                for row in result.get("results", [])
            ]
        time.sleep(2)
    raise gate.ProbeError("the handler log query did not complete")


def handler_records(
    session: Any, environment: str, key: str, since: datetime, phase: str
) -> list[dict[str, str]]:
    """Read the handler's drain-wait and terminal records for one manifest key.

    Logs Insights indexes a record some time after it is written, so the query repeats until a
    record of `phase` appears or `_LOG_WAIT_SECONDS` pass; an absent record then fails its check.
    """
    deadline = time.monotonic() + _LOG_WAIT_SECONDS
    records = _query_handler_records(session, environment, key, since)
    while not any(row.get("phase") == phase for row in records) and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        records = _query_handler_records(session, environment, key, since)
    return records


def settled_plan(  # noqa: PLR0913, PLR0917 -- the plan's identity, the wait and its target
    session: Any,
    environment: str,
    key: str,
    version: str,
    wait_seconds: int,
    until: frozenset[str] = _PLANNED,
) -> dict[str, object]:
    """Read the legacy write plan until its status is in `until`, or the wait ends.

    P6(a) waits for the reconciler to persist the plan (`planned`). b0-finish waits for a terminal
    plan status, because the plan's units still run after the handler's terminal record.
    """
    digest = gate._plan_digest(key, version)
    deadline = time.monotonic() + wait_seconds
    plan = gate._plan_state(session, environment, digest)
    while plan.get("status") not in until and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        plan = gate._plan_state(session, environment, digest)
    return plan


def _mode(target: gate.Target, arguments: argparse.Namespace) -> dict[str, object]:
    _, shown = gate._operator(target, arguments, "show-mode")
    return shown


def _cutover_args(arguments: argparse.Namespace) -> list[str]:
    return [
        "--coordinator-table",
        str(arguments.coordinator_table),
        "--coordinator-stack",
        str(arguments.coordinator_stack),
    ]


# --- Steps -----------------------------------------------------------------------------------


def _step_a(target: gate.Target, arguments: argparse.Namespace, evidence: gate.Evidence) -> bool:
    session = gate._session(target)
    table = gate._store_table(session, target.environment, str(arguments.store_stack))
    mode = _mode(target, arguments)
    evidence.document["mode_before"] = mode
    if mode.get("mode") != "SHADOW":
        raise gate.ProbeError("P6(a) runs in SHADOW")
    since = datetime.now(UTC)
    rc, report, twin = _land_with_twin(target, arguments)
    key = str(report["key"])
    version = str(cast("dict[str, object]", twin)["version_id"])
    evidence.document["probe_key"] = key
    evidence.document["probe_version"] = version
    evidence.document["land_exit"] = rc
    twin_receipt = cast("dict[str, object] | None", cast("dict[str, object]", twin).get("receipt"))
    delivery_id = _delivery_request_id(key, version)
    delivery = None
    deadline = time.monotonic() + int(arguments.wait_seconds)
    while time.monotonic() < deadline:
        delivery = _receipt(session, table, delivery_id)
        if delivery is not None:
            break
        time.sleep(_POLL_SECONDS)
    evidence.document["twin_receipt"] = twin_receipt
    evidence.document["delivery_receipt"] = delivery
    binding = shadow_binding_verdict(twin_receipt, delivery)
    evidence.document["shadow_binding"] = binding
    plan = settled_plan(session, target.environment, key, version, int(arguments.wait_seconds))
    evidence.document["legacy_plan"] = plan
    evidence.document["handler_records"] = handler_records(
        session, target.environment, key, since, "terminal"
    )
    # S6-2: AC-14 needs the schedule arm live, so this step is partial until the owner decides.
    evidence.document["acceptance"] = "partial: the schedule arm is not run"
    evidence.document["schedule_arm"] = {
        "state": "not_provisioned",
        "reason": "dev does not enable producer onboarding (ADLYTICS_PRODUCER_ONBOARDING_ENABLED),"
        " so no finalizer or landing role exists to supply the schedule request; the finalizer's"
        " schedule path is unit-proven (test_ingestion_authority_boundary.py).",
    }
    checks = {
        "shadow_binding": bool(binding["passed"]),
        "legacy_path_planned": plan.get("status") in _PLANNED,
        "mode_unchanged": _mode(target, arguments).get("mode") == "SHADOW",
    }
    evidence.document["checks"] = checks
    return all(checks.values())


def _step_b0_start(
    target: gate.Target, arguments: argparse.Namespace, evidence: gate.Evidence
) -> bool:
    session = gate._session(target)
    table = gate._store_table(session, target.environment, str(arguments.store_stack))
    expected = [f"adlytics-{target.environment}-{suffix}" for suffix in EXPECTED_DRAIN_ALARMS]
    evidence.document["expected_alarms"] = expected
    evidence.document["expected_alarms_file"] = _write_expected_alarms(
        target, "b0", expected, "the rehearsal drain, from this step to b0-finish"
    )
    evidence.document["staffed_notice"] = _staffed_notice(str(arguments.staffed_notice))
    start = _mode(target, arguments)
    evidence.document["mode_before"] = start
    if start.get("mode") not in {"SHADOW", "LEGACY"}:
        raise gate.ProbeError("the rehearsal starts in SHADOW or LEGACY")
    evidence.document["queues_before"] = _queue_counts(session, target.environment)
    in_flight = in_flight_verdict(_work_queue_counts(session, target.environment))
    evidence.document["in_flight_before"] = in_flight
    if not in_flight["passed"]:
        raise gate.ProbeError(f"ingestion work is in flight: {in_flight['busy']}; drain later")
    skew = _clock_skew(session)
    evidence.document["clock_skew"] = skew
    if not skew["passed"]:
        raise gate.ProbeError(f"host clock skew {skew['skew_seconds']} s exceeds the budget")
    stamp = gate._utc_stamp().lower().replace("t", "-").replace("z", "")
    drain_id, abort_id = f"p6b0-drain-{stamp}", f"p6b0-abort-{stamp}"
    deadline = drain_deadline(datetime.now(UTC))
    rc, drained = gate._operator(
        target,
        arguments,
        "set-mode",
        "--command-id",
        drain_id,
        "--to",
        "LEGACY_DRAINING",
        "--expected-revision",
        str(start["revision"]),
        "--drain-deadline",
        deadline.isoformat(),
        *_cutover_args(arguments),
    )
    evidence.document["drain_cas"] = {"exit": rc, "result": drained}
    if rc != 0:
        raise gate.ProbeError(f"the drain CAS exited {rc}")
    landed_at = datetime.now(UTC)
    land_rc, report, twin = _land_with_twin(target, arguments)
    key = str(report["key"])
    version = str(cast("dict[str, object]", twin)["version_id"])
    evidence.document.update(
        {"probe_key": key, "probe_version": version, "land_exit": land_rc, "twin": twin}
    )
    # Give the handler time to receive the S3 event and leave it waiting.
    time.sleep(120)
    delivery = _receipt(session, table, _delivery_request_id(key, version))
    records = handler_records(session, target.environment, key, landed_at, "drain_wait")
    evidence.document["handler_records_during_drain"] = records
    evidence.document["queues_during_drain"] = _queue_counts(session, target.environment)
    after_drain = _mode(target, arguments)
    rc_abort, aborted = gate._operator(
        target,
        arguments,
        "set-mode",
        "--command-id",
        abort_id,
        "--to",
        "LEGACY",
        "--expected-revision",
        str(after_drain["revision"]),
        *_cutover_args(arguments),
    )
    evidence.document["abort_cas"] = {"exit": rc_abort, "result": aborted}
    drain_receipt = _command(session, table, drain_id)
    abort_receipt = _command(session, table, abort_id)
    twin_receipt = cast("dict[str, object]", cast("dict[str, object]", twin).get("receipt") or {})
    evidence.document.update(
        {
            "drain_receipt": drain_receipt,
            "abort_receipt": abort_receipt,
            "drain_deadline": deadline.isoformat(),
            "visible_again_after": (deadline + timedelta(seconds=_MARGIN)).isoformat(),
        }
    )
    drain_waits = [row for row in records if row.get("phase") == "drain_wait"]
    checks = {
        "drain_committed": drained.get("mode") == "LEGACY_DRAINING",
        "handler_left_it_waiting": bool(drain_waits)
        and all(row.get("admission_calls") == "0" for row in drain_waits),
        "no_delivery_receipt": delivery is None,
        "twin_refused_not_active": twin_receipt.get("decision") == "REFUSED"
        and twin_receipt.get("reason") == "RUN_ADMISSION_NOT_ACTIVE"
        and twin_receipt.get("authority_epoch") == 0
        and twin_receipt.get("mode") == "LEGACY_DRAINING",
        "drain_receipt_epoch_0": drain_receipt is not None
        and drain_receipt["authority_epoch"] == 0,
        "abort_committed_epoch_0": rc_abort == 0
        and aborted.get("mode") == "LEGACY"
        and abort_receipt is not None
        and abort_receipt["authority_epoch"] == 0,
    }
    evidence.document["checks"] = checks
    return all(checks.values())


def _step_b0_finish(
    target: gate.Target, arguments: argparse.Namespace, evidence: gate.Evidence
) -> bool:
    session = gate._session(target)
    table = gate._store_table(session, target.environment, str(arguments.store_stack))
    started = json.loads(Path(arguments.p6_start).read_text())
    evidence.document["p6_start"] = str(arguments.p6_start)
    visible = datetime.fromisoformat(str(started["visible_again_after"]))
    key, version = str(started["probe_key"]), str(started["probe_version"])
    wait = (visible - datetime.now(UTC)).total_seconds()
    if wait > 0:
        raise gate.ProbeError(f"run b0-finish after {visible.isoformat()} ({int(wait)} s left)")
    plan = settled_plan(
        session, target.environment, key, version, int(arguments.wait_seconds), _FINISHED
    )
    since = datetime.fromisoformat(str(started["drain_deadline"])) - timedelta(minutes=30)
    records = handler_records(session, target.environment, key, since, "terminal")
    queues = _queue_counts(session, target.environment)
    before = cast("dict[str, int]", started["queues_before"])
    dlq = f"adlytics-{target.environment}-ingestion-dlq:visible"
    census = gate._table_census(session, table)
    epoch_1 = epoch_1_verdict(census, _epoch_1_items(session, table))
    delivery = _receipt(session, table, _delivery_request_id(key, version))
    mode = _mode(target, arguments)
    restored: dict[str, object] = {}
    if mode.get("mode") == "LEGACY":
        stamp = gate._utc_stamp().lower().replace("t", "-").replace("z", "")
        rc, restored = gate._operator(
            target,
            arguments,
            "set-mode",
            "--command-id",
            f"p6b0-shadow-{stamp}",
            "--to",
            "SHADOW",
            "--expected-revision",
            str(mode["revision"]),
        )
        restored["exit"] = rc
    # The legacy path may run only once the message is visible again: at `D` plus the margin.
    terminals = terminals_after(records, visible)
    evidence.document.update(
        {
            "legacy_plan": plan,
            "handler_records": records,
            "terminals_after_the_drain_wait": terminals,
            "queues_after": queues,
            "census": census,
            "epoch_1": epoch_1,
            "delivery_receipt_after_abort": delivery,
            "mode_after_abort": mode,
            "restored_shadow": restored,
        }
    )
    checks = {
        "legacy_completed_every_unit_attested": plan_completed(plan),
        "legacy_terminal_after_d_plus_margin": bool(terminals),
        "processed_on_receive_2": any(row.get("receive_count") == "2" for row in terminals),
        "no_dlq_arrival": queues.get(dlq, 0) <= before.get(dlq, 0),
        "no_epoch_1_record": bool(epoch_1["passed"]),
        "no_admission_in_legacy": delivery is None,
        "mode_back_to_shadow": restored.get("mode") == "SHADOW",
    }
    alarms = _alarm_window(session, target.environment, started)
    evidence.document["alarm_window"] = alarms
    checks["alarms_within_expected"] = bool(alarms["passed"])
    evidence.document["checks"] = checks
    return all(checks.values())


def _alarm_window(
    session: Any, environment: str, started: dict[str, object], end: datetime | None = None
) -> dict[str, object]:
    """Apply the drain window's alarm rule from the drain CAS to `end` or now (read-only)."""
    drain = cast("dict[str, Any]", started["drain_cas"])["result"]["receipt"]["recorded_at"]
    start = datetime.fromisoformat(str(drain))
    window = read_alarm_window(session, environment, start, end or datetime.now(UTC))
    expected, conditional = _expected_names(environment)
    return alarm_window_verdict(window, expected, conditional)


def _step_alarm_window(
    target: gate.Target, arguments: argparse.Namespace, evidence: gate.Evidence
) -> bool:
    """Read-only: re-apply the alarm rule to an earlier drain window (`--p6-start`)."""
    session = gate._session(target)
    started = json.loads(Path(arguments.p6_start).read_text())
    evidence.document["p6_start"] = str(arguments.p6_start)
    if not arguments.window_end:
        raise gate.ProbeError("alarm-window needs --window-end (the window's recorded end)")
    end = datetime.fromisoformat(str(arguments.window_end).replace("Z", "+00:00"))
    alarms = _alarm_window(session, target.environment, started, end)
    evidence.document["alarm_window"] = alarms
    return bool(alarms["passed"])


def probe_p6(target: gate.Target, arguments: argparse.Namespace) -> int:
    """Run one P6 part-1 step and write its evidence; nonzero on any failed check."""
    step = str(arguments.p6_step)
    evidence = gate.Evidence(f"P6-{step}", gate._utc_stamp())
    evidence.document["step"] = step
    # Step 7 of the deploy procedure: SHA, the set's asset readback and the import scan (S6-11).
    _, ready = gate._preflight(target, arguments, evidence)
    evidence.document["preflight_passed"] = ready
    passed = False
    try:
        # b0-finish always runs, so a failed readback never leaves the mode at LEGACY; the other
        # steps make no CAS and no landing unless the deployed set reads back first.
        if ready or step == "b0-finish":
            runner = {
                "a": _step_a,
                "b0-start": _step_b0_start,
                "b0-finish": _step_b0_finish,
                "alarm-window": _step_alarm_window,
            }
            passed = runner[step](target, arguments, evidence) and ready
    except gate.ProbeError as error:
        evidence.document["error"] = str(error)
        passed = False
    evidence.document["passed"] = passed
    path = evidence.write(target)
    print(f"evidence: {path}")  # noqa: T201
    return 0 if passed else 1
