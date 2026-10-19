# SPDX-License-Identifier: MIT-0
"""`exact_only_axes`: media or product grains a simple metric never sums across (design §6.2).

No shipped metric declares the key, so each test injects one synthetic metric into a copy of
the contracts. The synthetic metric reads an existing column; only its declared axes matter.
Every refusal is asserted with the planner's or loader's exact signal beside a positive
control.
"""

from __future__ import annotations

import pathlib
import shutil
from datetime import date

import pytest

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import GRAIN_NOT_SUPPORTED_CODE, Catalog, CatalogError
from domain.measurement.planning import FactShape, PlanRequest, plan
from domain.semantics.compatibility import AxisBehavior, SemanticAxis
from domain.shared_kernel.errors import DomainError
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-106")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
WEEK = DateRange(date(2026, 8, 10), date(2026, 8, 16))
ONE_MARKETPLACE = {"marketplace_key": "ATVPDKIKX0DER", "rows_suppressed": "false"}
SYNTHETIC = "synthetic_exact_only_purchases"
ADVERTISED = "purchases_new_to_brand_12mo"
_ANCHOR = "  revenue_new_to_brand_12mo:\n"
_INVENTORY = f"{ADVERTISED},"
_DECLARED = "    exact_only_axes: [channel_group, channel, ad_product]\n"


def _entry(axes_line: str) -> str:
    return f"""  {SYNTHETIC}:
    label: Synthetic exact-only purchases
    type: simple
    aggregation: sum
    ecosystem_aggregation: exact_only
{axes_line}    time_class: daily_additive
    time_grain: day
    compatibility: strict
    definition: sum(fct_attributed_conversion.{ADVERTISED})
    units: count
    measurement_basis: [ad_attributed]
    source_system: amc
    source_datasets: [amazon_attributed_events_by_conversion_time]
    lookback_months: 12
    valid_grains: [date, ecosystem, platform, channel_group, channel, campaign, ad_product, product, marketplace]
"""  # noqa: E501 — one YAML line, as the dictionary writes it


def _root_with(tmp_path: pathlib.Path, axes_line: str, *, onto: str | None = None) -> pathlib.Path:
    """Copy the contracts and add the synthetic metric, or add `axes_line` to metric `onto`."""
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    path = tmp_path / "contracts/shared/metric-dictionary.yaml"
    text = path.read_text()
    anchor = _ANCHOR if onto is None else f"  {onto}:\n"
    assert text.count(anchor) == 1
    if onto is None:
        # The catalog requires an inventory category to own every computable metric.
        assert text.count(_INVENTORY) == 1
        text = text.replace(_INVENTORY, f"{ADVERTISED}, {SYNTHETIC},")
        text = text.replace(anchor, _entry(axes_line) + anchor)
    else:
        text = text.replace(anchor, anchor + axes_line)
    path.write_text(text)
    return tmp_path


@pytest.fixture(scope="module")
def catalog(tmp_path_factory: pytest.TempPathFactory) -> Catalog:
    return load_catalog(_root_with(tmp_path_factory.mktemp("declared"), _DECLARED))


def _plan(catalog: Catalog, metric: str, grains: tuple[str, ...]) -> str:
    shape = FactShape.for_fact("fct_attributed_conversion", list(CONTRACTS))
    return plan(catalog, PlanRequest(metric, grains, WEEK, ONE_MARKETPLACE), shape).metric.name


def test_the_declared_axes_become_exact_only_and_the_rest_keep_their_default(
    catalog: Catalog,
) -> None:
    metric = catalog.metric(SYNTHETIC)
    assert metric is not None
    policy = metric.aggregation_policy
    assert policy is not None
    declared = {SemanticAxis.CHANNEL_GROUP, SemanticAxis.CHANNEL, SemanticAxis.AD_PRODUCT}
    assert {axis: policy.axes[axis].aggregation for axis in declared} == dict.fromkeys(
        declared, AxisBehavior.EXACT_ONLY
    )
    # Positive control: undeclared media and product axes stay additive.
    assert policy.axes[SemanticAxis.CAMPAIGN].aggregation is AxisBehavior.ADDITIVE
    assert policy.axes[SemanticAxis.PRODUCT].aggregation is AxisBehavior.ADDITIVE


def test_a_total_across_a_declared_axis_is_refused_by_the_planner(catalog: Catalog) -> None:
    # Positive control: inside one channel and ad product, at product grain, it plans.
    grains = ("channel", "ad_product", "product", "marketplace")
    assert _plan(catalog, SYNTHETIC, grains) == SYNTHETIC
    # Positive control: the same column under a metric without the key sums across channels.
    assert _plan(catalog, ADVERTISED, ("product", "marketplace")) == ADVERTISED
    with pytest.raises(DomainError) as across:
        _plan(catalog, SYNTHETIC, ("product", "marketplace"))
    assert across.value.code == GRAIN_NOT_SUPPORTED_CODE
    assert across.value.context["reason"] == "grain_not_supported"
    assert across.value.description == (
        f"{SYNTHETIC} over fct_attributed_conversion would sum across channel_key, which the "
        "fact's grain says makes a row distinct. Summing across a grain column is a claim that "
        "it is addition, and for an extraction-option column it is not — a MANUFACTURING pull "
        "and a SOURCING pull describe the same units under two lenses. Group by each column or "
        "filter it."
    )
    with pytest.raises(DomainError) as ad_products:
        _plan(catalog, SYNTHETIC, ("channel", "product", "marketplace"))
    assert ad_products.value.code == GRAIN_NOT_SUPPORTED_CODE
    assert ad_products.value.description == (
        "sum: ad_product is exact_only, so it cannot use sum. Available instead: date, "
        "ecosystem, platform, channel_group, channel, campaign, ad_product, product, marketplace."
    )


def test_exact_only_axes_is_refused_outside_its_declared_shape(
    catalog: Catalog, tmp_path: pathlib.Path
) -> None:
    # Positive control: the well-formed declaration loads (the module catalog).
    assert catalog.metric(SYNTHETIC) is not None
    for index, bad in enumerate(("[date]", "[platform]", "[channel, channel]", "[]")):
        root = _root_with(tmp_path / str(index), f"    exact_only_axes: {bad}\n")
        with pytest.raises(CatalogError, match="exact_only_axes must name distinct media"):
            load_catalog(root)
    ratio = _root_with(tmp_path / "ratio", "    exact_only_axes: [channel]\n", onto="cost_per_ntb")
    with pytest.raises(CatalogError, match="only a simple metric declares exact_only_axes"):
        load_catalog(ratio)
