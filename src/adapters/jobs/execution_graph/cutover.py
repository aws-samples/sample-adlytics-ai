# SPDX-License-Identifier: MIT-0
"""The S6 drain-to-zero cutover commands (design.md, "Legacy cutover", steps 3-6; V-17b, V-18).

Human-only, through the operator CLI (`operator.py`): each command is refused for an agent
principal before any read-modify-write. The commands:

- `begin_drain`: the `LEGACY|SHADOW -> LEGACY_DRAINING` CAS with the operator's deadline `D`,
  refused unless `D` lies between drain CAS + `T_max` + margin and the generated visibility
  bound (`DRAIN_DEADLINE_OUT_OF_BOUND`). The receipt records `D` (epoch 0).
- `abort_drain`: `LEGACY_DRAINING -> LEGACY`, refused with `CUTOVER_ABORT_NOT_PERMITTED` and no
  receipt when `graph_entered_at` is present; the CAS itself also conditions on its absence.
- `counted_enumeration`: read `DRAIN_SEQ`, enumerate F1-F4 by one consistent base-table scan,
  read `DRAIN_SEQ` again. Refused before drain CAS + `T_max` + margin (`DRAIN_FENCE_NOT_ELAPSED`).
- `enter_graph`: the zero proof and the `GRAPH` CAS. It reads `DRAIN_SEQ = s`, runs one complete
  enumeration, refuses unless every family is zero (`DRAIN_NOT_ZERO`), then commits the CAS with
  the hold and `graph_entered_at`, ConditionChecking `DRAIN_SEQ.seq = s` (`DRAIN_SEQ_CHANGED`
  repeats the proof). It then runs the post-CAS enumeration (P6(d')) and returns it; a non-zero
  post-CAS read is a stop with the hold kept set, which the operator reads from the result. A
  post-CAS read that fails does not hide the committed CAS: the result carries the receipt with
  the enumeration marked unread.
- `post_graph_enumeration`: the uncounted, read-only F1-F4 re-read, permitted only in `GRAPH`
  with `graph_entered_at` set. It repeats P6(d') after a failed read, a dead process or an
  ambiguous commit, and it is the post-`GRAPH` form of the F1-F4 invariant.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from adapters.outbound.dynamodb.legacy_families import FamilyEnumeration, ZeroProofUnreadableError
from domain.operations.cutover import (
    CUTOVER_REQUIRES_ZERO_PROOF,
    require_abort_permitted,
    require_drain_deadline,
    require_enumeration_permitted,
    require_post_graph_read_permitted,
    require_zero,
)
from domain.operations.execution_graph import (
    AuthorityMode,
    GraphCommandRefusedError,
    ModeGuardError,
    ModeState,
    epoch_for_mode,
)
from domain.operations.generated_execution_graphs import PROTOCOL_VERSION, T_MAX_SECONDS
from ports.outbound.execution_graph import (
    CommandReceipt,
    ExecutionGraphStore,
    GraphEntry,
    ModeChange,
)

__all__ = [
    "CutoverCommand",
    "CutoverDependencies",
    "DrainProof",
    "GraphEntryResult",
    "LegacyFamilyReader",
    "PostGraphRead",
    "abort_drain",
    "begin_drain",
    "counted_enumeration",
    "enter_graph",
    "post_graph_enumeration",
    "refuse_plain_graph_cas",
]


class LegacyFamilyReader(Protocol):
    """The consistent base-table reads the zero proof uses."""

    @property
    def table(self) -> str:
        """The coordinator table `DRAIN_SEQ` lives in."""
        ...

    def read_drain_seq(self) -> int:
        """Read `DRAIN_SEQ.seq` strongly (0 when absent)."""
        ...

    def enumerate_families(self) -> FamilyEnumeration:
        """Run one complete consistent enumeration of F1-F4."""
        ...


@dataclass(frozen=True, slots=True)
class CutoverDependencies:
    """The graph store, the legacy family reader and the clock the cutover runs against."""

    store: ExecutionGraphStore
    families: LegacyFamilyReader
    now: Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class CutoverCommand:
    """Who issues one cutover CAS, under which command id, against which mode revision."""

    principal: str
    command_id: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class DrainProof:
    """One counted enumeration: `DRAIN_SEQ` before and after, and the families between."""

    mode: ModeState
    drain_seq_before: int
    enumeration: FamilyEnumeration
    drain_seq_after: int
    started_at: str

    @property
    def stable(self) -> bool:
        """Whether no counted family-changing commit landed during the enumeration."""
        return self.drain_seq_before == self.drain_seq_after


@dataclass(frozen=True, slots=True)
class GraphEntryResult:
    """The committed `GRAPH` CAS, the proof it was bound to and the post-CAS enumeration.

    `post_enumeration` is `None` when the post-CAS read failed; `post_unread` then says why. The
    CAS has committed either way, so the stop rule reads the receipt plus a later
    `post_graph_enumeration`.
    """

    receipt: CommandReceipt
    proof: DrainProof
    post_enumeration: FamilyEnumeration | None
    post_unread: str | None = None


@dataclass(frozen=True, slots=True)
class PostGraphRead:
    """One uncounted post-`GRAPH` F1-F4 read and the mode it was permitted under."""

    mode: ModeState
    enumeration: FamilyEnumeration
    started_at: str


def _receipt(
    command: CutoverCommand,
    transition: tuple[ModeState, AuthorityMode, str],
    details: dict[str, str],
) -> CommandReceipt:
    current, to_mode, recorded_at = transition
    epoch_mode = to_mode if to_mode is AuthorityMode.GRAPH else current.mode
    return CommandReceipt(
        command_id=command.command_id,
        command="set_mode",
        principal=command.principal,
        authority_epoch=epoch_for_mode(epoch_mode),
        mode=current.mode,
        outcome="committed",
        recorded_at=recorded_at,
        protocol_version=PROTOCOL_VERSION,
        details={"from": current.mode.value, "to": to_mode.value, **details},
    )


def _expect(current: ModeState, expected_revision: int) -> None:
    if current.revision != expected_revision:
        raise ModeGuardError("the mode item moved since it was read.")


def refuse_plain_graph_cas() -> None:
    """`LEGACY_DRAINING -> GRAPH` exists only as `enter_graph`, never as a plain `set-mode`."""
    raise GraphCommandRefusedError(
        CUTOVER_REQUIRES_ZERO_PROOF, "GRAPH is entered only through the zero-proof command."
    )


def begin_drain(
    deps: CutoverDependencies, command: CutoverCommand, deadline: datetime
) -> CommandReceipt:
    """Commit the drain CAS with `D`, after checking `D` against both generated bounds."""
    current = deps.store.read_mode()
    _expect(current, command.expected_revision)
    if current.mode not in {AuthorityMode.LEGACY, AuthorityMode.SHADOW}:
        raise GraphCommandRefusedError(
            "MODE_TRANSITION_NOT_PERMITTED", f"{current.mode} -> LEGACY_DRAINING is not legal."
        )
    now = deps.now().astimezone(UTC)
    require_drain_deadline(now, deadline.astimezone(UTC))
    recorded_at = now.isoformat()
    drain_deadline = deadline.astimezone(UTC).isoformat()
    receipt = _receipt(
        command,
        (current, AuthorityMode.LEGACY_DRAINING, recorded_at),
        {"drain_deadline": drain_deadline, "t_max_seconds": str(T_MAX_SECONDS)},
    )
    deps.store.commit_mode(
        ModeChange(current, AuthorityMode.LEGACY_DRAINING, receipt, drain_deadline=drain_deadline)
    )
    return receipt


def abort_drain(deps: CutoverDependencies, command: CutoverCommand) -> CommandReceipt:
    """Commit `LEGACY_DRAINING -> LEGACY`; refused (no receipt) once `graph_entered_at` exists."""
    current = deps.store.read_mode()
    require_abort_permitted(current)
    _expect(current, command.expected_revision)
    receipt = _receipt(
        command,
        (current, AuthorityMode.LEGACY, deps.now().astimezone(UTC).isoformat()),
        {"abort": "true", "drain_deadline": current.drain_deadline or ""},
    )
    deps.store.commit_mode(ModeChange(current, AuthorityMode.LEGACY, receipt))
    return receipt


def counted_enumeration(deps: CutoverDependencies) -> DrainProof:
    """Read `DRAIN_SEQ`, enumerate F1-F4, read `DRAIN_SEQ` again (P6(c))."""
    mode = deps.store.read_mode()
    started = deps.now().astimezone(UTC)
    require_enumeration_permitted(mode, started)
    before = deps.families.read_drain_seq()
    enumeration = deps.families.enumerate_families()
    after = deps.families.read_drain_seq()
    return DrainProof(mode, before, enumeration, after, started.isoformat())


def enter_graph(deps: CutoverDependencies, command: CutoverCommand) -> GraphEntryResult:
    """Prove zero, commit the `GRAPH` CAS bound to the read `DRAIN_SEQ`, enumerate again."""
    proof = counted_enumeration(deps)
    _expect(proof.mode, command.expected_revision)
    require_zero(proof.enumeration.counts)
    receipt = _receipt(
        command,
        (proof.mode, AuthorityMode.GRAPH, deps.now().astimezone(UTC).isoformat()),
        {
            "drain_seq": str(proof.drain_seq_before),
            "families": ",".join(
                f"{name}={value}" for name, value in proof.enumeration.counts.as_dict().items()
            ),
            "hold": "set",
        },
    )
    deps.store.commit_mode(
        ModeChange(
            proof.mode,
            AuthorityMode.GRAPH,
            receipt,
            graph_entry=GraphEntry(deps.families.table, proof.drain_seq_before),
        )
    )
    try:
        post = deps.families.enumerate_families()
    except ZeroProofUnreadableError as error:
        return GraphEntryResult(receipt, proof, None, error.description)
    return GraphEntryResult(receipt, proof, post)


def post_graph_enumeration(deps: CutoverDependencies) -> PostGraphRead:
    """Re-read F1-F4 after the `GRAPH` CAS: uncounted, read-only, `GRAPH` only (P6(d'))."""
    mode = deps.store.read_mode()
    require_post_graph_read_permitted(mode)
    started = deps.now().astimezone(UTC).isoformat()
    return PostGraphRead(mode, deps.families.enumerate_families(), started)
