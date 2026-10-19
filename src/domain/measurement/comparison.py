# SPDX-License-Identifier: MIT-0
"""Period comparison and ranking, over figures that have already been measured.

Specified in docs/capabilities/conversational-runtime/architecture.md §5. Two operations, not
two metrics: ADR-032 shapes tools by what a caller wants done rather than by which metric it
is done to, so everything here works for all fifty and none of it knows any of their names.

**Deliberately downstream of `figures.measure`.** Both operations consume `Measurement` objects,
so neither knows what a `QueryPlan` is, how many queries produced it, or whether the rows came
from Athena. That is what makes the arithmetic below testable from two hand-built measurements,
and it is why the honesty rules in `figures.py` are inherited rather than restated — a figure that
was withheld or whose denominator was zero never became a `Figure`, so it cannot be compared or
ranked here either.

Three rules live here because each is a place a plausible wrong number would otherwise appear.

**A key present in only one window yields no change.** A `Measurement` records *how many* rows
carried no figure, never *which*, so a key missing from the prior window is either a period that
genuinely had no rows or a row dropped because Amazon withheld it — and those demand opposite
answers. Treating the absence as zero would report a campaign whose prior figure was suppressed as
having grown from nothing, which is the same fabrication `_side_value` refuses one layer down. Both
sides are still reported; only the derived change is withheld, and the count is stated.

**A prior of zero yields no percentage.** Growth from nothing has no percentage — the division is
undefined and infinity is not a figure. The absolute change is still exact and is still reported,
which is the difference between this and the case above.

**A grand total is only stated for a metric that adds.** Ten campaigns of five hundred with no
denominator invites "so that is most of my spend", which is why section 5.2 requires the total. But
summing ROAS across campaigns is not a quantity, and summing `reach_users` double-counts every
shopper who saw two campaigns. So the total is produced for an additive metric in countable or
monetary units and refused otherwise, and the narrative says which happened rather than showing a
number with no meaning.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import cast

from domain.measurement.figures import (
    MEASURE_ABSENT,
    MIXED_CURRENCY,
    Figure,
    Measurement,
    total_of,
)
from domain.narrative.shaped_response import Units
from domain.shared_kernel.errors import ErrorCode, InvariantError, ValidationError
from domain.shared_kernel.values import Grain

__all__ = [
    "CHANGE_ALIAS",
    "CHANGE_PERCENT_ALIAS",
    "CURRENT_ALIAS",
    "PRIOR_ALIAS",
    "RANK_ALIAS",
    "UNRANKABLE",
    "ComparedFigure",
    "Comparison",
    "RankOrder",
    "RankRequest",
    "Ranking",
    "absolute_change",
    "compare",
    "percentage_change",
    "rank",
]

UNRANKABLE = ErrorCode(area="MEASURE", number=4)
"""A ranking was asked for in terms it cannot be produced in.

`ValidationError` rather than `InvariantError`: every case is a caller asking for something
outside the operation's contract — a size of zero, or an ordering by change with nothing to
compare against — and the caller can fix all of them.
"""

CURRENT_ALIAS = "current"
PRIOR_ALIAS = "prior"
CHANGE_ALIAS = "change"
CHANGE_PERCENT_ALIAS = "change_percent"
"""What a compared row calls its four numbers.

Named rather than reusing `MEASURE_ALIAS`, because a comparison row carries four figures where a
breakdown carries one, and calling any of them `value` would leave the model to guess which.
"""

RANK_ALIAS = "rank"
"""The position a ranked row holds, starting at 1.

Stated explicitly rather than left implicit in tuple order. JSON preserves array order, but a model
relaying "the third one" from position alone is inferring where it could be reading.
"""


@dataclass(frozen=True, slots=True)
class ComparedFigure:
    """One key, in two periods, with the change between them where there is one.

    `current` and `prior` are `None` when the key produced no figure in that period. `change` and
    `change_percent` are `None` whenever they cannot be derived honestly — see the module
    docstring for the two separate reasons.
    """

    dimensions: tuple[tuple[str, str], ...]
    current: Decimal | int | None
    prior: Decimal | int | None
    change: Decimal | int | None
    change_percent: str | None
    """The relative change, pre-formatted with its sign and a percent sign, or `None`.

    **A string, and the only pre-formatted number in the platform.** `ResponseData.units`
    denominates a response's figures, and a percentage derived from them is a different
    denomination — so a bare `12.53` would sit in the row beside a monetary `1234.0000` looking
    like the same kind of number, under units that describe only the other three. Carrying the
    sign and the symbol makes the value self-denominating, which is what lets the envelope keep
    one `Units` rather than growing a second for one tool.
    """

    @property
    def is_comparable(self) -> bool:
        """Whether both periods produced a figure, so a change exists at all."""
        return self.change is not None

    def labelled(self) -> dict[str, str | int | Decimal | None]:
        """Flatten into the row shape `ResponseData.values` carries."""
        row: dict[str, str | int | Decimal | None] = dict(self.dimensions)
        row[CURRENT_ALIAS] = self.current
        row[PRIOR_ALIAS] = self.prior
        row[CHANGE_ALIAS] = self.change
        row[CHANGE_PERCENT_ALIAS] = self.change_percent
        return row


@dataclass(frozen=True, slots=True)
class Comparison:
    """Every key either period reported, and what changed.

    Ordered by dimensions rather than by change, deliberately. A comparison answers "what did each
    of these do", and ordering it by the size of the movement would answer the ranking question
    without being asked — which is `rank` with `order_by: CHANGE`, and which carries the grand
    total that makes a ranking honest.
    """

    rows: tuple[ComparedFigure, ...]
    units: Units | None
    grain: Grain

    def __post_init__(self) -> None:
        """Tie units to rows, the same way `Measurement` and `ResponseData` do.

        Third statement of one rule: a label for figures is absent exactly when the figures are.
        Asserted here rather than left to the envelope so a comparison that violated it named the
        comparison rather than the response two layers later.
        """
        if self.rows and self.units is None:
            raise InvariantError(
                MEASURE_ABSENT,
                "A comparison carries rows with no units to state them in.",
                context={"rows": len(self.rows)},
            )
        if not self.rows and self.units is not None:
            raise InvariantError(
                MEASURE_ABSENT,
                "A comparison states units for no rows, which is a claim about absent data.",
                context={"units": self.units.currency_code or self.units.kind.value},
            )

    @property
    def row_count(self) -> int:
        """How many keys the comparison carries."""
        return len(self.rows)

    @property
    def comparable(self) -> tuple[ComparedFigure, ...]:
        """The keys that appeared in both periods, so a change could be derived."""
        return tuple(row for row in self.rows if row.is_comparable)

    @property
    def one_sided(self) -> int:
        """How many keys appeared in only one of the two periods."""
        return len(self.rows) - len(self.comparable)

    @property
    def is_empty(self) -> bool:
        """Whether neither period reported anything.

        An answer rather than a zero, for the reason `Measurement.is_empty` gives.
        """
        return not self.rows


def compare(current: Measurement, prior: Measurement) -> Comparison:
    """Pair two periods' figures by key, deriving the change where one exists.

    Both measurements must describe the same grain and the same denomination. Neither is a
    caller's choice — the tool plans one metric at one grain over two windows — so a mismatch is
    a state this cannot be reached in and raises rather than being reconciled.
    """
    grain = _one_grain(current, prior)
    units = _one_denomination(current, prior)

    by_key = {figure.dimensions: figure for figure in prior.figures}
    rows = [_compared(figure, by_key.pop(figure.dimensions, None)) for figure in current.figures]
    # Whatever is left appeared only in the prior period. Carried rather than dropped: a campaign
    # that ran last month and not this one is exactly what somebody comparing two periods is
    # looking for, and omitting it would make the current period look unchanged.
    rows.extend(_compared(None, remaining) for remaining in by_key.values())

    return Comparison(
        rows=tuple(sorted(rows, key=lambda row: row.dimensions)), units=units, grain=grain
    )


def _compared(current: Figure | None, prior: Figure | None) -> ComparedFigure:
    """Build one compared row from whichever sides exist."""
    present = current if current is not None else prior
    if present is None:  # pragma: no cover — one side is always supplied by `compare`
        raise InvariantError(
            MEASURE_ABSENT,
            "A comparison row was built from two absent periods.",
            context={"problem": "at least one side must carry a figure"},
        )

    if current is None or prior is None:
        return ComparedFigure(
            dimensions=present.dimensions,
            current=current.value if current is not None else None,
            prior=prior.value if prior is not None else None,
            change=None,
            change_percent=None,
        )

    return ComparedFigure(
        dimensions=current.dimensions,
        current=current.value,
        prior=prior.value,
        change=absolute_change(current.value, prior.value),
        change_percent=percentage_change(current.value, prior.value),
    )


def _subtraction_precision(current: Decimal, prior: Decimal) -> int:
    """Return the context precision required to preserve every subtraction digit."""
    current_exponent = cast("int", current.as_tuple().exponent)
    prior_exponent = cast("int", prior.as_tuple().exponent)
    return max(
        28,
        max(current.adjusted(), prior.adjusted()) - min(current_exponent, prior_exponent) + 2,
    )


def absolute_change(current: Decimal | int, prior: Decimal | int) -> Decimal | int:
    """Subtract period values exactly, keeping a represented count integral."""
    if isinstance(current, int) and isinstance(prior, int):
        return current - prior
    current_decimal = Decimal(current)
    prior_decimal = Decimal(prior)
    with localcontext() as context:
        context.prec = _subtraction_precision(current_decimal, prior_decimal)
        return current_decimal - prior_decimal


def _percentage_hundredths(current: Decimal | int, prior: Decimal | int) -> tuple[bool, int]:
    """Round an exact percentage fraction to hundredths using Decimal's half-even rule."""
    current_numerator, current_denominator = Decimal(current).as_integer_ratio()
    prior_numerator, prior_denominator = Decimal(prior).as_integer_ratio()
    numerator = 100 * (
        current_numerator * prior_denominator - prior_numerator * current_denominator
    )
    denominator = current_denominator * prior_numerator
    if denominator < 0:
        numerator = -numerator
        denominator = -denominator
    negative = numerator < 0
    absolute_numerator = -numerator if negative else numerator
    quotient, remainder = divmod(absolute_numerator * 100, denominator)
    twice_remainder = remainder * 2
    if twice_remainder > denominator or (twice_remainder == denominator and quotient % 2 == 1):
        quotient += 1
    return negative, quotient


def percentage_change(current: Decimal | int, prior: Decimal | int) -> str | None:
    """Format exact relative change, or return none when the prior value is zero.

    The zero test is numeric, never truthiness: a canonical decimal text such as ``"0"`` or
    ``"0.0000"`` reaching an artifact decoder is truthy as a string, and the deployed retail
    1D page 500'd on exactly that when a product's prior-day derived rate was zero.
    """
    if Decimal(prior) == 0:
        return None
    negative, hundredths = _percentage_hundredths(current, prior)
    whole, fraction = divmod(hundredths, 100)
    return f"{'-' if negative else '+'}{whole}.{fraction:02d}%"


def _one_grain(current: Measurement, prior: Measurement) -> Grain:
    """Take the grain both periods share, refusing two that disagree."""
    if current.grain != prior.grain:
        raise InvariantError(
            MEASURE_ABSENT,
            "Two periods were compared at different grains.",
            context={"current": str(current.grain), "prior": str(prior.grain)},
        )
    return current.grain


def _one_denomination(current: Measurement, prior: Measurement) -> Units | None:
    """Take the units both periods share, refusing two that disagree.

    Either side may be absent, because `Measurement.units` is `None` exactly when that period
    produced no figures — a window holding nothing has nothing to denominate, and inventing a
    denomination for it is what `_units_for` refuses. So the units of a comparison are whichever
    period stated them, and `None` only when neither did.

    Two *different* currencies is a refusal rather than a conversion, for the reason `Money`
    refuses to add across them: the only rate available here is today's, and ADR-004 requires the
    rate as of the fact's own date. It is also a real finding about the data rather than a caller
    mistake, since the same filters produced both.
    """
    if current.units is None or prior.units is None:
        return current.units or prior.units
    if current.units != prior.units:
        raise InvariantError(
            MIXED_CURRENCY,
            "The two periods are not denominated in the same units, so no change is correct.",
            context={
                "current": current.units.currency_code or current.units.kind.value,
                "prior": prior.units.currency_code or prior.units.kind.value,
            },
        )
    return current.units


class RankOrder(StrEnum):
    """What a ranking sorts by.

    `CHANGE` is a parameter rather than a fourth tool. "Which campaigns grew most" is a ranking
    whose sort key is a delta, so composing it here is what stops the model ranking a comparison
    itself — which it would do by reading four numbers per row and choosing, with no total to say
    what it left out.
    """

    VALUE = "VALUE"
    """Largest figure in the current period first."""

    CHANGE = "CHANGE"
    """Largest increase first. Requires a prior period to change against."""


@dataclass(frozen=True, slots=True)
class RankRequest:
    """Everything a ranking is derived from.

    `measurement` is the **whole** population rather than the rows to be shown, because the grand
    total is computed over all of it and the top n is selected from all of it. Handing this the
    already-shortened list is the mistake the field name exists to prevent.
    """

    measurement: Measurement
    n: int
    additive: bool
    """Whether summing this metric across rows is arithmetic rather than nonsense.

    Supplied because it is resolved from the metric dictionary through a ratio's operands, which
    is `Catalog.is_non_additive`'s job — a metric definition must never be restated in code, and
    `reach_users` is non-additive without its unit kind saying so.
    """

    order_by: RankOrder = RankOrder.VALUE
    comparison: Comparison | None = None
    """The two-period comparison, present exactly when ordering by change."""

    def __post_init__(self) -> None:
        if self.n < 1:
            raise ValidationError(
                UNRANKABLE,
                "A ranking needs a size of at least 1.",
                context={"n": self.n},
            )
        if self.order_by is RankOrder.CHANGE and self.comparison is None:
            raise ValidationError(
                UNRANKABLE,
                "Ranking by change needs a prior period to change against.",
                context={"order_by": self.order_by.value},
            )
        if self.order_by is RankOrder.VALUE and self.comparison is not None:
            raise ValidationError(
                UNRANKABLE,
                "A comparison was supplied for a ranking that orders by value, which would "
                "carry a prior period no row reports.",
                context={"order_by": self.order_by.value},
            )


@dataclass(frozen=True, slots=True)
class Ranking:
    """The rows a ranking shows, and what quantifies the ones it does not.

    `rows` is already in the envelope's row shape because this is the last domain step before the
    response, and both orderings produce different row shapes — a value ranking carries one figure
    per row and a change ranking carries four. Flattening here keeps that difference from becoming
    a union type every caller has to narrow.
    """

    rows: tuple[dict[str, str | int | Decimal | None], ...]
    units: Units | None
    grain: Grain
    ranked_by: RankOrder
    considered: int
    """How many keys the population held, ranked or not. The denominator of `excluded`."""

    def __post_init__(self) -> None:
        """Tie units to rows, so an empty ranking states no denomination.

        Reachable and not defensive: a change ranking over a population where every key appeared
        in only one period has a comparison that knows its currency and no rows to denominate,
        and the envelope refuses that pairing one layer later with no idea which tool produced it.
        """
        if self.rows and self.units is None:
            raise InvariantError(
                MEASURE_ABSENT,
                "A ranking carries rows with no units to state them in.",
                context={"rows": len(self.rows)},
            )
        if not self.rows and self.units is not None:
            raise InvariantError(
                MEASURE_ABSENT,
                "A ranking states units for no rows, which is a claim about absent data.",
                context={"units": self.units.currency_code or self.units.kind.value},
            )

    total: Decimal | int | None = None
    """Every key's figure added up, or `None` when adding them is not arithmetic.

    Absent for a ratio and for a non-additive metric. See the module docstring — a number here
    that did not mean anything would be worse than the omission, because it would read as the
    denominator the ranking is meant to be judged against.
    """

    shown: Decimal | int | None = None
    """The ranked rows' figures added up, absent under the same conditions as `total`.

    **Carried rather than left for the reader to add, and that is not a convenience.** A grand
    total on its own only answers "is this most of my spend" if somebody sums the rows above it,
    and the one reader guaranteed to be doing that is the model — which must never sum, divide or
    infer a figure. Both ends of the comparison therefore come pre-computed.
    """

    not_comparable: int = 0
    """How many keys a change ranking could not consider, because one period had no figure."""

    @property
    def excluded(self) -> int:
        """How many of the population's keys are not shown."""
        return self.considered - len(self.rows)


def rank(request: RankRequest) -> Ranking:
    """Take the largest n by the requested key, with the totals the rest are judged against.

    The grand total is computed before the rows are cut, over every key the population held. That
    ordering is the point of the operation: ten campaigns of five hundred with no denominator
    invites "so that is most of my spend", and only a figure covering all five hundred can
    contradict it.
    """
    if request.order_by is RankOrder.CHANGE:
        return _ranked_by_change(request)
    return _ranked_by_value(request)


def _ranked_by_value(request: RankRequest) -> Ranking:
    """Order the current period's figures, largest first."""
    measurement = request.measurement
    grain = measurement.grain
    shown = _top(sorted(measurement.figures, key=_by_value), request.n)
    ranked = Measurement(
        figures=tuple(figure for _, figure in shown), units=measurement.units, grain=grain
    )

    return Ranking(
        rows=tuple(_positioned(figure.labelled(), place) for place, figure in shown),
        units=measurement.units if shown else None,
        grain=grain,
        ranked_by=request.order_by,
        considered=measurement.row_count + measurement.undefined_rows,
        total=total_of(measurement, additive=request.additive),
        shown=total_of(ranked, additive=request.additive),
    )


def _ranked_by_change(request: RankRequest) -> Ranking:
    """Order by how much each key moved, largest increase first.

    Only the keys both periods reported are ordered. The rest have no change to sort by, and
    placing them at either end would assert a direction the data does not support — so they are
    excluded from the ranking and counted, and the narrative states how many.

    **The totals are still the current period's figures, not the changes.** A total of changes is
    a net movement, which is a different question and a misleading denominator for a ranking of
    growth: ten campaigns that grew by a hundred each sum to the same net as one that grew by a
    thousand while nine were flat.
    """
    comparison = request.comparison
    if comparison is None:  # pragma: no cover — refused by RankRequest.__post_init__
        raise ValidationError(UNRANKABLE, "Ranking by change needs a comparison.")

    grain = comparison.grain
    shown = _top(sorted(comparison.comparable, key=_by_change), request.n)
    population = _as_measurement(comparison.comparable, comparison, grain)
    ranked = _as_measurement(tuple(row for _, row in shown), comparison, grain)

    return Ranking(
        rows=tuple(_positioned(row.labelled(), place) for place, row in shown),
        units=comparison.units if shown else None,
        grain=grain,
        ranked_by=request.order_by,
        considered=comparison.row_count,
        total=total_of(population, additive=request.additive),
        shown=total_of(ranked, additive=request.additive),
        not_comparable=comparison.one_sided,
    )


def _as_measurement(
    rows: tuple[ComparedFigure, ...], comparison: Comparison, grain: Grain
) -> Measurement:
    """Read the current-period figures out of compared rows, as something totallable.

    Routed through `Measurement` rather than summed here so the monetary total goes through the
    one implementation that owns adding money — `Money.total`, which refuses a mixed currency.
    """
    return Measurement(figures=_current_figures(rows), units=comparison.units, grain=grain)


def _by_value(figure: Figure) -> tuple[Decimal, tuple[tuple[str, str], ...]]:
    """Sort largest first, breaking ties on the dimensions."""
    return (-Decimal(figure.value), figure.dimensions)


def _by_change(row: ComparedFigure) -> tuple[Decimal, tuple[tuple[str, str], ...]]:
    """Sort by the largest increase, breaking ties on the dimensions.

    `row.change` is never `None` here — `Comparison.comparable` is the only source — and the
    fallback exists so a type narrowing failure cannot become a sort that silently reorders.
    """
    return (-Decimal(row.change if row.change is not None else 0), row.dimensions)


def _current_figures(rows: tuple[ComparedFigure, ...]) -> tuple[Figure, ...]:
    """Read the current-period figure out of each compared row, for totalling.

    Every row reaching here is comparable, so `current` is present on all of them; the filter is
    what makes that provable rather than asserted.
    """
    return tuple(
        Figure(dimensions=row.dimensions, value=row.current)
        for row in rows
        if row.current is not None
    )


def _top[T](ordered: list[T], n: int) -> tuple[tuple[int, T], ...]:
    """Take the first n, numbered from 1.

    Ties are already broken by the sort's second key — the dimensions — so two equal figures come
    back in a stable order rather than whichever the query happened to yield. Without that, the
    same question could return a different tenth row on each call.
    """
    return tuple(enumerate(ordered[:n], start=1))


def _positioned(
    row: dict[str, str | int | Decimal | None], place: int
) -> dict[str, str | int | Decimal | None]:
    """Put the position on a row, first, so the model reads it before the figures."""
    return {RANK_ALIAS: place, **row}
