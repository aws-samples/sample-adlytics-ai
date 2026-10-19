# SPDX-License-Identifier: MIT-0
"""Parse Logs Insights rows over `aws/spans` into JSON-ready span documents.

Each `aws/spans` record's `@message` is one exported span as a JSON object. A row whose message is
not a span (no W3C trace ID, span ID and name) is refused rather than skipped, so a reader never
mistakes a malformed export for a missing span.
"""

from __future__ import annotations

import json
import re
from typing import Final, cast

from domain.shared_kernel.errors import DependencyError, ErrorCode

__all__ = [
    "SPAN_DOCUMENT_MALFORMED",
    "SPAN_RETRIEVAL_FAILED",
    "SpanRetrievalFailedError",
    "parse_span_rows",
]

SPAN_RETRIEVAL_FAILED: Final = ErrorCode("SPAN_RETRIEVAL", 2)
"""The query did not start, ended Failed, Cancelled, Timeout or Unknown, or returned no rows."""
SPAN_DOCUMENT_MALFORMED: Final = ErrorCode("SPAN_RETRIEVAL", 3)
"""A returned record is not one exported span."""

_TRACE_ID: Final = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID: Final = re.compile(r"^[0-9a-f]{16}$")


class SpanRetrievalFailedError(DependencyError):
    """Logs Insights could not answer the lookup, or answered with something other than spans."""

    def __init__(self, code: ErrorCode, reason: str, description: str) -> None:
        super().__init__(code, description, context={"reason": reason})
        self.reason = reason


def _malformed(reason: str) -> SpanRetrievalFailedError:
    return SpanRetrievalFailedError(SPAN_DOCUMENT_MALFORMED, reason, "a span record is malformed.")


def _row_message(row: object) -> str:
    if not isinstance(row, list):
        raise _malformed("query_result_malformed")
    for field in cast("list[object]", row):
        if not isinstance(field, dict):
            raise _malformed("query_result_malformed")
        entry = cast("dict[str, object]", field)
        if entry.get("field") == "@message":
            value = entry.get("value")
            if not isinstance(value, str):
                raise _malformed("span_document_malformed")
            return value
    raise _malformed("span_message_missing")


def _document(message: str) -> dict[str, object]:
    try:
        raw: object = json.loads(message)
    except json.JSONDecodeError as error:
        raise _malformed("span_document_malformed") from error
    if not isinstance(raw, dict):
        raise _malformed("span_document_malformed")
    document = cast("dict[str, object]", raw)
    trace_id, span_id = document.get("traceId"), document.get("spanId")
    if not isinstance(trace_id, str) or _TRACE_ID.fullmatch(trace_id) is None:
        raise _malformed("span_identity_malformed")
    if not isinstance(span_id, str) or _SPAN_ID.fullmatch(span_id) is None:
        raise _malformed("span_identity_malformed")
    if not isinstance(document.get("name"), str):
        raise _malformed("span_name_missing")
    return document


def parse_span_rows(results: object) -> tuple[dict[str, object], ...]:
    """Return one span document per row of a `GetQueryResults` `results` list, in row order."""
    if not isinstance(results, list):
        raise _malformed("query_result_malformed")
    return tuple(_document(_row_message(row)) for row in cast("list[object]", results))
