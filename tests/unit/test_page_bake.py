# SPDX-License-Identifier: MIT-0
"""The one page execution both bake entries call (ADR-142, ADR-144): deadline, bound, ledger."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, cast

import pytest
from bake_application_support import bake_parts

from adapters.jobs.preset_artifact_publisher.bake_results import (
    BakeResult,
    BakeUnavailable,
    BakeUnchanged,
)
from adapters.jobs.preset_artifact_publisher.page_bake import (
    PageFatalError,
    PageWindow,
    ProcessBakeLedger,
    page_absence,
    render_page,
    stage_page,
)
from adapters.jobs.preset_artifact_publisher.transport import BakeIntent
from adapters.outbound.athena.deadline import BAKE_DEADLINE_UNSET, bake_statement_deadline
from adapters.outbound.athena.query import BAKE_INVOCATION_WAIT
from adapters.outbound.bake_sources import BakeQueryCapture
from domain.operations.execution_graph_release import (
    BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND,
    BakeRoster,
    BakeRosterRow,
)
from domain.presentation.preset_artifacts import PresetArtifactRevision
from domain.shared_kernel.errors import (
    DependencyError,
    DomainError,
    ErrorCode,
    InvariantError,
)
from ports.outbound.query import QueryResult

pytestmark = pytest.mark.unit

ROSTER = BakeRoster.from_contract()
AUDIENCES = ROSTER.row("audiences#last_complete_month")
CAMPAIGNS = ROSTER.row("campaigns#7d")
TAIL = 30.0
REMAINING_MS = 600_000


def _window(row: BakeRosterRow, remaining: int = REMAINING_MS) -> PageWindow:
    return PageWindow(lambda: remaining, row, TAIL, final=False)


def _intent(row: BakeRosterRow) -> BakeIntent:
    return BakeIntent(row.page, row.variant)


@dataclass
class _DeadlineBaker:
    """Read the statement deadline as a bake statement would, then return a result."""

    result: BakeResult | None = None
    error: Exception | None = None
    seen: list[tuple[float, float]] = field(default_factory=list[tuple[float, float]])

    def bake(self, intent: BakeIntent) -> BakeResult:
        self.seen.append((monotonic(), bake_statement_deadline()))
        if self.error is not None:
            raise self.error
        return self.result or BakeUnchanged(
            intent, PresetArtifactRevision.from_publication_identities(("p",))
        )


@pytest.mark.parametrize("row", [AUDIENCES, CAMPAIGNS], ids=lambda row: row.page)
def test_the_deadline_is_remaining_less_the_tail_and_the_row_write_term(row: BakeRosterRow) -> None:
    baker = _DeadlineBaker()
    before = monotonic()
    render_page(bake_parts(baker), _intent(row), _window(row))
    ((now, deadline),) = baker.seen
    expected = REMAINING_MS / 1_000 - (TAIL + row.write_seconds)
    # Set once, before the source ran: between `before` and the statement's own read.
    assert before + expected - 0.01 <= deadline <= now + expected
    # Control: the row's write term is reserved, not only the tail.
    assert deadline - before < REMAINING_MS / 1_000 - TAIL


def test_the_deadline_is_reset_after_the_page_and_a_later_statement_is_an_invariant() -> None:
    render_page(bake_parts(_DeadlineBaker()), _intent(CAMPAIGNS), _window(CAMPAIGNS))
    with pytest.raises(InvariantError) as raised:
        BAKE_INVOCATION_WAIT.deadline()
    assert raised.value.code == BAKE_DEADLINE_UNSET
    # Paired: a render whose source raises resets it too (no leak into the next page).
    failing = _DeadlineBaker(error=DependencyError(ErrorCode("FICTIONAL", 1), "down"))
    with pytest.raises(DependencyError):
        render_page(bake_parts(failing), _intent(CAMPAIGNS), _window(CAMPAIGNS))
    with pytest.raises(InvariantError):
        bake_statement_deadline()


def test_negative_remaining_time_is_read_as_zero_and_recorded() -> None:
    baker = _DeadlineBaker()
    page = render_page(bake_parts(baker), _intent(CAMPAIGNS), _window(CAMPAIGNS, remaining=-5))
    ((now, deadline),) = baker.seen
    assert page.deadline_clamped is True
    assert deadline <= now - (TAIL + CAMPAIGNS.write_seconds) + 0.01
    # Control: positive remaining time is not clamped.
    assert (
        render_page(
            bake_parts(_DeadlineBaker()), _intent(CAMPAIGNS), _window(CAMPAIGNS)
        ).deadline_clamped
        is False
    )


# --- the certified bound (A5.1 I5) -----------------------------------------------------------


@dataclass
class _Inner:
    calls: list[Any] = field(default_factory=list[Any])

    def run(self, request: Any) -> QueryResult:
        self.calls.append(request)
        return cast("QueryResult", "rows")

    def run_series(self, request: Any) -> QueryResult:
        self.calls.append(request)
        return cast("QueryResult", "rows")


@dataclass
class _QueryingBaker:
    """Run `count` distinct fake requests, swallowing a refusal as a careless source might."""

    queries: BakeQueryCapture
    count: int

    def bake(self, intent: BakeIntent) -> BakeResult:
        for index in range(self.count):
            with suppress(InvariantError):
                self.queries.run(cast("Any", f"request-{index}"))
        return BakeUnavailable(intent, "fictional")


@pytest.fixture
def fake_request_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Key fake string requests directly (the real digest needs a bound request)."""
    import adapters.outbound.bake_sources as module  # noqa: PLC0415

    monkeypatch.setattr(module, "request_key", lambda request: f"query:{request}")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]


def _bounded(count: int) -> tuple[Any, BakeRosterRow]:
    queries = BakeQueryCapture(_Inner())
    row = BakeRosterRow("campaigns", "7d", 300.0, 16.6, 30, 3)
    return bake_parts(_QueryingBaker(queries, count), queries=queries), row


@pytest.mark.usefixtures("fake_request_keys")
def test_over_the_certified_bound_is_fatal_even_when_the_source_swallows_it() -> None:
    parts, row = _bounded(4)
    with pytest.raises(PageFatalError) as raised:
        render_page(parts, BakeIntent("campaigns", "7d"), _window(row))
    assert raised.value.code == BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND
    # Paired: at the bound the page renders and reports what it executed.
    at_bound, row = _bounded(3)
    page = render_page(at_bound, BakeIntent("campaigns", "7d"), _window(row))
    assert (page.executed.distinct, page.executed.over_bound) == (3, False)


def test_a_source_refusal_is_the_page_absence_only_where_the_entry_names_it() -> None:
    refusal = DomainError(ErrorCode("FICTIONAL", 2), "refused")
    page = render_page(
        bake_parts(_DeadlineBaker(error=refusal)),
        _intent(CAMPAIGNS),
        _window(CAMPAIGNS),
        absent_on=(DomainError,),
    )
    assert page.result == page_absence(_intent(CAMPAIGNS)) and page.refusal is refusal
    # Paired: without it (the legacy entry today) the refusal propagates.
    with pytest.raises(DomainError):
        render_page(
            bake_parts(_DeadlineBaker(error=refusal)), _intent(CAMPAIGNS), _window(CAMPAIGNS)
        )


# --- the process ledger (02 §3.6, R5-M1) -----------------------------------------------------


def _scripted(*values: float) -> Any:
    reads = iter(values)
    return lambda: next(reads)


def test_the_ledger_counts_entries_and_keeps_the_first_entry_as_the_baseline() -> None:
    ledger = ProcessBakeLedger(peak=_scripted(100.0, 140.0))
    first, second = ledger.enter(), ledger.enter()
    assert (first.process_bake_ordinal, first.rss_before_mb, first.process_baseline_mb) == (
        1,
        100.0,
        100.0,
    )
    assert (second.process_bake_ordinal, second.rss_before_mb, second.process_baseline_mb) == (
        2,
        140.0,
        100.0,
    )
    assert first.process_id == second.process_id and len(first.process_id) == 32
    # Control: another process (ledger) has its own identity and ordinal.
    other = ProcessBakeLedger(peak=_scripted(7.0)).enter()
    assert other.process_id != first.process_id and other.process_bake_ordinal == 1


def test_a_render_that_raises_still_advanced_the_ledger() -> None:
    ledger = ProcessBakeLedger(peak=_scripted(100.0, 140.0, 180.0))
    failing = _DeadlineBaker(error=DependencyError(ErrorCode("FICTIONAL", 3), "down"))
    with pytest.raises(DependencyError):
        render_page(bake_parts(failing, process=ledger), _intent(CAMPAIGNS), _window(CAMPAIGNS))
    page = render_page(
        bake_parts(_DeadlineBaker(), process=ledger), _intent(CAMPAIGNS), _window(CAMPAIGNS)
    )
    assert (page.entry.process_bake_ordinal, page.entry.process_baseline_mb) == (2, 100.0)
    # Paired: a fresh ledger whose first render publishes starts at ordinal 1.
    fresh = ProcessBakeLedger(peak=_scripted(50.0))
    first = render_page(
        bake_parts(_DeadlineBaker(), process=fresh), _intent(CAMPAIGNS), _window(CAMPAIGNS)
    )
    assert (first.entry.process_bake_ordinal, first.entry.process_baseline_mb) == (1, 50.0)


def test_stage_page_writes_through_the_entry_stage() -> None:
    written: list[object] = []

    def write(request: Any) -> str:
        written.append(request)
        return "published"

    request = cast("Any", object())
    assert stage_page(write, request, _window(CAMPAIGNS)) == "published"
    assert written == [request]
