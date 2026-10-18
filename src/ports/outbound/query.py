# SPDX-License-Identifier: MIT-0
"""Query port.

The signature is the enforcement. There is deliberately no way to pass SQL
through this interface, and never should be: that one change would move "never
execute model-authored SQL" from a structural guarantee back to a rule someone
has to remember.

**What crosses is a `QueryPlan`, and the guarantee it carries is stronger than a
template name.** A plan is what `domain/measurement/planning.py` produces after
validating a request against the metric dictionary and the fact's own contract, so
by the time it arrives here:

- the fact and the measure column came from the dictionary's declared expression,
  not from a caller
- every group-by column came from a closed map of dimension names
- **every filter key has been checked against the fact's real column list**, which
  matters because a filter key is an identifier, SQL cannot bind an identifier, and
  an adapter therefore has to interpolate it
- every grain column is accounted for, so the aggregation cannot double-count
  ([ADR-024](../../../docs/decisions/adr-024-plans-account-for-every-grain-column.md))

The rule this leaves for an adapter: **interpolate identifiers, bind values.** Filter
values and window bounds are caller-supplied data and must be passed as query
parameters, never formatted into the statement. Identifiers are safe to interpolate
only because the plan validated them, which is why composing SQL from anything other
than a `QueryPlan` is not permitted here.

A template name was the earlier shape and was withdrawn. It would have needed one
registered template per metric-and-grain combination — dozens, each hand-written
beside a dictionary that already declares the same thing, and each free to drift
from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from domain.measurement.publications import BoundQueryPlan
from domain.measurement.ranking import RankedRead
from domain.shared_kernel.provenance import Suppression

type QueryParam = str | int | Decimal | bool
type QueryRow = dict[str, str | int | Decimal | None]

MAX_ROWS_CEILING = 10_000
"""The largest result any single tool call may ask for.

Not a performance tuning knob. A tool returns figures a model will narrate, and a
result too large to narrate honestly gets truncated — at which point the answer is
partial and must say so. Capping the request keeps that decision visible instead of
letting a caller ask for a million rows and quietly receive a page.
"""

BAKE_MAX_ROWS: Final = 10_000
"""The most rows one publish-time held answer may keep (ADR-143): a memory-sized defect guard.

Sized by the baker's memory model only (`bakeability.resident`), never by narration: a bake
renders a page, it does not narrate. Finite on purpose, because an unbounded fetch crashed dev
(2026-08-22). Derived by the row-ceiling probe on 2026-10-18 at 1,024 MiB (ADR-143 decision 6);
re-derived after bodies stream and the baker's memory changes.
"""

BAKE_LONGEST_WINDOW_DAYS: Final = 31
"""The longest bake window, in days; the bakeability gate holds it equal to the page policies."""

BAKE_MAX_STREAMED_ROWS: Final = BAKE_LONGEST_WINDOW_DAYS * BAKE_MAX_ROWS
"""The most rows a publish-time reduction may stream through its fold: derived, not chosen.

A reduction streams a daily or monthly series of a population the held ceiling already bounds,
so its ceiling is the longest window times that population. Its memory is the fold's enforced
`max_kept`, not its rows.
"""


class RowCeiling(StrEnum):
    """Which caller kind a read belongs to, and so which row ceiling bounds it (ADR-143).

    Not a tool input: chat and request-time presentation reads are always `NARRATION`.
    """

    NARRATION = "narration"
    """Chat and presentation live reads: `MAX_ROWS_CEILING`."""
    BAKE = "bake"
    """Publish-time held answers: `BAKE_MAX_ROWS`."""
    BAKE_STREAMED = "bake_streamed"
    """Publish-time reductions: `BAKE_MAX_STREAMED_ROWS`."""


def row_ceiling(kind: RowCeiling) -> int:
    """Return the most rows one read of this caller kind may ask for."""
    match kind:
        case RowCeiling.NARRATION:
            return MAX_ROWS_CEILING
        case RowCeiling.BAKE:
            return BAKE_MAX_ROWS
        case RowCeiling.BAKE_STREAMED:
            return BAKE_MAX_STREAMED_ROWS


def _require_rows_within(max_rows: int, ceiling: RowCeiling) -> None:
    """Refuse a row bound outside 1..the caller kind's ceiling (the port's `ValueError`)."""
    if max_rows < 1:
        raise ValueError("max_rows must be at least 1.")
    bound = row_ceiling(ceiling)
    if max_rows > bound:
        if ceiling is RowCeiling.NARRATION:
            raise ValueError(
                f"max_rows {max_rows} exceeds {MAX_ROWS_CEILING}. A result that large "
                "cannot be narrated honestly, so asking for it hides a truncation."
            )
        raise ValueError(
            f"max_rows {max_rows} exceeds the {ceiling.value} ceiling {bound}, which the "
            "baker's memory model certifies."
        )


@dataclass(frozen=True, slots=True)
class QueryRequest:
    """A validated plan, plus how many rows the caller can use and which ceiling bounds it.

    `ranked` restricts the read to a ranked set computed inside the statement (Retail's top 50,
    ADR-143): publish-time only, never a tool input.
    """

    plan: BoundQueryPlan
    max_rows: int
    ceiling: RowCeiling = RowCeiling.NARRATION
    ranked: RankedRead | None = None

    def __post_init__(self) -> None:
        _require_rows_within(self.max_rows, self.ceiling)
        if self.ranked is not None and self.ceiling is RowCeiling.NARRATION:
            raise ValueError("a ranked restriction is a publish-time read, never a narration one.")


def _series_shape(bound: BoundQueryPlan) -> tuple[object, ...]:
    """Everything a period series' plans share: the read itself, less its period."""
    plan = bound.logical
    return (
        plan.metric.name,
        plan.fact,
        plan.measures,
        plan.group_by,
        tuple(sorted(plan.filters.items())),
        plan.carried_columns,
        plan.currency_source,
        plan.suppression_columns,
        bound.primary_filters,
    )


@dataclass(frozen=True, slots=True)
class PeriodSeriesRequest:
    """One planned read over several exact complete periods, run as one statement.

    Each plan is the same single-fact periodic read, planned and bound to its own period's CURRENT
    publications exactly as `QueryRequest` would carry it, so one statement answers every period
    instead of one query per period. Each result row carries its own `period_start` and
    `period_end` and no measure is summed across periods; a caller measuring figures from the
    rows must group by those columns itself, and the result's `suppression` is folded over every
    period at once rather than reported per period. The work is bounded: the selection
    that bound the plans admits at most `MAX_EXACT_CURRENT_PARTITIONS` partitions, and the result
    at most `max_rows`. Plans are read in the order given; the statement orders rows newest period
    first.
    """

    plans: tuple[BoundQueryPlan, ...]
    max_rows: int
    ceiling: RowCeiling = RowCeiling.NARRATION

    def __post_init__(self) -> None:
        if not self.plans:
            raise ValueError("a period series reads at least one period.")
        _require_rows_within(self.max_rows, self.ceiling)  # the same row bound as one plan
        if any(item.logical.join is not None or not item.logical.periodic for item in self.plans):
            raise ValueError("a period series reads one periodic fact per plan.")
        shape = _series_shape(self.plans[0])
        if any(_series_shape(item) != shape for item in self.plans[1:]):
            raise ValueError("a period series repeats one read; its plans differ beyond period.")
        windows = [item.logical.window for item in self.plans]
        if len(set(windows)) != len(windows):
            raise ValueError("a period series reads each period once.")


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Rows, plus what is needed to report cost and honesty upstream."""

    rows: tuple[QueryRow, ...]
    bytes_scanned: int
    truncated: bool
    """True when max_rows cut the result short. Must reach ShapedResponse."""

    suppression: Suppression
    """Whether a privacy threshold withheld rows from these results.

    Required, with no default. A default would read downstream as "nothing was
    withheld", which is a claim rather than an absence — and the one claim this
    platform must never make by accident. Without this field an adapter had no way
    to state suppression at all, so a tool building a ShapedResponse had to invent
    the value; the envelope requires it.

    The adapter reads this from the curated row's suppression columns. It does not
    compute it: suppression is established at extraction, and by the time rows reach
    here the only question is whether the fact survived the journey. See
    docs/concepts/provenance-chain.md.
    """


class RowSink(Protocol):
    """Consumes a read's rows one at a time (a fold's row rules, `domain.measurement.answers`)."""

    def add(self, row: QueryRow) -> None:
        """Take one result row; the sink keeps only its own bounded summary."""
        ...


@dataclass(frozen=True, slots=True)
class FoldRead:
    """What a streamed read cost and said about itself; its rows went to the sink only.

    `rows_read` counts every row the statement returned, including the one past `max_rows`
    that detects truncation; the sink received at most `max_rows`. `suppression` is measured
    from the rows the sink received by the same accumulator a held read uses.
    """

    rows_read: int
    truncated: bool
    bytes_scanned: int
    suppression: Suppression


@runtime_checkable
class QueryPort(Protocol):
    """Reads the curated lake. Implemented by an Athena adapter.

    The domain never learns which engine answers.
    """

    def run(self, request: QueryRequest) -> QueryResult:
        """Execute a registered template.

        Raises DependencyError once retries are exhausted. Never leaks an SDK
        exception type.
        """
        ...


@runtime_checkable
class PeriodSeriesQueryPort(QueryPort, Protocol):
    """A `QueryPort` that also reads one plan over several exact periods in one statement."""

    def run_series(self, request: PeriodSeriesRequest) -> QueryResult:
        """Execute one statement over every period; each row names its period.

        Refuses before executing whatever `run` would refuse for any one of the plans, such as a
        publication Amazon withheld whole. Raises DependencyError once retries are exhausted.
        """
        ...


@runtime_checkable
class FoldingQueryPort(PeriodSeriesQueryPort, Protocol):
    """A `PeriodSeriesQueryPort` that also streams a read through a sink (ADR-143).

    A reduction never holds its rows: each one goes to the sink as it is read, and only the
    sink's bounded summary survives. Implemented for both result transports, so a fold reads
    the same rows whichever transport the composition named.
    """

    def fold(self, request: QueryRequest, sink: RowSink) -> FoldRead:
        """Execute one plan and hand each kept row to `sink`; refuse as `run` refuses."""
        ...

    def fold_series(self, request: PeriodSeriesRequest, sink: RowSink) -> FoldRead:
        """Execute one period series and hand each kept row to `sink`; refuse as `run_series`."""
        ...
