# SPDX-License-Identifier: MIT-0
"""The direct operator (`ingest_delivery`) as an `on_demand` requester (S6, V-19).

One strongly consistent mode read per invocation, before any ingestion, decides the path:

| Mode | Path |
|---|---|
| `LEGACY` | the legacy load, no admission call |
| `SHADOW` | the legacy load, then one non-blocking epoch-0 request per delivery |
| `LEGACY_DRAINING` | one `REFUSED(RUN_ADMISSION_NOT_ACTIVE)` receipt per delivery; the CLI exits
  nonzero with no ingestion |
| `GRAPH` | admission only; the receipt's graph does the work, the CLI ingests nothing |

The operator runs under the bound human profile, so admission holds it to the human-principal
rule. Each request ID derives from the operation ID and the exact version, so a re-run of the
same operation returns the same receipts.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from adapters.inbound.run_requests.service import RunRequest
from adapters.jobs.ingest_delivery.landing import ManifestInput
from domain.operations.execution_graph import AuthorityMode
from domain.operations.generated_execution_graphs import PROTOCOL_VERSION
from ports.outbound.execution_graph import ReceiptRecord

__all__ = [
    "GateDecision",
    "GatePath",
    "OperatorGate",
    "decide_gate",
    "operator_request",
    "record_shadow",
]

_ON_DEMAND: Final = "on_demand"
_OPERATOR_RERUN: Final = "operator_rerun"


class GatePath(StrEnum):
    """What the direct operator does after its one mode read."""

    LEGACY = "legacy"
    LEGACY_THEN_SHADOW = "legacy_then_shadow"
    REFUSED = "refused"
    ADMITTED_ONLY = "admitted_only"


@dataclass(frozen=True, slots=True)
class OperatorGate:
    """The mode read and the admission call; `admit` is None where no graph is provisioned."""

    mode: Callable[[], AuthorityMode]
    admit: Callable[[RunRequest], ReceiptRecord] | None
    principal: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class GateDecision:
    """The path and every receipt the gate wrote before any ingestion."""

    path: GatePath
    mode: AuthorityMode
    receipts: tuple[ReceiptRecord, ...] = ()

    @property
    def ingest(self) -> bool:
        """Whether the legacy load runs."""
        return self.path in {GatePath.LEGACY, GatePath.LEGACY_THEN_SHADOW}


def operator_request(gate: OperatorGate, manifest: ManifestInput) -> RunRequest:
    """Build the deterministic `on_demand` `operator_rerun` request for one exact version."""
    digest = hashlib.sha256(
        f"{gate.operation_id}\n{manifest.key}\n{manifest.version_id}".encode()
    ).hexdigest()
    return RunRequest(
        request_id=f"operator-{digest[:48]}",
        protocol_version=PROTOCOL_VERSION,
        caller_kind=_ON_DEMAND,
        reason=_OPERATOR_RERUN,
        manifest_key=manifest.key,
        version_id=manifest.version_id,
        requester=gate.principal,
        requester_is_iam=True,
    )


def decide_gate(gate: OperatorGate, manifests: Sequence[ManifestInput]) -> GateDecision:
    """Read the mode once; in `LEGACY_DRAINING` and `GRAPH` decide every delivery first."""
    mode = gate.mode()
    if mode is AuthorityMode.LEGACY:
        return GateDecision(GatePath.LEGACY, mode)
    if mode is AuthorityMode.SHADOW:
        return GateDecision(GatePath.LEGACY_THEN_SHADOW, mode)
    if gate.admit is None:
        raise PermissionError("the graph authority is required outside LEGACY and SHADOW.")
    receipts = tuple(gate.admit(operator_request(gate, manifest)) for manifest in manifests)
    if mode is AuthorityMode.LEGACY_DRAINING:
        return GateDecision(GatePath.REFUSED, mode, receipts)
    return GateDecision(GatePath.ADMITTED_ONLY, mode, receipts)


def record_shadow(
    gate: OperatorGate, manifests: Sequence[ManifestInput], on_failure: Callable[[Exception], None]
) -> tuple[ReceiptRecord, ...]:
    """Record one epoch-0 request per delivery after the legacy load; never raise."""
    if gate.admit is None:
        return ()
    receipts: list[ReceiptRecord] = []
    for manifest in manifests:
        try:
            receipts.append(gate.admit(operator_request(gate, manifest)))
        except Exception as error:
            on_failure(error)
    return tuple(receipts)
