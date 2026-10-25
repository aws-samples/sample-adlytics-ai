# SPDX-License-Identifier: MIT-0
"""The chunked two-pass exact snapshot: an accepted vector is the table at one instant (ADR-070).

An exact read past 100 partitions reads its heads (A), immutables (B) and heads again (C) in
transactions of at most 100 items. If a chunked read could accept a vector that never existed at one
instant, a bake or a chat answer would bind publications that never coexisted and could state a
delta no source delivered. Each case runs the real adapter over an in-memory transactional table and
injects forward-only head advances between transactions; the expected outcome is predicted from the
transaction positions alone.
"""

from __future__ import annotations

# The item renderer is the store's own private seam, so the fake holds the adapter's bytes.
# pyright: reportPrivateUsage=false
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from functools import cache
from typing import Any, cast

import pytest
from dynamodb_reserved_words import guard_reserved_words
from publication_support import _PublicationSpec, successful_publication

from adapters.outbound.dynamodb.semantic_state import (
    PARTITION_KEY,
    PUBLICATION_RECORD_KEY,
    PUBLICATION_UNREADABLE,
    DynamoCurrentPublications,
    DynamoPublicationStore,
    DynamoPublicationStoreAccess,
    _publication_item,
)
from domain.semantics.publication import PartitionIdentity, SuccessfulPublication
from domain.shared_kernel.errors import DependencyError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-070")]

TABLE = "fictional-publications"
AT = datetime(2026, 10, 25, 9, tzinfo=UTC)
DAY_ONE = date(2024, 1, 1)
CHUNK = 100
"""DynamoDB's `TransactGetItems` item cap, which the fake enforces as the service does."""
SIZES = (1, 100, 101, 184, 1_000)


@cache
def _publication(index: int, token: int) -> SuccessfulPublication:
    day = DAY_ONE + timedelta(days=index)
    return replace(
        successful_publication("unit_dataset", day, _PublicationSpec(f"v{token}-{day}", AT)),
        fencing_token=token,
    )


@cache
def _current_item(index: int, token: int) -> dict[str, Any]:
    """Render a servable `CURRENT` whose authority and projection name one token (never mutated)."""
    publication = _publication(index, token)
    item: dict[str, Any] = dict(_publication_item(publication, current=True))
    item["projected_publication_id"] = {"S": publication.publication_id.value}
    item["projected_fencing_token"] = {"N": str(token)}
    item["projected_at"] = {"S": AT.isoformat()}
    return item


@cache
def _immutable_item(index: int, token: int) -> dict[str, Any]:
    return dict(_publication_item(_publication(index, token), current=False))


@guard_reserved_words
class _Table:
    """Ordered transactional reads over an in-memory table, with a hook after each transaction."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls = 0
        self.after_call: Callable[[int], None] | None = None

    def transact_get_items(self, **request: Any) -> dict[str, Any]:
        gets = cast("list[dict[str, Any]]", request["TransactItems"])
        keys = [
            (get["Get"]["Key"][PARTITION_KEY]["S"], get["Get"]["Key"][PUBLICATION_RECORD_KEY]["S"])
            for get in gets
        ]
        # The service refuses an empty, oversized or self-duplicating transaction.
        assert 1 <= len(gets) <= CHUNK
        assert len(set(keys)) == len(keys)
        assert all(get["Get"]["TableName"] == TABLE for get in gets)
        responses = [
            {} if (item := self.items.get(key)) is None else {"Item": item} for key in keys
        ]
        self.calls += 1
        if self.after_call is not None:
            self.after_call(self.calls)
        return {"Responses": responses}


@dataclass
class _World:
    """N daily partitions at token 1, and the head tokens as they move."""

    size: int
    table: _Table = field(default_factory=_Table)
    tokens: list[int] = field(default_factory=list[int])
    states: dict[int, tuple[int, ...]] = field(default_factory=dict[int, tuple[int, ...]])
    pauses: list[int] = field(default_factory=list[int])

    def __post_init__(self) -> None:
        self.tokens = [1] * self.size
        for index in range(self.size):
            self._write(index, 1)

    @property
    def partitions(self) -> tuple[PartitionIdentity, ...]:
        return tuple(_publication(index, 1).partition for index in range(self.size))

    def _write(self, index: int, token: int) -> None:
        identity = _publication(index, 1).partition.identity
        immutable = _immutable_item(index, token)
        self.table.items[(identity, immutable[PUBLICATION_RECORD_KEY]["S"])] = immutable
        self.table.items[(identity, "CURRENT")] = _current_item(index, token)

    def advance(self, index: int) -> None:
        """Move one head forward, immutable first, as every publication path writes."""
        self.tokens[index] += 1
        self._write(index, self.tokens[index])

    def schedule(self, advances: tuple[tuple[int, int], ...]) -> None:
        """Apply each (gap, partition) advance; gap g lands after transaction g (0: before any)."""
        for gap, index in advances:
            if gap == 0:
                self.advance(index)
        self.states[0] = tuple(self.tokens)

        def after(call: int) -> None:
            for gap, index in advances:
                if gap == call:
                    self.advance(index)
            self.states[call] = tuple(self.tokens)

        self.table.after_call = after

    def authority(self) -> tuple[SuccessfulPublication | None, ...]:
        access = DynamoPublicationStoreAccess(client=cast("Any", self.table))
        store = DynamoPublicationStore(TABLE, "", access)
        return store.inspect_exact(self.partitions).publications

    def serving(self) -> tuple[SuccessfulPublication | None, ...]:
        reader = DynamoCurrentPublications(
            TABLE, cast("Any", self.table), retry_pause=lambda: self.pauses.append(self.table.calls)
        )
        return reader.inspect_exact(self.partitions).publications


def _chunks(size: int) -> int:
    return -(-size // CHUNK)


def _predicted_refusal(chunks: int, advances: tuple[tuple[int, int], ...], offset: int = 0) -> bool:
    """Whether some partition's A and C transactions see different heads (positions only).

    One attempt is A in transactions 1..k, B in k+1..2k (every head names a publication) and C in
    2k+1..3k. Partition p's chunk c is read by A in transaction c+1 and by C in 2k+c+1, so an
    advance at gap g is seen by A when g <= c and by C when g <= 2k+c.
    """
    for index in {index for _, index in advances}:
        chunk = index // CHUNK
        gaps = [gap - offset for gap, moved in advances if moved == index]
        seen_by_a = sum(gap <= chunk for gap in gaps)
        seen_by_c = sum(gap <= 2 * chunks + chunk for gap in gaps)
        if seen_by_a != seen_by_c:
            return True
    return False


def _tokens(vector: tuple[SuccessfulPublication | None, ...]) -> tuple[int, ...]:
    assert all(publication is not None for publication in vector)
    return tuple(cast("SuccessfulPublication", item).fencing_token for item in vector)


def _edge_partitions(size: int) -> tuple[int, ...]:
    """Return the first and last partition, and the two either side of the first chunk boundary."""
    return tuple(sorted({index for index in (0, CHUNK - 1, CHUNK, size - 1) if index < size}))


def _edge_gaps(chunks: int) -> tuple[int, ...]:
    """Each side of every edge of the first and last chunk's A-to-C windows.

    Partition p in chunk c refuses for an advance at gap g exactly when c < g <= 2k + c, so these
    gaps reach every combination of the first and last partition's outcomes.
    """
    k = chunks
    return tuple(sorted({0, 1, k - 1, k, 2 * k, 2 * k + 1, 3 * k - 1, 3 * k}))


@pytest.mark.parametrize("size", SIZES)
def test_one_advance_at_every_gap_refuses_exactly_inside_its_window_and_else_is_one_instant(
    size: int,
) -> None:
    chunks = _chunks(size)
    outcomes = {"accepted": 0, "refused": 0}
    for gap, index in itertools.product(range(3 * chunks + 1), _edge_partitions(size)):
        world = _World(size)
        advances = ((gap, index),)
        world.schedule(advances)
        if _predicted_refusal(chunks, advances):
            with pytest.raises(DependencyError) as refused:
                world.authority()
            assert refused.value.code == PUBLICATION_UNREADABLE
            assert refused.value.context == {
                "phase": "verify",
                "partitions": size,
                "attempts": 1,
            }
            outcomes["refused"] += 1
        else:
            vector = world.authority()
            # (a) the table as it stood between the last A and the first C transaction.
            assert _tokens(vector) == world.states[chunks]
            assert tuple(item.partition for item in vector if item) == world.partitions
            outcomes["accepted"] += 1
        assert world.table.calls == 3 * chunks
    # Both outcomes occur at every size, so neither branch passes vacuously.
    assert outcomes["accepted"] > 0 and outcomes["refused"] > 0


@pytest.mark.parametrize("size", SIZES)
def test_two_advances_at_every_pair_of_window_edges_refuse_or_accept_one_instant(size: int) -> None:
    chunks = _chunks(size)
    first, last = 0, size - 1
    accepted = 0
    for (gap_one, gap_two), (one, two) in itertools.product(
        itertools.combinations_with_replacement(_edge_gaps(chunks), 2),
        itertools.product((first, last), repeat=2),
    ):
        world = _World(size)
        advances = ((gap_one, one), (gap_two, two))
        world.schedule(advances)
        if _predicted_refusal(chunks, advances):
            with pytest.raises(DependencyError) as refused:
                world.authority()
            assert refused.value.code == PUBLICATION_UNREADABLE
            continue
        assert _tokens(world.authority()) == world.states[chunks]
        accepted += 1
    assert accepted > 0


@pytest.mark.parametrize("size", SIZES)
def test_a_serving_read_retries_a_change_at_each_window_edge_and_accepts_the_next_stable_vector(
    size: int,
) -> None:
    chunks = _chunks(size)
    for gap, index in itertools.product(_edge_gaps(chunks), (0, size - 1)):
        world = _World(size)
        world.schedule(((gap, index),))
        vector = _tokens(world.serving())
        if _predicted_refusal(chunks, ((gap, index),)):
            # (c) attempt 2 reads the moved head in both passes.
            assert world.pauses == [3 * chunks]
            assert vector[index] == 2 and world.table.calls == 6 * chunks
        else:
            assert world.pauses == []
            assert world.table.calls == 3 * chunks
        assert vector == world.states[world.table.calls - 2 * chunks]


@pytest.mark.parametrize("size", SIZES)
def test_a_serving_read_refuses_after_three_changed_attempts_and_accepts_after_two(
    size: int,
) -> None:
    chunks = _chunks(size)
    # One advance just after each attempt's first A transaction lands inside partition 0's window.
    inside = tuple((3 * chunks * attempt + 1, 0) for attempt in range(3))
    world = _World(size)
    world.schedule(inside)
    with pytest.raises(DependencyError) as refused:
        world.serving()
    assert refused.value.code == PUBLICATION_UNREADABLE
    assert refused.value.context == {"phase": "verify", "partitions": size, "attempts": 3}
    assert len(world.pauses) == 2
    # Paired: two changed attempts, then a stable third, is accepted at the third.
    world = _World(size)
    world.schedule(inside[:2])
    vector = _tokens(world.serving())
    assert vector[0] == 3 and len(world.pauses) == 2
    assert world.table.calls == 9 * chunks
