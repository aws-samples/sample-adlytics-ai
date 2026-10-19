# SPDX-License-Identifier: MIT-0
"""Direct FIFO transport for the closed operational-status queue union."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import boto3
from aws_lambda_powertools import Logger

from adapters.outbound.telemetry.reporting import Report, report_error
from domain.operations.generated_data_flows import REGISTRY_VERSION
from domain.operations.stage1_operational_status import (
    AthenaUsage,
    KnownAthenaUsage,
    OperationalDataFlowUsageContribution,
    OperationalDataFlowUsageEvidenceDisposition,
    SendStage1StatusMessageRequest,
    SettledUsageContributionAuthority,
    SettledUsageEvidenceDispositionAuthority,
    Stage1AuthorityMember,
    UsageCategory,
)
from domain.operations.status import (
    SCHEMA_VERSION,
    EvaluationBinding,
    Evidence,
    OperationalDataFlowEvent,
    OperationalStatusError,
    event_document,
    event_from_document,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.operational_status import (
    OperationalDataFlowSurfaceObservation,
    OperationalStatusMessage,
    OperationalStatusSink,
    SurfaceComputeObservation,
    SurfaceProviderLatencyObservation,
    SurfaceTransportObservation,
)
from ports.outbound.stage1_operational_status import Stage1OperationalStatusQueuePort

if TYPE_CHECKING:
    from types_boto3_sqs import SQSClient

_QUEUE_UNWRITABLE = ErrorCode("OPERATIONAL_STATUS", 1)
_MAX_BODY_BYTES = 256 * 1024
_LOGGER = Logger(service="adlytics-operational-status-sender")
_MEMBER_FIELDS = frozenset(
    {"event", "surface_observation", "usage_contribution", "usage_evidence_disposition"}
)


@dataclass(frozen=True, slots=True)
class _SendEnvelope:
    client: SQSClient
    queue_url: str
    message: OperationalStatusMessage
    group_id: str
    deduplication_id: str


class SqsOperationalStatusSink(OperationalStatusSink):
    """Send any exact v1 queue member to the dedicated encrypted FIFO queue."""

    def __init__(self, queue_url: str, client: SQSClient | None = None) -> None:
        if not queue_url.strip():
            raise ValueError("operational status queue URL is required")
        self._queue_url = queue_url
        self._client = client

    @property
    def _sqs(self) -> SQSClient:
        if self._client is None:
            self._client = boto3.client("sqs")
        return self._client

    def send(self, message: OperationalStatusMessage) -> None:
        """Use the member's contract-owned FIFO group and immutable deduplication ID."""
        group_id, deduplication_id = message_identity(message)
        _send(_SendEnvelope(self._sqs, self._queue_url, message, group_id, deduplication_id))


class SqsStage1OperationalStatusQueue(Stage1OperationalStatusQueuePort):
    """Adapt the Stage-1 delivery use case's derived FIFO request to the shared sender."""

    def __init__(self, queue_url: str, client: SQSClient | None = None) -> None:
        if not queue_url.strip():
            raise ValueError("operational status queue URL is required")
        self._queue_url = queue_url
        self._client = client

    @property
    def _sqs(self) -> SQSClient:
        if self._client is None:
            self._client = boto3.client("sqs")
        return self._client

    def send(self, request: SendStage1StatusMessageRequest) -> None:
        """Send only the exact identities derived by ``deliver_stage1_authority``."""
        message = _stage1_member(request.member)
        expected = message_identity(message)
        supplied = (request.message_group_id, request.message_deduplication_id)
        if supplied != expected:
            raise OperationalStatusError("Stage-1 FIFO identities differ from the queue member")
        _send(_SendEnvelope(self._sqs, self._queue_url, message, *supplied))


def _send(request: _SendEnvelope) -> None:
    body = serialize_message(request.message)
    if len(body.encode("utf-8")) > _MAX_BODY_BYTES:
        raise OperationalStatusError("operational status message exceeds queue capacity")
    try:
        request.client.send_message(
            QueueUrl=request.queue_url,
            MessageBody=body,
            MessageGroupId=request.group_id,
            MessageDeduplicationId=request.deduplication_id,
        )
    except Exception as error:
        failure = DependencyError(
            _QUEUE_UNWRITABLE,
            "Operational status evidence could not be durably admitted.",
            context={
                "cause": type(error).__name__,
                "message_group_id": request.group_id,
            },
        )
        report_error(
            failure,
            Report(operation="operational_status.send", correlation_id="", logger=_LOGGER),
        )
        raise failure from None


def message_identity(message: OperationalStatusMessage) -> tuple[str, str]:
    """Return the exact FIFO group and deduplication identities for one member."""
    if isinstance(message, OperationalDataFlowEvent):
        return message.flow_id, message.event_id
    if isinstance(message, OperationalDataFlowUsageContribution):
        return message.flow_id, message.contribution_id
    if isinstance(message, OperationalDataFlowUsageEvidenceDisposition):
        return message.flow_id, message.disposition_id
    return f"surface#{message.surface}", message.observation_id


def serialize_message(message: OperationalStatusMessage) -> str:
    """Encode one and only one tagged member using the canonical JSON parameters."""
    if isinstance(message, OperationalDataFlowEvent):
        member = "event"
        document = event_document(message)
    elif isinstance(message, OperationalDataFlowSurfaceObservation):
        member = "surface_observation"
        document = surface_observation_document(message)
    elif isinstance(message, OperationalDataFlowUsageContribution):
        member = "usage_contribution"
        document = usage_contribution_document(message)
    else:
        member = "usage_evidence_disposition"
        document = usage_evidence_disposition_document(message)
    return json.dumps(
        {"schema_version": SCHEMA_VERSION, member: document},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def parse_message(value: object) -> OperationalStatusMessage:
    """Parse the single active v1 four-member union without a compatibility branch."""
    if not isinstance(value, str):
        raise OperationalStatusError("operational status queue body must be text")
    try:
        decoded: object = json.loads(value)
    except json.JSONDecodeError as error:
        raise OperationalStatusError("operational status queue body is invalid") from error
    document = _mapping(decoded, "operational status queue body must be an object")
    members = frozenset(document) & _MEMBER_FIELDS
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or len(members) != 1
        or frozenset(document) != frozenset({"schema_version", *members})
    ):
        raise OperationalStatusError("operational status queue fields differ from v1")
    member = next(iter(members))
    if member == "event":
        return event_from_document(document[member])
    if member == "surface_observation":
        return surface_observation_from_document(document[member])
    if member == "usage_contribution":
        return usage_contribution_from_document(document[member])
    return usage_evidence_disposition_from_document(document[member])


def surface_observation_document(
    observation: OperationalDataFlowSurfaceObservation,
) -> dict[str, object]:
    """Return the exact public surface-observation member."""
    return {
        "schema_version": observation.schema_version,
        "registry_version": observation.registry_version,
        "observation_id": observation.observation_id,
        "surface": observation.surface,
        "observed_at": _iso(observation.observed_at),
        "source_sequence": observation.source_sequence,
        "transport": _optional_counts(observation.transport),
        "compute": _optional_counts(observation.compute),
        "provider_latency": _optional_counts(observation.provider_latency),
        "evidence": observation.evidence.value,
        "reason_code": observation.reason_code,
        "open_operational_case_count": observation.open_operational_case_count,
    }


def surface_observation_from_document(value: object) -> OperationalDataFlowSurfaceObservation:
    """Reject unknown or incomplete surface-observation fields."""
    document = _exact(
        value,
        {
            "schema_version",
            "registry_version",
            "observation_id",
            "surface",
            "observed_at",
            "source_sequence",
            "transport",
            "compute",
            "provider_latency",
            "evidence",
            "reason_code",
            "open_operational_case_count",
        },
        "surface observation",
    )
    return OperationalDataFlowSurfaceObservation(
        schema_version=_text(document["schema_version"], "surface observation schema version"),
        registry_version=_text(
            document["registry_version"], "surface observation registry version"
        ),
        observation_id=_text(document["observation_id"], "observation id"),
        surface=_text(document["surface"], "surface"),
        observed_at=_moment(document["observed_at"], "observed_at"),
        source_sequence=_integer(document["source_sequence"], "source sequence"),
        transport=_transport_counts(document["transport"]),
        compute=_compute_counts(document["compute"]),
        provider_latency=_latency_counts(document["provider_latency"]),
        evidence=Evidence(_text(document["evidence"], "evidence")),
        reason_code=_optional_text(document["reason_code"], "reason code"),
        open_operational_case_count=_integer(
            document["open_operational_case_count"], "open operational case count"
        ),
    )


def usage_contribution_document(
    contribution: OperationalDataFlowUsageContribution,
) -> dict[str, object]:
    """Return the exact ordinary usage-contribution member."""
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_version": REGISTRY_VERSION,
        "contribution_id": contribution.contribution_id,
        "flow_id": contribution.flow_id,
        "execution_id": contribution.execution_id,
        "evaluation_binding": _binding_document(contribution.evaluation_binding),
        "reconciles_disposition_id": contribution.reconciles_disposition_id,
        "usage": {
            contribution.category.value: {
                contribution.resource: {
                    "query_count": contribution.usage.query_count,
                    "scanned_bytes": contribution.usage.scanned_bytes,
                    "engine_ms": contribution.usage.engine_ms,
                }
            }
        },
        "observed_at": _iso(contribution.observed_at),
    }


def usage_contribution_from_document(value: object) -> OperationalDataFlowUsageContribution:
    """Parse one ordinary Athena contribution and reject deltas or extra categories."""
    document = _exact(
        value,
        {
            "schema_version",
            "registry_version",
            "contribution_id",
            "flow_id",
            "execution_id",
            "evaluation_binding",
            "reconciles_disposition_id",
            "usage",
            "observed_at",
        },
        "usage contribution",
    )
    _versions(document, "usage contribution")
    if document["reconciles_disposition_id"] is not None:
        raise OperationalStatusError("Stage-1 reconciliation deltas are forbidden")
    usage_by_category = _mapping(document["usage"], "usage must be an object")
    if len(usage_by_category) != 1:
        raise OperationalStatusError("usage must populate exactly one category")
    category_text, resource_value = next(iter(usage_by_category.items()))
    resource = _exact(resource_value, {"athena"}, "usage resource")
    units = _exact(
        resource["athena"],
        {"query_count", "scanned_bytes", "engine_ms"},
        "Athena usage",
    )
    return OperationalDataFlowUsageContribution(
        contribution_id=_text(document["contribution_id"], "contribution id"),
        flow_id=_text(document["flow_id"], "flow id"),
        execution_id=_text(document["execution_id"], "execution id"),
        evaluation_binding=_binding(document["evaluation_binding"]),
        category=UsageCategory(category_text),
        usage=AthenaUsage(
            _integer(units["query_count"], "query count"),
            _integer(units["scanned_bytes"], "scanned bytes"),
            _integer(units["engine_ms"], "engine milliseconds"),
        ),
        observed_at=_moment(document["observed_at"], "observed_at"),
    )


def usage_evidence_disposition_document(
    disposition: OperationalDataFlowUsageEvidenceDisposition,
) -> dict[str, object]:
    """Return the exact final unavailable-evidence member."""
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_version": REGISTRY_VERSION,
        "disposition_id": disposition.disposition_id,
        "flow_id": disposition.flow_id,
        "execution_id": disposition.execution_id,
        "evaluation_binding": _binding_document(disposition.evaluation_binding),
        "category": disposition.category.value,
        "resource": disposition.resource,
        "known_usage": {
            disposition.resource: {
                "query_count": disposition.known_usage.query_count,
                "scanned_bytes": disposition.known_usage.scanned_bytes,
                "engine_ms": disposition.known_usage.engine_ms,
            }
        },
        "missing_quantities": list(disposition.missing_quantities),
        "evidence": disposition.evidence.value,
        "observed_at": _iso(disposition.observed_at),
    }


def usage_evidence_disposition_from_document(
    value: object,
) -> OperationalDataFlowUsageEvidenceDisposition:
    """Parse one permanent final Athena evidence disposition."""
    document = _exact(
        value,
        {
            "schema_version",
            "registry_version",
            "disposition_id",
            "flow_id",
            "execution_id",
            "evaluation_binding",
            "category",
            "resource",
            "known_usage",
            "missing_quantities",
            "evidence",
            "observed_at",
        },
        "usage evidence disposition",
    )
    _versions(document, "usage evidence disposition")
    if document["resource"] != "athena":
        raise OperationalStatusError("Stage-1 usage authority must be Athena")
    known = _exact(document["known_usage"], {"athena"}, "known usage")
    units = _exact(
        known["athena"],
        {"query_count", "scanned_bytes", "engine_ms"},
        "known Athena usage",
    )
    missing_raw = document["missing_quantities"]
    if not isinstance(missing_raw, list):
        raise OperationalStatusError("missing quantities must be a text array")
    missing_values = cast("list[object]", missing_raw)
    if any(not isinstance(item, str) for item in missing_values):
        raise OperationalStatusError("missing quantities must be a text array")
    return OperationalDataFlowUsageEvidenceDisposition(
        disposition_id=_text(document["disposition_id"], "disposition id"),
        flow_id=_text(document["flow_id"], "flow id"),
        execution_id=_text(document["execution_id"], "execution id"),
        evaluation_binding=_binding(document["evaluation_binding"]),
        category=UsageCategory(_text(document["category"], "category")),
        resource="athena",
        known_usage=KnownAthenaUsage(
            _integer(units["query_count"], "query count"),
            _optional_integer(units["scanned_bytes"], "scanned bytes"),
            _optional_integer(units["engine_ms"], "engine milliseconds"),
        ),
        missing_quantities=tuple(cast("list[str]", missing_values)),
        evidence=Evidence(_text(document["evidence"], "evidence")),
        observed_at=_moment(document["observed_at"], "observed_at"),
    )


def _stage1_member(member: Stage1AuthorityMember) -> OperationalStatusMessage:
    if isinstance(member, SettledUsageContributionAuthority):
        return member.contribution
    if isinstance(member, SettledUsageEvidenceDispositionAuthority):
        return member.disposition
    return member.event


def _binding_document(binding: EvaluationBinding) -> dict[str, object]:
    return {
        "mode": binding.mode,
        "measurement_refs": [
            {"measurement_ref": reference, "measurement_version": version}
            for reference, version in binding.measurement_refs
        ],
    }


def _binding(value: object) -> EvaluationBinding:
    document = _exact(value, {"mode", "measurement_refs"}, "evaluation binding")
    raw_references = document["measurement_refs"]
    if not isinstance(raw_references, list):
        raise OperationalStatusError("measurement references must be an array")
    references: list[tuple[str, str]] = []
    for raw in cast("list[object]", raw_references):
        reference = _exact(raw, {"measurement_ref", "measurement_version"}, "measurement reference")
        references.append(
            (
                _text(reference["measurement_ref"], "measurement reference"),
                _text(reference["measurement_version"], "measurement version"),
            )
        )
    return EvaluationBinding(tuple(references), _text(document["mode"], "evaluation mode"))


def _optional_counts(
    value: SurfaceTransportObservation
    | SurfaceComputeObservation
    | SurfaceProviderLatencyObservation
    | None,
) -> dict[str, int] | None:
    if value is None:
        return None
    return {
        descriptor.name: cast("int", getattr(value, descriptor.name))
        for descriptor in fields(value)
    }


def _transport_counts(value: object) -> SurfaceTransportObservation | None:
    if value is None:
        return None
    document = _exact(
        value,
        {"visible", "in_flight", "delayed", "oldest_age_ms", "retrying", "dlq"},
        "transport counts",
    )
    return SurfaceTransportObservation(
        **{name: _integer(document[name], name) for name in document}
    )


def _compute_counts(value: object) -> SurfaceComputeObservation | None:
    if value is None:
        return None
    document = _exact(value, {"running", "errors", "throttles"}, "compute counts")
    return SurfaceComputeObservation(**{name: _integer(document[name], name) for name in document})


def _latency_counts(value: object) -> SurfaceProviderLatencyObservation | None:
    if value is None:
        return None
    document = _exact(value, {"count", "total_ms", "max_ms"}, "provider latency")
    return SurfaceProviderLatencyObservation(
        **{name: _integer(document[name], name) for name in document}
    )


def _versions(document: dict[str, object], name: str) -> None:
    if (
        document["schema_version"] != SCHEMA_VERSION
        or document["registry_version"] != REGISTRY_VERSION
    ):
        raise OperationalStatusError(f"{name} contract version is not active")


def _mapping(value: object, message: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OperationalStatusError(message)
    untyped = cast("dict[object, object]", value)
    if any(not isinstance(key, str) for key in untyped):
        raise OperationalStatusError(message)
    return cast("dict[str, object]", untyped)


def _exact(value: object, names: set[str], label: str) -> dict[str, object]:
    document = _mapping(value, f"{label} must be an object")
    if frozenset(document) != frozenset(names):
        raise OperationalStatusError(f"{label} fields differ from the closed v1 contract")
    return document


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OperationalStatusError(f"{name} must be nonblank text")
    return value


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OperationalStatusError(f"{name} must be a nonnegative integer")
    return value


def _optional_integer(value: object, name: str) -> int | None:
    return None if value is None else _integer(value, name)


def _moment(value: object, name: str) -> datetime:
    try:
        moment = datetime.fromisoformat(_text(value, name))
    except ValueError as error:
        raise OperationalStatusError(f"{name} must be an ISO instant") from error
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise OperationalStatusError(f"{name} must be timezone-aware")
    return moment.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()
