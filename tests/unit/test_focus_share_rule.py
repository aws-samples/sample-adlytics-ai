# SPDX-License-Identifier: MIT-0
"""C2/C3: driver shares name their basis, and no share inverts the attribution (design §4.6).

The earlier unsigned share (`abs(change) / abs(total)`) read every in-scope driver against the
portfolio's change, so a driver that moved with the portfolio could read as moving against it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import cast

import pytest
from change_support import SEMANTICS

from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.insights import (
    InsightOutputClass,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
)
from domain.presentation.metric_focus import (
    BuildMetricFocusRequest,
    MetricFocusDriverCandidate,
    MetricFocusDriverEntry,
    MetricFocusDriverGrain,
    MetricFocusDrivers,
    MetricFocusDriversAbsent,
    MetricFocusDriverScope,
    MetricFocusDriversSummary,
    MetricFocusIdentity,
    MetricFocusIngredients,
    MetricFocusSeries,
    MetricFocusSeriesPoint,
    MetricFocusShareBasis,
    MetricFocusValue,
    all_product_drivers,
    build_metric_focus,
    focus_drivers,
    metric_focus_payload,
)
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange, Grain

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-076")]
USD = Units(UnitKind.CURRENCY, "USD")
COUNT = Units(UnitKind.COUNT)
PERIOD = DateRange(date(2026, 8, 13), date(2026, 8, 19))
PRIOR = DateRange(date(2026, 8, 6), date(2026, 8, 12))
GRAIN = Grain(("period", "marketplace"))


def _ingredients(  # noqa: PLR0913 -- one fictional focus, every coordinate by keyword
    changes: tuple[int, ...],
    *,
    total: int,
    population: int | None = None,
    grain: MetricFocusDriverGrain = MetricFocusDriverGrain.CAMPAIGN,
    units: Units = USD,
    series: MetricFocusSeries | None = None,
) -> MetricFocusIngredients:
    base = 10_000
    candidates = tuple(
        MetricFocusDriverCandidate(
            f"Fictional Campaign {chr(65 + index)}",
            Decimal(base + change),
            Decimal(base),
            units=units,
        )
        for index, change in enumerate(changes)
    )
    prior = 100_000
    return MetricFocusIngredients(
        metric_id="spend",
        period=PERIOD,
        prior_period=PRIOR,
        current=MetricFocusValue(
            Decimal(prior + total), units, PERIOD, GRAIN, MeasurementBasis.AD_ATTRIBUTED
        ),
        prior=MetricFocusValue(Decimal(prior), units, PRIOR, GRAIN, MeasurementBasis.AD_ATTRIBUTED),
        series=series,
        drivers_grain=grain,
        driver_candidates=candidates,
        drivers_absent_reason=None,
        neighbors=("spend",),
        boundary=None,
        additive=True,
        driver_population=population,
    )


def _drivers(
    ingredients: MetricFocusIngredients,
) -> tuple[MetricFocusDrivers, MetricFocusDriversSummary | None]:
    drivers, summary = focus_drivers(ingredients)
    assert not isinstance(drivers, MetricFocusDriversAbsent)
    return drivers, summary


def _by_name(drivers: MetricFocusDrivers) -> dict[str, tuple[str | None, bool, str | None]]:
    return {
        entry.name: (entry.share_of_move, entry.moves_against_basis, entry.chip)
        for entry in drivers.entries
    }


def test_caveat_one_opposite_sign_nets_share_against_the_scope_net() -> None:
    """In scope +1,970, outside -6,846, total -4,876 (report §6), N = 4 of M = 60."""
    drivers, summary = _drivers(_ingredients((2500, 900, -978, -452), total=-4876, population=60))
    scope = "the change across these 4 campaigns"
    assert drivers.share_basis is MetricFocusShareBasis.SCOPE
    assert drivers.scope is not None and drivers.scope.share_suffix == f"of {scope}"
    assert _by_name(drivers) == {
        "Fictional Campaign A": (None, False, f"Larger than {scope}"),
        "Fictional Campaign B": ("46%", False, None),
        "Fictional Campaign C": (None, True, f"Moved against {scope}"),
        "Fictional Campaign D": (None, True, f"Moved against {scope}"),
    }
    assert summary is not None
    assert dict(summary.lines)["campaigns outside scope change"] == (
        "The 56 campaigns not among these 4: -6,846.00 USD"
    )
    assert dict(summary.lines)["direction counts in scope"] == (
        "Among these 4: 2 rose, 2 fell, 0 were flat"
    )


def test_same_sign_nets_share_against_the_overall_change() -> None:
    drivers, _ = _drivers(_ingredients((300, 200), total=1000, population=10))
    assert drivers.share_basis is MetricFocusShareBasis.PORTFOLIO
    assert drivers.scope is not None and drivers.scope.share_suffix == "of the overall change"
    assert _by_name(drivers) == {
        "Fictional Campaign A": ("30%", False, None),
        "Fictional Campaign B": ("20%", False, None),
    }


def test_an_outside_net_of_zero_shares_against_the_overall_change() -> None:
    drivers, summary = _drivers(_ingredients((300, 200), total=500))
    assert drivers.share_basis is MetricFocusShareBasis.PORTFOLIO
    assert _by_name(drivers)["Fictional Campaign A"] == ("60%", False, None)
    # N = M: no outside line, and the scope reads as the whole portfolio.
    assert summary is not None and summary.outside_scope_change is None
    assert drivers.scope is not None
    assert drivers.scope.label == "Largest changes among all 2 campaigns in the portfolio"


def test_a_scope_net_of_zero_shows_no_shares() -> None:
    drivers, summary = _drivers(_ingredients((100, -100), total=500, population=10))
    assert drivers.share_basis is MetricFocusShareBasis.NONE
    assert drivers.scope is not None and drivers.scope.share_suffix is None
    assert _by_name(drivers) == {
        "Fictional Campaign A": (None, False, None),
        "Fictional Campaign B": (None, True, "Moved against the overall change"),
    }
    assert summary is not None
    assert dict(summary.lines)["driver shares"] == (
        "Shares aren't shown because the changes among these 2 campaigns cancel out."
    )


def test_a_driver_larger_than_its_basis_or_against_it_gets_a_chip() -> None:
    drivers, _ = _drivers(_ingredients((800, -100), total=750, population=10))
    assert drivers.share_basis is MetricFocusShareBasis.PORTFOLIO
    assert _by_name(drivers) == {
        "Fictional Campaign A": (None, False, "Larger than the overall change"),
        "Fictional Campaign B": (None, True, "Moved against the overall change"),
    }


def test_a_zero_change_has_a_zero_share_and_no_chip() -> None:
    drivers, _ = _drivers(_ingredients((300, 0), total=300))
    assert _by_name(drivers)["Fictional Campaign B"] == ("0%", False, None)


def test_the_summary_splits_listed_other_and_outside_changes() -> None:
    drivers, summary = _drivers(_ingredients((50, 40, 30, 20, 10, 5), total=200, population=8))
    assert len(drivers.entries) == 4
    assert summary is not None
    assert [text for _, text in summary.lines] == [
        "Largest changes among the 6 campaigns with the most ad spend in this window, of 8 "
        "in the portfolio",
        "The 4 listed campaigns: 140.00 USD",
        "The other 2 of these 6 campaigns: 15.00 USD",
        "The 2 campaigns not among these 6: 45.00 USD",
        "Among these 6: 6 rose, 0 fell, 0 were flat",
    ]
    assert (summary.rose, summary.fell, summary.flat) == (6, 0, 0)


def test_products_use_their_own_noun_and_never_have_an_outside_line() -> None:
    drivers, summary = _drivers(
        _ingredients((30, -10), total=20, grain=MetricFocusDriverGrain.PRODUCT, units=COUNT)
    )
    assert drivers.scope is not None
    assert drivers.scope.label == "Largest changes among all 2 products in this view"
    assert summary is not None and summary.outside_scope_change is None


def test_ratio_components_carry_no_scope_basis_or_summary() -> None:
    drivers, summary = _drivers(
        _ingredients((30, -10), total=-5, grain=MetricFocusDriverGrain.RATIO_COMPONENT)
    )
    assert drivers.scope is None and drivers.share_basis is None and summary is None
    # Compared with the metric's own change (-5): the +30 component moved against it.
    assert _by_name(drivers)["Fictional Campaign A"] == (
        None,
        True,
        "Moved against the overall change",
    )


def test_day_steps_carry_no_scope_basis_or_summary() -> None:
    series = MetricFocusSeries(
        (
            MetricFocusSeriesPoint(date(2026, 8, 13), 10, None),
            MetricFocusSeriesPoint(date(2026, 8, 14), 30, None),
            MetricFocusSeriesPoint(date(2026, 8, 15), 25, None),
        )
    )
    drivers, summary = _drivers(
        _ingredients((), total=-5, grain=MetricFocusDriverGrain.DAY, series=series)
    )
    assert drivers.scope is None and drivers.share_basis is None and summary is None
    assert [entry.moves_against_basis for entry in drivers.entries] == [True, False]


def test_a_scope_exists_exactly_on_additive_grains() -> None:
    entries = _drivers(_ingredients((300, 200), total=500))[0].entries
    with pytest.raises(ValueError, match="additive grains"):
        MetricFocusDrivers(MetricFocusDriverGrain.CAMPAIGN, entries)
    with pytest.raises(ValueError, match="additive grains"):
        MetricFocusDrivers(
            MetricFocusDriverGrain.DAY,
            entries,
            share_basis=MetricFocusShareBasis.PORTFOLIO,
        )


def _payload(ingredients: MetricFocusIngredients) -> dict[str, object]:
    focus = build_metric_focus(
        BuildMetricFocusRequest(
            ingredients,
            MetricFocusIdentity(
                "spend",
                "Ad spend",
                "A fictional definition.",
                USD,
                MeasurementBasis.AD_ATTRIBUTED,
                GRAIN,
            ),
            InsightSlot(
                InsightSlotId.METRIC_FOCUS,
                InsightOutputClass.PERSPECTIVE,
                InsightSlotState.UNAVAILABLE,
                None,
                None,
                "fictional-revision",
                "unresolved",
            ),
            {},
            SEMANTICS,
        )
    )
    return metric_focus_payload(focus)


def test_the_revised_payload_keys_are_exact_on_an_additive_grain() -> None:
    payload = _payload(_ingredients((300, 200), total=1000, population=10))
    drivers = cast("dict[str, object]", payload["drivers"])
    entries = cast("list[dict[str, object]]", drivers["entries"])
    summary = cast("dict[str, object]", payload["drivers_summary"])
    assert set(drivers) == {"grain", "scope", "share_basis", "entries"}
    assert set(cast("dict[str, object]", drivers["scope"])) == {
        "count",
        "population",
        "label",
        "share_suffix",
    }
    assert set(entries[0]) == {
        "name",
        "change",
        "units",
        "display_change",
        "delta",
        "share_of_move",
        "moves_against_basis",
        "chip",
    }
    assert set(summary) == {
        "listed_change",
        "other_in_scope_change",
        "outside_scope_change",
        "rose",
        "fell",
        "flat",
        "lines",
    }
    assert summary["listed_change"] == {"value": "500", "display": "500.00 USD"}
    assert summary["other_in_scope_change"] is None


def test_the_revised_payload_is_null_by_grain_on_ratio_components() -> None:
    payload = _payload(
        _ingredients((30, -10), total=-5, grain=MetricFocusDriverGrain.RATIO_COMPONENT)
    )
    drivers = cast("dict[str, object]", payload["drivers"])
    assert drivers["scope"] is None and drivers["share_basis"] is None
    assert payload["drivers_summary"] is None


# Mutation review (2026-10-06, ADR-013) -----------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "total", "population", "basis", "expected"),
    [
        # Same-sign nets whose outside move opposes the scope: shares of the scope net.
        (
            (300, 200),
            300,
            10,
            MetricFocusShareBasis.SCOPE,
            {"A": ("60%", False, None), "B": ("40%", False, None)},
        ),
        # A falling scope beside a rising total: shares of the (negative) scope net.
        (
            (-300, -200),
            300,
            10,
            MetricFocusShareBasis.SCOPE,
            {"A": ("60%", False, None), "B": ("40%", False, None)},
        ),
        # Falling nets moving together: a rising driver moves against the overall change.
        (
            (-600, 100),
            -1000,
            10,
            MetricFocusShareBasis.PORTFOLIO,
            {"A": ("60%", False, None), "B": (None, True, "Moved against the overall change")},
        ),
        # An outside move of minus 50 cents still opposes a rising scope.
        (
            (Decimal("200.25"), Decimal("100.25")),
            Decimal("300.00"),
            10,
            MetricFocusShareBasis.SCOPE,
            {"A": ("67%", False, None), "B": ("33%", False, None)},
        ),
        # And plus 50 cents opposes a falling one.
        (
            (Decimal("-200.25"), Decimal("-100.25")),
            Decimal("-300.00"),
            10,
            MetricFocusShareBasis.SCOPE,
            {"A": ("67%", False, None), "B": ("33%", False, None)},
        ),
        # A basis of exactly one and drivers under one in size.
        (
            (Decimal("0.50"), Decimal("-0.40")),
            Decimal("1.00"),
            10,
            MetricFocusShareBasis.PORTFOLIO,
            {"A": ("50%", False, None), "B": (None, True, "Moved against the overall change")},
        ),
        # A negative basis smaller than one in size.
        (
            (Decimal("-0.30"), Decimal("0.10")),
            Decimal("-0.50"),
            10,
            MetricFocusShareBasis.PORTFOLIO,
            {"A": ("60%", False, None), "B": (None, True, "Moved against the overall change")},
        ),
        # A basis of exactly minus one: a rising driver still moves against it.
        (
            (Decimal("-0.50"), Decimal("0.40")),
            Decimal("-1.00"),
            10,
            MetricFocusShareBasis.PORTFOLIO,
            {"A": ("50%", False, None), "B": (None, True, "Moved against the overall change")},
        ),
        # A driver exactly equal to its basis is 100 %, never "Larger than".
        ((500,), 500, None, MetricFocusShareBasis.PORTFOLIO, {"A": ("100%", False, None)}),
    ],
)
def test_the_share_rule_holds_at_its_sign_and_size_edges(  # noqa: PLR0913, PLR0917
    changes: tuple[int | Decimal, ...],
    total: int | Decimal,
    population: int | None,
    basis: MetricFocusShareBasis,
    expected: dict[str, tuple[str | None, bool, str | None]],
) -> None:
    drivers, _ = _drivers(
        _ingredients(
            cast("tuple[int, ...]", changes), total=cast("int", total), population=population
        )
    )
    assert drivers.share_basis is basis
    assert _by_name(drivers) == {
        f"Fictional Campaign {name}": value for name, value in expected.items()
    }


def test_drivers_rank_by_size_of_change_not_by_name() -> None:
    drivers, summary = _drivers(
        _ingredients((10, 50, -30, 5, 2, 1, 40, 3, 4), total=85, population=12)
    )
    assert [entry.name[-1] for entry in drivers.entries] == ["B", "G", "C", "A"]
    assert summary is not None
    assert dict(summary.lines)["other campaigns in scope change"] == (
        "The other 5 of these 9 campaigns: 15.00 USD"
    )


def test_product_summary_lines_use_the_product_noun() -> None:
    _, summary = _drivers(
        _ingredients((30, -10), total=20, grain=MetricFocusDriverGrain.PRODUCT, units=COUNT)
    )
    assert summary is not None
    assert dict(summary.lines)["listed drivers change"].startswith("The 2 listed products: ")


def test_the_scope_rule_refuses_each_half_of_a_mismatched_declaration() -> None:
    entries = _drivers(_ingredients((300, 200), total=500))[0].entries
    scope = MetricFocusDriverScope(2, 2, "Largest changes among all 2 campaigns", None)
    for grain, kwargs in (
        (MetricFocusDriverGrain.DAY, {"scope": scope}),
        (MetricFocusDriverGrain.CAMPAIGN, {"share_basis": MetricFocusShareBasis.PORTFOLIO}),
        (MetricFocusDriverGrain.CAMPAIGN, {"scope": scope}),
    ):
        with pytest.raises(ValueError, match="additive grains"):
            MetricFocusDrivers(grain, entries, **kwargs)  # type: ignore[arg-type]


def test_driver_records_are_immutable_and_default_to_no_chip() -> None:
    drivers, summary = _drivers(_ingredients((300, 200), total=500, population=10))
    assert drivers.scope is not None and summary is not None
    for record, name in (
        (drivers.scope, "label"),
        (summary.listed_change, "display"),
        (summary, "rose"),
    ):
        with pytest.raises(AttributeError):
            setattr(record, name, "changed")
    plain = MetricFocusDriverEntry(
        "Fictional Campaign", Decimal(1), None, units=USD, current=Decimal(2), prior=Decimal(1)
    )
    assert plain.moves_against_basis is False and plain.chip is None


def test_all_product_drivers_exist_only_for_served_product_candidates() -> None:
    product = _ingredients((30, -10), total=20, grain=MetricFocusDriverGrain.PRODUCT, units=COUNT)
    built = all_product_drivers(product)
    assert built is not None and len(built[0].entries) == 2
    for refused in (
        _ingredients((30, -10), total=20),
        _ingredients((30, -10), total=-5, grain=MetricFocusDriverGrain.RATIO_COMPONENT),
        replace(product, driver_candidates=()),
        replace(product, drivers_absent_reason="No product rows are served for this window."),
    ):
        assert all_product_drivers(refused) is None
