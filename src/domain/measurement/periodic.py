# SPDX-License-Identifier: MIT-0
"""Serving periodic metrics at exact complete periods (ADR-100; design §4.3 D-1, D-6 to D-8).

A periodic fact holds one row set per measured period: an ISO week, a calendar month or a
calendar quarter, keyed by `period_start`, `period_end`, `time_grain` and `grain_set`. A distinct
shopper count exists only for that exact period, so serving one is selection, never summation.

The rules here are the planner's. Each refusal carries the exact reason and, where one exists,
the nearest period that could be served instead, so a caller can offer it rather than guess.

- **Exact period (D-1, D-7).** A request pins one `time_grain` the metric declares, and its
  window is exactly one complete period of that grain. A rolling or partial window is refused
  with the nearest complete period.
- **Served period.** A complete period with no current publication is refused with the nearest
  served one (`select_served_period`).
- **Daily alignment (D-6).** A daily-additive operand over a periodic one sums exactly the
  period's days. A day without a current publication refuses the figure
  (`require_daily_coverage`).
- **One population (D-8).** No figure combines two shopper-aggregate deliveries from different
  contracts or grain sets for one period: no sum, difference or quotient. The three exceptions:
  (a) the same metric, contract, grain set and bucket scheme across periods or vintages, as a
  labelled change; (b) the declared ARP cohort ratio, guarded by the cohort-size check; (c) a
  D-6 ratio with a daily-additive operand (`check_cross_delivery`).

Pure domain: publications and coverage arrive as plain values an adapter read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING

from domain.catalog.capability import (
    GRAIN_NOT_SUPPORTED_CODE,
    PERIOD_GRAIN_SELECTOR,
    CatalogError,
    CrossDeliveryException,
    Metric,
    MetricKind,
    composite_operand_names,
)
from domain.producer.variants import PACK_VARIANT_MISMATCH
from domain.semantics.calendar_periods import (
    CalendarPeriod,
    grain_list_text,
    grain_noun,
    is_complete_period,
    latest_complete_period,
)
from domain.semantics.time import FactTimeClass, TimeGrain
from domain.shared_kernel.errors import DomainError

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Mapping

    from domain.catalog.capability import Catalog
    from domain.shared_kernel.values import DateRange

__all__ = [
    "AS_OF_MONTH_COLUMN",
    "BASE_ROW_KIND",
    "BUCKET_SCHEME_COLUMN",
    "COHORT_GRAIN_COLUMN",
    "COHORT_PATH_COLUMN",
    "GRAIN_SET_COLUMN",
    "PERIOD_END_COLUMN",
    "PERIOD_START_COLUMN",
    "PROFILE_PATH_COLUMN",
    "ROW_KIND_COLUMN",
    "CrossDeliveryCheck",
    "CrossDeliveryOperation",
    "DeliveryOperand",
    "PeriodRefusal",
    "PeriodRefusalReason",
    "PeriodicPlanning",
    "VariantMismatchError",
    "active_variant",
    "arp_ratio_withheld_reason",
    "check_cross_delivery",
    "periodic_leaves",
    "require_daily_coverage",
    "require_exact_period",
    "require_same_variant",
    "select_served_period",
]

PERIOD_START_COLUMN = "period_start"
PERIOD_END_COLUMN = "period_end"
GRAIN_SET_COLUMN = "grain_set"
ROW_KIND_COLUMN = "row_kind"
BUCKET_SCHEME_COLUMN = "bucket_scheme"
"""The frequency bucket scheme a pack 6 or 7 row was bucketed under (`fine` or `coarse`, D11)."""
COHORT_GRAIN_COLUMN = "cohort_grain"
"""The cohort grain a pack 14 or 15 row was grouped under (`month` or `quarter`, D11)."""
COHORT_PATH_COLUMN = "cohort_path"
"""Which repeat source a cohort row counts: `standard` (pack 14) or `arp` (pack 15)."""
PROFILE_PATH_COLUMN = "profile_path"
"""Which profiled base a C2 row counts: `asi_in_market`, `asi_lifestyle` (packs 10-11, paid) or
`dsp_exposed_only` (pack 12); each is its own delivery and never one series with another."""
AS_OF_MONTH_COLUMN = "as_of_month"
"""A cohort vintage's last month (ADR-104), an exact selector defaulting to the latest."""
BASE_ROW_KIND = "combined"
"""The row kind every base-pack row of a variant-bearing fact carries (plan D11). A declared
variant's rows (pack 5's touch-only `exposure` and `outcome`) are a series of their own, in their
own publication lens, so a question about the base series pins this value."""


class VariantMismatchError(ValueError):
    """A period, or a comparison, that would read two pack variants' series (plan D9, D-8 (a)).

    Its text starts with `PACK_VARIANT_MISMATCH`, the load's code for the same rule: fine and
    coarse, or combined and touch-only, rows never meet in one figure or one comparison.
    """


def _series_name(variant: str | None) -> str:
    return "the base pack" if variant is None else f"variant {variant}"


def active_variant(series: tuple[str | None, ...], period_label: str) -> str | None:
    """Return the one series a published period is served from, refusing a mixed period.

    A period is served from the series holding its CURRENT publication. Two series holding one
    period (a base and a touch-only delivery both landed) can't be told apart by any rule the
    owner approved, so neither is served.
    """
    held = tuple(dict.fromkeys(series))
    if len(held) != 1:
        names = " and ".join(_series_name(item) for item in held) or "no series"
        raise VariantMismatchError(
            f"{PACK_VARIANT_MISMATCH}: {period_label} is published under {names}, so neither "
            "is served."
        )
    return held[0]


def require_same_variant(current: str | None, prior: str | None, prior_label: str) -> None:
    """Refuse a comparison whose two periods were measured by different pack variants."""
    if current != prior:
        raise VariantMismatchError(
            f"{PACK_VARIANT_MISMATCH}: {prior_label} was measured by {_series_name(prior)} and "
            f"this period by {_series_name(current)}, so they aren't compared."
        )


COHORT_SIZES_CHANGED = "cohort sizes changed between runs"
"""The typed reason an ARP rate is withheld (design §6.6, §7a)."""


class PeriodRefusalReason(StrEnum):
    """Why a periodic figure can't be served. Each is a closed, testable signal."""

    PERIOD_GRAIN_NOT_PINNED = "period_grain_not_pinned"
    PERIOD_GRAIN_NOT_DECLARED = "period_grain_not_declared"
    PERIOD_NOT_COMPLETE = "period_not_complete"
    PERIOD_NOT_SERVED = "period_not_served"
    DAILY_COVERAGE_INCOMPLETE = "daily_coverage_incomplete"
    DAILY_OPERAND_NOT_ADDITIVE = "daily_operand_not_additive"
    CROSS_DELIVERY = "cross_delivery"
    BUCKET_SCHEME_CHANGED = "bucket_scheme_changed"


@dataclass(frozen=True, slots=True)
class PeriodRefusal:
    """A refused periodic request, naming the nearest servable period when one exists."""

    requested: str
    reason: PeriodRefusalReason
    explanation: str
    nearest: CalendarPeriod | None = None

    def to_error(self) -> DomainError:
        """Raise as the catalog's grain refusal, with the reason and alternative in context."""
        context: dict[str, object] = {"requested": self.requested, "reason": self.reason.value}
        if self.nearest is not None:
            context |= {
                "nearest_period_start": self.nearest.start.isoformat(),
                "nearest_period_end": self.nearest.end.isoformat(),
                "nearest_time_grain": self.nearest.grain.value,
                "nearest_period_label": self.nearest.label,
            }
        return DomainError(
            code=GRAIN_NOT_SUPPORTED_CODE, description=self.explanation, context=context
        )


def _simple_leaves(catalog: Catalog, metric: Metric) -> tuple[Metric, ...]:
    if metric.kind is MetricKind.SIMPLE:
        return (metric,)
    found: list[Metric] = []
    for name in composite_operand_names(metric):
        operand = catalog.metric(name)
        if operand is None:  # pragma: no cover — catalog construction rejects it
            raise CatalogError(f"{metric.name} has unknown operand {name}.")
        found.extend(leaf for leaf in _simple_leaves(catalog, operand) if leaf not in found)
    return tuple(found)


def periodic_leaves(catalog: Catalog, metric: Metric) -> tuple[Metric, ...]:
    """Return the periodic simple metrics a metric reads, itself included."""
    return tuple(
        leaf
        for leaf in _simple_leaves(catalog, metric)
        if leaf.time_class is FactTimeClass.PERIODIC
    )


@dataclass(frozen=True, slots=True)
class PeriodicPlanning:
    """What the planner needs to decide one periodic request."""

    catalog: Catalog
    metric: Metric
    window: DateRange
    filters: Mapping[str, str]
    grain_set_periods: tuple[str, tuple[TimeGrain, ...]] | None = None
    """The pinned grain set and the period grains its contract declares, when it narrows them.

    Pack 3's campaign grain set is measured per calendar month only, while the buyer metrics
    are declared for weeks and months, so a week at grain set campaign is refused here."""


def _declared_grains(leaves: tuple[Metric, ...]) -> tuple[TimeGrain, ...]:
    shared = set(leaves[0].period_grains)
    for leaf in leaves[1:]:
        shared &= set(leaf.period_grains)
    return tuple(grain for grain in leaves[0].period_grains if grain in shared)


def require_exact_period(request: PeriodicPlanning) -> CalendarPeriod | None:
    """Return the exact period a periodic request reads, or None for a daily-only metric.

    Raises the planner's refusal for an unpinned or undeclared grain, a window that isn't one
    complete period, a cross-delivery quotient (D-8) and a non-additive daily operand (D-6).
    """
    leaves = periodic_leaves(request.catalog, request.metric)
    if not leaves:
        return None
    name = request.metric.name
    grains = _declared_grains(leaves)
    choices = ", ".join(grain.value for grain in grains)
    pinned = request.filters.get(PERIOD_GRAIN_SELECTOR)
    if pinned is None:
        raise PeriodRefusal(
            name,
            PeriodRefusalReason.PERIOD_GRAIN_NOT_PINNED,
            f"{name} is measured per complete period, and week and month rows sit side by side, "
            f"so a request pins {PERIOD_GRAIN_SELECTOR} to one of: {choices}.",
        ).to_error()
    grain = next((item for item in grains if item.value == pinned), None)
    if grain is None:
        raise PeriodRefusal(
            name,
            PeriodRefusalReason.PERIOD_GRAIN_NOT_DECLARED,
            f"{name} isn't measured per {pinned}. It's measured for complete "
            f"{grain_list_text(grains)}.",
        ).to_error()
    window = request.window
    if request.grain_set_periods is not None:
        grain_set, measured = request.grain_set_periods
        narrowed = tuple(item for item in grains if item in measured)
        if narrowed and grain not in narrowed:
            nearest = latest_complete_period(narrowed[0], window.end)
            raise PeriodRefusal(
                name,
                PeriodRefusalReason.PERIOD_GRAIN_NOT_DECLARED,
                f"{name} per {grain_set} is measured for complete {grain_list_text(narrowed)} "
                f"only, not per {pinned}. The nearest complete {grain_noun(narrowed[0])} is "
                f"{nearest.label} ({nearest.start.isoformat()} to {nearest.end.isoformat()}).",
                nearest,
            ).to_error()
    if not is_complete_period(grain, window.start, window.end):
        nearest = latest_complete_period(grain, window.end)
        raise PeriodRefusal(
            name,
            PeriodRefusalReason.PERIOD_NOT_COMPLETE,
            f"{name} counts distinct shoppers for complete {grain_list_text(grains)} only, so "
            f"{window.start.isoformat()} to {window.end.isoformat()} can't be served: it isn't "
            f"one whole {grain_noun(grain)}, and distinct shoppers can't be added up across days. "
            f"The nearest complete {grain_noun(grain)} is {nearest.label} "
            f"({nearest.start.isoformat()} to {nearest.end.isoformat()}).",
            nearest,
        ).to_error()
    period = CalendarPeriod(window.start, window.end, grain)
    _check_operands(request, leaves, period)
    return period


def _check_operands(
    request: PeriodicPlanning, periodic: tuple[Metric, ...], period: CalendarPeriod
) -> None:
    metric = request.metric
    leaves = _simple_leaves(request.catalog, metric)
    daily = tuple(leaf for leaf in leaves if leaf.time_class is not FactTimeClass.PERIODIC)
    not_additive = sorted(
        leaf.name for leaf in daily if leaf.time_class is not FactTimeClass.DAILY_ADDITIVE
    )
    if not_additive:
        raise PeriodRefusal(
            metric.name,
            PeriodRefusalReason.DAILY_OPERAND_NOT_ADDITIVE,
            f"{metric.name} aligns {', '.join(not_additive)} to {period.label}, and only a "
            "daily-additive figure can be summed over a period's days (D-6).",
        ).to_error()
    if metric.kind is MetricKind.SIMPLE:
        return
    operation = (
        CrossDeliveryOperation.DIVIDE
        if metric.kind is MetricKind.RATIO
        else CrossDeliveryOperation.MULTIPLY
    )
    contracts = tuple(dict.fromkeys(_contract_of(leaf) for leaf in periodic))
    for left, right in pairwise(contracts):
        check_cross_delivery(
            CrossDeliveryCheck(
                requested=metric.name,
                operation=operation,
                left=DeliveryOperand(left, request.filters.get(GRAIN_SET_COLUMN, ""), period),
                right=DeliveryOperand(right, request.filters.get(GRAIN_SET_COLUMN, ""), period),
                exception=metric.cross_delivery,
            )
        )


def _contract_of(leaf: Metric) -> str:
    """Name a leaf's delivery identity: the source datasets one pack writes for it."""
    return "+".join(leaf.source_datasets)


class CrossDeliveryOperation(StrEnum):
    """Every arithmetic a figure can apply to two operands (D-8)."""

    SUM = "sum"
    SUBTRACT = "subtract"
    DIVIDE = "divide"
    MULTIPLY = "multiply"
    COMPARE = "compare"
    """A labelled change between two periods or vintages; each side keeps its population."""


@dataclass(frozen=True, slots=True)
class DeliveryOperand:
    """Which delivery one operand's figure comes from."""

    contract: str
    grain_set: str
    period: CalendarPeriod
    bucket_scheme: str | None = None
    """The frequency bucket scheme (`fine` or `coarse`) the delivery used, when it has one."""


@dataclass(frozen=True, slots=True)
class CrossDeliveryCheck:
    """One two-operand operation to check against D-8."""

    requested: str
    operation: CrossDeliveryOperation
    left: DeliveryOperand
    right: DeliveryOperand
    exception: CrossDeliveryException | None = None


def check_cross_delivery(check: CrossDeliveryCheck) -> None:
    """Refuse any arithmetic across two shopper-aggregate deliveries except D-8 (a) and (b).

    D-8 (c), a daily-additive operand, never reaches here: only periodic operands are checked.
    """
    left, right = check.left, check.right
    same_source = (left.contract, left.grain_set) == (right.contract, right.grain_set)
    if same_source and left.period == right.period and left.bucket_scheme == right.bucket_scheme:
        return
    if check.operation is CrossDeliveryOperation.COMPARE and same_source:
        if left.bucket_scheme != right.bucket_scheme:
            later = max(left.period, right.period)
            raise PeriodRefusal(
                check.requested,
                PeriodRefusalReason.BUCKET_SCHEME_CHANGED,
                f"bucket scheme changed in {later.label}",
            ).to_error()
        return
    if (
        check.operation is CrossDeliveryOperation.DIVIDE
        and check.exception is CrossDeliveryException.ARP_COHORT_RATIO
        and left.period == right.period
    ):
        return
    raise PeriodRefusal(
        check.requested,
        PeriodRefusalReason.CROSS_DELIVERY,
        f"{check.requested} would {check.operation.value} figures from two deliveries "
        f"({left.contract} {left.grain_set or 'default'} grain set, {left.period.label}; "
        f"{right.contract} {right.grain_set or 'default'} grain set, {right.period.label}). "
        "Each delivery has its own suppression and timing, so the result would have no single "
        "population (D-8).",
    ).to_error()


def select_served_period(
    requested: str, period: CalendarPeriod, served: frozenset[CalendarPeriod]
) -> CalendarPeriod:
    """Return `period` when it has a current publication; refuse with the nearest served one."""
    if period in served:
        return period
    same_grain = [item for item in served if item.grain is period.grain]
    nearest = min(
        same_grain,
        key=lambda item: (abs((item.end - period.end).days), item.end > period.end),
        default=None,
    )
    alternative = (
        f" The nearest measured {grain_noun(period.grain)} is {nearest.label}."
        if nearest is not None
        else f" No {grain_noun(period.grain)} has been measured yet."
    )
    raise PeriodRefusal(
        requested,
        PeriodRefusalReason.PERIOD_NOT_SERVED,
        f"{requested} hasn't been measured for {period.label}.{alternative}",
        nearest,
    ).to_error()


def require_daily_coverage(
    requested: str, period: CalendarPeriod, covered: tuple[DateRange, ...]
) -> None:
    """D-6: refuse a daily operand over a period unless every day has a current publication."""
    day: date = period.start
    while day <= period.end:
        if not any(interval.start <= day <= interval.end for interval in covered):
            raise PeriodRefusal(
                requested,
                PeriodRefusalReason.DAILY_COVERAGE_INCOMPLETE,
                f"{requested} sums daily figures over {period.label}, and "
                f"{day.isoformat()} has no current publication, so the figure would cover "
                "part of the period only (D-6).",
            ).to_error()
        day += timedelta(days=1)


def arp_ratio_withheld_reason(
    repeat_buyers: int, cohort_buyers_account_ids: int, cohort_buyers_check: int
) -> str | None:
    """D-8 (b) guard: the ARP rate is withheld when the cohort sizes disagree (design §6.6)."""
    if cohort_buyers_check != cohort_buyers_account_ids or repeat_buyers > (
        cohort_buyers_account_ids
    ):
        return COHORT_SIZES_CHANGED
    return None
