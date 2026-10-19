# SPDX-License-Identifier: MIT-0
"""Read-only strongly consistent reads of the execution-graph store (S2 explanation).

The explanation's role holds only `GetItem` and `BatchGetItem` on the live table, so this
adapter has no write path at all: a cursor, a retry or an explanation can never mutate graph
authority (BF-5). Any read failure is a recoverable dependency failure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, cast

from botocore.exceptions import BotoCoreError, ClientError

from adapters.outbound.dynamodb.operational_status import decode_item
from domain.operations.execution_graph import AuthorityMode, ModeState
from domain.operations.generated_execution_graphs import STORE
from domain.shared_kernel.errors import DependencyError, ErrorCode

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

__all__ = ["DynamoExecutionGraphReader"]

_READ_FAILED: Final = ErrorCode("EXEC_EXPLAIN", 20)
_MODE_PK: Final = str(STORE["mode_key"]["pk"])
_MODE_SK: Final = str(STORE["mode_key"]["sk"])
_BATCH: Final = 100
_MAX_BATCH_ROUNDS: Final = 4

type Item = dict[str, AttributeValueTypeDef]


def _key(pk: str, sk: str) -> Item:
    return {"pk": {"S": pk}, "sk": {"S": sk}}


class DynamoExecutionGraphReader:
    """The live (or probe) execution-graph table, read-only."""

    def __init__(self, table: str, client: DynamoDBClient) -> None:
        self._table = table
        self._client = client

    def _failure(self, error: Exception) -> DependencyError:
        return DependencyError(
            _READ_FAILED,
            "the execution-graph store could not be read.",
            context={"cause": type(error).__name__},
        )

    def _get(self, pk: str, sk: str) -> dict[str, object] | None:
        try:
            found = self._client.get_item(
                TableName=self._table, Key=_key(pk, sk), ConsistentRead=True
            )
        except (BotoCoreError, ClientError) as error:
            raise self._failure(error) from None
        item = found.get("Item")
        return None if item is None else decode_item(dict(item))

    def read_mode(self) -> ModeState:
        """Read the mode item; absent is `LEGACY` at revision 0."""
        item = self._get(_MODE_PK, _MODE_SK)
        if item is None:
            return ModeState(AuthorityMode.LEGACY, 0)
        revision = item.get("revision")
        return ModeState(
            AuthorityMode(str(item.get("mode"))),
            revision if isinstance(revision, int) else 0,
        )

    def graph(self, graph_id: str) -> dict[str, object] | None:
        """Read one graph record."""
        return self._get(f"GRAPH#{graph_id}", "GRAPH")

    def epoch1_graph_id(self, fingerprint: str) -> str | None:
        """Follow `FINGERPRINT#1#F` to its run and the run's graph."""
        index = self._get(f"FINGERPRINT#1#{fingerprint}", "FINGERPRINT")
        if index is None:
            return None
        run = self._get(f"RUN#{index.get('run_id')}", "RUN")
        graph_name = None if run is None else run.get("graph_id")
        return graph_name if isinstance(graph_name, str) else None

    def items(
        self, keys: Sequence[tuple[str, str]]
    ) -> Mapping[tuple[str, str], Mapping[str, object]]:
        """Read exact items by key with strongly consistent batch reads."""
        found: dict[tuple[str, str], Mapping[str, object]] = {}
        unique = list(dict.fromkeys(keys))
        for start in range(0, len(unique), _BATCH):
            pending: list[Item] = [_key(pk, sk) for pk, sk in unique[start : start + _BATCH]]
            for _ in range(_MAX_BATCH_ROUNDS):
                if not pending:
                    break
                try:
                    response = self._client.batch_get_item(
                        RequestItems={
                            self._table: cast("Any", {"Keys": pending, "ConsistentRead": True})
                        }
                    )
                except (BotoCoreError, ClientError) as error:
                    raise self._failure(error) from None
                for raw in response.get("Responses", {}).get(self._table, []):
                    item = decode_item(dict(raw))
                    found[(str(item.get("pk")), str(item.get("sk")))] = item
                unprocessed = response.get("UnprocessedKeys", {}).get(self._table)
                pending = [] if unprocessed is None else list(unprocessed.get("Keys", []))
            if pending:
                raise DependencyError(
                    _READ_FAILED, "the execution-graph batch read left keys unprocessed."
                )
        return found
