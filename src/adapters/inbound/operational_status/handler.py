# SPDX-License-Identifier: MIT-0
"""Protected no-store read API for observation-only operational data-flow status.

S2 adds `POST /api/operations/execution-explanations` (FR-8), which reads graph authority plus
the status v2 projection through read-only ports (`explanation.py`). Its every typed failure,
including the unauthorized absence collapse, is reported exactly once by this module's outer
boundary, inside both flush wrappers.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, cast

import boto3
from aws_lambda_powertools import Logger

from adapters.config import operational_api_settings, run_request_api_settings, tracing_settings
from adapters.inbound.auth_handlers import events
from adapters.inbound.correlation import current_correlation_id, returns_correlation
from adapters.inbound.operational_status.explanation import ExplanationDependencies, explain
from adapters.outbound.dynamodb.execution_graph_reader import DynamoExecutionGraphReader
from adapters.outbound.dynamodb.operational_status import (
    DynamoGraphProjectionStore,
    DynamoOperationalStatusStore,
    encode_cursor,
)
from adapters.outbound.kms.cursor_mac import KmsCursorMac, cursor_mac_alias
from adapters.outbound.s3.manifest_versions import S3ManifestVersions
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import (
    OPERATIONAL_STATUS_REQUEST,
    span,
    trace_boundary,
)
from domain.identity.session import IdentityError
from domain.operations.execution_cursor import CursorCapacityLimitedError, CursorInvalidError
from domain.operations.flow_explain import (
    ExplanationInvalidError,
    ExplanationRefusal,
    ExplanationUnavailableError,
    parse_explanation_request,
)
from domain.operations.generated_data_flows import REGISTRY_VERSION, SURFACES
from domain.operations.status import (
    FlowState,
    OperationalStatusError,
    canonical_uuid,
    definitions_document,
)

_DEFINITIONS = "/api/operations/data-flow-definitions"
_FLOWS = "/api/operations/data-flows"
_EXPLANATIONS = "/api/operations/execution-explanations"
_MAX_EXPLANATION_BODY_BYTES = 8192
_EVENTS_ROUTE_PARTS = 2
_SUMMARY_FILTERS = frozenset({"surface", "state", "freshness"})
_FRESHNESS_FILTERS = frozenset({"current", "stale", "unavailable"})
_ACTIVE_SURFACES = frozenset(
    cast("str", surface["id"]) for surface in SURFACES if surface["status"] == "active"
)
_SETTINGS = operational_api_settings()
_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
_LOGGER = Logger(service="adlytics-operational-status-api")
_OPERATION = OPERATIONAL_STATUS_REQUEST
_COMPONENT = "operational_status_api"
_STORE = DynamoOperationalStatusStore(_SETTINGS.status_table)
_GRAPH_SETTINGS = run_request_api_settings()
_EXPLANATION = ExplanationDependencies(
    authority=DynamoExecutionGraphReader(
        _GRAPH_SETTINGS.execution_graph_table, boto3.client("dynamodb")
    ),
    projection=DynamoGraphProjectionStore(_SETTINGS.status_table),
    manifests=S3ManifestVersions(_GRAPH_SETTINGS.raw_bucket, boto3.client("s3")),
    cursor_mac=KmsCursorMac(
        cursor_mac_alias(_GRAPH_SETTINGS.execution_graph_table), boto3.client("kms")
    ),
)
# The response status of each typed explanation failure; the unauthorized collapse keeps the
# existing absence response.
_REFUSAL_STATUS = {
    ExplanationRefusal.INVALID_REFERENCE: 400,
    ExplanationRefusal.FLOW_NOT_FOUND: 404,
    ExplanationRefusal.MANIFEST_NOT_FOUND: 404,
    ExplanationRefusal.AMBIGUOUS_MANIFEST: 409,
    ExplanationRefusal.EXPLANATION_TOO_LARGE: 413,
}

report_operation(
    OperationLog(
        operation=_OPERATION,
        component=_COMPONENT,
        phase="start",
        outcome="active" if _TRACING else "disabled",
        severity=LogSeverity.INFO,
        details={
            "stage": "startup",
            "tracing": _TRACING,
            "sample_ratio": _TRACING_SETTINGS.sample_ratio,
        },
    ),
    _LOGGER,
)


def _json(status: int, body: dict[str, object]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
            "Pragma": "no-cache",
        },
        "body": json.dumps(body, sort_keys=True, separators=(",", ":")),
    }


def _absence() -> dict[str, Any]:
    """One response for malformed, absent, expired, cursor-invalid, or unauthorized access."""
    return _json(404, {"error": "not_found"})


def _reading(state: str, reason: str | None) -> tuple[str, str]:
    if state == FlowState.FAILED.value:
        return "The authority reported a failed flow.", "inspect_diagnostic"
    if state == FlowState.BLOCKED.value:
        return "The authority reported a blocked flow.", "escalate"
    if state in {FlowState.RETRYING.value, FlowState.DELAYED.value}:
        return "The authority reported work awaiting another attempt.", "observe"
    if state == FlowState.COMPLETED.value:
        return "The owning authority recorded a terminal decision.", "observe"
    if reason is not None:
        return "The flow has an authority-backed operational reason.", "retry_via_owner"
    return "The latest authority-backed transition is shown.", "observe"


def _summary(query: dict[str, str]) -> dict[str, object]:
    if not set(query) <= _SUMMARY_FILTERS:
        raise OperationalStatusError("summary filter is not allowed")
    surface_filter = query.get("surface")
    state_filter = query.get("state")
    freshness_filter = query.get("freshness")
    surface_ids = {cast("str", surface["id"]) for surface in SURFACES}
    if surface_filter is not None and surface_filter not in surface_ids:
        raise OperationalStatusError("surface filter is invalid")
    if state_filter is not None:
        FlowState(state_filter)
    if freshness_filter is not None and freshness_filter not in _FRESHNESS_FILTERS:
        raise OperationalStatusError("freshness filter is invalid")
    selected = [
        surface for surface in SURFACES if surface_filter is None or surface["id"] == surface_filter
    ]
    aggregate_states: Counter[str] = Counter()
    latest_observed: str | None = None
    surface_documents: list[dict[str, object]] = []
    for surface in selected:
        identifier = cast("str", surface["id"])
        active = identifier in _ACTIVE_SURFACES
        events = _STORE.surface(identifier, 100) if active else ()
        states = Counter(cast("str", item["state"]) for item in events)
        aggregate_states.update(states)
        observed = max((cast("str", item["occurred_at"]) for item in events), default=None)
        if observed is not None and (latest_observed is None or observed > latest_observed):
            latest_observed = observed
        lifecycle = (
            max(states, key=lambda state: states[state]) if states else FlowState.UNKNOWN.value
        )
        freshness = "current" if observed is not None else "unavailable"
        if state_filter is not None and lifecycle != state_filter:
            continue
        if freshness_filter is not None and freshness != freshness_filter:
            continue
        reading, next_action = _reading(lifecycle, None)
        completed = states[FlowState.COMPLETED.value]
        terminal = completed + states[FlowState.FAILED.value]
        surface_documents.append(
            {
                "surface": identifier,
                "evaluation_mode": "observation_only",
                "state": lifecycle,
                "freshness": freshness,
                "active": states[FlowState.RUNNING.value],
                "queued": states[FlowState.QUEUED.value],
                "retrying": states[FlowState.RETRYING.value],
                "blocked": states[FlowState.BLOCKED.value],
                "failed": states[FlowState.FAILED.value],
                "completed_rate": 0.0 if terminal == 0 else completed / terminal,
                "evidence": "complete" if observed is not None else "unavailable",
                "baseline_windows": (
                    _baseline_documents(cast("dict[str, object]", surface)) if active else []
                ),
                "objective_windows": [],
                "reason_code": None,
                "reading": reading,
                "next_action": next_action,
            }
        )
    status_counts = Counter(cast("str", surface["status"]) for surface in SURFACES)
    return {
        "schema_version": "1",
        "observed_at": latest_observed,
        "reconciled_at": latest_observed,
        "evidence": "complete" if latest_observed is not None else "unavailable",
        "registry": {
            "version": REGISTRY_VERSION,
            "active": status_counts["active"],
            "incomplete": status_counts["planned"],
            "external_boundaries": status_counts["external_boundary"],
        },
        "overall_coverage": "incomplete",
        "aggregate_lifecycle_state": _aggregate_state(aggregate_states, latest_observed),
        "surfaces": surface_documents,
    }


def _baseline_documents(surface: dict[str, object]) -> list[dict[str, object]]:
    identifier = cast("str", surface["id"])
    measurements = cast("list[dict[str, object]]", surface["measurements"])
    documents: list[dict[str, object]] = []
    for measurement in measurements:
        for window in _STORE.baselines(identifier, cast("str", measurement["measurement_ref"])):
            documents.append(
                {
                    "window_id": window.window_id,
                    "predecessor_window_id": window.predecessor_window_id,
                    "revision": window.revision,
                    "measurement_ref": window.measurement_ref,
                    "measurement_version": window.measurement_version,
                    "window_start": window.window_start.isoformat(),
                    "window_end": window.window_end.isoformat(),
                    "population_cutoff": window.population_cutoff.isoformat(),
                    "finalized_at": window.finalized_at.isoformat(),
                    "completeness": window.completeness,
                    "late_sample_count": window.late_sample_count,
                    "sample_count": window.sample_count,
                }
            )
    return documents


def _aggregate_state(states: Counter[str], observed_at: str | None) -> str:
    if observed_at is None:
        return FlowState.UNKNOWN.value
    for state in (
        FlowState.FAILED,
        FlowState.BLOCKED,
        FlowState.DELAYED,
        FlowState.RETRYING,
        FlowState.RUNNING,
        FlowState.QUEUED,
        FlowState.OBSERVED,
        FlowState.COMPLETED,
    ):
        if states[state.value]:
            return state.value
    return FlowState.UNKNOWN.value


def _flow(flow_id: str) -> dict[str, object] | None:
    snapshot = _STORE.flow(flow_id)
    if snapshot is None:
        return None
    state = cast("str", snapshot["state"])
    reason = cast("str | None", snapshot["reason_code"])
    reading, next_action = _reading(state, reason)
    authority = cast("dict[str, object]", snapshot["authority"])
    return {
        "schema_version": "1",
        "flow_id": flow_id,
        "parent_flow_id": snapshot["parent_flow_id"],
        "causation_event_id": snapshot["causation_event_id"],
        "execution_id": snapshot["execution_id"],
        "predecessor_execution_id": snapshot["predecessor_execution_id"],
        "latest_event_id": snapshot["event_id"],
        "latest_sequence": snapshot["transition_sequence"],
        "surface": snapshot["surface"],
        "evaluation_mode": "observation_only",
        "stage": snapshot["stage"],
        "state": state,
        "outcome": snapshot["outcome"],
        "continuity": {"state": "unknown", "served_revision_relation": "not_applicable"},
        "observation_samples": [],
        "objective_samples": [],
        "progress": snapshot["progress"],
        "authority": {"kind": authority["kind"], "observed_at": authority["observed_at"]},
        "evidence": "complete",
        "freshness": "current",
        "observed_at": snapshot["occurred_at"],
        "reconciled_at": snapshot["occurred_at"],
        "reason_code": reason,
        "reading": reading,
        "next_action": next_action,
        "required_children": {
            "declared": len(cast("list[object]", snapshot["required_child_flow_ids"])),
            "successful": 0,
            "terminal": 0,
        },
        "optional_children": {
            "declared": len(cast("list[object]", snapshot["optional_child_flow_ids"])),
            "successful": 0,
            "terminal": 0,
        },
        "events_url": f"{_FLOWS}/{flow_id}/events?cursor={encode_cursor(flow_id, 0)}",
    }


def _event_projection(value: object) -> dict[str, object]:
    event = cast("Any", value)
    reading, next_action = _reading(event.state.value, event.reason_code)
    return {
        "event_id": event.event_id,
        "predecessor_event_id": event.predecessor_event_id,
        "execution_id": event.execution_id,
        "predecessor_execution_id": event.predecessor_execution_id,
        "transition_sequence": event.transition_sequence,
        "stage": event.stage,
        "state": event.state.value,
        "progress": None,
        "authority": {
            "kind": event.authority.kind,
            "observed_at": event.authority.observed_at.isoformat(),
        },
        "reason_code": event.reason_code,
        "reading": reading,
        "next_action": next_action,
    }


def _dispatch(event: dict[str, Any]) -> dict[str, object] | None:
    if events.method(event) != "GET":
        raise OperationalStatusError("method is invalid")
    path = events.path(event)
    query = events.query(event)
    if path == _DEFINITIONS:
        if query:
            raise OperationalStatusError("definitions route has no query parameters")
        return definitions_document()
    if path == _FLOWS:
        return _summary(query)
    if not path.startswith(f"{_FLOWS}/"):
        raise OperationalStatusError("route is invalid")
    suffix = path.removeprefix(f"{_FLOWS}/")
    parts = suffix.split("/")
    flow_id = canonical_uuid(parts[0], "flow_id")
    if len(parts) == 1:
        if query:
            raise OperationalStatusError("flow route has no query parameters")
        return _flow(flow_id)
    if len(parts) == _EVENTS_ROUTE_PARTS and parts[1] == "events":
        if set(query) != {"cursor", "limit"}:
            raise OperationalStatusError("event cursor and limit are required")
        try:
            limit = int(query["limit"])
        except ValueError as error:
            raise OperationalStatusError("event limit is invalid") from error
        page = _STORE.events(flow_id, query["cursor"], limit)
        if page is None:
            return None
        return {
            "schema_version": "1",
            "flow_id": flow_id,
            "events": [_event_projection(item) for item in page.events],
            "next_cursor": page.next_cursor,
        }
    raise OperationalStatusError("route is invalid")


def _explanation_body(event: dict[str, Any]) -> object:
    raw = event.get("body") or ""
    if (
        not isinstance(raw, str)
        or event.get("isBase64Encoded")
        or len(raw.encode("utf-8")) > _MAX_EXPLANATION_BODY_BYTES
    ):
        raise ExplanationInvalidError(
            ExplanationRefusal.INVALID_REFERENCE, "the request body is missing or too large."
        )
    try:
        return cast("object", json.loads(raw))
    except ValueError:
        raise ExplanationInvalidError(
            ExplanationRefusal.INVALID_REFERENCE, "the request body is not JSON."
        ) from None


def _is_explanation(event: dict[str, Any]) -> bool:
    try:
        return events.method(event) == "POST" and events.path(event) == _EXPLANATIONS
    except IdentityError, KeyError, TypeError, ValueError:
        return False


def _report_explanation(error: BaseException) -> None:
    report_error(
        error,
        Report(operation=_OPERATION, correlation_id=current_correlation_id(), logger=_LOGGER),
    )


def _explanation(event: dict[str, Any], process_span: Any) -> dict[str, Any]:
    """Serve one explanation; every non-success return is reported exactly once, here."""
    try:
        try:
            entitled = events.authorizer_can_read_operational_status(event)
            events.authorizer_subject(event)
        except IdentityError:
            entitled = False
        if not entitled:
            raise ExplanationInvalidError(
                ExplanationRefusal.UNAUTHORIZED_COLLAPSED, "the explanation entitlement is absent."
            )
        request = parse_explanation_request(_explanation_body(event))
        document = explain(request, _EXPLANATION)
    except (ExplanationInvalidError, ExplanationUnavailableError) as error:
        _report_explanation(error)
        if error.refusal is ExplanationRefusal.UNAUTHORIZED_COLLAPSED:
            process_span.record(outcome="not_found", status=404)
            return _absence()
        status = _REFUSAL_STATUS[error.refusal]
        process_span.record(outcome=error.refusal.value, status=status)
        return _json(status, {"error": error.refusal.value})
    except CursorCapacityLimitedError as error:
        _report_explanation(error)
        process_span.record(outcome=error.signal, status=413)
        return _json(413, {"error": error.signal})
    except CursorInvalidError as error:
        _report_explanation(error)
        process_span.record(outcome=error.signal, status=400)
        return _json(400, {"error": error.signal})
    except Exception as error:
        process_span.note_failure(error)
        _report_explanation(error)
        process_span.record(outcome="failed", status=500)
        return _json(500, {"error": "unavailable"})
    complete = bool(document["complete"])
    process_span.record(outcome="completed" if complete else "incomplete", status=200)
    # A valid but incomplete tree is a typed non-success result, not an exception: one governed
    # terminal record names it, and the caller's CLI exits non-success.
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase="terminal",
            outcome="completed" if complete else "incomplete",
            severity=LogSeverity.INFO,
            details={
                "status": 200,
                "route": "execution_explanations",
                "missing_stage_count": len(
                    cast("list[object]", document["missing_required_stages"])
                ),
                "gap_count": len(cast("list[object]", document["gaps"])),
            },
        ),
        _LOGGER,
    )
    return _json(200, document)


@returns_correlation(_LOGGER)
@trace_boundary(OPERATIONAL_STATUS_REQUEST)
@flushes_traces
@flushes_metrics
@_LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, Any]:
    """Deny before projection access and collapse all absence classes to one response."""
    with span(OPERATIONAL_STATUS_REQUEST, operation=_OPERATION) as process_span:
        if _is_explanation(event):
            return _explanation(event, process_span)
        try:
            if not events.authorizer_can_read_operational_status(event):
                raise IdentityError("operational status entitlement is absent")
            events.authorizer_subject(event)
            document = _dispatch(event)
            if document is None:
                process_span.record(outcome="not_found", status=404)
                return _absence()
            process_span.record(outcome="completed", status=200)
            report_operation(
                OperationLog(
                    operation=_OPERATION,
                    component=_COMPONENT,
                    phase="terminal",
                    outcome="completed",
                    severity=LogSeverity.INFO,
                    details={"status": 200},
                ),
                _LOGGER,
            )
            return _json(200, document)
        except IdentityError, OperationalStatusError, ValueError, KeyError, TypeError:
            process_span.record(outcome="not_found", status=404)
            return _absence()
        except Exception as error:
            process_span.note_failure(error)
            report_error(
                error,
                Report(
                    operation=_OPERATION, correlation_id=current_correlation_id(), logger=_LOGGER
                ),
            )
            process_span.record(outcome="failed", status=500)
            return _json(500, {"error": "unavailable"})
