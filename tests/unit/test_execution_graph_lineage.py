# SPDX-License-Identifier: MIT-0
"""One-step daily lineage and its repair refusal (S4, BF-4 S2 variant).

A failed identity-bearing candidate stays failed: repair and higher-fence reuse return
`LINEAGE_REPAIR_UNSUPPORTED`, the original graph, lineage head and receipts stay byte-identical,
and the co-release domain stays BLOCKED (`lineage_failed_owner_review`) so row 6 refuses every
window in it. A healthy predecessor gets exactly one one-step append, and an exact replay
returns the same grant.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

import pytest
from execution_graph_dispatch_support import NOW, UNIT, write_text
from execution_graph_release_support import (
    context,
    keys,
    release_store,
    seed_granting_run,
)
from execution_graph_support import FakeDynamo, FakeManifests

from adapters.inbound.run_requests.service import (
    AdmissionDependencies,
    RunRequest,
    admit_run_request,
)
from adapters.jobs.execution_graph.promoter import PromoterContext, PromotionCounts, promote_unit
from adapters.jobs.execution_graph.release import (
    ReleaseOutcome,
    apply_run,
    issue_grant,
    release_run,
)
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore, due_shard
from adapters.outbound.dynamodb.execution_graph_release import head_epoch
from domain.operations.execution_graph_dispatch import GraphGuardError, PromoterBranch
from domain.operations.execution_graph_release import (
    INGESTION_RELEASE_DOMAIN,
    LINEAGE_REPAIR_UNSUPPORTED,
    LineageHead,
    LineageRepairUnsupportedError,
    grant_identity,
    next_lineage_step,
    refuse_lineage_repair,
    reservation_set,
)
from domain.operations.ingestion_root import RETRIEVED_FORMAT, window_identity
from ports.outbound.execution_graph_release import GrantCommit, GrantRecord, ReleaseOrderHead

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

R1 = NOW - timedelta(hours=2)
R2 = NOW - timedelta(hours=1)


def _block_by_injected_apply_refusal(fake: FakeDynamo) -> tuple[str, str]:
    """Grant a run, then fail its apply by fault injection: a post-grant lineage failure."""
    seeded = seed_granting_run(fake, retrieved=R1)
    run = seeded.record.run_id
    store = release_store(fake)
    order = store.read_release_order(INGESTION_RELEASE_DOMAIN)
    reservation = reservation_set(run, INGESTION_RELEASE_DOMAIN, 1, ("fictional-db.orders",))
    store.acquire_ordinal(order, seeded.record, reservation)
    store.reserve_target(reservation, "fictional-db.orders")
    store.seal_proof(reservation)
    record = store.read_run(run)
    assert record is not None
    grant = issue_grant(context(fake), run, ordinal=1, expected_revision=record.revision)

    def newer_head(table: Any) -> None:
        table.items[(f"HEAD#{UNIT}", "HEAD")] = {
            "pk": write_text(f"HEAD#{UNIT}"),
            "sk": write_text("HEAD"),
            "retrieved_at": write_text(R2.isoformat()),
            "head_epoch": {"N": str(head_epoch(R2))},
        }

    fake.before_transaction = newer_head
    assert apply_run(context(fake), run) is ReleaseOutcome.LINEAGE_FAILED
    return run, grant.grant_id


def test_bf4_repair_of_a_failed_candidate_is_refused_and_changes_nothing() -> None:
    fake = FakeDynamo()
    run, grant = _block_by_injected_apply_refusal(fake)
    before = copy.deepcopy(fake.items)

    with pytest.raises(LineageRepairUnsupportedError) as refused:
        refuse_lineage_repair()
    assert refused.value.signal == LINEAGE_REPAIR_UNSUPPORTED
    with pytest.raises(LineageRepairUnsupportedError):
        next_lineage_step(release_store(fake).read_lineage(UNIT), grant_identity(run, 2))

    assert fake.items == before
    block = release_store(fake).read_block(INGESTION_RELEASE_DOMAIN)
    assert block is not None and block.reason == "lineage_failed_owner_review"
    head = release_store(fake).read_lineage(UNIT)
    assert (head.sequence, head.tail_grant_id, head.failed) == (1, grant, True)


def test_bf4_higher_fence_reuse_of_a_failed_candidate_is_refused_by_the_lineage_member() -> None:
    fake = FakeDynamo()
    _run, grant = _block_by_injected_apply_refusal(fake)
    # A later run of the same window, OPEN, whose grant command read the lineage head before
    # the failure was recorded: its one-step append would reuse the failed candidate's fence.
    later = seed_granting_run(fake, retrieved=NOW, fingerprint="9" * 64, request_id="request-later")
    store = release_store(fake)
    stale_head = LineageHead(1, grant, failed=False)
    reuse = grant_identity(later.record.run_id, 2)
    step = next_lineage_step(stale_head, reuse)
    commit = GrantCommit(
        run=later.record,
        grant=GrantRecord(
            reuse,
            later.record.run_id,
            INGESTION_RELEASE_DOMAIN,
            UNIT,
            2,
            "set",
            "digest",
            NOW.isoformat(),
            None,
            step.sequence,
            NOW.isoformat(),
        ),
        reservation=reservation_set(
            later.record.run_id, INGESTION_RELEASE_DOMAIN, 2, ("fictional-db.orders",)
        ),
        # The failure returned the ordinal, so the later run presents ordinal 2 as it would
        # have read it; the lineage member must still be the one that refuses.
        order=ReleaseOrderHead(INGESTION_RELEASE_DOMAIN, 3, 3, 2, later.record.run_id),
        lineage=step,
        lineage_head=stale_head,
        graph_head_epoch=None,
    )
    before = copy.deepcopy(fake.items)
    with pytest.raises(GraphGuardError) as guard:
        store.issue_grant(commit)
    assert guard.value.signal == LINEAGE_REPAIR_UNSUPPORTED
    assert fake.items == before


def test_bf4_the_blocked_domain_refuses_every_window_at_row_6_and_holds_its_waiters() -> None:
    fake = FakeDynamo()
    _block_by_injected_apply_refusal(fake)
    window = (
        "amc/dsp_clicks/query_pack=amc-dsp-clicks/source_scope=fictional/"
        # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
        "marketplace=ATVPDKIKX0DER/window=2026-10-01_2026-10-01/options=fictional/"
    )
    key = f"{window}retrieved={R2.strftime(RETRIEVED_FORMAT)}/manifest.json"

    class _Legacy:
        def completed_plan_key(self, key: str, version_id: str) -> str | None:
            del key, version_id
            return None

        def head_after(self, window: str, r: Any) -> None:
            del window, r

    receipt = admit_run_request(
        RunRequest(
            request_id="request-blocked-0001",
            protocol_version="v1",
            caller_kind="on_demand",
            reason="operator_rerun",
            manifest_key=key,
            version_id="v-1",
            requester="arn:aws:iam::000000000000:user/operator",
            requester_is_iam=True,
        ),
        AdmissionDependencies(
            store=DynamoExecutionGraphStore("graph", fake),  # type: ignore[arg-type]
            manifests=FakeManifests(retrieved_at=window_identity(key).retrieved_at),  # type: ignore[arg-type]
            now=lambda: NOW,
            legacy=_Legacy(),
        ),
    )
    assert (receipt.decision, receipt.reason, receipt.row) == (
        "REFUSED",
        "conflict_unit_blocked",
        6,
    )
    assert not any(pk.startswith("RUN#") and "request-blocked" in pk for pk, _ in fake.items)

    unit = window_identity(key).conflict_unit
    fake.items[(due_shard(unit), f"WAITER#{unit}")] = {
        "pk": write_text(due_shard(unit)),
        "sk": write_text(f"WAITER#{unit}"),
        "request_id": write_text("request-waiting"),
        "fingerprint": write_text("w" * 64),
        "retrieved_at": write_text(R2.isoformat()),
        "held": {"BOOL": False},
        "revision": {"N": "1"},
    }

    def no_nodes(run: object, receipt: object) -> tuple[()]:
        del run, receipt
        return ()

    promoter = PromoterContext(release_store(fake), no_nodes, lambda: NOW)
    assert promote_unit(promoter, unit, PromotionCounts()) is PromoterBranch.SKIP_BLOCKED
    assert (due_shard(unit), f"WAITER#{unit}") in fake.items


def test_bf4_positive_a_healthy_predecessor_gets_one_one_step_append_and_replay_is_exact() -> None:
    fake = FakeDynamo()
    first = seed_granting_run(fake, retrieved=R1, fingerprint="1" * 64, request_id="request-0001")
    assert release_run(context(fake), first.record.run_id) is ReleaseOutcome.APPLIED
    # S6 §2.2: apply arms only `publish`, so it holds the run's one POST_GRANT lock.
    for name in ("publish",):
        node = next(
            n for n in release_store(fake).read_nodes(first.record.run_id) if n.name == name
        )
        fake.items.pop((f"LOCK#{node.lock_key}", "LOCK"))
    second = seed_granting_run(fake, retrieved=R2, fingerprint="2" * 64, request_id="request-0002")
    assert release_run(context(fake), second.record.run_id) is ReleaseOutcome.APPLIED

    store = release_store(fake)
    first_grant = store.read_run(first.record.run_id)
    second_run = store.read_run(second.record.run_id)
    assert first_grant is not None and second_run is not None
    assert keys(fake, f"LINEAGE#{UNIT}") == [
        (f"LINEAGE#{UNIT}", "HEAD"),
        (f"LINEAGE#{UNIT}", "STEP#0000000001"),
        (f"LINEAGE#{UNIT}", "STEP#0000000002"),
    ]
    step = fake.items[(f"LINEAGE#{UNIT}", "STEP#0000000002")]
    assert step["predecessor_grant_id"] == {"S": first_grant.grant_id}
    assert step["candidate_grant_id"] == {"S": second_run.grant_id}
    before = copy.deepcopy(fake.items)
    replay = issue_grant(context(fake), second.record.run_id, ordinal=None, expected_revision=None)
    assert replay.grant_id == second_run.grant_id
    assert fake.items == before
