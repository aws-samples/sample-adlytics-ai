# SPDX-License-Identifier: MIT-0
"""Folds keep a bounded summary and enforce it (ADR-143, design 03 §3.4, round-2 M3)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from domain.catalog.capability import MetricKind
from domain.measurement.figures import Figure
from domain.measurement.folds import (
    FOLD_INELIGIBLE,
    FOLD_UNDECLARED_CELL,
    AuthorizedDailySums,
    FoldResult,
    KeysByGroup,
    ObservedKeys,
    require_foldable,
)
from domain.shared_kernel.errors import InvariantError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-143")]

DAYS = (date(2026, 8, 1), date(2026, 8, 2))


def _figure(value: int = 1, **dimensions: str) -> Figure:
    return Figure(dimensions=tuple(dimensions.items()), value=Decimal(value))


def test_observed_keys_at_exactly_max_kept_is_not_overflowed() -> None:
    fold = ObservedKeys(("campaign_key",), max_kept=3)
    for key in ("c1", "c2", "c3", "c1"):
        fold.add(_figure(campaign_key=key))
    assert fold.value() == (("c1",), ("c2",), ("c3",))
    assert not fold.overflowed


def test_observed_keys_one_more_than_max_kept_overflows_and_keeps_no_more() -> None:
    """Paired with the exact case above: the fourth distinct key overflows, nothing is kept."""
    fold = ObservedKeys(("campaign_key",), max_kept=3)
    for key in ("c1", "c2", "c3", "c4", "c5"):
        fold.add(_figure(campaign_key=key))
    assert fold.overflowed
    assert len(fold.value()) == fold.max_kept == 3
    assert ("c4",) not in fold.value()


def test_keys_by_group_at_max_kept_and_one_more_count_the_total_over_groups() -> None:
    exact = KeysByGroup("campaign_key", "segment_id", max_kept=4)
    for group, key in (("a", "s1"), ("a", "s2"), ("b", "s1"), ("c", "s1"), ("a", "s1")):
        exact.add(_figure(campaign_key=group, segment_id=key))
    assert not exact.overflowed
    assert exact.value() == {"a": ("s1", "s2"), "b": ("s1",), "c": ("s1",)}

    # Spread over many groups, one key each: the bound is the total, never per group.
    spread = KeysByGroup("campaign_key", "segment_id", max_kept=4)
    for index in range(5):
        spread.add(_figure(campaign_key=f"g{index}", segment_id="s1"))
    assert spread.overflowed
    assert sum(len(keys) for keys in spread.value().values()) == 4


def test_authorized_daily_sums_add_only_authorized_identities_and_never_overflow() -> None:
    subsets = {"all": frozenset({"c1", "c2"}), "dsp": frozenset({"c2"})}
    fold = AuthorizedDailySums(subsets, DAYS, identity="campaign_key", day_column="date_key")
    fold.add(_figure(3, campaign_key="c1", date_key="2026-08-01"))
    fold.add(_figure(4, campaign_key="c2", date_key="2026-08-01"))
    fold.add(_figure(5, campaign_key="c9", date_key="2026-08-02"))
    assert fold.value() == {
        "all": {DAYS[0]: Decimal(7), DAYS[1]: Decimal(0)},
        "dsp": {DAYS[0]: Decimal(4), DAYS[1]: Decimal(0)},
    }
    assert fold.max_kept == 4 and not fold.overflowed
    # Its inputs are the render's own structure, held by reference, never copied.
    assert fold.subsets is subsets


def test_authorized_daily_sums_refuse_an_undeclared_day() -> None:
    """Paired with the declared days above: a day outside the fenced window is a defect."""
    fold = AuthorizedDailySums(
        {"all": frozenset({"c1"})}, DAYS, identity="campaign_key", day_column="date_key"
    )
    with pytest.raises(InvariantError) as raised:
        fold.add(_figure(1, campaign_key="c1", date_key="2026-08-03"))
    assert raised.value.code == FOLD_UNDECLARED_CELL


@pytest.mark.parametrize("bound", [0, -1])
def test_a_fold_keeps_at_least_one_entry(bound: int) -> None:
    with pytest.raises(ValueError, match="at least one"):
        ObservedKeys(("campaign_key",), max_kept=bound)


def test_the_fold_error_codes_are_stable() -> None:
    assert (str(FOLD_INELIGIBLE), str(FOLD_UNDECLARED_CELL)) == (
        "ADLY-FOLD-0001",
        "ADLY-FOLD-0002",
    )


def test_a_large_bound_is_enforced_by_value_not_identity() -> None:
    """Paired with the small bounds above: a bound past the small-int cache still holds."""
    fold = ObservedKeys(("campaign_key",), max_kept=300)
    for index in range(301):
        fold.add(_figure(campaign_key=f"c{index}"))
    assert fold.overflowed and len(fold.value()) == 300


def test_authorized_daily_sums_declare_subsets_times_days_and_refuse_either_empty() -> None:
    days = (*DAYS, date(2026, 8, 3))
    fold = AuthorizedDailySums(
        {"a": frozenset({"c1"}), "b": frozenset({"c2"})},
        days,
        identity="campaign_key",
        day_column="date_key",
    )
    assert fold.max_kept == 6
    with pytest.raises(ValueError, match="at least one subset"):
        AuthorizedDailySums({}, days, identity="campaign_key", day_column="date_key")
    with pytest.raises(ValueError, match="at least one subset"):
        AuthorizedDailySums(
            {"a": frozenset({"c1"})}, (), identity="campaign_key", day_column="date_key"
        )


def _plan(kind: MetricKind, measures: int) -> Any:
    return SimpleNamespace(
        metric=SimpleNamespace(kind=kind, name="fictional"),
        measures=tuple(f"m{index}" for index in range(measures)),
        join=None,
        roster=None,
    )


@pytest.mark.parametrize(
    ("kind", "measures", "problem"),
    [
        (MetricKind.RATIO, 1, "not a simple metric"),
        (MetricKind.SIMPLE, 2, "not one measure"),
    ],
)
def test_require_foldable_refuses_each_ineligible_shape(
    kind: MetricKind, measures: int, problem: str
) -> None:
    """Paired: a simple one-measure plan passes; each other shape is refused with its reason."""
    require_foldable(_plan(MetricKind.SIMPLE, 1))
    with pytest.raises(InvariantError) as raised:
        require_foldable(_plan(kind, measures))
    assert raised.value.code == FOLD_INELIGIBLE
    assert raised.value.context["problem"] == problem


def test_a_fold_result_is_frozen_and_slotted() -> None:
    result = FoldResult(
        value=(),
        rows_read=0,
        truncated=False,
        units=None,
        bytes_scanned=0,
        metadata=cast("Any", None),
    )
    with pytest.raises(FrozenInstanceError):
        result.rows_read = 1  # type: ignore[misc]
    assert not hasattr(result, "__dict__")
