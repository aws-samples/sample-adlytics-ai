# SPDX-License-Identifier: MIT-0
"""The evaluation gate's spec, readback assertions, release records, and calibration (ADR-079)."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from typing import Any, cast

import pytest
from evaluation_gate_support import (
    CONFIG_ID,
    EXECUTION_ROLE,
    GROUNDING,
    GROUNDING_ID,
    HELPFULNESS,
    HELPFULNESS_ID,
    INPUT_GROUP,
    RESULT_GROUP_ARN,
    SERVICE_NAME,
    case_document,
    evaluator_document,
    event,
    properties,
    spec_document,
)

from adapters.jobs.evaluation_writer.span import ReleasedSpanProjection, released_span_json
from domain.evaluation.gate import (
    CalibrationFailureKind,
    CalibrationFailureV1,
    CalibrationObservationV1,
    EvaluationGateSpecError,
    EvaluatorState,
    ExecutionStatus,
    GateAssertion,
    GateFinding,
    GateRequestType,
    OnlineEvaluationConfigState,
    OnlineEvaluationStatus,
    ResultGroupCustodyV1,
    binding_findings,
    calibration_inputs,
    calibration_outcome,
    evaluator_findings,
    is_config_id,
    parse_gate_event,
    parse_gate_spec,
    release_enabled,
    release_matches,
    release_not_enabled,
)
from domain.evaluation.release import EvaluationFamilyV1

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-079")]


def _spec(**overrides: object) -> Any:
    return parse_gate_spec(json.dumps(spec_document(**overrides)))


class TestSpecDecode:
    def test_the_whole_wire_spec_decodes_to_typed_members(self) -> None:
        spec = _spec()

        assert spec.family is EvaluationFamilyV1.V4
        assert spec.execution_requested is True
        assert spec.input_log_group_names == (INPUT_GROUP,)
        assert spec.input_service_names == (SERVICE_NAME,)
        assert spec.sampling_percentage == 100.0
        assert isinstance(spec.sampling_percentage, float)
        assert spec.session_timeout_minutes == 5
        assert spec.result_retention_days == 14
        assert spec.evaluator_ids == (HELPFULNESS_ID, GROUNDING_ID)
        assert [case.name for case in spec.calibration] == [
            "known_correct",
            "contradiction",
            "fluent_but_unsupported",
        ]

    def test_expected_labels_are_normalized_and_a_single_label_is_a_one_tuple(self) -> None:
        contradiction = _spec().calibration[1]

        assert dict(contradiction.expected) == {
            HELPFULNESS: ("poor", "needs_improvement"),
            GROUNDING: ("unsupported",),
        }

    def test_an_empty_grounded_context_is_accepted(self) -> None:
        assert _spec().calibration[2].grounded_context == ""

    def test_the_callback_carries_the_spec_and_ignores_the_digest(self) -> None:
        callback = parse_gate_event(event("Update"))

        assert callback.request_type is GateRequestType.UPDATE
        assert callback.physical_resource_id == CONFIG_ID
        assert callback.spec == _spec()

    def test_a_declared_service_timeout_is_accepted(self) -> None:
        callback = event("Create", ResourceProperties=properties(ServiceTimeout="1200"))
        assert parse_gate_event(callback).spec.config_name == "adlytics_test_released_v4"


def _refusal(document: object) -> str:
    with pytest.raises(EvaluationGateSpecError) as raised:
        parse_gate_spec(document if isinstance(document, str) else json.dumps(document))
    return str(raised.value)


class TestSpecValidation:
    def test_the_retired_v3_family_has_no_gate(self) -> None:
        """ADR-079: V3 was retired 2026-09-27, so no deployment can gate, enable, or record it."""
        assert _refusal(spec_document(family="v3")) == (
            "GateSpec.family v3 is retired; only a current family has a gate."
        )

    def test_the_owned_group_may_not_live_under_the_service_prefix(self) -> None:
        reserved = "/aws/bedrock-agentcore/evaluations/results/adlytics"
        message = _refusal(
            spec_document(
                result_log_group_name=reserved,
                result_log_group_arn=f"arn:aws:logs:us-east-1:123456789012:log-group:{reserved}",
            )
        )
        assert message == (
            "GateSpec.result_log_group_name must be outside /aws/bedrock-agentcore/evaluations/."
        )

    def test_an_expectation_must_name_a_declared_evaluator(self) -> None:
        cases = [
            *cast("list[object]", spec_document()["calibration"]),
            case_document("stray", {"undeclared_judge": "Excellent"}),
        ]
        assert _refusal(spec_document(calibration=cases)) == (
            "GateSpec calibration expects evaluators the spec does not declare: "
            "['undeclared_judge']."
        )

    def test_each_evaluator_needs_three_cases(self) -> None:
        cases = cast("list[object]", spec_document()["calibration"])[:2]
        assert _refusal(spec_document(calibration=cases)) == (
            "GateSpec calibration needs at least 3 cases for each evaluator; short: "
            f"['{GROUNDING}', '{HELPFULNESS}']."
        )

    def test_each_evaluator_needs_every_required_kind_not_just_three_cases(self) -> None:
        known, contradiction, unsupported = cast(
            "list[dict[str, object]]", spec_document()["calibration"]
        )
        cases = [
            known,
            {**contradiction, "kind": "off_topic"},
            unsupported,
            case_document("second_known_correct", {HELPFULNESS: "Good", GROUNDING: "Grounded"}),
        ]
        assert _refusal(spec_document(calibration=cases)) == (
            "GateSpec calibration needs a known_correct, a contradiction, and a "
            "fluent_unsupported case for each evaluator; missing: "
            f"['{GROUNDING}:contradiction', '{HELPFULNESS}:contradiction']."
        )

    def test_a_kind_counts_only_for_the_evaluators_its_case_names(self) -> None:
        known, contradiction, unsupported = cast(
            "list[dict[str, object]]", spec_document()["calibration"]
        )
        cases = [
            known,
            {**contradiction, "expected": {GROUNDING: "Unsupported"}},
            unsupported,
            case_document("clarifies", {HELPFULNESS: "Good"}, "clarification"),
        ]
        assert _refusal(spec_document(calibration=cases)).endswith(
            f"missing: ['{HELPFULNESS}:contradiction']."
        )

    @pytest.mark.parametrize(
        ("kind", "message"),
        [
            ("fluent_but_unsupported", "must be one of"),
            ("", "must be non-empty text"),
            (None, "is missing members: ['kind']"),
        ],
        ids=["unknown", "empty", "absent"],
    )
    def test_a_case_kind_is_closed_and_required(self, kind: str | None, message: str) -> None:
        first, *rest = cast("list[dict[str, object]]", spec_document()["calibration"])
        changed = {
            key: value for key, value in {**first, "kind": kind}.items() if value is not None
        }
        assert message in _refusal(spec_document(calibration=[changed, *rest]))

    def test_an_evaluator_without_any_case_is_refused(self) -> None:
        evaluators = [
            *cast("list[object]", spec_document()["evaluators"]),
            evaluator_document("adlytics_test_uncalibrated_v4", "adlytics_test_x-ZzZzZ12345"),
        ]
        assert _refusal(spec_document(evaluators=evaluators)).endswith(
            "short: ['adlytics_test_uncalibrated_v4']."
        )

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"sampling_percentage": 0}, "GateSpec.sampling_percentage must be in (0, 100]."),
            ({"sampling_percentage": 100.5}, "GateSpec.sampling_percentage must be in (0, 100]."),
            ({"sampling_percentage": True}, "GateSpec.sampling_percentage must be a number."),
            ({"session_timeout_minutes": 0}, "GateSpec.session_timeout_minutes must be 1-1440."),
            (
                {"session_timeout_minutes": "5"},
                "GateSpec.session_timeout_minutes must be an integer.",
            ),
            ({"execution_requested": "true"}, "GateSpec.execution_requested must be a boolean."),
            (
                {"result_retention_days": 15},
                "GateSpec.result_retention_days must be a CloudWatch Logs retention value.",
            ),
            (
                {"result_log_group_arn": f"{RESULT_GROUP_ARN}:*"},
                "GateSpec.result_log_group_arn must be the result group's ARN without ':*'.",
            ),
            (
                {
                    "result_log_group_name": INPUT_GROUP,
                    "result_log_group_arn": (
                        f"arn:aws:logs:us-east-1:123456789012:log-group:{INPUT_GROUP}"
                    ),
                },
                "GateSpec.result_log_group_name must not be an input log group.",
            ),
            (
                {"release_parameter_name": "/adlytics/test/evaluation/release-v5"},
                "GateSpec.release_parameter_name must be a path ending in /release-v4.",
            ),
            (
                {"input_service_names": [SERVICE_NAME, "second"]},
                "GateSpec.input_service_names must name exactly one service.",
            ),
            (
                {"config_name": "adlytics-test-released"},
                "GateSpec.config_name must match [a-zA-Z][a-zA-Z0-9_]{0,47}.",
            ),
            (
                {"execution_role_arn": "arn:aws:iam::123456789012:user/operator"},
                "GateSpec.execution_role_arn must be an IAM role ARN.",
            ),
            (
                {"ingress_function_arn": f"{EXECUTION_ROLE}"},
                "GateSpec.ingress_function_arn must be an unqualified Lambda function ARN.",
            ),
            ({"family": "v9"}, "GateSpec.family is not a known family."),
            (
                {"schema_version": "EvaluationGateSpec.v0"},
                "GateSpec.schema_version must be EvaluationGateSpec.v1.",
            ),
            ({"extra": 1}, "GateSpec carries unsupported members: ['extra']."),
            ({"description": None}, "GateSpec is missing members: ['description']."),
        ],
    )
    def test_an_invalid_member_is_refused_by_name(
        self, overrides: dict[str, object], message: str
    ) -> None:
        assert _refusal(spec_document(**overrides)) == message

    def test_duplicate_evaluator_names_are_refused(self) -> None:
        evaluators = [
            evaluator_document(HELPFULNESS, HELPFULNESS_ID),
            evaluator_document(HELPFULNESS, GROUNDING_ID),
        ]
        assert _refusal(spec_document(evaluators=evaluators)) == (
            "GateSpec evaluator names must be unique."
        )

    def test_a_malformed_digest_is_refused_through_the_release_contract(self) -> None:
        evaluators = [
            evaluator_document(HELPFULNESS, HELPFULNESS_ID, prompt_digest="A" * 64),
            evaluator_document(GROUNDING, GROUNDING_ID),
        ]
        assert _refusal(spec_document(evaluators=evaluators)) == (
            "GateSpec.evaluators[0]: released evaluator prompt digest must be 64 lowercase hex."
        )

    def test_invalid_json_does_not_quote_the_document(self) -> None:
        message = _refusal('{"calibration": "secret fictional answer')
        assert message == "GateSpec is not valid JSON."

    def test_an_empty_expectation_list_is_refused(self) -> None:
        cases = [
            case_document(f"case_{index}", {HELPFULNESS: [], GROUNDING: "Grounded"})
            for index in range(3)
        ]
        assert _refusal(spec_document(calibration=cases)) == (
            f"GateSpec.calibration[0].expected.{HELPFULNESS} must name at least one label."
        )

    def test_unsupported_resource_properties_are_refused(self) -> None:
        with pytest.raises(EvaluationGateSpecError, match=r"unsupported properties: \['Extra'\]"):
            parse_gate_event(event("Create", ResourceProperties={**properties(), "Extra": "x"}))


_STANDARD_TAGS = {"Application": "adlytics-ai", "Environment": "test", "ManagedBy": "cdk"}


class TestSpecTags:
    """H-IAM-AGENTCORE-CREATE-001: the optional tag set a create carries."""

    def test_a_spec_without_tags_parses_to_no_tags_as_a_rollback_delivers_it(self) -> None:
        assert dict(_spec().tags) == {}

    def test_the_standard_tag_set_parses_exactly(self) -> None:
        assert dict(_spec(tags=_STANDARD_TAGS).tags) == _STANDARD_TAGS

    def test_an_unknown_member_is_still_refused_beside_the_optional_one(self) -> None:
        assert _refusal(spec_document(tags=_STANDARD_TAGS, extra=1)) == (
            "GateSpec carries unsupported members: ['extra']."
        )

    def test_a_non_object_tag_set_is_refused(self) -> None:
        assert _refusal(spec_document(tags=["Application"])) == (
            "GateSpec.tags must be a JSON object."
        )

    def test_a_non_text_tag_value_is_refused(self) -> None:
        assert _refusal(spec_document(tags={"Application": 1})) == (
            "GateSpec.tags values must be text."
        )

    def test_fifty_tags_are_accepted_and_fifty_one_refused(self) -> None:
        fifty = {f"k{index}": "v" for index in range(50)}
        assert len(_spec(tags=fifty).tags) == 50
        assert _refusal(spec_document(tags={**fifty, "k50": "v"})) == (
            "GateSpec.tags must hold at most 50."
        )

    @pytest.mark.parametrize(
        "tags",
        [
            {"k" * 129: "v"},
            {"": "v"},
            {"Application": "v" * 257},
            {"Application": "adlytics;ai"},
            {"aws:cloudformation:stack-name": "x"},
            {"AWS:Owner": "x"},
        ],
        ids=["key-129", "key-empty", "value-257", "value-pattern", "aws-key", "aws-key-upper"],
    )
    def test_a_tag_outside_the_service_limits_is_refused(self, tags: dict[str, str]) -> None:
        assert _refusal(spec_document(tags=tags)) == (
            "GateSpec.tags keys must be 1-128 and values 0-256 tag characters, with no aws: key."
        )

    def test_the_limits_themselves_are_accepted(self) -> None:
        tags = {"k" * 128: "v" * 256, "Empty": ""}
        assert dict(_spec(tags=tags).tags) == tags


_CUSTODY_ARN_PREFIX = "arn:aws:logs:us-east-1:111122223333:log-group:"


def _custody(name: str, arn: str) -> ResultGroupCustodyV1:
    return ResultGroupCustodyV1(
        result_log_group_name=name,
        result_log_group_arn=arn,
        result_retention_days=14,
        ingress_subscription_filter_name="adlytics-dev-evaluation-result-ingress-v4",
        ingress_function_arn="arn:aws:lambda:us-east-1:111122223333:function:adlytics-dev-Ingress",
    )


class TestResultGroupCustody:
    """The probe's custody value is refused wherever a GateSpec refuses those five fields."""

    @pytest.mark.parametrize(
        ("name", "arn", "message"),
        [
            (
                "/adlytics/dev/evaluation/results-v4",
                f"{_CUSTODY_ARN_PREFIX}/adlytics/dev/evaluation/results-v4x",
                "custody group ARN must be the group's ARN without ':*'.",
            ),
            (
                "/adlytics/dev/evaluation/results-v4",
                f"{_CUSTODY_ARN_PREFIX}/adlytics/dev/evaluation/results-v4:*",
                "custody group ARN must be the group's ARN without ':*'.",
            ),
            (
                "/aws/bedrock-agentcore/evaluations/results-fictional",
                f"{_CUSTODY_ARN_PREFIX}/aws/bedrock-agentcore/evaluations/results-fictional",
                "custody group name must be outside /aws/bedrock-agentcore/evaluations/.",
            ),
            (
                "/adlytics/dev/evaluation/results v4",
                f"{_CUSTODY_ARN_PREFIX}/adlytics/dev/evaluation/results v4",
                "custody group name must be a log group name.",
            ),
        ],
        ids=["other-group-arn", "wildcard-arn", "reserved-prefix", "space-in-name"],
    )
    def test_a_custody_value_a_gate_spec_would_refuse_is_refused(
        self, name: str, arn: str, message: str
    ) -> None:
        with pytest.raises(EvaluationGateSpecError) as excinfo:
            _custody(name, arn)
        assert type(excinfo.value) is EvaluationGateSpecError
        assert str(excinfo.value) == message

    def test_the_exact_bare_arn_constructs(self) -> None:
        name = "/adlytics/dev/evaluation/results-v4"
        assert _custody(name, f"{_CUSTODY_ARN_PREFIX}{name}").result_log_group_arn == (
            f"{_CUSTODY_ARN_PREFIX}{name}"
        )

    def test_a_gate_spec_carries_exactly_its_five_custody_fields(self) -> None:
        spec = _spec()
        assert spec.custody() == ResultGroupCustodyV1(
            result_log_group_name=spec.result_log_group_name,
            result_log_group_arn=RESULT_GROUP_ARN,
            result_retention_days=14,
            ingress_subscription_filter_name=spec.ingress_subscription_filter_name,
            ingress_function_arn=spec.ingress_function_arn,
        )


def _state(spec: Any, **drift: object) -> OnlineEvaluationConfigState:
    values: dict[str, Any] = {
        "config_id": CONFIG_ID,
        "config_name": spec.config_name,
        "status": OnlineEvaluationStatus.ACTIVE,
        "execution_status": ExecutionStatus.DISABLED,
        "output_log_group_name": spec.result_log_group_name,
        "input_log_group_names": spec.input_log_group_names,
        "input_service_names": spec.input_service_names,
        "evaluator_ids": tuple(reversed(spec.evaluator_ids)),
        "execution_role_arn": spec.execution_role_arn,
        "sampling_percentage": spec.sampling_percentage,
        "session_timeout_minutes": spec.session_timeout_minutes,
    }
    values.update(drift)
    return OnlineEvaluationConfigState(**values)


class TestBindingFindings:
    def test_an_exact_binding_holds_whatever_the_evaluator_order(self) -> None:
        spec = _spec()
        assert binding_findings(spec, _state(spec), ExecutionStatus.DISABLED) == ()

    @pytest.mark.parametrize(
        ("drift", "assertion"),
        [
            ({"status": OnlineEvaluationStatus.UPDATING}, GateAssertion.CONFIG_STATUS),
            ({"config_name": "other"}, GateAssertion.CONFIG_NAME),
            ({"execution_status": ExecutionStatus.ENABLED}, GateAssertion.CONFIG_EXECUTION_STATUS),
            (
                {"output_log_group_name": "/aws/bedrock-agentcore/evaluations/results/x"},
                GateAssertion.CONFIG_OUTPUT_GROUP,
            ),
            ({"result_destination": "SOURCE_LOG_GROUP"}, GateAssertion.CONFIG_RESULT_DESTINATION),
            ({"input_log_group_prefixes": ("/adlytics/",)}, GateAssertion.CONFIG_DATA_SOURCE),
            ({"input_service_names": ("other",)}, GateAssertion.CONFIG_DATA_SOURCE),
            ({"evaluator_ids": (HELPFULNESS_ID,)}, GateAssertion.CONFIG_EVALUATORS),
            ({"execution_role_arn": "arn:aws:iam::1:role/x"}, GateAssertion.CONFIG_EXECUTION_ROLE),
            ({"sampling_percentage": 50.0}, GateAssertion.CONFIG_RULE),
            ({"filter_count": 1}, GateAssertion.CONFIG_RULE),
        ],
    )
    def test_each_drift_names_exactly_its_assertion(
        self, drift: dict[str, object], assertion: GateAssertion
    ) -> None:
        spec = _spec()
        assert binding_findings(spec, _state(spec, **drift), ExecutionStatus.DISABLED) == (
            GateFinding(assertion),
        )

    def test_the_dedicated_destination_is_accepted(self) -> None:
        spec = _spec()
        state = _state(spec, result_destination="DEDICATED_LOG_GROUP")
        assert binding_findings(spec, state, ExecutionStatus.DISABLED) == ()


def _live(spec: Any, **drift: object) -> list[EvaluatorState]:
    states = [
        EvaluatorState(
            evaluator_id=evaluator.evaluator_id,
            evaluator_name=evaluator.evaluator_name,
            status="ACTIVE",
            model_id=evaluator.model_id,
            prompt_digest=evaluator.prompt_digest,
        )
        for evaluator in spec.evaluators
    ]
    return [replace(states[0], **cast("dict[str, Any]", drift)), *states[1:]]


class TestEvaluatorFindings:
    def test_matching_evaluators_hold(self) -> None:
        spec = _spec()
        assert evaluator_findings(spec, _live(spec)) == ()

    @pytest.mark.parametrize(
        ("drift", "assertion"),
        [
            ({"status": "UPDATING"}, GateAssertion.EVALUATOR_STATUS),
            ({"evaluator_name": "renamed"}, GateAssertion.EVALUATOR_NAME),
            ({"model_id": "fictional.other-model:0"}, GateAssertion.EVALUATOR_MODEL),
            ({"prompt_digest": "b" * 64}, GateAssertion.EVALUATOR_PROMPT),
            ({"prompt_digest": ""}, GateAssertion.EVALUATOR_PROMPT),
        ],
    )
    def test_a_changed_evaluator_names_its_assertion_and_evaluator(
        self, drift: dict[str, object], assertion: GateAssertion
    ) -> None:
        spec = _spec()
        assert evaluator_findings(spec, _live(spec, **drift)) == (
            GateFinding(assertion, HELPFULNESS),
        )

    def test_a_missing_evaluator_fails_its_status(self) -> None:
        spec = _spec()
        assert evaluator_findings(spec, _live(spec)[1:]) == (
            GateFinding(GateAssertion.EVALUATOR_STATUS, HELPFULNESS),
        )

    def test_a_finding_renders_without_any_value(self) -> None:
        assert str(GateFinding(GateAssertion.EVALUATOR_PROMPT, HELPFULNESS)) == (
            f"evaluator_prompt({HELPFULNESS})"
        )
        assert str(GateFinding(GateAssertion.SUBSCRIPTION_FILTERS)) == "subscription_filters"


class TestReleaseRecords:
    def test_enabled_names_the_configuration_and_the_spec_evaluators(self) -> None:
        spec = _spec()
        release = release_enabled(spec, CONFIG_ID, "2026-09-27T12:00:00+00:00")
        assert (release.enabled, release.config_id, release.evaluators) == (
            True,
            CONFIG_ID,
            spec.evaluators,
        )

    def test_not_enabled_names_nothing(self) -> None:
        release = release_not_enabled(EvaluationFamilyV1.V4, "2026-09-27T12:00:00+00:00")
        assert (release.enabled, release.config_id, release.evaluators) == (False, None, ())

    def test_a_match_ignores_only_the_decision_time(self) -> None:
        spec = _spec()
        stored = release_enabled(spec, CONFIG_ID, "2026-01-01T00:00:00+00:00")
        assert release_matches(stored, spec, CONFIG_ID)
        assert not release_matches(stored, spec, "adlytics_test_other-ZzZzZ12345")
        assert not release_matches(None, spec, CONFIG_ID)
        changed = _spec(
            evaluators=[
                evaluator_document(HELPFULNESS, HELPFULNESS_ID, revision="r2"),
                evaluator_document(GROUNDING, GROUNDING_ID),
            ]
        )
        assert not release_matches(stored, changed, CONFIG_ID)

    def test_config_ids_are_told_apart_from_failure_placeholders(self) -> None:
        assert is_config_id(CONFIG_ID)
        assert not is_config_id("evaluation-gate:failed:request-1")


class TestCalibrationInputs:
    def test_one_input_per_declared_expectation_in_spec_order(self) -> None:
        inputs = calibration_inputs(_spec())
        assert [(item.case_name, item.evaluator_name) for item in inputs] == [
            ("known_correct", HELPFULNESS),
            ("known_correct", GROUNDING),
            ("contradiction", HELPFULNESS),
            ("contradiction", GROUNDING),
            ("fluent_but_unsupported", HELPFULNESS),
            ("fluent_but_unsupported", GROUNDING),
        ]

    def test_inputs_are_deterministic_uuid4_correlated_and_distinct(self) -> None:
        first = calibration_inputs(_spec())
        second = calibration_inputs(_spec())
        assert first == second
        correlations = {item.envelope.correlation_id for item in first}
        assert len(correlations) == len(first)
        for item in first:
            assert uuid.UUID(item.envelope.correlation_id).version == 4
            assert len(item.trace_id) == 32
            assert len(item.span_id) == 16

    def test_the_case_reaches_the_one_released_span_shape_unchanged(self) -> None:
        item = calibration_inputs(_spec())[0]
        span = json.loads(
            released_span_json(
                ReleasedSpanProjection(
                    envelope=item.envelope,
                    service_name=SERVICE_NAME,
                    timestamp_ns=1_800_000_000_000_000_000,
                    trace_id=item.trace_id,
                    span_id=item.span_id,
                )
            )
        )
        attributes = span["attributes"]
        assert attributes["gen_ai.task.output"] == "Twelve fictional widgets shipped."
        assert attributes["gen_ai.task.input"] == (
            "User request:\nHow many fictional widgets shipped?\n\n"
            "Trusted governed evidence and verification:\n"
            "The fictional ledger records 12 widgets shipped."
        )


def _observations(**labels: tuple[str, ...]) -> list[CalibrationObservationV1]:
    """Answer as a calibrated judge, with per-case helpfulness overrides keyed by case name."""
    table: dict[tuple[str, str], tuple[str, ...]] = {
        ("known_correct", HELPFULNESS): ("Excellent",),
        ("known_correct", GROUNDING): ("Grounded",),
        ("contradiction", HELPFULNESS): ("Needs Improvement",),
        ("contradiction", GROUNDING): ("Unsupported",),
        ("fluent_but_unsupported", HELPFULNESS): ("Good",),
        ("fluent_but_unsupported", GROUNDING): ("Unsupported",),
    }
    for case, override in labels.items():
        table[case, HELPFULNESS] = override
    return [
        CalibrationObservationV1(case, name, labels=value) for (case, name), value in table.items()
    ]


class TestCalibrationOutcome:
    def test_every_expectation_met_passes(self) -> None:
        outcome = calibration_outcome(_spec(), _observations())
        assert outcome.passed
        assert (outcome.check_count, outcome.failures) == (6, ())

    def test_an_unexpected_label_fails_naming_case_evaluator_and_labels(self) -> None:
        outcome = calibration_outcome(_spec(), _observations(contradiction=("Excellent",)))
        assert not outcome.passed
        assert outcome.failures == (
            CalibrationFailureV1(
                case_name="contradiction",
                evaluator_name=HELPFULNESS,
                kind=CalibrationFailureKind.UNEXPECTED_LABEL,
                expected=("poor", "needs_improvement"),
                observed=("excellent",),
            ),
        )
        assert str(outcome.failures[0]) == f"contradiction/{HELPFULNESS}:unexpected_label"

    def test_one_bad_label_among_several_results_fails(self) -> None:
        outcome = calibration_outcome(_spec(), _observations(known_correct=("Excellent", "Poor")))
        assert [failure.kind for failure in outcome.failures] == [
            CalibrationFailureKind.UNEXPECTED_LABEL
        ]

    def test_a_missing_result_fails_rather_than_passing(self) -> None:
        observations = [
            item
            for item in _observations()
            if (item.case_name, item.evaluator_name) != ("known_correct", GROUNDING)
        ]
        outcome = calibration_outcome(_spec(), observations)
        assert [(f.case_name, f.evaluator_name, f.kind) for f in outcome.failures] == [
            ("known_correct", GROUNDING, CalibrationFailureKind.MISSING)
        ]

    def test_a_service_error_fails_even_with_labels(self) -> None:
        observations = [
            CalibrationObservationV1(
                item.case_name,
                item.evaluator_name,
                labels=item.labels,
                error_code="ModelTimeout" if item.case_name == "contradiction" else "",
            )
            for item in _observations()
        ]
        kinds = {f.kind for f in calibration_outcome(_spec(), observations).failures}
        assert kinds == {CalibrationFailureKind.ERROR}

    def test_a_result_with_no_label_fails(self) -> None:
        outcome = calibration_outcome(_spec(), _observations(contradiction=()))
        assert [failure.kind for failure in outcome.failures] == [CalibrationFailureKind.NO_LABEL]

    def test_no_observations_fail_every_expectation(self) -> None:
        outcome = calibration_outcome(_spec(), [])
        assert not outcome.passed
        assert len(outcome.failures) == outcome.check_count == 6
