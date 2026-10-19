# SPDX-License-Identifier: MIT-0
"""The one Quality Review API function: `turn` and `turns`, routed by route key (ADR-078 §5).

`turn` returns one receipt's redacted sections, each rechecked against the review levels the
authorizer verified; `turns` lists receipts by creation time from the turns index. Neither
logs a request, claim, subject, selector, or record value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from aws_lambda_powertools import Logger

from adapters.config import (
    quality_review_settings,
    settings_without_client_secret,
    tracing_settings,
)
from adapters.inbound.auth_handlers import events
from adapters.inbound.correlation import current_correlation_id, returns_correlation
from adapters.outbound.dynamodb.audit import (
    DynamoAgentQualityAuditStore,
    DynamoAuditFindingStore,
    DynamoEvaluationDeliveryStore,
    DynamoEvaluationVerdictStore,
    DynamoEvaluationWriterReceiptStore,
)
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    METRICS,
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import QUALITY_REVIEW_REQUEST, span, trace_boundary
from domain.conversation.audit import (
    AuditTerminalOutcomeV2,
    ConversationAuditRecordV6,
    require_utc_instant,
)
from domain.evaluation.linkage import (
    EvaluationDeliveryOutcomeV1,
    EvaluationDeliveryReceiptV2,
    EvaluationResultV3,
)
from domain.identity.authorization import QUALITY_REVIEW_TURN_ROUTE, QUALITY_REVIEW_TURNS_ROUTE
from domain.identity.session import IdentityError, ReviewAccessLevel, may_receive_attribution
from domain.shared_kernel.errors import AdlyticsError, ErrorCode, InvariantError
from ports.outbound.audit import (
    DEFAULT_TURNS_PAGE_LIMIT,
    AuditCorrelationRequest,
    EvaluationDeliveryPredatesGate,
    ReceiptTurnsPage,
    ReceiptTurnsPageRequest,
    StoredRecord,
)

SETTINGS = settings_without_client_secret()
REVIEW = quality_review_settings()
"""Every record table this function reads, resolved before it becomes healthy."""
TRACING = configure_tracing(tracing_settings())
LOGGER = Logger(service="adlytics-quality-review")

# Each family is read from the one table its single writer owns (ADR-080 §1). The evaluation
# tables exist only where that plane is provisioned.
STORE = DynamoAgentQualityAuditStore(REVIEW.receipts_table)
FINDINGS = DynamoAuditFindingStore(REVIEW.findings_table)


@dataclass(frozen=True, slots=True)
class _EvaluationStores:
    deliveries: DynamoEvaluationDeliveryStore
    writer_receipts: DynamoEvaluationWriterReceiptStore
    verdicts: DynamoEvaluationVerdictStore


EVALUATION = (
    None
    if REVIEW.evaluation is None
    else _EvaluationStores(
        deliveries=DynamoEvaluationDeliveryStore(REVIEW.evaluation.deliveries_table),
        writer_receipts=DynamoEvaluationWriterReceiptStore(REVIEW.evaluation.writer_receipts_table),
        verdicts=DynamoEvaluationVerdictStore(REVIEW.evaluation.verdicts_table),
    )
)
RESULT_DEADLINE = (
    None
    if REVIEW.evaluation is None
    else timedelta(seconds=REVIEW.evaluation.result_deadline_seconds)
)
"""How long after a dispatched receipt's decision a turn waits for verdicts before overdue."""

TURN = "turn"
TURNS = "turns"
_ROUTES = {QUALITY_REVIEW_TURN_ROUTE: TURN, QUALITY_REVIEW_TURNS_ROUTE: TURNS}
_REVIEW_TABLE_UNCONFIGURED = ErrorCode("AGENT_QUALITY", 4)
_AUTHORIZER_CUSTODY_MISSING = ErrorCode("AGENT_QUALITY", 5)
_TURNS_MEMBERS = frozenset({"window", "filters", "limit", "cursor"})


class _NotFoundError(Exception):
    """The non-disclosing outcome: absent data, a non-unique correlation, or no entitlement."""


def _now() -> datetime:
    return datetime.now(UTC)


def _json(status: int, body: dict[str, object]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Cache-Control": "no-store", "Content-Type": "application/json"},
        "body": json.dumps(body, separators=(",", ":"), sort_keys=True),
    }


def _body(event: dict[str, Any]) -> dict[str, object]:
    raw = event.get("body")
    if not isinstance(raw, str):
        raise ValueError("quality review request is invalid")
    value: object = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("quality review request is invalid")
    return cast("dict[str, object]", value)


def _audit_id(event: dict[str, Any]) -> str | None:
    document_value = _body(event)
    if set(document_value) != {"selector"}:
        raise ValueError("quality review selector is invalid")
    selector_value = document_value.get("selector")
    if not isinstance(selector_value, dict):
        raise ValueError("quality review selector is invalid")
    selector = cast("dict[str, object]", selector_value)
    if len(selector) != 1:
        raise ValueError("quality review selector is invalid")
    audit_id = selector.get("audit_id")
    if isinstance(audit_id, str) and audit_id:
        return audit_id
    correlation = selector.get("correlation_id")
    if not isinstance(correlation, str):
        raise ValueError("quality review selector is invalid")
    records = STORE.discover(AuditCorrelationRequest(correlation_id=correlation))
    return records[0].audit_id if len(records) == 1 else None


def _audit_projection(stored: StoredRecord[ConversationAuditRecordV6]) -> dict[str, object]:
    """Project what happened and its chain of custody, without any content.

    Identity lets an investigator pivot to logs and traces; provenance names the configuration
    the turn ran under (ADR-062); the trajectory is every tool call in order with the anchor
    that reaches its own trace; delivery is the ADR-063 companion, `None` meaning unknown. The
    receipt and its delivery companion each carry the expiry read from their stored items.
    """
    record = stored.record
    delivery = STORE.get_delivery_with_expiry(record.audit_id)
    return {
        "schema_version": "QualityAuditReview.v3",
        "expires_at": stored.expires_at,
        "identity": {
            "audit_id": record.audit_id,
            "correlation_id": record.correlation_id,
            "trace_id": record.trace_id,
            "created_at": record.created_at,
        },
        "provenance": {
            "runtime_version": record.runtime_version,
            "prompt_catalog_digest": record.prompt_catalog_digest,
            "guardrail_version": record.guardrail_version,
            "page_schema_version": record.page_schema_version,
            "response_contract_version": record.response_contract_version,
        },
        "outcome": {
            "operation_category": record.operation_category.value,
            "operation_outcome": record.operation_outcome.value,
            "publication_outcome": record.publication_outcome.value,
            "terminal_outcome": record.terminal_outcome.value,
            "completion_refusal_stage": (
                None
                if record.completion_refusal_stage is None
                else record.completion_refusal_stage.value
            ),
            "failure_error_code": record.failure_error_code,
            "failure_service_code": record.failure_service_code,
        },
        "trajectory": [
            {
                "ordinal": step.ordinal,
                "tool_name": step.tool_name,
                "disposition": step.disposition.value,
                "step_ref": step.step_ref,
                "violations": list(step.violations),
                "selected_into_plan": step.selected_into_plan,
            }
            for step in record.trajectory
        ],
        "publication": {
            "tool_custody_digest": record.tool_custody_digest,
            "presentation_plan_digest": record.presentation_plan_digest,
            "public_block_kinds": [kind.value for kind in record.public_block_kinds],
            "public_block_count": record.public_block_count,
            "released_character_count": record.released_character_count,
            "evaluation_admission_ref": record.evaluation_admission_ref,
        },
        "delivery": (
            None
            if delivery is None
            else {
                "disposition": delivery.record.disposition.value,
                "emitted_block_count": delivery.record.emitted_block_count,
                "recorded_at": delivery.record.recorded_at,
                "expires_at": delivery.expires_at,
            }
        ),
    }


def _delivery_families(receipt: EvaluationDeliveryReceiptV2 | None) -> list[dict[str, object]]:
    if receipt is None:
        return []
    return [
        {
            "family": family.family.value,
            "config_id": family.config_id,
            "evaluators": [
                {
                    "evaluator_id": evaluator.evaluator_id,
                    "evaluator_name": evaluator.evaluator_name,
                    "revision": evaluator.revision,
                    "model_id": evaluator.model_id,
                    "prompt_digest": evaluator.prompt_digest,
                }
                for evaluator in family.evaluators
            ],
        }
        for family in receipt.families
    ]


def _expected_evaluators(receipt: EvaluationDeliveryReceiptV2 | None) -> frozenset[str]:
    """Every evaluator id the turn was dispatched to, across the receipt's families."""
    if receipt is None or receipt.outcome is not EvaluationDeliveryOutcomeV1.DISPATCHED:
        return frozenset()
    return frozenset(
        evaluator.evaluator_id for family in receipt.families for evaluator in family.evaluators
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Progress:
    """What a dispatched turn's evaluation has produced so far, and when it becomes overdue."""

    has_writer: bool
    expected: frozenset[str]
    reported: frozenset[str]
    overdue_at: datetime | None
    now: datetime


def _evaluation_state(
    delivery: EvaluationDeliveryReceiptV2 | EvaluationDeliveryPredatesGate | None,
    progress: _Progress,
) -> tuple[str, str | None]:
    """Return the evaluation state and its closed not-selected reason (ADR-079).

    Only a dispatched receipt can lead to results, so only it reports progressive states; a
    missing receipt is still pending, because the dispatcher may not have run yet. A dispatched
    turn has results when every evaluator it was sent to reported, is partial when only some
    did, and with none waits for the writer, then for the evaluators until the deadline after
    the receipt's decision, then is overdue: sampling, a stranded evaluator, or a broken link
    can each leave a turn without verdicts, and waiting forever would read as health.
    """
    if isinstance(delivery, EvaluationDeliveryPredatesGate):
        return "not_selected", "predates_evaluation_gate"
    if delivery is None:
        return "pending_delivery", None
    if delivery.outcome is EvaluationDeliveryOutcomeV1.NOT_DISPATCHED:
        reason = delivery.not_dispatched_reason
        return "not_selected", None if reason is None else reason.value
    return _dispatched_state(progress), None


def _dispatched_state(progress: _Progress) -> str:
    if progress.reported:
        return "results_available" if progress.reported >= progress.expected else "partial"
    if not progress.has_writer:
        return "pending_writer"
    if progress.overdue_at is not None and progress.now > progress.overdue_at:
        return "overdue"
    return "pending_evaluators"


def _reported(results: tuple[EvaluationResultV3, ...], expected: frozenset[str]) -> frozenset[str]:
    return frozenset(result.evaluator_ref for result in results) & expected


def _evaluation_projection(record: ConversationAuditRecordV6) -> dict[str, object]:
    """Project how good the released answer was and whether the pipeline that judged it ran.

    The turn outcome comes first because an answer the user never received has nothing to
    evaluate (ADR-061). The chain names each protected hop that must exist before a result can
    arrive, and the delivery receipt's outcome and evaluator set; a missing hop is the place to
    look when results are absent. A turn the dispatcher did not send is `not_selected` with its
    closed reason (ADR-079). Results are one per evaluator, each with the provenance it was
    produced under; an evaluator that has not reported yet is simply not listed, and absence is
    never reported as health (ADR-059). The expected and reported evaluator counts say how
    complete the results are. Every chain record and result carries its stored expiry.
    """
    admission_ref = record.evaluation_admission_ref
    projection: dict[str, object] = {
        "schema_version": "QualityEvaluationReview.v3",
        "turn_outcome": {
            "terminal_outcome": record.terminal_outcome.value,
            "publication_outcome": record.publication_outcome.value,
            "public_block_count": record.public_block_count,
            "released_character_count": record.released_character_count,
        },
    }
    if admission_ref is None:
        return {
            **projection,
            "state": "not_selected",
            "reason": "not_admitted",
            "expected_evaluator_count": 0,
            "reported_evaluator_count": 0,
            "chain": None,
            "results": [],
        }
    if EVALUATION is None or RESULT_DEADLINE is None:
        # A selected turn whose evaluation records this function cannot read is unavailable,
        # never reported as pending: absence is not evidence of a pipeline state (ADR-059).
        raise InvariantError(
            _REVIEW_TABLE_UNCONFIGURED, "Evaluation record tables are not configured."
        )
    admission = STORE.get_evaluation_admission_with_expiry(record.audit_id, admission_ref)
    delivery = EVALUATION.deliveries.get_evaluation_delivery_with_expiry(
        record.audit_id, admission_ref
    )
    writer = EVALUATION.writer_receipts.get_evaluation_writer_receipt_with_expiry(
        record.audit_id, admission_ref
    )
    stored_results = EVALUATION.verdicts.get_evaluation_results_with_expiry(
        record.audit_id, admission_ref
    )
    results = tuple(stored.record for stored in stored_results)
    decision = None if delivery is None else delivery.record
    receipt = decision if isinstance(decision, EvaluationDeliveryReceiptV2) else None
    dispatched = receipt is not None and receipt.outcome is EvaluationDeliveryOutcomeV1.DISPATCHED
    expected = _expected_evaluators(receipt)
    reported = _reported(results, expected)
    state, reason = _evaluation_state(
        decision,
        _Progress(
            has_writer=writer is not None,
            expected=expected,
            reported=reported,
            overdue_at=(
                datetime.fromisoformat(receipt.decided_at) + RESULT_DEADLINE
                if receipt is not None and dispatched
                else None
            ),
            now=_now(),
        ),
    )
    return {
        **projection,
        "state": state,
        "reason": reason,
        "expected_evaluator_count": len(expected),
        "reported_evaluator_count": len(reported),
        "chain": {
            "admission_ref": admission_ref,
            "admitted": None if admission is None else admission.record.selected_at,
            "delivery_outcome": None if receipt is None else receipt.outcome.value,
            "delivered_to_queue": receipt.decided_at
            if receipt is not None and dispatched
            else None,
            "families": _delivery_families(receipt),
            "input_emitted": None if writer is None else writer.record.emitted_at,
            "input_trace_id": None if writer is None else writer.record.trace_id,
            "expires_at": {
                "admission": None if admission is None else admission.expires_at,
                "delivery": None if delivery is None else delivery.expires_at,
                "writer": None if writer is None else writer.expires_at,
            },
        },
        "results": [
            {
                "evaluator_ref": stored.record.evaluator_ref,
                "evaluator_name": stored.record.evaluator_name,
                "state": stored.record.state.value,
                "label": stored.record.label,
                "score": stored.record.score,
                "evaluated_at": stored.record.evaluated_at,
                "family": stored.record.family.value,
                "config_id": stored.record.config_id,
                "evaluator_revision": stored.record.evaluator_revision,
                "model_id": stored.record.model_id,
                "prompt_digest": stored.record.prompt_digest,
                "expires_at": stored.expires_at,
            }
            for stored in stored_results
        ],
    }


def _findings_projection(record: ConversationAuditRecordV6) -> dict[str, object]:
    """Project the defect pattern: what went wrong, where in the trajectory, and under what.

    A finding anchored to a step carries that step's tool, disposition, and violation codes so
    the reader sees the defect beside the call that produced it without a second lookup. The
    provenance block is what a fix has to be tested against (ADR-062): the same prompt digest and
    Guardrail version recur across findings when the defect is systematic rather than incidental.
    Each finding carries the expiry of its stored item.
    """
    steps = {step.ordinal: step for step in record.trajectory}
    findings: list[dict[str, object]] = []
    for stored in FINDINGS.get_findings_with_expiry(record.audit_id):
        finding = stored.record
        step = None if finding.step_ordinal is None else steps.get(finding.step_ordinal)
        findings.append(
            {
                "kind": finding.kind.value,
                "severity": finding.severity.value,
                "created_at": finding.created_at,
                "expires_at": stored.expires_at,
                "step": (
                    None
                    if step is None
                    else {
                        "ordinal": step.ordinal,
                        "tool_name": step.tool_name,
                        "disposition": step.disposition.value,
                        "violations": list(step.violations),
                        "step_ref": step.step_ref,
                    }
                ),
            }
        )
    return {
        "schema_version": "QualityFindingsReview.v3",
        "audit_id": record.audit_id,
        "terminal_outcome": record.terminal_outcome.value,
        "provenance": {
            "runtime_version": record.runtime_version,
            "prompt_catalog_digest": record.prompt_catalog_digest,
            "guardrail_version": record.guardrail_version,
        },
        "findings": findings,
    }


def _turn_review(
    stored: StoredRecord[ConversationAuditRecordV6], levels: frozenset[ReviewAccessLevel]
) -> dict[str, object]:
    """Return each section the verified levels allow, rechecking every one of them here.

    The principal appears only in `attribution`, and only for a caller who holds both
    `quality_reviewer` and `audit_attribution` (ADR-081).
    """
    review: dict[str, object] = {"schema_version": "QualityTurnReview.v1"}
    if ReviewAccessLevel.QUALITY_REVIEWER in levels:
        review["audit"] = _audit_projection(stored)
        review["findings"] = _findings_projection(stored.record)
        review["evaluation"] = _evaluation_projection(stored.record)
        if may_receive_attribution(levels):
            review["attribution"] = {
                "principal": stored.record.principal,
                "expires_at": stored.expires_at,
            }
    return review


def _turn(event: dict[str, Any], levels: frozenset[ReviewAccessLevel]) -> dict[str, object]:
    audit_id = _audit_id(event)
    if audit_id is None:
        raise _NotFoundError
    stored = STORE.get_with_expiry(audit_id)
    if stored is None:
        raise _NotFoundError
    return _turn_review(stored, levels)


def _object(
    value: object, members: frozenset[str], *, required: frozenset[str]
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("quality review turns request is invalid")
    document = cast("dict[str, object]", value)
    if not set(document) <= members or not required <= set(document):
        raise ValueError("quality review turns request is invalid")
    return document


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("quality review turns window is invalid")
    return require_utc_instant(value, "turns window bound")


def _terminal_outcomes(value: object) -> frozenset[AuditTerminalOutcomeV2]:
    filters = _object(
        value, frozenset({"terminal_outcome"}), required=frozenset({"terminal_outcome"})
    )
    raw = filters["terminal_outcome"]
    if not isinstance(raw, list) or not raw:
        raise ValueError("quality review turns filter is invalid")
    values = cast("list[object]", raw)
    if not all(isinstance(item, str) for item in values) or len(set(values)) != len(values):
        raise ValueError("quality review turns filter is invalid")
    return frozenset(AuditTerminalOutcomeV2(cast("str", item)) for item in values)


def turns_request(event: dict[str, Any]) -> ReceiptTurnsPageRequest:
    """Strictly decode the `turns` request: a window, optional filters, limit, and cursor."""
    document = _object(_body(event), _TURNS_MEMBERS, required=frozenset({"window"}))
    window = _object(
        document["window"], frozenset({"from", "to"}), required=frozenset({"from", "to"})
    )
    cursor = document.get("cursor")
    if "cursor" in document and not isinstance(cursor, str):
        raise ValueError("quality review turns cursor is invalid")
    limit = document.get("limit", DEFAULT_TURNS_PAGE_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("quality review turns limit is invalid")
    return ReceiptTurnsPageRequest(
        window_from=_instant(window["from"]),
        window_to=_instant(window["to"]),
        terminal_outcomes=(
            _terminal_outcomes(document["filters"]) if "filters" in document else frozenset()
        ),
        limit=limit,
        cursor=cast("str | None", cursor),
    )


def _turns_page(page: ReceiptTurnsPage) -> dict[str, object]:
    return {
        "schema_version": "QualityTurnsPage.v1",
        "turns": [
            {
                "audit_id": row.audit_id,
                "correlation_id": row.correlation_id,
                "created_at": row.created_at,
                "terminal_outcome": row.terminal_outcome.value,
                "expires_at": row.expires_at,
            }
            for row in page.turns
        ],
        "cursor": page.cursor,
    }


def _respond(route: str | None, event: dict[str, Any]) -> dict[str, object]:
    """Recheck the verified levels, then serve the route; raise `_NotFoundError` to not disclose."""
    try:
        events.authorizer_subject(event)
        levels = events.authorizer_review_levels(event)
    except IdentityError as error:
        # Only a route that lost its authorizer, or an authorizer that stopped writing its
        # context, reaches this: a fault to surface, never a caller's mistake.
        raise InvariantError(
            _AUTHORIZER_CUSTODY_MISSING,
            "The review request carries no verified authorizer context.",
        ) from error
    if route is None or ReviewAccessLevel.QUALITY_REVIEWER not in levels:
        # `audit_attribution` alone grants nothing, and must not learn whether a turn exists.
        raise _NotFoundError
    if route == TURN:
        return _turn(event, levels)
    return _turns_page(STORE.list_turns(turns_request(event)))


def _terminal(operation: str, outcome: str, status: int) -> None:
    report_operation(
        OperationLog(
            operation=operation,
            component="quality_review_api",
            phase="terminal",
            outcome=outcome,
            severity=LogSeverity.INFO if status == 200 else LogSeverity.WARNING,  # noqa: PLR2004
            details={"status": status},
        ),
        LOGGER,
    )


@returns_correlation(LOGGER)
@trace_boundary(QUALITY_REVIEW_REQUEST)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, Any]:
    """Serve `turn` or `turns` by route key after authorizer custody is present."""
    route_key = event.get("routeKey")
    route = _ROUTES.get(route_key) if isinstance(route_key, str) else None
    operation = f"quality_review.{route or 'unknown'}"
    with span(QUALITY_REVIEW_REQUEST, operation=operation) as process_span:
        report_operation(
            OperationLog(
                operation=operation,
                component="quality_review_api",
                phase="start",
                outcome="started",
                severity=LogSeverity.DEBUG,
            ),
            LOGGER,
        )
        try:
            body = _respond(route, event)
        except _NotFoundError:
            _terminal(operation, "not_found", 404)
            process_span.record(outcome="not_found", status=404)
            return _json(404, {"error": "not_found"})
        except IdentityError, ValueError:
            _terminal(operation, "refused", 400)
            process_span.record(outcome="refused", status=400)
            return _json(400, {"error": "invalid_request"})
        except Exception as error:
            if isinstance(error, AdlyticsError):
                process_span.note_error(error)
            else:
                process_span.note_failure(error)
            report_error(
                error,
                Report(operation=operation, correlation_id=current_correlation_id(), logger=LOGGER),
            )
            process_span.record(outcome="failed", status=503)
            return _json(503, {"error": "unavailable"})
        _terminal(operation, "completed", 200)
        process_span.record(outcome="completed", status=200)
        return _json(200, body)
