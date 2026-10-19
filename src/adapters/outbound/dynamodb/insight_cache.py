# SPDX-License-Identifier: MIT-0
# ruff: noqa: B904, D102, E501
"""Immutable owner-scoped derived insight cache; prompts live in the retained registry."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, cast

import boto3
from botocore.exceptions import ClientError

from domain.presentation.insights import (
    InsightCacheKey,
    InsightCacheWriteResult,
    InsightDeepAnalysis,
    InsightDeepCacheWriteResult,
    InsightDeepDiveState,
    InsightDeepRecord,
    InsightOutputClass,
    InsightPromptTier,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    next_step_from_payload,
    next_step_payload,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.insights import (
    InsightCachePort,
    InsightCacheSupersessionPort,
    InsightCacheSupersessionRequest,
    InsightCacheSupersessionResult,
    InsightDeepCachePort,
)

from .items import ItemError, read_text, write_int, write_text

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import (
        AttributeValueTypeDef,
        UpdateItemInputTypeDef,
    )

_PARTITION_KEY = "ownership_key"
_SORT_KEY = "content_key"
_RECORD_TYPE = "record_type"
_CACHE_RECORD = "INSIGHT_CACHE"
# Deep rows share the table and key space discipline (the tier discriminator lives in
# the content key) but are their own record type: their content shape and byte budget
# differ from card slots by design (ADR-076 decision 1).
_DEEP_CACHE_RECORD = "INSIGHT_DEEP_CACHE"
_ANALYSIS = "analysis"
_SLOT_ID = "slot_id"
_OUTPUT_CLASS = "output_class"
_STATE = "state"
_TEXT = "text"
_EVIDENCE_LINE = "evidence_line"
_NEXT_STEP = "next_step"
"""FR-11: present only on a row whose card offered a step; its campaign target holds the key."""
_ARTIFACT_REVISION = "artifact_revision"
_PROMPT_VERSION = "prompt_version"
_SUPERSEDED_AT = "superseded_at"
_UNAVAILABLE_REASON = "unavailable_reason"
MAX_UNAVAILABLE_REASON_CHARS = 128
_INSIGHT_CACHE_UNREADABLE = ErrorCode("INSIGHT", 1)
_INSIGHT_CACHE_UNWRITABLE = ErrorCode("INSIGHT", 2)
_INSIGHT_CACHE_MALFORMED = ErrorCode("INSIGHT", 3)


@dataclass(frozen=True, slots=True)
class InsightCacheDegradeRequest:
    """One exhausted generation's exact key, declared class, and bounded reason code."""

    key: InsightCacheKey
    output_class: InsightOutputClass
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip() or len(self.reason) > MAX_UNAVAILABLE_REASON_CHARS:
            raise ValueError("insight cache degrade reason must be nonblank and bounded.")


def _cache_item(key: InsightCacheKey, slot: InsightSlot) -> dict[str, AttributeValueTypeDef]:
    """Encode a current immutable record; only a future superseder may add TTL."""
    if (
        slot.id is not key.slot_id
        or slot.artifact_revision != key.artifact_revision
        or slot.prompt_version != key.prompt_version
    ):
        raise ValueError("cached insight provenance must match its immutable cache identity.")
    item: dict[str, AttributeValueTypeDef] = {
        _PARTITION_KEY: write_text(key.ownership_key),
        _SORT_KEY: write_text(key.content_key),
        _RECORD_TYPE: write_text(_CACHE_RECORD),
        _SLOT_ID: write_text(slot.id.value),
        _OUTPUT_CLASS: write_text(slot.output_class.value),
        _STATE: write_text(slot.state.value),
        _ARTIFACT_REVISION: write_text(slot.artifact_revision),
        _PROMPT_VERSION: write_text(slot.prompt_version),
    }
    if slot.text is not None:
        item[_TEXT] = write_text(slot.text)
    if slot.evidence_line is not None:
        item[_EVIDENCE_LINE] = write_text(slot.evidence_line)
    if slot.next_step is not None:
        item[_NEXT_STEP] = write_text(
            json.dumps(
                next_step_payload(slot.next_step, cached=True),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return item


def _supersession_update(
    table_name: str,
    request: InsightCacheSupersessionRequest,
) -> UpdateItemInputTypeDef:
    """Build the one exact-key conditional mutation that adds a numeric TTL."""
    stale_key = request.stale_key
    return {
        "TableName": table_name,
        "Key": {
            _PARTITION_KEY: write_text(stale_key.ownership_key),
            _SORT_KEY: write_text(stale_key.content_key),
        },
        "UpdateExpression": "SET #superseded_at = :superseded_at",
        "ConditionExpression": (
            "attribute_exists(#ownership_key) AND attribute_exists(#content_key) "
            "AND #record_type = :record_type AND attribute_not_exists(#superseded_at)"
        ),
        "ExpressionAttributeNames": {
            "#ownership_key": _PARTITION_KEY,
            "#content_key": _SORT_KEY,
            "#record_type": _RECORD_TYPE,
            "#superseded_at": _SUPERSEDED_AT,
        },
        "ExpressionAttributeValues": {
            ":record_type": write_text(_CACHE_RECORD),
            ":superseded_at": write_int(request.superseded_at),
        },
    }


def _deep_item(key: InsightCacheKey, record: InsightDeepRecord) -> dict[str, AttributeValueTypeDef]:
    """Encode one immutable deep-tier record; the key must carry the deep tier."""
    if (
        key.tier is not InsightPromptTier.DEEP
        or record.slot_id is not key.slot_id
        or record.artifact_revision != key.artifact_revision
        or record.prompt_version != key.prompt_version
    ):
        raise ValueError("cached deep provenance must match its immutable tier-bearing identity.")
    item: dict[str, AttributeValueTypeDef] = {
        _PARTITION_KEY: write_text(key.ownership_key),
        _SORT_KEY: write_text(key.content_key),
        _RECORD_TYPE: write_text(_DEEP_CACHE_RECORD),
        _SLOT_ID: write_text(record.slot_id.value),
        _STATE: write_text(record.state.value),
        _ARTIFACT_REVISION: write_text(record.artifact_revision),
        _PROMPT_VERSION: write_text(record.prompt_version),
    }
    if record.analysis is not None:
        item[_ANALYSIS] = write_text(
            json.dumps(
                {
                    "what_stands_out": record.analysis.what_stands_out,
                    "candidate_readings": list(record.analysis.candidate_readings),
                    "discriminating_evidence": record.analysis.discriminating_evidence,
                    "cited_figures": list(record.analysis.cited_figures),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    if record.absence_reason is not None:
        item[_UNAVAILABLE_REASON] = write_text(record.absence_reason)
    return item


def _deep_analysis_from_text(raw: str) -> InsightDeepAnalysis:
    payload: object = json.loads(raw)
    if not isinstance(payload, dict) or set(cast("dict[str, object]", payload)) != {
        "what_stands_out",
        "candidate_readings",
        "discriminating_evidence",
        "cited_figures",
    }:
        raise ValueError("stored deep analysis fields are not exact.")
    document = cast("dict[str, object]", payload)
    standout, readings, evidence, cited = (
        document["what_stands_out"],
        document["candidate_readings"],
        document["discriminating_evidence"],
        document["cited_figures"],
    )
    if (
        not isinstance(standout, str)
        or not isinstance(evidence, str)
        or not isinstance(readings, list)
        or not isinstance(cited, list)
        or not all(
            isinstance(value, str)
            for value in [*cast("list[object]", readings), *cast("list[object]", cited)]
        )
    ):
        raise ValueError("stored deep analysis fields are malformed.")
    return InsightDeepAnalysis(
        standout,
        tuple(cast("list[str]", readings)),
        evidence,
        tuple(cast("list[str]", cited)),
    )


def _deep_record(item: dict[str, AttributeValueTypeDef], key: InsightCacheKey) -> InsightDeepRecord:
    try:
        if (
            read_text(item, _PARTITION_KEY) != key.ownership_key
            or read_text(item, _SORT_KEY) != key.content_key
            or read_text(item, _RECORD_TYPE) != _DEEP_CACHE_RECORD
        ):
            raise ValueError("stored deep cache identity does not match the request.")
        analysis_text = item.get(_ANALYSIS, {}).get("S")
        record = InsightDeepRecord(
            slot_id=InsightSlotId(read_text(item, _SLOT_ID)),
            state=InsightDeepDiveState(read_text(item, _STATE)),
            analysis=None if analysis_text is None else _deep_analysis_from_text(analysis_text),
            absence_reason=item.get(_UNAVAILABLE_REASON, {}).get("S"),
            artifact_revision=read_text(item, _ARTIFACT_REVISION),
            prompt_version=read_text(item, _PROMPT_VERSION),
        )
        if (
            record.slot_id is not key.slot_id
            or record.artifact_revision != key.artifact_revision
            or record.prompt_version != key.prompt_version
        ):
            raise ValueError("stored deep provenance does not match the request.")
        return record
    except (ItemError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvariantError(
            _INSIGHT_CACHE_MALFORMED, "Insight deep cache entry is malformed."
        ) from error


def _slot(item: dict[str, AttributeValueTypeDef], key: InsightCacheKey) -> InsightSlot:
    try:
        if (
            read_text(item, _PARTITION_KEY) != key.ownership_key
            or read_text(item, _SORT_KEY) != key.content_key
            or read_text(item, _RECORD_TYPE) != _CACHE_RECORD
        ):
            raise ValueError("stored insight cache identity does not match the request.")
        slot = InsightSlot(
            id=InsightSlotId(read_text(item, _SLOT_ID)),
            output_class=InsightOutputClass(read_text(item, _OUTPUT_CLASS)),
            state=InsightSlotState(read_text(item, _STATE)),
            text=item.get(_TEXT, {}).get("S"),
            evidence_line=item.get(_EVIDENCE_LINE, {}).get("S"),
            artifact_revision=read_text(item, _ARTIFACT_REVISION),
            prompt_version=read_text(item, _PROMPT_VERSION),
            next_step=(
                None
                if (step := item.get(_NEXT_STEP, {}).get("S")) is None
                else next_step_from_payload(json.loads(step), cached=True)
            ),
        )
        if (
            slot.id is not key.slot_id
            or slot.artifact_revision != key.artifact_revision
            or slot.prompt_version != key.prompt_version
        ):
            raise ValueError("stored insight provenance does not match the request.")
        # `superseded_at` schedules physical retention cleanup; it never changes this row's
        # exact artifact+prompt identity. A still-open page may truthfully request the retained
        # revision, and deletion later becomes an ordinary first-fill miss.
        return slot
    except (ItemError, ValueError, json.JSONDecodeError) as error:
        raise InvariantError(
            _INSIGHT_CACHE_MALFORMED, "Insight cache entry is malformed."
        ) from error


class DynamoInsightCache(InsightCachePort, InsightDeepCachePort, InsightCacheSupersessionPort):
    """Read-through/write-once cache whose validity is its complete exact content key."""

    def __init__(
        self,
        table_name: str,
        client: DynamoDBClient | None = None,
    ) -> None:
        if not table_name.strip():
            raise ValueError("insight cache table name is required.")
        self._table = table_name
        self._client = client

    @property
    def _dynamodb(self) -> DynamoDBClient:
        if self._client is None:
            self._client = boto3.client("dynamodb")
        return self._client

    def get(self, key: InsightCacheKey) -> InsightSlot | None:
        try:
            response = self._dynamodb.get_item(
                TableName=self._table,
                Key={
                    _PARTITION_KEY: write_text(key.ownership_key),
                    _SORT_KEY: write_text(key.content_key),
                },
                ConsistentRead=True,
            )
        except Exception as error:
            self._read_error(error)
        item = response.get("Item")
        return None if item is None else _slot(dict(item), key)

    def put_if_absent(self, slot: InsightSlot, key: InsightCacheKey) -> InsightCacheWriteResult:
        try:
            self._dynamodb.put_item(
                TableName=self._table,
                Item=_cache_item(key, slot),
                ConditionExpression="attribute_not_exists(ownership_key) AND attribute_not_exists(content_key)",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                existing = self.get(key)
                if existing is None:
                    raise InvariantError(
                        _INSIGHT_CACHE_MALFORMED,
                        "Concurrent cache entry disappeared before it could be read.",
                    )
                if (
                    existing.state is InsightSlotState.UNAVAILABLE
                    and slot.state is not InsightSlotState.UNAVAILABLE
                ):
                    return self._replace_degraded(slot, key)
                return InsightCacheWriteResult(stored=False, slot=existing)
            self._write_error(error)
        except Exception as error:
            self._write_error(error)
        return InsightCacheWriteResult(stored=True, slot=slot)

    def _replace_degraded(self, slot: InsightSlot, key: InsightCacheKey) -> InsightCacheWriteResult:
        """Replace a retry-exhausted placeholder with a real generation, asserting its state.

        A degrade row is a placeholder, never a tombstone (ADR-068 amendment): the one
        replacement a key permits is unavailable → generated, conditioned on the exact
        degraded state this writer believes it is replacing so a racing successful fill
        keeps exactly one winner.
        """
        try:
            self._dynamodb.put_item(
                TableName=self._table,
                Item=_cache_item(key, slot),
                ConditionExpression=(
                    "attribute_exists(ownership_key) AND attribute_exists(content_key) "
                    "AND #state = :unavailable"
                ),
                ExpressionAttributeNames={"#state": _STATE},
                ExpressionAttributeValues={
                    ":unavailable": write_text(InsightSlotState.UNAVAILABLE.value)
                },
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                existing = self.get(key)
                if existing is None:
                    raise InvariantError(
                        _INSIGHT_CACHE_MALFORMED,
                        "Concurrent cache entry disappeared before it could be read.",
                    )
                return InsightCacheWriteResult(stored=False, slot=existing)
            self._write_error(error)
        except Exception as error:
            self._write_error(error)
        return InsightCacheWriteResult(stored=True, slot=slot)

    def put_unavailable_if_absent(
        self, request: InsightCacheDegradeRequest
    ) -> InsightCacheWriteResult:
        """Durably record one exhausted generation as a visible, diagnosable absence.

        The row asserts only its own exact key (create-if-absent, never a clobber of a
        filled winner) and touches no sibling revision: retention of obsolete rows stays
        the successful fill's concern. The bounded reason lives beside the row as a
        diagnostic attribute; the served slot shape is unchanged.
        """
        slot = InsightSlot(
            id=request.key.slot_id,
            output_class=request.output_class,
            state=InsightSlotState.UNAVAILABLE,
            text=None,
            evidence_line=None,
            artifact_revision=request.key.artifact_revision,
            prompt_version=request.key.prompt_version,
        )
        item = _cache_item(request.key, slot)
        item[_UNAVAILABLE_REASON] = write_text(request.reason)
        try:
            self._dynamodb.put_item(
                TableName=self._table,
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(ownership_key) AND attribute_not_exists(content_key)"
                ),
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                existing = self.get(request.key)
                if existing is None:
                    raise InvariantError(
                        _INSIGHT_CACHE_MALFORMED,
                        "Concurrent cache entry disappeared before it could be read.",
                    ) from None
                return InsightCacheWriteResult(stored=False, slot=existing)
            self._write_error(error)
        except Exception as error:
            self._write_error(error)
        return InsightCacheWriteResult(stored=True, slot=slot)

    def get_deep(self, key: InsightCacheKey) -> InsightDeepRecord | None:
        if key.tier is not InsightPromptTier.DEEP:
            raise ValueError("deep cache reads require a deep-tier identity.")
        try:
            response = self._dynamodb.get_item(
                TableName=self._table,
                Key={
                    _PARTITION_KEY: write_text(key.ownership_key),
                    _SORT_KEY: write_text(key.content_key),
                },
                ConsistentRead=True,
            )
        except Exception as error:
            self._read_error(error)
        item = response.get("Item")
        return None if item is None else _deep_record(dict(item), key)

    def put_deep_if_absent(
        self, record: InsightDeepRecord, key: InsightCacheKey
    ) -> InsightDeepCacheWriteResult:
        """Create one exact deep entry with the card tier's exact write discipline.

        Create-if-absent; the one permitted replacement is unavailable → filled,
        conditioned on the exact degraded state this writer believes it is replacing
        (a degrade row is a placeholder, never a tombstone).
        """
        try:
            self._dynamodb.put_item(
                TableName=self._table,
                Item=_deep_item(key, record),
                ConditionExpression="attribute_not_exists(ownership_key) AND attribute_not_exists(content_key)",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                existing = self.get_deep(key)
                if existing is None:
                    raise InvariantError(
                        _INSIGHT_CACHE_MALFORMED,
                        "Concurrent deep cache entry disappeared before it could be read.",
                    )
                if (
                    existing.state is InsightDeepDiveState.UNAVAILABLE
                    and record.state is InsightDeepDiveState.FILLED
                ):
                    return self._replace_degraded_deep(record, key)
                return InsightDeepCacheWriteResult(stored=False, record=existing)
            self._write_error(error)
        except Exception as error:
            self._write_error(error)
        return InsightDeepCacheWriteResult(stored=True, record=record)

    def _replace_degraded_deep(
        self, record: InsightDeepRecord, key: InsightCacheKey
    ) -> InsightDeepCacheWriteResult:
        try:
            self._dynamodb.put_item(
                TableName=self._table,
                Item=_deep_item(key, record),
                ConditionExpression=(
                    "attribute_exists(ownership_key) AND attribute_exists(content_key) "
                    "AND #state = :unavailable"
                ),
                ExpressionAttributeNames={"#state": _STATE},
                ExpressionAttributeValues={
                    ":unavailable": write_text(InsightDeepDiveState.UNAVAILABLE.value)
                },
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                existing = self.get_deep(key)
                if existing is None:
                    raise InvariantError(
                        _INSIGHT_CACHE_MALFORMED,
                        "Concurrent deep cache entry disappeared before it could be read.",
                    )
                return InsightDeepCacheWriteResult(stored=False, record=existing)
            self._write_error(error)
        except Exception as error:
            self._write_error(error)
        return InsightDeepCacheWriteResult(stored=True, record=record)

    def supersede(self, request: InsightCacheSupersessionRequest) -> InsightCacheSupersessionResult:
        """Mark one lifecycle-proven obsolete exact key for asynchronous TTL removal."""
        try:
            self._dynamodb.update_item(**_supersession_update(self._table, request))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return InsightCacheSupersessionResult(superseded=False)
            self._write_error(error)
        except Exception as error:
            self._write_error(error)
        return InsightCacheSupersessionResult(superseded=True)

    @staticmethod
    def _read_error(error: Exception) -> NoReturn:
        raise DependencyError(
            _INSIGHT_CACHE_UNREADABLE,
            "Insight cache data could not be read.",
            context={"cause": type(error).__name__},
        ) from None

    @staticmethod
    def _write_error(error: Exception) -> NoReturn:
        raise DependencyError(
            _INSIGHT_CACHE_UNWRITABLE,
            "Insight cache data could not be written.",
            context={"cause": type(error).__name__},
        ) from None
