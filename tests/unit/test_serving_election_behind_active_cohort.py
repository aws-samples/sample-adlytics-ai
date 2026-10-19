# SPDX-License-Identifier: MIT-0
"""Candidates that become ready behind an ACTIVE direct-only cohort are elected together.

The acceptance control (x8 sequencing, 2026-10-16) launches wave A while the single-root cohort
is ACTIVE, so both A members become eligible behind it and the first election after its finalize
forms one A cohort. This proves the runtime behaves that way on `FakeDynamo` through the real
promoter pass and release sweep:

- while the single cohort is ACTIVE (and while it is bound), no candidate attaches to it and no
  `SERVING_TRANSITION#` is written: a cohort without aliases has no final-alias drain;
- its direct completion returns the head to IDLE, and the next pass elects both A members as one
  cohort (direct plus alias).

The paired control is the old sequencing (A launched only after the single root ends): members
that become ready in different passes split across cohorts.

Fictional data only; nothing here reaches AWS.
"""

from __future__ import annotations

from typing import Any

import pytest
from execution_graph_dispatch_support import write_text
from execution_graph_release_support import context, release_store
from fixed_member_byte_fixture_support import (
    NOW,
    FakeDynamo,
    complete_direct_winner,
    grant_and_publish,
    promoter_pass,
)

from adapters.jobs.execution_graph.release import contribute, try_complete_granted
from domain.operations.execution_graph_release import (
    INGESTION_RELEASE_DOMAIN,
    CohortContribution,
    CohortMember,
    CohortOutcome,
)
from ports.outbound.execution_graph_release import ServingMutationResult

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]


def _head(fake: FakeDynamo) -> dict[str, Any]:
    return fake.items[(f"SERVING_DOMAIN#{INGESTION_RELEASE_DOMAIN}", "HEAD")]


def _elected(fake: FakeDynamo) -> set[str]:
    return {str(item["S"]) for item in _head(fake).get("elected_run_ids", {}).get("L", [])}


def _transitions(fake: FakeDynamo) -> list[tuple[str, str]]:
    return [key for key in fake.items if key[0].startswith("SERVING_TRANSITION#")]


def test_runs_ready_behind_an_active_direct_only_cohort_are_elected_together() -> None:
    fake = FakeDynamo()
    single = grant_and_publish(fake, "s", 0)
    promoter_pass(fake, "elect-single")
    assert _head(fake)["state"] == write_text("ACTIVE") and _elected(fake) == {single}

    # Wave A becomes ready while the single cohort is ACTIVE: nothing attaches to it.
    a_runs = {grant_and_publish(fake, "a", index) for index in range(2)}
    promoter_pass(fake, "pass-while-active")
    assert _head(fake)["state"] == write_text("ACTIVE") and _elected(fake) == {single}
    assert _transitions(fake) == []

    # The single cohort binds (both contributions): a pass on the bound cohort drains nothing,
    # because a cohort without aliases has no final-alias drain.
    store = release_store(fake)
    record = store.read_run(single)
    assert record is not None and record.grant_id is not None
    for member in (CohortMember.PRECOMPUTE, CohortMember.BAKE):
        contribute(
            store,
            CohortContribution(
                record.grant_id, member, CohortOutcome.NO_ELIGIBLE, None, NOW, record.root_flow_id
            ),
            record,
        )
    promoter_pass(fake, "pass-while-bound")
    assert _transitions(fake) == [] and _elected(fake) == {single}
    # Its direct completion: still no drain transaction, head back to IDLE.
    for name in ("precompute", "bake"):
        fake.items[(f"RUN#{single}", f"NODE#{name}")]["state"] = write_text("SUCCEEDED")
    assert try_complete_granted(context(fake), single) is ServingMutationResult.COMMITTED
    assert _transitions(fake) == []
    assert _head(fake)["state"] == write_text("IDLE")

    # The first pass after its finalize elects both A members as one cohort.
    promoter_pass(fake, "elect-a")
    assert _head(fake)["state"] == write_text("ACTIVE")
    assert _elected(fake) == a_runs
    assert _transitions(fake) == []


def test_the_old_sequencing_splits_members_ready_in_different_passes() -> None:
    # Paired control: A launched only after the single root ends, its members ready one pass
    # apart (x4, x7): the first pass elects one member alone.
    fake = FakeDynamo()
    single = grant_and_publish(fake, "s", 0)
    promoter_pass(fake, "elect-single")
    complete_direct_winner(fake, single)
    first = grant_and_publish(fake, "a", 0)
    promoter_pass(fake, "elect-first")
    grant_and_publish(fake, "a", 1)
    promoter_pass(fake, "pass-after-second")
    assert _elected(fake) == {first}
