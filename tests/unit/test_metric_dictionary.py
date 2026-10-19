# SPDX-License-Identifier: MIT-0
"""Strict compilation of executable metric declarations into axis-aware policies."""

import shutil
from pathlib import Path

import pytest

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import Catalog, CatalogError, RatioScale, references_in_definition
from domain.narrative.shaped_response import UnitKind
from domain.semantics.compatibility import AggregationKind, SemanticAxis

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-048")]
ROOT = Path(__file__).resolve().parents[2]


_AXIS_DEFAULTS_YAML = """axis_defaults:
  time: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  ecosystem: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  platform: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  channel_group: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  channel: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  campaign: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  ad_product: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  product: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  marketplace: {aggregation: exact_only, groupable: true, filterable: true, alignable: true}
  attribution_model: {aggregation: exact_only, groupable: false, filterable: true, alignable: false}
  source_option: {aggregation: exact_only, groupable: false, filterable: true, alignable: false}
  exposure_group:
    {aggregation: disjoint_within_extraction, groupable: true, filterable: true,
     alignable: false}
  frequency_bucket:
    {aggregation: disjoint_within_extraction, groupable: true, filterable: true,
     alignable: false}
  identity_space:
    {aggregation: disjoint_within_extraction, groupable: true, filterable: true,
     alignable: false}
  audience_segment: {aggregation: exact_only, groupable: true, filterable: true, alignable: true}
  acquisition_channel_group:
    {aggregation: disjoint_within_extraction, groupable: true, filterable: true,
     alignable: true}
  cohort: {aggregation: exact_only, groupable: true, filterable: true, alignable: true}
  months_since_acquisition:
    {aggregation: exact_only, groupable: true, filterable: true, alignable: true}
compatibility_defaults:
  population: amazon_retail_us
  business_calendar: iso_gregorian
  week_starts_on: 0
  profile_version: 1
"""


def _write(tmp_path: Path, metric: str) -> Path:
    """Write a one-metric dictionary beside the committed contracts, packs and taxonomy.

    Loading derives each simple metric's source platforms from those files (ADR-106), so the
    synthetic metric reads a real fact and source dataset.
    """
    root = tmp_path
    shutil.copytree(ROOT / "contracts", root / "contracts")
    shared = root / "contracts/shared"
    (shared / "metric-dictionary.yaml").write_text(
        "version: 1\n" + _AXIS_DEFAULTS_YAML + "units:\n  count: {kind: count}\n"
        "inventory_categories:\n  - id: example_category\n"
        "    label: Example category\n    display_order: 1\n"
        "    metric_ids: [example]\n"
        "metrics:\n  example:\n" + metric + "    is_default_roas: true\n"
        "deferred: []\n"
    )
    return root


def _valid_metric(*, aliases: str = "") -> str:
    return (
        "    label: Example\n    type: simple\n    aggregation: sum\n"
        "    ecosystem_aggregation: exact_only\n"
        "    time_class: daily_additive\n    time_grain: day\n"
        "    compatibility: strict\n    definition: sum(fct_retail_sales.value)\n"
        "    units: count\n    measurement_basis: [retail_actual]\n"
        "    source_system: vendor_central\n    source_datasets: [GET_VENDOR_SALES_REPORT]\n"
        f"{aliases}"
        "    valid_grains: [date]\n"
    )


def test_committed_dictionary_compiles_every_leaf_to_complete_axis_policy() -> None:
    catalog = load_catalog(ROOT)
    leaves = [metric for metric in catalog.metrics if metric.kind.value == "simple"]
    assert leaves
    for metric in leaves:
        policy = metric.aggregation_policy
        assert policy is not None
        assert set(policy.axes) == set(policy.available_axes)
        assert policy.available_axes
    reach = catalog.metric("reach_users")
    assert reach is not None and reach.aggregation_policy is not None
    assert reach.aggregation_policy.kind is AggregationKind.EXACT_DISTINCT
    assert SemanticAxis.TIME in reach.aggregation_policy.available_axes


def test_every_declared_source_dataset_has_a_writer_of_each_column_the_metric_reads() -> None:
    """A metric's source dataset names a contract that writes its column, never only a sibling.

    The load's metric-semantics rule binds a metric to the contracts that write one of its
    columns, so a dictionary entry claiming a dataset no writer of that column reads is caught
    here, not at load. Pack 4 made it matter: `reach_users` reads `dsp_impressions` through
    `amc-dsp-reach`, not through `amc-dsp-impressions`.
    """
    catalog = load_catalog(ROOT)
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    unwritten = sorted(
        (metric.name, dataset, f"{table}.{column}")
        for metric in catalog.metrics
        if metric.kind.value == "simple"
        for dataset in metric.source_datasets
        for table, column in references_in_definition(metric.definition)
        if not any(
            contract.source_dataset == dataset
            and contract.target_table == table
            and column in contract.target_columns
            for contract in contracts
        )
    )
    assert unwritten == []
    reach = catalog.metric("reach_users")
    assert reach is not None
    assert reach.source_datasets == ("sponsored_ads_traffic", "dsp_impressions")


@pytest.mark.parametrize(
    "metric",
    [
        "    label: Example\n    type: simple\n    aggregation: sum\n"
        "    time_class: daily_additive\n"
        "    time_grain: day\n    compatibility: strict\n"
        "    definition: sum(fct_x.value)\n"
        "    units: count\n    measurement_basis: [retail_actual]\n"
        "    source_system: test\n    source_datasets: [test]\n"
        "    valid_grains: [date]\n    unknown: true\n",
        "    label: Example\n    type: simple\n    aggregation: sum\n"
        "    time_class: daily_additive\n"
        "    time_grain: day\n    compatibility: strict\n"
        "    definition: sum(fct_x.value)\n"
        "    units: count\n    measurement_basis: [retail_actual]\n"
        "    source_system: test\n    source_datasets: [test]\n"
        "    valid_grains: [date]\n    non_additive: 'false'\n",
        "    label: Example\n    type: simple\n    aggregation: sum\n"
        "    time_class: daily_additive\n"
        "    time_grain: day\n    compatibility: strict\n"
        "    definition: sum(fct_x.value)\n"
        "    units: count\n    measurement_basis: [retail_actual]\n"
        "    source_system: test\n    source_datasets: [test]\n"
        "    valid_grains: [unknown_axis]\n",
    ],
)
def test_unknown_fields_wrong_booleans_and_axes_fail_closed(tmp_path: Path, metric: str) -> None:
    with pytest.raises(CatalogError):
        load_catalog(_write(tmp_path, metric))


def test_optional_customer_copy_loads_as_strict_strings(tmp_path: Path) -> None:
    catalog = load_catalog(
        _write(
            tmp_path,
            _valid_metric(
                aliases=(
                    "    customer_definition: A governed customer definition.\n"
                    "    customer_caveat: A governed customer caveat.\n"
                )
            ),
        )
    )
    metric = catalog.metric("example")
    assert metric is not None
    assert metric.customer_definition == "A governed customer definition."
    assert metric.customer_caveat == "A governed customer caveat."


@pytest.mark.parametrize("field", ["customer_definition", "customer_caveat"])
def test_customer_copy_rejects_non_string_values(tmp_path: Path, field: str) -> None:
    with pytest.raises(CatalogError, match=rf"{field} must be a string"):
        load_catalog(_write(tmp_path, _valid_metric(aliases=f"    {field}: [not, text]\n")))


def test_aliases_load_as_an_immutable_tuple(tmp_path: Path) -> None:
    catalog = load_catalog(
        _write(tmp_path, _valid_metric(aliases="    aliases: [ad-attributed ROAS]\n"))
    )
    metric = catalog.metric("example")
    assert metric is not None
    assert metric.aliases == ("ad-attributed ROAS",)


def test_aliases_must_be_a_list(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="aliases must be a list"):
        load_catalog(_write(tmp_path, _valid_metric(aliases="    aliases: sales\n")))


@pytest.mark.parametrize(
    ("aliases", "message"),
    [
        ("    aliases: ['---']\n", "must not be blank after normalization"),
        (
            "    aliases: [ad-attributed ROAS, 'AD attributed ROAS']\n",
            "must be unique after exact normalization",
        ),
    ],
)
def test_aliases_must_be_nonblank_and_normalized_unique(
    tmp_path: Path, aliases: str, message: str
) -> None:
    with pytest.raises(CatalogError, match=message):
        load_catalog(_write(tmp_path, _valid_metric(aliases=aliases)))


def test_duplicate_yaml_keys_are_refused(tmp_path: Path) -> None:
    metric = (
        "    label: Example\n    label: Other\n    type: simple\n"
        "    definition: sum(fct_x.value)\n    units: count\n"
        "    measurement_basis: [retail_actual]\n    source_system: test\n"
        "    source_datasets: [test]\n    valid_grains: [date]\n"
    )
    with pytest.raises(CatalogError, match="duplicate YAML key"):
        load_catalog(_write(tmp_path, metric))


def test_compatibility_profile_version_starts_at_one(tmp_path: Path) -> None:
    metric = (
        "    label: Example\n    type: simple\n    aggregation: sum\n"
        "    time_class: daily_additive\n    time_grain: day\n"
        "    compatibility: strict\n    definition: sum(fct_x.value)\n"
        "    units: count\n    measurement_basis: [retail_actual]\n"
        "    source_system: test\n    source_datasets: [test]\n"
        "    valid_grains: [date]\n"
    )
    root = _write(tmp_path, metric)
    path = root / "contracts/shared/metric-dictionary.yaml"
    path.write_text(path.read_text().replace("profile_version: 1", "profile_version: 0"))
    with pytest.raises(CatalogError, match="starts at 1"):
        load_catalog(root)


def test_committed_dictionary_categories_partition_computable_metrics_in_display_order() -> None:
    catalog = load_catalog(ROOT)

    assert [category.display_order for category in catalog.ordered_inventory_categories()] == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
    ]  # 7 is shoppers_and_reach, added with pack 1's buyer metrics
    assert {
        metric_id for category in catalog.inventory_categories for metric_id in category.metric_ids
    } == {metric.name for metric in catalog.metrics}
    inventory_metric_ids = [
        metric_id for category in catalog.inventory_categories for metric_id in category.metric_ids
    ]
    assert len(inventory_metric_ids) == len(catalog.metrics)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("metric_ids: [example, example]", "metric ids must be unique"),
        ("metric_ids: [unknown]", "reference unknown metrics"),
    ],
)
def test_inventory_category_registry_fails_closed_on_membership_invariants(
    tmp_path: Path, replacement: str, message: str
) -> None:
    root = _write(tmp_path, _valid_metric())
    path = root / "contracts/shared/metric-dictionary.yaml"
    path.write_text(path.read_text().replace("metric_ids: [example]", replacement))

    with pytest.raises(CatalogError, match=message):
        load_catalog(root)


def test_inventory_category_registry_rejects_duplicate_display_order(tmp_path: Path) -> None:
    root = _write(tmp_path, _valid_metric())
    path = root / "contracts/shared/metric-dictionary.yaml"
    path.write_text(
        path.read_text().replace(
            "    metric_ids: [example]\nmetrics:",
            "    metric_ids: [example]\n"
            "  - id: second_category\n    label: Second category\n"
            "    display_order: 1\n    metric_ids: [example]\nmetrics:",
        )
    )

    with pytest.raises(CatalogError, match="display orders must be unique"):
        load_catalog(root)


def test_inventory_category_registry_rejects_unreferenced_metrics() -> None:
    catalog = load_catalog(ROOT)

    with pytest.raises(CatalogError, match="unreferenced"):
        Catalog(
            metrics=catalog.metrics,
            deferred=catalog.deferred,
            inventory_categories=catalog.inventory_categories[:-1],
        )


def test_deferred_aliases_load_as_governed_customer_surfaces(tmp_path: Path) -> None:
    root = _write(tmp_path, _valid_metric())
    path = root / "contracts/shared/metric-dictionary.yaml"
    path.write_text(
        path.read_text().replace(
            "deferred: []\n",
            "deferred:\n"
            "  - metric_id: roas_incremental\n"
            "    label: Incremental ROAS\n"
            "    aliases: [iROAS]\n"
            "    kind: ratio\n"
            "    unit: ratio\n",
        )
    )

    catalog = load_catalog(root)

    assert catalog.deferred[0].aliases == ("iROAS",)
    assert [metric.name for metric in catalog.deferred_candidates("iROAS")] == ["roas_incremental"]


def test_deferred_aliases_must_be_a_list(tmp_path: Path) -> None:
    root = _write(tmp_path, _valid_metric())
    path = root / "contracts/shared/metric-dictionary.yaml"
    path.write_text(
        path.read_text().replace(
            "deferred: []\n",
            "deferred:\n"
            "  - metric_id: roas_incremental\n"
            "    label: Incremental ROAS\n"
            "    aliases: iROAS\n"
            "    kind: ratio\n"
            "    unit: ratio\n",
        )
    )

    with pytest.raises(CatalogError, match="aliases must be a list"):
        load_catalog(root)


def test_integer_fields_reject_yaml_boolean_values(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="multiplier must be an integer"):
        load_catalog(_write(tmp_path, _valid_metric(aliases="    multiplier: true\n")))


def test_nested_yaml_duplicate_keys_are_refused_before_metric_loading(tmp_path: Path) -> None:
    root = _write(tmp_path, _valid_metric())
    path = root / "contracts/shared/metric-dictionary.yaml"
    path.write_text(
        path.read_text().replace(
            "units:\n  count: {kind: count}\n",
            "units:\n  count:\n    kind: count\n    kind: currency\n",
        )
    )

    with pytest.raises(CatalogError, match="duplicate YAML key"):
        load_catalog(root)


_RATES = frozenset(
    {
        "ctr",
        "viewable_rate",
        "ntb_rate_12mo",
        "ntb_revenue_share",
        "purchase_rate_of_conversions",
        "click_to_purchase_rate",
        "conversion_rate_retail",
        "return_rate",
        "fulfilment_rate",
        "tacos",
        # Pack 5 (design §6.3, §6.4): shares and rates inside one exposure-groups delivery.
        "overlap_share",
        "overlap_share_all_ids",
        "dsp_only_reach_share",
        "search_rate",
        "purchaser_rate",
        # Packs 6 and 7 (design §6.3 D2): a rate inside one frequency bucket.
        "purchaser_rate_by_frequency",
        # Pack 8 (design §6.5 C1): a rate inside one campaign and segment row.
        "segment_purchase_rate",
        # Packs 10-12 (design §6.5 C2): a segment's share of its delivery's profiled base.
        "segment_buyer_share",
        "segment_exposed_share",
        # Packs 14 and 15 (design §6.6 B3): cohort rates inside one vintage's cell, and the ARP
        # path's rates over pack 14's account-ID cohort size (D-8 (b)).
        "repeat_buyer_rate",
        "cumulative_repeat_rate",
        "repeat_buyer_rate_account_ids",
        "cumulative_repeat_rate_account_ids",
        "repeat_buyer_rate_arp",
        "cumulative_repeat_rate_arp",
    }
)
_MULTIPLIERS = frozenset(
    {
        "avg_frequency",
        "units_per_purchase",
        "roas_attributed_14d",
        "portfolio_roas_attributed_14d",
        "roas_attributed_incl_halo_14d",
        "roas_deduplicated_incl_halo_14d",
        "roas_blended",
        "purchaser_rate_index_vs_sponsored_only",
        "roas_incl_halo_by_frequency",
        "segment_roas_attributed",
        # Packs 10-12 (design §6.5 C2): a segment's buyer share over its reached share.
        "segment_index",
    }
)


@pytest.mark.covers("ADR-076")
def test_every_ratio_metric_declares_the_design_ratio_scale_and_no_other_does() -> None:
    catalog = load_catalog(ROOT)
    scales = {
        metric.name: metric.ratio_scale
        for metric in catalog.metrics
        if metric.unit_kind is UnitKind.RATIO
    }
    assert {name for name, scale in scales.items() if scale is RatioScale.RATE} == _RATES
    assert {name for name, scale in scales.items() if scale is RatioScale.MULTIPLIER} == (
        _MULTIPLIERS
    )
    assert all(
        metric.ratio_scale is None
        for metric in catalog.metrics
        if metric.unit_kind is not UnitKind.RATIO
    )


def _committed_root(tmp_path: Path, text: str) -> Path:
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    (tmp_path / "contracts/shared/metric-dictionary.yaml").write_text(text)
    return tmp_path


@pytest.mark.covers("ADR-076")
def test_a_ratio_without_its_scale_or_a_scale_off_a_ratio_is_refused(tmp_path: Path) -> None:
    text = (ROOT / "contracts/shared/metric-dictionary.yaml").read_text()
    assert text.count("    units: ratio\n    ratio_scale: rate\n") >= 1
    missing = text.replace("    units: ratio\n    ratio_scale: rate\n", "    units: ratio\n", 1)
    with pytest.raises(CatalogError, match="ratio unit requires ratio_scale"):
        load_catalog(_committed_root(tmp_path / "missing", missing))
    wrong = text.replace("    ratio_scale: rate\n", "    ratio_scale: percent\n", 1)
    with pytest.raises(CatalogError, match="expected rate or multiplier"):
        load_catalog(_committed_root(tmp_path / "wrong", wrong))
    off_ratio = text.replace("    units: count\n", "    units: count\n    ratio_scale: rate\n", 1)
    with pytest.raises(CatalogError, match="ratio_scale on a non-ratio unit"):
        load_catalog(_committed_root(tmp_path / "off", off_ratio))
    # Positive control: the committed text itself loads.
    assert load_catalog(_committed_root(tmp_path / "committed", text)).metric("ctr") is not None
