# SPDX-License-Identifier: MIT-0
"""Server-declared change semantics: governed direction, sentiment, text and word states."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from campaign_detail_support import detail_fixture
from campaign_portfolio_support import (
    campaign_display_signature,
    complete_portfolio,
    portfolio_row,
)
from change_support import SEMANTICS, as_retained

from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import CatalogError, FavourableDirection
from domain.measurement.comparison import percentage_change
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.campaign_attribution import CampaignSelectionCapability
from domain.presentation.campaign_portfolio import (
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
    campaign_portfolio_artifact_from_payload,
    campaign_portfolio_artifact_payload,
    campaign_portfolio_segment,
    campaign_portfolio_segment_from_payload,
    campaign_portfolio_segment_payload,
)
from domain.presentation.change import (
    CHANGE_KEY,
    PERCENTAGE_KEY,
    Change,
    ChangeDirection,
    ChangeOperands,
    ChangeScale,
    ChangeSentiment,
    ChangeUnit,
    ComparedMetric,
    NotMeaningful,
    build_change,
    change_from_payload,
    change_payload,
    change_semantics_from,
    require_change_blocks,
)
from domain.presentation.compared_metrics import (
    PRESENTATION_COMPARED_METRICS,
    presentation_change_semantics,
)
from domain.presentation.retail_health import (
    RETAIL_HEALTH_TOTAL_GRAIN,
    RetailMetricValue,
    retail_comparison,
)
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-121")]

ROOT = Path(__file__).resolve().parents[2]
USD = Units(UnitKind.CURRENCY, "USD")
COUNT = Units(UnitKind.COUNT)
RATIO = Units(UnitKind.RATIO)


def _change(current: object, prior: object, metric_id: str, units: Units) -> Change:  # noqa: PLR0913, PLR0917
    return build_change(
        ChangeOperands(Decimal(str(current)), Decimal(str(prior)), units),
        SEMANTICS.for_metric(metric_id),
    )


def _copy_dictionary(tmp_path: Path) -> Path:
    # The catalog also derives each metric's platforms from the contracts, source packs and
    # media taxonomy (ADR-106), so the whole contracts tree is copied, not the dictionary alone.
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    return tmp_path


# --- declaration loading ----------------------------------------------------------------


def test_the_committed_dictionary_declares_every_compared_metric() -> None:
    semantics = presentation_change_semantics(load_catalog(ROOT))
    assert {name for name, _ in semantics.metrics} == PRESENTATION_COMPARED_METRICS


def test_the_reviewed_directions_follow_the_adr() -> None:
    favourable = {name: metric.favourable for name, metric in SEMANTICS.metrics}
    assert favourable["ad_spend"] is FavourableDirection.NONE
    assert favourable["cpc"] is FavourableDirection.DOWN
    assert favourable["cpm"] is FavourableDirection.DOWN
    assert favourable["ctr"] is FavourableDirection.UP
    assert favourable["customer_returns"] is FavourableDirection.DOWN
    assert favourable["amazon_margin_proxy"] is FavourableDirection.NONE


def test_loading_refuses_a_compared_metric_without_the_field(tmp_path: Path) -> None:
    root = _copy_dictionary(tmp_path)
    path = root / "contracts/shared/metric-dictionary.yaml"
    text = path.read_text()
    marker = "  cpc:\n"
    head, tail = text.split(marker, 1)
    path.write_text(head + marker + tail.replace("    favourable_direction: down\n", "", 1))
    catalog = load_catalog(root)
    with pytest.raises(CatalogError, match=r"favourable_direction.*missing on: cpc"):
        presentation_change_semantics(catalog)


def test_loading_names_every_compared_metric_that_lacks_the_field(tmp_path: Path) -> None:
    root = _copy_dictionary(tmp_path)
    path = root / "contracts/shared/metric-dictionary.yaml"
    path.write_text(path.read_text().replace("    favourable_direction: none\n", ""))
    with pytest.raises(CatalogError, match=r"missing on: ad_spend, amazon_margin_proxy"):
        presentation_change_semantics(load_catalog(root))


def test_loading_refuses_an_unknown_direction(tmp_path: Path) -> None:
    root = _copy_dictionary(tmp_path)
    path = root / "contracts/shared/metric-dictionary.yaml"
    path.write_text(
        path.read_text().replace("favourable_direction: down", "favourable_direction: lower", 1)
    )
    with pytest.raises(CatalogError, match="expected up, down or none"):
        load_catalog(root)


def test_loading_refuses_a_compared_metric_the_dictionary_lacks() -> None:
    with pytest.raises(CatalogError, match="not declared: fictional_metric"):
        change_semantics_from(load_catalog(ROOT), ("ctr", "fictional_metric"))


# --- sentiment --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "prior", "metric_id", "units", "direction", "sentiment"),
    [
        ("1.21", "1.30", "cpc", USD, ChangeDirection.DOWN, ChangeSentiment.FAVOURABLE),
        ("1.40", "1.30", "cpc", USD, ChangeDirection.UP, ChangeSentiment.UNFAVOURABLE),
        ("0.0120", "0.0100", "ctr", RATIO, ChangeDirection.UP, ChangeSentiment.FAVOURABLE),
        ("0.0080", "0.0100", "ctr", RATIO, ChangeDirection.DOWN, ChangeSentiment.UNFAVOURABLE),
        ("106.20", "100.00", "ad_spend", USD, ChangeDirection.UP, ChangeSentiment.NEUTRAL),
        ("93.80", "100.00", "ad_spend", USD, ChangeDirection.DOWN, ChangeSentiment.NEUTRAL),
        ("57", "60", "customer_returns", COUNT, ChangeDirection.DOWN, ChangeSentiment.FAVOURABLE),
        (
            "3.65",
            "3.65",
            "roas_attributed_14d",
            RATIO,
            ChangeDirection.FLAT,
            ChangeSentiment.NEUTRAL,
        ),
    ],
)
def test_sentiment_comes_from_the_declared_direction_never_the_sign(  # noqa: PLR0913, PLR0917
    current: str,
    prior: str,
    metric_id: str,
    units: Units,
    direction: ChangeDirection,
    sentiment: ChangeSentiment,
) -> None:
    change = _change(current, prior, metric_id, units)
    assert (change.direction, change.sentiment) == (direction, sentiment)


# --- text and precision -----------------------------------------------------------------


def test_no_change_means_equal_unrounded_values() -> None:
    change = _change("0.0040", "0.0040", "ctr", RATIO)
    assert change == Change(
        ChangeDirection.FLAT,
        ChangeSentiment.NEUTRAL,
        ChangeUnit.PERCENTAGE_POINTS,
        "No change",
        None,
    )


def test_a_ctr_fall_under_one_percent_reads_in_points_at_three_decimals() -> None:
    change = _change("0.00398", "0.00400", "ctr", RATIO)
    assert change.unit is ChangeUnit.PERCENTAGE_POINTS
    assert change.display_text == "\u22120.002 pts"


def test_a_rate_change_below_the_display_step_reads_less_than_one_thousandth_point() -> None:
    change = _change("0.0040000001", "0.0040", "ctr", RATIO)
    assert change.direction is ChangeDirection.UP
    assert change.sentiment is ChangeSentiment.FAVOURABLE
    assert change.display_text == "<0.001 pts"


def test_a_percent_change_below_the_display_step_reads_less_than_a_tenth() -> None:
    change = _change("100.0001", "100", "retail_revenue_shipped", USD)
    assert (change.unit, change.display_text) == (ChangeUnit.PERCENT, "<0.1%")


def test_a_rate_at_or_above_one_percent_reads_in_points_at_one_decimal() -> None:
    change = _change("0.989", "0.999", "fulfilment_rate", RATIO)
    assert (change.display_text, change.sentiment) == (
        "\u22121.0 pts",
        ChangeSentiment.UNFAVOURABLE,
    )


def test_a_multiplier_reads_its_absolute_change_in_times() -> None:
    change = _change("3.65", "4.29", "portfolio_roas_attributed_14d", RATIO)
    assert (change.unit, change.display_text) == (ChangeUnit.ABSOLUTE, "\u22120.64\u00d7")


def test_a_percent_change_rounds_half_up_to_one_decimal() -> None:
    assert _change("106.25", "100", "glance_views", COUNT).display_text == "+6.3%"


# --- word states ------------------------------------------------------------------------


def test_a_prior_loss_reads_up_from_a_loss_never_a_percentage() -> None:
    change = _change("29960", "-4340", "amazon_margin_proxy", USD)
    assert change == Change(
        ChangeDirection.UP,
        ChangeSentiment.NEUTRAL,
        ChangeUnit.ABSOLUTE,
        "Up from a loss",
        NotMeaningful.NEGATIVE_PRIOR,
    )


def test_a_deeper_loss_reads_down_from_a_loss() -> None:
    change = _change("-5000", "-4340", "amazon_margin_proxy", USD)
    assert (change.display_text, change.not_meaningful) == (
        "Down from a loss",
        NotMeaningful.NEGATIVE_PRIOR,
    )


def test_a_zero_prior_reads_new() -> None:
    change = _change("5", "0", "purchases", COUNT)
    assert (change.display_text, change.not_meaningful, change.sentiment) == (
        "New",
        NotMeaningful.NO_PRIOR,
        ChangeSentiment.FAVOURABLE,
    )


def test_a_near_zero_prior_reads_the_absolute_change() -> None:
    change = _change("57", "3", "customer_returns", COUNT)
    assert change == Change(
        ChangeDirection.UP,
        ChangeSentiment.UNFAVOURABLE,
        ChangeUnit.ABSOLUTE,
        "+54",
        NotMeaningful.NEAR_ZERO_PRIOR,
    )


def test_a_near_zero_currency_prior_reads_two_places_with_its_code() -> None:
    change = _change("1200.005", "100", "retail_revenue_shipped", USD)
    assert change.display_text == "+1,100.01 USD"


def test_a_rate_never_reads_a_word_state() -> None:
    change = _change("0.0500", "0", "return_rate", RATIO)
    assert (change.not_meaningful, change.display_text) == (None, "+5.0 pts")


# --- payload ----------------------------------------------------------------------------


def test_a_block_round_trips_through_its_payload() -> None:
    change = _change("1.21", "1.30", "cpc", USD)
    operands = ChangeOperands(Decimal("1.21"), Decimal("1.30"), USD)
    assert change_from_payload(change_payload(change), operands, "test") == change


def test_a_block_that_disagrees_with_its_operands_is_refused() -> None:
    payload = change_payload(_change("1.21", "1.30", "cpc", USD))
    assert payload is not None
    operands = ChangeOperands(Decimal("1.30"), Decimal("1.21"), USD)
    with pytest.raises(ValueError, match="does not match"):
        change_from_payload(payload, operands, "test")
    tampered = {**payload, "display_text": "\u22121.0%"}
    with pytest.raises(ValueError, match="does not match"):
        change_from_payload(tampered, ChangeOperands(Decimal("1.21"), Decimal("1.30"), USD), "t")


def test_a_block_with_an_extra_key_is_refused() -> None:
    payload = change_payload(_change("1.21", "1.30", "cpc", USD))
    assert payload is not None
    operands = ChangeOperands(Decimal("1.21"), Decimal("1.30"), USD)
    with pytest.raises(ValueError, match="invalid shape"):
        change_from_payload({**payload, "tone": "good"}, operands, "test")


def test_a_flat_change_cannot_carry_a_tone() -> None:
    with pytest.raises(ValueError, match="neutral"):
        Change(
            ChangeDirection.FLAT,
            ChangeSentiment.FAVOURABLE,
            ChangeUnit.PERCENT,
            "No change",
            None,
        )


def test_every_computable_comparison_carries_a_block_and_none_or_a_mix_is_refused() -> None:
    block = _change("1.21", "1.30", "cpc", USD)
    # Positive control: every computable comparison carries its block.
    require_change_blocks((block, block), "body")
    # The retained all-null form and a mix are both refused (owner revision, 2026-10-08).
    for changes in ((None, None), (block, None)):
        with pytest.raises(ValueError) as refused:
            require_change_blocks(changes, "body")
        assert str(refused.value) == "body carries a computable comparison with no change block."


def test_semantics_refuse_a_metric_outside_the_roster() -> None:
    with pytest.raises(ValueError, match="no declared change semantics"):
        SEMANTICS.for_metric("fictional_metric")
    assert ComparedMetric(FavourableDirection.UP, ChangeScale.POINTS) == SEMANTICS.for_metric("ctr")


# --- artifacts: every comparison carries its block; the retained form is refused --------


def _portfolio_payload() -> dict[str, object]:
    period = DateRange(date(2026, 8, 1), date(2026, 8, 7))
    row = portfolio_row(
        campaign_display_signature("Fictional Campaign 001"),
        CampaignSelectionCapability("fictional-reference", datetime(2026, 9, 1, tzinfo=UTC)),
        period,
        Decimal("10"),
    )
    return campaign_portfolio_artifact_payload(complete_portfolio(period, (row,)))


def _comparisons(payload: dict[str, object]) -> list[dict[str, object]]:
    rows = cast("list[dict[str, object]]", payload["rows"])
    return [
        *cast("list[dict[str, object]]", payload["totals"]),
        *(item for row in rows for item in cast("list[dict[str, object]]", row["comparisons"])),
    ]


def test_every_portfolio_comparison_carries_its_change_block() -> None:
    comparisons = _comparisons(_portfolio_payload())
    assert all(isinstance(item[CHANGE_KEY], dict) for item in comparisons)
    spend = comparisons[0][CHANGE_KEY]
    assert spend == {
        "direction": "up",
        "sentiment": "neutral",
        "unit": "percent",
        "display_text": "+11.1%",
        "not_meaningful": None,
    }
    ctr = next(item for item in comparisons if item["metric"] == "ctr")[CHANGE_KEY]
    assert cast("dict[str, object]", ctr)["unit"] == "percentage_points"
    assert cast("dict[str, object]", ctr)["sentiment"] == "favourable"


def test_a_current_comparison_carries_its_block_in_place_of_the_percentage() -> None:
    assert all(PERCENTAGE_KEY not in item for item in _comparisons(_portfolio_payload()))


def test_a_format_6_portfolio_without_change_keys_is_refused_and_format_11_decodes() -> None:
    current = _portfolio_payload()
    # Positive control: the current body decodes with a block on every comparison.
    decoded = campaign_portfolio_artifact_from_payload(current)
    assert all(item.change is not None for item in decoded.totals)
    assert campaign_portfolio_artifact_payload(decoded) == current
    retained = json.loads(json.dumps(current))
    as_retained(retained)
    with pytest.raises(ValueError) as refused:
        campaign_portfolio_artifact_from_payload(retained)
    assert str(refused.value) == "a comparison without a change key is not a current body."


def test_a_two_sided_comparison_with_a_null_change_is_refused() -> None:
    """The retained form's re-encoding (percentage beside `change: null`) is no served form."""
    nulled = json.loads(json.dumps(_portfolio_payload()))
    for item in _comparisons(nulled):
        current = cast("dict[str, object]", item["current"])["value"]
        prior = cast("dict[str, object]", item["prior"])["value"]
        item[CHANGE_KEY] = None
        item[PERCENTAGE_KEY] = percentage_change(Decimal(str(current)), Decimal(str(prior)))
    with pytest.raises(ValueError) as refused:
        campaign_portfolio_artifact_from_payload(nulled)
    assert str(refused.value) == (
        "a portfolio with a coverage block serves every computable change."
    )


def test_a_retained_sibling_segment_is_refused_and_a_current_one_decodes() -> None:
    period = DateRange(date(2026, 8, 1), date(2026, 8, 7))
    rows = tuple(
        portfolio_row(
            campaign_display_signature(name),
            CampaignSelectionCapability(f"fictional-{index}", datetime(2026, 9, 1, tzinfo=UTC)),
            period,
            Decimal(10 - index),
        )
        for index, name in enumerate(("Zulu", "Alpha", "Mike"))
    )
    population = complete_portfolio(period, rows)
    parent = campaign_portfolio_segment(
        population, CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"), 0
    )
    sibling = campaign_portfolio_segment(
        population, CampaignPortfolioSort(CampaignPortfolioSortField.NAME, "asc"), 0
    )
    assert parent is not None and sibling is not None
    current_parent = campaign_portfolio_artifact_from_payload(
        campaign_portfolio_artifact_payload(parent)
    )
    # Positive control: a current sibling decodes against its current parent with its blocks.
    decoded = campaign_portfolio_segment_from_payload(
        campaign_portfolio_segment_payload(sibling), current_parent
    )
    assert all(item.change is not None for row in decoded.rows for item in row.comparisons)
    sibling_payload = campaign_portfolio_segment_payload(sibling)
    as_retained(sibling_payload)
    with pytest.raises(ValueError) as refused:
        campaign_portfolio_segment_from_payload(sibling_payload, current_parent)
    assert str(refused.value) == "a comparison without a change key is not a current body."


def test_a_portfolio_mixing_blocks_or_carrying_a_wrong_one_is_refused() -> None:
    mixed = json.loads(json.dumps(_portfolio_payload()))
    as_retained(_comparisons(mixed)[0])
    with pytest.raises(ValueError, match="without a change key"):
        campaign_portfolio_artifact_from_payload(mixed)
    rewritten = json.loads(json.dumps(_portfolio_payload()))
    cast("dict[str, object]", _comparisons(rewritten)[0][CHANGE_KEY])["display_text"] = "+11%"
    with pytest.raises(ValueError, match="does not match"):
        campaign_portfolio_artifact_from_payload(rewritten)
    flipped = json.loads(json.dumps(_portfolio_payload()))
    cast("dict[str, object]", _comparisons(flipped)[0][CHANGE_KEY])["direction"] = "down"
    with pytest.raises(ValueError, match="does not match"):
        campaign_portfolio_artifact_from_payload(flipped)


def test_the_campaign_detail_and_its_delivery_carry_change_blocks() -> None:
    detail, delivery = detail_fixture()
    assert detail.comparison is not None
    assert all(metric.change is not None for metric in detail.comparison.metrics)
    cpc = next(item for item in delivery.comparison if item.slot.value == "cpc")
    assert cpc.change is not None
    assert cpc.change.sentiment is ChangeSentiment.UNFAVOURABLE
    assert all(item.change is None for item in delivery.summary)


def test_a_retail_comparison_carries_the_governed_change() -> None:
    period = DateRange(date(2026, 9, 15), date(2026, 9, 21))
    prior = DateRange(date(2026, 9, 8), date(2026, 9, 14))
    comparison = retail_comparison(
        "customer_returns",
        RetailMetricValue(57, COUNT, period, RETAIL_HEALTH_TOTAL_GRAIN),
        RetailMetricValue(60, COUNT, prior, RETAIL_HEALTH_TOTAL_GRAIN),
        SEMANTICS,
    )
    assert comparison.change is not None
    assert (comparison.change.display_text, comparison.change.sentiment) == (
        "\u22125.0%",
        ChangeSentiment.FAVOURABLE,
    )


def test_a_rate_shown_per_thousand_changes_in_percent_never_points() -> None:
    change = _change("0.0201", "0.0200", "conversion_rate_retail", RATIO)
    assert (change.unit, change.display_text, change.sentiment) == (
        ChangeUnit.PERCENT,
        "+0.5%",
        ChangeSentiment.FAVOURABLE,
    )
    operands = ChangeOperands(Decimal("0.0201"), Decimal("0.0200"), RATIO)
    assert change_from_payload(change_payload(change), operands, "test") == change
