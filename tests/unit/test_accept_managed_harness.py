# SPDX-License-Identifier: MIT-0
"""ADR-079 deployment and evaluation-chain attestation in the managed-Harness acceptance runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import accept_managed_harness as harness  # noqa: E402

from domain.conversation.audit import (  # noqa: E402
    MANAGED_HARNESS_RUNTIME_V3,
    AuditOperationCategoryV2,
    AuditOperationOutcomeV2,
    AuditPublicationOutcomeV2,
    AuditTerminalOutcomeV2,
    AuditToolStepDispositionV3,
    AuditToolStepV3,
)
from domain.evaluation.linkage import (  # noqa: E402
    EvaluationDeliveryFamilyV1,
    EvaluationDeliveryOutcomeV1,
    EvaluationNotDispatchedReasonV1,
)
from domain.evaluation.release import (  # noqa: E402
    EvaluationFamilyV1,
    EvaluationReleaseEvaluatorV1,
    EvaluationReleaseV1,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-079")]
ReviewedDelivery = harness.ReviewedDelivery
ReviewedReceipt = harness.ReviewedReceipt

_DECIDED_AT = "2026-09-27T12:00:00+00:00"
_GATE_CONFIG = "adlytics_dev_released_v4-AbCdE12345"
_JUDGE = EvaluationReleaseEvaluatorV1(
    evaluator_id="adlytics_dev_helpfulness_v4-Helpful123",
    evaluator_name="helpfulness_v4",
    revision="r1",
    model_id="fictional.judge-model-v1:0",
    prompt_digest="a" * 64,
)


def _stack(*resources: dict[str, object]) -> harness._StackRecord:  # pyright: ignore[reportPrivateUsage]
    return harness._StackRecord(name="fictional", resources=resources)  # pyright: ignore[reportPrivateUsage]


def _filter(logical: str) -> dict[str, object]:
    return {
        "ResourceType": "AWS::Logs::SubscriptionFilter",
        "LogicalResourceId": logical,
        "PhysicalResourceId": f"physical-{logical}",
    }


def _gate(logical: str = "GateV4OnlineEvaluationGateABCD") -> dict[str, object]:
    return {
        "ResourceType": "Custom::AgentCoreOnlineEvaluation",
        "LogicalResourceId": logical,
        "PhysicalResourceId": _GATE_CONFIG,
    }


def _release(family: EvaluationFamilyV1, config_id: str | None) -> EvaluationReleaseV1:
    return EvaluationReleaseV1(
        family=family,
        enabled=config_id is not None,
        config_id=config_id,
        evaluators=() if config_id is None else (_JUDGE,),
        decided_at=_DECIDED_AT,
    )


def _receipt(
    outcome: EvaluationDeliveryOutcomeV1,
    reason: EvaluationNotDispatchedReasonV1 | None = None,
) -> ReviewedDelivery:
    families = (
        (
            EvaluationDeliveryFamilyV1(
                family=EvaluationFamilyV1.V4, config_id=_GATE_CONFIG, evaluators=(_JUDGE,)
            ),
        )
        if outcome is EvaluationDeliveryOutcomeV1.DISPATCHED
        else ()
    )
    return ReviewedDelivery(outcome=outcome, not_dispatched_reason=reason, families=families)


def test_subscribed_families_are_exactly_the_current_ones() -> None:
    assert harness._subscribed_families(  # pyright: ignore[reportPrivateUsage]
        _stack(_filter("ResultGroupSubscriptionV4ABCD"))
    ) == (EvaluationFamilyV1.V4,)


@pytest.mark.parametrize(
    "resources",
    [
        # The retired V3 family's group, alone or still subscribed beside V4.
        (_filter("ResultGroupSubscriptionV3ABCD"),),
        (_filter("ResultGroupSubscriptionV3ABCD"), _filter("ResultGroupSubscriptionV4ABCD")),
        (_filter("ResultGroupSubscriptionV4ABCD"), _filter("DirectedProbeSubscription")),
        (),
    ],
    ids=["retired-v3-only", "retired-v3-beside-v4", "foreign-filter", "none"],
)
def test_subscriptions_other_than_the_current_families_are_refused(
    resources: tuple[dict[str, object], ...],
) -> None:
    with pytest.raises(harness._StageError) as error:  # pyright: ignore[reportPrivateUsage]
        harness._subscribed_families(_stack(*resources))  # pyright: ignore[reportPrivateUsage]
    assert error.value.reason == "evaluation_subscription_mismatch"


def test_the_v4_stack_holds_exactly_one_gate_whose_physical_id_is_the_config() -> None:
    assert harness._attest_evaluation_gate(_stack(_gate())) == _GATE_CONFIG  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("resources", "reason"),
    [
        ((), "evaluation_gate_binding_mismatch"),
        ((_gate(), _gate("GateV4Second")), "evaluation_gate_binding_mismatch"),
        (
            (
                _gate(),
                {
                    "ResourceType": "AWS::BedrockAgentCore::OnlineEvaluationConfig",
                    "LogicalResourceId": "OnlineQualityV4",
                    "PhysicalResourceId": "retired",
                },
            ),
            "retired_online_evaluation_config_present",
        ),
    ],
)
def test_a_missing_duplicated_or_retired_online_evaluation_owner_is_refused(
    resources: tuple[dict[str, object], ...], reason: str
) -> None:
    with pytest.raises(harness._StageError) as error:  # pyright: ignore[reportPrivateUsage]
        harness._attest_evaluation_gate(_stack(*resources))  # pyright: ignore[reportPrivateUsage]
    assert error.value.reason == reason


def test_enabled_releases_must_be_subscribed_and_name_the_stack_gate() -> None:
    v4 = _release(EvaluationFamilyV1.V4, _GATE_CONFIG)
    families = (EvaluationFamilyV1.V4,)

    assert harness._enabled_release_bindings((v4,), _GATE_CONFIG, families) == (v4,)  # pyright: ignore[reportPrivateUsage]
    assert (
        harness._enabled_release_bindings(  # pyright: ignore[reportPrivateUsage]
            (_release(EvaluationFamilyV1.V4, None),), _GATE_CONFIG, families
        )
        == ()
    )
    assert harness._enabled_release_bindings((None,), _GATE_CONFIG, families) == ()  # pyright: ignore[reportPrivateUsage]
    for releases, subscribed, reason in (
        (
            (_release(EvaluationFamilyV1.V4, "adlytics_dev_released_v4-Other00000"),),
            families,
            "evaluation_config_binding_mismatch",
        ),
        ((v4,), (), "evaluation_release_unsubscribed"),
    ):
        with pytest.raises(harness._StageError) as error:  # pyright: ignore[reportPrivateUsage]
            harness._enabled_release_bindings(releases, _GATE_CONFIG, subscribed)  # pyright: ignore[reportPrivateUsage]
        assert error.value.reason == reason


def test_an_undispatched_receipt_is_a_terminal_state_with_nothing_emitted() -> None:
    receipt = _receipt(
        EvaluationDeliveryOutcomeV1.NOT_DISPATCHED,
        EvaluationNotDispatchedReasonV1.EVALUATION_NOT_ENABLED,
    )

    assert (
        harness._dispatch_state(receipt, writer_present=False, result_count=0)  # pyright: ignore[reportPrivateUsage]
        == "not_dispatched_evaluation_not_enabled"
    )
    for writer_present, result_count in ((True, 0), (False, 1)):
        with pytest.raises(harness._StageError) as error:  # pyright: ignore[reportPrivateUsage]
            harness._dispatch_state(  # pyright: ignore[reportPrivateUsage]
                receipt, writer_present=writer_present, result_count=result_count
            )
        assert error.value.reason == "undispatched_evaluation_emitted"


def test_a_dispatched_receipt_defers_to_the_writer_and_verdict_chain() -> None:
    receipt = _receipt(EvaluationDeliveryOutcomeV1.DISPATCHED)

    assert harness._dispatch_state(receipt, writer_present=False, result_count=0) is None  # pyright: ignore[reportPrivateUsage]


_ISSUER = "https://issuer.example/v1"


def _authorizer(**overrides: object) -> dict[str, object]:
    return {
        "discoveryUrl": f"{_ISSUER}/.well-known/openid-configuration",
        "allowedAudience": ["adlytics-dev-harness"],
        "allowedClients": ["adlytics-dev-conversation-route"],
    } | overrides


@pytest.mark.covers("ADR-077")
def test_the_harness_admits_only_a_route_minted_token() -> None:
    assert harness.harness_authorizer_matches(_authorizer(), issuer_url=_ISSUER, environment="dev")
    for bad in (
        # A Cognito pool's discovery, the BFF client, no audience: the pre-2026-09-26 shape.
        _authorizer(
            discoveryUrl="https://cognito-idp.us-east-1.amazonaws.com/pool/.well-known/openid-configuration"
        ),
        _authorizer(allowedClients=["bff-client"]),
        _authorizer(allowedAudience=[]),
        _authorizer(allowedAudience=["adlytics-dev-gateway"]),
        _authorizer(allowedClients=["adlytics-dev-conversation-route", "another"]),
    ):
        assert not harness.harness_authorizer_matches(bad, issuer_url=_ISSUER, environment="dev")
    assert not harness.harness_authorizer_matches(None, issuer_url=_ISSUER, environment="dev")


@pytest.mark.covers("ADR-077")
def test_the_route_role_may_invoke_exactly_the_route_exchange() -> None:
    exchange = "arn:aws:lambda:us-east-1:000000000000:function:fictional-route-exchange"
    scoped = [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": exchange}]
    assert harness.invoke_function_scope(scoped) == {exchange}
    broad = [{"Effect": "Allow", "Action": ["lambda:*"], "Resource": ["*"]}]
    assert harness.invoke_function_scope(broad) == {"*"}
    assert (
        harness.invoke_function_scope([{"Effect": "Deny", **scoped[0]} | {"Effect": "Deny"}])
        == set()
    )


def _record(operation: str, *, verified: bool = True) -> dict[str, object]:
    return {"component": "gateway_target", "operation": operation, "caller_verified": verified}


@pytest.mark.covers("ADR-077")
def test_tool_coverage_requires_each_expected_tool_verified() -> None:
    expected = frozenset({"catalog_describe_metric", "measurement_metric_by_grain"})
    both = [_record("catalog_describe_metric"), _record("measurement_metric_by_grain")]
    assert harness.tool_coverage_verdict(both, expected) is None
    # Other tools may run too: the route offers every declared tool.
    assert harness.tool_coverage_verdict([*both, _record("catalog_list_metrics")], expected) is None
    assert (
        harness.tool_coverage_verdict([_record("catalog_describe_metric")], expected)
        == "expected_tool_not_called"
    )
    assert (
        harness.tool_coverage_verdict(
            [*both[:1], _record("measurement_metric_by_grain", verified=False)], expected
        )
        == "tool_caller_unverified"
    )


@pytest.mark.covers("ADR-077")
def test_a_no_tool_turn_must_reach_no_tool() -> None:
    assert harness.tool_coverage_verdict([], frozenset()) is None
    assert (
        harness.tool_coverage_verdict([_record("catalog_list_metrics")], frozenset())
        == "no_tool_custody_unexpected"
    )


def _limitation(steps: tuple[tuple[str, str], ...], **outcomes: str) -> object:
    values = {
        "operation": "succeeded",
        "publication": "published",
        "terminal": "complete",
    } | outcomes
    return SimpleNamespace(
        operation_outcome=SimpleNamespace(value=values["operation"]),
        publication_outcome=SimpleNamespace(value=values["publication"]),
        terminal_outcome=SimpleNamespace(value=values["terminal"]),
        trajectory=tuple(
            AuditToolStepV3(
                ordinal=index,
                tool_name=name,
                disposition=AuditToolStepDispositionV3(disposition),
                step_ref="ref" if disposition in {"succeeded", "refused", "accepted"} else None,
            )
            for index, (name, disposition) in enumerate(steps, start=1)
        ),
    )


@pytest.mark.covers("ADR-006")
def test_a_limitation_is_published_with_no_governed_tool_succeeding_in_its_place() -> None:
    check = harness.limitation_published_without_substitute
    # A streamed answer that called no tool at all (ADR-083).
    assert check(_limitation(()))  # type: ignore[arg-type]
    assert check(_limitation((("catalog_describe_metric", "refused"),), operation="refused"))  # type: ignore[arg-type]
    assert not check(_limitation((("catalog_describe_metric", "succeeded"),)))  # type: ignore[arg-type]
    assert not check(_limitation((), publication="not_applicable"))  # type: ignore[arg-type]
    assert not check(_limitation((), operation="failed"))  # type: ignore[arg-type]


def _span(name: str, span_id: str, **extra: object) -> dict[str, object]:
    return {"name": name, "spanId": span_id, "status": {"code": "UNSET"}} | extra


_ROUTE_SPANS = [
    _span("conversation.turn", "a" * 16),
    _span("conversation.route_exchange", "b" * 16),
    _span("Bedrock AgentCore.InvokeHarness", "c" * 16),
]


@pytest.mark.covers("ADR-009")
def test_the_turn_trace_needs_every_route_span_and_no_error() -> None:
    topology = harness.trace_topology(_ROUTE_SPANS)
    assert (topology["gateway"], topology["harness_internal_spans"]) == (
        "separate_trace",
        "not_exported",
    )
    with pytest.raises(harness._StageError, match="trace_continuity"):  # pyright: ignore[reportPrivateUsage]
        harness.trace_topology(_ROUTE_SPANS[:2])
    with pytest.raises(harness._StageError, match="trace_continuity"):  # pyright: ignore[reportPrivateUsage]
        harness.trace_topology(
            [*_ROUTE_SPANS, _span("DynamoDB.PutItem", "d" * 16, status={"code": "ERROR"})]
        )


_DIGEST = "sha256:" + "a" * 64
_ANSWER = "Fictional **answer** with 3 metrics."


def _streamed(**overrides: object) -> ReviewedReceipt:
    values: dict[str, object] = {
        "audit_id": "audit-01",
        "correlation_id": "123e4567-e89b-42d3-a456-426614174000",
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "created_at": _DECIDED_AT,
        "runtime_version": MANAGED_HARNESS_RUNTIME_V3,
        "prompt_catalog_digest": _DIGEST,
        "response_contract_version": "conversation-stream.v4",
        "operation_category": AuditOperationCategoryV2.COMPOSITION,
        "operation_outcome": AuditOperationOutcomeV2.SUCCEEDED,
        "publication_outcome": AuditPublicationOutcomeV2.PUBLISHED,
        "terminal_outcome": AuditTerminalOutcomeV2.COMPLETE,
        "failure_error_code": None,
        "completion_refusal_stage": None,
        "trajectory": (
            AuditToolStepV3(
                ordinal=1,
                tool_name="catalog_list_metrics",
                disposition=AuditToolStepDispositionV3.SUCCEEDED,
                step_ref="7a1c9f2e-3b4d-4e5f-8a6b-1c2d3e4f5a6b",
            ),
        ),
        "tool_custody_digest": _DIGEST,
        "presentation_plan_digest": None,
        "public_block_kinds": (),
        "public_block_count": 0,
        "evaluation_admission_ref": "admission-01",
        "expires_at": None,
        "page_schema_version": "answer.markdown.v1",
        "released_character_count": len(_ANSWER),
    }
    return ReviewedReceipt(**(values | overrides))  # type: ignore[arg-type]


@pytest.mark.covers("ADR-083")
def test_a_streamed_receipt_counts_the_released_text_and_holds_no_plan() -> None:
    assert harness.streamed_receipt_mismatch(_streamed(), _ANSWER) is None
    # A turn that called no tool streams its answer with an empty trajectory.
    assert harness.streamed_receipt_mismatch(_streamed(trajectory=()), _ANSWER) is None


@pytest.mark.covers("ADR-083")
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"response_contract_version": "public-block-stream.v3"}, "answer_stream_contract_missing"),
        (
            {"terminal_outcome": AuditTerminalOutcomeV2.CLIENT_DISCONNECTED},
            "audit_terminal_incomplete",
        ),
        ({"publication_outcome": AuditPublicationOutcomeV2.REFUSED}, "audit_terminal_incomplete"),
        ({"page_schema_version": "presentation.v2"}, "answer_format_missing"),
        (
            {
                "trajectory": (
                    AuditToolStepV3(
                        ordinal=1,
                        tool_name="presentation_publish",
                        disposition=AuditToolStepDispositionV3.ACCEPTED,
                        step_ref="finalizer-message-1",
                    ),
                )
            },
            "audit_trajectory_finalizer_present",
        ),
        (
            {
                "trajectory": (
                    AuditToolStepV3(
                        ordinal=1,
                        tool_name="catalog_list_metrics",
                        disposition=AuditToolStepDispositionV3.SUCCEEDED,
                        step_ref="7a1c9f2e-3b4d-4e5f-8a6b-1c2d3e4f5a6b",
                        selected_into_plan=True,
                    ),
                )
            },
            "audit_trajectory_finalizer_present",
        ),
        (
            {
                "trajectory": (
                    AuditToolStepV3(
                        ordinal=1,
                        tool_name="catalog_list_metrics",
                        disposition=AuditToolStepDispositionV3.SUCCEEDED,
                        step_ref=None,
                    ),
                )
            },
            "audit_trajectory_unanchored",
        ),
        ({"tool_custody_digest": None}, "managed_publication_metadata_missing"),
        ({"prompt_catalog_digest": None}, "managed_publication_metadata_missing"),
        ({"presentation_plan_digest": _DIGEST}, "block_publication_present"),
        ({"public_block_count": 1}, "block_publication_present"),
        ({"released_character_count": len(_ANSWER) - 1}, "released_text_count_mismatch"),
    ],
    ids=[
        "block-stream-contract",
        "client-disconnected",
        "refused-publication",
        "block-page-schema",
        "finalizer-step",
        "plan-selection",
        "unanchored-governed-step",
        "no-custody-digest",
        "no-prompt-digest",
        "plan-digest",
        "block-count",
        "character-count",
    ],
)
def test_a_receipt_that_is_not_the_streamed_shape_is_refused(
    overrides: dict[str, object], reason: str
) -> None:
    assert harness.streamed_receipt_mismatch(_streamed(**overrides), _ANSWER) == reason


@pytest.mark.covers("ADR-083")
def test_the_declared_tools_are_catalog_and_measurement_only() -> None:
    names = harness._tool_contract()  # pyright: ignore[reportPrivateUsage]
    assert set(names) == set(harness._DIRECT_QUESTIONS)  # pyright: ignore[reportPrivateUsage]
    assert {harness._tool_family(name) for name in names} == {"catalog", "measurement"}  # pyright: ignore[reportPrivateUsage]
    assert harness._qualified_tool("catalog_list_metrics") == (  # pyright: ignore[reportPrivateUsage]
        "@adlytics/catalog___catalog_list_metrics"
    )
    with pytest.raises(harness._StageError) as error:  # pyright: ignore[reportPrivateUsage]
        harness._tool_family("presentation_publish")  # pyright: ignore[reportPrivateUsage]
    assert error.value.reason == "unknown_tool_family"


def _frame(kind: str, **fields: object) -> bytes:
    event = {"schema_version": "ConversationStream.v4", "kind": kind, **fields}
    return f"data:{json.dumps(event, separators=(',', ':'))}\n\n".encode()


@pytest.mark.covers("ADR-083")
def test_arrival_times_the_first_answer_text_and_counts_its_deltas() -> None:
    chunks = [
        (0.1, _frame("activity", stage="understanding_request")),
        (0.2, b": keep-alive\n\n"),
        (1.5, _frame("text", sequence=1, text="Fictional ")),
        (1.6, _frame("text", sequence=2, text="answer.")),
        (2.0, _frame("done", audit_id="audit-01")),
    ]

    assert harness._arrival(chunks) == harness._Arrival(  # pyright: ignore[reportPrivateUsage]
        first_event_ms=100, first_text_ms=1500, terminal_ms=2000, heartbeats=1, text_deltas=2
    )
    assert harness._released_text(chunks) == "Fictional answer."  # pyright: ignore[reportPrivateUsage]
