# SPDX-License-Identifier: MIT-0
"""The §3.2 two-session bootstrap and the invoke-only provider-fence requeue operator."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from adapters.jobs import stage1_operator_bootstrap as bootstrap
from adapters.jobs.requeue_provider_fence import __main__ as requeue_cli
from domain.shared_kernel.errors import DependencyError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

ACCOUNT = "000000000000"
REGION = "us-east-1"
ENV = "dev"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/fictional-operator-role"
FUNCTION_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:fictional-broker"
TABLE = "fictional-publications"
PATHS = {
    "providerFenceRecoveryOperatorRoleArn": (
        f"/adlytics/{ENV}/ingestion/provider-fence-recovery-operator-role-arn"
    ),
    "providerFenceRecoveryFunctionArn": (
        f"/adlytics/{ENV}/ingestion/provider-fence-recovery-function-arn"
    ),
    "semanticV2PublicationsTableName": f"/adlytics/{ENV}/data/v2/semantic-publications-table",
}


@dataclass
class _Client:
    name: str
    world: _World

    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": self.world.account}

    def get_parameter(self, *, Name: str) -> dict[str, Any]:  # noqa: N803 - SDK spelling
        return {"Parameter": {"Value": self.world.parameters[Name]}}

    def get_paginator(self, operation: str) -> _Client:
        assert operation == "list_stack_resources", "TD-123: no DescribeStackResources"
        return self

    def paginate(self, *, StackName: str) -> Any:  # noqa: N803 - SDK spelling
        # ListStackResources pages of `page_size`; a page named in `failing_pages` raises.
        resources = self.world.stacks[StackName]
        size = self.world.page_size
        for index, start in enumerate(range(0, len(resources), size)):
            if (StackName, index) in self.world.failing_pages:
                raise RuntimeError("injected page failure")
            yield {"StackResourceSummaries": resources[start : start + size]}

    def assume_role(self, **request: Any) -> dict[str, Any]:
        self.world.assumed.append(request)
        return {
            "Credentials": {
                "AccessKeyId": "AKIAFICTIONAL",
                # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
                "SecretAccessKey": "fictional",
                "SessionToken": "fictional-token",
            }
        }

    def invoke(self, **request: Any) -> dict[str, Any]:
        self.world.invocations.append(request)
        return self.world.invoke_response


@dataclass
class _World:
    account: str = ACCOUNT
    parameters: dict[str, str] = field(default_factory=dict[str, str])
    stacks: dict[str, list[dict[str, str]]] = field(default_factory=dict[str, list[dict[str, str]]])
    assumed: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    invocations: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    clients: list[str] = field(default_factory=list[str])
    invoke_response: dict[str, Any] = field(default_factory=dict[str, Any])
    page_size: int = 100
    """ListStackResources returns at most 100 summaries a page."""
    failing_pages: set[tuple[str, int]] = field(default_factory=set[tuple[str, int]])

    def session(self) -> _Session:
        return _Session(self)


@dataclass
class _Session:
    world: _World
    _session: object = field(default_factory=object)

    def client(self, name: str) -> _Client:
        self.world.clients.append(name)
        return _Client(name, self.world)


def _owned_world() -> _World:
    world = _World()
    world.parameters = {
        PATHS["providerFenceRecoveryOperatorRoleArn"]: ROLE_ARN,
        PATHS["providerFenceRecoveryFunctionArn"]: FUNCTION_ARN,
        PATHS["semanticV2PublicationsTableName"]: TABLE,
    }
    world.stacks = {
        f"Adlytics-DataV2-{ENV}": [
            {"ResourceType": "AWS::DynamoDB::Table", "PhysicalResourceId": TABLE},
            {
                "ResourceType": "AWS::SSM::Parameter",
                "PhysicalResourceId": PATHS["semanticV2PublicationsTableName"],
            },
        ],
        f"Adlytics-Ingestion-{ENV}": [
            {"ResourceType": "AWS::IAM::Role", "PhysicalResourceId": "fictional-operator-role"},
            {"ResourceType": "AWS::Lambda::Function", "PhysicalResourceId": "fictional-broker"},
            {
                "ResourceType": "AWS::SSM::Parameter",
                "PhysicalResourceId": PATHS["providerFenceRecoveryOperatorRoleArn"],
            },
            {
                "ResourceType": "AWS::SSM::Parameter",
                "PhysicalResourceId": PATHS["providerFenceRecoveryFunctionArn"],
            },
        ],
    }
    return world


def _request(**overrides: Any) -> bootstrap.BootstrapRequest:
    values: dict[str, Any] = {
        "profile": "explicit-profile",
        "region": REGION,
        "expected_account": ACCOUNT,
        "environment": ENV,
        "operation_id": "op-1",
        "role_key": "providerFenceRecoveryOperatorRoleArn",
        "parameter_keys": tuple(PATHS),
    }
    values.update(overrides)
    return bootstrap.BootstrapRequest(**values)


# --- Bootstrap ------------------------------------------------------------------------------------


def test_parameter_paths_come_from_the_generated_contract_only() -> None:
    assert (
        bootstrap.parameter_path(ENV, "providerFenceRecoveryFunctionArn")
        == (PATHS["providerFenceRecoveryFunctionArn"])
    )
    with pytest.raises(DependencyError):
        bootstrap.parameter_path(ENV, "notAContractKey")


def test_owned_parameters_resolve_and_the_verified_role_is_assumed_for_900_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _owned_world()
    sessions: list[dict[str, Any]] = []

    def assumed_session(**kwargs: Any) -> object:
        sessions.append(kwargs)
        return object()

    monkeypatch.setattr(bootstrap.boto3, "Session", assumed_session)
    result = bootstrap.bootstrap_operator_session(world.session(), _request())  # type: ignore[arg-type]

    assert result.values == {
        "providerFenceRecoveryOperatorRoleArn": ROLE_ARN,
        "providerFenceRecoveryFunctionArn": FUNCTION_ARN,
        "semanticV2PublicationsTableName": TABLE,
    }
    assert world.clients == ["sts", "ssm", "cloudformation"], "ambient session: STS, SSM, CFN only"
    assert world.assumed == [
        {"RoleArn": ROLE_ARN, "RoleSessionName": "adlytics-op-1", "DurationSeconds": 900}
    ]
    assert sessions == [
        {
            "aws_access_key_id": "AKIAFICTIONAL",
            # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
            "aws_secret_access_key": "fictional",
            "aws_session_token": "fictional-token",
            "region_name": REGION,
        }
    ]


def _refuses(world: _World, request: bootstrap.BootstrapRequest | None = None) -> None:
    with pytest.raises(DependencyError) as caught:
        bootstrap.bootstrap_operator_session(world.session(), request or _request())  # type: ignore[arg-type]
    assert world.assumed == [], "nothing is assumed after a refusal"
    assert ACCOUNT not in str(caught.value.context)


def test_account_mismatch_refuses_before_any_parameter_read() -> None:
    world = _owned_world()
    world.account = "111111111111"
    _refuses(world)
    assert world.clients == ["sts"]


@pytest.mark.parametrize(
    "tamper",
    [
        "foreign_account_role",
        "other_region_function",
        "qualified_function",
        "unowned_role",
        "unowned_parameter",
        "blank_parameter",
        "unowned_table",
    ],
)
def test_ownership_partition_account_region_and_form_defects_refuse(tamper: str) -> None:
    world = _owned_world()
    role_path = PATHS["providerFenceRecoveryOperatorRoleArn"]
    function_path = PATHS["providerFenceRecoveryFunctionArn"]
    ingestion = world.stacks[f"Adlytics-Ingestion-{ENV}"]
    if tamper == "foreign_account_role":
        world.parameters[role_path] = ROLE_ARN.replace(ACCOUNT, "222222222222")
    elif tamper == "other_region_function":
        world.parameters[function_path] = FUNCTION_ARN.replace(REGION, "eu-west-1")
    elif tamper == "qualified_function":
        world.parameters[function_path] = f"{FUNCTION_ARN}:live"
    elif tamper == "unowned_role":
        ingestion[:] = [item for item in ingestion if item["ResourceType"] != "AWS::IAM::Role"]
    elif tamper == "unowned_parameter":
        ingestion[:] = [item for item in ingestion if item["PhysicalResourceId"] != role_path]
    elif tamper == "blank_parameter":
        world.parameters[role_path] = "   "
    else:
        world.stacks[f"Adlytics-DataV2-{ENV}"] = [
            item
            for item in world.stacks[f"Adlytics-DataV2-{ENV}"]
            if item["ResourceType"] != "AWS::DynamoDB::Table"
        ]
    _refuses(world)


def _past_the_first_page(world: _World) -> None:
    """TD-123: 101 unrelated resources first, so every owned Ingestion item is on page two."""
    ingestion = world.stacks[f"Adlytics-Ingestion-{ENV}"]
    filler = [
        {"ResourceType": "AWS::Lambda::Function", "PhysicalResourceId": f"fictional-filler-{n}"}
        for n in range(101)
    ]
    ingestion[:] = [*filler, *ingestion]


def test_a_parameter_owned_past_the_100th_stack_resource_is_found_on_a_later_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TD-123: DescribeStackResources stopped at 100 and refused an owned parameter as unowned."""
    world = _owned_world()
    _past_the_first_page(world)
    assert len(world.stacks[f"Adlytics-Ingestion-{ENV}"]) == 105

    def assumed_session(**_kwargs: object) -> object:
        return object()

    monkeypatch.setattr(bootstrap.boto3, "Session", assumed_session)
    result = bootstrap.bootstrap_operator_session(world.session(), _request())  # type: ignore[arg-type]
    assert result.role_arn == ROLE_ARN
    assert world.assumed[0]["RoleArn"] == ROLE_ARN
    # Paired control: the same stack read only as its first 100 resources (the old cap) refuses.
    capped = _owned_world()
    _past_the_first_page(capped)
    ingestion = capped.stacks[f"Adlytics-Ingestion-{ENV}"]
    ingestion[:] = ingestion[:100]
    _refuses(capped)


def test_a_failed_later_page_refuses_the_whole_stack_and_assumes_nothing() -> None:
    world = _owned_world()
    _past_the_first_page(world)
    world.failing_pages.add((f"Adlytics-Ingestion-{ENV}", 1))
    with pytest.raises(DependencyError, match="could not be described") as caught:
        bootstrap.bootstrap_operator_session(world.session(), _request())  # type: ignore[arg-type]
    assert caught.value.context["cause"] == "RuntimeError"
    assert world.assumed == []


def test_request_rejects_default_profile_short_account_and_missing_table_key() -> None:
    with pytest.raises(ValueError, match="nondefault"):
        _request(profile="default")
    with pytest.raises(ValueError, match="twelve"):
        _request(expected_account="1234")
    with pytest.raises(ValueError, match="publications table"):
        _request(parameter_keys=("providerFenceRecoveryOperatorRoleArn",))


# --- Requeue CLI ----------------------------------------------------------------------------------

_LIST_ARGS = [
    "list",
    "--profile",
    "explicit-profile",
    "--region",
    REGION,
    "--expected-account",
    ACCOUNT,
    "--env",
    ENV,
    "--limit",
    "10",
]


def _no_aws(**_kwargs: object) -> object:
    pytest.fail("argument validation must complete before any session exists")


@pytest.mark.parametrize(
    "argv",
    [
        [*_LIST_ARGS[:-2], "--limit", "0"],
        [*_LIST_ARGS[:-2], "--limit", "101"],
        [*_LIST_ARGS, "--cursor", "not-a-uuid"],
        [
            "requeue",
            *_LIST_ARGS[1:-2],
            "--operation-id",
            "op",
            "--recovery-ref",
            "x",
            "--resolution-reason",
            "r",
        ],
        [*_LIST_ARGS[:2], "default", *_LIST_ARGS[3:]],
    ],
    ids=["limit-0", "limit-101", "cursor-form", "recovery-ref-form", "default-profile"],
)
def test_invocation_defects_are_refused_before_any_session(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.setattr(requeue_cli.boto3, "Session", _no_aws)
    with pytest.raises(BaseException) as raised:
        requeue_cli.main(argv)
    assert isinstance(raised.value, SystemExit | Exception)


def _sessions(monkeypatch: pytest.MonkeyPatch, ambient: _World, assumed: _World) -> None:
    """One ``boto3.Session`` factory: profile-keyed calls are ambient, credential calls assumed.

    ``requeue_cli.boto3`` and ``bootstrap.boto3`` are the same module object, so one patch serves
    both and the fake distinguishes the two sessions by how they are constructed.
    """

    def factory(**kwargs: Any) -> _Session:
        return _Session(assumed if "aws_access_key_id" in kwargs else ambient)

    monkeypatch.setattr(bootstrap.boto3, "Session", factory)


def _quiet_telemetry(monkeypatch: pytest.MonkeyPatch, flushed: list[str]) -> None:
    def configure(_settings: object, *, session: object) -> bool:
        del session
        return False

    def ignore(*_args: object) -> None:
        return None

    monkeypatch.setattr(requeue_cli, "configure_tracing", configure)
    monkeypatch.setattr(requeue_cli, "report_operation", ignore)
    monkeypatch.setattr(requeue_cli, "report_error", ignore)
    monkeypatch.setattr(requeue_cli, "flush_metrics", lambda: flushed.append("metrics"))
    monkeypatch.setattr(requeue_cli, "flush_traces", lambda: flushed.append("traces"))


def test_list_invokes_only_the_verified_function_from_the_assumed_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _owned_world()
    world.invoke_response = {
        "StatusCode": 200,
        "Payload": io.BytesIO(
            json.dumps({"schema_version": "1", "mode": "list", "cases": []}).encode()
        ),
    }
    assumed_world = _World()
    flushed: list[str] = []
    _quiet_telemetry(monkeypatch, flushed)
    _sessions(monkeypatch, world, assumed_world)
    assumed_world.invoke_response = world.invoke_response

    code = requeue_cli.main(_LIST_ARGS)

    assert code == 0
    assert world.clients == ["sts", "ssm", "cloudformation"]
    assert assumed_world.clients == ["lambda"], "the working session creates only Lambda"
    (invocation,) = assumed_world.invocations
    assert invocation["FunctionName"] == FUNCTION_ARN
    assert invocation["InvocationType"] == "RequestResponse"
    assert json.loads(invocation["Payload"]) == {"schema_version": "1", "mode": "list", "limit": 10}
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": "1",
        "mode": "list",
        "cases": [],
    }
    assert flushed == ["metrics", "traces"]


def test_broker_error_codes_surface_as_closed_codes_and_exit_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _owned_world()
    assumed_world = _World()
    assumed_world.invoke_response = {
        "StatusCode": 200,
        "FunctionError": "Unhandled",
        "Payload": io.BytesIO(json.dumps({"errorMessage": "stale_case"}).encode()),
    }
    flushed: list[str] = []
    _quiet_telemetry(monkeypatch, flushed)
    _sessions(monkeypatch, world, assumed_world)

    code = requeue_cli.main(
        [
            "requeue",
            *_LIST_ARGS[1:-2],
            "--operation-id",
            "op-7",
            "--recovery-ref",
            "0d2f4c9e-7a1b-4c3d-9e8f-1a2b3c4d5e6f",
            "--resolution-reason",
            "operator reviewed",
        ]
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out) == {"mode": "requeue", "error_code": "stale_case"}
    assert world.assumed[0]["RoleSessionName"] == "adlytics-op-7"
