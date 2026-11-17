# SPDX-License-Identifier: MIT-0
"""The demand forecast read contract's pure builders (ADR-163, ADR-084).

A wrong builder shows a planner a figure the publisher never stored, a partial list as whole,
a stale forecast as fresh or a money figure at the wrong scale. These tests pin each refusal
code with a positive control, every list and detail state, freshness at its 16-day boundary,
the schedule's next checks and the server display of stored figures, as literals.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from forecast_read_support import (
    EMPTY_POINTER,
    NOW,
    PUBLISHED,
    SERVING,
    attempt,
    generation,
    item,
    pages,
    pointer,
    ranked,
)

from domain.forecasting import read_api
from domain.forecasting.publish import RunOutcome
from domain.forecasting.read_api import (
    DetailSource,
    FreshnessRequest,
    ListRequest,
    ListSource,
    StatusSource,
)
from domain.shared_kernel.errors import AdlyticsError, InvariantError, ValidationError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-163")]

SCHEDULE = read_api.parse_daily_schedule("cron(0 13 * * ? *)")


def _code(error: pytest.ExceptionInfo[AdlyticsError]) -> str:
    return str(error.value.code)


@pytest.mark.parametrize("page", ["100000", "0", "01", "1" * 5_000, "-1", "1.0", ""])
def test_a_page_outside_one_to_99999_is_refused_before_any_read(page: str) -> None:
    with pytest.raises(ValidationError) as refused:
        read_api.list_request(None, page)
    assert _code(refused) == "ADLY-FORECAST_READ-0003"


def test_a_page_of_99999_and_a_listed_status_are_accepted() -> None:
    assert read_api.list_request("shown", "99999") == ListRequest("shown", 99_999)
    request = read_api.list_request("INSUFFICIENT_CONTEXT", None)
    assert request == ListRequest("withheld:INSUFFICIENT_CONTEXT", 1)
    assert request.status == "INSUFFICIENT_CONTEXT"


@pytest.mark.parametrize("status", ["all", "withheld:INSUFFICIENT_CONTEXT", "insufficient_context"])
def test_a_status_outside_shown_and_the_skip_reasons_is_refused(status: str) -> None:
    with pytest.raises(ValidationError) as refused:
        read_api.list_request(status, None)
    assert _code(refused) == "ADLY-FORECAST_READ-0002"


def test_an_asin_is_upper_cased_and_a_malformed_one_is_refused() -> None:
    assert read_api.parse_asin("b0fixtur01") == "B0FIXTUR01"
    with pytest.raises(ValidationError) as refused:
        read_api.parse_asin("B0FIXTUR0")
    assert _code(refused) == "ADLY-FORECAST_READ-0001"


@pytest.mark.parametrize(
    ("value", "target", "display"),
    [
        ("12.3000", "revenue_shipped", "12.30"),
        ("-12.3050", "cogs_shipped", "-12.31"),
        ("-0.0040", "revenue_shipped", "0.00"),
        ("1234.5000", "units_ordered", "1235"),
        ("-2.5000", "units_shipped", "-3"),
    ],
)
@pytest.mark.covers("ADR-084")
def test_a_figure_keeps_its_stored_value_and_displays_at_its_target_scale(
    value: str, target: str, display: str
) -> None:
    shown = read_api.figure(value, target)
    assert (shown.value, shown.display) == (value, display)


def test_an_instant_is_whole_second_utc_with_z() -> None:
    moment = datetime(2026, 11, 8, 15, 5, 9, 987_654, tzinfo=UTC).astimezone()
    assert read_api.api_instant(moment) == "2026-11-08T15:05:09Z"


@pytest.mark.parametrize(
    ("now", "next_check", "next_new_input"),
    [
        (
            datetime(2026, 11, 17, 12, 59, tzinfo=UTC),
            "2026-11-17T13:00:00Z",
            "2026-11-19T13:00:00Z",
        ),
        (datetime(2026, 11, 17, 13, 0, tzinfo=UTC), "2026-11-18T13:00:00Z", "2026-11-19T13:00:00Z"),
        (datetime(2026, 11, 19, 12, 0, tzinfo=UTC), "2026-11-19T13:00:00Z", "2026-11-19T13:00:00Z"),
        (
            datetime(2026, 11, 19, 13, 30, tzinfo=UTC),
            "2026-11-20T13:00:00Z",
            "2026-11-26T13:00:00Z",
        ),
    ],
)
def test_the_next_checks_follow_the_daily_schedule_and_the_admission_weekday(
    now: datetime, next_check: str, next_new_input: str
) -> None:
    assert read_api.api_instant(read_api.next_check_at(now, SCHEDULE)) == next_check
    shown = read_api.next_new_input_check_at(now, SCHEDULE, "thursday")
    assert read_api.api_instant(shown) == next_new_input


@pytest.mark.parametrize("cron", ["cron(0 13 * * ? 2026)", "rate(1 day)", "cron(60 13 * * ? *)"])
def test_a_schedule_other_than_one_daily_time_is_refused(cron: str) -> None:
    with pytest.raises(ValueError, match=r"schedule|time of day"):
        read_api.parse_daily_schedule(cron)


@pytest.mark.parametrize(
    ("age_days", "last", "expected"),
    [
        (16, RunOutcome.PUBLISHED, ("fresh", ())),
        (17, RunOutcome.PUBLISHED, ("stale", ("age_over_limit",))),
        (1, RunOutcome.FAILED, ("stale", ("last_run_failed",))),
        (17, RunOutcome.HELD_QUALITY, ("stale", ("age_over_limit", "last_run_held"))),
        (1, RunOutcome.ROLLED_BACK, ("fresh", ())),
    ],
)
def test_freshness_is_stale_past_16_days_or_after_a_failed_or_held_run(
    age_days: int, last: RunOutcome, expected: tuple[str, tuple[str, ...]]
) -> None:
    current = pointer(published_at=NOW - timedelta(days=age_days), last_attempt=attempt(last))
    shown = read_api.forecast_freshness(FreshnessRequest(current, NOW, 16))
    assert (shown.state, shown.causes) == expected
    assert shown.age_days == age_days


def test_freshness_is_unavailable_without_a_generation() -> None:
    shown = read_api.forecast_freshness(FreshnessRequest(EMPTY_POINTER, NOW, 16))
    assert (shown.state, shown.age_days, shown.causes) == ("unavailable", None, ())


@pytest.mark.parametrize(
    ("category", "family"),
    [
        ("item_too_large", "quality_check"),
        ("weekly_slot_not_due", "input_not_ready"),
        ("job:MODEL_ARTIFACT_MISMATCH", "forecast_job"),
        ("input_build:ROSTER_TOO_LARGE", "input_build"),
        ("retry_exhausted", "retry_exhausted"),
        ("publish:ForecastStoreThrottled", "other"),
    ],
)
def test_each_stored_failure_category_maps_to_its_family(category: str, family: str) -> None:
    assert read_api.failure_family(category) == family


def _three() -> tuple[list[read_api.AsinItemView], read_api.GenerationView]:
    items = [item(1), item(2, p50="20.0000"), item(3, shown=False)]
    return items, generation(ranked(items))


def test_list_page_one_serves_meta_first_page_in_rank_order_with_counts() -> None:
    _, meta = _three()
    shown = read_api.build_list(
        ListSource(ListRequest("all", 1), pointer(), meta, None, SERVING), NOW
    )
    assert [entry.asin for entry in shown.entries] == ["B0FIXTUR02", "B0FIXTUR01", "B0FIXTUR03"]
    assert (shown.outcome, shown.page, shown.page_count, shown.match_count) == ("complete", 1, 1, 3)
    totals = shown.entries[0].targets["units_ordered"].p50_totals
    assert {view: (f.value, f.display) for view, f in totals.items() if f} == {
        "4": ("80.0000", "80"),
        "8": ("160.0000", "160"),
        "13": ("260.0000", "260"),
    }
    money = shown.entries[0].targets["revenue_shipped"]
    assert (money.currency_code, money.p50_totals["4"]) == (
        "USD",
        read_api.figure("4938.0000", "revenue_shipped"),
    )
    assert money.p50_totals["4"] is not None and money.p50_totals["4"].display == "4938.00"
    assert shown.counts_by_reason == {
        "units_shipped": {},
        "units_ordered": {"INSUFFICIENT_CONTEXT": 1},
        "revenue_shipped": {},
        "cogs_shipped": {},
    }


def test_a_filtered_page_serves_its_stored_page_item() -> None:
    items, meta = _three()
    stored = pages(ranked(items))[("shown", 1)]
    source = ListSource(ListRequest("shown", 1), pointer(), meta, stored, SERVING)
    shown = read_api.build_list(source, NOW)
    assert [entry.asin for entry in shown.entries] == ["B0FIXTUR02", "B0FIXTUR01"]
    assert (shown.query.status, shown.match_count) == ("shown", 2)


def test_an_empty_filter_is_a_complete_first_page_and_its_page_two_is_refused() -> None:
    _, meta = _three()
    empty = ListRequest("withheld:ASIN_INACTIVE", 1)
    shown = read_api.build_list(ListSource(empty, pointer(), meta, None, SERVING), NOW)
    assert (shown.outcome, shown.entries, shown.match_count, shown.page_count) == (
        "complete",
        (),
        0,
        1,
    )
    with pytest.raises(ValidationError) as refused:
        read_api.build_list(
            ListSource(ListRequest("withheld:ASIN_INACTIVE", 2), pointer(), meta, None, SERVING),
            NOW,
        )
    assert _code(refused) == "ADLY-FORECAST_READ-0003"


def test_a_page_short_against_meta_or_missing_is_store_incomplete() -> None:
    items, meta = _three()
    stored = pages(ranked(items))[("shown", 1)]
    for entries in (stored[:1], None):
        with pytest.raises(InvariantError) as refused:
            read_api.build_list(
                ListSource(ListRequest("shown", 1), pointer(), meta, entries, SERVING), NOW
            )
        assert _code(refused) == "ADLY-FORECAST_READ-0008"


def test_meta_of_another_generation_is_store_incomplete() -> None:
    _, meta = _three()
    other = replace(pointer(), generation_id="run-2-0123456789ab")
    with pytest.raises(InvariantError) as refused:
        read_api.build_list(ListSource(ListRequest("all", 1), other, meta, None, SERVING), NOW)
    assert _code(refused) == "ADLY-FORECAST_READ-0008"


def test_an_unavailable_list_has_the_requested_page_and_no_figures() -> None:
    shown = read_api.build_unavailable_list(ListRequest("shown", 3), SERVING, NOW)
    assert (shown.outcome, shown.page, shown.page_count, shown.match_count) == (
        "unavailable",
        3,
        0,
        0,
    )
    assert (shown.entries, shown.item_count, shown.currency_codes) == ((), None, ())
    assert shown.freshness.state == "unavailable"


def test_a_detail_serves_stored_bands_totals_accuracy_and_recent_weeks() -> None:
    shown = read_api.build_detail(DetailSource("B0FIXTUR01", pointer(), item(1), SERVING), NOW)
    assert shown.outcome == "selected"
    assert shown.provenance is not None
    assert (shown.provenance.published_at, str(shown.provenance.first_forecast_week)) == (
        "2026-11-08T14:05:09Z",
        "2026-11-08",
    )
    assert shown.targets is not None
    units = shown.targets["units_ordered"]
    assert units.bands is not None
    assert (units.bands.p10[0].display, units.bands.p50[0].display, units.bands.p90[0].display) == (
        "10",
        "11",
        "12",
    )
    assert units.accuracy is not None
    assert (units.accuracy.accuracy_class, units.accuracy.mase, units.accuracy.weeks_in_range) == (
        "beat_naive",
        "0.8123",
        9,
    )
    money = shown.targets["cogs_shipped"]
    assert money.bands is not None and money.bands.p50[0].display == "1234.50"
    assert shown.recent_weeks is not None and shown.recent_weeks[-1].stockout is True


def test_detail_outcomes_for_withheld_absent_and_unpublished_products() -> None:
    withheld = read_api.build_detail(
        DetailSource("B0FIXTUR03", pointer(), item(3, shown=False), SERVING), NOW
    )
    assert withheld.outcome == "not_forecast"
    absent = read_api.build_detail(DetailSource("B0FIXTUR09", pointer(), None, SERVING), NOW)
    assert (absent.outcome, absent.title, absent.title_truncated, absent.targets) == (
        "not_in_forecast",
        None,
        False,
        None,
    )
    unpublished = read_api.build_detail(
        DetailSource("B0FIXTUR09", EMPTY_POINTER, None, SERVING), NOW
    )
    assert (unpublished.outcome, unpublished.provenance, unpublished.horizon) == (
        "unavailable",
        None,
        None,
    )


def test_an_item_for_another_asin_is_store_incomplete() -> None:
    with pytest.raises(InvariantError) as refused:
        read_api.build_detail(DetailSource("B0FIXTUR02", pointer(), item(1), SERVING), NOW)
    assert _code(refused) == "ADLY-FORECAST_READ-0008"


@pytest.mark.parametrize(
    ("last", "hold"),
    [
        (RunOutcome.PUBLISHED, "none"),
        (RunOutcome.HELD_QUALITY, "quality"),
        (RunOutcome.FAILED, "none"),
        (RunOutcome.ROLLED_BACK, "none"),
    ],
)
def test_status_hold_follows_the_last_forecast_run(last: RunOutcome, hold: str) -> None:
    category = "item_too_large" if last in (RunOutcome.HELD_QUALITY, RunOutcome.FAILED) else None
    current = pointer(last_attempt=attempt(last, category))
    shown = read_api.build_status(StatusSource(current, SERVING, SCHEDULE, "thursday"), NOW)
    assert shown.last_run is not None
    assert (shown.last_run.outcome, shown.hold) == (last.value, hold)
    assert shown.last_run.failure_family == (None if category is None else "quality_check")


def test_status_without_a_generation_still_names_the_next_checks() -> None:
    shown = read_api.build_status(StatusSource(EMPTY_POINTER, SERVING, SCHEDULE, "thursday"), NOW)
    assert (shown.generation, shown.last_run, shown.freshness.state) == (None, None, "unavailable")
    assert (shown.next_check_at, shown.next_new_input_check_at) == (
        "2026-11-17T13:00:00Z",
        "2026-11-19T13:00:00Z",
    )


def test_a_rollback_is_reported_with_its_source_generation() -> None:
    current = pointer(
        rolled_back_from="run-0-0123456789ab", last_attempt=attempt(RunOutcome.ROLLED_BACK)
    )
    shown = read_api.build_status(StatusSource(current, SERVING, SCHEDULE, "thursday"), NOW)
    assert (shown.rollback_active, shown.rolled_back_from) == (True, "run-0-0123456789ab")
    assert shown.generation is not None and shown.generation.published_at == "2026-11-08T14:05:09Z"
    assert PUBLISHED.microsecond == 250_000


def _figure(display: str) -> dict[str, str]:
    return {"value": "1.0000", "display": display}


def _target(
    currency: str | None, display: str, totals: tuple[str, ...] = ("4", "8", "13")
) -> dict[str, object]:
    return {
        "chosen_model": "chronos2",
        "withheld_reason": None,
        "currency_code": currency,
        "p50_totals": {view: _figure(display) for view in totals},
    }


def _entry(**money: dict[str, object]) -> dict[str, object]:
    targets = {
        "units_shipped": _target(None, "1"),
        "units_ordered": _target(None, "1"),
        "revenue_shipped": _target("USD", "1.00"),
        "cogs_shipped": _target("USD", "1.00"),
        **money,
    }
    return {
        "rank": 1,
        "asin": "B0FIXTUR01",
        "title": None,
        "title_truncated": False,
        "targets": targets,
        "stockout_weeks_last_13": 0,
    }


def test_a_well_formed_list_entry_validates() -> None:
    assert read_api.ListEntryV1.model_validate(_entry()).asin == "B0FIXTUR01"


@pytest.mark.parametrize(
    ("override", "rule"),
    [
        ({"revenue_shipped": _target("USD", "1")}, "two places"),
        ({"units_ordered": _target(None, "1.00")}, "two places"),
        ({"units_ordered": _target("USD", "1")}, "no currency"),
        ({"revenue_shipped": _target(None, "1.00")}, "carries its currency"),
        ({"revenue_shipped": _target("usd", "1.00")}, "pattern"),
        ({"units_ordered": _target(None, "1", ("4", "8"))}, "exactly 4, 8 and 13"),
    ],
)
def test_a_list_entry_off_the_display_currency_or_key_rules_is_refused(
    override: dict[str, dict[str, object]], rule: str
) -> None:
    with pytest.raises(ValueError, match=rule):
        read_api.ListEntryV1.model_validate(_entry(**override))


def test_a_list_entry_missing_a_target_is_refused() -> None:
    entry = _entry()
    targets = entry["targets"]
    assert isinstance(targets, dict)
    del targets["cogs_shipped"]
    with pytest.raises(ValueError, match="exactly the four forecast targets"):
        read_api.ListEntryV1.model_validate(entry)


def test_a_refusal_body_carries_the_code_name_and_user_visible_detail() -> None:
    body = read_api.refusal_body(
        ValidationError(read_api.QUERY_INVALID, "Use only asin, status and page.")
    )
    assert (body.refused, body.code, body.detail) == (
        "QUERY_INVALID",
        "ADLY-FORECAST_READ-0004",
        "Use only asin, status and page.",
    )
    failed = read_api.refusal_body(
        InvariantError(read_api.FORECAST_READ_FAILED, "boom: provider text")
    )
    assert failed.detail == "The request could not be completed. The issue has been recorded."
