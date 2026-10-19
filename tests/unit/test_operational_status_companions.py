# SPDX-License-Identifier: MIT-0
"""Deterministic companion flows and the baker's SQS-attribute flow derivation."""

from __future__ import annotations

# The baker's flow derivation is a private composition seam exercised directly.
# pyright: reportPrivateUsage=false
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from operational_status_support import NOW

from adapters.jobs.preset_artifact_publisher import baker
from domain.operations.companions import (
    CompanionFlowRequest,
    CompanionTerminal,
    companion_flow,
)
from domain.operations.status import (
    MEASUREMENT_REFS,
    CompletionOutcome,
    FlowState,
    OperationalDataFlowEvent,
    OperationalStatusError,
    Progress,
    ReductionKind,
    event_document,
    reduce_event,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-073")]

LATER = NOW + timedelta(minutes=5)


def _request(**overrides: Any) -> CompanionFlowRequest:
    values: dict[str, Any] = {
        "surface": "preset_bake",
        "authority_kind": "bake_message",
        "identity": {"message_id": "fictional-message-1", "receive_count": "1"},
        "started_at": NOW,
        **overrides,
    }
    return CompanionFlowRequest(**values)


def _terminal(**overrides: Any) -> CompanionTerminal:
    values: dict[str, Any] = {
        "state": FlowState.COMPLETED,
        "outcome": CompletionOutcome.SUCCEEDED,
        "stage": "pointer_last_attested",
        "occurred_at": LATER,
        "authority_digest_value": {"pointer_outcome": "advanced"},
        **overrides,
    }
    return CompanionTerminal(**values)


def _canonical(event: OperationalDataFlowEvent) -> str:
    return json.dumps(event_document(event), sort_keys=True, separators=(",", ":"))


class TestCompanionFlow:
    def test_identical_requests_derive_byte_identical_start_events(self) -> None:
        first = companion_flow(_request())
        second = companion_flow(_request())

        assert (first.flow_id, first.execution_id) == (second.flow_id, second.execution_id)
        assert _canonical(first.start) == _canonical(second.start)

    def test_a_different_identity_derives_a_different_flow(self) -> None:
        first = companion_flow(_request())
        retried = companion_flow(
            _request(identity={"message_id": "fictional-message-1", "receive_count": "2"})
        )

        assert first.flow_id != retried.flow_id

    def test_a_changed_clock_or_start_contract_derives_a_new_flow(self) -> None:
        original = companion_flow(_request())
        later = companion_flow(_request(started_at=LATER))
        changed_stage = companion_flow(_request(start_stage="different_authority_stage"))

        assert len({original.flow_id, later.flow_id, changed_stage.flow_id}) == 3
        assert (
            len({original.start.event_id, later.start.event_id, changed_stage.start.event_id}) == 3
        )

    def test_a_changed_terminal_disposition_derives_new_immutable_evidence(self) -> None:
        flow = companion_flow(_request())
        original = flow.terminal(_terminal())
        changed_time = flow.terminal(_terminal(occurred_at=LATER + timedelta(seconds=1)))
        changed_stage = flow.terminal(_terminal(stage="different_terminal_stage"))

        assert len({original.event_id, changed_time.event_id, changed_stage.event_id}) == 3
        assert (
            len(
                {
                    original.authority.opaque_digest,
                    changed_time.authority.opaque_digest,
                    changed_stage.authority.opaque_digest,
                }
            )
            == 3
        )

    def test_identical_dispositions_derive_byte_identical_terminal_events(self) -> None:
        """Redelivery determinism: identical inputs must reproduce one immutable event.

        Anything else makes every at-least-once duplicate dead-letter instead of
        deduplicating at the projection.
        """
        flow = companion_flow(_request())
        first = flow.terminal(_terminal())
        second = flow.terminal(_terminal())

        assert first == second
        assert first.event_id == second.event_id
        assert first.authority.opaque_digest == second.authority.opaque_digest

    def test_started_at_is_utc_normalized_for_terminal_reuse(self) -> None:
        flow = companion_flow(_request())
        assert flow.started_at.tzinfo is UTC
        reused = flow.terminal(_terminal(occurred_at=flow.started_at))
        replayed = flow.terminal(_terminal(occurred_at=flow.started_at))
        assert reused == replayed

    def test_start_then_terminal_reduce_cleanly_in_sequence(self) -> None:
        flow = companion_flow(_request())
        terminal = flow.terminal(_terminal())

        opened = reduce_event(None, flow.start)
        assert opened.kind is ReductionKind.APPLY
        closed = reduce_event(opened.snapshot, terminal)
        assert closed.kind is ReductionKind.APPLY
        assert closed.snapshot is not None
        assert closed.snapshot.state is FlowState.COMPLETED
        assert closed.snapshot.latest_sequence == 2

    def test_the_terminal_event_shares_flow_execution_and_clock(self) -> None:
        flow = companion_flow(_request())
        terminal = flow.terminal(_terminal())

        assert terminal.flow_id == flow.flow_id
        assert terminal.execution_id == flow.execution_id
        assert terminal.predecessor_event_id == flow.start.event_id
        assert terminal.clock_started_at == flow.start.clock_started_at == NOW
        assert terminal.evaluation_binding.measurement_refs == MEASUREMENT_REFS["preset_bake"]

    def test_identical_dispositions_on_two_flows_derive_distinct_terminal_events(self) -> None:
        """Prevent the terminal-id collision that dead-lettered every repeated disposition.

        One repeated refusal (same page, same reason) must never collide across flows.
        """
        first = companion_flow(_request())
        second = companion_flow(
            _request(identity={"message_id": "fictional-message-1", "receive_count": "2"})
        )

        first_terminal = first.terminal(_terminal())
        second_terminal = second.terminal(_terminal())

        assert first_terminal.event_id != second_terminal.event_id
        assert first_terminal.authority.opaque_digest != second_terminal.authority.opaque_digest

    def test_the_terminal_event_carries_the_attested_progress(self) -> None:
        progress = Progress(3, 3, "intents")

        terminal = companion_flow(_request()).terminal(_terminal(progress=progress))

        assert terminal.progress == progress

    def test_a_terminal_start_state_is_refused(self) -> None:
        with pytest.raises(OperationalStatusError, match="start state must be nonterminal"):
            _request(start_state=FlowState.COMPLETED)

    def test_a_nonterminal_terminal_state_is_refused(self) -> None:
        with pytest.raises(OperationalStatusError, match="requires a terminal state"):
            _terminal(state=FlowState.RUNNING)

    def test_an_empty_identity_is_refused(self) -> None:
        with pytest.raises(OperationalStatusError, match="canonical components"):
            _request(identity={})

    def test_an_unregistered_surface_is_refused(self) -> None:
        with pytest.raises(OperationalStatusError, match="not registered"):
            _request(surface="fictional_surface")


def _record(**overrides: Any) -> dict[str, object]:
    values: dict[str, object] = {
        "messageId": "fictional-message-1",
        "attributes": {"SentTimestamp": "1755600000000", "ApproximateReceiveCount": "1"},
        **overrides,
    }
    return values


class TestBakeFlow:
    def test_a_bake_flow_starts_its_clock_at_the_sent_instant(self) -> None:
        flow = baker._bake_flow(_record())

        assert flow is not None
        assert flow.start.surface == "preset_bake"
        assert flow.start.stage == "bake_message_received"
        assert flow.start.state is FlowState.RUNNING
        assert flow.start.clock_started_at == datetime.fromtimestamp(1_755_600_000, tz=UTC)

    def test_the_receive_count_distinguishes_redelivery_flows(self) -> None:
        first = baker._bake_flow(_record())
        redelivered = baker._bake_flow(
            _record(attributes={"SentTimestamp": "1755600000000", "ApproximateReceiveCount": "2"})
        )

        assert first is not None
        assert redelivered is not None
        assert first.flow_id != redelivered.flow_id

    def test_records_without_stable_attributes_derive_no_flow(self) -> None:
        assert baker._bake_flow(_record(messageId=None)) is None
        assert baker._bake_flow(_record(attributes=None)) is None
        assert (
            baker._bake_flow(
                _record(attributes={"SentTimestamp": "soon", "ApproximateReceiveCount": "1"})
            )
            is None
        )
        assert baker._bake_flow(_record(attributes={"SentTimestamp": "1755600000000"})) is None
