# SPDX-License-Identifier: MIT-0
"""Reusable operational-status domain values and recording fakes for unit tests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from domain.operations.status import (
    MEASUREMENT_REFS,
    AuthorityObservation,
    CompletionOutcome,
    EvaluationBinding,
    FlowState,
    OperationalDataFlowEvent,
    canonical_authority_digest,
)
from domain.semantics.identity import CommerceScopeKey
from domain.semantics.publication import (
    AttemptEvent,
    AttemptEventKind,
    AttemptId,
    CanonicalDatasetKey,
    PartitionIdentity,
    PublicationLensKey,
    PublicationScope,
)
from domain.semantics.time import BusinessCalendarKey, FactTimeClass, MeasurementBucket, TimeGrain
from domain.shared_kernel.values import DateRange

SURFACE = "semantic_candidate_publication"
BINDING = EvaluationBinding(MEASUREMENT_REFS[SURFACE])
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
DIGEST = canonical_authority_digest({"probe": "operational-status-unit"})
DAY = date(2026, 8, 10)

_CALENDAR = BusinessCalendarKey("iso_gregorian", week_starts_on=0)
_REASONED_KINDS = frozenset(
    {AttemptEventKind.REJECTED, AttemptEventKind.QUARANTINED, AttemptEventKind.FAILED}
)


def uuid_text(number: int) -> str:
    """Return a distinct canonical lowercase UUID for one small test ordinal."""
    return str(uuid.UUID(int=number))


def flow_event(**overrides: Any) -> OperationalDataFlowEvent:
    """Build a valid root RUNNING event on the registered attempt surface."""
    values: dict[str, Any] = {
        "event_id": uuid_text(1),
        "predecessor_event_id": None,
        "flow_id": uuid_text(2),
        "execution_id": uuid_text(3),
        "predecessor_execution_id": None,
        "transition_sequence": 1,
        "surface": SURFACE,
        "stage": "candidate_started",
        "state": FlowState.RUNNING,
        "occurred_at": NOW,
        "authority": AuthorityObservation("semantic_attempt", DIGEST, NOW),
        "evaluation_binding": BINDING,
        **overrides,
    }
    return OperationalDataFlowEvent(**values)


def terminal_event(**overrides: Any) -> OperationalDataFlowEvent:
    """Build a valid terminal COMPLETED event on the registered attempt surface."""
    values: dict[str, Any] = {
        "state": FlowState.COMPLETED,
        "outcome": CompletionOutcome.SUCCEEDED,
        "stage": "candidate_published",
        **overrides,
    }
    return flow_event(**values)


def partition(day: date = DAY) -> PartitionIdentity:
    """Build one canonical daily publication partition with fictional identity."""
    return PartitionIdentity(
        scope=PublicationScope(
            dataset=CanonicalDatasetKey("fact_unit_test"),
            commerce_scope=CommerceScopeKey("amazon_us"),
            population_key="unit_test_population",
            lens=PublicationLensKey("unit_test_lens"),
            time_class=FactTimeClass.DAILY_ADDITIVE,
            business_calendar=_CALENDAR,
            source_timezone="UTC",
        ),
        bucket=MeasurementBucket(
            window=DateRange(day, day),
            grain=TimeGrain.DAY,
            calendar=_CALENDAR,
            source_timezone="UTC",
        ),
    )


def attempt_event(attempt: str, kind: AttemptEventKind, **overrides: Any) -> AttemptEvent:
    """Build one valid attempt-history event with reason defaults for refusal kinds."""
    values: dict[str, Any] = {
        "attempt_id": AttemptId(attempt),
        "partition": partition(),
        "kind": kind,
        "occurred_at": NOW,
        "sequence": 0 if kind is AttemptEventKind.STARTED else 1,
        "predecessor": None,
        "reason": "unit_test_reason" if kind in _REASONED_KINDS else None,
        **overrides,
    }
    return AttemptEvent(**values)


@dataclass
class RecordingSink:
    """Record every admitted status event in order, optionally failing instead."""

    sent: list[OperationalDataFlowEvent] = field(default_factory=list[OperationalDataFlowEvent])
    failure: Exception | None = None

    def send(self, event: OperationalDataFlowEvent) -> None:
        if self.failure is not None:
            raise self.failure
        self.sent.append(event)
