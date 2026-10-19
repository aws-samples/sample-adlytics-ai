# SPDX-License-Identifier: MIT-0
"""The `QueryPort` implementation: run a composed plan and shape the result.

Three modules, split by what each can be tested against. `composition.py` is pure, so the SQL
is assertable character by character in the unit tier. `execution.py` holds the Athena
primitives shared with the write path. This module is the thin part that decides what a result
*means* — how many rows the caller asked for, and whether anything was withheld — and it is
covered against the real deployed workgroup, because that is the only place an engine can
reject what we wrote.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import boto3

from adapters.outbound.athena.composition import ComposedQuery, compose, compose_series
from adapters.outbound.athena.execution import (
    MAX_WAIT_SECONDS,
    QUERY_FAILED,
    StatementOutcome,
    StatementRequest,
    await_completion,
    fetch_rows,
    start,
)
from adapters.outbound.telemetry.span_context import (
    QueryStatistics,
    admission_wait_ms,
    collect_query,
    correlation,
)
from adapters.outbound.telemetry.tracing import ATHENA_QUERY, span, trace_boundary
from domain.measurement.figures import Withholding, withholding_in
from domain.measurement.periodic import PERIOD_END_COLUMN, PERIOD_START_COLUMN
from domain.measurement.planning import QueryPlan, measure_alias, withheld_rows_alias
from domain.shared_kernel.errors import DependencyError, ErrorCode, ValidationError
from domain.shared_kernel.provenance import Suppression
from ports.outbound.query import PeriodSeriesRequest, QueryRequest, QueryResult, QueryRow

if TYPE_CHECKING:  # pragma: no cover — kept out of the Lambda import path
    from types_boto3_athena.client import AthenaClient

__all__ = [
    "ALL_ROWS_WITHHELD",
    "SUPPRESSION_UNREADABLE",
    "AthenaQueryAdapter",
    "AthenaQueryTarget",
]

SUPPRESSION_UNREADABLE = ErrorCode("ATHENA", 3)
ALL_ROWS_WITHHELD = ErrorCode("ATHENA", 4)
STATEMENT_KIND = "measurement"
"""A2.1: the closed statement kind of every execution this adapter runs (never SQL text)."""

AGGREGATE_COLUMN = measure_alias(0)
"""What the composed statement aliases its first aggregate to.

Derived from the domain's own naming rather than restated. A plan may select more than one — a
difference of sums and a same-fact ratio each select two — so this names the first and
`measure_alias` names the rest.
"""


@dataclass(frozen=True, slots=True)
class AthenaQueryTarget:
    """One enforced V2 interactive workgroup and current-only generated database."""

    workgroup: str
    database: str
    reuse_minutes: int

    def __post_init__(self) -> None:
        if not self.workgroup.strip() or not self.database.strip():
            raise ValueError("Athena V2 query target requires workgroup and current database.")
        if self.reuse_minutes < 0:
            raise ValueError("Athena V2 query reuse age cannot be negative.")


@dataclass(frozen=True, slots=True)
class _Statement:
    """One composed statement, the plan its rows are read as, and the columns they carry."""

    composed: ComposedQuery
    logical: QueryPlan
    columns: tuple[str, ...]
    max_rows: int


class AthenaQueryAdapter:
    """Reads the curated lake through one enforced workgroup.

    The workgroup and database come from configuration rather than from the request, so a
    caller cannot redirect a query at another catalog. The workgroup also carries the scan
    cutoff and the result location, and it is configured with
    `EnforceWorkGroupConfiguration` — so this adapter *cannot* override either even if a
    future edit tried to.
    """

    def __init__(self, target: AthenaQueryTarget, client: AthenaClient | None = None) -> None:
        self._target = target
        self._client = client or boto3.client("athena")

    def run(self, request: QueryRequest) -> QueryResult:
        """Execute a plan and return its rows, or raise a `DependencyError`."""
        _refuse_withheld_only(request)
        _refuse_unreadable_suppression(request)
        logical = request.plan.logical
        return self._execute(
            _Statement(
                compose(request.plan, request.max_rows),
                logical,
                logical.selected_columns,
                request.max_rows,
            )
        )

    def run_series(self, request: PeriodSeriesRequest) -> QueryResult:
        """Execute one plan over several exact periods as one statement; each row names its period.

        Every plan passes the same preflight `run` applies, before any statement starts, so a
        period Amazon withheld whole refuses the series exactly as it would refuse its own read.
        """
        for bound in request.plans:
            single = QueryRequest(plan=bound, max_rows=request.max_rows)
            _refuse_withheld_only(single)
            _refuse_unreadable_suppression(single)
        logical = request.plans[0].logical
        return self._execute(
            _Statement(
                compose_series(request.plans, request.max_rows),
                logical,
                (PERIOD_START_COLUMN, PERIOD_END_COLUMN, *logical.selected_columns),
                request.max_rows,
            )
        )

    @trace_boundary(ATHENA_QUERY)
    def _execute(self, statement: _Statement) -> QueryResult:
        """Run one composed statement and shape its rows, or raise a `DependencyError`.

        Wrapped in the `athena.query` span observability §3 requires, which did not exist at all
        before 2026-08-22. Auto-instrumentation already records the SDK calls, and that is precisely
        what is not enough: it shows eleven `GetQueryExecution` polls without saying that one
        question waited four seconds on one statement.

        The attributes are recorded **after** completion because that is when they are known, and
        they are the point of the span rather than decoration. `bytes_scanned` separates a slow
        query from a large one; `cache_hit` says whether Athena answered from a reused result, which
        nothing here could previously observe; and the three timings separate queueing from planning
        from execution, where planning dominating means partition count rather than data volume.
        """
        logical, composed, max_rows = statement.logical, statement.composed, statement.max_rows
        # Owner decision 2026-10-15 ~04:25Z: beneath bake or precompute cohort correlation the
        # span carries run, task and cohort; a serve-time chat query has none and records them
        # absent rather than refusing the question.
        corr = correlation()
        waited = admission_wait_ms()
        with span(
            ATHENA_QUERY,
            metric=logical.metric.name,
            facts=",".join(logical.facts_read),
            max_rows=max_rows,
            run_id=None if corr is None else corr.run_id,
            task_id=None if corr is None else corr.task_id,
            cohort_revision=None if corr is None else corr.cohort_revision,
        ) as recording:
            # A2.1: the closed statement kind, the workgroup, the admission wait and the bake's
            # correlation ids (absent outside a bake) are known before the start.
            recording.record(
                workgroup=self._target.workgroup,
                statement_kind=STATEMENT_KIND,
                admission_wait_millis=waited,
            )
            execution_id = start(
                self._client,
                StatementRequest(
                    sql=composed.sql,
                    parameters=composed.parameters,
                    workgroup=self._target.workgroup,
                    database=self._target.database,
                    reuse_minutes=self._target.reuse_minutes,
                ),
            )
            # Not a user-facing identifier — it goes in the span and the log, never in a
            # description — but it is the one handle that ties a span to Athena's own history.
            recording.record(query_id=execution_id)
            try:
                outcome = await_completion(self._client, execution_id, limit=MAX_WAIT_SECONDS)
            except DependencyError as error:
                # A2.2: a failed, cancelled or timed-out execution keeps its span, with its
                # terminal state; its statistics are recorded as unavailable, never dropped.
                state = _terminal_state(error)
                recording.record(state=state, statistics_unavailable="execution_not_succeeded")
                collect_query(_statistics(state, None, waited))
                raise
            recording.record(
                state="SUCCEEDED",
                service_processing_millis=outcome.service_processing_millis,
                statistics_unavailable=None if outcome.statistics_present else "statistics_absent",
            )
            collect_query(_statistics("SUCCEEDED", outcome, waited))
            raw = fetch_rows(self._client, execution_id)
            aliases = logical.result_aliases
            rows = [_as_row(statement.columns, values, aggregates=aliases) for values in raw]

            kept = tuple(rows[:max_rows])
            truncated = len(rows) > max_rows
            recording.record(
                bytes_scanned=outcome.bytes_scanned,
                cache_hit=outcome.reused_previous_result,
                engine_millis=outcome.engine_millis,
                planning_millis=outcome.planning_millis,
                queue_millis=outcome.queue_millis,
                total_millis=outcome.total_millis,
                rows_returned=len(kept),
                truncated=truncated,
            )
            return QueryResult(
                # The statement asked for one row more than requested, which is the only way to
                # tell "there were exactly this many" from "there were more and you got a page".
                rows=kept,
                bytes_scanned=outcome.bytes_scanned,
                truncated=truncated,
                # Measured from the rows the statement returned, never asserted. ADR-035.
                suppression=_suppression_from(logical, kept),
            )


def _terminal_state(error: DependencyError) -> str:
    """Name a non-succeeded execution's terminal state: Athena's, or the timeout's outcome."""
    state = error.context.get("state")
    if isinstance(state, str) and state:
        return state
    return "CANCELLED" if error.context.get("cancellation_requested") else "UNKNOWN"


def _statistics(
    state: str, outcome: StatementOutcome | None, waited: int | None
) -> QueryStatistics:
    """Project one execution's statistics for the bake task's summary roll-up (A2.3)."""
    if outcome is None:
        return QueryStatistics(state, 0, 0, 0, 0, 0, 0, waited or 0, statistics_available=False)
    return QueryStatistics(
        state,
        outcome.queue_millis,
        outcome.planning_millis,
        outcome.engine_millis,
        outcome.service_processing_millis,
        outcome.total_millis,
        outcome.bytes_scanned,
        waited or 0,
        statistics_available=outcome.statistics_present,
    )


def _refuse_withheld_only(request: QueryRequest) -> None:
    """Refuse before Athena when any selected bucket has no attributable rows."""
    withheld = tuple(
        publication
        for publication in request.plan.all_publications
        if publication.evidence.withheld_only
    )
    if not withheld:
        return
    raise ValidationError(
        ALL_ROWS_WITHHELD,
        (
            "Amazon withheld every attributable row for at least one selected period, "
            "so no figure can be reported."
        ),
        context={"withheld_periods": len(withheld)},
    )


def _refuse_unreadable_suppression(request: QueryRequest) -> None:
    """Refuse a plan whose withholding this adapter could not report, before spending a query.

    **Narrowed by ADR-035 from a blanket refusal to a shape check.** It used to refuse every fact
    that *could* withhold,
    which blocked 38 of 50 metrics whether or not anything had been withheld — a capability refusal
    where a per-query measurement was available.

    What remains is the case that would still be a claim rather than a measurement: a plan that
    declares suppression columns but whose result carries no withheld aggregates to read. That is
    unreachable through `plan()`, because `result_aliases` derives the pair from the same
    `suppression_columns` the composer folds on. It is checked anyway, because the failure mode is a
    silent `rows_suppressed=False` on a fact that withholds.

    **Both sides, not just the numerator.** `roas_blended` divides vendor revenue by AMC spend, so
    its numerator declares nothing and its denominator declares three columns. Reading only the
    numerator returned "nothing was withheld" while the denominator folded a real flag — the exact
    false claim this refusal exists to prevent, made silently.
    """
    plan = request.plan.logical
    declared = tuple(plan.suppression_columns)
    if plan.join is not None:
        declared = (*declared, *plan.join.suppression_columns)
    if not declared:
        return

    operands = 2 if plan.join is not None else len(plan.measures)
    expected = {withheld_rows_alias(index) for index in range(operands)}
    if expected <= set(plan.result_aliases):
        return

    raise DependencyError(
        SUPPRESSION_UNREADABLE,
        "This fact can withhold rows for privacy, and this query cannot report that.",
        context={
            "fact": plan.fact,
            "declared": sorted(set(declared)),
            "problem": "the plan declares suppression columns but carries no withheld aggregate, "
            "so reporting rows_suppressed=False would be a claim rather than a measurement",
        },
    )


def _suppression_from(plan: QueryPlan, rows: tuple[QueryRow, ...]) -> Suppression:
    """State what was withheld, measured from the rows that came back.

    A fact with no suppression columns cannot have withheld anything — Vendor Central has no
    threshold mechanism at all — so `rows_suppressed=False` is a provable absence rather than an
    assumption. For a fact that can, the answer now comes from a fold over each group instead of a
    refusal, and `False` is earned the same way: the count came back zero.

    The reason is composed rather than read. `suppression_reason` is deliberately absent from every
    conformed fact (G-28) because a per-delivery value has no meaning at a row several deliveries
    assembled, so an answer says what it measured.
    """
    if not plan.suppression_columns and (plan.join is None or not plan.join.suppression_columns):
        return Suppression(rows_suppressed=False)

    withholding = withholding_in(plan, tuple(dict(row) for row in rows))
    if not withholding.occurred:
        return Suppression(rows_suppressed=False)
    return Suppression(rows_suppressed=True, reason=_reason(plan, withholding))


def _reason(plan: QueryPlan, withholding: Withholding) -> str:
    """Say what was withheld, in the metric's own units and against how much was not.

    Names the proportion of *groups* rather than of rows, because that is what a reader of a
    breakdown can act on — "one campaign of forty" is a usable answer and "thirty-nine of forty" is
    one to discard. `freshness-registry` §5.1 requires a measured rate rather than a boolean for
    exactly that reason.
    """
    grain = plan.grain.dimensions[0] if plan.grain.dimensions else "group"
    absent = withholding.withheld_amount
    if withholding.amount:
        # The stated amount covers only the groups whose withheld money kept its currency; the
        # currency-less ones are named after it, typed "withheld" (AMC-V decision 5).
        covered = withholding.groups - withholding.without_currency
        stated = (
            f"Amazon withheld {withholding.amount} {plan.metric.label.lower()} across "
            f"{covered} of {withholding.total_groups} {grain} value(s), because too few "
            "shoppers sat behind those rows."
        )
        tail = f"In {absent.rows} more {grain} value(s)," if absent is not None else ""
    else:
        stated = (
            f"Amazon withheld {withholding.withheld_rows} row(s) across {withholding.groups} of "
            f"{withholding.total_groups} {grain} value(s), because too few shoppers sat behind "
            "them."
        )
        tail = f"In {absent.rows} of those {grain} value(s)," if absent is not None else ""
    return stated if absent is None else f"{stated} {tail} {absent.text}"


def _as_row(
    columns: tuple[str, ...], values: list[str | None], *, aggregates: tuple[str, ...]
) -> QueryRow:
    """Pair the selected columns with one result row, plus every aggregate it carries.

    Athena returns every value as a string. They are handed over as strings rather than coerced
    here, because deciding what a value *means* — an exact quantity, a missing measure, a
    currency — is measurement semantics and belongs to `domain/measurement`. The exception is
    the aggregates, parsed to `Decimal` so a non-numeric total fails at the boundary that
    produced it rather than three layers later.

    `aggregates` names them, in the order the statement selects them, taken from the plan rather
    than inferred from the row's length. A difference of sums and a same-fact ratio each select
    two, and a drill-across selects four — its aggregates interleaved with the suppression
    indicators that decide what a missing side means. A count could not express that ordering, and
    guessing from the width would read a carried column as an operand.
    """
    row: QueryRow = {}
    for name, value in zip(columns, values, strict=False):
        row[name] = value
    # The trailing selected expressions, in the plan's own order. Guarded rather than indexed
    # blindly: a shorter row means the statement and this mapping disagree, and pairing the wrong
    # value with an aggregate would be silent.
    for index, alias in enumerate(aggregates):
        position = len(columns) + index
        raw = values[position] if position < len(values) else None
        row[alias] = _decimal_or_none(raw)
    return row


def _decimal_or_none(raw: str | None) -> Decimal | None:
    """Parse the aggregate, or None when the group produced no value."""
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise DependencyError(
            QUERY_FAILED,
            "The query returned a total that is not a number.",
            context={"problem": "aggregate did not parse as a decimal"},
        ) from None
