# SPDX-License-Identifier: MIT-0
"""The declared wire contract, and whether the handlers agree with it.

**The assertion worth the most here is the one a staleness gate cannot make.**
`make check-tool-schemas` proves `contracts/generated/tools.json` matches the declaration. It says
nothing about whether the declaration matches what a handler dereferences — a schema declaring
`windowStart` beside a handler reading `window_start` regenerates perfectly clean and then refuses
every call Gateway sends it. So the tests below build each event by iterating the **declaration's**
argument names and driving the real handler with the result.

Only `catalog` can be driven in this tier: `measurement_entrypoint` builds an Athena adapter at
import, so its half of this lives in `tests/integration/test_measurement_entrypoint.py`.

The second thing this file pins is the *absence* of three field classes. `computed_at` and
`correlation_id` come from Gateway metadata rather than model-authored schema arguments. Identity
and page context are absent from both the schema and the target's trusted context, so the model
cannot assert either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from tool_caller_support import import_tool_entrypoint, with_bearer

from adapters.inbound.tool_handlers.catalog import CatalogTool
from adapters.inbound.tool_handlers.measurement import MeasurementTool
from adapters.inbound.tool_handlers.schemas import (
    EXTERNAL_CLIENT_FIGURE_RULE,
    TOOL_CONTRACTS,
    Argument,
    ArgumentType,
    ToolContract,
    as_tool_definitions,
    contract_for,
)
from adapters.jobs.tool_schema_generation.__main__ import render

catalog_entrypoint = import_tool_entrypoint("adapters.inbound.tool_handlers.catalog_entrypoint")

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-006", "ADR-032", "G-36")]


SAMPLES: dict[str, Any] = {
    "metric_name": "retail_revenue_shipped",
    "source_reference": "",
    "metric_reference": "shipped revenue",
    "grain": "product",
    "grains": ["product"],
    "operation": "BREAKDOWN",
    "period_reference": "the 7 days ending yesterday",
    "period_start_candidate": "2026-08-10",
    "period_end_candidate": "2026-08-16",
    "prior_period_reference": "the previous 7 days",
    "prior_period_start_candidate": "2026-08-03",
    "prior_period_end_candidate": "2026-08-09",
    "entity_references": {"marketplace": "United States"},
    "report_lenses": {"selling_program": "BUSINESS"},
    "source_system_selector": "vendor_central",
    "source_dataset_selector": "GET_VENDOR_SALES_REPORT",
    "currency_mode": "NATIVE",
    "target_currency": "EUR",
    "max_rows": 10,
    "rank_size": 5,
    "rank_order": "INCREASE",
}
"""One plausible value per declared argument.

The test supplies *values*; the declaration supplies *names*. That split is what makes the drift
visible: an argument renamed in the declaration has no sample and fails here, and one renamed in a
handler is refused by the handler.
"""


def event_for(contract: ToolContract) -> dict[str, Any]:
    """Build a fully populated event from what the contract declares."""
    missing = [a.name for a in contract.arguments if a.name not in SAMPLES]
    if missing:
        raise AssertionError(
            f"{contract.name} declares {missing}, which this test has no sample value for. "
            "Add one, so a renamed argument cannot pass unnoticed."
        )
    return {argument.name: SAMPLES[argument.name] for argument in contract.arguments}


@dataclass(frozen=True)
class _ClientContext:
    custom: dict[str, Any]


@dataclass(frozen=True)
class _LambdaContext:
    client_context: _ClientContext
    function_name: str = "adlytics-catalog"
    function_version: str = "$LATEST"
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:000000000000:function:adlytics-catalog"
    memory_limit_in_mb: int = 512
    aws_request_id: str = "00000000-0000-4000-8000-000000000000"

    def get_remaining_time_in_millis(self) -> int:
        return 300_000


def _invoke(contract: ToolContract) -> dict[str, Any]:
    """Call the catalog handler as Gateway would, with the prefixed name and a flat event."""
    context = _LambdaContext(
        client_context=_ClientContext(
            custom=with_bearer(
                {
                    "bedrockAgentCoreToolName": f"catalog___{contract.name}",
                    "bedrockAgentCoreMcpMessageId": "schema-probe",
                }
            )
        )
    )
    return catalog_entrypoint.handler(event_for(contract), context)


class TestTheDeclarationMatchesTheHandler:
    @pytest.mark.parametrize(
        "tool", [CatalogTool.LIST_METRICS.value, CatalogTool.DESCRIBE_METRIC.value]
    )
    def test_a_catalog_tool_accepts_everything_its_contract_declares(self, tool: str) -> None:
        """The drift a staleness gate cannot catch.

        The event is assembled from the declaration, so a name only the schema knows is refused
        here rather than on the first real Gateway call.
        """
        answer = _invoke(contract_for(tool))
        assert answer.get("refused") is not True, answer.get("reason")

    def test_every_declared_tool_has_an_implemented_domain(self) -> None:
        """A declared tool with no handler is a Gateway target pointing nowhere.

        Both sides read their own enum rather than a literal list. The measurement half used to be
        a hard-coded set, which meant adding a tool required editing this test to say the same
        thing twice — and a test that has to be updated to keep passing is not asserting anything.
        """
        implemented = {member.value for member in CatalogTool} | {
            member.value for member in MeasurementTool
        }
        declared = tuple(contract.name for contract in TOOL_CONTRACTS)
        assert set(declared) == implemented
        assert declared == (
            "catalog_list_metrics",
            "catalog_describe_metric",
            "measurement_metric_by_grain",
            "measurement_compare_windows",
            "measurement_top_n",
        )

    def test_the_retired_finalizer_is_not_declared(self) -> None:
        """ADR-083: the answer streams as text, so no tool publishes it."""
        rendered = {definition["name"] for definition in as_tool_definitions()}
        assert "presentation_publish" not in rendered
        assert all(not name.startswith("presentation") for name in rendered)


class TestWhatMayNeverBeAnArgument:
    @pytest.mark.parametrize("forbidden", ["correlation_id", "computed_at", "today"])
    def test_the_clock_and_the_correlation_id_are_absent(self, forbidden: str) -> None:
        """Gateway metadata supplies both; model-authored values could fabricate provenance."""
        for contract in TOOL_CONTRACTS:
            assert forbidden not in {argument.name for argument in contract.arguments}

    @pytest.mark.parametrize(
        "forbidden",
        [
            "user_id",
            "identity",
            "actor",
            "actor_id",
            "tenant",
            "brand",
            "session_id",
            "page_context",
            "page_context_reference",
            "owner_id",
            "verified_owner_id",
        ],
    )
    def test_no_identity_or_page_context_is_model_authored(self, forbidden: str) -> None:
        # The target receives no trusted identity or page-context assertion, and model arguments
        # cannot create one.
        for contract in TOOL_CONTRACTS:
            assert forbidden not in {argument.name for argument in contract.arguments}

    def test_the_contract_refuses_one_at_construction(self) -> None:
        """Asserted on the type, not only on today's contracts.

        A test over the declared set passes the moment someone adds a fourth tool carrying one of
        these; refusing in `__post_init__` does not.
        """
        with pytest.raises(ValueError, match="correlation_id"):
            ToolContract(
                name="probe",
                description="a probe",
                arguments=(
                    Argument(
                        name="correlation_id",
                        type=ArgumentType.STRING,
                        description="should not be allowed",
                    ),
                ),
            )


class TestTheDeclarationIsWellFormed:
    def test_an_array_must_declare_its_element_type(self) -> None:
        # AgentCore's schema requires `items` on an array, and an array without it is accepted at
        # synth and rejected at gateway creation.
        with pytest.raises(ValueError, match="element type"):
            Argument(name="grains", type=ArgumentType.ARRAY, description="no items")

    def test_a_non_array_may_not_declare_one(self) -> None:
        with pytest.raises(ValueError, match="not an array"):
            Argument(
                name="metric_name",
                type=ArgumentType.STRING,
                description="a name",
                items=ArgumentType.STRING,
            )

    def test_every_argument_carries_a_description(self) -> None:
        """The model reads it. An undescribed argument is one it has to guess the meaning of."""
        with pytest.raises(ValueError, match="description"):
            Argument(name="grain", type=ArgumentType.STRING, description="  ")

    def test_a_duplicate_argument_is_refused(self) -> None:
        with pytest.raises(ValueError, match="twice"):
            ToolContract(
                name="probe",
                description="a probe",
                arguments=(
                    Argument(name="grain", type=ArgumentType.STRING, description="one"),
                    Argument(name="grain", type=ArgumentType.STRING, description="two"),
                ),
            )


class TestTheRenderedDefinitions:
    def test_every_tool_renders_an_object_schema(self) -> None:
        for definition in as_tool_definitions():
            assert definition["inputSchema"]["type"] == "object"
            assert definition["description"]

    def test_no_tool_exposes_a_grounding_nonce_argument(self) -> None:
        for definition in as_tool_definitions():
            schema = definition["inputSchema"]
            assert "grounding_nonce" not in schema.get("properties", {})
            assert "grounding_nonce" not in schema.get("required", [])

    def test_measurement_contracts_expose_only_candidate_semantic_intent(self) -> None:
        rendered = {d["name"]: d for d in as_tool_definitions()}
        measurement = {
            name: definition["inputSchema"]
            for name, definition in rendered.items()
            if name.startswith("measurement_")
        }
        forbidden = {
            "metric_name",
            "filters",
            "window_start",
            "window_end",
            "prior_window_start",
            "prior_window_end",
            "n",
            "order_by",
            "marketplace_key",
            "campaign_key",
            "amazon_asin",
            "source_scope",
        }
        required_semantic = {
            "metric_reference",
            "grains",
            "period_reference",
            "period_start_candidate",
            "period_end_candidate",
            "entity_references",
            "report_lenses",
            "selectors",
            "source_system_selector",
            "source_dataset_selector",
            "currency_mode",
            "target_currency",
            "max_rows",
        }
        for schema in measurement.values():
            properties = set(schema["properties"])
            assert properties.isdisjoint(forbidden)
            assert required_semantic <= properties

    def test_required_lists_only_fields_with_one_unambiguous_wire_shape(self) -> None:
        rendered = {d["name"]: d for d in as_tool_definitions()}
        breakdown = rendered["measurement_metric_by_grain"]["inputSchema"]
        comparison = rendered["measurement_compare_windows"]["inputSchema"]
        ranking = rendered["measurement_top_n"]["inputSchema"]

        assert breakdown["required"] == [
            "metric_reference",
            "grains",
        ]
        assert comparison["required"] == breakdown["required"]
        assert ranking["required"] == [
            *breakdown["required"],
            "rank_size",
            "rank_order",
        ]
        assert "period_reference" in breakdown["properties"]
        assert "period_reference" not in breakdown["required"]

    def test_catalog_describe_grain_requires_explicit_wording_to_be_preserved(self) -> None:
        rendered = {definition["name"]: definition for definition in as_tool_definitions()}
        grain = rendered["catalog_describe_metric"]["inputSchema"]["properties"]["grain"]

        assert "pass X unchanged" in grain["description"]
        assert "even when it may be unsupported" in grain["description"]
        assert "Entity selection is separate" in grain["description"]

    def test_catalog_list_is_for_dynamic_inventory_questions(self) -> None:
        """The description claims platform-inventory authority and disclaims general concepts."""
        rendered = {definition["name"]: definition for definition in as_tool_definitions()}
        description = rendered["catalog_list_metrics"]["description"]

        assert "inventory of metrics this platform declares" in description
        assert "source_reference" in description
        assert "general advertising or retail concept" in description

    def test_an_array_carries_its_element_type(self) -> None:
        rendered = {d["name"]: d for d in as_tool_definitions()}
        grains = rendered["measurement_metric_by_grain"]["inputSchema"]["properties"]["grains"]
        assert grains == {
            "type": "array",
            "description": grains["description"],
            "items": {"type": "string"},
        }

    def test_it_is_json_serialisable(self) -> None:
        # The consumer is CDK reading a file, so anything that cannot round-trip is a deploy
        # failure rather than a test failure.
        assert json.loads(json.dumps(as_tool_definitions()))


@pytest.mark.covers("ADR-150")
class TestTheExternalClientRule:
    """ADR-150: what an external MCP client is told, generated beside the tools, never into them."""

    def test_the_generated_file_carries_the_rule_and_leaves_the_tools_unchanged(self) -> None:
        document = json.loads(render())
        assert document["external_client_rule"] == EXTERNAL_CLIENT_FIGURE_RULE
        assert document["tools"] == as_tool_definitions()
        assert all(
            EXTERNAL_CLIENT_FIGURE_RULE not in tool["description"] for tool in document["tools"]
        )

    @pytest.mark.parametrize(
        "field",
        [
            "metadata.measurement_basis",
            "metadata.requested_coverage_intervals",
            "metadata.coverage_holes",
            "data.claim_fields[].label",
            "metadata.semantic_qualifiers",
            "attribution_window",
        ],
    )
    def test_the_rule_names_each_governed_field_a_result_states(self, field: str) -> None:
        assert field in EXTERNAL_CLIENT_FIGURE_RULE

    def test_the_rule_forbids_recomputing_and_summing_across_networks(self) -> None:
        assert "Do not add, total, average, convert or recompute figures" in (
            EXTERNAL_CLIENT_FIGURE_RULE
        )
        assert "never sum attributed sales across ad networks" in EXTERNAL_CLIENT_FIGURE_RULE


def test_the_grain_and_selector_descriptions_name_every_governed_value() -> None:
    """The model picks grains and selectors from these texts alone, so none may be missing.

    The D2 grains and selector values were absent, so a question on reach, segments or cohorts
    was answered with "audience segment isn't supported" (dev, 2026-10-17).
    """
    from adapters.outbound.metric_dictionary import COHORT_PATHS, PROFILE_PATHS  # noqa: PLC0415
    from domain.measurement.planning import GRAIN_COLUMNS  # noqa: PLC0415

    rendered = {d["name"]: d for d in as_tool_definitions()}
    for tool in ("measurement_metric_by_grain", "measurement_compare_windows", "measurement_top_n"):
        properties = rendered[tool]["inputSchema"]["properties"]
        grains, selectors = (
            properties["grains"]["description"],
            properties["selectors"]["description"],
        )
        assert [name for name in GRAIN_COLUMNS if name not in grains] == []
        values = (*PROFILE_PATHS, *COHORT_PATHS, "week", "month", "quarter", "custom")
        assert [value for value in values if value not in selectors] == []
        assert "time_grain" in selectors and "profile_path" in selectors


def test_customers_and_buyers_read_distinct_buyers_purchases_and_orders_read_purchases() -> None:
    """Run 3 `new_customers_lake`: "new-to-brand customers" read the NTB purchases metric.

    Checked in the generated file the Gateway serves, and in the dictionary's governed synonyms
    the server resolves the model's metric text through.
    """
    from pathlib import Path  # noqa: PLC0415

    from adapters.outbound.metric_dictionary import load_catalog  # noqa: PLC0415

    root = Path(__file__).resolve().parents[2]
    generated = json.loads((root / "contracts/generated/tools.json").read_text())
    for tool in ("measurement_metric_by_grain", "measurement_compare_windows", "measurement_top_n"):
        definition = next(item for item in generated["tools"] if item["name"] == tool)
        assert definition["inputSchema"]["properties"]["metric_reference"]["description"] == (
            "Customer-facing metric name, label, or governed alias to resolve deterministically. "
            "This is candidate intent, not an execution field; unknown or ambiguous text is "
            "refused. Customers and buyers are distinct people; purchases and orders are "
            "transactions, and one is never answered with the other. New-to-brand customers or "
            "buyers: buyers_new_to_brand_12mo, measured per complete week or month (time_grain "
            "week or month). New-to-brand purchases or orders: purchases_new_to_brand_12mo."
        )
    catalog = load_catalog(root)
    resolved = {
        reference: tuple(metric.name for metric in catalog.metric_candidates(reference))
        for reference in (
            "new-to-brand customers",
            "NTB customers",
            "new customers",
            "new-to-brand buyers",
            "new-to-brand purchases",
            "new-to-brand orders",
            "NTB orders",
        )
    }
    assert resolved == {
        "new-to-brand customers": ("buyers_new_to_brand_12mo",),
        "NTB customers": ("buyers_new_to_brand_12mo",),
        "new customers": ("buyers_new_to_brand_12mo",),
        "new-to-brand buyers": ("buyers_new_to_brand_12mo",),
        "new-to-brand purchases": ("purchases_new_to_brand_12mo",),
        "new-to-brand orders": ("purchases_new_to_brand_12mo",),
        "NTB orders": ("purchases_new_to_brand_12mo",),
    }
    purchases = catalog.metric("purchases_new_to_brand_12mo")
    assert purchases is not None
    assert purchases.customer_definition == (
        "Ad-attributed purchases of your advertised products by shoppers who hadn't bought from "
        "the brand in the previous 12 months. It counts purchases, not people: a shopper who buys "
        "twice counts twice. New-to-brand customers or buyers are counted by new-to-brand buyers."
    )
