# SPDX-License-Identifier: MIT-0
"""Per-item failure isolation for the graph-authority worker passes (S3V-7).

A worker pass visits every due shard and every item in it. One item's failure must not end the
pass for every later item and shard: parallel claims make a `TransactionConflict` on `RUN#` an
expected event, and one malformed or unreachable item would otherwise block every shard each
minute. So each item runs through `isolated`:

- a `GraphTransactionConflictError` is retried within the generated re-decide bound and counted;
- any other failure (a store or provider dependency error, a malformed item, a guard nothing
  expected) is handed to the pass's failure sink once, counted, and the pass moves on.

Every item is durable, so a skipped item is taken again by the next pass. The sink is how the
Lambda entrypoint reports each failure (`report_error`); tests count them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from domain.operations.generated_execution_graphs import BOUNDS
from ports.outbound.execution_graph_dispatch import GraphTransactionConflictError

__all__ = ["CONFLICT_RETRY", "FailureSink", "Isolation", "ignore_failure", "isolated"]

CONFLICT_RETRY: Final = int(BOUNDS["max_redecide"])
"""How many times one item is retried on `TransactionConflict` before it is left for later."""

type FailureSink = Callable[[BaseException], None]


def ignore_failure(error: BaseException) -> None:
    """Ignore the failure; the caller only counts it (the default sink)."""
    del error


@dataclass(frozen=True, slots=True)
class Isolation:
    """How one isolated item ended."""

    ok: bool
    conflicts: int = 0


def isolated(action: Callable[[], None], sink: FailureSink) -> Isolation:
    """Run one item; retry its conflicts within the bound; report and absorb any failure."""
    conflicts = 0
    for _ in range(CONFLICT_RETRY):
        try:
            action()
        except GraphTransactionConflictError:
            conflicts += 1
            continue
        except Exception as error:
            sink(error)
            return Isolation(ok=False, conflicts=conflicts)
        return Isolation(ok=True, conflicts=conflicts)
    sink(GraphTransactionConflictError("TransactionConflict retry exhausted"))
    return Isolation(ok=False, conflicts=conflicts)
