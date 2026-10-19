# SPDX-License-Identifier: MIT-0
"""Strict prompt-registry operator commands with authorizer-owned identity."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from aws_lambda_powertools import Logger

from adapters.config import settings_without_client_secret, tracing_settings
from adapters.inbound.auth_handlers import events
from adapters.inbound.correlation import current_correlation_id, returns_correlation
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.outbound.cognito.operator_directory import (
    CognitoOperatorDirectory,
    OperatorDirectoryRequest,
)
from adapters.outbound.dynamodb.insight_prompt_registry import (
    PROMPT_REGISTRY_MALFORMED,
    DynamoInsightPromptRegistry,
)
from adapters.outbound.sqs.insight_prompt_calibration import (
    SqsInsightPromptCalibrationQueue,
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
from adapters.outbound.telemetry.tracing import (
    PROMPT_ADMIN_REQUEST,
    Recording,
    span,
    trace_boundary,
)
from domain.identity.operator_directory import OperatorEmailResolution
from domain.identity.session import IdentityError
from domain.presentation.insights import (
    PROMPT_ACTIVATION_CALIBRATION_REQUIRED,
    InsightModelRoute,
    InsightPromptActivationRequest,
    InsightPromptRetirementRequest,
    InsightPromptTier,
    InsightPromptVersionCreateRequest,
    InsightPromptVersionRecord,
    InsightSlotId,
)
from domain.presentation.prompt_authorship import (
    OperatorPromptAuthor,
    PromptAttributionKind,
    PromptVersionAttribution,
    prompt_version_attribution,
)
from domain.presentation.prompt_composition import PromptFragmentDigests
from domain.shared_kernel.errors import AdlyticsError, DependencyError, InvariantError
from ports.outbound.operator_directory import OperatorDirectoryPort

SETTINGS = settings_without_client_secret()
TRACING = configure_tracing(tracing_settings())
LOGGER = Logger(service="adlytics-prompt-admin")
ROUTE = SETTINGS.prompt_admin_route
# ADR-078 §1 path-scoped body-size exclusion: the decoded request-body ceiling, in bytes. Equal to
# the edge's PROMPT_CREATE_VERSION_MAX_BODY_BYTES (infra/lib/config), asserted by the infra tests.
MAX_BODY_BYTES = 32_768
if not SETTINGS.insight_prompt_registry_table:
    raise ValueError("prompt administration requires an insight prompt registry table.")
STORE = DynamoInsightPromptRegistry(SETTINGS.insight_prompt_registry_table)
CONTRACTS = {contract.id: contract for _, contract in load_insight_contracts(contracts_root())}
QUEUE = (
    SqsInsightPromptCalibrationQueue(SETTINGS.insight_prompt_calibration_queue_url)
    if SETTINGS.insight_prompt_calibration_queue_url
    else None
)


def _versions_report() -> Report:
    return Report(
        operation="prompt_admin.versions",
        correlation_id=current_correlation_id(),
        logger=LOGGER,
    )


# Built only for the versions route: the other five functions never construct a Cognito client,
# and only the versions role holds cognito-idp:AdminGetUser (ADR-069).
DIRECTORY: OperatorDirectoryPort | None = (
    CognitoOperatorDirectory(
        OperatorDirectoryRequest(
            SETTINGS.cognito_user_pool_id,
            SETTINGS.aws_region,
            SETTINGS.operator_directory_max_concurrency,
        ),
        _versions_report,
    )
    if ROUTE == "versions"
    else None
)


def _json(status: int, body: dict[str, object]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Cache-Control": "no-store", "Content-Type": "application/json"},
        "body": json.dumps(body, separators=(",", ":"), sort_keys=True),
    }


class _BodyTooLargeError(Exception):
    """A request body over `MAX_BODY_BYTES`, refused before it is parsed."""

    def __init__(self, size: int) -> None:
        super().__init__("prompt administration request body exceeds its ceiling")
        self.size = size


def _body_bytes(event: dict[str, Any]) -> int:
    """Return the decoded body length in bytes; a missing body is 0, and `_body` refuses it."""
    raw = event.get("body")
    if not isinstance(raw, str):
        return 0
    if event.get("isBase64Encoded") is True:
        try:
            return len(base64.b64decode(raw, validate=True))
        except binascii.Error as error:
            raise ValueError("prompt administration request is invalid") from error
    return len(raw.encode("utf-8"))


def _body(event: dict[str, Any]) -> dict[str, object]:
    raw = event.get("body")
    if not isinstance(raw, str):
        raise ValueError("prompt administration request is invalid")
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("prompt administration request is invalid") from error
    if not isinstance(decoded, dict):
        raise ValueError("prompt administration request is invalid")
    return cast("dict[str, object]", decoded)


def _exact(document: dict[str, object], fields: set[str]) -> None:
    if set(document) != fields:
        raise ValueError("prompt administration request is invalid")


def _required_with_optional(
    document: dict[str, object], required: set[str], optional: set[str]
) -> None:
    if not required.issubset(document) or not set(document).issubset(required | optional):
        raise ValueError("prompt administration request is invalid")


def _text(document: dict[str, object], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("prompt administration request is invalid")
    return value


def _version(document: dict[str, object], name: str) -> int:
    value = document.get(name)
    if type(value) is not int or value < 1:
        raise ValueError("prompt administration request is invalid")
    return value


def _slot(document: dict[str, object]) -> InsightSlotId:
    try:
        return InsightSlotId(_text(document, "slot_id"))
    except ValueError as error:
        raise ValueError("prompt administration request is invalid") from error


def _model_routing_hint(document: dict[str, object]) -> InsightModelRoute:
    value = document.get("model_routing_hint")
    if value is None:
        return InsightModelRoute.QUICK
    try:
        return InsightModelRoute(_text(document, "model_routing_hint"))
    except ValueError as error:
        raise ValueError("prompt administration request is invalid") from error


def _tier(document: dict[str, object]) -> InsightPromptTier:
    value = document.get("tier")
    if value is None:
        return InsightPromptTier.CARD
    try:
        return InsightPromptTier(_text(document, "tier"))
    except ValueError as error:
        raise ValueError("prompt administration request is invalid") from error


def _fragment_digests(document: dict[str, object]) -> PromptFragmentDigests | None:
    value = document.get("fragment_digests")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("prompt administration request is invalid")
    digests = cast("dict[str, object]", value)
    if set(digests) != {"foundation", "tier_core", "slot_block"} or not all(
        isinstance(item, str) for item in digests.values()
    ):
        raise ValueError("prompt administration request is invalid")
    try:
        return PromptFragmentDigests(
            cast("str", digests["foundation"]),
            cast("str", digests["tier_core"]),
            cast("str", digests["slot_block"]),
        )
    except ValueError as error:
        raise ValueError("prompt administration request is invalid") from error


def _enqueue_retained_work(slot_id: InsightSlotId, version: int) -> bool:
    """Try immediate dispatch; the atomic registry outbox remains recovery authority."""
    if QUEUE is None:
        return False
    try:
        QUEUE.enqueue(slot_id, version)
        STORE.acknowledge_calibration_outbox(slot_id, version)
        return True
    except Exception as error:
        report_error(
            error,
            Report(
                operation=f"prompt_admin.{ROUTE or 'unknown'}",
                correlation_id=current_correlation_id(),
                logger=LOGGER,
            ),
        )
        return False


def _created_by(attribution: PromptVersionAttribution) -> dict[str, object]:
    """Project the closed attribution union; the Cognito subject never reaches the browser."""
    if attribution.kind is PromptAttributionKind.OPERATOR_EMAIL:
        return {"kind": attribution.kind.value, "email": attribution.email}
    if attribution.system_author is not None:
        return {"kind": attribution.kind.value, "name": attribution.system_author.value}
    return {"kind": attribution.kind.value}


def _version_record(
    value: InsightPromptVersionRecord, attribution: PromptVersionAttribution
) -> dict[str, object]:
    return {
        "slot_id": value.slot_id.value,
        "version": value.version,
        "prompt": value.prompt,
        "model_route": value.model_route.value,
        "tier": value.tier.value,
        # Composed versions carry the digests of their exact git-sourced fragments;
        # a version without them is unsourced (direct or seeded), stated explicitly.
        **(
            {"unsourced": True}
            if value.fragment_digests is None
            else {
                "fragment_digests": {
                    "foundation": value.fragment_digests.foundation,
                    "tier_core": value.fragment_digests.tier_core,
                    "slot_block": value.fragment_digests.slot_block,
                }
            }
        ),
        "created_by": _created_by(attribution),
        "created_at": (
            None if value.created_at is None else value.created_at.astimezone(UTC).isoformat()
        ),
        "calibration": value.calibration,
        "calibration_cases": [
            {"kind": item.kind.value, "outcome": item.outcome.value}
            for item in value.calibration_cases
        ],
    }


def _lifecycle_event(
    request: InsightPromptActivationRequest | InsightPromptRetirementRequest,
    to_version: int | None,
) -> dict[str, object]:
    return {
        "slot_id": request.slot_id.value,
        "from_version": request.expected_active_version,
        "to_version": to_version,
        "operator": request.operator,
        "at": request.at.astimezone(UTC).isoformat(),
    }


def _versions(document: dict[str, object]) -> tuple[int, dict[str, object], dict[str, object]]:
    """Read one slot's versions and attribute each to its author's email, resolved now."""
    if DIRECTORY is None:
        raise RuntimeError("operator directory is not configured for the versions route")
    _exact(document, {"slot_id"})
    slot_id = _slot(document)
    state = STORE.slot_states((slot_id,))[0]
    records = STORE.versions(slot_id)
    subjects = frozenset(
        record.author.subject
        for record in records
        if isinstance(record.author, OperatorPromptAuthor)
    )
    emails: Mapping[str, OperatorEmailResolution] = (
        DIRECTORY.email_addresses(subjects) if subjects else {}
    )
    attributions = [prompt_version_attribution(record.author, emails) for record in records]
    unavailable = sum(
        attribution.kind is PromptAttributionKind.OPERATOR_UNAVAILABLE
        for attribution in attributions
    )
    return (
        200,
        {
            "slot_id": slot_id.value,
            "active_version": state.active_version,
            "retired": (state.newest_version is not None and state.active_version is None),
            "versions": [
                _version_record(record, attribution)
                for record, attribution in zip(records, attributions, strict=True)
            ],
        },
        # Bounded counts only: never a subject or an email address.
        {"slot_id": slot_id.value, "versions": len(records), "authors_unavailable": unavailable},
    )


def _dispatch(
    document: dict[str, object], operator: str
) -> tuple[int, dict[str, object], dict[str, object]]:
    if ROUTE == "slots":
        _exact(document, set())
        contracts = tuple(CONTRACTS.values())
        states = {state.slot_id: state for state in STORE.slot_states(tuple(CONTRACTS))}
        return (
            200,
            {
                "slots": [
                    {
                        "slot_id": contract.id.value,
                        "display_name": contract.display_name,
                        "purpose": contract.purpose,
                        "output_class": contract.output_class.value,
                        "silence_condition": contract.silence_condition,
                        "active_version": states[contract.id].active_version,
                        "active_deep_version": states[contract.id].active_deep_version,
                        "newest_version": states[contract.id].newest_version,
                        "newest_calibration": states[contract.id].newest_calibration,
                        "retired": (
                            states[contract.id].newest_version is not None
                            and states[contract.id].active_version is None
                        ),
                    }
                    for contract in contracts
                ]
            },
            {"slots": len(contracts)},
        )
    if ROUTE == "versions":
        return _versions(document)
    if ROUTE == "create-version":
        _required_with_optional(
            document,
            {"slot_id", "prompt_template"},
            {"model_routing_hint", "tier", "fragment_digests"},
        )
        slot_id = _slot(document)
        request = InsightPromptVersionCreateRequest(
            slot_id,
            _text(document, "prompt_template"),
            _model_routing_hint(document),
            tier=_tier(document),
            fragment_digests=_fragment_digests(document),
            author=OperatorPromptAuthor(operator),
            created_at=datetime.now(UTC),
        )
        version = STORE.create_version(request)
        queued = _enqueue_retained_work(slot_id, version)
        return (
            202,
            {"slot_id": slot_id.value, "version": version, "outcome": "pending"},
            {"slot_id": slot_id.value, "version": version, "queued": queued},
        )
    if ROUTE == "calibrate":
        _exact(document, {"slot_id", "version"})
        slot_id = _slot(document)
        version = _version(document, "version")
        state = STORE.calibration_work_state(slot_id, version, int(datetime.now(UTC).timestamp()))
        queued = _enqueue_retained_work(slot_id, version) if state.should_enqueue else False
        status = 202 if state.calibration == "pending" else 200
        return (
            status,
            {"slot_id": slot_id.value, "version": version, "outcome": state.calibration},
            {"slot_id": slot_id.value, "version": version, "queued": queued},
        )
    if ROUTE == "activate":
        _exact(document, {"slot_id", "version", "expected_active_version"})
        slot_id = _slot(document)
        version = _version(document, "version")
        expected = document.get("expected_active_version")
        if expected is not None and (type(expected) is not int or expected < 1):
            raise ValueError("prompt administration request is invalid")
        request = InsightPromptActivationRequest(
            slot_id,
            version,
            expected,
            operator,
            uuid4().hex,
            datetime.now(UTC),
        )
        STORE.activate(request)
        return (
            200,
            {"event": _lifecycle_event(request, version)},
            {"slot_id": slot_id.value, "version": version},
        )
    if ROUTE == "retire":
        _required_with_optional(document, {"slot_id", "expected_active_version"}, {"tier"})
        slot_id = _slot(document)
        expected = _version(document, "expected_active_version")
        request = InsightPromptRetirementRequest(
            slot_id, expected, operator, uuid4().hex, datetime.now(UTC), tier=_tier(document)
        )
        STORE.retire(request)
        return (
            200,
            {"event": _lifecycle_event(request, None)},
            {"slot_id": slot_id.value, "version": expected},
        )
    raise ValueError("prompt administration route is invalid")


def _invariant_response(
    error: InvariantError, operation: str, process_span: Recording
) -> dict[str, Any]:
    """Answer a registry invariant: a malformed record alarms as 503; the rest are 409s."""
    process_span.note_error(error)
    if error.code == PROMPT_REGISTRY_MALFORMED:
        # A retained record that cannot be decoded is a fault that alarms, never a
        # conflict someone else caused.
        report_error(
            error,
            Report(
                operation=operation,
                correlation_id=current_correlation_id(),
                logger=LOGGER,
            ),
        )
        process_span.record(outcome="failed", status=503)
        return _json(503, {"error": "unavailable"})
    if error.code == PROMPT_ACTIVATION_CALIBRATION_REQUIRED:
        report_operation(
            OperationLog(
                operation=operation,
                component="prompt_admin_api",
                phase="terminal",
                outcome="refused",
                severity=LogSeverity.WARNING,
                description=error.user_visible_description,
                details={"status": 409},
            ),
            LOGGER,
        )
        process_span.record(outcome="refused", status=409)
        return _json(409, {"error": "calibration_required"})
    report_operation(
        OperationLog(
            operation=operation,
            component="prompt_admin_api",
            phase="terminal",
            outcome="conflict",
            severity=LogSeverity.WARNING,
            description=error.user_visible_description,
            details={"status": 409},
        ),
        LOGGER,
    )
    process_span.record(outcome="conflict", status=409)
    return _json(409, {"error": "conflict"})


@returns_correlation(LOGGER)
@trace_boundary(PROMPT_ADMIN_REQUEST)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, Any]:
    """Apply one registry command without logging prompt content or accepting body identity."""
    operation = f"prompt_admin.{ROUTE or 'unknown'}"
    with span(PROMPT_ADMIN_REQUEST, operation=operation) as process_span:
        report_operation(
            OperationLog(
                operation=operation,
                component="prompt_admin_api",
                phase="start",
                outcome="started",
                severity=LogSeverity.DEBUG,
            ),
            LOGGER,
        )
        try:
            if not events.authorizer_can_manage_prompts(event):
                raise ValueError("prompt administration request is invalid")
            size = _body_bytes(event)
            if size > MAX_BODY_BYTES:
                raise _BodyTooLargeError(size)
            operator = events.authorizer_subject(event)
            status, body, details = _dispatch(_body(event), operator)
        except _BodyTooLargeError as error:
            # No body content is logged: only its size and the ceiling.
            report_operation(
                OperationLog(
                    operation=operation,
                    component="prompt_admin_api",
                    phase="terminal",
                    outcome="refused",
                    severity=LogSeverity.WARNING,
                    description=str(error),
                    details={
                        "status": 413,
                        "body_bytes": error.size,
                        "max_body_bytes": MAX_BODY_BYTES,
                    },
                ),
                LOGGER,
            )
            process_span.record(outcome="refused", status=413)
            return _json(413, {"error": "request_too_large"})
        except (IdentityError, ValueError) as error:
            report_operation(
                OperationLog(
                    operation=operation,
                    component="prompt_admin_api",
                    phase="terminal",
                    outcome="refused",
                    severity=LogSeverity.WARNING,
                    description=str(error),
                    details={"status": 400},
                ),
                LOGGER,
            )
            process_span.record(outcome="refused", status=400)
            return _json(400, {"error": "invalid_request"})
        except InvariantError as error:
            return _invariant_response(error, operation, process_span)
        except DependencyError as error:
            process_span.note_error(error)
            report_error(
                error,
                Report(operation=operation, correlation_id=current_correlation_id(), logger=LOGGER),
            )
            process_span.record(outcome="failed", status=503)
            return _json(503, {"error": "unavailable"})
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

        report_operation(
            OperationLog(
                operation=operation,
                component="prompt_admin_api",
                phase="terminal",
                outcome="completed",
                severity=LogSeverity.INFO,
                details={"status": status, **details},
            ),
            LOGGER,
        )
        process_span.record(outcome="completed", status=status)
        return _json(status, body)
