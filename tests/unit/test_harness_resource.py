# SPDX-License-Identifier: MIT-0
"""The custom resource that owns the managed Harness (ADR-077, amended 2026-09-26).

Driven through `provider.run` against a fake control-plane port, a recording responder and a
fake clock, so every branch runs with no AWS and no SDK mock. Each test asserts the one response
CloudFormation receives, because on CREATE_FAILED that response is the only diagnostic left.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
import requests
from botocore.exceptions import ClientError

from adapters.jobs.harness_resource import handler as handler_module
from adapters.jobs.harness_resource import provider as provider_module
from adapters.jobs.harness_resource.provider import (
    MAX_REASON_LENGTH,
    ProviderRuntime,
    RunRequest,
    run,
)
from adapters.outbound.agentcore.harness_control import (
    HARNESS_CONTROL_FAILED,
    TagChanges,
    create_request,
    delete_request,
    tag_changes,
    translated,
    update_request,
)
from adapters.outbound.cloudformation_response import HttpsCustomResourceResponder
from adapters.outbound.logs.log_group_retention import (
    LOG_RETENTION_FAILED,
    CloudWatchLogRetention,
)
from domain.operations.harness_resource import (
    HarnessResourceError,
    parse_spec,
)
from domain.shared_kernel.errors import DependencyError, InvariantError
from ports.outbound.custom_resource_response import CustomResourceResponse
from ports.outbound.harness_control import (
    CreateHarnessRequest,
    HarnessNotFoundError,
    HarnessState,
    HarnessStatus,
    ReplaceTagsRequest,
    UpdateHarnessRequest,
)
from ports.outbound.log_retention import RetainLogGroupRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-077")]

RESPONSE_URL = "https://cloudformation-custom-resource-response.example/presigned?sig=abc"
STACK_ID = "arn:aws:cloudformation:us-east-1:123456789012:stack/Adlytics-Harness-test/guid"
LOGICAL_ID = "ManagedHarness"
# A-TEST-LITERALS-001: a ServiceToken ARN; see docs/security-dispositions.md
SERVICE_TOKEN = "arn:aws:lambda:us-east-1:123456789012:function:provider"  # nosec B105
ROLE_ARN = "arn:aws:iam::123456789012:role/harness-execution"
MEMORY_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/adlytics_test-AbCdEfGhIj"
PROVIDER_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
    "token-vault/default/oauth2credentialprovider/adlytics-test-token-service"
)
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
SUFFIX = "AbCdE12345"
RUNTIME = "Zz9Yy8Xx7W"
RUNTIME_LOG_GROUP = f"/aws/bedrock-agentcore/runtimes/harness_adlytics_test_agent-{RUNTIME}-DEFAULT"


def spec_document(**overrides: object) -> dict[str, object]:
    """Build a spec in the stack's API shape, before any override (None removes a member)."""
    document: dict[str, object] = {
        "harnessName": "adlytics_test_agent",
        "executionRoleArn": ROLE_ARN,
        "authorizerConfiguration": {
            "customJWTAuthorizer": {
                "discoveryUrl": "https://issuer.example/v1/.well-known/openid-configuration",
                "allowedAudience": ["adlytics-test-harness"],
                "allowedClients": ["adlytics-test-conversation-route"],
            }
        },
        "model": {"bedrockModelConfig": {"modelId": "model-v1", "maxTokens": 16384}},
        "memory": {"agentCoreMemoryConfiguration": {"arn": MEMORY_ARN}},
        "tools": [
            {
                "type": "agentcore_gateway",
                "name": "adlytics",
                "config": {
                    "agentCoreGateway": {
                        "gatewayArn": "arn:aws:bedrock-agentcore:us-east-1:1:gateway/g",
                        "outboundAuth": {
                            "oauth": {
                                "providerArn": PROVIDER_ARN,
                                "scopes": ["adlytics-gateway"],
                                "grantType": "TOKEN_EXCHANGE",
                            }
                        },
                    }
                },
            }
        ],
        "allowedTools": ["@adlytics/catalog___catalog_list_metrics"],
        "systemPrompt": [{"text": 'Say "hello".\nThen publish.'}],
        "maxIterations": 24,
        "maxTokens": 16384,
        "timeoutSeconds": 180,
        "tags": {"Application": "adlytics-ai", "Environment": "test"},
    }
    document.update(overrides)
    return {key: value for key, value in document.items() if value is not None}


def properties(document: Mapping[str, object] | None = None, **extra: str) -> dict[str, str]:
    """ResourceProperties as CloudFormation delivers them: every value is text."""
    return {
        "ServiceToken": SERVICE_TOKEN,
        "HarnessSpec": json.dumps(spec_document() if document is None else document),
        **extra,
    }


def event(request_type: str, **overrides: object) -> dict[str, object]:
    """Build one callback envelope."""
    base: dict[str, object] = {
        "RequestType": request_type,
        "ServiceToken": SERVICE_TOKEN,
        "ResponseURL": RESPONSE_URL,
        "StackId": STACK_ID,
        "RequestId": "request-1",
        "LogicalResourceId": LOGICAL_ID,
        "ResourceType": "Custom::AgentCoreHarness",
        "ResourceProperties": properties(),
    }
    if request_type != "Create":
        base["PhysicalResourceId"] = f"adlytics_test_agent-{SUFFIX}"
    if request_type == "Update":
        base["OldResourceProperties"] = properties()
    base.update(overrides)
    return base


def expected_token(request_id: str, operation: str, stack_id: str = STACK_ID) -> str:
    material = "\n".join(
        ("adlytics-harness-resource/v1", stack_id, LOGICAL_ID, request_id, operation)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class FakeControl:
    """In-memory control plane: each Harness answers GetHarness from a scripted status list."""

    statuses: dict[str, list[HarnessStatus | None]] = field(
        default_factory=dict[str, list[HarnessStatus | None]]
    )
    """None in a script means GetHarness raises not-found from that poll on."""
    failure_reason: str = ""
    create_error: Exception | None = None
    delete_error: Exception | None = None
    get_error: Exception | None = None
    create_script: list[HarnessStatus | None] = field(
        default_factory=lambda: [HarnessStatus.CREATING, HarnessStatus.READY]
    )
    calls: list[tuple[str, object]] = field(default_factory=list[tuple[str, object]])

    @staticmethod
    def arn(harness_id: str) -> str:
        return f"arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/{harness_id}"

    runtime_arn: str | None = None
    """Overrides the runtime this Harness reports; None derives its own."""

    def _state(self, harness_id: str, status: HarnessStatus) -> HarnessState:
        name = harness_id.rsplit("-", 1)[0]
        return HarnessState(
            harness_id=harness_id,
            harness_name=name,
            arn=self.arn(harness_id),
            status=status,
            failure_reason=self.failure_reason if status.failed else "",
            runtime_arn=(
                f"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/harness_{name}-{RUNTIME}"
                if self.runtime_arn is None
                else self.runtime_arn
            ),
        )

    def create(self, request: CreateHarnessRequest) -> HarnessState:
        self.calls.append(("create", request))
        if self.create_error is not None:
            raise self.create_error
        harness_id = f"{request.spec.name}-{SUFFIX}"
        self.statuses[harness_id] = list(self.create_script)
        return self._state(harness_id, HarnessStatus.CREATING)

    def update(self, request: UpdateHarnessRequest) -> HarnessState:
        self.calls.append(("update", request))
        return self._state(request.harness_id, HarnessStatus.UPDATING)

    def get(self, harness_id: str) -> HarnessState:
        self.calls.append(("get", harness_id))
        if self.get_error is not None:
            raise self.get_error
        script = self.statuses.get(harness_id)
        if not script:
            raise HarnessNotFoundError(harness_id)
        status = script.pop(0) if len(script) > 1 else script[0]
        if status is None:
            raise HarnessNotFoundError(harness_id)
        return self._state(harness_id, status)

    def delete(self, harness_id: str) -> None:
        self.calls.append(("delete", harness_id))
        if self.delete_error is not None:
            raise self.delete_error
        if harness_id not in self.statuses:
            raise HarnessNotFoundError(harness_id)

    def replace_tags(self, request: ReplaceTagsRequest) -> None:
        self.calls.append(("replace_tags", request))

    def operations(self) -> list[str]:
        return [name for name, _ in self.calls]


@dataclass
class RecordingResponder:
    sent: list[CustomResourceResponse] = field(default_factory=list[CustomResourceResponse])

    def send(self, response: CustomResourceResponse) -> None:
        self.sent.append(response)

    def only(self) -> dict[str, Any]:
        assert len(self.sent) == 1, "exactly one response per callback"
        (response,) = self.sent
        assert response.response_url == RESPONSE_URL
        return cast("dict[str, Any]", json.loads(response.body))


@dataclass
class FakeClock:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=list[float])

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class FakeLogs:
    retained: list[RetainLogGroupRequest] = field(default_factory=list[RetainLogGroupRequest])
    error: Exception | None = None

    def retain(self, request: RetainLogGroupRequest) -> None:
        if self.error is not None:
            raise self.error
        self.retained.append(request)


@dataclass
class World:
    control: FakeControl = field(default_factory=FakeControl)
    responder: RecordingResponder = field(default_factory=RecordingResponder)
    clock: FakeClock = field(default_factory=FakeClock)
    logs: FakeLogs = field(default_factory=FakeLogs)
    deadline: float = 780.0

    def run(self, callback: object) -> dict[str, Any]:
        runtime = ProviderRuntime(
            control=self.control,
            responder=self.responder,
            logs=self.logs,
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        status = run(RunRequest(event=callback, runtime=runtime, deadline=self.deadline))
        body = self.responder.only()
        assert body["Status"] == status
        return body


HARNESS_ID = f"adlytics_test_agent-{SUFFIX}"


class TestCreate:
    def test_creates_waits_for_ready_and_answers_with_the_harness_identity(self) -> None:
        world = World()
        body = world.run(event("Create"))

        assert body == {
            "Status": "SUCCESS",
            "Reason": "",
            "PhysicalResourceId": HARNESS_ID,
            "StackId": STACK_ID,
            "RequestId": "request-1",
            "LogicalResourceId": LOGICAL_ID,
            "Data": {
                "Arn": FakeControl.arn(HARNESS_ID),
                "HarnessId": HARNESS_ID,
                "HarnessName": "adlytics_test_agent",
            },
        }
        assert world.control.operations() == ["create", "get", "get"]
        request = cast("CreateHarnessRequest", world.control.calls[0][1])
        assert request.spec.create_document() == spec_document()
        assert request.client_token == expected_token("request-1", "Create:create")
        assert world.clock.sleeps == [10.0]

    def test_the_client_token_is_stable_for_a_retried_request_and_distinct_otherwise(self) -> None:
        tokens: list[str] = []
        for request_id in ("request-1", "request-1", "request-2"):
            world = World()
            world.run(event("Create", RequestId=request_id))
            tokens.append(cast("CreateHarnessRequest", world.control.calls[0][1]).client_token)
        assert tokens[0] == tokens[1]
        assert tokens[0] != tokens[2]

    def test_a_failed_create_names_the_harness_so_the_rollback_deletes_it(self) -> None:
        world = World(control=FakeControl(failure_reason="Role cannot be assumed"))
        world.control.create_script = [HarnessStatus.CREATING, HarnessStatus.CREATE_FAILED]
        body = world.run(event("Create"))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == HARNESS_ID
        assert body["Reason"] == (
            f"Create: Harness {HARNESS_ID} reached CREATE_FAILED: Role cannot be assumed"
        )
        assert "Data" not in body

    def test_a_refused_create_names_the_provider_code_and_message(self) -> None:
        refusal = DependencyError(
            HARNESS_CONTROL_FAILED,
            "CreateHarness failed.",
            context={
                "service_code": "ValidationException",
                "provider_message": "1 validation error detected: grantType",
            },
        )
        world = World(control=FakeControl(create_error=refusal))
        body = world.run(event("Create"))

        assert body["Status"] == "FAILED"
        assert body["Reason"] == (
            "CreateHarness failed. ValidationException: 1 validation error detected: grantType"
        )
        assert body["PhysicalResourceId"] == "harness-resource:failed:request-1"

    def test_a_poll_failure_after_create_still_names_the_harness(self) -> None:
        world = World()
        world.control.get_error = DependencyError(HARNESS_CONTROL_FAILED, "GetHarness failed.")
        body = world.run(event("Create"))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == HARNESS_ID
        assert body["Reason"] == "GetHarness failed."

    @pytest.mark.parametrize(
        "poll_error",
        [ValueError("'MYSTERY' is not a valid HarnessStatus"), KeyError("harnessId")],
        ids=["unknown-status", "missing-member"],
    )
    def test_an_unexpected_poll_failure_after_create_still_names_the_harness(
        self, poll_error: Exception
    ) -> None:
        world = World()
        world.control.get_error = poll_error
        body = world.run(event("Create"))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == HARNESS_ID
        assert body["Reason"] == (
            f"Create: unexpected {type(poll_error).__name__} while waiting for Harness "
            f"{HARNESS_ID}."
        )
        assert str(poll_error) not in body["Reason"]

    def test_gives_up_before_the_deadline_naming_the_last_status(self) -> None:
        world = World(deadline=25.0)
        world.control.create_script = [HarnessStatus.CREATING]
        body = world.run(event("Create"))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == HARNESS_ID
        assert body["Reason"] == (
            f"Create: gave up waiting for Harness {HARNESS_ID} before the provider timeout; "
            "last status CREATING."
        )
        # The last sleep is cut to the deadline, never past it.
        assert world.clock.sleeps == [10.0, 10.0, 5.0]
        assert world.clock.now == 25.0


class TestStringifiedProperties:
    def test_numbers_and_text_survive_the_json_string_exactly(self) -> None:
        spec = parse_spec(properties()["HarnessSpec"])
        assert spec.document["maxIterations"] == 24
        assert spec.document["systemPrompt"] == [{"text": 'Say "hello".\nThen publish.'}]

    def test_a_declared_service_timeout_is_accepted(self) -> None:
        world = World()
        callback = event("Create", ResourceProperties=properties(ServiceTimeout="1200"))
        assert world.run(callback)["Status"] == "SUCCESS"

    @pytest.mark.parametrize(
        ("resource_properties", "reason"),
        [
            (
                {**properties(), "Extra": "x"},
                "ResourceProperties carries unsupported properties: ['Extra'].",
            ),
            (
                {"ServiceToken": SERVICE_TOKEN},
                "HarnessSpec must be a non-empty JSON string.",
            ),
            (
                {"ServiceToken": SERVICE_TOKEN, "HarnessSpec": '{"systemPrompt": "secret'},
                "HarnessSpec is not valid JSON.",
            ),
            (
                {"ServiceToken": SERVICE_TOKEN, "HarnessSpec": "[]"},
                "HarnessSpec must be a JSON object.",
            ),
            (
                properties(spec_document(clientToken="t" * 40)),
                "HarnessSpec carries unsupported members: ['clientToken'].",
            ),
            (
                properties(spec_document(harnessName="adlytics-test-agent")),
                "HarnessSpec.harnessName must match [a-zA-Z][a-zA-Z0-9_]{0,39}.",
            ),
            (
                properties(spec_document(executionRoleArn=None)),
                "HarnessSpec.executionRoleArn is required.",
            ),
            (
                properties(spec_document(memory={"managedMemoryConfiguration": {}})),
                "HarnessSpec.memory must be exactly agentCoreMemoryConfiguration; "
                "managed memory is never created by this resource.",
            ),
            (
                properties(spec_document(tags={"Environment": 7})),
                "HarnessSpec.tags values must be text.",
            ),
        ],
    )
    def test_an_invalid_property_set_fails_without_a_control_plane_call(
        self, resource_properties: dict[str, str], reason: str
    ) -> None:
        world = World()
        body = world.run(event("Create", ResourceProperties=resource_properties))

        assert body["Status"] == "FAILED"
        assert body["Reason"] == reason
        assert world.control.calls == []

    def test_an_unknown_request_type_fails(self) -> None:
        world = World()
        body = world.run(event("Create", RequestType="Read"))
        assert body["Reason"] == "RequestType is outside Create, Update and Delete."
        assert world.control.calls == []


def _update(world: World, old: Mapping[str, object], new: Mapping[str, object]) -> dict[str, Any]:
    world.control.statuses[HARNESS_ID] = [HarnessStatus.READY]
    return world.run(
        event(
            "Update",
            OldResourceProperties=properties(old),
            ResourceProperties=properties(new),
        )
    )


class TestUpdate:
    def test_patches_every_mutable_member_as_the_full_desired_state(self) -> None:
        """An unchanged spec is still sent whole: that is what corrects out-of-band drift."""
        world = World()
        body = _update(world, spec_document(), spec_document())

        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == HARNESS_ID
        assert body["Data"]["HarnessId"] == HARNESS_ID
        assert world.control.operations() == ["get", "update", "get", "replace_tags"]
        request = cast("UpdateHarnessRequest", world.control.calls[1][1])
        desired = spec_document()
        assert request.harness_id == HARNESS_ID
        assert request.client_token == expected_token("request-1", "Update:update")
        assert request.document == {
            "executionRoleArn": ROLE_ARN,
            "authorizerConfiguration": {"optionalValue": desired["authorizerConfiguration"]},
            "model": desired["model"],
            "memory": {"optionalValue": desired["memory"]},
            "tools": desired["tools"],
            "allowedTools": desired["allowedTools"],
            "systemPrompt": desired["systemPrompt"],
            "maxIterations": 24,
            "maxTokens": 16384,
            "timeoutSeconds": 180,
        }
        tags = cast("ReplaceTagsRequest", world.control.calls[3][1])
        assert tags == ReplaceTagsRequest(
            arn=FakeControl.arn(HARNESS_ID),
            tags={"Application": "adlytics-ai", "Environment": "test"},
        )

    def test_waits_out_an_update_already_running_before_patching(self) -> None:
        world = World()
        world.control.statuses[HARNESS_ID] = [
            HarnessStatus.UPDATING,
            HarnessStatus.READY,
            HarnessStatus.UPDATING,
            HarnessStatus.READY,
        ]
        body = world.run(event("Update"))
        assert body["Status"] == "SUCCESS"
        assert world.control.operations() == [
            "get",
            "get",
            "update",
            "get",
            "get",
            "replace_tags",
        ]

    def test_a_name_change_creates_a_new_harness_under_a_new_physical_id(self) -> None:
        world = World()
        renamed = spec_document(harnessName="adlytics_test_agent2")
        body = _update(world, spec_document(), renamed)

        new_id = f"adlytics_test_agent2-{SUFFIX}"
        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == new_id
        assert body["Data"]["HarnessName"] == "adlytics_test_agent2"
        # CloudFormation deletes the old one after a new physical id; nothing is patched here.
        assert world.control.operations() == ["create", "get", "get"]
        request = cast("CreateHarnessRequest", world.control.calls[0][1])
        assert request.client_token == expected_token("request-1", "Update (replacement):create")

    def test_a_harness_deleted_out_of_band_is_recreated(self) -> None:
        world = World()
        body = world.run(event("Update"))  # no script: GetHarness says not found
        assert body["Status"] == "SUCCESS"
        assert world.control.operations() == ["get", "create", "get", "get"]

    def test_a_failed_replacement_removes_the_new_harness_and_keeps_the_old_id(self) -> None:
        world = World(control=FakeControl(failure_reason="quota"))
        world.control.create_script = [HarnessStatus.CREATE_FAILED]
        body = _update(world, spec_document(), spec_document(harnessName="adlytics_test_agent2"))

        new_id = f"adlytics_test_agent2-{SUFFIX}"
        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == HARNESS_ID
        assert body["Reason"] == (
            f"Update (replacement): Harness {new_id} reached CREATE_FAILED: quota"
        )
        assert world.control.calls[-1] == ("delete", new_id)

    @pytest.mark.parametrize("cleanup_fails", [True, False], ids=["cleanup-fails", "not-found"])
    def test_a_failed_replacement_cleanup_is_reported_and_the_response_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
    ) -> None:
        """A cleanup that fails can leave an orphan Harness; only its report says so."""
        reported: list[BaseException] = []

        def capture(error: BaseException, _report: object) -> None:
            reported.append(error)

        monkeypatch.setattr(provider_module, "report_error", capture)
        cleanup = DependencyError(HARNESS_CONTROL_FAILED, "fixture delete failed.")
        world = World(control=FakeControl(failure_reason="quota"))
        world.control.create_script = [HarnessStatus.CREATE_FAILED]
        world.control.delete_error = cleanup if cleanup_fails else None
        body = _update(world, spec_document(), spec_document(harnessName="adlytics_test_agent2"))

        new_id = f"adlytics_test_agent2-{SUFFIX}"
        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == HARNESS_ID
        assert body["Reason"] == (
            f"Update (replacement): Harness {new_id} reached CREATE_FAILED: quota"
        )
        assert world.control.calls[-1] == ("delete", new_id)
        # The callback's own failure is always reported once by `run`; the cleanup adds one.
        assert (cleanup in reported) is cleanup_fails
        assert len(reported) == (2 if cleanup_fails else 1)

    def test_an_unexpected_poll_failure_in_a_replacement_removes_the_new_harness(self) -> None:
        world = World()
        world.control.get_error = ValueError("'MYSTERY' is not a valid HarnessStatus")
        body = _update(world, spec_document(), spec_document(harnessName="adlytics_test_agent2"))

        new_id = f"adlytics_test_agent2-{SUFFIX}"
        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == HARNESS_ID
        assert body["Reason"] == (
            f"Update (replacement): unexpected ValueError while waiting for Harness {new_id}."
        )
        assert world.control.operations() == ["create", "get", "delete"]
        assert world.control.calls[-1] == ("delete", new_id)

    def test_dropping_a_member_in_place_is_refused(self) -> None:
        world = World()
        body = _update(world, spec_document(), spec_document(authorizerConfiguration=None))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == HARNESS_ID
        assert body["Reason"] == (
            "HarnessSpec no longer declares ['authorizerConfiguration']; removing a member in "
            "place is not supported. Change harnessName to replace the Harness instead."
        )
        assert "update" not in world.control.operations()

    def test_an_update_failure_names_the_status_and_keeps_the_id(self) -> None:
        world = World()
        world.control.statuses[HARNESS_ID] = [
            HarnessStatus.READY,
            HarnessStatus.UPDATE_FAILED,
        ]
        body = world.run(event("Update"))
        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == HARNESS_ID
        assert body["Reason"] == f"Update: Harness {HARNESS_ID} reached UPDATE_FAILED"

    def test_a_harness_being_deleted_is_not_patched(self) -> None:
        world = World()
        world.control.statuses[HARNESS_ID] = [HarnessStatus.DELETING]
        body = world.run(event("Update"))
        assert body["Reason"] == f"Update: Harness {HARNESS_ID} reached DELETING"
        assert world.control.operations() == ["get"]


class TestDelete:
    def test_deletes_and_waits_until_the_harness_is_gone(self) -> None:
        world = World()
        world.control.statuses[HARNESS_ID] = [
            HarnessStatus.READY,
            HarnessStatus.DELETING,
            None,
        ]
        body = world.run(event("Delete"))

        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == HARNESS_ID
        assert "Data" not in body
        assert world.control.operations() == ["get", "delete", "get", "get"]

    def test_an_absent_harness_is_already_deleted(self) -> None:
        world = World()
        body = world.run(event("Delete"))
        assert body["Status"] == "SUCCESS"
        assert world.control.operations() == ["get"]

    def test_not_found_from_the_delete_call_is_success(self) -> None:
        world = World()
        world.control.statuses[HARNESS_ID] = [HarnessStatus.READY]
        world.control.delete_error = HarnessNotFoundError(HARNESS_ID)
        body = world.run(event("Delete"))
        assert body["Status"] == "SUCCESS"
        assert world.control.operations() == ["get", "delete"]

    def test_a_failed_creates_placeholder_is_deleted_without_a_call(self) -> None:
        world = World()
        body = world.run(event("Delete", PhysicalResourceId="harness-resource:failed:request-0"))
        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == "harness-resource:failed:request-0"
        assert world.control.calls == []

    def test_a_harness_already_deleting_is_only_awaited(self) -> None:
        world = World()
        world.control.statuses[HARNESS_ID] = [HarnessStatus.DELETING, None]
        assert world.run(event("Delete"))["Status"] == "SUCCESS"
        assert "delete" not in world.control.operations()

    def test_delete_failed_is_reported(self) -> None:
        world = World(control=FakeControl(failure_reason="runtime busy"))
        world.control.statuses[HARNESS_ID] = [HarnessStatus.READY, HarnessStatus.DELETE_FAILED]
        body = world.run(event("Delete"))
        assert body["Status"] == "FAILED"
        assert body["Reason"] == f"Delete: Harness {HARNESS_ID} reached DELETE_FAILED: runtime busy"

    def test_gives_up_on_a_delete_that_does_not_finish(self) -> None:
        world = World(deadline=15.0)
        world.control.statuses[HARNESS_ID] = [HarnessStatus.READY, HarnessStatus.DELETING]
        body = world.run(event("Delete"))
        assert body["Status"] == "FAILED"
        assert body["Reason"].endswith("last status DELETING.")


class TestExactlyOneResponse:
    def test_an_unexpected_exception_is_answered_once_without_its_message(self) -> None:
        world = World(control=FakeControl(create_error=RuntimeError("token=abc.def.ghi")))
        body = world.run(event("Create"))
        assert body["Status"] == "FAILED"
        assert body["Reason"] == (
            "Unexpected RuntimeError in the Harness provider; RequestId request-1."
        )

    def test_a_long_reason_is_bounded(self) -> None:
        refusal = DependencyError(
            HARNESS_CONTROL_FAILED,
            "CreateHarness failed.",
            context={"service_code": "ValidationException", "provider_message": "x" * 5000},
        )
        world = World(control=FakeControl(create_error=refusal))
        body = world.run(event("Create"))
        assert len(body["Reason"]) == MAX_REASON_LENGTH
        assert body["Reason"].endswith("...")

    @pytest.mark.parametrize("url", [None, "", "http://insecure.example/response"])
    def test_a_callback_with_no_https_response_url_is_not_acted_on(self, url: str | None) -> None:
        world = World()
        callback = event("Create")
        if url is None:
            del callback["ResponseURL"]
        else:
            callback["ResponseURL"] = url
        runtime = ProviderRuntime(
            control=world.control,
            responder=world.responder,
            logs=world.logs,
            clock=world.clock,
            sleep=world.clock.sleep,
        )
        with pytest.raises(InvariantError):
            run(RunRequest(event=callback, runtime=runtime, deadline=780.0))
        assert world.responder.sent == []
        assert world.control.calls == []


def _retained() -> dict[str, str]:
    return properties(RuntimeLogRetentionDays="90")


_REVISION_A = "sha256:" + "a" * 64
_REVISION_B = "sha256:" + "b" * 64


@pytest.mark.covers("ADR-077", "ADR-147")
class TestMemoryStrategyRevision:
    """A changed memory-strategy digest must reach UpdateHarness, which re-derives retrieval."""

    def test_an_update_differing_only_in_the_revision_sends_the_full_desired_state(self) -> None:
        world = World()
        world.control.statuses[HARNESS_ID] = [HarnessStatus.READY]
        body = world.run(
            event(
                "Update",
                OldResourceProperties=properties(MemoryStrategyRevision=_REVISION_A),
                ResourceProperties=properties(MemoryStrategyRevision=_REVISION_B),
            )
        )
        assert body["Status"] == "SUCCESS"
        assert world.control.operations() == ["get", "update", "get", "replace_tags"]
        request = cast("UpdateHarnessRequest", world.control.calls[1][1])
        desired = spec_document()
        assert request.document == {
            "executionRoleArn": ROLE_ARN,
            "authorizerConfiguration": {"optionalValue": desired["authorizerConfiguration"]},
            "model": desired["model"],
            "memory": {"optionalValue": desired["memory"]},
            "tools": desired["tools"],
            "allowedTools": desired["allowedTools"],
            "systemPrompt": desired["systemPrompt"],
            "maxIterations": 24,
            "maxTokens": 16384,
            "timeoutSeconds": 180,
        }

    def test_a_create_carrying_the_revision_succeeds(self) -> None:
        world = World()
        body = world.run(
            event("Create", ResourceProperties=properties(MemoryStrategyRevision=_REVISION_A))
        )
        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == HARNESS_ID

    def test_an_empty_revision_fails_before_any_call(self) -> None:
        world = World()
        body = world.run(event("Create", ResourceProperties=properties(MemoryStrategyRevision="")))
        assert body["Status"] == "FAILED"
        assert body["Reason"] == "MemoryStrategyRevision must be non-empty text."
        assert world.control.calls == []


class TestRuntimeLogRetention:
    """infra-composition-stacks: the runtime's managed log group gets its retention before READY."""

    def test_create_retains_the_runtime_group_and_returns_its_name(self) -> None:
        world = World()
        body = world.run(event("Create", ResourceProperties=_retained()))

        assert body["Status"] == "SUCCESS"
        assert world.logs.retained == [
            RetainLogGroupRequest(
                log_group_name=RUNTIME_LOG_GROUP,
                retention_days=90,
                tags={"Application": "adlytics-ai", "Environment": "test"},
            )
        ]
        assert body["Data"]["RuntimeLogGroupName"] == RUNTIME_LOG_GROUP

    def test_update_reapplies_it_so_an_out_of_band_change_is_corrected(self) -> None:
        world = World()
        world.control.statuses[HARNESS_ID] = [HarnessStatus.READY]
        body = world.run(
            event("Update", OldResourceProperties=properties(), ResourceProperties=_retained())
        )

        assert body["Status"] == "SUCCESS"
        assert [request.retention_days for request in world.logs.retained] == [90]
        assert body["Data"]["RuntimeLogGroupName"] == RUNTIME_LOG_GROUP

    def test_without_the_property_nothing_is_retained(self) -> None:
        world = World()
        body = world.run(event("Create"))
        assert world.logs.retained == []
        assert "RuntimeLogGroupName" not in body["Data"]

    def test_a_retention_failure_fails_create_naming_the_harness_for_rollback(self) -> None:
        refusal = DependencyError(
            HARNESS_CONTROL_FAILED,
            "PutRetentionPolicy failed.",
            context={"service_code": "AccessDeniedException"},
        )
        world = World(logs=FakeLogs(error=refusal))
        body = world.run(event("Create", ResourceProperties=_retained()))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == HARNESS_ID
        assert body["Reason"] == "PutRetentionPolicy failed. AccessDeniedException:"

    def test_a_runtime_that_is_not_the_harnesss_own_is_refused(self) -> None:
        world = World(
            control=FakeControl(
                runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/other-AbCdE12345"
            )
        )
        body = world.run(event("Create", ResourceProperties=_retained()))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == HARNESS_ID
        assert "reports no runtime of its own" in body["Reason"]
        assert world.logs.retained == []

    def test_an_unexpected_retention_failure_still_names_the_harness(self) -> None:
        world = World(logs=FakeLogs(error=KeyError("logGroups")))
        body = world.run(event("Create", ResourceProperties=_retained()))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == HARNESS_ID
        assert body["Reason"] == f"Retaining {RUNTIME_LOG_GROUP}: unexpected KeyError."

    def test_the_group_carries_the_harnesss_tags(self) -> None:
        world = World()
        world.run(event("Create", ResourceProperties=_retained()))
        assert world.logs.retained[0].tags == {"Application": "adlytics-ai", "Environment": "test"}

    @pytest.mark.parametrize("days", ["45", "ninety", "", "-90"])
    def test_a_retention_cloudwatch_refuses_fails_before_any_call(self, days: str) -> None:
        world = World()
        body = world.run(
            event("Create", ResourceProperties=properties(RuntimeLogRetentionDays=days))
        )

        assert body["Status"] == "FAILED"
        assert body["Reason"] == (
            "RuntimeLogRetentionDays must be a retention CloudWatch Logs accepts."
        )
        assert world.control.calls == []


class TestCloudWatchLogRetention:
    @dataclass
    class Client:
        """A `logs` client stand-in recording each call."""

        create_error: Exception | None = None
        put_error: Exception | None = None
        read_back_days: int | None = 90
        calls: list[tuple[str, dict[str, object]]] = field(
            default_factory=list[tuple[str, dict[str, object]]]
        )

        def create_log_group(self, **kwargs: object) -> None:
            self.calls.append(("create_log_group", kwargs))
            if self.create_error is not None:
                raise self.create_error

        def put_retention_policy(self, **kwargs: object) -> None:
            self.calls.append(("put_retention_policy", kwargs))
            if self.put_error is not None:
                raise self.put_error

        def get_paginator(self, operation: str) -> TestCloudWatchLogRetention.Client:
            assert operation == "describe_log_groups"
            return self

        def paginate(self, **kwargs: object) -> list[dict[str, object]]:
            self.calls.append(("describe_log_groups", kwargs))
            group: dict[str, object] = {
                "logGroupName": RUNTIME_LOG_GROUP,
                "logGroupArn": f"arn:aws:logs:us-east-1:123456789012:log-group:{RUNTIME_LOG_GROUP}",
            }
            if self.read_back_days is not None:
                group["retentionInDays"] = self.read_back_days
            # A prefix match may also return a longer name; only the exact one counts, and it
            # is on the second page, so a first-page read would refuse a correct retention.
            return [
                {"logGroups": [{"logGroupName": f"{RUNTIME_LOG_GROUP}-other"}]},
                {"logGroups": [group]},
            ]

        def tag_resource(self, **kwargs: object) -> None:
            self.calls.append(("tag_resource", kwargs))

    @staticmethod
    def _error(code: str) -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": "m"}}, "Operation")

    def _retain(
        self, client: TestCloudWatchLogRetention.Client, tags: dict[str, str] | None = None
    ) -> None:
        CloudWatchLogRetention(cast("Any", client)).retain(
            RetainLogGroupRequest(
                log_group_name=RUNTIME_LOG_GROUP, retention_days=90, tags=tags or {}
            )
        )

    def test_creates_puts_the_policy_reads_it_back_and_tags_the_group(self) -> None:
        client = self.Client()
        self._retain(client, {"Environment": "test"})
        assert client.calls == [
            ("create_log_group", {"logGroupName": RUNTIME_LOG_GROUP}),
            (
                "put_retention_policy",
                {"logGroupName": RUNTIME_LOG_GROUP, "retentionInDays": 90},
            ),
            ("describe_log_groups", {"logGroupNamePrefix": RUNTIME_LOG_GROUP}),
            (
                "tag_resource",
                {
                    "resourceArn": (
                        f"arn:aws:logs:us-east-1:123456789012:log-group:{RUNTIME_LOG_GROUP}"
                    ),
                    "tags": {"Environment": "test"},
                },
            ),
        ]

    def test_an_existing_group_is_the_expected_case(self) -> None:
        client = self.Client(create_error=self._error("ResourceAlreadyExistsException"))
        self._retain(client)
        assert [name for name, _ in client.calls] == [
            "create_log_group",
            "put_retention_policy",
            "describe_log_groups",
        ]

    @pytest.mark.parametrize("read_back", [None, 30])
    def test_a_group_that_does_not_read_back_the_retention_is_refused(
        self, read_back: int | None
    ) -> None:
        with pytest.raises(DependencyError) as raised:
            self._retain(self.Client(read_back_days=read_back))
        assert raised.value.code == LOG_RETENTION_FAILED
        assert "did not read back" in raised.value.description

    @pytest.mark.parametrize("which", ["create_error", "put_error"])
    def test_any_other_refusal_is_a_dependency_error(self, which: str) -> None:
        denied = self._error("AccessDeniedException")
        client = (
            self.Client(create_error=denied)
            if which == "create_error"
            else self.Client(put_error=denied)
        )
        with pytest.raises(DependencyError) as raised:
            self._retain(client)
        assert raised.value.code == LOG_RETENTION_FAILED
        assert raised.value.context["service_code"] == "AccessDeniedException"


class TestSpecProjection:
    def test_update_document_never_carries_create_only_members(self) -> None:
        spec = parse_spec(json.dumps(spec_document()))
        document = spec.update_document(spec)
        assert "harnessName" not in document
        assert "tags" not in document

    def test_a_spec_without_tags_leaves_tags_alone(self) -> None:
        world = World()
        body = _update(world, spec_document(tags=None), spec_document(tags=None))
        assert body["Status"] == "SUCCESS"
        assert "replace_tags" not in world.control.operations()

    def test_a_non_object_spec_is_refused(self) -> None:
        with pytest.raises(HarnessResourceError):
            parse_spec('"text"')


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} happened"}}, "GetHarness")


class TestAgentCoreRequestShaping:
    def test_delete_always_keeps_memory(self) -> None:
        assert delete_request(HARNESS_ID) == {
            "harnessId": HARNESS_ID,
            "deleteManagedMemory": False,
        }

    def test_create_sends_the_spec_with_the_client_token(self) -> None:
        token = "a" * 64
        request = CreateHarnessRequest(
            spec=parse_spec(json.dumps(spec_document())), client_token=token
        )
        assert create_request(request) == {**spec_document(), "clientToken": token}

    def test_update_adds_identity_and_token_to_the_document(self) -> None:
        request = UpdateHarnessRequest(
            harness_id=HARNESS_ID, document={"maxIterations": 3}, client_token="b" * 64
        )
        assert update_request(request) == {
            "maxIterations": 3,
            "harnessId": HARNESS_ID,
            "clientToken": "b" * 64,
        }

    def test_an_update_document_cannot_carry_its_own_identity(self) -> None:
        with pytest.raises(ValueError, match="identity"):
            UpdateHarnessRequest(
                harness_id=HARNESS_ID, document={"harnessId": "other"}, client_token="b" * 64
            )

    def test_a_short_client_token_is_refused(self) -> None:
        with pytest.raises(ValueError, match="client token"):
            # A-TEST-LITERALS-001: the short token under test; see docs/security-dispositions.md
            CreateHarnessRequest(spec=parse_spec(json.dumps(spec_document())), client_token="x")  # nosec B106

    def test_not_found_is_expected_control_flow(self) -> None:
        error = translated(_client_error("ResourceNotFoundException"), "GetHarness")
        assert isinstance(error, HarnessNotFoundError)

    def test_any_other_refusal_carries_the_provider_diagnostic(self) -> None:
        error = translated(_client_error("AccessDeniedException"), "GetHarness")
        assert isinstance(error, DependencyError)
        assert error.code == HARNESS_CONTROL_FAILED
        assert error.description == "GetHarness failed."
        assert error.context["service_code"] == "AccessDeniedException"
        assert error.context["provider_message"] == "AccessDeniedException happened"

    def test_the_tag_diff_removes_stale_keys_and_writes_the_desired_set(self) -> None:
        changes = tag_changes(
            {"Application": "adlytics-ai", "Stale": "x"},
            {"Application": "adlytics-ai", "Env": "t"},
        )
        assert changes == TagChanges(
            remove=("Stale",), write={"Application": "adlytics-ai", "Env": "t"}
        )

    def test_matching_tags_make_no_write(self) -> None:
        assert tag_changes({"Env": "t"}, {"Env": "t"}) == TagChanges(remove=(), write={})


@dataclass
class FakeResult:
    status_code: int


@dataclass
class FakeSession:
    status_code: int = 200
    error: Exception | None = None
    puts: list[dict[str, object]] = field(default_factory=list[dict[str, object]])

    def put(self, url: str, **kwargs: object) -> FakeResult:
        self.puts.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return FakeResult(self.status_code)


class TestResponder:
    def test_puts_the_body_with_an_empty_content_type(self) -> None:
        session = FakeSession()
        HttpsCustomResourceResponder(cast("Any", session)).send(
            CustomResourceResponse(response_url=RESPONSE_URL, body=b"{}")
        )
        assert session.puts == [
            {"url": RESPONSE_URL, "data": b"{}", "headers": {"Content-Type": ""}, "timeout": 30}
        ]

    @pytest.mark.parametrize(
        "session",
        [FakeSession(status_code=403), FakeSession(error=requests.ConnectionError("down"))],
    )
    def test_a_refused_or_failed_delivery_raises_without_the_url(
        self, session: FakeSession
    ) -> None:
        with pytest.raises(DependencyError) as raised:
            HttpsCustomResourceResponder(cast("Any", session)).send(
                CustomResourceResponse(response_url=RESPONSE_URL, body=b"{}")
            )
        assert "presigned" not in str(raised.value)
        assert "presigned" not in json.dumps(raised.value.context)

    def test_only_an_https_url_is_answerable(self) -> None:
        with pytest.raises(ValueError, match="https"):
            CustomResourceResponse(response_url="http://example/response", body=b"{}")


@dataclass
class FakeContext:
    remaining_ms: int

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms


def test_the_handler_reads_the_lambda_budget_and_a_missing_budget_is_zero() -> None:
    assert handler_module.remaining_seconds(FakeContext(900_000)) == 900.0
    assert handler_module.remaining_seconds(object()) == 0.0
