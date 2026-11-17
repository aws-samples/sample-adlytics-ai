# SPDX-License-Identifier: MIT-0
"""Forecast summaries for the page and tools, one implementation for both (design §4a).

`roster_entry()` is the one function that builds a list row. Only the publisher calls it: the
row is stored in the roster part and in each product's `ASIN#` item, so readers never sum
(ADR-163). `cap_title()` is the display rule for titles over `serving.title_max_bytes`. The p50
totals are sums of the stored four-place values (§4d), so they are exact and stay stored values;
rounding for display happens later, once, on the server. The stored `accuracy_class` stays null:
`classify_accuracy()` labels the chosen model's stored score at read time and never changes
published bytes. `require_shared_currency()` refuses a money answer over two currencies.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from domain.forecasting.boundary import (
    CandidateSeriesV1,
    RosterEntryV1,
    RosterTargetV1,
    TargetCandidateV1,
)
from domain.forecasting.models import AccuracyGap
from domain.forecasting.numeric import stored_text
from domain.forecasting.policy import MONEY_TARGETS, TARGETS
from domain.shared_kernel.errors import DomainError, ErrorCode

P50_VIEWS: tuple[str, ...] = ("4", "8", "13")
"""The list row's p50 totals: the first 4, 8 and 13 forecast weeks (`serving.views`)."""

TRUNCATION_MARK = "…"

AccuracyClass = Literal["beat_naive", "not_beat_naive", "history_unchanging", "scale_unknown"]
"""A label for the chosen model's stored backtest score; never a figure (ADR-163)."""

CURRENCY_MISMATCH: Final = ErrorCode("FORECAST_READ", 6)
"""A money answer would carry more than one currency; `context.currencies` lists them."""


@dataclass(frozen=True)
class DisplayTitle:
    """A title capped to `serving.title_max_bytes`, and whether it was cut."""

    title: str | None
    truncated: bool


def cap_title(title: str | None, max_bytes: int) -> DisplayTitle:
    """Cut a title over `max_bytes` UTF-8 bytes to fit, ending in `…` (§6b).

    The cut falls at the last whole code point that leaves room for the mark. The full title
    stays in `dim_product`.
    """
    encoded = b"" if title is None else title.encode("utf-8")
    if title is None or len(encoded) <= max_bytes:
        return DisplayTitle(title, truncated=False)
    room = max_bytes - len(TRUNCATION_MARK.encode("utf-8"))
    if room < 0:
        raise ValueError(f"title_max_bytes {max_bytes} leaves no room for the truncation mark")
    kept = encoded[:room].decode("utf-8", errors="ignore")
    return DisplayTitle(kept + TRUNCATION_MARK, truncated=True)


def _target_row(target: str, candidate: TargetCandidateV1, currency: str | None) -> RosterTargetV1:
    bands = candidate.chosen_forecast
    if bands is None:
        totals: dict[str, str | None] = dict.fromkeys(P50_VIEWS)
    else:
        values = [Decimal(value) for value in bands.p50]
        totals = {view: stored_text(sum(values[: int(view)], Decimal(0))) for view in P50_VIEWS}
    return RosterTargetV1(
        chosen_model=candidate.chosen_model,
        withheld_reason=candidate.withheld_reason,
        p50_totals=totals,  # pyright: ignore[reportArgumentType]
        currency_code=currency if target in MONEY_TARGETS else None,
    )


def roster_entry(
    candidate: CandidateSeriesV1, title: str | None, *, title_truncated: bool
) -> RosterEntryV1:
    """Return the candidate's unranked list row, with its display title already capped."""
    return RosterEntryV1(
        schema_version=1,
        rank=None,
        display_asin=candidate.display_asin,
        title=title,
        title_truncated=title_truncated,
        targets={  # pyright: ignore[reportArgumentType]
            target: _target_row(
                target,
                candidate.targets[target],  # pyright: ignore[reportArgumentType]
                candidate.currency_code,
            )
            for target in TARGETS
        },
        accuracy_class=None,
        stockout_weeks_last_13=candidate.stockout_weeks_last_13,
    )


def classify_accuracy(candidate: TargetCandidateV1) -> AccuracyClass | None:
    """Label the chosen model's stored score, or None when withheld or unscored.

    MASE below 1 beat repeating the naive value; at or above 1 it did not. A valid backtest with
    no MASE carries its reason instead. This compares a stored value with 1; it makes no figure.
    """
    if candidate.chosen_model is None:
        return None
    score = next(
        (e.backtest_score for e in candidate.evaluations if e.model is candidate.chosen_model),
        None,
    )
    if score is None:
        return None
    if score.mase is not None:
        return "beat_naive" if Decimal(score.mase) < 1 else "not_beat_naive"
    if score.mase_gap is AccuracyGap.HISTORY_UNCHANGING:
        return "history_unchanging"
    if score.mase_gap is AccuracyGap.MASE_SCALE_UNKNOWN:
        return "scale_unknown"
    return None


def require_shared_currency(target: str, rows: Iterable[RosterTargetV1]) -> None:
    """Refuse a money target whose shown rows carry more than one currency (M2).

    A withheld row carries no figure, so its currency never counts. Unit targets never refuse.
    """
    if target not in MONEY_TARGETS:
        return
    codes = sorted(
        {row.currency_code for row in rows if row.chosen_model is not None and row.currency_code}
    )
    if len(codes) > 1:
        raise DomainError(
            CURRENCY_MISMATCH,
            "These products are forecast in different currencies, so they can't be answered "
            "together. Ask about one currency at a time.",
            context={"currencies": codes},
        )
