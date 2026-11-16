# SPDX-License-Identifier: MIT-0
"""The generation's ranking and precomputed list pages (design §6b, R5-03, R5-04, R5C2-10).

A wrong rank order shows planners the wrong products first; a page cut that drops, repeats or
misfiles an entry shows a list that disagrees with the generation; a closed filter missing from
the index turns an empty, valid filter into a store fault.
"""

from __future__ import annotations

from typing import Any

import pytest
from forecast_series_support import policy

from domain.forecasting.boundary import RosterEntryV1
from domain.forecasting.list_pages import (
    CLOSED_FILTERS,
    FilterPages,
    cut_pages,
    filter_of,
    merge_roster_parts,
    rank_entries,
)
from domain.forecasting.models import SkipReason
from domain.forecasting.policy import TARGETS

pytestmark = pytest.mark.unit

RANK_BY = "units_ordered_p50_13w"


def _row(total: str | None, reason: str | None = None) -> dict[str, Any]:
    if total is None:
        return {
            "chosen_model": None,
            "withheld_reason": reason or "INSUFFICIENT_CONTEXT",
            "p50_totals": {"4": None, "8": None, "13": None},
            "currency_code": None,
        }
    return {
        "chosen_model": "ets",
        "withheld_reason": None,
        "p50_totals": {"4": "1.0000", "8": "2.0000", "13": total},
        "currency_code": None,
    }


def _entry(display: str, ordered: str | None, *, reason: str | None = None) -> RosterEntryV1:
    targets = {t: _row("5.0000") for t in TARGETS}
    targets["units_ordered"] = _row(ordered, reason)
    return RosterEntryV1.model_validate(
        {
            "schema_version": 1,
            "rank": None,
            "display_asin": display,
            "title": None,
            "title_truncated": False,
            "targets": targets,
            "accuracy_class": None,
            "stockout_weeks_last_13": 0,
        }
    )


def test_the_status_filter_reads_the_ordered_units_target() -> None:
    shown = _entry("B000000001", "3.0000")
    money = _entry("B000000009", None, reason="CURRENCY_MIXED").targets["units_ordered"]
    money_withheld = shown.model_copy(
        update={"targets": {**shown.targets, "revenue_shipped": money}}
    )
    assert filter_of(money_withheld, "units_ordered") == "shown"
    assert filter_of(money_withheld, "revenue_shipped") == "withheld:CURRENCY_MIXED"
    withheld = _entry("B000000002", None, reason="ACCURACY_NOT_TESTABLE")
    assert filter_of(withheld, "units_ordered") == "withheld:ACCURACY_NOT_TESTABLE"


def test_the_closed_filter_set_is_all_shown_and_every_withheld_reason() -> None:
    reasons = {f"withheld:{reason.value}" for reason in SkipReason}
    assert set(CLOSED_FILTERS) == {"all", "shown"} | reasons
    assert len(CLOSED_FILTERS) == len(SkipReason) + 2


def test_ranking_sorts_by_13_week_ordered_units_then_display_asin_and_withheld_last() -> None:
    entries = [
        _entry("B00000000Z", None),
        _entry("B00000000C", "10.0000"),
        _entry("B00000000A", "10.0000"),
        _entry("B00000000Y", None),
        _entry("B00000000B", "99.5000"),
        _entry("B00000000D", "9.9999"),
    ]
    ranked = rank_entries(entries, RANK_BY)
    assert [(e.rank, e.display_asin) for e in ranked] == [
        (1, "B00000000B"),
        (2, "B00000000A"),
        (3, "B00000000C"),
        (4, "B00000000D"),
        (5, "B00000000Y"),
        (6, "B00000000Z"),
    ]


def test_ranking_by_another_key_is_refused() -> None:
    with pytest.raises(ValueError, match="rank_by"):
        rank_entries([_entry("B000000001", "1.0000")], "revenue_shipped_p50_13w")


def test_merge_concatenates_the_parts_in_order() -> None:
    first = (_entry("B000000002", "1.0000"), _entry("B000000001", "2.0000"))
    second = (_entry("B000000003", "3.0000"),)
    merged = merge_roster_parts([first, second])
    assert [e.display_asin for e in merged] == ["B000000002", "B000000001", "B000000003"]


def test_pages_cut_each_filter_in_rank_order_and_record_every_closed_filter() -> None:
    shown = [_entry(f"B{n:09d}", f"{1000 - n}.0000") for n in range(70)]
    withheld = [_entry(f"C{n:09d}", None, reason="ETS_FIT_FAILED") for n in range(50)]
    ranked = rank_entries(shown + withheld, RANK_BY)
    pages = cut_pages(ranked, policy().serving)
    by_key = {(page.filter, page.page): page.entries for page in pages.pages}
    assert {key: len(entries) for key, entries in by_key.items()} == {
        ("all", 1): 50,
        ("all", 2): 50,
        ("all", 3): 20,
        ("shown", 1): 50,
        ("shown", 2): 20,
        ("withheld:ETS_FIT_FAILED", 1): 50,
    }
    assert [e.rank for e in by_key[("shown", 2)]] == list(range(51, 71))
    assert [e.rank for e in by_key[("withheld:ETS_FIT_FAILED", 1)]] == list(range(71, 121))
    assert set(pages.index) == set(CLOSED_FILTERS)
    assert pages.index["all"] == FilterPages(page_count=3, entry_count=120)
    assert pages.index["withheld:ETS_FIT_FAILED"] == FilterPages(page_count=1, entry_count=50)
    assert pages.index["withheld:HOLDOUT_UNKNOWN"] == FilterPages(page_count=1, entry_count=0)
    assert pages.first_page == by_key[("all", 1)]


def test_an_empty_filter_has_one_page_in_the_index_and_no_page_item() -> None:
    pages = cut_pages(rank_entries([_entry("B000000001", "1.0000")], RANK_BY), policy().serving)
    assert pages.index["withheld:ACCURACY_NOT_TESTABLE"] == FilterPages(1, 0)
    assert {page.filter for page in pages.pages} == {"all", "shown"}


def test_cutting_unranked_entries_is_refused() -> None:
    with pytest.raises(ValueError, match="ranked"):
        cut_pages([_entry("B000000001", "1.0000")], policy().serving)


def test_a_50000_entry_merge_over_25_parts_gives_1000_all_pages_and_status_totals() -> None:
    """Exercise the commit's O(N) step at the 50,000-ASIN design point (§6c).

    Its time and memory feed the 200,000-ASIN projection (build-s2 commit-scale evidence).
    """
    parts = [
        tuple(
            _entry(f"B{n:09d}", None if n % 5 == 0 else f"{n % 997}.0000")
            for n in range(part * 2000, (part + 1) * 2000)
        )
        for part in range(25)
    ]
    ranked = rank_entries(merge_roster_parts(parts), RANK_BY)
    pages = cut_pages(ranked, policy().serving)
    assert [entry.rank for entry in ranked] == list(range(1, 50001))
    all_pages = [page for page in pages.pages if page.filter == "all"]
    assert len(all_pages) == 1000
    assert {len(page.entries) for page in all_pages} == {50}
    assert pages.index["all"] == FilterPages(page_count=1000, entry_count=50000)
    status = [name for name in pages.index if name != "all"]
    assert sum(pages.index[name].entry_count for name in status) == 50000
    assert pages.index["shown"] == FilterPages(page_count=800, entry_count=40000)
    assert pages.index["withheld:INSUFFICIENT_CONTEXT"] == FilterPages(200, 10000)
