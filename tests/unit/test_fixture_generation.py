# SPDX-License-Identifier: MIT-0
"""Generating source-shaped fixtures from the contracts.

The property everything else rests on is that a fixture is shaped like the *source*, not
like the curated table. A fixture already in target shape would skip the reader, which is
the only thing under test.

Two assertions here are correctness properties rather than conveniences. Monetary values
must be in the scaled space, because a fixture holding `12.34` lets a missing divisor pass
unnoticed while one holding `1234000` fails. And no two columns may hold the same value,
because analysis is mostly differences between columns: `total_cost` equal to
`impression_cost` makes the fee zero, and a fixture with no fees cannot show that fees
survive the divisor.
"""

import copy
import dataclasses
import functools
import json
import pathlib
import re
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
import yaml

from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.fixture_generation import values as values_module
from adapters.jobs.fixture_generation.__main__ import render
from adapters.jobs.fixture_generation.catalog import (
    CAMPAIGN_SCENARIOS,
    CATALOG_PATH,
    FIRST_DAY,
    CampaignFixturePopulation,
    SyntheticCampaign,
    SyntheticCatalogError,
    SyntheticEntityCatalog,
    load_synthetic_entity_catalog,
)
from adapters.jobs.fixture_generation.checks import check_ties
from adapters.jobs.fixture_generation.shape import (
    BudgetShift,
    CampaignDay,
    OrderedShippedDivergence,
    Pause,
    PriceShift,
    Promotion,
    ReturnRateShift,
    ShapeError,
    ShapeEvent,
)
from adapters.jobs.fixture_generation.shapes import (
    DEFAULT_ROWS,
    FixtureBuildOptions,
    build,
)
from adapters.jobs.fixture_generation.values import band, unit
from domain.data_contracts.contract import DataContract, FieldMapping, NullMeaning, SourceFormat
from domain.data_contracts.schema import CanonicalType, ColumnRegistry, RegisteredColumn
from domain.shared_kernel.provenance import MeasurementBasis

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-053")]


def _registry(*columns: RegisteredColumn) -> ColumnRegistry:
    return ColumnRegistry(
        columns={(c.table, c.name): c for c in columns},
        known_tables=frozenset(c.table for c in columns),
        inherits={},
    )


EMPTY_REGISTRY = _registry()
ROOT = pathlib.Path(__file__).resolve().parents[2]


@functools.cache
def _committed_contracts() -> tuple[DataContract, ...]:
    """Every contract the repository ships, so each rule covers the complete declaration set."""
    return tuple(contract for _, contract in load_contracts(ROOT))


@functools.cache
def _committed_registry() -> ColumnRegistry:
    return load_registry(ROOT)


def _contract(**overrides: object) -> DataContract:
    """Build an AMC contract with one bigint column, for overriding a field at a time."""
    defaults: dict[str, object] = {
        "contract_id": "test-contract",
        "contract_version": 1,
        "source_system": "amc",
        "source_dataset": "dsp_impressions",
        "source_format": SourceFormat.CSV,
        "target_table": "fct_ad_exposure",
        "grain": ("impression_date",),
        "partition_keys": ("impression_date",),
        "measurement_basis": MeasurementBasis.AD_ATTRIBUTED,
        "source_timezone": "UTC",
        "ecosystem_key": "amazon",
        "platform_key": "amazon_ads",
        "restatement_window_days": 30,
        "staleness_threshold_hours": 72,
        "source_tables": (),
        "fields": (
            FieldMapping(
                source_path="impressions", target_column="impressions", target_type="bigint"
            ),
        ),
    }
    return DataContract(**(defaults | overrides))  # type: ignore[arg-type]


def _vendor(fields: tuple[FieldMapping, ...], **overrides: object) -> DataContract:
    return _contract(
        source_system="vendor_central",
        source_dataset="sales",
        source_format=SourceFormat.JSON_DUAL_ARRAY,
        target_table="fct_retail_sales",
        source_timezone="America/Los_Angeles",
        measurement_basis=MeasurementBasis.RETAIL_ACTUAL,
        fields=fields,
        **overrides,
    )


class TestMonetaryScaling:
    def test_a_scaled_amount_is_generated_as_an_integer_in_the_scaled_space(self) -> None:
        """A fixture holding a currency-space value lets a missing divisor pass.

        `total_cost` arrives as millicents. If the fixture held 11.37 then a reader that
        forgot to divide would produce 11.37 and look right. Holding 1137000 means the
        divisor has to run for any assertion about spend to pass.
        """
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="total_cost",
                    target_column="spend",
                    scale_divisor=100_000,
                    currency_column="currency_code",
                ),
            )
        )
        value = build(contract, EMPTY_REGISTRY).rows[0]["total_cost"]

        assert isinstance(value, int), "a millicent amount must arrive as an integer"
        # The units are the assertion, not the magnitude: applying the divisor has to land
        # on a currency amount at the stored precision, and not applying it has to land
        # somewhere obviously wrong.
        in_currency = Decimal(value) / Decimal(100_000)
        assert in_currency == in_currency.quantize(Decimal("0.0001")), (
            f"{value} millicents does not divide cleanly into a stored decimal"
        )
        assert Decimal("0.01") < in_currency < Decimal("10000"), (
            f"{in_currency} is not a plausible spend once divided"
        )
        assert value > 1_000, f"{value} is small enough to be mistaken for currency space"

    def test_an_unscaled_amount_stays_a_json_number(self) -> None:
        """Vendor money arrives as a JSON number in currency space, never a string."""
        contract = _vendor(
            fields=(
                FieldMapping(
                    source_path="salesByAsin[].shippedRevenue.amount",
                    target_column="ordered_revenue",
                    scale_divisor=1,
                    currency_column="currency_code",
                    target_type="decimal(38,4)",
                ),
            )
        )
        document = build(contract, EMPTY_REGISTRY).document
        assert document is not None
        amount = document["salesByAsin"][0]["shippedRevenue"]["amount"]

        assert isinstance(amount, float)
        assert Decimal(str(amount)) == Decimal(str(amount)).quantize(Decimal("0.0001"))

    def test_two_money_columns_do_not_collide_so_their_difference_is_not_zero(self) -> None:
        """Total minus media-only is the fee. Equal values make every fee zero.

        This is the defect the column offset exists to prevent: a fixture built to show
        that fees survive the divisor would contain no fees at all.
        """
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="total_cost",
                    target_column="spend",
                    scale_divisor=100_000,
                    currency_column="currency_code",
                ),
                FieldMapping(
                    source_path="impression_cost",
                    target_column="spend_media_only",
                    scale_divisor=100_000,
                    currency_column="currency_code",
                ),
            )
        )
        row = build(contract, EMPTY_REGISTRY).rows[0]

        assert row["total_cost"] != row["impression_cost"], "fee would be zero"

    def test_two_money_fields_sharing_a_delivered_name_do_not_collide(self) -> None:
        """`shippedRevenue.amount` and `shippedCogs.amount` are both `amount`.

        Keyed on the delivered name they were equal, making every generated margin zero.
        The canonical column is the unique key, and the contract enforces that.
        """
        contract = _vendor(
            fields=(
                FieldMapping(
                    source_path="salesByAsin[].shippedRevenue.amount",
                    target_column="shipped_revenue",
                    scale_divisor=1,
                    currency_column="currency_code",
                    target_type="decimal(38,4)",
                ),
                FieldMapping(
                    source_path="salesByAsin[].shippedCogs.amount",
                    target_column="shipped_cogs",
                    scale_divisor=1,
                    currency_column="currency_code",
                    target_type="decimal(38,4)",
                ),
            )
        )
        document = build(contract, EMPTY_REGISTRY).document
        assert document is not None
        row = document["salesByAsin"][0]

        assert row["shippedRevenue"]["amount"] != row["shippedCogs"]["amount"], (
            "margin would be zero"
        )


class TestSourceShape:
    def test_a_csv_header_is_the_source_column_name_not_the_canonical_one(self) -> None:
        """The fixture is what Amazon delivers. Amazon does not know our column names."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="impression_dt_utc",
                    target_column="exposure_date",
                    target_type="date",
                ),
            )
        )
        rendered = render(build(contract, EMPTY_REGISTRY))

        assert rendered.splitlines()[0] == "impression_dt_utc"
        assert "exposure_date" not in rendered

    def test_a_derived_column_is_delivered_under_its_canonical_name(self) -> None:
        """A derived value has no source column, so the extraction has to alias it.

        `COUNT(DISTINCT user_id)` is not a column name. The convention is that the SQL
        aliases it to the canonical name, and this is what pins that convention down.
        """
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="COUNT(DISTINCT user_id)",
                    target_column="reach_users",
                    derived_from=("user_id",),
                    target_type="bigint",
                    non_additive=True,
                ),
            )
        )
        assert render(build(contract, EMPTY_REGISTRY)).splitlines()[0] == "reach_users"

    def test_a_literal_never_appears_in_a_fixture(self) -> None:
        """A literal is stamped during mapping. Emitting it would assert the source sent it."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="impressions", target_column="impressions", target_type="bigint"
                ),
                FieldMapping(
                    target_column="channel_key", literal_value="dsp", target_type="string"
                ),
            )
        )
        rendered = render(build(contract, EMPTY_REGISTRY))

        assert "channel_key" not in rendered
        assert "impressions" in rendered

    def test_a_path_that_is_a_prefix_of_another_is_not_written_as_a_scalar(self) -> None:
        """`reportSpecification.reportOptions` becomes a hash of the object.

        The reader hashes what its siblings materialise. Writing a scalar there would
        overwrite them and produce a document no source could emit.
        """
        contract = _vendor(
            fields=(
                FieldMapping(
                    source_path="reportSpecification.reportOptions.reportPeriod",
                    target_column="report_period",
                    target_type="string",
                ),
                FieldMapping(
                    source_path="reportSpecification.reportOptions",
                    target_column="report_options_hash",
                    target_type="string",
                ),
            )
        )
        document = build(contract, EMPTY_REGISTRY).document
        assert document is not None
        options = document["reportSpecification"]["reportOptions"]

        assert isinstance(options, dict), "the object was overwritten by a scalar"
        assert options["reportPeriod"] == "DAY"

    def test_an_array_path_repeats_per_row_and_a_document_path_appears_once(self) -> None:
        """The two grains in one vendor document are why a source path is a path."""
        contract = _vendor(
            fields=(
                FieldMapping(
                    source_path="salesByAsin[].asin",
                    target_column="product_key",
                    target_type="string",
                ),
                FieldMapping(
                    source_path="reportSpecification.reportType",
                    target_column="report_type",
                    target_type="string",
                ),
            )
        )
        document = build(contract, EMPTY_REGISTRY).document
        assert document is not None

        assert len(document["salesByAsin"]) == DEFAULT_ROWS
        assert isinstance(document["reportSpecification"]["reportType"], str)

    def test_a_manifest_path_goes_to_the_manifest_and_not_the_document(self) -> None:
        """The manifest is a sibling file. A load with no manifest validates nothing."""
        contract = _vendor(
            fields=(
                FieldMapping(
                    source_path="manifest.retrieved_at",
                    target_column="retrieved_at",
                    target_type="timestamp",
                ),
                FieldMapping(
                    source_path="salesByAsin[].asin",
                    target_column="product_key",
                    target_type="string",
                ),
            )
        )
        fixture = build(contract, EMPTY_REGISTRY)
        assert fixture.document is not None

        assert "manifest" not in fixture.document
        assert fixture.manifest["retrieved_at"]


class TestTheLandingDeclaration:
    """What accompanies a fixture is a declaration, not a manifest.

    It carries only what a producer knows before writing anything. Everything measured from the
    bytes — the option-set hash, the delivered columns, the objects and their checksums — is
    computed at landing time by `adapters/jobs/curation/manifests.build_manifest`. A committed
    checksum rots the moment a fixture is regenerated, and committed columns would make the
    header assertion vacuous.
    """

    def test_it_carries_every_key_the_manifest_builder_requires(self) -> None:
        """The pairing that matters: the generator writes what the landing job reads.

        These drifted before. The generated shape carried `contract_id`, `contract_version` and
        `row_count`, none of which landing has a use for, and none of `marketplace`,
        `period_start`, `period_end` or `source_watermark`, all of which are required — so no
        generated fixture could be landed at all.
        """
        required = {
            "source_system",
            "source_dataset",
            "source_scope",
            "marketplace",
            "option_set",
            "period_start",
            "period_end",
            "source_watermark",
            "retrieved_at",
        }
        for contract in _committed_contracts():
            declaration = build(contract, _committed_registry()).manifest
            assert required <= set(declaration), contract.contract_id

    def test_it_never_names_the_contract(self) -> None:
        """ADR-030 keys a delivery on its dataset, never on a contract.

        One dataset can feed two contracts, so a declaration naming one would be wrong about
        the other.
        """
        for contract in _committed_contracts():
            declaration = build(contract, _committed_registry()).manifest
            assert "contract_id" not in declaration, contract.contract_id

    def test_the_period_brackets_the_rows_it_describes(self) -> None:
        """The rows must fall inside the window their description claims.

        A declaration claiming otherwise is a document no source would emit.
        """
        for contract in _committed_contracts():
            declaration = build(contract, _committed_registry()).manifest
            assert declaration["period_start"] <= declaration["period_end"]
            if contract.contract_id == "vendor-product-hierarchy":
                assert declaration["source_watermark"] is None
            else:
                assert declaration["source_watermark"] >= declaration["period_end"]

    def test_amc_declares_no_option_set(self) -> None:
        """Empty is the honest answer rather than a gap.

        No AMC contract carries `report_options_hash` in its partition keys, so nothing the
        producer chose participates in the identity of an AMC delivery. Inventing a key to fill
        the field would be fabricating provenance.
        """
        for contract in _committed_contracts():
            if contract.source_system != "amc":
                continue
            assert build(contract, _committed_registry()).manifest["option_set"] == {}

    def test_a_vendor_option_set_comes_from_the_document_it_describes(self) -> None:
        """The options come from the document, because they are part of dataset identity.

        The same report type under two distributor views is two datasets, which is why
        `report_options_hash` is in those contracts' partition keys.
        """
        for contract in _committed_contracts():
            if contract.source_system != "vendor_central":
                continue
            fixture = build(contract, _committed_registry())
            if fixture.document is None:
                continue
            expected = fixture.document["reportSpecification"]["reportOptions"]
            assert fixture.manifest["option_set"] == expected


class TestClosedValueDomains:
    def test_generated_strings_use_only_each_contracts_declared_domain(self) -> None:
        for contract in _committed_contracts():
            fixture = build(contract, _committed_registry())
            for mapping in contract.fields:
                if not mapping.value_domain or not mapping.source_path:
                    continue
                delivered = mapping.source_path.rsplit(".", 1)[-1].rstrip("[]")
                values = {row[delivered] for row in fixture.rows if delivered in row}
                assert values <= set(mapping.value_domain), (
                    f"{contract.contract_id}.{mapping.target_column} generated {values} "
                    f"outside {mapping.value_domain}"
                )

    def test_deduplicated_channel_groups_match_their_ad_products(self) -> None:
        contract = next(
            item
            for item in _committed_contracts()
            if item.contract_id == "amc-deduplicated-temporal-attribution"
        )
        rows = build(contract, _committed_registry()).rows

        for row in rows:
            expected = (
                "search"
                if row["ad_product_type"] in {"sponsored_products", "sponsored_brands"}
                else "video"
                if row["ad_product_type"] == "sponsored_television"
                else "display"
            )
            assert row["channel_group_key"] == expected


class TestOneDeliveryCoversOneMarketplace:
    """A manifest declares a single `marketplace`, so its rows must not span two.

    Varying it per row broke a drill-across before it ran: the two sides of a ratio join on
    marketplace, so alternating values meant half the joined rows matched nothing. It also made
    every curated partition hold two populations under one provenance record.
    """

    def test_every_generated_row_shares_one_marketplace(self) -> None:
        for contract in _committed_contracts():
            fixture = build(contract, _committed_registry())
            columns = [
                mapping.source_path.rsplit(".", 1)[-1].rstrip("[]")
                for mapping in contract.fields
                if mapping.target_column == "marketplace_key" and mapping.source_path
            ]
            found = {row[column] for row in fixture.rows for column in columns if column in row}
            assert len(found) <= 1, f"{contract.contract_id} spans {found}"

    def test_the_declared_marketplace_is_rendered_in_each_source_namespace(self) -> None:
        aliases = {"campaign_country": "US", "marketplace_name": "AMAZON.COM"}
        for contract in _committed_contracts():
            fixture = build(contract, _committed_registry())
            for mapping in contract.fields:
                if mapping.target_column != "marketplace_key" or not mapping.source_path:
                    continue
                column = mapping.source_path.rsplit(".", 1)[-1].rstrip("[]")
                expected = aliases.get(column, fixture.manifest["marketplace"])
                for row in fixture.rows:
                    if column in row:
                        assert row[column] == expected


class TestAnalysabilityOfGeneratedRows:
    def test_several_children_share_a_parent_asin_so_a_rollup_has_something_to_roll_up(
        self,
    ) -> None:
        """Pairing every ASIN with itself makes a hierarchy fixture untestable."""
        contract = _contract(
            source_format=SourceFormat.JSONL,
            fields=(
                FieldMapping(source_path="asin", target_column="asin", target_type="string"),
                FieldMapping(
                    source_path="parentAsin", target_column="parent_asin", target_type="string"
                ),
            ),
        )
        rows = build(contract, EMPTY_REGISTRY).rows

        parents = {row["parentAsin"] for row in rows}
        assert len(parents) > 1, "one parent for every row exercises no grouping"
        assert len(parents) < len(rows), "a parent per child exercises no roll-up"
        assert all(row["asin"] != row["parentAsin"] for row in rows)

    def test_a_report_period_covers_the_rows_it_contains(self) -> None:
        """A one-day period over six days of rows is a document no source would emit."""
        contract = _vendor(
            fields=(
                FieldMapping(
                    source_path="reportSpecification.dataStartTime",
                    target_column="period_start",
                    target_type="date",
                ),
                FieldMapping(
                    source_path="reportSpecification.dataEndTime",
                    target_column="period_end",
                    target_type="date",
                ),
                FieldMapping(
                    source_path="salesByAsin[].startDate",
                    target_column="sales_date",
                    target_type="date",
                ),
            )
        )
        document = build(contract, EMPTY_REGISTRY).document
        assert document is not None
        specification = document["reportSpecification"]
        row_dates = [row["startDate"] for row in document["salesByAsin"]]

        assert specification["dataStartTime"] == min(row_dates)
        assert specification["dataEndTime"] == max(row_dates)

    def test_a_boolean_column_takes_both_values(self) -> None:
        """A constant boolean exercises one branch of whatever reads it."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="rows_suppressed",
                    target_column="rows_suppressed",
                    target_type="boolean",
                ),
            )
        )
        rendered = render(build(contract, EMPTY_REGISTRY))

        assert "true" in rendered
        assert "false" in rendered
        assert "True" not in rendered, "a delivered CSV carries lowercase booleans"


class TestDeterminism:
    def test_building_the_same_contract_twice_gives_the_same_bytes(self) -> None:
        """Fixtures are committed. A per-process value would diff on every run."""
        contract = _contract()
        assert render(build(contract, EMPTY_REGISTRY)) == render(build(contract, EMPTY_REGISTRY))

    def test_a_jsonl_fixture_is_one_record_per_line(self) -> None:
        contract = _contract(
            source_format=SourceFormat.JSONL,
            fields=(FieldMapping(source_path="asin", target_column="asin", target_type="string"),),
        )
        lines = render(build(contract, EMPTY_REGISTRY)).splitlines()

        assert len(lines) == DEFAULT_ROWS
        assert all(json.loads(line)["asin"] for line in lines)


class TestFailsLoudly:
    def test_a_column_with_no_resolvable_type_names_itself(self) -> None:
        """An AMC column absent from the registry has no declared type either.

        `target_schema` raises for the same reason: a partial fixture is worse than none.
        """
        contract = _contract(
            source_tables=("dsp_impressions",),
            fields=(FieldMapping(source_path="not_in_registry", target_column="mystery_column"),),
        )
        with pytest.raises(Exception, match="mystery_column"):
            build(contract, EMPTY_REGISTRY)

    def test_a_type_from_the_registry_needs_no_declaration_on_the_contract(self) -> None:
        """The registry is where an AMC type lives. Re-declaring it would let it drift."""
        contract = _contract(
            source_tables=("dsp_impressions",),
            fields=(FieldMapping(source_path="impressions", target_column="impressions"),),
        )
        registry = _registry(
            RegisteredColumn(
                name="impressions", table="dsp_impressions", threshold="LOW", source_type="LONG"
            )
        )
        value = build(contract, registry).rows[0]["impressions"]

        assert isinstance(value, int), "resolved from the registry's LONG"


class TestSuppressionColumnsReachTheFixture:
    def test_an_extraction_option_column_is_delivered_under_its_canonical_name(self) -> None:
        """AMC synthesises these into the output, and we choose the names.

        They cannot resolve against the registry, so a fixture is the only place the
        reader's handling of them can be exercised.
        """
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="filteredReasonColumn",
                    target_column="suppression_reason",
                    from_extraction_option=True,
                    target_type="string",
                    nullable=True,
                    null_meaning=NullMeaning.SUPPRESSED,
                ),
            )
        )
        assert render(build(contract, EMPTY_REGISTRY)).splitlines()[0] == "suppression_reason"


class TestEveryTypeIsCovered:
    @pytest.mark.parametrize("canonical", list(CanonicalType))
    def test_every_canonical_type_has_a_value_generator(self, canonical: CanonicalType) -> None:
        """Adding a type to the vocabulary without a generator must fail here.

        `_value_for` raises rather than defaulting to a string, for the same reason
        AMC_TYPE_MAP has no fallback — but that guard only helps if something notices. This
        is what notices, and it grows automatically with the enum.
        """
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="a_column",
                    target_column="a_column",
                    target_type=canonical.value,
                ),
            )
        )
        row = build(contract, EMPTY_REGISTRY).rows[0]

        assert row["a_column"] is not None


def test_fixture_declarations_use_fictional_source_owned_scopes() -> None:
    expected = {
        "amc": {"namespace": "amc.entity", "value": "fictional-fixture-amc-entity"},
        "vendor_central": {
            "namespace": "vendor_central.vendor_group",
            "value": "fictional-fixture-vendor-group",
        },
        "vendor_data_kiosk": {
            "namespace": "vendor_data_kiosk.vendor_group",
            "value": "fictional-fixture-vendor-group",
        },
    }
    for contract in _committed_contracts():
        declaration = build(contract, _committed_registry()).manifest
        assert declaration["source_scope"] == expected[contract.source_system]


def test_vendor_fixture_uses_governed_reporting_lens_defaults() -> None:
    """Generated Vendor documents must be landable under the same defaults serving queries use."""
    contracts = {contract.contract_id: contract for contract in _committed_contracts()}
    document = build(contracts["vendor-sales-by-asin"], _committed_registry()).document

    assert document is not None
    options = document["reportSpecification"]["reportOptions"]
    assert options["distributorView"] == "MANUFACTURING"
    assert options["sellingProgram"] == "RETAIL"
    assert options["reportPeriod"] == "DAY"


@functools.cache
def _scenario_fixture(contract_id: str):  # type: ignore[no-untyped-def]
    contract = next(item for item in _committed_contracts() if item.contract_id == contract_id)
    catalog = load_synthetic_entity_catalog(ROOT)
    return build(
        contract,
        _committed_registry(),
        FixtureBuildOptions(rows=catalog.shape.fixture_span.span_days, entity_catalog=catalog),
    )


def _evented_campaigns(catalog: SyntheticEntityCatalog) -> set[str]:
    """Every visible campaign key a declared event or the launch names."""
    shape = catalog.shape
    keys = {shape.launch.campaign}
    for event in shape.events:
        if isinstance(event, Promotion):
            keys |= set(event.campaigns)
        elif isinstance(event, BudgetShift):
            keys |= {event.down, event.up}
        elif isinstance(event, (Pause, CampaignDay)):
            keys.add(event.campaign)
    return keys


def _identified_rows(contract_id: str) -> tuple[dict[str, object], ...]:
    """Return the fixture rows, each identified by its campaign."""
    return tuple(
        row for row in _scenario_fixture(contract_id).rows if row["campaign_id_string"] is not None
    )


def _span_days() -> int:
    return load_synthetic_entity_catalog(ROOT).shape.fixture_span.span_days


def _coverage_gap() -> tuple[str, str]:
    """Return the declared coverage-gap campaign source id and ISO date (design §3.1)."""
    catalog = load_synthetic_entity_catalog(ROOT)
    event = next(
        item
        for item in catalog.shape.events
        if isinstance(item, CampaignDay) and item.kind == "coverage_gap"
    )
    campaign = next(item for item in catalog.campaigns if item.key == event.campaign)
    return next(iter(campaign.source_aliases.values())), event.on.isoformat()


def _not_delivering(day: str) -> set[str]:
    """Source ids of the visible campaigns with no delivery on `day` (launch-before, pause)."""
    catalog = load_synthetic_entity_catalog(ROOT)
    on = date.fromisoformat(day)
    return {
        next(iter(campaign.source_aliases.values()))
        for campaign in catalog.campaigns
        if not catalog.values.delivering(campaign.key, on)
    }


@pytest.mark.covers("ADR-051")
class TestDeclaredCampaignScenarios:
    def test_every_campaign_bearing_output_contains_each_eligible_campaign_every_day(
        self,
    ) -> None:
        catalog = load_synthetic_entity_catalog(ROOT)
        dsp_ids = {
            next(iter(campaign.source_aliases.values()))
            for campaign in catalog.campaigns
            if campaign.channel == "amazon_dsp"
        }
        sponsored_ids = {
            next(iter(campaign.source_aliases.values()))
            for campaign in catalog.campaigns
            if campaign.channel != "amazon_dsp"
        }
        contracts = {
            "amc-campaign-dsp": ("source_observed_on", dsp_ids),
            "amc-campaign-sponsored": ("source_observed_on", sponsored_ids),
            "amc-dsp-impressions": ("impression_date_utc", dsp_ids),
            "amc-dsp-clicks": ("impression_date_utc", dsp_ids),
            "amc-dsp-views": ("event_date_utc", dsp_ids),
            "amc-reach-frequency": ("event_date_utc", sponsored_ids),
        }

        # The gap is the declared coverage_gap event's campaign and date (was day index 33 of
        # the scenario `coverage_gap`); zero-delivery days keep their rows (design §3.3).
        gap_id, gap_day = _coverage_gap()
        gap_ids = {gap_id}
        for contract_id, (date_column, expected_ids) in contracts.items():
            rows = _identified_rows(contract_id)
            ids_by_day = {
                day: {row["campaign_id_string"] for row in rows if row[date_column] == day}
                for day in {row[date_column] for row in rows}
            }
            assert len(ids_by_day) == _span_days(), contract_id
            for day, ids in ids_by_day.items():
                expected = (
                    expected_ids - gap_ids
                    if contract_id
                    in {
                        "amc-dsp-impressions",
                        "amc-dsp-clicks",
                        "amc-dsp-views",
                        "amc-reach-frequency",
                    }
                    and day == gap_day
                    else expected_ids
                )
                assert ids == expected, (contract_id, day)

    def test_attributed_conversion_uses_all_governed_exposure_identities_daily(self) -> None:
        catalog = load_synthetic_entity_catalog(ROOT)
        governed_ids = {
            next(iter(campaign.source_aliases.values())) for campaign in catalog.campaigns
        }
        exposure_ids = {
            row["campaign_id_string"]
            for contract_id in ("amc-dsp-impressions", "amc-reach-frequency")
            for row in _identified_rows(contract_id)
        }
        conversion_rows = _scenario_fixture("amc-attributed-conversion").rows
        conversion_ids = {row["campaign_id_string"] for row in conversion_rows}
        days = {row["conversion_event_date_utc"] for row in conversion_rows}

        zero_ids = {
            next(iter(campaign.source_aliases.values()))
            for campaign in catalog.campaigns
            if campaign.scenario == "zero_conversion"
        }
        assert exposure_ids == governed_ids
        assert conversion_ids == governed_ids - zero_ids
        assert len(days) == _span_days()
        gap_id, gap_day = _coverage_gap()
        for day in days:
            # A grouped conversion query emits no row on a zero-delivery day (design §3.3),
            # and the coverage gap removes the campaign's rows on every contract (§2.2).
            assert {
                row["campaign_id_string"]
                for row in conversion_rows
                if row["conversion_event_date_utc"] == day
            } == governed_ids - zero_ids - _not_delivering(day) - (
                {gap_id} if day == gap_day else set()
            )

    def test_clicks_never_exceed_impressions_at_campaign_day_grain(self) -> None:
        impression_rows = _identified_rows("amc-dsp-impressions")
        click_rows = _scenario_fixture("amc-dsp-clicks").rows
        impressions = {
            (row["impression_date_utc"], row["campaign_id_string"]): row["impressions"]
            for row in impression_rows
        }
        clicks = {
            (row["impression_date_utc"], row["campaign_id_string"]): row["clicks"]
            for row in click_rows
        }

        assert clicks.keys() == impressions.keys()
        assert all(clicks[grain] <= impressions[grain] for grain in clicks)
        for row in _scenario_fixture("amc-reach-frequency").rows:
            assert row["clicks"] <= row["impressions"]

    def test_zero_conversion_campaigns_have_zero_conversion_measures(self) -> None:
        catalog = load_synthetic_entity_catalog(ROOT)
        zero_ids = {
            next(iter(campaign.source_aliases.values()))
            for campaign in catalog.campaigns
            if campaign.scenario == "zero_conversion"
        }
        rows = tuple(
            row
            for row in _scenario_fixture("amc-attributed-conversion").rows
            if row["campaign_id_string"] in zero_ids
        )
        assert rows == ()

    def test_named_scenarios_have_their_declared_deterministic_trends(self) -> None:
        # Design §3.3: each scenario's story curve, compared over 7-day totals at the story
        # window's first day and at the shape anchor (the old day-0 versus day-36 single-row
        # comparison read the linear shape; the noise and weekday factors make single days
        # unsuited). Campaigns an event names are excluded from the traffic direction.
        catalog = load_synthetic_entity_catalog(ROOT)
        shape = catalog.shape
        story_start = shape.shape_anchor - timedelta(days=shape.story_days - 1)
        exposure_rows = (
            *_scenario_fixture("amc-dsp-impressions").rows,
            *_scenario_fixture("amc-reach-frequency").rows,
        )
        conversion_rows = _scenario_fixture("amc-attributed-conversion").rows
        evented = _evented_campaigns(catalog)
        gap_id, _gap_day = _coverage_gap()
        observed_scenarios: set[str] = set()

        def week(series: dict[str, int], end: date) -> int:
            return sum(series.get((end - timedelta(days=i)).isoformat(), 0) for i in range(7))

        for campaign in catalog.campaigns:
            campaign_id = next(iter(campaign.source_aliases.values()))
            traffic = {
                row.get("impression_date_utc") or row["event_date_utc"]: row["impressions"]
                for row in exposure_rows
                if row["campaign_id_string"] == campaign_id
            }
            purchases = {
                row["conversion_event_date_utc"]: row["purchases"]
                for row in conversion_rows
                if row["campaign_id_string"] == campaign_id
            }
            observed_scenarios.add(campaign.scenario)
            expected_traffic_days = _span_days() - (1 if campaign_id == gap_id else 0)
            assert len(traffic) == expected_traffic_days
            delivering = [
                value
                for day, value in traffic.items()
                if catalog.values.delivering(campaign.key, date.fromisoformat(day))
            ]
            assert all(value > 0 for value in delivering)
            if campaign.scenario == "zero_conversion":
                assert purchases == {}
                continue
            delivering_days = sum(
                catalog.values.delivering(campaign.key, FIRST_DAY + timedelta(days=offset))
                for offset in range(_span_days())
            )
            # The coverage gap removes the campaign's rows on every contract (design §2.2).
            assert len(purchases) == delivering_days - (1 if campaign_id == gap_id else 0)
            if campaign.key in evented:
                continue
            start, anchor = (
                week(traffic, story_start + timedelta(days=6)),
                week(traffic, shape.shape_anchor),
            )
            if campaign.scenario in {"growth_ramp", "stable_growth", "coverage_gap"}:
                assert anchor > start, campaign.key
            elif campaign.scenario == "sustained_decline":
                assert anchor < start, campaign.key
            elif campaign.scenario == "spike_and_fade":
                centre = catalog.values.spike_centre(campaign.key)
                peak = week(traffic, centre + timedelta(days=3))
                assert peak > start and peak > anchor, campaign.key
            else:
                assert campaign.scenario == "flat_traffic_rising_conversion"
                rate_start = week(purchases, story_start + timedelta(days=6)) / max(1, start)
                rate_anchor = week(purchases, shape.shape_anchor) / max(1, anchor)
                assert rate_anchor > rate_start, campaign.key

        assert observed_scenarios == CAMPAIGN_SCENARIOS

    def test_ad_product_and_channel_group_follow_the_selected_campaign(self) -> None:
        catalog = load_synthetic_entity_catalog(ROOT)
        expected = {
            next(iter(campaign.source_aliases.values())): campaign.ad_product
            for campaign in catalog.campaigns
        }
        for row in _scenario_fixture("amc-attributed-conversion").rows:
            ad_product = expected[row["campaign_id_string"]]
            channel_group = (
                "search"
                if ad_product in {"sponsored_products", "sponsored_brands"}
                else "video"
                if ad_product == "sponsored_television"
                else "display"
            )
            assert row["ad_product_type"] == ad_product
            assert row["channel_group_key"] == channel_group

    def test_manifest_span_remains_logical_days_after_campaign_expansion(self) -> None:
        fixture = _scenario_fixture("amc-attributed-conversion")
        start = date.fromisoformat(fixture.manifest["period_start"])
        end = date.fromisoformat(fixture.manifest["period_end"])

        catalog = load_synthetic_entity_catalog(ROOT)
        assert (end - start).days + 1 == _span_days()
        assert start == FIRST_DAY
        # One row per delivering campaign-day of every campaign that converts (design §3.3),
        # less the coverage-gap day, which no contract carries (§2.2).
        delivering_rows = -1 + sum(
            catalog.values.delivering(campaign.key, start + timedelta(days=offset))
            for campaign in catalog.campaigns
            if campaign.scenario != "zero_conversion"
            for offset in range(_span_days())
        )
        assert len(fixture.rows) == delivering_rows
        # The pinned 2026-09-22T12:00:00Z became a pure function of the span (design §2.2).
        assert (
            fixture.manifest["retrieved_at"] == f"{catalog.shape.fixture_span.last_day}T00:00:00Z"
        )

    def test_generated_vendor_sales_preserve_returns_and_margin_constraints(self) -> None:
        document = _scenario_fixture("vendor-sales-by-asin").document
        assert document is not None

        for row in document["salesByAsin"]:
            assert row["customerReturns"] <= row["shippedUnits"]
            assert Decimal(row["shippedCogs"]["amount"]) < Decimal(row["shippedRevenue"]["amount"])


@pytest.mark.covers("ADR-051")
def test_declared_missing_campaign_name_reaches_the_source_delivery_as_null() -> None:
    catalog = load_synthetic_entity_catalog(ROOT)
    missing = next(campaign for campaign in catalog.campaigns if not campaign.names)
    missing_id = next(iter(missing.source_aliases.values()))
    rows = tuple(
        row
        for row in _scenario_fixture("amc-campaign-sponsored").rows
        if row["campaign_id_string"] == missing_id
    )
    assert len(rows) == _span_days()
    assert {row["campaign"] for row in rows} == {None}


@pytest.mark.covers("ADR-051")
def test_generated_vendor_documents_carry_every_required_source_sibling() -> None:
    sales = json.loads((ROOT / "tests/fixtures/generated/vendor-sales-by-asin.json").read_text())
    traffic = json.loads(
        (ROOT / "tests/fixtures/generated/vendor-traffic-by-asin.json").read_text()
    )
    for array_name in ("salesByAsin", "salesAggregate"):
        for row in sales[array_name]:
            assert row["endDate"] == row["startDate"]
            for amount_name in ("shippedRevenue", "shippedCogs", "orderedRevenue"):
                assert isinstance(row[amount_name]["amount"], (int, float))
                assert row[amount_name]["currencyCode"] == "USD"
    for array_name in ("trafficByAsin", "trafficAggregate"):
        assert all(row["endDate"] == row["startDate"] for row in traffic[array_name])


@pytest.mark.covers("ADR-051")
def test_declared_absence_scenarios_are_narrow_and_keep_campaign_identity_paired() -> None:
    catalog = load_synthetic_entity_catalog(ROOT)
    gap_id, gap_day = _coverage_gap()
    reach = _scenario_fixture("amc-reach-frequency").rows
    assert not any(
        row["campaign_id_string"] == gap_id and row["event_date_utc"] == gap_day for row in reach
    )
    assert all(
        any(row["campaign_id_string"] == gap_id and row["event_date_utc"] == day for row in reach)
        for day in {
            (FIRST_DAY + timedelta(days=offset)).isoformat() for offset in range(_span_days())
        }
        - {gap_day}
    )
    zero_ids = {
        next(iter(campaign.source_aliases.values()))
        for campaign in catalog.campaigns
        if campaign.scenario == "zero_conversion"
    }
    conversions = _scenario_fixture("amc-attributed-conversion").rows
    assert zero_ids.isdisjoint({row["campaign_id_string"] for row in conversions})


@pytest.mark.covers("ADR-071")
def test_bulk_campaign_scheme_expands_exactly_and_never_changes_the_visible_catalog() -> None:
    catalog = load_synthetic_entity_catalog(ROOT)
    first = catalog.bulk_campaigns
    second = catalog.bulk_campaigns

    assert len(catalog.campaigns) == 60
    assert len(first) == 1_000
    assert first == second
    assert first[0].key == "campaign:bulk-estate-0001"
    assert first[-1].key == "campaign:bulk-estate-1000"
    assert first[0].names[0].value == "Fictional Bulk Estate | Campaign 0001"
    assert first[-1].names[0].value == "Fictional Bulk Estate | Campaign 1000"
    assert next(iter(first[0].source_aliases.values())) == "920000000000000001"
    assert next(iter(first[-1].source_aliases.values())) == "920000000000001000"
    assert len({campaign.key for campaign in first}) == 1_000
    assert len({next(iter(campaign.source_aliases.values())) for campaign in first}) == 1_000
    assert {campaign.key for campaign in first}.isdisjoint(
        campaign.key for campaign in catalog.campaigns
    )
    assert {next(iter(campaign.source_aliases.values())) for campaign in first}.isdisjoint(
        next(iter(campaign.source_aliases.values())) for campaign in catalog.campaigns
    )
    assert tuple(campaign.scenario for campaign in first[:6]) == tuple(
        catalog.bulk_campaign_scheme.scenario_cycle
    )
    assert all(
        campaign.scenario == catalog.bulk_campaign_scheme.scenario_cycle[index % 6]
        for index, campaign in enumerate(first)
    )


@pytest.mark.covers("ADR-071")
def test_bulk_fixture_path_is_explicit_deterministic_and_provider_owned() -> None:
    catalog = load_synthetic_entity_catalog(ROOT)
    contract = next(
        item for item in _committed_contracts() if item.contract_id == "amc-dsp-impressions"
    )
    options = FixtureBuildOptions(
        rows=1,
        entity_catalog=catalog,
        campaign_population=CampaignFixturePopulation.BULK,
    )

    first = build(contract, _committed_registry(), options)
    second = build(contract, _committed_registry(), options)

    assert first == second
    assert len(first.rows) == 1_000
    assert {row["campaign_id_string"] for row in first.rows} == {
        next(iter(campaign.source_aliases.values())) for campaign in catalog.bulk_campaigns
    }
    assert all(row["impressions"] > 0 for row in first.rows)
    assert first.manifest["period_start"] == FIRST_DAY.isoformat()
    assert first.manifest["period_end"] == FIRST_DAY.isoformat()


# --- The declared shape (catalog v2, design §3.1): one loader test per validation rule ---------

type _Raw = dict[str, object]


def _committed_raw() -> _Raw:
    return yaml.safe_load((ROOT / CATALOG_PATH).read_text())


def _shape(raw: _Raw) -> dict[str, object]:
    return raw["shape"]  # type: ignore[return-value]


def _event_of(raw: _Raw, kind: str) -> dict[str, object]:
    events = _shape(raw)["events"]
    return next(item for item in events if item["kind"] == kind)  # type: ignore[union-attr,index]


def _load_mutated(tmp_path: pathlib.Path, mutate: Callable[[_Raw], None]) -> None:
    raw = copy.deepcopy(_committed_raw())
    mutate(raw)
    target = tmp_path / CATALOG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(raw, sort_keys=False))
    load_synthetic_entity_catalog(tmp_path)


def _set(path: str, value: object) -> Callable[[_Raw], None]:
    def mutate(raw: _Raw) -> None:
        node: object = _shape(raw)
        *parents, leaf = path.split(".")
        for part in parents:
            node = node[int(part)] if isinstance(node, list) else node[part]  # type: ignore[index]
        if isinstance(node, list):
            node[int(leaf)] = value
        else:
            node[leaf] = value  # type: ignore[index]

    return mutate


def _set_event(kind: str, field: str, value: object) -> Callable[[_Raw], None]:
    def mutate(raw: _Raw) -> None:
        _event_of(raw, kind)[field] = value

    return mutate


def _drop(path: str) -> Callable[[_Raw], None]:
    def mutate(raw: _Raw) -> None:
        node: object = _shape(raw)
        *parents, leaf = path.split(".")
        for part in parents:
            node = node[part]  # type: ignore[index]
        del node[leaf]  # type: ignore[attr-defined]

    return mutate


def _many(*steps: Callable[[_Raw], None]) -> Callable[[_Raw], None]:
    def mutate(raw: _Raw) -> None:
        for step in steps:
            step(raw)

    return mutate


def _version(raw: _Raw) -> None:
    raw["version"] = 1


def _late_name(raw: _Raw) -> None:
    campaigns = raw["campaigns"]
    campaigns[4]["names"][0]["valid_from"] = date(2025, 7, 2)  # type: ignore[index]


def _late_bulk(raw: _Raw) -> None:
    raw["bulk_campaign_scheme"]["valid_from"] = date(2025, 7, 2)  # type: ignore[index]


def _event_date(raw: _Raw) -> None:
    event = _event_of(raw, "pause")
    event["on"] = "2026-10-03"


def _duplicate_event_campaign(raw: _Raw) -> None:
    _event_of(raw, "coverage_gap")["campaign"] = "campaign:northstar-dsp-16"


def _price_product_reused(raw: _Raw) -> None:
    _event_of(raw, "return_rate_shift")["product"] = "B0FIXTUR06"


_LOADER_RULES: tuple[tuple[str, Callable[[_Raw], None], str], ...] = (
    ("version", _version, "version must be 2"),
    ("missing field", _drop("story_days"), "shape.(root): missing story_days"),
    ("unknown field", _set("extra", 1), "shape.(root): unknown key extra"),
    (
        "decimal not quoted",
        _set("min_trend_multiplier", 0.1),
        "min_trend_multiplier: must be a quoted decimal",
    ),
    (
        "event campaign unknown",
        _set_event("pause", "campaign", "campaign:not-in-catalog"),
        "is not a visible catalog campaign",
    ),
    (
        "bulk campaign refused",
        _set_event("pause", "campaign", "campaign:bulk-estate-0001"),
        "is a bulk campaign",
    ),
    (
        "event product unknown",
        _set_event("price_shift", "product", "B0NOTREAL1"),
        "is not a catalog product",
    ),
    (
        "offset above 0",
        _set_event("pause", "last_offset", 1),
        "must be an integer offset in [-13, 0]",
    ),
    (
        "offset below -13",
        _set_event("pause", "first_offset", -14),
        "must be an integer offset in [-13, 0]",
    ),
    (
        "first after last",
        _many(_set_event("pause", "first_offset", -1), _set_event("pause", "last_offset", -2)),
        "first offset must not be after the last offset",
    ),
    ("literal event date", _event_date, "events take offsets only, not a literal date (on)"),
    (
        "launch offset",
        _set("launch.first_delivery_offset", -20),
        "launch.first_delivery_offset: must be an integer offset",
    ),
    (
        "story start too early",
        _set("shape_anchor", "2025-08-01"),
        "anchors must satisfy first_day + 56",
    ),
    ("event before shape anchor", _set("event_anchor", "2026-10-04"), "anchors must satisfy"),
    ("last day beyond slack", _set("fixture_span.last_day", "2026-10-09"), "anchors must satisfy"),
    (
        "slack out of range",
        _set("event_anchor_slack_days", 3),
        "event_anchor_slack_days: must be an integer in [0, 2]",
    ),
    (
        "maturity keys",
        _set("amc.attribution_maturity", {"age_0": "1"}),
        "amc.attribution_maturity: missing age_1",
    ),
    (
        "maturity above 1",
        _set("amc.attribution_maturity.age_1", "1.2"),
        "age_1: must lie in (0, 1]",
    ),
    (
        "maturity order",
        _many(
            _set("amc.attribution_maturity.age_0", "0.95"),
            _set("amc.attribution_maturity.age_1", "0.9"),
        ),
        "age_0 must not exceed age_1",
    ),
    (
        "name after first_day",
        _late_name,
        "earliest name valid_from is after fixture_span.first_day",
    ),
    (
        "bulk name after first_day",
        _late_bulk,
        "bulk_campaign_scheme.valid_from is after fixture_span.first_day",
    ),
    (
        "price pct zero",
        _set_event("price_shift", "pct", "0"),
        "must be a non-zero integer string in [-20, 20]",
    ),
    (
        "price pct out of range",
        _set_event("price_shift", "pct", "-25"),
        "must be a non-zero integer string in [-20, 20]",
    ),
    (
        "price product reused",
        _price_product_reused,
        "the price_shift product B0FIXTUR06 is named by another event",
    ),
    (
        "long-run factor",
        _set("campaign_spread.annual_growth_pct", ["-15", "200"]),
        "the long-run factor must stay above min_trend_multiplier",
    ),
    (
        "conversion shape keys",
        _drop("conversion_shape.coverage_gap"),
        "conversion_shape: missing coverage_gap",
    ),
    (
        "conversion shape range",
        _set("conversion_shape.growth_ramp", ["1.00", "2.10"]),
        "conversion_shape.growth_ramp[1]: must lie in (0, 2]",
    ),
    ("story days", _set("story_days", 57), "story_days: must be at most 56"),
    ("traffic base", _set("traffic_base", 0), "traffic_base: must be a positive integer"),
    (
        "month factor count",
        _set("month_factors", ["1.00"] * 11),
        "month_factors: must have exactly 12 entries",
    ),
    (
        "month factor range",
        _set("month_factors.0", "1.60"),
        "month_factors[0]: must lie in (0.5, 1.5)",
    ),
    (
        "month amplitude",
        _set("month_amplitude", ["0.6", "2.4"]),
        "month_amplitude[1]: must lie in (0, 2)",
    ),
    (
        "growth floor",
        _set("campaign_spread.annual_growth_pct", ["-60", "35"]),
        "annual_growth_pct[0]: must be above -50",
    ),
    (
        "return rate end",
        _set_event("return_rate_shift", "return_rate_end", "0.60"),
        "return_rate_end: must lie in (0, 0.5)",
    ),
    (
        "return rate product",
        _set_event("return_rate_shift", "product", "B0FIXTUR03"),
        "must not be the returns-pressure product",
    ),
    ("cvr max", _set("campaign_checks.daily_cvr_max", "1.5"), "daily_cvr_max: must lie in (0, 1]"),
    (
        "check band floor",
        _set("campaign_checks.window_cvr_band", ["-0.1", "0.25"]),
        "window_cvr_band[0]: must be at least 0",
    ),
    (
        "funnel by construction",
        _set("campaign_spread.conversion_rate_pct", ["7", "19"]),
        "must stay below daily_cvr_max",
    ),
    (
        "budget shift same",
        _set_event("budget_shift", "up", "campaign:northstar-dsp-07"),
        "budget_shift down and up must be different campaigns",
    ),
    ("event campaigns distinct", _duplicate_event_campaign, "must be pairwise distinct"),
    (
        "lag shares",
        _set("retail.returns_lag_profile.0.share", "0.25"),
        "shares must sum to exactly 1",
    ),
    ("lag distinct", _set("retail.returns_lag_profile.1.lag_days", 3), "lags must be distinct"),
    ("lag bound", _set("retail.returns_lag_profile.3.lag_days", 29), "must be at most 28"),
    (
        "band order",
        _set("retail.cogs_ratio", ["0.72", "0.55"]),
        "retail.cogs_ratio: low must be below high",
    ),
    (
        "weekday count",
        _set("weekday_factors", ["1.00"] * 6),
        "weekday_factors: must have exactly 7 entries",
    ),
    (
        "weekday range",
        _set("weekday_factors.6", "0.40"),
        "weekday_factors[6]: must lie in (0.5, 1.5)",
    ),
    (
        "weekday amplitude",
        _set("weekday_amplitude", ["0", "1.5"]),
        "weekday_amplitude[0]: must lie in (0, 2)",
    ),
    (
        "slope floor",
        _set("campaign_spread.slope_pct", ["-100", "55"]),
        "slope_pct[0]: must be above -100",
    ),
    (
        "share range",
        _set("campaign_spread.viewable_share", ["0.70", "1.20"]),
        "viewable_share[1]: must lie in (0, 1]",
    ),
    (
        "first report share",
        _set("retail.first_report_returns_share", "0"),
        "first_report_returns_share: must lie in (0, 1]",
    ),
    (
        "frequency floor",
        _set("campaign_spread.frequency", ["0.9", "2.6"]),
        "frequency[0]: must be at least 1",
    ),
    (
        "per product scenario",
        _set("retail.return_rate", {"stable_growth": "0.06"}),
        "retail.return_rate: missing returns_pressure",
    ),
    (
        "unrestated value",
        _set("retail.unrestated_returns", "partial"),
        "must be first_report or final",
    ),
    ("seal generation", _set("seal_generation", -1), "seal_generation: must be an integer >= 0"),
)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [pytest.param(mutate, message, id=name) for name, mutate, message in _LOADER_RULES],
)
def test_the_shape_loader_refuses_each_rule_and_names_the_path(
    tmp_path: pathlib.Path, mutate: Callable[[_Raw], None], message: str
) -> None:
    with pytest.raises(SyntheticCatalogError, match=re.escape(message)):
        _load_mutated(tmp_path, mutate)


def test_the_committed_shape_loads_with_resolved_event_dates(tmp_path: pathlib.Path) -> None:
    # Positive control for every rule above: the unmodified catalog loads through the same path.
    _load_mutated(tmp_path, lambda _raw: None)
    shape = load_synthetic_entity_catalog(ROOT).shape
    anchor = shape.event_anchor
    assert shape.shape_anchor <= anchor <= shape.fixture_span.last_day
    assert shape.fixture_span.first_day == date(2025, 7, 1)
    assert shape.launch.first_delivery == shape.shape_anchor - timedelta(days=10)
    price = next(event for event in shape.events if isinstance(event, PriceShift))
    assert (price.first, price.pct) == (anchor - timedelta(days=8), -10)
    returns = next(event for event in shape.events if isinstance(event, ReturnRateShift))
    assert (returns.first, returns.ramp_end) == (anchor - timedelta(days=10), anchor)
    assert shape.retail.unrestated_returns == "final"
    assert (shape.attribution_maturity.age_0, shape.attribution_maturity.age_1) == (1, 1)
    assert shape.seal_generation == 2  # re-landed twice on 2026-10-06 (ADR-086 §14)


def test_every_named_visible_campaign_has_a_name_on_first_day() -> None:
    catalog = load_synthetic_entity_catalog(ROOT)
    first_day = catalog.shape.fixture_span.first_day
    named = [campaign for campaign in catalog.campaigns if campaign.names]
    assert named
    assert all(campaign.name_on(first_day) for campaign in named)
    assert [c.key for c in catalog.campaigns if not c.names] == ["campaign:missing-name-sponsored"]


# --- Design §3.7 generator tests over the declared shape (ADR-086) ---------------------------

_SHAPE_DAYS_CACHE: dict[str, list[date]] = {}


def _catalog() -> SyntheticEntityCatalog:
    return load_synthetic_entity_catalog(ROOT)


def _span_list() -> list[date]:
    if "span" not in _SHAPE_DAYS_CACHE:
        span = _catalog().shape.fixture_span
        _SHAPE_DAYS_CACHE["span"] = [
            span.first_day + timedelta(days=offset) for offset in range(span.span_days)
        ]
    return _SHAPE_DAYS_CACHE["span"]


def _quiet_campaigns() -> list[SyntheticCampaign]:
    """Visible campaigns no event names, not spike_and_fade and not zero series."""
    catalog = _catalog()
    evented = _evented_campaigns(catalog)
    return [
        campaign
        for campaign in catalog.campaigns
        if campaign.key not in evented and campaign.scenario != "spike_and_fade"
    ]


@pytest.mark.covers("ADR-086")
def test_1_the_weekday_factor_is_exact_per_entity_and_observed_across_campaigns() -> None:
    catalog = _catalog()
    shape = catalog.shape
    values = catalog.values
    days = _span_list()
    for entity in [c.key for c in catalog.campaigns] + [p.asin for p in catalog.products]:
        amplitude = shape.weekday_amplitude.low + (
            shape.weekday_amplitude.high - shape.weekday_amplitude.low
        ) * unit(shape.seed, entity, "weekday_amplitude")
        for day in days[:14]:
            assert values.weekday_factor(entity, day) == 1 + amplitude * (
                shape.weekday_factors[day.weekday()] - 1
            )
    mean_factor = sum(shape.weekday_factors, Decimal(0)) / 7
    ratios: dict[int, list[Decimal]] = {weekday: [] for weekday in range(7)}
    sunday_over_saturday: set[Decimal] = set()
    for campaign in _quiet_campaigns():
        series = [values.campaign_day(campaign.key, campaign.scenario, day).traffic for day in days]
        for index in range(3, len(series) - 3):
            average = Decimal(sum(series[index - 3 : index + 4])) / 7
            if average:
                ratios[days[index].weekday()].append(series[index] / average)
        saturday = next(i for i, day in enumerate(days) if day.weekday() == 5)
        sunday_over_saturday.add(Decimal(series[saturday + 1]) / series[saturday])
    for weekday in (1, 2, 5, 6):  # Tuesday, Wednesday, Saturday, Sunday
        observed = sum(ratios[weekday]) / len(ratios[weekday]) - 1
        expected = shape.weekday_factors[weekday] - mean_factor
        assert (observed > 0) == (expected > 0), weekday
    assert len(sunday_over_saturday) > 1


@pytest.mark.covers("ADR-086")
def test_1b_monthly_seasonality_has_no_boundary_step_and_year_over_year_shows_growth() -> None:
    catalog = _catalog()
    shape = catalog.shape
    values = catalog.values
    days = _span_list()
    for entity in ("campaign:northstar-dsp", "B0FIXTUR03"):
        factors = [values.month_factor(entity, day) for day in days]
        for index in range(2, len(days)):
            assert abs(factors[index] / factors[index - 1] - 1) <= Decimal("0.02")
            if days[index - 1].day != 15:
                second = factors[index] - 2 * factors[index - 1] + factors[index - 2]
                assert abs(second) <= Decimal("1e-20"), days[index]
    quiet = [c for c in _quiet_campaigns() if c.key != shape.launch.campaign]
    monthly: dict[tuple[int, int], list[int]] = {}
    for campaign in quiet:
        for day in days:
            monthly.setdefault((day.year, day.month), []).append(
                values.campaign_day(campaign.key, campaign.scenario, day).traffic
            )
    full = {
        key: sum(items) / len(items)
        for key, items in monthly.items()
        if len(items) >= 28 * len(quiet)
    }
    # The 12 full calendar months that end before the story window (August 2025 to July
    # 2026), so the scenario curves are flat and the long-run term is centred on the year.
    year = [(2025, month) for month in range(8, 13)] + [(2026, month) for month in range(1, 8)]
    annual = sum(full[key] for key in year) / 12
    mean_month = sum(shape.month_factors, Decimal(0)) / 12
    for month in (7, 11, 12, 1, 2):
        key = next(item for item in year if item[1] == month)
        assert (full[key] > annual) == (shape.month_factors[month - 1] > mean_month), key
    end = shape.shape_anchor - timedelta(days=56)
    for campaign in quiet:
        growth = band(
            shape.seed,
            shape.campaign_spread.annual_growth_pct.low,
            shape.campaign_spread.annual_growth_pct.high,
            campaign.key,
            "campaign_spread.annual_growth_pct",
        )
        if abs(growth) < 10:
            continue

        def total(last: date, key: str = campaign.key, scenario: str = campaign.scenario) -> int:
            return sum(
                values.campaign_day(key, scenario, last - timedelta(days=i)).traffic
                for i in range(28)
            )

        assert (total(end) > total(end - timedelta(days=364))) == (growth > 0), campaign.key


@pytest.mark.covers("ADR-086")
def test_2_events_move_their_campaigns_and_products_on_their_dates() -> None:
    catalog = _catalog()
    shape = catalog.shape
    values = catalog.values
    scenario = {c.key: c.scenario for c in catalog.campaigns}

    def traffic(key: str, day: date) -> int:
        return values.campaign_day(key, scenario[key], day).traffic

    for event in shape.events:
        if isinstance(event, Promotion):
            for key in event.campaigns:
                for day in (event.first, event.last):
                    neighbours = [
                        traffic(key, day + timedelta(days=d)) for d in (-6, -5, -4, 4, 5, 6)
                    ]
                    assert traffic(key, day) > max(neighbours), (key, day)
        elif isinstance(event, BudgetShift):
            before = event.on - timedelta(days=7)
            assert values.event_factor(event.down, event.on) < values.event_factor(
                event.down, before
            )
            assert values.event_factor(event.up, event.on) > values.event_factor(event.up, before)
        elif isinstance(event, Pause):
            for offset in range((event.last - event.first).days + 1):
                assert traffic(event.campaign, event.first + timedelta(days=offset)) == 0
            assert traffic(event.campaign, event.last + timedelta(days=1)) > 0
    launch = shape.launch
    assert traffic(launch.campaign, launch.first_delivery - timedelta(days=1)) == 0
    assert traffic(launch.campaign, launch.first_delivery) > 0
    gap_id, gap_day = _coverage_gap()
    reach = _scenario_fixture("amc-reach-frequency").rows
    absent = [
        r for r in reach if r["campaign_id_string"] == gap_id and r["event_date_utc"] == gap_day
    ]
    assert absent == []
    for event in shape.events:
        if isinstance(event, ReturnRateShift):
            for later in (1, 2):
                day = shape.event_anchor + timedelta(days=later)
                assert (
                    values.return_rate(event.product, "stable_growth", day) == event.return_rate_end
                )


@pytest.mark.covers("ADR-086")
@pytest.mark.parametrize("slack", [0, 1, 2])
def test_2b_the_zero_delivery_serve_shapes_exist_in_their_windows(slack: int) -> None:
    catalog = _catalog()
    shape = catalog.shape
    values = catalog.values
    last_day = shape.event_anchor + timedelta(days=slack)
    launch = shape.launch
    scenario = {c.key: c.scenario for c in catalog.campaigns}
    # Shape 4: the launch campaign's 14D prior is all launch-before days.
    prior = [last_day - timedelta(days=14 + i) for i in range(14)]
    assert all(not values.delivering(launch.campaign, day) for day in prior)
    if slack == 0:
        pause = next(event for event in shape.events if isinstance(event, Pause))

        def clicks(day: date) -> int:
            return values.campaign_day(pause.campaign, scenario[pause.campaign], day).clicks

        anchor = shape.event_anchor
        # Shape 1: 1D at offset 0 has a zero prior (-1) and a non-zero current.
        assert clicks(anchor) > 0 and clicks(anchor - timedelta(days=1)) == 0
        # Shape 2: 1D at -1 is zero in both.
        assert clicks(anchor - timedelta(days=1)) == clicks(anchor - timedelta(days=2)) == 0
        # Shape 3: 1D at -2 has a zero current and a non-zero prior (-3).
        assert clicks(anchor - timedelta(days=2)) == 0 and clicks(anchor - timedelta(days=3)) > 0


@pytest.mark.covers("ADR-086")
@pytest.mark.parametrize("slack", [0, 1, 2])
def test_3_no_tie_artifacts_on_the_served_windows(slack: int) -> None:
    catalog = _catalog()
    check_ties(catalog, catalog.shape.event_anchor + timedelta(days=slack))


def _served_rows(
    width: int,
) -> list[
    tuple[
        SyntheticCampaign,
        tuple[int, int, int, Decimal, Decimal],
        tuple[int, int, int, Decimal, Decimal],
    ]
]:
    """Per-campaign window totals the lake serves, summed by the shared fixture helper."""
    from fixture_artifacts_support import window_total  # noqa: PLC0415

    from domain.shared_kernel.values import DateRange  # noqa: PLC0415

    catalog = _catalog()
    last_day = catalog.shape.fixture_span.last_day
    current = DateRange(last_day - timedelta(days=width - 1), last_day)
    prior = DateRange(current.start - timedelta(days=width), current.start - timedelta(days=1))

    def totals(campaign: str, window: DateRange) -> tuple[int, int, int, Decimal, Decimal]:
        return (
            int(window_total("fct_ad_exposure", "impressions", campaign, window)),
            int(window_total("fct_ad_exposure", "clicks", campaign, window)),
            int(window_total("fct_attributed_conversion", "purchases", campaign, window)),
            window_total("fct_ad_exposure", "spend", campaign, window),
            window_total("fct_attributed_conversion", "revenue_attributed", campaign, window),
        )

    return [
        (campaign, totals(campaign.key, current), totals(campaign.key, prior))
        for campaign in catalog.campaigns
    ]


@pytest.mark.covers("ADR-086")
@pytest.mark.parametrize("width", [1, 7, 14])
def test_3_served_value_variant_has_no_tie_artifacts(width: int) -> None:
    """Design §3.7 test 3, served-value variant (R5-6): the same gate over helper totals."""
    from adapters.jobs.fixture_generation import checks  # noqa: PLC0415

    rows = _served_rows(width)
    assert any(current[3] for _, current, _prior in rows)
    checks._gate_ties(rows, width)  # pyright: ignore[reportPrivateUsage]
    # Positive control: three campaigns given one campaign's totals are refused.
    donor = next(row for row in rows if row[1][3] != row[2][3])
    tied = [(campaign, donor[1], donor[2]) for campaign, _, _ in rows[:3]] + rows[3:]
    with pytest.raises(SyntheticCatalogError, match="3 or more campaigns share"):
        checks._gate_ties(tied, width)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.covers("ADR-086")
def test_4_returns_follow_the_declared_lag_profile_and_never_exceed_shipments() -> None:
    catalog = _catalog()
    shape = catalog.shape
    values = catalog.values
    for product in catalog.products:
        for day in _span_list()[::17]:
            item = values.product_day(product.asin, product.scenario, day)
            lagged = sum(
                lag.share
                * values.units_shipped(
                    product.asin, product.scenario, day - timedelta(days=lag.lag_days)
                )
                for lag in shape.retail.returns_lag_profile
            )
            expected = int(
                (
                    values.return_rate(product.asin, product.scenario, day) * lagged
                ).to_integral_value(rounding="ROUND_HALF_EVEN")
            )
            assert item.returns == expected
            assert item.returns < item.units_shipped


@pytest.mark.covers("ADR-086")
def test_5_the_divergence_product_ratio_ramps_inside_its_stretch() -> None:
    catalog = _catalog()
    values = catalog.values
    event = next(
        item for item in catalog.shape.events if isinstance(item, OrderedShippedDivergence)
    )
    product = event.products[0]
    inside = {
        values.order_ratio(product, event.first + timedelta(days=i))
        for i in range((event.ramp_end - event.first).days + 1)
    }
    outside = values.order_ratio(product, event.first - timedelta(days=30))
    assert len(inside) > 2
    assert values.order_ratio(product, event.ramp_end) == event.ordered_ratio_end
    assert (
        values.order_ratio(product, event.ramp_end + timedelta(days=2)) == event.ordered_ratio_end
    )
    assert outside != event.ordered_ratio_end


@pytest.mark.covers("ADR-086")
def test_6_net_and_served_revenue_per_unit_stay_in_their_bands() -> None:
    catalog = _catalog()
    retail = catalog.shape.retail
    values = catalog.values
    days = _span_list()
    for product in catalog.products:
        for day in days:
            item = values.product_day(product.asin, product.scenario, day)
            net = item.revenue_per_unit / values.price(product.asin, day)
            assert retail.revenue_per_unit.low * Decimal("0.99") <= net
            assert net <= retail.revenue_per_unit.high * Decimal("1.01")
            served = item.shipped_revenue / item.units_shipped
            assert retail.served_rpu_band.low <= served <= retail.served_rpu_band.high
    shift = next(item for item in catalog.shape.events if isinstance(item, PriceShift))
    product = next(p for p in catalog.products if p.asin == shift.product)

    def mean_rpu(start: date) -> Decimal:
        week = (
            values.product_day(
                product.asin, product.scenario, start + timedelta(days=i)
            ).revenue_per_unit
            for i in range(7)
        )
        return sum(week, Decimal(0)) / 7

    ratio = mean_rpu(shift.first) / mean_rpu(shift.first - timedelta(days=7))
    assert abs(ratio - (1 + Decimal(shift.pct) / 100)) <= Decimal("0.02")


@pytest.mark.covers("ADR-086")
def test_7_row_invariants_hold_on_every_generated_row() -> None:
    catalog = _catalog()
    for row in _scenario_fixture("amc-dsp-views").rows:
        assert row["viewable_impressions"] <= row["measurable_impressions"]
        assert row["viewable_impressions"] <= row["unmeasurable_viewable_impressions"]
        assert row["unmeasurable_viewable_impressions"] <= row["measurable_impressions"]
    for row in _scenario_fixture("amc-reach-frequency").rows:
        assert row["clicks"] <= row["impressions"]
        assert row["reach_users"] <= row["impressions"]
        assert row["distinct_user_count"] <= row["reach_users"]
    document = _scenario_fixture("vendor-sales-by-asin").document
    assert document is not None
    for row in document["salesByAsin"]:
        assert Decimal(str(row["shippedCogs"]["amount"])) < Decimal(
            str(row["shippedRevenue"]["amount"])
        )
    pause = next(event for event in catalog.shape.events if isinstance(event, Pause))
    paused = next(c for c in catalog.campaigns if c.key == pause.campaign)
    paused_id = next(iter(paused.source_aliases.values()))
    pause_day = pause.first.isoformat()
    exposure = [
        row
        for row in _scenario_fixture("amc-dsp-impressions").rows
        if row["campaign_id_string"] == paused_id and row["impression_date_utc"] == pause_day
    ]
    assert len(exposure) == 1 and exposure[0]["impressions"] == 0 and exposure[0]["total_cost"] == 0
    conversions = [
        row
        for row in _scenario_fixture("amc-attributed-conversion").rows
        if row["campaign_id_string"] == paused_id and row["conversion_event_date_utc"] == pause_day
    ]
    assert conversions == []


@pytest.mark.covers("ADR-086")
def test_7a_ctr_viewable_rate_frequency_and_retail_conversion_vary() -> None:
    catalog = _catalog()
    values = catalog.values
    days = _span_list()[-60:]
    for campaign in catalog.campaigns:
        series = [values.campaign_day(campaign.key, campaign.scenario, day) for day in days]
        live = [item for item in series if item.traffic]
        assert len({Decimal(item.clicks) / item.traffic for item in live}) > 1, campaign.key
        assert len({Decimal(item.viewable) / item.measurable for item in live}) > 1
        assert len({Decimal(item.traffic) / item.reach for item in live}) > 1
    for product in catalog.products:
        rows = [values.product_day(product.asin, product.scenario, day) for day in days]
        assert len({Decimal(item.units_ordered) / item.glance_views for item in rows}) > 1


# Exact values for the committed shape, seed `adlytics-dev-shape-2026-10-04-r1`, anchors
# 2026-10-06. Pinned context: decimal prec 28, ROUND_HALF_EVEN quantize to 0.0001 and round to
# integer, ROUND_FLOOR for floor (design §3.2). Derivations follow design §3.3; for example
# northstar-dsp impressions on the anchor = round(4000 x level x trend x wk x mo x jitter):
# level = 0.60 + 0.90 x 0.006445476858543573532838583118 (unit(seed, key,
# "campaign_spread.level_pct")), trend at the anchor = growth_ramp (1200 + 55 x 55)/1200 x
# (1 + slope), jitter = 1 + vol x (2 x 0.2942789827475720700988674750 - 1). The draws were
# printed once by evidence/golden_values_helper.py and are not recomputed here. They move with
# the anchors: the re-anchor commit regenerates the table ("anchor-driven").
GOLDEN_VALUES: tuple[tuple[str, str, str, Decimal | int], ...] = (
    ("campaign:northstar-dsp", "2026-10-06", "impressions", 13181),  # growth_ramp
    ("campaign:northstar-dsp", "2026-10-06", "clicks", 785),  # growth_ramp
    ("campaign:northstar-dsp", "2026-10-06", "purchases", 91),  # growth_ramp
    ("campaign:northstar-dsp", "2026-10-06", "spend", Decimal("945.8893")),  # growth_ramp
    (
        "campaign:northstar-dsp",
        "2026-10-06",
        "revenue_attributed",
        Decimal("2777.8120"),
    ),  # growth_ramp
    ("campaign:northstar-dsp", "2026-10-06", "measurable_impressions", 10697),  # growth_ramp
    ("campaign:northstar-dsp", "2026-10-06", "viewable_impressions", 8870),  # growth_ramp
    ("campaign:northstar-dsp", "2026-10-06", "reach_users", 8294),  # growth_ramp
    ("campaign:northstar-dsp", "2026-10-06", "distinct_user_count", 805),  # growth_ramp
    ("campaign:harbor-dsp", "2026-10-06", "impressions", 1428),  # sustained_decline
    ("campaign:harbor-dsp", "2026-10-06", "clicks", 88),  # sustained_decline
    ("campaign:harbor-dsp", "2026-10-06", "purchases", 7),  # sustained_decline
    ("campaign:harbor-dsp", "2026-10-06", "spend", Decimal("66.3137")),  # sustained_decline
    (
        "campaign:harbor-dsp",
        "2026-10-06",
        "revenue_attributed",
        Decimal("193.3257"),
    ),  # sustained_decline
    ("campaign:harbor-dsp", "2026-10-06", "measurable_impressions", 1193),  # sustained_decline
    ("campaign:harbor-dsp", "2026-10-06", "viewable_impressions", 968),  # sustained_decline
    ("campaign:harbor-dsp", "2026-10-06", "reach_users", 820),  # sustained_decline
    ("campaign:harbor-dsp", "2026-10-06", "distinct_user_count", 108),  # sustained_decline
    ("campaign:fieldwork-sponsored", "2026-10-06", "impressions", 8637),  # spike_and_fade
    ("campaign:fieldwork-sponsored", "2026-10-06", "clicks", 863),  # spike_and_fade
    ("campaign:fieldwork-sponsored", "2026-10-06", "purchases", 92),  # spike_and_fade
    ("campaign:fieldwork-sponsored", "2026-10-06", "spend", Decimal("662.1907")),  # spike_and_fade
    (
        "campaign:fieldwork-sponsored",
        "2026-10-06",
        "revenue_attributed",
        Decimal("2295.0784"),
    ),  # spike_and_fade
    (
        "campaign:fieldwork-sponsored",
        "2026-10-06",
        "measurable_impressions",
        6505,
    ),  # spike_and_fade
    ("campaign:fieldwork-sponsored", "2026-10-06", "viewable_impressions", 5321),  # spike_and_fade
    ("campaign:fieldwork-sponsored", "2026-10-06", "reach_users", 4044),  # spike_and_fade
    ("campaign:fieldwork-sponsored", "2026-10-06", "distinct_user_count", 883),  # spike_and_fade
    (
        "campaign:northstar-dsp-05",
        "2026-10-06",
        "impressions",
        5668,
    ),  # flat_traffic_rising_conversion
    ("campaign:northstar-dsp-05", "2026-10-06", "clicks", 326),  # flat_traffic_rising_conversion
    ("campaign:northstar-dsp-05", "2026-10-06", "purchases", 55),  # flat_traffic_rising_conversion
    (
        "campaign:northstar-dsp-05",
        "2026-10-06",
        "spend",
        Decimal("267.9693"),
    ),  # flat_traffic_rising_conversion
    (
        "campaign:northstar-dsp-05",
        "2026-10-06",
        "revenue_attributed",
        Decimal("1786.5039"),
    ),  # flat_traffic_rising_conversion
    (
        "campaign:northstar-dsp-05",
        "2026-10-06",
        "measurable_impressions",
        4306,
    ),  # flat_traffic_rising_conversion
    (
        "campaign:northstar-dsp-05",
        "2026-10-06",
        "viewable_impressions",
        3755,
    ),  # flat_traffic_rising_conversion
    (
        "campaign:northstar-dsp-05",
        "2026-10-06",
        "reach_users",
        2677,
    ),  # flat_traffic_rising_conversion
    (
        "campaign:northstar-dsp-05",
        "2026-10-06",
        "distinct_user_count",
        346,
    ),  # flat_traffic_rising_conversion
    ("campaign:northstar-dsp-24", "2026-10-06", "impressions", 30613),  # stable_growth
    ("campaign:northstar-dsp-24", "2026-10-06", "clicks", 3032),  # stable_growth
    ("campaign:northstar-dsp-24", "2026-10-06", "purchases", 237),  # stable_growth
    ("campaign:northstar-dsp-24", "2026-10-06", "spend", Decimal("1950.1622")),  # stable_growth
    (
        "campaign:northstar-dsp-24",
        "2026-10-06",
        "revenue_attributed",
        Decimal("6699.6809"),
    ),  # stable_growth
    ("campaign:northstar-dsp-24", "2026-10-06", "measurable_impressions", 24139),  # stable_growth
    ("campaign:northstar-dsp-24", "2026-10-06", "viewable_impressions", 19552),  # stable_growth
    ("campaign:northstar-dsp-24", "2026-10-06", "reach_users", 13933),  # stable_growth
    ("campaign:northstar-dsp-24", "2026-10-06", "distinct_user_count", 3052),  # stable_growth
    ("campaign:missing-name-sponsored", "2026-10-06", "impressions", 3482),  # zero_conversion
    ("campaign:missing-name-sponsored", "2026-10-06", "clicks", 323),  # zero_conversion
    ("campaign:missing-name-sponsored", "2026-10-06", "purchases", 0),  # zero_conversion
    (
        "campaign:missing-name-sponsored",
        "2026-10-06",
        "spend",
        Decimal("243.1463"),
    ),  # zero_conversion
    (
        "campaign:missing-name-sponsored",
        "2026-10-06",
        "revenue_attributed",
        Decimal("0.0000"),
    ),  # zero_conversion
    (
        "campaign:missing-name-sponsored",
        "2026-10-06",
        "measurable_impressions",
        2737,
    ),  # zero_conversion
    (
        "campaign:missing-name-sponsored",
        "2026-10-06",
        "viewable_impressions",
        2352,
    ),  # zero_conversion
    ("campaign:missing-name-sponsored", "2026-10-06", "reach_users", 1478),  # zero_conversion
    (
        "campaign:missing-name-sponsored",
        "2026-10-06",
        "distinct_user_count",
        343,
    ),  # zero_conversion
    ("campaign:northstar-sponsored-30", "2026-09-26", "impressions", 9019),  # coverage_gap
    ("campaign:northstar-sponsored-30", "2026-09-26", "purchases", 63),  # coverage_gap
    ("campaign:northstar-sponsored-08", "2026-10-02", "impressions", 18293),  # promotion
    ("campaign:northstar-dsp-07", "2026-10-02", "impressions", 605),  # budget_shift_down
    ("campaign:northstar-dsp-11", "2026-10-02", "impressions", 29378),  # budget_shift_up
    ("campaign:northstar-dsp-29", "2026-09-25", "impressions", 0),  # launch_before
    ("campaign:northstar-dsp-29", "2026-09-25", "spend", Decimal("0")),  # launch_before
    ("campaign:northstar-dsp-16", "2026-10-05", "impressions", 0),  # pause
    ("campaign:northstar-dsp-16", "2026-10-05", "clicks", 0),  # pause
    ("campaign:northstar-dsp", "2025-11-20", "impressions", 3248),  # long_run_minus_320
    ("campaign:northstar-dsp", "2026-08-31", "impressions", 5128),  # month_boundary_2026-08-31
    ("campaign:northstar-dsp", "2026-09-01", "impressions", 5685),  # month_boundary_2026-09-01
    ("B0FIXTUR00", "2026-10-06", "units_shipped", 128),  # stable_growth
    ("B0FIXTUR00", "2026-10-06", "customer_returns", 7),  # stable_growth
    ("B0FIXTUR00", "2026-10-06", "revenue_shipped", Decimal("3653.4779")),  # stable_growth
    ("B0FIXTUR00", "2026-10-06", "units_ordered", 133),  # stable_growth
    ("B0FIXTUR00", "2026-10-06", "glance_views", 2350),  # stable_growth
    ("B0FIXTUR03", "2026-10-06", "units_shipped", 146),  # returns_pressure
    ("B0FIXTUR03", "2026-10-06", "customer_returns", 28),  # returns_pressure
    ("B0FIXTUR03", "2026-10-06", "revenue_shipped", Decimal("4232.6956")),  # returns_pressure
    ("B0FIXTUR03", "2026-10-06", "units_ordered", 155),  # returns_pressure
    ("B0FIXTUR03", "2026-10-06", "glance_views", 2711),  # returns_pressure
    ("B0FIXTUR02", "2026-10-03", "units_shipped", 63),  # divergence inside
    ("B0FIXTUR02", "2026-10-03", "customer_returns", 4),  # divergence inside
    ("B0FIXTUR02", "2026-10-03", "revenue_shipped", Decimal("2019.7169")),  # divergence inside
    ("B0FIXTUR02", "2026-10-03", "units_ordered", 81),  # divergence inside
    ("B0FIXTUR02", "2026-10-03", "glance_views", 1733),  # divergence inside
    ("B0FIXTUR06", "2026-09-27", "units_shipped", 95),  # price before
    ("B0FIXTUR06", "2026-09-27", "customer_returns", 6),  # price before
    ("B0FIXTUR06", "2026-09-27", "revenue_shipped", Decimal("2546.1187")),  # price before
    ("B0FIXTUR06", "2026-09-27", "units_ordered", 99),  # price before
    ("B0FIXTUR06", "2026-09-27", "glance_views", 1649),  # price before
    ("B0FIXTUR06", "2026-09-28", "units_shipped", 115),  # price first day
    ("B0FIXTUR06", "2026-09-28", "customer_returns", 6),  # price first day
    ("B0FIXTUR06", "2026-09-28", "revenue_shipped", Decimal("2817.7128")),  # price first day
    ("B0FIXTUR06", "2026-09-28", "units_ordered", 120),  # price first day
    ("B0FIXTUR06", "2026-09-28", "glance_views", 1967),  # price first day
)


@pytest.mark.covers("ADR-086")
def test_7b_golden_values_are_exact_in_the_generated_fixtures() -> None:
    catalog = _catalog()
    campaigns = {campaign.key: campaign for campaign in catalog.campaigns}
    impressions = {
        (row["impression_date_utc"], row["campaign_id_string"]): row
        for row in _scenario_fixture("amc-dsp-impressions").rows
    }
    sales = {
        (row["startDate"], row["asin"]): row
        for row in (_scenario_fixture("vendor-sales-by-asin").document or {})["salesByAsin"]
    }
    for entity, day, measure, expected in GOLDEN_VALUES:
        on = date.fromisoformat(day)
        if entity in campaigns:
            campaign = campaigns[entity]
            actual = catalog.values.campaign_measure(
                catalog.values.campaign_day(entity, campaign.scenario, on), measure
            )
            source_id = next(iter(campaign.source_aliases.values()))
            if measure == "impressions" and (day, source_id) in impressions:
                assert impressions[(day, source_id)]["impressions"] == expected
        else:
            product = next(item for item in catalog.products if item.asin == entity)
            actual = catalog.values.product_measure(
                catalog.values.product_day(entity, product.scenario, on), measure
            )
            if measure == "units_shipped":
                assert sales[(day, entity)]["shippedUnits"] == expected
        assert actual == expected, (entity, day, measure)


@pytest.mark.covers("ADR-086")
def test_7c_the_funnel_and_roas_hold_and_the_purchase_guard_never_binds() -> None:
    catalog = _catalog()
    shape = catalog.shape
    values = catalog.values
    days = _span_list()
    for campaign in catalog.campaigns:
        for day in days:
            item = values.campaign_day(campaign.key, campaign.scenario, day)
            assert item.purchases <= item.clicks
            if item.clicks >= 5 and campaign.scenario != "zero_conversion":
                assert item.purchases < item.clicks, (campaign.key, day)
        if campaign.scenario != "flat_traffic_rising_conversion":
            continue

        def rate(end: date, key: str = campaign.key) -> Decimal:
            window = [
                values.campaign_day(key, "flat_traffic_rising_conversion", end - timedelta(days=i))
                for i in range(7)
            ]
            return Decimal(sum(w.purchases for w in window)) / sum(w.clicks for w in window)

        story_start = shape.shape_anchor - timedelta(days=shape.story_days - 1)
        assert rate(shape.shape_anchor) > rate(story_start), campaign.key


@pytest.mark.covers("ADR-086")
def test_8_two_builds_are_byte_identical() -> None:
    catalog = _catalog()
    registry = _committed_registry()
    options = FixtureBuildOptions(rows=catalog.shape.fixture_span.span_days, entity_catalog=catalog)
    for contract_id in ("amc-dsp-clicks", "vendor-traffic-by-asin"):
        contract = next(item for item in _committed_contracts() if item.contract_id == contract_id)
        assert render(build(contract, registry, options)) == render(
            build(contract, registry, options)
        )


def _with_shape(**changes: Any) -> SyntheticEntityCatalog:
    base = _catalog()
    return dataclasses.replace(base, shape=dataclasses.replace(base.shape, **changes))


def _all_values(
    catalog: SyntheticEntityCatalog, days: list[date]
) -> dict[tuple[str, date], object]:
    found: dict[tuple[str, date], object] = {}
    for campaign in catalog.campaigns:
        for day in days:
            found[(campaign.key, day)] = catalog.values.campaign_day(
                campaign.key, campaign.scenario, day
            )
    for product in catalog.products:
        for day in days:
            found[(product.asin, day)] = catalog.values.product_day(
                product.asin, product.scenario, day
            )
    return found


@pytest.mark.covers("ADR-086")
def test_8b_a_longer_span_changes_no_earlier_final_value() -> None:
    base = _catalog()
    span = base.shape.fixture_span
    longer = _with_shape(
        fixture_span=dataclasses.replace(span, last_day=span.last_day + timedelta(days=1))
    )
    days = [span.last_day - timedelta(days=2 + i) for i in range(60)]
    assert _all_values(base, days) == _all_values(longer, days)


@pytest.mark.covers("ADR-086")
def test_8c_an_earlier_first_day_changes_no_common_value() -> None:
    base = _catalog()
    span = base.shape.fixture_span
    earlier = _with_shape(
        fixture_span=dataclasses.replace(span, first_day=span.first_day - timedelta(days=30))
    )
    days = [span.first_day + timedelta(days=i) for i in range(0, span.span_days, 9)]
    assert _all_values(base, days) == _all_values(earlier, days)
    registry = _committed_registry()
    contract = next(item for item in _committed_contracts() if item.contract_id == "amc-dsp-clicks")
    rows_base = build(
        contract, registry, FixtureBuildOptions(rows=span.span_days, entity_catalog=base)
    ).rows
    rows_earlier = build(
        contract, registry, FixtureBuildOptions(rows=span.span_days + 30, entity_catalog=earlier)
    ).rows
    later = [
        row for row in rows_earlier if row["impression_date_utc"] >= span.first_day.isoformat()
    ]
    assert later == list(rows_base)


@pytest.mark.covers("ADR-086")
@pytest.mark.parametrize("move", [1, 3, 7, 14])
def test_8d_an_event_anchor_move_changes_only_days_inside_its_re_landing(move: int) -> None:
    base = _catalog()
    anchor = base.shape.event_anchor
    shift = timedelta(days=move)

    def moved(event: ShapeEvent) -> ShapeEvent:
        return dataclasses.replace(
            event,
            **{
                name: getattr(event, name) + shift
                for name in ("first", "last", "on", "ramp_end")
                if hasattr(event, name)
            },
        )

    after = _with_shape(
        event_anchor=anchor + shift,
        fixture_span=dataclasses.replace(base.shape.fixture_span, last_day=anchor + shift),
        events=tuple(moved(event) for event in base.shape.events),
    )
    relanding = anchor + shift - timedelta(days=27)
    early = [relanding - timedelta(days=1 + i) for i in range(40)]
    assert _all_values(base, early) == _all_values(after, early)
    common = [anchor - timedelta(days=2 + i) for i in range(30)]
    before_values, after_values = _all_values(base, common), _all_values(after, common)
    changed = {
        day
        for (_entity, day), value in before_values.items()
        if after_values[(_entity, day)] != value
    }
    assert all(anchor - timedelta(days=13) <= day <= anchor - timedelta(days=2) for day in changed)


@pytest.mark.covers("ADR-086")
def test_10_hash_inputs_are_injective_and_campaign_constants_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ShapeError, match="hash input part contains"):
        unit("seed", "a|b")
    seen: dict[str, tuple[str, ...]] = {}
    original = values_module.unit

    def recording(seed: str, *parts: object) -> Decimal:
        texts = (seed, *map(str, parts))
        joined = "|".join(texts)
        assert seen.setdefault(joined, texts) == texts
        return original(seed, *parts)

    monkeypatch.setattr(values_module, "unit", recording)
    catalog = dataclasses.replace(_catalog())
    for campaign in catalog.campaigns:
        for day in _span_list()[-30:]:
            catalog.values.campaign_day(campaign.key, campaign.scenario, day)
    assert len(seen) > 1000
    monkeypatch.setattr(values_module, "unit", original)
    shape = _catalog().shape
    fields = (
        "level_pct",
        "annual_growth_pct",
        "slope_pct",
        "volatility_pct",
        "cpc",
        "conversion_rate_pct",
        "order_value",
        "ctr_pct",
        "measurable_share",
        "viewable_share",
        "frequency",
    )
    keys = [campaign.key for campaign in _catalog().campaigns]

    def ranks(name: str) -> list[int]:
        draws = [unit(shape.seed, key, f"campaign_spread.{name}") for key in keys]
        order = sorted(range(len(draws)), key=draws.__getitem__)
        result = [0] * len(draws)
        for rank, index in enumerate(order):
            result[index] = rank
        return result

    n = len(keys)
    for left_index, left in enumerate(fields):
        for right in fields[left_index + 1 :]:
            a, b = ranks(left), ranks(right)
            rho = 1 - 6 * sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) / (n * (n * n - 1))
            assert abs(rho) <= 0.5, (left, right, rho)
