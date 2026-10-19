# SPDX-License-Identifier: MIT-0
"""Deep-tier input supply: declared-line coverage, scope suffixes, W10, bounds and C7."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from campaign_channel_filter_support import filter_siblings
from campaign_detail_support import detail_fixture
from campaign_portfolio_support import (
    campaign_display_signature,
    complete_portfolio,
    portfolio_row,
)

import adapters.deep_insight_inputs as deep_inputs
from adapters.deep_insight_inputs import (
    ALLOWED_INPUT_SUPPLIERS,
    DEEP_SIBLING_METRIC,
    IN_VIEW_SUFFIX,
    MAX_DEEP_DRIVERS,
    MAX_DEEP_SERIES_POINTS,
    WHOLE_PORTFOLIO_SUFFIX,
    DeepInputs,
    SiblingDirectionRequest,
    detail_deep_inputs,
    portfolio_deep_inputs,
    servable_context,
    sibling_direction_entries,
)
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.telemetry.reporting import Report
from domain.presentation.campaign_attribution import CampaignSelectionCapability
from domain.presentation.campaign_portfolio import (
    CampaignPortfolioArtifact,
    CampaignPortfolioMetric,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
    campaign_portfolio_artifact_from_payload,
    campaign_portfolio_artifact_payload,
    campaign_portfolio_segment,
)
from domain.presentation.insights import InsightSlotId
from domain.presentation.metric_focus import (
    RETAIL_FOCUS_METRICS,
    campaign_portfolio_focus_ingredients,
)
from domain.presentation.page_schemas import TemporalPresetId
from domain.presentation.preset_artifacts import (
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactRevision,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
)
from domain.shared_kernel.errors import ErrorCode, InvariantError
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-076")]
ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT)
PERIOD = DateRange(date(2026, 8, 13), date(2026, 8, 19))
SPEND_DESC = CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc")


def _portfolio(count: int) -> CampaignPortfolioArtifact:
    rows = tuple(
        portfolio_row(
            campaign_display_signature(f"Fictional Campaign {index:03d}"),
            CampaignSelectionCapability(f"baked:{index:05d}", datetime(2027, 1, 1, tzinfo=UTC)),
            PERIOD,
            Decimal(1000 + index),
            prior_spend=Decimal(900),
            roas=(Decimal("2.5"), Decimal("2.6") if index % 3 else Decimal("2")),
        )
        for index in range(count)
    )
    return complete_portfolio(PERIOD, rows)


def _segment(count: int, index: int) -> CampaignPortfolioArtifact:
    segment = campaign_portfolio_segment(_portfolio(count), SPEND_DESC, index)
    assert segment is not None
    return segment


def _names(inputs: DeepInputs) -> list[str]:
    return [item.name for item in inputs.findings] + [entry.name for entry in inputs.context]


def test_every_declared_deep_input_line_has_a_supplier_for_its_slot() -> None:
    for _, contract in load_insight_contracts(ROOT):
        if contract.deep is None:
            continue
        for line in contract.deep.allowed_inputs:
            assert line in ALLOWED_INPUT_SUPPLIERS, (contract.id.value, line)
            assert contract.id in ALLOWED_INPUT_SUPPLIERS[line], (contract.id.value, line)


def test_a_segment_reads_as_campaigns_in_view_and_totals_as_the_whole_portfolio() -> None:
    inputs = portfolio_deep_inputs(_segment(60, 1), InsightSlotId.CONTRIBUTION, CATALOG)
    rows = [name for name in _names(inputs) if " for Fictional Campaign" in name]
    totals = [
        name for name in _names(inputs) if name.startswith("Ad spend ") and " for " not in name
    ]
    context = {entry.name: entry.value for entry in inputs.context}

    assert rows and all(name.endswith(IN_VIEW_SUFFIX) for name in rows)
    assert totals and all(name.endswith(WHOLE_PORTFOLIO_SUFFIX) for name in totals)
    assert not any(
        IN_VIEW_SUFFIX in name and WHOLE_PORTFOLIO_SUFFIX in name for name in _names(inputs)
    )
    assert context["campaigns in view"] == (
        "These are the 10 campaigns ranked 51 to 60 by ad spend, highest first in this window, "
        "of 60 in the portfolio"
    )
    assert f"spend change in view{IN_VIEW_SUFFIX}" in context
    assert "spend change outside the view" in context


def test_one_segment_holding_every_campaign_reads_as_the_whole_portfolio() -> None:
    inputs = portfolio_deep_inputs(_segment(30, 0), InsightSlotId.DIVERGENCE, CATALOG)
    rows = [name for name in _names(inputs) if " for Fictional Campaign" in name]
    context = {entry.name: entry.value for entry in inputs.context}

    assert rows and all(name.endswith(WHOLE_PORTFOLIO_SUFFIX) for name in rows)
    assert not any(IN_VIEW_SUFFIX in name for name in _names(inputs))
    assert context["campaigns in view"] == (
        "These are all 30 campaigns in the portfolio, ranked by ad spend, highest first"
    )
    assert context[f"ROAS direction counts{WHOLE_PORTFOLIO_SUFFIX}"] == (
        "Among these 30: 10 rose, 20 fell, 0 were flat"
    )
    assert "spend change outside the view" not in context


def test_drivers_are_bounded_by_one_segment_and_series_by_fourteen_days() -> None:
    inputs = portfolio_deep_inputs(_segment(60, 0), InsightSlotId.CONTRIBUTION, CATALOG)
    driver_currents = [
        item
        for item in inputs.findings
        if " for Fictional Campaign" in item.name and " current" in item.name
    ]
    series = [item for item in inputs.findings if item.name.startswith("Ad spend on ")]
    assert len(driver_currents) == MAX_DEEP_DRIVERS
    assert 0 < len(series) <= MAX_DEEP_SERIES_POINTS * 2


def test_detail_deep_carries_each_comparison_metric() -> None:
    detail, delivery = detail_fixture()
    inputs = detail_deep_inputs(detail, delivery, CATALOG)
    names = {item.name for item in inputs.findings}
    assert {"Ad spend current", "Ad spend prior", "Ad spend change"} <= names
    assert any(name.endswith("(current)") for name in names)


def test_every_sibling_metric_parses_and_the_divergence_one_is_the_portfolio_roas() -> None:
    """W10: the divergence key named no served metric, so its sibling entry never existed."""
    artifact = _portfolio(5)
    assert DEEP_SIBLING_METRIC[InsightSlotId.DIVERGENCE] == (
        CampaignPortfolioMetric.PORTFOLIO_ROAS.value
    )
    for slot, metric in DEEP_SIBLING_METRIC.items():
        if slot in {InsightSlotId.CONTRIBUTION, InsightSlotId.EXPOSURE, InsightSlotId.DIVERGENCE}:
            campaign_portfolio_focus_ingredients(artifact, metric)
        else:
            assert metric in RETAIL_FOCUS_METRICS
    # Positive control: the retired key does not parse.
    with pytest.raises(ValueError):
        campaign_portfolio_focus_ingredients(artifact, "portfolio_roas")


def test_the_divergence_sibling_read_yields_a_direction_entry() -> None:
    artifact = _portfolio(5)
    payload = campaign_portfolio_artifact_payload(artifact)
    revision = PresetArtifactRevision.from_publication_identities(("publication-1",))
    pointers = tuple(
        PresetArtifactPointer(
            PresetArtifactPage("campaigns"), PresetArtifactVariant(variant), revision, "a" * 64, 1
        )
        for variant in ("1d", "7d")
    )
    metric = DEEP_SIBLING_METRIC[InsightSlotId.DIVERGENCE]

    def extract(body: dict[str, object]) -> tuple[str, Decimal, Decimal | None]:
        del body
        ingredients = campaign_portfolio_focus_ingredients(artifact, metric)
        return (
            "Portfolio ROAS",
            Decimal(ingredients.current.value),
            Decimal(ingredients.prior.value),
        )

    entries = sibling_direction_entries(
        SiblingDirectionRequest(pointers, "7d", lambda _pointer: payload, extract)
    )
    assert [entry.name for entry in entries] == ["Sibling 1D window direction"]


def _sibling_pointers(*variants: str) -> tuple[PresetArtifactPointer, ...]:
    revision = PresetArtifactRevision.from_publication_identities(("publication-1",))
    return tuple(
        PresetArtifactPointer(
            PresetArtifactPage("campaigns"), PresetArtifactVariant(variant), revision, "a" * 64, 1
        )
        for variant in variants
    )


def _roas_extract(
    artifact: CampaignPortfolioArtifact,
) -> Callable[[dict[str, object]], tuple[str, Decimal, Decimal | None]]:
    metric = DEEP_SIBLING_METRIC[InsightSlotId.DIVERGENCE]

    def extract(body: dict[str, object]) -> tuple[str, Decimal, Decimal | None]:
        del body
        ingredients = campaign_portfolio_focus_ingredients(artifact, metric)
        return (
            "Portfolio ROAS",
            Decimal(ingredients.current.value),
            Decimal(ingredients.prior.value),
        )

    return extract


def test_a_failed_sibling_read_is_reported_and_the_other_siblings_still_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sibling hint never blocks the deep serve, but its failure must not be invisible."""
    reported: list[tuple[BaseException, str]] = []

    def capture(error: BaseException, report: Report) -> None:
        reported.append((error, report.operation))

    monkeypatch.setattr(deep_inputs, "report_error", capture)
    artifact = _portfolio(5)
    payload = campaign_portfolio_artifact_payload(artifact)
    unreadable = InvariantError(ErrorCode("PRESENTATION", 99), "fixture sibling is malformed.")

    def read_payload(pointer: PresetArtifactPointer) -> dict[str, object]:
        if pointer.variant.value == "1d":
            raise unreadable
        return payload

    entries = sibling_direction_entries(
        SiblingDirectionRequest(
            _sibling_pointers("1d", "7d", "14d"), "7d", read_payload, _roas_extract(artifact)
        )
    )
    assert [entry.name for entry in entries] == ["Sibling 14D window direction"]
    assert reported == [(unreadable, "presentation.deep_insight_inputs")]


def test_a_typed_absent_sibling_metric_contributes_nothing_and_reports_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported: list[BaseException] = []

    def capture(error: BaseException, _report: Report) -> None:
        reported.append(error)

    monkeypatch.setattr(deep_inputs, "report_error", capture)

    def absent(body: dict[str, object]) -> tuple[str, Decimal, Decimal | None] | None:
        del body
        return None

    entries = sibling_direction_entries(
        SiblingDirectionRequest(
            _sibling_pointers("1d", "7d"), "7d", lambda _pointer: {"fixture": True}, absent
        )
    )
    assert entries == ()
    assert reported == []


def test_a_sibling_whose_extract_raises_value_error_is_reported_not_read_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a None from `extract` is a typed absence; a raised ValueError is a failure."""
    reported: list[BaseException] = []

    def capture(error: BaseException, _report: Report) -> None:
        reported.append(error)

    monkeypatch.setattr(deep_inputs, "report_error", capture)
    stale = ValueError("campaign portfolio artifact fields are not exact.")

    def undecodable(body: dict[str, object]) -> tuple[str, Decimal, Decimal | None] | None:
        del body
        raise stale

    entries = sibling_direction_entries(
        SiblingDirectionRequest(
            _sibling_pointers("1d", "7d"), "7d", lambda _pointer: {"fixture": True}, undecodable
        )
    )
    assert entries == ()
    assert reported == [stale]


def test_servable_context_names_views_and_what_each_metric_serves() -> None:
    artifact = _portfolio(3)
    entry = servable_context(
        (TemporalPresetId.ONE_DAY, TemporalPresetId.SEVEN_DAYS, TemporalPresetId.FOURTEEN_DAYS),
        (
            ("Ad spend", campaign_portfolio_focus_ingredients(artifact, "spend")),
            ("Impressions", campaign_portfolio_focus_ingredients(artifact, "impressions")),
        ),
    )
    assert entry.value == (
        "This workspace serves: the 1D, 7D and 14D views; for Ad spend: a daily series and "
        "drivers; for Impressions: the total only"
    )


# --- campaign-detail-500 cause B: a multi-channel sibling carries spend and impressions only ---


def _served_sibling(filter_id: str) -> CampaignPortfolioArtifact:
    """One filter sibling as the bake writes it and the deep route decodes it (ADR-127)."""
    sibling = next(item for item in filter_siblings() if item.filter_id == filter_id)
    return campaign_portfolio_artifact_from_payload(
        json.loads(canonical_preset_artifact_json(campaign_portfolio_artifact_payload(sibling)))
    )


def _whole_portfolio_totals(inputs: DeepInputs) -> set[str]:
    return {
        item.name
        for item in inputs.findings
        if item.name.endswith(WHOLE_PORTFOLIO_SUFFIX)
        and " for " not in item.name
        and " on " not in item.name
    }


@pytest.mark.parametrize("filter_id", ["all", "sponsored"])
@pytest.mark.parametrize(
    "slot_id", [InsightSlotId.CONTRIBUTION, InsightSlotId.EXPOSURE, InsightSlotId.DIVERGENCE]
)
def test_a_multi_channel_sibling_supplies_only_the_totals_it_carries(
    filter_id: str, slot_id: InsightSlotId
) -> None:
    """Before the fix the purchases focus raised a bare StopIteration: a 500 on every slot."""
    sibling = _served_sibling(filter_id)
    assert tuple(item.metric for item in sibling.totals) == (
        CampaignPortfolioMetric.SPEND,
        CampaignPortfolioMetric.IMPRESSIONS,
    )

    inputs = portfolio_deep_inputs(sibling, slot_id, CATALOG)

    assert _whole_portfolio_totals(inputs) == {
        f"{label} {side}{WHOLE_PORTFOLIO_SUFFIX}"
        for label in ("Ad spend", "Impressions")
        for side in ("current", "prior", "change")
    }


def test_a_single_channel_sibling_still_supplies_every_portfolio_total() -> None:
    """Paired control: one channel's sibling carries all four totals and supplies each."""
    sibling = _served_sibling("amazon_dsp")
    inputs = portfolio_deep_inputs(sibling, InsightSlotId.CONTRIBUTION, CATALOG)
    assert {
        name.removesuffix(f" current{WHOLE_PORTFOLIO_SUFFIX}")
        for name in _whole_portfolio_totals(inputs)
        if name.endswith(f" current{WHOLE_PORTFOLIO_SUFFIX}")
    } == {
        "Ad spend",
        "Attributed purchases",
        "Impressions",
        "Portfolio ROAS (attributed, 14 day)",
    }


def test_a_metric_a_sibling_does_not_carry_is_a_value_error_not_stop_iteration() -> None:
    sibling = _served_sibling("all")
    with pytest.raises(ValueError, match="not carried on this artifact"):
        campaign_portfolio_focus_ingredients(sibling, CampaignPortfolioMetric.PURCHASES.value)
