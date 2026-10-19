# SPDX-License-Identifier: MIT-0
"""MetricFocus.v4 — the shared focused-metric payload and its per-surface derivations.

The composition, grammar, and designed states are owned by
docs/ui/metric-focus-contract.md. Everything here derives from evidence a page has
already been served — a baked preset artifact or a redeemed detail render — never from a
new measurement; the serving handler is what proves that, and this module is what makes
the proof meaningful by having no port of its own.

The v2 payload supersedes the unversioned ``{session_id, temporal, metric, insight}``
campaign focus envelope; both are never decoded side by side.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date as date_type
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING, cast

from domain.measurement.comparison import absolute_change, percentage_change
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.campaign_attribution import (
    CAMPAIGN_SUMMARY_GOVERNED_METRICS,
    CampaignAttributionDetailArtifact,
    CampaignDelivery,
    CampaignDeliverySlot,
    CampaignSummarySlot,
)
from domain.presentation.campaign_portfolio import (
    CAMPAIGN_PORTFOLIO_GOVERNED_METRICS,
    CampaignPortfolioArtifact,
    CampaignPortfolioMetric,
    campaign_population,
    portfolio_row_operand_labels,
)
from domain.presentation.change import (
    Change,
    ChangeOperands,
    ChangeSemantics,
    build_change,
    change_payload,
    check_change,
)
from domain.presentation.currency_display import (
    CurrencyTextStyle,
    currency_display_text,
    currency_display_value,
    metric_display_value,
)
from domain.presentation.insights import (
    DeterministicFinding,
    InsightSlot,
    decimal_figure,
    insight_slots_payload,
)
from domain.presentation.retail_health import (
    RETAIL_HEALTH_ROW_METRICS,
    RETAIL_HEALTH_TOTAL_METRICS,
    RETAIL_HEALTH_TREND_METRICS,
    RetailHealthArtifact,
)
from domain.presentation.retail_product_detail import RetailProductDetailArtifact
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange, Grain

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

METRIC_FOCUS_VERSION = 4
FOCUS_DERIVED_ASP_METRIC = "asp_shipped"
"""The dictionary metric the estimate's window average selling price component measures."""
_TOP_DRIVERS = 4
_SHARE_PRECISION = Decimal("1")
_DOMINANCE_FACTOR = 2
_MINIMUM_STEP_POINTS = 2
type FocusNumber = int | str | Decimal

__all__ = [
    "CAMPAIGN_DETAIL_FOCUS_METRICS",
    "CAMPAIGN_PORTFOLIO_FOCUS_METRICS",
    "FOCUS_DERIVED_ASP_METRIC",
    "METRIC_FOCUS_VERSION",
    "RETAIL_FOCUS_METRICS",
    "MetricFocusArtifact",
    "MetricFocusDriverCandidate",
    "MetricFocusDriverEntry",
    "MetricFocusDriverGrain",
    "MetricFocusDriverScope",
    "MetricFocusDrivers",
    "MetricFocusDriversAbsent",
    "MetricFocusDriversSummary",
    "MetricFocusFact",
    "MetricFocusIdentity",
    "MetricFocusIngredients",
    "MetricFocusNextCheck",
    "MetricFocusSeries",
    "MetricFocusSeriesPoint",
    "MetricFocusShareBasis",
    "MetricFocusSummaryChange",
    "MetricFocusValue",
    "build_metric_focus",
    "campaign_detail_focus_ingredients",
    "campaign_portfolio_focus_ingredients",
    "direction_counts",
    "direction_counts_sentence",
    "focus_drivers",
    "focus_governed_metric",
    "metric_focus_payload",
    "retail_health_focus_ingredients",
    "retail_product_focus_ingredients",
]


class MetricFocusDriverGrain(StrEnum):
    """The one decomposition grain a surface declares for a focused metric."""

    CAMPAIGN = "campaign"
    DAY = "day"
    RATIO_COMPONENT = "ratio_component"
    PRODUCT = "product"


class MetricFocusShareBasis(StrEnum):
    """What a driver's share is a share of (design §4.6, R3-7)."""

    PORTFOLIO = "portfolio"
    """The metric's total change: the in-scope and outside nets move the same way."""
    SCOPE = "scope"
    """The in-scope net: the outside net moves against it, so the total would invert shares."""
    NONE = "none"
    """The in-scope changes cancel out; no share is meaningful."""


_ADDITIVE_GRAINS = frozenset({MetricFocusDriverGrain.CAMPAIGN, MetricFocusDriverGrain.PRODUCT})


@dataclass(frozen=True, slots=True)
class MetricFocusIdentity:
    """The focused metric's dictionary-served identity; never hardcoded copy."""

    metric_id: str
    label: str
    definition: str
    units: Units
    basis: MeasurementBasis
    grain: Grain

    def __post_init__(self) -> None:
        if not self.metric_id.strip() or not self.label.strip() or not self.definition.strip():
            raise ValueError("metric focus identity requires id, label, and definition.")


@dataclass(frozen=True, slots=True)
class MetricFocusValue:
    """One exact server value with the semantics the owning artifact stated."""

    value: FocusNumber
    units: Units
    period: DateRange
    grain: Grain
    basis: MeasurementBasis


@dataclass(frozen=True, slots=True)
class MetricFocusSeriesPoint:
    """One dated series value, with its aligned prior where the surface carries one."""

    day: date
    current: FocusNumber
    prior: FocusNumber | None


@dataclass(frozen=True, slots=True)
class MetricFocusSeries:
    """The served daily evidence behind the focused metric."""

    points: tuple[MetricFocusSeriesPoint, ...]
    display_grain: str = "day"

    def __post_init__(self) -> None:
        days = tuple(point.day for point in self.points)
        if not self.points or days != tuple(sorted(days)) or len(set(days)) != len(days):
            raise ValueError("metric focus series points must be ordered, unique, nonempty.")


@dataclass(frozen=True, slots=True)
class MetricFocusFact:
    """One server-computed labeled observation; a value, never prose."""

    fact_id: str
    label: str
    value: str
    qualifier: str | None = None

    def __post_init__(self) -> None:
        if not self.fact_id.strip() or not self.label.strip() or not self.value.strip():
            raise ValueError("metric focus facts require id, label, and value.")


@dataclass(frozen=True, slots=True)
class MetricFocusDriverCandidate:
    """One decomposition contributor before ranking: exact current and prior values.

    ``metric_id`` names the contributor as a page-focusable metric where one exists
    (ratio components), which is what makes a deterministic next-check derivable.
    """

    name: str
    current: Decimal | int
    prior: Decimal | int
    metric_id: str | None = None
    units: Units = field(kw_only=True)
    """The contributor's own units, which may differ from the focused metric's."""
    change_metric_id: str | None = field(default=None, kw_only=True)
    """The dictionary metric whose change semantics apply, when it is neither ``metric_id``
    nor the focused metric (a derived component such as the window's average selling price)."""


@dataclass(frozen=True, slots=True)
class MetricFocusDriverEntry:
    """One ranked contributor with its exact change, own units, and share of its basis.

    ``moves_against_basis`` is true when the change is non-zero and its sign differs from the
    basis (the metric's own total change where no scope basis applies). ``chip`` is the exact
    served text that replaces the share when the share is not meaningful. ``delta`` is the
    entry's server-declared change (ADR-121), attached by `build_metric_focus` from
    ``current`` and ``prior`` under the semantics of ``change_metric_id``.
    """

    name: str
    change: FocusNumber
    share_of_move: str | None
    units: Units = field(kw_only=True)
    current: FocusNumber = field(kw_only=True)
    prior: FocusNumber = field(kw_only=True)
    change_metric_id: str | None = field(default=None, kw_only=True)
    """The dictionary metric of the entry's change; `None` means the focused metric."""
    moves_against_basis: bool = field(default=False, kw_only=True)
    chip: str | None = field(default=None, kw_only=True)
    delta: Change | None = field(default=None, kw_only=True)


@dataclass(frozen=True, slots=True)
class MetricFocusDriverScope:
    """Which N of M entities the drivers rank among, in one served sentence."""

    count: int
    population: int
    label: str
    share_suffix: str | None
    """What a served share is "of", or None when no share is meaningful."""


@dataclass(frozen=True, slots=True)
class MetricFocusDrivers:
    """The deterministic decomposition at the surface's declared grain.

    ``scope`` and ``share_basis`` exist exactly for the additive grains (campaign, product).
    """

    grain: MetricFocusDriverGrain
    entries: tuple[MetricFocusDriverEntry, ...]
    scope: MetricFocusDriverScope | None = None
    share_basis: MetricFocusShareBasis | None = None

    def __post_init__(self) -> None:
        if not self.entries:
            raise ValueError("metric focus drivers require entries; absence is typed.")
        additive = self.grain in _ADDITIVE_GRAINS
        if additive != (self.scope is not None) or additive != (self.share_basis is not None):
            raise ValueError("a driver scope and share basis exist exactly on additive grains.")


@dataclass(frozen=True, slots=True)
class MetricFocusSummaryChange:
    """One summed change with its server display string."""

    value: Decimal
    display: str


@dataclass(frozen=True, slots=True)
class MetricFocusDriversSummary:
    """What the listed drivers, the rest of the scope, and everything outside it moved."""

    listed_change: MetricFocusSummaryChange
    other_in_scope_change: MetricFocusSummaryChange | None
    outside_scope_change: MetricFocusSummaryChange | None
    rose: int
    fell: int
    flat: int
    lines: tuple[tuple[str, str], ...]
    """The served (context entry name, customer sentence) pairs, one home for card and dialog."""


@dataclass(frozen=True, slots=True)
class MetricFocusDriversAbsent:
    """The designed absence: plain words for why there is nothing to decompose."""

    absent_reason: str

    def __post_init__(self) -> None:
        if not self.absent_reason.strip():
            raise ValueError("an absent decomposition must state its reason in plain words.")


@dataclass(frozen=True, slots=True)
class MetricFocusNextCheck:
    """A deterministic next-check chip naming a same-surface focusable metric."""

    metric_id: str
    label: str


@dataclass(frozen=True, slots=True)
class MetricFocusIngredients:
    """Everything one surface extracts from its served artifact for one focused metric."""

    metric_id: str
    period: DateRange
    prior_period: DateRange
    current: MetricFocusValue
    prior: MetricFocusValue
    series: MetricFocusSeries | None
    drivers_grain: MetricFocusDriverGrain
    driver_candidates: tuple[MetricFocusDriverCandidate, ...]
    drivers_absent_reason: str | None
    neighbors: tuple[str, ...]
    boundary: str | None
    additive: bool
    """Whether summing this metric's daily values is arithmetic; gates the total fact."""
    driver_population: int | None = None
    """M, the entities the scope is drawn from (additive grains); None means M = N."""


@dataclass(frozen=True, slots=True)
class MetricFocusArtifact:
    """The complete v4 focus result, exactly the shape the contract's §3 states."""

    identity: MetricFocusIdentity
    period: DateRange
    prior_period: DateRange
    current: MetricFocusValue
    prior: MetricFocusValue
    change: Change
    """The focused metric's server-declared change (ADR-121)."""
    series: MetricFocusSeries | None
    facts: tuple[MetricFocusFact, ...]
    drivers: MetricFocusDrivers | MetricFocusDriversAbsent
    insight: InsightSlot
    neighbors: tuple[str, ...]
    next_check: MetricFocusNextCheck | None
    boundary: str | None
    drivers_summary: MetricFocusDriversSummary | None = None

    def __post_init__(self) -> None:
        if self.identity.metric_id not in self.neighbors:
            raise ValueError("the focused metric must appear in its own switcher roster.")
        if self.next_check is not None and self.next_check.metric_id not in self.neighbors:
            raise ValueError("a next check must name a same-surface focusable metric.")
        check_change(
            self.change,
            ChangeOperands(
                _number(self.current.value), _number(self.prior.value), self.current.units
            ),
        )
        if isinstance(self.drivers, MetricFocusDrivers):
            for entry in self.drivers.entries:
                if entry.delta is None:
                    raise ValueError("every served driver entry carries its change.")
                check_change(
                    entry.delta,
                    ChangeOperands(_number(entry.current), _number(entry.prior), entry.units),
                )


def _number(value: FocusNumber) -> Decimal | int:
    """Read one exact artifact value into arithmetic form; canonical text stays exact."""
    return value if isinstance(value, (int, Decimal)) else Decimal(value)


def _figure(value: FocusNumber, units: Units) -> str:
    """Render one value: money at two places with its code (ADR-084), others exactly."""
    if units.kind is UnitKind.CURRENCY and units.currency_code is not None:
        return currency_display_text(Decimal(value), units.currency_code)
    text = decimal_figure(Decimal(value))
    return f"{text} {units.currency_code}" if units.currency_code else text


def _series_fact_values(
    series: MetricFocusSeries, *, additive: bool
) -> tuple[tuple[str, str, date_type | None, Decimal, str | None], ...]:
    """Compute the fact roster once for both display rendering and deep evidence."""
    points = series.points
    peak = max(points, key=lambda point: (Decimal(point.current), point.day))
    trough = min(points, key=lambda point: (Decimal(point.current), point.day))
    values: list[tuple[str, str, date_type | None, Decimal, str | None]] = [
        ("peak_day", "Peak day", peak.day, Decimal(peak.current), None),
        ("trough_day", "Trough day", trough.day, Decimal(trough.current), None),
    ]
    if len(points) > 1:
        steps = [
            (
                Decimal(absolute_change(_number(after.current), _number(before.current))),
                before,
                after,
            )
            for before, after in pairwise(points)
        ]
        magnitude, before, after = max(steps, key=lambda step: (abs(step[0]), step[2].day))
        values.append(
            (
                "largest_day_move",
                "Largest day move",
                None,
                magnitude,
                f"{before.day.isoformat()} → {after.day.isoformat()}",
            )
        )
    aggregate = sum((Decimal(point.current) for point in points), Decimal(0))
    if additive:
        values.append(("window_total", "Window total", None, aggregate, None))
    else:
        average = (aggregate / len(points)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
        values.append(("daily_average", "Daily average", None, average, None))
    return tuple(values)


def _series_facts(
    series: MetricFocusSeries, units: Units, *, additive: bool
) -> tuple[MetricFocusFact, ...]:
    """Compute the contract's §2 block-3 roster from the served daily evidence."""
    facts: list[MetricFocusFact] = []
    for fact_id, label, day, value, qualifier in _series_fact_values(series, additive=additive):
        if fact_id == "largest_day_move":
            # A day-over-day move renders as a signed unitless magnitude — the
            # pre-ADR-076 presentation — while its qualifier names the day span. Money is
            # signed on its two-place rounding, so a move that rounds to zero reads 0.00;
            # the bare style writes no code, so it takes none.
            if units.kind is UnitKind.CURRENCY:
                rendered = currency_display_text(value, None, CurrencyTextStyle.SIGNED_BARE)
            else:
                rendered = decimal_figure(value) if value < 0 else f"+{decimal_figure(value)}"
        else:
            rendered = _figure(value, units)
            if day is not None:
                rendered = f"{day.isoformat()} · {rendered}"
        facts.append(MetricFocusFact(fact_id, label, rendered, qualifier))
    return tuple(facts)


def _value_facts(current: MetricFocusValue, prior: MetricFocusValue) -> tuple[MetricFocusFact, ...]:
    """Render the designed absent-series state: facts from the single returned values."""
    return (
        MetricFocusFact("window_value", "Window value", _figure(current.value, current.units)),
        MetricFocusFact("aligned_prior", "Aligned prior", _figure(prior.value, prior.units)),
    )


def _share_basis(
    scope_net: Decimal, total_change: Decimal
) -> tuple[MetricFocusShareBasis, Decimal | None]:
    """Choose what in-scope shares are of: the total, the in-scope net, or nothing."""
    if scope_net == 0:
        return MetricFocusShareBasis.NONE, None
    outside_net = total_change - scope_net
    if outside_net == 0 or (outside_net > 0) == (scope_net > 0):
        return MetricFocusShareBasis.PORTFOLIO, total_change
    return MetricFocusShareBasis.SCOPE, scope_net


def _share(change: Decimal, basis: Decimal | None) -> str | None:
    """State one change's share of its basis, or None when the share is not meaningful.

    None when there is no basis, when a non-zero change moves against the basis, or when the
    change outgrows it (a share above 100 % would read a net split as gross).
    """
    if not basis:
        return None
    if change != 0 and (change > 0) != (basis > 0):
        return None
    if abs(change) > abs(basis):
        return None
    share = (abs(change) / abs(basis) * 100).quantize(_SHARE_PRECISION, rounding=ROUND_HALF_EVEN)
    return f"{share}%"


def _moves_against(change: Decimal, comparison: Decimal) -> bool:
    return change != 0 and comparison != 0 and (change > 0) != (comparison > 0)


def _noun(grain: MetricFocusDriverGrain) -> str:
    return "products" if grain is MetricFocusDriverGrain.PRODUCT else "campaigns"


def _scope_words(basis: MetricFocusShareBasis, count: int, noun: str) -> str:
    return (
        f"the change across these {count} {noun}"
        if basis is MetricFocusShareBasis.SCOPE
        else "the overall change"
    )


def _chip(  # noqa: PLR0913, PLR0917 -- one change judged against one basis
    change: Decimal, basis_kind: MetricFocusShareBasis | None, comparison: Decimal, words: str
) -> str | None:
    """Return the served chip that replaces a share that is not meaningful (design §4.6).

    ``comparison`` is the share basis, or the metric's own total change where none applies.
    """
    if _moves_against(change, comparison):
        return f"Moved against {words}"
    if basis_kind in {MetricFocusShareBasis.PORTFOLIO, MetricFocusShareBasis.SCOPE} and abs(
        change
    ) > abs(comparison):
        return f"Larger than {words}"
    return None


def _scope_label(grain: MetricFocusDriverGrain, count: int, population: int) -> str:
    noun = _noun(grain)
    if grain is MetricFocusDriverGrain.PRODUCT:
        return f"Largest changes among all {count} products in this view"
    if count == population:
        return f"Largest changes among all {count} {noun} in the portfolio"
    return (
        f"Largest changes among the {count} {noun} with the most ad spend in this window, "
        f"of {population} in the portfolio"
    )


def _summary_change(value: Decimal, units: Units) -> MetricFocusSummaryChange:
    return MetricFocusSummaryChange(value, _figure(value, units))


def _contribution_drivers(  # noqa: PLR0913, PLR0917 -- one surface's ranked population
    candidates: tuple[MetricFocusDriverCandidate, ...],
    grain: MetricFocusDriverGrain,
    total_change: Decimal,
    population: int | None,
    *,
    limit: int = _TOP_DRIVERS,
) -> tuple[MetricFocusDrivers, MetricFocusDriversSummary]:
    """Rank contributors by absolute change, sharing each against one basis (design §4.6)."""
    changes = {
        candidate.name: Decimal(absolute_change(candidate.current, candidate.prior))
        for candidate in candidates
    }
    count = len(candidates)
    population = count if population is None else population
    scope_net = sum(changes.values(), Decimal(0))
    basis_kind, basis = _share_basis(scope_net, total_change)
    noun = _noun(grain)
    words = _scope_words(basis_kind, count, noun)
    ranked = sorted(
        candidates, key=lambda candidate: (-abs(changes[candidate.name]), candidate.name)
    )[:limit]
    comparison = total_change if basis is None else basis
    entries = tuple(
        MetricFocusDriverEntry(
            candidate.name,
            absolute_change(candidate.current, candidate.prior),
            _share(changes[candidate.name], basis),
            units=candidate.units,
            current=candidate.current,
            prior=candidate.prior,
            moves_against_basis=_moves_against(changes[candidate.name], comparison),
            chip=_chip(changes[candidate.name], basis_kind, comparison, words),
        )
        for candidate in ranked
    )
    scope = MetricFocusDriverScope(
        count,
        population,
        _scope_label(grain, count, population),
        None if basis_kind is MetricFocusShareBasis.NONE else f"of {words}",
    )
    units = candidates[0].units
    listed = sum((changes[candidate.name] for candidate in ranked), Decimal(0))
    other = scope_net - listed
    outside = total_change - scope_net
    rose, fell, flat = direction_counts(tuple(changes.values()))
    listed_change = _summary_change(listed, units)
    other_change = None if count == len(ranked) else _summary_change(other, units)
    outside_change = None if population == count else _summary_change(outside, units)
    lines: list[tuple[str, str]] = [
        ("driver scope", scope.label),
        ("listed drivers change", f"The {len(ranked)} listed {noun}: {listed_change.display}"),
    ]
    if other_change is not None:
        lines.append(
            (
                f"other {noun} in scope change",
                f"The other {count - len(ranked)} of these {count} {noun}: {other_change.display}",
            )
        )
    if outside_change is not None:
        lines.append(
            (
                f"{noun} outside scope change",
                f"The {population - count} {noun} not among these {count}: "
                f"{outside_change.display}",
            )
        )
    lines.append(("direction counts in scope", direction_counts_sentence(tuple(changes.values()))))
    if basis_kind is MetricFocusShareBasis.NONE:
        lines.append(
            (
                "driver shares",
                f"Shares aren't shown because the changes among these {count} {noun} cancel out.",
            )
        )
    summary = MetricFocusDriversSummary(
        listed_change, other_change, outside_change, rose, fell, flat, tuple(lines)
    )
    return MetricFocusDrivers(grain, entries, scope, basis_kind), summary


def _component_drivers(
    candidates: tuple[MetricFocusDriverCandidate, ...], total_change: Decimal
) -> MetricFocusDrivers:
    """State a ratio's arithmetic why: its declared operand movements, unranked."""
    return MetricFocusDrivers(
        MetricFocusDriverGrain.RATIO_COMPONENT,
        tuple(
            MetricFocusDriverEntry(
                candidate.name,
                absolute_change(candidate.current, candidate.prior),
                None,
                units=candidate.units,
                current=candidate.current,
                prior=candidate.prior,
                change_metric_id=candidate.change_metric_id or candidate.metric_id,
                # Non-additive grains compare each entry with the metric's own total change.
                moves_against_basis=_moves_against(
                    Decimal(absolute_change(candidate.current, candidate.prior)), total_change
                ),
                chip=_chip(
                    Decimal(absolute_change(candidate.current, candidate.prior)),
                    None,
                    total_change,
                    "the overall change",
                ),
            )
            for candidate in candidates
        ),
    )


def _day_step_drivers(
    series: MetricFocusSeries, units: Units, total_change: Decimal
) -> MetricFocusDrivers | None:
    """Per-day steps: the largest consecutive-day moves inside the window, in its units."""
    points = series.points
    if len(points) < _MINIMUM_STEP_POINTS:
        return None
    steps = [
        (
            Decimal(absolute_change(_number(after.current), _number(before.current))),
            before,
            after,
        )
        for before, after in pairwise(points)
    ]
    ranked = sorted(steps, key=lambda step: (-abs(step[0]), step[2].day))[:_TOP_DRIVERS]
    return MetricFocusDrivers(
        MetricFocusDriverGrain.DAY,
        tuple(
            MetricFocusDriverEntry(
                f"{before.day.isoformat()} → {after.day.isoformat()}",
                magnitude,
                None,
                units=units,
                current=after.current,
                prior=before.current,
                moves_against_basis=_moves_against(magnitude, total_change),
                chip=_chip(magnitude, None, total_change, "the overall change"),
            )
            for magnitude, before, after in ranked
        ),
    )


def _percentage_magnitude(candidate: MetricFocusDriverCandidate) -> Decimal:
    """State one component's relative movement size; a zero prior weighs nothing."""
    rendered = percentage_change(candidate.current, candidate.prior)
    if rendered is None:
        return Decimal(0)
    return abs(Decimal(rendered[:-1]))


def _next_check(
    drivers: MetricFocusDrivers | MetricFocusDriversAbsent,
    candidates: tuple[MetricFocusDriverCandidate, ...],
    labels: dict[str, str],
) -> MetricFocusNextCheck | None:
    """Derive a deterministic chip when one focusable ratio component dominates."""
    if (
        not isinstance(drivers, MetricFocusDrivers)
        or drivers.grain is not MetricFocusDriverGrain.RATIO_COMPONENT
    ):
        return None
    weighted = [(_percentage_magnitude(candidate), candidate) for candidate in candidates]
    focusable = [
        (magnitude, candidate)
        for magnitude, candidate in weighted
        if candidate.metric_id is not None and candidate.metric_id in labels
    ]
    if not focusable:
        return None
    magnitude, dominant = max(focusable, key=lambda item: item[0])
    rest = max((value for value, item in weighted if item is not dominant), default=Decimal(0))
    if magnitude == 0 or magnitude < rest * _DOMINANCE_FACTOR:
        return None
    return MetricFocusNextCheck(dominant.metric_id or "", labels[dominant.metric_id or ""])


@dataclass(frozen=True, slots=True)
class BuildMetricFocusRequest:
    """One surface's extracted ingredients plus the identities only a caller owns."""

    ingredients: MetricFocusIngredients
    identity: MetricFocusIdentity
    insight: InsightSlot
    neighbor_labels: dict[str, str]
    """Display labels for every focusable neighbor, keyed by metric id."""
    semantics: ChangeSemantics
    """The dictionary's change semantics, for the focus and every driver entry (ADR-121)."""


def focus_drivers(
    ingredients: MetricFocusIngredients,
) -> tuple[MetricFocusDrivers | MetricFocusDriversAbsent, MetricFocusDriversSummary | None]:
    """Build the dialog's drivers and summary once; the card and deep read the same ones."""
    current, prior = ingredients.current, ingredients.prior
    total_change = Decimal(absolute_change(_number(current.value), _number(prior.value)))
    if ingredients.drivers_absent_reason is not None:
        return MetricFocusDriversAbsent(ingredients.drivers_absent_reason), None
    if ingredients.drivers_grain is MetricFocusDriverGrain.RATIO_COMPONENT:
        return _component_drivers(ingredients.driver_candidates, total_change), None
    if ingredients.drivers_grain is MetricFocusDriverGrain.DAY:
        stepped = (
            None
            if ingredients.series is None
            else _day_step_drivers(ingredients.series, current.units, total_change)
        )
        return (
            stepped
            if stepped is not None
            else MetricFocusDriversAbsent(
                "A one-day window has no day-over-day steps to decompose."
            )
        ), None
    return _contribution_drivers(
        ingredients.driver_candidates,
        ingredients.drivers_grain,
        total_change,
        ingredients.driver_population,
    )


def all_product_drivers(
    ingredients: MetricFocusIngredients,
) -> tuple[MetricFocusDrivers, MetricFocusDriversSummary] | None:
    """FR-11 (design 4.12, R6-5): every product in view, with the dialog's summary lines.

    A retail card lists all N products so no listed-to-total reading can leave one out; the
    strings are the dialog's own (one home). None when products are not the driver grain.
    """
    if (
        ingredients.drivers_absent_reason is not None
        or ingredients.drivers_grain is not MetricFocusDriverGrain.PRODUCT
        or not ingredients.driver_candidates
    ):
        return None
    total_change = Decimal(
        absolute_change(_number(ingredients.current.value), _number(ingredients.prior.value))
    )
    return _contribution_drivers(
        ingredients.driver_candidates,
        MetricFocusDriverGrain.PRODUCT,
        total_change,
        ingredients.driver_population,
        limit=len(ingredients.driver_candidates),
    )


def build_metric_focus(request: BuildMetricFocusRequest) -> MetricFocusArtifact:
    """Assemble one exact focus artifact from served evidence and one governed slot."""
    ingredients = request.ingredients
    current, prior = ingredients.current, ingredients.prior
    drivers, summary = focus_drivers(ingredients)
    if isinstance(drivers, MetricFocusDrivers):
        drivers = replace(
            drivers,
            entries=tuple(
                replace(
                    entry,
                    delta=build_change(
                        ChangeOperands(_number(entry.current), _number(entry.prior), entry.units),
                        request.semantics.for_metric(
                            focus_governed_metric(entry.change_metric_id or ingredients.metric_id)
                        ),
                    ),
                )
                for entry in drivers.entries
            ),
        )
    facts = (
        _series_facts(ingredients.series, current.units, additive=ingredients.additive)
        if ingredients.series is not None
        else _value_facts(current, prior)
    )
    return MetricFocusArtifact(
        identity=request.identity,
        period=ingredients.period,
        prior_period=ingredients.prior_period,
        current=current,
        prior=prior,
        change=build_change(
            ChangeOperands(_number(current.value), _number(prior.value), current.units),
            request.semantics.for_metric(focus_governed_metric(ingredients.metric_id)),
        ),
        series=ingredients.series,
        facts=facts,
        drivers=drivers,
        insight=request.insight,
        neighbors=ingredients.neighbors,
        next_check=_next_check(drivers, ingredients.driver_candidates, request.neighbor_labels),
        boundary=ingredients.boundary,
        drivers_summary=summary,
    )


def metric_focus_deep_findings(
    ingredients: MetricFocusIngredients, identity: MetricFocusIdentity
) -> tuple[DeterministicFinding, ...]:
    """Project the complete numeric focus evidence into citable deep-tier findings.

    The card derivation already carries aggregate changes. This projection adds the
    complete current+aligned-prior daily series, the exact computed facts strip, and
    every supplied driver operand (current+prior) without reimplementing any fact
    calculation — `_series_fact_values` is the one computation home shared with the
    visible focus artifact.
    """
    findings: list[DeterministicFinding] = [
        DeterministicFinding(
            f"{identity.label} current window value", Decimal(ingredients.current.value)
        ),
        DeterministicFinding(
            f"{identity.label} aligned prior value", Decimal(ingredients.prior.value)
        ),
    ]
    if ingredients.series is not None:
        for point in ingredients.series.points:
            findings.append(
                DeterministicFinding(
                    f"{identity.label} on {point.day.isoformat()}", Decimal(point.current)
                )
            )
            if point.prior is not None:
                findings.append(
                    DeterministicFinding(
                        f"{identity.label} aligned prior for {point.day.isoformat()}",
                        Decimal(point.prior),
                    )
                )
        for _fact_id, label, _day, value, _qualifier in _series_fact_values(
            ingredients.series, additive=ingredients.additive
        ):
            # The governed label alone names the fact: a snake_case ID in a
            # model-visible finding name could be echoed into rendered prose.
            findings.append(DeterministicFinding(f"{identity.label} fact: {label}", value))
    for candidate in ingredients.driver_candidates:
        findings.extend(
            (
                DeterministicFinding(
                    f"{identity.label} driver {candidate.name} current", Decimal(candidate.current)
                ),
                DeterministicFinding(
                    f"{identity.label} driver {candidate.name} aligned prior",
                    Decimal(candidate.prior),
                ),
            )
        )
    # A malformed source with duplicate labels is refused at the request object's
    # unique-name invariant rather than silently selecting one value.
    return tuple(findings)


# --- payload ---------------------------------------------------------------------------


def _payload_number(value: FocusNumber) -> int | str:
    return value if isinstance(value, int) else str(value)


def _units_payload(units: Units) -> dict[str, str | None]:
    return {"kind": units.kind.value, "currency_code": units.currency_code}


def _period_payload(period: DateRange) -> dict[str, str]:
    return {"start": period.start.isoformat(), "end": period.end.isoformat()}


def _value_payload(value: MetricFocusValue) -> dict[str, object]:
    return {
        "basis": value.basis.value,
        "display_value": metric_display_value(value.units, value.value),
        "grain": list(value.grain.dimensions),
        "period": _period_payload(value.period),
        "units": _units_payload(value.units),
        "value": _payload_number(value.value),
    }


def _scope_payload(scope: MetricFocusDriverScope | None) -> dict[str, object] | None:
    if scope is None:
        return None
    return {
        "count": scope.count,
        "population": scope.population,
        "label": scope.label,
        "share_suffix": scope.share_suffix,
    }


def _summary_change_payload(change: MetricFocusSummaryChange | None) -> dict[str, str] | None:
    return None if change is None else {"value": str(change.value), "display": change.display}


def _summary_payload(summary: MetricFocusDriversSummary | None) -> dict[str, object] | None:
    if summary is None:
        return None
    return {
        "listed_change": _summary_change_payload(summary.listed_change),
        "other_in_scope_change": _summary_change_payload(summary.other_in_scope_change),
        "outside_scope_change": _summary_change_payload(summary.outside_scope_change),
        "rose": summary.rose,
        "fell": summary.fell,
        "flat": summary.flat,
        "lines": [text for _, text in summary.lines],
    }


def metric_focus_payload(artifact: MetricFocusArtifact) -> dict[str, object]:
    """Encode the exact v4 focus contract; strict decoders on both sides pin it."""
    identity = artifact.identity
    return {
        "focus_version": METRIC_FOCUS_VERSION,
        "metric": {
            "id": identity.metric_id,
            "label": identity.label,
            "definition": identity.definition,
            "unit": _units_payload(identity.units),
            "basis": identity.basis.value,
            "grain": list(identity.grain.dimensions),
        },
        "period": _period_payload(artifact.period),
        "prior_period": _period_payload(artifact.prior_period),
        "current": _value_payload(artifact.current),
        "prior": _value_payload(artifact.prior),
        "change": change_payload(artifact.change),
        "series": None
        if artifact.series is None
        else {
            "points": [
                {
                    "date": point.day.isoformat(),
                    "current": _payload_number(point.current),
                    "prior": None if point.prior is None else _payload_number(point.prior),
                }
                for point in artifact.series.points
            ],
            "display_grain": artifact.series.display_grain,
        },
        "facts": [
            {
                "id": fact.fact_id,
                "label": fact.label,
                "value": fact.value,
                "qualifier": fact.qualifier,
            }
            for fact in artifact.facts
        ],
        "drivers": {"absent_reason": artifact.drivers.absent_reason}
        if isinstance(artifact.drivers, MetricFocusDriversAbsent)
        else {
            "grain": artifact.drivers.grain.value,
            "scope": _scope_payload(artifact.drivers.scope),
            "share_basis": None
            if artifact.drivers.share_basis is None
            else artifact.drivers.share_basis.value,
            "entries": [
                {
                    "name": entry.name,
                    "change": _payload_number(entry.change),
                    # Each entry carries its own units: a ratio component may differ from
                    # the focused metric, and only a currency entry has a display change.
                    "units": _units_payload(entry.units),
                    "display_change": (
                        currency_display_value(entry.change)
                        if entry.units.kind is UnitKind.CURRENCY
                        else None
                    ),
                    "delta": change_payload(entry.delta),
                    "share_of_move": entry.share_of_move,
                    "moves_against_basis": entry.moves_against_basis,
                    "chip": entry.chip,
                }
                for entry in artifact.drivers.entries
            ],
        },
        "drivers_summary": _summary_payload(artifact.drivers_summary),
        "insight": insight_slots_payload((artifact.insight,))[0],
        "neighbors": list(artifact.neighbors),
        "next_check": None
        if artifact.next_check is None
        else {"metric_id": artifact.next_check.metric_id, "label": artifact.next_check.label},
        "boundary": artifact.boundary,
    }


def direction_counts(changes: tuple[Decimal | int, ...]) -> tuple[int, int, int]:
    """Count the changes that rose, fell and were flat."""
    return (
        sum(1 for change in changes if change > 0),
        sum(1 for change in changes if change < 0),
        sum(1 for change in changes if change == 0),
    )


def direction_counts_sentence(changes: tuple[Decimal | int, ...]) -> str:
    """State how N movements split by direction (one home, design §4.6)."""
    rose, fell, flat = direction_counts(changes)
    return f"Among these {len(changes)}: {rose} rose, {fell} fell, {flat} were flat"


# --- surface extractors ----------------------------------------------------------------

CAMPAIGN_PORTFOLIO_FOCUS_METRICS = (
    CampaignPortfolioMetric.SPEND.value,
    CampaignPortfolioMetric.PURCHASES.value,
    CampaignPortfolioMetric.IMPRESSIONS.value,
    CampaignPortfolioMetric.PORTFOLIO_ROAS.value,
)
"""The portfolio tiles, in the artifact's declared totals order."""

MULTI_CHANNEL_PORTFOLIO_FOCUS_METRICS = (
    CampaignPortfolioMetric.SPEND.value,
    CampaignPortfolioMetric.IMPRESSIONS.value,
)
"""A multi-channel option's focusable tiles: purchases and ROAS aren't added (ADR-127)."""


def portfolio_focus_metrics(artifact: CampaignPortfolioArtifact) -> tuple[str, ...]:
    """Return the focus metrics one sibling declares; no attributed total across channels."""
    return (
        MULTI_CHANNEL_PORTFOLIO_FOCUS_METRICS
        if artifact.multi_channel
        else CAMPAIGN_PORTFOLIO_FOCUS_METRICS
    )


RETAIL_FOCUS_METRICS = RETAIL_HEALTH_TOTAL_METRICS
"""Every retail outcome and derived tile; typed-absent tiles render no opener."""

CAMPAIGN_DETAIL_FOCUS_METRICS = (
    "purchases",
    "attributed_roas",
    "spend",
    "impressions",
    "clicks",
    "ctr",
    "cpc",
    "cpm",
)
"""The detail tiles, in the page's declared order."""

_PORTFOLIO_ROW_CARRIED = {
    CampaignPortfolioMetric.SPEND.value,
    CampaignPortfolioMetric.PURCHASES.value,
}
_RETAIL_ADDITIVE_ROWS = frozenset(
    {
        "retail_revenue_shipped",
        "units_shipped",
        "customer_returns",
        "glance_views",
        "amazon_margin_proxy",
    }
)
_FOCUS_GOVERNED_METRICS: dict[str, str] = {
    **{metric.value: governed for metric, governed in CAMPAIGN_PORTFOLIO_GOVERNED_METRICS.items()},
    **{slot.value: governed for slot, governed in CAMPAIGN_SUMMARY_GOVERNED_METRICS.items()},
}


def focus_governed_metric(metric_id: str) -> str:
    """Name the dictionary metric a focus or driver id measures; other ids already are one."""
    return _FOCUS_GOVERNED_METRICS.get(metric_id, metric_id)


_RETAIL_RATIO_COMPONENTS: dict[str, tuple[tuple[str, str], ...]] = {
    # metric -> ((component metric id carried in totals, display label), ...)
    "return_rate": (
        ("customer_returns", "Returned units"),
        ("units_shipped", "Shipped units"),
    ),
    "amazon_margin_proxy": (
        ("retail_revenue_shipped", "Shipped revenue"),
        ("cogs_shipped", "COGS shipped"),
    ),
    "asp_shipped": (
        ("retail_revenue_shipped", "Shipped revenue"),
        ("units_shipped", "Shipped units"),
    ),
    "revenue_per_glance_view": (
        ("retail_revenue_shipped", "Shipped revenue"),
        ("glance_views", "Glance views"),
    ),
}


def campaign_portfolio_focus_ingredients(
    artifact: CampaignPortfolioArtifact, metric_id: str
) -> MetricFocusIngredients:
    """Derive one portfolio focus from the exact baked artifact the page was served from."""
    metric = CampaignPortfolioMetric(metric_id)
    comparison = next((item for item in artifact.totals if item.metric is metric), None)
    if comparison is None:
        # A multi-channel sibling totals spend and impressions only (ADR-127).
        raise ValueError("the focused portfolio metric is not carried on this artifact.")
    current = MetricFocusValue(
        comparison.current.number,
        comparison.current.units,
        comparison.current.period,
        comparison.current.grain,
        MeasurementBasis.AD_ATTRIBUTED,
    )
    prior = MetricFocusValue(
        comparison.prior.number,
        comparison.prior.units,
        comparison.prior.period,
        comparison.prior.grain,
        MeasurementBasis.AD_ATTRIBUTED,
    )
    series: MetricFocusSeries | None = None
    trend = artifact.trend
    if trend is not None and metric in {
        CampaignPortfolioMetric.SPEND,
        CampaignPortfolioMetric.PORTFOLIO_ROAS,
        CampaignPortfolioMetric.PURCHASES,
    }:
        values = tuple(
            point.spend
            if metric is CampaignPortfolioMetric.SPEND
            else point.portfolio_roas
            if metric is CampaignPortfolioMetric.PORTFOLIO_ROAS
            else point.purchases
            for point in trend.points
        )
        # A spend-only trend has no other series, and a day with no spend has no ROAS: the
        # focus then shows no series rather than a gap read as zero.
        if all(value is not None for value in values):
            series = MetricFocusSeries(
                tuple(
                    MetricFocusSeriesPoint(point.day, cast("FocusNumber", value), None)
                    for point, value in zip(trend.points, values, strict=True)
                )
            )
    candidates: tuple[MetricFocusDriverCandidate, ...] = ()
    absent_reason: str | None = None
    population: int | None = None
    grain = MetricFocusDriverGrain.CAMPAIGN
    if metric.value in _PORTFOLIO_ROW_CARRIED and artifact.rows:
        labels = portfolio_row_operand_labels(artifact.rows)
        candidates = tuple(
            MetricFocusDriverCandidate(
                labels[index],
                Decimal(row.comparison_for(metric).current.number),
                Decimal(row.comparison_for(metric).prior.number),
                units=row.comparison_for(metric).current.units,
            )
            for index, row in enumerate(artifact.rows)
        )
        population = campaign_population(artifact)
    elif metric is CampaignPortfolioMetric.PORTFOLIO_ROAS:
        grain = MetricFocusDriverGrain.RATIO_COMPONENT
        absent_reason = (
            "Portfolio ROAS decomposes into attributed revenue over spend, and this page's "
            "artifact does not carry the revenue operand. Open a campaign for its own ratio."
        )
    else:
        absent_reason = (
            "This page's campaign rows do not carry per-campaign values for this metric, "
            "so there is nothing to decompose here."
        )
    return MetricFocusIngredients(
        metric_id=metric.value,
        period=artifact.period,
        prior_period=artifact.prior_period,
        current=current,
        prior=prior,
        series=series,
        drivers_grain=grain,
        driver_candidates=candidates,
        drivers_absent_reason=absent_reason,
        neighbors=portfolio_focus_metrics(artifact),
        boundary=None,
        additive=metric is not CampaignPortfolioMetric.PORTFOLIO_ROAS,
        driver_population=population,
    )


def retail_health_focus_ingredients(
    artifact: RetailHealthArtifact, metric_id: str
) -> MetricFocusIngredients:
    """Derive one retail focus from the exact baked artifact the page was served from."""
    comparison = next((item for item in artifact.totals if item.metric == metric_id), None)
    if comparison is None:
        raise ValueError("the focused retail metric is not carried on this artifact.")
    current = MetricFocusValue(
        comparison.current.value,
        comparison.current.units,
        comparison.current.period,
        comparison.current.grain,
        MeasurementBasis.RETAIL_ACTUAL,
    )
    prior = MetricFocusValue(
        comparison.prior.value,
        comparison.prior.units,
        comparison.prior.period,
        comparison.prior.grain,
        MeasurementBasis.RETAIL_ACTUAL,
    )
    series: MetricFocusSeries | None = None
    if metric_id in RETAIL_HEALTH_TREND_METRICS and artifact.trend:
        index = RETAIL_HEALTH_TREND_METRICS.index(metric_id)
        series = MetricFocusSeries(
            tuple(
                MetricFocusSeriesPoint(
                    point.day, point.current[index].value, point.prior[index].value
                )
                for point in artifact.trend
            )
        )
    candidates: tuple[MetricFocusDriverCandidate, ...] = ()
    absent_reason: str | None = None
    grain = MetricFocusDriverGrain.PRODUCT
    totals_by_metric = {item.metric: item for item in artifact.totals}
    if metric_id in _RETAIL_RATIO_COMPONENTS and metric_id != "amazon_margin_proxy":
        grain = MetricFocusDriverGrain.RATIO_COMPONENT
        candidates = tuple(
            MetricFocusDriverCandidate(
                label,
                Decimal(totals_by_metric[component].current.value),
                Decimal(totals_by_metric[component].prior.value),
                metric_id=component,
                units=totals_by_metric[component].current.units,
            )
            for component, label in _RETAIL_RATIO_COMPONENTS[metric_id]
            if component in totals_by_metric
        )
        if not candidates:
            absent_reason = (
                "This ratio's operands are not carried on this page's artifact, so there is "
                "nothing to decompose here."
            )
    elif metric_id == "returns_value_estimate_retail":
        grain = MetricFocusDriverGrain.RATIO_COMPONENT
        returns = totals_by_metric.get("customer_returns")
        revenue = totals_by_metric.get("retail_revenue_shipped")
        units = totals_by_metric.get("units_shipped")
        if returns is not None and revenue is not None and units is not None:
            current_asp = _window_asp(revenue.current.value, units.current.value)
            prior_asp = _window_asp(revenue.prior.value, units.prior.value)
            candidates = (
                MetricFocusDriverCandidate(
                    "Returned units",
                    Decimal(returns.current.value),
                    Decimal(returns.prior.value),
                    metric_id="customer_returns",
                    units=returns.current.units,
                ),
                MetricFocusDriverCandidate(
                    "Average selling price",
                    current_asp,
                    prior_asp,
                    units=revenue.current.units,
                    change_metric_id=FOCUS_DERIVED_ASP_METRIC,
                ),
            )
        else:  # pragma: no cover — a complete artifact always carries the operands
            absent_reason = "The estimate's operands are not carried on this page's artifact."
    elif metric_id in {"fulfilment_rate", "conversion_rate_retail"}:
        grain = MetricFocusDriverGrain.RATIO_COMPONENT
        absent_reason = (
            "This ratio's ordered-units operand is not carried on this page's artifact, so "
            "there is nothing to decompose here."
        )
    elif metric_id in RETAIL_HEALTH_ROW_METRICS and metric_id in _RETAIL_ADDITIVE_ROWS:
        row_index = RETAIL_HEALTH_ROW_METRICS.index(metric_id)
        candidates = tuple(
            MetricFocusDriverCandidate(
                f"{row.title} ({row.child_asin})",
                Decimal(row.comparisons[row_index].current.value),
                Decimal(row.comparisons[row_index].prior.value),
                units=row.comparisons[row_index].current.units,
            )
            for row in artifact.rows
        )
        if not candidates:
            absent_reason = "No product rows are served for this window."
    else:
        absent_reason = (
            "This page's product rows do not carry per-product values for this metric, so "
            "there is nothing to decompose here."
        )
    return MetricFocusIngredients(
        metric_id=metric_id,
        period=artifact.period,
        prior_period=artifact.prior_period,
        current=current,
        prior=prior,
        series=series,
        drivers_grain=grain,
        driver_candidates=candidates,
        drivers_absent_reason=absent_reason,
        neighbors=tuple(item.metric for item in artifact.totals),
        boundary=artifact.boundary,
        additive=comparison.current.units.kind is not UnitKind.RATIO,
        driver_population=len(artifact.rows) if candidates else None,
    )


def retail_product_focus_ingredients(
    detail: RetailProductDetailArtifact, metric_id: str
) -> MetricFocusIngredients:
    """Derive one product tile's focus from the exact baked child the page shows (ADR-125)."""
    comparison = next((item for item in detail.totals if item.metric == metric_id), None)
    if comparison is None:
        raise ValueError("the focused product metric is not carried on this artifact.")
    totals_by_metric = {item.metric: item for item in detail.totals}
    daily = {
        "retail_revenue_shipped": detail.sales_daily,
        "glance_views": detail.traffic_daily,
    }.get(metric_id)
    series = (
        None
        if daily is None
        else MetricFocusSeries(
            tuple(
                MetricFocusSeriesPoint(point.day, point.current.value, point.prior.value)
                for point in daily
            )
        )
    )
    candidates: tuple[MetricFocusDriverCandidate, ...] = ()
    absent_reason: str | None = None
    grain = MetricFocusDriverGrain.PRODUCT
    if metric_id in _RETAIL_RATIO_COMPONENTS and metric_id != "amazon_margin_proxy":
        grain = MetricFocusDriverGrain.RATIO_COMPONENT
        candidates = tuple(
            MetricFocusDriverCandidate(
                label,
                Decimal(totals_by_metric[component].current.value),
                Decimal(totals_by_metric[component].prior.value),
                metric_id=component,
                units=totals_by_metric[component].current.units,
            )
            for component, label in _RETAIL_RATIO_COMPONENTS[metric_id]
            if component in totals_by_metric
        )
    if not candidates:
        absent_reason = (
            "This page shows one product, so there are no smaller parts to compare here."
        )
    return MetricFocusIngredients(
        metric_id=metric_id,
        period=detail.period,
        prior_period=detail.prior_period,
        current=MetricFocusValue(
            comparison.current.value,
            comparison.current.units,
            comparison.current.period,
            comparison.current.grain,
            MeasurementBasis.RETAIL_ACTUAL,
        ),
        prior=MetricFocusValue(
            comparison.prior.value,
            comparison.prior.units,
            comparison.prior.period,
            comparison.prior.grain,
            MeasurementBasis.RETAIL_ACTUAL,
        ),
        series=series,
        drivers_grain=grain,
        driver_candidates=candidates,
        drivers_absent_reason=absent_reason,
        neighbors=tuple(item.metric for item in detail.totals),
        boundary=" ".join(detail.boundaries),
        additive=comparison.current.units.kind is not UnitKind.RATIO,
        driver_population=None,
    )


def _window_asp(revenue: FocusNumber, units: FocusNumber) -> Decimal:
    if not Decimal(units):
        return Decimal(0)
    return (Decimal(revenue) / Decimal(units)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)


_DETAIL_SUMMARY_SLOTS = {
    "purchases": CampaignSummarySlot.PURCHASES,
    "attributed_roas": CampaignSummarySlot.ATTRIBUTED_ROAS,
    "spend": CampaignSummarySlot.SPEND,
}
_DETAIL_DELIVERY_SLOTS = {
    "spend": CampaignDeliverySlot.AD_SPEND,
    "impressions": CampaignDeliverySlot.IMPRESSIONS,
    "clicks": CampaignDeliverySlot.CLICKS,
    "ctr": CampaignDeliverySlot.CTR,
    "cpc": CampaignDeliverySlot.CPC,
    "cpm": CampaignDeliverySlot.CPM,
}
_DETAIL_RATIO_COMPONENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "ctr": (("clicks", "Clicks"), ("impressions", "Impressions")),
    "cpc": (("spend", "Spend"), ("clicks", "Clicks")),
    "cpm": (("spend", "Spend"), ("impressions", "Impressions")),
}
_DETAIL_RATIOS = frozenset({"attributed_roas", "ctr", "cpc", "cpm"})


@dataclass(frozen=True, slots=True)
class CampaignDetailFocusRequest:
    """The redeemed detail evidence one focus derives from; never a new measurement."""

    detail: CampaignAttributionDetailArtifact
    delivery: CampaignDelivery
    metric_id: str


def _detail_value(
    request: CampaignDetailFocusRequest, metric_id: str, *, prior: bool
) -> MetricFocusValue | None:
    """Read one exact served value for a detail metric from summary or delivery."""
    summary_slot = _DETAIL_SUMMARY_SLOTS.get(metric_id)
    if summary_slot is not None:
        if prior:
            comparison = request.detail.comparison
            if comparison is None:
                return None
            item = next(
                (metric for metric in comparison.metrics if metric.slot is summary_slot), None
            )
            value = item.prior if item is not None else None
        else:
            value = request.detail.summary.value_for(summary_slot)
        if value is None:
            return None
        return MetricFocusValue(value.value, value.units, value.period, value.grain, value.basis)
    delivery_slot = _DETAIL_DELIVERY_SLOTS.get(metric_id)
    if delivery_slot is None:
        return None
    roster = request.delivery.comparison if prior else request.delivery.summary
    item = next((value for value in roster if value.slot is delivery_slot), None)
    if item is None or item.value is None:
        return None
    value = item.value
    return MetricFocusValue(value.value, value.units, value.period, value.grain, value.basis)


def _detail_series(request: CampaignDetailFocusRequest, metric_id: str) -> MetricFocusSeries | None:
    """Read the served daily evidence for one detail metric, with prior where carried."""
    if metric_id == "purchases":
        series = request.detail.purchase_series
        if series is None or not series.points:
            return None
        return MetricFocusSeries(
            tuple(
                MetricFocusSeriesPoint(point.day, point.purchases, None) for point in series.points
            )
        )
    delivery_slot = _DETAIL_DELIVERY_SLOTS.get(metric_id)
    if delivery_slot is None or not request.delivery.series_points:
        return None
    points: list[MetricFocusSeriesPoint] = []
    for row in request.delivery.series_points:
        raw_day = row.get("date")
        current_values = row.get("current")
        prior_values = row.get("prior")
        if (
            not isinstance(raw_day, str)
            or not isinstance(current_values, dict)
            or not isinstance(prior_values, dict)
        ):  # pragma: no cover — CampaignDelivery construction guarantees the row shape
            return None
        points.append(
            MetricFocusSeriesPoint(
                date_type.fromisoformat(raw_day),
                current_values[delivery_slot.value],  # type: ignore[arg-type]
                prior_values[delivery_slot.value],  # type: ignore[arg-type]
            )
        )
    return MetricFocusSeries(tuple(points))


def campaign_detail_focus_ingredients(
    request: CampaignDetailFocusRequest,
) -> MetricFocusIngredients:
    """Derive one detail focus from the redeemed detail and delivery evidence."""
    metric_id = request.metric_id
    current = _detail_value(request, metric_id, prior=False)
    prior = _detail_value(request, metric_id, prior=True)
    if current is None or prior is None:
        raise ValueError("the focused detail metric is not served for this selection.")
    series = _detail_series(request, metric_id)
    candidates: tuple[MetricFocusDriverCandidate, ...] = ()
    absent_reason: str | None = None
    if metric_id in _DETAIL_RATIO_COMPONENTS:
        grain = MetricFocusDriverGrain.RATIO_COMPONENT
        components: list[MetricFocusDriverCandidate] = []
        for component_id, label in _DETAIL_RATIO_COMPONENTS[metric_id]:
            component_current = _detail_value(request, component_id, prior=False)
            component_prior = _detail_value(request, component_id, prior=True)
            if component_current is None or component_prior is None:
                components = []
                break
            components.append(
                MetricFocusDriverCandidate(
                    label,
                    Decimal(component_current.value),
                    Decimal(component_prior.value),
                    metric_id=component_id,
                    units=component_current.units,
                )
            )
        candidates = tuple(components)
        if not candidates:
            absent_reason = (
                "This ratio's operands are not both served for this selection, so there is "
                "nothing to decompose here."
            )
    elif metric_id == "attributed_roas":
        grain = MetricFocusDriverGrain.RATIO_COMPONENT
        absent_reason = (
            "Attributed ROAS decomposes into attributed revenue over spend, and this page "
            "does not carry the revenue operand."
        )
    else:
        grain = MetricFocusDriverGrain.DAY
        if series is None:
            absent_reason = (
                "A daily series is not served for this metric, so there are no day steps "
                "to decompose."
            )
    return MetricFocusIngredients(
        metric_id=metric_id,
        period=request.detail.period,
        prior_period=prior.period,
        current=current,
        prior=prior,
        series=series,
        drivers_grain=grain,
        driver_candidates=candidates,
        drivers_absent_reason=absent_reason,
        neighbors=CAMPAIGN_DETAIL_FOCUS_METRICS,
        boundary=None,
        additive=metric_id not in _DETAIL_RATIOS,
    )
