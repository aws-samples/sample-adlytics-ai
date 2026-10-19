# SPDX-License-Identifier: MIT-0
"""The POST_GRANT sibling cohort, the serving binding and the ungranted-generation refusal (S4).

Bake and precompute are POST_GRANT siblings of one grant. Their terminals join in a durable
cohort settlement in either order, and only a settled cohort binds serving to one exact artifact
revision with its contributor roots. Presentation refuses a graph-authority generation that no
binding names; a legacy-authority artifact (before cutover) is served as before.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from execution_graph_dispatch_support import NOW
from execution_graph_release_support import (
    context,
    release_store,
    reserve_winner,
    seed_granting_run,
    settle_publish,
)
from execution_graph_support import FakeDynamo

from adapters.jobs.execution_graph.release import contribute, release_run
from domain.operations.execution_graph_dispatch import ServingCandidate, ServingFrontierVector
from domain.operations.execution_graph_release import (
    UNGRANTED_GENERATION,
    BindingKind,
    CohortContribution,
    CohortMember,
    CohortOutcome,
    ServingBinding,
    ServingInputFact,
    ServingOutcome,
    ServingReuseExpectation,
    UnavailableClass,
    UngrantedGenerationError,
    aggregate_serving_outcome,
    bind_serving,
    binding_authorizes_serving,
    cohort_revision,
    failure_entries,
    require_granted_generation,
    serving_input_revision,
    serving_outcome,
    serving_withheld,
    settle_cohort,
    unavailable_class,
    validate_exact_input_reuse,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

GRANT = "a" * 64
ROOT = "root-fictional-0001"


def _bake(revision: str | None = "rev-0001", *, after: int = 1) -> CohortContribution:
    outcome = CohortOutcome.SUCCEEDED if revision else CohortOutcome.UNAVAILABLE
    return CohortContribution(
        GRANT,
        CohortMember.BAKE,
        outcome,
        revision,
        NOW + timedelta(seconds=after),
        ROOT,
        () if revision else (("campaigns#7d", "campaign_materialization_unavailable"),),
    )


def _precompute(
    *, after: int = 2, outcome: CohortOutcome = CohortOutcome.SUCCEEDED
) -> CohortContribution:
    return CohortContribution(
        GRANT,
        CohortMember.PRECOMPUTE,
        outcome,
        None if outcome is not CohortOutcome.SUCCEEDED else "rev-0001",
        NOW + timedelta(seconds=after),
        ROOT,
    )


@pytest.mark.parametrize("order", ["bake_first", "precompute_first"])
def test_siblings_join_in_either_order_and_the_join_waits_for_both(order: str) -> None:
    items = [_bake(), _precompute()]
    if order == "precompute_first":
        items.reverse()
    assert settle_cohort(GRANT, tuple(CohortMember), items[:1]) is None
    settled = settle_cohort(GRANT, tuple(CohortMember), items)
    assert settled is not None
    assert [item.member for item in settled.contributions] == [
        CohortMember.BAKE,
        CohortMember.PRECOMPUTE,
    ]
    assert settled.settled_at == NOW + timedelta(seconds=2)


def test_a_retried_contribution_is_the_same_and_a_conflicting_one_is_refused() -> None:
    assert settle_cohort(GRANT, (CohortMember.BAKE,), [_bake(), _bake()]) is not None
    with pytest.raises(ValueError, match="two different terminals"):
        settle_cohort(GRANT, (CohortMember.BAKE,), [_bake(), _bake("rev-0002")])
    with pytest.raises(ValueError, match="different grant"):
        settle_cohort("b" * 64, (CohortMember.BAKE,), [_bake()])


def test_unavailable_and_no_eligible_siblings_bind_no_artifact() -> None:
    settled = settle_cohort(
        GRANT,
        tuple(CohortMember),
        [_bake(None), _precompute(outcome=CohortOutcome.NO_ELIGIBLE)],
    )
    assert settled is not None
    binding = bind_serving(settled, conflict_unit="u", served_revision=NOW)
    assert binding.artifact_revision is None
    with pytest.raises(UngrantedGenerationError):
        require_granted_generation({GRANT: binding}, "rev-0001", graph_authority=True)


def test_many_contributors_bind_one_exact_revision_and_presentation_returns_it() -> None:
    settled = settle_cohort(GRANT, tuple(CohortMember), [_bake(), _precompute()])
    assert settled is not None
    binding = bind_serving(
        settled,
        conflict_unit="u",
        served_revision=NOW,
        contributor_roots=("root-fictional-0002", ROOT),
        domain="domain-fictional",
        cohort_revision_value="cohort-1",
        serving_input_revision_value="input-1",
        plan_digest="plan-1",
        winner_run_id="run-1",
        execution_attempts=1,
    )
    assert binding.contributor_roots == ("root-fictional-0001", "root-fictional-0002")
    assert binding.artifact_revision == "rev-0001"
    served = require_granted_generation({GRANT: binding}, "rev-0001", graph_authority=True)
    assert served is not None and served.artifact_revision == "rev-0001"
    assert (
        bind_serving(
            settled,
            conflict_unit="u",
            served_revision=NOW,
            contributor_roots=(
                ROOT,
                "root-fictional-0002",
            ),
            domain="domain-fictional",
            cohort_revision_value="cohort-1",
            serving_input_revision_value="input-1",
            plan_digest="plan-1",
            winner_run_id="run-1",
            execution_attempts=1,
        ).binding_id
        == binding.binding_id
    )


def test_presentation_refuses_an_ungranted_graph_generation_and_serves_legacy_as_before() -> None:
    with pytest.raises(UngrantedGenerationError) as refused:
        require_granted_generation({}, "rev-ungranted", graph_authority=True)
    assert refused.value.signal == UNGRANTED_GENERATION
    assert require_granted_generation({}, "rev-legacy", graph_authority=False) is None


def test_a_settled_cohort_binds_serving_once_through_the_store() -> None:
    fake = FakeDynamo()
    seeded = seed_granting_run(fake)
    run = seeded.record.run_id
    release_run(context(fake), run)
    settle_publish(fake, run)
    reserve_winner(fake, run)
    store = release_store(fake)
    record = store.read_run(run)
    assert record is not None and record.grant_id is not None
    grant = record.grant_id
    bake = CohortContribution(
        grant, CohortMember.BAKE, CohortOutcome.SUCCEEDED, "rev-0001", NOW, record.root_flow_id
    )
    precompute = CohortContribution(
        grant,
        CohortMember.PRECOMPUTE,
        CohortOutcome.SUCCEEDED,
        "rev-0001",
        NOW + timedelta(seconds=5),
        record.root_flow_id,
    )

    assert contribute(store, precompute, record).binding is None
    assert store.read_serving_binding(grant) is None
    binding = contribute(store, bake, record).binding
    assert binding is not None and binding.artifact_revision == "rev-0001"
    assert binding.served_revision.isoformat() == record.retrieved_at
    assert binding.bound_at - NOW == timedelta(seconds=5)
    # Retry: same binding, no second write, settled from the stored contribution.
    retried = contribute(store, bake, record)
    assert (retried.binding, retried.replayed, retried.contribution) == (binding, True, bake)
    assert store.read_serving_binding(grant) == binding


# GRAPH serving coalescing Slice 1: separate lineage/content revisions and binding truth.


def _candidate(ordinal: int) -> ServingCandidate:
    return ServingCandidate(
        run_id=f"run-{ordinal}",
        grant_id=f"grant-{ordinal}",
        root_flow_id=f"root-{ordinal}",
        domain="domain-fictional",
        grant_ordinal=ordinal,
        publication_effect_digest=f"effect-{ordinal}",
    )


def _direct(**changes: object) -> ServingBinding:
    values: dict[str, Any] = {
        "binding_id": "direct-1",
        "grant_id": "grant-2",
        "conflict_unit": "unit",
        "served_revision": NOW,
        "artifact_revision": "artifact-1",
        "contributor_roots": (ROOT,),
        "bound_at": NOW,
        "domain": "domain-fictional",
        "cohort_revision": "cohort-1",
        "serving_input_revision": "input-1",
        "plan_digest": "plan-1",
        "binding_kind": BindingKind.DIRECT,
        "winner_run_id": "run-2",
        "execution_attempts": 1,
    }
    values.update(changes)
    return ServingBinding(**values)


def test_cohort_revision_changes_with_roster_but_input_revision_does_not() -> None:
    frontier = ServingFrontierVector(
        (1,) * 32, (2, *(0 for _ in range(31))), (2, *(0 for _ in range(31)))
    )
    one = cohort_revision("domain-fictional", (_candidate(2),), frontier)
    two = cohort_revision("domain-fictional", (_candidate(1), _candidate(2)), frontier)
    assert one != two
    facts = (ServingInputFact("p", "publication-1", "target-1", "prompt-1", "input-1"),)
    assert serving_input_revision("domain-fictional", facts, "plan-1") == (
        serving_input_revision("domain-fictional", tuple(reversed(facts)), "plan-1")
    )


def test_alias_cannot_authorize_generation_and_direct_truth_must_be_complete() -> None:
    direct = _direct()
    assert (
        require_granted_generation({direct.binding_id: direct}, "artifact-1", graph_authority=True)
        == direct
    )
    legacy = _direct(
        binding_kind=None,
        domain=None,
        cohort_revision=None,
        serving_input_revision=None,
        plan_digest=None,
    )
    assert (
        require_granted_generation({legacy.binding_id: legacy}, "artifact-1", graph_authority=True)
        == legacy
    )
    with pytest.raises(ValueError, match="artifact generation authority"):
        _direct(
            binding_id="invalid-alias",
            binding_kind=BindingKind.ALIAS,
            direct_binding_id=direct.binding_id,
            execution_attempts=0,
        )
    alias = _direct(
        binding_id="alias-1",
        artifact_revision=None,
        binding_kind=BindingKind.ALIAS,
        direct_binding_id=direct.binding_id,
        execution_attempts=0,
    )
    with pytest.raises(UngrantedGenerationError):
        require_granted_generation({alias.binding_id: alias}, "artifact-1", graph_authority=True)
    rejected = (
        _direct(binding_id="stale-1", stale=True),
        _direct(binding_id="partial-1", unavailable=(("campaigns#7d", "unavailable"),)),
        _direct(binding_id="unexecuted-1", execution_attempts=0),
    )
    for binding in rejected:
        with pytest.raises(UngrantedGenerationError):
            require_granted_generation(
                {binding.binding_id: binding}, "artifact-1", graph_authority=True
            )


_DESIGNED = (
    ("reach#last_complete_month", "reach_materialization_unavailable"),
    ("precompute#campaigns#7d#insight", "insight_slot_unavailable"),
)
"""A designed-absence-only binding: a page source with no bakeable period and an empty slot."""


@pytest.mark.covers("ADR-139")
def test_a_designed_absence_binding_is_granted_and_its_paired_failures_are_not() -> None:
    designed = _direct(unavailable=_DESIGNED)
    assert binding_authorizes_serving(designed)
    assert (
        require_granted_generation(
            {designed.binding_id: designed}, "artifact-1", graph_authority=True
        )
        == designed
    )
    # Controls: an unclassified reason, a page reason on another page's key, and every
    # non-authoritative shape still refuse with otherwise designed-absence-only entries.
    rejected = (
        _direct(binding_id="partial-1", unavailable=(("campaigns#7d", "unavailable"),)),
        _direct(
            binding_id="other-page",
            unavailable=(("campaigns#7d", "reach_materialization_unavailable"),),
        ),
        _direct(binding_id="stale-1", stale=True, unavailable=_DESIGNED),
        _direct(binding_id="unexecuted-1", execution_attempts=0, unavailable=_DESIGNED),
    )
    for binding in rejected:
        with pytest.raises(UngrantedGenerationError):
            require_granted_generation(
                {binding.binding_id: binding}, "artifact-1", graph_authority=True
            )
    alias = _direct(
        binding_id="alias-1",
        artifact_revision=None,
        binding_kind=BindingKind.ALIAS,
        direct_binding_id=designed.binding_id,
        execution_attempts=0,
        unavailable=_DESIGNED,
    )
    with pytest.raises(UngrantedGenerationError):
        require_granted_generation({alias.binding_id: alias}, "artifact-1", graph_authority=True)
    # Exact-input reuse stays strict: the same designed-absence binding is never reused.
    expected = ServingReuseExpectation("domain-fictional", "input-1", "plan-1")
    assert not validate_exact_input_reuse(designed, expected)
    assert serving_outcome(designed) is ServingOutcome.PARTIAL


@pytest.mark.covers("ADR-139")
@pytest.mark.parametrize(
    ("key", "reason", "expected"),
    [
        ("reach#last_complete_month", "reach_materialization_unavailable", "designed_absence"),
        ("retail-health#7d", "retail_health_materialization_unavailable", "designed_absence"),
        ("any-key", "insight_slot_unavailable", "designed_absence"),
        ("insight-without-hash", "insight_slot_unavailable", "designed_absence"),
        ("unknownpage#v", "unknownpage_materialization_unavailable", "failure"),
        ("reach", "reach_materialization_unavailable", "failure"),
        ("campaigns#7d", "reach_materialization_unavailable", "failure"),
        ("campaigns#7d", "precompute_budget_exhausted", "failure"),
        ("campaigns#7d", "bake_budget_exhausted", "failure"),
        ("campaigns#7d", "post_grant_invariant", "failure"),
        ("campaigns#7d", "a_pre_v3_source_reason", "failure"),
    ],
)
def test_unavailable_class_is_closed_and_fails_closed_without_raising(
    key: str, reason: str, expected: str
) -> None:
    assert unavailable_class(key, reason) is UnavailableClass(expected)


@pytest.mark.covers("ADR-139")
def test_only_a_direct_executed_current_revision_naming_binding_authorizes_serving() -> None:
    assert binding_authorizes_serving(_direct(unavailable=_DESIGNED))
    legacy = _direct(
        binding_kind=None,
        domain=None,
        cohort_revision=None,
        serving_input_revision=None,
        plan_digest=None,
        unavailable=_DESIGNED,
    )
    alias = _direct(
        artifact_revision=None,
        binding_kind=BindingKind.ALIAS,
        direct_binding_id="direct-1",
        execution_attempts=0,
        unavailable=_DESIGNED,
    )
    for binding in (
        legacy,
        alias,
        _direct(execution_attempts=0, unavailable=_DESIGNED),
        _direct(stale=True, unavailable=_DESIGNED),
        _direct(artifact_revision=None, unavailable=_DESIGNED),
    ):
        assert not binding_authorizes_serving(binding)


@pytest.mark.covers("ADR-139")
def test_withheld_means_a_failure_kept_pages_on_last_good_never_designed_absence_alone() -> None:
    exhausted = (("precompute#campaigns#7d#insight", "precompute_budget_exhausted"),)
    # Not withheld: authorized, or no revision and nothing failed (every row designed absent).
    assert not serving_withheld(_direct(unavailable=_DESIGNED))
    assert not serving_withheld(_direct(artifact_revision=None, unavailable=(_DESIGNED[0],)))
    # Withheld: a failure entry with or without a revision, and a non-authoritative binding
    # that names a revision (fails toward alarming).
    assert serving_withheld(_direct(unavailable=(*_DESIGNED, *exhausted)))
    assert serving_withheld(_direct(artifact_revision=None, unavailable=exhausted))
    alias = _direct(
        artifact_revision=None,
        binding_kind=BindingKind.ALIAS,
        direct_binding_id="direct-1",
        execution_attempts=0,
    )
    assert not serving_withheld(alias)  # an alias names no revision and no failure
    assert serving_withheld(_direct(execution_attempts=0, unavailable=_DESIGNED))
    assert serving_withheld(_direct(stale=True, unavailable=_DESIGNED))
    # failure_entries returns exactly the FAILURE entries, in binding order.
    mixed = _direct(unavailable=(*_DESIGNED, *exhausted, ("campaigns#7d", "unavailable")))
    assert failure_entries(mixed) == (*exhausted, ("campaigns#7d", "unavailable"))


def test_exact_reuse_requires_complete_executed_typed_direct_binding() -> None:
    expected = ServingReuseExpectation("domain-fictional", "input-1", "plan-1")
    assert validate_exact_input_reuse(_direct(), expected)
    assert not validate_exact_input_reuse(_direct(execution_attempts=0), expected)
    legacy = _direct(
        binding_kind=None,
        domain=None,
        cohort_revision=None,
        serving_input_revision=None,
        plan_digest=None,
    )
    assert not validate_exact_input_reuse(legacy, expected)
    assert not validate_exact_input_reuse(_direct(artifact_revision=None), expected)
    assert not validate_exact_input_reuse(
        _direct(unavailable=(("campaigns#7d", "unavailable"),)), expected
    )
    assert not validate_exact_input_reuse(_direct(stale=True), expected)


def test_direct_truth_precedes_aliases_and_aggregate_outcome_comes_from_direct() -> None:
    direct = _direct()
    alias = _direct(
        binding_id="alias-1",
        grant_id="grant-1",
        artifact_revision=None,
        binding_kind=BindingKind.ALIAS,
        direct_binding_id=direct.binding_id,
        execution_attempts=0,
    )
    assert aggregate_serving_outcome(direct, (alias,)) is ServingOutcome.COMPLETE
    with pytest.raises(ValueError, match="direct binding first"):
        aggregate_serving_outcome(alias, ())
    with pytest.raises(ValueError, match="exact direct"):
        aggregate_serving_outcome(
            direct,
            (
                _direct(
                    artifact_revision=None,
                    binding_kind=BindingKind.ALIAS,
                    direct_binding_id="other-direct",
                    execution_attempts=0,
                ),
            ),
        )


def test_exact_reuse_checks_only_the_presented_latest_direct_without_older_fallback() -> None:
    expected = ServingReuseExpectation("domain-fictional", "input-1", "plan-1")
    older = _direct(binding_id="older")
    latest = _direct(binding_id="latest", serving_input_revision="input-2")
    assert validate_exact_input_reuse(older, expected)
    assert not validate_exact_input_reuse(latest, expected)


@pytest.mark.covers("ADR-140")
def test_adr140_d2_a_u1_binding_neither_serves_nor_is_withheld_and_a_u2_binding_is_withheld() -> (
    None
):
    """ADR-140 keeps ADR-139's predicates: U1 is a quiet no-eligible, U2 pages withheld."""
    u1 = _direct(artifact_revision=None, unavailable=())
    u2 = _direct(
        artifact_revision=None,
        unavailable=(
            ("campaigns#30d", "post_grant_invariant"),
            ("campaigns#7d", "post_grant_invariant"),
        ),
    )
    assert (binding_authorizes_serving(u1), serving_withheld(u1)) == (False, False)
    assert (binding_authorizes_serving(u2), serving_withheld(u2)) == (False, True)
