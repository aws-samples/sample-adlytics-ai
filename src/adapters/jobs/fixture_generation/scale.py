# SPDX-License-Identifier: MIT-0
"""Streamed fictional products for demand-forecast scale fixtures (design §10, Task 2(f)).

The source engine's approach, not its code: each product's shape and every weekly value are
drawn from a SHA-256 hash of (seed, product index, name), so any product can be rebuilt alone,
nothing reads a clock, and `scale_products()` yields one product at a time. Memory is constant in
the count, for any count up to `MAX_SCALE_ASINS`. The values are fictional and prove only the
declared shapes, the types and the job's behaviour at size, never a source volume.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

MAX_SCALE_ASINS = 200_000
"""100 shards of 2,000 ASINs, the `UNLOAD` partition cap (design §3c)."""

SHAPES: tuple[str, ...] = (
    "regular",
    "trend_seasonal",
    "low_volume",
    "intermittent",
    "stockout",
    "launch",
    "delisted",
)
DEFAULT_SHAPE_MIX: Mapping[str, int] = MappingProxyType(
    {
        "regular": 20,
        "trend_seasonal": 12,
        "low_volume": 6,
        "intermittent": 6,
        "stockout": 4,
        "launch": 3,
        "delisted": 2,
    }
)
"""The weights of the dev catalogue's shape mix (approval K-4)."""
_LAUNCH_WEEKS = 40
_DELISTED_WEEKS = 20
_PLACES = Decimal("0.0001")


@dataclass(frozen=True)
class ScaleFixtureRequest:
    """How many products, from which seed, in which shape mix, over how many weeks."""

    asin_count: int
    seed: str
    shape_mix: Mapping[str, int] = DEFAULT_SHAPE_MIX
    weeks: int = 104

    def __post_init__(self) -> None:
        if not 1 <= self.asin_count <= MAX_SCALE_ASINS:
            raise ValueError(f"asin_count must be 1 to {MAX_SCALE_ASINS}")
        if not self.seed:
            raise ValueError("seed must be non-empty")
        if set(self.shape_mix) - set(SHAPES) or sum(self.shape_mix.values()) <= 0:
            raise ValueError(f"shape_mix weights name only {list(SHAPES)} and sum above 0")
        if any(weight < 0 for weight in self.shape_mix.values()):
            raise ValueError("shape_mix weights are never negative")
        if self.weeks < _LAUNCH_WEEKS:
            raise ValueError(f"weeks must be at least {_LAUNCH_WEEKS}")


@dataclass(frozen=True)
class ScaleWeek:
    """One product-week; `None` fields mean the product has no row that week."""

    units_ordered: int
    units_shipped: int
    revenue_shipped: Decimal
    cogs_shipped: Decimal
    glance_views: int


@dataclass(frozen=True)
class ScaleProduct:
    """One fictional product and its weekly values, oldest first; `None` is a week with no row."""

    index: int
    canonical_asin: str
    shape: str
    weeks: tuple[ScaleWeek | None, ...]


def _unit(seed: str, *parts: object) -> float:
    """Return a deterministic draw in [0, 1) from the seed and the parts."""
    text = "|".join((seed, *(str(part) for part in parts)))
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:13], 16) / float(1 << 52)


def _shape(request: ScaleFixtureRequest, index: int) -> str:
    total = sum(request.shape_mix.values())
    point = _unit(request.seed, index, "shape") * total
    for name in SHAPES:
        point -= request.shape_mix.get(name, 0)
        if point < 0:
            return name
    return next(name for name in reversed(SHAPES) if request.shape_mix.get(name, 0) > 0)


def _level(shape: str, draw: float) -> float:
    if shape == "low_volume":
        return 1.0 + 3.0 * draw
    if shape == "intermittent":
        return 2.0 + 4.0 * draw
    return 40.0 + 160.0 * draw


def _week(  # noqa: PLR0913 -- one product-week of one request
    request: ScaleFixtureRequest, index: int, *, shape: str, week: int
) -> ScaleWeek | None:
    seed = request.seed
    last = request.weeks - 1
    if shape == "launch" and week < request.weeks - _LAUNCH_WEEKS:
        return None
    if shape == "delisted" and week > last - _DELISTED_WEEKS:
        return None
    if shape == "intermittent" and _unit(seed, index, week, "sold") < 0.45:  # noqa: PLR2004
        return None
    level = _level(shape, _unit(seed, index, "level"))
    season = 1.0
    if shape == "trend_seasonal":
        season = (1 + 0.004 * week) * (1 + 0.25 * math.sin(2 * math.pi * week / 52))
    noise = 0.9 + 0.2 * _unit(seed, index, week, "noise")
    ordered = max(0, round(level * season * noise))
    shipped = ordered
    if shape == "stockout" and _unit(seed, index, week, "short") < 0.3:  # noqa: PLR2004
        shipped = round(ordered * 0.6)
    price = Decimal(str(round(18 + 24 * _unit(seed, index, "price"), 2)))
    revenue = (price * shipped).quantize(_PLACES)
    cogs = (revenue * Decimal("0.62")).quantize(_PLACES)
    views = max(1, round(ordered * (14 + 12 * _unit(seed, index, week, "views"))))
    return ScaleWeek(ordered, shipped, revenue, cogs, views)


def scale_asin(index: int) -> str:
    """Return scale product `index`'s canonical ASIN: `amazon_asin:b0sc` and six digits."""
    return f"amazon_asin:b0sc{index:06d}"


def scale_products(request: ScaleFixtureRequest) -> Iterator[ScaleProduct]:
    """Yield every product in canonical ASIN order, one at a time."""
    for index in range(request.asin_count):
        shape = _shape(request, index)
        weeks = tuple(
            _week(request, index, shape=shape, week=week) for week in range(request.weeks)
        )
        yield ScaleProduct(index, scale_asin(index), shape, weeks)
