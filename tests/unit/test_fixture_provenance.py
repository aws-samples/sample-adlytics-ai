# SPDX-License-Identifier: MIT-0
"""Fixture evidence classes and fictional identity invariants from ADR-051."""

from __future__ import annotations

import csv
import pathlib

import pytest

from adapters.jobs.fixture_generation.catalog import (
    CAMPAIGN_SCENARIOS,
    SyntheticCatalogError,
    load_synthetic_entity_catalog,
)
from adapters.jobs.fixture_generation.provenance import (
    FixtureEvidenceEntry,
    FixtureEvidenceManifest,
    FixtureProvenanceClass,
    FixtureProvenanceError,
    load_fixture_evidence_manifest,
    validate_fixture_provenance,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-051")]

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_every_managed_artifact_has_one_bounded_evidence_class() -> None:
    catalog = load_synthetic_entity_catalog(ROOT)
    manifest = load_fixture_evidence_manifest(ROOT)

    validate_fixture_provenance(ROOT, manifest, catalog)

    assert {entry.evidence_class for entry in manifest.entries} == set(FixtureProvenanceClass)


def test_campaign_aliases_preserve_distinct_dsp_and_sponsored_lexical_forms() -> None:
    catalog = load_synthetic_entity_catalog(ROOT)
    aliases = {
        namespace: value
        for campaign in catalog.campaigns
        for namespace, value in campaign.source_aliases.items()
    }

    assert aliases["amc.dsp.campaign_id_string"].isdigit()
    assert len(aliases["amc.dsp.campaign_id_string"]) == 18
    assert aliases["amc.sponsored.campaign_id_string"].startswith("A0")
    assert not aliases["amc.sponsored.campaign_id_string"].isdigit()


def test_duplicate_names_and_renames_are_catalogued_without_changing_identity() -> None:
    catalog = load_synthetic_entity_catalog(ROOT)
    names_to_keys: dict[str, set[str]] = {}
    for campaign in catalog.campaigns:
        for name in campaign.names:
            names_to_keys.setdefault(name.value, set()).add(campaign.key)

    assert len(names_to_keys["Northstar Home | Brand"]) == 2
    renamed = next(item for item in catalog.campaigns if item.key == "campaign:northstar-dsp")
    assert len(renamed.names) == 2
    assert {name.value for name in renamed.names} == {
        "Northstar Home | Prospecting",
        "Northstar Home | Growth",
    }


def test_schema_generated_amc_rows_use_a_clean_suppression_baseline() -> None:
    path = ROOT / "tests/fixtures/generated/amc-dsp-impressions.csv"
    rows = tuple(csv.DictReader(path.read_text().splitlines()))

    assert rows
    assert {row["rows_suppressed"] for row in rows} == {"false"}
    assert {row["suppression_reason"] for row in rows} == {""}


def test_privacy_gate_rejects_a_live_looking_amc_identity(tmp_path: pathlib.Path) -> None:
    catalog = load_synthetic_entity_catalog(ROOT)
    managed = tmp_path / "tests/fixtures/generated"
    managed.mkdir(parents=True)
    (managed / "unsafe.txt").write_text("instance=amc12345678")
    manifest = FixtureEvidenceManifest(
        managed_roots=("tests/fixtures/generated",),
        entries=(
            FixtureEvidenceEntry(
                entry_id="unsafe",
                evidence_class=FixtureProvenanceClass.SCHEMA_DERIVED,
                patterns=("tests/fixtures/generated/*",),
                owner_key="owner",
                owner="contracts",
            ),
        ),
    )

    with pytest.raises(FixtureProvenanceError, match="live-looking AMC"):
        validate_fixture_provenance(tmp_path, manifest, catalog)


@pytest.mark.parametrize(
    "case",
    (
        ("campaign_id_string", "REAL-CAMPAIGN-123", "campaign identity"),
        ("campaign", "Customer Holiday Campaign", "campaign name"),
        ("advertiser_id", "REAL-ADVERTISER-123", "advertiser identity"),
        ("advertiser", "Customer Legal Name", "advertiser name"),
    ),
)
def test_privacy_gate_rejects_undeclared_advertising_identity(
    tmp_path: pathlib.Path,
    case: tuple[str, str, str],
) -> None:
    field, value, message = case
    catalog = load_synthetic_entity_catalog(ROOT)
    managed = tmp_path / "tests/fixtures/generated"
    managed.mkdir(parents=True)
    (managed / "unsafe.csv").write_text(f"{field}\n{value}\n")
    manifest = FixtureEvidenceManifest(
        managed_roots=("tests/fixtures/generated",),
        entries=(
            FixtureEvidenceEntry(
                entry_id="unsafe",
                evidence_class=FixtureProvenanceClass.SCHEMA_DERIVED,
                patterns=("tests/fixtures/generated/*",),
                owner_key="owner",
                owner="contracts",
            ),
        ),
    )

    with pytest.raises(FixtureProvenanceError, match=message):
        validate_fixture_provenance(tmp_path, manifest, catalog)


def test_visible_catalog_has_exactly_sixty_scenario_declared_campaigns() -> None:
    catalog = load_synthetic_entity_catalog(ROOT)
    dsp_aliases = {
        campaign.source_aliases["amc.dsp.campaign_id_string"]
        for campaign in catalog.campaigns
        if campaign.channel == "amazon_dsp"
    }
    sponsored_aliases = {
        campaign.source_aliases["amc.sponsored.campaign_id_string"]
        for campaign in catalog.campaigns
        if campaign.channel != "amazon_dsp"
    }

    assert len(catalog.campaigns) == 60
    assert {campaign.scenario for campaign in catalog.campaigns} == CAMPAIGN_SCENARIOS
    assert len(dsp_aliases) == 30
    assert all(alias.isdigit() and len(alias) == 18 for alias in dsp_aliases)
    assert len(sponsored_aliases) == 30
    assert all(alias.startswith("A0") and not alias.isdigit() for alias in sponsored_aliases)
    assert any(not campaign.names for campaign in catalog.campaigns)


def test_catalog_loader_rejects_a_campaign_scenario_outside_the_closed_set(
    tmp_path: pathlib.Path,
) -> None:
    source = ROOT / "contracts/shared/synthetic-entity-catalog.yaml"
    target = tmp_path / "contracts/shared/synthetic-entity-catalog.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(source.read_text().replace("scenario: growth_ramp", "scenario: invented", 1))

    with pytest.raises(SyntheticCatalogError, match="campaign scenario must be one of"):
        load_synthetic_entity_catalog(tmp_path)


def test_catalog_loader_rejects_a_channel_ad_product_mismatch(tmp_path: pathlib.Path) -> None:
    source = ROOT / "contracts/shared/synthetic-entity-catalog.yaml"
    target = tmp_path / "contracts/shared/synthetic-entity-catalog.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        source.read_text().replace(
            "ad_product: amazon_dsp",
            "ad_product: sponsored_products",
            1,
        )
    )

    with pytest.raises(SyntheticCatalogError, match="requires ad product amazon_dsp"):
        load_synthetic_entity_catalog(tmp_path)
