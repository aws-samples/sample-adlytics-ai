# SPDX-License-Identifier: MIT-0
# ruff: noqa: PLR0911
"""Durable SQS admission and decoding for exact server-derived insight cache misses."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, cast

import boto3
from aws_lambda_powertools import Logger

from adapters.outbound.telemetry.reporting import Report, report_error
from domain.presentation.insights import (
    DeterministicFinding,
    InsightCacheKey,
    InsightContextEntry,
    InsightContinuationRequest,
    InsightGenerationRequest,
    InsightHorizonRole,
    InsightModelRoute,
    InsightNextStepKind,
    InsightNextStepOption,
    InsightOutputClass,
    InsightPromptActivation,
    InsightPromptTier,
    InsightSlotId,
    InsightWindowProfile,
)
from domain.presentation.next_steps import (
    next_step_target_from_payload,
    next_step_target_payload,
)
from domain.presentation.temporal import TemporalDisplayGrain
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.insights import InsightContinuationPort

if TYPE_CHECKING:
    from types_boto3_sqs import SQSClient

_CONTINUATION_UNWRITABLE = ErrorCode("INSIGHT", 10)
_CONTINUATION_INVALID = ErrorCode("INSIGHT", 11)
# Version 4 added the cache identity's tier discriminator (ADR-076 decision 1, slice G);
# version 3 added the typed window profile every payload declares (ADR-076 decision 5);
# version 2 added the display-context entries the ADR-068 context-depth amendment mandates.
# An in-flight prior-version envelope after a deploy is dropped, which degrades one slot to
# its absence state until the next publication re-bakes it — never a wrong narration.
_SCHEMA_VERSION = 4
_MAX_MESSAGE_BYTES = 256 * 1024
LOGGER = Logger(service="adlytics-insight-continuation")


class SqsInsightContinuation(InsightContinuationPort):
    """Admit an exact immutable cache fill without preserving browser or credential data."""

    def __init__(self, queue_url: str, client: SQSClient | None = None) -> None:
        if not queue_url.strip():
            raise ValueError("insight continuation queue URL is required.")
        self._queue_url = queue_url
        self._client = client

    @property
    def _sqs(self) -> SQSClient:
        if self._client is None:
            self._client = boto3.client("sqs")
        return self._client

    def enqueue(self, request: InsightContinuationRequest) -> str:
        """Acknowledge only after SQS has accepted the validated durable work envelope."""
        body = serialize(request)
        if len(body.encode("utf-8")) > _MAX_MESSAGE_BYTES:
            raise InvariantError(
                _CONTINUATION_INVALID, "Insight continuation exceeds queue capacity."
            )
        try:
            response = self._sqs.send_message(QueueUrl=self._queue_url, MessageBody=body)
            message_id = response.get("MessageId")
        except Exception as error:
            failure = DependencyError(
                _CONTINUATION_UNWRITABLE,
                "Insight continuation could not be durably admitted.",
                context={"cause": type(error).__name__},
            )
            report_error(
                failure,
                Report(operation="insight_continuation.enqueue", correlation_id="", logger=LOGGER),
            )
            raise failure from None
        if not message_id:
            failure = DependencyError(
                _CONTINUATION_UNWRITABLE,
                "Insight continuation admission returned no delivery reference.",
                context={"cause": "missing_message_id"},
            )
            report_error(
                failure,
                Report(operation="insight_continuation.enqueue", correlation_id="", logger=LOGGER),
            )
            raise failure
        return message_id


def serialize(request: InsightContinuationRequest) -> str:
    """Encode only validated cache, slot, findings, and immutable prompt identity data."""
    generation = request.generation
    key = generation.key
    document: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "key": {
            "analytical_scope_id": key.analytical_scope_id,
            "subject": key.subject,
            "window_start": key.window_start,
            "window_end": key.window_end,
            "artifact_revision": key.artifact_revision,
            "prompt_version": key.prompt_version,
            "slot_id": key.slot_id.value,
            "tier": key.tier.value,
            "input_digest": key.input_digest,
            "deep_input_revision": key.deep_input_revision,
            "filter_id": key.filter_id,
        },
        "output_class": generation.output_class.value,
        "purpose": generation.purpose,
        "context": [{"name": entry.name, "value": entry.value} for entry in generation.context],
        "findings": [
            {"name": finding.name, "value": str(finding.value)} for finding in generation.findings
        ],
        # FR-11: the served options travel with the request so a deferred card can resolve
        # its chosen step; the options entry in context is already covered by the digest.
        "next_step_options": [
            {
                "step_id": option.step_id,
                "kind": option.kind.value,
                "target": next_step_target_payload(option.target, cached=True),
                "label": option.label,
            }
            for option in generation.next_step_options
        ],
        "window_profile": {
            "days": generation.window_profile.days,
            "grain": generation.window_profile.grain.value,
            "prior_start": generation.window_profile.prior_start,
            "prior_end": generation.window_profile.prior_end,
            "role": generation.window_profile.role.value,
        },
    }
    try:
        return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise InvariantError(
            _CONTINUATION_INVALID, "Insight continuation is not serializable."
        ) from error


def parse(raw: object) -> InsightContinuationRequest | None:
    """Reject any queue payload that is not the exact bounded continuation envelope."""
    if not isinstance(raw, str):
        return None
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    document = cast("dict[str, object]", decoded)
    if set(document) != {
        "schema_version",
        "key",
        "output_class",
        "purpose",
        "context",
        "findings",
        "window_profile",
        "next_step_options",
    }:
        return None
    if document.get("schema_version") != _SCHEMA_VERSION:
        return None
    key = _key(document.get("key"))
    output_class = _enum(InsightOutputClass, document.get("output_class"))
    purpose = document.get("purpose")
    context = _context(document.get("context"))
    findings = _findings(document.get("findings"))
    window_profile = _window_profile(document.get("window_profile"))
    options = _next_step_options(document.get("next_step_options"))
    if options is None:
        return None
    if (
        key is None
        or output_class is None
        or not isinstance(purpose, str)
        or not purpose.strip()
        or findings is None
        or context is None
        or window_profile is None
    ):
        return None
    try:
        prompt = InsightPromptActivation(
            key.slot_id, key.prompt_version, "retained", InsightModelRoute.QUICK
        )
        return InsightContinuationRequest(
            InsightGenerationRequest(
                key, output_class, prompt, purpose, findings, window_profile, context, options
            )
        )
    except ValueError:
        return None


def _next_step_options(raw: object) -> tuple[InsightNextStepOption, ...] | None:
    if not isinstance(raw, list):
        return None
    options: list[InsightNextStepOption] = []
    for raw_item in cast("list[object]", raw):
        if not isinstance(raw_item, dict):
            return None
        item = cast("dict[str, object]", raw_item)
        if set(item) != {"step_id", "kind", "target", "label"}:
            return None
        step_id, kind, target, label = item["step_id"], item["kind"], item["target"], item["label"]
        if not isinstance(step_id, str) or not isinstance(kind, str) or not isinstance(label, str):
            return None
        try:
            step_kind = InsightNextStepKind(kind)
            options.append(
                InsightNextStepOption(
                    step_id,
                    step_kind,
                    next_step_target_from_payload(step_kind.value, target, cached=True),
                    label,
                )
            )
        except ValueError:
            return None
    return tuple(options)


def _window_profile(raw: object) -> InsightWindowProfile | None:
    if not isinstance(raw, dict):
        return None
    document = cast("dict[str, object]", raw)
    if set(document) != {"days", "grain", "prior_start", "prior_end", "role"}:
        return None
    days = document.get("days")
    grain = document.get("grain")
    role = document.get("role")
    prior_start = document.get("prior_start")
    prior_end = document.get("prior_end")
    if not isinstance(days, int) or isinstance(days, bool):
        return None
    if not isinstance(grain, str) or not isinstance(role, str):
        return None
    if prior_start is not None and not isinstance(prior_start, str):
        return None
    if prior_end is not None and not isinstance(prior_end, str):
        return None
    try:
        return InsightWindowProfile(
            days=days,
            grain=TemporalDisplayGrain(grain),
            prior_start=prior_start,
            prior_end=prior_end,
            role=InsightHorizonRole(role),
        )
    except ValueError:
        return None


def _key(raw: object) -> InsightCacheKey | None:
    if not isinstance(raw, dict):
        return None
    document = cast("dict[str, object]", raw)
    fields = {
        "analytical_scope_id",
        "subject",
        "window_start",
        "window_end",
        "artifact_revision",
        "prompt_version",
        "slot_id",
        "tier",
        "input_digest",
    }
    # A message enqueued before the input identity (2026-10-05) lacks both fields and is
    # refused here, then acknowledged without work: its slot stays pending until reopened.
    # One enqueued before the channel filter (ADR-127) has no `filter_id`: it read All channels.
    if set(document) - {"filter_id"} != fields | {"deep_input_revision"}:
        return None
    filter_id = document.get("filter_id", "all")
    if not isinstance(filter_id, str):
        return None
    try:
        values = {name: document[name] for name in fields}
        if any(not isinstance(value, str) for value in values.values()):
            return None
        revision = document["deep_input_revision"]
        if revision is not None and not isinstance(revision, str):
            return None
        return InsightCacheKey(
            cast("str", values["analytical_scope_id"]),
            cast("str", values["subject"]),
            cast("str", values["window_start"]),
            cast("str", values["window_end"]),
            cast("str", values["artifact_revision"]),
            cast("str", values["prompt_version"]),
            InsightSlotId(cast("str", values["slot_id"])),
            cast("str", values["input_digest"]),
            InsightPromptTier(cast("str", values["tier"])),
            revision,
            filter_id,
        )
    except ValueError:
        return None


def _context(raw: object) -> tuple[InsightContextEntry, ...] | None:
    if not isinstance(raw, list):
        return None
    entries: list[InsightContextEntry] = []
    for raw_item in cast("list[object]", raw):
        if not isinstance(raw_item, dict):
            return None
        item = cast("dict[str, object]", raw_item)
        if set(item) != {"name", "value"}:
            return None
        name, value = item.get("name"), item.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            return None
        try:
            entries.append(InsightContextEntry(name, value))
        except ValueError:
            return None
    return tuple(entries) if len({item.name for item in entries}) == len(entries) else None


def _findings(raw: object) -> tuple[DeterministicFinding, ...] | None:
    if not isinstance(raw, list) or not raw:
        return None
    findings: list[DeterministicFinding] = []
    for raw_item in cast("list[object]", raw):
        if not isinstance(raw_item, dict):
            return None
        item = cast("dict[str, object]", raw_item)
        if set(item) != {"name", "value"}:
            return None
        name, value = item.get("name"), item.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            return None
        try:
            findings.append(DeterministicFinding(name, Decimal(value)))
        except InvalidOperation, ValueError:
            return None
    return tuple(findings) if len({item.name for item in findings}) == len(findings) else None


def _enum[T: InsightOutputClass](enum: type[T], raw: object) -> T | None:
    if not isinstance(raw, str):
        return None
    try:
        return enum(raw)
    except ValueError:
        return None
