# SPDX-License-Identifier: MIT-0
"""Content-minimized immutable contracts for the Agent Quality audit boundary.

The audit record answers "what happened and under which configuration" for one managed Harness
turn without carrying the question, the answer, or any tool payload (ADR-059, ADR-062). Its
trajectory keeps every tool call the Harness made, including each finalizer attempt, keyed by a
reference that reaches that call's own trace. Delivery is a separate companion (ADR-063) because
the disposition is only known after the immutable record has been streamed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from domain.conversation.contract_common import require_integer, require_text

_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SERVICE_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
_ERROR_CODE = re.compile(r"^ADLY-[A-Z_]+-\d{4}$")
_VIOLATION_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CORRELATION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_PAGE_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_MAX_STEPS = 24
_MAX_STEP_REF = 256

MANAGED_HARNESS_RUNTIME_V1 = "managed-harness.v1"
"""The runtime generation whose stored receipts carry no step anchors and no trace ID."""

MANAGED_HARNESS_RUNTIME_V2 = "managed-harness.v2"
"""The runtime generation that anchors every trajectory step and stamps the turn trace."""

MANAGED_HARNESS_RUNTIME_V3 = "managed-harness.v3"
"""The runtime generation that also stamps the verified user as the principal (ADR-077 §6)."""

_RUNTIMES_WITHOUT_PRINCIPAL = frozenset({MANAGED_HARNESS_RUNTIME_V1, MANAGED_HARNESS_RUNTIME_V2})
_RUNTIMES = _RUNTIMES_WITHOUT_PRINCIPAL | {MANAGED_HARNESS_RUNTIME_V3}
BLOCK_STREAM_CONTRACT = "public-block-stream.v3"
"""The plan-published block stream that receipts before ADR-083 record."""
ANSWER_STREAM_CONTRACT = "conversation-stream.v4"
"""The streamed Markdown answer (ADR-083): no plan, no blocks, a governed-only trajectory."""
_RESPONSE_CONTRACTS = frozenset({BLOCK_STREAM_CONTRACT, ANSWER_STREAM_CONTRACT})


def _opaque(value: str, label: str) -> None:
    require_text(value, label, maximum=128)
    if not _OPAQUE_REFERENCE.fullmatch(value):
        raise ValueError(f"{label} must be an opaque bounded reference.")


def _instant(value: str, label: str) -> None:
    require_text(value, label)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} must be an ISO instant.") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{label} must be a UTC ISO instant.")


class AuditOperationCategoryV2(StrEnum):
    """Bounded terminal operation category."""

    NONE = "none"
    COMPOSITION = "composition"


class AuditOperationOutcomeV2(StrEnum):
    """Bounded terminal operation outcome."""

    NOT_ATTEMPTED = "not_attempted"
    SUCCEEDED = "succeeded"
    REFUSED = "refused"
    FAILED = "failed"


class AuditPublicationOutcomeV2(StrEnum):
    """Bounded publication disposition for the terminal turn."""

    NOT_APPLICABLE = "not_applicable"
    PUBLISHED = "published"
    REFUSED = "refused"


class AuditTerminalOutcomeV2(StrEnum):
    """Every terminal response class represented without response content.

    ADR-081 §1: an inbound Guardrail intervention and a refused token exchange end before the
    Harness runs and write a receipt too, with no trajectory and nothing published. A client that
    disconnects mid-stream leaves `client_disconnected` with whatever text it was sent (ADR-083).
    A killed function still leaves none (the Agent Quality Loop design states that gap).
    `output_policy_intervention` is read from receipts written before ADR-083 retired the
    outbound Guardrail; no current writer produces it.
    """

    COMPLETE = "complete"
    GOVERNED_REFUSAL = "governed_refusal"
    OUTPUT_POLICY_INTERVENTION = "output_policy_intervention"
    DEPENDENCY_FAILURE = "dependency_failure"
    INPUT_POLICY_INTERVENTION = "input_policy_intervention"
    EXCHANGE_REFUSED = "exchange_refused"
    CLIENT_DISCONNECTED = "client_disconnected"


class AuditCompletionRefusalStageV2(StrEnum):
    """Bounded completion boundary for a publication-refused terminal."""

    PUBLISHER_RESPONSE_BOUND = "publisher_response_bound"


class AuditFindingKindV2(StrEnum):
    """Structural findings derived deterministically from one receipt."""

    DEPENDENCY_FAILURE = "dependency_failure"
    GOVERNED_REFUSAL = "governed_refusal"
    OUTPUT_POLICY_INTERVENTION = "output_policy_intervention"
    FINALIZER_REPAIR = "finalizer_repair"
    UNSELECTED_GOVERNED_RESULT = "unselected_governed_result"


class AuditFindingSeverityV2(StrEnum):
    """Closed severity for structural findings."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AuditToolStepDispositionV3(StrEnum):
    """Content-free outcome of one tool call in the order the Harness made it."""

    SUCCEEDED = "succeeded"
    REFUSED = "refused"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERRORED = "errored"


_GOVERNED_DISPOSITIONS = frozenset(
    {AuditToolStepDispositionV3.SUCCEEDED, AuditToolStepDispositionV3.REFUSED}
)


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditToolStepV3:
    """One tool call without inputs, outputs, or provider identifiers.

    A governed step's reference is the correlation ID the tool minted inside its response and
    stamped on its own span; a finalizer step's reference is the Gateway message reference the
    finalizer echoed. Either reaches that call's trace, which Gateway does not join to the turn
    (ADR-062). `selected_into_plan` records whether the accepted plan used anything from a
    governed result, and `violations` keeps the typed reason codes a rejected finalizer attempt
    returned to the model.
    """

    ordinal: int
    tool_name: str
    disposition: AuditToolStepDispositionV3
    step_ref: str | None
    violations: tuple[str, ...] = ()
    selected_into_plan: bool | None = None

    def __post_init__(self) -> None:
        require_integer(self.ordinal, "audit tool step ordinal", minimum=1)
        require_text(self.tool_name, "audit tool step name", maximum=128)
        if not _SERVICE_CODE.fullmatch(self.tool_name):
            raise ValueError("audit tool step name is invalid.")
        if self.step_ref is not None and not (0 < len(self.step_ref) <= _MAX_STEP_REF):
            raise ValueError("audit tool step reference is out of bounds.")
        if len(set(self.violations)) != len(self.violations) or any(
            not _VIOLATION_CODE.fullmatch(item) for item in self.violations
        ):
            raise ValueError("audit tool step violations must be unique typed codes.")
        if self.governed:
            if self.violations:
                raise ValueError("a governed audit tool step carries no violations.")
            return
        if self.selected_into_plan is not None:
            raise ValueError("a finalizer audit tool step has no plan selection.")
        if self.disposition is AuditToolStepDispositionV3.REJECTED and not self.violations:
            raise ValueError("a rejected finalizer audit step requires its violation codes.")
        if self.disposition is AuditToolStepDispositionV3.ACCEPTED and (
            self.violations or self.step_ref is None
        ):
            raise ValueError("an accepted finalizer audit step has a reference and no violations.")
        if self.disposition is AuditToolStepDispositionV3.ERRORED and self.step_ref is not None:
            raise ValueError("an errored finalizer audit step has no reference to record.")

    @property
    def governed(self) -> bool:
        """Whether this step called a governed data tool rather than the finalizer."""
        return self.disposition in _GOVERNED_DISPOSITIONS


class AuditPublicConversationBlockKindV1(StrEnum):
    """Closed content-free projection of released ConversationStream block kinds."""

    PREFACE = "preface"
    PARAGRAPH = "paragraph"
    GROUPED_LIST = "grouped_list"
    GOVERNED_TABLE = "governed_table"
    NOTICE = "notice"


def _failure_details_are_valid(
    codes: tuple[str | None, str | None],
    terminal_outcome: AuditTerminalOutcomeV2,
) -> bool:
    error_code, service_code = codes
    if (
        error_code is not None or service_code is not None
    ) and terminal_outcome is not AuditTerminalOutcomeV2.DEPENDENCY_FAILURE:
        return False
    return (error_code is None or bool(_ERROR_CODE.fullmatch(error_code))) and (
        service_code is None or bool(_SERVICE_CODE.fullmatch(service_code))
    )


class AuditContextReasonV1(StrEnum):
    """Why a turn's page reference wasn't used (ADR-120 decision 5)."""

    EXPIRED = "expired"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditContextStatusV1:
    """The content-free part of a turn's context status: used, or the closed reason."""

    used: bool
    reason: AuditContextReasonV1 | None = None

    def __post_init__(self) -> None:
        if self.used != (self.reason is None):
            raise ValueError("an audit context status names a reason exactly when unused.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationAuditRecordV6:
    """One server-observed immutable terminal receipt without content or route data.

    Provenance is the prompt catalog digest, the Guardrail version, and the contract versions;
    identity is the audit ID the user can quote as a support reference (ADR-081 §2), the
    correlation ID, and the trace ID that joins the API span to the Harness spans. Receipts
    written by `managed-harness.v1` are read through this contract with an unanchored trajectory
    and no trace ID, because that runtime recorded neither.

    `principal` is the raw Cognito `sub` the route verified for this turn: the attribution
    record ADR-077 §6 requires, and, through the trajectory, the user every tool call acted for.
    A `managed-harness.v3` receipt always carries it; receipts from earlier generations never
    recorded one and are read with none. It is an opaque identifier, never a telemetry or
    aggregate dimension, and never part of a review projection (ADR-059).

    V6 adds the turn's page reference and context status (ADR-120 decision 6), never the page's
    label text. Earlier receipts are read with neither.
    """

    schema_version: Literal["ConversationAuditRecord.v6"] = "ConversationAuditRecord.v6"
    audit_id: str
    correlation_id: str
    trace_id: str | None
    operation_category: AuditOperationCategoryV2
    operation_outcome: AuditOperationOutcomeV2
    publication_outcome: AuditPublicationOutcomeV2
    terminal_outcome: AuditTerminalOutcomeV2
    runtime_version: str
    prompt_catalog_digest: str | None
    page_schema_version: str
    response_contract_version: str
    evaluation_admission_ref: str | None
    created_at: str
    failure_error_code: str | None = None
    failure_service_code: str | None = None
    completion_refusal_stage: AuditCompletionRefusalStageV2 | None = None
    trajectory: tuple[AuditToolStepV3, ...] = ()
    tool_custody_digest: str | None = None
    presentation_plan_digest: str | None = None
    public_block_kinds: tuple[AuditPublicConversationBlockKindV1, ...] = ()
    public_block_count: int = 0
    guardrail_version: str | None = None
    principal: str | None = field(default=None, repr=False)
    released_character_count: int = 0
    """Characters of streamed answer text the user was sent (ADR-083); zero for block receipts."""
    page_context_reference: str | None = None
    """The opaque page reference the turn was sent (ADR-120 decision 6), never its labels."""
    context_status: AuditContextStatusV1 | None = None
    """Whether that reference was used, and why not; present exactly when one was sent."""

    def __post_init__(self) -> None:
        self._validate_context()
        _opaque(self.audit_id, "audit ID")
        if not _CORRELATION_ID.fullmatch(self.correlation_id):
            raise ValueError("audit correlation ID must be a normalized UUID4.")
        if self.trace_id is not None and not _TRACE_ID.fullmatch(self.trace_id):
            raise ValueError("audit trace ID must be a 32-hex OpenTelemetry trace ID.")
        if self.runtime_version not in _RUNTIMES:
            raise ValueError("audit runtime version must name a managed Harness generation.")
        self._validate_principal()
        if self.response_contract_version not in _RESPONSE_CONTRACTS:
            raise ValueError("a managed audit must name a declared response contract.")
        _opaque(self.page_schema_version, "audit page schema version")
        if self.prompt_catalog_digest is not None and not _DIGEST.fullmatch(
            self.prompt_catalog_digest
        ):
            raise ValueError("audit prompt catalog digest must be a SHA-256 digest.")
        if self.evaluation_admission_ref is not None:
            _opaque(self.evaluation_admission_ref, "audit evaluation admission reference")
        if not _failure_details_are_valid(
            (self.failure_error_code, self.failure_service_code), self.terminal_outcome
        ):
            raise ValueError("audit failure details are invalid.")
        if self.completion_refusal_stage is not None and not (
            self.publication_outcome is AuditPublicationOutcomeV2.REFUSED
            and self.terminal_outcome is AuditTerminalOutcomeV2.GOVERNED_REFUSAL
        ):
            raise ValueError("audit completion refusal stage is invalid.")
        self._validate_trajectory()
        for digest, label in (
            (self.tool_custody_digest, "audit tool custody digest"),
            (self.presentation_plan_digest, "audit presentation plan digest"),
        ):
            if digest is not None and not _DIGEST.fullmatch(digest):
                raise ValueError(f"{label} must be a SHA-256 digest.")
        require_integer(self.public_block_count, "audit public block count", minimum=0)
        if self.public_block_count != len(self.public_block_kinds):
            raise ValueError("audit public block count must match its block-kind projection.")
        self._validate_response_shape()
        if self.guardrail_version is not None:
            require_text(self.guardrail_version, "audit guardrail version", maximum=128)
        _instant(self.created_at, "audit creation time")

    def _validate_context(self) -> None:
        if (self.page_context_reference is None) != (self.context_status is None):
            raise ValueError("an audit records a context status exactly when a reference was sent.")
        if self.page_context_reference is not None and not _PAGE_REFERENCE.fullmatch(
            self.page_context_reference
        ):
            raise ValueError("an audit page context reference must be an opaque reference.")

    def _validate_response_shape(self) -> None:
        require_integer(self.released_character_count, "audit released characters", minimum=0)
        if self.streamed:
            if (
                self.presentation_plan_digest is not None
                or self.public_block_kinds
                or self.completion_refusal_stage is not None
            ):
                raise ValueError("a streamed-answer receipt records no plan and no blocks.")
        elif self.released_character_count:
            raise ValueError("a block-stream receipt records no streamed characters.")

    @property
    def streamed(self) -> bool:
        """Whether this turn streamed its answer rather than publishing a verified plan."""
        return self.response_contract_version == ANSWER_STREAM_CONTRACT

    def _validate_principal(self) -> None:
        if self.runtime_version in _RUNTIMES_WITHOUT_PRINCIPAL:
            if self.principal is not None:
                raise ValueError("a receipt from this runtime generation records no principal.")
        elif self.principal is None:
            raise ValueError("a managed-harness.v3 receipt must name its verified principal.")
        else:
            _opaque(self.principal, "audit principal")

    def _validate_trajectory(self) -> None:
        steps = self.trajectory
        if len(steps) > _MAX_STEPS:
            raise ValueError("audit trajectory cannot exceed twenty-four steps.")
        if tuple(step.ordinal for step in steps) != tuple(range(1, len(steps) + 1)):
            raise ValueError("audit trajectory must be contiguous and ordered.")
        if self.runtime_version == MANAGED_HARNESS_RUNTIME_V1:
            if self.trace_id is not None or any(
                step.step_ref is not None or step.selected_into_plan is not None for step in steps
            ):
                raise ValueError("a managed-harness.v1 receipt carries no anchors or trace.")
            return
        if self.streamed:
            if any(
                step.disposition
                in {AuditToolStepDispositionV3.ACCEPTED, AuditToolStepDispositionV3.REJECTED}
                or step.selected_into_plan is not None
                or (step.governed and step.step_ref is None)
                for step in steps
            ):
                raise ValueError(
                    "a streamed-answer trajectory holds only governed calls with references."
                )
            return
        if any(
            step.governed and (step.step_ref is None or step.selected_into_plan is None)
            for step in steps
        ):
            raise ValueError("every governed audit step carries its reference and selection.")
        accepted = [
            step for step in steps if step.disposition is AuditToolStepDispositionV3.ACCEPTED
        ]
        if steps and (len(accepted) != 1 or steps[-1] is not accepted[0]):
            raise ValueError("a non-empty audit trajectory ends with one accepted finalizer.")


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditFindingV3:
    """Immutable structural finding with no free-text detail or customer content.

    `step_ordinal` anchors a finding to one trajectory step when the defect belongs to a single
    tool call; a turn-level finding leaves it unset. Stored `AuditFinding.v2` findings are read
    through this contract with no step anchor.
    """

    schema_version: Literal["AuditFinding.v3"] = "AuditFinding.v3"
    finding_id: str
    audit_id: str
    kind: AuditFindingKindV2
    severity: AuditFindingSeverityV2
    created_at: str
    step_ordinal: int | None = None

    def __post_init__(self) -> None:
        _opaque(self.finding_id, "audit finding ID")
        _opaque(self.audit_id, "audit finding audit ID")
        if self.step_ordinal is not None:
            require_integer(self.step_ordinal, "audit finding step ordinal", minimum=1)
        _instant(self.created_at, "audit finding creation time")


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditAggregateMetricV2:
    """Immutable aggregate-ready bounded measurement emitted per processed receipt."""

    schema_version: Literal["AuditAggregateMetric.v2"] = "AuditAggregateMetric.v2"
    metric_id: str
    audit_id: str
    terminal_outcome: AuditTerminalOutcomeV2
    operation_outcome: AuditOperationOutcomeV2
    value: int
    created_at: str

    def __post_init__(self) -> None:
        _opaque(self.metric_id, "audit aggregate metric ID")
        _opaque(self.audit_id, "audit aggregate audit ID")
        require_integer(self.value, "audit aggregate metric value", minimum=0)
        if self.value != 1:
            raise ValueError("audit aggregate metric value must be one per receipt.")
        _instant(self.created_at, "audit aggregate creation time")


class TurnDeliveryDispositionV1(StrEnum):
    """Closed server-observed delivery outcome for one released turn (ADR-063)."""

    DELIVERED = "delivered"
    TRUNCATED = "truncated"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnDeliveryRecordV1:
    """One bounded post-stream delivery companion; its absence reads as unknown, never delivered.

    Server-observed only: a block counts as emitted once the consumer returned for the next
    event, so the count is a lower bound and network-level receipt stays out of scope
    ([ADR-063](../../../docs/decisions/adr-063-record-delivery-disposition-for-released-turns.md)).
    """

    schema_version: Literal["TurnDeliveryRecord.v1"] = "TurnDeliveryRecord.v1"
    audit_id: str
    disposition: TurnDeliveryDispositionV1
    emitted_block_count: int
    recorded_at: str

    def __post_init__(self) -> None:
        if not self.audit_id.strip():
            raise ValueError("delivery disposition requires its audit ID.")
        if not self.recorded_at.strip():
            raise ValueError("delivery disposition requires its recorded timestamp.")
        if self.emitted_block_count < 0:
            raise ValueError("delivery disposition cannot count negative blocks.")
        if (self.disposition is TurnDeliveryDispositionV1.ABORTED) != (
            self.emitted_block_count == 0
        ):
            raise ValueError("aborted delivery is exactly the zero-block case.")


def require_utc_instant(value: str, label: str) -> datetime:
    """Parse one UTC ISO instant exactly as every audit contract validates its timestamps."""
    _instant(value, label)
    return datetime.fromisoformat(value)


MAX_RECORD_RETENTION_DAYS = 999_999
"""Six digits, the synth-time bound; past it an expiry leaves the representable dates."""


@dataclass(frozen=True, slots=True)
class RecordRetention:
    """The declared per-environment retention of every record of a turn (ADR-081 §5).

    Every record's expiry is its receipt's creation time plus these days, so each family's
    single writer computes the same instant from the same receipt, and an exact replay of a
    write computes the same expiry as the original.
    """

    days: int

    def __post_init__(self) -> None:
        require_integer(self.days, "record retention days", minimum=1)
        if self.days > MAX_RECORD_RETENTION_DAYS:
            raise ValueError(f"record retention days must be at most {MAX_RECORD_RETENTION_DAYS}.")

    def expires_at(self, receipt_created_at: str) -> int:
        """Return the record expiry in whole epoch seconds, truncated toward the past."""
        created = require_utc_instant(receipt_created_at, "receipt creation time")
        return int((created + timedelta(days=self.days)).timestamp())
