# SPDX-License-Identifier: MIT-0
"""Reader resolution through the window generation pointer (design §2.6, slice 4a).

Every serving read resolves the projected base plus each `staged#<grant_id>` entry its
window pointer covers, through one verified A/P/B/C/P' vector with a bounded retry. These
tests run the real adapter over an in-memory DynamoDB that can inject a write after any
transactional read, so each protocol interleaving is exercised exactly.
"""

from __future__ import annotations

# The envelope guards and the replay check are private attestation seams exercised directly.
# pyright: reportPrivateUsage=false
import copy
import json
import random
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from dynamodb_reserved_words import guard_reserved_words
from publication_support import _PublicationSpec, successful_publication

from adapters.outbound.dynamodb.semantic_state import (
    ATTEMPT_EVENT_KEY,
    ATTEMPT_KEY,
    COVERAGE_SCOPE,
    CURRENT_RECORD,
    GENERATION_POINTER_RECORD,
    PARTITION_KEY,
    PUBLICATION_RECORD_KEY,
    PUBLICATION_UNREADABLE,
    DynamoCurrentPublications,
    DynamoPublicationStore,
    DynamoPublicationStoreAccess,
    GraphCurrentBase,
    StagedEntry,
    _attempt_item,
    _publication_item,
    _require_envelope_fields,
    _started_for_success,
    generation_pointer_partition,
    graph_current_base,
    staged_attribute,
)
from domain.semantics.publication import (
    AttemptEvent,
    AttemptEventKind,
    PartitionIdentity,
    SuccessfulPublication,
)
from domain.shared_kernel.errors import DependencyError, InvariantError
from ports.outbound.semantic_state import CommitPublicationGroupRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

TABLE = "publications"
ATTEMPTS = "attempts"
AT = datetime(2026, 10, 9, 12, tzinfo=UTC)
DAY_ONE = date(2026, 9, 1)
DAY_TWO = date(2026, 9, 2)
_UNSTAMPED = "unstamped"


def _publication(day: date, name: str, token: int) -> SuccessfulPublication:
    return replace(
        successful_publication("unit_dataset", day, _PublicationSpec(f"{name}-{day}", AT)),
        fencing_token=token,
    )


def _key(table: str, key: dict[str, Any]) -> tuple[str, str]:
    return table, json.dumps(key, sort_keys=True)


def _kind(get: dict[str, Any]) -> str:
    record = get["Get"]["Key"].get(PUBLICATION_RECORD_KEY, {}).get("S", "")
    if record == CURRENT_RECORD:
        return "current"
    if record == GENERATION_POINTER_RECORD:
        return "pointer"
    if record.startswith("PUBLICATION#"):
        return "immutable"
    return "other"


@guard_reserved_words
class _FakeDynamo:
    """Transactional reads and the GSI over an in-memory table, with a post-read hook."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[str] = []
        self.queries = 0
        self.after_call: Callable[[int, str], None] | None = None

    def put(self, item: dict[str, Any], table: str = TABLE) -> None:
        names = (
            (PARTITION_KEY, PUBLICATION_RECORD_KEY)
            if table == TABLE
            else (ATTEMPT_KEY, ATTEMPT_EVENT_KEY)
        )
        self.items[_key(table, {name: item[name] for name in names})] = copy.deepcopy(item)

    def current(self, partition: PartitionIdentity) -> dict[str, Any]:
        return self.items[
            _key(
                TABLE,
                {
                    PARTITION_KEY: {"S": partition.identity},
                    PUBLICATION_RECORD_KEY: {"S": CURRENT_RECORD},
                },
            )
        ]

    def transact_get_items(self, **request: Any) -> dict[str, Any]:
        gets = cast("list[dict[str, Any]]", request["TransactItems"])
        assert 1 <= len(gets) <= 100
        responses: list[dict[str, Any]] = []
        for get in gets:
            item = self.items.get(_key(get["Get"]["TableName"], get["Get"]["Key"]))
            responses.append({} if item is None else {"Item": copy.deepcopy(item)})
        self.calls.append(_kind(gets[0]))
        if self.after_call is not None:
            self.after_call(len(self.calls), self.calls[-1])
        return {"Responses": responses}

    def query(self, **request: Any) -> dict[str, Any]:
        self.queries += 1
        scope = request["ExpressionAttributeValues"][":scope"]
        return {
            "Items": [
                {PARTITION_KEY: item[PARTITION_KEY]}
                for (table, _), item in self.items.items()
                if table == TABLE and item.get(COVERAGE_SCOPE) == scope
            ]
        }


@dataclass
class _World:
    fake: _FakeDynamo
    pauses: list[int]
    reader: DynamoCurrentPublications


def _world() -> _World:
    fake = _FakeDynamo()
    pauses: list[int] = []
    reader = DynamoCurrentPublications(
        TABLE, client=cast("Any", fake), retry_pause=lambda: pauses.append(1)
    )
    return _World(fake, pauses, reader)


def _seed(
    world: _World,
    publications: tuple[SuccessfulPublication, ...],
    projected: SuccessfulPublication | str | None,
) -> None:
    """Store every immutable and a CURRENT whose authority is the largest token."""
    for publication in publications:
        world.fake.put(_publication_item(publication, current=False))
    authority = max(publications, key=lambda publication: publication.fencing_token)
    current: dict[str, Any] = dict(_publication_item(authority, current=True))
    if projected != _UNSTAMPED:
        selected = cast("SuccessfulPublication | None", projected)
        current["projected_publication_id"] = {
            "S": "NONE" if selected is None else selected.publication_id.value
        }
        current["projected_fencing_token"] = {
            "N": "0" if selected is None else str(selected.fencing_token)
        }
        current["projected_at"] = {"S": AT.isoformat()}
    world.fake.put(current)


def _stage(world: _World, publication: SuccessfulPublication, grant: tuple[str, int]) -> None:
    window, ordinal = grant
    world.fake.current(publication.partition)[staged_attribute(f"grant-{window}-{ordinal}")] = {
        "M": {
            "publication_id": {"S": publication.publication_id.value},
            "fencing_token": {"N": str(publication.fencing_token)},
            "conflict_unit": {"S": window},
            "grant_ordinal": {"N": str(ordinal)},
            "staged_at": {"S": AT.isoformat()},
        }
    }


def _flip(world: _World, window: str, ordinal: int) -> None:
    world.fake.put(
        {
            PARTITION_KEY: {"S": generation_pointer_partition(window)},
            PUBLICATION_RECORD_KEY: {"S": GENERATION_POINTER_RECORD},
            "item_kind": {"S": "generation_pointer"},
            "schema_version": {"N": "1"},
            "grant_id": {"S": f"grant-{window}-{ordinal}"},
            "grant_ordinal": {"N": str(ordinal)},
        }
    )


def _fold(world: _World, partition: PartitionIdentity, grant: tuple[str, int]) -> None:
    """Fold exactly as §2.6 step 3: raise the base when the entry beats it, then remove."""
    window, ordinal = grant
    current = world.fake.current(partition)
    entry = current.pop(staged_attribute(f"grant-{window}-{ordinal}"), None)
    if entry is None:
        return
    token = int(entry["M"]["fencing_token"]["N"])
    base = current["projected_publication_id"]["S"]
    if base == "NONE" or int(current["projected_fencing_token"]["N"]) < token:
        current["projected_publication_id"] = entry["M"]["publication_id"]
        current["projected_fencing_token"] = entry["M"]["fencing_token"]
        current["projected_at"] = {"S": AT.isoformat()}


def _commit_authority(world: _World, publication: SuccessfulPublication) -> None:
    """Advance authority only, as a supersede does: servable attributes are never named."""
    world.fake.put(_publication_item(publication, current=False))
    current = world.fake.current(publication.partition)
    current.update(_publication_item(publication, current=True))


def _read(
    world: _World, target: str, partitions: tuple[PartitionIdentity, ...]
) -> tuple[SuccessfulPublication | None, ...]:
    if target == "current":
        return tuple(world.reader.current(partition) for partition in partitions)
    if target == "inspect_exact":
        return world.reader.inspect_exact(partitions).publications
    found = {
        publication.partition: publication
        for publication in world.reader.current_for_scope(partitions[0].scope)
    }
    return tuple(found.get(partition) for partition in partitions)


def _hook(world: _World, at_call: int, write: Callable[[], None]) -> None:
    def after(index: int, _kind: str) -> None:
        if index == at_call:
            write()

    world.fake.after_call = after


READERS = ("current", "inspect_exact", "current_for_scope")


@dataclass(frozen=True)
class _Window:
    """Two partitions; base token 1, window W staged at token 2 (authority)."""

    one: tuple[SuccessfulPublication, SuccessfulPublication]
    two: tuple[SuccessfulPublication, SuccessfulPublication]

    @property
    def partitions(self) -> tuple[PartitionIdentity, PartitionIdentity]:
        return self.one[0].partition, self.two[0].partition

    @property
    def old(self) -> tuple[SuccessfulPublication, SuccessfulPublication]:
        return self.one[0], self.two[0]

    @property
    def new(self) -> tuple[SuccessfulPublication, SuccessfulPublication]:
        return self.one[1], self.two[1]


def _staged_window(world: _World) -> _Window:
    window = _Window(
        (_publication(DAY_ONE, "base", 1), _publication(DAY_ONE, "w", 2)),
        (_publication(DAY_TWO, "base", 1), _publication(DAY_TWO, "w", 2)),
    )
    for old, new in (window.one, window.two):
        _seed(world, (old, new), old)
        _stage(world, new, ("W", 1))
    return window


class TestStagedVisibility:
    """AGG3-2 reader half."""

    @pytest.mark.parametrize("target", READERS)
    def test_a_staged_unflipped_entry_is_invisible(self, target: str) -> None:
        world = _world()
        window = _staged_window(world)

        assert _read(world, target, window.partitions) == window.old

    @pytest.mark.parametrize("target", READERS)
    def test_control_after_the_pointer_covers_it_every_target_resolves_new(
        self, target: str
    ) -> None:
        world = _world()
        window = _staged_window(world)
        _flip(world, "W", 1)

        assert _read(world, target, window.partitions) == window.new

    def test_a_pointer_below_the_entry_ordinal_does_not_cover_it(self) -> None:
        world = _world()
        window = _staged_window(world)
        for old, new in (window.one, window.two):
            del world.fake.current(old.partition)[staged_attribute("grant-W-1")]
            _stage(world, new, ("W", 2))
        _flip(world, "W", 1)

        assert _read(world, "inspect_exact", window.partitions) == window.old

    @pytest.mark.parametrize("at_call", (1, 2, 3, 4, 5))
    def test_one_window_resolves_all_old_or_all_new_across_an_injected_flip(
        self, at_call: int
    ) -> None:
        world = _world()
        window = _staged_window(world)
        _hook(world, at_call, lambda: _flip(world, "W", 1))

        resolved = world.reader.inspect_exact(window.partitions).publications

        assert resolved in {window.old, window.new}

    def test_a_none_base_with_a_covered_entry_resolves_the_entry(self) -> None:
        world = _world()
        publication = _publication(DAY_ONE, "w", 1)
        _seed(world, (publication,), None)
        _stage(world, publication, ("W", 1))

        assert world.reader.current(publication.partition) is None
        _flip(world, "W", 1)
        assert world.reader.current(publication.partition) == publication


def _fold_property_case(seed: int) -> None:
    """Random stage/flip/fold orders over two windows that share both partitions."""
    rng = random.Random(seed)  # noqa: S311 -- a seeded test order, not a secret
    world = _world()
    base = (_publication(DAY_ONE, "base", 1), _publication(DAY_TWO, "base", 1))
    # Window A wins day one and window B wins day two, so neither window dominates.
    tokens = {("A", 0): 2, ("B", 0): 3, ("A", 1): 3, ("B", 1): 2}
    staged = {
        (window, index): _publication((DAY_ONE, DAY_TWO)[index], window, token)
        for (window, index), token in tokens.items()
    }
    ordinals = {"A": 1, "B": 2}
    for index, old in enumerate(base):
        _seed(world, (old, staged["A", index], staged["B", index]), old)
    plans: dict[str, list[tuple[str, int]]] = {}
    for window in ("A", "B"):
        stages = [("stage", 0), ("stage", 1)]
        folds = [("fold", 0), ("fold", 1)]
        rng.shuffle(stages)
        rng.shuffle(folds)
        plans[window] = [*stages, ("flip", -1), *folds]
    flipped: set[str] = set()
    partitions = tuple(publication.partition for publication in base)

    def expected() -> tuple[SuccessfulPublication, ...]:
        return tuple(
            max(
                (old, *(staged[window, index] for window in sorted(flipped))),
                key=lambda publication: publication.fencing_token,
            )
            for index, old in enumerate(base)
        )

    before = world.reader.inspect_exact(partitions).publications
    while plans["A"] or plans["B"]:
        window = rng.choice([name for name in ("A", "B") if plans[name]])
        operation, index = plans[window].pop(0)
        if operation == "stage":
            _stage(world, staged[window, index], (window, ordinals[window]))
        elif operation == "flip":
            _flip(world, window, ordinals[window])
            flipped.add(window)
        else:
            _fold(world, partitions[index], (window, ordinals[window]))
        after = world.reader.inspect_exact(partitions).publications
        if operation == "fold":
            assert after == before, (seed, window, index)
        assert after == expected(), (seed, operation, window, index)
        before = after
    assert before == (staged["B", 0], staged["A", 1])
    for partition in partitions:
        assert not [name for name in world.fake.current(partition) if name.startswith("staged#")]


@pytest.mark.parametrize("seed", range(60))
def test_property_a_fold_never_changes_resolution(seed: int) -> None:
    _fold_property_case(seed)


class TestSnapshotProtocol:
    """AGG4-2: resolved-vector comparison and the bounded retry."""

    @staticmethod
    def _flipped_window(world: _World) -> _Window:
        window = _staged_window(world)
        _flip(world, "W", 1)
        return window

    def test_an_authority_only_commit_between_a_and_c_is_accepted_on_attempt_one(
        self,
    ) -> None:
        world = _world()
        window = self._flipped_window(world)
        later = _publication(DAY_ONE, "later", 3)
        _hook(world, 1, lambda: _commit_authority(world, later))

        assert world.reader.inspect_exact(window.partitions).publications == window.new
        assert world.fake.calls == ["current", "pointer", "immutable", "current", "pointer"]
        assert world.pauses == []

    def test_another_windows_staging_write_between_a_and_c_is_accepted_on_attempt_one(
        self,
    ) -> None:
        world = _world()
        window = self._flipped_window(world)
        other = _publication(DAY_ONE, "other", 3)
        _commit_authority(world, other)
        _hook(world, 1, lambda: _stage(world, other, ("OTHER", 5)))

        assert world.reader.inspect_exact(window.partitions).publications == window.new
        assert len(world.fake.calls) == 5
        assert world.pauses == []

    def test_a_fold_between_a_and_c_is_accepted_on_attempt_one(self) -> None:
        world = _world()
        window = self._flipped_window(world)
        _hook(world, 1, lambda: _fold(world, window.partitions[0], ("W", 1)))

        assert world.reader.inspect_exact(window.partitions).publications == window.new
        assert len(world.fake.calls) == 5
        assert world.pauses == []

    @pytest.mark.parametrize("at_call", (3, 4))
    def test_control_a_flip_between_p_and_p_prime_refuses_that_attempt(self, at_call: int) -> None:
        world = _world()
        window = _staged_window(world)
        _hook(world, at_call, lambda: _flip(world, "W", 1))

        assert world.reader.inspect_exact(window.partitions).publications == window.new
        assert world.pauses == [1]
        assert len(world.fake.calls) == 10

    def test_p_prime_covers_windows_named_only_in_c(self) -> None:
        """Entries staged and flipped after A are named only by C; P' must still read them."""
        world = _world()
        window = _Window(
            (_publication(DAY_ONE, "base", 1), _publication(DAY_ONE, "w", 2)),
            (_publication(DAY_TWO, "base", 1), _publication(DAY_TWO, "w", 2)),
        )
        for old, new in (window.one, window.two):
            _seed(world, (old, new), old)

        def stage_and_flip() -> None:
            for publication in window.new:
                _stage(world, publication, ("W", 1))
            _flip(world, "W", 1)

        _hook(world, 1, stage_and_flip)

        assert world.reader.inspect_exact(window.partitions).publications == window.new
        assert world.fake.calls[:4] == ["current", "immutable", "current", "pointer"]
        assert world.pauses == [1]

    @staticmethod
    def _three_grants(world: _World, grants: int) -> list[SuccessfulPublication]:
        """One window staged by `grants` successive grants (ordinals 1..n, tokens 2..n+1)."""
        old = _publication(DAY_ONE, "base", 1)
        later = [_publication(DAY_ONE, f"g{ordinal}", ordinal + 1) for ordinal in range(1, 4)]
        _seed(world, (old, *later[:grants]), old)
        for ordinal, publication in enumerate(later[:grants], start=1):
            _stage(world, publication, ("W", ordinal))
        return [old, *later]

    @staticmethod
    def _flip_inside_each_attempt(world: _World, flips: int) -> None:
        """Flip the window to the next ordinal between P and P' of the first attempts."""
        count = {"attempt": 0}

        def after(_index: int, kind: str) -> None:
            if kind == "immutable":
                count["attempt"] += 1
                if count["attempt"] <= flips:
                    _flip(world, "W", count["attempt"])

        world.fake.after_call = after

    def test_flips_in_two_attempts_succeed_on_the_third(self) -> None:
        world = _world()
        publications = self._three_grants(world, 2)
        self._flip_inside_each_attempt(world, 2)

        assert world.reader.current(publications[0].partition) == publications[2]
        assert world.pauses == [1, 1]

    def test_flips_in_three_attempts_raise_head_vector_changed(self) -> None:
        world = _world()
        publications = self._three_grants(world, 3)
        self._flip_inside_each_attempt(world, 3)

        with pytest.raises(DependencyError, match="head vector changed") as refused:
            world.reader.current(publications[0].partition)

        assert type(refused.value) is DependencyError
        assert refused.value.code == PUBLICATION_UNREADABLE
        assert refused.value.context["attempts"] == 3
        assert world.pauses == [1, 1]

    def test_current_for_scope_with_a_concurrent_unrelated_commit_returns(self) -> None:
        world = _world()
        window = self._flipped_window(world)
        later = _publication(DAY_TWO, "later", 3)
        _hook(world, 1, lambda: _commit_authority(world, later))

        assert world.reader.current_for_scope(window.partitions[0].scope) == window.new
        assert world.pauses == []

    def test_current_for_scope_discovers_keys_once_and_chunks_at_one_hundred(self) -> None:
        world = _world()
        days = [date(2026, 1, 1).toordinal() + offset for offset in range(101)]
        for day in days:
            _seed(world, (_publication(date.fromordinal(day), "p", 1),), None)
        first = _publication(date.fromordinal(days[0]), "p", 1)
        _seed(world, (first,), first)

        assert world.reader.current_for_scope(first.partition.scope) == (first,)
        assert world.fake.queries == 1
        assert world.fake.calls == ["current", "current", "immutable", "current", "current"]


class TestAbsentProjection:
    """AGG4-3 / AGG4-17 reader half: no pre-migration rule on serving reads."""

    @pytest.mark.parametrize("target", READERS)
    def test_an_item_without_projected_attributes_is_unreadable(self, target: str) -> None:
        world = _world()
        publication = _publication(DAY_ONE, "legacy", 1)
        _seed(world, (publication,), _UNSTAMPED)

        with pytest.raises(DependencyError, match="head vector could not be verified") as refused:
            _read(world, target, (publication.partition,))

        assert type(refused.value) is DependencyError
        assert refused.value.code == PUBLICATION_UNREADABLE
        assert world.pauses == []

    @pytest.mark.parametrize("target", READERS)
    def test_control_a_none_projection_resolves_to_absence(self, target: str) -> None:
        world = _world()
        publication = _publication(DAY_ONE, "unprojected", 1)
        _seed(world, (publication,), None)

        assert _read(world, target, (publication.partition,)) == (None,)

    def test_an_authority_read_still_resolves_an_unstamped_item(self) -> None:
        world = _world()
        publication = _publication(DAY_ONE, "legacy", 1)
        _seed(world, (publication,), _UNSTAMPED)
        store = DynamoPublicationStore(
            TABLE, "", DynamoPublicationStoreAccess(client=cast("Any", world.fake))
        )

        assert store.inspect_exact((publication.partition,)).publications == (publication,)


class TestGraphCurrentBase:
    """The classification helper slice 4b's staging and fold use (InvariantError)."""

    def test_an_item_without_projected_attributes_is_an_invariant(self) -> None:
        world = _world()
        publication = _publication(DAY_ONE, "legacy", 1)
        _seed(world, (publication,), _UNSTAMPED)

        with pytest.raises(InvariantError, match="no servable projection") as refused:
            graph_current_base(
                publication.partition.identity, world.fake.current(publication.partition)
            )

        assert type(refused.value) is InvariantError
        assert refused.value.code == PUBLICATION_UNREADABLE

    def test_an_absent_item_is_an_invariant(self) -> None:
        with pytest.raises(InvariantError, match="no CURRENT item") as refused:
            graph_current_base("partition-1", None)

        assert refused.value.code == PUBLICATION_UNREADABLE

    def test_a_malformed_staged_entry_is_an_invariant(self) -> None:
        world = _world()
        publication = _publication(DAY_ONE, "w", 1)
        _seed(world, (publication,), None)
        current = world.fake.current(publication.partition)
        current[staged_attribute("grant-1")] = {"M": {"publication_id": {"S": "w"}}}

        with pytest.raises(InvariantError, match="malformed") as refused:
            graph_current_base(publication.partition.identity, current)

        assert refused.value.code == PUBLICATION_UNREADABLE

    def test_control_a_stamped_item_classifies_with_its_staged_entries(self) -> None:
        world = _world()
        old = _publication(DAY_ONE, "base", 1)
        new = _publication(DAY_ONE, "w", 2)
        _seed(world, (old, new), None)
        _stage(world, new, ("W", 4))

        base = graph_current_base(old.partition.identity, world.fake.current(old.partition))

        assert base == GraphCurrentBase(
            partition_key=old.partition.identity,
            publication_id=new.publication_id.value,
            fencing_token=2,
            projected_publication_id=None,
            projected_fencing_token=0,
            staged=(
                StagedEntry(
                    grant_id="grant-W-4",
                    publication_id=new.publication_id.value,
                    fencing_token=2,
                    conflict_unit="W",
                    grant_ordinal=4,
                    staged_at=AT,
                ),
            ),
        )


class TestClosedEnvelopes:
    """`staged#*` is a non-authority attribute for every closed envelope."""

    @staticmethod
    def _stamped(world: _World) -> tuple[SuccessfulPublication, dict[str, Any]]:
        old = _publication(DAY_ONE, "base", 1)
        new = _publication(DAY_ONE, "w", 2)
        _seed(world, (old, new), new)
        _stage(world, new, ("W", 1))
        return new, world.fake.current(new.partition)

    def test_the_authority_envelope_accepts_staged_entries(self) -> None:
        world = _world()
        publication, current = self._stamped(world)

        _require_envelope_fields(current, _publication_item(publication, current=False))

    def test_control_the_authority_envelope_still_refuses_an_unknown_attribute(self) -> None:
        world = _world()
        publication, current = self._stamped(world)
        current["unknown"] = {"S": "x"}

        with pytest.raises(ValueError, match="missing or unexpected"):
            _require_envelope_fields(current, _publication_item(publication, current=False))

    def test_authority_reads_accept_staged_entries(self) -> None:
        world = _world()
        publication, _ = self._stamped(world)
        store = DynamoPublicationStore(
            TABLE, "", DynamoPublicationStoreAccess(client=cast("Any", world.fake))
        )

        assert store.inspect_exact((publication.partition,)).publications == (publication,)
        assert store.current_for_scope(publication.partition.scope) == (publication,)

    def test_control_authority_reads_refuse_an_unknown_attribute(self) -> None:
        world = _world()
        publication, current = self._stamped(world)
        current["unknown"] = {"S": "x"}
        store = DynamoPublicationStore(
            TABLE, "", DynamoPublicationStoreAccess(client=cast("Any", world.fake))
        )

        with pytest.raises(DependencyError) as refused:
            store.inspect_exact((publication.partition,))

        assert refused.value.code == PUBLICATION_UNREADABLE
        with pytest.raises(DependencyError) as serving:
            world.reader.current(publication.partition)
        assert serving.value.code == PUBLICATION_UNREADABLE

    @staticmethod
    def _replay(world: _World, publication: SuccessfulPublication) -> bool:
        event = AttemptEvent(
            attempt_id=publication.attempt_id,
            partition=publication.partition,
            kind=AttemptEventKind.SUCCEEDED,
            occurred_at=publication.published_at,
            sequence=1,
        )
        world.fake.put(_attempt_item(_started_for_success(event)), table=ATTEMPTS)
        world.fake.put(_attempt_item(event), table=ATTEMPTS)
        store = DynamoPublicationStore(
            TABLE,
            "",
            DynamoPublicationStoreAccess(attempts_table=ATTEMPTS, client=cast("Any", world.fake)),
        )
        request = SimpleNamespace(
            members=(SimpleNamespace(publication=publication, success_event=event),)
        )
        return store._exact_replay(cast("CommitPublicationGroupRequest", request))

    def test_exact_replay_accepts_staged_entries(self) -> None:
        world = _world()
        publication, _ = self._stamped(world)

        assert self._replay(world, publication) is True

    def test_control_exact_replay_refuses_an_unknown_attribute(self) -> None:
        world = _world()
        publication, current = self._stamped(world)
        current["unknown"] = {"S": "x"}

        assert self._replay(world, publication) is False

    def test_serving_reads_refuse_a_malformed_staged_entry(self) -> None:
        world = _world()
        publication, current = self._stamped(world)
        current[staged_attribute("grant-bad")] = {"M": {"publication_id": {"S": "x"}}}

        with pytest.raises(DependencyError) as refused:
            world.reader.current(publication.partition)

        assert refused.value.code == PUBLICATION_UNREADABLE

    def test_serving_reads_refuse_a_covered_entry_ahead_of_authority(self) -> None:
        world = _world()
        old = _publication(DAY_ONE, "base", 1)
        _seed(world, (old,), old)
        _stage(world, _publication(DAY_ONE, "ahead", 2), ("W", 1))
        assert world.reader.current(old.partition) == old  # control: uncovered, unread
        _flip(world, "W", 1)

        with pytest.raises(DependencyError) as refused:
            world.reader.current(old.partition)

        assert refused.value.code == PUBLICATION_UNREADABLE
