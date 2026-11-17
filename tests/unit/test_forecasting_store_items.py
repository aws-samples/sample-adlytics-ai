# SPDX-License-Identifier: MIT-0
"""Forecast store items: key layout, title caps and the item ceiling (design §6b, R2-17).

An item over DynamoDB's 400 KB limit fails the publish at write time, after other items of the
generation landed; a title cut inside a code point stores text that isn't UTF-8; a key that
shares a partition across a generation throttles a large publication. These tests hold the
worst-case items under the 350,000-byte ceiling and refuse an over-ceiling item before any
write.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any, cast

import pytest
from forecast_series_support import INPUT_END, asin
from forecast_store_support import ForecastTableFake

from adapters.outbound.dynamodb.demand_forecast_store import (
    DynamoDbForecastStore,
    asin_item,
    asin_key,
    item_size,
    meta_item,
    page_item,
    page_key,
)
from domain.forecasting.boundary import (
    CandidateSeriesV1,
    RosterEntryV1,
    RunIdentityV1,
    boundary_schema_digests,
)
from domain.forecasting.identity import display_asin
from domain.forecasting.list_pages import CLOSED_FILTERS, FilterPages, ListPage, ListPages
from domain.forecasting.policy import TARGETS
from domain.forecasting.publish import ShardPublishResult
from domain.forecasting.summaries import cap_title, roster_entry
from ports.outbound.forecast_store import (
    ForecastItemTooLargeError,
    ForecastStoreThrottled,
    GenerationMeta,
    GenerationSeries,
    SlotClaimRequest,
)

pytestmark = pytest.mark.unit

CEILING = 350_000
"""`serving.item_max_bytes`, pinned."""
TITLE_MAX = 400
"""`serving.title_max_bytes`, pinned."""
HEX = "a" * 64
GENERATION = "run-1-0123456789ab"
WIDE = "é"
"""Two UTF-8 bytes, six on the ASCII-escaped wire: the oracle's worst case per title byte."""


def _evaluation(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "backtest_score": {
            "wql": "0.4000",
            "mase": "1.0000",
            "mase_gap": None,
            "weeks_in_range": 13,
            "weeks_scored": 13,
        },
        "validity_reason": None,
        "origin_status": "publishable",
        "origin_failure": None,
    }


def _candidate(n: int) -> CandidateSeriesV1:
    bands = {q: ["12345.6789"] * 13 for q in ("p10", "p50", "p90")}
    target = {
        "chosen_model": "chronos2",
        "withheld_reason": None,
        "chosen_forecast": bands,
        "gate_reason": None,
        "scored_weeks": 13,
        "evaluations": [_evaluation(m) for m in ("chronos2", "seasonal_naive", "ets")],
    }
    weeks = [
        {
            "week_start": _week(n),
            "targets": {t: {"state": "observed", "value": "12345.6789"} for t in TARGETS},
            "stockout": False,
        }
        for n in range(13)
    ]
    return CandidateSeriesV1.model_validate(
        {
            "schema_version": 1,
            "canonical_asin": asin(n),
            "display_asin": display_asin(asin(n)),
            "shard_index": 0,
            "targets": dict.fromkeys(TARGETS, target),
            "recent_weeks": weeks,
            "week_state_counts": {"observed": 104, "inferred_zero": 0, "unknown": 0},
            "stockout_weeks_last_13": 0,
            "currency_code": "USD",
        }
    )


def _week(index: int) -> str:
    return (INPUT_END - timedelta(days=6) - timedelta(weeks=12 - index)).isoformat()


def _worst_entries() -> tuple[RosterEntryV1, ...]:
    capped = cap_title(WIDE * 1_000, TITLE_MAX)
    return tuple(
        roster_entry(_candidate(n), capped.title, title_truncated=capped.truncated).model_copy(
            update={"rank": 199_950 + n}
        )
        for n in range(1, 51)
    )


def _identity() -> RunIdentityV1:
    return RunIdentityV1(
        input_fingerprint=HEX,
        policy_sha256=HEX,
        policy_section_versions={"engine": 1, "gate": 1, "retry": 1},
        processor_code_identity=HEX,
        lock_digest=HEX,
        model_id="amazon/chronos-2",
        model_artifact_digest=HEX,
        boundary_schema_digests=boundary_schema_digests(),
        instance_type="ml.m5.xlarge",
        max_runtime_seconds=3600,
    )


def _meta(shards: int, entries: tuple[RosterEntryV1, ...]) -> GenerationMeta:
    attempts = tuple(
        ShardPublishResult(
            shard=shard,
            attempt_id="r12a34",
            series_sha256=HEX,
            item_count=2_000,
            roster_part_key=f"runs/run-1/shards/{shard}/attempts/r12a34/roster-part.jsonl.gz",
            roster_part_sha256=HEX,
        )
        for shard in range(shards)
    )
    index = dict.fromkeys(CLOSED_FILTERS, FilterPages(page_count=4_000, entry_count=200_000))
    return GenerationMeta(
        set_id="0123456789ab",
        run_id="run-1",
        receipt_sk="RUN#run-1#r0#commit",
        input_end=INPUT_END,
        run_fingerprint=HEX,
        run_identity=_identity(),
        attempts=attempts,
        currency_codes=("EUR", "USD"),
        counts_by_reason=dict.fromkeys((f.split(":")[-1] for f in CLOSED_FILTERS), 200_000),
        counts_by_model={"chronos2": 200_000, "seasonal_naive": 200_000, "ets": 200_000},
        pages=ListPages((ListPage("all", 1, entries),), index),
    )


def test_a_401_byte_title_is_cut_to_fit_and_labelled_truncated() -> None:
    capped = cap_title("a" * 401, TITLE_MAX)
    assert capped.truncated is True
    assert capped.title == "a" * 397 + "…"
    assert len(cast("str", capped.title).encode("utf-8")) == 400


def test_a_4000_byte_title_is_cut_to_the_same_400_bytes() -> None:
    capped = cap_title("b" * 4_000, TITLE_MAX)
    assert capped.truncated is True
    assert capped.title == "b" * 397 + "…"


def test_a_multibyte_title_is_cut_on_a_code_point_boundary() -> None:
    capped = cap_title(WIDE * 300, TITLE_MAX)
    assert capped.title == WIDE * 198 + "…"
    assert len(cast("str", capped.title).encode("utf-8")) == 399


def test_a_title_within_the_cap_and_a_missing_title_are_kept_unlabelled() -> None:
    assert cap_title("a" * 400, TITLE_MAX).truncated is False
    assert cap_title("a" * 400, TITLE_MAX).title == "a" * 400
    assert cap_title(None, TITLE_MAX).title is None
    assert cap_title(None, TITLE_MAX).truncated is False


def test_a_full_page_of_50_entries_with_capped_titles_is_under_the_ceiling() -> None:
    entries = _worst_entries()
    item = page_item(GENERATION, ListPage("withheld:ACCURACY_NOT_TESTABLE", 4_000, entries))
    assert len(cast("list[object]", item["entries"])) == 50
    assert item_size(item) < CEILING


def test_a_meta_with_100_shards_every_filter_and_a_full_first_page_is_under_the_ceiling() -> None:
    item = meta_item(_meta(100, _worst_entries()))
    assert len(cast("list[object]", item["attempts"])) == 100
    assert set(cast("dict[str, object]", item["pages"])) == set(CLOSED_FILTERS)
    assert item["item_count"] == 200_000
    assert item_size(item) < CEILING


def test_an_asin_item_with_a_capped_title_is_under_the_ceiling() -> None:
    capped = cap_title(WIDE * 1_000, TITLE_MAX)
    item = asin_item(GENERATION, GenerationSeries(_candidate(1), capped.title, capped.truncated))
    assert item["title_truncated"] is True
    assert item_size(item) < CEILING


@pytest.mark.covers("ADR-163")
def test_an_asin_item_stores_its_unranked_list_row_with_the_capped_title() -> None:
    capped = cap_title(WIDE * 1_000, TITLE_MAX)
    series = GenerationSeries(_candidate(1), capped.title, capped.truncated)
    row = cast("dict[str, Any]", asin_item(GENERATION, series)["roster"])
    expected = roster_entry(_candidate(1), capped.title, title_truncated=True)
    assert row == expected.model_dump(mode="json")
    assert (row["rank"], row["title_truncated"]) == (None, True)
    assert row["targets"]["units_ordered"]["p50_totals"]["13"] == "160493.8257"


def test_every_asin_and_page_item_has_its_own_partition_key() -> None:
    first, second = (
        asin_item(GENERATION, GenerationSeries(_candidate(n), None, False)) for n in (1, 2)
    )
    assert first["PK"] == f"GEN#{GENERATION}#ASIN#{display_asin(asin(1))}"
    assert first["PK"] != second["PK"]
    assert asin_key(GENERATION, "B000000001") == f"GEN#{GENERATION}#ASIN#B000000001"
    assert page_key(GENERATION, "shown", 7) == f"GEN#{GENERATION}#PAGE#shown#00007"
    assert page_key(GENERATION, "shown", 99_999).endswith("#99999")


@pytest.mark.parametrize("page", [0, 100_000])
def test_a_page_number_outside_five_digits_is_refused(page: int) -> None:
    with pytest.raises(ValueError, match="outside 1 to 99999"):
        page_key(GENERATION, "all", page)


def test_an_over_ceiling_item_is_refused_before_any_item_of_the_call_is_written() -> None:
    table = ForecastTableFake()
    store = DynamoDbForecastStore("forecast-table", 20_000, cast("Any", table))
    small = ListPage("shown", 1, _worst_entries()[:1])
    large = ListPage("all", 1, _worst_entries())
    with pytest.raises(ForecastItemTooLargeError) as refused:
        store.put_list_pages(GENERATION, (small, large))
    assert refused.value.key == f"GEN#{GENERATION}#PAGE#all#00001"
    assert refused.value.size > 20_000
    assert refused.value.limit == 20_000
    assert table.puts == []
    assert store.put_list_pages(GENERATION, (small,)).written == 1


def test_the_same_item_built_twice_carries_the_same_digest() -> None:
    first = asin_item(GENERATION, GenerationSeries(_candidate(1), "Title", False))
    again = asin_item(GENERATION, GenerationSeries(_candidate(1), "Title", False))
    other = asin_item(GENERATION, GenerationSeries(_candidate(1), "Other", False))
    assert first["item_sha256"] == again["item_sha256"]
    assert first["item_sha256"] != other["item_sha256"]


# ---- the weekly slot marker (F-A8 ruling, 2026-11-16) ----

SLOT_DAY = INPUT_END + timedelta(days=5)


def _claim(execution: str, run_id: str = "a1b2c3d4e5f6-slot") -> SlotClaimRequest:
    return SlotClaimRequest(
        set_id="a1b2c3d4e5f6",
        slot_date=SLOT_DAY,
        run_id=run_id,
        execution_name=execution,
        scheduled_time=datetime.combine(SLOT_DAY, time(13), UTC),
    )


def test_the_first_claim_writes_the_marker_under_its_own_partition_on_absence() -> None:
    table = ForecastTableFake()
    store = DynamoDbForecastStore("forecast-table", CEILING, cast("Any", table))
    result = store.claim_slot(_claim("exec-1"))
    assert (result.claimed, result.holder_run_id) == (True, "a1b2c3d4e5f6-slot")
    (put,) = table.puts
    assert put["ConditionExpression"] == "attribute_not_exists(#pk)"
    assert put["Item"]["PK"] == {"S": "SLOT#a1b2c3d4e5f6"}
    assert put["Item"]["SK"] == {"S": SLOT_DAY.isoformat()}
    assert put["Item"]["execution_name"] == {"S": "exec-1"}


def test_another_execution_finds_the_slot_held_and_the_marker_unchanged() -> None:
    table = ForecastTableFake()
    store = DynamoDbForecastStore("forecast-table", CEILING, cast("Any", table))
    store.claim_slot(_claim("exec-1"))
    result = store.claim_slot(_claim("exec-2", run_id="a1b2c3d4e5f6-other"))
    assert (result.claimed, result.holder_run_id) == (False, "a1b2c3d4e5f6-slot")
    (marker,) = table.items.values()
    assert marker["execution_name"] == {"S": "exec-1"}
    assert table.gets[-1]["ConsistentRead"] is True


def test_the_same_execution_re_claiming_still_holds_the_slot() -> None:
    table = ForecastTableFake()
    store = DynamoDbForecastStore("forecast-table", CEILING, cast("Any", table))
    store.claim_slot(_claim("exec-1"))
    assert store.claim_slot(_claim("exec-1")).claimed is True
    assert len(table.items) == 1


def test_a_throttled_claim_raises_the_typed_throttle_and_writes_nothing() -> None:
    table = ForecastTableFake(throttle_puts=True)
    store = DynamoDbForecastStore("forecast-table", CEILING, cast("Any", table))
    with pytest.raises(ForecastStoreThrottled):
        store.claim_slot(_claim("exec-1"))
    assert table.items == {}
