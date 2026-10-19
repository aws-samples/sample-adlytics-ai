# SPDX-License-Identifier: MIT-0
"""P5 (slice S5) for the traceability probe runner: measurement, stuck flows, staffed alarms.

Invariants (I), re-run by every later slice:

- the alarm inventory with the stuck-flow and graph alarms, every action exactly the staffed
  topic, and the destination verified (`python -m adapters.jobs.alarm_staffing verify`): at least
  one confirmed email subscription whose endpoint digest is the given one, never the address;
- metric fault controls with `AuthorityScope=probe`, through to an immutable `GetMetricData`
  confirmation receipt, in the probe table under the probe role: a failed then a conflicting
  acceptance, an ambiguous acceptance with a competitor first, an emission whose state change
  failed, a suppressed sink read (stays unconfirmed, re-emits), and a confirmation that failed
  before `DELIVERED`. Every contribution is in the aggregate exactly once;
- the live paths: the deployed detector's latest complete sweep of the live table reads zero, and
  one live run-request receipt's measurement is accepted and delivered with its receipt;
- V-15 extended to the detector and the metric reconciler (no action on any probe table), and the
  P0-P4 invariants in their S5 form (P4's probe). Owner-gate Item 20 (2026-10-08) retired the
  acknowledgement route this probe used to exercise.

Slice state (S), recorded once: a probe-scope stuck-flow obligation in the probe table, run
through the detector under the probe role: `ALARM -> OK` of the probe stuck-flow alarm, with
both actions executed against the staffed topic. That alarm is the only expected alarm.
"""

# The P5 probe composes the runner's and P3/P4's private helpers.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import probe_ingestion_traceability_gate as gate
import probe_ingestion_traceability_p3 as p3
import probe_ingestion_traceability_p4 as p4
from botocore.exceptions import ClientError

from adapters.jobs.execution_graph.stuck_flow import DetectorContext, detect, summary
from adapters.jobs.operational_status.metric_reconciler import (
    AcceptOutcome,
    PublishOutcome,
    ReconcileContext,
    accept_one,
    publish_one,
)
from adapters.outbound.dynamodb.due_shards import DynamoDueShardReader
from adapters.outbound.dynamodb.execution_graph import due_shard
from adapters.outbound.dynamodb.metric_store import AmbiguousCommitError, DynamoMetricStore
from adapters.outbound.telemetry.graph_metrics import (
    AuthorityScope,
    put_probe_stuck_flow_reading,
)
from adapters.outbound.telemetry.measurement_sink import (
    CloudWatchMeasurementReader,
    PutMetricDataSink,
)
from domain.operations.measurement import (
    Gauge,
    Measurement,
    SnapshotState,
    measurement,
    receipt_measurement,
)
from domain.operations.stuck_flow import PROTOCOL_CEILING, SweepReading

__all__ = ["EXPECTED_ALARMS", "alarm_transition_verdict", "probe_p5", "unexpected_alarms"]

PROBE_ALARM_SUFFIX: Final = "open-flows-past-ceiling-probe"
GRAPH_ALARM_SUFFIXES: Final = (
    "open-flows-past-ceiling",
    "open-flows-without-root-clock",
    "promote-obligation-age",
    PROBE_ALARM_SUFFIX,
    "ingestion-alarming-errors-execution-graph-stuck-flow-detector",
)
EXPECTED_ALARMS: Final = (PROBE_ALARM_SUFFIX,)
"""The only alarm P5 may fire, written into the evidence before the control runs."""
_POLL_SECONDS: Final = 20


# --- Pure verdicts (unit-tested) -------------------------------------------------------------


def alarm_transition_verdict(
    history: Sequence[Mapping[str, Any]], topic: str, since: datetime
) -> dict[str, object]:
    """Require `ALARM` then `OK`, each with its action executed against the staffed topic."""
    items = sorted(
        (item for item in history if cast("datetime", item["Timestamp"]) >= since),
        key=lambda item: cast("datetime", item["Timestamp"]),
    )
    states: list[str] = []
    actions: list[str] = []
    for item in items:
        summary_text = str(item.get("HistorySummary", ""))
        if item.get("HistoryItemType") == "StateUpdate":
            data = json.loads(str(item.get("HistoryData", "{}")))
            states.append(str(data.get("newState", {}).get("stateValue", "")))
        elif item.get("HistoryItemType") == "Action" and topic in summary_text.split():
            actions.append(summary_text)
    alarmed = "ALARM" in states
    cleared = alarmed and "OK" in states[states.index("ALARM") :]
    executed = [text for text in actions if text.startswith("Successfully executed action")]
    return {
        "states": states,
        "actions": actions,
        "passed": alarmed and cleared and len(executed) >= 2,  # noqa: PLR2004 -- ALARM and OK
    }


def unexpected_alarms(
    transitions: Mapping[str, Sequence[str]], environment: str, expected: Sequence[str]
) -> list[str]:
    """Every alarm that entered `ALARM` in the window and is not on the expected list."""
    allowed = {f"adlytics-{environment}-{suffix}" for suffix in expected}
    return sorted(
        name for name, states in transitions.items() if "ALARM" in states and name not in allowed
    )


# --- Faults over the real client -------------------------------------------------------------


@dataclass
class _Faults:
    """A DynamoDB client whose next matching transactions fail as scripted, then pass through."""

    client: Any
    plan: list[tuple[Callable[[list[dict[str, Any]]], bool], str]] = field(
        default_factory=list[tuple[Callable[[list[dict[str, Any]]], bool], str]]
    )
    fired: list[str] = field(default_factory=list[str])

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    def transact_write_items(self, **kwargs: Any) -> Any:
        members = cast("list[dict[str, Any]]", kwargs["TransactItems"])
        for index, (matches, fault) in enumerate(self.plan):
            if not matches(members):
                continue
            del self.plan[index]
            self.fired.append(fault)
            if fault == "fail":
                raise ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "injected before commit"}},
                    "TransactWriteItems",
                )
            if fault == "conflict":
                raise ClientError(
                    cast(
                        "Any",
                        {
                            "Error": {
                                "Code": "TransactionCanceledException",
                                "Message": "injected",
                            },
                            "CancellationReasons": [{"Code": "TransactionConflict"}] * len(members),
                        },
                    ),
                    "TransactWriteItems",
                )
            # "ambiguous": the transaction commits, then its response is lost.
            self.client.transact_write_items(**kwargs)
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "injected: response lost"}},
                "TransactWriteItems",
            )
        return self.client.transact_write_items(**kwargs)


def _touches(prefix: str) -> Callable[[list[dict[str, Any]]], bool]:
    def matches(members: list[dict[str, Any]]) -> bool:
        return prefix in json.dumps(members)

    return matches


def _probe_session(session: Any, environment: str) -> Any:
    """Assume the probe role: the probe tables only, never the live table (V-15)."""
    import boto3

    account = str(session.client("sts").get_caller_identity()["Account"])
    role = f"arn:aws:iam::{account}:role/adlytics-{environment}-execution-graph-probe"
    credentials = session.client("sts").assume_role(
        RoleArn=role, RoleSessionName=f"p5-{gate._utc_stamp().lower()}", DurationSeconds=3600
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=session.region_name,
    )


# --- Stuck-flow control (S) ------------------------------------------------------------------


def _alarm_state(session: Any, name: str) -> str:
    # aws-pagination: one exact alarm name returns at most one alarm.
    described = session.client("cloudwatch").describe_alarms(AlarmNames=[name])
    alarms = described.get("MetricAlarms", [])
    return str(alarms[0]["StateValue"]) if alarms else "ABSENT"


def _emit_probe(probe: Any, reading: SweepReading) -> None:
    put_probe_stuck_flow_reading(probe.client("cloudwatch"), reading)


def _await_state(
    operator: Any, probe: Any, alarm: str, want: str, reading: SweepReading, deadline_s: int
) -> dict[str, object]:
    start = time.monotonic()
    emitted = 0
    while time.monotonic() - start < deadline_s:
        _emit_probe(probe, reading)
        emitted += 1
        state = _alarm_state(operator, alarm)
        if state == want:
            return {
                "state": state,
                "emissions": emitted,
                "seconds": round(time.monotonic() - start),
            }
        time.sleep(_POLL_SECONDS)
    return {"state": _alarm_state(operator, alarm), "emissions": emitted, "timed_out": True}


def _stuck_flow_control(
    session: Any, probe: Any, environment: str, topic: str
) -> dict[str, object]:
    table = f"adlytics-{environment}-execution-graph-probe"
    dynamo = probe.client("dynamodb")
    reader = DynamoDueShardReader(table, dynamo)
    failures: list[BaseException] = []

    def no_emit(reading: SweepReading, scope: AuthorityScope) -> None:
        del reading, scope

    def sweep_now() -> SweepReading:
        return detect(
            DetectorContext(
                reader,
                no_emit,
                lambda: datetime.now(UTC),
                failures.append,
                AuthorityScope.PROBE,
            )
        )

    alarm = f"adlytics-{environment}-{PROBE_ALARM_SUFFIX}"
    started = datetime.now(UTC) - timedelta(seconds=5)
    unit = secrets.token_hex(32)
    shard = due_shard(unit)
    sk = f"INSTANTIATE#p5-stuck-{gate._utc_stamp().lower()}"
    root = datetime.now(UTC) - PROTOCOL_CEILING - timedelta(minutes=10)
    before = sweep_now()
    dynamo.put_item(
        TableName=table,
        Item={
            "pk": {"S": shard},
            "sk": {"S": sk},
            "item_kind": {"S": "instantiate"},
            "authority_epoch": {"N": "1"},
            "run_id": {"S": sk.removeprefix("INSTANTIATE#")},
            "due_at": {"S": datetime.now(UTC).isoformat()},
            "root_clock_started_at": {"S": root.isoformat()},
        },
        ConditionExpression="attribute_not_exists(pk)",
    )
    stuck = sweep_now()
    counted = [item.sk for item in stuck.past_ceiling]
    raised = _await_state(session, probe, alarm, "ALARM", stuck, 420)
    dynamo.delete_item(
        TableName=table,
        Key={"pk": {"S": shard}, "sk": {"S": sk}},
        ConditionExpression="attribute_exists(pk)",
    )
    clear = sweep_now()
    cleared = _await_state(session, probe, alarm, "OK", clear, 420)
    pages = (
        session.client("cloudwatch")
        .get_paginator("describe_alarm_history")
        .paginate(AlarmName=alarm, StartDate=started)
    )
    history = [item for page in pages for item in page["AlarmHistoryItems"]]
    transition = alarm_transition_verdict(history, topic, started)
    return {
        "obligation": {"shard": shard, "sk": sk, "root_clock_started_at": root.isoformat()},
        "before": summary(before),
        "stuck": summary(stuck),
        "clear": summary(clear),
        "raised": raised,
        "cleared": cleared,
        "transition": transition,
        "shard_failures": [str(error) for error in failures],
        "passed": counted == [sk]
        and before.past_ceiling_count == 0
        and clear.past_ceiling_count == 0
        and raised.get("state") == "ALARM"
        and cleared.get("state") == "OK"
        and bool(transition["passed"])
        and not failures,
    }


# --- Measurement fault controls (I) ----------------------------------------------------------


def _measurement_controls(probe: Any, environment: str) -> dict[str, object]:
    table = f"adlytics-{environment}-execution-graph-probe"
    faults = _Faults(probe.client("dynamodb"))
    store = DynamoMetricStore(table, cast("Any", faults), partition_key="pk", sort_key="sk")
    plain = DynamoMetricStore(table, probe.client("dynamodb"), partition_key="pk", sort_key="sk")
    cloudwatch = probe.client("cloudwatch")
    sink = PutMetricDataSink(cloudwatch)
    reader = CloudWatchMeasurementReader(cloudwatch)
    stamp = gate._utc_stamp().lower()
    now = datetime.now(UTC)
    dims = {
        "Protocol": "v1",
        "Surface": "graph",
        "Stage": "p5_fault_control",
        "Outcome": "ADMITTED",
        "AuthorityScope": "probe",
    }
    contributions: list[Measurement] = [
        measurement(f"P5#{stamp}#{index}", "FlowRootElapsed", dims, 1_000 * (index + 1), now)
        for index in range(3)
    ]
    aggregate = contributions[0].aggregate_id
    start = plain.read_aggregate(aggregate)
    steps: dict[str, object] = {}
    # 1. The projection transaction committed: three contributions, each PENDING.
    for item in contributions:
        probe.client("dynamodb").transact_write_items(
            TransactItems=cast("Any", plain.contribution_members(item))
        )
    # 2. Before acceptance, then a condition conflict: both re-read and accept once.
    faults.plan = [
        (_touches("AGGREGATE_MEMBER#"), "fail"),
        (_touches("AGGREGATE_MEMBER#"), "conflict"),
    ]
    steps["fail_then_conflict"] = accept_one(store, contributions[0].measurement_id).value
    # 3. An ambiguous acceptance whose competitor commits before the recovery read.
    original = store.read_acceptance
    reads: list[str] = []

    def competing(measurement_id: str) -> Any:
        reads.append(measurement_id)
        if measurement_id == contributions[1].measurement_id and len(reads) == 2:  # noqa: PLR2004
            steps["competitor"] = accept_one(plain, contributions[2].measurement_id).value
        return original(measurement_id)

    store.read_acceptance = competing  # type: ignore[method-assign]
    faults.plan = [(_touches(f"METRIC_ACCEPT#{contributions[1].measurement_id}"), "ambiguous")]
    steps["ambiguous_with_competitor"] = accept_one(store, contributions[1].measurement_id).value
    store.read_acceptance = original  # type: ignore[method-assign]
    context = ReconcileContext(store, sink, reader, lambda: datetime.now(UTC), lambda _e: None)
    # 4. Emitted, then the state change failed: the same snapshot is emitted again.
    faults.plan = [(_touches("EMITTED_UNCONFIRMED"), "fail")]
    try:
        publish_one(context, aggregate)
        steps["emit_then_crash"] = "no_fault"
    except AmbiguousCommitError:
        steps["emit_then_crash"] = "refused_state_change"
    steps["re_emit"] = publish_one(context, aggregate).value

    # 5. Local success, sink read suppressed: stays unconfirmed and re-emits.
    class _Suppressed:
        def read(self, gauges: Sequence[Gauge], timestamp: datetime) -> Mapping[Gauge, None]:
            del timestamp
            return dict.fromkeys(gauges)

    suppressed = ReconcileContext(
        store, sink, _Suppressed(), lambda: datetime.now(UTC), lambda _e: None
    )
    steps["suppressed_read"] = publish_one(suppressed, aggregate).value
    # 6. The confirmation fails before DELIVERED once, then confirms from GetMetricData.
    faults.plan = [(_touches("SINK_CONFIRMATION#"), "fail")]
    outcome, attempts, deadline = None, 0, time.monotonic() + 900
    while time.monotonic() < deadline:
        attempts += 1
        try:
            outcome = publish_one(context, aggregate)
        except AmbiguousCommitError:
            outcome = None
        if outcome is PublishOutcome.DELIVERED:
            break
        time.sleep(30)
    end = plain.read_aggregate(aggregate)
    snapshot = plain.read_snapshot(aggregate, end.revision)
    receipt = plain.read_confirmation(aggregate, end.revision)
    members = [plain.member_present(aggregate, item.measurement_id) for item in contributions]
    return {
        "table": table,
        "aggregate_id": aggregate,
        "start": {"revision": start.revision, "count": start.statistics.count},
        "end": {"revision": end.revision, "count": end.statistics.count},
        "steps": steps,
        "faults_fired": faults.fired,
        "confirm_attempts": attempts,
        "snapshot_state": None if snapshot is None else snapshot.state.value,
        "confirmation_receipt": None if receipt is None else sorted(receipt),
        "passed": steps.get("fail_then_conflict") == AcceptOutcome.ACCEPTED.value
        and steps.get("ambiguous_with_competitor") == AcceptOutcome.ALREADY_ACCEPTED.value
        and steps.get("competitor") == AcceptOutcome.ACCEPTED.value
        and steps.get("emit_then_crash") == "refused_state_change"
        and steps.get("re_emit") == PublishOutcome.EMITTED.value
        and steps.get("suppressed_read") == PublishOutcome.UNCONFIRMED.value
        and sorted(faults.fired) == sorted(["fail", "conflict", "ambiguous", "fail", "fail"])
        and outcome is PublishOutcome.DELIVERED
        and end.statistics.count == start.statistics.count + 3
        and end.revision == start.revision + 3
        and all(members)
        and snapshot is not None
        and snapshot.state is SnapshotState.DELIVERED
        and receipt is not None,
    }


# --- Live paths (I) --------------------------------------------------------------------------


def _live_detector(session: Any, environment: str) -> dict[str, object]:
    end = datetime.now(UTC)
    data = session.client("cloudwatch").get_metric_data(
        MetricDataQueries=[
            {
                "Id": "live",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "Adlytics",
                        "MetricName": "OpenFlowsPastProtocolCeiling",
                        "Dimensions": [
                            {"Name": "Protocol", "Value": "v1"},
                            {"Name": "AuthorityScope", "Value": "live"},
                        ],
                    },
                    "Period": 60,
                    "Stat": "Maximum",
                },
            }
        ],
        StartTime=end - timedelta(minutes=15),
        EndTime=end,
    )
    values = [float(v) for result in data["MetricDataResults"] for v in result.get("Values", [])]
    state = _alarm_state(session, f"adlytics-{environment}-open-flows-past-ceiling")
    return {
        "datapoints": len(values),
        "maximum": max(values) if values else None,
        "alarm_state": state,
        "passed": len(values) >= 5 and max(values) == 0 and state == "OK",  # noqa: PLR2004
    }


def _status_table(session: Any, environment: str) -> str:
    resources = gate.stack_resources(
        session.client("cloudformation"), f"Adlytics-OperationalStatusStore-{environment}"
    )
    tables = [r for r in resources if r["ResourceType"] == "AWS::DynamoDB::Table"]
    if len(tables) != 1:
        raise gate.ProbeError("the status store does not have exactly one table")
    return str(tables[0]["PhysicalResourceId"])


def _live_measurement(  # noqa: PLR0917 -- the runner's inputs and the admit argv
    target: gate.Target,
    arguments: argparse.Namespace,
    session: Any,
    graph_table: str,
    admit: Sequence[str],
) -> dict[str, object]:
    request_id = f"p5-live-measure-{gate._utc_stamp().lower()}"
    _, admitted = gate._operator(target, arguments, *admit, "--request-id", request_id)
    item = (
        session.client("dynamodb")
        .get_item(
            TableName=graph_table,
            Key={"pk": {"S": f"RECEIPT#{request_id}"}, "sk": {"S": "RECEIPT"}},
            ConsistentRead=True,
        )
        .get("Item", {})
    )
    plain = {name: next(iter(value.values())) for name, value in item.items()}
    contribution = receipt_measurement(
        authority_key=f"RECEIPT#{request_id}/RECEIPT",
        authority_epoch=int(str(plain.get("authority_epoch", "0"))),
        decision=str(plain.get("decision", "")),
        recorded_at=str(plain.get("recorded_at", "")),
        root_clock_started_at=cast("str | None", plain.get("root_clock_started_at")),
    )
    if contribution is None:
        return {"request_id": request_id, "admitted": admitted, "passed": False}
    store = DynamoMetricStore(
        _status_table(session, target.environment), session.client("dynamodb")
    )
    deadline = time.monotonic() + int(arguments.wait_seconds)
    state: str | None = None
    while time.monotonic() < deadline:
        acceptance = store.read_acceptance(contribution.measurement_id)
        if acceptance is not None and acceptance.revision is not None:
            current = store.read_aggregate(contribution.aggregate_id)
            snapshot = store.read_snapshot(contribution.aggregate_id, current.revision)
            state = None if snapshot is None else snapshot.state.value
            if state == SnapshotState.DELIVERED.value:
                break
        time.sleep(30)
    current = store.read_aggregate(contribution.aggregate_id)
    receipt = store.read_confirmation(contribution.aggregate_id, current.revision)
    return {
        "request_id": request_id,
        "decision": plain.get("decision"),
        "measurement_id": contribution.measurement_id,
        "value_ms": contribution.value_ms,
        "measurement_present": store.read_measurement(contribution.measurement_id) is not None,
        "aggregate_revision": current.revision,
        "snapshot_state": state,
        "confirmation_receipt": receipt is not None,
        "passed": state == SnapshotState.DELIVERED.value and receipt is not None,
    }


def _destination(target: gate.Target, arguments: argparse.Namespace) -> dict[str, object]:
    completed = subprocess.run(  # noqa: S603 -- fixed argv, explicit inputs only
        [
            "uv",
            "run",
            "python",
            "-m",
            "adapters.jobs.alarm_staffing",
            "verify",
            "--profile",
            target.profile,
            "--region",
            target.region,
            "--expected-account",
            target.expected_account,
            "--env",
            target.environment,
            "--endpoint-sha256",
            str(arguments.staffed_endpoint_sha256),
        ],
        cwd=Path(arguments.worktree).resolve(),
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT ")]
    if len(lines) != 1:
        raise gate.ProbeError(f"verify printed {len(lines)} results: {completed.stderr[-300:]}")
    return cast("dict[str, object]", json.loads(lines[0][7:]))


def _isolation_extension(session: Any, environment: str) -> dict[str, object]:
    account = str(session.client("sts").get_caller_identity()["Account"])
    region = str(session.region_name)
    probe_arns = [
        f"arn:aws:dynamodb:{region}:{account}:table/adlytics-{environment}-{suffix}"
        for suffix in ("execution-graph-probe", "execution-graph-probe-legacy")
    ]
    roles = {
        **p3._stack_roles(
            session, f"Adlytics-Ingestion-{environment}", ["ExecutionGraphStuckFlowDetector"]
        ),
        **p3._stack_roles(
            session, f"Adlytics-OperationalStatusProcessor-{environment}", ["MetricReconciler"]
        ),
    }
    iam = session.client("iam")
    decisions = {
        f"{name}#{index}|probe": p3._simulate(iam, role, arn)
        for name, role in roles.items()
        for index, arn in enumerate(probe_arns)
    }
    verdict = p3.iam_isolation_verdict(decisions)
    verdict["roles"] = roles
    verdict["passed"] = bool(verdict["passed"]) and len(roles) == 2  # noqa: PLR2004
    return verdict


def _transitions(session: Any, environment: str, since: datetime) -> dict[str, list[str]]:
    """Every state each of this environment's alarms entered since the window began.

    `DescribeAlarmHistory` takes one alarm name or none, so the account's history is read once and
    filtered to this environment's alarm prefix.
    """
    prefix = f"adlytics-{environment}-"
    found: dict[str, list[str]] = {}
    paginator = session.client("cloudwatch").get_paginator("describe_alarm_history")
    for page in paginator.paginate(HistoryItemType="StateUpdate", StartDate=since):
        for item in page.get("AlarmHistoryItems", []):
            name = str(item["AlarmName"])
            if not name.startswith(prefix):
                continue
            data = json.loads(str(item.get("HistoryData", "{}")))
            state = str(data.get("newState", {}).get("stateValue", ""))
            found.setdefault(name, []).append(state)
    return found


def _passed(document: object) -> bool:
    return bool(cast("dict[str, object]", document).get("passed"))


def probe_p5(target: gate.Target, arguments: argparse.Namespace) -> int:
    """Run P5 and write its evidence; nonzero on any failed check."""
    evidence = gate.Evidence("P5", gate._utc_stamp())
    evidence.document["expected_alarms"] = [
        f"adlytics-{target.environment}-{suffix}: ALARM then OK (P5 probe control)"
        for suffix in EXPECTED_ALARMS
    ]
    window_start = datetime.now(UTC)
    try:
        session, ready = gate._preflight(target, arguments, evidence)
        headers = gate.diff_header_check(
            [Path(path) for path in arguments.diff_file],
            cast("str | None", evidence.document.get("deployed_sha")),
        )
        evidence.document["diff_headers"] = headers
        p4_doc = json.loads(Path(arguments.p4_evidence).read_text())
        if not p4_doc.get("passed"):
            raise gate.ProbeError("the P4 evidence did not pass")
        if not ready:
            raise gate.ProbeError("asset readback, import scan or alarm inventory failed")
        inventory = cast("dict[str, object]", evidence.document["alarm_inventory"])
        alarms = cast("dict[str, dict[str, object]]", inventory["alarms"])
        topic = str(inventory["topic"])
        graph_alarms = {
            suffix: alarms.get(f"adlytics-{target.environment}-{suffix}")
            for suffix in GRAPH_ALARM_SUFFIXES
        }
        destination = _destination(target, arguments)
        evidence.document["destination"] = destination
        graph_table = gate._store_table(session, target.environment, str(arguments.store_stack))
        probe = _probe_session(session, target.environment)
        evidence.document["measurement_controls"] = _measurement_controls(probe, target.environment)
        evidence.document["stuck_flow_control"] = _stuck_flow_control(
            session, probe, target.environment, topic
        )
        evidence.document["live_detector"] = _live_detector(session, target.environment)
        p1 = json.loads(Path(arguments.p1_evidence).read_text())
        key = str(p1["p0_probe_key"])
        versions = gate._exact_versions(session.client("s3"), arguments.bucket, key)
        admit = (
            "admit",
            "--manifest-key",
            key,
            "--version-id",
            versions[0],
            "--reason",
            "operator_rerun",
            "--raw-stack",
            str(arguments.raw_stack),
            "--bucket",
            str(arguments.bucket),
        )
        evidence.document["live_measurement"] = _live_measurement(
            target, arguments, session, graph_table, admit
        )
        evidence.document["isolation_extension"] = _isolation_extension(session, target.environment)
        # P0-P4 invariants in their S5 form: P4's own probe (release live tests, attribution and
        # the P0-P3 invariants it re-runs), recorded as a nested evidence file.
        p4_run = p4.probe_p4(target, arguments)
        evidence.document["p4_rerun_exit"] = p4_run
        transitions = _transitions(session, target.environment, window_start)
        unexpected = unexpected_alarms(transitions, target.environment, EXPECTED_ALARMS)
        evidence.document["alarm_transitions"] = transitions
        evidence.document["unexpected_alarms"] = unexpected
        checks: dict[str, bool] = {
            "preflight": ready,
            "diff_headers_name_the_deployed_sha": bool(headers["passed"]),
            "graph_alarms_present_on_the_staffed_topic": all(
                item is not None and item.get("actions") == [topic]
                for item in graph_alarms.values()
            ),
            "destination_confirmed_and_exact_actions": bool(destination.get("passed")),
            "metric_fault_controls_to_confirmation_receipt": _passed(
                evidence.document["measurement_controls"]
            ),
            "stuck_flow_alarm_to_ok_at_the_staffed_destination": _passed(
                evidence.document["stuck_flow_control"]
            ),
            "live_detector_complete_zero": _passed(evidence.document["live_detector"]),
            "live_measurement_delivered": _passed(evidence.document["live_measurement"]),
            "v15_detector_reconciler_no_probe_action": _passed(
                evidence.document["isolation_extension"]
            ),
            "p0_p4_invariants": p4_run == 0,
            "no_unexpected_alarm": not unexpected,
        }
        evidence.document["graph_alarms"] = graph_alarms
        evidence.document["checks"] = checks
        evidence.document["passed"] = all(checks.values())
    except Exception as error:
        evidence.document["passed"] = False
        evidence.document["error"] = f"{type(error).__name__}: {error}"[:2000]
    path = evidence.write(target)
    print(f"evidence: {path}")
    return 0 if evidence.document.get("passed") else 1
