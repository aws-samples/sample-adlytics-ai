# SPDX-License-Identifier: MIT-0
"""`ReachAndFunnelArtifact` v1: the Reach and funnel tab's overlap and funnel.

Owned by docs/ui/reach-and-funnel.

Pure: the materializer plans every per-cell figure through the measurement layer and hands this
module one complete period's exposure-group cells (pack 5, `fct_exposure_group_outcome`). Every
share, rate and index is computed here from those cells, inside the one delivery (design D-3):
a figure that needs a `not_returned` cell reads "not available", never a partial sum. Every text
is a server field, taken from the low-level design; the browser formats nothing.

Groups aren't random, so every funnel figure shows association, not cause (ADR-102): the caveat
block sits inside the funnel section and the boundary repeats it. Rates read Amazon account IDs
only. Frequency reads pack 6's platform buckets: the curve per bucket, the platform return beside
the per-channel table, whose every cell reads "Not available per channel" because pack 7 ships
without its credit branch (plan D12).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final, cast

from domain.catalog.capability import WithinDelivery
from domain.presentation.insights import (
    InsightAbsenceClass,
    InsightOutputClass,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    SaturationPolicy,
    insight_slots_payload,
)
from domain.presentation.page_schemas import TemporalPresetId
from domain.semantics.calendar_periods import CalendarPeriod, grain_noun

__all__ = [
    "ACCOUNT",
    "BOUNDARY",
    "BUCKET_SCHEMES",
    "CAVEAT_BLOCK",
    "COARSE_BUCKETS",
    "DEVICE_OR_BROWSER",
    "EXPOSURE_GROUPS",
    "FINE_BUCKETS",
    "FREQUENCY_CAVEAT",
    "FREQUENCY_DECLARED_PERIODS",
    "MIN_ACTIVE_CAMPAIGNS",
    "NEEDS_CAMPAIGNS_TEXT",
    "NOT_AVAILABLE_PER_CHANNEL_REASON",
    "NOT_AVAILABLE_PER_CHANNEL_TEXT",
    "OVERLAP_DECLARED_PERIODS",
    "REACH_ARTIFACT_TYPE",
    "REACH_ARTIFACT_VERSION",
    "REACH_INSIGHT_ROSTER",
    "REACH_PAGE_ID",
    "SCHEME_AXIS_LABELS",
    "SPONSORED_ONLY",
    "ExposureCell",
    "FrequencyBucket",
    "FrequencyCell",
    "FrequencyChannel",
    "FrequencyDelivery",
    "FrequencySection",
    "FrequencySlotRequest",
    "FunnelRow",
    "FunnelSection",
    "GroupReach",
    "OverlapSection",
    "OverlapTile",
    "PerChannelReturn",
    "ReachAbsence",
    "ReachAbsenceReason",
    "ReachAndFunnelArtifact",
    "ReachDelivery",
    "ReachEvidence",
    "ReachMarket",
    "ReachOutcome",
    "ReachSectionState",
    "ReachSections",
    "ReachSlotRequest",
    "ReachValue",
    "ReachValueUnit",
    "SectionsRequest",
    "build_sections",
    "frequency_saturation_slot",
    "full_funnel_association_slot",
    "funnel_rate",
    "purchaser_rate_by_frequency",
    "reach_artifact_payload",
    "reach_overlap_slot",
    "require_reach_artifact_payload",
    "return_by_frequency",
    "saturated_bucket",
    "share_above",
    "within_delivery_value",
]

REACH_ARTIFACT_TYPE: Final = "ReachAndFunnelArtifact"
REACH_ARTIFACT_VERSION: Final = 1
REACH_PAGE_ID: Final = "reach"
ACCOUNT: Final = "account"
DEVICE_OR_BROWSER: Final = "device_or_browser"
EXPOSURE_GROUPS: Final = (
    ("dsp_only", "Saw DSP only"),
    ("sponsored_only", "Saw sponsored ads only"),
    ("dsp_and_sponsored", "Saw both"),
)
"""The canonical exposure groups in display order, with their governed labels (ADR-067)."""
SPONSORED_ONLY: Final = "sponsored_only"
OVERLAP_DECLARED_PERIODS: Final = (
    TemporalPresetId.LAST_COMPLETE_WEEK,
    TemporalPresetId.LAST_COMPLETE_MONTH,
)
FREQUENCY_DECLARED_PERIODS: Final = (
    TemporalPresetId.LAST_COMPLETE_MONTH,
    TemporalPresetId.LAST_COMPLETE_QUARTER,
)
REACH_INSIGHT_ROSTER: Final = (
    InsightSlotId.REACH_OVERLAP,
    InsightSlotId.FULL_FUNNEL_ASSOCIATION,
    InsightSlotId.FREQUENCY_SATURATION,
)
"""The tab's fixed insight roster, in its order (LLD "Insights")."""

OVERLAP_SHARE = WithinDelivery(
    numerator=(("exposure_group", "dsp_and_sponsored"), ("identity_space", ACCOUNT)),
    denominator=(("identity_space", ACCOUNT),),
)
OVERLAP_SHARE_ALL_IDS = WithinDelivery(
    numerator=(("exposure_group", "dsp_and_sponsored"),), denominator=()
)
DSP_ONLY_REACH_SHARE = WithinDelivery(
    numerator=(("exposure_group", "dsp_only"), ("identity_space", ACCOUNT)),
    denominator=(("identity_space", ACCOUNT),),
)
"""The three overlap shares, as the metric dictionary declares them (`within_delivery`)."""

TILE_LABELS: Final = (
    (
        "overlap_share",
        "Saw both within 14 days of their first ad · Amazon account IDs · a lower bound: DSP "
        "impressions Amazon couldn't link to an account aren't matched",
    ),
    (
        "overlap_share_all_ids",
        "Saw both within 14 days of their first ad · all ID types · overlap is understated",
    ),
    ("dsp_only_reach_share", "Saw DSP only within 14 days of their first ad · Amazon account IDs"),
)
ALL_IDS_LABEL: Final = "All ID types"
"""The overlap table column scope; tile labels continue to name only their own metric."""
CAVEAT_BLOCK: Final = (
    "This shows association, not cause. Shoppers were not randomly assigned to groups.",
    "Shoppers can see an ad after they buy, so groups that include DSP retargeting may look "
    "stronger.",
    "Sponsored ads appear when shoppers search, so sponsored groups start closer to buying.",
)
"""The caveat block inside the funnel visual, above the table, never collapsible (ADR-102)."""
FUNNEL_COLUMNS: Final = (
    ("search_rate", "Search rate"),
    ("purchaser_rate", "Purchaser rate"),
    ("revenue_per_exposed_shopper", "Sales per shopper reached"),
    ("purchaser_rate_index_vs_sponsored_only", "Purchaser rate index vs sponsored ads only"),
)
"""The funnel table's columns; the first reads "Search rate", never "branded" (plan D13)."""
INDEX_NOTE: Final = "may understate the difference between groups"
BOUNDARY: Final = (
    "Figures are for complete weeks, months or quarters only.",
    "Rates read Amazon account IDs only.",
    "This shows association, not cause.",
)
NOT_RETURNED_TEXT: Final = "too few shoppers, or none"
NEEDS_DSP_TEXT: Final = "Needs DSP and sponsored ads on one AMC instance"
NO_SHOPPERS_TEXT: Final = "No shoppers were reached in this group."
NO_SALES_TEXT: Final = "No sales were recorded for this group."
CHANGE_UNAVAILABLE_TEXT: Final = "No change is shown, because a figure isn't available."
PRIOR_UNAVAILABLE_TEXT: Final = "The previous {grain} isn't available."
_PERCENT: Final = Decimal(100)
_SHARE_QUANTUM: Final = Decimal("0.0001")
_POINTS_QUANTUM: Final = Decimal("0.01")
_MONEY_QUANTUM: Final = Decimal("0.01")
_MINUS: Final = "\u2212"

FINE_BUCKETS: Final = (
    ("1", "1 ad"),
    ("2", "2 ads"),
    ("3", "3 ads"),
    ("4", "4 ads"),
    ("5", "5 ads"),
    ("6", "6 ads"),
    ("7", "7 ads"),
    ("8", "8 ads"),
    ("9", "9 ads"),
    ("10_plus", "10 or more"),
)
COARSE_BUCKETS: Final = (
    ("1", "1 ad"),
    ("2", "2 ads"),
    ("3", "3 ads"),
    ("4_6", "4 to 6"),
    ("7_9", "7 to 9"),
    ("10_plus", "10 or more"),
)
BUCKET_SCHEMES: Final = {"fine": FINE_BUCKETS, "coarse": COARSE_BUCKETS}
"""Each declared bucket scheme's ordered buckets and display labels (design §6.3)."""
SINGLE_COUNT_BUCKETS: Final = frozenset({"1", "2", "3", "4", "5", "6", "7", "8", "9"})
"""Buckets holding one impression count; a range bucket (4 to 6, 10 or more) holds several."""
SCHEME_AXIS_LABELS: Final = {
    "fine": "Ads seen within 14 days of the first ad: 1 to 9, then 10 or more",
    "coarse": "Ads seen within 14 days of the first ad, in coarse buckets: 1, 2, 3, 4 to 6, "
    "7 to 9, then 10 or more",
}
"""The bucket axis label, which names the scheme the delivery used (LLD "Frequency curve")."""
FREQUENCY_CAVEAT: Final = (
    *CAVEAT_BLOCK,
    "Shoppers can see more ads after they buy, so higher buckets may look stronger.",
)
"""The caveat block inside the frequency visual, with its own line (ADR-102, review DS5-10)."""
MIN_ACTIVE_CAMPAIGNS: Final = 4
"""The optimal-frequency playbook's readiness prerequisite (design §6.3 edge cases)."""
NO_SPEND_TEXT: Final = "No spend was recorded for these shoppers."
CURRENCY_MISMATCH_TEXT: Final = (
    "Spend and sales are in different currencies, so return isn't shown."
)
NEEDS_CAMPAIGNS_TEXT: Final = "Needs at least 4 active campaigns"
PER_CHANNEL_LABEL: Final = (
    "Credited revenue ÷ spend · linear model · 14-day lookback from each shopper's "
    "first ad · includes brand halo · {period}"
)
NOT_AVAILABLE_PER_CHANNEL_TEXT: Final = "Not available per channel"
NOT_AVAILABLE_PER_CHANNEL_REASON: Final = (
    "one purchase can follow ads on several channels, and we can't split its credit fairly "
    "here. See the platform view."
)
PER_CHANNEL_FOOT: Final = (
    "Attributed sales aren't added across channels, because one purchase can be credited to "
    "more than one channel."
)
PLATFORM_RETURN_LABEL: Final = (
    "Platform view · sales ÷ spend on these shoppers in their 14-day window · all "
    "ads · includes brand halo · Amazon account IDs"
)


class ReachOutcome(StrEnum):
    """Whether the period's artifact carries figures (the page entry's declared outcomes)."""

    COMPLETE = "complete"
    EMPTY = "empty"
    """The period isn't measured yet or didn't arrive: page-level text, no sections."""
    INSUFFICIENT = "insufficient"
    """A delivery landed, but no account-ID cell came back, so nothing can be shown."""


class ReachSectionState(StrEnum):
    """How one section reads for the resolved preset."""

    FIGURES = "figures"
    OUTSIDE_DECLARED_PERIODS = "outside_declared_periods"
    PENDING = "pending"
    NEEDS_DSP = "needs_dsp"
    NEEDS_CAMPAIGNS = "needs_campaigns"
    """Frequency only: fewer than 4 campaigns had spend in the period (playbook readiness)."""


class ReachAbsenceReason(StrEnum):
    """Why one figure isn't shown; each carries its server text."""

    NOT_RETURNED = "not_returned"
    """A cell it needs didn't come back: too few shoppers, or none (design §4.5)."""
    NO_SHOPPERS = "no_shoppers"
    NO_SALES = "no_sales"
    """Sales came back without a currency (no purchase in the cell), so none is labelled."""
    PRIOR_UNAVAILABLE = "prior_unavailable"
    CHANGE_UNAVAILABLE = "change_unavailable"
    PACK_VARIANT_MISMATCH = "pack_variant_mismatch"
    """The prior period was measured by another pack variant, so no change is computed (D9)."""
    NO_SPEND = "no_spend"
    """Platform return: no window spend reached the bucket, so there's nothing to divide by."""
    CURRENCY_MISMATCH = "currency_mismatch"
    """Platform return: the bucket's DSP, sponsored or purchase currencies differ (design §5.5)."""
    NOT_AVAILABLE_PER_CHANNEL = "not_available_per_channel"
    """Per-channel return: pack 7 ships without its credit branch (plan D12)."""


@dataclass(frozen=True, slots=True)
class ReachAbsence:
    """A figure that isn't shown, and the text the page renders for it."""

    reason: ReachAbsenceReason
    text: str


class ReachValueUnit(StrEnum):
    """How a value reads: a share or rate, a multiplier, a count, or an amount of money."""

    SHARE = "share"
    MULTIPLIER = "multiplier"
    COUNT = "count"
    CURRENCY = "currency"


@dataclass(frozen=True, slots=True)
class ReachValue:
    """One shown figure: its exact value, unit, currency and the server-built display text."""

    value: Decimal
    unit: ReachValueUnit
    currency_code: str | None = None

    def __post_init__(self) -> None:
        if (self.unit is ReachValueUnit.CURRENCY) != (self.currency_code is not None):
            raise ValueError("an amount carries its currency, and only an amount does.")

    @property
    def display_text(self) -> str:
        """Render the value the way the page shows it; the browser formats nothing."""
        if self.unit is ReachValueUnit.SHARE:
            return f"{(self.value * _PERCENT).quantize(_POINTS_QUANTUM):f}%"
        if self.unit is ReachValueUnit.MULTIPLIER:
            return f"{self.value.quantize(_POINTS_QUANTUM):f}x"
        if self.unit is ReachValueUnit.CURRENCY:
            return f"{self.value.quantize(_MONEY_QUANTUM):f} {self.currency_code}"
        return f"{self.value:f}"


type ReachFigure = ReachValue | ReachAbsence


@dataclass(frozen=True, slots=True)
class ExposureCell:
    """One cell of a pack 5 delivery, as the measurement layer answered it.

    `returned` is False on a `not_returned` cell, whose measures are all None. On a returned cell
    a None sales figure means no purchase reached the cell (its currency came back NULL).
    `outcome_returned` is False when the cell's outcomes came back on rows of their own and those
    didn't (pack 5's touch-only `outcome` branch, account IDs only): its outcomes are absent,
    never 0, while its reach stands.
    """

    exposure_group: str
    identity_space: str
    returned: bool
    shoppers_reached: Decimal | None = None
    search_shoppers: Decimal | None = None
    purchasers: Decimal | None = None
    revenue_incl_halo: Decimal | None = None
    currency_code: str | None = None
    outcome_returned: bool = True

    def __post_init__(self) -> None:
        outcomes = (self.search_shoppers, self.purchasers, self.revenue_incl_halo)
        if not self.returned and any(
            item is not None for item in (self.shoppers_reached, *outcomes)
        ):
            raise ValueError("a not_returned cell carries no measure: it is absent, never 0.")
        if not self.outcome_returned and any(item is not None for item in outcomes):
            raise ValueError("a cell whose outcomes didn't come back carries none.")


@dataclass(frozen=True, slots=True)
class ReachDelivery:
    """One complete period's exposure-group cells, every expected cell present (returned or not).

    `variant` is the pack variant the period was measured by (None for the base pack, plan D9);
    a period is compared only with a prior period of the same variant.
    """

    period: CalendarPeriod
    cells: tuple[ExposureCell, ...]
    variant: str | None = None

    def __post_init__(self) -> None:
        keys = tuple((cell.exposure_group, cell.identity_space) for cell in self.cells)
        expected = {
            (group, space)
            for group, _label in EXPOSURE_GROUPS
            for space in (ACCOUNT, DEVICE_OR_BROWSER)
        }
        if len(set(keys)) != len(keys) or set(keys) != expected:
            raise ValueError("a delivery carries each of the 3 x 2 exposure-group cells once.")

    def cell(self, group: str, space: str) -> ExposureCell:
        """Return one cell of the grid."""
        return next(
            item
            for item in self.cells
            if item.exposure_group == group and item.identity_space == space
        )

    @property
    def has_account_cell(self) -> bool:
        """Whether any account-ID cell came back."""
        return any(cell.returned for cell in self.cells if cell.identity_space == ACCOUNT)


_NOT_RETURNED = ReachAbsence(ReachAbsenceReason.NOT_RETURNED, NOT_RETURNED_TEXT)


def _matching(
    delivery: ReachDelivery, pins: dict[str, str], per: tuple[tuple[str, str], ...]
) -> tuple[ExposureCell, ...]:
    """Every cell a side reads: pinned and `per` values fixed, every other disjoint axis summed."""
    fixed = {**pins, **dict(per)}
    return tuple(
        cell
        for cell in delivery.cells
        if all(
            (cell.exposure_group if axis == "exposure_group" else cell.identity_space) == value
            for axis, value in fixed.items()
        )
    )


def _shoppers(cells: tuple[ExposureCell, ...]) -> Decimal | ReachAbsence:
    """Sum shoppers reached over every value of the summed axes (D-3), refusing any gap."""
    if not cells or any(not cell.returned or cell.shoppers_reached is None for cell in cells):
        return _NOT_RETURNED
    return sum((cell.shoppers_reached or Decimal(0) for cell in cells), Decimal(0))


def within_delivery_value(
    spec: WithinDelivery, delivery: ReachDelivery, per: tuple[tuple[str, str], ...] = ()
) -> ReachFigure:
    """Compute one overlap share inside one delivery: summed shoppers over summed shoppers.

    Every cell a side needs must have come back (D-3), so one `not_returned` cell makes the share
    "not available", never a share of the cells that did.
    """
    numerator = _shoppers(_matching(delivery, spec.pinned("numerator"), per))
    denominator = _shoppers(_matching(delivery, spec.pinned("denominator"), ()))
    if isinstance(numerator, ReachAbsence):
        return numerator
    if isinstance(denominator, ReachAbsence):
        return denominator
    if denominator == 0:
        return ReachAbsence(ReachAbsenceReason.NO_SHOPPERS, NO_SHOPPERS_TEXT)
    return ReachValue((numerator / denominator).quantize(_SHARE_QUANTUM), ReachValueUnit.SHARE)


def funnel_rate(cell: ExposureCell, column: str) -> ReachFigure:
    """Compute one per-cell funnel figure: search rate, purchaser rate or sales per shopper."""
    if not cell.returned or cell.shoppers_reached is None or not cell.outcome_returned:
        return _NOT_RETURNED
    if cell.shoppers_reached == 0:
        return ReachAbsence(ReachAbsenceReason.NO_SHOPPERS, NO_SHOPPERS_TEXT)
    if column == "revenue_per_exposed_shopper":
        if cell.revenue_incl_halo is None or cell.currency_code is None:
            return ReachAbsence(ReachAbsenceReason.NO_SALES, NO_SALES_TEXT)
        return ReachValue(
            (cell.revenue_incl_halo / cell.shoppers_reached).quantize(_SHARE_QUANTUM),
            ReachValueUnit.CURRENCY,
            cell.currency_code,
        )
    numerator = cell.search_shoppers if column == "search_rate" else cell.purchasers
    if numerator is None:
        return _NOT_RETURNED
    return ReachValue(
        (numerator / cell.shoppers_reached).quantize(_SHARE_QUANTUM), ReachValueUnit.SHARE
    )


def _index(group: ReachFigure, reference: ReachFigure) -> ReachFigure:
    """Divide a group's purchaser rate by the sponsored-only group's, in the same delivery."""
    if isinstance(group, ReachAbsence):
        return group
    if isinstance(reference, ReachAbsence):
        return reference
    if reference.value == 0:
        return ReachAbsence(ReachAbsenceReason.NO_SHOPPERS, NO_SHOPPERS_TEXT)
    return ReachValue(
        (group.value / reference.value).quantize(_SHARE_QUANTUM), ReachValueUnit.MULTIPLIER
    )


@dataclass(frozen=True, slots=True)
class ReachChange:
    """The server change block against the prior calendar period (D-8 (a)), or its absence."""

    points: Decimal
    text: str


def _change(  # noqa: PLR0913 — the figure pair, then keyword-only grain and refusal
    current: ReachFigure,
    prior: ReachFigure | None,
    *,
    grain: str,
    refused: bool = False,
) -> ReachChange | ReachAbsence:
    if refused:
        return ReachAbsence(ReachAbsenceReason.PACK_VARIANT_MISMATCH, CHANGE_UNAVAILABLE_TEXT)
    if prior is None:
        return ReachAbsence(
            ReachAbsenceReason.PRIOR_UNAVAILABLE, PRIOR_UNAVAILABLE_TEXT.format(grain=grain)
        )
    if isinstance(current, ReachAbsence) or isinstance(prior, ReachAbsence):
        return ReachAbsence(ReachAbsenceReason.CHANGE_UNAVAILABLE, CHANGE_UNAVAILABLE_TEXT)
    if current.unit is ReachValueUnit.SHARE:
        points = ((current.value - prior.value) * _PERCENT).quantize(_POINTS_QUANTUM)
        sign = "+" if points > 0 else _MINUS if points < 0 else ""
        return ReachChange(points, f"{sign}{abs(points):f} points")
    delta = current.value - prior.value
    sign = "+" if delta > 0 else _MINUS if delta < 0 else ""
    return ReachChange(delta, f"{sign}{abs(delta):f}")


@dataclass(frozen=True, slots=True)
class OverlapTile:
    """One overlap tile: its metric, label, current and prior figures and the change block."""

    metric: str
    label: str
    current: ReachFigure
    prior: ReachFigure | None
    change: ReachChange | ReachAbsence


@dataclass(frozen=True, slots=True)
class GroupReach:
    """One exposure group's shoppers reached, on account IDs and over both identity spaces."""

    exposure_group: str
    label: str
    account: ReachFigure
    all_ids: ReachFigure
    prior_account: ReachFigure | None
    change: ReachChange | ReachAbsence


@dataclass(frozen=True, slots=True)
class OverlapSection:
    """Overlap between DSP and sponsored ads: tiles, the three-part bar's groups, window notes."""

    state: ReachSectionState
    text: str | None
    all_ids_label: str = ALL_IDS_LABEL
    subtitle: str | None = None
    window_notes: tuple[str, ...] = ()
    tiles: tuple[OverlapTile, ...] = ()
    groups: tuple[GroupReach, ...] = ()

    def __post_init__(self) -> None:
        figures = self.state is ReachSectionState.FIGURES
        if self.all_ids_label != ALL_IDS_LABEL:
            raise ValueError('the overlap all-ID column heading is "All ID types".')
        if figures != (self.text is None) or figures != bool(self.tiles):
            raise ValueError("an overlap section carries figures or its text, never both.")


@dataclass(frozen=True, slots=True)
class FunnelRow:
    """One exposure group's funnel figures, on account IDs."""

    exposure_group: str
    label: str
    figures: tuple[tuple[str, ReachFigure], ...]


@dataclass(frozen=True, slots=True)
class FunnelSection:
    """Full funnel by exposure group: the caveat block, then rates per group in a table."""

    state: ReachSectionState
    text: str | None
    caveat: tuple[str, ...] = ()
    columns: tuple[tuple[str, str], ...] = ()
    rows: tuple[FunnelRow, ...] = ()
    index_note: str | None = None

    def __post_init__(self) -> None:
        figures = self.state is ReachSectionState.FIGURES
        if figures != (self.text is None) or figures != bool(self.rows):
            raise ValueError("a funnel section carries figures or its text, never both.")
        if figures and self.caveat != CAVEAT_BLOCK:
            raise ValueError("the funnel's figures always carry the caveat block (ADR-102).")


@dataclass(frozen=True, slots=True)
class FrequencyCell:
    """One bucket of a pack 6 delivery on one identity space, as the measurement layer answered.

    `returned` is False on a `not_returned` bucket, whose measures are all None. On a returned
    bucket a None sales figure means no purchase reached it, and a None spend means no touch of
    that kind did (its currency came back NULL): neither is money to label.
    """

    bucket: str
    identity_space: str
    returned: bool
    shoppers: Decimal | None = None
    purchasers: Decimal | None = None
    revenue_incl_halo: Decimal | None = None
    currency_code: str | None = None
    dsp_window_spend: Decimal | None = None
    dsp_currency: str | None = None
    sponsored_window_spend: Decimal | None = None
    sponsored_currency: str | None = None

    def __post_init__(self) -> None:
        measures = (
            self.shoppers,
            self.purchasers,
            self.revenue_incl_halo,
            self.dsp_window_spend,
            self.sponsored_window_spend,
        )
        if not self.returned and any(item is not None for item in measures):
            raise ValueError("a not_returned bucket carries no measure: it is absent, never 0.")
        for amount, currency in (
            (self.revenue_incl_halo, self.currency_code),
            (self.dsp_window_spend, self.dsp_currency),
            (self.sponsored_window_spend, self.sponsored_currency),
        ):
            if amount is not None and amount != 0 and currency is None:
                raise ValueError("an amount carries its currency.")


@dataclass(frozen=True, slots=True)
class FrequencyDelivery:
    """One complete month's or quarter's platform frequency buckets (pack 6), every cell present.

    `scheme` is the bucket scheme the delivery used (`fine`, or `coarse` once switched on for
    the marketplace, plan D10); the page names it in the axis label.
    """

    period: CalendarPeriod
    scheme: str
    cells: tuple[FrequencyCell, ...]

    def __post_init__(self) -> None:
        buckets = BUCKET_SCHEMES.get(self.scheme)
        if buckets is None:
            raise ValueError(f"{self.scheme!r} isn't a declared bucket scheme.")
        keys = tuple((cell.bucket, cell.identity_space) for cell in self.cells)
        expected = {
            (bucket, space) for bucket, _label in buckets for space in (ACCOUNT, DEVICE_OR_BROWSER)
        }
        if len(set(keys)) != len(keys) or set(keys) != expected:
            raise ValueError("a frequency delivery carries each bucket and identity space once.")

    def cell(self, bucket: str, space: str = ACCOUNT) -> FrequencyCell:
        """Return one bucket on one identity space."""
        return next(
            item for item in self.cells if item.bucket == bucket and item.identity_space == space
        )

    @property
    def buckets(self) -> tuple[tuple[str, str], ...]:
        """The scheme's buckets in order, with their display labels."""
        return BUCKET_SCHEMES[self.scheme]


def purchaser_rate_by_frequency(cell: FrequencyCell) -> ReachFigure:
    """Purchasers over shoppers in one bucket (account IDs), or why it isn't shown."""
    if not cell.returned or cell.shoppers is None or cell.purchasers is None:
        return _NOT_RETURNED
    if cell.shoppers == 0:
        return ReachAbsence(ReachAbsenceReason.NO_SHOPPERS, NO_SHOPPERS_TEXT)
    return ReachValue(
        (cell.purchasers / cell.shoppers).quantize(_SHARE_QUANTUM), ReachValueUnit.SHARE
    )


def return_by_frequency(cell: FrequencyCell) -> ReachFigure:
    """Platform return in one bucket: halo-inclusive sales over both window spends.

    The two spends carry their own currencies (decision B2-5), so they're added only here, and
    only where the bucket's DSP, sponsored and purchase currencies agree (design §5.5): a
    disagreement reads "not available" with its reason, never a sum across currencies.
    """
    if not cell.returned or cell.shoppers is None:
        return _NOT_RETURNED
    priced = (
        (cell.dsp_window_spend, cell.dsp_currency),
        (cell.sponsored_window_spend, cell.sponsored_currency),
    )
    spend = sum((amount for amount, _c in priced if amount is not None), Decimal(0))
    currencies = {currency for amount, currency in priced if amount and currency is not None}
    if spend == 0:
        return ReachAbsence(ReachAbsenceReason.NO_SPEND, NO_SPEND_TEXT)
    if cell.revenue_incl_halo is not None and cell.revenue_incl_halo != 0:
        currencies.add(str(cell.currency_code))
    if len(currencies) != 1:
        return ReachAbsence(ReachAbsenceReason.CURRENCY_MISMATCH, CURRENCY_MISMATCH_TEXT)
    sales = cell.revenue_incl_halo or Decimal(0)
    return ReachValue((sales / spend).quantize(_SHARE_QUANTUM), ReachValueUnit.MULTIPLIER)


@dataclass(frozen=True, slots=True)
class FrequencyBucket:
    """One bucket of the frequency curve and the platform return beside the per-channel table."""

    bucket: str
    label: str
    purchaser_rate: ReachFigure
    platform_return: ReachFigure


@dataclass(frozen=True, slots=True)
class FrequencyChannel:
    """One channel row of the per-channel return table: an ad product pack 7 holds or rosters."""

    ad_product_key: str
    label: str


@dataclass(frozen=True, slots=True)
class PerChannelReturn:
    """Return per channel by frequency, without pack 7's credit branch (plan D12).

    Every cell reads "Not available per channel" with its reason; the platform figure renders
    beside it (`FrequencyBucket.platform_return`). The browser never adds buckets or channels.
    """

    label: str
    unavailable_reason: str
    foot: str
    platform_label: str
    rows: tuple[tuple[FrequencyChannel, tuple[tuple[str, ReachAbsence], ...]], ...]


@dataclass(frozen=True, slots=True)
class FrequencySection:
    """Frequency (packs 6 and 7): the curve, the per-channel table, or the section's own text."""

    state: ReachSectionState
    text: str | None
    expected_after: date | None = None
    caveat: tuple[str, ...] = ()
    bucket_scheme: str | None = None
    axis_label: str | None = None
    buckets: tuple[FrequencyBucket, ...] = ()
    per_channel: PerChannelReturn | None = None

    def __post_init__(self) -> None:
        figures = self.state is ReachSectionState.FIGURES
        if figures != (self.text is None) or figures != bool(self.buckets):
            raise ValueError("a frequency section carries figures or its text, never both.")
        if figures and (self.caveat != FREQUENCY_CAVEAT or self.per_channel is None):
            raise ValueError("frequency figures carry the caveat block and the per-channel table.")
        if (self.state is ReachSectionState.PENDING) != (self.expected_after is not None):
            raise ValueError("a pending frequency section names the date it is expected after.")


@dataclass(frozen=True, slots=True)
class ReachEvidence:
    """What the evidence trace records about the pack's validation status (design §8).

    Machine-readable only: the low-level design places no text for it, so v1 renders none.
    """

    pre_exposure_exclusion: str = "pending_step_b"
    """AC-E1.5: no pre-exposure purchase and no post-window DSP touch counted. Pending Step B."""
    fence_check: str = "structural check, not the exact pack text"
    """The 2026-10-09 Sandbox fence result's label (`validation/RESULTS.md`)."""


def _grain_word(period: CalendarPeriod) -> str:
    return grain_noun(period.grain)


def _section_text(subject: str) -> str:
    return (
        f"{subject} is measured for complete weeks and months. Choose Latest measured month to "
        "see it."
    )


OVERLAP_ABSENT_TEXT: Final = _section_text("Overlap")
FUNNEL_ABSENT_TEXT: Final = _section_text("Full funnel")
FREQUENCY_ABSENT_TEXT: Final = (
    "Frequency is measured for complete months and quarters. Choose Latest measured month to "
    "see it."
)


def _overlap(
    delivery: ReachDelivery, prior: ReachDelivery | None, *, refused: bool = False
) -> OverlapSection:
    """Build the overlap section; `refused` when the prior period is another variant's (D9)."""
    period = delivery.period
    grain = _grain_word(period)
    specs = (OVERLAP_SHARE, OVERLAP_SHARE_ALL_IDS, DSP_ONLY_REACH_SHARE)
    tiles: list[OverlapTile] = []
    for (metric, label), spec in zip(TILE_LABELS, specs, strict=True):
        current = within_delivery_value(spec, delivery)
        before = None if prior is None else within_delivery_value(spec, prior)
        tiles.append(
            OverlapTile(
                metric,
                label,
                current,
                before,
                _change(current, before, grain=grain, refused=refused),
            )
        )
    groups: list[GroupReach] = []
    for group, label in EXPOSURE_GROUPS:
        account = _cell_shoppers(delivery, group, (ACCOUNT,))
        before = None if prior is None else _cell_shoppers(prior, group, (ACCOUNT,))
        groups.append(
            GroupReach(
                group,
                label,
                account,
                _cell_shoppers(delivery, group, (ACCOUNT, DEVICE_OR_BROWSER)),
                before,
                _change(account, before, grain=grain, refused=refused),
            )
        )
    return OverlapSection(
        state=ReachSectionState.FIGURES,
        text=None,
        subtitle=_overlap_subtitle(period.label, grain),
        window_notes=_overlap_window_notes(grain),
        tiles=tuple(tiles),
        groups=tuple(groups),
    )


def _overlap_subtitle(label: str, grain: str) -> str:
    return (
        f"Shoppers reached in {label}, grouped by the ads they saw within 14 days of their "
        f"first ad in that {grain}."
    )


def _overlap_window_notes(grain: str) -> tuple[str, ...]:
    return (
        "These groups use each shopper's 14-day window, so they don't match Amazon's own "
        "period reach reports.",
        f"Ads seen before the {grain} began aren't counted.",
    )


def _cell_shoppers(delivery: ReachDelivery, group: str, spaces: tuple[str, ...]) -> ReachFigure:
    shoppers = _shoppers(tuple(delivery.cell(group, space) for space in spaces))
    if isinstance(shoppers, ReachAbsence):
        return shoppers
    return ReachValue(shoppers, ReachValueUnit.COUNT)


def _funnel(delivery: ReachDelivery) -> FunnelSection:
    reference = funnel_rate(delivery.cell(SPONSORED_ONLY, ACCOUNT), "purchaser_rate")
    rows: list[FunnelRow] = []
    for group, label in EXPOSURE_GROUPS:
        cell = delivery.cell(group, ACCOUNT)
        purchaser = funnel_rate(cell, "purchaser_rate")
        rows.append(
            FunnelRow(
                group,
                label,
                (
                    ("search_rate", funnel_rate(cell, "search_rate")),
                    ("purchaser_rate", purchaser),
                    (
                        "revenue_per_exposed_shopper",
                        funnel_rate(cell, "revenue_per_exposed_shopper"),
                    ),
                    ("purchaser_rate_index_vs_sponsored_only", _index(purchaser, reference)),
                ),
            )
        )
    return FunnelSection(
        state=ReachSectionState.FIGURES,
        text=None,
        caveat=CAVEAT_BLOCK,
        columns=FUNNEL_COLUMNS,
        rows=tuple(rows),
        index_note=INDEX_NOTE,
    )


@dataclass(frozen=True, slots=True)
class SectionsRequest:
    """One resolved preset, its period's delivery (and the prior period's), and the instance.

    `delivery` is None when the period has no pack 5 publication. `dsp_available` is False on an
    instant-access AMC instance, which can't run DSP (docs/sources/amc.md); the materializer reads
    it from the governed declaration `contracts/shared/amc-instance.yaml` and passes it on every
    request. `prior_refused` is True when the prior period can't be compared because it was
    published by two pack variants; a prior delivery of another variant than `delivery`'s is
    refused here too (plan D9).
    """

    preset: TemporalPresetId
    period: CalendarPeriod
    delivery: ReachDelivery | None
    prior: ReachDelivery | None = None
    dsp_available: bool = True
    prior_refused: bool = False
    frequency: FrequencyDelivery | None = None
    """The period's pack 6 delivery, None when frequency hasn't landed for it."""
    channels: tuple[FrequencyChannel, ...] = ()
    """The per-channel table's rows: ad products pack 7 holds for the period or rosters."""
    active_campaigns: int | None = None
    """Campaigns with spend in the period (the campaign roster), read with frequency."""
    frequency_lag_days: int = 16
    """Packs 6 and 7's governed cadence lag, for the pending date (design §4.6)."""


def _pending_prefix(label: str) -> str:
    return f"Figures for {label} are expected after "


def _frequency(request: SectionsRequest) -> FrequencySection:
    """Build frequency for one resolved preset (LLD designed states).

    Outside the month and quarter presets it reads its absence text; before pack 6 lands, the
    date it's expected after; with fewer than 4 active campaigns, the playbook's readiness text.
    """
    if request.preset not in FREQUENCY_DECLARED_PERIODS:
        return FrequencySection(ReachSectionState.OUTSIDE_DECLARED_PERIODS, FREQUENCY_ABSENT_TEXT)
    if request.active_campaigns is not None and request.active_campaigns < MIN_ACTIVE_CAMPAIGNS:
        return FrequencySection(ReachSectionState.NEEDS_CAMPAIGNS, NEEDS_CAMPAIGNS_TEXT)
    delivery = request.frequency
    if delivery is None:
        expected = request.period.end + timedelta(days=request.frequency_lag_days)
        return FrequencySection(
            ReachSectionState.PENDING,
            f"{_pending_prefix(request.period.label)}{expected.day} {expected:%b} {expected.year}.",
            expected_after=expected,
        )
    per_channel = tuple(
        (
            channel,
            tuple(
                (
                    bucket,
                    ReachAbsence(
                        ReachAbsenceReason.NOT_AVAILABLE_PER_CHANNEL,
                        NOT_AVAILABLE_PER_CHANNEL_TEXT,
                    ),
                )
                for bucket, _label in delivery.buckets
            ),
        )
        for channel in request.channels
    )
    return FrequencySection(
        state=ReachSectionState.FIGURES,
        text=None,
        caveat=FREQUENCY_CAVEAT,
        bucket_scheme=delivery.scheme,
        axis_label=SCHEME_AXIS_LABELS[delivery.scheme],
        buckets=tuple(
            FrequencyBucket(
                bucket,
                label,
                purchaser_rate_by_frequency(delivery.cell(bucket)),
                return_by_frequency(delivery.cell(bucket)),
            )
            for bucket, label in delivery.buckets
        ),
        per_channel=PerChannelReturn(
            label=PER_CHANNEL_LABEL.format(period=delivery.period.label),
            unavailable_reason=NOT_AVAILABLE_PER_CHANNEL_REASON,
            foot=PER_CHANNEL_FOOT,
            platform_label=PLATFORM_RETURN_LABEL,
            rows=per_channel,
        ),
    )


@dataclass(frozen=True, slots=True)
class ReachSections:
    """The outcome and the sections one resolved preset shows; none on an insufficient delivery."""

    outcome: ReachOutcome
    overlap: OverlapSection | None
    funnel: FunnelSection | None
    frequency: FrequencySection

    def __post_init__(self) -> None:
        complete = self.outcome is ReachOutcome.COMPLETE
        if complete != (self.overlap is not None and self.funnel is not None):
            raise ValueError("only a complete artifact carries the overlap and funnel sections.")


def build_sections(request: SectionsRequest) -> ReachSections:
    """Build the outcome and the three sections for one resolved preset (LLD designed states)."""
    frequency = _frequency(request)
    if request.preset not in OVERLAP_DECLARED_PERIODS:
        # The quarter preset binds frequency only: overlap and funnel read their texts.
        return ReachSections(
            ReachOutcome.COMPLETE,
            OverlapSection(ReachSectionState.OUTSIDE_DECLARED_PERIODS, OVERLAP_ABSENT_TEXT),
            FunnelSection(ReachSectionState.OUTSIDE_DECLARED_PERIODS, FUNNEL_ABSENT_TEXT),
            frequency,
        )
    if not request.dsp_available:
        return ReachSections(
            ReachOutcome.COMPLETE,
            OverlapSection(ReachSectionState.NEEDS_DSP, NEEDS_DSP_TEXT),
            FunnelSection(ReachSectionState.NEEDS_DSP, NEEDS_DSP_TEXT),
            frequency,
        )
    delivery = request.delivery
    if delivery is None:
        return ReachSections(ReachOutcome.EMPTY, None, None, frequency)
    if not delivery.has_account_cell:
        return ReachSections(ReachOutcome.INSUFFICIENT, None, None, frequency)
    prior = request.prior
    refused = request.prior_refused or (prior is not None and prior.variant != delivery.variant)
    return ReachSections(
        ReachOutcome.COMPLETE,
        _overlap(delivery, None if refused else prior, refused=refused),
        _funnel(delivery),
        frequency,
    )


# --- insights --------------------------------------------------------------------------------

_DETERMINISTIC: Final = "deterministic-v1"
_EVIDENCE_LINE: Final = "Found by a fixed rule from the figures above. Not written by an AI model."


@dataclass(frozen=True, slots=True)
class ReachSlotRequest:
    """One delivery, whether its finding cleared the contract's materiality, and the provenance."""

    delivery: ReachDelivery | None
    material: bool
    artifact_revision: str
    silence_reason: str


def _silent(slot: InsightSlotId, request: ReachSlotRequest) -> InsightSlot:
    return InsightSlot(
        slot,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.SILENT,
        None,
        None,
        request.artifact_revision,
        _DETERMINISTIC,
        absence_class=InsightAbsenceClass.EVALUATED_QUIET,
        absence_reason=request.silence_reason,
    )


def reach_overlap_slot(request: ReachSlotRequest) -> InsightSlot:
    """Render `reach-overlap` v1: DSP-only shoppers on account IDs, named with ID space and window.

    Deterministic, no prompt or model. Silent when a cell it needs is `not_returned` or the share
    isn't material. The sentence never presents the account overlap as exact.
    """
    delivery = request.delivery
    if delivery is None or not request.material:
        return _silent(InsightSlotId.REACH_OVERLAP, request)
    cell = delivery.cell("dsp_only", ACCOUNT)
    share = within_delivery_value(DSP_ONLY_REACH_SHARE, delivery)
    if not cell.returned or cell.shoppers_reached is None or isinstance(share, ReachAbsence):
        return _silent(InsightSlotId.REACH_OVERLAP, request)
    period = delivery.period
    return InsightSlot(
        InsightSlotId.REACH_OVERLAP,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.FILLED,
        f"Among Amazon account IDs reached in {period.label}, DSP reached "
        f"{cell.shoppers_reached:f} shoppers that sponsored ads didn't within 14 days of their "
        f"first ad in that {_grain_word(period)}, {share.display_text} of the total. Overlap on "
        "account IDs is a lower bound.",
        _EVIDENCE_LINE,
        request.artifact_revision,
        _DETERMINISTIC,
    )


def full_funnel_association_slot(request: ReachSlotRequest) -> InsightSlot:
    """Render `full-funnel-association` v1: both groups' purchaser rate against sponsored only.

    Account IDs only; association, never cause; never a credit for search. Silent when either
    rate is unavailable or the difference isn't material.
    """
    delivery = request.delivery
    if delivery is None or not request.material:
        return _silent(InsightSlotId.FULL_FUNNEL_ASSOCIATION, request)
    both = funnel_rate(delivery.cell("dsp_and_sponsored", ACCOUNT), "purchaser_rate")
    sponsored = funnel_rate(delivery.cell(SPONSORED_ONLY, ACCOUNT), "purchaser_rate")
    if isinstance(both, ReachAbsence) or isinstance(sponsored, ReachAbsence):
        return _silent(InsightSlotId.FULL_FUNNEL_ASSOCIATION, request)
    relation = "higher" if both.value > sponsored.value else "lower"
    return InsightSlot(
        InsightSlotId.FULL_FUNNEL_ASSOCIATION,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.FILLED,
        f"Among Amazon account IDs reached in {delivery.period.label}, shoppers who saw both DSP "
        f"and sponsored ads bought at a {relation} rate than shoppers who saw sponsored ads only: "
        f"{both.display_text} against {sponsored.display_text}. This shows association, not "
        "cause.",
        _EVIDENCE_LINE,
        request.artifact_revision,
        _DETERMINISTIC,
    )


def _steps_pass(low: FrequencyCell, high: FrequencyCell, policy: SaturationPolicy) -> bool:
    """Whether the purchaser rate's rise from `low` to `high` is bounded within the tolerance.

    U = (r2 - r1) + z * sqrt(r1(1 - r1)/n1 + r2(1 - r2)/n2), the normal-approximation upper
    95% bound of the rise; the step passes when U <= tolerance * r1 (design §6.3).
    """
    counts = (low.shoppers, low.purchasers, high.shoppers, high.purchasers)
    if not (low.returned and high.returned) or any(item is None for item in counts):
        return False
    n1, p1, n2, p2 = (cast("Decimal", item) for item in counts)
    if min(n1, n2) < policy.shoppers_min or min(p1, p2) < policy.purchasers_min:
        return False
    r1, r2 = p1 / n1, p2 / n2
    bound = (r2 - r1) + policy.z * (r1 * (1 - r1) / n1 + r2 * (1 - r2) / n2).sqrt()
    return bound <= policy.tolerance * r1


def saturated_bucket(delivery: FrequencyDelivery, policy: SaturationPolicy) -> str | None:
    """Name the smallest bucket b whose steps b to b+1 and b+1 to b+2 both pass, or None.

    Account IDs only, returned buckets only. Only single-count buckets are compared (1 to 9 on
    the fine scheme; 1, 2 and 3 on the coarse one), never a range bucket. Silent whenever an
    account bucket came back not available: the share the slot states sums every bucket (D-3).
    """
    cells = tuple(delivery.cell(bucket) for bucket, _label in delivery.buckets)
    if any(not cell.returned for cell in cells):
        return None
    singles = tuple(cell for cell in cells if cell.bucket in SINGLE_COUNT_BUCKETS)
    for index in range(len(singles) - 2):
        first, second, third = singles[index : index + 3]
        if _steps_pass(first, second, policy) and _steps_pass(second, third, policy):
            return first.bucket
    return None


def share_above(delivery: FrequencyDelivery, bucket: str) -> Decimal | None:
    """Account shoppers who saw more ads than `bucket`, as a percent of every bucket's."""
    order = tuple(item for item, _label in delivery.buckets)
    cells = tuple(delivery.cell(item) for item in order)
    if any(not cell.returned or cell.shoppers is None for cell in cells):
        return None
    total = sum((cast("Decimal", cell.shoppers) for cell in cells), Decimal(0))
    if total == 0:
        return None
    above = sum(
        (cast("Decimal", cell.shoppers) for cell in cells[order.index(bucket) + 1 :]), Decimal(0)
    )
    return (above / total * _PERCENT).quantize(_POINTS_QUANTUM)


@dataclass(frozen=True, slots=True)
class FrequencySlotRequest:
    """One frequency delivery, the contract's policy, materiality and the provenance."""

    delivery: FrequencyDelivery | None
    policy: SaturationPolicy
    material: bool
    artifact_revision: str
    silence_reason: str


def frequency_saturation_slot(request: FrequencySlotRequest) -> InsightSlot:
    """Render `frequency-saturation` v1: the bucket after which purchaser rate stops rising.

    Deterministic, no prompt or model. Account IDs only; association, never cause. Silent when
    no bucket qualifies under the contract's two-step rule, or the finding isn't material.
    """
    delivery = request.delivery
    silent = InsightSlot(
        InsightSlotId.FREQUENCY_SATURATION,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.SILENT,
        None,
        None,
        request.artifact_revision,
        _DETERMINISTIC,
        absence_class=InsightAbsenceClass.EVALUATED_QUIET,
        absence_reason=request.silence_reason,
    )
    if delivery is None or not request.material:
        return silent
    bucket = saturated_bucket(delivery, request.policy)
    if bucket is None:
        return silent
    label = dict(delivery.buckets)[bucket]
    tolerance = (request.policy.tolerance * _PERCENT).normalize()
    return InsightSlot(
        InsightSlotId.FREQUENCY_SATURATION,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.FILLED,
        f"Among Amazon account IDs reached in {delivery.period.label}, purchaser rate stopped "
        f"clearly rising after {label} within 14 days of the first ad: each of the next two "
        f"steps rose by at most {tolerance:f}% of the rate, even at the upper bound. This shows "
        "association, not cause; shoppers can see more ads after they buy.",
        _EVIDENCE_LINE,
        request.artifact_revision,
        _DETERMINISTIC,
    )


# --- the artifact --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReachMarket:
    """One marketplace's sections and insights for the resolved period."""

    marketplace_key: str
    sections: ReachSections
    insights: tuple[InsightSlot, ...]

    def __post_init__(self) -> None:
        if tuple(slot.id for slot in self.insights) != REACH_INSIGHT_ROSTER:
            raise ValueError(
                "the Reach insight roster is reach-overlap, full-funnel-association, "
                "frequency-saturation."
            )


@dataclass(frozen=True, slots=True)
class ReachAndFunnelArtifact:
    """`ReachAndFunnelArtifact` v1. Rendering-free: every text is a server field.

    `outcome` is complete when any marketplace has figures, insufficient when deliveries landed
    but none returned an account-ID cell, and empty when no marketplace's period landed.
    """

    preset: TemporalPresetId
    period: CalendarPeriod
    markets: tuple[ReachMarket, ...]
    evidence: ReachEvidence = ReachEvidence()

    def __post_init__(self) -> None:
        keys = tuple(market.marketplace_key for market in self.markets)
        if len(set(keys)) != len(keys):
            raise ValueError("a Reach artifact carries one section set per marketplace.")

    @property
    def outcome(self) -> ReachOutcome:
        """The page entry's declared outcome for this period."""
        outcomes = {market.sections.outcome for market in self.markets}
        if ReachOutcome.COMPLETE in outcomes:
            return ReachOutcome.COMPLETE
        if ReachOutcome.INSUFFICIENT in outcomes:
            return ReachOutcome.INSUFFICIENT
        return ReachOutcome.EMPTY

    @property
    def page_context_label(self) -> str:
        """The assistant chip's label (ADR-120)."""
        return _page_context_label(self.period.label)

    @property
    def starter_questions(self) -> tuple[str, ...]:
        """Starter questions say "associated with", never "drove", "caused" or "led to"."""
        return _starter_questions(self.period.label)


def _page_context_label(label: str) -> str:
    return f"Reach and funnel · {label}"


def _starter_questions(label: str) -> tuple[str, ...]:
    return (
        f"Which exposure group's purchaser rate was highest in {label}, and what is it "
        "associated with?",
    )


def _decimal(value: Decimal) -> str:
    return f"{value:f}"


def _figure(figure: ReachFigure | None) -> dict[str, object] | None:
    if figure is None:
        return None
    if isinstance(figure, ReachAbsence):
        return {"absence": {"reason": figure.reason.value, "text": figure.text}}
    return {
        "value": _decimal(figure.value),
        "unit": figure.unit.value,
        "currency_code": figure.currency_code,
        "display_text": figure.display_text,
    }


def _change_payload(change: ReachChange | ReachAbsence) -> dict[str, object]:
    if isinstance(change, ReachAbsence):
        return {"absence": {"reason": change.reason.value, "text": change.text}}
    return {"value": _decimal(change.points), "text": change.text}


def _overlap_payload(section: OverlapSection | None) -> dict[str, object] | None:
    if section is None:
        return None
    return {
        "state": section.state.value,
        "text": section.text,
        "all_ids_label": section.all_ids_label,
        "subtitle": section.subtitle,
        "window_notes": list(section.window_notes),
        "tiles": [
            {
                "metric": tile.metric,
                "label": tile.label,
                "current": _figure(tile.current),
                "prior": _figure(tile.prior),
                "change": _change_payload(tile.change),
            }
            for tile in section.tiles
        ],
        "groups": [
            {
                "exposure_group": group.exposure_group,
                "label": group.label,
                "account": _figure(group.account),
                "all_ids": _figure(group.all_ids),
                "prior_account": _figure(group.prior_account),
                "change": _change_payload(group.change),
            }
            for group in section.groups
        ],
    }


def _funnel_payload(section: FunnelSection | None) -> dict[str, object] | None:
    if section is None:
        return None
    return {
        "state": section.state.value,
        "text": section.text,
        "caveat": list(section.caveat),
        "columns": [{"metric": metric, "label": label} for metric, label in section.columns],
        "rows": [
            {
                "exposure_group": row.exposure_group,
                "label": row.label,
                "figures": {metric: _figure(figure) for metric, figure in row.figures},
            }
            for row in section.rows
        ],
        "index_note": section.index_note,
    }


def _frequency_payload(section: FrequencySection) -> dict[str, object]:
    per_channel = section.per_channel
    return {
        "state": section.state.value,
        "text": section.text,
        "expected_after": None
        if section.expected_after is None
        else section.expected_after.isoformat(),
        "caveat": list(section.caveat),
        "bucket_scheme": section.bucket_scheme,
        "axis_label": section.axis_label,
        "buckets": [
            {
                "bucket": item.bucket,
                "label": item.label,
                "purchaser_rate": _figure(item.purchaser_rate),
                "platform_return": _figure(item.platform_return),
            }
            for item in section.buckets
        ],
        "per_channel": None
        if per_channel is None
        else {
            "label": per_channel.label,
            "unavailable_reason": per_channel.unavailable_reason,
            "foot": per_channel.foot,
            "platform_label": per_channel.platform_label,
            "rows": [
                {
                    "ad_product_key": channel.ad_product_key,
                    "label": channel.label,
                    "cells": {bucket: _figure(cell) for bucket, cell in cells},
                }
                for channel, cells in per_channel.rows
            ],
        },
    }


def _market_payload(market: ReachMarket) -> dict[str, object]:
    sections = market.sections
    return {
        "marketplace_key": market.marketplace_key,
        "outcome": sections.outcome.value,
        "overlap": _overlap_payload(sections.overlap),
        "funnel": _funnel_payload(sections.funnel),
        "frequency": _frequency_payload(sections.frequency),
        "insights": insight_slots_payload(market.insights),
    }


def _evidence_status(artifact: ReachAndFunnelArtifact) -> dict[str, object]:
    """`EvidenceStatus.v2`: one entry per section not shown in full (ADR-119), server text only.

    Only frequency can be pending (pack 6 lands 16 days after its period, and doesn't anchor
    the month preset); a `not_returned` cell reads "Not available" in place and its section
    still shows in full.
    """
    slots: list[dict[str, object]] = [
        {
            "marketplace_key": market.marketplace_key,
            "slot_label": "Frequency",
            "role_label": "This period",
            "missing_dates": [
                {"start": artifact.period.start.isoformat(), "end": artifact.period.end.isoformat()}
            ],
            "reason_text": market.sections.frequency.text,
            "recovery_text": None,
        }
        for market in artifact.markets
        if market.sections.frequency.state is ReachSectionState.PENDING
    ]
    return {"version": 2, "slots": slots}


def reach_artifact_payload(artifact: ReachAndFunnelArtifact) -> dict[str, object]:
    """Serialize the artifact for the baked body; the UI's strict v1 decoder reads exactly this."""
    return {
        "type": REACH_ARTIFACT_TYPE,
        "version": REACH_ARTIFACT_VERSION,
        "outcome": artifact.outcome.value,
        "preset": artifact.preset.value,
        "period": {
            "start": artifact.period.start.isoformat(),
            "end": artifact.period.end.isoformat(),
            "grain": artifact.period.grain.value,
            "label": artifact.period.label,
        },
        "markets": [_market_payload(market) for market in artifact.markets],
        "boundary": list(BOUNDARY),
        "evidence": {
            "pre_exposure_exclusion": artifact.evidence.pre_exposure_exclusion,
            "fence_check": artifact.evidence.fence_check,
        },
        "evidence_status": _evidence_status(artifact),
        "page_context_label": artifact.page_context_label,
        "page_context_expires_at": None,
        "starter_questions": list(artifact.starter_questions),
    }


# --- the closed served contract ------------------------------------------------------------
#
# `require_reach_artifact_payload` refuses exactly what the browser's strict decoder
# (`frontend/lib/reach.ts`, `parseReachAndFunnelArtifact`) refuses, so the server never issues a
# page record or a capability for a body the page can't decode. Every rule below mirrors one rule
# there. Python's Unicode `\d` and `str.strip` differ from JavaScript's: digits are ASCII-only
# and blank is Python whitespace or U+FEFF, the union both sides refuse (as Audiences does).

_BLANK: Final = re.compile(r"[\s\ufeff]*")
_DECIMAL_TEXT: Final = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_ISO_DATE_TEXT: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ISO_INSTANT_TEXT: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?\+00:00"
)
_DISPLAY_DAY: Final = re.compile(r"([0-9]{1,2}) ([A-Z][a-z]{2}) ([0-9]{4})")
_DISPLAY_MONTHS: Final = {
    name: index
    for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}
_TOTAL_KEY: Final = re.compile(r"(?:^|[_-])(?:all|subtotal|summary|total)(?:[_-]|\Z)", re.I | re.A)
_TOTAL_LABEL: Final = re.compile(r"\b(?:all channels|subtotal|total)\b", re.I | re.A)
_EVIDENCE: Final = ReachEvidence()
_EVIDENCE_STATUS_VERSION: Final = 2
_ARTIFACT_KEYS: Final = frozenset(
    {
        "boundary",
        "evidence",
        "evidence_status",
        "markets",
        "outcome",
        "page_context_expires_at",
        "page_context_label",
        "period",
        "preset",
        "starter_questions",
        "type",
        "version",
    }
)
_PRESETS: Final = (
    TemporalPresetId.LAST_COMPLETE_MONTH.value,
    TemporalPresetId.LAST_COMPLETE_QUARTER.value,
    TemporalPresetId.LAST_COMPLETE_WEEK.value,
)
_OUTCOMES: Final = tuple(item.value for item in ReachOutcome)
_FIGURES: Final = ReachSectionState.FIGURES.value
_OUTSIDE: Final = ReachSectionState.OUTSIDE_DECLARED_PERIODS.value
_PENDING: Final = ReachSectionState.PENDING.value
_PRIOR_UNAVAILABLE: Final = ReachAbsenceReason.PRIOR_UNAVAILABLE.value
_CHANGE_UNAVAILABLE: Final = ReachAbsenceReason.CHANGE_UNAVAILABLE.value
_ABSENCE_TEXTS: Final = {
    ReachAbsenceReason.CHANGE_UNAVAILABLE.value: CHANGE_UNAVAILABLE_TEXT,
    ReachAbsenceReason.CURRENCY_MISMATCH.value: CURRENCY_MISMATCH_TEXT,
    ReachAbsenceReason.NO_SALES.value: NO_SALES_TEXT,
    ReachAbsenceReason.NO_SHOPPERS.value: NO_SHOPPERS_TEXT,
    ReachAbsenceReason.NO_SPEND.value: NO_SPEND_TEXT,
    ReachAbsenceReason.NOT_AVAILABLE_PER_CHANNEL.value: NOT_AVAILABLE_PER_CHANNEL_TEXT,
    ReachAbsenceReason.NOT_RETURNED.value: NOT_RETURNED_TEXT,
    ReachAbsenceReason.PACK_VARIANT_MISMATCH.value: CHANGE_UNAVAILABLE_TEXT,
}
_CHANGE_REASONS: Final = (
    _CHANGE_UNAVAILABLE,
    ReachAbsenceReason.PACK_VARIANT_MISMATCH.value,
    _PRIOR_UNAVAILABLE,
)


@dataclass(frozen=True, slots=True)
class _FigureRule:
    """The absence reasons and value units one figure position allows."""

    reasons: tuple[str, ...]
    units: tuple[str, ...]


_SHARE_RULE: Final = _FigureRule(("no_shoppers", "not_returned"), (ReachValueUnit.SHARE.value,))
_COUNT_RULE: Final = _FigureRule(("not_returned",), (ReachValueUnit.COUNT.value,))
_RETURN_RULE: Final = _FigureRule(
    ("currency_mismatch", "no_spend", "not_returned"), (ReachValueUnit.MULTIPLIER.value,)
)
_PER_CHANNEL_RULE: Final = _FigureRule(("not_available_per_channel",), ())
_FUNNEL_RULES: Final = {
    "search_rate": _SHARE_RULE,
    "purchaser_rate": _SHARE_RULE,
    "revenue_per_exposed_shopper": _FigureRule(
        ("no_sales", "no_shoppers", "not_returned"), (ReachValueUnit.CURRENCY.value,)
    ),
    "purchaser_rate_index_vs_sponsored_only": _FigureRule(
        ("no_shoppers", "not_returned"), (ReachValueUnit.MULTIPLIER.value,)
    ),
}
_SLOT_KEYS: Final = frozenset(
    {"artifact_revision", "evidence_line", "id", "output_class", "prompt_version", "state", "text"}
)
_ABSENT_SLOT_KEYS: Final = _SLOT_KEYS | {"absence_class", "absence_reason"}


def _invalid() -> ValueError:
    return ValueError("the Reach and funnel artifact is invalid.")


def _record(value: object, keys: frozenset[str] | set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(cast("dict[object, object]", value)) != keys:
        raise _invalid()
    return cast("dict[str, object]", value)


def _items(value: object) -> list[object]:
    if not isinstance(value, list):
        raise _invalid()
    return cast("list[object]", value)


def _text(value: object) -> str:
    if not isinstance(value, str) or _BLANK.fullmatch(value):
        raise _invalid()
    return value


def _nullable_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _exact(value: object, expected: str) -> str:
    if _text(value) != expected:
        raise _invalid()
    return expected


def _exact_texts(value: object, expected: tuple[str, ...]) -> None:
    if tuple(_text(item) for item in _items(value)) != expected:
        raise _invalid()


def _enum(value: object, values: tuple[str, ...]) -> str:
    candidate = _text(value)
    if candidate not in values:
        raise _invalid()
    return candidate


def _empty(value: object) -> None:
    if _items(value):
        raise _invalid()


def _decimal_text(value: object) -> None:
    if not isinstance(value, str) or not _DECIMAL_TEXT.fullmatch(value):
        raise _invalid()


def _iso_date(value: object) -> date:
    candidate = _text(value)
    if not _ISO_DATE_TEXT.fullmatch(candidate):
        raise _invalid()
    try:
        return date.fromisoformat(candidate)
    except ValueError:
        raise _invalid() from None


def _nullable_instant(value: object) -> None:
    if value is None:
        return
    candidate = _text(value)
    if not _ISO_INSTANT_TEXT.fullmatch(candidate):
        raise _invalid()
    try:
        datetime.fromisoformat(candidate)
    except ValueError:
        raise _invalid() from None


def _expected_after_in_text(value: object, prefix: str) -> date:
    line = _text(value)
    if not line.startswith(prefix) or not line.endswith("."):
        raise _invalid()
    match = _DISPLAY_DAY.fullmatch(line[len(prefix) : -1])
    month = None if match is None else _DISPLAY_MONTHS.get(match.group(2))
    if match is None or month is None:
        raise _invalid()
    return _iso_date(f"{match.group(3)}-{month:02d}-{int(match.group(1)):02d}")


def _absence(value: object, allowed: tuple[str, ...], grain: str) -> str:
    item = _record(value, {"reason", "text"})
    reason = _enum(item["reason"], allowed)
    expected = (
        PRIOR_UNAVAILABLE_TEXT.format(grain=grain)
        if reason == _PRIOR_UNAVAILABLE
        else _ABSENCE_TEXTS[reason]
    )
    _exact(item["text"], expected)
    return reason


def _carries(value: object, key: str) -> bool:
    """Return whether `value` is an object with `key`, as the browser's `key in value` reads."""
    return isinstance(value, dict) and key in cast("dict[str, object]", value)


def _is_absent(value: object) -> bool:
    return _carries(value, "absence")


def _checked_figure(value: object, rule: _FigureRule, grain: str) -> bool:
    """Validate one value-or-absence figure; return whether it carries a value."""
    if _is_absent(value):
        _absence(_record(value, {"absence"})["absence"], rule.reasons, grain)
        return False
    item = _record(value, {"currency_code", "display_text", "unit", "value"})
    unit = _enum(item["unit"], rule.units)
    currency = _nullable_text(item["currency_code"])
    if (unit == ReachValueUnit.CURRENCY.value) != (currency is not None):
        raise _invalid()
    _text(item["display_text"])
    _decimal_text(item["value"])
    return True


def _checked_nullable_figure(value: object, rule: _FigureRule, grain: str) -> bool | None:
    return None if value is None else _checked_figure(value, rule, grain)


def _change_block(value: object, grain: str) -> str | None:
    """Validate one change block; return its absence reason, None when it carries a value."""
    if _is_absent(value):
        return _absence(_record(value, {"absence"})["absence"], _CHANGE_REASONS, grain)
    item = _record(value, {"text", "value"})
    _text(item["text"])
    _decimal_text(item["value"])
    return None


def _agreeing_change(current: bool, prior: bool | None, reason: str | None) -> None:
    """Require a change to agree with its current and prior figures' availability."""
    if prior is None:
        if reason not in (_PRIOR_UNAVAILABLE, ReachAbsenceReason.PACK_VARIANT_MISMATCH.value):
            raise _invalid()
        return
    available = current and prior
    if available != (reason is None) or (not available and reason != _CHANGE_UNAVAILABLE):
        raise _invalid()


@dataclass(frozen=True, slots=True)
class _Served:
    """The served period and preset every section is checked against."""

    preset: str
    label: str
    grain: str
    start: date
    end: date


_PAIR_STATES: Final = (_FIGURES, ReachSectionState.NEEDS_DSP.value, _OUTSIDE)


def _section_head(item: dict[str, object], served: _Served, absent_text: str) -> str:
    """Validate a section's state and its text; overlap and funnel share these rules."""
    state = _enum(item["state"], _PAIR_STATES)
    quarter = served.preset == TemporalPresetId.LAST_COMPLETE_QUARTER.value
    if quarter != (state == _OUTSIDE):
        raise _invalid()
    if state == _OUTSIDE:
        _exact(item["text"], absent_text)
    elif state == ReachSectionState.NEEDS_DSP.value:
        _exact(item["text"], NEEDS_DSP_TEXT)
    elif item["text"] is not None:
        raise _invalid()
    return state


def _validate_overlap(value: object, served: _Served) -> str:
    item = _record(
        value, {"all_ids_label", "groups", "state", "subtitle", "text", "tiles", "window_notes"}
    )
    _exact(item["all_ids_label"], ALL_IDS_LABEL)
    state = _section_head(item, served, OVERLAP_ABSENT_TEXT)
    if state != _FIGURES:
        if item["subtitle"] is not None:
            raise _invalid()
        for key in ("window_notes", "tiles", "groups"):
            _empty(item[key])
        return state
    _exact(item["subtitle"], _overlap_subtitle(served.label, served.grain))
    _exact_texts(item["window_notes"], _overlap_window_notes(served.grain))
    tiles = _items(item["tiles"])
    if len(tiles) != len(TILE_LABELS):
        raise _invalid()
    for raw, (metric, label) in zip(tiles, TILE_LABELS, strict=True):
        tile = _record(raw, {"change", "current", "label", "metric", "prior"})
        if tile["metric"] != metric or tile["label"] != label:
            raise _invalid()
        current = _checked_figure(tile["current"], _SHARE_RULE, served.grain)
        prior = _checked_nullable_figure(tile["prior"], _SHARE_RULE, served.grain)
        _agreeing_change(current, prior, _change_block(tile["change"], served.grain))
    groups = _items(item["groups"])
    if len(groups) != len(EXPOSURE_GROUPS):
        raise _invalid()
    for raw, (key, label) in zip(groups, EXPOSURE_GROUPS, strict=True):
        group = _record(
            raw, {"account", "all_ids", "change", "exposure_group", "label", "prior_account"}
        )
        if group["exposure_group"] != key or group["label"] != label:
            raise _invalid()
        account = _checked_figure(group["account"], _COUNT_RULE, served.grain)
        _checked_figure(group["all_ids"], _COUNT_RULE, served.grain)
        prior = _checked_nullable_figure(group["prior_account"], _COUNT_RULE, served.grain)
        _agreeing_change(account, prior, _change_block(group["change"], served.grain))
    return state


def _validate_funnel(value: object, served: _Served) -> str:
    item = _record(value, {"caveat", "columns", "index_note", "rows", "state", "text"})
    state = _section_head(item, served, FUNNEL_ABSENT_TEXT)
    if state != _FIGURES:
        for key in ("caveat", "columns", "rows"):
            _empty(item[key])
        if item["index_note"] is not None:
            raise _invalid()
        return state
    _exact_texts(item["caveat"], CAVEAT_BLOCK)
    columns = _items(item["columns"])
    if len(columns) != len(FUNNEL_COLUMNS):
        raise _invalid()
    for raw, (metric, label) in zip(columns, FUNNEL_COLUMNS, strict=True):
        column = _record(raw, {"label", "metric"})
        if column["metric"] != metric or column["label"] != label:
            raise _invalid()
    rows = _items(item["rows"])
    if len(rows) != len(EXPOSURE_GROUPS):
        raise _invalid()
    for raw, (key, label) in zip(rows, EXPOSURE_GROUPS, strict=True):
        row = _record(raw, {"exposure_group", "figures", "label"})
        if row["exposure_group"] != key or row["label"] != label:
            raise _invalid()
        figures = _record(row["figures"], set(_FUNNEL_RULES))
        for metric, rule in _FUNNEL_RULES.items():
            _checked_figure(figures[metric], rule, served.grain)
    _exact(item["index_note"], INDEX_NOTE)
    return state


def _validate_per_channel(value: object, buckets: tuple[str, ...], served: _Served) -> None:
    item = _record(value, {"foot", "label", "platform_label", "rows", "unavailable_reason"})
    keys: set[str] = set()
    for raw in _items(item["rows"]):
        row = _record(raw, {"ad_product_key", "cells", "label"})
        key = _text(row["ad_product_key"])
        if _TOTAL_KEY.search(key) or _TOTAL_LABEL.search(_text(row["label"])):
            raise _invalid()
        cells = _record(row["cells"], set(buckets))
        for bucket in buckets:
            _checked_figure(cells[bucket], _PER_CHANNEL_RULE, served.grain)
        if key in keys:
            raise _invalid()
        keys.add(key)
    _exact(item["foot"], PER_CHANNEL_FOOT)
    _exact(item["label"], PER_CHANNEL_LABEL.format(period=served.label))
    _text(item["platform_label"])
    _exact(item["unavailable_reason"], NOT_AVAILABLE_PER_CHANNEL_REASON)


_FREQUENCY_STATES: Final = (
    _FIGURES,
    ReachSectionState.NEEDS_CAMPAIGNS.value,
    _OUTSIDE,
    _PENDING,
)


def _validate_frequency(value: object, served: _Served) -> tuple[str, str | None]:
    """Validate the frequency section; return its state and its text."""
    item = _record(
        value,
        {
            "axis_label",
            "bucket_scheme",
            "buckets",
            "caveat",
            "expected_after",
            "per_channel",
            "state",
            "text",
        },
    )
    state = _enum(item["state"], _FREQUENCY_STATES)
    expected_after = None if item["expected_after"] is None else _iso_date(item["expected_after"])
    if (state == _PENDING) != (expected_after is not None):
        raise _invalid()
    if (served.preset == TemporalPresetId.LAST_COMPLETE_WEEK.value) != (state == _OUTSIDE):
        raise _invalid()
    if state == _OUTSIDE:
        _exact(item["text"], FREQUENCY_ABSENT_TEXT)
    elif state == ReachSectionState.NEEDS_CAMPAIGNS.value:
        _exact(item["text"], NEEDS_CAMPAIGNS_TEXT)
    elif state == _PENDING:
        if _expected_after_in_text(item["text"], _pending_prefix(served.label)) != expected_after:
            raise _invalid()
    elif item["text"] is not None:
        raise _invalid()
    if state != _FIGURES:
        if any(item[key] is not None for key in ("bucket_scheme", "axis_label", "per_channel")):
            raise _invalid()
        _empty(item["caveat"])
        _empty(item["buckets"])
        return state, cast("str", item["text"])
    scheme = _enum(item["bucket_scheme"], tuple(sorted(BUCKET_SCHEMES)))
    roster = BUCKET_SCHEMES[scheme]
    _exact_texts(item["caveat"], FREQUENCY_CAVEAT)
    buckets = _items(item["buckets"])
    if len(buckets) != len(roster):
        raise _invalid()
    for raw, (key, label) in zip(buckets, roster, strict=True):
        bucket = _record(raw, {"bucket", "label", "platform_return", "purchaser_rate"})
        if bucket["bucket"] != key or bucket["label"] != label:
            raise _invalid()
        _checked_figure(bucket["platform_return"], _RETURN_RULE, served.grain)
        _checked_figure(bucket["purchaser_rate"], _SHARE_RULE, served.grain)
    _exact(item["axis_label"], SCHEME_AXIS_LABELS[scheme])
    _validate_per_channel(item["per_channel"], tuple(key for key, _label in roster), served)
    return state, None


def _validate_insight(value: object, expected: InsightSlotId) -> tuple[str, str]:
    """Validate one insight slot; return its state and its artifact revision."""
    absent = _carries(value, "absence_class")
    item = _record(value, _ABSENT_SLOT_KEYS if absent else _SLOT_KEYS)
    if item["id"] != expected.value or item["output_class"] != "perspective":
        raise _invalid()
    state = _enum(item["state"], tuple(entry.value for entry in InsightSlotState))
    filled = state == InsightSlotState.FILLED.value
    if filled == absent:
        raise _invalid()
    text = _nullable_text(item["text"])
    evidence_line = _nullable_text(item["evidence_line"])
    if filled != (text is not None) or filled != (evidence_line is not None):
        raise _invalid()
    if absent:
        _enum(item["absence_class"], tuple(entry.value for entry in InsightAbsenceClass))
        _text(item["absence_reason"])
    _text(item["prompt_version"])
    return state, _text(item["artifact_revision"])


def _validate_market(value: object, served: _Served) -> tuple[str, str, str, str | None]:
    """Validate one market; return its outcome, key, frequency state and frequency text."""
    item = _record(
        value, {"frequency", "funnel", "insights", "marketplace_key", "outcome", "overlap"}
    )
    outcome = _enum(item["outcome"], _OUTCOMES)
    if (outcome == ReachOutcome.COMPLETE.value) != (
        item["overlap"] is not None and item["funnel"] is not None
    ):
        raise _invalid()
    overlap = None if item["overlap"] is None else _validate_overlap(item["overlap"], served)
    funnel = None if item["funnel"] is None else _validate_funnel(item["funnel"], served)
    if overlap is not None and funnel is not None and overlap != funnel:
        raise _invalid()
    frequency, frequency_text = _validate_frequency(item["frequency"], served)
    insights = _items(item["insights"])
    if len(insights) != len(REACH_INSIGHT_ROSTER):
        raise _invalid()
    slots = [
        _validate_insight(raw, slot_id)
        for raw, slot_id in zip(insights, REACH_INSIGHT_ROSTER, strict=True)
    ]
    if len({revision for _state, revision in slots}) != 1:
        raise _invalid()
    # A slot is filled only beside its section's figures.
    for (state, _revision), section in zip(slots, (overlap, funnel, frequency), strict=True):
        if section != _FIGURES and state == InsightSlotState.FILLED.value:
            raise _invalid()
    return outcome, _text(item["marketplace_key"]), frequency, frequency_text


def _validate_evidence_status(value: object, served: _Served, pending: dict[str, str]) -> None:
    item = _record(value, {"slots", "version"})
    if isinstance(item["version"], bool) or item["version"] != _EVIDENCE_STATUS_VERSION:
        raise _invalid()
    keys: set[str] = set()
    for raw in _items(item["slots"]):
        slot = _record(
            raw,
            {
                "marketplace_key",
                "missing_dates",
                "reason_text",
                "recovery_text",
                "role_label",
                "slot_label",
            },
        )
        marketplace = _text(slot["marketplace_key"])
        if marketplace not in pending or marketplace in keys:
            raise _invalid()
        keys.add(marketplace)
        dates = _items(slot["missing_dates"])
        for raw_window in dates:
            window = _record(raw_window, {"end", "start"})
            if (_iso_date(window["start"]), _iso_date(window["end"])) != (served.start, served.end):
                raise _invalid()
        if (
            len(dates) != 1
            or slot["reason_text"] != pending[marketplace]
            or slot["recovery_text"] is not None
        ):
            raise _invalid()
        _exact(slot["role_label"], "This period")
        _exact(slot["slot_label"], "Frequency")
    if len(keys) != len(pending):
        raise _invalid()


def require_reach_artifact_payload(value: object) -> dict[str, object]:
    """Refuse any Reach body the browser's strict v1 decoder would refuse; return it unchanged.

    Closed keys, the declared outcome, exactly one market agreeing with it, every section's
    state, text and roster, every insight slot, the evidence and boundary copy, and the
    period-derived context fields (`frontend/lib/reach.ts`). Raises ValueError.
    """
    item = _record(value, _ARTIFACT_KEYS)
    # `True == 1` in Python; the browser's `!== 1` refuses a boolean, so the server does too.
    version = item["version"]
    if (
        item["type"] != REACH_ARTIFACT_TYPE
        or isinstance(version, bool)
        or version != REACH_ARTIFACT_VERSION
    ):
        raise _invalid()
    preset = _enum(item["preset"], _PRESETS)
    period = _record(item["period"], {"end", "grain", "label", "start"})
    grain = _enum(period["grain"], ("month", "quarter", "week"))
    served = _Served(
        preset, _text(period["label"]), grain, _iso_date(period["start"]), _iso_date(period["end"])
    )
    if served.end < served.start or preset != f"last_complete_{grain}":
        raise _invalid()
    outcome = _enum(item["outcome"], _OUTCOMES)
    markets = [_validate_market(raw, served) for raw in _items(item["markets"])]
    # marketplace_key has no customer label in v1, so exactly one market is presentable.
    if len(markets) != 1 or markets[0][0] != outcome:
        raise _invalid()
    pending = {
        key: cast("str", text) for _outcome, key, state, text in markets if state == _PENDING
    }
    evidence = _record(item["evidence"], {"fence_check", "pre_exposure_exclusion"})
    _exact(evidence["fence_check"], _EVIDENCE.fence_check)
    _exact(evidence["pre_exposure_exclusion"], _EVIDENCE.pre_exposure_exclusion)
    _exact(item["page_context_label"], _page_context_label(served.label))
    _nullable_instant(item["page_context_expires_at"])
    _exact_texts(item["starter_questions"], _starter_questions(served.label))
    _exact_texts(item["boundary"], BOUNDARY)
    _validate_evidence_status(item["evidence_status"], served, pending)
    return item
