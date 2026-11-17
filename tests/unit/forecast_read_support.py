# SPDX-License-Identifier: MIT-0
"""Synthetic stored views for the demand forecast read tests (ADR-163).

Every value is fictional: `B0FIXTUR..` ASINs, round stored figures and one fixed clock. The
builders return the exact stored types the store adapter parses, so the tests drive the real
read builders and the real `ShapedResponse` construction.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from domain.forecasting import list_pages
from domain.forecasting.boundary import CandidateSeriesV1, RosterEntryV1
from domain.forecasting.policy import MONEY_TARGETS, TARGETS, ServingPolicy
from domain.forecasting.publish import CurrentPointer, Disposition, LastAttempt, RunOutcome
from domain.forecasting.read_api import AsinItemView, GenerationView
from domain.forecasting.summaries import roster_entry

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


def _target(*, shown: bool, p50: str, mase: str | None = "0.8123") -> dict[str, Any]:
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
            "withheld_reason": "INSUFFICIENT_CONTEXT",
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
) -> CandidateSeriesV1:
    """Return one fictional product's candidate; withheld products withhold every target."""
    targets = {
        t: _target(shown=shown, p50=money_p50 if t in MONEY_TARGETS else p50) for t in TARGETS
    }
    weeks = [
        {
            "week_start": (INPUT_END - timedelta(days=6) - timedelta(weeks=12 - k)).isoformat(),
            "targets": {t: {"state": "observed", "value": "9.0000"} for t in TARGETS},
            "stockout": k == 12,
        }
        for k in range(13)
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
