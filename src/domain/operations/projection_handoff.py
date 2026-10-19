# SPDX-License-Identifier: MIT-0
"""Pure ADR-075 §2.5 projection-handoff callback decisions and canonical identities.

Everything here is deterministic: the closed CloudFormation envelope and property
validation, the six-state resolver rows and listed edges, the canonical JSON
encoder the §2.5 hash formulas pin (distinct from ``canonical_authority_bytes``),
the candidate stack-update/transition/decommission token derivations, the five
closed ``STATE`` disposition forms, and the immutable audit payload bytes. The
adapter owns clocks, DynamoDB, and HTTP; this module owns every byte it writes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

PROTOCOL_VERSION: Final = 1
STATE_RECORD_KEY: Final = "STATE"
PHYSICAL_ID_PREFIX: Final = "projection-handoff:v1"
ABSENT: Final = "ABSENT"

_STACK_UPDATE_LABEL: Final = "projection-handoff-stack-update/v1"
_TRANSITION_LABEL: Final = "projection-handoff-transition/v1"
_DECOMMISSION_LABEL: Final = "projection-handoff-decommission/v1"

_TOKEN_TEXT: Final = re.compile(r"\A[0-9a-f]{64}\Z")
_INSTANT_TEXT: Final = re.compile(r"\A[0-9]{20}\Z")
_DECIMAL_TEXT: Final = re.compile(r"\A(?:0|[1-9][0-9]*)\Z")


class ProjectionHandoffError(ValueError):
    """A handoff callback, retained row, or audit violates the closed §2.5 contract."""


class ResourceKind(StrEnum):
    """The three and only three private handoff custom resources."""

    PREPARE = "prepare"
    COMMIT = "commit"
    DECOMMISSION = "decommission"


class PrepareMode(StrEnum):
    """Closed prepare mode selection; replay alone is read-only."""

    TRANSITION = "transition"
    BOOTSTRAP = "bootstrap"
    RECREATE = "recreate"
    REPLAY = "replay"


class Disposition(StrEnum):
    """Closed dispositions of the single retained handoff ``STATE`` row."""

    ACTIVE = "ACTIVE"
    PREPARED = "PREPARED"
    ROLLING_BACK = "ROLLING_BACK"
    DECOMMISSIONING = "DECOMMISSIONING"
    DECOMMISSIONED = "DECOMMISSIONED"


class AuditPhase(StrEnum):
    """Closed immutable audit phases under ``AUDIT#<token>#<phase>``."""

    PREPARED = "PREPARED"
    COMMITTED = "COMMITTED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    DECOMMISSIONING = "DECOMMISSIONING"
    DECOMMISSIONED = "DECOMMISSIONED"


class RollbackOwner(StrEnum):
    """Which resource established ``ROLLING_BACK`` for the token."""

    PREPARE = "prepare"
    COMMIT = "commit"


@dataclass(frozen=True, slots=True)
class ResolverControls:
    """One six-state resolver row; no caller supplies the three controls independently."""

    projection_composition: str
    delivery_mapping_enabled: bool
    semantic_writer_mapping_enabled: bool


RESOLVER_CONTROLS: Final[Mapping[str, ResolverControls]] = {
    "INLINE_ACTIVE": ResolverControls("inline", True, False),
    "INLINE_QUIESCED": ResolverControls("inline", False, False),
    "QUEUED_QUIESCED": ResolverControls("queued", False, False),
    "QUEUED_ACCUMULATE": ResolverControls("queued", True, False),
    "QUEUED_ACTIVE": ResolverControls("queued", True, True),
    "QUEUED_DRAIN": ResolverControls("queued", False, True),
}

SIX_STATES: Final = frozenset(RESOLVER_CONTROLS)

LISTED_EDGES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        # Forward edges.
        ("INLINE_ACTIVE", "INLINE_QUIESCED"),
        ("INLINE_QUIESCED", "QUEUED_QUIESCED"),
        ("QUEUED_QUIESCED", "QUEUED_ACCUMULATE"),
        ("QUEUED_ACCUMULATE", "QUEUED_ACTIVE"),
        # Rollback edges.
        ("QUEUED_ACTIVE", "QUEUED_DRAIN"),
        ("QUEUED_DRAIN", "QUEUED_QUIESCED"),
        ("QUEUED_QUIESCED", "INLINE_QUIESCED"),
        ("INLINE_QUIESCED", "INLINE_ACTIVE"),
        # Emergency park and its sole restoration edge.
        ("QUEUED_ACTIVE", "QUEUED_QUIESCED"),
        ("QUEUED_QUIESCED", "QUEUED_DRAIN"),
    }
)

# The states a stack may bootstrap (from ABSENT) or recreate (from DECOMMISSIONED) into. The
# listed edges exist to hand an inline writer's work over to the queue without two automatic
# writers overlapping. Neither source has a writer to hand over: ABSENT means no Ingestion stack
# ever committed in this environment, and DECOMMISSIONED is written only after the old stack's
# application resources (its functions, mappings and queues) were deleted, because every one of
# them depends on prepare. Every resource of the new stack is created between prepare and commit
# in its first composition, so QUEUED_ACTIVE needs no drain. Amended 2026-10-19 (fresh-env run 1).
FIRST_DEPLOYMENT_STATES: Final = frozenset({"INLINE_ACTIVE", "QUEUED_ACTIVE"})


def partition_key(environment: str) -> str:
    """Return the sole deployment-handoff authority partition for one environment."""
    if not environment.strip():
        raise ProjectionHandoffError("environment must be nonblank")
    return f"PROJECTION_HANDOFF#{environment}"


def audit_record_key(token: str, phase: AuditPhase) -> str:
    """Return the immutable audit record key for one token and phase."""
    if not _TOKEN_TEXT.match(token):
        raise ProjectionHandoffError("audit token must be 64 lowercase hex characters")
    return f"AUDIT#{token}#{phase.value}"


def canonical_json(value: object) -> bytes:
    """Encode the §2.5 canonical JSON form.

    UTF-8, array order preserved, strings unchanged (never ASCII-escaped),
    booleans lowercase, integers in minimal decimal, no insignificant
    whitespace. Object members keep the insertion order this module fixes.
    This is deliberately not ``canonical_authority_bytes``.
    """
    return _canonical_text(value).encode("utf-8")


def _canonical_text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list | tuple):
        items = cast("list[object] | tuple[object, ...]", value)
        return "[" + ",".join(_canonical_text(item) for item in items) + "]"
    if isinstance(value, dict):
        members = cast("dict[object, object]", value)
        parts: list[str] = []
        for key, member in members.items():
            if not isinstance(key, str):
                raise ProjectionHandoffError("canonical JSON object keys must be text")
            parts.append(f"{json.dumps(key, ensure_ascii=False)}:{_canonical_text(member)}")
        return "{" + ",".join(parts) + "}"
    raise ProjectionHandoffError("value is outside the canonical JSON forms")


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class StackUpdateIdentityRequest:
    """The three callback identifiers the stack-update identity hashes."""

    stack_id: str
    logical_resource_id: str
    request_id: str


def stack_update_identity(request: StackUpdateIdentityRequest) -> str:
    """Derive the fresh candidate stack-update identity for one prepare callback."""
    return _sha256_hex(
        canonical_json(
            [
                _STACK_UPDATE_LABEL,
                request.stack_id,
                request.logical_resource_id,
                request.request_id,
            ]
        )
    )


@dataclass(frozen=True, slots=True)
class TransitionTokenRequest:
    """The exact ordered inputs of the candidate transition token."""

    environment: str
    stack_identity: str
    from_disposition: str
    from_state: str
    requested_state: str
    expected_revision: int
    stack_update_identity: str


def candidate_transition_token(request: TransitionTokenRequest) -> str:
    """Derive the fresh candidate transition token; prepare alone computes this."""
    return _sha256_hex(
        canonical_json(
            [
                _TRANSITION_LABEL,
                request.environment,
                request.stack_identity,
                request.from_disposition,
                request.from_state,
                request.requested_state,
                request.expected_revision,
                request.stack_update_identity,
            ]
        )
    )


@dataclass(frozen=True, slots=True)
class DecommissionTokenRequest:
    """The exact ordered inputs of the deterministic decommission token."""

    environment: str
    stack_identity: str
    state: str
    revision: int
    last_transition_id: str


def decommission_token(request: DecommissionTokenRequest) -> str:
    """Derive the deterministic decommission token from the complete ``ACTIVE`` row."""
    return _sha256_hex(
        canonical_json(
            [
                _DECOMMISSION_LABEL,
                request.environment,
                request.stack_identity,
                request.state,
                request.revision,
                request.last_transition_id,
            ]
        )
    )


def stack_identity_hash(stack_identity: str) -> str:
    """Hash one stack identity for the deletion-guard physical ID."""
    if not stack_identity.strip():
        raise ProjectionHandoffError("stack identity must be nonblank")
    return _sha256_hex(stack_identity.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class PhysicalId:
    """One parsed token-specific handoff physical resource ID."""

    environment: str
    kind: ResourceKind
    token: str


def physical_id_text(physical: PhysicalId) -> str:
    """Render ``projection-handoff:v1:<environment>:<kind>:<token>``."""
    return f"{PHYSICAL_ID_PREFIX}:{physical.environment}:{physical.kind.value}:{physical.token}"


@dataclass(frozen=True, slots=True)
class ParsePhysicalIdRequest:
    """One incoming physical ID with the identity it must belong to."""

    value: str
    environment: str
    kind: ResourceKind


def parse_physical_id(request: ParsePhysicalIdRequest) -> PhysicalId:
    """Accept only this environment's token-specific physical ID for the resource kind."""
    prefix = f"{PHYSICAL_ID_PREFIX}:{request.environment}:{request.kind.value}:"
    if not request.value.startswith(prefix):
        raise ProjectionHandoffError("physical resource ID is outside the closed form")
    token = request.value[len(prefix) :]
    if not _TOKEN_TEXT.match(token):
        raise ProjectionHandoffError("physical resource ID token must be 64 lowercase hex")
    return PhysicalId(request.environment, request.kind, token)


# A Create that fails before it decides a token-specific identity answers with this marker. The
# prefix is not ``projection-handoff:v1:`` followed by an environment, so no environment name
# (``failed`` included) can collide with it, and the RequestId charset excludes ``:``.
FAILED_CREATE_PREFIX: Final = "projection-handoff:v1-failed:"
_REQUEST_ID_TEXT: Final = re.compile(r"\A[0-9A-Za-z-]+\Z")
_UNKNOWN_REQUEST: Final = "unknown"


def failed_create_physical_id(request_id: str) -> str:
    """Render the marker for an undecided failed Create; an unparseable RequestId is ``unknown``."""
    named = request_id if _REQUEST_ID_TEXT.match(request_id) else _UNKNOWN_REQUEST
    return f"{FAILED_CREATE_PREFIX}{named}"


def failed_create_request_id(value: str) -> str | None:
    """Return the failed Create's RequestId when ``value`` is exactly the marker, else ``None``."""
    if not value.startswith(FAILED_CREATE_PREFIX):
        return None
    request_id = value[len(FAILED_CREATE_PREFIX) :]
    return request_id if _REQUEST_ID_TEXT.match(request_id) else None


# --- Closed callback envelope and property maps -------------------------------------------------

# The official request envelope plus the top-level ``ServiceToken`` that a Lambda-backed
# callback carries on the wire alongside its ``ResourceProperties`` copy.
_BASE_ENVELOPE_KEYS: Final = frozenset(
    {
        "RequestType",
        "ServiceToken",
        "ResponseURL",
        "StackId",
        "RequestId",
        "ResourceType",
        "LogicalResourceId",
        "ResourceProperties",
    }
)
_SHARED_PROPERTY_KEYS: Final = frozenset(
    {"ServiceToken", "protocolVersion", "resourceKind", "environment", "stackIdentity"}
)
_TARGET_PROPERTY_KEYS: Final = _SHARED_PROPERTY_KEYS | {
    "requestedState",
    "projectionComposition",
    "deliveryMappingEnabled",
    "semanticWriterMappingEnabled",
}
_COMMIT_PROPERTY_KEYS: Final = _TARGET_PROPERTY_KEYS | {
    "mode",
    "transitionToken",
    "fromDisposition",
    "fromState",
    "expectedRevision",
}
_FROM_DISPOSITIONS: Final = frozenset({ABSENT, "ACTIVE", "DECOMMISSIONED"})


@dataclass(frozen=True, slots=True)
class PrepareProperties:
    """The closed ``ProjectionHandoffPrepare`` property map."""

    environment: str
    stack_identity: str
    requested_state: str
    projection_composition: str
    delivery_mapping_enabled: bool
    semantic_writer_mapping_enabled: bool


@dataclass(frozen=True, slots=True)
class CommitProperties:
    """The closed ``ProjectionHandoffCommit`` property map; ``Fn::GetAtt`` values are text."""

    environment: str
    stack_identity: str
    requested_state: str
    projection_composition: str
    delivery_mapping_enabled: bool
    semantic_writer_mapping_enabled: bool
    mode: str
    transition_token: str
    from_disposition: str
    from_state: str
    expected_revision: str


@dataclass(frozen=True, slots=True)
class GuardProperties:
    """The closed ``ProjectionHandoffDeletionGuard`` property map."""

    environment: str
    stack_identity: str


type CallbackProperties = PrepareProperties | CommitProperties | GuardProperties


@dataclass(frozen=True, slots=True)
class CallbackEnvelope:
    """One validated callback against the official closed request envelope.

    The raw property maps are retained beside their parsed forms so a caller can
    enforce byte-identity requirements (the deletion-guard update) over exactly
    what CloudFormation delivered, including ``ServiceToken``.
    """

    request_type: str
    response_url: str
    stack_id: str
    request_id: str
    resource_type: str
    logical_resource_id: str
    physical_resource_id: str
    resource_kind: ResourceKind
    properties: CallbackProperties
    old_properties: CallbackProperties | None
    raw_properties: Mapping[str, object]
    raw_old_properties: Mapping[str, object] | None


def _text_member(document: Mapping[str, object], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ProjectionHandoffError(f"{name} must be nonblank text")
    return value


# CloudFormation delivers every custom-resource property value as text: a template `true` arrives
# as "true" and a template `1` as "1". The closed wire form is therefore the canonical text
# rendering, and nothing else: no other spelling, and no native JSON type, which the wire never
# carries.
_WIRE_BOOLEANS: Final = {"true": True, "false": False}
_WIRE_PROTOCOL_VERSION: Final = str(PROTOCOL_VERSION)


def _bool_member(document: Mapping[str, object], name: str) -> bool:
    value = document.get(name)
    if not isinstance(value, str) or value not in _WIRE_BOOLEANS:
        raise ProjectionHandoffError(f'{name} must be the wire boolean "true" or "false"')
    return _WIRE_BOOLEANS[value]


def _require_protocol_version(document: Mapping[str, object]) -> None:
    value = document.get("protocolVersion")
    if not isinstance(value, str) or value != _WIRE_PROTOCOL_VERSION:
        raise ProjectionHandoffError('protocolVersion must be exactly the wire text "1"')


def _string_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProjectionHandoffError(f"{name} must be an object")
    mapping = cast("Mapping[object, object]", value)
    if any(not isinstance(key, str) for key in mapping):
        raise ProjectionHandoffError(f"{name} keys must be text")
    return cast("Mapping[str, object]", mapping)


@dataclass(frozen=True, slots=True)
class _PropertyMapRequest:
    document: Mapping[str, object]
    environment: str
    stack_id: str


def _shared_property_checks(request: _PropertyMapRequest, expected_kind: ResourceKind) -> None:
    document = request.document
    _require_protocol_version(document)
    _text_member(document, "ServiceToken")
    if document.get("resourceKind") != expected_kind.value:
        raise ProjectionHandoffError("resourceKind is outside the closed vocabulary")
    if _text_member(document, "environment") != request.environment:
        raise ProjectionHandoffError("environment does not match this provider deployment")
    if _text_member(document, "stackIdentity") != request.stack_id:
        raise ProjectionHandoffError("stackIdentity must equal the top-level StackId")


def _resolved_controls(document: Mapping[str, object]) -> tuple[str, ResolverControls]:
    requested_state = _text_member(document, "requestedState")
    controls = RESOLVER_CONTROLS.get(requested_state)
    if controls is None:
        raise ProjectionHandoffError("requestedState is outside the six-state vocabulary")
    supplied = ResolverControls(
        _text_member(document, "projectionComposition"),
        _bool_member(document, "deliveryMappingEnabled"),
        _bool_member(document, "semanticWriterMappingEnabled"),
    )
    if supplied != controls:
        raise ProjectionHandoffError("resolver controls were supplied independently")
    return requested_state, controls


def _parse_prepare_properties(request: _PropertyMapRequest) -> PrepareProperties:
    document = request.document
    if frozenset(document) != _TARGET_PROPERTY_KEYS:
        raise ProjectionHandoffError("prepare properties differ from the closed map")
    _shared_property_checks(request, ResourceKind.PREPARE)
    requested_state, controls = _resolved_controls(document)
    return PrepareProperties(
        environment=request.environment,
        stack_identity=request.stack_id,
        requested_state=requested_state,
        projection_composition=controls.projection_composition,
        delivery_mapping_enabled=controls.delivery_mapping_enabled,
        semantic_writer_mapping_enabled=controls.semantic_writer_mapping_enabled,
    )


def _commit_shape_checks(properties: CommitProperties) -> None:
    mode, from_disposition, from_state = (
        properties.mode,
        properties.from_disposition,
        properties.from_state,
    )
    if mode == PrepareMode.BOOTSTRAP.value:
        valid = from_disposition == ABSENT and from_state == ABSENT
        valid = valid and properties.expected_revision == "0"
    elif mode in (PrepareMode.TRANSITION.value, PrepareMode.REPLAY.value):
        valid = from_disposition == "ACTIVE" and from_state in SIX_STATES
    else:
        valid = from_disposition == "DECOMMISSIONED" and from_state == "DECOMMISSIONED"
    if not valid:
        raise ProjectionHandoffError("commit propagation values are mutually inconsistent")


def _parse_commit_properties(request: _PropertyMapRequest) -> CommitProperties:
    document = request.document
    if frozenset(document) != _COMMIT_PROPERTY_KEYS:
        raise ProjectionHandoffError("commit properties differ from the closed map")
    _shared_property_checks(request, ResourceKind.COMMIT)
    requested_state, controls = _resolved_controls(document)
    mode = _text_member(document, "mode")
    if mode not in {member.value for member in PrepareMode}:
        raise ProjectionHandoffError("mode is outside the closed vocabulary")
    transition_token = _text_member(document, "transitionToken")
    if not _TOKEN_TEXT.match(transition_token):
        raise ProjectionHandoffError("transitionToken must be 64 lowercase hex characters")
    from_disposition = _text_member(document, "fromDisposition")
    if from_disposition not in _FROM_DISPOSITIONS:
        raise ProjectionHandoffError("fromDisposition is outside the closed vocabulary")
    from_state = _text_member(document, "fromState")
    if from_state not in SIX_STATES | {ABSENT, "DECOMMISSIONED"}:
        raise ProjectionHandoffError("fromState is outside the closed vocabulary")
    expected_revision = _text_member(document, "expectedRevision")
    if not _DECIMAL_TEXT.match(expected_revision):
        raise ProjectionHandoffError("expectedRevision must be canonical decimal text")
    properties = CommitProperties(
        environment=request.environment,
        stack_identity=request.stack_id,
        requested_state=requested_state,
        projection_composition=controls.projection_composition,
        delivery_mapping_enabled=controls.delivery_mapping_enabled,
        semantic_writer_mapping_enabled=controls.semantic_writer_mapping_enabled,
        mode=mode,
        transition_token=transition_token,
        from_disposition=from_disposition,
        from_state=from_state,
        expected_revision=expected_revision,
    )
    _commit_shape_checks(properties)
    return properties


def _parse_guard_properties(request: _PropertyMapRequest) -> GuardProperties:
    document = request.document
    if frozenset(document) != _SHARED_PROPERTY_KEYS:
        raise ProjectionHandoffError("deletion-guard properties differ from the closed map")
    _shared_property_checks(request, ResourceKind.DECOMMISSION)
    return GuardProperties(environment=request.environment, stack_identity=request.stack_id)


def _parse_properties(request: _PropertyMapRequest, kind: ResourceKind) -> CallbackProperties:
    if kind is ResourceKind.PREPARE:
        return _parse_prepare_properties(request)
    if kind is ResourceKind.COMMIT:
        return _parse_commit_properties(request)
    return _parse_guard_properties(request)


@dataclass(frozen=True, slots=True)
class ParseCallbackRequest:
    """One raw callback event and the deployment environment it must belong to."""

    event: object
    environment: str


def _envelope_keys(request_type: str) -> frozenset[str]:
    if request_type == "Create":
        return _BASE_ENVELOPE_KEYS
    if request_type == "Update":
        return _BASE_ENVELOPE_KEYS | {"PhysicalResourceId", "OldResourceProperties"}
    return _BASE_ENVELOPE_KEYS | {"PhysicalResourceId"}


def parse_callback(request: ParseCallbackRequest) -> CallbackEnvelope:
    """Accept only the official closed envelope and the resource's closed property map."""
    event = _string_mapping(request.event, "callback event")
    request_type = _text_member(event, "RequestType")
    if request_type not in {"Create", "Update", "Delete"}:
        raise ProjectionHandoffError("RequestType is outside the closed vocabulary")
    if frozenset(event) != _envelope_keys(request_type):
        raise ProjectionHandoffError("callback envelope differs from the official allowlist")
    stack_id = _text_member(event, "StackId")
    properties_document = _string_mapping(event["ResourceProperties"], "ResourceProperties")
    if _text_member(event, "ServiceToken") != properties_document.get("ServiceToken"):
        raise ProjectionHandoffError("top-level ServiceToken must equal the property ServiceToken")
    kind_value = properties_document.get("resourceKind")
    if kind_value not in {member.value for member in ResourceKind}:
        raise ProjectionHandoffError("resourceKind is outside the closed vocabulary")
    kind = ResourceKind(cast("str", kind_value))
    properties = _parse_properties(
        _PropertyMapRequest(properties_document, request.environment, stack_id), kind
    )
    old_properties: CallbackProperties | None = None
    old_document: Mapping[str, object] | None = None
    if request_type == "Update":
        old_document = _string_mapping(event["OldResourceProperties"], "OldResourceProperties")
        old_properties = _parse_properties(
            _PropertyMapRequest(old_document, request.environment, stack_id), kind
        )
    physical_resource_id = ""
    if request_type != "Create":
        physical_resource_id = _text_member(event, "PhysicalResourceId")
    return CallbackEnvelope(
        request_type=request_type,
        response_url=_text_member(event, "ResponseURL"),
        stack_id=stack_id,
        request_id=_text_member(event, "RequestId"),
        resource_type=_text_member(event, "ResourceType"),
        logical_resource_id=_text_member(event, "LogicalResourceId"),
        physical_resource_id=physical_resource_id,
        resource_kind=kind,
        properties=properties,
        old_properties=old_properties,
        raw_properties=properties_document,
        raw_old_properties=old_document,
    )


# --- Closed STATE row forms ----------------------------------------------------------------------

type RowValue = str | int | bool
type StateRowMap = Mapping[str, RowValue]


@dataclass(frozen=True, slots=True)
class ActiveRow:
    """The only form that claims the six-state controls are deployed."""

    state: str
    revision: int
    stack_identity: str
    last_transition_id: str
    last_transition_at: str


@dataclass(frozen=True, slots=True)
class PreparedRow:
    """One prepared mutation identity with its byte-exact prior snapshot."""

    mode: str
    token: str
    requested_state: str
    expected_revision: int
    stack_identity: str
    stack_update_identity: str
    projection_composition: str
    delivery_mapping_enabled: bool
    semantic_writer_mapping_enabled: bool
    prepared_at: str
    prior_snapshot: str


@dataclass(frozen=True, slots=True)
class RollingBackRow:
    """Every ``PREPARED`` field plus the rollback owner and instant."""

    mode: str
    token: str
    requested_state: str
    expected_revision: int
    stack_identity: str
    stack_update_identity: str
    projection_composition: str
    delivery_mapping_enabled: bool
    semantic_writer_mapping_enabled: bool
    prepared_at: str
    prior_snapshot: str
    rollback_owner: str
    rollback_at: str


@dataclass(frozen=True, slots=True)
class DecommissioningRow:
    """Stack deletion started; application resources may remain."""

    state: str
    revision: int
    stack_identity: str
    last_transition_id: str
    decommission_token: str
    decommissioning_at: str
    prior_snapshot: str


@dataclass(frozen=True, slots=True)
class DecommissionedRow:
    """The truthful terminal disposition with no active-state claim."""

    revision: int
    stack_identity: str
    last_transition_id: str
    decommissioned_at: str
    prior_snapshot: str


type StateRow = ActiveRow | PreparedRow | RollingBackRow | DecommissioningRow | DecommissionedRow


def _row_text(values: StateRowMap, name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ProjectionHandoffError(f"STATE row {name} must be nonblank text")
    return value


def _row_int(values: StateRowMap, name: str) -> int:
    value = values.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProjectionHandoffError(f"STATE row {name} must be a nonnegative integer")
    return value


def _row_bool(values: StateRowMap, name: str) -> bool:
    value = values.get(name)
    if not isinstance(value, bool):
        raise ProjectionHandoffError(f"STATE row {name} must be a boolean")
    return value


def _row_token(values: StateRowMap, name: str) -> str:
    value = _row_text(values, name)
    if not _TOKEN_TEXT.match(value):
        raise ProjectionHandoffError(f"STATE row {name} must be 64 lowercase hex characters")
    return value


def _row_instant(values: StateRowMap, name: str) -> str:
    value = _row_text(values, name)
    if not _INSTANT_TEXT.match(value):
        raise ProjectionHandoffError(f"STATE row {name} must be a 20-digit instant")
    return value


def _row_state(values: StateRowMap, name: str = "state") -> str:
    value = _row_text(values, name)
    if value not in SIX_STATES:
        raise ProjectionHandoffError(f"STATE row {name} is outside the six-state vocabulary")
    return value


_ACTIVE_ATTRIBUTES: Final = frozenset(
    {
        "protocol_version",
        "disposition",
        "state",
        "revision",
        "stack_identity",
        "last_transition_id",
        "last_transition_at",
    }
)
_PREPARED_ATTRIBUTES: Final = frozenset(
    {
        "protocol_version",
        "disposition",
        "mode",
        "token",
        "requested_state",
        "expected_revision",
        "stack_identity",
        "stack_update_identity",
        "projection_composition",
        "delivery_mapping_enabled",
        "semantic_writer_mapping_enabled",
        "prepared_at",
        "prior_snapshot",
    }
)
_ROLLING_BACK_ATTRIBUTES: Final = _PREPARED_ATTRIBUTES | {"rollback_owner", "rollback_at"}
_DECOMMISSIONING_ATTRIBUTES: Final = frozenset(
    {
        "protocol_version",
        "disposition",
        "state",
        "revision",
        "stack_identity",
        "last_transition_id",
        "decommission_token",
        "decommissioning_at",
        "prior_snapshot",
    }
)
_DECOMMISSIONED_ATTRIBUTES: Final = frozenset(
    {
        "protocol_version",
        "disposition",
        "revision",
        "stack_identity",
        "last_transition_id",
        "decommissioned_at",
        "prior_snapshot",
    }
)


def _require_attributes(values: StateRowMap, expected: frozenset[str]) -> None:
    if frozenset(values) != expected:
        raise ProjectionHandoffError("STATE row attributes differ from the closed form")
    version = values.get("protocol_version")
    if isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise ProjectionHandoffError("STATE row protocol_version must be exactly 1")


def _parse_active(values: StateRowMap) -> ActiveRow:
    _require_attributes(values, _ACTIVE_ATTRIBUTES)
    return ActiveRow(
        state=_row_state(values),
        revision=_row_int(values, "revision"),
        stack_identity=_row_text(values, "stack_identity"),
        last_transition_id=_row_token(values, "last_transition_id"),
        last_transition_at=_row_instant(values, "last_transition_at"),
    )


def _prepared_fields(values: StateRowMap) -> PreparedRow:
    mode = _row_text(values, "mode")
    if mode not in {
        PrepareMode.TRANSITION.value,
        PrepareMode.BOOTSTRAP.value,
        PrepareMode.RECREATE.value,
    }:
        raise ProjectionHandoffError("STATE row mode is outside the mutating vocabulary")
    requested_state = _row_state(values, "requested_state")
    controls = RESOLVER_CONTROLS[requested_state]
    row = PreparedRow(
        mode=mode,
        token=_row_token(values, "token"),
        requested_state=requested_state,
        expected_revision=_row_int(values, "expected_revision"),
        stack_identity=_row_text(values, "stack_identity"),
        stack_update_identity=_row_token(values, "stack_update_identity"),
        projection_composition=_row_text(values, "projection_composition"),
        delivery_mapping_enabled=_row_bool(values, "delivery_mapping_enabled"),
        semantic_writer_mapping_enabled=_row_bool(values, "semantic_writer_mapping_enabled"),
        prepared_at=_row_instant(values, "prepared_at"),
        prior_snapshot=_row_text(values, "prior_snapshot"),
    )
    supplied = ResolverControls(
        row.projection_composition,
        row.delivery_mapping_enabled,
        row.semantic_writer_mapping_enabled,
    )
    if supplied != controls:
        raise ProjectionHandoffError("STATE row resolver controls contradict the requested state")
    prepared_source_facts(row.mode, row.prior_snapshot)
    return row


def _parse_prepared(values: StateRowMap) -> PreparedRow:
    _require_attributes(values, _PREPARED_ATTRIBUTES)
    return _prepared_fields(values)


def _parse_rolling_back(values: StateRowMap) -> RollingBackRow:
    _require_attributes(values, _ROLLING_BACK_ATTRIBUTES)
    owner = _row_text(values, "rollback_owner")
    if owner not in {member.value for member in RollbackOwner}:
        raise ProjectionHandoffError("STATE row rollback_owner is outside the closed vocabulary")
    prepared = _prepared_fields(
        {name: value for name, value in values.items() if name in _PREPARED_ATTRIBUTES}
    )
    return RollingBackRow(
        mode=prepared.mode,
        token=prepared.token,
        requested_state=prepared.requested_state,
        expected_revision=prepared.expected_revision,
        stack_identity=prepared.stack_identity,
        stack_update_identity=prepared.stack_update_identity,
        projection_composition=prepared.projection_composition,
        delivery_mapping_enabled=prepared.delivery_mapping_enabled,
        semantic_writer_mapping_enabled=prepared.semantic_writer_mapping_enabled,
        prepared_at=prepared.prepared_at,
        prior_snapshot=prepared.prior_snapshot,
        rollback_owner=owner,
        rollback_at=_row_instant(values, "rollback_at"),
    )


def _parse_decommissioning(values: StateRowMap) -> DecommissioningRow:
    _require_attributes(values, _DECOMMISSIONING_ATTRIBUTES)
    row = DecommissioningRow(
        state=_row_state(values),
        revision=_row_int(values, "revision"),
        stack_identity=_row_text(values, "stack_identity"),
        last_transition_id=_row_token(values, "last_transition_id"),
        decommission_token=_row_token(values, "decommission_token"),
        decommissioning_at=_row_instant(values, "decommissioning_at"),
        prior_snapshot=_row_text(values, "prior_snapshot"),
    )
    prior = parse_snapshot(row.prior_snapshot)
    consistent = (
        isinstance(prior, ActiveRow)
        and prior.state == row.state
        and prior.revision == row.revision
        and prior.stack_identity == row.stack_identity
        and prior.last_transition_id == row.last_transition_id
    )
    if not consistent:
        raise ProjectionHandoffError("DECOMMISSIONING prior snapshot contradicts its row")
    return row


def _parse_decommissioned(values: StateRowMap) -> DecommissionedRow:
    _require_attributes(values, _DECOMMISSIONED_ATTRIBUTES)
    row = DecommissionedRow(
        revision=_row_int(values, "revision"),
        stack_identity=_row_text(values, "stack_identity"),
        last_transition_id=_row_token(values, "last_transition_id"),
        decommissioned_at=_row_instant(values, "decommissioned_at"),
        prior_snapshot=_row_text(values, "prior_snapshot"),
    )
    prior = parse_snapshot(row.prior_snapshot)
    if not isinstance(prior, ActiveRow) or row.revision != prior.revision + 1:
        raise ProjectionHandoffError("DECOMMISSIONED prior snapshot contradicts its row")
    return row


def parse_state_row(values: StateRowMap) -> StateRow:
    """Accept only one of the five complete closed ``STATE`` forms."""
    disposition = _row_text(values, "disposition")
    if disposition == Disposition.ACTIVE.value:
        return _parse_active(values)
    if disposition == Disposition.PREPARED.value:
        return _parse_prepared(values)
    if disposition == Disposition.ROLLING_BACK.value:
        return _parse_rolling_back(values)
    if disposition == Disposition.DECOMMISSIONING.value:
        return _parse_decommissioning(values)
    if disposition == Disposition.DECOMMISSIONED.value:
        return _parse_decommissioned(values)
    raise ProjectionHandoffError("STATE row disposition is outside the closed vocabulary")


def state_row_map(row: StateRow) -> dict[str, RowValue]:
    """Render one closed ``STATE`` form as its exact retained attribute map."""
    if isinstance(row, ActiveRow):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "disposition": Disposition.ACTIVE.value,
            "state": row.state,
            "revision": row.revision,
            "stack_identity": row.stack_identity,
            "last_transition_id": row.last_transition_id,
            "last_transition_at": row.last_transition_at,
        }
    if isinstance(row, PreparedRow | RollingBackRow):
        values: dict[str, RowValue] = {
            "protocol_version": PROTOCOL_VERSION,
            "disposition": (
                Disposition.ROLLING_BACK.value
                if isinstance(row, RollingBackRow)
                else Disposition.PREPARED.value
            ),
            "mode": row.mode,
            "token": row.token,
            "requested_state": row.requested_state,
            "expected_revision": row.expected_revision,
            "stack_identity": row.stack_identity,
            "stack_update_identity": row.stack_update_identity,
            "projection_composition": row.projection_composition,
            "delivery_mapping_enabled": row.delivery_mapping_enabled,
            "semantic_writer_mapping_enabled": row.semantic_writer_mapping_enabled,
            "prepared_at": row.prepared_at,
            "prior_snapshot": row.prior_snapshot,
        }
        if isinstance(row, RollingBackRow):
            values["rollback_owner"] = row.rollback_owner
            values["rollback_at"] = row.rollback_at
        return values
    if isinstance(row, DecommissioningRow):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "disposition": Disposition.DECOMMISSIONING.value,
            "state": row.state,
            "revision": row.revision,
            "stack_identity": row.stack_identity,
            "last_transition_id": row.last_transition_id,
            "decommission_token": row.decommission_token,
            "decommissioning_at": row.decommissioning_at,
            "prior_snapshot": row.prior_snapshot,
        }
    return {
        "protocol_version": PROTOCOL_VERSION,
        "disposition": Disposition.DECOMMISSIONED.value,
        "revision": row.revision,
        "stack_identity": row.stack_identity,
        "last_transition_id": row.last_transition_id,
        "decommissioned_at": row.decommissioned_at,
        "prior_snapshot": row.prior_snapshot,
    }


# --- Byte-exact prior snapshots ------------------------------------------------------------------


def snapshot_text(row: ActiveRow | DecommissionedRow | None) -> str:
    """Encode the byte-exact prior snapshot: ``ABSENT`` or the complete prior row."""
    if row is None:
        return canonical_json(ABSENT).decode("utf-8")
    if isinstance(row, ActiveRow):
        document: dict[str, object] = {
            "disposition": Disposition.ACTIVE.value,
            "state": row.state,
            "revision": row.revision,
            "stack_identity": row.stack_identity,
            "last_transition_id": row.last_transition_id,
            "last_transition_at": row.last_transition_at,
        }
        return canonical_json(document).decode("utf-8")
    document = {
        "disposition": Disposition.DECOMMISSIONED.value,
        "revision": row.revision,
        "stack_identity": row.stack_identity,
        "last_transition_id": row.last_transition_id,
        "decommissioned_at": row.decommissioned_at,
        "prior_snapshot": row.prior_snapshot,
    }
    return canonical_json(document).decode("utf-8")


def parse_snapshot(text: str) -> ActiveRow | DecommissionedRow | None:
    """Decode one byte-exact prior snapshot; ``None`` means ``ABSENT``."""
    try:
        decoded = cast("object", json.loads(text))
    except ValueError as error:
        raise ProjectionHandoffError("prior snapshot is not valid JSON") from error
    if decoded == ABSENT:
        if text != canonical_json(ABSENT).decode("utf-8"):
            raise ProjectionHandoffError("prior snapshot bytes are not canonical")
        return None
    document = _string_mapping(decoded, "prior snapshot")
    disposition = document.get("disposition")
    row: ActiveRow | DecommissionedRow
    if disposition == Disposition.ACTIVE.value:
        row = ActiveRow(
            state=_row_state(cast("StateRowMap", document)),
            revision=_row_int(cast("StateRowMap", document), "revision"),
            stack_identity=_row_text(cast("StateRowMap", document), "stack_identity"),
            last_transition_id=_row_token(cast("StateRowMap", document), "last_transition_id"),
            last_transition_at=_row_instant(cast("StateRowMap", document), "last_transition_at"),
        )
    elif disposition == Disposition.DECOMMISSIONED.value:
        row = DecommissionedRow(
            revision=_row_int(cast("StateRowMap", document), "revision"),
            stack_identity=_row_text(cast("StateRowMap", document), "stack_identity"),
            last_transition_id=_row_token(cast("StateRowMap", document), "last_transition_id"),
            decommissioned_at=_row_instant(cast("StateRowMap", document), "decommissioned_at"),
            prior_snapshot=_row_text(cast("StateRowMap", document), "prior_snapshot"),
        )
    else:
        raise ProjectionHandoffError("prior snapshot disposition is outside the closed forms")
    if snapshot_text(row) != text:
        raise ProjectionHandoffError("prior snapshot bytes are not canonical")
    return row


@dataclass(frozen=True, slots=True)
class SourceFacts:
    """The exact from-facts one prepared mutation was computed over."""

    from_disposition: str
    from_state: str
    expected_revision: int


def prepared_source_facts(mode: str, prior_snapshot: str) -> SourceFacts:
    """Derive the from-facts a prepared row's mode and snapshot pin."""
    snapshot = parse_snapshot(prior_snapshot)
    if mode == PrepareMode.BOOTSTRAP.value:
        if snapshot is not None:
            raise ProjectionHandoffError("bootstrap requires the ABSENT prior snapshot")
        return SourceFacts(ABSENT, ABSENT, 0)
    if mode == PrepareMode.TRANSITION.value:
        if not isinstance(snapshot, ActiveRow):
            raise ProjectionHandoffError("transition requires the prior ACTIVE snapshot")
        return SourceFacts("ACTIVE", snapshot.state, snapshot.revision)
    if mode == PrepareMode.RECREATE.value:
        if not isinstance(snapshot, DecommissionedRow):
            raise ProjectionHandoffError("recreation requires the prior DECOMMISSIONED snapshot")
        return SourceFacts("DECOMMISSIONED", "DECOMMISSIONED", snapshot.revision)
    raise ProjectionHandoffError("mode is outside the mutating vocabulary")


def committed_revision(mode: str, expected_revision: int) -> int:
    """First bootstrap commits revision 0; transition and recreation commit expected+1."""
    if mode == PrepareMode.BOOTSTRAP.value:
        return 0
    return expected_revision + 1


# --- Prepare and commit decisions ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrepareData:
    """The closed prepare ``Data`` values behind the commit ``Fn::GetAtt`` seam."""

    mode: str
    transition_token: str
    from_disposition: str
    from_state: str
    expected_revision: int

    def document(self) -> dict[str, object]:
        """Render the byte-exact prepare ``Data`` object."""
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "mode": self.mode,
            "transitionToken": self.transition_token,
            "fromDisposition": self.from_disposition,
            "fromState": self.from_state,
            "expectedRevision": str(self.expected_revision),
        }


@dataclass(frozen=True, slots=True)
class PrepareReplayDecision:
    """Same-state replay: read-only, zero ``STATE`` and zero ``AUDIT`` mutation."""

    data: PrepareData
    physical_resource_id: str


@dataclass(frozen=True, slots=True)
class PrepareMutationDecision:
    """One prepared mutation identity, or its already-completed idempotent retry."""

    mode: str
    token: str
    stack_update_identity: str
    source: SourceFacts
    prior_snapshot: str
    already_prepared: bool
    prepared_at: str
    data: PrepareData
    physical_resource_id: str


type PrepareDecision = PrepareReplayDecision | PrepareMutationDecision


@dataclass(frozen=True, slots=True)
class PrepareDecisionRequest:
    """One validated prepare callback with the strongly read ``STATE`` row."""

    envelope: CallbackEnvelope
    row: StateRow | None


def _prepare_properties(envelope: CallbackEnvelope) -> PrepareProperties:
    if not isinstance(envelope.properties, PrepareProperties):
        raise ProjectionHandoffError("callback does not carry prepare properties")
    return envelope.properties


def _prepare_mutation(
    request: PrepareDecisionRequest, mode: PrepareMode, source: SourceFacts
) -> PrepareMutationDecision:
    envelope = request.envelope
    properties = _prepare_properties(envelope)
    update_identity = stack_update_identity(
        StackUpdateIdentityRequest(
            envelope.stack_id, envelope.logical_resource_id, envelope.request_id
        )
    )
    token = candidate_transition_token(
        TransitionTokenRequest(
            environment=properties.environment,
            stack_identity=envelope.stack_id,
            from_disposition=source.from_disposition,
            from_state=source.from_state,
            requested_state=properties.requested_state,
            expected_revision=source.expected_revision,
            stack_update_identity=update_identity,
        )
    )
    snapshot = (
        snapshot_text(None)
        if request.row is None
        else snapshot_text(cast("ActiveRow | DecommissionedRow", request.row))
    )
    data = PrepareData(
        mode=mode.value,
        transition_token=token,
        from_disposition=source.from_disposition,
        from_state=source.from_state,
        expected_revision=source.expected_revision,
    )
    return PrepareMutationDecision(
        mode=mode.value,
        token=token,
        stack_update_identity=update_identity,
        source=source,
        prior_snapshot=snapshot,
        already_prepared=False,
        prepared_at="",
        data=data,
        physical_resource_id=physical_id_text(
            PhysicalId(properties.environment, ResourceKind.PREPARE, token)
        ),
    )


def _prepare_retry(request: PrepareDecisionRequest, row: PreparedRow) -> PrepareMutationDecision:
    """Recognize only the byte-identical completed prepare mutation as a retry."""
    envelope = request.envelope
    properties = _prepare_properties(envelope)
    source = prepared_source_facts(row.mode, row.prior_snapshot)
    update_identity = stack_update_identity(
        StackUpdateIdentityRequest(
            envelope.stack_id, envelope.logical_resource_id, envelope.request_id
        )
    )
    candidate = candidate_transition_token(
        TransitionTokenRequest(
            environment=properties.environment,
            stack_identity=envelope.stack_id,
            from_disposition=source.from_disposition,
            from_state=source.from_state,
            requested_state=properties.requested_state,
            expected_revision=source.expected_revision,
            stack_update_identity=update_identity,
        )
    )
    same = (
        candidate == row.token
        and row.stack_identity == envelope.stack_id
        and row.requested_state == properties.requested_state
    )
    if not same:
        raise ProjectionHandoffError("a different prepared mutation holds the STATE row")
    data = PrepareData(
        mode=row.mode,
        transition_token=row.token,
        from_disposition=source.from_disposition,
        from_state=source.from_state,
        expected_revision=source.expected_revision,
    )
    return PrepareMutationDecision(
        mode=row.mode,
        token=row.token,
        stack_update_identity=row.stack_update_identity,
        source=source,
        prior_snapshot=row.prior_snapshot,
        already_prepared=True,
        prepared_at=row.prepared_at,
        data=data,
        physical_resource_id=physical_id_text(
            PhysicalId(properties.environment, ResourceKind.PREPARE, row.token)
        ),
    )


def decide_prepare(request: PrepareDecisionRequest) -> PrepareDecision:
    """Select exactly one prepare mode from the strongly read ``STATE`` row."""
    envelope = request.envelope
    properties = _prepare_properties(envelope)
    row = request.row
    if row is None:
        if properties.requested_state not in FIRST_DEPLOYMENT_STATES:
            raise ProjectionHandoffError(
                "ABSENT permits only bootstrap to INLINE_ACTIVE or QUEUED_ACTIVE"
            )
        return _prepare_mutation(request, PrepareMode.BOOTSTRAP, SourceFacts(ABSENT, ABSENT, 0))
    if isinstance(row, ActiveRow):
        if row.state == properties.requested_state:
            if row.stack_identity != envelope.stack_id:
                raise ProjectionHandoffError("same-state replay requires the same stack identity")
            data = PrepareData(
                mode=PrepareMode.REPLAY.value,
                transition_token=row.last_transition_id,
                from_disposition="ACTIVE",
                from_state=row.state,
                expected_revision=row.revision,
            )
            return PrepareReplayDecision(
                data=data,
                physical_resource_id=physical_id_text(
                    PhysicalId(properties.environment, ResourceKind.PREPARE, row.last_transition_id)
                ),
            )
        if (row.state, properties.requested_state) not in LISTED_EDGES:
            raise ProjectionHandoffError("requested transition is not a listed edge")
        return _prepare_mutation(
            request, PrepareMode.TRANSITION, SourceFacts("ACTIVE", row.state, row.revision)
        )
    if isinstance(row, DecommissionedRow):
        if properties.requested_state not in FIRST_DEPLOYMENT_STATES:
            raise ProjectionHandoffError(
                "DECOMMISSIONED permits only recreation to INLINE_ACTIVE or QUEUED_ACTIVE"
            )
        if row.stack_identity == envelope.stack_id:
            raise ProjectionHandoffError("same-stack recreation is forbidden")
        return _prepare_mutation(
            request,
            PrepareMode.RECREATE,
            SourceFacts("DECOMMISSIONED", "DECOMMISSIONED", row.revision),
        )
    if isinstance(row, PreparedRow):
        return _prepare_retry(request, row)
    raise ProjectionHandoffError("STATE row disposition does not permit prepare")


@dataclass(frozen=True, slots=True)
class BuildPreparedRowRequest:
    """One prepared mutation with the instant captured immediately before its CAS."""

    decision: PrepareMutationDecision
    properties: PrepareProperties
    prepared_at: str


def build_prepared_row(request: BuildPreparedRowRequest) -> PreparedRow:
    """Assemble the complete closed ``PREPARED`` form for one prepared mutation."""
    decision, properties = request.decision, request.properties
    return PreparedRow(
        mode=decision.mode,
        token=decision.token,
        requested_state=properties.requested_state,
        expected_revision=decision.source.expected_revision,
        stack_identity=properties.stack_identity,
        stack_update_identity=decision.stack_update_identity,
        projection_composition=properties.projection_composition,
        delivery_mapping_enabled=properties.delivery_mapping_enabled,
        semantic_writer_mapping_enabled=properties.semantic_writer_mapping_enabled,
        prepared_at=request.prepared_at,
        prior_snapshot=decision.prior_snapshot,
    )


@dataclass(frozen=True, slots=True)
class CommitData:
    """The closed commit ``Data`` values."""

    mode: str
    transition_token: str
    state: str
    revision: int
    outcome: str

    def document(self) -> dict[str, object]:
        """Render the byte-exact commit ``Data`` object."""
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "mode": self.mode,
            "transitionToken": self.transition_token,
            "disposition": Disposition.ACTIVE.value,
            "state": self.state,
            "revision": self.revision,
            "outcome": self.outcome,
        }


@dataclass(frozen=True, slots=True)
class CommitReplayDecision:
    """Same-state replay: read-only, zero ``STATE`` and zero ``AUDIT`` mutation."""

    data: CommitData
    physical_resource_id: str


@dataclass(frozen=True, slots=True)
class CommitMutationDecision:
    """One conditioned ``PREPARED`` consumption, or its already-committed retry."""

    prepared: PreparedRow
    state: str
    revision: int
    already_committed: bool
    last_transition_at: str
    data: CommitData
    physical_resource_id: str


type CommitDecision = CommitReplayDecision | CommitMutationDecision


@dataclass(frozen=True, slots=True)
class CommitDecisionRequest:
    """One validated commit callback with the strongly read ``STATE`` row."""

    envelope: CallbackEnvelope
    row: StateRow | None


def _commit_properties(envelope: CallbackEnvelope) -> CommitProperties:
    if not isinstance(envelope.properties, CommitProperties):
        raise ProjectionHandoffError("callback does not carry commit properties")
    return envelope.properties


def _decide_commit_replay(request: CommitDecisionRequest) -> CommitReplayDecision:
    properties = _commit_properties(request.envelope)
    row = request.row
    if not isinstance(row, ActiveRow):
        raise ProjectionHandoffError("replay commit requires the complete exact ACTIVE row")
    exact = (
        row.stack_identity == request.envelope.stack_id
        and row.state == properties.requested_state
        and properties.transition_token == row.last_transition_id
        and properties.from_state == row.state
        and properties.expected_revision == str(row.revision)
    )
    if not exact:
        raise ProjectionHandoffError("replay commit propagation values differ from ACTIVE")
    data = CommitData(
        mode=PrepareMode.REPLAY.value,
        transition_token=row.last_transition_id,
        state=row.state,
        revision=row.revision,
        outcome="replayed",
    )
    return CommitReplayDecision(
        data=data,
        physical_resource_id=physical_id_text(
            PhysicalId(properties.environment, ResourceKind.COMMIT, row.last_transition_id)
        ),
    )


def _prepared_matches_commit(row: PreparedRow, properties: CommitProperties) -> bool:
    source = prepared_source_facts(row.mode, row.prior_snapshot)
    return (
        row.token == properties.transition_token
        and row.mode == properties.mode
        and row.requested_state == properties.requested_state
        and row.expected_revision == int(properties.expected_revision)
        and row.stack_identity == properties.stack_identity
        and row.projection_composition == properties.projection_composition
        and row.delivery_mapping_enabled == properties.delivery_mapping_enabled
        and row.semantic_writer_mapping_enabled == properties.semantic_writer_mapping_enabled
        and source.from_disposition == properties.from_disposition
        and source.from_state == properties.from_state
    )


def decide_commit(request: CommitDecisionRequest) -> CommitDecision:
    """Verify the prepared or active snapshot against the ``Fn::GetAtt`` propagation."""
    properties = _commit_properties(request.envelope)
    if properties.mode == PrepareMode.REPLAY.value:
        return _decide_commit_replay(request)
    revision = committed_revision(properties.mode, int(properties.expected_revision))
    row = request.row
    data = CommitData(
        mode=properties.mode,
        transition_token=properties.transition_token,
        state=properties.requested_state,
        revision=revision,
        outcome="committed",
    )
    physical = physical_id_text(
        PhysicalId(properties.environment, ResourceKind.COMMIT, properties.transition_token)
    )
    if isinstance(row, PreparedRow):
        if not _prepared_matches_commit(row, properties):
            raise ProjectionHandoffError("PREPARED row differs from the commit propagation")
        return CommitMutationDecision(
            prepared=row,
            state=properties.requested_state,
            revision=revision,
            already_committed=False,
            last_transition_at="",
            data=data,
            physical_resource_id=physical,
        )
    if isinstance(row, ActiveRow) and row.last_transition_id == properties.transition_token:
        exact = (
            row.state == properties.requested_state
            and row.revision == revision
            and row.stack_identity == properties.stack_identity
        )
        if not exact:
            raise ProjectionHandoffError("committed ACTIVE row differs from the propagation")
        return CommitMutationDecision(
            prepared=PreparedRow(
                mode=properties.mode,
                token=properties.transition_token,
                requested_state=properties.requested_state,
                expected_revision=int(properties.expected_revision),
                stack_identity=properties.stack_identity,
                stack_update_identity="",
                projection_composition=properties.projection_composition,
                delivery_mapping_enabled=properties.delivery_mapping_enabled,
                semantic_writer_mapping_enabled=properties.semantic_writer_mapping_enabled,
                prepared_at="",
                prior_snapshot="",
            ),
            state=row.state,
            revision=row.revision,
            already_committed=True,
            last_transition_at=row.last_transition_at,
            data=data,
            physical_resource_id=physical,
        )
    raise ProjectionHandoffError("STATE row disposition does not permit commit")


@dataclass(frozen=True, slots=True)
class BuildActiveRowRequest:
    """One committed transition with the instant captured immediately before its write."""

    prepared: PreparedRow
    state: str
    revision: int
    last_transition_at: str


def build_active_row(request: BuildActiveRowRequest) -> ActiveRow:
    """Assemble the complete closed ``ACTIVE`` form for one committed transition."""
    return ActiveRow(
        state=request.state,
        revision=request.revision,
        stack_identity=request.prepared.stack_identity,
        last_transition_id=request.prepared.token,
        last_transition_at=request.last_transition_at,
    )


@dataclass(frozen=True, slots=True)
class BuildRollingBackRowRequest:
    """One rollback claim with the instant captured immediately before its CAS."""

    prepared: PreparedRow
    owner: RollbackOwner
    rollback_at: str


def build_rolling_back_row(request: BuildRollingBackRowRequest) -> RollingBackRow:
    """Assemble the complete closed ``ROLLING_BACK`` form for one owned rollback."""
    prepared = request.prepared
    return RollingBackRow(
        mode=prepared.mode,
        token=prepared.token,
        requested_state=prepared.requested_state,
        expected_revision=prepared.expected_revision,
        stack_identity=prepared.stack_identity,
        stack_update_identity=prepared.stack_update_identity,
        projection_composition=prepared.projection_composition,
        delivery_mapping_enabled=prepared.delivery_mapping_enabled,
        semantic_writer_mapping_enabled=prepared.semantic_writer_mapping_enabled,
        prepared_at=prepared.prepared_at,
        prior_snapshot=prepared.prior_snapshot,
        rollback_owner=request.owner.value,
        rollback_at=request.rollback_at,
    )


@dataclass(frozen=True, slots=True)
class BuildDecommissioningRowRequest:
    """One decommission claim with the instant captured immediately before its CAS."""

    active: ActiveRow
    decommission_token: str
    decommissioning_at: str


def build_decommissioning_row(request: BuildDecommissioningRowRequest) -> DecommissioningRow:
    """Assemble the complete closed ``DECOMMISSIONING`` form for one guard deletion."""
    active = request.active
    return DecommissioningRow(
        state=active.state,
        revision=active.revision,
        stack_identity=active.stack_identity,
        last_transition_id=active.last_transition_id,
        decommission_token=request.decommission_token,
        decommissioning_at=request.decommissioning_at,
        prior_snapshot=snapshot_text(active),
    )


@dataclass(frozen=True, slots=True)
class BuildDecommissionedRowRequest:
    """One terminal decommission with the instant captured immediately before its write."""

    decommissioning: DecommissioningRow
    decommissioned_at: str


def build_decommissioned_row(request: BuildDecommissionedRowRequest) -> DecommissionedRow:
    """Assemble the complete closed terminal ``DECOMMISSIONED`` form."""
    row = request.decommissioning
    return DecommissionedRow(
        revision=row.revision + 1,
        stack_identity=row.stack_identity,
        last_transition_id=row.decommission_token,
        decommissioned_at=request.decommissioned_at,
        prior_snapshot=row.prior_snapshot,
    )


# --- Immutable audit payload bytes ---------------------------------------------------------------

_DECOMMISSION_MODE: Final = "decommission"


@dataclass(frozen=True, slots=True)
class _AuditFacts:
    environment: str
    token: str
    phase: AuditPhase
    mode: str
    stack_identity: str
    from_disposition: str
    from_state: str
    from_revision: int
    to_disposition: str
    to_state: str
    to_revision: int
    transition_instant: str
    projection_composition: str
    delivery_mapping_enabled: bool
    semantic_writer_mapping_enabled: bool
    prior_snapshot: str
    stack_update_identity: str = ""
    requested_state: str = ""


def _audit_payload(facts: _AuditFacts) -> bytes:
    document: dict[str, object] = {
        "protocolVersion": PROTOCOL_VERSION,
        "environment": facts.environment,
        "token": facts.token,
        "phase": facts.phase.value,
        "mode": facts.mode,
        "stackIdentity": facts.stack_identity,
        "fromDisposition": facts.from_disposition,
        "fromState": facts.from_state,
        "fromRevision": facts.from_revision,
        "toDisposition": facts.to_disposition,
        "toState": facts.to_state,
        "toRevision": facts.to_revision,
    }
    if facts.stack_update_identity:
        document["stackUpdateIdentity"] = facts.stack_update_identity
    document["transitionInstant"] = facts.transition_instant
    if facts.requested_state:
        document["requestedState"] = facts.requested_state
    document["projectionComposition"] = facts.projection_composition
    document["deliveryMappingEnabled"] = facts.delivery_mapping_enabled
    document["semanticWriterMappingEnabled"] = facts.semantic_writer_mapping_enabled
    document["priorSnapshot"] = facts.prior_snapshot
    return canonical_json(document)


def prepared_audit_payload(environment: str, row: PreparedRow) -> bytes:
    """Encode the immutable ``PREPARED`` audit; its instant is ``prepared_at``."""
    source = prepared_source_facts(row.mode, row.prior_snapshot)
    return _audit_payload(
        _AuditFacts(
            environment=environment,
            token=row.token,
            phase=AuditPhase.PREPARED,
            mode=row.mode,
            stack_identity=row.stack_identity,
            from_disposition=source.from_disposition,
            from_state=source.from_state,
            from_revision=source.expected_revision,
            to_disposition=Disposition.PREPARED.value,
            to_state=row.requested_state,
            to_revision=row.expected_revision,
            transition_instant=row.prepared_at,
            projection_composition=row.projection_composition,
            delivery_mapping_enabled=row.delivery_mapping_enabled,
            semantic_writer_mapping_enabled=row.semantic_writer_mapping_enabled,
            prior_snapshot=row.prior_snapshot,
            stack_update_identity=row.stack_update_identity,
            requested_state=row.requested_state,
        )
    )


@dataclass(frozen=True, slots=True)
class CommittedAuditRequest:
    """The prepared facts and committed row one ``COMMITTED`` audit copies."""

    environment: str
    prepared: PreparedRow
    active: ActiveRow


def committed_audit_payload(request: CommittedAuditRequest) -> bytes:
    """Encode the immutable ``COMMITTED`` audit; its instant is ``last_transition_at``."""
    prepared, active = request.prepared, request.active
    source = prepared_source_facts(prepared.mode, prepared.prior_snapshot)
    return _audit_payload(
        _AuditFacts(
            environment=request.environment,
            token=prepared.token,
            phase=AuditPhase.COMMITTED,
            mode=prepared.mode,
            stack_identity=prepared.stack_identity,
            from_disposition=source.from_disposition,
            from_state=source.from_state,
            from_revision=source.expected_revision,
            to_disposition=Disposition.ACTIVE.value,
            to_state=active.state,
            to_revision=active.revision,
            transition_instant=active.last_transition_at,
            projection_composition=prepared.projection_composition,
            delivery_mapping_enabled=prepared.delivery_mapping_enabled,
            semantic_writer_mapping_enabled=prepared.semantic_writer_mapping_enabled,
            prior_snapshot=prepared.prior_snapshot,
            stack_update_identity=prepared.stack_update_identity,
            requested_state=prepared.requested_state,
        )
    )


def rolling_back_audit_payload(environment: str, row: RollingBackRow) -> bytes:
    """Encode the immutable ``ROLLING_BACK`` audit; its instant is ``rollback_at``."""
    if row.rollback_owner == RollbackOwner.COMMIT.value:
        from_disposition = Disposition.ACTIVE.value
        from_revision = committed_revision(row.mode, row.expected_revision)
    else:
        from_disposition = Disposition.PREPARED.value
        from_revision = row.expected_revision
    return _audit_payload(
        _AuditFacts(
            environment=environment,
            token=row.token,
            phase=AuditPhase.ROLLING_BACK,
            mode=row.mode,
            stack_identity=row.stack_identity,
            from_disposition=from_disposition,
            from_state=row.requested_state,
            from_revision=from_revision,
            to_disposition=Disposition.ROLLING_BACK.value,
            to_state=row.requested_state,
            to_revision=row.expected_revision,
            transition_instant=row.rollback_at,
            projection_composition=row.projection_composition,
            delivery_mapping_enabled=row.delivery_mapping_enabled,
            semantic_writer_mapping_enabled=row.semantic_writer_mapping_enabled,
            prior_snapshot=row.prior_snapshot,
            stack_update_identity=row.stack_update_identity,
            requested_state=row.requested_state,
        )
    )


@dataclass(frozen=True, slots=True)
class RollbackFacts:
    """Everything one ``ROLLED_BACK`` audit needs, owner-independent."""

    token: str
    mode: str
    stack_identity: str
    stack_update_identity: str
    requested_state: str
    expected_revision: int
    projection_composition: str
    delivery_mapping_enabled: bool
    semantic_writer_mapping_enabled: bool
    prior_snapshot: str
    rollback_at: str


def rollback_facts_from_row(row: RollingBackRow) -> RollbackFacts:
    """Project the owner-independent rollback facts from the live row."""
    return RollbackFacts(
        token=row.token,
        mode=row.mode,
        stack_identity=row.stack_identity,
        stack_update_identity=row.stack_update_identity,
        requested_state=row.requested_state,
        expected_revision=row.expected_revision,
        projection_composition=row.projection_composition,
        delivery_mapping_enabled=row.delivery_mapping_enabled,
        semantic_writer_mapping_enabled=row.semantic_writer_mapping_enabled,
        prior_snapshot=row.prior_snapshot,
        rollback_at=row.rollback_at,
    )


def _parsed_audit_document(payload: bytes, phase: AuditPhase) -> Mapping[str, object]:
    try:
        decoded = cast("object", json.loads(payload.decode("utf-8")))
    except ValueError as error:
        raise ProjectionHandoffError("audit payload is not valid JSON") from error
    document = _string_mapping(decoded, "audit payload")
    if canonical_json(document) != payload:
        raise ProjectionHandoffError("audit payload bytes are not canonical")
    if document.get("phase") != phase.value:
        raise ProjectionHandoffError("audit payload phase differs from its record key")
    return document


def rollback_facts_from_audit(payload: bytes) -> RollbackFacts:
    """Recover the rollback facts from the immutable ``ROLLING_BACK`` audit sibling."""
    document = _parsed_audit_document(payload, AuditPhase.ROLLING_BACK)
    values = cast("StateRowMap", document)
    return RollbackFacts(
        token=_row_token(values, "token"),
        mode=_row_text(values, "mode"),
        stack_identity=_row_text(values, "stackIdentity"),
        stack_update_identity=_row_token(values, "stackUpdateIdentity"),
        requested_state=_row_state(values, "requestedState"),
        expected_revision=_row_int(values, "toRevision"),
        projection_composition=_row_text(values, "projectionComposition"),
        delivery_mapping_enabled=_row_bool(values, "deliveryMappingEnabled"),
        semantic_writer_mapping_enabled=_row_bool(values, "semanticWriterMappingEnabled"),
        prior_snapshot=_row_text(values, "priorSnapshot"),
        rollback_at=_row_instant(values, "transitionInstant"),
    )


def prepared_row_from_audit(payload: bytes) -> PreparedRow:
    """Recover the complete prepared fields from the immutable ``PREPARED`` audit."""
    document = _parsed_audit_document(payload, AuditPhase.PREPARED)
    values = cast("StateRowMap", document)
    row = PreparedRow(
        mode=_row_text(values, "mode"),
        token=_row_token(values, "token"),
        requested_state=_row_state(values, "requestedState"),
        expected_revision=_row_int(values, "fromRevision"),
        stack_identity=_row_text(values, "stackIdentity"),
        stack_update_identity=_row_token(values, "stackUpdateIdentity"),
        projection_composition=_row_text(values, "projectionComposition"),
        delivery_mapping_enabled=_row_bool(values, "deliveryMappingEnabled"),
        semantic_writer_mapping_enabled=_row_bool(values, "semanticWriterMappingEnabled"),
        prepared_at=_row_instant(values, "transitionInstant"),
        prior_snapshot=_row_text(values, "priorSnapshot"),
    )
    return _parse_prepared(state_row_map(row))


def rolled_back_audit_payload(environment: str, facts: RollbackFacts) -> bytes:
    """Encode the immutable ``ROLLED_BACK`` audit; its instant is the rollback instant."""
    snapshot = parse_snapshot(facts.prior_snapshot)
    if snapshot is None:
        to_facts = (ABSENT, ABSENT, 0)
    elif isinstance(snapshot, ActiveRow):
        to_facts = (Disposition.ACTIVE.value, snapshot.state, snapshot.revision)
    else:
        to_facts = (Disposition.DECOMMISSIONED.value, "DECOMMISSIONED", snapshot.revision)
    return _audit_payload(
        _AuditFacts(
            environment=environment,
            token=facts.token,
            phase=AuditPhase.ROLLED_BACK,
            mode=facts.mode,
            stack_identity=facts.stack_identity,
            from_disposition=Disposition.ROLLING_BACK.value,
            from_state=facts.requested_state,
            from_revision=facts.expected_revision,
            to_disposition=to_facts[0],
            to_state=to_facts[1],
            to_revision=to_facts[2],
            transition_instant=facts.rollback_at,
            projection_composition=facts.projection_composition,
            delivery_mapping_enabled=facts.delivery_mapping_enabled,
            semantic_writer_mapping_enabled=facts.semantic_writer_mapping_enabled,
            prior_snapshot=facts.prior_snapshot,
            stack_update_identity=facts.stack_update_identity,
            requested_state=facts.requested_state,
        )
    )


def decommissioning_audit_payload(environment: str, row: DecommissioningRow) -> bytes:
    """Encode the immutable ``DECOMMISSIONING`` audit; its instant is ``decommissioning_at``."""
    controls = RESOLVER_CONTROLS[row.state]
    return _audit_payload(
        _AuditFacts(
            environment=environment,
            token=row.decommission_token,
            phase=AuditPhase.DECOMMISSIONING,
            mode=_DECOMMISSION_MODE,
            stack_identity=row.stack_identity,
            from_disposition=Disposition.ACTIVE.value,
            from_state=row.state,
            from_revision=row.revision,
            to_disposition=Disposition.DECOMMISSIONING.value,
            to_state=row.state,
            to_revision=row.revision,
            transition_instant=row.decommissioning_at,
            projection_composition=controls.projection_composition,
            delivery_mapping_enabled=controls.delivery_mapping_enabled,
            semantic_writer_mapping_enabled=controls.semantic_writer_mapping_enabled,
            prior_snapshot=row.prior_snapshot,
        )
    )


def decommissioned_audit_payload(environment: str, row: DecommissionedRow) -> bytes:
    """Encode the immutable terminal ``DECOMMISSIONED`` audit; its instant is retained."""
    prior = parse_snapshot(row.prior_snapshot)
    if not isinstance(prior, ActiveRow):
        raise ProjectionHandoffError("DECOMMISSIONED prior snapshot must be the prior ACTIVE")
    controls = RESOLVER_CONTROLS[prior.state]
    return _audit_payload(
        _AuditFacts(
            environment=environment,
            token=row.last_transition_id,
            phase=AuditPhase.DECOMMISSIONED,
            mode=_DECOMMISSION_MODE,
            stack_identity=row.stack_identity,
            from_disposition=Disposition.DECOMMISSIONING.value,
            from_state=prior.state,
            from_revision=prior.revision,
            to_disposition=Disposition.DECOMMISSIONED.value,
            to_state=Disposition.DECOMMISSIONED.value,
            to_revision=row.revision,
            transition_instant=row.decommissioned_at,
            projection_composition=controls.projection_composition,
            delivery_mapping_enabled=controls.delivery_mapping_enabled,
            semantic_writer_mapping_enabled=controls.semantic_writer_mapping_enabled,
            prior_snapshot=row.prior_snapshot,
        )
    )
