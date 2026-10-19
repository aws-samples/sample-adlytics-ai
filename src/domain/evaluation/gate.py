# SPDX-License-Identifier: MIT-0
"""Pure decisions of the evaluation enablement gate, `Custom::AgentCoreOnlineEvaluation` (ADR-079).

The gate owns one family's online-evaluation configuration and is the only writer of that
family's release record. The stack delivers the whole desired state as one JSON string property,
`GateSpec`, because CloudFormation delivers every custom-resource property value as text; a
second property, `SpecDigest`, exists only so that any change reruns the gate.

This module owns the closed callback envelope, the spec and its validation, the readback
assertions over the configuration and its evaluators, the release records the gate writes, and
the calibration verdict. The adapter owns the clock, the AgentCore and CloudWatch Logs calls,
Parameter Store, and the HTTPS response.

Calibration follows the agent-evaluation-validity steering: every evaluator ships with a
fictional corpus of at least `MIN_CALIBRATION_CASES` cases that includes at least one case of
each `REQUIRED_CALIBRATION_KINDS` kind, each case declares its closed kind and acceptable
labels, and a missing, errored, or unexpected verdict blocks enablement. Nothing here carries an
evaluator's explanation; a failure names the case, the evaluator, and closed labels only.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final, cast

from domain.evaluation.envelope import EvaluationEnvelope
from domain.evaluation.linkage import normalize_scale_label
from domain.evaluation.release import (
    CURRENT_EVALUATION_FAMILIES,
    EvaluationFamilyV1,
    EvaluationReleaseEvaluatorV1,
    EvaluationReleaseV1,
    require_unique_evaluators,
)

__all__ = [
    "DIGEST_PROPERTY",
    "GATE_SPEC_SCHEMA",
    "MIN_CALIBRATION_CASES",
    "REQUIRED_CALIBRATION_KINDS",
    "RESERVED_RESULT_GROUP_PREFIX",
    "SPEC_PROPERTY",
    "CalibrationCaseKind",
    "CalibrationCaseV1",
    "CalibrationFailureKind",
    "CalibrationFailureV1",
    "CalibrationInputV1",
    "CalibrationObservationV1",
    "CalibrationOutcomeV1",
    "EvaluationGateSpecError",
    "EvaluationGateSpecV1",
    "EvaluatorState",
    "ExecutionStatus",
    "GateAssertion",
    "GateCallbackEvent",
    "GateFinding",
    "GateRequestType",
    "OnlineEvaluationConfigState",
    "OnlineEvaluationStatus",
    "ResultGroupCustodyV1",
    "binding_findings",
    "calibration_inputs",
    "calibration_outcome",
    "client_token",
    "evaluator_findings",
    "is_config_id",
    "parse_gate_event",
    "parse_gate_spec",
    "release_enabled",
    "release_matches",
    "release_not_enabled",
]

SPEC_PROPERTY: Final = "GateSpec"
"""The one resource property that carries the whole desired gate state as JSON text."""

DIGEST_PROPERTY: Final = "SpecDigest"
"""A digest the stack renders so every spec change reruns the gate; the provider ignores it."""

GATE_SPEC_SCHEMA: Final = "EvaluationGateSpec.v1"
RESERVED_RESULT_GROUP_PREFIX: Final = "/aws/bedrock-agentcore/evaluations/"
"""The service-created result prefix; an owned result group never lives under it."""

MIN_CALIBRATION_CASES: Final = 3
"""Fewest calibration cases each evaluator needs; `REQUIRED_CALIBRATION_KINDS` names the kinds."""

_SERVICE_PROPERTIES: Final = frozenset({"ServiceToken", "ServiceTimeout"})
_RESOURCE_PROPERTIES: Final = frozenset({SPEC_PROPERTY, DIGEST_PROPERTY})
_CLIENT_TOKEN_LABEL: Final = "adlytics-evaluation-gate/v1"  # noqa: S105 - a hash label
_CALIBRATION_LABEL: Final = "adlytics-evaluation-gate-calibration/v1"

_CONFIG_NAME: Final = re.compile(r"\A[a-zA-Z][a-zA-Z0-9_]{0,47}\Z")
_CONFIG_ID: Final = re.compile(r"\A[a-zA-Z][a-zA-Z0-9_-]{0,99}-[a-zA-Z0-9]{10}\Z")
_LOG_GROUP_NAME: Final = re.compile(r"\A[.\-_/#A-Za-z0-9]{1,512}\Z")
_SERVICE_NAME: Final = re.compile(r"\A[a-zA-Z0-9._-]{1,256}\Z")
# A-REGEX-RUNTIME-001: anchored, no overlapping repeats; see docs/security-dispositions.md
# nosemgrep: python.lang.security.audit.regex_dos
_ROLE_ARN: Final = re.compile(r"\Aarn:aws(-[a-z]+)*:iam::[0-9]{12}:role/[\w+=,.@/-]{1,512}\Z")
# A-REGEX-RUNTIME-001: anchored, no overlapping repeats; see docs/security-dispositions.md
# nosemgrep: python.lang.security.audit.regex_dos
_FUNCTION_ARN: Final = re.compile(
    r"\Aarn:aws(-[a-z]+)*:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9_-]{1,140}\Z"
)
# A-REGEX-RUNTIME-001: anchored, no overlapping repeats; see docs/security-dispositions.md
# nosemgrep: python.lang.security.audit.regex_dos
_LOG_GROUP_ARN_PREFIX: Final = re.compile(
    r"\Aarn:aws(-[a-z]+)*:logs:[a-z0-9-]+:[0-9]{12}:log-group:"
)
_FILTER_NAME: Final = re.compile(r"\A[^:*]{1,512}\Z")
_PARAMETER_PATH: Final = re.compile(r"\A(/[A-Za-z0-9_.-]+)+\Z")
_CASE_NAME: Final = re.compile(r"\A[A-Za-z0-9._-]{1,128}\Z")
_LABEL: Final = re.compile(r"\A[a-z0-9_]{1,64}\Z")
_MAX_DESCRIPTION: Final = 200
_MAX_INPUT_GROUPS: Final = 10
_MAX_EVALUATORS: Final = 25
_MAX_SESSION_TIMEOUT_MINUTES: Final = 1440
_MAX_SAMPLING_PERCENTAGE: Final = 100.0
_MAX_CASE_TEXT: Final = 64 * 1024
# CreateOnlineEvaluationConfig `tags` (the service model's TagsMap): at most 50 entries, a key of
# 1-128 and a value of 0-256 characters, both from this set; `aws:` keys are the service's own.
_MAX_TAGS: Final = 50
_TAG_KEY: Final = re.compile(r"\A[a-zA-Z0-9\s._:/=+@-]{1,128}\Z")
_TAG_VALUE: Final = re.compile(r"\A[a-zA-Z0-9\s._:/=+@-]{0,256}\Z")
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
"""The retention values CloudWatch Logs accepts."""

_SPEC_MEMBERS: Final = frozenset(
    {
        "schema_version",
        "family",
        "config_name",
        "description",
        "execution_requested",
        "execution_role_arn",
        "input_log_group_names",
        "input_service_names",
        "sampling_percentage",
        "session_timeout_minutes",
        "result_log_group_name",
        "result_log_group_arn",
        "result_retention_days",
        "ingress_subscription_filter_name",
        "ingress_function_arn",
        "release_parameter_name",
        "evaluators",
        "calibration",
    }
)
_OPTIONAL_SPEC_MEMBERS: Final = frozenset({"tags"})
"""Members a GateSpec may omit. A rollback delivers the properties of the template before `tags`
existed, and a required member would fail that rollback."""
_EVALUATOR_MEMBERS: Final = frozenset(
    {"evaluator_id", "evaluator_name", "revision", "model_id", "prompt_digest"}
)
_CASE_MEMBERS: Final = frozenset(
    {"name", "kind", "question", "grounded_context", "candidate", "expected"}
)
_UUID_VARIANTS: Final = "89ab"
_SAMPLING_TOLERANCE: Final = 1e-9


class EvaluationGateSpecError(ValueError):
    """A callback or its GateSpec violates the closed custom-resource contract."""


class GateRequestType(StrEnum):
    """The three CloudFormation custom-resource request types."""

    CREATE = "Create"
    UPDATE = "Update"
    DELETE = "Delete"


class OnlineEvaluationStatus(StrEnum):
    """GetOnlineEvaluationConfig `status`, closed to the service model's enum."""

    ACTIVE = "ACTIVE"
    CREATING = "CREATING"
    CREATE_FAILED = "CREATE_FAILED"
    UPDATING = "UPDATING"
    UPDATE_FAILED = "UPDATE_FAILED"
    DELETING = "DELETING"
    ERROR = "ERROR"

    @property
    def in_progress(self) -> bool:
        """Whether a create or update is still running and a new mutation would conflict."""
        return self in {OnlineEvaluationStatus.CREATING, OnlineEvaluationStatus.UPDATING}

    @property
    def failed(self) -> bool:
        """Whether the last operation ended in failure."""
        return self in {
            OnlineEvaluationStatus.CREATE_FAILED,
            OnlineEvaluationStatus.UPDATE_FAILED,
            OnlineEvaluationStatus.ERROR,
        }


class CalibrationCaseKind(StrEnum):
    """What a fictional calibration case probes, closed to the evaluation-validity steering."""

    KNOWN_CORRECT = "known_correct"
    CONTRADICTION = "contradiction"
    FLUENT_UNSUPPORTED = "fluent_unsupported"
    CLARIFICATION = "clarification"
    OFF_TOPIC = "off_topic"


REQUIRED_CALIBRATION_KINDS: Final = (
    CalibrationCaseKind.KNOWN_CORRECT,
    CalibrationCaseKind.CONTRADICTION,
    CalibrationCaseKind.FLUENT_UNSUPPORTED,
)
"""Every evaluator is calibrated on at least one case of each of these kinds."""


class ExecutionStatus(StrEnum):
    """Whether the service evaluates sessions under a configuration."""

    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class GateAssertion(StrEnum):
    """The closed set of readback assertions; a failure names only these and evaluator names."""

    CONFIG_STATUS = "config_status"
    CONFIG_NAME = "config_name"
    CONFIG_EXECUTION_STATUS = "config_execution_status"
    CONFIG_OUTPUT_GROUP = "config_output_group"
    CONFIG_RESULT_DESTINATION = "config_result_destination"
    CONFIG_DATA_SOURCE = "config_data_source"
    CONFIG_EVALUATORS = "config_evaluators"
    CONFIG_EXECUTION_ROLE = "config_execution_role"
    CONFIG_RULE = "config_rule"
    EVALUATOR_STATUS = "evaluator_status"
    EVALUATOR_NAME = "evaluator_name"
    EVALUATOR_MODEL = "evaluator_model"
    EVALUATOR_PROMPT = "evaluator_prompt"
    GROUP_EXISTS = "group_exists"
    GROUP_ARN = "group_arn"
    GROUP_RETENTION = "group_retention"
    GROUP_DELETION_PROTECTION = "group_deletion_protection"
    GROUP_KEY = "group_key"
    GROUP_DATA_PROTECTION = "group_data_protection"
    SUBSCRIPTION_FILTERS = "subscription_filters"
    ACCOUNT_SUBSCRIPTION_POLICY = "account_subscription_policy"
    ACCOUNT_DATA_PROTECTION_POLICY = "account_data_protection_policy"
    GROUP_TRANSFORMER = "group_transformer"
    ACCOUNT_TRANSFORMER_POLICY = "account_transformer_policy"
    DELIVERY_DESTINATION = "delivery_destination"
    EXPORT_TASK = "export_task"
    RESOURCE_POLICY_WRITE = "resource_policy_write"


@dataclass(frozen=True, slots=True)
class GateFinding:
    """One failed assertion, optionally about one named evaluator."""

    assertion: GateAssertion
    subject: str = ""
    """An evaluator name from the spec, or empty; never a response or configuration value."""

    def __str__(self) -> str:
        return f"{self.assertion}({self.subject})" if self.subject else str(self.assertion)


def _object(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EvaluationGateSpecError(f"{what} must be a JSON object.")
    mapping = cast("Mapping[object, object]", value)
    if not all(isinstance(key, str) for key in mapping):
        raise EvaluationGateSpecError(f"{what} must have text keys.")
    return cast("Mapping[str, object]", mapping)


def _closed(mapping: Mapping[str, object], members: frozenset[str], what: str) -> None:
    unknown = sorted(set(mapping) - members)
    missing = sorted(members - set(mapping))
    if unknown:
        raise EvaluationGateSpecError(f"{what} carries unsupported members: {unknown}.")
    if missing:
        raise EvaluationGateSpecError(f"{what} is missing members: {missing}.")


def _optional_text(mapping: Mapping[str, object], name: str, what: str) -> str:
    value = mapping.get(name)
    if not isinstance(value, str):
        raise EvaluationGateSpecError(f"{what}.{name} must be text.")
    return value


def _text(mapping: Mapping[str, object], name: str, what: str) -> str:
    value = _optional_text(mapping, name, what)
    if not value.strip():
        raise EvaluationGateSpecError(f"{what}.{name} must be non-empty text.")
    return value


def _texts(mapping: Mapping[str, object], name: str, what: str) -> tuple[str, ...]:
    value = mapping.get(name)
    if not isinstance(value, list):
        raise EvaluationGateSpecError(f"{what}.{name} must be a list of text.")
    items = cast("list[object]", value)
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise EvaluationGateSpecError(f"{what}.{name} must be a list of text.")
    return tuple(cast("list[str]", items))


def _integer(mapping: Mapping[str, object], name: str, what: str) -> int:
    value = mapping.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvaluationGateSpecError(f"{what}.{name} must be an integer.")
    return value


def _labels(value: object, where: str) -> tuple[str, ...]:
    raw: list[object]
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, list):
        raw = cast("list[object]", value)
    else:
        raise EvaluationGateSpecError(f"{where} must be a label or a list of labels.")
    if not raw or not all(isinstance(item, str) and item.strip() for item in raw):
        raise EvaluationGateSpecError(f"{where} must name at least one label.")
    labels = tuple(dict.fromkeys(normalize_scale_label(cast("str", item)) for item in raw))
    if not all(_LABEL.fullmatch(label) for label in labels):
        raise EvaluationGateSpecError(f"{where} labels must be bounded scale labels.")
    return labels


@dataclass(frozen=True, slots=True, kw_only=True)
class CalibrationCaseV1:
    """One fictional calibration case and the labels each named evaluator may return."""

    name: str
    kind: CalibrationCaseKind
    question: str
    grounded_context: str
    candidate: str
    expected: Mapping[str, tuple[str, ...]]
    """Evaluator name to its acceptable labels, each already normalized."""

    def __post_init__(self) -> None:
        if not _CASE_NAME.fullmatch(self.name):
            raise EvaluationGateSpecError("calibration case name must be a bounded reference.")
        if not self.question.strip() or not self.candidate.strip():
            raise EvaluationGateSpecError(
                f"calibration case {self.name} needs a question and a candidate."
            )
        if any(
            len(text) > _MAX_CASE_TEXT
            for text in (self.question, self.candidate, self.grounded_context)
        ):
            raise EvaluationGateSpecError(f"calibration case {self.name} text is too long.")
        if not self.expected:
            raise EvaluationGateSpecError(
                f"calibration case {self.name} must expect at least one evaluator."
            )
        if not all(
            labels and all(_LABEL.fullmatch(label) for label in labels)
            for labels in self.expected.values()
        ):
            raise EvaluationGateSpecError(
                f"calibration case {self.name} expectations must be normalized labels."
            )
        object.__setattr__(self, "expected", MappingProxyType(dict(self.expected)))


def _case(value: object, index: int) -> CalibrationCaseV1:
    what = f"GateSpec.calibration[{index}]"
    mapping = _object(value, what)
    _closed(mapping, _CASE_MEMBERS, what)
    expected = _object(mapping["expected"], f"{what}.expected")
    kind_text = _text(mapping, "kind", what)
    try:
        kind = CalibrationCaseKind(kind_text)
    except ValueError:
        kinds = [str(item) for item in CalibrationCaseKind]
        raise EvaluationGateSpecError(f"{what}.kind must be one of {kinds}.") from None
    return CalibrationCaseV1(
        name=_text(mapping, "name", what),
        kind=kind,
        question=_text(mapping, "question", what),
        grounded_context=_optional_text(mapping, "grounded_context", what),
        candidate=_text(mapping, "candidate", what),
        expected={
            evaluator: _labels(labels, f"{what}.expected.{evaluator}")
            for evaluator, labels in expected.items()
        },
    )


def _evaluator(value: object, index: int) -> EvaluationReleaseEvaluatorV1:
    what = f"GateSpec.evaluators[{index}]"
    mapping = _object(value, what)
    _closed(mapping, _EVALUATOR_MEMBERS, what)
    try:
        return EvaluationReleaseEvaluatorV1(
            evaluator_id=_text(mapping, "evaluator_id", what),
            evaluator_name=_text(mapping, "evaluator_name", what),
            revision=_text(mapping, "revision", what),
            model_id=_text(mapping, "model_id", what),
            prompt_digest=_text(mapping, "prompt_digest", what),
        )
    except EvaluationGateSpecError:
        raise
    except ValueError as error:
        raise EvaluationGateSpecError(f"{what}: {error}") from None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EvaluationGateSpecError(message)


@dataclass(frozen=True, slots=True)
class ResultGroupCustodyV1:
    """What the result-group readback asserts against. A GateSpec carries exactly this."""

    result_log_group_name: str
    result_log_group_arn: str
    result_retention_days: int
    ingress_subscription_filter_name: str
    ingress_function_arn: str

    def __post_init__(self) -> None:
        # GateSpec's own result-group predicates, so the probe's custody value is refused wherever
        # a GateSpec would refuse those five fields. The one GateSpec check it cannot repeat is
        # "not an input log group": it carries no inputs, and the deployed GateSpec was already
        # validated by the gate provider when it was parsed.
        _require(
            _LOG_GROUP_NAME.fullmatch(self.result_log_group_name) is not None,
            "custody group name must be a log group name.",
        )
        _require(
            not self.result_log_group_name.startswith(RESERVED_RESULT_GROUP_PREFIX),
            f"custody group name must be outside {RESERVED_RESULT_GROUP_PREFIX}.",
        )
        prefix = _LOG_GROUP_ARN_PREFIX.match(self.result_log_group_arn)
        # Exact remainder, not substring: the ARN of "/a/bc" must not pass for the name "/a/b",
        # and a trailing ":*" is refused, as GateSpec refuses it.
        _require(
            prefix is not None
            and self.result_log_group_arn[prefix.end() :] == self.result_log_group_name,
            "custody group ARN must be the group's ARN without ':*'.",
        )
        _require(
            self.result_retention_days in _RETENTION_DAYS,
            "custody retention must be a CloudWatch Logs retention value.",
        )
        _require(
            _FILTER_NAME.fullmatch(self.ingress_subscription_filter_name) is not None,
            "custody filter name must be a subscription filter name.",
        )
        _require(
            _FUNCTION_ARN.fullmatch(self.ingress_function_arn) is not None,
            "custody ingress must be an unqualified Lambda function ARN.",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationGateSpecV1:
    """One family's validated desired gate state."""

    family: EvaluationFamilyV1
    config_name: str
    description: str
    execution_requested: bool
    execution_role_arn: str
    input_log_group_names: tuple[str, ...]
    input_service_names: tuple[str, ...]
    sampling_percentage: float
    session_timeout_minutes: int
    result_log_group_name: str
    result_log_group_arn: str
    """The group ARN without the `:*` suffix DescribeLogGroups' `arn` member carries."""
    result_retention_days: int
    ingress_subscription_filter_name: str
    ingress_function_arn: str
    release_parameter_name: str
    evaluators: tuple[EvaluationReleaseEvaluatorV1, ...]
    calibration: tuple[CalibrationCaseV1, ...]
    tags: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    """The tags a create carries. IAM refuses an untagged create (H-IAM-AGENTCORE-CREATE-001), so
    an empty mapping fails closed; an Update never sends them, the service having no member."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", MappingProxyType(dict(self.tags)))
        self._validate_configuration()
        self._validate_result_group()
        self._validate_evaluators()
        self._validate_calibration()

    def custody(self) -> ResultGroupCustodyV1:
        """Return the five result-group fields the custody readback asserts against."""
        return ResultGroupCustodyV1(
            result_log_group_name=self.result_log_group_name,
            result_log_group_arn=self.result_log_group_arn,
            result_retention_days=self.result_retention_days,
            ingress_subscription_filter_name=self.ingress_subscription_filter_name,
            ingress_function_arn=self.ingress_function_arn,
        )

    def _validate_configuration(self) -> None:
        _require(
            self.family in CURRENT_EVALUATION_FAMILIES,
            f"GateSpec.family {self.family} is retired; only a current family has a gate.",
        )
        _require(
            _CONFIG_NAME.fullmatch(self.config_name) is not None,
            "GateSpec.config_name must match [a-zA-Z][a-zA-Z0-9_]{0,47}.",
        )
        _require(
            bool(self.description.strip()) and len(self.description) <= _MAX_DESCRIPTION,
            "GateSpec.description must be 1-200 characters.",
        )
        _require(
            _ROLE_ARN.fullmatch(self.execution_role_arn) is not None,
            "GateSpec.execution_role_arn must be an IAM role ARN.",
        )
        _require(
            0 < len(self.input_log_group_names) <= _MAX_INPUT_GROUPS
            and all(_LOG_GROUP_NAME.fullmatch(name) for name in self.input_log_group_names)
            and len(set(self.input_log_group_names)) == len(self.input_log_group_names),
            "GateSpec.input_log_group_names must name 1-10 distinct log groups.",
        )
        _require(
            len(self.input_service_names) == 1
            and _SERVICE_NAME.fullmatch(self.input_service_names[0]) is not None,
            "GateSpec.input_service_names must name exactly one service.",
        )
        _require(
            math.isfinite(self.sampling_percentage)
            and 0 < self.sampling_percentage <= _MAX_SAMPLING_PERCENTAGE,
            "GateSpec.sampling_percentage must be in (0, 100].",
        )
        _require(
            1 <= self.session_timeout_minutes <= _MAX_SESSION_TIMEOUT_MINUTES,
            "GateSpec.session_timeout_minutes must be 1-1440.",
        )
        _require(
            _PARAMETER_PATH.fullmatch(self.release_parameter_name) is not None
            and self.release_parameter_name.endswith(f"/release-{self.family}"),
            f"GateSpec.release_parameter_name must be a path ending in /release-{self.family}.",
        )
        _require(len(self.tags) <= _MAX_TAGS, f"GateSpec.tags must hold at most {_MAX_TAGS}.")
        _require(
            all(
                _TAG_KEY.fullmatch(key) is not None
                and _TAG_VALUE.fullmatch(value) is not None
                and not key.lower().startswith("aws:")
                for key, value in self.tags.items()
            ),
            "GateSpec.tags keys must be 1-128 and values 0-256 tag characters, with no aws: key.",
        )

    def _validate_result_group(self) -> None:
        group = self.result_log_group_name
        _require(
            _LOG_GROUP_NAME.fullmatch(group) is not None,
            "GateSpec.result_log_group_name must be a log group name.",
        )
        _require(
            not group.startswith(RESERVED_RESULT_GROUP_PREFIX),
            f"GateSpec.result_log_group_name must be outside {RESERVED_RESULT_GROUP_PREFIX}.",
        )
        _require(
            group not in self.input_log_group_names,
            "GateSpec.result_log_group_name must not be an input log group.",
        )
        prefix = _LOG_GROUP_ARN_PREFIX.match(self.result_log_group_arn)
        _require(
            prefix is not None and self.result_log_group_arn[prefix.end() :] == group,
            "GateSpec.result_log_group_arn must be the result group's ARN without ':*'.",
        )
        _require(
            self.result_retention_days in _RETENTION_DAYS,
            "GateSpec.result_retention_days must be a CloudWatch Logs retention value.",
        )
        _require(
            _FILTER_NAME.fullmatch(self.ingress_subscription_filter_name) is not None,
            "GateSpec.ingress_subscription_filter_name must be a subscription filter name.",
        )
        _require(
            _FUNCTION_ARN.fullmatch(self.ingress_function_arn) is not None,
            "GateSpec.ingress_function_arn must be an unqualified Lambda function ARN.",
        )

    def _validate_evaluators(self) -> None:
        _require(
            0 < len(self.evaluators) <= _MAX_EVALUATORS,
            "GateSpec.evaluators must name 1-25 evaluators.",
        )
        try:
            require_unique_evaluators(self.evaluators, "GateSpec")
        except ValueError as error:
            raise EvaluationGateSpecError(str(error)) from None
        names = [evaluator.evaluator_name for evaluator in self.evaluators]
        _require(len(set(names)) == len(names), "GateSpec evaluator names must be unique.")

    def _validate_calibration(self) -> None:
        names = [case.name for case in self.calibration]
        _require(len(set(names)) == len(names), "GateSpec calibration case names must be unique.")
        declared = {evaluator.evaluator_name for evaluator in self.evaluators}
        unknown = sorted({name for case in self.calibration for name in case.expected} - declared)
        _require(
            not unknown,
            f"GateSpec calibration expects evaluators the spec does not declare: {unknown}.",
        )
        # agent-evaluation-validity: an evaluator is never enabled on absent calibration evidence.
        short = sorted(
            name
            for name in declared
            if sum(name in case.expected for case in self.calibration) < MIN_CALIBRATION_CASES
        )
        _require(
            not short,
            f"GateSpec calibration needs at least {MIN_CALIBRATION_CASES} cases for each "
            f"evaluator; short: {short}.",
        )
        uncovered = sorted(
            f"{name}:{kind}"
            for name in declared
            for kind in REQUIRED_CALIBRATION_KINDS
            if not any(case.kind is kind and name in case.expected for case in self.calibration)
        )
        _require(
            not uncovered,
            "GateSpec calibration needs a known_correct, a contradiction, and a "
            f"fluent_unsupported case for each evaluator; missing: {uncovered}.",
        )

    @property
    def evaluator_ids(self) -> tuple[str, ...]:
        """The configured evaluator identities, in spec order."""
        return tuple(evaluator.evaluator_id for evaluator in self.evaluators)


def parse_gate_spec(text: object) -> EvaluationGateSpecV1:
    """Parse the `GateSpec` property text into a validated spec."""
    if not isinstance(text, str) or not text.strip():
        raise EvaluationGateSpecError(f"{SPEC_PROPERTY} must be a non-empty JSON string.")
    try:
        document: object = json.loads(text)
    except ValueError:
        # The parser's message quotes the document, which holds calibration text.
        raise EvaluationGateSpecError(f"{SPEC_PROPERTY} is not valid JSON.") from None
    mapping = _object(document, SPEC_PROPERTY)
    # The optional members are exempt from both the unknown and the missing check.
    _closed(
        {key: value for key, value in mapping.items() if key not in _OPTIONAL_SPEC_MEMBERS},
        _SPEC_MEMBERS,
        SPEC_PROPERTY,
    )
    if mapping["schema_version"] != GATE_SPEC_SCHEMA:
        raise EvaluationGateSpecError(f"{SPEC_PROPERTY}.schema_version must be {GATE_SPEC_SCHEMA}.")
    try:
        family = EvaluationFamilyV1(_text(mapping, "family", SPEC_PROPERTY))
    except ValueError:
        raise EvaluationGateSpecError(f"{SPEC_PROPERTY}.family is not a known family.") from None
    requested = mapping["execution_requested"]
    if not isinstance(requested, bool):
        raise EvaluationGateSpecError(f"{SPEC_PROPERTY}.execution_requested must be a boolean.")
    sampling = mapping["sampling_percentage"]
    if isinstance(sampling, bool) or not isinstance(sampling, int | float):
        raise EvaluationGateSpecError(f"{SPEC_PROPERTY}.sampling_percentage must be a number.")
    evaluators = mapping["evaluators"]
    calibration = mapping["calibration"]
    if not isinstance(evaluators, list) or not isinstance(calibration, list):
        raise EvaluationGateSpecError(f"{SPEC_PROPERTY}.evaluators and .calibration must be lists.")
    return EvaluationGateSpecV1(
        family=family,
        config_name=_text(mapping, "config_name", SPEC_PROPERTY),
        description=_text(mapping, "description", SPEC_PROPERTY),
        execution_requested=requested,
        execution_role_arn=_text(mapping, "execution_role_arn", SPEC_PROPERTY),
        input_log_group_names=_texts(mapping, "input_log_group_names", SPEC_PROPERTY),
        input_service_names=_texts(mapping, "input_service_names", SPEC_PROPERTY),
        sampling_percentage=float(sampling),
        session_timeout_minutes=_integer(mapping, "session_timeout_minutes", SPEC_PROPERTY),
        result_log_group_name=_text(mapping, "result_log_group_name", SPEC_PROPERTY),
        result_log_group_arn=_text(mapping, "result_log_group_arn", SPEC_PROPERTY),
        result_retention_days=_integer(mapping, "result_retention_days", SPEC_PROPERTY),
        ingress_subscription_filter_name=_text(
            mapping, "ingress_subscription_filter_name", SPEC_PROPERTY
        ),
        ingress_function_arn=_text(mapping, "ingress_function_arn", SPEC_PROPERTY),
        release_parameter_name=_text(mapping, "release_parameter_name", SPEC_PROPERTY),
        evaluators=tuple(
            _evaluator(item, index) for index, item in enumerate(cast("list[object]", evaluators))
        ),
        calibration=tuple(
            _case(item, index) for index, item in enumerate(cast("list[object]", calibration))
        ),
        tags=_tags(mapping),
    )


def _tags(mapping: Mapping[str, object]) -> Mapping[str, str]:
    """Read the optional `tags` member; absent is empty, the form a pre-`tags` rollback sends."""
    if "tags" not in mapping:
        return {}
    tags = _object(mapping["tags"], f"{SPEC_PROPERTY}.tags")
    if not all(isinstance(value, str) for value in tags.values()):
        raise EvaluationGateSpecError(f"{SPEC_PROPERTY}.tags values must be text.")
    return cast("Mapping[str, str]", tags)


def _properties(value: object, what: str) -> EvaluationGateSpecV1:
    properties = _object(value, what)
    unknown = sorted(set(properties) - _SERVICE_PROPERTIES - _RESOURCE_PROPERTIES)
    if unknown:
        raise EvaluationGateSpecError(f"{what} carries unsupported properties: {unknown}.")
    digest = properties.get(DIGEST_PROPERTY)
    if digest is not None and not isinstance(digest, str):
        raise EvaluationGateSpecError(f"{what}.{DIGEST_PROPERTY} must be text.")
    return parse_gate_spec(properties.get(SPEC_PROPERTY))


def _event_text(event: Mapping[str, object], name: str) -> str:
    value = event.get(name)
    if not isinstance(value, str) or not value.strip():
        raise EvaluationGateSpecError(f"The callback has no {name}.")
    return value


@dataclass(frozen=True, slots=True)
class GateCallbackEvent:
    """One validated custom-resource callback.

    `OldResourceProperties` is not parsed: whether an Update replaces the configuration is decided
    by the live configuration's name, which also holds on a rollback, where the old properties are
    the failed ones.
    """

    request_type: GateRequestType
    request_id: str
    stack_id: str
    logical_resource_id: str
    physical_resource_id: str
    """Empty for Create; otherwise the id this provider returned earlier."""
    spec: EvaluationGateSpecV1


def parse_gate_event(event: object) -> GateCallbackEvent:
    """Validate a CloudFormation callback and parse its stringified properties."""
    mapping = _object(event, "The callback event")
    try:
        request_type = GateRequestType(_event_text(mapping, "RequestType"))
    except ValueError:
        raise EvaluationGateSpecError("RequestType is outside Create, Update and Delete.") from None
    physical = (
        "" if request_type is GateRequestType.CREATE else _event_text(mapping, "PhysicalResourceId")
    )
    return GateCallbackEvent(
        request_type=request_type,
        request_id=_event_text(mapping, "RequestId"),
        stack_id=_event_text(mapping, "StackId"),
        logical_resource_id=_event_text(mapping, "LogicalResourceId"),
        physical_resource_id=physical,
        spec=_properties(mapping.get("ResourceProperties"), "ResourceProperties"),
    )


def client_token(event: GateCallbackEvent, operation: str) -> str:
    """Derive the idempotency token for one control-plane mutation of one callback.

    CloudFormation keeps the RequestId when it retries a callback, so a retried Create names the
    configuration the first attempt started rather than creating a second one.
    """
    parts = (event.stack_id, event.logical_resource_id, event.request_id, operation)
    material = "\n".join((_CLIENT_TOKEN_LABEL, *parts))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def is_config_id(value: str) -> bool:
    """Whether a physical id names a configuration rather than a failed create's placeholder."""
    return _CONFIG_ID.fullmatch(value) is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class OnlineEvaluationConfigState:
    """What GetOnlineEvaluationConfig reports about one configuration, projected for the gate."""

    config_id: str
    config_name: str
    status: OnlineEvaluationStatus
    execution_status: ExecutionStatus
    failure_reason: str = ""
    description: str = ""
    output_log_group_name: str = ""
    result_destination: str = ""
    input_log_group_names: tuple[str, ...] = ()
    input_log_group_prefixes: tuple[str, ...] = ()
    input_service_names: tuple[str, ...] = ()
    evaluator_ids: tuple[str, ...] = ()
    execution_role_arn: str = ""
    sampling_percentage: float = 0.0
    session_timeout_minutes: int = 0
    filter_count: int = 0


_DEDICATED_RESULT_DESTINATIONS: Final = frozenset({"", "DEDICATED_LOG_GROUP"})
"""An absent destination or the dedicated group; `SOURCE_LOG_GROUP` would write to the input."""


def binding_findings(
    spec: EvaluationGateSpecV1,
    state: OnlineEvaluationConfigState,
    execution: ExecutionStatus,
) -> tuple[GateFinding, ...]:
    """Assert the configuration is exactly the spec's, in the expected execution state."""
    checks: tuple[tuple[GateAssertion, bool], ...] = (
        (GateAssertion.CONFIG_STATUS, state.status is OnlineEvaluationStatus.ACTIVE),
        (GateAssertion.CONFIG_NAME, state.config_name == spec.config_name),
        (GateAssertion.CONFIG_EXECUTION_STATUS, state.execution_status is execution),
        (
            GateAssertion.CONFIG_OUTPUT_GROUP,
            state.output_log_group_name == spec.result_log_group_name,
        ),
        (
            GateAssertion.CONFIG_RESULT_DESTINATION,
            state.result_destination in _DEDICATED_RESULT_DESTINATIONS,
        ),
        (
            GateAssertion.CONFIG_DATA_SOURCE,
            sorted(state.input_log_group_names) == sorted(spec.input_log_group_names)
            and not state.input_log_group_prefixes
            and sorted(state.input_service_names) == sorted(spec.input_service_names),
        ),
        (
            GateAssertion.CONFIG_EVALUATORS,
            sorted(state.evaluator_ids) == sorted(spec.evaluator_ids),
        ),
        (GateAssertion.CONFIG_EXECUTION_ROLE, state.execution_role_arn == spec.execution_role_arn),
        (
            GateAssertion.CONFIG_RULE,
            abs(state.sampling_percentage - spec.sampling_percentage) <= _SAMPLING_TOLERANCE
            and state.session_timeout_minutes == spec.session_timeout_minutes
            and state.filter_count == 0,
        ),
    )
    return tuple(GateFinding(assertion) for assertion, holds in checks if not holds)


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluatorState:
    """What GetEvaluator reports about one evaluator, with its prompt reduced to a digest."""

    evaluator_id: str
    evaluator_name: str
    status: str
    model_id: str
    """Empty when the evaluator is not a Bedrock model judge."""
    prompt_digest: str
    """SHA-256 hex of the judge instructions as the service stores them; empty when absent."""


def evaluator_findings(
    spec: EvaluationGateSpecV1, states: Iterable[EvaluatorState]
) -> tuple[GateFinding, ...]:
    """Assert every live evaluator is the one the release record will claim."""
    live = {state.evaluator_id: state for state in states}
    findings: list[GateFinding] = []
    for evaluator in spec.evaluators:
        state = live.get(evaluator.evaluator_id)
        name = evaluator.evaluator_name
        if state is None or state.status != "ACTIVE":
            findings.append(GateFinding(GateAssertion.EVALUATOR_STATUS, name))
            continue
        if state.evaluator_name != name:
            findings.append(GateFinding(GateAssertion.EVALUATOR_NAME, name))
        if state.model_id != evaluator.model_id:
            findings.append(GateFinding(GateAssertion.EVALUATOR_MODEL, name))
        if state.prompt_digest != evaluator.prompt_digest:
            findings.append(GateFinding(GateAssertion.EVALUATOR_PROMPT, name))
    return tuple(findings)


def release_not_enabled(family: EvaluationFamilyV1, decided_at: str) -> EvaluationReleaseV1:
    """Build the record that sends nothing to the family."""
    return EvaluationReleaseV1(
        family=family, enabled=False, config_id=None, evaluators=(), decided_at=decided_at
    )


def release_enabled(
    spec: EvaluationGateSpecV1, config_id: str, decided_at: str
) -> EvaluationReleaseV1:
    """Build the record that names the configuration and the calibrated evaluator set."""
    return EvaluationReleaseV1(
        family=spec.family,
        enabled=True,
        config_id=config_id,
        evaluators=spec.evaluators,
        decided_at=decided_at,
    )


def release_matches(
    record: EvaluationReleaseV1 | None, spec: EvaluationGateSpecV1, config_id: str
) -> bool:
    """Whether a stored record already enables exactly this configuration and evaluator set.

    The decision time is not compared: it records when the gate decided, not what it decided.
    """
    return (
        record is not None
        and record.enabled
        and record.family is spec.family
        and record.config_id == config_id
        and record.evaluators == spec.evaluators
    )


@dataclass(frozen=True, slots=True)
class CalibrationInputV1:
    """One Evaluate call: one fictional case judged by one evaluator."""

    case_name: str
    evaluator_name: str
    evaluator_id: str
    envelope: EvaluationEnvelope
    """The case as a released-response envelope, so the span serializer is the only span shape."""
    trace_id: str
    span_id: str


def _uuid4_shape(digest: str) -> str:
    """Render 32 hex characters as a canonical UUID4, which the envelope requires."""
    hexes = list(digest[:32])
    hexes[12] = "4"
    hexes[16] = _UUID_VARIANTS[int(hexes[16], 16) % len(_UUID_VARIANTS)]
    text = "".join(hexes)
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def _calibration_input(
    spec: EvaluationGateSpecV1, case: CalibrationCaseV1, evaluator: EvaluationReleaseEvaluatorV1
) -> CalibrationInputV1:
    material = "\n".join((_CALIBRATION_LABEL, spec.family, case.name, evaluator.evaluator_id))
    identity = hashlib.sha256(material.encode("utf-8")).hexdigest()
    trace = hashlib.sha256(f"{material}\ntrace".encode()).hexdigest()
    return CalibrationInputV1(
        case_name=case.name,
        evaluator_name=evaluator.evaluator_name,
        evaluator_id=evaluator.evaluator_id,
        envelope=EvaluationEnvelope(
            question=case.question,
            released_answer=case.candidate,
            correlation_id=_uuid4_shape(identity),
            audit_id=f"calibration-{identity[:24]}",
            evaluation_admission_ref=f"calibration-{identity[24:48]}",
            grounded_context=case.grounded_context,
        ),
        trace_id=trace[:32],
        span_id=trace[32:48],
    )


def calibration_inputs(spec: EvaluationGateSpecV1) -> tuple[CalibrationInputV1, ...]:
    """Every (case, evaluator) pair a case declares an expectation for, in spec order."""
    return tuple(
        _calibration_input(spec, case, evaluator)
        for case in spec.calibration
        for evaluator in spec.evaluators
        if evaluator.evaluator_name in case.expected
    )


@dataclass(frozen=True, slots=True)
class CalibrationObservationV1:
    """What one Evaluate call returned for one pair: its labels, or a service error code."""

    case_name: str
    evaluator_name: str
    labels: tuple[str, ...] = ()
    """Every label the evaluator returned for the case, as the service spelled it."""
    error_code: str = ""
    """The per-result error code the service returned; never its message."""


class CalibrationFailureKind(StrEnum):
    """Why one expected outcome was not met."""

    MISSING = "missing"
    ERROR = "error"
    NO_LABEL = "no_label"
    UNEXPECTED_LABEL = "unexpected_label"


@dataclass(frozen=True, slots=True)
class CalibrationFailureV1:
    """One unmet expectation, naming only the case, the evaluator, and scale labels."""

    case_name: str
    evaluator_name: str
    kind: CalibrationFailureKind
    expected: tuple[str, ...]
    observed: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"{self.case_name}/{self.evaluator_name}:{self.kind}"


@dataclass(frozen=True, slots=True)
class CalibrationOutcomeV1:
    """The verdict over a whole corpus; it passes only when no expectation failed."""

    check_count: int
    failures: tuple[CalibrationFailureV1, ...]

    @property
    def passed(self) -> bool:
        """Whether every declared expectation was met, and at least one was declared."""
        return self.check_count > 0 and not self.failures


def _judged(
    expected: tuple[str, ...], observation: CalibrationObservationV1 | None
) -> tuple[CalibrationFailureKind, tuple[str, ...]] | None:
    if observation is None:
        return CalibrationFailureKind.MISSING, ()
    if observation.error_code:
        return CalibrationFailureKind.ERROR, ()
    observed = tuple(normalize_scale_label(label) for label in observation.labels)
    if not observed:
        return CalibrationFailureKind.NO_LABEL, ()
    if any(label not in expected for label in observed):
        return CalibrationFailureKind.UNEXPECTED_LABEL, observed
    return None


def calibration_outcome(
    spec: EvaluationGateSpecV1, observations: Iterable[CalibrationObservationV1]
) -> CalibrationOutcomeV1:
    """Judge every (case, evaluator) expectation against what the evaluators returned.

    An expectation passes only when a result arrived with no error and every returned label, after
    normalization, is one the case accepts. A missing or errored result fails; it is never read as
    a pass.
    """
    observed = {(item.case_name, item.evaluator_name): item for item in observations}
    failures: list[CalibrationFailureV1] = []
    checks = 0
    for case in spec.calibration:
        for evaluator in spec.evaluators:
            expected = case.expected.get(evaluator.evaluator_name)
            if expected is None:
                continue
            checks += 1
            verdict = _judged(expected, observed.get((case.name, evaluator.evaluator_name)))
            if verdict is not None:
                kind, labels = verdict
                failures.append(
                    CalibrationFailureV1(
                        case_name=case.name,
                        evaluator_name=evaluator.evaluator_name,
                        kind=kind,
                        expected=expected,
                        observed=labels,
                    )
                )
    return CalibrationOutcomeV1(check_count=checks, failures=tuple(failures))
