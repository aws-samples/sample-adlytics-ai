# SPDX-License-Identifier: MIT-0
"""Context carried from a bake task down to the spans its queries and writes open (plan A2).

The Athena adapter and the artifact writer are shared by many callers and know nothing of a bake.
A bake task binds three facts here, as context variables, and the deeper spans read them:

- `correlated(run_id, task_id, cohort_revision)`: the cohort identity every bake and
  precompute span carries (A2.1); `correlation()` is `None` for serve-time work.
- `admitted(wait_ms)`: set by the FIFO query admission around one execution, so that execution's
  `athena.query` span records how long it waited for a slot.
- `collecting()`: a per-task list each `athena.query` appends its statistics to, so the task's
  one summary line rolls them up (A2.3).

Context variables reach a worker thread only through `contextvars.copy_context()`; the fan-out
pools that run queries and writes submit their work that way, which also parents their spans.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

__all__ = [
    "QueryStatistics",
    "SpanCorrelation",
    "admission_wait_ms",
    "admitted",
    "collect_query",
    "collecting",
    "correlated",
    "correlation",
]


@dataclass(frozen=True, slots=True)
class SpanCorrelation:
    """The exact rolling cohort identity propagated through nested producer spans."""

    run_id: str
    task_id: str
    cohort_revision: str

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (self.run_id, self.task_id, self.cohort_revision)):
            raise ValueError("span correlation fields must be nonblank strings.")


@dataclass(frozen=True, slots=True)
class QueryStatistics:
    """One execution's terminal state and `GetQueryExecution` statistics, in milliseconds."""

    state: str
    queue_ms: int
    planning_ms: int
    engine_ms: int
    service_processing_ms: int
    total_ms: int
    scanned_bytes: int
    admission_wait_ms: int
    statistics_available: bool
    throttled_starts: int = 0
    """Throttled `StartQueryExecution` attempts the start retried through (ADR-144)."""


_CORRELATION: ContextVar[SpanCorrelation | None] = ContextVar(
    "adlytics_span_correlation", default=None
)
_ADMISSION_WAIT: ContextVar[int | None] = ContextVar("adlytics_admission_wait", default=None)
_COLLECTOR: ContextVar[list[QueryStatistics] | None] = ContextVar(
    "adlytics_query_statistics", default=None
)


@contextmanager
def correlated(run_id: str, task_id: str, cohort_revision: str) -> Generator[None]:
    """Bind the validated cohort identity every nested producer span records."""
    token = _CORRELATION.set(SpanCorrelation(run_id, task_id, cohort_revision))
    try:
        yield
    finally:
        _CORRELATION.reset(token)


def correlation() -> SpanCorrelation | None:
    """Return the bound cohort identity, or ``None`` outside cohort work."""
    return _CORRELATION.get()


@contextmanager
def admitted(wait_ms: int) -> Generator[None]:
    """Bind how long one execution waited for its admission slot."""
    token = _ADMISSION_WAIT.set(wait_ms)
    try:
        yield
    finally:
        _ADMISSION_WAIT.reset(token)


def admission_wait_ms() -> int | None:
    """Return the bound admission wait, or None when no admission ran the execution."""
    return _ADMISSION_WAIT.get()


@contextmanager
def collecting() -> Generator[list[QueryStatistics]]:
    """Collect every execution's statistics made under this context (one task)."""
    collected: list[QueryStatistics] = []
    token = _COLLECTOR.set(collected)
    try:
        yield collected
    finally:
        _COLLECTOR.reset(token)


def collect_query(statistics: QueryStatistics) -> None:
    """Append one execution's statistics to the bound collector, if any."""
    collector = _COLLECTOR.get()
    if collector is not None:
        collector.append(statistics)
