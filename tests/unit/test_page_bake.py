# SPDX-License-Identifier: MIT-0
"""The one page execution both bake entries call (ADR-142, ADR-144): deadline, bound, ledger."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, cast

import pytest
from bake_application_support import bake_parts
from refusal_telemetry_support import LOGGER, capture

from adapters.jobs.preset_artifact_publisher.bake_results import (
    BakeResult,
    BakeUnavailable,
    BakeUnchanged,
)
from adapters.jobs.preset_artifact_publisher.isolated_refusals import note_isolated_refusal
from adapters.jobs.preset_artifact_publisher.page_bake import (
    PageFatalError,
    PageWindow,
    ProcessBakeLedger,
    RefusalLineage,
    classify_page_error,
    emit_isolated_refusals,
    page_absence,
    render_page,
    report_over_budget,
    stage_page,
)
from adapters.jobs.preset_artifact_publisher.transport import BakeIntent
from adapters.outbound.athena.deadline import BAKE_DEADLINE_UNSET, bake_statement_deadline
from adapters.outbound.athena.execution import QUERY_TIMED_OUT
from adapters.outbound.athena.query import BAKE_INVOCATION_WAIT
from adapters.outbound.bake_sources import BakeQueryCapture
from domain.operations.bake_refusals import (
    REFUSAL_SCOPE_INVALID,
    IsolatedRefusal,
    IsolatedRefusalReason,
    RefusalScope,
    group_refusals,
)
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
    ValidationError,
)
from ports.outbound.query import QueryResult

pytestmark = pytest.mark.unit

ROSTER = BakeRoster.from_contract()
AUDIENCES = ROSTER.row("audiences#last_complete_month")
CAMPAIGNS = ROSTER.row("campaigns#7d")
TAIL = 30.0
REMAINING_MS = 600_000


def _window(
    row: BakeRosterRow, remaining: int = REMAINING_MS, *, final: bool = False
) -> PageWindow:
    return PageWindow(lambda: remaining, row, TAIL, final=final)


def _intent(row: BakeRosterRow) -> BakeIntent:
    return BakeIntent(row.page, row.variant)


@dataclass
class _DeadlineBaker:
    """Read the statement deadline as a bake statement would, then return a result."""

    result: BakeResult | None = None
    error: Exception | None = None
    seen: list[tuple[float, float]] = field(default_factory=list[tuple[float, float]])
    notes: list[IsolatedRefusal] = field(default_factory=list[IsolatedRefusal])
    """Item or section refusals the source notes before it returns or raises."""

    def bake(self, intent: BakeIntent) -> BakeResult:
        self.seen.append((monotonic(), bake_statement_deadline()))
        for note in self.notes:
            note_isolated_refusal(
                note.reason,
                note.scope,
                note.count,
                section=cast("str", note.section),
                **cast("dict[str, Any]", dict(note.refusal_context)),
            )
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


_TIMED_OUT = DependencyError(
    QUERY_TIMED_OUT,
    "The statement took too long.",
    context={
        "wait_policy": "bake_invocation",
        "waited_seconds": 812.4,
        "wait_limit_seconds": 812.0,
    },
)


def test_a_source_refusal_is_the_page_absence_with_one_page_group_and_no_item_notes() -> None:
    refusal = DomainError(ErrorCode("FICTIONAL", 2), "refused")
    item = IsolatedRefusal(IsolatedRefusalReason.HISTORY_GAP, RefusalScope.ITEM, 3, section="x")
    baker = _DeadlineBaker(error=refusal, notes=[item])
    page = render_page(bake_parts(baker), _intent(CAMPAIGNS), _window(CAMPAIGNS))
    assert page.result == page_absence(_intent(CAMPAIGNS)) and page.cause is refusal
    assert [(g.scope, g.reason, g.count, dict(g.context)) for g in page.groups] == [
        (
            RefusalScope.PAGE,
            IsolatedRefusalReason.SOURCE_REFUSED,
            1,
            {"error_code": "ADLY-FICTIONAL-0002"},
        )
    ]
    # Paired: a typed page refusal passes through with its own figures.
    typed = IsolatedRefusal(
        IsolatedRefusalReason.ROWS_OVER_CEILING,
        RefusalScope.PAGE,
        rows_read=10_001,
        max_rows=10_000,
    )
    page = render_page(
        bake_parts(_DeadlineBaker(error=typed)), _intent(CAMPAIGNS), _window(CAMPAIGNS)
    )
    assert page.refusal is typed and page.groups == group_refusals((typed,))


def test_item_notes_publish_with_the_body_and_group_by_scope_and_reason() -> None:
    notes = [
        IsolatedRefusal(
            IsolatedRefusalReason.HISTORY_GAP, RefusalScope.ITEM, 2, section="campaign-audiences"
        ),
        IsolatedRefusal(
            IsolatedRefusalReason.HISTORY_GAP, RefusalScope.ITEM, 1, section="segment-ranking"
        ),
    ]
    audiences = BakeIntent("audiences", "last_complete_month")
    page = render_page(bake_parts(_DeadlineBaker(notes=notes)), audiences, _window(AUDIENCES))
    assert isinstance(page.result, BakeUnchanged) and page.refusal is None
    assert [(g.scope, g.reason, g.count) for g in page.groups] == [
        (RefusalScope.ITEM, IsolatedRefusalReason.HISTORY_GAP, 3)
    ]
    # Paired: a page whose source returns its designed absence reports no items.
    absent = _DeadlineBaker(
        result=BakeUnavailable(audiences, "audiences_materialization_unavailable"), notes=notes
    )
    assert render_page(bake_parts(absent), audiences, _window(AUDIENCES)).groups == ()


def test_a_scope_claim_the_page_does_not_declare_is_an_invariant() -> None:
    note = IsolatedRefusal(
        IsolatedRefusalReason.ROWS_OVER_CEILING, RefusalScope.SECTION, section="gateway-products"
    )
    with pytest.raises(InvariantError) as raised:
        render_page(
            bake_parts(_DeadlineBaker(notes=[note])), _intent(CAMPAIGNS), _window(CAMPAIGNS)
        )
    assert raised.value.code == REFUSAL_SCOPE_INVALID
    # A raised item refusal (items are noted, never raised) is a defect, not an absence.
    raised_item = IsolatedRefusal(IsolatedRefusalReason.HISTORY_GAP, RefusalScope.ITEM, section="x")
    with pytest.raises(IsolatedRefusal):
        render_page(
            bake_parts(_DeadlineBaker(error=raised_item)), _intent(CAMPAIGNS), _window(CAMPAIGNS)
        )


def test_a_dependency_error_propagates_before_the_final_receive_and_is_unavailable_on_it() -> None:
    down = DependencyError(ErrorCode("FICTIONAL", 4), "down")
    with pytest.raises(DependencyError):
        render_page(bake_parts(_DeadlineBaker(error=down)), _intent(CAMPAIGNS), _window(CAMPAIGNS))
    page = render_page(
        bake_parts(_DeadlineBaker(error=down)), _intent(CAMPAIGNS), _window(CAMPAIGNS, final=True)
    )
    assert page.result == page_absence(_intent(CAMPAIGNS))
    assert [(g.reason, dict(g.context)) for g in page.groups] == [
        (IsolatedRefusalReason.DEPENDENCY_UNAVAILABLE, {"error_code": "ADLY-FICTIONAL-0004"})
    ]


def test_a_bake_wait_timeout_is_statement_time_over_invocation_on_any_receive() -> None:
    page = render_page(
        bake_parts(_DeadlineBaker(error=_TIMED_OUT)), _intent(CAMPAIGNS), _window(CAMPAIGNS)
    )
    assert [(g.reason, dict(g.context)) for g in page.groups] == [
        (
            IsolatedRefusalReason.STATEMENT_TIME_OVER_INVOCATION,
            {"wait_limit_seconds": 812, "waited_seconds": 812},
        )
    ]
    # Paired: a timeout under another wait policy is any other dependency: it retries.
    interactive = DependencyError(QUERY_TIMED_OUT, "slow", context={"wait_policy": "interactive"})
    assert classify_page_error(interactive, final=False) is None
    with pytest.raises(DependencyError):
        render_page(
            bake_parts(_DeadlineBaker(error=interactive)), _intent(CAMPAIGNS), _window(CAMPAIGNS)
        )


def test_only_taxonomy_refusals_and_dependencies_are_classified() -> None:
    assert classify_page_error(ValidationError(ErrorCode("PLAN", 8), "x"), final=False) is not None
    assert classify_page_error(InvariantError(ErrorCode("FICTIONAL", 5), "x"), final=True) is None
    assert classify_page_error(ValueError("x"), final=True) is None


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


def test_stage_page_passes_the_window_and_catches_only_page_refusals() -> None:
    written: list[tuple[object, int, float]] = []

    def write(request: Any, *, remaining_ms: Any, tail_seconds: float) -> str:
        written.append((request, remaining_ms(), tail_seconds))
        return "published"

    request = cast("Any", object())
    assert stage_page(write, request, _window(CAMPAIGNS, remaining=1_234)) == "published"
    assert written == [(request, 1_234, TAIL)]
    deadline = IsolatedRefusal(
        IsolatedRefusalReason.BODIES_OVER_INVOCATION, RefusalScope.PAGE, written=3, bodies=9
    )
    # ADR-145: the stage draws the sibling stream as it writes, so a sibling the source renders
    # then refuses the page as it would at render: the page's absence, returned, not raised.
    over_cap = IsolatedRefusal(IsolatedRefusalReason.BODY_OVER_CAP, RefusalScope.PAGE, max_bytes=1)

    def refusing(refusal: IsolatedRefusal) -> Any:
        def stage(_request: Any, *, remaining_ms: Any, tail_seconds: float) -> str:
            del remaining_ms, tail_seconds
            raise refusal

        return stage

    assert stage_page(refusing(deadline), request, _window(CAMPAIGNS)) is deadline
    assert stage_page(refusing(over_cap), request, _window(CAMPAIGNS)) is over_cap
    # Paired: an item-scope refusal is a source defect when raised, and any other stage error
    # propagates.
    item = IsolatedRefusal(
        IsolatedRefusalReason.BODY_OVER_CAP, RefusalScope.ITEM, section="fictional", max_bytes=1
    )
    with pytest.raises(IsolatedRefusal):
        stage_page(refusing(item), request, _window(CAMPAIGNS))

    def failing(_request: Any, *, remaining_ms: Any, tail_seconds: float) -> str:
        del remaining_ms, tail_seconds
        raise DependencyError(ErrorCode("FICTIONAL", 6), "s3 down")

    with pytest.raises(DependencyError):
        stage_page(failing, request, _window(CAMPAIGNS))


# --- the one emitter (02 §4) ----------------------------------------------------------------


def test_each_group_emits_one_failed_span_one_line_and_one_datapoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = capture(monkeypatch)
    groups = group_refusals(
        (
            IsolatedRefusal(IsolatedRefusalReason.HISTORY_GAP, RefusalScope.ITEM, 200, section="s"),
            IsolatedRefusal(
                IsolatedRefusalReason.ROWS_OVER_CEILING,
                RefusalScope.SECTION,
                section="s",
                rows_read=10_001,
                max_rows=10_000,
            ),
        )
    )
    lineage = RefusalLineage("graph", "audiences", "last_complete_month", run_id="r", task_id="t")
    emit_isolated_refusals(LOGGER, groups, lineage, replayed=False, metric=telemetry.metric)
    assert telemetry.triples() == [
        ("item", "history_gap", 200),
        ("section", "rows_over_ceiling", 1),
    ]
    assert all(item["failed"] == "IsolatedRefusal" for item in telemetry.spans)
    assert telemetry.spans[1]["rows_read"] == 10_001 and "rows_read" not in telemetry.spans[0]
    assert telemetry.datapoints == [("data", 200), ("limit", 1)]
    first, second = telemetry.refusal_lines()
    # Exactly the allowlisted fields: the entry, the lineage, the page and the group; no other
    # identifier, and `count` is items (200 campaigns, one line).
    assert dict(first.details) == {
        "entry": "graph",
        "run_id": "r",
        "task_id": "t",
        "page": "audiences",
        "variant": "last_complete_month",
        "scope": "item",
        "refusal_class": "data",
        "count": 200,
        "replayed": False,
    }
    assert (first.outcome, first.severity.value, first.component) == (
        "history_gap",
        "warning",
        "page_bake",
    )
    # ADR-152: a limit names its operator action; a memory-sized one, the memory assumed.
    assert set(second.details) - set(first.details) == {
        "rows_read",
        "max_rows",
        "operator_action",
        "memory_mib",
    }
    assert second.details["memory_mib"] == 2048
    assert str(second.details["operator_action"]).startswith("raise the Baker memory")
    # Paired: no groups, nothing emitted.
    emit_isolated_refusals(LOGGER, (), lineage, replayed=False, metric=telemetry.metric)
    assert len(telemetry.spans) == len(telemetry.datapoints) == 2


def test_the_legacy_lineage_carries_the_execution_and_a_replay_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = capture(monkeypatch)
    groups = group_refusals(
        (
            IsolatedRefusal(
                IsolatedRefusalReason.PARTITIONS_OVER_BOUND,
                RefusalScope.PAGE,
                required_partitions=1_001,
                maximum_partitions=1_000,
            ),
        )
    )
    lineage = RefusalLineage("legacy", "reach", "last_complete_quarter", execution_id="e")
    emit_isolated_refusals(LOGGER, groups, lineage, replayed=True, metric=telemetry.metric)
    (line,) = telemetry.refusal_lines()
    assert {key: line.details[key] for key in ("entry", "execution_id", "replayed")} == {
        "entry": "legacy",
        "execution_id": "e",
        "replayed": True,
    }
    assert "run_id" not in line.details and telemetry.datapoints == [("limit", 1)]
    assert telemetry.spans[0]["replayed"] is True


def test_over_budget_reports_once_and_at_the_budget_reports_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = capture(monkeypatch)
    lineage = RefusalLineage("legacy", "campaigns", "7d", execution_id="e")
    assert report_over_budget(
        LOGGER,
        CAMPAIGNS.budget_seconds + 0.5,
        CAMPAIGNS,
        lineage,
        replayed=False,
        metric=telemetry.budget,
    )
    (line,) = telemetry.budget_lines()
    assert line.details["budget_seconds"] == CAMPAIGNS.budget_seconds and line.phase == "task"
    assert telemetry.over_budget == [1]
    # Paired: at the budget, nothing is reported.
    assert not report_over_budget(
        LOGGER,
        CAMPAIGNS.budget_seconds,
        CAMPAIGNS,
        lineage,
        replayed=False,
        metric=telemetry.budget,
    )
    assert telemetry.over_budget == [1] and len(telemetry.budget_lines()) == 1
