# SPDX-License-Identifier: MIT-0
"""Runtime configuration, resolved and validated in one place.

Every deployment-derived value arrives as an environment variable set by CDK from
SSM parameters in ADR-012. Nothing here has a default, because a default is how
a deploy silently points at the wrong host and still looks healthy.

Validation happens when a handler imports this and calls `settings()`, not on first
use. A container that cannot resolve its configuration must never reach a healthy
state and start taking traffic.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING, NoReturn

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from adapters.inbound.auth_handlers.routing import API_PREFIX
from domain.conversation.audit import MAX_RECORD_RETENTION_DAYS
from domain.identity.cookies import CookiePolicy, CookieScope
from domain.identity.session import IdentityError
from domain.shared_kernel.errors import ErrorCode, InvariantError

if TYPE_CHECKING:
    from types_boto3_secretsmanager.client import SecretsManagerClient
ENV_PREFIX = "ADL_"

CONFIG_UNRESOLVED = ErrorCode("CONFIG", 1)

MAX_RESULT_REUSE_MINUTES = 7 * 24 * 60
"""Athena's own ceiling on result-reuse age: seven days."""


@dataclass(frozen=True, slots=True)
class EvaluationWriterSettings:
    """Configuration used only by the asynchronous evaluation trace writer."""

    input_log_group: str
    service_name: str
    writer_receipts_table: str
    receipt_retention_days: int


@dataclass(frozen=True, slots=True)
class EvaluationOutboxDispatcherSettings:
    """Exact resources and retention used only by the scheduled outbox dispatcher."""

    outbox_table: str
    deliveries_table: str
    queue_url: str
    delivered_retention_days: int
    receipt_retention_days: int
    release_parameters: tuple[str, ...]
    """Each family's release record name, read once per run before any claim (ADR-079)."""
    audit_table: str
    """The receipts table, read only for each turn's ADR-063 delivery companion."""
    delivery_wait_seconds: int
    """How long after admission an absent delivery companion is waited for."""


@dataclass(frozen=True, slots=True)
class AgentQualityProcessorSettings:
    """Deployment identities consumed only by the Agent Quality stream processor."""

    audit_table: str
    findings_table: str
    aggregate_table: str
    receipt_retention_days: int


@dataclass(frozen=True, slots=True)
class EvaluationResultIngressSettings:
    """Deployment identities consumed only by protected CloudWatch subscription ingress."""

    audit_table: str
    writer_receipts_table: str
    deliveries_table: str
    projection_queue_url: str
    result_log_groups: frozenset[str]


@dataclass(frozen=True, slots=True)
class EvaluationResultPersisterSettings:
    """Deployment identity consumed only by safe result-projection persistence."""

    verdicts_table: str
    receipt_retention_days: int


@dataclass(frozen=True, slots=True)
class InsightPrecomputeSettings:
    """Dedicated bounded worker configuration without request-serving identity settings."""

    semantic_publications_table: str
    semantic_v2_bucket: str
    preset_artifact_bucket: str
    insight_cache_table: str
    insight_prompt_registry_table: str
    insight_quick_model_id: str
    insight_deep_model_id: str
    insight_generation_timeout_seconds: int
    insight_generation_max_concurrency: int
    insight_precompute_max_requests: int
    athena_workgroup: str
    semantic_v2_current_database: str
    athena_result_reuse_max_age_minutes: int
    athena_query_batch_max_concurrency: int


@dataclass(frozen=True, slots=True)
class InsightContinuationWorkerSettings:
    """Dedicated exact cache-fill worker configuration without request-serving identity settings."""

    insight_cache_table: str
    insight_prompt_registry_table: str
    insight_quick_model_id: str
    insight_deep_model_id: str
    insight_generation_timeout_seconds: int
    insight_continuation_max_receive_count: int


@dataclass(frozen=True, slots=True)
class InsightPromptCalibrationWorkerSettings:
    """Queue worker resources and bounded lease/transport configuration."""

    insight_prompt_registry_table: str
    insight_prompt_calibration_queue_url: str
    insight_quick_model_id: str
    insight_deep_model_id: str
    insight_generation_timeout_seconds: int
    insight_prompt_calibration_lease_seconds: int


@dataclass(frozen=True, slots=True)
class PromptRegistrySeedSettings:
    """Retained registry identity consumed only by the deployment seed custom resource."""

    prompt_registry_table: str


@dataclass(frozen=True, slots=True)
class PresetArtifactEnqueueSettings:
    """Stream-admission resources consumed only by the preset-artifact enqueue worker."""

    semantic_publications_table: str
    bake_queue_url: str


@dataclass(frozen=True, slots=True)
class PresetArtifactBakeSettings:
    """Immutable persistence targets and retained-truth sources for the preset-artifact baker.

    Persistence targets are mandatory. The campaign-source fields mirror the insight
    precompute worker; when any is absent the baker composes fail-closed with no campaign
    source rather than measuring against a partially configured runtime.
    """

    artifact_bucket: str
    pointer_table: str
    semantic_publications_table: str = ""
    semantic_v2_bucket: str = ""
    athena_workgroup: str = ""
    semantic_v2_current_database: str = ""
    athena_result_reuse_max_age_minutes: int = 0
    athena_query_batch_max_concurrency: int = 0
    # The optional §5.3 insight-fill child (ADR-074): when either target is absent the
    # baker composes without the child and serve-time first-fill remains the fallback.
    insight_continuation_queue_url: str = ""
    insight_prompt_registry_table: str = ""


@dataclass(frozen=True, slots=True)
class PresetArtifactBackfillSettings:
    """The declared FIFO target consumed only by the backfill custom resource."""

    bake_queue_url: str


@dataclass(frozen=True, slots=True)
class EvaluationOperatorCredentials:
    """Browser credentials used only by the explicit local evaluation operator."""

    session: SecretStr
    refresh: SecretStr
    csrf: SecretStr


def evaluation_operator_credentials(
    source: Mapping[str, str] | None = None,
) -> EvaluationOperatorCredentials:
    """Resolve operator credentials without exposing them through command arguments."""
    values = source if source is not None else os.environ
    session = values.get(f"{ENV_PREFIX}SESSION", "").strip()
    refresh = values.get(f"{ENV_PREFIX}REFRESH", "").strip()
    csrf = values.get(f"{ENV_PREFIX}CSRF", "").strip()
    missing: list[str] = []
    if not session and not refresh:
        missing.append("session_or_refresh")
    if refresh and not csrf:
        missing.append("csrf")
    if missing:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Evaluation operator credentials are incomplete.",
            context={"missing": missing},
        )
    return EvaluationOperatorCredentials(
        session=SecretStr(session),
        refresh=SecretStr(refresh),
        csrf=SecretStr(csrf),
    )


@lru_cache(maxsize=1)
def insight_precompute_settings() -> InsightPrecomputeSettings:
    """Resolve the bounded worker's exact read/generate resources at initialization."""
    table = os.environ.get(f"{ENV_PREFIX}SEMANTIC_PUBLICATIONS_TABLE", "").strip()
    semantic_v2_bucket = os.environ.get(f"{ENV_PREFIX}SEMANTIC_V2_BUCKET", "").strip()
    preset_artifact_bucket = os.environ.get(f"{ENV_PREFIX}PRESET_ARTIFACT_BUCKET", "").strip()
    cache = os.environ.get(f"{ENV_PREFIX}INSIGHT_CACHE_TABLE", "").strip()
    registry = os.environ.get(f"{ENV_PREFIX}INSIGHT_PROMPT_REGISTRY_TABLE", "").strip()
    quick_model = os.environ.get(f"{ENV_PREFIX}INSIGHT_QUICK_MODEL_ID", "").strip()
    deep_model = os.environ.get(f"{ENV_PREFIX}INSIGHT_DEEP_MODEL_ID", "").strip()
    raw_timeout = os.environ.get(f"{ENV_PREFIX}INSIGHT_GENERATION_TIMEOUT_SECONDS", "").strip()
    raw_concurrency = os.environ.get(f"{ENV_PREFIX}INSIGHT_GENERATION_MAX_CONCURRENCY", "").strip()
    raw_requests = os.environ.get(f"{ENV_PREFIX}INSIGHT_PRECOMPUTE_MAX_REQUESTS", "").strip()
    workgroup = os.environ.get(
        f"{ENV_PREFIX}SEMANTIC_V2_ATHENA_INSIGHT_PRECOMPUTE_WORKGROUP", ""
    ).strip()
    database = os.environ.get(f"{ENV_PREFIX}SEMANTIC_V2_CURRENT_DATABASE", "").strip()
    raw_reuse = os.environ.get(f"{ENV_PREFIX}ATHENA_RESULT_REUSE_MAX_AGE_MINUTES", "").strip()
    raw_query_concurrency = os.environ.get(
        f"{ENV_PREFIX}ATHENA_QUERY_BATCH_MAX_CONCURRENCY", ""
    ).strip()
    try:
        timeout, concurrency, requests, reuse, query_concurrency = (
            int(raw_timeout),
            int(raw_concurrency),
            int(raw_requests),
            int(raw_reuse),
            int(raw_query_concurrency),
        )
    except ValueError:
        timeout, concurrency, requests, reuse, query_concurrency = 0, 0, 0, 0, 0
    values = {
        "semantic_publications_table": table,
        "semantic_v2_bucket": semantic_v2_bucket,
        "preset_artifact_bucket": preset_artifact_bucket,
        "insight_cache_table": cache,
        "insight_prompt_registry_table": registry,
        "insight_quick_model_id": quick_model,
        "insight_deep_model_id": deep_model,
        "insight_generation_timeout_seconds": timeout,
        "insight_generation_max_concurrency": concurrency,
        "insight_precompute_max_requests": requests,
        "athena_workgroup": workgroup,
        "semantic_v2_current_database": database,
        "athena_result_reuse_max_age_minutes": reuse,
        "athena_query_batch_max_concurrency": query_concurrency,
    }
    if any(not value for value in values.values()):
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Insight precompute configuration is incomplete.",
            context={"missing": [name for name, value in values.items() if not value]},
        )
    return InsightPrecomputeSettings(
        semantic_publications_table=table,
        semantic_v2_bucket=semantic_v2_bucket,
        preset_artifact_bucket=preset_artifact_bucket,
        insight_cache_table=cache,
        insight_prompt_registry_table=registry,
        insight_quick_model_id=quick_model,
        insight_deep_model_id=deep_model,
        insight_generation_timeout_seconds=timeout,
        insight_generation_max_concurrency=concurrency,
        insight_precompute_max_requests=requests,
        athena_workgroup=workgroup,
        semantic_v2_current_database=database,
        athena_result_reuse_max_age_minutes=reuse,
        athena_query_batch_max_concurrency=query_concurrency,
    )


@lru_cache(maxsize=1)
def insight_continuation_worker_settings() -> InsightContinuationWorkerSettings:
    """Resolve the exact durable cache-fill worker resources at initialization."""
    cache = os.environ.get(f"{ENV_PREFIX}INSIGHT_CACHE_TABLE", "").strip()
    registry = os.environ.get(f"{ENV_PREFIX}INSIGHT_PROMPT_REGISTRY_TABLE", "").strip()
    quick_model = os.environ.get(f"{ENV_PREFIX}INSIGHT_QUICK_MODEL_ID", "").strip()
    deep_model = os.environ.get(f"{ENV_PREFIX}INSIGHT_DEEP_MODEL_ID", "").strip()
    raw_timeout = os.environ.get(f"{ENV_PREFIX}INSIGHT_GENERATION_TIMEOUT_SECONDS", "").strip()
    raw_receives = os.environ.get(f"{ENV_PREFIX}INSIGHT_CONTINUATION_MAX_RECEIVE_COUNT", "").strip()
    try:
        timeout = int(raw_timeout)
    except ValueError:
        timeout = 0
    try:
        max_receive_count = int(raw_receives)
    except ValueError:
        max_receive_count = 0
    values = {
        "insight_cache_table": cache,
        "insight_prompt_registry_table": registry,
        "insight_quick_model_id": quick_model,
        "insight_deep_model_id": deep_model,
        "insight_generation_timeout_seconds": timeout,
        "insight_continuation_max_receive_count": max_receive_count,
    }
    if any(not value for value in values.values()):
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Insight continuation worker configuration is incomplete.",
            context={"missing": [name for name, value in values.items() if not value]},
        )
    return InsightContinuationWorkerSettings(
        cache, registry, quick_model, deep_model, timeout, max_receive_count
    )


@lru_cache(maxsize=1)
def insight_prompt_calibration_worker_settings() -> InsightPromptCalibrationWorkerSettings:
    """Resolve the queued calibration worker's exact resources at initialization."""
    registry = os.environ.get(f"{ENV_PREFIX}INSIGHT_PROMPT_REGISTRY_TABLE", "").strip()
    queue_url = os.environ.get(f"{ENV_PREFIX}INSIGHT_PROMPT_CALIBRATION_QUEUE_URL", "").strip()
    quick_model = os.environ.get(f"{ENV_PREFIX}INSIGHT_QUICK_MODEL_ID", "").strip()
    deep_model = os.environ.get(f"{ENV_PREFIX}INSIGHT_DEEP_MODEL_ID", "").strip()
    raw_timeout = os.environ.get(f"{ENV_PREFIX}INSIGHT_GENERATION_TIMEOUT_SECONDS", "").strip()
    raw_lease = os.environ.get(f"{ENV_PREFIX}INSIGHT_PROMPT_CALIBRATION_LEASE_SECONDS", "").strip()
    try:
        timeout, lease_seconds = int(raw_timeout), int(raw_lease)
    except ValueError:
        timeout, lease_seconds = 0, 0
    values = {
        "insight_prompt_registry_table": registry,
        "insight_prompt_calibration_queue_url": queue_url,
        "insight_quick_model_id": quick_model,
        "insight_deep_model_id": deep_model,
        "insight_generation_timeout_seconds": timeout,
        "insight_prompt_calibration_lease_seconds": lease_seconds,
    }
    if any(not value for value in values.values()):
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Insight prompt calibration worker configuration is incomplete.",
            context={"missing": [name for name, value in values.items() if not value]},
        )
    return InsightPromptCalibrationWorkerSettings(
        registry, queue_url, quick_model, deep_model, timeout, lease_seconds
    )


@lru_cache(maxsize=1)
def prompt_registry_seed_settings() -> PromptRegistrySeedSettings:
    """Resolve the retained registry target without building request-serving settings."""
    table = os.environ.get(f"{ENV_PREFIX}INSIGHT_PROMPT_REGISTRY_TABLE", "").strip()
    if not table:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Prompt registry seed configuration is incomplete.",
            context={"missing": ["prompt_registry_table"]},
        )
    return PromptRegistrySeedSettings(prompt_registry_table=table)


@lru_cache(maxsize=1)
def preset_artifact_enqueue_settings() -> PresetArtifactEnqueueSettings:
    """Resolve the stream worker's exact read/send resources at initialization."""
    table = os.environ.get(f"{ENV_PREFIX}SEMANTIC_PUBLICATIONS_TABLE", "").strip()
    queue_url = os.environ.get(f"{ENV_PREFIX}PRESET_ARTIFACT_BAKE_QUEUE_URL", "").strip()
    if not table or not queue_url:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Preset artifact enqueue configuration is incomplete.",
            context={
                "missing": [
                    name
                    for name, value in (
                        ("semantic_publications_table", table),
                        ("bake_queue_url", queue_url),
                    )
                    if not value
                ]
            },
        )
    return PresetArtifactEnqueueSettings(
        semantic_publications_table=table, bake_queue_url=queue_url
    )


@lru_cache(maxsize=1)
def preset_artifact_bake_settings() -> PresetArtifactBakeSettings:
    """Resolve the baker's exact immutable persistence targets at initialization."""
    bucket = os.environ.get(f"{ENV_PREFIX}PRESET_ARTIFACT_BUCKET", "").strip()
    pointer_table = os.environ.get(f"{ENV_PREFIX}PRESET_ARTIFACT_POINTER_TABLE", "").strip()
    if not bucket or not pointer_table:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Preset artifact baker configuration is incomplete.",
            context={
                "missing": [
                    name
                    for name, value in (
                        ("artifact_bucket", bucket),
                        ("pointer_table", pointer_table),
                    )
                    if not value
                ]
            },
        )
    raw_reuse = os.environ.get(f"{ENV_PREFIX}ATHENA_RESULT_REUSE_MAX_AGE_MINUTES", "").strip()
    raw_query_concurrency = os.environ.get(
        f"{ENV_PREFIX}ATHENA_QUERY_BATCH_MAX_CONCURRENCY", ""
    ).strip()
    try:
        reuse, query_concurrency = int(raw_reuse), int(raw_query_concurrency)
    except ValueError:
        reuse, query_concurrency = 0, 0
    return PresetArtifactBakeSettings(
        artifact_bucket=bucket,
        pointer_table=pointer_table,
        semantic_publications_table=os.environ.get(
            f"{ENV_PREFIX}SEMANTIC_PUBLICATIONS_TABLE", ""
        ).strip(),
        semantic_v2_bucket=os.environ.get(f"{ENV_PREFIX}SEMANTIC_V2_BUCKET", "").strip(),
        athena_workgroup=os.environ.get(
            f"{ENV_PREFIX}SEMANTIC_V2_ATHENA_BAKE_WORKGROUP", ""
        ).strip(),
        semantic_v2_current_database=os.environ.get(
            f"{ENV_PREFIX}SEMANTIC_V2_CURRENT_DATABASE", ""
        ).strip(),
        athena_result_reuse_max_age_minutes=reuse,
        athena_query_batch_max_concurrency=query_concurrency,
        insight_continuation_queue_url=os.environ.get(
            f"{ENV_PREFIX}INSIGHT_CONTINUATION_QUEUE_URL", ""
        ).strip(),
        insight_prompt_registry_table=os.environ.get(
            f"{ENV_PREFIX}INSIGHT_PROMPT_REGISTRY_TABLE", ""
        ).strip(),
    )


@lru_cache(maxsize=1)
def preset_artifact_backfill_settings() -> PresetArtifactBackfillSettings:
    """Resolve the backfill provider's declared FIFO target at initialization."""
    queue_url = os.environ.get(f"{ENV_PREFIX}PRESET_ARTIFACT_BAKE_QUEUE_URL", "").strip()
    if not queue_url:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Preset artifact backfill configuration is incomplete.",
            context={"missing": ["bake_queue_url"]},
        )
    return PresetArtifactBackfillSettings(bake_queue_url=queue_url)


def _receipt_retention_days() -> int:
    """Read the declared receipt retention (ADR-081 §5); anything but a whole day count is 0.

    A count past the representable bound is also 0, so the writer refuses to start with the
    configuration error rather than failing later on its first expiry.
    """
    raw = os.environ.get(f"{ENV_PREFIX}RECEIPT_RETENTION_DAYS", "").strip()
    if not raw.isdecimal() or not raw.isascii():
        return 0
    days = int(raw)
    return days if days <= MAX_RECORD_RETENTION_DAYS else 0


@lru_cache(maxsize=1)
def evaluation_writer_settings() -> EvaluationWriterSettings:
    """Resolve writer-only configuration without constructing request-serving Settings."""
    group = os.environ.get(f"{ENV_PREFIX}EVALUATION_INPUT_LOG_GROUP", "").strip()
    service = os.environ.get(f"{ENV_PREFIX}EVALUATION_INPUT_SERVICE_NAME", "").strip()
    writer_receipts_table = os.environ.get(
        f"{ENV_PREFIX}EVALUATION_WRITER_RECEIPTS_TABLE", ""
    ).strip()
    retention_days = _receipt_retention_days()
    if not group or not service or not writer_receipts_table or retention_days < 1:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Evaluation writer configuration is incomplete.",
            context={
                "missing": [
                    name
                    for name, value in (
                        ("input_log_group", group),
                        ("service_name", service),
                        ("writer_receipts_table", writer_receipts_table),
                        ("receipt_retention_days", retention_days > 0),
                    )
                    if not value
                ]
            },
        )
    return EvaluationWriterSettings(group, service, writer_receipts_table, retention_days)


@lru_cache(maxsize=1)
def evaluation_outbox_dispatcher_settings() -> EvaluationOutboxDispatcherSettings:
    """Resolve dispatcher-only resources without constructing request-serving Settings."""
    outbox_table = os.environ.get(f"{ENV_PREFIX}EVALUATION_OUTBOX_TABLE", "").strip()
    deliveries_table = os.environ.get(f"{ENV_PREFIX}EVALUATION_DELIVERIES_TABLE", "").strip()
    queue_url = os.environ.get(f"{ENV_PREFIX}EVALUATION_QUEUE_URL", "").strip()
    raw_days = os.environ.get(f"{ENV_PREFIX}EVALUATION_OUTBOX_RETENTION_DAYS", "").strip()
    try:
        retention_days = int(raw_days)
    except ValueError:
        retention_days = 0
    receipt_retention_days = _receipt_retention_days()
    release_parameters = _csv_tuple(f"{ENV_PREFIX}EVALUATION_RELEASE_PARAMETERS")
    audit_table = os.environ.get(f"{ENV_PREFIX}AGENT_QUALITY_AUDIT_TABLE", "").strip()
    delivery_wait_seconds = _positive_whole(f"{ENV_PREFIX}EVALUATION_DELIVERY_WAIT_SECONDS")
    present = (
        ("outbox_table", bool(outbox_table)),
        ("deliveries_table", bool(deliveries_table)),
        ("queue_url", bool(queue_url)),
        ("delivered_retention_days", retention_days > 0),
        ("receipt_retention_days", receipt_retention_days > 0),
        ("release_parameters", bool(release_parameters)),
        ("audit_table", bool(audit_table)),
        ("delivery_wait_seconds", delivery_wait_seconds > 0),
    )
    if not all(value for _, value in present):
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Evaluation outbox dispatcher configuration is incomplete.",
            context={"missing": [name for name, value in present if not value]},
        )
    return EvaluationOutboxDispatcherSettings(
        outbox_table=outbox_table,
        deliveries_table=deliveries_table,
        queue_url=queue_url,
        delivered_retention_days=retention_days,
        receipt_retention_days=receipt_retention_days,
        release_parameters=release_parameters,
        audit_table=audit_table,
        delivery_wait_seconds=delivery_wait_seconds,
    )


def _csv_tuple(name: str) -> tuple[str, ...]:
    """Read one comma list in declared order, dropping blank entries."""
    return tuple(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())


def _positive_whole(name: str) -> int:
    """Read one positive whole ASCII number; anything else is 0, which refuses to start."""
    raw = os.environ.get(name, "").strip()
    if not raw.isdecimal() or not raw.isascii():
        return 0
    return int(raw)


@lru_cache(maxsize=1)
def agent_quality_processor_settings() -> AgentQualityProcessorSettings:
    """Resolve the three retained table identities used only by the audit processor."""
    audit_table = os.environ.get(f"{ENV_PREFIX}AGENT_QUALITY_AUDIT_TABLE", "").strip()
    findings_table = os.environ.get(f"{ENV_PREFIX}AGENT_QUALITY_FINDINGS_TABLE", "").strip()
    aggregate_table = os.environ.get(f"{ENV_PREFIX}AGENT_QUALITY_AGGREGATE_TABLE", "").strip()
    retention_days = _receipt_retention_days()
    if not audit_table or not findings_table or not aggregate_table or retention_days < 1:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Agent quality processor configuration is incomplete.",
            context={
                "missing": [
                    name
                    for name, value in (
                        ("audit_table", audit_table),
                        ("findings_table", findings_table),
                        ("aggregate_table", aggregate_table),
                        ("receipt_retention_days", retention_days > 0),
                    )
                    if not value
                ]
            },
        )
    return AgentQualityProcessorSettings(
        audit_table=audit_table,
        findings_table=findings_table,
        aggregate_table=aggregate_table,
        receipt_retention_days=retention_days,
    )


def _csv_set(name: str) -> frozenset[str]:
    return frozenset(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())


@lru_cache(maxsize=1)
def evaluation_result_ingress_settings() -> EvaluationResultIngressSettings:
    """Resolve exact protected subscription sources without request-serving Settings.

    The owned result log groups decide whether a subscription record is ours. Which
    configuration and evaluator a result may come from is decided per turn by its delivery
    receipt (ADR-079), so no configuration or evaluator identity is configured here.
    """
    audit_table = os.environ.get(f"{ENV_PREFIX}AGENT_QUALITY_AUDIT_TABLE", "").strip()
    writer_receipts_table = os.environ.get(
        f"{ENV_PREFIX}EVALUATION_WRITER_RECEIPTS_TABLE", ""
    ).strip()
    deliveries_table = os.environ.get(f"{ENV_PREFIX}EVALUATION_DELIVERIES_TABLE", "").strip()
    projection_queue_url = os.environ.get(
        f"{ENV_PREFIX}EVALUATION_RESULT_PROJECTION_QUEUE_URL", ""
    ).strip()
    result_log_groups = _csv_set(f"{ENV_PREFIX}RELEASED_EVALUATION_RESULT_LOG_GROUPS")
    present = (
        ("audit_table", bool(audit_table)),
        ("writer_receipts_table", bool(writer_receipts_table)),
        ("deliveries_table", bool(deliveries_table)),
        ("projection_queue_url", bool(projection_queue_url)),
        ("result_log_groups", bool(result_log_groups)),
    )
    if not all(value for _, value in present):
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Evaluation result ingress configuration is incomplete.",
            context={"missing": [name for name, value in present if not value]},
        )
    return EvaluationResultIngressSettings(
        audit_table=audit_table,
        writer_receipts_table=writer_receipts_table,
        deliveries_table=deliveries_table,
        projection_queue_url=projection_queue_url,
        result_log_groups=result_log_groups,
    )


@lru_cache(maxsize=1)
def evaluation_result_persister_settings() -> EvaluationResultPersisterSettings:
    """Resolve the retained verdicts table for safe result-projection persistence."""
    verdicts_table = os.environ.get(f"{ENV_PREFIX}EVALUATION_VERDICTS_TABLE", "").strip()
    retention_days = _receipt_retention_days()
    if not verdicts_table or retention_days < 1:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Evaluation result persister configuration is incomplete.",
            context={
                "missing": [
                    name
                    for name, present in (
                        ("verdicts_table", bool(verdicts_table)),
                        ("receipt_retention_days", retention_days > 0),
                    )
                    if not present
                ]
            },
        )
    return EvaluationResultPersisterSettings(
        verdicts_table=verdicts_table, receipt_retention_days=retention_days
    )


@dataclass(frozen=True, slots=True)
class OperationalAttemptsSettings:
    """Exact FIFO and semantic authority consumed by the attempts-stream producer."""

    queue_url: str
    attempts_table: str


@dataclass(frozen=True, slots=True)
class OperationalProjectorSettings:
    """Retained projection and retained Stage-1 authority consumed by the FIFO projector."""

    status_table: str
    publications_table: str


@dataclass(frozen=True, slots=True)
class OperationalReconcilerSettings:
    """Exact projection, FIFO, and semantic authority consumed by reconciliation."""

    status_table: str
    queue_url: str
    attempts_table: str


@dataclass(frozen=True, slots=True)
class OperationalApiSettings:
    """Read-only retained projection consumed by the protected status API."""

    status_table: str


def _required_operational_values(*names: str) -> tuple[str, ...]:
    values = tuple(os.environ.get(f"{ENV_PREFIX}{name}", "").strip() for name in names)
    missing = [name.lower() for name, value in zip(names, values, strict=True) if not value]
    if missing:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Operational status configuration is incomplete.",
            context={"missing": missing},
        )
    return values


@lru_cache(maxsize=1)
def operational_attempts_settings() -> OperationalAttemptsSettings:
    """Resolve the direct FIFO target and exact attempt authority."""
    queue_url, attempts_table = _required_operational_values(
        "OPERATIONAL_STATUS_QUEUE_URL", "SEMANTIC_ATTEMPTS_TABLE"
    )
    return OperationalAttemptsSettings(queue_url, attempts_table)


@lru_cache(maxsize=1)
def operational_projector_settings() -> OperationalProjectorSettings:
    """Resolve the projector's retained projection and retained publications targets."""
    status_table, publications_table = _required_operational_values(
        "OPERATIONAL_STATUS_TABLE", "SEMANTIC_PUBLICATIONS_TABLE"
    )
    return OperationalProjectorSettings(status_table, publications_table)


@lru_cache(maxsize=1)
def operational_reconciler_settings() -> OperationalReconcilerSettings:
    """Resolve all and only resources used by reconciliation."""
    status_table, queue_url, attempts_table = _required_operational_values(
        "OPERATIONAL_STATUS_TABLE",
        "OPERATIONAL_STATUS_QUEUE_URL",
        "SEMANTIC_ATTEMPTS_TABLE",
    )
    return OperationalReconcilerSettings(status_table, queue_url, attempts_table)


@lru_cache(maxsize=1)
def operational_api_settings() -> OperationalApiSettings:
    """Resolve the protected API's read-only projection table."""
    (status_table,) = _required_operational_values("OPERATIONAL_STATUS_TABLE")
    return OperationalApiSettings(status_table)


@dataclass(frozen=True, slots=True)
class RunRequestApiSettings:
    """The retained execution-graph table and raw bucket the run-request API uses."""

    execution_graph_table: str
    raw_bucket: str


@lru_cache(maxsize=1)
def run_request_api_settings() -> RunRequestApiSettings:
    """Resolve the run-request routes' execution-graph and raw-zone targets."""
    table, bucket = _required_operational_values("EXECUTION_GRAPH_TABLE", "RAW_BUCKET")
    return RunRequestApiSettings(table, bucket)


@dataclass(frozen=True, slots=True)
class GraphDispatcherSettings:
    """S6 §2.1: the graph dispatcher's queue for each generated contract route.

    Read only by the dispatcher entrypoint (the other graph workers share its module and set
    none of these), and wired in CDK from each queue's deterministic name.
    """

    delivery_queue_url: str
    semantic_write_queue_url: str
    bake_queue_url: str
    precompute_queue_url: str


@lru_cache(maxsize=1)
def graph_dispatcher_settings() -> GraphDispatcherSettings:
    """Resolve the dispatcher's four route queue URLs; any missing one is a config failure."""
    delivery, semantic_write, bake, precompute = _required_operational_values(
        "GRAPH_DELIVERY_QUEUE_URL",
        "SEMANTIC_WRITE_QUEUE_URL",
        "GRAPH_BAKE_QUEUE_URL",
        "GRAPH_PRECOMPUTE_QUEUE_URL",
    )
    return GraphDispatcherSettings(delivery, semantic_write, bake, precompute)


@dataclass(frozen=True, slots=True)
class GraphConsumerSettings:
    """S6 §2.8, §2.9: the graph table a bake, precompute or backfill worker reads its mode from.

    Blank where the graph authority is not provisioned: the worker then reads `LEGACY`, exactly
    as an absent mode item reads, and composes no graph consumer.
    """

    execution_graph_table: str


@lru_cache(maxsize=1)
def graph_consumer_settings() -> GraphConsumerSettings:
    """Resolve the optional graph table; absence means the graph authority is not provisioned."""
    return GraphConsumerSettings(os.environ.get(f"{ENV_PREFIX}EXECUTION_GRAPH_TABLE", "").strip())


@dataclass(frozen=True, slots=True)
class OperationalCompanionSettings:
    """Optional status FIFO for workload companion producers; blank disables emission."""

    queue_url: str


@lru_cache(maxsize=1)
def operational_companion_settings() -> OperationalCompanionSettings:
    """Resolve the optional companion target; absence means the surface is not provisioned."""
    return OperationalCompanionSettings(
        os.environ.get(f"{ENV_PREFIX}OPERATIONAL_STATUS_QUEUE_URL", "").strip()
    )


class ProjectionHandoffState(StrEnum):
    """Closed Stage-1 projection composition and event-source handoff states."""

    INLINE_ACTIVE = "INLINE_ACTIVE"
    INLINE_QUIESCED = "INLINE_QUIESCED"
    QUEUED_QUIESCED = "QUEUED_QUIESCED"
    QUEUED_ACCUMULATE = "QUEUED_ACCUMULATE"
    QUEUED_ACTIVE = "QUEUED_ACTIVE"
    QUEUED_DRAIN = "QUEUED_DRAIN"


@dataclass(frozen=True, slots=True)
class IngestionConfig:
    """Sole strict owner of the reviewed Stage-1 ingestion limits and handoff state."""

    projection_handoff_state: ProjectionHandoffState
    max_units: int
    max_accepted_attempts_per_unit: int
    max_accepted_attempts_per_plan: int
    max_group_members: int
    max_pointers_per_table: int
    max_object_refs_per_unit: int
    athena_max_statement_bytes: int
    max_encoded_identity_bytes: int
    max_partition_identity_bytes: int
    table_writer_timeout_seconds: int
    table_writer_cleanup_reserve_ms: int
    table_writer_concurrency: int
    semantic_write_max_receive_count: int
    table_lease_duration_seconds: int
    semantic_write_visibility_margin_seconds: int
    table_attempt_reconcile_batch_size: int
    table_attempt_reconcile_base_backoff_seconds: int
    table_attempt_reconcile_max_backoff_seconds: int
    plan_intent_reconcile_batch_size: int
    plan_intent_reconcile_base_backoff_seconds: int
    plan_intent_reconcile_max_backoff_seconds: int
    drain_observation_interval_seconds: int


@dataclass(frozen=True, slots=True)
class Stage1DeliverySettings:
    """Limits consumed only by Stage-1 plan preparation and the transitional inline writer.

    No queue target: the delivery Lambda never sends a semantic-write message, so it carries no
    queue URL. ``table_lease_duration_seconds`` is the §11 ``tableLeaseDurationSeconds`` value the
    transitional inline delivery composition passes to its table-lease acquisition.
    """

    max_units: int
    max_accepted_attempts_per_unit: int
    max_accepted_attempts_per_plan: int
    max_group_members: int
    max_pointers_per_table: int
    max_object_refs_per_unit: int
    athena_max_statement_bytes: int
    max_encoded_identity_bytes: int
    max_partition_identity_bytes: int
    table_lease_duration_seconds: int


@dataclass(frozen=True, slots=True)
class TableWriterSettings:
    """Shared limits and queue target consumed only by the serialized table writer."""

    semantic_write_queue_url: str
    max_units: int
    max_accepted_attempts_per_unit: int
    max_accepted_attempts_per_plan: int
    max_group_members: int
    max_pointers_per_table: int
    max_object_refs_per_unit: int
    athena_max_statement_bytes: int
    max_encoded_identity_bytes: int
    max_partition_identity_bytes: int
    table_writer_timeout_seconds: int
    table_writer_cleanup_reserve_ms: int
    table_writer_concurrency: int
    semantic_write_max_receive_count: int
    table_lease_duration_seconds: int
    semantic_write_visibility_margin_seconds: int


@dataclass(frozen=True, slots=True)
class TableAttemptReconcilerSettings:
    """Exact query and retry limits plus the two retained tables provider reconciliation reads."""

    batch_size: int
    base_backoff_seconds: int
    max_backoff_seconds: int
    publications_table: str
    leases_table: str


@dataclass(frozen=True, slots=True)
class PlanIntentReconcilerSettings:
    """Query/retry limits, roster bound, queue target, and table for intent reconciliation."""

    batch_size: int
    base_backoff_seconds: int
    max_backoff_seconds: int
    max_units: int
    semantic_write_queue_url: str
    publications_table: str


@dataclass(frozen=True, slots=True)
class ProviderFenceRecoverySettings:
    """Only the retained publications table the fence recovery broker reads and requeues."""

    publications_table: str


@dataclass(frozen=True, slots=True)
class ProjectionHandoffSettings:
    """Exact state and drain interval consumed only by the handoff provider."""

    state: ProjectionHandoffState
    drain_observation_interval_seconds: int


@dataclass(frozen=True, slots=True)
class Stage1LifecycleProducerSettings:
    """Exact limits and authorities consumed only by Stage-1 lifecycle delivery."""

    reconcile_batch_size: int
    max_consecutive_failed_delivery_attempts: int
    queue_url: str
    publications_table: str
    status_table: str


@dataclass(frozen=True, slots=True)
class Stage1OutboxRecoverySettings:
    """Only the two retained tables available to protected Stage-1 recovery."""

    publications_table: str
    status_table: str


@dataclass(frozen=True, slots=True)
class ProjectionHandoffProviderSettings:
    """Only the deployment identity and retained authority table for the handoff provider."""

    environment: str
    publications_table: str


_INGESTION_INTEGER_ENV = (
    ("max_units", "ADL_STAGE1_MAX_UNITS"),
    ("max_accepted_attempts_per_unit", "ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_UNIT"),
    ("max_accepted_attempts_per_plan", "ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_PLAN"),
    ("max_group_members", "ADL_STAGE1_MAX_GROUP_MEMBERS"),
    ("max_pointers_per_table", "ADL_STAGE1_MAX_POINTERS_PER_TABLE"),
    ("max_object_refs_per_unit", "ADL_STAGE1_MAX_OBJECT_REFS_PER_UNIT"),
    ("athena_max_statement_bytes", "ADL_STAGE1_ATHENA_MAX_STATEMENT_BYTES"),
    ("max_encoded_identity_bytes", "ADL_STAGE1_MAX_ENCODED_IDENTITY_BYTES"),
    ("max_partition_identity_bytes", "ADL_STAGE1_MAX_PARTITION_IDENTITY_BYTES"),
    ("table_writer_timeout_seconds", "ADL_TABLE_WRITER_TIMEOUT_SECONDS"),
    ("table_writer_cleanup_reserve_ms", "ADL_TABLE_WRITER_CLEANUP_RESERVE_MS"),
    ("table_writer_concurrency", "ADL_TABLE_WRITER_CONCURRENCY"),
    ("semantic_write_max_receive_count", "ADL_TABLE_WRITER_MAX_RECEIVE_COUNT"),
    ("table_lease_duration_seconds", "ADL_TABLE_LEASE_DURATION_SECONDS"),
    (
        "semantic_write_visibility_margin_seconds",
        "ADL_TABLE_WRITER_VISIBILITY_MARGIN_SECONDS",
    ),
    ("table_attempt_reconcile_batch_size", "ADL_TABLE_ATTEMPT_RECONCILE_BATCH_SIZE"),
    (
        "table_attempt_reconcile_base_backoff_seconds",
        "ADL_TABLE_ATTEMPT_RECONCILE_BASE_BACKOFF_SECONDS",
    ),
    (
        "table_attempt_reconcile_max_backoff_seconds",
        "ADL_TABLE_ATTEMPT_RECONCILE_MAX_BACKOFF_SECONDS",
    ),
    ("plan_intent_reconcile_batch_size", "ADL_PLAN_INTENT_RECONCILE_BATCH_SIZE"),
    (
        "plan_intent_reconcile_base_backoff_seconds",
        "ADL_PLAN_INTENT_RECONCILE_BASE_BACKOFF_SECONDS",
    ),
    (
        "plan_intent_reconcile_max_backoff_seconds",
        "ADL_PLAN_INTENT_RECONCILE_MAX_BACKOFF_SECONDS",
    ),
    (
        "drain_observation_interval_seconds",
        "ADL_PROJECTION_HANDOFF_DRAIN_OBSERVATION_SECONDS",
    ),
)


_RECONCILE_BATCH_SIZE = 10
# Write-discipline §11 amendment, 2026-10-10: raised from 10 so the intent reconciler
# (one tick a minute) feeds the faster table writer instead of capping it.
_PLAN_INTENT_RECONCILE_BATCH_SIZE = 50
_TABLE_WRITER_CLEANUP_RESERVE_MS = 60_000
_MAX_ACCEPTED_ATTEMPTS_PER_UNIT = 10
_MAX_ACCEPTED_ATTEMPTS_PER_PLAN = 32
_SEMANTIC_WRITE_VISIBILITY_SECONDS = 960
_SQS_MAX_VISIBILITY_SECONDS = 43_200
_STAGE1_OUTBOX_FAILURE_THRESHOLD = 3


def _strict_positive_integer(value: object) -> int | None:
    if type(value) is int:
        return value if value > 0 else None
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        return None
    parsed = int(value)
    return parsed if parsed > 0 and value == str(parsed) else None


def _configuration_failure(description: str, problems: list[str]) -> NoReturn:
    raise InvariantError(
        CONFIG_UNRESOLVED,
        description,
        context={"problems": sorted(problems)},
    )


def _ingestion_constraint_problems(values: Mapping[str, int]) -> list[str]:
    problems: list[str] = []
    if values["table_attempt_reconcile_batch_size"] != _RECONCILE_BATCH_SIZE:
        problems.append("ADL_TABLE_ATTEMPT_RECONCILE_BATCH_SIZE (must equal 10)")
    if values["plan_intent_reconcile_batch_size"] != _PLAN_INTENT_RECONCILE_BATCH_SIZE:
        problems.append("ADL_PLAN_INTENT_RECONCILE_BATCH_SIZE (must equal 50)")
    if values["table_writer_cleanup_reserve_ms"] != _TABLE_WRITER_CLEANUP_RESERVE_MS:
        problems.append("ADL_TABLE_WRITER_CLEANUP_RESERVE_MS (must equal 60000)")
    if values["table_writer_cleanup_reserve_ms"] >= values["table_writer_timeout_seconds"] * 1_000:
        problems.append("ADL_TABLE_WRITER_CLEANUP_RESERVE_MS (must be less than writer timeout)")
    if values["table_lease_duration_seconds"] <= values["table_writer_timeout_seconds"]:
        problems.append("ADL_TABLE_LEASE_DURATION_SECONDS (must exceed writer timeout)")
    if values["max_accepted_attempts_per_unit"] != _MAX_ACCEPTED_ATTEMPTS_PER_UNIT:
        problems.append("ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_UNIT (must equal 10)")
    if values["max_accepted_attempts_per_plan"] != _MAX_ACCEPTED_ATTEMPTS_PER_PLAN:
        problems.append("ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_PLAN (must equal 32)")
    visibility_seconds = (
        values["table_writer_timeout_seconds"] + values["semantic_write_visibility_margin_seconds"]
    )
    if (
        visibility_seconds != _SEMANTIC_WRITE_VISIBILITY_SECONDS
        or visibility_seconds > _SQS_MAX_VISIBILITY_SECONDS
    ):
        problems.append(
            "ADL_TABLE_WRITER_VISIBILITY_MARGIN_SECONDS "
            "(writer timeout plus margin must equal 960 seconds within the SQS ceiling)"
        )
    if (
        values["table_attempt_reconcile_max_backoff_seconds"]
        < values["table_attempt_reconcile_base_backoff_seconds"]
    ):
        problems.append(
            "ADL_TABLE_ATTEMPT_RECONCILE_MAX_BACKOFF_SECONDS (must be at least the base backoff)"
        )
    if (
        values["plan_intent_reconcile_max_backoff_seconds"]
        < values["plan_intent_reconcile_base_backoff_seconds"]
    ):
        problems.append(
            "ADL_PLAN_INTENT_RECONCILE_MAX_BACKOFF_SECONDS (must be at least the base backoff)"
        )
    return problems


def _parse_ingestion_config(
    source: Mapping[str, object], *, required_nonblank: tuple[str, ...] = ()
) -> IngestionConfig:
    problems: list[str] = []
    raw_state = source.get("ADL_PROJECTION_HANDOFF_STATE")
    try:
        state = ProjectionHandoffState(raw_state)
    except TypeError, ValueError:
        state = None
        problem = "field required" if raw_state is None or raw_state == "" else "unknown state"
        problems.append(f"ADL_PROJECTION_HANDOFF_STATE ({problem})")

    values: dict[str, int] = {}
    for field_name, environment_name in _INGESTION_INTEGER_ENV:
        raw_value = source.get(environment_name)
        parsed = _strict_positive_integer(raw_value)
        if parsed is None:
            problem = (
                "field required"
                if raw_value is None or raw_value == ""
                else "must be a canonical positive integer"
            )
            problems.append(f"{environment_name} ({problem})")
        else:
            values[field_name] = parsed
    for environment_name in required_nonblank:
        raw_value = source.get(environment_name)
        if not isinstance(raw_value, str) or not raw_value.strip():
            problems.append(f"{environment_name} (field required)")
    if problems:
        _configuration_failure("Stage-1 ingestion configuration did not resolve.", problems)

    problems.extend(_ingestion_constraint_problems(values))
    if problems:
        _configuration_failure("Stage-1 ingestion configuration did not resolve.", problems)
    if state is None:  # pragma: no cover - rejected above; narrows the strict parsed type
        raise RuntimeError("validated handoff state is unavailable")
    return IngestionConfig(projection_handoff_state=state, **values)


@lru_cache(maxsize=1)
def _ingestion_config() -> IngestionConfig:
    return _parse_ingestion_config(os.environ)


@lru_cache(maxsize=1)
def _queued_ingestion_config() -> tuple[IngestionConfig, str]:
    queue_name = "ADL_SEMANTIC_WRITE_QUEUE_URL"
    config = _parse_ingestion_config(os.environ, required_nonblank=(queue_name,))
    queue_url = os.environ.get(queue_name)
    if queue_url is None:  # pragma: no cover - rejected above; narrows the parsed type
        raise RuntimeError("validated semantic-write queue URL is unavailable")
    return config, queue_url.strip()


@lru_cache(maxsize=1)
def stage1_delivery_settings() -> Stage1DeliverySettings:
    """Resolve all and only Stage-1 plan-preparation and inline-writer limits."""
    config = _ingestion_config()
    return Stage1DeliverySettings(
        max_units=config.max_units,
        max_accepted_attempts_per_unit=config.max_accepted_attempts_per_unit,
        max_accepted_attempts_per_plan=config.max_accepted_attempts_per_plan,
        max_group_members=config.max_group_members,
        max_pointers_per_table=config.max_pointers_per_table,
        max_object_refs_per_unit=config.max_object_refs_per_unit,
        athena_max_statement_bytes=config.athena_max_statement_bytes,
        max_encoded_identity_bytes=config.max_encoded_identity_bytes,
        max_partition_identity_bytes=config.max_partition_identity_bytes,
        table_lease_duration_seconds=config.table_lease_duration_seconds,
    )


@lru_cache(maxsize=1)
def table_writer_settings() -> TableWriterSettings:
    """Resolve all and only serialized table-writer limits and its queue target."""
    config, queue_url = _queued_ingestion_config()
    return TableWriterSettings(
        semantic_write_queue_url=queue_url,
        max_units=config.max_units,
        max_accepted_attempts_per_unit=config.max_accepted_attempts_per_unit,
        max_accepted_attempts_per_plan=config.max_accepted_attempts_per_plan,
        max_group_members=config.max_group_members,
        max_pointers_per_table=config.max_pointers_per_table,
        max_object_refs_per_unit=config.max_object_refs_per_unit,
        athena_max_statement_bytes=config.athena_max_statement_bytes,
        max_encoded_identity_bytes=config.max_encoded_identity_bytes,
        max_partition_identity_bytes=config.max_partition_identity_bytes,
        table_writer_timeout_seconds=config.table_writer_timeout_seconds,
        table_writer_cleanup_reserve_ms=config.table_writer_cleanup_reserve_ms,
        table_writer_concurrency=config.table_writer_concurrency,
        semantic_write_max_receive_count=config.semantic_write_max_receive_count,
        table_lease_duration_seconds=config.table_lease_duration_seconds,
        semantic_write_visibility_margin_seconds=(config.semantic_write_visibility_margin_seconds),
    )


_PUBLICATIONS_TABLE_ENV = "ADL_SEMANTIC_PUBLICATIONS_TABLE"
_LEASES_TABLE_ENV = "ADL_SEMANTIC_LEASES_TABLE"


def _required_text(source: Mapping[str, object], name: str) -> str:
    value = source.get(name)
    if not isinstance(value, str) or not value.strip():
        _configuration_failure(
            "Stage-1 ingestion configuration did not resolve.", [f"{name} (field required)"]
        )
    return value.strip()


@lru_cache(maxsize=1)
def table_attempt_reconciler_settings() -> TableAttemptReconcilerSettings:
    """Resolve only provider-attempt reconciliation limits and its two retained tables."""
    config = _parse_ingestion_config(
        os.environ, required_nonblank=(_PUBLICATIONS_TABLE_ENV, _LEASES_TABLE_ENV)
    )
    return TableAttemptReconcilerSettings(
        batch_size=config.table_attempt_reconcile_batch_size,
        base_backoff_seconds=config.table_attempt_reconcile_base_backoff_seconds,
        max_backoff_seconds=config.table_attempt_reconcile_max_backoff_seconds,
        publications_table=_required_text(os.environ, _PUBLICATIONS_TABLE_ENV),
        leases_table=_required_text(os.environ, _LEASES_TABLE_ENV),
    )


@lru_cache(maxsize=1)
def plan_intent_reconciler_settings() -> PlanIntentReconcilerSettings:
    """Resolve only plan-intent reconciliation limits, its queue target, and its table."""
    config, queue_url = _queued_ingestion_config()
    return PlanIntentReconcilerSettings(
        batch_size=config.plan_intent_reconcile_batch_size,
        base_backoff_seconds=config.plan_intent_reconcile_base_backoff_seconds,
        max_backoff_seconds=config.plan_intent_reconcile_max_backoff_seconds,
        max_units=config.max_units,
        semantic_write_queue_url=queue_url,
        publications_table=_required_text(os.environ, _PUBLICATIONS_TABLE_ENV),
    )


@lru_cache(maxsize=1)
def provider_fence_recovery_settings() -> ProviderFenceRecoverySettings:
    """Resolve only the retained publications table the recovery broker is authorized on."""
    _parse_ingestion_config(os.environ, required_nonblank=(_PUBLICATIONS_TABLE_ENV,))
    return ProviderFenceRecoverySettings(
        publications_table=_required_text(os.environ, _PUBLICATIONS_TABLE_ENV)
    )


DRAIN_OBSERVATION_INTERVAL_ENV = "ADL_PROJECTION_HANDOFF_DRAIN_OBSERVATION_SECONDS"


def parse_drain_observation_interval(rendered: Mapping[str, object]) -> int:
    """Read the §11 drain interval from one deployed function's rendered environment.

    The read-only verifier consumes the deployed value rather than its own configuration, so the
    interval it enforces is byte-identical to the one every deployed consumer received; a missing
    or non-canonical value is a configuration failure, never a default.
    """
    parsed = _strict_positive_integer(rendered.get(DRAIN_OBSERVATION_INTERVAL_ENV))
    if parsed is None:
        _configuration_failure(
            "Stage-1 ingestion configuration did not resolve.",
            [f"{DRAIN_OBSERVATION_INTERVAL_ENV} (field required)"],
        )
    return parsed


@lru_cache(maxsize=1)
def projection_handoff_settings() -> ProjectionHandoffSettings:
    """Resolve only the closed handoff state and its drain observation interval."""
    config = _ingestion_config()
    return ProjectionHandoffSettings(
        state=config.projection_handoff_state,
        drain_observation_interval_seconds=config.drain_observation_interval_seconds,
    )


@lru_cache(maxsize=1)
def stage1_lifecycle_producer_settings() -> Stage1LifecycleProducerSettings:
    """Resolve the approved lifecycle producer limits and exact retained authorities."""
    source: Mapping[str, object] = os.environ
    integer_names = (
        "ADL_STAGE1_LIFECYCLE_RECONCILE_BATCH_SIZE",
        "ADL_STAGE1_OUTBOX_MAX_CONSECUTIVE_FAILED_DELIVERY_ATTEMPTS",
    )
    integer_values = {name: _strict_positive_integer(source.get(name)) for name in integer_names}
    resource_names = (
        "ADL_OPERATIONAL_STATUS_QUEUE_URL",
        "ADL_SEMANTIC_PUBLICATIONS_TABLE",
        "ADL_OPERATIONAL_STATUS_TABLE",
    )
    resources = {
        name: value.strip() if isinstance(value := source.get(name), str) else ""
        for name in resource_names
    }
    problems: list[str] = []
    for name, value in integer_values.items():
        if value is None:
            reason = (
                "field required"
                if source.get(name) in (None, "")
                else "must be a canonical positive integer"
            )
            problems.append(f"{name} ({reason})")
    problems.extend(f"{name} (field required)" for name, value in resources.items() if not value)
    if not problems and integer_values[integer_names[0]] != _RECONCILE_BATCH_SIZE:
        problems.append(f"{integer_names[0]} (must equal 10)")
    if not problems and integer_values[integer_names[1]] != _STAGE1_OUTBOX_FAILURE_THRESHOLD:
        problems.append(f"{integer_names[1]} (must equal 3)")
    if problems:
        _configuration_failure("Stage-1 lifecycle configuration did not resolve.", problems)

    batch_size = integer_values[integer_names[0]] or 0
    threshold = integer_values[integer_names[1]] or 0
    return Stage1LifecycleProducerSettings(
        reconcile_batch_size=batch_size,
        max_consecutive_failed_delivery_attempts=threshold,
        queue_url=resources[resource_names[0]],
        publications_table=resources[resource_names[1]],
        status_table=resources[resource_names[2]],
    )


@lru_cache(maxsize=1)
def stage1_outbox_recovery_settings() -> Stage1OutboxRecoverySettings:
    """Resolve only the retained tables supplied to protected recovery."""
    source: Mapping[str, object] = os.environ
    names = ("ADL_SEMANTIC_PUBLICATIONS_TABLE", "ADL_OPERATIONAL_STATUS_TABLE")
    values = {
        name: value.strip() if isinstance(value := source.get(name), str) else "" for name in names
    }
    problems = [f"{name} (field required)" for name, value in values.items() if not value]
    if problems:
        _configuration_failure("Stage-1 recovery configuration did not resolve.", problems)
    return Stage1OutboxRecoverySettings(values[names[0]], values[names[1]])


@lru_cache(maxsize=1)
def projection_handoff_provider_settings() -> ProjectionHandoffProviderSettings:
    """Resolve only the environment identity and retained table the provider may touch."""
    source: Mapping[str, object] = os.environ
    names = ("ADL_ENVIRONMENT", "ADL_SEMANTIC_PUBLICATIONS_TABLE")
    values = {
        name: value.strip() if isinstance(value := source.get(name), str) else "" for name in names
    }
    problems = [f"{name} (field required)" for name, value in values.items() if not value]
    if problems:
        _configuration_failure(
            "Projection handoff provider configuration did not resolve.", problems
        )
    return ProjectionHandoffProviderSettings(values[names[0]], values[names[1]])


def cognito_issuer_for(aws_region: str, user_pool_id: str) -> str:
    """Return the Cognito user pool issuer; the one derivation every verifier shares."""
    return f"https://cognito-idp.{aws_region}.amazonaws.com/{user_pool_id}"


def cognito_jwks_url_for(issuer: str) -> str:
    """Return the user pool key set published under its issuer."""
    return f"{issuer}/.well-known/jwks.json"


@dataclass(frozen=True, slots=True)
class TokenServiceSettings:
    """Configuration for the token service's route exchange and public issuer (ADR-077 §3).

    Narrow on purpose: the token functions hold no BFF client, cookie, table, or Harness
    configuration, so they are not given the request-serving `Settings`.
    """

    environment: str
    aws_region: str
    cognito_user_pool_id: str
    cognito_client_id: str
    token_issuer: str
    token_signing_key_id: str
    harness_token_audience: str
    route_client_id: str
    harness_token_lifetime_seconds: int
    gateway_token_audience: str
    agent_client_id: str
    agent_client_secret_id: str
    """The secret's ARN, not its value: the value is read at run time through ClientSecretPort."""

    @property
    def cognito_issuer(self) -> str:
        """Return the issuer the route exchange's subject tokens must carry."""
        return cognito_issuer_for(self.aws_region, self.cognito_user_pool_id)

    @property
    def cognito_jwks_url(self) -> str:
        """Return the user pool key set, derived exactly as the route's verifier derives it."""
        return cognito_jwks_url_for(self.cognito_issuer)


_TOKEN_SERVICE_TEXT_ENV = (
    "ENVIRONMENT",
    "AWS_REGION",
    "COGNITO_USER_POOL_ID",
    "COGNITO_CLIENT_ID",
    "TOKEN_ISSUER",
    "TOKEN_SIGNING_KEY_ID",
    "HARNESS_TOKEN_AUDIENCE",
    "ROUTE_CLIENT_ID",
    "GATEWAY_TOKEN_AUDIENCE",
    "AGENT_CLIENT_ID",
    "AGENT_CLIENT_SECRET_ID",
)
_HARNESS_LIFETIME_ENV = "HARNESS_TOKEN_LIFETIME_SECONDS"


def read_token_service_settings(source: Mapping[str, str] | None = None) -> TokenServiceSettings:
    """Resolve the token service's configuration, reporting every problem at once."""
    read: Mapping[str, str] = os.environ if source is None else source
    values = {name: read.get(f"{ENV_PREFIX}{name}", "").strip() for name in _TOKEN_SERVICE_TEXT_ENV}
    problems = [f"{ENV_PREFIX}{name}" for name, value in values.items() if not value]
    issuer = values["TOKEN_ISSUER"]
    if issuer and (not issuer.startswith("https://") or issuer.endswith("/")):
        problems.append(f"{ENV_PREFIX}TOKEN_ISSUER (must be https without a trailing slash)")
    raw_lifetime = read.get(f"{ENV_PREFIX}{_HARNESS_LIFETIME_ENV}", "").strip()
    lifetime = _strict_positive_integer(raw_lifetime)
    if lifetime is None:
        problems.append(f"{ENV_PREFIX}{_HARNESS_LIFETIME_ENV} (must be a positive integer)")
    if problems or lifetime is None:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Token service configuration is incomplete.",
            context={"missing": sorted(problems)},
        )
    return TokenServiceSettings(
        environment=values["ENVIRONMENT"],
        aws_region=values["AWS_REGION"],
        cognito_user_pool_id=values["COGNITO_USER_POOL_ID"],
        cognito_client_id=values["COGNITO_CLIENT_ID"],
        token_issuer=issuer,
        token_signing_key_id=values["TOKEN_SIGNING_KEY_ID"],
        harness_token_audience=values["HARNESS_TOKEN_AUDIENCE"],
        route_client_id=values["ROUTE_CLIENT_ID"],
        harness_token_lifetime_seconds=lifetime,
        gateway_token_audience=values["GATEWAY_TOKEN_AUDIENCE"],
        agent_client_id=values["AGENT_CLIENT_ID"],
        agent_client_secret_id=values["AGENT_CLIENT_SECRET_ID"],
    )


@lru_cache(maxsize=1)
def token_service_settings() -> TokenServiceSettings:
    """Return the process-wide token service configuration, resolved at initialization."""
    return read_token_service_settings()


_KMS_KEY_ARN = re.compile(
    r"arn:aws[a-z-]*:kms:(?P<region>[a-z0-9-]+):\d{12}:key/(?P<key_id>[A-Za-z0-9-]+)"
)


@dataclass(frozen=True, slots=True)
class ToolCallerSettings:
    """What every Gateway tool needs to verify its caller (ADR-077 §5), and nothing more.

    Narrow like `TokenServiceSettings`: the presentation finalizer reads no other configuration,
    so it is not given the request-serving `Settings` to get these four values.
    """

    token_issuer: str
    token_signing_key_arn: str
    gateway_token_audience: str
    agent_client_id: str

    @property
    def token_signing_key_id(self) -> str:
        """Return the `kid` the issuer stamps: the KMS key id, the ARN's last path segment."""
        return self._key_arn_part("key_id")

    @property
    def token_signing_key_region(self) -> str:
        """Return the region the key lives in, which is where `GetPublicKey` must be called."""
        return self._key_arn_part("region")

    def _key_arn_part(self, name: str) -> str:
        match = _KMS_KEY_ARN.fullmatch(self.token_signing_key_arn)
        if match is None:  # pragma: no cover - refused by read_tool_caller_settings
            raise ValueError("token_signing_key_arn is not a KMS key ARN.")
        return match[name]


_TOOL_CALLER_ENV = (
    "TOKEN_ISSUER",
    "TOKEN_SIGNING_KEY_ARN",
    "GATEWAY_TOKEN_AUDIENCE",
    "AGENT_CLIENT_ID",
)


def read_tool_caller_settings(source: Mapping[str, str] | None = None) -> ToolCallerSettings:
    """Resolve a tool's caller-verification configuration, reporting every problem at once."""
    read: Mapping[str, str] = os.environ if source is None else source
    values = {name: read.get(f"{ENV_PREFIX}{name}", "").strip() for name in _TOOL_CALLER_ENV}
    problems = [f"{ENV_PREFIX}{name}" for name, value in values.items() if not value]
    issuer = values["TOKEN_ISSUER"]
    if issuer and (not issuer.startswith("https://") or issuer.endswith("/")):
        problems.append(f"{ENV_PREFIX}TOKEN_ISSUER (must be https without a trailing slash)")
    key_arn = values["TOKEN_SIGNING_KEY_ARN"]
    if key_arn and _KMS_KEY_ARN.fullmatch(key_arn) is None:
        problems.append(f"{ENV_PREFIX}TOKEN_SIGNING_KEY_ARN (must be a KMS key ARN)")
    if problems:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Tool caller verification configuration is incomplete.",
            context={"missing": sorted(problems)},
        )
    return ToolCallerSettings(
        token_issuer=issuer,
        token_signing_key_arn=key_arn,
        gateway_token_audience=values["GATEWAY_TOKEN_AUDIENCE"],
        agent_client_id=values["AGENT_CLIENT_ID"],
    )


@lru_cache(maxsize=1)
def tool_caller_settings() -> ToolCallerSettings:
    """Return the process-wide tool caller configuration, resolved at initialization."""
    return read_tool_caller_settings()


class Settings(BaseSettings):
    """Everything the backend needs from its environment."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        frozen=True,
        extra="ignore",
        case_sensitive=False,
    )

    environment: str = Field(min_length=1)
    aws_region: str = Field(min_length=1)
    cognito_user_pool_id: str = Field(min_length=1)
    cognito_client_id: str = Field(min_length=1)
    # The public PKCE client a bearer is verified against (ADR-078 §2). Required only by the
    # authorizer, which reads it through `authorizer_public_client_id()`.
    cognito_public_client_id: str = ""
    cognito_client_secret: SecretStr = SecretStr("")
    cognito_client_secret_id: str = ""
    cognito_login_domain: str = Field(min_length=1)
    app_base_url: str
    api_base_url: str
    cookie_scope: CookieScope
    cookie_domain: str = ""
    refresh_cookie_max_age_seconds: int = Field(gt=0)
    athena_workgroup: str = Field(min_length=1)
    page_context_table: str = ""
    preset_artifact_bucket: str = ""
    preset_artifact_pointer_table: str = ""
    insight_cache_table: str = ""
    insight_continuation_queue_url: str = ""
    insight_prompt_calibration_queue_url: str = ""
    insight_prompt_registry_table: str = ""
    insight_quick_model_id: str = ""
    insight_deep_model_id: str = ""
    # No product deadline is applied inside the request. Connect/read limits remain
    # separate transport failure boundaries; the Lambda/API envelope may end first.
    # The ceiling admits the continuation worker's patient 60s deep transport
    # (measured live 2026-09-24: Opus deep generations run 14-17s and can exceed
    # 20s on full evidence rosters); request-path deployments stay API-bounded.
    insight_generation_timeout_seconds: int = Field(default=20, ge=1, le=90)
    insight_generation_max_concurrency: int = Field(default=4, ge=1)
    # Concurrent Cognito AdminGetUser lookups per versions response (ADR-069). The ceiling keeps
    # one container's burst small against Cognito's per-account user-read rate; a throttle
    # degrades the author to unavailable rather than failing the response.
    operator_directory_max_concurrency: int = Field(default=4, ge=1, le=8)
    prompt_admin_route: str = ""
    # ADR-115 decision 8: whether this deployment provisions the operational status API, from the
    # same `config.operationalStatus.provisioned` value that creates its routes. `/me` grants
    # `can_read_operational_status` only where it's true, so Data pipeline status is listed only
    # where its API exists. Absent means not provisioned.
    operational_status_provisioned: bool = False
    measurement_function_arn: str = ""
    evaluation_queue_url: str = ""
    evaluation_outbox_table: str = ""
    agent_quality_audit_table: str = ""
    # ADR-081 §5: every record of a turn expires this many days after its receipt. Required by
    # the conversation route, which writes receipts; a reader never needs it.
    receipt_retention_days: int | None = Field(default=None, ge=1, le=MAX_RECORD_RETENTION_DAYS)
    agent_quality_findings_table: str = ""
    evaluation_deliveries_table: str = ""
    evaluation_writer_receipts_table: str = ""
    evaluation_verdicts_table: str = ""
    # How long after a dispatched receipt's decision the review route waits for a first verdict
    # before it reports the turn overdue. Required by the evaluation projection; other readers
    # never need it.
    evaluation_result_deadline_seconds: int | None = Field(default=None, ge=1)
    harness_arn: str = ""
    harness_prompt_digest: str = ""
    guardrail_id: str = ""
    guardrail_version: str = ""
    guardrails_enabled: bool = True
    quick_chat_max_tokens: int = 4096
    quick_chat_timeout_seconds: int = 120
    deep_dive_max_iterations: int = 12
    deep_dive_max_tokens: int = 16384
    deep_dive_timeout_seconds: int = 180
    token_route_exchange_function: str = ""
    semantic_attempts_table: str = Field(min_length=1)
    semantic_leases_table: str = Field(min_length=1)
    semantic_publications_table: str = Field(min_length=1)
    semantic_v2_bucket: str = ""
    semantic_v2_athena_bake_workgroup: str = ""
    semantic_v2_source_database: str = ""
    semantic_v2_candidate_database: str = ""
    semantic_v2_current_database: str = ""
    semantic_v2_staging_database: str = ""
    """ADR-149: blank by default; only the table writer and delivery runtimes require it."""
    semantic_v2_staging_bucket: str = ""
    """ADR-149: blank by default; only the table writer and delivery runtimes require it."""
    registration_status_table: str = ""
    raw_bucket: str = ""
    athena_result_reuse_max_age_minutes: int = Field(ge=0, le=MAX_RESULT_REUSE_MINUTES)
    athena_query_batch_max_concurrency: int = Field(ge=1)

    @field_validator("app_base_url", "api_base_url")
    @classmethod
    def _must_be_a_deployed_https_origin(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("must be an absolute https URL")
        host = value.removeprefix("https://").split("/", 1)[0].split(":", 1)[0]
        if host == "localhost" or host.endswith(".localhost") or host == "127.0.0.1":
            raise ValueError("resolved to localhost, which means configuration did not resolve")
        return value.rstrip("/")

    @field_validator("cognito_login_domain")
    @classmethod
    def _must_be_a_bare_host(cls, value: str) -> str:
        if "://" in value or "/" in value:
            raise ValueError("must be a bare host with no scheme or path")
        return value

    @model_validator(mode="after")
    def _cookie_scope_must_be_usable(self) -> Settings:
        try:
            CookiePolicy(scope=self.cookie_scope, domain=self.cookie_domain)
        except IdentityError as exc:
            raise ValueError(str(exc)) from exc
        return self

    @model_validator(mode="after")
    def _evaluation_tables_must_be_complete(self) -> Settings:
        """Require the evaluation record tables as one set.

        The evaluation record tables exist only where the evaluation plane is provisioned, so
        their joint absence is legitimate; a partial set means a deployment read the wrong
        parameters (ADR-080 §1).
        """
        evaluation = (
            self.evaluation_deliveries_table,
            self.evaluation_writer_receipts_table,
            self.evaluation_verdicts_table,
        )
        if any(evaluation) and not all(evaluation):
            raise ValueError("the evaluation record tables must be configured together")
        return self

    @property
    def cognito_issuer(self) -> str:
        """Return the issuer derived from the pool and configured region."""
        return cognito_issuer_for(self.aws_region, self.cognito_user_pool_id)

    @property
    def cognito_jwks_url(self) -> str:
        """Return the provider key set derived from the issuer."""
        return cognito_jwks_url_for(self.cognito_issuer)

    @property
    def cognito_token_url(self) -> str:
        """Return the managed-login token endpoint."""
        return f"https://{self.cognito_login_domain}/oauth2/token"

    @property
    def cognito_revoke_url(self) -> str:
        """Return the managed-login revocation endpoint."""
        return f"https://{self.cognito_login_domain}/oauth2/revoke"

    @property
    def cognito_authorize_url(self) -> str:
        """Return the managed-login authorization endpoint."""
        return f"https://{self.cognito_login_domain}/oauth2/authorize"

    @property
    def callback_redirect_uri(self) -> str:
        """Return the callback path derived from the public API origin."""
        return f"{self.api_base_url}{API_PREFIX}/auth/callback"

    @property
    def harness_read_timeout_seconds(self) -> int:
        """Derive the managed Harness wait limit from server-selected turn ceilings."""
        return max(self.quick_chat_timeout_seconds, self.deep_dive_timeout_seconds)

    @property
    def cookie_policy(self) -> CookiePolicy:
        """Return the validated cookie policy owned by the domain."""
        return CookiePolicy(scope=self.cookie_scope, domain=self.cookie_domain)


def load_settings(
    source: Mapping[str, str] | None = None, *, resolve_client_secret: bool = True
) -> Settings:
    """Build Settings from deployment environment or an explicit test mapping."""
    try:
        settings_value = (
            Settings()  # pyright: ignore[reportCallIssue] - fields resolve from environment
            if source is None
            else Settings.model_validate({_field_name(key): value for key, value in source.items()})
        )
    except ValidationError as error:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Configuration did not resolve.",
            context={"problems": _problems(error)},
        ) from None
    return _with_resolved_secret(settings_value) if resolve_client_secret else settings_value


def _with_resolved_secret(settings_value: Settings) -> Settings:
    """Resolve the BFF secret at initialization only for secret-owning containers."""
    if settings_value.cognito_client_secret.get_secret_value():
        return settings_value
    if not settings_value.cognito_client_secret_id:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Configuration did not resolve.",
            context={
                "problems": [
                    f"exactly one of {ENV_PREFIX}COGNITO_CLIENT_SECRET or "
                    f"{ENV_PREFIX}COGNITO_CLIENT_SECRET_ID is required"
                ]
            },
        )
    try:
        fetched = _fetch_secret(settings_value.cognito_client_secret_id)
    except Exception as error:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Configuration did not resolve.",
            context={
                "problems": ["the client secret could not be fetched"],
                "cause": type(error).__name__,
            },
        ) from None
    return settings_value.model_copy(update={"cognito_client_secret": SecretStr(fetched)})


def _fetch_secret(secret_id: str) -> str:
    """Read one configured secret using the typed boto3 client surface."""
    import boto3  # noqa: PLC0415 - deferred so unit paths never initialize an SDK client

    client = boto3.client("secretsmanager")
    return client.get_secret_value(SecretId=secret_id)["SecretString"]


def _field_name(key: str) -> str:
    """Translate explicit deployment-style environment keys to Settings field names."""
    return key.removeprefix(ENV_PREFIX).lower()


def _problems(error: ValidationError) -> list[str]:
    """Report validation locations and messages without echoing rejected values."""
    return sorted(
        f"{ENV_PREFIX}{'.'.join(str(part) for part in detail['loc']).upper()} ({detail['msg']})"
        for detail in error.errors()
    )


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Return process-wide Settings with the BFF secret resolved."""
    return load_settings()


@dataclass(frozen=True, slots=True)
class TracingSettings:
    """The tolerant tracing-only configuration that cannot affect availability."""

    enabled: bool
    aws_region: str
    environment: str
    sample_ratio: float

    @property
    def service_name(self) -> str:
        """Return the stable telemetry service identity."""
        return f"adlytics-{self.environment}"


def read_tracing_settings(source: Mapping[str, str] | None = None) -> TracingSettings:
    """Read tracing configuration with a safe untraced fallback."""
    read: Mapping[str, str] = os.environ if source is None else source
    enabled = read.get(f"{ENV_PREFIX}TRACING_ENABLED", "").strip().lower() == "true"
    try:
        ratio = float(read.get(f"{ENV_PREFIX}TRACE_SAMPLE_RATIO", "1.0"))
    except ValueError:
        ratio = 1.0
    if not 0.0 <= ratio <= 1.0:
        ratio = 1.0
    region = read.get(f"{ENV_PREFIX}AWS_REGION", "").strip()
    return TracingSettings(
        enabled=enabled and bool(region),
        aws_region=region,
        environment=read.get(f"{ENV_PREFIX}ENVIRONMENT", "unknown").strip() or "unknown",
        sample_ratio=ratio,
    )


@lru_cache(maxsize=1)
def tracing_settings() -> TracingSettings:
    """Return the process-wide tracing configuration."""
    return read_tracing_settings()


@lru_cache(maxsize=1)
def settings_without_client_secret() -> Settings:
    """Return process-wide Settings without resolving an unused BFF secret."""
    return load_settings(resolve_client_secret=False)


@lru_cache(maxsize=1)
def authorizer_public_client_id() -> str:
    """Return the public PKCE client id the authorizer verifies a bearer against (ADR-078 §2).

    Required by the authorizer alone, so it is resolved here rather than required of every
    container that builds Settings. A blank value refuses to start, never admits no bearer.
    """
    client_id = settings_without_client_secret().cognito_public_client_id.strip()
    if not client_id:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Configuration did not resolve.",
            context={"problems": [f"{ENV_PREFIX}COGNITO_PUBLIC_CLIENT_ID is required"]},
        )
    return client_id


@dataclass(frozen=True, slots=True)
class OriginVerifySettings:
    """Where the origin-verify values live and how long a container may hold them (ADR-078)."""

    secret_id: str
    max_cache_age_seconds: int
    reread_interval_seconds: int


_ORIGIN_VERIFY_KEYS = (
    "ORIGIN_VERIFY_SECRET_ID",
    "ORIGIN_VERIFY_MAX_CACHE_AGE_SECONDS",
    "ORIGIN_VERIFY_REREAD_INTERVAL_SECONDS",
)


def load_origin_verify_settings(source: Mapping[str, str] | None = None) -> OriginVerifySettings:
    """Read the origin-verify settings, refusing to start on any gap.

    Required by the authorizer and the auth handler alone, like the public client id. The re-read
    interval must be below the cache age, or a warm container would refuse a rotated value until
    its cache expired instead of re-reading it.
    """
    read: Mapping[str, str] = os.environ if source is None else source
    values = {key: read.get(f"{ENV_PREFIX}{key}", "").strip() for key in _ORIGIN_VERIFY_KEYS}
    problems = [f"{ENV_PREFIX}{key} is required" for key, value in values.items() if not value]
    age = _positive_seconds(values["ORIGIN_VERIFY_MAX_CACHE_AGE_SECONDS"])
    interval = _positive_seconds(values["ORIGIN_VERIFY_REREAD_INTERVAL_SECONDS"])
    if not problems and (age is None or interval is None or interval >= age):
        problems.append(
            f"{ENV_PREFIX}ORIGIN_VERIFY_REREAD_INTERVAL_SECONDS must be a positive whole number "
            f"of seconds below {ENV_PREFIX}ORIGIN_VERIFY_MAX_CACHE_AGE_SECONDS"
        )
    if problems or age is None or interval is None:
        raise InvariantError(
            CONFIG_UNRESOLVED, "Configuration did not resolve.", context={"problems": problems}
        )
    return OriginVerifySettings(
        secret_id=values["ORIGIN_VERIFY_SECRET_ID"],
        max_cache_age_seconds=age,
        reread_interval_seconds=interval,
    )


def _positive_seconds(raw: str) -> int | None:
    """Parse a positive whole number of seconds, or `None`; the raw value is never echoed."""
    return int(raw) if re.fullmatch(r"[1-9][0-9]{0,5}", raw) else None


@lru_cache(maxsize=1)
def origin_verify_settings() -> OriginVerifySettings:
    """Return the process-wide origin-verify settings."""
    return load_origin_verify_settings()


def read_origin_verify_secret(secret_id: str) -> str:
    """Read the origin-verify secret's current version; called at cold start and on re-read.

    On the authorizer's request path, whose timeout is 5 seconds, so the client is bounded well
    inside it: a slow read fails as a clean refusal, never an authorizer timeout.
    """
    response = origin_verify_client().get_secret_value(SecretId=secret_id)
    return response["SecretString"]


@lru_cache(maxsize=1)
def origin_verify_client() -> SecretsManagerClient:
    """One bounded Secrets Manager client per container.

    One attempt, never a retry: with 1 s connect and read timeouts the read ends within about
    2 s, leaving the rest of the authorizer's 5 s for credential verification. A failed read
    refuses, and the next request past the re-read interval tries again.
    """
    import boto3  # noqa: PLC0415 - deferred so unit paths never initialize an SDK client
    from botocore.config import Config  # noqa: PLC0415 - deferred with boto3

    bounded = Config(
        connect_timeout=1,
        read_timeout=1,
        retries={"mode": "standard", "total_max_attempts": 1},
    )
    return boto3.client("secretsmanager", config=bounded)


@dataclass(frozen=True, slots=True)
class QualityReviewEvaluationTables:
    """The three evaluation record tables and the result deadline, present only as one set."""

    deliveries_table: str
    writer_receipts_table: str
    verdicts_table: str
    result_deadline_seconds: int


@dataclass(frozen=True, slots=True)
class QualityReviewSettings:
    """Every record table the one review function reads (ADR-078 §5, ADR-080 §1)."""

    receipts_table: str
    findings_table: str
    evaluation: QualityReviewEvaluationTables | None
    """`None` only where the evaluation plane is not provisioned."""


def read_quality_review_settings(value: Settings) -> QualityReviewSettings:
    """Require the receipts and findings tables, and the evaluation set with its deadline.

    Without a deadline a turn no evaluator reports on would read as pending forever, so where
    the evaluation tables are configured the deadline is required before the function starts.
    """
    missing = [
        f"{ENV_PREFIX}{name}"
        for name, present in (
            ("AGENT_QUALITY_AUDIT_TABLE", value.agent_quality_audit_table),
            ("AGENT_QUALITY_FINDINGS_TABLE", value.agent_quality_findings_table),
        )
        if not present.strip()
    ]
    evaluation: QualityReviewEvaluationTables | None = None
    if value.evaluation_deliveries_table:
        if value.evaluation_result_deadline_seconds is None:
            missing.append(f"{ENV_PREFIX}EVALUATION_RESULT_DEADLINE_SECONDS")
        else:
            evaluation = QualityReviewEvaluationTables(
                deliveries_table=value.evaluation_deliveries_table,
                writer_receipts_table=value.evaluation_writer_receipts_table,
                verdicts_table=value.evaluation_verdicts_table,
                result_deadline_seconds=value.evaluation_result_deadline_seconds,
            )
    if missing:
        raise InvariantError(
            CONFIG_UNRESOLVED,
            "Configuration did not resolve.",
            context={
                "problems": [f"{name} is required by the quality review API" for name in missing]
            },
        )
    return QualityReviewSettings(
        receipts_table=value.agent_quality_audit_table,
        findings_table=value.agent_quality_findings_table,
        evaluation=evaluation,
    )


@lru_cache(maxsize=1)
def quality_review_settings() -> QualityReviewSettings:
    """Return the review function's record tables, resolved once at initialization."""
    return read_quality_review_settings(settings_without_client_secret())
