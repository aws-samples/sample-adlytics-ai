# SPDX-License-Identifier: MIT-0
"""MetricFocus.v3 derivations: facts, drivers, next-check, and the detail extractor."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest
from campaign_detail_support import detail_fixture, metric_value
from campaign_portfolio_support import (
    campaign_display_signature,
    complete_portfolio,
    portfolio_row,
)
from change_support import FICTIONAL_SEMANTICS

from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.campaign_attribution import (
    SUMMARY_GRAIN,
    CampaignSelectionCapability,
    SafeCampaignDisplaySignature,
)
from domain.presentation.campaigns import EffectiveNameStatus
from domain.presentation.insights import (
    InsightOutputClass,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
)
from domain.presentation.metric_focus import (
    METRIC_FOCUS_VERSION,
    BuildMetricFocusRequest,
    CampaignDetailFocusRequest,
    MetricFocusDriverCandidate,
    MetricFocusDriverGrain,
    MetricFocusDrivers,
    MetricFocusDriversAbsent,
    MetricFocusIdentity,
    MetricFocusIngredients,
    MetricFocusSeries,
    MetricFocusSeriesPoint,
    MetricFocusValue,
    build_metric_focus,
    campaign_detail_focus_ingredients,
    campaign_portfolio_focus_ingredients,
    metric_focus_payload,
)
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

PERIOD = DateRange(date(2026, 8, 1), date(2026, 8, 3))
PRIOR = DateRange(date(2026, 7, 29), date(2026, 7, 31))
COUNT = Units(UnitKind.COUNT)
RATIO = Units(UnitKind.RATIO)
USD = Units(UnitKind.CURRENCY, "USD")


_metric = metric_value
_detail_fixture = detail_fixture


def _identity(metric_id: str, units: Units, basis: MeasurementBasis) -> MetricFocusIdentity:
    return MetricFocusIdentity(
        metric_id,
        f"Fictional {metric_id}",
        "A fictional dictionary definition line.",
        units,
        basis,
        SUMMARY_GRAIN,
    )


def _slot(slot_id: InsightSlotId) -> InsightSlot:
    return InsightSlot(
        slot_id,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.UNAVAILABLE,
        None,
        None,
        "fictional-revision",
        "unresolved",
    )


class TestCampaignDetailFocus:
    def test_cpc_decomposes_into_its_declared_operand_movements(self) -> None:
        """A ratio's why is arithmetic first: spend and clicks movements, unranked."""
        detail, delivery = _detail_fixture()
        ingredients = campaign_detail_focus_ingredients(
            CampaignDetailFocusRequest(detail=detail, delivery=delivery, metric_id="cpc")
        )
        assert ingredients.current.value == Decimal("2.5000")
        assert ingredients.current.units.kind is UnitKind.CURRENCY
        assert ingredients.prior.period == PRIOR
        focus = build_metric_focus(
            BuildMetricFocusRequest(
                ingredients,
                _identity("cpc", USD, MeasurementBasis.AD_ATTRIBUTED),
                _slot(InsightSlotId.METRIC_FOCUS),
                {"clicks": "Clicks", "spend": "Ad spend"},
                FICTIONAL_SEMANTICS,
            )
        )
        drivers = focus.drivers
        assert isinstance(drivers, MetricFocusDrivers)
        assert drivers.grain.value == "ratio_component"
        assert [entry.name for entry in drivers.entries] == ["Spend", "Clicks"]
        # Spend +20%, clicks +50%: clicks dominate at more than twice the spend move,
        # so the deterministic next-check names the clicks focus.
        assert focus.next_check is not None
        assert focus.next_check.metric_id == "clicks"

    def test_spend_carries_day_step_drivers_and_series_facts(self) -> None:
        detail, delivery = _detail_fixture()
        ingredients = campaign_detail_focus_ingredients(
            CampaignDetailFocusRequest(detail=detail, delivery=delivery, metric_id="spend")
        )
        assert ingredients.series is not None
        assert len(ingredients.series.points) == PERIOD.days
        assert ingredients.series.points[0].prior is not None
        focus = build_metric_focus(
            BuildMetricFocusRequest(
                ingredients,
                _identity("spend", USD, MeasurementBasis.AD_ATTRIBUTED),
                _slot(InsightSlotId.METRIC_FOCUS),
                {},
                FICTIONAL_SEMANTICS,
            )
        )
        assert isinstance(focus.drivers, MetricFocusDrivers)
        assert focus.drivers.grain.value == "day"
        assert [fact.fact_id for fact in focus.facts] == [
            "peak_day",
            "trough_day",
            "largest_day_move",
            "window_total",
        ]
        # The window total sums the served daily USD series: 1.5 USD at two places.
        assert focus.facts[3].value == "1.50 USD"
        # The day-over-day move renders as a unitless magnitude (the pre-ADR-076
        # presentation); its qualifier names the day span. Spend is a flat 0.50 USD a
        # day here, so the USD move is zero and reads 0.00, unsigned (ADR-084).
        move = focus.facts[2]
        assert move.value == "0.00"
        assert "USD" not in move.value
        assert move.qualifier is not None
        assert "→" in move.qualifier

    def test_attributed_roas_states_its_designed_absent_decomposition(self) -> None:
        detail, delivery = _detail_fixture()
        ingredients = campaign_detail_focus_ingredients(
            CampaignDetailFocusRequest(
                detail=detail, delivery=delivery, metric_id="attributed_roas"
            )
        )
        assert ingredients.drivers_absent_reason is not None
        assert "revenue operand" in ingredients.drivers_absent_reason

    def test_purchases_reads_the_summary_and_purchase_series(self) -> None:
        detail, delivery = _detail_fixture()
        ingredients = campaign_detail_focus_ingredients(
            CampaignDetailFocusRequest(detail=detail, delivery=delivery, metric_id="purchases")
        )
        assert ingredients.current.value == 3
        assert ingredients.current.units.kind is UnitKind.COUNT
        assert ingredients.series is not None
        assert ingredients.series.points[0].prior is None


class TestPortfolioFocusPayload:
    def test_two_unnamed_campaigns_yield_uniquely_named_drivers(self) -> None:
        """The deployed defect: duplicate 'Unnamed campaign' operands refused the findings."""
        period = DateRange(date(2026, 8, 13), date(2026, 8, 19))
        unnamed = SafeCampaignDisplaySignature(
            effective_name=None,
            channel="Fictional channel",
            marketplace="United States",
            platform="Amazon Ads",
            effective_name_status=EffectiveNameStatus.MISSING,
        )
        second = SafeCampaignDisplaySignature(
            effective_name=None,
            channel="Second fictional channel",
            marketplace="United States",
            platform="Amazon Ads",
            effective_name_status=EffectiveNameStatus.MISSING,
        )
        rows = (
            portfolio_row(
                unnamed,
                CampaignSelectionCapability("baked:00000", datetime(2027, 1, 1, tzinfo=UTC)),
                period,
                Decimal("10"),
            ),
            portfolio_row(
                second,
                CampaignSelectionCapability("baked:00001", datetime(2027, 1, 1, tzinfo=UTC)),
                period,
                Decimal("7"),
            ),
        )
        ingredients = campaign_portfolio_focus_ingredients(
            complete_portfolio(period, rows), "spend"
        )
        names = [candidate.name for candidate in ingredients.driver_candidates]
        assert len(set(names)) == len(names) == 2
        assert names == ["Unnamed campaign 1", "Unnamed campaign 2"]
        focus = build_metric_focus(
            BuildMetricFocusRequest(
                ingredients,
                _identity("spend", USD, MeasurementBasis.AD_ATTRIBUTED),
                _slot(InsightSlotId.METRIC_FOCUS),
                {},
                FICTIONAL_SEMANTICS,
            )
        )
        assert isinstance(focus.drivers, MetricFocusDrivers)

    def test_portfolio_roas_payload_carries_the_absent_reason_in_plain_words(self) -> None:
        period = DateRange(date(2026, 8, 13), date(2026, 8, 19))
        row = portfolio_row(
            campaign_display_signature("Fictional Campaign 001"),
            CampaignSelectionCapability("baked:00000", datetime(2027, 1, 1, tzinfo=UTC)),
            period,
            Decimal("10"),
        )
        artifact = complete_portfolio(period, (row,))
        ingredients = campaign_portfolio_focus_ingredients(
            artifact, "portfolio_roas_attributed_14d"
        )
        focus = build_metric_focus(
            BuildMetricFocusRequest(
                ingredients,
                _identity("portfolio_roas_attributed_14d", RATIO, MeasurementBasis.AD_ATTRIBUTED),
                _slot(InsightSlotId.METRIC_FOCUS),
                {},
                FICTIONAL_SEMANTICS,
            )
        )
        assert isinstance(focus.drivers, MetricFocusDriversAbsent)
        payload = metric_focus_payload(focus)
        drivers = cast("dict[str, object]", payload["drivers"])
        assert set(drivers) == {"absent_reason"}
        # A ratio metric renders a daily-average fact, never an invented total.
        facts = cast("list[dict[str, object]]", payload["facts"])
        assert [fact["id"] for fact in facts][-1] == "daily_average"


def _entries(payload: dict[str, object]) -> list[dict[str, object]]:
    drivers = cast("dict[str, object]", payload["drivers"])
    return cast("list[dict[str, object]]", drivers["entries"])


def _detail_focus_payload(metric_id: str, units: Units) -> dict[str, object]:
    detail, delivery = _detail_fixture()
    ingredients = campaign_detail_focus_ingredients(
        CampaignDetailFocusRequest(detail=detail, delivery=delivery, metric_id=metric_id)
    )
    focus = build_metric_focus(
        BuildMetricFocusRequest(
            ingredients,
            _identity(metric_id, units, MeasurementBasis.AD_ATTRIBUTED),
            _slot(InsightSlotId.METRIC_FOCUS),
            {},
            FICTIONAL_SEMANTICS,
        )
    )
    return metric_focus_payload(focus)


def _value(value: Decimal | int, units: Units, period: DateRange) -> MetricFocusValue:
    return MetricFocusValue(value, units, period, SUMMARY_GRAIN, MeasurementBasis.AD_ATTRIBUTED)


def _series_focus_facts(daily: tuple[Decimal | int, Decimal | int], units: Units) -> dict[str, str]:
    """Focus a two-day series and return its facts by id."""
    period = DateRange(date(2026, 8, 1), date(2026, 8, 2))
    prior = DateRange(date(2026, 7, 30), date(2026, 7, 31))
    ingredients = MetricFocusIngredients(
        metric_id="fictional_metric",
        period=period,
        prior_period=prior,
        current=_value(daily[0] + daily[1], units, period),
        prior=_value(daily[0] + daily[1], units, prior),
        series=MetricFocusSeries(
            (
                MetricFocusSeriesPoint(period.start, daily[0], None),
                MetricFocusSeriesPoint(period.end, daily[1], None),
            )
        ),
        drivers_grain=MetricFocusDriverGrain.DAY,
        driver_candidates=(),
        drivers_absent_reason=None,
        neighbors=("fictional_metric",),
        boundary=None,
        additive=True,
    )
    focus = build_metric_focus(
        BuildMetricFocusRequest(
            ingredients,
            _identity("fictional_metric", units, MeasurementBasis.AD_ATTRIBUTED),
            _slot(InsightSlotId.METRIC_FOCUS),
            {},
            FICTIONAL_SEMANTICS,
        )
    )
    return {fact.fact_id: fact.value for fact in focus.facts}


@pytest.mark.covers("ADR-084")
class TestMetricFocusV3Display:
    """Money in a focus result displays at two places; counts and ratios stay exact."""

    def test_the_payload_states_focus_version_four(self) -> None:
        payload = _detail_focus_payload("cpc", USD)
        assert METRIC_FOCUS_VERSION == 4
        assert payload["focus_version"] == 4

    def test_head_values_carry_the_two_place_display_for_currency_only(self) -> None:
        # CPC is 2.5000 USD current and 1.5000 USD aligned prior in the fixture.
        payload = _detail_focus_payload("cpc", USD)
        assert cast("dict[str, object]", payload["current"])["display_value"] == "2.50"
        assert cast("dict[str, object]", payload["prior"])["display_value"] == "1.50"
        # Purchases are a count of 3 current: no display value.
        purchases = _detail_focus_payload("purchases", COUNT)
        assert cast("dict[str, object]", purchases["current"])["display_value"] is None

    def test_cpc_spend_entry_is_two_place_and_clicks_entry_is_a_raw_count(self) -> None:
        # Spend moves by 2.0000 USD; clicks move 20 -> 30 (a count).
        entries = _entries(_detail_focus_payload("cpc", USD))
        spend, clicks = entries
        assert spend["name"] == "Spend"
        assert spend["change"] == "2.0000"
        assert spend["units"] == {"kind": "currency", "currency_code": "USD"}
        assert spend["display_change"] == "2.00"
        assert clicks["name"] == "Clicks"
        assert clicks["units"] == {"kind": "count", "currency_code": None}
        assert clicks["display_change"] is None
        assert clicks["change"] == "10"

    def test_cpm_impressions_entry_has_no_display_change(self) -> None:
        # Impressions move 20 -> 30 (count) under a USD CPM focus.
        entries = {entry["name"]: entry for entry in _entries(_detail_focus_payload("cpm", USD))}
        assert entries["Impressions"]["units"] == {"kind": "count", "currency_code": None}
        assert entries["Impressions"]["display_change"] is None
        assert entries["Spend"]["display_change"] == "2.00"

    def test_day_grain_currency_entries_are_two_place(self) -> None:
        payload = _detail_focus_payload("spend", USD)
        entries = _entries(payload)
        assert cast("dict[str, object]", payload["drivers"])["grain"] == "day"
        assert entries
        for entry in entries:
            assert entry["units"] == {"kind": "currency", "currency_code": "USD"}
            assert entry["display_change"] is not None
            whole, _, places = str(entry["display_change"]).lstrip("-").partition(".")
            assert whole.isdigit() and len(places) == 2

    def test_a_currency_day_move_is_signed_on_its_two_place_rounding(self) -> None:
        # A USD day move of +1234.5 reads +1,234.50 with no code (the unitless convention).
        facts = _series_focus_facts((Decimal("100"), Decimal("1334.5")), USD)
        assert facts["largest_day_move"] == "+1,234.50"
        assert facts["window_total"] == "1,434.50 USD"
        # A USD day move of +0.004 rounds to zero and reads 0.00, unsigned.
        facts = _series_focus_facts((Decimal("100"), Decimal("100.004")), USD)
        assert facts["largest_day_move"] == "0.00"

    def test_a_count_day_move_keeps_its_exact_text_and_a_zero_move_reads_plus_zero(
        self,
    ) -> None:
        # Counts of 5 and 7 units: exact text, no places added.
        facts = _series_focus_facts((5, 7), COUNT)
        assert facts["largest_day_move"] == "+2"
        assert facts["window_total"] == "12"
        # Counts of 5 and 5 units: the zero move keeps today's "+0".
        assert _series_focus_facts((5, 5), COUNT)["largest_day_move"] == "+0"

    def test_a_currency_driver_change_under_half_a_cent_displays_as_zero(self) -> None:
        # One campaign's USD spend moves by 0.004: the exact change is kept, displayed 0.00.
        period = DateRange(date(2026, 8, 1), date(2026, 8, 2))
        prior = DateRange(date(2026, 7, 30), date(2026, 7, 31))
        ingredients = MetricFocusIngredients(
            metric_id="spend",
            period=period,
            prior_period=prior,
            current=_value(Decimal("10.004"), USD, period),
            prior=_value(Decimal("10"), USD, prior),
            series=None,
            drivers_grain=MetricFocusDriverGrain.CAMPAIGN,
            driver_candidates=(
                MetricFocusDriverCandidate(
                    "Fictional Campaign 001", Decimal("10.004"), Decimal("10"), units=USD
                ),
            ),
            drivers_absent_reason=None,
            neighbors=("spend",),
            boundary=None,
            additive=True,
        )
        focus = build_metric_focus(
            BuildMetricFocusRequest(
                ingredients,
                _identity("spend", USD, MeasurementBasis.AD_ATTRIBUTED),
                _slot(InsightSlotId.METRIC_FOCUS),
                {},
                FICTIONAL_SEMANTICS,
            )
        )
        (entry,) = _entries(metric_focus_payload(focus))
        assert entry["change"] == "0.004"
        assert entry["display_change"] == "0.00"
