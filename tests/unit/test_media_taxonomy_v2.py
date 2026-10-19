# SPDX-License-Identifier: MIT-0
"""Media taxonomy version 2: the `amazon_ads` media profile and `exposure_source_sets`.

Business-outcomes slice S0.10 (ADR-099, ADR-101, C-16). Packs emit source-set labels; the one
taxonomy resolves them to the two disjoint grouping axes. The loader takes version 2 only, with
an exact key set, so a refusal here is a load failure, never a silent default.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import yaml

from adapters.outbound.media_taxonomy import TAXONOMY_PATH, load_media_taxonomy
from domain.semantics.identity import (
    AcquisitionChannelGroupKey,
    EcosystemKey,
    ExposureGroupKey,
    PlatformKey,
    SourceIdentifier,
)
from domain.semantics.media_taxonomy import (
    MediaTaxonomy,
    ResolveExposureSourceSetRequest,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-099", "ADR-101")]

ROOT = Path(__file__).resolve().parents[2]
AMAZON_ADS = PlatformKey("amazon_ads")
ON = date(2026, 10, 8)


@pytest.fixture(scope="module")
def taxonomy() -> MediaTaxonomy:
    return load_media_taxonomy(ROOT)


def _write_variant(tmp_path: Path, edit: str) -> Path:
    """Copy the real taxonomy into a scratch root with one top-level edit applied."""
    target = tmp_path / TAXONOMY_PATH
    target.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / TAXONOMY_PATH, target)
    document = yaml.safe_load(target.read_text())
    if edit == "version-1":
        document["version"] = 1
    elif edit == "no-source-sets":
        del document["exposure_source_sets"]
    elif edit == "undeclared-platform":
        document["exposure_source_sets"][0]["platform"] = "google_ads"
    target.write_text(yaml.safe_dump(document, sort_keys=False))
    return tmp_path


def test_the_shipped_taxonomy_is_version_2(taxonomy: MediaTaxonomy) -> None:
    assert taxonomy.version == 2


def test_the_amazon_ads_profile_resolves_platform_with_no_media_split(
    taxonomy: MediaTaxonomy,
) -> None:
    resolved = taxonomy.resolve(SourceIdentifier("adlytics.media_profile", "amazon_ads"), ON)

    assert (resolved.ecosystem, resolved.platform) == (EcosystemKey("amazon"), AMAZON_ADS)
    assert (resolved.channel_group, resolved.channel, resolved.ad_product) == (None, None, None)


@pytest.mark.parametrize(
    ("source_set", "key"),
    (
        ("dsp", "dsp_only"),
        ("sponsored", "sponsored_only"),
        ("dsp+sponsored", "dsp_and_sponsored"),
    ),
)
def test_each_amazon_source_set_resolves_on_both_grouping_axes(
    taxonomy: MediaTaxonomy, source_set: str, key: str
) -> None:
    mapping = taxonomy.resolve_exposure_source_set(
        ResolveExposureSourceSetRequest(AMAZON_ADS, source_set, ON)
    )

    assert mapping.exposure_group == ExposureGroupKey(key)
    assert mapping.acquisition_channel_group == AcquisitionChannelGroupKey(key)


def test_an_undeclared_source_set_is_refused_rather_than_guessed(taxonomy: MediaTaxonomy) -> None:
    with pytest.raises(ValueError, match=r"amazon_ads:sponsored\+dsp has no exposure source set"):
        taxonomy.resolve_exposure_source_set(
            ResolveExposureSourceSetRequest(AMAZON_ADS, "sponsored+dsp", ON)
        )


def test_a_source_set_on_another_platform_is_refused(taxonomy: MediaTaxonomy) -> None:
    with pytest.raises(ValueError, match="amazon_retail:dsp has no exposure source set"):
        taxonomy.resolve_exposure_source_set(
            ResolveExposureSourceSetRequest(PlatformKey("amazon_retail"), "dsp", ON)
        )


@pytest.mark.parametrize(
    ("edit", "message"),
    (
        ("version-1", r"at version 2\."),
        ("no-source-sets", r"requires exactly \[.*'exposure_source_sets'.*\] at version 2"),
        ("undeclared-platform", "exposure source set names undeclared platform 'google_ads'"),
    ),
)
def test_the_loader_refuses_a_file_that_is_not_a_complete_version_2(
    tmp_path: Path, edit: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_media_taxonomy(_write_variant(tmp_path, edit))


def test_an_unedited_copy_loads_so_the_refusals_above_are_the_edit(tmp_path: Path) -> None:
    """Paired control for the loader refusals: the scratch copy itself is valid."""
    assert load_media_taxonomy(_write_variant(tmp_path, "none")).version == 2


def test_two_source_sets_sharing_an_exposure_group_are_refused(taxonomy: MediaTaxonomy) -> None:
    """One group per source set keeps the axis disjoint (ADR-101 D-3)."""
    dsp, sponsored, both = taxonomy.exposure_source_sets
    merged = replace(both, exposure_group=dsp.exposure_group)

    with pytest.raises(ValueError, match="exposure group dsp_only is shared by 'dsp' and"):
        replace(taxonomy, exposure_source_sets=(dsp, sponsored, merged))


def test_a_source_set_mapped_twice_at_overlapping_dates_is_refused(
    taxonomy: MediaTaxonomy,
) -> None:
    dsp = taxonomy.exposure_source_sets[0]

    with pytest.raises(ValueError, match="exposure source set 'dsp' maps twice"):
        replace(taxonomy, exposure_source_sets=(dsp, replace(dsp, mapping_version=2)))


def test_a_successor_mapping_after_the_first_ends_is_accepted(taxonomy: MediaTaxonomy) -> None:
    """Paired control: effective dating, not repetition, is what the rule refuses."""
    dsp = taxonomy.exposure_source_sets[0]
    ended = replace(dsp, valid_to=date(2026, 1, 1))
    successor = replace(dsp, mapping_version=2, valid_from=date(2026, 1, 1))

    revised = replace(taxonomy, exposure_source_sets=(ended, successor))

    resolved = revised.resolve_exposure_source_set(
        ResolveExposureSourceSetRequest(AMAZON_ADS, "dsp", ON)
    )
    assert resolved.mapping_version == 2
