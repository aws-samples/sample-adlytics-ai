# SPDX-License-Identifier: MIT-0
# ruff: noqa: PLR0912, PLR0913
"""Closed V2 campaign-portfolio parent artifact and exact browser-safe codec."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, cast

from domain.measurement.comparison import absolute_change, percentage_change
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.campaign_attribution import (
    CampaignListEvidence,
    CampaignNumber,
    CampaignSelectionCapability,
    SafeCampaignDisplaySignature,
    safe_campaign_display_signature_from_payload,
    safe_campaign_display_signature_payload,
)
from domain.presentation.change import (
    CHANGE_KEY,
    PERCENTAGE_KEY,
    Change,
    ChangeOperands,
    ChangeSemantics,
    build_change,
    change_from_payload,
    check_change,
    comparison_change_payload,
    comparison_fields,
    require_change_blocks,
)
from domain.presentation.currency_display import (
    DISPLAY_VALUE_KEY,
    canonical_matches,
    carries_display_value,
    display_value_mode,
    metric_display_value,
)
from domain.presentation.evidence_status import (
    EVIDENCE_STATUS_KEY,
    CoverageFact,
    CoverageKind,
    EvidenceStatus,
    evidence_status_from_payload,
    evidence_status_payload,
    labels_for,
    require_entries,
    withheld_slots,
    write_evidence_status,
)
from domain.presentation.insights import (
    InsightSlot,
    InsightSlotId,
    default_portfolio_insights,
    insight_slots_from_payload,
    insight_slots_payload,
    require_insight_roster,
)
from domain.presentation.preset_artifacts import PresetArtifactSiblingIdentity
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange, Grain

CAMPAIGN_PORTFOLIO_ARTIFACT_TYPE = "CampaignPortfolioArtifact"
CAMPAIGN_PORTFOLIO_ARTIFACT_VERSION = 1
CAMPAIGN_PORTFOLIO_MAX_ROWS = 50
CAMPAIGN_PORTFOLIO_ROW_GRAIN = Grain(("period", "campaign", "marketplace"))
CAMPAIGN_PORTFOLIO_TOTAL_GRAIN = Grain(("period", "marketplace"))
CAMPAIGN_PORTFOLIO_TREND_GRAIN = Grain(("date", "marketplace"))


class CampaignPortfolioOutcome(StrEnum):
    """Closed parent outcomes; insufficient never substitutes zeroes."""

    COMPLETE = "complete"
    EMPTY = "empty"
    INSUFFICIENT = "insufficient"


class CampaignPortfolioSortField(StrEnum):
    """The only server-owned table comparators (ADR-071 adds the display name)."""

    SPEND = "spend"
    ATTRIBUTED_ROAS = "attributed_roas"
    PURCHASES = "purchases"
    CTR = "ctr"
    NAME = "name"


class CampaignPortfolioInsufficiencyReason(StrEnum):
    """Closed, ordered blockers for a portfolio artifact with no display data."""

    MISSING_CATALOG = "missing_catalog"
    INCOMPATIBLE_SCOPE = "incompatible_scope"


class CampaignPortfolioMetric(StrEnum):
    """Metric identifiers fixed by the command-center artifact contract."""

    SPEND = "spend"
    ATTRIBUTED_ROAS = "attributed_roas"
    PURCHASES = "purchases"
    CTR = "ctr"
    IMPRESSIONS = "impressions"
    PORTFOLIO_ROAS = "portfolio_roas_attributed_14d"
    CLICKS = "clicks"
    CPC = "cpc"
    CPM = "cpm"
    VIEWABLE_RATE = "viewable_rate"


_ROW_METRICS = (
    CampaignPortfolioMetric.SPEND,
    CampaignPortfolioMetric.ATTRIBUTED_ROAS,
    CampaignPortfolioMetric.PURCHASES,
    CampaignPortfolioMetric.CTR,
)
_TOTAL_METRICS = (
    CampaignPortfolioMetric.SPEND,
    CampaignPortfolioMetric.PURCHASES,
    CampaignPortfolioMetric.IMPRESSIONS,
    CampaignPortfolioMetric.PORTFOLIO_ROAS,
)
_MULTI_CHANNEL_TOTAL_METRICS = (
    CampaignPortfolioMetric.SPEND,
    CampaignPortfolioMetric.IMPRESSIONS,
)
"""The totals a multi-channel option shows: neither is attributed, so both add up (ADR-127)."""
_NOT_ADDED_METRICS = (
    CampaignPortfolioMetric.PURCHASES,
    CampaignPortfolioMetric.PORTFOLIO_ROAS,
)
"""The attributed totals a multi-channel option never adds across channels (ADR-127 d3)."""
_DELIVERY_METRICS = (
    CampaignPortfolioMetric.CLICKS,
    CampaignPortfolioMetric.CTR,
    CampaignPortfolioMetric.CPC,
    CampaignPortfolioMetric.CPM,
)
_TREND_METRICS = (
    CampaignPortfolioMetric.SPEND,
    CampaignPortfolioMetric.PORTFOLIO_ROAS,
    CampaignPortfolioMetric.PURCHASES,
)
CAMPAIGN_PORTFOLIO_GOVERNED_METRICS: Final[Mapping[CampaignPortfolioMetric, str]] = {
    CampaignPortfolioMetric.SPEND: "ad_spend",
    CampaignPortfolioMetric.ATTRIBUTED_ROAS: "roas_attributed_14d",
    CampaignPortfolioMetric.PURCHASES: "purchases",
    CampaignPortfolioMetric.CTR: "ctr",
    CampaignPortfolioMetric.IMPRESSIONS: "impressions",
    CampaignPortfolioMetric.PORTFOLIO_ROAS: "portfolio_roas_attributed_14d",
    CampaignPortfolioMetric.CLICKS: "clicks",
    CampaignPortfolioMetric.CPC: "cpc",
    CampaignPortfolioMetric.CPM: "cpm",
    CampaignPortfolioMetric.VIEWABLE_RATE: "viewable_rate",
}
"""The metric-dictionary entry each wire metric id measures, for its change semantics."""
_INSUFFICIENCY_REASON_ORDER = tuple(CampaignPortfolioInsufficiencyReason)
_METRIC_UNIT_KINDS = {
    CampaignPortfolioMetric.SPEND: UnitKind.CURRENCY,
    CampaignPortfolioMetric.ATTRIBUTED_ROAS: UnitKind.RATIO,
    CampaignPortfolioMetric.PURCHASES: UnitKind.COUNT,
    CampaignPortfolioMetric.CTR: UnitKind.RATIO,
    CampaignPortfolioMetric.IMPRESSIONS: UnitKind.COUNT,
    CampaignPortfolioMetric.PORTFOLIO_ROAS: UnitKind.RATIO,
    CampaignPortfolioMetric.CLICKS: UnitKind.COUNT,
    CampaignPortfolioMetric.CPC: UnitKind.CURRENCY,
    CampaignPortfolioMetric.CPM: UnitKind.CURRENCY,
    CampaignPortfolioMetric.VIEWABLE_RATE: UnitKind.RATIO,
}
_QUOTIENT_METRICS = frozenset(
    {
        CampaignPortfolioMetric.ATTRIBUTED_ROAS,
        CampaignPortfolioMetric.CTR,
        CampaignPortfolioMetric.PORTFOLIO_ROAS,
        CampaignPortfolioMetric.CPC,
        CampaignPortfolioMetric.CPM,
        CampaignPortfolioMetric.VIEWABLE_RATE,
    }
)
"""Metrics computed as a quotient: over a zero denominator each is typed absent (ADR-087)."""
CAMPAIGN_PORTFOLIO_CHANNEL_GRAIN = Grain(("period", "channel", "marketplace"))


def _previous_period(period: DateRange) -> DateRange:
    end = period.start - timedelta(days=1)
    return DateRange(end - timedelta(days=period.days - 1), end)


def _number(value: CampaignNumber, label: str) -> None:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric.")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError(f"{label} must be finite.")
    if value < 0:
        raise ValueError(f"{label} cannot be negative.")


def _signed_number(value: CampaignNumber, label: str) -> None:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric.")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError(f"{label} must be finite.")


def _number_payload(value: CampaignNumber) -> int | str:
    return value if isinstance(value, int) else str(value)


def _ordered_insufficiency_reasons(values: tuple[object, ...]) -> None:
    if (
        any(not isinstance(reason, CampaignPortfolioInsufficiencyReason) for reason in values)
        or len(set(values)) != len(values)
        or values != tuple(reason for reason in _INSUFFICIENCY_REASON_ORDER if reason in values)
    ):
        raise ValueError("campaign portfolio reason codes must be unique and canonical.")


@dataclass(frozen=True, slots=True)
class CampaignPortfolioSort:
    """Exact requested or default server sort; the browser has no comparator."""

    field: CampaignPortfolioSortField
    direction: str = "desc"

    def __post_init__(self) -> None:
        if self.direction not in {"asc", "desc"}:
            raise ValueError("campaign portfolio sort direction is unsupported.")


@dataclass(frozen=True, slots=True)
class CampaignPortfolioMetricValue:
    """A represented server-calculated portfolio metric and its governed semantics.

    `value` is `None` only for a ratio whose denominator is zero in its window: a ratio over
    nothing is undefined, not zero (owner decisions #4 and #8, ADR-087). An additive metric
    always carries a number.
    """

    value: CampaignNumber | None
    units: Units
    basis: MeasurementBasis
    period: DateRange
    grain: Grain

    def __post_init__(self) -> None:
        # A quotient over nothing is undefined: a ratio, or a cost per click or per thousand
        # impressions (currency). The comparison holds absence to its quotient metrics.
        if self.value is None:
            if self.units.kind not in {UnitKind.RATIO, UnitKind.CURRENCY}:
                raise ValueError("only a quotient portfolio metric may be typed absent.")
        else:
            _number(self.value, "campaign portfolio metric value")
        if self.basis is not MeasurementBasis.AD_ATTRIBUTED:
            raise ValueError("campaign portfolio metrics must be ad-attributed.")

    @property
    def number(self) -> CampaignNumber:
        """Return the value of a metric that is always defined (additive metrics and totals)."""
        if self.value is None:
            raise ValueError("this campaign portfolio metric value is typed absent.")
        return self.value


@dataclass(frozen=True, slots=True)
class CampaignPortfolioComparison:
    """Exact server comparison, never a browser-derived delta or ratio."""

    metric: CampaignPortfolioMetric
    current: CampaignPortfolioMetricValue
    prior: CampaignPortfolioMetricValue
    absolute_change: CampaignNumber | None
    percentage_change: str | None
    change: Change | None = None
    """The server-declared change (ADR-121); `None` only when a side is undefined or the change
    is withheld."""
    withheld: bool = False
    """A missing or partial day in this figure's window withholds its change (ADR-118 decision
    5): no delta, percentage or block is served, and the coverage block says why."""

    def __post_init__(self) -> None:
        if (
            self.current.units != self.prior.units
            or self.current.basis is not self.prior.basis
            or self.current.grain != self.prior.grain
            or self.prior.period != _previous_period(self.current.period)
        ):
            raise ValueError("campaign portfolio comparison has incompatible semantics.")
        if self.metric not in _QUOTIENT_METRICS and (
            self.current.value is None or self.prior.value is None
        ):
            raise ValueError("only a quotient portfolio metric may be typed absent.")
        if self.withheld:
            if (
                self.current.value is None
                or self.prior.value is None
                or self.absolute_change is not None
                or self.percentage_change is not None
                or self.change is not None
            ):
                raise ValueError("a withheld portfolio comparison serves no change.")
            return
        if self.current.value is None or self.prior.value is None:
            # An undefined side has no change: neither a delta nor a percentage is served.
            if (
                self.absolute_change is not None
                or self.percentage_change is not None
                or self.change is not None
            ):
                raise ValueError("an undefined portfolio ratio comparison carries no change.")
            return
        if self.change is not None:
            check_change(
                self.change,
                ChangeOperands(self.current.value, self.prior.value, self.current.units),
            )
        if self.absolute_change is None:
            raise ValueError("campaign portfolio comparison must carry the exact server delta.")
        _signed_number(self.absolute_change, "campaign portfolio comparison delta")
        expected = absolute_change(self.current.value, self.prior.value)
        if Decimal(self.absolute_change) != Decimal(expected):
            raise ValueError("campaign portfolio comparison must carry the exact server delta.")
        expected_percent = percentage_change(self.current.value, self.prior.value)
        if self.percentage_change != expected_percent:
            raise ValueError("campaign portfolio comparison percentage is not server-exact.")


def campaign_portfolio_comparison(  # noqa: PLR0917 -- a metric, its two sides, and semantics
    metric: CampaignPortfolioMetric,
    current: CampaignPortfolioMetricValue,
    prior: CampaignPortfolioMetricValue,
    semantics: ChangeSemantics,
) -> CampaignPortfolioComparison:
    """Compare two values exactly; an undefined ratio on either side has no change (ADR-087)."""
    if current.value is None or prior.value is None:
        return CampaignPortfolioComparison(metric, current, prior, None, None, None)
    return CampaignPortfolioComparison(
        metric,
        current,
        prior,
        absolute_change(current.value, prior.value),
        percentage_change(current.value, prior.value),
        build_change(
            ChangeOperands(current.value, prior.value, current.units),
            semantics.for_metric(CAMPAIGN_PORTFOLIO_GOVERNED_METRICS[metric]),
        ),
    )


@dataclass(frozen=True, slots=True)
class CampaignPortfolioRow:
    """A safe selected campaign plus its authoritative metrics.

    A row carries no spend spark (render format 11): the approved table has no trend column, a
    campaign's daily spend has its home in campaign detail, and at a 30-day window fifty sparks
    would push the body past its byte cap (ADR-127). The format-10 sparked row retired in the
    integration change.
    """

    signature: SafeCampaignDisplaySignature
    capability: CampaignSelectionCapability
    comparisons: tuple[CampaignPortfolioComparison, ...]
    freshness_qualifiers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if tuple(item.metric for item in self.comparisons) != _ROW_METRICS:
            raise ValueError("campaign portfolio row metric order is fixed.")
        periods = {item.current.period for item in self.comparisons}
        if len(periods) != 1 or any(
            item.current.grain != CAMPAIGN_PORTFOLIO_ROW_GRAIN
            or item.prior.grain != CAMPAIGN_PORTFOLIO_ROW_GRAIN
            for item in self.comparisons
        ):
            raise ValueError("campaign portfolio row metrics must use the resolved row grain.")
        kinds = tuple(item.current.units.kind for item in self.comparisons)
        if kinds != (UnitKind.CURRENCY, UnitKind.RATIO, UnitKind.COUNT, UnitKind.RATIO):
            raise ValueError("campaign portfolio row metric units are fixed.")
        if len(set(self.freshness_qualifiers)) != len(self.freshness_qualifiers) or any(
            not value.strip() for value in self.freshness_qualifiers
        ):
            raise ValueError(
                "campaign portfolio row freshness qualifiers must be unique and nonblank."
            )

    def comparison_for(self, metric: CampaignPortfolioMetric) -> CampaignPortfolioComparison:
        """Return the closed server-computed row comparison for one declared metric."""
        return next(item for item in self.comparisons if item.metric is metric)


@dataclass(frozen=True, slots=True)
class CampaignPortfolioTrendPoint:
    """One exact dated row carrying every client-switchable chart metric."""

    day: date
    spend: CampaignNumber
    portfolio_roas: CampaignNumber | None
    purchases: CampaignNumber | None
    """Both `None` on a spend-only trend, which a multi-channel option serves (ADR-127 d3)."""

    def __post_init__(self) -> None:
        # A spend-only point has neither; a complete one always has purchases, and its ROAS
        # is typed absent on a day with no spend (ADR-087).
        if self.purchases is None and self.portfolio_roas is not None:
            raise ValueError("a campaign portfolio trend point is spend-only or complete.")
        for label, value in (
            ("campaign portfolio trend spend", self.spend),
            ("campaign portfolio trend ROAS", self.portfolio_roas),
            ("campaign portfolio trend purchases", self.purchases),
        ):
            if value is not None:
                _number(value, label)


@dataclass(frozen=True, slots=True)
class CampaignPortfolioTrend:
    """Closed portfolio trend—metric switching only selects a returned column.

    A spend-only trend carries no ROAS or purchases units or points: under a multi-channel
    option those series would be attributed totals across channels (ADR-127 decision 3).
    """

    period: DateRange
    points: tuple[CampaignPortfolioTrendPoint, ...]
    spend_units: Units
    roas_units: Units | None
    purchases_units: Units | None
    basis: MeasurementBasis
    grain: Grain = CAMPAIGN_PORTFOLIO_TREND_GRAIN

    @property
    def spend_only(self) -> bool:
        """Whether this trend serves the spend series alone."""
        return self.roas_units is None

    def __post_init__(self) -> None:
        if (self.roas_units is None) != (self.purchases_units is None) or any(
            (point.purchases is None) != self.spend_only for point in self.points
        ):
            raise ValueError("a campaign portfolio trend is spend-only or complete throughout.")
        if (
            self.spend_units.kind is not UnitKind.CURRENCY
            or (self.roas_units is not None and self.roas_units.kind is not UnitKind.RATIO)
            or (
                self.purchases_units is not None and self.purchases_units.kind is not UnitKind.COUNT
            )
            or self.basis is not MeasurementBasis.AD_ATTRIBUTED
            or self.grain != CAMPAIGN_PORTFOLIO_TREND_GRAIN
        ):
            raise ValueError("campaign portfolio trend semantics are fixed.")
        days = tuple(point.day for point in self.points)
        expected = tuple(
            self.period.start + timedelta(days=index) for index in range(self.period.days)
        )
        if days != expected:
            raise ValueError("campaign portfolio trend must cover the resolved period exactly.")


@dataclass(frozen=True, slots=True)
class CampaignPortfolioReductionRequest:
    """Validated campaign-grain operands and the catalog identities allowed into the portfolio."""

    candidates: frozenset[tuple[str, str]]
    current: Mapping[str, Mapping[tuple[str, ...], CampaignNumber]]
    prior: Mapping[str, Mapping[tuple[str, ...], CampaignNumber]]
    daily: Mapping[str, Mapping[tuple[str, ...], CampaignNumber]]
    """Per-campaign daily operands keyed `(day, campaign, marketplace)`; empty with sums."""
    period: DateRange
    daily_sums: Mapping[str, Mapping[date, Decimal]] | None = None
    """The bake's daily operands already summed over `candidates` (`AuthorizedDailySums`)."""

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError("campaign portfolio reduction requires catalog candidates.")
        required = frozenset({"ad_spend", "attributed_revenue_14d", "impressions", "purchases"})
        daily = self.daily if self.daily_sums is None else self.daily_sums
        if (
            frozenset(self.current) != required
            or frozenset(self.prior) != required
            or frozenset(daily) != required
            or (self.daily_sums is not None and self.daily)
        ):
            raise ValueError("campaign portfolio reduction operands are incomplete.")


@dataclass(frozen=True, slots=True)
class CampaignPortfolioReduction:
    """Portfolio totals and trend reduced only from the server-authorized campaign identities."""

    current: Mapping[CampaignPortfolioMetric, CampaignNumber | None]
    prior: Mapping[CampaignPortfolioMetric, CampaignNumber | None]
    trend_points: tuple[CampaignPortfolioTrendPoint, ...]

    def __post_init__(self) -> None:
        if frozenset(self.current) != frozenset(_TOTAL_METRICS) or frozenset(
            self.prior
        ) != frozenset(_TOTAL_METRICS):
            raise ValueError("campaign portfolio reduction totals are incomplete.")


def _authorized_sum(
    values: Mapping[tuple[str, ...], CampaignNumber],
    identities: frozenset[tuple[str, str]],
    *,
    day: date | None = None,
) -> Decimal:
    """Sum additive operands over catalog identities.

    A campaign absent from a fully covered additive answer contributed nothing in that grain:
    a grouped conversion query has no row for a campaign with zero attributed purchases.
    Absence is therefore zero, not incompatible evidence. Coverage, suppression, and
    truncation are refused upstream before an answer reaches this reduction.
    """
    keys = (
        tuple((day.isoformat(), *identity) for identity in identities)
        if day is not None
        else tuple(identities)
    )
    return sum((Decimal(values.get(key, 0)) for key in keys), Decimal())


def _portfolio_roas(revenue: Decimal, spend: Decimal) -> Decimal | None:
    """Reconstruct the governed portfolio ratio from its additive operands.

    Over no spend the ratio is undefined, so it is typed absent (ADR-087): one channel's
    sibling can have a day, or a window, with no spend (ADR-127).
    """
    if not spend:
        return None
    return (revenue / spend).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)


def reduce_campaign_portfolio(
    request: CampaignPortfolioReductionRequest,
) -> CampaignPortfolioReduction:
    """Reduce fixed campaign-grain operands after catalog authorization, never campaign ratios."""
    current_spend = _authorized_sum(request.current["ad_spend"], request.candidates)
    current_revenue = _authorized_sum(request.current["attributed_revenue_14d"], request.candidates)
    prior_spend = _authorized_sum(request.prior["ad_spend"], request.candidates)
    prior_revenue = _authorized_sum(request.prior["attributed_revenue_14d"], request.candidates)
    current = {
        CampaignPortfolioMetric.SPEND: current_spend,
        CampaignPortfolioMetric.PURCHASES: _authorized_sum(
            request.current["purchases"], request.candidates
        ),
        CampaignPortfolioMetric.IMPRESSIONS: _authorized_sum(
            request.current["impressions"], request.candidates
        ),
        CampaignPortfolioMetric.PORTFOLIO_ROAS: _portfolio_roas(current_revenue, current_spend),
    }
    prior = {
        CampaignPortfolioMetric.SPEND: prior_spend,
        CampaignPortfolioMetric.PURCHASES: _authorized_sum(
            request.prior["purchases"], request.candidates
        ),
        CampaignPortfolioMetric.IMPRESSIONS: _authorized_sum(
            request.prior["impressions"], request.candidates
        ),
        CampaignPortfolioMetric.PORTFOLIO_ROAS: _portfolio_roas(prior_revenue, prior_spend),
    }

    def daily(metric: str, day: date) -> Decimal:
        if request.daily_sums is not None:
            return request.daily_sums[metric][day]
        return _authorized_sum(request.daily[metric], request.candidates, day=day)

    points = tuple(
        CampaignPortfolioTrendPoint(
            day,
            daily("ad_spend", day),
            _portfolio_roas(daily("attributed_revenue_14d", day), daily("ad_spend", day)),
            daily("purchases", day),
        )
        for day in (
            request.period.start + timedelta(days=index) for index in range(request.period.days)
        )
    )
    return CampaignPortfolioReduction(current, prior, points)


@dataclass(frozen=True, slots=True)
class CampaignPortfolioPagination:
    """The served segment's position inside one complete baked ordering (ADR-071).

    Replaces the dead-end truncation record: rows are a contiguous segment of a complete
    ordering, and the record carries the boundless population total so "rows m to n of N"
    is always honest. Totals remain over the unbounded authorized population.
    """

    segment_index: int
    segment_size: int
    segment_count: int
    total_count: int

    def __post_init__(self) -> None:
        if self.segment_size != CAMPAIGN_PORTFOLIO_MAX_ROWS:
            raise ValueError("campaign portfolio pagination must use the fixed segment size.")
        if self.total_count < 1:
            raise ValueError("campaign portfolio pagination requires a positive population.")
        expected_segments = -(-self.total_count // self.segment_size)
        if self.segment_count != expected_segments:
            raise ValueError("campaign portfolio pagination segment count is not derivable.")
        if not 0 <= self.segment_index < self.segment_count:
            raise ValueError("campaign portfolio pagination segment index is out of range.")

    @property
    def expected_row_count(self) -> int:
        """Return the exact row count of this segment within the population."""
        return min(
            self.segment_size,
            self.total_count - self.segment_index * self.segment_size,
        )


@dataclass(frozen=True, slots=True)
class CampaignPortfolioRosterEntry:
    """One display-only estate roster entry: labels and canonical position, never a key."""

    name: str | None
    channel: str
    marketplace: str
    position: int

    def __post_init__(self) -> None:
        if self.name is not None and not self.name.strip():
            raise ValueError("campaign portfolio roster name cannot be blank.")
        for label, value in (("channel", self.channel), ("marketplace", self.marketplace)):
            if not value.strip():
                raise ValueError(f"campaign portfolio roster {label} cannot be blank.")
        if self.position < 0:
            raise ValueError("campaign portfolio roster position cannot be negative.")


CAMPAIGN_ROSTER_SHARD_ENTRIES: Final = 500
"""Roster entries per shard (ADR-145, class A). A shard is size-checked like every body, so one
past the cap is the page's `body_over_cap`, never a truncated roster."""
CAMPAIGN_ROSTER_ORDERING: Final = "roster"
"""The sibling ordering a filter's roster shards are written under: `{filter}/roster/segment-N`."""
CAMPAIGN_ROSTER_SHARD_KIND: Final = "campaign_roster_shard"


def campaign_roster_shard_count(entries: int) -> int:
    """Count the shards a filter's name roster of `entries` campaigns is written in."""
    return -(-entries // CAMPAIGN_ROSTER_SHARD_ENTRIES)


def campaign_roster_shard_identity(filter_id: str, shard: int) -> PresetArtifactSiblingIdentity:
    """Return one roster shard's sibling identity: the bake writes it and the serve reads it."""
    return PresetArtifactSiblingIdentity(CAMPAIGN_ROSTER_ORDERING, shard, filter_id)


@dataclass(frozen=True, slots=True)
class CampaignPortfolioRosterShards:
    """What a baked body says about its name roster: its shards and the bake's revision.

    The roster itself is written in `shards` ordered siblings (ADR-145), fetched on the name
    field's first focus. Each shard names `revision` as its `parent_revision`, so the browser
    binds the shards it holds to the body it holds.
    """

    revision: str
    shards: int

    def __post_init__(self) -> None:
        if not self.revision.strip() or self.shards < 1:
            raise ValueError("a campaign roster names its revision and at least one shard.")


def campaign_roster_shard_payload(
    roster: tuple[CampaignPortfolioRosterEntry, ...], shard: int, revision: str
) -> dict[str, object]:
    """Encode one ordered shard of a filter's roster, naming the bake's revision."""
    start = shard * CAMPAIGN_ROSTER_SHARD_ENTRIES
    entries = roster[start : start + CAMPAIGN_ROSTER_SHARD_ENTRIES]
    if not entries:
        raise ValueError("a campaign roster shard holds at least one entry.")
    return {
        **_roster_payload(entries),
        "kind": CAMPAIGN_ROSTER_SHARD_KIND,
        "parent_revision": revision,
        "shard": shard,
    }


def campaign_roster_shard_from_payload(
    value: object, shard: int, revision: str
) -> tuple[CampaignPortfolioRosterEntry, ...]:
    """Refuse a roster shard that isn't exactly what the bake writes for `shard` of `revision`."""
    item = _object(
        value,
        frozenset({"channels", "entries", "kind", "marketplaces", "parent_revision", "shard"}),
        "campaign roster shard",
    )
    if (
        item["kind"] != CAMPAIGN_ROSTER_SHARD_KIND
        or item["parent_revision"] != revision
        or item["shard"] != shard
        or isinstance(item["shard"], bool)
    ):
        raise ValueError("campaign roster shard is not the one requested.")
    entries = _campaign_roster_from_payload(
        {key: item[key] for key in ("channels", "entries", "marketplaces")}
    )
    start = shard * CAMPAIGN_ROSTER_SHARD_ENTRIES
    if not entries or tuple(entry.position for entry in entries) != tuple(
        range(start, start + len(entries))
    ):
        raise ValueError("campaign roster shard positions are not its own.")
    return entries


def order_campaign_portfolio_rows(
    rows: tuple[CampaignPortfolioRow, ...], sort: CampaignPortfolioSort
) -> tuple[CampaignPortfolioRow, ...]:
    """Order rows by one declared comparator; the single home every ordering derives from."""
    if sort.field is CampaignPortfolioSortField.NAME:
        named = tuple(row for row in rows if row.signature.effective_name is not None)
        unnamed = tuple(row for row in rows if row.signature.effective_name is None)
        stable_named = sorted(named, key=lambda row: row.signature.sort_key)
        stable_unnamed = tuple(sorted(unnamed, key=lambda row: row.signature.sort_key))
        return (
            *tuple(
                sorted(
                    stable_named,
                    key=lambda row: cast("str", row.signature.effective_name).casefold(),
                    reverse=sort.direction == "desc",
                )
            ),
            *stable_unnamed,
        )
    metric = CampaignPortfolioMetric(sort.field.value)

    def key(row: CampaignPortfolioRow) -> tuple[bool, Decimal, tuple[str, ...]]:
        # An undefined ratio (ADR-087) sorts after every value in both directions, as an
        # unnamed campaign does under the name sort.
        value = row.comparison_for(metric).current.value
        if value is None:
            return (True, Decimal(0), row.signature.sort_key)
        number = Decimal(value)
        return (False, -number if sort.direction == "desc" else number, row.signature.sort_key)

    return tuple(sorted(rows, key=key))


def campaign_portfolio_segment(
    artifact: CampaignPortfolioArtifact,
    sort: CampaignPortfolioSort,
    segment_index: int,
) -> CampaignPortfolioArtifact | None:
    """Select one segment of one complete baked ordering; None when the index is out of range.

    Valid while the artifact carries its complete population — every population up to the
    bake bound. Populations beyond one artifact read baked segment bodies instead (the
    ADR-071 bulk-tier slice); reordering a partial carry would present a sorted page of an
    unsorted whole, which the population-scale decision forbids.
    """
    if artifact.outcome is not CampaignPortfolioOutcome.COMPLETE:
        return replace(artifact, sort=sort) if segment_index == 0 else None
    pagination = artifact.pagination
    if pagination is None:
        raise ValueError("a complete campaign portfolio requires its pagination record.")
    if len(artifact.rows) != artifact.total_count:
        raise ValueError("campaign portfolio segments require the complete retained population.")
    if not 0 <= segment_index < pagination.segment_count:
        return None
    ordered = order_campaign_portfolio_rows(artifact.rows, sort)
    start = segment_index * pagination.segment_size
    return replace(
        artifact,
        sort=sort,
        rows=ordered[start : start + pagination.segment_size],
        pagination=CampaignPortfolioPagination(
            segment_index,
            pagination.segment_size,
            pagination.segment_count,
            pagination.total_count,
        ),
    )


def campaign_portfolio_roster(
    rows: tuple[CampaignPortfolioRow, ...],
) -> tuple[CampaignPortfolioRosterEntry, ...]:
    """Project the full ordered population into its display-only estate roster."""
    ordered = order_campaign_portfolio_rows(
        rows, CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc")
    )
    return tuple(
        CampaignPortfolioRosterEntry(
            name=row.signature.effective_name,
            channel=row.signature.channel,
            marketplace=row.signature.marketplace,
            position=index,
        )
        for index, row in enumerate(ordered)
    )


# --- the channel filter (ADR-127) ------------------------------------------------------------

ALL_CHANNELS_FILTER_ID = "all"
SPONSORED_FILTER_ID = "sponsored"
DSP_CHANNEL_KEY = "amazon_dsp"
"""Campaigns' "Sponsored ads" option is the governed channels other than this one (ADR-127 d1)."""
ALL_CHANNELS_LABEL = "All channels"
SPONSORED_FILTER_LABEL = "Sponsored ads (all sponsored channels)"
MULTI_CHANNEL_FILTER_IDS = frozenset({ALL_CHANNELS_FILTER_ID, SPONSORED_FILTER_ID})
_FILTER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
"""A filter id is a URL governed id, the same grammar `NextStep.v2` targets use."""
NOT_ADDED_VALUE_TEXT = "Not added across channels"
NOT_ADDED_REASON = "Choose a channel to see it."
MULTI_CHANNEL_TREND_LINE = "Choose a channel to see purchases and ROAS over time."
SCORECARD_POINTER_LINE = "Return on ad spend per channel is on the Scorecard."
SPONSORED_VIEWABILITY_REASON = "Not available: sponsored ads don't report viewability"
DSP_VIEWABILITY_HEADING = "Amazon DSP only"
DELIVERY_COLUMN_BASES: Final[Mapping[CampaignPortfolioMetric, str]] = {
    CampaignPortfolioMetric.CTR: "Clicks ÷ impressions",
    CampaignPortfolioMetric.CPC: "Ad spend ÷ clicks",
    CampaignPortfolioMetric.CPM: "Ad spend per 1,000 impressions",
}
"""Each delivery ratio's formula, shown as its column's basis line (ia §7.2 item 4)."""
DELIVERY_QUOTIENT_ABSENCE: Final[Mapping[CampaignPortfolioMetric, str]] = {
    CampaignPortfolioMetric.CTR: "Not applicable: no impressions in this window",
    CampaignPortfolioMetric.CPC: "Not applicable: no clicks in this window",
    CampaignPortfolioMetric.CPM: "Not applicable: no impressions in this window",
}
"""Why a delivery ratio is "—": its channel's denominator is zero in the window (ADR-087)."""


def multi_channel_insights_line(option_label: str) -> str:
    """Write the insights row's one server line under a multi-channel option (ia §7.2 item 5)."""
    return (
        f"Under {option_label}, insights cover spend and single-campaign changes. "
        "Choose a channel for insights on attributed sales."
    )


@dataclass(frozen=True, slots=True)
class CampaignChannelFilterOption:
    """One server-declared channel filter value: a governed id and its display label."""

    filter_id: str
    label: str

    def __post_init__(self) -> None:
        if not _FILTER_ID.fullmatch(self.filter_id):
            raise ValueError("a campaign channel filter id must be a governed id.")
        if not self.label.strip():
            raise ValueError("a campaign channel filter needs a display label.")

    @property
    def multi_channel(self) -> bool:
        """Whether this option spans more than one governed channel."""
        return self.filter_id in MULTI_CHANNEL_FILTER_IDS


@dataclass(frozen=True, slots=True)
class CampaignChannelFilter:
    """The closed filter set a preset offers and the one this sibling renders (ADR-127 d1)."""

    selected: str
    options: tuple[CampaignChannelFilterOption, ...]

    def __post_init__(self) -> None:
        ids = tuple(option.filter_id for option in self.options)
        if (
            not ids
            or ids[0] != ALL_CHANNELS_FILTER_ID
            or self.options[0].label != (ALL_CHANNELS_LABEL)
        ):
            raise ValueError("a campaign channel filter set starts with All channels.")
        if len(set(ids)) != len(ids):
            raise ValueError("campaign channel filter ids must be unique.")
        if SPONSORED_FILTER_ID in ids and (
            ids[1] != SPONSORED_FILTER_ID
            or self.options[1].label != SPONSORED_FILTER_LABEL
            or len([item for item in ids[2:] if item != DSP_CHANNEL_KEY]) < 2  # noqa: PLR2004
        ):
            raise ValueError(
                "Sponsored ads follows All channels, only with two or more sponsored channels."
            )
        if self.selected not in ids:
            raise ValueError("the rendered campaign channel filter must be offered.")

    @property
    def option(self) -> CampaignChannelFilterOption:
        """The option this sibling renders."""
        return next(item for item in self.options if item.filter_id == self.selected)

    @property
    def multi_channel(self) -> bool:
        """Whether this sibling spans more than one governed channel."""
        return self.option.multi_channel

    @property
    def offered(self) -> frozenset[str]:
        """Every filter id this preset offers."""
        return frozenset(option.filter_id for option in self.options)


def campaign_channel_filter_options(
    channels: tuple[tuple[str, str], ...],
) -> tuple[CampaignChannelFilterOption, ...]:
    """Declare the options for the channels present, given in taxonomy order (key, label).

    All channels first; "Sponsored ads" when two or more sponsored channels are present (with
    one, it would equal that channel); then each channel present (ADR-127 decision 1).
    """
    sponsored = [key for key, _ in channels if key != DSP_CHANNEL_KEY]
    return (
        CampaignChannelFilterOption(ALL_CHANNELS_FILTER_ID, ALL_CHANNELS_LABEL),
        *(
            (CampaignChannelFilterOption(SPONSORED_FILTER_ID, SPONSORED_FILTER_LABEL),)
            if len(sponsored) >= 2  # noqa: PLR2004
            else ()
        ),
        *(CampaignChannelFilterOption(key, label) for key, label in channels),
    )


def channels_for_filter(filter_id: str, channels: tuple[str, ...]) -> tuple[str, ...]:
    """Return the governed channel keys one option spans, in the order given."""
    if filter_id == ALL_CHANNELS_FILTER_ID:
        return channels
    if filter_id == SPONSORED_FILTER_ID:
        return tuple(key for key in channels if key != DSP_CHANNEL_KEY)
    return tuple(key for key in channels if key == filter_id)


@dataclass(frozen=True, slots=True)
class CampaignChannelTotalsRow:
    """One "By channel" row: a channel's spend and impressions, never an attributed figure."""

    channel: str
    label: str
    spend: CampaignPortfolioComparison
    impressions: CampaignPortfolioComparison

    def __post_init__(self) -> None:
        if not _FILTER_ID.fullmatch(self.channel) or not self.label.strip():
            raise ValueError("a By channel row names a governed channel and its label.")
        if (
            self.spend.metric is not CampaignPortfolioMetric.SPEND
            or self.impressions.metric is not CampaignPortfolioMetric.IMPRESSIONS
            or any(
                item.current.grain != CAMPAIGN_PORTFOLIO_CHANNEL_GRAIN
                for item in (self.spend, self.impressions)
            )
        ):
            raise ValueError("a By channel row carries channel-grain spend and impressions.")


@dataclass(frozen=True, slots=True)
class CampaignViewabilityCell:
    """The Amazon DSP viewable rate in one delivery row, or why the row has none.

    A partly reported window serves the rate over the days that arrived, names them in
    `coverage_text` and withholds the change (ADR-118 decision 5).
    """

    comparison: CampaignPortfolioComparison | None
    coverage_text: str | None = None
    absence_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.comparison is None) == (self.absence_reason is None):
            raise ValueError("a viewability cell is a value or a typed absence, never both.")
        if self.comparison is not None and (
            self.comparison.metric is not CampaignPortfolioMetric.VIEWABLE_RATE
        ):
            raise ValueError("a viewability cell serves the viewable rate.")
        if self.coverage_text is not None and (
            self.comparison is None or not self.coverage_text.strip()
        ):
            raise ValueError("only a partly reported viewable rate names its covered days.")
        # A withheld change's reason is in the coverage block: this window's entry names its
        # covered days here too, and a prior-window gap has its own entry (ADR-118 decision 5).
        if self.absence_reason is not None and not self.absence_reason.strip():
            raise ValueError("a viewability absence states its reason.")


@dataclass(frozen=True, slots=True)
class CampaignChannelDeliveryRow:
    """One "Delivery per channel" row: each ratio from that channel's own operands."""

    channel: str
    label: str
    clicks: CampaignPortfolioComparison
    ctr: CampaignPortfolioComparison
    cpc: CampaignPortfolioComparison
    cpm: CampaignPortfolioComparison
    viewable: CampaignViewabilityCell | None = None

    def __post_init__(self) -> None:
        if not _FILTER_ID.fullmatch(self.channel) or not self.label.strip():
            raise ValueError("a delivery row names a governed channel and its label.")
        cells = (self.clicks, self.ctr, self.cpc, self.cpm)
        if tuple(item.metric for item in cells) != _DELIVERY_METRICS or any(
            item.current.grain != CAMPAIGN_PORTFOLIO_CHANNEL_GRAIN or item.withheld
            for item in cells
        ):
            raise ValueError("a delivery row carries channel-grain clicks, CTR, CPC and CPM.")
        if (
            self.viewable is not None
            and self.viewable.comparison is not None
            and (self.channel != DSP_CHANNEL_KEY)
        ):
            raise ValueError("only the Amazon DSP row reports a viewable rate.")

    @property
    def comparisons(self) -> tuple[CampaignPortfolioComparison, ...]:
        """Every served comparison in this row, the viewable rate last."""
        viewable = None if self.viewable is None else self.viewable.comparison
        return (
            self.clicks,
            self.ctr,
            self.cpc,
            self.cpm,
            *(() if viewable is None else (viewable,)),
        )


@dataclass(frozen=True, slots=True)
class CampaignNotAddedTile:
    """An attributed headline a multi-channel option shows in place, with no figure."""

    metric: CampaignPortfolioMetric
    label: str
    value_text: str = NOT_ADDED_VALUE_TEXT
    reason: str = NOT_ADDED_REASON

    def __post_init__(self) -> None:
        if self.metric not in _NOT_ADDED_METRICS:
            raise ValueError("only an attributed headline is not added across channels.")
        if not all(text.strip() for text in (self.label, self.value_text, self.reason)):
            raise ValueError("a not-added tile names its figure, its state and its reason.")


@dataclass(frozen=True, slots=True)
class CampaignMultiChannelView:
    """What a multi-channel sibling carries beyond its rows (ADR-127 decisions 3 and 4)."""

    by_channel: tuple[CampaignChannelTotalsRow, ...]
    delivery: tuple[CampaignChannelDeliveryRow, ...]
    viewability: bool
    """Whether the option includes Amazon DSP, so the delivery table declares viewable rate."""
    not_added: tuple[CampaignNotAddedTile, ...]
    insights_line: str
    trend_line: str = MULTI_CHANNEL_TREND_LINE
    scorecard_line: str = SCORECARD_POINTER_LINE

    def __post_init__(self) -> None:
        channels = tuple(row.channel for row in self.by_channel)
        if not channels or len(set(channels)) != len(channels):
            raise ValueError("By channel lists each channel of the option once.")
        if tuple(row.channel for row in self.delivery) != channels:
            raise ValueError("Delivery per channel lists the By channel rows, in order.")
        if self.viewability != (DSP_CHANNEL_KEY in channels):
            raise ValueError("viewable rate is declared exactly when the option has Amazon DSP.")
        if any((row.viewable is not None) != self.viewability for row in self.delivery):
            raise ValueError("every delivery row has a viewability cell when one is declared.")
        if tuple(tile.metric for tile in self.not_added) != _NOT_ADDED_METRICS:
            raise ValueError("purchases and ROAS are the not-added tiles, in order.")
        if not all(
            text.strip() for text in (self.insights_line, self.trend_line, self.scorecard_line)
        ):
            raise ValueError("a multi-channel view's lines are nonblank server text.")

    @property
    def legend(self) -> tuple[str, ...]:
        """One visible legend line for every "—" reason the delivery table uses."""
        reasons: dict[str, None] = {}
        for row in self.delivery:
            # A ratio over a zero denominator, then a viewable rate the channel doesn't report.
            for cell in (row.ctr, row.cpc, row.cpm):
                if cell.current.value is None:
                    reasons[DELIVERY_QUOTIENT_ABSENCE[cell.metric]] = None
            reason = None if row.viewable is None else row.viewable.absence_reason
            if reason is not None:
                reasons[reason] = None
        return tuple(f"— {reason}." for reason in reasons)


CAMPAIGN_PORTFOLIO_COVERAGE: Final[
    tuple[tuple[CampaignPortfolioMetric, str, CoverageKind], ...]
] = (
    (CampaignPortfolioMetric.SPEND, "Headline and campaign rows", CoverageKind.ADDITIVE),
    (CampaignPortfolioMetric.PURCHASES, "Headline and campaign rows", CoverageKind.ADDITIVE),
    (CampaignPortfolioMetric.IMPRESSIONS, "Headline", CoverageKind.ADDITIVE),
    (CampaignPortfolioMetric.PORTFOLIO_ROAS, "Headline", CoverageKind.RATIO),
    (CampaignPortfolioMetric.ATTRIBUTED_ROAS, "Campaign rows", CoverageKind.RATIO),
    (CampaignPortfolioMetric.CTR, "Campaign rows", CoverageKind.RATIO),
)
"""Every figure the Campaigns page shows, where it shows, and how a missing day reduces it."""

_MULTI_CHANNEL_COVERAGE: Final[tuple[tuple[CampaignPortfolioMetric, str, CoverageKind], ...]] = (
    (
        CampaignPortfolioMetric.SPEND,
        "Headline, By channel and campaign rows",
        CoverageKind.ADDITIVE,
    ),
    (CampaignPortfolioMetric.PURCHASES, "Campaign rows", CoverageKind.ADDITIVE),
    (CampaignPortfolioMetric.IMPRESSIONS, "Headline and By channel", CoverageKind.ADDITIVE),
    (CampaignPortfolioMetric.ATTRIBUTED_ROAS, "Campaign rows", CoverageKind.RATIO),
    (CampaignPortfolioMetric.CTR, "Campaign rows", CoverageKind.RATIO),
)
"""A multi-channel option's figures: no attributed headline, so none is listed (ADR-127)."""
DELIVERY_COVERAGE_PREFIX = "channel_"
"""Delivery per channel's coverage slots are prefixed, so its CTR never shares the rows' slot."""
_DELIVERY_COVERAGE_ROLE = "Delivery per channel"
VIEWABLE_PRIOR_ROLE = "prior"
"""The role of the viewable rate's prior-window entry, listed only while that window misses days."""


def delivery_coverage_slot(metric: CampaignPortfolioMetric) -> str:
    """Name the coverage slot one delivery column reads its days and absence text from."""
    return f"{DELIVERY_COVERAGE_PREFIX}{metric.value}"


def _coverage_roster(
    multi_channel: bool, viewability: bool
) -> tuple[tuple[CampaignPortfolioMetric, str, CoverageKind, str], ...]:
    headline = _MULTI_CHANNEL_COVERAGE if multi_channel else CAMPAIGN_PORTFOLIO_COVERAGE
    delivery = (
        ()
        if not multi_channel
        else (
            *(
                (
                    metric,
                    _DELIVERY_COVERAGE_ROLE,
                    CoverageKind.ADDITIVE
                    if metric is CampaignPortfolioMetric.CLICKS
                    else CoverageKind.RATIO,
                    delivery_coverage_slot(metric),
                )
                for metric in _DELIVERY_METRICS
            ),
            *(
                (
                    (
                        CampaignPortfolioMetric.VIEWABLE_RATE,
                        _DELIVERY_COVERAGE_ROLE,
                        CoverageKind.RATIO,
                        delivery_coverage_slot(CampaignPortfolioMetric.VIEWABLE_RATE),
                    ),
                )
                if viewability
                else ()
            ),
        )
    )
    return (
        *((metric, role, kind, metric.value) for metric, role, kind in headline),
        *delivery,
    )


def campaign_portfolio_coverage_facts(
    labels: Mapping[str, str],
    *,
    multi_channel: bool = False,
    viewability: bool = False,
) -> tuple[CoverageFact, ...]:
    """Name every Campaigns figure from its dictionary label, each covering its whole window.

    `labels` maps dictionary ids to display labels. A bake measures only answers whose window is
    covered end to end, so its figures are complete; a missing or partial day is stated by
    replacing a fact's dates before `write_evidence_status` writes the block. A multi-channel
    option lists no attributed headline and adds Delivery per channel (ADR-127).
    """
    roster = _coverage_roster(multi_channel, viewability)
    named = labels_for(
        (CAMPAIGN_PORTFOLIO_GOVERNED_METRICS[item[0]] for item in roster),
        labels,
        "campaign portfolio",
    )
    return tuple(
        CoverageFact(
            slot,
            named[CAMPAIGN_PORTFOLIO_GOVERNED_METRICS[metric]],
            role_label,
            kind,
            entity_word="campaigns",
        )
        for metric, role_label, kind, slot in roster
    )


def campaign_portfolio_evidence_status(
    period: DateRange,
    labels: Mapping[str, str],
    *,
    multi_channel: bool = False,
    viewability_missing: tuple[date, ...] | None = None,
    viewability_prior_missing: tuple[date, ...] = (),
) -> EvidenceStatus:
    """Write the coverage block of a portfolio.

    `viewability_missing` is `None` when the option has no Amazon DSP; otherwise it names the
    days the DSP viewability source hasn't sent (empty when it covers the whole window).
    `viewability_prior_missing` names the prior window's unsent days: when there are any, the
    viewable rate gains a prior-window entry right after its own, the one place that says why
    its change is withheld (ADR-118 decision 5).
    """
    if viewability_prior_missing and viewability_missing is None:
        raise ValueError("only an option with Amazon DSP names prior viewability days.")
    facts = campaign_portfolio_coverage_facts(
        labels, multi_channel=multi_channel, viewability=viewability_missing is not None
    )
    viewable_slot = delivery_coverage_slot(CampaignPortfolioMetric.VIEWABLE_RATE)
    if viewability_missing:
        facts = tuple(
            replace(fact, missing_dates=viewability_missing) if fact.slot == viewable_slot else fact
            for fact in facts
        )
    if viewability_prior_missing:
        facts = tuple(
            item
            for fact in facts
            for item in (
                (
                    fact,
                    replace(
                        fact,
                        role=VIEWABLE_PRIOR_ROLE,
                        role_label=f"{_DELIVERY_COVERAGE_ROLE}, prior window",
                        missing_dates=viewability_prior_missing,
                    ),
                )
                if fact.slot == viewable_slot
                else (fact,)
            )
        )
    return write_evidence_status(period, facts)


def _require_coverage(
    artifact: CampaignPortfolioArtifact,
    comparisons: tuple[CampaignPortfolioComparison, ...],
) -> None:
    """Hold every withheld change to the coverage block, and the block to the page's figures."""
    status = artifact.evidence_status
    if status is None:
        if any(item.withheld for item in comparisons):
            raise ValueError("a withheld portfolio change requires its coverage block.")
        return
    if artifact.outcome is not CampaignPortfolioOutcome.COMPLETE:
        raise ValueError("only a complete campaign portfolio serves a coverage block.")
    view = artifact.channel_view
    viewable_slot = delivery_coverage_slot(CampaignPortfolioMetric.VIEWABLE_RATE)
    # The viewable rate's prior-window entry is listed only while that window misses days.
    prior_listed = any(
        entry.slot == viewable_slot and entry.role == VIEWABLE_PRIOR_ROLE
        for entry in status.entries
    )
    roster = tuple(
        pair
        for _, _, _, slot in _coverage_roster(
            view is not None, view is not None and view.viewability
        )
        for pair in (
            ((slot, "current"), (slot, VIEWABLE_PRIOR_ROLE))
            if slot == viewable_slot and prior_listed
            else ((slot, "current"),)
        )
    )
    require_entries(status, roster, artifact.period, label="campaign portfolio")
    withheld = withheld_slots(status)
    if prior_listed:
        prior_entry = status.entry(viewable_slot, VIEWABLE_PRIOR_ROLE)
        if not prior_entry.withheld:
            raise ValueError("a listed prior viewability entry names the days it misses.")
        withheld |= {viewable_slot}
    delivery = (
        ()
        if view is None
        else tuple(
            (delivery_coverage_slot(item.metric), item)
            for row in view.delivery
            for item in row.comparisons
        )
    )
    for slot, item in (*((item.metric.value, item) for item in comparisons), *delivery):
        # An undefined ratio (ADR-087) has no change to withhold, whatever its coverage.
        if item.current.value is None or item.prior.value is None:
            continue
        if item.withheld != (slot in withheld):
            raise ValueError("a portfolio change is withheld exactly where coverage says so.")
        if not item.withheld and item.change is None:
            raise ValueError("a portfolio with a coverage block serves every computable change.")


@dataclass(frozen=True, slots=True)
class CampaignPortfolioArtifact:
    """The V2 campaign parent artifact, with ordering and arithmetic owned by the server."""

    outcome: CampaignPortfolioOutcome
    period: DateRange
    prior_period: DateRange
    sort: CampaignPortfolioSort
    rows: tuple[CampaignPortfolioRow, ...]
    total_count: int
    pagination: CampaignPortfolioPagination | None
    totals: tuple[CampaignPortfolioComparison, ...]
    trend: CampaignPortfolioTrend | None
    evidence: tuple[CampaignListEvidence, ...]
    reason_codes: tuple[CampaignPortfolioInsufficiencyReason, ...] = ()
    kind: str = "campaign_portfolio"
    insights: tuple[InsightSlot, ...] = field(default_factory=default_portfolio_insights)
    roster: tuple[CampaignPortfolioRosterEntry, ...] = ()
    """The whole name roster, held in process; a baked body carries `roster_shards` instead."""
    evidence_status: EvidenceStatus | None = None
    """The window's coverage block (ADR-119), on a complete portfolio. `None` on an empty or
    insufficient portfolio, which shows no tile."""
    channel_filter: CampaignChannelFilter | None = None
    """The preset's channel filter set and the option this sibling renders (ADR-127). Every
    served body carries it; `None` only on the unfiltered in-process portfolio the insight
    precompute reduces, which is never served."""
    channel_view: CampaignMultiChannelView | None = None
    """A complete multi-channel sibling's By channel, Delivery per channel and not-added tiles."""
    roster_shards: CampaignPortfolioRosterShards | None = None
    """A baked body's roster: its shards and the bake's revision (ADR-145), never with `roster`."""

    @property
    def filter_id(self) -> str:
        """The channel filter this body renders; the unfiltered portfolio is All channels."""
        selected = None if self.channel_filter is None else self.channel_filter.selected
        return ALL_CHANNELS_FILTER_ID if selected is None else selected

    @property
    def multi_channel(self) -> bool:
        """Whether this body spans more than one governed channel (no attributed total)."""
        return self.channel_filter is not None and self.channel_filter.multi_channel

    def __post_init__(self) -> None:  # noqa: PLR0915 -- one closed artifact contract
        if self.kind != "campaign_portfolio":
            raise ValueError("campaign portfolio kind is unsupported.")
        complete = self.outcome is CampaignPortfolioOutcome.COMPLETE
        if (self.channel_view is not None) != (complete and self.multi_channel):
            raise ValueError("a complete multi-channel sibling carries its channel view.")
        if self.channel_view is not None and self.channel_filter is not None:
            offered = self.channel_filter.offered
            if any(row.channel not in offered for row in self.channel_view.by_channel):
                raise ValueError("each By channel row applies a filter this preset offers.")
            if any(
                comparison.current.period != self.period
                or comparison.prior.period != self.prior_period
                for row in self.channel_view.by_channel
                for comparison in (row.spend, row.impressions)
            ) or any(
                comparison.current.period != self.period
                or comparison.prior.period != self.prior_period
                for row in self.channel_view.delivery
                for comparison in row.comparisons
            ):
                raise ValueError("per-channel figures use the sibling's own windows.")
        if self.prior_period != _previous_period(self.period):
            raise ValueError("campaign portfolio prior period must be equal-length and preceding.")
        if self.total_count < 0:
            raise ValueError("campaign portfolio total count is invalid.")
        if self.rows and len(self.rows) not in {
            self.total_count,
            0 if self.pagination is None else self.pagination.expected_row_count,
        }:
            raise ValueError(
                "campaign portfolio rows must be the retained population or one exact segment."
            )
        if any(
            comparison.current.period != self.period
            for row in self.rows
            for comparison in row.comparisons
        ):
            raise ValueError("campaign portfolio rows must use the resolved period.")
        if not self.evidence or any(item.requested_period != self.period for item in self.evidence):
            raise ValueError("campaign portfolio evidence must retain the resolved period.")
        _ordered_insufficiency_reasons(self.reason_codes)
        require_insight_roster(
            self.insights,
            (
                InsightSlotId.CONTRIBUTION,
                InsightSlotId.DIVERGENCE,
                InsightSlotId.EXPOSURE,
                InsightSlotId.MOVERS,
            ),
        )
        total_metrics = _MULTI_CHANNEL_TOTAL_METRICS if self.multi_channel else _TOTAL_METRICS
        if self.outcome is CampaignPortfolioOutcome.COMPLETE:
            if (
                not self.rows
                or self.total_count < len(self.rows)
                or tuple(item.metric for item in self.totals) != total_metrics
                or self.trend is None
                or self.trend.spend_only != self.multi_channel
                or self.reason_codes
            ):
                raise ValueError("complete campaign portfolio fields are contradictory.")
        elif self.outcome is CampaignPortfolioOutcome.EMPTY:
            if (
                self.rows
                or self.total_count
                or self.pagination is not None
                or self.roster
                or self.totals
                or self.trend is not None
                or self.reason_codes
            ):
                raise ValueError("empty campaign portfolio fields are contradictory.")
        elif (
            self.rows
            or self.total_count
            or self.pagination is not None
            or self.roster
            or self.totals
            or self.trend is not None
            or not self.reason_codes
        ):
            raise ValueError("insufficient campaign portfolio fields are contradictory.")
        if self.pagination is None:
            if self.rows:
                raise ValueError("campaign portfolio rows require their pagination record.")
        elif self.pagination.total_count != self.total_count or len(self.rows) not in {
            self.total_count,
            self.pagination.expected_row_count,
        }:
            raise ValueError("campaign portfolio pagination contradicts rows or total count.")
        if self.roster and self.roster_shards is not None:
            raise ValueError("a campaign portfolio holds its roster or names its shards.")
        if self.roster:
            if len(self.roster) != self.total_count or tuple(
                entry.position for entry in self.roster
            ) != tuple(range(self.total_count)):
                raise ValueError("campaign portfolio roster must cover the population exactly.")
        elif self.roster_shards is not None:
            if not complete or self.roster_shards.shards != campaign_roster_shard_count(
                self.total_count
            ):
                raise ValueError("campaign portfolio roster shards must cover the population.")
        elif self.rows:
            raise ValueError("campaign portfolio rows require their estate roster.")
        if self.rows:
            identities = tuple(row.signature for row in self.rows)
            capabilities = tuple(row.capability.value for row in self.rows)
            if len(set(identities)) != len(identities) or len(set(capabilities)) != len(
                capabilities
            ):
                raise ValueError("campaign portfolio rows must be safe-identity unique.")
            ordered = order_campaign_portfolio_rows(self.rows, self.sort)
            if self.rows != ordered:
                raise ValueError("campaign portfolio rows must use the declared server order.")
        if self.totals:
            for comparison, expected_metric in zip(self.totals, total_metrics, strict=True):
                if (
                    comparison.metric is not expected_metric
                    or comparison.current.units.kind is not _METRIC_UNIT_KINDS[expected_metric]
                    or comparison.current.period != self.period
                    or comparison.prior.period != self.prior_period
                    or comparison.current.grain != CAMPAIGN_PORTFOLIO_TOTAL_GRAIN
                    or comparison.prior.grain != CAMPAIGN_PORTFOLIO_TOTAL_GRAIN
                ):
                    raise ValueError(
                        "campaign portfolio totals must use the declared portfolio semantics."
                    )
        if self.trend is not None and self.trend.period != self.period:
            raise ValueError("campaign portfolio trend must retain the resolved period.")
        comparisons = (*self.totals, *(item for row in self.rows for item in row.comparisons))
        _require_coverage(self, comparisons)
        view = self.channel_view
        channel_comparisons = (
            ()
            if view is None
            else (
                *(item for row in view.by_channel for item in (row.spend, row.impressions)),
                *(item for row in view.delivery for item in row.comparisons),
            )
        )
        require_change_blocks(
            (
                comparison.change
                for comparison in (*comparisons, *channel_comparisons)
                if comparison.current.value is not None
                and comparison.prior.value is not None
                and not comparison.withheld
            ),
            "campaign portfolio",
        )


def portfolio_row_operand_labels(rows: tuple[CampaignPortfolioRow, ...]) -> tuple[str, ...]:
    """Name each row's finding operand uniquely from its safe display signature.

    Two campaigns may share a display name across channels, and a campaign may have none;
    the portfolio contract guarantees only the full safe signature is unique. Operands carry
    the name qualified by channel when that is needed to disambiguate, and an unnamed
    campaign uses the same deterministic placeholder the table renders. One home for the
    rule, shared by the portfolio insight slots and the focus dialog's driver roster.
    """
    names = tuple(
        row.signature.effective_name
        if row.signature.effective_name is not None
        else f"Unnamed campaign {index + 1}"
        for index, row in enumerate(rows)
    )
    counts = Counter(names)
    return tuple(
        name if counts[name] == 1 else f"{name} · {row.signature.channel}"
        for name, row in zip(names, rows, strict=True)
    )


CAMPAIGN_SORT_FIELD_WORDS: Final[Mapping[str, str]] = {
    "spend": "ad spend",
    "attributed_roas": "attributed ROAS",
    "purchases": "attributed purchases",
    "ctr": "click-through rate",
    "name": "campaign name",
}
"""The table's sort words (its `label()`, lowercased, with "campaign name" for `name`)."""


def campaign_sort_label(sort: CampaignPortfolioSort) -> str:
    """Name how the served rows are ordered, in the words the table uses (design §4.5)."""
    words = CAMPAIGN_SORT_FIELD_WORDS[sort.field.value]
    if sort.field is CampaignPortfolioSortField.NAME:
        return f"{words}, {'A to Z' if sort.direction == 'asc' else 'Z to A'}"
    return f"{words}, {'lowest first' if sort.direction == 'asc' else 'highest first'}"


def campaign_population(artifact: CampaignPortfolioArtifact) -> int:
    """Return M, the boundless authorized population the totals reduce over (ADR-071)."""
    return artifact.total_count if artifact.pagination is None else artifact.pagination.total_count


def campaigns_in_view_sentence(artifact: CampaignPortfolioArtifact) -> str:
    """State which N of M campaigns a segment holds and how they were chosen (one home).

    "These are the N campaigns ranked X to Y by <sort label> in this window, of M in the
    portfolio", or "These are all N campaigns in the portfolio, ranked by <sort label>" when
    one segment holds every campaign.
    """
    count = len(artifact.rows)
    population = campaign_population(artifact)
    label = campaign_sort_label(artifact.sort)
    if count == population:
        return f"These are all {count} campaigns in the portfolio, ranked by {label}"
    pagination = artifact.pagination
    start = 0 if pagination is None else pagination.segment_index * pagination.segment_size
    return (
        f"These are the {count} campaigns ranked {start + 1} to {start + count} by {label} "
        f"in this window, of {population} in the portfolio"
    )


def portfolio_spend_change_split(
    artifact: CampaignPortfolioArtifact,
) -> tuple[Decimal, Decimal] | None:
    """Return the spend change in view and outside it (outside: total minus the in-view sum)."""
    total = next(
        (item for item in artifact.totals if item.metric is CampaignPortfolioMetric.SPEND), None
    )
    if total is None or total.absolute_change is None:
        return None
    in_view = sum(
        (
            Decimal(change)
            for row in artifact.rows
            for change in (row.comparison_for(CampaignPortfolioMetric.SPEND).absolute_change,)
            if change is not None
        ),
        Decimal(0),
    )
    return in_view, Decimal(total.absolute_change) - in_view


def build_campaign_portfolio(
    *,
    period: DateRange,
    sort: CampaignPortfolioSort,
    rows: tuple[CampaignPortfolioRow, ...],
    totals: tuple[CampaignPortfolioComparison, ...],
    trend: CampaignPortfolioTrend | None,
    evidence: tuple[CampaignListEvidence, ...],
    reason_codes: tuple[CampaignPortfolioInsufficiencyReason, ...] = (),
    authorized_count: int | None = None,
    evidence_status: EvidenceStatus | None = None,
    channel_filter: CampaignChannelFilter | None = None,
    channel_view: CampaignMultiChannelView | None = None,
) -> CampaignPortfolioArtifact:
    """Order and bound rows only after totals have been reduced over the authorized population.

    `evidence_status` is the complete portfolio's coverage block; an empty or insufficient
    portfolio shows no figure, so it serves none. `channel_filter` names the sibling this is
    (ADR-127); `channel_view` is a complete multi-channel sibling's per-channel content.
    """
    prior = _previous_period(period)
    total_count = len(rows) if authorized_count is None else authorized_count
    if total_count < len(rows):
        raise ValueError("campaign portfolio authorized count is smaller than returned rows.")
    if reason_codes:
        return CampaignPortfolioArtifact(
            CampaignPortfolioOutcome.INSUFFICIENT,
            period,
            prior,
            sort,
            (),
            0,
            None,
            (),
            None,
            evidence,
            reason_codes,
            channel_filter=channel_filter,
        )
    if not rows:
        return CampaignPortfolioArtifact(
            CampaignPortfolioOutcome.EMPTY,
            period,
            prior,
            sort,
            (),
            0,
            None,
            (),
            None,
            evidence,
            channel_filter=channel_filter,
        )
    if total_count != len(rows):
        raise ValueError(
            "campaign portfolio pagination requires the full measured population (ADR-071)."
        )
    ordered = order_campaign_portfolio_rows(rows, sort)
    pagination = CampaignPortfolioPagination(
        0,
        CAMPAIGN_PORTFOLIO_MAX_ROWS,
        -(-total_count // CAMPAIGN_PORTFOLIO_MAX_ROWS),
        total_count,
    )
    return CampaignPortfolioArtifact(
        CampaignPortfolioOutcome.COMPLETE,
        period,
        prior,
        sort,
        ordered,
        total_count,
        pagination,
        totals,
        trend,
        evidence,
        roster=campaign_portfolio_roster(rows),
        evidence_status=evidence_status,
        channel_filter=channel_filter,
        channel_view=channel_view,
    )


def _period_payload(period: DateRange) -> dict[str, str]:
    return {"start": period.start.isoformat(), "end": period.end.isoformat()}


def _units_payload(units: Units) -> dict[str, str | None]:
    return {"kind": units.kind.value, "currency_code": units.currency_code}


def _value_payload(value: CampaignPortfolioMetricValue) -> dict[str, object]:
    return {
        "basis": value.basis.value,
        "display_value": metric_display_value(value.units, value.value),
        "grain": list(value.grain.dimensions),
        "period": _period_payload(value.period),
        "units": _units_payload(value.units),
        "value": None if value.value is None else _number_payload(value.value),
    }


def _comparison_payload(value: CampaignPortfolioComparison) -> dict[str, object]:
    return {
        "absolute_change": (
            None if value.absolute_change is None else _number_payload(value.absolute_change)
        ),
        "current": _value_payload(value.current),
        "metric": value.metric.value,
        "prior": _value_payload(value.prior),
        **comparison_change_payload(value.percentage_change, value.change),
    }


def _capability_payload(value: CampaignSelectionCapability) -> dict[str, str]:
    return {
        "selection_reference": value.value,
        "expires_at": value.expires_at.astimezone(UTC).isoformat(),
    }


def _evidence_payload(value: CampaignListEvidence) -> dict[str, object]:
    return {
        "actual_coverage": [_period_payload(item) for item in value.actual_coverage],
        "coverage_holes": [_period_payload(item) for item in value.coverage_holes],
        "freshness_qualifiers": list(value.freshness_qualifiers),
        "requested_period": _period_payload(value.requested_period),
        "source": value.source,
    }


_ROSTER_ENTRY_FIELD_COUNT = 4


def _roster_payload(
    roster: tuple[CampaignPortfolioRosterEntry, ...],
) -> dict[str, object]:
    """Dictionary-encode repeated labels so the complete estate stays body-bounded."""
    channels = tuple(sorted({entry.channel for entry in roster}))
    marketplaces = tuple(sorted({entry.marketplace for entry in roster}))
    channel_index = {value: index for index, value in enumerate(channels)}
    marketplace_index = {value: index for index, value in enumerate(marketplaces)}
    return {
        "channels": list(channels),
        "entries": [
            [
                entry.name,
                channel_index[entry.channel],
                marketplace_index[entry.marketplace],
                entry.position,
            ]
            for entry in roster
        ],
        "marketplaces": list(marketplaces),
    }


CHANNEL_FILTER_KEY = "channel_filter"
CHANNEL_VIEW_KEY = "channel_view"


def _channel_filter_payload(value: CampaignChannelFilter) -> dict[str, object]:
    return {
        "options": [
            {"filter_id": option.filter_id, "label": option.label} for option in value.options
        ],
        "selected": value.selected,
    }


def _viewability_payload(cell: CampaignViewabilityCell | None) -> dict[str, object] | None:
    if cell is None:
        return None
    return {
        "absence_reason": cell.absence_reason,
        "comparison": None if cell.comparison is None else _comparison_payload(cell.comparison),
        "coverage_text": cell.coverage_text,
    }


def _channel_view_payload(view: CampaignMultiChannelView | None) -> dict[str, object] | None:
    if view is None:
        return None
    return {
        "by_channel": [
            {
                "channel": row.channel,
                "impressions": _comparison_payload(row.impressions),
                "label": row.label,
                "spend": _comparison_payload(row.spend),
            }
            for row in view.by_channel
        ],
        "delivery": [
            {
                "channel": row.channel,
                "clicks": _comparison_payload(row.clicks),
                "cpc": _comparison_payload(row.cpc),
                "cpm": _comparison_payload(row.cpm),
                "ctr": _comparison_payload(row.ctr),
                "label": row.label,
                "viewable_rate": _viewability_payload(row.viewable),
            }
            for row in view.delivery
        ],
        "delivery_bases": {metric.value: text for metric, text in DELIVERY_COLUMN_BASES.items()},
        "delivery_legend": list(view.legend),
        "insights_line": view.insights_line,
        "not_added": [
            {
                "label": tile.label,
                "metric": tile.metric.value,
                "reason": tile.reason,
                "value_text": tile.value_text,
            }
            for tile in view.not_added
        ],
        "scorecard_line": view.scorecard_line,
        "trend_line": view.trend_line,
        "viewability": view.viewability,
        "viewability_heading": DSP_VIEWABILITY_HEADING if view.viewability else None,
    }


def _trend_payload(trend: CampaignPortfolioTrend | None) -> dict[str, object] | None:
    if trend is None:
        return None
    if trend.spend_only:
        return {
            "basis": trend.basis.value,
            "grain": list(trend.grain.dimensions),
            "period": _period_payload(trend.period),
            "points": [
                {"date": point.day.isoformat(), "spend": _number_payload(point.spend)}
                for point in trend.points
            ],
            "units": {"spend": _units_payload(trend.spend_units)},
        }
    return {
        "basis": trend.basis.value,
        "grain": list(trend.grain.dimensions),
        "period": _period_payload(trend.period),
        "points": [
            {
                "date": point.day.isoformat(),
                "portfolio_roas_attributed_14d": (
                    None if point.portfolio_roas is None else _number_payload(point.portfolio_roas)
                ),
                "purchases": _number_payload(cast("CampaignNumber", point.purchases)),
                "spend": _number_payload(point.spend),
            }
            for point in trend.points
        ],
        "units": {
            "portfolio_roas_attributed_14d": _units_payload(cast("Units", trend.roas_units)),
            "purchases": _units_payload(cast("Units", trend.purchases_units)),
            "spend": _units_payload(trend.spend_units),
        },
    }


def campaign_portfolio_artifact_payload(artifact: CampaignPortfolioArtifact) -> dict[str, object]:
    """Encode only server-owned row arithmetic, sorting, and chart columns.

    Only the unfiltered in-process portfolio (the insight precompute's) has no channel filter;
    it encodes without the two channel keys, which the served-body decoder refuses.
    """
    channel_keys: dict[str, object] = (
        {}
        if artifact.channel_filter is None
        else {
            CHANNEL_FILTER_KEY: _channel_filter_payload(artifact.channel_filter),
            CHANNEL_VIEW_KEY: _channel_view_payload(artifact.channel_view),
        }
    )
    return {
        **channel_keys,
        "evidence": [_evidence_payload(item) for item in artifact.evidence],
        EVIDENCE_STATUS_KEY: evidence_status_payload(artifact.evidence_status),
        "insights": insight_slots_payload(artifact.insights),
        "kind": artifact.kind,
        "outcome": artifact.outcome.value,
        "period": _period_payload(artifact.period),
        "prior_period": _period_payload(artifact.prior_period),
        "reason_codes": [reason.value for reason in artifact.reason_codes],
        "rows": [
            {
                "capability": _capability_payload(row.capability),
                "comparisons": [_comparison_payload(item) for item in row.comparisons],
                "freshness_qualifiers": list(row.freshness_qualifiers),
                "signature": safe_campaign_display_signature_payload(row.signature),
            }
            for row in artifact.rows
        ],
        "sort": {"field": artifact.sort.field.value, "direction": artifact.sort.direction},
        "total_count": artifact.total_count,
        "totals": [_comparison_payload(item) for item in artifact.totals],
        "trend": _trend_payload(artifact.trend),
        "pagination": (
            None
            if artifact.pagination is None
            else {
                "segment_count": artifact.pagination.segment_count,
                "segment_index": artifact.pagination.segment_index,
                "segment_size": artifact.pagination.segment_size,
                "total_count": artifact.pagination.total_count,
            }
        ),
        "roster": (
            _roster_payload(artifact.roster)
            if artifact.roster_shards is None
            else {
                "revision": artifact.roster_shards.revision,
                "shards": artifact.roster_shards.shards,
            }
        ),
    }


def campaign_portfolio_segment_payload(
    artifact: CampaignPortfolioArtifact,
) -> dict[str, object]:
    """Encode only the selected ordered rows and pagination for an immutable sibling."""
    payload = campaign_portfolio_artifact_payload(artifact)
    return {
        "kind": "campaign_portfolio_segment",
        "pagination": payload["pagination"],
        "rows": payload["rows"],
        "sort": payload["sort"],
    }


def campaign_portfolio_segment_from_payload(
    value: object,
    parent: CampaignPortfolioArtifact,
) -> CampaignPortfolioArtifact:
    """Decode one compact sibling by joining it to its already-attested parent facts."""
    item = _object(
        value,
        frozenset({"kind", "pagination", "rows", "sort"}),
        "campaign portfolio sibling",
    )
    if item["kind"] != "campaign_portfolio_segment":
        raise ValueError("campaign portfolio sibling kind is unsupported.")
    combined = campaign_portfolio_artifact_payload(parent)
    combined.update(
        {
            "pagination": item["pagination"],
            "rows": item["rows"],
            "sort": item["sort"],
        }
    )
    return campaign_portfolio_artifact_from_payload(combined)


def _object(value: object, fields: frozenset[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} fields are not exact.")
    item = cast("dict[str, object]", value)
    if frozenset(item) != fields:
        raise ValueError(f"{label} fields are not exact.")
    return item


def _items(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    return cast("list[object]", value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonblank text.")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer.")
    return value


def _date(value: object, label: str) -> date:
    text = _text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} must be an ISO date.") from None
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must be canonical.")
    return parsed


def _period(value: object, label: str) -> DateRange:
    item = _object(value, frozenset({"start", "end"}), label)
    return DateRange(_date(item["start"], f"{label} start"), _date(item["end"], f"{label} end"))


def _periods(value: object, label: str) -> tuple[DateRange, ...]:
    periods = tuple(_period(item, label) for item in _items(value, label))
    if tuple(sorted(periods, key=lambda item: (item.start, item.end))) != periods:
        raise ValueError(f"{label} must be ordered.")
    return periods


def _signed_numeric(value: object, label: str) -> CampaignNumber:
    if isinstance(value, bool):
        raise ValueError(f"{label} is invalid.")
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be canonical Decimal text.")
    try:
        numeric = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{label} Decimal text is invalid.") from None
    if not numeric.is_finite() or str(numeric) != value:
        raise ValueError(f"{label} Decimal text is not canonical.")
    return numeric


def _numeric(value: object, label: str) -> CampaignNumber:
    numeric = _signed_numeric(value, label)
    if numeric < 0:
        raise ValueError(f"{label} cannot be negative.")
    return numeric


def _units(value: object, label: str) -> Units:
    item = _object(value, frozenset({"kind", "currency_code"}), label)
    try:
        kind = UnitKind(_text(item["kind"], f"{label} kind"))
    except ValueError:
        raise ValueError(f"{label} kind is unsupported.") from None
    currency = item["currency_code"]
    if currency is not None and not isinstance(currency, str):
        raise ValueError(f"{label} currency is invalid.")
    return Units(kind, currency)


def _grain(value: object, label: str) -> Grain:
    return Grain(tuple(_text(item, label) for item in _items(value, label)))


_METRIC_VALUE_FIELDS = frozenset({"basis", "grain", "period", "units", "value"})


def _metric_value(value: object, label: str) -> CampaignPortfolioMetricValue:
    # A retained pre-display body carries the five fields; a current one adds display_value.
    fields = (
        _METRIC_VALUE_FIELDS | {DISPLAY_VALUE_KEY}
        if carries_display_value(value)
        else _METRIC_VALUE_FIELDS
    )
    item = _object(value, fields, label)
    try:
        basis = MeasurementBasis(_text(item["basis"], f"{label} basis"))
    except ValueError:
        raise ValueError(f"{label} basis is unsupported.") from None
    result = CampaignPortfolioMetricValue(
        None if item["value"] is None else _numeric(item["value"], f"{label} value"),
        _units(item["units"], f"{label} units"),
        basis,
        _period(item["period"], f"{label} period"),
        _grain(item["grain"], f"{label} grain"),
    )
    if not canonical_matches(_value_payload(result), item):
        raise ValueError(f"{label} is not canonical.")
    return result


_COMPARISON_FIELDS = frozenset({"absolute_change", "current", "metric", "prior"})


def _comparison(value: object, label: str) -> CampaignPortfolioComparison:
    # Every comparison carries the change key; the retained form without one is refused.
    item = _object(value, comparison_fields(value, _COMPARISON_FIELDS), label)
    try:
        metric = CampaignPortfolioMetric(_text(item["metric"], f"{label} metric"))
    except ValueError:
        raise ValueError(f"{label} metric is unsupported.") from None
    current = _metric_value(item["current"], f"{label} current")
    prior = _metric_value(item["prior"], f"{label} prior")
    percentage = item.get(PERCENTAGE_KEY)
    if PERCENTAGE_KEY not in item and current.value is not None and prior.value is not None:
        # A current body states the change block in place of the percentage.
        percentage = percentage_change(current.value, prior.value)
    if percentage is not None and (not isinstance(percentage, str) or not percentage.strip()):
        raise ValueError(f"{label} percentage is invalid.")
    change_value = item.get(CHANGE_KEY)
    change = (
        None
        if current.value is None or prior.value is None
        else change_from_payload(
            change_value, ChangeOperands(current.value, prior.value, current.units), label
        )
    )
    if change is None and change_value is not None:
        raise ValueError(f"{label} carries a change for an undefined side.")
    # Two sides with no delta is a withheld change; the artifact holds it to the coverage block.
    withheld = (
        CHANGE_KEY in item
        and current.value is not None
        and prior.value is not None
        and item["absolute_change"] is None
    )
    return CampaignPortfolioComparison(
        metric,
        current,
        prior,
        None
        if item["absolute_change"] is None
        else _signed_numeric(item["absolute_change"], f"{label} delta"),
        None if withheld else percentage,
        change,
        withheld=withheld,
    )


def _capability(value: object) -> CampaignSelectionCapability:
    item = _object(
        value, frozenset({"selection_reference", "expires_at"}), "campaign portfolio capability"
    )
    try:
        expires = datetime.fromisoformat(_text(item["expires_at"], "campaign portfolio expiry"))
    except ValueError:
        raise ValueError("campaign portfolio expiry is invalid.") from None
    result = CampaignSelectionCapability(
        _text(item["selection_reference"], "campaign portfolio reference"), expires
    )
    if _capability_payload(result) != item:
        raise ValueError("campaign portfolio capability is not canonical.")
    return result


def _evidence(value: object) -> CampaignListEvidence:
    item = _object(
        value,
        frozenset(
            {
                "actual_coverage",
                "coverage_holes",
                "freshness_qualifiers",
                "requested_period",
                "source",
            }
        ),
        "campaign portfolio evidence",
    )
    qualifiers = tuple(
        _text(item, "campaign portfolio freshness")
        for item in _items(item["freshness_qualifiers"], "campaign portfolio freshness")
    )
    result = CampaignListEvidence(
        _text(item["source"], "campaign portfolio evidence source"),
        _period(item["requested_period"], "campaign portfolio evidence period"),
        _periods(item["actual_coverage"], "campaign portfolio evidence coverage"),
        _periods(item["coverage_holes"], "campaign portfolio evidence holes"),
        qualifiers,
    )
    if _evidence_payload(result) != item:
        raise ValueError("campaign portfolio evidence is not canonical.")
    return result


def _campaign_roster_from_payload(value: object) -> tuple[CampaignPortfolioRosterEntry, ...]:
    """Decode current compact rosters and retained format-3 row rosters during rollout."""
    if isinstance(value, list):
        legacy: list[CampaignPortfolioRosterEntry] = []
        for raw in cast("list[object]", value):
            entry = _object(
                raw,
                frozenset({"channel", "marketplace", "name", "position"}),
                "campaign portfolio roster entry",
            )
            name = entry["name"]
            legacy.append(
                CampaignPortfolioRosterEntry(
                    None if name is None else _text(name, "campaign portfolio roster name"),
                    _text(entry["channel"], "campaign portfolio roster channel"),
                    _text(entry["marketplace"], "campaign portfolio roster marketplace"),
                    _integer(entry["position"], "campaign portfolio roster position"),
                )
            )
        return tuple(legacy)
    roster_item = _object(
        value,
        frozenset({"channels", "entries", "marketplaces"}),
        "campaign portfolio roster",
    )
    channels = tuple(
        _text(item, "campaign portfolio roster channel")
        for item in _items(roster_item["channels"], "campaign portfolio roster channels")
    )
    marketplaces = tuple(
        _text(item, "campaign portfolio roster marketplace")
        for item in _items(roster_item["marketplaces"], "campaign portfolio roster marketplaces")
    )
    roster: list[CampaignPortfolioRosterEntry] = []
    for raw in _items(roster_item["entries"], "campaign portfolio roster entries"):
        entry = _items(raw, "campaign portfolio roster entry")
        if len(entry) != _ROSTER_ENTRY_FIELD_COUNT:
            raise ValueError("campaign portfolio roster entry fields are not exact.")
        name, raw_channel, raw_marketplace, raw_position = entry
        channel_index = _integer(raw_channel, "campaign portfolio roster channel index")
        marketplace_index = _integer(raw_marketplace, "campaign portfolio roster marketplace index")
        if channel_index >= len(channels) or marketplace_index >= len(marketplaces):
            raise ValueError("campaign portfolio roster label index is out of range.")
        roster.append(
            CampaignPortfolioRosterEntry(
                None if name is None else _text(name, "campaign portfolio roster name"),
                channels[channel_index],
                marketplaces[marketplace_index],
                _integer(raw_position, "campaign portfolio roster position"),
            )
        )
    return tuple(roster)


def _roster_shards_from_payload(value: object) -> CampaignPortfolioRosterShards:
    item = _object(value, frozenset({"revision", "shards"}), "campaign portfolio roster shards")
    return CampaignPortfolioRosterShards(
        _text(item["revision"], "campaign portfolio roster revision"),
        _integer(item["shards"], "campaign portfolio roster shards"),
    )


def _has_keys(value: object, keys: frozenset[str], *, exact: bool = False) -> bool:
    """Whether a decoded object carries these keys (exactly these, with `exact`)."""
    if not isinstance(value, dict):
        return False
    present = frozenset(cast("dict[str, object]", value))
    return present == keys if exact else keys <= present


def _channel_filter_from_payload(value: object) -> CampaignChannelFilter:
    item = _object(value, frozenset({"options", "selected"}), "campaign channel filter")
    options = tuple(
        CampaignChannelFilterOption(
            _text(option["filter_id"], "campaign channel filter id"),
            _text(option["label"], "campaign channel filter label"),
        )
        for option in (
            _object(raw, frozenset({"filter_id", "label"}), "campaign channel filter option")
            for raw in _items(item["options"], "campaign channel filter options")
        )
    )
    return CampaignChannelFilter(_text(item["selected"], "campaign channel filter"), options)


def _optional_text_field(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _channel_view_from_payload(value: object) -> CampaignMultiChannelView | None:
    if value is None:
        return None
    item = _object(
        value,
        frozenset(
            {
                "by_channel",
                "delivery",
                "delivery_bases",
                "delivery_legend",
                "insights_line",
                "not_added",
                "scorecard_line",
                "trend_line",
                "viewability",
                "viewability_heading",
            }
        ),
        "campaign channel view",
    )
    by_channel = tuple(
        CampaignChannelTotalsRow(
            _text(row["channel"], "By channel channel"),
            _text(row["label"], "By channel label"),
            _comparison(row["spend"], "By channel spend"),
            _comparison(row["impressions"], "By channel impressions"),
        )
        for row in (
            _object(raw, frozenset({"channel", "impressions", "label", "spend"}), "By channel row")
            for raw in _items(item["by_channel"], "By channel rows")
        )
    )
    delivery: list[CampaignChannelDeliveryRow] = []
    for raw in _items(item["delivery"], "delivery rows"):
        row = _object(
            raw,
            frozenset({"channel", "clicks", "cpc", "cpm", "ctr", "label", "viewable_rate"}),
            "delivery row",
        )
        cell_value = row["viewable_rate"]
        cell: CampaignViewabilityCell | None = None
        if cell_value is not None:
            cell_item = _object(
                cell_value,
                frozenset({"absence_reason", "comparison", "coverage_text"}),
                "viewability cell",
            )
            cell = CampaignViewabilityCell(
                None
                if cell_item["comparison"] is None
                else _comparison(cell_item["comparison"], "viewable rate"),
                _optional_text_field(cell_item["coverage_text"], "viewability coverage"),
                _optional_text_field(cell_item["absence_reason"], "viewability absence"),
            )
        delivery.append(
            CampaignChannelDeliveryRow(
                _text(row["channel"], "delivery channel"),
                _text(row["label"], "delivery label"),
                _comparison(row["clicks"], "delivery clicks"),
                _comparison(row["ctr"], "delivery CTR"),
                _comparison(row["cpc"], "delivery CPC"),
                _comparison(row["cpm"], "delivery CPM"),
                cell,
            )
        )
    viewability = item["viewability"]
    if not isinstance(viewability, bool):
        raise ValueError("campaign channel view viewability must be a boolean.")
    return CampaignMultiChannelView(
        by_channel,
        tuple(delivery),
        viewability,
        tuple(
            CampaignNotAddedTile(
                CampaignPortfolioMetric(_text(tile["metric"], "not-added metric")),
                _text(tile["label"], "not-added label"),
                _text(tile["value_text"], "not-added value"),
                _text(tile["reason"], "not-added reason"),
            )
            for tile in (
                _object(raw, frozenset({"label", "metric", "reason", "value_text"}), "not added")
                for raw in _items(item["not_added"], "not-added tiles")
            )
        ),
        _text(item["insights_line"], "insights line"),
        _text(item["trend_line"], "trend line"),
        _text(item["scorecard_line"], "Scorecard line"),
    )


def campaign_portfolio_artifact_from_payload(
    value: object,
) -> CampaignPortfolioArtifact:
    """Decode and re-attest the exact V2 campaign portfolio parent contract (render format 11).

    Every body carries `evidence_status` (null on an empty or insufficient portfolio),
    `channel_filter` and `channel_view`. The retained reads of format-6 comparisons without a
    change key, format-7 bodies without the coverage key and format-10 bodies without a channel
    filter retired in the integration change: the API, a full re-bake and the browser deploy
    together (owner revision, 2026-10-08), so each is refused like any other contract error.
    """
    item = _object(
        value,
        frozenset(
            {
                EVIDENCE_STATUS_KEY,
                CHANNEL_FILTER_KEY,
                CHANNEL_VIEW_KEY,
                "evidence",
                "insights",
                "kind",
                "outcome",
                "period",
                "prior_period",
                "reason_codes",
                "rows",
                "pagination",
                "roster",
                "sort",
                "total_count",
                "totals",
                "trend",
            }
        ),
        "campaign portfolio artifact",
    )
    # A body is wholly pre-display or wholly current; a mixed one is refused before decoding.
    display_value_mode(item)
    try:
        outcome = CampaignPortfolioOutcome(_text(item["outcome"], "campaign portfolio outcome"))
        sort_item = _object(
            item["sort"], frozenset({"field", "direction"}), "campaign portfolio sort"
        )
        sort = CampaignPortfolioSort(
            CampaignPortfolioSortField(_text(sort_item["field"], "campaign portfolio sort field")),
            _text(sort_item["direction"], "campaign portfolio sort direction"),
        )
    except ValueError:
        raise ValueError("campaign portfolio enum is unsupported.") from None
    rows: list[CampaignPortfolioRow] = []
    for raw in _items(item["rows"], "campaign portfolio rows"):
        # A row carries no spend spark; the format-10 sparked row is refused by the exact keys.
        row = _object(
            raw,
            frozenset({"capability", "comparisons", "freshness_qualifiers", "signature"}),
            "campaign portfolio row",
        )
        rows.append(
            CampaignPortfolioRow(
                safe_campaign_display_signature_from_payload(row["signature"]),
                _capability(row["capability"]),
                tuple(
                    _comparison(entry, "campaign portfolio row comparison")
                    for entry in _items(row["comparisons"], "campaign portfolio comparisons")
                ),
                tuple(
                    _text(entry, "campaign portfolio row freshness")
                    for entry in _items(
                        row["freshness_qualifiers"], "campaign portfolio row freshness"
                    )
                ),
            )
        )
    trend_value = item["trend"]
    trend: CampaignPortfolioTrend | None = None
    if trend_value is not None:
        trend_item = _object(
            trend_value,
            frozenset({"basis", "grain", "period", "points", "units"}),
            "campaign portfolio trend",
        )
        raw_units = trend_item["units"]
        spend_only = _has_keys(
            raw_units, frozenset({CampaignPortfolioMetric.SPEND.value}), exact=True
        )
        units_item = _object(
            raw_units,
            frozenset({CampaignPortfolioMetric.SPEND.value})
            if spend_only
            else frozenset(metric.value for metric in _TREND_METRICS),
            "campaign portfolio trend units",
        )
        try:
            trend_basis = MeasurementBasis(
                _text(trend_item["basis"], "campaign portfolio trend basis")
            )
        except ValueError:
            raise ValueError("campaign portfolio trend basis is unsupported.") from None
        point_fields = (
            frozenset({"date", "spend"})
            if spend_only
            else frozenset({"date", "spend", "portfolio_roas_attributed_14d", "purchases"})
        )
        points: list[CampaignPortfolioTrendPoint] = []
        for raw in _items(trend_item["points"], "campaign portfolio trend points"):
            point = _object(raw, point_fields, "campaign portfolio trend point")
            points.append(
                CampaignPortfolioTrendPoint(
                    _date(point["date"], "campaign portfolio trend date"),
                    _numeric(point["spend"], "campaign portfolio trend spend"),
                    None
                    if spend_only or point["portfolio_roas_attributed_14d"] is None
                    else _numeric(
                        point["portfolio_roas_attributed_14d"], "campaign portfolio trend ROAS"
                    ),
                    None
                    if spend_only
                    else _numeric(point["purchases"], "campaign portfolio trend purchases"),
                )
            )
        trend = CampaignPortfolioTrend(
            _period(trend_item["period"], "campaign portfolio trend period"),
            tuple(points),
            _units(units_item["spend"], "campaign portfolio trend spend units"),
            None
            if spend_only
            else _units(
                units_item["portfolio_roas_attributed_14d"], "campaign portfolio trend ROAS units"
            ),
            None
            if spend_only
            else _units(units_item["purchases"], "campaign portfolio trend purchases units"),
            trend_basis,
            _grain(trend_item["grain"], "campaign portfolio trend grain"),
        )
    pagination_value = item["pagination"]
    pagination = None
    if pagination_value is not None:
        pagination_item = _object(
            pagination_value,
            frozenset({"segment_count", "segment_index", "segment_size", "total_count"}),
            "campaign portfolio pagination",
        )
        pagination = CampaignPortfolioPagination(
            _integer(pagination_item["segment_index"], "campaign portfolio segment index"),
            _integer(pagination_item["segment_size"], "campaign portfolio segment size"),
            _integer(pagination_item["segment_count"], "campaign portfolio segment count"),
            _integer(pagination_item["total_count"], "campaign portfolio pagination total"),
        )
    shards = (
        _roster_shards_from_payload(item["roster"])
        if _has_keys(item["roster"], frozenset({"revision", "shards"}), exact=True)
        else None
    )
    roster = () if shards is not None else _campaign_roster_from_payload(item["roster"])
    result = CampaignPortfolioArtifact(
        outcome,
        _period(item["period"], "campaign portfolio period"),
        _period(item["prior_period"], "campaign portfolio prior period"),
        sort,
        tuple(rows),
        _integer(item["total_count"], "campaign portfolio total count"),
        pagination,
        tuple(
            _comparison(entry, "campaign portfolio total")
            for entry in _items(item["totals"], "campaign portfolio totals")
        ),
        trend,
        tuple(
            _evidence(entry) for entry in _items(item["evidence"], "campaign portfolio evidence")
        ),
        tuple(
            CampaignPortfolioInsufficiencyReason(_text(entry, "campaign portfolio reason"))
            for entry in _items(item["reason_codes"], "campaign portfolio reasons")
        ),
        _text(item["kind"], "campaign portfolio kind"),
        insight_slots_from_payload(
            item["insights"],
            expected=(
                InsightSlotId.CONTRIBUTION,
                InsightSlotId.DIVERGENCE,
                InsightSlotId.EXPOSURE,
                InsightSlotId.MOVERS,
            ),
        ),
        tuple(roster),
        evidence_status_from_payload(item[EVIDENCE_STATUS_KEY]),
        _channel_filter_from_payload(item[CHANNEL_FILTER_KEY]),
        _channel_view_from_payload(item[CHANNEL_VIEW_KEY]),
        shards,
    )
    # ADR-119 decision 4: the block is null only on an empty or insufficient portfolio.
    if result.outcome is CampaignPortfolioOutcome.COMPLETE and result.evidence_status is None:
        raise ValueError("a complete campaign portfolio body carries its coverage block.")
    canonical = campaign_portfolio_artifact_payload(result)
    if isinstance(item["roster"], list):
        canonical["roster"] = item["roster"]
    if not canonical_matches(canonical, item):
        raise ValueError("campaign portfolio payload is not canonical.")
    return result
