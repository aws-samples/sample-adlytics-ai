# SPDX-License-Identifier: MIT-0
"""The evaluation gate's AgentCore, CloudWatch Logs, and Parameter Store adapters (ADR-079).

Request shaping, response projection, finding rules, and resource-policy matching are pure
functions tested directly. The adapter classes are driven by recording clients that return one
scripted response per call, never a fake of the service: they prove what is sent and how each
documented response shape is read, not that the service behaves that way.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from evaluation_gate_support import (
    CONFIG_ID,
    EXECUTION_ROLE,
    FILTER_NAME,
    GROUNDING,
    GROUNDING_ID,
    HELPFULNESS,
    HELPFULNESS_ID,
    INGRESS_ARN,
    INPUT_GROUP,
    RELEASE_PARAMETER,
    RESULT_GROUP,
    RESULT_GROUP_ARN,
    SERVICE_NAME,
    spec_document,
)

from adapters.inbound.conversation.contract_codec import contract_to_json
from adapters.jobs.evaluation_writer.span import ReleasedSpanProjection, released_span_json
from adapters.outbound.agentcore.online_evaluation_control import (
    EVALUATION_GATE_CONTROL_FAILED,
    AgentCoreOnlineEvaluationControl,
    config_state,
    create_request,
    evaluate_request,
    evaluator_state,
    observation,
    translated,
    update_request,
)
from adapters.outbound.logs.evaluation_result_group import (
    EVALUATION_GATE_READBACK_FAILED,
    CloudWatchResultGroupReadback,
    grants_group_write,
    iam_pattern_matches,
)
from adapters.outbound.ssm.evaluation_release_store import (
    EVALUATION_RELEASE_UNWRITABLE,
    SsmEvaluationReleaseStore,
)
from domain.evaluation.gate import (
    ExecutionStatus,
    GateAssertion,
    GateFinding,
    OnlineEvaluationStatus,
    calibration_inputs,
    parse_gate_spec,
    release_enabled,
    release_not_enabled,
)
from domain.evaluation.release import EvaluationFamilyV1
from domain.shared_kernel.errors import DependencyError
from ports.outbound.evaluation_gate import (
    CalibrationEvaluateRequest,
    CreateOnlineEvaluationRequest,
    OnlineEvaluationNotFoundError,
    UpdateOnlineEvaluationRequest,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-079")]

TOKEN = "t" * 64
TIMESTAMP_NS = 1_800_000_000_000_000_000


def _spec(**overrides: object) -> Any:
    return parse_gate_spec(json.dumps(spec_document(**overrides)))


def _client_error(code: str, operation: str, message: str = "") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message or f"{code} happened"}}, operation
    )


_BINDING: dict[str, object] = {
    "description": "Evaluates exact released fictional responses.",
    "rule": {
        "samplingConfig": {"samplingPercentage": 100.0},
        "sessionConfig": {"sessionTimeoutMinutes": 5},
    },
    "dataSourceConfig": {
        "cloudWatchLogs": {"logGroupNames": [INPUT_GROUP], "serviceNames": [SERVICE_NAME]}
    },
    "evaluators": [{"evaluatorId": HELPFULNESS_ID}, {"evaluatorId": GROUNDING_ID}],
    "outputConfig": {"cloudWatchConfig": {"logGroupName": RESULT_GROUP}},
    "evaluationExecutionRoleArn": EXECUTION_ROLE,
}


class TestControlShaping:
    def test_create_binds_the_owned_group_and_never_enables(self) -> None:
        request = CreateOnlineEvaluationRequest(spec=_spec(), client_token=TOKEN)
        assert create_request(request) == {
            "onlineEvaluationConfigName": "adlytics_test_released_v4",
            **_BINDING,
            "enableOnCreate": False,
            "clientToken": TOKEN,
        }

    def test_create_carries_the_spec_tags_and_an_update_never_does(self) -> None:
        tags = {"Application": "adlytics-ai", "Environment": "test", "ManagedBy": "cdk"}
        spec = _spec(tags=tags)
        create = create_request(CreateOnlineEvaluationRequest(spec=spec, client_token=TOKEN))
        assert create == {
            "onlineEvaluationConfigName": "adlytics_test_released_v4",
            **_BINDING,
            "enableOnCreate": False,
            "clientToken": TOKEN,
            "tags": tags,
        }
        update = update_request(
            UpdateOnlineEvaluationRequest(
                config_id=CONFIG_ID,
                execution_status=ExecutionStatus.ENABLED,
                client_token=TOKEN,
                spec=spec,
            )
        )
        assert update == {
            "onlineEvaluationConfigId": CONFIG_ID,
            **_BINDING,
            "executionStatus": "ENABLED",
            "clientToken": TOKEN,
        }

    def test_create_with_no_tags_sends_no_tags_member(self) -> None:
        create = create_request(CreateOnlineEvaluationRequest(spec=_spec(), client_token=TOKEN))
        assert "tags" not in create

    def test_update_with_a_spec_rebinds_every_member(self) -> None:
        request = UpdateOnlineEvaluationRequest(
            config_id=CONFIG_ID,
            execution_status=ExecutionStatus.ENABLED,
            client_token=TOKEN,
            spec=_spec(),
        )
        assert update_request(request) == {
            "onlineEvaluationConfigId": CONFIG_ID,
            **_BINDING,
            "executionStatus": "ENABLED",
            "clientToken": TOKEN,
        }

    def test_update_without_a_spec_changes_only_the_execution_state(self) -> None:
        request = UpdateOnlineEvaluationRequest(
            config_id=CONFIG_ID, execution_status=ExecutionStatus.DISABLED, client_token=TOKEN
        )
        assert update_request(request) == {
            "onlineEvaluationConfigId": CONFIG_ID,
            "executionStatus": "DISABLED",
            "clientToken": TOKEN,
        }

    def test_an_update_refuses_a_placeholder_id(self) -> None:
        with pytest.raises(ValueError, match="configuration id"):
            UpdateOnlineEvaluationRequest(
                config_id="evaluation-gate:failed:r",
                execution_status=ExecutionStatus.DISABLED,
                client_token=TOKEN,
            )

    def test_get_projects_every_asserted_member(self) -> None:
        response = {
            "onlineEvaluationConfigId": CONFIG_ID,
            "onlineEvaluationConfigName": "adlytics_test_released_v4",
            "status": "ACTIVE",
            "executionStatus": "DISABLED",
            **_BINDING,
            "rule": {
                **cast("dict[str, object]", _BINDING["rule"]),
                "filters": [{"key": "k", "operator": "Equals", "value": {"stringValue": "v"}}],
            },
            "outputConfig": {
                "cloudWatchConfig": {
                    "logGroupName": RESULT_GROUP,
                    "resultDestination": "DEDICATED_LOG_GROUP",
                }
            },
        }
        state = config_state(cast("Any", response))

        assert state.status is OnlineEvaluationStatus.ACTIVE
        assert state.execution_status is ExecutionStatus.DISABLED
        assert state.output_log_group_name == RESULT_GROUP
        assert state.result_destination == "DEDICATED_LOG_GROUP"
        assert state.input_log_group_names == (INPUT_GROUP,)
        assert state.input_service_names == (SERVICE_NAME,)
        assert state.evaluator_ids == (HELPFULNESS_ID, GROUNDING_ID)
        assert state.execution_role_arn == EXECUTION_ROLE
        assert (state.sampling_percentage, state.session_timeout_minutes) == (100.0, 5)
        assert state.filter_count == 1
        assert state.description == "Evaluates exact released fictional responses."

    def test_an_unbound_output_reads_as_empty(self) -> None:
        response = {
            "onlineEvaluationConfigId": CONFIG_ID,
            "onlineEvaluationConfigName": "adlytics_test_released_v4",
            "status": "CREATING",
            "executionStatus": "DISABLED",
        }
        state = config_state(cast("Any", response))
        assert (state.output_log_group_name, state.evaluator_ids) == ("", ())

    def test_an_evaluator_prompt_is_reduced_to_its_digest(self) -> None:
        instructions = "Judge the fictional answer.\n"
        state = evaluator_state(
            cast(
                "Any",
                {
                    "evaluatorId": HELPFULNESS_ID,
                    "evaluatorName": HELPFULNESS,
                    "status": "ACTIVE",
                    "evaluatorConfig": {
                        "llmAsAJudge": {
                            "instructions": instructions,
                            "ratingScale": {},
                            "modelConfig": {
                                "bedrockEvaluatorModelConfig": {"modelId": "fictional.m:0"}
                            },
                        }
                    },
                },
            )
        )
        assert state.prompt_digest == hashlib.sha256(instructions.encode()).hexdigest()
        assert state.model_id == "fictional.m:0"
        assert instructions not in repr(state)

    def test_a_non_judge_evaluator_has_no_model_or_digest(self) -> None:
        state = evaluator_state(
            cast(
                "Any",
                {
                    "evaluatorId": "Builtin.Helpfulness",
                    "evaluatorName": "Builtin.Helpfulness",
                    "status": "ACTIVE",
                    "evaluatorConfig": {},
                },
            )
        )
        assert (state.model_id, state.prompt_digest) == ("", "")


def _evaluate(index: int = 0) -> CalibrationEvaluateRequest:
    return CalibrationEvaluateRequest(
        calibration=calibration_inputs(_spec())[index], service_name=SERVICE_NAME
    )


class TestEvaluateShaping:
    def test_the_session_span_is_exactly_the_released_span_serializer_output(self) -> None:
        request = _evaluate()
        calibration = request.calibration
        expected = json.loads(
            released_span_json(
                ReleasedSpanProjection(
                    envelope=calibration.envelope,
                    service_name=SERVICE_NAME,
                    timestamp_ns=TIMESTAMP_NS,
                    trace_id=calibration.trace_id,
                    span_id=calibration.span_id,
                )
            )
        )
        assert evaluate_request(request, TIMESTAMP_NS) == {
            "evaluatorId": HELPFULNESS_ID,
            "evaluationInput": {"sessionSpans": [expected]},
        }

    def test_only_the_requested_evaluators_labels_are_kept(self) -> None:
        response = {
            "evaluationResults": [
                {"evaluatorId": HELPFULNESS_ID, "label": "Excellent", "explanation": "secret"},
                {"evaluatorId": GROUNDING_ID, "label": "Unsupported"},
            ]
        }
        result = observation(_evaluate(), cast("Any", response))
        assert (result.case_name, result.evaluator_name) == ("known_correct", HELPFULNESS)
        assert (result.labels, result.error_code) == (("Excellent",), "")
        assert "secret" not in repr(result)

    def test_a_result_error_keeps_its_code_and_drops_labels(self) -> None:
        response = {
            "evaluationResults": [
                {"evaluatorId": HELPFULNESS_ID, "label": "Excellent"},
                {"evaluatorId": HELPFULNESS_ID, "errorCode": "ModelTimeout", "errorMessage": "x"},
            ]
        }
        result = observation(_evaluate(), cast("Any", response))
        assert (result.labels, result.error_code) == ((), "ModelTimeout")

    def test_an_error_message_without_a_code_is_still_an_error(self) -> None:
        response = {"evaluationResults": [{"evaluatorId": HELPFULNESS_ID, "errorMessage": "x"}]}
        assert observation(_evaluate(), cast("Any", response)).error_code == "error"

    def test_no_result_for_the_evaluator_is_no_label(self) -> None:
        result = observation(_evaluate(), cast("Any", {"evaluationResults": []}))
        assert (result.labels, result.error_code) == ((), "")


class TestControlErrors:
    def test_not_found_on_the_configuration_is_control_flow(self) -> None:
        error = translated(
            _client_error("ResourceNotFoundException", "GetOnlineEvaluationConfig"),
            "GetOnlineEvaluationConfig",
        )
        assert isinstance(error, OnlineEvaluationNotFoundError)

    def test_not_found_on_an_evaluator_is_a_refusal(self) -> None:
        error = translated(
            _client_error("ResourceNotFoundException", "GetEvaluator"), "GetEvaluator"
        )
        assert isinstance(error, DependencyError)
        assert error.code == EVALUATION_GATE_CONTROL_FAILED
        assert error.context["service_code"] == "ResourceNotFoundException"

    def test_an_evaluate_refusal_keeps_its_code_but_never_its_message(self) -> None:
        error = translated(
            _client_error("ValidationException", "Evaluate", "span text: Twelve widgets"),
            "Evaluate",
        )
        assert isinstance(error, DependencyError)
        assert error.context["service_code"] == "ValidationException"
        assert "provider_message" not in error.context
        assert "widgets" not in json.dumps(error.context, default=str)

    def test_a_control_refusal_carries_its_redacted_message(self) -> None:
        error = translated(
            _client_error("ConflictException", "CreateOnlineEvaluationConfig", "name exists"),
            "CreateOnlineEvaluationConfig",
        )
        assert isinstance(error, DependencyError)
        assert error.context["provider_message"] == "name exists"


@dataclass
class _RecordingAgentCore:
    """Answers each operation with one scripted response or error, recording the request."""

    responses: dict[str, Any] = field(default_factory=dict[str, Any])
    errors: dict[str, Exception] = field(default_factory=dict[str, Exception])
    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )

    def _answer(self, operation: str, request: dict[str, Any]) -> Any:
        self.calls.append((operation, request))
        if operation in self.errors:
            raise self.errors[operation]
        return self.responses.get(operation, {})

    def create_online_evaluation_config(self, **request: Any) -> Any:
        return self._answer("create", request)

    def get_evaluator(self, **request: Any) -> Any:
        return self._answer("get_evaluator", request)

    def delete_online_evaluation_config(self, **request: Any) -> Any:
        return self._answer("delete", request)

    def evaluate(self, **request: Any) -> Any:
        return self._answer("evaluate", request)

    def list_online_evaluation_configs(self, **request: Any) -> Any:
        self.calls.append(("list", request))
        if "list" in self.errors:
            raise self.errors["list"]
        pages = cast("list[dict[str, Any]]", self.responses.get("list", [{}]))
        page = int(request.get("nextToken", "0"))
        response = {"onlineEvaluationConfigs": pages[page].get("items", [])}
        if page + 1 < len(pages):
            response["nextToken"] = str(page + 1)
        return response


def _summary(name: str, config_id: str, **overrides: object) -> dict[str, object]:
    return {
        "onlineEvaluationConfigId": config_id,
        "onlineEvaluationConfigName": name,
        "status": "ACTIVE",
        "executionStatus": "ENABLED",
        **overrides,
    }


class TestControlAdapter:
    def _control(self, client: _RecordingAgentCore) -> AgentCoreOnlineEvaluationControl:
        return AgentCoreOnlineEvaluationControl(
            cast("Any", client), cast("Any", client), clock_ns=lambda: TIMESTAMP_NS
        )

    def test_create_returns_the_new_configuration_id(self) -> None:
        client = _RecordingAgentCore(responses={"create": {"onlineEvaluationConfigId": CONFIG_ID}})
        request = CreateOnlineEvaluationRequest(spec=_spec(), client_token=TOKEN)
        assert self._control(client).create(request) == CONFIG_ID
        assert client.calls == [("create", dict(create_request(request)))]

    def test_the_evaluator_is_read_with_its_instructions(self) -> None:
        client = _RecordingAgentCore(
            responses={
                "get_evaluator": {
                    "evaluatorId": GROUNDING_ID,
                    "evaluatorName": GROUNDING,
                    "status": "ACTIVE",
                    "evaluatorConfig": {},
                }
            }
        )
        self._control(client).evaluator(GROUNDING_ID)
        assert client.calls == [
            ("get_evaluator", {"evaluatorId": GROUNDING_ID, "includedData": "ALL_DATA"})
        ]

    def test_evaluate_stamps_the_span_with_the_clock(self) -> None:
        client = _RecordingAgentCore(responses={"evaluate": {"evaluationResults": []}})
        self._control(client).evaluate(_evaluate())
        ((_, request),) = client.calls
        assert request == evaluate_request(_evaluate(), TIMESTAMP_NS)

    def test_find_by_name_pages_until_the_named_configuration(self) -> None:
        other = _summary("adlytics_test_released_v5", "adlytics_test_released_v5-ZzZzZ12345")
        pages = [{"items": [other]}, {"items": [_summary("adlytics_test_released_v4", CONFIG_ID)]}]
        client = _RecordingAgentCore(responses={"list": pages})
        found = self._control(client).find_by_name("adlytics_test_released_v4")

        assert found is not None
        assert (found.config_id, found.status, found.execution_status) == (
            CONFIG_ID,
            OnlineEvaluationStatus.ACTIVE,
            ExecutionStatus.ENABLED,
        )
        assert client.calls == [
            ("list", {"maxResults": 100}),
            ("list", {"maxResults": 100, "nextToken": "1"}),
        ]

    def test_find_by_name_is_none_when_no_configuration_has_the_name(self) -> None:
        pages = [{"items": [_summary("adlytics_test_released_v3b", CONFIG_ID)]}, {}]
        client = _RecordingAgentCore(responses={"list": pages})
        assert self._control(client).find_by_name("adlytics_test_released_v4") is None
        assert len(client.calls) == 2

    def test_a_refused_listing_is_a_dependency_error_not_absence(self) -> None:
        client = _RecordingAgentCore(
            errors={"list": _client_error("AccessDeniedException", "ListOnlineEvaluationConfigs")}
        )
        with pytest.raises(DependencyError, match="ListOnlineEvaluationConfigs failed"):
            self._control(client).find_by_name("adlytics_test_released_v4")

    def test_a_listing_that_never_ends_fails_closed(self) -> None:
        client = _RecordingAgentCore(responses={"list": [{}] * 60})
        with pytest.raises(DependencyError, match="did not finish within 50 pages"):
            self._control(client).find_by_name("adlytics_test_released_v4")

    def test_a_delete_of_an_absent_configuration_is_not_found(self) -> None:
        client = _RecordingAgentCore(
            errors={
                "delete": _client_error("ResourceNotFoundException", "DeleteOnlineEvaluationConfig")
            }
        )
        with pytest.raises(OnlineEvaluationNotFoundError):
            self._control(client).delete(CONFIG_ID)


def _statement(**members: object) -> str:
    return json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", **members}]})


class TestResourcePolicyMatching:
    @pytest.mark.parametrize(
        ("pattern", "value", "matches"),
        [
            ("*", RESULT_GROUP_ARN, True),
            ("arn:aws:logs:*:*:log-group:/adlytics/*", RESULT_GROUP_ARN, True),
            (f"{RESULT_GROUP_ARN}:*", f"{RESULT_GROUP_ARN}:*", True),
            (
                "arn:aws:logs:us-east-1:123456789012:log-group:/adlytics/test/evaluation/results-v?",
                RESULT_GROUP_ARN,
                True,
            ),
            ("arn:aws:logs:us-east-1:123456789012:log-group:aws/spans:*", RESULT_GROUP_ARN, False),
            (
                "arn:aws:logs:us-east-1:123456789012:log-group:/adlytics/test/evaluation/results-v5",
                RESULT_GROUP_ARN,
                False,
            ),
            (
                "arn:aws:logs:us-east-1:123456789012:log-group:${aws:username}",
                RESULT_GROUP_ARN,
                True,
            ),
            (
                "arn:aws:logs:us-east-1:123456789012:log-group:/adlytics/test/evaluation/results-v4.x",
                RESULT_GROUP_ARN,
                False,
            ),
        ],
    )
    def test_iam_wildcards_match_like_iam(self, pattern: str, value: str, matches: bool) -> None:
        assert iam_pattern_matches(pattern, value) is matches

    @pytest.mark.parametrize(
        "document",
        [
            _statement(
                Principal={"Service": "delivery.logs.amazonaws.com"},
                Action="logs:PutLogEvents",
                Resource=f"{RESULT_GROUP_ARN}:*",
            ),
            _statement(Action=["logs:CreateLogStream"], Resource=[RESULT_GROUP_ARN]),
            _statement(Action="logs:*", Resource="arn:aws:logs:*:*:log-group:/adlytics/*"),
            _statement(Action="*", Resource="*"),
            _statement(Action="LOGS:PUT*", Resource=f"{RESULT_GROUP_ARN}:log-stream:*"),
            _statement(NotAction="logs:DeleteLogGroup", Resource=f"{RESULT_GROUP_ARN}:*"),
            _statement(Action="logs:PutLogEvents", NotResource="arn:aws:logs:*:*:log-group:x"),
            _statement(Action="logs:PutLogEvents"),
            json.dumps({"Statement": {"Effect": "Allow", "Action": "logs:*", "Resource": "*"}}),
            "not json",
            json.dumps({"Statement": "Allow everything"}),
        ],
        ids=[
            "service-principal-stream-wildcard",
            "create-stream-exact",
            "logs-star-prefix",
            "star-star",
            "action-case-and-stream",
            "not-action",
            "not-resource",
            "no-resource",
            "single-statement-object",
            "unparseable",
            "malformed-statement",
        ],
    )
    def test_a_write_grant_reaching_the_group_is_found(self, document: str) -> None:
        assert grants_group_write(document, RESULT_GROUP_ARN)

    @pytest.mark.parametrize(
        "document",
        [
            _statement(
                Principal={"Service": "xray.amazonaws.com"},
                Action="logs:PutLogEvents",
                Resource="arn:aws:logs:us-east-1:123456789012:log-group:aws/spans:*",
            ),
            _statement(Action="logs:GetLogEvents", Resource=f"{RESULT_GROUP_ARN}:*"),
            _statement(Action="events:PutEvents", Resource="*"),
            json.dumps({"Statement": [{"Effect": "Deny", "Action": "logs:*", "Resource": "*"}]}),
        ],
        ids=["transaction-search-spans", "read-only", "other-service", "deny"],
    )
    def test_a_policy_that_cannot_write_to_the_group_is_not_a_grant(self, document: str) -> None:
        assert not grants_group_write(document, RESULT_GROUP_ARN)

    def test_a_star_suffixed_group_arn_is_matched_in_its_bare_form(self) -> None:
        document = _statement(Action="logs:PutLogEvents", Resource=RESULT_GROUP_ARN)
        assert grants_group_write(document, f"{RESULT_GROUP_ARN}:*")


def _group(**overrides: object) -> dict[str, Any]:
    return {
        "logGroupName": RESULT_GROUP,
        "arn": f"{RESULT_GROUP_ARN}:*",
        "logGroupArn": RESULT_GROUP_ARN,
        "retentionInDays": 14,
        "deletionProtectionEnabled": True,
        **overrides,
    }


_INGRESS_FILTER = {"filterName": FILTER_NAME, "destinationArn": INGRESS_ARN}
_SPANS_POLICY = {
    "policyName": "TransactionSearchAccess",
    "policyDocument": _statement(
        Principal={"Service": "xray.amazonaws.com"},
        Action="logs:PutLogEvents",
        Resource="arn:aws:logs:us-east-1:123456789012:log-group:aws/spans:*",
    ),
}


@dataclass
class _RecordingLogs:
    """Scripted pages per (operation, discriminator); records every request."""

    groups: list[dict[str, Any]] = field(default_factory=lambda: [_group()])
    data_protection: str = ""
    transformer: list[dict[str, Any]] | None = None
    """None has no transformer (the service's not-found); a list is its transformerConfig."""
    filters: list[list[dict[str, Any]]] = field(default_factory=lambda: [[_INGRESS_FILTER]])
    account_policies: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict[str, list[dict[str, Any]]]
    )
    destinations: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    exports: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict[str, list[dict[str, Any]]]
    )
    account_resource_policies: list[dict[str, Any]] = field(default_factory=lambda: [_SPANS_POLICY])
    group_resource_policies: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    errors: dict[str, Exception] = field(default_factory=dict[str, Exception])
    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )

    def _call(self, operation: str, request: dict[str, Any]) -> None:
        self.calls.append((operation, request))
        if operation in self.errors:
            raise self.errors[operation]

    def describe_log_groups(self, **request: Any) -> Any:
        self._call("DescribeLogGroups", request)
        return {"logGroups": self.groups}

    def get_data_protection_policy(self, **request: Any) -> Any:
        self._call("GetDataProtectionPolicy", request)
        return {"logGroupIdentifier": RESULT_GROUP, "policyDocument": self.data_protection}

    def get_transformer(self, **request: Any) -> Any:
        self._call("GetTransformer", request)
        if self.transformer is None:
            raise _client_error("ResourceNotFoundException", "GetTransformer")
        return {"logGroupIdentifier": RESULT_GROUP, "transformerConfig": self.transformer}

    def describe_subscription_filters(self, **request: Any) -> Any:
        self._call("DescribeSubscriptionFilters", request)
        page = 0 if "nextToken" not in request else int(request["nextToken"])
        response: dict[str, Any] = {"subscriptionFilters": self.filters[page]}
        if page + 1 < len(self.filters):
            response["nextToken"] = str(page + 1)
        return response

    def describe_account_policies(self, **request: Any) -> Any:
        self._call("DescribeAccountPolicies", request)
        return {"accountPolicies": self.account_policies.get(request["policyType"], [])}

    def describe_delivery_destinations(self, **request: Any) -> Any:
        self._call("DescribeDeliveryDestinations", request)
        return {"deliveryDestinations": self.destinations}

    def describe_export_tasks(self, **request: Any) -> Any:
        self._call("DescribeExportTasks", request)
        return {"exportTasks": self.exports.get(request["statusCode"], [])}

    def describe_resource_policies(self, **request: Any) -> Any:
        self._call("DescribeResourcePolicies", request)
        if request.get("policyScope") == "RESOURCE":
            return {"resourcePolicies": self.group_resource_policies}
        return {"resourcePolicies": self.account_resource_policies}


def _findings(client: _RecordingLogs) -> tuple[GateFinding, ...]:
    return CloudWatchResultGroupReadback(cast("Any", client)).findings(_spec().custody())


class TestResultGroupReadback:
    def test_every_control_holding_yields_no_finding(self) -> None:
        client = _RecordingLogs()
        assert _findings(client) == ()
        assert client.calls == [
            ("DescribeLogGroups", {"logGroupIdentifiers": [RESULT_GROUP]}),
            ("DescribeSubscriptionFilters", {"logGroupName": RESULT_GROUP}),
            ("DescribeAccountPolicies", {"policyType": "SUBSCRIPTION_FILTER_POLICY"}),
            ("DescribeAccountPolicies", {"policyType": "DATA_PROTECTION_POLICY"}),
            ("DescribeAccountPolicies", {"policyType": "TRANSFORMER_POLICY"}),
            ("DescribeDeliveryDestinations", {}),
            ("DescribeExportTasks", {"statusCode": "PENDING"}),
            ("DescribeExportTasks", {"statusCode": "RUNNING"}),
            ("DescribeResourcePolicies", {"policyScope": "ACCOUNT"}),
            (
                "DescribeResourcePolicies",
                {"resourceArn": RESULT_GROUP_ARN, "policyScope": "RESOURCE"},
            ),
            ("GetDataProtectionPolicy", {"logGroupIdentifier": RESULT_GROUP}),
            ("GetTransformer", {"logGroupIdentifier": RESULT_GROUP}),
        ]

    def test_an_absent_group_is_the_only_finding(self) -> None:
        client = _RecordingLogs(groups=[{**_group(), "logGroupName": f"{RESULT_GROUP}-other"}])
        assert _findings(client) == (GateFinding(GateAssertion.GROUP_EXISTS),)
        assert [operation for operation, _ in client.calls] == ["DescribeLogGroups"]

    @pytest.mark.parametrize(
        ("client", "assertion"),
        [
            (_RecordingLogs(groups=[_group(retentionInDays=30)]), GateAssertion.GROUP_RETENTION),
            (_RecordingLogs(groups=[_group(retentionInDays=None)]), GateAssertion.GROUP_RETENTION),
            (
                _RecordingLogs(groups=[_group(deletionProtectionEnabled=False)]),
                GateAssertion.GROUP_DELETION_PROTECTION,
            ),
            (
                _RecordingLogs(groups=[_group(kmsKeyId="arn:aws:kms:us-east-1:1:key/k")]),
                GateAssertion.GROUP_KEY,
            ),
            (
                _RecordingLogs(groups=[_group(logGroupArn="arn:aws:logs:us-east-1:9:log-group:x")]),
                GateAssertion.GROUP_ARN,
            ),
            (
                _RecordingLogs(groups=[_group(dataProtectionStatus="ACTIVATED")]),
                GateAssertion.GROUP_DATA_PROTECTION,
            ),
            (_RecordingLogs(data_protection='{"Name": "p"}'), GateAssertion.GROUP_DATA_PROTECTION),
            (_RecordingLogs(filters=[[]]), GateAssertion.SUBSCRIPTION_FILTERS),
            (
                _RecordingLogs(filters=[[_INGRESS_FILTER], [{"filterName": "copy"}]]),
                GateAssertion.SUBSCRIPTION_FILTERS,
            ),
            (
                _RecordingLogs(filters=[[{**_INGRESS_FILTER, "destinationArn": "arn:x"}]]),
                GateAssertion.SUBSCRIPTION_FILTERS,
            ),
            (
                _RecordingLogs(
                    account_policies={"SUBSCRIPTION_FILTER_POLICY": [{"policyName": "a"}]}
                ),
                GateAssertion.ACCOUNT_SUBSCRIPTION_POLICY,
            ),
            (
                _RecordingLogs(account_policies={"DATA_PROTECTION_POLICY": [{"policyName": "d"}]}),
                GateAssertion.ACCOUNT_DATA_PROTECTION_POLICY,
            ),
            (
                _RecordingLogs(transformer=[{"parseJSON": {}}]),
                GateAssertion.GROUP_TRANSFORMER,
            ),
            (
                _RecordingLogs(account_policies={"TRANSFORMER_POLICY": [{"policyName": "t"}]}),
                GateAssertion.ACCOUNT_TRANSFORMER_POLICY,
            ),
            (
                _RecordingLogs(
                    destinations=[
                        {
                            "deliveryDestinationConfiguration": {
                                "destinationResourceArn": f"{RESULT_GROUP_ARN}:*"
                            }
                        }
                    ]
                ),
                GateAssertion.DELIVERY_DESTINATION,
            ),
            (
                _RecordingLogs(exports={"RUNNING": [{"logGroupName": RESULT_GROUP}]}),
                GateAssertion.EXPORT_TASK,
            ),
            (
                _RecordingLogs(
                    group_resource_policies=[
                        {"policyDocument": _statement(Action="logs:PutLogEvents")}
                    ]
                ),
                GateAssertion.RESOURCE_POLICY_WRITE,
            ),
            (
                _RecordingLogs(
                    account_resource_policies=[
                        _SPANS_POLICY,
                        {"policyDocument": _statement(Action="logs:*", Resource="*")},
                    ]
                ),
                GateAssertion.RESOURCE_POLICY_WRITE,
            ),
        ],
        ids=[
            "retention",
            "no-retention",
            "deletion-protection",
            "key",
            "arn",
            "data-protection-status",
            "data-protection-policy",
            "no-ingress-filter",
            "second-filter-on-page-two",
            "filter-elsewhere",
            "account-subscription-policy",
            "account-data-protection-policy",
            "group-transformer",
            "account-transformer-policy",
            "delivery-destination",
            "running-export",
            "group-resource-policy",
            "account-resource-policy",
        ],
    )
    def test_each_broken_control_names_exactly_its_assertion(
        self, client: _RecordingLogs, assertion: GateAssertion
    ) -> None:
        assert _findings(client) == (GateFinding(assertion),)

    def test_other_groups_exports_and_destinations_do_not_count(self) -> None:
        client = _RecordingLogs(
            exports={"PENDING": [{"logGroupName": INPUT_GROUP}]},
            destinations=[
                {"deliveryDestinationConfiguration": {"destinationResourceArn": "arn:aws:s3:::b"}}
            ],
        )
        assert _findings(client) == ()

    def test_a_refused_readback_raises_rather_than_passing(self) -> None:
        client = _RecordingLogs(
            errors={
                "DescribeResourcePolicies": _client_error(
                    "AccessDeniedException", "DescribeResourcePolicies"
                )
            }
        )
        with pytest.raises(DependencyError) as raised:
            _findings(client)
        assert raised.value.code == EVALUATION_GATE_READBACK_FAILED
        assert raised.value.description == "DescribeResourcePolicies failed."

    def test_a_missing_group_from_describe_is_absent(self) -> None:
        client = _RecordingLogs(
            errors={"DescribeLogGroups": _client_error("ResourceNotFoundException", "x")}
        )
        assert _findings(client) == (GateFinding(GateAssertion.GROUP_EXISTS),)

    def test_no_data_protection_policy_as_not_found_is_none(self) -> None:
        client = _RecordingLogs(
            errors={"GetDataProtectionPolicy": _client_error("ResourceNotFoundException", "x")}
        )
        assert _findings(client) == ()

    def test_an_empty_transformer_is_none(self) -> None:
        assert _findings(_RecordingLogs(transformer=[])) == ()

    def test_a_refused_transformer_read_raises_rather_than_passing(self) -> None:
        client = _RecordingLogs(
            errors={"GetTransformer": _client_error("AccessDeniedException", "GetTransformer")}
        )
        with pytest.raises(DependencyError, match="GetTransformer failed"):
            _findings(client)

    def test_a_readback_that_never_ends_fails_closed(self) -> None:
        client = _RecordingLogs(filters=[[_INGRESS_FILTER]] * 60)
        with pytest.raises(DependencyError, match="did not finish within 50 pages"):
            _findings(client)


@dataclass
class _RecordingSsm:
    parameters: dict[str, str] = field(default_factory=dict[str, str])
    errors: dict[str, Exception] = field(default_factory=dict[str, Exception])
    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )

    def _call(self, operation: str, request: dict[str, Any]) -> None:
        self.calls.append((operation, request))
        if operation in self.errors:
            raise self.errors[operation]

    def get_parameters(self, **request: Any) -> Any:
        self._call("GetParameters", request)
        names = request["Names"]
        return {
            "Parameters": [
                {"Name": name, "Value": self.parameters[name]}
                for name in names
                if name in self.parameters
            ],
            "InvalidParameters": [name for name in names if name not in self.parameters],
        }

    def put_parameter(self, **request: Any) -> Any:
        self._call("PutParameter", request)
        return {"Version": 1}

    def delete_parameter(self, **request: Any) -> Any:
        self._call("DeleteParameter", request)
        return {}


class TestReleaseStore:
    def test_a_write_is_one_canonical_string_parameter_overwrite(self) -> None:
        client = _RecordingSsm()
        release = release_enabled(_spec(), CONFIG_ID, "2026-09-27T12:00:00+00:00")
        SsmEvaluationReleaseStore(cast("Any", client)).write(RELEASE_PARAMETER, release)
        assert client.calls == [
            (
                "PutParameter",
                {
                    "Name": RELEASE_PARAMETER,
                    "Value": contract_to_json(release),
                    "Type": "String",
                    "Overwrite": True,
                },
            )
        ]

    def test_a_read_uses_the_dispatchers_decoder_and_missing_is_none(self) -> None:
        release = release_not_enabled(EvaluationFamilyV1.V4, "2026-09-27T12:00:00+00:00")
        client = _RecordingSsm(parameters={RELEASE_PARAMETER: contract_to_json(release)})
        store = SsmEvaluationReleaseStore(cast("Any", client))
        assert store.read(RELEASE_PARAMETER) == release
        assert (
            SsmEvaluationReleaseStore(cast("Any", _RecordingSsm())).read(RELEASE_PARAMETER) is None
        )

    def test_deleting_an_absent_record_succeeds(self) -> None:
        client = _RecordingSsm(
            errors={"DeleteParameter": _client_error("ParameterNotFound", "DeleteParameter")}
        )
        SsmEvaluationReleaseStore(cast("Any", client)).delete(RELEASE_PARAMETER)
        assert client.calls == [("DeleteParameter", {"Name": RELEASE_PARAMETER})]

    @pytest.mark.parametrize("operation", ["PutParameter", "DeleteParameter"])
    def test_a_refused_write_or_delete_is_a_typed_dependency_error(self, operation: str) -> None:
        client = _RecordingSsm(
            errors={operation: _client_error("AccessDeniedException", operation)}
        )
        store = SsmEvaluationReleaseStore(cast("Any", client))
        with pytest.raises(DependencyError) as raised:
            if operation == "PutParameter":
                store.write(
                    RELEASE_PARAMETER,
                    release_not_enabled(EvaluationFamilyV1.V4, "2026-09-27T12:00:00+00:00"),
                )
            else:
                store.delete(RELEASE_PARAMETER)
        assert raised.value.code == EVALUATION_RELEASE_UNWRITABLE
        assert raised.value.description == f"{operation} failed."
