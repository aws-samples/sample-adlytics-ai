# SPDX-License-Identifier: MIT-0
"""Execution-graph protocol root: identities, epochs, modes and the pre-domain gates.

One retained execution-graph store owns admission, graph identity and release permission
(control-plane Option B, S2 minimal v1 core). This module holds the parts every command shares:

- the one immutable `protocol_version` (v1). Any other version, and any live-migration
  command, is refused before any read-modify-write with `PROTOCOL_MIGRATION_UNSUPPORTED` (BF-3);
- `authority_epoch` 0 (SHADOW, and every receipt written outside GRAPH) and 1 (GRAPH). Graph,
  run and node identities include the epoch, so an epoch-1 request never matches an epoch-0
  record (V-5);
- the mode compare-and-swap table and the human-only principal rule: an agent or workload
  principal gets `AGENT_MUTATION_NOT_ENABLED` (Tier 0/1 only, V-3);
- the adoption refusal: no command may name a legacy `WRITE_PLAN#`, intent or unit identity as
  its source (`LEGACY_ADOPTION_UNSUPPORTED`, V-4).

Pure: no I/O, no clock, no AWS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from domain.operations.generated_execution_graphs import (
    AUTHORITY_EPOCHS,
    MODE_TRANSITIONS,
    PROTOCOL_VERSION,
)
from domain.operations.status import canonical_authority_digest
from domain.shared_kernel.errors import DomainError, ErrorCode, ValidationError

__all__ = [
    "AGENT_MUTATION_NOT_ENABLED",
    "CUTOVER_GATE_NOT_DEPLOYED",
    "LEGACY_ADOPTION_UNSUPPORTED",
    "MIGRATION_FENCE_SET",
    "MODE_REVISION_CHANGED",
    "MODE_TRANSITION_NOT_PERMITTED",
    "PROTOCOL_MIGRATION_UNSUPPORTED",
    "AuthorityMode",
    "GraphCommandRefusedError",
    "ModeState",
    "PrincipalKind",
    "classify_principal",
    "epoch_for_mode",
    "graph_id",
    "require_human_principal",
    "require_no_legacy_source",
    "require_protocol_v1",
    "run_id",
    "validate_mode_transition",
]

PROTOCOL_MIGRATION_UNSUPPORTED: Final = "PROTOCOL_MIGRATION_UNSUPPORTED"
AGENT_MUTATION_NOT_ENABLED: Final = "AGENT_MUTATION_NOT_ENABLED"
LEGACY_ADOPTION_UNSUPPORTED: Final = "LEGACY_ADOPTION_UNSUPPORTED"
MODE_TRANSITION_NOT_PERMITTED: Final = "MODE_TRANSITION_NOT_PERMITTED"
CUTOVER_GATE_NOT_DEPLOYED: Final = "CUTOVER_GATE_NOT_DEPLOYED"
MODE_REVISION_CHANGED: Final = "MODE_REVISION_CHANGED"
MIGRATION_FENCE_SET: Final = "MIGRATION_FENCE_SET"

_SIGNAL_CODES: Final = {
    PROTOCOL_MIGRATION_UNSUPPORTED: 1,
    AGENT_MUTATION_NOT_ENABLED: 2,
    LEGACY_ADOPTION_UNSUPPORTED: 3,
    MODE_TRANSITION_NOT_PERMITTED: 4,
    CUTOVER_GATE_NOT_DEPLOYED: 5,
    MODE_REVISION_CHANGED: 6,
    "FULL_HISTORY_NOT_ENABLED": 7,
    "RUN_REQUEST_REASON_NOT_PERMITTED": 8,
    "RUN_REQUEST_RECEIPT_REQUIRED": 9,
    "AUTOMATIC_ADMISSION_HOLD_REQUIRES_GRAPH": 10,
    "RUN_REQUESTER_GROUP_REQUIRED": 11,
    # S3 (S3V-12): an operator command id reused for a different command or run.
    "COMMAND_ID_CONFLICT": 12,
    # S6 cutover (domain/operations/cutover.py).
    "DRAIN_DEADLINE_OUT_OF_BOUND": 13,
    "DRAIN_FENCE_NOT_ELAPSED": 14,
    "DRAIN_SEQ_CHANGED": 15,
    "CUTOVER_ABORT_NOT_PERMITTED": 16,
    "DRAIN_NOT_ZERO": 17,
    "CUTOVER_REQUIRES_ZERO_PROOF": 18,
    # S6 part 2 (§2.12): the operator's dead-letter redrive (`dead_letters.py`). 20-28 and
    # 30 and 40-44 are other modules' EXEC_GRAPH codes, so these take 31 to 34.
    "REDRIVE_QUEUE_UNKNOWN": 31,
    "REDRIVE_COUNT_MISMATCH": 32,
    "REDRIVE_IN_FLIGHT": 33,
    "REDRIVE_MOVE_ACTIVE": 34,
    # D7 one-time rolling-freshness migration fence. Reserved permanently after removal.
    MIGRATION_FENCE_SET: 36,
}
_LEGACY_SOURCE: Final = re.compile(r"(?:^|[#/])(?:WRITE_PLAN|INTENT|UNIT)#")


class GraphCommandRefusedError(ValidationError):
    """A graph command refused before any read-modify-write: no receipt, zero graph writes."""

    def __init__(self, signal: str, description: str) -> None:
        super().__init__(ErrorCode("EXEC_GRAPH", _SIGNAL_CODES[signal]), description)
        self.signal = signal


class ModeGuardError(DomainError):
    """A mode CAS whose expected revision moved: no receipt; the operator re-reads."""

    def __init__(self, description: str) -> None:
        super().__init__(ErrorCode("EXEC_GRAPH", _SIGNAL_CODES[MODE_REVISION_CHANGED]), description)
        self.signal = MODE_REVISION_CHANGED


class AuthorityMode(StrEnum):
    """The mode item's closed values."""

    LEGACY = "LEGACY"
    SHADOW = "SHADOW"
    LEGACY_DRAINING = "LEGACY_DRAINING"
    GRAPH = "GRAPH"


@dataclass(frozen=True, slots=True)
class ModeState:
    """One strongly consistent mode read. An absent mode item reads `LEGACY` at revision 0."""

    mode: AuthorityMode
    revision: int
    graph_entered_at: str | None = None
    hold: bool = False
    """S3: the automatic-admission hold lives on the mode item, so only the operator writes it."""
    hold_revision: int = 0
    hold_cleared_at: str | None = None
    drain_entered_at: str | None = None
    """S6: when the drain CAS committed; the writer-lifetime fence counts from it (V-17b)."""
    drain_deadline: str | None = None
    """S6: the operator's drain deadline `D`; refused deliveries wait until `D` plus the margin."""


def epoch_for_mode(mode: AuthorityMode) -> int:
    """Every record written outside GRAPH carries epoch 0; GRAPH writes epoch 1 (r4, R3-2)."""
    return AUTHORITY_EPOCHS["graph"] if mode is AuthorityMode.GRAPH else AUTHORITY_EPOCHS["shadow"]


def require_protocol_v1(protocol_version: str, *, migration: bool = False) -> None:
    """Refuse any version other than v1 and any live-migration command (BF-3)."""
    if migration or protocol_version != PROTOCOL_VERSION:
        raise GraphCommandRefusedError(
            PROTOCOL_MIGRATION_UNSUPPORTED,
            f"protocol {protocol_version!r} or a migration command is not supported in v1.",
        )


class PrincipalKind(StrEnum):
    """Who issued a mutating command."""

    HUMAN = "human"
    AGENT = "agent"


_HUMAN_ARN: Final = re.compile(
    r"^arn:aws:(?:iam::\d{12}:user/[\w+=,.@/-]+"
    r"|sts::\d{12}:assumed-role/AWSReservedSSO_[\w+=,.@-]+/[\w+=,.@-]+)$"
)


def classify_principal(arn: str) -> PrincipalKind:
    """Classify a principal: a named IAM user or an SSO session is the human operator."""
    return PrincipalKind.HUMAN if _HUMAN_ARN.fullmatch(arn) else PrincipalKind.AGENT


def require_human_principal(arn: str) -> None:
    """Refuse an agent or workload principal before any mutation (Tier 0/1 only, V-3)."""
    if classify_principal(arn) is not PrincipalKind.HUMAN:
        raise GraphCommandRefusedError(
            AGENT_MUTATION_NOT_ENABLED, "only the bound human operator may mutate the graph."
        )


def require_no_legacy_source(*sources: str) -> None:
    """Refuse a command that names a legacy plan, intent or unit identity (V-4)."""
    if any(_LEGACY_SOURCE.search(source) for source in sources):
        raise GraphCommandRefusedError(
            LEGACY_ADOPTION_UNSUPPORTED, "legacy plans, intents and units are never adopted."
        )


def validate_mode_transition(current: ModeState, to_mode: AuthorityMode, expected: int) -> None:
    """Refuse a transition the v1 table does not enable, or one against a moved revision."""
    if current.revision != expected:
        raise ModeGuardError("the mode item moved since it was read.")
    for item in MODE_TRANSITIONS:
        if item["from"] == current.mode.value and item["to"] == to_mode.value:
            if not item["enabled"]:
                raise GraphCommandRefusedError(
                    CUTOVER_GATE_NOT_DEPLOYED,
                    f"{current.mode} -> {to_mode} needs the S6 ingress gate.",
                )
            return
    raise GraphCommandRefusedError(
        MODE_TRANSITION_NOT_PERMITTED, f"{current.mode} -> {to_mode} is not a legal transition."
    )


def run_id(fingerprint: str, epoch: int, generation: int) -> str:
    """Derive a run's identity from fingerprint, epoch and retry generation."""
    return canonical_authority_digest(["execution-graph-run/v1", fingerprint, epoch, generation])


def graph_id(fingerprint: str, epoch: int, generation: int) -> str:
    """Derive a graph's identity: the run's, under its own namespace."""
    return canonical_authority_digest(["execution-graph/v1", fingerprint, epoch, generation])
