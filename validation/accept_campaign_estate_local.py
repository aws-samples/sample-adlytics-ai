# SPDX-License-Identifier: MIT-0
"""Run the deterministic 1,000-campaign ADR-071 local acceptance and retain raw evidence."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic
from types import ModuleType
from typing import Any, Protocol, cast

from adapters.campaign_artifact_renderer import CampaignArtifactRenderInput
from adapters.campaign_measurement import (
    CampaignArtifactMeasurementService,
    CampaignMeasurementDependencies,
    CampaignMeasurementSelection,
    CampaignPortfolioMeasurementInput,
)
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.fixture_generation.catalog import (
    BULK_CAMPAIGN_COUNT,
    SyntheticCampaign,
    load_synthetic_entity_catalog,
)
from adapters.jobs.preset_artifact_publisher.campaign_source import (
    RetainedCampaignPresetArtifactSource,
)
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.preset_artifacts import PresetArtifactPersistence
from domain.presentation.analytical_pages import PresentationDimensionLabel
from domain.presentation.campaign_attribution import CampaignSelectionCapability
from domain.presentation.campaign_custody import (
    campaign_custody_manifest_from_payload,
    custody_rows_align,
)
from domain.presentation.campaign_portfolio import (
    CAMPAIGN_PORTFOLIO_MAX_ROWS,
    DSP_CHANNEL_KEY,
    CampaignPortfolioRow,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
    campaign_portfolio_artifact_from_payload,
    campaign_portfolio_artifact_payload,
    campaign_portfolio_evidence_status,
    campaign_portfolio_segment_from_payload,
    order_campaign_portfolio_rows,
)
from domain.presentation.compared_metrics import presentation_labels
from domain.presentation.insights import (
    MAX_INSIGHT_EVIDENCE_BYTES,
    MAX_INSIGHT_SLOT_TEXT_BYTES,
    MAX_NEXT_STEP_REASON_CHARS,
    InsightNextStep,
    InsightNextStepKind,
    InsightSlotId,
    InsightSlotState,
)
from domain.presentation.next_steps import MAX_ASSISTANT_QUESTION_CHARS, AssistantTarget
from domain.presentation.preset_artifacts import (
    MAX_PRESET_ARTIFACT_BYTES,
    ArtifactBodyWriteResult,
    CustodyManifestWriteResult,
    PresetArtifactBody,
    PresetArtifactCustodyManifest,
    PresetArtifactIndexShardIdentity,
    PresetArtifactPublicationRequest,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingCustodyWriteResult,
    PresetArtifactSiblingDescriptor,
    PresetArtifactSiblingIdentity,
    PresetArtifactSiblingWriteResult,
    PresetArtifactVariant,
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
from domain.shared_kernel.values import DateRange
from ports.outbound.entity_catalog import (
    AnalyticalEntityCatalogPort,
    EntityCatalogBrowseRequest,
    EntityCatalogBrowseResult,
)
from ports.outbound.query import MAX_ROWS_CEILING


@dataclass(frozen=True, slots=True)
class CampaignEstateAcceptanceResult:
    """Measured local output from one complete bulk publication render."""

    campaign_count: int
    segment_count: int
    sibling_pair_count: int
    body_count: int
    maximum_body_bytes: int
    maximum_served_bytes: int
    measurement_plan_count: int
    duration_ms: int

    def payload(self) -> dict[str, int | str]:
        """Project the stable raw evidence document."""
        return {
            "body_count": self.body_count,
            "campaign_count": self.campaign_count,
            "duration_ms": self.duration_ms,
            "maximum_body_bytes": self.maximum_body_bytes,
            "maximum_served_bytes": self.maximum_served_bytes,
            "measurement_plan_count": self.measurement_plan_count,
            "outcome": "passed",
            "segment_count": self.segment_count,
            "sibling_pair_count": self.sibling_pair_count,
        }


class _AcceptanceSource(Protocol):
    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | None: ...


class _AcceptanceCompiler(Protocol):
    received: list[CampaignArtifactRenderInput]


@dataclass
class _MemoryStores:
    """In-memory body and custody stores: every object one real stage writes, by key."""

    objects: dict[str, str]

    def write(self, item: Any) -> Any:
        self.objects[item.key] = item.payload_json
        kind = (
            ArtifactBodyWriteResult
            if isinstance(item, PresetArtifactBody)
            else CustodyManifestWriteResult
        )
        return kind(item.page, item.variant, item.revision, item.key, item.digest, None)

    def write_sibling(self, item: Any) -> Any:
        self.objects[item.key] = item.payload_json
        kind = (
            PresetArtifactSiblingWriteResult
            if isinstance(item, PresetArtifactSiblingBody)
            else PresetArtifactSiblingCustodyWriteResult
        )
        return kind(
            item.page, item.variant, item.revision, item.identity, item.key, item.digest, None
        )


@dataclass(frozen=True, slots=True)
class _Staged:
    """What the real stage wrote for one publication (ADR-145): every object, the parent custody,
    and the descriptors its sibling index lists."""

    objects: dict[str, str]
    custody: PresetArtifactCustodyManifest
    descriptors: dict[object, PresetArtifactSiblingDescriptor]


def _stage(request: PresetArtifactPublicationRequest) -> _Staged:
    """Stage one publication through the real streamed stage into memory; nothing is persisted."""
    objects: dict[str, str] = {}
    stores = _MemoryStores(objects)
    persistence = PresetArtifactPersistence(
        cast("Any", stores), cast("Any", object()), cast("Any", stores)
    )
    staged = persistence.stage(request, remaining_ms=lambda: 900_000, tail_seconds=30.0)
    if staged.custody is None:
        raise RuntimeError("bulk campaign parent custody is absent.")
    body = request.body
    custody = PresetArtifactCustodyManifest(
        body.page, body.variant, body.revision, objects[staged.custody.key]
    )
    _, index = split_parent_custody(custody)
    descriptors: dict[object, PresetArtifactSiblingDescriptor] = {}
    for position, entry in enumerate(index):
        identity = PresetArtifactIndexShardIdentity(position)
        shard = PresetArtifactSiblingCustody(
            body.page,
            body.variant,
            body.revision,
            identity,
            objects[
                f"custody/{body.page.value}/{body.variant.value}/{body.revision.value}/"
                f"{identity.path}.json"
            ],
        )
        descriptors.update(
            (item.identity, item) for item in index_shard_descriptors(shard, entry)
        )
    siblings = (len(objects) - len(index) - 2) // 2
    if staged.pairs != siblings + len(index) + 1:
        raise RuntimeError("the stage's write chains disagree with the objects it wrote.")
    return _Staged(objects, custody, descriptors)


_AcceptanceSourceFactory = Callable[
    ...,
    tuple[_AcceptanceSource, object, _AcceptanceCompiler, object],
]


@dataclass
class _BulkCatalog:
    result: EntityCatalogBrowseResult

    def browse(self, request: EntityCatalogBrowseRequest) -> EntityCatalogBrowseResult:
        del request
        return self.result


_SCOPE = CommerceScopeKey("amazon_retail_us")
_PLATFORM = PlatformKey("amazon_ads")
_CHANNEL = ChannelKey("amazon_dsp")
_MARKETPLACE = MarketplaceKey("amazon_us")


def _governed_aliases(campaigns: tuple[SyntheticCampaign, ...]) -> tuple[EntityAlias, ...]:
    """Project governed bulk identities and names into the retained catalog boundary."""
    aliases: list[EntityAlias] = []
    for campaign in campaigns:
        key = campaign.key
        name = campaign.names[0].value
        canonical = CampaignKey(key, _SCOPE, _PLATFORM)
        aliases.append(
            EntityAlias(
                kind=EntityKind.CAMPAIGN,
                key=canonical,
                commerce_scope=_SCOPE,
                labels=(EffectiveLabel(name, date(2026, 1, 1)),),
                source_identifiers=(
                    SourceIdentifier(
                        "fictional.bulk.campaign",
                        next(iter(campaign.source_aliases.values())),
                    ),
                ),
                evidence=MappingEvidence("governed-bulk-campaign-estate", 1),
                valid_from=date(2026, 1, 1),
                platform=_PLATFORM,
                channel=_CHANNEL,
                marketplace=_MARKETPLACE,
            )
        )
    return tuple(aliases)


def _publisher_fixture(root: Path) -> ModuleType:
    """Load the deterministic compiler fixture while exercising production publication code."""
    test_root = root / "tests" / "unit"
    sys.path.insert(0, str(test_root))
    path = test_root / "test_campaign_preset_source.py"
    spec = importlib.util.spec_from_file_location("campaign_estate_acceptance_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("campaign publisher acceptance fixture cannot be loaded.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _measurement_plan_count(
    root: Path,
    render: CampaignArtifactRenderInput,
) -> int:
    """Prove the real grouped plan count does not grow with the campaign population."""
    service = CampaignArtifactMeasurementService(
        CampaignMeasurementDependencies(
            scope=AuthorizedAnalyticalScope(frozenset({_SCOPE})),
            catalog=load_catalog(root),
            contracts=tuple(contract for _, contract in load_contracts(root)),
            clock=lambda: datetime.now(UTC),
        )
    )

    def count(selections: tuple[object, ...]) -> int:
        typed = cast("tuple[CampaignMeasurementSelection, ...]", selections)
        batch = service.portfolio_batch(
            CampaignPortfolioMeasurementInput(
                typed,
                render.period,
                render.prior_period,
                render.sort,
            )
        )
        population = len(typed)
        for plan in batch.prepared.plans:
            daily = "date_key" in plan.logical.grain.dimensions
            expected_rows = population * plan.logical.window.days if daily else population
            if plan.max_rows != expected_rows or plan.max_rows > MAX_ROWS_CEILING:
                raise RuntimeError("portfolio plan row bound is not population-complete.")
        return len(batch.prepared.plans)

    full = count(cast("tuple[object, ...]", render.selections))
    bounded = count(cast("tuple[object, ...]", render.selections[:50]))

    def expected(population: int) -> int:
        days_per_request = MAX_ROWS_CEILING // population
        chunks = -(-render.period.days // days_per_request)
        # Delivery per channel (ADR-127) adds clicks, and the two DSP viewability operands
        # when the cohort has Amazon DSP campaigns, each as a current and prior plan.
        dsp = any(item.channel.value == DSP_CHANNEL_KEY for item in render.selections)
        return 16 + 4 * chunks + 2 + (4 if dsp else 0)

    if full != expected(len(render.selections)) or bounded != expected(50):
        raise RuntimeError("portfolio grouped measurement chunk count is not derivable.")
    return full


def _attest_request(  # noqa: PLR0912
    request: PresetArtifactPublicationRequest,
    campaigns: tuple[SyntheticCampaign, ...],
) -> tuple[int, int]:
    """Stage and walk every sibling; return maximum persisted and worst-case served bytes."""
    staged = _stage(request)
    parent = campaign_portfolio_artifact_from_payload(json.loads(request.body.payload_json))
    header, _ = split_parent_custody(staged.custody)
    manifest = campaign_custody_manifest_from_payload(header)
    segment_count = -(-len(campaigns) // CAMPAIGN_PORTFOLIO_MAX_ROWS)
    if (
        len(parent.rows) != CAMPAIGN_PORTFOLIO_MAX_ROWS
        or len(parent.roster) != len(campaigns)
        or parent.roster[-1].position != len(campaigns) - 1
        or manifest.body_digest != request.body.digest
        or not custody_rows_align(manifest, tuple(row.signature for row in parent.rows))
    ):
        raise RuntimeError("bulk campaign parent, roster, or custody is incomplete.")
    descriptors = staged.descriptors
    ordered: dict[str, dict[int, tuple[object, ...]]] = {
        f"{field.value}-{direction}": {}
        for field in CampaignPortfolioSortField
        for direction in ("asc", "desc")
    }
    ordered["spend-desc"][0] = cast("tuple[object, ...]", parent.rows)
    forbidden = tuple(
        value
        for campaign in campaigns
        for value in (campaign.key, *campaign.source_aliases.values())
    )
    if any(value in request.body.payload_json for value in forbidden):
        raise RuntimeError("bulk campaign parent exposes canonical or source identity.")
    for public, private in request.siblings:
        descriptor = descriptors.get(public.identity)
        if (
            descriptor is None
            or private.identity != public.identity
            or descriptor.body_digest != public.digest
            or descriptor.custody_digest != private.digest
        ):
            raise RuntimeError("bulk campaign sibling descriptor digest binding failed.")
        identity = cast("PresetArtifactSiblingIdentity", public.identity)
        segment = campaign_portfolio_segment_from_payload(
            json.loads(public.payload_json), parent
        )
        custody = campaign_custody_manifest_from_payload(json.loads(private.payload_json))
        if (
            segment.pagination is None
            or segment.pagination.segment_index != identity.segment_index
            or segment.pagination.segment_count != segment_count
            or custody.body_digest != public.digest
            or not custody_rows_align(custody, tuple(row.signature for row in segment.rows))
        ):
            raise RuntimeError("bulk campaign sibling or custody is not exact.")
        if any(value in public.payload_json for value in forbidden):
            raise RuntimeError("bulk campaign sibling exposes canonical or source identity.")
        ordered[identity.ordering][identity.segment_index] = cast(
            "tuple[object, ...]", segment.rows
        )
    expected_identities = len(tuple(CampaignPortfolioSortField)) * 2 * segment_count - 1
    if len(descriptors) != expected_identities:
        raise RuntimeError("bulk campaign descriptor index is incomplete.")
    baseline: set[object] | None = None
    for field in CampaignPortfolioSortField:
        for direction in ("asc", "desc"):
            ordering = f"{field.value}-{direction}"
            segments = ordered[ordering]
            if set(segments) != set(range(segment_count)):
                raise RuntimeError("bulk campaign ordering has a missing segment.")
            rows = tuple(row for index in range(segment_count) for row in segments[index])
            signatures = {cast("Any", row).signature for row in rows}
            if len(rows) != len(campaigns) or len(signatures) != len(campaigns):
                raise RuntimeError("bulk campaign ordering loses or duplicates a row.")
            if baseline is None:
                baseline = signatures
            elif signatures != baseline:
                raise RuntimeError("bulk campaign orderings do not cover the same population.")
            expected = order_campaign_portfolio_rows(
                cast("tuple[CampaignPortfolioRow, ...]", rows),
                CampaignPortfolioSort(field, direction),
            )
            if rows != expected:
                raise RuntimeError("bulk campaign ordering is not deterministic.")
    persisted_maximum = max(len(item.encode()) for item in staged.objects.values())
    served = replace(
        parent,
        rows=tuple(
            replace(
                row,
                capability=CampaignSelectionCapability(
                    f"{index:043d}", datetime.max.replace(tzinfo=UTC)
                ),
            )
            for index, row in enumerate(parent.rows)
        ),
        insights=tuple(
            replace(
                slot,
                state=InsightSlotState.FILLED,
                text="N" * (MAX_INSIGHT_SLOT_TEXT_BYTES - 2),
                evidence_line="E" * (MAX_INSIGHT_EVIDENCE_BYTES - 2),
                absence_class=None,
                absence_reason=None,
                # NextStep.v2 (ADR-122): each model card's largest step, a full-length reason
                # and question. The deterministic movers card offers none.
                next_step=(
                    None
                    if slot.id is InsightSlotId.MOVERS
                    else InsightNextStep(
                        InsightNextStepKind.ASK_ASSISTANT,
                        AssistantTarget("Q" * MAX_ASSISTANT_QUESTION_CHARS),
                        "Ask the assistant",
                        "R" * MAX_NEXT_STEP_REASON_CHARS,
                    )
                ),
            )
            for slot in parent.insights
        ),
        # A real bake serves the window's coverage block (ADR-119), which the deterministic
        # fixture compiler doesn't write.
        evidence_status=campaign_portfolio_evidence_status(
            parent.period,
            presentation_labels(load_catalog(Path(__file__).resolve().parents[1])),
            multi_channel=parent.multi_channel,
        ),
    )
    served_bytes = len(
        canonical_preset_artifact_json(campaign_portfolio_artifact_payload(served)).encode()
    )
    if served_bytes > MAX_PRESET_ARTIFACT_BYTES:
        raise RuntimeError("maximum-scale served campaign artifact exceeds its byte cap.")
    return persisted_maximum, served_bytes


def run(root: Path) -> CampaignEstateAcceptanceResult:
    """Render the governed bulk estate and attest every bounded immutable body."""
    started = monotonic()
    catalog = load_synthetic_entity_catalog(root)
    campaigns = catalog.bulk_campaigns
    if (
        len(campaigns) != BULK_CAMPAIGN_COUNT
        or len({item.key for item in campaigns}) != BULK_CAMPAIGN_COUNT
    ):
        raise RuntimeError("governed bulk campaign expansion is incomplete.")
    fixture = _publisher_fixture(root)
    source_factory = cast("_AcceptanceSourceFactory", fixture.__dict__["_source"])
    template, _, compiler, _ = source_factory(population=len(campaigns))
    source = replace(
        cast("RetainedCampaignPresetArtifactSource", template),
        coverage=fixture.Coverage(
            (DateRange(date(2026, 6, 1), date(2026, 8, 21)),)
        ),
        catalog=cast(
            "AnalyticalEntityCatalogPort",
            _BulkCatalog(EntityCatalogBrowseResult(_governed_aliases(campaigns))),
        ),
    )
    requests: list[PresetArtifactPublicationRequest] = []
    for variant in (
        PresetArtifactVariant("1d"),
        PresetArtifactVariant("7d"),
        PresetArtifactVariant("14d"),
        PresetArtifactVariant("30d"),
    ):
        request = source.publication_for(variant)
        if request is None or request.custody_header is None:
            raise RuntimeError(f"bulk campaign publication {variant.value} was unavailable.")
        requests.append(request)
    request = requests[-1]
    segment_count = -(-len(campaigns) // CAMPAIGN_PORTFOLIO_MAX_ROWS)
    expected_pairs = len(tuple(CampaignPortfolioSortField)) * 2 * segment_count - 1
    sizes = tuple(_attest_request(item, campaigns) for item in requests)
    maximum = max(persisted for persisted, _served in sizes)
    maximum_served = max(served for _persisted, served in sizes)
    if maximum > MAX_PRESET_ARTIFACT_BYTES:
        raise RuntimeError("a bulk campaign body exceeds the artifact byte cap.")
    if len(compiler.received) != len(requests) or any(
        len(render.selections) != len(campaigns) for render in compiler.received
    ):
        raise RuntimeError("bulk campaign renders did not consume the full population once each.")
    measurement_plan_count = _measurement_plan_count(root, compiler.received[-1])
    return CampaignEstateAcceptanceResult(
        campaign_count=len(campaigns),
        segment_count=segment_count,
        sibling_pair_count=expected_pairs,
        body_count=len(_stage(request).objects),
        maximum_body_bytes=maximum,
        maximum_served_bytes=maximum_served,
        measurement_plan_count=measurement_plan_count,
        duration_ms=int((monotonic() - started) * 1_000),
    )


_FILTER_ESTATE_SPLIT = (
    ("amazon_dsp", 1),
    ("amazon_sponsored_products", 251),
    ("amazon_sponsored_brands", 251),
    ("amazon_sponsored_display", 251),
    ("amazon_sponsored_television", 246),
)
"""1,000 campaigns over the five governed channels, split so every filter but Amazon DSP ends on a
partly filled segment: 64 of the 65 segments the seven-sibling envelope allows (ADR-127)."""


@dataclass(frozen=True, slots=True)
class CampaignFilterEstateResult:
    """Measured local output of one seven-sibling bake at the estate bound."""

    campaign_count: int
    filter_count: int
    segments_by_filter: dict[str, int]
    object_count: int
    expected_object_count: int
    total_bytes: int
    maximum_body_bytes: int
    render_ms: int
    peak_traced_bytes: int

    def payload(self) -> dict[str, object]:
        """Project the stable raw evidence document."""
        return {
            "campaign_count": self.campaign_count,
            "expected_object_count": self.expected_object_count,
            "filter_count": self.filter_count,
            "maximum_body_bytes": self.maximum_body_bytes,
            "object_count": self.object_count,
            "peak_traced_bytes": self.peak_traced_bytes,
            "render_ms": self.render_ms,
            "segments_by_filter": self.segments_by_filter,
            "total_bytes": self.total_bytes,
        }


def run_filter_estate(root: Path) -> CampaignFilterEstateResult:  # noqa: PLR0914
    """Bake the 30d preset of a 1,000-campaign estate over five channels, seven siblings.

    The real renderer reduces every filter sibling from one parsed batch and the real source
    writes the publication; nothing is persisted. Render time is measured untraced, then peak
    Python allocation is traced over a second identical bake.
    """
    import tracemalloc  # noqa: PLC0415 -- evidence-only instrumentation

    from adapters.jobs.preset_artifact_publisher import bakeability  # noqa: PLC0415

    fixture = _publisher_fixture(root)
    support = importlib.import_module("campaign_channel_filter_support")
    figures = support.Figures
    estate = tuple(
        support.FictionalCampaign(
            f"Fictional Estate {channel[7:].replace('_', ' ').title()} {index:04d}",
            channel,
            figures(Decimal(100 + (index * 37) % 991), Decimal(300 + (index * 53) % 1999), 1 + index % 41, 10_000 + (index * 97) % 9_973, 20 + index % 211),
            figures(Decimal(90 + (index * 41) % 983), Decimal(280 + (index * 59) % 1987), 1 + index % 37, 9_500 + (index * 89) % 9_967, 18 + index % 199),
            viewable=(6_000 + index % 500, 9_000 + index % 700) if channel == DSP_CHANNEL_KEY else None,
            prior_viewable=(5_800 + index % 480, 8_900 + index % 690) if channel == DSP_CHANNEL_KEY else None,
        )
        for channel, count in _FILTER_ESTATE_SPLIT
        for index in range(1, count + 1)
    )
    aliases = tuple(
        EntityAlias(
            kind=EntityKind.CAMPAIGN,
            key=item.selection.campaign,
            commerce_scope=item.selection.campaign.commerce_scope,
            labels=(EffectiveLabel(item.name, date(2026, 1, 1)),),
            source_identifiers=(SourceIdentifier("fictional.filter.estate", item.selection.campaign.value),),
            evidence=MappingEvidence("governed-filter-estate", 1),
            valid_from=date(2026, 1, 1),
            platform=item.selection.campaign.platform,
            channel=item.selection.channel,
            marketplace=item.selection.marketplace,
        )
        for item in estate
    )
    labels = (
        PresentationDimensionLabel("platform_key", "amazon_ads", "platform", "Amazon Ads"),
        PresentationDimensionLabel("marketplace_key", "amazon_us", "marketplace", "United States"),
        *(
            PresentationDimensionLabel("channel_key", key, "channel", label)
            for key, label in support.CHANNEL_LABELS.items()
        ),
    )
    source_factory = cast("_AcceptanceSourceFactory", fixture.__dict__["_source"])
    template, _, _, _ = source_factory(population=len(estate))
    source = replace(
        cast("RetainedCampaignPresetArtifactSource", template),
        coverage=fixture.Coverage((DateRange(date(2026, 6, 1), date(2026, 8, 21)),)),
        catalog=cast(
            "AnalyticalEntityCatalogPort",
            _BulkCatalog(EntityCatalogBrowseResult(aliases)),
        ),
        compiler=support.FilterCompiler(estate),
        labels=labels,
        channel_order=support.CHANNEL_ORDER,
    )
    variant = PresetArtifactVariant("30d")
    started = monotonic()
    request = source.publication_for(variant)
    render_ms = int((monotonic() - started) * 1_000)
    tracemalloc.start()
    traced = source.publication_for(variant)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if request is None or request.custody_header is None or traced is None:
        raise RuntimeError("the seven-sibling estate publication was unavailable.")
    if traced.body.digest != request.body.digest:
        raise RuntimeError("the seven-sibling estate bake is not deterministic.")
    parent = campaign_portfolio_artifact_from_payload(json.loads(request.body.payload_json))
    if parent.channel_filter is None:
        raise RuntimeError("the seven-sibling estate offers no channel filter.")
    offered = tuple(option.filter_id for option in parent.channel_filter.options)
    population = {key: count for key, count in _FILTER_ESTATE_SPLIT}
    expected_population = {
        "all": len(estate),
        "sponsored": len(estate) - population[DSP_CHANNEL_KEY],
        **population,
    }
    if offered != ("all", "sponsored", *(key for key, _ in _FILTER_ESTATE_SPLIT)):
        raise RuntimeError("the seven-sibling estate offers the wrong filters.")
    orderings = len(tuple(CampaignPortfolioSortField)) * 2
    # Each filter writes every ordering's segments except its canonical first, which is the
    # filter's own parent body, so a filter of S segments has S x orderings - 1 segment siblings.
    pairs: dict[str, int] = dict.fromkeys(offered, 0)
    for body, _ in request.siblings:
        identity = cast("Any", body.identity)
        filter_id = identity.filter_id
        if not hasattr(identity, "ordering"):
            sibling = campaign_portfolio_artifact_from_payload(json.loads(body.payload_json))
            if sibling.channel_filter is None or sibling.channel_filter.selected != filter_id:
                raise RuntimeError("a filter sibling names another filter.")
            if len(sibling.roster) != expected_population[filter_id]:
                raise RuntimeError("a filter sibling's roster isn't its own population.")
            if filter_id in population and {row.signature.channel for row in sibling.rows} != {
                support.CHANNEL_LABELS[filter_id]
            }:
                raise RuntimeError("a one-channel sibling serves another channel's rows.")
            continue
        pairs[filter_id] += 1
    if any((count + 1) % orderings for count in pairs.values()):
        raise RuntimeError("a filter is missing an ordering's segment.")
    segments = {key: (count + 1) // orderings for key, count in pairs.items()}
    if len(parent.roster) != len(estate):
        raise RuntimeError("the All channels roster isn't the whole estate.")
    expected_segments = {key: -(-count // CAMPAIGN_PORTFOLIO_MAX_ROWS) for key, count in expected_population.items()}
    if segments != expected_segments:
        raise RuntimeError(f"segments by filter {segments} differ from {expected_segments}.")
    # Every object the real stage writes: the pairs, the index shards, the parent (ADR-145).
    objects = tuple(_stage(request).objects.values())
    sizes = tuple(len(item.encode()) for item in objects)
    return CampaignFilterEstateResult(
        campaign_count=len(estate),
        filter_count=len(offered),
        segments_by_filter=segments,
        object_count=len(objects),
        expected_object_count=bakeability.campaign_bake_object_count(tuple(expected_population.values())),
        total_bytes=sum(sizes),
        maximum_body_bytes=max(sizes),
        render_ms=render_ms,
        peak_traced_bytes=peak,
    )


def main() -> int:
    """Run acceptance and write its raw result only below ignored local evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    evidence_root = (root / "validation" / "evidence").resolve()
    evidence_dir = args.evidence_dir.resolve()
    if evidence_root not in evidence_dir.parents:
        raise ValueError("campaign estate evidence must remain under validation/evidence.")
    evidence_dir.mkdir(parents=True, exist_ok=False)
    result = run(root)
    filters = run_filter_estate(root)
    if filters.object_count != filters.expected_object_count:
        raise RuntimeError("the seven-sibling bake writes a different object count than the gate counts.")
    payload = {**result.payload(), "filter_estate": filters.payload()}
    output = evidence_dir / "result.json"
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({**payload, "evidence": str(output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
