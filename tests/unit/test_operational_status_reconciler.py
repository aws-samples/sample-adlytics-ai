# SPDX-License-Identifier: MIT-0
"""Bounded reconciliation dispositions and descriptive baseline window folding."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from operational_status_support import (
    RecordingSink,
    flow_event,
    terminal_event,
    uuid_text,
)

from adapters.config import (
    operational_attempts_settings,
    operational_reconciler_settings,
    tracing_settings,
)
from domain.operations.generated_data_flows import SURFACES
from domain.operations.status import (
    MEASUREMENT_REFS,
    BaselineFoldRequest,
    BaselineWindow,
    OperationalDataFlowEvent,
    ReductionKind,
    initial_baseline,
)
from ports.outbound.operational_status import EventPage, PendingPage, ProjectionOutcome

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-073", "ADR-074")]

_ENV = {
    "ADL_OPERATIONAL_STATUS_TABLE": "unit-test-operational-status",
    "ADL_OPERATIONAL_STATUS_QUEUE_URL": "https://sqs.test.invalid/operational-status.fifo",
    "ADL_SEMANTIC_ATTEMPTS_TABLE": "unit-test-semantic-attempts",
    "AWS_DEFAULT_REGION": "us-east-1",
}

RUN_AT = datetime(2026, 8, 19, 12, 30, tzinfo=UTC)
WINDOW_START = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
MEASURED_SURFACE = "semantic_candidate_publication"
ACTIVE_MEASURED = frozenset(
    cast("str", surface["id"])
    for surface in SURFACES
    if surface["status"] == "active" and surface["measurements"]
)


@pytest.fixture
def reconciler_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import production composition under fictional module-level settings."""
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    operational_reconciler_settings.cache_clear()
    operational_attempts_settings.cache_clear()
    tracing_settings.cache_clear()
    return cast("Any", importlib.import_module("adapters.jobs.operational_status.reconciler"))


@dataclass
class _Store:
    """Record projector calls and answer them from configured per-surface state."""

    pending_events: tuple[OperationalDataFlowEvent, ...] = ()
    outcomes: dict[str, object] = field(default_factory=dict[str, object])
    contributions: dict[str, tuple[OperationalDataFlowEvent, ...]] = field(
        default_factory=dict[str, tuple[OperationalDataFlowEvent, ...]]
    )
    existing: dict[str, tuple[BaselineWindow, ...]] = field(
        default_factory=dict[str, tuple[BaselineWindow, ...]]
    )
    recorded: list[tuple[str, BaselineWindow]] = field(
        default_factory=list[tuple[str, BaselineWindow]]
    )
    pending_limits: list[int] = field(default_factory=list[int])
    contribution_calls: list[tuple[str, datetime, datetime]] = field(
        default_factory=list[tuple[str, datetime, datetime]]
    )

    def reduce(self, event: OperationalDataFlowEvent) -> ProjectionOutcome:
        outcome = self.outcomes[event.event_id]
        if isinstance(outcome, Exception):
            raise outcome
        return cast("ProjectionOutcome", outcome)

    def pending(self, limit: int) -> PendingPage:
        self.pending_limits.append(limit)
        return PendingPage(self.pending_events)

    def flow(self, flow_id: str) -> dict[str, object] | None:
        del flow_id
        return None

    def events(self, flow_id: str, cursor: str, limit: int) -> EventPage | None:
        del flow_id, cursor, limit
        return None

    def surface(self, surface: str, limit: int) -> tuple[dict[str, object], ...]:
        del surface, limit
        return ()

    def window_contributions(
        self, surface: str, window_start: datetime, window_end: datetime
    ) -> tuple[OperationalDataFlowEvent, ...]:
        self.contribution_calls.append((surface, window_start, window_end))
        return self.contributions.get(surface, ())

    def baselines(self, surface: str, measurement_ref: str) -> tuple[BaselineWindow, ...]:
        del measurement_ref
        return self.existing.get(surface, ())

    def record_baseline(self, surface: str, window: BaselineWindow) -> bool:
        self.recorded.append((surface, window))
        return True


def _existing_window(reference: tuple[str, str], sample_count: int) -> BaselineWindow:
    measurement_ref, measurement_version = reference
    return initial_baseline(
        BaselineFoldRequest(
            measurement_ref=measurement_ref,
            measurement_version=measurement_version,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            finalized_at=WINDOW_END,
            sample_count=sample_count,
        )
    )


def _settled_baselines() -> dict[str, tuple[BaselineWindow, ...]]:
    """Every active measured surface already holds a zero-count window for the hour."""
    return {
        surface: tuple(_existing_window(reference, 0) for reference in MEASUREMENT_REFS[surface])
        for surface in ACTIVE_MEASURED
    }


def _reconciler(module: Any, store: _Store, sink: RecordingSink) -> Any:
    return module.Reconciler(store=store, sink=sink, attempt_authority=cast("Any", None))


def _in_window(**overrides: Any) -> OperationalDataFlowEvent:
    values: dict[str, Any] = {
        "clock_started_at": WINDOW_START,
        "occurred_at": WINDOW_START.replace(minute=15),
        **overrides,
    }
    return terminal_event(**values)


class TestRun:
    def test_dispositions_are_counted_and_parked_events_are_readmitted(
        self, reconciler_module: Any
    ) -> None:
        applied = flow_event(event_id=uuid_text(11))
        parked = flow_event(event_id=uuid_text(12), flow_id=uuid_text(22))
        failed = flow_event(event_id=uuid_text(13), flow_id=uuid_text(23))
        store = _Store(
            pending_events=(applied, parked, failed),
            outcomes={
                applied.event_id: ProjectionOutcome(ReductionKind.APPLY, applied_count=1),
                parked.event_id: ProjectionOutcome(ReductionKind.PARK),
                failed.event_id: RuntimeError("projection unavailable"),
            },
            existing=_settled_baselines(),
        )
        sink = RecordingSink()

        counts = _reconciler(reconciler_module, store, sink).run(RUN_AT)

        assert counts == reconciler_module.ReconciliationCounts(
            queried=3, applied=1, parked=1, failed=1, windows_written=0
        )
        assert sink.sent == [parked]
        assert store.pending_limits == [10]

    def test_already_applied_counts_as_applied_without_readmission(
        self, reconciler_module: Any
    ) -> None:
        event = flow_event(event_id=uuid_text(11))
        store = _Store(
            pending_events=(event,),
            outcomes={event.event_id: ProjectionOutcome(ReductionKind.ALREADY_APPLIED)},
            existing=_settled_baselines(),
        )
        sink = RecordingSink()

        counts = _reconciler(reconciler_module, store, sink).run(RUN_AT)

        assert (counts.applied, counts.parked, counts.failed) == (1, 0, 0)
        assert sink.sent == []


class TestCloseWindows:
    def test_the_first_fold_conditionally_creates_revision_one_with_the_hour_count(
        self, reconciler_module: Any
    ) -> None:
        store = _Store(
            contributions={
                MEASURED_SURFACE: (
                    _in_window(event_id=uuid_text(11)),
                    _in_window(event_id=uuid_text(12), flow_id=uuid_text(22)),
                    flow_event(
                        event_id=uuid_text(13),
                        flow_id=uuid_text(23),
                        clock_started_at=WINDOW_START,
                        occurred_at=WINDOW_START.replace(minute=20),
                    ),
                )
            }
        )

        written = _reconciler(reconciler_module, store, RecordingSink()).close_windows(RUN_AT)

        assert written == sum(len(MEASUREMENT_REFS[surface]) for surface in ACTIVE_MEASURED)
        by_surface = dict(store.recorded)
        window = by_surface[MEASURED_SURFACE]
        assert window.revision == 1
        assert window.predecessor_window_id is None
        assert window.measurement_ref == "semantic_candidate_decision_latency"
        assert window.sample_count == 2
        assert (window.window_start, window.window_end) == (WINDOW_START, WINDOW_END)
        assert window.finalized_at == RUN_AT

    def test_a_rerun_with_an_unchanged_count_writes_no_window(self, reconciler_module: Any) -> None:
        store = _Store(existing=_settled_baselines())

        written = _reconciler(reconciler_module, store, RecordingSink()).close_windows(RUN_AT)

        assert written == 0
        assert store.recorded == []

    def test_a_grown_recount_appends_one_correction_revision(self, reconciler_module: Any) -> None:
        existing = _settled_baselines()
        prior = _existing_window(MEASUREMENT_REFS[MEASURED_SURFACE][0], 1)
        existing[MEASURED_SURFACE] = (prior,)
        store = _Store(
            contributions={
                MEASURED_SURFACE: (
                    _in_window(event_id=uuid_text(11)),
                    _in_window(event_id=uuid_text(12), flow_id=uuid_text(22)),
                    _in_window(event_id=uuid_text(13), flow_id=uuid_text(23)),
                )
            },
            existing=existing,
        )

        written = _reconciler(reconciler_module, store, RecordingSink()).close_windows(RUN_AT)

        assert written == 1
        ((surface, corrected),) = store.recorded
        assert surface == MEASURED_SURFACE
        assert corrected.revision == 2
        assert corrected.predecessor_window_id == prior.window_id
        assert corrected.sample_count == 3
        assert corrected.late_sample_count == 2
        assert corrected.finalized_at == RUN_AT

    def test_only_active_surfaces_with_measurements_fold_the_closed_hour(
        self, reconciler_module: Any
    ) -> None:
        store = _Store(existing=_settled_baselines())

        _reconciler(reconciler_module, store, RecordingSink()).close_windows(RUN_AT)

        assert {surface for surface, _, _ in store.contribution_calls} == ACTIVE_MEASURED
        assert {(start, end) for _, start, end in store.contribution_calls} == {
            (WINDOW_START, WINDOW_END)
        }
