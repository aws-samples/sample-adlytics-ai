# SPDX-License-Identifier: MIT-0
"""ADR-075 Stage-1 reconciler and recovery jobs over the real coordinator and a fake DynamoDB."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from stage1_world_support import ENVELOPE, PUBLICATIONS, World

from adapters.jobs.plan_intent_reconciler import PlanIntentReconciler, WriteUnitSender
from adapters.jobs.provider_attempt_reconciler import (
    CORRUPT_PROVIDER_CORE_AUTHORITY,
    ProviderAttemptReconciler,
)
from adapters.jobs.provider_fence_recovery import ProviderFenceRecovery, recover
from domain.operations.cutover import DRAIN_SEQ_PARTITION_KEY, DRAIN_SEQ_RECORD_KEY
from domain.operations.execution_graph import AuthorityMode
from domain.operations.provider_fence_recovery import (
    ProviderFenceRecoveryError,
    ProviderFenceRecoveryErrorCode,
    parse_invocation,
)
from domain.operations.table_attempt import (
    ProviderErrorClass,
    ProviderTerminalState,
    SanitizedStartEvidence,
)
from domain.shared_kernel.errors import (
    DependencyError,
    ErrorClass,
    ErrorCode,
    InvariantError,
)
from ports.outbound.projection_coordinator import (
    BlockTableAttemptReconcileRequest,
    SameSnapshotRescheduleRejectedObservation,
)
from ports.outbound.table_attempt_start import (
    AcceptanceAmbiguous,
    Accepted,
    StartTableAttemptRequest,
    StartTableAttemptResult,
)
from ports.outbound.table_attempt_terminal import (
    ObserveTableAttemptResult,
    ProviderNonterminal,
    ProviderTerminalObservation,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

_SUCCEEDED = ProviderTerminalObservation(ProviderTerminalState.SUCCEEDED, 1, 2048, 700)
_EVIDENCE_NONE = ProviderErrorClass.NONE


@dataclass
class _Athena:
    """A scripted start/terminal client pair recording every provider call."""

    observations: list[ObserveTableAttemptResult] = field(
        default_factory=list[ObserveTableAttemptResult]
    )
    starts: list[StartTableAttemptResult] = field(default_factory=list[StartTableAttemptResult])
    observed: list[str] = field(default_factory=list[str])
    stopped: list[str] = field(default_factory=list[str])
    started: list[StartTableAttemptRequest] = field(default_factory=list[StartTableAttemptRequest])

    def observe(self, query_execution_id: str) -> ObserveTableAttemptResult:
        self.observed.append(query_execution_id)
        return self.observations.pop(0)

    def stop(self, query_execution_id: str) -> None:
        self.stopped.append(query_execution_id)

    def readback_rows(self, query_execution_id: str) -> tuple[tuple[str | None, ...], ...]:
        raise AssertionError(f"the reconciler never reads rows ({query_execution_id})")

    def start_table_attempt(self, request: StartTableAttemptRequest) -> StartTableAttemptResult:
        self.started.append(request)
        return self.starts.pop(0)


def _reconciler(
    world: World, athena: _Athena, *, now: int | None = None
) -> ProviderAttemptReconciler:
    clock = world.clock

    def _now() -> str:
        if now is not None:
            return f"{now:020d}"
        return clock.instant()

    return ProviderAttemptReconciler(
        discovery=world.discovery,
        reader=world.reader,
        coordinator=world.coordinator,
        start=athena,
        terminal=athena,
        batch_size=10,
        now=_now,
    )


def _far_future(world: World) -> int:
    return world.clock.micros + 3_600 * 1_000_000


def _captured_errors(
    monkeypatch: pytest.MonkeyPatch, module: Any
) -> list[tuple[BaseException, Any]]:
    reported: list[tuple[BaseException, Any]] = []

    def capture(error: BaseException, report: Any) -> None:
        reported.append((error, report))

    monkeypatch.setattr(module, "report_error", capture)
    return reported


def _assert_dependency_report(
    reported: list[tuple[BaseException, Any]], failure: DependencyError, operation: str
) -> None:
    [(error, report)] = reported
    assert error is failure
    assert failure.error_class is ErrorClass.DEPENDENCY
    assert report.operation == operation


# --- ProviderAttemptReconciler --------------------------------------------------------------------


def test_a_due_submitted_queued_attempt_with_a_provider_terminal_is_completed() -> None:
    world = World()
    plan_key = world.bootstrap_plan("plan", ("fictional_db.alpha",))
    plan_digest = str(world.dynamo.items[(PUBLICATIONS, plan_key, "META")]["plan_digest"]["S"])
    writer = world.claim_and_lease_unit(plan_digest, plan_key, "fictional_db.alpha")
    begun = world.begin_queued(plan_key, "fictional_db.alpha", writer)
    world.record("fictional_db.alpha", begun, "fictional-query-0001")
    athena = _Athena(observations=[_SUCCEEDED])

    counters = _reconciler(world, athena, now=_far_future(world)).run_once()

    assert counters == {"candidates": 1, "completed": 1}
    assert athena.observed == ["fictional-query-0001"]
    fence = world.reader.fence("fictional_db.alpha")
    assert fence is not None and fence.state == "clear"
    attempt = world.reader.attempt("fictional_db.alpha", begun.attempt_id)
    assert attempt.state == "SUCCEEDED" if attempt else False
    assert attempt is not None and (attempt.scanned_bytes, attempt.engine_ms) == (2048, 700)


def test_a_provider_dependency_is_reported_once_before_the_candidate_is_rescheduled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters.jobs import provider_attempt_reconciler as module  # noqa: PLC0415

    reported = _captured_errors(monkeypatch, module)
    world = World()
    plan_key = world.bootstrap_plan("plan", ("fictional_db.alpha",))
    plan_digest = str(world.dynamo.items[(PUBLICATIONS, plan_key, "META")]["plan_digest"]["S"])
    writer = world.claim_and_lease_unit(plan_digest, plan_key, "fictional_db.alpha")
    begun = world.begin_queued(plan_key, "fictional_db.alpha", writer)
    world.record("fictional_db.alpha", begun, "fictional-query-dependency")
    athena = _Athena()
    failure = DependencyError(ErrorCode("TEST", 1), "provider unavailable")

    def fail_observe(_query_execution_id: str) -> ObserveTableAttemptResult:
        raise failure

    monkeypatch.setattr(athena, "observe", fail_observe)

    counters = _reconciler(world, athena, now=_far_future(world)).run_once()

    assert counters == {"candidates": 1, "dependency_rescheduled": 1}
    fence = world.reader.fence("fictional_db.alpha")
    assert fence is not None and fence.reconciliation.reconcile_status == "pending"
    _assert_dependency_report(reported, failure, "stage1_provider_attempt.reconcile")


def test_an_intent_attempt_is_parked_without_any_start_replay() -> None:
    world = World()
    plan_key = world.bootstrap_plan("plan", ("fictional_db.alpha",))
    plan_digest = str(world.dynamo.items[(PUBLICATIONS, plan_key, "META")]["plan_digest"]["S"])
    writer = world.claim_and_lease_unit(plan_digest, plan_key, "fictional_db.alpha")
    begun = world.begin_queued(plan_key, "fictional_db.alpha", writer)
    athena = _Athena()

    counters = _reconciler(world, athena, now=_far_future(world)).run_once()

    assert counters == {"candidates": 1, "parked": 1}
    assert athena.started == [] and athena.observed == []
    fence = world.reader.fence("fictional_db.alpha")
    assert fence is not None and fence.state == "parked"
    assert fence.reconciliation.reconcile_attempt_count == 1
    lease = world.reader.lease("fictional_db.alpha")
    assert lease is not None and lease.parked_at is not None
    assert world.reader.attempt("fictional_db.alpha", begun.attempt_id).state == "parked"  # type: ignore[union-attr]


def test_a_parked_attempt_without_a_query_id_replays_start_then_recovers_and_reschedules() -> None:
    world = World()
    plan_key = world.bootstrap_plan("plan", ("fictional_db.alpha",))
    plan_digest = str(world.dynamo.items[(PUBLICATIONS, plan_key, "META")]["plan_digest"]["S"])
    writer = world.claim_and_lease_unit(plan_digest, plan_key, "fictional_db.alpha")
    begun = world.begin_queued(plan_key, "fictional_db.alpha", writer)
    parked = _reconciler(world, _Athena(), now=_far_future(world)).run_once()
    assert parked == {"candidates": 1, "parked": 1}
    athena = _Athena(
        starts=[Accepted("fictional-query-0002", _evidence())],
        observations=[ProviderNonterminal(reason="nonterminal")],
    )

    counters = _reconciler(world, athena, now=_far_future(world) + 1_000_000_000).run_once()

    assert counters == {"candidates": 1, "recovered_rescheduled": 1}
    assert [request.client_request_token for request in athena.started] == [begun.attempt_id]
    assert athena.started[0].start_envelope == ENVELOPE
    attempt = world.reader.attempt("fictional_db.alpha", begun.attempt_id)
    assert attempt is not None
    assert (attempt.state, attempt.query_execution_id) == ("parked", "fictional-query-0002")
    fence = world.reader.fence("fictional_db.alpha")
    assert fence is not None and fence.reconciliation.reconcile_attempt_count == 2


def test_an_ambiguous_start_replay_only_reschedules() -> None:
    world = World()
    plan_key = world.bootstrap_plan("plan", ("fictional_db.alpha",))
    plan_digest = str(world.dynamo.items[(PUBLICATIONS, plan_key, "META")]["plan_digest"]["S"])
    writer = world.claim_and_lease_unit(plan_digest, plan_key, "fictional_db.alpha")
    world.begin_queued(plan_key, "fictional_db.alpha", writer)
    _reconciler(world, _Athena(), now=_far_future(world)).run_once()
    athena = _Athena(starts=[AcceptanceAmbiguous(ProviderErrorClass.THROTTLING, _evidence(429))])

    counters = _reconciler(world, athena, now=_far_future(world) + 1_000_000_000).run_once()

    assert counters == {"candidates": 1, "rescheduled": 1}
    assert athena.observed == []


def test_a_parked_nonterminal_past_the_cancel_due_is_cancelled_stopped_and_rescheduled() -> None:
    world = World()
    plan_key = world.bootstrap_plan("plan", ("fictional_db.alpha",))
    plan_digest = str(world.dynamo.items[(PUBLICATIONS, plan_key, "META")]["plan_digest"]["S"])
    writer = world.claim_and_lease_unit(plan_digest, plan_key, "fictional_db.alpha")
    begun = world.begin_queued(plan_key, "fictional_db.alpha", writer)
    world.record("fictional_db.alpha", begun, "fictional-query-0003")
    # Active + nonterminal at/after lease expiry: cancellation, stop, then park.
    athena = _Athena(observations=[ProviderNonterminal(reason="nonterminal")])
    first = _reconciler(world, athena, now=_far_future(world)).run_once()
    assert first == {"candidates": 1, "parked": 1}
    assert athena.stopped == ["fictional-query-0003"]
    attempt = world.reader.attempt("fictional_db.alpha", begun.attempt_id)
    assert attempt is not None and attempt.cancel_requested_at is not None
    assert attempt.state == "parked"

    # Parked + nonterminal well past submitted_at + 780 s: already cancelled, so only reschedule.
    athena = _Athena(observations=[ProviderNonterminal(reason="nonterminal")])
    second = _reconciler(world, athena, now=_far_future(world) + 2_000_000_000).run_once()
    assert second == {"candidates": 1, "rescheduled": 1}
    assert athena.stopped == []


def test_a_parked_attempt_reaching_its_terminal_is_completed_and_its_lease_released() -> None:
    world = World()
    plan_key = world.bootstrap_plan("plan", ("fictional_db.alpha",))
    plan_digest = str(world.dynamo.items[(PUBLICATIONS, plan_key, "META")]["plan_digest"]["S"])
    writer = world.claim_and_lease_unit(plan_digest, plan_key, "fictional_db.alpha")
    begun = world.begin_queued(plan_key, "fictional_db.alpha", writer)
    world.record("fictional_db.alpha", begun, "fictional-query-0004")
    _reconciler(
        world,
        _Athena(observations=[ProviderNonterminal(reason="nonterminal")]),
        now=_far_future(world),
    ).run_once()
    cancelled = ProviderTerminalObservation(ProviderTerminalState.CANCELLED, 1, None, None)

    counters = _reconciler(
        world, _Athena(observations=[cancelled]), now=_far_future(world) + 1_000_000_000
    ).run_once()

    assert counters == {"candidates": 1, "completed": 1}
    lease = world.reader.lease("fictional_db.alpha")
    assert lease is not None and lease.parked_at is None
    assert lease.expires_at == "1970-01-01T00:00:00+00:00"
    fence = world.reader.fence("fictional_db.alpha")
    assert fence is not None and fence.state == "clear"


def test_an_inline_run_completion_advances_the_run_to_reconciled_with_its_event() -> None:
    world = World()
    run_id, execution = world.open_inline_run("fictional_db.beta", "one")
    begun = world.begin_inline("fictional_db.beta", run_id, execution)
    world.record("fictional_db.beta", begun, "fictional-query-0005")
    heads_before = len([k for k in world.dynamo.items if k[2] == "HEAD"])

    counters = _reconciler(
        world, _Athena(observations=[_SUCCEEDED]), now=_far_future(world)
    ).run_once()

    assert counters == {"candidates": 1, "completed": 1}
    run = world.reader.run_item("fictional_db.beta", run_id)
    assert run is not None and run["state"] == "reconciled"
    assert run.get("current_attempt_id") is None
    events = [k for k in world.dynamo.items if k[2].startswith("EVENT#")]
    assert len([k for k in world.dynamo.items if k[2] == "HEAD"]) == heads_before
    assert any("STATUS_FLOW#" in k[1] for k in events)


def test_repairable_reconciliation_metadata_is_typed_blocked_without_a_provider_call() -> None:
    world = World()
    plan_key = world.bootstrap_plan("plan", ("fictional_db.alpha",))
    plan_digest = str(world.dynamo.items[(PUBLICATIONS, plan_key, "META")]["plan_digest"]["S"])
    writer = world.claim_and_lease_unit(plan_digest, plan_key, "fictional_db.alpha")
    world.begin_queued(plan_key, "fictional_db.alpha", writer)
    fence_item = world.dynamo.items[(PUBLICATIONS, "TABLE_ATTEMPT#fictional_db.alpha", "FENCE")]
    fence_item["reconcile_attempt_count"] = {"BOOL": True}
    athena = _Athena()

    counters = _reconciler(world, athena, now=_far_future(world)).run_once()

    assert counters == {"candidates": 1, "blocked": 1}
    assert athena.observed == [] and athena.started == []
    fence = world.reader.fence("fictional_db.alpha")
    assert fence is not None and fence.reconciliation.reconcile_status == "blocked"
    assert fence.reconciliation.reconcile_block_reason == "MALFORMED_RECONCILIATION_METADATA"
    cases = [k for k in world.dynamo.items if k[1].startswith("PROVIDER_RECONCILE_BLOCK#")]
    assert len(cases) == 1


def test_a_core_invalid_candidate_is_the_hard_failure_with_zero_mutation() -> None:
    world = World()
    plan_key = world.bootstrap_plan("plan", ("fictional_db.alpha",))
    plan_digest = str(world.dynamo.items[(PUBLICATIONS, plan_key, "META")]["plan_digest"]["S"])
    writer = world.claim_and_lease_unit(plan_digest, plan_key, "fictional_db.alpha")
    world.begin_queued(plan_key, "fictional_db.alpha", writer)
    fence_item = world.dynamo.items[(PUBLICATIONS, "TABLE_ATTEMPT#fictional_db.alpha", "FENCE")]
    fence_item["lease_holder"] = {"S": "someone-else"}
    transactions = len(world.dynamo.transacts)
    athena = _Athena()

    with pytest.raises(InvariantError) as caught:
        _reconciler(world, athena, now=_far_future(world)).run_once()

    assert caught.value.code == CORRUPT_PROVIDER_CORE_AUTHORITY
    assert len(world.dynamo.transacts) == transactions
    assert athena.observed == [] and athena.started == [] and athena.stopped == []


def test_a_clear_fence_left_in_the_index_is_a_no_op() -> None:
    world = World()
    plan_key = world.bootstrap_plan("plan", ("fictional_db.alpha",))
    plan_digest = str(world.dynamo.items[(PUBLICATIONS, plan_key, "META")]["plan_digest"]["S"])
    writer = world.claim_and_lease_unit(plan_digest, plan_key, "fictional_db.alpha")
    begun = world.begin_queued(plan_key, "fictional_db.alpha", writer)
    world.record("fictional_db.alpha", begun, "fictional-query-0006")
    _reconciler(world, _Athena(observations=[_SUCCEEDED]), now=_far_future(world)).run_once()
    # The naive fake keeps the cleared fence's index attributes until the next real write; the
    # eventually consistent GSI can do the same on the service.
    fence_item = world.dynamo.items[(PUBLICATIONS, "TABLE_ATTEMPT#fictional_db.alpha", "FENCE")]
    fence_item["reconcile_status"] = {"S": "pending"}
    fence_item["reconcile_after_or_block"] = {"S": f"{0:020d}#stale"}
    stored_state = fence_item["state"]

    counters = _reconciler(world, _Athena(), now=_far_future(world)).run_once()

    assert counters == {"candidates": 1, "clear_noop": 1}
    assert fence_item["state"] == stored_state


# --- ProviderFenceRecoveryFunction ----------------------------------------------------------------


def _blocked_world() -> tuple[World, str, str]:
    world = World()
    plan_key = world.bootstrap_plan("plan", ("fictional_db.alpha",))
    plan_digest = str(world.dynamo.items[(PUBLICATIONS, plan_key, "META")]["plan_digest"]["S"])
    writer = world.claim_and_lease_unit(plan_digest, plan_key, "fictional_db.alpha")
    begun = world.begin_queued(plan_key, "fictional_db.alpha", writer)
    fence = world.reader.fence("fictional_db.alpha")
    attempt = world.reader.attempt("fictional_db.alpha", begun.attempt_id)
    assert fence is not None and attempt is not None
    owner = world.reader.attempt_owner(attempt)
    assert owner is not None
    blocked = world.coordinator.block_table_attempt_reconcile(
        BlockTableAttemptReconcileRequest(
            "fictional_db.alpha",
            fence,
            attempt,
            owner,
            SameSnapshotRescheduleRejectedObservation(fence.reconciliation),
            world.instant(),
        )
    )
    return world, blocked.recovery_ref, begun.attempt_id


def _broker(world: World) -> ProviderFenceRecovery:
    return ProviderFenceRecovery(
        discovery=world.discovery,
        reader=world.reader,
        attempts=world.attempts,
        coordinator=world.coordinator,
        now=world.clock.instant,
    )


def _assert_recovery_dependency_report(
    monkeypatch: pytest.MonkeyPatch,
    broker: ProviderFenceRecovery,
    event: dict[str, object],
) -> None:
    from adapters.jobs import provider_fence_recovery as module  # noqa: PLC0415

    reported = _captured_errors(monkeypatch, module)
    monkeypatch.setattr(module, "_broker", lambda: broker)
    with pytest.raises(ProviderFenceRecoveryError) as raised:
        inspect.unwrap(module.handler)(event, object())

    assert raised.value.recovery_code is ProviderFenceRecoveryErrorCode.DEPENDENCY_FAILURE
    assert str(raised.value).endswith("dependency_failure")
    dependency = raised.value.__cause__
    assert isinstance(dependency, DependencyError)
    _assert_dependency_report(reported, dependency, "stage1_provider_fence.recovery")


def test_a_list_dependency_keeps_the_closed_result_and_reports_dependency_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World()
    failure = DependencyError(ErrorCode("TEST", 1), "discovery unavailable")

    def fail_discovery(_query: object) -> Any:
        raise failure

    monkeypatch.setattr(world.discovery, "blocked_fences", fail_discovery)
    _assert_recovery_dependency_report(
        monkeypatch,
        _broker(world),
        {"schema_version": "1", "mode": "list", "limit": 10},
    )


def test_a_recovery_read_dependency_keeps_the_closed_result_and_reports_dependency_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, recovery_ref, _attempt_id = _blocked_world()
    failure = DependencyError(ErrorCode("TEST", 2), "case read unavailable")

    def fail_read(_recovery_ref: str) -> Any:
        raise failure

    monkeypatch.setattr(world.attempts, "read_protected_case", fail_read)
    _assert_recovery_dependency_report(
        monkeypatch,
        _broker(world),
        {
            "schema_version": "1",
            "mode": "requeue",
            "recovery_ref": recovery_ref,
            "operation_id": "fictional-recovery-op-read",
            "resolution_reason": "fixture resolution",
        },
    )


def test_a_requeue_dependency_keeps_the_closed_result_and_reports_dependency_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, recovery_ref, _attempt_id = _blocked_world()
    failure = DependencyError(ErrorCode("TEST", 3), "requeue unavailable")

    def fail_requeue(_request: object) -> Any:
        raise failure

    monkeypatch.setattr(
        world.coordinator,
        "requeue_blocked_table_attempt_reconcile",
        fail_requeue,
    )
    _assert_recovery_dependency_report(
        monkeypatch,
        _broker(world),
        {
            "schema_version": "1",
            "mode": "requeue",
            "recovery_ref": recovery_ref,
            "operation_id": "fictional-recovery-op-write",
            "resolution_reason": "fixture resolution",
        },
    )


def test_list_resolves_blocked_fences_to_opaque_case_summaries_only() -> None:
    world, recovery_ref, _attempt_id = _blocked_world()

    result = recover({"schema_version": "1", "mode": "list", "limit": 10}, _broker(world))

    assert result == {
        "schema_version": "1",
        "mode": "list",
        "cases": [
            {
                "recovery_ref": recovery_ref,
                "safe_reason": "SAME_SNAPSHOT_RESCHEDULE_REJECTED",
                "blocked_at": cast("list[dict[str, Any]]", result["cases"])[0]["blocked_at"],
            }
        ],
    }
    paged = recover({"schema_version": "1", "mode": "list", "limit": 1}, _broker(world))
    assert paged["next_cursor"] == recovery_ref
    after = recover(
        {"schema_version": "1", "mode": "list", "limit": 1, "cursor": recovery_ref}, _broker(world)
    )
    assert after["cases"] == [] and "next_cursor" not in after


def test_requeue_returns_the_case_to_canonical_pending_and_replays_as_already_requeued() -> None:
    world, recovery_ref, _attempt_id = _blocked_world()
    invocation = {
        "schema_version": "1",
        "mode": "requeue",
        "recovery_ref": recovery_ref,
        "operation_id": "fictional-recovery-op-0001",
        "resolution_reason": "fixture resolution",
    }

    first = recover(invocation, _broker(world))
    second = recover(invocation, _broker(world))

    assert first == {
        "schema_version": "1",
        "mode": "requeue",
        "recovery_ref": recovery_ref,
        "outcome": "requeued",
    }
    assert second["outcome"] == "already_requeued"
    fence = world.reader.fence("fictional_db.alpha")
    assert fence is not None
    assert (
        fence.reconciliation.reconcile_status,
        fence.reconciliation.reconcile_attempt_count,
    ) == (
        "pending",
        0,
    )
    assert fence.reconciliation.requeued_from_case_ref == recovery_ref
    with pytest.raises(ProviderFenceRecoveryError) as stale:
        recover({**invocation, "operation_id": "fictional-recovery-op-0002"}, _broker(world))
    assert stale.value.recovery_code is ProviderFenceRecoveryErrorCode.STALE_CASE


def test_recovery_failures_carry_exactly_one_closed_code() -> None:
    world, _recovery_ref, _attempt_id = _blocked_world()
    with pytest.raises(ProviderFenceRecoveryError) as missing:
        recover(
            {
                "schema_version": "1",
                "mode": "requeue",
                "recovery_ref": "00000000-0000-4000-8000-000000000000",
                "operation_id": "op",
                "resolution_reason": "reason",
            },
            _broker(world),
        )
    assert missing.value.recovery_code is ProviderFenceRecoveryErrorCode.CASE_NOT_FOUND
    assert str(missing.value).endswith("case_not_found")
    for invalid in (
        {"schema_version": "1", "mode": "list", "limit": 0},
        {"schema_version": "1", "mode": "list", "limit": 10, "extra": 1},
        {"schema_version": "2", "mode": "list", "limit": 10},
        {"schema_version": "1", "mode": "repair", "recovery_ref": "x"},
        {
            "schema_version": "1",
            "mode": "requeue",
            "recovery_ref": "not-a-uuid",
            "operation_id": "o",
            "resolution_reason": "r",
        },
        "not an object",
    ):
        with pytest.raises(ProviderFenceRecoveryError) as rejected:
            parse_invocation(invalid)
        assert rejected.value.recovery_code is ProviderFenceRecoveryErrorCode.INVALID_INVOCATION


# --- PlanIntentReconciler -------------------------------------------------------------------------


@dataclass
class _Sender(WriteUnitSender):
    sent: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])

    def send(self, body: str, group_id: str, deduplication_id: str) -> None:
        self.sent.append((body, group_id, deduplication_id))


def _intent_reconciler(  # noqa: PLR0913 -- the test factory's optional mode read
    world: World, sender: _Sender, *, now: int, mode: AuthorityMode | None = None
) -> PlanIntentReconciler:
    return PlanIntentReconciler(
        discovery=world.discovery,
        reader=world.reader,
        coordinator=world.coordinator,
        sender=sender,
        batch_size=10,
        max_units=15,
        now=lambda: f"{now:020d}",
        mode=None if mode is None else (lambda: mode),
    )


def test_a_due_pending_intent_is_claimed_persisted_sent_and_admitted_in_one_tick() -> None:
    world = World()
    plan_digest, intent_id = world.submit_intent(
        "plan", ("fictional_db.alpha", "fictional_db.beta")
    )
    sender = _Sender()

    counters = _intent_reconciler(world, sender, now=_far_future(world)).run_once()

    assert counters == {"candidates": 1, "admitted": 2, "claimed": 1, "persisted": 1, "sent": 2}
    intent = world.reader.intent(plan_digest, intent_id)
    assert intent is not None and intent.state.value == "admitted"
    owner = world.reader.owner(plan_digest)
    assert owner is not None and owner.active_plan_key is not None
    meta = world.reader.plan_meta(owner.active_plan_key)
    assert meta is not None and (meta.unit_count, meta.admitted_count, meta.generation) == (2, 2, 0)
    assert [group for _, group, _ in sender.sent] == ["fictional_db.alpha", "fictional_db.beta"]
    body = sender.sent[0][0]
    assert '"plan_key":"' + owner.active_plan_key + '"' in body
    assert '"generation":0' in body
    assert world.plan_head(plan_digest, meta.internal_execution_id).latest_state.value == "queued"


def test_a_second_delivery_while_the_first_plan_is_active_performs_the_replacement() -> None:
    world = World()
    plan_key = world.bootstrap_plan("plan", ("fictional_db.alpha",))
    plan_digest = str(world.dynamo.items[(PUBLICATIONS, plan_key, "META")]["plan_digest"]["S"])
    # The same source identity redelivered with a different roster is a second pending intent.
    _digest, intent_id = world.submit_intent("plan", ("fictional_db.alpha", "fictional_db.gamma"))
    sender = _Sender()

    counters = _intent_reconciler(world, sender, now=_far_future(world)).run_once()

    assert counters == {"candidates": 1, "admitted": 2, "claimed": 1, "persisted": 1, "sent": 2}
    source = world.reader.plan_meta(plan_key)
    assert source is not None and source.status.value == "superseded"
    owner = world.reader.owner(plan_digest)
    assert owner is not None and owner.last_generation == 1
    target = world.reader.plan_meta(owner.active_plan_key or "")
    assert target is not None and (target.generation, target.origin_intent_id) == (1, intent_id)
    head = world.plan_head(plan_digest, target.internal_execution_id)
    assert head.predecessor_execution_id is not None


@pytest.mark.parametrize("failure_point", ["assemble", "persist"])
def test_a_plan_persistence_dependency_is_reported_once_before_reschedule(
    monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    from adapters.jobs import plan_intent_reconciler as module  # noqa: PLC0415

    reported = _captured_errors(monkeypatch, module)
    world = World()
    plan_digest, intent_id = world.submit_intent("plan", ("fictional_db.alpha",))
    failure = DependencyError(ErrorCode("TEST", 1), f"{failure_point} unavailable")
    reconciler = _intent_reconciler(world, _Sender(), now=_far_future(world))

    def fail(*_args: object, **_kwargs: object) -> Any:
        raise failure

    if failure_point == "assemble":
        monkeypatch.setattr(reconciler, "_persist_request", fail)
    else:
        monkeypatch.setattr(world.coordinator, "persist_plan", fail)

    counters = reconciler.run_once()

    assert counters == {"candidates": 1, "claimed": 1, "rescheduled": 1}
    intent = world.reader.intent(plan_digest, intent_id)
    assert intent is not None and intent.state.value == "pending"
    _assert_dependency_report(reported, failure, "stage1_plan_intent.reconcile")


def test_a_transient_send_failure_leaves_the_intent_claimed_for_the_next_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters.jobs import plan_intent_reconciler as module  # noqa: PLC0415

    reported = _captured_errors(monkeypatch, module)
    world = World()
    plan_digest, intent_id = world.submit_intent("plan", ("fictional_db.alpha",))
    failure = DependencyError(ErrorCode("TEST", 1), "queue unavailable")

    class _Failing(_Sender):
        def send(self, body: str, group_id: str, deduplication_id: str) -> None:
            del body, group_id, deduplication_id
            raise failure

    counters = _intent_reconciler(world, _Failing(), now=_far_future(world)).run_once()

    assert counters == {"candidates": 1, "claimed": 1, "persisted": 1, "rescheduled": 1}
    intent = world.reader.intent(plan_digest, intent_id)
    assert intent is not None and intent.state.value == "claimed"
    # The next tick finds nothing ready (a claimed intent has no ready fields) ...
    assert _intent_reconciler(world, _Sender(), now=_far_future(world)).run_once() == {
        "candidates": 0
    }
    # ... but a later intent for the same digest resumes the partial admission first.
    world.submit_intent("plan", ("fictional_db.alpha", "fictional_db.delta"))
    sender = _Sender()
    resumed = _intent_reconciler(world, sender, now=_far_future(world)).run_once()
    assert resumed == {"candidates": 1, "admitted": 1, "sent": 1}
    intent = world.reader.intent(plan_digest, intent_id)
    assert intent is not None and intent.state.value == "admitted"
    _assert_dependency_report(reported, failure, "stage1_plan_intent.reconcile")


def _evidence(status: int = 200) -> SanitizedStartEvidence:
    return SanitizedStartEvidence(
        status, _EVIDENCE_NONE if status == 200 else ProviderErrorClass.THROTTLING
    )


# --- C2: one admission lifecycle record per reconciled intent ----------------------------------


def _captured_admissions(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from adapters.jobs import plan_intent_reconciler as module  # noqa: PLC0415

    logged: list[Any] = []

    def capture(log: Any, _logger: Any) -> None:
        if log.phase == "intent":
            logged.append(log)

    monkeypatch.setattr(module, "report_operation", capture)
    return logged


def test_a_reconciled_intent_writes_its_admission_record_with_exact_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain.operations.stage1_operational_status import (  # noqa: PLC0415
        PlanIdentityRequest,
        derive_plan_identity,
    )

    logged = _captured_admissions(monkeypatch)
    world = World()
    plan_digest, intent_id = world.submit_intent(
        "plan", ("fictional_db.alpha", "fictional_db.beta")
    )
    ticks = iter([10.0, 10.004, 10.010, 10.0125])  # send, admit, send, admit
    reconciler = _intent_reconciler(world, _Sender(), now=_far_future(world))
    reconciler._clock = lambda: next(ticks)  # pyright: ignore[reportPrivateUsage]
    reconciler.run_once()
    owner = world.reader.owner(plan_digest)
    assert owner is not None and owner.active_plan_key is not None
    meta = world.reader.plan_meta(owner.active_plan_key)
    assert meta is not None
    [record] = logged
    assert (record.operation, record.component, record.outcome) == (
        "stage1_plan_intent.reconcile",
        "plan_intent_reconciler",
        "admitted",
    )
    assert record.details == {
        "plan_digest": plan_digest,
        "intent_id": intent_id,
        "flow_id": derive_plan_identity(
            PlanIdentityRequest(plan_digest, meta.internal_execution_id)
        ).flow_id,
        "units_sent": 2,
        "units_admitted": 2,
        "send_to_last_admit_ms": 12.5,
    }


def test_a_failed_send_still_writes_the_record_with_nothing_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Positive control: the record reports what happened, not what was planned.
    logged = _captured_admissions(monkeypatch)
    world = World()
    _plan_digest, intent_id = world.submit_intent("plan", ("fictional_db.alpha",))

    class _Failing(_Sender):
        def send(self, body: str, group_id: str, deduplication_id: str) -> None:
            del body, group_id, deduplication_id
            raise DependencyError(ErrorCode("TEST", 1), "queue unavailable")

    _intent_reconciler(world, _Failing(), now=_far_future(world)).run_once()
    [record] = logged
    assert record.outcome == "rescheduled"
    assert {key: record.details[key] for key in ("intent_id", "units_sent", "units_admitted")} == {
        "intent_id": intent_id,
        "units_sent": 0,
        "units_admitted": 0,
    }
    assert record.details["send_to_last_admit_ms"] == "none"


# --- S6 (V-17b): the reconciler's drain counter ---------------------------------------------------


def _drain_members(world: World) -> list[int]:
    """Per transaction, how many members add to DRAIN_SEQ (built-transaction assertion)."""
    return [
        sum(
            1
            for member in cast("list[dict[str, Any]]", request["TransactItems"])
            if member.get("Update", {}).get("Key", {}).get("partition_key", {}).get("S")
            == DRAIN_SEQ_PARTITION_KEY
        )
        for request in world.dynamo.transacts
    ]


def test_v17b_a_draining_reconciler_counts_every_family_changing_transaction() -> None:
    """Count every transaction after a LEGACY_DRAINING read.

    Claim, persist and admit each add 1 to DRAIN_SEQ, and the reconciler still finishes the
    pre-CAS intent: claim and persist are never mode-gated.
    """
    world = World()
    world.submit_intent("plan", ("fictional_db.alpha",))
    before = len(world.dynamo.transacts)
    counters = _intent_reconciler(
        world, _Sender(), now=_far_future(world), mode=AuthorityMode.LEGACY_DRAINING
    ).run_once()
    assert counters["admitted"] == 1
    counted = _drain_members(world)[before:]
    assert counted and all(count == 1 for count in counted)
    seq = world.dynamo.items[(PUBLICATIONS, DRAIN_SEQ_PARTITION_KEY, DRAIN_SEQ_RECORD_KEY)]
    assert int(seq["seq"]["N"]) == len(counted)


@pytest.mark.parametrize("mode", [None, AuthorityMode.LEGACY, AuthorityMode.SHADOW])
def test_v17b_positive_a_legacy_reconciler_builds_no_drain_member(
    mode: AuthorityMode | None,
) -> None:
    world = World()
    world.submit_intent("plan", ("fictional_db.alpha",))
    _intent_reconciler(world, _Sender(), now=_far_future(world), mode=mode).run_once()
    assert all(count == 0 for count in _drain_members(world))
