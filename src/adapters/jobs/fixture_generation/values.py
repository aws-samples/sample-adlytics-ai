# SPDX-License-Identifier: MIT-0
"""The dev seed's value functions: pure functions of the declared shape (ADR-086, design §3.2-§3.3).

Every value is a function of the catalog's declarations and its seed, through a SHA-256 draw keyed
by what it draws (entity, catalog field, period, measure). There is no RNG object, no global
state and no clock. Nothing reads `first_day` or `last_day`: every position is relative to
`shape_anchor`, and only the dated events read `event_anchor` (through their resolved dates), so
a longer span adds days without changing an earlier one.

Every computation runs under `decimal.localcontext(prec=28)`; "quantize" is `ROUND_HALF_EVEN` to
four places, "round" is `to_integral_value(ROUND_HALF_EVEN)` and "floor" is `ROUND_FLOOR`.
"""

from __future__ import annotations

import decimal
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

from adapters.jobs.fixture_generation.shape import (
    BudgetShift,
    CampaignDay,
    CatalogShape,
    OrderedShippedDivergence,
    Pause,
    PriceShift,
    Promotion,
    ReturnRateShift,
    ShapeError,
)

FOUR_PLACES = Decimal("0.0001")
_TWO64 = Decimal(2**64)
_CURVE_BASE = Decimal(1200)
_HALO = Decimal("1.25")
_NEW_TO_BRAND = Decimal("0.50")
_RETAIL_PURCHASED = Decimal("1.80")
_CLICK_JITTER = Decimal("0.06")
_ORDER_VALUE_JITTER = Decimal("0.04")
_CPC_JITTER = Decimal("0.02")
_VIEWABLE_JITTER = Decimal("0.03")
_FREQUENCY_JITTER = Decimal("0.04")
_RPU_DRIFT = Decimal("0.01")


def unit(seed: str, *parts: object) -> Decimal:
    """Return a declared value in [0, 1): SHA-256 of the seed and the parts, first 8 bytes."""
    texts = (seed, *map(str, parts))
    if any("|" in text for text in texts):
        # The join must be injective, or two distinct tuples could share a draw (R6-12).
        raise ShapeError(f"hash input part contains '|': {texts!r}")
    digest = hashlib.sha256("|".join(texts).encode()).digest()
    return Decimal(int.from_bytes(digest[:8], "big")) / _TWO64


def band(seed: str, low: Decimal, high: Decimal, entity_key: str, field_path: str) -> Decimal:  # noqa: PLR0913, PLR0917 -- the design §3.2 signature
    """Return a per-entity constant: one independent draw per (entity, catalog field)."""
    return low + (high - low) * unit(seed, entity_key, field_path)


def jitter(seed: str, amplitude: Decimal, entity_key: str, period: str, measure: str) -> Decimal:  # noqa: PLR0913, PLR0917 -- the design §3.2 signature
    """Return symmetric multiplicative noise: 1 + amplitude x (2u - 1)."""
    return 1 + amplitude * (2 * unit(seed, entity_key, period, measure) - 1)


def isoweek(on: date) -> str:
    """Return the ISO week of `on`, for weekly drift: '2026-W40'. Never the weekday."""
    year, week, _ = on.isocalendar()
    return f"{year}-W{week:02d}"


def quantize(value: Decimal) -> Decimal:
    """Quantize to four places, half-even."""
    return value.quantize(FOUR_PLACES, rounding=ROUND_HALF_EVEN)


def rounded(value: Decimal) -> int:
    """Round to an integer, half-even."""
    return int(value.to_integral_value(rounding=ROUND_HALF_EVEN))


def floored(value: Decimal) -> int:
    """Round down to an integer."""
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _context() -> decimal.Context:
    return decimal.Context(prec=28, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class CampaignDayValues:
    """One campaign-day's generated measures, all zero on a zero-delivery day."""

    delivering: bool
    trend: Decimal
    traffic: int
    clicks: int
    purchases: int
    revenue: Decimal
    spend: Decimal
    measurable: int
    viewable: int
    viewable_estimated: int
    reach: int
    distinct_users: int


@dataclass(frozen=True, slots=True)
class ProductDayValues:
    """One product-day's final Vendor report values, net of returns."""

    units_shipped: int
    returns: int
    units_ordered: int
    revenue_per_unit: Decimal
    cost_per_unit: Decimal
    shipped_revenue: Decimal
    shipped_cogs: Decimal
    ordered_revenue: Decimal
    glance_views: int


@dataclass(slots=True)
class ShapeValues:
    """The value functions over one declared shape, with per-(entity, day) memoization."""

    shape: CatalogShape
    visible_campaigns: frozenset[str]
    _campaign_cache: dict[tuple[str, str, date], CampaignDayValues] = field(
        default_factory=dict[tuple[str, str, date], CampaignDayValues]
    )
    _units_cache: dict[tuple[str, str, date], int] = field(
        default_factory=dict[tuple[str, str, date], int]
    )
    _product_cache: dict[tuple[str, str, date], ProductDayValues] = field(
        default_factory=dict[tuple[str, str, date], ProductDayValues]
    )

    # --- shared positions and seasonality -------------------------------------------------

    @property
    def story_span(self) -> int:
        """S = story_days - 1."""
        return self.shape.story_days - 1

    def position(self, on: date) -> int:
        """Return a(t): days from the shape anchor, negative in the past."""
        return (on - self.shape.shape_anchor).days

    def story(self, on: date) -> int:
        """Return u(t): 0 before the story window, counting up through it, S from the anchor on."""
        return min(max(self.position(on) + self.story_span, 0), self.story_span)

    def _band(self, pair: tuple[Decimal, Decimal], entity: str, path: str) -> Decimal:
        return band(self.shape.seed, pair[0], pair[1], entity, path)

    def _jitter(self, amplitude: Decimal, entity: str, period: str, measure: str) -> Decimal:  # noqa: PLR0913, PLR0917 -- the design §3.2 signature
        return jitter(self.shape.seed, amplitude, entity, period, measure)

    def weekday_factor(self, entity: str, on: date) -> Decimal:
        """Return wk(e, t) = 1 + amp_e x (weekday_factors[t.weekday()] - 1)."""
        with decimal.localcontext(_context()):
            amplitude = self._band(
                (self.shape.weekday_amplitude.low, self.shape.weekday_amplitude.high),
                entity,
                "weekday_amplitude",
            )
            return 1 + amplitude * (self.shape.weekday_factors[on.weekday()] - 1)

    def month_curve(self, on: date) -> Decimal:
        """Return m(t): month_factors interpolated linearly between the 15th of each month."""
        with decimal.localcontext(_context()):
            if on.day >= 15:  # noqa: PLR2004 -- the declared knot day
                start = on.replace(day=15)
            else:
                start = (on.replace(day=1) - timedelta(days=1)).replace(day=15)
            end = (start.replace(day=1) + timedelta(days=32)).replace(day=15)
            low = self.shape.month_factors[start.month - 1]
            high = self.shape.month_factors[end.month - 1]
            return low + (high - low) * Decimal((on - start).days) / Decimal((end - start).days)

    def month_factor(self, entity: str, on: date) -> Decimal:
        """Return mo(e, t) = 1 + mamp_e x (m(t) - 1)."""
        with decimal.localcontext(_context()):
            amplitude = self._band(
                (self.shape.month_amplitude.low, self.shape.month_amplitude.high),
                entity,
                "month_amplitude",
            )
            return 1 + amplitude * (self.month_curve(on) - 1)

    # --- campaigns -------------------------------------------------------------------------

    def _spread(self, campaign: str, name: str) -> Decimal:
        pair = getattr(self.shape.campaign_spread, name)
        return self._band((pair.low, pair.high), campaign, f"campaign_spread.{name}")

    def spike_centre(self, campaign: str) -> date:
        """Return the spike centre, shape_anchor - S + 30 + floor(22 x u(c, 'spike_centre'))."""
        with decimal.localcontext(_context()):
            draw = floored(22 * unit(self.shape.seed, campaign, "spike_centre"))
        return self.shape.shape_anchor - timedelta(days=self.story_span - 30 - draw)

    def curve(self, campaign: str, scenario: str, on: date) -> Decimal:
        """Return the scenario's story curve, normalized to 1 before the story window."""
        position = Decimal(self.story(on))
        if scenario in {"growth_ramp", "stable_growth", "coverage_gap"}:
            return (_CURVE_BASE + 55 * position) / _CURVE_BASE
        if scenario == "sustained_decline":
            return (Decimal(3400) - 45 * position) / Decimal(3400)
        if scenario == "spike_and_fade":
            distance = abs((on - self.spike_centre(campaign)).days)
            return (_CURVE_BASE + max(Decimal(0), Decimal(2400) - distance * 180)) / _CURVE_BASE
        return Decimal(1)

    def trend(self, campaign: str, scenario: str, on: date) -> Decimal:
        """Return long_run x curve x (1 + slope_c x u / S)."""
        with decimal.localcontext(_context()):
            growth = self._spread(campaign, "annual_growth_pct") / 100
            slope = self._spread(campaign, "slope_pct") / 100
            long_run = 1 + growth * Decimal(self.position(on)) / Decimal(365)
            story = Decimal(self.story(on)) / Decimal(self.story_span)
            return long_run * self.curve(campaign, scenario, on) * (1 + slope * story)

    def conversion_shape(self, scenario: str, on: date) -> Decimal:
        """Return s0 + (s1 - s0) x u / S for the scenario; zero-conversion has none."""
        pair = self.shape.conversion_shape[scenario]
        story = Decimal(self.story(on)) / Decimal(self.story_span)
        return pair.low + (pair.high - pair.low) * story

    def delivering(self, campaign: str, on: date) -> bool:
        """Whether the campaign delivers on `on`: not before the launch, not on a pause day."""
        if campaign not in self.visible_campaigns:
            return True
        if campaign == self.shape.launch.campaign and on < self.shape.launch.first_delivery:
            return False
        return not any(
            isinstance(event, Pause)
            and event.campaign == campaign
            and event.first <= on <= event.last
            for event in self.shape.events
        )

    def event_factor(self, campaign: str, on: date) -> Decimal:
        """Return the traffic multiplier of every promotion and budget shift on this day."""
        factor = Decimal(1)
        if campaign not in self.visible_campaigns:
            return factor
        for event in self.shape.events:
            if isinstance(event, Promotion) and campaign in event.campaigns:
                if event.first <= on <= event.last:
                    factor *= 1 + event.lift_pct / 100
            elif isinstance(event, BudgetShift) and on >= event.on:
                if campaign == event.down:
                    factor *= 1 - event.shift_pct / 100
                elif campaign == event.up:
                    factor *= 1 + event.shift_pct / 100
        return factor

    def campaign_day(self, campaign: str, scenario: str, on: date) -> CampaignDayValues:
        """Return one campaign-day's measures (design §3.3 campaign measures)."""
        key = (campaign, scenario, on)
        cached = self._campaign_cache.get(key)
        if cached is None:
            cached = self._campaign_day(campaign, scenario, on)
            self._campaign_cache[key] = cached
        return cached

    def _campaign_day(self, campaign: str, scenario: str, on: date) -> CampaignDayValues:
        with decimal.localcontext(_context()):
            trend = self.trend(campaign, scenario, on)
            if not self.delivering(campaign, on):
                return CampaignDayValues(
                    False, trend, 0, 0, 0, Decimal(0), Decimal(0), 0, 0, 0, 0, 0
                )
            day = on.isoformat()
            level = self._spread(campaign, "level_pct") / 100
            volatility = self._spread(campaign, "volatility_pct") / 100
            traffic = max(
                0,
                rounded(
                    self.shape.traffic_base
                    * level
                    * trend
                    * self.weekday_factor(campaign, on)
                    * self.month_factor(campaign, on)
                    * self._jitter(volatility, campaign, day, "traffic")
                    * self.event_factor(campaign, on)
                ),
            )
            ctr = self._spread(campaign, "ctr_pct") / 100
            clicks = min(
                traffic,
                floored(traffic * ctr * self._jitter(_CLICK_JITTER, campaign, day, "clicks")),
            )
            if scenario == "zero_conversion":
                purchases = 0
            else:
                rate = (
                    self._spread(campaign, "conversion_rate_pct")
                    / 100
                    * self.conversion_shape(scenario, on)
                    * self._jitter(volatility, campaign, day, "conversion")
                )
                purchases = min(clicks, rounded(clicks * rate))
            revenue = quantize(
                purchases
                * self._spread(campaign, "order_value")
                * self._jitter(_ORDER_VALUE_JITTER, campaign, day, "order_value")
            )
            spend = quantize(
                clicks
                * self._spread(campaign, "cpc")
                * self._jitter(_CPC_JITTER, campaign, day, "cpc")
            )
            measurable = floored(traffic * self._spread(campaign, "measurable_share"))
            viewable = min(
                measurable,
                floored(
                    measurable
                    * self._spread(campaign, "viewable_share")
                    * self._jitter(_VIEWABLE_JITTER, campaign, day, "viewable")
                ),
            )
            reach = min(
                traffic,
                floored(
                    traffic
                    / (
                        self._spread(campaign, "frequency")
                        * self._jitter(_FREQUENCY_JITTER, campaign, day, "frequency")
                    )
                ),
            )
            return CampaignDayValues(
                delivering=True,
                trend=trend,
                traffic=traffic,
                clicks=clicks,
                purchases=purchases,
                revenue=revenue,
                spend=spend,
                measurable=measurable,
                viewable=viewable,
                viewable_estimated=(viewable + measurable) // 2,
                reach=reach,
                distinct_users=min(reach, clicks + 20),
            )

    def campaign_measure(self, values: CampaignDayValues, column: str) -> Decimal | int | None:
        """Map one AMC target column to its value; None when the column is not an AMC measure."""
        purchases = values.purchases
        measures: dict[str, Callable[[], Decimal | int]] = {
            "impressions": lambda: values.traffic,
            "clicks": lambda: values.clicks,
            "viewable_impressions": lambda: values.viewable,
            "measurable_impressions": lambda: values.measurable,
            "viewable_impressions_estimated": lambda: values.viewable_estimated,
            "reach_users": lambda: values.reach,
            "conversions": lambda: purchases * 2,
            "purchases": lambda: purchases,
            "purchases_incl_halo": lambda: purchases + purchases // 4,
            "purchases_new_to_brand_12mo": lambda: purchases // 2,
            "purchases_new_to_brand_incl_halo_12mo": lambda: purchases // 2 + purchases // 8,
            "units_attributed": lambda: purchases * 2,
            "units_attributed_incl_halo": lambda: purchases * 3,
            "units_purchased": lambda: purchases * 2,
            "distinct_user_count": lambda: values.distinct_users,
            "matched_purchase_credit": lambda: quantize(Decimal(purchases)),
            "spend": lambda: values.spend,
            "revenue_attributed": lambda: values.revenue,
            "revenue_attributed_incl_halo": lambda: quantize(values.revenue * _HALO),
            "revenue_attributed_deduplicated_incl_halo": lambda: quantize(values.revenue * _HALO),
            "revenue_new_to_brand_12mo": lambda: quantize(values.revenue * _NEW_TO_BRAND),
            "revenue_purchased": lambda: quantize(values.revenue * _RETAIL_PURCHASED),
        }
        produce = measures.get(column)
        return None if produce is None else produce()

    # --- retail ----------------------------------------------------------------------------

    def _retail_band(self, product: str, name: str) -> Decimal:
        pair = getattr(self.shape.retail, name)
        return self._band((pair.low, pair.high), product, f"retail.{name}")

    def units_shipped(self, product: str, scenario: str, on: date) -> int:
        """Return U(p, t); days before first_day evaluate the same pure function."""
        key = (product, scenario, on)
        cached = self._units_cache.get(key)
        if cached is not None:
            return cached
        with decimal.localcontext(_context()):
            growth = self.shape.retail.annual_growth_pct[scenario] / 100
            story_trend = self.shape.retail.story_trend_pct_per_day[scenario] / 100
            trend = (1 + growth * Decimal(self.position(on)) / Decimal(365)) * (
                1 + story_trend * self.story(on)
            )
            units = max(
                1,
                rounded(
                    self._retail_band(product, "units_level")
                    * trend
                    * self.weekday_factor(product, on)
                    * self.month_factor(product, on)
                    * self._jitter(
                        self.shape.retail.noise_pct / 100, product, on.isoformat(), "units"
                    )
                ),
            )
        self._units_cache[key] = units
        return units

    def return_rate(self, product: str, scenario: str, on: date) -> Decimal:
        """Return rr_p(t): the scenario rate, or the shift product's ramp, held after its end."""
        base = self.shape.retail.return_rate[scenario]
        for event in self.shape.events:
            if isinstance(event, ReturnRateShift) and event.product == product:
                return _ramp(base, event.return_rate_end, event.first, event.ramp_end, on)
        return base

    def order_ratio(self, product: str, on: date) -> Decimal:
        """Return r_p(t): the product's ordered/shipped ratio, ramped on the divergence product."""
        with decimal.localcontext(_context()):
            base = self._retail_band(product, "order_to_ship_ratio")
            for event in self.shape.events:
                if isinstance(event, OrderedShippedDivergence) and product in event.products:
                    return _ramp(base, event.ordered_ratio_end, event.first, event.ramp_end, on)
            return base

    def price(self, product: str, on: date) -> Decimal:
        """Return the price factor: 1 + pct/100 on the price-shift product from its first day."""
        for event in self.shape.events:
            if isinstance(event, PriceShift) and event.product == product and on >= event.first:
                return 1 + Decimal(event.pct) / 100
        return Decimal(1)

    def product_day(self, product: str, scenario: str, on: date) -> ProductDayValues:
        """Return one product-day's final report (design §3.3 retail)."""
        key = (product, scenario, on)
        cached = self._product_cache.get(key)
        if cached is None:
            cached = self._product_day(product, scenario, on)
            self._product_cache[key] = cached
        return cached

    def _product_day(self, product: str, scenario: str, on: date) -> ProductDayValues:
        with decimal.localcontext(_context()):
            units = self.units_shipped(product, scenario, on)
            lagged = sum(
                (
                    lag.share
                    * self.units_shipped(product, scenario, on - timedelta(days=lag.lag_days))
                    for lag in self.shape.retail.returns_lag_profile
                ),
                Decimal(0),
            )
            returns = rounded(self.return_rate(product, scenario, on) * lagged)
            rpu = (
                self._retail_band(product, "revenue_per_unit")
                * self._jitter(_RPU_DRIFT, product, isoweek(on), "rpu")
                * self.price(product, on)
            )
            cost = rpu * self._retail_band(product, "cogs_ratio")
            ordered = rounded(units * self.order_ratio(product, on))
            views = max(
                1,
                rounded(
                    ordered
                    * self._retail_band(product, "views_per_ordered_unit")
                    * self._jitter(
                        self.shape.retail.views_noise_pct / 100, product, on.isoformat(), "views"
                    )
                ),
            )
            return ProductDayValues(
                units_shipped=units,
                returns=returns,
                units_ordered=ordered,
                revenue_per_unit=rpu,
                cost_per_unit=cost,
                shipped_revenue=quantize((units - returns) * rpu),
                shipped_cogs=quantize((units - returns) * cost),
                ordered_revenue=quantize((ordered - returns) * rpu),
                glance_views=views,
            )

    @staticmethod
    def product_measure(values: ProductDayValues, column: str) -> Decimal | int | None:
        """Map one Vendor target column to its value; None when it is not a Vendor measure."""
        measures: dict[str, Decimal | int] = {
            "units_shipped": values.units_shipped,
            "units_ordered": values.units_ordered,
            "customer_returns": values.returns,
            "revenue_shipped": values.shipped_revenue,
            "cogs_shipped": values.shipped_cogs,
            "revenue_ordered": values.ordered_revenue,
            "glance_views": values.glance_views,
        }
        return measures.get(column)

    # --- events read by the row plan --------------------------------------------------------

    def campaign_day_event(self, kind: str, campaign: str, on: date) -> bool:
        """Whether a declared single-day campaign event (coverage gap, suppressed rows) is here."""
        return any(
            isinstance(event, CampaignDay)
            and event.kind == kind
            and event.campaign == campaign
            and event.on == on
            for event in self.shape.events
        )


def _ramp(start: Decimal, end: Decimal, first: date, last: date, on: date) -> Decimal:  # noqa: PLR0913, PLR0917
    """Linear from `start` on `first` to `end` on `last`, `start` before and `end` after."""
    if on <= first:
        return start
    if on >= last:
        return end
    with decimal.localcontext(_context()):
        return start + (end - start) * Decimal((on - first).days) / Decimal((last - first).days)
