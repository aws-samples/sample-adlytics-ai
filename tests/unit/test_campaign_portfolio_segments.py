# SPDX-License-Identifier: MIT-0
"""Campaigns segment pages: ordering each population once yields the base bake's exact bytes.

A segment page is a 50-row slice of one sorted ordering. The bake used to re-sort the whole
population for every page; it now sorts each ordering once and slices it. A failure here means a
served page would reorder, duplicate or lose a campaign row against what the bake wrote before,
so the expected bytes come from a verbatim copy of the base algorithm (a96eab71), never from the
changed seam.
"""

# ruff: noqa: PLR0913, PLR0917
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from campaign_estate_support import EstateQueries, estate, render_input, renderer
from campaign_portfolio_support import complete_portfolio, portfolio_row

from adapters.campaign_measurement import CampaignMeasurementSelection
from adapters.jobs.preset_artifact_publisher.campaign_source import (
    CAMPAIGN_PRESET_RENDER_FORMAT,
    RetainedCampaignPresetArtifactSource,
    _CampaignFilterPublication,  # pyright: ignore[reportPrivateUsage]
    _CampaignSiblingPairs,  # pyright: ignore[reportPrivateUsage]
)
from adapters.jobs.preset_artifact_publisher.isolated_refusals import capped_preset_artifact_json
from domain.presentation.campaign_attribution import (
    CampaignListEvidence,
    CampaignNumber,
    CampaignSelectionCapability,
    SafeCampaignDisplaySignature,
)
from domain.presentation.campaign_custody import (
    CampaignCustodyManifest,
    CampaignCustodyRow,
    campaign_custody_manifest_payload,
)
from domain.presentation.campaign_portfolio import (
    CampaignPortfolioArtifact,
    CampaignPortfolioComparison,
    CampaignPortfolioInsufficiencyReason,
    CampaignPortfolioMetric,
    CampaignPortfolioOutcome,
    CampaignPortfolioPagination,
    CampaignPortfolioRow,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
    build_campaign_portfolio,
    campaign_portfolio_artifact_payload,
    campaign_portfolio_segment_payload,
    campaign_portfolio_segments,
    campaign_roster_shard_count,
    campaign_roster_shard_identity,
    campaign_roster_shard_payload,
    order_campaign_portfolio_rows,
)
from domain.presentation.campaigns import EffectiveNameStatus
from domain.presentation.preset_artifacts import (
    PresetArtifactFilterIdentity,
    PresetArtifactPage,
    PresetArtifactRevision,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingIdentity,
    PresetArtifactSiblingPair,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
)
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-071", "ADR-145")]

PERIOD = DateRange(date(2026, 7, 23), date(2026, 8, 21))
EXPIRY = datetime.max.replace(tzinfo=UTC)
SORTS = tuple(
    CampaignPortfolioSort(field, direction)
    for field in CampaignPortfolioSortField
    for direction in ("asc", "desc")
)


# --- The base algorithm, verbatim (a96eab71) -----------------------------------------------


def _base_campaign_portfolio_segment(
    artifact: CampaignPortfolioArtifact,
    sort: CampaignPortfolioSort,
    segment_index: int,
) -> CampaignPortfolioArtifact | None:
    """Verbatim copy of a96eab71 src/domain/presentation/campaign_portfolio.py:750-784."""
    if artifact.outcome is not CampaignPortfolioOutcome.COMPLETE:
        return replace(artifact, sort=sort) if segment_index == 0 else None
    pagination = artifact.pagination
    if pagination is None:
        raise ValueError("a complete campaign portfolio requires its pagination record.")
    if len(artifact.rows) != artifact.total_count:
        raise ValueError("campaign portfolio segments require the complete retained population.")
    if not 0 <= segment_index < pagination.segment_count:
        return None
    ordered = order_campaign_portfolio_rows(artifact.rows, sort)
    start = segment_index * pagination.segment_size
    return replace(
        artifact,
        sort=sort,
        rows=ordered[start : start + pagination.segment_size],
        pagination=CampaignPortfolioPagination(
            segment_index,
            pagination.segment_size,
            pagination.segment_count,
            pagination.total_count,
        ),
    )


def _base_custody_rows(
    portfolio: CampaignPortfolioArtifact,
    by_signature: dict[SafeCampaignDisplaySignature, CampaignMeasurementSelection],
) -> tuple[CampaignCustodyRow, ...] | None:
    """Verbatim copy of a96eab71 campaign_source.py:514-532 (`_custody_rows`)."""
    rows: list[CampaignCustodyRow] = []
    for row in portfolio.rows:
        selection = by_signature.get(row.signature)
        if selection is None:
            return None
        rows.append(
            CampaignCustodyRow(
                selection.campaign,
                selection.channel,
                selection.marketplace,
                row.signature,
            )
        )
    return tuple(rows)


def _base_pair(
    pairs: _CampaignSiblingPairs,
    identity: PresetArtifactSiblingIdentity | PresetArtifactFilterIdentity,
    payload_json: str,
    rows: tuple[CampaignCustodyRow, ...],
) -> PresetArtifactSiblingPair:
    """Verbatim copy of a96eab71 campaign_source.py:285-303 (`_CampaignSiblingPairs._pair`)."""
    body = PresetArtifactSiblingBody(
        pairs.page, pairs.variant, pairs.revision, identity, payload_json
    )
    custody = PresetArtifactSiblingCustody(
        pairs.page,
        pairs.variant,
        pairs.revision,
        identity,
        capped_preset_artifact_json(
            campaign_custody_manifest_payload(CampaignCustodyManifest(body.digest, rows))
        ),
    )
    return body, custody


def _base_segment_pairs(
    pairs: _CampaignSiblingPairs, population: CampaignPortfolioArtifact
) -> Iterator[PresetArtifactSiblingPair]:
    """Verbatim reproduction of a96eab71 campaign_source.py:255-283 (`_segments`)."""
    if population.pagination is None:
        return
    for sort_field in CampaignPortfolioSortField:
        for direction in ("asc", "desc"):
            sort = CampaignPortfolioSort(sort_field, direction)
            for segment_index in range(population.pagination.segment_count):
                if (
                    sort_field is CampaignPortfolioSortField.SPEND
                    and direction == "desc"
                    and segment_index == 0
                ):
                    continue
                artifact = _base_campaign_portfolio_segment(population, sort, segment_index)
                if artifact is None:
                    raise ValueError("declared campaign segment is unavailable.")
                rows = _base_custody_rows(artifact, pairs.by_signature)
                if rows is None:
                    raise ValueError("campaign segment custody is unavailable.")
                yield _base_pair(
                    pairs,
                    PresetArtifactSiblingIdentity(
                        f"{sort_field.value}-{direction}", segment_index, population.filter_id
                    ),
                    capped_preset_artifact_json(campaign_portfolio_segment_payload(artifact)),
                    rows,
                )


def _base_stream(pairs: _CampaignSiblingPairs) -> Iterator[PresetArtifactSiblingPair]:
    """Verbatim reproduction of a96eab71 campaign_source.py:230-244 (`__call__`)."""
    for publication in pairs.filters:
        yield from _base_segment_pairs(pairs, publication.population)
        for shard in range(campaign_roster_shard_count(len(publication.population.roster))):
            yield _base_pair(
                pairs,
                campaign_roster_shard_identity(publication.filter_id, shard),
                capped_preset_artifact_json(
                    campaign_roster_shard_payload(
                        publication.population.roster, shard, pairs.revision.value
                    )
                ),
                (),
            )
    for publication in pairs.filters[1:]:
        identity = PresetArtifactFilterIdentity(publication.filter_id)
        yield _base_pair(pairs, identity, publication.payload_json, publication.custody_rows)


# --- Populations the comparator distinguishes ----------------------------------------------


EDGE_NAMES: tuple[str | None, ...] = (
    "alpha",
    "Alpha",
    "ALPHA",
    "Straße",
    "STRASSE",
    "strasse",
    "\u00e9clair",  # NFC
    "e\u0301clair",  # NFD: casefold does not merge it with the NFC form
    "Campaign 2",
    "Campaign 10",
    None,
    "Shared name",
)


def _signature(index: int, name: str | None, channel: str) -> SafeCampaignDisplaySignature:
    if name is None:
        # An unnamed campaign is told apart only by its channel, never by a fallback label.
        return SafeCampaignDisplaySignature(
            effective_name=None,
            channel=f"{channel} {index:04d}",
            marketplace="United States",
            platform="Amazon Ads",
            effective_name_status=EffectiveNameStatus.MISSING,
        )
    return SafeCampaignDisplaySignature(
        effective_name=name,
        channel=channel,
        marketplace="United States",
        platform="Amazon Ads",
        effective_name_status=EffectiveNameStatus.AVAILABLE,
        # Equal names stay unique through their validity, so `sort_key` breaks the tie.
        effective_name_valid_from=date(2026, 1, 1) + timedelta(days=index),
    )


def _undefined_roas(row: CampaignPortfolioRow) -> CampaignPortfolioRow:
    """Return the row with its current ROAS typed absent (ADR-087), as test_campaign_portfolio."""
    comparisons = list(row.comparisons)
    index = next(
        position
        for position, item in enumerate(comparisons)
        if item.metric is CampaignPortfolioMetric.ATTRIBUTED_ROAS
    )
    roas = comparisons[index]
    comparisons[index] = CampaignPortfolioComparison(
        roas.metric, replace(roas.current, value=None), roas.prior, None, None
    )
    return replace(row, comparisons=tuple(comparisons))


def _row(
    index: int, name: str | None, spend: CampaignNumber, roas: Decimal | None, channel: str
) -> CampaignPortfolioRow:
    row = portfolio_row(
        _signature(index, name, channel),
        CampaignSelectionCapability(f"baked:{index:06d}", EXPIRY),
        PERIOD,
        spend,
        roas=(Decimal("2") if roas is None else roas, Decimal("2")),
    )
    return _undefined_roas(row) if roas is None else row


def _tied_spend(index: int) -> CampaignNumber:
    """Seven spend values, zero included, alternating int and an equal Decimal (`3` vs `3.0`)."""
    value = index % 7
    return value if index % 2 else Decimal(f"{value}.0")


def _tied_roas(index: int) -> Decimal | None:
    """Three ROAS values, `2` and `2.0` equal, and every fourth row undefined."""
    return None if index % 4 == 3 else (Decimal("1.5"), Decimal("2"), Decimal("2.0"))[index % 3]


def _ties(count: int) -> CampaignPortfolioArtifact:
    """Heavy ties on every metric; purchases and CTR tie across the whole population."""
    return complete_portfolio(
        PERIOD,
        tuple(
            _row(index, f"Fictional Tie {index % 10}", _tied_spend(index), _tied_roas(index), "A")
            for index in range(count)
        ),
    )


def _straddle() -> CampaignPortfolioArtifact:
    """Tie rows 40-69 of 100 on spend and ROAS, between 40 lower and 30 higher spends.

    The tie group then sits at positions 40-69 ascending and 30-59 descending, so in both
    directions the page boundary at 50 splits it and `sort_key` alone places the split.
    """
    return complete_portfolio(
        PERIOD,
        tuple(
            _row(
                index,
                f"Fictional Straddle {index:03d}",
                index if index < 40 else Decimal("39.5") if index < 70 else 1_000 + index,
                Decimal("2") if 40 <= index < 70 else Decimal(100 + index),
                "A",
            )
            for index in range(100)
        ),
    )


def _names() -> CampaignPortfolioArtifact:
    """Cycle the casefold, Unicode, numeric-text and unnamed edge names over 105 rows.

    Two channels, so equal names and the unnamed tail (rows 97-104) cross page boundaries.
    """
    return complete_portfolio(
        PERIOD,
        tuple(
            _row(
                index,
                EDGE_NAMES[index % len(EDGE_NAMES)],
                _tied_spend(index),
                _tied_roas(index),
                ("A", "B")[index % 2],
            )
            for index in range(105)
        ),
    )


def _encoded(segments: list[CampaignPortfolioArtifact]) -> list[bytes]:
    return [
        canonical_preset_artifact_json(campaign_portfolio_segment_payload(segment)).encode()
        for segment in segments
    ]


@pytest.mark.parametrize(
    ("population", "segments"),
    [
        pytest.param(lambda: _ties(1), 1, id="1"),
        pytest.param(lambda: _ties(49), 1, id="49"),
        pytest.param(lambda: _ties(50), 1, id="50-exactly-one-page"),
        pytest.param(lambda: _ties(51), 2, id="51-one-row-tail"),
        pytest.param(lambda: _ties(101), 3, id="101"),
        pytest.param(lambda: _ties(400), 8, id="400"),
        pytest.param(_straddle, 2, id="tie-group-across-a-page-boundary"),
        pytest.param(_names, 3, id="name-edge-cases"),
    ],
)
def test_each_ordering_sorted_once_yields_the_base_segments_byte_for_byte(
    population: Callable[[], CampaignPortfolioArtifact], segments: int
) -> None:
    artifact = population()
    for sort in SORTS:
        drawn = list(campaign_portfolio_segments(artifact, sort))
        base = [
            _base_campaign_portfolio_segment(artifact, sort, index) for index in range(segments)
        ]
        assert _base_campaign_portfolio_segment(artifact, sort, segments) is None
        assert all(item is not None for item in base)
        expected = [item for item in base if item is not None]
        assert len(drawn) == segments, sort
        assert _encoded(drawn) == _encoded(expected), sort
        assert drawn == expected, sort


def _insufficient() -> CampaignPortfolioArtifact:
    return build_campaign_portfolio(
        period=PERIOD,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=(),
        totals=(),
        trend=None,
        evidence=(CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
        reason_codes=(CampaignPortfolioInsufficiencyReason.MISSING_CATALOG,),
    )


def _empty() -> CampaignPortfolioArtifact:
    return build_campaign_portfolio(
        period=PERIOD,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=(),
        totals=(),
        trend=None,
        evidence=(CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
    )


@pytest.mark.parametrize("population", [_empty, _insufficient], ids=["empty", "insufficient"])
def test_a_portfolio_without_rows_yields_the_base_single_artifact(
    population: Callable[[], CampaignPortfolioArtifact],
) -> None:
    artifact = population()
    assert artifact.outcome is not CampaignPortfolioOutcome.COMPLETE
    for sort in SORTS:
        drawn = list(campaign_portfolio_segments(artifact, sort))
        expected = _base_campaign_portfolio_segment(artifact, sort, 0)
        assert expected is not None
        assert _base_campaign_portfolio_segment(artifact, sort, 1) is None
        assert drawn == [expected], sort
        assert [
            canonical_preset_artifact_json(campaign_portfolio_artifact_payload(item)).encode()
            for item in drawn
        ] == [
            canonical_preset_artifact_json(campaign_portfolio_artifact_payload(expected)).encode()
        ]


# --- The real sibling stream over a measured estate ----------------------------------------


def _pairs(
    selections: tuple[CampaignMeasurementSelection, ...],
) -> _CampaignSiblingPairs:
    """Build the 30d stream `publication_for` builds, from the real renderer over an estate."""
    rendered = renderer(EstateQueries(selections)).render_portfolio(render_input(selections))
    assert rendered.portfolio is not None
    revision = PresetArtifactRevision.from_publication_identities(
        rendered.publication_identities, render_format=CAMPAIGN_PRESET_RENDER_FORMAT
    )
    by_signature = {selection.signature: selection for selection in selections}
    filters: list[_CampaignFilterPublication] = []
    for artifact in (rendered.portfolio, *rendered.siblings):
        publication = RetainedCampaignPresetArtifactSource._filter_publication(  # pyright: ignore[reportPrivateUsage]
            artifact, by_signature, revision
        )
        assert publication is not None
        filters.append(publication)
    return _CampaignSiblingPairs(
        PresetArtifactPage("campaigns"),
        PresetArtifactVariant("30d"),
        revision,
        tuple(filters),
        by_signature,
    )


def _bytes(pair: PresetArtifactSiblingPair) -> tuple[str, bytes, str, bytes]:
    body, custody = pair
    return body.key, body.payload_json.encode(), custody.key, custody.payload_json.encode()


@pytest.mark.parametrize(
    ("campaigns", "filters", "pairs"),
    [
        pytest.param(1, 2, 21, id="1"),
        # All, Sponsored and five channels: 10 orderings x ceil(n/50) segments less the
        # canonical one, plus roster shards (500 each), per filter; then six filter siblings.
        pytest.param(49, 7, 76, id="49"),
        pytest.param(50, 7, 76, id="50"),
        pytest.param(51, 7, 86, id="51"),
        pytest.param(400, 7, 256, id="400"),
        pytest.param(1_000, 7, 568, id="1000"),
    ],
)
def test_the_campaign_sibling_stream_matches_the_base_stream_byte_for_byte(
    campaigns: int, filters: int, pairs: int
) -> None:
    """Cover every pair the stage writes, in stream order, not only the segments.

    Roster shards and filter siblings sit between and after the segments, so a lost or extra
    segment shifts every later identity and the strict zip refuses it.
    """
    subject = _pairs(estate(campaigns))
    assert len(subject.filters) == filters
    stream = subject.stream()
    assert stream.count == pairs
    drawn = 0
    for actual, expected in zip(stream, _base_stream(subject), strict=True):
        assert _bytes(actual) == _bytes(expected)
        drawn += 1
    assert drawn == pairs
