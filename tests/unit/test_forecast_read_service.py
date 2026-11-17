# SPDX-License-Identifier: MIT-0
"""The shared demand forecast read service: read plans, deadlines and store failures (ADR-163).

A plan that reads more items than its row in the read-plan table breaks the latency budget at
catalogue scale; a read that mixes two generations shows an answer no publish produced; a blocked
read that holds a warm function makes every later request wait; a store failure without its code
reaches the user as a generic 500. The fake reader applies the port's own request validation.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from forecast_read_support import (
    EMPTY_POINTER,
    GENERATION,
    NOW,
    SERVING,
    generation,
    item,
    pages,
    pointer,
    ranked,
)
from forecast_series_support import policy

from adapters.config import DemandForecastReaderValues
from adapters.inbound.forecast_read.service import (
    AsinRowsRequest,
    ForecastReadService,
    ListLookupRequest,
    ReadContext,
    ReadResult,
)
from adapters.outbound.dynamodb.items import ItemError
from domain.forecasting import list_pages, read_api
from domain.forecasting.boundary import ForecastSetV1, RosterEntryV1
from domain.forecasting.list_pages import FilterPages
from domain.forecasting.publish import CurrentPointer, forecast_set_id
from domain.forecasting.read_api import AsinItemView, GenerationView, ListRequest
from domain.shared_kernel.errors import AdlyticsError, DependencyError, DomainError, InvariantError
from ports.outbound.forecast_store import (
    ForecastStoreIncomplete,
    ForecastStoreThrottled,
    ReadListPageRequest,
    ReadListPageResult,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-163")]

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


def _context(seconds: float = 5.0) -> ReadContext:
    return ReadContext(NOW, time.monotonic() + seconds)


def _reader() -> FakeReader:
    views = [item(1), item(2, p50="20.0000"), item(3, shown=False)]
    entries = ranked(views)
    return FakeReader(
        current=pointer(),
        meta=generation(entries),
        stored_pages=pages(entries),
        items={view.candidate.display_asin: view for view in views},
    )


def _service(reader: FakeReader) -> ForecastReadService:
    return ForecastReadService(reader, VALUES, policy())


def test_the_set_id_is_composed_as_the_publisher_composes_it() -> None:
    declared = policy().input
    expected = forecast_set_id(
        ForecastSetV1(
            marketplace_key="fictional-marketplace",
            distributor_view=declared.distributor_view,
            selling_program=declared.selling_program,
        )
    )
    assert _service(_reader()).set_id == expected
    assert len(expected) == 12


def _run(plan: str, service: ForecastReadService, context: ReadContext) -> int:
    five = tuple(f"B0FIXTUR0{n}" for n in range(1, 6))
    plans: dict[str, Callable[[], ReadResult[Any]]] = {
        "list page 1": lambda: service.list_page(ListRequest("all", 1), context),
        "list filtered": lambda: service.list_page(ListRequest("shown", 1), context),
        "detail": lambda: service.asin_detail("B0FIXTUR01", context),
        "status": lambda: service.status(context),
        "tool list": lambda: service.list_lookup(
            ListLookupRequest("all", "units_ordered"), context
        ),
        "tool list filtered": lambda: service.list_lookup(
            ListLookupRequest("shown", "units_ordered"), context
        ),
        "tool list by asin": lambda: service.list_lookup(
            ListLookupRequest("all", "units_ordered", "B0FIXTUR02"), context
        ),
        "tool rows": lambda: service.asin_rows(AsinRowsRequest(five, "units_ordered"), context),
    }
    return plans[plan]().items_read


@pytest.mark.parametrize(
    ("plan", "items"),
    [
        ("list page 1", 2),
        ("list filtered", 3),
        ("detail", 2),
        ("status", 1),
        ("tool list", 2),
        ("tool list filtered", 3),
        ("tool list by asin", 2),
        ("tool rows", 6),
    ],
)
def test_each_plan_reads_the_items_its_read_plan_row_names(plan: str, items: int) -> None:
    reader = _reader()
    assert _run(plan, _service(reader), _context()) == items
    assert len(reader.calls) == items


def test_a_later_page_reads_meta_and_its_page_item_concurrently() -> None:
    reader = _reader()
    with pytest.raises(AdlyticsError) as refused:
        _service(reader).list_page(ListRequest("all", 2), _context())
    assert str(refused.value.code) == "ADLY-FORECAST_READ-0003"
    assert sorted(kind for kind, _ in reader.calls) == ["current", "meta", "page"]


def test_without_a_generation_every_route_reads_only_current() -> None:
    reader = FakeReader(current=None)
    service = _service(reader)
    assert service.list_page(ListRequest("shown", 2), _context()).items_read == 1
    assert service.asin_detail("B0FIXTUR01", _context()).response.outcome == "unavailable"
    assert service.status(_context()).response.generation is None
    assert [kind for kind, _ in reader.calls] == ["current", "current", "current"]
    with pytest.raises(DomainError) as refused:
        service.asin_rows(AsinRowsRequest(("B0FIXTUR01",), "units_ordered"), _context())
    assert str(refused.value.code) == "ADLY-FORECAST_READ-0007"


def test_page_1000_of_a_50000_product_generation_reads_exactly_three_items() -> None:
    rows = tuple(item(n).roster.model_copy(update={"rank": 49_950 + n}) for n in range(1, 51))
    index = dict.fromkeys(list_pages.CLOSED_FILTERS, FilterPages(page_count=1, entry_count=0))
    index["all"] = FilterPages(page_count=1_000, entry_count=50_000)
    meta = replace(generation(()), item_count=50_000, pages=index, first_page=())
    reader = FakeReader(current=pointer(), meta=meta, stored_pages={("all", 1_000): rows})
    result = _service(reader).list_page(ListRequest("all", 1_000), _context())
    assert result.items_read == 3
    assert (result.response.page, result.response.page_count, result.response.match_count) == (
        1_000,
        1_000,
        50_000,
    )
    assert [entry.rank for entry in result.response.entries][:2] == [49_951, 49_952]
    assert sorted(kind for kind, _ in reader.calls) == ["current", "meta", "page"]


def test_round_two_reads_the_generation_round_one_named_after_a_publish() -> None:
    reader = _reader()
    service = _service(reader)
    original = reader.get_current

    def current_then_publish(set_id: str) -> CurrentPointer | None:
        found = original(set_id)
        reader.current = replace(pointer(), generation_id="run-2-0123456789ab")
        return found

    reader.get_current = current_then_publish  # type: ignore[method-assign]
    result = service.asin_detail("B0FIXTUR01", _context())
    assert result.response.provenance is not None
    assert result.response.provenance.generation_id == GENERATION
    assert ("asin", f"{GENERATION}#B0FIXTUR01") in reader.calls


def test_a_read_blocked_past_the_deadline_is_unavailable_and_never_holds_the_next_request() -> None:
    """H3: a warm function reuses the service, so a blocked thread must not delay the next call."""
    blocked = _reader()
    gate = threading.Event()
    blocked.block = gate
    service = _service(blocked)
    try:
        started = time.monotonic()
        with pytest.raises(DependencyError) as refused:
            service.asin_detail("B0FIXTUR01", _context(0.2))
        assert str(refused.value.code) == "ADLY-FORECAST_READ-0009"
        assert refused.value.context == {"cause": "deadline"}
        assert time.monotonic() - started < 2.0
        warm = time.monotonic()
        assert service.status(_context()).items_read == 1
        assert time.monotonic() - warm < 1.0
    finally:
        gate.set()


@pytest.mark.parametrize(
    ("failure", "code", "cause"),
    [
        (
            ForecastStoreThrottled("ThrottlingException", 0),
            "ADLY-FORECAST_READ-0009",
            "ForecastStoreThrottled",
        ),
        (
            ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem"),
            "ADLY-FORECAST_READ-0009",
            "ClientError",
        ),
        (
            ReadTimeoutError(endpoint_url="https://dynamodb"),
            "ADLY-FORECAST_READ-0009",
            "ReadTimeoutError",
        ),
        (
            ForecastStoreIncomplete("GEN#x#ASIN#B0FIXTUR01", "ValidationError"),
            "ADLY-FORECAST_READ-0008",
            None,
        ),
        (ItemError("CURRENT: fence"), "ADLY-FORECAST_READ-0008", None),
    ],
)
def test_each_store_failure_leaves_with_its_forecast_code(
    failure: Exception, code: str, cause: str | None
) -> None:
    reader = _reader()
    reader.fail = failure
    with pytest.raises(AdlyticsError) as refused:
        _service(reader).asin_detail("B0FIXTUR01", _context())
    assert str(refused.value.code) == code
    expected = DependencyError if cause else InvariantError
    assert isinstance(refused.value, expected)
    if cause:
        assert refused.value.context == {"cause": cause}


def test_a_meta_the_pointer_names_but_the_table_lacks_is_store_incomplete() -> None:
    reader = _reader()
    reader.meta = None
    with pytest.raises(InvariantError) as refused:
        _service(reader).list_page(ListRequest("all", 1), _context())
    assert str(refused.value.code) == "ADLY-FORECAST_READ-0008"


def test_a_money_answer_over_two_shown_currencies_is_refused_and_units_answer() -> None:
    reader = _reader()
    reader.items["B0FIXTUR02"] = item(2, currency="EUR")
    reader.items["B0FIXTUR04"] = item(4, currency="EUR", shown=False)
    service = _service(reader)
    with pytest.raises(DomainError) as refused:
        service.asin_rows(
            AsinRowsRequest(("B0FIXTUR01", "B0FIXTUR02"), "revenue_shipped"), _context()
        )
    assert str(refused.value.code) == "ADLY-FORECAST_READ-0006"
    assert refused.value.context == {"currencies": ["EUR", "USD"]}
    units = service.asin_rows(
        AsinRowsRequest(("B0FIXTUR01", "B0FIXTUR02"), "units_ordered"), _context()
    )
    assert [view is not None for view in units.response.items] == [True, True]
    withheld = service.asin_rows(
        AsinRowsRequest(("B0FIXTUR01", "B0FIXTUR04"), "revenue_shipped"), _context()
    )
    assert withheld.items_read == 3


def test_an_empty_current_is_the_unpublished_pointer() -> None:
    reader = FakeReader(current=EMPTY_POINTER)
    assert _service(reader).status(_context()).response.freshness.state == "unavailable"
    assert SERVING.compare_max_asins == 5
