# SPDX-License-Identifier: MIT-0
"""Closed FIFO queue message shape, direct sink transport, and companion emission."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from operational_status_support import NOW, flow_event, terminal_event, uuid_text

from adapters.outbound.operational_companions import CompanionEmitter
from adapters.outbound.sqs import operational_status as transport
from adapters.outbound.sqs.operational_status import (
    SqsOperationalStatusSink,
    parse_message,
    serialize_message,
)
from domain.operations.status import OperationalStatusError, event_document
from domain.shared_kernel.errors import DependencyError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-073")]

QUEUE_URL = "https://sqs.test.invalid/operational-status.fifo"


@dataclass
class _RecordingSqs:
    """Record each low-level send exactly, optionally failing after recording."""

    sent: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    failure: Exception | None = None

    def send_message(self, **request: Any) -> dict[str, Any]:
        self.sent.append(request)
        if self.failure is not None:
            raise self.failure
        return {}


def _sink(client: _RecordingSqs) -> SqsOperationalStatusSink:
    return SqsOperationalStatusSink(QUEUE_URL, cast("Any", client))


class TestQueueMessageShape:
    def test_a_serialized_event_round_trips_exactly(self) -> None:
        event = terminal_event(clock_started_at=NOW)

        assert parse_message(serialize_message(event)) == event

    def test_a_non_text_body_is_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="must be text"):
            parse_message(42)

    def test_an_unparseable_body_is_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="body is invalid"):
            parse_message("{not-json")

    def test_a_non_object_body_is_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="must be an object"):
            parse_message("[]")

    def test_a_foreign_schema_version_is_rejected(self) -> None:
        body = json.dumps({"schema_version": "2", "event": event_document(flow_event())})

        with pytest.raises(OperationalStatusError, match="differ from v1"):
            parse_message(body)

    def test_an_extended_envelope_is_rejected(self) -> None:
        body = json.dumps(
            {"schema_version": "1", "event": event_document(flow_event()), "extra": True}
        )

        with pytest.raises(OperationalStatusError, match="differ from v1"):
            parse_message(body)

    def test_an_extended_inner_event_is_rejected(self) -> None:
        document = {**event_document(flow_event()), "extra": True}
        body = json.dumps({"schema_version": "1", "event": document})

        with pytest.raises(OperationalStatusError, match="differ from the closed v1"):
            parse_message(body)


class TestSink:
    def test_send_orders_by_flow_and_deduplicates_by_event(self) -> None:
        client = _RecordingSqs()
        event = flow_event()

        _sink(client).send(event)

        (request,) = client.sent
        assert request["QueueUrl"] == QUEUE_URL
        assert request["MessageGroupId"] == event.flow_id
        assert request["MessageDeduplicationId"] == event.event_id
        assert parse_message(request["MessageBody"]) == event

    def test_a_transport_failure_becomes_a_dependency_error(self) -> None:
        client = _RecordingSqs(failure=RuntimeError("queue unavailable"))

        with pytest.raises(DependencyError):
            _sink(client).send(flow_event())

    def test_an_oversized_body_is_refused_before_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _RecordingSqs()
        monkeypatch.setattr(transport, "_MAX_BODY_BYTES", 64)

        with pytest.raises(OperationalStatusError, match="exceeds queue capacity"):
            _sink(client).send(flow_event())

        assert client.sent == []

    def test_a_blank_queue_url_is_refused(self) -> None:
        with pytest.raises(ValueError, match="queue URL is required"):
            SqsOperationalStatusSink("  ")


class TestCompanionEmitter:
    def test_events_are_emitted_in_order(self) -> None:
        client = _RecordingSqs()
        start = flow_event()
        terminal = terminal_event(
            event_id=uuid_text(4), transition_sequence=2, predecessor_event_id=start.event_id
        )

        CompanionEmitter(sink=_sink(client), operation="unit_test").emit(start, terminal)

        assert [request["MessageDeduplicationId"] for request in client.sent] == [
            start.event_id,
            terminal.event_id,
        ]

    def test_a_transport_failure_is_swallowed_and_stops_the_sequence(self) -> None:
        """A lost observation must never fail or retry the workload that carried it."""
        client = _RecordingSqs(failure=RuntimeError("queue unavailable"))
        start = flow_event()
        terminal = terminal_event(
            event_id=uuid_text(4), transition_sequence=2, predecessor_event_id=start.event_id
        )

        CompanionEmitter(sink=_sink(client), operation="unit_test").emit(start, terminal)

        assert len(client.sent) == 1

    def test_an_absent_sink_disables_emission_entirely(self) -> None:
        CompanionEmitter(sink=None, operation="unit_test").emit(flow_event())
