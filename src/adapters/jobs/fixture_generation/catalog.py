# SPDX-License-Identifier: MIT-0
"""Strict access to fictional identities shared by generated fixtures and prototypes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

import yaml

from adapters.jobs.fixture_generation.shape import (
    CampaignDay,
    CatalogShape,
    Pause,
    ShapeContext,
    ShapeError,
    load_catalog_shape,
)
from adapters.jobs.fixture_generation.values import ShapeValues
from domain.data_contracts.contract import DataContract

CATALOG_PATH = Path("contracts/shared/synthetic-entity-catalog.yaml")
CATALOG_VERSION = 2
"""Version 2 adds the declared `shape` section (ADR-086)."""

FIRST_DAY = date(2025, 7, 1)
"""Where the fictional fixture span starts: a load-time-checked alias of
`shape.fixture_span.first_day`, kept because validation probes import it (design §2.2)."""

BULK_CAMPAIGN_SCENARIOS = frozenset(
    {
        "growth_ramp",
        "sustained_decline",
        "spike_and_fade",
        "zero_conversion",
        "flat_traffic_rising_conversion",
        "coverage_gap",
    }
)
"""The six-scenario vocabulary the bulk tier cycles through, unchanged (design §3.1, R-5)."""
CAMPAIGN_SCENARIOS = BULK_CAMPAIGN_SCENARIOS | {"stable_growth"}
"""Visible campaigns may also carry `stable_growth`, the R-5 reassignment target."""
PRODUCT_SCENARIOS = frozenset({"returns_pressure", "stable_growth"})
SHAPED_CAMPAIGN_SCENARIOS = CAMPAIGN_SCENARIOS - {"zero_conversion"}
"""Campaign scenarios that carry a declared conversion shape (design §3.1 `conversion_shape`)."""
BULK_CAMPAIGN_COUNT = 1_000
COVERAGE_GAP_CONTRACTS = frozenset(
    {
        "amc-dsp-impressions",
        "amc-dsp-clicks",
        "amc-dsp-views",
        "amc-reach-frequency",
        "amc-attributed-conversion",
    }
)
"""The campaign-bearing fact contracts whose rows the declared coverage gap removes.

Every one, conversion included (design §2.2: 0 rows on any contract on the gap day), so the
gap day never serves purchases without the clicks they follow (found by the item 14 probe
test, 2026-10-05).
"""
CAMPAIGN_CHANNEL_PRODUCTS = {
    "amazon_dsp": "amazon_dsp",
    "amazon_sponsored_products": "sponsored_products",
    "amazon_sponsored_brands": "sponsored_brands",
    "amazon_sponsored_display": "sponsored_display",
    "amazon_sponsored_television": "sponsored_television",
}

type CampaignScenario = Literal[
    "growth_ramp",
    "sustained_decline",
    "spike_and_fade",
    "zero_conversion",
    "flat_traffic_rising_conversion",
    "coverage_gap",
    "stable_growth",
]
type ProductScenario = Literal["returns_pressure", "stable_growth"]
type Scenario = CampaignScenario | ProductScenario


class SyntheticCatalogError(ValueError):
    """The governed synthetic entity catalog is malformed or inconsistent."""


type RawMapping = dict[str, object]


def _mapping(value: object, label: str) -> RawMapping:
    if not isinstance(value, dict):
        raise SyntheticCatalogError(f"{label} must be a mapping.")
    return cast("RawMapping", value)


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise SyntheticCatalogError(f"{label} must be a list.")
    return cast("list[object]", value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SyntheticCatalogError(f"{label} must be a non-empty string.")
    return value.strip()


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SyntheticCatalogError(f"{label} must be a positive integer.")
    return value


def _scenario(value: object, allowed: frozenset[str], label: str) -> str:
    scenario = _text(value, label)
    if scenario not in allowed:
        choices = ", ".join(sorted(allowed))
        raise SyntheticCatalogError(f"{label} must be one of: {choices}.")
    return scenario


def _day(value: object, label: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(_text(value, label))
    except ValueError as error:
        raise SyntheticCatalogError(f"{label} must be an ISO date.") from error


def _optional_day(value: object, label: str) -> date | None:
    return None if value is None else _day(value, label)


@dataclass(frozen=True, slots=True)
class SyntheticName:
    """One effective-dated fictional display name."""

    value: str
    valid_from: date
    valid_to: date | None = None

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise SyntheticCatalogError("a synthetic name cannot be blank.")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise SyntheticCatalogError("a synthetic name interval must be increasing.")

    def applies(self, on: date) -> bool:
        """Whether this name is in force on the supplied day."""
        return self.valid_from <= on and (self.valid_to is None or on < self.valid_to)


@dataclass(frozen=True, slots=True)
class SyntheticCampaign:
    """One fictional canonical campaign and its declared measurement scenario."""

    key: str
    advertiser: str
    marketplace: str
    platform: str
    channel: str
    ad_product: str
    scenario: CampaignScenario
    source_aliases: dict[str, str]
    names: tuple[SyntheticName, ...]

    def name_on(self, on: date) -> str | None:
        """Return the one effective name, or no name for the explicit missing-name case."""
        matches = tuple(item.value for item in self.names if item.applies(on))
        if len(matches) > 1:
            raise SyntheticCatalogError(f"{self.key} has overlapping names on {on}.")
        return matches[0] if matches else None


class CampaignFixturePopulation(StrEnum):
    """Closed campaign populations available to explicit fixture callers."""

    VISIBLE = "visible"
    BULK = "bulk"


@dataclass(frozen=True, slots=True)
class SyntheticBulkCampaignScheme:
    """Governed deterministic expansion rule for the opt-in scale population."""

    count: int
    advertiser: str
    marketplace: str
    platform: str
    channel: str
    ad_product: str
    canonical_pattern: str
    source_namespace: str
    source_pattern: str
    name_pattern: str
    valid_from: date
    scenario_cycle: tuple[CampaignScenario, ...]

    def __post_init__(self) -> None:
        if self.count != BULK_CAMPAIGN_COUNT:
            raise SyntheticCatalogError("bulk campaign scheme must expand exactly 1000 campaigns.")
        if frozenset(self.scenario_cycle) != BULK_CAMPAIGN_SCENARIOS or len(
            self.scenario_cycle
        ) != len(BULK_CAMPAIGN_SCENARIOS):
            raise SyntheticCatalogError(
                "bulk campaign scenario cycle must cover each scenario once."
            )
        for label, pattern in (
            ("canonical", self.canonical_pattern),
            ("source", self.source_pattern),
            ("name", self.name_pattern),
        ):
            if pattern.count("{ordinal:04d}") != 1:
                raise SyntheticCatalogError(
                    f"bulk campaign {label} pattern must contain one zero-padded ordinal."
                )

    def expand(self) -> tuple[SyntheticCampaign, ...]:
        """Expand the declaration deterministically without entering the visible catalog."""
        return tuple(
            SyntheticCampaign(
                key=self.canonical_pattern.format(ordinal=ordinal),
                advertiser=self.advertiser,
                marketplace=self.marketplace,
                platform=self.platform,
                channel=self.channel,
                ad_product=self.ad_product,
                scenario=self.scenario_cycle[(ordinal - 1) % len(self.scenario_cycle)],
                source_aliases={self.source_namespace: self.source_pattern.format(ordinal=ordinal)},
                names=(SyntheticName(self.name_pattern.format(ordinal=ordinal), self.valid_from),),
            )
            for ordinal in range(1, self.count + 1)
        )


@dataclass(frozen=True, slots=True)
class SyntheticProduct:
    """One fictional Amazon product with hierarchy and a declared measurement scenario."""

    asin: str
    parent_asin: str
    brand: str
    title: str
    roles: tuple[str, ...]
    scenario: ProductScenario


@dataclass(frozen=True, slots=True)
class SyntheticEntityCatalog:
    """Governed fictional identities and deterministic scenario declarations."""

    marketplace_key: str
    marketplace_label: str
    marketplace_aliases: dict[str, str]
    brand_names: dict[str, tuple[str, str]]
    advertiser_names: dict[str, tuple[str, dict[str, str]]]
    campaigns: tuple[SyntheticCampaign, ...]
    bulk_campaign_scheme: SyntheticBulkCampaignScheme
    products: tuple[SyntheticProduct, ...]
    asin_prefixes: tuple[str, ...]
    dsp_campaign_pattern: str
    sponsored_campaign_pattern: str
    trap_campaign_ids: tuple[str, ...]
    unnamed_campaign_names: tuple[str, ...]
    evidence: dict[str, str]
    shape: CatalogShape
    values: ShapeValues = field(init=False, compare=False, repr=False)
    """The value functions over `shape`, shared by every contract's provider (memoized)."""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "values",
            ShapeValues(self.shape, frozenset(item.key for item in self.campaigns)),
        )
        keys = tuple(item.key for item in self.campaigns)
        if len(set(keys)) != len(keys):
            raise SyntheticCatalogError("synthetic campaign keys must be unique.")
        asins = tuple(item.asin for item in self.products)
        if len(set(asins)) != len(asins):
            raise SyntheticCatalogError("synthetic product ASINs must be unique.")
        if len(set(self.trap_campaign_ids)) != len(self.trap_campaign_ids):
            raise SyntheticCatalogError("synthetic trap campaign identities must be unique.")
        if len(set(self.unnamed_campaign_names)) != len(self.unnamed_campaign_names):
            raise SyntheticCatalogError("synthetic unnamed campaign labels must be unique.")
        aliases: list[str] = []
        for campaign in (*self.campaigns, *self.bulk_campaigns):
            self._validate_campaign(campaign)
            aliases.extend(campaign.source_aliases.values())
        all_keys = tuple(item.key for item in (*self.campaigns, *self.bulk_campaigns))
        if len(set(all_keys)) != len(all_keys):
            raise SyntheticCatalogError("visible and bulk campaign keys must not collide.")
        if len(set(aliases)) != len(aliases):
            raise SyntheticCatalogError("synthetic campaign aliases must be unique.")
        for product in self.products:
            self._validate_product(product)

    def _validate_campaign(self, campaign: SyntheticCampaign) -> None:
        if campaign.advertiser not in self.advertiser_names:
            raise SyntheticCatalogError(f"{campaign.key} names an unknown advertiser.")
        if campaign.marketplace != self.marketplace_key:
            raise SyntheticCatalogError(f"{campaign.key} names an unknown marketplace.")
        if campaign.scenario not in CAMPAIGN_SCENARIOS:
            raise SyntheticCatalogError(
                f"{campaign.key} names unknown scenario {campaign.scenario}."
            )
        try:
            expected_ad_product = CAMPAIGN_CHANNEL_PRODUCTS[campaign.channel]
        except KeyError as error:
            raise SyntheticCatalogError(
                f"{campaign.key} names unknown channel {campaign.channel}."
            ) from error
        if campaign.ad_product != expected_ad_product:
            raise SyntheticCatalogError(
                f"{campaign.key} channel {campaign.channel} requires ad product "
                f"{expected_ad_product}."
            )
        expected_namespace = (
            "amc.dsp.campaign_id_string"
            if campaign.channel == "amazon_dsp"
            else "amc.sponsored.campaign_id_string"
        )
        if set(campaign.source_aliases) != {expected_namespace}:
            raise SyntheticCatalogError(
                f"{campaign.key} must declare only its {expected_namespace} alias."
            )
        value = campaign.source_aliases[expected_namespace]
        pattern = (
            self.dsp_campaign_pattern
            if expected_namespace == "amc.dsp.campaign_id_string"
            else self.sponsored_campaign_pattern
        )
        if re.fullmatch(pattern, value) is None:
            raise SyntheticCatalogError(
                f"{campaign.key} alias {expected_namespace}:{value} violates its lexical invariant."
            )

    def _validate_product(self, product: SyntheticProduct) -> None:
        if product.brand not in self.brand_names:
            raise SyntheticCatalogError(f"{product.asin} names an unknown brand.")
        if product.scenario not in PRODUCT_SCENARIOS:
            raise SyntheticCatalogError(
                f"{product.asin} names unknown scenario {product.scenario}."
            )

    @property
    def bulk_campaigns(self) -> tuple[SyntheticCampaign, ...]:
        """The explicit scale-only population; never part of ``campaigns``."""
        return self.bulk_campaign_scheme.expand()

    @property
    def asins(self) -> frozenset[str]:
        """Every exact fictional child ASIN governed by the catalog."""
        return frozenset(item.asin for item in self.products)


@dataclass(frozen=True, slots=True)
class GovernedValueProvider:
    """Source-qualified fictional values for one contract fixture."""

    contract: DataContract
    catalog: SyntheticEntityCatalog
    population: CampaignFixturePopulation = CampaignFixturePopulation.VISIBLE

    @property
    def values(self) -> ShapeValues:
        """The catalog's shared value functions."""
        return self.catalog.values

    @property
    def campaign_bearing(self) -> bool:
        """Whether the delivered source rows carry campaign identity."""
        return any(
            not mapping.has_no_delivered_value
            and mapping.source_path.rsplit(".", maxsplit=1)[-1].replace("[]", "")
            == "campaign_id_string"
            for mapping in self.contract.fields
        )

    @property
    def eligible_campaigns(self) -> tuple[SyntheticCampaign, ...]:
        """Campaign population governed for this source dataset."""
        candidates = (
            self.catalog.bulk_campaigns
            if self.population is CampaignFixturePopulation.BULK
            else self.catalog.campaigns
        )
        if self.contract.source_dataset.startswith("dsp_"):
            candidates = tuple(item for item in candidates if item.channel == "amazon_dsp")
        elif self.contract.source_dataset == "sponsored_ads_traffic":
            candidates = tuple(item for item in candidates if item.channel != "amazon_dsp")
        if not candidates:
            raise SyntheticCatalogError(
                f"{self.contract.contract_id} has no synthetic campaign for its source context."
            )
        return candidates

    @property
    def product_bearing(self) -> bool:
        """Whether one Vendor source row is at ASIN grain."""
        return self.contract.source_system == "vendor_central" and any(
            "[]" in mapping.source_path and mapping.target_column == "amazon_asin"
            for mapping in self.contract.fields
        )

    @property
    def product_count(self) -> int:
        """How many governed products one logical Vendor day emits."""
        return len(self.catalog.products)

    @property
    def campaign_count(self) -> int:
        """How many campaigns one logical day emits for this contract."""
        return len(self.eligible_campaigns)

    def day_index(self, row: int) -> int:
        """Recover the logical day from a physical entity-expanded row."""
        if self.campaign_bearing:
            return row // self.campaign_count
        if self.product_bearing:
            return row // self.product_count
        return row

    def campaign_index(self, row: int) -> int:
        """Recover the campaign position from a physical row."""
        return row % self.campaign_count

    def product_index(self, row: int) -> int:
        """Recover the product position from a physical Vendor row."""
        return row % self.product_count

    def campaign(self, row: int) -> SyntheticCampaign:
        """Return the campaign selected by the shared day-by-campaign row plan."""
        return self.eligible_campaigns[self.campaign_index(row)]

    def campaign_name(self, row: int) -> str | None:
        """Return the source campaign name, preserving the declared missing-name null."""
        return self.campaign(row).name_on(self.day(row))

    def include_row(self, row: int) -> bool:
        """Apply only explicitly declared absence scenarios to the complete entity grid."""
        if not self.campaign_bearing:
            return True
        campaign = self.campaign(row)
        if self.contract.contract_id == "amc-attributed-conversion" and (
            campaign.scenario == "zero_conversion" or self.zero_delivery(row)
        ):
            # A grouped conversion query emits no row for a campaign-day with no conversions:
            # a zero-conversion campaign, a launch-before day and a pause day. The covered
            # window therefore reduces to the honest EMPTY_COVERED condition.
            return False
        return not (
            self.contract.contract_id in COVERAGE_GAP_CONTRACTS
            and self.values.campaign_day_event("coverage_gap", campaign.key, self.day(row))
        )

    def product(self, row: int) -> SyntheticProduct:
        """Return the product selected deterministically for this physical row."""
        index = self.campaign_index(row) if self.campaign_bearing else self.product_index(row)
        return self.catalog.products[index % len(self.catalog.products)]

    def string_value(self, delivered_name: str, target_column: str, row: int) -> str | None:
        """Return a governed string where the catalog owns that source field."""
        delivered = delivered_name.lower()
        campaign = self.campaign(row)
        value = self._campaign_string(campaign, delivered, row)
        if value is None and (
            delivered == "ad_product_type"
            or target_column in {"ad_product_key", "channel_key", "source_ad_product_type"}
        ):
            value = campaign.ad_product
        elif value is None and target_column == "channel_group_key":
            value = self._channel_group(campaign.ad_product)
        if value is None:
            value = self._product_string(self.product(row), delivered, target_column)
        if value is None and delivered == "campaign_country":
            value = self.catalog.marketplace_aliases["amc.campaign_country"]
        elif value is None and delivered in {
            "marketplace_name",
            "conversion_event_marketplace_name",
        }:
            value = self.catalog.marketplace_aliases["amc.marketplace_name"]
        elif value is None and "marketplace" in target_column:
            value = self.catalog.marketplace_aliases["amazon.marketplace_id"]
        return value

    def _campaign_string(
        self,
        campaign: SyntheticCampaign,
        delivered: str,
        row: int,
    ) -> str | None:
        if delivered == "campaign_id_string":
            return next(iter(campaign.source_aliases.values()))
        if delivered == "campaign":
            return campaign.name_on(self.day(row))
        if delivered == "advertiser":
            return self.catalog.advertiser_names[campaign.advertiser][0]
        if delivered == "advertiser_id":
            aliases = self.catalog.advertiser_names[campaign.advertiser][1]
            namespace = (
                "amc.dsp.advertiser_id"
                if campaign.channel == "amazon_dsp"
                else "amc.sponsored.advertiser_id"
            )
            return aliases[namespace]
        return None

    def _product_string(
        self,
        product: SyntheticProduct,
        delivered: str,
        target_column: str,
    ) -> str | None:
        if delivered.endswith("asin") or target_column.endswith("asin"):
            return product.parent_asin if "parent" in delivered else product.asin
        if delivered == "brand":
            return self.catalog.brand_names[product.brand][1]
        if delivered == "brandcode":
            return self.catalog.brand_names[product.brand][0]
        if delivered == "producttitle":
            return product.title
        return None

    def day(self, row: int) -> date:
        """Return the calendar day of a physical row."""
        return self.catalog.shape.fixture_span.first_day + timedelta(days=self.day_index(row))

    def zero_delivery(self, row: int) -> bool:
        """Whether this campaign row is a launch-before or pause day (every measure 0)."""
        return self.campaign_bearing and not self.values.delivering(
            self.campaign(row).key, self.day(row)
        )

    def numeric_value(self, target_column: str, row: int) -> Decimal | int:
        """Produce one declared value in canonical units (design §3.3)."""
        on = self.day(row)
        value: Decimal | int | None
        if self.contract.source_system != "amc":
            product = self.product(row)
            value = self.values.product_measure(
                self.values.product_day(product.asin, product.scenario, on), target_column
            )
        elif self.campaign_bearing:
            campaign = self.campaign(row)
            value = self.values.campaign_measure(
                self.values.campaign_day(campaign.key, campaign.scenario, on), target_column
            )
        elif self.contract.source_dataset == "amazon_retail_purchases":
            # One row per day, its ASIN chosen by the row plan; a stable-growth entity curve
            # keyed by that ASIN, so the AMC retail purchases move with their own draws.
            value = self.values.campaign_measure(
                self.values.campaign_day(self.product(row).asin, "stable_growth", on),
                target_column,
            )
        else:
            # A portfolio-level AMC result (deduplicated attribution): the day's eligible
            # campaigns summed, so it agrees with the campaign-grain facts.
            value = self._portfolio_measure(target_column, on)
        if value is None:
            raise SyntheticCatalogError(
                f"{self.contract.contract_id}.{target_column} has no declared scenario value."
            )
        return value

    def _portfolio_measure(self, target_column: str, on: date) -> Decimal | int | None:
        total: Decimal | int | None = None
        for campaign in self.eligible_campaigns:
            value = self.values.campaign_measure(
                self.values.campaign_day(campaign.key, campaign.scenario, on), target_column
            )
            if value is None:
                return None
            total = value if total is None else total + value
        return total

    @staticmethod
    def _channel_group(ad_product: str) -> str:
        if ad_product in {"sponsored_products", "sponsored_brands"}:
            return "search"
        if ad_product == "sponsored_television":
            return "video"
        return "display"


def load_synthetic_entity_catalog(root: Path) -> SyntheticEntityCatalog:
    """Load the governed fictional identity catalog with strict narrowing."""
    path = root / CATALOG_PATH
    raw = _mapping(cast("object", yaml.safe_load(path.read_text())), str(path))
    if raw.get("version") != CATALOG_VERSION:
        raise SyntheticCatalogError(f"{path}: version must be {CATALOG_VERSION}.")
    reserved = _mapping(raw.get("reserved_identifiers"), "reserved_identifiers")
    marketplace_key, marketplace_label, marketplace_aliases = _marketplace(raw)
    campaigns = _campaigns(raw)
    bulk = _bulk_campaign_scheme(raw)
    products = _products(raw)
    try:
        shape = load_catalog_shape(
            raw.get("shape"),
            ShapeContext(
                campaign_scenarios={item.key: item.scenario for item in campaigns},
                bulk_campaign_keys=frozenset(item.key for item in bulk.expand()),
                product_scenarios={item.asin: item.scenario for item in products},
                shaped_campaign_scenarios=SHAPED_CAMPAIGN_SCENARIOS,
            ),
        )
    except ShapeError as error:
        raise SyntheticCatalogError(f"{path}: {error}") from error
    _check_names_cover_span(campaigns, bulk, shape.fixture_span.first_day)
    if shape.fixture_span.first_day != FIRST_DAY:
        raise SyntheticCatalogError(
            f"{path}: shape.fixture_span.first_day must equal FIRST_DAY ({FIRST_DAY})."
        )
    _check_event_campaigns_deliver(campaigns, shape)
    return SyntheticEntityCatalog(
        marketplace_key=marketplace_key,
        marketplace_label=marketplace_label,
        marketplace_aliases=marketplace_aliases,
        brand_names=_brands(raw),
        advertiser_names=_advertisers(raw),
        campaigns=campaigns,
        bulk_campaign_scheme=bulk,
        products=products,
        asin_prefixes=tuple(
            _text(item, "ASIN prefix")
            for item in _list(reserved.get("asin_prefixes"), "asin_prefixes")
        ),
        dsp_campaign_pattern=_text(reserved.get("dsp_campaign_pattern"), "DSP campaign pattern"),
        sponsored_campaign_pattern=_text(
            reserved.get("sponsored_campaign_pattern"), "sponsored campaign pattern"
        ),
        trap_campaign_ids=tuple(
            _text(item, "trap campaign identity")
            for item in _list(reserved.get("trap_campaign_ids"), "trap_campaign_ids")
        ),
        unnamed_campaign_names=tuple(
            _text(item, "unnamed campaign label")
            for item in _list(reserved.get("unnamed_campaign_names"), "unnamed_campaign_names")
        ),
        evidence={
            _text(key, "evidence key"): _text(value, "evidence reference")
            for key, value in _mapping(raw.get("evidence"), "evidence").items()
        },
        shape=shape,
    )


def _check_event_campaigns_deliver(
    campaigns: tuple[SyntheticCampaign, ...], shape: CatalogShape
) -> None:
    """Refuse a pause, launch or coverage-gap campaign that is zero-conversion."""
    zero = {item.key for item in campaigns if item.scenario == "zero_conversion"}
    named = {shape.launch.campaign} | {
        event.campaign for event in shape.events if isinstance(event, (Pause, CampaignDay))
    }
    clash = sorted(named & zero)
    if clash:
        raise SyntheticCatalogError(
            f"shape.events: {', '.join(clash)} must not be a zero-conversion campaign."
        )


def _check_names_cover_span(
    campaigns: tuple[SyntheticCampaign, ...], bulk: SyntheticBulkCampaignScheme, first_day: date
) -> None:
    """Every named entity has a name valid on `first_day`; an entity with no names is exempt."""
    for campaign in campaigns:
        if campaign.names and min(item.valid_from for item in campaign.names) > first_day:
            raise SyntheticCatalogError(
                f"{campaign.key}: earliest name valid_from is after fixture_span.first_day."
            )
    if bulk.valid_from > first_day:
        raise SyntheticCatalogError(
            "bulk_campaign_scheme.valid_from is after fixture_span.first_day."
        )


def _marketplace(raw: RawMapping) -> tuple[str, str, dict[str, str]]:
    marketplaces = _list(raw.get("marketplaces"), "marketplaces")
    if len(marketplaces) != 1:
        raise SyntheticCatalogError("the initial synthetic catalog requires one marketplace.")
    marketplace = _mapping(marketplaces[0], "marketplaces[]")
    aliases = {
        _text(key, "marketplace alias namespace"): _text(value, "marketplace alias")
        for key, value in _mapping(marketplace.get("aliases"), "marketplace aliases").items()
    }
    return (
        _text(marketplace.get("key"), "marketplace key"),
        _text(marketplace.get("label"), "marketplace label"),
        aliases,
    )


def _brands(raw: RawMapping) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for item in _list(raw.get("brands"), "brands"):
        value = _mapping(item, "brands[]")
        found[_text(value.get("key"), "brand key")] = (
            _text(value.get("code"), "brand code"),
            _text(value.get("name"), "brand name"),
        )
    return found


def _advertisers(raw: RawMapping) -> dict[str, tuple[str, dict[str, str]]]:
    found: dict[str, tuple[str, dict[str, str]]] = {}
    for item in _list(raw.get("advertisers"), "advertisers"):
        value = _mapping(item, "advertisers[]")
        aliases = _mapping(value.get("source_aliases"), "advertiser source aliases")
        found[_text(value.get("key"), "advertiser key")] = (
            _text(value.get("name"), "advertiser name"),
            {
                _text(key, "advertiser alias namespace"): _text(alias, "advertiser alias")
                for key, alias in aliases.items()
            },
        )
    return found


def _campaigns(raw: RawMapping) -> tuple[SyntheticCampaign, ...]:
    found: list[SyntheticCampaign] = []
    for item in _list(raw.get("campaigns"), "campaigns"):
        value = _mapping(item, "campaigns[]")
        aliases = _mapping(value.get("source_aliases"), "campaign source aliases")
        found.append(
            SyntheticCampaign(
                key=_text(value.get("key"), "campaign key"),
                advertiser=_text(value.get("advertiser"), "campaign advertiser"),
                marketplace=_text(value.get("marketplace"), "campaign marketplace"),
                platform=_text(value.get("platform"), "campaign platform"),
                channel=_text(value.get("channel"), "campaign channel"),
                ad_product=_text(value.get("ad_product"), "campaign ad product"),
                scenario=cast(
                    "CampaignScenario",
                    _scenario(value.get("scenario"), CAMPAIGN_SCENARIOS, "campaign scenario"),
                ),
                source_aliases={
                    _text(key, "campaign alias namespace"): _text(alias, "campaign alias")
                    for key, alias in aliases.items()
                },
                names=tuple(_name(item) for item in _list(value.get("names"), "campaign names")),
            )
        )
    return tuple(found)


def _bulk_campaign_scheme(raw: RawMapping) -> SyntheticBulkCampaignScheme:
    value = _mapping(raw.get("bulk_campaign_scheme"), "bulk_campaign_scheme")
    return SyntheticBulkCampaignScheme(
        count=_positive_integer(value.get("count"), "bulk campaign count"),
        advertiser=_text(value.get("advertiser"), "bulk campaign advertiser"),
        marketplace=_text(value.get("marketplace"), "bulk campaign marketplace"),
        platform=_text(value.get("platform"), "bulk campaign platform"),
        channel=_text(value.get("channel"), "bulk campaign channel"),
        ad_product=_text(value.get("ad_product"), "bulk campaign ad product"),
        canonical_pattern=_text(value.get("canonical_pattern"), "bulk canonical pattern"),
        source_namespace=_text(value.get("source_namespace"), "bulk source namespace"),
        source_pattern=_text(value.get("source_pattern"), "bulk source pattern"),
        name_pattern=_text(value.get("name_pattern"), "bulk name pattern"),
        valid_from=_day(value.get("valid_from"), "bulk valid_from"),
        scenario_cycle=tuple(
            cast(
                "CampaignScenario",
                _scenario(item, BULK_CAMPAIGN_SCENARIOS, "bulk campaign scenario"),
            )
            for item in _list(value.get("scenario_cycle"), "bulk scenario cycle")
        ),
    )


def _name(raw: object) -> SyntheticName:
    value = _mapping(raw, "campaign names[]")
    return SyntheticName(
        value=_text(value.get("value"), "campaign name"),
        valid_from=_day(value.get("valid_from"), "valid_from"),
        valid_to=_optional_day(value.get("valid_to"), "valid_to"),
    )


def _products(raw: RawMapping) -> tuple[SyntheticProduct, ...]:
    return tuple(
        SyntheticProduct(
            asin=_text(value.get("asin"), "product ASIN"),
            parent_asin=_text(value.get("parent_asin"), "parent ASIN"),
            brand=_text(value.get("brand"), "product brand"),
            title=_text(value.get("title"), "product title"),
            roles=tuple(_text(role, "product role") for role in _list(value.get("roles"), "roles")),
            scenario=cast(
                "ProductScenario",
                _scenario(value.get("scenario"), PRODUCT_SCENARIOS, "product scenario"),
            ),
        )
        for value in (
            _mapping(item, "products[]") for item in _list(raw.get("products"), "products")
        )
    )
