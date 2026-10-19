# SPDX-License-Identifier: MIT-0
"""One log line and one metric for every error, from one place.

The rule this exists to satisfy is in the observability steering: an inbound
adapter catches everything, maps it to a response, and emits a metric. Doing that
per handler would produce as many spellings of the dimension set as there are
handlers, and dimensions are the part that must not drift — a metric named
differently in two places cannot be alarmed on.

What must never appear here: a token, a cookie value, a credential, or a raw
Cognito response. `ErrorMetric.from_error` deliberately drops the error's context
for the metric; the log line carries only its bounded allowlisted diagnostics because a log is
queryable and a dimension is a cost.
"""

from __future__ import annotations

import functools
import math
import traceback
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit, single_metric

from adapters.outbound.aws_errors import aws_failure_context, redact_diagnostic_text
from adapters.outbound.telemetry.tracing import current_trace_ids
from domain.evaluation.quality import VerdictCount
from domain.presentation.insights import InsightCacheOutcome, InsightPromptTier
from domain.shared_kernel.errors import AdlyticsError, ErrorClass, ErrorMetric

METRICS = Metrics(namespace="Adlytics")
"""One namespace for the platform. The dimension set is what separates concerns."""


@dataclass(frozen=True, slots=True)
class Report:
    """Who is reporting, about what, for which request.

    Built once per invocation and passed down, so the operation and boundary-owned
    investigation locator cannot drift between failure reporting steps.
    """

    operation: str
    correlation_id: str
    logger: Logger
    gateway_message_ref: str = ""


ERROR_METRIC_NAME = "Errors"

INVOCATION_METRIC_NAME = "ToolInvocations"
"""Declared in observability §5, dimensioned on tool and outcome.

Emitted on **every** path, not only failures, and that is the point rather than completeness. An
error count with no denominator cannot express a rate, and the alarm rules are written on the
`DependencyError` and `InvariantError` *rates*. It also means a Powertools flush is never empty,
which was previously a warning on every successful invocation.
"""


class Outcome(StrEnum):
    """How a tool call ended, from the caller's point of view.

    Three values rather than two. A refusal is not a failure — the tool answered, and the
    answer was "no, and here is why" — but it is not an ordinary answer either, and a surface
    where refusals climb is a product signal worth seeing apart from one where calls fail.
    """

    ANSWERED = "answered"
    REFUSED = "refused"
    FAILED = "failed"


class LogSeverity(StrEnum):
    """Structured application severity for non-error operation events."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"


def _empty_details() -> Mapping[str, object]:
    return {}


@dataclass(frozen=True, slots=True)
class OperationLog:
    """One structured operation event at debug, info, or warning severity."""

    operation: str
    component: str
    phase: str
    outcome: str
    severity: LogSeverity
    description: str = ""
    details: Mapping[str, object] = field(default_factory=_empty_details)

    def __post_init__(self) -> None:
        required = (self.operation, self.component, self.phase, self.outcome)
        if not all(value.strip() for value in required):
            raise ValueError("structured operation fields must be nonblank")


class LifecycleOutcome(StrEnum):
    """Closed processing outcomes used by managed-path lifecycle projections."""

    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"
    PROCESSED = "processed"
    RETRY = "retry"
    LINKED = "linked"
    IGNORED = "ignored"
    PERSISTED = "persisted"


class ManagedTerminal(StrEnum):
    """Closed Conversation API terminal states mirrored from the immutable audit."""

    COMPLETE = "complete"
    GOVERNED_REFUSAL = "governed_refusal"
    OUTPUT_POLICY_INTERVENTION = "output_policy_intervention"
    DEPENDENCY_FAILURE = "dependency_failure"
    INPUT_POLICY_INTERVENTION = "input_policy_intervention"
    EXCHANGE_REFUSED = "exchange_refused"
    CLIENT_DISCONNECTED = "client_disconnected"


class PublicationOutcome(StrEnum):
    """Closed Conversation API publication outcomes mirrored from the immutable audit."""

    PUBLISHED = "published"
    REFUSED = "refused"
    NOT_APPLICABLE = "not_applicable"


class EvaluationState(StrEnum):
    """Closed request-boundary view of protected evaluation admission."""

    NOT_SELECTED = "not_selected"
    PENDING = "pending"
    ADMITTED = "admitted"
    FAILED = "failed"


class ConversationInvestigationNext(StrEnum):
    """Closed operator handoffs available at the Conversation API boundary."""

    HARNESS_TRACE = "inspect_harness_trace"
    TERMINAL_AUDIT = "inspect_terminal_audit"
    EVALUATION_CHAIN = "inspect_evaluation_chain"
    APPLICATION_FAILURE = "inspect_application_failure"


class GatewayInvestigationNext(StrEnum):
    """Closed operator handoffs available at a Gateway target boundary."""

    MANAGED_TOOL_TRACE = "inspect_managed_tool_trace"
    TOOL_BOUNDARY = "inspect_tool_boundary"


class GatewayResultClass(StrEnum):
    """Bounded Gateway target result classifications; never tool or result content."""

    ANSWER = "answer"
    CLASSIFIED_REFUSAL = "classified_refusal"
    INVALID_TOOL = "invalid_tool"
    INVALID_INVOCATION = "invalid_invocation"
    CALLER_UNVERIFIED = "caller_unverified"
    UNEXPECTED_FAILURE = "unexpected_failure"


_MAX_LIFECYCLE_COUNT: Final = 10_000


def _require_bounded_count(value: int) -> None:
    """Reject negative or unexpectedly large lifecycle counts before logging."""
    if not 0 <= value <= _MAX_LIFECYCLE_COUNT:
        raise ValueError("lifecycle count is outside the closed telemetry bound")


@dataclass(frozen=True, slots=True)
class ConversationTerminalLog:
    """Closed Conversation API terminal projection, one per turn (observability §3.9).

    Written for every terminal class. `audit_id` is empty only when the receipt write itself
    failed, and `audit_state` then says `not_persisted`, so the line still names the outcome.
    """

    correlation_id: str
    managed_terminal: ManagedTerminal
    publication_outcome: PublicationOutcome
    audit_id: str
    evaluation_state: EvaluationState
    investigation_next: ConversationInvestigationNext
    persisted: bool = True

    @property
    def projection(self) -> dict[str, str]:
        """Return the fixed fields for this lifecycle boundary."""
        outcome = {
            ManagedTerminal.COMPLETE: LifecycleOutcome.COMPLETED,
            ManagedTerminal.GOVERNED_REFUSAL: LifecycleOutcome.REFUSED,
            ManagedTerminal.OUTPUT_POLICY_INTERVENTION: LifecycleOutcome.REFUSED,
            ManagedTerminal.DEPENDENCY_FAILURE: LifecycleOutcome.FAILED,
            ManagedTerminal.INPUT_POLICY_INTERVENTION: LifecycleOutcome.REFUSED,
            ManagedTerminal.EXCHANGE_REFUSED: LifecycleOutcome.REFUSED,
            # The user left; not a platform fault, so it never reads as a failure.
            ManagedTerminal.CLIENT_DISCONNECTED: LifecycleOutcome.REFUSED,
        }[self.managed_terminal]
        return {
            "operation": "conversation.turn",
            "component": "conversation_api",
            "phase": "terminal",
            "outcome": outcome.value,
            "correlation_id": self.correlation_id,
            "managed_terminal": self.managed_terminal.value,
            "publication_outcome": self.publication_outcome.value,
            "audit_id": self.audit_id,
            "audit_state": "persisted" if self.persisted else "not_persisted",
            "evaluation_state": self.evaluation_state.value,
            "investigation_next": self.investigation_next.value,
        }


@dataclass(frozen=True, slots=True)
class ConversationEvaluationLog:
    """Closed post-release update for protected evaluation handoff state."""

    correlation_id: str
    audit_id: str
    evaluation_state: EvaluationState
    admission_ref: str = ""
    delivery_ref: str = ""

    def __post_init__(self) -> None:
        if self.evaluation_state not in {EvaluationState.ADMITTED, EvaluationState.FAILED}:
            raise ValueError("evaluation handoff must finish admitted or failed")

    @property
    def projection(self) -> dict[str, str]:
        """Return the fixed fields for this lifecycle boundary."""
        outcome = (
            LifecycleOutcome.COMPLETED
            if self.evaluation_state is EvaluationState.ADMITTED
            else LifecycleOutcome.FAILED
        )
        projected = {
            "operation": "conversation.turn",
            "component": "conversation_api",
            "phase": "evaluation_handoff",
            "outcome": outcome.value,
            "correlation_id": self.correlation_id,
            "audit_id": self.audit_id,
            "audit_state": "persisted",
            "evaluation_state": self.evaluation_state.value,
            "investigation_next": ConversationInvestigationNext.EVALUATION_CHAIN.value,
        }
        if self.admission_ref:
            projected["admission_ref"] = self.admission_ref
        if self.delivery_ref:
            projected["delivery_ref"] = self.delivery_ref
        return projected


MAX_CHANNEL_KEY_NAMES: Final = 32
MAX_CHANNEL_KEY_NAME_LENGTH: Final = 64
_BEARER_CHANNEL_MARKER: Final = "authorization"


def _bounded_key_names(names: Iterable[str]) -> tuple[str, ...]:
    """Sorted, de-duplicated, single-line key names, each and all bounded."""
    cleaned = {redact_diagnostic_text(name)[:MAX_CHANNEL_KEY_NAME_LENGTH] for name in names}
    return tuple(sorted(cleaned))[:MAX_CHANNEL_KEY_NAMES]


@dataclass(frozen=True, slots=True)
class GatewayChannelKeys:
    """Which channels one Gateway target invocation carried, by key name only (ADR-077 probe P3).

    Names and never values: the question P3 answers is whether an interceptor-set `Authorization`
    reaches a Lambda target at all, and a key name answers it without the credential.
    """

    context_custom_keys: tuple[str, ...]
    argument_keys: tuple[str, ...]
    bearer_channel_seen: bool
    propagated_header_names: tuple[str, ...] = ()
    """Names inside `bedrockAgentCorePropagatedHeaders`, which Gateway sends undocumented."""

    def __post_init__(self) -> None:
        for names in (self.context_custom_keys, self.argument_keys, self.propagated_header_names):
            if (
                len(names) > MAX_CHANNEL_KEY_NAMES
                or any(len(name) > MAX_CHANNEL_KEY_NAME_LENGTH for name in names)
                or list(names) != sorted(set(names))
            ):
                raise ValueError("channel key names must be sorted, unique, and bounded")

    @classmethod
    def from_names(
        cls,
        custom_keys: Iterable[str],
        argument_keys: Iterable[str],
        propagated_header_names: Iterable[str] = (),
    ) -> GatewayChannelKeys:
        """Bound the names; the bearer flag is decided on every name before bounding."""
        custom = tuple(custom_keys)
        propagated = tuple(propagated_header_names)
        return cls(
            context_custom_keys=_bounded_key_names(custom),
            argument_keys=_bounded_key_names(argument_keys),
            bearer_channel_seen=any(
                _BEARER_CHANNEL_MARKER in name.lower() for name in (*custom, *propagated)
            ),
            propagated_header_names=_bounded_key_names(propagated),
        )

    @property
    def projection(self) -> dict[str, list[str] | bool]:
        """Return the diagnostic fields."""
        return {
            "context_custom_keys": list(self.context_custom_keys),
            "argument_keys": list(self.argument_keys),
            "bearer_channel_seen": self.bearer_channel_seen,
            "propagated_header_names": list(self.propagated_header_names),
        }


@dataclass(frozen=True, slots=True)
class GatewayTerminalLog:
    """Closed target-local Gateway tool completion projection."""

    operation: str
    outcome: Outcome
    result_class: GatewayResultClass
    investigation_next: GatewayInvestigationNext
    gateway_message_ref: str = ""
    channel: GatewayChannelKeys | None = None
    caller_verified: bool = False
    """Whether the caller's bearer verified (ADR-077 §5). Never the subject, never a claim."""

    @property
    def projection(self) -> dict[str, str | list[str] | bool]:
        """Return the fixed fields for this lifecycle boundary."""
        projected: dict[str, str | list[str] | bool] = {
            "operation": self.operation,
            "component": "gateway_target",
            "phase": "terminal",
            "outcome": self.outcome.value,
            "result_class": self.result_class.value,
            "investigation_next": self.investigation_next.value,
            "caller_verified": self.caller_verified,
        }
        if self.gateway_message_ref:
            projected["gateway_message_ref"] = self.gateway_message_ref
        if self.channel is not None:
            projected.update(self.channel.projection)
        return projected


@dataclass(frozen=True, slots=True)
class AuditProcessorLog:
    """Closed immutable-audit processor completion projection."""

    correlation_id: str
    audit_id: str
    outcome: LifecycleOutcome
    finding_count: int
    aggregate_written: bool

    def __post_init__(self) -> None:
        _require_bounded_count(self.finding_count)

    @property
    def projection(self) -> dict[str, str | int | bool]:
        """Return the fixed fields for this lifecycle boundary."""
        projected: dict[str, str | int | bool] = {
            "operation": "agent_quality.processor",
            "component": "audit_processor",
            "phase": "processing",
            "outcome": self.outcome.value,
            "finding_count": self.finding_count,
            "aggregate_written": self.aggregate_written,
            "investigation_next": ConversationInvestigationNext.TERMINAL_AUDIT.value,
        }
        if self.audit_id:
            projected["audit_id"] = self.audit_id
        if self.correlation_id:
            projected["correlation_id"] = self.correlation_id
        return projected


@dataclass(frozen=True, slots=True)
class EvaluationIngressLog:
    """Closed protected-result ingress completion projection."""

    outcome: LifecycleOutcome
    audit_id: str = ""
    correlation_id: str = ""
    admission_ref: str = ""
    evaluator_ref: str = ""
    evaluator_name: str = ""
    result_state: str = ""
    quality_label: str = ""
    quality_score: float | None = None
    linked_count: int = 0
    ignored_count: int = 0

    def __post_init__(self) -> None:
        _require_bounded_count(self.linked_count)
        _require_bounded_count(self.ignored_count)

    @property
    def projection(self) -> dict[str, str | int | float]:
        """Return the fixed fields for this lifecycle boundary."""
        projected: dict[str, str | int | float] = {
            "operation": "evaluation_result.ingress",
            "component": "evaluation_ingress",
            "phase": "processing",
            "outcome": self.outcome.value,
            "linked_count": self.linked_count,
            "ignored_count": self.ignored_count,
            "investigation_next": ConversationInvestigationNext.EVALUATION_CHAIN.value,
        }
        if self.audit_id:
            projected["audit_id"] = self.audit_id
        if self.correlation_id:
            projected["correlation_id"] = self.correlation_id
        if self.admission_ref:
            projected["admission_ref"] = self.admission_ref
        if self.evaluator_ref:
            projected["evaluator_ref"] = self.evaluator_ref
        if self.evaluator_name:
            projected["evaluator_name"] = self.evaluator_name
        if self.result_state:
            projected["result_state"] = self.result_state
        if self.quality_label:
            projected["quality_label"] = self.quality_label
        if self.quality_score is not None:
            projected["quality_score"] = self.quality_score
        return projected


@dataclass(frozen=True, slots=True)
class EvaluationPersisterLog:
    """Closed canonical evaluation-result persistence completion projection."""

    audit_id: str
    outcome: LifecycleOutcome
    persisted_count: int

    def __post_init__(self) -> None:
        _require_bounded_count(self.persisted_count)

    @property
    def projection(self) -> dict[str, str | int]:
        """Return the fixed fields for this lifecycle boundary."""
        projected: dict[str, str | int] = {
            "operation": "evaluation_result.persist",
            "component": "evaluation_persister",
            "phase": "persistence",
            "outcome": self.outcome.value,
            "persisted_count": self.persisted_count,
            "investigation_next": ConversationInvestigationNext.EVALUATION_CHAIN.value,
        }
        if self.audit_id:
            projected["audit_id"] = self.audit_id
        return projected


@dataclass(frozen=True, slots=True)
class ConversationTurnTimingLog:
    """One turn's latency milestones in ms since the route received it (observability §3.10).

    Closed: milestone names, logical tool names, counts, and the provider's numeric token usage.
    Never content, tool input, or an identifier beyond the turn's correlation and audit IDs.
    """

    correlation_id: str
    audit_id: str
    cold_start: bool
    milestones_ms: Mapping[str, int]
    tools: tuple[Mapping[str, str | int], ...] = ()
    assistant_messages_ms: tuple[int, ...] = ()
    text_deltas: int = 0
    metadata_events: int = 0
    usage: Mapping[str, int] = field(default_factory=lambda: {})

    @property
    def projection(self) -> dict[str, object]:
        """Return the fixed fields for this lifecycle boundary, milestones flattened for queries."""
        projected: dict[str, object] = {
            "operation": "conversation.turn",
            "component": "conversation_api",
            "phase": "timing",
            "outcome": LifecycleOutcome.COMPLETED.value,
            "correlation_id": self.correlation_id,
            "cold_start": self.cold_start,
            "text_deltas": self.text_deltas,
            "metadata_events": self.metadata_events,
            "tool_calls": len(self.tools),
            "assistant_messages": len(self.assistant_messages_ms),
        }
        if self.audit_id:
            projected["audit_id"] = self.audit_id
        projected.update({f"ms_{name}": value for name, value in self.milestones_ms.items()})
        projected.update({f"usage_{name}": value for name, value in self.usage.items()})
        if self.tools:
            projected["tools"] = [dict(item) for item in self.tools]
        if self.assistant_messages_ms:
            projected["assistant_messages_ms"] = list(self.assistant_messages_ms)
        return projected


type LifecycleLog = (
    ConversationTerminalLog
    | ConversationTurnTimingLog
    | ConversationEvaluationLog
    | GatewayTerminalLog
    | AuditProcessorLog
    | EvaluationIngressLog
    | EvaluationPersisterLog
)


def report_lifecycle(event: LifecycleLog, logger: Logger) -> None:
    """Emit one closed lifecycle projection plus supported current trace/span IDs."""
    logger.info(
        "managed lifecycle",
        extra={**event.projection, **current_trace_ids()},
    )


UNEXPECTED_CODE = "ADLY-UNEXPECTED-0000"
"""For an exception that is not one of our four classes.

Reported as an invariant breach, because an unclassified exception reaching an
inbound adapter means a path we did not model. It alarms on purpose.
"""

UNIDENTIFIED_TOOL = "unidentified"
"""The `Tool` dimension for an invocation that never said which tool it wanted.

A fixed placeholder rather than a skipped metric. Something invoking a tool function without a
Gateway tool name is either a misconfigured target or a caller that should not be there, and both
are worth seeing — a rate on this dimension is the signal, and there is nothing else to name it by.
"""

INVALID_TOOL = "invalid"
"""The bounded tool dimension used before a target-specific tool enum accepts the name."""

UNVERIFIED_CALLER = "unverified_caller"
"""The bounded tool dimension for a call refused before routing because its caller did not verify.

Fixed rather than the requested tool's name, because nothing about an unverified call, its tool
name included, is read before the caller is (ADR-077 §5).
"""

_MAX_DIAGNOSTIC_TEXT: Final = 256
_MAX_PROVIDER_MESSAGE_TEXT: Final = 2_048
_MAX_DIAGNOSTIC_ITEMS: Final = 20
_MAX_DIAGNOSTIC_DEPTH: Final = 3
_MAX_STACK_LOCATIONS: Final = 12
_CORRELATION_ID_TEXT_LENGTH: Final = 36
_UUID4_VERSION: Final = 4
_MIN_DIAGNOSTIC_INTEGER: Final = -(2**63)
_MAX_DIAGNOSTIC_INTEGER: Final = 2**63 - 1

_ALLOWED_ERROR_CONTEXT_KEYS: Final = frozenset(
    {
        "argument",
        "attempt",
        "attempt_id",
        "available",
        "canonical_partition",
        "canonical_partition_identity",
        "canonical_scope",
        "canonical_scope_identity",
        "cause",
        "column",
        "contract",
        "contract_id",
        "currencies",
        "dataset",
        "dataset_id",
        "execution",
        "execution_id",
        "fact",
        "field",
        "gate",
        "gate_id",
        "gates",
        "grain",
        "manifest_key",
        "measured_for",
        "metric",
        "missing",
        "n",
        "operation",
        "operation_id",
        "order_by",
        "outcome",
        "partition",
        "partition_identity",
        "publication",
        "publication_id",
        "provider_message",
        "query",
        "query_id",
        "request_id",
        "requested",
        "record_kind",
        "required_partitions",
        "rows",
        "scope",
        "scope_identity",
        "source",
        "source-publication",
        "source-publication-id",
        "source_id",
        "source_publication",
        "source_publication_id",
        "service_code",
        "state",
        "status",
        "units",
        "waited_seconds",
        "workgroup",
    }
)
_SENSITIVE_ERROR_CONTEXT_KEY_PARTS: Final = (
    "access_key",
    "authorization",
    "body",
    "code_verifier",
    "cookie",
    "credential",
    "header",
    "password",
    "payload",
    "private_key",
    "secret",
    "token",
)

type _DiagnosticValue = (
    str | int | float | bool | list[_DiagnosticValue] | dict[str, _DiagnosticValue] | None
)


class _DroppedDiagnostic:
    """Sentinel for a context value that is not safe structured diagnostics."""


_DROPPED = _DroppedDiagnostic()


CORRELATION_HEADER: Final = "x-correlation-id"
"""The response header every API handler returns the request's server-minted correlation in
(ADR-081 §2). No handler reads it from a request."""


def normalize_correlation_id(value: object) -> str:
    """Return a canonical UUID correlation key, replacing every other input.

    Correlation is a diagnostic lookup key, never identity. Requiring the canonical textual form
    bounds it and rejects values that could carry an email address, account reference, or other
    caller-selected content into every log line and span.
    """
    if isinstance(value, str) and len(value) == _CORRELATION_ID_TEXT_LENGTH:
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            pass
        else:
            if str(parsed) == value and parsed.version == _UUID4_VERSION:
                return value
    return str(uuid.uuid4())


def _sanitized_error_context(context: Mapping[str, object]) -> dict[str, _DiagnosticValue]:
    """Keep only bounded structured diagnostics with explicitly approved meanings."""
    sanitized: dict[str, _DiagnosticValue] = {}
    for key, value in context.items():
        lowered = key.lower()
        if not key or any(part in lowered for part in _SENSITIVE_ERROR_CONTEXT_KEY_PARTS):
            continue
        if key == "provider_message":
            if isinstance(value, str):
                sanitized[key] = value[:_MAX_PROVIDER_MESSAGE_TEXT]
            continue
        cleaned = _sanitized_diagnostic(value, depth=0)
        if not isinstance(cleaned, _DroppedDiagnostic):
            sanitized[key] = cleaned
    return sanitized


def _sanitized_mapping(
    value: Mapping[object, object], *, depth: int
) -> dict[str, _DiagnosticValue]:
    """Recursively retain diagnostic mappings while removing credential-bearing fields."""
    values: dict[str, _DiagnosticValue] = {}
    for key, item in list(value.items())[:_MAX_DIAGNOSTIC_ITEMS]:
        if not isinstance(key, str) or not key:
            continue
        lowered = key.lower()
        if any(part in lowered for part in _SENSITIVE_ERROR_CONTEXT_KEY_PARTS):
            continue
        sanitized_item = _sanitized_diagnostic(item, depth=depth + 1)
        if not isinstance(sanitized_item, _DroppedDiagnostic):
            values[key[:_MAX_DIAGNOSTIC_TEXT]] = sanitized_item
    return values


def _sanitized_diagnostic(value: object, *, depth: int) -> _DiagnosticValue | _DroppedDiagnostic:
    """Convert one approved context value to a bounded JSON primitive or list."""
    cleaned: _DiagnosticValue | _DroppedDiagnostic = _DROPPED
    if value is None or isinstance(value, bool):
        cleaned = value
    elif isinstance(value, str):
        cleaned = redact_diagnostic_text(value)[:_MAX_DIAGNOSTIC_TEXT]
    elif isinstance(value, int):
        if _MIN_DIAGNOSTIC_INTEGER <= value <= _MAX_DIAGNOSTIC_INTEGER:
            cleaned = value
    elif isinstance(value, float):
        if math.isfinite(value):
            cleaned = value
    elif isinstance(value, Mapping) and depth < _MAX_DIAGNOSTIC_DEPTH:
        cleaned = _sanitized_mapping(cast("Mapping[object, object]", value), depth=depth)
    elif isinstance(value, list | tuple) and depth < _MAX_DIAGNOSTIC_DEPTH:
        items: list[_DiagnosticValue] = []
        raw_items = cast("list[object] | tuple[object, ...]", value)
        for item in raw_items[:_MAX_DIAGNOSTIC_ITEMS]:
            sanitized_item = _sanitized_diagnostic(item, depth=depth + 1)
            if not isinstance(sanitized_item, _DroppedDiagnostic):
                items.append(sanitized_item)
        cleaned = items
    return cleaned


def report_operation(event: OperationLog, logger: Logger) -> None:
    """Emit one structured non-error operation event at its declared severity."""
    extra: dict[str, object] = {
        "operation": event.operation,
        "component": event.component,
        "phase": event.phase,
        "outcome": event.outcome,
        **_sanitized_error_context(event.details),
        **current_trace_ids(),
    }
    if event.description:
        extra["description"] = redact_diagnostic_text(event.description)[
            :_MAX_PROVIDER_MESSAGE_TEXT
        ]
    message = "operation lifecycle"
    if event.severity is LogSeverity.DEBUG:
        logger.debug(message, extra=extra)
    elif event.severity is LogSeverity.WARNING:
        logger.warning(message, extra=extra)
    else:
        logger.info(message, extra=extra)


def _stack_locations(error: BaseException) -> list[dict[str, str | int]]:
    """Return bounded code locations without exception text, source lines, or frame locals."""
    locations = [
        {
            "filename": Path(frame.f_code.co_filename).name[:_MAX_DIAGNOSTIC_TEXT],
            "function": frame.f_code.co_name[:_MAX_DIAGNOSTIC_TEXT],
            "line": line,
        }
        for frame, line in traceback.walk_tb(error.__traceback__)
    ]
    return locations[-_MAX_STACK_LOCATIONS:]


def _report_locators(report: Report) -> dict[str, str]:
    """Project only the locator kinds owned by the reporting boundary."""
    locators = current_trace_ids()
    if report.correlation_id:
        locators["correlation_id"] = report.correlation_id
    if report.gateway_message_ref:
        locators["gateway_message_ref"] = report.gateway_message_ref
    return locators


def report_error(error: BaseException, report: Report) -> None:
    """Log the failure and emit exactly one metric for it.

    Called on every error path before the response is formed. An error that is
    logged but not counted cannot be alarmed on, and one that is counted but not
    logged cannot be diagnosed — so both happen here or neither does.
    """
    if isinstance(error, AdlyticsError):
        metric = ErrorMetric.from_error(error, operation=report.operation)
        context = {**error.context, **aws_failure_context(error)}
        extra = {
            **_report_locators(report),
            "operation": report.operation,
            "error_class": metric.error_class.value,
            "error_code": metric.code,
            "error_type": type(error).__name__[:_MAX_DIAGNOSTIC_TEXT],
            "description": redact_diagnostic_text(error.description)[:_MAX_PROVIDER_MESSAGE_TEXT],
            "error_context": _sanitized_error_context(context),
            "alarms": error.alarms,
        }
        if error.error_class in {ErrorClass.DOMAIN, ErrorClass.VALIDATION}:
            report.logger.warning("operation refused", extra=extra)
        else:
            report.logger.error("operation failed", extra=extra)
        _emit(metric)
        return

    provider_context = _sanitized_error_context(aws_failure_context(error))
    extra: dict[str, object] = {
        **_report_locators(report),
        "operation": report.operation,
        "error_class": ErrorClass.INVARIANT.value,
        "error_code": UNEXPECTED_CODE,
        "error_type": type(error).__name__[:_MAX_DIAGNOSTIC_TEXT],
        "description": redact_diagnostic_text(str(error))[:_MAX_PROVIDER_MESSAGE_TEXT],
        "alarms": True,
        "stack": _stack_locations(error),
    }
    if provider_context:
        extra["error_context"] = provider_context
    report.logger.error("unexpected failure", extra=extra)
    _emit(
        ErrorMetric(
            error_class=ErrorClass.INVARIANT, code=UNEXPECTED_CODE, operation=report.operation
        )
    )


def report_denial(reason: str, report: Report) -> None:
    """Record an authorization denial with its credential-redacted description."""
    report.logger.warning(
        "authorization denied",
        extra={
            **_report_locators(report),
            "operation": report.operation,
            "category": "authorization_failed",
            "description": redact_diagnostic_text(reason)[:_MAX_PROVIDER_MESSAGE_TEXT],
        },
    )
    METRICS.add_dimension(name="Operation", value=report.operation)
    METRICS.add_metric(name="AuthorizationDenied", unit=MetricUnit.Count, value=1)


def report_invocation(report: Report, outcome: Outcome) -> None:
    """Count one tool call and how it ended.

    Dimensioned on the tool and the outcome only. Never on the correlation id, the metric name asked
    about or anything else per-request — EMF cardinality is both a cost and a usability problem, and
    those belong in the log line and the span, which are queryable.
    """
    METRICS.add_dimension(name="Tool", value=report.operation)
    METRICS.add_dimension(name="Outcome", value=outcome.value)
    METRICS.add_metric(name=INVOCATION_METRIC_NAME, unit=MetricUnit.Count, value=1)


def report_cache_outcome(tier: InsightPromptTier, outcome: InsightCacheOutcome) -> None:
    """Count one insight-cache read outcome (ADR-076 slice G owner deliverable).

    Dimensioned only on the closed tier and cache-outcome vocabularies — never a
    slot, correlation id, or anything per-request; those live in the slot log and
    the span. Emitted isolated (`single_metric`) so concurrent slot threads in one
    batch cannot overwrite each other's dimensions, and so the record is flushed at
    emission rather than depending on the handler's terminal flush.
    """
    with single_metric(
        name="InsightCacheOutcome",
        unit=MetricUnit.Count,
        value=1,
        namespace="Adlytics",
    ) as emitted:
        emitted.add_dimension(name="Tier", value=tier.value)
        emitted.add_dimension(name="Outcome", value=outcome.value)


SERVING_REFUSED_METRIC_NAME: Final = "PresentationServingRefused"
_SERVING_REFUSAL_REASONS: Final = frozenset({"ungranted_generation", "serving_refresh_pending"})
"""The two closed grant-serving refusal reasons (`domain.presentation.graph_serving`)."""


def report_serving_refusal(reason: str, *, past_settle: bool) -> None:
    """Count one presentation grant-serving refusal (ADR-139, layer 4).

    Dimensioned only on the closed `Reason` and on `Settle` (`past_settle` when the refusal is
    older than `SERVING_REFUSAL_SETTLE` after its bind, else `within_settle`); the alarm counts
    only `past_settle`. Emitted isolated (`single_metric`) beside the refusal's `Errors` record,
    which stays excluded from every alarm. A reason outside the closed set is a programming error.
    """
    if reason not in _SERVING_REFUSAL_REASONS:
        raise ValueError("a serving refusal reason is one of the two closed values.")
    with single_metric(
        name=SERVING_REFUSED_METRIC_NAME,
        unit=MetricUnit.Count,
        value=1,
        namespace="Adlytics",
    ) as emitted:
        emitted.add_dimension(name="Reason", value=reason)
        emitted.add_dimension(
            name="Settle", value="past_settle" if past_settle else "within_settle"
        )


class EvaluationDispatchOutcome(StrEnum):
    """The closed per-row outcomes of one evaluation-outbox dispatch (ADR-079)."""

    DISPATCHED = "dispatched"
    EVALUATION_NOT_ENABLED = "evaluation_not_enabled"
    ANSWER_NOT_RECEIVED = "answer_not_received"
    DELIVERY_UNKNOWN = "delivery_unknown"
    WAITING = "waiting"
    RETRY = "retry"


def report_evaluation_dispatch(outcome: EvaluationDispatchOutcome) -> None:
    """Count one claimed row's dispatch outcome, one row per data point.

    Dimensioned only on the closed outcome vocabulary, so the enabled-but-silent alarm can
    compare dispatched rows with completed turns without any per-turn identifier. Emitted
    isolated (`single_metric`) and flushed at emission, like every other per-item count here.
    """
    with single_metric(
        name="EvaluationDispatch",
        unit=MetricUnit.Count,
        value=1,
        namespace="Adlytics",
    ) as emitted:
        emitted.add_dimension(name="Outcome", value=outcome.value)


def report_evaluation_admission() -> None:
    """Count one released turn newly admitted to the evaluation outbox (ADR-079 §3).

    The no-dispatch alarm's denominator: a family enabled, turns admitted, and no row sent. It
    has no dimension, so it can never carry a per-turn identifier. An exact replay of an
    admission is not counted again.
    """
    with single_metric(
        name="EvaluationAdmission",
        unit=MetricUnit.Count,
        value=1,
        namespace="Adlytics",
    ):
        pass


def report_evaluation_verdict(count: VerdictCount) -> None:
    """Count one newly linked verdict by its closed dimension and label (ADR-079 §5).

    Two data points: one per dimension and label, and one per dimension alone, which is the
    verdict total the worst-label share divides by. Both dimensions come from the closed
    vocabulary in `domain/evaluation/quality.py`.
    """
    with single_metric(
        name="EvaluationVerdict", unit=MetricUnit.Count, value=1, namespace="Adlytics"
    ) as emitted:
        emitted.add_dimension(name="Dimension", value=count.dimension)
        emitted.add_dimension(name="Label", value=count.label)
    with single_metric(
        name="EvaluationVerdict", unit=MetricUnit.Count, value=1, namespace="Adlytics"
    ) as emitted:
        emitted.add_dimension(name="Dimension", value=count.dimension)


def flushes_metrics[H: Callable[..., object]](handler: H) -> H:
    """Wrap a handler so buffered metrics are flushed on every terminal path."""

    @functools.wraps(handler)
    def wrapper(*args: object, **kwargs: object) -> object:
        try:
            return handler(*args, **kwargs)
        finally:
            flush_metrics()

    return cast("H", wrapper)


def flush_metrics() -> None:
    """Flush buffered EMF only when this invocation recorded an operational metric.

    Powertools warns when asked to flush an empty set. An empty set has no metric buffer to lose,
    while an error-focused request-serving invocation does, so skipping only the former preserves
    terminal durability without turning ordinary healthy turns into warning noise.
    """
    if METRICS.metric_set:
        METRICS.flush_metrics()


def _emit(metric: ErrorMetric) -> None:
    """Emit one isolated EMF record so another batch error cannot overwrite its dimensions."""
    with single_metric(
        name=ERROR_METRIC_NAME,
        unit=MetricUnit.Count,
        value=1,
        namespace="Adlytics",
    ) as emitted:
        for name, value in metric.dimensions.items():
            emitted.add_dimension(name=name, value=value)
