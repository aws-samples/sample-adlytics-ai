# SPDX-License-Identifier: MIT-0
"""The dispatch rule: which admitted turns reach evaluation, decided per run (ADR-079).

The dispatcher is the only switch. A row is sent only when the user received the answer
(ADR-063) and at least one family is enabled in its release record at that run; every other row
ends not dispatched, except that a turn whose delivery companion has not arrived yet waits a
bounded time for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from domain.conversation.audit import TurnDeliveryDispositionV1
from domain.evaluation.linkage import EvaluationDeliveryFamilyV1, EvaluationNotDispatchedReasonV1
from domain.evaluation.release import EvaluationReleaseV1


class EvaluationDispatchActionV1(StrEnum):
    """What the dispatcher does with one claimed row."""

    SEND = "send"
    WITHHOLD = "withhold"
    WAIT = "wait"


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationDispatchDecision:
    """One row's action; a send names its families and a withhold names its reason."""

    action: EvaluationDispatchActionV1
    families: tuple[EvaluationDeliveryFamilyV1, ...] = ()
    reason: EvaluationNotDispatchedReasonV1 | None = None

    def __post_init__(self) -> None:
        if (self.action is EvaluationDispatchActionV1.SEND) != bool(self.families):
            raise ValueError("only a dispatch send names families, and it names at least one.")
        if (self.action is EvaluationDispatchActionV1.WITHHOLD) != (self.reason is not None):
            raise ValueError("only a dispatch withhold names a reason, and it always does.")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationDispatchRequest:
    """The facts one row's decision depends on; the disposition is `None` when absent."""

    families: tuple[EvaluationDeliveryFamilyV1, ...]
    disposition: TurnDeliveryDispositionV1 | None
    admitted_at: datetime
    now: datetime
    delivery_wait_seconds: int

    def __post_init__(self) -> None:
        if self.delivery_wait_seconds < 1:
            raise ValueError("evaluation delivery wait must be a positive number of seconds.")
        for value in (self.admitted_at, self.now):
            if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
                raise ValueError("evaluation dispatch instants must be UTC.")


def enabled_delivery_families(
    releases: tuple[EvaluationReleaseV1 | None, ...],
) -> tuple[EvaluationDeliveryFamilyV1, ...]:
    """Project the enabled release records into the families a send would name.

    A missing record reads as not enabled. Two records naming one family are refused, because
    the dispatcher could not say which evaluator set a row was sent to.
    """
    families: list[EvaluationDeliveryFamilyV1] = []
    for release in releases:
        if release is None or not release.enabled or release.config_id is None:
            continue
        families.append(
            EvaluationDeliveryFamilyV1(
                family=release.family,
                config_id=release.config_id,
                evaluators=release.evaluators,
            )
        )
    names = [family.family for family in families]
    if len(set(names)) != len(names):
        raise ValueError("two release records enable the same evaluation family.")
    return tuple(families)


def decide_dispatch(request: EvaluationDispatchRequest) -> EvaluationDispatchDecision:
    """Apply the dispatch rule to one claimed row."""
    if not request.families:
        return EvaluationDispatchDecision(
            action=EvaluationDispatchActionV1.WITHHOLD,
            reason=EvaluationNotDispatchedReasonV1.EVALUATION_NOT_ENABLED,
        )
    if request.disposition is TurnDeliveryDispositionV1.DELIVERED:
        return EvaluationDispatchDecision(
            action=EvaluationDispatchActionV1.SEND, families=request.families
        )
    if request.disposition is not None:
        return EvaluationDispatchDecision(
            action=EvaluationDispatchActionV1.WITHHOLD,
            reason=EvaluationNotDispatchedReasonV1.ANSWER_NOT_RECEIVED,
        )
    deadline = request.admitted_at + timedelta(seconds=request.delivery_wait_seconds)
    if request.now < deadline:
        return EvaluationDispatchDecision(action=EvaluationDispatchActionV1.WAIT)
    return EvaluationDispatchDecision(
        action=EvaluationDispatchActionV1.WITHHOLD,
        reason=EvaluationNotDispatchedReasonV1.DELIVERY_UNKNOWN,
    )
