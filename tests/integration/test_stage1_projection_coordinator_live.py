# SPDX-License-Identifier: MIT-0
"""ADR-075 Stage-1 Slice A: the projection coordinator and Athena clients against real dev services.

These probes establish what no in-process substitute can: that the coordinator's condition
grammar, transaction shapes, and replay classification are accepted and behave as designed on
the real DynamoDB service, and that the one-attempt Athena start client's request token is
honoured by the real Athena service. Every identity carries a probe token, every written key is
recorded by the client wrapper and deleted in teardown, and no real contract id is ever written.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import boto3
import pytest
from probe_identity import pytest_node_id
from stage1_probe_world import (
    InlineWorld,
    PlanWorld,
    ProbeContext,
    ProbeIdentity,
    RecordingDynamo,
    UnitWorld,
)

from adapters.outbound.athena.table_attempt_start import (
    START_RETRY_CONFIG,
    Stage1AthenaStartClient,
)
from adapters.outbound.athena.table_attempt_statements import (
    ComposeStartEnvelopeRequest,
    compose_start_envelope,
)
from adapters.outbound.athena.table_attempt_terminal import Stage1AthenaTerminalClient
from domain.operations.status import FlowState
from domain.operations.table_attempt import (
    BindingDigestRequest,
    FenceObservation,
    ProviderErrorClass,
    ProviderTerminalState,
    SanitizedStartEvidence,
    binding_digest,
    pending_reconcile_key,
    rescheduled_reconcile_due_micros,
)
from domain.operations.table_lease import (
    RELEASED_LEASE_EXPIRY,
    TableLeaseRecord,
    lease_expiry_epoch_micros,
)
from domain.shared_kernel.errors import DomainError, InvariantError
from ports.outbound.projection_coordinator import (
    AcquireTableFenceOutcome,
    AcquireTableFenceRequest,
    AdmitUnitOutcome,
    BeginTableAttemptOutcome,
    BlockTableAttemptReconcileOutcome,
    BlockTableAttemptReconcileRequest,
    ClaimPlanIntentOutcome,
    ClaimUnitOutcome,
    CompletePlanOutcome,
    CompleteTableAttemptOutcome,
    FailUnitOutcome,
    FinalizeInlineOutcome,
    FinalizeUnitOutcome,
    MalformedReconciliationMetadataObservation,
    ParkAttemptOutcome,
    ParkAttemptRequest,
    PersistInlineRunOutcome,
    PersistPlanOutcome,
    RecordStage1RefusalOutcome,
    RecoverParkedAttemptSubmissionOutcome,
    RecoverParkedAttemptSubmissionRequest,
    ReleaseTableFenceOutcome,
    RequestAttemptCancellationOutcome,
    RequestAttemptCancellationRequest,
    RequeueBlockedTableAttemptReconcileOutcome,
    RequeueBlockedTableAttemptReconcileRequest,
    RescheduleTableAttemptReconcileOutcome,
    RescheduleTableAttemptReconcileRequest,
    SameSnapshotRescheduleRejectedObservation,
    SubmitPlanIntentOutcome,
    UnitFailureReason,
)
from ports.outbound.table_attempt import RecordSubmittedAttemptResult
from ports.outbound.table_attempt_start import (
    AcceptanceAmbiguous,
    Accepted,
    StartTableAttemptRequest,
)
from ports.outbound.table_attempt_terminal import ProviderTerminalObservation

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-075")]

_TERMINAL_POLL_SECONDS = 1.0
_TERMINAL_POLL_LIMIT = 60


# --- Fixtures -------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def probe_tables(deployed_env: dict[str, str]) -> Iterator[tuple[str, str]]:
    """Sweep stray probe tokens from both retained tables before and after the session."""
    publications = deployed_env["ADL_SEMANTIC_PUBLICATIONS_TABLE"]
    leases = deployed_env["ADL_SEMANTIC_LEASES_TABLE"]
    dynamo = RecordingDynamo(boto3.client("dynamodb"), publications, leases)
    dynamo.sweep_probe_tokens()
    yield publications, leases
    dynamo.sweep_probe_tokens()


@pytest.fixture
def world(request: pytest.FixtureRequest, probe_tables: tuple[str, str]) -> Iterator[ProbeContext]:
    """One recording client and probe identity per test; teardown proves every key gone."""
    publications, leases = probe_tables
    dynamo = RecordingDynamo(boto3.client("dynamodb"), publications, leases)
    ctx = ProbeContext(ProbeIdentity(pytest_node_id(request)), dynamo, publications, leases)
    try:
        yield ctx
    finally:
        report = dynamo.cleanup()
        assert report.residual == (), f"probe keys survived teardown: {report.residual}"


class _CountingAthena:
    """A real Athena client that counts every SDK call it forwards."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.calls: Counter[str] = Counter()

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._client, name)
        if not callable(target):
            return target

        def _forward(*args: Any, **kwargs: Any) -> Any:
            self.calls[name] += 1
            return target(*args, **kwargs)

        return _forward


@pytest.fixture
def athena() -> _CountingAthena:
    return _CountingAthena(boto3.client("athena", config=START_RETRY_CONFIG))


def _select_one_envelope(deployed_env: dict[str, str], tag: str) -> str:
    """Compose a real, cheap, deterministic statement for the deployed interactive workgroup."""
    return compose_start_envelope(
        ComposeStartEnvelopeRequest(
            workgroup=deployed_env["ADL_ATHENA_WORKGROUP"],
            database=deployed_env["ADL_SEMANTIC_V2_CURRENT_DATABASE"],
            sql=f"SELECT 1 AS integration_probe_{tag}",
            parameters=(),
        )
    )


def _await_terminal(
    terminal: Stage1AthenaTerminalClient, query_execution_id: str
) -> ProviderTerminalObservation:
    for _ in range(_TERMINAL_POLL_LIMIT):
        observed = terminal.observe(query_execution_id)
        if isinstance(observed, ProviderTerminalObservation):
            return observed
        time.sleep(_TERMINAL_POLL_SECONDS)
    pytest.fail("Athena execution did not reach a terminal state within the probe bound")


def _claimed_and_leased(plan: PlanWorld, unit_key: str, writer_tag: str) -> UnitWorld:
    """Claim one admitted unit and acquire its table lease, as the table writer does."""
    unit = plan.unit(unit_key, writer_tag)
    claimed = plan.ctx.coordinator.claim_unit(unit.claim_request())
    assert claimed.outcome is ClaimUnitOutcome.CLAIMED
    acquired = plan.ctx.coordinator.acquire_table_fence(unit.acquire_request())
    assert acquired.outcome is AcquireTableFenceOutcome.ACQUIRED
    return unit


# --- Plan and queued-unit lifecycle ---------------------------------------------------------------


def test_queued_unit_lifecycle_lands_exact_bytes_and_exact_replays_are_no_ops(  # noqa: PLR0915
    world: ProbeContext,
) -> None:
    coordinator = world.coordinator
    plan = PlanWorld(world, "plan", ("unit_a",))
    unit_key = plan.units[0].unit_key
    partition = world.identity.dataset_partition(1)
    world.seed_current(partition)

    submit = plan.submit_request()
    assert coordinator.submit_plan_intent(submit).outcome is SubmitPlanIntentOutcome.SUBMITTED
    assert coordinator.submit_plan_intent(submit).outcome is SubmitPlanIntentOutcome.REPLAYED

    claim = plan.claim_request()
    assert coordinator.claim_plan_intent(claim).outcome is ClaimPlanIntentOutcome.CLAIMED
    assert coordinator.claim_plan_intent(claim).outcome is ClaimPlanIntentOutcome.REPLAYED

    persist = plan.persist_request()
    assert coordinator.persist_plan(persist).outcome is PersistPlanOutcome.PERSISTED
    assert coordinator.persist_plan(persist).outcome is PersistPlanOutcome.REPLAYED

    admit = plan.admit_request(unit_key)
    assert coordinator.admit_unit(admit).outcome is AdmitUnitOutcome.ADMITTED
    assert coordinator.admit_unit(admit).outcome is AdmitUnitOutcome.REPLAYED
    meta = plan.meta()
    assert (meta.unit_count, meta.admitted_count) == (1, 1)

    unit = plan.unit(unit_key, "writer_1")
    claim_unit = unit.claim_request()
    assert coordinator.claim_unit(claim_unit).outcome is ClaimUnitOutcome.CLAIMED
    assert coordinator.claim_unit(claim_unit).outcome is ClaimUnitOutcome.REPLAYED
    assert unit.unit_item()["status"] == "writing"

    acquired = coordinator.acquire_table_fence(unit.acquire_request())
    assert acquired.outcome is AcquireTableFenceOutcome.ACQUIRED
    assert acquired.fencing_token == 1
    lease = unit.lease()
    assert (lease.holder, lease.fencing_token) == (unit.holder, 1)

    begin = unit.begin_request(world.start_envelope("first"))
    begun = coordinator.begin_table_attempt(begin)
    assert begun.outcome is BeginTableAttemptOutcome.BEGUN
    assert begun.ordinal == 1
    replayed_begin = coordinator.begin_table_attempt(begin)
    assert replayed_begin.outcome is BeginTableAttemptOutcome.REPLAYED
    assert replayed_begin.attempt_id == begun.attempt_id
    fence = unit.fence()
    assert fence is not None and fence.state == "active" and fence.attempt_id == begun.attempt_id
    assert plan.meta_item()["plan_reservation_count"] == 1
    assert unit.unit_item()["unit_reservation_count"] == 1

    record = unit.record_request(begun, world.identity.query_execution_id("first"))
    assert (
        world.attempt_store.record_submitted_attempt(record)
        is RecordSubmittedAttemptResult.RECORDED
    )
    assert (
        world.attempt_store.record_submitted_attempt(record)
        is RecordSubmittedAttemptResult.REPLAYED
    )

    complete = unit.complete_request(begun.attempt_id)
    assert (
        coordinator.complete_table_attempt(complete).outcome
        is CompleteTableAttemptOutcome.COMPLETED
    )
    assert (
        coordinator.complete_table_attempt(complete).outcome is CompleteTableAttemptOutcome.REPLAYED
    )
    fence = unit.fence()
    assert fence is not None and fence.state == "clear" and fence.attempt_id is None
    attempt = unit.attempt(begun.attempt_id)
    assert attempt.state == ProviderTerminalState.SUCCEEDED.value
    assert attempt.athena_usage_state == "unsettled"
    assert unit.unit_item().get("current_attempt_id") is None

    finalize = unit.finalize_request((partition,))
    assert coordinator.finalize_unit(finalize).outcome is FinalizeUnitOutcome.FINALIZED
    assert coordinator.finalize_unit(finalize).outcome is FinalizeUnitOutcome.REPLAYED
    assert unit.unit_item()["status"] == "attested"
    current = world.reader.current_item(partition)
    assert current is not None
    assert (current["projected_publication_id"], current["projected_fencing_token"]) == (
        "integration-probe-pub-0009",
        9,
    )
    assert unit.attempt(begun.attempt_id).athena_usage_state == "settled"
    meta = plan.meta()
    assert (meta.attested_count, meta.settled_attempt_count) == (1, 1)

    release = unit.release_request()
    assert coordinator.release_table_fence(release).outcome is ReleaseTableFenceOutcome.RELEASED
    assert unit.lease().expires_at == RELEASED_LEASE_EXPIRY

    complete_plan = plan.complete_plan_request()
    assert coordinator.complete_plan(complete_plan).outcome is CompletePlanOutcome.COMPLETED
    assert coordinator.complete_plan(complete_plan).outcome is CompletePlanOutcome.REPLAYED
    assert plan.meta().status.value == "completed"
    owner = plan.owner()
    assert owner.active_plan_key is None and owner.active_intent_id is None


def test_inline_run_lifecycle_with_refusal_lands_exact_bytes(world: ProbeContext) -> None:
    coordinator = world.coordinator
    inline = InlineWorld(world, "inline")
    partition = world.identity.dataset_partition(1, "inline")
    world.seed_current(partition)

    persist = inline.persist_request()
    assert coordinator.persist_inline_run(persist).outcome is PersistInlineRunOutcome.PERSISTED
    assert coordinator.persist_inline_run(persist).outcome is PersistInlineRunOutcome.REPLAYED
    assert inline.run_item()["state"] == "open"

    refusal = inline.refusal_request()
    assert coordinator.record_stage1_refusal(refusal).outcome is RecordStage1RefusalOutcome.RECORDED
    assert coordinator.record_stage1_refusal(refusal).outcome is RecordStage1RefusalOutcome.REPLAYED
    assert inline.head().latest_state.value == "retrying"

    acquired = coordinator.acquire_table_fence(inline.acquire_request())
    assert acquired.outcome is AcquireTableFenceOutcome.ACQUIRED

    first = inline.run_attempt("first")
    second = inline.run_attempt("second")
    assert first != second
    run = inline.run_item()
    assert run["accepted_attempt_reservations"] == 2
    assert run["next_ordinal"] == 3
    assert run.get("current_attempt_id") is None

    finalize = inline.finalize_request((partition,))
    assert coordinator.finalize_inline(finalize).outcome is FinalizeInlineOutcome.FINALIZED
    assert coordinator.finalize_inline(finalize).outcome is FinalizeInlineOutcome.REPLAYED
    run = inline.run_item()
    assert (run["state"], run["terminal_outcome"]) == ("terminal", "successful")
    assert inline.attempt(first).athena_usage_state == "settled"
    assert inline.attempt(second).athena_usage_state == "settled"
    current = world.reader.current_item(partition)
    assert current is not None and current["projected_fencing_token"] == 9

    assert (
        coordinator.release_table_fence(inline.release_request()).outcome
        is ReleaseTableFenceOutcome.RELEASED
    )


# --- Contention shapes ----------------------------------------------------------------------------


def test_foreign_holder_acquire_is_busy_before_and_after_the_service(world: ProbeContext) -> None:
    coordinator = world.coordinator
    plan = PlanWorld(world, "plan", ("unit_a",))
    plan.bootstrap()
    unit_key = plan.units[0].unit_key
    holder = _claimed_and_leased(plan, unit_key, "writer_1")
    live_lease = holder.lease()

    contender = UnitWorld(plan, unit_key, "writer_2")
    observed = coordinator.acquire_table_fence(contender.acquire_request())
    assert observed.outcome is AcquireTableFenceOutcome.TABLE_BUSY
    assert observed.held_until == live_lease.expires_at
    transactions_before = world.dynamo.calls["TransactWriteItems"]

    stale = AcquireTableFenceRequest(
        physical_table_id=contender.table_id,
        holder=contender.holder,
        operation_instant=world.instant(),
        new_expires_at=world.clock.lease_expiry(900),
        observed_lease=None,
        observed_fence=None,
    )
    aborted = coordinator.acquire_table_fence(stale)
    assert aborted.outcome is AcquireTableFenceOutcome.TABLE_BUSY
    assert aborted.held_until == live_lease.expires_at
    assert world.dynamo.calls["TransactWriteItems"] == transactions_before + 1
    assert holder.lease() == live_lease

    coordinator.release_table_fence(holder.release_request())
    reacquired = coordinator.acquire_table_fence(contender.acquire_request())
    assert reacquired.outcome is AcquireTableFenceOutcome.ACQUIRED
    assert reacquired.fencing_token == 2


def test_stale_lease_begin_is_a_typed_conflict_not_a_write(world: ProbeContext) -> None:
    coordinator = world.coordinator
    plan = PlanWorld(world, "plan", ("unit_a",))
    plan.bootstrap()
    unit_key = plan.units[0].unit_key
    first = _claimed_and_leased(plan, unit_key, "writer_1")
    stale_lease = first.lease()

    # A second acquisition by the same holder renews the token; the first snapshot is stale.
    renewed = coordinator.acquire_table_fence(first.acquire_request())
    assert renewed.outcome is AcquireTableFenceOutcome.ACQUIRED
    assert renewed.fencing_token == 2

    stale_begin = replace(first.begin_request(world.start_envelope("stale")), lease=stale_lease)
    with pytest.raises(DomainError) as conflict:
        coordinator.begin_table_attempt(stale_begin)
    assert (conflict.value.code.area, conflict.value.code.number) == ("PLAN_COORD", 1)
    assert first.fence() is None
    assert "plan_reservation_count" not in plan.meta_item()


def test_reclaim_refuses_a_live_prior_writer_then_claims_after_release(
    world: ProbeContext,
) -> None:
    coordinator = world.coordinator
    plan = PlanWorld(world, "plan", ("unit_a",))
    plan.bootstrap()
    unit_key = plan.units[0].unit_key
    dead = _claimed_and_leased(plan, unit_key, "writer_1")
    live_lease = dead.lease()

    successor = UnitWorld(plan, unit_key, "writer_2")
    reclaim = successor.claim_request(prior_writer_execution_id=dead.writer_execution_id)
    assert reclaim.reclaim
    refused = coordinator.claim_unit(reclaim)
    assert refused.outcome is ClaimUnitOutcome.PRIOR_WRITER_LIVE
    assert refused.held_until == live_lease.expires_at
    assert dead.unit_item()["writer_execution_id"] == dead.writer_execution_id

    coordinator.release_table_fence(dead.release_request())
    reclaimed = coordinator.claim_unit(
        successor.claim_request(prior_writer_execution_id=dead.writer_execution_id)
    )
    assert reclaimed.outcome is ClaimUnitOutcome.CLAIMED
    assert successor.unit_item()["writer_execution_id"] == successor.writer_execution_id
    assert successor.head().latest_state.value == "running"


def test_record_submitted_attempt_collision_is_a_typed_invariant(world: ProbeContext) -> None:
    coordinator = world.coordinator
    inline = InlineWorld(world, "inline")
    coordinator.persist_inline_run(inline.persist_request())
    coordinator.acquire_table_fence(inline.acquire_request())
    begun = coordinator.begin_table_attempt(inline.begin_request(world.start_envelope("one")))
    record = inline.record_request(begun, world.identity.query_execution_id("one"))
    assert (
        world.attempt_store.record_submitted_attempt(record)
        is RecordSubmittedAttemptResult.RECORDED
    )
    other = inline.record_request(begun, world.identity.query_execution_id("other"))
    with pytest.raises(InvariantError):
        world.attempt_store.record_submitted_attempt(other)
    attempt = inline.attempt(begun.attempt_id)
    assert attempt.query_execution_id == world.identity.query_execution_id("one")


def test_lease_expiry_comparison_is_lexical_across_the_fractional_second_boundary(
    world: ProbeContext,
) -> None:
    """Prevent a whole-second and a fractional rendering of one second from misordering.

    The reclaim condition compares ``expires_at <= :instant`` as strings; both renderings
    must order the way the domain's epoch-micros comparison does.
    """
    whole = "2026-09-21T03:00:00+00:00"
    fractional = "2026-09-21T03:00:00.000001+00:00"
    assert lease_expiry_epoch_micros(whole) < lease_expiry_epoch_micros(fractional)
    assert whole < fractional
    assert whole > RELEASED_LEASE_EXPIRY
    later_whole = "2026-09-21T03:00:01+00:00"
    assert fractional < later_whole
    assert lease_expiry_epoch_micros(fractional) < lease_expiry_epoch_micros(later_whole)
    record = TableLeaseRecord(
        physical_table_id=world.identity.table("lexical"),
        holder="integration-probe-holder",
        fencing_token=1,
        expires_at=fractional,
    )
    assert record.expiry_epoch_micros() == lease_expiry_epoch_micros(fractional)


# --- Athena start and terminal clients ------------------------------------------------------------


def test_same_envelope_and_token_yield_the_same_query_execution(
    deployed_env: dict[str, str], athena: _CountingAthena, world: ProbeContext
) -> None:
    start = Stage1AthenaStartClient(cast("Any", athena))
    terminal = Stage1AthenaTerminalClient(cast("Any", athena))
    envelope = _select_one_envelope(deployed_env, world.identity.suffix)
    token = world.identity.writer_execution("athena-token")
    first = start.start_table_attempt(StartTableAttemptRequest(envelope, token))
    assert isinstance(first, Accepted), first
    second = start.start_table_attempt(StartTableAttemptRequest(envelope, token))
    assert isinstance(second, Accepted), second
    assert second.query_execution_id == first.query_execution_id
    assert athena.calls["start_query_execution"] == 2
    observed = _await_terminal(terminal, first.query_execution_id)
    assert observed.state is ProviderTerminalState.SUCCEEDED
    assert observed.query_count == 1
    assert observed.engine_ms is not None and observed.engine_ms >= 0
    rows = terminal.readback_rows(first.query_execution_id)
    assert rows == (("1",),)


def test_malformed_envelope_is_ambiguous_unknown_with_zero_provider_calls(
    athena: _CountingAthena,
) -> None:
    start = Stage1AthenaStartClient(cast("Any", athena))
    malformed = json.dumps({"WorkGroup": "integration-probe", "Statement": "SELECT 1"})
    result = start.start_table_attempt(StartTableAttemptRequest(malformed, "0" * 64))
    assert isinstance(result, AcceptanceAmbiguous)
    assert result.reason is ProviderErrorClass.UNKNOWN
    assert result.sanitized_evidence.provider_status_code is None
    assert athena.calls == Counter()


def test_stop_drives_a_running_execution_to_cancelled(
    deployed_env: dict[str, str], athena: _CountingAthena, world: ProbeContext
) -> None:
    start = Stage1AthenaStartClient(cast("Any", athena))
    terminal = Stage1AthenaTerminalClient(cast("Any", athena))
    envelope = compose_start_envelope(
        ComposeStartEnvelopeRequest(
            workgroup=deployed_env["ADL_ATHENA_WORKGROUP"],
            database=deployed_env["ADL_SEMANTIC_V2_CURRENT_DATABASE"],
            sql="SELECT count(*) FROM UNNEST(sequence(1, 20000000)) AS t(n) WHERE n % 7 = 0",
            parameters=(),
        )
    )
    accepted = start.start_table_attempt(
        StartTableAttemptRequest(envelope, world.identity.writer_execution("athena-stop"))
    )
    assert isinstance(accepted, Accepted), accepted
    terminal.stop(accepted.query_execution_id)
    observed = _await_terminal(terminal, accepted.query_execution_id)
    assert observed.state is ProviderTerminalState.CANCELLED
    # A cancelled execution reports no engine time; the settlement must carry it as missing.
    assert observed.engine_ms is None
    assert athena.calls["stop_query_execution"] == 1


# --- Reconciliation family ------------------------------------------------------------------------


def _corrupt_fence_count(world: ProbeContext, table_id: str) -> None:
    """Write a boolean where the reconciler expects an integer count, as a bug would."""
    world.dynamo.update_item(
        TableName=world.publications,
        Key={
            "partition_key": {"S": f"TABLE_ATTEMPT#{table_id}"},
            "record_key": {"S": "FENCE"},
        },
        UpdateExpression="SET reconcile_attempt_count = :corrupt",
        ExpressionAttributeValues={":corrupt": {"BOOL": True}},
    )


def test_reconcile_family_drives_park_recover_cancel_reschedule_block_and_requeue(  # noqa: PLR0915
    world: ProbeContext,
) -> None:
    coordinator = world.coordinator
    plan = PlanWorld(world, "plan", ("unit_a",))
    plan.bootstrap()
    unit = _claimed_and_leased(plan, plan.units[0].unit_key, "writer_1")
    begun = coordinator.begin_table_attempt(unit.begin_request(world.start_envelope("one")))
    fence = unit.fence()
    assert fence is not None
    digest = binding_digest(
        BindingDigestRequest(
            unit.table_id, begun.attempt_id, unit.holder, unit.lease().fencing_token
        )
    )

    reschedule = RescheduleTableAttemptReconcileRequest(
        unit.table_id, fence, unit.attempt(begun.attempt_id), world.instant()
    )
    rescheduled = coordinator.reschedule_table_attempt_reconcile(reschedule)
    assert rescheduled.outcome is RescheduleTableAttemptReconcileOutcome.RESCHEDULED
    assert rescheduled.reconcile_attempt_count == 1
    assert rescheduled.reconcile_after_or_block == pending_reconcile_key(
        rescheduled_reconcile_due_micros(reschedule.operation_instant, 0), digest
    )
    replayed = coordinator.reschedule_table_attempt_reconcile(reschedule)
    assert replayed.outcome is RescheduleTableAttemptReconcileOutcome.REPLAYED
    assert replayed.reconcile_after_or_block == rescheduled.reconcile_after_or_block

    park = ParkAttemptRequest(
        unit.table_id,
        unit.lease(),
        _required_fence(unit),
        unit.attempt(begun.attempt_id),
        world.instant(),
    )
    assert coordinator.park_attempt(park).outcome is ParkAttemptOutcome.PARKED
    lease = unit.lease()
    assert lease.parked_at == park.park_instant
    parked_fence = _required_fence(unit)
    assert (parked_fence.state, parked_fence.parked_at) == ("parked", park.park_instant)
    assert parked_fence.reconciliation.reconcile_attempt_count == 2
    assert parked_fence.reconciliation.reconcile_after_or_block == pending_reconcile_key(
        rescheduled_reconcile_due_micros(park.park_instant, 1), digest
    )
    assert unit.attempt(begun.attempt_id).state == "parked"
    transactions = world.dynamo.calls["TransactWriteItems"]
    replay_park = ParkAttemptRequest(
        unit.table_id, lease, parked_fence, unit.attempt(begun.attempt_id), park.park_instant
    )
    assert coordinator.park_attempt(replay_park).outcome is ParkAttemptOutcome.REPLAYED
    assert world.dynamo.calls["TransactWriteItems"] == transactions

    recover = RecoverParkedAttemptSubmissionRequest(
        physical_table_id=unit.table_id,
        fence=parked_fence,
        attempt=unit.attempt(begun.attempt_id),
        query_execution_id=world.identity.query_execution_id("recovered"),
        submitted_at=world.instant(),
        sanitized_start_evidence=SanitizedStartEvidence(200, ProviderErrorClass.NONE),
    )
    assert (
        coordinator.recover_parked_attempt_submission(recover).outcome
        is RecoverParkedAttemptSubmissionOutcome.RECOVERED
    )
    assert (
        coordinator.recover_parked_attempt_submission(recover).outcome
        is RecoverParkedAttemptSubmissionOutcome.REPLAYED
    )
    attempt = unit.attempt(begun.attempt_id)
    assert attempt.query_execution_id == recover.query_execution_id
    assert attempt.state == "parked"

    cancel = RequestAttemptCancellationRequest(
        physical_table_id=unit.table_id,
        fence=_required_fence(unit),
        attempt=attempt,
        cancel_requested_at=world.instant(),
        owner=unit.owner_snapshot(),
    )
    assert (
        coordinator.request_attempt_cancellation(cancel).outcome
        is RequestAttemptCancellationOutcome.REQUESTED
    )
    assert (
        coordinator.request_attempt_cancellation(cancel).outcome
        is RequestAttemptCancellationOutcome.REPLAYED
    )
    attempt = unit.attempt(begun.attempt_id)
    assert (attempt.state, attempt.cancel_requested_at) == ("parked", cancel.cancel_requested_at)

    fence = _required_fence(unit)
    block = BlockTableAttemptReconcileRequest(
        unit.table_id,
        fence,
        attempt,
        unit.owner_snapshot(),
        SameSnapshotRescheduleRejectedObservation(fence.reconciliation),
        world.instant(),
    )
    blocked = coordinator.block_table_attempt_reconcile(block)
    assert blocked.outcome is BlockTableAttemptReconcileOutcome.BLOCKED
    assert coordinator.block_table_attempt_reconcile(block).outcome is (
        BlockTableAttemptReconcileOutcome.REPLAYED
    )
    fence = _required_fence(unit)
    assert fence.reconciliation.reconcile_status == "blocked"
    assert fence.reconciliation.reconcile_after_or_block == f"BLOCK#{blocked.recovery_ref}"
    assert fence.reconciliation.reconcile_attempt_count is None
    case = world.attempt_store.read_protected_case(blocked.recovery_ref)
    assert case is not None
    assert (case.state.value, case.reason.value) == (
        "blocked",
        "SAME_SNAPSHOT_RESCHEDULE_REJECTED",
    )

    requeue = RequeueBlockedTableAttemptReconcileRequest(
        physical_table_id=unit.table_id,
        case=case,
        fence=fence,
        attempt=unit.attempt(begun.attempt_id),
        owner=unit.owner_snapshot(),
        operation_id=world.identity.operation_id("requeue"),
        resolution_reason="integration-probe resolution",
        requeue_instant=world.instant(),
    )
    requeued = coordinator.requeue_blocked_table_attempt_reconcile(requeue)
    assert requeued.outcome is RequeueBlockedTableAttemptReconcileOutcome.REQUEUED
    assert coordinator.requeue_blocked_table_attempt_reconcile(requeue).outcome is (
        RequeueBlockedTableAttemptReconcileOutcome.ALREADY_REQUEUED
    )
    fence = _required_fence(unit)
    assert fence.reconciliation.reconcile_status == "pending"
    assert fence.reconciliation.reconcile_attempt_count == 0
    assert fence.reconciliation.reconcile_after_or_block == f"{requeue.requeue_instant}#{digest}"
    assert fence.reconciliation.requeued_from_case_ref == blocked.recovery_ref
    consumed = world.attempt_store.read_protected_case(blocked.recovery_ref)
    assert consumed is not None and consumed.state.value == "requeued"

    _corrupt_fence_count(world, unit.table_id)
    fence = _required_fence(unit)
    assert fence.reconciliation.reconcile_attempt_count is True
    malformed = BlockTableAttemptReconcileRequest(
        unit.table_id,
        fence,
        unit.attempt(begun.attempt_id),
        unit.owner_snapshot(),
        MalformedReconciliationMetadataObservation(fence.reconciliation),
        world.instant(),
    )
    malformed_block = coordinator.block_table_attempt_reconcile(malformed)
    assert malformed_block.outcome is BlockTableAttemptReconcileOutcome.BLOCKED
    assert malformed_block.recovery_ref != blocked.recovery_ref
    fence = _required_fence(unit)
    assert fence.reconciliation.reconcile_block_reason == "MALFORMED_RECONCILIATION_METADATA"
    assert fence.reconciliation.reconcile_attempt_count is None

    complete = unit.complete_request(begun.attempt_id, ProviderTerminalState.CANCELLED)
    assert (
        coordinator.complete_table_attempt(complete).outcome
        is CompleteTableAttemptOutcome.COMPLETED
    )
    fence = _required_fence(unit)
    assert fence.state == "clear"
    assert fence.reconciliation.reconcile_status is None


def _required_fence(unit: UnitWorld | InlineWorld) -> FenceObservation:
    fence = unit.fence()
    assert fence is not None, "the fence is present in the live store"
    return fence


def test_inline_tagged_cancellation_changes_the_run_so_no_further_begin_is_accepted(
    world: ProbeContext,
) -> None:
    coordinator = world.coordinator
    inline = InlineWorld(world, "inline")
    coordinator.persist_inline_run(inline.persist_request())
    coordinator.acquire_table_fence(inline.acquire_request())
    begun = coordinator.begin_table_attempt(inline.begin_request(world.start_envelope("one")))
    world.attempt_store.record_submitted_attempt(
        inline.record_request(begun, world.identity.query_execution_id("one"))
    )
    cancel = RequestAttemptCancellationRequest(
        physical_table_id=inline.table_id,
        fence=_required_fence(inline),
        attempt=inline.attempt(begun.attempt_id),
        cancel_requested_at=world.instant(),
        owner=inline.owner_snapshot(),
        proof=inline.later_proof(
            "semantic_inline_run_cancel_requested",
            FlowState.RUNNING,
            None,
            None,
            "inline_run_cancel_requested",
        ),
    )
    assert (
        coordinator.request_attempt_cancellation(cancel).outcome
        is RequestAttemptCancellationOutcome.REQUESTED
    )
    assert (
        coordinator.request_attempt_cancellation(cancel).outcome
        is RequestAttemptCancellationOutcome.REPLAYED
    )
    assert inline.run_item()["state"] == "cancel_requested"
    assert inline.attempt(begun.attempt_id).state == "cancel_requested"
    assert inline.head().latest_state.value == "running"
    with pytest.raises(ValueError, match="open or reconciled"):
        inline.begin_request(world.start_envelope("two"))
    complete = inline.complete_request(begun.attempt_id, ProviderTerminalState.CANCELLED)
    assert (
        coordinator.complete_table_attempt(complete).outcome
        is CompleteTableAttemptOutcome.COMPLETED
    )
    # §3.1: the direct path returns the run to ``open`` after the provider terminal, so the
    # next statement's Begin is accepted again against the now-clear fence.
    run = inline.run_item()
    assert (run["state"], run["next_ordinal"]) == ("open", 2)
    assert run.get("current_attempt_id") is None
    assert inline.attempt(begun.attempt_id).state == ProviderTerminalState.CANCELLED.value


# --- Unit failure branches ------------------------------------------------------------------------


def test_ordinary_unit_failure_then_redrive_claim_decrements_failed_count_once(
    world: ProbeContext,
) -> None:
    coordinator = world.coordinator
    plan = PlanWorld(world, "plan", ("unit_a",))
    plan.bootstrap()
    unit_key = plan.units[0].unit_key
    unit = _claimed_and_leased(plan, unit_key, "writer_1")
    attempt_id = unit.run_attempt("one")

    fail = unit.fail_request(UnitFailureReason.STATEMENT_RETRIES_EXHAUSTED)
    assert coordinator.fail_unit(fail).outcome is FailUnitOutcome.FAILED
    assert coordinator.fail_unit(fail).outcome is FailUnitOutcome.REPLAYED
    item = unit.unit_item()
    assert (item["status"], item["failure_reason"]) == ("failed", "statement_retries_exhausted")
    meta = plan.meta()
    assert (meta.failed_count, meta.settled_attempt_count, meta.plan_reservation_count) == (1, 1, 1)
    assert unit.attempt(attempt_id).athena_usage_state == "settled"
    coordinator.release_table_fence(unit.release_request())

    successor = UnitWorld(plan, unit_key, "writer_2")
    redrive = successor.claim_request()
    assert redrive.redrive
    claimed = coordinator.claim_unit(redrive)
    assert (claimed.outcome, claimed.redrive) == (ClaimUnitOutcome.CLAIMED, True)
    assert coordinator.claim_unit(redrive).outcome is ClaimUnitOutcome.REPLAYED
    item = successor.unit_item()
    assert item["status"] == "writing" and "failure_reason" not in item
    assert plan.meta().failed_count == 0


def test_attempt_33_supersedes_the_plan_and_creates_one_successor_intent(
    world: ProbeContext,
) -> None:
    coordinator = world.coordinator
    plan = PlanWorld(world, "plan", ("unit_a", "unit_b"))
    plan.bootstrap()
    unit = _claimed_and_leased(plan, plan.units[0].unit_key, "writer_1")
    attempt_id = unit.run_attempt("one")
    owner_before = plan.owner()

    budget = unit.budget_fail_request()
    failed = coordinator.fail_unit(budget)
    assert failed.outcome is FailUnitOutcome.FAILED
    assert failed.successor_intent_id == budget.successor_intent_id()
    replayed = coordinator.fail_unit(budget)
    assert replayed.outcome is FailUnitOutcome.REPLAYED
    assert replayed.successor_intent_id == failed.successor_intent_id

    meta = plan.meta()
    assert (meta.status.value, meta.terminal_at) == ("superseded", budget.operation_instant)
    assert meta.settled_attempt_count == 1
    owner = plan.owner()
    assert owner.revision == owner_before.revision + 1
    assert owner.active_plan_key is None and owner.active_intent_id is None
    assert failed.successor_intent_id is not None
    successor = world.reader.intent(plan.plan_digest, failed.successor_intent_id)
    assert successor is not None
    assert (successor.state.value, successor.origin.kind.value, successor.source_plan_key) == (
        "pending",
        "plan_attempt_budget",
        plan.plan_key,
    )
    assert unit.attempt(attempt_id).athena_usage_state == "settled"
    assert unit.unit_item()["status"] == "writing"


# --- Maximum action maps --------------------------------------------------------------------------


def test_maximum_finalize_unit_and_inline_transactions_are_accepted(world: ProbeContext) -> None:
    coordinator = world.coordinator
    unit_partitions = tuple(world.identity.dataset_partition(n, "unit") for n in range(37))
    inline_partitions = tuple(world.identity.dataset_partition(n, "inline") for n in range(37))
    for partition in (*unit_partitions, *inline_partitions):
        world.seed_current(partition)

    plan = PlanWorld(world, "plan", ("unit_a",))
    plan.bootstrap()
    unit = _claimed_and_leased(plan, plan.units[0].unit_key, "writer_1")
    attempts = [unit.run_attempt(f"a{n}") for n in range(10)]
    assert len(set(attempts)) == 10
    assert unit.unit_item()["unit_reservation_count"] == 10
    with pytest.raises(ValueError, match="10-reservation unit cap"):
        unit.begin_request(world.start_envelope("beyond"))
    finalize = unit.finalize_request(unit_partitions)
    assert coordinator.finalize_unit(finalize).outcome is FinalizeUnitOutcome.FINALIZED
    assert len(world.dynamo.transactions[-1]["TransactItems"]) == 100
    assert coordinator.finalize_unit(finalize).outcome is FinalizeUnitOutcome.REPLAYED
    meta = plan.meta()
    assert (meta.attested_count, meta.settled_attempt_count, meta.plan_reservation_count) == (
        1,
        10,
        10,
    )
    for partition in unit_partitions:
        current = world.reader.current_item(partition)
        assert current is not None and current["projected_fencing_token"] == 9

    inline = InlineWorld(world, "inline")
    coordinator.persist_inline_run(inline.persist_request())
    coordinator.acquire_table_fence(inline.acquire_request())
    for n in range(10):
        inline.run_attempt(f"i{n}")
    with pytest.raises(ValueError, match="10-reservation run cap"):
        inline.begin_request(world.start_envelope("beyond"))
    finalize_inline = inline.finalize_request(inline_partitions)
    assert coordinator.finalize_inline(finalize_inline).outcome is FinalizeInlineOutcome.FINALIZED
    assert len(world.dynamo.transactions[-1]["TransactItems"]) == 99
    assert coordinator.finalize_inline(finalize_inline).outcome is FinalizeInlineOutcome.REPLAYED
    assert inline.run_item()["state"] == "terminal"


def test_maximum_planned_plan_replacement_is_one_accepted_100_action_transaction(
    world: ProbeContext,
) -> None:
    """A redelivery while the source plan is active supersedes it in one transaction.

    The maximum: 15 source units, 32 settled attempts (the whole permanent plan budget),
    and a 15-unit target roster — 6 + 15 + 15 + 32 + 32 = 100 distinct-item actions.
    """
    coordinator = world.coordinator
    unit_tags = tuple(f"u{n:02d}" for n in range(15))
    source = PlanWorld(world, "plan", unit_tags)
    source.bootstrap()
    assert source.meta().unit_count == 15

    attempts_per_unit = (10, 10, 10, 2)
    for index, count in enumerate(attempts_per_unit):
        unit = _claimed_and_leased(source, source.units[index].unit_key, f"writer_{index}")
        for ordinal in range(count):
            unit.run_attempt(f"u{index}a{ordinal}")
        partition = world.identity.dataset_partition(index, "replacement")
        world.seed_current(partition)
        finalize = unit.finalize_request((partition,), prior_settled=source.settled_references())
        assert coordinator.finalize_unit(finalize).outcome is FinalizeUnitOutcome.FINALIZED
        coordinator.release_table_fence(unit.release_request())
    meta = source.meta()
    assert (meta.settled_attempt_count, meta.plan_reservation_count, meta.attested_count) == (
        32,
        32,
        4,
    )
    with pytest.raises(ValueError, match="32-reservation plan cap"):
        _claimed_and_leased(source, source.units[4].unit_key, "writer_4").begin_request(
            world.start_envelope("beyond")
        )
    coordinator.release_table_fence(
        UnitWorld(source, source.units[4].unit_key, "writer_4").release_request()
    )

    target = PlanWorld(world, "plan", unit_tags, source=source)
    assert target.intent_id != source.intent_id
    assert target.generation == 1
    submit = target.submit_request()
    assert coordinator.submit_plan_intent(submit).outcome is SubmitPlanIntentOutcome.SUBMITTED
    claim = target.claim_request()
    claimed = coordinator.claim_plan_intent(claim)
    assert (claimed.outcome, claimed.claimed_generation) == (ClaimPlanIntentOutcome.CLAIMED, 1)

    head_before = source.plan_head()
    persist = target.persist_request()
    assert persist.source_plan is not None
    assert len(persist.source_plan.attempts) == 32
    persisted = coordinator.persist_plan(persist)
    assert persisted.outcome is PersistPlanOutcome.PERSISTED
    assert len(world.dynamo.transactions[-1]["TransactItems"]) == 100
    replayed = coordinator.persist_plan(persist)
    assert (replayed.outcome, replayed.generation) == (PersistPlanOutcome.REPLAYED, 1)

    superseded = source.meta()
    assert (superseded.status.value, superseded.terminal_at) == (
        "superseded",
        persist.operation_instant,
    )
    installed = target.meta()
    assert (installed.status.value, installed.generation, installed.unit_count) == (
        "planned",
        1,
        15,
    )
    owner = target.owner()
    assert (owner.active_plan_key, owner.last_generation, owner.reserved_intent_id) == (
        target.plan_key,
        1,
        None,
    )
    head = target.plan_head()
    assert head.current_internal_execution_id == target.internal_execution_id
    assert head.predecessor_execution_id == head_before.current_execution_id
    assert head.latest_transition_sequence == head_before.latest_transition_sequence + 2
