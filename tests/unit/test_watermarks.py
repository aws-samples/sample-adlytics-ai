# SPDX-License-Identifier: MIT-0
"""Stable source partition identity retained by Semantic V2 ingestion."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from domain.curation.watermarks import PartitionKey, WatermarkError

pytestmark = [pytest.mark.unit, pytest.mark.covers("G-01", "G-02")]

WEEK_START = date(2026, 8, 10)
WEEK_END = date(2026, 8, 16)


def _partition(*, options: str = "opt-a", marketplace: str = "ATVPDKIKX0DER") -> PartitionKey:
    return PartitionKey(
        source_system="vendor_central",
        contract_id="vendor-sales-by-asin",
        report_options_hash=options,
        marketplace_key=marketplace,
        period_start=WEEK_START,
        period_end=WEEK_END,
    )


class TestPartitionIdentity:
    def test_two_option_sets_are_different_partitions(self) -> None:
        assert (
            _partition(options="sourcing").identity != _partition(options="manufacturing").identity
        )

    def test_two_marketplaces_are_different_partitions(self) -> None:
        assert _partition(marketplace="A1F83G8C2ARO7P").identity != _partition().identity

    def test_identity_orders_general_before_specific(self) -> None:
        assert _partition().identity.startswith("vendor_central|vendor-sales-by-asin|")

    def test_a_single_day_window_is_valid(self) -> None:
        key = PartitionKey(
            source_system="vendor_central",
            contract_id="vendor-sales-by-asin",
            report_options_hash="opt-a",
            marketplace_key="ATVPDKIKX0DER",
            period_start=WEEK_START,
            period_end=WEEK_START,
        )
        assert key.period_start == key.period_end

    def test_a_reversed_period_is_refused(self) -> None:
        with pytest.raises(WatermarkError, match="before it starts"):
            PartitionKey("vendor", "contract", "hash", "market", WEEK_END, WEEK_START)

    def test_a_missing_part_is_refused_by_name(self) -> None:
        with pytest.raises(WatermarkError, match="marketplace_key"):
            PartitionKey("vendor", "contract", "hash", " ", WEEK_START, WEEK_END)

    def test_partition_identity_is_immutable(self) -> None:
        with pytest.raises(FrozenInstanceError):
            _partition().report_options_hash = "mutated"  # type: ignore[misc]
