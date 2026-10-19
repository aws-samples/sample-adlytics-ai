# SPDX-License-Identifier: MIT-0
"""Report whether the dev pipeline has settled after a landing phase (design §2.3, R5-8).

Read-only. Settled is signal-driven (owner, 2026-10-06): the probe polls every 60 s and reports
settled at the first pair of consecutive reads 60 s apart for which every provisioned pipeline
queue holds no visible, in-flight or delayed message, every dead-letter queue holds the number
it held in the baseline taken before the phase, no CURRENT pointer changed between the two
reads, and no preset CURRENT pointer advanced within the quiet period (3 minutes by default),
measured from the pointer table's own timestamps. `--observe-minutes` (10 by default) is only
the maximum: at that point the probe reports not settled with the last findings.

The quiet period's evidence: the pointer table stamps a superseded revision with
`expires_at = advance + 30 days` (`SUPERSEDED_HISTORY_RETENTION`), so the last advance of each
variant is that value minus 30 days. On 2026-10-06 the slowest observed fan-out from a write to
a pointer advance was about 43 s (Publisher deploy 05:48:18Z, campaigns pointers 05:49:01Z;
retail attested 08:58:39Z, pointers 08:58:56Z), so 3 minutes is more than four times it.

`--capture-baseline` writes the baseline before a phase. A full run writes a report to
`validation/evidence/reports/` and, with `--settled-report`, the `pipeline-settled` v1 file the
landing job's seal guard requires (`land_fixtures/job.py` `check_settled_report`).

The fan-out figures are measured over `--since` .. now from CloudWatch metrics the named
stacks own; they are evidence, never part of the settled decision.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn, cast

import boto3
from botocore.exceptions import ClientError
from probe_deadlines import ProbeDeadlineError, bounded_client, within

from adapters.jobs.preset_artifact_publisher.campaign_source import SUPERSEDED_HISTORY_RETENTION
from adapters.outbound.cloudformation_stack import stack_resource_summaries

if TYPE_CHECKING:
    from types_boto3_cloudformation.client import CloudFormationClient
    from types_boto3_cloudwatch.client import CloudWatchClient
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_sqs.client import SQSClient

_ROOT: Final = Path(__file__).resolve().parents[1]
_ENVIRONMENT: Final = "dev"
_REGION: Final = "us-east-1"
_REPORTS: Final = _ROOT / "validation" / "evidence" / "reports"
QUEUES: Final = (
    "adlytics-dev-ingestion",
    "adlytics-dev-registration",
    "adlytics-dev-semantic-write.fifo",
    "adlytics-dev-preset-bake.fifo",
    "adlytics-dev-InsightContinuation",
)
"""The five pipeline queues (design §2.3, verified against the stack sources)."""
_POINTER_TABLE: Final = "adlytics-dev-preset-artifact-pointers"
_POINTER_STACK: Final = "Adlytics-PublisherArtifactStore-dev"
_INGESTION_STACK: Final = "Adlytics-Ingestion-dev"
_INGESTION_QUEUE: Final = "adlytics-dev-ingestion"
_REGISTRATION_QUEUE: Final = "adlytics-dev-registration"
_CURRENT_RECORD: Final = "CURRENT"
_PRECOMPUTE_FUNCTION: Final = "adlytics-dev-InsightPrecompute"
_BAKE_QUEUE: Final = "adlytics-dev-preset-bake.fifo"
_CONTINUATION_QUEUE: Final = "adlytics-dev-InsightContinuation"
_READ_GAP_SECONDS: Final = 60.0
_DEFAULT_OBSERVE_MINUTES: Final = 10
_DEFAULT_QUIET_SECONDS: Final = 180
_RUN_MARGIN_SECONDS: Final = 300
"""The run deadline is the observation maximum plus this margin for the reads around it."""
_ACCOUNT_DIGITS: Final = 12


class ProbeError(Exception):
    """A bounded failure safe to print."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ProbeError(f"invalid arguments: {message}")


@dataclass(frozen=True, slots=True)
class QueueDepth:
    """One queue's three approximate depths."""

    visible: int
    in_flight: int
    delayed: int

    @property
    def empty(self) -> bool:
        """Whether the queue holds no message in any state."""
        return self.visible == self.in_flight == self.delayed == 0


@dataclass(frozen=True, slots=True)
class PipelineRead:
    """One read of every pipeline queue, its dead-letter queue, and the CURRENT pointers."""

    queues: Mapping[str, QueueDepth]
    dead_letters: Mapping[str, int]
    pointers: Mapping[str, str] = field(default_factory=dict[str, str])
    advanced_at: Mapping[str, datetime | None] = field(default_factory=dict[str, "datetime | None"])


def settled_findings(
    baseline: PipelineRead,
    first: PipelineRead,
    second: PipelineRead,
    expected: tuple[str, ...] = QUEUES,
) -> tuple[str, ...]:
    """Name every unsettled reason across the two end reads (empty when settled).

    `baseline` is the read taken before the phase (its dead-letter depths) or at the start of
    the observation window (its pointers). `expected` is the provisioned queue set
    (`provisioned_queues`).
    """
    findings: list[str] = []
    for label, read in (("first read", first), ("second read", second)):
        for name in expected:
            depth = read.queues.get(name)
            if depth is None:
                findings.append(f"{label}: {name} was not read")
            elif not depth.empty:
                findings.append(
                    f"{label}: {name} holds visible={depth.visible} "
                    f"in_flight={depth.in_flight} delayed={depth.delayed}"
                )
        for name, count in sorted(read.dead_letters.items()):
            before = baseline.dead_letters.get(name)
            if before is None:
                findings.append(f"{label}: no baseline for dead-letter queue {name}")
            elif count != before:
                findings.append(f"{label}: dead-letter queue {name} moved {before} -> {count}")
    for name in sorted(set(second.pointers) | set(baseline.pointers)):
        if baseline.pointers.get(name) != second.pointers.get(name):
            findings.append(f"pointer {name} advanced during the observation window")
    if first.pointers != second.pointers:
        findings.append("a pointer advanced between the two end reads")
    return tuple(findings)


def quiet_findings(read: PipelineRead, now: datetime, quiet_seconds: float) -> tuple[str, ...]:
    """Name every CURRENT pointer that advanced inside the quiet period, or can't be dated."""
    findings: list[str] = []
    for name in sorted(read.pointers):
        advanced = read.advanced_at.get(name)
        if advanced is None:
            findings.append(f"pointer {name} has no measurable last advance")
            continue
        age = (now - advanced).total_seconds()
        if age < quiet_seconds:
            findings.append(
                f"pointer {name} advanced {age:.0f} s ago, inside the {quiet_seconds:g} s quiet period"
            )
    return tuple(findings)


@dataclass(frozen=True, slots=True)
class Observation:
    """The outcome of one signal-driven observation."""

    findings: tuple[str, ...]
    first: PipelineRead
    second: PipelineRead
    elapsed_seconds: float


def observe_until_settled(  # noqa: PLR0913 -- the reads, the bounds and the injected clocks
    *,
    read: Callable[[], PipelineRead],
    baseline: PipelineRead,
    expected: tuple[str, ...],
    quiet_seconds: float,
    max_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Observation:
    """Poll every 60 s; settled at the first consecutive pair that meets every condition.

    No condition is weakened: the two reads are 60 s apart, both must show empty queues and
    baseline dead letters, the pointers must not change between them, and the second read's
    pointers must all be older than the quiet period. Past `max_seconds` the last findings stand.
    """
    started = monotonic()
    first = read()
    while True:
        sleep(_READ_GAP_SECONDS)
        second = read()
        findings = settled_findings(
            PipelineRead({}, baseline.dead_letters, first.pointers), first, second, expected
        ) + quiet_findings(second, now(), quiet_seconds)
        elapsed = monotonic() - started
        if not findings or elapsed >= max_seconds:
            return Observation(findings, first, second, elapsed)
        first = second


def settled_report(
    *, environment: str, account: str, checked_at: datetime, findings: tuple[str, ...]
) -> dict[str, object]:
    """Build the `pipeline-settled` v1 document the landing job's seal guard reads."""
    return {
        "report": "pipeline-settled",
        "version": 1,
        "environment": environment,
        "account": account,
        "checked_at": checked_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "settled": not findings,
        "findings": list(findings),
    }


# --- Reads ---------------------------------------------------------------------------------


def _queue_url(sqs: SQSClient, name: str, account: str) -> str:
    return sqs.get_queue_url(QueueName=name, QueueOwnerAWSAccountId=account)["QueueUrl"]


def _depth(sqs: SQSClient, url: str) -> tuple[QueueDepth, str | None]:
    attributes = sqs.get_queue_attributes(
        QueueUrl=url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
            "RedrivePolicy",
        ],
    ).get("Attributes", {})
    redrive = attributes.get("RedrivePolicy")
    dead_letter = (
        None
        if redrive is None
        else str(json.loads(redrive)["deadLetterTargetArn"]).rsplit(":", 1)[-1]
    )
    return (
        QueueDepth(
            int(attributes.get("ApproximateNumberOfMessages", "0")),
            int(attributes.get("ApproximateNumberOfMessagesNotVisible", "0")),
            int(attributes.get("ApproximateNumberOfMessagesDelayed", "0")),
        ),
        dead_letter,
    )


def provisioned_queues(ingestion_queue_ids: frozenset[str]) -> tuple[str, ...]:
    """The pipeline queues this environment provisions, from the Ingestion stack's queue URLs.

    The registration queue exists only when producer onboarding is enabled
    (`IngestionStack`, `config.landing.enabled`), and dev has it off, so on 2026-10-06 the probe
    stopped on `NonExistentQueue`. It is omitted only when the stack provably owns no such queue;
    the stack must own the ingestion queue, the positive control that the read is the right stack.
    """
    names = {queue_id.rsplit("/", 1)[-1] for queue_id in ingestion_queue_ids}
    if _INGESTION_QUEUE not in names:
        raise ProbeError("the named ingestion stack does not own the ingestion queue")
    return tuple(name for name in QUEUES if name != _REGISTRATION_QUEUE or name in names)


def _ingestion_queue_ids(cloudformation: CloudFormationClient) -> frozenset[str]:
    resources = stack_resource_summaries(cloudformation, _INGESTION_STACK)
    return frozenset(
        str(item.get("PhysicalResourceId", ""))
        for item in resources
        if item.get("ResourceType") == "AWS::SQS::Queue"
    )


def _owned_pointer_table(cloudformation: CloudFormationClient) -> str:
    resources = stack_resource_summaries(cloudformation, _POINTER_STACK)
    if not any(
        item.get("ResourceType") == "AWS::DynamoDB::Table"
        and item.get("PhysicalResourceId") == _POINTER_TABLE
        for item in resources
    ):
        raise ProbeError("the named artifact-store stack does not own the pointer table")
    return _POINTER_TABLE


def _pointers(
    dynamodb: DynamoDBClient, table: str
) -> tuple[dict[str, str], dict[str, datetime | None]]:
    """The CURRENT revisions and each variant's last advance, from the table's own stamps."""
    found: dict[str, str] = {}
    supersedes: dict[str, str] = {}
    paginator = dynamodb.get_paginator("scan")
    for page in paginator.paginate(
        TableName=table,
        FilterExpression="record_key = :current",
        ExpressionAttributeValues={":current": {"S": _CURRENT_RECORD}},
        ProjectionExpression="page_variant_key, revision, supersedes",
    ):
        for item in page.get("Items", []):
            key = item.get("page_variant_key", {}).get("S")
            revision = item.get("revision", {}).get("S")
            if key is None or revision is None:
                raise ProbeError("a CURRENT pointer item is malformed")
            found[key] = revision
            prior = item.get("supersedes", {}).get("S")
            if prior is not None:
                supersedes[key] = prior
    advanced: dict[str, datetime | None] = {}
    for key in found:
        prior = supersedes.get(key)
        history = (
            None
            if prior is None
            else dynamodb.get_item(
                TableName=table,
                Key={"page_variant_key": {"S": key}, "record_key": {"S": f"REVISION#{prior}"}},
                ConsistentRead=True,
            ).get("Item")
        )
        stamp = None if history is None else history.get("expires_at", {})
        raw = None if stamp is None else (stamp.get("N") or stamp.get("S"))
        advanced[key] = (
            None
            if raw is None
            else datetime.fromtimestamp(int(raw), UTC) - SUPERSEDED_HISTORY_RETENTION
        )
    return found, advanced


@dataclass(frozen=True, slots=True)
class _Clients:
    sqs: SQSClient
    dynamodb: DynamoDBClient
    cloudwatch: CloudWatchClient
    account: str
    pointer_table: str
    queues: tuple[str, ...] = QUEUES


def _read(clients: _Clients) -> PipelineRead:
    queues: dict[str, QueueDepth] = {}
    dead_letters: dict[str, int] = {}
    for name in clients.queues:
        depth, dead_letter = _depth(clients.sqs, _queue_url(clients.sqs, name, clients.account))
        queues[name] = depth
        if dead_letter is None:
            raise ProbeError(f"{name} has no dead-letter queue")
        dlq, _ = _depth(clients.sqs, _queue_url(clients.sqs, dead_letter, clients.account))
        dead_letters[dead_letter] = dlq.visible + dlq.in_flight + dlq.delayed
    pointers, advanced = _pointers(clients.dynamodb, clients.pointer_table)
    return PipelineRead(queues, dead_letters, pointers, advanced)


def _metric_sum(  # noqa: PLR0917 -- a metric's namespace, name, dimension, window
    cloudwatch: CloudWatchClient,
    namespace: str,
    metric: str,
    dimension: tuple[str, str],
    window: tuple[datetime, datetime],
) -> int:
    response = cloudwatch.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric,
        Dimensions=[{"Name": dimension[0], "Value": dimension[1]}],
        StartTime=window[0],
        EndTime=window[1],
        Period=300,
        Statistics=["Sum"],
    )
    return int(sum(point.get("Sum", 0.0) for point in response.get("Datapoints", [])))


def fan_out(
    clients: _Clients,
    window: tuple[datetime, datetime],
    pointers: tuple[PipelineRead, PipelineRead],
) -> dict[str, object]:
    """Measure the fan-out figures this probe can read from owned CloudWatch metrics.

    Generation spans by tier and Bedrock throttles come from `aws/spans` and are read by the
    operator's Logs Insights query recorded in the phase evidence, not here.
    """
    before, after = pointers
    return {
        "window": [window[0].isoformat(), window[1].isoformat()],
        "bake_intents_enqueued": _metric_sum(
            clients.cloudwatch,
            "AWS/SQS",
            "NumberOfMessagesSent",
            ("QueueName", _BAKE_QUEUE),
            window,
        ),
        "continuation_enqueues": _metric_sum(
            clients.cloudwatch,
            "AWS/SQS",
            "NumberOfMessagesSent",
            ("QueueName", _CONTINUATION_QUEUE),
            window,
        ),
        "precompute_invocations": _metric_sum(
            clients.cloudwatch,
            "AWS/Lambda",
            "Invocations",
            ("FunctionName", _PRECOMPUTE_FUNCTION),
            window,
        ),
        "pointers_advanced": sum(
            before.pointers.get(name) != revision for name, revision in after.pointers.items()
        ),
    }


def _read_document(path: Path) -> PipelineRead:
    try:
        raw = cast("dict[str, object]", json.loads(path.read_text()))
        dead_letters = cast("dict[str, int]", raw["dead_letters"])
        pointers = cast("dict[str, str]", raw["pointers"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProbeError(f"the baseline is unreadable: {type(error).__name__}") from None
    return PipelineRead({}, dict(dead_letters), dict(pointers))


def _write(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser(description="Report whether the dev pipeline has settled.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--environment", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture-baseline", type=Path, help="write the pre-phase baseline here")
    mode.add_argument("--baseline", type=Path, help="the baseline captured before the phase")
    parser.add_argument("--since", type=datetime.fromisoformat, help="fan-out window start")
    parser.add_argument(
        "--observe-minutes",
        type=int,
        default=_DEFAULT_OBSERVE_MINUTES,
        help="the maximum observation; settled is reported as soon as it holds",
    )
    parser.add_argument("--quiet-seconds", type=int, default=_DEFAULT_QUIET_SECONDS)
    parser.add_argument("--settled-report", type=Path)
    return parser


def run(
    argv: list[str] | None = None,
    *,
    session_factory: Callable[..., boto3.Session] = boto3.Session,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Capture a baseline, or observe the window and report settled; return the exit code."""
    args = _parser().parse_args(argv)
    run_seconds = max(0, args.observe_minutes) * 60 + _RUN_MARGIN_SECONDS
    with within(run_seconds, "pipeline-settled run"):
        return _run(args, session_factory=session_factory, sleep=sleep, now=now)


def _run(
    args: argparse.Namespace,
    *,
    session_factory: Callable[..., boto3.Session],
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
) -> int:
    if args.environment != _ENVIRONMENT or args.region != _REGION:
        raise ProbeError("probe accepts only the governed dev target")
    if str(args.profile).casefold() == "default":
        raise ProbeError("an explicit nondefault profile is required")
    account = str(args.expected_account)
    if len(account) != _ACCOUNT_DIGITS or not account.isdigit():
        raise ProbeError("expected account must be 12 digits")
    session = session_factory(profile_name=args.profile, region_name=args.region)
    if str(bounded_client(session, "sts").get_caller_identity().get("Account", "")) != account:
        raise ProbeError("explicit profile account differs from expected account")
    cloudformation = bounded_client(session, "cloudformation")
    clients = _Clients(
        bounded_client(session, "sqs"),
        bounded_client(session, "dynamodb"),
        bounded_client(session, "cloudwatch"),
        account,
        _owned_pointer_table(cloudformation),
        provisioned_queues(_ingestion_queue_ids(cloudformation)),
    )
    if args.capture_baseline is not None:
        read = _read(clients)
        _write(
            args.capture_baseline,
            {
                "captured_at": now().isoformat(),
                "dead_letters": dict(read.dead_letters),
                "pointers": dict(read.pointers),
            },
        )
        print(
            f"Baseline written: {len(read.dead_letters)} dead-letter queues, "
            f"{len(read.pointers)} pointers"
        )
        return 0
    baseline = _read_document(args.baseline)
    start = _read(clients)
    observation = observe_until_settled(
        read=lambda: _read(clients),
        baseline=baseline,
        expected=clients.queues,
        quiet_seconds=float(args.quiet_seconds),
        max_seconds=max(0, args.observe_minutes) * 60.0,
        sleep=sleep,
        now=now,
    )
    first, second, findings = observation.first, observation.second, observation.findings
    checked_at = now()
    report = settled_report(
        environment=args.environment, account=account, checked_at=checked_at, findings=findings
    )
    evidence = {
        **report,
        "queues_not_provisioned": sorted(set(QUEUES) - set(clients.queues)),
        "elapsed_seconds": round(observation.elapsed_seconds),
        "quiet_seconds": args.quiet_seconds,
        "reads": [
            {
                "queues": {name: asdict(depth) for name, depth in read.queues.items()},
                "dead_letters": dict(read.dead_letters),
            }
            for read in (first, second)
        ],
        "fan_out": None
        if args.since is None
        else fan_out(clients, (args.since, checked_at), (start, second)),
    }
    stamp = checked_at.strftime("%Y%m%dT%H%M%SZ")
    _write(_REPORTS / f"pipeline-settled-{stamp}.json", evidence)
    if args.settled_report is not None:
        _write(args.settled_report, report)
    for finding in findings:
        print(f"  {finding}")
    print(
        f"{'SETTLED' if not findings else 'NOT SETTLED'} after {observation.elapsed_seconds:.0f} s"
    )
    return 0 if not findings else 1


def main() -> int:
    """Run the probe and return nonzero without printing a trace."""
    try:
        return run()
    except ProbeDeadlineError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 2
    except ProbeError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 2
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", "unknown"))
        print(f"FAILED: AWS request returned {code}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
