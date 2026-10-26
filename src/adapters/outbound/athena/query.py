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

import random
import time
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal

import boto3

from adapters.outbound.athena.composition import (
    POPULATION_COUNT,
    POPULATION_SUPPRESSED,
    ComposedQuery,
    compose,
    compose_series,
    population_columns,
    population_sum_alias,
)
from adapters.outbound.athena.deadline import bake_statement_deadline
from adapters.outbound.athena.execution import (
    MAX_WAIT_SECONDS,
    QUERY_FAILED,
    StatementOutcome,
    StatementRequest,
    await_completion,
    iter_pages,
    start_counted,
    stream_result_rows,
)
from adapters.outbound.telemetry.span_context import (
    QueryStatistics,
    admission_wait_ms,
    collect_query,
    correlation,
)
from adapters.outbound.telemetry.tracing import ATHENA_QUERY, span, trace_boundary
from domain.measurement.figures import Withholding, WithholdingAccumulator
from domain.measurement.periodic import PERIOD_END_COLUMN, PERIOD_START_COLUMN
from domain.measurement.planning import QueryPlan, measure_alias, withheld_rows_alias
from domain.measurement.ranking import (
    RANKED_INVARIANT,
    RankedPopulation,
    RankedProjection,
    RankedRead,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError, ValidationError
from domain.shared_kernel.provenance import Suppression
from ports.outbound.query import (
    FoldRead,
    PeriodSeriesRequest,
    QueryRequest,
    QueryResult,
    QueryRow,
    RowCeiling,
    RowSink,
    statement_limit,
)

if TYPE_CHECKING:  # pragma: no cover — kept out of the Lambda import path
    from types_boto3_athena.client import AthenaClient
    from types_boto3_s3.client import S3Client

__all__ = [
    "ALL_ROWS_WITHHELD",
    "BAKE_INVOCATION_WAIT",
    "INTERACTIVE_THROTTLED_START_MAX_WAIT_SECONDS",
    "INTERACTIVE_WAIT",
    "SUPPRESSION_UNREADABLE",
    "THROTTLED_START_MAX_WAIT_SECONDS",
    "AthenaQueryAdapter",
    "AthenaQueryTarget",
    "ResultTransport",
    "StatementWait",
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


THROTTLED_START_MAX_WAIT_SECONDS: Final = 60.0
"""How long a bake's throttled start keeps retrying (ADR-144): at least 3x the longest observed
bake statement (16.2 s), so active queries finish and free quota inside it. A throttle that
outlasts it is a sustained overload, which the task's receive retry handles."""

INTERACTIVE_THROTTLED_START_MAX_WAIT_SECONDS: Final = 10.0
"""How long a chat or presentation throttled start keeps retrying (owner, A7 option 1): it
leaves at least 20 s of the interactive 30 s wait for the statement itself."""


@dataclass(frozen=True, slots=True)
class StatementWait:
    """How long a target's statements may wait, and for how long a throttled start retries.

    Two policies for two kinds of caller (ADR-144). `interactive` waits `MAX_WAIT_SECONDS` from
    the statement's start and retries a throttled start for 10 s inside that. `bake_invocation`
    waits until the page's statement deadline, which the page execution sets from the Lambda
    invocation's remaining time; outside it a bake statement is an invariant (ATHENA/5). Its
    throttled start retries for 60 s, never past the deadline. No caller has both.
    """

    name: Literal["interactive", "bake_invocation"]

    def deadline(self) -> float:
        """Return the monotonic instant a statement started now must finish by."""
        if self.name == "interactive":
            return time.monotonic() + MAX_WAIT_SECONDS
        return bake_statement_deadline()

    def throttled_start_until(self, first_throttle: float) -> float | None:
        """Return the monotonic instant a throttled start stops retrying."""
        if self.name == "interactive":
            return first_throttle + INTERACTIVE_THROTTLED_START_MAX_WAIT_SECONDS
        return min(bake_statement_deadline(), first_throttle + THROTTLED_START_MAX_WAIT_SECONDS)


INTERACTIVE_WAIT: Final = StatementWait("interactive")
"""Chat, presentation and precompute: the API Gateway-bounded 30 s wait."""
BAKE_INVOCATION_WAIT: Final = StatementWait("bake_invocation")
"""The baker: the statement waits within its invocation (ADR-144)."""


class ResultTransport(StrEnum):
    """How a target reads its statements' rows (ADR-143). Closed; no caller has two.

    `PAGED` pages `GetQueryResults` (at most 1,000 rows a call, from an account-wide 100 calls a
    second): chat and request-time presentation reads, whose results live under a prefix their
    roles can't read as objects, bounded by `MAX_ROWS_CEILING`. `OBJECT` streams the statement's
    CSV result object with one `GetObject`, one buffer resident at any row count: the baker only.
    """

    PAGED = "paged"
    OBJECT = "object"


@dataclass(frozen=True, slots=True)
class AthenaQueryTarget:
    """One enforced V2 interactive workgroup and current-only generated database.

    `wait` and `results` are required with no default: every composition names its policy and
    its transport, so none can silently get another caller's (ADR-144, ADR-143).
    """

    workgroup: str
    database: str
    reuse_minutes: int
    wait: StatementWait
    results: ResultTransport

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
    max_rows: int | None
    """None for a grouped fold (`RowCeiling.BAKE_GROUPED`): every row is read, none truncated."""
    ceiling: RowCeiling
    """The caller kind, recorded on the span so a grouped read is told from a held one."""
    ranked: RankedRead | None = None
    """A ranked-keys statement's restriction: its rows carry the population columns too."""


class AthenaQueryAdapter:
    """Reads the curated lake through one enforced workgroup.

    The workgroup and database come from configuration rather than from the request, so a
    caller cannot redirect a query at another catalog. The workgroup also carries the scan
    cutoff and the result location, and it is configured with
    `EnforceWorkGroupConfiguration` — so this adapter *cannot* override either even if a
    future edit tried to.
    """

    def __init__(  # noqa: PLR0913 -- the target, its two clients and the injectable jitter
        self,
        target: AthenaQueryTarget,
        client: AthenaClient | None = None,
        random_source: random.Random | None = None,
        *,
        s3: S3Client | None = None,
    ) -> None:
        self._target = target
        self._client = client or boto3.client("athena")
        # The result-object reader's client, made only for a target that reads objects, and made
        # here: reads run on the batch's worker threads, and boto3's shared default session
        # isn't safe to create clients from concurrently.
        if s3 is None and target.results is ResultTransport.OBJECT:
            s3 = boto3.client("s3")
        self._s3 = s3
        # The throttled start's full jitter, from the OS source; injectable so a test fixes the
        # sleeps.
        self._random = random_source or random.SystemRandom()

    def run(self, request: QueryRequest) -> QueryResult:
        """Execute a plan and return its rows, or raise a `DependencyError`."""
        statement = self._statement(request)
        _refuse_grouped_held(request.ceiling)
        held = _Held()
        lifted = None if statement.ranked is None else _PopulationLift(statement.ranked)
        read = self._execute(statement, held, lifted)
        population = () if lifted is None else lifted.result()
        return QueryResult(
            # The statement asked for one row more than requested, which is the only way to
            # tell "there were exactly this many" from "there were more and you got a page".
            rows=tuple(held.rows),
            bytes_scanned=read.bytes_scanned,
            truncated=read.truncated,
            # Measured from the rows the statement returned, never asserted. ADR-035. A ranked
            # population withheld anywhere is withheld here too, though no shown key was.
            suppression=_with_population(statement.logical, read.suppression, population),
            population=population,
        )

    def run_series(self, request: PeriodSeriesRequest) -> QueryResult:
        """Execute one plan over several exact periods as one statement; each row names its period.

        Every plan passes the same preflight `run` applies, before any statement starts, so a
        period Amazon withheld whole refuses the series exactly as it would refuse its own read.
        """
        statement = self._series_statement(request)
        _refuse_grouped_held(request.ceiling)
        held = _Held()
        read = self._execute(statement, held)
        return QueryResult(
            rows=tuple(held.rows),
            bytes_scanned=read.bytes_scanned,
            truncated=read.truncated,
            suppression=read.suppression,
        )

    def fold(self, request: QueryRequest, sink: RowSink) -> FoldRead:
        """Execute a plan and hand each kept row to `sink`, holding none (ADR-143)."""
        return self._execute(self._statement(request), sink)

    def fold_series(self, request: PeriodSeriesRequest, sink: RowSink) -> FoldRead:
        """Execute a period series and hand each kept row to `sink`, holding none (ADR-143)."""
        return self._execute(self._series_statement(request), sink)

    @staticmethod
    def _statement(request: QueryRequest) -> _Statement:
        _refuse_withheld_only(request)
        _refuse_unreadable_suppression(request)
        logical = request.plan.logical
        ranked = request.ranked
        keys = (
            ranked
            if ranked is not None and ranked.restriction.projection is RankedProjection.RANKED_KEYS
            else None
        )
        limit = statement_limit(request.max_rows, request.ceiling)
        return _Statement(
            compose(request.plan, limit, ranked),
            logical,
            logical.selected_columns,
            limit,
            request.ceiling,
            keys,
        )

    @staticmethod
    def _series_statement(request: PeriodSeriesRequest) -> _Statement:
        for bound in request.plans:
            single = QueryRequest(plan=bound, max_rows=request.max_rows, ceiling=request.ceiling)
            _refuse_withheld_only(single)
            _refuse_unreadable_suppression(single)
        logical = request.plans[0].logical
        limit = statement_limit(request.max_rows, request.ceiling)
        return _Statement(
            compose_series(request.plans, limit),
            logical,
            (PERIOD_START_COLUMN, PERIOD_END_COLUMN, *logical.selected_columns),
            limit,
            request.ceiling,
        )

    def _values(
        self, execution_id: str, outcome: StatementOutcome, cells: int
    ) -> Iterator[list[str | None]]:
        """Read a completed statement's rows through the target's one transport."""
        if self._target.results is ResultTransport.OBJECT:
            if self._s3 is None:  # pragma: no cover - built in __init__ for an OBJECT target
                raise AssertionError("an object-reading target lost its S3 client")
            return stream_result_rows(self._s3, outcome, execution_id=execution_id, cells=cells)
        # Page by page, held or folded: one page is resident while the sink takes its rows.
        return iter_pages(self._client, execution_id)

    @trace_boundary(ATHENA_QUERY)
    def _execute(
        self, statement: _Statement, sink: RowSink, lifted: _PopulationLift | None = None
    ) -> FoldRead:
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
            row_ceiling=statement.ceiling.value,
            max_rows=max_rows,
            run_id=None if corr is None else corr.run_id,
            task_id=None if corr is None else corr.task_id,
            cohort_revision=None if corr is None else corr.cohort_revision,
        ) as recording:
            # A2.1: the closed statement kind, the workgroup, the admission wait and the bake's
            # correlation ids (absent outside a bake) are known before the start.
            wait = self._target.wait
            recording.record(
                workgroup=self._target.workgroup,
                statement_kind=STATEMENT_KIND,
                admission_wait_millis=waited,
                wait_policy=wait.name,
                transport=self._target.results.value,
            )
            # ADR-144: the deadline is fixed before the start, so a throttled start's retries
            # come out of the statement's own wait, never on top of it.
            deadline = wait.deadline()
            started = start_counted(
                self._client,
                StatementRequest(
                    sql=composed.sql,
                    parameters=composed.parameters,
                    workgroup=self._target.workgroup,
                    database=self._target.database,
                    reuse_minutes=self._target.reuse_minutes,
                ),
                retry_throttled_until=wait.throttled_start_until(time.monotonic()),
                random_source=self._random,
            )
            execution_id, throttled = started.execution_id, started.throttled_attempts
            limit = max(0.0, deadline - time.monotonic())
            # Not a user-facing identifier — it goes in the span and the log, never in a
            # description — but it is the one handle that ties a span to Athena's own history.
            recording.record(
                query_id=execution_id,
                start_throttled_attempts=throttled,
                wait_limit_s=round(limit, 3),
            )
            try:
                outcome = await_completion(
                    self._client, execution_id, limit=limit, wait_policy=wait.name
                )
            except DependencyError as error:
                # A2.2: a failed, cancelled or timed-out execution keeps its span, with its
                # terminal state; its statistics are recorded as unavailable, never dropped.
                state = _terminal_state(error)
                recording.record(state=state, statistics_unavailable="execution_not_succeeded")
                collect_query(_statistics(state, None, waited, throttled=throttled))
                raise
            recording.record(
                state="SUCCEEDED",
                service_processing_millis=outcome.service_processing_millis,
                statistics_unavailable=None if outcome.statistics_present else "statistics_absent",
            )
            collect_query(_statistics("SUCCEEDED", outcome, waited, throttled=throttled))
            aliases = logical.result_aliases
            suppression = _SuppressionAccumulator(logical)
            rows_read = 0
            # Each row is mapped, measured for withholding and handed on as it is read; a held
            # read's sink keeps it, a fold's keeps only its summary. The one row past `max_rows`
            # is counted, never kept: it is how truncation is told from an exact fit.
            trailing = () if statement.ranked is None else population_columns(statement.ranked)
            width = len(statement.columns) + len(aliases)
            for values in self._values(execution_id, outcome, width + len(trailing)):
                rows_read += 1
                if max_rows is not None and rows_read > max_rows:
                    continue
                row = _as_row(statement.columns, values[:width], aggregates=aliases)
                if lifted is not None:
                    # The population columns never reach an answer: lifted off, checked, kept.
                    lifted.add(row, values[width:])
                suppression.add(row)
                sink.add(row)
            truncated = max_rows is not None and rows_read > max_rows
            kept = max_rows if truncated and max_rows is not None else rows_read
            recording.record(
                bytes_scanned=outcome.bytes_scanned,
                cache_hit=outcome.reused_previous_result,
                engine_millis=outcome.engine_millis,
                planning_millis=outcome.planning_millis,
                queue_millis=outcome.queue_millis,
                total_millis=outcome.total_millis,
                rows_returned=kept,
                truncated=truncated,
            )
            return FoldRead(
                rows_read=rows_read,
                truncated=truncated,
                bytes_scanned=outcome.bytes_scanned,
                suppression=suppression.result(),
            )


def _terminal_state(error: DependencyError) -> str:
    """Name a non-succeeded execution's terminal state: Athena's, or the timeout's outcome."""
    state = error.context.get("state")
    if isinstance(state, str) and state:
        return state
    return "CANCELLED" if error.context.get("cancellation_requested") else "UNKNOWN"


def _statistics(  # noqa: PLR0913 -- one execution's state, outcome, wait and throttles
    state: str, outcome: StatementOutcome | None, waited: int | None, *, throttled: int
) -> QueryStatistics:
    """Project one execution's statistics for the bake task's summary roll-up (A2.3)."""
    if outcome is None:
        return QueryStatistics(
            state,
            0,
            0,
            0,
            0,
            0,
            0,
            waited or 0,
            statistics_available=False,
            throttled_starts=throttled,
        )
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
        throttled_starts=throttled,
    )


def _refuse_grouped_held(ceiling: RowCeiling) -> None:
    """Refuse to hold a grouped fold's rows: with no row limit, only a sink may take them."""
    if ceiling is RowCeiling.BAKE_GROUPED:
        raise ValueError("a grouped read is folded, never held: it has no row limit.")


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


class _Held:
    """A held read's sink: keeps every row it is handed (at most `max_rows`), in order."""

    def __init__(self) -> None:
        self.rows: list[QueryRow] = []

    def add(self, row: QueryRow) -> None:
        self.rows.append(row)


class _PopulationLift:
    """Collect a ranked-keys statement's population columns, one entry per partition.

    Every row of a partition carries the same population figures, because the statement takes
    them over the partition before keeping its top `n`. A row that disagrees means the SQL and
    the declaration differ: an invariant (`RANKING/2`), never a figure picked from one row.
    """

    def __init__(self, ranked: RankedRead) -> None:
        self._ranked = ranked
        self._by_partition: dict[tuple[str, ...], RankedPopulation] = {}

    def add(self, row: QueryRow, values: list[str | None]) -> None:
        names = population_columns(self._ranked)
        cells = dict(zip(names, values, strict=True))
        partition = tuple(str(row.get(column)) for column in self._ranked.partition_columns)
        sums = self._ranked.restriction.population_sums
        population = RankedPopulation(
            partition=partition,
            count=_population_int(cells[POPULATION_COUNT]),
            sums=tuple(
                (metric, _decimal_or_none(cells[population_sum_alias(index)]) or Decimal(0))
                for index, metric in enumerate(sums)
            ),
            suppressed=_population_int(cells[POPULATION_SUPPRESSED]) > 0,
        )
        held = self._by_partition.setdefault(partition, population)
        if held != population:
            raise InvariantError(
                RANKED_INVARIANT,
                "A ranked statement's population figures differ within one partition.",
                context={"metric": self._ranked.restriction.rank_metric},
            )

    def result(self) -> tuple[RankedPopulation, ...]:
        return tuple(self._by_partition[key] for key in sorted(self._by_partition))


def _population_int(raw: str | None) -> int:
    value = _decimal_or_none(raw)
    if value is None or value != value.to_integral_value() or value < 0:
        raise InvariantError(
            RANKED_INVARIANT,
            "A ranked statement's population count is not a whole number.",
            context={"problem": "population column did not parse as a count"},
        )
    return int(value)


def _with_population(
    plan: QueryPlan, measured: Suppression, population: tuple[RankedPopulation, ...]
) -> Suppression:
    """Return a ranked read's suppression: its rows' own, or its population's withholding."""
    if measured.rows_suppressed or not any(item.suppressed for item in population):
        return measured
    grain = plan.grain.dimensions[0] if plan.grain.dimensions else "group"
    return Suppression(
        rows_suppressed=True,
        reason=(
            f"Amazon withheld rows for some {grain} values in the population this ranking is "
            "taken from, because too few shoppers sat behind them."
        ),
    )


class _SuppressionAccumulator:
    """`_suppression_from`, one row at a time, so a held and a folded read can't diverge."""

    def __init__(self, plan: QueryPlan) -> None:
        self._plan = plan
        self._measured = bool(plan.suppression_columns) or (
            plan.join is not None and bool(plan.join.suppression_columns)
        )
        self._withholding = WithholdingAccumulator(plan)

    def add(self, row: QueryRow) -> None:
        if self._measured:
            self._withholding.add(dict(row))

    def result(self) -> Suppression:
        if not self._measured:
            return Suppression(rows_suppressed=False)
        withholding = self._withholding.result()
        if not withholding.occurred:
            return Suppression(rows_suppressed=False)
        return Suppression(rows_suppressed=True, reason=_reason(self._plan, withholding))


def _suppression_from(plan: QueryPlan, rows: tuple[QueryRow, ...]) -> Suppression:  # pyright: ignore[reportUnusedFunction] -- the tuple form of the accumulator
    """State what was withheld, measured from the rows that came back.

    A fact with no suppression columns cannot have withheld anything — Vendor Central has no
    threshold mechanism at all — so `rows_suppressed=False` is a provable absence rather than an
    assumption. For a fact that can, the answer now comes from a fold over each group instead of a
    refusal, and `False` is earned the same way: the count came back zero.

    The reason is composed rather than read. `suppression_reason` is deliberately absent from every
    conformed fact (G-28) because a per-delivery value has no meaning at a row several deliveries
    assembled, so an answer says what it measured.
    """
    accumulator = _SuppressionAccumulator(plan)
    for row in rows:
        accumulator.add(row)
    return accumulator.result()


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
