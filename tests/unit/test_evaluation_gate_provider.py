# SPDX-License-Identifier: MIT-0
"""The evaluation enablement gate's custom-resource lifecycle (ADR-079).

Driven through `provider.run` against in-memory fakes of the gate's three ports, a recording
responder, and a fake clock, so every branch runs with no AWS and no SDK stand-in. Each test
asserts the one response CloudFormation receives and the ordered writes, because the order is
the safety property: the release record says not enabled before anything is disabled or
changed, and says enabled only after `ENABLED` read back.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from evaluation_gate_support import (
    CONFIG_ID,
    DECIDED_AT,
    GROUNDING,
    GROUNDING_ID,
    HELPFULNESS,
    HELPFULNESS_ID,
    RELEASE_PARAMETER,
    SUFFIX,
    FakeClock,
    World,
    evaluator_document,
    event,
    properties,
    spec_document,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from adapters.jobs.evaluation_gate import handler as handler_module
from adapters.jobs.evaluation_gate.provider import (
    EVALUATION_GATE_REFUSED,
    MAX_REASON_LENGTH,
    GateRefusedError,
    RunRequest,
    run,
)
from adapters.outbound.agentcore.online_evaluation_control import EVALUATION_GATE_CONTROL_FAILED
from adapters.outbound.telemetry import tracing
from adapters.outbound.telemetry.tracing import EVALUATION_GATE
from domain.evaluation.gate import (
    ExecutionStatus,
    GateAssertion,
    GateFinding,
    OnlineEvaluationStatus,
    ResultGroupCustodyV1,
    client_token,
    parse_gate_event,
    parse_gate_spec,
    release_enabled,
    release_not_enabled,
)
from domain.evaluation.release import EvaluationFamilyV1
from domain.shared_kernel.errors import DependencyError, InvariantError
from ports.outbound.evaluation_gate import (
    CreateOnlineEvaluationRequest,
    UpdateOnlineEvaluationRequest,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-079")]

EVALUATIONS = ["control.evaluate"] * 6
"""Three fictional cases, each judged by both evaluators."""
FULL_ENABLE = [
    "release.write:not_enabled",
    "control.update:DISABLED",
    *EVALUATIONS,
    "control.update:ENABLED",
    "release.write:enabled",
]
"""The evaluator-set change order of the LLD."""
PLACEHOLDER = "evaluation-gate:failed:request-1"


def _spec(**overrides: object) -> Any:
    return parse_gate_spec(json.dumps(spec_document(**overrides)))


def _requested(requested: bool) -> dict[str, str]:
    return properties(spec_document(execution_requested=requested))


def _record(world: World) -> Any:
    return world.releases.records.get(RELEASE_PARAMETER)


def _slow_readback(world: World, until: float) -> None:
    """Make the first custody readback take until the given clock instant."""
    findings = world.readback.findings

    def slow(custody: ResultGroupCustodyV1) -> tuple[GateFinding, ...]:
        world.clock.now = max(world.clock.now, until)
        return findings(custody)

    world.readback.findings = slow  # type: ignore[method-assign]


class TestCreate:
    def test_requested_calibrates_enables_reads_back_then_records_enabled(self) -> None:
        world = World()
        body = world.run(event("Create"))

        assert body == {
            "Status": "SUCCESS",
            "Reason": "",
            "PhysicalResourceId": CONFIG_ID,
            "StackId": body["StackId"],
            "RequestId": "request-1",
            "LogicalResourceId": body["LogicalResourceId"],
            "Data": {"ConfigId": CONFIG_ID, "ExecutionStatus": "ENABLED"},
        }
        assert world.mutations() == [
            "release.write:not_enabled",
            "control.create",
            *EVALUATIONS,
            "control.update:ENABLED",
            "release.write:enabled",
        ]
        assert _record(world) == release_enabled(_spec(), CONFIG_ID, DECIDED_AT)
        create = cast("CreateOnlineEvaluationRequest", world.control.requests[0])
        assert create.spec == _spec()

    def test_not_requested_stops_disabled_with_the_record_not_enabled(self) -> None:
        world = World()
        body = world.run(event("Create", ResourceProperties=_requested(False)))

        assert body["Status"] == "SUCCESS"
        assert body["Data"] == {"ConfigId": CONFIG_ID, "ExecutionStatus": "DISABLED"}
        assert world.mutations() == ["release.write:not_enabled", "control.create"]
        assert _record(world) == release_not_enabled(EvaluationFamilyV1.V4, DECIDED_AT)

    def test_every_evaluate_uses_the_input_service_and_one_case_evaluator_pair(self) -> None:
        world = World()
        world.run(event("Create"))

        pairs = [
            (request.calibration.case_name, request.calibration.evaluator_id)
            for request in world.control.evaluations
        ]
        assert pairs == [
            ("known_correct", HELPFULNESS_ID),
            ("known_correct", GROUNDING_ID),
            ("contradiction", HELPFULNESS_ID),
            ("contradiction", GROUNDING_ID),
            ("fluent_but_unsupported", HELPFULNESS_ID),
            ("fluent_but_unsupported", GROUNDING_ID),
        ]
        assert {request.service_name for request in world.control.evaluations} == {
            "adlytics-test-released-response"
        }

    def test_a_failed_assertion_fails_disabled_and_names_the_config_for_rollback(self) -> None:
        world = World()
        world.readback.results = [
            (
                GateFinding(GateAssertion.SUBSCRIPTION_FILTERS),
                GateFinding(GateAssertion.GROUP_RETENTION),
            )
        ]
        body = world.run(event("Create"))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == CONFIG_ID
        assert body["Reason"] == (
            "Evaluation gate v4 refused: failed assertions subscription_filters, group_retention."
        )
        assert world.mutations() == ["release.write:not_enabled", "control.create"]
        assert not _record(world).enabled
        assert world.last is not None
        assert world.last.failed_assertion_count == 2
        assert world.last.execution_status == "DISABLED"

    def test_a_calibration_failure_never_enables_and_names_only_case_and_evaluator(
        self,
    ) -> None:
        world = World()
        world.control.labels["contradiction", GROUNDING] = ("Grounded",)
        world.control.errors["known_correct", HELPFULNESS] = "ModelTimeout"
        body = world.run(event("Create"))

        assert body["Status"] == "FAILED"
        assert body["Reason"] == (
            "Evaluation gate v4 refused: calibration failed for "
            f"known_correct/{HELPFULNESS}:error, contradiction/{GROUNDING}:unexpected_label."
        )
        assert "widgets" not in json.dumps(body)
        assert "control.update:ENABLED" not in world.mutations()
        assert not _record(world).enabled
        assert world.last is not None
        assert world.last.error is not None
        assert isinstance(world.last.error, GateRefusedError)
        assert world.last.error.code == EVALUATION_GATE_REFUSED
        assert (world.last.calibration_case_count, world.last.calibration_failure_count) == (3, 2)

    def test_a_readback_failure_after_enabling_fails_closed(self) -> None:
        world = World()
        world.readback.results = [(), (GateFinding(GateAssertion.RESOURCE_POLICY_WRITE),)]
        body = world.run(event("Create"))

        assert body["Status"] == "FAILED"
        assert body["Reason"] == (
            "Evaluation gate v4 refused: failed assertions resource_policy_write."
        )
        assert world.mutations() == [
            "release.write:not_enabled",
            "control.create",
            *EVALUATIONS,
            "control.update:ENABLED",
            "release.write:not_enabled",
            "control.update:DISABLED",
        ]
        assert not _record(world).enabled

    def test_gives_up_before_the_deadline_naming_the_last_status(self) -> None:
        world = World(deadline=25.0)
        world.control.after_create = [OnlineEvaluationStatus.CREATING]
        body = world.run(event("Create"))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == CONFIG_ID
        assert body["Reason"] == (
            f"Create: gave up waiting for online evaluation configuration {CONFIG_ID} before the "
            "provider timeout; last status CREATING/DISABLED."
        )
        assert world.clock.sleeps == [10.0, 10.0, 5.0]

    def test_calibration_gives_up_at_the_deadline_without_enabling(self) -> None:
        world = World(deadline=10.0)
        world.control.after_create = [OnlineEvaluationStatus.ACTIVE]
        _slow_readback(world, until=10.0)
        body = world.run(event("Create"))

        assert body["Reason"] == (
            "Calibration of evaluation family v4 gave up before the provider timeout after 0 of "
            "6 evaluations."
        )
        assert "control.evaluate" not in world.mutations()
        assert not _record(world).enabled

    def test_a_failed_status_names_the_service_reason(self) -> None:
        world = World()
        world.control.after_create = [OnlineEvaluationStatus.CREATE_FAILED]
        world.control.create_failure_reason = "Role cannot be assumed"
        body = world.run(event("Create"))

        assert body["Reason"] == (
            f"Create: online evaluation configuration {CONFIG_ID} reached CREATE_FAILED: "
            "Role cannot be assumed"
        )
        assert body["PhysicalResourceId"] == CONFIG_ID
        assert "control.evaluate" not in world.mutations()

    def test_a_refused_create_answers_with_a_placeholder_and_the_provider_code(self) -> None:
        world = World()
        world.control.create_error = DependencyError(
            EVALUATION_GATE_CONTROL_FAILED,
            "CreateOnlineEvaluationConfig failed.",
            context={"service_code": "ConflictException", "provider_message": "name exists"},
        )
        body = world.run(event("Create"))

        assert body["PhysicalResourceId"] == PLACEHOLDER
        assert body["Reason"] == (
            "CreateOnlineEvaluationConfig failed. ConflictException: name exists"
        )
        assert world.mutations() == ["release.write:not_enabled", "control.create"]

    def test_the_client_token_is_stable_for_a_retried_request_and_distinct_otherwise(self) -> None:
        tokens: list[str] = []
        for request_id in ("request-1", "request-1", "request-2"):
            world = World()
            world.run(event("Create", RequestId=request_id))
            request = cast("CreateOnlineEvaluationRequest", world.control.requests[0])
            tokens.append(request.client_token)
        assert tokens[0] == tokens[1] != tokens[2]

    def test_a_record_that_does_not_read_back_as_written_fails_closed(self) -> None:
        world = World()
        world.releases.corrupt_reads = True
        body = world.run(event("Create"))

        assert body["Status"] == "FAILED"
        assert body["Reason"] == "The evaluation release record did not read back as written."
        assert world.mutations() == ["release.write:not_enabled"]


def _seed(world: World, execution: ExecutionStatus, **drift: object) -> None:
    world.control.seed(_spec(), execution, **drift)


class TestUpdate:
    def test_not_requested_records_not_enabled_before_disabling_and_rebinding(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED, output_log_group_name="/drifted")
        world.releases.records[RELEASE_PARAMETER] = release_enabled(_spec(), CONFIG_ID, DECIDED_AT)
        body = world.run(event("Update", ResourceProperties=_requested(False)))

        assert body["Status"] == "SUCCESS"
        assert body["Data"] == {"ConfigId": CONFIG_ID, "ExecutionStatus": "DISABLED"}
        assert world.mutations() == ["release.write:not_enabled", "control.update:DISABLED"]
        update = cast("UpdateOnlineEvaluationRequest", world.control.requests[0])
        assert update.spec == _spec(execution_requested=False)
        assert not _record(world).enabled

    def test_a_held_configuration_is_left_running_and_only_recalibrated(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED)
        stored = release_enabled(_spec(), CONFIG_ID, "2026-09-01T00:00:00+00:00")
        world.releases.records[RELEASE_PARAMETER] = stored
        body = world.run(event("Update"))

        assert body["Status"] == "SUCCESS"
        assert body["Data"] == {"ConfigId": CONFIG_ID, "ExecutionStatus": "ENABLED"}
        assert world.mutations() == EVALUATIONS
        assert _record(world) is stored

    def test_an_evaluator_set_change_follows_the_full_order(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED)
        world.releases.records[RELEASE_PARAMETER] = release_enabled(_spec(), CONFIG_ID, DECIDED_AT)
        revised = spec_document(
            evaluators=[
                evaluator_document(HELPFULNESS, HELPFULNESS_ID, revision="r2"),
                evaluator_document(GROUNDING, GROUNDING_ID),
            ]
        )
        body = world.run(event("Update", ResourceProperties=properties(revised)))

        assert body["Status"] == "SUCCESS"
        assert world.mutations() == FULL_ENABLE
        assert _record(world).evaluators[0].revision == "r2"

    def test_a_missing_record_on_a_running_configuration_is_not_trusted(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED)
        assert world.run(event("Update"))["Status"] == "SUCCESS"
        assert world.mutations() == FULL_ENABLE

    def test_a_drifted_running_configuration_is_disabled_rebound_and_recalibrated(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED, execution_role_arn="arn:aws:iam::1:role/other")
        world.releases.records[RELEASE_PARAMETER] = release_enabled(_spec(), CONFIG_ID, DECIDED_AT)
        assert world.run(event("Update"))["Status"] == "SUCCESS"
        assert world.mutations() == FULL_ENABLE

    def test_a_disabled_configuration_is_calibrated_before_enabling(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.DISABLED)
        assert world.run(event("Update"))["Status"] == "SUCCESS"
        # Already disabled and bound, so no disable is issued; the order is otherwise the LLD's.
        assert world.mutations() == [
            "release.write:not_enabled",
            *EVALUATIONS,
            "control.update:ENABLED",
            "release.write:enabled",
        ]

    def test_a_drifted_disabled_configuration_is_still_rebound(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.DISABLED, description="A drifted description.")
        assert world.run(event("Update"))["Status"] == "SUCCESS"
        assert world.mutations() == FULL_ENABLE

    def test_a_held_configuration_that_fails_calibration_is_disabled(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED)
        world.releases.records[RELEASE_PARAMETER] = release_enabled(_spec(), CONFIG_ID, DECIDED_AT)
        world.control.labels["fluent_but_unsupported", GROUNDING] = ("Grounded",)
        body = world.run(event("Update"))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == CONFIG_ID
        assert world.mutations() == [
            *EVALUATIONS,
            "release.write:not_enabled",
            "control.update:DISABLED",
        ]
        assert not _record(world).enabled

    def test_an_assertion_that_rebinding_cannot_fix_fails_disabled(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED)
        world.readback.results = [(GateFinding(GateAssertion.EXPORT_TASK),)]
        body = world.run(event("Update"))

        assert body["Reason"] == "Evaluation gate v4 refused: failed assertions export_task."
        assert world.mutations() == ["release.write:not_enabled", "control.update:DISABLED"]

    def test_a_name_change_creates_a_new_configuration_under_a_new_physical_id(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED)
        renamed = spec_document(config_name="adlytics_test_released_v3b")
        body = world.run(event("Update", ResourceProperties=properties(renamed)))

        new_id = f"adlytics_test_released_v3b-{SUFFIX}"
        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == new_id
        assert world.mutations()[:2] == ["release.write:not_enabled", "control.create"]
        assert _record(world).config_id == new_id

    def test_a_rollback_whose_old_properties_name_another_config_converges_in_place(
        self,
    ) -> None:
        world = World()
        _seed(world, ExecutionStatus.DISABLED)
        failed = properties(spec_document(config_name="adlytics_test_released_v3b"))
        body = world.run(event("Update", OldResourceProperties=failed))

        assert body["PhysicalResourceId"] == CONFIG_ID
        assert "control.create" not in world.mutations()

    def test_a_configuration_deleted_out_of_band_is_recreated(self) -> None:
        world = World()
        body = world.run(event("Update"))
        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == CONFIG_ID
        assert "control.create" in world.mutations()

    def test_a_failed_replacement_removes_the_new_config_and_keeps_the_old_id(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED)
        world.readback.results = [(), (GateFinding(GateAssertion.GROUP_KEY),)]
        renamed = spec_document(config_name="adlytics_test_released_v3b")
        body = world.run(event("Update", ResourceProperties=properties(renamed)))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == CONFIG_ID
        assert world.mutations()[-1] == "control.delete"

    def test_a_placeholder_physical_id_creates(self) -> None:
        world = World()
        body = world.run(event("Update", PhysicalResourceId=PLACEHOLDER))
        assert body["PhysicalResourceId"] == CONFIG_ID

    def test_a_configuration_being_deleted_is_not_changed(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.DISABLED, status=OnlineEvaluationStatus.DELETING)
        body = world.run(event("Update"))

        assert body["Reason"] == (
            f"Update: online evaluation configuration {CONFIG_ID} reached DELETING"
        )
        assert world.mutations() == []


class TestDelete:
    def test_records_not_enabled_disables_deletes_then_removes_the_record(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED)
        world.releases.records[RELEASE_PARAMETER] = release_enabled(_spec(), CONFIG_ID, DECIDED_AT)
        body = world.run(event("Delete"))

        assert body["Status"] == "SUCCESS"
        assert "Data" not in body
        assert world.mutations() == [
            "release.write:not_enabled",
            "control.update:DISABLED",
            "control.delete",
            "release.delete",
        ]
        assert _record(world) is None
        assert CONFIG_ID in world.control.configs  # deleting, then absent
        update = cast("UpdateOnlineEvaluationRequest", world.control.requests[0])
        assert update.spec is None

    def test_a_record_enabling_the_replacement_is_left_alone(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.DISABLED)
        successor = f"adlytics_test_released_v3b-{SUFFIX}"
        record = release_enabled(_spec(), successor, DECIDED_AT)
        world.releases.records[RELEASE_PARAMETER] = record
        assert world.run(event("Delete"))["Status"] == "SUCCESS"

        assert world.mutations() == ["control.delete"]
        assert _record(world) is record

    def test_a_failed_creates_placeholder_touches_only_the_record(self) -> None:
        world = World()
        world.releases.records[RELEASE_PARAMETER] = release_not_enabled(
            EvaluationFamilyV1.V4, DECIDED_AT
        )
        body = world.run(event("Delete", PhysicalResourceId=PLACEHOLDER))

        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == PLACEHOLDER
        assert world.mutations() == ["release.delete"]
        assert "control.get" not in world.log

    def test_a_placeholder_whose_spec_is_still_refused_succeeds_without_a_call(self) -> None:
        world = World()
        refused = {"ServiceToken": "x", "GateSpec": "{}"}
        body = world.run(
            event("Delete", PhysicalResourceId=PLACEHOLDER, ResourceProperties=refused)
        )

        assert body["Status"] == "SUCCESS"
        assert world.log == []

    def test_a_real_config_with_a_refused_spec_fails(self) -> None:
        world = World()
        body = world.run(event("Delete", ResourceProperties={"GateSpec": "{}"}))
        assert body["Status"] == "FAILED"
        assert world.log == []

    def test_an_absent_configuration_is_already_deleted(self) -> None:
        world = World()
        assert world.run(event("Delete"))["Status"] == "SUCCESS"
        assert world.mutations() == ["release.delete"]

    def test_gives_up_on_a_delete_that_does_not_finish(self) -> None:
        world = World(deadline=15.0)
        _seed(world, ExecutionStatus.DISABLED)

        def stuck(config_id: str) -> None:
            world.log.append("control.delete")
            world.control.configs[config_id].script = [OnlineEvaluationStatus.DELETING]

        world.control.delete = stuck  # type: ignore[method-assign]
        body = world.run(event("Delete"))
        assert body["Status"] == "FAILED"
        assert body["Reason"].endswith("last status DELETING.")


def _update_requests(world: World) -> list[UpdateOnlineEvaluationRequest]:
    return [
        request
        for request in world.control.requests
        if isinstance(request, UpdateOnlineEvaluationRequest)
    ]


def _token(callback: dict[str, object], operation: str) -> str:
    return client_token(parse_gate_event(callback), operation)


def _slow_gets(world: World, until: float) -> None:
    """Make every configuration read take until the given clock instant."""
    get = world.control.get

    def slow(config_id: str) -> Any:
        world.clock.now = max(world.clock.now, until)
        return get(config_id)

    world.control.get = slow  # type: ignore[method-assign]


class TestFailedDisableFailsClosed:
    def test_a_refused_bound_disable_is_followed_by_a_status_only_disable(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED)
        world.releases.records[RELEASE_PARAMETER] = release_enabled(_spec(), CONFIG_ID, DECIDED_AT)
        world.control.bound_update_error = DependencyError(
            EVALUATION_GATE_CONTROL_FAILED, "UpdateOnlineEvaluationConfig failed."
        )
        callback = event("Update", ResourceProperties=_requested(False))
        body = world.run(callback)

        assert body["Status"] == "FAILED"
        assert body["Reason"] == "UpdateOnlineEvaluationConfig failed."
        assert world.mutations() == [
            "release.write:not_enabled",
            "control.update:DISABLED",
            "control.update:DISABLED",
        ]
        bound, status_only = _update_requests(world)
        assert bound.spec is not None
        assert status_only.spec is None
        assert status_only.client_token == _token(callback, "Update:fail-closed-disable")
        assert status_only.client_token != bound.client_token
        assert world.control.configs[CONFIG_ID].state.execution_status is ExecutionStatus.DISABLED
        assert not _record(world).enabled

    def test_a_failed_wait_after_the_bound_disable_also_disables(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED, execution_role_arn="arn:aws:iam::1:role/other")
        world.control.after_update = [OnlineEvaluationStatus.UPDATE_FAILED]
        body = world.run(event("Update"))

        assert body["Reason"] == (
            f"Update: online evaluation configuration {CONFIG_ID} reached UPDATE_FAILED"
        )
        assert world.mutations() == [
            "release.write:not_enabled",
            "control.update:DISABLED",
            "control.update:DISABLED",
        ]
        assert _update_requests(world)[-1].spec is None

    def test_a_refused_fail_closed_disable_still_answers_the_original_failure(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED)
        world.control.update_error = DependencyError(
            EVALUATION_GATE_CONTROL_FAILED, "UpdateOnlineEvaluationConfig failed."
        )
        body = world.run(event("Update", ResourceProperties=_requested(False)))

        assert body["Reason"] == "UpdateOnlineEvaluationConfig failed."
        assert world.mutations() == [
            "release.write:not_enabled",
            "control.update:DISABLED",
            "control.update:DISABLED",
        ]


class TestDeadlineDiscipline:
    def test_nothing_is_written_once_the_deadline_has_passed(self) -> None:
        world = World(deadline=0.0)
        body = world.run(event("Create"))

        assert body["Status"] == "FAILED"
        assert body["Reason"] == (
            "Evaluation release record v4 write: gave up before the provider timeout; nothing "
            "further was changed."
        )
        assert world.mutations() == []

    def test_no_create_starts_after_a_record_write_that_outlasted_the_deadline(self) -> None:
        world = World(deadline=5.0)
        write = world.releases.write

        def slow(parameter_name: str, release: Any) -> None:
            world.clock.now = 5.0
            write(parameter_name, release)

        world.releases.write = slow  # type: ignore[method-assign]
        body = world.run(event("Create"))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == PLACEHOLDER
        assert body["Reason"] == (
            "Create: create: gave up before the provider timeout; nothing further was changed."
        )
        assert world.mutations() == ["release.write:not_enabled"]

    def test_no_poll_starts_after_the_deadline(self) -> None:
        world = World(deadline=5.0)
        _seed(world, ExecutionStatus.ENABLED)

        def stuck(request: UpdateOnlineEvaluationRequest) -> None:
            world.log.append(f"control.update:{request.execution_status}")
            world.clock.now = 5.0

        world.control.update = stuck  # type: ignore[method-assign]
        body = world.run(event("Update", ResourceProperties=_requested(False)))

        assert body["Reason"] == (
            f"Update: gave up waiting for online evaluation configuration {CONFIG_ID} before the "
            "provider timeout; last status not yet read."
        )

    def test_no_delete_or_record_delete_starts_after_the_deadline(self) -> None:
        world = World(deadline=5.0)
        _seed(world, ExecutionStatus.DISABLED)
        world.releases.records[RELEASE_PARAMETER] = release_not_enabled(
            EvaluationFamilyV1.V4, DECIDED_AT
        )
        _slow_gets(world, until=5.0)
        body = world.run(event("Delete"))

        assert body["Status"] == "FAILED"
        assert body["Reason"] == (
            "Delete: delete: gave up before the provider timeout; nothing further was changed."
        )
        assert world.mutations() == []
        assert _record(world) is not None

    def test_the_fail_closed_steps_still_run_past_the_deadline(self) -> None:
        world = World(deadline=5.0)
        world.control.after_create = [OnlineEvaluationStatus.ACTIVE]
        update = world.control.update

        def late(request: UpdateOnlineEvaluationRequest) -> None:
            update(request)
            if request.execution_status is ExecutionStatus.ENABLED:
                world.clock.now = 5.0

        world.control.update = late  # type: ignore[method-assign]
        body = world.run(event("Create"))

        assert body["Reason"] == (
            f"Create: gave up waiting for online evaluation configuration {CONFIG_ID} before the "
            "provider timeout; last status not yet read."
        )
        assert world.mutations() == [
            "release.write:not_enabled",
            "control.create",
            *EVALUATIONS,
            "control.update:ENABLED",
            "release.write:not_enabled",
            "control.update:DISABLED",
        ]
        assert not _record(world).enabled

    def test_the_handler_bounds_every_control_logs_and_ssm_call(self) -> None:
        # botocore's stubs declare no Config attributes; the values are set by the constructor.
        bounds = cast("Any", handler_module._CONTROL_BOUNDS)  # pyright: ignore[reportPrivateUsage]
        assert (bounds.connect_timeout, bounds.read_timeout) == (10, 20)
        assert bounds.retries == {"mode": "standard", "total_max_attempts": 3}


class TestReplayedCallback:
    def test_a_retried_disable_that_already_holds_issues_no_update(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED)
        callback = event("Update", ResourceProperties=_requested(False))
        assert world.run(callback)["Status"] == "SUCCESS"
        assert world.mutations() == ["release.write:not_enabled", "control.update:DISABLED"]

        world.log.clear()
        world.responder.sent.clear()
        body = world.run(callback)  # Lambda's async retry of the same event

        assert body["Status"] == "SUCCESS"
        assert world.mutations() == ["release.write:not_enabled"]


class TestNameCollision:
    def test_a_create_whose_name_exists_fails_without_touching_the_record(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED)
        live = release_enabled(_spec(), CONFIG_ID, DECIDED_AT)
        world.releases.records[RELEASE_PARAMETER] = live
        body = world.run(event("Create", LogicalResourceId="EvaluationGateV4Renamed"))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == PLACEHOLDER
        assert body["Reason"] == (
            f"Create: online evaluation configuration {CONFIG_ID} already has the name "
            "adlytics_test_released_v4; the release record was not written."
        )
        assert world.mutations() == []
        assert _record(world) is live

    def test_the_rollback_delete_of_that_placeholder_neither_writes_nor_deletes(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED)
        live = release_enabled(_spec(), CONFIG_ID, DECIDED_AT)
        world.releases.records[RELEASE_PARAMETER] = live
        body = world.run(event("Delete", PhysicalResourceId=PLACEHOLDER))

        assert body["Status"] == "SUCCESS"
        assert world.mutations() == []
        assert _record(world) is live
        assert world.control.configs[CONFIG_ID].state.execution_status is ExecutionStatus.ENABLED

    def test_a_replacement_whose_name_exists_fails_and_keeps_the_old_id(self) -> None:
        world = World()
        _seed(world, ExecutionStatus.ENABLED)
        body = world.run(event("Update", PhysicalResourceId=PLACEHOLDER))

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == PLACEHOLDER
        assert world.mutations() == []


class TestExactlyOneResponse:
    def test_an_invalid_spec_fails_without_any_call(self) -> None:
        world = World()
        body = world.run(
            event("Create", ResourceProperties=properties(spec_document(sampling_percentage=0)))
        )

        assert body["Status"] == "FAILED"
        assert body["PhysicalResourceId"] == PLACEHOLDER
        assert body["Reason"] == "GateSpec.sampling_percentage must be in (0, 100]."
        assert world.log == []

    def test_an_unexpected_exception_is_answered_once_without_its_message(self) -> None:
        world = World()
        world.control.create_error = RuntimeError("fictional answer text")
        body = world.run(event("Create"))
        assert body["Reason"] == (
            "Unexpected RuntimeError in the evaluation gate provider; RequestId request-1."
        )

    def test_a_long_reason_is_bounded(self) -> None:
        world = World()
        world.control.create_error = DependencyError(
            EVALUATION_GATE_CONTROL_FAILED,
            "CreateOnlineEvaluationConfig failed.",
            context={"service_code": "ValidationException", "provider_message": "x" * 5000},
        )
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
        with pytest.raises(InvariantError):
            run(RunRequest(event=callback, runtime=world.runtime(), deadline=600.0))
        assert world.responder.sent == []
        assert world.log == []


class _Context:
    function_name = "adlytics-test-EvaluationGate"
    function_version = "$LATEST"
    invoked_function_arn = (
        "arn:aws:lambda:us-east-1:123456789012:function:adlytics-test-EvaluationGate"
    )
    memory_limit_in_mb = 512
    aws_request_id = "lambda-request-1"

    def __init__(self, remaining_ms: int) -> None:
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms


def _handled(
    monkeypatch: pytest.MonkeyPatch, world: World, remaining_ms: int
) -> tuple[dict[str, str], dict[str, Any]]:
    """Run one Create through the Lambda entry point and return its root span's attributes."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "TRACER", provider.get_tracer("test"))
    runtime = world.runtime()
    monkeypatch.setattr(handler_module, "_runtime", lambda: runtime)
    result = handler_module.handler(event("Create"), _Context(remaining_ms))
    (root,) = [item for item in exporter.get_finished_spans() if item.name == EVALUATION_GATE]
    return result, dict(root.attributes or {})


class TestHandler:
    def test_the_lambda_budget_is_read_and_a_missing_budget_is_zero(self) -> None:
        assert handler_module.remaining_seconds(_Context(900_000)) == 900.0
        assert handler_module.remaining_seconds(object()) == 0.0

    def test_answers_failed_before_the_lambda_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world = World(clock=FakeClock())
        world.control.after_create = [OnlineEvaluationStatus.CREATING]
        result, attributes = _handled(monkeypatch, world, remaining_ms=300_000)

        assert result == {"status": "FAILED"}
        body = world.responder.only()
        assert body["Reason"].startswith("Create: gave up waiting")
        # 300 s remaining less the 240 s response margin leaves 60 s of polling.
        assert sum(world.clock.sleeps) == 60.0
        assert attributes["outcome"] == "failed"
        assert attributes["error_type"] == "_ProvisionFailedError"

    def test_the_span_records_content_free_gate_facts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world = World()
        result, attributes = _handled(monkeypatch, world, remaining_ms=900_000)

        assert result == {"status": "SUCCESS"}
        assert attributes == {
            "operation": EVALUATION_GATE,
            "outcome": "success",
            "request_type": "Create",
            "family": "v4",
            "requested": True,
            "execution_status": "ENABLED",
            "failed_assertion_count": 0,
            "calibration_case_count": 3,
            "calibration_failure_count": 0,
        }

    def test_a_refusal_marks_the_span_with_its_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        world = World()
        world.readback.results = [(GateFinding(GateAssertion.GROUP_EXISTS),)]
        _, attributes = _handled(monkeypatch, world, remaining_ms=900_000)

        assert attributes["error_code"] == str(EVALUATION_GATE_REFUSED)
        assert attributes["error_class"] == "invariant"
        assert attributes["alarms"] is True
        assert attributes["failed_assertion_count"] == 1
