# SPDX-License-Identifier: MIT-0
"""SelectInput's admission rules over days: CURRENT, last_attempt, operator retry, weekly slot.

A wrong rule re-runs and re-bills an input already shown, publishes a restatement a week late,
loses a week after a missed Thursday (R7-M2), or lets an operator retry skip the no-op.
The world below records each outcome the way the publisher does, through the domain's own
`retry_disposition()`, `next_pointer()` and `rollback_pointer()`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from adapters.config import DemandForecastInputSettings
from adapters.jobs.demand_forecast_input.handler import (
    InputBuilder,
    InputBuildFailedError,
    SelectRequest,
    select_input,
    select_request,
)
from adapters.jobs.preset_artifact_publisher.retail_source import RetailProductHierarchyResult
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.demand_forecast_policy import load_policy
from adapters.outbound.telemetry.demand_forecast_metrics import Datapoint
from domain.forecasting.publish import (
    CurrentPointer,
    GenerationRecord,
    LastAttempt,
    OutcomeRecord,
    PointerAdvanceRequest,
    PointerDecision,
    PublishedGeneration,
    ReceiptRecord,
    RollbackRequest,
    RollbackState,
    RunOutcome,
    ShardAttempt,
    generation_id,
    next_pointer,
    retry_disposition,
    rollback_pointer,
)
from domain.forecasting.selection import (
    pointer_consumes_slot,
    previous_slot_date,
    scheduled_run_id,
)
from domain.shared_kernel.values import DateRange
from ports.outbound.forecast_history_selection import (
    ForecastHistorySelectionRequest,
    ForecastHistorySelectionResult,
    PinnedContractHistory,
)
from ports.outbound.publication_selection import PinnedPublication
from ports.outbound.weekly_series_source import WeeklySeriesSourcePort

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
POLICY = load_policy(ROOT / "contracts/forecasting/demand-forecast.yaml")
BRIDGE = load_commerce_identity_bridge(ROOT)
INPUT_END = date(2030, 1, 5)
"""A Saturday; every other date is derived from it (lesson L1)."""
THURSDAY = INPUT_END + timedelta(days=5)
"""The first weekly slot after `INPUT_END` (policy `admission_weekday: thursday`)."""
HISTORY_START = INPUT_END - timedelta(days=800)
SCHEDULER_EXECUTION_ID = "0123abcd4567ef89"
SETTINGS = DemandForecastInputSettings(
    semantic_publications_table="publications",
    semantic_v2_bucket="semantic",
    athena_batch_workgroup="batch",
    semantic_v2_current_database="current",
    processing_bucket="adlytics-dev-demand-forecast",
    forecast_table="adlytics-dev-demand-forecast",
    marketplace_key="amazon_us",
    model_artifact_digest="1" * 64,
    processor_code_identity="2" * 64,
    lock_digest="3" * 64,
    instance_type="ml.m5.xlarge",
    max_runtime_seconds=3600,
)


def _history(
    contract: str, days: Sequence[date], versions: Mapping[date, int]
) -> PinnedContractHistory:
    """One daily pin per day; the covered intervals are the runs of consecutive pinned days."""
    pins: list[PinnedPublication] = []
    for day in days:
        version = versions.get(day, 1)
        name = f"{contract}-{day}-v{version}"
        pins.append(PinnedPublication(name, name, version, f"{version:x}" * 64))
    covered: list[DateRange] = []
    for day in days:
        if covered and covered[-1].end + timedelta(days=1) == day:
            covered[-1] = DateRange(covered[-1].start, day)
        else:
            covered.append(DateRange(day, day))
    return PinnedContractHistory(
        contract_id=contract,
        pins=tuple(pins),
        buckets=tuple(DateRange(d, d) for d in days),
        covered=tuple(covered),
        newest_published_at=datetime.combine(days[-1], time(6), UTC),
    )


def _span(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


@dataclass
class Selection:
    """The CURRENT publications the selector returns; a test restates or extends them."""

    sales_end: date = INPUT_END + timedelta(days=2)
    sales_start: date = HISTORY_START
    sales_versions: dict[date, int] = field(default_factory=dict[date, int])
    sales_holes: set[date] = field(default_factory=set[date])
    products_version: int = 1

    def restate(self, day: date) -> None:
        self.sales_versions[day] = self.sales_versions.get(day, 1) + 1

    def select(self, request: ForecastHistorySelectionRequest) -> ForecastHistorySelectionResult:
        del request
        products_day = INPUT_END - timedelta(days=3)
        sales = [d for d in _span(self.sales_start, self.sales_end) if d not in self.sales_holes]
        return ForecastHistorySelectionResult(
            sales=_history("vendor-sales-by-asin", sales, self.sales_versions),
            traffic=_history("vendor-traffic-by-asin", _span(HISTORY_START, INPUT_END), {}),
            products=_history(
                "vendor-product-hierarchy", [products_day], {products_day: self.products_version}
            ),
        )


@dataclass
class NoTitles:
    def hierarchy(self, window: DateRange) -> RetailProductHierarchyResult | None:
        del window


@dataclass
class NoObjects:
    def list_sizes(self, prefix: str) -> dict[str, int]:
        raise AssertionError(f"select never lists {prefix}")

    def sha256(self, key: str) -> str:
        raise AssertionError(f"select never reads {key}")

    def tail(self, key: str, size: int) -> bytes:
        raise AssertionError(f"select never reads {key} ({size})")

    def put(self, key: str, body: bytes) -> None:
        raise AssertionError(f"select never writes {key} ({len(body)})")


def _no_weekly(remaining: object) -> WeeklySeriesSourcePort:
    raise AssertionError(f"select never builds ({remaining})")


@dataclass
class Table:
    """The set's `CURRENT` and receipts as the publisher leaves them, and every read."""

    set_id: str = ""
    current: CurrentPointer | None = None
    receipts: dict[str, ReceiptRecord] = field(default_factory=dict[str, ReceiptRecord])
    reads: list[str] = field(default_factory=list[str])

    def get_current(self, set_id: str) -> CurrentPointer | None:
        assert set_id == self.set_id
        self.reads.append("CURRENT")
        return self.current

    def get_receipt(self, set_id: str, sk: str) -> ReceiptRecord | None:
        assert set_id == self.set_id
        self.reads.append(sk)
        return self.receipts.get(sk)


@dataclass(frozen=True)
class OperatorStart:
    """An operator start's `RETRY_FAILED` and `REASON`."""

    retry_failed: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class Ending:
    """One outcome the publisher records into `last_attempt`."""

    run_id: str
    attempt_id: str | None
    fingerprint: str | None
    outcome: RunOutcome
    category: str | None


@dataclass
class Metrics:
    points: list[Datapoint] = field(default_factory=list[Datapoint])

    def emit(self, points: Sequence[Datapoint], timestamp: datetime) -> None:
        assert timestamp.tzinfo is not None
        self.points.extend(points)


class World:
    """One forecast set across days: daily checks, operator starts and recorded outcomes."""

    def __init__(self, selection: Selection | None = None) -> None:
        self.selection = selection or Selection()
        self.table = Table()
        self.metrics = Metrics()
        self.clock = datetime.combine(INPUT_END, time(13), UTC)
        self.builder = InputBuilder(
            settings=SETTINGS,
            policy=POLICY,
            bridge=BRIDGE,
            selection=self.selection,
            titles=NoTitles(),
            weekly=_no_weekly,
            objects=NoObjects(),
            store=self.table,
            metrics=self.metrics,
            now=lambda: self.clock,
        )
        self.table.set_id = self.builder.set_id
        self.published: dict[str, tuple[PublishedGeneration, str]] = {}
        self.operator_starts = 0

    def check(self, day: date, operator: OperatorStart | None = None) -> dict[str, object]:
        """Run `select` at 13:00 UTC on `day`; a no-op or hold writes its `#select` receipt."""
        self.clock = datetime.combine(day, time(13), UTC)
        self.table.reads.clear()
        if operator is not None:
            self.operator_starts += 1
            name = f"{self.builder.set_id}-{day.isoformat()}-op-{self.operator_starts}"
            request = SelectRequest(name, None, operator.retry_failed, operator.reason)
        else:
            request = SelectRequest(SCHEDULER_EXECUTION_ID, self.clock)
        result = select_input(self.builder, request)
        if result["outcome"] != "run":
            sk = f"RUN#{result['run_id']}#r0#select"
            self.table.receipts[sk] = ReceiptRecord(sk, RunOutcome(result["outcome"]), None, None)
        return result

    def end(
        self, result: Mapping[str, object], outcome: RunOutcome, category: str | None = None
    ) -> None:
        """Record a run's terminal outcome as the publisher's commit or `record` does."""
        assert result["outcome"] == "run"
        run_id, fingerprint = str(result["run_id"]), str(result["run_fingerprint"])
        current = self.table.current
        generation = None
        if outcome is RunOutcome.PUBLISHED:
            generation = PublishedGeneration(
                generation_id(run_id, [ShardAttempt(0, "r0a0")]),
                date.fromisoformat(str(result["input_end"])),
                fingerprint,
                self.clock,
            )
        attempt = self._attempt(Ending(run_id, "r0a0", fingerprint, outcome, category))
        fence = result["expected_fence"]
        assert isinstance(fence, int)
        advance = next_pointer(PointerAdvanceRequest(current, fence, attempt, generation))
        assert advance.decision is PointerDecision.ADVANCE
        self.table.current = advance.pointer
        if generation is not None:
            sk = f"RUN#{run_id}#r0#commit"
            self.table.receipts[sk] = ReceiptRecord(
                sk, RunOutcome.PUBLISHED, generation.generation_id, self.clock
            )
            self.published[generation.generation_id] = (generation, sk)

    def select_fails(self, day: date) -> None:
        """Record a scheduled `SelectInput` failure as its Catch does: no fingerprint."""
        self.clock = datetime.combine(day, time(13), UTC)
        run_id = scheduled_run_id(self.builder.set_id, day)
        attempt = self._attempt(
            Ending(run_id, None, None, RunOutcome.FAILED, "input_build:Unhandled")
        )
        current = self.table.current
        fence = 0 if current is None else current.fence
        advance = next_pointer(PointerAdvanceRequest(current, fence, attempt))
        self.table.current = advance.pointer
        sk = f"RUN#{run_id}#r0#select"
        self.table.receipts[sk] = ReceiptRecord(sk, RunOutcome.FAILED, None, None)

    def rollback(self, day: date, target: str) -> None:
        self.clock = datetime.combine(day, time(15), UTC)
        generation, sk = self.published[target]
        current = self.table.current
        assert current is not None
        plan = rollback_pointer(
            RollbackRequest(self.builder.set_id, target, current.fence, "bad model"),
            RollbackState(
                current,
                _record(self.builder.set_id, generation, sk),
                self.table.receipts[sk],
                f"{self.builder.set_id}-{day.isoformat()}-rb-1",
                self.clock,
            ),
        )
        self.table.current = plan.pointer

    def _attempt(self, ending: Ending) -> LastAttempt:
        current = self.table.current
        decision = retry_disposition(
            OutcomeRecord(
                outcome=ending.outcome,
                failure_category=ending.category,
                run_fingerprint=ending.fingerprint,
                previous=None if current is None else current.last_attempt,
                policy=POLICY.policy.retry,
            )
        )
        return LastAttempt(
            run_id=ending.run_id,
            attempt_id=ending.attempt_id,
            run_fingerprint=ending.fingerprint,
            outcome=ending.outcome,
            failure_category=decision.failure_category,
            retry_disposition=decision.disposition,
            transient_retries=decision.transient_retries,
            at=self.clock,
        )

    def publish_by_operator(self, day: date) -> dict[str, object]:
        result = self.check(day, OperatorStart())
        self.end(result, RunOutcome.PUBLISHED)
        return result

    def publish_on_slot(self, day: date) -> dict[str, object]:
        result = self.check(day)
        assert (result["outcome"], result["basis"]) == ("run", "weekly_slot")
        self.end(result, RunOutcome.PUBLISHED)
        return result


def _record(set_id: str, generation: PublishedGeneration, sk: str) -> GenerationRecord:
    return GenerationRecord(
        generation.generation_id, set_id, sk, generation.input_end, generation.run_fingerprint
    )


def _decided(result: Mapping[str, object]) -> tuple[object, object, object]:
    return result["outcome"], result["category"], result.get("basis")


def _days(first: date, count: int) -> list[date]:
    return [first + timedelta(days=n) for n in range(count)]


# ---- days alone, new days, restatements and product publications (R5-01, R4-02) ----


@pytest.mark.parametrize("lag", [3, 8, 10, 40, 272])
def test_days_alone_never_change_the_fingerprint_so_every_lag_no_ops(lag: int) -> None:
    world = World()
    world.publish_by_operator(INPUT_END + timedelta(days=1))
    result = world.check(INPUT_END + timedelta(days=lag))
    assert _decided(result) == ("no_op_same_input", None, "same_as_current")
    assert world.table.current is not None
    assert result["run_fingerprint"] == world.table.current.run_fingerprint


def test_a_lag_of_273_days_holds_input_end_too_old_before_comparing() -> None:
    world = World()
    world.publish_by_operator(INPUT_END + timedelta(days=1))
    result = world.check(INPUT_END + timedelta(days=273))
    assert _decided(result) == ("held_input_not_ready", "input_end_too_old", None)
    assert "run_fingerprint" not in result


def test_no_complete_week_holds_before_the_fingerprint_is_compared() -> None:
    world = World(Selection(sales_start=INPUT_END - timedelta(days=5)))
    result = world.check(THURSDAY)
    assert _decided(result) == ("held_input_not_ready", "no_complete_week", None)
    assert "run_fingerprint" not in result


def test_a_new_day_after_input_end_mid_week_no_ops() -> None:
    world = World()
    world.publish_by_operator(INPUT_END + timedelta(days=1))
    world.selection.sales_end = INPUT_END + timedelta(days=4)
    assert _decided(world.check(THURSDAY)) == ("no_op_same_input", None, "same_as_current")


@pytest.mark.parametrize("days_before_input_end", [0, 700])
def test_a_restated_day_at_or_before_input_end_runs_on_the_slot(
    days_before_input_end: int,
) -> None:
    world = World()
    first = world.publish_by_operator(INPUT_END + timedelta(days=1))
    world.selection.restate(INPUT_END - timedelta(days=days_before_input_end))
    result = world.check(THURSDAY)
    assert _decided(result) == ("run", None, "weekly_slot")
    assert result["run_fingerprint"] != first["run_fingerprint"]
    assert result["run_id"] == f"{world.builder.set_id}-2030-01-10"


def test_a_new_product_publication_alone_no_ops() -> None:
    world = World()
    world.publish_by_operator(INPUT_END + timedelta(days=1))
    world.selection.products_version = 2
    assert _decided(world.check(THURSDAY)) == ("no_op_same_input", None, "same_as_current")


# ---- last_attempt, retry counting and operator retry (§6d step 3, R3-02) ----


def test_a_transient_failure_reruns_three_times_on_any_day_then_suppresses() -> None:
    world = World()
    world.publish_by_operator(INPUT_END + timedelta(days=1))
    world.selection.restate(INPUT_END - timedelta(days=10))
    bases: list[tuple[object, object, object]] = []
    for day in _days(THURSDAY, 4):
        result = world.check(day)
        bases.append(_decided(result))
        world.end(result, RunOutcome.FAILED, "input_build:athena_timeout")
    assert bases == [
        ("run", None, "weekly_slot"),
        ("run", None, "last_attempt_retry"),
        ("run", None, "last_attempt_retry"),
        ("run", None, "last_attempt_retry"),
    ]
    assert world.table.current is not None
    last = world.table.current.last_attempt
    assert last is not None
    assert (last.failure_category, last.retry_disposition.value, last.transient_retries) == (
        "retry_exhausted",
        "suppress",
        3,
    )
    after = world.check(THURSDAY + timedelta(days=4))
    assert _decided(after) == ("no_op_same_input", None, "last_attempt_suppressed")


def test_a_larger_runtime_after_a_timeout_is_a_new_fingerprint_that_runs_on_the_slot() -> None:
    world = World()
    world.publish_by_operator(INPUT_END + timedelta(days=1))
    world.selection.restate(INPUT_END - timedelta(days=10))
    failed = world.check(THURSDAY)
    world.end(failed, RunOutcome.FAILED, "job:States.Timeout")
    longer = replace(SETTINGS, max_runtime_seconds=SETTINGS.max_runtime_seconds * 2)
    world.builder = replace(world.builder, settings=longer)
    result = world.check(THURSDAY + timedelta(days=7))
    assert _decided(result) == ("run", None, "weekly_slot")
    assert result["run_fingerprint"] != failed["run_fingerprint"]


def test_a_deterministic_failure_no_ops_daily_until_an_operator_retry_with_a_reason() -> None:
    world = World()
    world.publish_by_operator(INPUT_END + timedelta(days=1))
    world.selection.restate(INPUT_END - timedelta(days=10))
    world.end(world.check(THURSDAY), RunOutcome.FAILED, "job:INPUT_SCHEMA_MISMATCH")
    friday = THURSDAY + timedelta(days=1)
    assert _decided(world.check(friday)) == ("no_op_same_input", None, "last_attempt_suppressed")
    plain = world.check(friday, OperatorStart())
    assert _decided(plain) == ("no_op_same_input", None, "last_attempt_suppressed")
    retried = world.check(friday, OperatorStart(retry_failed=True, reason="schema fixed"))
    assert _decided(retried) == ("run", None, "operator_start")
    assert retried["operator_reason"] == "schema fixed"
    world.end(retried, RunOutcome.PUBLISHED)
    assert world.table.current is not None
    assert world.table.current.run_fingerprint == retried["run_fingerprint"]


def test_an_operator_retry_never_skips_the_no_op_against_current() -> None:
    world = World()
    world.publish_by_operator(INPUT_END + timedelta(days=1))
    result = world.check(THURSDAY, OperatorStart(retry_failed=True, reason="force"))
    assert _decided(result) == ("no_op_same_input", None, "same_as_current")


def test_a_rollback_then_two_daily_checks_no_op_and_leave_last_attempt_unchanged() -> None:
    world = World()
    first = world.publish_by_operator(INPUT_END + timedelta(days=1))
    world.selection.restate(INPUT_END - timedelta(days=10))
    world.publish_on_slot(THURSDAY)
    g1 = generation_id(str(first["run_id"]), [ShardAttempt(0, "r0a0")])
    world.rollback(THURSDAY + timedelta(days=1), g1)
    rolled = world.table.current
    for day in _days(THURSDAY + timedelta(days=2), 2):
        result = world.check(day)
        assert _decided(result) == ("no_op_same_input", None, "last_attempt_suppressed")
        assert world.table.current == rolled


def test_a_held_quality_then_two_daily_checks_no_op_and_leave_last_attempt_unchanged() -> None:
    world = World()
    world.publish_by_operator(INPUT_END + timedelta(days=1))
    world.selection.restate(INPUT_END - timedelta(days=10))
    world.end(world.check(THURSDAY), RunOutcome.HELD_QUALITY, "target_empty")
    held = world.table.current
    for day in _days(THURSDAY + timedelta(days=1), 2):
        result = world.check(day)
        assert _decided(result) == ("no_op_same_input", None, "last_attempt_suppressed")
        assert world.table.current == held


# ---- the weekly slot (round 7, R5C2-02) ----


def test_restatements_monday_to_wednesday_hold_and_thursday_runs_the_latest_once() -> None:
    world = World()
    world.publish_on_slot(THURSDAY)
    held: list[object] = []
    for day in _days(THURSDAY + timedelta(days=4), 3):
        world.selection.restate(day - timedelta(days=30))
        result = world.check(day)
        assert _decided(result) == ("held_input_not_ready", "weekly_slot_not_due", "slot_not_due")
        held.append(result["run_fingerprint"])
    slot = world.check(THURSDAY + timedelta(days=7))
    assert _decided(slot) == ("run", None, "weekly_slot")
    assert slot["run_fingerprint"] == held[-1]
    assert len(set(held)) == 3


def test_a_friday_change_waits_for_the_next_thursday() -> None:
    world = World()
    world.publish_on_slot(THURSDAY)
    world.selection.restate(INPUT_END - timedelta(days=10))
    waiting = [_decided(world.check(day)) for day in _days(THURSDAY + timedelta(days=1), 6)]
    assert waiting == [("held_input_not_ready", "weekly_slot_not_due", "slot_not_due")] * 6
    assert _decided(world.check(THURSDAY + timedelta(days=7))) == ("run", None, "weekly_slot")


def test_an_operator_start_runs_a_new_fingerprint_on_monday() -> None:
    world = World()
    world.publish_on_slot(THURSDAY)
    monday = THURSDAY + timedelta(days=4)
    world.selection.restate(INPUT_END - timedelta(days=10))
    assert _decided(world.check(monday))[1] == "weekly_slot_not_due"
    assert _decided(world.check(monday, OperatorStart())) == ("run", None, "operator_start")


def test_a_five_thursday_month_with_weekly_restatements_starts_five_scheduled_runs() -> None:
    world = World()
    world.publish_on_slot(THURSDAY)
    runs: list[date] = []
    for day in _days(THURSDAY + timedelta(days=1), 35):
        if day.weekday() == 0:
            world.selection.restate(day - timedelta(days=60))
        result = world.check(day)
        if result["outcome"] == "run":
            runs.append(day)
            world.end(result, RunOutcome.PUBLISHED)
    assert runs == [THURSDAY + timedelta(days=7 * n) for n in range(1, 6)]


# ---- OQ-S2-1: what consumes the slot, and the R7-M2 late admission ----


def test_slot_consumed_by_the_slot_runs_select_receipt() -> None:
    world = World()
    world.publish_by_operator(INPUT_END + timedelta(days=1))
    assert _decided(world.check(THURSDAY))[0] == "no_op_same_input"
    world.selection.restate(INPUT_END - timedelta(days=10))
    result = world.check(THURSDAY + timedelta(days=1))
    assert _decided(result) == ("held_input_not_ready", "weekly_slot_not_due", "slot_not_due")
    slot_run = f"{world.builder.set_id}-2030-01-10"
    assert world.table.reads == ["CURRENT", f"RUN#{slot_run}#r0#select"]


def test_slot_consumed_by_current_published_by_the_slot_run() -> None:
    world = World()
    world.publish_on_slot(THURSDAY)
    friday = THURSDAY + timedelta(days=1)
    world.selection.restate(INPUT_END - timedelta(days=10))
    started = world.check(friday, OperatorStart())
    world.end(started, RunOutcome.FAILED, "input_build:athena_timeout")
    world.selection.restate(INPUT_END - timedelta(days=20))
    result = world.check(friday + timedelta(days=1))
    assert _decided(result) == ("held_input_not_ready", "weekly_slot_not_due", "slot_not_due")
    assert world.table.reads == ["CURRENT"]


def test_slot_consumed_by_the_slot_runs_last_attempt_with_a_fingerprint() -> None:
    world = World()
    world.publish_by_operator(INPUT_END + timedelta(days=1))
    world.selection.restate(INPUT_END - timedelta(days=10))
    world.end(world.check(THURSDAY), RunOutcome.FAILED, "input_build:athena_timeout")
    world.selection.restate(INPUT_END - timedelta(days=20))
    result = world.check(THURSDAY + timedelta(days=1))
    assert _decided(result) == ("held_input_not_ready", "weekly_slot_not_due", "slot_not_due")
    assert world.table.reads == ["CURRENT"]


def test_a_thursday_select_failure_admits_the_new_fingerprint_on_friday() -> None:
    world = World()
    world.publish_by_operator(INPUT_END + timedelta(days=1))
    world.selection.restate(INPUT_END - timedelta(days=10))
    world.select_fails(THURSDAY)
    result = world.check(THURSDAY + timedelta(days=1))
    assert _decided(result) == ("run", None, "slot_missed")


def test_no_thursday_start_admits_the_new_fingerprint_on_friday() -> None:
    world = World()
    world.publish_by_operator(INPUT_END + timedelta(days=1))
    world.selection.restate(INPUT_END - timedelta(days=10))
    result = world.check(THURSDAY + timedelta(days=1))
    assert _decided(result) == ("run", None, "slot_missed")
    slot_run = f"{world.builder.set_id}-2030-01-10"
    assert world.table.reads == ["CURRENT", f"RUN#{slot_run}#r0#select"]


def test_only_the_slot_runs_own_generation_consumes_the_slot() -> None:
    slot_run = "a1b2c3d4e5f6-2030-01-10"
    digest = "0123456789ab"

    def published_by(run_id: str) -> CurrentPointer:
        return CurrentPointer(
            fence=1,
            generation_id=f"{run_id}-{digest}",
            input_end=INPUT_END,
            published_at=datetime.combine(THURSDAY, time(14), UTC),
            run_fingerprint="f" * 64,
        )

    assert pointer_consumes_slot(published_by(slot_run), slot_run)
    assert not pointer_consumes_slot(published_by(f"{slot_run}-op-1"), slot_run)
    assert not pointer_consumes_slot(None, slot_run)


def test_the_previous_slot_is_strictly_before_the_check_date() -> None:
    assert previous_slot_date(THURSDAY, "thursday") == date(2030, 1, 3)
    assert previous_slot_date(THURSDAY + timedelta(days=1), "thursday") == date(2030, 1, 10)


# ---- the request: scheduled time, operator name and REASON ----


def test_a_scheduled_input_never_carries_retry_failed() -> None:
    at = datetime.combine(THURSDAY, time(13), UTC)
    with pytest.raises(InputBuildFailedError, match=r"^REQUEST_INVALID$"):
        SelectRequest(SCHEDULER_EXECUTION_ID, at, retry_failed=True, operator_reason="x")


@pytest.mark.parametrize("reason", ["", "x" * 201, "two\nlines"])
def test_an_operator_reason_outside_1_to_200_printable_characters_is_refused(reason: str) -> None:
    with pytest.raises(InputBuildFailedError, match=r"^OPERATOR_REASON_INVALID$"):
        SelectRequest("a1b2c3d4e5f6-2030-01-10-op-1", None, True, reason)


def test_an_operator_retry_without_a_reason_is_refused_and_200_characters_pass() -> None:
    with pytest.raises(InputBuildFailedError, match=r"^OPERATOR_REASON_INVALID$"):
        SelectRequest("a1b2c3d4e5f6-2030-01-10-op-1", None, True, None)
    accepted = SelectRequest("a1b2c3d4e5f6-2030-01-10-op-1", None, True, "x" * 200)
    assert accepted.operator_reason == "x" * 200


@pytest.mark.parametrize(
    "name", ["a1b2c3d4e5f6-2030-01-10", "a1b2c3d4e5f6-2030-01-10-op-0", "manual-run"]
)
def test_an_operator_start_not_named_with_the_op_suffix_is_refused(name: str) -> None:
    with pytest.raises(InputBuildFailedError, match=r"^REQUEST_INVALID$"):
        SelectRequest(name)


def test_an_operator_start_named_for_another_set_is_refused() -> None:
    world = World()
    other = "0" * 12 if world.builder.set_id != "0" * 12 else "1" * 12
    with pytest.raises(InputBuildFailedError, match=r"^REQUEST_INVALID$"):
        select_input(world.builder, SelectRequest(f"{other}-2030-01-10-op-1"))


def test_the_check_date_and_run_id_come_from_the_scheduled_time_in_utc() -> None:
    world = World()
    world.selection.restate(INPUT_END - timedelta(days=10))
    world.clock = datetime.combine(THURSDAY + timedelta(days=1), time(1), UTC)
    event = {"execution_name": SCHEDULER_EXECUTION_ID, "scheduled_time": "2030-01-10T13:00:00Z"}
    result = select_input(world.builder, select_request(event))
    assert (result["check_date"], result["trigger"]) == ("2030-01-10", "scheduled")
    assert result["run_id"] == f"{world.builder.set_id}-2030-01-10"
    assert _decided(result) == ("run", None, "weekly_slot")


def test_a_malformed_scheduled_time_is_refused() -> None:
    event = {"execution_name": SCHEDULER_EXECUTION_ID, "scheduled_time": "Thursday"}
    with pytest.raises(InputBuildFailedError, match=r"^REQUEST_INVALID$"):
        select_request(event)


# ---- metrics on every call (§9) ----


def test_every_check_emits_unknown_sales_days_and_the_age_of_current() -> None:
    """A backfill gap before the first pin never counts; a hole inside the window does."""
    world = World(Selection(sales_start=INPUT_END - timedelta(days=700)))
    first = world.check(THURSDAY)
    assert first["outcome"] == "run"
    assert [(p.spec.name, p.spec.unit, p.value) for p in world.metrics.points] == [
        ("UnknownSalesDays", "Count", 0.0)
    ]
    world.end(first, RunOutcome.PUBLISHED)
    world.metrics.points.clear()
    world.selection.sales_holes.add(INPUT_END - timedelta(days=100))
    result = world.check(THURSDAY + timedelta(days=4))
    assert _decided(result) == ("held_input_not_ready", "weekly_slot_not_due", "slot_not_due")
    assert [(p.spec.name, p.spec.unit, p.value) for p in world.metrics.points] == [
        ("UnknownSalesDays", "Count", 1.0),
        ("ForecastCurrentAgeDays", "Count", 4.0),
    ]
    assert (result["unknown_sales_days"], result["current_age_days"]) == (1, 4)


def test_a_held_check_still_emits_its_metrics() -> None:
    world = World()
    world.publish_by_operator(INPUT_END + timedelta(days=1))
    world.metrics.points.clear()
    result = world.check(INPUT_END + timedelta(days=273))
    assert result["category"] == "input_end_too_old"
    assert [(p.spec.name, p.value) for p in world.metrics.points] == [
        ("UnknownSalesDays", 0.0),
        ("ForecastCurrentAgeDays", 272.0),
    ]
