# SPDX-License-Identifier: MIT-0
"""Evaluation release records: domain invariants and the dispatcher's Parameter Store reader."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError

import adapters.outbound.ssm.evaluation_release as reader_module
import domain.evaluation.release as release_module
from adapters.inbound.conversation.contract_codec import contract_to_json
from adapters.outbound.ssm.evaluation_release import SsmEvaluationReleaseReader
from domain.evaluation.release import (
    CURRENT_EVALUATION_FAMILIES,
    EvaluationFamilyV1,
    EvaluationReleaseEvaluatorV1,
    EvaluationReleaseV1,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-079")]

_RETIRED_V3_NAME = "/adlytics/dev/evaluation/release-v3"
_V4_NAME = "/adlytics/dev/evaluation/release-v4"
_AT = "2026-09-27T00:00:00+00:00"


def _evaluator(evaluator_id: str = "fictional-helpful-01") -> EvaluationReleaseEvaluatorV1:
    return EvaluationReleaseEvaluatorV1(
        evaluator_id=evaluator_id,
        evaluator_name="fictional_helpful",
        revision="r1",
        model_id="fictional.judge-model-v1:0",
        prompt_digest="c" * 64,
    )


def _enabled(family: EvaluationFamilyV1 = EvaluationFamilyV1.V4) -> EvaluationReleaseV1:
    return EvaluationReleaseV1(
        family=family,
        enabled=True,
        config_id=f"fictional_released_{family.value}-0001",
        evaluators=(_evaluator(),),
        decided_at=_AT,
    )


def _disabled(family: EvaluationFamilyV1 = EvaluationFamilyV1.V4) -> EvaluationReleaseV1:
    return EvaluationReleaseV1(
        family=family, enabled=False, config_id=None, evaluators=(), decided_at=_AT
    )


# --- Domain invariants -----------------------------------------------------------------------


def test_v4_is_the_only_current_family() -> None:
    """ADR-079: V3 was retired 2026-09-27; its value survives only to decode stored records."""
    assert CURRENT_EVALUATION_FAMILIES == (EvaluationFamilyV1.V4,)
    assert EvaluationFamilyV1("v3") is EvaluationFamilyV1.V3


@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "disabled"])
def test_a_release_refuses_the_retired_v3_family(enabled: bool) -> None:
    with pytest.raises(ValueError, match="retired evaluation family v3"):
        EvaluationReleaseV1(
            family=EvaluationFamilyV1.V3,
            enabled=enabled,
            config_id="fictional_released_v3-0001" if enabled else None,
            evaluators=(_evaluator(),) if enabled else (),
            decided_at=_AT,
        )


def test_an_enabled_release_names_its_configuration_and_at_least_one_evaluator() -> None:
    assert _enabled().config_id == "fictional_released_v4-0001"
    with pytest.raises(ValueError, match="requires its configuration and evaluators"):
        replace(_enabled(), config_id=None)
    with pytest.raises(ValueError, match="requires its configuration and evaluators"):
        replace(_enabled(), evaluators=())


def test_a_disabled_release_names_neither_a_configuration_nor_evaluators() -> None:
    """A disabled record carrying an evaluator set could leak a stale set forward."""
    assert _disabled().evaluators == ()
    with pytest.raises(ValueError, match="cannot name a configuration or evaluators"):
        replace(_disabled(), config_id="fictional_released_v4-0001")
    with pytest.raises(ValueError, match="cannot name a configuration or evaluators"):
        replace(_disabled(), evaluators=(_evaluator(),))


def test_a_release_refuses_the_same_evaluator_twice() -> None:
    with pytest.raises(ValueError, match="evaluator IDs must be unique"):
        replace(_enabled(), evaluators=(_evaluator(), _evaluator()))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"prompt_digest": "C" * 64}, "64 lowercase hex"),
        ({"prompt_digest": "c" * 63}, "64 lowercase hex"),
        ({"evaluator_id": "has#hash"}, "opaque bounded reference"),
        ({"revision": ""}, "opaque bounded reference"),
        ({"model_id": "model id with spaces"}, "bounded model identifier"),
    ],
    ids=["upper-digest", "short-digest", "hash-in-id", "blank-revision", "spaced-model"],
)
def test_a_released_evaluator_refuses_a_malformed_provenance_field(
    changes: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_evaluator(), **changes)


def test_a_released_evaluator_accepts_an_inference_profile_arn_as_its_model() -> None:
    model = "arn:aws:bedrock:us-east-1:111122223333:inference-profile/fictional.judge-v1:0"

    assert replace(_evaluator(), model_id=model).model_id == model


@pytest.mark.parametrize("decided_at", ["2026-09-27T00:00:00", "2026-09-27T02:00:00+02:00"])
def test_a_release_decision_time_must_be_a_utc_instant(decided_at: str) -> None:
    with pytest.raises(ValueError, match="UTC ISO instant"):
        replace(_disabled(), decided_at=decided_at)


# --- Parameter Store reader ------------------------------------------------------------------


@dataclass
class _RecordingSsm:
    """Returns one scripted GetParameters response; never a fake of Parameter Store."""

    response: dict[str, Any] = field(default_factory=dict[str, Any])
    failure: Exception | None = None
    requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def get_parameters(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return self.response


def _parameter(name: str, value: str) -> dict[str, Any]:
    return {"Name": name, "Type": "String", "Value": value, "Version": 1}


def _reader(client: _RecordingSsm) -> SsmEvaluationReleaseReader:
    return SsmEvaluationReleaseReader((_V4_NAME,), cast("Any", client))


def test_the_reader_reads_every_family_in_one_undecrypted_get_parameters_call() -> None:
    client = _RecordingSsm(
        response={
            "Parameters": [_parameter(_V4_NAME, contract_to_json(_enabled()))],
            "InvalidParameters": [],
        }
    )

    releases = _reader(client).read()

    assert releases == (_enabled(),)
    assert client.requests == [{"Names": [_V4_NAME], "WithDecryption": False}]


def test_a_parameter_that_does_not_exist_reads_as_not_enabled() -> None:
    client = _RecordingSsm(response={"Parameters": [], "InvalidParameters": [_V4_NAME]})

    assert _reader(client).read() == (None,)


def _two_current_families(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in a successor family beside V4; ADR-079 keeps the per-family mechanism for it."""
    both = (EvaluationFamilyV1.V3, EvaluationFamilyV1.V4)
    monkeypatch.setattr(release_module, "CURRENT_EVALUATION_FAMILIES", both)
    monkeypatch.setattr(reader_module, "CURRENT_EVALUATION_FAMILIES", both)


def test_with_two_current_families_the_reader_returns_each_in_configured_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two_current_families(monkeypatch)
    other = _enabled(EvaluationFamilyV1.V3)
    client = _RecordingSsm(
        response={
            "Parameters": [
                _parameter(_V4_NAME, contract_to_json(_disabled())),
                _parameter(_RETIRED_V3_NAME, contract_to_json(other)),
            ],
            "InvalidParameters": [],
        }
    )

    releases = SsmEvaluationReleaseReader((_RETIRED_V3_NAME, _V4_NAME), cast("Any", client)).read()

    assert releases == (other, _disabled())


def test_with_two_current_families_a_record_under_the_other_familys_name_fails_the_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A V4 record under another family's name would send that family's turns to V4's set."""
    _two_current_families(monkeypatch)
    client = _RecordingSsm(
        response={
            "Parameters": [_parameter(_RETIRED_V3_NAME, contract_to_json(_enabled()))],
            "InvalidParameters": [],
        }
    )

    with pytest.raises(InvariantError, match="names another family"):
        SsmEvaluationReleaseReader((_RETIRED_V3_NAME,), cast("Any", client)).read()


def test_a_record_naming_the_retired_family_under_a_current_name_fails_the_read_closed() -> None:
    """A V3 record under release-v4 would send V4 turns to a retired evaluator set."""
    retired = contract_to_json(_enabled()).replace('"family":"v4"', '"family":"v3"')
    assert '"family":"v3"' in retired
    client = _RecordingSsm(
        response={"Parameters": [_parameter(_V4_NAME, retired)], "InvalidParameters": []}
    )

    with pytest.raises(InvariantError, match="malformed") as raised:
        _reader(client).read()

    assert raised.value.code == ErrorCode("EVALUATION", 7)


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        "[]",
        contract_to_json(_enabled()).replace("EvaluationRelease.v1", "EvaluationRelease.v2"),
        contract_to_json(_enabled()).replace('"enabled":true', '"enabled":true,"extra":1'),
        contract_to_json(_enabled()).replace(
            '"config_id":"fictional_released_v4-0001"', '"config_id":null'
        ),
    ],
    ids=["not-json", "not-object", "other-version", "unknown-field", "enabled-without-config"],
)
def test_a_record_that_does_not_decode_strictly_fails_the_read_closed(value: str) -> None:
    client = _RecordingSsm(
        response={"Parameters": [_parameter(_V4_NAME, value)], "InvalidParameters": []}
    )

    with pytest.raises(InvariantError, match="malformed") as raised:
        _reader(client).read()

    assert raised.value.code == ErrorCode("EVALUATION", 7)


def test_a_response_that_neither_returns_nor_disowns_a_name_fails_the_read() -> None:
    client = _RecordingSsm(response={"Parameters": [], "InvalidParameters": []})

    with pytest.raises(InvariantError, match="neither returned nor missing"):
        _reader(client).read()


def test_a_get_parameters_failure_is_a_typed_dependency_error() -> None:
    client = _RecordingSsm(
        failure=ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetParameters")
    )

    with pytest.raises(DependencyError) as raised:
        _reader(client).read()

    assert raised.value.code == ErrorCode("EVALUATION", 6)
    assert raised.value.context == {"cause": "ClientError"}


@pytest.mark.parametrize(
    "names",
    [
        (),
        ("/adlytics/dev/evaluation/release-v5",),
        (_RETIRED_V3_NAME,),
        ("/adlytics/dev/evaluation/results-v4",),
        ("adlytics/dev/evaluation/release-v4",),
        (_V4_NAME, "/adlytics/other/evaluation/release-v4"),
    ],
    ids=["none", "unknown-family", "retired-family", "not-a-release", "relative", "family-twice"],
)
def test_the_reader_refuses_parameter_names_that_do_not_name_each_family_once(
    names: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="evaluation release parameter"):
        SsmEvaluationReleaseReader(names, cast("Any", _RecordingSsm()))
