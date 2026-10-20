# SPDX-License-Identifier: MIT-0
"""Fictional gate specs and in-memory port fakes for the evaluation-gate unit tests (ADR-079).

The fakes implement the gate's own ports, never an AWS API: one shared ordered log records every
call so a test can assert the order the LLD mandates (record not enabled before any disable,
enable before the enabled record).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

from adapters.jobs.evaluation_gate.provider import ProviderRuntime, RunRequest, RunResult, run
from domain.evaluation.gate import (
    CalibrationObservationV1,
    EvaluatorState,
    ExecutionStatus,
    GateFinding,
    OnlineEvaluationConfigState,
    OnlineEvaluationStatus,
    ResultGroupCustodyV1,
)
from domain.evaluation.release import EvaluationReleaseV1
from ports.outbound.custom_resource_response import CustomResourceResponse
from ports.outbound.evaluation_gate import (
    CalibrationEvaluateRequest,
    CreateOnlineEvaluationRequest,
    OnlineEvaluationNotFoundError,
    UpdateOnlineEvaluationRequest,
)

RESPONSE_URL = "https://cloudformation-custom-resource-response.example/presigned?sig=abc"
STACK_ID = "arn:aws:cloudformation:us-east-1:123456789012:stack/Adlytics-EvaluationV4-test/guid"
LOGICAL_ID = "EvaluationGateV4"
# A-TEST-LITERALS-001: a ServiceToken ARN; see docs/security-dispositions.md
SERVICE_TOKEN = "arn:aws:lambda:us-east-1:123456789012:function:adlytics-test-EvaluationGate"  # nosec B105
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
SUFFIX = "AbCdE12345"
CONFIG_NAME = "adlytics_test_released_v4"
CONFIG_ID = f"{CONFIG_NAME}-{SUFFIX}"
RESULT_GROUP = "/adlytics/test/evaluation/results-v4"
RESULT_GROUP_ARN = f"arn:aws:logs:us-east-1:123456789012:log-group:{RESULT_GROUP}"
INGRESS_ARN = "arn:aws:lambda:us-east-1:123456789012:function:adlytics-test-EvaluationResultIngress"
FILTER_NAME = "adlytics-test-evaluation-result-ingress-v4"
RELEASE_PARAMETER = "/adlytics/test/evaluation/release-v4"
EXECUTION_ROLE = "arn:aws:iam::123456789012:role/adlytics-test-evaluation-execution-v4"
INPUT_GROUP = "/adlytics/test/evaluation/released-responses"
SERVICE_NAME = "adlytics-test-released-response"
HELPFULNESS = "adlytics_test_helpfulness_v4"
GROUNDING = "adlytics_test_grounding_v4"
HELPFULNESS_ID = f"{HELPFULNESS}-HeLpFuL123"
GROUNDING_ID = f"{GROUNDING}-GrOuNd1234"
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
DECIDED_AT = "2026-09-27T12:00:00+00:00"


def evaluator_document(name: str, evaluator_id: str, **overrides: object) -> dict[str, object]:
    """One spec evaluator in the stack's wire shape."""
    return {
        "evaluator_id": evaluator_id,
        "evaluator_name": name,
        "revision": "r1",
        "model_id": "fictional.judge-model-v1:0",
        "prompt_digest": "a" * 64,
        **overrides,
    }


def expected_labels(document: Mapping[str, object]) -> dict[tuple[str, str], tuple[str, ...]]:
    """Answer every expectation with its first acceptable label, as a calibrated judge does."""
    table: dict[tuple[str, str], tuple[str, ...]] = {}
    for case in cast("list[dict[str, Any]]", document["calibration"]):
        for evaluator, labels in cast("dict[str, Any]", case["expected"]).items():
            first = labels if isinstance(labels, str) else labels[0]
            table[str(case["name"]), evaluator] = (first,)
    return table


def case_document(
    name: str, expected: Mapping[str, object], kind: str = "known_correct", **overrides: object
) -> dict[str, object]:
    """One fictional calibration case in the stack's wire shape."""
    return {
        "name": name,
        "kind": kind,
        "question": "How many fictional widgets shipped?",
        "grounded_context": "The fictional ledger records 12 widgets shipped.",
        "candidate": "Twelve fictional widgets shipped.",
        "expected": dict(expected),
        **overrides,
    }


def spec_document(**overrides: object) -> dict[str, object]:
    """Build a two-evaluator V4 spec with three cases each; None removes a member."""
    document: dict[str, object] = {
        "schema_version": "EvaluationGateSpec.v1",
        "family": "v4",
        "config_name": CONFIG_NAME,
        "description": "Evaluates exact released fictional responses.",
        "execution_requested": True,
        "execution_role_arn": EXECUTION_ROLE,
        "input_log_group_names": [INPUT_GROUP],
        "input_service_names": [SERVICE_NAME],
        "sampling_percentage": 100,
        "session_timeout_minutes": 5,
        "result_log_group_name": RESULT_GROUP,
        "result_log_group_arn": RESULT_GROUP_ARN,
        "result_retention_days": 14,
        "ingress_subscription_filter_name": FILTER_NAME,
        "ingress_function_arn": INGRESS_ARN,
        "release_parameter_name": RELEASE_PARAMETER,
        "evaluators": [
            evaluator_document(HELPFULNESS, HELPFULNESS_ID),
            evaluator_document(GROUNDING, GROUNDING_ID),
        ],
        "calibration": [
            case_document("known_correct", {HELPFULNESS: "Excellent", GROUNDING: "Grounded"}),
            case_document(
                "contradiction",
                {HELPFULNESS: ["Poor", "Needs Improvement"], GROUNDING: "Unsupported"},
                "contradiction",
                candidate="Seven fictional widgets shipped.",
            ),
            case_document(
                "fluent_but_unsupported",
                {HELPFULNESS: ["Good", "Excellent"], GROUNDING: "Unsupported"},
                "fluent_unsupported",
                grounded_context="",
                candidate="Twelve widgets shipped, a record for the fictional region.",
            ),
        ],
    }
    document.update(overrides)
    return {key: value for key, value in document.items() if value is not None}


def properties(document: Mapping[str, object] | None = None, **extra: str) -> dict[str, str]:
    """ResourceProperties as CloudFormation delivers them: every value is text."""
    spec = json.dumps(spec_document() if document is None else document)
    return {"ServiceToken": SERVICE_TOKEN, "GateSpec": spec, "SpecDigest": "d" * 64, **extra}


def event(request_type: str, **overrides: object) -> dict[str, object]:
    """Build one callback envelope."""
    base: dict[str, object] = {
        "RequestType": request_type,
        "ServiceToken": SERVICE_TOKEN,
        "ResponseURL": RESPONSE_URL,
        "StackId": STACK_ID,
        "RequestId": "request-1",
        "LogicalResourceId": LOGICAL_ID,
        "ResourceType": "Custom::AgentCoreOnlineEvaluation",
        "ResourceProperties": properties(),
    }
    if request_type != "Create":
        base["PhysicalResourceId"] = CONFIG_ID
    if request_type == "Update":
        base["OldResourceProperties"] = properties()
    base.update(overrides)
    return base


@dataclass
class _Config:
    state: OnlineEvaluationConfigState
    script: list[OnlineEvaluationStatus | None]
    """Statuses successive gets report; the last is sticky; None means not found from then."""


def _bound(
    config_id: str, request_spec: Any, execution: ExecutionStatus
) -> OnlineEvaluationConfigState:
    return OnlineEvaluationConfigState(
        config_id=config_id,
        config_name=request_spec.config_name,
        description=request_spec.description,
        status=OnlineEvaluationStatus.ACTIVE,
        execution_status=execution,
        output_log_group_name=request_spec.result_log_group_name,
        input_log_group_names=request_spec.input_log_group_names,
        input_service_names=request_spec.input_service_names,
        evaluator_ids=request_spec.evaluator_ids,
        execution_role_arn=request_spec.execution_role_arn,
        sampling_percentage=request_spec.sampling_percentage,
        session_timeout_minutes=request_spec.session_timeout_minutes,
    )


@dataclass
class FakeControl:
    """In-memory configurations, evaluators, and a scripted judge."""

    log: list[str]
    configs: dict[str, _Config] = field(default_factory=dict[str, _Config])
    evaluators: dict[str, EvaluatorState] = field(default_factory=dict[str, EvaluatorState])
    labels: dict[tuple[str, str], tuple[str, ...]] = field(
        default_factory=dict[tuple[str, str], tuple[str, ...]]
    )
    """The judge's labels per (case, evaluator name); an absent pair returns no label."""
    errors: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])
    create_error: Exception | None = None
    create_failure_reason: str = ""
    update_error: Exception | None = None
    bound_update_error: Exception | None = None
    """Raised only by an update that rebinds the configuration to a spec."""
    evaluate_error: Exception | None = None
    after_create: list[OnlineEvaluationStatus | None] = field(
        default_factory=lambda: [OnlineEvaluationStatus.CREATING, OnlineEvaluationStatus.ACTIVE]
    )
    after_update: list[OnlineEvaluationStatus | None] = field(
        default_factory=lambda: [OnlineEvaluationStatus.UPDATING, OnlineEvaluationStatus.ACTIVE]
    )
    requests: list[object] = field(default_factory=list[object])
    evaluations: list[CalibrationEvaluateRequest] = field(
        default_factory=list[CalibrationEvaluateRequest]
    )

    def seed(
        self,
        spec: Any,
        execution: ExecutionStatus,
        **drift: object,
    ) -> None:
        """Install the live configuration exactly bound to a spec, then apply drift."""
        state = replace(_bound(CONFIG_ID, spec, execution), **cast("dict[str, Any]", drift))
        self.configs[CONFIG_ID] = _Config(state, [state.status])

    def create(self, request: CreateOnlineEvaluationRequest) -> str:
        self.log.append("control.create")
        self.requests.append(request)
        if self.create_error is not None:
            raise self.create_error
        config_id = f"{request.spec.config_name}-{SUFFIX}"
        state = _bound(config_id, request.spec, ExecutionStatus.DISABLED)
        self.configs[config_id] = _Config(
            replace(state, failure_reason=self.create_failure_reason), list(self.after_create)
        )
        return config_id

    def get(self, config_id: str) -> OnlineEvaluationConfigState:
        self.log.append("control.get")
        config = self.configs.get(config_id)
        if config is None:
            raise OnlineEvaluationNotFoundError(config_id)
        status = config.script.pop(0) if len(config.script) > 1 else config.script[0]
        if status is None:
            raise OnlineEvaluationNotFoundError(config_id)
        return replace(config.state, status=status)

    def find_by_name(self, config_name: str) -> OnlineEvaluationConfigState | None:
        self.log.append("control.find_by_name")
        for config in self.configs.values():
            if config.state.config_name == config_name and config.script[0] is not None:
                return config.state
        return None

    def update(self, request: UpdateOnlineEvaluationRequest) -> None:
        self.log.append(f"control.update:{request.execution_status}")
        self.requests.append(request)
        if self.update_error is not None:
            raise self.update_error
        if self.bound_update_error is not None and request.spec is not None:
            raise self.bound_update_error
        config = self.configs.get(request.config_id)
        if config is None:
            raise OnlineEvaluationNotFoundError(request.config_id)
        state = config.state
        if request.spec is not None:
            state = _bound(request.config_id, request.spec, request.execution_status)
        config.state = replace(state, execution_status=request.execution_status)
        config.script = list(self.after_update)

    def delete(self, config_id: str) -> None:
        self.log.append("control.delete")
        config = self.configs.get(config_id)
        if config is None:
            raise OnlineEvaluationNotFoundError(config_id)
        config.script = [OnlineEvaluationStatus.DELETING, None]

    def evaluator(self, evaluator_id: str) -> EvaluatorState:
        self.log.append("control.evaluator")
        return self.evaluators[evaluator_id]

    def evaluate(self, request: CalibrationEvaluateRequest) -> CalibrationObservationV1:
        self.log.append("control.evaluate")
        self.evaluations.append(request)
        if self.evaluate_error is not None:
            raise self.evaluate_error
        calibration = request.calibration
        key = (calibration.case_name, calibration.evaluator_name)
        if key in self.errors:
            return CalibrationObservationV1(*key, error_code=self.errors[key])
        return CalibrationObservationV1(*key, labels=self.labels.get(key, ()))


@dataclass
class FakeReadback:
    log: list[str]
    results: list[tuple[GateFinding, ...]] = field(default_factory=list[tuple[GateFinding, ...]])
    """Findings for successive readbacks; the last is sticky; empty means every control holds."""

    def findings(self, custody: ResultGroupCustodyV1) -> tuple[GateFinding, ...]:
        del custody
        self.log.append("readback.findings")
        if not self.results:
            return ()
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


@dataclass
class FakeReleases:
    log: list[str]
    records: dict[str, EvaluationReleaseV1] = field(default_factory=dict[str, EvaluationReleaseV1])
    corrupt_reads: bool = False

    def read(self, parameter_name: str) -> EvaluationReleaseV1 | None:
        self.log.append("release.read")
        record = self.records.get(parameter_name)
        if record is not None and self.corrupt_reads:
            return replace(record, decided_at="2000-01-01T00:00:00+00:00")
        return record

    def write(self, parameter_name: str, release: EvaluationReleaseV1) -> None:
        self.log.append(f"release.write:{'enabled' if release.enabled else 'not_enabled'}")
        self.records[parameter_name] = release

    def delete(self, parameter_name: str) -> None:
        self.log.append("release.delete")
        self.records.pop(parameter_name, None)


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


def live_evaluators(spec_evaluators: list[dict[str, object]]) -> dict[str, EvaluatorState]:
    """Build live evaluators whose identity, model, and prompt digest match the spec's."""
    return {
        str(item["evaluator_id"]): EvaluatorState(
            evaluator_id=str(item["evaluator_id"]),
            evaluator_name=str(item["evaluator_name"]),
            status="ACTIVE",
            model_id=str(item["model_id"]),
            prompt_digest=str(item["prompt_digest"]),
        )
        for item in spec_evaluators
    }


@dataclass
class World:
    log: list[str] = field(default_factory=list[str])
    control: FakeControl = field(init=False)
    readback: FakeReadback = field(init=False)
    releases: FakeReleases = field(init=False)
    responder: RecordingResponder = field(default_factory=RecordingResponder)
    clock: FakeClock = field(default_factory=FakeClock)
    deadline: float = 660.0
    last: RunResult | None = None

    def __post_init__(self) -> None:
        self.control = FakeControl(self.log)
        self.readback = FakeReadback(self.log)
        self.releases = FakeReleases(self.log)
        document = spec_document()
        self.control.evaluators = live_evaluators(
            cast("list[dict[str, object]]", document["evaluators"])
        )
        self.control.labels = expected_labels(document)

    def runtime(self) -> ProviderRuntime:
        return ProviderRuntime(
            control=self.control,
            readback=self.readback,
            releases=self.releases,
            responder=self.responder,
            clock=self.clock,
            sleep=self.clock.sleep,
            now=lambda: NOW,
        )

    def run(self, callback: object) -> dict[str, Any]:
        self.last = run(RunRequest(event=callback, runtime=self.runtime(), deadline=self.deadline))
        body = self.responder.only()
        assert body["Status"] == self.last.status
        return body

    def mutations(self) -> list[str]:
        """Return the ordered writes and judge calls, without the reads between them."""
        prefixes = (
            "release.write",
            "release.delete",
            "control.create",
            "control.update",
            "control.delete",
            "control.evaluate",
        )
        return [entry for entry in self.log if entry.startswith(prefixes)]
