# SPDX-License-Identifier: MIT-0
"""Run-request admission: the derived class, the conflict unit and the decision table.

Two parts. The first derives the `historical` admission class (V-8, FULL_HISTORY_NOT_ENABLED).
The second is the 20-row decision table (19 rows if the owner declines row 11), its guards
(BF-7, BF-9, V-29), the input-revision ordering (V-7) and its totality proof (V-6).

Historical class.

The admission class is derived from manifest authority, never taken from a caller's label. A
manifest is historical when it is part of the phase-B backfill of days before the replayed
window, or one of the old daily runs that backfill replaces. v1 refuses every such manifest.

The rule is decided over the replay band: the retrieval instants the dev fixture planner
assigns, measured in seconds from one replay base instant. Inside that band the instant encodes
the delivery's run day, so the declared period alone cannot decide. A phase-A run restates
days before the cut, and a phase-0 seal or product-hierarchy chunk covers days from the first
span day. Two encodings exist:

- *strided* (AMC daily runs): offset `slot x stride + chunk`. Slots below the seal base are
  the run index, so the run day is `first_day + slot`. Seal slots follow, then backfill slots,
  then the reserved probe slots, which are never historical.
- *paired* (Vendor day reports): offset `day_index x 2 + variant`, where variant 1 is the
  restating report that falls due two days after its day.

An instant before the base is a hand-built delivery that is not part of the replay, and an
instant above the probe slots is outside the band, where the declared period decides. The
planner's own output is the totality test (`tests/unit/test_historical_predicate.py`).

Pure: no I/O, no clock, no AWS.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Final

__all__ = [
    "ACTIVE_COMPUTE_NOT_REPLACEABLE",
    "ACTIVE_RUN_GRANTED",
    "DECISION_TABLE_NOT_TOTAL",
    "FULL_HISTORY_NOT_ENABLED",
    "RUN_REQUEST_WAIT_SLOT_CONFLICT",
    "SLOT_REVISION_CHANGED",
    "ActiveRun",
    "AdmissionClass",
    "AdmissionGuardError",
    "CallerKind",
    "Decision",
    "DecisionInputs",
    "DecisionTableError",
    "FingerprintMatch",
    "HistoricalPolicy",
    "ManifestFacts",
    "MatchState",
    "RetrievalEncoding",
    "RunState",
    "Waiter",
    "check_decision_table",
    "decide",
    "derive_admission_class",
    "is_historical",
    "is_probe_slot",
    "probe_slot_instants",
    "require_replaceable",
    "require_sole_waiter",
]

FULL_HISTORY_NOT_ENABLED: Final = "FULL_HISTORY_NOT_ENABLED"
"""The v1 hard-deny signal for every historical manifest and every `full_history` label."""

_PAIRED_STRIDE_SECONDS: Final = 2
_RESTATING_LAG_DAYS: Final = 2


class RetrievalEncoding(StrEnum):
    """How the replay band encodes a dataset's run day in its retrieval instant."""

    STRIDED = "strided"
    PAIRED = "paired"


@dataclass(frozen=True, slots=True)
class HistoricalPolicy:
    """The replay band's constants, computed by the planner from its own output."""

    replay_base: datetime
    first_day: date
    cut: date
    """`backfill_last_day`: the newest day phase B covers."""
    stride_seconds: int
    seal_slot_base: int
    backfill_slot_base: int
    probe_slot_base: int
    probe_slot_count: int
    encodings: Mapping[tuple[str, str], RetrievalEncoding] = field(
        default_factory=dict[tuple[str, str], RetrievalEncoding]
    )
    """Keyed by `(source_system, source_dataset)`; a dataset absent here is never decoded."""

    def __post_init__(self) -> None:
        if self.replay_base.tzinfo is None:
            raise ValueError("the replay base must be timezone-aware.")
        if self.stride_seconds < 1 or self.probe_slot_count < 1:
            raise ValueError("the stride and the probe slot count must be positive.")
        if not self.seal_slot_base < self.backfill_slot_base < self.probe_slot_base:
            raise ValueError("seal, backfill and probe slot bases must strictly increase.")
        if self.cut < self.first_day:
            raise ValueError("the backfill cut precedes the first span day.")

    @property
    def probe_first_offset(self) -> int:
        """The first probe retrieval offset, in seconds from the replay base."""
        return self.probe_slot_base * self.stride_seconds

    @property
    def probe_end_offset(self) -> int:
        """One past the last probe retrieval offset."""
        return (self.probe_slot_base + self.probe_slot_count) * self.stride_seconds


@dataclass(frozen=True, slots=True)
class ManifestFacts:
    """The manifest authority the predicate reads: dataset, declared period and retrieval."""

    source_system: str
    source_dataset: str
    period_end: date
    retrieved_at: datetime


def _offset_seconds(facts: ManifestFacts, policy: HistoricalPolicy) -> int:
    if facts.retrieved_at.tzinfo is None:
        raise ValueError("a manifest retrieval instant must be timezone-aware.")
    delta = facts.retrieved_at.astimezone(UTC) - policy.replay_base.astimezone(UTC)
    return int(delta // timedelta(seconds=1))


def is_probe_slot(retrieved_at: datetime, policy: HistoricalPolicy) -> bool:
    """Whether an instant lies in the reserved probe retrieval slots."""
    facts = ManifestFacts("", "", policy.first_day, retrieved_at)
    offset = _offset_seconds(facts, policy)
    return policy.probe_first_offset <= offset < policy.probe_end_offset


def probe_slot_instants(policy: HistoricalPolicy) -> tuple[datetime, ...]:
    """Every reserved probe retrieval instant, oldest first."""
    base = policy.replay_base.astimezone(UTC)
    return tuple(
        base + timedelta(seconds=offset)
        for offset in range(policy.probe_first_offset, policy.probe_end_offset)
    )


def _strided_historical(offset: int, policy: HistoricalPolicy) -> bool:
    slot = offset // policy.stride_seconds
    if slot >= policy.backfill_slot_base:
        return True
    if slot >= policy.seal_slot_base:
        return False
    return policy.first_day + timedelta(days=slot) <= policy.cut


def _paired_historical(offset: int, policy: HistoricalPolicy) -> bool:
    day_index, variant = divmod(offset, _PAIRED_STRIDE_SECONDS)
    run_day = policy.first_day + timedelta(days=day_index + _RESTATING_LAG_DAYS * variant)
    return run_day <= policy.cut


def is_historical(facts: ManifestFacts, policy: HistoricalPolicy) -> bool:
    """Classify one manifest: True means admission class `historical` (refused in v1)."""
    offset = _offset_seconds(facts, policy)
    if offset < 0 or policy.probe_first_offset <= offset < policy.probe_end_offset:
        return False
    if offset >= policy.probe_end_offset:
        return facts.period_end <= policy.cut
    encoding = policy.encodings.get((facts.source_system, facts.source_dataset))
    if encoding is RetrievalEncoding.STRIDED:
        return _strided_historical(offset, policy)
    if encoding is RetrievalEncoding.PAIRED:
        return _paired_historical(offset, policy)
    return False


# --- The decision table (V-6, V-21, V-22, V-27, V-29; BF-7, BF-9) ------------------------------

DECISION_TABLE_NOT_TOTAL: Final = "DECISION_TABLE_NOT_TOTAL"
RUN_REQUEST_WAIT_SLOT_CONFLICT: Final = "RUN_REQUEST_WAIT_SLOT_CONFLICT"
ACTIVE_COMPUTE_NOT_REPLACEABLE: Final = "ACTIVE_COMPUTE_NOT_REPLACEABLE"
ACTIVE_RUN_GRANTED: Final = "ACTIVE_RUN_GRANTED"
SLOT_REVISION_CHANGED: Final = "SLOT_REVISION_CHANGED"


class DecisionTableError(ValueError):
    """The generated table is not total over its closed inputs (generator failure)."""


class AdmissionGuardError(Exception):
    """A domain guard: the proposed transition would break an invariant. No receipt.

    The service re-reads and re-decides, so a guard never reaches a caller as a final outcome.
    """

    def __init__(self, signal: str) -> None:
        super().__init__(signal)
        self.signal = signal


class CallerKind(StrEnum):
    """The closed caller kinds."""

    SCHEDULE = "schedule"
    DELIVERY_EVENT = "delivery_event"
    ON_DEMAND = "on_demand"


class AdmissionClass(StrEnum):
    """Derived by the service, never taken from the caller alone."""

    CADENCE = "cadence"
    BOUNDED_VALIDATION = "bounded_validation"
    HISTORICAL = "historical"


class RunState(StrEnum):
    """A run's lifecycle. `A` is only ever OPEN or PAUSED; the rest have released the slot."""

    OPEN = "OPEN"
    PAUSED = "PAUSED"
    FENCING = "FENCING"
    GRANTED = "GRANTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"


class MatchState(StrEnum):
    """What a request's fingerprint already names, other than `A` or `W`."""

    GRANTED = "GRANTED"
    SUCCEEDED = "SUCCEEDED"
    SHADOW_RECORDED = "SHADOW_RECORDED"
    LEGACY_COMPLETED = "LEGACY_COMPLETED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"
    FENCING = "FENCING"


_ACTIVE_STATES: Final = frozenset({RunState.OPEN, RunState.PAUSED})
_COMPLETED: Final = frozenset({MatchState.GRANTED, MatchState.SUCCEEDED})
_TERMINAL: Final = frozenset(
    {MatchState.FAILED, MatchState.SUPERSEDED, MatchState.CANCELLED, MatchState.FENCING}
)
_RUN_MATCHES: Final = _COMPLETED | _TERMINAL


@dataclass(frozen=True, slots=True)
class ActiveRun:
    """`A`: exactly the run `SLOT#<U>` names, in state OPEN or PAUSED."""

    run_id: str
    state: RunState
    fingerprint: str
    r: datetime
    compute_started: bool
    grant_id: str | None
    slot_revision: int
    root_clock_started_at: str | None = None
    """S5: the run's root clock, carried to the `FENCE#` a replacement writes. Not decided on."""

    def __post_init__(self) -> None:
        if self.state not in _ACTIVE_STATES:
            raise DecisionTableError(
                f"{DECISION_TABLE_NOT_TOTAL}: an active run in state {self.state} is not covered."
            )


@dataclass(frozen=True, slots=True)
class Waiter:
    """`W`: the one `WAITER#<U>` item."""

    request_id: str
    fingerprint: str
    r: datetime
    held: bool
    revision: int
    window: str | None = None
    """S4: the dataset window prefix, carried to the promoted run for its legacy-head read."""
    root_clock_started_at: str | None = None
    """S5: the waiter's root clock (its manifest's `LastModified`), carried to the promoted run so
    the waiter and the run it becomes are due from one clock. Not decided on."""


@dataclass(frozen=True, slots=True)
class FingerprintMatch:
    """A run, shadow graph or legacy plan the fingerprint already names."""

    state: MatchState
    run_id: str | None = None
    generation: int = 0
    issued_grant: bool = False
    legacy_plan_key: str | None = None


@dataclass(frozen=True, slots=True)
class DecisionInputs:
    """Every closed input the table reads; the service reads them strongly, the table decides."""

    request_digest: str
    receipt_digest: str | None
    """The digest on an existing receipt for this request ID, if one exists."""
    epoch: int
    admitting: bool
    """Mode is SHADOW or GRAPH."""
    hold: bool
    caller: CallerKind
    reason: str
    admission_class: AdmissionClass
    version_count: int
    blocked: bool
    fingerprint: str
    r: datetime
    active: ActiveRun | None
    waiter: Waiter | None
    match: FingerprintMatch | None
    head: datetime | None


@dataclass(frozen=True, slots=True)
class Decision:
    """The first matching row's outcome; `retry_generation` is set only through row 11."""

    row: int
    decision: str
    reason: str | None
    retry_generation: int | None = None


RETRY_ROW: Final = 11
RETRY_RESUMES_AT: Final = 13
_DATA_CORRECTION: Final = "data_correction"
_OPERATOR_RERUN: Final = "operator_rerun"


def _completed(inputs: DecisionInputs) -> bool:
    match = inputs.match
    if match is None:
        return False
    if match.state in _COMPLETED:
        return True
    if match.state is MatchState.SHADOW_RECORDED:
        return inputs.epoch == 0
    return match.state is MatchState.LEGACY_COMPLETED and inputs.epoch == 1


def _pre_grant_rerun(inputs: DecisionInputs) -> bool:
    match = inputs.match
    return (
        match is not None
        and match.state is MatchState.FAILED
        and not match.issued_grant
        and inputs.reason == _OPERATOR_RERUN
        and inputs.caller is CallerKind.ON_DEMAND
    )


def _computing(active: ActiveRun | None) -> bool:
    return active is not None and active.compute_started


CONDITIONS: Final[Mapping[str, Callable[[DecisionInputs], bool]]] = {
    "receipt_same_digest": lambda i: i.receipt_digest == i.request_digest,
    "receipt_other_digest": lambda i: (
        i.receipt_digest is not None and i.receipt_digest != i.request_digest
    ),
    "mode_not_admitting": lambda i: not i.admitting,
    "class_historical": lambda i: i.admission_class is AdmissionClass.HISTORICAL,
    "key_rewritten": lambda i: i.version_count > 1,
    "unit_blocked": lambda i: i.blocked,
    "fingerprint_is_active": lambda i: (
        i.active is not None and i.active.fingerprint == i.fingerprint
    ),
    "fingerprint_is_waiter": lambda i: (
        i.waiter is not None
        and i.waiter.fingerprint == i.fingerprint
        and not (i.waiter.held and i.caller is CallerKind.ON_DEMAND)
    ),
    "fingerprint_completed": _completed,
    "correction_of_existing": lambda i: (
        i.reason == _DATA_CORRECTION and i.match is not None and i.match.state in _RUN_MATCHES
    ),
    "pre_grant_failure_rerun": _pre_grant_rerun,
    "fingerprint_terminal": lambda i: i.match is not None and i.match.state in _TERMINAL,
    "older_than_current": lambda i: i.head is not None and i.r < i.head,
    "older_than_waiting": lambda i: i.waiter is not None and i.r < i.waiter.r,
    "older_than_active": lambda i: i.active is not None and i.r < i.active.r,
    "automatic_held": lambda i: (
        i.epoch == 1 and i.hold and i.caller in {CallerKind.SCHEDULE, CallerKind.DELIVERY_EVENT}
    ),
    "active_before_compute": lambda i: (
        i.epoch == 1
        and i.active is not None
        and i.active.state is RunState.OPEN
        and not i.active.compute_started
    ),
    # Row 18 applies only to an OPEN run, as row 17 does: the replace CAS requires OPEN, so a
    # PAUSED computing run queues at row 19 and the guard cannot livelock (S1A-5).
    "correction_of_computing": lambda i: (
        i.epoch == 1
        and _computing(i.active)
        and i.active is not None
        and i.active.state is RunState.OPEN
        and i.active.grant_id is None
        and i.reason == _DATA_CORRECTION
    ),
    "active_exists": lambda i: i.epoch == 1 and i.active is not None,
    "otherwise": lambda _i: True,
}
"""Each decision-table row's condition, keyed by the contract's `when` identifier."""


def decide(rows: tuple[Mapping[str, object], ...], inputs: DecisionInputs) -> Decision:
    """Return the first matching row's decision. Row 11 sets identity and continues to 13."""
    retry: int | None = None
    resume_at = 0
    for row in rows:
        number = int(cast_int(row["row"]))
        if number < resume_at:
            continue
        if not CONDITIONS[str(row["when"])](inputs):
            continue
        if "continue_to" in row:
            match = inputs.match
            retry = (0 if match is None else match.generation) + 1
            resume_at = int(cast_int(row["continue_to"]))
            continue
        reason = row.get("reason")
        return Decision(
            number, str(row["decision"]), None if reason is None else str(reason), retry
        )
    raise DecisionTableError(f"{DECISION_TABLE_NOT_TOTAL}: no row matched.")


def cast_int(value: object) -> int:
    """Narrow a contract integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise DecisionTableError("a decision-table row number is not an integer.")
    return value


def require_sole_waiter(existing: Waiter | None, *, supersedes: bool) -> None:
    """BF-7: a transition may add a waiter only by superseding the one already there."""
    if existing is not None and not supersedes:
        raise AdmissionGuardError(RUN_REQUEST_WAIT_SLOT_CONFLICT)


def require_replaceable(active: ActiveRun, *, invalidating: bool) -> None:
    """BF-9 and V-29: replace only an ungranted run, and only before compute unless invalidating."""
    if active.grant_id is not None:
        raise AdmissionGuardError(ACTIVE_RUN_GRANTED)
    if active.compute_started and not invalidating:
        raise AdmissionGuardError(ACTIVE_COMPUTE_NOT_REPLACEABLE)


def derive_admission_class(
    facts: ManifestFacts, policy: HistoricalPolicy, reason: str
) -> AdmissionClass:
    """Derive the class from manifest authority; a caller label never decides it."""
    if is_historical(facts, policy):
        return AdmissionClass.HISTORICAL
    if reason == "bounded_validation":
        return AdmissionClass.BOUNDED_VALIDATION
    return AdmissionClass.CADENCE


# --- Totality (V-6) -------------------------------------------------------------------------

_R: Final = datetime(2026, 1, 1, 12, tzinfo=UTC)
_OLDER: Final = _R - timedelta(seconds=1)
_NEWER: Final = _R + timedelta(seconds=1)
_CALLER_REASONS: Final = (
    (CallerKind.SCHEDULE, "scheduled_cadence"),
    (CallerKind.DELIVERY_EVENT, "delivery_arrived"),
    (CallerKind.ON_DEMAND, "operator_rerun"),
    (CallerKind.ON_DEMAND, "bounded_validation"),
    (CallerKind.ON_DEMAND, _DATA_CORRECTION),
)


def _actives(fingerprint: str) -> list[ActiveRun | None]:
    found: list[ActiveRun | None] = [None]
    for state in (RunState.OPEN, RunState.PAUSED):
        for compute, grant in ((False, None), (True, None), (True, "grant")):
            if state is RunState.PAUSED and grant is not None:
                continue
            for same in (True, False):
                for r in (_R,) if same else (_OLDER, _NEWER):
                    found.append(
                        ActiveRun("a", state, fingerprint if same else "fa", r, compute, grant, 1)
                    )
    return found


def _waiters(fingerprint: str) -> list[Waiter | None]:
    found: list[Waiter | None] = [None]
    for held in (False, True):
        for same in (True, False):
            for r in (_R,) if same else (_OLDER, _NEWER):
                found.append(Waiter("w", fingerprint if same else "fw", r, held, 1))
    return found


def _matches() -> list[FingerprintMatch | None]:
    found: list[FingerprintMatch | None] = [None]
    for state in MatchState:
        grants = (False, True) if state is MatchState.FAILED else (state in _COMPLETED,)
        found.extend(FingerprintMatch(state, "m", 0, grant) for grant in grants)
    return found


def _inputs(retry_approved: bool) -> list[DecisionInputs]:
    """Enumerate the closed input domains, with impossible combinations removed.

    A fingerprint names at most one of `A`, `W` and a recorded match. Outside GRAPH (epoch 0)
    no `A`, `W` or hold exists. Rows 1 and 2 read only the receipt, so a present receipt is
    enumerated against one representative of everything else.
    """
    del retry_approved
    f = "f"
    found: list[DecisionInputs] = []
    base = DecisionInputs(
        "d",
        None,
        0,
        True,
        False,
        CallerKind.ON_DEMAND,
        "operator_rerun",
        AdmissionClass.CADENCE,
        1,
        False,
        f,
        _R,
        None,
        None,
        None,
        None,
    )
    found.extend(replace(base, receipt_digest=digest) for digest in ("d", "x"))
    for epoch, admitting in ((0, False), (0, True), (1, True)):
        for hold in (False, True) if epoch == 1 else (False,):
            for caller, reason in _CALLER_REASONS:
                for cls in AdmissionClass:
                    for versions in (1, 2):
                        for blocked in (False, True):
                            for active in _actives(f) if epoch == 1 else [None]:
                                for waiter in _waiters(f) if epoch == 1 else [None]:
                                    for match in _matches():
                                        same = [
                                            active is not None and active.fingerprint == f,
                                            waiter is not None and waiter.fingerprint == f,
                                            match is not None,
                                        ]
                                        if sum(same) > 1:
                                            continue
                                        for head in (None, _OLDER, _NEWER):
                                            found.append(
                                                DecisionInputs(
                                                    "d",
                                                    None,
                                                    epoch,
                                                    admitting,
                                                    hold,
                                                    caller,
                                                    reason,
                                                    cls,
                                                    versions,
                                                    blocked,
                                                    f,
                                                    _R,
                                                    active,
                                                    waiter,
                                                    match,
                                                    head,
                                                )
                                            )
    return found


def check_decision_table(rows: tuple[Mapping[str, object], ...], *, retry_approved: bool) -> None:
    """Prove the table total (V-6): fails with `DECISION_TABLE_NOT_TOTAL` on any gap.

    The rows must be exactly the closed row set, each condition known, row 20 last and always
    true, every row reachable as a first match, rows 16-19 never matching in epoch 0, row 11
    present only when approved, deciding nothing and always continuing to row 13.
    """
    numbers = [cast_int(row["row"]) for row in rows]
    expected = [n for n in range(1, 21) if retry_approved or n != RETRY_ROW]
    if numbers != expected:
        raise DecisionTableError(f"{DECISION_TABLE_NOT_TOTAL}: rows {numbers} != {expected}.")
    for row in rows:
        if str(row["when"]) not in CONDITIONS:
            raise DecisionTableError(
                f"{DECISION_TABLE_NOT_TOTAL}: unknown condition {row['when']}."
            )
    if rows[-1]["when"] != "otherwise":
        raise DecisionTableError(f"{DECISION_TABLE_NOT_TOTAL}: the last row must be 'otherwise'.")
    for row in rows:
        is_retry = "continue_to" in row
        if is_retry != (row["row"] == RETRY_ROW) or (
            is_retry and row["continue_to"] != RETRY_RESUMES_AT
        ):
            raise DecisionTableError(
                f"{DECISION_TABLE_NOT_TOTAL}: only row 11 continues, and only to row 13."
            )
        if bool(row.get("epoch1_only")) != (16 <= cast_int(row["row"]) <= 19):  # noqa: PLR2004
            raise DecisionTableError(f"{DECISION_TABLE_NOT_TOTAL}: rows 16-19 are epoch-1 only.")
    reached: set[int] = set()
    for inputs in _inputs(retry_approved):
        decision = decide(rows, inputs)
        if inputs.epoch == 0 and 16 <= decision.row <= 19:  # noqa: PLR2004
            raise DecisionTableError(f"{DECISION_TABLE_NOT_TOTAL}: row {decision.row} in epoch 0.")
        reached.add(decision.row)
        if decision.retry_generation is not None:
            reached.add(RETRY_ROW)
    missing = sorted(set(numbers) - reached)
    if missing:
        raise DecisionTableError(f"{DECISION_TABLE_NOT_TOTAL}: rows {missing} are unreachable.")
