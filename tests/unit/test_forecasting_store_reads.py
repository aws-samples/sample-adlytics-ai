# SPDX-License-Identifier: MIT-0
"""The read API's store readers over the items the publisher writes (ADR-163).

A reader that accepts a malformed item would serve a partial or wrong forecast; one that reads
eventually consistent data could serve two generations in one answer. Items here are built by the
publisher's own item functions; the table fake records each `GetItem` request.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from boto3.dynamodb.types import TypeSerializer
from forecast_read_support import GENERATION, INPUT_END, candidate, item, ranked
from forecast_store_support import ForecastTableFake

from adapters.outbound.dynamodb.demand_forecast_store import (
    DynamoDbForecastStore,
    asin_item,
    page_item,
)
from domain.forecasting.list_pages import CLOSED_FILTERS, ListPage
from ports.outbound.forecast_store import (
    ForecastStoreIncomplete,
    GenerationSeries,
    ReadListPageRequest,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-163")]

_SERIALIZER = TypeSerializer()


def _wire(stored: dict[str, object]) -> dict[str, Any]:
    return {name: _SERIALIZER.serialize(value) for name, value in stored.items()}


def _meta(**overrides: object) -> dict[str, object]:
    entries = ranked([item(1), item(2)])
    stored: dict[str, object] = {
        "PK": f"GEN#{GENERATION}",
        "SK": "META",
        "generation_id": GENERATION,
        "input_end": INPUT_END.isoformat(),
        "first_forecast_week": "2026-11-08",
        "item_count": 2,
        "currency_codes": ["USD"],
        "counts_by_reason": {},
        "counts_by_model": {"units_ordered.chronos2": 2},
        "first_page": [entry.model_dump(mode="json") for entry in entries],
        "pages": {
            name: {"page_count": 1, "entry_count": 2 if name in ("all", "shown") else 0}
            for name in CLOSED_FILTERS
        },
    }
    stored.update(overrides)
    return stored


def _store(*stored: dict[str, object]) -> tuple[DynamoDbForecastStore, ForecastTableFake]:
    table = ForecastTableFake()
    for one in stored:
        wired = _wire(one)
        table.items[(wired["PK"]["S"], wired["SK"]["S"])] = wired
    return DynamoDbForecastStore("forecast-table", 350_000, cast("Any", table)), table


def test_meta_parses_with_every_filter_and_reads_strongly_consistent() -> None:
    store, table = _store(_meta())
    view = store.read_generation(GENERATION)
    assert view is not None
    assert (view.item_count, view.currency_codes, len(view.first_page)) == (2, ("USD",), 2)
    assert set(view.pages) == set(CLOSED_FILTERS)
    assert view.pages["shown"].entry_count == 2
    (request,) = table.gets
    assert request["ConsistentRead"] is True
    assert request["Key"] == {"PK": {"S": f"GEN#{GENERATION}"}, "SK": {"S": "META"}}


def test_a_missing_meta_is_none() -> None:
    store, _ = _store()
    assert store.read_generation(GENERATION) is None


@pytest.mark.parametrize(
    "override",
    [
        {"pages": {"all": {"page_count": 1, "entry_count": 2}}},
        {"first_page": [{"schema_version": 1}]},
        {"counts_by_model": {"units_ordered.chronos2": -1}},
        {"currency_codes": [1]},
    ],
)
def test_meta_missing_a_filter_or_failing_a_field_is_store_incomplete(
    override: dict[str, object],
) -> None:
    store, _ = _store(_meta(**override))
    with pytest.raises(ForecastStoreIncomplete) as refused:
        store.read_generation(GENERATION)
    assert refused.value.key == f"GEN#{GENERATION}"


def test_a_page_item_parses_its_entries_and_a_missing_page_is_none() -> None:
    entries = ranked([item(1), item(2)])
    store, table = _store(page_item(GENERATION, ListPage("shown", 1, entries)))
    found = store.read_list_page(ReadListPageRequest(GENERATION, "shown", 1))
    assert found.entries == entries
    assert store.read_list_page(ReadListPageRequest(GENERATION, "shown", 2)).entries is None
    assert table.gets[-1]["Key"]["PK"] == {"S": f"GEN#{GENERATION}#PAGE#shown#00002"}


def test_a_page_item_naming_another_page_is_store_incomplete() -> None:
    stored = page_item(GENERATION, ListPage("shown", 1, ranked([item(1)])))
    stored["PK"] = f"GEN#{GENERATION}#PAGE#shown#00002"
    store, _ = _store(stored)
    with pytest.raises(ForecastStoreIncomplete):
        store.read_list_page(ReadListPageRequest(GENERATION, "shown", 2))


@pytest.mark.parametrize(("name", "page"), [("everything", 1), ("all", 0), ("all", 100_000)])
def test_a_page_request_outside_the_closed_filters_or_pages_is_refused(
    name: str, page: int
) -> None:
    with pytest.raises(ValueError, match=r"closed list filter|outside 1 to 99999"):
        ReadListPageRequest(GENERATION, name, page)


def test_an_asin_item_parses_with_its_stored_row() -> None:
    series = GenerationSeries(candidate(1), "Fictional product 1", title_truncated=False)
    store, _ = _store(asin_item(GENERATION, series))
    view = store.read_asin(GENERATION, "B0FIXTUR01")
    assert view is not None
    assert view.roster == series.roster
    assert (view.title, view.title_truncated) == ("Fictional product 1", False)
    assert view.candidate == candidate(1)
    assert store.read_asin(GENERATION, "B0FIXTUR02") is None


def test_an_asin_item_without_its_list_row_is_store_incomplete() -> None:
    """A generation published before slice 3 has no row; the reader never sums one (R19)."""
    stored = asin_item(GENERATION, GenerationSeries(candidate(1), None, title_truncated=False))
    del stored["roster"]
    store, _ = _store(stored)
    with pytest.raises(ForecastStoreIncomplete) as refused:
        store.read_asin(GENERATION, "B0FIXTUR01")
    assert refused.value.key == f"GEN#{GENERATION}#ASIN#B0FIXTUR01"


def test_an_asin_item_with_a_malformed_series_is_store_incomplete() -> None:
    stored = asin_item(GENERATION, GenerationSeries(candidate(1), None, title_truncated=False))
    stored["series"] = {"schema_version": 1}
    store, _ = _store(stored)
    with pytest.raises(ForecastStoreIncomplete):
        store.read_asin(GENERATION, "B0FIXTUR01")
