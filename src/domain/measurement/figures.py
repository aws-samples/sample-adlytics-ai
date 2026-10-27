# SPDX-License-Identifier: MIT-0
"""Turn query rows into figures that carry their own units, or refuse them.

The aggregation itself is SQL's job: a simple metric is `SUM` over one column, and doing it
again here would be a second implementation of addition. What this module owns is everything
that makes a returned number safe to present — that it exists, that it is a quantity rather
than a string, that a monetary amount states its currency, and that a set of amounts does not
quietly span two.

**Why this is not the adapter's job.** An adapter's contract is rows, and a row is
`dict[str, str | int | Decimal | None]` — the shape a query engine hands back. Deciding that
a missing measure is a defect rather than a zero, or that two currencies in one answer is a
refusal rather than a sum, is measurement semantics. It has to be testable without a query
engine, and it has to be identical whichever engine ran the query.

**Zero and absent are different answers, and this module keeps them different.** A row whose
measure is `NULL` is not a row worth zero. `revenue_shipped` is non-nullable in the contract,
so a null there means the query or the load is wrong, and turning it into `0` would report a
real product as having sold nothing. Absent rows are a separate matter and correctly invisible
here: a product with no sales in the window produces no row, and a tool must not invent one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from domain.measurement.planning import (
    CURRENCY_COLUMN,
    MEASURE_ALIAS,
    Combination,
    QueryPlan,
    measure_alias,
    withheld_alias,
    withheld_rows_alias,
)
from domain.narrative.shaped_response import UnitKind, Units
from domain.shared_kernel.errors import ErrorCode, InvariantError
from domain.shared_kernel.provenance import WithheldAmount, withheld_money_counts
from domain.shared_kernel.values import Grain, Money

__all__ = [
    "MEASURE_ABSENT",
    "MEASURE_NOT_A_QUANTITY",
    "MIXED_CURRENCY",
    "RATIO_SCALE",
    "CurrencyAccumulator",
    "Figure",
    "Measurement",
    "Withholding",
    "WithholdingAccumulator",
    "figure_from_row",
    "measure",
    "total_of",
    "varying_columns",
    "withholding_in",
]

# InvariantError, not DomainError: every one of these means the query and the model disagree.
# CDK builds the tables from the same contracts the planner reads, so a missing column or a
# null in a non-nullable measure is a deploy-versus-runtime mismatch, which alarms (ADR-010).
MEASURE_ABSENT = ErrorCode(area="MEASURE", number=1)
MEASURE_NOT_A_QUANTITY = ErrorCode(area="MEASURE", number=2)
MIXED_CURRENCY = ErrorCode(area="MEASURE", number=3)

RATIO_SCALE = Decimal("0.0001")
"""The scale a computed quotient is rounded to.

Four places, matching `decimal(38,4)` in the canonical model, so a derived figure carries the
same precision as a stored one. Rounded here because the tool pre-formats every number it
returns — handing a model a repeating decimal invites it to shorten the figure itself.
"""


@dataclass(frozen=True, slots=True)
class Figure:
    """One row of an answer: where it sits, and how much.

    `dimensions` is an ordered tuple rather than a mapping so a figure is hashable and two
    figures compare equal only when their dimensions match in order — the order is the plan's
    `group_by`, which is the order the answer's grain declares.
    """

    dimensions: tuple[tuple[str, str], ...]
    value: Decimal | int

    def labelled(self) -> dict[str, str | int | Decimal | None]:
        """Flatten into the row shape `ResponseData.values` carries."""
        row: dict[str, str | int | Decimal | None] = dict(self.dimensions)
        row[MEASURE_ALIAS] = self.value
        return row


@dataclass(frozen=True, slots=True)
class Measurement:
    """Every figure an answer contains, with the units and grain they share.

    `units` is built here rather than copied from the plan, because for currency it needs the
    code that came back in the rows. The plan knows a figure will be money; only the data
    knows which money.
    """

    figures: tuple[Figure, ...]
    units: Units | None
    grain: Grain

    undefined_rows: int = 0
    """How many rows the query returned that carry no figure.

    Only a quotient produces these, and only where the denominator was zero. Counted rather than
    discarded silently: a breakdown missing rows without saying so does not add up to its own
    total, and the reader has no way to tell. The narrative states the number.
    """

    def __post_init__(self) -> None:
        if self.figures and self.units is None:
            raise InvariantError(
                MEASURE_ABSENT,
                "Figures were produced with no units to state them in.",
                context={"problem": "units may only be absent when there are no figures"},
            )

    @property
    def row_count(self) -> int:
        """Give how many figures this measurement carries."""
        return len(self.figures)

    @property
    def is_empty(self) -> bool:
        """Whether the window genuinely contained nothing.

        A legitimate answer, and not the same as zero. ADR-010 makes "we looked and there was
        nothing there" a valid response; a zero total would claim a product sold nothing.
        """
        return not self.figures

    def varying(self, columns: tuple[str, ...]) -> tuple[str, ...]:
        """Name the columns, of those given, on which two of these figures differ.

        Adding rows aggregates away exactly these columns, so a sum is forbidden only along one
        of them: one segment's row is its own figure, and four segments' rows never add.
        """
        return varying_columns(tuple(figure.dimensions for figure in self.figures), columns)

    def total(self) -> Money | int:
        """Add every figure, keeping the units.

        Raises on an empty measurement rather than returning zero, for the reason
        `Money.total` gives: zero is a claim that nothing sold, and no rows is a claim that
        nothing is known. A caller with no figures should say so instead of totalling them.
        """
        if not self.figures:
            raise InvariantError(
                MEASURE_ABSENT,
                "Nothing to total.",
                context={
                    "problem": "an empty measurement has no total; zero is a different answer"
                },
            )
        if self.units is not None and self.units.kind is UnitKind.CURRENCY:
            code = self.units.currency_code or ""
            return Money.total(tuple(Money(Decimal(f.value), code) for f in self.figures))
        return sum(int(figure.value) for figure in self.figures)


def varying_columns(
    keys: tuple[tuple[tuple[str, str], ...], ...], columns: tuple[str, ...]
) -> tuple[str, ...]:
    """Name the columns, of those given, on which two of these rows' keys differ."""
    return tuple(column for column in columns if len({dict(key).get(column) for key in keys}) > 1)


def total_of(measurement: Measurement, *, additive: bool) -> Decimal | int | None:
    """Add the figures, or refuse when the sum would not be a quantity.

    Three separate reasons there may be no total, and all three are absences rather than zeros:

    - **Nothing to add.** No figures means no total, because zero would claim the population sold
      nothing where the truth is that it held no keys. `Measurement.total` refuses it outright.
    - **The metric does not add.** `reach_users` is a distinct count, so two campaigns' reach is
      not their sum — it overstates by however much their audiences overlapped, and no sketch
      exists to merge them. Resolved from the dictionary and supplied by the caller.
    - **The units are not a quantity.** A ratio, a percentage or a duration does not total.
      Summing ROAS across campaigns produces a number with no denominator and no meaning, and
      `Measurement.total` would silently truncate a `Decimal` to `int` doing it.

    Delegates the arithmetic to `Measurement.total` rather than adding here, so a monetary total
    is checked for a single currency by the one implementation that owns that rule.

    **Shared by the ranking and the breakdown paths, which is why it lives here rather than beside
    either.** It answers "does this measurement have a total at all", which is a property of the
    measurement and not of the operation that asked. The reasons a caller may *still* not state a
    total it could compute — a truncated page, a withheld row — are the caller's, because they are
    facts about the read rather than about the figures.
    """
    units = measurement.units
    if measurement.is_empty or not additive or units is None:
        return None
    if units.kind not in (UnitKind.CURRENCY, UnitKind.COUNT):
        return None
    totalled = measurement.total()
    return totalled.amount if isinstance(totalled, Money) else totalled


def measure(
    plan: QueryPlan, rows: tuple[dict[str, str | int | Decimal | None], ...]
) -> Measurement:
    """Read a query result into figures, refusing anything that cannot be presented honestly.

    An empty result is a legitimate `Measurement` with no figures. That is the one case where
    absence is an answer: the question was asked of real data and the window genuinely
    contains nothing, which ADR-010 makes a valid response rather than an error. Only
    `total()` refuses it, and only because a total of nothing is not zero.
    """
    units = _units_for(plan, rows)
    if plan.units is UnitKind.CURRENCY and units is None:
        return Measurement(
            figures=(),
            units=None,
            grain=plan.grain,
            undefined_rows=len(rows),
        )
    currency = units.currency_code if units is not None else None
    read = tuple(figure_from_row(plan, row, currency) for row in rows)
    figures = tuple(figure for figure in read if figure is not None)
    return Measurement(
        figures=figures,
        units=units if figures else None,
        grain=plan.grain,
        undefined_rows=len(read) - len(figures),
    )


def _units_for(
    plan: QueryPlan, rows: tuple[dict[str, str | int | Decimal | None], ...]
) -> Units | None:
    """Decide what the figures are denominated in, or None when there are none.

    **An empty monetary result has no currency, and inventing one is not available.** The
    currency of an answer comes from the rows, because that is the only place it is stated —
    `currency_code` is a column on the fact, not a property of the query. With no rows there
    is no amount to denominate, and `Units` rightly refuses a currency without a code.

    Returning None rather than guessing is the honest option. The alternatives were to read
    `dim_marketplace.default_currency_code` (a second query, a table that is not built, and
    still an assumption that a marketplace's default is what the rows would have said) or to
    label the empty answer with whatever currency seemed likely, which is precisely the
    invented figure this platform refuses.

    Consequence a caller must handle: `ResponseData.units` is required by the envelope, so a
    tool answering an empty monetary question cannot fill it from here. It has to report "no
    rows" rather than a denominated zero.
    """
    if not rows:
        return None
    if plan.units is not UnitKind.CURRENCY:
        return Units(kind=plan.units)
    code = _one_currency(plan, rows)
    # No row stated one, so there is no amount to denominate either. Same absence as no rows at
    # all, rather than a currency chosen on the caller's behalf.
    return None if code is None else Units(kind=UnitKind.CURRENCY, currency_code=code)


def _combine(plan: QueryPlan, operands: tuple[Decimal | int, ...]) -> Decimal | int | None:
    """Reduce a row's aggregates to the one figure it reports, or None when there is none.

    **A quotient with a zero denominator has no value, and None says so.** Not zero: a campaign
    that spent nothing did not achieve a ROAS of zero, and a product with no page views did not
    convert at 0%. Not infinity either, which is not a number a figure can carry. The row is
    dropped and `Measurement.undefined_rows` counts it, so the omission is stated rather than
    silent — dropping it quietly would make a breakdown that does not add up to its own total.

    Division happens on the aggregates, never on per-row quotients averaged afterwards. The mean
    of ratios is not the ratio of sums, and they diverge most exactly where the denominators are
    least even, which is where somebody is most likely to be looking.
    """
    if plan.combination is Combination.SINGLE:
        return operands[0] * plan.multiplier if plan.multiplier != 1 else operands[0]

    left, right = operands
    if plan.combination is Combination.DIFFERENCE:
        return Decimal(left) - Decimal(right)

    if not right:
        return None
    quotient = (Decimal(left) / Decimal(right)) * plan.multiplier
    # Quantised to the canonical model's own monetary scale, so a figure a tool presents is
    # already rounded and the model is never handed a repeating decimal to shorten itself.
    return quotient.quantize(RATIO_SCALE)


def _side_value(row: dict[str, str | int | Decimal | None], index: int) -> Decimal | None:
    """Read one side of a drill-across, distinguishing "measured nothing" from "withheld".

    A null attributable aggregate with a positive withheld-row count means every source row on
    this side was withheld, so no figure exists. A null with no withheld row comes from the absent
    side of the full join and is the provable zero the wasted-spend case needs. A real zero remains
    a real zero even when other rows in the same group were withheld: the SQL fold preserves null
    exactly when no attributable row exists, so zero no longer has to stand in for that state.
    """
    raw = row.get(measure_alias(index))
    withheld = row.get(withheld_rows_alias(index))
    if raw is None:
        return None if _is_set(withheld) else Decimal(0)
    return Decimal(str(raw))


def _is_set(flag: str | int | Decimal | None) -> bool:
    """Whether any row was withheld, read from the withheld-row count.

    A count rather than a boolean since ADR-035, and this reads "more than none" from it. The count
    is what has to be consulted rather than the withheld *amount*: a suppressed row with no clicks
    contributes zero to the amount, so an amount of zero cannot tell a withholding from an absence.
    """
    if flag is None:
        return False
    try:
        return int(Decimal(str(flag))) != 0
    except InvalidOperation, ValueError:  # pragma: no cover — the composer emits a count
        return False


@dataclass(frozen=True, slots=True)
class Withholding:
    """What a result had withheld, measured rather than asserted.

    Carried as its own value so the adapter that fills `QueryResult.suppression` and the narrative
    that explains it read one measurement. `groups` beside `total_groups` is what makes the
    difference between "one campaign of forty" and "thirty-nine of forty" sayable, which is the
    difference between a usable answer and one to discard.
    """

    amount: Decimal
    withheld_rows: int
    groups: int
    total_groups: int
    without_currency: int = 0
    """Groups whose withheld money came back with no currency, so `amount` leaves it out."""

    @property
    def occurred(self) -> bool:
        """Whether anything was withheld at all."""
        return self.withheld_rows > 0

    @property
    def withheld_amount(self) -> WithheldAmount | None:
        """The typed "withheld" absence for money left out of `amount`, or None when none was."""
        return WithheldAmount(self.without_currency) if self.without_currency else None


def _combine_across(plan: QueryPlan, row: dict[str, str | int | Decimal | None]) -> Decimal | None:
    """Divide one drill-across row, or produce nothing when either side is unknowable.

    Two independent reasons a row yields no figure, and the narrative counts both together because
    both mean the same thing to a reader: this key has no ratio. A zero denominator is undefined
    arithmetic; a withheld side is missing evidence.
    """
    numerator = _side_value(row, 0)
    denominator = _side_value(row, 1)
    if numerator is None or denominator is None or not denominator:
        return None
    return ((numerator / denominator) * plan.multiplier).quantize(RATIO_SCALE)


def figure_from_row(
    plan: QueryPlan, row: dict[str, str | int | Decimal | None], currency: str | None
) -> Figure | None:
    """Read one row into a figure, or raise naming the column that failed.

    Returns None for a row whose quotient is undefined, or whose operand was withheld or not
    returned. See `_combine`. Per row, so a held answer (`measure`) and a fold
    (`answers.fold_answer`) read a row into a figure by one rule.
    """
    dimensions: list[tuple[str, str]] = []
    for column in plan.group_by:
        if column not in row or row[column] is None:
            raise InvariantError(
                MEASURE_ABSENT,
                "A result row is missing a dimension the answer groups by.",
                context={"column": column, "fact": plan.fact, "metric": plan.metric.name},
            )
        dimensions.append((column, str(row[column])))

    if plan.join is not None:
        combined = _combine_across(plan, row)
        if combined is None:
            return None
        return Figure(dimensions=tuple(dimensions), value=combined)

    operands: list[Decimal | int] = []
    for index in range(len(plan.measures)):
        # Keyed by the alias the query gave it, not by the contract column it came from. The two
        # differ and used to be conflated — see `MEASURE_ALIAS`.
        raw = row.get(measure_alias(index))
        if raw is None:
            if _is_set(row.get(withheld_rows_alias(index))):
                # The SQL fold returns NULL only when no attributable row exists. A positive
                # withheld count therefore makes this operand unknowable rather than zero.
                return None
            if (
                plan.measures[index] in plan.not_returned_columns
                or plan.measures[index] in plan.partially_written_columns
            ):
                # Not returned: a shopper-aggregate cell that didn't come back (ADR-099 point 8),
                # too few shoppers or none, declared absent by its contract.
                # Partially written: no contributing row in this group carried the column, because
                # the contract that writes it wrote nothing here. An absence of evidence, so the
                # row yields no figure and is counted as one that could not be produced — the same
                # treatment a withheld side gets, and for the same reason. Still not zero in
                # either case: a day with no viewability measurement did not have zero viewable
                # impressions (ADR-037), and a cell AMC didn't return had no zero buyers.
                return None
            # Not coerced to zero, deliberately. A measure every writer of this fact supplies is
            # non-nullable in its contract, so a null is the model and the data disagreeing — and
            # a zero would report a product that sold as having sold nothing.
            raise InvariantError(
                MEASURE_ABSENT,
                "A result row carries no value for the measure.",
                context={
                    "column": plan.measures[index],
                    "fact": plan.fact,
                    "metric": plan.metric.name,
                },
            )
        operands.append(_quantity(plan, raw))

    combined = _combine(plan, tuple(operands))
    if combined is None:
        return None
    value = combined
    if currency is not None:
        # Validate the amount against `Money`'s rules here, once, rather than at total time.
        # `Money` raises `ValueError` because it is a value object; an inbound adapter would
        # map that to an unclassified failure and lose the reason. Translating it now also
        # means `total()` cannot raise, and the error can name the row that was wrong.
        try:
            Money(Decimal(value), currency)
        except ValueError:
            raise InvariantError(
                MEASURE_NOT_A_QUANTITY,
                "A monetary value came back in a form this model cannot store.",
                context={
                    "column": plan.measure_column,
                    "fact": plan.fact,
                    "dimensions": dict(dimensions),
                    "cause": "invalid_money",
                },
            ) from None
    return Figure(dimensions=tuple(dimensions), value=value)


def _quantity(plan: QueryPlan, raw: str | int | Decimal) -> Decimal | int:
    """Coerce a returned value to an exact quantity, never through a float.

    A string is what a query engine returns for a decimal over some transports, so it has to
    be accepted — but through `Decimal(str)`, because `float("11.37")` is not 11.37 and the
    error survives every later sum.
    """
    if plan.units is UnitKind.COUNT:
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise InvariantError(
                MEASURE_NOT_A_QUANTITY,
                "A count came back as something that is not a whole number.",
                context={"column": plan.measure_column, "cause": type(exc).__name__},
            ) from None

    try:
        return Decimal(str(raw))
    except InvalidOperation:
        raise InvariantError(
            MEASURE_NOT_A_QUANTITY,
            "A measure came back as something that is not a number.",
            context={"column": plan.measure_column, "fact": plan.fact},
        ) from None


def _one_currency(
    plan: QueryPlan, rows: tuple[dict[str, str | int | Decimal | None], ...]
) -> str | None:
    """Find the single currency every monetary row shares, or refuse the result.

    Refusing rather than converting, for the reason `Money` refuses to add across currencies:
    the only rate available here is today's, and ADR-004 requires the rate as of the fact's
    own date. Refusing rather than picking the first, because a figure labelled `USD` that
    silently includes euros is exactly the plausible wrong number this platform exists to
    avoid.

    Reaching the mixed-currency refusal at all means `marketplace_key` was pinned or grouped and
    currency still varied, which is a real finding about the data rather than a caller mistake — so
    the error names the currencies it saw.

    **A row with no currency is skipped when the column is partially written, and refused
    otherwise.** In a fact several contracts write, `currency_code` arrives only from those that
    carry money, so a group assembled from a clicks-only delivery states no currency and has no
    amount to denominate either — it will yield no figure. Refusing there would discard every
    other group over a row that was never going to be reported. ADR-037.
    """
    accumulator = CurrencyAccumulator(plan)
    for row in rows:
        accumulator.add(row)
    return accumulator.result()


class CurrencyAccumulator:
    """`_one_currency`, one row at a time: a held answer and a fold share one currency rule.

    `add` raises the same `InvariantError` the whole-result check raises for a monetary row that
    owes a currency and states none; `result` refuses mixed currencies (`MIXED_CURRENCY`).
    """

    def __init__(self, plan: QueryPlan) -> None:
        self._plan = plan
        self._absence_is_expected = CURRENCY_COLUMN in plan.partially_written_columns
        self._seen: list[str] = []

    def add(self, row: dict[str, str | int | Decimal | None]) -> str | None:
        """Read one row's currency, if it owes one; return it (None when the row has none)."""
        plan = self._plan
        value = row.get(CURRENCY_COLUMN)
        if value is None and _withheld_only(plan, row):
            # Every row in this group was withheld and AMC returned no currency with them, so
            # the group has no figure and its amount isn't money anyone can add (AMC-V decision
            # 5). It reads "withheld" through `withholding_in`; the other groups still answer.
            return None
        if value is None and _no_amount_to_denominate(plan, row):
            return None
        if value is None:
            drill_currency_sides: tuple[int, ...] = ()
            if plan.join is not None:
                drill_currency_sides = tuple(
                    index
                    for index, carried in enumerate(
                        (plan.primary_carried_columns, plan.join.carried_columns)
                    )
                    if CURRENCY_COLUMN in carried
                )
            if self._absence_is_expected or (
                drill_currency_sides
                and all(row.get(measure_alias(index)) is None for index in drill_currency_sides)
            ):
                return None
            raise InvariantError(
                MEASURE_ABSENT,
                "A monetary result row does not say what currency it is in.",
                context={
                    "column": CURRENCY_COLUMN,
                    "fact": plan.fact,
                    "problem": "the plan must carry currency_code for a monetary metric",
                },
            )
        code = str(value)
        if code not in self._seen:
            self._seen.append(code)
        return code

    def result(self) -> str | None:
        """Return the one currency every row stated, or None when no row stated one.

        Every row silent means nothing to denominate and no figure will survive: a row with no
        currency also had no amount. Reported as an absence of units rather than an error. More
        than one is refused, naming every currency seen.
        """
        if len(self._seen) > 1:
            raise InvariantError(
                MIXED_CURRENCY,
                "This result spans more than one currency, so no single total is correct.",
                context={
                    "currencies": sorted(self._seen),
                    "fact": self._plan.fact,
                    "grain": str(self._plan.grain),
                },
            )
        return self._seen[0] if self._seen else None


def withholding_in(
    plan: QueryPlan, rows: tuple[dict[str, str | int | Decimal | None], ...]
) -> Withholding:
    """Total what a result had withheld, across every group and every measure.

    **The measurement `Suppression` is built from**, and the reason it can be built at all: the
    curated fact no longer carries a reason column (G-28), so an answer has to say what it measured
    rather than repeat what one delivery said. See
    [ADR-035](../../../docs/decisions/adr-035-suppression-is-a-quantity-not-a-flag.md).

    Only the first measure's amount is totalled, deliberately. A ratio's two operands are different
    quantities in different units — withheld spend and withheld revenue do not add — so summing
    them would produce a number with no unit at all. The row count is totalled across every
    measure, because "some rows were withheld" is true regardless of which operand lost them.

    For the same reason a money amount whose row states its currency as NULL is left out of
    `amount` and counted in `without_currency`, which the answer reads as the typed "withheld"
    absence (AMC-V decision 5, `WithheldAmount`).
    """
    accumulator = WithholdingAccumulator(plan)
    for row in rows:
        accumulator.add(row)
    return accumulator.result()


class WithholdingAccumulator:
    """`withholding_in`, one row at a time: what a held and a streamed read both measure.

    A streamed read never holds its rows, so the withholding it reports is accumulated as each
    row arrives; `withholding_in` is this accumulator over a tuple, so the two can't diverge.
    """

    def __init__(self, plan: QueryPlan) -> None:
        self._plan = plan
        self._money = CURRENCY_COLUMN in plan.carried_columns
        self._amount = Decimal(0)
        self._withheld_rows = 0
        self._groups = 0
        self._without_currency = 0
        self._total_groups = 0

    def add(self, row: dict[str, str | int | Decimal | None]) -> None:
        """Measure one result row's withholding."""
        self._total_groups += 1
        touched = False
        for index in range(_operand_count(self._plan)):
            count = row.get(withheld_rows_alias(index))
            if not _is_set(count):
                continue
            touched = True
            self._withheld_rows += int(Decimal(str(count)))
            if index == 0:
                raw = row.get(withheld_alias(index))
                if raw is None:
                    continue
                if (
                    self._money
                    and CURRENCY_COLUMN in row
                    and not withheld_money_counts(row[CURRENCY_COLUMN])
                ):
                    # The row states its currency as NULL, so its withheld amount is never
                    # summed into money: it is typed "withheld" instead (AMC-V decision 5). The
                    # row count still counts.
                    self._without_currency += 1
                else:
                    self._amount += Decimal(str(raw))
        if touched:
            self._groups += 1

    def result(self) -> Withholding:
        """Return what every row added so far had withheld."""
        return Withholding(
            amount=self._amount,
            withheld_rows=self._withheld_rows,
            groups=self._groups,
            total_groups=self._total_groups,
            without_currency=self._without_currency,
        )


def _no_amount_to_denominate(plan: QueryPlan, row: dict[str, str | int | Decimal | None]) -> bool:
    """Whether a shopper-aggregate row with no currency also carries no money to label.

    Only for measures their contract declares nullable `not_returned` (ADR-099 point 8): a cell
    that didn't come back has a NULL amount, and a cell no purchase or no DSP touch reached sums
    to NULL or 0 with its currency NULL (pack 5's revenue on a cell with no buyer; packs 5-7's
    DSP spend on sponsored-only shoppers). There is nothing to denominate, so no currency is
    owed; a non-zero amount without its currency is still the model and the data disagreeing.
    """
    if plan.join is not None or not set(plan.measures) <= set(plan.not_returned_columns):
        return False
    return all(row.get(measure_alias(index)) in (None, 0) for index in range(len(plan.measures)))


def _withheld_only(plan: QueryPlan, row: dict[str, str | int | Decimal | None]) -> bool:
    """Whether a result row has no attributable operand and a withheld row behind every one."""
    operands = range(_operand_count(plan))
    return all(
        row.get(measure_alias(index)) is None and _is_set(row.get(withheld_rows_alias(index)))
        for index in operands
    )


def _operand_count(plan: QueryPlan) -> int:
    """How many aggregate operands a plan's rows carry."""
    return 2 if plan.join is not None else len(plan.measures)
