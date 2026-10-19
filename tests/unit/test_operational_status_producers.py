# SPDX-License-Identifier: MIT-0
"""Attempt-stream trigger parsing and exact-readback status production, without AWS."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from operational_status_support import NOW, RecordingSink, attempt_event

from adapters.config import operational_attempts_settings, tracing_settings
from domain.operations.status import FlowState, OperationalStatusError
from domain.semantics.publication import AttemptEvent, AttemptEventKind, AttemptId

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-073")]

_ENV = {
    "ADL_OPERATIONAL_STATUS_QUEUE_URL": "https://sqs.test.invalid/operational-status.fifo",
    "ADL_SEMANTIC_ATTEMPTS_TABLE": "unit-test-semantic-attempts",
    "AWS_DEFAULT_REGION": "us-east-1",
}


@pytest.fixture
def attempts(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import production composition under fictional module-level settings."""
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    operational_attempts_settings.cache_clear()
    tracing_settings.cache_clear()
    return cast("Any", importlib.import_module("adapters.jobs.operational_status.attempts"))


def _image(**overrides: Any) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "attempt_id": {"S": "attempt-a"},
        "event_key": {"S": "00000000"},
        "payload": {"S": "immutable-payload"},
        **overrides,
    }
    return {
        "eventName": "INSERT",
        "dynamodb": {"NewImage": attributes, "SequenceNumber": "1"},
    }


class TestParseRecord:
    def test_an_insert_image_parses_to_its_exact_trigger(self, attempts: Any) -> None:
        trigger = attempts.parse_record(_image())

        assert trigger == attempts.AttemptTrigger("attempt-a", "00000000", "immutable-payload")

    def test_a_remove_record_is_ignored(self, attempts: Any) -> None:
        record = _image()
        record["eventName"] = "REMOVE"

        assert attempts.parse_record(record) is None

    def test_a_record_missing_a_key_attribute_is_ignored(self, attempts: Any) -> None:
        assert attempts.parse_record(_image(payload={"N": "1"})) is None
        assert attempts.parse_record(_image(event_key=None)) is None
        assert attempts.parse_record({"eventName": "INSERT", "dynamodb": {}}) is None
        assert attempts.parse_record("not-a-record") is None


@dataclass
class _Authority:
    histories: dict[str, tuple[AttemptEvent, ...]]
    payloads: dict[tuple[str, str], str]

    def events_for(self, attempt_id: str) -> tuple[AttemptEvent, ...]:
        return self.histories.get(attempt_id, ())

    def exact_payload(self, attempt_id: str, event_key: str) -> str | None:
        return self.payloads.get((attempt_id, event_key))


def _retry_world() -> _Authority:
    """Attempt a failed, then attempt b succeeded with an exact predecessor link."""
    first = (
        attempt_event("attempt-a", AttemptEventKind.STARTED),
        attempt_event("attempt-a", AttemptEventKind.FAILED, occurred_at=NOW + timedelta(minutes=1)),
    )
    second = (
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
    return _Authority(
        histories={"attempt-a": first, "attempt-b": second},
        payloads={
            ("attempt-a", "00000000"): "payload-a0",
            ("attempt-a", "00000001"): "payload-a1",
            ("attempt-b", "00000000"): "payload-b0",
            ("attempt-b", "00000001"): "payload-b1",
        },
    )


class TestAttemptsProducer:
    def test_a_failed_exact_readback_refuses_the_trigger(self, attempts: Any) -> None:
        producer = attempts.AttemptsProducer(_retry_world(), RecordingSink())

        with pytest.raises(OperationalStatusError, match="exact consistent readback"):
            producer.apply(attempts.AttemptTrigger("attempt-b", "00000001", "stale-payload"))

    def test_an_absent_stored_payload_refuses_the_trigger(self, attempts: Any) -> None:
        producer = attempts.AttemptsProducer(_retry_world(), RecordingSink())

        with pytest.raises(OperationalStatusError, match="exact consistent readback"):
            producer.apply(attempts.AttemptTrigger("attempt-b", "00000009", "payload-b1"))

    def test_a_trigger_absent_from_exact_history_is_refused(self, attempts: Any) -> None:
        world = _retry_world()
        world.payloads[("attempt-b", "00000009")] = "payload-b9"
        producer = attempts.AttemptsProducer(world, RecordingSink())

        with pytest.raises(OperationalStatusError, match="absent from exact history"):
            producer.apply(attempts.AttemptTrigger("attempt-b", "00000009", "payload-b9"))

    def test_the_full_chain_resends_in_reconstructed_predecessor_order(self, attempts: Any) -> None:
        sink = RecordingSink()
        producer = attempts.AttemptsProducer(_retry_world(), sink)

        producer.apply(attempts.AttemptTrigger("attempt-b", "00000001", "payload-b1"))

        assert [event.transition_sequence for event in sink.sent] == [1, 2, 3, 4]
        assert [event.state for event in sink.sent] == [
            FlowState.RUNNING,
            FlowState.FAILED,
            FlowState.RUNNING,
            FlowState.COMPLETED,
        ]
        assert len({event.flow_id for event in sink.sent}) == 1

    def test_the_resend_prefix_ends_at_the_triggering_event(self, attempts: Any) -> None:
        sink = RecordingSink()
        producer = attempts.AttemptsProducer(_retry_world(), sink)

        producer.apply(attempts.AttemptTrigger("attempt-b", "00000000", "payload-b0"))

        assert [event.transition_sequence for event in sink.sent] == [1, 2, 3]
        assert sink.sent[-1].state is FlowState.RUNNING

    def test_an_unavailable_predecessor_history_is_refused(self, attempts: Any) -> None:
        world = _retry_world()
        world.histories.pop("attempt-a")
        producer = attempts.AttemptsProducer(world, RecordingSink())

        with pytest.raises(OperationalStatusError, match="history is unavailable"):
            producer.apply(attempts.AttemptTrigger("attempt-b", "00000001", "payload-b1"))

    def test_a_predecessor_chain_beyond_its_bound_is_refused(self, attempts: Any) -> None:
        histories: dict[str, tuple[AttemptEvent, ...]] = {}
        payloads: dict[tuple[str, str], str] = {}
        for ordinal in range(33):
            name = f"attempt-{ordinal:03d}"
            predecessor = None if ordinal == 0 else AttemptId(f"attempt-{ordinal - 1:03d}")
            histories[name] = (
                attempt_event(
                    name,
                    AttemptEventKind.STARTED,
                    predecessor=predecessor,
                    occurred_at=NOW + timedelta(minutes=ordinal),
                ),
            )
            payloads[(name, "00000000")] = f"payload-{name}"
        producer = attempts.AttemptsProducer(_Authority(histories, payloads), RecordingSink())

        with pytest.raises(OperationalStatusError, match="exceeds its bound"):
            producer.apply(
                attempts.AttemptTrigger("attempt-032", "00000000", "payload-attempt-032")
            )
