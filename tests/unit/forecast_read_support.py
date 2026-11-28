# SPDX-License-Identifier: MIT-0
"""Synthetic stored views for the demand forecast read tests (ADR-163).

Every value is fictional: `B0FIXTUR..` ASINs, round stored figures and one fixed clock. The
builders return the exact stored types the store adapter parses, so the tests drive the real
read builders and the real `ShapedResponse` construction.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from adapters.config import DemandForecastReaderValues
from domain.forecasting import list_pages, read_api
from domain.forecasting.boundary import CandidateSeriesV1, RosterEntryV1
from domain.forecasting.policy import MONEY_TARGETS, TARGETS, ServingPolicy
from domain.forecasting.publish import CurrentPointer, Disposition, LastAttempt, RunOutcome
from domain.forecasting.read_api import AsinItemView, GenerationView
from domain.forecasting.summaries import roster_entry
from ports.outbound.forecast_store import ReadListPageRequest, ReadListPageResult

INPUT_END = date(2026, 11, 7)
"""A Saturday."""
PUBLISHED = datetime(2026, 11, 8, 14, 5, 9, 250_000, tzinfo=UTC)
NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
GENERATION = "run-1-0123456789ab"
SERVING = ServingPolicy(
    version=1,
    stale_after_days=16,
    page_rows=50,
    tool_list_max=50,
    compare_max_asins=5,
    views=(4, 8, 13),
    title_max_bytes=400,
    item_max_bytes=350_000,
    rank_by=list_pages.RANK_BY_UNITS_ORDERED_13W,
    status_target="units_ordered",
    search="exact_asin",
)


def fixture_asin(n: int) -> str:
    """Return the n-th fictional display ASIN, `B0FIXTUR01` onwards."""
    return f"B0FIXTUR{n:02d}" if n < 100 else f"B0FIXT{n:04d}"


def _score(mase: str | None, gap: str | None = None) -> dict[str, Any]:
    return {
        "wql": "0.2100",
        "mase": mase,
        "mase_gap": gap,
        "weeks_in_range": 9,
        "weeks_scored": 13,
    }


def _target(  # noqa: PLR0913 -- one fictional target with keyword overrides
    *, shown: bool, p50: str, mase: str | None = "0.8123", reason: str = "INSUFFICIENT_CONTEXT"
) -> dict[str, Any]:
    evaluations = [
        {
            "model": model,
            "backtest_score": _score(mase),
            "validity_reason": None,
            "origin_status": "publishable",
            "origin_failure": None,
        }
        for model in ("chronos2", "seasonal_naive", "ets")
    ]
    if not shown:
        return {
            "chosen_model": None,
            "withheld_reason": reason,
            "chosen_forecast": None,
            "gate_reason": None,
            "scored_weeks": 0,
            "evaluations": evaluations,
        }
    low = f"{float(p50) - 1:.4f}"
    high = f"{float(p50) + 1:.4f}"
    return {
        "chosen_model": "chronos2",
        "withheld_reason": None,
        "chosen_forecast": {"p10": [low] * 13, "p50": [p50] * 13, "p90": [high] * 13},
        "gate_reason": None,
        "scored_weeks": 13,
        "evaluations": evaluations,
    }


def candidate(  # noqa: PLR0913 -- one fictional product with keyword overrides
    n: int,
    *,
    shown: bool = True,
    currency: str | None = "USD",
    p50: str = "10.5000",
    money_p50: str = "1234.5000",
    week_states: tuple[str, ...] = (),
    reason: str = "INSUFFICIENT_CONTEXT",
) -> CandidateSeriesV1:
    """Return one fictional product's candidate; withheld products withhold every target.

    `week_states` sets the state of the first recent weeks for every target; the rest are
    observed. `reason` is the skip reason a withheld product carries.
    """
    targets = {
        t: _target(shown=shown, p50=money_p50 if t in MONEY_TARGETS else p50, reason=reason)
        for t in TARGETS
    }
    states = [*week_states, *["observed"] * (13 - len(week_states))]
    stored_value = {"observed": "9.0000", "inferred_zero": "0.0000", "unknown": None}
    weeks = [
        {
            "week_start": (INPUT_END - timedelta(days=6) - timedelta(weeks=12 - k)).isoformat(),
            "targets": {t: {"state": state, "value": stored_value[state]} for t in TARGETS},
            "stockout": k == 12 if state == "observed" else None,
        }
        for k, state in enumerate(states)
    ]
    return CandidateSeriesV1.model_validate(
        {
            "schema_version": 1,
            "canonical_asin": f"amazon_asin:{fixture_asin(n).lower()}",
            "display_asin": fixture_asin(n),
            "shard_index": 0,
            "targets": targets,
            "recent_weeks": weeks,
            "week_state_counts": {"observed": 104, "inferred_zero": 0, "unknown": 0},
            "stockout_weeks_last_13": 1,
            "currency_code": currency,
        }
    )


def item(n: int, **kwargs: Any) -> AsinItemView:
    """Return one product's parsed `ASIN#` item with its stored list row."""
    series = candidate(n, **kwargs)
    title = f"Fictional product {n}"
    return AsinItemView(
        candidate=series,
        title=title,
        title_truncated=False,
        roster=roster_entry(series, title, title_truncated=False),
    )


def ranked(items: list[AsinItemView]) -> tuple[RosterEntryV1, ...]:
    """Rank the items' stored rows as the publisher does."""
    return list_pages.rank_entries(
        [view.roster for view in items], list_pages.RANK_BY_UNITS_ORDERED_13W
    )


def generation(
    entries: tuple[RosterEntryV1, ...], *, currencies: tuple[str, ...] = ("USD",)
) -> GenerationView:
    """Return `META` for these ranked entries, cut into pages as the publisher does."""
    cut = list_pages.cut_pages(entries, SERVING)
    return GenerationView(
        generation_id=GENERATION,
        input_end=INPUT_END,
        first_forecast_week=INPUT_END + timedelta(days=1),
        item_count=len(entries),
        currency_codes=currencies,
        counts_by_reason={"units_ordered.INSUFFICIENT_CONTEXT": 1},
        counts_by_model={"units_ordered.chronos2": max(0, len(entries) - 1)},
        pages=cut.index,
        first_page=cut.first_page,
    )


def pages(entries: tuple[RosterEntryV1, ...]) -> dict[tuple[str, int], tuple[RosterEntryV1, ...]]:
    """Return every stored page by (filter, page)."""
    cut = list_pages.cut_pages(entries, SERVING)
    return {(page.filter, page.page): page.entries for page in cut.pages}


def attempt(outcome: RunOutcome, category: str | None = None) -> LastAttempt:
    """Return `CURRENT.last_attempt` with this outcome."""
    return LastAttempt(
        run_id="run-1",
        attempt_id="r1a1",
        run_fingerprint="f" * 64,
        outcome=outcome,
        failure_category=category,
        retry_disposition=Disposition.SUPPRESS,
        transient_retries=0,
        at=PUBLISHED,
    )


def pointer(
    *,
    published_at: datetime = PUBLISHED,
    last_attempt: LastAttempt | None = None,
    rolled_back_from: str | None = None,
) -> CurrentPointer:
    """Return a `CURRENT` that names the fixture generation."""
    return CurrentPointer(
        fence=3,
        generation_id=GENERATION,
        input_end=INPUT_END,
        published_at=published_at,
        run_fingerprint="f" * 64,
        rolled_back_from=rolled_back_from,
        last_attempt=last_attempt or attempt(RunOutcome.PUBLISHED),
    )


EMPTY_POINTER = CurrentPointer(fence=0)


VALUES = DemandForecastReaderValues(
    forecast_table="adlytics-test-demand-forecast",
    marketplace_key="fictional-marketplace",
    check_schedule=read_api.parse_daily_schedule("cron(0 13 * * ? *)"),
)


@dataclass
class FakeReader:
    """The reader port over in-memory stored views; every call is one `GetItem`."""

    current: CurrentPointer | None
    meta: GenerationView | None = None
    stored_pages: dict[tuple[str, int], tuple[RosterEntryV1, ...]] = field(
        default_factory=dict[tuple[str, int], tuple[RosterEntryV1, ...]]
    )
    items: dict[str, AsinItemView] = field(default_factory=dict[str, AsinItemView])
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    fail: Exception | None = None
    block: threading.Event | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _call(self, kind: str, key: str) -> None:
        with self._lock:
            self.calls.append((kind, key))
        if self.block is not None and kind == "asin":
            self.block.wait(30)
        if self.fail is not None and kind != "current":
            raise self.fail

    def get_current(self, set_id: str) -> CurrentPointer | None:
        self._call("current", set_id)
        return self.current

    def read_generation(self, generation_id: str) -> GenerationView | None:
        self._call("meta", generation_id)
        return self.meta if self.meta and self.meta.generation_id == generation_id else None

    def read_list_page(self, request: ReadListPageRequest) -> ReadListPageResult:
        self._call("page", f"{request.generation_id}#{request.filter}#{request.page}")
        return ReadListPageResult(self.stored_pages.get((request.filter, request.page)))

    def read_asin(self, generation_id: str, display_asin: str) -> AsinItemView | None:
        self._call("asin", f"{generation_id}#{display_asin}")
        return self.items.get(display_asin) if generation_id == GENERATION else None


def fake_reader() -> FakeReader:
    views = [item(1), item(2, p50="20.0000"), item(3, shown=False)]
    entries = ranked(views)
    return FakeReader(
        current=pointer(),
        meta=generation(entries),
        stored_pages=pages(entries),
        items={view.candidate.display_asin: view for view in views},
    )
