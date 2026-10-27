# SPDX-License-Identifier: MIT-0
"""One page execution for both bake entries (ADR-142, ADR-144).

The graph entry (dev) and the legacy entry (every other deployment) are two triggers and two
pointer protocols, not two implementations: each renders a roster page through `render_page`
and writes it through `stage_page`. So what a page's render is held to lives here once:

- **The statement deadline** (ADR-144). `render_page` sets it from the invocation's remaining
  time, less the row's write term and the contract's tail, before the source runs, and resets
  it on exit. Every bake statement waits within it; one outside it is an invariant (ATHENA/5).
- **The certified count** (A5.1 I5). The render runs under the row's certified plan count; over
  it is `PageFatalError(BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND)`, sticky, whatever the source did.
- **Isolation** (ADR-142). A source's item and section refusals are collected and grouped; a
  page-scope refusal, typed or classified (`classify_page_error`), is the page's designed
  absence with one page group and the render's item notes discarded. Anything else propagates.
- **The stage's write deadline** (ADR-145). `stage_page` hands the stage the invocation's time
  and the contract's tail; a stage that runs out before every pair is submitted refuses the page
  (`bodies_over_invocation`). The stage draws the sibling stream as it writes, so a sibling the
  source renders then may refuse the page too (`body_over_cap`). A page-scope refusal is the
  only stage error caught here.
- **The one emitter.** `emit_isolated_refusals` produces exactly one span, one line and one
  datapoint per `(scope, reason)` group, and `report_over_budget` the over-budget alarm, for
  both entries; telemetry runs through `guarded`, so it never changes the business result.
- **The process ledger.** Each render enters the process's ledger first, so a page's memory
  reading can be told apart from the process's (02 §3.6, "one page observation").

This module imports neither `baker` nor `graph_bake`: both import it.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from uuid import uuid4

from adapters.jobs.preset_artifact_publisher.bake_results import BakeResult, BakeUnavailable
from adapters.jobs.preset_artifact_publisher.isolated_refusals import collecting_refusals
from adapters.outbound.athena.deadline import (
    reset_bake_statement_deadline,
    set_bake_statement_deadline,
)
from adapters.outbound.athena.execution import QUERY_TIMED_OUT
from adapters.outbound.telemetry.graph_metrics import (
    emit_isolated_refusal,
    emit_task_over_budget,
    process_memory_peak_mb,
)
from adapters.outbound.telemetry.reporting import LogSeverity, OperationLog, report_operation
from adapters.outbound.telemetry.tracing import BAKE_ISOLATED_REFUSAL, span, trace_boundary
from domain.operations.bake_memory import BAKER_MEMORY_BYTES, MIB
from domain.operations.bake_refusals import (
    MEMORY_SIZED,
    OPERATOR_ACTIONS,
    IsolatedRefusal,
    IsolatedRefusalReason,
    RefusalGroup,
    RefusalScope,
    check_refusal_scope,
    classify_refusal,
    group_refusals,
)
from domain.operations.execution_graph_release import (
    BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND,
    BakeRosterRow,
    unavailable_reason,
)
from domain.shared_kernel.errors import (
    AdlyticsError,
    DependencyError,
    DomainError,
    ValidationError,
)

if TYPE_CHECKING:
    from aws_lambda_powertools import Logger

    from adapters.jobs.preset_artifact_publisher.transport import BakeIntent
    from adapters.outbound.bake_sources import BakeQueryCapture, LimitResult
    from domain.presentation.preset_artifacts import PresetArtifactPublicationRequest

__all__ = [
    "PROCESS_LEDGER",
    "PageBaker",
    "PageFatalError",
    "PageParts",
    "PageRender",
    "PageWindow",
    "PageWrite",
    "ProcessBakeLedger",
    "ProcessEntry",
    "RefusalLineage",
    "classify_page_error",
    "emit_isolated_refusals",
    "guarded",
    "page_absence",
    "render_page",
    "report_over_budget",
    "stage_page",
]

_OPERATION = "preset_bake"
_COMPONENT = "page_bake"


class PageBaker(Protocol):
    """The page registry as the page execution uses it: render one intent."""

    def bake(self, intent: BakeIntent) -> BakeResult:
        """Render one roster intent."""
        ...


class PageFatalError(Exception):
    """A deterministic page failure: a defect, never the page's designed absence."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def page_absence(intent: BakeIntent) -> BakeUnavailable:
    """Return a refused page's designed absence: its page's generated unavailable reason."""
    return BakeUnavailable(intent, unavailable_reason(intent.page))


@dataclass(frozen=True, slots=True)
class PageWindow:
    """What one page's render may spend: the invocation's time, its row's terms, its receive."""

    remaining_ms: Callable[[], int]
    """The Lambda context's remaining time; required, no default."""
    row: BakeRosterRow
    """Its certified plan count and write term."""
    tail_seconds: float
    """`BAKE_TASKS["tail_seconds"]`, the contract's 30 s (not a row field)."""
    final: bool
    """Whether this is the record's last receive (`ROUTES["bake"]["max_receive_count"]`)."""


@dataclass(frozen=True, slots=True)
class ProcessEntry:
    """What one `render_page` entry read from its process's ledger."""

    process_id: str
    process_bake_ordinal: int
    rss_before_mb: float
    process_baseline_mb: float


class ProcessBakeLedger:
    """One per Python process: the renders it ran, in order, and its peak before the first.

    `process_id` names the process; each `enter()` advances the ordinal and reads the peak. The
    first entry's read is the process's baseline, kept for every later entry, so growth over the
    process is measurable on any line. One record per invocation and one invocation per
    environment at a time, so no lock is needed (`count` is also atomic under the GIL).
    """

    def __init__(self, peak: Callable[[], float] = process_memory_peak_mb) -> None:
        self._peak = peak
        self._process_id = uuid4().hex
        self._ordinals = itertools.count(1)
        self._baseline: float | None = None

    def enter(self) -> ProcessEntry:
        """Advance the ordinal and read the peak for one page's render."""
        ordinal = next(self._ordinals)
        before = self._peak()
        if self._baseline is None:
            self._baseline = before
        return ProcessEntry(self._process_id, ordinal, before, self._baseline)

    def peak(self) -> float:
        """Read the process's peak now (the page observation's `memory_peak_mb`)."""
        return self._peak()


PROCESS_LEDGER = ProcessBakeLedger()
"""The process's one ledger; both compositions pass it, and a test passes a fresh one."""


@dataclass(frozen=True, slots=True)
class PageParts:
    """What a page's render runs on: the registry, the counted query port, the ledger."""

    baker: PageBaker
    queries: BakeQueryCapture
    """Never None: an unconfigured invocation passes a capture over a port that refuses."""
    process: ProcessBakeLedger


@dataclass(frozen=True, slots=True)
class PageRender:
    """One page's render: its result, its refusal groups, what it executed, its ledger entry."""

    result: BakeResult
    groups: tuple[RefusalGroup, ...]
    """The receive's `(scope, reason)` groups: the page's one group, or its item notes."""
    executed: LimitResult
    entry: ProcessEntry
    refusal: IsolatedRefusal | None = None
    """The page-scope refusal the result's designed absence is, if any."""
    cause: AdlyticsError | None = None
    """The error that refusal was classified from (itself, for a typed refusal)."""
    deadline_clamped: bool = False
    """Whether the invocation reported negative remaining time (read as zero)."""


def classify_page_error(error: BaseException, *, final: bool) -> IsolatedRefusal | None:
    """Return the page-scope refusal an error raised by a page's work is, or None (ADR-142).

    A source's `DomainError` or `ValidationError` is classified (`classify_refusal`). A bake-wait
    timeout (`ATHENA/2` under `bake_invocation`) is `statement_time_over_invocation` on any
    receive: it isn't retried. Any other `DependencyError` is `dependency_unavailable` only on
    the final receive; before it, it retries. Everything else is a defect (None).
    """
    if isinstance(error, IsolatedRefusal) and error.scope is not RefusalScope.PAGE:
        return None  # an item or section refusal is noted, never raised: a source defect
    if isinstance(error, DomainError | ValidationError):
        return classify_refusal(error)
    if not isinstance(error, DependencyError):
        return None
    if error.code == QUERY_TIMED_OUT and error.context.get("wait_policy") == "bake_invocation":
        figures = {
            key: round(value)
            for key in ("waited_seconds", "wait_limit_seconds")
            if isinstance(value := error.context.get(key), int | float)
            and not isinstance(value, bool)
            and 0 <= value < 2**31
        }
        return IsolatedRefusal(
            IsolatedRefusalReason.STATEMENT_TIME_OVER_INVOCATION,
            RefusalScope.PAGE,
            **cast("dict[str, Any]", figures),
        )
    if final:
        return IsolatedRefusal(
            IsolatedRefusalReason.DEPENDENCY_UNAVAILABLE,
            RefusalScope.PAGE,
            error_code=str(error.code),
        )
    return None


def render_page(parts: PageParts, intent: BakeIntent, window: PageWindow) -> PageRender:
    """Render one roster page under its statement deadline and its certified count.

    In order: enter the process ledger (so the entry is read even when the render raises); set
    the statement deadline to `now + remaining - (tail + write_seconds)` and reset it on exit;
    open the item-refusal collector; run the page's baker under `limited(certified_plan_count)`.
    Over the bound is `PageFatalError`, sticky and chained to any render error. A render error
    `classify_page_error` maps is the page's designed absence with one page group and the item
    notes discarded; any other propagates. A scope claim the page doesn't declare is an
    invariant (`check_refusal_scope`).
    """
    entry = parts.process.enter()
    remaining = window.remaining_ms()
    clamped = remaining < 0
    margin = window.tail_seconds + window.row.write_seconds
    token = set_bake_statement_deadline(monotonic() + max(0, remaining) / 1_000 - margin)
    render_error: Exception | None = None
    result: BakeResult | None = None
    try:
        with (
            collecting_refusals() as notes,
            parts.queries.limited(window.row.certified_plan_count) as limit,
        ):
            try:
                result = parts.baker.bake(intent)
            # Held, not swallowed: the over-bound check below must win over any render error.
            except Exception as error:
                render_error = error
    finally:
        reset_bake_statement_deadline(token)
    executed = cast("LimitResult", limit.result)
    if executed.over_bound:
        raise PageFatalError(BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND) from render_error
    if render_error is not None:
        refusal = classify_page_error(render_error, final=window.final)
        if refusal is None:
            raise render_error
        return PageRender(
            page_absence(intent),
            group_refusals((refusal,)),
            executed,
            entry,
            refusal,
            cast("AdlyticsError", render_error),
            clamped,
        )
    rendered = cast("BakeResult", result)
    # A page that publishes nothing reports no items: its notes describe no published body.
    kept = () if isinstance(rendered, BakeUnavailable) else tuple(notes)
    groups = group_refusals(check_refusal_scope(note, intent.page) for note in kept)
    return PageRender(rendered, groups, executed, entry, None, None, clamped)


class PageWrite[T](Protocol):
    """An entry's one write: the legacy `publish` or the graph `stage`, with its deadline."""

    def __call__(
        self,
        request: PresetArtifactPublicationRequest,
        /,
        *,
        remaining_ms: Callable[[], int],
        tail_seconds: float,
    ) -> T:
        """Write one rendered page, refusing it when the write deadline is reached."""
        ...


def stage_page[T](
    write: PageWrite[T],
    request: PresetArtifactPublicationRequest,
    window: PageWindow,
) -> T | IsolatedRefusal:
    """Write one rendered page through its entry's one stage, under the write deadline.

    `write` is the legacy entry's `publish` or the graph entry's `stage`. A stage that reaches
    its deadline (`remaining_ms() / 1000 < tail_seconds`) before every pair is submitted refuses
    the page with `bodies_over_invocation`; a sibling the stream renders while the stage writes
    may refuse it as at render (`body_over_cap`). Either page-scope refusal is returned, so the
    caller settles the page's absence (no parent, index or pointer was written). Any other error
    propagates.
    """
    try:
        return write(request, remaining_ms=window.remaining_ms, tail_seconds=window.tail_seconds)
    except IsolatedRefusal as refusal:
        if refusal.scope is RefusalScope.PAGE:
            return refusal
        raise


# --- the one emitter ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefusalLineage:
    """Which entry and which bake a refusal belongs to: the line's only non-closed fields."""

    entry: Literal["graph", "legacy"]
    page: str
    variant: str
    run_id: str | None = None
    task_id: str | None = None
    execution_id: str | None = None

    def details(self) -> dict[str, object]:
        """Return the entry, the lineage and the page; an absent lineage field is omitted."""
        lineage = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
        }
        return {
            "entry": self.entry,
            **{name: value for name, value in lineage.items() if value is not None},
            "page": self.page,
            "variant": self.variant,
        }


def guarded(emit: Callable[[], None], lost: Callable[[str], None]) -> None:
    """Run one telemetry emission; report a failure through `lost(cause)`, never raise.

    Telemetry is never a bake dependency (plan 4.7, A2.2): the business result is unchanged, and
    a failed loss report can't itself be reported.
    """
    try:
        emit()
    # error-propagation: reported through `lost` (counted and logged WARNING by the entry)
    except Exception as error:
        cause = type(error).__name__
    else:
        return
    try:
        lost(cause)
    # error-propagation: guards the telemetry seam itself; a failed report cannot be reported
    except Exception:
        return


def _refusal_of(group: RefusalGroup) -> IsolatedRefusal:
    return IsolatedRefusal(
        group.reason, group.scope, group.count, **cast("dict[str, Any]", dict(group.context))
    )


@trace_boundary(BAKE_ISOLATED_REFUSAL)
def _emit_group(  # noqa: PLR0913, PLR0917 -- one group, its lineage and its three sinks
    logger: Logger | None,
    metric: Callable[[str, int], None],
    group: RefusalGroup,
    lineage: RefusalLineage,
    replayed: bool,
) -> None:
    """Emit one group: its failed span, its one WARNING line and its one datapoint."""
    figures = dict(group.context)
    with span(
        BAKE_ISOLATED_REFUSAL,
        entry=lineage.entry,
        page=lineage.page,
        variant=lineage.variant,
        scope=group.scope,
        reason=group.reason,
        refusal_class=group.refusal_class,
        count=group.count,
        replayed=replayed,
    ) as recording:
        recording.record(
            rows_read=figures.get("rows_read"),
            max_rows=figures.get("max_rows"),
            encoded_bytes=figures.get("encoded_bytes"),
            max_bytes=figures.get("max_bytes"),
            bodies=figures.get("bodies"),
            max_bodies=figures.get("max_bodies"),
            written=figures.get("written"),
            required_partitions=figures.get("required_partitions"),
            maximum_partitions=figures.get("maximum_partitions"),
            waited_seconds=figures.get("waited_seconds"),
            wait_limit_seconds=figures.get("wait_limit_seconds"),
            population=figures.get("population"),
            guard=figures.get("guard"),
            attempts=figures.get("attempts"),
            error_code=figures.get("error_code"),
        )
        recording.note_failure(_refusal_of(group))
    if logger is not None:
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="isolated_refusal",
                outcome=group.reason.value,
                severity=LogSeverity.WARNING,
                details={
                    **lineage.details(),
                    "scope": group.scope.value,
                    "refusal_class": group.refusal_class.value,
                    "count": group.count,
                    "replayed": replayed,
                    **figures,
                    **_operator_action(group),
                },
            ),
            logger,
        )
    metric(group.refusal_class.value, group.count)


def _operator_action(group: RefusalGroup) -> dict[str, object]:
    """Name what an operator does about a limit, and the memory a memory-sized one assumed."""
    action = OPERATOR_ACTIONS.get(group.reason)
    if action is None:
        return {}
    if group.reason not in MEMORY_SIZED:
        return {"operator_action": action}
    return {"operator_action": action, "memory_mib": BAKER_MEMORY_BYTES // MIB}


def emit_isolated_refusals(  # noqa: PLR0913 -- the sinks, the groups, their lineage, replay
    logger: Logger | None,
    groups: tuple[RefusalGroup, ...],
    lineage: RefusalLineage,
    *,
    replayed: bool,
    metric: Callable[[str, int], None] = emit_isolated_refusal,
) -> None:
    """Emit each `(scope, reason)` group once: one span, one line, one datapoint (ADR-142)."""
    for group in groups:
        _emit_group(logger, metric, group, lineage, replayed)


def report_over_budget(  # noqa: PLR0913, PLR0917 -- the sink, the measure, row, lineage, replay
    logger: Logger | None,
    seconds: float,
    row: BakeRosterRow,
    lineage: RefusalLineage,
    *,
    replayed: bool,
    metric: Callable[[], None] = emit_task_over_budget,
) -> bool:
    """Report a page bake over its certified seconds once; return whether it was over.

    Nothing is cut: the bake ran to completion. One WARNING line and one `BakeTaskOverBudget`
    datapoint; the operator decides whether it is a defect or data beyond the envelope.
    """
    if seconds <= row.budget_seconds:
        return False
    if logger is not None:
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="task",
                outcome="over_budget",
                severity=LogSeverity.WARNING,
                details={
                    **lineage.details(),
                    "seconds": round(seconds, 3),
                    "budget_seconds": row.budget_seconds,
                    "replayed": replayed,
                },
            ),
            logger,
        )
    metric()
    return True
