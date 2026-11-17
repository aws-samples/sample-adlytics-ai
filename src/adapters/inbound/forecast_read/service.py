# SPDX-License-Identifier: MIT-0
"""The read service the HTTP routes and the forecast tools share (ADR-163).

Callers may rely on:

- every request reads `CURRENT` once (round 1), then at most one concurrent round of items keyed
  by the generation it names (round 2), so a publish between rounds never mixes generations;
- `items_read` is the number of `GetItem` calls the request's plan made, counted per request;
- each round runs in its own short-lived thread pool, waited on until the request's deadline and
  shut down without waiting on every exit, so a blocked read can never hold a later request;
- every store failure leaves as a coded error: a throttle, a client or connection error, or the
  deadline is `FORECAST_STORE_UNAVAILABLE` (503); an item that fails validation, or a `META` the
  pointer names but the table lacks, is `FORECAST_STORE_INCOMPLETE` (503).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from botocore.exceptions import BotoCoreError, ClientError

from adapters.config import DemandForecastReaderValues
from adapters.outbound.dynamodb.items import ItemError
from domain.forecasting import list_pages, read_api
from domain.forecasting.boundary import ForecastSetV1, RosterEntryV1, Target
from domain.forecasting.policy import DemandForecastPolicy
from domain.forecasting.publish import CurrentPointer, forecast_set_id
from domain.forecasting.read_api import (
    AsinItemView,
    DemandForecastDetailV1,
    DemandForecastListV1,
    DemandForecastStatusV1,
    DetailSource,
    GenerationView,
    ListRequest,
    ListSource,
    StatusSource,
)
from domain.forecasting.summaries import require_shared_currency
from domain.shared_kernel.errors import DependencyError, InvariantError
from ports.outbound.forecast_store import (
    ForecastReaderPort,
    ForecastStoreIncomplete,
    ForecastStoreThrottled,
    ReadListPageRequest,
)

RESPONSE_RESERVE_SECONDS: Final = 1.0
"""Time kept from the function's own timeout to shape the response, report and flush metrics."""


@dataclass(frozen=True)
class ReadContext:
    """One request's clock, read once, and its deadline in `time.monotonic()` seconds."""

    now: datetime
    deadline: float

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("now is timezone-aware")

    @classmethod
    def for_invocation(cls, now: datetime, remaining_ms: int) -> ReadContext:
        """Build the context from the Lambda's remaining time, less the response reserve."""
        return cls(now, time.monotonic() + remaining_ms / 1000 - RESPONSE_RESERVE_SECONDS)


@dataclass(frozen=True)
class ReadResult[T]:
    """A response and the number of `GetItem` calls its plan made."""

    response: T
    items_read: int


@dataclass(frozen=True)
class AsinRowsRequest:
    """The tools' per-product read: unique display ASINs and the target answered."""

    asins: tuple[str, ...]
    target: Target

    def __post_init__(self) -> None:
        if not self.asins or len(set(self.asins)) != len(self.asins):
            raise ValueError("asins are one or more unique display ASINs")


@dataclass(frozen=True)
class AsinRows:
    """The pointer and one parsed item, or None, per requested ASIN in request order."""

    pointer: CurrentPointer
    items: tuple[AsinItemView | None, ...]


@dataclass(frozen=True)
class ListLookupRequest:
    """`forecast_list_asins`: page 1 of one filter, or one exact ASIN."""

    filter: str
    target: Target
    asin: str | None = None

    def __post_init__(self) -> None:
        if self.filter not in list_pages.CLOSED_FILTERS:
            raise ValueError(f"{self.filter!r} is not a closed list filter")
        if self.asin is not None and self.filter != list_pages.ALL:
            raise ValueError("an exact-ASIN lookup takes no status filter")


@dataclass(frozen=True)
class ListLookup:
    """Page 1 of the filter with its counts, or the one product's item for an exact lookup."""

    pointer: CurrentPointer
    generation: GenerationView | None
    entries: tuple[RosterEntryV1, ...]
    item: AsinItemView | None


class ForecastReadDeadline(Exception):  # noqa: N818 - names the event, as the cause does
    """A round was not complete when the request's deadline passed."""


def _unavailable(cause: str) -> DependencyError:
    return DependencyError(
        read_api.FORECAST_STORE_UNAVAILABLE,
        "The demand forecast store could not be read.",
        context={"cause": cause},
    )


def _incomplete(key: str) -> InvariantError:
    return InvariantError(
        read_api.FORECAST_STORE_INCOMPLETE,
        f"The demand forecast store is incomplete at {key}.",
        context={"key": key},
    )


class ForecastReadService:
    """The §3.6 read plans under a per-request deadline, then the pure builders."""

    def __init__(
        self,
        reader: ForecastReaderPort,
        values: DemandForecastReaderValues,
        policy: DemandForecastPolicy,
    ) -> None:
        self._reader = reader
        self._values = values
        self._policy = policy
        self._set_id = forecast_set_id(
            ForecastSetV1(
                marketplace_key=values.marketplace_key,
                distributor_view=policy.input.distributor_view,
                selling_program=policy.input.selling_program,
            )
        )

    @property
    def set_id(self) -> str:
        """The forecast set this service reads, composed as the publisher composes it."""
        return self._set_id

    def list_page(
        self, request: ListRequest, context: ReadContext
    ) -> ReadResult[DemandForecastListV1]:
        """Serve one stored page of the ranked roster (2 or 3 items)."""
        pointer = self._current(context)
        serving = self._policy.serving
        generation_id = pointer.generation_id
        if generation_id is None:
            return ReadResult(read_api.build_unavailable_list(request, serving, context.now), 1)
        reads: dict[str, Callable[[], object]] = {
            "meta": lambda: self._reader.read_generation(generation_id)
        }
        if request.filter != list_pages.ALL or request.page != 1:
            page = ReadListPageRequest(generation_id, request.filter, request.page)
            reads["page"] = lambda: self._reader.read_list_page(page).entries
        found = _round(reads, context)
        meta = _meta(found["meta"], generation_id)
        entries = found.get("page")
        source = ListSource(request, pointer, meta, _entries(entries), serving)
        return ReadResult(read_api.build_list(source, context.now), 1 + len(reads))

    def asin_detail(self, asin: str, context: ReadContext) -> ReadResult[DemandForecastDetailV1]:
        """Serve one product's stored forecast (2 items), or the designed absence (1)."""
        pointer = self._current(context)
        serving = self._policy.serving
        generation_id = pointer.generation_id
        if generation_id is None:
            source = DetailSource(asin, pointer, None, serving)
            return ReadResult(read_api.build_detail(source, context.now), 1)
        found = _round({"asin": lambda: self._reader.read_asin(generation_id, asin)}, context)
        source = DetailSource(asin, pointer, _item(found["asin"]), serving)
        return ReadResult(read_api.build_detail(source, context.now), 2)

    def status(self, context: ReadContext) -> ReadResult[DemandForecastStatusV1]:
        """Serve the generation, freshness, last forecast run and next checks (1 item)."""
        source = StatusSource(
            self._current(context),
            self._policy.serving,
            self._values.check_schedule,
            self._policy.retry.admission_weekday,
        )
        return ReadResult(read_api.build_status(source, context.now), 1)

    def asin_rows(self, request: AsinRowsRequest, context: ReadContext) -> ReadResult[AsinRows]:
        """Read up to `compare_max_asins` products' items concurrently (at most 6 items)."""
        if len(request.asins) > self._policy.serving.compare_max_asins:
            raise ValueError("more ASINs than serving.compare_max_asins")
        pointer = self._current(context)
        generation_id = pointer.generation_id
        if generation_id is None:
            raise read_api.not_published()
        reader, generation = self._reader, generation_id
        reads: dict[str, Callable[[], object]] = {
            asin: (lambda asin=asin: reader.read_asin(generation, asin)) for asin in request.asins
        }
        found = _round(reads, context)
        items = tuple(_item(found[asin]) for asin in request.asins)
        require_shared_currency(
            request.target,
            (view.roster.targets[request.target] for view in items if view is not None),
        )
        return ReadResult(AsinRows(pointer, items), 1 + len(reads))

    def list_lookup(
        self, request: ListLookupRequest, context: ReadContext
    ) -> ReadResult[ListLookup]:
        """Read page 1 of a filter with its counts (2 or 3 items), or one exact ASIN (2)."""
        pointer = self._current(context)
        generation_id = pointer.generation_id
        if generation_id is None:
            raise read_api.not_published()
        if request.asin is not None:
            asin = request.asin
            found = _round({"asin": lambda: self._reader.read_asin(generation_id, asin)}, context)
            item = _item(found["asin"])
            if item is not None:
                require_shared_currency(request.target, [item.roster.targets[request.target]])
            return ReadResult(ListLookup(pointer, None, (), item), 2)
        reads: dict[str, Callable[[], object]] = {
            "meta": lambda: self._reader.read_generation(generation_id)
        }
        if request.filter != list_pages.ALL:
            page = ReadListPageRequest(generation_id, request.filter, 1)
            reads["page"] = lambda: self._reader.read_list_page(page).entries
        found = _round(reads, context)
        meta = _meta(found["meta"], generation_id)
        source = ListSource(
            ListRequest(request.filter, 1),
            pointer,
            meta,
            _entries(found.get("page")),
            self._policy.serving,
        )
        if pointer.generation_id != meta.generation_id:
            raise _incomplete(f"GEN#{generation_id}")
        entries = read_api.page_entries(source)
        require_shared_currency(
            request.target, (entry.targets[request.target] for entry in entries)
        )
        return ReadResult(ListLookup(pointer, meta, entries, None), 1 + len(reads))

    def _current(self, context: ReadContext) -> CurrentPointer:
        found = _round({"current": lambda: self._reader.get_current(self._set_id)}, context)
        pointer = found["current"]
        if pointer is None:
            return CurrentPointer(fence=0)
        if not isinstance(pointer, CurrentPointer):
            raise _incomplete("CURRENT")
        return pointer


def _meta(found: object, generation_id: str) -> GenerationView:
    if found is None:
        raise _incomplete(f"GEN#{generation_id}")
    if not isinstance(found, GenerationView):
        raise _incomplete(f"GEN#{generation_id}")
    return found


def _entries(found: object) -> tuple[RosterEntryV1, ...] | None:
    return found if isinstance(found, tuple) else None  # pyright: ignore[reportUnknownVariableType]


def _item(found: object) -> AsinItemView | None:
    return found if isinstance(found, AsinItemView) else None


def _round(reads: Mapping[str, Callable[[], object]], context: ReadContext) -> dict[str, object]:
    """Run one round concurrently until the deadline; map every store failure to its code."""
    if context.deadline <= time.monotonic():
        raise _unavailable("deadline")
    executor = ThreadPoolExecutor(max_workers=len(reads))
    try:
        futures = {key: executor.submit(read) for key, read in reads.items()}
        remaining = context.deadline - time.monotonic()
        _done, pending = wait(futures.values(), timeout=max(0.0, remaining))
        if pending:
            raise ForecastReadDeadline
        return {key: future.result() for key, future in futures.items()}
    except ForecastReadDeadline:
        raise _unavailable("deadline") from None
    except (ForecastStoreThrottled, ClientError, BotoCoreError) as error:
        raise _unavailable(type(error).__name__) from None
    except ForecastStoreIncomplete as error:
        raise _incomplete(error.key) from None
    except ItemError:
        raise _incomplete("CURRENT") from None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
