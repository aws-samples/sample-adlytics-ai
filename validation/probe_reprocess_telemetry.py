# SPDX-License-Identifier: MIT-0
"""Run and attest one dev-only Semantic V2 reprocess telemetry workflow."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, NoReturn, cast

import boto3

from adapters.jobs.span_retrieval.client import LogsInsightsSpanClient
from adapters.jobs.span_retrieval.parse import SpanRetrievalFailedError
from adapters.jobs.span_retrieval.query import (
    SpanQueryRefusedError,
    build_span_query,
    declared_span_attributes,
)
from adapters.jobs.trace_contract.contract import (
    SpanDeclaration,
    forbidden_attribute,
    load_trace_contract,
)
from adapters.outbound.cloudformation_stack import stack_resource_summaries

_ROOT: Final = Path(__file__).resolve().parents[1]
_EVIDENCE_ROOT: Final = (_ROOT / "validation/evidence").resolve()
_REPROCESS_MODULE: Final = "adapters.jobs.reprocess_semantic_v2"
_REQUIRED_ENVIRONMENT: Final = "dev"
_REQUIRED_REGION: Final = "us-east-1"
_REQUIRED_DATA_STACK: Final = "Adlytics-DataV2-dev"
_REQUIRED_OBSERVABILITY_STACK: Final = "Adlytics-Observability-dev"
_REQUIRED_GRAPH_STORE_STACK: Final = "Adlytics-ControlPlaneAuthorityStore-dev"
"""S6 (D22): the apply reads the authority mode through this exact dev store stack."""
_HEARTBEAT_INTERVAL_SECONDS: Final = 30.0
_QUERY_TIMEOUT_SECONDS: Final = 180.0
_QUERY_POLL_SECONDS: Final = 2.0
_QUERY_LIMIT: Final = 1_000
_COMPONENT: Final = "reprocess_semantic_v2.operator"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DIGEST_LINE = re.compile(r"^\s*digest:\s*(sha256:[0-9a-f]{64})\s*$", re.MULTILINE)
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_EXECUTION_ID = re.compile(r"^[0-9a-f]{32}$")
_SECRET_VALUE = re.compile(
    r"(?i)(?:s3://|arn:aws|authorization\s*[:=]|x-amz-security-token|"
    r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|"
    r"\b(?:select|insert|update|delete|merge|create|drop)\b.{0,80}\b(?:from|into|table)\b)"
)
_EXPORTER_ATTRIBUTE_NAMES: Final = frozenset(
    {
        "PlatformType",
        "aws.local.environment",
        "aws.local.operation",
        "aws.local.service",
        "aws.span.kind",
        "telemetry.extended",
    }
)
_PROHIBITED_KEY_PARTS: Final = frozenset(
    {
        "answer",
        "argument",
        "arguments",
        "authorization",
        "baggage",
        "body",
        "cookie",
        "credential",
        "credentials",
        "manifest",
        "object",
        "parameter",
        "parameters",
        "password",
        "path",
        "payload",
        "prompt",
        "question",
        "row",
        "rows",
        "secret",
        "sql",
        "statement_text",
        "token",
        "tokens",
        "traceparent",
        "tracestate",
    }
)
_LIFECYCLE_ALLOWED_KEYS: Final = frozenset(
    {
        "cold_start",
        "completed",
        "component",
        "contract_ids",
        "execution_id",
        "function_arn",
        "function_memory_size",
        "function_name",
        "function_request_id",
        "level",
        "location",
        "message",
        "operation",
        "operation_id",
        "outcome",
        "phase",
        "plan_digest",
        "predecessor_execution_id",
        "service",
        "span_id",
        "stage",
        "timestamp",
        "total",
        "trace_id",
        "tracing",
        "xray_trace_id",
    }
)
_REQUIRED_SPAN_NAMES: Final = frozenset(
    {
        "reprocess.semantic_v2",
        "reprocess.semantic_v2.plan",
        "reprocess.semantic_v2.apply",
        "reprocess.semantic_v2.delivery",
        "ingest.delivery",
        "ingest.partition",
        "athena.statement",
        "reprocess.semantic_v2.acceptance",
    }
)
_SINGLE_SPAN_NAMES: Final = frozenset(
    {
        "reprocess.semantic_v2",
        "reprocess.semantic_v2.plan",
        "reprocess.semantic_v2.apply",
        "reprocess.semantic_v2.acceptance",
    }
)


class _StageError(Exception):
    """A bounded acceptance classification that cannot expose raw evidence."""

    def __init__(self, stage: str, reason: str, *, pending: bool = False) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.pending = pending


class _ArgumentParser(argparse.ArgumentParser):
    """Keep invalid operator input in the same bounded summary format."""

    def error(self, message: str) -> NoReturn:
        del message
        raise _StageError("input_validation", "invalid_arguments")


@dataclass(frozen=True, slots=True)
class _DryPlan:
    digest: str
    delivery_count: int


@dataclass(frozen=True, slots=True)
class _LifecycleAcceptance:
    operation_id: str
    execution_id: str
    predecessor_execution_id: str | None
    plan_digest: str
    trace_id: str
    root_span_id: str
    completed: int
    total: int
    progress_records: int
    heartbeat_records: int


@dataclass(frozen=True, slots=True)
class _SpanAcceptance:
    span_count: int
    required_name_count: int
    athena_statement_count: int


@dataclass(frozen=True, slots=True)
class _ProbeRequest:
    profile: str
    environment: str
    region: str
    expected_account: str
    data_stack: str
    observability_stack: str
    bucket: str
    window_start: date
    window_end: date
    operation_id: str
    contracts: tuple[str, ...]
    retrieved_token: str
    predecessor_execution_id: str | None
    evidence_dir: Path


def _non_blank(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be blank")
    return value.strip()


def _profile(value: str) -> str:
    selected = _non_blank(value)
    if selected.casefold() == "default":
        raise argparse.ArgumentTypeError("an explicit nondefault profile is required")
    return selected


def _account(value: str) -> str:
    selected = _non_blank(value)
    if _ACCOUNT_ID.fullmatch(selected) is None:
        raise argparse.ArgumentTypeError("expected account must have twelve digits")
    return selected


def _retrieved_token(value: str) -> str:
    selected = _non_blank(value)
    if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", selected) is None:
        raise argparse.ArgumentTypeError("retrieved token must use YYYYMMDDTHHMMSSZ")
    return selected


def build_parser() -> argparse.ArgumentParser:
    """Build the explicit dev-only operator interface."""
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=_profile)
    parser.add_argument("--environment", required=True, type=_non_blank)
    parser.add_argument("--region", required=True, type=_non_blank)
    parser.add_argument("--expected-account", required=True, type=_account)
    parser.add_argument("--data-stack", required=True, type=_non_blank)
    parser.add_argument("--observability-stack", required=True, type=_non_blank)
    parser.add_argument("--bucket", required=True, type=_non_blank)
    parser.add_argument("--window-start", required=True, type=date.fromisoformat)
    parser.add_argument("--window-end", required=True, type=date.fromisoformat)
    parser.add_argument("--operation-id", required=True, type=_non_blank)
    parser.add_argument("--contract", required=True, action="append", dest="contracts")
    parser.add_argument("--retrieved-token", required=True, type=_retrieved_token)
    parser.add_argument("--predecessor-execution-id", type=_non_blank)
    parser.add_argument("--evidence-dir", type=Path)
    return parser


def validate_target(environment: str, region: str) -> None:
    """Refuse every target except the one approved dev region before AWS construction."""
    if environment != _REQUIRED_ENVIRONMENT or region != _REQUIRED_REGION:
        raise _StageError("target_validation", "dev_target_required")


def _evidence_directory(selected: Path | None) -> Path:
    _EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    if selected is None:
        return Path(tempfile.mkdtemp(prefix="reprocess-telemetry.", dir=_EVIDENCE_ROOT))
    resolved = selected.resolve()
    if resolved == _EVIDENCE_ROOT or _EVIDENCE_ROOT not in resolved.parents:
        raise _StageError("input_validation", "evidence_directory_not_ignored")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _write_text(evidence_dir: Path, name: str, value: str) -> None:
    (evidence_dir / name).write_text(value, encoding="utf-8")


def _write_json(evidence_dir: Path, name: str, value: object) -> None:
    (evidence_dir / name).write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _stack(
    cloudformation: Any,
    name: str,
    *,
    evidence_dir: Path,
    evidence_name: str,
) -> tuple[Mapping[str, object], tuple[Mapping[str, object], ...]]:
    # aws-pagination: an exact StackName returns one stack; the count is checked below.
    described = cast("dict[str, object]", cloudformation.describe_stacks(StackName=name))
    raw_stacks_value = described.get("Stacks")
    if not isinstance(raw_stacks_value, list):
        raise _StageError("deployment_attestation", "exact_stack_missing")
    raw_stacks = cast("list[object]", raw_stacks_value)
    if len(raw_stacks) != 1 or not isinstance(raw_stacks[0], Mapping):
        raise _StageError("deployment_attestation", "exact_stack_missing")
    stack = cast("Mapping[str, object]", raw_stacks[0])
    status = stack.get("StackStatus")
    if stack.get("StackName") != name or not isinstance(status, str) or not status.endswith(
        "_COMPLETE"
    ):
        raise _StageError("deployment_attestation", "exact_stack_inactive")
    raw_resources: list[object] = list(stack_resource_summaries(cloudformation, name))
    if not raw_resources or any(not isinstance(item, Mapping) for item in raw_resources):
        raise _StageError("deployment_attestation", "stack_resources_missing")
    resources = tuple(cast("Mapping[str, object]", item) for item in raw_resources)
    _write_json(
        evidence_dir,
        evidence_name,
        {"stack": described, "resources": {"StackResourceSummaries": raw_resources}},
    )
    return stack, resources


def _active_resource(
    resources: Sequence[Mapping[str, object]],
    resource_type: str,
    *,
    physical_id: str | None = None,
) -> None:
    matches = [
        resource
        for resource in resources
        if resource.get("ResourceType") == resource_type
        and (physical_id is None or resource.get("PhysicalResourceId") == physical_id)
    ]
    if len(matches) != 1:
        raise _StageError("deployment_attestation", "exact_stack_resource_missing")
    status = matches[0].get("ResourceStatus")
    if not isinstance(status, str) or not status.endswith("_COMPLETE"):
        raise _StageError("deployment_attestation", "stack_resource_inactive")


def _attest_deployment(session: boto3.Session, request: _ProbeRequest) -> None:
    identity = cast("dict[str, object]", session.client("sts").get_caller_identity())
    _write_json(request.evidence_dir, "identity.json", identity)
    if identity.get("Account") != request.expected_account:
        raise _StageError("identity_attestation", "account_mismatch")

    cloudformation = session.client("cloudformation")
    _, data_resources = _stack(
        cloudformation,
        request.data_stack,
        evidence_dir=request.evidence_dir,
        evidence_name="data-stack.json",
    )
    _active_resource(data_resources, "AWS::S3::Bucket", physical_id=request.bucket)
    _, observability_resources = _stack(
        cloudformation,
        request.observability_stack,
        evidence_dir=request.evidence_dir,
        evidence_name="observability-stack.json",
    )
    _active_resource(observability_resources, "AWS::XRay::TransactionSearchConfig")
    _active_resource(observability_resources, "AWS::Logs::ResourcePolicy")

    destination = cast(
        "dict[str, object]", session.client("xray").get_trace_segment_destination()
    )
    _write_json(request.evidence_dir, "trace-segment-destination.json", destination)
    if destination.get("Destination") != "CloudWatchLogs" or destination.get("Status") != "ACTIVE":
        raise _StageError("deployment_attestation", "span_destination_inactive", pending=True)


def _base_command(request: _ProbeRequest) -> list[str]:
    command = [
        sys.executable,
        "-m",
        _REPROCESS_MODULE,
        "--profile",
        request.profile,
        "--region",
        request.region,
        "--expected-account",
        request.expected_account,
        "--env",
        request.environment,
        "--stack",
        request.data_stack,
        "--bucket",
        request.bucket,
        "--window-start",
        request.window_start.isoformat(),
        "--window-end",
        request.window_end.isoformat(),
        "--operation-id",
        request.operation_id,
        "--retrieved-token",
        request.retrieved_token,
    ]
    for contract in request.contracts:
        command.extend(("--contract", contract))
    return command


def _run_reprocess(
    command: Sequence[str], evidence_dir: Path, prefix: str
) -> subprocess.CompletedProcess[str]:
    # A-SUBPROCESS-001: fixed interpreter and module; see docs/security-dispositions.md
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    result = subprocess.run(  # noqa: S603
        list(command),
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    _write_text(evidence_dir, f"{prefix}-stdout.log", result.stdout)
    _write_text(evidence_dir, f"{prefix}-stderr.log", result.stderr)
    return result


def parse_dry_plan(result: subprocess.CompletedProcess[str]) -> _DryPlan:
    """Parse one non-empty dry plan without returning its raw object paths."""
    if result.returncode != 0:
        raise _StageError("dry_plan", "subprocess_failed")
    digests = _DIGEST_LINE.findall(result.stdout)
    if len(digests) != 1:
        raise _StageError("dry_plan", "plan_digest_cardinality")
    manifest_lines = sum(line.startswith("  manifest ") for line in result.stdout.splitlines())
    object_lines = sum(line.startswith("  object   ") for line in result.stdout.splitlines())
    if manifest_lines == 0 or object_lines != manifest_lines:
        raise _StageError("dry_plan", "plan_empty_or_ambiguous")
    return _DryPlan(digest=digests[0], delivery_count=manifest_lines)


def _key_parts(key: str) -> frozenset[str]:
    return frozenset(part for part in re.split(r"[^a-z0-9]+", key.casefold()) if part)


def _prohibited_key(key: str) -> bool:
    return forbidden_attribute(key.replace(".", "_")) or bool(
        _key_parts(key) & _PROHIBITED_KEY_PARTS
    )


def _string_values(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        for nested in mapping.values():
            yield from _string_values(nested)
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        sequence = cast("Sequence[object]", value)
        for nested in sequence:
            yield from _string_values(nested)


def _safe_content(values: Mapping[str, object], *, stage: str) -> None:
    if any(_prohibited_key(str(key)) for key in values):
        raise _StageError(stage, "prohibited_field_present")
    if any(_SECRET_VALUE.search(value) for value in _string_values(values)):
        raise _StageError(stage, "prohibited_content_present")


def _json_lifecycle_lines(output: str, operation_id: str) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    for line in output.splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        event = cast("dict[str, object]", raw)
        if event.get("message") != "operation lifecycle":
            continue
        if event.get("operation") != "reprocess.semantic_v2":
            continue
        if event.get("operation_id") != operation_id:
            continue
        unknown = set(event) - _LIFECYCLE_ALLOWED_KEYS
        if unknown:
            raise _StageError("lifecycle", "unbounded_field_present")
        _safe_content(event, stage="lifecycle")
        records.append(event)
    return tuple(records)


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise _StageError("lifecycle", "timestamp_missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _StageError("lifecycle", "timestamp_invalid") from error
    if parsed.tzinfo is None:
        raise _StageError("lifecycle", "timestamp_invalid")
    return parsed.astimezone(UTC)


def parse_lifecycle(
    result: subprocess.CompletedProcess[str],
    *,
    operation_id: str,
    expected_digest: str,
    expected_predecessor: str | None,
) -> _LifecycleAcceptance:
    """Attest closed, monotonic lifecycle evidence for one successful traced apply."""
    records = _json_lifecycle_lines(result.stdout + "\n" + result.stderr, operation_id)
    starts = [record for record in records if record.get("phase") == "start"]
    terminals = [record for record in records if record.get("phase") == "terminal"]
    if len(starts) != 1:
        raise _StageError("lifecycle", "start_cardinality")
    if len(terminals) != 1:
        raise _StageError("lifecycle", "terminal_cardinality")
    start = starts[0]
    terminal = terminals[0]
    if (
        start.get("component") != _COMPONENT
        or start.get("outcome") != "started"
        or start.get("stage") != "start"
        or start.get("completed") != 0
        or start.get("total") != 0
        or start.get("tracing") is not True
    ):
        raise _StageError("lifecycle", "start_contract_mismatch")
    if result.returncode != 0:
        raise _StageError("lifecycle", "apply_subprocess_failed")
    completed = _integer(terminal.get("completed"))
    total = _integer(terminal.get("total"))
    execution_id = terminal.get("execution_id")
    trace_id = terminal.get("trace_id")
    span_id = terminal.get("span_id")
    if (
        terminal.get("component") != _COMPONENT
        or terminal.get("outcome") != "completed"
        or terminal.get("stage") != "acceptance"
        or completed is None
        or total is None
        or total <= 0
        or completed != total
        or not isinstance(execution_id, str)
        or _EXECUTION_ID.fullmatch(execution_id) is None
        or terminal.get("plan_digest") != expected_digest
        or not isinstance(trace_id, str)
        or _TRACE_ID.fullmatch(trace_id) is None
        or not isinstance(span_id, str)
        or _SPAN_ID.fullmatch(span_id) is None
    ):
        raise _StageError("lifecycle", "terminal_contract_mismatch")
    if terminal.get("predecessor_execution_id") != expected_predecessor:
        raise _StageError("lifecycle", "predecessor_mismatch")
    if start.get("execution_id") != execution_id:
        raise _StageError("lifecycle", "execution_mismatch")
    if start.get("trace_id") != trace_id or start.get("span_id") != span_id:
        raise _StageError("lifecycle", "trace_context_mismatch")

    progress = [
        record for record in records if record.get("phase") in {"progress", "heartbeat"}
    ]
    previous_completed = 0
    for record in progress:
        current = _integer(record.get("completed"))
        current_total = _integer(record.get("total"))
        if (
            record.get("execution_id") != execution_id
            or record.get("plan_digest") != expected_digest
            or record.get("predecessor_execution_id") != expected_predecessor
            or current is None
            or current_total != total
            or current < previous_completed
            or current > total
        ):
            raise _StageError("lifecycle", "progress_not_monotonic")
        previous_completed = current
    progress_records = sum(record.get("phase") == "progress" for record in progress)
    heartbeat_records = sum(record.get("phase") == "heartbeat" for record in progress)
    if progress_records == 0 or previous_completed != total:
        raise _StageError("lifecycle", "progress_incomplete")
    elapsed = (
        _timestamp(terminal.get("timestamp")) - _timestamp(start.get("timestamp"))
    ).total_seconds()
    if elapsed >= _HEARTBEAT_INTERVAL_SECONDS and heartbeat_records == 0:
        raise _StageError("lifecycle", "heartbeat_missing")
    return _LifecycleAcceptance(
        operation_id=operation_id,
        execution_id=execution_id,
        predecessor_execution_id=expected_predecessor,
        plan_digest=expected_digest,
        trace_id=trace_id,
        root_span_id=span_id,
        completed=completed,
        total=total,
        progress_records=progress_records,
        heartbeat_records=heartbeat_records,
    )


def _query_once(
    logs: Any,
    *,
    environment: str,
    trace_id: str,
    start_time: int,
    end_time: int,
    names: frozenset[str],
    limit: int,
) -> dict[str, object]:
    """Read one page of the trace's named spans through the 5a span-retrieval module."""
    try:
        query = build_span_query(
            environment=environment,
            window_start=datetime.fromtimestamp(start_time, UTC),
            window_end=datetime.fromtimestamp(end_time, UTC),
            declared_attributes=declared_span_attributes(_ROOT),
            trace_id=trace_id,
            span_names=sorted(names),
            limit=limit,
            max_pages=1,
        )
        result = LogsInsightsSpanClient(
            logs, poll_seconds=_QUERY_POLL_SECONDS, timeout_seconds=_QUERY_TIMEOUT_SECONDS
        ).retrieve(query)
    except SpanQueryRefusedError as error:
        raise _StageError("trace_query", "query_start_failed") from error
    except SpanRetrievalFailedError as error:
        if error.reason.startswith("span_"):
            raise _StageError("trace_contract", "span_document_malformed") from error
        raise _StageError("trace_query", "query_failed") from error
    if result["reason"] == "query_running":
        raise _StageError("trace_query", "query_pending", pending=True)
    return result


def _span_documents(
    result: Mapping[str, object], trace_id: str
) -> tuple[dict[str, object], ...]:
    documents = tuple(cast("list[dict[str, object]]", result["spans"]))
    for document in documents:
        if document.get("traceId") != trace_id:
            raise _StageError("trace_contract", "span_trace_mismatch")
    return documents


def _attributes(document: Mapping[str, object]) -> dict[str, object]:
    raw = document.get("attributes")
    if not isinstance(raw, dict):
        raise _StageError("trace_contract", "span_attributes_missing")
    return cast("dict[str, object]", raw)


def validate_span_hierarchy(
    documents: Sequence[Mapping[str, object]],
    lifecycle: _LifecycleAcceptance,
) -> _SpanAcceptance:
    """Validate required names, declared fixed parents, and closed span attributes."""
    if not documents:
        raise _StageError("trace_contract", "trace_spans_pending", pending=True)
    if len(documents) >= _QUERY_LIMIT:
        raise _StageError("trace_contract", "trace_span_bound_exceeded", pending=True)
    if {document.get("traceId") for document in documents} != {lifecycle.trace_id}:
        raise _StageError("trace_contract", "span_trace_mismatch")

    contract = load_trace_contract(_ROOT)
    declarations_by_name = {declaration.name: declaration for declaration in contract.spans}
    declarations_by_constant = {
        declaration.constant: declaration for declaration in contract.spans
    }
    selected = {name: declarations_by_name[name] for name in _REQUIRED_SPAN_NAMES}
    ids = {
        cast("str", document["spanId"]): document
        for document in documents
        if isinstance(document.get("spanId"), str)
    }
    if len(ids) != len(documents):
        raise _StageError("trace_contract", "span_identity_ambiguous")
    counts = Counter(cast("str", document.get("name")) for document in documents)
    if any(counts[name] == 0 for name in _REQUIRED_SPAN_NAMES):
        raise _StageError("trace_contract", "required_span_missing", pending=True)
    if any(counts[name] != 1 for name in _SINGLE_SPAN_NAMES):
        raise _StageError("trace_contract", "singleton_span_cardinality")
    if counts["reprocess.semantic_v2.delivery"] * 2 != lifecycle.total:
        raise _StageError("trace_contract", "delivery_span_cardinality")

    for document in documents:
        attributes = _attributes(document)
        _safe_content(attributes, stage="trace_contract")
        name = document.get("name")
        declaration = selected.get(cast("str", name))
        if declaration is None:
            continue
        _validate_declared_span(
            document=document,
            attributes=attributes,
            declaration=declaration,
            ids=ids,
            declarations=declarations_by_constant,
            lifecycle=lifecycle,
        )

    root = next(
        document
        for document in documents
        if document.get("name") == "reprocess.semantic_v2"
    )
    if root.get("spanId") != lifecycle.root_span_id or root.get("parentSpanId") not in {None, ""}:
        raise _StageError("trace_contract", "root_span_mismatch")
    acceptance = next(
        document
        for document in documents
        if document.get("name") == "reprocess.semantic_v2.acceptance"
    )
    if _attributes(acceptance).get("accepted") is not True:
        raise _StageError("trace_contract", "acceptance_span_not_accepted")
    return _SpanAcceptance(
        span_count=len(documents),
        required_name_count=len(_REQUIRED_SPAN_NAMES),
        athena_statement_count=counts["athena.statement"],
    )


def _attribute_present(value: object) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return False
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        return bool(value)
    return True


def _validate_declared_span(
    *,
    document: Mapping[str, object],
    attributes: Mapping[str, object],
    declaration: SpanDeclaration,
    ids: Mapping[str, Mapping[str, object]],
    declarations: Mapping[str, SpanDeclaration],
    lifecycle: _LifecycleAcceptance,
) -> None:
    keys = frozenset(attributes) - _EXPORTER_ATTRIBUTE_NAMES
    authored_attributes = {key: value for key, value in attributes.items() if key in keys}
    if not set(declaration.required_attributes) <= keys or any(
        not _attribute_present(authored_attributes[key]) for key in declaration.required_attributes
    ):
        raise _StageError("trace_contract", "required_attribute_missing")
    if not keys <= set(declaration.allowed_attributes):
        raise _StageError("trace_contract", "undeclared_attribute_present")
    expected_values: dict[str, object] = {
        "execution_id": lifecycle.execution_id,
        "predecessor_execution_id": lifecycle.predecessor_execution_id or "none",
        "plan_digest": lifecycle.plan_digest,
    }
    if (
        declaration.name.startswith("reprocess.semantic_v2")
        and attributes.get("operation_id") != lifecycle.operation_id
    ):
        raise _StageError("trace_contract", "operation_attribute_mismatch")
    for key, expected in expected_values.items():
        if key in attributes and attributes.get(key) != expected:
            raise _StageError("trace_contract", "lineage_attribute_mismatch")
    completed = _integer(attributes.get("completed"))
    total = _integer(attributes.get("total"))
    if "completed" in attributes and (
        completed is None or total is None or completed < 0 or completed > total
    ):
        raise _StageError("trace_contract", "span_progress_invalid")
    if declaration.name == "reprocess.semantic_v2" and (
        attributes.get("stage") != "acceptance"
        or completed != lifecycle.completed
        or total != lifecycle.total
    ):
        raise _StageError("trace_contract", "root_progress_mismatch")
    if declaration.name == "athena.statement":
        athena_required = {
            "operation_kind",
            "logical_table",
            "query_id",
            "queue_millis",
            "planning_millis",
            "engine_millis",
            "total_millis",
        }
        if not athena_required <= keys or any(
            not _attribute_present(attributes[key]) for key in athena_required
        ):
            raise _StageError("trace_contract", "athena_timing_attribute_missing")
        if not all(
            isinstance(attributes[key], int | float)
            and not isinstance(attributes[key], bool)
            and cast("int | float", attributes[key]) >= 0
            for key in ("queue_millis", "planning_millis", "engine_millis", "total_millis")
        ):
            raise _StageError("trace_contract", "athena_timing_attribute_invalid")
    if declaration.parent in {"root", "current"}:
        return
    parent_id = document.get("parentSpanId")
    parent = ids.get(cast("str", parent_id))
    expected_parent = declarations[declaration.parent].name
    if parent is None or parent.get("name") != expected_parent:
        raise _StageError("trace_contract", "fixed_parent_mismatch")


def _poll_trace(
    session: boto3.Session,
    request: _ProbeRequest,
    lifecycle: _LifecycleAcceptance,
    *,
    started_at: int,
) -> _SpanAcceptance:
    logs = session.client("logs")
    deadline = time.monotonic() + _QUERY_TIMEOUT_SECONDS
    last_documents: tuple[dict[str, object], ...] = ()
    while time.monotonic() < deadline:
        business_result = _query_once(
            logs,
            environment=request.environment,
            trace_id=lifecycle.trace_id,
            start_time=started_at,
            end_time=int(time.time()) + 1,
            names=_REQUIRED_SPAN_NAMES - {"athena.statement"},
            limit=_QUERY_LIMIT,
        )
        athena_result = _query_once(
            logs,
            environment=request.environment,
            trace_id=lifecycle.trace_id,
            start_time=started_at,
            end_time=int(time.time()) + 1,
            names=frozenset({"athena.statement"}),
            limit=1,
        )
        _write_json(request.evidence_dir, "trace-business-query.json", business_result)
        _write_json(request.evidence_dir, "trace-athena-query.json", athena_result)
        if business_result["truncated"]:
            raise _StageError("trace_contract", "trace_span_bound_exceeded", pending=True)
        last_documents = (
            *_span_documents(business_result, lifecycle.trace_id),
            *_span_documents(athena_result, lifecycle.trace_id),
        )
        try:
            return validate_span_hierarchy(last_documents, lifecycle)
        except _StageError as error:
            if not error.pending:
                raise
        time.sleep(_QUERY_POLL_SECONDS)
    if last_documents:
        raise _StageError("trace_contract", "trace_incomplete", pending=True)
    raise _StageError("trace_contract", "trace_spans_pending", pending=True)


def _request(args: argparse.Namespace, evidence_dir: Path) -> _ProbeRequest:
    contracts = tuple(cast("list[str]", args.contracts))
    if not contracts or any(not contract.strip() for contract in contracts):
        raise _StageError("input_validation", "contract_scope_invalid")
    if len(set(contracts)) != len(contracts):
        raise _StageError("input_validation", "contract_scope_ambiguous")
    predecessor = cast("str | None", args.predecessor_execution_id)
    if predecessor is not None and _EXECUTION_ID.fullmatch(predecessor) is None:
        raise _StageError("input_validation", "predecessor_invalid")
    window_start = cast("date", args.window_start)
    window_end = cast("date", args.window_end)
    if window_end < window_start:
        raise _StageError("input_validation", "window_invalid")
    return _ProbeRequest(
        profile=cast("str", args.profile),
        environment=cast("str", args.environment),
        region=cast("str", args.region),
        expected_account=cast("str", args.expected_account),
        data_stack=cast("str", args.data_stack),
        observability_stack=cast("str", args.observability_stack),
        bucket=cast("str", args.bucket),
        window_start=window_start,
        window_end=window_end,
        operation_id=cast("str", args.operation_id),
        contracts=contracts,
        retrieved_token=cast("str", args.retrieved_token),
        predecessor_execution_id=predecessor,
        evidence_dir=evidence_dir,
    )


def _validate_named_targets(args: argparse.Namespace) -> None:
    validate_target(cast("str", args.environment), cast("str", args.region))
    if (
        args.data_stack != _REQUIRED_DATA_STACK
        or args.observability_stack != _REQUIRED_OBSERVABILITY_STACK
    ):
        raise _StageError("target_validation", "exact_dev_stacks_required")


def _summary(status: str, stage: str, outcome: str, **counts: object) -> None:
    print(
        json.dumps(
            {
                "status": status,
                "stage": stage,
                "outcome": outcome,
                "trace_id_present": status == "accepted",
                **counts,
            },
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    """Run the guarded probe and print only a closed sanitized outcome."""
    evidence_dir: Path | None = None
    try:
        args = build_parser().parse_args(argv)
        _validate_named_targets(args)
        evidence_dir = _evidence_directory(cast("Path | None", args.evidence_dir))
        request = _request(args, evidence_dir)
        session = boto3.Session(profile_name=request.profile, region_name=request.region)
        _attest_deployment(session, request)

        dry_result = _run_reprocess(_base_command(request), evidence_dir, "dry-plan")
        dry_plan = parse_dry_plan(dry_result)
        apply_command = [
            *_base_command(request),
            "--trace",
            "--apply",
            "--expected-plan-digest",
            dry_plan.digest,
            "--graph-store-stack",
            _REQUIRED_GRAPH_STORE_STACK,
        ]
        if request.predecessor_execution_id is not None:
            apply_command.extend(
                ("--predecessor-execution-id", request.predecessor_execution_id)
            )
        apply_started = int(time.time()) - 60
        apply_result = _run_reprocess(apply_command, evidence_dir, "apply")
        lifecycle = parse_lifecycle(
            apply_result,
            operation_id=request.operation_id,
            expected_digest=dry_plan.digest,
            expected_predecessor=request.predecessor_execution_id,
        )
        spans = _poll_trace(session, request, lifecycle, started_at=apply_started)
        _summary(
            "accepted",
            "complete",
            "attested",
            delivery_count=dry_plan.delivery_count,
            lifecycle_progress_count=lifecycle.progress_records,
            lifecycle_heartbeat_count=lifecycle.heartbeat_records,
            span_count=spans.span_count,
            required_span_name_count=spans.required_name_count,
            athena_statement_count=spans.athena_statement_count,
        )
        return 0
    except _StageError as error:
        _summary(
            "pending" if error.pending else "failed",
            error.stage,
            error.reason,
        )
        return 2 if error.stage in {"input_validation", "target_validation"} else 1
    except Exception:
        _summary("failed", "probe", "unexpected_error")
        return 1
    finally:
        if evidence_dir is not None:
            print("local ignored evidence retained", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
