# SPDX-License-Identifier: MIT-0
# ruff: noqa: E501
"""Retained immutable DynamoDB registry for governed insight prompts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from adapters.outbound.telemetry.reporting import Report, report_error
from domain.presentation.insights import (
    DETERMINISTIC_SLOT_IDS,
    PROMPT_ACTIVATION_CALIBRATION_REQUIRED,
    InsightCalibrationCaseResult,
    InsightCalibrationClaimDisposition,
    InsightCalibrationKind,
    InsightCalibrationOutcome,
    InsightCalibrationResult,
    InsightClaimedPromptCalibrationRequest,
    InsightModelRoute,
    InsightPendingPromptVersion,
    InsightPromptActivation,
    InsightPromptActivationRequest,
    InsightPromptCalibrationClaimRequest,
    InsightPromptCalibrationClaimResult,
    InsightPromptCalibrationOutboxWork,
    InsightPromptCalibrationRequest,
    InsightPromptCalibrationWorkState,
    InsightPromptRetirementRequest,
    InsightPromptSeedRequest,
    InsightPromptSlotState,
    InsightPromptTier,
    InsightPromptVersionCreateRequest,
    InsightPromptVersionRecord,
    InsightSlotId,
)
from domain.presentation.prompt_authorship import (
    OperatorPromptAuthor,
    PromptVersionAuthor,
    SystemPromptAuthor,
)
from domain.presentation.prompt_composition import PromptFragmentDigests
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.insights import InsightPromptRegistryPort

from .items import ItemError, read_text, write_text

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import (
        AttributeValueTypeDef,
        QueryInputTypeDef,
        TransactWriteItemTypeDef,
    )

_SLOT_KEY = "slot_key"
_RECORD_KEY = "record_key"
_RECORD_TYPE = "record_type"
_VERSION = "version"
_PROMPT = "prompt"
_MODEL_ROUTE = "model_route"
_CALIBRATION = "calibration"
_OUTCOME = "outcome"
_CALIBRATION_KIND = "calibration_kind"
_CALIBRATION_LEASE_REF = "calibration_lease_ref"
_CALIBRATION_LEASE_EXPIRES = "calibration_lease_expires_at"
_ACTIVE_VERSION = "active_version"
_CREATED_AT = "created_at"
_OPERATOR = "operator"
_AUTHOR_KIND = "author_kind"
_SYSTEM_AUTHOR = "system_author"
_OPERATOR_AUTHOR_KIND = "operator"
_SYSTEM_AUTHOR_KIND = "system"
_FROM_VERSION = "from_version"
_TO_VERSION = "to_version"
_EVENT_ACTION = "event_action"
_POINTER = "POINTER"
# The card tier keeps the retained bare POINTER key so every pre-tier record stays
# readable unmigrated (ADR-076 slice D); the deep tier's pointer is its own item.
_DEEP_POINTER = "POINTER#deep"
_TIER = "tier"
_FRAGMENT_DIGESTS = "fragment_digests"
_FRAGMENT_DIGEST = "fragment_digest"
_COUNTER = "COUNTER"
_VERSION_PREFIX = "VERSION#"
_CALIBRATION_RESULT_PREFIX = "CALIBRATION#"
_CALIBRATION_CASE_SEGMENT = "#CASE#"
_CALIBRATION_OUTBOX_PREFIX = "CALIBRATION_OUTBOX#"
_EVENT_PREFIX = "EVENT#"
_PASSED = InsightCalibrationOutcome.PASSED.value
_PENDING = "pending"
_VERSION_RECORD = "PROMPT_VERSION"
_POINTER_RECORD = "PROMPT_POINTER"
_EVENT_RECORD = "PROMPT_LIFECYCLE_EVENT"
_COUNTER_RECORD = "PROMPT_COUNTER"
_CALIBRATION_RESULT_RECORD = "PROMPT_CALIBRATION_RESULT"
_CALIBRATION_CASE_RECORD = "PROMPT_CALIBRATION_CASE"
_CALIBRATION_OUTBOX_RECORD = "PROMPT_CALIBRATION_OUTBOX"
_CONDITIONAL = "ConditionalCheckFailedException"
_TRANSACTION_CANCELLED = "TransactionCanceledException"
_REGISTRY_UNREADABLE = ErrorCode("INSIGHT", 6)
_REGISTRY_UNWRITABLE = ErrorCode("INSIGHT", 7)
_REGISTRY_CONFLICT = ErrorCode("INSIGHT", 8)
_REGISTRY_MALFORMED = ErrorCode("INSIGHT", 9)
PROMPT_REGISTRY_MALFORMED = _REGISTRY_MALFORMED
"""A retained registry record that cannot be decoded; the prompt-admin route answers 503."""
_VERSION_REPORT = Report(
    operation="insight_prompt_registry.version",
    correlation_id="",
    logger=Logger(service="adlytics-insight-prompt-registry"),
)


def _slot(slot_id: InsightSlotId) -> str:
    return f"SLOT#{slot_id.value}"


def _pointer_key(tier: InsightPromptTier) -> str:
    return _POINTER if tier is InsightPromptTier.CARD else _DEEP_POINTER


def _authorship_attributes(author: PromptVersionAuthor) -> dict[str, AttributeValueTypeDef]:
    """Encode the version row's author; the subject is opaque and never an email address."""
    if isinstance(author, SystemPromptAuthor):
        return {
            _AUTHOR_KIND: write_text(_SYSTEM_AUTHOR_KIND),
            _SYSTEM_AUTHOR: write_text(author.value),
        }
    return {
        _AUTHOR_KIND: write_text(_OPERATOR_AUTHOR_KIND),
        _OPERATOR: write_text(author.subject),
    }


def _item_authorship(
    item: dict[str, AttributeValueTypeDef],
) -> tuple[PromptVersionAuthor | None, datetime | None]:
    """Decode a version row's author and time: exactly an operator row, a system row, or legacy.

    A legacy row (written before 2026-10-01) carries none of the four attributes. Anything else
    raises, and the caller reports the record as malformed.
    """
    present = {
        name for name in (_AUTHOR_KIND, _OPERATOR, _SYSTEM_AUTHOR, _CREATED_AT) if name in item
    }
    if not present:
        return None, None
    kind = read_text(item, _AUTHOR_KIND)
    author: PromptVersionAuthor
    if kind == _OPERATOR_AUTHOR_KIND and present == {_AUTHOR_KIND, _OPERATOR, _CREATED_AT}:
        author = OperatorPromptAuthor(read_text(item, _OPERATOR))
    elif kind == _SYSTEM_AUTHOR_KIND and present == {_AUTHOR_KIND, _SYSTEM_AUTHOR, _CREATED_AT}:
        author = SystemPromptAuthor(read_text(item, _SYSTEM_AUTHOR))
    else:
        raise ValueError("prompt version authorship is partial or of an unknown kind.")
    created_at = datetime.fromisoformat(read_text(item, _CREATED_AT))
    if created_at.tzinfo is None:
        raise ValueError("prompt version creation time is not timezone-aware.")
    return author, created_at


def _item_tier(item: dict[str, AttributeValueTypeDef]) -> InsightPromptTier:
    """Read a record's tier; an absent attribute is a retained pre-tier card record."""
    value = item.get(_TIER, {}).get("S")
    if value is None:
        return InsightPromptTier.CARD
    return InsightPromptTier(value)


def _item_fragment_digests(
    item: dict[str, AttributeValueTypeDef],
) -> PromptFragmentDigests | None:
    """Read a composed version's digest map; absence reads as unsourced."""
    raw = item.get(_FRAGMENT_DIGESTS, {}).get("M")
    if raw is None:
        return None
    values = {name: value.get("S") for name, value in raw.items()}
    foundation, tier_core, slot_block = (
        values.get("foundation"),
        values.get("tier_core"),
        values.get("slot_block"),
    )
    if (
        set(values) != {"foundation", "tier_core", "slot_block"}
        or not isinstance(foundation, str)
        or not isinstance(tier_core, str)
        or not isinstance(slot_block, str)
    ):
        raise ValueError("registry fragment digests are malformed.")
    return PromptFragmentDigests(foundation, tier_core, slot_block)


def _version_key(version: int) -> str:
    return f"{_VERSION_PREFIX}{version}"


def _calibration_result_key(version: int) -> str:
    return f"{_CALIBRATION_RESULT_PREFIX}{version}"


def _calibration_case_key(version: int, kind: str) -> str:
    return f"{_calibration_result_key(version)}{_CALIBRATION_CASE_SEGMENT}{kind}"


def _calibration_outbox_key(version: int) -> str:
    return f"{_CALIBRATION_OUTBOX_PREFIX}{version}"


def _number(value: int) -> AttributeValueTypeDef:
    return {"N": str(value)}


def _read_number(item: dict[str, AttributeValueTypeDef], key: str) -> int:
    value = item.get(key, {}).get("N")
    if not isinstance(value, str) or not value.isdigit() or int(value) < 1:
        raise ValueError(f"registry item has invalid {key}.")
    return int(value)


@dataclass(frozen=True, slots=True)
class _PointerRecord:
    slot_id: InsightSlotId
    version: int
    prompt: str
    model_route: InsightModelRoute
    tier: InsightPromptTier = InsightPromptTier.CARD
    fragment_digest: str | None = None


@dataclass(frozen=True, slots=True)
class _LifecycleEvent:
    request: InsightPromptActivationRequest | InsightPromptRetirementRequest
    from_version: int | None
    to_version: int | None
    action: str


class DynamoInsightPromptRegistry(InsightPromptRegistryPort):
    """Read active prompts and manage immutable version, calibration, and lifecycle records."""

    def __init__(self, table_name: str, client: DynamoDBClient | None = None) -> None:
        if not table_name.strip():
            raise ValueError("insight prompt registry table name is required.")
        self._table = table_name
        self._client = client

    @property
    def _dynamodb(self) -> DynamoDBClient:
        if self._client is None:
            self._client = boto3.client("dynamodb")
        return self._client

    def active(
        self, slot_id: InsightSlotId, tier: InsightPromptTier
    ) -> InsightPromptActivation | None:
        """Read one tier's replicated active-pointer record; movers have no pointer."""
        if slot_id is InsightSlotId.MOVERS:
            return None
        try:
            response = self._dynamodb.get_item(
                TableName=self._table,
                Key={
                    _SLOT_KEY: write_text(_slot(slot_id)),
                    _RECORD_KEY: write_text(_pointer_key(tier)),
                },
                ConsistentRead=True,
            )
        except Exception as error:
            self._read_error(error)
        item = response.get("Item")
        if item is None:
            return None
        try:
            stored = dict(item)
            if (
                read_text(stored, _RECORD_TYPE) != _POINTER_RECORD
                or InsightSlotId(read_text(stored, "slot_id")) is not slot_id
                or _item_tier(stored) is not tier
            ):
                raise ValueError("active pointer identity is invalid.")
            digest = stored.get(_FRAGMENT_DIGEST, {}).get("S")
            return InsightPromptActivation(
                slot_id,
                f"v{_read_number(stored, _ACTIVE_VERSION)}",
                read_text(stored, _PROMPT),
                InsightModelRoute(read_text(stored, _MODEL_ROUTE)),
                fragment_digest=digest,
            )
        except (ItemError, ValueError) as error:
            raise InvariantError(
                _REGISTRY_MALFORMED, "Insight prompt pointer is malformed."
            ) from error

    def seed_v1(self, request: InsightPromptSeedRequest) -> None:
        """Idempotently install a reviewed v1 and its active pointer; never seed movers.

        The v1 records the deployment seed as its author and the run's start as its time.
        """
        slot_id, prompt, model_route = request.slot_id, request.prompt, request.model_route
        if slot_id is InsightSlotId.MOVERS:
            return
        slot = _slot(slot_id)
        calibration = InsightCalibrationResult.passed_result()
        version_request = InsightPromptVersionCreateRequest(
            slot_id,
            prompt,
            model_route,
            author=SystemPromptAuthor.CONTRACT_SEED,
            created_at=request.created_at,
        )
        item = self._version_item(version_request, 1, _PASSED)
        pointer = self._pointer_item(_PointerRecord(slot_id, 1, prompt, model_route))
        counter = {
            _SLOT_KEY: write_text(slot),
            _RECORD_KEY: write_text(_COUNTER),
            _RECORD_TYPE: write_text(_COUNTER_RECORD),
            _VERSION: _number(1),
        }
        try:
            transaction_items: list[TransactWriteItemTypeDef] = [
                self._conditional_put(item),
                self._conditional_put(pointer),
                self._conditional_put(counter),
                *(
                    self._conditional_put(record)
                    for record in self._calibration_items(slot_id, 1, calibration)
                ),
            ]
            self._dynamodb.transact_write_items(TransactItems=transaction_items)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {
                _CONDITIONAL,
                _TRANSACTION_CANCELLED,
            }:
                self._write_error(error)
            existing = self.active(slot_id, InsightPromptTier.CARD)
            if (
                existing is not None
                and existing.version == "v1"
                and existing.prompt == prompt
                and existing.model_route is model_route
            ):
                return
            raise InvariantError(
                _REGISTRY_MALFORMED, "Prompt registry seed conflicts with retained state."
            ) from None
        except Exception as error:
            self._write_error(error)

    def create_version(self, request: InsightPromptVersionCreateRequest) -> int:
        """Reserve the next dense server-assigned version with an optimistic counter condition."""
        counter = self._get_counter(request.slot_id)
        next_version = counter + 1
        try:
            # One conditional Update owns both the optimistic check and the advance: a
            # transaction cannot include two operations on one item, so the counter's
            # condition lives in its own UpdateExpression rather than a ConditionCheck.
            transaction_items: list[TransactWriteItemTypeDef] = [
                self._conditional_put(self._version_item(request, next_version, _PENDING)),
                self._conditional_put(self._calibration_outbox_item(request.slot_id, next_version)),
                {
                    "Update": {
                        "TableName": self._table,
                        "Key": {
                            _SLOT_KEY: write_text(_slot(request.slot_id)),
                            _RECORD_KEY: write_text(_COUNTER),
                        },
                        "UpdateExpression": "SET #version = :next",
                        "ConditionExpression": "#version = :expected",
                        "ExpressionAttributeNames": {"#version": _VERSION},
                        "ExpressionAttributeValues": {
                            ":expected": _number(counter),
                            ":next": _number(next_version),
                        },
                    }
                },
            ]
            self._dynamodb.transact_write_items(TransactItems=transaction_items)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {
                _CONDITIONAL,
                _TRANSACTION_CANCELLED,
            }:
                self._conflict("Prompt version creation conflicted; reload and retry.")
            self._write_error(error)
        return next_version

    def pending_version(self, slot_id: InsightSlotId, version: int) -> InsightPendingPromptVersion:
        """Read one pending version's immutable prompt and route for server-run calibration."""
        item = self._read_version(slot_id, version)
        try:
            return self._pending_from_item(slot_id, version, item)
        except (ItemError, ValueError) as error:
            raise InvariantError(
                _REGISTRY_MALFORMED, "Prompt registry pending version is malformed."
            ) from error

    def pending_calibration_outbox(
        self, slot_ids: tuple[InsightSlotId, ...]
    ) -> tuple[InsightPromptCalibrationOutboxWork, ...]:
        """Read every retained work identity not yet accepted by SQS."""
        found: list[InsightPromptCalibrationOutboxWork] = []
        for slot_id in slot_ids:
            if slot_id in DETERMINISTIC_SLOT_IDS:
                continue
            for item in self._query_all(
                {
                    "TableName": self._table,
                    "KeyConditionExpression": (
                        "#slot_key = :slot_key AND begins_with(#record_key, :prefix)"
                    ),
                    "ExpressionAttributeNames": {
                        "#slot_key": _SLOT_KEY,
                        "#record_key": _RECORD_KEY,
                    },
                    "ExpressionAttributeValues": {
                        ":slot_key": write_text(_slot(slot_id)),
                        ":prefix": write_text(_CALIBRATION_OUTBOX_PREFIX),
                    },
                    "ConsistentRead": True,
                }
            ):
                try:
                    if (
                        read_text(item, _RECORD_TYPE) != _CALIBRATION_OUTBOX_RECORD
                        or InsightSlotId(read_text(item, "slot_id")) is not slot_id
                    ):
                        raise ValueError("calibration outbox identity is invalid.")
                    found.append(
                        InsightPromptCalibrationOutboxWork(slot_id, _read_number(item, _VERSION))
                    )
                except (ItemError, ValueError) as error:
                    raise InvariantError(
                        _REGISTRY_MALFORMED, "Prompt calibration outbox is malformed."
                    ) from error
        return tuple(sorted(found, key=lambda item: (item.slot_id.value, item.version)))

    def acknowledge_calibration_outbox(self, slot_id: InsightSlotId, version: int) -> None:
        """Delete exact retained work only after its identity was accepted by SQS."""
        key = {
            _SLOT_KEY: write_text(_slot(slot_id)),
            _RECORD_KEY: write_text(_calibration_outbox_key(version)),
        }
        try:
            self._dynamodb.delete_item(
                TableName=self._table,
                Key=key,
                ConditionExpression=(
                    "#record_type = :record_type AND #slot_id = :slot_id AND #version = :version"
                ),
                ExpressionAttributeNames={
                    "#record_type": _RECORD_TYPE,
                    "#slot_id": "slot_id",
                    "#version": _VERSION,
                },
                ExpressionAttributeValues={
                    ":record_type": write_text(_CALIBRATION_OUTBOX_RECORD),
                    ":slot_id": write_text(slot_id.value),
                    ":version": _number(version),
                },
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != _CONDITIONAL:
                self._write_error(error)
            # A concurrent dispatcher already acknowledged the same work: exact absence
            # is convergence, while a surviving malformed/different item is a conflict.
            try:
                response = self._dynamodb.get_item(
                    TableName=self._table, Key=key, ConsistentRead=True
                )
            except Exception as read_error:
                self._read_error(read_error)
            if response.get("Item") is None:
                return
            self._conflict("Prompt calibration outbox acknowledgement conflicted.")

    def calibration_work_state(
        self, slot_id: InsightSlotId, version: int, now_epoch_seconds: int
    ) -> InsightPromptCalibrationWorkState:
        """Read whether recovery should enqueue without claiming the pending version."""
        if now_epoch_seconds < 1:
            raise ValueError("prompt calibration state read requires a positive epoch second.")
        return self._work_state(self._read_version(slot_id, version), now_epoch_seconds)

    def claim_calibration(
        self, request: InsightPromptCalibrationClaimRequest
    ) -> InsightPromptCalibrationClaimResult:
        """Lease one pending version; concurrent or terminal redelivery performs no model work."""
        try:
            response = self._dynamodb.update_item(
                TableName=self._table,
                Key={
                    _SLOT_KEY: write_text(_slot(request.slot_id)),
                    _RECORD_KEY: write_text(_version_key(request.version)),
                },
                UpdateExpression="SET #lease = :lease, #lease_expires = :lease_expires",
                ConditionExpression=(
                    "#record_type = :record_type AND #slot_id = :slot_id "
                    "AND #version = :version AND #calibration = :pending "
                    "AND (attribute_not_exists(#lease) OR #lease_expires <= :now)"
                ),
                ExpressionAttributeNames={
                    "#record_type": _RECORD_TYPE,
                    "#slot_id": "slot_id",
                    "#version": _VERSION,
                    "#calibration": _CALIBRATION,
                    "#lease": _CALIBRATION_LEASE_REF,
                    "#lease_expires": _CALIBRATION_LEASE_EXPIRES,
                },
                ExpressionAttributeValues={
                    ":record_type": write_text(_VERSION_RECORD),
                    ":slot_id": write_text(request.slot_id.value),
                    ":version": _number(request.version),
                    ":pending": write_text(_PENDING),
                    ":lease": write_text(request.lease_ref),
                    ":lease_expires": _number(request.lease_expires_epoch_seconds),
                    ":now": _number(request.now_epoch_seconds),
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != _CONDITIONAL:
                self._write_error(error)
            state = self.calibration_work_state(
                request.slot_id, request.version, request.now_epoch_seconds
            )
            disposition = (
                InsightCalibrationClaimDisposition.TERMINAL
                if state.calibration != _PENDING
                else InsightCalibrationClaimDisposition.ACTIVE_LEASE
            )
            return InsightPromptCalibrationClaimResult(disposition)
        item = response.get("Attributes")
        if not item:
            raise InvariantError(
                _REGISTRY_MALFORMED, "Prompt calibration claim returned no version."
            )
        try:
            pending = self._pending_from_item(request.slot_id, request.version, dict(item))
        except (ItemError, ValueError) as error:
            raise InvariantError(
                _REGISTRY_MALFORMED, "Claimed prompt calibration is malformed."
            ) from error
        return InsightPromptCalibrationClaimResult(
            InsightCalibrationClaimDisposition.CLAIMED, pending
        )

    def complete_claimed_calibration(self, request: InsightClaimedPromptCalibrationRequest) -> None:
        """Persist all outcomes and clear only the worker's exact lease in one transaction."""
        slot = _slot(request.slot_id)
        try:
            transaction_items: list[TransactWriteItemTypeDef] = [
                {
                    "Update": {
                        "TableName": self._table,
                        "Key": {
                            _SLOT_KEY: write_text(slot),
                            _RECORD_KEY: write_text(_version_key(request.version)),
                        },
                        "UpdateExpression": (
                            "SET #calibration = :outcome REMOVE #lease, #lease_expires"
                        ),
                        "ConditionExpression": (
                            "#record_type = :record_type AND #slot_id = :slot_id "
                            "AND #version = :version AND #calibration = :pending "
                            "AND #lease = :lease AND #lease_expires > :completed_at"
                        ),
                        "ExpressionAttributeNames": {
                            "#record_type": _RECORD_TYPE,
                            "#slot_id": "slot_id",
                            "#version": _VERSION,
                            "#calibration": _CALIBRATION,
                            "#lease": _CALIBRATION_LEASE_REF,
                            "#lease_expires": _CALIBRATION_LEASE_EXPIRES,
                        },
                        "ExpressionAttributeValues": {
                            ":record_type": write_text(_VERSION_RECORD),
                            ":slot_id": write_text(request.slot_id.value),
                            ":version": _number(request.version),
                            ":pending": write_text(_PENDING),
                            ":lease": write_text(request.lease_ref),
                            ":completed_at": _number(request.completed_at_epoch_seconds),
                            ":outcome": write_text(request.result.outcome.value),
                        },
                    }
                },
                *(
                    self._conditional_put(item)
                    for item in self._calibration_items(
                        request.slot_id, request.version, request.result
                    )
                ),
            ]
            self._dynamodb.transact_write_items(TransactItems=transaction_items)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {
                _CONDITIONAL,
                _TRANSACTION_CANCELLED,
            }:
                self._conflict("Prompt calibration lease was lost before completion.")
            self._write_error(error)

    def record_calibration(self, request: InsightPromptCalibrationRequest) -> None:
        """Persist all case outcomes and transition a pending immutable version exactly once."""
        slot = _slot(request.slot_id)
        try:
            transaction_items: list[TransactWriteItemTypeDef] = [
                {
                    "Update": {
                        "TableName": self._table,
                        "Key": {
                            _SLOT_KEY: write_text(slot),
                            _RECORD_KEY: write_text(_version_key(request.version)),
                        },
                        "UpdateExpression": "SET #calibration = :outcome",
                        "ConditionExpression": (
                            "#record_type = :record_type AND #slot_id = :slot_id "
                            "AND #version = :version AND #calibration = :pending"
                        ),
                        "ExpressionAttributeNames": {
                            "#record_type": _RECORD_TYPE,
                            "#slot_id": "slot_id",
                            "#version": _VERSION,
                            "#calibration": _CALIBRATION,
                        },
                        "ExpressionAttributeValues": {
                            ":record_type": write_text(_VERSION_RECORD),
                            ":slot_id": write_text(request.slot_id.value),
                            ":version": _number(request.version),
                            ":pending": write_text(_PENDING),
                            ":outcome": write_text(request.result.outcome.value),
                        },
                    }
                },
                *(
                    self._conditional_put(item)
                    for item in self._calibration_items(
                        request.slot_id, request.version, request.result
                    )
                ),
            ]
            self._dynamodb.transact_write_items(TransactItems=transaction_items)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {
                _CONDITIONAL,
                _TRANSACTION_CANCELLED,
            }:
                self._conflict(
                    "Prompt calibration result already exists or its version is missing."
                )
            self._write_error(error)

    def activate(self, request: InsightPromptActivationRequest) -> None:
        """Atomically move a stored-passed version pointer and append an immutable activation event.

        The tier is derived from the named version record — never from the request —
        so an activation can only ever move the pointer of the tier the version
        belongs to (ADR-076 slice D).
        """
        version_item = self._read_version(request.slot_id, request.version)
        prompt = read_text(version_item, _PROMPT)
        try:
            model_route = InsightModelRoute(read_text(version_item, _MODEL_ROUTE))
            tier = _item_tier(version_item)
            digests = _item_fragment_digests(version_item)
        except (ItemError, ValueError) as error:
            raise InvariantError(
                _REGISTRY_MALFORMED, "Prompt registry version is malformed."
            ) from error
        self._require_passed_calibration(request.slot_id, request.version)
        condition = (
            "attribute_not_exists(record_key)"
            if request.expected_active_version is None
            else "active_version = :expected"
        )
        slot = _slot(request.slot_id)
        # Only the optimistic-lock value may appear: DynamoDB refuses an expression
        # attribute value that no expression uses.
        pointer_put: TransactWriteItemTypeDef = {
            "Put": {
                "TableName": self._table,
                "Item": self._pointer_item(
                    _PointerRecord(
                        request.slot_id,
                        request.version,
                        prompt,
                        model_route,
                        tier=tier,
                        fragment_digest=None if digests is None else digests.combined,
                    )
                ),
                "ConditionExpression": condition,
            }
        }
        if request.expected_active_version is not None:
            pointer_put["Put"]["ExpressionAttributeValues"] = {
                ":expected": _number(request.expected_active_version)
            }
        try:
            transaction_items: list[TransactWriteItemTypeDef] = [
                {
                    "ConditionCheck": {
                        "TableName": self._table,
                        "Key": {
                            _SLOT_KEY: write_text(slot),
                            _RECORD_KEY: write_text(_calibration_result_key(request.version)),
                        },
                        "ConditionExpression": "outcome = :passed",
                        "ExpressionAttributeValues": {":passed": write_text(_PASSED)},
                    }
                },
                pointer_put,
                self._conditional_put(
                    self._event_item(
                        _LifecycleEvent(
                            request,
                            request.expected_active_version,
                            request.version,
                            "activated",
                        )
                    )
                ),
            ]
            self._dynamodb.transact_write_items(TransactItems=transaction_items)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {
                _CONDITIONAL,
                _TRANSACTION_CANCELLED,
            }:
                self._conflict("Prompt activation conflicted; reload the active version.")
            self._write_error(error)

    def retire(self, request: InsightPromptRetirementRequest) -> None:
        """Conditionally clear the active pointer and retain one immutable retirement event."""
        slot = _slot(request.slot_id)
        try:
            transaction_items: list[TransactWriteItemTypeDef] = [
                {
                    "Delete": {
                        "TableName": self._table,
                        "Key": {
                            _SLOT_KEY: write_text(slot),
                            _RECORD_KEY: write_text(_pointer_key(request.tier)),
                        },
                        "ConditionExpression": "active_version = :expected",
                        "ExpressionAttributeValues": {
                            ":expected": _number(request.expected_active_version)
                        },
                    }
                },
                self._conditional_put(
                    self._event_item(
                        _LifecycleEvent(
                            request,
                            request.expected_active_version,
                            None,
                            "retired",
                        )
                    )
                ),
            ]
            self._dynamodb.transact_write_items(TransactItems=transaction_items)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {
                _CONDITIONAL,
                _TRANSACTION_CANCELLED,
            }:
                self._conflict("Prompt retirement conflicted; reload the active version.")
            self._write_error(error)

    def slot_states(
        self, slot_ids: tuple[InsightSlotId, ...]
    ) -> tuple[InsightPromptSlotState, ...]:
        """Overlay retained pointer and newest records onto the declared roster without a scan."""
        model_slots = tuple(slot_id for slot_id in slot_ids if slot_id is not InsightSlotId.MOVERS)
        requests = [
            {
                _SLOT_KEY: write_text(_slot(slot_id)),
                _RECORD_KEY: write_text(record_key),
            }
            for slot_id in model_slots
            for record_key in (_POINTER, _DEEP_POINTER, _COUNTER)
        ]
        items: dict[tuple[InsightSlotId, str], dict[str, AttributeValueTypeDef]] = {}
        if requests:
            try:
                response = self._dynamodb.batch_get_item(
                    RequestItems={self._table: {"Keys": requests, "ConsistentRead": True}}
                )
            except Exception as error:
                self._read_error(error)
            for item in response.get("Responses", {}).get(self._table, []):
                stored = dict(item)
                try:
                    slot_key = read_text(stored, _SLOT_KEY)
                    slot_id = InsightSlotId(slot_key.removeprefix("SLOT#"))
                    record_key = read_text(stored, _RECORD_KEY)
                except (ItemError, ValueError) as error:
                    raise InvariantError(
                        _REGISTRY_MALFORMED, "Prompt registry state is malformed."
                    ) from error
                items[(slot_id, record_key)] = stored
        latest_keys = [
            {
                _SLOT_KEY: write_text(_slot(slot_id)),
                _RECORD_KEY: write_text(_version_key(_read_number(counter, _VERSION))),
            }
            for slot_id in model_slots
            if (counter := items.get((slot_id, _COUNTER))) is not None
        ]
        if latest_keys:
            try:
                response = self._dynamodb.batch_get_item(
                    RequestItems={self._table: {"Keys": latest_keys, "ConsistentRead": True}}
                )
            except Exception as error:
                self._read_error(error)
            for item in response.get("Responses", {}).get(self._table, []):
                stored = dict(item)
                try:
                    slot_key = read_text(stored, _SLOT_KEY)
                    slot_id = InsightSlotId(slot_key.removeprefix("SLOT#"))
                    record_key = read_text(stored, _RECORD_KEY)
                except (ItemError, ValueError) as error:
                    raise InvariantError(
                        _REGISTRY_MALFORMED, "Prompt registry state is malformed."
                    ) from error
                items[(slot_id, record_key)] = stored
        states: list[InsightPromptSlotState] = []
        for slot_id in slot_ids:
            if slot_id is InsightSlotId.MOVERS:
                states.append(InsightPromptSlotState(slot_id, None, None, None))
                continue
            pointer = items.get((slot_id, _POINTER))
            deep_pointer = items.get((slot_id, _DEEP_POINTER))
            counter = items.get((slot_id, _COUNTER))
            try:
                active = None if pointer is None else _read_number(pointer, _ACTIVE_VERSION)
                active_deep = (
                    None if deep_pointer is None else _read_number(deep_pointer, _ACTIVE_VERSION)
                )
                newest = None if counter is None else _read_number(counter, _VERSION)
                latest = None if newest is None else items.get((slot_id, _version_key(newest)))
                calibration = None if latest is None else read_text(latest, _CALIBRATION)
            except (ItemError, ValueError) as error:
                raise InvariantError(
                    _REGISTRY_MALFORMED, "Prompt registry state is malformed."
                ) from error
            states.append(
                InsightPromptSlotState(
                    slot_id, active, newest, calibration, active_deep_version=active_deep
                )
            )
        return tuple(states)

    def versions(self, slot_id: InsightSlotId) -> tuple[InsightPromptVersionRecord, ...]:
        """Read immutable version and calibration records for one declared slot newest first."""
        if slot_id is InsightSlotId.MOVERS:
            return ()
        items = self._query_all(
            {
                "TableName": self._table,
                "KeyConditionExpression": "#slot_key = :slot_key",
                "ExpressionAttributeNames": {"#slot_key": _SLOT_KEY},
                "ExpressionAttributeValues": {":slot_key": write_text(_slot(slot_id))},
                "ConsistentRead": True,
            }
        )
        versions: dict[int, dict[str, AttributeValueTypeDef]] = {}
        cases: dict[int, list[InsightCalibrationCaseResult]] = {}
        for item in items:
            try:
                record_type = read_text(item, _RECORD_TYPE)
                version = _read_number(item, _VERSION)
            except ItemError, ValueError:
                continue
            if record_type == _VERSION_RECORD:
                versions[version] = item
            elif record_type == _CALIBRATION_CASE_RECORD:
                try:
                    cases.setdefault(version, []).append(
                        InsightCalibrationCaseResult(
                            InsightCalibrationKind(read_text(item, _CALIBRATION_KIND)),
                            InsightCalibrationOutcome(read_text(item, _OUTCOME)),
                        )
                    )
                except (ItemError, ValueError) as error:
                    raise InvariantError(
                        _REGISTRY_MALFORMED, "Prompt calibration case is malformed."
                    ) from error
        records: list[InsightPromptVersionRecord] = []
        for version, item in versions.items():
            try:
                author, created_at = _item_authorship(item)
                records.append(
                    InsightPromptVersionRecord(
                        slot_id=slot_id,
                        version=version,
                        prompt=read_text(item, _PROMPT),
                        model_route=InsightModelRoute(read_text(item, _MODEL_ROUTE)),
                        author=author,
                        created_at=created_at,
                        calibration=read_text(item, _CALIBRATION),
                        calibration_cases=tuple(cases.get(version, [])),
                        tier=_item_tier(item),
                        fragment_digests=_item_fragment_digests(item),
                    )
                )
            except (ItemError, ValueError) as error:
                raise InvariantError(
                    _REGISTRY_MALFORMED, "Prompt version record is malformed."
                ) from error
        return tuple(sorted(records, key=lambda record: record.version, reverse=True))

    def version(self, slot_id: InsightSlotId, version: str) -> InsightPromptActivation | None:
        """Read an immutable stored version rather than resolving a moving active pointer."""
        if (
            slot_id is InsightSlotId.MOVERS
            or not version.startswith("v")
            or not version[1:].isdigit()
        ):
            return None
        try:
            item = self._read_version(slot_id, int(version[1:]))
            digests = _item_fragment_digests(item)
            return InsightPromptActivation(
                slot_id,
                version,
                read_text(item, _PROMPT),
                InsightModelRoute(read_text(item, _MODEL_ROUTE)),
                fragment_digest=None if digests is None else digests.combined,
            )
        except InvariantError as error:
            # An absent version is the caller's typed miss, which it degrades durably. Any other
            # invariant (a malformed stored version) degrades the same way but is reported first.
            if error.code != _REGISTRY_CONFLICT:
                report_error(error, _VERSION_REPORT)
            return None
        except (ItemError, ValueError) as error:
            raise InvariantError(
                _REGISTRY_MALFORMED, "Prompt version record is malformed."
            ) from error

    def _get_counter(self, slot_id: InsightSlotId) -> int:
        item = self._get(slot_id, _COUNTER)
        try:
            if read_text(item, _RECORD_TYPE) != _COUNTER_RECORD:
                raise ValueError("counter record type is invalid.")
            return _read_number(item, _VERSION)
        except (ItemError, ValueError) as error:
            raise InvariantError(
                _REGISTRY_MALFORMED, "Prompt registry counter is malformed."
            ) from error

    @staticmethod
    def _pending_from_item(
        slot_id: InsightSlotId,
        version: int,
        item: dict[str, AttributeValueTypeDef],
    ) -> InsightPendingPromptVersion:
        if (
            InsightSlotId(read_text(item, "slot_id")) is not slot_id
            or _read_number(item, _VERSION) != version
            or read_text(item, _CALIBRATION) != _PENDING
        ):
            raise ValueError("prompt version is not pending calibration.")
        return InsightPendingPromptVersion(
            slot_id,
            version,
            read_text(item, _PROMPT),
            InsightModelRoute(read_text(item, _MODEL_ROUTE)),
            _item_tier(item),
        )

    @staticmethod
    def _work_state(
        item: dict[str, AttributeValueTypeDef], now_epoch_seconds: int
    ) -> InsightPromptCalibrationWorkState:
        try:
            calibration = read_text(item, _CALIBRATION)
            lease = item.get(_CALIBRATION_LEASE_REF)
            expires = item.get(_CALIBRATION_LEASE_EXPIRES)
            if (lease is None) != (expires is None):
                raise ValueError("prompt calibration lease fields are incomplete.")
            lease_active = False
            if lease is not None and expires is not None:
                if not read_text(item, _CALIBRATION_LEASE_REF).strip():
                    raise ValueError("prompt calibration lease is blank.")
                lease_active = _read_number(item, _CALIBRATION_LEASE_EXPIRES) > now_epoch_seconds
            return InsightPromptCalibrationWorkState(calibration, lease_active)
        except (ItemError, ValueError) as error:
            raise InvariantError(
                _REGISTRY_MALFORMED, "Prompt calibration work state is malformed."
            ) from error

    def _read_version(
        self, slot_id: InsightSlotId, version: int
    ) -> dict[str, AttributeValueTypeDef]:
        item = self._get(slot_id, _version_key(version))
        try:
            if (
                read_text(item, _RECORD_TYPE) != _VERSION_RECORD
                or InsightSlotId(read_text(item, "slot_id")) is not slot_id
                or _read_number(item, _VERSION) != version
            ):
                raise ValueError("version record identity is invalid.")
        except (ItemError, ValueError) as error:
            raise InvariantError(
                _REGISTRY_MALFORMED, "Prompt registry version is malformed."
            ) from error
        return item

    def _require_passed_calibration(self, slot_id: InsightSlotId, version: int) -> None:
        try:
            item = self._get(slot_id, _calibration_result_key(version))
        except InvariantError as error:
            if error.code == _REGISTRY_CONFLICT:
                raise InvariantError(
                    PROMPT_ACTIVATION_CALIBRATION_REQUIRED,
                    "Prompt activation requires a passed calibration result.",
                ) from None
            raise
        try:
            if (
                read_text(item, _RECORD_TYPE) != _CALIBRATION_RESULT_RECORD
                or InsightSlotId(read_text(item, "slot_id")) is not slot_id
                or _read_number(item, _VERSION) != version
                or read_text(item, _OUTCOME) != _PASSED
            ):
                raise ValueError("calibration result is not passed.")
        except ItemError, ValueError:
            raise InvariantError(
                PROMPT_ACTIVATION_CALIBRATION_REQUIRED,
                "Prompt activation requires a passed calibration result.",
            ) from None

    def _get(self, slot_id: InsightSlotId, record_key: str) -> dict[str, AttributeValueTypeDef]:
        try:
            response = self._dynamodb.get_item(
                TableName=self._table,
                Key={_SLOT_KEY: write_text(_slot(slot_id)), _RECORD_KEY: write_text(record_key)},
                ConsistentRead=True,
            )
        except Exception as error:
            self._read_error(error)
        item = response.get("Item")
        if item is None:
            self._conflict("Prompt registry record does not exist.")
        return dict(item)

    def _calibration_items(
        self,
        slot_id: InsightSlotId,
        version: int,
        result: InsightCalibrationResult,
    ) -> tuple[dict[str, AttributeValueTypeDef], ...]:
        slot = _slot(slot_id)
        summary = {
            _SLOT_KEY: write_text(slot),
            _RECORD_KEY: write_text(_calibration_result_key(version)),
            _RECORD_TYPE: write_text(_CALIBRATION_RESULT_RECORD),
            "slot_id": write_text(slot_id.value),
            _VERSION: _number(version),
            _OUTCOME: write_text(result.outcome.value),
        }
        cases = tuple(
            {
                _SLOT_KEY: write_text(slot),
                _RECORD_KEY: write_text(_calibration_case_key(version, case.kind.value)),
                _RECORD_TYPE: write_text(_CALIBRATION_CASE_RECORD),
                "slot_id": write_text(slot_id.value),
                _VERSION: _number(version),
                _CALIBRATION_KIND: write_text(case.kind.value),
                _OUTCOME: write_text(case.outcome.value),
            }
            for case in result.cases
        )
        return (summary, *cases)

    @staticmethod
    def _calibration_outbox_item(
        slot_id: InsightSlotId, version: int
    ) -> dict[str, AttributeValueTypeDef]:
        return {
            _SLOT_KEY: write_text(_slot(slot_id)),
            _RECORD_KEY: write_text(_calibration_outbox_key(version)),
            _RECORD_TYPE: write_text(_CALIBRATION_OUTBOX_RECORD),
            "slot_id": write_text(slot_id.value),
            _VERSION: _number(version),
        }

    @staticmethod
    def _version_item(
        request: InsightPromptVersionCreateRequest,
        version: int,
        calibration: str,
    ) -> dict[str, AttributeValueTypeDef]:
        digests = request.fragment_digests
        return {
            _SLOT_KEY: write_text(_slot(request.slot_id)),
            _RECORD_KEY: write_text(_version_key(version)),
            _RECORD_TYPE: write_text(_VERSION_RECORD),
            "slot_id": write_text(request.slot_id.value),
            _TIER: write_text(request.tier.value),
            _VERSION: _number(version),
            _PROMPT: write_text(request.prompt),
            _MODEL_ROUTE: write_text(request.model_route.value),
            _CALIBRATION: write_text(calibration),
            **_authorship_attributes(request.author),
            _CREATED_AT: write_text(request.created_at.astimezone(UTC).isoformat()),
            # Composed versions record their exact git-sourced fragments (ADR-076
            # decision 6); absence marks the version unsourced (direct or seeded).
            **(
                {}
                if digests is None
                else {
                    _FRAGMENT_DIGESTS: {
                        "M": {
                            "foundation": write_text(digests.foundation),
                            "tier_core": write_text(digests.tier_core),
                            "slot_block": write_text(digests.slot_block),
                        }
                    }
                }
            ),
        }

    @staticmethod
    def _pointer_item(pointer: _PointerRecord) -> dict[str, AttributeValueTypeDef]:
        return {
            _SLOT_KEY: write_text(_slot(pointer.slot_id)),
            _RECORD_KEY: write_text(_pointer_key(pointer.tier)),
            _RECORD_TYPE: write_text(_POINTER_RECORD),
            "slot_id": write_text(pointer.slot_id.value),
            _TIER: write_text(pointer.tier.value),
            _ACTIVE_VERSION: _number(pointer.version),
            _PROMPT: write_text(pointer.prompt),
            _MODEL_ROUTE: write_text(pointer.model_route.value),
            # Composed provenance replicated for the generation logs; absent = unsourced.
            **(
                {}
                if pointer.fragment_digest is None
                else {_FRAGMENT_DIGEST: write_text(pointer.fragment_digest)}
            ),
        }

    def _event_item(self, event: _LifecycleEvent) -> dict[str, AttributeValueTypeDef]:
        request = event.request
        return {
            _SLOT_KEY: write_text(_slot(request.slot_id)),
            _RECORD_KEY: write_text(f"{_EVENT_PREFIX}{request.event_id}"),
            _RECORD_TYPE: write_text(_EVENT_RECORD),
            "slot_id": write_text(request.slot_id.value),
            _FROM_VERSION: (
                {"NULL": True} if event.from_version is None else _number(event.from_version)
            ),
            _TO_VERSION: (
                {"NULL": True} if event.to_version is None else _number(event.to_version)
            ),
            _EVENT_ACTION: write_text(event.action),
            _OPERATOR: write_text(request.operator),
            _CREATED_AT: write_text(request.at.astimezone(UTC).isoformat()),
        }

    def _conditional_put(self, item: dict[str, AttributeValueTypeDef]) -> TransactWriteItemTypeDef:
        return {
            "Put": {
                "TableName": self._table,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(slot_key) AND attribute_not_exists(record_key)",
            }
        }

    def _query_all(self, request: QueryInputTypeDef) -> list[dict[str, AttributeValueTypeDef]]:
        """Read every page of one Query, following `LastEvaluatedKey` to the last page.

        One Query page stops at 1 MB, so reading only the first would drop a slot's later
        versions or outbox work silently once its partition outgrows that.
        """
        items: list[dict[str, AttributeValueTypeDef]] = []
        page: QueryInputTypeDef = request
        while True:
            try:
                response = self._dynamodb.query(**page)
            except Exception as error:
                self._read_error(error)
            items.extend(dict(raw) for raw in response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if last is None:
                return items
            page = {**request, "ExclusiveStartKey": last}

    @staticmethod
    def _read_error(error: Exception) -> NoReturn:
        raise DependencyError(
            _REGISTRY_UNREADABLE,
            "Insight prompt registry could not be read.",
            context={"cause": type(error).__name__},
        ) from None

    @staticmethod
    def _write_error(error: Exception) -> NoReturn:
        raise DependencyError(
            _REGISTRY_UNWRITABLE,
            "Insight prompt registry could not be written.",
            context={"cause": type(error).__name__},
        ) from None

    @staticmethod
    def _conflict(description: str) -> NoReturn:
        raise InvariantError(_REGISTRY_CONFLICT, description)
