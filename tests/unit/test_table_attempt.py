# SPDX-License-Identifier: MIT-0
"""ADR-075 §3.1 table-attempt authority: closed forms, predicates, and the direct record write."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words

from adapters.outbound.dynamodb.table_attempt import DynamoTableAttemptStore
from domain.operations.table_attempt import (
    ActiveFence,
    AttemptCore,
    AttemptIdentityRequest,
    AttemptObservation,
    AttemptOwnerKind,
    BindingDigestRequest,
    CancelRequestedAttempt,
    CoreValidityRequest,
    FenceObservation,
    InlineRun,
    InlineRunOwnerObservation,
    InlineRunState,
    IntentAttempt,
    ParkedAttempt,
    PlanUnitOwnerObservation,
    ProtectedCaseState,
    ProtectedReconcileCase,
    ProviderErrorClass,
    RecomputedAttemptIdentity,
    ReconcileBlockReason,
    ReconciliationObservation,
    RunIdentityRequest,
    SanitizedStartEvidence,
    SubmittedAttempt,
    TableAttemptError,
    TerminalAttempt,
    WriterKind,
    attempt_identity,
    attempt_reservation_identity,
    binding_digest,
    blocked_reconcile_key,
    core_invalid,
    core_valid,
    parse_attempt,
    parse_sanitized_evidence,
    pending_reconcile_key,
    recompute_attempt_identity,
    reconcile_backoff_delay_seconds,
    reconciliation_valid,
    repairable_reconciliation_metadata,
    rescheduled_reconcile_due_micros,
    run_identity,
)
from domain.shared_kernel.errors import InvariantError
from ports.outbound.table_attempt import (
    RecordSubmittedAttemptRequest,
    RecordSubmittedAttemptResult,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

# Every identity below is fictional fixture data; none is copied from a live system.
TABLE = "adlytics_dev.fact_semantic_rows"
ATTEMPT_ID = "attempt-fixture-0001"
HOLDER = "holder-fixture-0001"
TOKEN = 7
ORDINAL = 3
EXECUTION = "exec-fixture-0001"
STATEMENT_KIND = "merge"
ENVELOPE = '{"WorkGroup":"fixture-workgroup"}'
ENVELOPE_DIGEST = "e" * 64
RESERVATION = "reservation-slot-0003"
PLAN_KEY = "WRITE_PLAN#fixture-plan-0001"
PARTITION = f"TABLE_ATTEMPT#{TABLE}"
INSTANT = f"{1_700_000_000_000_000:020d}"
OTHER_INSTANT = f"{1_700_000_099_000_000:020d}"
DUE_MICROS = 1_700_000_060_000_000
OPERATION_ID = "op-fixture-0001"
RECOVERY_REF = "3f2b8c9a-6d4e-4f1a-9b7c-2a5d8e0f1c3b"
QUERY_ID = "query-exec-fixture-0001"

BINDING = binding_digest(
    BindingDigestRequest(
        physical_table_id=TABLE, attempt_id=ATTEMPT_ID, lease_holder=HOLDER, fencing_token=TOKEN
    )
)
RUN_ID = run_identity(
    RunIdentityRequest(
        writer_kind=WriterKind.TRANSITIONAL_DELIVERY,
        operation_id=OPERATION_ID,
        execution_id=EXECUTION,
        physical_table_id=TABLE,
    )
)


def _identity(core: AttemptCore) -> RecomputedAttemptIdentity:
    """Byte-equality stub hook so the predicate matrix stays independent of the constructor."""
    del core
    return RecomputedAttemptIdentity(
        attempt_id=ATTEMPT_ID, client_request_token=ATTEMPT_ID, envelope_digest=ENVELOPE_DIGEST
    )


def _pending(**overrides: object) -> ReconciliationObservation:
    base = ReconciliationObservation(
        reconcile_status="pending",
        reconcile_after_or_block=f"{DUE_MICROS:020d}#{BINDING}",
        reconcile_attempt_count=0,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _blocked(**overrides: object) -> ReconciliationObservation:
    base = ReconciliationObservation(
        reconcile_status="blocked",
        reconcile_after_or_block=f"BLOCK#{RECOVERY_REF}",
        reconcile_block_id="block-fixture-0001",
        reconcile_block_reason="MALFORMED_RECONCILIATION_METADATA",
        reconcile_blocked_at=INSTANT,
        reconcile_observation_digest="a" * 64,
        reconcile_recovery_ref=RECOVERY_REF,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _fence(**overrides: object) -> FenceObservation:
    base = FenceObservation(
        partition_key=PARTITION,
        record_key="FENCE",
        state="active",
        physical_table_id=TABLE,
        attempt_id=ATTEMPT_ID,
        lease_holder=HOLDER,
        fencing_token=TOKEN,
        parked_at=None,
        reconciliation=_pending(),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _attempt(**overrides: object) -> AttemptObservation:
    base = AttemptObservation(
        partition_key=PARTITION,
        record_key=f"ATTEMPT#{ATTEMPT_ID}",
        state="submitted",
        physical_table_id=TABLE,
        attempt_id=ATTEMPT_ID,
        plan_unit_owner_ref=PLAN_KEY,
        inline_run_owner_ref=None,
        ordinal=ORDINAL,
        statement_kind=STATEMENT_KIND,
        lease_holder=HOLDER,
        fencing_token=TOKEN,
        writer_execution_id=EXECUTION,
        start_envelope=ENVELOPE,
        client_request_token=ATTEMPT_ID,
        envelope_digest=ENVELOPE_DIGEST,
        reservation_identity=RESERVATION,
        query_execution_id=QUERY_ID,
        submitted_at=INSTANT,
        sanitized_start_evidence=SanitizedStartEvidence(200, ProviderErrorClass.NONE),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _plan_owner(**overrides: object) -> PlanUnitOwnerObservation:
    base = PlanUnitOwnerObservation(
        plan_partition_key=PLAN_KEY,
        unit_record_key=f"UNIT#{TABLE}",
        plan_meta_exists=True,
        origin_intent_admitted=True,
        unit_status="writing",
        writer_execution_id=EXECUTION,
        ordinal=ORDINAL,
        current_attempt_id=ATTEMPT_ID,
        lease_holder=HOLDER,
        fencing_token=TOKEN,
        unit_reservation_count=3,
        plan_reservation_count=5,
        reserved_slot_identity=RESERVATION,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _inline_owner() -> InlineRunOwnerObservation:
    return InlineRunOwnerObservation(
        run_partition_key=PARTITION,
        run_record_key=f"RUN#{RUN_ID}",
        run_id=RUN_ID,
        state="open",
        writer_execution_id=EXECUTION,
        ordinal=ORDINAL,
        current_attempt_id=ATTEMPT_ID,
        lease_holder=HOLDER,
        fencing_token=TOKEN,
        reservation_count=3,
        reserved_slot_identity=RESERVATION,
    )


def _request(**overrides: object) -> CoreValidityRequest:
    base = CoreValidityRequest(
        table_id=TABLE,
        fence=_fence(),
        attempt=_attempt(),
        recompute_identity=_identity,
        plan_unit_owner=_plan_owner(),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# --- Pinned identity constructors: golden bytes over fictional inputs -----------------------------


def test_binding_digest_matches_inline_sha256_of_canonical_json() -> None:
    payload = f'["{TABLE}","{ATTEMPT_ID}","{HOLDER}",{TOKEN}]'.encode()
    assert hashlib.sha256(payload).hexdigest() == BINDING


def test_run_identity_matches_inline_sha256_of_canonical_json() -> None:
    payload = f'["transitional_delivery","{OPERATION_ID}","{EXECUTION}","{TABLE}"]'.encode()
    assert hashlib.sha256(payload).hexdigest() == RUN_ID


def _identity_request(**overrides: object) -> AttemptIdentityRequest:
    base = AttemptIdentityRequest(
        owner_kind=AttemptOwnerKind.QUEUED_UNIT,
        owner_ref=PLAN_KEY,
        physical_table_id=TABLE,
        lease_holder=HOLDER,
        fencing_token=TOKEN,
        ordinal=ORDINAL,
        start_envelope=ENVELOPE,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_attempt_identity_matches_inline_sha256_of_the_pinned_canonical_tuple() -> None:
    """§5 step 2: the digest is over the exact seven-element tuple bytes, nothing else."""
    payload = (
        f'["stage1-table-attempt/v1","queued_unit","{PLAN_KEY}","{TABLE}","{HOLDER}",'
        f"{TOKEN},{ORDINAL}]"
    ).encode()
    derived = attempt_identity(_identity_request())
    assert derived.attempt_id == hashlib.sha256(payload).hexdigest()
    assert derived.client_request_token == derived.attempt_id
    assert derived.envelope_digest == hashlib.sha256(ENVELOPE.encode()).hexdigest()


def test_attempt_reservation_identity_matches_inline_sha256_of_the_pinned_pair() -> None:
    """§5 step 2: the permanent-slot digest is over the exact two-element pair bytes."""
    derived = attempt_reservation_identity(ATTEMPT_ID)
    payload = f'["stage1-attempt-reservation/v1","{ATTEMPT_ID}"]'.encode()
    assert derived == hashlib.sha256(payload).hexdigest()


def test_attempt_reservation_identity_rejects_a_blank_attempt_id() -> None:
    with pytest.raises(TableAttemptError):
        attempt_reservation_identity("  ")


def test_attempt_identity_rejects_non_positive_ordinal_and_fencing_token() -> None:
    with pytest.raises(TableAttemptError):
        _identity_request(ordinal=0)
    with pytest.raises(TableAttemptError):
        _identity_request(fencing_token=0)


def test_recompute_attempt_identity_selects_the_owner_kind_from_the_core_binding() -> None:
    """A plan-bound core recomputes as queued_unit and a run-bound core as inline_run."""
    queued = attempt_identity(_identity_request())
    inline = attempt_identity(
        _identity_request(owner_kind=AttemptOwnerKind.INLINE_RUN, owner_ref=RUN_ID)
    )
    assert queued.attempt_id != inline.attempt_id
    plan_bound = parse_attempt(
        _attempt(
            record_key=f"ATTEMPT#{queued.attempt_id}",
            attempt_id=queued.attempt_id,
            client_request_token=queued.client_request_token,
            envelope_digest=queued.envelope_digest,
        )
    ).core
    run_bound = parse_attempt(
        _attempt(
            record_key=f"ATTEMPT#{inline.attempt_id}",
            attempt_id=inline.attempt_id,
            plan_unit_owner_ref=None,
            inline_run_owner_ref=RUN_ID,
            client_request_token=inline.client_request_token,
            envelope_digest=inline.envelope_digest,
        )
    ).core
    assert recompute_attempt_identity(plan_bound) == queued
    assert recompute_attempt_identity(run_bound) == inline


def test_core_valid_when_stored_identities_come_from_the_pinned_constructor() -> None:
    derived = attempt_identity(_identity_request())
    request = _request(
        fence=_fence(attempt_id=derived.attempt_id),
        attempt=_attempt(
            record_key=f"ATTEMPT#{derived.attempt_id}",
            attempt_id=derived.attempt_id,
            client_request_token=derived.client_request_token,
            envelope_digest=derived.envelope_digest,
        ),
        plan_unit_owner=_plan_owner(current_attempt_id=derived.attempt_id),
        recompute_identity=recompute_attempt_identity,
    )
    assert core_valid(request)


def test_core_invalid_when_the_stored_digest_disagrees_with_the_pinned_recomputation() -> None:
    """A single tampered identity byte must fail closed under the production hook."""
    derived = attempt_identity(_identity_request())
    request = _request(
        fence=_fence(attempt_id=derived.attempt_id),
        attempt=_attempt(
            record_key=f"ATTEMPT#{derived.attempt_id}",
            attempt_id=derived.attempt_id,
            client_request_token=derived.client_request_token,
            envelope_digest="f" * 64,
        ),
        plan_unit_owner=_plan_owner(current_attempt_id=derived.attempt_id),
        recompute_identity=recompute_attempt_identity,
    )
    assert not core_valid(request)
    assert core_invalid(request)


def test_pending_reconcile_key_is_twenty_digit_epoch_micros_then_binding_digest() -> None:
    key = pending_reconcile_key(DUE_MICROS, BINDING)
    due_text, _, digest = key.partition("#")
    assert len(due_text) == 20
    assert int(due_text) == DUE_MICROS  # UTC epoch microseconds, zero-padded
    assert digest == BINDING


def test_blocked_reconcile_key_wraps_the_recovery_reference() -> None:
    assert blocked_reconcile_key(RECOVERY_REF) == f"BLOCK#{RECOVERY_REF}"


def test_inline_run_rejects_a_run_id_that_does_not_reproduce_from_its_tuple() -> None:
    with pytest.raises(TableAttemptError):
        _run_form(run_id="0" * 64)


def _run_form(**overrides: object) -> InlineRun:
    base = InlineRun(
        run_id=RUN_ID,
        writer_kind=WriterKind.TRANSITIONAL_DELIVERY,
        operation_id=OPERATION_ID,
        execution_id=EXECUTION,
        physical_table_id=TABLE,
        status_flow_binding="status-flow-fixture-0001",
        execution_binding="execution-binding-fixture-0001",
        table_kind="current",
        statement_intent_kinds=("merge", "verify"),
        pointer_finalization_payload="pointer-payload-fixture",
        created_at=INSTANT,
        state=InlineRunState.OPEN,
        current_attempt_id=None,
        next_ordinal=1,
        accepted_attempt_reservations=0,
        terminal_outcome=None,
        terminal_at=None,
        attempt_references=(),
        status_outbox_disposition=None,
        usage_outbox_disposition=None,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_inline_run_accepts_the_reproducible_immutable_tuple() -> None:
    assert _run_form().run_id == RUN_ID


def test_protected_case_requeue_audit_exists_exactly_when_requeued() -> None:
    case = ProtectedReconcileCase(
        recovery_ref=RECOVERY_REF,
        state=ProtectedCaseState.BLOCKED,
        physical_table_id=TABLE,
        attempt_id=ATTEMPT_ID,
        reconcile_block_id="block-fixture-0001",
        binding_digest=BINDING,
        reason=ReconcileBlockReason.MALFORMED_RECONCILIATION_METADATA,
        blocked_at=INSTANT,
        observation_digest="a" * 64,
        requeued_at=None,
    )
    assert case.state is ProtectedCaseState.BLOCKED
    with pytest.raises(TableAttemptError):
        replace(case, state=ProtectedCaseState.REQUEUED)


# --- core_valid / core_invalid matrix -------------------------------------------------------------


def test_core_valid_for_an_active_pending_pair_with_a_queued_owner() -> None:
    request = _request()
    assert core_valid(request)
    assert not core_invalid(request)
    fence = request.fence
    assert fence is not None
    assert reconciliation_valid(fence)
    assert not repairable_reconciliation_metadata(request)


def test_core_valid_for_a_parked_pair_with_byte_equal_park_instants() -> None:
    request = _request(
        fence=_fence(state="parked", parked_at=INSTANT),
        attempt=_attempt(
            state="parked",
            parked_at=INSTANT,
            query_execution_id=None,
            submitted_at=None,
            sanitized_start_evidence=None,
        ),
    )
    assert core_valid(request)


def test_core_valid_for_an_inline_run_owner_binding() -> None:
    request = _request(
        attempt=_attempt(plan_unit_owner_ref=None, inline_run_owner_ref=RUN_ID),
        plan_unit_owner=None,
        inline_run_owner=_inline_owner(),
    )
    assert core_valid(request)


_VIOLATIONS: list[tuple[str, CoreValidityRequest]] = [
    ("missing fence", _request(fence=None)),
    ("missing attempt", _request(attempt=None)),
    ("unresolved projected base keys", _request(projected_base_keys_resolve=False)),
    ("bad fence sort key", _request(fence=_fence(record_key="FENCE#0"))),
    ("bad attempt sort key", _request(attempt=_attempt(record_key="ATTEMPT"))),
    (
        "fence/attempt state disagreement",
        _request(attempt=_attempt(state="parked", parked_at=INSTANT)),
    ),
    ("missing core field", _request(attempt=_attempt(statement_kind=None))),
    (
        "retyped core field",
        _request(attempt=_attempt(malformed_field_names=frozenset({"ordinal"}))),
    ),
    (
        "extra attempt field",
        _request(attempt=_attempt(unrecognized_field_names=frozenset({"free_text"}))),
    ),
    ("stored envelope digest disagreement", _request(attempt=_attempt(envelope_digest="f" * 64))),
    (
        "stored client token disagreement",
        # A-TEST-LITERALS-001: a fictional client token; see docs/security-dispositions.md
        _request(attempt=_attempt(client_request_token="token-other")),  # nosec B106
    ),
    ("pair binding mismatch", _request(fence=_fence(lease_holder="holder-other"))),
    (
        "park instant mismatch",
        _request(
            fence=_fence(state="parked", parked_at=INSTANT),
            attempt=_attempt(
                state="parked",
                parked_at=OTHER_INSTANT,
                query_execution_id=None,
                submitted_at=None,
                sanitized_start_evidence=None,
            ),
        ),
    ),
    ("zero owner bindings", _request(plan_unit_owner=None)),
    ("two owner bindings", _request(inline_run_owner=_inline_owner())),
    (
        "unreserved attempt",
        _request(plan_unit_owner=_plan_owner(reserved_slot_identity="reservation-other")),
    ),
    (
        "unit reservation count below range",
        _request(plan_unit_owner=_plan_owner(unit_reservation_count=0)),
    ),
    (
        "unit reservation count above range",
        _request(plan_unit_owner=_plan_owner(unit_reservation_count=11)),
    ),
    (
        "plan reservation count above range",
        _request(plan_unit_owner=_plan_owner(plan_reservation_count=33)),
    ),
    (
        "terminal attempt is outside the nonterminal predicate",
        _request(
            attempt=_attempt(state="SUCCEEDED", query_count=1, scanned_bytes=1024, engine_ms=250)
        ),
    ),
]


@pytest.mark.parametrize(
    ("label", "request_"), _VIOLATIONS, ids=[label for label, _ in _VIOLATIONS]
)
def test_core_invalid_is_the_closed_complement_for_each_violated_clause(
    label: str, request_: CoreValidityRequest
) -> None:
    del label
    assert not core_valid(request_)
    assert core_invalid(request_)


# --- reconciliation_valid and the repairable set --------------------------------------------------


def test_reconciliation_valid_accepts_pending_with_a_complete_requeue_audit() -> None:
    fence = _fence(
        reconciliation=_pending(requeued_from_case_ref=RECOVERY_REF, requeued_at=INSTANT)
    )
    assert reconciliation_valid(fence)


def test_reconciliation_valid_accepts_the_canonical_blocked_form() -> None:
    assert reconciliation_valid(_fence(reconciliation=_blocked()))


@pytest.mark.parametrize(
    "reconciliation",
    [
        _pending(reconcile_after_or_block=f"{DUE_MICROS:020d}#{'0' * 64}"),
        _pending(reconcile_attempt_count="0"),
        _pending(reconcile_attempt_count=-1),
        _pending(reconcile_attempt_count=None),
        _pending(requeued_from_case_ref=RECOVERY_REF),
        _pending(malformed_field_names=frozenset({"reconcile_attempt_count"})),
        _blocked(reconcile_observation_digest=None),
        _blocked(reconcile_block_reason="OTHER_REASON"),
        _blocked(reconcile_after_or_block="BLOCK#other"),
        ReconciliationObservation(),
    ],
    ids=[
        "pending digest is not the pair binding digest",
        "pending count retyped to text",
        "pending count negative",
        "pending count missing",
        "incomplete requeue audit",
        "malformed pending field type",
        "blocked without observation digest",
        "blocked reason outside the two-value vocabulary",
        "blocked key disagrees with the recovery reference",
        "no reconciliation attributes at all",
    ],
)
def test_reconciliation_valid_rejects_each_broken_form(
    reconciliation: ReconciliationObservation,
) -> None:
    assert not reconciliation_valid(_fence(reconciliation=reconciliation))


def test_repairable_when_only_reconciliation_metadata_is_malformed() -> None:
    request = _request(fence=_fence(reconciliation=_pending(reconcile_attempt_count="0")))
    assert core_valid(request)
    assert repairable_reconciliation_metadata(request)


def test_not_repairable_when_the_candidate_did_not_come_from_the_pending_gsi() -> None:
    request = _request(
        fence=_fence(reconciliation=_pending(reconcile_attempt_count="0")),
        came_from_pending_gsi=False,
    )
    assert not repairable_reconciliation_metadata(request)


def test_not_repairable_when_the_core_itself_is_invalid() -> None:
    request = _request(
        fence=_fence(
            lease_holder="holder-other",
            reconciliation=_pending(reconcile_attempt_count="0"),
        )
    )
    assert not repairable_reconciliation_metadata(request)


def test_not_repairable_when_reconciliation_is_already_canonical() -> None:
    assert not repairable_reconciliation_metadata(_request())


# --- Attempt state forms and the evidence presence matrix -----------------------------------------


def test_intent_forbids_evidence_and_every_later_field_family() -> None:
    clean = _attempt(
        state="intent", query_execution_id=None, submitted_at=None, sanitized_start_evidence=None
    )
    assert isinstance(parse_attempt(clean), IntentAttempt)
    with pytest.raises(TableAttemptError):
        parse_attempt(_attempt(state="intent", query_execution_id=None, submitted_at=None))


def test_submitted_requires_the_query_submission_evidence_triple_together() -> None:
    assert isinstance(parse_attempt(_attempt()), SubmittedAttempt)
    with pytest.raises(TableAttemptError):
        parse_attempt(_attempt(sanitized_start_evidence=None))
    with pytest.raises(TableAttemptError):
        parse_attempt(_attempt(query_execution_id=" "))


def test_cancel_requested_requires_the_triple_plus_the_cancellation_instant() -> None:
    parsed = parse_attempt(_attempt(state="cancel_requested", cancel_requested_at=INSTANT))
    assert isinstance(parsed, CancelRequestedAttempt)
    with pytest.raises(TableAttemptError):
        parse_attempt(_attempt(state="cancel_requested"))


def test_parked_permits_only_the_absent_pair_or_the_complete_triple() -> None:
    absent = _attempt(
        state="parked",
        parked_at=INSTANT,
        query_execution_id=None,
        submitted_at=None,
        sanitized_start_evidence=None,
    )
    assert isinstance(parse_attempt(absent), ParkedAttempt)
    triple = _attempt(state="parked", parked_at=INSTANT)
    assert isinstance(parse_attempt(triple), ParkedAttempt)
    with pytest.raises(TableAttemptError):
        parse_attempt(replace(absent, query_execution_id=QUERY_ID))  # query without evidence
    with pytest.raises(TableAttemptError):
        parse_attempt(
            replace(
                absent,
                sanitized_start_evidence=SanitizedStartEvidence(None, ProviderErrorClass.NONE),
            )
        )
    with pytest.raises(TableAttemptError):
        parse_attempt(replace(absent, cancel_requested_at=INSTANT))  # cancel needs the triple


def test_terminal_quantities_are_each_exactly_known_or_missing() -> None:
    known = parse_attempt(
        _attempt(state="SUCCEEDED", query_count=1, scanned_bytes=1024, engine_ms=250)
    )
    assert isinstance(known, TerminalAttempt)
    # Grain: one attempt; units are statements, bytes scanned, and engine milliseconds.
    assert (known.query_count, known.scanned_bytes, known.engine_ms) == (1, 1024, 250)
    missing = parse_attempt(_attempt(state="CANCELLED"))
    assert isinstance(missing, TerminalAttempt)
    assert (missing.query_count, missing.scanned_bytes, missing.engine_ms) == (None, None, None)
    with pytest.raises(TableAttemptError):
        parse_attempt(_attempt(state="FAILED", scanned_bytes=-1))


def test_terminal_accepts_the_completion_written_instant_and_usage_state() -> None:
    parsed = parse_attempt(
        _attempt(state="SUCCEEDED", terminal_at=INSTANT, athena_usage_state="unsettled")
    )
    assert isinstance(parsed, TerminalAttempt)
    assert (parsed.terminal_at, parsed.athena_usage_state) == (INSTANT, "unsettled")
    with pytest.raises(TableAttemptError, match="unsettled or settled"):
        parse_attempt(_attempt(state="SUCCEEDED", athena_usage_state="pending"))


def test_nonterminal_states_forbid_the_terminal_instant_and_usage_state() -> None:
    with pytest.raises(TableAttemptError, match="forbidden"):
        parse_attempt(_attempt(terminal_at=INSTANT))
    with pytest.raises(TableAttemptError, match="forbidden"):
        parse_attempt(_attempt(athena_usage_state="unsettled"))


def test_settled_terminal_carries_exactly_the_three_settlement_attributes() -> None:
    """The first live pre-switch verification read every settled attempt back as malformed.

    §3.1: a settled attempt also stores the immutable owner disposition, the concept-derived usage
    category, and the settlement authority digest; before this, the closed form treated those three
    owner-written attributes as extra core fields and refused every settled row.
    """
    settled = parse_attempt(
        _attempt(
            state="SUCCEEDED",
            terminal_at=INSTANT,
            athena_usage_state="settled",
            owner_disposition="successful",
            usage_category="projection_merge",
            usage_authority_digest="a" * 64,
        )
    )
    assert isinstance(settled, TerminalAttempt)
    assert (settled.owner_disposition, settled.usage_category) == ("successful", "projection_merge")
    with pytest.raises(TableAttemptError, match="exactly when usage is settled"):
        parse_attempt(_attempt(state="SUCCEEDED", athena_usage_state="settled"))
    with pytest.raises(TableAttemptError, match="exactly when usage is settled"):
        parse_attempt(
            _attempt(state="SUCCEEDED", athena_usage_state="unsettled", owner_disposition="x")
        )
    with pytest.raises(TableAttemptError, match="forbidden"):
        parse_attempt(_attempt(usage_category="projection_merge"))


def test_reconcile_backoff_is_exactly_the_pinned_five_step_schedule() -> None:
    # Grain: whole seconds of delay per old reconcile count c.
    assert [reconcile_backoff_delay_seconds(count) for count in (0, 1, 2, 3, 4, 9)] == [
        60,
        120,
        240,
        480,
        900,
        900,
    ]
    with pytest.raises(TableAttemptError, match="nonnegative"):
        reconcile_backoff_delay_seconds(-1)


def test_rescheduled_due_is_the_instant_plus_the_exact_backoff_in_microseconds() -> None:
    # Grain: UTC epoch microseconds; 60 seconds is exactly 60_000_000 microseconds.
    assert rescheduled_reconcile_due_micros(INSTANT, 0) == int(INSTANT) + 60_000_000
    assert rescheduled_reconcile_due_micros(INSTANT, 4) == int(INSTANT) + 900_000_000


def test_sanitized_evidence_is_exactly_the_closed_two_field_form() -> None:
    parsed = parse_sanitized_evidence(
        {"provider_status_code": None, "provider_error_class": "throttling"}
    )
    assert parsed == SanitizedStartEvidence(None, ProviderErrorClass.THROTTLING)
    with pytest.raises(TableAttemptError):
        parse_sanitized_evidence(
            {"provider_status_code": 200, "provider_error_class": "none", "message": "free text"}
        )
    with pytest.raises(TableAttemptError):
        parse_sanitized_evidence({"provider_status_code": 200, "provider_error_class": "odd"})


# --- RecordSubmittedAttemptRequest against the recording fake -------------------------------------

RECORD_CONDITION = (
    "#digest = :digest AND #token = :token AND "
    "((#state = :intent AND attribute_not_exists(#query)) OR "
    "(#state = :submitted_state AND #query = :query AND "
    "#submitted_at = :submitted_at AND #evidence = :evidence))"
)


def _conditional_error(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "fixture"}}, operation
    )


@guard_reserved_words
@dataclass
class _RecordingDynamo:
    """Evaluates exactly the conditional expression the store generates."""

    items: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict[tuple[str, str], dict[str, Any]]
    )
    gets: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    updates: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    @staticmethod
    def _key(request: dict[str, Any]) -> tuple[str, str]:
        key = cast("dict[str, dict[str, str]]", request["Key"])
        return key["partition_key"]["S"], key["record_key"]["S"]

    def get_item(self, **request: Any) -> dict[str, object]:
        self.gets.append(request)
        item = self.items.get(self._key(request))
        return {} if item is None else {"Item": item}

    def update_item(self, **request: Any) -> dict[str, object]:
        self.updates.append(request)
        if request["ConditionExpression"] != RECORD_CONDITION:
            raise AssertionError("the store changed its pinned condition bytes")
        key = self._key(request)
        item = self.items.get(key)
        names = cast("dict[str, str]", request["ExpressionAttributeNames"])
        values = cast("dict[str, Any]", request["ExpressionAttributeValues"])

        def same(name: str, value: str) -> bool:
            return item is not None and item.get(names[name]) == values[value]

        fresh = (
            item is not None
            and item.get("state") == values[":intent"]
            and names["#query"] not in item
        )
        replay = (
            same("#state", ":submitted_state")
            and same("#query", ":query")
            and same("#submitted_at", ":submitted_at")
            and same("#evidence", ":evidence")
        )
        if not (same("#digest", ":digest") and same("#token", ":token") and (fresh or replay)):
            raise _conditional_error("UpdateItem")
        assert item is not None
        old = dict(item)
        stored = dict(item)
        for assignment in cast("str", request["UpdateExpression"]).removeprefix("SET ").split(", "):
            name, _, value = assignment.partition(" = ")
            stored[names[name.strip()]] = values[value.strip()]
        self.items[key] = stored
        return {"Attributes": old}


def _intent_item() -> dict[str, Any]:
    return {
        "partition_key": {"S": PARTITION},
        "record_key": {"S": f"ATTEMPT#{ATTEMPT_ID}"},
        "state": {"S": "intent"},
        "physical_table_id": {"S": TABLE},
        "attempt_id": {"S": ATTEMPT_ID},
        "plan_unit_owner_ref": {"S": PLAN_KEY},
        "ordinal": {"N": str(ORDINAL)},
        "statement_kind": {"S": STATEMENT_KIND},
        "lease_holder": {"S": HOLDER},
        "fencing_token": {"N": str(TOKEN)},
        "writer_execution_id": {"S": EXECUTION},
        "start_envelope": {"S": ENVELOPE},
        "client_request_token": {"S": ATTEMPT_ID},
        "envelope_digest": {"S": ENVELOPE_DIGEST},
        "reservation_identity": {"S": RESERVATION},
    }


_BASE_RECORD = RecordSubmittedAttemptRequest(
    physical_table_id=TABLE,
    attempt_id=ATTEMPT_ID,
    envelope_digest=ENVELOPE_DIGEST,
    client_request_token=ATTEMPT_ID,
    query_execution_id=QUERY_ID,
    submitted_at=INSTANT,
    sanitized_start_evidence=SanitizedStartEvidence(200, ProviderErrorClass.NONE),
)


def _store(dynamo: _RecordingDynamo) -> DynamoTableAttemptStore:
    return DynamoTableAttemptStore("publications-fixture", cast("Any", dynamo))


def test_record_accepts_intent_and_writes_the_four_fields_together() -> None:
    dynamo = _RecordingDynamo()
    dynamo.items[(PARTITION, f"ATTEMPT#{ATTEMPT_ID}")] = _intent_item()
    result = _store(dynamo).record_submitted_attempt(_BASE_RECORD)
    assert result is RecordSubmittedAttemptResult.RECORDED
    stored = dynamo.items[(PARTITION, f"ATTEMPT#{ATTEMPT_ID}")]
    assert stored["state"] == {"S": "submitted"}
    assert stored["query_execution_id"] == {"S": QUERY_ID}
    assert stored["submitted_at"] == {"S": INSTANT}
    assert stored["sanitized_start_evidence"] == {
        "M": {"provider_status_code": {"N": "200"}, "provider_error_class": {"S": "none"}}
    }
    parsed = _store(dynamo).read_attempt(TABLE, ATTEMPT_ID)
    assert isinstance(parsed, SubmittedAttempt)
    assert parsed.query_execution_id == QUERY_ID


def test_record_exact_replay_is_a_byte_identical_no_op() -> None:
    dynamo = _RecordingDynamo()
    dynamo.items[(PARTITION, f"ATTEMPT#{ATTEMPT_ID}")] = _intent_item()
    store = _store(dynamo)
    store.record_submitted_attempt(_BASE_RECORD)
    snapshot = dict(dynamo.items[(PARTITION, f"ATTEMPT#{ATTEMPT_ID}")])
    result = store.record_submitted_attempt(_BASE_RECORD)
    assert result is RecordSubmittedAttemptResult.REPLAYED
    assert dynamo.items[(PARTITION, f"ATTEMPT#{ATTEMPT_ID}")] == snapshot


@pytest.mark.parametrize(
    "request_",
    [
        replace(_BASE_RECORD, query_execution_id="query-exec-fixture-0002"),
        replace(_BASE_RECORD, submitted_at=OTHER_INSTANT),
        replace(
            _BASE_RECORD,
            sanitized_start_evidence=SanitizedStartEvidence(None, ProviderErrorClass.NONE),
        ),
    ],
    ids=["changed query id", "changed submission instant", "changed evidence bytes"],
)
def test_record_classifies_a_changed_id_time_or_evidence_as_invariant_collision(
    request_: RecordSubmittedAttemptRequest,
) -> None:
    dynamo = _RecordingDynamo()
    dynamo.items[(PARTITION, f"ATTEMPT#{ATTEMPT_ID}")] = _intent_item()
    store = _store(dynamo)
    store.record_submitted_attempt(_BASE_RECORD)
    snapshot = dict(dynamo.items[(PARTITION, f"ATTEMPT#{ATTEMPT_ID}")])
    with pytest.raises(InvariantError):
        store.record_submitted_attempt(request_)
    assert dynamo.gets, "collision classification must strongly reread first"
    assert dynamo.items[(PARTITION, f"ATTEMPT#{ATTEMPT_ID}")] == snapshot


def test_record_against_an_absent_attempt_is_an_invariant_collision() -> None:
    dynamo = _RecordingDynamo()
    with pytest.raises(InvariantError):
        _store(dynamo).record_submitted_attempt(_BASE_RECORD)


def test_read_fence_returns_the_closed_form_beside_raw_reconciliation() -> None:
    dynamo = _RecordingDynamo()
    dynamo.items[(PARTITION, "FENCE")] = {
        "partition_key": {"S": PARTITION},
        "record_key": {"S": "FENCE"},
        "state": {"S": "active"},
        "physical_table_id": {"S": TABLE},
        "attempt_id": {"S": ATTEMPT_ID},
        "lease_holder": {"S": HOLDER},
        "fencing_token": {"N": str(TOKEN)},
        "reconcile_status": {"S": "pending"},
        "reconcile_after_or_block": {"S": f"{DUE_MICROS:020d}#{BINDING}"},
        "reconcile_attempt_count": {"N": "0"},
    }
    record = _store(dynamo).read_fence(TABLE)
    assert record is not None
    assert record.fence == ActiveFence(TABLE, ATTEMPT_ID, HOLDER, TOKEN)
    assert record.reconciliation.reconcile_status == "pending"
    assert reconciliation_valid(_fence(reconciliation=record.reconciliation))


def test_read_fence_raises_a_typed_error_for_a_malformed_item_and_never_repairs() -> None:
    dynamo = _RecordingDynamo()
    dynamo.items[(PARTITION, "FENCE")] = {
        "partition_key": {"S": PARTITION},
        "record_key": {"S": "FENCE"},
        "state": {"S": "active"},
        "physical_table_id": {"S": TABLE},
        # attempt_id/holder/token missing: an active fence without its binding.
    }
    before = dict(dynamo.items[(PARTITION, "FENCE")])
    with pytest.raises(InvariantError):
        _store(dynamo).read_fence(TABLE)
    assert dynamo.items[(PARTITION, "FENCE")] == before
    assert not dynamo.updates
