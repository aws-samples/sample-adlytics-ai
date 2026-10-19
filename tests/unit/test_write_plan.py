# SPDX-License-Identifier: MIT-0
"""ADR-075 §3 plan-intent domain authority: identities, closed forms, and schedules."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from domain.operations.write_plan import (
    MAX_PARTITION_IDENTITY_BYTES,
    CanonicalObjectEntry,
    CanonicalUnitEntry,
    IntentAuthorityInputs,
    IntentMeta,
    IntentOrigin,
    IntentOriginKind,
    IntentState,
    PlanMeta,
    PlanStatus,
    PlanUnit,
    PlanUnitAdmission,
    PlanUnitStatus,
    RequiredChildExpectation,
    SuccessorOwner,
    WritePlanError,
    WriteUnitMessageRequest,
    bootstrap_owner,
    intent_authority_tuple,
    intent_identity,
    intent_partition_key,
    intent_roster_tuple,
    internal_execution_identity,
    owner_partition_key,
    plan_attempt_budget_origin,
    plan_digest_for_source_delivery,
    plan_intent_retry_schedule,
    plan_partition_key,
    ready_order_key,
    roster_digest,
    source_delivery_identity,
    unit_message,
    unit_record_key,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

# Every identity below is fictional fixture data; none is copied from a live system.

CHECKSUM = "0123456789abcdef" * 4
SYNTHETIC_PLAN_DIGEST = "2" * 64
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
INSTANT = "01764950400000000000"
LATER = "01764950500000000000"


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _vector_unit() -> CanonicalUnitEntry:
    return CanonicalUnitEntry(
        unit_key="db.campaigns",
        table_kind="current",
        merge_count=2,
        objects=(
            CanonicalObjectEntry(
                kind="manifest",
                reference="s3://authority/a",
                content_checksum=CHECKSUM,
                publication_id="pub-001",
                partition_key=None,
                fencing_token=17,
            ),
        ),
    )


def _vector_inputs() -> IntentAuthorityInputs:
    return IntentAuthorityInputs(
        root_plan_key=None,
        source_plan_key=None,
        plan_digest=SYNTHETIC_PLAN_DIGEST,
        origin=IntentOrigin(IntentOriginKind.SOURCE_DELIVERY, "delivery-001"),
        units=(_vector_unit(),),
    )


# --- Committed §3 constructor vectors -------------------------------------------------------------


def test_roster_tuple_reproduces_the_committed_bytes_and_digest() -> None:
    """The committed vector pins encoding, ordering, and digest behavior only."""
    tuple_ = intent_roster_tuple((_vector_unit(),))
    encoded = json.dumps(
        tuple_, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    assert len(encoded) == 182
    assert roster_digest((_vector_unit(),)) == (
        # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
        "e7294e98228c1dd05947ca0655c8b6015a98244d67e0c544873525e802448d66"
    )
    assert roster_digest((_vector_unit(),)) == _canonical_digest(tuple_)


def test_intent_authority_tuple_reproduces_the_committed_bytes_and_intent_id() -> None:
    tuple_ = intent_authority_tuple(_vector_inputs())
    encoded = json.dumps(
        tuple_, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    assert len(encoded) == 318
    assert intent_identity(_vector_inputs()) == (
        # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
        "8595e699935a8ec7ee6d3d87d3e0214300e09831da6a4808f10c9fab4174269b"
    )


def test_plan_attempt_budget_origin_reproduces_the_committed_vector() -> None:
    payload = json.dumps(
        ["stage1-plan-attempt-budget/v1", "WRITE_PLAN#2222#exec-g7"],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert len(payload) == 59
    assert plan_attempt_budget_origin("WRITE_PLAN#2222#exec-g7") == (
        # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
        "4a8f41b7fc1bd98dfb2e082b3ef5f8fee64af9b166c9c46589e090b736f1e378"
    )


def test_source_delivery_identity_and_plan_digest_share_one_constructor_chain() -> None:
    identity = source_delivery_identity((("manifests/a.json", "v-1"), ("manifests/b.json", "v-2")))
    assert identity == _canonical_digest(
        ["stage1-source-delivery/v1", [["manifests/a.json", "v-1"], ["manifests/b.json", "v-2"]]]
    )
    assert plan_digest_for_source_delivery(identity) == _canonical_digest(
        ["stage1-plan-digest/v1", identity]
    )


def test_internal_execution_identity_binds_intent_id_and_claimed_generation() -> None:
    intent_id = intent_identity(_vector_inputs())
    assert internal_execution_identity(intent_id, 7) == _canonical_digest(
        ["stage1-plan-materialization/v1", intent_id, 7]
    )
    assert internal_execution_identity(intent_id, 7) != internal_execution_identity(intent_id, 8)
    with pytest.raises(WritePlanError):
        internal_execution_identity(intent_id, -1)


# --- Semantic changes yield distinct identities; noncanonical bytes reject ------------------------


def test_every_semantic_roster_change_yields_a_distinct_intent() -> None:
    base = intent_identity(_vector_inputs())
    merge_changed = replace(_vector_unit(), merge_count=3)
    assert intent_identity(replace(_vector_inputs(), units=(merge_changed,))) != base
    regrouped = replace(_vector_unit(), unit_key="db.attribution")
    assert intent_identity(replace(_vector_inputs(), units=(regrouped,))) != base
    origin_changed = replace(
        _vector_inputs(), origin=IntentOrigin(IntentOriginKind.SOURCE_DELIVERY, "delivery-002")
    )
    assert intent_identity(origin_changed) != base
    rooted = replace(_vector_inputs(), root_plan_key="WRITE_PLAN#2222#exec-g7")
    assert intent_identity(rooted) != base


def test_noncanonical_roster_ordering_is_rejected_rather_than_hashed() -> None:
    alpha = replace(_vector_unit(), unit_key="db.alpha")
    beta = replace(_vector_unit(), unit_key="db.beta")
    intent_roster_tuple((alpha, beta))
    with pytest.raises(WritePlanError):
        intent_roster_tuple((beta, alpha))
    with pytest.raises(WritePlanError):
        intent_roster_tuple((alpha, alpha))


def test_noncanonical_object_ordering_or_repeated_object_key_rejects() -> None:
    first = CanonicalObjectEntry("manifest", "s3://authority/a", CHECKSUM, "pub-001", None, 1)
    second = CanonicalObjectEntry("manifest", "s3://authority/b", CHECKSUM, "pub-002", None, 2)
    CanonicalUnitEntry("db.campaigns", "current", 0, (first, second))
    with pytest.raises(WritePlanError):
        CanonicalUnitEntry("db.campaigns", "current", 0, (second, first))
    with pytest.raises(WritePlanError):
        CanonicalUnitEntry("db.campaigns", "current", 0, (first, first))


def test_a_roster_carries_at_most_fifteen_units() -> None:
    units = tuple(replace(_vector_unit(), unit_key=f"db.table_{index:02d}") for index in range(16))
    intent_roster_tuple(units[:15])
    with pytest.raises(WritePlanError):
        intent_roster_tuple(units)


def test_identity_members_are_capped_at_128_encoded_bytes() -> None:
    with pytest.raises(WritePlanError):
        replace(_vector_unit(), unit_key="u" * 129)
    with pytest.raises(WritePlanError):
        CanonicalObjectEntry("manifest", "r" * 129, CHECKSUM, "pub-001", None, 1)


# A fictional PartitionIdentity.identity in the Publication-State shape: scope members,
# grain, and the bucket window, joined by "|". Realistic identities run ~150 UTF-8 bytes,
# past the 128-byte internal cap, so the roster member carries its own §11 bound.
REALISTIC_PARTITION_IDENTITY = "|".join(
    (
        "fct_retail_purchase_amc",
        "scope-0001",
        "pop-0001",
        "reporting",
        "daily_additive",
        "gregorian",
        "1",
        "America/Los_Angeles",
        "source_business_date",
        "day",
        "2026-09-19",
        "2026-09-19",
    )
)


def test_partition_identity_member_is_capped_at_256_not_128_encoded_bytes() -> None:
    # Grain: UTF-8 bytes of the roster partition_key-or-null member.
    assert MAX_PARTITION_IDENTITY_BYTES == 256
    assert len(REALISTIC_PARTITION_IDENTITY.encode("utf-8")) == 147
    accepted = CanonicalObjectEntry(
        "manifest", "s3://authority/a", CHECKSUM, "pub-001", REALISTIC_PARTITION_IDENTITY, 1
    )
    assert accepted.partition_key == REALISTIC_PARTITION_IDENTITY
    CanonicalObjectEntry("manifest", "s3://authority/a", CHECKSUM, "pub-001", "p" * 256, 1)
    with pytest.raises(WritePlanError, match="256-byte partition identity cap"):
        CanonicalObjectEntry("manifest", "s3://authority/a", CHECKSUM, "pub-001", "p" * 257, 1)
    with pytest.raises(WritePlanError, match="nonblank"):
        CanonicalObjectEntry("manifest", "s3://authority/a", CHECKSUM, "pub-001", "  ", 1)


def test_manifest_pairs_must_be_canonically_ordered_and_distinct() -> None:
    with pytest.raises(WritePlanError):
        source_delivery_identity((("b", "v-1"), ("a", "v-1")))
    with pytest.raises(WritePlanError):
        source_delivery_identity((("a", "v-1"), ("a", "v-1")))
    with pytest.raises(WritePlanError):
        source_delivery_identity(())


# --- Canonical keys and the ready GSI pair --------------------------------------------------------


def test_canonical_keys_reproduce_the_pinned_forms() -> None:
    assert owner_partition_key(DIGEST_A) == f"WRITE_PLAN#{DIGEST_A}"
    assert intent_partition_key(DIGEST_A, DIGEST_B) == f"WRITE_PLAN#{DIGEST_A}#INTENT#{DIGEST_B}"
    assert plan_partition_key(DIGEST_A, DIGEST_B) == f"WRITE_PLAN#{DIGEST_A}#{DIGEST_B}"
    assert unit_record_key("db.campaigns") == "UNIT#db.campaigns"
    assert ready_order_key(INSTANT, DIGEST_B) == f"{INSTANT}#{DIGEST_B}"


def test_key_constructors_reject_malformed_production_members() -> None:
    with pytest.raises(WritePlanError):
        owner_partition_key("not-a-digest")
    with pytest.raises(WritePlanError):
        ready_order_key("123", DIGEST_B)
    with pytest.raises(WritePlanError):
        unit_record_key("   ")


# --- Retry schedule -------------------------------------------------------------------------------


def test_retry_delay_is_exactly_the_capped_backoff_formula() -> None:
    """Delay in seconds from the pre-increment count: min(60 * 2^min(c, 4), 900)."""
    expected_seconds = {0: 60, 1: 120, 2: 240, 3: 480, 4: 900, 9: 900}
    for count, seconds in expected_seconds.items():
        schedule = plan_intent_retry_schedule(count, INSTANT)
        assert schedule.retry_count == count + 1
        assert int(schedule.due_at) - int(INSTANT) == seconds * 1_000_000
        assert len(schedule.due_at) == 20


def test_retry_schedule_refuses_overflow_and_negative_counts() -> None:
    with pytest.raises(WritePlanError):
        plan_intent_retry_schedule(0, "9" * 20)
    with pytest.raises(WritePlanError):
        plan_intent_retry_schedule(-1, INSTANT)


# --- Closed SUCCESSOR_OWNER form ------------------------------------------------------------------


def _owner_with_plan() -> SuccessorOwner:
    plan_key = plan_partition_key(DIGEST_A, DIGEST_B)
    return SuccessorOwner(
        revision=4,
        root_plan_key=plan_key,
        last_plan_key=plan_key,
        last_generation=1,
        active_plan_key=None,
        active_intent_id=None,
        reserved_intent_id=None,
        reserved_intent_digest=None,
        reserved_generation=None,
        latest_internal_execution_id=DIGEST_B,
    )


def test_bootstrap_owner_is_revision_zero_with_generation_minus_one() -> None:
    owner = bootstrap_owner()
    assert (owner.revision, owner.last_generation) == (0, -1)
    assert owner.root_plan_key is None and owner.last_plan_key is None


def test_owner_reservation_trio_is_all_or_none_with_byte_equal_digest() -> None:
    owner = _owner_with_plan()
    replace(
        owner,
        reserved_intent_id=DIGEST_B,
        reserved_intent_digest=DIGEST_B,
        reserved_generation=2,
    )
    with pytest.raises(WritePlanError):
        replace(owner, reserved_intent_id=DIGEST_B)
    with pytest.raises(WritePlanError):
        replace(
            owner,
            reserved_intent_id=DIGEST_B,
            reserved_intent_digest=DIGEST_A,
            reserved_generation=2,
        )
    with pytest.raises(WritePlanError):  # reserved_generation must be last_generation + 1
        replace(
            owner,
            reserved_intent_id=DIGEST_B,
            reserved_intent_digest=DIGEST_B,
            reserved_generation=5,
        )


def test_owner_root_last_active_field_pairings_are_closed() -> None:
    owner = _owner_with_plan()
    with pytest.raises(WritePlanError):
        replace(owner, last_generation=-1)
    with pytest.raises(WritePlanError):
        replace(owner, root_plan_key=None)
    with pytest.raises(WritePlanError):
        replace(owner, active_plan_key=owner.last_plan_key)  # active pair must be both present
    with pytest.raises(WritePlanError):
        replace(owner, latest_internal_execution_id=None)
    replace(owner, active_plan_key=owner.last_plan_key, active_intent_id=DIGEST_B)


# --- Closed intent META form ----------------------------------------------------------------------


def _pending_meta() -> IntentMeta:
    return IntentMeta(
        root_plan_key=None,
        source_plan_key=None,
        plan_digest=DIGEST_A,
        intent_id=DIGEST_B,
        roster_digest=CHECKSUM,
        origin=IntentOrigin(IntentOriginKind.SOURCE_DELIVERY, DIGEST_A),
        state=IntentState.PENDING,
        claimed_generation=None,
        retry_count=0,
        due_at=INSTANT,
        ready_status="ready",
        ready_order=f"{INSTANT}#{DIGEST_B}",
        created_at=INSTANT,
        last_transition_at=INSTANT,
    )


def test_only_a_pending_intent_carries_the_exact_ready_field_pair() -> None:
    meta = _pending_meta()
    with pytest.raises(WritePlanError):
        replace(meta, ready_status=None)
    with pytest.raises(WritePlanError):
        replace(meta, ready_order=f"{LATER}#{DIGEST_B}")  # embeds a due it does not have
    with pytest.raises(WritePlanError):
        replace(meta, claimed_generation=3)
    claimed = replace(
        meta,
        state=IntentState.CLAIMED,
        claimed_generation=2,
        ready_status=None,
        ready_order=None,
    )
    with pytest.raises(WritePlanError):
        replace(claimed, ready_status="ready", ready_order=f"{INSTANT}#{DIGEST_B}")
    with pytest.raises(WritePlanError):
        replace(claimed, claimed_generation=None)
    replace(claimed, state=IntentState.ADMITTED)  # admitted retains the generation


# --- Closed plan META and plan UNIT# forms --------------------------------------------------------

UNIT_FLOW = "960cfeca-0bad-57fd-b4b0-383e15d59085"
OTHER_FLOW = "e4de2f19-58db-5f51-b924-b4e5118d4db3"


def _plan_meta() -> PlanMeta:
    return PlanMeta(
        plan_digest=DIGEST_A,
        internal_execution_id=DIGEST_B,
        root_plan_key=plan_partition_key(DIGEST_A, DIGEST_B),
        generation=2,
        source_plan_key=None,
        origin_intent_id=DIGEST_B,
        roster_digest=CHECKSUM,
        origin=IntentOrigin(IntentOriginKind.SOURCE_DELIVERY, DIGEST_A),
        status=PlanStatus.PLANNED,
        unit_count=1,
        admitted_count=0,
        attested_count=0,
        failed_count=0,
        child_outcome_reduction_version=2,
        required_child_expectations=(RequiredChildExpectation(UNIT_FLOW, 2),),
        settled_attempt_count=0,
        settled_attempt_digest=None,
        terminal_at=None,
        created_at=INSTANT,
    )


def test_plan_meta_expectations_are_generation_bound_sorted_and_complete() -> None:
    meta = _plan_meta()
    with pytest.raises(WritePlanError):
        replace(meta, required_child_expectations=())
    with pytest.raises(WritePlanError):
        replace(meta, required_child_expectations=(RequiredChildExpectation(UNIT_FLOW, 3),))
    with pytest.raises(WritePlanError):
        replace(meta, child_outcome_reduction_version=1)
    with pytest.raises(WritePlanError):  # unsorted flow ids
        replace(
            meta,
            unit_count=2,
            required_child_expectations=(
                RequiredChildExpectation(OTHER_FLOW, 2),
                RequiredChildExpectation(UNIT_FLOW, 2),
            ),
        )


def test_plan_meta_terminality_and_settled_aggregate_pairings_are_closed() -> None:
    meta = _plan_meta()
    with pytest.raises(WritePlanError):
        replace(meta, terminal_at=INSTANT)  # planned plans carry no terminal time
    with pytest.raises(WritePlanError):
        replace(meta, status=PlanStatus.COMPLETED)  # terminal plans carry one
    with pytest.raises(WritePlanError):
        replace(meta, settled_attempt_count=1)  # nonzero count carries its digest
    with pytest.raises(WritePlanError):
        replace(meta, admitted_count=2)  # counters stay within unit_count
    replace(meta, status=PlanStatus.ADMISSION_FAILED, terminal_at=INSTANT)


def test_plan_unit_admission_audit_exists_exactly_when_admitted() -> None:
    unit = PlanUnit(
        unit=_vector_unit(),
        flow_id=UNIT_FLOW,
        owner_generation=2,
        status=PlanUnitStatus.QUEUED,
        admission=PlanUnitAdmission.PENDING,
        admission_deduplication_digest=None,
        admitted_at=None,
        created_at=INSTANT,
    )
    with pytest.raises(WritePlanError):
        replace(unit, admission=PlanUnitAdmission.ADMITTED)
    with pytest.raises(WritePlanError):
        replace(unit, admission_deduplication_digest=CHECKSUM)
    replace(
        unit,
        admission=PlanUnitAdmission.ADMITTED,
        admission_deduplication_digest=CHECKSUM,
        admitted_at=LATER,
    )


# --- §4 write-unit message constructor ------------------------------------------------------------


def test_unit_message_reproduces_the_committed_section_4_vector_byte_for_byte() -> None:
    """The reconciler's SQS body, deduplication id, and group id share one constructor.

    Write discipline §4 pins these bytes; a drift here would collapse or split FIFO
    deduplication across generations silently.
    """
    message = unit_message(
        WriteUnitMessageRequest(
            plan_key="WRITE_PLAN#2222#exec-g7",
            plan_digest="2" * 64,
            origin_intent_id="8595e699935a8ec7ee6d3d87d3e0214300e09831da6a4808f10c9fab4174269b",
            generation=7,
            unit_key="db.campaigns",
        )
    )
    assert message.body == (
        '{"generation":7,"origin_intent_id":'
        # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
        '"8595e699935a8ec7ee6d3d87d3e0214300e09831da6a4808f10c9fab4174269b",'
        '"plan_digest":"2222222222222222222222222222222222222222222222222222222222222222",'
        '"plan_key":"WRITE_PLAN#2222#exec-g7","schema_version":"1","unit_key":"db.campaigns"}'
    )
    assert len(message.body.encode("utf-8")) == 267
    assert hashlib.sha256(message.body.encode("utf-8")).hexdigest() == (
        # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
        "a6555c645b6a896c38c098f4197a47747b291ebb7064362a1d426fcc632e0454"
    )
    assert message.deduplication_id == (
        # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
        "7708aa64f547dd52815c30960bd182fb1896bbf2849ca5ad3a24c8a76b513ea6"
    )
    assert message.group_id == "db.campaigns"
