# SPDX-License-Identifier: MIT-0
"""The two-session bootstrap every Stage-1 operator entry point performs (write discipline §3.2).

The ambient (developer-profile) session may create only STS, SSM, and CloudFormation clients: it
proves the caller account, resolves exactly the typed Config-owned parameter paths the operator
names, proves that the named DataV2 and Ingestion stacks own each parameter and each resolved
resource,
requires every ARN's partition, account, and Region to match the explicit values, and only then
assumes the one verified role for exactly 900 seconds. The operator constructs every working client
from the returned temporary-credential session and never from the ambient one.

Nothing here enumerates account resources or derives a name as a fallback: a value is accepted only
because the named stack owns it.
"""

from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast

import boto3

from adapters.outbound.cloudformation_stack import stack_resource_summaries
from domain.shared_kernel.errors import DependencyError, ErrorCode

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_cloudformation.client import CloudFormationClient
    from types_boto3_ssm.client import SSMClient
    from types_boto3_sts.client import STSClient

__all__ = [
    "ASSUMED_SESSION_SECONDS",
    "BOOTSTRAP_FAILED",
    "PUBLICATIONS_TABLE_KEY",
    "BootstrapRequest",
    "BootstrapResult",
    "bootstrap_operator_session",
    "parameter_path",
    "session_name",
]

ASSUMED_SESSION_SECONDS: Final = 900
PUBLICATIONS_TABLE_KEY: Final = "semanticV2PublicationsTableName"
BOOTSTRAP_FAILED: Final = ErrorCode("STAGE1_OPERATOR_BOOTSTRAP", 1)

_ROOT: Final = pathlib.Path(__file__).resolve().parents[3]
_PARAMETER_CONTRACT: Final = _ROOT / "contracts" / "generated" / "parameter-paths.json"
_STACK_BY_PRODUCER: Final = {
    "DataV2Stack": "Adlytics-DataV2-{environment}",
    "IngestionStack": "Adlytics-Ingestion-{environment}",
}
_ACCOUNT: Final = re.compile(r"\A\d{12}\Z")
_SESSION_NAME_SAFE: Final = re.compile(r"[^A-Za-z0-9+=,.@_-]")
_SESSION_NAME_MAX: Final = 64
_ARN_MIN_PARTS: Final = 6
_UNQUALIFIED_FUNCTION_ARN_PARTS: Final = 7


def _refuse(description: str, **context: object) -> DependencyError:
    return DependencyError(BOOTSTRAP_FAILED, description, context=context)


def parameter_path(environment: str, key: str) -> str:
    """Render one typed parameter path from the generated Config contract; unknown keys refuse."""
    document = json.loads(_PARAMETER_CONTRACT.read_text(encoding="utf-8"))
    parameters = cast("Mapping[str, object]", document["parameters"])
    raw = parameters.get(key)
    if not isinstance(raw, Mapping):
        raise _refuse("The operator names a parameter outside the typed contract.", key=key)
    entry = cast("Mapping[str, object]", raw)
    area, name = entry.get("area"), entry.get("name")
    if not isinstance(area, str) or not isinstance(name, str):
        raise _refuse("The parameter contract entry is malformed.", key=key)
    return f"/adlytics/{environment}/{area}/{name}"


def _producer_stack(environment: str, key: str) -> str:
    document = json.loads(_PARAMETER_CONTRACT.read_text(encoding="utf-8"))
    parameters = cast("Mapping[str, Mapping[str, object]]", document["parameters"])
    producer = parameters[key].get("producedBy")
    template = _STACK_BY_PRODUCER.get(cast("str", producer))
    if template is None:
        raise _refuse(
            "The parameter is produced by a stack this bootstrap does not verify.", key=key
        )
    return template.format(environment=environment)


def session_name(operation_id: str) -> str:
    """Derive the bounded role session name from the operator's operation id."""
    safe = _SESSION_NAME_SAFE.sub("-", operation_id.strip())[: _SESSION_NAME_MAX - len("adlytics-")]
    if not safe:
        raise _refuse("The operation id yields no usable session name.")
    return f"adlytics-{safe}"


@dataclass(frozen=True, slots=True)
class _Arn:
    partition: str
    service: str
    region: str
    account: str
    resource: str
    parts: int

    @classmethod
    def parse(cls, value: str, key: str) -> _Arn:
        parts = value.split(":")
        if len(parts) < _ARN_MIN_PARTS or parts[0] != "arn" or not parts[1]:
            raise _refuse("A resolved parameter is not an ARN.", key=key)
        return cls(parts[1], parts[2], parts[3], parts[4], ":".join(parts[5:]), len(parts))


@dataclass(frozen=True, slots=True)
class BootstrapRequest:
    """Explicit operator identity and the exact parameter set one entry point may resolve."""

    profile: str
    region: str
    expected_account: str
    environment: str
    operation_id: str
    role_key: str
    parameter_keys: tuple[str, ...]
    stack_names: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.profile.strip() or self.profile.strip().casefold() == "default":
            raise ValueError("an explicit nondefault AWS profile is required.")
        if not self.region.strip():
            raise ValueError("an explicit Region is required.")
        if _ACCOUNT.match(self.expected_account) is None:
            raise ValueError("the expected account must be exactly twelve decimal digits.")
        if not self.environment.strip():
            raise ValueError("an explicit environment is required.")
        if not self.operation_id.strip():
            raise ValueError("an explicit operation id is required.")
        if self.role_key not in self.parameter_keys:
            raise ValueError("the assumed role's parameter key must be among the resolved keys.")
        if PUBLICATIONS_TABLE_KEY not in self.parameter_keys:
            raise ValueError("every Stage-1 operator resolves the typed publications table.")
        if len(set(self.parameter_keys)) != len(self.parameter_keys):
            raise ValueError("parameter keys must be unique.")
        object.__setattr__(
            self,
            "stack_names",
            (
                f"Adlytics-DataV2-{self.environment}",
                f"Adlytics-Ingestion-{self.environment}",
            ),
        )


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """The verified parameter values and the one temporary-credential working session."""

    values: Mapping[str, str]
    assumed: boto3.Session
    role_arn: str


@dataclass(frozen=True, slots=True)
class _Owned:
    """Physical ids the named stacks own, by CloudFormation resource type."""

    by_type: Mapping[str, frozenset[str]]

    def owns(self, resource_type: str, physical_id: str) -> bool:
        return physical_id in self.by_type.get(resource_type, frozenset())


def _describe(cloudformation: CloudFormationClient, stack_name: str) -> _Owned:
    # TD-123: every ListStackResources page, read to the end. DescribeStackResources stops at
    # 100 (Adlytics-Ingestion-dev holds 118), so an owned parameter past the 100th was refused
    # as unowned. Any page error refuses the whole stack: no partial ownership set is used.
    try:
        resources = stack_resource_summaries(cloudformation, stack_name)
    except Exception as error:
        raise _refuse(
            "The named stack could not be described.",
            cause=type(error).__name__,
            stack=stack_name,
        ) from None
    by_type: dict[str, set[str]] = {}
    for item in resources:
        resource_type = str(item.get("ResourceType", ""))
        physical = str(item.get("PhysicalResourceId", ""))
        if resource_type and physical:
            by_type.setdefault(resource_type, set()).add(physical)
    return _Owned({kind: frozenset(ids) for kind, ids in by_type.items()})


def _read_parameter(ssm: SSMClient, path: str, key: str) -> str:
    try:
        value = str(ssm.get_parameter(Name=path)["Parameter"].get("Value", ""))
    except Exception as error:
        raise _refuse(
            "A typed parameter could not be read.", cause=type(error).__name__, key=key
        ) from None
    if not value.strip():
        raise _refuse("A typed parameter resolved to nothing.", key=key)
    return value.strip()


def _verify_arn_scope(arn: _Arn, request: BootstrapRequest, key: str) -> None:
    if arn.partition != "aws":
        raise _refuse("A resolved ARN names a foreign partition.", key=key)
    if arn.account != request.expected_account:
        raise _refuse("A resolved ARN names another account.", key=key)
    if arn.service != "iam" and arn.region != request.region:
        raise _refuse("A resolved ARN names another Region.", key=key)


@dataclass(frozen=True, slots=True)
class _ResolvedParameter:
    key: str
    value: str
    owned: _Owned


def _verify_resource_ownership(resolved: _ResolvedParameter, request: BootstrapRequest) -> None:
    """Prove the producing stack owns the physical resource the parameter names, by its kind."""
    key, value, owned = resolved.key, resolved.value, resolved.owned
    if key == PUBLICATIONS_TABLE_KEY:
        if not owned.owns("AWS::DynamoDB::Table", value):
            raise _refuse("The DataV2 stack does not own the publications table.", key=key)
        return
    arn = _Arn.parse(value, key)
    _verify_arn_scope(arn, request, key)
    if arn.service == "iam":
        if not arn.resource.startswith("role/") or not owned.owns(
            "AWS::IAM::Role", arn.resource.removeprefix("role/")
        ):
            raise _refuse("The Ingestion stack does not own the named role.", key=key)
    elif arn.service == "lambda":
        if arn.parts != _UNQUALIFIED_FUNCTION_ARN_PARTS or not arn.resource.startswith("function:"):
            raise _refuse("The function ARN must be exact and unqualified.", key=key)
        if not owned.owns("AWS::Lambda::Function", arn.resource.removeprefix("function:")):
            raise _refuse("The Ingestion stack does not own the named function.", key=key)
    elif arn.service == "sqs":
        expected_url = f"https://sqs.{arn.region}.amazonaws.com/{arn.account}/{arn.resource}"
        if not owned.owns("AWS::SQS::Queue", expected_url):
            raise _refuse("The Ingestion stack does not own the named queue.", key=key)
    else:
        raise _refuse("A resolved ARN names a service this bootstrap does not verify.", key=key)


def _assume(sts: STSClient, role_arn: str, request: BootstrapRequest) -> boto3.Session:
    try:
        credentials = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name(request.operation_id),
            DurationSeconds=ASSUMED_SESSION_SECONDS,
        )["Credentials"]
    except Exception as error:
        raise _refuse(
            "The verified operator role could not be assumed.", cause=type(error).__name__
        ) from None
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=request.region,
    )


def bootstrap_operator_session(
    ambient: boto3.Session, request: BootstrapRequest
) -> BootstrapResult:
    """Run the complete §3.2 ambient bootstrap and return the assumed working session.

    The caller has already proved the caller account through this ambient session's STS client
    (so the identity check is itself instrumented); this function re-reads it rather than
    trusting the caller, because account equality is what authorizes every later read.
    """
    sts: STSClient = ambient.client("sts")
    try:
        account = str(sts.get_caller_identity().get("Account", ""))
    except Exception as error:
        raise _refuse(
            "The caller identity could not be read.", cause=type(error).__name__
        ) from None
    if account != request.expected_account:
        raise _refuse("The caller account differs from the explicitly expected account.")

    ssm: SSMClient = ambient.client("ssm")
    cloudformation: CloudFormationClient = ambient.client("cloudformation")
    owned_by_stack = {name: _describe(cloudformation, name) for name in request.stack_names}

    values: dict[str, str] = {}
    seen_paths: set[str] = set()
    for key in request.parameter_keys:
        path = parameter_path(request.environment, key)
        if path in seen_paths:
            raise _refuse("Two parameter keys resolve to one path.", key=key)
        seen_paths.add(path)
        stack_name = _producer_stack(request.environment, key)
        owned = owned_by_stack[stack_name]
        if not owned.owns("AWS::SSM::Parameter", path):
            raise _refuse("The producing stack does not own the parameter path.", key=key)
        value = _read_parameter(ssm, path, key)
        _verify_resource_ownership(_ResolvedParameter(key, value, owned), request)
        values[key] = value

    return BootstrapResult(
        values=values,
        assumed=_assume(sts, values[request.role_key], request),
        role_arn=values[request.role_key],
    )
