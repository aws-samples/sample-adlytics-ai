# SPDX-License-Identifier: MIT-0
"""Reading an AgentCore Gateway tool call, and rendering the envelope back as JSON.

Pure transport logic, so it belongs here rather than in the integration tier: none of it needs a
Gateway, and the parts most likely to be wrong are the ones a live test would be slowest to find.

The prefixed tool name is the reason this file exists. Gateway sends
`${target_name}___${tool_name}` and does not strip it, so a handler comparing the raw value against
its own enum matches nothing and reports an unknown tool for every correctly registered target.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import pytest
from fictional_tokens import fictional_jwt

from adapters.inbound.tool_handlers.gateway import (
    GatewayInvocation,
    channel_keys,
    contracts_root,
    strip_target_prefix,
    to_json_safe,
)
from adapters.outbound.telemetry.reporting import GatewayChannelKeys
from domain.shared_kernel.errors import ValidationError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-006", "G-36")]


@dataclass(frozen=True)
class _ClientContext:
    custom: dict[str, Any]


@dataclass(frozen=True)
class _LambdaContext:
    """The shape AWS gives a handler, reduced to the part Gateway populates."""

    client_context: _ClientContext | None


def _context(**custom: object) -> _LambdaContext:
    return _LambdaContext(client_context=_ClientContext(custom=dict(custom)))


class TestTheToolNameIsPrefixedByGateway:
    def test_the_target_prefix_is_stripped(self) -> None:
        assert (
            strip_target_prefix("measurement___measurement_metric_by_grain")
            == "measurement_metric_by_grain"
        )

    def test_a_name_with_no_prefix_is_returned_unchanged(self) -> None:
        """So a direct invocation does not have to fake a prefix to be routed."""
        assert strip_target_prefix("catalog_list_metrics") == "catalog_list_metrics"

    def test_it_splits_on_the_last_delimiter_not_the_first(self) -> None:
        """The delimiter is three underscores and a tool name may legitimately contain one.

        Splitting on the first occurrence would truncate the tool name rather than the prefix,
        which reads as an unknown tool for a target that was registered correctly.
        """
        assert strip_target_prefix("target___a___b") == "b"

    def test_a_single_underscore_is_not_a_delimiter(self) -> None:
        assert strip_target_prefix("measurement_metric_by_grain") == "measurement_metric_by_grain"


class TestReadingAnInvocation:
    def test_the_event_is_the_arguments_with_no_unwrapping(self) -> None:
        """Gateway sends a flat map of the input schema's properties, not an envelope."""
        invocation = GatewayInvocation.read(
            {"metric_name": "revenue_ordered", "grains": ["date"]},
            _context(
                bedrockAgentCoreToolName="measurement___measurement_metric_by_grain",
                bedrockAgentCoreMcpMessageId="mcp-1",
            ),
        )

        assert invocation.tool_name == "measurement_metric_by_grain"
        assert invocation.arguments == {"metric_name": "revenue_ordered", "grains": ["date"]}

    @pytest.mark.covers("G-36")
    def test_the_gateway_message_reference_comes_from_context_never_arguments(self) -> None:
        """A local reference supplied by the model is one it can fabricate or reuse."""
        invocation = GatewayInvocation.read(
            {"correlation_id": "supplied-by-the-model"},
            _context(
                bedrockAgentCoreToolName="catalog___catalog_list_metrics",
                bedrockAgentCoreMcpMessageId="mcp-2",
            ),
        )

        assert invocation.gateway_message_ref == "mcp-2"

    def test_it_falls_back_to_the_request_id_when_no_message_id_is_present(self) -> None:
        invocation = GatewayInvocation.read(
            {},
            _context(
                bedrockAgentCoreToolName="catalog___catalog_list_metrics",
                bedrockAgentCoreAwsRequestId="req-9",
            ),
        )

        assert invocation.gateway_message_ref == "req-9"

    def test_an_invocation_naming_no_tool_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="which tool"):
            GatewayInvocation.read({}, _context(bedrockAgentCoreMcpMessageId="mcp-3"))

    def test_an_invocation_with_nothing_to_correlate_by_is_refused(self) -> None:
        """Refused rather than given an invented id, which would look like provenance."""
        with pytest.raises(ValidationError, match="message reference"):
            GatewayInvocation.read({}, _context(bedrockAgentCoreToolName="a___b"))

    def test_a_context_without_a_client_context_is_refused_not_crashed(self) -> None:
        """A console test invocation has no client context. It should refuse, naming the absence."""
        with pytest.raises(ValidationError):
            GatewayInvocation.read({}, _LambdaContext(client_context=None))


@pytest.mark.covers("ADR-077")
class TestNamingTheChannelsAnInvocationCarried:
    """Probe P3 diagnostics: key names only, bounded, so no credential can reach a log."""

    def test_names_are_sorted_and_values_never_appear(self) -> None:
        # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
        secret = "Bearer " + fictional_jwt("claims", signature="signature")
        keys = channel_keys(
            {"metric_name": "argument-value", "grains": ["date"]},
            _context(
                bedrockAgentCoreToolName="measurement___x",
                authorization=secret,
                bedrockAgentCoreMcpMessageId="mcp-1",
            ),
        )
        assert keys.projection == {
            "context_custom_keys": [
                "authorization",
                "bedrockAgentCoreMcpMessageId",
                "bedrockAgentCoreToolName",
            ],
            "argument_keys": ["grains", "metric_name"],
            "bearer_channel_seen": True,
            "propagated_header_names": [],
        }
        rendered = json.dumps(keys.projection)
        for value in (secret, "argument-value", "measurement___x", "mcp-1", "date"):
            assert value not in rendered

    @pytest.mark.parametrize(
        "name",
        ["Authorization", "AUTHORIZATION", "x-amzn-authorization", "customAuthorizationHeader"],
    )
    def test_any_custom_key_naming_authorization_marks_the_bearer_channel(self, name: str) -> None:
        keys = channel_keys({}, _context(**{name: "value"}))
        assert keys.bearer_channel_seen is True
        assert keys.context_custom_keys == (name,)

    def test_no_authorization_key_means_no_bearer_channel_even_in_an_argument_name(self) -> None:
        """The flag reads the client context; an argument name is model-authored, not a channel."""
        keys = channel_keys({"authorization": "x"}, _context(bedrockAgentCoreToolName="a___b"))
        assert keys.bearer_channel_seen is False
        assert keys.argument_keys == ("authorization",)

    def test_at_most_32_names_of_at_most_64_characters_are_kept(self) -> None:
        long_name = "a" * 100
        many = {f"key_{index:02d}": index for index in range(40)}
        keys = channel_keys({**many, long_name: 1}, _context(**{long_name: "x"}))
        assert len(keys.argument_keys) == 32
        assert keys.argument_keys[0] == "a" * 64
        assert keys.argument_keys[-1] == "key_30"
        assert keys.context_custom_keys == ("a" * 64,)

    def test_the_bearer_flag_is_decided_before_names_are_bounded(self) -> None:
        name = "x" * 70 + "Authorization"
        keys = channel_keys({}, _context(**{name: "v"}))
        assert keys.context_custom_keys == ("x" * 64,)
        assert keys.bearer_channel_seen is True

    def test_an_absent_client_context_or_a_non_object_event_names_nothing(self) -> None:
        keys = channel_keys(["not", "an", "object"], _LambdaContext(client_context=None))
        assert keys.projection == {
            "context_custom_keys": [],
            "argument_keys": [],
            "bearer_channel_seen": False,
            "propagated_header_names": [],
        }

    @pytest.mark.parametrize(
        "propagated",
        [
            {"Authorization": "Bearer " + fictional_jwt("claims", signature="signature")},
            json.dumps(
                {"Authorization": "Bearer " + fictional_jwt("claims", signature="signature")}
            ),
        ],
    )
    def test_propagated_header_names_are_named_and_mark_the_bearer_channel(
        self, propagated: object
    ) -> None:
        """Gateway delivers propagated headers under an undocumented client-context key."""
        keys = channel_keys({}, _context(bedrockAgentCorePropagatedHeaders=propagated))
        assert keys.propagated_header_names == ("Authorization",)
        assert keys.bearer_channel_seen is True
        assert "eyJ" not in json.dumps(keys.projection)

    def test_propagated_headers_without_authorization_do_not_mark_the_bearer_channel(self) -> None:
        keys = channel_keys({}, _context(bedrockAgentCorePropagatedHeaders={"x-probe": "1"}))
        assert keys.propagated_header_names == ("x-probe",)
        assert keys.bearer_channel_seen is False

    def test_an_unbounded_or_unsorted_projection_cannot_be_constructed(self) -> None:
        with pytest.raises(ValueError, match="bounded"):
            GatewayChannelKeys(("b", "a"), (), bearer_channel_seen=False)
        with pytest.raises(ValueError, match="bounded"):
            GatewayChannelKeys(
                (), tuple(f"k{index:02d}" for index in range(33)), bearer_channel_seen=False
            )
        with pytest.raises(ValueError, match="bounded"):
            GatewayChannelKeys(("a" * 65,), (), bearer_channel_seen=False)


class TestRenderingTheEnvelope:
    def test_a_decimal_becomes_a_string_and_keeps_its_scale(self) -> None:
        """Through `float`, `0.9420` stops stating four decimal places and money loses a cent.

        The envelope's premise is that the tool already rounded and scaled, so the scale is part of
        the answer rather than a presentation choice.
        """
        assert to_json_safe(Decimal("0.9420")) == "0.9420"
        assert to_json_safe(Decimal("17.9400")) == "17.9400"

    def test_an_enum_is_unwrapped_to_its_value(self) -> None:
        class Status(StrEnum):
            FRESH = "fresh"

        assert to_json_safe(Status.FRESH) == "fresh"

    def test_a_datetime_becomes_an_iso_string(self) -> None:
        moment = datetime(2026, 8, 10, 12, 30, tzinfo=UTC)
        assert to_json_safe(moment) == moment.isoformat()

    def test_a_nested_dataclass_is_rendered_by_field(self) -> None:
        @dataclass(frozen=True)
        class Inner:
            amount: Decimal

        @dataclass(frozen=True)
        class Outer:
            inner: Inner
            labels: tuple[str, ...]

        rendered = to_json_safe(Outer(inner=Inner(amount=Decimal("1.5000")), labels=("a", "b")))

        assert rendered == {"inner": {"amount": "1.5000"}, "labels": ["a", "b"]}

    def test_the_result_actually_serialises(self) -> None:
        """The assertion that matters to Gateway, which requires parseable JSON.

        Asserted by round-tripping rather than by inspecting the intermediate shape: a value that
        looks converted and still raises in `json.dumps` is the failure this prevents.
        """

        @dataclass(frozen=True)
        class Row:
            when: datetime
            value: Decimal

        payload = to_json_safe({"rows": (Row(datetime(2026, 8, 10, tzinfo=UTC), Decimal("2.00")),)})

        assert json.loads(json.dumps(payload)) == {
            "rows": [{"when": "2026-08-10T00:00:00+00:00", "value": "2.00"}]
        }


class TestFindingTheContractsInThisProcess:
    def test_it_finds_the_repository_root_when_running_from_a_checkout(self) -> None:
        """Searching upward for the dictionary, rather than counting parent directories.

        The same search has to work from a Lambda task root, where `contracts/` is a sibling of
        `adapters/` rather than of `src/`. A fixed number of hops serves one layout and breaks the
        other.
        """
        found = contracts_root()

        assert (found / "contracts" / "shared" / "metric-dictionary.yaml").is_file()
