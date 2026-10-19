# SPDX-License-Identifier: MIT-0
"""boto3 implementation of HarnessControlPort over the AgentCore control plane (ADR-077).

The request documents come from a validated `HarnessSpec`, whose members are the API's own
camelCase shape; the service validates the nested structure. `ResourceNotFoundException` is the
one expected control-flow error and becomes `HarnessNotFoundError`; every other SDK failure
becomes a `DependencyError` carrying the shared provider diagnostic, so the custom resource can
name the exact refusal in its CloudFormation Reason.

Request shaping, error translation, and the tag diff are pure functions, unit-tested without an
SDK stand-in; the calls themselves are covered against the deployed Harness.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from botocore.exceptions import BotoCoreError, ClientError

from adapters.outbound.aws_errors import aws_failure_context
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.harness_control import (
    CreateHarnessRequest,
    HarnessControlPort,
    HarnessNotFoundError,
    HarnessState,
    HarnessStatus,
    ReplaceTagsRequest,
    UpdateHarnessRequest,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from types_boto3_bedrock_agentcore_control.client import BedrockAgentCoreControlClient
    from types_boto3_bedrock_agentcore_control.type_defs import (
        CreateHarnessRequestTypeDef,
        DeleteHarnessRequestTypeDef,
        HarnessTypeDef,
        UpdateHarnessRequestTypeDef,
    )

__all__ = [
    "HARNESS_CONTROL_FAILED",
    "AgentCoreHarnessControl",
    "TagChanges",
    "create_request",
    "delete_request",
    "harness_state",
    "tag_changes",
    "translated",
    "update_request",
]

HARNESS_CONTROL_FAILED: Final = ErrorCode("HARNESSCR", 2)
_NOT_FOUND: Final = "ResourceNotFoundException"


def harness_state(harness: HarnessTypeDef) -> HarnessState:
    """Project the service's Harness description onto the port's state."""
    runtime = harness.get("environment", {}).get("agentCoreRuntimeEnvironment", {})
    return HarnessState(
        harness_id=harness["harnessId"],
        harness_name=harness["harnessName"],
        arn=harness["arn"],
        status=HarnessStatus(harness["status"]),
        failure_reason=harness.get("failureReason", ""),
        runtime_arn=runtime.get("agentRuntimeArn", ""),
    )


def create_request(request: CreateHarnessRequest) -> CreateHarnessRequestTypeDef:
    """Shape CreateHarness: the spec's document plus the callback's client token."""
    # The spec's members are exactly CreateHarness members (HarnessSpec refuses others), and
    # their nested shapes are the service's to validate.
    return cast(
        "CreateHarnessRequestTypeDef",
        {**request.spec.create_document(), "clientToken": request.client_token},
    )


def update_request(request: UpdateHarnessRequest) -> UpdateHarnessRequestTypeDef:
    """Shape UpdateHarness: the full desired document plus identity and client token."""
    return cast(
        "UpdateHarnessRequestTypeDef",
        {**request.document, "harnessId": request.harness_id, "clientToken": request.client_token},
    )


def delete_request(harness_id: str) -> DeleteHarnessRequestTypeDef:
    """Shape DeleteHarness, always keeping memory: the retained Memory stack owns it."""
    return {"harnessId": harness_id, "deleteManagedMemory": False}


@dataclass(frozen=True, slots=True)
class TagChanges:
    """What makes a resource's tags exactly the desired set."""

    remove: tuple[str, ...]
    write: Mapping[str, str]
    """Empty when the current tags already equal the desired ones."""


def tag_changes(current: Mapping[str, str], desired: Mapping[str, str]) -> TagChanges:
    """Diff the live tags against the desired set."""
    return TagChanges(
        remove=tuple(sorted(set(current) - set(desired))),
        write={} if dict(current) == dict(desired) else dict(desired),
    )


def translated(error: ClientError | BotoCoreError, operation: str) -> Exception:
    """Map an SDK failure to not-found or a DependencyError carrying the provider diagnostic."""
    if isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") == _NOT_FOUND:
        return HarnessNotFoundError(operation)
    return DependencyError(
        HARNESS_CONTROL_FAILED,
        f"{operation} failed.",
        context={"operation": operation, **aws_failure_context(error)},
    )


class AgentCoreHarnessControl(HarnessControlPort):
    """One typed `bedrock-agentcore-control` client per process."""

    def __init__(self, client: BedrockAgentCoreControlClient) -> None:
        self._client = client

    def create(self, request: CreateHarnessRequest) -> HarnessState:
        """Call CreateHarness."""
        try:
            return harness_state(self._client.create_harness(**create_request(request))["harness"])
        except (ClientError, BotoCoreError) as error:
            raise translated(error, "CreateHarness") from error

    def update(self, request: UpdateHarnessRequest) -> HarnessState:
        """Call UpdateHarness."""
        try:
            return harness_state(self._client.update_harness(**update_request(request))["harness"])
        except (ClientError, BotoCoreError) as error:
            raise translated(error, "UpdateHarness") from error

    def get(self, harness_id: str) -> HarnessState:
        """Call GetHarness for the current version."""
        try:
            return harness_state(self._client.get_harness(harnessId=harness_id)["harness"])
        except (ClientError, BotoCoreError) as error:
            raise translated(error, "GetHarness") from error

    def delete(self, harness_id: str) -> None:
        """Call DeleteHarness without deleting memory."""
        try:
            self._client.delete_harness(**delete_request(harness_id))
        except (ClientError, BotoCoreError) as error:
            raise translated(error, "DeleteHarness") from error

    def replace_tags(self, request: ReplaceTagsRequest) -> None:
        """Remove tags outside the desired set, then add or overwrite the desired ones."""
        try:
            # aws-pagination: AgentCore Control ListTagsForResource returns one ARN's whole map.
            current = self._client.list_tags_for_resource(resourceArn=request.arn).get("tags", {})
            changes = tag_changes(current, request.tags)
            if changes.remove:
                self._client.untag_resource(resourceArn=request.arn, tagKeys=list(changes.remove))
            if changes.write:
                self._client.tag_resource(resourceArn=request.arn, tags=dict(changes.write))
        except (ClientError, BotoCoreError) as error:
            raise translated(error, "ReplaceHarnessTags") from error
