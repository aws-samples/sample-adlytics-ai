# SPDX-License-Identifier: MIT-0
"""The stuck-flow sweep and the `PROMOTE#` age (S5; FR-7's kept root-clock rule, V-20, V-26, V-9).

Every open flow obligation lives in one of the graph store's fixed due shards (`DUE#<n>`) and is
due at `root_clock_started_at + ceiling(v1)`. The root clock is the manifest version's
`LastModified` (FR-2), stamped on the obligation by the transaction that writes it, so a later
transition or a retry never resets it. The detector reads every shard with a strongly consistent
`Query` and reports a value only after the whole sweep completed: a failed or unreadable shard,
or an item kind the protocol does not declare, yields no value at all, never a zero.

Flow obligations are the items that keep a flow open: `INSTANTIATE#`, `WAITER#`, `VERIFY#`,
`OUTBOX#`, `FENCE#`, `RELEASE#`, `APPLY#`, and (S6 §2.2) `ARM#` (an arming that waits) and
`COMPLETE#` (a granted run not yet `SUCCEEDED`), and (plan 4.1, F9) `BAKE_RECOVERY#` (a failed bake
task's recovery owner, deleted only by the transaction that obsoletes or settles its winner).
`PROMOTE#` is a wakeup, not a flow obligation, so it is measured by its own age instead. A flow
obligation without a stamped root clock is counted apart (`without_root_clock`): every S5 writer
stamps one, so such an item is either residue from before S5 or a writer defect, and it has its
own alarm rather than a guessed due time.

`PROMOTE#` age (r5, R4-6): an item marked `held=true` is skipped while the hold is set, because a
held waiter ages by design; it is watched through its `WAITER#` obligation instead. Every other
item is measured from the later of its `woken_at` and the hold's `hold_cleared_at`, so clearing a
long hold does not make a fresh item look old.

Bake counts (plan 6.1, F9, DB26), each from its one named read:
- `BAKE_RECOVERY#` items by their closed `reason`: `fatal` counts `GraphBakeTaskFailed`,
  `retry_exhausted` counts `GraphBakeRetryExhausted`; a missing or unknown reason is no value.
- A `COMPLETE#` stamped with `bake_readiness_at` (5.1) and the winner's precompute contribution
  (`COHORT#<grant_id>/CONTRIBUTION#precompute`): past `PRECOMPUTE_BLOCKED_AFTER` with the exact
  contribution absent (or carrying another roster digest), it counts `GraphBakePrecomputeBlocked`;
  with the exact contribution settled more than `FINALIZER_BLOCKED_AFTER` ago, it counts
  `GraphBakeFinalizerBlocked`. The final transaction deletes `COMPLETE#` with the `FINALIZED`
  receipt, so a present `COMPLETE#` means no receipt. Both thresholds derive from the contract's
  precompute readiness timeline (TD-279), so neither pages on the designed healthy path: the
  precompute's designed longest gap and, for the finalizer, the armed redelivery plus its control
  claim, each plus one detector/alarm period.
- ADR-140: a `COMPLETE#` without the stamp is anchored on the grant's stored bake contribution
  (`COHORT#<grant_id>/CONTRIBUTION#bake`), because a bake that settled without its finalizer
  (U1/U2) never stamps readiness and the Release settles it within one pass. With the bake member
  stored and no precompute member past `PRECOMPUTE_BLOCKED_AFTER` it counts
  `GraphBakePrecomputeBlocked`; with both stored past `FINALIZER_BLOCKED_AFTER` (bound, not
  completed) it counts `GraphBakeFinalizerBlocked`. No bake member (fan-out pending, or a loser)
  counts nothing.

Pure: no I/O, no clock, no AWS.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from domain.operations.bake_scheduling import PRECOMPUTE_READY_SECONDS
from domain.operations.execution_graph_release import BakeRecoveryReason
from domain.operations.generated_execution_graphs import BAKE_TASKS, BAKE_V3, BOUNDS
from domain.shared_kernel.errors import ErrorCode, InvariantError

__all__ = [
    "FINALIZER_BLOCKED_AFTER",
    "FLOW_OBLIGATION_KINDS",
    "PRECOMPUTE_BLOCKED_AFTER",
    "PROMOTION_KIND",
    "PROTOCOL_CEILING",
    "ContributionObservation",
    "DueObservation",
    "HoldState",
    "SweepIncompleteError",
    "SweepReading",
    "bake_contribution_candidates",
    "promotion_age_seconds",
    "sweep",
    "unarmed_contribution_candidates",
]

FLOW_OBLIGATION_KINDS: Final = frozenset(
    {
        "INSTANTIATE",
        "WAITER",
        "VERIFY",
        "OUTBOX",
        "FENCE",
        "RELEASE",
        "APPLY",
        "ARM",
        "COMPLETE",
        "COALESCE",
        "BAKE_RECOVERY",
    }
)
"""Every item kind that keeps a flow open; the detector counts these and nothing else."""
PROMOTION_KIND: Final = "PROMOTE"
"""A wakeup for the promoter: measured by age, never counted as a flow."""
PROTOCOL_CEILING: Final = timedelta(seconds=int(BOUNDS["protocol_ceiling_seconds"]))
"""The v1 protocol ceiling, from the generated contract. A changed ceiling is a new protocol."""
_PERIOD: Final = int(BAKE_TASKS["worker_schedule_seconds"])
"""One detector schedule, which is also each bake alarm's evaluation period (contract)."""
PRECOMPUTE_BLOCKED_AFTER: Final = timedelta(seconds=PRECOMPUTE_READY_SECONDS + _PERIOD)
"""DB26: readiness to contribution may take one dispatcher schedule, one dispatcher pass and one
precompute claim lease (`PRECOMPUTE_READY_SECONDS`, 180 s); plus one period: 240 s."""
FINALIZER_BLOCKED_AFTER: Final = timedelta(
    seconds=PRECOMPUTE_READY_SECONDS + int(BAKE_TASKS["control_claim_seconds"]) + _PERIOD
)
"""DB26: a settled contribution waits at most the armed finalizer's redelivery (the same
`PRECOMPUTE_READY_SECONDS` the finalizer uses) and its 30 s control claim; plus one period:
270 s."""
_CONTRIBUTION_READ_AFTER: Final = min(PRECOMPUTE_BLOCKED_AFTER, FINALIZER_BLOCKED_AFTER)
"""The youngest readiness stamp either gauge can count: no younger stamp needs a read."""
_RECOVERY_KIND: Final = "BAKE_RECOVERY"
_COMPLETE_KIND: Final = "COMPLETE"
_RECOVERY_REASONS: Final = frozenset(str(reason) for reason in BAKE_V3["recovery_reasons"])


class SweepIncompleteError(InvariantError):
    """The sweep cannot yield a value: a shard failed, or an item breaks the declared protocol."""

    def __init__(self, description: str) -> None:
        super().__init__(ErrorCode("STUCK_FLOW", 1), description)


@dataclass(frozen=True, slots=True)
class DueObservation:
    """One item as a strongly consistent shard query returned it."""

    shard: str
    sk: str
    root_clock_started_at: datetime | None = None
    woken_at: datetime | None = None
    held: bool = False
    recovery_reason: str | None = None
    """`BAKE_RECOVERY#` only: the closed recovery reason (`reason`)."""
    run_id: str | None = None
    grant_id: str | None = None
    bake_readiness_at: datetime | None = None
    """`COMPLETE#` only: the 5.1 readiness stamp, with its roster digest below."""
    input_effect_roster_digest: str | None = None

    @property
    def kind(self) -> str:
        """The item kind: the sort key before its first `#`."""
        return self.sk.split("#", 1)[0]


@dataclass(frozen=True, slots=True)
class ContributionObservation:
    """A winner's precompute contribution as one strongly consistent `GetItem` returned it."""

    grant_id: str
    settled_at: datetime
    input_effect_roster_digest: str | None


@dataclass(frozen=True, slots=True)
class HoldState:
    """The automatic-admission hold as the mode item records it."""

    set: bool
    cleared_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SweepReading:
    """One complete sweep: the counted flows, by shard, and the promotion age."""

    shards: int
    open_flows: int
    past_ceiling: tuple[DueObservation, ...]
    without_root_clock: tuple[DueObservation, ...]
    promotion_age_seconds: float
    promotions_measured: int
    promotions_skipped_held: int
    bake_tasks_failed: tuple[DueObservation, ...] = ()
    bake_retry_exhausted: tuple[DueObservation, ...] = ()
    bake_precompute_blocked: tuple[DueObservation, ...] = ()
    bake_finalizer_blocked: tuple[DueObservation, ...] = ()

    @property
    def past_ceiling_count(self) -> int:
        """The `OpenFlowsPastProtocolCeiling` value."""
        return len(self.past_ceiling)

    @property
    def without_root_clock_count(self) -> int:
        """The `OpenFlowsWithoutRootClock` value."""
        return len(self.without_root_clock)

    def per_shard(self) -> dict[str, dict[str, list[str]]]:
        """Return the detector's per-shard evidence: each counted flow by its sort key."""
        evidence: dict[str, dict[str, list[str]]] = {}
        for label, items in (
            ("past_ceiling", self.past_ceiling),
            ("without_root_clock", self.without_root_clock),
            ("bake_tasks_failed", self.bake_tasks_failed),
            ("bake_retry_exhausted", self.bake_retry_exhausted),
            ("bake_precompute_blocked", self.bake_precompute_blocked),
            ("bake_finalizer_blocked", self.bake_finalizer_blocked),
        ):
            for item in items:
                evidence.setdefault(item.shard, {}).setdefault(label, []).append(item.sk)
        return evidence


def promotion_age_seconds(
    items: Sequence[DueObservation], hold: HoldState, now: datetime
) -> tuple[float, int, int]:
    """Return the oldest measured `PROMOTE#` age, how many were measured and how many skipped."""
    oldest = 0.0
    measured = skipped = 0
    for item in items:
        if item.kind != PROMOTION_KIND:
            continue
        if item.held and hold.set:
            skipped += 1
            continue
        if item.woken_at is None:
            raise SweepIncompleteError(f"a PROMOTE# item carries no woken_at ({item.shard}).")
        start = item.woken_at
        if hold.cleared_at is not None and hold.cleared_at > start:
            start = hold.cleared_at
        measured += 1
        oldest = max(oldest, (now - start).total_seconds())
    return oldest, measured, skipped


def _stamped_grant(item: DueObservation) -> str:
    """Return a readiness-stamped `COMPLETE#`'s grant, or refuse a partial stamp."""
    if item.grant_id is None or item.input_effect_roster_digest is None:
        raise SweepIncompleteError(f"a readiness-stamped COMPLETE# is partial ({item.shard}).")
    return item.grant_id


def bake_contribution_candidates(
    observations: Sequence[DueObservation], now: datetime
) -> tuple[str, ...]:
    """Name the grants whose precompute contribution the sweep must read (plan 6.1).

    Only a readiness-stamped `COMPLETE#` older than the younger of the two thresholds can count:
    the contribution settles after readiness, so a younger stamp is neither precompute- nor
    finalizer-blocked and needs no read.
    """
    grants = {
        _stamped_grant(item)
        for item in observations
        if item.kind == _COMPLETE_KIND
        and item.bake_readiness_at is not None
        and now - item.bake_readiness_at > _CONTRIBUTION_READ_AFTER
    }
    return tuple(sorted(grants))


def unarmed_contribution_candidates(observations: Sequence[DueObservation]) -> tuple[str, ...]:
    """Name the grant of every `COMPLETE#` without a readiness stamp (ADR-140).

    The sweep reads each one's bake contribution, and the precompute one only when bake is
    present. A `COMPLETE#` without its `grant_id` breaks the protocol, so the sweep refuses.
    """
    grants: set[str] = set()
    for item in observations:
        if item.kind != _COMPLETE_KIND or item.bake_readiness_at is not None:
            continue
        if item.grant_id is None:
            raise SweepIncompleteError(f"a COMPLETE# names no grant ({item.shard}).")
        grants.add(item.grant_id)
    return tuple(sorted(grants))


type UnarmedReads = Mapping[
    str, tuple[ContributionObservation | None, ContributionObservation | None]
]
"""Each unstamped `COMPLETE#` grant's (bake, precompute) contributions, None when not stored."""


@dataclass(frozen=True, slots=True)
class _BakeCounts:
    failed: list[DueObservation]
    exhausted: list[DueObservation]
    precompute: list[DueObservation]
    finalizer: list[DueObservation]


def _count_unarmed(  # noqa: PLR0913, PLR0917 -- the item, the reads, the clock and the tallies
    item: DueObservation, unarmed: UnarmedReads, now: datetime, counts: _BakeCounts
) -> None:
    """ADR-140: count an unstamped `COMPLETE#` from its grant's stored bake contribution."""
    if item.grant_id is None:
        raise SweepIncompleteError(f"a COMPLETE# names no grant ({item.shard}).")
    if item.grant_id not in unarmed:
        raise SweepIncompleteError(f"a COMPLETE#'s bake contribution was not read ({item.shard}).")
    bake, precompute = unarmed[item.grant_id]
    if bake is None:
        return
    if precompute is None:
        if now - bake.settled_at > PRECOMPUTE_BLOCKED_AFTER:
            counts.precompute.append(item)
    elif now - max(bake.settled_at, precompute.settled_at) > FINALIZER_BLOCKED_AFTER:
        counts.finalizer.append(item)


def _count_bake(  # noqa: PLR0913, PLR0917 -- the item, the reads, the clock and the tallies
    item: DueObservation,
    reads: tuple[Mapping[str, ContributionObservation | None], UnarmedReads],
    now: datetime,
    counts: _BakeCounts,
) -> None:
    """Count one flow obligation in its bake gauge, if it has one; refuse what it cannot read."""
    contributions, unarmed = reads
    if item.kind == _RECOVERY_KIND:
        if item.recovery_reason not in _RECOVERY_REASONS:
            raise SweepIncompleteError(f"a BAKE_RECOVERY# names no closed reason ({item.shard}).")
        fatal = item.recovery_reason == BakeRecoveryReason.FATAL.value
        (counts.failed if fatal else counts.exhausted).append(item)
        return
    if item.kind != _COMPLETE_KIND:
        return
    if item.bake_readiness_at is None:
        _count_unarmed(item, unarmed, now, counts)
        return
    age = now - item.bake_readiness_at
    if age <= _CONTRIBUTION_READ_AFTER:
        return
    grant = _stamped_grant(item)
    if grant not in contributions:
        raise SweepIncompleteError(f"a candidate's contribution was not read ({item.shard}).")
    found = contributions[grant]
    if found is None or found.input_effect_roster_digest != item.input_effect_roster_digest:
        if age > PRECOMPUTE_BLOCKED_AFTER:
            counts.precompute.append(item)
    elif now - found.settled_at > FINALIZER_BLOCKED_AFTER:
        counts.finalizer.append(item)


def sweep(  # noqa: PLR0913, PLR0917 -- the shards, their names, the hold, the clock, the reads
    shards: Mapping[str, Sequence[DueObservation] | None],
    expected_shards: Sequence[str],
    hold: HoldState,
    now: datetime,
    contributions: Mapping[str, ContributionObservation | None] | None = None,
    unarmed: UnarmedReads | None = None,
) -> SweepReading:
    """Fold one complete sweep, or refuse to yield a value.

    `shards` maps each shard to its strongly read items, or to None when that shard's read
    failed. `contributions` maps each `bake_contribution_candidates` grant to its contribution
    (None when the strong read found none); a candidate missing from it was not read. `unarmed`
    maps each `unarmed_contribution_candidates` grant to its (bake, precompute) contributions
    (ADR-140). The reading exists only when every expected shard and candidate was read and every
    item is a declared kind with a readable bake stamp; otherwise `SweepIncompleteError`, and
    the caller emits nothing.
    """
    read = (contributions or {}, unarmed or {})
    counts = _BakeCounts([], [], [], [])
    missing = [shard for shard in expected_shards if shards.get(shard) is None]
    if missing:
        raise SweepIncompleteError(f"{len(missing)} due shard(s) were not read.")
    extra = set(shards) - set(expected_shards)
    if extra:
        raise SweepIncompleteError("a shard outside the generated set was read.")
    past: list[DueObservation] = []
    unclocked: list[DueObservation] = []
    promotions: list[DueObservation] = []
    open_flows = 0
    for shard in expected_shards:
        for item in shards[shard] or ():
            if item.kind == PROMOTION_KIND:
                promotions.append(item)
                continue
            if item.kind not in FLOW_OBLIGATION_KINDS:
                raise SweepIncompleteError(f"an undeclared due item kind was read ({shard}).")
            open_flows += 1
            _count_bake(item, read, now, counts)
            if item.root_clock_started_at is None:
                unclocked.append(item)
            elif now >= item.root_clock_started_at + PROTOCOL_CEILING:
                past.append(item)
    age, measured, skipped = promotion_age_seconds(promotions, hold, now)
    return SweepReading(
        shards=len(expected_shards),
        open_flows=open_flows,
        past_ceiling=tuple(past),
        without_root_clock=tuple(unclocked),
        promotion_age_seconds=age,
        promotions_measured=measured,
        promotions_skipped_held=skipped,
        bake_tasks_failed=tuple(counts.failed),
        bake_retry_exhausted=tuple(counts.exhausted),
        bake_precompute_blocked=tuple(counts.precompute),
        bake_finalizer_blocked=tuple(counts.finalizer),
    )
