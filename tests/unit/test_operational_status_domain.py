# SPDX-License-Identifier: MIT-0
"""Closed operational data-flow event validation, reduction, mapping, and baselines."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from operational_status_support import (
    BINDING,
    DIGEST,
    NOW,
    SURFACE,
    attempt_event,
    flow_event,
    partition,
    terminal_event,
    uuid_text,
)

from domain.operations.status import (
    MEASUREMENT_REFS,
    UUID_NAMESPACE,
    AuthorityObservation,
    BaselineFoldRequest,
    CompletionOutcome,
    EvaluationBinding,
    FlowSnapshot,
    FlowState,
    OperationalStatusError,
    Progress,
    ReductionKind,
    canonical_authority_digest,
    canonical_uuid,
    corrected_baseline,
    derived_uuid,
    event_document,
    event_from_document,
    initial_baseline,
    map_attempt_history,
    reduce_event,
    window_sample_count,
)
from domain.semantics.publication import AttemptEventKind, AttemptId

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-073", "ADR-074")]

LATER = NOW + timedelta(minutes=5)


def _rich_event() -> Any:
    return flow_event(
        parent_flow_id=uuid_text(20),
        causation_event_id=uuid_text(21),
        clock_started_at=NOW - timedelta(minutes=1),
        required_child_flow_ids=(uuid_text(30), uuid_text(31)),
        optional_child_flow_ids=(uuid_text(40),),
        progress=Progress(1, 4, "candidates"),
        reason_code="candidate_skipped",
    )


class TestEventValidation:
    def test_terminal_state_requires_a_completion_outcome(self) -> None:
        with pytest.raises(OperationalStatusError, match="requires a completion outcome"):
            flow_event(state=FlowState.COMPLETED)

    def test_nonterminal_state_cannot_carry_an_outcome(self) -> None:
        with pytest.raises(OperationalStatusError, match="cannot carry an outcome"):
            flow_event(outcome=CompletionOutcome.SUCCEEDED)

    def test_a_naive_transition_instant_is_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="timezone-aware"):
            flow_event(occurred_at=datetime(2026, 8, 19, 12))  # noqa: DTZ001 — deliberate

    def test_the_flow_clock_cannot_start_after_a_transition(self) -> None:
        with pytest.raises(OperationalStatusError, match="cannot start after"):
            flow_event(clock_started_at=NOW + timedelta(seconds=1))

    def test_unsorted_children_are_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="bounded, unique, and sorted"):
            flow_event(required_child_flow_ids=(uuid_text(31), uuid_text(30)))

    def test_duplicate_children_are_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="bounded, unique, and sorted"):
            flow_event(optional_child_flow_ids=(uuid_text(30), uuid_text(30)))

    def test_more_than_the_child_bound_is_rejected(self) -> None:
        children = tuple(sorted(uuid_text(number) for number in range(101)))
        with pytest.raises(OperationalStatusError, match="bounded, unique, and sorted"):
            flow_event(required_child_flow_ids=children)

    def test_a_child_cannot_be_both_required_and_optional(self) -> None:
        with pytest.raises(OperationalStatusError, match="both required and optional"):
            flow_event(
                required_child_flow_ids=(uuid_text(30),),
                optional_child_flow_ids=(uuid_text(30),),
            )

    def test_a_non_canonical_event_id_is_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="lowercase canonical"):
            flow_event(event_id=uuid_text(0xDEADBEEF).upper())

    def test_an_unregistered_surface_is_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="not registered"):
            flow_event(surface="fictional_surface")

    def test_an_inactive_contract_version_is_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="not active"):
            flow_event(schema_version="2")
        with pytest.raises(OperationalStatusError, match="not active"):
            flow_event(registry_version="0")

    def test_a_blank_stage_is_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="stage must be nonblank"):
            flow_event(stage="  ")

    def test_the_root_transition_cannot_name_a_predecessor_event(self) -> None:
        with pytest.raises(OperationalStatusError, match="root transition"):
            flow_event(predecessor_event_id=uuid_text(9))

    def test_a_non_root_transition_requires_its_predecessor_event(self) -> None:
        with pytest.raises(OperationalStatusError, match="requires its predecessor"):
            flow_event(transition_sequence=2)

    def test_the_transition_sequence_must_be_positive(self) -> None:
        with pytest.raises(OperationalStatusError, match="must be positive"):
            flow_event(transition_sequence=0)

    def test_the_measurement_binding_must_match_the_registry(self) -> None:
        with pytest.raises(OperationalStatusError, match="differs from the registry"):
            flow_event(evaluation_binding=EvaluationBinding((("fictional_measure", "1"),)))

    def test_progress_outside_its_bound_is_rejected(self) -> None:
        for completed, total in ((-1, 4), (0, 0), (5, 4)):
            with pytest.raises(OperationalStatusError, match="outside its declared bound"):
                Progress(completed, total, "candidates")
        with pytest.raises(OperationalStatusError, match="unit must be nonblank"):
            Progress(1, 4, " ")

    def test_authority_evidence_must_be_a_canonical_utc_observation(self) -> None:
        with pytest.raises(OperationalStatusError, match="kind must be nonblank"):
            AuthorityObservation(" ", DIGEST, NOW)
        with pytest.raises(OperationalStatusError, match="canonical lowercase sha256"):
            AuthorityObservation("semantic_attempt", DIGEST.upper(), NOW)
        with pytest.raises(OperationalStatusError, match="canonical lowercase sha256"):
            AuthorityObservation("semantic_attempt", DIGEST[:32], NOW)
        with pytest.raises(OperationalStatusError, match="timezone-aware"):
            AuthorityObservation("semantic_attempt", DIGEST, datetime(2026, 8, 19))  # noqa: DTZ001

    def test_the_binding_is_observation_only_with_unique_versioned_references(self) -> None:
        with pytest.raises(OperationalStatusError, match="observation_only"):
            EvaluationBinding((), mode="evaluated")
        with pytest.raises(OperationalStatusError, match="must be unique"):
            EvaluationBinding((("measure_a", "1"), ("measure_a", "1")))
        with pytest.raises(OperationalStatusError, match="must be versioned"):
            EvaluationBinding((("measure_a", ""),))


class TestEventDocument:
    def test_a_fully_populated_event_round_trips_exactly(self) -> None:
        event = _rich_event()

        assert event_from_document(event_document(event)) == event

    def test_an_extra_field_is_rejected(self) -> None:
        document = {**event_document(flow_event()), "extra": 1}

        with pytest.raises(OperationalStatusError, match="differ from the closed v1"):
            event_from_document(document)

    def test_a_missing_field_is_rejected(self) -> None:
        document = event_document(flow_event())
        del document["stage"]

        with pytest.raises(OperationalStatusError, match="differ from the closed v1"):
            event_from_document(document)

    def test_a_non_object_document_is_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="must be an object"):
            event_from_document("not-a-document")

    def test_nested_attainment_like_fields_are_rejected(self) -> None:
        """A forbidden name inside any nested value fails before shape parsing."""
        document = event_document(_rich_event())
        document["progress"] = {"completed": 1, "total": 4, "unit": "candidates", "objective": 1}

        with pytest.raises(OperationalStatusError, match="attainment fields are forbidden"):
            event_from_document(document)

    def test_a_target_like_measurement_reference_is_rejected(self) -> None:
        document = event_document(flow_event())
        binding = cast("dict[str, Any]", document["evaluation_binding"])
        binding["measurement_refs"] = [
            {"measurement_ref": "measure_a", "measurement_version": "1", "target_value": 2}
        ]

        with pytest.raises(OperationalStatusError, match="attainment fields are forbidden"):
            event_from_document(document)


class TestReduceEvent:
    def test_a_root_event_applies_and_projects_its_snapshot(self) -> None:
        event = flow_event()

        decision = reduce_event(None, event)

        assert decision.kind is ReductionKind.APPLY
        assert decision.snapshot == FlowSnapshot.from_event(event)
        assert decision.snapshot is not None
        assert decision.snapshot.latest_sequence == 1

    def test_a_root_execution_with_a_predecessor_execution_conflicts(self) -> None:
        event = flow_event(predecessor_execution_id=uuid_text(9))

        decision = reduce_event(None, event)

        assert decision.kind is ReductionKind.CONFLICT
        assert decision.reason == "root execution has predecessor"

    def test_a_successor_without_its_root_parks(self) -> None:
        orphan = flow_event(transition_sequence=2, predecessor_event_id=uuid_text(9))

        decision = reduce_event(None, orphan)

        assert decision.kind is ReductionKind.PARK
        assert decision.reason == "predecessor is not applied"

    def test_a_sequence_gap_after_the_snapshot_parks(self) -> None:
        snapshot = FlowSnapshot.from_event(flow_event())
        gapped = flow_event(
            event_id=uuid_text(4), transition_sequence=3, predecessor_event_id=uuid_text(9)
        )

        assert reduce_event(snapshot, gapped).kind is ReductionKind.PARK

    def test_the_same_event_again_is_already_applied(self) -> None:
        event = flow_event()
        snapshot = FlowSnapshot.from_event(event)

        assert reduce_event(snapshot, event).kind is ReductionKind.ALREADY_APPLIED

    def test_a_different_event_in_an_owned_slot_conflicts(self) -> None:
        snapshot = FlowSnapshot.from_event(flow_event())
        rival = flow_event(event_id=uuid_text(4))

        decision = reduce_event(snapshot, rival)

        assert decision.kind is ReductionKind.CONFLICT
        assert decision.reason == "history slot is already owned"

    def test_a_changed_flow_identity_conflicts(self) -> None:
        snapshot = FlowSnapshot.from_event(flow_event())
        stranger = _successor(flow_id=uuid_text(9))

        decision = reduce_event(snapshot, stranger)

        assert decision.kind is ReductionKind.CONFLICT
        assert decision.reason == "flow identity or surface changed"

    def test_a_broken_predecessor_fence_conflicts(self) -> None:
        snapshot = FlowSnapshot.from_event(flow_event())
        forged = _successor(predecessor_event_id=uuid_text(9))

        decision = reduce_event(snapshot, forged)

        assert decision.kind is ReductionKind.CONFLICT
        assert decision.reason == "predecessor event fence differs"

    def test_a_changed_child_set_conflicts(self) -> None:
        snapshot = FlowSnapshot.from_event(flow_event(required_child_flow_ids=(uuid_text(30),)))
        rewritten = _successor()

        decision = reduce_event(snapshot, rewritten)

        assert decision.kind is ReductionKind.CONFLICT
        assert decision.reason == "registered child set changed"

    def test_a_changed_flow_clock_conflicts(self) -> None:
        snapshot = FlowSnapshot.from_event(flow_event(clock_started_at=NOW))
        reclocked = _successor()

        decision = reduce_event(snapshot, reclocked)

        assert decision.kind is ReductionKind.CONFLICT
        assert decision.reason == "flow clock changed"

    def test_a_changed_evaluation_binding_conflicts(self) -> None:
        """A snapshot whose binding drifted must refuse the next event, not absorb it."""
        drifted = replace(
            FlowSnapshot.from_event(flow_event()),
            evaluation_binding=EvaluationBinding((("fictional_measure", "1"),)),
        )

        decision = reduce_event(drifted, _successor())

        assert decision.kind is ReductionKind.CONFLICT
        assert decision.reason == "evaluation binding changed"

    def test_a_changed_execution_lineage_conflicts(self) -> None:
        snapshot = FlowSnapshot.from_event(flow_event())
        relinked = _successor(predecessor_execution_id=uuid_text(9))

        decision = reduce_event(snapshot, relinked)

        assert decision.kind is ReductionKind.CONFLICT
        assert decision.reason == "execution lineage changed"

    def test_a_retry_that_skips_its_predecessor_execution_conflicts(self) -> None:
        snapshot = FlowSnapshot.from_event(flow_event())
        skipped = _successor(execution_id=uuid_text(5), predecessor_execution_id=uuid_text(9))

        decision = reduce_event(snapshot, skipped)

        assert decision.kind is ReductionKind.CONFLICT
        assert decision.reason == "retry skipped its predecessor execution"

    def test_a_retry_naming_the_prior_execution_applies(self) -> None:
        snapshot = FlowSnapshot.from_event(terminal_event())
        retry = _successor(execution_id=uuid_text(5), predecessor_execution_id=uuid_text(3))

        assert reduce_event(snapshot, retry).kind is ReductionKind.APPLY

    def test_progress_after_a_terminal_state_conflicts(self) -> None:
        snapshot = FlowSnapshot.from_event(terminal_event())
        late = _successor(progress=Progress(1, 4, "candidates"))

        decision = reduce_event(snapshot, late)

        assert decision.kind is ReductionKind.CONFLICT
        assert decision.reason == "progress followed terminal state"


def _successor(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "event_id": uuid_text(4),
        "transition_sequence": 2,
        "predecessor_event_id": uuid_text(1),
        "occurred_at": LATER,
        **overrides,
    }
    return flow_event(**values)


class TestMapAttemptHistory:
    def test_a_single_started_attempt_maps_to_one_running_root(self) -> None:
        history = (attempt_event("attempt-a", AttemptEventKind.STARTED),)

        (mapped,) = map_attempt_history(history)

        assert mapped.transition_sequence == 1
        assert mapped.state is FlowState.RUNNING
        assert mapped.outcome is None
        assert mapped.surface == SURFACE
        assert mapped.stage == "candidate_started"
        assert mapped.clock_started_at == mapped.occurred_at == NOW
        assert mapped.evaluation_binding == BINDING
        assert uuid.UUID(mapped.flow_id).version == 5

    def test_a_started_then_succeeded_attempt_maps_to_a_completed_pair(self) -> None:
        history = (
            attempt_event("attempt-a", AttemptEventKind.STARTED),
            attempt_event("attempt-a", AttemptEventKind.SUCCEEDED, occurred_at=LATER),
        )

        started, succeeded = map_attempt_history(history)

        assert succeeded.transition_sequence == 2
        assert succeeded.predecessor_event_id == started.event_id
        assert succeeded.execution_id == started.execution_id
        assert succeeded.state is FlowState.COMPLETED
        assert succeeded.outcome is CompletionOutcome.SUCCEEDED
        assert succeeded.stage == "candidate_published"
        assert succeeded.reason_code is None

    def test_a_quarantined_attempt_maps_to_a_blocked_refusal(self) -> None:
        history = (
            attempt_event("attempt-a", AttemptEventKind.STARTED),
            attempt_event("attempt-a", AttemptEventKind.QUARANTINED, occurred_at=LATER),
        )

        _, quarantined = map_attempt_history(history)

        assert quarantined.state is FlowState.BLOCKED
        assert quarantined.outcome is CompletionOutcome.COMPLETED_REFUSED
        assert quarantined.reason_code == "candidate_quarantined"

    def test_a_retry_chain_maps_to_linked_executions_that_reduce_cleanly(self) -> None:
        history = _retry_history()

        mapped = map_attempt_history(history)

        assert [event.transition_sequence for event in mapped] == [1, 2, 3, 4]
        assert [event.state for event in mapped] == [
            FlowState.RUNNING,
            FlowState.FAILED,
            FlowState.RUNNING,
            FlowState.COMPLETED,
        ]
        first_execution = mapped[0].execution_id
        second_execution = mapped[2].execution_id
        assert first_execution != second_execution
        assert {event.flow_id for event in mapped} == {mapped[0].flow_id}
        assert mapped[2].predecessor_execution_id == first_execution
        assert mapped[3].predecessor_execution_id == first_execution
        assert [event.predecessor_event_id for event in mapped[1:]] == [
            event.event_id for event in mapped[:-1]
        ]
        snapshot = None
        for event in mapped:
            decision = reduce_event(snapshot, event)
            assert decision.kind is ReductionKind.APPLY
            snapshot = decision.snapshot

    def test_an_empty_history_is_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="history is empty"):
            map_attempt_history(())

    def test_a_terminal_event_without_its_started_event_is_rejected(self) -> None:
        history = (attempt_event("attempt-a", AttemptEventKind.SUCCEEDED),)

        with pytest.raises(OperationalStatusError, match="no adjacent STARTED"):
            map_attempt_history(history)

    def test_a_terminal_event_not_adjacent_to_its_attempt_is_rejected(self) -> None:
        history = (
            attempt_event("attempt-a", AttemptEventKind.STARTED),
            attempt_event(
                "attempt-b", AttemptEventKind.STARTED, predecessor=AttemptId("attempt-a")
            ),
            attempt_event("attempt-a", AttemptEventKind.FAILED, occurred_at=LATER),
        )

        with pytest.raises(OperationalStatusError, match="no adjacent STARTED"):
            map_attempt_history(history)

    def test_an_inexact_predecessor_chain_is_rejected(self) -> None:
        history = (
            attempt_event("attempt-a", AttemptEventKind.STARTED),
            attempt_event("attempt-b", AttemptEventKind.STARTED),
        )

        with pytest.raises(OperationalStatusError, match="predecessor history is not exact"):
            map_attempt_history(history)

    def test_a_partition_change_across_the_chain_is_rejected(self) -> None:
        history = (
            attempt_event("attempt-a", AttemptEventKind.STARTED),
            attempt_event(
                "attempt-b",
                AttemptEventKind.STARTED,
                predecessor=AttemptId("attempt-a"),
                partition=partition(DAY_AFTER),
            ),
        )

        with pytest.raises(OperationalStatusError, match="changed its partition"):
            map_attempt_history(history)

    def test_a_terminal_event_predating_started_is_rejected(self) -> None:
        history = (
            attempt_event("attempt-a", AttemptEventKind.STARTED),
            attempt_event(
                "attempt-a", AttemptEventKind.FAILED, occurred_at=NOW - timedelta(minutes=1)
            ),
        )

        with pytest.raises(OperationalStatusError, match="predates STARTED"):
            map_attempt_history(history)


DAY_AFTER = NOW.date() + timedelta(days=1)


def _retry_history() -> tuple[Any, ...]:
    return (
        attempt_event("attempt-a", AttemptEventKind.STARTED),
        attempt_event("attempt-a", AttemptEventKind.FAILED, occurred_at=NOW + timedelta(minutes=1)),
        attempt_event(
            "attempt-b",
            AttemptEventKind.STARTED,
            predecessor=AttemptId("attempt-a"),
            occurred_at=NOW + timedelta(minutes=2),
        ),
        attempt_event(
            "attempt-b", AttemptEventKind.SUCCEEDED, occurred_at=NOW + timedelta(minutes=3)
        ),
    )


class TestIdentityDerivation:
    def test_canonical_uuid_accepts_only_lowercase_hyphenated_text(self) -> None:
        assert canonical_uuid(uuid_text(0xDEADBEEF)) == uuid_text(0xDEADBEEF)
        with pytest.raises(OperationalStatusError, match="lowercase canonical"):
            canonical_uuid(uuid_text(0xDEADBEEF).upper())
        with pytest.raises(OperationalStatusError, match="canonical UUID"):
            canonical_uuid("not-a-uuid")

    def test_the_authority_digest_is_canonical_and_key_order_free(self) -> None:
        first = canonical_authority_digest({"b": 1, "a": "two"})
        second = canonical_authority_digest({"a": "two", "b": 1})

        assert first == second
        assert len(first) == 64
        assert first == first.lower()
        assert first != canonical_authority_digest({"a": "two", "b": 2})

    def test_non_canonical_authority_evidence_is_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="not canonical JSON"):
            canonical_authority_digest(object())
        with pytest.raises(OperationalStatusError, match="not canonical JSON"):
            canonical_authority_digest({"value": float("nan")})

    def test_derived_uuids_are_stable_v5_identities_in_the_registry_namespace(self) -> None:
        first = derived_uuid("flow", SURFACE, DIGEST)

        assert first == derived_uuid("flow", SURFACE, DIGEST)
        assert first != derived_uuid("execution", SURFACE, DIGEST)
        assert uuid.UUID(first) == uuid.uuid5(
            UUID_NAMESPACE, "\x1f".join(("operational-data-flow-v1", "flow", SURFACE, DIGEST))
        )

    def test_derivation_refuses_blank_components(self) -> None:
        with pytest.raises(OperationalStatusError, match="nonblank canonical components"):
            derived_uuid(" ", "component")
        with pytest.raises(OperationalStatusError, match="nonblank canonical components"):
            derived_uuid("flow")
        with pytest.raises(OperationalStatusError, match="nonblank canonical components"):
            derived_uuid("flow", "")


WINDOW_START = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _fold(sample_count: int) -> BaselineFoldRequest:
    return BaselineFoldRequest(
        measurement_ref="semantic_candidate_decision_latency",
        measurement_version="1",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        finalized_at=WINDOW_END,
        sample_count=sample_count,
    )


class TestBaselines:
    def test_the_initial_baseline_is_a_complete_first_revision(self) -> None:
        window = initial_baseline(_fold(3))

        assert window.revision == 1
        assert window.predecessor_window_id is None
        assert window.sample_count == 3
        assert window.late_sample_count == 0
        assert window.completeness == "complete"
        assert (window.window_start, window.window_end) == (WINDOW_START, WINDOW_END)
        assert window.population_cutoff == window.finalized_at == WINDOW_END
        assert window.window_id == initial_baseline(_fold(3)).window_id

    def test_a_correction_is_an_immutable_successor_revision(self) -> None:
        first = initial_baseline(_fold(3))

        corrected = corrected_baseline(first, finalized_at=WINDOW_END + HOUR, sample_count=5)

        assert corrected.revision == 2
        assert corrected.predecessor_window_id == first.window_id
        assert corrected.window_id != first.window_id
        assert corrected.sample_count == 5
        assert corrected.late_sample_count == 2
        assert corrected.finalized_at == WINDOW_END + HOUR

    def test_a_correction_cannot_move_authority_backward(self) -> None:
        first = initial_baseline(_fold(3))

        with pytest.raises(OperationalStatusError, match="cannot move authority backward"):
            corrected_baseline(first, finalized_at=WINDOW_END - HOUR, sample_count=5)
        with pytest.raises(OperationalStatusError, match="cannot move authority backward"):
            corrected_baseline(first, finalized_at=WINDOW_END + HOUR, sample_count=2)

    def test_baseline_window_invariants_fail_closed(self) -> None:
        valid = initial_baseline(_fold(3))
        with pytest.raises(OperationalStatusError, match="positive revision"):
            replace(valid, revision=0)
        with pytest.raises(OperationalStatusError, match="completeness is invalid"):
            replace(valid, completeness="partial")
        with pytest.raises(OperationalStatusError, match="not monotonic"):
            replace(valid, population_cutoff=WINDOW_START)
        with pytest.raises(OperationalStatusError, match="cannot name a predecessor"):
            replace(valid, predecessor_window_id=uuid_text(9))
        with pytest.raises(OperationalStatusError, match="requires its predecessor"):
            replace(valid, revision=2)

    def test_a_fold_request_requires_a_versioned_measurement(self) -> None:
        with pytest.raises(OperationalStatusError, match="versioned measurement"):
            BaselineFoldRequest(
                measurement_ref="",
                measurement_version="1",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
                finalized_at=WINDOW_END,
                sample_count=0,
            )

    def test_window_sample_count_counts_only_closed_clocked_samples_in_the_window(self) -> None:
        inside = terminal_event(clock_started_at=WINDOW_START, occurred_at=WINDOW_START + HOUR / 2)
        at_end = terminal_event(clock_started_at=WINDOW_START, occurred_at=WINDOW_END)
        unclocked = terminal_event(occurred_at=WINDOW_START + HOUR / 2)
        open_flow = flow_event(clock_started_at=WINDOW_START, occurred_at=WINDOW_START + HOUR / 2)

        count = window_sample_count(
            (inside, at_end, unclocked, open_flow), WINDOW_START, WINDOW_END
        )

        assert count == 1

    def test_measurement_refs_expose_the_registered_versioned_measurement(self) -> None:
        assert MEASUREMENT_REFS[SURFACE] == (("semantic_candidate_decision_latency", "1"),)


HOUR = timedelta(hours=1)
