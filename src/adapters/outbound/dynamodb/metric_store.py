# SPDX-License-Identifier: MIT-0
"""DynamoDB authority for crash-safe measurements (FR-3, S5).

Items, all in one table (the status table for `AuthorityScope=live`, the dev probe graph table
for `probe`), under the key attribute names that table uses:

- `MEASUREMENT#<measurement_id>` / `MEASUREMENT`: the immutable contribution;
- `METRIC_ACCEPT#<measurement_id>` / `ACCEPT`: `PENDING`, then `ACCEPTED` bound to its aggregate
  revision and snapshot digest;
- `METRIC_DUE#<n>` / `ACCEPT#<measurement_id>`: the pending acceptance's fixed-shard marker;
- `AGGREGATE#<aggregate_id>` / `AGGREGATE`: revision `r` and complete statistics `S`;
- `AGGREGATE_MEMBER#<aggregate_id>#<measurement_id>` / `MEMBER`: created once per acceptance;
- `METRIC_SNAPSHOT#<aggregate_id>` / `<20-digit revision>`: one complete revision and its state;
- `METRIC_DUE#<n>` / `SNAPSHOT#<aggregate_id>`: the latest snapshot that still needs delivery;
- `SINK_CONFIRMATION#<aggregate_id>` / `<20-digit revision>`: the immutable confirmation receipt.

Every read is strongly consistent and every write is one conditioned transaction. Pending work
has no TTL. The due shards are base-table partitions, so the reconciler finds pending work with
strong `Query` reads, never a GSI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast

from botocore.exceptions import BotoCoreError, ClientError

from domain.operations.measurement import (
    AcceptanceRecord,
    AcceptanceState,
    Aggregate,
    Gauge,
    Measurement,
    Snapshot,
    SnapshotState,
    Statistics,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient

__all__ = [
    "METRIC_DUE_SHARDS",
    "AcceptanceConflictError",
    "AmbiguousCommitError",
    "DynamoMetricStore",
    "MetricStoreUnavailableError",
    "SinkConfirmation",
    "metric_shard",
]

METRIC_DUE_SHARDS: Final = 16
_UNAVAILABLE: Final = ErrorCode("MEASUREMENT", 20)

type Item = dict[str, Any]


class MetricStoreUnavailableError(DependencyError):
    """A read failed, or a write failed before DynamoDB could have committed it."""

    def __init__(self, description: str) -> None:
        super().__init__(_UNAVAILABLE, description)


class AcceptanceConflictError(Exception):
    """A condition moved: re-read and decide again (never an ambiguous commit)."""


class AmbiguousCommitError(Exception):
    """The commit's outcome is unknown: recover only from strongly read evidence."""


@dataclass(frozen=True, slots=True)
class SinkConfirmation:
    """The immutable receipt of one confirmed snapshot revision."""

    aggregate_id: str
    revision: int
    query_start: datetime
    query_end: datetime
    values: tuple[tuple[str, float], ...]
    confirmed_at: str


def metric_shard(identity: str) -> str:
    """Return the fixed due shard of a measurement or aggregate."""
    return f"METRIC_DUE#{int(identity[:8], 16) % METRIC_DUE_SHARDS}"


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _rev(revision: int) -> str:
    return f"{revision:020d}"


def _stats(statistics: Statistics) -> dict[str, Any]:
    return {"S": json.dumps(statistics.canonical(), separators=(",", ":"))}


def _read_stats(item: Item) -> Statistics:
    count, total, buckets, maximum = json.loads(str(item["statistics"]["S"]))
    return Statistics(int(count), int(total), tuple(int(b) for b in buckets), int(maximum))


def _text(item: Item, name: str) -> str | None:
    value = item.get(name, {}).get("S")
    return None if value is None else str(value)


def _int(item: Item, name: str) -> int | None:
    value = item.get(name, {}).get("N")
    return None if value is None else int(str(value))


class DynamoMetricStore:
    """The measurement authority over one table and its key attribute names."""

    def __init__(  # noqa: PLR0913 -- the table, its client and its two key attribute names
        self,
        table: str,
        client: DynamoDBClient,
        *,
        partition_key: str = "partition_key",
        sort_key: str = "sort_key",
    ) -> None:
        if not table.strip():
            raise ValueError("the measurement table is required")
        self._table = table
        self._client = client
        self._pk = partition_key
        self._sk = sort_key

    @property
    def table(self) -> str:
        """Return the table name."""
        return self._table

    def key(self, pk: str, sk: str) -> Item:
        """Return one key in this table's attribute names."""
        return {self._pk: _s(pk), self._sk: _s(sk)}

    # Contribution: written by the projection transaction it measures.

    def contribution_members(self, contribution: Measurement) -> list[dict[str, Any]]:
        """Return the contribution's transaction members: create-once, with its pending marker."""
        start, end = contribution.window
        new = f"attribute_not_exists({self._pk})"
        measurement_item = {
            **self.key(f"MEASUREMENT#{contribution.measurement_id}", "MEASUREMENT"),
            "family": _s(contribution.family),
            "dimensions": _s(json.dumps([list(pair) for pair in contribution.dimensions])),
            "value_ms": _n(contribution.value_ms),
            "occurred_at": _s(contribution.occurred_at.isoformat()),
            "window_start": _s(start.isoformat()),
            "window_end": _s(end.isoformat()),
            "aggregate_id": _s(contribution.aggregate_id),
            "source": _s(contribution.source),
        }
        accept_item = {
            **self.key(f"METRIC_ACCEPT#{contribution.measurement_id}", "ACCEPT"),
            "state": _s(AcceptanceState.PENDING.value),
            "aggregate_id": _s(contribution.aggregate_id),
        }
        marker = {
            **self.key(
                metric_shard(contribution.measurement_id), f"ACCEPT#{contribution.measurement_id}"
            ),
            "measurement_id": _s(contribution.measurement_id),
        }
        return [
            {"Put": {"TableName": self._table, "Item": item, "ConditionExpression": new}}
            for item in (measurement_item, accept_item, marker)
        ]

    # Strong reads.

    def _get(self, pk: str, sk: str) -> Item | None:
        try:
            found = self._client.get_item(
                TableName=self._table, Key=cast("Any", self.key(pk, sk)), ConsistentRead=True
            )
        except (BotoCoreError, ClientError) as error:
            raise MetricStoreUnavailableError(
                f"a measurement read failed: {type(error).__name__}"
            ) from None
        item = found.get("Item")
        return None if item is None else cast("Item", dict(item))

    def _query(self, pk: str, prefix: str) -> list[Item]:
        items: list[Item] = []
        start: dict[str, Any] | None = None
        while True:
            arguments: dict[str, Any] = {
                "TableName": self._table,
                "KeyConditionExpression": "#pk = :pk AND begins_with(#sk, :prefix)",
                "ExpressionAttributeNames": {"#pk": self._pk, "#sk": self._sk},
                "ExpressionAttributeValues": {":pk": _s(pk), ":prefix": _s(prefix)},
                "ConsistentRead": True,
            }
            if start is not None:
                arguments["ExclusiveStartKey"] = start
            try:
                page = self._client.query(**arguments)
            except (BotoCoreError, ClientError) as error:
                raise MetricStoreUnavailableError(
                    f"a measurement query failed: {type(error).__name__}"
                ) from None
            items.extend(cast("Item", dict(item)) for item in page.get("Items", []))
            start = cast("dict[str, Any] | None", page.get("LastEvaluatedKey"))
            if start is None:
                return items

    def pending(self, shard: str) -> tuple[str, ...]:
        """Return the measurement IDs with a pending acceptance marker in one shard."""
        return tuple(str(_text(item, "measurement_id")) for item in self._query(shard, "ACCEPT#"))

    def snapshot_markers(self, shard: str) -> tuple[tuple[str, int], ...]:
        """Return each aggregate with a snapshot still to deliver, and that revision."""
        found: list[tuple[str, int]] = []
        for item in self._query(shard, "SNAPSHOT#"):
            sk = str(item[self._sk]["S"])
            found.append((sk.removeprefix("SNAPSHOT#"), int(_int(item, "revision") or 0)))
        return tuple(found)

    def read_measurement(self, measurement_id: str) -> Measurement | None:
        """Read one immutable contribution."""
        item = self._get(f"MEASUREMENT#{measurement_id}", "MEASUREMENT")
        if item is None:
            return None
        return Measurement(
            measurement_id=measurement_id,
            family=str(_text(item, "family")),
            dimensions=tuple(
                (str(name), str(value))
                for name, value in json.loads(str(_text(item, "dimensions")))
            ),
            value_ms=int(_int(item, "value_ms") or 0),
            occurred_at=datetime.fromisoformat(str(_text(item, "occurred_at"))),
            source=str(_text(item, "source")),
        )

    def read_acceptance(self, measurement_id: str) -> AcceptanceRecord | None:
        """Read one acceptance record."""
        item = self._get(f"METRIC_ACCEPT#{measurement_id}", "ACCEPT")
        if item is None:
            return None
        bound = _text(item, "bound_aggregate_id")
        return AcceptanceRecord(
            measurement_id=measurement_id,
            state=AcceptanceState(str(_text(item, "state"))),
            aggregate_id=bound,
            revision=_int(item, "revision"),
            snapshot_digest=_text(item, "snapshot_digest"),
        )

    def read_aggregate(self, aggregate_id: str) -> Aggregate:
        """Read one aggregate; absence is revision 0 with zero statistics."""
        item = self._get(f"AGGREGATE#{aggregate_id}", "AGGREGATE")
        if item is None:
            return Aggregate(aggregate_id, 0, Statistics())
        return Aggregate(aggregate_id, int(_int(item, "revision") or 0), _read_stats(item))

    def member_present(self, aggregate_id: str, measurement_id: str) -> bool:
        """Return whether the deterministic membership row exists."""
        return self._get(f"AGGREGATE_MEMBER#{aggregate_id}#{measurement_id}", "MEMBER") is not None

    def _snapshot(self, item: Item, aggregate_id: str) -> Snapshot:
        return Snapshot(
            aggregate_id=aggregate_id,
            revision=int(str(item[self._sk]["S"])),
            statistics=_read_stats(item),
            measurement_id=str(_text(item, "measurement_id")),
            digest=str(_text(item, "snapshot_digest")),
            state=SnapshotState(str(_text(item, "state"))),
        )

    def read_snapshot(self, aggregate_id: str, revision: int) -> Snapshot | None:
        """Read one snapshot revision."""
        item = self._get(f"METRIC_SNAPSHOT#{aggregate_id}", _rev(revision))
        return None if item is None else self._snapshot(item, aggregate_id)

    def read_chain(self, aggregate_id: str, start: int) -> tuple[Snapshot, ...]:
        """Read every snapshot revision from `start` on, in revision order."""
        # Revisions are zero-padded to 20 digits, so every one starts with "0"; a key condition
        # cannot take an empty prefix.
        items = self._query(f"METRIC_SNAPSHOT#{aggregate_id}", "0")
        chain = [self._snapshot(item, aggregate_id) for item in items]
        return tuple(item for item in chain if item.revision >= start)

    def read_family(self, aggregate_id: str) -> tuple[str, tuple[tuple[str, str], ...], datetime]:
        """Read the aggregate's family, dimensions and window start (for its gauges)."""
        item = self._get(f"AGGREGATE#{aggregate_id}", "AGGREGATE")
        if item is None:
            raise MetricStoreUnavailableError("a snapshot names an aggregate that is absent.")
        dims = tuple(
            (str(name), str(value)) for name, value in json.loads(str(_text(item, "dimensions")))
        )
        return (
            str(_text(item, "family")),
            dims,
            datetime.fromisoformat(str(_text(item, "window_start"))),
        )

    def read_confirmation(self, aggregate_id: str, revision: int) -> Item | None:
        """Read one sink-confirmation receipt as stored."""
        return self._get(f"SINK_CONFIRMATION#{aggregate_id}", _rev(revision))

    # Conditioned transactions.

    def _transact(self, members: list[dict[str, Any]]) -> None:
        try:
            self._client.transact_write_items(TransactItems=cast("Any", members))
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException":
                raise AcceptanceConflictError(code) from None
            raise AmbiguousCommitError(code) from None
        except BotoCoreError as error:
            raise AmbiguousCommitError(type(error).__name__) from None

    def accept(self, contribution: Measurement, current: Aggregate, snapshot: Snapshot) -> None:
        """Commit one acceptance: member, aggregate `r -> r+1`, snapshot, measurement, markers."""
        start, end = contribution.window
        aggregate_item = {
            **self.key(f"AGGREGATE#{current.aggregate_id}", "AGGREGATE"),
            "revision": _n(snapshot.revision),
            "statistics": _stats(snapshot.statistics),
            "family": _s(contribution.family),
            "dimensions": _s(json.dumps([list(pair) for pair in contribution.dimensions])),
            "window_start": _s(start.isoformat()),
            "window_end": _s(end.isoformat()),
        }
        aggregate_condition: dict[str, Any] = (
            {"ConditionExpression": f"attribute_not_exists({self._pk})"}
            if current.revision == 0
            else {
                "ConditionExpression": "revision = :rev",
                "ExpressionAttributeValues": {":rev": _n(current.revision)},
            }
        )
        new = f"attribute_not_exists({self._pk})"
        members: list[dict[str, Any]] = [
            {
                "Put": {
                    "TableName": self._table,
                    "Item": {
                        **self.key(
                            f"AGGREGATE_MEMBER#{current.aggregate_id}#{contribution.measurement_id}",
                            "MEMBER",
                        ),
                        "revision": _n(snapshot.revision),
                    },
                    "ConditionExpression": new,
                }
            },
            {"Put": {"TableName": self._table, "Item": aggregate_item, **aggregate_condition}},
            {
                "Put": {
                    "TableName": self._table,
                    "Item": {
                        **self.key(
                            f"METRIC_SNAPSHOT#{current.aggregate_id}", _rev(snapshot.revision)
                        ),
                        "state": _s(SnapshotState.PENDING.value),
                        "statistics": _stats(snapshot.statistics),
                        "measurement_id": _s(contribution.measurement_id),
                        "snapshot_digest": _s(snapshot.digest),
                    },
                    "ConditionExpression": new,
                }
            },
            {
                "Update": {
                    "TableName": self._table,
                    "Key": self.key(f"METRIC_ACCEPT#{contribution.measurement_id}", "ACCEPT"),
                    "UpdateExpression": "SET #state = :accepted, bound_aggregate_id = :agg, "
                    "revision = :next, snapshot_digest = :digest",
                    "ConditionExpression": "#state = :pending",
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ExpressionAttributeValues": {
                        ":accepted": _s(AcceptanceState.ACCEPTED.value),
                        ":pending": _s(AcceptanceState.PENDING.value),
                        ":agg": _s(current.aggregate_id),
                        ":next": _n(snapshot.revision),
                        ":digest": _s(snapshot.digest),
                    },
                }
            },
            {
                "Delete": {
                    "TableName": self._table,
                    "Key": self.key(
                        metric_shard(contribution.measurement_id),
                        f"ACCEPT#{contribution.measurement_id}",
                    ),
                }
            },
            {
                "Put": {
                    "TableName": self._table,
                    "Item": {
                        **self.key(
                            metric_shard(current.aggregate_id), f"SNAPSHOT#{current.aggregate_id}"
                        ),
                        "revision": _n(snapshot.revision),
                    },
                }
            },
        ]
        self._transact(members)

    def retire_marker(self, measurement_id: str) -> None:
        """Remove the pending marker of a measurement already proved accepted."""
        self._transact(
            [
                {
                    "Delete": {
                        "TableName": self._table,
                        "Key": self.key(metric_shard(measurement_id), f"ACCEPT#{measurement_id}"),
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": self._table,
                        "Key": self.key(f"METRIC_ACCEPT#{measurement_id}", "ACCEPT"),
                        "ConditionExpression": "#state = :accepted",
                        "ExpressionAttributeNames": {"#state": "state"},
                        "ExpressionAttributeValues": {
                            ":accepted": _s(AcceptanceState.ACCEPTED.value)
                        },
                    }
                },
            ]
        )

    def mark_emitted(self, aggregate_id: str, revision: int, at: str) -> None:
        """Move `PENDING -> EMITTED_UNCONFIRMED` after a successful local write and flush."""
        self._transact(
            [
                {
                    "Update": {
                        "TableName": self._table,
                        "Key": self.key(f"METRIC_SNAPSHOT#{aggregate_id}", _rev(revision)),
                        "UpdateExpression": "SET #state = :emitted, emitted_at = :at",
                        "ConditionExpression": "#state = :pending",
                        "ExpressionAttributeNames": {"#state": "state"},
                        "ExpressionAttributeValues": {
                            ":emitted": _s(SnapshotState.EMITTED_UNCONFIRMED.value),
                            ":pending": _s(SnapshotState.PENDING.value),
                            ":at": _s(at),
                        },
                    }
                }
            ]
        )

    def confirm(self, receipt: SinkConfirmation) -> None:
        """Write the immutable receipt and move `EMITTED_UNCONFIRMED -> DELIVERED`, atomically."""
        aggregate, revision = receipt.aggregate_id, receipt.revision
        self._transact(
            [
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": {
                            **self.key(f"SINK_CONFIRMATION#{aggregate}", _rev(revision)),
                            "query_start": _s(receipt.query_start.isoformat()),
                            "query_end": _s(receipt.query_end.isoformat()),
                            "values": _s(json.dumps([list(pair) for pair in receipt.values])),
                            "confirmed_at": _s(receipt.confirmed_at),
                        },
                        "ConditionExpression": f"attribute_not_exists({self._pk})",
                    }
                },
                {
                    "Update": {
                        "TableName": self._table,
                        "Key": self.key(f"METRIC_SNAPSHOT#{aggregate}", _rev(revision)),
                        "UpdateExpression": "SET #state = :delivered, confirmed_at = :at",
                        "ConditionExpression": "#state = :emitted",
                        "ExpressionAttributeNames": {"#state": "state"},
                        "ExpressionAttributeValues": {
                            ":delivered": _s(SnapshotState.DELIVERED.value),
                            ":emitted": _s(SnapshotState.EMITTED_UNCONFIRMED.value),
                            ":at": _s(receipt.confirmed_at),
                        },
                    }
                },
                {
                    "Delete": {
                        "TableName": self._table,
                        "Key": self.key(metric_shard(aggregate), f"SNAPSHOT#{aggregate}"),
                        "ConditionExpression": "revision = :rev",
                        "ExpressionAttributeValues": {":rev": _n(revision)},
                    }
                },
            ]
        )


def gauge_key(gauge: Gauge) -> str:
    """Return a stable text form of one gauge, for the confirmation receipt."""
    return json.dumps([gauge.name, [list(pair) for pair in gauge.dimensions]])
