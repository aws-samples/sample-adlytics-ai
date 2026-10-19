# SPDX-License-Identifier: MIT-0
"""The hand-authored span seam: attribute coercion, and what counts as a failed span.

**Nothing here needs a collector, and that is the property being relied on rather than a
convenience.** The repository depends on the OpenTelemetry *API* only, so with no SDK configured
every span is a `NonRecordingSpan` and `set_attribute` does nothing. That is what lets ingestion be
a cost dial while instrumentation stays permanently in the code — and it is also why the assertions
below go through a double: a real span in this tier would record into nothing and prove nothing.

The double implements the three methods the seam actually calls. It is not a fake of the SDK; there
is no SDK here to fake.

Two things are worth the most in this file. Coercion, because OTel silently drops a value of an
unsupported type — a `Decimal` figure or a status enum would vanish rather than fail. And the
`alarms` split, because a refusal painted red makes a service map report a broken tool for behaving
correctly, while a `DependencyError` painted green hides a real outage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from opentelemetry.trace import SpanContext, StatusCode, TraceFlags, TraceState

from adapters.outbound.telemetry.tracing import (
    ATHENA_QUERY,
    ATHENA_STATEMENT,
    PRESET_ARTIFACT_DELIVERY,
    PRESET_ARTIFACT_PROCESS,
    REPROCESS_ACCEPTANCE,
    REPROCESS_APPLY,
    REPROCESS_DELIVERY,
    REPROCESS_PLAN,
    REPROCESS_PROCESS,
    Recording,
    current_trace_ids,
    span,
    tool_span,
)
from domain.narrative.shaped_response import FreshnessStatus
from domain.shared_kernel.errors import (
    DependencyError,
    DomainError,
    ErrorCode,
    InvariantError,
    ValidationError,
)

if TYPE_CHECKING:
    from opentelemetry.trace import Span

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-009", "ADR-010")]

CODE = ErrorCode(area="TEST", number=7)


class _Colour(StrEnum):
    RED = "red"


@dataclass
class _SpanDouble:
    """Captures what the seam did, implementing only the three methods it calls."""

    attributes: dict[str, Any] = field(default_factory=dict[str, Any])
    exceptions: list[BaseException] = field(default_factory=list[BaseException])
    status: object | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def record_exception(self, exception: BaseException) -> None:
        self.exceptions.append(exception)

    def set_status(self, status: Any) -> None:
        self.status = status

    @property
    def failed(self) -> bool:
        return self.status is not None


def _recording() -> tuple[Recording, _SpanDouble]:
    double = _SpanDouble()
    return Recording(cast("Span", double)), double


class TestSpanNames:
    def test_a_tool_span_is_named_for_its_tool(self) -> None:
        assert tool_span("measurement_metric_by_grain") == "tool.measurement_metric_by_grain"

    def test_the_query_span_name_is_the_one_the_design_names(self) -> None:
        """A constant because dashboards match on it — a typo is an empty graph, not a red test."""
        assert ATHENA_QUERY == "athena.query"

    def test_operator_and_background_span_names_are_stable(self) -> None:
        assert ATHENA_STATEMENT == "athena.statement"
        assert REPROCESS_PROCESS == "reprocess.semantic_v2"
        assert REPROCESS_PLAN == "reprocess.semantic_v2.plan"
        assert REPROCESS_APPLY == "reprocess.semantic_v2.apply"
        assert REPROCESS_DELIVERY == "reprocess.semantic_v2.delivery"
        assert REPROCESS_ACCEPTANCE == "reprocess.semantic_v2.acceptance"
        assert PRESET_ARTIFACT_PROCESS == "preset_artifact.publish"
        assert PRESET_ARTIFACT_DELIVERY == "preset_artifact.publish.delivery"


class TestAttributesAreCoerced:
    def test_strings_numbers_and_bools_pass_through(self) -> None:
        recording, double = _recording()
        recording.record(metric="ctr", bytes_scanned=4096, ratio=0.5, cache_hit=True)
        assert double.attributes == {
            "metric": "ctr",
            "bytes_scanned": 4096,
            "ratio": 0.5,
            "cache_hit": True,
        }

    def test_a_cache_hit_stays_a_bool_rather_than_becoming_one(self) -> None:
        """`bool` is a subclass of `int`, so a careless coercion renders False as 0.

        A cache-hit flag shown as 0 or 1 reads as a count, which is a different question.
        """
        recording, double = _recording()
        recording.record(cache_hit=False)
        assert double.attributes["cache_hit"] is False

    def test_none_is_dropped_rather_than_recorded(self) -> None:
        """An absent attribute is honest. The string "None" sorts alongside real values."""
        recording, double = _recording()
        recording.record(present="yes", absent=None)
        assert "absent" not in double.attributes
        assert double.attributes == {"present": "yes"}

    def test_an_enum_is_unwrapped_to_its_value(self) -> None:
        recording, double = _recording()
        recording.record(colour=_Colour.RED, freshness=FreshnessStatus.FRESH)
        assert double.attributes["colour"] == "red"
        assert "FreshnessStatus." not in str(double.attributes["freshness"])

    def test_a_decimal_becomes_its_exact_string(self) -> None:
        """Through `float` a figure stops stating its scale; OTel would drop it outright."""
        recording, double = _recording()
        recording.record(value=Decimal("88.5000"))
        assert double.attributes["value"] == "88.5000"


class TestAnErrorTurnedIntoAnAnswer:
    """`note_error`: recorded on the span, and marked failed only when it alarms."""

    @pytest.mark.parametrize("error_type", [DomainError, ValidationError])
    def test_a_refusal_that_does_not_alarm_leaves_the_span_healthy(
        self, error_type: type[DomainError] | type[ValidationError]
    ) -> None:
        """No data for that window is an answer, not a fault.

        Colouring it red would make a service map report a broken tool for working correctly.

        Worded without a leading quotation mark deliberately. `ruff format` inserts a space after
        the opening delimiter when the text begins with a quote, and `D210` rejects that space — so
        the original phrasing put the formatter and the linter permanently at odds.
        """
        recording, double = _recording()
        recording.note_error(error_type(CODE, "no."))

        assert double.failed is False
        assert double.exceptions == []

    @pytest.mark.parametrize("error_type", [DependencyError, InvariantError])
    def test_a_failure_that_alarms_marks_the_span_even_though_it_was_answered(
        self, error_type: type[DependencyError] | type[InvariantError]
    ) -> None:
        """Returned to the caller as a refusal, and still a real failure.

        A trace that showed this as healthy would be worse than no trace: the one signal that says
        which hop broke would agree with the one that says nothing broke.
        """
        recording, double = _recording()
        recording.note_error(error_type(CODE, "broken."))

        assert double.failed is True
        assert double.exceptions == []
        assert getattr(double.status, "status_code", None) is StatusCode.ERROR
        assert getattr(double.status, "description", None) is None

    def test_every_class_contributes_its_code_and_class(self) -> None:
        """Spelled as in the log line and the metric dimension.

        One failure is then findable by the same two strings in all three signals, which is the
        difference between one query and three guesses.
        """
        recording, double = _recording()
        recording.note_error(DomainError(CODE, "no."))

        assert double.attributes["error_type"] == "DomainError"
        assert double.attributes["error_code"] == "ADLY-TEST-0007"
        assert double.attributes["error_class"] == "domain"
        assert double.attributes["alarms"] is False


class TestCurrentTraceIds:
    def test_only_fixed_width_trace_and_span_ids_are_projected(self) -> None:
        context = SpanContext(
            trace_id=int("a" * 32, 16),
            span_id=int("b" * 16, 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState([("vendor", "opaque-state")]),
        )
        current = SimpleNamespace(get_span_context=lambda: context)

        with patch(
            "adapters.outbound.telemetry.tracing.trace.get_current_span", return_value=current
        ):
            projected = current_trace_ids()

        assert projected == {"trace_id": "a" * 32, "span_id": "b" * 16}
        assert {"traceparent", "tracestate", "baggage"}.isdisjoint(projected)

    def test_an_invalid_context_projects_no_locator(self) -> None:
        context = SpanContext(
            trace_id=0,
            span_id=0,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.DEFAULT),
            trace_state=TraceState(),
        )
        current = SimpleNamespace(get_span_context=lambda: context)

        with patch(
            "adapters.outbound.telemetry.tracing.trace.get_current_span", return_value=current
        ):
            assert current_trace_ids() == {}


class TestTheContextManager:
    def test_it_yields_a_recording(self) -> None:
        with span("probe", metric="ctr") as recording:
            assert isinstance(recording, Recording)

    def test_an_unclassified_error_records_only_its_type_and_propagates_unchanged(self) -> None:
        """Exception messages can contain caller or customer text and must not reach tracing."""
        double = _SpanDouble()
        raised = RuntimeError("customer-secret")

        with patch("adapters.outbound.telemetry.tracing.TRACER") as tracer:
            tracer.start_as_current_span.return_value.__enter__.return_value = double
            with pytest.raises(RuntimeError) as caught, span("probe"):
                raise raised

        assert caught.value is raised
        assert double.exceptions == []
        assert double.attributes == {"error_type": "RuntimeError"}
        assert getattr(double.status, "status_code", None) is StatusCode.ERROR
        assert getattr(double.status, "description", None) is None

    def test_a_classified_error_adds_only_safe_metadata_and_propagates_unchanged(self) -> None:
        """Class, code, and alarm policy join the bounded type without serializing the error."""
        double = _SpanDouble()
        raised = DependencyError(CODE, "customer-secret")

        with patch("adapters.outbound.telemetry.tracing.TRACER") as tracer:
            tracer.start_as_current_span.return_value.__enter__.return_value = double
            with pytest.raises(DependencyError) as caught, span("probe"):
                raise raised

        assert caught.value is raised
        assert double.exceptions == []
        assert double.attributes == {
            "error_type": "DependencyError",
            "error_class": "dependency",
            "error_code": "ADLY-TEST-0007",
            "alarms": True,
        }
        assert getattr(double.status, "status_code", None) is StatusCode.ERROR
        assert getattr(double.status, "description", None) is None

    def test_recording_works_with_no_sdk_configured(self) -> None:
        """The degradation the whole approach rests on.

        No collector, no exporter, no credentials — and a span still opens and accepts attributes.
        If this raised, every unit test in the suite would need a tracer provider.
        """
        with span("probe") as recording:
            recording.record(bytes_scanned=1, cache_hit=False, absent=None)
