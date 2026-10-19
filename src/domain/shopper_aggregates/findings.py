# SPDX-License-Identifier: MIT-0
"""Stable, machine-readable reasons a shopper-aggregate delivery is refused."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ShopperAggregateFinding:
    """One contract violation in a delivery: a code to count on and a detail to read."""

    code: str
    detail: str

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.detail.strip():
            raise ValueError("a shopper-aggregate finding requires a code and a detail.")


class ShopperAggregateContractError(ValueError):
    """A delivery breaks its declared shape, so it is rejected whole (design §7a)."""

    def __init__(self, findings: tuple[ShopperAggregateFinding, ...]) -> None:
        if not findings:
            raise ValueError("a violation carries at least one finding.")
        super().__init__("; ".join(f"{item.code}: {item.detail}" for item in findings))
        self.findings = findings
