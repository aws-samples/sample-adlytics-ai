# SPDX-License-Identifier: MIT-0
"""The declared shape of the fictional dev seed: strict narrowing of the catalog `shape` section.

Every value the generator draws or bounds comes from here (ADR-086). The loader is fail-closed:
every field is required, unknown keys are refused, every Decimal is a YAML string, and each
failure names the catalog path. Event offsets resolve to dates once, here, from the two anchors;
no other code computes an event date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final, Literal, cast

type RawMapping = dict[str, object]
type UnrestatedReturns = Literal["first_report", "final"]

STORY_DAYS_MAX = 56
"""The served windows' horizon: the story fits inside the newest 56 days (design §2.2)."""
EVENT_OFFSET_RANGE = (-13, 0)
"""The offsets the designed states are proven for (design §3.1)."""
SLACK_RANGE = (0, 2)


class ShapeError(ValueError):
    """The catalog `shape` section is malformed; the message names the path."""


@dataclass(frozen=True, slots=True)
class Band:
    """An ordered pair of declared Decimals, low < high."""

    low: Decimal
    high: Decimal


@dataclass(frozen=True, slots=True)
class FixtureSpan:
    """The declared first and last fixture day; nothing else reads them for values."""

    first_day: date
    last_day: date

    @property
    def span_days(self) -> int:
        """Days from `first_day` through `last_day`, inclusive."""
        return (self.last_day - self.first_day).days + 1


@dataclass(frozen=True, slots=True)
class Launch:
    """The launch campaign and its first delivering day."""

    campaign: str
    first_delivery: date
    """`shape_anchor + first_delivery_offset`, resolved once."""


@dataclass(frozen=True, slots=True)
class AttributionMaturity:
    """The AMC attributed-measure factors on the last two days (age 0 and age 1)."""

    age_0: Decimal
    age_1: Decimal


@dataclass(frozen=True, slots=True)
class NewToBrandBuyersShape:
    """How the periodic new-to-brand buyers fixture (pack 1) is drawn from the AMC corpus.

    Each complete ISO week and calendar month in the span is one delivery of one platform row.
    Its distinct buyers are `buyers_per_purchase` times that period's credited purchases in the
    attributed-conversion corpus, rounded down, so a buyer count never exceeds the purchases it
    could have made (design §5.5). The ISO week `not_returned_week` returns only a suppressed row
    for one shopper, so the load writes its platform cell `not_returned` (ADR-099 point 8).
    """

    buyers_per_purchase: Decimal
    not_returned_week: date


@dataclass(frozen=True, slots=True)
class NewToBrandBuyersChannelShape:
    """How the per-ad-product new buyers fixture (pack 2) differs from pack 1's draw.

    Each complete ISO week and calendar month is one delivery of one row per ad product with
    credited purchases in the period, drawn with pack 1's `buyers_per_purchase` from that ad
    product's own purchases, so it never exceeds them or the period's platform buyers. In the ISO
    week `missing_week`, `missing_ad_product` returns only a suppressed one-shopper row with its
    keys NULL: it had spend that week, so it is on the roster and reads "not available"
    (design §4.5). Pack 1's not-returned week returns one suppressed row here too.
    """

    missing_week: date
    missing_ad_product: str


@dataclass(frozen=True, slots=True)
class NewToBrandBuyersCampaignShape:
    """How the per-campaign new buyers fixture (pack 3) differs from pack 2's draw.

    Each calendar month is one delivery of one row per campaign with credited purchases in the
    month, drawn with pack 1's `buyers_per_purchase` from that campaign's own purchases, so it
    never exceeds them or the month's platform buyers. Every campaign with a credited purchase
    has spend that month, so it is on the campaign roster. Two months are shaped, each returning
    one merged suppressed row, keys NULL, for the campaigns it drops:

    - `withdrawn_month`: the first `missing_share` of that month's campaigns, by source key
      (rounded up), don't return, so a roster of 20 or more loses more than a fifth and the
      grain set is withdrawn, pointing to new buyers per channel (design §4.4);
    - `missing_month`: only `missing_campaign` doesn't return, so it reads "not available" and
      the rest are served (the graded control).
    """

    withdrawn_month: date
    missing_share: Decimal
    missing_month: date
    missing_campaign: str


@dataclass(frozen=True, slots=True)
class ExposureCellShape:
    """One declared cell of pack 5's grid in one period: its source set and identity space."""

    period_start: date
    exposure_source_set: str
    identity_space: str


@dataclass(frozen=True, slots=True)
class ExposureGroupsShape:
    """How the exposure-groups fixture (pack 5, `amc-exposure-groups`) is drawn from the corpus.

    Each complete ISO week and calendar month is one delivery of the 3 x 2 grid. A period's DSP
    and sponsored impressions and raw costs come from the `amc-dsp-impressions` and
    `amc-reach-frequency` corpora over its days; `dsp_only_share` of the DSP side goes to the
    DSP-only group and the rest to both, and `sponsored_only_share` of the sponsored side to the
    sponsored-only group and the rest to both. `account_share` of each group's figures sits on
    account IDs and the rest on device or browser IDs. Shoppers are impressions over
    `impressions_per_shopper`, rounded down; outcome counts are the declared rates of shoppers
    (device or browser IDs at `device_rate_factor` of them), rounded down; each purchaser buys
    `purchases_per_purchaser` times, at `order_value`. Four cells or periods are shaped:

    - `not_returned`: that cell returns only a suppressed one-shopper row (reads not available);
    - `withheld_path`: that cell held `withheld_shoppers` shoppers but one source path had fewer
      than two, so it returns suppressed with a `(path i of n)` reason, its purchase-side sums and
      currency NULL (plan D19);
    - `click_shoppers`: that cell holds `click_shoppers` Sponsored Products shoppers who each saw 3
      impressions and made one billed click inside W, so its window spend is their clicks' spend;
    - `currency_disagreement_week`: one row's sponsored currency disagrees with its MIN twin, so
      the load refuses that delivery and it is never landed.
    """

    impressions_per_shopper: int
    dsp_only_share: Decimal
    sponsored_only_share: Decimal
    account_share: Decimal
    search_rate: Decimal
    detail_page_view_rate: Decimal
    purchaser_rate: Decimal
    ntb_share: Decimal
    device_rate_factor: Decimal
    purchases_per_purchaser: Decimal
    order_value: Decimal
    not_returned: ExposureCellShape
    withheld_path: ExposureCellShape
    withheld_shoppers: int
    click_cell: ExposureCellShape
    click_shoppers: int
    click_spend: Decimal
    currency_disagreement_week: date


@dataclass(frozen=True, slots=True)
class AcquisitionCohortsShape:
    """How the cohort fixtures (packs 14 and 15, `amc-acquisition-cohorts*`) are drawn.

    One delivery per cohort vintage (the 13 calendar months ending with its as-of month). Each
    cohort month and acquisition group holds `cohort_buyers` x its `group_shares` entry
    (dsp_only, sponsored_only, dsp_and_sponsored), `account_share` of them account IDs; in
    follow-up month k `repeat_rates[k-1]` of the cohort buy again, `first_repeat_share` of those
    for the first time, the cumulative count summing the first-time ones. The paid path repeats at
    `arp_lift` x the account-ID rate; a quarterly cohort's quarter j at `quarter_lift` x
    `repeat_rates[j-1]`. Three vintages are shaped, by as-of month offset from the shape anchor:
    `refused` (a cohort size differs between two follow-up rows), `degraded` (monthly, with the
    absent cohort, the per-path withheld cell and a degraded dsp_only slice) and `quarterly` (the
    calendar-quarter variant the degraded vintage switched on). On the degraded vintage pack 15's
    own count of `arp_check_gap` is one under pack 14's.
    """

    cohort_buyers: int
    group_shares: tuple[Decimal, ...]
    account_share: Decimal
    repeat_rates: tuple[Decimal, ...]
    first_repeat_share: Decimal
    purchases_per_buyer: Decimal
    order_value: Decimal
    arp_lift: Decimal
    quarter_lift: Decimal
    refused_vintage: date
    degraded_vintage: date
    quarterly_vintage: date
    absent_cohort: date
    absent_group: str
    withheld_cohort: date
    withheld_k: int
    withheld_group: str
    withheld_shoppers: int
    arp_check_gap_cohort: date
    arp_check_gap_group: str


@dataclass(frozen=True, slots=True)
class ExposureFrequencyShape:
    """How the frequency fixtures (packs 6 and 7, `amc-exposure-frequency-*`) are drawn.

    Each complete calendar month and quarter is one delivery of every bucket by two identity
    spaces (pack 7: per ad product). A period's impressions and raw costs come from the
    `amc-dsp-impressions` and `amc-reach-frequency` corpora over its days, per ad product for
    pack 7 (DSP is `amazon_dsp`). `account_share` of each slice's impressions sits on account
    IDs. Shoppers in a slice are its impressions over the shares' mean impressions per shopper
    (each single-count bucket holds that many; `10_plus` holds `ten_plus_impressions`), split by
    `bucket_shares` and rounded down; a bucket's impressions are its shoppers times its count,
    and its spend the slice's spend in proportion. Purchasers are `purchaser_rates` of shoppers
    (device or browser IDs at `device_rate_factor` of them), each buying
    `purchases_per_purchaser` times at `order_value`. The coarse scheme merges 4 to 6 and 7 to 9.
    Two months are shaped:
    - `withheld_path`: that month the declared bucket and identity space held
      `withheld_shoppers` shoppers but one source path had fewer than two, so it comes back
      withheld with a `(path i of n)` reason (plan D19); its slice grades degraded, so every
      later month is delivered under the coarse scheme (sticky, plan D10);
    - `null_bucket`: that month one account row's bucket is NULL, so the load refuses the
      delivery (`FREQUENCY_BUCKET_NULL`) and it is never landed.
    """

    bucket_shares: tuple[Decimal, ...]
    ten_plus_impressions: int
    purchaser_rates: tuple[Decimal, ...]
    account_share: Decimal
    device_rate_factor: Decimal
    ntb_share: Decimal
    purchases_per_purchaser: Decimal
    order_value: Decimal
    withheld_month: date
    withheld_bucket: str
    withheld_space: str
    withheld_shoppers: int
    withheld_ad_product: str
    null_bucket_month: date


@dataclass(frozen=True, slots=True)
class AudienceSegment:
    """One declared segment of the audience fixtures: its source ID, free text and categories."""

    segment_id: str
    name: str
    description: str
    categories: tuple[str, str, str] | None = None
    """ASI only: `category_level_1`, `category_level_2` and `category_path`."""


@dataclass(frozen=True, slots=True)
class AudienceSegmentsShape:
    """How the audience fixtures (packs 9, 13 and 8, design §6.5) are drawn.

    Every complete calendar month is one delivery per pack. Pack 9 returns each DSP segment's
    name, pack 13 each ASI segment's metadata, and pack 8 one row per DSP campaign with
    impressions in the month (the `amc-dsp-impressions` corpus) and DSP segment: `segment_shares`
    of the campaign's impressions and raw cost matched to each segment (overlapping, so they
    don't sum to 1), `impressions_per_shopper` impressions per reached shopper, `purchaser_rate`
    of them buying `purchases_per_purchaser` times at `order_value`. Declared shapes:
    - a DSP and an ASI segment share one ID and stay separate rows (ADR-103 point 6);
    - a classified Amazon and a classified third-party demographic segment, and unclassified ones;
    - a segment name with an embedded quote, which the CSV escapes with a backslash;
    - `name_withheld`: that month the segment's pack 9 row comes back withheld and is dropped, so
      its name arrives a month later (design §5.4);
    - `roster_gap`: that month one campaign's row for the segment comes back withheld the live
      multi-source way, so it is missing from a roster that listed it the month before.
    """

    dsp_segments: tuple[AudienceSegment, ...]
    asi_segments: tuple[AudienceSegment, ...]
    segment_shares: tuple[Decimal, ...]
    impressions_per_shopper: int
    purchaser_rate: Decimal
    purchases_per_purchaser: Decimal
    order_value: Decimal
    name_withheld_month: date
    name_withheld_segment: str
    roster_gap_month: date
    roster_gap_segment: str
    roster_gap_campaign: str


@dataclass(frozen=True, slots=True)
class ProfileSource:
    """One buyer-profile pack's declared segments (packs 10-12) and their shares of each base."""

    source: str
    """The contract's `profile_path` literal: `asi_in_market`, `asi_lifestyle` or
    `dsp_exposed_only`."""
    segments: tuple[str, ...]
    exposed_shares: tuple[Decimal, ...]
    buyer_shares: tuple[Decimal, ...]


@dataclass(frozen=True, slots=True)
class SegmentProfileShape:
    """How the buyer-profile fixtures (packs 10-12, design §5.5, §6.5 C2) are drawn.

    Every complete calendar month is one delivery per pack: one row per declared segment and one
    `profiled_total` row. `profiled_exposed` shoppers are profiled, `buyer_rate` of them buy in
    the month, `ntb_share` of those new to the brand; a segment holds `exposed_shares[i]` of the
    exposed and `buyer_shares[i]` of the buyers (segments overlap, so neither sums to 1).
    Declared shapes:
    - `asi_from`: the paid packs 10-11 deliver only from that month, so earlier months have the
      standard path alone (ASI absent; the page defaults to "DSP-exposed buyers only");
    - one paid in-market segment pack 13 doesn't describe, which reads by its ID with "name not
      available yet" (partial enrolment);
    - `total_withheld`: that month the source's `profiled_total` row comes back withheld, so the
      load writes it not_returned and every share and index of that delivery is not available;
    - `segment_withheld`: that month the source's segment row comes back withheld per path with
      two or more shoppers, the conversions path under the bar (plan D19).
    """

    profiled_exposed: int
    buyer_rate: Decimal
    ntb_share: Decimal
    sources: tuple[ProfileSource, ...]
    asi_from: date
    totals_withheld: tuple[tuple[date, str], ...]
    """(month, source) whose `profiled_total` row comes back withheld."""
    segments_withheld: tuple[tuple[date, str, str, int], ...]
    """(month, source, segment, shoppers) whose segment row comes back withheld per path."""

    def source(self, name: str) -> ProfileSource:
        """Return one pack's declared segments by its `profile_path`."""
        return next(item for item in self.sources if item.source == name)


@dataclass(frozen=True, slots=True)
class DspReachShape:
    """Which UTC days the DSP reach fixture (pack 4, `amc-dsp-reach`) delivers, one per day.

    Pack 4 lands long after the DSP delivery contracts, so its few days are the only covered ones
    and every earlier DSP day is uncovered (plan D6). On `suppressed_day` the visible DSP
    campaign `suppressed_campaign` had one shopper, below the bar of 2: AMC returns its row
    suppressed with its LOW keys NULL, and the pack's `reject_delivery` refuses that day's
    delivery whole. Every other declared day is clean.
    """

    days: tuple[date, ...]
    suppressed_day: date
    suppressed_campaign: str


@dataclass(frozen=True, slots=True)
class DeduplicatedAttributionShape:
    """How each model splits one day's deduplicated total across channels (dev-seeding §2).

    `channel_weights[model][ad_product]` weighs a channel's own attributed measure for that
    model. Every model splits the same day total, so a channel's credit is the total times its
    weighted measure over the sum of weighted measures; equal weights credit each channel its
    own measure, as linear credit does. A `whole_credit_models` model credits each purchase
    whole to one touch (first or last), so its purchase credit per ad product is a whole number.
    """

    channel_weights: dict[str, dict[str, Decimal]]
    whole_credit_models: frozenset[str]


@dataclass(frozen=True, slots=True)
class CampaignSpread:
    """Per-campaign constant bands, one independent draw per campaign and field."""

    level_pct: Band
    annual_growth_pct: Band
    slope_pct: Band
    volatility_pct: Band
    cpc: Band
    conversion_rate_pct: Band
    order_value: Band
    ctr_pct: Band
    measurable_share: Band
    viewable_share: Band
    frequency: Band


@dataclass(frozen=True, slots=True)
class CampaignChecks:
    """Generator and lake-probe funnel bands, fixed at review and never widened."""

    daily_cvr_max: Decimal
    window_cvr_band: Band
    window_roas_band: Band


@dataclass(frozen=True, slots=True)
class ReturnsLag:
    """One returns-lag step: the share of returns arriving `lag_days` after shipment."""

    lag_days: int
    share: Decimal


@dataclass(frozen=True, slots=True)
class RetailShape:
    """The retail value-function declarations, per product scenario where keyed."""

    noise_pct: Decimal
    units_level: Band
    annual_growth_pct: dict[str, Decimal]
    story_trend_pct_per_day: dict[str, Decimal]
    revenue_per_unit: Band
    cogs_ratio: Band
    views_per_ordered_unit: Band
    views_noise_pct: Decimal
    order_to_ship_ratio: Band
    return_rate: dict[str, Decimal]
    returns_lag_profile: tuple[ReturnsLag, ...]
    first_report_returns_share: Decimal
    unrestated_returns: UnrestatedReturns
    first_report_step_max_pct: Decimal
    served_rpu_band: Band


@dataclass(frozen=True, slots=True)
class Promotion:
    """A traffic lift on named campaigns over a resolved date range."""

    campaigns: tuple[str, ...]
    first: date
    last: date
    lift_pct: Decimal


@dataclass(frozen=True, slots=True)
class BudgetShift:
    """Spend moved from one campaign to another on a resolved day, held after."""

    down: str
    up: str
    on: date
    shift_pct: Decimal


@dataclass(frozen=True, slots=True)
class Pause:
    """A campaign with no delivery over a resolved date range."""

    campaign: str
    first: date
    last: date


@dataclass(frozen=True, slots=True)
class CampaignDay:
    """`coverage_gap`: one campaign on one day."""

    kind: Literal["coverage_gap"]
    campaign: str
    on: date


@dataclass(frozen=True, slots=True)
class OrderedShippedDivergence:
    """Ordered units ramping above shipped units, held after the ramp end."""

    products: tuple[str, ...]
    first: date
    ramp_end: date
    ordered_ratio_end: Decimal


@dataclass(frozen=True, slots=True)
class ReturnRateShift:
    """One product's return rate ramping to a new level, held after the ramp end."""

    product: str
    first: date
    ramp_end: date
    return_rate_end: Decimal


@dataclass(frozen=True, slots=True)
class ReconciliationMismatch:
    """A detail-versus-reported-total units mismatch on one product-day."""

    product: str
    on: date
    units_delta: int


@dataclass(frozen=True, slots=True)
class PriceShift:
    """A held change in one product's net revenue per unit from a resolved day."""

    product: str
    first: date
    pct: int


type ShapeEvent = (
    Promotion
    | BudgetShift
    | Pause
    | CampaignDay
    | OrderedShippedDivergence
    | ReturnRateShift
    | ReconciliationMismatch
    | PriceShift
)


@dataclass(frozen=True, slots=True)
class CatalogShape:
    """The loaded, validated and date-resolved `shape` section."""

    seed: str
    fixture_span: FixtureSpan
    shape_anchor: date
    event_anchor: date
    event_anchor_slack_days: int
    story_days: int
    launch: Launch
    attribution_maturity: AttributionMaturity
    new_to_brand_buyers: NewToBrandBuyersShape
    new_to_brand_buyers_channel: NewToBrandBuyersChannelShape
    new_to_brand_buyers_campaign: NewToBrandBuyersCampaignShape
    dsp_reach: DspReachShape
    deduplicated_attribution: DeduplicatedAttributionShape
    exposure_groups: ExposureGroupsShape
    exposure_frequency: ExposureFrequencyShape
    audience_segments: AudienceSegmentsShape
    segment_profile: SegmentProfileShape
    acquisition_cohorts: AcquisitionCohortsShape
    weekday_factors: tuple[Decimal, ...]
    weekday_amplitude: Band
    month_factors: tuple[Decimal, ...]
    month_amplitude: Band
    min_trend_multiplier: Decimal
    traffic_base: int
    campaign_spread: CampaignSpread
    conversion_shape: dict[str, Band]
    campaign_checks: CampaignChecks
    retail: RetailShape
    events: tuple[ShapeEvent, ...]
    seal_generation: int


@dataclass(frozen=True, slots=True)
class ShapeContext:
    """What the shape is validated against: the visible catalog's entities and scenarios."""

    campaign_scenarios: dict[str, str]
    bulk_campaign_keys: frozenset[str]
    product_scenarios: dict[str, str]
    shaped_campaign_scenarios: frozenset[str]


# --- primitive narrowing -------------------------------------------------------------------


def _fail(path: str, why: str) -> ShapeError:
    return ShapeError(f"shape.{path}: {why}")


def _mapping(value: object, path: str, keys: frozenset[str]) -> RawMapping:
    if not isinstance(value, dict):
        raise _fail(path, "must be a mapping.")
    raw = cast("RawMapping", value)
    missing = sorted(keys - set(raw))
    unknown = sorted(str(key) for key in set(raw) - keys)
    if missing:
        raise _fail(path, f"missing {', '.join(missing)}.")
    if unknown:
        raise _fail(path, f"unknown key {', '.join(unknown)}.")
    return raw


def _list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise _fail(path, "must be a list.")
    return cast("list[object]", value)


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(path, "must be a non-empty string.")
    return value.strip()


def _decimal(value: object, path: str) -> Decimal:
    if not isinstance(value, str):
        raise _fail(path, "must be a quoted decimal string.")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise _fail(path, "must be a decimal.") from error
    if not parsed.is_finite():
        raise _fail(path, "must be finite.")
    return parsed


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(path, "must be an integer.")
    return value


def _positive(value: object, path: str) -> int:
    number = _integer(value, path)
    if number < 1:
        raise _fail(path, "must be a positive integer.")
    return number


def _day(value: object, path: str) -> date:
    if isinstance(value, date):
        raise _fail(path, "must be a quoted ISO date string.")
    try:
        return date.fromisoformat(_text(value, path))
    except ValueError as error:
        raise _fail(path, "must be an ISO date.") from error


def _band(value: object, path: str) -> Band:
    items = _list(value, path)
    if len(items) != 2:  # noqa: PLR2004 -- a band is a pair
        raise _fail(path, "must be a [low, high] pair.")
    low, high = (_decimal(item, f"{path}[{index}]") for index, item in enumerate(items))
    if not low < high:
        raise _fail(path, "low must be below high.")
    return Band(low, high)


def _open_interval(value: Decimal, bounds: tuple[Decimal, Decimal], path: str) -> Decimal:
    low, high = bounds
    if not low < value < high:
        raise _fail(path, f"must lie in ({low}, {high}).")
    return value


def _band_within(band: Band, bounds: tuple[Decimal, Decimal], path: str) -> Band:
    _open_interval(band.low, bounds, f"{path}[0]")
    _open_interval(band.high, bounds, f"{path}[1]")
    return band


def _share(value: Decimal, path: str) -> Decimal:
    if not Decimal(0) < value <= 1:
        raise _fail(path, "must lie in (0, 1].")
    return value


def _offset(value: object, path: str) -> int:
    offset = _integer(value, path)
    low, high = EVENT_OFFSET_RANGE
    if not low <= offset <= high:
        raise _fail(path, f"must be an integer offset in [{low}, {high}].")
    return offset


def _per_scenario(value: object, path: str, scenarios: frozenset[str]) -> dict[str, Decimal]:
    raw = _mapping(value, path, scenarios)
    return {key: _decimal(raw[key], f"{path}.{key}") for key in sorted(scenarios)}


# --- sections ------------------------------------------------------------------------------

_TOP_KEYS = frozenset(
    {
        "seed",
        "fixture_span",
        "shape_anchor",
        "event_anchor",
        "event_anchor_slack_days",
        "story_days",
        "launch",
        "amc",
        "weekday_factors",
        "weekday_amplitude",
        "month_factors",
        "month_amplitude",
        "min_trend_multiplier",
        "traffic_base",
        "campaign_spread",
        "conversion_shape",
        "campaign_checks",
        "retail",
        "events",
        "seal_generation",
    }
)
_SPREAD_KEYS = frozenset(CampaignSpread.__dataclass_fields__)
_RETAIL_KEYS = frozenset(RetailShape.__dataclass_fields__)
_EVENT_KEYS: dict[str, frozenset[str]] = {
    "promotion": frozenset({"kind", "campaigns", "first_offset", "last_offset", "lift_pct"}),
    "budget_shift": frozenset({"kind", "down", "up", "on_offset", "shift_pct"}),
    "pause": frozenset({"kind", "campaign", "first_offset", "last_offset"}),
    "coverage_gap": frozenset({"kind", "campaign", "on_offset"}),
    "ordered_shipped_divergence": frozenset(
        {"kind", "products", "first_offset", "ramp_end_offset", "ordered_ratio_end"}
    ),
    "return_rate_shift": frozenset(
        {"kind", "product", "first_offset", "ramp_end_offset", "return_rate_end"}
    ),
    "reconciliation_mismatch": frozenset({"kind", "product", "on_offset", "units_delta"}),
    "price_shift": frozenset({"kind", "product", "first_offset", "pct"}),
}
_DATE_KEYS = frozenset({"on", "first", "last", "date", "first_day", "last_day", "ramp_end"})


def _campaign(value: object, path: str, context: ShapeContext) -> str:
    key = _text(value, path)
    if key in context.bulk_campaign_keys:
        raise _fail(path, f"{key} is a bulk campaign; events name visible campaigns only.")
    if key not in context.campaign_scenarios:
        raise _fail(path, f"{key} is not a visible catalog campaign.")
    return key


def _product(value: object, path: str, context: ShapeContext) -> str:
    asin = _text(value, path)
    if asin not in context.product_scenarios:
        raise _fail(path, f"{asin} is not a catalog product.")
    return asin


@dataclass(frozen=True, slots=True)
class _EventInput:
    """One raw event entry, its catalog path, the event anchor and the catalog it names."""

    raw: RawMapping
    path: str
    anchor: date
    context: ShapeContext

    def at(self, name: str) -> date:
        """Resolve one offset field to a date, once."""
        return self.anchor + timedelta(days=_offset(self.raw[name], f"{self.path}.{name}"))

    def ordered(self, first: str, last: str) -> None:
        """Refuse a first offset after the last (or ramp-end) offset."""
        if _offset(self.raw[first], self.path) > _offset(self.raw[last], self.path):
            raise _fail(self.path, "first offset must not be after the last offset.")

    def campaign(self, name: str) -> str:
        """Return the named visible campaign."""
        return _campaign(self.raw[name], f"{self.path}.{name}", self.context)

    def product(self, name: str) -> str:
        """Return the named catalog product."""
        return _product(self.raw[name], f"{self.path}.{name}", self.context)

    def decimal(self, name: str) -> Decimal:
        """One declared Decimal field."""
        return _decimal(self.raw[name], f"{self.path}.{name}")

    def campaigns(self) -> tuple[str, ...]:
        """Return the non-empty `campaigns` list."""
        found = tuple(
            _campaign(item, f"{self.path}.campaigns[{index}]", self.context)
            for index, item in enumerate(_list(self.raw["campaigns"], f"{self.path}.campaigns"))
        )
        if not found:
            raise _fail(f"{self.path}.campaigns", "must name at least one campaign.")
        return found

    def products(self) -> tuple[str, ...]:
        """Return the non-empty `products` list."""
        found = tuple(
            _product(item, f"{self.path}.products[{index}]", self.context)
            for index, item in enumerate(_list(self.raw["products"], f"{self.path}.products"))
        )
        if not found:
            raise _fail(f"{self.path}.products", "must name at least one product.")
        return found


def _promotion(event: _EventInput) -> ShapeEvent:
    event.ordered("first_offset", "last_offset")
    return Promotion(
        event.campaigns(),
        event.at("first_offset"),
        event.at("last_offset"),
        event.decimal("lift_pct"),
    )


def _budget_shift(event: _EventInput) -> ShapeEvent:
    down, up = event.campaign("down"), event.campaign("up")
    if down == up:
        raise _fail(event.path, "budget_shift down and up must be different campaigns.")
    return BudgetShift(down, up, event.at("on_offset"), event.decimal("shift_pct"))


def _pause(event: _EventInput) -> ShapeEvent:
    event.ordered("first_offset", "last_offset")
    return Pause(event.campaign("campaign"), event.at("first_offset"), event.at("last_offset"))


def _campaign_day(event: _EventInput) -> ShapeEvent:
    kind = cast("Literal['coverage_gap']", event.raw["kind"])
    return CampaignDay(kind, event.campaign("campaign"), event.at("on_offset"))


def _divergence(event: _EventInput) -> ShapeEvent:
    event.ordered("first_offset", "ramp_end_offset")
    return OrderedShippedDivergence(
        event.products(),
        event.at("first_offset"),
        event.at("ramp_end_offset"),
        event.decimal("ordered_ratio_end"),
    )


def _return_rate_shift(event: _EventInput) -> ShapeEvent:
    event.ordered("first_offset", "ramp_end_offset")
    product = event.product("product")
    if event.context.product_scenarios[product] == "returns_pressure":
        raise _fail(f"{event.path}.product", "must not be the returns-pressure product.")
    end = _open_interval(
        event.decimal("return_rate_end"),
        (Decimal(0), Decimal("0.5")),
        f"{event.path}.return_rate_end",
    )
    return ReturnRateShift(product, event.at("first_offset"), event.at("ramp_end_offset"), end)


def _mismatch(event: _EventInput) -> ShapeEvent:
    return ReconciliationMismatch(
        event.product("product"),
        event.at("on_offset"),
        _integer(event.raw["units_delta"], f"{event.path}.units_delta"),
    )


def _price_shift(event: _EventInput) -> ShapeEvent:
    path = f"{event.path}.pct"
    try:
        pct = int(_text(event.raw["pct"], path))
    except ValueError as error:
        raise _fail(path, "must be a non-zero integer string in [-20, 20].") from error
    if pct == 0 or not -20 <= pct <= 20:  # noqa: PLR2004 -- the declared range
        raise _fail(path, "must be a non-zero integer string in [-20, 20].")
    return PriceShift(event.product("product"), event.at("first_offset"), pct)


_EVENT_PARSERS = {
    "promotion": _promotion,
    "budget_shift": _budget_shift,
    "pause": _pause,
    "coverage_gap": _campaign_day,
    "ordered_shipped_divergence": _divergence,
    "return_rate_shift": _return_rate_shift,
    "reconciliation_mismatch": _mismatch,
    "price_shift": _price_shift,
}


def _event(raw_value: object, path: str, scope: tuple[date, ShapeContext]) -> ShapeEvent:
    if not isinstance(raw_value, dict):
        raise _fail(path, "must be a mapping.")
    probe = cast("RawMapping", raw_value)
    kind = _text(probe.get("kind"), f"{path}.kind")
    if kind not in _EVENT_KEYS:
        raise _fail(f"{path}.kind", f"unknown event kind {kind}.")
    literal = sorted(str(key) for key in set(probe) & _DATE_KEYS)
    if literal:
        raise _fail(path, f"events take offsets only, not a literal date ({', '.join(literal)}).")
    raw = _mapping(probe, path, _EVENT_KEYS[kind])
    anchor, context = scope
    return _EVENT_PARSERS[kind](_EventInput(raw, path, anchor, context))


def _retail(value: object, product_scenarios: frozenset[str]) -> RetailShape:
    raw = _mapping(value, "retail", _RETAIL_KEYS)
    lags: list[ReturnsLag] = []
    for index, item in enumerate(_list(raw["returns_lag_profile"], "retail.returns_lag_profile")):
        path = f"retail.returns_lag_profile[{index}]"
        entry = _mapping(item, path, frozenset({"lag_days", "share"}))
        lag = _positive(entry["lag_days"], f"{path}.lag_days")
        if lag > 28:  # noqa: PLR2004 -- the declared bound
            raise _fail(f"{path}.lag_days", "must be at most 28.")
        lags.append(ReturnsLag(lag, _share(_decimal(entry["share"], path), f"{path}.share")))
    if len({item.lag_days for item in lags}) != len(lags):
        raise _fail("retail.returns_lag_profile", "lags must be distinct.")
    if sum((item.share for item in lags), Decimal(0)) != 1:
        raise _fail("retail.returns_lag_profile", "shares must sum to exactly 1.")
    unrestated = _text(raw["unrestated_returns"], "retail.unrestated_returns")
    if unrestated not in {"first_report", "final"}:
        raise _fail("retail.unrestated_returns", "must be first_report or final.")
    return RetailShape(
        noise_pct=_decimal(raw["noise_pct"], "retail.noise_pct"),
        units_level=_band(raw["units_level"], "retail.units_level"),
        annual_growth_pct=_per_scenario(
            raw["annual_growth_pct"], "retail.annual_growth_pct", product_scenarios
        ),
        story_trend_pct_per_day=_per_scenario(
            raw["story_trend_pct_per_day"], "retail.story_trend_pct_per_day", product_scenarios
        ),
        revenue_per_unit=_band(raw["revenue_per_unit"], "retail.revenue_per_unit"),
        cogs_ratio=_band(raw["cogs_ratio"], "retail.cogs_ratio"),
        views_per_ordered_unit=_band(
            raw["views_per_ordered_unit"], "retail.views_per_ordered_unit"
        ),
        views_noise_pct=_decimal(raw["views_noise_pct"], "retail.views_noise_pct"),
        order_to_ship_ratio=_band(raw["order_to_ship_ratio"], "retail.order_to_ship_ratio"),
        return_rate=_per_scenario(raw["return_rate"], "retail.return_rate", product_scenarios),
        returns_lag_profile=tuple(lags),
        first_report_returns_share=_share(
            _decimal(raw["first_report_returns_share"], "retail.first_report_returns_share"),
            "retail.first_report_returns_share",
        ),
        unrestated_returns=cast("UnrestatedReturns", unrestated),
        first_report_step_max_pct=_decimal(
            raw["first_report_step_max_pct"], "retail.first_report_step_max_pct"
        ),
        served_rpu_band=_band(raw["served_rpu_band"], "retail.served_rpu_band"),
    )


def _spread(value: object) -> CampaignSpread:
    raw = _mapping(value, "campaign_spread", _SPREAD_KEYS)
    bands = {key: _band(raw[key], f"campaign_spread.{key}") for key in sorted(_SPREAD_KEYS)}
    spread = CampaignSpread(**bands)
    if spread.slope_pct.low <= -100:  # noqa: PLR2004 -- the declared floor
        raise _fail("campaign_spread.slope_pct[0]", "must be above -100.")
    if spread.annual_growth_pct.low <= -50:  # noqa: PLR2004 -- the declared floor
        raise _fail("campaign_spread.annual_growth_pct[0]", "must be above -50.")
    for key in ("measurable_share", "viewable_share"):
        band = cast("Band", getattr(spread, key))
        _share(band.low, f"campaign_spread.{key}[0]")
        _share(band.high, f"campaign_spread.{key}[1]")
    if spread.frequency.low < 1:
        raise _fail("campaign_spread.frequency[0]", "must be at least 1.")
    return spread


def _checks(value: object) -> CampaignChecks:
    raw = _mapping(
        value,
        "campaign_checks",
        frozenset({"daily_cvr_max", "window_cvr_band", "window_roas_band"}),
    )
    cvr_max = _share(
        _decimal(raw["daily_cvr_max"], "campaign_checks.daily_cvr_max"),
        "campaign_checks.daily_cvr_max",
    )
    checks = CampaignChecks(
        cvr_max,
        _band(raw["window_cvr_band"], "campaign_checks.window_cvr_band"),
        _band(raw["window_roas_band"], "campaign_checks.window_roas_band"),
    )
    for key in ("window_cvr_band", "window_roas_band"):
        if cast("Band", getattr(checks, key)).low < 0:
            raise _fail(f"campaign_checks.{key}[0]", "must be at least 0.")
    return checks


def _factors(value: object, path: str, count: int) -> tuple[Decimal, ...]:
    items = _list(value, path)
    if len(items) != count:
        raise _fail(path, f"must have exactly {count} entries.")
    return tuple(
        _open_interval(
            _decimal(item, f"{path}[{index}]"), (Decimal("0.5"), Decimal("1.5")), f"{path}[{index}]"
        )
        for index, item in enumerate(items)
    )


def load_catalog_shape(value: object, context: ShapeContext) -> CatalogShape:
    """Narrow, validate and resolve the `shape` section against the visible catalog."""
    raw = _mapping(value, "(root)", _TOP_KEYS)
    span_raw = _mapping(raw["fixture_span"], "fixture_span", frozenset({"first_day", "last_day"}))
    span = FixtureSpan(
        _day(span_raw["first_day"], "fixture_span.first_day"),
        _day(span_raw["last_day"], "fixture_span.last_day"),
    )
    shape_anchor = _day(raw["shape_anchor"], "shape_anchor")
    event_anchor = _day(raw["event_anchor"], "event_anchor")
    slack = _integer(raw["event_anchor_slack_days"], "event_anchor_slack_days")
    if not SLACK_RANGE[0] <= slack <= SLACK_RANGE[1]:
        raise _fail("event_anchor_slack_days", "must be an integer in [0, 2].")
    story_days = _positive(raw["story_days"], "story_days")
    if story_days > STORY_DAYS_MAX:
        raise _fail("story_days", f"must be at most {STORY_DAYS_MAX}.")
    if not (
        span.first_day + timedelta(days=STORY_DAYS_MAX)
        <= shape_anchor
        <= event_anchor
        <= span.last_day
        <= event_anchor + timedelta(days=slack)
    ):
        raise _fail(
            "event_anchor",
            "anchors must satisfy first_day + 56 <= shape_anchor <= event_anchor <= last_day "
            "<= event_anchor + event_anchor_slack_days.",
        )

    launch_raw = _mapping(raw["launch"], "launch", frozenset({"campaign", "first_delivery_offset"}))
    launch = Launch(
        _campaign(launch_raw["campaign"], "launch.campaign", context),
        shape_anchor
        + timedelta(
            days=_offset(launch_raw["first_delivery_offset"], "launch.first_delivery_offset")
        ),
    )

    amc = _mapping(
        raw["amc"],
        "amc",
        frozenset(
            {
                "attribution_maturity",
                "new_to_brand_buyers",
                "new_to_brand_buyers_channel",
                "new_to_brand_buyers_campaign",
                "dsp_reach",
                "deduplicated_attribution",
                "exposure_groups",
                "exposure_frequency",
                "audience_segments",
                "segment_profile",
                "acquisition_cohorts",
            }
        ),
    )
    maturity_raw = _mapping(
        amc["attribution_maturity"], "amc.attribution_maturity", frozenset({"age_0", "age_1"})
    )
    maturity = AttributionMaturity(
        _share(
            _decimal(maturity_raw["age_0"], "amc.attribution_maturity.age_0"),
            "amc.attribution_maturity.age_0",
        ),
        _share(
            _decimal(maturity_raw["age_1"], "amc.attribution_maturity.age_1"),
            "amc.attribution_maturity.age_1",
        ),
    )
    if maturity.age_0 > maturity.age_1:
        raise _fail("amc.attribution_maturity", "age_0 must not exceed age_1.")
    buyers = _new_to_brand_buyers(amc["new_to_brand_buyers"], span, shape_anchor)
    channel = _new_to_brand_buyers_channel(
        amc["new_to_brand_buyers_channel"], (span, shape_anchor), buyers.not_returned_week
    )
    campaign = _new_to_brand_buyers_campaign(
        amc["new_to_brand_buyers_campaign"], (span, shape_anchor), context
    )
    dsp_reach = _dsp_reach(amc["dsp_reach"], (span, shape_anchor), context)
    exposure_groups = _exposure_groups(amc["exposure_groups"], (span, shape_anchor))
    exposure_frequency = _exposure_frequency(amc["exposure_frequency"], (span, shape_anchor))
    audience_segments = _audience_segments(amc["audience_segments"], (span, shape_anchor), context)

    month_amplitude = _band_within(
        _band(raw["month_amplitude"], "month_amplitude"),
        (Decimal(0), Decimal(2)),
        "month_amplitude",
    )
    weekday_amplitude = _band_within(
        _band(raw["weekday_amplitude"], "weekday_amplitude"),
        (Decimal(0), Decimal(2)),
        "weekday_amplitude",
    )
    min_trend = _decimal(raw["min_trend_multiplier"], "min_trend_multiplier")
    if not Decimal(0) < min_trend < 1:
        raise _fail("min_trend_multiplier", "must lie in (0, 1).")
    spread = _spread(raw["campaign_spread"])
    years = Decimal((shape_anchor - span.first_day).days) / Decimal(365)
    if spread.annual_growth_pct.high / 100 * years >= 1 - min_trend:
        raise _fail(
            "campaign_spread.annual_growth_pct[1]",
            "the long-run factor must stay above min_trend_multiplier over the span.",
        )

    conversion_raw = _mapping(
        raw["conversion_shape"], "conversion_shape", context.shaped_campaign_scenarios
    )
    conversion_shape = {
        key: _band_pair_in(conversion_raw[key], f"conversion_shape.{key}")
        for key in sorted(context.shaped_campaign_scenarios)
    }
    checks = _checks(raw["campaign_checks"])
    largest_shape = max(max(band.low, band.high) for band in conversion_shape.values())
    funnel = (
        spread.conversion_rate_pct.high * largest_shape * (1 + spread.volatility_pct.high / 100)
    )
    if funnel >= checks.daily_cvr_max * 100:
        raise _fail(
            "campaign_spread.conversion_rate_pct[1]",
            "the highest conversion rate times the largest shape and noise must stay below "
            "daily_cvr_max.",
        )

    events = tuple(
        _event(item, f"events[{index}]", (event_anchor, context))
        for index, item in enumerate(_list(raw["events"], "events"))
    )
    _check_event_campaigns(events, launch)
    _check_price_shift_product(events)

    seal_generation = _integer(raw["seal_generation"], "seal_generation")
    if seal_generation < 0:
        raise _fail("seal_generation", "must be an integer >= 0.")

    return CatalogShape(
        seed=_text(raw["seed"], "seed"),
        fixture_span=span,
        shape_anchor=shape_anchor,
        event_anchor=event_anchor,
        event_anchor_slack_days=slack,
        story_days=story_days,
        launch=launch,
        attribution_maturity=maturity,
        new_to_brand_buyers=buyers,
        new_to_brand_buyers_channel=channel,
        new_to_brand_buyers_campaign=campaign,
        dsp_reach=dsp_reach,
        deduplicated_attribution=_deduplicated_attribution(amc["deduplicated_attribution"]),
        exposure_groups=exposure_groups,
        exposure_frequency=exposure_frequency,
        audience_segments=audience_segments,
        segment_profile=_segment_profile(
            amc["segment_profile"], (span, shape_anchor), audience_segments
        ),
        acquisition_cohorts=_acquisition_cohorts(amc["acquisition_cohorts"], (span, shape_anchor)),
        weekday_factors=_factors(raw["weekday_factors"], "weekday_factors", 7),
        weekday_amplitude=weekday_amplitude,
        month_factors=_factors(raw["month_factors"], "month_factors", 12),
        month_amplitude=month_amplitude,
        min_trend_multiplier=min_trend,
        traffic_base=_positive(raw["traffic_base"], "traffic_base"),
        campaign_spread=spread,
        conversion_shape=conversion_shape,
        campaign_checks=checks,
        retail=_retail(raw["retail"], frozenset(context.product_scenarios.values())),
        events=events,
        seal_generation=seal_generation,
    )


def _new_to_brand_buyers(
    value: object, span: FixtureSpan, shape_anchor: date
) -> NewToBrandBuyersShape:
    """Narrow `amc.new_to_brand_buyers`: a buyer share in (0, 1] and a week inside the span."""
    path = "amc.new_to_brand_buyers"
    raw = _mapping(value, path, frozenset({"buyers_per_purchase", "not_returned_week_offset"}))
    share = _decimal(raw["buyers_per_purchase"], f"{path}.buyers_per_purchase")
    if not Decimal(0) < share <= 1:
        raise _fail(f"{path}.buyers_per_purchase", "must lie in (0, 1].")
    weeks = _offset(raw["not_returned_week_offset"], f"{path}.not_returned_week_offset")
    anchor_week = shape_anchor - timedelta(days=shape_anchor.weekday())
    week = anchor_week + timedelta(weeks=weeks)
    if week < span.first_day or week + timedelta(days=6) > span.last_day:
        raise _fail(
            f"{path}.not_returned_week_offset", "must name an ISO week wholly inside the span."
        )
    return NewToBrandBuyersShape(share, week)


def _new_to_brand_buyers_channel(
    value: object, anchors: tuple[FixtureSpan, date], platform_missing: date
) -> NewToBrandBuyersChannelShape:
    """Narrow `amc.new_to_brand_buyers_channel`: one missing ad product in a week of the span."""
    path = "amc.new_to_brand_buyers_channel"
    span, shape_anchor = anchors
    raw = _mapping(value, path, frozenset({"missing"}))
    missing = _mapping(raw["missing"], f"{path}.missing", frozenset({"ad_product", "week_offset"}))
    weeks = _offset(missing["week_offset"], f"{path}.missing.week_offset")
    anchor_week = shape_anchor - timedelta(days=shape_anchor.weekday())
    week = anchor_week + timedelta(weeks=weeks)
    if week < span.first_day or week + timedelta(days=6) > span.last_day:
        raise _fail(f"{path}.missing.week_offset", "must name an ISO week wholly inside the span.")
    if week == platform_missing:
        raise _fail(
            f"{path}.missing.week_offset",
            "must differ from pack 1's not-returned week, where every ad product is missing.",
        )
    return NewToBrandBuyersChannelShape(
        week, _text(missing["ad_product"], f"{path}.missing.ad_product")
    )


def _new_to_brand_buyers_campaign(
    value: object, anchors: tuple[FixtureSpan, date], context: ShapeContext
) -> NewToBrandBuyersCampaignShape:
    """Narrow `amc.new_to_brand_buyers_campaign`: two distinct calendar months of the span."""
    path = "amc.new_to_brand_buyers_campaign"
    span, shape_anchor = anchors
    raw = _mapping(value, path, frozenset({"withdrawn", "missing"}))
    withdrawn = _mapping(
        raw["withdrawn"], f"{path}.withdrawn", frozenset({"month_offset", "missing_share"})
    )
    missing = _mapping(raw["missing"], f"{path}.missing", frozenset({"month_offset", "campaign"}))
    share = _decimal(withdrawn["missing_share"], f"{path}.withdrawn.missing_share")
    if not Decimal("0.2") < share < 1:
        raise _fail(f"{path}.withdrawn.missing_share", "must lie in (0.2, 1): more than a fifth.")
    months: list[date] = []
    for name, item in (("withdrawn", withdrawn), ("missing", missing)):
        month = _month_at(
            shape_anchor, _offset(item["month_offset"], f"{path}.{name}.month_offset")
        )
        # A month before the span's first day is outside a shortened horizon (the shortest
        # legal one holds a single complete month) and simply isn't shaped; one past the last
        # day can never be delivered, so it is refused.
        if _month_end(month) > span.last_day:
            raise _fail(f"{path}.{name}.month_offset", "must name a month ending inside the span.")
        months.append(month)
    if months[0] == months[1]:
        raise _fail(f"{path}.missing.month_offset", "must differ from the withdrawn month.")
    return NewToBrandBuyersCampaignShape(
        months[0],
        share,
        months[1],
        _campaign(missing["campaign"], f"{path}.missing.campaign", context),
    )


_EXPOSURE_SOURCE_SETS = ("dsp", "sponsored", "dsp+sponsored")
_IDENTITY_SPACES = ("account", "device_or_browser")
_EXPOSURE_RATES = frozenset(
    {"search", "detail_page_view", "purchaser", "ntb_share", "device_factor"}
)


def _positive_decimal(value: object, path: str) -> Decimal:
    number = _decimal(value, path)
    if number <= 0:
        raise _fail(path, "must be a positive decimal.")
    return number


def _deduplicated_attribution(value: object) -> DeduplicatedAttributionShape:
    """Narrow `amc.deduplicated_attribution`: positive weights, the same channels per model."""
    path = "amc.deduplicated_attribution"
    raw = _mapping(value, path, frozenset({"channel_weights", "whole_credit_models"}))
    models = raw["channel_weights"]
    if not isinstance(models, dict) or not models:
        raise _fail(f"{path}.channel_weights", "must be a non-empty mapping of models.")
    weights: dict[str, dict[str, Decimal]] = {}
    for model, channels in cast("RawMapping", models).items():
        model_path = f"{path}.channel_weights.{model}"
        if not isinstance(channels, dict) or not channels:
            raise _fail(model_path, "must be a non-empty mapping of ad products.")
        weights[_text(model, model_path)] = {
            _text(channel, model_path): _positive_decimal(weight, f"{model_path}.{channel}")
            for channel, weight in cast("RawMapping", channels).items()
        }
    if len({frozenset(channels) for channels in weights.values()}) != 1:
        raise _fail(f"{path}.channel_weights", "every model must weigh the same ad products.")
    whole = frozenset(
        _text(item, f"{path}.whole_credit_models")
        for item in _list(raw["whole_credit_models"], f"{path}.whole_credit_models")
    )
    if not whole <= set(weights):
        raise _fail(f"{path}.whole_credit_models", "must name models with channel weights.")
    return DeduplicatedAttributionShape(weights, whole)


def _exposure_cell(value: object, path: str, period_start: date) -> ExposureCellShape:
    cell = _list(value, path)
    if len(cell) != 2 or cell[0] not in _EXPOSURE_SOURCE_SETS or cell[1] not in _IDENTITY_SPACES:  # noqa: PLR2004 -- a source set and an identity space
        raise _fail(path, f"must be [one of {_EXPOSURE_SOURCE_SETS}, one of {_IDENTITY_SPACES}].")
    return ExposureCellShape(period_start, str(cell[0]), str(cell[1]))


def _anchor_week(anchors: tuple[FixtureSpan, date], offset: int, path: str) -> date:
    span, shape_anchor = anchors
    week = shape_anchor - timedelta(days=shape_anchor.weekday()) + timedelta(weeks=offset)
    if week < span.first_day or week + timedelta(days=6) > span.last_day:
        raise _fail(path, "must name an ISO week wholly inside the span.")
    return week


def _exposure_groups(value: object, anchors: tuple[FixtureSpan, date]) -> ExposureGroupsShape:
    """Narrow `amc.exposure_groups`: shares in (0, 1), and four distinct shaped periods."""
    path = "amc.exposure_groups"
    span, shape_anchor = anchors
    raw = _mapping(
        value,
        path,
        frozenset(
            {
                "impressions_per_shopper",
                "group_shares",
                "account_share",
                "rates",
                "purchases_per_purchaser",
                "order_value",
                "not_returned",
                "withheld_path",
                "click_shoppers",
                "currency_disagreement",
            }
        ),
    )
    groups = _mapping(raw["group_shares"], f"{path}.group_shares", frozenset({"dsp", "sponsored"}))
    rates = _mapping(raw["rates"], f"{path}.rates", _EXPOSURE_RATES)

    def share(item: object, where: str) -> Decimal:
        number = _decimal(item, where)
        if not Decimal(0) < number < 1:
            raise _fail(where, "must lie in (0, 1).")
        return number

    not_returned = _mapping(
        raw["not_returned"], f"{path}.not_returned", frozenset({"week_offset", "cell"})
    )
    withheld = _mapping(
        raw["withheld_path"],
        f"{path}.withheld_path",
        frozenset({"month_offset", "cell", "shoppers"}),
    )
    clicks = _mapping(
        raw["click_shoppers"],
        f"{path}.click_shoppers",
        frozenset({"week_offset", "cell", "shoppers", "click_spend"}),
    )
    disagreement = _mapping(
        raw["currency_disagreement"], f"{path}.currency_disagreement", frozenset({"week_offset"})
    )
    weeks = {
        name: _anchor_week(
            anchors, _offset(item["week_offset"], f"{path}.{name}.week_offset"), f"{path}.{name}"
        )
        for name, item in (
            ("not_returned", not_returned),
            ("click_shoppers", clicks),
            ("currency_disagreement", disagreement),
        )
    }
    if len(set(weeks.values())) != len(weeks):
        raise _fail(path, "the shaped weeks must differ.")
    month = _month_at(shape_anchor, _offset(withheld["month_offset"], f"{path}.withheld_path"))
    if month < span.first_day or _month_end(month) > span.last_day:
        raise _fail(f"{path}.withheld_path.month_offset", "must name a month inside the span.")
    withheld_shoppers = _integer(withheld["shoppers"], f"{path}.withheld_path.shoppers")
    if withheld_shoppers < 2:  # noqa: PLR2004 -- the per-path case holds two or more shoppers
        raise _fail(f"{path}.withheld_path.shoppers", "must be at least 2 (plan D19).")
    click_shoppers = _integer(clicks["shoppers"], f"{path}.click_shoppers.shoppers")
    if click_shoppers < 2:  # noqa: PLR2004 -- a returned cell holds at least the bar
        raise _fail(f"{path}.click_shoppers.shoppers", "must be at least the bar of 2.")
    per_shopper = _integer(raw["impressions_per_shopper"], f"{path}.impressions_per_shopper")
    if per_shopper < 1:
        raise _fail(f"{path}.impressions_per_shopper", "must be at least 1.")
    return ExposureGroupsShape(
        impressions_per_shopper=per_shopper,
        dsp_only_share=share(groups["dsp"], f"{path}.group_shares.dsp"),
        sponsored_only_share=share(groups["sponsored"], f"{path}.group_shares.sponsored"),
        account_share=share(raw["account_share"], f"{path}.account_share"),
        search_rate=share(rates["search"], f"{path}.rates.search"),
        detail_page_view_rate=share(rates["detail_page_view"], f"{path}.rates.detail_page_view"),
        purchaser_rate=share(rates["purchaser"], f"{path}.rates.purchaser"),
        ntb_share=share(rates["ntb_share"], f"{path}.rates.ntb_share"),
        device_rate_factor=share(rates["device_factor"], f"{path}.rates.device_factor"),
        purchases_per_purchaser=_positive_decimal(
            raw["purchases_per_purchaser"], f"{path}.purchases_per_purchaser"
        ),
        order_value=_positive_decimal(raw["order_value"], f"{path}.order_value"),
        not_returned=_exposure_cell(
            not_returned["cell"], f"{path}.not_returned.cell", weeks["not_returned"]
        ),
        withheld_path=_exposure_cell(withheld["cell"], f"{path}.withheld_path.cell", month),
        withheld_shoppers=withheld_shoppers,
        click_cell=_exposure_cell(
            clicks["cell"], f"{path}.click_shoppers.cell", weeks["click_shoppers"]
        ),
        click_shoppers=click_shoppers,
        click_spend=_positive_decimal(clicks["click_spend"], f"{path}.click_shoppers.click_spend"),
        currency_disagreement_week=weeks["currency_disagreement"],
    )


_FINE_BUCKETS = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10_plus")


def _shares(value: object, path: str, *, total: Decimal | None) -> tuple[Decimal, ...]:
    items = _list(value, path)
    if len(items) != len(_FINE_BUCKETS):
        raise _fail(path, f"must list one value per fine bucket, {len(_FINE_BUCKETS)} in all.")
    shares = tuple(_share(_decimal(item, f"{path}[]"), f"{path}[]") for item in items)
    if any(share <= 0 for share in shares) or (total is not None and sum(shares) != total):
        raise _fail(path, "must be positive" + ("" if total is None else f" and sum to {total}."))
    return shares


_ACQUISITION_GROUPS = ("dsp_only", "sponsored_only", "dsp_and_sponsored")


def _group_shares(value: object, path: str) -> tuple[Decimal, ...]:
    """Read one positive share per acquisition group, summing to 1."""
    shares = tuple(_share(_decimal(item, path), path) for item in _list(value, path))
    if len(shares) != len(_ACQUISITION_GROUPS) or sum(shares) != 1 or min(shares) <= 0:
        raise _fail(path, "must list three positive shares, one per group, summing to 1.")
    return shares


_VINTAGE_MONTHS = 13


def _acquisition_cohorts(
    value: object, anchors: tuple[FixtureSpan, date]
) -> AcquisitionCohortsShape:
    """Narrow `amc.acquisition_cohorts`: three ordered vintages inside the span, shaped cells."""
    path = "amc.acquisition_cohorts"
    span, shape_anchor = anchors
    raw = _mapping(
        value,
        path,
        frozenset(
            {
                "cohort_buyers",
                "group_shares",
                "account_share",
                "repeat_rates",
                "first_repeat_share",
                "purchases_per_buyer",
                "order_value",
                "arp_lift",
                "quarter_lift",
                "vintages",
                "absent",
                "withheld",
                "arp_check_gap",
            }
        ),
    )
    vintages = _mapping(
        raw["vintages"], f"{path}.vintages", frozenset({"refused", "degraded", "quarterly"})
    )
    months = {
        name: _month_at(shape_anchor, _offset(vintages[name], f"{path}.vintages.{name}"))
        for name in ("refused", "degraded", "quarterly")
    }
    if not months["refused"] < months["degraded"] < months["quarterly"]:
        raise _fail(f"{path}.vintages", "must be refused, degraded, quarterly, oldest first.")
    for name, month in months.items():
        # A vintage starting before the span's first day is outside a shortened horizon and
        # simply isn't delivered; one ending past the last day never can be, so it is refused.
        if _month_end(month) > span.last_day:
            raise _fail(f"{path}.vintages.{name}", "must be a 13-month vintage inside the span.")

    def cell(key: str, keys: frozenset[str]) -> RawMapping:
        return _mapping(raw[key], f"{path}.{key}", keys)

    def group(item: RawMapping, key: str) -> str:
        found = str(item["group"])
        if found not in _ACQUISITION_GROUPS:
            groups = ", ".join(_ACQUISITION_GROUPS)
            raise _fail(f"{path}.{key}.group", f"must be one of {groups}.")
        return found

    def cohort(item: RawMapping, key: str) -> date:
        where = f"{path}.{key}.cohort_offset"
        return _month_at(shape_anchor, _offset(item["cohort_offset"], where))

    absent = cell("absent", frozenset({"cohort_offset", "group"}))
    withheld = cell("withheld", frozenset({"cohort_offset", "k", "group", "shoppers"}))
    gap = cell("arp_check_gap", frozenset({"cohort_offset", "group"}))
    shoppers = _integer(withheld["shoppers"], f"{path}.withheld.shoppers")
    if shoppers < 2:  # noqa: PLR2004 -- the per-path case holds two or more shoppers
        raise _fail(f"{path}.withheld.shoppers", "must be at least 2 (plan D19).")
    rates = tuple(
        _share(_decimal(item, f"{path}.repeat_rates"), f"{path}.repeat_rates")
        for item in _list(raw["repeat_rates"], f"{path}.repeat_rates")
    )
    if len(rates) != 12:  # noqa: PLR2004 -- one rate per follow-up month
        raise _fail(f"{path}.repeat_rates", "must name 12 follow-up months.")
    return AcquisitionCohortsShape(
        cohort_buyers=_integer(raw["cohort_buyers"], f"{path}.cohort_buyers"),
        group_shares=_group_shares(raw["group_shares"], f"{path}.group_shares"),
        account_share=_share(_decimal(raw["account_share"], f"{path}.account_share"), path),
        repeat_rates=rates,
        first_repeat_share=_share(
            _decimal(raw["first_repeat_share"], f"{path}.first_repeat_share"), path
        ),
        purchases_per_buyer=_decimal(raw["purchases_per_buyer"], f"{path}.purchases_per_buyer"),
        order_value=_decimal(raw["order_value"], f"{path}.order_value"),
        arp_lift=_decimal(raw["arp_lift"], f"{path}.arp_lift"),
        quarter_lift=_decimal(raw["quarter_lift"], f"{path}.quarter_lift"),
        refused_vintage=months["refused"],
        degraded_vintage=months["degraded"],
        quarterly_vintage=months["quarterly"],
        absent_cohort=cohort(absent, "absent"),
        absent_group=group(absent, "absent"),
        withheld_cohort=cohort(withheld, "withheld"),
        withheld_k=_integer(withheld["k"], f"{path}.withheld.k"),
        withheld_group=group(withheld, "withheld"),
        withheld_shoppers=shoppers,
        arp_check_gap_cohort=cohort(gap, "arp_check_gap"),
        arp_check_gap_group=group(gap, "arp_check_gap"),
    )


def _exposure_frequency(value: object, anchors: tuple[FixtureSpan, date]) -> ExposureFrequencyShape:
    """Narrow `amc.exposure_frequency`: shares summing to 1, and two distinct shaped months."""
    path = "amc.exposure_frequency"
    span, shape_anchor = anchors
    raw = _mapping(
        value,
        path,
        frozenset(
            {
                "bucket_shares",
                "ten_plus_impressions",
                "purchaser_rates",
                "account_share",
                "device_factor",
                "ntb_share",
                "purchases_per_purchaser",
                "order_value",
                "withheld_path",
                "null_bucket",
            }
        ),
    )
    withheld = _mapping(
        raw["withheld_path"],
        f"{path}.withheld_path",
        frozenset({"month_offset", "cell", "shoppers", "ad_product"}),
    )
    null = _mapping(raw["null_bucket"], f"{path}.null_bucket", frozenset({"month_offset"}))
    months = {
        name: _month_at(shape_anchor, _offset(item["month_offset"], f"{path}.{name}.month_offset"))
        for name, item in (("withheld_path", withheld), ("null_bucket", null))
    }
    for name, month in months.items():
        # A month before the span's first day is outside a shortened horizon and simply isn't
        # shaped; one past the last day can never be delivered, so it is refused.
        if _month_end(month) > span.last_day:
            raise _fail(f"{path}.{name}.month_offset", "must name a month ending inside the span.")
    if months["null_bucket"] >= months["withheld_path"]:
        raise _fail(f"{path}.null_bucket", "must come before the withheld month.")
    cell = _list(withheld["cell"], f"{path}.withheld_path.cell")
    if len(cell) != 2 or cell[0] not in _FINE_BUCKETS or cell[1] not in _IDENTITY_SPACES:  # noqa: PLR2004 -- a bucket and an identity space
        raise _fail(f"{path}.withheld_path.cell", "must be [a fine bucket, an identity space].")
    shoppers = _integer(withheld["shoppers"], f"{path}.withheld_path.shoppers")
    if shoppers < 2:  # noqa: PLR2004 -- the per-path case holds two or more shoppers
        raise _fail(f"{path}.withheld_path.shoppers", "must be at least 2 (plan D19).")
    ten_plus = _integer(raw["ten_plus_impressions"], f"{path}.ten_plus_impressions")
    if ten_plus < 10:  # noqa: PLR2004 -- the 10_plus bucket starts at 10
        raise _fail(f"{path}.ten_plus_impressions", "must be at least 10.")
    return ExposureFrequencyShape(
        bucket_shares=_shares(raw["bucket_shares"], f"{path}.bucket_shares", total=Decimal(1)),
        ten_plus_impressions=ten_plus,
        purchaser_rates=_shares(raw["purchaser_rates"], f"{path}.purchaser_rates", total=None),
        account_share=_share(_decimal(raw["account_share"], f"{path}.account_share"), path),
        device_rate_factor=_share(_decimal(raw["device_factor"], f"{path}.device_factor"), path),
        ntb_share=_share(_decimal(raw["ntb_share"], f"{path}.ntb_share"), path),
        purchases_per_purchaser=_decimal(
            raw["purchases_per_purchaser"], f"{path}.purchases_per_purchaser"
        ),
        order_value=_decimal(raw["order_value"], f"{path}.order_value"),
        withheld_month=months["withheld_path"],
        withheld_bucket=str(cell[0]),
        withheld_space=str(cell[1]),
        withheld_shoppers=shoppers,
        withheld_ad_product=str(withheld["ad_product"]),
        null_bucket_month=months["null_bucket"],
    )


def _month_at(anchor: date, offset: int) -> date:
    """Return the first day of the calendar month `offset` months from the anchor's month."""
    index = anchor.year * 12 + anchor.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def _month_end(month: date) -> date:
    """Return the last day of the calendar month starting on `month`."""
    return _month_at(month, 1) - timedelta(days=1)


def _audience_segment(value: object, path: str, *, asi: bool) -> AudienceSegment:
    keys = {"id", "name", "description"}
    if asi:
        keys |= {"category_level_1", "category_level_2", "category_path"}
    raw = _mapping(value, path, frozenset(keys))
    segment_id = _text(raw["id"], f"{path}.id")
    if not segment_id.isdigit():
        raise _fail(f"{path}.id", "must be the decimal text of an AMC INTEGER segment ID.")
    return AudienceSegment(
        segment_id=segment_id,
        name=_text(raw["name"], f"{path}.name"),
        description=_text(raw["description"], f"{path}.description"),
        categories=(
            _text(raw["category_level_1"], f"{path}.category_level_1"),
            _text(raw["category_level_2"], f"{path}.category_level_2"),
            _text(raw["category_path"], f"{path}.category_path"),
        )
        if asi
        else None,
    )


def _audience_segments(
    value: object, anchors: tuple[FixtureSpan, date], context: ShapeContext
) -> AudienceSegmentsShape:
    """Narrow `amc.audience_segments`: colliding IDs across namespaces, two shaped months."""
    path = "amc.audience_segments"
    span, shape_anchor = anchors
    raw = _mapping(
        value,
        path,
        frozenset(
            {
                "dsp_segments",
                "asi_segments",
                "segment_shares",
                "impressions_per_shopper",
                "purchaser_rate",
                "purchases_per_purchaser",
                "order_value",
                "name_withheld",
                "roster_gap",
            }
        ),
    )
    dsp = tuple(
        _audience_segment(item, f"{path}.dsp_segments[{index}]", asi=False)
        for index, item in enumerate(_list(raw["dsp_segments"], f"{path}.dsp_segments"))
    )
    asi = tuple(
        _audience_segment(item, f"{path}.asi_segments[{index}]", asi=True)
        for index, item in enumerate(_list(raw["asi_segments"], f"{path}.asi_segments"))
    )
    dsp_ids = [item.segment_id for item in dsp]
    asi_ids = [item.segment_id for item in asi]
    if len(set(dsp_ids)) != len(dsp_ids) or len(set(asi_ids)) != len(asi_ids):
        raise _fail(path, "lists each segment ID once per namespace.")
    if not set(dsp_ids) & set(asi_ids):
        raise _fail(path, "must declare one ID in both namespaces (ADR-103 point 6).")
    if not any('"' in item.name for item in dsp):
        raise _fail(f"{path}.dsp_segments", "must name one segment with an embedded quote.")
    shares = tuple(
        _share(_decimal(item, f"{path}.segment_shares[{index}]"), f"{path}.segment_shares")
        for index, item in enumerate(_list(raw["segment_shares"], f"{path}.segment_shares"))
    )
    if len(shares) != len(dsp):
        raise _fail(f"{path}.segment_shares", "must give one share per DSP segment.")
    withheld = _mapping(
        raw["name_withheld"], f"{path}.name_withheld", frozenset({"month_offset", "segment"})
    )
    gap = _mapping(
        raw["roster_gap"], f"{path}.roster_gap", frozenset({"month_offset", "segment", "campaign"})
    )
    months = {
        name: _month_at(shape_anchor, _offset(item["month_offset"], f"{path}.{name}.month_offset"))
        for name, item in (("name_withheld", withheld), ("roster_gap", gap))
    }
    for name, month in months.items():
        if _month_end(month) > span.last_day:
            raise _fail(f"{path}.{name}.month_offset", "must name a month ending inside the span.")
        segment = _text((withheld if name == "name_withheld" else gap)["segment"], name)
        if segment not in dsp_ids:
            raise _fail(f"{path}.{name}.segment", "must name a declared DSP segment.")
    if months["roster_gap"] <= span.first_day:
        raise _fail(f"{path}.roster_gap", "needs a delivered month before it (its roster).")
    return AudienceSegmentsShape(
        dsp_segments=dsp,
        asi_segments=asi,
        segment_shares=shares,
        impressions_per_shopper=_positive(
            raw["impressions_per_shopper"], f"{path}.impressions_per_shopper"
        ),
        purchaser_rate=_share(_decimal(raw["purchaser_rate"], f"{path}.purchaser_rate"), path),
        purchases_per_purchaser=_decimal(
            raw["purchases_per_purchaser"], f"{path}.purchases_per_purchaser"
        ),
        order_value=_decimal(raw["order_value"], f"{path}.order_value"),
        name_withheld_month=months["name_withheld"],
        name_withheld_segment=str(withheld["segment"]),
        roster_gap_month=months["roster_gap"],
        roster_gap_segment=str(gap["segment"]),
        roster_gap_campaign=_campaign(gap["campaign"], f"{path}.roster_gap.campaign", context),
    )


PROFILE_SOURCES: Final = ("asi_in_market", "asi_lifestyle", "dsp_exposed_only")
"""Packs 10, 11 and 12's `profile_path` literals, in pack order."""


def _profile_source(value: object, path: str, name: str) -> ProfileSource:
    raw = _mapping(value, path, frozenset({"segments", "exposed_shares", "buyer_shares"}))
    segments = tuple(
        _text(item, f"{path}.segments[{index}]")
        for index, item in enumerate(_list(raw["segments"], f"{path}.segments"))
    )
    if (
        not segments
        or len(set(segments)) != len(segments)
        or not all(item.isdigit() for item in segments)
    ):
        raise _fail(f"{path}.segments", "lists unique decimal segment IDs.")
    shares = {
        key: tuple(
            _share(_decimal(item, f"{path}.{key}[{index}]"), f"{path}.{key}")
            for index, item in enumerate(_list(raw[key], f"{path}.{key}"))
        )
        for key in ("exposed_shares", "buyer_shares")
    }
    if any(len(items) != len(segments) for items in shares.values()):
        raise _fail(path, "gives one exposed and one buyer share per segment.")
    return ProfileSource(name, segments, shares["exposed_shares"], shares["buyer_shares"])


def _segment_profile(
    value: object, anchors: tuple[FixtureSpan, date], audience: AudienceSegmentsShape
) -> SegmentProfileShape:
    """Narrow `amc.segment_profile`: three packs' segments and their shaped months."""
    path = "amc.segment_profile"
    span, shape_anchor = anchors
    raw = _mapping(
        value,
        path,
        frozenset(
            {
                "profiled_exposed",
                "buyer_rate",
                "ntb_share",
                "sources",
                "asi_from",
                "total_withheld",
                "segment_withheld",
            }
        ),
    )
    sources_raw = _mapping(raw["sources"], f"{path}.sources", frozenset(PROFILE_SOURCES))
    sources = tuple(
        _profile_source(sources_raw[name], f"{path}.sources.{name}", name)
        for name in PROFILE_SOURCES
    )
    asi_ids = {item.segment_id for item in audience.asi_segments}
    dsp_ids = {item.segment_id for item in audience.dsp_segments}
    paid = {segment for item in sources[:2] for segment in item.segments}
    if not paid & asi_ids or paid <= asi_ids:
        raise _fail(
            f"{path}.sources",
            "the paid sources name a segment pack 13 describes and one it doesn't (partial "
            "enrolment).",
        )
    if not set(sources[2].segments) <= dsp_ids:
        raise _fail(f"{path}.sources.dsp_exposed_only", "names declared DSP segments only.")
    asi_from = _mapping(raw["asi_from"], f"{path}.asi_from", frozenset({"month_offset"}))

    def month(item: RawMapping, where: str) -> date:
        found = _month_at(shape_anchor, _offset(item["month_offset"], f"{where}.month_offset"))
        if _month_end(found) > span.last_day:
            raise _fail(f"{where}.month_offset", "must name a month ending inside the span.")
        return found

    def source(item: RawMapping, where: str) -> ProfileSource:
        name = _text(item["source"], f"{where}.source")
        found = next((entry for entry in sources if entry.source == name), None)
        if found is None:
            raise _fail(f"{where}.source", f"must be one of {', '.join(PROFILE_SOURCES)}.")
        return found

    totals: list[tuple[date, str]] = []
    for index, entry in enumerate(_list(raw["total_withheld"], f"{path}.total_withheld")):
        where = f"{path}.total_withheld[{index}]"
        item = _mapping(entry, where, frozenset({"month_offset", "source"}))
        totals.append((month(item, where), source(item, where).source))
    segments: list[tuple[date, str, str, int]] = []
    for index, entry in enumerate(_list(raw["segment_withheld"], f"{path}.segment_withheld")):
        where = f"{path}.segment_withheld[{index}]"
        item = _mapping(entry, where, frozenset({"month_offset", "source", "segment", "shoppers"}))
        owner = source(item, where)
        if str(item["segment"]) not in owner.segments:
            raise _fail(f"{where}.segment", "must name one of its source's segments.")
        shoppers = _positive(item["shoppers"], f"{where}.shoppers")
        if shoppers < 2:  # noqa: PLR2004 -- plan D19: withheld with two or more shoppers
            raise _fail(f"{where}.shoppers", "must be 2 or more (plan D19).")
        segments.append((month(item, where), owner.source, str(item["segment"]), shoppers))
    withheld = {item[1] for item in totals} | {item[1] for item in segments}
    if withheld != set(PROFILE_SOURCES):
        raise _fail(path, "withholds at least one row of every source (each pack discards one).")
    return SegmentProfileShape(
        profiled_exposed=_positive(raw["profiled_exposed"], f"{path}.profiled_exposed"),
        buyer_rate=_share(_decimal(raw["buyer_rate"], f"{path}.buyer_rate"), path),
        ntb_share=_share(_decimal(raw["ntb_share"], f"{path}.ntb_share"), path),
        sources=sources,
        asi_from=_month_at(shape_anchor, _offset(asi_from["month_offset"], f"{path}.asi_from")),
        totals_withheld=tuple(totals),
        segments_withheld=tuple(segments),
    )


def _dsp_reach(
    value: object, anchors: tuple[FixtureSpan, date], context: ShapeContext
) -> DspReachShape:
    """Narrow `amc.dsp_reach`: distinct days inside the span, one of them suppressed."""
    path = "amc.dsp_reach"
    span, shape_anchor = anchors
    raw = _mapping(value, path, frozenset({"day_offsets", "suppressed"}))
    offsets = tuple(
        _offset(item, f"{path}.day_offsets[{index}]")
        for index, item in enumerate(_list(raw["day_offsets"], f"{path}.day_offsets"))
    )
    if not offsets or len(set(offsets)) != len(offsets):
        raise _fail(f"{path}.day_offsets", "must name at least one day, each once.")
    days = tuple(sorted(shape_anchor + timedelta(days=offset) for offset in offsets))
    if days[0] < span.first_day or days[-1] > span.last_day:
        raise _fail(f"{path}.day_offsets", "every day must lie inside the span.")
    suppressed = _mapping(
        raw["suppressed"], f"{path}.suppressed", frozenset({"day_offset", "campaign"})
    )
    day = shape_anchor + timedelta(
        days=_offset(suppressed["day_offset"], f"{path}.suppressed.day_offset")
    )
    if day not in days:
        raise _fail(f"{path}.suppressed.day_offset", "must be one of day_offsets.")
    if len(days) < 2:  # noqa: PLR2004 -- a refused day needs a clean one beside it
        raise _fail(f"{path}.day_offsets", "must name a clean day beside the suppressed one.")
    campaign = _campaign(suppressed["campaign"], f"{path}.suppressed.campaign", context)
    return DspReachShape(days, day, campaign)


def _band_pair_in(value: object, path: str) -> Band:
    """Narrow a conversion shape [s0, s1]: two Decimals in (0, 2], in either order."""
    items = _list(value, path)
    if len(items) != 2:  # noqa: PLR2004 -- a pair
        raise _fail(path, "must be an [s0, s1] pair.")
    s0, s1 = (_decimal(item, f"{path}[{index}]") for index, item in enumerate(items))
    for index, item in enumerate((s0, s1)):
        if not Decimal(0) < item <= 2:  # noqa: PLR2004 -- the declared bound
            raise _fail(f"{path}[{index}]", "must lie in (0, 2].")
    return Band(s0, s1)


def _check_event_campaigns(events: tuple[ShapeEvent, ...], launch: Launch) -> None:
    named: list[str] = [launch.campaign]
    for event in events:
        if isinstance(event, (Pause, CampaignDay)):
            named.append(event.campaign)
    if len(set(named)) != len(named):
        raise _fail(
            "events",
            "the pause, launch and coverage-gap campaigns must be pairwise distinct.",
        )


def _check_price_shift_product(events: tuple[ShapeEvent, ...]) -> None:
    for event in events:
        if not isinstance(event, PriceShift):
            continue
        others = {
            product for other in events if other is not event for product in _event_products(other)
        }
        if event.product in others:
            raise _fail(
                "events", f"the price_shift product {event.product} is named by another event."
            )


def _event_products(event: ShapeEvent) -> tuple[str, ...]:
    if isinstance(event, OrderedShippedDivergence):
        return event.products
    if isinstance(event, (ReturnRateShift, ReconciliationMismatch, PriceShift)):
        return (event.product,)
    return ()
