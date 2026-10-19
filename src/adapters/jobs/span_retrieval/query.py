# SPDX-License-Identifier: MIT-0
"""The pure span query: one validated lookup over an explicit window, and its Logs Insights text.

A lookup names at least one selector: a W3C trace ID, business keys, or span names. Every key must
be an attribute some span declares in `contracts/observability/spans.yaml`, so a lookup can never
filter on a field no emitter writes. Every value is checked against a closed character set before
it is rendered, so no input can change the query's shape. The window is half-open,
`[window_start, window_end)`, at Logs Insights' one-second resolution.

The query is also the exclusion predicate (`matches`): the client re-checks every returned span
against the trace, keys, names and environment, so a span of another trace, key or deployment is
never returned even when Logs Insights would match it loosely.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, cast

from adapters.jobs.trace_contract.contract import load_trace_contract
from domain.shared_kernel.errors import ErrorCode, ValidationError

__all__ = [
    "DEFAULT_LAG_SECONDS",
    "MAX_LIMIT",
    "MAX_PAGES",
    "SPAN_QUERY_REFUSED",
    "SpanQuery",
    "SpanQueryRefusedError",
    "build_span_query",
    "declared_span_attributes",
    "query_string",
]

SPAN_QUERY_REFUSED: Final = ErrorCode("SPAN_RETRIEVAL", 1)
DEFAULT_LAG_SECONDS: Final = 600
"""B5: the largest `aws/spans` lag bound the four replaced probes used. The managed-harness probe
waited 600 s for a turn's spans (`_TRACE_WAIT_SECONDS`); the reprocess probe polled for 180 s, the
turn-latency probe settled 75 s and the drain probe had none. None exceeds 600 s, so 600 s holds."""
MAX_LIMIT: Final = 10_000
"""Rows per page. One `GetQueryResults` response carries at most 10,000 events (AWS SDK reference,
`StartQueryCommandInput.limit`, read 2026-10-09), and the client reads one response per page."""
MAX_PAGES: Final = 64
"""Queries per lookup, counting every window split."""
MAX_LAG_SECONDS: Final = 3_600
MAX_WINDOW: Final = timedelta(days=14)
MAX_KEYS: Final = 8
MAX_NAMES: Final = 16
DEPLOYMENT_ENVIRONMENT: Final = "deployment.environment"
"""The resource attribute every repository emitter sets from its configured environment."""

_ROOT: Final = Path(__file__).resolve().parents[4]
_TRACE_ID: Final = re.compile(r"^[0-9a-f]{32}$")
_ENVIRONMENT: Final = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_KEY_VALUE: Final = re.compile(r"^[A-Za-z0-9._:#/=+@-]{1,256}$")
_SPAN_NAME: Final = re.compile(r"^[A-Za-z0-9 ._/:-]{1,128}$")
_SPAN_PREFIX: Final = re.compile(r"^[A-Za-z0-9 ._:-]{1,128}$")
"""A prefix renders inside `/.../`, so it may not carry the slash that would close the regex."""


class SpanQueryRefusedError(ValidationError):
    """A lookup is malformed or names something the span contract does not declare."""

    def __init__(self, reason: str, description: str) -> None:
        super().__init__(SPAN_QUERY_REFUSED, description, context={"reason": reason})
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SpanQuery:
    """One validated span lookup over an explicit half-open window."""

    environment: str
    window_start: datetime
    window_end: datetime
    trace_id: str | None
    keys: tuple[tuple[str, str], ...]
    span_names: tuple[str, ...]
    span_name_prefixes: tuple[str, ...]
    limit: int
    max_pages: int
    lag_seconds: int

    def describe(self) -> dict[str, object]:
        """Return the lookup as JSON-ready inputs, echoed in every result."""
        return {
            "environment": self.environment,
            "trace_id": self.trace_id,
            "keys": dict(self.keys),
            "span_names": list(self.span_names),
            "span_name_prefixes": list(self.span_name_prefixes),
            "limit": self.limit,
            "max_pages": self.max_pages,
            "lag_seconds": self.lag_seconds,
        }

    def matches(self, document: Mapping[str, object]) -> bool:
        """Whether one parsed span belongs to this lookup (the exclusion predicate)."""
        if self.trace_id is not None and document.get("traceId") != self.trace_id:
            return False
        attributes = _mapping(document.get("attributes"))
        if any(_text(attributes.get(key)) != value for key, value in self.keys):
            return False
        if self.span_names or self.span_name_prefixes:
            name = document.get("name")
            if not isinstance(name, str):
                return False
            if name not in self.span_names and not any(
                name.startswith(prefix) for prefix in self.span_name_prefixes
            ):
                return False
        # A span from a repository emitter names its deployment; another deployment's span is
        # excluded. A managed service's span carries no such attribute and is kept.
        environment = _mapping(_mapping(document.get("resource")).get("attributes")).get(
            DEPLOYMENT_ENVIRONMENT
        )
        return not isinstance(environment, str) or environment == self.environment


def _mapping(value: object) -> Mapping[str, object]:
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    """Render an identifier attribute as the text a key lookup compares, or nothing."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str | int):
        return str(value)
    return None


def declared_span_attributes(root: Path | None = None) -> frozenset[str]:
    """Return every attribute some span declares, read from the trace contract."""
    contract = load_trace_contract(root or _ROOT)
    return frozenset(attribute for span in contract.spans for attribute in span.allowed_attributes)


def _refuse(reason: str, description: str) -> SpanQueryRefusedError:
    return SpanQueryRefusedError(reason, description)


def _window(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise _refuse("window_naive", "the window needs explicit UTC offsets.")
    if end <= start:
        raise _refuse("window_empty", "window_end must be after window_start.")
    if end - start > MAX_WINDOW:
        raise _refuse("window_too_wide", "the window exceeds the 14-day bound.")


def _bounded(value: int, bounds: tuple[int, int], reason: str) -> int:
    lower, upper = bounds
    if isinstance(value, bool) or not lower <= value <= upper:
        raise _refuse(reason, f"the value must be an integer in [{lower}, {upper}].")
    return value


def _keys(keys: Iterable[tuple[str, str]], declared: frozenset[str]) -> tuple[tuple[str, str], ...]:
    found = tuple(keys)
    if len(found) > MAX_KEYS:
        raise _refuse("too_many_keys", f"a lookup names at most {MAX_KEYS} keys.")
    names = [key for key, _ in found]
    if len(set(names)) != len(names):
        raise _refuse("key_duplicate", "a key may be named once.")
    for key, value in found:
        if key not in declared:
            raise _refuse("undeclared_key", f"{key!r} is not an attribute any span declares.")
        if _KEY_VALUE.fullmatch(value) is None:
            raise _refuse("key_value_malformed", f"the value of {key!r} is malformed.")
    return tuple(sorted(found))


def _names(values: Iterable[str], pattern: re.Pattern[str], reason: str) -> tuple[str, ...]:
    found = tuple(values)
    if len(found) > MAX_NAMES:
        raise _refuse("too_many_names", f"a lookup names at most {MAX_NAMES} span names.")
    for value in found:
        if pattern.fullmatch(value) is None:
            raise _refuse(reason, f"the span name {value!r} is malformed.")
    return tuple(sorted(set(found)))


def build_span_query(  # noqa: PLR0913 -- one keyword per lookup input, all explicit
    *,
    environment: str,
    window_start: datetime,
    window_end: datetime,
    declared_attributes: frozenset[str],
    trace_id: str | None = None,
    keys: Iterable[tuple[str, str]] = (),
    span_names: Iterable[str] = (),
    span_name_prefixes: Iterable[str] = (),
    limit: int = 1_000,
    max_pages: int = 8,
    lag_seconds: int = DEFAULT_LAG_SECONDS,
) -> SpanQuery:
    """Validate one lookup; refuse with `SpanQueryRefusedError` before any read."""
    if _ENVIRONMENT.fullmatch(environment) is None:
        raise _refuse("environment_malformed", "the environment name is malformed.")
    _window(window_start, window_end)
    if trace_id is not None and _TRACE_ID.fullmatch(trace_id) is None:
        raise _refuse("trace_id_malformed", "a trace ID is 32 lower-case hex digits.")
    query = SpanQuery(
        environment=environment,
        window_start=window_start,
        window_end=window_end,
        trace_id=trace_id,
        keys=_keys(keys, declared_attributes),
        span_names=_names(span_names, _SPAN_NAME, "span_name_malformed"),
        span_name_prefixes=_names(span_name_prefixes, _SPAN_PREFIX, "span_name_malformed"),
        limit=_bounded(limit, (1, MAX_LIMIT), "limit_out_of_bound"),
        max_pages=_bounded(max_pages, (1, MAX_PAGES), "max_pages_out_of_bound"),
        lag_seconds=_bounded(lag_seconds, (0, MAX_LAG_SECONDS), "lag_out_of_bound"),
    )
    if not (query.trace_id or query.keys or query.span_names or query.span_name_prefixes):
        raise _refuse(
            "selector_missing", "a lookup needs a trace ID, a declared key or a span name."
        )
    return query


def query_string(query: SpanQuery) -> str:
    """Render the Logs Insights text; every value was validated against a closed set."""
    clauses: list[str] = []
    if query.trace_id is not None:
        clauses.append(f"traceId = '{query.trace_id}'")
    clauses.extend(f"attributes.{key} = '{value}'" for key, value in query.keys)
    names = [f"name = '{name}'" for name in query.span_names]
    names.extend(f"name like /^{re.escape(prefix)}/" for prefix in query.span_name_prefixes)
    if names:
        clauses.append(f"({' or '.join(names)})")
    return (
        "fields @timestamp, @message "
        f"| filter {' and '.join(clauses)} "
        f"| sort @timestamp asc | limit {query.limit}"
    )
