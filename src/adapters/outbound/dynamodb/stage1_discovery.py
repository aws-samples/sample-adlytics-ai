# SPDX-License-Identifier: MIT-0
"""Sparse-index discovery over the retained publications table (write discipline §§5, 5.3).

The two DataV2-owned ``KEYS_ONLY`` GSIs are read here and nowhere else: ``table-attempt-reconcile``
(partition ``reconcile_status``, sort ``reconcile_after_or_block``) and ``stage1-plan-intent-ready``
(partition ``ready_status``, sort ``ready_order``). Every query is one eventually consistent
ascending ``Query`` from the first key with a caller-bounded ``Limit``; no ``ExclusiveStartKey``,
``LastEvaluatedKey``, cursor item, or ``Scan`` exists. The blocked page's only continuation is the
opaque recovery reference the caller already holds, applied as a lexical lower bound on the sort
key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from domain.operations.table_attempt import blocked_reconcile_key
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.stage1_discovery import (
    BlockedFenceQuery,
    DiscoveredKey,
    DueQuery,
    Stage1DiscoveryPort,
)

if TYPE_CHECKING:  # pragma: no cover — kept out of the Lambda import path
    from collections.abc import Mapping

    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

__all__ = [
    "PLAN_INTENT_READY_INDEX",
    "TABLE_ATTEMPT_RECONCILE_INDEX",
    "DynamoStage1Discovery",
]

TABLE_ATTEMPT_RECONCILE_INDEX: Final = "table-attempt-reconcile"
PLAN_INTENT_READY_INDEX: Final = "stage1-plan-intent-ready"
_PENDING: Final = "pending"
_BLOCKED: Final = "blocked"
_READY: Final = "ready"
_UNREADABLE: Final = ErrorCode("STAGE1_DISCOVERY", 1)


def _text(item: Mapping[str, AttributeValueTypeDef], name: str) -> str:
    value = cast("Mapping[str, object] | None", item.get(name))
    text = None if value is None else value.get("S")
    if not isinstance(text, str):
        raise DependencyError(
            _UNREADABLE,
            "A sparse-index projection returned a key outside its String form.",
            context={"attribute": name},
        )
    return text


class DynamoStage1Discovery(Stage1DiscoveryPort):
    """Bounded, cursorless queries over the two Stage-1 sparse GSIs."""

    def __init__(self, publications_table: str, client: DynamoDBClient | None = None) -> None:
        if not publications_table.strip():
            raise ValueError("Stage-1 discovery requires the publications table name.")
        self._table = publications_table
        self._client: DynamoDBClient = client if client is not None else boto3.client("dynamodb")

    def due_pending_fences(self, query: DueQuery) -> tuple[DiscoveredKey, ...]:
        """Query the pending partition for every fence due at or before ``now``."""
        return self._query(
            TABLE_ATTEMPT_RECONCILE_INDEX,
            "reconcile_status = :partition AND reconcile_after_or_block <= :bound",
            {":partition": {"S": _PENDING}, ":bound": {"S": query.upper_bound}},
            query.limit,
            "reconcile_after_or_block",
        )

    def blocked_fences(self, query: BlockedFenceQuery) -> tuple[DiscoveredKey, ...]:
        """Query the blocked partition, lexically after the optional opaque cursor."""
        condition = "reconcile_status = :partition"
        values: dict[str, AttributeValueTypeDef] = {":partition": {"S": _BLOCKED}}
        if query.after_recovery_ref is not None:
            condition += " AND reconcile_after_or_block > :cursor"
            values[":cursor"] = {"S": blocked_reconcile_key(query.after_recovery_ref)}
        return self._query(
            TABLE_ATTEMPT_RECONCILE_INDEX,
            condition,
            values,
            query.limit,
            "reconcile_after_or_block",
        )

    def ready_intents(self, query: DueQuery) -> tuple[DiscoveredKey, ...]:
        """Query the ready partition for every pending intent due at or before ``now``."""
        return self._query(
            PLAN_INTENT_READY_INDEX,
            "ready_status = :partition AND ready_order <= :bound",
            {":partition": {"S": _READY}, ":bound": {"S": query.upper_bound}},
            query.limit,
            "ready_order",
        )

    def _query(  # noqa: PLR0913, PLR0917 -- one private query shape for three indexes
        self,
        index: str,
        condition: str,
        values: Mapping[str, AttributeValueTypeDef],
        limit: int,
        sort_attribute: str,
    ) -> tuple[DiscoveredKey, ...]:
        try:
            response = self._client.query(
                TableName=self._table,
                IndexName=index,
                KeyConditionExpression=condition,
                ExpressionAttributeValues=dict(values),
                Limit=limit,
                ScanIndexForward=True,
            )
        except (BotoCoreError, ClientError) as error:
            raise DependencyError(
                _UNREADABLE,
                "A Stage-1 sparse-index query failed.",
                context={"index": index, "cause": type(error).__name__},
            ) from error
        return tuple(
            DiscoveredKey(
                partition_key=_text(item, "partition_key"),
                record_key=_text(item, "record_key"),
                sort_value=_text(item, sort_attribute),
            )
            for item in response.get("Items", [])
        )
