# SPDX-License-Identifier: MIT-0
"""ADR-162 §10: no reader of an elected cohort reads its members, at any roster size.

Before this, the Release's unarmed settlement and the precompute consumer bound the cohort by
reading every member's run (about 10 ms each on dev), and both consumers' eligibility read every
member's publish node until one was eligible: about 35 s at dev's 3,502 members, past the
precompute's 30 s timeout. Now the election records the cohort's contributor roots in the
winner's `SERVING_ROSTER` item and its summed publish effect on the bake node. Each test pads an
elected snapshot to near the derived roster bound with members that do not exist, so a member
read would fail, and drives one real caller under a metered clock at dev's per-call rates and
that caller's own budget. A paired small cohort runs the same path: the store calls are equal.
A winner elected before §10 (no roster) is still bound, by reading its members as that code
did, up to the 512 cap that code enforced. The direct binding, the item that limits the roster
bound, is measured at the bound with every contract roster row unavailable. Fictional data only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from execution_graph_dispatch_support import NOW
from execution_graph_release_support import FakeLegacy, release_store
from graph_bake_v3_support import Bake, roster
from graph_consumer_support import arm_and_dispatch, publish_settled
from test_execution_graph_pass_budget import Metered
from test_graph_precompute import (  # pyright: ignore[reportPrivateUsage]
    World,
    _inputs,  # pyright: ignore[reportPrivateUsage]
    _roster,  # pyright: ignore[reportPrivateUsage]
)

from adapters.jobs.execution_graph.release import ReleaseContext, settle_unarmed_cohort
from domain.operations.execution_graph_dispatch import NodeState, ServingAuthorityIdentity
from domain.operations.execution_graph_release import (
    BindingKind,
    CohortContribution,
    CohortMember,
    CohortOutcome,
    ServingBinding,
)
from domain.operations.generated_execution_graphs import (
    BAKE_TASKS,
    BAKE_UNAVAILABLE_REASONS,
    BAKE_V3,
    SERVING_COALESCING,
)
from domain.operations.status import canonical_authority_digest
from domain.shared_kernel.errors import ErrorCode, InvariantError
from ports.outbound.execution_graph_release import ServingMutationResult

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-162", "ADR-140")]

NEAR_BOUND = 5_417
"""At `serving_candidate_roster_max`, the derived item limit (contract comment)."""
RELEASE_PASS_MS = 60_000
PRECOMPUTE_TIMEOUT_MS = 30_000
BAKER_TIMEOUT_MS = 900_000


def _fictional_root(index: int) -> str:
    """Return a 36-character root flow id, the production length."""
    return f"{index:08x}-0000-4000-8000-{index:012x}"


def _pad(fake: Any, winner: str, size: int) -> tuple[str, ...]:
    """Grow the winner's elected snapshot to `size` members that have no items at all."""
    bake = fake.items[(f"RUN#{winner}", "NODE#bake")]
    roster_item = fake.items[(f"RUN#{winner}", "SERVING_ROSTER")]
    real_runs = [value["S"] for value in bake["serving_run_ids"]["L"]]
    real_roots = [value["S"] for value in roster_item["contributor_roots"]["L"]]
    extra = size - len(real_runs)
    runs = [
        *(canonical_authority_digest(["fictional-member", index]) for index in range(extra)),
        *real_runs,
    ]
    roots = tuple(sorted({*real_roots, *(_fictional_root(index) for index in range(extra))}))
    listed = {"L": [{"S": run} for run in runs]}
    bake["serving_run_ids"] = listed
    roster_item["contributor_roots"] = {"L": [{"S": root} for root in roots]}
    cohort = bake["cohort_revision"]["S"]
    for key, item in fake.items.items():
        if key[1] == f"COALESCE#{cohort}":
            item["run_ids"] = listed
        if key[1] == "HEAD" and item.get("cohort_revision") == {"S": cohort}:
            item["elected_run_ids"] = listed
    return roots


def _binding_roots(fake: Any, winner: str) -> tuple[str, ...]:
    grant = fake.items[(f"RUN#{winner}", "RUN")]["grant_id"]["S"]
    binding = fake.items[(f"SERVING#{grant}", "BINDING")]
    return tuple(value["S"] for value in binding["contributor_roots"]["L"])


def _member_reads(metered: Metered, winner: str) -> list[str]:
    return [pk for pk in metered.meter.runs_read if pk.startswith("RUN#") and pk != f"RUN#{winner}"]


def _metered_call(fake: Any, budget_ms: int, action: Callable[[], Any]) -> tuple[Any, Metered]:
    metered = Metered(fake, due_query_ms=15, call_ms=10)
    metered.invocation(remaining=budget_ms)
    result = action()
    assert metered.meter.remaining > 0  # inside the caller's own budget
    return result, metered


# --- the Release's unarmed settlement (60 s pass) --------------------------------------------


def _unarmed_cohort(size: int | None) -> tuple[Bake, str, tuple[str, ...] | None]:
    """Elect two uncurated members; the bake settles U1 and leaves precompute unarmed."""
    bake = Bake(roster=roster(2))
    loser, winner = (
        publish_settled(
            bake.tables,
            advanced_curation=0,
            unit=canonical_authority_digest(["fictional-cohort", name]),
            fingerprint=name * 64,
            request_id=f"request-{name}",
        )
        for name in ("a", "b")
    )
    del loser
    arm_and_dispatch(bake.tables, winner, "bake")
    roots = None if size is None else _pad(bake.tables, winner, size)
    assert bake.fan_out(winner).node_state is NodeState.SUCCEEDED
    assert bake.node(winner, "precompute").state is NodeState.WAITING_INPUT
    return bake, winner, roots


@pytest.mark.parametrize("size", [None, NEAR_BOUND], ids=["two-members", "near-bound"])
def test_the_release_settles_and_binds_an_unarmed_cohort_reading_no_member(
    size: int | None,
) -> None:
    bake, winner, padded = _unarmed_cohort(size)
    context = ReleaseContext(release_store(bake.tables), FakeLegacy(), lambda: NOW)

    result, metered = _metered_call(
        bake.tables, RELEASE_PASS_MS, lambda: settle_unarmed_cohort(context, winner)
    )

    assert result is ServingMutationResult.COMMITTED
    assert _member_reads(metered, winner) == []
    assert metered.meter.calls == 46  # the same for two members and for 5,417
    roots = _binding_roots(bake.tables, winner)
    assert len(roots) == (2 if padded is None else NEAR_BOUND)
    if padded is not None:
        assert roots == padded


# --- the precompute consumer (30 s timeout): eligibility, then the binding -------------------


def _precompute_world(size: int | None) -> tuple[World, tuple[str, ...] | None]:
    """Elect a curated loser and a zero-curation winner; the bake member is contributed."""
    plan = _roster(3)
    world = World(plan, _inputs(plan), cohort=(1, 0))
    store = release_store(world.fake)
    run = store.read_run(world.run)
    assert run is not None and run.grant_id is not None
    store.record_contribution(
        CohortContribution(
            run.grant_id, CohortMember.BAKE, CohortOutcome.NO_ELIGIBLE, None, NOW, run.root_flow_id
        )
    )
    roots = None if size is None else _pad(world.fake, world.run, size)
    return world, roots


@pytest.mark.parametrize("size", [None, NEAR_BOUND], ids=["two-members", "near-bound"])
def test_the_precompute_consumer_decides_eligibility_and_binds_reading_no_member(
    size: int | None,
) -> None:
    world, padded = _precompute_world(size)

    result, metered = _metered_call(world.fake, PRECOMPUTE_TIMEOUT_MS, world.consume)

    assert result.node_state is NodeState.SUCCEEDED
    assert len(world.planner.calls) == 1  # the loser's curation made the cohort eligible
    assert _member_reads(metered, world.run) == []
    assert metered.meter.calls == 28  # the same for two members and for 5,417
    roots = _binding_roots(world.fake, world.run)
    assert len(roots) == (2 if padded is None else NEAR_BOUND)
    if padded is not None:
        assert roots == padded


# --- the bake finalizer (900 s) ----------------------------------------------------------------


def _finalizer_world(size: int | None) -> tuple[Bake, str, tuple[str, ...] | None]:
    bake = Bake(roster=roster(3))
    bake.seed_predecessors()
    run = bake.granted()
    roots = None if size is None else _pad(bake.tables, run, size)
    bake.fan_out(run)
    assert all(result.node_state is NodeState.SUCCEEDED for result in bake.all_tasks(run))
    return bake, run, roots


@pytest.mark.parametrize("size", [None, NEAR_BOUND], ids=["one-member", "near-bound"])
def test_the_bake_finalizer_binds_reading_no_member(size: int | None) -> None:
    bake, run, padded = _finalizer_world(size)

    result, metered = _metered_call(bake.tables, BAKER_TIMEOUT_MS, lambda: bake.finalize(run))

    assert result.node_state is NodeState.SUCCEEDED
    assert _member_reads(metered, run) == []
    assert metered.meter.calls == 266  # the same for one member and for 5,417
    roots = _binding_roots(bake.tables, run)
    assert len(roots) == (1 if padded is None else NEAR_BOUND)
    if padded is not None:
        assert roots == padded


# --- a legacy winner (elected before §10): its readers read members, bounded -----------------


def _as_legacy(fake: Any, winner: str) -> None:
    """Rewrite an elected winner into the pre-§10 item shape: no roster, no cohort effect."""
    del fake.items[(f"RUN#{winner}", "SERVING_ROSTER")]
    node = fake.items[(f"RUN#{winner}", "NODE#bake")]
    for name in (
        "serving_cohort_advanced_count",
        "serving_cohort_advanced_curation_count",
        "serving_cohort_unverified_count",
        "serving_cohort_unverified_run_id",
    ):
        node.pop(name, None)


def _legacy_pad(node: dict[str, Any], real: list[str], size: int) -> None:
    """Set a legacy bake node's snapshot to `real` plus members with no items, `size` in all."""
    extra = [canonical_authority_digest(["fictional-member", n]) for n in range(size - len(real))]
    node["serving_run_ids"] = {"L": [{"S": run} for run in [*extra, *real]]}


def test_the_release_binds_a_legacy_cohort_from_its_members_runs() -> None:
    bake, winner, _padded = _unarmed_cohort(None)
    loser = next(
        value["S"]
        for value in bake.tables.items[(f"RUN#{winner}", "NODE#bake")]["serving_run_ids"]["L"]
        if value["S"] != winner
    )
    roots = {
        bake.tables.items[(f"RUN#{run}", "RUN")]["root_flow_id"]["S"] for run in (loser, winner)
    }
    _as_legacy(bake.tables, winner)
    context = ReleaseContext(release_store(bake.tables), FakeLegacy(), lambda: NOW)

    result, metered = _metered_call(
        bake.tables, RELEASE_PASS_MS, lambda: settle_unarmed_cohort(context, winner)
    )

    assert result is ServingMutationResult.COMMITTED
    assert set(_member_reads(metered, winner)) == {f"RUN#{loser}"}
    assert set(_binding_roots(bake.tables, winner)) == roots


def test_a_legacy_cohort_above_the_pre_section_10_cap_is_refused_before_any_member_read() -> None:
    bake, winner, _padded = _unarmed_cohort(None)
    _as_legacy(bake.tables, winner)
    node = bake.tables.items[(f"RUN#{winner}", "NODE#bake")]
    real = [value["S"] for value in node["serving_run_ids"]["L"]]
    _legacy_pad(node, real, 513)  # LEGACY_SERVING_ROSTER_MAX (512) + 1
    context = ReleaseContext(release_store(bake.tables), FakeLegacy(), lambda: NOW)
    grant = bake.tables.items[(f"RUN#{winner}", "RUN")]["grant_id"]["S"]

    with pytest.raises(InvariantError) as refused:
        settle_unarmed_cohort(context, winner)

    assert refused.value.code == ErrorCode("EXEC_GRAPH", 45)
    assert refused.value.context == {"reason": "legacy_roster", "roster": 513, "bound": 512}
    assert (f"SERVING#{grant}", "BINDING") not in bake.tables.items
    # Control: at the cap the same legacy path reads the members, and the first padded member,
    # which has no run, fails as the pre-§10 code did, not with the cap's refusal.
    _legacy_pad(node, real, 512)
    with pytest.raises(ValueError, match="names a missing candidate run"):
        settle_unarmed_cohort(context, winner)


def test_the_precompute_consumer_decides_a_legacy_cohort_from_its_members_effects() -> None:
    world, _padded = _precompute_world(None)
    _as_legacy(world.fake, world.run)

    result = world.consume()

    assert result.node_state is NodeState.SUCCEEDED
    assert len(world.planner.calls) == 1  # the loser's curation, read from its publish node
    assert len(_binding_roots(world.fake, world.run)) == 2


def test_a_legacy_winner_is_armed_reported_and_finalized_with_its_own_root() -> None:
    """The mixed-version window: an old promoter elects, the new workers carry the cohort on.

    Before the legacy path the promoter saw no serving context, never armed the finalizer and
    the domain's head stayed ACTIVE for good, with nothing reported.
    """
    from adapters.jobs.execution_graph.fence_settler import due_shards  # noqa: PLC0415
    from adapters.jobs.execution_graph.instantiate import ingestion_node_plan  # noqa: PLC0415
    from adapters.jobs.execution_graph.promoter import PromoterContext, arm_pass  # noqa: PLC0415

    bake = Bake(roster=roster(3))
    bake.seed_predecessors()
    run = bake.granted()
    _as_legacy(bake.tables, run)
    bake.fan_out(run)
    assert all(result.node_state is NodeState.SUCCEEDED for result in bake.all_tasks(run))

    counts = arm_pass(
        PromoterContext(release_store(bake.tables), ingestion_node_plan, lambda: NOW),
        due_shards(),
        lambda: 60_000,
        "test-request",
    )

    assert (counts.serving_legacy_winners, counts.armed) == (1, 1)  # the finalizer, reported
    assert bake.finalize(run).node_state is NodeState.SUCCEEDED
    root = bake.run_item(run)["root_flow_id"]["S"]
    assert _binding_roots(bake.tables, run) == (root,)


# --- the binding at the roster bound fits one item (ADR-162 §9, AGG-R2-4) ---------------------

ROSTER_BOUND = 5_417
"""`serving_candidate_roster_max`, limited by this item (contract comment)."""
_WIDE = "x" * 64
"""Wider than every production identifier and digest (36 and 64 characters)."""
_WIDE_TIME = datetime(2026, 11, 13, 23, 59, 59, 999_999, tzinfo=timezone(timedelta(hours=-11)))
"""32 characters in ISO form, microseconds and an offset."""


def _widest_direct_binding_bytes(rows: list[str]) -> int:
    """Measure the widest direct binding a cohort at the bound can write, terminal included."""
    from adapters.jobs.execution_graph.post_grant_consumer import (  # noqa: PLC0415
        _FAILING_ORDER,  # pyright: ignore[reportPrivateUsage]
    )
    from adapters.outbound.dynamodb.execution_graph_release import (  # noqa: PLC0415
        DynamoGraphReleaseStore,
    )
    from adapters.outbound.dynamodb.graph_transaction_bytes import item_bytes  # noqa: PLC0415

    reasons = [*map(str, BAKE_UNAVAILABLE_REASONS.values()), *map(str, _FAILING_ORDER)]
    longest = max(reasons, key=len)
    binding = ServingBinding(
        binding_id=_WIDE,
        grant_id=_WIDE,
        conflict_unit=_WIDE,
        served_revision=_WIDE_TIME,
        artifact_revision=_WIDE,
        contributor_roots=tuple(f"{index:064x}" for index in range(ROSTER_BOUND)),
        bound_at=_WIDE_TIME,
        unavailable=tuple((row, longest) for row in rows),
        root_flow_id=_WIDE,
        authority_epoch=10**12,
        domain=_WIDE,
        cohort_revision=_WIDE,
        serving_input_revision=_WIDE,
        plan_digest=_WIDE,
        binding_kind=BindingKind.DIRECT,
        winner_run_id=_WIDE,
        execution_attempts=10**6,
    )
    member = DynamoGraphReleaseStore._binding_member(  # pyright: ignore[reportPrivateUsage]
        binding,
        None,
        "bake_finalize",
        _WIDE,
        ServingAuthorityIdentity(_WIDE, _WIDE, 10**12, _WIDE),
    )
    return item_bytes(member.item["Put"]["Item"])  # type: ignore[typeddict-item]


def _roster_rows(extra: int = 0) -> list[str]:
    """Every contract bake roster row, padded to the identifier limit, plus `extra` more."""
    width = int(BAKE_V3["identifier_max_length"])
    names = [f"{row['page']}#{row['variant']}" for row in BAKE_TASKS["roster"]]
    names.extend(f"fictional-page-{index}#variant" for index in range(extra))
    return [name.ljust(width, "x") for name in names]


def test_the_direct_binding_at_the_roster_bound_fits_one_item_with_every_row_unavailable() -> None:
    """A roster row added to the bake contract lowers the binding's limit below the bound.

    Without this, a cohort between the new limit and the bound fails its binding write at
    DynamoDB instead of being refused at the election with `EXEC_GRAPH-45`.
    """
    assert int(SERVING_COALESCING["serving_candidate_roster_max"]) == ROSTER_BOUND
    assert len(BAKE_TASKS["roster"]) == 20  # the rows the bound was derived over

    assert _widest_direct_binding_bytes(_roster_rows()) <= 400_000  # the oracle's item cap
    # Control: one more roster row, every row unavailable, no longer fits.
    assert _widest_direct_binding_bytes(_roster_rows(extra=1)) > 400_000
