# SPDX-License-Identifier: MIT-0
"""The baker's memory model: how many held rows one bake's deployed memory certifies (ADR-152).

`BAKE_MAX_ROWS` is not chosen. It is `bake_max_rows(BAKER_MEMORY_BYTES)`: the largest ceiling V
at which the binding roster row's resident bytes still fit `BAKE_RESIDENT_SHARE` of the baker.
Every row's resident bytes are affine in V (`bakeability.resident`), so the binding row's
intercept `BAKE_FIXED_BYTES` and slope `BAKE_BYTES_PER_ROW` give V for any memory size at the
contract's write width; the intercept holds W, which grows with the width (ADR-145). The
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
    "BAKE_WRITE_WIDTH",
    "MIB",
    "WRITES_IN_FLIGHT_BYTES",
    "BakeMemoryTerms",
    "bake_max_rows",
    "write_buffer_bytes",
]

MIB: Final = 1024 * 1024

BAKER_MEMORY_BYTES: Final = int(BAKE_TASKS["baker_memory_mib"]) * MIB
"""The deployed Baker's memory: the contract's `bake_tasks.baker_memory_mib`, its one home, which
infra/lib/stacks/publisher-stack.ts also reads for the function's memorySize."""

BAKE_RESIDENT_SHARE: Final = 0.5
"""The share of the Baker a bake's resident bytes may use; the rest is the interpreter, its
libraries and the allocator's own overhead."""


@dataclass(frozen=True, slots=True)
class BakeMemoryTerms:
    """Bytes per row the baker holds, measured by `validation/probe_bake_row_ceiling.py` (P1).

    Each is the largest `tracemalloc` peak over three runs at each probed N, through the real
    stream, shape and fold path, divided by N, on the widest held shapes.
    """

    raw: int
    """B_raw: one result row in flight, as the dict a slot's worker maps it to."""
    shaped: int
    """B_shaped: one row of a shaped answer, resident until the render ends."""
    kept: int
    """B_kept: one entry a fold keeps (the largest over the fold kinds)."""
    render: int
    """B_render: the render's own per-row structures."""
    record: int
    """B_record: one entity a page renders whole holds: its compact record (`KeyedColumns`) and
    the render's rows for it in every filter (Campaigns, ADR-152)."""
    measured: str
    """When and how the terms were measured: the evidence ADR-143 cites."""


BAKE_MEMORY_TERMS: Final = BakeMemoryTerms(
    raw=987,
    shaped=930,
    kept=241,
    render=158,
    record=7_865,
    measured=(
        "row-ceiling probe P1, 2026-10-25: tracemalloc, the maximum over 3 runs at 10k and 100k "
        "rows; B_render is the Retail render's own structures per product, B_record the real "
        "Campaigns bake render per campaign at 10k and at the 31,514-campaign ceiling"
    ),
)
"""The measured per-row terms; ADR-143's Decision cites them by date."""

BAKE_WRITE_WIDTH: Final = int(BAKE_TASKS["baker_write_width"])
"""The sibling body->custody pairs one Baker writes at once, and its shared S3 client's pool:
the contract's `bake_tasks.baker_write_width`, its one home. `make check-bakeability` derives it
from this memory model and refuses any other value (ADR-145)."""


def write_buffer_bytes(width: int) -> int:
    """W at `width` pairs in flight: each pair holds its body and custody, each at the cap."""
    if width < 1:
        raise ValueError("a write width is at least one pair.")
    return 2 * width * MAX_PRESET_ARTIFACT_BYTES


WRITES_IN_FLIGHT_BYTES: Final = write_buffer_bytes(BAKE_WRITE_WIDTH)
"""W: the write buffer at the contract's width, the most a bake's writes hold at once (bodies
stream)."""

BAKE_FIXED_BYTES: Final = 11_779_720
"""The binding row's resident bytes at V = 0 (`audiences#last_complete_month`): W plus its
fixed-width slots. Recomputed and held equal by `make check-bakeability`."""

BAKE_BYTES_PER_ROW: Final = 23_632
"""The binding row's resident bytes per unit of V: its ceiling-wide slots times their terms.
Recomputed and held equal by `make check-bakeability`."""


def bake_max_rows(memory_bytes: int) -> int:
    """Return the held-row ceiling V a Baker of `memory_bytes` certifies at the contract's width.

    V = floor((BAKE_RESIDENT_SHARE x memory - BAKE_FIXED_BYTES) / BAKE_BYTES_PER_ROW)

    Another memory size derives its own width, and so its own W: `make check-bakeability` prints
    the ceiling each size certifies at the width it derives.
    """
    limit = int(BAKE_RESIDENT_SHARE * memory_bytes)
    if limit <= BAKE_FIXED_BYTES:
        raise ValueError("a Baker this small certifies no held row.")
    return (limit - BAKE_FIXED_BYTES) // BAKE_BYTES_PER_ROW
