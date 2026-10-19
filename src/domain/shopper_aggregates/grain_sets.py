# SPDX-License-Identifier: MIT-0
"""A shopper-aggregate fact's closed set of served grain sets (ADR-099 point 2).

Each grain set is its own contract and extraction, stamped on every row as the `grain_set`
literal; there's no `__all__` sentinel. Withdrawal needs somewhere coarser to send the reader, so
the fact declares its grain sets from coarsest to finest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_GRAIN_SET_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class GrainSet:
    """One served grain set: its row literal and the label a pointer to it reads."""

    name: str
    label: str

    def __post_init__(self) -> None:
        if not _GRAIN_SET_NAME.fullmatch(self.name):
            raise ValueError(f"grain set {self.name!r} must be a lowercase identifier.")
        if not self.label.strip():
            raise ValueError(f"grain set {self.name} needs a label.")


@dataclass(frozen=True, slots=True)
class ServedGrainSets:
    """The grain sets one fact serves, ordered coarsest first.

    For example platform, ad product, then campaign for `fct_new_to_brand_buyers`.
    """

    fact: str
    grain_sets: tuple[GrainSet, ...]

    def __post_init__(self) -> None:
        if not self.fact.strip() or not self.grain_sets:
            raise ValueError("a fact must serve at least one grain set.")
        names = tuple(item.name for item in self.grain_sets)
        if len(set(names)) != len(names):
            raise ValueError(f"{self.fact} repeats a grain set.")

    def coarser_served(self, name: str) -> GrainSet | None:
        """Return the nearest coarser grain set this fact serves, or `None` for the coarsest."""
        names = tuple(item.name for item in self.grain_sets)
        if name not in names:
            raise ValueError(f"{self.fact} serves no grain set {name!r}.")
        position = names.index(name)
        return self.grain_sets[position - 1] if position else None
