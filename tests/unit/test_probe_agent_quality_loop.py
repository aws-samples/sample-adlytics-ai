# SPDX-License-Identifier: MIT-0
"""Semantic acceptance contract for the protected Agent Quality probe."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import probe_agent_quality_loop as probe  # noqa: E402

from domain.conversation.answer_stream import (  # noqa: E402
    ConversationRefusedEventV4,
    ConversationTextEventV4,
)
from domain.conversation.audit import (  # noqa: E402
    ANSWER_STREAM_CONTRACT,
    MANAGED_HARNESS_RUNTIME_V3,
    AuditFindingKindV2,
    AuditFindingSeverityV2,
    AuditOperationCategoryV2,
    AuditOperationOutcomeV2,
    AuditPublicationOutcomeV2,
    AuditTerminalOutcomeV2,
    AuditToolStepDispositionV3,
    AuditToolStepV3,
    ConversationAuditRecordV6,
)
from domain.conversation.audit_findings import derive_findings  # noqa: E402
from domain.evaluation.linkage import (  # noqa: E402
    EvaluationDeliveryFamilyV1,
    EvaluationDeliveryOutcomeV1,
    EvaluationNotDispatchedReasonV1,
    EvaluationResultStateV1,
)
from domain.evaluation.release import (  # noqa: E402
    EvaluationFamilyV1,
    EvaluationReleaseEvaluatorV1,
    EvaluationReleaseV1,
)

pytestmark = pytest.mark.unit
# The probe's own view types, so isinstance checks see the one module the probe imported.
PREDATES_GATE = probe.PREDATES_GATE
ReviewedDelivery = probe.ReviewedDelivery
ReviewedEvaluation = probe.ReviewedEvaluation
ReviewedVerdict = probe.ReviewedVerdict

_AUDIT_ID = "audit-01"
_ADMISSION_REF = "admission-01"
_DECIDED_AT = "2026-09-27T12:00:00+00:00"
_RETIRED_V3_CONFIG = "adlytics_dev_released_v3-AbCdE12345"
_V4_CONFIG = "adlytics_dev_released_v4-FgHiJ67890"


def _evaluator(evaluator_id: str, name: str) -> EvaluationReleaseEvaluatorV1:
    return EvaluationReleaseEvaluatorV1(
        evaluator_id=evaluator_id,
        evaluator_name=name,
        revision="r1",
        model_id="fictional.judge-model-v1:0",
        prompt_digest="a" * 64,
    )


_V4_HELPFUL = _evaluator("adlytics_dev_helpfulness_v4-Helpful123", "helpfulness_v4")
_V4_GROUNDING = _evaluator("adlytics_dev_grounding_v4-Ground1234", "grounding_v4")
_V4_OTHER = _evaluator("adlytics_dev_other_v4-Other12345", "other_v4")


def _release(
    family: EvaluationFamilyV1,
    config_id: str | None,
    *evaluators: EvaluationReleaseEvaluatorV1,
) -> EvaluationReleaseV1:
    return EvaluationReleaseV1(
        family=family,
        enabled=config_id is not None,
        config_id=config_id,
        evaluators=evaluators,
        decided_at=_DECIDED_AT,
    )


def _dispatched(*families: EvaluationDeliveryFamilyV1) -> ReviewedDelivery:
    return ReviewedDelivery(
        outcome=EvaluationDeliveryOutcomeV1.DISPATCHED,
        not_dispatched_reason=None,
        families=families,
    )


def _not_dispatched(reason: EvaluationNotDispatchedReasonV1) -> ReviewedDelivery:
    return ReviewedDelivery(
        outcome=EvaluationDeliveryOutcomeV1.NOT_DISPATCHED,
        not_dispatched_reason=reason,
        families=(),
    )


# A receipt written while the retired V3 family ran names it; it still decodes (ADR-079).
_RETIRED_V3_FAMILY = EvaluationDeliveryFamilyV1(
    family=EvaluationFamilyV1.V3, config_id=_RETIRED_V3_CONFIG, evaluators=(_V4_OTHER,)
)
_V4_FAMILY = EvaluationDeliveryFamilyV1(
    family=EvaluationFamilyV1.V4, config_id=_V4_CONFIG, evaluators=(_V4_HELPFUL, _V4_GROUNDING)
)


def _verdict(
    evaluator: EvaluationReleaseEvaluatorV1,
    released_family: EvaluationFamilyV1,
    released_config: str,
    /,
    **overrides: object,
) -> ReviewedVerdict:
    fields: dict[str, object] = {
        "evaluator_ref": evaluator.evaluator_id,
        "evaluator_name": evaluator.evaluator_name,
        "state": EvaluationResultStateV1.COMPLETED,
        "label": "good",
        "family": released_family,
        "config_id": released_config,
        "evaluator_revision": evaluator.revision,
        "model_id": evaluator.model_id,
        "prompt_digest": evaluator.prompt_digest,
        **overrides,
    }
    return ReviewedVerdict(**fields)  # pyright: ignore[reportArgumentType]


class _State:
    def __init__(self, value: str) -> None:
        self.value = value


class _Result:
    def __init__(self, state: str) -> None:
        self.state = _State(state)


@pytest.mark.parametrize("state", ["completed", "evaluator_failed"])
def test_semantic_evaluation_states_are_accepted(state: str) -> None:
    assert probe.semantic_evaluation_result_state(_Result(state)) == state


@pytest.mark.covers("ADR-079")
def test_release_records_are_read_from_each_family_gate_parameter() -> None:
    assert probe._release_parameter_names("dev") == (  # pyright: ignore[reportPrivateUsage]
        "/adlytics/dev/evaluation/release-v4",
    )


@pytest.mark.covers("ADR-079")
def test_missing_and_disabled_release_records_are_not_enabled() -> None:
    enabled = _release(EvaluationFamilyV1.V4, _V4_CONFIG, _V4_HELPFUL, _V4_GROUNDING)

    assert probe.enabled_evaluation_releases((None,)) == ()
    assert probe.enabled_evaluation_releases((_release(EvaluationFamilyV1.V4, None),)) == ()
    assert probe.enabled_evaluation_releases((enabled,)) == (enabled,)


@pytest.mark.covers("ADR-079")
def test_no_enabled_family_requires_a_not_enabled_undispatched_receipt() -> None:
    assert (
        probe.evaluation_dispatch_mismatch(
            _not_dispatched(EvaluationNotDispatchedReasonV1.EVALUATION_NOT_ENABLED), ()
        )
        is None
    )
    assert (
        probe.evaluation_dispatch_mismatch(
            _not_dispatched(EvaluationNotDispatchedReasonV1.DELIVERY_UNKNOWN), ()
        )
        == "delivery_not_undispatched"
    )
    assert probe.evaluation_dispatch_mismatch(_dispatched(_V4_FAMILY), ()) == (
        "delivery_not_undispatched"
    )


@pytest.mark.covers("ADR-079")
def test_enabled_families_require_a_receipt_dispatched_to_exactly_those_releases() -> None:
    enabled = (_release(EvaluationFamilyV1.V4, _V4_CONFIG, _V4_HELPFUL, _V4_GROUNDING),)

    assert probe.evaluation_dispatch_mismatch(_dispatched(_V4_FAMILY), enabled) is None
    # A receipt that also names the retired V3 family was not sent to exactly the enabled set.
    assert probe.evaluation_dispatch_mismatch(
        _dispatched(_RETIRED_V3_FAMILY, _V4_FAMILY), enabled
    ) == ("delivery_family_mismatch")
    other_config = EvaluationDeliveryFamilyV1(
        family=EvaluationFamilyV1.V4,
        config_id="adlytics_dev_released_v4-Other00000",
        evaluators=(_V4_HELPFUL, _V4_GROUNDING),
    )
    assert probe.evaluation_dispatch_mismatch(_dispatched(other_config), enabled) == (
        "delivery_family_mismatch"
    )
    assert (
        probe.evaluation_dispatch_mismatch(
            _not_dispatched(EvaluationNotDispatchedReasonV1.EVALUATION_NOT_ENABLED), enabled
        )
        == "delivery_not_dispatched"
    )


@pytest.mark.covers("ADR-079")
def test_a_receipt_that_predates_the_gate_is_never_current_dispatch_evidence() -> None:
    assert probe.evaluation_dispatch_mismatch(PREDATES_GATE, ()) == "delivery_predates_gate"
    assert probe.evaluation_dispatch_mismatch(None, ()) == "delivery_invalid"


@pytest.mark.covers("ADR-079")
def test_verdicts_are_pending_until_every_dispatched_evaluator_has_one() -> None:
    receipt = _dispatched(_V4_FAMILY)
    helpful = _verdict(_V4_HELPFUL, EvaluationFamilyV1.V4, _V4_CONFIG)
    grounding = _verdict(
        _V4_GROUNDING,
        EvaluationFamilyV1.V4,
        _V4_CONFIG,
        state=EvaluationResultStateV1.EVALUATOR_FAILED,
        label=None,
    )

    assert probe.dispatched_verdicts(receipt, (helpful,)) is None
    # One verdict per dispatched evaluator, in the receipt's evaluator order.
    assert probe.dispatched_verdicts(receipt, (grounding, helpful)) == (helpful, grounding)
    assert probe.evaluation_verdict_labels((helpful, grounding)) == {
        "helpfulness_v4": "good",
        "grounding_v4": None,
    }


@pytest.mark.covers("ADR-079")
@pytest.mark.parametrize(
    "override",
    [
        {"config_id": "adlytics_dev_released_v4-Other00000"},
        {"family": EvaluationFamilyV1.V3},
        {"evaluator_revision": "r2"},
        {"model_id": "fictional.other-model-v1:0"},
        {"prompt_digest": "b" * 64},
    ],
)
def test_a_verdict_whose_provenance_differs_from_its_receipt_fails(
    override: dict[str, object],
) -> None:
    verdict = _verdict(_V4_HELPFUL, EvaluationFamilyV1.V4, _V4_CONFIG, **override)

    with pytest.raises(Exception, match="evaluation_verdict_provenance"):
        probe.dispatched_verdicts(_dispatched(_V4_FAMILY), (verdict,))


@pytest.mark.covers("ADR-079")
def test_a_verdict_from_an_evaluator_the_row_was_not_sent_to_fails() -> None:
    stray = _verdict(_V4_OTHER, EvaluationFamilyV1.V4, _V4_CONFIG)

    with pytest.raises(Exception, match="evaluation_verdict_provenance"):
        probe.dispatched_verdicts(_dispatched(_V4_FAMILY), (stray,))


def _evaluation(*results: dict[str, object]) -> ReviewedEvaluation:
    return ReviewedEvaluation(
        state="results_available",
        admitted=True,
        delivery=_dispatched(_V4_FAMILY),
        writer_present=True,
        writer_trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        verdicts=(),
        results=results,
    )


@pytest.mark.covers("ADR-078")
def test_returned_verdicts_must_hold_none_of_the_turns_protected_values() -> None:
    """The verdicts are read through the review API, which the probe checks rather than trusts."""
    protected = ("123e4567-e89b-42d3-a456-426614174000", "fictional question")
    clean = _evaluation({"evaluator_ref": _V4_HELPFUL.evaluator_id, "label": "good"})
    leaked = _evaluation({"evaluator_ref": _V4_HELPFUL.evaluator_id, "label": "fictional question"})
    assert probe.results_are_content_free(clean, protected) is True
    assert probe.results_are_content_free(leaked, protected) is False


@dataclass(frozen=True, slots=True)
class _Finding:
    """A finding as the review API returns it: closed kind, severity, time, and anchor."""

    kind: AuditFindingKindV2
    severity: AuditFindingSeverityV2
    created_at: str
    step_ordinal: int | None


def _reviewed(finding: object) -> _Finding:
    item = cast("Any", finding)
    return _Finding(
        kind=item.kind,
        severity=item.severity,
        created_at=item.created_at,
        step_ordinal=item.step_ordinal,
    )


@pytest.mark.covers("ADR-078")
def test_reviewed_findings_attest_exactly_what_the_processor_derives() -> None:
    # A streamed turn that ended without writing an answer (ADR-083) is a governed refusal.
    refusal = replace(
        _summary_record(),
        operation_outcome=AuditOperationOutcomeV2.REFUSED,
        publication_outcome=AuditPublicationOutcomeV2.REFUSED,
        terminal_outcome=AuditTerminalOutcomeV2.GOVERNED_REFUSAL,
        released_character_count=0,
    )
    derived = derive_findings(refusal)
    reviewed = tuple(_reviewed(finding) for finding in derived)
    assert [finding.kind for finding in derived] == [AuditFindingKindV2.GOVERNED_REFUSAL]
    assert probe.reviewed_findings_match(derived, reviewed) is True
    assert probe.reviewed_findings_match(derived, reviewed[1:]) is False
    extra = _Finding(
        kind=AuditFindingKindV2.DEPENDENCY_FAILURE,
        severity=AuditFindingSeverityV2.ERROR,
        created_at="2026-09-02T12:00:00+00:00",
        step_ordinal=None,
    )
    assert probe.reviewed_findings_match(derived, (*reviewed, extra)) is False


def _result_link_resources(*filter_ids: str) -> list[dict[str, object]]:
    return [
        {
            "ResourceType": "AWS::Lambda::Function",
            "LogicalResourceId": "IngressV3ABCD",
            "PhysicalResourceId": "adlytics-dev-ingress",
        },
        {
            "ResourceType": "AWS::Lambda::Function",
            "LogicalResourceId": "LinkerV3ABCD",
            "PhysicalResourceId": "adlytics-dev-linker",
        },
        {"ResourceType": "AWS::SQS::Queue", "LogicalResourceId": "ProjectionQueueV3"},
        {"ResourceType": "AWS::SQS::Queue", "LogicalResourceId": "ProjectionDeadLetterQueueV3"},
        {"ResourceType": "AWS::Lambda::EventSourceMapping", "LogicalResourceId": "Mapping"},
        {"ResourceType": "AWS::Lambda::ResourcePolicy", "LogicalResourceId": "IngressPolicy"},
        *(
            {
                "ResourceType": "AWS::Logs::SubscriptionFilter",
                "LogicalResourceId": "ResultGroupSubscription",
                "PhysicalResourceId": physical,
            }
            for physical in filter_ids
        ),
    ]


@pytest.mark.covers("ADR-079")
def test_result_link_attests_logs_only_ingress_and_one_subscription_per_family() -> None:
    link = probe._attest_result_link_resources(  # pyright: ignore[reportPrivateUsage]
        _result_link_resources(
            "adlytics-dev-evaluation-result-ingress-v4|/adlytics/dev/evaluation/results-v4",
        ),
        "dev",
    )

    assert link.ingress_function == "adlytics-dev-ingress"
    assert link.families == frozenset({EvaluationFamilyV1.V4})
    by_name = probe._attest_result_link_resources(  # pyright: ignore[reportPrivateUsage]
        _result_link_resources("adlytics-dev-evaluation-result-ingress-v4"), "dev"
    )
    assert by_name.families == frozenset({EvaluationFamilyV1.V4})


@pytest.mark.covers("ADR-079")
@pytest.mark.parametrize(
    "resources",
    [
        # A directed-invocation Permission beside the resource policy.
        [
            *_result_link_resources("adlytics-dev-evaluation-result-ingress-v4"),
            {"ResourceType": "AWS::Lambda::Permission", "LogicalResourceId": "Invoke"},
        ],
        # No resource policy at all.
        [
            item
            for item in _result_link_resources("adlytics-dev-evaluation-result-ingress-v4")
            if item["ResourceType"] != "AWS::Lambda::ResourcePolicy"
        ],
        # A filter that is not one of the owned ingress subscriptions.
        _result_link_resources(
            "adlytics-dev-evaluation-result-ingress-v4", "adlytics-dev-directed-probe"
        ),
        # Two filters for one family.
        _result_link_resources(
            "adlytics-dev-evaluation-result-ingress-v4",
            "adlytics-dev-evaluation-result-ingress-v4",
        ),
        # The retired V3 family's group still subscribed, alone or beside V4.
        _result_link_resources("adlytics-dev-evaluation-result-ingress-v3"),
        _result_link_resources(
            "adlytics-dev-evaluation-result-ingress-v3",
            "adlytics-dev-evaluation-result-ingress-v4",
        ),
        # No subscription at all.
        _result_link_resources(),
    ],
)
def test_result_link_refuses_any_other_invocation_or_subscription_shape(
    resources: list[dict[str, object]],
) -> None:
    with pytest.raises(ValueError, match="result-link stack"):
        probe._attest_result_link_resources(resources, "dev")  # pyright: ignore[reportPrivateUsage]


def test_verdict_timeout_is_an_explicit_bounded_cli_argument() -> None:
    required = [
        "--profile",
        "explicit-profile",
        "--environment",
        "dev",
        "--region",
        "us-east-1",
        "--expected-account",
        "000000000000",
        "--result-link-stack",
        "Adlytics-EvaluationResultLinkV3-dev",
        "--evidence-dir",
        "validation/evidence/test",
    ]
    parser = probe.probe_parser()

    # Above the 434-675 s AgentCore online-evaluation result lag measured in dev (2026-09-29).
    assert parser.parse_args(required).verdict_timeout_seconds == 900
    assert (
        parser.parse_args([*required, "--verdict-timeout-seconds", "90"]).verdict_timeout_seconds
        == 90
    )
    for invalid in ("0", "3601"):
        with pytest.raises(SystemExit):
            parser.parse_args([*required, "--verdict-timeout-seconds", invalid])


def test_unknown_evaluation_result_fails_semantic_link_acceptance() -> None:
    with pytest.raises(Exception, match="evaluation_result_missing_or_invalid"):
        probe.semantic_evaluation_result_state(_Result("missing"))


_V4_REFUSAL = (
    b'data:{"schema_version":"ConversationStream.v4","kind":"refused",'
    b'"reason_code":"no_answer","reason":"Fictional refusal."}\n\n'
)


def test_terminal_decodes_streamed_text_and_refusal_sequences() -> None:
    terminal, events = probe._terminal(  # pyright: ignore[reportPrivateUsage]
        [
            (
                0.0,
                b'data:{"schema_version":"ConversationStream.v4","kind":"activity",'
                b'"stage":"understanding_request"}\n\n'
                b'data:{"schema_version":"ConversationStream.v4","kind":"text",'
                b'"sequence":1,"text":"Fictional "}\n\n',
            ),
            (
                0.5,
                b'data:{"schema_version":"ConversationStream.v4","kind":"text",'
                b'"sequence":2,"text":"answer."}\n\n'
                b'data:{"audit_id":"audit-01","kind":"done",'
                b'"schema_version":"ConversationStream.v4"}\n\n',
            ),
        ]
    )
    assert terminal == "response_done"
    assert "".join(
        event.text for event in events if isinstance(event, ConversationTextEventV4)
    ) == ("Fictional answer.")

    # Text already sent stays sent; the refusal still ends the stream.
    partial_terminal, partial_events = probe._terminal(  # pyright: ignore[reportPrivateUsage]
        [
            (
                0.0,
                b'data:{"schema_version":"ConversationStream.v4","kind":"text",'
                b'"sequence":1,"text":"Partial."}\n\n' + _V4_REFUSAL,
            )
        ]
    )
    assert partial_terminal == "refused"
    refusal = partial_events[-1]
    assert isinstance(refusal, ConversationRefusedEventV4)
    assert refusal.reason_code.value == "no_answer"
    textless_terminal, textless_events = probe._terminal(  # pyright: ignore[reportPrivateUsage]
        [(0.0, _V4_REFUSAL)]
    )
    assert textless_terminal == "refused"
    assert isinstance(textless_events[-1], ConversationRefusedEventV4)


@pytest.mark.parametrize(
    "frames",
    [
        # The retired block stream is not a V4 event.
        b'data:{"schema_version":"ConversationStream.v3","kind":"refused",'
        b'"reason_code":"output_intervened","reason":"Fictional refusal."}\n\n',
        # `done` requires released text.
        b'data:{"kind":"done","schema_version":"ConversationStream.v4"}\n\n',
        # A gap in the text sequence.
        b'data:{"schema_version":"ConversationStream.v4","kind":"text",'
        b'"sequence":2,"text":"Gap."}\n\n' + _V4_REFUSAL,
        # No terminal before the transport closed.
        b'data:{"schema_version":"ConversationStream.v4","kind":"text",'
        b'"sequence":1,"text":"Open."}\n\n',
    ],
    ids=["v3-event", "textless-done", "sequence-gap", "unterminated"],
)
def test_terminal_refuses_an_illegal_v4_stream(frames: bytes) -> None:
    with pytest.raises(ValueError):
        probe._terminal([(0.0, frames)])  # pyright: ignore[reportPrivateUsage]


def test_terminal_ignores_the_routes_keep_alive_comment_frames() -> None:
    """The route heartbeats with SSE comments during long turns; they carry no event."""
    terminal, events = probe._terminal(  # pyright: ignore[reportPrivateUsage]
        [
            (0.0, b": keep-alive\n\n"),
            (1.0, _V4_REFUSAL),
        ]
    )

    assert terminal == "refused"
    assert len(events) == 1


@pytest.mark.parametrize(
    ("environment", "region"),
    [("production", "us-east-1"), ("dev", "us-west-2")],
)
def test_main_rejects_nonapproved_target_before_constructing_an_aws_session(
    capsys: pytest.CaptureFixture[str],
    environment: str,
    region: str,
) -> None:
    assert (
        probe.main(
            [
                "--profile",
                "explicit-profile",
                "--environment",
                environment,
                "--region",
                region,
                "--expected-account",
                "000000000000",
                "--result-link-stack",
                "ResultLink-dev",
                "--evidence-dir",
                "validation/evidence/test",
            ]
        )
        == 2
    )
    assert capsys.readouterr().out == "agent quality probe accepts only the approved dev region\n"

    with pytest.raises(ValueError, match="exact dev result-link stack"):
        probe._attest_result_link_stack(  # pyright: ignore[reportPrivateUsage]
            None,
            "Adlytics-EvaluationResultLink-dev",
            "dev",
        )


def test_processor_algorithm_import_suppresses_lambda_startup_log() -> None:
    script = """
import sys
sys.path[:0] = ["src", "validation", "."]
from validation import probe_agent_quality_loop as probe
probe._processor_algorithms()
"""
    # A-SUBPROCESS-001: fixed interpreter and code; see docs/security-dispositions.md
    # The interpreter and inline code are test constants; no caller input reaches the subprocess.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ADL_AGENT_QUALITY_AUDIT_TABLE": "test-audit-table",
            "ADL_AGENT_QUALITY_FINDINGS_TABLE": "test-findings-table",
            "ADL_AGENT_QUALITY_AGGREGATE_TABLE": "test-aggregate-table",
            "ADL_RECEIPT_RETENTION_DAYS": "90",
            "ADL_AWS_REGION": "us-east-1",
            "ADL_TRACING_ENABLED": "false",
        },
    )

    assert completed.stdout == ""


def test_cookie_refresh_preserves_the_cookie_without_emitting_helper_progress(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADL_REFRESH", "fictional-refresh")
    monkeypatch.setenv("ADL_CSRF", "fictional-csrf")
    monkeypatch.delenv("ADL_SESSION", raising=False)

    def noisy_refresh(_session: object, _refresh: str, _csrf: str) -> str:
        sys.stdout.write("refresh helper progress\n")
        return "fictional-session"

    monkeypatch.setattr(probe, "_refreshed_session", noisy_refresh)

    assert probe._cookies(  # pyright: ignore[reportPrivateUsage]
        object()  # pyright: ignore[reportArgumentType]
    ) == (
        "adl_session=fictional-session; adl_csrf=fictional-csrf",
        "fictional-csrf",
    )
    assert capsys.readouterr().out == ""


def _summary_record() -> ConversationAuditRecordV6:
    return ConversationAuditRecordV6(
        audit_id="audit-01",
        correlation_id="123e4567-e89b-42d3-a456-426614174000",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        operation_category=AuditOperationCategoryV2.COMPOSITION,
        operation_outcome=AuditOperationOutcomeV2.SUCCEEDED,
        publication_outcome=AuditPublicationOutcomeV2.PUBLISHED,
        terminal_outcome=AuditTerminalOutcomeV2.COMPLETE,
        runtime_version=MANAGED_HARNESS_RUNTIME_V3,
        prompt_catalog_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        page_schema_version="answer.markdown.v1",
        response_contract_version=ANSWER_STREAM_CONTRACT,
        evaluation_admission_ref=None,
        created_at="2026-09-02T12:00:00+00:00",
        trajectory=(
            AuditToolStepV3(
                ordinal=1,
                tool_name="catalog_list_metrics",
                disposition=AuditToolStepDispositionV3.SUCCEEDED,
                step_ref="7a1c9f2e-3b4d-4e5f-8a6b-1c2d3e4f5a6b",
            ),
            AuditToolStepV3(
                ordinal=2,
                tool_name="catalog_describe_metric",
                disposition=AuditToolStepDispositionV3.REFUSED,
                step_ref="8b2d0a3f-4c5e-4f60-9b7c-2d3e4f5a6b7c",
            ),
        ),
        principal="5fdbe1d5-7901-4fee-9d1d-518103789c94",
        released_character_count=42,
    )


def test_probe_projects_only_sanitized_audit_classifications_and_trajectory() -> None:
    record = _summary_record()
    summary = probe._audit_receipt_summary(record)  # pyright: ignore[reportPrivateUsage]
    assert summary == {
        "runtime_version": MANAGED_HARNESS_RUNTIME_V3,
        "response_contract_version": "conversation-stream.v4",
        "trace_present": True,
        "trajectory": [
            {
                "ordinal": 1,
                "tool_name": "catalog_list_metrics",
                "disposition": "succeeded",
                "violations": [],
            },
            {
                "ordinal": 2,
                "tool_name": "catalog_describe_metric",
                "disposition": "refused",
                "violations": [],
            },
        ],
        "released_character_count": 42,
        "operation_category": "composition",
        "operation_outcome": "succeeded",
        "publication_outcome": "published",
        "terminal_outcome": "complete",
    }
    assert {"audit_id", "correlation_id", "trace_id", "step_ref"}.isdisjoint(str(summary))


def test_the_probe_reads_no_record_table_directly() -> None:
    """ADR-078 §5: receipts, findings, and evaluation come only through the review API."""
    source = (_ROOT / "validation" / "probe_agent_quality_loop.py").read_text(encoding="utf-8")
    for name in (
        "agentQualityAuditTableName",
        "agentQualityFindingsTableName",
        "evaluationDeliveriesTableName",
        "evaluationWriterReceiptsTableName",
        "evaluationVerdictsTableName",
        "DynamoAgentQualityAuditStore",
    ):
        assert name not in source


def test_service_polling_uses_increasing_intervals_with_a_responsive_cap() -> None:
    intervals = probe._poll_intervals()  # pyright: ignore[reportPrivateUsage]

    assert [next(intervals) for _ in range(8)] == [2, 3, 5, 8, 13, 13, 13, 13]


@pytest.mark.covers("ADR-078")
def test_no_validation_probe_reads_a_record_table_directly() -> None:
    """ADR-078 §2: probes read receipts only through the review API, which the read deny keeps."""
    offenders = sorted(
        path.name
        for path in (_ROOT / "validation").glob("*.py")
        if any(
            name in path.read_text(encoding="utf-8")
            for name in (
                "DynamoAgentQualityAuditStore",
                "DynamoAuditFindingStore",
                "DynamoEvaluationVerdictStore",
                "DynamoEvaluationDeliveryStore",
                "DynamoEvaluationWriterReceiptStore",
            )
        )
    )
    assert offenders == []
