# SPDX-License-Identifier: MIT-0
"""Outcome metrics stay inside one network by type (ADR-106; AC-BR.1, AC-BR.1b to AC-BR.1d).

Three rules, each with its refusal and a paired positive control:

- every entry under `metrics:` declares `ecosystem_aggregation`, and only `ad_spend` is additive;
- each simple metric's source platforms are derived from contracts, source packs and the media
  taxonomy, with the predicate publication selection uses;
- the planner accounts the network axes per leaf operand, so today's single-platform plans are
  unchanged and a second platform turns an omitted platform into a refusal that names the axis.

Every second network here is synthetic and built in memory; nothing is added to `contracts/`.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, replace
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import PublicationSelector, _FactSide
from adapters.outbound.source_pack import load_source_packs
from domain.catalog.capability import (
    GRAIN_NOT_SUPPORTED_CODE,
    Catalog,
    CatalogError,
    Metric,
    MetricKind,
)
from domain.measurement.periodic import periodic_leaves
from domain.measurement.planning import (
    _COLUMN_AXES,
    _GRAIN_AXES,
    GRAIN_COLUMNS,
    RATIO_OPERAND_COUNT,
    FactShape,
    FactShapes,
    GrainSetShape,
    PlanRequest,
    _additive_rollup_columns,
    _validate_semantic_axes,
    leaf_references,
    plan,
    served_grain_set,
)
from domain.semantics.calendar_periods import vintage_ending
from domain.semantics.compatibility import (
    NETWORK_AXES,
    AggregationKind,
    AggregationOperation,
    AggregationPolicy,
    AxisBehavior,
    AxisPolicy,
    RatioOfAggregatesPolicy,
    SemanticAxis,
    SemanticCompatibilityError,
)
from domain.semantics.identity import EcosystemKey, PlatformKey, SourceIdentifier
from domain.semantics.media_taxonomy import (
    MediaTaxonomy,
    MediaTaxonomyMapping,
    MediaTaxonomyResolution,
    MediaTaxonomyTerm,
)
from domain.semantics.source_pack import MediaIdentityField
from domain.semantics.source_platforms import (
    SourcePlatformsRequest,
    SourcePlatformsResult,
    derive_source_platforms,
)
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import DomainError
from domain.shared_kernel.values import DateRange

if TYPE_CHECKING:
    from domain.data_contracts.contract import DataContract
    from domain.semantics.source_pack import SourcePack
    from ports.outbound.semantic_state import CurrentPublicationInspectionPort

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-106")]

ROOT = Path(__file__).resolve().parents[2]
DICTIONARY = ROOT / "contracts/shared/metric-dictionary.yaml"
WEEK = DateRange(date(2026, 8, 10), date(2026, 8, 16))
MONTH = DateRange(date(2026, 8, 1), date(2026, 8, 31))
"""One complete calendar month: packs 6 and 7 are measured at months and quarters only."""
ONE_DAY = DateRange(date(2026, 8, 10), date(2026, 8, 10))
VINTAGE = vintage_ending(date(2026, 8, 1)).window
AMAZON_ADS = PlatformKey("amazon_ads")
AMAZON_RETAIL = PlatformKey("amazon_retail")
OTHER = EcosystemKey("other")
OTHER_ADS = PlatformKey("other_ads")
AMC_ONE_MARKETPLACE = {"marketplace_key": "ATVPDKIKX0DER", "rows_suppressed": "false"}
VENDOR_OPTIONS = {"distributor_view": "MANUFACTURING", "selling_program": "RETAIL"}


@dataclass(frozen=True, slots=True)
class _Governed:
    """The three governed declarations the walk reads besides the dictionary."""

    contracts: tuple[DataContract, ...]
    packs: tuple[SourcePack, ...]
    taxonomy: MediaTaxonomy

    def request(self, catalog: Catalog) -> SourcePlatformsRequest:
        return SourcePlatformsRequest(catalog, self.contracts, self.packs, self.taxonomy)


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(ROOT)


@pytest.fixture(scope="module")
def governed() -> _Governed:
    return _Governed(
        contracts=tuple(contract for _, contract in load_contracts(ROOT)),
        packs=load_source_packs(ROOT),
        taxonomy=load_media_taxonomy(ROOT),
    )


def _committed_root(tmp_path: Path, text: str) -> Path:
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    (tmp_path / "contracts/shared/metric-dictionary.yaml").write_text(text)
    return tmp_path


_NEXT_ENTRY = re.compile(r"\n  [a-z0-9_]+:\n|\ndeferred:")


def _entry_replaced(text: str, metric: str, change: tuple[str, str]) -> str:
    """Replace text inside one metric's entry only, failing if the entry doesn't contain it."""
    old, new = change
    start = text.index(f"\n  {metric}:\n")
    following = _NEXT_ENTRY.search(text, start + 1)
    assert following is not None
    end = following.start()
    entry = text[start:end]
    assert old in entry, f"{metric} has no {old!r}"
    return text[:start] + entry.replace(old, new, 1) + text[end:]


_TO_ADDITIVE = ("ecosystem_aggregation: exact_only", "ecosystem_aggregation: additive")


# --- The declaration: AC-BR.1 -----------------------------------------------------------------


def test_every_metric_declares_the_rule_and_only_ad_spend_is_additive(catalog: Catalog) -> None:
    """AC-BR.1: no attributed outcome, shopper count or delivery count sums across networks."""
    assert all(metric.ecosystem_aggregation is not None for metric in catalog.metrics)
    additive = {
        metric.name
        for metric in catalog.metrics
        if metric.ecosystem_aggregation is AxisBehavior.ADDITIVE
    }
    assert additive == {"ad_spend"}
    for metric in catalog.metrics:
        policy = metric.aggregation_policy
        if policy is None:
            continue
        assert policy.ecosystem_aggregation is metric.ecosystem_aggregation
        for axis in NETWORK_AXES & policy.available_axes:
            assert policy.axes[axis].aggregation is metric.ecosystem_aggregation, metric.name
    ad_spend = catalog.metric("ad_spend")
    assert ad_spend is not None and ad_spend.aggregation_policy is not None
    # Within one marketplace and currency only: marketplace stays exact.
    marketplace = ad_spend.aggregation_policy.axes[SemanticAxis.MARKETPLACE]
    assert marketplace.aggregation is AxisBehavior.EXACT_ONLY


def test_every_entry_under_metrics_carries_the_field_in_the_file(catalog: Catalog) -> None:
    """The back-fill itself: one declaration per entry, none inherited or defaulted."""
    text = DICTIONARY.read_text()
    metrics_block = text[text.index("\nmetrics:\n") : text.index("\ndeferred:")]
    lines = metrics_block.splitlines()
    entries = [line for line in lines if line.startswith("    aggregation:")]
    declared = [line for line in lines if line.startswith("    ecosystem_aggregation:")]
    assert len(entries) == len(declared) == len(catalog.metrics)


def test_a_metric_without_the_field_fails_loading(tmp_path: Path) -> None:
    text = DICTIONARY.read_text()
    missing = _entry_replaced(text, "impressions", ("    ecosystem_aggregation: exact_only\n", ""))
    with pytest.raises(CatalogError, match=r"impressions requires ecosystem_aggregation"):
        load_catalog(_committed_root(tmp_path / "missing", missing))
    unknown = _entry_replaced(
        text, "impressions", ("ecosystem_aggregation: exact_only", "ecosystem_aggregation: often")
    )
    with pytest.raises(CatalogError, match=r"impressions.ecosystem_aggregation is 'often'"):
        load_catalog(_committed_root(tmp_path / "unknown", unknown))
    # Positive control: the committed text loads.
    assert load_catalog(_committed_root(tmp_path / "committed", text)).metric("impressions")


def test_a_distinct_count_declared_additive_is_refused_at_load(tmp_path: Path) -> None:
    """The dictionary adapter reports the policy's refusal, naming the metric."""
    text = DICTIONARY.read_text()
    additive = _entry_replaced(text, "reach_users", _TO_ADDITIVE)
    with pytest.raises(CatalogError, match=r"reach_users: ecosystem_aggregation is additive"):
        load_catalog(_committed_root(tmp_path / "additive", additive))
    assert load_catalog(_committed_root(tmp_path / "committed", text)).metric("reach_users")


def test_a_composite_cannot_claim_more_than_its_operands(tmp_path: Path) -> None:
    """`tacos` divides additive spend by exact-only revenue, so it can't be additive itself."""
    text = DICTIONARY.read_text()
    claimed = _entry_replaced(text, "tacos", _TO_ADDITIVE)
    with pytest.raises(
        CatalogError, match=r"tacos: ecosystem_aggregation is additive, but its operands imply"
    ):
        load_catalog(_committed_root(tmp_path / "claimed", claimed))
    assert load_catalog(_committed_root(tmp_path / "committed", text)).metric("tacos")


def _policy(network: AxisBehavior, declared: AxisBehavior | None) -> AggregationPolicy:
    rule = AxisPolicy(aggregation=network, groupable=True, filterable=True, alignable=True)
    time = AxisPolicy(
        aggregation=AxisBehavior.ADDITIVE, groupable=True, filterable=True, alignable=True
    )
    return AggregationPolicy(
        kind=AggregationKind.SUM,
        available_axes=frozenset({SemanticAxis.TIME, *NETWORK_AXES}),
        axes={SemanticAxis.TIME: time, **dict.fromkeys(NETWORK_AXES, rule)},
        ecosystem_aggregation=declared,
    )


def test_the_policy_refuses_a_declaration_its_axes_contradict() -> None:
    with pytest.raises(SemanticCompatibilityError, match="exact_only, but ecosystem, platform"):
        _policy(AxisBehavior.ADDITIVE, AxisBehavior.EXACT_ONLY)
    with pytest.raises(SemanticCompatibilityError, match="expected additive or exact_only"):
        _policy(AxisBehavior.FORBIDDEN, AxisBehavior.FORBIDDEN)
    assert _policy(AxisBehavior.EXACT_ONLY, AxisBehavior.EXACT_ONLY).ecosystem_aggregation
    assert _policy(AxisBehavior.ADDITIVE, AxisBehavior.ADDITIVE).ecosystem_aggregation


# --- Derived source platforms -----------------------------------------------------------------


def test_v1_platforms_are_amazon_ads_for_ads_and_amazon_retail_for_retail(
    catalog: Catalog,
) -> None:
    retail = {
        "retail_revenue_shipped",
        "revenue_ordered",
        "units_shipped",
        "units_ordered",
        "cogs_shipped",
        "amazon_margin_proxy",
        "customer_returns",
        "glance_views",
        # AMC's Amazon Retail Purchases table, labelled amazon_retail by its source pack.
        "retail_revenue_purchased",
        "retail_units_purchased",
    }
    for metric in catalog.metrics:
        if metric.kind is MetricKind.SIMPLE:
            expected = {AMAZON_RETAIL} if metric.name in retail else {AMAZON_ADS}
            assert metric.source_platforms == expected, metric.name
        else:
            assert metric.source_platforms == frozenset(), metric.name


def _with_other_platform(
    taxonomy: MediaTaxonomy, source: SourceIdentifier, valid_from: date
) -> MediaTaxonomy:
    """Synthetic: name an `other` ecosystem and `other_ads` platform, and map one source to it."""
    return replace(
        taxonomy,
        terms=(
            *taxonomy.terms,
            MediaTaxonomyTerm(OTHER, "Other", ecosystem=OTHER),
            MediaTaxonomyTerm(OTHER_ADS, "Other Ads", ecosystem=OTHER, platform=OTHER_ADS),
        ),
        mappings=(
            *taxonomy.mappings,
            MediaTaxonomyMapping(
                MediaTaxonomyResolution(
                    source=source,
                    ecosystem=OTHER,
                    platform=OTHER_ADS,
                    channel_group=None,
                    channel=None,
                    ad_product=None,
                    mapping_version=1,
                    valid_from=valid_from,
                    valid_to=None,
                    evidence="synthetic second network (test only)",
                )
            ),
        ),
    )


def _second_network(governed: _Governed) -> _Governed:
    """Synthetic: another network's contract that reuses AMC's attributed-events dataset name.

    Same fact and the same `source_dataset`, under another `source_system`, another ecosystem
    and another platform. Publication selection would read it, so derivation must count it.
    """
    contract = replace(
        next(c for c in governed.contracts if c.contract_id == "amc-attributed-conversion"),
        contract_id="other-attributed-conversion",
        source_system="other_network",
    )
    pack = replace(
        next(p for p in governed.packs if p.contract_id == "amc-attributed-conversion"),
        contract_id="other-attributed-conversion",
        media=MediaIdentityField("adlytics.media_profile", literal_value="other_ads"),
    )
    return _Governed(
        contracts=(*governed.contracts, contract),
        packs=(*governed.packs, pack),
        taxonomy=_with_other_platform(
            governed.taxonomy,
            SourceIdentifier("adlytics.media_profile", "other_ads"),
            date(2000, 1, 1),
        ),
    )


def _with_platforms(catalog: Catalog, derived: SourcePlatformsResult) -> Catalog:
    return Catalog(
        metrics=tuple(
            replace(metric, source_platforms=derived.platforms[metric.name])
            if metric.name in derived.platforms
            else metric
            for metric in catalog.metrics
        ),
        deferred=catalog.deferred,
        inventory_categories=catalog.inventory_categories,
    )


def test_a_second_networks_contract_reusing_a_dataset_name_adds_its_platform(
    catalog: Catalog, governed: _Governed
) -> None:
    """The predicate ignores `source_system`, as publication selection does."""
    committed = derive_source_platforms(governed.request(catalog))
    assert committed.platforms["attributed_revenue_14d"] == {AMAZON_ADS}
    extended = derive_source_platforms(_second_network(governed).request(catalog))
    assert extended.platforms["attributed_revenue_14d"] == {AMAZON_ADS, OTHER_ADS}
    # Only metrics that read that fact from that dataset gain it.
    assert extended.platforms["ad_spend"] == {AMAZON_ADS}
    assert extended.platforms["retail_revenue_shipped"] == {AMAZON_RETAIL}


def test_a_media_path_counts_every_mapping_in_its_namespace_whatever_its_date(
    catalog: Catalog, governed: _Governed
) -> None:
    """A mapping that only starts in 2099 still counts, so effective dating hides nothing."""
    future = replace(
        governed,
        taxonomy=_with_other_platform(
            governed.taxonomy,
            SourceIdentifier("amc.ad_product_type", "other_product"),
            date(2099, 1, 1),
        ),
    )
    derived = derive_source_platforms(future.request(catalog))
    # amc-attributed-conversion reads media from the amc.ad_product_type path.
    assert derived.platforms["attributed_revenue_14d"] == {AMAZON_ADS, OTHER_ADS}
    # A literal resolves only to its own mapping: vendor rows stay amazon_retail.
    assert derived.platforms["retail_revenue_shipped"] == {AMAZON_RETAIL}


def test_a_metric_no_pack_feeds_fails_loading(tmp_path: Path) -> None:
    text = DICTIONARY.read_text()
    unfed = _entry_replaced(
        text,
        "glance_views",
        ("source_datasets: [GET_VENDOR_TRAFFIC_REPORT]", "source_datasets: [NOT_A_DATASET]"),
    )
    with pytest.raises(CatalogError, match=r"glance_views: no source pack writes"):
        load_catalog(_committed_root(tmp_path / "unfed", unfed))
    assert load_catalog(_committed_root(tmp_path / "committed", text)).metric("glance_views")


def test_a_pack_whose_media_cannot_resolve_fails_derivation(
    catalog: Catalog, governed: _Governed
) -> None:
    def with_media(media: MediaIdentityField | None) -> _Governed:
        return replace(
            governed,
            packs=tuple(
                replace(pack, media=media) if pack.contract_id == "vendor-traffic-by-asin" else pack
                for pack in governed.packs
            ),
        )

    with pytest.raises(CatalogError, match=r"vendor-traffic-by-asin: the source pack declares no"):
        derive_source_platforms(with_media(None).request(catalog))
    unmapped = MediaIdentityField("adlytics.media_profile", literal_value="unmapped_profile")
    with pytest.raises(CatalogError, match=r"unmapped_profile has no media taxonomy mapping"):
        derive_source_platforms(with_media(unmapped).request(catalog))
    # Positive control: the committed packs derive.
    derived = derive_source_platforms(governed.request(catalog))
    assert derived.platforms["glance_views"] == {AMAZON_RETAIL}


def _selectable_platforms(
    metric: Metric, catalog: Catalog, governed: _Governed
) -> frozenset[PlatformKey]:
    """Every platform whose pack `PublicationSelector._selected_side` would read for a metric.

    Asks the real selection code one contract at a time: a pack is selectable exactly when
    selection, given only that pack's contract, finds a pack to read.
    """
    selector = PublicationSelector(
        cast("CurrentPublicationInspectionPort", object()),
        governed.packs,
        load_commerce_identity_bridge(ROOT),
    )
    found: set[PlatformKey] = set()
    # A periodic pack serves only one complete declared period (ADR-100), so a periodic metric
    # is asked over one complete ISO week; a day window finds no pack (see the control below).
    window = WEEK if periodic_leaves(catalog, metric) else ONE_DAY
    if {"frequency_bucket", "audience_segment"} & set(metric.valid_grains):
        window = MONTH
    leaves = periodic_leaves(catalog, metric)
    if leaves and all(leaf.period_grains == (TimeGrain.MONTH,) for leaf in leaves):
        # Packs 10-12's totals are monthly and read at marketplace grain (design §6.5).
        window = MONTH
    if "months_since_acquisition" in metric.valid_grains:
        # Packs 14 and 15 serve one cohort vintage: 13 calendar months (ADR-104).
        window = VINTAGE
    for contract in governed.contracts:
        for fact in catalog.facts_read(metric):
            references = leaf_references(catalog, metric)
            measures = tuple(sorted(column for table, column in references if table == fact))
            side = _FactSide(fact, metric.source_datasets, {}, window, measures, metric.label)
            try:
                selector._selected_side(side, {contract.contract_id: contract})
            except ValueError as error:
                assert "no source pack matches" in str(error)
                continue
            for pack in governed.packs:
                if pack.contract_id != contract.contract_id or pack.media is None:
                    continue
                literal = pack.media.literal_value
                found |= {
                    mapping.resolution.platform
                    for mapping in governed.taxonomy.mappings
                    if mapping.resolution.source.namespace == pack.media.namespace
                    and (literal is None or mapping.resolution.source.value == literal)
                }
    return frozenset(found)


@pytest.mark.parametrize("network", ["committed", "with a second network"])
def test_derivation_covers_every_pack_publication_selection_can_read(
    catalog: Catalog, governed: _Governed, network: str
) -> None:
    """The coverage test: if selection's predicate changes, this fails until derivation does."""
    inputs = governed if network == "committed" else _second_network(governed)
    derived = derive_source_platforms(inputs.request(catalog))
    for metric in catalog.metrics:
        if metric.kind is not MetricKind.SIMPLE:
            continue
        assert derived.platforms[metric.name] == _selectable_platforms(metric, catalog, inputs), (
            metric.name
        )


# --- Per-operand accounting in the planner: AC-BR.1b to AC-BR.1d --------------------------------


def _as_on_main(catalog: Catalog) -> Catalog:
    """Take the network rule out of the committed dictionary, as `main` declared it.

    On `main` the ecosystem and platform axes took the `additive` axis default, except on a
    distinct count, where every axis is exact. No metric carried source platforms.
    """
    metrics: list[Metric] = []
    for metric in catalog.metrics:
        policy = metric.aggregation_policy
        if policy is not None:
            network = (
                AxisBehavior.EXACT_ONLY
                if policy.kind is AggregationKind.EXACT_DISTINCT
                else AxisBehavior.ADDITIVE
            )
            policy = AggregationPolicy(
                kind=policy.kind,
                available_axes=policy.available_axes,
                axes={
                    axis: replace(rule, aggregation=network) if axis in NETWORK_AXES else rule
                    for axis, rule in policy.axes.items()
                },
            )
        metrics.append(
            replace(
                metric,
                aggregation_policy=policy,
                ecosystem_aggregation=None,
                source_platforms=frozenset(),
            )
        )
    return Catalog(
        metrics=tuple(metrics),
        deferred=catalog.deferred,
        inventory_categories=catalog.inventory_categories,
    )


def _main_leaf_policies(catalog: Catalog, metric: Metric) -> tuple[AggregationPolicy, ...]:
    if metric.kind is MetricKind.SIMPLE:
        assert metric.aggregation_policy is not None
        return (metric.aggregation_policy,)
    found: list[AggregationPolicy] = []
    for name in (metric.numerator, metric.denominator):
        operand = catalog.metric(name)
        assert operand is not None
        for policy in _main_leaf_policies(catalog, operand):
            if policy not in found:
                found.append(policy)
    return tuple(found)


def _main_validate_semantic_axes(catalog: Catalog, metric: Metric, request: PlanRequest) -> None:
    """Check axes as `_validate_semantic_axes` did on `main`, before ADR-106 (`1a9771f6`).

    Frozen here as the AC-BR.1b oracle: the rule before per-operand accounting, so the test
    compares today's answers with the answers `main` gave. Reopen this test when a second
    network adds a platform to a metric, because then the two are meant to differ.
    """
    groups = frozenset(_GRAIN_AXES[name] for name in request.grains)
    filters = frozenset(
        axis for column in request.filters if (axis := _COLUMN_AXES.get(column)) is not None
    )
    accounted = groups | filters
    if SemanticAxis.CHANNEL in accounted or SemanticAxis.AD_PRODUCT in accounted:
        accounted |= {SemanticAxis.CHANNEL_GROUP, SemanticAxis.PLATFORM, SemanticAxis.ECOSYSTEM}
    elif SemanticAxis.CHANNEL_GROUP in accounted:
        accounted |= {SemanticAxis.PLATFORM, SemanticAxis.ECOSYSTEM}
    elif SemanticAxis.PLATFORM in accounted:
        accounted |= {SemanticAxis.ECOSYSTEM}
    if request.window.days == 1:
        accounted |= {SemanticAxis.TIME}
    policies = _main_leaf_policies(catalog, metric)
    try:
        if metric.kind is MetricKind.RATIO and len(policies) == RATIO_OPERAND_COUNT:
            ratio = RatioOfAggregatesPolicy(policies[0], policies[1])
            ratio.require_capabilities(groups, filters)
            ratio.require_operand_rollup(
                policies[0].available_axes - accounted,
                policies[1].available_axes - accounted,
            )
            return
        for policy in policies:
            policy.require_groupable(groups)
            if metric.kind is MetricKind.RATIO:
                policy.require_alignable(groups)
            policy.require_filterable(filters & policy.available_axes)
            omitted = policy.available_axes - accounted
            if omitted:
                policy.require_rollup(omitted, operation=AggregationOperation.SUM)
    except SemanticCompatibilityError as error:
        raise DomainError(code=GRAIN_NOT_SUPPORTED_CODE, description=str(error)) from None


def _axes_outcome(  # noqa: PLR0913 — the request, and which rule and grain set check it
    catalog: Catalog,
    request: PlanRequest,
    *,
    on_main: bool = False,
    grain_set: GrainSetShape | None = None,
) -> str | None:
    """Return the refusal text, less the offered grains, or None when the request plans."""
    metric = catalog.metric(request.metric_name)
    assert metric is not None
    try:
        if on_main:
            _main_validate_semantic_axes(catalog, metric, request)
        else:
            _validate_semantic_axes(catalog, metric, request, grain_set)
    except DomainError as error:
        return error.description.split(" Available instead: ", 1)[0]
    return None


def _grain_set_read(shape: FactShape, request: PlanRequest) -> GrainSetShape | None:
    """Return the grain set a periodic request reads, chosen as chat chooses it (D-1)."""
    requested = frozenset({*(GRAIN_COLUMNS[name] for name in request.grains), *request.filters})
    name = served_grain_set(shape.grain_sets, requested)
    return next((item for item in shape.grain_sets if item.name == name), None)


def test_every_metric_at_every_grain_set_plans_as_it_did_on_main(  # noqa: PLR0915 -- one branch per fact family
    catalog: Catalog, governed: _Governed
) -> None:
    """AC-BR.1b: the axis check gives the same answer, refusal text included, everywhere.

    Every metric, every non-empty subset of its valid grains, with no filter, a marketplace
    filter, or a marketplace and platform filter, over one day and over a week, against
    `main`'s rule on `main`'s axis behaviour. Page bindings, chat and alerts all plan through
    this check, so this covers every `pages.yaml` binding.
    """
    on_main = _as_on_main(catalog)
    buyers = FactShape.for_fact("fct_new_to_brand_buyers", governed.contracts)
    exposure = FactShape.for_fact("fct_exposure_group_outcome", governed.contracts)
    frequency = FactShape.for_fact("fct_exposure_frequency", governed.contracts)
    segments = FactShape.for_fact("fct_segment_delivery", governed.contracts)
    cells = {"exposure_group", "identity_space"}
    buckets = {"frequency_bucket", "identity_space"}
    filter_sets: tuple[dict[str, str], ...] = (
        {},
        {"marketplace_key": "ATVPDKIKX0DER"},
        {"marketplace_key": "ATVPDKIKX0DER", "platform_key": "amazon_ads"},
    )
    compared = 0
    periodic_compared = 0
    for metric in catalog.metrics:
        if metric.kind is MetricKind.PRODUCT:
            # `plan` refuses a product of operands before this check, on main and here.
            continue
        grains = metric.valid_grains
        for size in range(1, len(grains) + 1):
            for subset in combinations(grains, size):
                for filters in filter_sets:
                    for window in (ONE_DAY, WEEK):
                        request = PlanRequest(metric.name, subset, window, filters)
                        if window.days > 1 and periodic_leaves(catalog, metric):
                            # The one intended difference (ADR-100): `main` had no periodic
                            # metric and summed a week's days; today a periodic metric reads
                            # one exact extracted period, which `require_exact_period` pins
                            # before this check. WEEK is one complete ISO week. The planner
                            # passes the grain set the request reads (D-1); one that leaves the
                            # marketplace open still sums distinct shoppers across marketplaces.
                            marketplace = "marketplace" in subset or "marketplace_key" in filters
                            if "frequency_bucket" in metric.valid_grains:
                                # Packs 6 and 7 (ADR-101, D-3, D-9): a figure reads one bucket
                                # and identity-space cell, per ad product at the channel grain
                                # set, so it plans only with both as grains, the marketplace
                                # pinned and, per channel, the ad product grouped; a rate only
                                # under its account filter. On `main` only an additive sum over
                                # that same cell planned.
                                read = _grain_set_read(frequency, request)
                                now = _axes_outcome(catalog, request, grain_set=read)
                                before = _axes_outcome(on_main, request, on_main=True)
                                per_channel = read is not None and read.name == "channel"
                                plans = (
                                    marketplace
                                    and buckets <= set(subset)
                                    and not metric.requires
                                    and (not per_channel or "ad_product" in subset)
                                )
                                # On `main` a sum over one cell also had to group the
                                # channel and ad product its exact-only axes name (D-9).
                                media = {"channel", "ad_product"} & set(metric.valid_grains)
                                additive = not catalog.is_non_additive(metric) and media <= set(
                                    subset
                                )
                                assert (now is None, before is None) == (
                                    plans,
                                    plans and additive,
                                ), (metric.name, subset, filters, now)
                                periodic_compared += 1
                                continue
                            if "audience_segment" in metric.valid_grains:
                                # Pack 8 (ADR-103, D-4): a figure reads one campaign and
                                # segment row of one month, so it plans only with both as
                                # grains and the marketplace pinned; segment figures never add
                                # up, so no request summing across either plans.
                                now = _axes_outcome(
                                    catalog, request, grain_set=_grain_set_read(segments, request)
                                )
                                before = _axes_outcome(on_main, request, on_main=True)
                                plans = marketplace and {"campaign", "audience_segment"} <= set(
                                    subset
                                )
                                # On `main` a distinct count never planned at all.
                                additive = not catalog.is_non_additive(metric)
                                assert (now is None, before is None) == (
                                    plans,
                                    plans and additive,
                                ), (
                                    metric.name,
                                    subset,
                                    filters,
                                    now,
                                )
                                periodic_compared += 1
                                continue
                            if "exposure_group" in metric.valid_grains:
                                # Pack 5 (ADR-101, D-3): a figure reads one exposure-group and
                                # identity-space cell, so it plans only with both as grains and
                                # the marketplace pinned, and a rate only under its account
                                # filter, which no filter set here carries; a share or index
                                # dividing a delivery's cells never plans. On `main` only an
                                # additive sum over that same cell planned.
                                now = _axes_outcome(
                                    catalog, request, grain_set=_grain_set_read(exposure, request)
                                )
                                before = _axes_outcome(on_main, request, on_main=True)
                                plans = (
                                    marketplace
                                    and cells <= set(subset)
                                    and not metric.requires
                                    and metric.within_delivery is None
                                )
                                additive = not catalog.is_non_additive(metric)
                                assert (now is None, before is None) == (
                                    plans,
                                    plans and additive,
                                ), (metric.name, subset, filters)
                                periodic_compared += 1
                                continue
                            now = _axes_outcome(
                                catalog, request, grain_set=_grain_set_read(buyers, request)
                            )
                            before = _axes_outcome(on_main, request, on_main=True)
                            assert (now is None, before is None) == (marketplace, False), (
                                metric.name,
                                subset,
                                filters,
                            )
                            periodic_compared += 1
                            continue
                        assert _axes_outcome(catalog, request) == _axes_outcome(
                            on_main, request, on_main=True
                        ), (metric.name, subset, filters, window.days)
                        compared += 1
    assert compared > 10_000
    # buyers_new_to_brand_12mo, buyers_attributed and cost_per_new_to_brand_buyer_12mo, each at
    # the 15 non-empty subsets of marketplace, channel, ad product and campaign under the three
    # filter sets (135), and pack 5's 17 metrics, each at the 7 non-empty subsets of
    # marketplace, exposure group and identity space under the three filter sets (357); packs 6
    # and 7's nine metrics valid at marketplace, channel, ad product, frequency bucket and
    # identity space at their 31 subsets (837), and the two at marketplace, bucket and identity
    # space only at their 7 (42), each under the three filter sets; and pack 8's nine metrics at
    # the 7 subsets of marketplace, campaign and audience segment under the three (189); and
    # packs 14 and 15's 15 cohort metrics at the 15 subsets of marketplace, acquisition channel
    # group, cohort and follow-up month under the three (675); and packs 10-12's six segment
    # metrics at the 3 subsets of marketplace and audience segment, and its two totals at
    # marketplace, under the three (60).
    assert periodic_compared == 2295


def test_the_additive_rollup_columns_differ_only_on_columns_no_fact_has(
    catalog: Catalog, governed: _Governed
) -> None:
    """The grain check reads axis rules too; the change touches no fact's grain column."""
    on_main = _as_on_main(catalog)
    network_columns = {GRAIN_COLUMNS["ecosystem"], GRAIN_COLUMNS["platform"]}
    for metric in catalog.metrics:
        now = _additive_rollup_columns(catalog, metric.name)
        before = _additive_rollup_columns(on_main, metric.name)
        assert now ^ before <= network_columns, metric.name
    contracts = list(governed.contracts)
    for fact in {contract.target_table for contract in contracts}:
        if fact.startswith("fct_"):
            grain = set(FactShape.for_fact(fact, contracts).grain_columns)
            assert not grain & network_columns, fact


def _shapes(governed: _Governed, numerator: str, denominator: str) -> FactShapes:
    contracts = list(governed.contracts)
    return FactShapes(
        numerator=FactShape.for_fact(numerator, contracts),
        denominator=FactShape.for_fact(denominator, contracts),
    )


_TACOS = PlanRequest("tacos", ("date", "ecosystem", "marketplace"), WEEK, VENDOR_OPTIONS)


def test_tacos_plans_at_its_only_grain(catalog: Catalog, governed: _Governed) -> None:
    """AC-BR.1c: additive spend over single-platform shipped revenue, `[date, ecosystem, ...]`."""
    built = plan(catalog, _TACOS, _shapes(governed, "fct_ad_exposure", "fct_retail_sales"))
    assert built.metric.name == "tacos"
    assert built.join is not None


def test_a_second_platform_refuses_a_campaign_request_and_names_the_platform_axis(
    catalog: Catalog, governed: _Governed
) -> None:
    """AC-BR.1d. The planner refuses, with GRAIN_NOT_SUPPORTED, naming `platform`."""
    shape = FactShape.for_fact("fct_attributed_conversion", list(governed.contracts))
    request = PlanRequest("attributed_revenue_14d", ("campaign",), WEEK, AMC_ONE_MARKETPLACE)
    # Positive control: today the same request plans.
    assert plan(catalog, request, shape).metric.name == "attributed_revenue_14d"

    two_networks = _with_platforms(
        catalog, derive_source_platforms(_second_network(governed).request(catalog))
    )
    with pytest.raises(DomainError) as refused:
        plan(two_networks, request, shape)
    assert refused.value.code == GRAIN_NOT_SUPPORTED_CODE
    assert refused.value.context["reason"] == "grain_not_supported"
    assert refused.value.description.startswith(
        "sum: platform is exact_only, so it cannot use sum."
    )

    # Paired positive control: grouping by platform answers it inside each network.
    by_platform = PlanRequest(
        "attributed_revenue_14d", ("campaign", "platform"), WEEK, AMC_ONE_MARKETPLACE
    )
    assert plan(two_networks, by_platform, shape).metric.name == "attributed_revenue_14d"


def test_a_second_platform_refuses_roas_across_networks_but_tacos_still_sums_spend(
    catalog: Catalog, governed: _Governed
) -> None:
    """Each operand keeps its own rule: no ratio-level policy (ADR-106 points 3 and 4).

    Synthetic: a second ad platform feeds both spend and attributed revenue.
    """
    fed_twice = {"ad_spend", "attributed_revenue_14d"}
    two_networks = _with_platforms(
        catalog,
        SourcePlatformsResult(
            {
                metric.name: (
                    frozenset({AMAZON_ADS, OTHER_ADS})
                    if metric.name in fed_twice
                    else metric.source_platforms
                )
                for metric in catalog.metrics
                if metric.kind is MetricKind.SIMPLE
            }
        ),
    )
    roas = PlanRequest("roas_attributed_14d", ("campaign",), WEEK, AMC_ONE_MARKETPLACE)
    roas_shapes = _shapes(governed, "fct_attributed_conversion", "fct_ad_exposure")
    assert plan(catalog, roas, roas_shapes).metric.name == "roas_attributed_14d"
    with pytest.raises(DomainError) as refused:
        plan(two_networks, roas, roas_shapes)
    assert refused.value.code == GRAIN_NOT_SUPPORTED_CODE
    assert refused.value.description.startswith(
        "sum: platform is exact_only, so it cannot use sum."
    )

    tacos_shapes = _shapes(governed, "fct_ad_exposure", "fct_retail_sales")
    assert plan(two_networks, _TACOS, tacos_shapes).metric.name == "tacos"
