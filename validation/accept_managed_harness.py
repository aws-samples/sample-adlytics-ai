# SPDX-License-Identifier: MIT-0
"""Join the non-production managed-Harness cutover acceptance evidence.

The runner accepts only the literal dev target and exact caller-supplied stack names. It
attests the selected account before any other AWS call, never enumerates account resources, retains
protected raw material only beneath validation/evidence, and prints one bounded JSON summary. Each
turn's receipt and evaluation chain are read only through `POST /api/quality-review/turn` with a
reviewer persona's bearer (ADR-078 §5); the outbox is still read directly. Tool coverage runs
through `/api/chat` as a synthetic persona, because the Harness admits only a token the route
exchange mints for the route (ADR-077 §2).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fnmatch
import hashlib
import io
import json
import os
import re
import secrets
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, cast

import boto3
from cognito_personas import app_client, load_persona, sign_in
from probe_agent_quality_loop import (
    enabled_evaluation_releases,
    evaluation_dispatch_mismatch,
    semantic_evaluation_result_state,
)
from probe_conversation_route import _refreshed_session  # pyright: ignore[reportPrivateUsage]
from probe_identity_chain import LogWindow, deployed_function_name, terminal_records
from quality_review_client import (
    DEFAULT_REVIEWER_PERSONA,
    ReviewClient,
    ReviewedDelivery,
    ReviewedReceipt,
    ReviewUnavailableError,
    reviewed_evaluation,
    reviewed_receipt,
    reviewer_client,
)

from adapters.inbound.conversation.contract_codec import decode_public_conversation_stream_event
from adapters.jobs.span_retrieval.client import SPAN_LOG_GROUP, LogsInsightsSpanClient
from adapters.jobs.span_retrieval.parse import SpanRetrievalFailedError
from adapters.jobs.span_retrieval.query import (
    SpanQueryRefusedError,
    build_span_query,
    declared_span_attributes,
)
from adapters.outbound.cloudformation_stack import stack_resource_summaries
from adapters.outbound.https import HttpsSession, require_https
from adapters.outbound.ssm.evaluation_release import SsmEvaluationReleaseReader
from domain.conversation.answer_stream import (
    ConversationDoneEventV4,
    ConversationRefusedEventV4,
    ConversationStreamEventV4,
    ConversationTextEventV4,
    validate_conversation_stream,
)
from domain.conversation.audit import ANSWER_STREAM_CONTRACT, MANAGED_HARNESS_RUNTIME_V3
from domain.evaluation.linkage import EvaluationDeliveryOutcomeV1
from domain.evaluation.release import (
    CURRENT_EVALUATION_FAMILIES,
    EvaluationFamilyV1,
    EvaluationReleaseV1,
)
from domain.shared_kernel.errors import AdlyticsError

_ROOT = Path(__file__).resolve().parents[1]
_EVIDENCE_ROOT = (_ROOT / "validation" / "evidence").resolve()
_PARAMETER_CONTRACT = _ROOT / "contracts" / "generated" / "parameter-paths.json"
_TOOL_CONTRACT = _ROOT / "contracts" / "generated" / "tools.json"
_HARNESS_PROMPT = _ROOT / "contracts" / "prompts" / "v1" / "managed-conversation.md"
# ADR-083: a streamed turn's receipt names the Markdown answer format as its page schema.
_ANSWER_FORMAT = "answer.markdown.v1"
# Dispositions only the retired `presentation_publish` finalizer produced.
_FINALIZER_DISPOSITIONS = frozenset({"accepted", "rejected"})
_REQUIRED_ENVIRONMENT = "dev"
_REQUIRED_REGION = "us-east-1"
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_HTTP_OK = 200
_QUERY_LIMIT = 100
_POLL_INTERVAL_SECONDS = 2
_AUDIT_TIMEOUT_SECONDS = 90
_EVALUATION_TIMEOUT_SECONDS = 240
_QUERY_TIMEOUT_SECONDS = 180
_SPAN_LOG_GROUP = SPAN_LOG_GROUP
_HARNESS_QUALIFIER = "DEFAULT"
# ADR-077 §7: tool coverage runs as this synthetic dev chat persona through `/api/chat`, the only
# path to the Harness: it admits only a token the route exchange mints for the route.
_TOOL_PERSONA = "probe-a"
_HARNESS_RESOURCE_TYPE = "Custom::AgentCoreHarness"
_HARNESS_AUDIENCE = "adlytics-{environment}-harness"
_ROUTE_CLIENT = "adlytics-{environment}-conversation-route"
# Tool Lambdas by family, as `pythonFunction` names them: `adlytics-<env>-<logical id>`.
_TOOL_FUNCTIONS = {"catalog": "CatalogTool", "measurement": "MeasurementTool"}
_TOOL_LOG_TIMEOUT_SECONDS = 240
_TOOL_LOG_SETTLE_SECONDS = 30
_AGENTCORE_DENIED_ACTIONS = (
    "bedrock-agentcore:InvokeAgentRuntimeCommand",
    "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
    "bedrock-agentcore:InvokeHarness",
    "bedrock-agentcore:InvokeAgentRuntime",
)

_EXPECTED_STACKS = {
    "harness_stack": "Adlytics-Harness-dev",
    "api_stack": "Adlytics-Api-dev",
    "agent_stack": "Adlytics-Agent-dev",
    "quality_stack": "Adlytics-AgentQuality-dev",
    "quality_processor_stack": "Adlytics-AgentQualityProcessor-dev",
    "evaluation_evidence_stack": "Adlytics-EvaluationEvidence-dev",
    "evaluation_input_stack": "Adlytics-EvaluationInput-dev",
    "evaluation_outbox_dispatch_stack": "Adlytics-EvaluationOutboxDispatch-dev",
    "evaluation_stack": "Adlytics-EvaluationV4-dev",
    "evaluation_result_link_stack": "Adlytics-EvaluationResultLinkV3-dev",
    "observability_stack": "Adlytics-Observability-dev",
}
# ADR-079: each family's gate owns its online-evaluation configuration and is the only writer of
# its release record; the ingress subscribes each current family's stack-owned result group. V4 is
# the only current family, so the attested evaluation stack is V4's, with its three evaluators.
_EVALUATION_GATE_TYPE = "Custom::AgentCoreOnlineEvaluation"
_RETIRED_ONLINE_EVALUATION_TYPE = "AWS::BedrockAgentCore::OnlineEvaluationConfig"
_RETIRED_INGRESS_VARIABLE = "ADL_RELEASED_ONLINE_EVALUATION_CONFIG_IDS"
_EVALUATION_STACK_FAMILY = EvaluationFamilyV1.V4
_EVALUATION_STACK_EVALUATORS = 3
_RESULT_GROUP_PARAMETER_KEYS = {
    EvaluationFamilyV1.V4: "evaluationV4ResultLogGroupName",
}
_RELEASE_PARAMETER_NAMES = tuple(
    f"/adlytics/{_REQUIRED_ENVIRONMENT}/evaluation/release-{family.value}"
    for family in CURRENT_EVALUATION_FAMILIES
)

# Fixed fictional requests target the checked-in synthetic fixture. The route offers the model every
# declared tool, so each question is written to need its tool; coverage requires that tool to have
# run with a verified caller, not that no other tool could.
_DIRECT_QUESTIONS = {
    "catalog_list_metrics": "List the metrics this fictional workspace can report.",
    "catalog_describe_metric": "Explain retail_revenue_shipped.",
    "measurement_metric_by_grain": (
        "Report retail_revenue_shipped by product in the United States marketplace on "
        "August 11, 2026."
    ),
    "measurement_compare_windows": (
        "Compare retail_revenue_shipped by product for the later one-day window August 11, 2026 "
        "against the distinct earlier one-day window August 10, 2026 in the United States "
        "marketplace. Treat August 11 as the current period and August 10 as the prior period."
    ),
    "measurement_top_n": (
        "Rank the top five products by retail_revenue_shipped in the United States marketplace on "
        "August 11, 2026."
    ),
}
_REFUSAL_QUESTION = "Explain the fictional undeclared metric lunar basket affinity."
_MULTI_TOOL_QUESTION = (
    "First explain retail_revenue_shipped, then report retail_revenue_shipped by product in the "
    "United States marketplace on August 11, 2026."
)
_ROUTE_QUESTION = "Which declared metrics can this fictional workspace report?"
# A social turn that needs no tool. "Introduce how you can help" is not one: through the route the
# agent may list the catalog to answer it, as it did in dev on 2026-09-29.
_NO_TOOL_QUESTION = "Thank you, that is all I needed for now."

_LOG_ALLOWED_KEYS = frozenset(
    {
        "operation",
        "component",
        "phase",
        "outcome",
        "correlation_id",
        "managed_terminal",
        "publication_outcome",
        "audit_id",
        "audit_state",
        "cold_start",
        "evaluation_state",
        "function_arn",
        "function_memory_size",
        "function_name",
        "function_request_id",
        "investigation_next",
        "trace_id",
        "span_id",
        "level",
        "location",
        "message",
        "service",
        "timestamp",
        "xray_trace_id",
    }
)
_PROHIBITED_LOG_KEYS = frozenset(
    {
        "actor",
        "answer",
        "arguments",
        "baggage",
        "cookie",
        "identity",
        "nonce",
        "owner",
        "payload",
        "prompt",
        "question",
        "response",
        "session",
        "tool_result",
        "tracestate",
        "traceparent",
        "user",
    }
)


class _JsonArgumentParser(argparse.ArgumentParser):
    """Raise a bounded stage error instead of writing argparse prose to stderr."""

    def error(self, message: str) -> NoReturn:
        del message
        raise _StageError("input_validation", "invalid_arguments")


class _StageError(Exception):
    """A closed acceptance failure or pending classification without provider detail."""

    def __init__(self, stage: str, reason: str, *, pending: bool = False) -> None:
        super().__init__(stage)
        self.stage = stage
        self.reason = reason
        self.pending = pending


@dataclass(frozen=True, slots=True)
class _StackRecord:
    name: str
    resources: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class _Deployment:
    harness_arn: str
    harness_id: str
    account_id: str
    api_function: str
    api_role: str
    api_log_group: str
    outbox_table: str
    public_origin: str
    tracing_enabled: bool
    stacks: Mapping[str, _StackRecord]
    enabled_releases: tuple[EvaluationReleaseV1, ...]


@dataclass(frozen=True, slots=True)
class _RouteResult:
    name: str
    correlation_id: str
    terminal: str
    started_at: int
    ended_at: int
    receipt: ReviewedReceipt
    trace_id: str
    span_id: str
    evaluation_state: str
    arrival: _Arrival


@dataclass(frozen=True, slots=True)
class _Arrival:
    """Sanitized wire milestones for one route, in milliseconds after the request was sent.

    Content never enters this record; it exists so a release-timing change (ADR-065) is judged
    by measurement rather than by impression.
    """

    first_event_ms: int | None
    first_text_ms: int | None
    terminal_ms: int | None
    heartbeats: int
    text_deltas: int


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--reviewer-persona", default=DEFAULT_REVIEWER_PERSONA)
    for argument in _EXPECTED_STACKS:
        parser.add_argument(f"--{argument.replace('_', '-')}", required=True)
    return parser


def _validate_target(args: argparse.Namespace) -> None:
    if args.environment != _REQUIRED_ENVIRONMENT:
        raise _StageError("input_validation", "environment_not_dev")
    if args.region != _REQUIRED_REGION:
        raise _StageError("input_validation", "region_not_us_east_1")
    if not isinstance(args.expected_account, str) or not _ACCOUNT_ID.fullmatch(
        args.expected_account
    ):
        raise _StageError("input_validation", "invalid_expected_account")
    for argument, expected in _EXPECTED_STACKS.items():
        if getattr(args, argument) != expected:
            raise _StageError("input_validation", f"invalid_{argument}")


def _evidence_dir(value: str) -> Path:
    path = (_ROOT / value).resolve()
    if path == _EVIDENCE_ROOT or _EVIDENCE_ROOT not in path.parents:
        raise _StageError("input_validation", "invalid_evidence_directory")
    return path


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    return str(value)


def _write_json(directory: Path, name: str, value: object) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        json.dumps(value, default=_json_default, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _parameter_contract() -> dict[str, dict[str, str]]:
    document = cast(
        "dict[str, object]",
        json.loads(_PARAMETER_CONTRACT.read_text(encoding="utf-8")),
    )
    parameters = document.get("parameters")
    if not isinstance(parameters, dict):
        raise _StageError("deployment_attestation", "parameter_contract_malformed")
    result: dict[str, dict[str, str]] = {}
    for key, raw_value in cast("dict[object, object]", parameters).items():
        if not isinstance(key, str) or not isinstance(raw_value, dict):
            raise _StageError("deployment_attestation", "parameter_contract_malformed")
        value = cast("dict[str, object]", raw_value)
        area = value.get("area")
        name = value.get("name")
        if not isinstance(area, str) or not isinstance(name, str):
            raise _StageError("deployment_attestation", "parameter_contract_malformed")
        result[key] = {"area": area, "name": name}
    return result


def _parameter_path(parameters: Mapping[str, Mapping[str, str]], key: str) -> str:
    value = parameters.get(key)
    if value is None:
        raise _StageError("deployment_attestation", "parameter_contract_key_missing")
    return f"/adlytics/dev/{value['area']}/{value['name']}"


def _parameter_values(
    session: boto3.Session,
    parameters: Mapping[str, Mapping[str, str]],
    keys: Iterable[str],
) -> dict[str, str]:
    client = session.client("ssm")
    values: dict[str, str] = {}
    for key in keys:
        response = client.get_parameter(Name=_parameter_path(parameters, key))
        parameter = response.get("Parameter")
        value = parameter.get("Value")
        if not isinstance(value, str) or not value.strip():
            raise _StageError("deployment_attestation", "published_parameter_missing")
        values[key] = value
    return values


def _account(session: boto3.Session, expected: str) -> None:
    account = session.client("sts").get_caller_identity().get("Account")
    if account != expected:
        raise _StageError("identity_attestation", "account_mismatch")


def _stack_records(
    session: boto3.Session, args: argparse.Namespace, evidence_dir: Path
) -> dict[str, _StackRecord]:
    cloudformation = session.client("cloudformation")
    records: dict[str, _StackRecord] = {}
    raw: dict[str, object] = {}
    for argument in _EXPECTED_STACKS:
        name = cast("str", getattr(args, argument))
        # aws-pagination: an exact StackName returns one stack; the count is checked below.
        described = cloudformation.describe_stacks(StackName=name)
        stacks = described.get("Stacks", [])
        if len(stacks) != 1 or stacks[0].get("StackName") != name:
            raise _StageError("deployment_attestation", "exact_stack_record_missing")
        status = stacks[0].get("StackStatus")
        if not status.endswith("_COMPLETE"):
            raise _StageError("deployment_attestation", "stack_not_complete")
        resources = stack_resource_summaries(cloudformation, name)
        if not resources:
            raise _StageError("deployment_attestation", "stack_resources_missing")
        copied = tuple(dict(resource) for resource in resources)
        records[argument] = _StackRecord(name=name, resources=copied)
        raw[argument] = {"stack": described, "resources": {"StackResourceSummaries": copied}}
    _write_json(evidence_dir, "deployment-records.json", raw)
    return records


def _resource(
    record: _StackRecord, *, resource_type: str, logical_prefix: str
) -> dict[str, object]:
    matches = [
        resource
        for resource in record.resources
        if resource.get("ResourceType") == resource_type
        and isinstance(resource.get("LogicalResourceId"), str)
        and cast("str", resource["LogicalResourceId"]).startswith(logical_prefix)
    ]
    if len(matches) != 1:
        raise _StageError(
            "deployment_attestation",
            f"exact_stack_resource_missing:{resource_type}:{logical_prefix}:{len(matches)}",
        )
    physical = matches[0].get("PhysicalResourceId")
    if not isinstance(physical, str) or not physical:
        raise _StageError("deployment_attestation", "stack_resource_identity_missing")
    return matches[0]


def _physical(resource: Mapping[str, object]) -> str:
    return cast("str", resource["PhysicalResourceId"])


def _tool_contract() -> tuple[str, ...]:
    document = cast("dict[str, object]", json.loads(_TOOL_CONTRACT.read_text(encoding="utf-8")))
    tools = document.get("tools")
    if not isinstance(tools, list):
        raise _StageError("direct_tool_coverage", "tool_contract_malformed")
    names: list[str] = []
    for raw_tool in cast("list[object]", tools):
        if not isinstance(raw_tool, dict):
            raise _StageError("direct_tool_coverage", "tool_contract_malformed")
        tool = cast("dict[str, object]", raw_tool)
        if not isinstance(tool.get("name"), str):
            raise _StageError("direct_tool_coverage", "tool_contract_malformed")
        names.append(cast("str", tool["name"]))
    # ADR-083: the declared tools are the governed catalog and measurement tools only; the
    # retired finalizer is no longer declared, so its reappearance is an inventory mismatch.
    if len(names) != len(set(names)) or set(names) != set(_DIRECT_QUESTIONS):
        raise _StageError("direct_tool_coverage", "scenario_inventory_mismatch")
    return tuple(names)


def _tool_family(name: str) -> str:
    if name.startswith("catalog_"):
        return "catalog"
    if name.startswith("measurement_"):
        return "measurement"
    raise _StageError("direct_tool_coverage", "unknown_tool_family")


def _qualified_tool(name: str) -> str:
    return f"@adlytics/{_tool_family(name)}___{name}"


def _event_tool_name(name: str) -> str:
    return f"{_tool_family(name)}___{name}"


def harness_authorizer_matches(
    authorizer: Mapping[str, object] | None, *, issuer_url: str, environment: str
) -> bool:
    """Whether the Harness admits only a route-minted token (ADR-077 §2, amended 2026-09-26).

    The token service's issuer, the Harness audience, and the route client, each exactly; a
    Cognito token, a Gateway-audience token, or any other client is refused by the service.
    """
    if authorizer is None:
        return False
    return (
        authorizer.get("discoveryUrl") == f"{issuer_url}/.well-known/openid-configuration"
        and authorizer.get("allowedAudience") == [_HARNESS_AUDIENCE.format(environment=environment)]
        and authorizer.get("allowedClients") == [_ROUTE_CLIENT.format(environment=environment)]
    )


def _harness_control(
    *,
    session: boto3.Session,
    deployment_values: Mapping[str, str],
    stacks: Mapping[str, _StackRecord],
    evidence_dir: Path,
    tool_names: tuple[str, ...],
) -> tuple[str, str, str]:
    harness_arn = deployment_values["harnessArn"]
    expected_prompt_digest = (
        "sha256:"
        + hashlib.sha256(_HARNESS_PROMPT.read_text(encoding="utf-8").strip().encode()).hexdigest()
    )
    if deployment_values["harnessPromptDigest"] != expected_prompt_digest:
        raise _StageError("deployment_attestation", "harness_prompt_digest_mismatch")
    # ADR-077 (amended 2026-09-26): a custom resource owns the Harness, and its physical ID is the
    # Harness ID.
    harness_resource = _resource(
        stacks["harness_stack"],
        resource_type=_HARNESS_RESOURCE_TYPE,
        logical_prefix="ManagedHarness",
    )
    if _physical(harness_resource) not in harness_arn:
        raise _StageError("deployment_attestation", "harness_stack_parameter_mismatch")
    harness_id = harness_arn.rsplit("/", maxsplit=1)[-1]
    if not harness_id or harness_id == harness_arn:
        raise _StageError("deployment_attestation", "harness_identity_malformed")
    control = session.client("bedrock-agentcore-control").get_harness(harnessId=harness_id)
    harness = control.get("harness")
    if harness.get("arn") != harness_arn:
        raise _StageError("deployment_attestation", "harness_control_mismatch")
    expected_allowed = {_qualified_tool(name) for name in tool_names}
    actual_allowed = harness.get("allowedTools")
    if set(actual_allowed) != expected_allowed:
        raise _StageError("deployment_attestation", "harness_tool_authority_mismatch")
    authorizer = harness.get("authorizerConfiguration", {}).get("customJWTAuthorizer")
    if not harness_authorizer_matches(
        cast("Mapping[str, object] | None", authorizer),
        issuer_url=deployment_values["tokenIssuerUrl"],
        environment=_REQUIRED_ENVIRONMENT,
    ):
        raise _StageError("deployment_attestation", "harness_jwt_authorizer_mismatch")
    runtime_environment = harness.get("environment")
    if not runtime_environment:
        raise _StageError("deployment_attestation", "harness_runtime_missing")
    agentcore_environment = runtime_environment.get("agentCoreRuntimeEnvironment")
    if not isinstance(agentcore_environment, dict):
        raise _StageError("deployment_attestation", "harness_runtime_missing")
    runtime_arn = agentcore_environment.get("agentRuntimeArn")
    if ":runtime/" not in runtime_arn:
        raise _StageError("deployment_attestation", "harness_runtime_missing")
    _write_json(evidence_dir, "harness-control.json", control)
    execution_role = harness.get("executionRoleArn")
    harness_role = _physical(
        _resource(
            stacks["harness_stack"],
            resource_type="AWS::IAM::Role",
            logical_prefix="ExecutionRole",
        )
    )
    if execution_role.rsplit("/", maxsplit=1)[-1] != harness_role:
        raise _StageError("deployment_attestation", "harness_role_binding_mismatch")
    return harness_arn, harness_id, runtime_arn


def _collect_role_policies(
    session: boto3.Session, role_name: str, evidence_dir: Path
) -> list[Mapping[str, object]]:
    iam = session.client("iam")
    documents: list[Mapping[str, object]] = []
    raw: dict[str, object] = {"inline": {}, "attached": {}}

    inline = [
        name
        for page in iam.get_paginator("list_role_policies").paginate(RoleName=role_name)
        for name in page.get("PolicyNames", [])
    ]
    for policy_name in inline:
        policy = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)
        document = policy.get("PolicyDocument")
        if not isinstance(document, dict):
            raise _StageError("api_role_authority", "role_policy_malformed")
        documents.append(document)
        cast("dict[str, object]", raw["inline"])[policy_name] = policy

    attached = [
        item
        for page in iam.get_paginator("list_attached_role_policies").paginate(RoleName=role_name)
        for item in page.get("AttachedPolicies", [])
    ]
    for item in attached:
        arn = item.get("PolicyArn")
        if not isinstance(arn, str):
            raise _StageError("api_role_authority", "role_policy_malformed")
        policy = iam.get_policy(PolicyArn=arn).get("Policy")
        version_id = policy.get("DefaultVersionId")
        if not isinstance(version_id, str):
            raise _StageError("api_role_authority", "role_policy_malformed")
        version = iam.get_policy_version(PolicyArn=arn, VersionId=version_id)
        document = version.get("PolicyVersion", {}).get("Document")
        if not isinstance(document, dict):
            raise _StageError("api_role_authority", "role_policy_malformed")
        documents.append(document)
        cast("dict[str, object]", raw["attached"])[arn] = {
            "policy": policy,
            "version": version,
        }
    _write_json(evidence_dir, "api-role-policies.json", raw)
    return documents


def _items(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        items = cast("list[object]", value)
        if all(isinstance(item, str) for item in items):
            return tuple(cast("list[str]", items))
    return ()


def _statement_allows(
    statement: Mapping[str, object], action: str, resource: str | None = None
) -> bool:
    if statement.get("Effect") != "Allow" or "NotAction" in statement:
        return False
    actions = _items(statement.get("Action"))
    if not any(fnmatch.fnmatchcase(action.lower(), candidate.lower()) for candidate in actions):
        return False
    if resource is None:
        return True
    resources = _items(statement.get("Resource"))
    return any(fnmatch.fnmatchcase(resource, candidate) for candidate in resources)


def _policy_statements(
    documents: Iterable[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    statements: list[Mapping[str, object]] = []
    for document in documents:
        raw = document.get("Statement")
        values: list[object]
        if isinstance(raw, dict):
            values = [cast("dict[str, object]", raw)]
        elif isinstance(raw, list):
            values = cast("list[object]", raw)
        else:
            raise _StageError("api_role_authority", "role_policy_malformed")
        for value in values:
            if not isinstance(value, dict):
                raise _StageError("api_role_authority", "role_policy_malformed")
            statements.append(cast("dict[str, object]", value))
    return tuple(statements)


def invoke_function_scope(statements: Iterable[Mapping[str, object]]) -> set[str]:
    """Every resource an Allow statement grants `lambda:InvokeFunction` on."""
    return {
        resource
        for item in statements
        if _statement_allows(item, "lambda:InvokeFunction")
        for resource in _items(item.get("Resource"))
    }


def _attest_api_role(
    *,
    session: boto3.Session,
    role_name: str,
    harness_arn: str,
    harness_runtime_arn: str,
    audit_table_arn: str,
    outbox_table_arn: str,
    route_exchange_arn: str,
    evidence_dir: Path,
) -> None:
    statements = _policy_statements(_collect_role_policies(session, role_name, evidence_dir))
    endpoint_arn = f"{harness_arn}/harness-endpoint/{_HARNESS_QUALIFIER}"
    # ADR-077 §2: the route invokes the Harness only with the user's bearer, so this role holds no
    # AgentCore Allow at all, on the Harness, its endpoint, or its underlying runtime.
    if any(
        item.get("Effect") == "Allow"
        and any(
            action.lower().startswith("bedrock-agentcore:") for action in _items(item.get("Action"))
        )
        for item in statements
    ):
        raise _StageError("api_role_authority", "agentcore_authority_present")
    for resource in (harness_arn, endpoint_arn, harness_runtime_arn):
        for action in ("bedrock-agentcore:InvokeHarness", "bedrock-agentcore:InvokeAgentRuntime"):
            if any(_statement_allows(item, action, resource) for item in statements):
                raise _StageError("api_role_authority", "agentcore_authority_present")
    denied = [
        item
        for item in statements
        if item.get("Effect") == "Deny" and _items(item.get("Resource")) == ("*",)
    ]
    # A statement's action order carries no meaning, and the deployed policy sorts it.
    expected_deny = set(_AGENTCORE_DENIED_ACTIONS)
    if not any(set(_items(item.get("Action"))) == expected_deny for item in denied):
        raise _StageError("api_role_authority", "agentcore_invoke_deny_missing")
    transaction = [
        item for item in statements if _statement_allows(item, "dynamodb:TransactWriteItems")
    ]
    if len(transaction) != 1 or set(_items(transaction[0].get("Resource"))) != {
        audit_table_arn,
        outbox_table_arn,
    }:
        raise _StageError("api_role_authority", "evaluation_outbox_authority_mismatch")
    # ADR-077 §2-§3: the one Lambda this role invokes is the route exchange.
    if invoke_function_scope(statements) != {route_exchange_arn}:
        raise _StageError("api_role_authority", "route_exchange_authority_mismatch")
    for action in (
        "lambda:InvokeAsync",
        "dynamodb:BatchWriteItem",
        "dynamodb:DeleteItem",
        "sqs:SendMessage",
        "bedrock-agentcore:InvokeAgentRuntimeCommand",
        "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
    ):
        if any(_statement_allows(item, action) for item in statements):
            raise _StageError("api_role_authority", "legacy_authority_present")
    for item in statements:
        if item.get("Effect") == "Allow" and any(
            "turn-evidence" in resource for resource in _items(item.get("Resource"))
        ):
            raise _StageError("api_role_authority", "turn_evidence_authority_present")


def _subscribed_families(record: _StackRecord) -> tuple[EvaluationFamilyV1, ...]:
    """Return the current families, once the ingress subscribes exactly their owned groups."""
    filters = [
        cast("str", resource["LogicalResourceId"])
        for resource in record.resources
        if resource.get("ResourceType") == "AWS::Logs::SubscriptionFilter"
        and isinstance(resource.get("LogicalResourceId"), str)
    ]
    families = tuple(
        family
        for family in CURRENT_EVALUATION_FAMILIES
        if any(
            logical.startswith(f"ResultGroupSubscription{family.value.upper()}")
            for logical in filters
        )
    )
    total = sum(
        resource.get("ResourceType") == "AWS::Logs::SubscriptionFilter"
        for resource in record.resources
    )
    if families != CURRENT_EVALUATION_FAMILIES or total != len(CURRENT_EVALUATION_FAMILIES):
        raise _StageError("deployment_attestation", "evaluation_subscription_mismatch")
    for family in families:
        _resource(
            record,
            resource_type="AWS::Logs::SubscriptionFilter",
            logical_prefix=f"ResultGroupSubscription{family.value.upper()}",
        )
    return families


def _attest_evaluation_gate(record: _StackRecord) -> str:
    """Attest the V4 stack's single gate and return its configuration ID (its physical ID)."""
    if any(
        resource.get("ResourceType") == _RETIRED_ONLINE_EVALUATION_TYPE
        for resource in record.resources
    ):
        raise _StageError("deployment_attestation", "retired_online_evaluation_config_present")
    gates = [
        resource
        for resource in record.resources
        if resource.get("ResourceType") == _EVALUATION_GATE_TYPE
    ]
    if len(gates) != 1:
        raise _StageError("deployment_attestation", "evaluation_gate_binding_mismatch")
    return _physical(
        _resource(record, resource_type=_EVALUATION_GATE_TYPE, logical_prefix="GateV4")
    )


def _attest_release_records(
    *,
    session: boto3.Session,
    gate_config_id: str,
    families: tuple[EvaluationFamilyV1, ...],
    evidence_dir: Path,
) -> tuple[EvaluationReleaseV1, ...]:
    """Decode every current family's release record and return the enabled ones (ADR-079)."""
    try:
        releases = SsmEvaluationReleaseReader(
            _RELEASE_PARAMETER_NAMES, client=session.client("ssm")
        ).read()
    except AdlyticsError:
        raise _StageError("deployment_attestation", "evaluation_release_unreadable") from None
    _write_json(evidence_dir, "evaluation-releases.json", list(releases))
    return _enabled_release_bindings(releases, gate_config_id, families)


def _enabled_release_bindings(
    releases: Iterable[EvaluationReleaseV1 | None],
    gate_config_id: str,
    families: tuple[EvaluationFamilyV1, ...],
) -> tuple[EvaluationReleaseV1, ...]:
    """Return the enabled releases once each is subscribed and V4 names the stack's gate."""
    enabled = enabled_evaluation_releases(releases)
    if any(release.family not in families for release in enabled):
        raise _StageError("deployment_attestation", "evaluation_release_unsubscribed")
    if any(
        release.family is _EVALUATION_STACK_FAMILY and release.config_id != gate_config_id
        for release in enabled
    ):
        raise _StageError("deployment_attestation", "evaluation_config_binding_mismatch")
    return enabled


def _attest_named_stack_resources(
    *,
    session: boto3.Session,
    stacks: Mapping[str, _StackRecord],
    values: Mapping[str, str],
    families: tuple[EvaluationFamilyV1, ...],
    evidence_dir: Path,
) -> tuple[EvaluationReleaseV1, ...]:
    agent_gateway = _resource(
        stacks["agent_stack"],
        resource_type="AWS::BedrockAgentCore::Gateway",
        # ADR-077 slice 4: the CUSTOM_JWT Gateway.
        logical_prefix="JwtToolGateway",
    )
    if values["gatewayId"] not in _physical(agent_gateway):
        raise _StageError("deployment_attestation", "agent_gateway_binding_mismatch")

    audit = _resource(
        stacks["quality_stack"],
        resource_type="AWS::DynamoDB::Table",
        logical_prefix="Audit",
    )
    _resource(
        stacks["quality_stack"],
        resource_type="AWS::DynamoDB::Table",
        logical_prefix="Aggregate",
    )
    if _physical(audit) != values["agentQualityAuditTableName"]:
        raise _StageError("deployment_attestation", "quality_table_binding_mismatch")
    _resource(
        stacks["quality_processor_stack"],
        resource_type="AWS::Lambda::Function",
        logical_prefix="Processor",
    )
    _resource(
        stacks["quality_processor_stack"],
        resource_type="AWS::Lambda::EventSourceMapping",
        logical_prefix="AuditKeysOnlyMapping",
    )

    evaluation_log = _resource(
        stacks["evaluation_evidence_stack"],
        resource_type="AWS::Logs::LogGroup",
        logical_prefix="EvaluationInputLogs",
    )
    if _physical(evaluation_log) != values["evaluationInputLogGroupName"]:
        raise _StageError("deployment_attestation", "evaluation_log_binding_mismatch")
    evaluation_outbox = _resource(
        stacks["evaluation_evidence_stack"],
        resource_type="AWS::DynamoDB::Table",
        logical_prefix="EvaluationOutbox",
    )
    if _physical(evaluation_outbox) != values["evaluationOutboxTableName"]:
        raise _StageError("deployment_attestation", "evaluation_outbox_binding_mismatch")
    _resource(
        stacks["evaluation_outbox_dispatch_stack"],
        resource_type="AWS::Lambda::Function",
        logical_prefix="Dispatcher",
    )
    _resource(
        stacks["evaluation_outbox_dispatch_stack"],
        resource_type="AWS::Events::Rule",
        logical_prefix="Schedule",
    )
    evaluation_queue = _resource(
        stacks["evaluation_input_stack"],
        resource_type="AWS::SQS::Queue",
        logical_prefix="Queue",
    )
    if _physical(evaluation_queue) != values["evaluationInputQueueUrl"]:
        raise _StageError("deployment_attestation", "evaluation_queue_binding_mismatch")
    _resource(
        stacks["evaluation_input_stack"],
        resource_type="AWS::Lambda::Function",
        logical_prefix="Writer",
    )

    evaluators = [
        resource
        for resource in stacks["evaluation_stack"].resources
        if resource.get("ResourceType") == "AWS::BedrockAgentCore::Evaluator"
    ]
    if len(evaluators) != _EVALUATION_STACK_EVALUATORS:
        raise _StageError("deployment_attestation", "evaluator_binding_mismatch")
    gate_config_id = _attest_evaluation_gate(stacks["evaluation_stack"])
    enabled = _attest_release_records(
        session=session,
        gate_config_id=gate_config_id,
        families=families,
        evidence_dir=evidence_dir,
    )

    ingress = _resource(
        stacks["evaluation_result_link_stack"],
        resource_type="AWS::Lambda::Function",
        logical_prefix="Ingress",
    )
    _resource(
        stacks["evaluation_result_link_stack"],
        resource_type="AWS::Lambda::Function",
        logical_prefix="Linker",
    )
    # ADR-079: one full resource policy admits only CloudWatch Logs; no Permission is mixed in.
    _resource(
        stacks["evaluation_result_link_stack"],
        resource_type="AWS::Lambda::ResourcePolicy",
        logical_prefix="IngressResourcePolicy",
    )
    if any(
        resource.get("ResourceType") == "AWS::Lambda::Permission"
        for resource in stacks["evaluation_result_link_stack"].resources
    ):
        raise _StageError("deployment_attestation", "evaluation_ingress_permission_present")
    ingress_config = session.client("lambda").get_function_configuration(
        FunctionName=_physical(ingress)
    )
    ingress_environment = ingress_config.get("Environment", {}).get("Variables", {})
    if (
        ingress_environment.get("ADL_AGENT_QUALITY_AUDIT_TABLE")
        != values["agentQualityAuditTableName"]
    ):
        raise _StageError("deployment_attestation", "evaluation_ingress_binding_mismatch")
    if (
        ingress_environment.get("ADL_EVALUATION_WRITER_RECEIPTS_TABLE")
        != values["evaluationWriterReceiptsTableName"]
    ):
        raise _StageError("deployment_attestation", "evaluation_ingress_binding_mismatch")
    # ADR-079: the delivery receipt decides which configuration and evaluator a result may come
    # from, so the ingress reads it and carries no deploy-time configuration list.
    if (
        ingress_environment.get("ADL_EVALUATION_DELIVERIES_TABLE")
        != values["evaluationDeliveriesTableName"]
        or _RETIRED_INGRESS_VARIABLE in ingress_environment
    ):
        raise _StageError("deployment_attestation", "evaluation_ingress_binding_mismatch")
    # Every current family's owned group must be admitted, or one family of evaluators would
    # be silently ignored at the source (ADR-059).
    admitted = {
        item.strip()
        for item in ingress_environment.get("ADL_RELEASED_EVALUATION_RESULT_LOG_GROUPS", "").split(
            ","
        )
    }
    if admitted != {values[_RESULT_GROUP_PARAMETER_KEYS[family]] for family in families}:
        raise _StageError("deployment_attestation", "evaluation_ingress_binding_mismatch")
    _write_json(evidence_dir, "evaluation-ingress-configuration.json", ingress_config)

    _resource(
        stacks["observability_stack"],
        resource_type="AWS::XRay::TransactionSearchConfig",
        logical_prefix="TransactionSearch",
    )
    _resource(
        stacks["observability_stack"],
        resource_type="AWS::Logs::ResourcePolicy",
        logical_prefix="XRaySpanDelivery",
    )
    return enabled


def _deployment(
    *,
    session: boto3.Session,
    args: argparse.Namespace,
    evidence_dir: Path,
    tool_names: tuple[str, ...],
) -> _Deployment:
    stacks = _stack_records(session, args, evidence_dir)
    families = _subscribed_families(stacks["evaluation_result_link_stack"])
    parameters = _parameter_contract()
    values = _parameter_values(
        session,
        parameters,
        (
            "harnessArn",
            "harnessPromptDigest",
            "publicBaseUrl",
            "tokenIssuerUrl",
            "tokenRouteExchangeFunctionArn",
            "agentQualityAuditTableName",
            "agentQualityAuditTableArn",
            "gatewayId",
            "evaluationInputQueueUrl",
            "evaluationInputLogGroupName",
            "evaluationOutboxTableName",
            "evaluationOutboxTableArn",
            "evaluationDeliveriesTableName",
            "evaluationWriterReceiptsTableName",
            "evaluationVerdictsTableName",
            *(_RESULT_GROUP_PARAMETER_KEYS[family] for family in families),
        ),
    )
    harness_arn, harness_id, harness_runtime_arn = _harness_control(
        session=session,
        deployment_values=values,
        stacks=stacks,
        evidence_dir=evidence_dir,
        tool_names=tool_names,
    )
    enabled_releases = _attest_named_stack_resources(
        session=session,
        stacks=stacks,
        values=values,
        families=families,
        evidence_dir=evidence_dir,
    )
    api = stacks["api_stack"]
    api_function = _physical(
        _resource(
            api,
            resource_type="AWS::Lambda::Function",
            logical_prefix="ConversationFunction",
        )
    )
    api_role = _physical(
        _resource(
            api,
            resource_type="AWS::IAM::Role",
            logical_prefix="ConversationFunctionRole",
        )
    )
    api_log_group = _physical(
        _resource(
            api,
            resource_type="AWS::Logs::LogGroup",
            logical_prefix="ConversationFunctionLogs",
        )
    )
    function = session.client("lambda").get_function_configuration(FunctionName=api_function)
    environment = function.get("Environment", {}).get("Variables", {})
    tracing_enabled = environment.get("ADL_TRACING_ENABLED") == "true"
    if environment.get("ADL_HARNESS_ARN") != harness_arn:
        raise _StageError("deployment_attestation", "api_harness_binding_mismatch")
    if environment.get("ADL_AGENT_QUALITY_AUDIT_TABLE") != values["agentQualityAuditTableName"]:
        raise _StageError("deployment_attestation", "api_quality_binding_mismatch")
    if environment.get("ADL_EVALUATION_OUTBOX_TABLE") != values["evaluationOutboxTableName"]:
        raise _StageError("deployment_attestation", "api_evaluation_binding_mismatch")
    if environment.get("ADL_EVALUATION_QUEUE_URL") is not None:
        raise _StageError("deployment_attestation", "api_direct_queue_binding_present")
    if environment.get("ADL_HARNESS_PROMPT_DIGEST") != values["harnessPromptDigest"]:
        raise _StageError("deployment_attestation", "api_prompt_binding_mismatch")
    route_exchange = values["tokenRouteExchangeFunctionArn"]
    if environment.get("ADL_TOKEN_ROUTE_EXCHANGE_FUNCTION") != route_exchange:
        raise _StageError("deployment_attestation", "api_route_exchange_binding_mismatch")
    if function.get("Role", "").rsplit("/", maxsplit=1)[-1] != api_role:
        raise _StageError("deployment_attestation", "api_role_binding_mismatch")
    _write_json(evidence_dir, "api-function-configuration.json", function)
    _attest_api_role(
        session=session,
        role_name=api_role,
        harness_arn=harness_arn,
        harness_runtime_arn=harness_runtime_arn,
        audit_table_arn=values["agentQualityAuditTableArn"],
        outbox_table_arn=values["evaluationOutboxTableArn"],
        route_exchange_arn=values["tokenRouteExchangeFunctionArn"],
        evidence_dir=evidence_dir,
    )
    return _Deployment(
        harness_arn=harness_arn,
        harness_id=harness_id,
        account_id=args.expected_account,
        api_function=api_function,
        api_role=api_role,
        api_log_group=api_log_group,
        outbox_table=values["evaluationOutboxTableName"],
        public_origin=require_https(values["publicBaseUrl"].rstrip("/")),
        tracing_enabled=tracing_enabled,
        stacks=stacks,
        enabled_releases=enabled_releases,
    )


def _persona_access_token(session: boto3.Session, environment: str) -> str:
    """Sign the synthetic chat persona in on the BFF client and return its access token."""
    try:
        return sign_in(
            session.client("cognito-idp"),
            load_persona(session.client("ssm"), environment, _TOOL_PERSONA),
            app_client(session, environment),
        ).access_token
    except Exception as error:
        raise _StageError("tool_coverage", "tool_persona_sign_in_failed") from error


def _tool_records(
    session: boto3.Session, environment: str, window: LogWindow
) -> list[Mapping[str, object]]:
    """Every catalog and measurement tool terminal record the Gateway reached in the window."""
    return [
        record
        for logical_id in _TOOL_FUNCTIONS.values()
        for record in terminal_records(
            session,
            function_name=deployed_function_name(environment, logical_id),
            window=window,
        )
        if record.get("component") == "gateway_target"
    ]


def tool_coverage_verdict(
    records: Iterable[Mapping[str, object]], expected: frozenset[str]
) -> str | None:
    """Return why a route turn's tool records fail coverage, or `None` when they pass.

    Every record must report a verified caller. A turn that expects no tool must reach none; one
    that expects tools must have run each of them, named by the record's `operation`.
    """
    listed = list(records)
    if any(record.get("caller_verified") is not True for record in listed):
        return "tool_caller_unverified"
    if not expected:
        return "no_tool_custody_unexpected" if listed else None
    ran = {str(record.get("operation")) for record in listed}
    return None if expected <= ran else "expected_tool_not_called"


def _route_tool_turn(
    *,
    session: boto3.Session,
    deployment: _Deployment,
    evidence_dir: Path,
    environment: str,
    name: str,
    question: str,
    expected: frozenset[str],
) -> int:
    """Run one persona turn through `/api/chat` and require its tool evidence; return its calls."""
    started_ms = int(time.time() * 1000)
    status, chunks, correlation_id, _, _ = _same_origin_call(
        origin=deployment.public_origin,
        token=_persona_access_token(session, environment),
        csrf=secrets.token_urlsafe(24),
        question=question,
        session_id=f"managed-harness-tools-{name}-{uuid.uuid4()}",
    )
    window = LogWindow.around(started_ms, int(time.time() * 1000))
    _write_json(
        evidence_dir,
        f"tools-{name}.json",
        {
            "correlation_id": correlation_id,
            "http_status": status,
            "frames": [raw.decode("utf-8", errors="strict") for _, raw in chunks],
        },
    )
    if status != _HTTP_OK:
        raise _StageError("tool_coverage", "non_success_status")
    if _terminal(chunks) != "response_done":
        raise _StageError("tool_coverage", "managed_success_missing")
    if not expected:
        # Absence is evidence only once log ingestion has caught up.
        time.sleep(_TOOL_LOG_SETTLE_SECONDS)
        records = _tool_records(session, environment, window)
    else:
        deadline = time.monotonic() + _TOOL_LOG_TIMEOUT_SECONDS
        while True:
            records = _tool_records(session, environment, window)
            if tool_coverage_verdict(records, expected) is None or time.monotonic() > deadline:
                break
            time.sleep(_POLL_INTERVAL_SECONDS * 5)
    _write_json(
        evidence_dir,
        f"tools-{name}-records.json",
        [
            {"operation": record.get("operation"), "caller_verified": record.get("caller_verified")}
            for record in records
        ],
    )
    reason = tool_coverage_verdict(records, expected)
    if reason is not None:
        raise _StageError("tool_coverage", reason)
    return len(records)


def _tool_coverage(
    *,
    session: boto3.Session,
    deployment: _Deployment,
    evidence_dir: Path,
    environment: str,
    tool_names: tuple[str, ...],
) -> dict[str, object]:
    """Cover every declared tool, one multi-tool turn, and one no-tool turn through the route."""
    calls = 0
    for name in tool_names:
        calls += _route_tool_turn(
            session=session,
            deployment=deployment,
            evidence_dir=evidence_dir,
            environment=environment,
            name=name,
            question=_DIRECT_QUESTIONS[name],
            expected=frozenset({name}),
        )
    calls += _route_tool_turn(
        session=session,
        deployment=deployment,
        evidence_dir=evidence_dir,
        environment=environment,
        name="same-turn-multi-tool",
        question=_MULTI_TOOL_QUESTION,
        expected=frozenset({"catalog_describe_metric", "measurement_metric_by_grain"}),
    )
    _route_tool_turn(
        session=session,
        deployment=deployment,
        evidence_dir=evidence_dir,
        environment=environment,
        name="no-tool-final-prose",
        question=_NO_TOOL_QUESTION,
        expected=frozenset(),
    )
    return {
        "declared_tools": len(tool_names),
        "per_tool_turns": len(tool_names),
        "verified_tool_calls": calls,
        "tool_selection": "observed",
        "same_turn_multi_tool": "attested",
        "no_tool_clarification": "attested",
        "path": "route_minted_token",
    }


def _browser_session(session: boto3.Session) -> tuple[str, str, str]:
    """Return a BFF session for the route turns and how it was obtained.

    An operator's `ADL_REFRESH` is refreshed through `/api/auth/refresh`, the human-session path;
    otherwise an `ADL_SESSION` access token is used as given. `ADL_CSRF` is required either way.
    """
    refresh = os.environ.get("ADL_REFRESH", "").strip()
    access = os.environ.get("ADL_SESSION", "").strip()
    csrf = os.environ.get("ADL_CSRF", "").strip()
    if not csrf or not (refresh or access):
        raise _StageError("same_origin_route", "browser_session_missing")
    if not refresh:
        return access, csrf, "session"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            access_token = _refreshed_session(session, refresh, csrf)
    except SystemExit as error:
        raise _StageError("same_origin_route", "browser_session_refresh_failed") from error
    except Exception as error:
        raise _StageError("same_origin_route", "browser_session_refresh_failed") from error
    return access_token, csrf, "refresh"


def _same_origin_call(
    *,
    origin: str,
    token: str,
    csrf: str,
    question: str,
    session_id: str,
) -> tuple[int, list[tuple[float, bytes]], str, int, int]:
    # ADR-081 §2: a decoy the route must not honor. The correlation used for every later lookup
    # is the one the server minted and returned.
    decoy = str(uuid.uuid4())
    payload = json.dumps(
        {
            "schema_version": "ConversationRequest.v3",
            "question": question,
            "session_id": session_id,
        },
        separators=(",", ":"),
    ).encode()
    started_at = int(time.time()) - 5
    started = time.monotonic()
    chunks: list[tuple[float, bytes]] = []
    http = HttpsSession()
    with http.request(
        "POST",
        require_https(f"{origin}/api/chat"),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-amz-content-sha256": hashlib.sha256(payload).hexdigest(),
            "Cookie": f"adl_session={token}; adl_csrf={csrf}",
            "x-adl-csrf": csrf,
            "x-correlation-id": decoy,
            "Sec-Fetch-Site": "same-origin",
        },
        timeout=180,
        stream=True,
    ) as response:
        correlation_id = response.headers.get("x-correlation-id", "")
        if not correlation_id or correlation_id == decoy:
            raise _StageError("same_origin_route", "correlation_not_server_minted")
        while True:
            chunk = (
                response.raw.read1(8192)
                if hasattr(response.raw, "read1")
                else response.raw.read(8192)
            )
            if not chunk:
                break
            chunks.append((time.monotonic() - started, chunk))
        status = response.status_code
    return status, chunks, correlation_id, started_at, int(time.time()) + 5


def _frames(chunks: list[tuple[float, bytes]]) -> list[tuple[float, str]]:
    """Split the chunked body into SSE frames, each stamped with the arrival of its last byte."""
    frames: list[tuple[float, str]] = []
    pending = b""
    for elapsed, raw in chunks:
        pending += raw
        while b"\n\n" in pending:
            frame, pending = pending.split(b"\n\n", 1)
            frames.append((elapsed, frame.decode("utf-8", errors="strict")))
    if pending.strip():
        raise _StageError("same_origin_route", "invalid_sse")
    return frames


def _decoded_frames(
    chunks: list[tuple[float, bytes]],
) -> tuple[list[tuple[float, ConversationStreamEventV4]], int]:
    """Decode data frames; SSE comment lines are transport heartbeats and carry no event."""
    events: list[tuple[float, ConversationStreamEventV4]] = []
    heartbeats = 0
    for elapsed, frame in _frames(chunks):
        lines = [line for line in frame.splitlines() if line.strip()]
        if not lines:
            continue
        if all(line.startswith(":") for line in lines):
            heartbeats += 1
            continue
        data = [line[5:].lstrip() for line in lines if line.startswith("data:")]
        if not data or len(data) != len(lines):
            raise _StageError("same_origin_route", "invalid_sse")
        events.append((elapsed, decode_public_conversation_stream_event("\n".join(data))))
    return events, heartbeats


def _arrival(chunks: list[tuple[float, bytes]]) -> _Arrival:
    events, heartbeats = _decoded_frames(chunks)
    text = [elapsed for elapsed, event in events if isinstance(event, ConversationTextEventV4)]
    terminal = [
        elapsed
        for elapsed, event in events
        if isinstance(event, ConversationDoneEventV4 | ConversationRefusedEventV4)
    ]
    return _Arrival(
        first_event_ms=round(events[0][0] * 1000) if events else None,
        first_text_ms=round(text[0] * 1000) if text else None,
        terminal_ms=round(terminal[0] * 1000) if terminal else None,
        heartbeats=heartbeats,
        text_deltas=len(text),
    )


def _released_text(chunks: list[tuple[float, bytes]]) -> str | None:
    """Return the streamed answer of a `done` stream, `None` for a refusal (ADR-083).

    The answer is the concatenation of the `text` events in sequence; any other ending is not a
    legal V4 terminal.
    """
    events = [event for _, event in _decoded_frames(chunks)[0]]
    released = validate_conversation_stream(tuple(events), closed=True)
    if released is not None:
        if not released.strip():
            raise _StageError("same_origin_route", "managed_final_public_content_missing")
        return released
    if events and isinstance(events[-1], ConversationRefusedEventV4):
        return None
    raise _StageError("same_origin_route", "invalid_terminal")


def _terminal(chunks: list[tuple[float, bytes]]) -> str:
    return "refused" if _released_text(chunks) is None else "response_done"


def _review(review: ReviewClient, correlation_id: str, stage: str) -> dict[str, object] | None:
    """Read one turn's review, classifying an API failure as this stage's closed reason."""
    try:
        return review.turn(correlation_id)
    except ReviewUnavailableError as error:
        raise _StageError(stage, "review_api_unavailable") from error


def _reviewed_turn(review: ReviewClient, correlation_id: str) -> ReviewedReceipt:
    """Wait for the turn's receipt through the review API and attest its correlation."""
    deadline = time.monotonic() + _AUDIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        # Absent and non-unique correlations read alike through the API: both are not yet one.
        document = _review(review, correlation_id, "audit_receipt")
        if document is not None:
            try:
                receipt = reviewed_receipt(document)
            except (TypeError, ValueError) as error:
                raise _StageError("audit_receipt", "reviewed_receipt_malformed") from error
            if receipt.correlation_id != correlation_id:
                raise _StageError("audit_receipt", "exact_receipt_mismatch")
            return receipt
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise _StageError("audit_receipt", "receipt_pending", pending=True)


def _query(
    logs: Any,
    *,
    log_group: str,
    start_time: int,
    end_time: int,
    query_string: str,
    stage: str,
) -> dict[str, object]:
    started = logs.start_query(
        logGroupName=log_group,
        startTime=start_time,
        endTime=end_time,
        queryString=query_string,
        limit=_QUERY_LIMIT,
    )
    query_id = started.get("queryId")
    if not isinstance(query_id, str):
        raise _StageError(stage, "query_start_failed")
    deadline = time.monotonic() + _QUERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = cast("dict[str, object]", logs.get_query_results(queryId=query_id))
        status = result.get("status")
        if status == "Complete":
            return result
        if status in {"Cancelled", "Failed", "Timeout", "Unknown"}:
            raise _StageError(stage, "query_failed")
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise _StageError(stage, "query_pending", pending=True)


def _query_rows(result: Mapping[str, object], *, stage: str) -> list[dict[str, str]]:
    rows = result.get("results")
    if not isinstance(rows, list):
        raise _StageError(stage, "query_result_malformed")
    projected: list[dict[str, str]] = []
    for raw_row in cast("list[object]", rows):
        if not isinstance(raw_row, list):
            raise _StageError(stage, "query_result_malformed")
        row: dict[str, str] = {}
        for raw_field in cast("list[object]", raw_row):
            if not isinstance(raw_field, dict):
                raise _StageError(stage, "query_result_malformed")
            field_value = cast("dict[str, object]", raw_field)
            field = field_value.get("field")
            value = field_value.get("value")
            if isinstance(field, str) and isinstance(value, str):
                row[field] = value
        projected.append(row)
    return projected


def _structured_log(  # noqa: PLR0912 - one closed validation pass over returned log evidence
    *,
    session: boto3.Session,
    deployment: _Deployment,
    evidence_dir: Path,
    correlation_id: str,
    start_time: int,
    end_time: int,
) -> tuple[str, str, str]:
    escaped = correlation_id.replace("'", "")
    deadline = time.monotonic() + _QUERY_TIMEOUT_SECONDS
    rows: list[dict[str, str]] = []
    result: dict[str, object] = {}
    while time.monotonic() < deadline:
        result = _query(
            session.client("logs"),
            log_group=deployment.api_log_group,
            start_time=start_time,
            end_time=end_time,
            query_string=(
                "fields @timestamp, @message | "
                f"filter correlation_id = '{escaped}' and operation = 'conversation.turn' "
                "and phase = 'terminal' | sort @timestamp asc | limit 2"
            ),
            stage="structured_log",
        )
        rows = _query_rows(result, stage="structured_log")
        if rows:
            break
        time.sleep(_POLL_INTERVAL_SECONDS)
    _write_json(evidence_dir, f"log-{correlation_id}.json", result)
    if len(rows) != 1:
        reason = "terminal_log_pending" if not rows else "terminal_log_cardinality"
        raise _StageError("structured_log", reason, pending=not rows)
    message = rows[0].get("@message")
    if not isinstance(message, str):
        raise _StageError("structured_log", "terminal_log_malformed")
    raw_event = json.loads(message)
    if not isinstance(raw_event, dict):
        raise _StageError("structured_log", "terminal_log_malformed")
    event = cast("dict[str, object]", raw_event)
    lowered_keys = {key.lower() for key in event}
    if any(prohibited in key for key in lowered_keys for prohibited in _PROHIBITED_LOG_KEYS):
        raise _StageError("structured_log", "protected_log_field_present")
    unknown = lowered_keys - _LOG_ALLOWED_KEYS
    if unknown:
        raise _StageError("structured_log", "unbounded_log_field_present")
    required = {
        "operation": "conversation.turn",
        "component": "conversation_api",
        "phase": "terminal",
        "audit_state": "persisted",
    }
    if any(event.get(key) != value for key, value in required.items()):
        raise _StageError("structured_log", "terminal_log_contract_mismatch")
    if event.get("correlation_id") != correlation_id:
        raise _StageError("structured_log", "terminal_log_correlation_mismatch")
    if event.get("investigation_next") not in {
        "inspect_harness_trace",
        "inspect_terminal_audit",
        "inspect_evaluation_chain",
        "inspect_application_failure",
    }:
        raise _StageError("structured_log", "investigation_handoff_mismatch")
    trace_id = event.get("trace_id")
    span_id = event.get("span_id")
    audit_id = event.get("audit_id")
    if not isinstance(trace_id, str) or not _TRACE_ID.fullmatch(trace_id):
        raise _StageError("structured_log", "trace_id_missing", pending=True)
    if not isinstance(span_id, str) or not _SPAN_ID.fullmatch(span_id):
        raise _StageError("structured_log", "span_id_missing", pending=True)
    if not isinstance(audit_id, str) or not audit_id:
        raise _StageError("structured_log", "audit_id_missing", pending=True)
    return trace_id, span_id, audit_id


def _dispatch_state(
    delivery: ReviewedDelivery,
    *,
    writer_present: bool,
    result_count: int,
) -> str | None:
    """Classify an attested dispatch decision; `None` while a dispatched row awaits its writer."""
    if delivery.outcome is EvaluationDeliveryOutcomeV1.NOT_DISPATCHED:
        # Nothing was sent, so nothing may have been emitted for or judged about the turn.
        if writer_present or result_count:
            raise _StageError("evaluation_chain", "undispatched_evaluation_emitted")
        reason = cast("Any", delivery.not_dispatched_reason).value
        return f"not_dispatched_{reason}"
    return None


def _evaluation_chain(
    *,
    review: ReviewClient,
    correlation_id: str,
    receipt: ReviewedReceipt,
    enabled: tuple[EvaluationReleaseV1, ...],
    evidence_dir: Path,
    scenario: str,
) -> str:
    admission_ref = receipt.evaluation_admission_ref
    if not isinstance(admission_ref, str) or not admission_ref:
        raise _StageError("evaluation_chain", "admission_not_selected", pending=True)
    deadline = time.monotonic() + _EVALUATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        document = _review(review, correlation_id, "evaluation_chain")
        if document is None:
            raise _StageError("evaluation_chain", "reviewed_receipt_absent")
        try:
            evaluation = reviewed_evaluation(document)
        except (TypeError, ValueError) as error:
            raise _StageError("evaluation_chain", "reviewed_evaluation_malformed") from error
        if evaluation is None:
            raise _StageError("evaluation_chain", "admission_not_selected")
        state: str | None = None
        if evaluation.admitted and evaluation.delivery is not None:
            # ADR-079: the receipt must agree with the release records read at deployment.
            mismatch = evaluation_dispatch_mismatch(evaluation.delivery, enabled)
            if mismatch is not None:
                raise _StageError("evaluation_chain", mismatch)
            state = _dispatch_state(
                cast("ReviewedDelivery", evaluation.delivery),
                writer_present=evaluation.writer_present,
                result_count=len(evaluation.verdicts),
            )
        if state is None and evaluation.admitted and evaluation.writer_present:
            states = sorted(
                {
                    semantic_evaluation_result_state(cast("object", item))
                    for item in evaluation.verdicts
                }
            )
            state = "writer_attested" if not evaluation.verdicts else "sampled_" + "+".join(states)
        if state is not None:
            _write_json(evidence_dir, f"evaluation-{scenario}.json", document.get("evaluation"))
            return state
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise _StageError("evaluation_chain", "evaluation_delivery_pending", pending=True)


def streamed_receipt_mismatch(  # noqa: PLR0911 - one closed reason per receipt defect
    receipt: ReviewedReceipt, released: str
) -> str | None:
    """Return why a completed turn's receipt is not the streamed-answer shape, or `None`.

    ADR-083: the receipt records a complete, published turn under the V4 stream and the Markdown
    answer format, holds no finalizer step and a reference on every governed one, records no plan
    and no blocks, and counts exactly the characters of answer text the stream released.
    """
    if receipt.response_contract_version != ANSWER_STREAM_CONTRACT:
        return "answer_stream_contract_missing"
    if (
        receipt.terminal_outcome.value != "complete"
        or receipt.publication_outcome.value != "published"
    ):
        return "audit_terminal_incomplete"
    if receipt.page_schema_version != _ANSWER_FORMAT:
        return "answer_format_missing"
    if any(
        step.disposition.value in _FINALIZER_DISPOSITIONS or step.selected_into_plan is not None
        for step in receipt.trajectory
    ):
        return "audit_trajectory_finalizer_present"
    if any(step.governed and step.step_ref is None for step in receipt.trajectory):
        return "audit_trajectory_unanchored"
    if receipt.prompt_catalog_digest is None or receipt.tool_custody_digest is None:
        return "managed_publication_metadata_missing"
    if (
        receipt.presentation_plan_digest is not None
        or receipt.public_block_kinds
        or receipt.public_block_count
    ):
        return "block_publication_present"
    if receipt.released_character_count != len(released):
        return "released_text_count_mismatch"
    return None


def limitation_published_without_substitute(receipt: ReviewedReceipt) -> bool:
    """Whether an unmappable request was answered, and no governed tool succeeded in its place.

    The operation outcome alone cannot say so: the route records `succeeded` for a completed turn
    that called no governed tool, and `not_attempted` only when the Harness returned no result.
    """
    return (
        receipt.publication_outcome.value == "published"
        and receipt.terminal_outcome.value == "complete"
        and receipt.operation_outcome.value != "failed"
        and not any(
            step.governed and step.disposition.value == "succeeded" for step in receipt.trajectory
        )
    )


def _route_scenario(
    *,
    session: boto3.Session,
    deployment: _Deployment,
    evidence_dir: Path,
    review: ReviewClient,
    token: str,
    csrf: str,
    name: str,
    question: str,
) -> _RouteResult:
    status, chunks, correlation_id, started_at, ended_at = _same_origin_call(
        origin=deployment.public_origin,
        token=token,
        csrf=csrf,
        question=question,
        session_id=f"managed-harness-{name}-{uuid.uuid4()}",
    )
    _write_json(
        evidence_dir,
        f"route-{name}.json",
        {
            "correlation_id": correlation_id,
            "http_status": status,
            "frames": [raw.decode("utf-8", errors="strict") for _, raw in chunks],
            "timing": [
                {"arrival_ms": round(elapsed * 1000, 3), "size_bytes": len(raw)}
                for elapsed, raw in chunks
            ],
            "arrival": asdict(_arrival(chunks)) if status == _HTTP_OK else None,
        },
    )
    if status != _HTTP_OK:
        raise _StageError("same_origin_route", "non_success_status")
    released = _released_text(chunks)
    if released is None:
        raise _StageError("same_origin_route", "managed_success_missing")
    terminal = "response_done"
    trace_id, span_id, logged_audit_id = _structured_log(
        session=session,
        deployment=deployment,
        evidence_dir=evidence_dir,
        correlation_id=correlation_id,
        start_time=started_at,
        end_time=ended_at + _AUDIT_TIMEOUT_SECONDS,
    )
    receipt = _reviewed_turn(review, correlation_id)
    _write_json(evidence_dir, f"audit-{name}.json", asdict(receipt))
    if receipt.audit_id != logged_audit_id:
        raise _StageError("audit_receipt", "structured_log_audit_mismatch")
    if receipt.runtime_version != MANAGED_HARNESS_RUNTIME_V3:
        raise _StageError("audit_receipt", "managed_runtime_version_missing")
    if receipt.trace_id != trace_id:
        raise _StageError("audit_receipt", "audit_trace_mismatch")
    reason = streamed_receipt_mismatch(receipt, released)
    if reason is not None:
        raise _StageError("audit_receipt", reason)
    admission_ref = receipt.evaluation_admission_ref
    if not isinstance(admission_ref, str) or not admission_ref:
        raise _StageError("evaluation_outbox", "admission_not_selected", pending=True)
    outbox = (
        session.client("dynamodb")
        .get_item(
            TableName=deployment.outbox_table,
            Key={"admission_ref": {"S": admission_ref}},
            ConsistentRead=True,
        )
        .get("Item")
    )
    if not isinstance(outbox, dict):
        raise _StageError("evaluation_outbox", "durable_record_missing", pending=True)
    if outbox.get("audit_id", {}).get("S") != receipt.audit_id:
        raise _StageError("evaluation_outbox", "durable_record_mismatch")
    _write_json(evidence_dir, f"outbox-{name}.json", outbox)
    evaluation_state = _evaluation_chain(
        review=review,
        correlation_id=correlation_id,
        receipt=receipt,
        enabled=deployment.enabled_releases,
        evidence_dir=evidence_dir,
        scenario=name,
    )
    return _RouteResult(
        name=name,
        correlation_id=correlation_id,
        terminal=terminal,
        started_at=started_at,
        ended_at=ended_at,
        receipt=receipt,
        trace_id=trace_id,
        span_id=span_id,
        evaluation_state=evaluation_state,
        arrival=_arrival(chunks),
    )


def _string_leaves(value: object) -> tuple[str, ...]:
    leaves: list[str] = []
    if isinstance(value, str):
        leaves.append(value)
    elif isinstance(value, dict):
        for key, item in cast("dict[object, object]", value).items():
            if isinstance(key, str):
                leaves.append(key)
            leaves.extend(_string_leaves(item))
    elif isinstance(value, list):
        for item in cast("list[object]", value):
            leaves.extend(_string_leaves(item))
    return tuple(leaves)


def _trace_query(
    *,
    session: boto3.Session,
    deployment: _Deployment,
    evidence_dir: Path,
    route: _RouteResult,
) -> dict[str, object]:
    if not deployment.tracing_enabled:
        raise _StageError("trace_continuity", "transaction_search_not_configured", pending=True)
    # The turn's spans through the 5a span-retrieval module: one page, by the turn's trace ID.
    try:
        query = build_span_query(
            environment=_REQUIRED_ENVIRONMENT,
            window_start=datetime.fromtimestamp(route.started_at, UTC),
            window_end=datetime.fromtimestamp(route.ended_at + _AUDIT_TIMEOUT_SECONDS, UTC),
            declared_attributes=declared_span_attributes(),
            trace_id=route.trace_id,
            limit=_QUERY_LIMIT,
            max_pages=1,
        )
        result = LogsInsightsSpanClient(
            session.client("logs"),
            poll_seconds=_POLL_INTERVAL_SECONDS,
            timeout_seconds=_QUERY_TIMEOUT_SECONDS,
        ).retrieve(query)
    except SpanQueryRefusedError as error:
        raise _StageError("trace_continuity", "query_start_failed") from error
    except SpanRetrievalFailedError as error:
        if error.reason.startswith("span_"):
            raise _StageError("trace_continuity", error.reason, pending=True) from error
        raise _StageError("trace_continuity", "query_failed") from error
    if result["reason"] == "query_running":
        raise _StageError("trace_continuity", "query_pending", pending=True)
    _write_json(evidence_dir, f"trace-{route.name}.json", result)
    documents = tuple(cast("list[Mapping[str, object]]", result["spans"]))
    if not documents:
        raise _StageError("trace_continuity", "trace_spans_pending", pending=True)
    if result["truncated"]:
        raise _StageError("trace_continuity", "trace_span_bound_exceeded", pending=True)

    return trace_topology(documents)


_TRACE_WAIT_SECONDS = 600
"""Span ingestion into `aws/spans` lags the turn by minutes, and a turn's spans arrive separately."""
_TRACE_POLL_SECONDS = 20


def _trace_when_ingested(**query: Any) -> dict[str, object]:
    """Re-run the trace query while it is only pending, until every route span has arrived."""
    deadline = time.monotonic() + _TRACE_WAIT_SECONDS
    while True:
        try:
            return _trace_query(**query)
        except _StageError as error:
            if not error.pending or time.monotonic() >= deadline:
                raise
        time.sleep(_TRACE_POLL_SECONDS)


# Observability §3.3: the Conversation API's own spans and its InvokeHarness call share the turn's
# trace; the Gateway tool boundary starts a new trace. The service exported no Harness-internal
# span to `aws/spans` in dev on 2026-09-29, so those are reported, never required.
_ROUTE_TRACE_SPANS = (
    "conversation.turn",
    "conversation.route_exchange",
    "Bedrock AgentCore.InvokeHarness",
)


def trace_topology(documents: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """Judge one turn's trace: every span the route owns is present, once, and none errored."""
    listed = list(documents)
    names = [str(document.get("name", "")) for document in listed]
    errored = [
        document
        for document in listed
        if str((cast("Mapping[str, object]", document.get("status") or {})).get("code", "")).upper()
        == "ERROR"
    ]
    if errored:
        raise _StageError("trace_continuity", "trace_span_errored")
    missing = [name for name in _ROUTE_TRACE_SPANS if name not in names]
    if missing:
        raise _StageError("trace_continuity", "exact_trace_topology_unavailable", pending=True)
    by_id = {str(document.get("spanId")) for document in listed}
    if len(by_id) < len(listed):
        raise _StageError("trace_continuity", "trace_boundaries_not_distinct", pending=True)
    tool_names = {_event_tool_name(name).lower() for name in _tool_contract()}
    in_trace_tools = any(
        {value.lower() for value in _string_leaves(document)} & tool_names for document in listed
    )
    harness_internal = any(
        value == "bedrock-agentcore" or value.startswith("strands")
        for document in listed
        for value in (item.lower() for item in _string_leaves(document))
    )
    return {
        "span_count": len(listed),
        "api": "attested",
        "route_exchange": "attested",
        "harness_invocation": "attested",
        "harness_internal_spans": "present" if harness_internal else "not_exported",
        "gateway": "same_trace" if in_trace_tools else "separate_trace",
        "outcome": "attested",
    }


def _exact_log_group(logs: Any, *, log_group: str, account_id: str) -> Mapping[str, object]:
    arn = f"arn:aws:logs:{_REQUIRED_REGION}:{account_id}:log-group:{log_group}"
    # aws-pagination: one exact log group ARN names at most one group.
    response = logs.describe_log_groups(logGroupIdentifiers=[arn], limit=1)
    groups = [
        group for group in response.get("logGroups", []) if group.get("logGroupName") == log_group
    ]
    if len(groups) != 1:
        raise _StageError("f23_custody", "exact_log_group_missing", pending=True)
    return cast("Mapping[str, object]", groups[0])


def _f23_custody(
    session: boto3.Session,
    deployment: _Deployment,
    evidence_dir: Path,
) -> dict[str, object]:
    """Read exact available controls without blocking fictional dev acceptance."""
    logs = session.client("logs")
    groups: dict[str, object] = {}
    subscriptions: dict[str, object] = {}
    for name in (deployment.api_log_group, _SPAN_LOG_GROUP):
        group = _exact_log_group(
            logs,
            log_group=name,
            account_id=deployment.account_id,
        )
        groups[name] = group
        pages = logs.get_paginator("describe_subscription_filters").paginate(logGroupName=name)
        subscriptions[name] = {
            "subscriptionFilters": [
                item for page in pages for item in page.get("subscriptionFilters", [])
            ]
        }
    _write_json(
        evidence_dir,
        "f23-log-custody.json",
        {"groups": groups, "subscription_filters": subscriptions},
    )
    span_group = cast("Mapping[str, object]", groups[_SPAN_LOG_GROUP])
    retention = span_group.get("retentionInDays")
    filters = cast("Mapping[str, object]", subscriptions[_SPAN_LOG_GROUP]).get(
        "subscriptionFilters"
    )
    return {
        "exact_group": "attested",
        "retention": "attested" if isinstance(retention, int) and retention > 0 else "pending",
        "subscription_exports": "attested" if isinstance(filters, list) else "pending",
        "protected_reader_authority": "pending",
        "customer_traffic": "prohibited",
    }


def _summary(
    *,
    tools: Mapping[str, object],
    session_source: str,
    routes: tuple[_RouteResult, ...],
    trace: Mapping[str, object],
    f23_custody: Mapping[str, object],
) -> dict[str, object]:
    return {
        "status": "passed",
        "deployment": {
            "exact_stack_records": len(_EXPECTED_STACKS),
            "harness_binding": "attested",
            "api_role": "no_legacy_authority",
        },
        "tool_coverage": dict(tools),
        "same_origin": {
            "session_source": session_source,
            "scenarios": len(routes),
            "managed_successes": sum(route.terminal == "response_done" for route in routes),
            "unavailable_page_continuation": "attested",
            "exact_structured_logs": len(routes),
            "exact_audit_receipts": len(routes),
            "evaluation_chains": len(routes),
            "evaluation_states": sorted({route.evaluation_state for route in routes}),
            "arrival_ms": {route.name: asdict(route.arrival) for route in routes},
        },
        "trace": dict(trace),
        "f23_custody": dict(f23_custody),
    }


def _failure_summary(error: _StageError) -> dict[str, str]:
    summary = {
        "status": "pending" if error.pending else "failed",
        "stage": error.stage,
        "reason": error.reason,
    }
    if error.stage == "f23_custody":
        summary["result_scope"] = "pre_customer_traffic"
    return summary


def main(argv: list[str] | None = None) -> int:
    """Run the joined acceptance and emit only one bounded JSON summary."""
    try:
        args = _parser().parse_args(argv)
        _validate_target(args)
        evidence_dir = _evidence_dir(args.evidence_dir)
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        _account(session, args.expected_account)
        tool_names = _tool_contract()
        deployment = _deployment(
            session=session,
            args=args,
            evidence_dir=evidence_dir,
            tool_names=tool_names,
        )
        tools = _tool_coverage(
            session=session,
            deployment=deployment,
            evidence_dir=evidence_dir,
            environment=args.environment,
            tool_names=tool_names,
        )
        access_token, csrf, session_source = _browser_session(session)
        try:
            review = reviewer_client(session, args.environment, args.reviewer_persona)
        except Exception as error:
            raise _StageError("audit_receipt", "reviewer_persona_unavailable") from error
        success = _route_scenario(
            session=session,
            deployment=deployment,
            evidence_dir=evidence_dir,
            review=review,
            token=access_token,
            csrf=csrf,
            name="managed-success",
            question=_ROUTE_QUESTION,
        )
        typed_refusal = _route_scenario(
            session=session,
            deployment=deployment,
            evidence_dir=evidence_dir,
            review=review,
            token=access_token,
            csrf=csrf,
            name="typed-tool-refusal",
            question=_REFUSAL_QUESTION,
        )
        if not limitation_published_without_substitute(typed_refusal.receipt):
            raise _StageError("same_origin_route", "typed_refusal_publication_mismatch")
        trace = _trace_when_ingested(
            session=session,
            deployment=deployment,
            evidence_dir=evidence_dir,
            route=success,
        )
        f23_custody = _f23_custody(session, deployment, evidence_dir)
        summary = _summary(
            tools=tools,
            session_source=session_source,
            routes=(success, typed_refusal),
            trace=trace,
            f23_custody=f23_custody,
        )
    except _StageError as error:
        print(json.dumps(_failure_summary(error), separators=(",", ":"), sort_keys=True))
        return 2
    except Exception:  # Outer boundary never exposes raw provider failures.
        print(
            json.dumps(
                {
                    "reason": "evidence_not_established",
                    "stage": "unexpected_failure",
                    "status": "failed",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
