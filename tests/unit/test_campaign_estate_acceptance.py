# SPDX-License-Identifier: MIT-0
"""Deterministic local bulk-estate acceptance runner contract."""

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from runpy import run_path
from typing import Protocol, cast

import pytest

from adapters.campaign_artifact_renderer import (
    _merge_portfolio_answer,  # pyright: ignore[reportPrivateUsage]
)
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.preset_artifacts import MAX_PRESET_ARTIFACT_BYTES

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-071")]
ROOT = Path(__file__).resolve().parents[2]
type PortfolioAnswer = tuple[dict[tuple[str, ...], Decimal | int], Units, tuple[str, ...]]


class _Result(Protocol):
    campaign_count: int
    segment_count: int
    sibling_pair_count: int
    body_count: int
    maximum_body_bytes: int
    maximum_served_bytes: int
    measurement_plan_count: int


def test_bulk_runner_attests_full_segment_fanout_without_a_wall_clock_assertion() -> None:
    namespace = run_path(str(ROOT / "validation" / "accept_campaign_estate_local.py"))
    run = cast("Callable[[Path], _Result]", namespace["run"])
    result = run(ROOT)

    assert result.campaign_count == 1_000
    assert result.segment_count == 20
    assert result.sibling_pair_count == 199
    # 200 pairs (the parent's and 199 siblings) and the one index shard (ADR-145), as staged.
    assert result.body_count == 401
    assert 0 < result.maximum_body_bytes <= MAX_PRESET_ARTIFACT_BYTES
    assert result.maximum_body_bytes < result.maximum_served_bytes <= MAX_PRESET_ARTIFACT_BYTES
    # 30d chunks the trend into three windows; Delivery per channel adds clicks and the DSP
    # viewability operands; the row spark is no longer measured (ADR-127).
    assert result.measurement_plan_count == 34


def test_chunk_merge_requires_identical_units_qualifiers_and_disjoint_dates() -> None:
    units = Units(UnitKind.COUNT)
    first: PortfolioAnswer = ({("2026-09-01",): 1}, units, ("fresh",))
    second: PortfolioAnswer = ({("2026-09-02",): 2}, units, ("fresh",))
    changed_qualifier: PortfolioAnswer = ({("2026-09-02",): 2}, units, ("stale",))
    duplicate: PortfolioAnswer = ({("2026-09-01",): 2}, units, ("fresh",))
    changed_units: PortfolioAnswer = (
        {("2026-09-02",): 2},
        Units(UnitKind.RATIO),
        ("fresh",),
    )

    assert _merge_portfolio_answer(first, second) == (
        {("2026-09-01",): 1, ("2026-09-02",): 2},
        units,
        ("fresh",),
    )
    assert _merge_portfolio_answer(first, changed_qualifier) is None
    assert _merge_portfolio_answer(first, duplicate) is None
    assert _merge_portfolio_answer(first, changed_units) is None
