# SPDX-License-Identifier: MIT-0
"""Strongly consistent reads of the graph store's due shards and hold, for the stuck-flow detector.

Read-only by construction: one paginated `Query` of a whole `DUE#<n>` partition, with
`ConsistentRead=True` (a GSI cannot be read strongly, so a GSI is never authority, R-27), and one
`GetItem` of the mode item for the hold. The same adapter reads the live table under the
detector's role and the probe table under the probe role (S5, P5).

Plan 6.1 (F9): the shard `Query` already returns full items, so a `BAKE_RECOVERY#` item's
`reason` and a `COMPLETE#` item's readiness stamp are parsed from it with no extra read. The one
added read is a strongly consistent `GetItem` of `COHORT#<grant_id>/CONTRIBUTION#<member>`: the
precompute member for each grant `stuck_flow.bake_contribution_candidates` names, and (ADR-140)
the bake member, then the precompute member when bake is stored, for each grant
`stuck_flow.unarmed_contribution_candidates` names.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast

from botocore.exceptions import BotoCoreError, ClientError

from domain.operations.execution_graph_release import CohortMember
from domain.operations.generated_execution_graphs import STORE
from domain.operations.stuck_flow import ContributionObservation, DueObservation, HoldState
from ports.outbound.execution_graph_dispatch import GraphStoreUnavailableError

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient

__all__ = ["DynamoDueShardReader"]

_MODE_PK: Final = str(STORE["mode_key"]["pk"])
_MODE_SK: Final = str(STORE["mode_key"]["sk"])
_RECOVERY_PREFIX: Final = "BAKE_RECOVERY#"
_COMPLETE_PREFIX: Final = "COMPLETE#"


def _text(item: dict[str, Any], name: str) -> str | None:
    value = item.get(name, {}).get("S")
    return None if value is None else str(value)


def _moment(item: dict[str, Any], name: str) -> datetime | None:
    value = _text(item, name)
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise GraphStoreUnavailableError(f"a due item's {name} is not ISO-8601.") from None


def _observation(shard: str, item: dict[str, Any]) -> DueObservation:
    """Project one due item; only its own kind's bake fields are read."""
    sk = str(_text(item, "sk"))
    complete = sk.startswith(_COMPLETE_PREFIX)
    return DueObservation(
        shard=shard,
        sk=sk,
        root_clock_started_at=_moment(item, "root_clock_started_at"),
        woken_at=_moment(item, "woken_at"),
        held=bool(item.get("held", {}).get("BOOL", False)),
        recovery_reason=_text(item, "reason") if sk.startswith(_RECOVERY_PREFIX) else None,
        run_id=_text(item, "run_id"),
        grant_id=_text(item, "grant_id"),
        bake_readiness_at=_moment(item, "bake_readiness_at") if complete else None,
        input_effect_roster_digest=(
            _text(item, "input_effect_roster_digest") if complete else None
        ),
    )


class DynamoDueShardReader:
    """Read one due shard, or the hold, strongly."""

    def __init__(self, table: str, client: DynamoDBClient) -> None:
        if not table.strip():
            raise ValueError("the execution-graph table is required")
        self._table = table
        self._client = client

    def read_shard(self, shard: str) -> tuple[DueObservation, ...]:
        """Return every item in one due shard, in sort-key order."""
        found: list[DueObservation] = []
        start: dict[str, Any] | None = None
        while True:
            arguments: dict[str, Any] = {
                "TableName": self._table,
                "KeyConditionExpression": "pk = :pk",
                "ExpressionAttributeValues": {":pk": {"S": shard}},
                "ConsistentRead": True,
            }
            if start is not None:
                arguments["ExclusiveStartKey"] = start
            try:
                page = self._client.query(**arguments)
            except (BotoCoreError, ClientError) as error:
                raise GraphStoreUnavailableError(
                    f"a due shard could not be read: {type(error).__name__}"
                ) from None
            for raw in page.get("Items", []):
                found.append(_observation(shard, cast("dict[str, Any]", raw)))
            start = cast("dict[str, Any] | None", page.get("LastEvaluatedKey"))
            if start is None:
                return tuple(found)

    def read_contribution(
        self, grant_id: str, member: CohortMember
    ) -> ContributionObservation | None:
        """Return one grant's contribution of one member, strongly, or None when none is stored."""
        try:
            response = self._client.get_item(
                TableName=self._table,
                Key={
                    "pk": {"S": f"COHORT#{grant_id}"},
                    "sk": {"S": f"CONTRIBUTION#{member.value}"},
                },
                ConsistentRead=True,
            )
        except (BotoCoreError, ClientError) as error:
            raise GraphStoreUnavailableError(
                f"a {member.value} contribution could not be read: {type(error).__name__}"
            ) from None
        item = cast("dict[str, Any] | None", response.get("Item"))
        if item is None:
            return None
        settled = _moment(item, "settled_at")
        if settled is None:
            raise GraphStoreUnavailableError(
                f"a {member.value} contribution carries no settled_at."
            )
        return ContributionObservation(grant_id, settled, _text(item, "input_effect_roster_digest"))

    def read_hold(self) -> HoldState:
        """Return the automatic-admission hold from the mode item (absent reads as clear)."""
        try:
            response = self._client.get_item(
                TableName=self._table,
                Key={"pk": {"S": _MODE_PK}, "sk": {"S": _MODE_SK}},
                ConsistentRead=True,
            )
        except (BotoCoreError, ClientError) as error:
            raise GraphStoreUnavailableError(
                f"the mode item could not be read: {type(error).__name__}"
            ) from None
        item = cast("dict[str, Any] | None", response.get("Item"))
        if item is None:
            return HoldState(set=False)
        return HoldState(
            set=bool(item.get("hold", {}).get("BOOL", False)),
            cleared_at=_moment(item, "hold_cleared_at"),
        )
