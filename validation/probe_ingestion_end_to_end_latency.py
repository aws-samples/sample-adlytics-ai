# SPDX-License-Identifier: MIT-0
"""Measure one isolated fictional delivery and observe downstream queue quiescence."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, NoReturn

import boto3
from botocore.exceptions import ClientError

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_EVIDENCE_ROOT = (_ROOT / "validation/evidence").resolve()
sys.path.insert(0, str(_ROOT / "src"))

from adapters.jobs.preset_artifact_publisher.transport import (  # noqa: E402
    load_declared_bake_intents,
)
from adapters.outbound.cloudformation_stack import stack_resource_summaries  # noqa: E402
from adapters.outbound.dynamodb.preset_artifacts import (  # noqa: E402
    DynamoPresetArtifactPointerStore,
)
from domain.presentation.preset_artifacts import (  # noqa: E402
    PresetArtifactPage,
    PresetArtifactVariant,
)

if TYPE_CHECKING:
    from types_boto3_cloudformation.client import CloudFormationClient
    from types_boto3_lambda.client import LambdaClient
    from types_boto3_sqs.client import SQSClient
    from types_boto3_ssm.client import SSMClient

_ENVIRONMENT = "dev"
_REGION = "us-east-1"
_INGESTION_STACK = "Adlytics-Ingestion-dev"
_PUBLISHER_STACK = "Adlytics-Publisher-dev"
_STORE_STACK = "Adlytics-PublisherArtifactStore-dev"
_INGESTION_QUEUE = "adlytics-dev-ingestion"
_DLQ = "adlytics-dev-ingestion-dlq"
_POINTER_PARAMETER = "/adlytics/dev/publisher/artifact-pointer-table-name"
_BAKE_QUEUE_PARAMETER = "/adlytics/dev/publisher/bake-queue-url"
_PAGE = PresetArtifactPage("retail-health")
_ACCOUNT_DIGITS = 12


class ProbeError(RuntimeError):
    """A bounded isolated latency diagnostic failure safe to print."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise ProbeError("invalid explicit full-path probe arguments")


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """Every explicit target and bound for one isolated delivery measurement."""

    profile: str
    environment: str
    region: str
    expected_account: str
    ingestion_stack: str
    mapping_uuid: str
    publisher_stack: str
    store_stack: str
    day: date
    glance_views: int
    max_seconds: int
    evidence_dir: pathlib.Path


@dataclass(frozen=True, slots=True)
class QueueState:
    """One exact queue's approximate non-consuming state."""

    visible: int
    in_flight: int
    delayed: int

    @property
    def total(self) -> int:
        """Return all messages not yet acknowledged."""
        return self.visible + self.in_flight + self.delayed


@dataclass(frozen=True, slots=True)
class LatencyResult:
    """Wall-clock brackets for the isolated end-to-end run."""

    probe_to_semantic_current_ms: int
    semantic_current_to_bake_observation_ms: int
    probe_to_bake_observation_ms: int
    ingestion_queue: QueueState
    ingestion_dlq: QueueState
    bake_queue: QueueState
    pointer_token_deltas: dict[str, int]
    all_pointers_advanced: bool


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


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("day must be an ISO date") from error


def _nonnegative(value: str) -> int:
    try:
        selected = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be an integer") from error
    if selected < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return selected


def _positive(value: str) -> int:
    selected = _nonnegative(value)
    if selected == 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return selected


def _evidence_dir(value: str) -> pathlib.Path:
    selected = pathlib.Path(value).resolve()
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
    parser.add_argument("--publisher-stack", required=True, type=_non_blank)
    parser.add_argument("--store-stack", required=True, type=_non_blank)
    parser.add_argument("--day", required=True, type=_day)
    parser.add_argument("--glance-views", required=True, type=_nonnegative)
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
        args.publisher_stack,
        args.store_stack,
        args.day,
        args.glance_views,
        args.max_seconds,
        args.evidence_dir,
    )


def _validate_request(request: ProbeRequest) -> None:
    if request.environment != _ENVIRONMENT or request.region != _REGION:
        raise ProbeError("full-path probe accepts only the governed dev target")
    if (
        request.ingestion_stack != _INGESTION_STACK
        or request.publisher_stack != _PUBLISHER_STACK
        or request.store_stack != _STORE_STACK
    ):
        raise ProbeError("full-path probe requires the exact dev stacks")


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


def _stack_mapping(
    cloudformation: CloudFormationClient,
    lambda_client: LambdaClient,
    request: ProbeRequest,
) -> None:
    for stack_name in (
        request.ingestion_stack,
        request.publisher_stack,
        request.store_stack,
    ):
        # aws-pagination: an exact StackName returns one stack; the count is checked below.
        described = cloudformation.describe_stacks(StackName=stack_name).get("Stacks", [])
        if len(described) != 1 or not str(described[0].get("StackStatus", "")).endswith(
            "_COMPLETE"
        ):
            raise ProbeError("one exact full-path stack is not complete")
    resources = stack_resource_summaries(cloudformation, request.ingestion_stack)
    mappings = {
        str(item.get("PhysicalResourceId", ""))
        for item in resources
        if item.get("ResourceType") == "AWS::Lambda::EventSourceMapping"
    }
    if request.mapping_uuid not in mappings:
        raise ProbeError("mapping UUID is not owned by the exact ingestion stack")
    mapping = lambda_client.get_event_source_mapping(UUID=request.mapping_uuid)
    if mapping.get("State") != "Enabled":
        raise ProbeError("exact ingestion mapping must be enabled before the isolated run")
    if not str(mapping.get("EventSourceArn", "")).endswith(f":{_INGESTION_QUEUE}"):
        raise ProbeError("exact ingestion mapping names another queue")


def _parameter(client: SSMClient, name: str) -> str:
    value = str(client.get_parameter(Name=name)["Parameter"].get("Value", "")).strip()
    if not value:
        raise ProbeError("one exact full-path parameter resolved empty")
    return value


def _variants() -> tuple[PresetArtifactVariant, ...]:
    selected = tuple(
        PresetArtifactVariant(intent.variant)
        for intent in load_declared_bake_intents(_ROOT).intents
        if intent.page == _PAGE.value
    )
    if not selected:
        raise ProbeError("retail-health declares no preset variants")
    return selected


def _pointer_tokens(
    pointers: DynamoPresetArtifactPointerStore,
    variants: tuple[PresetArtifactVariant, ...],
) -> dict[str, int]:
    result: dict[str, int] = {}
    for variant in variants:
        pointer = pointers.current(_PAGE, variant)
        if pointer is None:
            raise ProbeError("one retail-health preset pointer is absent")
        result[variant.value] = pointer.forward_token
    return result


def _run_cutover(request: ProbeRequest) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(_ROOT / "validation/probe_ingestion_cutover.py"),
        "--profile",
        request.profile,
        "--env",
        request.environment,
        "--day",
        request.day.isoformat(),
        "--glance-views",
        str(request.glance_views),
    ]
    environment = dict(os.environ)
    environment["AWS_REGION"] = request.region
    environment["AWS_DEFAULT_REGION"] = request.region
    try:
        # A-SUBPROCESS-001: fixed interpreter and script; see docs/security-dispositions.md.
        # Semgrep matches whole rule IDs: ASH's ruleset carries the full form, other runners the
        # short form, so each rule is named both ways.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-audit, python.django.security.injection.command.subprocess-injection.subprocess-injection, python.django.security.injection.command.subprocess-injection  # noqa: E501
        return subprocess.run(  # noqa: S603 - fixed local probe and validated arguments only
            command,
            cwd=_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=request.max_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ProbeError("semantic cutover probe exceeded the explicit time bound") from error


def _write_json(path: pathlib.Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def _run(request: ProbeRequest) -> int:
    _validate_request(request)
    request.evidence_dir.mkdir(parents=True, exist_ok=False)
    session = boto3.Session(profile_name=request.profile, region_name=request.region)
    identity = session.client("sts").get_caller_identity()
    if str(identity.get("Account", "")) != request.expected_account:
        raise ProbeError("explicit profile account differs from expected account")
    _stack_mapping(
        session.client("cloudformation"),
        session.client("lambda"),
        request,
    )

    sqs = session.client("sqs")
    ingestion_url = sqs.get_queue_url(
        QueueName=_INGESTION_QUEUE,
        QueueOwnerAWSAccountId=request.expected_account,
    )["QueueUrl"]
    dlq_url = sqs.get_queue_url(
        QueueName=_DLQ,
        QueueOwnerAWSAccountId=request.expected_account,
    )["QueueUrl"]
    baseline_ingestion = _queue_state(sqs, ingestion_url)
    baseline_dlq = _queue_state(sqs, dlq_url)
    if baseline_ingestion.total != 0:
        raise ProbeError("isolated full-path measurement requires an empty ingestion queue")

    ssm = session.client("ssm")
    pointer_table = _parameter(ssm, _POINTER_PARAMETER)
    bake_queue_url = _parameter(ssm, _BAKE_QUEUE_PARAMETER)
    baseline_bake = _queue_state(sqs, bake_queue_url)
    if baseline_bake.total != 0:
        raise ProbeError("isolated full-path measurement requires an empty bake queue")
    pointers = DynamoPresetArtifactPointerStore(pointer_table, session.client("dynamodb"))
    variants = _variants()
    baseline_tokens = _pointer_tokens(pointers, variants)

    started_at = datetime.now(UTC)
    started = time.monotonic()
    cutover = _run_cutover(request)
    semantic_completed_at = datetime.now(UTC)
    semantic_completed = time.monotonic()
    (request.evidence_dir / "cutover.stdout.log").write_text(cutover.stdout)
    (request.evidence_dir / "cutover.stderr.log").write_text(cutover.stderr)
    if cutover.returncode != 0:
        raise ProbeError("semantic cutover probe did not reach exact CURRENT success")

    current_tokens = _pointer_tokens(pointers, variants)
    final_bake = _queue_state(sqs, bake_queue_url)
    ready_at = datetime.now(UTC)
    ready = time.monotonic()
    final_ingestion = _queue_state(sqs, ingestion_url)
    final_dlq = _queue_state(sqs, dlq_url)
    if final_ingestion.total != 0:
        raise ProbeError("ingestion queue did not return to zero after the isolated delivery")
    if final_dlq != baseline_dlq:
        raise ProbeError("ingestion DLQ changed during the isolated delivery")

    token_deltas = {
        name: current_tokens[name] - baseline_tokens[name] for name in baseline_tokens
    }
    result = LatencyResult(
        probe_to_semantic_current_ms=round((semantic_completed - started) * 1_000),
        semantic_current_to_bake_observation_ms=round(
            (ready - semantic_completed) * 1_000
        ),
        probe_to_bake_observation_ms=round((ready - started) * 1_000),
        ingestion_queue=final_ingestion,
        ingestion_dlq=final_dlq,
        bake_queue=final_bake,
        pointer_token_deltas=token_deltas,
        all_pointers_advanced=all(delta > 0 for delta in token_deltas.values()),
    )
    _write_json(
        request.evidence_dir / "summary.json",
        {
            "status": "diagnostic_only",
            "evidence_limit": (
                "One post-CURRENT bake queue and pointer snapshot does not prove producer "
                "convergence, DLQ stability, or causally linked terminal outcomes."
            ),
            "started_at": started_at,
            "semantic_completed_at": semantic_completed_at,
            "bake_queue_observed_at": ready_at,
            "baseline_ingestion_queue": asdict(baseline_ingestion),
            "baseline_ingestion_dlq": asdict(baseline_dlq),
            "baseline_bake_queue": asdict(baseline_bake),
            "result": asdict(result),
        },
    )
    print(
        "OBSERVED: isolated delivery "
        f"semantic_current_ms={result.probe_to_semantic_current_ms} "
        f"bake_observation_ms={result.probe_to_bake_observation_ms}",
        flush=True,
    )
    return 0


def main() -> int:
    """Run the exact dev-only full-path latency probe."""
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
