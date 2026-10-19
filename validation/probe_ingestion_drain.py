# SPDX-License-Identifier: MIT-0
"""Observe one dev ingestion-queue drain and produce a sanitized performance report."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn, cast

import boto3
from botocore.exceptions import ClientError

from adapters.jobs.span_retrieval.client import LogsInsightsSpanClient
from adapters.jobs.span_retrieval.parse import SpanRetrievalFailedError
from adapters.jobs.span_retrieval.query import (
    SpanQueryRefusedError,
    build_span_query,
    declared_span_attributes,
)
from adapters.outbound.cloudformation_stack import stack_resource_summaries

if TYPE_CHECKING:
    from types_boto3_cloudformation.client import CloudFormationClient
    from types_boto3_cloudwatch.client import CloudWatchClient
    from types_boto3_cloudwatch.literals import StatisticType
    from types_boto3_cloudwatch.type_defs import DimensionTypeDef
    from types_boto3_lambda.client import LambdaClient
    from types_boto3_logs.client import CloudWatchLogsClient
    from types_boto3_sqs.client import SQSClient

_ROOT: Final = Path(__file__).resolve().parents[1]
_EVIDENCE_ROOT: Final = (_ROOT / "validation/evidence").resolve()
_ENVIRONMENT: Final = "dev"
_REGION: Final = "us-east-1"
_STACK: Final = "Adlytics-Ingestion-dev"
_FUNCTION: Final = "adlytics-dev-IngestionFunction"
_QUEUE: Final = "adlytics-dev-ingestion"
_DLQ: Final = "adlytics-dev-ingestion-dlq"
_LOG_GROUP: Final = f"/aws/lambda/{_FUNCTION}"
_TERMINAL_QUERY_STATES: Final = frozenset({"Complete", "Failed", "Cancelled", "Timeout"})
_ACCOUNT_DIGITS: Final = 12
_BATCH_SIZE: Final = 1
_MAX_CONCURRENCY: Final = 2
_EMPTY_CONFIRMATIONS: Final = 2


class ProbeError(RuntimeError):
    """A bounded drain-observation failure safe to print."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise ProbeError("invalid explicit observer arguments")


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """Every explicit target and observation bound for one drain interval."""

    profile: str
    environment: str
    region: str
    expected_account: str
    ingestion_stack: str
    mapping_uuid: str
    poll_seconds: int
    max_seconds: int
    evidence_dir: Path


@dataclass(frozen=True, slots=True)
class QueueState:
    """One non-consuming queue snapshot."""

    visible: int
    in_flight: int
    delayed: int

    @property
    def total(self) -> int:
        """Approximate messages not yet acknowledged."""
        return self.visible + self.in_flight + self.delayed


@dataclass(frozen=True, slots=True)
class LifecycleState:
    """Structured log counts observed since the frozen start instant."""

    terminals: int
    progress: int
    errors: int
    retries: int


@dataclass(frozen=True, slots=True)
class DrainSample:
    """One timestamped operational status point."""

    timestamp: str
    elapsed_seconds: int
    mapping_state: str
    last_processing_result: str
    queue: QueueState
    dlq: QueueState
    lifecycle: LifecycleState
    completed_per_minute: float | None
    eta_seconds: int | None


@dataclass(frozen=True, slots=True)
class Resources:
    """Exact stack-owned resources bound before observation."""

    function_name: str
    queue_url: str
    dlq_url: str


def _non_blank(value: str) -> str:
    selected = value.strip()
    if not selected:
        raise argparse.ArgumentTypeError("value must not be blank")
    return selected


def _profile(value: str) -> str:
    selected = _non_blank(value)
    if selected.casefold() == "default":
        raise argparse.ArgumentTypeError("an explicit nondefault profile is required")
    return selected


def _account(value: str) -> str:
    selected = _non_blank(value)
    if len(selected) != _ACCOUNT_DIGITS or not selected.isdigit():
        raise argparse.ArgumentTypeError("expected account must be 12 digits")
    return selected


def _positive(value: str) -> int:
    try:
        selected = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be an integer") from error
    if selected < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return selected


def _evidence_dir(value: str) -> Path:
    selected = Path(value).resolve()
    if not selected.is_relative_to(_EVIDENCE_ROOT):
        raise argparse.ArgumentTypeError("evidence directory must be beneath validation/evidence")
    return selected


def _arguments() -> ProbeRequest:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=_profile)
    parser.add_argument("--environment", required=True, type=_non_blank)
    parser.add_argument("--region", required=True, type=_non_blank)
    parser.add_argument("--expected-account", required=True, type=_account)
    parser.add_argument("--ingestion-stack", required=True, type=_non_blank)
    parser.add_argument("--mapping-uuid", required=True, type=_non_blank)
    parser.add_argument("--poll-seconds", required=True, type=_positive)
    parser.add_argument("--max-seconds", required=True, type=_positive)
    parser.add_argument("--evidence-dir", required=True, type=_evidence_dir)
    args = parser.parse_args()
    return ProbeRequest(
        args.profile,
        args.environment,
        args.region,
        args.expected_account,
        args.ingestion_stack,
        args.mapping_uuid,
        args.poll_seconds,
        args.max_seconds,
        args.evidence_dir,
    )


def _validate_target(request: ProbeRequest) -> None:
    if request.environment != _ENVIRONMENT or request.region != _REGION:
        raise ProbeError("observer accepts only the governed dev target")
    if request.ingestion_stack != _STACK:
        raise ProbeError("observer requires the exact ingestion dev stack")
    if request.poll_seconds >= request.max_seconds:
        raise ProbeError("poll interval must be shorter than the observation bound")


def _stack_resources(  # noqa: PLR0917
    request: ProbeRequest,
    cloudformation: CloudFormationClient,
    sqs: SQSClient,
    lambda_client: LambdaClient,
) -> Resources:
    resources = stack_resource_summaries(cloudformation, request.ingestion_stack)
    physical = {
        (str(item.get("LogicalResourceId", "")), str(item.get("ResourceType", ""))): str(
            item.get("PhysicalResourceId", "")
        )
        for item in resources
    }
    functions = {
        value
        for (_logical, resource_type), value in physical.items()
        if resource_type == "AWS::Lambda::Function"
    }
    queues = {
        value
        for (_logical, resource_type), value in physical.items()
        if resource_type == "AWS::SQS::Queue"
    }
    mappings = {
        value
        for (_logical, resource_type), value in physical.items()
        if resource_type == "AWS::Lambda::EventSourceMapping"
    }
    queue_owned = any(value.endswith(f"/{_QUEUE}") for value in queues)
    dlq_owned = any(value.endswith(f"/{_DLQ}") for value in queues)
    if _FUNCTION not in functions or not queue_owned or not dlq_owned:
        raise ProbeError("exact ingestion stack ownership is incomplete")
    if request.mapping_uuid not in mappings:
        raise ProbeError("mapping UUID is not owned by the exact ingestion stack")
    queue_url = sqs.get_queue_url(
        QueueName=_QUEUE,
        QueueOwnerAWSAccountId=request.expected_account,
    )["QueueUrl"]
    dlq_url = sqs.get_queue_url(
        QueueName=_DLQ,
        QueueOwnerAWSAccountId=request.expected_account,
    )["QueueUrl"]
    mapping = lambda_client.get_event_source_mapping(UUID=request.mapping_uuid)
    if (
        not str(mapping.get("FunctionArn", "")).endswith(f":function:{_FUNCTION}")
        or not str(mapping.get("EventSourceArn", "")).endswith(f":{_QUEUE}")
        or mapping.get("BatchSize") != _BATCH_SIZE
        or mapping.get("ScalingConfig", {}).get("MaximumConcurrency") != _MAX_CONCURRENCY
    ):
        raise ProbeError("event-source mapping differs from the reviewed drain contract")
    return Resources(_FUNCTION, queue_url, dlq_url)


def _queue_state(client: SQSClient, queue_url: str) -> QueueState:
    attributes = client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
        ],
    ).get("Attributes", {})
    return QueueState(
        int(attributes.get("ApproximateNumberOfMessages", "0")),
        int(attributes.get("ApproximateNumberOfMessagesNotVisible", "0")),
        int(attributes.get("ApproximateNumberOfMessagesDelayed", "0")),
    )


def _log_events(logs: CloudWatchLogsClient, start_ms: int) -> tuple[dict[str, object], ...]:
    events: list[dict[str, object]] = []
    token: str | None = None
    while True:
        response = (
            logs.filter_log_events(logGroupName=_LOG_GROUP, startTime=start_ms)
            if token is None
            else logs.filter_log_events(
                logGroupName=_LOG_GROUP,
                startTime=start_ms,
                nextToken=token,
            )
        )
        events.extend(cast("list[dict[str, object]]", response.get("events", [])))
        next_token = response.get("nextToken")
        if not isinstance(next_token, str) or not next_token or next_token == token:
            return tuple(events)
        token = next_token


def _lifecycle(logs: CloudWatchLogsClient, start_ms: int) -> LifecycleState:
    terminals = 0
    progress = 0
    errors = 0
    retry_executions: set[str] = set()
    for event in _log_events(logs, start_ms):
        message = event.get("message")
        if not isinstance(message, str):
            continue
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        record = cast("dict[str, object]", parsed)
        if record.get("operation") != "ingest_delivery":
            continue
        phase = record.get("phase")
        if phase == "terminal" and record.get("outcome") == "completed":
            terminals += 1
        if phase == "progress":
            progress += 1
        if record.get("error_code") is not None or record.get("level") == "ERROR":
            errors += 1
        predecessor = record.get("predecessor_execution_id")
        execution = record.get("execution_id") or record.get("correlation_id")
        if predecessor not in {None, "", "none"} and isinstance(execution, str):
            retry_executions.add(execution)
    return LifecycleState(terminals, progress, errors, len(retry_executions))


def _mapping_state(client: LambdaClient, uuid: str) -> tuple[str, str]:
    mapping = client.get_event_source_mapping(UUID=uuid)
    return str(mapping.get("State", "UNKNOWN")), str(
        mapping.get("LastProcessingResult") or "none"
    )


def _rate_and_eta(
    lifecycle: LifecycleState,
    queue: QueueState,
    elapsed_seconds: int,
) -> tuple[float | None, int | None]:
    if lifecycle.terminals < 1 or elapsed_seconds < 1:
        return None, None
    per_minute = lifecycle.terminals * 60 / elapsed_seconds
    eta = None if per_minute <= 0 else math.ceil(queue.total / per_minute * 60)
    return round(per_minute, 3), eta


def _query_insights(  # noqa: PLR0917
    logs: CloudWatchLogsClient,
    group: str,
    start_seconds: int,
    end_seconds: int,
    query: str,
) -> dict[str, object]:
    started = logs.start_query(
        logGroupName=group,
        startTime=start_seconds,
        endTime=end_seconds,
        queryString=query,
        limit=1_000,
    )
    query_id = started["queryId"]
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        result = cast("dict[str, object]", logs.get_query_results(queryId=query_id))
        status = result.get("status")
        if status == "Complete":
            return result
        if status in _TERMINAL_QUERY_STATES:
            raise ProbeError("performance Logs Insights query failed")
        time.sleep(2)
    raise ProbeError("performance Logs Insights query remained pending")


def _metric(  # noqa: PLR0917
    cloudwatch: CloudWatchClient,
    namespace: str,
    metric_name: str,
    dimensions: list[DimensionTypeDef],
    start: datetime,
    end: datetime,
    statistic: StatisticType,
) -> float | None:
    response = cloudwatch.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dimensions,
        StartTime=start,
        EndTime=end,
        Period=60,
        Statistics=[statistic],
    )
    values: list[float] = []
    for point in response.get("Datapoints", []):
        value = point.get(statistic)
        if isinstance(value, int | float):
            values.append(float(value))
    if not values:
        return None
    return max(values) if statistic == "Maximum" else sum(values)


def _percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile over the retrieved spans (exact, not Logs Insights' estimate)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _window_spans(
    logs: CloudWatchLogsClient, started_at: datetime, ended_at: datetime, *, names: list[str]
) -> dict[str, object]:
    """Read the window's named spans through the 5a span-retrieval module."""
    try:
        query = build_span_query(
            environment=_ENVIRONMENT,
            window_start=started_at,
            window_end=ended_at,
            declared_attributes=declared_span_attributes(),
            span_names=names,
            limit=10_000,
            max_pages=16,
        )
        result = LogsInsightsSpanClient(logs, timeout_seconds=180.0).retrieve(query)
    except (SpanQueryRefusedError, SpanRetrievalFailedError) as error:
        raise ProbeError("performance span retrieval failed") from error
    if result["reason"] == "query_running":
        raise ProbeError("performance span retrieval remained pending")
    return result


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _latency(values: list[float]) -> dict[str, object]:
    return {
        "p50_ms": _percentile(values, 0.5),
        "p95_ms": _percentile(values, 0.95),
        "max_ms": max(values) if values else None,
    }


def _retrieval(result: dict[str, object]) -> dict[str, object]:
    return {key: result[key] for key in ("status", "reason", "pages", "truncated")}


def _span_latency(
    logs: CloudWatchLogsClient, started_at: datetime, ended_at: datetime
) -> dict[str, object]:
    names = ["ingest.delivery", "ingest.fetch", "ingest.parse", "ingest.partition"]
    result = _window_spans(logs, started_at, ended_at, names=names)
    durations: dict[str, list[float]] = {name: [] for name in names}
    for span in cast("list[dict[str, object]]", result["spans"]):
        duration = _number(span.get("durationNano"))
        if duration is not None:
            durations[str(span.get("name"))].append(duration / 1e6)
    return {
        **_retrieval(result),
        "by_name": {
            name: {"spans": len(values), **_latency(values)}
            for name, values in durations.items()
            if values
        },
    }


def _athena_latency(
    logs: CloudWatchLogsClient, started_at: datetime, ended_at: datetime
) -> dict[str, object]:
    result = _window_spans(logs, started_at, ended_at, names=["athena.statement"])
    totals: list[float] = []
    sums = {"queue_ms": 0.0, "planning_ms": 0.0, "engine_ms": 0.0}
    for span in cast("list[dict[str, object]]", result["spans"]):
        attributes = cast("dict[str, object]", span.get("attributes") or {})
        total = _number(attributes.get("total_millis"))
        if total is not None:
            totals.append(total)
        for name, attribute in (
            ("queue_ms", "queue_millis"),
            ("planning_ms", "planning_millis"),
            ("engine_ms", "engine_millis"),
        ):
            sums[name] += _number(attributes.get(attribute)) or 0.0
    latency = _latency(totals)
    return {
        **_retrieval(result),
        "statements": len(cast("list[object]", result["spans"])),
        "p50_total_ms": latency["p50_ms"],
        "p95_total_ms": latency["p95_ms"],
        "max_total_ms": latency["max_ms"],
        **sums,
    }


def _aggregate_report(  # noqa: PLR0917
    logs: CloudWatchLogsClient,
    cloudwatch: CloudWatchClient,
    started_at: datetime,
    ended_at: datetime,
) -> dict[str, object]:
    start_seconds = int(started_at.timestamp())
    end_seconds = int(ended_at.timestamp())
    lambda_latency = _query_insights(
        logs,
        _LOG_GROUP,
        start_seconds,
        end_seconds,
        "filter @type = 'REPORT' | stats count(*) as attempts, "
        "pct(@duration, 50) as p50_ms, pct(@duration, 95) as p95_ms, "
        "max(@duration) as max_ms, sum(@billedDuration) as billed_ms",
    )
    span_latency = _span_latency(logs, started_at, ended_at)
    athena_latency = _athena_latency(logs, started_at, ended_at)
    function_dimension: list[DimensionTypeDef] = [
        {"Name": "FunctionName", "Value": _FUNCTION}
    ]
    queue_dimension: list[DimensionTypeDef] = [{"Name": "QueueName", "Value": _QUEUE}]
    return {
        "window": {
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "elapsed_seconds": int((ended_at - started_at).total_seconds()),
        },
        "logs_insights": {
            "lambda_attempt_latency": lambda_latency,
            "business_span_latency": span_latency,
            "athena_statement_latency": athena_latency,
        },
        "cloudwatch": {
            "lambda_invocations": _metric(
                cloudwatch,
                "AWS/Lambda",
                "Invocations",
                function_dimension,
                started_at,
                ended_at,
                "Sum",
            ),
            "lambda_errors": _metric(
                cloudwatch,
                "AWS/Lambda",
                "Errors",
                function_dimension,
                started_at,
                ended_at,
                "Sum",
            ),
            "lambda_throttles": _metric(
                cloudwatch,
                "AWS/Lambda",
                "Throttles",
                function_dimension,
                started_at,
                ended_at,
                "Sum",
            ),
            "max_concurrent_executions": _metric(
                cloudwatch,
                "AWS/Lambda",
                "ConcurrentExecutions",
                function_dimension,
                started_at,
                ended_at,
                "Maximum",
            ),
            "messages_received": _metric(
                cloudwatch,
                "AWS/SQS",
                "NumberOfMessagesReceived",
                queue_dimension,
                started_at,
                ended_at,
                "Sum",
            ),
            "messages_deleted": _metric(
                cloudwatch,
                "AWS/SQS",
                "NumberOfMessagesDeleted",
                queue_dimension,
                started_at,
                ended_at,
                "Sum",
            ),
            "max_oldest_message_age_seconds": _metric(
                cloudwatch,
                "AWS/SQS",
                "ApproximateAgeOfOldestMessage",
                queue_dimension,
                started_at,
                ended_at,
                "Maximum",
            ),
        },
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def _sample(  # noqa: PLR0917
    sqs: SQSClient,
    logs: CloudWatchLogsClient,
    lambda_client: LambdaClient,
    resources: Resources,
    request: ProbeRequest,
    start_ms: int,
    started_monotonic: float,
) -> DrainSample:
    queue = _queue_state(sqs, resources.queue_url)
    dlq = _queue_state(sqs, resources.dlq_url)
    lifecycle = _lifecycle(logs, start_ms)
    elapsed = max(0, int(time.monotonic() - started_monotonic))
    rate, eta = _rate_and_eta(lifecycle, queue, elapsed)
    mapping_state, processing = _mapping_state(lambda_client, request.mapping_uuid)
    return DrainSample(
        datetime.now(UTC).isoformat(),
        elapsed,
        mapping_state,
        processing,
        queue,
        dlq,
        lifecycle,
        rate,
        eta,
    )


def _console(sample: DrainSample) -> None:
    rate = "pending" if sample.completed_per_minute is None else str(sample.completed_per_minute)
    eta = "pending" if sample.eta_seconds is None else str(sample.eta_seconds)
    print(
        f"status elapsed_s={sample.elapsed_seconds} mapping={sample.mapping_state} "
        f"queue={sample.queue.visible}/{sample.queue.in_flight}/{sample.queue.delayed} "
        f"dlq={sample.dlq.visible}/{sample.dlq.in_flight}/{sample.dlq.delayed} "
        f"terminal={sample.lifecycle.terminals} progress={sample.lifecycle.progress} "
        f"errors={sample.lifecycle.errors} retries={sample.lifecycle.retries} "
        f"completed_per_min={rate} eta_s={eta}",
        flush=True,
    )


def _run(request: ProbeRequest) -> int:
    _validate_target(request)
    request.evidence_dir.mkdir(parents=True, exist_ok=False)
    session = boto3.Session(profile_name=request.profile, region_name=request.region)
    identity = session.client("sts").get_caller_identity()
    if str(identity.get("Account", "")) != request.expected_account:
        raise ProbeError("explicit profile account differs from expected account")
    sqs = session.client("sqs")
    logs = session.client("logs")
    lambda_client = session.client("lambda")
    cloudformation = session.client("cloudformation")
    resources = _stack_resources(request, cloudformation, sqs, lambda_client)
    started_at = datetime.now(UTC)
    start_ms = int(started_at.timestamp() * 1_000)
    started_monotonic = time.monotonic()
    baseline_queue = _queue_state(sqs, resources.queue_url)
    baseline_dlq = _queue_state(sqs, resources.dlq_url)
    _write_json(
        request.evidence_dir / "run.json",
        {
            "status": "in_progress",
            "started_at": started_at.isoformat(),
            "baseline_queue": asdict(baseline_queue),
            "baseline_dlq": asdict(baseline_dlq),
            "configured_batch_size": _BATCH_SIZE,
            "configured_max_concurrency": _MAX_CONCURRENCY,
        },
    )
    samples_path = request.evidence_dir / "samples.jsonl"
    empty_confirmations = 0
    status = "pending"
    final_sample: DrainSample | None = None
    while True:
        sample = _sample(
            sqs,
            logs,
            lambda_client,
            resources,
            request,
            start_ms,
            started_monotonic,
        )
        final_sample = sample
        with samples_path.open("a") as stream:
            stream.write(json.dumps(asdict(sample), sort_keys=True) + "\n")
        _console(sample)
        if sample.dlq.total != baseline_dlq.total or sample.lifecycle.errors:
            status = "failed"
            break
        processed_drain = (
            baseline_queue.total > 0
            and sample.mapping_state == "Enabled"
            and sample.lifecycle.terminals >= baseline_queue.total
        )
        empty_confirmations = (
            empty_confirmations + 1
            if sample.queue.total == 0 and processed_drain
            else 0
        )
        if empty_confirmations >= _EMPTY_CONFIRMATIONS:
            status = "drained"
            break
        if sample.elapsed_seconds >= request.max_seconds:
            status = "pending"
            break
        time.sleep(request.poll_seconds)
    ended_at = datetime.now(UTC)
    aggregates: dict[str, object]
    try:
        aggregates = _aggregate_report(
            logs, session.client("cloudwatch"), started_at, ended_at
        )
    except (ClientError, ProbeError) as error:
        aggregates = {"status": "pending", "cause": type(error).__name__}
    report = {
        "status": status,
        "baseline_queue": asdict(baseline_queue),
        "final": asdict(final_sample),
        "dlq_delta": final_sample.dlq.total - baseline_dlq.total,
        "aggregates": aggregates,
    }
    _write_json(request.evidence_dir / "summary.json", report)
    print(
        f"result status={status} baseline={baseline_queue.total} "
        f"remaining={final_sample.queue.total} completed={final_sample.lifecycle.terminals} "
        f"dlq_delta={report['dlq_delta']}",
        flush=True,
    )
    return 0 if status == "drained" else 1


def main() -> int:
    """Run the bounded read-only observer and preserve exact local evidence."""
    try:
        return _run(_arguments())
    except ProbeError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", "unknown"))
        print(f"FAILED: AWS request returned {code}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"FAILED: unexpected {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
