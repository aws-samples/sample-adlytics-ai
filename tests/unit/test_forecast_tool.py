# SPDX-License-Identifier: MIT-0
"""The four demand forecast tools: envelopes, arguments and refusals (ADR-163, ADR-084).

A tool envelope at the wrong scale or grain lets the agent state a figure the publisher never
stored; a missing forecast qualifier lets a forecast read as observed; a silent truncation lets a
page read as the whole roster; a failure without a code reaches the agent as a raise. Each test
builds the real `ShapedResponse` from the shared fake reader's stored views.
"""

from __future__ import annotations

import json
import time as time_module
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from forecast_read_support import (
    NOW,
    VALUES,
    FakeReader,
    attempt,
    fake_reader,
    generation,
    item,
    pages,
    pointer,
    ranked,
)
from forecast_series_support import policy
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_config import VALID
from tool_caller_support import import_tool_entrypoint, with_bearer

from adapters.inbound.forecast_read.service import ForecastReadService, ReadContext
from adapters.inbound.tool_handlers.forecast import (
    FORECAST_RENDERING,
    MIXED_RENDERING,
    NEXT_STEPS,
    NO_PROMISE,
    ForecastTool,
    ForecastToolRequest,
    handle,
)
from adapters.inbound.tool_handlers.schemas import contract_for
from adapters.outbound.telemetry import tracing
from adapters.outbound.telemetry.tracing import FORECAST_GATEWAY
from domain.forecasting import summaries
from domain.forecasting.models import SkipReason
from domain.forecasting.publish import RunOutcome
from domain.narrative.publication import QualifierKind, RenderingKind
from domain.narrative.shaped_response import ShapedResponse, UnitKind, Units
from domain.shared_kernel.errors import AdlyticsError
from domain.shared_kernel.provenance import FreshnessStatus, MeasurementBasis

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-163")]

READER_ENV = {
    **VALID,
    "ADL_DEMAND_FORECAST_TABLE": "adlytics-test-demand-forecast",
    "ADL_DEMAND_FORECAST_MARKETPLACE_KEY": "fictional-marketplace",
    "ADL_DEMAND_FORECAST_CHECK_SCHEDULE": "cron(0 13 * * ? *)",
    "AWS_DEFAULT_REGION": "us-east-1",
}


def _answer(
    tool: ForecastTool, reader: FakeReader | None = None, **arguments: Any
) -> ShapedResponse:
    service = ForecastReadService(reader or fake_reader(), VALUES, policy())
    request = ForecastToolRequest(tool, arguments, NOW, "c" * 32)
    return handle(request, service, ReadContext(NOW, time_module.monotonic() + 60)).response


def _refusal(
    tool: ForecastTool, reader: FakeReader | None = None, **arguments: Any
) -> AdlyticsError:
    with pytest.raises(AdlyticsError) as refused:
        _answer(tool, reader, **arguments)
    return refused.value


def _qualifiers(answer: ShapedResponse) -> list[QualifierKind]:
    return [qualifier.kind for qualifier in answer.metadata.qualifiers]


def test_the_list_carries_stored_unit_totals_by_asin_with_its_forecast_qualifier() -> None:
    answer = _answer(ForecastTool.LIST)
    assert answer.data.grain.dimensions == ("asin",)
    assert answer.data.units == Units(UnitKind.COUNT)
    first = answer.data.values[0]
    assert (first["asin"], first["rank"], first["p50_total_4w"], first["p50_total_13w"]) == (
        "B0FIXTUR02",
        1,
        80,
        260,
    )
    assert [field.units for field in answer.data.claim_fields][:3] == [Units(UnitKind.COUNT)] * 3
    assert answer.data.values[2]["withheld_reason"] == "INSUFFICIENT_CONTEXT"
    assert answer.data.values[2]["p50_total_4w"] is None
    assert _qualifiers(answer) == [QualifierKind.FORECAST]
    assert answer.narrative.count(FORECAST_RENDERING) == 1
    assert answer.metadata.measurement_basis == (MeasurementBasis.MODELLED,)
    assert answer.metadata.suppression.rows_suppressed is False
    assert answer.metadata.coverage_start == datetime(2026, 11, 8, tzinfo=UTC)
    assert answer.metadata.coverage_end == datetime.combine(date(2027, 2, 6), time.max, tzinfo=UTC)
    assert answer.data.additivity is not None and answer.data.additivity.across == ("asin",)
    assert answer.data.population_total is None
    assert answer.metadata.truncated is False


@pytest.mark.covers("ADR-084")
def test_a_money_list_carries_two_place_figures_in_their_currency() -> None:
    answer = _answer(ForecastTool.LIST, target="revenue_shipped")
    assert answer.data.units == Units(UnitKind.CURRENCY, "USD")
    assert answer.data.values[0]["p50_total_4w"] == Decimal("4938.00")
    assert answer.data.values[0]["p50_total_13w"] == Decimal("16048.50")
    assert [row["asin"] for row in answer.data.values] == ["B0FIXTUR02", "B0FIXTUR01"]
    assert "B0FIXTUR03 isn't forecast for shipped revenue: too little history." in (
        answer.narrative
    )


_WITHHELD_REVENUE = "B0FIXTUR03 isn't forecast for shipped revenue: too little history."
_WITHHELD_COGS = "B0FIXTUR03 isn't forecast for shipped cost of goods sold: too little history."
_ABSENT = "B0FIXTUR09 is not in this forecast."


@pytest.mark.parametrize(
    ("tool", "arguments", "lines"),
    [
        (ForecastTool.LIST, {"asin": "B0FIXTUR09"}, [_ABSENT]),
        (ForecastTool.LIST, {"asin": "B0FIXTUR03"}, [_WITHHELD_REVENUE]),
        (ForecastTool.LIST, {"status": "INSUFFICIENT_CONTEXT"}, [_WITHHELD_REVENUE]),
        (ForecastTool.QUERY, {"asins": ["B0FIXTUR09"]}, [_ABSENT]),
        (ForecastTool.QUERY, {"asins": ["B0FIXTUR03"], "target": "cogs_shipped"}, [_WITHHELD_COGS]),
        (ForecastTool.SUMMARIZE, {"asin": "B0FIXTUR03"}, [_WITHHELD_REVENUE]),
        (ForecastTool.SUMMARIZE, {"asin": "B0FIXTUR09"}, [_ABSENT]),
        (
            ForecastTool.COMPARE,
            {"asins": ["B0FIXTUR03", "B0FIXTUR09"]},
            [_WITHHELD_REVENUE, _ABSENT],
        ),
    ],
)
@pytest.mark.covers("ADR-084")
def test_a_money_target_with_no_shown_row_is_the_valid_empty_answer_without_a_currency(
    tool: ForecastTool, arguments: dict[str, Any], lines: list[str]
) -> None:
    """A money figure's currency comes from a shown row; with none, inventing one is the defect."""
    called: dict[str, Any] = {"target": "revenue_shipped", **arguments}
    answer = _answer(tool, **called)
    assert (answer.data.values, answer.data.units, answer.data.claim_fields) == ((), None, ())
    assert answer.metadata.row_count == 0
    assert all(line in answer.narrative for line in lines)
    assert QualifierKind.FORECAST not in _qualifiers(answer)


def test_an_exact_asin_lookup_returns_its_stored_row_unranked() -> None:
    answer = _answer(ForecastTool.LIST, asin="b0fixtur02")
    assert [row["asin"] for row in answer.data.values] == ["B0FIXTUR02"]
    assert answer.data.values[0]["rank"] is None
    absent = _answer(ForecastTool.LIST, asin="B0FIXTUR09")
    assert (absent.data.values, absent.data.units) == ((), None)
    assert "B0FIXTUR09 is not in this forecast." in absent.narrative


def test_a_filter_with_more_matches_than_one_page_is_labelled_truncated() -> None:
    views = [item(n) for n in range(1, 52)]
    entries = ranked(views)
    meta = generation(entries)
    reader = FakeReader(current=pointer(), meta=meta)
    answer = _answer(ForecastTool.LIST, reader)
    assert (len(answer.data.values), answer.metadata.truncated) == (50, True)
    line = next(text for text in answer.narrative if "truncated" in text)
    assert "top 50 of 51 matching products (page 1 of 2);" in line
    assert QualifierKind.TRUNCATED in _qualifiers(answer)


def test_a_truncated_money_list_counts_only_the_rows_it_shows() -> None:
    """A withheld money row is named, not shown, so the line must not count it as shown."""
    # Withheld rows rank last, so of these two the first ends page 1 and the second opens page 2.
    views = [item(n, shown=n > 2) for n in range(1, 52)]
    reader = FakeReader(current=pointer(), meta=generation(ranked(views)))
    answer = _answer(ForecastTool.LIST, reader, target="revenue_shipped")
    assert len(answer.data.values) == 49
    line = next(text for text in answer.narrative if "truncated" in text)
    assert (
        "top 50 of 51 matching products (page 1 of 2), 49 shown and 1 named as not forecast;"
    ) in line


@pytest.mark.covers("ADR-084")
def test_query_carries_weekly_bands_at_the_target_scale_with_week_grain() -> None:
    units = _answer(ForecastTool.QUERY, asins=["B0FIXTUR01"], target="units_ordered", view_weeks=4)
    assert units.data.grain.dimensions == ("asin", "week_start")
    assert [
        (row["week_start"], row["p10"], row["p50"], row["p90"]) for row in units.data.values
    ] == [
        ("2026-11-08", 10, 11, 12),
        ("2026-11-15", 10, 11, 12),
        ("2026-11-22", 10, 11, 12),
        ("2026-11-29", 10, 11, 12),
    ]
    money = _answer(ForecastTool.QUERY, asins=["B0FIXTUR01"], target="cogs_shipped")
    assert money.data.units == Units(UnitKind.CURRENCY, "USD")
    assert money.data.values[0]["p50"] == Decimal("1234.50")
    assert len(money.data.values) == 13
    claim = money.narrative[money.data.claim_narrative_indices[0]]
    assert claim.endswith("over the next 13 weeks: 16,048.50 USD.")


def test_summarize_pairs_the_forecast_with_recent_weeks_under_mixed_basis() -> None:
    answer = _answer(
        ForecastTool.SUMMARIZE, asin="B0FIXTUR01", target="units_shipped", view_weeks=8
    )
    assert answer.data.grain.dimensions == ("asin", "week_start", "series")
    series = [row["series"] for row in answer.data.values]
    assert (series.count("recent_actual"), series.count("forecast")) == (13, 8)
    recent = answer.data.values[0]
    assert (recent["actual"], recent["p50"], recent["measurement_basis"]) == (
        9,
        None,
        "retail_actual",
    )
    assert answer.metadata.measurement_basis == (
        MeasurementBasis.MODELLED,
        MeasurementBasis.RETAIL_ACTUAL,
    )
    assert _qualifiers(answer) == [QualifierKind.FORECAST, QualifierKind.MIXED_BASIS]
    assert answer.narrative.count(MIXED_RENDERING) == 1
    assert answer.metadata.coverage_start == datetime(2026, 8, 9, tzinfo=UTC)
    claims = [answer.narrative[i] for i in answer.data.claim_narrative_indices]
    assert claims == [
        "Stored forecast total (p50) of shipped units for B0FIXTUR01 over the next 8 weeks: 84.",
        "In backtest for B0FIXTUR01, the actual was inside the range in 9 of 13 test weeks; "
        "MASE 0.8123.",
    ]
    assert answer.data.additivity is not None
    assert answer.data.additivity.across == ("asin", "week_start")


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (ForecastTool.SUMMARIZE, {"asin": "B0FIXTUR01"}),
        (ForecastTool.COMPARE, {"asins": ["B0FIXTUR01", "B0FIXTUR02"]}),
    ],
)
def test_recent_history_carries_each_week_state_and_is_never_called_all_observed(
    tool: ForecastTool, arguments: dict[str, Any]
) -> None:
    """An inferred zero or an unknown week stated as observed sales is a wrong figure."""
    reader = fake_reader()
    reader.items["B0FIXTUR01"] = item(1, week_states=("inferred_zero", "unknown"))
    answer = _answer(tool, reader, target="units_ordered", view_weeks=4, **arguments)
    recent = [
        (row["week_state"], row["actual"], row["stockout"])
        for row in answer.data.values
        if row["asin"] == "B0FIXTUR01" and row["series"] == "recent_actual"
    ]
    assert recent[:3] == [
        ("inferred_zero", 0, None),
        ("unknown", None, None),
        ("observed", 9, "false"),
    ]
    assert answer.data.units == Units(UnitKind.COUNT)
    assert MIXED_RENDERING == (
        "These figures span forecasts and recent retail history; each recent week's state says "
        "whether its value was observed, inferred as zero or is unknown."
    )
    assert answer.narrative.count(MIXED_RENDERING) == 1
    assert not any("recent weeks are observed" in line for line in answer.narrative)
    actual = next(field for field in answer.data.claim_fields if field.name == "actual")
    assert actual.label == "Recent week value (state in week_state)"


def test_compare_names_a_withheld_and_an_absent_product_without_rows() -> None:
    answer = _answer(
        ForecastTool.COMPARE,
        asins=["B0FIXTUR01", "B0FIXTUR03", "B0FIXTUR09"],
        target="units_ordered",
    )
    assert {row["asin"] for row in answer.data.values} == {"B0FIXTUR01"}
    assert (
        "B0FIXTUR03 isn't forecast for demand (ordered units): too little history."
        in answer.narrative
    )
    assert "B0FIXTUR09 is not in this forecast." in answer.narrative


_INACTIVE = (
    "B0FIXTUR04 isn't forecast for demand (ordered units): no sales were reported for it in the "
    "13 weeks of data through 2026-11-07, so there is no recent pattern to forecast. That alone "
    "doesn't say whether it is still for sale or what its future demand will be."
)
_INACTIVE_NEXT_STEP = (
    "Next step for a product withheld as ASIN_INACTIVE: If it's still on sale, plan it by hand for "
    "now. The next weekly update will check again after new sales data arrives; a forecast "
    "appears only once there's enough complete history. A next step says what has to happen "
    "first, not that a forecast will follow: a product is forecast only when a forecasting method "
    "passes its checks."
)
_NEXT_STEPS_FIXTURE = (
    Path(__file__).resolve().parents[2] / "frontend/test/fixtures/demand-forecast-next-steps.json"
)


def test_every_next_step_is_the_pages_own_undated_copy() -> None:
    """Chat added an unserved "it may be forecast later" (dev, Q4); the tool now serves the step.

    The page owns the wording and `frontend/test/demand-forecast-copy.test.ts` pins both of its
    forms to the same fixture, so the tool's copy must be the fixture's undated form for every
    reason, or chat and page would say different things about the same product.
    """
    pinned = json.loads(_NEXT_STEPS_FIXTURE.read_text(encoding="utf-8"))
    assert {reason.value: text for reason, text in NEXT_STEPS.items()} == {
        reason: step["undated"] for reason, step in pinned.items()
    }
    assert set(NEXT_STEPS) == set(SkipReason)


def test_a_withheld_reasons_next_step_is_stated_once_and_as_a_prerequisite() -> None:
    """A filter to withheld products repeated one next step per row (review, round 3).

    Chat also turned "appears only once" into "will appear once"; the line says it isn't a promise.
    """
    views = [
        item(1),
        item(4, shown=False, reason="ASIN_INACTIVE"),
        item(5, shown=False, reason="ASIN_INACTIVE"),
        item(6, shown=False, reason="CURRENCY_MIXED"),
    ]
    entries = ranked(views)
    reader = FakeReader(current=pointer(), meta=generation(entries), stored_pages=pages(entries))
    answer = _answer(ForecastTool.LIST, reader)
    claims = [answer.narrative[i] for i in answer.data.claim_narrative_indices]
    steps = [line for line in claims if line.startswith("Next step for")]
    assert steps == [
        _INACTIVE_NEXT_STEP,
        "Next step for a product withheld as CURRENCY_MIXED: Ask your administrator which "
        f"marketplace this product should report in. {NO_PROMISE}",
    ]
    assert sum(line.startswith(("B0FIXTUR04 ", "B0FIXTUR05 ")) for line in claims) == 2


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (ForecastTool.LIST, {"asin": "B0FIXTUR04"}),
        (ForecastTool.LIST, {"status": "ASIN_INACTIVE"}),
        (ForecastTool.SUMMARIZE, {"asin": "B0FIXTUR04", "target": "units_ordered"}),
    ],
)
def test_a_product_with_no_recent_sales_is_named_by_a_claim_that_says_no_more_than_its_reason(
    tool: ForecastTool, arguments: dict[str, Any]
) -> None:
    """A bare `ASIN_INACTIVE` let the agent claim the product isn't for sale (chat Q4, dev).

    The line is a claim, so evaluation grades the answer against these exact words.
    """
    inactive = item(4, shown=False, reason="ASIN_INACTIVE")
    entries = ranked([item(1), inactive])
    reader = FakeReader(
        current=pointer(),
        meta=generation(entries),
        stored_pages=pages(entries),
        items={"B0FIXTUR04": inactive},
    )
    answer = _answer(tool, reader, **arguments)
    claims = [answer.narrative[i] for i in answer.data.claim_narrative_indices]
    assert claims[:2] == [_INACTIVE, _INACTIVE_NEXT_STEP]
    grounding = answer.metadata.grounding
    assert grounding is not None
    for line in (_INACTIVE, _INACTIVE_NEXT_STEP):
        assert [(r.kind, r.text) for r in grounding.renderings if r.text == line] == [
            (RenderingKind.CLAIM, line)
        ]


def test_a_stale_forecast_carries_the_stale_qualifier_with_its_cause() -> None:
    reader = fake_reader()
    reader.current = pointer(last_attempt=attempt(RunOutcome.HELD_QUALITY, "item_too_large"))
    answer = _answer(ForecastTool.QUERY, reader, asins=["B0FIXTUR01"], target="units_ordered")
    assert answer.metadata.freshness_status is FreshnessStatus.STALE
    assert "This forecast is stale: a quality check held the last forecast run." in answer.narrative
    assert QualifierKind.STALE in _qualifiers(answer)


def test_a_money_comparison_over_two_currencies_is_refused_and_units_answer() -> None:
    reader = fake_reader()
    reader.items["B0FIXTUR02"] = item(2, currency="EUR")
    refused = _refusal(
        ForecastTool.COMPARE, reader, asins=["B0FIXTUR01", "B0FIXTUR02"], target="revenue_shipped"
    )
    assert str(refused.code) == "ADLY-FORECAST_READ-0006"
    units = _answer(
        ForecastTool.COMPARE, reader, asins=["B0FIXTUR01", "B0FIXTUR02"], target="units_ordered"
    )
    assert units.data.units == Units(UnitKind.COUNT)


@pytest.mark.parametrize(
    ("tool", "arguments", "rule"),
    [
        (ForecastTool.QUERY, {"asins": ["B0FIXTUR0"], "target": "units_ordered"}, "asin_format"),
        (
            ForecastTool.QUERY,
            {"asins": [f"B0FIXTUR0{n}" for n in range(1, 7)], "target": "units_ordered"},
            "asins_count",
        ),
        (ForecastTool.COMPARE, {"asins": ["B0FIXTUR01"], "target": "units_ordered"}, "asins_count"),
        (
            ForecastTool.QUERY,
            {"asins": ["B0FIXTUR01", "b0fixtur01"], "target": "units_ordered"},
            "asins_unique",
        ),
        (ForecastTool.QUERY, {"asins": ["B0FIXTUR01"], "target": "revenue"}, "target_unknown"),
        (
            ForecastTool.QUERY,
            {"asins": ["B0FIXTUR01"], "target": "units_ordered", "view_weeks": 5},
            "view_unknown",
        ),
        (ForecastTool.LIST, {"status": "everything"}, "status_unknown"),
        (ForecastTool.LIST, {"asin": "B0FIXTUR01", "status": "shown"}, "asin_with_status"),
        (ForecastTool.LIST, {"page": 2}, "unknown_argument"),
    ],
)
def test_each_argument_rule_is_refused_before_any_read(
    tool: ForecastTool, arguments: dict[str, Any], rule: str
) -> None:
    reader = fake_reader()
    refused = _refusal(tool, reader, **arguments)
    assert (str(refused.code), refused.context) == ("ADLY-FORECAST_READ-0005", {"rule": rule})
    assert reader.calls == []


def test_store_incomplete_and_unpublished_are_coded_refusals() -> None:
    reader = fake_reader()
    reader.meta = None
    assert str(_refusal(ForecastTool.LIST, reader).code) == "ADLY-FORECAST_READ-0008"
    empty = FakeReader(current=None)
    assert str(
        _refusal(ForecastTool.QUERY, empty, asins=["B0FIXTUR01"], target="units_ordered").code
    ) == ("ADLY-FORECAST_READ-0007")


def test_the_read_path_never_builds_a_list_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """R17: the stored row is served; a read-time sum would be a figure computed in the API."""
    reader = fake_reader()

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("roster_entry called on the read path")

    monkeypatch.setattr(summaries, "roster_entry", refuse)
    for tool, arguments in (
        (ForecastTool.LIST, {}),
        (ForecastTool.SUMMARIZE, {"asin": "B0FIXTUR01", "target": "units_ordered"}),
    ):
        assert _answer(tool, reader, **arguments).data.values


# --- The entry point ---------------------------------------------------------------------------

entrypoint = import_tool_entrypoint(
    "adapters.inbound.tool_handlers.forecast_entrypoint", extra_env=READER_ENV
)


class _ClientContext:
    def __init__(self, custom: dict[str, Any]) -> None:
        self.custom = custom


class _LambdaContext:
    function_name = "adlytics-forecast"
    function_version = "$LATEST"
    invoked_function_arn = "arn:aws:lambda:us-east-1:000000000000:function:adlytics-forecast"
    memory_limit_in_mb = 1024
    aws_request_id = "00000000-0000-4000-8000-000000000000"

    def __init__(self, tool: str, remaining_ms: int = 15_000) -> None:
        self.client_context = _ClientContext(
            with_bearer(
                {
                    "bedrockAgentCoreToolName": tool,
                    "bedrockAgentCoreMcpMessageId": "mcp-message-under-test",
                    "bedrockAgentCoreGatewayId": "gw-under-test",
                    "bedrockAgentCoreTargetId": "tgt-under-test",
                }
            )
        )
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms


def _invoke(  # noqa: PLR0913 -- one Gateway call with keyword overrides
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    reader: FakeReader | None = None,
) -> dict[str, Any]:
    monkeypatch.setattr(
        entrypoint, "SERVICE", ForecastReadService(reader or fake_reader(), VALUES, policy())
    )
    result = entrypoint.handler(dict(arguments or {}), _LambdaContext(f"forecast___{tool}"))
    json.dumps(result)
    return result


_SAMPLES: dict[str, object] = {
    "asin": "B0FIXTUR01",
    "asins": ["B0FIXTUR01", "B0FIXTUR02"],
    "target": "units_ordered",
    "view_weeks": 4,
}


@pytest.mark.parametrize("tool", [member.value for member in ForecastTool])
def test_an_event_built_from_each_declaration_drives_the_real_handler(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    arguments = {name: _SAMPLES[name] for name in contract_for(tool).required}
    answer = _invoke(monkeypatch, tool, arguments)
    assert answer.get("refused") is not True, answer.get("reason")
    assert answer["data"]["values"]


@pytest.mark.parametrize(
    "case",
    [
        ("forecast_list_asins", {"target": "units_ordered"}, 2),
        ("forecast_list_asins", {"asin": "B0FIXTUR02", "target": "units_ordered"}, 2),
        (
            "forecast_query_asins",
            {"asins": ["B0FIXTUR01", "B0FIXTUR02"], "target": "units_ordered"},
            3,
        ),
        (
            "forecast_compare_asins",
            {"asins": ["B0FIXTUR01", "B0FIXTUR02"], "target": "revenue_shipped"},
            3,
        ),
        ("forecast_summarize_asin", {"asin": "B0FIXTUR01", "target": "units_ordered"}, 2),
    ],
)
def test_an_answer_records_its_plans_items_read_on_the_gateway_span(
    monkeypatch: pytest.MonkeyPatch, case: tuple[str, dict[str, Any], int]
) -> None:
    """Without `items_read` on `tool.forecast_gateway`, a tool's read count can't be seen live."""
    tool, arguments, items = case
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "TRACER", provider.get_tracer("test"))
    answer = _invoke(monkeypatch, tool, arguments)
    assert answer.get("refused") is not True, answer.get("reason")
    (gateway_span,) = [s for s in exporter.get_finished_spans() if s.name == FORECAST_GATEWAY]
    assert (gateway_span.attributes or {})["items_read"] == items


def test_an_unknown_tool_and_a_malformed_invocation_return_typed_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown = _invoke(monkeypatch, "forecast_page_two")
    assert (unknown["refused"], unknown["code"]) == (True, "ADLY-GATEWAY-0001")
    monkeypatch.setattr(entrypoint, "SERVICE", ForecastReadService(fake_reader(), VALUES, policy()))
    malformed = entrypoint.handler({}, _LambdaContext("no-target-prefix"))
    assert (malformed["refused"], malformed["code"]) == (True, "ADLY-GATEWAY-0001")


def test_a_store_deadline_and_an_unexpected_error_return_their_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = fake_reader()
    reader.fail = RuntimeError("provider text")
    failed = _invoke(
        monkeypatch,
        "forecast_query_asins",
        {"asins": ["B0FIXTUR01"], "target": "units_ordered"},
        reader=reader,
    )
    assert (failed["refused"], failed["code"]) == (True, "ADLY-FORECAST_READ-0011")
    assert "provider text" not in json.dumps(failed)
    monkeypatch.setattr(entrypoint, "SERVICE", ForecastReadService(fake_reader(), VALUES, policy()))
    late = entrypoint.handler(
        {"asins": ["B0FIXTUR01"], "target": "units_ordered"},
        _LambdaContext("forecast___forecast_query_asins", remaining_ms=500),
    )
    assert (late["refused"], late["code"]) == (True, "ADLY-FORECAST_READ-0009")
