# SPDX-License-Identifier: MIT-0
"""ADR-075 §2.5 projection-handoff provider: envelopes, state machine, and audit bytes."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words
from requests import PreparedRequest, Response

from adapters.jobs.projection_handoff import (
    HANDOFF_DEPENDENCY,
    OPERATION,
    HandoffStore,
    ProviderRuntime,
    RunRequest,
    _deliverer,  # pyright: ignore[reportPrivateUsage]
    process,
    run,
)
from adapters.outbound.cloudformation_response import HttpsCustomResourceResponder
from adapters.outbound.https import HttpsSession
from adapters.outbound.telemetry.reporting import Report
from domain.operations.projection_handoff import (
    LISTED_EDGES,
    RESOLVER_CONTROLS,
    ActiveRow,
    CallbackEnvelope,
    ParseCallbackRequest,
    PrepareData,
    PrepareDecisionRequest,
    PrepareMutationDecision,
    PrepareReplayDecision,
    ProjectionHandoffError,
    SourceFacts,
    StackUpdateIdentityRequest,
    StateRow,
    TransitionTokenRequest,
    candidate_transition_token,
    canonical_json,
    decide_prepare,
    parse_callback,
    parse_state_row,
    stack_update_identity,
)
from domain.shared_kernel.errors import (
    DependencyError,
    ErrorClass,
    InvariantError,
    ValidationError,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

ROOT = Path(__file__).resolve().parents[2]
ENV = "development"
TABLE = "publications-fixture"
PARTITION = f"PROJECTION_HANDOFF#{ENV}"
STACK_A = (
    "arn:aws:cloudformation:eu-west-1:123456789012:stack/adlytics-ingestion-development/"
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
)
STACK_B = (
    "arn:aws:cloudformation:eu-west-1:123456789012:stack/adlytics-ingestion-development/"
    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
)
# A-TEST-LITERALS-001: a ServiceToken ARN; see docs/security-dispositions.md
SERVICE_TOKEN = (  # nosec B105
    "arn:aws:lambda:eu-west-1:123456789012:function:adlytics-development-ProjectionHandoffProvider"
)
RESPONSE_URL = "https://cloudformation-custom-resource-response.s3.amazonaws.com/fixture"
INSTANTS = [f"{index:020d}" for index in range(1, 13)]


# --- Recording fakes ------------------------------------------------------------------------------


def _conditional_error(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "fixture"}}, operation
    )


@guard_reserved_words
@dataclass
class _RecordingDynamo:
    """Evaluates exactly the conditional expressions the provider generates."""

    items: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict[tuple[str, str], dict[str, Any]]
    )
    gets: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    updates: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    deletes: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    @staticmethod
    def _key(request: dict[str, Any]) -> tuple[str, str]:
        key = cast("dict[str, dict[str, str]]", request["Key"])
        return key["partition_key"]["S"], key["record_key"]["S"]

    def get_item(self, **request: Any) -> dict[str, object]:
        self.gets.append(request)
        item = self.items.get(self._key(request))
        return {} if item is None else {"Item": item}

    @staticmethod
    def _holds(item: dict[str, Any] | None, request: dict[str, Any]) -> bool:
        expression = cast("str", request["ConditionExpression"])
        names = cast("dict[str, str]", request.get("ExpressionAttributeNames", {}))
        values = cast("dict[str, Any]", request.get("ExpressionAttributeValues", {}))

        def equals(clause: str) -> bool:
            name_placeholder, _, value_placeholder = clause.partition(" = ")
            attribute = names[name_placeholder.strip()]
            return item is not None and item.get(attribute) == values[value_placeholder.strip()]

        if expression == "attribute_not_exists(partition_key)":
            return item is None
        if " OR " in expression:
            _, _, right = expression.partition(" OR ")
            return item is None or equals(right)
        return item is not None and all(equals(clause) for clause in expression.split(" AND "))

    def update_item(self, **request: Any) -> dict[str, object]:
        self.updates.append(request)
        key = self._key(request)
        item = self.items.get(key)
        if not self._holds(item, request):
            raise _conditional_error("UpdateItem")
        names = cast("dict[str, str]", request.get("ExpressionAttributeNames", {}))
        values = cast("dict[str, Any]", request.get("ExpressionAttributeValues", {}))
        expression = cast("str", request["UpdateExpression"])
        set_part, _, remove_part = expression.partition(" REMOVE ")
        stored = (
            dict(item)
            if item is not None
            else {"partition_key": {"S": key[0]}, "record_key": {"S": key[1]}}
        )
        for assignment in set_part.removeprefix("SET ").split(", "):
            name_placeholder, _, value_placeholder = assignment.partition(" = ")
            stored[names[name_placeholder.strip()]] = values[value_placeholder.strip()]
        if remove_part:
            for name_placeholder in remove_part.split(", "):
                stored.pop(names[name_placeholder.strip()], None)
        self.items[key] = stored
        return {}

    def delete_item(self, **request: Any) -> dict[str, object]:
        self.deletes.append(request)
        key = self._key(request)
        item = self.items.get(key)
        if not self._holds(item, request):
            raise _conditional_error("DeleteItem")
        self.items.pop(key)
        return {}


@dataclass
class _RecordingResponder:
    deliveries: list[tuple[str, bytes]] = field(default_factory=list[tuple[str, bytes]])

    def __call__(self, url: str, body: bytes) -> None:
        self.deliveries.append((url, body))


def _runtime(dynamo: _RecordingDynamo, instants: list[str]) -> ProviderRuntime:
    queue = list(instants)
    store = HandoffStore(TABLE, ENV, cast("Any", dynamo))
    return ProviderRuntime(ENV, store, queue.pop)


def _invoke(event: dict[str, Any], runtime: ProviderRuntime) -> tuple[str, dict[str, Any]]:
    responder = _RecordingResponder()
    outcome = run(RunRequest(event=event, runtime=runtime, deliver=responder))
    assert len(responder.deliveries) == 1
    url, body = responder.deliveries[0]
    assert url == RESPONSE_URL
    return outcome, cast("dict[str, Any]", json.loads(body))


def _drive(
    dynamo: _RecordingDynamo, event: dict[str, Any], instant: str = INSTANTS[0]
) -> tuple[str, dict[str, Any]]:
    return _invoke(event, _runtime(dynamo, [instant]))


# --- Event builders -------------------------------------------------------------------------------


def _prepare_properties(requested: str = "INLINE_ACTIVE", stack: str = STACK_A) -> dict[str, Any]:
    controls = RESOLVER_CONTROLS[requested]
    return {
        # The CloudFormation wire form: every property value arrives as text.
        "ServiceToken": SERVICE_TOKEN,
        "protocolVersion": "1",
        "resourceKind": "prepare",
        "environment": ENV,
        "stackIdentity": stack,
        "requestedState": requested,
        "projectionComposition": controls.projection_composition,
        "deliveryMappingEnabled": _wire_bool(controls.delivery_mapping_enabled),
        "semanticWriterMappingEnabled": _wire_bool(controls.semantic_writer_mapping_enabled),
    }


def _wire_bool(value: bool) -> str:
    return "true" if value else "false"


def _commit_properties(
    data: dict[str, Any], requested: str = "INLINE_ACTIVE", stack: str = STACK_A
) -> dict[str, Any]:
    properties = _prepare_properties(requested, stack)
    properties["resourceKind"] = "commit"
    properties.update(
        {
            "mode": data["mode"],
            "transitionToken": data["transitionToken"],
            "fromDisposition": data["fromDisposition"],
            "fromState": data["fromState"],
            "expectedRevision": data["expectedRevision"],
        }
    )
    return properties


def _guard_properties(stack: str = STACK_A) -> dict[str, Any]:
    return {
        "ServiceToken": SERVICE_TOKEN,
        "protocolVersion": "1",
        "resourceKind": "decommission",
        "environment": ENV,
        "stackIdentity": stack,
    }


@dataclass(frozen=True, slots=True)
class _EventShape:
    request_type: str = "Create"
    stack: str = STACK_A
    request_id: str = "request-1"
    logical: str = "ProjectionHandoffPrepare"
    physical: str | None = None
    old: dict[str, Any] | None = None


def _event(properties: dict[str, Any], shape: _EventShape | None = None) -> dict[str, Any]:
    shape = shape if shape is not None else _EventShape()
    event: dict[str, Any] = {
        "RequestType": shape.request_type,
        # Lambda-backed callbacks carry the token at the top level as well as in the properties.
        "ServiceToken": SERVICE_TOKEN,
        "ResponseURL": RESPONSE_URL,
        "StackId": shape.stack,
        "RequestId": shape.request_id,
        "ResourceType": f"Custom::{shape.logical}",
        "LogicalResourceId": shape.logical,
        "ResourceProperties": properties,
    }
    if shape.physical is not None:
        event["PhysicalResourceId"] = shape.physical
    if shape.old is not None:
        event["OldResourceProperties"] = shape.old
    return event


def _state_item(dynamo: _RecordingDynamo) -> dict[str, Any] | None:
    return dynamo.items.get((PARTITION, "STATE"))


def _audit_bytes(dynamo: _RecordingDynamo, token: str, phase: str) -> bytes | None:
    item = dynamo.items.get((PARTITION, f"AUDIT#{token}#{phase}"))
    if item is None:
        return None
    return cast("str", item["payload"]["S"]).encode("utf-8")


def _write_counts(dynamo: _RecordingDynamo) -> tuple[int, int]:
    return len(dynamo.updates), len(dynamo.deletes)


# --- Lifecycle drivers ----------------------------------------------------------------------------


def _bootstrap(dynamo: _RecordingDynamo) -> dict[str, Any]:
    """Bootstrap to INLINE_ACTIVE; returns the prepare Data consumed by commit."""
    _, response = _drive(dynamo, _event(_prepare_properties()), INSTANTS[0])
    data = cast("dict[str, Any]", response["Data"])
    outcome, commit = _drive(
        dynamo,
        _event(
            _commit_properties(data),
            _EventShape(logical="ProjectionHandoffCommit"),
        ),
        INSTANTS[1],
    )
    assert outcome == "completed"
    assert commit["Data"]["outcome"] == "committed"
    return data


def _prepare_physical(token: str) -> str:
    return f"projection-handoff:v1:{ENV}:prepare:{token}"


def _commit_physical(token: str) -> str:
    return f"projection-handoff:v1:{ENV}:commit:{token}"


def _guard_physical(stack: str = STACK_A) -> str:
    digest = hashlib.sha256(stack.encode("utf-8")).hexdigest()
    return f"projection-handoff:v1:{ENV}:decommission:{digest}"


def _transition(
    dynamo: _RecordingDynamo, prior_data: dict[str, Any], requested: str = "INLINE_QUIESCED"
) -> dict[str, Any]:
    """Run one prepare+commit Update edge; returns the new prepare Data."""
    prior_token = cast("str", prior_data["transitionToken"])
    shape = _EventShape(
        request_type="Update",
        request_id="request-2",
        physical=_prepare_physical(prior_token),
        old=_prepare_properties(),
    )
    _, response = _invoke(
        _event(_prepare_properties(requested), shape), _runtime(dynamo, [INSTANTS[2]])
    )
    data = cast("dict[str, Any]", response["Data"])
    commit_shape = _EventShape(
        request_type="Update",
        request_id="request-3",
        logical="ProjectionHandoffCommit",
        physical=_commit_physical(prior_token),
        old=_commit_properties(prior_data),
    )
    outcome, _ = _invoke(
        _event(_commit_properties(data, requested), commit_shape),
        _runtime(dynamo, [INSTANTS[3]]),
    )
    assert outcome == "completed"
    return data


def _decommissioned(dynamo: _RecordingDynamo) -> tuple[dict[str, Any], str]:
    """Full delete: guard, commit no-op, prepare terminal; returns (data, decommission token)."""
    data = _bootstrap(dynamo)
    token = cast("str", data["transitionToken"])
    outcome, _ = _drive(
        dynamo,
        _event(
            _guard_properties(),
            _EventShape(
                request_type="Delete",
                logical="ProjectionHandoffDeletionGuard",
                physical=_guard_physical(),
            ),
        ),
        INSTANTS[4],
    )
    assert outcome == "completed"
    state = _state_item(dynamo)
    assert state is not None and state["disposition"]["S"] == "DECOMMISSIONING"
    decommission = state["decommission_token"]["S"]
    outcome, _ = _drive(
        dynamo,
        _event(
            _commit_properties(data),
            _EventShape(
                request_type="Delete",
                logical="ProjectionHandoffCommit",
                physical=_commit_physical(token),
            ),
        ),
        INSTANTS[5],
    )
    assert outcome == "completed"
    outcome, _ = _drive(
        dynamo,
        _event(
            _prepare_properties(),
            _EventShape(
                request_type="Delete",
                physical=_prepare_physical(token),
            ),
        ),
        INSTANTS[6],
    )
    assert outcome == "completed"
    return data, cast("str", decommission)


# --- Golden identity and Data bytes ---------------------------------------------------------------


def test_stack_update_identity_matches_pinned_canonical_bytes() -> None:
    pinned = (
        f'["projection-handoff-stack-update/v1","{STACK_A}","ProjectionHandoffPrepare","request-1"]'
    ).encode()
    expected = hashlib.sha256(pinned).hexdigest()
    derived = stack_update_identity(
        StackUpdateIdentityRequest(STACK_A, "ProjectionHandoffPrepare", "request-1")
    )
    assert derived == expected


def test_candidate_transition_token_matches_pinned_canonical_bytes() -> None:
    update_identity = stack_update_identity(
        StackUpdateIdentityRequest(STACK_A, "ProjectionHandoffPrepare", "request-1")
    )
    pinned = (
        f'["projection-handoff-transition/v1","{ENV}","{STACK_A}","ABSENT","ABSENT",'
        f'"INLINE_ACTIVE",0,"{update_identity}"]'
    ).encode()
    expected = hashlib.sha256(pinned).hexdigest()
    derived = candidate_transition_token(
        TransitionTokenRequest(
            environment=ENV,
            stack_identity=STACK_A,
            from_disposition="ABSENT",
            from_state="ABSENT",
            requested_state="INLINE_ACTIVE",
            expected_revision=0,
            stack_update_identity=update_identity,
        )
    )
    assert derived == expected


def _expected_bootstrap_token(requested: str = "INLINE_ACTIVE") -> str:
    update_identity = stack_update_identity(
        StackUpdateIdentityRequest(STACK_A, "ProjectionHandoffPrepare", "request-1")
    )
    return candidate_transition_token(
        TransitionTokenRequest(
            environment=ENV,
            stack_identity=STACK_A,
            from_disposition="ABSENT",
            from_state="ABSENT",
            requested_state=requested,
            expected_revision=0,
            stack_update_identity=update_identity,
        )
    )


def test_prepare_bootstrap_data_bytes_and_physical_id() -> None:
    dynamo = _RecordingDynamo()
    _, response = _drive(dynamo, _event(_prepare_properties()))
    token = _expected_bootstrap_token()
    assert response["PhysicalResourceId"] == _prepare_physical(token)
    assert (
        canonical_json(response["Data"])
        == (
            f'{{"protocolVersion":1,"mode":"bootstrap","transitionToken":"{token}",'
            f'"fromDisposition":"ABSENT","fromState":"ABSENT","expectedRevision":"0"}}'
        ).encode()
    )


def test_commit_bootstrap_data_bytes_and_physical_id() -> None:
    dynamo = _RecordingDynamo()
    _, response = _drive(dynamo, _event(_prepare_properties()), INSTANTS[0])
    data = cast("dict[str, Any]", response["Data"])
    token = cast("str", data["transitionToken"])
    _, commit = _drive(
        dynamo,
        _event(_commit_properties(data), _EventShape(logical="ProjectionHandoffCommit")),
        INSTANTS[1],
    )
    assert commit["PhysicalResourceId"] == _commit_physical(token)
    assert (
        canonical_json(commit["Data"])
        == (
            f'{{"protocolVersion":1,"mode":"bootstrap","transitionToken":"{token}",'
            f'"disposition":"ACTIVE","state":"INLINE_ACTIVE","revision":0,"outcome":"committed"}}'
        ).encode()
    )


def test_prepare_transition_data_shape() -> None:
    dynamo = _RecordingDynamo()
    data = _bootstrap(dynamo)
    new_data = _transition(dynamo, data)
    assert new_data["mode"] == "transition"
    assert new_data["fromDisposition"] == "ACTIVE"
    assert new_data["fromState"] == "INLINE_ACTIVE"
    assert new_data["expectedRevision"] == "0"
    state = _state_item(dynamo)
    assert state is not None
    assert state["state"]["S"] == "INLINE_QUIESCED"
    assert state["revision"]["N"] == "1"


def test_replay_prepare_and_commit_are_read_only_with_byte_exact_data() -> None:
    dynamo = _RecordingDynamo()
    data = _bootstrap(dynamo)
    token = cast("str", data["transitionToken"])
    state_before = json.dumps(_state_item(dynamo), sort_keys=True)
    keyspace_before = json.dumps(
        {f"{key[0]}|{key[1]}": value for key, value in dynamo.items.items()}, sort_keys=True
    )
    writes_before = _write_counts(dynamo)
    replay_shape = _EventShape(
        request_type="Update",
        request_id="request-9",
        physical=_prepare_physical(token),
        old=_prepare_properties(),
    )
    outcome, response = _invoke(_event(_prepare_properties(), replay_shape), _runtime(dynamo, []))
    assert outcome == "completed"
    assert response["PhysicalResourceId"] == _prepare_physical(token)
    assert (
        canonical_json(response["Data"])
        == (
            f'{{"protocolVersion":1,"mode":"replay","transitionToken":"{token}",'
            f'"fromDisposition":"ACTIVE","fromState":"INLINE_ACTIVE","expectedRevision":"0"}}'
        ).encode()
    )
    replay_data = cast("dict[str, Any]", response["Data"])
    commit_shape = _EventShape(
        request_type="Update",
        request_id="request-10",
        logical="ProjectionHandoffCommit",
        physical=_commit_physical(token),
        old=_commit_properties(data),
    )
    outcome, commit = _invoke(
        _event(_commit_properties(replay_data), commit_shape), _runtime(dynamo, [])
    )
    assert outcome == "completed"
    assert commit["PhysicalResourceId"] == _commit_physical(token)
    assert (
        canonical_json(commit["Data"])
        == (
            f'{{"protocolVersion":1,"mode":"replay","transitionToken":"{token}",'
            f'"disposition":"ACTIVE","state":"INLINE_ACTIVE","revision":0,"outcome":"replayed"}}'
        ).encode()
    )
    assert _write_counts(dynamo) == writes_before
    assert json.dumps(_state_item(dynamo), sort_keys=True) == state_before
    keyspace_after = json.dumps(
        {f"{key[0]}|{key[1]}": value for key, value in dynamo.items.items()}, sort_keys=True
    )
    assert keyspace_after == keyspace_before


def test_prepared_audit_payload_pins_canonical_bytes_with_transition_instant() -> None:
    dynamo = _RecordingDynamo()
    _drive(dynamo, _event(_prepare_properties()), INSTANTS[0])
    token = _expected_bootstrap_token()
    update_identity = stack_update_identity(
        StackUpdateIdentityRequest(STACK_A, "ProjectionHandoffPrepare", "request-1")
    )
    expected = (
        f'{{"protocolVersion":1,"environment":"{ENV}","token":"{token}","phase":"PREPARED",'
        f'"mode":"bootstrap","stackIdentity":"{STACK_A}","fromDisposition":"ABSENT",'
        f'"fromState":"ABSENT","fromRevision":0,"toDisposition":"PREPARED",'
        f'"toState":"INLINE_ACTIVE","toRevision":0,"stackUpdateIdentity":"{update_identity}",'
        f'"transitionInstant":"{INSTANTS[0]}","requestedState":"INLINE_ACTIVE",'
        f'"projectionComposition":"inline","deliveryMappingEnabled":true,'
        f'"semanticWriterMappingEnabled":false,"priorSnapshot":"\\"ABSENT\\""}}'
    ).encode()
    assert _audit_bytes(dynamo, token, "PREPARED") == expected


def test_committed_audit_payload_pins_canonical_bytes_with_transition_instant() -> None:
    dynamo = _RecordingDynamo()
    _bootstrap(dynamo)
    token = _expected_bootstrap_token()
    update_identity = stack_update_identity(
        StackUpdateIdentityRequest(STACK_A, "ProjectionHandoffPrepare", "request-1")
    )
    expected = (
        f'{{"protocolVersion":1,"environment":"{ENV}","token":"{token}","phase":"COMMITTED",'
        f'"mode":"bootstrap","stackIdentity":"{STACK_A}","fromDisposition":"ABSENT",'
        f'"fromState":"ABSENT","fromRevision":0,"toDisposition":"ACTIVE",'
        f'"toState":"INLINE_ACTIVE","toRevision":0,"stackUpdateIdentity":"{update_identity}",'
        f'"transitionInstant":"{INSTANTS[1]}","requestedState":"INLINE_ACTIVE",'
        f'"projectionComposition":"inline","deliveryMappingEnabled":true,'
        f'"semanticWriterMappingEnabled":false,"priorSnapshot":"\\"ABSENT\\""}}'
    ).encode()
    assert _audit_bytes(dynamo, token, "COMMITTED") == expected


def test_deletion_guard_physical_id_is_stack_identity_hash() -> None:
    dynamo = _RecordingDynamo()
    outcome, response = _drive(
        dynamo,
        _event(_guard_properties(), _EventShape(logical="ProjectionHandoffDeletionGuard")),
    )
    assert outcome == "completed"
    assert response["PhysicalResourceId"] == _guard_physical()
    assert "Data" not in response
    assert _write_counts(dynamo) == (0, 0)


# --- Closed envelope and property validation ------------------------------------------------------


def _parse(event: dict[str, Any]) -> None:
    parse_callback(ParseCallbackRequest(event, ENV))


def test_envelope_rejects_keys_outside_the_official_allowlist() -> None:
    event = _event(_prepare_properties())
    event["ServiceTimeout"] = "60"
    with pytest.raises(ProjectionHandoffError):
        _parse(event)


def test_envelope_requires_the_top_level_service_token_to_match_the_property_copy() -> None:
    """The first live Prepare callback proved the wire carries a top-level ServiceToken.

    An envelope without it, or with a token naming another provider, is not this stack's callback.
    """
    missing = _event(_prepare_properties())
    del missing["ServiceToken"]
    with pytest.raises(ProjectionHandoffError):
        _parse(missing)
    foreign = _event(_prepare_properties())
    foreign["ServiceToken"] = SERVICE_TOKEN.replace("function", "other")
    with pytest.raises(ProjectionHandoffError):
        _parse(foreign)


def test_create_rejects_physical_resource_id_and_old_properties() -> None:
    with_physical = _event(_prepare_properties(), _EventShape(physical=_prepare_physical("a" * 64)))
    with pytest.raises(ProjectionHandoffError):
        _parse(with_physical)
    with_old = _event(_prepare_properties(), _EventShape(old=_prepare_properties()))
    with pytest.raises(ProjectionHandoffError):
        _parse(with_old)


def test_update_requires_physical_resource_id_and_old_properties() -> None:
    missing_old = _event(
        _prepare_properties(),
        _EventShape(request_type="Update", physical=_prepare_physical("a" * 64)),
    )
    with pytest.raises(ProjectionHandoffError):
        _parse(missing_old)
    missing_physical = _event(
        _prepare_properties(), _EventShape(request_type="Update", old=_prepare_properties())
    )
    with pytest.raises(ProjectionHandoffError):
        _parse(missing_physical)


def test_delete_requires_physical_and_rejects_old_properties() -> None:
    missing_physical = _event(_prepare_properties(), _EventShape(request_type="Delete"))
    with pytest.raises(ProjectionHandoffError):
        _parse(missing_physical)
    with_old = _event(
        _prepare_properties(),
        _EventShape(
            request_type="Delete",
            physical=_prepare_physical("a" * 64),
            old=_prepare_properties(),
        ),
    )
    with pytest.raises(ProjectionHandoffError):
        _parse(with_old)


@pytest.mark.parametrize(
    "properties",
    [_prepare_properties(), None, _guard_properties()],
    ids=["prepare", "commit", "decommission"],
)
@pytest.mark.parametrize("defect", ["extra", "missing", "retyped", "blank"])
def test_closed_property_maps_reject_extra_missing_retyped_blank(
    properties: dict[str, Any] | None, defect: str
) -> None:
    document = dict(properties) if properties is not None else _commit_properties(_replay_data())
    if defect == "extra":
        document["unexpected"] = "value"
    elif defect == "missing":
        document.pop("environment")
    elif defect == "retyped":
        # A native integer is the template author's spelling, never the wire's.
        document["protocolVersion"] = 1
    else:
        document["environment"] = "   "
    with pytest.raises(ProjectionHandoffError):
        _parse(_event(document))


def _replay_data() -> dict[str, Any]:
    return {
        "mode": "replay",
        "transitionToken": "c" * 64,
        "fromDisposition": "ACTIVE",
        "fromState": "INLINE_ACTIVE",
        "expectedRevision": "0",
    }


@pytest.mark.parametrize("spelling", [True, "True", "TRUE", "1", " true", 1])
def test_boolean_controls_accept_only_the_canonical_wire_spelling(spelling: object) -> None:
    """CloudFormation stringifies custom-resource properties, so "true"/"false" is the form.

    A native boolean never arrives on the wire and a variant spelling is a retyped field.
    """
    document = _prepare_properties()
    assert document["deliveryMappingEnabled"] == "true"
    document["deliveryMappingEnabled"] = spelling
    with pytest.raises(ProjectionHandoffError):
        _parse(_event(document))


def test_stack_identity_must_equal_top_level_stack_id() -> None:
    document = _prepare_properties(stack=STACK_B)
    with pytest.raises(ProjectionHandoffError):
        _parse(_event(document))


def test_independently_supplied_resolver_controls_are_rejected() -> None:
    document = _prepare_properties("INLINE_ACTIVE")
    document["deliveryMappingEnabled"] = False
    with pytest.raises(ProjectionHandoffError):
        _parse(_event(document))


def test_unknown_request_type_and_state_are_rejected() -> None:
    event = _event(_prepare_properties())
    event["RequestType"] = "Read"
    with pytest.raises(ProjectionHandoffError):
        _parse(event)
    document = _prepare_properties()
    document["requestedState"] = "INLINE_PAUSED"
    document["projectionComposition"] = "inline"
    with pytest.raises(ProjectionHandoffError):
        _parse(_event(document))


# --- State machine: bootstrap, replay, transition, recreate ---------------------------------------


def _prepare_envelope(requested: str, stack: str = STACK_A) -> CallbackEnvelope:
    shape = _EventShape(stack=stack)
    return parse_callback(
        ParseCallbackRequest(_event(_prepare_properties(requested, stack), shape), ENV)
    )


SIX_STATES_IN_ORDER = list(RESOLVER_CONTROLS)
ABSENT_REFUSAL = "ABSENT permits only bootstrap to INLINE_ACTIVE or QUEUED_ACTIVE"


def test_bootstrap_from_absent_refuses_a_non_bootstrap_state_without_writes() -> None:
    dynamo = _RecordingDynamo()
    outcome, response = _drive(dynamo, _event(_prepare_properties("INLINE_QUIESCED")))
    assert outcome == "failed"
    assert response["Status"] == "FAILED"
    assert _write_counts(dynamo) == (0, 0)


@pytest.mark.parametrize("requested", ["INLINE_ACTIVE", "QUEUED_ACTIVE"])
def test_absent_bootstraps_directly_to_each_permitted_state(requested: str) -> None:
    """Fresh-env run 1 (2026-10-19): the shipped QUEUED_ACTIVE must bootstrap a new environment."""
    decision = decide_prepare(PrepareDecisionRequest(_prepare_envelope(requested), None))
    token = _expected_bootstrap_token(requested)
    update_identity = stack_update_identity(
        StackUpdateIdentityRequest(STACK_A, "ProjectionHandoffPrepare", "request-1")
    )
    assert decision == PrepareMutationDecision(
        mode="bootstrap",
        token=token,
        stack_update_identity=update_identity,
        source=SourceFacts("ABSENT", "ABSENT", 0),
        prior_snapshot='"ABSENT"',
        already_prepared=False,
        prepared_at="",
        data=PrepareData("bootstrap", token, "ABSENT", "ABSENT", 0),
        physical_resource_id=_prepare_physical(token),
    )


def test_absent_queued_active_bootstrap_commits_active_at_revision_zero() -> None:
    dynamo = _RecordingDynamo()
    outcome, response = _drive(dynamo, _event(_prepare_properties("QUEUED_ACTIVE")), INSTANTS[0])
    assert outcome == "completed"
    data = cast("dict[str, Any]", response["Data"])
    token = _expected_bootstrap_token("QUEUED_ACTIVE")
    assert data == {
        "protocolVersion": 1,
        "mode": "bootstrap",
        "transitionToken": token,
        "fromDisposition": "ABSENT",
        "fromState": "ABSENT",
        "expectedRevision": "0",
    }
    outcome, commit = _drive(
        dynamo,
        _event(
            _commit_properties(data, "QUEUED_ACTIVE"),
            _EventShape(logical="ProjectionHandoffCommit"),
        ),
        INSTANTS[1],
    )
    assert outcome == "completed"
    assert commit["Data"] == {
        "protocolVersion": 1,
        "mode": "bootstrap",
        "transitionToken": token,
        "disposition": "ACTIVE",
        "state": "QUEUED_ACTIVE",
        "revision": 0,
        "outcome": "committed",
    }
    assert _parsed_state(dynamo) == (ActiveRow("QUEUED_ACTIVE", 0, STACK_A, token, INSTANTS[1]))


@pytest.mark.parametrize(
    "requested", ["INLINE_QUIESCED", "QUEUED_QUIESCED", "QUEUED_ACCUMULATE", "QUEUED_DRAIN"]
)
def test_absent_refuses_every_other_requested_state(requested: str) -> None:
    with pytest.raises(ProjectionHandoffError) as refused:
        decide_prepare(PrepareDecisionRequest(_prepare_envelope(requested), None))
    assert str(refused.value) == ABSENT_REFUSAL


def _parsed_state(dynamo: _RecordingDynamo) -> StateRow:
    return parse_state_row(cast("Any", HandoffStore(TABLE, ENV, cast("Any", dynamo)).read_state()))


def _decommissioned_row() -> StateRow:
    dynamo = _RecordingDynamo()
    _decommissioned(dynamo)
    return _parsed_state(dynamo)


@pytest.mark.parametrize(
    "requested", ["INLINE_QUIESCED", "QUEUED_QUIESCED", "QUEUED_ACCUMULATE", "QUEUED_DRAIN"]
)
def test_decommissioned_refuses_every_other_requested_state(requested: str) -> None:
    row = _decommissioned_row()
    with pytest.raises(ProjectionHandoffError) as refused:
        decide_prepare(PrepareDecisionRequest(_prepare_envelope(requested, STACK_B), row))
    assert str(refused.value) == (
        "DECOMMISSIONED permits only recreation to INLINE_ACTIVE or QUEUED_ACTIVE"
    )


@pytest.mark.parametrize("requested", ["INLINE_ACTIVE", "QUEUED_ACTIVE"])
def test_decommissioned_still_refuses_same_stack_recreation(requested: str) -> None:
    row = _decommissioned_row()
    with pytest.raises(ProjectionHandoffError) as refused:
        decide_prepare(PrepareDecisionRequest(_prepare_envelope(requested, STACK_A), row))
    assert str(refused.value) == "same-stack recreation is forbidden"


def test_decommissioned_recreates_directly_to_queued_active_under_a_new_stack() -> None:
    """Redeploying a destroyed environment keeps the retained DECOMMISSIONED row (run-1 B1)."""
    dynamo = _RecordingDynamo()
    _decommissioned(dynamo)
    shape = _EventShape(stack=STACK_B, request_id="request-4")
    outcome, response = _invoke(
        _event(_prepare_properties("QUEUED_ACTIVE", STACK_B), shape),
        _runtime(dynamo, [INSTANTS[7]]),
    )
    assert outcome == "completed"
    data = cast("dict[str, Any]", response["Data"])
    assert {key: data[key] for key in ("mode", "fromDisposition", "fromState")} == {
        "mode": "recreate",
        "fromDisposition": "DECOMMISSIONED",
        "fromState": "DECOMMISSIONED",
    }
    assert data["expectedRevision"] == "1"
    commit_shape = _EventShape(
        stack=STACK_B, request_id="request-5", logical="ProjectionHandoffCommit"
    )
    outcome, commit = _invoke(
        _event(_commit_properties(data, "QUEUED_ACTIVE", STACK_B), commit_shape),
        _runtime(dynamo, [INSTANTS[8]]),
    )
    assert outcome == "completed"
    assert _parsed_state(dynamo) == (
        ActiveRow("QUEUED_ACTIVE", 2, STACK_B, cast("str", data["transitionToken"]), INSTANTS[8])
    )
    assert commit["Data"]["revision"] == 2


def test_the_listed_edges_are_unchanged() -> None:
    assert (
        frozenset(
            {
                ("INLINE_ACTIVE", "INLINE_QUIESCED"),
                ("INLINE_QUIESCED", "QUEUED_QUIESCED"),
                ("QUEUED_QUIESCED", "QUEUED_ACCUMULATE"),
                ("QUEUED_ACCUMULATE", "QUEUED_ACTIVE"),
                ("QUEUED_ACTIVE", "QUEUED_DRAIN"),
                ("QUEUED_DRAIN", "QUEUED_QUIESCED"),
                ("QUEUED_QUIESCED", "INLINE_QUIESCED"),
                ("INLINE_QUIESCED", "INLINE_ACTIVE"),
                ("QUEUED_ACTIVE", "QUEUED_QUIESCED"),
                ("QUEUED_QUIESCED", "QUEUED_DRAIN"),
            }
        )
        == LISTED_EDGES
    )


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (current, requested)
        for current in SIX_STATES_IN_ORDER
        for requested in SIX_STATES_IN_ORDER
        if current != requested and (current, requested) not in LISTED_EDGES
    ],
)
def test_active_still_refuses_every_unlisted_edge(current: str, requested: str) -> None:
    row = ActiveRow(current, 3, STACK_A, "c" * 64, INSTANTS[0])
    with pytest.raises(ProjectionHandoffError) as refused:
        decide_prepare(PrepareDecisionRequest(_prepare_envelope(requested), row))
    assert str(refused.value) == "requested transition is not a listed edge"


def _shipped_default_handoff_state() -> str:
    """Read the one ``projectionHandoffState`` every environment's shipped config carries."""
    source = (ROOT / "infra/lib/config/index.ts").read_text()
    # Exactly one literal in the whole config module: no per-environment value exists.
    assert len(re.findall(r"projectionHandoffState: '", source)) == 1
    block = re.search(
        r"^const DEFAULT_INGESTION_CONFIG: IngestionConfig = Object\.freeze\(\{\n(.*?)^\}\);$",
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert block is not None
    states = re.findall(r"^  projectionHandoffState: '([A-Z_]+)',$", block.group(1), re.MULTILINE)
    assert len(states) == 1
    # Every environment's ingestion spreads the default and overrides only the legacy reconcile.
    spread = re.search(
        r"^    ingestion: Object\.freeze\(\{\n"
        r"      \.\.\.DEFAULT_INGESTION_CONFIG,\n(.*?)^    \}\),$",
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert spread is not None
    assert spread.group(1) == (
        "      legacyPlanIntentReconcileEnabled: legacyPlanIntentReconcileEnabled(envName),\n"
    )
    return cast("str", states[0])


def test_the_shipped_default_handoff_state_is_a_permitted_absent_bootstrap() -> None:
    """Guard: every customer's first deploy prepares the shipped state against an ABSENT row.

    ``infra/test/adr-075-foundations.test.ts`` pins that the template synthesized from
    ``defaultConfig(<non-dev>)`` requests exactly this state; this test pins that the state
    machine accepts it from ``ABSENT`` and from another stack's ``DECOMMISSIONED`` row, and that
    dev's retained ``ACTIVE`` row in the same state still replays. Fresh-env run 1 (2026-10-19)
    failed because nothing tied the shipped value to the state machine.
    """
    state = _shipped_default_handoff_state()
    assert state == "QUEUED_ACTIVE"
    bootstrap = decide_prepare(PrepareDecisionRequest(_prepare_envelope(state), None))
    token = _expected_bootstrap_token(state)
    assert cast("PrepareMutationDecision", bootstrap).data == PrepareData(
        "bootstrap", token, "ABSENT", "ABSENT", 0
    )
    assert bootstrap.physical_resource_id == _prepare_physical(token)
    recreate = decide_prepare(
        PrepareDecisionRequest(_prepare_envelope(state, STACK_B), _decommissioned_row())
    )
    assert cast("PrepareMutationDecision", recreate).mode == "recreate"
    assert cast("PrepareMutationDecision", recreate).source == SourceFacts(
        "DECOMMISSIONED", "DECOMMISSIONED", 1
    )
    dev_row = ActiveRow(state, 4, STACK_A, "a" * 64, INSTANTS[0])
    replay = decide_prepare(PrepareDecisionRequest(_prepare_envelope(state), dev_row))
    assert replay == PrepareReplayDecision(
        data=PrepareData("replay", "a" * 64, "ACTIVE", state, 4),
        physical_resource_id=_prepare_physical("a" * 64),
    )


def test_bootstrap_prepare_writes_prepared_row_with_prepared_at() -> None:
    dynamo = _RecordingDynamo()
    outcome, _ = _drive(dynamo, _event(_prepare_properties()), INSTANTS[0])
    assert outcome == "completed"
    state = _state_item(dynamo)
    assert state is not None
    assert state["disposition"]["S"] == "PREPARED"
    assert state["mode"]["S"] == "bootstrap"
    assert state["expected_revision"]["N"] == "0"
    assert state["prepared_at"]["S"] == INSTANTS[0]
    assert state["prior_snapshot"]["S"] == '"ABSENT"'


def test_commit_conditions_the_byte_exact_prepared_row() -> None:
    dynamo = _RecordingDynamo()
    _, response = _drive(dynamo, _event(_prepare_properties()), INSTANTS[0])
    data = cast("dict[str, Any]", response["Data"])
    state = _state_item(dynamo)
    assert state is not None
    state["expected_revision"] = {"N": "5"}  # tamper: no longer the prepared snapshot
    outcome, _ = _drive(
        dynamo,
        _event(_commit_properties(data), _EventShape(logical="ProjectionHandoffCommit")),
        INSTANTS[1],
    )
    assert outcome == "failed"
    tampered = _state_item(dynamo)
    assert tampered is not None
    assert tampered["disposition"]["S"] == "PREPARED"


def test_commit_writes_active_with_last_transition_at_and_revision() -> None:
    dynamo = _RecordingDynamo()
    data = _bootstrap(dynamo)
    state = _state_item(dynamo)
    assert state is not None
    assert state["disposition"]["S"] == "ACTIVE"
    assert state["state"]["S"] == "INLINE_ACTIVE"
    assert state["revision"]["N"] == "0"
    assert state["last_transition_id"]["S"] == data["transitionToken"]
    assert state["last_transition_at"]["S"] == INSTANTS[1]


def test_transition_commit_increments_revision_and_copies_instant_into_audit() -> None:
    dynamo = _RecordingDynamo()
    data = _bootstrap(dynamo)
    new_data = _transition(dynamo, data)
    token = cast("str", new_data["transitionToken"])
    state = _state_item(dynamo)
    assert state is not None
    assert state["revision"]["N"] == "1"
    assert state["last_transition_at"]["S"] == INSTANTS[3]
    committed = _audit_bytes(dynamo, token, "COMMITTED")
    assert committed is not None
    document = json.loads(committed)
    assert document["transitionInstant"] == INSTANTS[3]
    assert document["toRevision"] == 1


def test_unlisted_edge_fails_without_writes() -> None:
    dynamo = _RecordingDynamo()
    data = _bootstrap(dynamo)
    writes = _write_counts(dynamo)
    shape = _EventShape(
        request_type="Update",
        physical=_prepare_physical(cast("str", data["transitionToken"])),
        old=_prepare_properties(),
    )
    outcome, _ = _invoke(_event(_prepare_properties("QUEUED_ACTIVE"), shape), _runtime(dynamo, []))
    assert outcome == "failed"
    assert _write_counts(dynamo) == writes


def test_replay_requires_the_same_stack_identity() -> None:
    dynamo = _RecordingDynamo()
    data = _bootstrap(dynamo)
    shape = _EventShape(
        request_type="Update",
        stack=STACK_B,
        physical=_prepare_physical(cast("str", data["transitionToken"])),
        old=_prepare_properties(stack=STACK_B),
    )
    outcome, _ = _invoke(_event(_prepare_properties(stack=STACK_B), shape), _runtime(dynamo, []))
    assert outcome == "failed"


def test_recreate_only_from_decommissioned_with_a_different_stack() -> None:
    dynamo = _RecordingDynamo()
    _decommissioned(dynamo)
    same_stack, _ = _drive(dynamo, _event(_prepare_properties()), INSTANTS[7])
    assert same_stack == "failed"
    shape = _EventShape(stack=STACK_B, request_id="request-4")
    outcome, response = _invoke(
        _event(_prepare_properties(stack=STACK_B), shape), _runtime(dynamo, [INSTANTS[7]])
    )
    assert outcome == "completed"
    data = cast("dict[str, Any]", response["Data"])
    assert data["mode"] == "recreate"
    assert data["fromDisposition"] == "DECOMMISSIONED"
    assert data["fromState"] == "DECOMMISSIONED"
    assert data["expectedRevision"] == "1"
    commit_shape = _EventShape(
        stack=STACK_B, request_id="request-5", logical="ProjectionHandoffCommit"
    )
    outcome, commit = _invoke(
        _event(_commit_properties(data, stack=STACK_B), commit_shape),
        _runtime(dynamo, [INSTANTS[8]]),
    )
    assert outcome == "completed"
    assert commit["Data"]["revision"] == 2
    state = _state_item(dynamo)
    assert state is not None
    assert state["stack_identity"]["S"] == STACK_B


# --- Delete paths ---------------------------------------------------------------------------------


def test_commit_delete_owns_rollback_and_prepare_delete_restores_prior_active() -> None:
    dynamo = _RecordingDynamo()
    data = _bootstrap(dynamo)
    new_data = _transition(dynamo, data)
    token = cast("str", new_data["transitionToken"])
    outcome, response = _drive(
        dynamo,
        _event(
            _commit_properties(new_data, "INLINE_QUIESCED"),
            _EventShape(
                request_type="Delete",
                logical="ProjectionHandoffCommit",
                physical=_commit_physical(token),
            ),
        ),
        INSTANTS[4],
    )
    assert outcome == "completed"
    assert response["PhysicalResourceId"] == _commit_physical(token)
    assert "Data" not in response
    state = _state_item(dynamo)
    assert state is not None
    assert state["disposition"]["S"] == "ROLLING_BACK"
    assert state["rollback_owner"]["S"] == "commit"
    assert state["rollback_at"]["S"] == INSTANTS[4]
    rolling = _audit_bytes(dynamo, token, "ROLLING_BACK")
    assert rolling is not None
    assert json.loads(rolling)["transitionInstant"] == INSTANTS[4]
    outcome, _ = _drive(
        dynamo,
        _event(
            _prepare_properties("INLINE_QUIESCED"),
            _EventShape(request_type="Delete", physical=_prepare_physical(token)),
        ),
        INSTANTS[5],
    )
    assert outcome == "completed"
    state = _state_item(dynamo)
    assert state is not None
    assert state["disposition"]["S"] == "ACTIVE"
    assert state["state"]["S"] == "INLINE_ACTIVE"
    assert state["revision"]["N"] == "0"
    assert state["last_transition_id"]["S"] == data["transitionToken"]
    rolled = _audit_bytes(dynamo, token, "ROLLED_BACK")
    assert rolled is not None
    assert json.loads(rolled)["transitionInstant"] == INSTANTS[4]


def test_stale_commit_delete_after_newer_transition_is_a_zero_write_no_op() -> None:
    dynamo = _RecordingDynamo()
    data = _bootstrap(dynamo)
    _transition(dynamo, data)
    writes = _write_counts(dynamo)
    keyspace = dict(dynamo.items)
    outcome, _ = _invoke(
        _event(
            _commit_properties(data),
            _EventShape(
                request_type="Delete",
                logical="ProjectionHandoffCommit",
                physical=_commit_physical(cast("str", data["transitionToken"])),
            ),
        ),
        _runtime(dynamo, []),
    )
    assert outcome == "completed"
    assert _write_counts(dynamo) == writes
    assert dynamo.items == keyspace


def test_commit_delete_during_decommissioning_is_an_exact_no_op() -> None:
    dynamo = _RecordingDynamo()
    data = _bootstrap(dynamo)
    token = cast("str", data["transitionToken"])
    outcome, _ = _drive(
        dynamo,
        _event(
            _guard_properties(),
            _EventShape(
                request_type="Delete",
                logical="ProjectionHandoffDeletionGuard",
                physical=_guard_physical(),
            ),
        ),
        INSTANTS[4],
    )
    assert outcome == "completed"
    writes = _write_counts(dynamo)
    outcome, _ = _invoke(
        _event(
            _commit_properties(data),
            _EventShape(
                request_type="Delete",
                logical="ProjectionHandoffCommit",
                physical=_commit_physical(token),
            ),
        ),
        _runtime(dynamo, []),
    )
    assert outcome == "completed"
    assert _write_counts(dynamo) == writes


def test_first_bootstrap_prepare_delete_restores_absent_via_conditional_delete() -> None:
    dynamo = _RecordingDynamo()
    _, response = _drive(dynamo, _event(_prepare_properties()), INSTANTS[0])
    token = cast("str", cast("dict[str, Any]", response["Data"])["transitionToken"])
    outcome, _ = _drive(
        dynamo,
        _event(
            _prepare_properties(),
            _EventShape(request_type="Delete", physical=_prepare_physical(token)),
        ),
        INSTANTS[1],
    )
    assert outcome == "completed"
    assert _state_item(dynamo) is None
    assert len(dynamo.deletes) == 1
    assert _audit_bytes(dynamo, token, "ROLLING_BACK") is not None
    rolled = _audit_bytes(dynamo, token, "ROLLED_BACK")
    assert rolled is not None
    document = json.loads(rolled)
    assert document["toDisposition"] == "ABSENT"
    assert document["transitionInstant"] == INSTANTS[1]


def test_failed_recreation_prepare_delete_restores_the_decommissioned_row() -> None:
    dynamo = _RecordingDynamo()
    _decommissioned(dynamo)
    terminal_before = json.dumps(_state_item(dynamo), sort_keys=True)
    shape = _EventShape(stack=STACK_B, request_id="request-4")
    _, response = _invoke(
        _event(_prepare_properties(stack=STACK_B), shape), _runtime(dynamo, [INSTANTS[7]])
    )
    token = cast("str", cast("dict[str, Any]", response["Data"])["transitionToken"])
    outcome, _ = _invoke(
        _event(
            _prepare_properties(stack=STACK_B),
            _EventShape(request_type="Delete", stack=STACK_B, physical=_prepare_physical(token)),
        ),
        _runtime(dynamo, [INSTANTS[8]]),
    )
    assert outcome == "completed"
    assert json.dumps(_state_item(dynamo), sort_keys=True) == terminal_before
    rolled = _audit_bytes(dynamo, token, "ROLLED_BACK")
    assert rolled is not None
    assert json.loads(rolled)["toDisposition"] == "DECOMMISSIONED"


def test_rolled_back_audit_retry_reads_the_rolling_back_sibling_instant() -> None:
    dynamo = _RecordingDynamo()
    data = _bootstrap(dynamo)
    new_data = _transition(dynamo, data)
    token = cast("str", new_data["transitionToken"])
    delete_event = _event(
        _commit_properties(new_data, "INLINE_QUIESCED"),
        _EventShape(
            request_type="Delete",
            logical="ProjectionHandoffCommit",
            physical=_commit_physical(token),
        ),
    )
    _drive(dynamo, delete_event, INSTANTS[4])
    prepare_delete = _event(
        _prepare_properties("INLINE_QUIESCED"),
        _EventShape(request_type="Delete", physical=_prepare_physical(token)),
    )
    _drive(dynamo, prepare_delete, INSTANTS[5])
    original = _audit_bytes(dynamo, token, "ROLLED_BACK")
    assert original is not None
    dynamo.items.pop((PARTITION, f"AUDIT#{token}#ROLLED_BACK"))
    outcome, _ = _invoke(prepare_delete, _runtime(dynamo, []))
    assert outcome == "completed"
    assert _audit_bytes(dynamo, token, "ROLLED_BACK") == original


def test_stale_prepare_delete_with_unknown_token_writes_nothing() -> None:
    dynamo = _RecordingDynamo()
    data = _bootstrap(dynamo)
    _transition(dynamo, data)
    writes = _write_counts(dynamo)
    keyspace = dict(dynamo.items)
    outcome, _ = _invoke(
        _event(
            _prepare_properties(),
            _EventShape(request_type="Delete", physical=_prepare_physical("d" * 64)),
        ),
        _runtime(dynamo, []),
    )
    assert outcome == "completed"
    assert _write_counts(dynamo) == writes
    assert dynamo.items == keyspace


FAILED_CREATE_PHYSICAL = "projection-handoff:v1-failed:request-1"


@guard_reserved_words
@dataclass
class _FaultyStateWrite(_RecordingDynamo):
    """Fails the next ``STATE`` write with a non-conditional error, after applying it if landed."""

    armed: bool = False
    landed: bool = False

    def update_item(self, **request: Any) -> dict[str, object]:
        if self.armed and self._key(request)[1] == "STATE":
            self.armed = False
            if self.landed:
                super().update_item(**request)
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "fixture"}}, "UpdateItem"
            )
        return super().update_item(**request)


def _queued_bootstrap_data() -> dict[str, Any]:
    return {
        "mode": "bootstrap",
        "transitionToken": _expected_bootstrap_token("QUEUED_ACTIVE"),
        "fromDisposition": "ABSENT",
        "fromState": "ABSENT",
        "expectedRevision": "0",
    }


def _failed_create_delete(
    logical: str, properties: dict[str, Any], stack: str = STACK_A
) -> dict[str, Any]:
    return _event(
        properties,
        _EventShape(
            request_type="Delete",
            stack=stack,
            request_id="request-9",
            logical=logical,
            physical=FAILED_CREATE_PHYSICAL,
        ),
    )


def _occupy_audit(dynamo: _RecordingDynamo, token: str, phase: str) -> None:
    record_key = f"AUDIT#{token}#{phase}"
    dynamo.items[(PARTITION, record_key)] = {
        "partition_key": {"S": PARTITION},
        "record_key": {"S": record_key},
        "payload": {"S": '{"occupied":true}'},
    }


@pytest.mark.parametrize(
    ("logical", "properties"),
    [
        ("ProjectionHandoffPrepare", _prepare_properties("QUEUED_ACTIVE")),
        ("ProjectionHandoffCommit", _commit_properties(_queued_bootstrap_data(), "QUEUED_ACTIVE")),
        ("ProjectionHandoffDeletionGuard", _guard_properties()),
    ],
)
def test_delete_of_a_failed_create_marker_is_a_zero_write_success(
    logical: str, properties: dict[str, Any]
) -> None:
    """Fresh-env run 1 (2026-10-19): a refused Create must roll back, not ROLLBACK_FAILED."""
    dynamo = _RecordingDynamo()
    outcome, response = _invoke(_failed_create_delete(logical, properties), _runtime(dynamo, []))
    assert outcome == "completed"
    assert response == {
        "Status": "SUCCESS",
        "Reason": "",
        "PhysicalResourceId": FAILED_CREATE_PHYSICAL,
        "StackId": STACK_A,
        "RequestId": "request-9",
        "LogicalResourceId": logical,
    }
    assert dynamo.updates == []
    assert dynamo.deletes == []
    assert dynamo.items == {}


def test_the_refused_first_create_and_its_rollback_delete_leave_the_table_untouched() -> None:
    """The exact run-1 sequence: refused prepare Create, then the rollback Delete of its ID."""
    dynamo = _RecordingDynamo()
    outcome, created = _drive(dynamo, _event(_prepare_properties("INLINE_QUIESCED")))
    assert outcome == "failed"
    assert created["PhysicalResourceId"] == FAILED_CREATE_PHYSICAL
    outcome, deleted = _invoke(
        _failed_create_delete("ProjectionHandoffPrepare", _prepare_properties("INLINE_QUIESCED")),
        _runtime(dynamo, []),
    )
    assert outcome == "completed"
    assert deleted["Status"] == "SUCCESS"
    assert _write_counts(dynamo) == (0, 0)
    assert dynamo.items == {}


def test_failed_create_marker_delete_restores_an_orphan_same_stack_prepared_row() -> None:
    """An earlier provider named no token for a PREPARED row it wrote; the marker restores it."""
    dynamo = _RecordingDynamo()
    _drive(dynamo, _event(_prepare_properties("QUEUED_ACTIVE")), INSTANTS[0])
    token = _expected_bootstrap_token("QUEUED_ACTIVE")
    outcome, _ = _invoke(
        _failed_create_delete("ProjectionHandoffPrepare", _prepare_properties("QUEUED_ACTIVE")),
        _runtime(dynamo, [INSTANTS[1]]),
    )
    assert outcome == "completed"
    assert _state_item(dynamo) is None
    assert len(dynamo.deletes) == 1
    rolled = _audit_bytes(dynamo, token, "ROLLED_BACK")
    assert rolled is not None
    assert json.loads(rolled)["toDisposition"] == "ABSENT"


def test_failed_create_marker_delete_leaves_another_stacks_prepared_row_alone() -> None:
    dynamo = _RecordingDynamo()
    _drive(dynamo, _event(_prepare_properties("QUEUED_ACTIVE")), INSTANTS[0])
    writes = _write_counts(dynamo)
    keyspace = dict(dynamo.items)
    outcome, _ = _invoke(
        _failed_create_delete(
            "ProjectionHandoffPrepare", _prepare_properties("QUEUED_ACTIVE", STACK_B), STACK_B
        ),
        _runtime(dynamo, []),
    )
    assert outcome == "completed"
    assert _write_counts(dynamo) == writes
    assert dynamo.items == keyspace


def test_a_prepare_that_fails_after_its_write_names_its_token_and_its_delete_restores() -> None:
    """Harness pattern: once the token is decided, a failed Create names the closed-form ID."""
    dynamo = _RecordingDynamo()
    token = _expected_bootstrap_token("QUEUED_ACTIVE")
    _occupy_audit(dynamo, token, "PREPARED")
    outcome, created = _drive(dynamo, _event(_prepare_properties("QUEUED_ACTIVE")), INSTANTS[0])
    assert outcome == "failed"
    assert created["PhysicalResourceId"] == _prepare_physical(token)
    assert created["Reason"] == (
        "Projection handoff state authority conflict; see provider logs for RequestId request-1."
    )
    state = _state_item(dynamo)
    assert state is not None and state["disposition"]["S"] == "PREPARED"
    outcome, _ = _drive(
        dynamo,
        _event(
            _prepare_properties("QUEUED_ACTIVE"),
            _EventShape(request_type="Delete", physical=_prepare_physical(token)),
        ),
        INSTANTS[1],
    )
    assert outcome == "completed"
    assert _state_item(dynamo) is None
    rolled = _audit_bytes(dynamo, token, "ROLLED_BACK")
    assert rolled is not None
    assert json.loads(rolled)["toDisposition"] == "ABSENT"


def test_a_commit_that_fails_after_its_write_names_its_token_and_rollback_restores() -> None:
    dynamo = _RecordingDynamo()
    _, prepared = _drive(dynamo, _event(_prepare_properties("QUEUED_ACTIVE")), INSTANTS[0])
    data = cast("dict[str, Any]", prepared["Data"])
    token = cast("str", data["transitionToken"])
    _occupy_audit(dynamo, token, "COMMITTED")
    commit_properties = _commit_properties(data, "QUEUED_ACTIVE")
    outcome, created = _drive(
        dynamo,
        _event(commit_properties, _EventShape(logical="ProjectionHandoffCommit")),
        INSTANTS[1],
    )
    assert outcome == "failed"
    assert created["PhysicalResourceId"] == _commit_physical(token)
    outcome, _ = _drive(
        dynamo,
        _event(
            commit_properties,
            _EventShape(
                request_type="Delete",
                logical="ProjectionHandoffCommit",
                physical=_commit_physical(token),
            ),
        ),
        INSTANTS[2],
    )
    assert outcome == "completed"
    state = _state_item(dynamo)
    assert state is not None
    assert state["disposition"]["S"] == "ROLLING_BACK"
    assert state["rollback_owner"]["S"] == "commit"
    outcome, _ = _drive(
        dynamo,
        _event(
            _prepare_properties("QUEUED_ACTIVE"),
            _EventShape(request_type="Delete", physical=_prepare_physical(token)),
        ),
        INSTANTS[3],
    )
    assert outcome == "completed"
    assert _state_item(dynamo) is None


@pytest.mark.parametrize("landed", [False, True])
def test_a_failed_commit_write_rolls_back_through_its_marker_then_prepare(landed: bool) -> None:
    """A commit write error names the marker; landed or not, the delete order restores ABSENT."""
    dynamo = _FaultyStateWrite()
    _, prepared = _drive(dynamo, _event(_prepare_properties("QUEUED_ACTIVE")), INSTANTS[0])
    data = cast("dict[str, Any]", prepared["Data"])
    token = cast("str", data["transitionToken"])
    commit_properties = _commit_properties(data, "QUEUED_ACTIVE")
    dynamo.armed, dynamo.landed = True, landed
    outcome, created = _drive(
        dynamo,
        _event(commit_properties, _EventShape(logical="ProjectionHandoffCommit")),
        INSTANTS[1],
    )
    assert outcome == "failed"
    assert created["PhysicalResourceId"] == FAILED_CREATE_PHYSICAL
    state = _state_item(dynamo)
    assert state is not None
    assert state["disposition"]["S"] == ("ACTIVE" if landed else "PREPARED")
    writes = _write_counts(dynamo)
    outcome, _ = _invoke(
        _failed_create_delete("ProjectionHandoffCommit", commit_properties),
        _runtime(dynamo, [INSTANTS[2]]),
    )
    assert outcome == "completed"
    state = _state_item(dynamo)
    assert state is not None
    if landed:
        assert state["disposition"]["S"] == "ROLLING_BACK"
        assert state["rollback_owner"]["S"] == "commit"
    else:
        assert state["disposition"]["S"] == "PREPARED"
        assert _write_counts(dynamo) == writes
    outcome, _ = _drive(
        dynamo,
        _event(
            _prepare_properties("QUEUED_ACTIVE"),
            _EventShape(request_type="Delete", physical=_prepare_physical(token)),
        ),
        INSTANTS[3],
    )
    assert outcome == "completed"
    assert _state_item(dynamo) is None


def test_a_guard_create_failure_names_the_marker_and_its_delete_writes_nothing() -> None:
    dynamo = _RecordingDynamo()
    _bootstrap(dynamo)
    keyspace = dict(dynamo.items)
    writes = _write_counts(dynamo)
    properties = _guard_properties()
    properties["protocolVersion"] = "2"
    outcome, created = _drive(
        dynamo, _event(properties, _EventShape(logical="ProjectionHandoffDeletionGuard"))
    )
    assert outcome == "failed"
    assert created["PhysicalResourceId"] == FAILED_CREATE_PHYSICAL
    outcome, _ = _invoke(
        _failed_create_delete("ProjectionHandoffDeletionGuard", _guard_properties()),
        _runtime(dynamo, []),
    )
    assert outcome == "completed"
    assert _write_counts(dynamo) == writes
    assert dynamo.items == keyspace


@pytest.mark.parametrize(
    "physical",
    [
        "projection-handoff:v1:failed:request-1",
        "projection-handoff:v1-failed:",
        "projection-handoff:v1-failed:request 1",
        "projection-handoff:v1-failed:request-1:extra",
        "projection-handoff:v1-failed:request-1\n",
        "projection-handoff:v2-failed:request-1",
        "projection-handoff:v1-FAILED:request-1",
        f"projection-handoff:v1:{ENV}:failed:request-1",
    ],
)
def test_every_other_malformed_physical_id_is_still_refused(physical: str) -> None:
    dynamo = _RecordingDynamo()
    event = _event(
        _prepare_properties("QUEUED_ACTIVE"),
        _EventShape(request_type="Delete", request_id="request-9", physical=physical),
    )
    with pytest.raises(ValidationError) as refused:
        process(event, _runtime(dynamo, []))
    assert refused.value.description == "physical resource ID is outside the closed form"
    assert _write_counts(dynamo) == (0, 0)


def test_a_create_request_id_outside_the_closed_form_fails_with_the_unknown_marker() -> None:
    dynamo = _RecordingDynamo()
    event = _event(_prepare_properties("INLINE_QUIESCED"), _EventShape(request_id="request 1/odd"))
    outcome, response = _drive(dynamo, event)
    assert outcome == "failed"
    assert response["PhysicalResourceId"] == "projection-handoff:v1-failed:unknown"


def test_guard_update_requires_byte_identical_properties() -> None:
    dynamo = _RecordingDynamo()
    _bootstrap(dynamo)
    identical = _EventShape(
        request_type="Update",
        logical="ProjectionHandoffDeletionGuard",
        physical=_guard_physical(),
        old=_guard_properties(),
    )
    outcome, response = _invoke(_event(_guard_properties(), identical), _runtime(dynamo, []))
    assert outcome == "completed"
    assert response["PhysicalResourceId"] == _guard_physical()
    differing = dict(_guard_properties())
    differing["ServiceToken"] = SERVICE_TOKEN + "-next"
    shape = _EventShape(
        request_type="Update",
        logical="ProjectionHandoffDeletionGuard",
        physical=_guard_physical(),
        old=differing,
    )
    outcome, _ = _invoke(_event(_guard_properties(), shape), _runtime(dynamo, []))
    assert outcome == "failed"


def test_guard_delete_derives_the_decommission_token_and_cases_active() -> None:
    dynamo = _RecordingDynamo()
    data = _bootstrap(dynamo)
    token = cast("str", data["transitionToken"])
    outcome, _ = _drive(
        dynamo,
        _event(
            _guard_properties(),
            _EventShape(
                request_type="Delete",
                logical="ProjectionHandoffDeletionGuard",
                physical=_guard_physical(),
            ),
        ),
        INSTANTS[4],
    )
    assert outcome == "completed"
    state = _state_item(dynamo)
    assert state is not None
    assert state["disposition"]["S"] == "DECOMMISSIONING"
    assert state["decommissioning_at"]["S"] == INSTANTS[4]
    pinned = (
        f'["projection-handoff-decommission/v1","{ENV}","{STACK_A}","INLINE_ACTIVE",0,"{token}"]'
    ).encode()
    expected_token = hashlib.sha256(pinned).hexdigest()
    assert state["decommission_token"]["S"] == expected_token
    assert _audit_bytes(dynamo, expected_token, "DECOMMISSIONING") is not None


def test_prepare_delete_completes_decommission_at_prior_revision_plus_one() -> None:
    dynamo = _RecordingDynamo()
    _, decommission = _decommissioned(dynamo)
    state = _state_item(dynamo)
    assert state is not None
    assert state["disposition"]["S"] == "DECOMMISSIONED"
    assert state["revision"]["N"] == "1"
    assert state["last_transition_id"]["S"] == decommission
    assert "state" not in state
    assert state["decommissioned_at"]["S"] == INSTANTS[6]
    assert _audit_bytes(dynamo, decommission, "DECOMMISSIONED") is not None


def test_guard_delete_from_a_deleted_stack_after_recreation_is_a_no_op() -> None:
    dynamo = _RecordingDynamo()
    state_map = {
        "partition_key": {"S": PARTITION},
        "record_key": {"S": "STATE"},
        "protocol_version": {"N": "1"},
        "disposition": {"S": "ACTIVE"},
        "state": {"S": "INLINE_ACTIVE"},
        "revision": {"N": "2"},
        "stack_identity": {"S": STACK_B},
        "last_transition_id": {"S": "e" * 64},
        "last_transition_at": {"S": INSTANTS[0]},
    }
    dynamo.items[(PARTITION, "STATE")] = state_map
    outcome, _ = _invoke(
        _event(
            _guard_properties(),
            _EventShape(
                request_type="Delete",
                logical="ProjectionHandoffDeletionGuard",
                physical=_guard_physical(),
            ),
        ),
        _runtime(dynamo, []),
    )
    assert outcome == "completed"
    assert _write_counts(dynamo) == (0, 0)
    assert dynamo.items[(PARTITION, "STATE")] == state_map


def test_guard_delete_fails_on_same_stack_prepared_row() -> None:
    dynamo = _RecordingDynamo()
    _drive(dynamo, _event(_prepare_properties()), INSTANTS[0])
    writes = _write_counts(dynamo)
    outcome, _ = _invoke(
        _event(
            _guard_properties(),
            _EventShape(
                request_type="Delete",
                logical="ProjectionHandoffDeletionGuard",
                physical=_guard_physical(),
            ),
        ),
        _runtime(dynamo, []),
    )
    assert outcome == "failed"
    assert _write_counts(dynamo) == writes


# --- Retry semantics ------------------------------------------------------------------------------


def test_prepare_retry_recreates_the_missing_byte_identical_audit() -> None:
    dynamo = _RecordingDynamo()
    _drive(dynamo, _event(_prepare_properties()), INSTANTS[0])
    token = _expected_bootstrap_token()
    original = _audit_bytes(dynamo, token, "PREPARED")
    assert original is not None
    dynamo.items.pop((PARTITION, f"AUDIT#{token}#PREPARED"))
    state_before = json.dumps(_state_item(dynamo), sort_keys=True)
    outcome, response = _invoke(_event(_prepare_properties()), _runtime(dynamo, []))
    assert outcome == "completed"
    assert _audit_bytes(dynamo, token, "PREPARED") == original
    assert json.dumps(_state_item(dynamo), sort_keys=True) == state_before
    assert response["Data"]["transitionToken"] == token


def test_commit_retry_reads_the_instant_back_from_the_completed_row() -> None:
    dynamo = _RecordingDynamo()
    data = _bootstrap(dynamo)
    token = cast("str", data["transitionToken"])
    original = _audit_bytes(dynamo, token, "COMMITTED")
    assert original is not None
    dynamo.items.pop((PARTITION, f"AUDIT#{token}#COMMITTED"))
    outcome, response = _invoke(
        _event(_commit_properties(data), _EventShape(logical="ProjectionHandoffCommit")),
        _runtime(dynamo, []),
    )
    assert outcome == "completed"
    assert response["Data"]["outcome"] == "committed"
    assert _audit_bytes(dynamo, token, "COMMITTED") == original


def test_occupied_non_identical_audit_fails_the_callback() -> None:
    dynamo = _RecordingDynamo()
    _drive(dynamo, _event(_prepare_properties()), INSTANTS[0])
    token = _expected_bootstrap_token()
    record = dynamo.items[(PARTITION, f"AUDIT#{token}#PREPARED")]
    record["payload"] = {"S": '{"tampered":true}'}
    outcome, response = _invoke(_event(_prepare_properties()), _runtime(dynamo, []))
    assert outcome == "failed"
    assert response["Status"] == "FAILED"
    assert dynamo.items[(PARTITION, f"AUDIT#{token}#PREPARED")]["payload"]["S"] == (
        '{"tampered":true}'
    )


def test_failure_reason_is_sanitized_and_names_only_the_request_id() -> None:
    dynamo = _RecordingDynamo()
    outcome, response = _drive(dynamo, _event(_prepare_properties("INLINE_QUIESCED")))
    assert outcome == "failed"
    assert response["Reason"] == (
        "Projection handoff callback validation failed; see provider logs for RequestId request-1."
    )
    assert STACK_A not in response["Reason"]


def test_successful_callback_emits_no_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported: list[tuple[BaseException, Report]] = []

    def capture_error(error: BaseException, report: Report) -> None:
        reported.append((error, report))

    monkeypatch.setattr(
        "adapters.jobs.projection_handoff.report_error",
        capture_error,
    )

    outcome, _response = _drive(_RecordingDynamo(), _event(_prepare_properties()))

    assert outcome == "completed"
    assert reported == []


def test_non_https_response_url_reports_one_invariant_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(_prepare_properties())
    event["ResponseURL"] = "http://example.test/response"
    reported: list[tuple[BaseException, Report]] = []

    def capture_error(error: BaseException, report: Report) -> None:
        reported.append((error, report))

    monkeypatch.setattr(
        "adapters.jobs.projection_handoff.report_error",
        capture_error,
    )

    with pytest.raises(InvariantError) as raised:
        run(
            RunRequest(
                event=event,
                runtime=_runtime(_RecordingDynamo(), [INSTANTS[0]]),
                deliver=_RecordingResponder(),
            )
        )

    assert len(reported) == 1
    error, report = reported[0]
    assert error is raised.value
    assert raised.value.error_class is ErrorClass.INVARIANT
    assert report.operation == OPERATION


def test_response_delivery_failure_reports_one_dependency_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = DependencyError(
        HANDOFF_DEPENDENCY,
        "response delivery failed",
        context={"cause": "fixture"},
    )
    reported: list[tuple[BaseException, Report]] = []

    def capture_error(error: BaseException, report: Report) -> None:
        reported.append((error, report))

    monkeypatch.setattr(
        "adapters.jobs.projection_handoff.report_error",
        capture_error,
    )

    def fail_delivery(_url: str, _body: bytes) -> None:
        raise failure

    with pytest.raises(DependencyError) as raised:
        run(
            RunRequest(
                event=_event(_prepare_properties()),
                runtime=_runtime(_RecordingDynamo(), [INSTANTS[0]]),
                deliver=fail_delivery,
            )
        )

    assert raised.value is failure
    assert failure.error_class is ErrorClass.DEPENDENCY
    assert failure.context == {"cause": "fixture"}
    assert len(reported) == 1
    assert reported[0][0] is failure
    assert reported[0][1].operation == OPERATION


def test_projection_handoff_error_operation_is_in_the_ingestion_alarm_roster() -> None:
    alarm_source = (ROOT / "infra/lib/stacks/ingestion-stage1.ts").read_text()
    assert f"  '{OPERATION}'," in alarm_source


# --- Response delivery through the shared HTTPS responder -----------------------------------------


class _ScriptedAdapter:
    """Answer every request with one scripted status, recording what was sent."""

    def __init__(self, status: int, location: str | None = None) -> None:
        self.status = status
        self.location = location
        self.sent: list[tuple[str, str, dict[str, str | bytes]]] = []

    def send(self, request: PreparedRequest, **kwargs: Any) -> Response:
        del kwargs
        self.sent.append((request.method or "", request.url or "", dict(request.headers)))
        response = Response()
        response.status_code = self.status
        response.url = request.url or ""
        response.request = request
        if self.location is not None:
            response.headers["Location"] = self.location
        return response

    def close(self) -> None:
        """No resources are opened by this adapter."""


def _scripted_delivery(adapter: _ScriptedAdapter) -> Any:
    session = HttpsSession()
    session.mount("https://", cast("Any", adapter))
    session.mount("http://", cast("Any", adapter))
    return _deliverer(HttpsCustomResourceResponder(session))


def test_a_redirect_to_plaintext_is_refused_with_the_handoff_code() -> None:
    adapter = _ScriptedAdapter(302, location="http://example.test/captured")

    with pytest.raises(DependencyError) as raised:
        _scripted_delivery(adapter)(RESPONSE_URL, b"{}")

    assert type(raised.value) is DependencyError
    assert raised.value.code == HANDOFF_DEPENDENCY
    assert raised.value.description == "response delivery failed"
    assert raised.value.context == {"cause": "InsecureUrlError"}
    assert [url for _method, url, _headers in adapter.sent] == [RESPONSE_URL]


def test_a_refused_delivery_names_only_the_status() -> None:
    adapter = _ScriptedAdapter(403)

    with pytest.raises(DependencyError) as raised:
        _scripted_delivery(adapter)(RESPONSE_URL, b"{}")

    assert type(raised.value) is DependencyError
    assert raised.value.code == HANDOFF_DEPENDENCY
    assert raised.value.description == "response delivery was refused"
    assert raised.value.context == {"status": 403}


def test_an_accepted_delivery_is_one_put_with_an_empty_content_type() -> None:
    adapter = _ScriptedAdapter(200)

    _scripted_delivery(adapter)(RESPONSE_URL, b'{"Status":"SUCCESS"}')

    assert len(adapter.sent) == 1
    method, url, headers = adapter.sent[0]
    assert (method, url) == ("PUT", RESPONSE_URL)
    assert headers["Content-Type"] == ""
