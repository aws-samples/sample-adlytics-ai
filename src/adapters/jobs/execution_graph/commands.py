# SPDX-License-Identifier: MIT-0
"""Human-only run and hold commands for the operator CLI (S3, BF-2, V-14, V-29).

Each command writes exactly one immutable command receipt: with its effect when it commits, or
alone when it is refused in the domain.

- `pause_plan` moves an OPEN run to PAUSED and advances its dispatch generation, so a READY
  node's permit is refused with `PLAN_DISPATCH_FENCED` until resume (BF-2). READY nodes keep
  their locks.
- `resume_plan` moves a PAUSED run to OPEN, advances the generation again and re-arms every
  READY node's outbox row under it, so each sends once.
- `cancel_plan` is release point 1: FENCING, generation + 1, slot freed, `PROMOTE#` woken.
  Claimed units still finish; the fence settler settles the rest.
- Pause and cancel are conditioned on `grant_id` absent: against a run whose grant committed
  and has not applied they are refused with `RUN_GRANT_PENDING_APPLY` (V-29, r5).
- `set_automatic_admission_hold` / `clear_automatic_admission_hold` change the hold on the mode
  item and are accepted only in GRAPH; elsewhere they are refused with
  `AUTOMATIC_ADMISSION_HOLD_REQUIRES_GRAPH` and the hold is unchanged (V-14(b), r5).

An exact replay of a command ID returns the receipt already written and writes nothing; the
same ID naming a different command or run is refused `COMMAND_ID_CONFLICT` before any write. A
run command whose `RUN#` revision moved between its read and its commit (a first claim or an
arming bumps it) re-reads and decides again, within the generated re-decide bound, before it is
refused `RUN_STATE_CHANGED` (S3V-12). Every guarded write first re-reads the command ID, so a
concurrent twin that committed the same command first is answered with the twin's receipt, as
a sequential replay is (S3V2-3).

The principal check (`AGENT_MUTATION_NOT_ENABLED`) and the protocol gate run before any read,
in the CLI.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from adapters.outbound.dynamodb.execution_graph import due_shard
from domain.operations.execution_graph import (
    AuthorityMode,
    GraphCommandRefusedError,
    epoch_for_mode,
)
from domain.operations.execution_graph_dispatch import (
    AUTOMATIC_ADMISSION_HOLD_REQUIRES_GRAPH,
    RUN_GRANT_PENDING_APPLY,
    RUN_STATE_CHANGED,
    FenceReason,
    GraphGuardError,
    NodeState,
)
from domain.operations.generated_execution_graphs import BOUNDS, PROTOCOL_VERSION
from domain.operations.run_request import RunState
from ports.outbound.execution_graph import CommandReceipt
from ports.outbound.execution_graph_dispatch import (
    FenceRunRequest,
    GraphDispatchStore,
    HoldRequest,
    OutboxRecord,
    RunCommand,
    RunRecord,
)

__all__ = [
    "COMMITTED",
    "CommandResult",
    "OperatorCommand",
    "RunCommandKind",
    "run_operator_command",
]

COMMITTED: Final = "committed"
RUN_NOT_FOUND: Final = "RUN_NOT_FOUND"
COMMAND_ID_CONFLICT: Final = "COMMAND_ID_CONFLICT"
_REDECIDE: Final = int(BOUNDS["max_redecide"])
_REFUSED: Final = "refused:"


class RunCommandKind(StrEnum):
    """The closed human-only run and hold commands."""

    PAUSE = "pause_plan"
    RESUME = "resume_plan"
    CANCEL = "cancel_plan"
    SET_HOLD = "set_automatic_admission_hold"
    CLEAR_HOLD = "clear_automatic_admission_hold"


@dataclass(frozen=True, slots=True)
class OperatorCommand:
    """One verified human command."""

    kind: RunCommandKind
    command_id: str
    principal: str
    now: Callable[[], str]
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The receipt the command wrote and whether it committed its effect."""

    receipt: CommandReceipt
    committed: bool
    details: dict[str, str] = field(default_factory=dict[str, str])


def _receipt(
    command: OperatorCommand, state: tuple[AuthorityMode, str, dict[str, str]]
) -> CommandReceipt:
    mode, outcome, details = state
    return CommandReceipt(
        command_id=command.command_id,
        command=command.kind.value,
        principal=command.principal,
        authority_epoch=epoch_for_mode(mode),
        mode=mode,
        outcome=outcome,
        recorded_at=command.now(),
        protocol_version=PROTOCOL_VERSION,
        details=details,
    )


def _refuse(
    store: GraphDispatchStore,
    context: tuple[OperatorCommand, AuthorityMode, dict[str, str]],
    signal: str,
) -> CommandResult:
    command, mode, details = context
    receipt = _receipt(command, (mode, f"refused:{signal}", details))
    try:
        store.record_command(receipt)
    except GraphGuardError:
        twin = _replayed(store, command)
        if twin is None:
            raise
        return twin
    return CommandResult(receipt, committed=False, details={"signal": signal})


def _hold(store: GraphDispatchStore, command: OperatorCommand) -> CommandResult:
    mode = store.read_mode()
    hold = command.kind is RunCommandKind.SET_HOLD
    details = {"hold": str(hold).lower(), "workload": "ingestion"}
    if mode.mode is not AuthorityMode.GRAPH:
        return _refuse(
            store, (command, mode.mode, details), AUTOMATIC_ADMISSION_HOLD_REQUIRES_GRAPH
        )
    receipt = _receipt(command, (mode.mode, COMMITTED, details))
    try:
        store.set_hold(HoldRequest(mode, hold, receipt))
    except GraphGuardError as guard:
        twin = _replayed(store, command)
        if twin is not None:
            return twin
        return _refuse(store, (command, store.read_mode().mode, details), guard.signal)
    return CommandResult(receipt, committed=True)


def _ready_outboxes(store: GraphDispatchStore, run_id: str, unit: str) -> tuple[OutboxRecord, ...]:
    found: list[OutboxRecord] = []
    for node in store.read_nodes(run_id):
        if node.state is not NodeState.READY:
            continue
        outbox = store.read_outbox(due_shard(unit), node.node_id)
        if outbox is not None and outbox.state == "OPEN":
            found.append(outbox)
    return tuple(found)


_REQUIRED_STATE: Final = {
    RunCommandKind.PAUSE: frozenset({RunState.OPEN}),
    RunCommandKind.RESUME: frozenset({RunState.PAUSED}),
    RunCommandKind.CANCEL: frozenset({RunState.OPEN, RunState.PAUSED}),
}


def _replayed(store: GraphDispatchStore, command: OperatorCommand) -> CommandResult | None:
    """Return the receipt an exact replay already wrote; refuse a reused ID (S3V-12)."""
    existing = store.read_command(command.command_id)
    if existing is None:
        return None
    same_run = command.run_id is None or existing.details.get("run_id") == command.run_id
    if existing.command != command.kind.value or not same_run:
        raise GraphCommandRefusedError(
            COMMAND_ID_CONFLICT, "the command id already names a different command."
        )
    committed = existing.outcome == COMMITTED
    details = {} if committed else {"signal": existing.outcome.removeprefix(_REFUSED)}
    return CommandResult(existing, committed=committed, details=details)


def _apply(
    store: GraphDispatchStore,
    command: OperatorCommand,
    target: tuple[RunRecord, CommandReceipt],
) -> None:
    run, receipt = target
    if command.kind is RunCommandKind.PAUSE:
        store.pause_run(RunCommand(run, receipt))
    elif command.kind is RunCommandKind.RESUME:
        ready = _ready_outboxes(store, run.run_id, run.conflict_unit)
        store.resume_run(RunCommand(run, receipt, ready))
    else:
        store.fence_run(FenceRunRequest(run, FenceReason.CANCELLED, receipt.recorded_at, receipt))


def run_operator_command(  # noqa: PLR0911 -- one return per receipt outcome
    store: GraphDispatchStore, command: OperatorCommand
) -> CommandResult:
    """Run one human command and write exactly one command receipt."""
    replayed = _replayed(store, command)
    if replayed is not None:
        return replayed
    if command.kind in {RunCommandKind.SET_HOLD, RunCommandKind.CLEAR_HOLD}:
        return _hold(store, command)
    mode = store.read_mode().mode
    details = {"run_id": command.run_id or ""}
    context = (command, mode, details)
    signal = RUN_STATE_CHANGED
    for _ in range(_REDECIDE):
        run = None if command.run_id is None else store.read_run(command.run_id)
        if run is None:
            return _refuse(store, context, RUN_NOT_FOUND)
        if command.kind is not RunCommandKind.RESUME and run.grant_id is not None:
            return _refuse(store, context, RUN_GRANT_PENDING_APPLY)
        if run.state not in _REQUIRED_STATE[command.kind]:
            return _refuse(store, context, RUN_STATE_CHANGED)
        receipt = _receipt(command, (mode, COMMITTED, details))
        try:
            _apply(store, command, (run, receipt))
        except GraphGuardError as guard:
            twin = _replayed(store, command)
            if twin is not None:
                return twin  # a concurrent twin of this command committed first (S3V2-3)
            signal = guard.signal
            if signal == RUN_STATE_CHANGED:
                continue  # a claim or an arming bumped the revision: re-read and decide again
            return _refuse(store, context, signal)
        return CommandResult(receipt, committed=True)
    return _refuse(store, context, signal)
