# SPDX-License-Identifier: MIT-0
"""Shared doubles for ADR-149 staging: a recording stager and exact verification count rows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from adapters.outbound.athena.semantic_storage import UnitWorkload
from adapters.outbound.athena.staging import stage_unit
from domain.operations.statement_protocol import MAX_STATEMENT_BYTES
from ports.outbound.unit_staging import StagedUnit

SIDES = ("target", "staged", "matched_target", "matched_staged")


@dataclass
class RecordingStager:
    """A `UnitStager` that renders each body once and records the call.

    ``calls`` is an optional shared call log the stager appends ``"stage"`` to, so a test can
    pin where staging falls among the other port calls. ``failure`` is raised after recording.
    """

    calls: list[str] | None = None
    failure: BaseException | None = None
    ensured: list[StagedUnit] = field(default_factory=list[StagedUnit])
    bodies: list[bytes] = field(default_factory=list[bytes])

    def ensure(self, staged: StagedUnit, render: Callable[[], bytes]) -> None:
        self.ensured.append(staged)
        if self.calls is not None:
            self.calls.append("stage")
        if self.failure is not None:
            raise self.failure
        self.bodies.append(render())


def verification_rows(staged: StagedUnit) -> tuple[tuple[str | None, ...], ...]:
    """Return the exact count rows a correct unit's verification returns, four per identity."""
    return tuple(
        (side, identity, str(count)) for side in SIDES for identity, count in staged.row_counts
    )


def workload_verification_rows(workload: UnitWorkload) -> tuple[tuple[str | None, ...], ...]:
    """Return the exact count rows for the unit ``workload`` stages as."""
    return verification_rows(stage_unit(workload, max_record_bytes=MAX_STATEMENT_BYTES))
