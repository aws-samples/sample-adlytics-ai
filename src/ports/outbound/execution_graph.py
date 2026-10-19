# SPDX-License-Identifier: MIT-0
"""Outbound port for the retained execution-graph store (S1).

Reads are strongly consistent. Every admission decision commits its receipt and its effect in
one transaction (BF-10): either both commit or neither does. A transaction whose read-time
conditions no longer hold raises `AdmissionConflictError` (a domain guard, no receipt); the service
re-reads and re-decides. Any other store failure raises `RunRequestNotRecordedError`, which commits
nothing and is recoverable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from domain.operations.execution_graph import AuthorityMode, ModeState
from domain.operations.run_request import ActiveRun, FingerprintMatch, Waiter
from domain.shared_kernel.errors import DependencyError, ErrorCode

__all__ = [
    "AdmissionCommit",
    "AdmissionConflictError",
    "CommandReceipt",
    "EffectKind",
    "ExecutionGraphStore",
    "GraphEntry",
    "ManifestRead",
    "ManifestReader",
    "ModeChange",
    "NewRun",
    "ReceiptRecord",
    "ReplacedRun",
    "RollingFreshnessFence",
    "RollingFreshnessFenceChange",
    "RollingFreshnessFenceRead",
    "RunRequestNotRecordedError",
    "ShadowGraph",
    "UnitState",
    "conflict_receipt_sk",
]

RUN_REQUEST_NOT_RECORDED = "RUN_REQUEST_NOT_RECORDED"


class RunRequestNotRecordedError(DependencyError):
    """The receipt transaction failed: nothing committed; the request may be retried."""

    def __init__(self, description: str) -> None:
        super().__init__(ErrorCode("RUN_REQUEST", 10), description)
        self.signal = RUN_REQUEST_NOT_RECORDED


class AdmissionConflictError(Exception):
    """A read-time condition moved before commit (a domain guard): re-read and re-decide."""

    def __init__(self, signal: str) -> None:
        super().__init__(signal)
        self.signal = signal


@dataclass(frozen=True, slots=True)
class ReceiptRecord:
    """One immutable `RunRequestReceipt.v1`, or a supersession receipt for a waiter."""

    request_id: str
    request_digest: str
    decision: str
    reason: str | None
    row: int | None
    authority_epoch: int
    mode: AuthorityMode
    conflict_unit: str
    input_revision: str
    fingerprint: str
    recorded_at: str
    protocol_version: str
    contract_revision: int
    run_id: str | None = None
    graph_id: str | None = None
    root_flow_id: str | None = None
    retry_generation: int | None = None
    superseded_request_id: str | None = None
    legacy_plan_key: str | None = None
    caller_kind: str | None = None
    caller_reason: str | None = None
    requester: str | None = None
    joined_request_id: str | None = None
    """Row 8: the waiter whose request this one joined."""
    retried_run_id: str | None = None
    """Row 11 (V-22): the failed run whose retry identity this receipt carries."""
    root_clock_started_at: str | None = None
    """S5: the manifest version's `LastModified` admission read (FR-2), when it read one. The
    status projection measures root-to-decision elapsed from it (FR-3)."""
    manifest_key: str | None = None
    version_id: str | None = None
    """S6 §2.4: the admitted manifest version (`RunRequest.v1` requires both), recorded on every
    epoch-1 receipt so the `materialize_raw` node reads its inputs from the admitting receipt."""


def conflict_receipt_sk(request_digest: str) -> str:
    """Row 2's own receipt key: one per conflicting digest beside the original receipt."""
    return f"CONFLICT#{request_digest}"


@dataclass(frozen=True, slots=True)
class UnitState:
    """`U`'s slot, waiter, head and block, read strongly."""

    active: ActiveRun | None
    waiter: Waiter | None
    head: datetime | None
    blocked: bool
    slot_revision: int


class EffectKind(StrEnum):
    """What a decision writes beside its receipt."""

    NONE = "none"
    SHADOW_GRAPH = "shadow_graph"
    ADMIT_RUN = "admit_run"
    REPLACE_RUN = "replace_run"
    QUEUE_WAITER = "queue_waiter"


@dataclass(frozen=True, slots=True)
class ShadowGraph:
    """An immutable epoch-0 graph created directly in `SHADOW_RECORDED` (V-5)."""

    graph_id: str
    template: str
    nodes: tuple[str, ...]
    fingerprint: str
    root_flow_id: str


@dataclass(frozen=True, slots=True)
class NewRun:
    """The run a decision makes `A`, with its graph's `INSTANTIATE#` obligation."""

    run_id: str
    graph_id: str
    fingerprint: str
    retrieved_at: str
    root_flow_id: str
    generation: int
    template: str
    window: str | None = None
    """S4: the dataset window prefix; release reads `U`'s legacy head under it."""
    root_clock_started_at: str | None = None
    """S5: the root clock every obligation of this run is due from (FR-2, FR-7 kept rule)."""


@dataclass(frozen=True, slots=True)
class ReplacedRun:
    """The run rows 17 and 18 move to FENCING in the replacing transaction."""

    run_id: str
    invalidating: bool
    root_clock_started_at: str | None = None
    """S5: the replaced run's own root clock, stamped on its `FENCE#` obligation."""


@dataclass(frozen=True, slots=True)
class AdmissionCommit:
    """One decision's receipt and effect, committed atomically (BF-10)."""

    receipt: ReceiptRecord
    kind: EffectKind
    conflict_unit: str
    slot_revision: int
    shadow_graph: ShadowGraph | None = None
    new_run: NewRun | None = None
    replaced: ReplacedRun | None = None
    new_waiter: Waiter | None = None
    superseded_waiter: Waiter | None = None
    wake_held: bool = False
    receipt_sk: str = "RECEIPT"
    """`RECEIPT`, or row 2's `CONFLICT#<digest>` (the original receipt already holds `RECEIPT`)."""


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """One immutable operator command receipt (epoch 0 outside GRAPH)."""

    command_id: str
    command: str
    principal: str
    authority_epoch: int
    mode: AuthorityMode
    outcome: str
    recorded_at: str
    protocol_version: str
    details: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class RollingFreshnessFence:
    """The retained one-time migration fence over the old serving writers."""

    migration_id: str
    owner: str
    set_at: str
    revision: int
    state: str = "SET"


@dataclass(frozen=True, slots=True)
class RollingFreshnessFenceChange:
    """A held-GRAPH fence write and its immutable operator receipt."""

    fence: RollingFreshnessFence
    receipt: CommandReceipt
    expected_mode_revision: int


@dataclass(frozen=True, slots=True)
class RollingFreshnessFenceRead:
    """Strong mode, fence, and matching receipt reads returned together."""

    mode: ModeState
    fence: RollingFreshnessFence | None
    receipt: CommandReceipt | None


@dataclass(frozen=True, slots=True)
class ModeChange:
    """A mode CAS and its command receipt, in one transaction."""

    expected: ModeState
    to_mode: AuthorityMode
    receipt: CommandReceipt
    drain_deadline: str | None = None
    """S6: `D`, recorded on the mode item with the drain CAS (and in the receipt)."""
    graph_entry: GraphEntry | None = None
    """S6: the `GRAPH` CAS's zero-proof binding; required for `LEGACY_DRAINING -> GRAPH`."""


@dataclass(frozen=True, slots=True)
class GraphEntry:
    """The `GRAPH` CAS binding: the `DRAIN_SEQ` value the zero proof read, and where it lives."""

    coordinator_table: str
    drain_seq: int
    """The `DRAIN_SEQ.seq` read before the enumeration; 0 when the item is absent."""


class ExecutionGraphStore(Protocol):
    """The retained execution-graph store."""

    def read_mode(self) -> ModeState:
        """Read the mode item strongly; absent reads `LEGACY` at revision 0."""
        ...

    def read_rolling_freshness_fence(self, migration_id: str) -> RollingFreshnessFenceRead:
        """Strongly read the mode, migration fence, and its matching command receipt."""
        ...

    def commit_rolling_freshness_fence(self, change: RollingFreshnessFenceChange) -> None:
        """Atomically set the fence and receipt while the expected GRAPH revision is held."""
        ...

    def read_receipt(self, request_id: str) -> ReceiptRecord | None:
        """Read one decision receipt by request ID."""
        ...

    def read_conflict_receipt(self, request_id: str, request_digest: str) -> ReceiptRecord | None:
        """Read row 2's receipt for this request ID and conflicting digest, if one committed."""
        ...

    def read_unit(self, conflict_unit: str, epoch: int) -> UnitState:
        """Read `U`'s slot (and the run it names), waiter, head and block."""
        ...

    def read_match(self, fingerprint: str, epoch: int) -> FingerprintMatch | None:
        """Read the run or shadow graph a fingerprint already names in this epoch."""
        ...

    def commit_admission(self, commit: AdmissionCommit) -> None:
        """Commit one receipt with its effect; raise a conflict or not-recorded error."""
        ...

    def commit_mode(self, change: ModeChange) -> None:
        """CAS the mode and write its receipt, or raise `AdmissionConflictError`."""
        ...

    def record_command(self, receipt: CommandReceipt) -> None:
        """Write one refusal command receipt and nothing else."""
        ...


@dataclass(frozen=True, slots=True)
class ManifestRead:
    """The exact manifest version admission reads, and the key's exact-version listing."""

    retrieved_at: datetime
    source_system: str
    source_dataset: str
    period_end: str
    exact_versions: int
    window_retrievals: int
    """How many retrievals the window prefix lists, compared with the generated bound."""
    last_modified: datetime | None = None
    """S5: the exact version's `LastModified`, the root clock's only authority (FR-2)."""


class ManifestReader(Protocol):
    """Reads one raw manifest version; a failed listing raises a dependency error (no receipt)."""

    def read(self, key: str, version_id: str) -> ManifestRead:
        """Read the exact version body and the key's exact-version count."""
        ...
