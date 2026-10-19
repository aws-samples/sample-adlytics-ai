# SPDX-License-Identifier: MIT-0
"""Suppression splitting and delivery-keyed residual evidence used by Semantic V2."""

from __future__ import annotations

import json
import pathlib
from datetime import date

import pytest

from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.curation.manifests import (
    BuildManifestRequest,
    build_manifest,
    checksum_of,
    declared_columns_of,
)
from adapters.jobs.curation.readers import DeliveredFile, read
from adapters.jobs.schema_generation.merge import (
    WITHHELD_GRAIN,
    merge_tables,
    withheld_table,
    withheld_tables,
)
from domain.curation.ingestion import IngestionError, split_withheld
from domain.curation.landing import LandedObject, Manifest
from domain.curation.mapping import MappingRequest, map_rows
from domain.data_contracts.contract import DataContract

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-034", "G-31")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
TRAPS = ROOT / "tests/fixtures/traps"
FIXTURE = "amc-withheld-residual"
CONTRACT = "amc-dsp-impressions"


@pytest.fixture(scope="module")
def contracts() -> tuple[DataContract, ...]:
    return tuple(contract for _, contract in load_contracts(ROOT))


@pytest.fixture(scope="module")
def by_id(contracts: tuple[DataContract, ...]) -> dict[str, DataContract]:
    return {contract.contract_id: contract for contract in contracts}


def _delivery(contract: DataContract) -> tuple[Manifest, tuple[dict[str, object], ...]]:
    text = (TRAPS / f"{FIXTURE}.csv").read_text()
    body = text.encode()
    manifest = build_manifest(
        BuildManifestRequest(
            declaration=json.loads((TRAPS / f"{FIXTURE}.manifest.json").read_text()),
            objects=(
                LandedObject(
                    name="data-0001.csv",
                    byte_count=len(body),
                    checksum=checksum_of(body),
                ),
            ),
            declared_columns=declared_columns_of(text, source_format="csv"),
        )
    )
    source = read(
        DeliveredFile(
            text=text,
            source_format=contract.source_format,
            manifest_text=(TRAPS / f"{FIXTURE}.manifest.json").read_text(),
        )
    )
    return manifest, map_rows(MappingRequest(contract=contract, source=source))


class TestResidualSchema:
    def test_one_table_exists_for_every_suppressible_fact(
        self, contracts: tuple[DataContract, ...]
    ) -> None:
        tables = merge_tables(list(contracts), load_registry(ROOT))
        assert {table.name for table in withheld_tables(tables, list(contracts))} == {
            "fct_ad_exposure_withheld",
            "fct_attributed_conversion_withheld",
            "fct_deduplicated_temporal_attribution_withheld",
            "fct_exposure_frequency_withheld",
            "fct_exposure_group_outcome_withheld",
            "fct_retail_purchase_amc_withheld",
            "fct_segment_delivery_withheld",
            "fct_shopper_acquisition_withheld",
        }

    def test_every_residual_grain_column_comes_from_the_manifest(
        self, contracts: tuple[DataContract, ...]
    ) -> None:
        tables = {table.name: table for table in merge_tables(list(contracts), load_registry(ROOT))}
        residual = withheld_table(tables["fct_ad_exposure"])
        assert residual.grain == WITHHELD_GRAIN
        assert "campaign_key" not in residual.grain
        assert "date_key" not in residual.grain
        assert all(
            column.nullable is False for column in residual.columns if column.name in residual.grain
        )


class TestSplittingTheDelivery:
    def test_named_rows_keep_their_source_ordinal_and_nulled_rows_become_residuals(
        self, by_id: dict[str, DataContract]
    ) -> None:
        contract = by_id[CONTRACT]
        manifest, rows = _delivery(contract)
        split = split_withheld(contract, manifest, rows)

        assert len(split.attributable_rows) == 1
        assert split.attributable_rows[0]["campaign_key"] == "TRAPRES-NAMED"
        assert split.attributable_ordinals == (0,)
        assert split.residual is not None
        assert split.residual.row_count == 1

    def test_the_residual_is_keyed_on_the_delivery(self, by_id: dict[str, DataContract]) -> None:
        contract = by_id[CONTRACT]
        manifest, rows = _delivery(contract)
        residual = split_withheld(contract, manifest, rows).residual

        assert residual is not None
        assert residual.contract_id == CONTRACT
        assert residual.marketplace == "ATVPDKIKX0DER"
        assert residual.period_start == date(2026, 8, 25)
        assert residual.period_end == date(2026, 8, 25)

    def test_a_withheld_amount_without_its_currency_is_never_summed(
        self, by_id: dict[str, DataContract]
    ) -> None:
        """The live shape: the withheld row's currency is NULL, so its spend has no unit.

        Its count still joins the residual; its money doesn't, and with no currency-bearing
        withheld row the residual's spend stays NULL, which reads "withheld" (AMC-V decision 5).
        """
        contract = by_id[CONTRACT]
        manifest, rows = _delivery(contract)
        assert rows[1]["currency_code"] is None
        residual = split_withheld(contract, manifest, rows).residual

        assert residual is not None
        assert {name: str(value) for name, value in residual.measures.items()} == {
            "channel_group_key": "display",
            "impressions": "457",
        }

    def test_a_withheld_amount_with_its_currency_is_summed_as_before(
        self, by_id: dict[str, DataContract]
    ) -> None:
        """Positive control: the same row carrying its currency sums its scaled spend."""
        contract = by_id[CONTRACT]
        manifest, rows = _delivery(contract)
        with_currency = (rows[0], {**rows[1], "currency_code": "USD"})
        residual = split_withheld(contract, manifest, with_currency).residual

        assert residual is not None
        assert str(residual.measures["impressions"]) == "457"
        assert str(residual.measures["spend"]) == "1.3940"
        assert residual.measures["currency_code"] == "USD"
        assert "reach_users" not in residual.measures

    def test_only_the_currency_bearing_withheld_amount_joins_the_residual(
        self, by_id: dict[str, DataContract]
    ) -> None:
        """Two withheld rows, one with its currency: only that one's spend is summed."""
        contract = by_id[CONTRACT]
        manifest, rows = _delivery(contract)
        withheld = rows[1]
        mixed = (rows[0], {**withheld, "currency_code": "USD"}, withheld)
        residual = split_withheld(contract, manifest, mixed).residual

        assert residual is not None
        assert residual.row_count == 2
        assert str(residual.measures["impressions"]) == "914"
        assert str(residual.measures["spend"]) == "1.3940"

    def test_the_residual_carries_conformed_provenance(
        self, by_id: dict[str, DataContract]
    ) -> None:
        contract = by_id[CONTRACT]
        manifest, rows = _delivery(contract)
        residual = split_withheld(contract, manifest, rows).residual

        assert residual is not None
        assert residual.provenance == {
            "basis_key": "ad_attributed",
            "ecosystem_key": "amazon",
            "platform_key": "amazon_ads",
            "source_timezone": "UTC",
        }

    def test_nothing_withheld_produces_no_residual(self, by_id: dict[str, DataContract]) -> None:
        contract = by_id[CONTRACT]
        manifest, rows = _delivery(contract)
        named = tuple(row for row in rows if row.get("campaign_key"))
        split = split_withheld(contract, manifest, named)

        assert len(split.attributable_rows) == 1
        assert split.residual is None

    def test_a_partially_nulled_row_is_refused(self, by_id: dict[str, DataContract]) -> None:
        contract = by_id[CONTRACT]
        manifest, rows = _delivery(contract)
        with pytest.raises(IngestionError, match="belongs in neither"):
            split_withheld(contract, manifest, ({**rows[0], "campaign_key": None},))
