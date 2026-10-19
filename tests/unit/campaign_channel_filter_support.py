# SPDX-License-Identifier: MIT-0
"""Builders for Campaigns channel filter bakes (ADR-127): one parsed batch, real reduction.

Each fictional campaign states its window totals; the builder lays them out as the parsed
answers one measured batch yields, and the real renderer reduces every filter sibling from
them, so a test sees exactly what the baker writes.
"""

# ruff: noqa: PLR0913, PLR0917
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

from adapters.campaign_artifact_renderer import (
    CampaignArtifactRenderer,
    CampaignArtifactRenderInput,
    CampaignPortfolioRenderResult,
    ParsedPortfolio,
    Viewability,
)
from adapters.campaign_measurement import (
    CampaignArtifactMeasurementService,
    CampaignMeasurementDependencies,
    CampaignMeasurementSelection,
)
from adapters.inbound.tool_handlers.measurement import MeasurementTooling
from adapters.outbound.metric_dictionary import load_catalog
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.campaign_attribution import (
    CampaignSelectionCapability,
    SafeCampaignDisplaySignature,
)
from domain.presentation.campaign_portfolio import (
    CampaignPortfolioArtifact,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
)
from domain.presentation.campaigns import EffectiveNameStatus
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
)
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import PresentationEvidenceInspectionPort

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCOPE = CommerceScopeKey("amazon_retail_us")
PLATFORM = PlatformKey("amazon_ads")
MARKETPLACE = MarketplaceKey("amazon_us")
USD = Units(UnitKind.CURRENCY, "USD")
COUNT = Units(UnitKind.COUNT)
RATIO = Units(UnitKind.RATIO)
CHANNEL_ORDER = (
    "amazon_dsp",
    "amazon_sponsored_products",
    "amazon_sponsored_brands",
    "amazon_sponsored_display",
    "amazon_sponsored_television",
)
CHANNEL_LABELS = {
    "amazon_dsp": "Amazon DSP",
    "amazon_sponsored_products": "Sponsored Products",
    "amazon_sponsored_brands": "Sponsored Brands",
    "amazon_sponsored_display": "Sponsored Display",
    "amazon_sponsored_television": "Sponsored Television",
}


@dataclass(frozen=True, slots=True)
class Figures:
    """One window's additive totals for one fictional campaign."""

    spend: Decimal
    revenue: Decimal
    purchases: int
    impressions: int
    clicks: int


@dataclass(frozen=True, slots=True)
class FictionalCampaign:
    """A fictional campaign, its channel, and both windows' totals."""

    name: str
    channel: str
    current: Figures
    prior: Figures
    viewable: tuple[int, int] | None = None
    """DSP only: (viewable, measurable) impressions in the current window."""
    prior_viewable: tuple[int, int] | None = None

    @property
    def selection(self) -> CampaignMeasurementSelection:
        """Return the re-attested selection the baker measures this campaign under."""
        return CampaignMeasurementSelection(
            CampaignKey(f"campaign:{self.name.lower().replace(' ', '-')}", SCOPE, PLATFORM),
            ChannelKey(self.channel),
            MARKETPLACE,
            SafeCampaignDisplaySignature(
                effective_name=self.name,
                channel=CHANNEL_LABELS[self.channel],
                marketplace="United States",
                platform="Amazon Ads",
                effective_name_status=EffectiveNameStatus.AVAILABLE,
                effective_name_valid_from=date(2026, 1, 1),
            ),
        )


def _f(spend: str, revenue: str, purchases: int, impressions: int, clicks: int) -> Figures:
    return Figures(Decimal(spend), Decimal(revenue), purchases, impressions, clicks)


ESTATE = (
    FictionalCampaign(
        "Fictional DSP Prospecting",
        "amazon_dsp",
        _f("700", "2100", 21, 70000, 49),
        _f("560", "1400", 14, 63000, 42),
        viewable=(4200, 6300),
        prior_viewable=(3500, 5600),
    ),
    FictionalCampaign(
        "Fictional DSP Retargeting",
        "amazon_dsp",
        _f("1400", "3500", 28, 210000, 63),
        _f("1750", "2800", 35, 196000, 70),
        viewable=(10500, 16800),
        prior_viewable=(9800, 16100),
    ),
    FictionalCampaign(
        "Fictional SP Exact",
        "amazon_sponsored_products",
        _f("350", "1050", 14, 28000, 280),
        _f("350", "840", 14, 35000, 210),
    ),
    FictionalCampaign(
        "Fictional SB Launch",
        "amazon_sponsored_brands",
        _f("420", "840", 7, 21000, 140),
        _f("280", "700", 7, 17500, 140),
    ),
)
"""Two Amazon DSP campaigns and two sponsored ones: All, Sponsored ads and three channels."""


def _identity(campaign: FictionalCampaign) -> tuple[str, str]:
    selection = campaign.selection
    return selection.campaign.value, selection.marketplace.value


def _per_campaign(
    estate: tuple[FictionalCampaign, ...], value: str, window: str
) -> dict[tuple[str, ...], Decimal | int]:
    return {_identity(item): getattr(getattr(item, window), value) for item in estate}


def _quotient(
    estate: tuple[FictionalCampaign, ...], numerator: str, denominator: str, window: str
) -> dict[tuple[str, ...], Decimal | int]:
    """Omit the identities whose denominator is zero, as a covered ratio answer does (ADR-087)."""
    values: dict[tuple[str, ...], Decimal | int] = {}
    for item in estate:
        figures = getattr(item, window)
        top, bottom = getattr(figures, numerator), getattr(figures, denominator)
        if bottom:
            values[_identity(item)] = Decimal(top) / Decimal(bottom)
    return values


def _daily(
    estate: tuple[FictionalCampaign, ...], value: str, period: DateRange
) -> dict[tuple[str, ...], Decimal | int]:
    """Lay each window total on the window's first day; the other days delivered nothing."""
    return {
        (period.start.isoformat(), *_identity(item)): getattr(item.current, value)
        for item in estate
    }


@dataclass(frozen=True, slots=True)
class ViewabilityCoverage:
    """Which days of each window the DSP views source has sent."""

    missing: tuple[date, ...] = ()
    absent: bool = False
    prior_missing: tuple[date, ...] = ()
    """Days of the prior window the source hasn't sent."""


def parsed_portfolio(
    estate: tuple[FictionalCampaign, ...],
    period: DateRange,
    viewability: ViewabilityCoverage = ViewabilityCoverage(),  # noqa: B008 -- immutable
) -> ParsedPortfolio:
    """Return the parsed answers one measured batch yields for this fictional estate."""
    answers: dict[
        tuple[str, str, str], tuple[dict[tuple[str, ...], Decimal | int], Units, tuple[str, ...]]
    ] = {}
    for window, role in (("current", "current"), ("prior", "prior")):
        answers[("row", "ad_spend", role)] = (_per_campaign(estate, "spend", window), USD, ())
        answers[("row", "roas_attributed_14d", role)] = (
            _quotient(estate, "revenue", "spend", window),
            RATIO,
            (),
        )
        answers[("row", "purchases", role)] = (
            _per_campaign(estate, "purchases", window),
            COUNT,
            (),
        )
        answers[("row", "ctr", role)] = (
            _quotient(estate, "clicks", "impressions", window),
            RATIO,
            (),
        )
        answers[("portfolio", "ad_spend", role)] = (_per_campaign(estate, "spend", window), USD, ())
        answers[("portfolio", "attributed_revenue_14d", role)] = (
            _per_campaign(estate, "revenue", window),
            USD,
            (),
        )
        answers[("portfolio", "purchases", role)] = (
            _per_campaign(estate, "purchases", window),
            COUNT,
            (),
        )
        answers[("portfolio", "impressions", role)] = (
            _per_campaign(estate, "impressions", window),
            COUNT,
            (),
        )
        answers[("delivery", "clicks", role)] = (_per_campaign(estate, "clicks", window), COUNT, ())
    for metric, value, units in (
        ("ad_spend", "spend", USD),
        ("attributed_revenue_14d", "revenue", USD),
        ("purchases", "purchases", COUNT),
        ("impressions", "impressions", COUNT),
    ):
        answers[("trend", metric, "current")] = (_daily(estate, value, period), units, ())
    dsp = tuple(item for item in estate if item.viewable is not None)
    views: dict[tuple[str, str], Viewability | None] = {}
    if dsp:
        for role, attribute, missing in (
            ("current", "viewable", viewability.missing),
            ("prior", "prior_viewable", viewability.prior_missing),
        ):
            for index, metric in enumerate(("viewable_impressions", "measurable_impressions")):
                views[(metric, role)] = (
                    None
                    if viewability.absent and role == "current"
                    else Viewability(
                        {
                            _identity(item): cast("tuple[int, int]", getattr(item, attribute))[
                                index
                            ]
                            for item in dsp
                        },
                        missing,
                    )
                )
    return ParsedPortfolio(answers, frozenset({"roas_attributed_14d", "ctr"}), views)


def renderer() -> CampaignArtifactRenderer:
    """Return the real renderer over the committed dictionary; it never executes a query."""
    return CampaignArtifactRenderer(
        measurements=CampaignArtifactMeasurementService(
            CampaignMeasurementDependencies(
                scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
                catalog=load_catalog(ROOT),
                contracts=(),
                clock=lambda: datetime(2026, 8, 22, 12, tzinfo=UTC),
            )
        ),
        evidence=cast("PresentationEvidenceInspectionPort", None),
        tooling=cast("MeasurementTooling", None),
    )


def render_input(
    estate: tuple[FictionalCampaign, ...], period: DateRange
) -> CampaignArtifactRenderInput:
    """Return the bake's render input for this estate at one window."""
    expiry = datetime.max.replace(tzinfo=UTC)
    return CampaignArtifactRenderInput(
        selections=tuple(item.selection for item in estate),
        period=period,
        prior_period=DateRange(
            period.start - timedelta(days=period.days), period.start - timedelta(days=1)
        ),
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        capabilities=tuple(
            CampaignSelectionCapability(f"baked:{index:05d}", expiry)
            for index in range(len(estate))
        ),
        channel_order=CHANNEL_ORDER,
    )


def filter_siblings(
    estate: tuple[FictionalCampaign, ...] = ESTATE,
    period: DateRange = DateRange(date(2026, 8, 15), date(2026, 8, 21)),  # noqa: B008
    viewability: ViewabilityCoverage = ViewabilityCoverage(),  # noqa: B008 -- immutable
) -> tuple[CampaignPortfolioArtifact, ...]:
    """Every filter sibling the baker renders for this estate, All channels first."""
    rendered = renderer().filters_from_parsed(
        parsed_portfolio(estate, period, viewability), render_input(estate, period)
    )
    assert rendered is not None
    return rendered


@dataclass
class FilterCompiler:
    """A compiler whose one batch is this fictional estate, reduced by the real renderer."""

    estate: tuple[FictionalCampaign, ...] = ESTATE
    viewability: ViewabilityCoverage = field(default_factory=ViewabilityCoverage)
    identities: tuple[str, ...] = ("publication-a",)

    def render_portfolio(
        self, request: CampaignArtifactRenderInput
    ) -> CampaignPortfolioRenderResult:
        by_name = {item.selection.campaign: item for item in self.estate}
        estate = tuple(by_name[selection.campaign] for selection in request.selections)
        rendered = renderer().filters_from_parsed(
            parsed_portfolio(estate, request.period, self.viewability), request
        )
        assert rendered is not None
        return CampaignPortfolioRenderResult(rendered[0], self.identities, rendered[1:])
