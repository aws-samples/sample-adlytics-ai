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
- **The process ledger.** Each render enters the process's ledger first, so a page's memory
  reading can be told apart from the process's (02 §3.6, "one page observation").

This module imports neither `baker` nor `graph_bake`: both import it.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Protocol, cast
from uuid import uuid4

from adapters.jobs.preset_artifact_publisher.bake_results import BakeResult, BakeUnavailable
from adapters.outbound.athena.deadline import (
    reset_bake_statement_deadline,
    set_bake_statement_deadline,
)
from adapters.outbound.telemetry.graph_metrics import process_memory_peak_mb
from domain.operations.execution_graph_release import (
    BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND,
    BakeRosterRow,
    unavailable_reason,
)

if TYPE_CHECKING:
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
    "ProcessBakeLedger",
    "ProcessEntry",
    "page_absence",
    "render_page",
    "stage_page",
]


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
    """One page's render: its result, what it executed, and its ledger entry."""

    result: BakeResult
    executed: LimitResult
    entry: ProcessEntry
    refusal: Exception | None = None
    """The source refusal the result's designed absence was mapped from, if any."""
    deadline_clamped: bool = False
    """Whether the invocation reported negative remaining time (read as zero)."""


def render_page(  # noqa: PLR0913 -- the page's parts, intent, window and absence rule
    parts: PageParts,
    intent: BakeIntent,
    window: PageWindow,
    *,
    absent_on: tuple[type[Exception], ...] = (),
) -> PageRender:
    """Render one roster page under its statement deadline and its certified count.

    In order: enter the process ledger (so the entry is read even when the render raises); set
    the statement deadline to `now + remaining - (tail + write_seconds)` and reset it on exit;
    run the page's baker under `limited(certified_plan_count)`. Over the bound is
    `PageFatalError`, sticky and chained to any render error. A render error of an `absent_on`
    type is the page's designed absence, with what the render executed; any other propagates.
    """
    entry = parts.process.enter()
    remaining = window.remaining_ms()
    clamped = remaining < 0
    margin = window.tail_seconds + window.row.write_seconds
    token = set_bake_statement_deadline(monotonic() + max(0, remaining) / 1_000 - margin)
    render_error: Exception | None = None
    result: BakeResult | None = None
    try:
        with parts.queries.limited(window.row.certified_plan_count) as limit:
            try:
                result = parts.baker.bake(intent)
            # Held, not swallowed: the over-bound check below must win over any render error.
            # error-propagation: re-raised below, after the sticky over-bound check
            except Exception as error:
                render_error = error
    finally:
        reset_bake_statement_deadline(token)
    executed = cast("LimitResult", limit.result)
    if executed.over_bound:
        raise PageFatalError(BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND) from render_error
    if render_error is not None:
        if isinstance(render_error, absent_on):
            return PageRender(page_absence(intent), executed, entry, render_error, clamped)
        raise render_error
    return PageRender(cast("BakeResult", result), executed, entry, None, clamped)


def stage_page[T](
    write: Callable[[PresetArtifactPublicationRequest], T],
    request: PresetArtifactPublicationRequest,
    window: PageWindow,
) -> T:
    """Write one rendered page through its entry's one stage.

    `write` is the legacy entry's `publish` or the graph entry's `stage`; `window` is the page's,
    which the stage's runtime write deadline reads (ADR-145).
    """
    del window
    return write(request)
