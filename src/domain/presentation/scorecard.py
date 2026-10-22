# SPDX-License-Identifier: MIT-0
"""The per-network ROI Scorecard artifact, `NetworkScorecardArtifact` v1 (design §6.1, ADR-106).

The artifact is typed so the non-additive rule can't be broken by construction:

- Attributed outcomes live only on a channel row, inside one network card. No type here holds
  an attributed figure for more than one network, and the one figure above one channel is the
  network headline's deduplicated ROAS (`ScorecardHeadline`). The owner allowed it: its credit
  is deduplicated across DSP and sponsored ads under one attribution model, so each purchase
  counts once. So no total row, channel-group subtotal or cross-network attributed figure can
  be built.
- Spend may be totalled. A network card totals its own channels' spend in its one marketplace
  and currency, and a cross-network spend row totals network spend only inside one marketplace
  and one currency, and only when two or more advertising networks are loaded there.
- Every return figure carries a typed basis: its network, attribution basis and window.

The page's rendering, its route and its v1/v2 page entries belong to the UI build slice; this
module is the server contract it renders.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.alert_rules import AlertRule, AlertRuleFinding
from domain.presentation.calendar_presets import (
    DailyOperandCoverage,
    PresetSection,
    ResolveCalendarPresetRequest,
    ResolveCalendarPresetResult,
    resolve_calendar_preset,
)
from domain.presentation.change import Change, ChangeOperands, change_payload, check_change
from domain.presentation.currency_display import currency_display_value, metric_display_value
from domain.presentation.evidence_status import EvidenceStatus, evidence_status_payload
from domain.presentation.insights import (
    DeterministicFinding,
    DeterministicFindingRequest,
    FindingUnit,
    InsightAbsenceClass,
    InsightFindingKind,
    InsightFindingOperand,
    InsightOutputClass,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    OperandBasis,
    calculate_deterministic_findings,
    horizon_role_for,
    insight_slots_payload,
)
from domain.presentation.page_schemas import TemporalPresetId
from domain.shared_kernel.values import DateRange

__all__ = [
    "CHANNEL_FIGURE_MISSING_REASON",
    "CHANNEL_RETURN_NO_SPEND_REASON",
    "CHANNEL_TABLE_FOOT",
    "DEDUPLICATED_MISSING_REASON",
    "DEDUPLICATED_MODELS_DISAGREE_REASON",
    "DEDUPLICATED_WITHHELD_REASON",
    "MODEL_NOTE",
    "NETWORK_SCORECARD_ARTIFACT_TYPE",
    "NETWORK_SCORECARD_ARTIFACT_VERSION",
    "NOT_AVAILABLE",
    "NO_DEDUPLICATED_CREDIT_TEXT",
    "SCORECARD_BOUNDARY",
    "SCORECARD_MARKETPLACE_LABELS",
    "TACOS_NEEDS_VENDOR",
    "CrossNetworkSpendRow",
    "NetworkCard",
    "NetworkKey",
    "NetworkScorecardArtifact",
    "NetworkScorecardSlotRequest",
    "ScorecardAbsentChannel",
    "ScorecardBasis",
    "ScorecardBasisKind",
    "ScorecardChannelRow",
    "ScorecardComparison",
    "ScorecardFinding",
    "ScorecardFindingCandidate",
    "ScorecardFindingId",
    "ScorecardHeadline",
    "ScorecardModelValue",
    "ScorecardOutcome",
    "ScorecardRanking",
    "ScorecardRankingOutcome",
    "ScorecardRankingRow",
    "ScorecardTacosRow",
    "cross_network_spend_rows",
    "network_scorecard_artifact_payload",
    "network_scorecard_finding_candidates",
    "network_scorecard_slot",
    "network_spend_total",
    "ratio_display_value",
    "scorecard_marketplace_label",
    "scorecard_ranking",
]

NETWORK_SCORECARD_ARTIFACT_TYPE: Final = "NetworkScorecardArtifact"
NETWORK_SCORECARD_ARTIFACT_VERSION: Final = 1

SCORECARD_BOUNDARY: Final = (
    "Channel rows are Amazon-attributed: Amazon credits a purchase to each channel the shopper "
    "saw within 14 days, so one purchase can count in two rows.",
    "Deduplicated ROAS credits each purchase once, so it is the only return figure across "
    "channels.",
    "TACoS divides ad spend by Vendor Central shipped revenue, two measurement systems. When "
    "Vendor Central is late, TACoS shows when its data last arrived and the rest of the page "
    "keeps its window.",
)
"""The approved mockup's "About these figures" statements (slice 9a D13a)."""
BOUNDARY_HEADING: Final = "About these figures"
CHANNEL_TABLE_FOOT: Final = (
    "Attributed sales aren't added across channels, because one purchase can be credited to "
    "more than one channel."
)
MODEL_NOTE: Final = (
    "Every model credits each deduplicated purchase once, so the headline is the same under "
    "each. The model changes how that revenue splits across channels: the ranking below shows "
    "all three. Channel rows are attributed by Amazon and don't depend on a model."
)
HEADLINE_LABEL: Final = "Deduplicated ROAS"
SPEND_LABEL: Final = "Ad spend, all channels"
MODEL_SELECTOR_LABEL: Final = "Attribution model for deduplicated ROAS"
RANKING_HEADING: Final = "Does the ranking hold under every model?"
RANKING_CAPTION: Final = "Deduplicated ROAS and its rank per channel, under each attribution model"
RANKING_BY_DAY_LABEL: Final = "By day"
RANKING_SENSITIVE_TEXT: Final = "The leading channel changes with the attribution model."
RANKING_WITHHELD_TEXT: Final = (
    "Not available: Amazon withheld the deduplicated figure for this window, so channels can't "
    "be ranked under each model."
)
RANKING_MISSING_TEXT: Final = (
    "Not available: the deduplicated figure for this window hasn't arrived yet, so channels "
    "can't be ranked under each model."
)
RANKING_MODELS_DISAGREE_TEXT: Final = (
    "Not available: the attribution models credited different deduplicated totals for this "
    "window, so channels can't be ranked under each model."
)
NO_DEDUPLICATED_CREDIT_TEXT: Final = "No deduplicated credit"
"""A spending channel's ranking cell under a model that credited it nothing (owner, 2026-10-22).

Its governed 0.00 stays in the artifact as its value, but the cell names the absence of credit
and states no rank, because a tie at zero read like a broken second place beside the channel
table's attributed ROAS."""
RANKING_NO_SPEND_LEAD: Final = "Not available:"
RANKING_NO_SPEND_TAIL: Final = (
    "had no ad spend in this window, so channels can't be ranked under each model."
)
"""The ranking's answer when a channel's deduplicated ROAS has no spend to divide (G9A-1):
"Not available: {channels} had no ad spend in this window, so channels can't be ranked under
each model." No rank is stated for any channel, so no winner is concluded from part of them."""
CHANNEL_TABLE_HEADING: Final = "By channel"
CHANNEL_TABLE_CAPTION: Final = "Spend and attributed return per channel"
CHANNEL_LINK_COLUMN: Final = "Campaigns"
CROSS_NETWORK_LABEL: Final = "Spend across networks"
CROSS_NETWORK_REASON: Final = (
    "A total across networks appears when a second advertising network is loaded in this "
    "marketplace."
)
TACOS_LABEL: Final = "TACoS"
RETAIL_SALES_HEADING: Final = "Ad spend and retail sales"
INSIGHTS_HEADING: Final = "Insights"
EMPTY_TEXT: Final = (
    "No ad spend was delivered in this window. Choose another window to see the Scorecard."
)
CALENDAR_SCOPE_NOTE: Final = "No comparison for calendar periods."
SCORECARD_COMPARED_METRICS: Final = frozenset(
    {"ad_spend", "roas_attributed_14d", "roas_deduplicated_incl_halo_14d", "tacos"}
)
"""The figures the Scorecard serves with a change chip (ADR-121): spend, channel ROAS, the
deduplicated headline and TACoS. Attributed revenue and ROAS with halo show no chip."""
TACOS_NEEDS_VENDOR: Final = "Needs Vendor Central sales"
NOT_AVAILABLE: Final = "Not available"
DEDUPLICATED_WITHHELD_REASON: Final = (
    "Amazon withheld the deduplicated figure for this window because too few shoppers were "
    "matched, or none."
)
"""The ADR-035 reason: availability requires an unsuppressed deduplicated delivery."""
DEDUPLICATED_MISSING_REASON: Final = "The deduplicated figure for this window hasn't arrived yet."
DEDUPLICATED_MODELS_DISAGREE_REASON: Final = (
    "The attribution models credited different deduplicated totals for this window."
)
"""Why the ranking isn't stated when the complete headline differs by model (bake guard).

Every model credits each deduplicated purchase once, so the governed AMC query can't produce
this; malformed input can (the rotating dev feed found 2026-10-22), and no leader is concluded
from it."""
CHANNEL_RETURN_NO_SPEND_REASON: Final = (
    "No ad spend on this channel in this window, so there's no spend to divide its return by."
)
"""Why a channel's ROAS is absent: a ratio over zero spend is undefined (gates F2, G9A-2).

The missing side is the divisor: the row's attributed revenue can still show, because 14-day
attribution credits revenue to spend that fell outside the window."""
CHANNEL_FIGURE_MISSING_REASON: Final = "This channel's figure for this window hasn't arrived yet."
"""Why any other channel figure is absent: its row didn't arrive for a covered window."""
ROW_HEADER_LABEL: Final = "Channel"
HIGHLIGHT_TAG: Final = "This campaign's channel"
"""The tag on the row a campaign's step highlights (LLD "Arrived from campaign detail")."""

SCORECARD_MARKETPLACE_LABELS: Final[Mapping[str, str]] = {"amazon_us": "US"}
"""The short marketplace name the card header and captions use (slice 9a D13c, the approved
mockup's "Amazon · US marketplace · USD"). A marketplace without one uses its governed display
name."""

_DSP_CHANNEL: Final = "amazon_dsp"
_DSP_ABSENT: Final = "No DSP delivery in this window"
_SEVERAL_NETWORKS: Final = 2
_SEVERAL_MARKETPLACES: Final = 2
_MAX_CARDS: Final = 3
_MAX_ALSO_CHANGED: Final = 5


class ScorecardOutcome(StrEnum):
    """The artifact's closed outcomes, as the v2 page entry declares them.

    The source bakes `empty` when no channel delivers and `complete` otherwise. A partial or
    withheld channel delivery bakes nothing, so no input yields an `insufficient` body and the
    page declares none (SC9A-GATE-R5-001). A ranking's `insufficient` is a different, reachable
    outcome (`ScorecardRankingOutcome`).
    """

    COMPLETE = "complete"
    EMPTY = "empty"


class ScorecardBasisKind(StrEnum):
    """What a figure measures, which decides the words of its basis label."""

    SPEND = "spend"
    NETWORK_SPEND = "network_spend"
    """A network's spend added across its channels, the one additive figure above a channel."""
    ATTRIBUTED = "attributed"
    ATTRIBUTED_INCL_HALO = "attributed_incl_halo"
    DEDUPLICATED = "deduplicated"
    MIXED = "mixed"


_ATTRIBUTED_KINDS: Final = frozenset(
    {
        ScorecardBasisKind.ATTRIBUTED,
        ScorecardBasisKind.ATTRIBUTED_INCL_HALO,
        ScorecardBasisKind.DEDUPLICATED,
    }
)


@dataclass(frozen=True, slots=True)
class ScorecardBasis:
    """A figure's typed basis: network, attribution basis, model and window (BR-3).

    The window is required on every attributed figure, so a return figure can't be built
    without it. The label text is built from these fields, never stored apart from them.
    """

    kind: ScorecardBasisKind
    network_label: str
    window_days: int | None = None
    model_label: str | None = None

    def __post_init__(self) -> None:
        if not self.network_label.strip():
            raise ValueError("a scorecard basis names its network.")
        attributed = self.kind in _ATTRIBUTED_KINDS
        if attributed != (self.window_days is not None):
            raise ValueError("an attributed scorecard figure carries its window, and only it.")
        if self.window_days is not None and self.window_days < 1:
            raise ValueError("an attribution window is a positive number of days.")
        if (self.kind is ScorecardBasisKind.DEDUPLICATED) != (self.model_label is not None):
            raise ValueError("a deduplicated figure names its model, and only it.")

    @property
    def text(self) -> str:
        """The server-built basis label (Scorecard LLD "Basis labels")."""
        days = f"{self.window_days}-day"
        if self.kind is ScorecardBasisKind.ATTRIBUTED:
            return (
                f"{self.network_label}-attributed · {days} · conversion date · excludes brand halo"
            )
        if self.kind is ScorecardBasisKind.ATTRIBUTED_INCL_HALO:
            return f"{self.network_label}-attributed · {days} · includes brand halo"
        if self.kind is ScorecardBasisKind.DEDUPLICATED:
            return (
                f"Deduplicated across DSP and sponsored · {self.model_label} · {days} · "
                "includes brand halo"
            )
        if self.kind is ScorecardBasisKind.MIXED:
            return "Mixed basis: ad spend ÷ Vendor shipped revenue"
        if self.kind is ScorecardBasisKind.NETWORK_SPEND:
            return f"{self.network_label} Ads · spend across DSP and sponsored"
        return f"{self.network_label} ad spend"


@dataclass(frozen=True, slots=True)
class ScorecardComparison:
    """One figure in the window, its equal prior when the preset compares, or a typed absence."""

    metric: str
    current: Decimal | None
    prior: Decimal | None
    units: Units | None
    basis: ScorecardBasis
    absence_reason: str | None = None
    change: Change | None = None
    """The server change block (ADR-121), on a compared figure with a prior only."""

    def __post_init__(self) -> None:
        if not self.metric.strip():
            raise ValueError("a scorecard figure names its metric.")
        if self.change is not None:
            if self.current is None or self.prior is None or self.units is None:
                raise ValueError("only a figure with a prior carries a change.")
            check_change(self.change, ChangeOperands(self.current, self.prior, self.units))
        if (self.current is None) != (self.absence_reason is not None):
            raise ValueError("a scorecard figure carries a value or a typed absence, not both.")
        if self.current is None and self.prior is not None:
            raise ValueError("an absent scorecard figure carries no prior value.")
        if (self.current is None) != (self.units is None):
            raise ValueError("a scorecard figure carries units exactly when it has a value.")
        for value in (self.current, self.prior):
            if value is not None and not value.is_finite():
                raise ValueError("scorecard figures are finite.")

    @classmethod
    def absent(cls, metric: str, basis: ScorecardBasis, reason: str) -> ScorecardComparison:
        """Build the typed absence of one figure."""
        return cls(metric, None, None, None, basis, reason)


@dataclass(frozen=True, slots=True)
class NetworkKey:
    """One advertising network in one marketplace: the scorecard's isolation unit (ADR-106)."""

    ecosystem_key: str
    platform_key: str
    marketplace_key: str

    def __post_init__(self) -> None:
        if not all(
            value.strip() for value in (self.ecosystem_key, self.platform_key, self.marketplace_key)
        ):
            raise ValueError("a scorecard network key is fully named.")


@dataclass(frozen=True, slots=True)
class ScorecardChannelRow:
    """One governed channel inside one network card. Never totalled with another row."""

    channel_key: str
    channel_label: str
    spend: ScorecardComparison
    attributed_revenue: ScorecardComparison
    roas: ScorecardComparison
    roas_incl_halo: ScorecardComparison

    def __post_init__(self) -> None:
        if not self.channel_key.strip() or not self.channel_label.strip():
            raise ValueError("a scorecard channel row names its channel.")
        expected = (
            ("ad_spend", ScorecardBasisKind.SPEND),
            ("attributed_revenue_14d", ScorecardBasisKind.ATTRIBUTED),
            ("roas_attributed_14d", ScorecardBasisKind.ATTRIBUTED),
            ("roas_attributed_incl_halo_14d", ScorecardBasisKind.ATTRIBUTED_INCL_HALO),
        )
        figures = (self.spend, self.attributed_revenue, self.roas, self.roas_incl_halo)
        if tuple((item.metric, item.basis.kind) for item in figures) != expected:
            raise ValueError("a scorecard channel row carries its four declared figures in order.")
        if self.spend.current is None:
            raise ValueError("a scorecard channel row exists only for a channel with delivery.")


@dataclass(frozen=True, slots=True)
class ScorecardModelValue:
    """One registered model's deduplicated ROAS, with its dense rank inside one network."""

    model_key: str
    model_label: str
    value: Decimal | None
    rank: int | None = None
    prior: Decimal | None = None
    """The headline's equal-prior value on a rolling preset; ranking values carry none."""
    change: Change | None = None

    def __post_init__(self) -> None:
        if not self.model_key.strip() or not self.model_label.strip():
            raise ValueError("a scorecard model value names its model.")
        if self.rank is not None and (self.value is None or self.rank < 1):
            raise ValueError("a scorecard rank starts at one and needs a value.")
        if self.value is None and self.prior is not None:
            raise ValueError("an absent model value carries no prior.")
        if self.change is not None:
            if self.value is None or self.prior is None:
                raise ValueError("only a model value with a prior carries a change.")
            check_change(self.change, ChangeOperands(self.value, self.prior, Units(UnitKind.RATIO)))


@dataclass(frozen=True, slots=True)
class ScorecardHeadline:
    """The network headline: deduplicated ROAS under every registered model, one network only."""

    values: tuple[ScorecardModelValue, ...]
    default_model_key: str
    basis: tuple[ScorecardBasis, ...]
    absence_reason: str | None = None

    def __post_init__(self) -> None:
        keys = tuple(item.model_key for item in self.values)
        if not keys or len(set(keys)) != len(keys) or self.default_model_key not in keys:
            raise ValueError("a scorecard headline carries every model once, default included.")
        if tuple(item.model_label for item in self.values) != tuple(
            item.model_label for item in self.basis
        ) or any(item.kind is not ScorecardBasisKind.DEDUPLICATED for item in self.basis):
            raise ValueError("each headline model carries its deduplicated basis.")
        withheld = any(item.value is None for item in self.values)
        if withheld != (self.absence_reason is not None):
            raise ValueError("a scorecard headline is complete or carries its absence reason.")


class ScorecardRankingOutcome(StrEnum):
    """Whether the channel ranking holds under every model (the retiring module's states)."""

    ROBUST_LEADER = "robust_leader"
    MODEL_SENSITIVE = "model_sensitive"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True, slots=True)
class ScorecardRankingRow:
    """One channel's deduplicated ROAS and rank under each model, and its spend (LLD table).

    `spend` is the channel's window spend in the card's currency, the same figure as its
    channel row, so the ranking table needs no join.
    """

    channel_key: str
    channel_label: str
    spend: Decimal
    values: tuple[ScorecardModelValue, ...]


@dataclass(frozen=True, slots=True)
class ScorecardRanking:
    """The answer to "does the ranking hold under every model?", inside one network."""

    outcome: ScorecardRankingOutcome
    rows: tuple[ScorecardRankingRow, ...]
    agreed_leader_key: str | None
    absence_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.agreed_leader_key is not None) != (
            self.outcome is ScorecardRankingOutcome.ROBUST_LEADER
        ):
            raise ValueError("only a robust ranking names one agreed leader.")
        if (self.outcome is ScorecardRankingOutcome.INSUFFICIENT) != (
            self.absence_reason is not None
        ):
            raise ValueError("only an insufficient ranking carries an absence reason.")
        stated = self.outcome is not ScorecardRankingOutcome.INSUFFICIENT
        for row in self.rows:
            for item in row.values:
                if stated and (item.value is None or (item.rank is None) != (item.value == 0)):
                    raise ValueError("a stated ranking ranks every credited value and no zero.")
                if not stated and item.rank is not None:
                    raise ValueError("an insufficient ranking ranks no channel.")


@dataclass(frozen=True, slots=True)
class ScorecardAbsentChannel:
    """A governed channel of this network with no delivery in the window."""

    channel_key: str
    channel_label: str

    @property
    def text(self) -> str:
        """The line a delivering-channel table shows for this absent channel."""
        return f"No {self.channel_label} delivery in this window."


@dataclass(frozen=True, slots=True)
class NetworkCard:
    """One network in one marketplace and currency. Nothing on it is converted or shared."""

    network: NetworkKey
    network_label: str
    currency_code: str
    network_spend: ScorecardComparison
    headline: ScorecardHeadline
    ranking: ScorecardRanking
    channel_rows: tuple[ScorecardChannelRow, ...]
    absent_channels: tuple[ScorecardAbsentChannel, ...]
    marketplace_label: str
    """The governed marketplace display name the card header names."""

    def __post_init__(self) -> None:
        if (
            not self.network_label.strip()
            or not self.currency_code.strip()
            or not self.marketplace_label.strip()
        ):
            raise ValueError("a network card names its network, marketplace and currency.")
        keys = tuple(row.channel_key for row in self.channel_rows)
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("a network card carries each delivering channel once.")
        if set(keys) & {item.channel_key for item in self.absent_channels}:
            raise ValueError("a channel is delivering or absent, never both.")
        for row in self.channel_rows:
            for figure in (row.spend, row.attributed_revenue, row.roas, row.roas_incl_halo):
                if figure.basis.network_label != self.network_label:
                    raise ValueError("a channel figure's basis names its own network.")
                units = figure.units
                if (
                    units is not None
                    and units.kind is UnitKind.CURRENCY
                    and units.currency_code != self.currency_code
                ):
                    raise ValueError("a network card is never converted across currencies.")
        expected = network_spend_total(self.channel_rows, self.currency_code)
        if (self.network_spend.current, self.network_spend.prior) != expected:
            raise ValueError("network spend is exactly its own channels' spend.")
        ranked = {row.channel_key: row.spend for row in self.ranking.rows}
        if ranked != {row.channel_key: row.spend.current for row in self.channel_rows}:
            raise ValueError("a ranking row carries its own channel row's spend.")

    @property
    def dsp_absence_text(self) -> str | None:
        """The card line when this network has a DSP channel with no delivery."""
        absent = {item.channel_key for item in self.absent_channels}
        return _DSP_ABSENT if _DSP_CHANNEL in absent else None


def network_spend_total(
    rows: tuple[ScorecardChannelRow, ...], currency_code: str
) -> tuple[Decimal, Decimal | None]:
    """Total spend across one network's channels in one currency (`ad_spend` is additive).

    Spend is the only figure that adds. A row in another currency is refused, so a total
    never converts. The prior total exists only when every row has a prior.
    """
    if not rows:
        raise ValueError("a spend total needs at least one channel row.")
    for row in rows:
        units = row.spend.units
        if units is None or units.kind is not UnitKind.CURRENCY:
            raise ValueError("a spend total adds currency figures only.")
        if units.currency_code != currency_code:
            raise ValueError("a spend total never adds two currencies.")
    current = sum((row.spend.current or Decimal(0) for row in rows), Decimal(0))
    priors = tuple(row.spend.prior for row in rows)
    prior = (
        None
        if any(value is None for value in priors)
        else sum((value or Decimal(0) for value in priors), Decimal(0))
    )
    return current, prior


@dataclass(frozen=True, slots=True)
class ScorecardTacosRow:
    """`tacos` at ecosystem x marketplace: a mixed-basis ratio, labelled as such."""

    ecosystem_key: str
    marketplace_key: str
    tacos: ScorecardComparison

    def __post_init__(self) -> None:
        if self.tacos.metric != "tacos" or self.tacos.basis.kind is not ScorecardBasisKind.MIXED:
            raise ValueError("a TACoS row carries tacos on its mixed basis.")


@dataclass(frozen=True, slots=True)
class CrossNetworkSpendRow:
    """Spend across advertising networks inside one marketplace and one currency (AC-A1.8).

    Figures appear only when two or more advertising networks are loaded there; otherwise
    the row is the single line "Networks loaded: {names}". Spend is the only figure here.
    """

    marketplace_key: str
    currency_code: str
    platform_keys: tuple[str, ...]
    networks_loaded: tuple[str, ...]
    spend: Decimal | None
    prior_spend: Decimal | None

    def __post_init__(self) -> None:
        if (
            not self.platform_keys
            or len(set(self.platform_keys)) != len(self.platform_keys)
            or len(self.networks_loaded) != len(self.platform_keys)
        ):
            raise ValueError("a cross-network spend row names each loaded network once.")
        several = len(self.platform_keys) >= _SEVERAL_NETWORKS
        if several != (self.spend is not None):
            raise ValueError("cross-network spend figures appear only with two or more networks.")
        if self.spend is None and self.prior_spend is not None:
            raise ValueError("a single-network spend row carries no figure.")

    @property
    def text(self) -> str | None:
        """The single line shown when only one advertising network is loaded."""
        return (
            None
            if self.spend is not None
            else f"Networks loaded: {', '.join(self.networks_loaded)}"
        )


def cross_network_spend_rows(cards: tuple[NetworkCard, ...]) -> tuple[CrossNetworkSpendRow, ...]:
    """Group network spend by marketplace and currency; never one row across either.

    Two marketplaces in one currency give two rows, and one marketplace with networks in two
    currencies gives two rows too. Attributed figures never enter this function.
    """
    groups: dict[tuple[str, str], list[NetworkCard]] = {}
    for card in cards:
        groups.setdefault((card.network.marketplace_key, card.currency_code), []).append(card)
    rows: list[CrossNetworkSpendRow] = []
    for (marketplace, currency), members in sorted(groups.items()):
        ordered = sorted(members, key=lambda card: card.network.platform_key)
        names = tuple(card.network_label for card in ordered)
        platforms = tuple(card.network.platform_key for card in ordered)
        if len(ordered) < _SEVERAL_NETWORKS:
            rows.append(CrossNetworkSpendRow(marketplace, currency, platforms, names, None, None))
            continue
        current = sum((card.network_spend.current or Decimal(0) for card in ordered), Decimal(0))
        priors = tuple(card.network_spend.prior for card in ordered)
        prior = (
            None
            if any(value is None for value in priors)
            else sum((value or Decimal(0) for value in priors), Decimal(0))
        )
        rows.append(CrossNetworkSpendRow(marketplace, currency, platforms, names, current, prior))
    return tuple(rows)


def scorecard_ranking(
    rows: tuple[tuple[str, str, Decimal, Mapping[str, Decimal | None]], ...],
    models: tuple[tuple[str, str], ...],
    absence_reason: str | None,
) -> ScorecardRanking:
    """Rank one network's channels under each model; robust only with one agreed leader.

    ``rows`` are ``(channel_key, channel_label, spend, {model_key: value})``. A missing value,
    or a withheld delivery, makes the ranking insufficient: no winner is concluded from part
    of it. Spend only travels with its row; it never ranks. A zero value is a channel the model
    credited nothing: it keeps its value and takes no rank (`NO_DEDUPLICATED_CREDIT_TEXT`).
    """
    missing = (
        absence_reason is not None
        or not rows
        or any(values.get(key) is None for _, _, _, values in rows for key, _ in models)
    )
    if missing:
        unranked = tuple(
            ScorecardRankingRow(
                channel,
                label,
                spend,
                tuple(ScorecardModelValue(key, name, values.get(key)) for key, name in models),
            )
            for channel, label, spend, values in rows
        )
        return ScorecardRanking(
            ScorecardRankingOutcome.INSUFFICIENT,
            unranked,
            None,
            absence_reason or DEDUPLICATED_WITHHELD_REASON,
        )
    ranks: dict[str, dict[str, int]] = {}
    leaders: list[tuple[str, ...]] = []
    for key, _ in models:
        values = {channel: values[key] for channel, _, _, values in rows}
        credited = {
            channel: value for channel, value in values.items() if value is not None and value != 0
        }
        distinct = sorted(set(credited.values()), reverse=True)
        ranks[key] = {channel: distinct.index(value) + 1 for channel, value in credited.items()}
        leaders.append(tuple(sorted(c for c, rank in ranks[key].items() if rank == 1)))
    ranked = tuple(
        ScorecardRankingRow(
            channel,
            label,
            spend,
            tuple(
                ScorecardModelValue(key, name, values[key], ranks[key].get(channel))
                for key, name in models
            ),
        )
        for channel, label, spend, values in rows
    )
    agreed = {leader[0] for leader in leaders if len(leader) == 1}
    if all(len(leader) == 1 for leader in leaders) and len(agreed) == 1:
        return ScorecardRanking(ScorecardRankingOutcome.ROBUST_LEADER, ranked, agreed.pop())
    return ScorecardRanking(ScorecardRankingOutcome.MODEL_SENSITIVE, ranked, None)


class ScorecardFindingId(StrEnum):
    """The `network-scorecard` v1 deterministic findings, each inside one network and channel."""

    CHANNEL_SHARE_OF_NETWORK_SPEND = "channel-share-of-network-spend"
    CHANNEL_ROAS_MOVER = "channel-roas-mover"
    CHANNEL_SPEND_MOVER = "channel-spend-mover"
    """A channel's spend change inside its network. Baked for the governed alert rules
    ("Also changed"); never narrated, because spend is neutral and its share already is."""


_FINDING_KINDS: Final = {
    ScorecardFindingId.CHANNEL_SHARE_OF_NETWORK_SPEND: InsightFindingKind.SHARE_OF_TOTAL,
    ScorecardFindingId.CHANNEL_ROAS_MOVER: InsightFindingKind.PERCENTAGE_CHANGE,
    ScorecardFindingId.CHANNEL_SPEND_MOVER: InsightFindingKind.PERCENTAGE_CHANGE,
}
_FINDING_METRICS: Final = {
    ScorecardFindingId.CHANNEL_SHARE_OF_NETWORK_SPEND: "ad_spend",
    ScorecardFindingId.CHANNEL_ROAS_MOVER: "roas_attributed_14d",
    ScorecardFindingId.CHANNEL_SPEND_MOVER: "ad_spend",
}
_NARRATED_FINDINGS: Final = frozenset(
    {ScorecardFindingId.CHANNEL_SHARE_OF_NETWORK_SPEND, ScorecardFindingId.CHANNEL_ROAS_MOVER}
)


@dataclass(frozen=True, slots=True)
class ScorecardFindingCandidate:
    """A computed finding before the contract's materiality is applied (by the adapter)."""

    finding_id: ScorecardFindingId
    network: NetworkKey
    network_label: str
    channel_key: str
    channel_label: str
    finding: DeterministicFinding
    prior: Decimal | None

    @property
    def kind(self) -> InsightFindingKind:
        """The insight finding kind this scorecard finding is computed as."""
        return _FINDING_KINDS[self.finding_id]

    @property
    def metric_id(self) -> str:
        """The dictionary metric this finding measures."""
        return _FINDING_METRICS[self.finding_id]


@dataclass(frozen=True, slots=True)
class ScorecardFinding:
    """One baked deterministic finding, with whether it met the contract's materiality."""

    finding_id: ScorecardFindingId
    network: NetworkKey
    channel_key: str
    value: Decimal
    material: bool


def network_scorecard_finding_candidates(
    cards: tuple[NetworkCard, ...],
) -> tuple[ScorecardFindingCandidate, ...]:
    """Compute each channel's spend share and ROAS move, each inside its own network card.

    The share divides by the card's own network spend, never a cross-network total. The ROAS
    move needs a nonzero prior, so a calendar preset (no comparison) yields no mover.
    """
    found: list[ScorecardFindingCandidate] = []
    for card in cards:
        total = card.network_spend.current
        names = {
            row.channel_key: f"{row.channel_label} share of {card.network_label} network spend"
            for row in card.channel_rows
        }
        if total is not None and total > 0:
            shares = calculate_deterministic_findings(
                DeterministicFindingRequest(
                    InsightFindingKind.SHARE_OF_TOTAL,
                    tuple(
                        InsightFindingOperand(
                            names[row.channel_key], row.spend.current or Decimal(0)
                        )
                        for row in card.channel_rows
                    ),
                    total=total,
                )
            )
            for row, finding in zip(card.channel_rows, shares, strict=True):
                found.append(
                    ScorecardFindingCandidate(
                        ScorecardFindingId.CHANNEL_SHARE_OF_NETWORK_SPEND,
                        card.network,
                        card.network_label,
                        row.channel_key,
                        row.channel_label,
                        finding,
                        None,
                    )
                )
        for row in card.channel_rows:
            spent, spent_before = row.spend.current, row.spend.prior
            if spent is not None and spent_before is not None and spent_before != 0:
                (moved,) = calculate_deterministic_findings(
                    DeterministicFindingRequest(
                        InsightFindingKind.PERCENTAGE_CHANGE,
                        (
                            InsightFindingOperand(
                                f"{row.channel_label} ad spend change", spent, spent_before
                            ),
                        ),
                    )
                )
                found.append(
                    ScorecardFindingCandidate(
                        ScorecardFindingId.CHANNEL_SPEND_MOVER,
                        card.network,
                        card.network_label,
                        row.channel_key,
                        row.channel_label,
                        moved,
                        spent_before,
                    )
                )
            current, prior = row.roas.current, row.roas.prior
            if current is None or prior is None or prior == 0:
                continue
            (mover,) = calculate_deterministic_findings(
                DeterministicFindingRequest(
                    InsightFindingKind.PERCENTAGE_CHANGE,
                    (
                        InsightFindingOperand(
                            f"{row.channel_label} attributed ROAS change", current, prior
                        ),
                    ),
                )
            )
            found.append(
                ScorecardFindingCandidate(
                    ScorecardFindingId.CHANNEL_ROAS_MOVER,
                    card.network,
                    card.network_label,
                    row.channel_key,
                    row.channel_label,
                    mover,
                    prior,
                )
            )
    return tuple(found)


def _sentence(candidate: ScorecardFindingCandidate) -> str:
    value = candidate.finding.value
    where = f"inside the {candidate.network_label} network in {candidate.network.marketplace_key}"
    if candidate.finding_id is ScorecardFindingId.CHANNEL_SHARE_OF_NETWORK_SPEND:
        return f"{candidate.channel_label} holds {value}% of ad spend {where}."
    direction = "rose" if value > 0 else "fell"
    return (
        f"{candidate.channel_label} attributed ROAS {direction} {abs(value)}% against the "
        f"prior window, {where}."
    )


@dataclass(frozen=True, slots=True)
class NetworkScorecardSlotRequest:
    """The material findings, the artifact revision and the contract's silence condition."""

    material: tuple[ScorecardFindingCandidate, ...]
    artifact_revision: str
    silence_reason: str


def scorecard_narrated(
    material: tuple[ScorecardFindingCandidate, ...],
) -> tuple[ScorecardFindingCandidate, ...]:
    """Choose the at most three findings the slot and its cards narrate.

    Material ROAS moves come first, then the largest spend shares. A spend move is never
    narrated: it feeds the governed alert rules ("Also changed") only.
    """
    return tuple(
        sorted(
            (item for item in material if item.finding_id in _NARRATED_FINDINGS),
            key=lambda item: (
                item.finding_id is not ScorecardFindingId.CHANNEL_ROAS_MOVER,
                -abs(item.finding.value),
                item.network.marketplace_key,
                item.channel_key,
            ),
        )[:3]
    )


def network_scorecard_slot(request: NetworkScorecardSlotRequest) -> InsightSlot:
    """Render the deterministic `network-scorecard` slot; no prompt or model is involved.

    Each sentence names its channel and its network. With no material finding the slot is
    silent with the contract's silence condition. The slot carries at most three sentences,
    the narration bound, choosing material ROAS moves first, then the largest shares.
    """
    ordered = scorecard_narrated(request.material)
    if not ordered:
        return InsightSlot(
            InsightSlotId.NETWORK_SCORECARD,
            InsightOutputClass.OBSERVED,
            InsightSlotState.SILENT,
            None,
            None,
            request.artifact_revision,
            "deterministic-v1",
            absence_class=InsightAbsenceClass.EVALUATED_QUIET,
            absence_reason=request.silence_reason,
        )
    return InsightSlot(
        InsightSlotId.NETWORK_SCORECARD,
        InsightOutputClass.OBSERVED,
        InsightSlotState.FILLED,
        " ".join(_sentence(item) for item in ordered),
        "Server-computed channel shares of network spend and channel ROAS moves.",
        request.artifact_revision,
        "deterministic-v1",
    )


ALSO_CHANGED_HEADING: Final = "Also changed"
ALSO_CHANGED_INTRO: Final = (
    "Other changes in this window that crossed an alert threshold. Changes the insights above "
    "already explain aren't repeated."
)
_CARD_KIND_LABELS: Final = {
    ScorecardFindingId.CHANNEL_ROAS_MOVER: "Channel mover",
    ScorecardFindingId.CHANNEL_SHARE_OF_NETWORK_SPEND: "Spend share",
}


@dataclass(frozen=True, slots=True)
class ScorecardInsightCard:
    """One narrated finding as an insight card: its kind, headline and one-line body."""

    finding_id: ScorecardFindingId
    network: NetworkKey
    channel_key: str
    kind_label: str
    headline: str
    body: str


@dataclass(frozen=True, slots=True)
class AlsoChangedRow:
    """One direction-only "Also changed" row: a finding a governed rule lists (ADR-108)."""

    rule_id: str
    finding_id: ScorecardFindingId
    network: NetworkKey
    channel_key: str
    label: str
    direction_text: str
    window_text: str
    scope_text: str


@dataclass(frozen=True, slots=True)
class NetworkScorecardArtifact:
    """`NetworkScorecardArtifact` v1. Rendering-free: every text is a server field."""

    outcome: ScorecardOutcome
    period: DateRange
    prior_period: DateRange | None
    cards: tuple[NetworkCard, ...]
    tacos_rows: tuple[ScorecardTacosRow, ...]
    cross_network_spend: tuple[CrossNetworkSpendRow, ...]
    findings: tuple[ScorecardFinding, ...]
    insights: tuple[InsightSlot, ...]
    preset_label: str
    """The served preset's version-2 label ("7 days", "Latest measured month")."""
    period_label: str | None = None
    """A calendar preset's server-built period label; None on a rolling preset."""
    evidence_status: EvidenceStatus | None = None
    """ADR-119 coverage of every served figure; on a complete body only."""
    insight_cards: tuple[ScorecardInsightCard, ...] = ()
    """The narrated findings as cards, in the slot's order (at most three)."""
    also_changed: tuple[AlsoChangedRow, ...] = ()
    """At most five direction-only rows over governed thresholds, none already narrated."""

    def __post_init__(self) -> None:  # noqa: PLR0912 -- one check per artifact invariant
        if not self.preset_label.strip():
            raise ValueError("a scorecard names the preset it serves.")
        if self.period_label is not None and self.prior_period is not None:
            raise ValueError("a calendar period on the Scorecard compares nothing.")
        if (self.outcome is ScorecardOutcome.COMPLETE) != (self.evidence_status is not None):
            raise ValueError("a complete scorecard, and only it, carries its coverage block.")
        if self.prior_period is not None and self.prior_period.end >= self.period.start:
            raise ValueError("a scorecard prior window ends before its window starts.")
        networks = tuple(card.network for card in self.cards)
        if len(set(networks)) != len(networks):
            raise ValueError("a scorecard carries one card per network and marketplace.")
        complete = self.outcome is ScorecardOutcome.COMPLETE
        if complete != bool(self.cards):
            raise ValueError("only a complete scorecard carries network cards.")
        if self.cross_network_spend != cross_network_spend_rows(self.cards):
            raise ValueError("cross-network spend rows are derived from the cards alone.")
        for finding in self.findings:
            if finding.network not in networks:
                raise ValueError("a scorecard finding belongs to one of its network cards.")
        if tuple(slot.id for slot in self.insights) != (InsightSlotId.NETWORK_SCORECARD,):
            raise ValueError("the scorecard insight roster is network-scorecard v1.")
        tacos = tuple((row.ecosystem_key, row.marketplace_key) for row in self.tacos_rows)
        if len(set(tacos)) != len(tacos):
            raise ValueError("a scorecard carries one TACoS row per ecosystem and marketplace.")
        if len(self.insight_cards) > _MAX_CARDS or len(self.also_changed) > _MAX_ALSO_CHANGED:
            raise ValueError("a scorecard narrates at most three cards and lists five changes.")
        narrated = {
            (card.finding_id, card.network, card.channel_key) for card in self.insight_cards
        }
        if any(
            (row.finding_id, row.network, row.channel_key) in narrated for row in self.also_changed
        ):
            raise ValueError("an Also changed row never repeats a narrated finding.")


_MONTHS: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_ORDINAL_SUFFIX: Final = {1: "st", 2: "nd", 3: "rd"}
_TEEN_LOW: Final = 11
_TEEN_HIGH: Final = 13
_DECADE: Final = 10
_HUNDRED: Final = 100
_COUNT_WORDS: Final = {2: "both", 3: "all three", 4: "all four", 5: "all five"}
_EN_DASH: Final = "\u2013"
_SEPARATOR: Final = "\u203a"


def day_text(value: date) -> str:
    """Write a day as the page does: "Sep 15, 2026"."""
    return f"{_MONTHS[value.month - 1]} {value.day}, {value.year}"


def range_text(window: DateRange, *, with_year: bool = True) -> str:
    """Write a window as the page does, with an en dash: "Sep 9-15, 2026" or "Sep 2-8"."""
    start, end = window.start, window.end
    first = f"{_MONTHS[start.month - 1]} {start.day}"
    if start == end:
        last = ""
    elif (start.year, start.month) == (end.year, end.month):
        last = f"{_EN_DASH}{end.day}"
    else:
        last = f"{_EN_DASH}{_MONTHS[end.month - 1]} {end.day}"
    if start.year != end.year:
        return f"{first}, {start.year}{last}, {end.year}"
    return f"{first}{last}, {end.year}" if with_year else f"{first}{last}"


def ordinal_text(rank: int) -> str:
    """Write a dense rank as the ranking table does: "1st", "2nd", "3rd", "11th"."""
    if _TEEN_LOW <= rank % _HUNDRED <= _TEEN_HIGH:
        return f"{rank}th"
    return f"{rank}{_ORDINAL_SUFFIX.get(rank % _DECADE, 'th')}"


def scope_text(artifact: NetworkScorecardArtifact) -> str:
    """Write the scope band: the window, its comparison and how far the data reaches."""
    if artifact.period_label is not None:
        return f"Showing {artifact.period_label} ({artifact.preset_label}). {CALENDAR_SCOPE_NOTE}"
    shown = f"Showing {range_text(artifact.period)} ({artifact.preset_label})"
    if artifact.prior_period is not None:
        shown += f" compared with {range_text(artifact.prior_period, with_year=False)}"
    return f"{shown}. Data through {day_text(artifact.period.end)}."


def ranking_outcome_text(card: NetworkCard) -> str:
    """Write the ranking's one-sentence answer from its outcome and the card's channels."""
    ranking = card.ranking
    if ranking.outcome is ScorecardRankingOutcome.INSUFFICIENT:
        if ranking.absence_reason == CHANNEL_RETURN_NO_SPEND_REASON:
            unspent = [row.channel_label for row in ranking.rows if row.spend == 0]
            return f"{RANKING_NO_SPEND_LEAD} {_listed(unspent)} {RANKING_NO_SPEND_TAIL}"
        if ranking.absence_reason == DEDUPLICATED_MODELS_DISAGREE_REASON:
            return RANKING_MODELS_DISAGREE_TEXT
        return (
            RANKING_WITHHELD_TEXT
            if ranking.absence_reason == DEDUPLICATED_WITHHELD_REASON
            else RANKING_MISSING_TEXT
        )
    if ranking.outcome is ScorecardRankingOutcome.MODEL_SENSITIVE:
        return RANKING_SENSITIVE_TEXT
    leader = next(
        row.channel_label for row in ranking.rows if row.channel_key == ranking.agreed_leader_key
    )
    count = len(card.headline.values)
    if count == 1:
        return f"{leader} leads under the one registered model."
    return f"{leader} leads under {_COUNT_WORDS.get(count, f'all {count}')} models."


def _listed(names: list[str]) -> str:
    """Join channel names as a sentence does: "A", "A and B", "A, B and C"."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def scorecard_marketplace_label(marketplace_key: str, governed: str) -> str:
    """Name the marketplace in a card header and caption: the short name, else the governed one."""
    return SCORECARD_MARKETPLACE_LABELS.get(marketplace_key, governed)


def card_id(network: NetworkKey) -> str:
    """Return the card's stable identity on the page, built from its governed key values."""
    return f"{network.ecosystem_key}-{network.platform_key}-{network.marketplace_key}"


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _format(units: Units | None, metric: str) -> str | None:
    """Name how the browser shows the value: money, a multiplier or a percent."""
    if units is None:
        return None
    if units.kind is UnitKind.CURRENCY:
        return "currency"
    return "percent" if metric == "tacos" else "multiplier"


def _units(units: Units | None) -> dict[str, object] | None:
    return (
        None if units is None else {"kind": units.kind.value, "currency_code": units.currency_code}
    )


def _shown(units: Units | None, value: Decimal | None, metric: str) -> str | None:
    """Return the served display value: money and multipliers at two places, a percent none.

    Money follows ADR-084. A Scorecard multiplier (ROAS) takes the same two-place derivation,
    because plan S26 and the approved mockups show "3.86x" and the browser never rounds
    (G9A-4). TACoS is a rate the shared percent formatter shows, so it carries none.
    """
    if units is None or value is None:
        return None
    if _format(units, metric) == "multiplier":
        return ratio_display_value(value)
    return metric_display_value(units, value)


def ratio_display_value(value: Decimal) -> str:
    """Return a Scorecard multiplier's served two-place display value ("3.9600" -> "3.96")."""
    return currency_display_value(value)


def _figure(
    item: ScorecardComparison, label: str, prior_period: DateRange | None
) -> dict[str, object]:
    """One served figure: exact value, display value, prior, change, basis and absence text."""
    prior_label = (
        None
        if item.prior is None or prior_period is None
        else range_text(prior_period, with_year=False)
    )
    return {
        "metric": item.metric,
        "label": label,
        "format": _format(item.units, item.metric),
        "units": _units(item.units),
        "value": _decimal(item.current),
        "display_value": _shown(item.units, item.current, item.metric),
        "prior_value": _decimal(item.prior),
        "prior_display_value": _shown(item.units, item.prior, item.metric),
        "prior_label": prior_label,
        "change": change_payload(item.change),
        "basis": item.basis.text,
        "absence_text": item.absence_reason,
    }


def _headline_figure(
    card: NetworkCard, index: int, prior_period: DateRange | None
) -> dict[str, object]:
    value = card.headline.values[index]
    basis = card.headline.basis[index]
    units = None if value.value is None else Units(UnitKind.RATIO)
    return _figure(
        ScorecardComparison(
            "roas_deduplicated_incl_halo_14d",
            value.value,
            value.prior,
            units,
            basis,
            None if value.value is not None else card.headline.absence_reason,
            value.change,
        ),
        HEADLINE_LABEL,
        prior_period,
    )


_CHANNEL_COLUMNS: Final = (
    ("ad_spend", "Ad spend"),
    ("attributed_revenue_14d", "Attributed revenue"),
    ("roas_attributed_14d", "ROAS"),
    ("roas_attributed_incl_halo_14d", "ROAS with brand halo"),
)


def _card(card: NetworkCard, artifact: NetworkScorecardArtifact) -> dict[str, object]:
    prior = artifact.prior_period
    period = artifact.period_label or range_text(artifact.period)
    default = card.headline.default_model_key
    first_row = card.channel_rows[0]
    networks = next(
        (
            row.text
            for row in artifact.cross_network_spend
            if row.marketplace_key == card.network.marketplace_key
            and row.currency_code == card.currency_code
        ),
        None,
    )
    return {
        "card_id": card_id(card.network),
        "title": (
            f"{card.network_label} · {card.marketplace_label} marketplace · {card.currency_code}"
        ),
        "networks_text": networks,
        "currency_code": card.currency_code,
        "dsp_absence_text": card.dsp_absence_text,
        "headline": {
            "label": HEADLINE_LABEL,
            "selector_label": MODEL_SELECTOR_LABEL,
            "model_note": MODEL_NOTE,
            "default_model": default,
            "absence_text": card.headline.absence_reason,
            "models": [
                {
                    "model": value.model_key,
                    "label": value.model_label,
                    "option_label": (
                        f"{value.model_label} (default)"
                        if value.model_key == default
                        else value.model_label
                    ),
                    "figure": _headline_figure(card, index, prior),
                }
                for index, value in enumerate(card.headline.values)
            ],
        },
        "spend": _figure(card.network_spend, SPEND_LABEL, prior),
        "ranking": {
            "heading": RANKING_HEADING,
            "row_header_label": ROW_HEADER_LABEL,
            "outcome": card.ranking.outcome.value,
            "outcome_text": ranking_outcome_text(card),
            "caption": RANKING_CAPTION,
            "caption_detail": (
                f"Deduplicated across DSP and sponsored · {first_row.roas.basis.window_days}-day"
                f" · includes brand halo · {period}"
            ),
            "by_day_label": RANKING_BY_DAY_LABEL,
            "foot": (
                "Each model credits each deduplicated purchase once, across the ads the shopper "
                f"saw. Ranks compare channels inside the {card.network_label} network. This "
                "shows how much the ranking depends on the model, not which channel caused sales."
            ),
            "models": [
                {
                    "model": value.model_key,
                    "label": (
                        f"{value.model_label} (default)"
                        if value.model_key == default
                        else value.model_label
                    ),
                }
                for value in card.headline.values
            ],
            "rows": [
                {
                    "channel": row.channel_key,
                    "channel_label": row.channel_label,
                    "spend_display_value": metric_display_value(
                        Units(UnitKind.CURRENCY, card.currency_code), row.spend
                    ),
                    "values": [
                        {
                            "model": item.model_key,
                            "value": _decimal(item.value),
                            "display_value": None
                            if item.value is None
                            else ratio_display_value(item.value),
                            "rank": item.rank,
                            "rank_text": None if item.rank is None else ordinal_text(item.rank),
                            # The governed zero stays as the value; the cell names it instead.
                            "credit_text": NO_DEDUPLICATED_CREDIT_TEXT if item.value == 0 else None,
                        }
                        for item in row.values
                    ],
                }
                for row in card.ranking.rows
            ],
        },
        "channel_table": {
            "heading": CHANNEL_TABLE_HEADING,
            "fresh_text": (
                f"{card.network_label} Ads data through {day_text(artifact.period.end)}."
            ),
            "caption": CHANNEL_TABLE_CAPTION,
            "caption_detail": f"{card.network_label} · {card.marketplace_label} · {period}",
            "row_header_label": ROW_HEADER_LABEL,
            "highlight_tag": HIGHLIGHT_TAG,
            # With two marketplaces the arrival tag names the marketplace its row measures,
            # the same served words as the retail rows; with one it names none (SC9A-GATE-001).
            "marketplace_text": _marketplace_text(artifact.cards, card.network.marketplace_key),
            "link_column": CHANNEL_LINK_COLUMN,
            "columns": [
                {
                    "column": metric,
                    "label": label,
                    "basis": None if metric == "ad_spend" else figure.basis.text,
                }
                for (metric, label), figure in zip(
                    _CHANNEL_COLUMNS,
                    (
                        first_row.spend,
                        first_row.attributed_revenue,
                        first_row.roas,
                        first_row.roas_incl_halo,
                    ),
                    strict=True,
                )
            ],
            "rows": [
                {
                    "channel": row.channel_key,
                    "channel_label": row.channel_label,
                    "figures": [
                        _figure(item, label, prior)
                        for (_, label), item in zip(
                            _CHANNEL_COLUMNS,
                            (row.spend, row.attributed_revenue, row.roas, row.roas_incl_halo),
                            strict=True,
                        )
                    ],
                    "link": None,
                }
                for row in card.channel_rows
            ],
            "foot": CHANNEL_TABLE_FOOT,
            "absent_lines": [item.text for item in card.absent_channels],
        },
    }


def _tacos(
    row: ScorecardTacosRow, cards: tuple[NetworkCard, ...], prior: DateRange | None
) -> dict[str, object]:
    owner = next(
        card
        for card in cards
        if (card.network.ecosystem_key, card.network.marketplace_key)
        == (row.ecosystem_key, row.marketplace_key)
    )
    return {
        "card_id": card_id(owner.network),
        "coverage_slot": f"tacos:{row.ecosystem_key}:{row.marketplace_key}",
        "marketplace_text": _marketplace_text(cards, row.marketplace_key),
        "figure": _figure(row.tacos, TACOS_LABEL, prior),
        "link": None,
    }


def _marketplace_text(
    cards: tuple[NetworkCard, ...], marketplace_key: str, currency_code: str | None = None
) -> str | None:
    """Name a row's marketplace when the page shows more than one (G9A-6), else none.

    With one marketplace the page reads as the approved mockup; with two, each TACoS and spend
    row, insight card and Also changed row says which marketplace (and, for spend, currency) it
    measures, so none reads as merged or as a page-level fact (SC9A-GATE-001).
    """
    if len({card.network.marketplace_key for card in cards}) < _SEVERAL_MARKETPLACES:
        return None
    label = next(
        card.marketplace_label for card in cards if card.network.marketplace_key == marketplace_key
    )
    shown = f"{label} marketplace"
    return shown if currency_code is None else f"{shown} · {currency_code}"


def _named(text: str, marketplace_text: str | None) -> str:
    """Name an insight card or Also changed row: its own words, plus its marketplace when shown.

    The name is the card's accessible name and its dialog heading, so two cards that read alike
    in two marketplaces never share one (SC9A-GATE-001).
    """
    return text if marketplace_text is None else f"{text} \u00b7 {marketplace_text}"


def _cross_network(
    row: CrossNetworkSpendRow, cards: tuple[NetworkCard, ...], prior: DateRange | None
) -> dict[str, object]:
    figure = (
        None
        if row.spend is None
        else _figure(
            ScorecardComparison(
                "ad_spend",
                row.spend,
                row.prior_spend,
                Units(UnitKind.CURRENCY, row.currency_code),
                ScorecardBasis(ScorecardBasisKind.SPEND, " and ".join(row.networks_loaded)),
            ),
            CROSS_NETWORK_LABEL,
            prior,
        )
    )
    return {
        "row_id": f"{row.marketplace_key}-{row.currency_code}",
        "label": CROSS_NETWORK_LABEL,
        "marketplace_text": _marketplace_text(cards, row.marketplace_key, row.currency_code),
        "currency_code": row.currency_code,
        "networks_loaded": list(row.networks_loaded),
        "figure": figure,
        "text": row.text,
        "reason": None if figure is not None else CROSS_NETWORK_REASON,
    }


def network_scorecard_artifact_payload(artifact: NetworkScorecardArtifact) -> dict[str, object]:
    """Encode the artifact as the browser renders it; the browser decoder is strict.

    Every text is a server field and every figure carries its basis label. No key here names
    a canonical selector (`channel_key`, `marketplace_key`, `platform_key`): a channel is its
    governed id under `channel`, a card its `card_id`. Link texts are `null` in the bake and
    built when the page is served, from the target page's pointer.
    """
    prior = artifact.prior_period
    cards = artifact.cards
    return {
        "type": NETWORK_SCORECARD_ARTIFACT_TYPE,
        "version": NETWORK_SCORECARD_ARTIFACT_VERSION,
        "outcome": artifact.outcome.value,
        "outcome_text": None if artifact.outcome is ScorecardOutcome.COMPLETE else EMPTY_TEXT,
        "period": {
            "start": artifact.period.start.isoformat(),
            "end": artifact.period.end.isoformat(),
        },
        "prior_period": None
        if prior is None
        else {"start": prior.start.isoformat(), "end": prior.end.isoformat()},
        "preset_label": artifact.preset_label,
        "period_label": artifact.period_label,
        "not_available_text": NOT_AVAILABLE,
        "scope_text": scope_text(artifact),
        "cards": [_card(card, artifact) for card in cards],
        "insights_heading": INSIGHTS_HEADING,
        "retail_sales_heading": RETAIL_SALES_HEADING,
        "tacos": [_tacos(row, cards, prior) for row in artifact.tacos_rows],
        "cross_network_spend": [
            _cross_network(row, cards, prior) for row in artifact.cross_network_spend
        ],
        "findings": [
            {
                "finding_id": item.finding_id.value,
                "card_id": card_id(item.network),
                "channel": item.channel_key,
                "value": _decimal(item.value),
                "material": item.material,
            }
            for item in artifact.findings
        ],
        "insights": insight_slots_payload(artifact.insights),
        "insight_cards": [
            {
                "finding_id": card.finding_id.value,
                "card_id": card_id(card.network),
                "channel": card.channel_key,
                "kind_label": card.kind_label,
                # With two marketplaces, each card says whose figures it narrates, as the
                # retail rows do; with one it names none (SC9A-GATE-001).
                "marketplace_text": _marketplace_text(cards, card.network.marketplace_key),
                "headline": card.headline,
                "title": _named(
                    card.headline, _marketplace_text(cards, card.network.marketplace_key)
                ),
                "body": card.body,
                "next_step": None,
            }
            for card in artifact.insight_cards
        ],
        "also_changed": None
        if not artifact.also_changed
        else {
            "heading": ALSO_CHANGED_HEADING,
            "intro": ALSO_CHANGED_INTRO,
            "rows": [
                {
                    "rule_id": row.rule_id,
                    "finding_id": row.finding_id.value,
                    "card_id": card_id(row.network),
                    "channel": row.channel_key,
                    "label": row.label,
                    "marketplace_text": _marketplace_text(cards, row.network.marketplace_key),
                    "title": _named(
                        row.label, _marketplace_text(cards, row.network.marketplace_key)
                    ),
                    "direction_text": row.direction_text,
                    "window_text": row.window_text,
                    "scope_text": row.scope_text,
                    # The row's insight dialog narration, written here so the browser
                    # composes no sentence from served parts (G9A-3).
                    "dialog_text": (
                        f"{row.direction_text} {row.window_text} \u00b7 {row.scope_text}"
                    ),
                }
                for row in artifact.also_changed
            ],
        },
        "pointers": [
            {"tab_id": tab_id, "lead": lead, "link_text": link, "href": href}
            for tab_id, lead, link, href in POINTER_LINES
        ],
        "boundary_heading": BOUNDARY_HEADING,
        "boundary": list(SCORECARD_BOUNDARY),
        "evidence_status": evidence_status_payload(artifact.evidence_status),
    }


POINTER_LINES: Final = (
    (
        "new-customers",
        "New-to-brand customers and gateway products are on",
        f"Customers {_SEPARATOR} New customers",
        "/new-customers/",
    ),
    ("simulator", "Try a budget move in", "Budget simulator", "/simulator/"),
)
"""The governed pointer lines (LLD "Pointer lines"); each renders only when the session's
navigation manifest lists its tab, so neither shows before its page ships."""


SCORECARD_DAILY_OPERAND: Final = "scorecard-spend-and-attributed-conversions"
"""The one daily operand a Scorecard calendar preset resolves on: the coverage that gates a
bake (spend and attributed conversions). TACoS degrades to its typed absence instead."""


def scorecard_calendar_period(
    preset: TemporalPresetId, as_of: date, covered: tuple[DateRange, ...]
) -> ResolveCalendarPresetResult:
    """Resolve a Scorecard calendar preset on daily coverage alone (Scorecard LLD, ADR-100).

    With no periodic anchor left on the page, the latest complete ISO week or calendar month
    ending on or before `as_of` whose every day of spend and attributed conversions is covered
    is the period; `none` names when the first is expected. The bake and the serve-time miss
    path both resolve through here, so they can't disagree about the period.
    """
    return resolve_calendar_preset(
        ResolveCalendarPresetRequest(
            preset=preset,
            as_of=as_of,
            sections=(PresetSection("network-card", period_anchor=True),),
            publications=frozenset(),
            daily_coverage=(DailyOperandCoverage(SCORECARD_DAILY_OPERAND, covered),),
        )
    )


def _moved(current: Decimal, prior: Decimal) -> str:
    return "rose" if current > prior else "fell" if current < prior else "held"


def _whole(value: Decimal) -> str:
    return str(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _card_row(
    cards: tuple[NetworkCard, ...], network: NetworkKey, channel: str
) -> tuple[NetworkCard, ScorecardChannelRow]:
    card = next(item for item in cards if item.network == network)
    return card, next(row for row in card.channel_rows if row.channel_key == channel)


def scorecard_insight_cards(
    narrated: tuple[ScorecardFindingCandidate, ...],
    cards: tuple[NetworkCard, ...],
    prior_period: DateRange | None,
) -> tuple[ScorecardInsightCard, ...]:
    """Write each narrated finding as a card, every word from its own channel's figures."""
    built: list[ScorecardInsightCard] = []
    for item in narrated:
        card, row = _card_row(cards, item.network, item.channel_key)
        channel = row.channel_label
        if item.finding_id is ScorecardFindingId.CHANNEL_ROAS_MOVER:
            current, prior = row.roas.current, row.roas.prior
            if current is None or prior is None:
                continue
            headline = f"{channel} ROAS {_moved(current, prior)} {abs(current - prior):.2f}\u00d7"
            spent, before = row.spend.current, row.spend.prior
            revenue, revenue_before = row.attributed_revenue.current, row.attributed_revenue.prior
            if spent is None or not before or revenue is None or revenue_before is None:
                body = f"Inside {channel} only."
            else:
                change = abs((spent - before) / before * 100).quantize(
                    Decimal("0.1"), rounding=ROUND_HALF_UP
                )
                body = (
                    f"Spend {_moved(spent, before)} {change}% while attributed revenue "
                    f"{_moved(revenue, revenue_before)}, inside {channel} only."
                )
        else:
            headline = f"{channel} took {_whole(item.finding.value)}% of {card.network_label} spend"
            total_before = card.network_spend.prior
            before = row.spend.prior
            if prior_period is None or not total_before or before is None:
                body = f"Inside the {card.network_label} network."
            else:
                share_before = before / total_before * 100
                shown, shown_before = _whole(item.finding.value), _whole(share_before)
                direction = (
                    "Unchanged"
                    if shown == shown_before
                    else "Up"
                    if item.finding.value > share_before
                    else "Down"
                )
                body = (
                    f"{direction} from {shown_before}% in "
                    f"{range_text(prior_period, with_year=False)}, inside the "
                    f"{card.network_label} network."
                )
        built.append(
            ScorecardInsightCard(
                item.finding_id,
                item.network,
                item.channel_key,
                _CARD_KIND_LABELS[item.finding_id],
                headline,
                body,
            )
        )
    return tuple(built)


def also_changed_rows(  # noqa: PLR0913, PLR0917 -- the window's findings, rules and narration
    candidates: tuple[ScorecardFindingCandidate, ...],
    rules: tuple[AlertRule, ...],
    narrated: tuple[ScorecardFindingCandidate, ...],
    period: DateRange,
    prior_period: DateRange | None,
) -> tuple[AlsoChangedRow, ...]:
    """List at most five findings a governed rule meets, none a card already narrates.

    Rows are direction only. Rules at another horizon than the window's list nothing, and a
    window with no comparison (a calendar period) has no movers to list.
    """
    if prior_period is None:
        return ()
    horizon = horizon_role_for(period.days)
    skipped = {(item.finding_id, item.network, item.channel_key) for item in narrated}
    window = f"{range_text(period, with_year=False)} vs {range_text(prior_period, with_year=False)}"
    rows: list[AlsoChangedRow] = []
    for rule in rules:
        for item in candidates:
            scope = rule.scope
            if (
                item.finding_id.value != rule.finding_id
                or (item.finding_id, item.network, item.channel_key) in skipped
                or (scope.ecosystem, scope.platform)
                != (
                    item.network.ecosystem_key,
                    item.network.platform_key,
                )
                or (scope.channel is not None and scope.channel != item.channel_key)
                or not rule.met_by(item.finding.value, horizon)
            ):
                continue
            noun = (
                "ROAS" if item.finding_id is ScorecardFindingId.CHANNEL_ROAS_MOVER else "ad spend"
            )
            rows.append(
                AlsoChangedRow(
                    rule.rule_id,
                    item.finding_id,
                    item.network,
                    item.channel_key,
                    f"{item.channel_label} {noun}",
                    "Up" if item.finding.value > 0 else "Down",
                    window,
                    f"inside {item.channel_label} only",
                )
            )
            skipped.add((item.finding_id, item.network, item.channel_key))
    return tuple(rows[:_MAX_ALSO_CHANGED])


SCORECARD_ALERT_FINDINGS: Final = (
    AlertRuleFinding(
        ScorecardFindingId.CHANNEL_ROAS_MOVER.value,
        _FINDING_KINDS[ScorecardFindingId.CHANNEL_ROAS_MOVER],
        FindingUnit.PERCENT,
        OperandBasis.CHANNEL_WINDOW,
        "roas_attributed_14d",
    ),
    AlertRuleFinding(
        ScorecardFindingId.CHANNEL_SPEND_MOVER.value,
        _FINDING_KINDS[ScorecardFindingId.CHANNEL_SPEND_MOVER],
        FindingUnit.PERCENT,
        OperandBasis.CHANNEL_WINDOW,
        "ad_spend",
    ),
    AlertRuleFinding(
        ScorecardFindingId.CHANNEL_SHARE_OF_NETWORK_SPEND.value,
        _FINDING_KINDS[ScorecardFindingId.CHANNEL_SHARE_OF_NETWORK_SPEND],
        FindingUnit.SHARE,
        OperandBasis.CHANNEL_WINDOW,
        "ad_spend",
    ),
)
"""The findings a governed alert rule may read from `network-scorecard`, with their units."""
