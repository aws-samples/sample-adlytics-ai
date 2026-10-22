# SPDX-License-Identifier: MIT-0
# ruff: noqa: PLR0913
"""Retained campaign preset baking: population scale, revision custody, and body-first writes."""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from campaign_portfolio_support import all_channels_portfolio, portfolio_row
from preset_artifact_support import staged_custody, staged_index, staged_pointer

from adapters.campaign_artifact_renderer import (
    CampaignArtifactRenderInput,
    CampaignPortfolioRenderResult,
)
from adapters.config import PresetArtifactBakeSettings
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher.campaign_source import (
    CAMPAIGN_PRESET_RENDER_FORMAT,
    CampaignPortfolioCompiler,
    RetainedCampaignPresetArtifactSource,
    retained_campaign_preset_artifact_source,
)
from adapters.outbound.preset_artifacts import PresetArtifactPersistence
from domain.presentation.analytical_pages import PresentationDimensionLabel
from domain.presentation.campaign_custody import (
    campaign_custody_manifest_from_payload,
    campaign_custody_manifest_payload,
)
from domain.presentation.campaign_portfolio import (
    CAMPAIGN_PORTFOLIO_MAX_ROWS,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
    campaign_portfolio_artifact_from_payload,
)
from domain.presentation.preset_artifacts import (
    MAX_PRESET_ARTIFACT_BYTES,
    ArtifactBodyWriteResult,
    CustodyManifestWriteResult,
    PresetArtifactBody,
    PresetArtifactCustodyHeader,
    PresetArtifactCustodyManifest,
    PresetArtifactIndexShardIdentity,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceOutcome,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactPointerAdvanceResult,
    PresetArtifactPointerRosterRequest,
    PresetArtifactPointerRosterResult,
    PresetArtifactPointerTemplate,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingCustodyWriteResult,
    PresetArtifactSiblingIdentity,
    PresetArtifactSiblingReadRequest,
    PresetArtifactSiblingWriteResult,
    PresetArtifactVariant,
    SourceUnchanged,
    canonical_preset_artifact_json,
    index_shard_descriptors,
    split_parent_custody,
)
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EffectiveLabel,
    EntityAlias,
    EntityKind,
    MappingEvidence,
)
from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
    SourceIdentifier,
)
from domain.shared_kernel.errors import InvariantError
from domain.shared_kernel.values import DateRange
from ports.outbound.entity_catalog import (
    AnalyticalEntityCatalogPort,
    EntityCatalogBrowseRequest,
    EntityCatalogBrowseResult,
)
from ports.outbound.preset_artifacts import PresetArtifactPointerStorePort
from ports.outbound.query import QueryRequest, QueryResult
from ports.outbound.source_coverage import (
    SourceCoverageBounds,
    SourceCoverageRequest,
    SourceCoverageRequirement,
    SourceCoverageResult,
)

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
CAMPAIGN_POLICY = next(
    schema for schema in load_v2_page_schemas(ROOT).pages if schema.page_id == "campaigns"
).temporal_policy
PAGE = PresetArtifactPage("campaigns")
VARIANT = PresetArtifactVariant("7d")
NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
SCOPE = CommerceScopeKey("amazon_retail_us")
PLATFORM = PlatformKey("amazon_ads")
CHANNEL = ChannelKey("amazon_dsp")
MARKETPLACE = MarketplaceKey("amazon_us")
LABELS = (
    PresentationDimensionLabel("platform_key", PLATFORM.value, "platform", "Amazon Ads"),
    PresentationDimensionLabel("channel_key", CHANNEL.value, "channel", "Amazon DSP"),
    PresentationDimensionLabel(
        "marketplace_key", MARKETPLACE.value, "marketplace", "United States"
    ),
)
COVERAGE = (DateRange(date(2026, 8, 1), date(2026, 8, 21)),)
REQUIREMENT = SourceCoverageRequirement((("amc", "dsp-impressions"),))


def _alias(index: int) -> EntityAlias:
    campaign = CampaignKey(f"campaign:{index:03d}", SCOPE, PLATFORM)
    valid_from = date(2026, 1, 1)
    return EntityAlias(
        kind=EntityKind.CAMPAIGN,
        key=campaign,
        commerce_scope=SCOPE,
        labels=(EffectiveLabel(f"Fictional Campaign {index:03d}", valid_from, None),),
        source_identifiers=(SourceIdentifier("fictional.campaign", campaign.value),),
        evidence=MappingEvidence("fictional-preset-source", 1),
        valid_from=valid_from,
        valid_to=None,
        platform=PLATFORM,
        channel=CHANNEL,
        marketplace=MARKETPLACE,
    )


@dataclass
class Coverage:
    intervals: tuple[DateRange, ...] = COVERAGE
    calls: list[SourceCoverageRequest] = field(default_factory=list[SourceCoverageRequest])

    def source_coverage(self, request: SourceCoverageRequest) -> SourceCoverageResult:
        self.calls.append(request)
        return SourceCoverageResult(
            contributors=(),
            bounds=SourceCoverageBounds(
                intervals=self.intervals,
                earliest_start=None if not self.intervals else self.intervals[0].start,
                latest_end=None if not self.intervals else self.intervals[-1].end,
            ),
            latest_loaded_at=None,
        )


@dataclass
class Catalog:
    result: EntityCatalogBrowseResult
    calls: list[EntityCatalogBrowseRequest] = field(
        default_factory=list[EntityCatalogBrowseRequest]
    )

    def browse(self, request: EntityCatalogBrowseRequest) -> EntityCatalogBrowseResult:
        self.calls.append(request)
        return self.result


@dataclass
class Compiler:
    identities: tuple[str, ...] = ("publication-b", "publication-a")
    unavailable: bool = False
    received: list[CampaignArtifactRenderInput] = field(
        default_factory=list[CampaignArtifactRenderInput]
    )

    def render_portfolio(
        self, request: CampaignArtifactRenderInput
    ) -> CampaignPortfolioRenderResult:
        self.received.append(request)
        if self.unavailable:
            return CampaignPortfolioRenderResult(None, ())
        rows = tuple(
            portfolio_row(
                selection.signature,
                capability,
                request.period,
                Decimal(len(request.selections) - index),
            )
            for index, (selection, capability) in enumerate(
                zip(request.selections, request.capabilities, strict=True)
            )
        )
        return CampaignPortfolioRenderResult(
            all_channels_portfolio(request.period, rows, authorized_count=len(request.selections)),
            self.identities,
        )


@dataclass
class Pointers:
    pointer: PresetArtifactPointer | None = None
    reads: list[tuple[PresetArtifactPage, PresetArtifactVariant]] = field(
        default_factory=list[tuple[PresetArtifactPage, PresetArtifactVariant]]
    )

    def current(
        self, page: PresetArtifactPage, variant: PresetArtifactVariant
    ) -> PresetArtifactPointer | None:
        self.reads.append((page, variant))
        return self.pointer

    def roster(
        self, request: PresetArtifactPointerRosterRequest
    ) -> PresetArtifactPointerRosterResult:
        return PresetArtifactPointerRosterResult(
            request,
            tuple(self.current(request.page, variant) for variant in request.variants),
        )

    def advance(
        self, request: PresetArtifactPointerAdvanceRequest
    ) -> PresetArtifactPointerAdvanceResult:
        del request
        raise AssertionError("the campaign source must never advance a pointer itself")


def _source(
    *,
    population: int = 3,
    truncated: bool = False,
    compiler: Compiler | None = None,
    pointers: Pointers | None = None,
) -> tuple[RetainedCampaignPresetArtifactSource, Catalog, Compiler, Pointers]:
    aliases = tuple(_alias(index + 1) for index in range(population))
    catalog = Catalog(
        EntityCatalogBrowseResult(
            aliases,
            truncated=truncated,
            authorized_count=population + 1 if truncated else population,
        )
    )
    resolved_compiler = compiler or Compiler()
    resolved_pointers = pointers or Pointers()
    source = RetainedCampaignPresetArtifactSource(
        coverage=Coverage(),
        coverage_requirements=(REQUIREMENT,),
        temporal_policy=CAMPAIGN_POLICY,
        catalog=cast("AnalyticalEntityCatalogPort", catalog),
        scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
        compiler=cast("CampaignPortfolioCompiler", resolved_compiler),
        pointers=cast("PresetArtifactPointerStorePort", resolved_pointers),
        clock=lambda: NOW,
        labels=LABELS,
    )
    return source, catalog, resolved_compiler, resolved_pointers


@pytest.mark.covers("ADR-070")
def test_baker_measures_full_population_and_emits_boundless_pagination() -> None:
    """Rank, don't refuse: totals cover the population; rows are one segment of it (ADR-071)."""
    population = CAMPAIGN_PORTFOLIO_MAX_ROWS + 11
    source, _, compiler, _ = _source(population=population)

    request = source.publication_for(VARIANT)

    assert isinstance(request, PresetArtifactPublicationRequest)
    assert len(compiler.received) == 1
    assert len(compiler.received[0].selections) == population
    artifact = campaign_portfolio_artifact_from_payload(json.loads(request.body.payload_json))
    assert artifact.pagination is not None
    assert artifact.pagination.segment_size == CAMPAIGN_PORTFOLIO_MAX_ROWS
    assert artifact.pagination.segment_index == 0
    assert artifact.pagination.segment_count == 2
    assert artifact.pagination.total_count == population
    assert artifact.total_count == population
    assert len(artifact.rows) == CAMPAIGN_PORTFOLIO_MAX_ROWS
    assert len(artifact.roster) == population
    assert request.siblings.count == len(tuple(request.siblings)) == 19
    # The pointer binds All channels, whose totals add up across channels (ADR-127).
    assert tuple(comparison.metric.value for comparison in artifact.totals) == (
        "spend",
        "impressions",
    )
    assert all(
        comparison.current.grain.dimensions == ("period", "marketplace")
        for comparison in artifact.totals
    )


def test_baker_refuses_a_population_beyond_the_declared_measurable_bound() -> None:
    source, catalog, compiler, _ = _source(population=3, truncated=True)

    assert source.publication_for(VARIANT) is None
    assert compiler.received == []
    assert len(catalog.calls) == 1


@pytest.mark.covers("ADR-070")
def test_revision_derives_from_the_exact_publication_identities_used() -> None:
    source, _, _, _ = _source(compiler=Compiler(identities=("publication-b", "publication-a")))

    request = source.publication_for(VARIANT)

    assert isinstance(request, PresetArtifactPublicationRequest)
    pointer = request.pointer_template.complete(None).pointer
    assert pointer.revision == PresetArtifactRevision.from_publication_identities(
        ("publication-a", "publication-b"),
        render_format=CAMPAIGN_PRESET_RENDER_FORMAT,
    )
    assert pointer.forward_token == 1
    assert pointer.supersedes is None
    # ADR-116 decision 4: the pointer names the window its body serves and its equal prior.
    body_payload = json.loads(request.body.payload_json)
    assert pointer.resolved_window is not None
    assert pointer.resolved_window.start.isoformat() == body_payload["period"]["start"]
    assert pointer.resolved_window.end.isoformat() == body_payload["period"]["end"]
    assert pointer.comparison_window is not None
    assert pointer.comparison_window.end < pointer.resolved_window.start
    assert request.pointer_template.superseded_history_expires_at is None


@pytest.mark.covers("ADR-070")
def test_a_new_revision_supersedes_the_current_pointer_with_a_history_expiry() -> None:
    source, _, _, _ = _source()
    first = source.publication_for(VARIANT)
    assert isinstance(first, PresetArtifactPublicationRequest)
    current = first.pointer_template.complete(None).pointer

    source_two, _, _, _ = _source(
        compiler=Compiler(identities=("publication-c",)),
        pointers=Pointers(pointer=current),
    )
    request = source_two.publication_for(VARIANT)

    assert isinstance(request, PresetArtifactPublicationRequest)
    pointer = request.pointer_template.complete(None).pointer
    assert pointer.supersedes == current.revision
    assert pointer.forward_token == current.forward_token + 1
    assert request.pointer_template.superseded_history_expires_at is not None
    assert request.pointer_template.superseded_history_expires_at > NOW


def test_an_unchanged_revision_is_not_republished() -> None:
    source, _, _, _ = _source()
    first = source.publication_for(VARIANT)
    assert isinstance(first, PresetArtifactPublicationRequest)

    source_again, _, compiler, _ = _source(
        pointers=Pointers(pointer=first.pointer_template.complete(None).pointer)
    )

    # S6 §2.8 (D26): unchanged is typed, never None, so a bake can tell it from unavailable.
    assert source_again.publication_for(VARIANT) == SourceUnchanged(
        first.pointer_template.complete(None).pointer.revision
    )
    assert len(compiler.received) == 1


def test_an_undeclared_variant_and_an_unavailable_portfolio_bake_nothing() -> None:
    source, _, compiler, _ = _source()
    assert source.publication_for(PresetArtifactVariant("undeclared")) is None
    assert compiler.received == []

    unavailable, _, _, _ = _source(compiler=Compiler(unavailable=True))
    assert unavailable.publication_for(VARIANT) is None


def test_custody_manifest_rows_align_with_public_rows_and_bind_the_body() -> None:
    population = CAMPAIGN_PORTFOLIO_MAX_ROWS + 11
    source, _, _, _ = _source(population=population)

    request = source.publication_for(VARIANT)

    assert isinstance(request, PresetArtifactPublicationRequest)
    custody = staged_custody(request)
    assert custody is not None
    assert (custody.page, custody.variant, custody.revision) == (
        request.body.page,
        request.body.variant,
        request.body.revision,
    )
    assert custody.key.startswith("custody/campaigns/7d/")
    assert request.body.payload_json.find(custody.digest) == -1
    pointer = staged_pointer(request)
    assert pointer.custody_digest == custody.digest
    header, index = split_parent_custody(custody)
    manifest = campaign_custody_manifest_from_payload(header)
    assert manifest.body_digest == request.body.digest
    artifact = campaign_portfolio_artifact_from_payload(json.loads(request.body.payload_json))
    assert len(manifest.rows) == len(artifact.rows) == CAMPAIGN_PORTFOLIO_MAX_ROWS
    # ADR-145: the parent lists no sibling inline; its one index shard lists all 19.
    assert len(index) == 1
    assert tuple(row.safe_signature for row in manifest.rows) == tuple(
        row.signature for row in artifact.rows
    )
    assert all(
        (row.campaign.commerce_scope, row.campaign.platform, row.channel, row.marketplace)
        == (SCOPE, PLATFORM, CHANNEL, MARKETPLACE)
        for row in manifest.rows
    )
    assert not request.body.payload_json.count("campaign:")
    # The deployed 2026-09-16 defect: raw selector keys in a baked signature can never
    # equal the redemption-time label-projected signature, refusing every child selection.
    assert all(
        (row.safe_signature.channel, row.safe_signature.marketplace, row.safe_signature.platform)
        == ("Amazon DSP", "United States", "Amazon Ads")
        for row in manifest.rows
    )


def test_custody_manifest_codec_round_trips_and_refuses_noncanonical_payloads() -> None:
    source, _, _, _ = _source()
    request = source.publication_for(VARIANT)
    assert isinstance(request, PresetArtifactPublicationRequest)
    assert request.custody_header is not None
    payload = cast("dict[str, object]", json.loads(request.custody_header.payload_json))
    assert set(payload) == {"body_digest", "kind", "rows"}

    decoded = campaign_custody_manifest_from_payload(payload)
    assert campaign_custody_manifest_payload(decoded) == payload
    # ADR-145: a custody listing its siblings inline (format 11 and before) is refused, as is
    # a parent custody decoded with the stage's index still in it.
    inline_forms: tuple[dict[str, object], ...] = (
        {"descriptors": []},
        {"descriptors": [], "filter_siblings": []},
    )
    for inline in inline_forms:
        with pytest.raises(ValueError, match="fields are not exact"):
            campaign_custody_manifest_from_payload({**payload, **inline})
    with pytest.raises(ValueError, match="fields are not exact"):
        campaign_custody_manifest_from_payload({**payload, "sibling_index": []})

    with pytest.raises(ValueError, match="fields are not exact"):
        campaign_custody_manifest_from_payload({**payload, "extra": True})
    forged = cast("dict[str, object]", json.loads(request.custody_header.payload_json))
    forged_row = cast("dict[str, object]", cast("list[object]", forged["rows"])[0])
    cast("dict[str, object]", forged_row["campaign"])["value"] = " padded:campaign "
    with pytest.raises(ValueError, match=r"not canonical|invalid"):
        campaign_custody_manifest_from_payload(forged)


@dataclass
class RecordingBodies:
    events: list[str]

    def write(self, body: PresetArtifactBody) -> ArtifactBodyWriteResult:
        self.events.append("body")
        return ArtifactBodyWriteResult(
            body.page, body.variant, body.revision, body.key, body.digest, None
        )

    def write_sibling(self, body: PresetArtifactSiblingBody) -> PresetArtifactSiblingWriteResult:
        self.events.append(f"sibling-body:{body.identity.path}")
        return PresetArtifactSiblingWriteResult(
            body.page,
            body.variant,
            body.revision,
            body.identity,
            body.key,
            body.digest,
            None,
        )

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactBody:
        del pointer
        raise AssertionError("publish must not read a body")

    def read_sibling(self, request: PresetArtifactSiblingReadRequest) -> PresetArtifactSiblingBody:
        del request
        raise AssertionError("publish must not read a sibling body")


@dataclass
class RecordingCustody:
    events: list[str]

    def write(self, manifest: PresetArtifactCustodyManifest) -> CustodyManifestWriteResult:
        self.events.append("custody")
        return CustodyManifestWriteResult(
            manifest.page,
            manifest.variant,
            manifest.revision,
            manifest.key,
            manifest.digest,
            None,
        )

    def write_sibling(
        self, manifest: PresetArtifactSiblingCustody
    ) -> PresetArtifactSiblingCustodyWriteResult:
        kind = (
            "index"
            if isinstance(manifest.identity, PresetArtifactIndexShardIdentity)
            else "sibling-custody"
        )
        self.events.append(f"{kind}:{manifest.identity.path}")
        return PresetArtifactSiblingCustodyWriteResult(
            manifest.page,
            manifest.variant,
            manifest.revision,
            manifest.identity,
            manifest.key,
            manifest.digest,
            None,
        )

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactCustodyManifest:
        del pointer
        raise AssertionError("publish must not read a custody manifest")

    def read_sibling(
        self, request: PresetArtifactSiblingReadRequest
    ) -> PresetArtifactSiblingCustody:
        del request
        raise AssertionError("publish must not read sibling custody")


@dataclass
class RecordingPointers:
    events: list[str]

    def current(
        self, page: PresetArtifactPage, variant: PresetArtifactVariant
    ) -> PresetArtifactPointer | None:
        del page, variant
        raise AssertionError("publish must not read the current pointer")

    def roster(
        self, request: PresetArtifactPointerRosterRequest
    ) -> PresetArtifactPointerRosterResult:
        del request
        raise AssertionError("publish must not read the pointer roster")

    def advance(
        self, request: PresetArtifactPointerAdvanceRequest
    ) -> PresetArtifactPointerAdvanceResult:
        del request
        self.events.append("pointer")
        return PresetArtifactPointerAdvanceResult(PresetArtifactPointerAdvanceOutcome.ADVANCED)


def _custody_request() -> PresetArtifactPublicationRequest:
    revision = PresetArtifactRevision.from_publication_identities(("publication-1",))
    body = PresetArtifactBody(
        PAGE, VARIANT, revision, canonical_preset_artifact_json({"kind": "real"})
    )
    return PresetArtifactPublicationRequest(
        body,
        PresetArtifactPointerTemplate(PAGE, VARIANT, revision, body.digest, 1),
        PresetArtifactCustodyHeader(
            canonical_preset_artifact_json(
                {"body_digest": body.digest, "kind": "campaign_custody_manifest", "rows": []}
            )
        ),
    )


@pytest.mark.covers("ADR-070")
def test_persistence_writes_body_and_custody_before_the_conditional_pointer() -> None:
    events: list[str] = []
    persistence = PresetArtifactPersistence(
        RecordingBodies(events),
        RecordingPointers(events),
        RecordingCustody(events),
    )

    result = persistence.publish(
        _custody_request(), remaining_ms=lambda: 600_000, tail_seconds=30.0
    )

    assert events == ["body", "custody", "pointer"]
    assert result.custody is not None
    assert result.pointer.outcome is PresetArtifactPointerAdvanceOutcome.ADVANCED


def test_a_custody_bearing_publication_without_a_custody_store_fails_closed() -> None:
    events: list[str] = []
    persistence = PresetArtifactPersistence(RecordingBodies(events), RecordingPointers(events))

    with pytest.raises(InvariantError, match="custody store"):
        persistence.publish(_custody_request(), remaining_ms=lambda: 600_000, tail_seconds=30.0)

    # ADR-145: refused before any write, since the stage builds the custody it would write.
    assert events == []


class _RecordingQueries:
    """A required `queries` port that records every request; an unconfigured source sends none."""

    def __init__(self) -> None:
        self.seen: list[QueryRequest] = []

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        raise AssertionError("an unconfigured source must never query")


def test_campaign_source_composition_fails_closed_without_retained_configuration() -> None:
    settings = PresetArtifactBakeSettings(artifact_bucket="bucket", pointer_table="table")
    queries = _RecordingQueries()

    source = retained_campaign_preset_artifact_source(
        settings,
        cast("PresetArtifactPointerStorePort", Pointers()),
        cast("Any", queries),
        cast("Any", object()),
    )

    assert source is None
    assert queries.seen == []


@pytest.mark.covers("ADR-071")
def test_parent_contains_only_canonical_first_segment_and_indexes_every_required_sibling() -> None:
    population = CAMPAIGN_PORTFOLIO_MAX_ROWS + 11
    source, _, _, _ = _source(population=population)
    request = source.publication_for(VARIANT)
    assert isinstance(request, PresetArtifactPublicationRequest)
    custody = staged_custody(request)
    assert custody is not None

    parent = campaign_portfolio_artifact_from_payload(json.loads(request.body.payload_json))
    pairs = tuple(request.siblings)
    identities = tuple(public.identity for public, _ in pairs)
    shards = staged_index(request)

    assert parent.sort == CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc")
    assert parent.pagination is not None and parent.pagination.segment_index == 0
    assert len(parent.rows) == CAMPAIGN_PORTFOLIO_MAX_ROWS
    assert PresetArtifactSiblingIdentity("spend-desc", 0, "all") not in identities
    assert len(identities) == 19
    # ADR-145: every sibling is indexed, sorted by identity path, with its pair's digests.
    indexed = [item for shard, entry in shards for item in index_shard_descriptors(shard, entry)]
    assert [item.identity for item in indexed] == sorted(identities, key=lambda item: item.path)
    by_identity: dict[object, tuple[PresetArtifactSiblingBody, PresetArtifactSiblingCustody]] = {
        public.identity: (public, private) for public, private in pairs
    }
    assert all(
        (item.body_digest, item.custody_digest)
        == (by_identity[item.identity][0].digest, by_identity[item.identity][1].digest)
        for item in indexed
    )
    assert all(
        len(item.payload_json.encode()) <= MAX_PRESET_ARTIFACT_BYTES
        for item in (
            request.body,
            custody,
            *(shard for shard, _ in shards),
            *(item for pair in pairs for item in pair),
        )
    )


@pytest.mark.covers("ADR-071")
def test_persistence_writes_every_sibling_pair_before_parent_and_pointer() -> None:
    source, _, _, _ = _source(population=CAMPAIGN_PORTFOLIO_MAX_ROWS + 1)
    request = source.publication_for(VARIANT)
    assert isinstance(request, PresetArtifactPublicationRequest)
    events: list[str] = []
    persistence = PresetArtifactPersistence(
        RecordingBodies(events), RecordingPointers(events), RecordingCustody(events)
    )

    persistence.publish(request, remaining_ms=lambda: 600_000, tail_seconds=30.0)

    # Every pair, then the one index shard, then the parent body, its custody and the pointer.
    assert events[-4:] == ["index:index/segment-0000", "body", "custody", "pointer"]
    assert all(event.startswith("sibling-") for event in events[:-4])
    assert len(events[:-4]) == 38


@dataclass
class FailingSiblingCustody(RecordingCustody):
    remaining: int = 2

    def write_sibling(
        self, manifest: PresetArtifactSiblingCustody
    ) -> PresetArtifactSiblingCustodyWriteResult:
        self.remaining -= 1
        if self.remaining == 0:
            raise RuntimeError("partial sibling write")
        return super().write_sibling(manifest)


@pytest.mark.covers("ADR-071")
def test_partial_sibling_failure_never_writes_parent_or_advances_pointer() -> None:
    source, _, _, _ = _source(population=CAMPAIGN_PORTFOLIO_MAX_ROWS + 1)
    request = source.publication_for(VARIANT)
    assert isinstance(request, PresetArtifactPublicationRequest)
    events: list[str] = []
    persistence = PresetArtifactPersistence(
        RecordingBodies(events), RecordingPointers(events), FailingSiblingCustody(events)
    )

    with pytest.raises(RuntimeError, match="partial sibling write"):
        persistence.publish(request, remaining_ms=lambda: 600_000, tail_seconds=30.0)

    assert "body" not in events
    assert "custody" not in events
    assert "pointer" not in events


@pytest.mark.covers("ADR-071")
@pytest.mark.parametrize("population", [1_000])
def test_declared_fourteen_day_variant_stays_body_bounded_at_scale(population: int) -> None:
    source, _, _, _ = _source(population=population)
    aliases = tuple(
        replace(
            _alias(index),
            labels=(
                EffectiveLabel(
                    f"Fictional Bulk Estate | Campaign {index:04d}",
                    date(2026, 1, 1),
                ),
            ),
        )
        for index in range(1, population + 1)
    )
    catalog = Catalog(EntityCatalogBrowseResult(aliases))
    source = replace(
        source,
        catalog=cast("AnalyticalEntityCatalogPort", catalog),
        coverage=Coverage((DateRange(date(2026, 7, 1), date(2026, 8, 21)),)),
    )

    request = source.publication_for(PresetArtifactVariant("14d"))

    assert isinstance(request, PresetArtifactPublicationRequest)
    custody = staged_custody(request)
    assert custody is not None
    shards = staged_index(request)
    bodies = (
        request.body,
        custody,
        *(shard for shard, _ in shards),
        *(item for pair in request.siblings for item in pair),
    )
    assert all(len(body.payload_json.encode()) <= MAX_PRESET_ARTIFACT_BYTES for body in bodies)
    # 200 pairs (the parent's and 199 siblings) and the one shard indexing the 199.
    assert len(bodies) == (401 if population == 1_000 else 801)


@pytest.mark.covers("ADR-070", "ADR-084")
def test_display_values_start_campaign_render_format_five() -> None:
    """Identical publications re-render with display values, so the revision must move."""
    assert PresetArtifactRevision.from_publication_identities(
        ("publication-a",), render_format=5
    ) != PresetArtifactRevision.from_publication_identities(("publication-a",), render_format=4)


@pytest.mark.covers("ADR-070", "ADR-087")
def test_typed_absent_ratios_start_campaign_render_format_six() -> None:
    """A format-6 body may carry a null ratio, so it must never share a format-5 revision."""
    assert PresetArtifactRevision.from_publication_identities(
        ("publication-a",), render_format=6
    ) != PresetArtifactRevision.from_publication_identities(("publication-a",), render_format=5)


@pytest.mark.covers("ADR-070", "ADR-121")
def test_change_blocks_start_campaign_render_format_seven() -> None:
    """A format-7 body carries change blocks, so it must never share a format-6 revision.

    Format 8 adds the coverage block (ADR-119), format 9 NextStep.v2 (ADR-122) and format 10
    moves with the compared-metric roster (ADR-125); format 11 bakes the channel filter
    siblings (ADR-127); format 12 indexes every sibling (ADR-145); each keeps them.
    """
    assert CAMPAIGN_PRESET_RENDER_FORMAT == 12
    assert PresetArtifactRevision.from_publication_identities(
        ("publication-a",), render_format=CAMPAIGN_PRESET_RENDER_FORMAT
    ) != PresetArtifactRevision.from_publication_identities(("publication-a",), render_format=6)


@pytest.mark.covers("ADR-070", "ADR-122")
def test_next_step_v2_starts_campaign_render_format_nine() -> None:
    """A format-9 revision never shares a format-8 one, so no cached slot keeps a v1 step."""
    assert PresetArtifactRevision.from_publication_identities(
        ("publication-a",), render_format=CAMPAIGN_PRESET_RENDER_FORMAT
    ) != PresetArtifactRevision.from_publication_identities(("publication-a",), render_format=8)
