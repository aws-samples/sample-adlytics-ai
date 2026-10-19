# SPDX-License-Identifier: MIT-0
r"""Read-only custody acceptance for the owned evaluation result group (ADR-079).

Runs eleven named checks in order against the deployed dev environment and prints one line per
check, then one report-only line. Output carries counts and repository stack short names only,
never an ARN, account, identifier or record value. Every call is a read (STS, CloudFormation
template and resource reads, SSM parameter reads, IAM role reads and policy simulation, the gate's
own CloudWatch Logs readback, one online-evaluation configuration read); nothing is changed.

    make accept-evaluation-custody PROFILE=<p> ENV=dev REGION=us-east-1 ACCOUNT=<account> \\
        EXPECTED_FLAGS='<the line make print-deploy-flags printed before the deploy>'

Checks: target, config_flags, stack_set, spec_resolution, reader_bound, writer_bound,
custody_bound, unscoped_bound, invoke_bound, custody, wiring. A failed prerequisite (the first
four) stops the run. Exit status is non-zero on any FAIL; the REPORT line never changes it.

The decision logic is pure functions over plain data; every AWS read is a callable passed in, so
the unit tests exercise each check without AWS (tests/unit/test_probe_evaluation_custody.py).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain.evaluation.gate import (
    EvaluationGateSpecError,
    ExecutionStatus,
    GateFinding,
    OnlineEvaluationConfigState,
    OnlineEvaluationStatus,
    ResultGroupCustodyV1,
)
from domain.evaluation.release import EvaluationReleaseV1

ENVIRONMENT = "dev"
REGION = "us-east-1"
# `evaluation-gate.ts` EVALUATION_GATE_RESOURCE_TYPE; a unit test reads the constant's line.
EVALUATION_GATE_RESOURCE_TYPE = "Custom::AgentCoreOnlineEvaluation"
REQUIRED_TRUE_FLAGS = (
    "ADLYTICS_ONLINE_EVALUATION_PROVISIONED",
    "ADLYTICS_ONLINE_EVALUATION_V4_REQUESTED",
)
ALLOWED_STACK_STATUSES = frozenset(
    {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}
)

# The action classes are the synth gate's (infra/test/support/log-write-scope.ts and
# invoke-sources.ts); parity tests assert the tuples are equal.
READ = (
    "logs:GetLogEvents",
    "logs:FilterLogEvents",
    "logs:StartQuery",
    "logs:GetQueryResults",
    "logs:StartLiveTail",
    "logs:GetLogRecord",
    "logs:Unmask",
)
WRITE_STREAM = ("logs:CreateLogStream", "logs:PutLogEvents")
CREATE_GROUP = "logs:CreateLogGroup"
CUSTODY_SCOPED = (
    "logs:PutSubscriptionFilter",
    "logs:DeleteSubscriptionFilter",
    "logs:CreateExportTask",
    "logs:AssociateKmsKey",
    "logs:DisassociateKmsKey",
    "logs:PutRetentionPolicy",
    "logs:DeleteRetentionPolicy",
    "logs:DeleteLogGroup",
    "logs:PutLogGroupDeletionProtection",
    "logs:PutDataProtectionPolicy",
    "logs:DeleteDataProtectionPolicy",
    "logs:PutTransformer",
    "logs:DeleteTransformer",
)
CUSTODY_UNSCOPED = (
    "logs:PutResourcePolicy",
    "logs:PutAccountPolicy",
    "logs:DeleteAccountPolicy",
    "logs:PutDeliveryDestination",
)
INVOKE_SOURCE = (
    "lambda:CreateEventSourceMapping",
    "lambda:UpdateEventSourceMapping",
    "lambda:AddPermission",
    "lambda:RemovePermission",
    "lambda:PutResourcePolicy",
    "lambda:DeleteResourcePolicy",
    "lambda:UpdateFunctionCode",
    "lambda:UpdateFunctionConfiguration",
    "lambda:PutFunctionEventInvokeConfig",
    "lambda:UpdateFunctionEventInvokeConfig",
    "lambda:CreateFunctionUrlConfig",
    "lambda:UpdateFunctionUrlConfig",
    "lambda:DeleteFunction",
    "lambda:PublishVersion",
    "lambda:CreateAlias",
    "lambda:UpdateAlias",
)
# Write actions on `log-group` that are neither WRITE nor watched by the result-group rule.
UNWATCHED_LOGS_ACTIONS = (
    "logs:PutBearerTokenAuthentication",
    "logs:PutSyslogConfiguration",
    "logs:DeleteSyslogConfiguration",
    "logs:IntegrateWithS3Table",
    "logs:IntegrateWithDataset",
    "logs:ProcessWithPipeline",
    "logs:PutMetricFilter",
    "logs:CreateLogAnomalyDetector",
)
_PROBE_STREAM = "adl-custody-probe"
_FICTIONAL_MAPPING = "event-source-mapping:00000000-0000-4000-8000-000000000001"
_UNRESOLVED = "<unresolved>"
_LOGICAL_HASH = re.compile(r"[0-9A-F]{8}\Z")
_PSEUDO_REFS = ("AWS::Partition", "AWS::Region", "AWS::AccountId", "AWS::URLSuffix")


@dataclass(frozen=True, slots=True)
class Role:
    """One application role: its repository short name and its ARN."""

    short_name: str
    arn: str


@dataclass(frozen=True, slots=True)
class Simulation:
    """One simulated call: the action, the resource and the optional Lambda function context."""

    action: str
    resource: str
    function_arn: str | None = None


Allowed = Callable[[Role, Simulation], bool]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def short_stack(name: str, environment: str = ENVIRONMENT) -> str:
    """Return `EvaluationV4` for `Adlytics-EvaluationV4-dev`."""
    return name.removeprefix("Adlytics-").removesuffix(f"-{environment}")


def role_short_name(stack_name: str, logical_id: str) -> str:
    """Return `Harness/ExecutionRole` for the HarnessStack role `ExecutionRole<8-hex hash>`."""
    return f"{short_stack(stack_name)}/{_LOGICAL_HASH.sub('', logical_id)}"


# --- target, config_flags, stack_set ----------------------------------------------------------


def check_target(
    environment: str, region: str, expected_account: str, caller_account: Callable[[], str]
) -> str:
    """Refuse a non-dev target before any AWS call, then compare the profile's account."""
    if environment != ENVIRONMENT:
        return "FAIL target: environment must be dev"
    if region != REGION:
        return "FAIL target: region must be us-east-1"
    if caller_account() != expected_account:
        return "FAIL target: the profile resolves to another account"
    return "PASS target"


def _flag_map(line: str) -> dict[str, str]:
    pairs = [item.partition("=") for item in line.split(",") if item.strip()]
    return {name.strip(): value.strip() for name, _, value in pairs}


def check_config_flags(actual: str, expected: str) -> list[str]:
    """Compare the run's flags with the recorded line; both evaluation flags must be true."""
    run, recorded = _flag_map(actual), _flag_map(expected)
    lines = [
        f"FAIL config_flags: {name} must be true"
        for name in REQUIRED_TRUE_FLAGS
        if run.get(name) != "true" or recorded.get(name) != "true"
    ]
    lines += [
        f"FAIL config_flags: {name} differs from the expected deploy flags"
        for name in sorted(set(run) | set(recorded))
        if name not in REQUIRED_TRUE_FLAGS and run.get(name) != recorded.get(name)
    ]
    return lines or ["PASS config_flags"]


def check_stack_set(
    names: Sequence[str], status_of: Callable[[str], str], environment: str = ENVIRONMENT
) -> list[str]:
    """Pass when every stack is a settled application stack and both evaluation stacks exist."""
    pattern = re.compile(rf"\AAdlytics-[A-Za-z0-9]+-{re.escape(environment)}\Z")
    lines: list[str] = []
    for required in (
        f"Adlytics-EvaluationV4-{environment}",
        f"Adlytics-EvaluationResultLinkV3-{environment}",
    ):
        if required not in names:
            lines.append(f"FAIL stack_set: {required} is not in the stack list")
    for name in names:
        if pattern.fullmatch(name) is None:
            lines.append(f"FAIL stack_set: {name} is not an application stack name")
            continue
        status = status_of(name)
        if status not in ALLOWED_STACK_STATUSES:
            lines.append(f"FAIL stack_set: {short_stack(name, environment)} is {status}")
    return lines or ["PASS stack_set"]


# --- spec_resolution ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedSpec:
    """What the deployed GateSpec names: the custody value, its writer and its release record."""

    custody: ResultGroupCustodyV1
    execution_role_arn: str
    release_parameter_name: str


@dataclass(frozen=True, slots=True)
class SpecLookups:
    """The reads the resolver may make: SSM values, pseudo parameters and role ARNs."""

    ssm_value: Callable[[str], str]
    pseudo: Mapping[str, str]
    role_arn: Callable[[str], str]


def _resolve(value: object, template: Mapping[str, Any], lookups: SpecLookups) -> str:
    """Render one template value; any token the resolver does not accept becomes a sentinel."""
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return _UNRESOLVED
    node = cast("Mapping[str, Any]", value)
    if "Ref" in node:
        ref = node["Ref"]
        if ref in _PSEUDO_REFS:
            return lookups.pseudo[ref]
        parameter = cast("Mapping[str, Any]", template.get("Parameters", {})).get(ref, {})
        if parameter.get("Type") == "AWS::SSM::Parameter::Value<String>" and isinstance(
            parameter.get("Default"), str
        ):
            return lookups.ssm_value(parameter["Default"])
        return _UNRESOLVED
    if "Fn::GetAtt" in node:
        target, attribute = [*list(node["Fn::GetAtt"]), "", ""][:2]
        resources = cast("Mapping[str, Any]", template.get("Resources", {}))
        if attribute == "Arn" and resources.get(target, {}).get("Type") == "AWS::IAM::Role":
            return lookups.role_arn(str(target))
        return _UNRESOLVED
    if "Fn::Join" in node:
        delimiter, parts = node["Fn::Join"]
        return str(delimiter).join(_resolve(part, template, lookups) for part in parts)
    return _UNRESOLVED


_REQUIRED_SPEC_FIELDS = (
    "result_log_group_name",
    "result_log_group_arn",
    "result_retention_days",
    "ingress_subscription_filter_name",
    "ingress_function_arn",
    "execution_role_arn",
    "release_parameter_name",
)


def gate_resource(template: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    """Return the one gate custom resource of the V4 template, by its logical id."""
    resources = cast("Mapping[str, Mapping[str, Any]]", template.get("Resources", {}))
    found = [
        (lid, r) for lid, r in resources.items() if r.get("Type") == EVALUATION_GATE_RESOURCE_TYPE
    ]
    return found[0] if len(found) == 1 else None


def check_spec_resolution(
    template: Mapping[str, Any], lookups: SpecLookups
) -> tuple[list[str], ResolvedSpec | None]:
    """Resolve the deployed GateSpec property and decode its required fields."""
    gate = gate_resource(template)
    if gate is None:
        return ["FAIL spec_resolution: the gate resource is not in the template"], None
    rendered = _resolve(gate[1].get("Properties", {}).get("GateSpec"), template, lookups)
    try:
        document = cast("dict[str, Any]", json.loads(rendered))
    except ValueError:
        return ["FAIL spec_resolution: GateSpec is not resolvable"], None
    unresolved = [
        f"FAIL spec_resolution: {name} is not resolvable"
        for name in _REQUIRED_SPEC_FIELDS
        if _UNRESOLVED in json.dumps(document.get(name, _UNRESOLVED))
    ]
    if unresolved:
        return unresolved, None

    # Only the required fields are decoded. The deployed GateSpec also names each evaluator through
    # `Fn::GetAtt` on its evaluator resource, which no read can resolve, and the provider already
    # validated the whole spec when it parsed it. ResultGroupCustodyV1 repeats GateSpec's own
    # result-group predicates, so a malformed custody field is refused with the gate's message.
    def typed(name: str, value: object) -> bool:
        if name == "result_retention_days":
            return isinstance(value, int) and not isinstance(value, bool)
        return isinstance(value, str)

    mistyped = [
        f"FAIL spec_resolution: {name} is not resolvable"
        for name in _REQUIRED_SPEC_FIELDS
        if not typed(name, document.get(name))
    ]
    if mistyped:
        return mistyped, None
    try:
        custody = ResultGroupCustodyV1(
            result_log_group_name=document["result_log_group_name"],
            result_log_group_arn=document["result_log_group_arn"],
            result_retention_days=document["result_retention_days"],
            ingress_subscription_filter_name=document["ingress_subscription_filter_name"],
            ingress_function_arn=document["ingress_function_arn"],
        )
    except EvaluationGateSpecError as error:
        return [f"FAIL spec_resolution: {error}"], None
    resolved = ResolvedSpec(
        custody, document["execution_role_arn"], document["release_parameter_name"]
    )
    return ["PASS spec_resolution"], resolved


# --- the bound checks ---------------------------------------------------------------------------


def _group_targets(arn: str) -> tuple[str, ...]:
    return (arn, f"{arn}:*", f"{arn}:log-stream:{_PROBE_STREAM}")


def _holders(roles: Iterable[Role], allowed: Allowed, calls: Sequence[Simulation]) -> list[Role]:
    return [role for role in roles if any(allowed(role, call) for call in calls)]


def check_reader_bound(roles: Sequence[Role], allowed: Allowed, arn: str) -> str:
    """No role may read the result group's events."""
    calls = [Simulation(a, r) for a in READ for r in _group_targets(arn)]
    readers = _holders(roles, allowed, calls)
    if readers:
        return f"FAIL reader_bound: {_plural(len(readers), 'role')} may read the result group"
    return "PASS reader_bound"


def check_writer_bound(
    roles: Sequence[Role], allowed: Allowed, arn: str, execution_role_arn: str
) -> list[str]:
    """Exactly the execution role may write the result group, and it may do all three writes."""
    stream_calls = [Simulation(a, r) for a in WRITE_STREAM for r in _group_targets(arn)]
    create_calls = [Simulation(CREATE_GROUP, r) for r in (arn, f"{arn}:*")]
    writers = _holders(roles, allowed, [*stream_calls, *create_calls])
    execution = [role for role in roles if role.arn == execution_role_arn]
    lines: list[str] = []
    if any(role.arn != execution_role_arn for role in writers):
        lines.append(
            f"FAIL writer_bound: {_plural(len(writers), 'role')} may write the result group; "
            "expected only the execution role"
        )
    complete = len(execution) == 1 and all(
        any(allowed(execution[0], call) for call in calls)
        for calls in (
            [Simulation(WRITE_STREAM[0], r) for r in _group_targets(arn)],
            [Simulation(WRITE_STREAM[1], r) for r in _group_targets(arn)],
            create_calls,
        )
    )
    if not complete:
        lines.append("FAIL writer_bound: the execution role may not write the result group")
    return lines or ["PASS writer_bound"]


def check_custody_bound(roles: Sequence[Role], allowed: Allowed, arn: str) -> str:
    """No role may change or export the result group."""
    calls = [Simulation(a, r) for a in CUSTODY_SCOPED for r in (arn, f"{arn}:*")]
    holders = _holders(roles, allowed, calls)
    if holders:
        count = _plural(len(holders), "role")
        return f"FAIL custody_bound: {count} may change or export the result group"
    return "PASS custody_bound"


def check_unscoped_bound(roles: Sequence[Role], allowed: Allowed) -> str:
    """No role may create or change an account or resource Logs policy, on any resource."""
    holders = _holders(roles, allowed, [Simulation(a, "*") for a in CUSTODY_UNSCOPED])
    if holders:
        names = ", ".join(sorted(role.short_name for role in holders))
        return (
            f"FAIL unscoped_bound: {_plural(len(holders), 'role')} may create or change an account "
            f"or resource Logs policy ({names})"
        )
    return "PASS unscoped_bound"


def invoke_simulations(function_arn: str, region: str, account: str) -> list[Simulation]:
    """Each invocation-source action against one function, as the service authorizes it."""
    calls: list[Simulation] = []
    for action in INVOKE_SOURCE:
        if action == "lambda:CreateEventSourceMapping":
            resource = "*"
        elif action == "lambda:UpdateEventSourceMapping":
            resource = f"arn:aws:lambda:{region}:{account}:{_FICTIONAL_MAPPING}"
        else:
            resource = function_arn
        calls.append(Simulation(action, resource, function_arn))
    return calls


def check_invoke_bound(
    roles: Sequence[Role], allowed: Allowed, function_arns: Sequence[str], region: str, account: str
) -> str:
    """No role may attach an invocation source to, or change, the result ingress or a gate."""
    if not function_arns:
        return "FAIL invoke_bound: the result ingress or gate function could not be resolved"
    calls = [call for arn in function_arns for call in invoke_simulations(arn, region, account)]
    holders = _holders(roles, allowed, calls)
    if holders:
        return (
            f"FAIL invoke_bound: {_plural(len(holders), 'role')} may attach an invocation source "
            "to, or change, the result ingress or gate"
        )
    return "PASS invoke_bound"


def resource_policy_functions(template: Mapping[str, Any]) -> list[str]:
    """Logical ids of the functions each `AWS::Lambda::ResourcePolicy` protects."""
    resources = cast("Mapping[str, Mapping[str, Any]]", template.get("Resources", {}))
    found: list[str] = []
    for resource in resources.values():
        if resource.get("Type") != "AWS::Lambda::ResourcePolicy":
            continue
        target = resource.get("Properties", {}).get("ResourceArn", {})
        get_att = target.get("Fn::GetAtt") if isinstance(target, Mapping) else None
        if isinstance(get_att, list) and len(get_att) == 2 and get_att[1] == "Arn":  # noqa: PLR2004
            found.append(str(get_att[0]))
    return found


# --- custody, wiring, report --------------------------------------------------------------------


def check_custody(findings: Sequence[GateFinding]) -> str:
    """Pass when the gate's own readback finds nothing on the result group."""
    if findings:
        return "FAIL custody: " + ", ".join(str(finding.assertion) for finding in findings)
    return "PASS custody"


def check_wiring(
    record: EvaluationReleaseV1 | None, state: OnlineEvaluationConfigState, gate_config_id: str
) -> list[str]:
    """Pass when the release record and the configuration name one enabled evaluation."""
    lines: list[str] = []
    if record is None or not record.enabled:
        # A disabled record names no configuration or evaluators, so nothing else can be compared.
        lines.append("FAIL wiring: the release record is not enabled")
    else:
        if record.config_id != gate_config_id:
            lines.append("FAIL wiring: record config_id does not equal the gate configuration")
        if {e.evaluator_id for e in record.evaluators} != set(state.evaluator_ids):
            lines.append("FAIL wiring: record and configuration evaluator sets differ")
    if (
        state.status is not OnlineEvaluationStatus.ACTIVE
        or state.execution_status is not ExecutionStatus.ENABLED
    ):
        lines.append("FAIL wiring: the configuration is not ACTIVE and ENABLED")
    return lines or ["PASS wiring"]


def report_unwatched(roles: Sequence[Role], allowed: Allowed, arn: str) -> str:
    """Report-only: how many roles hold each unwatched log-group write action on the group."""
    counts = {
        action: len(_holders(roles, allowed, [Simulation(action, r) for r in (arn, f"{arn}:*")]))
        for action in UNWATCHED_LOGS_ACTIONS
    }
    held = [f"{action}={count}" for action, count in counts.items() if count]
    return "REPORT unwatched_logs_actions: " + (", ".join(held) if held else "none")


# --- orchestration ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """The operator's target and the deploy configuration the run must match."""

    environment: str
    region: str
    expected_account: str
    stacks: tuple[str, ...]
    config_flags: str
    expected_config_flags: str


@dataclass(frozen=True, slots=True)
class Lookups:
    """Every AWS read the probe makes, as plain callables."""

    caller_account: Callable[[], str]
    stack_status: Callable[[str], str]
    template: Callable[[str], Mapping[str, Any]]
    spec_lookups: Callable[[str], SpecLookups]
    roles: Callable[[Sequence[str]], list[Role]]
    allowed: Allowed
    function_arn: Callable[[str, str], str]
    gate_config_id: Callable[[str, str], str]
    readback: Callable[[ResultGroupCustodyV1], Sequence[GateFinding]]
    release: Callable[[str], EvaluationReleaseV1 | None]
    config_state: Callable[[str], OnlineEvaluationConfigState]


def run(request: ProbeRequest, lookups: Lookups) -> tuple[list[str], int]:
    """Run the checks in order; a failed prerequisite stops the run."""
    lines = [
        check_target(
            request.environment, request.region, request.expected_account, lookups.caller_account
        )
    ]
    lines += (
        []
        if lines[-1].startswith("FAIL")
        else check_config_flags(request.config_flags, request.expected_config_flags)
    )
    if any(line.startswith("FAIL") for line in lines):
        return lines, 1
    lines += check_stack_set(request.stacks, lookups.stack_status, request.environment)
    if any(line.startswith("FAIL") for line in lines):
        return lines, 1
    v4_stack = f"Adlytics-EvaluationV4-{request.environment}"
    link_stack = f"Adlytics-EvaluationResultLinkV3-{request.environment}"
    v4_template = lookups.template(v4_stack)
    resolution, spec = check_spec_resolution(v4_template, lookups.spec_lookups(v4_stack))
    lines += resolution
    if spec is None:
        return lines, 1
    roles = lookups.roles(request.stacks)
    arn = spec.custody.result_log_group_arn
    lines.append(check_reader_bound(roles, lookups.allowed, arn))
    lines += check_writer_bound(roles, lookups.allowed, arn, spec.execution_role_arn)
    lines.append(check_custody_bound(roles, lookups.allowed, arn))
    lines.append(check_unscoped_bound(roles, lookups.allowed))
    functions = [
        lookups.function_arn(stack, logical_id)
        for stack in (link_stack, v4_stack)
        for logical_id in resource_policy_functions(lookups.template(stack))
    ]
    lines.append(
        check_invoke_bound(
            roles, lookups.allowed, functions, request.region, request.expected_account
        )
    )
    lines.append(check_custody(lookups.readback(spec.custody)))
    gate = gate_resource(v4_template)
    config_id = lookups.gate_config_id(v4_stack, gate[0] if gate else "")
    lines += check_wiring(
        lookups.release(spec.release_parameter_name), lookups.config_state(config_id), config_id
    )
    status = 1 if any(line.startswith("FAIL") for line in lines) else 0
    lines.append(report_unwatched(roles, lookups.allowed, arn))
    return lines, status


# --- AWS wiring (not unit tested; every call is a read) -----------------------------------------


def _decision_allowed(result: Mapping[str, Any]) -> bool:
    specific = cast("list[Mapping[str, Any]]", result.get("ResourceSpecificResults", []))
    if specific:
        return any(entry.get("EvalResourceDecision") == "allowed" for entry in specific)
    return result.get("EvalDecision") == "allowed"


# Every action one service's checks simulate. A role is simulated once per resource and context with
# the whole batch of its service, instead of once per action, so a run stays within IAM's request
# rate; each action's decision is still read from its own evaluation result.
SIMULATION_BATCHES: Mapping[str, tuple[str, ...]] = {
    "logs": tuple(
        dict.fromkeys(
            (*READ, *WRITE_STREAM, CREATE_GROUP, *CUSTODY_SCOPED, *CUSTODY_UNSCOPED)
            + UNWATCHED_LOGS_ACTIONS
        )
    ),
    "lambda": INVOKE_SOURCE,
}


def decisions_by_action(results: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
    """Map each simulated action to whether it is allowed, from one batched simulation."""
    return {str(result["EvalActionName"]): _decision_allowed(result) for result in results}


def _aws_lookups(profile: str, region: str) -> Lookups:  # pragma: no cover
    import boto3

    from adapters.outbound.agentcore.online_evaluation_control import (
        config_state,
    )
    from adapters.outbound.cloudformation_stack import stack_resource_summaries
    from adapters.outbound.logs.evaluation_result_group import (
        CloudWatchResultGroupReadback,
    )
    from adapters.outbound.ssm.evaluation_release import (
        SsmEvaluationReleaseReader,
    )

    from botocore.config import Config

    session = boto3.Session(profile_name=profile, region_name=region)
    cfn, ssm = session.client("cloudformation"), session.client("ssm")
    iam = session.client("iam", config=Config(retries={"mode": "adaptive", "max_attempts": 10}))
    account = session.client("sts").get_caller_identity()["Account"]
    cache: dict[tuple[str, str, str, str | None], dict[str, bool]] = {}

    def stack_status(name: str) -> str:
        # aws-pagination: an exact StackName returns one stack.
        return str(cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"])

    def template(name: str) -> Mapping[str, Any]:
        body = cfn.get_template(StackName=name, TemplateStage="Original")["TemplateBody"]
        return cast("Mapping[str, Any]", json.loads(body) if isinstance(body, str) else body)

    def role_arn_in(stack: str) -> Callable[[str], str]:
        def resolve(logical_id: str) -> str:
            physical = cfn.describe_stack_resource(StackName=stack, LogicalResourceId=logical_id)
            name = physical["StackResourceDetail"]["PhysicalResourceId"]
            return str(iam.get_role(RoleName=name)["Role"]["Arn"])

        return resolve

    def spec_lookups(stack: str) -> SpecLookups:
        return SpecLookups(
            ssm_value=lambda name: str(ssm.get_parameter(Name=name)["Parameter"]["Value"]),
            pseudo={
                "AWS::Partition": "aws",
                "AWS::Region": region,
                "AWS::AccountId": account,
                "AWS::URLSuffix": "amazonaws.com",
            },
            role_arn=role_arn_in(stack),
        )

    def roles(stacks: Sequence[str]) -> list[Role]:
        found: list[Role] = []
        for stack in stacks:
            for resource in stack_resource_summaries(cfn, stack):
                if resource["ResourceType"] != "AWS::IAM::Role":
                    continue
                arn = iam.get_role(RoleName=resource["PhysicalResourceId"])["Role"]["Arn"]
                found.append(Role(role_short_name(stack, resource["LogicalResourceId"]), str(arn)))
        return found

    def allowed(role: Role, call: Simulation) -> bool:
        service = call.action.partition(":")[0]
        batch = SIMULATION_BATCHES[service]
        key = (role.arn, service, call.resource, call.function_arn)
        if key not in cache:
            context = (
                [
                    {
                        "ContextKeyName": "lambda:FunctionArn",
                        "ContextKeyValues": [call.function_arn],
                        "ContextKeyType": "string",
                    }
                ]
                if call.function_arn
                else []
            )
            results = iam.simulate_principal_policy(
                PolicySourceArn=role.arn,
                ActionNames=list(batch),
                ResourceArns=[call.resource],
                ContextEntries=cast("Any", context),
            )["EvaluationResults"]
            cache[key] = decisions_by_action(cast("list[Mapping[str, Any]]", results))
        return cache[key][call.action]

    def function_arn(stack: str, logical_id: str) -> str:
        detail = cfn.describe_stack_resource(StackName=stack, LogicalResourceId=logical_id)
        name = detail["StackResourceDetail"]["PhysicalResourceId"]
        return f"arn:aws:lambda:{region}:{account}:function:{name}"

    def gate_config_id(stack: str, logical_id: str) -> str:
        detail = cfn.describe_stack_resource(StackName=stack, LogicalResourceId=logical_id)
        return str(detail["StackResourceDetail"]["PhysicalResourceId"])

    control = session.client("bedrock-agentcore-control")
    return Lookups(
        caller_account=lambda: str(account),
        stack_status=stack_status,
        template=template,
        spec_lookups=spec_lookups,
        roles=roles,
        allowed=allowed,
        function_arn=function_arn,
        gate_config_id=gate_config_id,
        readback=lambda custody: CloudWatchResultGroupReadback(session.client("logs")).findings(
            custody
        ),
        release=lambda name: SsmEvaluationReleaseReader((name,), ssm).read()[0],
        config_state=lambda config_id: config_state(
            control.get_online_evaluation_config(onlineEvaluationConfigId=config_id)
        ),
    )


def _arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--family", required=True, choices=("v4",))
    parser.add_argument("--stacks", required=True, help="comma-separated `cdk list` output")
    parser.add_argument("--config-flags", required=True)
    parser.add_argument("--expected-config-flags", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - AWS entry point
    """Run the acceptance and print one line per check."""
    args = _arguments(sys.argv[1:] if argv is None else argv)
    request = ProbeRequest(
        environment=args.environment,
        region=args.region,
        expected_account=args.expected_account,
        stacks=tuple(name for name in args.stacks.split(",") if name),
        config_flags=args.config_flags,
        expected_config_flags=args.expected_config_flags,
    )
    if request.environment != ENVIRONMENT or request.region != REGION:
        print(
            check_target(request.environment, request.region, request.expected_account, lambda: "")
        )
        return 1
    lines, status = run(request, _aws_lookups(args.profile, args.region))
    for line in lines:
        print(line)
    return status


if __name__ == "__main__":
    sys.exit(main())
