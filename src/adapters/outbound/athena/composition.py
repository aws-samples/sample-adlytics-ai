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
from domain.measurement.ranking import (
    RANK_ORDER,
    RankedMembership,
    RankedProjection,
    RankedRead,
)

__all__ = [
    "POPULATION_COUNT",
    "POPULATION_SUPPRESSED",
    "RANKED_CTE",
    "ComposedQuery",
    "compose",
    "compose_series",
    "population_columns",
    "population_sum_alias",
]

RANKED_CTE = "ranked"
"""The one CTE a ranked statement declares: the restriction's top `n` keys and rank values."""


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


def compose(
    bound: BoundQueryPlan, max_rows: int, ranked: RankedRead | None = None
) -> ComposedQuery:
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

    With `ranked` (ADR-143), the statement opens with the `ranked` CTE (`_ranked_cte`) and either
    selects the ranked keys themselves or semi-joins its own read to them.
    """
    request_plan = bound.logical
    if ranked is not None:
        return _compose_ranked(bound, max_rows, ranked)
    if request_plan.join is not None:
        return _compose_drill_across(bound, max_rows)
    return _compose_single(bound, max_rows)


def _compose_single(
    request: BoundQueryPlan, max_rows: int, leading: tuple[str, ...] = ()
) -> ComposedQuery:
    """Build one single-fact read; `leading` predicates (no parameters) open its `WHERE`."""
    request_plan = request.logical
    projection = _projection(request_plan)
    columns, grouped, aggregates = projection.columns, projection.grouped, projection.aggregates

    predicates, parameters = _time_predicates(request_plan, periodic=request_plan.periodic)
    predicates[:0] = leading
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


def _rank_order() -> str:
    """Render the rank rule's one order list (`RANK_ORDER`) as SQL."""
    return ", ".join(f"{_quoted(field.value)} {direction.value}" for field, direction in RANK_ORDER)


def population_sum_alias(index: int) -> str:
    """Name the nth declared population sum a ranked-keys row carries (ADR-152)."""
    return f"population_sum_{index}"


POPULATION_COUNT = "population_count"
"""The ranked-keys column holding the partition's population count."""
POPULATION_SUPPRESSED = "population_suppressed"
"""The ranked-keys column saying whether any of the partition's population rows was withheld."""
_SUPPRESSED = "suppressed"
_POSITION = "rank_position"


def population_columns(ranked: RankedRead) -> tuple[str, ...]:
    """Every population column a ranked-keys row carries after the plan's own aggregates."""
    return (
        POPULATION_COUNT,
        POPULATION_SUPPRESSED,
        *(population_sum_alias(index) for index in range(len(ranked.restriction.population_sums))),
    )


def _fenced_read(bound: BoundQueryPlan) -> tuple[list[str], list[str]]:
    """Write one bound single-fact read's fence: its window, publications and filters."""
    plan = bound.logical
    predicates, parameters = _time_predicates(plan, periodic=plan.periodic)
    ids = ", ".join("?" for _ in bound.primary_ids)
    predicates.append(f"{_quoted('publication_id')} IN ({ids})")
    parameters.extend(bound.primary_ids)
    _filter_predicates(bound, _discriminator(plan.suppression_columns), (predicates, parameters))
    return predicates, parameters


def _withheld_flag(plan: QueryPlan) -> str:
    """Flag a withheld row as 1, else 0; always 0 for a fact that can't withhold."""
    discriminator = _discriminator(plan.suppression_columns)
    return f"CASE WHEN {_quoted(discriminator)} THEN 1 ELSE 0 END" if discriminator else "0"


def _attributable(plan: QueryPlan) -> str:
    """Sum the rank metric's attributable rows: withheld ones excluded, as every read does."""
    column = _quoted(plan.measures[0])
    discriminator = _discriminator(plan.suppression_columns)
    if not discriminator:
        return f"SUM({column})"
    return f"SUM(CASE WHEN NOT {_quoted(discriminator)} THEN {column} END)"


def _keyed(ranked: RankedRead) -> tuple[str, ...]:
    """Name the ranked grain's CTE columns: the partitions, then key and marketplace."""
    return (
        *(_quoted(name) for name in ranked.partition_columns),
        _quoted("key"),
        _quoted("marketplace"),
    )


def _source_keyed(ranked: RankedRead) -> tuple[str, ...]:
    """Select the ranked grain from a fact under the CTE's names."""
    return (
        *(_quoted(name) for name in ranked.partition_columns),
        f"{_quoted(ranked.key_column)} AS {_quoted('key')}",
        f"{_quoted(ranked.marketplace_column)} AS {_quoted('marketplace')}",
    )


def _rank_sql(ranked: RankedRead, bound: BoundQueryPlan) -> tuple[str, list[str]]:
    """One rank read summed per ranked key (the rank or the change window), with withholding."""
    plan = bound.logical
    carried = tuple(_quoted(name) for name in plan.carried_columns)
    predicates, parameters = _fenced_read(bound)
    grouped = ", ".join(
        (
            *(_quoted(name) for name in ranked.partition_columns),
            _quoted(ranked.key_column),
            _quoted(ranked.marketplace_column),
            *carried,
        )
    )
    discriminator = _discriminator(plan.suppression_columns)
    withheld = (
        ()
        if not discriminator
        else (
            f"SUM(CASE WHEN {_quoted(discriminator)} THEN {_quoted(plan.measures[0])} ELSE 0 END) "
            f"AS {withheld_alias(0)}",
            f"SUM(CASE WHEN {_quoted(discriminator)} THEN 1 ELSE 0 END) "
            f"AS {withheld_rows_alias(0)}",
        )
    )
    selected = ", ".join(
        (
            *_source_keyed(ranked),
            *carried,
            f"{_attributable(plan)} AS {_quoted('rank_value')}",
            *withheld,
        )
    )
    sql = (
        # A-SQL-001: quoted identifiers, values bind as ?; see docs/security-dispositions.md
        f"SELECT {selected} FROM {_quoted(plan.fact)} "  # noqa: S608  # nosec B608
        f"WHERE {' AND '.join(predicates)} GROUP BY {grouped}"
    )
    return sql, parameters


def _joined_on(ranked: RankedRead, alias: str) -> str:
    return " AND ".join(f'"p".{name} = "{alias}".{name}' for name in _keyed(ranked))


def _ranked_cte(ranked: RankedRead) -> tuple[str, list[str]]:
    """Write the `ranked` CTE: the population, each key's rank value, the top `n` (ADR-143).

        WITH ranked AS (
          SELECT <grain>, rank_value, <carried>, <withheld>, population_count,
                 population_suppressed [, (SELECT SUM(m) FROM <sum read>) AS population_sum_i]
          FROM (SELECT *, COUNT(*) OVER (PARTITION BY <partition>),
                       MAX(suppressed) OVER (PARTITION BY <partition>),
                       ROW_NUMBER() OVER (PARTITION BY <partition> ORDER BY <rank order>)
                FROM (SELECT p.<grain>, p.suppressed, <rank value>, r.<carried>, r.<withheld>
                      FROM (<every population read's keys and withheld flag, UNION ALL,
                             grouped per key>) p
                      LEFT JOIN (<the rank read, summed per key>) r ON <grain>
                      [LEFT JOIN (<the change read, summed per key>) c ON <grain>]
                      [WHERE <rank value> > 0]))
          WHERE rank_position <= n)

    The rank value is `COALESCE(r, 0)`, or `ABS(COALESCE(r, 0) - COALESCE(c, 0))` by change.
    Every read in it is fenced to its own exact publications, as every composed read is, so the
    population is the one the batch's compares measured; the count, the withholding and each sum
    are taken before the top `n` is kept (ADR-152). No key list is built, so the statement's size
    is independent of the catalog. Parameters bind in text order: sums, population, rank, change.
    """
    restriction = ranked.restriction
    keyed = _keyed(ranked)
    sums: list[str] = []
    sum_parameters: list[str] = []
    for index, bound in enumerate(ranked.sums):
        predicates, values = _fenced_read(bound)
        sums.append(
            # A-SQL-001: quoted identifiers, values bind as ?; see docs/security-dispositions.md
            f"(SELECT {_attributable(bound.logical)} FROM {_quoted(bound.logical.fact)} "  # noqa: S608  # nosec B608
            f"WHERE {' AND '.join(predicates)}) AS {population_sum_alias(index)}"
        )
        sum_parameters.extend(values)
    population: list[str] = []
    population_parameters: list[str] = []
    for bound in ranked.population:
        predicates, values = _fenced_read(bound)
        population.append(
            # A-SQL-001: quoted identifiers, values bind as ?; see docs/security-dispositions.md
            f"SELECT {', '.join(_source_keyed(ranked))}, "  # noqa: S608  # nosec B608
            f"{_withheld_flag(bound.logical)} AS {_quoted(_SUPPRESSED)} "
            f"FROM {_quoted(bound.logical.fact)} WHERE {' AND '.join(predicates)}"
        )
        population_parameters.extend(values)
    rank_sql, rank_parameters = _rank_sql(ranked, ranked.rank)
    rank = ranked.rank.logical
    carried = tuple(_quoted(name) for name in rank.carried_columns)
    withheld = tuple(alias for alias in rank.result_aliases if alias != measure_alias(0))
    value = 'COALESCE("r".' + _quoted("rank_value") + ", 0)"
    change_join, change_parameters = "", []
    if ranked.change is not None:
        change_sql, change_parameters = _rank_sql(ranked, ranked.change)
        change_join = f'LEFT JOIN ({change_sql}) "c" ON {_joined_on(ranked, "c")} '
        value = f'ABS({value} - COALESCE("c".{_quoted("rank_value")}, 0))'
    positive = f"WHERE {value} > 0 " if restriction.membership is RankedMembership.POSITIVE else ""
    grouped_population = (
        # A-SQL-001: identifiers only; every value binds in the population reads as ?
        f"SELECT {', '.join(keyed)}, MAX({_quoted(_SUPPRESSED)}) AS {_quoted(_SUPPRESSED)} "  # noqa: S608  # nosec B608
        f'FROM ({" UNION ALL ".join(population)}) "u" GROUP BY {", ".join(keyed)}'
    )
    base = ", ".join(
        (
            *(f'"p".{name}' for name in keyed),
            f'"p".{_quoted(_SUPPRESSED)}',
            f"{value} AS {_quoted('rank_value')}",
            *(f'"r".{name}' for name in carried),
            *(f'COALESCE("r".{name}, 0) AS {name}' for name in withheld),
        )
    )
    partition = ", ".join(_quoted(name) for name in ranked.partition_columns)
    over = f"PARTITION BY {partition} " if partition else ""
    windowed = ", ".join(
        (
            "*",
            f"COUNT(*) OVER ({over.strip()}) AS {_quoted(POPULATION_COUNT)}",
            f"MAX({_quoted(_SUPPRESSED)}) OVER ({over.strip()}) "
            f"AS {_quoted(POPULATION_SUPPRESSED)}",
            f"ROW_NUMBER() OVER ({over}ORDER BY {_rank_order()}) AS {_quoted(_POSITION)}",
        )
    )
    kept = ", ".join(
        (
            *keyed,
            _quoted("rank_value"),
            *carried,
            *withheld,
            _quoted(POPULATION_COUNT),
            _quoted(POPULATION_SUPPRESSED),
            *sums,
        )
    )
    cte = (
        # A-SQL-001: quoted identifiers, values bind as ?; see docs/security-dispositions.md
        f"WITH {_quoted(RANKED_CTE)} AS (SELECT {kept} "  # noqa: S608  # nosec B608
        f"FROM (SELECT {windowed} FROM (SELECT {base} "
        f'FROM ({grouped_population}) "p" '
        f'LEFT JOIN ({rank_sql}) "r" ON {_joined_on(ranked, "r")} '
        f'{change_join}{positive}) "b") "w" '
        f"WHERE {_quoted(_POSITION)} <= {restriction.n}) "
    )
    return cte, [
        *sum_parameters,
        *population_parameters,
        *rank_parameters,
        *change_parameters,
    ]


def _compose_ranked(bound: BoundQueryPlan, max_rows: int, ranked: RankedRead) -> ComposedQuery:
    """Build a statement restricted to the ranked keys: the keys themselves, or a semi-join.

    `RANKED_KEYS` selects `ranked` under the plan's own column and aggregate names, so its rows
    read exactly as a `METRIC_BY_GRAIN` answer of the rank metric: one row per ranked key, its
    value the rank value, then the population columns (`population_columns`), which the adapter
    lifts off before any row reaches an answer. A carried column (the currency) comes from the
    key's rank row, or the ranked set's own when the key has none, so a zero-revenue key still
    states the currency. `SEMI_JOIN` is the plan's ordinary read, keeping only rows whose key
    (and partition) is ranked.
    """
    cte, parameters = _ranked_cte(ranked)
    plan = bound.logical
    if ranked.restriction.projection is RankedProjection.SEMI_JOIN:
        fact = _quoted(plan.fact)
        matched = " AND ".join(
            f"{_quoted(RANKED_CTE)}.{name} = {fact}.{_quoted(column)}"
            for name, column in zip(
                _keyed(ranked),
                (*ranked.partition_columns, ranked.key_column, ranked.marketplace_column),
                strict=True,
            )
        )
        exists = (
            # A-SQL-001: identifiers only, from the plan; no value is interpolated
            f"EXISTS (SELECT 1 FROM {_quoted(RANKED_CTE)} WHERE {matched})"  # noqa: S608  # nosec B608
        )
        # The read's own WHERE gains the semi-join as its first conjunct; nothing else changes.
        inner = _compose_single(bound, max_rows, (exists,))
        return ComposedQuery(sql=cte + inner.sql, parameters=(*parameters, *inner.parameters))
    columns: list[str] = []
    for name in plan.selected_columns:
        if name == ranked.key_column:
            columns.append(f"{_quoted('key')} AS {_quoted(name)}")
        elif name == ranked.marketplace_column:
            columns.append(f"{_quoted('marketplace')} AS {_quoted(name)}")
        elif name in ranked.partition_columns:
            columns.append(_quoted(name))
        else:
            columns.append(
                f"COALESCE({_quoted(name)}, MAX({_quoted(name)}) OVER ()) AS {_quoted(name)}"
            )
    aggregates = ", ".join(
        f"{_quoted('rank_value')} AS {alias}" if alias == measure_alias(0) else alias
        for alias in plan.result_aliases
    )
    trailing = ", ".join(_quoted(name) for name in population_columns(ranked))
    ordering = ", ".join((*(_quoted(name) for name in ranked.partition_columns), _rank_order()))
    sql = (
        # A-SQL-001: identifiers only, from the plan; every value binds in the CTE as ?
        f"SELECT {', '.join(columns)}, {aggregates}, {trailing} "  # noqa: S608  # nosec B608
        f"FROM {_quoted(RANKED_CTE)} ORDER BY {ordering} LIMIT {max_rows + 1}"
    )
    return ComposedQuery(sql=cte + sql, parameters=tuple(parameters))
