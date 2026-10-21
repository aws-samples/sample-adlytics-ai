# SPDX-License-Identifier: MIT-0
"""`publish` stages, flips one generation pointer, then folds (design §2.6, slice 4b).

Every test drives the real `DynamoGraphDispatchStore` and `DynamoPublicationStore` over one
transactional fake holding both tables (`graph_publish_support.Tables`), so the flip's
cross-table fences and the stage and fold conditions are evaluated exactly as written. Each
refusal asserts its exact error class and code beside a paired positive control.
"""

from __future__ import annotations

# The fakes and the store's private item renderer are exercised directly.
# pyright: reportPrivateUsage=false
import copy
import json
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words
from execution_graph_dispatch_support import TABLE, UNIT, kinds
from graph_publish_support import (
    NOW,
    PublishWorld,
    Tables,
    publication,
    publications_store,
    publish_world,
    seed_current,
)

from adapters.inbound.ingestion_handlers import graph_publish
from adapters.inbound.ingestion_handlers.graph_publish import (
    GRAPH_POST_GRANT_RETRY_EXHAUSTED,
    PUBLISH_INVARIANT,
    PublishInvariantError,
    PublishRetryExhaustedError,
)
from adapters.outbound.dynamodb.execution_graph import due_shard
from adapters.outbound.dynamodb.execution_graph_dispatch import (
    DynamoGraphDispatchStore,
    grant_fence_checks,
)
from adapters.outbound.dynamodb.semantic_state import (
    GENERATION_POINTER_RECORD,
    PUBLICATION_RECORD_KEY,
    FlipOutcome,
    FlipRequest,
    FoldOutcome,
    GenerationFlipBarrierError,
    GenerationFlipLostError,
    GrantStaging,
    PublishCounts,
    PublishTarget,
    StageClass,
    staged_attribute,
)
from domain.operations.execution_graph_dispatch import serving_frontier_shard
from domain.semantics.publication import SuccessfulPublication
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

DAY_A = date(2026, 9, 1)
DAY_B = date(2026, 9, 2)
DAY_Z = date(2026, 9, 3)


@dataclass
class _Seeded:
    world: PublishWorld
    a_old: SuccessfulPublication
    a_new: SuccessfulPublication
    b_new: SuccessfulPublication
    zero: SuccessfulPublication

    @property
    def fake(self) -> Tables:
        return self.world.fake

    @property
    def targets(self) -> tuple[SuccessfulPublication, ...]:
        return (self.a_new, self.b_new, self.zero)


@pytest.fixture
def errors(monkeypatch: pytest.MonkeyPatch) -> list[BaseException]:
    reported: list[BaseException] = []

    def record(error: BaseException, _report: object) -> None:
        reported.append(error)

    monkeypatch.setattr(graph_publish, "report_error", record)
    return reported


def _seeded(
    errors: list[BaseException], *, curation: bool = True, fake: Tables | None = None
) -> _Seeded:
    """Seed unit 1 advancing `a` over an older base, unit 2 seeding `b`, and zero-row `z`."""
    fake = fake or Tables()
    a_old = publication(DAY_A, "a-old", 1)
    a_new = publication(DAY_A, "a-new", 2, curation=curation)
    b_new = publication(DAY_B, "b-new", 1)
    zero = publication(DAY_Z, "z-zero", 1)
    seed_current(fake, a_new, a_old)
    seed_current(fake, b_new, None)
    seed_current(fake, zero, None)
    world = publish_world(
        units={"db.table_a": (a_new,), "db.table_b": (b_new,)}, zero_row=(zero,), fake=fake
    )
    del errors  # the fixture captures every report; the world does not need it
    return _Seeded(world, a_old, a_new, b_new, zero)


def _staged(fake: Tables, item: SuccessfulPublication) -> list[str]:
    return sorted(
        name for name in fake.current(item.partition.identity) if name.startswith("staged#")
    )


def _projected(fake: Tables, item: SuccessfulPublication) -> tuple[str, int]:
    current = fake.current(item.partition.identity)
    return (
        str(current["projected_publication_id"]["S"]),
        int(current["projected_fencing_token"]["N"]),
    )


def _grant(seeded: _Seeded) -> tuple[str, int]:
    run = seeded.world.store.read_run(seeded.world.run)
    assert run is not None and run.grant_id is not None and run.grant_ordinal is not None
    return run.grant_id, run.grant_ordinal


def _flip_request(seeded: _Seeded, **fences: Any) -> FlipRequest:
    grant_id, ordinal = _grant(seeded)
    claim = seeded.world.claim
    arguments: dict[str, Any] = {
        "run_id": seeded.world.run,
        "grant_id": grant_id,
        "grant_ordinal": ordinal,
        "node_id": claim.node_id,
        "lease_version": claim.lease_version,
    }
    arguments.update(fences)
    return FlipRequest(
        conflict_unit=UNIT,
        grant_id=grant_id,
        grant_ordinal=ordinal,
        run_id=seeded.world.run,
        root_flow_id="f" * 64,
        authority_epoch=1,
        retrieved_at=NOW.isoformat(),
        revision_epoch=1,
        counts=PublishCounts(3, 1, 0, 0),
        target_count=3,
        target_digest="d" * 64,
        flipped_at=NOW,
        fences=grant_fence_checks(TABLE, **arguments),
    )


def _stage_all(seeded: _Seeded) -> list[Any]:
    grant_id, ordinal = _grant(seeded)
    store = publications_store(seeded.fake)
    grant = GrantStaging(grant_id, ordinal, UNIT, NOW)
    return [
        store.stage_for_grant(
            grant,
            PublishTarget(item.partition.identity, item.publication_id.value, item.fencing_token),
            max_redecide=8,
        )
        for item in seeded.targets
    ]


# --- The whole step ---------------------------------------------------------------------------


def test_publish_stages_flips_folds_and_settles_with_its_counts(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)
    result = seeded.world.publish()
    assert (result.acknowledge, result.outcome) == (True, "graph_publish_succeeded")
    assert result.counts == PublishCounts(3, 1, 0, 0)
    pointer = seeded.fake.pointer()
    assert pointer is not None
    grant_id, ordinal = _grant(seeded)
    assert (pointer["grant_id"]["S"], pointer["grant_ordinal"]["N"]) == (grant_id, str(ordinal))
    assert pointer["target_count"] == {"N": "3"}
    assert pointer["advanced_curation_count"] == {"N": "1"}
    assert pointer["item_kind"] == {"S": "generation_pointer"}
    for item in seeded.targets:
        assert _projected(seeded.fake, item) == (item.publication_id.value, item.fencing_token)
        assert _staged(seeded.fake, item) == []  # the fold removed every entry
    node = seeded.world.publish_node()
    assert node.state.value == "SUCCEEDED"
    assert json.loads(node.effect_record)["advanced_curation_count"] == 1
    assert any(sk.startswith("ARM#") for _, sk in kinds(seeded.fake, "ARM#"))  # serving
    assert errors == []


# --- AGG3-2 writer half -----------------------------------------------------------------------


def test_agg3_2_a_replayed_flip_is_a_no_op(errors: list[BaseException]) -> None:
    seeded = _seeded(errors)
    store = publications_store(seeded.fake)
    assert store.flip_generation(_flip_request(seeded)) is FlipOutcome.FLIPPED
    committed = len(seeded.fake.transactions)
    assert store.flip_generation(_flip_request(seeded)) is FlipOutcome.ALREADY_FLIPPED
    assert len(seeded.fake.transactions) == committed


def test_final_cross_table_flip_ignores_the_inert_migration_fence(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)

    def set_fence(current: Tables) -> None:
        current.items[("MIGRATION#GRAPH_ROLLING_FRESHNESS", "FENCE")] = {
            "pk": {"S": "MIGRATION#GRAPH_ROLLING_FRESHNESS"},
            "sk": {"S": "FENCE"},
            "state": {"S": "SET"},
        }

    seeded.fake.before_transaction = set_fence
    authority = seeded.world.store.read_serving_publication_authority(seeded.world.run)
    members, _marker_key, _marker = seeded.world.store.build_publication_flip_participant(
        authority,
        root_flow_id="f" * 64,
        target_digest="d" * 64,
        flipped_at=NOW.isoformat(),
    )
    request = replace(_flip_request(seeded), graph_members=members)

    assert publications_store(seeded.fake).flip_generation(request) is FlipOutcome.FLIPPED
    assert seeded.fake.items[("MIGRATION#GRAPH_ROLLING_FRESHNESS", "FENCE")]["state"] == {
        "S": "SET"
    }


def test_sequential_generation_advances_over_an_older_pointer_and_replays_ambiguous_success(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)
    store = publications_store(seeded.fake)
    grant_id, _ = _grant(seeded)
    seeded.fake.items[(f"RUN#{seeded.world.run}", "RUN")]["grant_ordinal"] = {"N": "2"}
    seeded.fake.items[(f"GRANT#{grant_id}", "GRANT")]["active_ordinal"] = {"N": "2"}
    seeded.fake.items[(due_shard(UNIT), f"COMPLETE#{seeded.world.run}")]["grant_ordinal"] = {
        "N": "2"
    }
    current = _flip_request(seeded)
    older = replace(
        current,
        grant_id="older-grant",
        grant_ordinal=1,
        run_id="older-run",
        fences=(),
    )
    assert store.flip_generation(older) is FlipOutcome.FLIPPED

    class _AmbiguousFlip:
        def __getattr__(self, name: str) -> Any:
            return getattr(store, name)

        def flip_generation(self, request: FlipRequest) -> FlipOutcome:
            seeded.fake.ambiguous_next = True
            return store.flip_generation(request)

    seeded.world.publications = _AmbiguousFlip()
    result = seeded.world.publish()

    assert (result.acknowledge, result.outcome) == (True, "graph_publish_succeeded")
    pointer = seeded.fake.pointer()
    assert pointer is not None and pointer["grant_id"] == {"S": current.grant_id}
    assert (
        len(
            [
                key
                for key in seeded.fake.items
                if key[0] == f"SERVING_PUBLICATION#{seeded.world.run}"
            ]
        )
        == 1
    )
    assert errors == []


def test_ambiguous_flip_replay_rejects_a_substituted_pointer_field(
    errors: list[BaseException],
) -> None:
    @guard_reserved_words
    class _MalformedAmbiguousFlip(Tables):
        def transact_write_items(self, **request: Any) -> dict[str, Any]:
            pointer_update = any(
                member.get("Update", {}).get("Key", {}).get(PUBLICATION_RECORD_KEY)
                == {"S": GENERATION_POINTER_RECORD}
                for member in request["TransactItems"]
            )
            if pointer_update:
                self.ambiguous_next = True
            try:
                return super().transact_write_items(**request)
            except ClientError:
                if pointer_update:
                    pointer = self.pointer()
                    assert pointer is not None
                    pointer["root_flow_id"] = {"S": "substituted-root"}
                raise

    seeded = _seeded(errors, fake=_MalformedAmbiguousFlip())
    result = seeded.world.publish()

    assert result.outcome == "graph_publish_invariant"
    assert len(errors) == 1 and isinstance(errors[0], InvariantError)
    pointer = seeded.fake.pointer()
    assert pointer is not None and pointer["root_flow_id"] == {"S": "substituted-root"}


@pytest.mark.parametrize(
    ("fences", "position"),
    [
        pytest.param({"lease_version": 99}, 2, id="stale_lease"),
        pytest.param({"grant_id": "another-grant"}, 1, id="run_without_the_grant"),
        pytest.param({"grant_ordinal": 99}, 1, id="run_at_another_ordinal"),
    ],
)
def test_agg3_2_a_flip_on_a_stale_lease_or_without_the_grant_is_refused_with_zero_effect(
    errors: list[BaseException], fences: dict[str, Any], position: int
) -> None:
    seeded = _seeded(errors)
    with pytest.raises(GenerationFlipLostError) as lost:
        publications_store(seeded.fake).flip_generation(_flip_request(seeded, **fences))
    assert lost.value.position == position
    assert seeded.fake.pointer() is None
    # Positive control: the exact fences flip.
    assert publications_store(seeded.fake).flip_generation(_flip_request(seeded)) is (
        FlipOutcome.FLIPPED
    )


def test_agg3_2_staged_but_unflipped_is_invisible_and_flipped_is_visible(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)
    _stage_all(seeded)
    reader = seeded.world.reader()
    before = reader.current(seeded.a_new.partition)
    assert before is not None and before.publication_id == seeded.a_old.publication_id
    assert reader.current(seeded.b_new.partition) is None  # NONE base, entry uncovered
    publications_store(seeded.fake).flip_generation(_flip_request(seeded))
    after = reader.current(seeded.a_new.partition)
    assert after is not None and after.publication_id == seeded.a_new.publication_id
    created = reader.current(seeded.b_new.partition)
    assert created is not None and created.publication_id == seeded.b_new.publication_id


def test_agg3_2_the_claim_lost_at_the_flip_acknowledges_with_zero_effect(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)
    store = publications_store(seeded.fake)

    class _TakenOver:
        def __getattr__(self, name: str) -> Any:
            return getattr(store, name)

        def flip_generation(self, request: FlipRequest) -> FlipOutcome:
            # A takeover bumps the lease between staging and the flip.
            version = int(seeded.world.claim_item()["lease_version"]["N"])
            seeded.world.claim_item()["lease_version"] = {"N": str(version + 1)}
            return store.flip_generation(request)

    seeded.world.publications = _TakenOver()
    result = seeded.world.publish()
    assert (result.acknowledge, result.outcome) == (True, "graph_claim_lost")
    assert seeded.fake.pointer() is None
    assert seeded.world.publish_node().state.value == "CLAIMED"
    assert errors == []


# --- AGG3-4: counts are classified from stored state and frozen at the flip -------------------


def test_agg3_4_a_replay_after_stage_flip_and_fold_reports_the_same_advanced_count(
    errors: list[BaseException], monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seeded(errors)
    calls = {"settle": 0}
    original = DynamoGraphDispatchStore.settle_publish

    def flaky(self: Any, request: Any) -> Any:
        calls["settle"] += 1
        if calls["settle"] == 1:
            raise DependencyError(ErrorCode("FICTIONAL", 1), "settlement lost")
        return original(self, request)

    monkeypatch.setattr(DynamoGraphDispatchStore, "settle_publish", flaky)
    first = seeded.world.publish(receive_count=1)
    assert (first.acknowledge, first.outcome) == (False, "graph_publish_retryable")
    assert all(_staged(seeded.fake, item) == [] for item in seeded.targets)  # folded already
    seeded.world.reclaim()
    replay = seeded.world.publish(receive_count=2)
    assert replay.outcome == "graph_publish_succeeded"
    assert replay.counts == PublishCounts(3, 1, 0, 0)  # from the pointer, not re-classified
    assert json.loads(seeded.world.publish_node().effect_record)["advanced_count"] == 3


@pytest.mark.usefixtures("errors")
def test_agg3_4_control_a_truly_superseded_target_counts_superseded() -> None:
    fake = Tables()
    target = publication(DAY_A, "a-target", 2)
    newer = publication(DAY_A, "a-newer", 3)
    seed_current(fake, newer, newer, others=(target,))
    world = publish_world(units={"db.table_a": (target,)}, fake=fake)
    result = world.publish()
    assert result.outcome == "graph_publish_succeeded"
    assert result.counts == PublishCounts(0, 0, 1, 0)
    current = fake.current(target.partition.identity)
    assert current["projected_publication_id"] == {"S": newer.publication_id.value}  # never lowered
    assert not [name for name in current if name.startswith("staged#")]


# --- AGG4-4: the fold covers every target carrying this grant's entry -------------------------


def test_agg4_4_staged_on_attempt_1_and_superseded_on_attempt_2_leaves_no_entry(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)
    store = publications_store(seeded.fake)
    flips = {"count": 0}

    class _FlipFailsOnce:
        def __getattr__(self, name: str) -> Any:
            return getattr(store, name)

        def flip_generation(self, request: FlipRequest) -> FlipOutcome:
            flips["count"] += 1
            if flips["count"] == 1:
                raise DependencyError(ErrorCode("FICTIONAL", 2), "flip lost")
            return store.flip_generation(request)

    seeded.world.publications = _FlipFailsOnce()
    other = staged_attribute("another-grant")
    a_current = seeded.fake.current(seeded.a_new.partition.identity)
    a_current[other] = {
        "M": {
            "publication_id": {"S": seeded.a_old.publication_id.value},
            "fencing_token": {"N": "1"},
            "conflict_unit": {"S": "another-window"},
            "grant_ordinal": {"N": "7"},
            "staged_at": {"S": NOW.isoformat()},
        }
    }
    assert seeded.world.publish().outcome == "graph_publish_retryable"
    grant_id, _ = _grant(seeded)
    assert staged_attribute(grant_id) in _staged(seeded.fake, seeded.a_new)
    # Between attempts a newer publication advances the base past this grant's entry.
    newer = publication(DAY_A, "a-newest", 5)
    a_current.update(
        {
            "publication_id": {"S": newer.publication_id.value},
            "fencing_token": {"N": "5"},
            "projected_publication_id": {"S": newer.publication_id.value},
            "projected_fencing_token": {"N": "5"},
        }
    )
    seeded.world.reclaim()
    second = seeded.world.publish(receive_count=2)
    assert second.outcome == "graph_publish_succeeded"
    assert second.counts is not None and second.counts.superseded == 1
    assert staged_attribute(grant_id) not in _staged(seeded.fake, seeded.a_new)
    assert _projected(seeded.fake, seeded.a_new) == (newer.publication_id.value, 5)
    # Control: another grant's entry on the same CURRENT is untouched.
    assert _staged(seeded.fake, seeded.a_new) == [other]


# --- AGG4-3 writer half -----------------------------------------------------------------------


def test_agg4_3_staging_and_the_fold_refuse_a_current_without_projected_fields(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)
    store = publications_store(seeded.fake)
    grant_id, ordinal = _grant(seeded)
    item = seeded.b_new
    target = PublishTarget(item.partition.identity, item.publication_id.value, 1)
    current = seeded.fake.current(item.partition.identity)
    stamped = {name: current.pop(name) for name in list(current) if name.startswith("projected_")}
    with pytest.raises(InvariantError) as staging:
        store.stage_for_grant(GrantStaging(grant_id, ordinal, UNIT, NOW), target, max_redecide=8)
    assert str(staging.value.code) == "ADLY-SEMANTIC_STATE-0004"
    with pytest.raises(InvariantError) as folding:
        store.fold_for_grant(grant_id, item.partition.identity)
    assert str(folding.value.code) == "ADLY-SEMANTIC_STATE-0004"
    # Positive control: the stamped item stages and folds.
    current.update(stamped)
    staged = store.stage_for_grant(
        GrantStaging(grant_id, ordinal, UNIT, NOW), target, max_redecide=8
    )
    assert staged.classification is StageClass.STAGED
    assert store.fold_for_grant(grant_id, item.partition.identity) is FoldOutcome.ADVANCED


def test_agg4_3_publish_dead_letters_an_unstamped_target_as_an_invariant(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)
    current = seeded.fake.current(seeded.zero.partition.identity)
    for name in [name for name in current if name.startswith("projected_")]:
        del current[name]
    result = seeded.world.publish()
    assert (result.acknowledge, result.outcome) == (False, "graph_publish_invariant")
    assert seeded.world.metrics == [PUBLISH_INVARIANT]
    assert seeded.fake.pointer() is None  # nothing visible before the flip
    (error,) = errors
    assert isinstance(error, InvariantError)
    assert str(error.code) == "ADLY-SEMANTIC_STATE-0004"


# --- AGG4-16: the grant ordinal ----------------------------------------------------------------


def test_agg4_16_the_ordinal_is_on_every_staged_entry_and_on_the_pointer(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)
    _, ordinal = _grant(seeded)
    _stage_all(seeded)
    grant_id, _ = _grant(seeded)
    for item in seeded.targets:
        entry = seeded.fake.current(item.partition.identity)[staged_attribute(grant_id)]["M"]
        assert entry["grant_ordinal"] == {"N": str(ordinal)}
        assert entry["conflict_unit"] == {"S": UNIT}
    publications_store(seeded.fake).flip_generation(_flip_request(seeded))
    pointer = seeded.fake.pointer()
    assert pointer is not None and pointer["grant_ordinal"] == {"N": str(ordinal)}


def test_agg4_16_a_granted_run_without_grant_ordinal_is_an_invariant_with_zero_effect(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)
    del seeded.fake.items[(f"RUN#{seeded.world.run}", "RUN")]["grant_ordinal"]
    writes = len(seeded.fake.publication_writes())
    result = seeded.world.publish()
    assert (result.acknowledge, result.outcome) == (False, "graph_publish_invariant")
    (error,) = errors
    assert isinstance(error, PublishInvariantError)
    assert str(error.code) == "ADLY-GRAPH_PUBLISH-0001"
    assert error.reason == "grant_ordinal_missing"
    assert len(seeded.fake.publication_writes()) == writes
    assert all(_staged(seeded.fake, item) == [] for item in seeded.targets)
    assert seeded.fake.pointer() is None
    assert seeded.world.publish_node().state.value == "CLAIMED"
    assert seeded.world.metrics == [PUBLISH_INVARIANT]


# --- The failure table --------------------------------------------------------------------------


def _failing_stage(seeded: _Seeded, error: BaseException) -> None:
    store = publications_store(seeded.fake)
    staged = {"count": 0}

    class _Failing:
        def __getattr__(self, name: str) -> Any:
            return getattr(store, name)

        def stage_for_grant(self, grant: Any, target: Any, *, max_redecide: int) -> Any:
            staged["count"] += 1
            if staged["count"] == 2:
                raise error
            return store.stage_for_grant(grant, target, max_redecide=max_redecide)

    seeded.world.publications = _Failing()


def test_failure_a_dependency_error_with_receives_remaining_releases_the_lease_with_counts(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)
    failure = DependencyError(ErrorCode("FICTIONAL", 3), "injected")
    _failing_stage(seeded, failure)
    result = seeded.world.publish(receive_count=1)
    assert (result.acknowledge, result.outcome) == (False, "graph_publish_retryable")
    claim = seeded.world.claim_item()
    assert claim["lease_expires_at"] == {"S": NOW.isoformat()}
    assert (claim["publish_staged"], claim["publish_total"]) == ({"N": "1"}, {"N": "3"})
    assert seeded.world.publish_node().state.value == "CLAIMED"
    assert errors == [failure]


def test_failure_a_dependency_error_on_the_final_receive_dead_letters_never_failed(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)
    _failing_stage(seeded, DependencyError(ErrorCode("FICTIONAL", 4), "injected"))
    result = seeded.world.publish(receive_count=graph_publish.MAX_RECEIVES)
    assert (result.acknowledge, result.outcome) == (False, "graph_publish_retryable")
    (error,) = errors
    assert isinstance(error, PublishRetryExhaustedError)
    assert str(error.code) == "ADLY-GRAPH_PUBLISH-0002"
    assert error.context["signal"] == GRAPH_POST_GRANT_RETRY_EXHAUSTED
    assert seeded.world.publish_node().state.value == "CLAIMED"  # publish never settles FAILED


def test_failure_a_validation_mismatch_is_an_invariant_with_zero_effect(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)
    node = seeded.fake.items[(f"RUN#{seeded.world.run}", "NODE#write_unit:db.table_a")]
    node["effect_digest"] = {"S": "0" * 64}
    result = seeded.world.publish()
    assert result.outcome == "graph_publish_invariant"
    (error,) = errors
    assert isinstance(error, PublishInvariantError)
    assert (str(error.code), error.reason) == ("ADLY-GRAPH_PUBLISH-0001", "effect_digest_mismatch")
    assert seeded.fake.pointer() is None
    assert seeded.world.metrics == [PUBLISH_INVARIANT]


def test_failure_one_failed_pre_grant_unit_beside_succeeded_ones_is_an_invariant_with_zero_effect(
    errors: list[BaseException],
) -> None:
    """Publish needs every pre-grant node succeeded; one success must not publish a partial run."""
    seeded = _seeded(errors)
    node = seeded.fake.items[(f"RUN#{seeded.world.run}", "NODE#write_unit:db.table_b")]
    node["state"] = {"S": "FAILED"}  # materialize_raw and write_unit:db.table_a stay SUCCEEDED
    writes = len(seeded.fake.publication_writes())
    result = seeded.world.publish()
    assert (result.acknowledge, result.outcome) == (False, "graph_publish_invariant")
    (error,) = errors
    assert isinstance(error, PublishInvariantError)
    assert (str(error.code), error.reason) == ("ADLY-GRAPH_PUBLISH-0001", "pre_grant_not_succeeded")
    assert len(seeded.fake.publication_writes()) == writes
    assert all(_staged(seeded.fake, item) == [] for item in seeded.targets)
    assert seeded.fake.pointer() is None
    assert seeded.world.metrics == [PUBLISH_INVARIANT]


def test_failure_bounded_frontier_conflicts_retry_without_parking(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)
    store = publications_store(seeded.fake)

    class _Contended:
        def __getattr__(self, name: str) -> Any:
            return getattr(store, name)

        def flip_generation(self, request: FlipRequest) -> FlipOutcome:
            del request
            raise GenerationFlipBarrierError

    seeded.world.publications = _Contended()
    result = seeded.world.publish(receive_count=1)

    assert (result.acknowledge, result.outcome) == (False, "frontier_conflict")
    assert seeded.world.publish_node().state.value == "CLAIMED"
    assert seeded.world.store.read_due(due_shard(UNIT), "PUBLISH_WAIT#") == ()
    assert seeded.fake.pointer() is None
    assert errors == []


def test_failure_bounded_settle_frontier_races_release_and_return_frontier_conflict(
    errors: list[BaseException], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan item 4: settle races, like flip races, end as a recoverable `frontier_conflict`."""
    seeded = _seeded(errors)
    original = DynamoGraphDispatchStore.settle_publish
    attempts: list[int] = []

    def racing(self: DynamoGraphDispatchStore, request: Any) -> Any:
        # A concurrent publication on the same live shard moves it after this settle's read.
        authority = request.authority
        counter = seeded.fake.items[
            (f"SERVING_FRONTIER#{authority.domain}#{authority.shard:02d}", "COUNTER")
        ]
        counter["revision"] = {"N": str(int(counter["revision"]["N"]) + 1)}
        attempts.append(authority.shard_revision)
        return original(self, request)

    monkeypatch.setattr(DynamoGraphDispatchStore, "settle_publish", racing)
    result = seeded.world.publish(receive_count=1)

    assert (result.acknowledge, result.outcome) == (False, "frontier_conflict")
    assert len(attempts) == graph_publish._MAX_REDECIDE  # each retry read a fresh frontier
    assert len(set(attempts)) == len(attempts)
    assert seeded.world.publish_node().state.value == "CLAIMED"
    assert seeded.world.claim_item()["lease_expires_at"] == {"S": NOW.isoformat()}
    marker = seeded.fake.items[(f"SERVING_PUBLICATION#{seeded.world.run}", "MARKER")]
    assert marker["state"] == {"S": "VISIBLE_UNSETTLED"}  # the flip stands; settle retries
    assert seeded.fake.pointer() is not None
    assert seeded.world.store.read_due(due_shard(UNIT), "PUBLISH_WAIT#") == ()
    assert errors == []


def _domain(seeded: _Seeded) -> str:
    complete = seeded.fake.items[(due_shard(UNIT), f"COMPLETE#{seeded.world.run}")]
    return str(complete["domain"]["S"])


def test_wave_b_flips_and_settles_on_the_live_frontier_while_wave_a_is_active(
    errors: list[BaseException], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan item 4: publication never reads or waits on the head; A's snapshot is untouched."""
    seeded = _seeded(errors)
    domain, run = _domain(seeded), seeded.world.run
    head_key = (f"SERVING_DOMAIN#{domain}", "HEAD")
    head = seeded.fake.items[head_key]
    head.update(
        {
            "state": {"S": "ACTIVE"},
            "version": {"N": "3"},
            "cohort_revision": {"S": "cohort-wave-a"},
            "winner_run_id": {"S": "run-wave-a"},
            "elected_frontier_digest": {"S": "a" * 64},
            "elected_candidate_digest": {"S": "c" * 64},
            "elected_run_ids": {"L": [{"S": "run-wave-a"}]},
        }
    )
    frozen = copy.deepcopy(head)
    frontier_key = (f"SERVING_FRONTIER#{domain}#{serving_frontier_shard(run):02d}", "COUNTER")
    before = {
        name: int(seeded.fake.items[frontier_key][name]["N"])
        for name in ("revision", "visible_count", "eligible_count")
    }
    reads: list[str] = []
    real_get = seeded.fake.get_item

    def recording_get(**kwargs: Any) -> dict[str, Any]:
        reads.append(str(kwargs["Key"].get("pk", {}).get("S", "")))
        return real_get(**kwargs)

    real_transact_get = seeded.fake.transact_get_items

    def recording_transact_get(**kwargs: Any) -> dict[str, Any]:
        reads.extend(
            str(get["Get"]["Key"].get("pk", {}).get("S", "")) for get in kwargs["TransactItems"]
        )
        return real_transact_get(**kwargs)

    monkeypatch.setattr(seeded.fake, "get_item", recording_get)
    monkeypatch.setattr(seeded.fake, "transact_get_items", recording_transact_get)

    result = seeded.world.publish()

    assert (result.acknowledge, result.outcome) == (True, "graph_publish_succeeded")
    assert seeded.world.publish_node().state.value == "SUCCEEDED"
    frontier = seeded.fake.items[frontier_key]
    assert {name: int(frontier[name]["N"]) for name in before} == {
        "revision": before["revision"] + 2,  # one flip, one settlement
        "visible_count": before["visible_count"] + 1,
        "eligible_count": before["eligible_count"] + 1,
    }
    marker = seeded.fake.items[(f"SERVING_PUBLICATION#{run}", "MARKER")]
    assert marker["state"] == {"S": "SETTLED_ELIGIBLE"}
    assert marker["eligible_coordinate"] == {"N": str(before["eligible_count"] + 1)}
    assert marker["eligible_revision"] == {"N": str(before["revision"] + 2)}
    assert seeded.fake.items[head_key] == frozen  # never joins or mutates wave A
    assert not any(pk.startswith("SERVING_DOMAIN#") for pk in reads)
    assert kinds(seeded.fake, "ARM#")
    retired = ("PUBLISH_WAIT#", "CAPACITY_WAIT#", "SERVING_DAY#")
    assert not any(pk.startswith(retired) or sk.startswith(retired) for pk, sk in seeded.fake.items)
    assert errors == []


def test_failure_a_pointer_already_at_a_later_grant_is_an_invariant(
    errors: list[BaseException],
) -> None:
    seeded = _seeded(errors)
    later = replace(_flip_request(seeded), grant_id="later-grant", grant_ordinal=99, fences=())
    publications_store(seeded.fake).flip_generation(later)
    result = seeded.world.publish()
    assert result.outcome == "graph_publish_invariant"
    (error,) = errors
    assert isinstance(error, InvariantError)
    assert str(error.code) == "ADLY-SEMANTIC_STATE-0006"
    assert seeded.fake.pointer() is not None
    assert seeded.fake.pointer()["grant_ordinal"] == {"N": "99"}  # type: ignore[index]
