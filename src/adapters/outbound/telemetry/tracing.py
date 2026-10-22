# SPDX-License-Identifier: MIT-0
"""Hand-authored spans, from one place, so no other module touches the OTel API directly.

Auto-instrumentation covers HTTP and SDK calls, which is not enough to attribute latency: it shows
that `GetQueryExecution` was called eleven times, not that one question spent four seconds waiting
for Athena. The observability steering therefore requires a hand-authored span around each operation
whose duration must be explainable, and requires each to carry the attributes that explain its *own*
duration — a span with only a duration cannot distinguish a slow query from a large one.

**The OTel API only, never the SDK.** `pyproject.toml` records why at the dependency. The
consequence here is that this module works identically whether or not anything is collecting: with
no SDK configured every span is a `NonRecordingSpan`, `record` is a no-op, and nothing fails. That
is what lets ingestion be a cost dial while instrumentation stays permanently in the code.

`get_tracer` is called once at module scope, which is safe *because* the API returns a proxy. The
ADOT layer installs the real tracer provider during its own init, and a proxy acquired before that
resolves to it on first use — so this does not have to care about import order.

**Not Powertools Tracer.** It wraps the X-Ray SDK, which entered maintenance on 2026-02-25.
Powertools Logger and Metrics are unaffected and both are used; see `reporting.py`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Final

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from adapters.outbound.telemetry.generated_spans import (
    AGENT_QUALITY_PROCESS,
    ARTIFACT_WRITE,
    ATHENA_QUERY,
    ATHENA_STATEMENT,
    AUTH_AUTHORIZE,
    AUTH_REQUEST,
    BAKE_FAN_OUT,
    BAKE_FINALIZE,
    BAKE_PHASE,
    BAKE_RUN,
    BAKE_SOURCE_PIN,
    BAKE_TASK,
    CATALOG_GATEWAY,
    CONVERSATION_ROUTE_EXCHANGE,
    CONVERSATION_TURN,
    EVALUATION_GATE,
    EVALUATION_OUTBOX_DISPATCH,
    EVALUATION_RESULT_INGRESS,
    EVALUATION_RESULT_PERSIST,
    EVALUATION_WRITER,
    EXECUTION_GRAPH_PROMOTER_DOMAIN,
    EXECUTION_GRAPH_PROMOTER_PASS,
    GATEWAY_INTERCEPTOR,
    GATEWAY_INTERCEPTOR_REQUIRE_BEARER,
    GRAPH_PUBLISH,
    INGEST_DELIVERY,
    INGEST_FETCH,
    INGEST_GRAPH_MATERIALIZE,
    INGEST_HANDLER_PROCESS,
    INGEST_OPERATOR_PROCESS,
    INGEST_PARSE,
    INGEST_PARTITION,
    INSIGHT_CONTINUATION,
    INSIGHT_GENERATION,
    INSIGHT_PRECOMPUTE,
    INSIGHT_PRECOMPUTE_PLAN,
    INSIGHT_PROMPT_CALIBRATION,
    INSIGHT_PROMPT_REGISTRY_SEED,
    ISSUER_REQUEST,
    LAND_FIXTURES_DELIVERY,
    LAND_FIXTURES_PROCESS,
    MEASUREMENT_GATEWAY,
    OPERATIONAL_STATUS_ATTEMPTS,
    OPERATIONAL_STATUS_PROJECTOR,
    OPERATIONAL_STATUS_RECONCILER,
    OPERATIONAL_STATUS_REQUEST,
    POST_GRANT_CONSUME,
    PRECOMPUTE_RUN,
    PRESENTATION_RELEASE_PREFLIGHT,
    PRESENTATION_REQUEST,
    PRESET_ARTIFACT_BACKFILL,
    PRESET_ARTIFACT_DELIVERY,
    PRESET_ARTIFACT_ENQUEUE,
    PRESET_ARTIFACT_ENQUEUE_DELIVERY,
    PRESET_ARTIFACT_PROCESS,
    PRODUCER_REGISTRATION_FINALIZE,
    PROMPT_ADMIN_REQUEST,
    QUALITY_REVIEW_REQUEST,
    REPROCESS_ACCEPTANCE,
    REPROCESS_APPLY,
    REPROCESS_DELIVERY,
    REPROCESS_PLAN,
    REPROCESS_PROCESS,
    REQUEUE_PROVIDER_FENCE_PROCESS,
    ROUTE_EXCHANGE_PROCESS,
    TABLE_INLINE_RUN,
    TABLE_WRITER_GRAPH_UNIT,
    TABLE_WRITER_GRAPH_UNIT_BATCH,
    TABLE_WRITER_PLAN_TERMINAL,
    TABLE_WRITER_PROCESS,
    TABLE_WRITER_UNIT,
    VERIFY_STAGE1_PRESWITCH_PROCESS,
)
from domain.shared_kernel.errors import AdlyticsError

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Generator

    from opentelemetry.trace import Span
    from opentelemetry.util.types import AttributeValue

__all__ = [
    "AGENT_QUALITY_PROCESS",
    "ARTIFACT_WRITE",
    "ATHENA_QUERY",
    "ATHENA_STATEMENT",
    "AUTH_AUTHORIZE",
    "AUTH_REQUEST",
    "BAKE_FAN_OUT",
    "BAKE_FINALIZE",
    "BAKE_PHASE",
    "BAKE_RUN",
    "BAKE_SOURCE_PIN",
    "BAKE_TASK",
    "CATALOG_GATEWAY",
    "CONVERSATION_ROUTE_EXCHANGE",
    "CONVERSATION_TURN",
    "EVALUATION_GATE",
    "EVALUATION_OUTBOX_DISPATCH",
    "EVALUATION_RESULT_INGRESS",
    "EVALUATION_RESULT_PERSIST",
    "EVALUATION_WRITER",
    "EXECUTION_GRAPH_PROMOTER_DOMAIN",
    "EXECUTION_GRAPH_PROMOTER_PASS",
    "GATEWAY_INTERCEPTOR",
    "GATEWAY_INTERCEPTOR_REQUIRE_BEARER",
    "GRAPH_PUBLISH",
    "INGEST_DELIVERY",
    "INGEST_FETCH",
    "INGEST_GRAPH_MATERIALIZE",
    "INGEST_HANDLER_PROCESS",
    "INGEST_OPERATOR_PROCESS",
    "INGEST_PARSE",
    "INGEST_PARTITION",
    "INSIGHT_CONTINUATION",
    "INSIGHT_GENERATION",
    "INSIGHT_PRECOMPUTE",
    "INSIGHT_PRECOMPUTE_PLAN",
    "INSIGHT_PROMPT_CALIBRATION",
    "INSIGHT_PROMPT_REGISTRY_SEED",
    "ISSUER_REQUEST",
    "LAND_FIXTURES_DELIVERY",
    "LAND_FIXTURES_PROCESS",
    "MEASUREMENT_GATEWAY",
    "OPERATIONAL_STATUS_ATTEMPTS",
    "OPERATIONAL_STATUS_PROJECTOR",
    "OPERATIONAL_STATUS_RECONCILER",
    "OPERATIONAL_STATUS_REQUEST",
    "POST_GRANT_CONSUME",
    "PRECOMPUTE_RUN",
    "PRESENTATION_RELEASE_PREFLIGHT",
    "PRESENTATION_REQUEST",
    "PRESET_ARTIFACT_BACKFILL",
    "PRESET_ARTIFACT_DELIVERY",
    "PRESET_ARTIFACT_ENQUEUE",
    "PRESET_ARTIFACT_ENQUEUE_DELIVERY",
    "PRESET_ARTIFACT_PROCESS",
    "PRODUCER_REGISTRATION_FINALIZE",
    "PROMPT_ADMIN_REQUEST",
    "QUALITY_REVIEW_REQUEST",
    "REPROCESS_ACCEPTANCE",
    "REPROCESS_APPLY",
    "REPROCESS_DELIVERY",
    "REPROCESS_PLAN",
    "REPROCESS_PROCESS",
    "REQUEUE_PROVIDER_FENCE_PROCESS",
    "ROUTE_EXCHANGE_PROCESS",
    "TABLE_INLINE_RUN",
    "TABLE_WRITER_GRAPH_UNIT",
    "TABLE_WRITER_GRAPH_UNIT_BATCH",
    "TABLE_WRITER_PLAN_TERMINAL",
    "TABLE_WRITER_PROCESS",
    "TABLE_WRITER_UNIT",
    "VERIFY_STAGE1_PRESWITCH_PROCESS",
    "Recording",
    "current_trace_ids",
    "span",
    "tool_span",
    "trace_boundary",
]

TRACER = trace.get_tracer("adlytics")

_TOOL_PREFIX: Final = "tool."
_TRACE_BOUNDARY_MARKER: Final = "__trace_boundaries__"


def trace_boundary[**P, R](
    *span_names: str,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Mark one function as the owner of declared spans without wrapping or opening them."""
    if not span_names:
        raise ValueError("trace_boundary requires at least one span name.")
    if any(not name.strip() for name in span_names):
        raise ValueError("trace_boundary span names must be nonblank strings.")
    if len(span_names) != len(set(span_names)):
        raise ValueError("trace_boundary span names must be unique.")
    marker = tuple(span_names)

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        if hasattr(function, _TRACE_BOUNDARY_MARKER):
            raise ValueError("a function may have only one trace_boundary decorator.")
        setattr(function, _TRACE_BOUNDARY_MARKER, marker)
        return function

    return decorate


def current_trace_ids() -> dict[str, str]:
    """Project only valid current OTel trace and span IDs for structured logs.

    A non-recording or invalid context contributes nothing. The projection deliberately excludes
    trace flags, trace state, the full W3C carrier, and baggage.
    """
    context = trace.get_current_span().get_span_context()
    if not context.is_valid or context.trace_id == 0 or context.span_id == 0:
        return {}
    return {
        "trace_id": f"{context.trace_id:032x}",
        "span_id": f"{context.span_id:016x}",
    }


def tool_span(tool_name: str) -> str:
    """Name the span for one tool call, as `tool.<name>`.

    A function rather than a formatted string at each call site, so every handler spells it the same
    way. The tool name arrives from Gateway already stripped of its target prefix — `gateway.py`
    owns that — so what lands here is the tool's own name and not `measurement___...`.
    """
    return f"{_TOOL_PREFIX}{tool_name}"


class Recording:
    """One open span, to which attributes are added as they become known.

    A wrapper rather than the raw span for two reasons. The interesting attributes are only knowable
    *after* the work — bytes scanned, rows returned, whether a cached result was reused — so they
    cannot be passed when the span is opened, and a call site holding a raw span would each coerce
    its own values. Coercion matters more than it sounds: OTel accepts only strings, bools, numbers
    and sequences of those, and a `Decimal` or an enum passed straight in is dropped with a warning
    rather than recorded.
    """

    __slots__ = ("_span",)

    def __init__(self, span: Span) -> None:
        self._span = span

    def record(self, **attributes: object) -> None:
        """Attach attributes that explain this span's duration.

        `None` is dropped rather than recorded. An absent attribute is honest; the string "None" is
        a value that looks like data and sorts alongside real ones.
        """
        for name, value in attributes.items():
            coerced = _as_attribute(value)
            if coerced is not None:
                self._span.set_attribute(name, coerced)

    def note_failure(self, error: BaseException) -> None:
        """Mark a caught failure that will be converted to a response or retry outcome.

        Exception text is never recorded. This complements the context manager's automatic error
        status for raised failures and prevents swallowed dependency/invariant failures from
        appearing successful in the trace.
        """
        self.record(error_type=type(error).__name__)
        self._span.set_status(Status(StatusCode.ERROR))

    def note_error(self, error: AdlyticsError) -> None:
        """Record an error the caller is deliberately turning into an answer, not re-raising.

        **Whether this marks the span failed is decided by `error.alarms`, not by this module.** A
        tool handler turns every classified error into a structured refusal, so by the time one gets
        here it is not propagating — but the four error classes mean different things. "No data for
        that window" and "you did not name a marketplace" are answers, and colouring their spans red
        would make a service map report a broken tool for working correctly. A `DependencyError` or
        an `InvariantError` is the opposite: returned as a refusal, still a real failure, and a
        trace that hid it would be worse than no trace at all.

        `alarms` already encodes exactly that split for logs and alarms, so reusing it keeps one
        judgement in one place rather than inventing a second that can disagree.
        """
        self.record(
            error_type=type(error).__name__,
            error_class=error.error_class,
            error_code=str(error.code),
            alarms=error.alarms,
        )
        if error.alarms:
            # A status code is enough to mark the span failed. A description would repeat the
            # exception message, and record_exception would serialize both that message and the
            # exception object into telemetry.
            self._span.set_status(Status(StatusCode.ERROR))


@contextlib.contextmanager
def span(name: str, **attributes: object) -> Generator[Recording]:
    """Open a span, and record any error on it before letting the error continue.

    **Every error is recorded on the active span, and then re-raised unchanged.** The steering rule
    is that a failed turn must be traceable and not merely countable — a counted error tells you the
    rate, and the span tells you which hop it happened in and what the hop was doing at the time.
    Nothing is swallowed: translating an error is `reporting.py`'s job on the way out of a handler,
    and doing it here as well would report one failure twice.

    An `AdlyticsError` also contributes its class and code, spelled the same as in the log line and
    the metric dimension. One failure is then findable by the same two strings in all three signals,
    which is the difference between one query and three guesses.
    """
    with TRACER.start_as_current_span(name) as raw:
        recording = Recording(raw)
        recording.record(**attributes)
        try:
            yield recording
        except BaseException as error:
            recording.record(error_type=type(error).__name__)
            if isinstance(error, AdlyticsError):
                recording.record(
                    error_class=error.error_class,
                    error_code=str(error.code),
                    alarms=error.alarms,
                )
            raw.set_status(Status(StatusCode.ERROR))
            raise


def _as_attribute(value: object) -> AttributeValue | None:
    """Coerce one value into something OTel will actually store, or `None` to drop it.

    Enums are unwrapped to their values so a span shows `retail_actual` rather than
    `MeasurementBasis.RETAIL_ACTUAL`. Anything else unrecognised becomes its string form, which is
    lossy but visible — the alternative is a dropped attribute and a warning nobody reads.

    `bool` is tested before `int` because `bool` is a subclass of it, and a cache-hit flag rendered
    as 1 reads as a count.
    """
    if value is None:
        return None
    if isinstance(value, Enum):
        return _as_attribute(value.value)
    if isinstance(value, bool | int | float | str):
        return value
    return str(value)
