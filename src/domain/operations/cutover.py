# SPDX-License-Identifier: MIT-0
"""The S6 drain-to-zero cutover rules (design.md, "Legacy cutover", steps 3-6).

Pure: every instant is passed in, nothing reads a clock or AWS.

- **Drain deadline `D`.** The operator supplies `D` with the drain CAS. It has a floor and a
  ceiling, both generated: the floor is drain CAS + `T_max` + margin, because the first counted
  enumeration may begin only then (r5, R4-20); the ceiling is drain CAS + 12 h less the delivery
  queue's visibility (`TIMEOUT_SECONDS + VISIBILITY_MARGIN_SECONDS`), because SQS caps a message's
  visibility at 12 h from its receive. A `D` outside both is refused before any write.
- **Writer-lifetime fence.** A legacy writer that read the mode before the drain CAS does not
  count on `DRAIN_SEQ`, but it can commit only while its invocation lives, at most `T_max`. So a
  counted enumeration started before drain CAS + `T_max` + margin is refused
  (`DRAIN_FENCE_NOT_ELAPSED`).
- **Zero proof.** F1-F4 read by consistent base-table reads; any non-zero family refuses the
  `GRAPH` CAS (`DRAIN_NOT_ZERO`). The CAS itself ConditionChecks the `DRAIN_SEQ` value read
  before the enumeration (`DRAIN_SEQ_CHANGED`).
- **Abort legality.** `LEGACY_DRAINING -> LEGACY` is permitted only while `graph_entered_at` is
  absent (`CUTOVER_ABORT_NOT_PERMITTED`, V-18).
- **Drain-wait visibility.** A delivery refused during the drain becomes visible again at `D`
  plus the queue's visibility margin.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from domain.operations.execution_graph import (
    AuthorityMode,
    GraphCommandRefusedError,
    ModeState,
)
from domain.operations.generated_execution_graphs import CUTOVER, ROUTES, T_MAX_SECONDS

__all__ = [
    "CUTOVER_ABORT_NOT_PERMITTED",
    "CUTOVER_REQUIRES_ZERO_PROOF",
    "DRAIN_DEADLINE_OUT_OF_BOUND",
    "DRAIN_FENCE_NOT_ELAPSED",
    "DRAIN_LAST_RECEIVE",
    "DRAIN_NOT_ZERO",
    "DRAIN_SEQ_CHANGED",
    "DRAIN_SEQ_PARTITION_KEY",
    "DRAIN_SEQ_RECORD_KEY",
    "FENCE_MARGIN_SECONDS",
    "FamilyCounts",
    "drain_deadline_bounds",
    "drain_wait_visibility_seconds",
    "is_last_receive",
    "require_abort_permitted",
    "require_drain_deadline",
    "require_enumeration_permitted",
    "require_post_graph_read_permitted",
    "require_zero",
    "writer_counts_drain",
]

DRAIN_DEADLINE_OUT_OF_BOUND: Final = "DRAIN_DEADLINE_OUT_OF_BOUND"
DRAIN_FENCE_NOT_ELAPSED: Final = "DRAIN_FENCE_NOT_ELAPSED"
DRAIN_SEQ_CHANGED: Final = "DRAIN_SEQ_CHANGED"
CUTOVER_ABORT_NOT_PERMITTED: Final = "CUTOVER_ABORT_NOT_PERMITTED"
DRAIN_NOT_ZERO: Final = "DRAIN_NOT_ZERO"
CUTOVER_REQUIRES_ZERO_PROOF: Final = "CUTOVER_REQUIRES_ZERO_PROOF"
DRAIN_LAST_RECEIVE: Final = "DRAIN_LAST_RECEIVE"
"""A delivery refused during the drain on its last allowed receive; it still reaches the DLQ."""

DRAIN_SEQ_PARTITION_KEY: Final = str(CUTOVER["drain_seq_key"]["partition_key"])
DRAIN_SEQ_RECORD_KEY: Final = str(CUTOVER["drain_seq_key"]["record_key"])
FENCE_MARGIN_SECONDS: Final = int(CUTOVER["drain_fence_margin_seconds"])
_VISIBILITY_CAP: Final = int(CUTOVER["sqs_visibility_cap_seconds"])
_QUEUE_TIMEOUT: Final = int(CUTOVER["delivery_queue_timeout_seconds"])
_QUEUE_MARGIN: Final = int(CUTOVER["delivery_queue_visibility_margin_seconds"])
_MAX_RECEIVES: Final = int(ROUTES["delivery"]["max_receive_count"])


def drain_deadline_bounds(drain_cas_at: datetime) -> tuple[datetime, datetime]:
    """Return the earliest and latest legal `D` for a drain CAS at `drain_cas_at`."""
    earliest = drain_cas_at + timedelta(seconds=T_MAX_SECONDS + FENCE_MARGIN_SECONDS)
    latest = drain_cas_at + timedelta(seconds=_VISIBILITY_CAP - (_QUEUE_TIMEOUT + _QUEUE_MARGIN))
    return earliest, latest


def require_drain_deadline(drain_cas_at: datetime, deadline: datetime) -> None:
    """Refuse a `D` before the writer-lifetime fence or beyond the visibility bound."""
    earliest, latest = drain_deadline_bounds(drain_cas_at)
    if deadline < earliest or deadline > latest:
        raise GraphCommandRefusedError(
            DRAIN_DEADLINE_OUT_OF_BOUND,
            f"the drain deadline must lie in [{earliest.isoformat()}, {latest.isoformat()}].",
        )


def require_enumeration_permitted(mode: ModeState, now: datetime) -> None:
    """Refuse a counted enumeration before drain CAS + `T_max` + margin (V-17b(b))."""
    if mode.mode is not AuthorityMode.LEGACY_DRAINING or mode.drain_entered_at is None:
        raise GraphCommandRefusedError(
            CUTOVER_REQUIRES_ZERO_PROOF, "a counted enumeration needs mode LEGACY_DRAINING."
        )
    fence = datetime.fromisoformat(mode.drain_entered_at) + timedelta(
        seconds=T_MAX_SECONDS + FENCE_MARGIN_SECONDS
    )
    if now < fence:
        raise GraphCommandRefusedError(
            DRAIN_FENCE_NOT_ELAPSED,
            f"the writer-lifetime fence ends at {fence.isoformat()}; enumerate after it.",
        )


def require_post_graph_read_permitted(mode: ModeState) -> None:
    """Permit the uncounted F1-F4 re-read only in `GRAPH` with `graph_entered_at` set (P6(d')).

    It needs no fence and no `DRAIN_SEQ` binding: after the CAS no legacy writer counts, and the
    read decides nothing. It exists so the post-CAS zero check can be repeated when the read
    inside `enter_graph` failed, the process died after the commit, or the commit was ambiguous.
    """
    if mode.mode is not AuthorityMode.GRAPH or mode.graph_entered_at is None:
        raise GraphCommandRefusedError(
            CUTOVER_REQUIRES_ZERO_PROOF, "the post-GRAPH re-read needs GRAPH with graph_entered_at."
        )


def require_abort_permitted(mode: ModeState) -> None:
    """Permit `LEGACY_DRAINING -> LEGACY` only while `graph_entered_at` is absent (V-18)."""
    if mode.mode is not AuthorityMode.LEGACY_DRAINING or mode.graph_entered_at is not None:
        raise GraphCommandRefusedError(
            CUTOVER_ABORT_NOT_PERMITTED, "the abort needs LEGACY_DRAINING and no graph entry."
        )


@dataclass(frozen=True, slots=True)
class FamilyCounts:
    """One complete strongly consistent enumeration of the open-work families F1-F4."""

    pending_intents: int
    """F1: intents in `pending`, of both origins."""
    claimed_unpersisted_intents: int
    """F2: intents in `claimed` whose plan is not yet persisted."""
    plans_with_pending_admission: int
    """F3: plans in `planned` with an `admission=pending` unit."""
    admitted_unfinalized_units: int
    """F4: admitted units of a `planned` plan that are neither attested nor failed."""

    @property
    def zero(self) -> bool:
        """Whether every family read zero."""
        return not (
            self.pending_intents
            or self.claimed_unpersisted_intents
            or self.plans_with_pending_admission
            or self.admitted_unfinalized_units
        )

    def as_dict(self) -> dict[str, int]:
        """Return the counts by family name."""
        return {
            "F1_pending_intents": self.pending_intents,
            "F2_claimed_unpersisted_intents": self.claimed_unpersisted_intents,
            "F3_plans_with_pending_admission": self.plans_with_pending_admission,
            "F4_admitted_unfinalized_units": self.admitted_unfinalized_units,
        }


def require_zero(counts: FamilyCounts) -> None:
    """Refuse the `GRAPH` CAS unless every family read zero."""
    if not counts.zero:
        raise GraphCommandRefusedError(
            DRAIN_NOT_ZERO, f"open legacy work remains: {counts.as_dict()}."
        )


def writer_counts_drain(observed: AuthorityMode) -> bool:
    """Whether a legacy writer's family-changing transactions add 1 to `DRAIN_SEQ`."""
    return observed is AuthorityMode.LEGACY_DRAINING


def drain_wait_visibility_seconds(deadline: datetime, now: datetime) -> int:
    """Return the visibility that holds a refused delivery until `D` plus the queue margin.

    Clamped below the SQS cap; a delivery refused after `D` waits only the margin.
    """
    remaining = max(0.0, (deadline - now).total_seconds())
    seconds = math.ceil(remaining) + _QUEUE_MARGIN
    return min(seconds, _VISIBILITY_CAP - _QUEUE_TIMEOUT)


def is_last_receive(receive_count: int) -> bool:
    """Whether this receive is the delivery queue's last allowed one (`maxReceiveCount`)."""
    return receive_count >= _MAX_RECEIVES
