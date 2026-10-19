# SPDX-License-Identifier: MIT-0
"""Pure decisions for the Lambda-backed custom resource that owns the managed Harness (ADR-077).

CloudFormation's `AWS::BedrockAgentCore::Harness` schema rejects the Gateway tool's
`TOKEN_EXCHANGE` grant, while the control-plane API accepts it, so one custom resource owns the
whole Harness through Create/Update/DeleteHarness. The stack delivers the complete API request
document as one JSON string property, `HarnessSpec`, because CloudFormation delivers every
custom-resource property value as text.

This module owns the closed callback envelope, the spec's validation, the create and update
request documents, the deterministic client tokens, and the name of the runtime log group whose
retention the provider applies (infra-composition-stacks: a managed AgentCore telemetry group gets
its configured retention before the Harness is reported ready). The adapter owns the clock, the
control-plane calls, and the HTTPS response.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

__all__ = [
    "MEMORY_REVISION_PROPERTY",
    "RETENTION_PROPERTY",
    "SPEC_PROPERTY",
    "CallbackEvent",
    "HarnessResourceError",
    "HarnessSpec",
    "RequestType",
    "client_token",
    "is_harness_id",
    "parse_event",
    "parse_spec",
    "runtime_log_group",
]

SPEC_PROPERTY: Final = "HarnessSpec"
"""The one resource property that carries the whole CreateHarness document as JSON text."""

_SERVICE_PROPERTIES: Final = frozenset({"ServiceToken", "ServiceTimeout"})
"""What CloudFormation itself adds to (or the stack declares on) every custom resource."""

MEMORY_REVISION_PROPERTY: Final = "MemoryStrategyRevision"
"""A digest of the memory's declared long-term strategies (ADR-147). Never read: its only job is
that a changed value makes CloudFormation send an Update, and Update always sends the full desired
state to UpdateHarness, which re-derives the retrieval configuration."""

RETENTION_PROPERTY: Final = "RuntimeLogRetentionDays"
"""Days the Harness runtime's own log group keeps events, as text. Absent only in properties
written before the provider owned that group's retention."""

_RETENTION_DAYS: Final = frozenset(
    {
        1,
        3,
        5,
        7,
        14,
        30,
        60,
        90,
        120,
        150,
        180,
        365,
        400,
        545,
        731,
        1096,
        1827,
        2192,
        2557,
        2922,
        3288,
        3653,
    }
)
"""The values `PutRetentionPolicy` accepts (CloudWatch Logs API reference)."""

_RUNTIME_ARN: Final = re.compile(
    r"\Aarn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime/"
    r"(?P<runtime>harness_[a-zA-Z][a-zA-Z0-9_]{0,39}-[a-zA-Z0-9]{10})\Z"
)
_RUNTIME_LOG_GROUP_PREFIX: Final = "/aws/bedrock-agentcore/runtimes/"
_RUNTIME_ENDPOINT: Final = "DEFAULT"
"""The endpoint qualifier the Harness invokes, which names the runtime's log group."""

_HARNESS_NAME: Final = re.compile(r"\A[a-zA-Z][a-zA-Z0-9_]{0,39}\Z")
_HARNESS_ID: Final = re.compile(r"\A[a-zA-Z][a-zA-Z0-9_]{0,39}-[a-zA-Z0-9]{10}\Z")
_CLIENT_TOKEN_LABEL: Final = "adlytics-harness-resource/v1"  # noqa: S105 - a hash label

_CREATE_MEMBERS: Final = frozenset(
    {
        "harnessName",
        "executionRoleArn",
        "environment",
        "environmentArtifact",
        "environmentVariables",
        "authorizerConfiguration",
        "model",
        "systemPrompt",
        "tools",
        "skills",
        "allowedTools",
        "memory",
        "truncation",
        "maxIterations",
        "maxTokens",
        "timeoutSeconds",
        "tags",
    }
)
"""CreateHarness members the spec may carry. `clientToken` is the provider's, never the spec's."""

_CREATE_ONLY: Final = frozenset({"harnessName", "tags"})
"""Not UpdateHarness members: a name change replaces the Harness, and tags use the tag API."""

_OPTIONAL_VALUE_WRAPPED: Final = frozenset(
    {"authorizerConfiguration", "memory", "environmentArtifact"}
)
"""UpdateHarness members whose value travels inside an `optionalValue` wrapper."""


class HarnessResourceError(ValueError):
    """A callback or its HarnessSpec violates the closed custom-resource contract."""


class RequestType(StrEnum):
    """The three CloudFormation custom-resource request types."""

    CREATE = "Create"
    UPDATE = "Update"
    DELETE = "Delete"


def _object(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HarnessResourceError(f"{what} must be a JSON object.")
    mapping = cast("Mapping[object, object]", value)
    if not all(isinstance(key, str) for key in mapping):
        raise HarnessResourceError(f"{what} must have text keys.")
    return cast("Mapping[str, object]", mapping)


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    """One validated CreateHarness document, without a client token."""

    document: Mapping[str, object]

    def __post_init__(self) -> None:
        unknown = sorted(set(self.document) - _CREATE_MEMBERS)
        if unknown:
            raise HarnessResourceError(f"HarnessSpec carries unsupported members: {unknown}.")
        name = self.document.get("harnessName")
        if not isinstance(name, str) or _HARNESS_NAME.fullmatch(name) is None:
            raise HarnessResourceError(
                "HarnessSpec.harnessName must match [a-zA-Z][a-zA-Z0-9_]{0,39}."
            )
        role = self.document.get("executionRoleArn")
        if not isinstance(role, str) or not role.strip():
            raise HarnessResourceError("HarnessSpec.executionRoleArn is required.")
        if "memory" in self.document:
            memory = _object(self.document["memory"], "HarnessSpec.memory")
            # infra-composition-stacks: conversation memory is never created as a side effect of
            # this resource. The retained Memory stack's ARN is the only form accepted.
            if set(memory) != {"agentCoreMemoryConfiguration"}:
                raise HarnessResourceError(
                    "HarnessSpec.memory must be exactly agentCoreMemoryConfiguration; "
                    "managed memory is never created by this resource."
                )
        tags = self.document.get("tags")
        if tags is not None and not all(
            isinstance(value, str) for value in _object(tags, "HarnessSpec.tags").values()
        ):
            raise HarnessResourceError("HarnessSpec.tags values must be text.")

    @property
    def name(self) -> str:
        """The Harness name; changing it replaces the Harness."""
        return cast("str", self.document["harnessName"])

    @property
    def tags(self) -> Mapping[str, str] | None:
        """The desired tag set, or None when the spec declares none."""
        tags = self.document.get("tags")
        return None if tags is None else cast("Mapping[str, str]", tags)

    def create_document(self) -> dict[str, object]:
        """Return the CreateHarness members, exactly as the stack declared them."""
        return dict(self.document)

    def update_document(self, previous: HarnessSpec) -> dict[str, object]:
        """Return every mutable member as the full desired state for UpdateHarness (PATCH).

        Sending every declared member, not only those that changed, is what corrects drift from
        a change made outside CloudFormation. A member the previous spec declared and this one
        omits is refused: PATCH leaves an omitted member untouched, so dropping it would leave
        the live Harness disagreeing with its template.
        """
        dropped = sorted(
            (set(previous.document) - set(self.document)) - _CREATE_ONLY,
        )
        if dropped:
            raise HarnessResourceError(
                f"HarnessSpec no longer declares {dropped}; removing a member in place is not "
                "supported. Change harnessName to replace the Harness instead."
            )
        return {
            key: {"optionalValue": value} if key in _OPTIONAL_VALUE_WRAPPED else value
            for key, value in self.document.items()
            if key not in _CREATE_ONLY
        }


def parse_spec(text: object) -> HarnessSpec:
    """Parse the `HarnessSpec` property text into a validated spec."""
    if not isinstance(text, str) or not text.strip():
        raise HarnessResourceError(f"{SPEC_PROPERTY} must be a non-empty JSON string.")
    try:
        document: object = json.loads(text)
    except ValueError:
        # The parser's message quotes the document, which holds the system prompt.
        raise HarnessResourceError(f"{SPEC_PROPERTY} is not valid JSON.") from None
    return HarnessSpec(dict(_object(document, SPEC_PROPERTY)))


@dataclass(frozen=True, slots=True)
class _Properties:
    spec: HarnessSpec
    retention_days: int | None


def _properties(value: object, what: str) -> _Properties:
    properties = _object(value, what)
    unknown = sorted(
        set(properties)
        - _SERVICE_PROPERTIES
        - {SPEC_PROPERTY, RETENTION_PROPERTY, MEMORY_REVISION_PROPERTY}
    )
    if unknown:
        raise HarnessResourceError(f"{what} carries unsupported properties: {unknown}.")
    revision = properties.get(MEMORY_REVISION_PROPERTY)
    if revision is not None and (not isinstance(revision, str) or not revision.strip()):
        raise HarnessResourceError(f"{MEMORY_REVISION_PROPERTY} must be non-empty text.")
    return _Properties(
        spec=parse_spec(properties.get(SPEC_PROPERTY)),
        retention_days=_retention(properties.get(RETENTION_PROPERTY)),
    )


def _retention(value: object) -> int | None:
    if value is None:
        return None
    days = int(value) if isinstance(value, str) and value.isdigit() else None
    if days not in _RETENTION_DAYS:
        raise HarnessResourceError(
            f"{RETENTION_PROPERTY} must be a retention CloudWatch Logs accepts."
        )
    return days


def runtime_log_group(runtime_arn: str, harness_name: str) -> str:
    """Name the log group a Harness's runtime writes, from the runtime ARN GetHarness reports.

    Refuses a runtime that is not this Harness's own (`harness_<name>-<suffix>`), so retention is
    never applied to another runtime's group, and the provider's grant can name exactly that
    prefix.
    """
    match = _RUNTIME_ARN.fullmatch(runtime_arn)
    if match is None or not match["runtime"].startswith(f"harness_{harness_name}-"):
        raise HarnessResourceError(
            f"Harness {harness_name} reports no runtime of its own, so its log group is unknown."
        )
    return f"{_RUNTIME_LOG_GROUP_PREFIX}{match['runtime']}-{_RUNTIME_ENDPOINT}"


def _text(event: Mapping[str, object], name: str) -> str:
    value = event.get(name)
    if not isinstance(value, str) or not value.strip():
        raise HarnessResourceError(f"The callback has no {name}.")
    return value


@dataclass(frozen=True, slots=True)
class CallbackEvent:
    """One validated custom-resource callback."""

    request_type: RequestType
    request_id: str
    stack_id: str
    logical_resource_id: str
    physical_resource_id: str
    """Empty for Create; otherwise the id this provider returned earlier."""
    spec: HarnessSpec
    previous_spec: HarnessSpec | None = None
    """The spec before this Update; None for Create and Delete."""
    runtime_log_retention_days: int | None = None
    """Retention to apply to the runtime's log group once the Harness is READY; None skips it."""


def parse_event(event: object) -> CallbackEvent:
    """Validate a CloudFormation callback and parse its stringified properties."""
    mapping = _object(event, "The callback event")
    raw_type = _text(mapping, "RequestType")
    try:
        request_type = RequestType(raw_type)
    except ValueError:
        raise HarnessResourceError("RequestType is outside Create, Update and Delete.") from None
    physical = "" if request_type is RequestType.CREATE else _text(mapping, "PhysicalResourceId")
    previous = (
        _properties(mapping.get("OldResourceProperties"), "OldResourceProperties").spec
        if request_type is RequestType.UPDATE
        else None
    )
    current = _properties(mapping.get("ResourceProperties"), "ResourceProperties")
    return CallbackEvent(
        request_type=request_type,
        request_id=_text(mapping, "RequestId"),
        stack_id=_text(mapping, "StackId"),
        logical_resource_id=_text(mapping, "LogicalResourceId"),
        physical_resource_id=physical,
        spec=current.spec,
        previous_spec=previous,
        runtime_log_retention_days=current.retention_days,
    )


def client_token(event: CallbackEvent, operation: str) -> str:
    """Derive the idempotency token for one control-plane operation of one callback.

    CloudFormation keeps the RequestId when Lambda retries a callback, so a retried Create names
    the Harness the first attempt started rather than creating a second one. The stack and
    logical ids keep two resources' tokens apart; the operation keeps one callback's create and
    update apart.
    """
    parts = (event.stack_id, event.logical_resource_id, event.request_id, operation)
    material = "\n".join((_CLIENT_TOKEN_LABEL, *parts))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def is_harness_id(value: str) -> bool:
    """Whether a physical resource id names a Harness, rather than a failed create's placeholder."""
    return _HARNESS_ID.fullmatch(value) is not None
