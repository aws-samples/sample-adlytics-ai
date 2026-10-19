# SPDX-License-Identifier: MIT-0
"""`POST /api/operations/run-requests` and `GET /api/operations/run-requests/{request_id}`.

Behind the existing session authorizer, which verifies the session and CSRF and admits these two
routes only for the `operations_run_request` level, projected from the
`operations-run-requesters` group (ADR-078 route entitlements). The function re-reads the
projected entitlement before any store access. Every API caller is `on_demand`; only such a
caller, in the group, may use `data_correction`.

A pre-domain refusal returns 400/403 with its signal and writes no receipt. A dependency
failure returns 503 and writes no receipt. Every domain decision returns 200 with its receipt.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Final, cast

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import BotoCoreError, ClientError

from adapters.config import run_request_api_settings, tracing_settings
from adapters.inbound.auth_handlers import events
from adapters.inbound.correlation import current_correlation_id
from adapters.inbound.run_requests.service import (
    AdmissionDependencies,
    LegacyLookupUnavailableError,
    RunRequest,
    admit_run_request,
)
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore
from adapters.outbound.dynamodb.stage1_writer_authority import Stage1WriterAuthorityReader
from adapters.outbound.legacy_head import S3CoordinatorLegacyHead
from adapters.outbound.s3.run_request_manifests import S3RunRequestManifests
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from domain.identity.session import IdentityError
from domain.operations.execution_graph import GraphCommandRefusedError
from domain.operations.ingestion_root import RootRefusedError
from domain.shared_kernel.errors import DependencyError

__all__ = ["handler"]

LOGGER: Final = Logger(service="adlytics-run-requests")
_OPERATION: Final = "run_requests.api"
_COMPONENT: Final = "run_requests.handler"
_SETTINGS: Final = run_request_api_settings()
_STORE: Final = DynamoExecutionGraphStore(_SETTINGS.execution_graph_table, boto3.client("dynamodb"))
_MANIFESTS: Final = S3RunRequestManifests(_SETTINGS.raw_bucket, boto3.client("s3"))


class _LazyLegacyPlans:
    """S6 entry item 4: rows 9 and 13's legacy authority in epoch 1, resolved on first use.

    The coordinator tables resolve through the exact SSM paths the DataV2 stack publishes for
    this environment, so `adapters/config.py` (pinned while the lagging stacks are accepted,
    owner-gate Item 14) gains no accessor. Epoch 0 never calls it, so SHADOW pays nothing.
    """

    def __init__(self) -> None:
        self._head: S3CoordinatorLegacyHead | None = None

    def _resolved(self) -> S3CoordinatorLegacyHead:
        if self._head is None:
            environment = tracing_settings().environment
            ssm = boto3.client("ssm")
            names = [
                str(
                    ssm.get_parameter(Name=f"/adlytics/{environment}/data/v2/{name}")[
                        "Parameter"
                    ].get("Value", "")
                )
                for name in ("semantic-publications-table", "semantic-leases-table")
            ]
            if not all(names):
                raise LegacyLookupUnavailableError
            dynamodb = boto3.client("dynamodb")
            self._head = S3CoordinatorLegacyHead(
                _SETTINGS.raw_bucket,
                boto3.client("s3"),
                Stage1WriterAuthorityReader(names[0], names[1], dynamodb),
            )
        return self._head

    def completed_plan_key(self, key: str, version_id: str) -> str | None:
        """Return the completed legacy plan's key, or None."""
        return self._resolved().completed_plan_key(key, version_id)

    def head_after(self, window: str, r: datetime) -> datetime | None:
        """Return the newest retrieval above `r` whose legacy plan completed."""
        return self._resolved().head_after(window, r)


_LEGACY: Final = _LazyLegacyPlans()
_MAX_BODY_BYTES: Final = 4096
_POST: Final = "POST /api/operations/run-requests"
_GET: Final = "GET /api/operations/run-requests/{request_id}"
_GROUP_REQUIRED: Final = "RUN_REQUESTER_GROUP_REQUIRED"

report_operation(
    OperationLog(
        operation=_OPERATION,
        component=_COMPONENT,
        phase="start",
        outcome="disabled",
        severity=LogSeverity.INFO,
        # S1 declares no span for this route: a new span changes the shared span module and
        # with it the deep-input revision every insight cache key carries. S2 adds the
        # FR-1/FR-2 span vocabulary together with that revision decision.
        details={"stage": "startup", "tracing": False},
    ),
    LOGGER,
)


def _response(status: int, body: dict[str, object]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, sort_keys=True, default=str),
    }


def can_request_runs(event: dict[str, Any]) -> bool:
    """Return only the verified run-request entitlement the authorizer projected."""
    context = cast("dict[str, Any]", event.get("requestContext") or {})
    authorizer = cast("dict[str, Any]", context.get("authorizer") or {})
    projected = cast("dict[str, Any]", authorizer.get("lambda") or {})
    return projected.get("can_request_runs") == "true"


def _body(event: dict[str, Any]) -> dict[str, str]:
    raw = event.get("body") or ""
    if (
        not isinstance(raw, str)
        or len(raw.encode()) > _MAX_BODY_BYTES
        or event.get("isBase64Encoded")
    ):
        raise RootRefusedError("SCHEMA_INVALID", "the request body is missing or too large.")
    parsed: object = json.loads(raw)
    if not isinstance(parsed, dict) or not all(
        isinstance(value, str) for value in cast("dict[str, object]", parsed).values()
    ):
        raise RootRefusedError("SCHEMA_INVALID", "the request body is not a flat string object.")
    return cast("dict[str, str]", parsed)


def _admit(event: dict[str, Any], subject: str) -> dict[str, Any]:
    body = _body(event)
    unknown = set(body) - {
        "request_id",
        "protocol_version",
        "reason",
        "manifest_key",
        "version_id",
        "label",
    }
    if unknown:
        raise RootRefusedError("SCHEMA_INVALID", "the request body names an unknown field.")
    request = RunRequest(
        request_id=body.get("request_id", ""),
        protocol_version=body.get("protocol_version", ""),
        caller_kind="on_demand",
        reason=body.get("reason", ""),
        manifest_key=body.get("manifest_key", ""),
        version_id=body.get("version_id", ""),
        requester=subject,
        requester_is_iam=False,
        via_api_group=True,
        label=body.get("label"),
    )
    receipt = admit_run_request(
        request,
        AdmissionDependencies(
            store=_STORE, manifests=_MANIFESTS, now=lambda: datetime.now(UTC), legacy=_LEGACY
        ),
    )
    return _response(200, {"receipt": asdict(receipt)})


def _show(event: dict[str, Any]) -> dict[str, Any]:
    parameters = cast("dict[str, str]", event.get("pathParameters") or {})
    found = _STORE.read_receipt(parameters.get("request_id", ""))
    if found is None:
        return _response(404, {"error": "not_found"})
    return _response(200, {"receipt": asdict(found)})


def _report(error: BaseException) -> None:
    report_error(
        error,
        Report(operation=_OPERATION, correlation_id=current_correlation_id(), logger=LOGGER),
    )


@flushes_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, Any]:
    """Serve one run-request route."""
    return _serve(event)


def _serve(event: dict[str, Any]) -> dict[str, Any]:
    """Serve one route; every non-success return is reported once."""
    route = events.route_key(event)
    try:
        subject = events.authorizer_subject(event)
        if not can_request_runs(event):
            raise GraphCommandRefusedError(_GROUP_REQUIRED, "the requester group is required.")
        if route == _POST:
            result = _admit(event, subject)
        elif route == _GET:
            result = _show(event)
        else:
            result = _response(404, {"error": "not_found"})
    except IdentityError as error:
        _report(error)
        return _response(403, {"refused": "IDENTITY_REQUIRED"})
    except (GraphCommandRefusedError, RootRefusedError) as error:
        _report(error)
        status = 403 if error.signal == _GROUP_REQUIRED else 400
        return _response(status, {"refused": error.signal})
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        _report(error)
        return _response(400, {"refused": "SCHEMA_INVALID"})
    except (DependencyError, BotoCoreError, ClientError) as error:
        _report(error)
        return _response(503, {"error": "dependency_unavailable"})
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase="terminal",
            outcome="completed",
            severity=LogSeverity.INFO,
            details={"route": route, "status": int(result["statusCode"])},
        ),
        LOGGER,
    )
    return result
