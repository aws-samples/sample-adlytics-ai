# SPDX-License-Identifier: MIT-0
"""boto3 implementation of the evaluation gate's control-plane port (ADR-079).

Configuration create, read, list, update, and delete go through `bedrock-agentcore-control`, always
bound to the family's owned result group through `outputConfig.cloudWatchConfig.logGroupName`
and never enabled on create. Evaluators are read back through GetEvaluator with their judge
instructions reduced to a SHA-256 digest here, so no port or log ever carries the prompt.
Calibration scores each fictional case through `bedrock-agentcore` Evaluate with exactly one
session span serialized by the released-response serializer, so the gate calibrates the span
shape online evaluation receives and no second shape exists.

`ResourceNotFoundException` from the control plane is the one expected control-flow error and
becomes `OnlineEvaluationNotFoundError`; every other SDK failure becomes a `DependencyError`
with the shared provider diagnostic. An Evaluate refusal keeps only the service code: its
message could quote the calibration text. A per-result error is an observation with its code
alone, and an evaluator's explanation is never read.

Request shaping, response projection, and error translation are pure functions, unit-tested
without an SDK stand-in.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, cast

from botocore.exceptions import BotoCoreError, ClientError

from adapters.jobs.evaluation_writer.span import ReleasedSpanProjection, released_span_json
from adapters.outbound.aws_errors import aws_failure_context
from domain.evaluation.gate import (
    CalibrationObservationV1,
    EvaluatorState,
    ExecutionStatus,
    OnlineEvaluationConfigState,
    OnlineEvaluationStatus,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.evaluation_gate import (
    CalibrationEvaluateRequest,
    CreateOnlineEvaluationRequest,
    OnlineEvaluationControlPort,
    OnlineEvaluationNotFoundError,
    UpdateOnlineEvaluationRequest,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from types_boto3_bedrock_agentcore.client import BedrockAgentCoreClient
    from types_boto3_bedrock_agentcore.type_defs import (
        EvaluateRequestTypeDef,
        EvaluateResponseTypeDef,
    )
    from types_boto3_bedrock_agentcore_control.client import BedrockAgentCoreControlClient
    from types_boto3_bedrock_agentcore_control.type_defs import (
        CreateOnlineEvaluationConfigRequestTypeDef,
        GetEvaluatorResponseTypeDef,
        GetOnlineEvaluationConfigResponseTypeDef,
        OnlineEvaluationConfigSummaryTypeDef,
        UpdateOnlineEvaluationConfigRequestTypeDef,
    )

    from domain.evaluation.gate import EvaluationGateSpecV1

__all__ = [
    "EVALUATION_GATE_CONTROL_FAILED",
    "AgentCoreOnlineEvaluationControl",
    "config_state",
    "create_request",
    "evaluate_request",
    "evaluator_state",
    "named_config",
    "observation",
    "translated",
    "update_request",
]

EVALUATION_GATE_CONTROL_FAILED: Final = ErrorCode("EVALUATION", 9)
_NOT_FOUND: Final = "ResourceNotFoundException"
_LIST_PAGE_SIZE: Final = 100
"""ListOnlineEvaluationConfigs' maxResults upper bound in the service model."""
_MAX_LIST_PAGES: Final = 50


def _bound_members(spec: EvaluationGateSpecV1) -> dict[str, object]:
    """Every member that binds a configuration to its spec, shared by create and update."""
    return {
        "description": spec.description,
        "rule": {
            "samplingConfig": {"samplingPercentage": spec.sampling_percentage},
            "sessionConfig": {"sessionTimeoutMinutes": spec.session_timeout_minutes},
        },
        "dataSourceConfig": {
            "cloudWatchLogs": {
                "logGroupNames": list(spec.input_log_group_names),
                "serviceNames": list(spec.input_service_names),
            }
        },
        "evaluators": [{"evaluatorId": evaluator_id} for evaluator_id in spec.evaluator_ids],
        "outputConfig": {"cloudWatchConfig": {"logGroupName": spec.result_log_group_name}},
        "evaluationExecutionRoleArn": spec.execution_role_arn,
    }


def create_request(
    request: CreateOnlineEvaluationRequest,
) -> CreateOnlineEvaluationConfigRequestTypeDef:
    """Shape CreateOnlineEvaluationConfig: the spec's binding, never enabled on create."""
    return cast(
        "CreateOnlineEvaluationConfigRequestTypeDef",
        {
            "onlineEvaluationConfigName": request.spec.config_name,
            **_bound_members(request.spec),
            "enableOnCreate": False,
            "clientToken": request.client_token,
            # The provider role may create only with exactly the standard tag set; an empty set
            # is sent as no member, which that grant refuses (H-IAM-AGENTCORE-CREATE-001).
            **({"tags": dict(request.spec.tags)} if request.spec.tags else {}),
        },
    )


def update_request(
    request: UpdateOnlineEvaluationRequest,
) -> UpdateOnlineEvaluationConfigRequestTypeDef:
    """Shape UpdateOnlineEvaluationConfig: the execution state, and the full binding if given."""
    members = {} if request.spec is None else _bound_members(request.spec)
    return cast(
        "UpdateOnlineEvaluationConfigRequestTypeDef",
        {
            "onlineEvaluationConfigId": request.config_id,
            **members,
            "executionStatus": str(request.execution_status),
            "clientToken": request.client_token,
        },
    )


def config_state(response: GetOnlineEvaluationConfigResponseTypeDef) -> OnlineEvaluationConfigState:
    """Project GetOnlineEvaluationConfig onto the members the gate asserts."""
    rule = response.get("rule", {})
    source = response.get("dataSourceConfig", {}).get("cloudWatchLogs", {})
    output = response.get("outputConfig", {}).get("cloudWatchConfig", {})
    return OnlineEvaluationConfigState(
        config_id=response["onlineEvaluationConfigId"],
        config_name=response["onlineEvaluationConfigName"],
        status=OnlineEvaluationStatus(response["status"]),
        execution_status=ExecutionStatus(response["executionStatus"]),
        failure_reason=response.get("failureReason", ""),
        description=response.get("description", ""),
        output_log_group_name=output.get("logGroupName", ""),
        result_destination=output.get("resultDestination", ""),
        input_log_group_names=tuple(source.get("logGroupNames", [])),
        input_log_group_prefixes=tuple(source.get("logGroupNamePrefixes", [])),
        input_service_names=tuple(source.get("serviceNames", [])),
        evaluator_ids=tuple(
            evaluator.get("evaluatorId", "") for evaluator in response.get("evaluators", [])
        ),
        execution_role_arn=response.get("evaluationExecutionRoleArn", ""),
        sampling_percentage=float(rule.get("samplingConfig", {}).get("samplingPercentage", 0.0)),
        session_timeout_minutes=int(rule.get("sessionConfig", {}).get("sessionTimeoutMinutes", 0)),
        filter_count=len(rule.get("filters", [])),
    )


def named_config(
    summaries: list[OnlineEvaluationConfigSummaryTypeDef], config_name: str
) -> OnlineEvaluationConfigState | None:
    """Project the listed configuration with this name onto its identity and status only."""
    for summary in summaries:
        if summary.get("onlineEvaluationConfigName") == config_name:
            return OnlineEvaluationConfigState(
                config_id=summary["onlineEvaluationConfigId"],
                config_name=config_name,
                status=OnlineEvaluationStatus(summary["status"]),
                execution_status=ExecutionStatus(summary["executionStatus"]),
                failure_reason=summary.get("failureReason", ""),
                description=summary.get("description", ""),
            )
    return None


def evaluator_state(response: GetEvaluatorResponseTypeDef) -> EvaluatorState:
    """Project GetEvaluator, reducing the judge instructions to their SHA-256 digest."""
    judge = response.get("evaluatorConfig", {}).get("llmAsAJudge")
    model_id = ""
    prompt_digest = ""
    if judge is not None:
        bedrock = judge.get("modelConfig", {}).get("bedrockEvaluatorModelConfig")
        model_id = "" if bedrock is None else bedrock.get("modelId", "")
        instructions = judge.get("instructions")
        if instructions:
            prompt_digest = hashlib.sha256(instructions.encode("utf-8")).hexdigest()
    return EvaluatorState(
        evaluator_id=response["evaluatorId"],
        evaluator_name=response["evaluatorName"],
        status=str(response["status"]),
        model_id=model_id,
        prompt_digest=prompt_digest,
    )


def evaluate_request(
    request: CalibrationEvaluateRequest, timestamp_ns: int
) -> EvaluateRequestTypeDef:
    """Shape Evaluate over one released-response span of one calibration case."""
    calibration = request.calibration
    span = released_span_json(
        ReleasedSpanProjection(
            envelope=calibration.envelope,
            service_name=request.service_name,
            timestamp_ns=timestamp_ns,
            trace_id=calibration.trace_id,
            span_id=calibration.span_id,
        )
    )
    document = cast("dict[str, object]", json.loads(span))
    return {
        "evaluatorId": calibration.evaluator_id,
        "evaluationInput": {"sessionSpans": [document]},
    }


def observation(
    request: CalibrationEvaluateRequest, response: EvaluateResponseTypeDef
) -> CalibrationObservationV1:
    """Reduce Evaluate's results for the requested evaluator to labels or an error code."""
    calibration = request.calibration
    results = [
        result
        for result in response.get("evaluationResults", [])
        if result.get("evaluatorId") == calibration.evaluator_id
    ]
    error_code = next(
        (
            result.get("errorCode") or "error"
            for result in results
            if result.get("errorCode") or result.get("errorMessage")
        ),
        "",
    )
    labels = tuple(
        label for result in results if isinstance(label := result.get("label"), str) and label
    )
    return CalibrationObservationV1(
        case_name=calibration.case_name,
        evaluator_name=calibration.evaluator_name,
        labels=() if error_code else labels,
        error_code=error_code,
    )


_CONFIG_OPERATIONS: Final = frozenset(
    {
        "GetOnlineEvaluationConfig",
        "UpdateOnlineEvaluationConfig",
        "DeleteOnlineEvaluationConfig",
    }
)
"""Where not-found names the configuration; elsewhere (an evaluator) it is a refusal."""
_CONTENT_OPERATIONS: Final = frozenset({"Evaluate"})
"""Whose refusal message could quote the calibration text, so only its code is kept."""


def translated(error: ClientError | BotoCoreError, operation: str) -> Exception:
    """Map an SDK failure to not-found or a DependencyError carrying the provider diagnostic."""
    code = error.response.get("Error", {}).get("Code") if isinstance(error, ClientError) else None
    if code == _NOT_FOUND and operation in _CONFIG_OPERATIONS:
        return OnlineEvaluationNotFoundError(operation)
    context = {"operation": operation, **aws_failure_context(error)}
    if operation in _CONTENT_OPERATIONS:
        context.pop("provider_message", None)
    return DependencyError(EVALUATION_GATE_CONTROL_FAILED, f"{operation} failed.", context=context)


class AgentCoreOnlineEvaluationControl(OnlineEvaluationControlPort):
    """One control-plane client and one data-plane client per process."""

    def __init__(
        self,
        control: BedrockAgentCoreControlClient,
        runtime: BedrockAgentCoreClient,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._control = control
        self._runtime = runtime
        self._clock_ns = clock_ns

    def create(self, request: CreateOnlineEvaluationRequest) -> str:
        """Call CreateOnlineEvaluationConfig."""
        try:
            response = self._control.create_online_evaluation_config(**create_request(request))
        except (ClientError, BotoCoreError) as error:
            raise translated(error, "CreateOnlineEvaluationConfig") from error
        return response["onlineEvaluationConfigId"]

    def get(self, config_id: str) -> OnlineEvaluationConfigState:
        """Call GetOnlineEvaluationConfig."""
        try:
            response = self._control.get_online_evaluation_config(
                onlineEvaluationConfigId=config_id
            )
        except (ClientError, BotoCoreError) as error:
            raise translated(error, "GetOnlineEvaluationConfig") from error
        return config_state(response)

    def find_by_name(self, config_name: str) -> OnlineEvaluationConfigState | None:
        """Page ListOnlineEvaluationConfigs for the configuration with this name."""
        # A-PROTOCOL-CONSTANT-001: the empty paging cursor; see docs/security-dispositions.md
        token = ""  # nosec B105
        for _ in range(_MAX_LIST_PAGES):
            try:
                response = (
                    self._control.list_online_evaluation_configs(
                        maxResults=_LIST_PAGE_SIZE, nextToken=token
                    )
                    if token
                    else self._control.list_online_evaluation_configs(maxResults=_LIST_PAGE_SIZE)
                )
            except (ClientError, BotoCoreError) as error:
                raise translated(error, "ListOnlineEvaluationConfigs") from error
            found = named_config(list(response.get("onlineEvaluationConfigs", [])), config_name)
            if found is not None:
                return found
            token = response.get("nextToken", "")
            if not token:
                return None
        raise DependencyError(
            EVALUATION_GATE_CONTROL_FAILED,
            f"ListOnlineEvaluationConfigs did not finish within {_MAX_LIST_PAGES} pages.",
            context={"operation": "ListOnlineEvaluationConfigs"},
        )

    def update(self, request: UpdateOnlineEvaluationRequest) -> None:
        """Call UpdateOnlineEvaluationConfig."""
        try:
            self._control.update_online_evaluation_config(**update_request(request))
        except (ClientError, BotoCoreError) as error:
            raise translated(error, "UpdateOnlineEvaluationConfig") from error

    def delete(self, config_id: str) -> None:
        """Call DeleteOnlineEvaluationConfig."""
        try:
            self._control.delete_online_evaluation_config(onlineEvaluationConfigId=config_id)
        except (ClientError, BotoCoreError) as error:
            raise translated(error, "DeleteOnlineEvaluationConfig") from error

    def evaluator(self, evaluator_id: str) -> EvaluatorState:
        """Call GetEvaluator with its instructions, which are digested and dropped here."""
        try:
            response = self._control.get_evaluator(
                evaluatorId=evaluator_id, includedData="ALL_DATA"
            )
        except (ClientError, BotoCoreError) as error:
            raise translated(error, "GetEvaluator") from error
        return evaluator_state(response)

    def evaluate(self, request: CalibrationEvaluateRequest) -> CalibrationObservationV1:
        """Call Evaluate on one calibration span."""
        try:
            response = self._runtime.evaluate(**evaluate_request(request, self._clock_ns()))
        except (ClientError, BotoCoreError) as error:
            raise translated(error, "Evaluate") from error
        return observation(request, response)
