# SPDX-License-Identifier: MIT-0
"""The closed §5.3 source-delivery acknowledgment classifier (Load Path write discipline).

Pure classification over the strongly consistent post-submit read: exactly one of the four
acknowledgment outcomes is proved from the retained intent/owner/plan/head forms, or the
source stays unacknowledged. No transaction return or SDK response is acknowledgment
authority; only the read is. The owning adapter (`load.py`) performs the read and consumes
the proof; this module owns the decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from domain.operations.stage1_lifecycle_head import Stage1LifecycleHeadTuple
from domain.operations.status import canonical_authority_digest
from domain.operations.write_plan import (
    IntentMeta,
    IntentState,
    PlanMeta,
    PlanStatus,
    SuccessorOwner,
)
from domain.shared_kernel.errors import ErrorCode, InvariantError

__all__ = [
    "SOURCE_ACKNOWLEDGMENT_NAMESPACE",
    "SOURCE_UNACKNOWLEDGED",
    "ProvedSourceOutcome",
    "SourceDeliveryObservation",
    "classify_source_delivery",
    "source_acknowledgment_digest",
]

SOURCE_UNACKNOWLEDGED: Final = ErrorCode("STAGE1_DELIVERY", 2)
"""The closed §5.3 strong read proved no single outcome; the source stays unacknowledged."""

SOURCE_ACKNOWLEDGMENT_NAMESPACE: Final = "stage1-source-acknowledgment/v1"

_UNPROVED_ORDINAL: Final = 0


@dataclass(frozen=True, slots=True)
class SourceDeliveryObservation:
    """One closed strongly consistent read after ``SubmitPlanIntent`` (§5.3).

    ``active_plan``/``active_head`` are the owner's paired active plan and its lifecycle head;
    ``last_plan``/``last_head`` are the owner's last persisted plan when no active pair exists.
    """

    intent_id: str
    intent: IntentMeta | None
    owner: SuccessorOwner | None
    active_plan: PlanMeta | None = None
    active_head: Stage1LifecycleHeadTuple | None = None
    last_plan: PlanMeta | None = None
    last_head: Stage1LifecycleHeadTuple | None = None


@dataclass(frozen=True, slots=True)
class ProvedSourceOutcome:
    """Exactly one proved §5.3 outcome, with its §10 acknowledgment digest."""

    ordinal: int
    owner_revision: int
    acknowledgment_digest: str


def source_acknowledgment_digest(intent_id: str, ordinal: int, owner_revision: int) -> str:
    """Derive the §10 acknowledgment digest through its sole constructor."""
    return canonical_authority_digest(
        [SOURCE_ACKNOWLEDGMENT_NAMESPACE, intent_id, ordinal, owner_revision]
    )


def _unproved(description: str, **context: object) -> InvariantError:
    return InvariantError(
        SOURCE_UNACKNOWLEDGED,
        f"The source delivery stays unacknowledged: {description}",
        context=dict(context),
    )


def _head_agrees(
    owner: SuccessorOwner, plan: PlanMeta, head: Stage1LifecycleHeadTuple | None
) -> bool:
    return (
        head is not None
        and head.current_internal_execution_id == plan.internal_execution_id
        and owner.latest_internal_execution_id == plan.internal_execution_id
    )


def _admission_failed_transfer(observation: SourceDeliveryObservation) -> bool:
    owner = observation.owner
    plan = observation.last_plan
    return (
        owner is not None
        and plan is not None
        and owner.active_plan_key is None
        and plan.status is PlanStatus.ADMISSION_FAILED
        and plan.origin_intent_id == observation.intent_id
        and _head_agrees(owner, plan, observation.last_head)
    )


def _proved_ordinal(observation: SourceDeliveryObservation) -> int:
    intent = observation.intent
    owner = observation.owner
    if intent is None or owner is None:
        return _UNPROVED_ORDINAL
    if intent.state is IntentState.PENDING:
        # IntentMeta already proves the pending form: no claimed_generation and both exact
        # ready fields. Outcome 4 is the same pending intent reached by an admission-failed
        # transfer; outcome 1 is every other pending intent.
        return 4 if _admission_failed_transfer(observation) else 1
    if intent.state is IntentState.CLAIMED:
        reserved = (
            owner.reserved_intent_id == intent.intent_id
            and owner.reserved_intent_digest == intent.intent_id
            and owner.reserved_generation == intent.claimed_generation
        )
        return 2 if reserved else _UNPROVED_ORDINAL
    plan = observation.active_plan
    admitted = (
        plan is not None
        and owner.active_intent_id == intent.intent_id
        and owner.active_plan_key is not None
        and plan.origin_intent_id == intent.intent_id
        and plan.generation == intent.claimed_generation
        and plan.status is PlanStatus.PLANNED
        and plan.admitted_count == plan.unit_count
        and _head_agrees(owner, plan, observation.active_head)
    )
    return 3 if admitted else _UNPROVED_ORDINAL


def classify_source_delivery(observation: SourceDeliveryObservation) -> ProvedSourceOutcome:
    """Prove exactly one §5.3 outcome from the closed strong read, or raise unacknowledged.

    No transaction return or SDK response is acknowledgment authority; only this read is.
    """
    intent = observation.intent
    owner = observation.owner
    if intent is None or owner is None:
        raise _unproved(
            "the intent META or SUCCESSOR_OWNER is absent after the submit.",
            intent_id=observation.intent_id,
        )
    if intent.intent_id != observation.intent_id:
        raise _unproved(
            "the strongly read intent names another identity.", intent_id=observation.intent_id
        )
    ordinal = _proved_ordinal(observation)
    if ordinal == _UNPROVED_ORDINAL:
        raise _unproved(
            "the closed read matches no acknowledgment outcome.",
            intent_id=observation.intent_id,
            state=intent.state.value,
            owner_revision=owner.revision,
        )
    return ProvedSourceOutcome(
        ordinal=ordinal,
        owner_revision=owner.revision,
        acknowledgment_digest=source_acknowledgment_digest(
            observation.intent_id, ordinal, owner.revision
        ),
    )
