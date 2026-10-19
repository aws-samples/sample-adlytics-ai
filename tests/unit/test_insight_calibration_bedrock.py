# SPDX-License-Identifier: MIT-0
"""Generate-then-judge pipeline and decode seams of the Bedrock insight-calibration runner."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import pytest
from botocore.exceptions import ClientError

from adapters.outbound.bedrock import insight_calibration as calibration
from adapters.outbound.bedrock.insight_calibration import BedrockInsightCalibrationRunner
from adapters.outbound.bedrock.insights import InsightModelRouteModels
from domain.presentation.insights import (
    DeterministicFinding,
    InsightCalibrationCase,
    InsightCalibrationDraft,
    InsightCalibrationKind,
    InsightCalibrationOutcome,
    InsightModelRoute,
    InsightOutputClass,
    InsightPromptCalibrationRunRequest,
    InsightPromptTier,
    InsightSlotId,
    decimal_figure,
)
from domain.shared_kernel.errors import DependencyError, InvariantError

if TYPE_CHECKING:
    from types_boto3_bedrock_runtime.client import BedrockRuntimeClient

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-069", "ADR-076", "ADR-082")]

_CANDIDATE_PROMPT = "A reviewed fictional prompt."


def _tool_response(
    tool_name: str, payload: dict[str, object], stop_reason: str = "tool_use"
) -> dict[str, object]:
    content: list[dict[str, object]] = (
        [{"toolUse": {"toolUseId": "tooluse-fictional", "name": tool_name, "input": payload}}]
        if stop_reason == "tool_use"
        else []
    )
    return {
        "output": {"message": {"content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 120, "outputTokens": 40, "totalTokens": 160},
    }


def _narration(sentences: list[str]) -> dict[str, object]:
    return {"output_class": "perspective", "sentences": sentences}


def _grounded_generations() -> list[dict[str, object]]:
    """Scripted clean generations for the two must-accept cases, in case order."""
    return [
        _tool_response(
            "insight_narration_v1",
            _narration(["The fictional metric change of 12.45 is material."]),
        ),
        _tool_response(
            "insight_narration_v1",
            _narration(["One reading: the basis of 3 hints at a fictional shift."]),
        ),
    ]


# Every family's expected disposition, in ``_cases`` order.
_EXPECTED_DISPOSITIONS = {
    "known-correct": "accept",
    "contradiction": "reject",
    "fluent-unsupported": "reject",
    "exploratory": "accept",
    "derived-figure": "accept",
    "miscomputed-derivation": "reject",
    "incompatible-basis": "reject",
}


_JUDGMENT_SCRIPT = "judgment_script"


def _judgment(
    *, omit: tuple[str, ...] = (), overrides: dict[str, str] | None = None
) -> dict[str, object]:
    """Script the evaluator's disposition per case; each call answers only its own case."""
    dispositions = {**_EXPECTED_DISPOSITIONS, **(overrides or {})}
    return {
        _JUDGMENT_SCRIPT: {
            kind: disposition for kind, disposition in dispositions.items() if kind not in omit
        }
    }


def _rendered_findings(case: InsightCalibrationCase) -> list[dict[str, str]]:
    return [
        {"name": finding.name, "value": decimal_figure(finding.value)} for finding in case.findings
    ]


def _judged_kind(body: dict[str, Any]) -> str:
    """Resolve which fictional case an evaluator call judges: a draft by its text, else findings.

    Either way the call's findings must be that case's own, so a runner that paired one
    case's narration with another case's findings (cross-case bleed) fails here.
    """
    for case in _cases():
        if case.draft is not None and list(case.draft.sentences) == body["narration"]:
            assert body["findings"] == _rendered_findings(case), "judged findings are crossed"
            return case.kind.value
    for case in _cases():
        if case.draft is None and body["findings"] == _rendered_findings(case):
            return case.kind.value
    raise AssertionError("evaluator call judges no scripted case")


def _scripted_judgment(script: dict[str, str], body: dict[str, Any]) -> dict[str, object]:
    kind = _judged_kind(body)
    assert kind in script, f"unscripted judgment for {kind}"
    return _tool_response(
        "insight_calibration_v1",
        {"check": "A fictional recomputation.", "disposition": script[kind]},
    )


def _matching_judgment() -> dict[str, object]:
    return _judgment()


# The two must-accept cases' first finding names from ``_cases``, in the order
# ``_grounded_generations`` scripts their responses. The stub keys generation dispatch
# on them because the runner fans both generations out in one parallel wave, so
# arrival order is nondeterministic, and both payloads carry the same slot purpose.
_MUST_GENERATE_FINDINGS = (
    "Fictional metric change",
    "Fictional reading basis",
)
_SLOT_PURPOSE = "Explain which fictional campaign changes contribute most."


def _body(call: dict[str, object]) -> dict[str, Any]:
    messages = cast("list[dict[str, Any]]", call["messages"])
    return cast("dict[str, Any]", json.loads(cast("str", messages[0]["content"][0]["text"])))


def _is_judgment(body: dict[str, Any]) -> bool:
    return body.get("schema") == "insight-calibration-v4"


def _generation_finding(body: dict[str, Any]) -> str | None:
    if "purpose" not in body:
        return None
    return cast("str", body["findings"][0]["name"])


@dataclass
class ScriptedConverseClient:
    """Fake Converse dispatching one scripted response per call, thread-safe.

    A generation call is matched to its scripted position by the first finding its
    payload carries (case order); any other call — the evaluator's judgment —
    receives the final scripted response.
    """

    responses: list[dict[str, object]]
    calls: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def converse(self, **kwargs: object) -> dict[str, object]:
        with self._lock:
            self.calls.append(kwargs)
            response = self.responses[self._scripted_index(kwargs)]
        script = response.get(_JUDGMENT_SCRIPT)
        if script is None:
            return response
        return _scripted_judgment(cast("dict[str, str]", script), _body(kwargs))

    def _scripted_index(self, kwargs: dict[str, object]) -> int:
        body = _body(kwargs)
        if _generation_finding(body) in _MUST_GENERATE_FINDINGS:
            return _MUST_GENERATE_FINDINGS.index(cast("str", _generation_finding(body)))
        # Only the evaluator's judgment payload may take the final scripted
        # response; anything else is a mis-scripted test and fails loudly here.
        assert _is_judgment(body), f"unscripted converse payload: {sorted(body)}"
        return len(self.responses) - 1


_PERIOD_FINDINGS = (
    DeterministicFinding("Fictional spend this window", Decimal("125")),
    DeterministicFinding("Fictional spend prior window", Decimal("100")),
)


def _cases() -> tuple[InsightCalibrationCase, ...]:
    return (
        InsightCalibrationCase(
            InsightCalibrationKind.KNOWN_CORRECT,
            "A fictional material change.",
            "Accept a grounded perspective.",
            (DeterministicFinding("Fictional metric change", Decimal("12.45")),),
        ),
        InsightCalibrationCase(
            InsightCalibrationKind.CONTRADICTION,
            "The fictional change is negative but the draft calls it a gain.",
            "Reject the contradiction.",
            (DeterministicFinding("Fictional metric change", Decimal("-7.5")),),
            draft=InsightCalibrationDraft(("The fictional metric rose 7.5.",)),
        ),
        InsightCalibrationCase(
            InsightCalibrationKind.FLUENT_UNSUPPORTED,
            "A fluent draft cites a benchmark outside the findings.",
            "Reject the figure that is neither supplied nor derived from the findings.",
            (DeterministicFinding("Fictional metric change", Decimal("12.45")),),
            draft=InsightCalibrationDraft(
                ("The change of 12.45 beats the fictional benchmark of 99.",),
            ),
        ),
        InsightCalibrationCase(
            InsightCalibrationKind.EXPLORATORY,
            "A fictional movement invites a hedged reading.",
            "Accept the labeled reading.",
            (DeterministicFinding("Fictional reading basis", Decimal("3")),),
        ),
        InsightCalibrationCase(
            InsightCalibrationKind.DERIVED_FIGURE,
            "A fictional draft derives a percentage change from two supplied values.",
            "Accept the correctly derived percentage change.",
            _PERIOD_FINDINGS,
            draft=InsightCalibrationDraft(
                ("Fictional spend rose from 100 to 125, up 25 percent.",)
            ),
        ),
        InsightCalibrationCase(
            InsightCalibrationKind.MISCOMPUTED_DERIVATION,
            "A fictional draft divides the change by the current value.",
            "Reject the miscomputed percentage change.",
            _PERIOD_FINDINGS,
            draft=InsightCalibrationDraft(
                ("Fictional spend rose from 100 to 125, up 20 percent.",)
            ),
        ),
        InsightCalibrationCase(
            InsightCalibrationKind.INCOMPATIBLE_BASIS,
            "A fictional draft adds spend reported in two currencies.",
            "Reject the figure that combines findings of incompatible currency.",
            (
                DeterministicFinding("Fictional spend in US dollars", Decimal("100")),
                DeterministicFinding("Fictional spend in euros", Decimal("125")),
            ),
            draft=InsightCalibrationDraft(("Fictional spend totals 225 across both.",)),
        ),
    )


def _request(
    cases: tuple[InsightCalibrationCase, ...] | None = None,
) -> InsightPromptCalibrationRunRequest:
    return InsightPromptCalibrationRunRequest(
        InsightSlotId.CONTRIBUTION,
        2,
        _CANDIDATE_PROMPT,
        InsightModelRoute.QUICK,
        _cases() if cases is None else cases,
        InsightOutputClass.PERSPECTIVE,
        purpose=_SLOT_PURPOSE,
    )


def _runner(client: ScriptedConverseClient) -> BedrockInsightCalibrationRunner:
    return BedrockInsightCalibrationRunner(
        InsightModelRouteModels("model-quick-fictional", "model-deep-fictional"),
        2,
        cast("BedrockRuntimeClient", client),
    )


def _outcomes(
    client: ScriptedConverseClient,
) -> dict[InsightCalibrationKind, InsightCalibrationOutcome]:
    result = _runner(client).run(_request())
    return {case.kind: case.outcome for case in result.cases}


def _evaluator_calls(client: ScriptedConverseClient) -> list[dict[str, object]]:
    return [call for call in client.calls if _is_judgment(_body(call))]


def _evaluator_body_kinds(client: ScriptedConverseClient) -> set[str]:
    return {_judged_kind(_body(call)) for call in _evaluator_calls(client)}


def test_the_judge_never_sees_a_case_family_or_its_summary() -> None:
    """The family and summary name the verdict; ADR-082 makes the judge the only decider."""
    client = ScriptedConverseClient([*_grounded_generations(), _matching_judgment()])

    _runner(client).run(_request())

    bodies = [_body(call) for call in _evaluator_calls(client)]
    for body in bodies:
        # The judge gets the evidence the generator had, including the window a
        # narration may legitimately name ("this week"), and nothing that names the verdict.
        assert set(body) == {"schema", "instruction", "window_profile", "findings", "narration"}
    rendered = json.dumps(bodies)
    for case in _cases():
        assert case.kind.value not in rendered
        assert case.input_summary not in rendered


def test_must_accept_cases_generate_under_the_candidate_prompt_with_the_production_tool() -> None:
    """The candidate is the subject: generations run under it, with the serve-time envelope."""
    client = ScriptedConverseClient([*_grounded_generations(), _matching_judgment()])

    result = _runner(client).run(_request())

    assert result.outcome is InsightCalibrationOutcome.PASSED
    generation = cast("dict[str, Any]", client.calls[0])
    assert generation["system"] == [{"text": _CANDIDATE_PROMPT}]
    assert generation["inferenceConfig"] == {"maxTokens": 512}
    assert "outputConfig" not in generation
    # Card candidates keep the model default; only the deep tier carries the dial.
    assert "additionalModelRequestFields" not in generation
    tool_config = cast("dict[str, Any]", generation["toolConfig"])
    assert tool_config["toolChoice"] == {"tool": {"name": "insight_narration_v1"}}


def test_the_evaluator_judges_each_case_alone_under_its_own_instruction() -> None:
    """Batched judging let one case's findings bleed into a neighbour's verdict (2026-09-28)."""
    client = ScriptedConverseClient([*_grounded_generations(), _matching_judgment()])

    result = _runner(client).run(_request())

    assert result.evaluator == "model-quick-fictional"
    evaluators = _evaluator_calls(client)
    assert len(evaluators) == len(_EXPECTED_DISPOSITIONS)
    assert _evaluator_body_kinds(client) == set(_EXPECTED_DISPOSITIONS)
    for call in evaluators:
        evaluator = cast("dict[str, Any]", call)
        assert evaluator["system"] != [{"text": _CANDIDATE_PROMPT}]
        assert evaluator["inferenceConfig"] == {"maxTokens": 512}
        tool_config = cast("dict[str, Any]", evaluator["toolConfig"])
        assert tool_config["toolChoice"] == {"tool": {"name": "insight_calibration_v1"}}
        schema = tool_config["tools"][0]["toolSpec"]["inputSchema"]["json"]
        # The judge writes its recomputation before the disposition it decides.
        assert schema["required"] == ["check", "disposition"]


def test_every_case_with_text_reaches_the_evaluator_and_no_text_check_decides_one() -> None:
    """ADR-082: the calibrated evaluator decides every figure, derived or not."""
    client = ScriptedConverseClient([*_grounded_generations(), _matching_judgment()])

    outcomes = _outcomes(client)

    assert set(outcomes.values()) == {InsightCalibrationOutcome.PASSED}
    assert _evaluator_body_kinds(client) == set(_EXPECTED_DISPOSITIONS)


def test_the_evaluator_judges_the_derived_figure_families_by_disposition() -> None:
    """Accepting a miscomputed derivation or rejecting a correct one fails its case."""
    client = ScriptedConverseClient(
        [
            *_grounded_generations(),
            _judgment(
                overrides={
                    "derived-figure": "reject",
                    "miscomputed-derivation": "accept",
                }
            ),
        ]
    )

    outcomes = _outcomes(client)

    assert outcomes[InsightCalibrationKind.DERIVED_FIGURE] is InsightCalibrationOutcome.FAILED
    assert (
        outcomes[InsightCalibrationKind.MISCOMPUTED_DERIVATION] is InsightCalibrationOutcome.FAILED
    )
    assert outcomes[InsightCalibrationKind.INCOMPATIBLE_BASIS] is InsightCalibrationOutcome.PASSED


def test_silence_on_a_material_exploratory_payload_fails_the_must_accept_case() -> None:
    """Over-restriction fails symmetrically.

    A prompt that goes quiet on a material exploratory payload is exactly the
    defect this family exists to catch.
    """
    client = ScriptedConverseClient(
        [
            _grounded_generations()[0],
            _tool_response("insight_narration_v1", _narration([])),
            _judgment(omit=("exploratory",)),
        ]
    )

    outcomes = _outcomes(client)

    assert outcomes[InsightCalibrationKind.EXPLORATORY] is InsightCalibrationOutcome.FAILED
    assert outcomes[InsightCalibrationKind.KNOWN_CORRECT] is InsightCalibrationOutcome.PASSED


def test_a_generation_stating_an_unsupplied_figure_is_judged_by_the_evaluator() -> None:
    """No citation gate refuses it first; the evaluator's rejection fails the case."""
    client = ScriptedConverseClient(
        [
            _tool_response(
                "insight_narration_v1",
                _narration(["The change of 12.45 beats the benchmark of 99."]),
            ),
            _grounded_generations()[1],
            _judgment(overrides={"known-correct": "reject"}),
        ]
    )

    outcomes = _outcomes(client)

    assert "known-correct" in _evaluator_body_kinds(client)
    assert outcomes[InsightCalibrationKind.KNOWN_CORRECT] is InsightCalibrationOutcome.FAILED
    assert outcomes[InsightCalibrationKind.EXPLORATORY] is InsightCalibrationOutcome.PASSED


def test_a_generation_declaring_the_wrong_output_class_fails_its_must_accept_case() -> None:
    """The gate enforces the slot's declared class.

    Production refuses a class that differs from the declaration, so a passed
    calibration must attest an output production would accept.
    """
    client = ScriptedConverseClient(
        [
            _tool_response(
                "insight_narration_v1",
                {
                    "output_class": "hypothesis",
                    "sentences": ["The fictional metric change of 12.45 is material."],
                },
            ),
            _grounded_generations()[1],
            _judgment(omit=("known-correct",)),
        ]
    )

    outcomes = _outcomes(client)

    assert outcomes[InsightCalibrationKind.KNOWN_CORRECT] is InsightCalibrationOutcome.FAILED
    assert outcomes[InsightCalibrationKind.EXPLORATORY] is InsightCalibrationOutcome.PASSED


def test_the_calibration_payload_declares_the_slots_output_class_and_horizon_days() -> None:
    client = ScriptedConverseClient([*_grounded_generations(), _matching_judgment()])

    _runner(client).run(_request())

    generation = cast("dict[str, Any]", client.calls[0])
    body = json.loads(cast("str", generation["messages"][0]["content"][0]["text"]))
    assert body["output_class"] == "perspective"
    # Production sends the slot's purpose; a case summary describes the expected answer
    # and would hand the generator facts its findings do not carry.
    assert body["purpose"] == _SLOT_PURPOSE
    # Production profiles carry the aligned prior period the findings compare against;
    # a null prior would tell generator and judge that "prior window" does not exist.
    assert body["window_profile"] == {
        "days": 7,
        "grain": "day",
        "prior_start": "2031-03-08",
        "prior_end": "2031-03-14",
        "role": "tactical-review",
    }


def test_a_truncated_generation_fails_its_case_rather_than_aborting_the_run() -> None:
    """A candidate prompt that overruns the production budget is the prompt's own failure."""
    client = ScriptedConverseClient(
        [
            _tool_response("insight_narration_v1", {}, stop_reason="max_tokens"),
            _grounded_generations()[1],
            _judgment(omit=("known-correct",)),
        ]
    )

    outcomes = _outcomes(client)

    assert outcomes[InsightCalibrationKind.KNOWN_CORRECT] is InsightCalibrationOutcome.FAILED
    assert outcomes[InsightCalibrationKind.EXPLORATORY] is InsightCalibrationOutcome.PASSED


def test_a_mismatched_evaluator_disposition_fails_the_case() -> None:
    client = ScriptedConverseClient(
        [
            *_grounded_generations(),
            _judgment(overrides={"known-correct": "reject", "contradiction": "accept"}),
        ]
    )

    outcomes = _outcomes(client)

    assert outcomes[InsightCalibrationKind.KNOWN_CORRECT] is InsightCalibrationOutcome.FAILED
    assert outcomes[InsightCalibrationKind.CONTRADICTION] is InsightCalibrationOutcome.FAILED
    assert outcomes[InsightCalibrationKind.EXPLORATORY] is InsightCalibrationOutcome.PASSED


def test_failed_generations_still_leave_every_draft_to_the_evaluator() -> None:
    """Structural failures decide only their own cases; the drafts are always judged."""
    client = ScriptedConverseClient(
        [
            _tool_response("insight_narration_v1", _narration([])),
            _tool_response("insight_narration_v1", _narration([])),
            _judgment(omit=("known-correct", "exploratory")),
        ]
    )

    result = _runner(client).run(_request())

    assert result.evaluator == "model-quick-fictional"
    assert _evaluator_body_kinds(client) == set(_EXPECTED_DISPOSITIONS) - {
        "known-correct",
        "exploratory",
    }
    by_kind = {case.kind: case.outcome for case in result.cases}
    assert by_kind[InsightCalibrationKind.KNOWN_CORRECT] is InsightCalibrationOutcome.FAILED
    assert by_kind[InsightCalibrationKind.CONTRADICTION] is InsightCalibrationOutcome.PASSED


def test_a_truncated_evaluator_call_is_typed_with_stop_reason_and_usage() -> None:
    """A budget cut must not read as a model-behavior problem (2026-09-18 RCA lesson)."""
    client = ScriptedConverseClient(
        [
            *_grounded_generations(),
            _tool_response("insight_calibration_v1", {}, stop_reason="max_tokens"),
        ]
    )

    with pytest.raises(InvariantError) as raised:
        _runner(client).run(_request())

    assert str(raised.value.code) == "ADLY-INSIGHT-0007"
    assert raised.value.context["stop_reason"] == "max_tokens"
    assert raised.value.context["usage"] == {
        "inputTokens": 120,
        "outputTokens": 40,
        "totalTokens": 160,
    }


def test_an_evaluator_tool_call_that_is_not_the_pinned_tool_is_refused() -> None:
    client = ScriptedConverseClient(
        [
            *_grounded_generations(),
            _tool_response("fabricated_tool", {"check": "x", "disposition": "accept"}),
        ]
    )

    with pytest.raises(InvariantError) as raised:
        _runner(client).run(_request())

    assert str(raised.value.code) == "ADLY-INSIGHT-0007"
    assert "fabricated_tool" not in str(raised.value.context)


def test_a_second_evaluator_tool_call_is_refused_as_ambiguous() -> None:
    """Two tool calls cannot both be the disposition set; ambiguity is refused."""
    block = {
        "toolUse": {
            "toolUseId": "tooluse-fictional",
            "name": "insight_calibration_v1",
            "input": {"check": "A fictional recomputation.", "disposition": "accept"},
        }
    }
    client = ScriptedConverseClient(
        [
            *_grounded_generations(),
            {
                "output": {"message": {"content": [block, dict(block)]}},
                "stopReason": "tool_use",
                "usage": {"inputTokens": 120, "outputTokens": 40, "totalTokens": 160},
            },
        ]
    )

    with pytest.raises(InvariantError) as raised:
        _runner(client).run(_request())

    assert str(raised.value.code) == "ADLY-INSIGHT-0007"


def test_an_evaluator_reply_without_its_check_is_malformed() -> None:
    client = ScriptedConverseClient(
        [
            *_grounded_generations(),
            _tool_response("insight_calibration_v1", {"disposition": "accept"}),
        ]
    )

    with pytest.raises(InvariantError) as raised:
        _runner(client).run(_request())

    assert str(raised.value.code) == "ADLY-INSIGHT-0007"


def test_an_evaluator_disposition_outside_accept_or_reject_is_malformed() -> None:
    client = ScriptedConverseClient(
        [
            *_grounded_generations(),
            _tool_response(
                "insight_calibration_v1",
                {"check": "A fictional recomputation.", "disposition": "maybe"},
            ),
        ]
    )

    with pytest.raises(InvariantError) as raised:
        _runner(client).run(_request())

    assert str(raised.value.code) == "ADLY-INSIGHT-0007"


def test_an_evaluator_check_that_is_not_text_is_malformed() -> None:
    client = ScriptedConverseClient(
        [
            *_grounded_generations(),
            _tool_response(
                "insight_calibration_v1",
                {"check": 42, "disposition": "accept"},
            ),
        ]
    )

    with pytest.raises(InvariantError) as raised:
        _runner(client).run(_request())

    assert str(raised.value.code) == "ADLY-INSIGHT-0007"


@pytest.mark.covers("ADR-076")
def test_deep_prompt_calibration_generates_through_the_production_deep_envelope() -> None:
    def deep_generation(value: str) -> dict[str, object]:
        return _tool_response(
            "insight_deep_dive_v1",
            {
                "what_stands_out": f"The supplied fictional movement of {value} stands out.",
                "candidate_readings": ["Reading one: the movement may be temporary."],
                "discriminating_evidence": "The next delivered window would discriminate.",
                "cited_figures": [value],
            },
        )

    client = ScriptedConverseClient(
        [deep_generation("12.45"), deep_generation("3"), _matching_judgment()]
    )
    request = replace(_request(), tier=InsightPromptTier.DEEP)

    result = _runner(client).run(request)

    assert result.outcome is InsightCalibrationOutcome.PASSED
    first = cast("dict[str, Any]", client.calls[0])
    assert first["inferenceConfig"] == {"maxTokens": 2500}
    # Calibration attests the production envelope: the deep candidate generates
    # under the serve path's effort dial, while the evaluator keeps the default.
    assert first["additionalModelRequestFields"] == {"output_config": {"effort": "medium"}}
    evaluator_call = cast("dict[str, Any]", client.calls[-1])
    assert "additionalModelRequestFields" not in evaluator_call
    tool_config = cast("dict[str, Any]", first["toolConfig"])
    assert tool_config["toolChoice"] == {"tool": {"name": "insight_deep_dive_v1"}}
    messages = cast("list[dict[str, Any]]", first["messages"])
    body = json.loads(cast("str", messages[0]["content"][0]["text"]))
    assert body["schema"] == "insight-deep-dive-v1"
    assert body["silence_allowed"] is False


@pytest.mark.covers("ADR-076")
def test_deep_calibration_reuses_the_production_reading_count_validation() -> None:
    invalid = _tool_response(
        "insight_deep_dive_v1",
        {
            "what_stands_out": "The supplied movement stands out.",
            "candidate_readings": [],
            "discriminating_evidence": "The next window would discriminate.",
            "cited_figures": [],
        },
    )
    client = ScriptedConverseClient(
        [
            invalid,
            invalid,
            _judgment(omit=("known-correct", "exploratory")),
        ]
    )

    result = _runner(client).run(replace(_request(), tier=InsightPromptTier.DEEP))

    by_kind = {case.kind: case.outcome for case in result.cases}
    assert by_kind[InsightCalibrationKind.KNOWN_CORRECT] is InsightCalibrationOutcome.FAILED
    assert by_kind[InsightCalibrationKind.EXPLORATORY] is InsightCalibrationOutcome.FAILED
    assert by_kind[InsightCalibrationKind.CONTRADICTION] is InsightCalibrationOutcome.PASSED


@pytest.mark.covers("ADR-076")
def test_the_two_must_accept_generations_share_one_parallel_wave() -> None:
    """Serial generations would add their provider tails and waste the worker envelope.

    The wave bounds generation at one patient transport before the parallel evaluator
    wave. Each generation blocks on a two-party barrier, so a sequential runner
    deadlocks into the barrier timeout and fails this test.
    """
    scripted = ScriptedConverseClient([*_grounded_generations(), _matching_judgment()])
    barrier = threading.Barrier(2)

    class WaveClient:
        def converse(self, **kwargs: object) -> dict[str, object]:
            messages = cast("list[dict[str, Any]]", kwargs["messages"])
            body = json.loads(cast("str", messages[0]["content"][0]["text"]))
            if _generation_finding(body) in _MUST_GENERATE_FINDINGS:
                barrier.wait(timeout=5)
            return scripted.converse(**kwargs)

    runner = BedrockInsightCalibrationRunner(
        InsightModelRouteModels("model-quick-fictional", "model-deep-fictional"),
        2,
        cast("BedrockRuntimeClient", WaveClient()),
    )

    result = runner.run(_request())

    assert result.outcome is InsightCalibrationOutcome.PASSED


def test_every_judged_case_runs_in_one_parallel_evaluator_wave() -> None:
    """Serial judge calls would overrun the 6s evaluator transport and the 25s Lambda.

    Each evaluator call blocks on a barrier sized to the judged-case count, so a
    runner that judges sequentially deadlocks into the barrier timeout and fails.
    """
    scripted = ScriptedConverseClient([*_grounded_generations(), _matching_judgment()])
    barrier = threading.Barrier(len(_EXPECTED_DISPOSITIONS))

    class WaveClient:
        def converse(self, **kwargs: object) -> dict[str, object]:
            if _is_judgment(_body(kwargs)):
                barrier.wait(timeout=5)
            return scripted.converse(**kwargs)

    runner = BedrockInsightCalibrationRunner(
        InsightModelRouteModels("model-quick-fictional", "model-deep-fictional"),
        2,
        cast("BedrockRuntimeClient", WaveClient()),
    )

    result = runner.run(_request())

    assert result.outcome is InsightCalibrationOutcome.PASSED


def test_one_failed_judge_call_among_passing_ones_leaves_the_run_unrecorded() -> None:
    """A single unavailable judgment must not let the others decide the version (pending)."""
    scripted = ScriptedConverseClient([*_grounded_generations(), _matching_judgment()])
    failing_narration = next(
        list(case.draft.sentences)
        for case in _cases()
        if case.kind is InsightCalibrationKind.INCOMPATIBLE_BASIS and case.draft is not None
    )

    class OneFailingJudgeClient:
        def converse(self, **kwargs: object) -> dict[str, object]:
            body = _body(kwargs)
            if _is_judgment(body) and body["narration"] == failing_narration:
                raise ClientError(
                    {"Error": {"Code": "ThrottlingException", "Message": "fictional"}},
                    "Converse",
                )
            return scripted.converse(**kwargs)

    runner = BedrockInsightCalibrationRunner(
        InsightModelRouteModels("model-quick-fictional", "model-deep-fictional"),
        2,
        cast("BedrockRuntimeClient", OneFailingJudgeClient()),
    )

    with pytest.raises(DependencyError):
        runner.run(_request())


def test_an_evaluator_check_that_is_blank_is_malformed() -> None:
    client = ScriptedConverseClient(
        [
            *_grounded_generations(),
            _tool_response("insight_calibration_v1", {"check": "  ", "disposition": "accept"}),
        ]
    )

    with pytest.raises(InvariantError) as raised:
        _runner(client).run(_request())

    assert str(raised.value.code) == "ADLY-INSIGHT-0007"


def test_runtime_clients_bound_connect_and_read_phases_inside_worker_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs: list[object] = []

    def client(_service: str, *, config: object) -> object:
        configs.append(config)
        return ScriptedConverseClient([])

    monkeypatch.setattr(calibration.boto3, "client", client)

    calibration.BedrockInsightCalibrationRunner(
        InsightModelRouteModels("fictional-quick", "fictional-deep"), 45
    )

    generation, evaluator = cast("tuple[Any, Any]", tuple(configs))
    assert generation.connect_timeout == 5
    assert generation.read_timeout == 45
    assert evaluator.connect_timeout == 5
    assert evaluator.read_timeout == 10
    assert 5 + 45 + 5 + 10 < 85
