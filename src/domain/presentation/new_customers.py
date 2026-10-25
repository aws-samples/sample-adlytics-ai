# SPDX-License-Identifier: MIT-0
"""The New customers tab artifact, `NewCustomersArtifact` v2 (docs/ui/new-customers).

Every field and text follows the package's low-level design. The types keep three rules by
construction:

- No figure spans channels. A step-1 row and a gateway entry each belong to one ad product of the
  Amazon Ads network in one marketplace, and nothing here holds a total or subtotal row: one
  purchase can be credited to more than one channel (ADR-106).
- Every nullable figure is a value or a typed absence `{reason, text}`, and the browser never
  computes a value, share, change or total. A share of channel is divided here, on the server,
  from two planned answers.
- New buyers exist only for complete weeks and months (ADR-100), so a rolling preset carries the
  section's absence text and its action, never a tile.
- v2 (manifest revision 2026-10-09.2, plan D5) adds new buyers per channel under the tiles: one row
  per ad product on the period's ad-product roster, each figure a value or a typed absence, and no
  total row: one shopper can buy after ads on more than one channel. v1 never shipped, so it is
  replaced, not kept beside v2.
- UI slice 9 (plan D3, D4) aligns v2 to the shared contracts before it is ever baked: the share
  change is ADR-121's `change` block, toned by the dictionary's `favourable_direction`, and
  `evidence_status` is ADR-119's one coverage block. With two marketplaces each market names
  itself (`marketplace_text`), as the Scorecard's rows do.

The page's rendering, route and page entries belong to the UI workstream; this module is the
server contract it renders.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final, cast

from domain.capabilities.paid_features import (
    PaidFeatureState,
    PeriodState,
    ResolvePeriodStateRequest,
    resolve_period_state,
)
from domain.measurement.pack_coverage import (
    PACK_COVERAGE_RECOVERY,
    PackCoverageVerdict,
    pack_coverage_text,
)
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.calendar_presets import (
    AbsenceSubject,
    DailyOperandCoverage,
    PresetSection,
    ResolveCalendarPresetRequest,
    ResolveCalendarPresetResult,
    declared_period_absence_text,
    resolve_calendar_preset,
)
from domain.presentation.change import (
    Change,
    ChangeOperands,
    ChangeUnit,
    ComparedMetric,
    build_change,
    change_payload,
)
from domain.presentation.currency_display import metric_display_value
from domain.presentation.evidence_status import (
    CoverageFact,
    CoverageKind,
    evidence_status_payload,
    write_evidence_status,
)
from domain.presentation.insights import (
    DeterministicFinding,
    InsightAbsenceClass,
    InsightOutputClass,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    insight_slots_payload,
)
from domain.presentation.page_schemas import (
    TemporalPresetId,
    calendar_preset_grain,
    is_calendar_preset,
)
from domain.presentation.scorecard import day_text, range_text
from domain.presentation.starter_questions import window_label as control_window_label
from domain.presentation.temporal import temporal_preset_label_v2
from domain.semantics.calendar_periods import CalendarPeriod, day_label
from domain.semantics.time import TimeGrain
from domain.shared_kernel.values import DateRange

__all__ = [
    "GATEWAY_CAPTIONS",
    "GATEWAY_COLUMN_HEADER",
    "GATEWAY_LIMIT",
    "NEW_BUYERS_ACTION_LABEL",
    "NEW_BUYERS_DECLARED_PERIODS",
    "NEW_CUSTOMERS_ARTIFACT_TYPE",
    "NEW_CUSTOMERS_ARTIFACT_VERSION",
    "NEW_CUSTOMERS_BOUNDARY",
    "NEW_CUSTOMERS_COMPARED_METRICS",
    "NEW_CUSTOMERS_COVERAGE_METRICS",
    "NEW_CUSTOMERS_DAILY_OPERAND",
    "NEW_CUSTOMERS_EMPTY_TEXT",
    "NEW_CUSTOMERS_INSUFFICIENT_TEXT",
    "NEW_CUSTOMERS_PAGE_ID",
    "NEW_CUSTOMERS_PAGE_TEXT",
    "NEW_CUSTOMERS_POINTERS",
    "NOT_RETURNED_TEXT",
    "NO_CALENDAR_COMPARISON_TEXT",
    "PER_CHANNEL_CAPTION",
    "PER_CHANNEL_FOOT",
    "STEP_ONE_FOOT",
    "GatewayEntry",
    "GatewayRow",
    "GatewayState",
    "NewBuyers",
    "NewBuyersAction",
    "NewBuyersAwaitingRequest",
    "NewBuyersFiguresRequest",
    "NewBuyersState",
    "NewCustomersAbsence",
    "NewCustomersAbsenceReason",
    "NewCustomersArtifact",
    "NewCustomersFigure",
    "NewCustomersLabels",
    "NewCustomersMarket",
    "NewCustomersOutcome",
    "NewCustomersPointer",
    "NtbShareMover",
    "PerChannel",
    "PerChannelAwaitingRequest",
    "PerChannelFiguresRequest",
    "PerChannelInput",
    "PerChannelRow",
    "PerChannelState",
    "PortfolioAcquisitionCard",
    "PortfolioAcquisitionSlotRequest",
    "ShareChange",
    "StepOneInput",
    "StepOneRow",
    "gateway_entry",
    "gateway_not_covered",
    "new_buyers_awaiting",
    "new_buyers_figures",
    "new_buyers_outside_declared_periods",
    "new_customers_artifact_payload",
    "new_customers_calendar_period",
    "ntb_share_movers",
    "per_channel_awaiting",
    "per_channel_figures",
    "portfolio_acquisition_cards",
    "portfolio_acquisition_slot",
    "resolve_new_buyers",
    "resolve_new_customers_payload",
    "resolve_per_channel",
    "share_change",
    "short_window_label",
    "step_one_row",
    "window_label",
]

NEW_CUSTOMERS_ARTIFACT_TYPE: Final = "NewCustomersArtifact"
NEW_CUSTOMERS_ARTIFACT_VERSION: Final = 2
NEW_CUSTOMERS_PAGE_ID: Final = "new-customers"

GATEWAY_LIMIT: Final = 10
"""Products listed per channel before "And {N} more products." [proposal, LLD §6]."""

STEP_ONE_FOOT: Final = (
    "Attributed sales aren't added across channels, because one purchase can be credited to "
    "more than one channel."
)
GATEWAY_CAPTIONS: Final = (
    "Includes products bought because of brand halo, not only the products you advertised.",
    "Credited per channel. One purchase can be credited to more than one channel or campaign, "
    "so channels aren't added together.",
)
"""The two gateway captions, verbatim from design §6.2."""
GATEWAY_COLUMN_HEADER: Final = "New-to-brand purchases, brand halo included"
SHARE_BASIS: Final = "New-to-brand revenue (12-month lookback) ÷ attributed revenue (14-day)"
NEW_CUSTOMERS_BOUNDARY: Final = (
    "Amazon flags a purchase as new to brand when the shopper hasn't bought from the brand in "
    "the previous 12 months.",
    "New-to-brand revenue share divides a 12-month new-to-brand numerator by 14-day attributed "
    "revenue, so it uses two windows.",
)
NOT_RETURNED_TEXT: Final = "too few shoppers, or none"
NO_CALENDAR_COMPARISON_TEXT: Final = "No comparison for calendar periods."
"""A calendar preset's one no-comparison sentence: the scope band, the share-change cells and
the insight slot (LLD "Designed states"; NC-GATE-101)."""
NEW_BUYERS_SUBJECT: Final = AbsenceSubject("New buyers", plural=True)
NEW_BUYERS_DECLARED_PERIODS: Final = (
    TemporalPresetId.LAST_COMPLETE_WEEK,
    TemporalPresetId.LAST_COMPLETE_MONTH,
)
NEW_BUYERS_ACTION_LABEL: Final = "Show Latest measured week"
PER_CHANNEL_CAPTION: Final = "New buyers per channel"
PER_CHANNEL_COST_HEADER: Final = "Cost per new buyer"
PER_CHANNEL_FOOT: Final = (
    "New buyers aren't added across channels, because one shopper can buy after ads on more "
    "than one channel."
)
NEW_CUSTOMERS_COMPARED_METRICS: Final = frozenset({"ntb_revenue_share"})
"""The one figure the page compares across two windows: the step-1 change in share (ADR-121)."""
NEW_CUSTOMERS_PAGE_TEXT: Final[Mapping[str, str]] = {
    "boundary_heading": "About these figures",
    "channel_header": "Channel",
    "cost_per_new_buyer_label": "Cost per new buyer",
    "gateway_heading": "Gateway products",
    "gateway_product_header": "Product",
    "gateway_selector_label": "Channel",
    "gateway_share_header": "Share of channel new-to-brand purchases",
    "insights_heading": "Insights",
    "new_buyers_heading": "New buyers",
    "new_buyers_label": "New buyers",
    "not_available_text": "Not available",
}
"""The page's headings and labels, from the LLD and the approved mockups: every text the page
shows is a server field (LLD "Designed states"), as the Scorecard's are (slice 9a gate F7)."""
STEP_ONE_COLUMNS: Final = (
    ("ntb_purchases", "New-to-brand purchases"),
    ("ntb_revenue", "New-to-brand revenue"),
    ("ntb_revenue_share", "New-to-brand revenue share"),
    ("ntb_revenue_share_change", "Change in share"),
    ("cost_per_ntb_purchase", "Cost per new-to-brand purchase"),
)
"""Step 1's columns after Channel, in order, with the LLD's names."""
INSIGHT_KIND_LABEL: Final = "New-to-brand share mover"
"""The insight card's kind line (LLD "Insight contract: `portfolio-acquisition` v1")."""
NTB_SHARE_MOVER: Final = "ntb-share-mover"
NEW_CUSTOMERS_COVERAGE_METRICS: Final = ("ad_spend", "purchases_new_to_brand_12mo")
"""Delivery and attributed conversions gate a bake; gateway products and new buyers degrade."""
NEW_CUSTOMERS_DAILY_OPERAND: Final = "new-customers-daily-operands"
"""The one daily operand a calendar preset resolves on: the coverage that gates a bake."""
NEW_CUSTOMERS_EMPTY_TEXT: Final = (
    "No ad spend was delivered in this window. Choose another window to see New customers."
)
"""The page text of an empty body: no marketplace had delivery (slice 9 plan D17) [proposal]."""
NEW_CUSTOMERS_INSUFFICIENT_TEXT: Final = (
    "New customers can't be shown for this window. Choose another window."
)
"""The page text of an insufficient body, the third declared outcome (plan D17) [proposal]."""
_SEVERAL_MARKETPLACES: Final = 2
_COVERAGE_RECOVERY: Final = "It shows by itself when the data arrives."
"""A section's recovery while its delivery hasn't landed: the Scorecard's words (ADR-119)."""
_NO_SHARE_TEXT: Final = "No attributed revenue in this window, so there's no share."
_NO_NTB_TEXT: Final = "No new-to-brand purchases in this window."
_GATEWAY_SUBJECT: Final = "Gateway products"
_PERCENT: Final = Decimal(100)
_POINTS_QUANTUM: Final = Decimal("0.01")
_CRUMB: Final = "\u203a"
"""The breadcrumb separator the pointer lines name a tab with."""


class NewCustomersOutcome(StrEnum):
    """The artifact's closed outcomes, as the v2 page entry declares them."""

    COMPLETE = "complete"
    EMPTY = "empty"
    INSUFFICIENT = "insufficient"


class NewCustomersAbsenceReason(StrEnum):
    """Why a nullable figure is absent; the browser renders the paired text, never the code."""

    NO_ATTRIBUTED_REVENUE = "no_attributed_revenue"
    NO_NTB_PURCHASES = "no_ntb_purchases"
    NO_SHARE_TO_COMPARE = "no_share_to_compare"
    CALENDAR_PERIOD_SELECTED = "calendar_period_selected"
    NOT_RETURNED = "not_returned"
    NO_AD_SPEND = "no_ad_spend"
    NO_NEW_BUYERS = "no_new_buyers"
    """A returned row holds 0 new buyers: they show 0; the cost, a division by zero, is absent."""


@dataclass(frozen=True, slots=True)
class NewCustomersAbsence:
    """A typed absence: the reason code and the server text rendered under "Not available"."""

    reason: NewCustomersAbsenceReason
    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("a typed absence carries its server text.")


@dataclass(frozen=True, slots=True)
class NewCustomersFigure:
    """One figure: a finite value with its units, or a typed absence, never both."""

    metric: str
    value: Decimal | None
    units: Units | None
    absence: NewCustomersAbsence | None = None

    def __post_init__(self) -> None:
        if not self.metric.strip():
            raise ValueError("a New customers figure names its metric.")
        if (self.value is None) == (self.absence is None):
            raise ValueError("a New customers figure carries a value or a typed absence.")
        if (self.value is None) != (self.units is None):
            raise ValueError("a New customers figure carries units exactly when it has a value.")
        if self.value is not None and not self.value.is_finite():
            raise ValueError("New customers figures are finite.")

    @classmethod
    def absent(cls, metric: str, absence: NewCustomersAbsence) -> NewCustomersFigure:
        """Build the typed absence of one figure."""
        return cls(metric, None, None, absence)

    @property
    def display_value(self) -> str | None:
        """The server display value: currency at two places (ADR-084), else None."""
        if self.value is None or self.units is None:
            return None
        return metric_display_value(self.units, self.value)

    @property
    def display_text(self) -> str | None:
        """A rate's percentage text ("42.31%"), so the browser never scales a share."""
        if self.value is None or self.units is None or self.units.kind is not UnitKind.RATIO:
            return None
        return f"{(self.value * _PERCENT).quantize(_POINTS_QUANTUM)}%"


class ChangeDirection(StrEnum):
    """ADR-121's server-declared direction of a change."""

    UP = "up"
    DOWN = "down"
    FLAT = "flat"


@dataclass(frozen=True, slots=True)
class ShareChange:
    """The change in new-to-brand revenue share: ADR-121's block and its unrounded points.

    `change` is `build_change` over the two unrounded shares with the dictionary's
    `favourable_direction` for `ntb_revenue_share` (plan D4), so its tone, unit and text follow
    every other page's chips; `points` is the unrounded move the insight finding reads.
    """

    points: Decimal
    """Unrounded change in percentage points: (window share - prior share) x 100."""
    change: Change

    def __post_init__(self) -> None:
        if self.change.unit is not ChangeUnit.PERCENTAGE_POINTS:
            raise ValueError("a change in share reads in percentage points.")
        if self.change.direction.value != self.direction.value:
            raise ValueError("a change in share's block follows its unrounded points.")

    @property
    def direction(self) -> ChangeDirection:
        """Up, down, or flat for equal unrounded values (ADR-121 point 4)."""
        if self.points > 0:
            return ChangeDirection.UP
        if self.points < 0:
            return ChangeDirection.DOWN
        return ChangeDirection.FLAT

    @property
    def display_text(self) -> str:
        """The chip's text, exactly as ADR-121's builder writes it."""
        return self.change.display_text


def share_change(current: Decimal, prior: Decimal, semantics: ComparedMetric) -> ShareChange:
    """Build the change in share from the two unrounded shares and their governed semantics."""
    return ShareChange(
        (current - prior) * _PERCENT,
        build_change(ChangeOperands(current, prior, Units(UnitKind.RATIO)), semantics),
    )


@dataclass(frozen=True, slots=True)
class StepOneRow:
    """Step 1: one governed ad product with delivery in the window. Never totalled."""

    ad_product_key: str
    ad_product_label: str
    channel_group_label: str
    ntb_purchases: NewCustomersFigure
    ntb_revenue: NewCustomersFigure
    ntb_revenue_share: NewCustomersFigure
    ntb_revenue_share_change: ShareChange | NewCustomersAbsence
    cost_per_ntb_purchase: NewCustomersFigure

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.ad_product_key, self.ad_product_label, self.channel_group_label)
        ):
            raise ValueError("a step-1 row names its ad product and channel group.")
        if self.ntb_purchases.value is None or self.ntb_revenue.value is None:
            raise ValueError("step-1 purchases and revenue are never absent in a covered window.")
        if (self.ntb_purchases.value == 0) != (self.cost_per_ntb_purchase.value is None):
            raise ValueError("cost per new-to-brand purchase is absent exactly when there's none.")


@dataclass(frozen=True, slots=True)
class StepOneInput:
    """One ad product's planned answers in the window, and in the prior on a rolling preset.

    Every value is a planned answer (None where the answer had no row); nothing is divided here
    but the change in points. `prior_share` is None on a rolling preset with no prior share.
    """

    ad_product_key: str
    ad_product_label: str
    channel_group_label: str
    currency_code: str
    ntb_purchases: Decimal | None
    ntb_revenue: Decimal | None
    attributed_revenue: Decimal | None
    share: Decimal | None
    cost_per_ntb: Decimal | None
    compared: bool
    """True on a rolling preset (equal prior); a calendar preset shows no comparison."""
    prior_share: Decimal | None = None
    window_text: str = "this window"
    prior_text: str = "the prior window"
    share_semantics: ComparedMetric | None = None
    """`ntb_revenue_share`'s governed change semantics (ADR-121), required on a rolling preset."""


def step_one_row(item: StepOneInput) -> StepOneRow:
    """Build one step-1 row; a covered ad product with no conversion row is a true zero.

    Raises ValueError when a planned answer contradicts its operands (a share with no attributed
    revenue, or none with some), which the materializer reports as an invariant break.
    """
    count = Units(UnitKind.COUNT)
    money = Units(UnitKind.CURRENCY, item.currency_code)
    ratio = Units(UnitKind.RATIO)
    purchases = item.ntb_purchases or Decimal(0)
    revenue = item.ntb_revenue or Decimal(0)
    attributed = item.attributed_revenue or Decimal(0)
    if (attributed == 0) != (item.share is None):
        raise ValueError("a new-to-brand share is planned exactly when attributed revenue isn't 0.")
    if (purchases == 0) != (item.cost_per_ntb is None):
        raise ValueError(
            "cost per new-to-brand purchase is planned exactly when purchases aren't 0."
        )
    share = (
        NewCustomersFigure.absent(
            "ntb_revenue_share",
            NewCustomersAbsence(NewCustomersAbsenceReason.NO_ATTRIBUTED_REVENUE, _NO_SHARE_TEXT),
        )
        if item.share is None
        else NewCustomersFigure("ntb_revenue_share", item.share, ratio)
    )
    change: ShareChange | NewCustomersAbsence
    if item.compared and item.share_semantics is None:
        raise ValueError("a compared share declares its change semantics (ADR-121).")
    if not item.compared:
        change = NewCustomersAbsence(
            NewCustomersAbsenceReason.CALENDAR_PERIOD_SELECTED, NO_CALENDAR_COMPARISON_TEXT
        )
    elif item.share is None or item.prior_share is None:
        missing = item.window_text if item.share is None else item.prior_text
        change = NewCustomersAbsence(
            NewCustomersAbsenceReason.NO_SHARE_TO_COMPARE,
            f"No new-to-brand revenue share for {missing}, so there's no change.",
        )
    else:
        semantics = cast("ComparedMetric", item.share_semantics)
        change = share_change(item.share, item.prior_share, semantics)
    return StepOneRow(
        ad_product_key=item.ad_product_key,
        ad_product_label=item.ad_product_label,
        channel_group_label=item.channel_group_label,
        ntb_purchases=NewCustomersFigure("purchases_new_to_brand_12mo", purchases, count),
        ntb_revenue=NewCustomersFigure("revenue_new_to_brand_12mo", revenue, money),
        ntb_revenue_share=share,
        ntb_revenue_share_change=change,
        cost_per_ntb_purchase=(
            NewCustomersFigure.absent(
                "cost_per_ntb",
                NewCustomersAbsence(NewCustomersAbsenceReason.NO_NTB_PURCHASES, _NO_NTB_TEXT),
            )
            if item.cost_per_ntb is None
            else NewCustomersFigure("cost_per_ntb", item.cost_per_ntb, money)
        ),
    )


class NewBuyersState(StrEnum):
    """What the New buyers section shows for the page's period (LLD "Step 2")."""

    FIGURES = "figures"
    OUTSIDE_DECLARED_PERIODS = "outside_declared_periods"
    PENDING = "pending"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class NewBuyersAction:
    """The rolling callout's button: it selects a declared preset."""

    label: str
    preset: TemporalPresetId


@dataclass(frozen=True, slots=True)
class NewBuyers:
    """Step 2: platform-grain new buyers for complete weeks and months, or why there are none."""

    state: NewBuyersState
    text: str | None = None
    action: NewBuyersAction | None = None
    new_buyers: NewCustomersFigure | None = None
    cost_per_new_buyer: NewCustomersFigure | None = None
    period: CalendarPeriod | None = None
    expected_after: date | None = None
    latest_available_period: DateRange | None = None
    new_buyers_basis: str | None = None
    cost_basis: str | None = None
    per_channel: PerChannel | None = None
    """New buyers per channel for the same period: present exactly with the two tiles (v2)."""

    def __post_init__(self) -> None:
        figures = self.state is NewBuyersState.FIGURES
        if figures != (self.new_buyers is not None and self.cost_per_new_buyer is not None):
            raise ValueError("only the figures state carries the two tiles.")
        if figures != (self.per_channel is not None):
            raise ValueError("the per-channel table nests under the tiles, never without them.")
        if self.per_channel is not None and self.per_channel.period != self.period:
            raise ValueError("the per-channel table shows the tiles' period.")
        if figures == (self.text is not None):
            raise ValueError("every state but figures carries its server text.")
        if (self.state is NewBuyersState.OUTSIDE_DECLARED_PERIODS) != (self.action is not None):
            raise ValueError("only the rolling callout carries its action.")
        if self.state in {NewBuyersState.PENDING, NewBuyersState.MISSING} and (
            self.period is None or self.expected_after is None
        ):
            raise ValueError("a pending or missing state names its period and expected date.")
        if figures:
            buyers, cost = self.new_buyers, self.cost_per_new_buyer
            if buyers is None or cost is None:  # pragma: no cover — checked above
                raise ValueError("the figures state carries the two tiles.")
            if (cost.value is None) != (buyers.value is None or buyers.value == 0):
                raise ValueError(
                    "cost per new buyer is absent exactly when new buyers are absent or 0."
                )


def new_buyers_outside_declared_periods(period_grains: tuple[TimeGrain, ...]) -> NewBuyers:
    """Build the rolling-preset callout: one text and the "Show Latest measured week" action."""
    return NewBuyers(
        state=NewBuyersState.OUTSIDE_DECLARED_PERIODS,
        text=declared_period_absence_text(
            NEW_BUYERS_SUBJECT, period_grains, NEW_BUYERS_DECLARED_PERIODS
        ),
        action=NewBuyersAction(NEW_BUYERS_ACTION_LABEL, TemporalPresetId.LAST_COMPLETE_WEEK),
    )


def _new_buyers_basis(period: CalendarPeriod, caveat: str) -> str:
    basis = (
        f"Ad-attributed buyers · 14-day attribution · conversion date · advertised products · "
        f"{period.label}"
    )
    return f"{basis}. {caveat}" if caveat else basis


@dataclass(frozen=True, slots=True)
class NewBuyersFiguresRequest:
    """Period P's two planned answers, the spend its cost basis names, and their labels.

    `buyers` None is the stored `not_returned` cell; 0 is a returned cell with no new buyer, whose
    planned cost has no value (a division by zero). The cost is the planned D-6 ratio, never
    divided here; `spend` is the operand the cost basis names.
    """

    period: CalendarPeriod
    buyers: Decimal | None
    cost: Decimal | None
    spend: Decimal | None
    currency_code: str
    caveat: str
    per_channel: PerChannel

    def __post_init__(self) -> None:
        if _has_new_buyers(self.buyers) and (self.cost is None or self.spend is None):
            raise ValueError("returned new buyers carry their planned cost and its spend.")
        if self.buyers == 0 and self.cost is not None:
            raise ValueError("no new buyers have no cost per new buyer.")


def _has_new_buyers(buyers: Decimal | None) -> bool:
    """Return whether a returned cell is above 0: only then has cost per new buyer a value."""
    return buyers is not None and buyers != 0


def new_buyers_figures(request: NewBuyersFiguresRequest) -> NewBuyers:
    """Both tiles for period P; a cell that didn't come back reads "too few shoppers, or none"."""
    period, buyers, cost, spend = request.period, request.buyers, request.cost, request.spend
    currency_code = request.currency_code
    absence = NewCustomersAbsence(NewCustomersAbsenceReason.NOT_RETURNED, NOT_RETURNED_TEXT)
    if buyers is None:
        tile = NewCustomersFigure.absent("buyers_new_to_brand_12mo", absence)
        price = NewCustomersFigure.absent("cost_per_new_to_brand_buyer_12mo", absence)
        cost_basis = f"Ad spend ÷ new buyers · {period.label}"
    elif buyers == 0:
        # A returned 0: new buyers show 0 and the cost reads why it has no value.
        tile = NewCustomersFigure("buyers_new_to_brand_12mo", buyers, Units(UnitKind.COUNT))
        price = NewCustomersFigure.absent(
            "cost_per_new_to_brand_buyer_12mo",
            NewCustomersAbsence(
                NewCustomersAbsenceReason.NO_NEW_BUYERS, f"No new buyers in {period.label}."
            ),
        )
        cost_basis = f"Ad spend ÷ new buyers · {period.label}"
    else:
        if cost is None or spend is None:  # pragma: no cover — the request refuses it
            raise ValueError("returned new buyers carry their planned cost and its spend.")
        tile = NewCustomersFigure("buyers_new_to_brand_12mo", buyers, Units(UnitKind.COUNT))
        price = NewCustomersFigure(
            "cost_per_new_to_brand_buyer_12mo", cost, Units(UnitKind.CURRENCY, currency_code)
        )
        cost_basis = (
            f"Ad spend ÷ new buyers · {period.label} · "
            f"{metric_display_value(Units(UnitKind.CURRENCY, currency_code), spend)} "
            f"{currency_code} ÷ {buyers}"
        )
    return NewBuyers(
        state=NewBuyersState.FIGURES,
        new_buyers=tile,
        cost_per_new_buyer=price,
        period=period,
        new_buyers_basis=_new_buyers_basis(period, request.caveat),
        cost_basis=cost_basis,
        per_channel=request.per_channel,
    )


def _awaiting_text(state: NewBuyersState, period: CalendarPeriod, expected_after: date) -> str:
    if state is NewBuyersState.PENDING:
        return f"Figures for {period.label} are expected after {day_label(expected_after)}."
    return f"Figures for {period.label} didn't arrive."


@dataclass(frozen=True, slots=True)
class NewBuyersAwaitingRequest:
    """Period P whose delivery hasn't landed, its expected date, and the date deciding it."""

    period: CalendarPeriod
    expected_after: date
    latest_available_period: DateRange | None
    as_of: date

    def __post_init__(self) -> None:
        if self.expected_after < self.period.end:
            raise ValueError("a period's delivery is expected no earlier than its last day.")


def new_buyers_awaiting(request: NewBuyersAwaitingRequest) -> NewBuyers:
    """P's delivery hasn't landed: pending through `expected_after`, missing after it.

    Baked with the bake's date; `resolve_new_buyers` re-decides it with the serving date, so a
    late delivery turns pending into missing without a rebake (ADR-107).
    """
    state = _awaiting_state(request.expected_after, request.latest_available_period, request.as_of)
    return NewBuyers(
        state=state,
        text=_awaiting_text(state, request.period, request.expected_after),
        period=request.period,
        expected_after=request.expected_after,
        latest_available_period=request.latest_available_period,
    )


def _awaiting_state(
    expected_after: date, latest_available_period: DateRange | None, as_of: date
) -> NewBuyersState:
    resolved = resolve_period_state(
        ResolvePeriodStateRequest(
            PaidFeatureState(
                feature_observed=True,
                period_published=False,
                expected_after=expected_after,
                latest_available_period=latest_available_period,
            ),
            as_of,
        )
    )
    return (
        NewBuyersState.PENDING if resolved.state is PeriodState.PENDING else NewBuyersState.MISSING
    )


def resolve_new_buyers(baked: NewBuyers, as_of: date) -> NewBuyers:
    """Re-decide a baked pending or missing section, or its per-channel table, with today's date."""
    if baked.state is NewBuyersState.FIGURES and baked.per_channel is not None:
        return replace(baked, per_channel=resolve_per_channel(baked.per_channel, as_of))
    if baked.state not in {NewBuyersState.PENDING, NewBuyersState.MISSING}:
        return baked
    period, expected_after = baked.period, baked.expected_after
    if period is None or expected_after is None:  # pragma: no cover — NewBuyers refuses it
        raise ValueError("an awaiting New buyers section names its period.")
    return new_buyers_awaiting(
        NewBuyersAwaitingRequest(
            period,
            expected_after=expected_after,
            latest_available_period=baked.latest_available_period,
            as_of=as_of,
        )
    )


class PerChannelState(StrEnum):
    """What the per-channel table shows for the tiles' period (LLD "Step 2", v2)."""

    ROWS = "rows"
    NO_CHANNELS = "no_channels"
    PENDING = "pending"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class PerChannelRow:
    """One ad product's new buyers for the period. There's no total or subtotal row."""

    ad_product_key: str
    ad_product_label: str
    channel_group_label: str
    new_buyers: NewCustomersFigure
    cost_per_new_buyer: NewCustomersFigure

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.ad_product_key, self.ad_product_label, self.channel_group_label)
        ):
            raise ValueError("a per-channel row names its ad product and channel group.")
        if self.new_buyers.value is None and self.cost_per_new_buyer.value is not None:
            raise ValueError("cost per new buyer is absent when the ad product's buyers are.")


@dataclass(frozen=True, slots=True)
class PerChannel:
    """New buyers per channel: the rows, or the line that replaces the table, and the note."""

    state: PerChannelState
    period: CalendarPeriod
    text: str | None = None
    rows: tuple[PerChannelRow, ...] = ()
    note: str | None = None
    """`DEGRADED_NOTE` when the roster loss is graded degraded; the roster is never withdrawn."""
    expected_after: date | None = None
    latest_available_period: DateRange | None = None
    new_buyers_basis: str | None = None

    def __post_init__(self) -> None:
        listed = self.state is PerChannelState.ROWS
        if listed != bool(self.rows) or listed == (self.text is not None):
            raise ValueError("a per-channel table carries rows, or its server text, never both.")
        if self.note is not None and not listed:
            raise ValueError("only a table carries the degraded note.")
        if listed != (self.new_buyers_basis is not None):
            raise ValueError("only a table carries the new buyers basis.")
        awaiting = self.state in {PerChannelState.PENDING, PerChannelState.MISSING}
        if awaiting != (self.expected_after is not None):
            raise ValueError("a pending or missing per-channel table names its expected date.")
        keys = tuple(row.ad_product_key for row in self.rows)
        if len(set(keys)) != len(keys):
            raise ValueError("the per-channel table carries one row per ad product.")


@dataclass(frozen=True, slots=True)
class PerChannelInput:
    """One ad product's planned answers at grain set `channel`, and whether it's on the roster.

    `buyers` None is a rostered ad product with no row; 0 is a returned row with no new buyer,
    whose cost has no value; `on_roster` False is a returned ad product with no ad spend in the
    period. The cost is the planned D-6 ratio, never divided here.
    """

    ad_product_key: str
    ad_product_label: str
    channel_group_label: str
    buyers: Decimal | None
    cost: Decimal | None
    on_roster: bool

    def __post_init__(self) -> None:
        if self.buyers is None and not self.on_roster:
            raise ValueError("an ad product with no row is listed only from the roster.")
        if _has_new_buyers(self.buyers) and self.on_roster and self.cost is None:
            raise ValueError("a rostered ad product's new buyers carry their planned cost.")
        if self.buyers == 0 and self.cost is not None:
            raise ValueError("no new buyers have no cost per new buyer.")


@dataclass(frozen=True, slots=True)
class PerChannelFiguresRequest:
    """The landed per-channel delivery for P: its rows in taxonomy order and the roster's note."""

    period: CalendarPeriod
    currency_code: str
    rows: tuple[PerChannelInput, ...]
    note: str | None
    caveat: str


def per_channel_figures(request: PerChannelFiguresRequest) -> PerChannel:
    """Build the per-channel table; an empty roster with no row reads the no-channels line."""
    period = request.period
    if not request.rows:
        # 0 / 0 grades not degraded, so no note shows (LLD "An empty roster").
        return PerChannel(
            PerChannelState.NO_CHANNELS,
            period,
            text=f"No channel had ad spend in {period.label}.",
        )
    not_returned = NewCustomersAbsence(NewCustomersAbsenceReason.NOT_RETURNED, NOT_RETURNED_TEXT)
    count, money = Units(UnitKind.COUNT), Units(UnitKind.CURRENCY, request.currency_code)
    rows: list[PerChannelRow] = []
    for item in request.rows:
        if item.buyers is None:
            buyers = NewCustomersFigure.absent("buyers_new_to_brand_12mo", not_returned)
            cost = NewCustomersFigure.absent("cost_per_new_to_brand_buyer_12mo", not_returned)
        else:
            buyers = NewCustomersFigure("buyers_new_to_brand_12mo", item.buyers, count)
            if not item.on_roster:
                cost = NewCustomersFigure.absent(
                    "cost_per_new_to_brand_buyer_12mo",
                    NewCustomersAbsence(
                        NewCustomersAbsenceReason.NO_AD_SPEND,
                        f"No ad spend for {item.ad_product_label} in {period.label}.",
                    ),
                )
            elif item.buyers == 0:
                # A returned 0 with spend: new buyers show 0, the cost is a division by zero.
                cost = NewCustomersFigure.absent(
                    "cost_per_new_to_brand_buyer_12mo",
                    NewCustomersAbsence(
                        NewCustomersAbsenceReason.NO_NEW_BUYERS,
                        f"No new buyers for {item.ad_product_label} in {period.label}.",
                    ),
                )
            else:
                cost = NewCustomersFigure(
                    "cost_per_new_to_brand_buyer_12mo", cast("Decimal", item.cost), money
                )
        rows.append(
            PerChannelRow(
                item.ad_product_key, item.ad_product_label, item.channel_group_label, buyers, cost
            )
        )
    return PerChannel(
        PerChannelState.ROWS,
        period,
        rows=tuple(rows),
        note=request.note,
        new_buyers_basis=_new_buyers_basis(period, request.caveat),
    )


@dataclass(frozen=True, slots=True)
class PerChannelAwaitingRequest:
    """Period P whose per-channel delivery hasn't landed, its expected date and today's date."""

    period: CalendarPeriod
    expected_after: date
    latest_available_period: DateRange | None
    as_of: date

    def __post_init__(self) -> None:
        if self.expected_after < self.period.end:
            raise ValueError("a period's delivery is expected no earlier than its last day.")


def per_channel_awaiting(request: PerChannelAwaitingRequest) -> PerChannel:
    """P's per-channel delivery hasn't landed: pending through `expected_after`, missing after.

    Even with an empty roster: `no_channels` applies only once the delivery has landed.
    """
    state = _awaiting_state(request.expected_after, request.latest_available_period, request.as_of)
    label = request.period.label
    pending = state is NewBuyersState.PENDING
    return PerChannel(
        PerChannelState.PENDING if pending else PerChannelState.MISSING,
        request.period,
        text=(
            f"Figures per channel for {label} are expected after "
            f"{day_label(request.expected_after)}."
            if pending
            else f"Figures per channel for {label} didn't arrive."
        ),
        expected_after=request.expected_after,
        latest_available_period=request.latest_available_period,
    )


def resolve_per_channel(baked: PerChannel, as_of: date) -> PerChannel:
    """Re-decide a baked pending or missing per-channel table with the serving date (ADR-107)."""
    if baked.state not in {PerChannelState.PENDING, PerChannelState.MISSING}:
        return baked
    expected_after = baked.expected_after
    if expected_after is None:  # pragma: no cover — PerChannel refuses it
        raise ValueError("an awaiting per-channel table names its expected date.")
    return per_channel_awaiting(
        PerChannelAwaitingRequest(
            baked.period, expected_after, baked.latest_available_period, as_of
        )
    )


class GatewayState(StrEnum):
    """What one channel's gateway entry shows."""

    ROWS = "rows"
    NO_NTB_PURCHASES = "no_ntb_purchases"
    NOT_COVERED = "not_covered"


@dataclass(frozen=True, slots=True)
class GatewayRow:
    """One product's halo-inclusive new-to-brand purchases inside one channel."""

    asin: str
    title: str
    ntb_purchases: int
    share_of_channel: Decimal

    def __post_init__(self) -> None:
        if not self.asin.strip() or not self.title.strip():
            raise ValueError("a gateway row names its product.")
        if self.ntb_purchases < 1:
            raise ValueError("a product with no new-to-brand purchases isn't listed.")
        if not Decimal(0) < self.share_of_channel <= 1:
            raise ValueError("a gateway share of channel is a fraction of that channel's total.")

    @property
    def share_text(self) -> str:
        """The share as a percentage, so the browser never scales it."""
        return f"{(self.share_of_channel * _PERCENT).quantize(_POINTS_QUANTUM)}%"


@dataclass(frozen=True, slots=True)
class GatewayEntry:
    """Gateway products of exactly one channel. There's no all-channel list or product total."""

    ad_product_key: str
    channel_label: str
    state: GatewayState
    rows: tuple[GatewayRow, ...] = ()
    more_count: int = 0
    text: str | None = None
    first_covered_date: date | None = None

    def __post_init__(self) -> None:
        if not self.ad_product_key.strip() or not self.channel_label.strip():
            raise ValueError("a gateway entry names its channel.")
        listed = self.state is GatewayState.ROWS
        if listed != bool(self.rows) or listed == (self.text is not None):
            raise ValueError("a gateway entry carries rows, or its server text, never both.")
        if len(self.rows) > GATEWAY_LIMIT or self.more_count < 0:
            raise ValueError("a gateway entry lists at most ten products and counts the rest.")
        if self.more_count and len(self.rows) < GATEWAY_LIMIT:
            raise ValueError("a gateway entry counts more products only once it's full.")
        order = [(-row.ntb_purchases, row.asin) for row in self.rows]
        if order != sorted(order) or len({row.asin for row in self.rows}) != len(self.rows):
            raise ValueError("gateway rows are distinct and ordered by purchases, then ASIN.")
        if self.first_covered_date is not None and self.state is not GatewayState.NOT_COVERED:
            raise ValueError("only a not-covered entry names its first covered date.")

    @property
    def caption_title(self) -> str:
        """The caption title, naming this one channel."""
        return f"Products new-to-brand shoppers bought first, {self.channel_label}"

    @property
    def more_text(self) -> str | None:
        """The bounded list's foot line."""
        return None if not self.more_count else f"And {self.more_count} more products."


def gateway_entry(  # noqa: PLR0913 -- the channel, its two answers and its listed count
    channel: tuple[str, str],
    products: dict[str, tuple[str, int]],
    channel_total: int | None,
    *,
    listed_count: int,
) -> GatewayEntry:
    """Build one channel's covered gateway entry from its two planned answers.

    `products` maps each ASIN to (title, halo-inclusive new-to-brand purchases summed across this
    channel's campaigns): the query's top `GATEWAY_LIMIT` (ADR-152). `listed_count` is how many
    products with a purchase the channel has in all, which the "N more" line counts from.
    `channel_total` is the same metric at this ad product's grain, the share's denominator
    (never step 1's halo-excluding count). Zero or no row there reads the "no purchases were
    credited" text, whatever step 1 shows.
    """
    ad_product_key, channel_label = channel
    listed = sorted(
        ((asin, title, count) for asin, (title, count) in products.items() if count > 0),
        key=lambda item: (-item[2], item[0]),
    )
    if listed_count < len(listed):
        raise ValueError("a channel lists at least the products it shows.")
    if not channel_total:
        if listed:
            raise ValueError("a channel with product purchases has a channel total above 0.")
        return GatewayEntry(
            ad_product_key,
            channel_label,
            GatewayState.NO_NTB_PURCHASES,
            text=f"No new-to-brand purchases were credited to {channel_label} in this window.",
        )
    total = Decimal(channel_total)
    shown = tuple(
        GatewayRow(asin, title, count, Decimal(count) / total)
        for asin, title, count in listed[:GATEWAY_LIMIT]
    )
    if not shown:
        raise ValueError("a channel total above 0 needs at least one product row.")
    return GatewayEntry(
        ad_product_key,
        channel_label,
        GatewayState.ROWS,
        rows=shown,
        more_count=listed_count - len(shown),
    )


def gateway_not_covered(channel: tuple[str, str], verdict: PackCoverageVerdict) -> GatewayEntry:
    """Build one channel's coverage-gate text: never a partial list over a pack-v1 day."""
    ad_product_key, channel_label = channel
    return GatewayEntry(
        ad_product_key,
        channel_label,
        GatewayState.NOT_COVERED,
        text=pack_coverage_text(_GATEWAY_SUBJECT, verdict),
        first_covered_date=verdict.first_covered_date,
    )


@dataclass(frozen=True, slots=True)
class NewCustomersPointer:
    """A governed pointer line; the browser renders it only when its tab is in the manifest."""

    order: int
    text: str
    link_text: str
    tab_id: str
    href: str

    def __post_init__(self) -> None:
        if not self.text.endswith(f" {self.link_text}."):
            raise ValueError("a pointer line ends with its link text.")

    @property
    def lead(self) -> str:
        """The words before the link, so the page links the tab without cutting served text."""
        return self.text.removesuffix(f" {self.link_text}.")


NEW_CUSTOMERS_POINTERS: Final = (
    NewCustomersPointer(
        1,
        f"Which audience segments your buyers belong to is on Reach and audiences {_CRUMB} "
        "Audiences.",
        f"Reach and audiences {_CRUMB} Audiences",
        "audiences",
        "/audiences/",
    ),
    NewCustomersPointer(
        2,
        f"Whether new customers buy again is on Customers {_CRUMB} Retention.",
        f"Customers {_CRUMB} Retention",
        "retention",
        "/retention/",
    ),
)


@dataclass(frozen=True, slots=True)
class NewCustomersLabels:
    """Halo scope and windows read from dictionary metadata, never mapped in code (LLD §6)."""

    network_label: str
    attribution_window_days: int
    lookback_months: int
    step_one_includes_brand_halo: bool
    gateway_includes_brand_halo: bool

    def __post_init__(self) -> None:
        if self.step_one_includes_brand_halo or not self.gateway_includes_brand_halo:
            raise ValueError(
                "step 1 counts advertised products only, and gateway products include brand halo."
            )

    def step_one_caption(self, window: str) -> str:
        """Build the step-1 caption's basis line, with its halo scope from the dictionary."""
        halo = "includes brand halo" if self.step_one_includes_brand_halo else "excludes brand halo"
        return (
            f"{self.network_label}-attributed · {self.attribution_window_days}-day · conversion "
            f"date · new to brand in the last {self.lookback_months} months · {halo} · {window}"
        )


@dataclass(frozen=True, slots=True)
class NewCustomersMarket:
    """Every section for one marketplace and currency; two marketplaces never share a table."""

    marketplace_key: str
    currency_code: str
    new_to_brand: tuple[StepOneRow, ...]
    new_buyers: NewBuyers
    gateway: tuple[GatewayEntry, ...]
    marketplace_label: str | None = None
    """The marketplace's short or governed name, which names the market with two shown."""

    def __post_init__(self) -> None:
        if self.marketplace_label is not None and not self.marketplace_label.strip():
            raise ValueError("a marketplace label is nonblank.")
        keys = tuple(row.ad_product_key for row in self.new_to_brand)
        if len(set(keys)) != len(keys):
            raise ValueError("step 1 carries one row per ad product.")
        if tuple(entry.ad_product_key for entry in self.gateway) != keys:
            raise ValueError("gateway products carry one entry per step-1 ad product, in order.")
        for row in self.new_to_brand:
            for figure in (row.ntb_revenue, row.cost_per_ntb_purchase):
                units = figure.units
                if units is not None and units.currency_code != self.currency_code:
                    raise ValueError("a marketplace section is in one currency.")
        states = {entry.state for entry in self.gateway}
        if GatewayState.NOT_COVERED in states and states != {GatewayState.NOT_COVERED}:
            raise ValueError("an uncovered window shows every channel's coverage text.")
        per_channel = self.new_buyers.per_channel
        for row in () if per_channel is None else per_channel.rows:
            units = row.cost_per_new_buyer.units
            if units is not None and units.currency_code != self.currency_code:
                raise ValueError("a marketplace section is in one currency.")
            # The browser's rule, mirrored so a disagreement fails the bake (NC-GATE-103): a
            # row is one ad product, either in step 1 or off it with no ad spend. Anything
            # else, such as a total or subtotal row, is refused (ADR-106).
            absence = row.cost_per_new_buyer.absence
            no_spend = (
                absence is not None and absence.reason is NewCustomersAbsenceReason.NO_AD_SPEND
            )
            if (row.ad_product_key in keys) == no_spend:
                raise ValueError(
                    "a per-channel row is a step-1 ad product, or one with no ad spend."
                )


@dataclass(frozen=True, slots=True)
class NtbShareMover:
    """An `ntb-share-mover` candidate: one ad product's share move, inside one marketplace."""

    marketplace_key: str
    ad_product_key: str
    ad_product_label: str
    finding: DeterministicFinding
    prior: Decimal


def ntb_share_movers(markets: tuple[NewCustomersMarket, ...]) -> tuple[NtbShareMover, ...]:
    """Each compared ad product's share move as an absolute change, never across channels."""
    found: list[NtbShareMover] = []
    for market in markets:
        for row in market.new_to_brand:
            change = row.ntb_revenue_share_change
            current = row.ntb_revenue_share.value
            if not isinstance(change, ShareChange) or current is None:
                continue
            prior = current - change.points / _PERCENT
            found.append(
                NtbShareMover(
                    market.marketplace_key,
                    row.ad_product_key,
                    row.ad_product_label,
                    DeterministicFinding(
                        f"{row.ad_product_label} new-to-brand revenue share change",
                        current - prior,
                    ),
                    prior,
                )
            )
    return tuple(found)


def _points_word(points: Decimal) -> str:
    return "point" if points == 1 else "points"


@dataclass(frozen=True, slots=True)
class PortfolioAcquisitionSlotRequest:
    """The material movers, the artifact revision, the silence condition and the two windows.

    `windows` is (window label, prior label), or None on a calendar preset (no comparison).
    `silence_reason` is served only on a rolling preset, where a prior window was compared.
    """

    material: tuple[NtbShareMover, ...]
    artifact_revision: str
    silence_reason: str
    windows: tuple[str, str] | None

    def __post_init__(self) -> None:
        if not self.artifact_revision.strip() or not self.silence_reason.strip():
            raise ValueError("a portfolio-acquisition slot names its revision and silence reason.")


@dataclass(frozen=True, slots=True)
class PortfolioAcquisitionCard:
    """One material mover's card: its title and body, inside one ad product and marketplace."""

    marketplace_key: str
    ad_product_key: str
    headline: str
    body: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.marketplace_key, self.ad_product_key, self.headline, self.body)
        ):
            raise ValueError("a portfolio-acquisition card names its mover, title and body.")


def portfolio_acquisition_cards(
    request: PortfolioAcquisitionSlotRequest,
) -> tuple[PortfolioAcquisitionCard, ...]:
    """Each material mover's card, largest first, at most three; none when the slot is silent.

    The LLD's card: title "{ad product} new-to-brand share {rose or fell} {n} points" ("point"
    when n is 1), body "Inside {ad product} only, {window} compared with {prior window}."
    """
    material, windows = request.material, request.windows
    if not material or windows is None:
        return ()
    window, prior = windows
    ordered = sorted(
        material,
        key=lambda item: (-abs(item.finding.value), item.marketplace_key, item.ad_product_key),
    )[:3]
    cards: list[PortfolioAcquisitionCard] = []
    for item in ordered:
        points = abs(item.finding.value * _PERCENT).quantize(_POINTS_QUANTUM).normalize()
        verb = "rose" if item.finding.value > 0 else "fell"
        cards.append(
            PortfolioAcquisitionCard(
                item.marketplace_key,
                item.ad_product_key,
                # Fixed-point, so a whole ten reads "10", never normalize()'s "1E+1".
                f"{item.ad_product_label} new-to-brand share {verb} {points:f} "
                f"{_points_word(points)}",
                f"Inside {item.ad_product_label} only, {window} compared with {prior}.",
            )
        )
    return tuple(cards)


def portfolio_acquisition_slot(request: PortfolioAcquisitionSlotRequest) -> InsightSlot:
    """Render the deterministic `portfolio-acquisition` slot; no prompt or model is involved.

    One sentence pair per material mover (its card's title and body), largest first, at most
    three. Silent on a calendar preset (`windows` None: no comparison) and when nothing is
    material.

    A calendar preset compared nothing, so its reason is the page's no-comparison sentence,
    never the prior-window silence condition (NC-GATE-101). Quiet for a missing comparison is
    `evaluated-quiet` (insight-presentation contract §2).
    """
    material, windows = request.material, request.windows
    artifact_revision = request.artifact_revision
    if not material or windows is None:
        silence_reason = NO_CALENDAR_COMPARISON_TEXT if windows is None else request.silence_reason
        return InsightSlot(
            InsightSlotId.PORTFOLIO_ACQUISITION,
            InsightOutputClass.PERSPECTIVE,
            InsightSlotState.SILENT,
            None,
            None,
            artifact_revision,
            "deterministic-v1",
            absence_class=InsightAbsenceClass.EVALUATED_QUIET,
            absence_reason=silence_reason,
        )
    sentences = [f"{card.headline}. {card.body}" for card in portfolio_acquisition_cards(request)]
    return InsightSlot(
        InsightSlotId.PORTFOLIO_ACQUISITION,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.FILLED,
        " ".join(sentences),
        "Found by a fixed rule from the figures above. Not written by an AI model.",
        artifact_revision,
        "deterministic-v1",
    )


def window_label(window: DateRange) -> str:
    """Name a rolling window as the date control does: "Sep 14\u201320, 2026" (ADR-120)."""
    return control_window_label(window)


def short_window_label(window: DateRange) -> str:
    """Name a window without its year, as the insight card's body does: "Sep 7\u201313"."""
    return range_text(window, with_year=False)


@dataclass(frozen=True, slots=True)
class NewCustomersArtifact:
    """`NewCustomersArtifact` v2. Rendering-free: every text is a server field."""

    outcome: NewCustomersOutcome
    preset: TemporalPresetId
    period: DateRange
    prior_period: DateRange | None
    period_label: str
    labels: NewCustomersLabels
    markets: tuple[NewCustomersMarket, ...]
    insights: tuple[InsightSlot, ...]
    insight_cards: tuple[PortfolioAcquisitionCard, ...] = ()
    """The filled slot's cards, one per material mover, in its narration order."""

    def __post_init__(self) -> None:
        if self.prior_period is not None and self.prior_period.end >= self.period.start:
            raise ValueError("a New customers prior window ends before its window starts.")
        if (self.outcome is NewCustomersOutcome.COMPLETE) != bool(self.markets):
            raise ValueError("only a complete New customers artifact carries sections.")
        marketplaces = tuple(market.marketplace_key for market in self.markets)
        if len(set(marketplaces)) != len(marketplaces):
            raise ValueError("a New customers artifact carries one section per marketplace.")
        if len(self.markets) >= _SEVERAL_MARKETPLACES and any(
            market.marketplace_label is None for market in self.markets
        ):
            raise ValueError("with two marketplaces each market names its marketplace.")
        if tuple(slot.id for slot in self.insights) != (InsightSlotId.PORTFOLIO_ACQUISITION,):
            raise ValueError("the New customers insight roster is portfolio-acquisition v1.")
        (slot,) = self.insights
        narrated = " ".join(f"{card.headline}. {card.body}" for card in self.insight_cards)
        if (slot.state is InsightSlotState.FILLED) != bool(self.insight_cards) or (
            self.insight_cards and slot.text != narrated
        ):
            raise ValueError("the insight cards are exactly the filled slot's narration.")
        rolling = self.prior_period is not None
        for market in self.markets:
            outside = market.new_buyers.state is NewBuyersState.OUTSIDE_DECLARED_PERIODS
            if rolling != outside:
                raise ValueError("new buyers show only on a calendar preset.")
            for row in market.new_to_brand:
                calendar_absence = (
                    isinstance(row.ntb_revenue_share_change, NewCustomersAbsence)
                    and row.ntb_revenue_share_change.reason
                    is NewCustomersAbsenceReason.CALENDAR_PERIOD_SELECTED
                )
                if rolling == calendar_absence:
                    raise ValueError("a change in share shows on a rolling preset only.")

    @property
    def page_context_label(self) -> str:
        """The assistant chip's label (ADR-120)."""
        return f"New customers · {self.period_label}"

    @property
    def starter_questions(self) -> tuple[str, ...]:
        """One starter, inside one channel at a time; it never asks for a cross-channel total."""
        return (f"Which channel had the highest new-to-brand share in {self.period_label}?",)

    @property
    def scope_text(self) -> str:
        """The scope band: the window, the server preset label and its comparison."""
        label = temporal_preset_label_v2(self.preset)
        if self.prior_period is None:
            return f"Showing {self.period_label} ({label}). {NO_CALENDAR_COMPARISON_TEXT}"
        return (
            f"Showing {self.period_label} ({label}) compared with "
            f"{short_window_label(self.prior_period)}. Data through {day_text(self.period.end)}."
        )


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _units(units: Units | None) -> dict[str, object] | None:
    return (
        None if units is None else {"kind": units.kind.value, "currency_code": units.currency_code}
    )


def _absence(absence: NewCustomersAbsence | None) -> dict[str, object] | None:
    return None if absence is None else {"reason": absence.reason.value, "text": absence.text}


def _figure(figure: NewCustomersFigure | None) -> dict[str, object] | None:
    if figure is None:
        return None
    return {
        "metric": figure.metric,
        "value": _decimal(figure.value),
        "units": _units(figure.units),
        "display_value": figure.display_value,
        "display_text": figure.display_text,
        "absence": _absence(figure.absence),
    }


def _change(change: ShareChange | NewCustomersAbsence) -> dict[str, object]:
    if isinstance(change, NewCustomersAbsence):
        return {"change": None, "absence": _absence(change)}
    return {"change": change_payload(change.change), "absence": None}


def _range(window: DateRange | None) -> dict[str, str] | None:
    return (
        None
        if window is None
        else {"start": window.start.isoformat(), "end": window.end.isoformat()}
    )


def _new_buyers(section: NewBuyers) -> dict[str, object]:
    return {
        "state": section.state.value,
        "text": section.text,
        "action": None
        if section.action is None
        else {"label": section.action.label, "preset": section.action.preset.value},
        "new_buyers": _figure(section.new_buyers),
        "cost_per_new_buyer": _figure(section.cost_per_new_buyer),
        "period": None if section.period is None else _range(section.period.window),
        "period_label": None if section.period is None else section.period.label,
        "expected_after": (
            None if section.expected_after is None else section.expected_after.isoformat()
        ),
        "latest_available_period": _range(section.latest_available_period),
        "new_buyers_basis": section.new_buyers_basis,
        "cost_basis": section.cost_basis,
        "per_channel": None if section.per_channel is None else _per_channel(section.per_channel),
    }


def _per_channel(table: PerChannel) -> dict[str, object]:
    listed = table.state is PerChannelState.ROWS
    return {
        "state": table.state.value,
        "text": table.text,
        "caption": PER_CHANNEL_CAPTION if listed else None,
        "caption_detail": (
            f"Each row names its channel group. {table.new_buyers_basis}" if listed else None
        ),
        "cost_header": PER_CHANNEL_COST_HEADER if listed else None,
        "cost_basis": f"Ad spend ÷ new buyers · {table.period.label}" if listed else None,
        "foot": PER_CHANNEL_FOOT if listed else None,
        "rows": [
            {
                "ad_product_key": row.ad_product_key,
                "ad_product_label": row.ad_product_label,
                "channel_group_label": row.channel_group_label,
                "new_buyers": _figure(row.new_buyers),
                "cost_per_new_buyer": _figure(row.cost_per_new_buyer),
            }
            for row in table.rows
        ],
        "note": table.note,
        "expected_after": (
            None if table.expected_after is None else table.expected_after.isoformat()
        ),
        "latest_available_period": _range(table.latest_available_period),
    }


def _gateway(entry: GatewayEntry) -> dict[str, object]:
    return {
        "ad_product_key": entry.ad_product_key,
        "channel_label": entry.channel_label,
        "state": entry.state.value,
        "text": entry.text,
        "caption_title": entry.caption_title,
        "captions": list(GATEWAY_CAPTIONS),
        "column_header": GATEWAY_COLUMN_HEADER,
        "rows": [
            {
                "asin": row.asin,
                "title": row.title,
                "ntb_purchases": row.ntb_purchases,
                "share_of_channel": format(row.share_of_channel, "f"),
                "share_text": row.share_text,
            }
            for row in entry.rows
        ],
        "more_count": entry.more_count or None,
        "more_text": entry.more_text,
        "first_covered_date": (
            None if entry.first_covered_date is None else entry.first_covered_date.isoformat()
        ),
    }


def _marketplace_text(artifact: NewCustomersArtifact, market: NewCustomersMarket) -> str | None:
    """Name a market's sections when the page shows two or more, else none (as the Scorecard).

    With one marketplace the page reads as the approved mockups; with two, every section of a
    market says which marketplace and currency it measures, so no table reads as merged.
    """
    if len(artifact.markets) < _SEVERAL_MARKETPLACES or market.marketplace_label is None:
        return None
    return f"{market.marketplace_label} marketplace \u00b7 {market.currency_code}"


def _named(label: str, marketplace_text: str | None) -> str:
    return label if marketplace_text is None else f"{label} \u00b7 {marketplace_text}"


def _awaiting_fact(slot: str, label: str, text: str | None) -> CoverageFact:
    if text is None:  # pragma: no cover — a pending or missing state carries its text
        raise ValueError("a pending or missing section carries its server text.")
    return CoverageFact(
        slot, label, "This period", unavailable=(text, _COVERAGE_RECOVERY), unsent=True
    )


def _market_facts(artifact: NewCustomersArtifact, market: NewCustomersMarket) -> list[CoverageFact]:
    """One market's coverage: step 1 per window, then new buyers, per channel and gateway."""
    shown = _marketplace_text(artifact, market)
    key = market.marketplace_key
    step_one = _named("New-to-brand purchases per channel", shown)
    facts = [
        CoverageFact(f"new-to-brand:{key}", step_one, "This window", CoverageKind.RATIO),
    ]
    if artifact.prior_period is not None:
        facts.append(
            CoverageFact(
                f"new-to-brand:{key}", step_one, "Prior window", CoverageKind.RATIO, role="prior"
            )
        )
    section = market.new_buyers
    buyers = _named("New buyers", shown)
    if section.state is NewBuyersState.FIGURES:
        facts.append(CoverageFact(f"new-buyers:{key}", buyers, "This period"))
    elif section.state in {NewBuyersState.PENDING, NewBuyersState.MISSING}:
        facts.append(_awaiting_fact(f"new-buyers:{key}", buyers, section.text))
    per_channel = section.per_channel
    if per_channel is not None:
        slot, label = f"new-buyers-per-channel:{key}", _named(PER_CHANNEL_CAPTION, shown)
        awaiting = per_channel.state in {PerChannelState.PENDING, PerChannelState.MISSING}
        facts.append(
            _awaiting_fact(slot, label, per_channel.text)
            if awaiting
            else CoverageFact(slot, label, "This period")
        )
    gateway = _named(_GATEWAY_SUBJECT, shown)
    uncovered = next(
        (entry for entry in market.gateway if entry.state is GatewayState.NOT_COVERED), None
    )
    if uncovered is None or uncovered.text is None:
        facts.append(CoverageFact(f"gateway-products:{key}", gateway, "This window"))
    else:
        # The sheet reads "{reason} {recovery}", so the reason drops the text's own recovery.
        reason = uncovered.text.removesuffix(f" {PACK_COVERAGE_RECOVERY}")
        facts.append(
            CoverageFact(
                f"gateway-products:{key}",
                gateway,
                "This window",
                unavailable=(reason, PACK_COVERAGE_RECOVERY),
                unsent=True,
            )
        )
    return facts


def _insight_card(
    artifact: NewCustomersArtifact, card: PortfolioAcquisitionCard
) -> dict[str, object]:
    """One card, named for its marketplace with two shown (as the Scorecard's cards are)."""
    market = next(item for item in artifact.markets if item.marketplace_key == card.marketplace_key)
    shown = _marketplace_text(artifact, market)
    return {
        "finding_id": NTB_SHARE_MOVER,
        "kind_label": INSIGHT_KIND_LABEL,
        "marketplace": card.marketplace_key,
        "ad_product_key": card.ad_product_key,
        "marketplace_text": shown,
        "headline": card.headline,
        "title": _named(card.headline, shown),
        "body": card.body,
    }


def _evidence_status(artifact: NewCustomersArtifact) -> dict[str, object] | None:
    """ADR-119's `EvidenceStatus.v2` over the artifact's window; none without sections.

    Step 1 is baked only over a window covered end to end, so it is complete for both windows;
    new buyers, new buyers per channel and gateway products are unavailable, with their server
    text as the reason, while their delivery hasn't landed or their coverage doesn't reach.
    """
    facts = [fact for market in artifact.markets for fact in _market_facts(artifact, market)]
    if not facts:
        return None
    return evidence_status_payload(write_evidence_status(artifact.period, tuple(facts)))


def new_customers_artifact_payload(artifact: NewCustomersArtifact) -> dict[str, object]:
    """Encode the artifact as canonical JSON-ready data; the browser decoder is strict."""
    labels = artifact.labels
    return {
        "type": NEW_CUSTOMERS_ARTIFACT_TYPE,
        "version": NEW_CUSTOMERS_ARTIFACT_VERSION,
        "outcome": artifact.outcome.value,
        # The browser never maps an outcome code to words (plan D17).
        "outcome_text": (
            None
            if artifact.outcome is NewCustomersOutcome.COMPLETE
            else NEW_CUSTOMERS_EMPTY_TEXT
            if artifact.outcome is NewCustomersOutcome.EMPTY
            else NEW_CUSTOMERS_INSUFFICIENT_TEXT
        ),
        "preset": artifact.preset.value,
        "period": _range(artifact.period),
        "prior_period": _range(artifact.prior_period),
        "period_label": artifact.period_label,
        "scope_text": artifact.scope_text,
        "markets": [
            {
                # Named `marketplace`, as the Scorecard names `channel`: the presentation envelope
                # refuses raw dimension-key fields (`marketplace_key`) anywhere in an artifact.
                "marketplace": market.marketplace_key,
                "currency_code": market.currency_code,
                "marketplace_text": _marketplace_text(artifact, market),
                "new_to_brand": {
                    "heading": "New-to-brand purchases",
                    "caption": "New-to-brand purchases per channel",
                    "caption_detail": (
                        "Each row names its channel group. "
                        f"{labels.step_one_caption(artifact.period_label)}"
                    ),
                    "share_basis": SHARE_BASIS,
                    "columns": [
                        {"column": column, "label": label} for column, label in STEP_ONE_COLUMNS
                    ],
                    "foot": STEP_ONE_FOOT,
                    "rows": [
                        {
                            "ad_product_key": row.ad_product_key,
                            "ad_product_label": row.ad_product_label,
                            "channel_group_label": row.channel_group_label,
                            "ntb_purchases": _figure(row.ntb_purchases),
                            "ntb_revenue": _figure(row.ntb_revenue),
                            "ntb_revenue_share": _figure(row.ntb_revenue_share),
                            "ntb_revenue_share_change": _change(row.ntb_revenue_share_change),
                            "cost_per_ntb_purchase": _figure(row.cost_per_ntb_purchase),
                        }
                        for row in market.new_to_brand
                    ],
                },
                "new_buyers": _new_buyers(market.new_buyers),
                "gateway": [_gateway(entry) for entry in market.gateway],
            }
            for market in artifact.markets
        ],
        "insights": insight_slots_payload(artifact.insights),
        "insight_cards": [_insight_card(artifact, card) for card in artifact.insight_cards],
        "page_text": dict(NEW_CUSTOMERS_PAGE_TEXT),
        "pointers": [
            {
                "order": pointer.order,
                "text": pointer.text,
                "lead": pointer.lead,
                "link_text": pointer.link_text,
                "tab_id": pointer.tab_id,
                "href": pointer.href,
            }
            for pointer in NEW_CUSTOMERS_POINTERS
        ],
        "evidence_status": _evidence_status(artifact),
        "page_context_label": artifact.page_context_label,
        "page_context_expires_at": None,
        "starter_questions": list(artifact.starter_questions),
        "boundary": list(NEW_CUSTOMERS_BOUNDARY),
    }


def new_customers_calendar_period(
    preset: TemporalPresetId, as_of: date, covered: tuple[DateRange, ...]
) -> ResolveCalendarPresetResult:
    """Resolve a calendar preset on daily coverage alone (LLD "Time policy", ADR-100).

    The anchors, step 1 and gateway products, read daily operands only; the step-2 section
    isn't an anchor, so a late new-buyers delivery shows its pending text for the period and
    never holds back the rest of the tab. The bake and the serve-time miss path both resolve
    through here, so they can't disagree about the period.
    """
    return resolve_calendar_preset(
        ResolveCalendarPresetRequest(
            preset=preset,
            as_of=as_of,
            sections=(
                PresetSection("new-to-brand", period_anchor=True),
                PresetSection("gateway-products", period_anchor=True),
                PresetSection("new-to-brand-step-2", period_anchor=False),
            ),
            publications=frozenset(),
            daily_coverage=(DailyOperandCoverage(NEW_CUSTOMERS_DAILY_OPERAND, covered),),
        )
    )


def _served_range(value: object) -> DateRange:
    window = cast("Mapping[str, str]", value)
    return DateRange(date.fromisoformat(window["start"]), date.fromisoformat(window["end"]))


def _optional_range(value: object) -> DateRange | None:
    return None if value is None else _served_range(value)


def _reason(entries: list[dict[str, object]], slot: str, text: str | None) -> None:
    """Point a section's coverage entry at its re-decided text: the sheet reads it verbatim.

    A re-decided section always has its current entry (the ADR-119 block lists every slot the
    bake wrote). A body without it is refused, so the section line and the coverage sheet can't
    name the same window two ways.
    """
    current = [entry for entry in entries if entry["slot"] == slot and entry["role"] == "current"]
    if len(current) != 1:
        raise ValueError("a re-decided section has exactly one current coverage entry.")
    current[0]["reason"] = text


def resolve_new_customers_payload(payload: Mapping[str, object], as_of: date) -> dict[str, object]:
    """Re-decide every baked pending section of a served body with the serving date (ADR-107).

    A baked `pending` new-buyers section, or per-channel table, turns `missing` the day after
    its own `expected_after`, with no rebake, and its coverage entry reads the new text; the
    result equals the body a bake on `as_of` would write. A rolling body has no such section.
    """
    served = copy.deepcopy(dict(payload))
    preset = TemporalPresetId(cast("str", served["preset"]))
    if not is_calendar_preset(preset):
        return served
    grain = calendar_preset_grain(preset)
    status = cast("dict[str, object] | None", served["evidence_status"])
    entries = [] if status is None else cast("list[dict[str, object]]", status["entries"])
    for market in cast("list[dict[str, object]]", served["markets"]):
        key = cast("str", market["marketplace"])
        section = cast("dict[str, object]", market["new_buyers"])
        awaiting = {NewBuyersState.PENDING.value, NewBuyersState.MISSING.value}
        if section["state"] in awaiting:
            window = _served_range(section["period"])
            resolved = resolve_new_buyers(
                NewBuyers(
                    state=NewBuyersState(cast("str", section["state"])),
                    text=cast("str", section["text"]),
                    period=CalendarPeriod(window.start, window.end, grain),
                    expected_after=date.fromisoformat(cast("str", section["expected_after"])),
                    latest_available_period=_optional_range(section["latest_available_period"]),
                ),
                as_of,
            )
            section["state"], section["text"] = resolved.state.value, resolved.text
            _reason(entries, f"new-buyers:{key}", resolved.text)
        table = cast("dict[str, object] | None", section["per_channel"])
        if table is not None and table["state"] in awaiting:
            window = _served_range(section["period"])
            resolved_table = resolve_per_channel(
                PerChannel(
                    PerChannelState(cast("str", table["state"])),
                    CalendarPeriod(window.start, window.end, grain),
                    text=cast("str", table["text"]),
                    expected_after=date.fromisoformat(cast("str", table["expected_after"])),
                    latest_available_period=_optional_range(table["latest_available_period"]),
                ),
                as_of,
            )
            table["state"], table["text"] = resolved_table.state.value, resolved_table.text
            _reason(entries, f"new-buyers-per-channel:{key}", resolved_table.text)
    return served
