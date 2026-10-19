# SPDX-License-Identifier: MIT-0
"""Programmatic span retrieval from `aws/spans` (impl-plan S6 item 5a; owner Items 16 and 13c).

One reusable lookup replaces every probe's own Logs Insights query: by trace ID, by business keys
the span contract declares, or by span name, over an explicit window. The result is structured
JSON with an explicit `pending` state for the ingestion lag of `aws/spans`, never an empty pass.
"""

from adapters.jobs.span_retrieval.client import (
    SPAN_LOG_GROUP,
    LogsInsightsSpanClient,
    SpanRetrievalFailedError,
    retrieve_spans,
)
from adapters.jobs.span_retrieval.parse import parse_span_rows
from adapters.jobs.span_retrieval.query import (
    DEFAULT_LAG_SECONDS,
    SpanQuery,
    SpanQueryRefusedError,
    build_span_query,
    declared_span_attributes,
)

__all__ = [
    "DEFAULT_LAG_SECONDS",
    "SPAN_LOG_GROUP",
    "LogsInsightsSpanClient",
    "SpanQuery",
    "SpanQueryRefusedError",
    "SpanRetrievalFailedError",
    "build_span_query",
    "declared_span_attributes",
    "parse_span_rows",
    "retrieve_spans",
]
