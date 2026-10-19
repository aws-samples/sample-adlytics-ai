# SPDX-License-Identifier: MIT-0
"""Fixtures for the S4 release, lineage, legacy-head and cohort tests.

Every test drives the real DynamoDB adapters (`DynamoGraphReleaseStore`, and the S1 admission
store and S3 command path where a race needs them) over `FakeDynamo`, which evaluates the
adapters' own condition and update expressions transactionally. Every identity is fictional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from execution_graph_dispatch_support import (
    NOW,
    ROOT_CLOCK,
    TABLE,
    UNIT,
    SeededRun,
    finish,
    now_text,
    seed_run,
    set_mode,
    unit_body,
    write_int,
    write_text,
)
from execution_graph_support import FakeDynamo

from adapters.jobs.execution_graph.release import ReleaseContext, ReleaseOutcome, release_run
from adapters.outbound.dynamodb.execution_graph import due_shard
from adapters.outbound.dynamodb.execution_graph_release import DynamoGraphReleaseStore
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_dispatch import (
    ArmScope,
    NodePhase,
    NodeState,
    ServingCandidate,
    serving_frontier_shard,
)
from domain.operations.execution_graph_release import (
    POST_GRANT_INVARIANT,
    SERVING_PLAN_DIGEST,
    CohortContribution,
    CohortMember,
    CohortOutcome,
    serving_generation,
)
from ports.outbound.execution_graph_dispatch import NodeSpec, PutNodesRequest, SuccessorRequest
from ports.outbound.execution_graph_release import ActivateServingWinnerRequest

WINDOW = (
    "amc/dsp_impressions/query_pack=amc-dsp-impressions/source_scope=fictional/"
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    "marketplace=ATVPDKIKX0DER/window=2026-10-04_2026-10-06/options=fictional/"
)


def release_store(fake: FakeDynamo) -> DynamoGraphReleaseStore:
    """Return the real release adapter over the fake table."""
    return DynamoGraphReleaseStore(TABLE, fake)  # type: ignore[arg-type]


@dataclass
class FakeLegacy:
    """The legacy-head reader: a fixed newest COMPLETED retrieval per window."""

    heads: dict[str, datetime] = field(default_factory=dict[str, datetime])
    completed: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])
    calls: list[tuple[str, datetime]] = field(default_factory=list[tuple[str, datetime]])

    def head_after(self, window: str, r: datetime) -> datetime | None:
        """Return the window's legacy head when it is newer than `r`."""
        self.calls.append((window, r))
        head = self.heads.get(window)
        return head if head is not None and head > r else None

    def completed_plan_key(self, key: str, version_id: str) -> str | None:
        """Return the exact version's completed legacy plan key."""
        return self.completed.get((key, version_id))


def context(fake: FakeDynamo, legacy: FakeLegacy | None = None) -> ReleaseContext:
    """Return a release context over the fake table at the fixed clock."""
    return ReleaseContext(release_store(fake), legacy or FakeLegacy(), lambda: NOW)


def granting_specs(run: str, *tables: str) -> tuple[NodeSpec, ...]:
    """One write node per physical table, then `grant_apply` after all of them."""
    writes = tuple(
        NodeSpec(
            name=f"write_{table}",
            after=(),
            phase=NodePhase.PRE_GRANT,
            lock_key=f"fictional-db.{table}",
            group_id=f"fictional-db.{table}",
            body=unit_body(run, f"write_{table}", f"fictional-db.{table}"),
        )
        for table in tables
    )
    grant = NodeSpec(
        name="grant_apply",
        after=tuple(spec.name for spec in writes),
        phase=NodePhase.PRE_GRANT,
        lock_key=f"grant_apply#{run}",
        group_id=f"grant_apply#{run}",
        body=unit_body(run, "grant_apply", f"grant_apply#{run}"),
    )
    return (*writes, grant)


def seed_granting_run(  # noqa: PLR0913 -- one fixture builder with explicit knobs
    fake: FakeDynamo,
    *,
    tables: tuple[str, ...] = ("orders",),
    retrieved: datetime = NOW - timedelta(hours=1),
    fingerprint: str = "f" * 64,
    unit: str = UNIT,
    request_id: str = "request-0001",
    window: str = WINDOW,
    settled: bool = True,
    compute: bool = True,
) -> SeededRun:
    """Seed an OPEN epoch-1 run whose write nodes succeeded and whose `grant_apply` is pending."""
    if ("MODE#ingestion", "MODE") not in fake.items:
        set_mode(fake, AuthorityMode.GRAPH)
    seeded = seed_run(
        fake,
        fingerprint=fingerprint,
        retrieved=retrieved,
        unit=unit,
        request_id=request_id,
        compute=compute,
        instantiate=False,
    )
    name = seeded.record.run_id
    fake.items[(f"RUN#{name}", "RUN")]["window"] = write_text(window)
    store = release_store(fake)
    run = store.read_run(name)
    assert run is not None
    store.put_nodes(PutNodesRequest(run, granting_specs(name, *tables), now_text()))
    if settled:
        for table in tables:
            fake.items[(f"RUN#{name}", f"NODE#write_{table}")]["state"] = write_text(
                NodeState.SUCCEEDED.value
            )
    fake.items[(due_shard(unit), f"RELEASE#{name}")] = {
        "pk": write_text(due_shard(unit)),
        "sk": write_text(f"RELEASE#{name}"),
        "item_kind": write_text("release"),
        "run_id": write_text(name),
        "due_at": write_text(now_text()),
    }
    record = store.read_run(name)
    assert record is not None
    return SeededRun(record, request_id)


def granted_run(  # noqa: PLR0913 -- one fixture builder with explicit knobs
    fake: FakeDynamo, *, unit: str = UNIT, fingerprint: str = "f" * 64, request_id: str = "r-1"
) -> str:
    """S6: a run granted and applied: `publish` READY, `precompute` and `bake` PENDING."""
    seeded = seed_granting_run(fake, unit=unit, fingerprint=fingerprint, request_id=request_id)
    name = seeded.record.run_id
    fake.items[(f"RUN#{name}", "RUN")]["root_clock_started_at"] = write_text(ROOT_CLOCK)
    assert release_run(context(fake), name) is ReleaseOutcome.APPLIED
    return name


def settle_publish(fake: FakeDynamo, run: str) -> None:
    """Settle publish and project its eligible marker/frontier authority."""
    finish(fake, run, "publish")
    record = release_store(fake).read_run(run)
    assert record is not None and record.grant_id is not None and record.grant_ordinal is not None
    release_store(fake).put_successors(
        SuccessorRequest(record, (), ArmScope.SERVING, False, now_text())
    )
    fake.items[(f"RUN#{run}", "NODE#publish")].setdefault(
        "effect_digest", write_text(f"effect-{run}")
    )
    complete = fake.items[(due_shard(record.conflict_unit), f"COMPLETE#{run}")]
    identity = {name: complete[name] for name in ("run_id", "grant_id", "grant_ordinal", "domain")}
    fake.items[(due_shard(record.conflict_unit), f"ARM#{run}")].update(identity)
    domain = str(complete["domain"]["S"])
    shard = serving_frontier_shard(run)
    frontier = fake.items[(f"SERVING_FRONTIER#{domain}#{shard:02d}", "COUNTER")]
    frontier["revision"] = write_int(int(frontier["revision"]["N"]) + 1)
    frontier["visible_count"] = write_int(int(frontier["visible_count"]["N"]) + 1)
    frontier["eligible_count"] = write_int(int(frontier["eligible_count"]["N"]) + 1)
    fake.items[(f"SERVING_PUBLICATION#{run}", "MARKER")] = {
        "pk": write_text(f"SERVING_PUBLICATION#{run}"),
        "sk": write_text("MARKER"),
        "state": write_text("SETTLED_ELIGIBLE"),
        "target_digest": write_text(f"target-{run}"),
        "frontier_shard": write_int(shard),
        "eligible_coordinate": write_int(int(frontier["eligible_count"]["N"])),
        "eligible_revision": write_int(int(frontier["revision"]["N"])),
        **identity,
    }


def reserve_winner(  # noqa: PLR0913 -- one fixture builder with explicit knobs
    fake: FakeDynamo,
    run: str,
    *,
    cohort_revision: str = "cohort-1",
    serving_input_revision: str = "input-1",
) -> ServingCandidate:
    """Reserve `run` as its domain's sole winner through the store's real election CAS.

    The run is granted and applied (the bootstrap wrote its day, head and frontier) and its
    `publish` is settled (`settle_publish`). The reservation writes `COALESCE#<cohort>` and
    stamps the winner/cohort/day authority on `precompute`, which `contribute` and the
    completion's direct drain then read; nothing here writes that authority by hand.
    """
    store = release_store(fake)
    record = store.read_run(run)
    assert record is not None and record.grant_id is not None and record.grant_ordinal is not None
    complete = fake.items[(due_shard(record.conflict_unit), f"COMPLETE#{run}")]
    domain = str(complete["domain"]["S"])
    authority = store.read_serving_domain(domain)
    assert authority.head is not None and authority.frontier is not None
    publish = store.read_node(run, "publish")
    assert publish is not None and publish.effect_digest is not None
    winner = ServingCandidate(
        run_id=run,
        grant_id=record.grant_id,
        root_flow_id=record.root_flow_id,
        domain=domain,
        grant_ordinal=record.grant_ordinal,
        publication_effect_digest=publish.effect_digest,
    )
    result = store.activate_serving_winner(
        ActivateServingWinnerRequest(
            authority.head,
            authority.frontier,
            (winner,),
            winner,
            cohort_revision,
            serving_input_revision,
            now_text(),
            SERVING_PLAN_DIGEST,
        )
    )
    assert result.mutation.value == "committed"
    return winner


# ADR-140: a winner whose bake settled without its finalizer.

U2_KEYS = ("campaigns#7d", "campaigns#30d")


def bake_member(grant: str, root: str, shape: str) -> CohortContribution:
    """Return the bake member a fan-out that skipped the finalizer stores: U1, U2, or a defect."""
    if shape == "u1":
        return CohortContribution(
            grant, CohortMember.BAKE, CohortOutcome.NO_ELIGIBLE, None, NOW, root
        )
    if shape == "u2":
        unavailable = tuple((key, POST_GRANT_INVARIANT) for key in U2_KEYS)
        return CohortContribution(
            grant, CohortMember.BAKE, CohortOutcome.UNAVAILABLE, None, NOW, root, unavailable
        )
    if shape == "revision":
        return CohortContribution(
            grant, CohortMember.BAKE, CohortOutcome.SUCCEEDED, serving_generation(grant), NOW, root
        )
    reasons = {"designed": ("campaigns#7d", "campaigns_materialization_unavailable")}
    mixed = (("campaigns#7d", POST_GRANT_INVARIANT), ("campaigns#30d", "bake_budget_exhausted"))
    unavailable = mixed if shape == "mixed" else (reasons["designed"],)
    return CohortContribution(
        grant, CohortMember.BAKE, CohortOutcome.UNAVAILABLE, None, NOW, root, unavailable
    )


def unarmed_winner(fake: FakeDynamo, shape: str = "u1", bake_state: str | None = None) -> str:
    """Return the live wedge's shape: an elected winner whose bake settled with no finalizer.

    The winner is granted, applied, published and reserved ACTIVE alone; its bake member is
    stored and its bake node terminal, and its precompute is still `WAITING_INPUT`.
    """
    run = seed_granting_run(fake).record.run_id
    release_run(context(fake), run)
    settle_publish(fake, run)
    reserve_winner(fake, run)
    record = release_store(fake).read_run(run)
    assert record is not None and record.grant_id is not None
    release_store(fake).record_contribution(
        bake_member(record.grant_id, record.root_flow_id, shape)
    )
    state = bake_state or ("SUCCEEDED" if shape == "u1" else "FAILED")
    fake.items[(f"RUN#{run}", "NODE#bake")]["state"] = write_text(state)
    return run


def head_item(fake: FakeDynamo, unit: str = UNIT) -> dict[str, Any] | None:
    """Return the graph head item of one unit, if any."""
    return fake.items.get((f"HEAD#{unit}", "HEAD"))


def keys(fake: FakeDynamo, prefix: str) -> list[tuple[str, str]]:
    """Every stored key whose pk or sk starts with the prefix."""
    return sorted(k for k in fake.items if k[0].startswith(prefix) or k[1].startswith(prefix))
