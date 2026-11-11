# SPDX-License-Identifier: MIT-0
"""The hierarchy reader's marketplace filter.

Unset, Retail health reads every marketplace scope as before; set, the demand forecast reads only
its one marketplace, so a title never comes from another population.
"""

from __future__ import annotations

from datetime import date
from typing import cast

import pytest
from forecast_publication_support import MemoryCurrent, declarations

from adapters.jobs.preset_artifact_publisher.retail_source import (
    CanonicalRetailProductHierarchy,
)
from domain.shared_kernel.values import DateRange
from ports.outbound.canonical import CanonicalDatasetReadPort
from ports.outbound.semantic_state import CurrentPublicationPort

pytestmark = pytest.mark.unit

WINDOW = DateRange(date(2026, 1, 1), date(2026, 1, 31))


def _reader(marketplace: str | None) -> tuple[CanonicalRetailProductHierarchy, MemoryCurrent]:
    known = declarations()
    current = MemoryCurrent()
    reader = CanonicalRetailProductHierarchy(
        cast("CurrentPublicationPort", current),
        cast("CanonicalDatasetReadPort", object()),
        known.packs,
        known.contracts,
        known.bridge,
        marketplace_key=marketplace,
    )
    return reader, current


def test_unset_reads_every_marketplace_scope() -> None:
    reader, current = _reader(None)
    assert reader.hierarchy(WINDOW) is None
    assert len(current.reads) >= 1


def test_set_reads_only_that_marketplace() -> None:
    reader, current = _reader("amazon_us")
    assert reader.hierarchy(WINDOW) is None
    assert len(current.reads) == 1


def test_a_marketplace_outside_the_bridge_reads_nothing() -> None:
    reader, current = _reader("not_a_marketplace")
    assert reader.hierarchy(WINDOW) is None
    assert current.reads == []
