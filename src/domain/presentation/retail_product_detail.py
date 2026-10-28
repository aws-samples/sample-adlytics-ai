# SPDX-License-Identifier: MIT-0
# ruff: noqa: D101, PLR0913, PLR0917
"""Retail health product detail: the baked ``RetailProductDetail`` v1 child (ADR-125).

One child body per served Retail health row is baked with its parent, in the same revision, so
every product page shows the parent's window and its additive figures add up to the parent's
tiles. The safe display signature, the typed reasons, the daily points and the boundary text
moved here from the retired request-time Product Evidence module.

From render format 12 the same bake also writes one ``RetailProductAds`` v1 part: each served
row's ad credit and conversion, projected from that row's child, so Products and ads reads one
object instead of one child per row.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import cast

from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.campaigns import EffectiveNameStatus
from domain.presentation.change import (
    Change,
    ChangeDirection,
    ChangeOperands,
    ChangeSemantics,
    ChangeSentiment,
    ChangeUnit,
    NotMeaningful,
    build_change,
    change_from_payload,
    change_payload,
    check_change,
)
from domain.presentation.currency_display import currency_display_text, metric_display_value
from domain.presentation.evidence_status import (
    EVIDENCE_STATUS_KEY,
    EvidenceStatus,
    evidence_status_from_payload,
    evidence_status_payload,
    require_entries,
    withheld_slots,
)
from domain.presentation.insights import (
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    insight_slots_from_payload,
    insight_slots_payload,
)
from domain.presentation.retail_health import (
    AMC_RETAIL_PURCHASES_KEY,
    RETAIL_HEALTH_ROW_GRAIN,
    RETURN_REASONS_KEY,
    RetailComparison,
    RetailDistributorView,
    RetailMetricValue,
    RetailTypedAbsence,
    check_retail_fixed_statuses,
    retail_comparison_from_payload,
    retail_comparison_payload,
    retail_fixed_statuses_payload,
    retail_health_expected_totals,
    retail_point_from_payload,
    retail_point_payload,
    retail_value_from_payload,
    retail_value_payload,
)
from domain.shared_kernel.values import DateRange, Grain

__all__ = [
    "RETAIL_PRODUCT_ADS_KIND",
    "RETAIL_PRODUCT_AD_METRICS",
    "RETAIL_PRODUCT_BOUNDARIES",
    "RETAIL_PRODUCT_DETAIL_ARTIFACT_TYPE",
    "RETAIL_PRODUCT_DETAIL_ARTIFACT_VERSION",
    "RETAIL_PRODUCT_SERIES_GRAIN",
    "RetailAdAttributed",
    "RetailAdChannelRow",
    "RetailAdComparison",
    "RetailAdValue",
    "RetailDetailReason",
    "RetailProductAdCell",
    "RetailProductAdsArtifact",
    "RetailProductAdsRow",
    "RetailProductDetailArtifact",
    "RetailProductDetailOutcome",
    "RetailProductIdentity",
    "RetailProductInsight",
    "RetailProductInsightStat",
    "RetailSalesPoint",
    "RetailTrafficPoint",
    "SafeProductDisplaySignature",
    "retail_product_ads",
    "retail_product_ads_from_payload",
    "retail_product_ads_payload",
    "retail_product_detail_from_payload",
    "retail_product_detail_payload",
]

RETAIL_PRODUCT_DETAIL_ARTIFACT_TYPE = "RetailProductDetail"
RETAIL_PRODUCT_DETAIL_ARTIFACT_VERSION = 1
RETAIL_PRODUCT_SERIES_GRAIN = Grain(("date", "product", "marketplace"))
RETAIL_PRODUCT_AD_METRICS = ("purchases", "attributed_revenue_14d")
"""Ad-attributed purchases and revenue, per channel, never totalled (ia §7.3a item 3)."""
RETAIL_PRODUCT_AD_BASIS = (
    "Amazon-attributed · 14-day · conversion date · excludes brand halo · Amazon Marketing Cloud"
)
RETAIL_PRODUCT_BOUNDARIES = (
    "Shipped retail revenue, shipped units, and glance views are Vendor Central product evidence.",
    "Retail and traffic co-movement is descriptive and does not establish campaign attribution "
    "or causality.",
    "This page does not provide product ROAS, inventory, brand, or parent-product inference.",
)
RETAIL_PRODUCT_INSIGHT_PROVENANCE = (
    "Ranked by the server from Retail health's figures for all products. "
    "The figures below are this product's own."
)
"""The movers card's title was ranked over every product, not this one (render-more RA12-5)."""
_KIND = "retail_product_detail"


class RetailDetailReason(StrEnum):
    """Why a part of product detail is a typed absence; the server writes the sentence."""

    MISSING_CURRENT_PUBLICATION = "missing_current_publication"
    MISSING_COMPARATOR_PUBLICATION = "missing_comparator_publication"
    EMPTY_COVERED = "empty_covered"
    SUPPRESSED = "suppressed"
    TRUNCATED = "truncated"
    COVERAGE_HOLE = "coverage_hole"
    INCOMPATIBLE_SCOPE = "incompatible_scope"
    ONE_SIDED_COMPARISON = "one_sided_comparison"
    SERIES_RECONCILIATION_FAILED = "series_reconciliation_failed"


class RetailProductDetailOutcome(StrEnum):
    SELECTED = "selected"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True, slots=True)
class SafeProductDisplaySignature:
    """Only browser-safe product display attributes; canonical identity stays server-side."""

    effective_name: str | None
    effective_name_status: EffectiveNameStatus
    marketplace: str

    def __post_init__(self) -> None:
        if not self.marketplace.strip():
            raise ValueError("product marketplace display label is required.")
        if self.effective_name_status is EffectiveNameStatus.AVAILABLE and not self.effective_name:
            raise ValueError("available product display signature requires a safe name.")
        if self.effective_name_status is not EffectiveNameStatus.AVAILABLE and self.effective_name:
            raise ValueError("unavailable product display signature cannot invent a name.")


@dataclass(frozen=True, slots=True)
class RetailProductIdentity:
    """The display identity the parent row shows: title, child ASIN and parent ASIN."""

    title: str
    child_asin: str
    parent_asin: str

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.title, self.child_asin, self.parent_asin)):
            raise ValueError("retail product identity is incomplete.")


def _day_after(period: DateRange, offset: int) -> date:
    return period.start + timedelta(days=offset)


@dataclass(frozen=True, slots=True)
class RetailSalesPoint:
    """One day's shipped revenue for the product, beside the same offset in the prior window."""

    day: date
    current: RetailMetricValue
    prior: RetailMetricValue


@dataclass(frozen=True, slots=True)
class RetailTrafficPoint:
    """One day's glance views for the product, beside the same offset in the prior window."""

    day: date
    current: RetailMetricValue
    prior: RetailMetricValue


def _check_series(
    points: tuple[RetailSalesPoint, ...] | tuple[RetailTrafficPoint, ...],
    period: DateRange,
    prior: DateRange,
    kind: UnitKind,
) -> None:
    if tuple(point.day for point in points) != tuple(
        _day_after(period, offset) for offset in range(period.days)
    ):
        raise ValueError("product series must cover the window exactly.")
    for offset, point in enumerate(points):
        prior_day = _day_after(prior, offset)
        if (
            point.current.period != DateRange(point.day, point.day)
            or point.prior.period != DateRange(prior_day, prior_day)
            or point.current.grain != RETAIL_PRODUCT_SERIES_GRAIN
            or point.prior.grain != RETAIL_PRODUCT_SERIES_GRAIN
            or point.current.units.kind is not kind
            or point.current.units != point.prior.units
        ):
            raise ValueError("product series point is not aligned.")


@dataclass(frozen=True, slots=True)
class RetailAdValue:
    """One ad-attributed figure (Amazon Marketing Cloud), never added to retail figures."""

    value: int | Decimal
    units: Units
    period: DateRange

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or Decimal(self.value) < 0:
            raise ValueError("an ad-attributed figure is a non-negative number.")


@dataclass(frozen=True, slots=True)
class RetailAdComparison:
    metric: str
    current: RetailAdValue
    prior: RetailAdValue
    change: Change

    def __post_init__(self) -> None:
        if self.metric not in RETAIL_PRODUCT_AD_METRICS or self.current.units != self.prior.units:
            raise ValueError("ad-attributed comparison is invalid.")
        check_change(
            self.change,
            ChangeOperands(self.current.value, self.prior.value, self.current.units),
        )


def retail_ad_comparison(
    metric: str, current: RetailAdValue, prior: RetailAdValue, semantics: ChangeSemantics
) -> RetailAdComparison:
    """Compare one channel's ad-attributed figure with the change its dictionary entry governs."""
    return RetailAdComparison(
        metric,
        current,
        prior,
        build_change(
            ChangeOperands(current.value, prior.value, current.units),
            semantics.for_metric(metric),
        ),
    )


@dataclass(frozen=True, slots=True)
class RetailAdChannelRow:
    """One channel's attributed purchases and revenue for the product, each with its change."""

    channel: str
    label: str
    purchases: RetailAdComparison
    revenue: RetailAdComparison

    def __post_init__(self) -> None:
        if not self.channel.strip() or not self.label.strip():
            raise ValueError("an ad-attributed row names its channel.")
        if (self.purchases.metric, self.revenue.metric) != RETAIL_PRODUCT_AD_METRICS:
            raise ValueError("an ad-attributed row carries purchases then revenue.")


@dataclass(frozen=True, slots=True)
class RetailAdAttributed:
    """The ad-attributed section: per-channel rows, or a typed absence with its reason.

    There is no total row and nothing here is added to shipped revenue: one purchase can be
    credited to more than one channel, and the two measurement systems differ (§8.5).
    """

    rows: tuple[RetailAdChannelRow, ...]
    reason_code: RetailDetailReason | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        absent = self.reason_code is not None
        if absent != (self.reason is not None) or (absent and self.rows):
            raise ValueError("an ad-attributed absence has a reason and no rows.")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("an ad-attributed absence reason is nonblank.")
        if not absent and not self.rows:
            raise ValueError("an available ad-attributed section has at least one channel.")
        channels = tuple(row.channel for row in self.rows)
        if len(set(channels)) != len(channels):
            raise ValueError("ad-attributed channels are unique.")


@dataclass(frozen=True, slots=True)
class RetailProductInsightStat:
    """One dialog stat the server writes for this product; its label names the product."""

    label: str
    text: str
    change: Change | None = None

    def __post_init__(self) -> None:
        if not self.label.strip() or not self.text.strip():
            raise ValueError("a product insight stat has a label and text.")


@dataclass(frozen=True, slots=True)
class RetailProductInsight:
    """A parent insight that names the product, shown as context; Retail health is its home."""

    slot: InsightSlot
    stats: tuple[RetailProductInsightStat, ...]
    provenance: str

    def __post_init__(self) -> None:
        if self.slot.state is not InsightSlotState.FILLED or self.slot.next_step is not None:
            raise ValueError("a product page shows a filled parent card without its step.")
        if not self.stats or not self.provenance.strip():
            raise ValueError("a product insight carries this product's stats and provenance.")


@dataclass(frozen=True, slots=True)
class RetailProductDetailArtifact:
    """One product's figures for the parent's exact window and revision."""

    outcome: RetailProductDetailOutcome
    period: DateRange
    prior_period: DateRange
    product: RetailProductIdentity
    distributor_view: RetailDistributorView
    totals: tuple[RetailComparison, ...]
    absences: tuple[RetailTypedAbsence, ...]
    sales_daily: tuple[RetailSalesPoint, ...]
    traffic_daily: tuple[RetailTrafficPoint, ...]
    ad_attributed: RetailAdAttributed | None
    insights: tuple[RetailProductInsight, ...]
    no_insight_line: str | None
    evidence_status: EvidenceStatus | None
    boundaries: tuple[str, ...] = RETAIL_PRODUCT_BOUNDARIES

    def __post_init__(self) -> None:  # noqa: PLR0912
        if self.boundaries != RETAIL_PRODUCT_BOUNDARIES:
            raise ValueError("product detail boundaries are fixed policy.")
        if self.prior_period != DateRange(
            self.period.start - timedelta(days=self.period.days),
            self.period.start - timedelta(days=1),
        ):
            raise ValueError("product detail compares the equal preceding window.")
        absent = frozenset(item.metric for item in self.absences)
        if len(absent) != len(self.absences):
            raise ValueError("product typed absences are unique per metric.")
        if self.outcome is RetailProductDetailOutcome.INSUFFICIENT:
            if (
                self.totals
                or self.sales_daily
                or self.traffic_daily
                or self.ad_attributed is not None
                or self.insights
                or self.evidence_status is not None
            ):
                raise ValueError("insufficient product detail carries no figures.")
            return
        if tuple(item.metric for item in self.totals) != retail_health_expected_totals(
            self.distributor_view, absent
        ):
            raise ValueError("product detail serves the Retail health tile roster.")
        for item in self.totals:
            if (
                item.current.grain != RETAIL_HEALTH_ROW_GRAIN
                or item.current.period != self.period
                or item.prior.period != self.prior_period
            ):
                raise ValueError("product detail figures are the product's for the window.")
        _check_series(self.sales_daily, self.period, self.prior_period, UnitKind.CURRENCY)
        _check_series(self.traffic_daily, self.period, self.prior_period, UnitKind.COUNT)
        if self.ad_attributed is None:
            raise ValueError("selected product detail states its ad-attributed section.")
        for row in self.ad_attributed.rows:
            for item in (row.purchases, row.revenue):
                if item.current.period != self.period or item.prior.period != self.prior_period:
                    raise ValueError("ad-attributed sales use the page's window.")
        if bool(self.insights) == (self.no_insight_line is not None):
            raise ValueError("product insights or the no-insight line, never both or neither.")
        if self.no_insight_line is not None and not self.no_insight_line.strip():
            raise ValueError("the no-insight line is nonblank.")
        status = self.evidence_status
        if status is None:
            raise ValueError("selected product detail serves its coverage block.")
        require_entries(
            status,
            tuple((item.metric, "current") for item in self.totals),
            self.period,
            label="Retail product detail",
        )
        withheld = withheld_slots(status)
        for item in self.totals:
            if item.withheld != (item.metric in withheld) or (
                not item.withheld and item.change is None
            ):
                raise ValueError("a product change is withheld exactly where coverage says.")


def _period_payload(period: DateRange) -> dict[str, str]:
    return {"start": period.start.isoformat(), "end": period.end.isoformat()}


def _ad_value_payload(value: RetailAdValue) -> dict[str, object]:
    return {
        "basis": "ad_attributed",
        "display_value": metric_display_value(value.units, value.value),
        "period": _period_payload(value.period),
        "units": {"kind": value.units.kind.value, "currency_code": value.units.currency_code},
        "value": value.value if isinstance(value.value, int) else str(value.value),
    }


def _ad_comparison_payload(value: RetailAdComparison) -> dict[str, object]:
    return {
        "metric": value.metric,
        "current": _ad_value_payload(value.current),
        "prior": _ad_value_payload(value.prior),
        "change": change_payload(value.change),
    }


def _series_payload(
    points: tuple[RetailSalesPoint, ...] | tuple[RetailTrafficPoint, ...],
) -> list[dict[str, object]]:
    return [
        {
            "date": point.day.isoformat(),
            "current": retail_point_payload(point.current),
            "prior": retail_point_payload(point.prior),
        }
        for point in points
    ]


def retail_product_detail_payload(artifact: RetailProductDetailArtifact) -> dict[str, object]:
    """Encode one child body; it names the product by display identity, never by key."""
    ads = artifact.ad_attributed
    return {
        "kind": _KIND,
        "outcome": artifact.outcome.value,
        "period": _period_payload(artifact.period),
        "prior_period": _period_payload(artifact.prior_period),
        "product": {
            "title": artifact.product.title,
            "child_asin": artifact.product.child_asin,
            "parent_asin": artifact.product.parent_asin,
        },
        "distributor_view": artifact.distributor_view.value,
        "totals": [retail_comparison_payload(item) for item in artifact.totals],
        "absences": [{"metric": item.metric, "reason": item.reason} for item in artifact.absences],
        "sales_daily": _series_payload(artifact.sales_daily),
        "traffic_daily": _series_payload(artifact.traffic_daily),
        "ad_attributed": None
        if ads is None
        else {
            "basis": RETAIL_PRODUCT_AD_BASIS,
            "rows": [
                {
                    "channel": row.channel,
                    "label": row.label,
                    "purchases": _ad_comparison_payload(row.purchases),
                    "revenue": _ad_comparison_payload(row.revenue),
                }
                for row in ads.rows
            ],
            "reason_code": None if ads.reason_code is None else ads.reason_code.value,
            "reason": ads.reason,
        },
        "insights": [
            {
                "slot": insight_slots_payload((item.slot,))[0],
                "stats": [
                    {"label": stat.label, "text": stat.text, "change": change_payload(stat.change)}
                    for stat in item.stats
                ],
                "provenance": item.provenance,
            }
            for item in artifact.insights
        ],
        "no_insight_line": artifact.no_insight_line,
        "boundaries": list(artifact.boundaries),
        EVIDENCE_STATUS_KEY: evidence_status_payload(artifact.evidence_status),
        **retail_fixed_statuses_payload(),
    }


# Decoder ---------------------------------------------------------------------------------------


def _object(value: object, keys: frozenset[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(cast("dict[str, object]", value)) != keys:
        raise ValueError(f"{label} fields are not exact.")
    return cast("dict[str, object]", value)


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    return cast("list[object]", value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonblank text.")
    return value


def _period(value: object, label: str) -> DateRange:
    item = _object(value, frozenset({"start", "end"}), label)
    start, end = _text(item["start"], label), _text(item["end"], label)
    parsed = DateRange(date.fromisoformat(start), date.fromisoformat(end))
    if parsed.start.isoformat() != start or parsed.end.isoformat() != end:
        raise ValueError(f"{label} must be canonical.")
    return parsed


def _units(value: object, label: str) -> Units:
    item = _object(value, frozenset({"kind", "currency_code"}), label)
    code = item["currency_code"]
    if code is not None and not isinstance(code, str):
        raise ValueError(f"{label} currency is invalid.")
    return Units(UnitKind(_text(item["kind"], label)), code)


def _ad_value(value: object, label: str) -> RetailAdValue:
    item = _object(value, frozenset({"basis", "display_value", "period", "units", "value"}), label)
    raw = item["value"]
    if item["basis"] != "ad_attributed" or isinstance(raw, bool):
        raise ValueError(f"{label} basis is unsupported.")
    if isinstance(raw, int):
        number: int | Decimal = raw
    elif isinstance(raw, str):
        number = Decimal(raw)
    else:
        raise ValueError(f"{label} value must be a canonical number.")
    return RetailAdValue(number, _units(item["units"], label), _period(item["period"], label))


def _ad_comparison(value: object, label: str) -> RetailAdComparison:
    item = _object(value, frozenset({"metric", "current", "prior", "change"}), label)
    current = _ad_value(item["current"], f"{label} current")
    prior = _ad_value(item["prior"], f"{label} prior")
    change = change_from_payload(
        item["change"], ChangeOperands(current.value, prior.value, current.units), label
    )
    if change is None:
        raise ValueError(f"{label} serves its change.")
    return RetailAdComparison(_text(item["metric"], label), current, prior, change)


def _ad_attributed(value: object) -> RetailAdAttributed | None:
    if value is None:
        return None
    item = _object(value, frozenset({"basis", "rows", "reason_code", "reason"}), "ad section")
    if item["basis"] != RETAIL_PRODUCT_AD_BASIS:
        raise ValueError("ad-attributed basis is not the server's.")
    rows = tuple(
        RetailAdChannelRow(
            _text(row["channel"], "ad channel"),
            _text(row["label"], "ad channel label"),
            _ad_comparison(row["purchases"], "ad purchases"),
            _ad_comparison(row["revenue"], "ad revenue"),
        )
        for row in (
            _object(raw, frozenset({"channel", "label", "purchases", "revenue"}), "ad row")
            for raw in _list(item["rows"], "ad rows")
        )
    )
    code = item["reason_code"]
    return RetailAdAttributed(
        rows,
        None if code is None else RetailDetailReason(_text(code, "ad reason code")),
        None if item["reason"] is None else _text(item["reason"], "ad reason"),
    )


def _series[P: (RetailSalesPoint, RetailTrafficPoint)](
    value: object, point: type[P], label: str
) -> tuple[P, ...]:
    result: list[P] = []
    for raw in _list(value, label):
        item = _object(raw, frozenset({"date", "current", "prior"}), label)
        result.append(
            point(
                date.fromisoformat(_text(item["date"], label)),
                retail_point_from_payload(item["current"], label),
                retail_point_from_payload(item["prior"], label),
            )
        )
    return tuple(result)


def _stat(value: object) -> RetailProductInsightStat:
    item = _object(value, frozenset({"label", "text", "change"}), "insight stat")
    change = item["change"]
    return RetailProductInsightStat(
        _text(item["label"], "insight stat label"),
        _text(item["text"], "insight stat text"),
        None if change is None else _stat_change(change),
    )


def _stat_change(value: object) -> Change:
    """Decode a stat's change, copied from the row's own comparison, structurally."""
    item = _object(
        value,
        frozenset({"direction", "sentiment", "unit", "display_text", "not_meaningful"}),
        "insight stat change",
    )
    meaningless = item["not_meaningful"]
    return Change(
        ChangeDirection(_text(item["direction"], "change direction")),
        ChangeSentiment(_text(item["sentiment"], "change sentiment")),
        ChangeUnit(_text(item["unit"], "change unit")),
        _text(item["display_text"], "change text"),
        None if meaningless is None else NotMeaningful(_text(meaningless, "change flag")),
    )


_FIELDS = frozenset(
    {
        "kind",
        "outcome",
        "period",
        "prior_period",
        "product",
        "distributor_view",
        "totals",
        "absences",
        "sales_daily",
        "traffic_daily",
        "ad_attributed",
        "insights",
        "no_insight_line",
        "boundaries",
        EVIDENCE_STATUS_KEY,
        RETURN_REASONS_KEY,
        AMC_RETAIL_PURCHASES_KEY,
    }
)


def retail_product_detail_from_payload(value: object) -> RetailProductDetailArtifact:
    """Strictly decode one child body and re-attest that it re-encodes to the same bytes."""
    item = _object(value, _FIELDS, "retail product detail")
    if item["kind"] != _KIND:
        raise ValueError("retail product detail kind is unsupported.")
    check_retail_fixed_statuses(item, "retail product detail")
    product = _object(item["product"], frozenset({"title", "child_asin", "parent_asin"}), "product")
    absences = tuple(
        RetailTypedAbsence(_text(raw["metric"], "absence"), _text(raw["reason"], "absence"))
        for raw in (
            _object(entry, frozenset({"metric", "reason"}), "absence")
            for entry in _list(item["absences"], "absences")
        )
    )
    insights = tuple(
        RetailProductInsight(
            insight_slots_from_payload([raw["slot"]], expected=(InsightSlotId.RETAIL_MOVERS,))[0],
            tuple(_stat(stat) for stat in _list(raw["stats"], "insight stats")),
            _text(raw["provenance"], "insight provenance"),
        )
        for raw in (
            _object(entry, frozenset({"slot", "stats", "provenance"}), "product insight")
            for entry in _list(item["insights"], "product insights")
        )
    )
    line = item["no_insight_line"]
    artifact = RetailProductDetailArtifact(
        RetailProductDetailOutcome(_text(item["outcome"], "outcome")),
        _period(item["period"], "period"),
        _period(item["prior_period"], "prior period"),
        RetailProductIdentity(
            _text(product["title"], "title"),
            _text(product["child_asin"], "child ASIN"),
            _text(product["parent_asin"], "parent ASIN"),
        ),
        RetailDistributorView(_text(item["distributor_view"], "distributor view")),
        tuple(
            retail_comparison_from_payload(entry, "product total")
            for entry in _list(item["totals"], "product totals")
        ),
        absences,
        _series(item["sales_daily"], RetailSalesPoint, "sales daily"),
        _series(item["traffic_daily"], RetailTrafficPoint, "traffic daily"),
        _ad_attributed(item["ad_attributed"]),
        insights,
        None if line is None else _text(line, "no-insight line"),
        evidence_status_from_payload(item[EVIDENCE_STATUS_KEY]),
        tuple(_text(entry, "boundary") for entry in _list(item["boundaries"], "boundaries")),
    )
    if retail_product_detail_payload(artifact) != value:
        raise ValueError("retail product detail is not canonical.")
    return artifact


def product_insight_stats(
    title: str, revenue: RetailComparison, change_text: str, rank: int, population: int
) -> tuple[RetailProductInsightStat, ...]:
    """Write the three movers stats for one product, each label naming it (render-more RA-1)."""
    ordinal = {1: "1st", 2: "2nd", 3: "3rd"}.get(rank, f"{rank}th")
    noun = "product" if population == 1 else "products"
    return (
        RetailProductInsightStat(
            f"Shipped revenue, {title}",
            currency_display_text(revenue.current.value, revenue.current.units.currency_code),
            revenue.change,
        ),
        RetailProductInsightStat(f"Change, {title}", change_text),
        RetailProductInsightStat(f"Rank by change, {title}", f"{ordinal} of {population} {noun}"),
    )


def ad_values_by_channel(
    rows: Mapping[str, tuple[RetailAdComparison, RetailAdComparison]],
    labels: Mapping[str, str],
) -> tuple[RetailAdChannelRow, ...]:
    """Order a product's channels by the governed taxonomy order the labels are given in."""
    return tuple(
        RetailAdChannelRow(channel, label, *rows[channel])
        for channel, label in labels.items()
        if channel in rows
    )


# Products and ads (render format 12) ------------------------------------------------------------

RETAIL_PRODUCT_ADS_KIND = "retail_product_ads"
_CONVERSION = "conversion_rate_retail"


@dataclass(frozen=True, slots=True)
class RetailProductAdCell:
    """One channel's ad credit for one product, as Products and ads shows it.

    The current purchases and revenue and the revenue's change, copied from the product's baked
    detail. The prior figures stay in the detail: the tab renders none of them.
    """

    channel: str
    label: str
    purchases: RetailAdValue
    revenue: RetailAdValue
    revenue_change: Change

    def __post_init__(self) -> None:
        if not self.channel.strip() or not self.label.strip():
            raise ValueError("a product ad cell names its channel.")
        if (
            self.purchases.units.kind is not UnitKind.COUNT
            or self.revenue.units.kind is not UnitKind.CURRENCY
            or self.purchases.period != self.revenue.period
        ):
            raise ValueError("a product ad cell carries purchases and revenue for one window.")


@dataclass(frozen=True, slots=True)
class RetailProductAdsRow:
    """One served row's ad cells and conversion, or its ad section's typed absence."""

    child_asin: str
    conversion: RetailMetricValue | None
    """The product's units per glance view, absent where its detail serves no such tile."""
    cells: tuple[RetailProductAdCell, ...]
    reason_code: RetailDetailReason | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.child_asin.strip():
            raise ValueError("a product ads row names its product.")
        absent = self.reason_code is not None
        if absent != (self.reason is not None) or (absent and self.cells):
            raise ValueError("a product ads absence has a reason and no cells.")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("a product ads absence reason is nonblank.")
        if not absent and not self.cells:
            raise ValueError("a product ads row with no absence credits at least one channel.")
        channels = tuple(cell.channel for cell in self.cells)
        if len(set(channels)) != len(channels):
            raise ValueError("product ads channels are unique.")


@dataclass(frozen=True, slots=True)
class RetailProductAdsArtifact:
    """Every served row's ad credit for the parent's exact window, in row order."""

    period: DateRange
    prior_period: DateRange
    rows: tuple[RetailProductAdsRow, ...]

    def __post_init__(self) -> None:
        if self.prior_period != DateRange(
            self.period.start - timedelta(days=self.period.days),
            self.period.start - timedelta(days=1),
        ):
            raise ValueError("product ads compare the equal preceding window.")
        if not self.rows:
            raise ValueError("a product ads part serves at least one row.")
        asins = tuple(row.child_asin for row in self.rows)
        if len(set(asins)) != len(asins):
            raise ValueError("product ads rows are unique per product.")
        for row in self.rows:
            if row.conversion is not None and (
                row.conversion.period != self.period
                or row.conversion.grain != RETAIL_HEALTH_ROW_GRAIN
            ):
                raise ValueError("product ads conversion is the product's for the window.")
            if any(cell.revenue.period != self.period for cell in row.cells):
                raise ValueError("product ads use the page's window.")


def retail_product_ads(
    details: tuple[RetailProductDetailArtifact, ...],
) -> RetailProductAdsArtifact:
    """Project each served row's baked detail onto what Products and ads shows, in row order.

    Every figure is the detail's own: nothing is added, divided, ranked or re-derived here.
    """
    if not details:
        raise ValueError("a product ads part projects at least one served row.")
    first = details[0]
    rows: list[RetailProductAdsRow] = []
    for detail in details:
        ads = detail.ad_attributed
        if (
            detail.outcome is not RetailProductDetailOutcome.SELECTED
            or ads is None
            or (detail.period, detail.prior_period) != (first.period, first.prior_period)
        ):
            raise ValueError("product ads project selected details of one window.")
        conversion = next(
            (total.current for total in detail.totals if total.metric == _CONVERSION), None
        )
        rows.append(
            RetailProductAdsRow(
                detail.product.child_asin,
                conversion,
                tuple(
                    RetailProductAdCell(
                        row.channel,
                        row.label,
                        row.purchases.current,
                        row.revenue.current,
                        row.revenue.change,
                    )
                    for row in ads.rows
                ),
                ads.reason_code,
                ads.reason,
            )
        )
    return RetailProductAdsArtifact(first.period, first.prior_period, tuple(rows))


def retail_product_ads_payload(artifact: RetailProductAdsArtifact) -> dict[str, object]:
    """Encode the Products and ads part; it names products only by their served child ASIN."""
    return {
        "kind": RETAIL_PRODUCT_ADS_KIND,
        "period": _period_payload(artifact.period),
        "prior_period": _period_payload(artifact.prior_period),
        "basis": RETAIL_PRODUCT_AD_BASIS,
        "rows": [
            {
                "child_asin": row.child_asin,
                "conversion": None
                if row.conversion is None
                else retail_value_payload(row.conversion),
                "channels": [
                    {
                        "channel": cell.channel,
                        "label": cell.label,
                        "purchases": _ad_value_payload(cell.purchases),
                        "revenue": _ad_value_payload(cell.revenue),
                        "revenue_change": change_payload(cell.revenue_change),
                    }
                    for cell in row.cells
                ],
                "reason_code": None if row.reason_code is None else row.reason_code.value,
                "reason": row.reason,
            }
            for row in artifact.rows
        ],
    }


def _ads_cell(value: object) -> RetailProductAdCell:
    item = _object(
        value,
        frozenset({"channel", "label", "purchases", "revenue", "revenue_change"}),
        "product ad cell",
    )
    return RetailProductAdCell(
        _text(item["channel"], "product ad channel"),
        _text(item["label"], "product ad channel label"),
        _ad_value(item["purchases"], "product ad purchases"),
        _ad_value(item["revenue"], "product ad revenue"),
        # Copied from the detail's own comparison at bake, so decoded structurally: the prior
        # side it was built from stays in the detail.
        _stat_change(item["revenue_change"]),
    )


def _ads_row(value: object) -> RetailProductAdsRow:
    item = _object(
        value,
        frozenset({"child_asin", "conversion", "channels", "reason_code", "reason"}),
        "product ads row",
    )
    conversion, code, reason = item["conversion"], item["reason_code"], item["reason"]
    return RetailProductAdsRow(
        _text(item["child_asin"], "product ads child ASIN"),
        None if conversion is None else retail_value_from_payload(conversion, "product conversion"),
        tuple(_ads_cell(raw) for raw in _list(item["channels"], "product ad cells")),
        None if code is None else RetailDetailReason(_text(code, "product ads reason code")),
        None if reason is None else _text(reason, "product ads reason"),
    )


def retail_product_ads_from_payload(value: object) -> RetailProductAdsArtifact:
    """Strictly decode the Products and ads part and re-attest that it re-encodes exactly."""
    item = _object(
        value, frozenset({"kind", "period", "prior_period", "basis", "rows"}), "product ads"
    )
    if item["kind"] != RETAIL_PRODUCT_ADS_KIND or item["basis"] != RETAIL_PRODUCT_AD_BASIS:
        raise ValueError("product ads kind or basis is not the server's.")
    artifact = RetailProductAdsArtifact(
        _period(item["period"], "product ads period"),
        _period(item["prior_period"], "product ads prior period"),
        tuple(_ads_row(raw) for raw in _list(item["rows"], "product ads rows")),
    )
    if retail_product_ads_payload(artifact) != value:
        raise ValueError("product ads are not canonical.")
    return artifact
