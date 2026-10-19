# SPDX-License-Identifier: MIT-0
"""Phase-one delivery planning: composition, canonical roster, feasibility, and §5.3 acknowledgment.

Pure adapter logic with no AWS and no I/O: the roster and classifier are computed over fictional
typed inputs and the packer that measures ``M(u)`` is the real §6 composer over the real catalog.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from phase_one_support import (
    CATALOG,
    CONTRACT_ID,
    DATASET,
    FENCING_TOKEN,
    INSTANT,
    LANDED,
    MANIFEST_KEY,
    MANIFEST_VERSION,
    NOW,
    ORIGIN_SOURCE_IDENTITY,
    PLAN_DIGEST,
    PUBLICATION_ID,
    SETTINGS,
    TARGET,
    digest,
    partition,
    prepared,
    published,
    result,
    rows,
    source,
)

from adapters.config import ProjectionHandoffState, Stage1DeliverySettings
from adapters.inbound.ingestion_handlers import delivery_plan
from adapters.inbound.ingestion_handlers.delivery_plan import (
    FEASIBILITY_REFUSED,
    SOURCE_OBJECT_FENCING_TOKEN,
    TABLE_KIND_CANDIDATE,
    TABLE_KIND_CURRENT,
    TABLE_KIND_SOURCE,
    FeasibilityCheck,
    PreparedRosterRequest,
    ProjectionComposition,
    PublishedRosterRequest,
    prepared_roster,
    projection_composition,
    published_roster,
    require_feasible,
)
from adapters.jobs.ingest_delivery.semantic_sidecar import (
    PreparedProjectionPartition,
    SemanticSidecarReceipt,
)
from adapters.outbound.athena.table_attempt_statements import (
    ComposeUnitStatementsRequest,
    compose_unit_statements,
)
from adapters.outbound.s3.semantic_storage import canonical_object_key
from domain.operations.projection_feasibility import FeasibleProjection
from domain.operations.source_delivery import (
    SOURCE_UNACKNOWLEDGED,
    SourceDeliveryObservation,
    classify_source_delivery,
    source_acknowledgment_digest,
)
from domain.operations.stage1_lifecycle_head import Stage1LifecycleHeadTuple
from domain.operations.stage1_operational_status import (
    PlanIdentityRequest,
    Stage1Surface,
    derive_plan_identity,
)
from domain.operations.status import FlowState
from domain.operations.write_plan import (
    IntentMeta,
    IntentOrigin,
    IntentOriginKind,
    IntentState,
    PlanMeta,
    PlanStatus,
    SuccessorOwner,
    plan_digest_for_source_delivery,
    ready_order_key,
    source_delivery_identity,
)
from domain.semantics.canonical_compiler import (
    canonical_checksum_digest,
    canonical_rows_checksum,
)
from domain.shared_kernel.errors import InvariantError, ValidationError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

# --- Composition ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "composition"),
    (
        (ProjectionHandoffState.INLINE_ACTIVE, ProjectionComposition.INLINE),
        (ProjectionHandoffState.INLINE_QUIESCED, ProjectionComposition.INLINE),
        (ProjectionHandoffState.QUEUED_QUIESCED, ProjectionComposition.QUEUED),
        (ProjectionHandoffState.QUEUED_ACCUMULATE, ProjectionComposition.QUEUED),
        (ProjectionHandoffState.QUEUED_ACTIVE, ProjectionComposition.QUEUED),
        (ProjectionHandoffState.QUEUED_DRAIN, ProjectionComposition.QUEUED),
    ),
)
def test_composition_follows_the_six_state_resolver_row(
    state: ProjectionHandoffState, composition: ProjectionComposition
) -> None:
    assert projection_composition(state) is composition


# --- §3 identity constructors ---------------------------------------------------------------------


def test_source_delivery_identity_and_plan_digest_reproduce_the_golden_bytes() -> None:
    """The delivery's stable identity is the sole §3 constructor over the exact manifest pair."""
    origin = source_delivery_identity(((MANIFEST_KEY, MANIFEST_VERSION),))
    assert origin == ORIGIN_SOURCE_IDENTITY
    assert origin == digest(["stage1-source-delivery/v1", [[MANIFEST_KEY, MANIFEST_VERSION]]])
    plan_digest = plan_digest_for_source_delivery(origin)
    assert plan_digest == PLAN_DIGEST
    assert plan_digest == digest(["stage1-plan-digest/v1", origin])
    assert len(plan_digest) == 64
    assert (
        plan_digest_for_source_delivery(
            source_delivery_identity(((MANIFEST_KEY, "manifest-version-other"),))
        )
        != plan_digest
    )


# --- Roster ---------------------------------------------------------------------------------------


def test_published_roster_shapes_three_units_in_unit_key_byte_order_with_exact_members() -> None:
    roster = published_roster(PublishedRosterRequest(published(), TARGET, CATALOG))

    candidate, current, source = roster.units
    assert [unit.entry.unit_key for unit in roster.units] == [
        f"a_candidate.{DATASET}",
        f"m_current.{DATASET}",
        f"z_source.{CATALOG.source_for_interpreter(f'source_pack.{CONTRACT_ID}').name}",
    ]
    assert (candidate.entry.table_kind, current.entry.table_kind, source.entry.table_kind) == (
        TABLE_KIND_CANDIDATE,
        TABLE_KIND_CURRENT,
        TABLE_KIND_SOURCE,
    )
    (current_object,) = current.entry.objects
    assert current_object.kind == "canonical"
    assert current_object.reference == canonical_object_key("successful", DATASET, PUBLICATION_ID)
    assert current_object.publication_id == PUBLICATION_ID
    assert current_object.partition_key == partition().identity
    assert current_object.fencing_token == FENCING_TOKEN
    assert current_object.content_checksum == canonical_checksum_digest(
        canonical_rows_checksum(rows())
    )
    assert len(current_object.content_checksum) == 64
    (candidate_object,) = candidate.entry.objects
    assert candidate_object.reference == canonical_object_key("candidate", DATASET, PUBLICATION_ID)
    assert candidate_object.partition_key is None
    (source_object,) = source.entry.objects
    assert source_object.kind == "source"
    assert source_object.reference == source_object.publication_id == "source-fictional-0001"
    assert source_object.partition_key is None
    assert source_object.fencing_token == SOURCE_OBJECT_FENCING_TOKEN
    assert [target.partition_key for target in current.servable] == [partition().identity]
    assert candidate.servable == () and source.servable == ()


def test_roster_merge_counts_come_from_the_single_packer_over_the_unit_workload() -> None:
    roster = published_roster(PublishedRosterRequest(published(row_count=3), TARGET, CATALOG))

    for unit in roster.units:
        composed = compose_unit_statements(
            ComposeUnitStatementsRequest(workload=unit.workload, workgroup="fictional-batch")
        )
        assert unit.entry.merge_count == composed.merge_count == unit.measurement.merge_count
        assert unit.entry.merge_count >= 1
        assert unit.composition.verification.kind.value == "verification"


def test_zero_row_canonical_objects_join_no_unit_while_every_source_object_does() -> None:
    """§2.1 rule 3: source objects always carry their envelope; rowless canonicals emit nothing."""
    empty = source(rows=0)
    identity = partition()
    rowless = SemanticSidecarReceipt(
        landed_delivery=LANDED,
        source_publications=(empty,),
        candidate=None,
        successful=None,
    )
    roster = published_roster(PublishedRosterRequest((result(rowless),), TARGET, CATALOG))
    assert [unit.entry.table_kind for unit in roster.units] == [TABLE_KIND_SOURCE]

    rowless_plan = replace(
        prepared(),
        partitions=(
            PreparedProjectionPartition(
                contract_id=CONTRACT_ID,
                dataset=DATASET,
                partition_identity=identity.identity,
                source_publications=(empty,),
                canonical_rows=(),
                content_checksum=canonical_rows_checksum(()),
            ),
        ),
    )
    assert [
        unit.entry.table_kind
        for unit in prepared_roster(PreparedRosterRequest(rowless_plan, TARGET, CATALOG, NOW)).units
    ] == [TABLE_KIND_SOURCE]


def test_prepared_roster_substitutes_minted_length_identities_and_matches_published_shape() -> None:
    """Identities minted after the lease are stand-ins of the exact encoded length (§2.1)."""
    prepared_units = prepared_roster(PreparedRosterRequest(prepared(), TARGET, CATALOG, NOW))
    published_units = published_roster(PublishedRosterRequest(published(), TARGET, CATALOG))

    assert [unit.entry.unit_key for unit in prepared_units.units] == [
        unit.entry.unit_key for unit in published_units.units
    ]
    assert [unit.entry.merge_count for unit in prepared_units.units] == [
        unit.entry.merge_count for unit in published_units.units
    ]
    _, current, _ = prepared_units.units
    (current_object,) = current.entry.objects
    assert len(current_object.publication_id) == len("publication-") + 32
    assert current_object.publication_id.startswith("publication-")
    assert current_object.partition_key == partition().identity
    assert current_object.content_checksum == canonical_checksum_digest(
        canonical_rows_checksum(rows())
    )
    assert current_object.fencing_token == 2**63 - 1


def test_conflicting_repeated_source_evidence_is_refused_before_shaping() -> None:
    first = SemanticSidecarReceipt(landed_delivery=LANDED, source_publications=(source(rows=1),))
    conflicting = SemanticSidecarReceipt(
        landed_delivery=LANDED, source_publications=(source(rows=2),)
    )
    with pytest.raises(ValueError, match="conflicting evidence"):
        published_roster(
            PublishedRosterRequest((result(first), result(conflicting)), TARGET, CATALOG)
        )


# --- Feasibility ----------------------------------------------------------------------------------


def test_feasible_roster_proves_the_clean_attempt_sum_over_the_complete_roster() -> None:
    roster = published_roster(PublishedRosterRequest(published(), TARGET, CATALOG))
    proof = require_feasible(FeasibilityCheck(roster, prepared().groups, SETTINGS))

    assert isinstance(proof, FeasibleProjection)
    assert proof.creates_plan is True
    assert proof.clean_attempts == sum(unit.entry.merge_count + 1 for unit in roster.units)
    assert [key for key, _ in proof.unit_clean_attempts] == [
        unit.entry.unit_key for unit in roster.units
    ]


def test_empty_roster_is_feasible_and_creates_no_plan() -> None:
    proof = require_feasible(FeasibilityCheck(delivery_plan.DeliveryRoster(()), (), SETTINGS))
    assert proof == FeasibleProjection(clean_attempts=0, unit_clean_attempts=(), creates_plan=False)


@pytest.mark.parametrize(
    ("settings", "reason"),
    (
        (replace(SETTINGS, max_group_members=1), "group_members_exceed_cap"),
        (replace(SETTINGS, max_units=2), "units_exceed_cap"),
        (replace(SETTINGS, max_accepted_attempts_per_plan=3), "plan_clean_attempts_exceed_cap"),
        (replace(SETTINGS, max_accepted_attempts_per_unit=1), "unit_clean_attempts_exceed_cap"),
        (replace(SETTINGS, max_pointers_per_table=0), "pointers_exceed_cap"),
    ),
)
def test_over_budget_rosters_refuse_typed_with_the_closed_reason(
    settings: Stage1DeliverySettings, reason: str
) -> None:
    roster = published_roster(PublishedRosterRequest(published(), TARGET, CATALOG))
    identity = partition().identity
    groups = (((CONTRACT_ID, identity), ("other", identity)),)

    with pytest.raises(ValidationError) as caught:
        require_feasible(FeasibilityCheck(roster, groups, settings))

    assert caught.value.code == FEASIBILITY_REFUSED
    assert caught.value.context["reason"] == reason


# --- §5.3 acknowledgment classifier ---------------------------------------------------------------

INTENT_ID = "8595e699935a8ec7ee6d3d87d3e0214300e09831da6a4808f10c9fab4174269b"
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
ROSTER_DIGEST = "e7294e98228c1dd05947ca0655c8b6015a98244d67e0c544873525e802448d66"
EXECUTION_ID = "ab" * 32
PLAN_KEY = f"WRITE_PLAN#{PLAN_DIGEST}#{EXECUTION_ID}"
ORIGIN = IntentOrigin(IntentOriginKind.SOURCE_DELIVERY, ORIGIN_SOURCE_IDENTITY)


def _intent(state: IntentState, generation: int | None = None) -> IntentMeta:
    pending = state is IntentState.PENDING
    return IntentMeta(
        root_plan_key=None,
        source_plan_key=None,
        plan_digest=PLAN_DIGEST,
        intent_id=INTENT_ID,
        roster_digest=ROSTER_DIGEST,
        origin=ORIGIN,
        state=state,
        claimed_generation=None if pending else generation,
        retry_count=0,
        due_at=INSTANT,
        ready_status="ready" if pending else None,
        ready_order=ready_order_key(INSTANT, INTENT_ID) if pending else None,
        created_at=INSTANT,
        last_transition_at=INSTANT,
    )


def _bootstrap_owner(revision: int = 0) -> SuccessorOwner:
    return SuccessorOwner(
        revision=revision,
        root_plan_key=None,
        last_plan_key=None,
        last_generation=-1,
        active_plan_key=None,
        active_intent_id=None,
        reserved_intent_id=None,
        reserved_intent_digest=None,
        reserved_generation=None,
        latest_internal_execution_id=None,
    )


def _plan(status: PlanStatus, *, admitted: int = 0) -> PlanMeta:
    return PlanMeta(
        plan_digest=PLAN_DIGEST,
        internal_execution_id=EXECUTION_ID,
        root_plan_key=PLAN_KEY,
        generation=0,
        source_plan_key=None,
        origin_intent_id=INTENT_ID,
        roster_digest=ROSTER_DIGEST,
        origin=ORIGIN,
        status=status,
        unit_count=0,
        admitted_count=admitted,
        attested_count=0,
        failed_count=0,
        child_outcome_reduction_version=2,
        required_child_expectations=(),
        settled_attempt_count=0,
        settled_attempt_digest=None,
        terminal_at=None if status is PlanStatus.PLANNED else INSTANT,
        created_at=INSTANT,
    )


def _head(internal_execution_id: str = EXECUTION_ID) -> Stage1LifecycleHeadTuple:
    identity = derive_plan_identity(PlanIdentityRequest(PLAN_DIGEST, internal_execution_id))
    return Stage1LifecycleHeadTuple(
        surface=Stage1Surface.PLAN,
        flow_id=identity.flow_id,
        current_execution_id=identity.execution_id,
        current_internal_execution_id=internal_execution_id,
        predecessor_execution_id=None,
        latest_event_id=str(uuid.UUID(int=1)),
        latest_authority_digest="0" * 64,
        latest_transition_sequence=1,
        latest_state=FlowState.RUNNING,
        parent_flow_id=None,
        causation_event_id=None,
    )


def test_pending_intent_on_a_bootstrap_owner_proves_outcome_one_with_the_golden_digest() -> None:
    proved = classify_source_delivery(
        SourceDeliveryObservation(INTENT_ID, _intent(IntentState.PENDING), _bootstrap_owner())
    )

    assert (proved.ordinal, proved.owner_revision) == (1, 0)
    assert proved.acknowledgment_digest == source_acknowledgment_digest(INTENT_ID, 1, 0)
    assert proved.acknowledgment_digest == digest(
        ["stage1-source-acknowledgment/v1", INTENT_ID, 1, 0]
    )
    assert proved.acknowledgment_digest == (
        # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
        "282e96205cb41aea0783ba79daa944a6baa20fb356379a02f812988b41f343f1"
    )


def test_claimed_intent_proves_outcome_two_only_with_the_owner_reservation_trio() -> None:
    reserved = replace(
        _bootstrap_owner(revision=1),
        reserved_intent_id=INTENT_ID,
        reserved_intent_digest=INTENT_ID,
        reserved_generation=0,
    )
    proved = classify_source_delivery(
        SourceDeliveryObservation(INTENT_ID, _intent(IntentState.CLAIMED, 0), reserved)
    )
    assert (proved.ordinal, proved.owner_revision) == (2, 1)

    with pytest.raises(InvariantError) as caught:
        classify_source_delivery(
            SourceDeliveryObservation(
                INTENT_ID, _intent(IntentState.CLAIMED, 0), _bootstrap_owner(revision=1)
            )
        )
    assert caught.value.code == SOURCE_UNACKNOWLEDGED


def test_admitted_intent_proves_outcome_three_with_owner_plan_and_head_agreement() -> None:
    owner = SuccessorOwner(
        revision=3,
        root_plan_key=PLAN_KEY,
        last_plan_key=PLAN_KEY,
        last_generation=0,
        active_plan_key=PLAN_KEY,
        active_intent_id=INTENT_ID,
        reserved_intent_id=None,
        reserved_intent_digest=None,
        reserved_generation=None,
        latest_internal_execution_id=EXECUTION_ID,
    )
    observation = SourceDeliveryObservation(
        INTENT_ID,
        _intent(IntentState.ADMITTED, 0),
        owner,
        active_plan=_plan(PlanStatus.PLANNED),
        active_head=_head(),
    )
    assert classify_source_delivery(observation).ordinal == 3

    disagreeing_head = replace(observation, active_head=_head("cd" * 32))
    with pytest.raises(InvariantError) as caught:
        classify_source_delivery(disagreeing_head)
    assert caught.value.code == SOURCE_UNACKNOWLEDGED
    assert caught.value.context["state"] == "admitted"


def test_admission_failed_transfer_proves_outcome_four_over_the_same_pending_intent() -> None:
    owner = SuccessorOwner(
        revision=4,
        root_plan_key=PLAN_KEY,
        last_plan_key=PLAN_KEY,
        last_generation=0,
        active_plan_key=None,
        active_intent_id=None,
        reserved_intent_id=None,
        reserved_intent_digest=None,
        reserved_generation=None,
        latest_internal_execution_id=EXECUTION_ID,
    )
    proved = classify_source_delivery(
        SourceDeliveryObservation(
            INTENT_ID,
            _intent(IntentState.PENDING),
            owner,
            last_plan=_plan(PlanStatus.ADMISSION_FAILED),
            last_head=_head(),
        )
    )
    assert (proved.ordinal, proved.owner_revision) == (4, 4)
    assert proved.acknowledgment_digest == source_acknowledgment_digest(INTENT_ID, 4, 4)


@pytest.mark.parametrize(
    "observation",
    (
        SourceDeliveryObservation(INTENT_ID, None, _bootstrap_owner()),
        SourceDeliveryObservation(INTENT_ID, _intent(IntentState.PENDING), None),
        SourceDeliveryObservation("0" * 64, _intent(IntentState.PENDING), _bootstrap_owner()),
        SourceDeliveryObservation(INTENT_ID, _intent(IntentState.ADMITTED, 0), _bootstrap_owner()),
    ),
)
def test_missing_or_inconsistent_readback_leaves_the_source_unacknowledged(
    observation: SourceDeliveryObservation,
) -> None:
    """No SDK response or transaction return is acknowledgment authority; only the read is."""
    with pytest.raises(InvariantError) as caught:
        classify_source_delivery(observation)
    assert caught.value.code == SOURCE_UNACKNOWLEDGED
