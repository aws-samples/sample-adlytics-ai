# SPDX-License-Identifier: MIT-0
"""The result-group custody acceptance's decisions, and its parity with the repository's lists.

Each check of `validation/probe_evaluation_custody.py` is a pure function of plain data with its
AWS reads passed as callables, so every refusal below asserts the exact output line and is paired
with a positive control. The parity tests read the TypeScript and Makefile sources the probe must
agree with, so a new watched action, invocation route or deploy flag fails here until the probe
covers it.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_evaluation_custody as probe  # noqa: E402

from domain.evaluation.gate import (  # noqa: E402
    ExecutionStatus,
    GateAssertion,
    GateFinding,
    OnlineEvaluationConfigState,
    OnlineEvaluationStatus,
)
from domain.evaluation.release import (  # noqa: E402
    EvaluationFamilyV1,
    EvaluationReleaseEvaluatorV1,
    EvaluationReleaseV1,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-079")]

ACCOUNT = "111122223333"
GROUP_ARN = f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/adlytics/dev/evaluation/results-v4"
EXECUTION = probe.Role("EvaluationV4/GateV4ExecutionRole", f"arn:aws:iam::{ACCOUNT}:role/execution")
HARNESS = probe.Role("Harness/ExecutionRole", f"arn:aws:iam::{ACCOUNT}:role/harness")
OTHER = probe.Role("Api/ConversationFunctionRole", f"arn:aws:iam::{ACCOUNT}:role/other")
INGRESS = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:adlytics-dev-Ingress"
GATE = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:adlytics-dev-EvaluationGate"
ELSEWHERE = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:adlytics-dev-Elsewhere"
FLAGS = (
    "ADLYTICS_ONLINE_EVALUATION_PROVISIONED=true,ADLYTICS_ONLINE_EVALUATION_V4_REQUESTED=true,"
    "ADLYTICS_ONLINE_EVALUATION_SAMPLE_PERCENT=100"
)


def _grants(*grants: tuple[probe.Role, str, str | None]) -> probe.Allowed:
    """Allow exactly the listed (role, action, resource) triples; a None resource allows any."""

    def allowed(role: probe.Role, call: probe.Simulation) -> bool:
        return any(
            role == r and call.action == a and (res is None or call.resource == res)
            for r, a, res in grants
        )

    return allowed


def _execution_writes() -> list[tuple[probe.Role, str, str | None]]:
    return [
        (EXECUTION, "logs:CreateLogGroup", GROUP_ARN),
        (EXECUTION, "logs:CreateLogStream", f"{GROUP_ARN}:*"),
        (EXECUTION, "logs:PutLogEvents", f"{GROUP_ARN}:*"),
    ]


class TestTarget:
    def test_a_non_dev_region_is_refused_before_any_aws_call(self) -> None:
        calls: list[str] = []

        def account() -> str:
            calls.append("sts")
            return ACCOUNT

        assert probe.check_target("dev", "eu-west-1", ACCOUNT, account) == (
            "FAIL target: region must be us-east-1"
        )
        assert calls == []

    def test_the_dev_target_passes(self) -> None:
        assert probe.check_target("dev", "us-east-1", ACCOUNT, lambda: ACCOUNT) == "PASS target"

    def test_a_refused_target_stops_the_run(self) -> None:
        def unreachable(*_args: object) -> Any:
            raise AssertionError("no lookup may run after a refused target")

        lookups = probe.Lookups(*([unreachable] * 11))  # type: ignore[arg-type]
        request = probe.ProbeRequest("dev", "eu-west-1", ACCOUNT, (), FLAGS, FLAGS)
        assert probe.run(request, lookups) == (["FAIL target: region must be us-east-1"], 1)


class TestConfigFlags:
    def test_a_differing_flag_is_named_without_its_value(self) -> None:
        expected = FLAGS.replace("SAMPLE_PERCENT=100", "SAMPLE_PERCENT=10")
        assert probe.check_config_flags(FLAGS, expected) == [
            "FAIL config_flags: ADLYTICS_ONLINE_EVALUATION_SAMPLE_PERCENT differs from the "
            "expected deploy flags"
        ]

    def test_an_unset_evaluation_flag_is_refused_even_when_both_lines_agree(self) -> None:
        line = FLAGS.replace("V4_REQUESTED=true", "V4_REQUESTED=unset")
        assert probe.check_config_flags(line, line) == [
            "FAIL config_flags: ADLYTICS_ONLINE_EVALUATION_V4_REQUESTED must be true"
        ]

    def test_equal_lines_with_both_flags_true_pass(self) -> None:
        assert probe.check_config_flags(FLAGS, FLAGS) == ["PASS config_flags"]


STACKS = ("Adlytics-EvaluationV4-dev", "Adlytics-EvaluationResultLinkV3-dev", "Adlytics-Api-dev")


class TestStackSet:
    def test_a_missing_evaluation_stack_is_named(self) -> None:
        names = [n for n in STACKS if n != "Adlytics-EvaluationV4-dev"]
        assert probe.check_stack_set(names, lambda _n: "UPDATE_COMPLETE") == [
            "FAIL stack_set: Adlytics-EvaluationV4-dev is not in the stack list"
        ]

    def test_an_unsettled_stack_is_named_by_short_name(self) -> None:
        def status(name: str) -> str:
            return "UPDATE_ROLLBACK_FAILED" if "EvaluationV4" in name else "UPDATE_COMPLETE"

        assert probe.check_stack_set(STACKS, status) == [
            "FAIL stack_set: EvaluationV4 is UPDATE_ROLLBACK_FAILED"
        ]

    def test_a_name_outside_the_application_pattern_is_refused(self) -> None:
        assert probe.check_stack_set((*STACKS, "CDKToolkit"), lambda _n: "UPDATE_COMPLETE") == [
            "FAIL stack_set: CDKToolkit is not an application stack name"
        ]

    def test_every_stack_settled_passes(self) -> None:
        assert probe.check_stack_set(STACKS, lambda _n: "UPDATE_COMPLETE") == ["PASS stack_set"]


def _gate_template(result_name: object) -> Mapping[str, Any]:
    from evaluation_gate_support import spec_document  # noqa: PLC0415 - unit support module

    document = spec_document()
    text = probe.json.dumps(document)
    group = str(document["result_log_group_name"])
    before, after = text.split(f'"{group}"', 1)
    join: list[object] = [before, '"', result_name, '"', after]
    return {
        "Parameters": {
            "SsmParameterValueResultGroup": {
                "Type": "AWS::SSM::Parameter::Value<String>",
                "Default": "/adlytics/test/evaluation/result-log-group-v4",
            }
        },
        "Resources": {
            "EvaluationGateV4": {
                "Type": probe.EVALUATION_GATE_RESOURCE_TYPE,
                "Properties": {"GateSpec": {"Fn::Join": ["", join]}},
            }
        },
    }


def _spec_lookups(values: Mapping[str, str]) -> probe.SpecLookups:
    return probe.SpecLookups(
        ssm_value=lambda name: values[name],
        pseudo={"AWS::Partition": "aws", "AWS::Region": "us-east-1", "AWS::AccountId": ACCOUNT},
        role_arn=lambda _logical_id: EXECUTION.arn,
    )


def _with_token(template: Mapping[str, Any], literal: str, token: object) -> Mapping[str, Any]:
    """Replace the quoted `literal` in the GateSpec join with `token`, as CDK renders one."""
    gate = template["Resources"]["EvaluationGateV4"]
    parts: list[object] = []
    for part in gate["Properties"]["GateSpec"]["Fn::Join"][1]:
        if isinstance(part, str) and f'"{literal}"' in part:
            before, after = part.split(f'"{literal}"', 1)
            parts += [before, '"', token, '"', after]
        else:
            parts.append(part)
    spec = {"GateSpec": {"Fn::Join": ["", parts]}}
    resources = {"EvaluationGateV4": {**gate, "Properties": spec}}
    return {**template, "Resources": resources}


_GROUP_VALUES = {
    "/adlytics/test/evaluation/result-log-group-v4": "/adlytics/test/evaluation/results-v4"
}


class TestSpecResolution:
    def test_an_evaluator_named_by_its_resource_attribute_still_resolves(self) -> None:
        # The deployed GateSpec names each evaluator by `Fn::GetAtt [<evaluator>, EvaluatorId]`,
        # which no read can resolve; only the required fields are decoded.
        from evaluation_gate_support import spec_document  # noqa: PLC0415 - unit support module

        evaluators = cast("list[Mapping[str, object]]", spec_document()["evaluators"])
        evaluator_id = str(evaluators[0]["evaluator_id"])
        template = _with_token(
            _gate_template({"Ref": "SsmParameterValueResultGroup"}),
            evaluator_id,
            {"Fn::GetAtt": ["FictionalEvaluator", "EvaluatorId"]},
        )
        lines, resolved = probe.check_spec_resolution(template, _spec_lookups(_GROUP_VALUES))
        assert lines == ["PASS spec_resolution"]
        assert resolved is not None
        assert resolved.custody.result_log_group_name == "/adlytics/test/evaluation/results-v4"

    def test_a_custody_field_the_gate_would_refuse_is_refused_with_its_message(self) -> None:
        from evaluation_gate_support import spec_document  # noqa: PLC0415 - unit support module

        arn = str(spec_document()["result_log_group_arn"])
        template = _with_token(
            _gate_template({"Ref": "SsmParameterValueResultGroup"}),
            arn,
            {"Fn::Join": ["", [arn, ":*"]]},
        )
        lines, resolved = probe.check_spec_resolution(template, _spec_lookups(_GROUP_VALUES))
        assert lines == [
            "FAIL spec_resolution: custody group ARN must be the group's ARN without ':*'."
        ]
        assert resolved is None

    def test_an_unsupported_token_in_a_required_field_is_refused(self) -> None:
        template = _gate_template({"Fn::ImportValue": "fictional"})
        lines, resolved = probe.check_spec_resolution(template, _spec_lookups({}))
        assert lines == ["FAIL spec_resolution: result_log_group_name is not resolvable"]
        assert resolved is None

    def test_a_join_with_an_ssm_reference_resolves_to_the_custody_value(self) -> None:
        template = _gate_template({"Ref": "SsmParameterValueResultGroup"})
        values = {
            "/adlytics/test/evaluation/result-log-group-v4": "/adlytics/test/evaluation/results-v4"
        }
        lines, resolved = probe.check_spec_resolution(template, _spec_lookups(values))
        assert lines == ["PASS spec_resolution"]
        assert resolved is not None
        assert resolved.custody.result_log_group_name == "/adlytics/test/evaluation/results-v4"
        assert resolved.release_parameter_name == "/adlytics/test/evaluation/release-v4"


class TestReaderBound:
    @pytest.mark.parametrize("action", ["logs:StartLiveTail", "logs:GetLogRecord"])
    def test_one_role_that_can_read_fails(self, action: str) -> None:
        allowed = _grants((OTHER, action, GROUP_ARN))
        assert probe.check_reader_bound([OTHER, EXECUTION], allowed, GROUP_ARN) == (
            "FAIL reader_bound: 1 role may read the result group"
        )

    def test_no_reader_passes(self) -> None:
        allowed = _grants(*_execution_writes())
        assert (
            probe.check_reader_bound([OTHER, EXECUTION], allowed, GROUP_ARN) == "PASS reader_bound"
        )


class TestWriterBound:
    def test_a_second_writer_fails(self) -> None:
        allowed = _grants(*_execution_writes(), (OTHER, "logs:PutLogEvents", f"{GROUP_ARN}:*"))
        assert probe.check_writer_bound([OTHER, EXECUTION], allowed, GROUP_ARN, EXECUTION.arn) == [
            "FAIL writer_bound: 2 roles may write the result group; "
            "expected only the execution role"
        ]

    def test_an_execution_role_without_put_log_events_fails(self) -> None:
        allowed = _grants(*_execution_writes()[:2])
        assert probe.check_writer_bound([OTHER, EXECUTION], allowed, GROUP_ARN, EXECUTION.arn) == [
            "FAIL writer_bound: the execution role may not write the result group"
        ]

    def test_only_the_execution_role_with_all_three_passes(self) -> None:
        allowed = _grants(*_execution_writes())
        assert probe.check_writer_bound([OTHER, EXECUTION], allowed, GROUP_ARN, EXECUTION.arn) == [
            "PASS writer_bound"
        ]


class TestCustodyBound:
    @pytest.mark.parametrize("action", ["logs:CreateExportTask", "logs:PutSubscriptionFilter"])
    def test_one_role_that_can_change_or_export_fails(self, action: str) -> None:
        allowed = _grants((OTHER, action, GROUP_ARN))
        assert probe.check_custody_bound([OTHER], allowed, GROUP_ARN) == (
            "FAIL custody_bound: 1 role may change or export the result group"
        )

    def test_an_unscoped_action_never_fails_it(self) -> None:
        allowed = _grants((HARNESS, "logs:PutResourcePolicy", "*"))
        assert probe.check_custody_bound([HARNESS], allowed, GROUP_ARN) == "PASS custody_bound"


class TestUnscopedBound:
    @pytest.mark.parametrize("action", ["logs:PutResourcePolicy", "logs:PutDeliveryDestination"])
    def test_a_role_that_can_open_any_group_is_named(self, action: str) -> None:
        allowed = _grants((HARNESS, action, "*"))
        assert probe.check_unscoped_bound([HARNESS, OTHER], allowed) == (
            "FAIL unscoped_bound: 1 role may create or change an account or resource Logs policy "
            "(Harness/ExecutionRole)"
        )

    def test_no_holder_passes(self) -> None:
        assert probe.check_unscoped_bound([HARNESS, OTHER], _grants()) == "PASS unscoped_bound"


class TestInvokeBound:
    def test_an_event_source_mapping_on_the_ingress_fails(self) -> None:
        def allowed(role: probe.Role, call: probe.Simulation) -> bool:
            return (
                role == OTHER
                and call.action == "lambda:CreateEventSourceMapping"
                and call.function_arn == INGRESS
            )

        assert probe.check_invoke_bound(
            [OTHER], allowed, [INGRESS, GATE], "us-east-1", ACCOUNT
        ) == (
            "FAIL invoke_bound: 1 role may attach an invocation source to, or change, the result "
            "ingress or gate"
        )

    def test_a_resource_policy_change_on_the_gate_fails(self) -> None:
        allowed = _grants((OTHER, "lambda:PutResourcePolicy", GATE))
        assert probe.check_invoke_bound(
            [OTHER], allowed, [INGRESS, GATE], "us-east-1", ACCOUNT
        ) == (
            "FAIL invoke_bound: 1 role may attach an invocation source to, or change, the result "
            "ingress or gate"
        )

    def test_unresolved_functions_fail(self) -> None:
        assert probe.check_invoke_bound([OTHER], _grants(), [], "us-east-1", ACCOUNT) == (
            "FAIL invoke_bound: the result ingress or gate function could not be resolved"
        )

    def test_invoking_another_function_passes(self) -> None:
        allowed = _grants((OTHER, "lambda:InvokeFunction", ELSEWHERE))
        assert probe.check_invoke_bound(
            [OTHER], allowed, [INGRESS, GATE], "us-east-1", ACCOUNT
        ) == ("PASS invoke_bound")


class TestCustodyAndWiring:
    def test_a_readback_finding_is_named(self) -> None:
        assert probe.check_custody([GateFinding(GateAssertion.GROUP_RETENTION)]) == (
            "FAIL custody: group_retention"
        )

    def test_no_finding_passes(self) -> None:
        assert probe.check_custody([]) == "PASS custody"


_CONFIG_ID = "config-AbCdE12345"
_EVALUATOR_IDS = ("a-AbCdE12345", "b-AbCdE12345")


def _record(
    *,
    enabled: bool = True,
    config_id: str | None = _CONFIG_ID,
    ids: tuple[str, ...] = _EVALUATOR_IDS,
) -> EvaluationReleaseV1:
    return EvaluationReleaseV1(
        family=EvaluationFamilyV1.V4,
        enabled=enabled,
        config_id=config_id,
        evaluators=tuple(
            EvaluationReleaseEvaluatorV1(
                evaluator_id=evaluator_id,
                evaluator_name=f"name_{index}",
                revision="1",
                model_id="fictional.model-v1:0",
                prompt_digest="0" * 64,
            )
            for index, evaluator_id in enumerate(ids)
        ),
        decided_at="2026-10-02T00:00:00+00:00",
    )


def _state(execution: ExecutionStatus = ExecutionStatus.ENABLED) -> OnlineEvaluationConfigState:
    return OnlineEvaluationConfigState(
        config_id=_CONFIG_ID,
        config_name="adlytics_dev_released_v4",
        status=OnlineEvaluationStatus.ACTIVE,
        execution_status=execution,
        evaluator_ids=_EVALUATOR_IDS,
    )


class TestWiring:
    @pytest.mark.parametrize(
        ("record", "state", "line"),
        [
            (
                _record(config_id="other-AbCdE12345"),
                _state(),
                "FAIL wiring: record config_id does not equal the gate configuration",
            ),
            (
                _record(enabled=False, config_id=None, ids=()),
                _state(),
                "FAIL wiring: the release record is not enabled",
            ),
            (
                _record(),
                _state(ExecutionStatus.DISABLED),
                "FAIL wiring: the configuration is not ACTIVE and ENABLED",
            ),
            (
                _record(ids=(*_EVALUATOR_IDS, "c-AbCdE12345")),
                _state(),
                "FAIL wiring: record and configuration evaluator sets differ",
            ),
        ],
        ids=["config-id", "disabled-record", "disabled-config", "evaluator-sets"],
    )
    def test_each_wiring_break_is_one_line(
        self, record: EvaluationReleaseV1, state: OnlineEvaluationConfigState, line: str
    ) -> None:
        assert probe.check_wiring(record, state, _CONFIG_ID) == [line]

    def test_the_matching_record_and_configuration_pass(self) -> None:
        assert probe.check_wiring(_record(), _state(), _CONFIG_ID) == ["PASS wiring"]


class TestReport:
    def test_an_unwatched_action_is_reported_without_failing(self) -> None:
        allowed = _grants(*_execution_writes(), (OTHER, "logs:PutMetricFilter", GROUP_ARN))
        assert probe.report_unwatched([OTHER, EXECUTION], allowed, GROUP_ARN) == (
            "REPORT unwatched_logs_actions: logs:PutMetricFilter=1"
        )

    def test_a_clean_report_says_none(self) -> None:
        assert probe.report_unwatched([OTHER], _grants(), GROUP_ARN) == (
            "REPORT unwatched_logs_actions: none"
        )


def _unreachable(*_args: object) -> Any:
    raise AssertionError("no lookup may run after a failed prerequisite")


def _templates(v4: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Return the V4 template with its gate's resource policy, and the link with the ingress's."""

    def policy(function: str) -> dict[str, Any]:
        return {
            "Type": "AWS::Lambda::ResourcePolicy",
            "Properties": {"ResourceArn": {"Fn::GetAtt": [function, "Arn"]}},
        }

    resources = {**v4["Resources"], "GatePolicy": policy("GateFunction")}
    return {
        "Adlytics-EvaluationV4-dev": {**v4, "Resources": resources},
        "Adlytics-EvaluationResultLinkV3-dev": {
            "Resources": {"IngressPolicy": policy("IngressV3")}
        },
    }


def _passing_lookups(**overrides: object) -> probe.Lookups:
    """Lookups under which every check passes and one role holds one unwatched action."""
    templates = _templates(_gate_template({"Ref": "SsmParameterValueResultGroup"}))
    lines, resolved = probe.check_spec_resolution(
        templates["Adlytics-EvaluationV4-dev"], _spec_lookups(_GROUP_VALUES)
    )
    assert lines == ["PASS spec_resolution"]
    assert resolved is not None
    arn = resolved.custody.result_log_group_arn
    execution = probe.Role("EvaluationV4/GateV4ExecutionRole", resolved.execution_role_arn)
    functions = {"IngressV3": INGRESS, "GateFunction": GATE}
    lookups = probe.Lookups(
        caller_account=lambda: ACCOUNT,
        stack_status=lambda _name: "UPDATE_COMPLETE",
        template=lambda name: templates[name],
        spec_lookups=lambda _name: _spec_lookups(_GROUP_VALUES),
        roles=lambda _stacks: [OTHER, execution],
        allowed=_grants(
            (execution, "logs:CreateLogGroup", arn),
            (execution, "logs:CreateLogStream", f"{arn}:*"),
            (execution, "logs:PutLogEvents", f"{arn}:*"),
            (OTHER, "logs:PutMetricFilter", arn),
        ),
        function_arn=lambda _stack, logical_id: functions[logical_id],
        gate_config_id=lambda _stack, _logical_id: _CONFIG_ID,
        readback=lambda _custody: [],
        release=lambda _name: _record(),
        config_state=lambda _config_id: _state(),
    )
    return dataclasses.replace(lookups, **overrides)


class TestBatchedSimulation:
    def test_each_action_keeps_its_own_decision_in_a_batch(self) -> None:
        results = [
            {
                "EvalActionName": "logs:GetLogEvents",
                "EvalDecision": "implicitDeny",
                "ResourceSpecificResults": [{"EvalResourceDecision": "implicitDeny"}],
            },
            {
                "EvalActionName": "logs:PutLogEvents",
                "EvalDecision": "allowed",
                "ResourceSpecificResults": [{"EvalResourceDecision": "allowed"}],
            },
            {"EvalActionName": "logs:PutResourcePolicy", "EvalDecision": "explicitDeny"},
        ]
        assert probe.decisions_by_action(results) == {
            "logs:GetLogEvents": False,
            "logs:PutLogEvents": True,
            "logs:PutResourcePolicy": False,
        }

    def test_every_simulated_action_is_in_its_service_batch(self) -> None:
        simulated = {
            *probe.READ,
            *probe.WRITE_STREAM,
            probe.CREATE_GROUP,
            *probe.CUSTODY_SCOPED,
            *probe.CUSTODY_UNSCOPED,
            *probe.UNWATCHED_LOGS_ACTIONS,
            *probe.INVOKE_SOURCE,
        }
        missing = sorted(
            action
            for action in simulated
            if action not in probe.SIMULATION_BATCHES.get(action.partition(":")[0], ())
        )
        assert missing == []


_REQUEST = probe.ProbeRequest("dev", "us-east-1", ACCOUNT, STACKS, FLAGS, FLAGS)
_PREREQUISITES = ["PASS target", "PASS config_flags"]


class TestRun:
    def test_the_report_never_fails_a_run_whose_eleven_checks_pass(self) -> None:
        assert probe.run(_REQUEST, _passing_lookups()) == (
            [
                *_PREREQUISITES,
                "PASS stack_set",
                "PASS spec_resolution",
                "PASS reader_bound",
                "PASS writer_bound",
                "PASS custody_bound",
                "PASS unscoped_bound",
                "PASS invoke_bound",
                "PASS custody",
                "PASS wiring",
                "REPORT unwatched_logs_actions: logs:PutMetricFilter=1",
            ],
            0,
        )

    def test_a_failed_stack_set_stops_the_run_before_any_template_or_role_lookup(self) -> None:
        def status(name: str) -> str:
            return "UPDATE_ROLLBACK_FAILED" if "EvaluationV4" in name else "UPDATE_COMPLETE"

        lookups = _passing_lookups(
            stack_status=status,
            template=_unreachable,
            spec_lookups=_unreachable,
            roles=_unreachable,
        )
        assert probe.run(_REQUEST, lookups) == (
            [*_PREREQUISITES, "FAIL stack_set: EvaluationV4 is UPDATE_ROLLBACK_FAILED"],
            1,
        )

    def test_a_failed_spec_resolution_stops_the_run_before_any_role_lookup(self) -> None:
        unresolvable = _templates(_gate_template({"Fn::ImportValue": "fictional"}))

        def template(name: str) -> Mapping[str, Any]:
            return unresolvable[name]

        lookups = _passing_lookups(template=template, roles=_unreachable)
        assert probe.run(_REQUEST, lookups) == (
            [
                *_PREREQUISITES,
                "PASS stack_set",
                "FAIL spec_resolution: result_log_group_name is not resolvable",
            ],
            1,
        )


# --- Parity with the repository's own lists --------------------------------------------------

_RECORD_INTEGRITY = _ROOT / "infra/lib/constructs/agent-quality/record-integrity.ts"
_LOG_WRITE_SCOPE = _ROOT / "infra/test/support/log-write-scope.ts"
_INVOKE_SOURCES = _ROOT / "infra/test/support/invoke-sources.ts"
_CONFIG = _ROOT / "infra/lib/config/index.ts"


def _ts_array(path: Path, name: str) -> list[str]:
    source = re.sub(r"//[^\n]*", "", path.read_text())
    match = re.search(rf"export const {name}\b[^=]*=\s*\[(.*?)\]", source, re.DOTALL)
    assert match is not None, f"{name} not found in {path.name}"
    return re.findall(r"'([^']+)'", match.group(1))


class TestParity:
    def test_the_custody_classes_split_every_watched_logs_action_once(self) -> None:
        watched = {
            f"logs:{name}"
            for name in _ts_array(_RECORD_INTEGRITY, "RESULT_GROUP_EVENT_NAMES")
            + _ts_array(_RECORD_INTEGRITY, "ACCOUNT_LOGS_EVENT_NAMES")
        } - {"logs:StartLiveTail"}
        assert not set(probe.CUSTODY_SCOPED) & set(probe.CUSTODY_UNSCOPED)
        assert set(probe.CUSTODY_SCOPED) | set(probe.CUSTODY_UNSCOPED) == watched
        assert len(watched) == 17

    @pytest.mark.parametrize("name", ["READ", "CUSTODY_SCOPED", "CUSTODY_UNSCOPED"])
    def test_the_probe_classes_equal_the_synth_gate(self, name: str) -> None:
        assert list(getattr(probe, name)) == _ts_array(_LOG_WRITE_SCOPE, name)

    def test_the_invocation_routes_equal_the_synth_gate_and_cover_every_watched_verb(self) -> None:
        assert list(probe.INVOKE_SOURCE) == _ts_array(_INVOKE_SOURCES, "INVOKE_SOURCE_ACTIONS")
        verbs = _ts_array(_RECORD_INTEGRITY, "FUNCTION_MAPPING_EVENT_VERBS") + _ts_array(
            _RECORD_INTEGRITY, "FUNCTION_POLICY_EVENT_VERBS"
        )
        codes = [
            re.sub(r"20150331.*\Z", "", name)
            for name in _ts_array(_RECORD_INTEGRITY, "FUNCTION_CODE_EVENT_NAMES")
        ]
        expected = {f"lambda:{verb}" for verb in [*verbs, *codes] if verb != "CreateFunction"}
        assert expected <= set(probe.INVOKE_SOURCE)

    def test_the_gate_resource_type_equals_the_construct_constant(self) -> None:
        source = (_ROOT / "infra/lib/constructs/agent-quality/evaluation-gate.ts").read_text()
        match = re.search(r"export const EVALUATION_GATE_RESOURCE_TYPE = '([^']+)';", source)
        assert match is not None
        assert match.group(1) == probe.EVALUATION_GATE_RESOURCE_TYPE


# Every ADLYTICS_* key infra/lib/config reads that is not a deploy flag, with its reason.
_REVIEWED_EXCLUSIONS = {
    "ADLYTICS_API_ORIGIN_ATTACHED": "set by deploy-all.sh itself",
    "ADLYTICS_CONVERSATION_ORIGIN_ATTACHED": "set by deploy-all.sh itself",
    "ADLYTICS_QUICK_CHAT_MODEL_ID": "a string input, never printed",
    "ADLYTICS_DEEP_DIVE_MODEL_ID": "a string input, never printed",
    "ADLYTICS_MEMORY_MODEL_ID": "a string input, never printed",
    "ADLYTICS_INSIGHT_QUICK_MODEL_ID": "a string input, never printed",
    "ADLYTICS_INSIGHT_DEEP_MODEL_ID": "a string input, never printed",
    "ADLYTICS_LOGIN_DOMAIN_PREFIX": "a string input, never printed",
    "ADLYTICS_LANDING_PIPELINE_ROLE_ARN": "a string input, never printed",
    "ADLYTICS_LANDING_EXTERNAL_ID": "a string input, never printed",
    "ADLYTICS_ORIGIN_VERIFY_SECRET_VERSION_ID": "a string input, never printed",
    "ADLYTICS_STAFFED_ALARM_TOPIC_ARN": "a string input, never printed",
}


def _deploy_flag_names() -> set[str]:
    makefile = (_ROOT / "Makefile").read_text()
    match = re.search(r"^DEPLOY_FLAG_NAMES := ((?:.*\\\n)*.*)$", makefile, re.MULTILINE)
    assert match is not None
    return set(re.findall(r"ADLYTICS_[A-Z0-9_]+", match.group(1)))


def _unclassified(config_source: str) -> list[str]:
    keys = sorted(set(re.findall(r"ADLYTICS_[A-Z0-9_]+", config_source)))
    flags = _deploy_flag_names()
    return [
        f"{key} is neither a deploy flag nor a reviewed exclusion"
        for key in keys
        if key not in flags and key not in _REVIEWED_EXCLUSIONS
    ]


class TestDeployFlags:
    def test_a_new_config_key_must_be_classified(self) -> None:
        source = _CONFIG.read_text() + "\nconst limit = envInt('ADLYTICS_FICTIONAL_LIMIT', 5);\n"
        assert _unclassified(source) == [
            "ADLYTICS_FICTIONAL_LIMIT is neither a deploy flag nor a reviewed exclusion"
        ]

    def test_every_config_key_is_a_flag_or_a_reviewed_exclusion(self) -> None:
        assert _unclassified(_CONFIG.read_text()) == []
        keys = set(re.findall(r"ADLYTICS_[A-Z0-9_]+", _CONFIG.read_text()))
        assert keys == _deploy_flag_names() | set(_REVIEWED_EXCLUSIONS)

    def test_only_the_config_module_reads_the_process_environment(self) -> None:
        readers = sorted(
            str(path.relative_to(_ROOT))
            for root in ("infra/bin", "infra/lib")
            for path in (_ROOT / root).rglob("*.ts")
            if "process.env" in path.read_text()
        )
        assert readers == ["infra/lib/config/index.ts"]
