# SPDX-License-Identifier: MIT-0
"""The baker's memory model: how many held rows one bake's deployed memory certifies (ADR-152).

`BAKE_MAX_ROWS` is not chosen. It is `bake_max_rows(BAKER_MEMORY_BYTES)`: the largest ceiling V
at which the binding roster row's resident bytes still fit `BAKE_RESIDENT_SHARE` of the baker.
Every row's resident bytes are affine in V (`bakeability.resident`), so the binding row's
intercept `BAKE_FIXED_BYTES` and slope `BAKE_BYTES_PER_ROW` give V for any memory size. The
bakeability gate recomputes both from the real batches and fails on any difference, so these
two figures can't drift from the code they model. Raising the Baker's memory raises V; that is
the operator's lever, never a lower limit on what a page shows.

Pure: no I/O, no clock, no AWS.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from domain.operations.generated_execution_graphs import BAKE_TASKS
from domain.presentation.preset_artifacts import MAX_PRESET_ARTIFACT_BYTES

__all__ = [
    "BAKER_MEMORY_BYTES",
    "BAKE_BYTES_PER_ROW",
    "BAKE_FIXED_BYTES",
    "BAKE_MEMORY_TERMS",
    "BAKE_RESIDENT_SHARE",
    "MIB",
    "WRITES_IN_FLIGHT_BYTES",
    "BakeMemoryTerms",
    "bake_max_rows",
]

MIB: Final = 1024 * 1024

BAKER_MEMORY_BYTES: Final = 1024 * MIB
"""The deployed Baker's memory. Must match its memorySize in infra/lib/stacks/publisher-stack.ts;
a test reads it."""

BAKE_RESIDENT_SHARE: Final = 0.5
"""The share of the Baker a bake's resident bytes may use; the rest is the interpreter, its
libraries and the allocator's own overhead."""


@dataclass(frozen=True, slots=True)
class BakeMemoryTerms:
    """Bytes per row the baker holds, measured by `validation/probe_bake_row_ceiling.py` (P1).

    Each is the largest `tracemalloc` peak over three runs at N in {10,000; 30,000; 50,000},
    through the real stream, shape and fold path, divided by N, on the widest held shapes.
    """

    raw: int
    """B_raw: one result row in flight, as the dict a slot's worker maps it to."""
    shaped: int
    """B_shaped: one row of a shaped answer, resident until the render ends."""
    kept: int
    """B_kept: one entry a fold keeps (the largest over the fold kinds)."""
    render: int
    """B_render: the render's own per-row structures."""
    measured: str
    """When and how the terms were measured: the evidence ADR-143 cites."""


BAKE_MEMORY_TERMS: Final = BakeMemoryTerms(
    raw=987,
    shaped=930,
    kept=241,
    render=10_467,
    measured=(
        "row-ceiling probe P1, 2026-10-18: tracemalloc, the maximum over 3 runs at 10k, 30k and "
        "50k rows; B_render is the Retail render's own structures per product"
    ),
)
"""The measured per-row terms; ADR-143's Decision cites them by date."""

WRITES_IN_FLIGHT_BYTES: Final = (
    2 * int(BAKE_TASKS["s3_chains_per_baker"]) * MAX_PRESET_ARTIFACT_BYTES
)
"""W: two bodies per S3 chain in flight, the most a bake's writes hold at once (bodies stream)."""

BAKE_FIXED_BYTES: Final = 4_099_720
"""The binding row's resident bytes at V = 0 (`audiences#last_complete_month`): W plus its
fixed-width slots. Recomputed and held equal by `make check-bakeability`."""

BAKE_BYTES_PER_ROW: Final = 33_941
"""The binding row's resident bytes per unit of V: its ceiling-wide slots times their terms.
Recomputed and held equal by `make check-bakeability`."""


def bake_max_rows(memory_bytes: int) -> int:
    """Return the held-row ceiling V a Baker of `memory_bytes` certifies.

    V = floor((BAKE_RESIDENT_SHARE x memory - BAKE_FIXED_BYTES) / BAKE_BYTES_PER_ROW)
    """
    limit = int(BAKE_RESIDENT_SHARE * memory_bytes)
    if limit <= BAKE_FIXED_BYTES:
        raise ValueError("a Baker this small certifies no held row.")
    return (limit - BAKE_FIXED_BYTES) // BAKE_BYTES_PER_ROW
