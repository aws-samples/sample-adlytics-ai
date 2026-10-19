# SPDX-License-Identifier: MIT-0
"""The raw-zone landing contract: what makes a delivery identifiable before it is read.

Three properties carry most of the weight here, and each prevents a distinct silent failure.

- **The prefix is derived from the manifest, never parsed back into one.** So the path is a
  checksum on the manifest rather than a second source of truth, and a producer that renames a
  segment is caught instead of loading a delivery as something it is not.
- **One dataset can resolve to two contracts.** A vendor document carries two sibling arrays at
  two grains, so a landing keyed on a contract could not express it. Getting this wrong means
  one of the two grains is silently never loaded.
- **A rejected landing is not a quarantine.** When we cannot tell which partition a delivery
  belongs to there is nothing to quarantine, so it raises rather than returning an outcome that
  would make the load-error metric fire for a case the platform handles.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from adapters.jobs.curation.manifests import read_manifest, write_manifest
from domain.curation.landing import (
    GOVERNED_MANIFEST_LAYOUT,
    MANIFEST_NAME,
    LandedObject,
    LandingError,
    Manifest,
    contracts_for,
    delivery_prefix_of,
    misplacement,
    require_installed_governance,
)
from domain.curation.mapping import options_hash
from domain.data_contracts.contract import DataContract, FieldMapping, SourceFormat
from domain.semantics.identity import SourceIdentifier
from domain.shared_kernel.provenance import MeasurementBasis, ProducerGovernance

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-030", "ADR-053", "F-01")]

RETRIEVED = datetime(2026, 8, 20, 6, 30, 15, tzinfo=UTC)
WINDOW_START = date(2026, 8, 10)
WINDOW_END = date(2026, 8, 16)
WATERMARK = date(2026, 8, 17)
SOURCE_SCOPE = SourceIdentifier("vendor_central.vendor_group", "fictional-vendor-group")
_DIGEST = "sha256:" + "a" * 64


def _governance(contract_id: str, *, source_pack_id: str | None = None) -> ProducerGovernance:
    return ProducerGovernance(
        query_pack_id=contract_id,
        query_pack_version=1,
        query_pack_digest=_DIGEST,
        query_digest=_DIGEST,
        data_contract_id=contract_id,
        data_contract_digest=_DIGEST,
        source_pack_id=source_pack_id or contract_id,
        source_pack_digest=_DIGEST,
    )


SALES_OPTIONS: dict[str, object] = {
    "reportPeriod": "DAY",
    "distributorView": "MANUFACTURING",
    "sellingProgram": "RETAIL",
}


@dataclass(frozen=True, slots=True)
class _Landing:
    """A manifest plus the key it was found at, so a test states both in one place."""

    manifest: Manifest
    key: str


def _object(name: str = "data-0001.json") -> LandedObject:
    return LandedObject(name=name, byte_count=2048, checksum="sha256:abc123")


def _manifest(
    *,
    dataset: str = "GET_VENDOR_SALES_REPORT",
    source_scope: SourceIdentifier = SOURCE_SCOPE,
    options: dict[str, object] | None = None,
) -> Manifest:
    chosen = SALES_OPTIONS if options is None else options
    return Manifest(
        source_system="vendor_central",
        source_dataset=dataset,
        source_scope=source_scope,
        option_set=chosen,
        option_set_hash=options_hash(chosen),
        marketplace="ATVPDKIKX0DER",
        period_start=WINDOW_START,
        period_end=WINDOW_END,
        source_watermark=WATERMARK,
        retrieved_at=RETRIEVED,
        declared_columns=("asin", "orderedUnits", "orderedRevenue"),
        objects=(_object(),),
    )


def _landing() -> _Landing:
    manifest = _manifest()
    return _Landing(manifest=manifest, key=manifest.manifest_key)


@dataclass(frozen=True, slots=True)
class _Spec:
    """What varies between the contracts these tests need. A dataclass keeps the arity down."""

    contract_id: str
    dataset: str = "GET_VENDOR_SALES_REPORT"
    system: str = "vendor_central"
    source_format: SourceFormat = SourceFormat.JSON_DUAL_ARRAY
    grain: tuple[str, ...] = ("amazon_asin", "date_key", "marketplace_key")


def _contract(spec: _Spec) -> DataContract:
    return DataContract(
        contract_id=spec.contract_id,
        contract_version=1,
        source_system=spec.system,
        source_dataset=spec.dataset,
        source_format=spec.source_format,
        target_table="fct_retail_sales",
        grain=spec.grain,
        partition_keys=("marketplace_key", "date_key"),
        measurement_basis=MeasurementBasis.RETAIL_ACTUAL,
        source_timezone="America/Los_Angeles",
        ecosystem_key="amazon",
        platform_key="amazon_retail",
        fields=(FieldMapping(source_path="salesByAsin[].asin", target_column="amazon_asin"),),
        restatement_window_days=45,
        staleness_threshold_hours=96,
    )


# ---------------------------------------------------------------------------
# The prefix is derived, and it is ordered general to specific
# ---------------------------------------------------------------------------


def test_prefix_orders_segments_from_source_down_to_retrieval() -> None:
    assert _manifest().prefix == (
        "vendor_central/"
        "GET_VENDOR_SALES_REPORT/"
        "source_scope=b89009b9b4bec2af317c97bd2e576519c0e53005ecabfeeb0a3e859e7f60178f/"
        "marketplace=ATVPDKIKX0DER/"
        "window=2026-08-10_2026-08-16/"
        f"options={options_hash(SALES_OPTIONS)}/"
        "retrieved=20260820T063015Z/"
    )


def test_governed_layout_qualifies_the_prefix_with_the_exact_query_pack() -> None:
    governed = replace(
        _manifest(),
        governance=_governance("amc-campaign-dsp"),
        layout_version=GOVERNED_MANIFEST_LAYOUT,
    )

    assert "/query_pack=amc-campaign-dsp/source_scope=" in governed.prefix


def test_a_governed_legacy_layout_manifest_remains_byte_readable() -> None:
    legacy = replace(_manifest(), governance=_governance("vendor-sales-by-asin"))
    payload = json.loads(write_manifest(legacy))

    assert "layout_version" not in payload
    assert read_manifest(json.dumps(payload)) == legacy


def test_manifest_key_is_the_prefix_plus_the_marker_name() -> None:
    manifest = _manifest()
    assert manifest.manifest_key == f"{manifest.prefix}{MANIFEST_NAME}"


def test_object_keys_sit_beside_the_manifest_in_the_same_prefix() -> None:
    manifest = _manifest()
    assert manifest.object_keys() == (f"{manifest.prefix}data-0001.json",)


def test_a_second_retrieval_of_one_option_set_lands_at_a_different_prefix() -> None:
    """A re-pull must never overwrite. `retrieved=` is what makes that structural."""
    first = _manifest()
    later = replace(first, retrieved_at=datetime(2026, 8, 21, 6, 0, tzinfo=UTC))
    assert first.prefix != later.prefix


def test_two_option_sets_for_one_window_land_at_different_prefixes() -> None:
    """A MANUFACTURING pull and a SOURCING pull are different datasets, not versions."""
    sourcing = dict(SALES_OPTIONS) | {"distributorView": "SOURCING"}
    assert _manifest().prefix != _manifest(options=sourcing).prefix


def test_a_differing_option_set_that_reorders_keys_hashes_the_same() -> None:
    """Key order is not part of the option set, so a reordered manifest is the same pull."""
    reordered: dict[str, object] = {
        "sellingProgram": "RETAIL",
        "reportPeriod": "DAY",
        "distributorView": "MANUFACTURING",
    }
    assert _manifest(options=reordered).prefix == _manifest().prefix


# ---------------------------------------------------------------------------
# Placement: the path as a checksum on the manifest
# ---------------------------------------------------------------------------


def test_a_manifest_at_its_own_derived_prefix_is_not_misplaced() -> None:
    landing = _landing()
    assert misplacement(landing.manifest, landing.key) is None


def test_a_manifest_moved_to_another_prefix_is_reported_with_both_prefixes() -> None:
    """The reason has to name both, or nobody can tell which half is wrong."""
    landing = _landing()
    reason = misplacement(landing.manifest, f"vendor_central/elsewhere/{MANIFEST_NAME}")
    assert reason is not None
    assert "vendor_central/elsewhere/" in reason
    assert landing.manifest.prefix in reason


def test_a_manifest_whose_window_was_edited_no_longer_matches_where_it_sits() -> None:
    """The check that earns the prefix its place: an edited manifest stops agreeing."""
    landing = _landing()
    edited = replace(landing.manifest, period_end=date(2026, 8, 23))
    assert misplacement(edited, landing.key) is not None


def test_a_key_that_is_not_a_marker_is_refused_rather_than_split() -> None:
    with pytest.raises(LandingError, match="not a manifest key"):
        delivery_prefix_of("vendor_central/GET_VENDOR_SALES_REPORT/data-0001.json")


def test_the_prefix_of_a_marker_key_drops_only_the_marker() -> None:
    assert delivery_prefix_of(f"a/b/{MANIFEST_NAME}") == "a/b/"


# ---------------------------------------------------------------------------
# The manifest refuses what cannot be verified
# ---------------------------------------------------------------------------


def test_a_hash_that_does_not_match_its_own_option_set_is_refused() -> None:
    """Catches a producer that hashed differently, which would split one pull into two."""
    with pytest.raises(LandingError, match="disagree"):
        Manifest(
            source_system="vendor_central",
            source_dataset="GET_VENDOR_SALES_REPORT",
            source_scope=SourceIdentifier("vendor_central.vendor_group", "fictional-vendor-group"),
            option_set=SALES_OPTIONS,
            option_set_hash="0000000000000000",
            marketplace="ATVPDKIKX0DER",
            period_start=WINDOW_START,
            period_end=WINDOW_END,
            source_watermark=WATERMARK,
            retrieved_at=RETRIEVED,
            declared_columns=("asin",),
            objects=(_object(),),
        )


def test_a_window_that_ends_before_it_begins_is_refused() -> None:
    with pytest.raises(LandingError, match="ends"):
        replace(_manifest(), period_start=WINDOW_END, period_end=WINDOW_START)


def test_a_naive_retrieval_instant_is_refused() -> None:
    with pytest.raises(LandingError, match="timezone-aware"):
        replace(_manifest(), retrieved_at=datetime(2026, 8, 20, 6, 30, 15))  # noqa: DTZ001


def test_a_subsecond_retrieval_instant_is_refused_before_key_rendering() -> None:
    with pytest.raises(LandingError, match="whole-second precision"):
        replace(_manifest(), retrieved_at=RETRIEVED.replace(microsecond=1))


def test_a_fractional_offset_retrieval_is_refused_after_utc_normalization() -> None:
    fractional_offset = timezone(timedelta(seconds=1, microseconds=500_000))
    retrieved = datetime(2026, 8, 20, 6, 30, 15, tzinfo=fractional_offset)
    assert retrieved.microsecond == 0
    assert retrieved.astimezone(UTC).microsecond == 500_000

    with pytest.raises(LandingError, match="whole-second precision"):
        replace(_manifest(), retrieved_at=retrieved)


def test_a_manifest_declaring_no_columns_is_refused() -> None:
    """A producer that cannot say what it wrote is one whose output cannot be validated."""
    with pytest.raises(LandingError, match="declared_columns"):
        replace(_manifest(), declared_columns=())


def test_a_manifest_describing_no_data_object_is_refused() -> None:
    with pytest.raises(LandingError, match="completion marker for nothing"):
        replace(_manifest(), objects=())


def test_a_marketplace_containing_a_separator_is_refused() -> None:
    """A slash would create a directory level the manifest's own prefix does not describe."""
    with pytest.raises(LandingError, match="path segment"):
        replace(_manifest(), marketplace="US/CA")


def test_an_empty_delivered_object_is_refused() -> None:
    """A report with no rows still has a header, so zero bytes is a failed extraction."""
    with pytest.raises(LandingError, match="failed extraction"):
        LandedObject(name="data-0001.json", byte_count=0, checksum="sha256:abc")


def test_an_object_with_no_checksum_is_refused() -> None:
    with pytest.raises(LandingError, match="checksum"):
        LandedObject(name="data-0001.json", byte_count=10, checksum="  ")


# ---------------------------------------------------------------------------
# Fan-out: one delivery, possibly two contracts
# ---------------------------------------------------------------------------


def test_one_dataset_resolves_to_both_contracts_that_read_it() -> None:
    """The property a contract-keyed prefix could not express."""
    detail = _contract(_Spec(contract_id="vendor-sales-by-asin"))
    total = _contract(_Spec(contract_id="vendor-sales-reported-total", grain=("date_key",)))
    resolved = contracts_for(_manifest(), (detail, total))
    assert tuple(c.contract_id for c in resolved.contracts) == (
        "vendor-sales-by-asin",
        "vendor-sales-reported-total",
    )


def test_an_unknown_legacy_dual_array_fanout_is_refused() -> None:
    first = _contract(_Spec(contract_id="vendor-sales-by-asin"))
    unexpected = _contract(_Spec(contract_id="vendor-sales-unexpected", grain=("date_key",)))

    with pytest.raises(LandingError, match="exact installed legacy document fan-out"):
        contracts_for(_manifest(), (first, unexpected))


def test_governed_flat_delivery_selects_its_exact_contract() -> None:
    fact = _contract(
        _Spec(
            contract_id="amc-dsp-impressions",
            dataset="dsp_impressions",
            system="amc",
            source_format=SourceFormat.CSV,
        )
    )
    campaign = _contract(
        _Spec(
            contract_id="amc-campaign-dsp",
            dataset="dsp_impressions",
            system="amc",
            source_format=SourceFormat.CSV,
        )
    )
    manifest = replace(
        _manifest(),
        source_system="amc",
        source_dataset="dsp_impressions",
        source_scope=SourceIdentifier("amc.entity", "fictional-amc-entity"),
        governance=_governance("amc-campaign-dsp"),
    )

    resolved = contracts_for(manifest, (fact, campaign))

    assert resolved.contracts == (campaign,)
    assert resolved.source_format is SourceFormat.CSV


def test_governed_manifest_requires_the_exact_installed_artifact_tuple() -> None:
    governance = _governance("amc-dsp-impressions")
    manifest = replace(_manifest(), governance=governance)

    require_installed_governance(manifest, (governance,))
    with pytest.raises(LandingError, match="artifacts differ"):
        require_installed_governance(
            manifest,
            (replace(governance, query_digest="sha256:" + "b" * 64),),
        )


def test_ungoverned_flat_delivery_matching_several_contracts_is_refused() -> None:
    contracts = tuple(
        _contract(
            _Spec(
                contract_id=contract_id,
                dataset="dsp_impressions",
                system="amc",
                source_format=SourceFormat.CSV,
            )
        )
        for contract_id in ("amc-dsp-impressions", "amc-campaign-dsp")
    )
    manifest = replace(
        _manifest(),
        source_system="amc",
        source_dataset="dsp_impressions",
        source_scope=SourceIdentifier("amc.entity", "fictional-amc-entity"),
    )

    with pytest.raises(LandingError, match="requires exact producer governance"):
        contracts_for(manifest, contracts)


def test_ungoverned_unique_amc_delivery_is_refused() -> None:
    contract = _contract(
        _Spec(
            contract_id="amc-dsp-clicks",
            dataset="dsp_clicks",
            system="amc",
            source_format=SourceFormat.CSV,
        )
    )
    manifest = replace(
        _manifest(),
        source_system="amc",
        source_dataset="dsp_clicks",
        source_scope=SourceIdentifier("amc.entity", "fictional-amc-entity"),
    )

    with pytest.raises(LandingError, match="requires exact producer governance"):
        contracts_for(manifest, (contract,))


def test_governed_contract_must_match_dataset_and_source_pack_identity() -> None:
    contract = _contract(
        _Spec(
            contract_id="amc-dsp-impressions",
            dataset="dsp_impressions",
            system="amc",
            source_format=SourceFormat.CSV,
        )
    )
    manifest = replace(
        _manifest(),
        source_system="amc",
        source_dataset="dsp_impressions",
        source_scope=SourceIdentifier("amc.entity", "fictional-amc-entity"),
        governance=_governance("amc-campaign-dsp"),
    )
    with pytest.raises(LandingError, match="does not uniquely read"):
        contracts_for(manifest, (contract,))

    mismatched_pack = replace(
        manifest,
        governance=_governance(
            "amc-dsp-impressions",
            source_pack_id="amc-campaign-dsp",
        ),
    )
    with pytest.raises(LandingError, match="identities disagree"):
        contracts_for(mismatched_pack, (contract,))


def test_resolution_carries_the_one_format_its_contracts_agree_on() -> None:
    resolved = contracts_for(_manifest(), (_contract(_Spec(contract_id="vendor-sales-by-asin")),))
    assert resolved.source_format is SourceFormat.JSON_DUAL_ARRAY


def test_a_dataset_no_contract_reads_is_a_rejected_landing() -> None:
    """Raises rather than quarantining: there is no partition to quarantine yet."""
    other = _contract(
        _Spec(contract_id="vendor-traffic-by-asin", dataset="GET_VENDOR_TRAFFIC_REPORT")
    )
    with pytest.raises(LandingError, match="no contract reads"):
        contracts_for(_manifest(), (other,))


def test_a_dataset_matching_only_under_a_different_source_system_is_refused() -> None:
    """A dataset name is Amazon's, so two sources choosing one name must not collide."""
    impostor = _contract(_Spec(contract_id="amc-something", system="amc"))
    with pytest.raises(LandingError, match="no contract reads"):
        contracts_for(_manifest(), (impostor,))


def test_contracts_disagreeing_about_the_format_of_one_object_are_refused() -> None:
    """One delivered object cannot be parsed two ways, so this is a contract defect."""
    detail = _contract(_Spec(contract_id="vendor-sales-by-asin"))
    total = _contract(
        _Spec(
            contract_id="vendor-sales-reported-total",
            source_format=SourceFormat.CSV,
            grain=("date_key",),
        )
    )
    with pytest.raises(LandingError, match="two ways"):
        contracts_for(_manifest(), (detail, total))


def test_manifest_source_scope_round_trips_and_separates_prefixes() -> None:
    manifest = _manifest()
    assert read_manifest(write_manifest(manifest)) == manifest
    other = replace(
        manifest,
        source_scope=SourceIdentifier(
            "vendor_central.vendor_group", "fictional-other-vendor-group"
        ),
    )
    assert other.prefix != manifest.prefix
    assert misplacement(other, manifest.manifest_key) is not None


def test_manifest_parser_refuses_missing_blank_or_extended_source_scope() -> None:
    baseline = json.loads(write_manifest(_manifest()))

    missing = dict(baseline)
    missing.pop("source_scope")
    with pytest.raises(LandingError, match="source_scope"):
        read_manifest(json.dumps(missing))

    blank = json.loads(json.dumps(baseline))
    blank["source_scope"]["value"] = " "
    with pytest.raises(LandingError, match="source_scope"):
        read_manifest(json.dumps(blank))

    extended = json.loads(json.dumps(baseline))
    extended["source_scope"]["unexpected"] = "value"
    with pytest.raises(LandingError, match="source_scope"):
        read_manifest(json.dumps(extended))
