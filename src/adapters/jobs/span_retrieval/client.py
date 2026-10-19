# SPDX-License-Identifier: MIT-0
"""Run one span lookup against Logs Insights on `aws/spans`, with bounded polling and pages.

Each page is one `StartQuery` over a sub-window, polled with `GetQueryResults` until `Complete` or
the poll bound. A page that returns `limit` rows may have been cut short, so its window is split
in two and both halves are read instead (window-splitting pagination). A full page that can't be
split, because it covers one second or the `max_pages` bound can't cover both halves, is kept and
the result is marked `truncated`. At most `max_pages` queries run.

The result's `status` is one of three states (plan B5):
- `pending`: a page had not reached `Complete` within the poll bound; or nothing was found and
  either the page bound stopped the read or the window ends inside the ingestion lag
  (`window_end > now - lag_seconds`);
- `empty`: nothing was found in a window older than the lag bound;
- `complete`: at least one matching span was found.
So an empty read inside the lag is never reported as an empty pass.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, cast

from adapters.jobs.span_retrieval.parse import (
    SPAN_RETRIEVAL_FAILED,
    SpanRetrievalFailedError,
    parse_span_rows,
)
from adapters.jobs.span_retrieval.query import SpanQuery, query_string

__all__ = [
    "SPAN_LOG_GROUP",
    "LogsInsightsSpanClient",
    "LogsQueryClient",
    "SpanRetrievalFailedError",
    "retrieve_spans",
]

SPAN_LOG_GROUP: Final = "aws/spans"
"""The account's Transaction Search span group, which every traced service exports to."""
_FAILED_STATES: Final = frozenset({"Failed", "Cancelled", "Timeout", "Unknown"})


type LogsQueryClient = Any
"""A boto3 `logs` client, or a fake with its `start_query` and `get_query_results`."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class LogsInsightsSpanClient:
    """A lookup runner over one Logs client, with explicit poll bounds and clocks."""

    logs: LogsQueryClient
    poll_seconds: float = 2.0
    timeout_seconds: float = 180.0
    sleep: Callable[[float], None] = field(default=time.sleep)
    monotonic: Callable[[], float] = field(default=time.monotonic)
    now: Callable[[], datetime] = field(default=_utc_now)

    def page(self, query: SpanQuery, start_second: int, end_second: int) -> list[object] | None:
        """Return one sub-window's rows, or None when it did not complete in the poll bound.

        `start_second` is inclusive and `end_second` exclusive; Logs Insights' `endTime` is
        inclusive, so the request ends one second earlier.
        """
        started = self.logs.start_query(
            logGroupName=SPAN_LOG_GROUP,
            startTime=start_second,
            endTime=end_second - 1,
            queryString=query_string(query),
            limit=query.limit,
        )
        query_id = started.get("queryId")
        if not isinstance(query_id, str) or not query_id:
            raise SpanRetrievalFailedError(
                SPAN_RETRIEVAL_FAILED, "query_start_failed", "the span query did not start."
            )
        deadline = self.monotonic() + self.timeout_seconds
        while True:
            result = self.logs.get_query_results(queryId=query_id)
            status = result.get("status")
            if status == "Complete":
                rows = result.get("results")
                if not isinstance(rows, list):
                    raise SpanRetrievalFailedError(
                        SPAN_RETRIEVAL_FAILED, "query_result_malformed", "the result is malformed."
                    )
                return cast("list[object]", rows)
            if status in _FAILED_STATES:
                raise SpanRetrievalFailedError(
                    SPAN_RETRIEVAL_FAILED, "query_failed", f"the span query ended {status}."
                )
            if self.monotonic() >= deadline:
                return None
            self.sleep(self.poll_seconds)

    def retrieve(self, query: SpanQuery) -> dict[str, object]:
        """Run the lookup and return its JSON-ready result."""
        windows = [
            (
                math.floor(query.window_start.timestamp()),
                math.ceil(query.window_end.timestamp()),
            )
        ]
        rows: list[object] = []
        pages = 0
        truncated = False
        running = False
        while windows:
            if pages >= query.max_pages:
                truncated = True
                break
            low, high = windows.pop()
            pages += 1
            found = self.page(query, low, high)
            if found is None:
                running = True
                break
            full = len(found) >= query.limit
            # Split only while the page bound still covers both halves and every queued window;
            # otherwise keep the rows already read and mark the lookup truncated.
            if full and high - low > 1 and pages + len(windows) + 2 <= query.max_pages:
                middle = (low + high) // 2
                # Read the earlier half first, so spans keep ascending time order.
                windows.extend([(middle, high), (low, middle)])
                continue
            truncated = truncated or full
            rows.extend(found)
        return _result(query, rows, _Progress(pages, truncated, running, self.now()))


@dataclass(frozen=True, slots=True)
class _Progress:
    pages: int
    truncated: bool
    running: bool
    now: datetime


def _result(query: SpanQuery, rows: list[object], progress: _Progress) -> dict[str, object]:
    spans: list[dict[str, object]] = []
    seen: set[tuple[object, object]] = set()
    excluded = 0
    for document in parse_span_rows(rows):
        identity = (document.get("traceId"), document.get("spanId"))
        if identity in seen:
            continue
        seen.add(identity)
        if query.matches(document):
            spans.append(document)
        else:
            excluded += 1
    inside_lag = query.window_end.timestamp() > progress.now.timestamp() - query.lag_seconds
    if progress.running:
        status, reason = "pending", "query_running"
    elif spans:
        status, reason = "complete", None
    elif progress.truncated:
        # The page bound stopped the read before the whole window was seen: never `empty`.
        status, reason = "pending", "page_bound_reached"
    elif inside_lag:
        status, reason = "pending", "inside_lag"
    else:
        status, reason = "empty", None
    return {
        "status": status,
        "reason": reason,
        "spans": spans,
        "window": {
            "start": query.window_start.isoformat(),
            "end": query.window_end.isoformat(),
            "lag_seconds": query.lag_seconds,
            "inside_lag": inside_lag,
        },
        "query": query.describe(),
        "pages": progress.pages,
        "truncated": progress.truncated,
        "excluded": excluded,
    }


def retrieve_spans(logs: LogsQueryClient, query: SpanQuery, **bounds: Any) -> dict[str, object]:
    """Run one lookup with a Logs client and optional poll bounds (`poll_seconds`, ...)."""
    return LogsInsightsSpanClient(logs, **bounds).retrieve(query)
