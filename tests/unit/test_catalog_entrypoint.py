# SPDX-License-Identifier: MIT-0
"""The catalog Lambda entry point: routing, refusals, and a JSON-encodable envelope.

**This file belongs in the unit tier, and that placement is itself the assertion.** The unit loop
runs with no AWS credentials and no network by ADR-014. `catalog` reads the metric dictionary out of
its own deployment artefact and returns canonical grounding in the tool result without an outbound
store. Its distinct business-data permissions are why it remains separate from `measurement`.

Its measurement sibling's composition is exercised from the built bundle against deployed dev; its
caller gate is unit-tested in `test_tool_caller_gate`.

What is exercised here is the seam between the transport and the tool — the prefixed tool name
reaching an enum, a classified error becoming a refusal rather than a raise, and the envelope
surviving `json.dumps`. The Gateway contract itself is `test_gateway_invocation`. Every context
carries a valid propagated bearer (ADR-077 §5), because a tool answers nobody it cannot verify.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from aws_lambda_powertools import Logger
from fictional_tokens import fictional_jwt
from tool_caller_support import gateway_token, import_tool_entrypoint, with_bearer

from adapters.inbound.tool_handlers.catalog import CatalogTool
from adapters.outbound.telemetry.reporting import GatewayTerminalLog, report_lifecycle
from domain.narrative.publication import RenderingKind

catalog_entrypoint = import_tool_entrypoint("adapters.inbound.tool_handlers.catalog_entrypoint")

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-006", "ADR-009", "G-36")]

MESSAGE_ID = "mcp-message-under-test"
TARGET = "catalog"
GATEWAY_CHANNEL: dict[str, list[str] | bool] = {
    "context_custom_keys": [
        "bedrockAgentCoreGatewayId",
        "bedrockAgentCoreMcpMessageId",
        "bedrockAgentCoreMessageVersion",
        "bedrockAgentCorePropagatedHeaders",
        "bedrockAgentCoreTargetId",
        "bedrockAgentCoreToolName",
    ],
    "argument_keys": [],
    "bearer_channel_seen": True,
    "propagated_header_names": ["Authorization"],
}
"""What `_invoke` carries, by key name: the Gateway's own context keys, the propagated bearer,
and no arguments."""


@dataclass(frozen=True)
class _ClientContext:
    custom: dict[str, Any]


@dataclass(frozen=True)
class _LambdaContext:
    """The attributes Powertools reads, plus the part Gateway populates.

    The logger decorator reads every one of these off the context, so an incomplete object fails
    the test inside Powertools for a reason that has nothing to do with the handler.
    """

    client_context: _ClientContext
    function_name: str = "adlytics-catalog"
    function_version: str = "$LATEST"
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:000000000000:function:adlytics-catalog"
    memory_limit_in_mb: int = 512
    aws_request_id: str = "00000000-0000-4000-8000-000000000000"

    def get_remaining_time_in_millis(self) -> int:
        return 300_000


def _invoke(tool: str, **arguments: Any) -> dict[str, Any]:
    """Call the handler as Gateway would: a flat event, and the tool name prefixed."""
    context = _LambdaContext(
        client_context=_ClientContext(
            custom=with_bearer(
                {
                    "bedrockAgentCoreToolName": f"{TARGET}___{tool}",
                    "bedrockAgentCoreMcpMessageId": MESSAGE_ID,
                    "bedrockAgentCoreGatewayId": "gw-under-test",
                    "bedrockAgentCoreTargetId": "tgt-under-test",
                    "bedrockAgentCoreMessageVersion": "1.0",
                }
            )
        )
    )
    return catalog_entrypoint.handler(arguments, context)


class TestItRoutesThePrefixedName:
    def test_listing_metrics_answers(self) -> None:
        """The whole reason `strip_target_prefix` exists, asserted through the handler.

        A handler comparing the raw `catalog___catalog_list_metrics` against its own enum would
        refuse every correctly registered target, and the refusal would read as a routing bug.
        """
        answer = _invoke(CatalogTool.LIST_METRICS.value)
        assert answer.get("refused") is not True
        assert answer["metadata"]["row_count"] > 0

    def test_describing_one_metric_answers(self) -> None:
        answer = _invoke(CatalogTool.DESCRIBE_METRIC.value, metric_name="retail_revenue_shipped")
        assert answer.get("refused") is not True
        assert answer["narrative"]

    def test_an_unprefixed_name_still_routes(self) -> None:
        """So a console test invocation does not have to fake a prefix Gateway would have added."""
        context = _LambdaContext(
            client_context=_ClientContext(
                custom=with_bearer(
                    {
                        "bedrockAgentCoreToolName": CatalogTool.LIST_METRICS.value,
                        "bedrockAgentCoreMcpMessageId": MESSAGE_ID,
                    }
                )
            )
        )
        assert catalog_entrypoint.handler({}, context).get("refused") is not True


class TestGatewayTelemetryBoundary:
    def test_answer_log_uses_only_the_local_reference_and_closed_classification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[GatewayTerminalLog] = []

        def capture_lifecycle(event: GatewayTerminalLog, _logger: Logger) -> None:
            events.append(event)

        monkeypatch.setattr(catalog_entrypoint, "report_lifecycle", capture_lifecycle)

        _invoke(CatalogTool.LIST_METRICS.value)

        projection = events[-1].projection
        assert projection == {
            "operation": CatalogTool.LIST_METRICS.value,
            "component": "gateway_target",
            "phase": "terminal",
            "outcome": "answered",
            "result_class": "answer",
            "investigation_next": "inspect_managed_tool_trace",
            "caller_verified": True,
            "gateway_message_ref": MESSAGE_ID,
            **GATEWAY_CHANNEL,
        }
        assert {
            "correlation_id",
            "tool",
            "rows",
            "result",
            "traceparent",
            "tracestate",
            "baggage",
        }.isdisjoint(projection)


class TestTheEnvelopeCrossesTheWireIntact:
    def test_it_is_json_encodable(self) -> None:
        """A Gateway target must return JSON the gateway can parse.

        `json.dumps` over the whole envelope is the only check that covers every field at once —
        the response carries enums and datetimes, and a missed conversion is a runtime failure on
        the first real call rather than anything the type checker sees.
        """
        encoded = json.dumps(_invoke(CatalogTool.LIST_METRICS.value))
        assert len(encoded) > 0

    def test_enums_arrive_as_their_values(self) -> None:
        # `fresh`, not `FreshnessStatus.FRESH`. A model should not have to read Python reprs.
        assert "FreshnessStatus." not in json.dumps(_invoke(CatalogTool.LIST_METRICS.value))

    def test_the_metadata_a_user_must_see_is_present(self) -> None:
        metadata = _invoke(CatalogTool.LIST_METRICS.value)["metadata"]
        correlation_id = metadata["correlation_id"]
        assert correlation_id != MESSAGE_ID
        assert str(UUID(correlation_id)) == correlation_id
        assert metadata["computed_at"]
        assert isinstance(metadata["suppression"], dict)
        assert metadata["grounding"]

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            (CatalogTool.LIST_METRICS.value, {}),
            (
                CatalogTool.DESCRIBE_METRIC.value,
                {"metric_name": "retail_revenue_shipped"},
            ),
        ],
    )
    def test_direct_catalog_evidence_crosses_the_release_boundary(
        self, tool: str, arguments: dict[str, str]
    ) -> None:
        answer = _invoke(tool, **arguments)
        grounding = answer["metadata"]["grounding"]
        renderings = grounding["renderings"]
        if tool == CatalogTool.LIST_METRICS.value:
            claims = [item for item in renderings if item["kind"] == RenderingKind.CLAIM.value]
            assert len(claims) == 1
            assert claims[0]["text"].startswith("The platform declares 124 metrics")
            assert len(grounding["collections"]) == 8
            assert "receipt_token" not in grounding
            return
        descriptor = next(
            item for item in renderings if item["kind"] == RenderingKind.CONTEXT.value
        )

        assert descriptor["rendering_id"]
        assert descriptor["text"]
        assert descriptor["source_paths"]


class TestAClassifiedErrorIsARefusalRatherThanARaise:
    def test_describing_nothing_is_refused_pointing_at_the_other_tool(self) -> None:
        """A refusal the model can act on, rather than an error it can only retry."""
        answer = _invoke(CatalogTool.DESCRIBE_METRIC.value)
        assert answer["refused"] is True
        assert CatalogTool.LIST_METRICS.value in str(answer["reason"])
        assert answer["metadata"]["grounding"]["renderings"][0]["text"] == answer["reason"]

    def test_unsupported_advertiser_breakdown_is_a_governed_refusal_rendering(self) -> None:
        answer = _invoke(
            CatalogTool.DESCRIBE_METRIC.value,
            metric_name="ad_spend",
            grain="advertiser",
        )

        assert answer["refused"] is True
        assert answer["reason"] == (
            "The requested advertiser breakdown is unsupported for ad_spend. A figure at an "
            "unsupported breakdown is computable and wrong, which is worse than absent. Available "
            "instead: date, ecosystem, platform, channel_group, channel, campaign, ad_product, "
            "marketplace."
        )
        rendering = answer["metadata"]["grounding"]["renderings"][0]
        assert rendering["text"] == answer["reason"]

    def test_an_unknown_tool_is_refused_naming_the_real_ones(self) -> None:
        """A Gateway target pointed at the wrong function is a registration mistake.

        Naming what exists turns a silent routing failure into one that says what was expected.
        """
        answer = _invoke("catalog_invent_a_metric")
        assert answer["refused"] is True
        assert "catalog_invent_a_metric" not in str(answer["reason"])
        assert CatalogTool.LIST_METRICS.value in str(answer["reason"])

    def test_a_refusal_never_relabels_the_gateway_message_reference_as_correlation(self) -> None:
        correlation_id = _invoke(CatalogTool.DESCRIBE_METRIC.value)["correlation_id"]
        assert correlation_id != MESSAGE_ID
        assert str(UUID(correlation_id)) == correlation_id

    def test_an_invocation_with_no_tool_name_is_refused(self) -> None:
        """Not a Gateway invocation at all, so there is nothing to route.

        Raised rather than answered, because guessing a tool from an empty context would answer a
        question nobody asked.
        """
        from domain.shared_kernel.errors import ValidationError  # noqa: PLC0415 — local to one test

        context = _LambdaContext(
            client_context=_ClientContext(
                custom=with_bearer({"bedrockAgentCoreMcpMessageId": MESSAGE_ID})
            )
        )
        with pytest.raises(ValidationError):
            catalog_entrypoint.handler({}, context)


def test_governed_catalog_refusal_is_a_blocking_refusal_rendering() -> None:
    answer = _invoke(CatalogTool.DESCRIBE_METRIC.value)

    rendering = answer["metadata"]["grounding"]["renderings"][0]
    assert rendering["kind"] == RenderingKind.REFUSAL.value
    assert rendering["text"] == answer["reason"]


def test_unexpected_catalog_failure_is_reported_and_reraised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[GatewayTerminalLog] = []

    def capture_lifecycle(event: GatewayTerminalLog, _logger: Logger) -> None:
        events.append(event)

    def fail(_request: object):
        raise RuntimeError("fictional catalog failure")

    monkeypatch.setattr(catalog_entrypoint, "report_lifecycle", capture_lifecycle)
    monkeypatch.setattr(catalog_entrypoint, "handle", fail)

    with pytest.raises(RuntimeError, match="fictional catalog failure"):
        _invoke(CatalogTool.LIST_METRICS.value)

    assert events[-1].projection == {
        "operation": CatalogTool.LIST_METRICS.value,
        "component": "gateway_target",
        "phase": "terminal",
        "outcome": "failed",
        "result_class": "unexpected_failure",
        "investigation_next": "inspect_tool_boundary",
        "caller_verified": True,
        "gateway_message_ref": MESSAGE_ID,
        **GATEWAY_CHANNEL,
    }


@pytest.mark.covers("ADR-077")
def test_the_terminal_record_names_the_bearer_channel_keys_and_never_their_values(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Probe P3 reads which channel an interceptor-set `Authorization` arrived on, by name only."""
    bearer = "Bearer " + fictional_jwt("secret-claims", signature="secret-signature")
    # A declared metric, so the tool answers: a refusal legitimately names the metric it refused.
    argument = "retail_revenue_shipped"
    events: list[GatewayTerminalLog] = []

    def capture_lifecycle(event: GatewayTerminalLog, logger: Logger) -> None:
        events.append(event)
        report_lifecycle(event, logger)

    monkeypatch.setattr(catalog_entrypoint, "report_lifecycle", capture_lifecycle)
    caplog.set_level("DEBUG")
    propagated = gateway_token()
    context = _LambdaContext(
        client_context=_ClientContext(
            custom=with_bearer(
                {
                    "bedrockAgentCoreToolName": f"{TARGET}___{CatalogTool.DESCRIBE_METRIC.value}",
                    "bedrockAgentCoreMcpMessageId": MESSAGE_ID,
                    "Authorization": bearer,
                },
                propagated,
            )
        )
    )
    catalog_entrypoint.handler({"metric_name": argument, "grain": "date"}, context)

    projection = events[-1].projection
    assert projection["caller_verified"] is True
    assert {key: projection[key] for key in GATEWAY_CHANNEL} == {
        "context_custom_keys": [
            "Authorization",
            "bedrockAgentCoreMcpMessageId",
            "bedrockAgentCorePropagatedHeaders",
            "bedrockAgentCoreToolName",
        ],
        "argument_keys": ["grain", "metric_name"],
        "bearer_channel_seen": True,
        "propagated_header_names": ["Authorization"],
    }
    logged = "".join(repr(vars(record)) for record in caplog.records)
    assert "managed lifecycle" in logged
    assert "context_custom_keys" in logged
    for secret in (bearer, bearer.split(" ")[1], argument, propagated, *propagated.split(".")):
        assert secret not in logged
