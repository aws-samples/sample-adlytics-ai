# SPDX-License-Identifier: MIT-0
# ruff: noqa: PLR0913, PLR0917
"""Builders for complete campaign portfolio artifacts shared by preset-bake tests."""

from __future__ import annotations

import pathlib
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from change_support import SEMANTICS

from adapters.outbound.metric_dictionary import load_catalog
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.campaign_attribution import (
    CampaignListEvidence,
    CampaignNumber,
    CampaignSelectionCapability,
    SafeCampaignDisplaySignature,
)
from domain.presentation.campaign_portfolio import (
    CAMPAIGN_PORTFOLIO_CHANNEL_GRAIN,
    CAMPAIGN_PORTFOLIO_ROW_GRAIN,
    CAMPAIGN_PORTFOLIO_TOTAL_GRAIN,
    CampaignChannelDeliveryRow,
    CampaignChannelFilter,
    CampaignChannelTotalsRow,
    CampaignMultiChannelView,
    CampaignNotAddedTile,
    CampaignPortfolioArtifact,
    CampaignPortfolioComparison,
    CampaignPortfolioMetric,
    CampaignPortfolioMetricValue,
    CampaignPortfolioRow,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
    CampaignPortfolioTrend,
    CampaignPortfolioTrendPoint,
    build_campaign_portfolio,
    campaign_channel_filter_options,
    campaign_portfolio_comparison,
    campaign_portfolio_coverage_facts,
    campaign_portfolio_evidence_status,
    multi_channel_insights_line,
)
from domain.presentation.campaigns import EffectiveNameStatus
from domain.presentation.compared_metrics import presentation_labels
from domain.presentation.evidence_status import write_evidence_status
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange, Grain

USD = Units(UnitKind.CURRENCY, "USD")
COUNT = Units(UnitKind.COUNT)
RATIO = Units(UnitKind.RATIO)


def campaign_display_signature(name: str) -> SafeCampaignDisplaySignature:
    """Build a safe display signature whose only variation is its effective name."""
    return SafeCampaignDisplaySignature(
        effective_name=name,
        channel="Fictional channel",
        marketplace="United States",
        platform="Amazon Ads",
        effective_name_status=EffectiveNameStatus.AVAILABLE,
        effective_name_valid_from=date(2026, 1, 1),
    )


def _prior(period: DateRange) -> DateRange:
    return DateRange(period.start - timedelta(days=period.days), period.start - timedelta(days=1))


def _comparison(
    metric: CampaignPortfolioMetric,
    current: CampaignNumber,
    prior: CampaignNumber,
    units: Units,
    grain: Grain,
    period: DateRange,
) -> CampaignPortfolioComparison:
    current_value = CampaignPortfolioMetricValue(
        current, units, MeasurementBasis.AD_ATTRIBUTED, period, grain
    )
    prior_value = CampaignPortfolioMetricValue(
        prior, units, MeasurementBasis.AD_ATTRIBUTED, _prior(period), grain
    )
    return campaign_portfolio_comparison(metric, current_value, prior_value, SEMANTICS)


def portfolio_row(
    signature: SafeCampaignDisplaySignature,
    capability: CampaignSelectionCapability,
    period: DateRange,
    spend: CampaignNumber,
    *,
    prior_spend: CampaignNumber = Decimal("5"),
    roas: tuple[Decimal, Decimal] = (Decimal("2.5"), Decimal("2")),
) -> CampaignPortfolioRow:
    """One valid COMPLETE-portfolio row whose sort position is driven by its spend."""
    row_grain = CAMPAIGN_PORTFOLIO_ROW_GRAIN
    comparisons = (
        _comparison(CampaignPortfolioMetric.SPEND, spend, prior_spend, USD, row_grain, period),
        _comparison(CampaignPortfolioMetric.ATTRIBUTED_ROAS, *roas, RATIO, row_grain, period),
        _comparison(CampaignPortfolioMetric.PURCHASES, 3, 1, COUNT, row_grain, period),
        _comparison(
            CampaignPortfolioMetric.CTR,
            Decimal("0.25"),
            Decimal("0.2"),
            RATIO,
            row_grain,
            period,
        ),
    )
    # A current bake carries no row spark (render format 11, ADR-127).
    return CampaignPortfolioRow(signature, capability, comparisons)


def portfolio_totals(
    period: DateRange,
    *,
    spend: tuple[Decimal, Decimal] = (Decimal("1000"), Decimal("900")),
    roas: tuple[Decimal, Decimal] = (Decimal("2.5"), Decimal("2")),
) -> tuple[CampaignPortfolioComparison, ...]:
    """Fixed-order portfolio totals at the declared total grain (each pair: current, prior)."""
    total_grain = CAMPAIGN_PORTFOLIO_TOTAL_GRAIN
    return (
        _comparison(CampaignPortfolioMetric.SPEND, *spend, USD, total_grain, period),
        _comparison(CampaignPortfolioMetric.PURCHASES, 100, 90, COUNT, total_grain, period),
        _comparison(CampaignPortfolioMetric.IMPRESSIONS, 10000, 9000, COUNT, total_grain, period),
        _comparison(CampaignPortfolioMetric.PORTFOLIO_ROAS, *roas, RATIO, total_grain, period),
    )


def portfolio_trend(period: DateRange) -> CampaignPortfolioTrend:
    """Build a trend covering the resolved period exactly, one point per day."""
    return CampaignPortfolioTrend(
        period,
        tuple(
            CampaignPortfolioTrendPoint(
                period.start + timedelta(days=offset), 10, Decimal("2.5"), 1
            )
            for offset in range(period.days)
        ),
        USD,
        RATIO,
        COUNT,
        MeasurementBasis.AD_ATTRIBUTED,
    )


def complete_portfolio(
    period: DateRange,
    rows: tuple[CampaignPortfolioRow, ...],
    authorized_count: int | None = None,
    totals: tuple[CampaignPortfolioComparison, ...] | None = None,
) -> CampaignPortfolioArtifact:
    """Build a COMPLETE portfolio over the full population, top-N selected by the domain.

    It is a served render-format-11 body: a one-channel sibling (attributed totals kept, no
    channel view) with its coverage block, because the decoders refuse the retired forms.
    """
    return build_campaign_portfolio(
        period=period,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=rows,
        totals=portfolio_totals(period) if totals is None else totals,
        trend=portfolio_trend(period),
        evidence=(CampaignListEvidence("Governed campaign catalog", period, (period,)),),
        authorized_count=authorized_count,
        evidence_status=campaign_portfolio_evidence_status(period, LABELS),
        channel_filter=ONE_CHANNEL_FILTER,
    )


ONE_CHANNEL_FILTER = CampaignChannelFilter(
    "sponsored_products",
    campaign_channel_filter_options((("sponsored_products", "Sponsored Products"),)),
)
"""A one-channel preset's sibling for that channel: it keeps the attributed totals (ADR-127)."""


LABELS = presentation_labels(load_catalog(pathlib.Path(__file__).resolve().parents[2]))
"""The dictionary's display labels, as the bakers and the handler read them."""


def with_missing_day(
    artifact: CampaignPortfolioArtifact, metric: CampaignPortfolioMetric, day: date
) -> CampaignPortfolioArtifact:
    """Fixture: the source hasn't sent `day` for `metric` (ADR-118 decision 5, unit-proven).

    Every other figure stays complete; the metric's change is withheld on every surface.
    """
    facts = tuple(
        replace(fact, missing_dates=(day,)) if fact.slot == metric.value else fact
        for fact in campaign_portfolio_coverage_facts(LABELS)
    )

    def held(item: CampaignPortfolioComparison) -> CampaignPortfolioComparison:
        if item.metric is not metric or item.current.value is None or item.prior.value is None:
            return item
        return replace(
            item, absolute_change=None, percentage_change=None, change=None, withheld=True
        )

    return replace(
        artifact,
        totals=tuple(held(item) for item in artifact.totals),
        rows=tuple(
            replace(row, comparisons=tuple(held(item) for item in row.comparisons))
            for row in artifact.rows
        ),
        evidence_status=write_evidence_status(artifact.period, facts),
    )


def all_channels_portfolio(
    period: DateRange,
    rows: tuple[CampaignPortfolioRow, ...],
    authorized_count: int | None = None,
) -> CampaignPortfolioArtifact:
    """Build the COMPLETE All channels body a pointer binds, over one sponsored channel.

    All channels is a multi-channel option (ADR-127): spend and impressions totals, a
    spend-only trend, the channel view and its coverage block.
    """
    channel, label = "sponsored_products", "Sponsored Products"
    total, by_channel = CAMPAIGN_PORTFOLIO_TOTAL_GRAIN, CAMPAIGN_PORTFOLIO_CHANNEL_GRAIN
    metric = CampaignPortfolioMetric
    return build_campaign_portfolio(
        period=period,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=rows,
        totals=(
            _comparison(metric.SPEND, Decimal("1000"), Decimal("900"), USD, total, period),
            _comparison(metric.IMPRESSIONS, 10000, 9000, COUNT, total, period),
        ),
        trend=CampaignPortfolioTrend(
            period,
            tuple(
                CampaignPortfolioTrendPoint(period.start + timedelta(days=offset), 10, None, None)
                for offset in range(period.days)
            ),
            USD,
            None,
            None,
            MeasurementBasis.AD_ATTRIBUTED,
        ),
        evidence=(CampaignListEvidence("Governed campaign catalog", period, (period,)),),
        authorized_count=authorized_count,
        evidence_status=campaign_portfolio_evidence_status(period, LABELS, multi_channel=True),
        channel_filter=CampaignChannelFilter(
            "all", campaign_channel_filter_options(((channel, label),))
        ),
        channel_view=CampaignMultiChannelView(
            (
                CampaignChannelTotalsRow(
                    channel,
                    label,
                    _comparison(
                        metric.SPEND, Decimal("1000"), Decimal("900"), USD, by_channel, period
                    ),
                    _comparison(metric.IMPRESSIONS, 10000, 9000, COUNT, by_channel, period),
                ),
            ),
            (
                CampaignChannelDeliveryRow(
                    channel,
                    label,
                    _comparison(metric.CLICKS, 100, 90, COUNT, by_channel, period),
                    _comparison(
                        metric.CTR, Decimal("0.01"), Decimal("0.01"), RATIO, by_channel, period
                    ),
                    _comparison(metric.CPC, Decimal("10"), Decimal("10"), USD, by_channel, period),
                    _comparison(
                        metric.CPM, Decimal("100"), Decimal("100"), USD, by_channel, period
                    ),
                ),
            ),
            False,
            (
                CampaignNotAddedTile(metric.PURCHASES, "Attributed purchases"),
                CampaignNotAddedTile(metric.PORTFOLIO_ROAS, "Attributed ROAS"),
            ),
            multi_channel_insights_line("All channels"),
        ),
    )
