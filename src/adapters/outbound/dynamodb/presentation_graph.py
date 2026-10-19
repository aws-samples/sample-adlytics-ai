# SPDX-License-Identifier: MIT-0
"""Presentation's read-only graph-table reads: the mode item and `SERVING#` (S6 §2.9).

Presentation needs two facts from the execution-graph table: when `GRAPH` was entered (the
`built_before` bound of a grant-less pointer) and whether a grant's serving binding exists and
names a pointer's generation. Both are strong `GetItem`s, and this adapter has no write path.

The `GRAPH` CAS sets `graph_entered_at` once, so the normalized instant is cached for the life
of the container once it is read. Before then every call reads the mode item again, so a warm
container sees the CAS without a redeploy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from adapters.outbound.dynamodb.execution_graph import mode_state_from_item
from adapters.outbound.dynamodb.execution_graph_release import (
    serving_binding_from_item,
    serving_binding_key,
)
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_release import ServingBinding
from domain.operations.generated_execution_graphs import STORE
from domain.presentation.graph_serving import normalize_graph_entered_at
from domain.shared_kernel.errors import GRAPH_SERVING_ENTRY_MALFORMED, InvariantError
from ports.outbound.execution_graph_dispatch import GraphStoreUnavailableError

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

__all__ = ["DynamoPresentationGraphReader"]

type Item = dict[str, AttributeValueTypeDef]

_MODE_KEY: Final[Item] = {
    "pk": {"S": str(STORE["mode_key"]["pk"])},
    "sk": {"S": str(STORE["mode_key"]["sk"])},
}


class DynamoPresentationGraphReader:
    """The live execution-graph table, read only, for presentation."""

    def __init__(self, table: str, client: DynamoDBClient | None = None) -> None:
        if not table.strip():
            raise ValueError("presentation graph table cannot be blank.")
        self._table = table
        self._client = client or boto3.client("dynamodb")
        self._graph_entered_at: str | None = None

    def _get(self, key: Item) -> Item | None:
        try:
            found = self._client.get_item(TableName=self._table, Key=key, ConsistentRead=True)
        except (BotoCoreError, ClientError) as error:
            raise GraphStoreUnavailableError(
                f"execution-graph read failed: {type(error).__name__}"
            ) from None
        item = found.get("Item")
        return None if item is None else dict(item)

    def graph_entered_at(self) -> str | None:
        """Return the normalized `GRAPH` entry instant, or None outside `GRAPH`."""
        if self._graph_entered_at is not None:
            return self._graph_entered_at
        try:
            mode = mode_state_from_item(self._get(_MODE_KEY))
        except ValueError:
            raise InvariantError(
                GRAPH_SERVING_ENTRY_MALFORMED, "the graph mode item names no known mode."
            ) from None
        if mode.mode is not AuthorityMode.GRAPH:
            return None
        self._graph_entered_at = normalize_graph_entered_at(mode.graph_entered_at)
        return self._graph_entered_at

    def serving_binding(self, grant_id: str) -> ServingBinding | None:
        """Return one grant's serving binding, or None while its cohort is open."""
        item = self._get(serving_binding_key(grant_id))
        return None if item is None else serving_binding_from_item(grant_id, item)
