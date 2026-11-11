# SPDX-License-Identifier: MIT-0
"""Batch membership for cross-learning Chronos-2 calls (design §4c, `engine` v1).

With cross-learning, a series' forecast depends on the other series of its call, so membership
is policy: series are sorted by `series_sort_key()` and consecutive runs of `batch_size` form
the batches, never mixing a target or an origin. Callers may rely on: the same series and policy
give the same batches whatever order the rows arrived in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class Origin(StrEnum):
    """Where a forecast starts: the backtest origin or the forecast origin (`input_end`)."""

    BACKTEST = "backtest"
    FORECAST = "forecast"


@dataclass(frozen=True, order=True)
class SeriesSortKey:
    """`engine.sort_key`: target, origin, the forecast set, then the canonical ASIN."""

    target: str
    origin: str
    marketplace_key: str
    distributor_view: str
    selling_program: str
    canonical_asin: str


@dataclass(frozen=True)
class SeriesKeyRequest:
    """Everything one series' sort key is made from."""

    target: str
    origin: Origin
    marketplace_key: str
    distributor_view: str
    selling_program: str
    canonical_asin: str


def series_sort_key(request: SeriesKeyRequest) -> SeriesSortKey:
    """Return the declared sort key of one series at one origin."""
    return SeriesSortKey(
        request.target,
        request.origin.value,
        request.marketplace_key,
        request.distributor_view,
        request.selling_program,
        request.canonical_asin,
    )


@dataclass(frozen=True)
class BatchPlan:
    """Batches as positions into the caller's key list, each batch in sort order."""

    batches: tuple[tuple[int, ...], ...]


def plan_batches(keys: Sequence[SeriesSortKey], batch_size: int) -> BatchPlan:
    """Sort by key; cut runs of at most `batch_size` that never span a target or an origin."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if len(set(keys)) != len(keys):
        raise ValueError("every series key must be distinct")
    order = sorted(range(len(keys)), key=lambda index: keys[index])
    batches: list[tuple[int, ...]] = []
    current: list[int] = []
    for index in order:
        scope = (keys[index].target, keys[index].origin)
        if current and (
            len(current) == batch_size
            or (keys[current[0]].target, keys[current[0]].origin) != scope
        ):
            batches.append(tuple(current))
            current = []
        current.append(index)
    if current:
        batches.append(tuple(current))
    return BatchPlan(tuple(batches))
