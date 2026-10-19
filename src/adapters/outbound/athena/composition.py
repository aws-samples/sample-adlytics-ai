# SPDX-License-Identifier: MIT-0
"""Compose one Athena statement from a validated plan.

Separated from execution deliberately: this half is a string function with no SDK, no
network and no clock, so the SQL a plan produces is assertable character by character in the
unit tier. The half that talks to Athena is `query.py`, and it is covered in the integration
tier because that is the only place a real engine can reject what we wrote.

**The rule this module exists to keep: interpolate identifiers, parameterise values.**

SQL cannot bind an identifier — there is no placeholder for a table or column name — so every
identifier here is formatted into the statement text. That is safe only because a `QueryPlan`
has already checked each one, and it goes further than checking: `plan()` rebuilds the filter
mapping from the fact's own column tuple, so the strings arriving here provably originate in a
data contract rather than in a caller's request. See ADR-024 and `ports/outbound/query.py`.

Values are the opposite. Filter values and window bounds are caller-supplied data and never
appear in the statement: they become `?` placeholders and travel as `ExecutionParameters`.

"Parameterise", not "bind", and the distinction is load-bearing. [verified 2026-08-19] Athena
**parses** an `ExecutionParameters` entry as a SQL expression rather than binding it as an
opaque typed value, so an unquoted entry is evaluated — `2026-08-10` arrives as the integer
2008. `execution.start` therefore encodes every value as a quoted literal, which is what makes
a `?` here reliably a varchar. What the parameter mechanism does still give us is that a value
cannot restructure the statement around it, because it is substituted as one expression node.
`parameters.py` records what was measured.

Column and table names are additionally quoted with double quotes, which is not belt-and-braces
over the validation — it is what stops a legitimate canonical name from colliding with a
reserved word. `date_key` is fine today; a future `order` or `end` would not be.
"""

from __future__ import annotations

from dataclasses import dataclass

from domain.measurement.periodic import PERIOD_END_COLUMN, PERIOD_START_COLUMN
from domain.measurement.planning import (
    CURRENCY_COLUMN,
    DATE_COLUMN,
    JoinSide,
    QueryPlan,
    measure_alias,
    withheld_alias,
    withheld_rows_alias,
)
from domain.measurement.publications import BoundQueryPlan

__all__ = ["ComposedQuery", "compose", "compose_series"]


@dataclass(frozen=True, slots=True)
class ComposedQuery:
    """A statement and the values to bind into it, in order.

    Kept together because they are meaningless apart: the nth `?` in `sql` is the nth entry
    in `parameters`, and any code path that could pair one statement with another's
    parameters would bind the wrong values without failing.
    """

    sql: str
    parameters: tuple[str, ...]


_BOUND_DATE = "CAST(? AS DATE)"
"""How a window bound is written once the parameter is a varchar.

**This was `CAST(CAST(? AS VARCHAR) AS DATE)` and the double cast was cargo cult.** The earlier
reading — "Athena infers an unhinted placeholder as `integer`" — described a real symptom and
got the cause wrong. Athena does not infer a *type* for a parameter at all: it parses the
parameter's text as a SQL expression, and `2026-08-10` parses as the arithmetic 2026 - 8 - 10.
The messages that reading was built on

    CAST(? AS DATE)         TYPE_MISMATCH: Cannot cast integer to date
    date(?)                 FUNCTION_NOT_FOUND: Unexpected parameters (integer)

were all the same integer 2008 arriving under different casts, and the `VARCHAR` cast appeared
to fix it because it made the *cast* legal while leaving the value 2008. It was never right; it
only stopped failing. The empty tables hid that, because a cast over zero rows is never
evaluated — the first seeded row failed it with
`INVALID_CAST_ARGUMENT: Value cannot be cast to date: 2008`.

The fix belongs at the parameter, not in the SQL: `execution.start` sends every value as a
quoted literal, so `?` is a varchar here and one cast is both sufficient and honest. See
`parameters.py` for the measurements.

Only the *parameter* is cast, never the column. `date_key` stays bare because casting a
partition key would stop the predicate pruning partitions and turn every windowed question into
a full scan.

**The predicate does reach file selection.** [verified 2026-08-19] against the seeded table:

    window 2026-08-10 .. 2026-08-10     942 bytes scanned
    window 2026-01-01 .. 2026-12-31     942 bytes scanned
    window 2020-01-01 .. 2020-01-07       0 bytes scanned

Zero for an excluded range is the finding. Nothing was read at all, so the cast is folded and
the bound compared *before* any data file is opened — which is what the earlier note set out to
check, and it settles the concern that a cast around the parameter might defeat pushdown.

The first two figures are equal because every seeded row shares one date and therefore one data
file: widening the window adds no file to read. Whether the skipping is Iceberg partition
pruning or its file-level column statistics is [unverified] and not distinguishable with one
file. It matters for a large table, so re-measure once more than one day of data is loaded.
"""


def _quoted(identifier: str) -> str:
    """Quote a canonical identifier for Athena.

    No escaping of embedded quotes, and that is not an oversight: a `QueryPlan` only carries
    identifiers taken from a data contract's column list, and the contract gate constrains
    those to lowercase, digits and underscores. An embedded quote here would mean the
    validation upstream had already failed, so silently escaping it would hide that.
    """
    return f'"{identifier}"'


def compose(request: BoundQueryPlan, max_rows: int) -> ComposedQuery:
    """Build a publication-fenced aggregate query from one bound logical plan.

    Shape, and every part of it comes from the plan rather than from a choice made here:

        SELECT   <group_by>, <carried>, SUM(<measure_0>) AS value_0 [, SUM(<measure_1>) AS value_1]
        FROM     <database is set on the request, not here>.<fact>
        WHERE    <date_key> BETWEEN ? AND ?  AND  <each filter> = ?
        GROUP BY <group_by>, <carried>
        ORDER BY <group_by>
        LIMIT    <max_rows + 1>

    `ORDER BY` exists so a truncated result is deterministic. Without it, "the first 100
    rows" is whichever 100 the engine happened to produce, so the same question could return
    a different hundred each time and nothing would look wrong.

    `LIMIT max_rows + 1` is how truncation is detected. Asking for exactly `max_rows` cannot
    distinguish "there were exactly that many" from "there were more and you got a page",
    and the difference is the whole point of the `truncated` flag — a partial answer
    presented as complete is the failure this platform exists to avoid. The extra row is
    discarded by the caller.

    No `dim_product` join. The target question groups by `amazon_asin`, which the fact
    carries, so joining the dimension would add nothing but the SCD2 fan-out ADR-023 warns
    about. A question needing a product *attribute* — brand, parent ASIN — needs the join and
    a validity predicate with it, and that is a different plan than any built today.
    """
    request_plan = request.logical
    if request_plan.join is not None:
        return _compose_drill_across(request, max_rows)

    projection = _projection(request_plan)
    columns, grouped, aggregates = projection.columns, projection.grouped, projection.aggregates

    predicates, parameters = _time_predicates(request_plan, periodic=request_plan.periodic)
    publication_parameters = ", ".join("?" for _ in request.primary_ids)
    predicates.append(f"{_quoted('publication_id')} IN ({publication_parameters})")
    parameters.extend(request.primary_ids)
    _filter_predicates(request, projection.discriminator, (predicates, parameters))

    # S608 is correct that this builds a statement by interpolation, and the interpolation is
    # the point: SQL has no placeholder for an identifier. What makes it safe is upstream and
    # not visible here, so it is worth restating rather than waving at — every interpolated
    # name is either a literal in this file, `max_rows` (an int bounded by the port), or a
    # string `plan()` took from a data contract's own column tuple. Every caller-supplied
    # *value* is a `?`. If that upstream validation is ever weakened, this line becomes a
    # genuine injection vector, which is why ADR-024 and the port docstring both say so.
    sql = (
        # A-SQL-001: quoted identifiers, values bind as ?; see docs/security-dispositions.md
        f"SELECT {columns}, {aggregates} "  # noqa: S608 — see the note above  # nosec B608
        f"FROM {_quoted(request_plan.fact)} "
        f"WHERE {' AND '.join(predicates)} "
        f"GROUP BY {grouped} "
        f"ORDER BY {', '.join(_quoted(name) for name in request_plan.group_by)} "
        f"LIMIT {max_rows + 1}"
    )
    return ComposedQuery(sql=sql, parameters=tuple(parameters))


def compose_series(plans: tuple[BoundQueryPlan, ...], max_rows: int) -> ComposedQuery:
    """Build one publication-fenced statement reading the same plan over several exact periods.

        SELECT   period_start, period_end, <group_by>, <carried>, <aggregates>
        FROM     <fact>
        WHERE    ((period_start = ? AND period_end = ? AND publication_id IN (…)) OR …)
                 AND <each filter> = ?
        GROUP BY period_start, period_end, <group_by>, <carried>
        ORDER BY period_start DESC, period_end DESC, <group_by>
        LIMIT    <max_rows + 1>

    The plans are one validated read repeated per period (`PeriodSeriesRequest`), so the
    projection, aggregates and filters are the first plan's, exactly as `compose` writes them.
    **Each period is fenced to its own publications**, so a row is read only from a publication
    its period was bound to, and grouping by the period keeps every period's figures apart: no
    measure is summed across periods. Newest first, so a truncated result keeps the newest periods.
    """
    first = plans[0].logical
    projection = _projection(first)
    period_columns = f"{_quoted(PERIOD_START_COLUMN)}, {_quoted(PERIOD_END_COLUMN)}"
    alternatives: list[str] = []
    parameters: list[str] = []
    for bound in plans:
        # The period fence `compose` writes for one periodic read, so the two never diverge.
        fence, bounds = _time_predicates(bound.logical, periodic=True)
        ids = ", ".join("?" for _ in bound.primary_ids)
        fence.append(f"{_quoted('publication_id')} IN ({ids})")
        alternatives.append(f"({' AND '.join(fence)})")
        parameters.extend(bounds)
        parameters.extend(bound.primary_ids)
    predicates = [f"({' OR '.join(alternatives)})"]
    _filter_predicates(plans[0], projection.discriminator, (predicates, parameters))
    selected = ", ".join(item for item in (period_columns, projection.columns) if item)
    grouped = ", ".join(item for item in (period_columns, projection.grouped) if item)
    ordering = ", ".join(
        (
            f"{_quoted(PERIOD_START_COLUMN)} DESC",
            f"{_quoted(PERIOD_END_COLUMN)} DESC",
            *(_quoted(name) for name in first.group_by),
        )
    )
    sql = (
        # A-SQL-001: quoted identifiers, values bind as ?; see docs/security-dispositions.md
        f"SELECT {selected}, {projection.aggregates} "  # noqa: S608 — see `compose`  # nosec B608
        f"FROM {_quoted(first.fact)} "
        f"WHERE {' AND '.join(predicates)} "
        f"GROUP BY {grouped} "
        f"ORDER BY {ordering} "
        f"LIMIT {max_rows + 1}"
    )
    return ComposedQuery(sql=sql, parameters=tuple(parameters))


@dataclass(frozen=True, slots=True)
class _Projection:
    """What one single-fact plan selects, groups by and aggregates."""

    columns: str
    grouped: str
    aggregates: str
    discriminator: str


def _projection(plan: QueryPlan) -> _Projection:
    """Write one single-fact plan's selected columns, grouping and aggregates."""
    selected = [*plan.group_by, *plan.carried_columns]
    columns = ", ".join(_quoted(name) for name in selected)
    grouped = columns
    if plan.currency_source != CURRENCY_COLUMN:
        # A money measure whose contract names its own currency column (pack 5's window
        # spends): that column is selected and grouped under the one name an answer reads.
        source = _quoted(plan.currency_source)
        columns = ", ".join(
            f"{source} AS {_quoted(name)}" if name == CURRENCY_COLUMN else _quoted(name)
            for name in selected
        )
        grouped = ", ".join(
            source if name == CURRENCY_COLUMN else _quoted(name) for name in selected
        )
    # One aggregate per declared measure, each under its own numbered alias. A plain measure has
    # one; a difference of sums and a same-fact ratio each have two, aggregated here and combined
    # in `domain/measurement/figures.py` — the combination is arithmetic on figures, which is
    # measurement semantics rather than something to express in SQL and hand back pre-mixed.
    # Every measure folds over the discriminator when the fact can withhold, so a group reports
    # what it can attribute *and* how much was withheld — ADR-035. A fact with no threshold
    # mechanism gets a plain sum, because nothing could have been withheld and selecting a zero
    # would be an expression with nothing behind it.
    discriminator = _discriminator(plan.suppression_columns)
    aggregates = ", ".join(
        _folded(column, index, discriminator) for index, column in enumerate(plan.measures)
    )
    return _Projection(columns, grouped, aggregates, discriminator)


def _filter_predicates(
    bound: BoundQueryPlan, discriminator: str, into: tuple[list[str], list[str]]
) -> None:
    """Append one bound plan's execution filters as predicates and their bound values."""
    predicates, parameters = into
    # Sorted so the statement is deterministic for a given plan. `filters` is already ordered
    # by the fact's column tuple, but sorting states the guarantee rather than inheriting it —
    # and a stable statement is what makes Athena's result reuse able to hit at all.
    execution_filters = dict(bound.primary_filters)
    for column in sorted(execution_filters):
        if column == discriminator:
            # Expressed in the aggregates above. Filtering it here is what destroyed the
            # distinction between withheld and absent before ADR-035.
            continue
        predicates.append(_equality(column, bound.logical.typed_columns))
        parameters.append(str(execution_filters[column]))


_CAST_TYPES = {
    "boolean": "BOOLEAN",
    "int": "INTEGER",
    "bigint": "BIGINT",
    "double": "DOUBLE",
    "decimal(38,4)": "DECIMAL(38,4)",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
}
"""Each canonical non-string type (`CanonicalType`) and the type its bound value casts to."""


def _equality(column: str, typed_columns: tuple[tuple[str, str], ...]) -> str:
    """Write one filter predicate; a non-string column compares to its value cast to its type.

    Every value is bound as a quoted varchar (`execution.start`), so `"as_of_month" = ?` would
    compare a date to a varchar, and `"months_since_acquisition" = ?` a bigint to one, which
    Athena refuses (TYPE_MISMATCH). A date casts as the window bounds already do (`_BOUND_DATE`).
    A type with no declared cast is refused rather than bound as a varchar.
    """
    declared = dict(typed_columns).get(column)
    if declared is None:
        return f"{_quoted(column)} = ?"
    cast_to = _CAST_TYPES.get(declared)
    if cast_to is None:
        raise ValueError(f"{column}: no filter cast is declared for type {declared}.")
    return f"{_quoted(column)} = CAST(? AS {cast_to})"


def _time_predicates(plan: QueryPlan, *, periodic: bool) -> tuple[list[str], list[str]]:
    """Bound one side to the plan's window: a run of days, or one exact measured period.

    A periodic fact holds one row set per measured period, so its side selects
    `period_start = start AND period_end = end` exactly (ADR-100, D-1). A daily side over the
    same plan sums the period's days with the usual `BETWEEN` (D-6).
    """
    start, end = plan.window.start.isoformat(), plan.window.end.isoformat()
    if periodic:
        return (
            [
                f"{_quoted(PERIOD_START_COLUMN)} = {_BOUND_DATE}",
                f"{_quoted(PERIOD_END_COLUMN)} = {_BOUND_DATE}",
            ],
            [start, end],
        )
    return ([f"{_quoted(DATE_COLUMN)} BETWEEN {_BOUND_DATE} AND {_BOUND_DATE}"], [start, end])


def _discriminator(suppression_columns: tuple[str, ...]) -> str:
    """Name the column saying whether a row's values were removed, or nothing.

    Element zero by the order `FactShape` builds the tuple in. Empty for a vendor fact, which has
    no threshold mechanism at all — and an empty string is safe to compare a column name against.
    """
    return suppression_columns[0] if suppression_columns else ""


def _folded(measure: str, index: int, discriminator: str) -> str:
    """Aggregate one measure, splitting it over the discriminator when there is one.

    Three expressions rather than one, and each answers a different question — see
    `withheld_alias` and `withheld_rows_alias` for why the count cannot be inferred from the
    amount. A fact that cannot withhold gets one expression, because it has nothing to split on.
    """
    column = _quoted(measure)
    if not discriminator:
        return f"SUM({column}) AS {measure_alias(index)}"
    flag = _quoted(discriminator)
    return (
        f"SUM(CASE WHEN NOT {flag} THEN {column} END) AS {measure_alias(index)}, "
        f"SUM(CASE WHEN {flag} THEN {column} ELSE 0 END) AS {withheld_alias(index)}, "
        f"SUM(CASE WHEN {flag} THEN 1 ELSE 0 END) AS {withheld_rows_alias(index)}"
    )


@dataclass(frozen=True, slots=True)
class _Side:
    """One aggregated side of a drill-across, ready to be joined."""

    sql: str
    parameters: tuple[str, ...]
    alias: str
    carried_columns: tuple[str, ...] = ()


def _compose_side(bound: BoundQueryPlan, side: tuple[int, str, str]) -> _Side:
    """Aggregate one publication-fenced fact side, preserving withholding evidence.

    **The suppression filter is moved out of the `WHERE` and into the aggregates, deliberately.**
    A drill-across joins two independently grouped sides, so a key can appear on one and not the
    other — and the whole correctness of the join rests on telling "this fact measured nothing"
    apart from "Amazon withheld it". Filtering suppressed rows away in the `WHERE` destroys that
    distinction before the join can see it: the group vanishes entirely and looks identical to a
    genuine absence.

    So the measure sums only unsuppressed rows, which is exactly what the caller's
    `rows_suppressed` filter asked for, and a second aggregate reports whether any suppressed row
    existed for the key. A fact with no threshold mechanism gets the literal `0`, because nothing
    could have been withheld — a provable absence rather than an assumption.
    """
    plan = bound.logical
    index, fact, measure = side
    suppression = plan.suppression_columns if index == 0 else _join_of(plan).suppression_columns
    typed_columns = plan.typed_columns if index == 0 else _join_of(plan).typed_columns
    discriminator = _discriminator(suppression)
    filters = dict(bound.primary_filters if index == 0 else bound.joined_filters)

    side_carried = plan.primary_carried_columns if index == 0 else _join_of(plan).carried_columns
    grouped_names = (*plan.group_by, *side_carried)
    grouped = ", ".join(_quoted(name) for name in grouped_names)
    # A side that cannot withhold still selects the pair, as literal zeroes, so both sides of the
    # join carry the same shape and `result_aliases` describes one row rather than two.
    aggregates = (
        _folded(measure, index, discriminator)
        if discriminator
        else (
            f"SUM({_quoted(measure)}) AS {measure_alias(index)}, "
            f"0 AS {withheld_alias(index)}, 0 AS {withheld_rows_alias(index)}"
        )
    )

    predicates, parameters = _time_predicates(
        plan, periodic=plan.periodic if index == 0 else _join_of(plan).periodic
    )
    publication_ids = bound.primary_ids if index == 0 else bound.joined_ids
    publication_parameters = ", ".join("?" for _ in publication_ids)
    predicates.append(f"{_quoted('publication_id')} IN ({publication_parameters})")
    parameters.extend(publication_ids)
    for column in sorted(filters):
        # The discriminator is expressed in the aggregates above rather than here. Every other
        # filter is an ordinary predicate.
        if column == discriminator:
            continue
        predicates.append(_equality(column, typed_columns))
        parameters.append(str(filters[column]))

    sql = (
        # A-SQL-001: quoted identifiers, values bind as ?; see docs/security-dispositions.md
        f"SELECT {grouped}, {aggregates} "  # noqa: S608 — see `compose`  # nosec B608
        f"FROM {_quoted(fact)} "
        f"WHERE {' AND '.join(predicates)} "
        f"GROUP BY {grouped}"
    )
    return _Side(
        sql=sql,
        parameters=tuple(parameters),
        alias=f"s{index}",
        carried_columns=side_carried,
    )


def _join_of(plan: QueryPlan) -> JoinSide:
    """Read the plan's join side, which a drill-across always has."""
    join = plan.join
    if join is None:  # pragma: no cover — only reached through the drill-across branch
        raise ValueError(f"{plan.metric.name}: expected a join side.")
    return join


def _compose_drill_across(bound: BoundQueryPlan, max_rows: int) -> ComposedQuery:
    """Build the publication-fenced two-subquery join a cross-fact ratio needs.

        SELECT   COALESCE(s0.<dim>, s1.<dim>) AS <dim>, s0.value_0, s0.suppressed_0,
                 s1.value_1, s1.suppressed_1
        FROM     (<numerator aggregated>) s0
        FULL JOIN (<denominator aggregated>) s1 ON s0.<dim> = s1.<dim> AND …
        ORDER BY 1 … LIMIT <max_rows + 1>

    **`FULL JOIN`, not inner.** An inner join silently drops a campaign that spent without
    converting, which is the row somebody asking about waste most needs to see. Keeping it and
    deciding what its absent side means is `domain/measurement/figures.py`'s job — the query's
    job is to not throw the evidence away.

    **`COALESCE` on every joined dimension**, because a full join leaves the key null on whichever
    side is missing, and a row whose grain column came back null would be refused downstream as a
    result that does not state where it sits.

    No aggregate is combined here. The division happens on figures, where a zero denominator can
    produce no figure rather than an error, and where the suppression flags decide whether an
    absent side is a zero or an unknown.
    """
    plan = bound.logical
    join = _join_of(plan)
    numerator = _compose_side(bound, (0, plan.fact, plan.measures[0]))
    denominator = _compose_side(bound, (1, join.fact, join.measure))

    keys = ", ".join(
        f"COALESCE({numerator.alias}.{_quoted(name)}, {denominator.alias}.{_quoted(name)}) "
        f"AS {_quoted(name)}"
        for name in plan.group_by
    )
    carried_columns: list[str] = []
    for name in plan.carried_columns:
        sources = tuple(side for side in (numerator, denominator) if name in side.carried_columns)
        if not sources:  # pragma: no cover — QueryPlan validates the side/output union
            raise ValueError(f"{plan.metric.name}: no drill side carries {name}.")
        expression = (
            f"COALESCE({sources[0].alias}.{_quoted(name)}, {sources[1].alias}.{_quoted(name)})"
            if len(sources) == 2  # noqa: PLR2004 — a drill-across has exactly two sides
            else f"{sources[0].alias}.{_quoted(name)}"
        )
        carried_columns.append(f"{expression} AS {_quoted(name)}")
    aggregates = ", ".join(
        f"{side.alias}.{measure_alias(index)}, {side.alias}.{withheld_alias(index)}, "
        f"{side.alias}.{withheld_rows_alias(index)}"
        for index, side in enumerate((numerator, denominator))
    )
    shared_carried = tuple(
        name
        for name in plan.carried_columns
        if name in numerator.carried_columns and name in denominator.carried_columns
    )
    join_columns = (*plan.group_by, *shared_carried)
    on = " AND ".join(
        f"{numerator.alias}.{_quoted(name)} = {denominator.alias}.{_quoted(name)}"
        for name in join_columns
    )

    # Ordered by position rather than by name, because the selected key is a COALESCE expression
    # and repeating it in the ORDER BY would state the same thing twice.
    ordering = ", ".join(str(position) for position in range(1, len(plan.group_by) + 1))

    selected = ", ".join((keys, *carried_columns, aggregates))
    sql = (
        # A-SQL-001: quoted identifiers, values bind as ?; see docs/security-dispositions.md
        f"SELECT {selected} "  # noqa: S608 — see `compose`  # nosec B608
        f"FROM ({numerator.sql}) {numerator.alias} "
        f"FULL JOIN ({denominator.sql}) {denominator.alias} ON {on} "
        f"ORDER BY {ordering} "
        f"LIMIT {max_rows + 1}"
    )
    return ComposedQuery(sql=sql, parameters=(*numerator.parameters, *denominator.parameters))
