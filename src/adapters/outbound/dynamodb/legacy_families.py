# SPDX-License-Identifier: MIT-0
"""The S6 zero proof's reads over the legacy coordinator (publications) table (V-17b).

Every read is strongly consistent and against the base table, never a GSI and never SQS
attributes (design.md, "Legacy cutover", step 1):

- `read_drain_seq` is one consistent `GetItem` of `DRAIN_SEQ`;
- `enumerate_families` is one complete consistent `Scan` of the coordinator table, filtered to
  the `WRITE_PLAN#` partitions, paginated to the end under a generated page bound. A scan that
  would need more pages than the bound does not return a count: it raises, because an
  incomplete enumeration is never a zero proof.

The four families are classified from the items alone (`classify_families`, pure):

- F1: an intent `META` in state `pending` (both origins);
- F2: an intent `META` in state `claimed` with no plan `META` naming it (`origin_intent_id`);
- F3: a plan `META` in status `planned` with a unit whose `admission` is `pending`;
- F4: a unit of a `planned` plan whose `admission` is `admitted` and whose status is neither
  `attested` nor `failed`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast

from botocore.exceptions import BotoCoreError, ClientError

from domain.operations.cutover import (
    DRAIN_SEQ_PARTITION_KEY,
    DRAIN_SEQ_RECORD_KEY,
    FamilyCounts,
)
from domain.operations.write_plan import (
    INTENT_SEGMENT,
    META_RECORD_KEY,
    UNIT_RECORD_PREFIX,
    WRITE_PLAN_PREFIX,
    IntentState,
    PlanStatus,
    PlanUnitAdmission,
    PlanUnitStatus,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

__all__ = [
    "MAX_SCAN_PAGES",
    "DynamoLegacyFamilies",
    "FamilyEnumeration",
    "ZeroProofUnreadableError",
    "classify_families",
]

MAX_SCAN_PAGES: Final = 20000
"""The enumeration's page bound; exceeding it is an unreadable proof, never a partial count.

A loop guard, not a size estimate: the `Scan` is filtered after the read, so every page of the
whole coordinator table counts. The dev table read 2,767 pages on 2026-10-11, past the earlier
bound of 2,000, which made the zero proof unreadable at any drain state.
"""
_SAMPLE: Final = 20
_UNIT_DONE: Final = frozenset({PlanUnitStatus.ATTESTED.value, PlanUnitStatus.FAILED.value})


class ZeroProofUnreadableError(DependencyError):
    """A zero proof could not be read from authority (an S6 stop rule)."""

    def __init__(self, description: str) -> None:
        super().__init__(ErrorCode("EXEC_GRAPH", 40), description)
        self.signal = "ZERO_PROOF_UNREADABLE"


@dataclass(frozen=True, slots=True)
class FamilyEnumeration:
    """One complete enumeration: the counts, the items read and a bounded sample of open keys."""

    counts: FamilyCounts
    items_read: int
    pages: int
    open_keys: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])


def _text(item: Mapping[str, AttributeValueTypeDef], name: str) -> str | None:
    value = item.get(name, {}).get("S")
    return None if value is None else str(value)


def classify_families(items: Iterable[Mapping[str, AttributeValueTypeDef]]) -> FamilyEnumeration:
    """Classify `WRITE_PLAN#` items into F1-F4 (pure; the caller supplies a complete read)."""
    claimed: list[tuple[str, str]] = []
    pending: list[str] = []
    persisted: set[tuple[str, str]] = set()
    planned: set[str] = set()
    units: list[tuple[str, str, str | None, str | None]] = []
    count = 0
    for item in items:
        count += 1
        partition = _text(item, "partition_key") or ""
        record = _text(item, "record_key") or ""
        if not partition.startswith(WRITE_PLAN_PREFIX):
            continue
        if INTENT_SEGMENT in partition:
            if record != META_RECORD_KEY:
                continue
            state = _text(item, "state")
            if state == IntentState.PENDING.value:
                pending.append(partition)
            elif state == IntentState.CLAIMED.value:
                claimed.append((partition, str(_text(item, "intent_id"))))
            continue
        if record == META_RECORD_KEY:
            persisted.add((str(_text(item, "plan_digest")), str(_text(item, "origin_intent_id"))))
            if _text(item, "status") == PlanStatus.PLANNED.value:
                planned.add(partition)
        elif record.startswith(UNIT_RECORD_PREFIX):
            units.append((partition, record, _text(item, "admission"), _text(item, "status")))
    unpersisted = [
        partition
        for partition, intent_id in claimed
        if (partition.removeprefix(WRITE_PLAN_PREFIX).split("#", 1)[0], intent_id) not in persisted
    ]
    pending_admission = sorted(
        {
            partition
            for partition, _, admission, _ in units
            if partition in planned and admission == PlanUnitAdmission.PENDING.value
        }
    )
    unfinalized = [
        f"{partition}/{record}"
        for partition, record, admission, status in units
        if partition in planned
        and admission == PlanUnitAdmission.ADMITTED.value
        and status not in _UNIT_DONE
    ]
    return FamilyEnumeration(
        counts=FamilyCounts(
            pending_intents=len(pending),
            claimed_unpersisted_intents=len(unpersisted),
            plans_with_pending_admission=len(pending_admission),
            admitted_unfinalized_units=len(unfinalized),
        ),
        items_read=count,
        pages=0,
        open_keys={
            "F1": tuple(sorted(pending)[:_SAMPLE]),
            "F2": tuple(sorted(unpersisted)[:_SAMPLE]),
            "F3": tuple(pending_admission[:_SAMPLE]),
            "F4": tuple(sorted(unfinalized)[:_SAMPLE]),
        },
    )


class DynamoLegacyFamilies:
    """Consistent base-table reads of `DRAIN_SEQ` and the F1-F4 families."""

    def __init__(self, publications_table: str, client: DynamoDBClient) -> None:
        self._table = publications_table
        self._client = client

    @property
    def table(self) -> str:
        """The coordinator table the `GRAPH` CAS ConditionChecks `DRAIN_SEQ` in."""
        return self._table

    def read_drain_seq(self) -> int:
        """Read `DRAIN_SEQ.seq` strongly; an absent item reads 0."""
        try:
            found = self._client.get_item(
                TableName=self._table,
                Key={
                    "partition_key": {"S": DRAIN_SEQ_PARTITION_KEY},
                    "record_key": {"S": DRAIN_SEQ_RECORD_KEY},
                },
                ConsistentRead=True,
            )
        except (BotoCoreError, ClientError) as error:
            raise ZeroProofUnreadableError(
                f"DRAIN_SEQ could not be read: {type(error).__name__}"
            ) from None
        item = found.get("Item")
        if item is None:
            return 0
        value = item.get("seq", {}).get("N")
        if value is None:
            raise ZeroProofUnreadableError("DRAIN_SEQ carries no seq.")
        return int(str(value))

    def enumerate_families(self) -> FamilyEnumeration:
        """Run one complete consistent Scan of the `WRITE_PLAN#` partitions and classify it."""
        items: list[Mapping[str, AttributeValueTypeDef]] = []
        start: dict[str, AttributeValueTypeDef] | None = None
        pages = 0
        while True:
            pages += 1
            if pages > MAX_SCAN_PAGES:
                raise ZeroProofUnreadableError("the enumeration exceeds its page bound.")
            arguments: dict[str, object] = {
                "TableName": self._table,
                "ConsistentRead": True,
                "FilterExpression": "begins_with(partition_key, :prefix)",
                "ExpressionAttributeValues": {":prefix": {"S": WRITE_PLAN_PREFIX}},
            }
            if start is not None:
                arguments["ExclusiveStartKey"] = start
            try:
                page = self._client.scan(**cast("dict[str, object]", arguments))  # type: ignore[arg-type]
            except (BotoCoreError, ClientError) as error:
                raise ZeroProofUnreadableError(
                    f"the enumeration could not be read: {type(error).__name__}"
                ) from None
            items.extend(page.get("Items", []))
            last = page.get("LastEvaluatedKey")
            if not last:
                break
            start = dict(last)
        found = classify_families(items)
        return FamilyEnumeration(found.counts, found.items_read, pages, found.open_keys)
