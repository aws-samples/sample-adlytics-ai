# SPDX-License-Identifier: MIT-0
"""The remaining-time budget of one graph-worker pass (ADR-162).

A graph worker runs every minute under a fixed Lambda timeout. A pass that walks more due items
than fit in one invocation is killed, and a killed pass loses everything it held only in memory.
So the promoter and the release read the invocation's remaining time before each item and start
no new item below a reserve. A pass that stops on its budget leaves every unstarted item in its
`DUE#` shard, where the next pass reads it strongly; each item's commit is one conditioned
transaction, so stopping early loses nothing and repeats nothing. There is no cursor.

The reserves are Python constants, not contract bounds: a changed bound is a new protocol
version, and the generated projection under `infra/` would move (ADR-162, "Where the reserves
live"). Each reserve is measured as remaining time, so it holds for any timeout above the
largest one; `test_execution_graph_pass_budget.py` refuses a reserve at or over the deployed
pass time (`bake_tasks.worker_pass_seconds`).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from domain.shared_kernel.errors import ErrorCode, InvariantError

__all__ = [
    "CLOCK_INVALID",
    "COMPLETE_RESERVE_MS",
    "ITEM_RESERVE_MS",
    "NO_DEADLINE",
    "PROMOTE_RESERVE_MS",
    "SERVING_STAGE_RESERVE_MS",
    "UNITS_COLLECT_RESERVE_MS",
    "PassBudget",
    "remaining_reader",
]

CLOCK_INVALID: Final = ErrorCode("EXEC_GRAPH", 35)
"""A remaining-time reading that is not a positive integer, or that rose: the pass ends."""

ITEM_RESERVE_MS: Final = 10_000
"""No graph-worker loop starts a new item, or a new table grant, with less time remaining.

It covers the one item already started plus the pass's tail. Measured on dev on 2026-11-13
(the graph-stall RCA, §3): a store call costs about 11 ms client side at 512 MB (a release
`NOT_READY` item is 4 calls in about 44 ms; a promoter units item 2 calls in about 32 ms). The
largest item is a release that reserves, grants and applies inline, about 40 calls (0.5 s);
`max_redecide` (8) re-reads bound it at about 4 s [inference from those rates]. The tail is the
terminal line plus the 2 s bounded trace flush. 10 s covers both with margin; the deploy step
confirms it from the REPORT durations.
"""

UNITS_COLLECT_RESERVE_MS: Final = 25_000
"""The promoter stops collecting units `ARM#` items here, so the grant phase that commits them
gets at least 15 s before `ITEM_RESERVE_MS`. One `TABLE#<k>` grant is one transaction; a guard
adds one re-read of at most 24 members (48 calls, about 0.5 s) per retry. Tables the grant phase
does not reach keep their units PENDING for the next pass, oldest head first."""

UNITS_SHARE_MS: Final = 10_000
"""The units collection's share of a pass in which a serving backlog also runs: about 310 units
`ARM#` items at the measured 32 ms each (graph-stall RCA §3)."""

SERVING_STAGE_RESERVE_MS: Final = UNITS_COLLECT_RESERVE_MS + UNITS_SHARE_MS
"""35 s: the serving election reads no new candidate, and the drain starts no further alias,
below this, so the units collection that follows in the same pass still gets its 10 s share
before `UNITS_COLLECT_RESERVE_MS` (serve-rca.md §3.4). Serving starts about 4 s into a 60 s pass
once the waiter phase is quiet, so it gets about 20 s: about 200 candidates at the measured
101.5 ms each (dev, 2026-11-13, 135 drains, R² 0.996), or 130 aliases at about 150 ms.
The first alias of a pass keeps today's `next_election_min_remaining_ms` guard alone.

Worst case: a waiter backlog runs its phase to `PROMOTE_RESERVE_MS`, so serving starts with at
most 40 s left and gets 5 s above this reserve. Less its fresh iterations (about 0.8 s each: 32
due-shard queries, the head and 32 frontier reads), that is about 40 candidates a staging pass,
or about 33 on the final path, which reads its own iteration. That still outruns the at most 22
publications that settled a minute on dev, so staging converges, but a 3,502 roster then takes
hours instead of about 18 passes [inference from the dev rates]. The terminal line's
`serving_unstaged` shows it."""

COMPLETE_RESERVE_MS: Final = 35_000
"""The release stops visiting `COMPLETE#` items that are not a domain's ACTIVE winner here, so
its `RELEASE#` phase keeps at least 25 s before `ITEM_RESERVE_MS` (about 570 triggers at 44 ms).
A non-winner waits for the promoter's alias drain, so its visit is a poll; the winner, whose
completion is the only move from ACTIVE to draining, is visited first under `ITEM_RESERVE_MS`.
Without this reserve a `COMPLETE#` backlog of about 1,100 waiting runs would take every pass and
no new grant would be issued (graph-fix review round 1, AGG-1) [inference from rca.md §3]."""

PROMOTE_RESERVE_MS: Final = 40_000
"""The promoter's waiter phase (`promote_shard`) starts no unit below this, so the arm pass that
follows it in the same invocation always keeps at least 40 s of a 60 s pass."""


def remaining_reader(remaining_ms: Callable[[], object]) -> Callable[[], int]:
    """Wrap the Lambda clock so every reading is a positive integer that never rises."""
    prior: int | None = None

    def read() -> int:
        nonlocal prior
        # The Lambda callable is untrusted at runtime; validate the reading as an object.
        value = remaining_ms()
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            or (prior is not None and value > prior)
        ):
            raise InvariantError(CLOCK_INVALID, "the graph worker's remaining clock is invalid.")
        prior = value
        return value

    return read


@dataclass(frozen=True, slots=True)
class PassBudget:
    """One invocation's remaining time, read through a validated reader."""

    remaining_ms: Callable[[], int]

    @classmethod
    def of(cls, remaining_ms: Callable[[], object]) -> PassBudget:
        """Build a budget over the Lambda context's `get_remaining_time_in_millis`."""
        return cls(remaining_reader(remaining_ms))

    def allows(self, reserve_ms: int) -> bool:
        """Return whether a new item may start: at least `reserve_ms` remains."""
        return self.remaining_ms() >= reserve_ms


NO_DEADLINE: Final = PassBudget(lambda: sys.maxsize)
"""A budget that never stops a pass: a single-shard sweep with no invocation (tests, tooling)."""
