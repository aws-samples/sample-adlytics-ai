# SPDX-License-Identifier: MIT-0
"""Finalize one exact cross-account registration into the canonical manifest boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, cast

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.metrics import MetricUnit, single_metric

from adapters.amc_csv import AmcCsvEscapeError
from adapters.config import settings_without_client_secret, tracing_settings
from adapters.graph_authority import provisioned_graph_table
from adapters.inbound.ingestion_handlers.ingress import DeliveryIngress, record_schedule
from adapters.inbound.run_requests.service import AdmissionDependencies, admit_run_request
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore
from adapters.outbound.dynamodb.producer_status import DynamoRegistrationStatus
from adapters.outbound.dynamodb.stage1_writer_authority import Stage1WriterAuthorityReader
from adapters.outbound.legacy_head import S3CoordinatorLegacyHead
from adapters.outbound.s3.producer_landing import S3ProducerLanding
from adapters.outbound.s3.run_request_manifests import S3RunRequestManifests
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
    PRODUCER_REGISTRATION_FINALIZE,
    span,
    trace_boundary,
)
from adapters.producer.landing import write_governed_manifest
from adapters.producer.packs import load_query_packs
from adapters.producer.registrations import registration_from_json
from adapters.producer.results import parse_csv_result
from domain.producer.models import MAX_REGISTRATION_BYTES, GovernedQueryPack
from domain.producer.planning import (
    PlanManifestRequest,
    canonical_result_key,
    plan_manifest,
)
from domain.producer.status import (
    FinalizedRegistration,
    RegistrationAttempt,
    RegistrationRejection,
    RegistrationState,
    RegistrationStatus,
)
from domain.producer.validation import (
    ConformanceReport,
    validate_registration,
    validate_result,
    validate_result_window,
)
from domain.semantics.commerce_identity import CommerceIdentityBridge
from domain.shared_kernel.errors import (
    AdlyticsError,
    ErrorCode,
    InvariantError,
    ValidationError,
)
from ports.outbound.producer_landing import ProducerLandingPort, RegistrationStatusPort

LOGGER = Logger(service="adlytics-producer-finalizer")
METRIC_NAME = "ProducerRegistrations"
METRICS_NAMESPACE = "Adlytics"
REGISTRATION_SUFFIX = "/registration.json"
INCOMING_PREFIX = "incoming/"
OPERATION = "finalize_registration"
PRODUCER_INVALID = ErrorCode("PRODUCER", 1)
PRODUCER_STATE_CONFLICT = ErrorCode("PRODUCER", 2)

SETTINGS = settings_without_client_secret()
TRACING = configure_tracing(tracing_settings())
ROOT = contracts_root()
PACKS = {pack.pack_id: pack for pack in load_query_packs(ROOT)}
COMMERCE_IDENTITIES = load_commerce_identity_bridge(ROOT)


@dataclass(frozen=True, slots=True)
class FinalizerMessage:
    """Exact S3 registration version selected by one internal queue message."""

    bucket: str
    key: str
    version_id: str

    def __post_init__(self) -> None:
        if not self.bucket.strip():
            raise ValueError("registration event requires a bucket name.")
        if not self.key.startswith(INCOMING_PREFIX) or not self.key.endswith(REGISTRATION_SUFFIX):
            raise ValueError("registration event key must match incoming/*/registration.json.")
        if not self.version_id.strip():
            raise ValueError("registration event requires an exact VersionId.")

    @property
    def attempt(self) -> RegistrationAttempt:
        """Project the immutable event identity onto operational status identity."""
        return RegistrationAttempt(self.key, self.version_id)


@dataclass(frozen=True, slots=True)
class FinalizerRuntime:
    """Least-privilege dependencies for registration finalization."""

    bucket: str
    packs: dict[str, GovernedQueryPack]
    landing: ProducerLandingPort
    statuses: RegistrationStatusPort
    commerce_identities: CommerceIdentityBridge
    """The governed bridge a pack's `inputParameters` are re-resolved from (ADR-110 point 2)."""
    requests: DeliveryIngress | None = None
    """S6 (V-19): the `schedule` requester; None where the graph authority is not provisioned."""


class RegistrationContentError(ValueError):
    """A complete registration attempt was terminally invalid rather than retryable."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _runtime() -> FinalizerRuntime:
    missing = [
        label
        for label, value in (
            ("raw bucket", SETTINGS.raw_bucket),
            ("registration status table", SETTINGS.registration_status_table),
        )
        if not value.strip()
    ]
    if missing:
        raise ValueError("Producer Finalizer configuration is incomplete: " + ", ".join(missing))
    return FinalizerRuntime(
        bucket=SETTINGS.raw_bucket,
        packs=PACKS,
        landing=S3ProducerLanding(SETTINGS.raw_bucket),
        statuses=DynamoRegistrationStatus(SETTINGS.registration_status_table),
        requests=_schedule_requester(),
        commerce_identities=COMMERCE_IDENTITIES,
    )


def _schedule_requester() -> DeliveryIngress | None:
    """Compose the `schedule` requester where the graph authority is provisioned (S6)."""
    table = provisioned_graph_table()
    if table is None:
        return None
    dynamodb = boto3.client("dynamodb")
    store = DynamoExecutionGraphStore(table, dynamodb)

    @lru_cache(maxsize=1)
    def deps() -> AdmissionDependencies:
        # Composed on the first request, so an import never needs the coordinator tables.
        s3 = boto3.client("s3")
        reader = Stage1WriterAuthorityReader(
            SETTINGS.semantic_publications_table, SETTINGS.semantic_leases_table, dynamodb
        )
        return AdmissionDependencies(
            store=store,
            manifests=S3RunRequestManifests(SETTINGS.raw_bucket, s3),
            now=lambda: datetime.now(UTC),
            legacy=S3CoordinatorLegacyHead(SETTINGS.raw_bucket, s3, reader),
        )

    return DeliveryIngress(
        mode=store.read_mode,
        admit=lambda request: admit_run_request(request, deps()),
        visibility=_no_visibility,
        requester=lambda: str(boto3.client("sts").get_caller_identity()["Arn"]),
        logger=LOGGER,
    )


def _no_visibility(_queue_url: str, _receipt_handle: str, _seconds: int) -> None:
    """Refuse: the finalizer never waits a message (the drain only skips its admission)."""
    raise InvariantError(PRODUCER_STATE_CONFLICT, "the finalizer changes no message visibility.")


RUNTIME = _runtime()


def _mapping(value: object, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(message)
    return cast("dict[str, Any]", value)


def _text(value: object, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    return value


def registration_messages(event: dict[str, Any]) -> tuple[FinalizerMessage, ...]:
    """Parse internal SQS/EventBridge envelopes and fail closed on missing version evidence."""
    records_value = event.get("Records", [])
    if not isinstance(records_value, list):
        raise ValueError("A Finalizer event requires a Records list.")
    found: list[FinalizerMessage] = []
    for value in cast("list[object]", records_value):
        record = _mapping(value, "A Finalizer record must be an object.")
        envelope = _mapping(
            json.loads(_text(record.get("body"), "A Finalizer record body must be JSON text.")),
            "A Finalizer record body must contain an EventBridge object.",
        )
        detail = _mapping(envelope.get("detail"), "A registration event carried no S3 detail.")
        bucket = _mapping(detail.get("bucket"), "A registration event carried no bucket.")
        object_detail = _mapping(detail.get("object"), "A registration event carried no object.")
        found.append(
            FinalizerMessage(
                bucket=_text(bucket.get("name"), "A registration event needs a bucket name."),
                key=_text(object_detail.get("key"), "A registration event needs an object key."),
                version_id=_text(
                    object_detail.get("version-id"),
                    "A registration event needs an object VersionId.",
                ),
            )
        )
    return tuple(found)


def _report(action: str, message: FinalizerMessage) -> None:
    with single_metric(
        name=METRIC_NAME,
        unit=MetricUnit.Count,
        value=1,
        namespace=METRICS_NAMESPACE,
    ) as metric:
        metric.add_dimension(name="Action", value=action)
    report_operation(
        OperationLog(
            operation=OPERATION,
            component="producer_finalizer",
            phase="terminal",
            outcome=action,
            severity=LogSeverity.INFO,
            description="Producer registration reached a terminal or replay state.",
            details={"attempt_id": message.attempt.attempt_id},
        ),
        LOGGER,
    )


def _finding(report: ConformanceReport) -> None:
    if report.valid:
        return
    detail = "; ".join(f"{item.code}: {item.detail}" for item in report.findings)
    raise RegistrationContentError(report.findings[0].code, detail)


def _record(statuses: RegistrationStatusPort, status: RegistrationStatus) -> RegistrationStatus:
    outcome = statuses.record(status)
    if outcome.accepted:
        return outcome.status
    if outcome.status.state in {RegistrationState.FINALIZED, RegistrationState.REJECTED}:
        return outcome.status
    raise InvariantError(
        PRODUCER_STATE_CONFLICT,
        "Registration finalization state refused its own exact transition.",
        context={"attempt_id": status.attempt.attempt_id, "reason": outcome.reason},
    )


@dataclass(frozen=True, slots=True)
class _RejectRequest:
    runtime: FinalizerRuntime
    message: FinalizerMessage
    error: RegistrationContentError
    now: datetime
    report: Report


def _reject(request: _RejectRequest) -> ValidationError:
    runtime = request.runtime
    message = request.message
    error = request.error
    detail = error.detail[:1000]
    _record(
        runtime.statuses,
        RegistrationStatus.rejected(
            message.attempt,
            RegistrationRejection(error.code, detail),
            at=request.now,
        ),
    )
    classified = ValidationError(
        PRODUCER_INVALID,
        "The source registration did not conform.",
        context={"attempt_id": message.attempt.attempt_id, "code": error.code, "detail": detail},
    )
    report_error(classified, request.report)
    _report("rejected", message)
    return classified


def finalize_registration(
    runtime: FinalizerRuntime,
    message: FinalizerMessage,
    now: datetime,
) -> RegistrationStatus:
    """Finalize exact bytes, or record a terminal content refusal without raising."""
    if message.bucket != runtime.bucket:
        raise ValueError("A registration event names a bucket outside this Finalizer runtime.")

    existing = runtime.statuses.get(message.attempt)
    if existing is not None and existing.state is not RegistrationState.PROCESSING:
        return existing
    if existing is None:
        _record(runtime.statuses, RegistrationStatus.processing(message.attempt, at=now))

    registration_size = runtime.landing.size(message.key, message.version_id)
    if registration_size > MAX_REGISTRATION_BYTES:
        raise RegistrationContentError(
            "REGISTRATION_TOO_LARGE",
            f"registration is {registration_size} bytes; maximum is {MAX_REGISTRATION_BYTES}.",
        )
    registration_object = runtime.landing.body(message.key, message.version_id)
    try:
        registration = registration_from_json(registration_object.body.decode("utf-8"))
        if registration.registration_key != message.key:
            raise RegistrationContentError(
                "REGISTRATION_KEY_MISMATCH",
                "registration identity does not reproduce the event-selected incoming key.",
            )
        installed = runtime.packs.get(registration.query_pack.pack_id)
        if installed is None:
            raise RegistrationContentError(
                "REGISTRATION_PACK_UNKNOWN",
                "registration names a query pack that is not installed.",
            )
        # The binding for the registration's version: the current pack or one of its declared
        # variants (plan D9). Any other version is checked against the current pack and refused
        # by its identity finding.
        pack = installed.binding_for(registration.query_pack.version) or installed
        _finding(validate_registration(pack, registration, runtime.commerce_identities))
        result_size = runtime.landing.size(
            registration.object.key,
            registration.object.version_id,
        )
        if result_size > pack.max_result_bytes:
            raise RegistrationContentError(
                "RESULT_TOO_LARGE",
                f"result is {result_size} bytes; maximum is {pack.max_result_bytes}.",
            )
        result_object = runtime.landing.body(
            registration.object.key,
            registration.object.version_id,
        )
        summary, evidence = parse_csv_result(result_object.body)
        _finding(validate_result(pack, summary))
        _finding(validate_result_window(registration, summary))
    except RegistrationContentError as error:
        raise error
    except AmcCsvEscapeError as error:
        raise RegistrationContentError(error.finding.code, str(error)) from error
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise RegistrationContentError("REGISTRATION_INVALID", str(error)) from error

    result_key = canonical_result_key(registration)
    stored_result = runtime.landing.put(result_key, result_object.body, "text/csv")
    manifest_plan = plan_manifest(
        PlanManifestRequest(
            registration=registration,
            pack=pack,
            evidence=evidence,
            destination_version_id=stored_result.version_id,
        )
    )
    manifest_body = write_governed_manifest(manifest_plan).encode()
    stored_manifest = runtime.landing.put(
        manifest_plan.manifest.manifest_key,
        manifest_body,
        "application/json",
    )
    return _record(
        runtime.statuses,
        RegistrationStatus.finalized(
            message.attempt,
            FinalizedRegistration(
                result_key=stored_result.key,
                result_version_id=stored_result.version_id,
                manifest_key=stored_manifest.key,
                manifest_version_id=stored_manifest.version_id,
            ),
            at=now,
        ),
    )


@trace_boundary(PRODUCER_REGISTRATION_FINALIZE)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: object) -> None:
    """Process one-message batches; retry dependencies and acknowledge decided rejections."""
    del context
    with span(
        PRODUCER_REGISTRATION_FINALIZE,
        operation=OPERATION,
    ) as process_span:
        boundary_report = Report(
            operation=OPERATION,
            correlation_id="",
            logger=LOGGER,
        )
        try:
            messages = registration_messages(event)
        except Exception as error:
            report_error(error, boundary_report)
            raise
        process_span.record(record_count=len(messages))
        refused = False
        for message in messages:
            report = Report(
                operation=OPERATION,
                correlation_id=message.attempt.attempt_id,
                logger=LOGGER,
            )
            now = datetime.now(tz=UTC)
            try:
                status = finalize_registration(RUNTIME, message, now)
            except RegistrationContentError as error:
                try:
                    classified = _reject(
                        _RejectRequest(
                            runtime=RUNTIME,
                            message=message,
                            error=error,
                            now=now,
                            report=report,
                        )
                    )
                except Exception as rejection_error:
                    report_error(rejection_error, report)
                    raise
                process_span.note_error(classified)
                refused = True
                continue
            except AdlyticsError as error:
                report_error(error, report)
                raise
            except Exception as error:
                report_error(error, report)
                raise

            finalized = status.output
            if RUNTIME.requests is not None and finalized is not None:
                # S6 (V-19): after the manifest is stored, the mode decides the request.
                try:
                    record_schedule(
                        RUNTIME.requests,
                        (finalized.manifest_key, finalized.manifest_version_id),
                        message.attempt.attempt_id,
                    )
                except Exception as error:
                    report_error(error, report)
                    raise
            _report(status.state.value, message)
            refused = refused or status.state is RegistrationState.REJECTED
        process_span.record(outcome="refused" if refused else "completed")
