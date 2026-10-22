# SPDX-License-Identifier: MIT-0
"""Authenticated campaign presentation routing, custody, and page-context minting."""

from __future__ import annotations

import importlib
import inspect
import json
import pathlib
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from campaign_portfolio_support import (
    LABELS,
    ONE_CHANNEL_FILTER,
    campaign_display_signature,
    complete_portfolio,
    portfolio_row,
    portfolio_totals,
    with_missing_day,
)
from change_support import SEMANTICS, as_retained
from dynamodb_reserved_words import guard_reserved_words
from insight_key_support import FICTIONAL_INPUT_DIGEST
from preset_artifact_support import indexed_parent_custody
from publication_support import StubPublications
from test_publication_selection import RecordingCurrent

from adapters import deep_insight_inputs
from adapters.config import settings_without_client_secret
from adapters.inbound.correlation import returns_correlation
from adapters.inbound.presentation_handlers.insights import (
    InsightAssemblyRequest,
    InsightDeepServe,
)
from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
)
from adapters.insight_requests import InsightRequestContext
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.presentation_validation.loader import load_page_schemas
from adapters.outbound.catalog import (
    PageDefaultWindowCompleteness,
    PageDefaultWindowRequest,
    PageDefaultWindowResult,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.presentation_graph import DynamoPresentationGraphReader
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.source_pack import load_source_packs
from domain.catalog.freshness import Coverage
from domain.measurement.planning import measure_alias
from domain.narrative.shaped_response import UnitKind, Units
from domain.operations.execution_graph_release import (
    BindingKind,
    ServingBinding,
    serving_generation,
)
from domain.presentation.absence_copy import SERVING_CHECK_TEXT
from domain.presentation.analytical_pages import PresentationDimensionLabel
from domain.presentation.artifacts import (
    CampaignSelectionCapabilityReadRequest,
    CampaignSelectionCapabilityRecord,
    CampaignSelectionCapabilityValidationResult,
    ChannelRowCapabilityReadRequest,
    ChannelRowCapabilityRecord,
    ChannelRowCapabilityValidationResult,
    MediaScope,
    PageArtifactReadRequest,
    PageArtifactRecord,
    PageArtifactValidationResult,
    ParentCapabilityIssuanceOutcome,
    ParentCapabilityIssuanceRequest,
    ParentCapabilityIssuanceResult,
    campaign_capability_parent_is_valid,
    channel_capability_parent_is_valid,
    validate_campaign_selection_capability,
    validate_channel_row_capability,
    validate_page_artifact,
)
from domain.presentation.campaign_attribution import (
    CampaignListEvidence,
    CampaignSelectionCapability,
)
from domain.presentation.campaign_custody import (
    CampaignCustodyManifest,
    CampaignCustodyRow,
    campaign_custody_manifest_payload,
)
from domain.presentation.campaign_portfolio import (
    CampaignPortfolioMetric,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
    build_campaign_portfolio,
    campaign_portfolio_artifact_from_payload,
    campaign_portfolio_artifact_payload,
    campaign_portfolio_segment,
    campaign_portfolio_segment_payload,
)
from domain.presentation.graph_serving import refusal_age_seconds
from domain.presentation.insights import (
    DeterministicFinding,
    InsightCacheKey,
    InsightDeepAnalysis,
    InsightDeepDiveState,
    InsightHorizonRole,
    InsightNextStep,
    InsightNextStepKind,
    InsightOutputClass,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    InsightWindowProfile,
    unavailable_insight_slots,
)
from domain.presentation.metric_focus import CAMPAIGN_PORTFOLIO_FOCUS_METRICS
from domain.presentation.next_steps import CampaignKeyTarget, campaign_step_key
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactCustodyManifest,
    PresetArtifactIndexShardIdentity,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerRosterRequest,
    PresetArtifactPointerRosterResult,
    PresetArtifactRevision,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingIdentity,
    PresetArtifactSiblingPair,
    PresetArtifactSiblingReadRequest,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
    split_parent_custody,
)
from domain.presentation.retail_health import (
    RETAIL_HEALTH_INSIGHT_IDS,
    RETAIL_HEALTH_METRIC_UNITS,
    RETAIL_HEALTH_ROW_METRICS,
    RETAIL_HEALTH_TOTAL_METRICS,
    RETAIL_HEALTH_TREND_METRICS,
    RETURN_REASONS_ABSENCE,
    RetailComparison,
    RetailDistributorView,
    RetailHealthArtifact,
    RetailHealthOutcome,
    RetailReconciliation,
    RetailTrendPoint,
    retail_comparison,
    retail_health_artifact_from_payload,
    retail_health_artifact_payload,
    retail_health_evidence_status,
)
from domain.presentation.retail_health import (
    RetailMetricValue as RetailHealthMetricValue,
)
from domain.presentation.retail_health import (
    RetailProductRow as RetailHealthProductRow,
)
from domain.presentation.retail_health_custody import (
    RetailHealthCustodyManifest,
    RetailHealthCustodyProduct,
    product_ref_for,
    retail_health_custody_manifest_payload,
)
from domain.presentation.temporal import TemporalDisplayGrain
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EffectiveLabel,
    EntityAlias,
    EntityKind,
    MappingEvidence,
    ResolutionOutcome,
)
from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
    ProductKey,
    SourceIdentifier,
)
from domain.semantics.page_context import (
    CampaignSelectionReadRequest,
    CampaignSelectionValidationResult,
    PageContextRecord,
    PageContextReference,
    PageContextSnapshot,
    ProductSelectionReadRequest,
    ProductSelectionValidationResult,
    ReportingWindowStatus,
    validate_campaign_selection,
    validate_product_selection,
)
from domain.shared_kernel.errors import (
    AdlyticsError,
    DependencyError,
    DomainError,
    ErrorCode,
    InvariantError,
    ValidationError,
)
from domain.shared_kernel.provenance import (
    FreshnessStatus,
    MeasurementBasis,
    Suppression,
)
from domain.shared_kernel.values import DateRange, Grain
from ports.outbound.entity_catalog import (
    EntityCatalogBrowseRequest,
    EntityCatalogBrowseResult,
    EntityCatalogLookupRequest,
    EntityCatalogLookupResult,
    PublicEntityLabelOutcome,
    PublicEntityLabelRequest,
    PublicEntityLabelResolution,
    PublicEntityLabelResult,
)
from ports.outbound.query import QueryRequest, QueryResult, QueryRow
from ports.outbound.source_coverage import (
    SourceCoverageBounds,
    SourceCoverageRequest,
    SourceCoverageResult,
)

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMAS = load_page_schemas(ROOT)
METRIC_CATALOG = load_catalog(ROOT)
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))

LIST_PATH = "/api/presentation/campaigns"
DETAIL_PATH = "/api/presentation/campaign-detail"
OWNER = "verified-owner"
SESSION = "conversation-session"
NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)
SCOPE = CommerceScopeKey("amazon_retail_us")
PLATFORM = PlatformKey("amazon_ads")
CHANNEL = ChannelKey("amazon_dsp")
MARKETPLACE = MarketplaceKey("amazon_us")
DATA_HEALTH_GRAIN = ("date", "channel", "campaign", "ad_product", "marketplace")
CAMPAIGN_PLATFORM_LABELS = (
    PresentationDimensionLabel("platform_key", PLATFORM.value, "platform", "Amazon Ads"),
)
DIMENSION_LABELS = (
    PresentationDimensionLabel("channel_key", CHANNEL.value, "channel", "Fictional channel"),
    PresentationDimensionLabel(
        "marketplace_key", MARKETPLACE.value, "marketplace", "United States"
    ),
    PresentationDimensionLabel(
        "channel_key", "canonical-channel-secret", "channel", "Fictional channel"
    ),
    PresentationDimensionLabel(
        "marketplace_key", "canonical-marketplace-secret", "marketplace", "United States"
    ),
)
CAMPAIGN = CampaignKey("campaign:fictional", SCOPE, PLATFORM)
OTHER_CAMPAIGN = CampaignKey("campaign:other", SCOPE, PLATFORM)
PRODUCT = ProductKey("product:fictional", SCOPE, MARKETPLACE)
TOKEN = "T" * 43
FIRST_SELECTION_TOKEN = "S" * 43
SECOND_SELECTION_TOKEN = "R" * 43
THIRD_SELECTION_TOKEN = "Q" * 43

_ENV = {
    "AWS_DEFAULT_REGION": "eu-west-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "ADL_ENVIRONMENT": "dev",
    "ADL_AWS_REGION": "eu-west-1",
    "ADL_COGNITO_USER_POOL_ID": "eu-west-1_example",
    "ADL_COGNITO_CLIENT_ID": "1example23456789",
    "ADL_ATHENA_WORKGROUP": "interactive",
    "ADL_PAGE_CONTEXT_TABLE": "page-context",
    "ADL_SEMANTIC_ATTEMPTS_TABLE": "semantic-attempts",
    "ADL_SEMANTIC_LEASES_TABLE": "semantic-leases",
    "ADL_SEMANTIC_PUBLICATIONS_TABLE": "semantic-publications",
    "ADL_SEMANTIC_V2_CURRENT_DATABASE": "semantic-current",
    "ADL_SEMANTIC_V2_BUCKET": "semantic-bucket",
    "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "60",
    "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "8",
    "ADL_COGNITO_LOGIN_DOMAIN": "auth.customer.com",
    "ADL_APP_BASE_URL": "https://app.customer.com",
    "ADL_API_BASE_URL": "https://api.customer.com",
    "ADL_COOKIE_SCOPE": "host_only",
    "ADL_REFRESH_COOKIE_MAX_AGE_SECONDS": "2592000",
}


@dataclass(frozen=True, slots=True)
class _PresentationModule:
    module: ModuleType
    runtime_factory: Callable[..., object]
    handle: Callable[[dict[str, Any], object], dict[str, Any]]
    handler: Callable[[dict[str, Any], object], dict[str, Any]]


@pytest.fixture(scope="module")
def presentation_module() -> _PresentationModule:
    settings_without_client_secret.cache_clear()
    sys.modules.pop("adapters.inbound.presentation_handlers.handler", None)
    with patch.dict("os.environ", _ENV, clear=False):
        module: ModuleType = importlib.import_module(
            "adapters.inbound.presentation_handlers.handler"
        )
    settings_without_client_secret.cache_clear()
    return _PresentationModule(
        module,
        cast("Callable[..., object]", module.PresentationRuntime),
        cast(
            "Callable[[dict[str, Any], object], dict[str, Any]]",
            module.handle,
        ),
        cast(
            "Callable[[dict[str, Any], object], dict[str, Any]]",
            # Only the request-correlation layer is kept: it is what gives the handler its one
            # server-minted identifier, while the tracing and Lambda-context layers are skipped.
            returns_correlation(module.LOGGER)(inspect.unwrap(module.handler)),
        ),
    )


def _alias(
    campaign: CampaignKey,
    name: str | None,
    channel: ChannelKey = CHANNEL,
) -> EntityAlias:
    valid_from = date(2026, 1, 1)
    valid_to = None
    return EntityAlias(
        kind=EntityKind.CAMPAIGN,
        key=campaign,
        commerce_scope=SCOPE,
        labels=() if name is None else (EffectiveLabel(name, valid_from, valid_to),),
        source_identifiers=(SourceIdentifier("fictional.campaign", campaign.value),),
        evidence=MappingEvidence("fictional-presentation-handler", 1),
        valid_from=valid_from,
        valid_to=valid_to,
        platform=PLATFORM,
        channel=channel,
        marketplace=MARKETPLACE,
    )


@dataclass
class RecordingCatalog:
    browse_result: EntityCatalogBrowseResult = field(
        default_factory=lambda: EntityCatalogBrowseResult(())
    )
    lookup_result: EntityCatalogLookupResult = field(
        default_factory=lambda: EntityCatalogLookupResult(())
    )
    browse_calls: list[EntityCatalogBrowseRequest] = field(
        default_factory=list[EntityCatalogBrowseRequest]
    )
    lookup_calls: list[EntityCatalogLookupRequest] = field(
        default_factory=list[EntityCatalogLookupRequest]
    )

    def browse(self, request: EntityCatalogBrowseRequest) -> EntityCatalogBrowseResult:
        self.browse_calls.append(request)
        return self.browse_result

    def lookup(self, request: EntityCatalogLookupRequest) -> EntityCatalogLookupResult:
        self.lookup_calls.append(request)
        return self.lookup_result

    def public_labels(self, request: PublicEntityLabelRequest) -> PublicEntityLabelResult:
        return PublicEntityLabelResult(
            tuple(
                PublicEntityLabelResolution(PublicEntityLabelOutcome.NOT_FOUND)
                for _ in request.items
            )
        )


@dataclass
class RecordingWindows:
    selected: DateRange = field(
        default_factory=lambda: DateRange(date(2026, 8, 10), date(2026, 8, 16))
    )
    completeness: PageDefaultWindowCompleteness = PageDefaultWindowCompleteness.PREFERRED
    calls: list[PageDefaultWindowRequest] = field(default_factory=list[PageDefaultWindowRequest])

    def resolve_default(self, request: PageDefaultWindowRequest) -> PageDefaultWindowResult:
        self.calls.append(request)
        return PageDefaultWindowResult(self.selected, self.completeness)


@dataclass
class RecordingCoverage:
    intervals: tuple[DateRange, ...]
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
class RecordingPresetArtifacts:
    pointer: PresetArtifactPointer | None
    body: PresetArtifactBody | None
    extra_pointers: tuple[PresetArtifactPointer, ...] = ()
    sibling_body: PresetArtifactSiblingBody | None = None
    sibling_reads: list[PresetArtifactSiblingReadRequest] = field(
        default_factory=list[PresetArtifactSiblingReadRequest]
    )
    pointer_reads: list[tuple[PresetArtifactPage, PresetArtifactVariant]] = field(
        default_factory=lambda: cast("list[tuple[PresetArtifactPage, PresetArtifactVariant]]", [])
    )
    roster_reads: list[PresetArtifactPointerRosterRequest] = field(
        default_factory=lambda: cast("list[PresetArtifactPointerRosterRequest]", [])
    )
    body_reads: list[PresetArtifactPointer] = field(
        default_factory=lambda: cast("list[PresetArtifactPointer]", [])
    )

    def _match(
        self, page: PresetArtifactPage, variant: PresetArtifactVariant
    ) -> PresetArtifactPointer | None:
        for candidate in (self.pointer, *self.extra_pointers):
            if candidate is not None and (candidate.page, candidate.variant) == (page, variant):
                return candidate
        return None

    def current(
        self, page: PresetArtifactPage, variant: PresetArtifactVariant
    ) -> PresetArtifactPointer | None:
        self.pointer_reads.append((page, variant))
        return self._match(page, variant)

    def roster(
        self, request: PresetArtifactPointerRosterRequest
    ) -> PresetArtifactPointerRosterResult:
        self.roster_reads.append(request)
        return PresetArtifactPointerRosterResult(
            request,
            tuple(self._match(request.page, variant) for variant in request.variants),
        )

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactBody:
        self.body_reads.append(pointer)
        if self.body is None:
            raise AssertionError("a missing pointer must not read a body")
        return self.body

    def write_sibling(self, body: PresetArtifactSiblingBody) -> object:
        del body
        raise AssertionError("serve must not write sibling bodies")

    def read_sibling(self, request: PresetArtifactSiblingReadRequest) -> PresetArtifactSiblingBody:
        self.sibling_reads.append(request)
        if self.sibling_body is None:
            raise AssertionError("no sibling body was configured")
        return self.sibling_body


def _campaign_preset_artifact(
    *, identity: str = "publication-1"
) -> tuple[PresetArtifactPointer, PresetArtifactBody]:
    period = DateRange(date(2026, 8, 13), date(2026, 8, 19))
    artifact = build_campaign_portfolio(
        period=period,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=(),
        totals=(),
        trend=None,
        evidence=(CampaignListEvidence("retained campaign cohort", period, (period,)),),
        channel_filter=ONE_CHANNEL_FILTER,
    )
    revision = PresetArtifactRevision.from_publication_identities((identity,))
    body = PresetArtifactBody(
        PresetArtifactPage("campaigns"),
        PresetArtifactVariant("7d"),
        revision,
        canonical_preset_artifact_json(campaign_portfolio_artifact_payload(artifact)),
    )
    return PresetArtifactPointer(body.page, body.variant, body.revision, body.digest, 1), body


_INDEXED: dict[str, tuple[PresetArtifactSiblingCustody, ...]] = {}
"""Each fixture parent custody's index shards, by its digest, as the stage writes them."""


def _indexed_custody(
    body: PresetArtifactBody,
    header: dict[str, object],
    pairs: tuple[PresetArtifactSiblingPair, ...] = (),
) -> PresetArtifactCustodyManifest:
    """Return the parent custody the stage builds for `pairs`, keeping its shards to serve."""
    custody, shards = indexed_parent_custody(body, header, pairs)
    _INDEXED[custody.digest] = shards
    return custody


@dataclass
class RecordingCustodyStore:
    manifest: PresetArtifactCustodyManifest | None
    sibling: PresetArtifactSiblingCustody | None = None
    shards: tuple[PresetArtifactSiblingCustody, ...] | None = None
    """Overrides the index shards the fixture wrote (a tampered or missing shard)."""
    reads: list[PresetArtifactPointer] = field(
        default_factory=lambda: cast("list[PresetArtifactPointer]", [])
    )
    sibling_reads: list[PresetArtifactSiblingReadRequest] = field(
        default_factory=list[PresetArtifactSiblingReadRequest]
    )

    def write(self, manifest: PresetArtifactCustodyManifest) -> object:
        del manifest
        raise AssertionError("the serve path must never write a custody manifest")

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactCustodyManifest:
        self.reads.append(pointer)
        if self.manifest is None:
            raise AssertionError("a custody-free pointer must not read a custody manifest")
        return self.manifest

    def write_sibling(self, manifest: PresetArtifactSiblingCustody) -> object:
        del manifest
        raise AssertionError("serve must not write sibling custody")

    def read_sibling(
        self, request: PresetArtifactSiblingReadRequest
    ) -> PresetArtifactSiblingCustody:
        self.sibling_reads.append(request)
        identity = request.descriptor.identity
        if isinstance(identity, PresetArtifactIndexShardIdentity):
            # ADR-145: the parent custody's index shards, as the fixture's stage wrote them.
            shards = (
                self.shards
                if self.shards is not None
                else _INDEXED.get("" if self.manifest is None else self.manifest.digest, ())
            )
            if identity.shard_index >= len(shards):
                raise InvariantError(ErrorCode("FICTIONAL", 9), "no such index shard")
            return shards[identity.shard_index]
        if self.sibling is None:
            raise AssertionError("no sibling custody was configured")
        return self.sibling


def _campaign_preset_artifact_with_custody(
    *,
    identity: str = "publication-1",
    with_custody_digest: bool = True,
    channel: ChannelKey = CHANNEL,
) -> tuple[PresetArtifactPointer, PresetArtifactBody, PresetArtifactCustodyManifest]:
    period = DateRange(date(2026, 8, 13), date(2026, 8, 19))
    signature = campaign_display_signature("Fictional Campaign 001")
    row = portfolio_row(
        signature,
        CampaignSelectionCapability("baked:00000", datetime(2027, 1, 1, tzinfo=UTC)),
        period,
        Decimal("10"),
    )
    artifact = complete_portfolio(period, (row,))
    revision = PresetArtifactRevision.from_publication_identities((identity,))
    body = PresetArtifactBody(
        PresetArtifactPage("campaigns"),
        PresetArtifactVariant("7d"),
        revision,
        canonical_preset_artifact_json(campaign_portfolio_artifact_payload(artifact)),
    )
    custody = _indexed_custody(
        body,
        campaign_custody_manifest_payload(
            CampaignCustodyManifest(
                body.digest,
                (CampaignCustodyRow(CAMPAIGN, channel, MARKETPLACE, signature),),
            )
        ),
    )
    pointer = PresetArtifactPointer(
        body.page,
        body.variant,
        revision,
        body.digest,
        1,
        None,
        custody.digest if with_custody_digest else None,
    )
    return pointer, body, custody


RETIRED_FORMS = ("no-change-key", "no-coverage-key")
"""The retired baked forms (owner revision, 2026-10-08): comparisons without a change key
(Campaigns format 6, Retail health format 4) and bodies without the coverage key (Campaigns
format 7, Retail health format 5). The serve path refuses both."""

RETAIL_RETIRED_FORMS = (*RETIRED_FORMS, "no-fixed-statuses")
"""Retail health also retired its format-7 body: the eleven-tile roster with neither fixed
status, served with no product detail (ADR-125). The serve path refuses it too."""


def _baked(payload: dict[str, object], retired: str | None) -> dict[str, object]:
    """Return a body as baked: current, or in one retired form to prove it is refused."""
    if retired == "no-change-key":
        as_retained(payload)
    elif retired == "no-coverage-key":
        # A segment sibling carries no coverage key of its own; its parent body does.
        payload.pop("evidence_status", None)
    elif retired == "no-fixed-statuses":
        del payload["return_reasons"], payload["amc_retail_purchases"]
    return payload


def _segmented_campaign_preset_artifact(
    *, material: bool = False, retired: str | None = None, missing_spend_day: bool = False
) -> tuple[
    PresetArtifactPointer,
    PresetArtifactBody,
    PresetArtifactCustodyManifest,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
]:
    """Three campaigns; `material` sizes spend moves (about 1000 off 1000) to admit cards.

    `retired` bakes the body and sibling in one retired form (`RETIRED_FORMS`).
    `missing_spend_day` bakes a format-8 body whose source hasn't sent the window's last day of
    spend: a coverage block, and spend's change withheld on every surface (ADR-119).
    """
    period = DateRange(date(2026, 8, 13), date(2026, 8, 19))
    rows = tuple(
        portfolio_row(
            campaign_display_signature(name),
            CampaignSelectionCapability(f"baked:{index:05d}", datetime(2027, 1, 1, tzinfo=UTC)),
            period,
            Decimal(2000 - index) if material else Decimal(str(10 - index)),
            prior_spend=Decimal(1000) if material else Decimal(5),
        )
        for index, name in enumerate(("Zulu", "Alpha", "Mike"))
    )
    population = complete_portfolio(period, rows)
    if missing_spend_day:
        population = with_missing_day(population, CampaignPortfolioMetric.SPEND, period.end)
    canonical = campaign_portfolio_segment(
        population, CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"), 0
    )
    selected = campaign_portfolio_segment(
        population, CampaignPortfolioSort(CampaignPortfolioSortField.NAME, "asc"), 0
    )
    assert canonical is not None and selected is not None
    revision = PresetArtifactRevision.from_publication_identities(("segmented-publication",))
    page = PresetArtifactPage("campaigns")
    variant = PresetArtifactVariant("7d")
    identity = PresetArtifactSiblingIdentity("name-asc", 0, "all")
    sibling_body = PresetArtifactSiblingBody(
        page,
        variant,
        revision,
        identity,
        canonical_preset_artifact_json(
            _baked(campaign_portfolio_segment_payload(selected), retired)
        ),
    )
    by_signature = {
        row.signature: CampaignKey(f"campaign:segment-{index}", SCOPE, PLATFORM)
        for index, row in enumerate(rows)
    }
    sibling_manifest = CampaignCustodyManifest(
        sibling_body.digest,
        tuple(
            CampaignCustodyRow(by_signature[row.signature], CHANNEL, MARKETPLACE, row.signature)
            for row in selected.rows
        ),
    )
    sibling_custody = PresetArtifactSiblingCustody(
        page,
        variant,
        revision,
        identity,
        canonical_preset_artifact_json(campaign_custody_manifest_payload(sibling_manifest)),
    )
    body = PresetArtifactBody(
        page,
        variant,
        revision,
        canonical_preset_artifact_json(
            _baked(campaign_portfolio_artifact_payload(canonical), retired)
        ),
    )
    custody = _indexed_custody(
        body,
        campaign_custody_manifest_payload(
            CampaignCustodyManifest(
                body.digest,
                tuple(
                    CampaignCustodyRow(
                        by_signature[row.signature], CHANNEL, MARKETPLACE, row.signature
                    )
                    for row in canonical.rows
                ),
            )
        ),
        ((sibling_body, sibling_custody),),
    )
    pointer = PresetArtifactPointer(page, variant, revision, body.digest, 1, None, custody.digest)
    return pointer, body, custody, sibling_body, sibling_custody


def _campaign_metric_values() -> dict[tuple[str, str], Decimal]:
    return {}


@dataclass
class ScenarioQueries:
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])
    fail: bool = False
    empty_metrics: frozenset[str] = frozenset({"retail_revenue_shipped"})
    campaign_row_key: str = "canonical-campaign-secret"
    campaign_row_keys: tuple[str, ...] = ()
    marketplace_row_key: str = "canonical-marketplace-secret"
    suppressed_metrics: frozenset[str] = frozenset({"ad_spend"})
    truncated_metrics: frozenset[str] = frozenset({"roas_deduplicated_incl_halo_14d"})
    reach_value: Decimal = Decimal("10")
    campaign_metric_values: dict[tuple[str, str], Decimal] = field(
        default_factory=_campaign_metric_values
    )
    divergent_prior_delivery_window: DateRange | None = None
    # Per-DAY values keyed by (metric, window start): lets a scenario give the
    # current and aligned-prior windows different totals so a focus change is
    # material. Daily rows serve the value itself and aggregate reads scale it by
    # the window's day count, so both grains stay consistent. Where set, this
    # override outranks campaign_metric_values for every campaign row. The
    # default stub answers identically for both windows.
    window_metric_values: dict[tuple[str, date], Decimal] = field(
        default_factory=dict[tuple[str, date], Decimal]
    )

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        if self.fail:
            raise RuntimeError("query failed")
        plan = request.plan.logical
        metric = plan.metric.name
        suppressed = metric in self.suppressed_metrics
        if metric in self.empty_metrics or suppressed:
            rows: tuple[dict[str, str | int | Decimal | None], ...] = ()
        else:
            row: dict[str, str | int | Decimal | None] = {
                column: plan.filters.get(
                    column,
                    self.campaign_row_key
                    if column == "campaign_key"
                    else self.marketplace_row_key
                    if column == "marketplace_key"
                    else _dimension_value(column),
                )
                for column in plan.group_by
            }
            for column in plan.carried_columns:
                row[column] = "USD" if column == "currency_code" else "label"
            operands = (
                (Decimal("20"), self.reach_value)
                if metric == "avg_frequency"
                else (self.reach_value,)
                if metric == "reach_users"
                else (Decimal(plan.window.days),)
                if metric in {"impressions", "clicks"}
                else (Decimal(plan.window.days),)
                if metric == "purchases" and "date_key" not in plan.group_by
                else tuple(
                    Decimal(index + 1)
                    for index in range(2 if plan.join is not None else len(plan.measures))
                )
            )
            for index, value in enumerate(operands):
                row[measure_alias(index)] = value
            campaign_keys = (
                (self.campaign_row_keys or (self.campaign_row_key,))
                if "campaign_key" in plan.group_by and "campaign_key" not in plan.filters
                else (cast("str", row.get("campaign_key", self.campaign_row_key)),)
            )
            divergent_prior_delivery_value = (
                Decimal("2")
                if (
                    self.divergent_prior_delivery_window == plan.window
                    and metric == "impressions"
                    and "date_key" in plan.group_by
                )
                else Decimal(1)
            )
            day_value = self.window_metric_values.get((metric, plan.window.start))
            window_total = None if day_value is None else day_value * plan.window.days
            if "date_key" in plan.group_by:
                rows = tuple(
                    {
                        **row,
                        **(
                            {measure_alias(0): divergent_prior_delivery_value}
                            if metric in {"impressions", "clicks"}
                            else {}
                        ),
                        **(
                            {measure_alias(0): self.campaign_metric_values[(metric, campaign_key)]}
                            if (metric, campaign_key) in self.campaign_metric_values
                            else {}
                        ),
                        **({measure_alias(0): day_value} if day_value is not None else {}),
                        "campaign_key": campaign_key,
                        "date_key": (plan.window.start + timedelta(days=offset)).isoformat(),
                    }
                    for campaign_key in campaign_keys
                    for offset in range(plan.window.days)
                )
            elif "campaign_key" in plan.group_by:
                rows = tuple(
                    {
                        **row,
                        **(
                            {measure_alias(0): self.campaign_metric_values[(metric, campaign_key)]}
                            if (metric, campaign_key) in self.campaign_metric_values
                            else {}
                        ),
                        **({measure_alias(0): window_total} if window_total is not None else {}),
                        "campaign_key": campaign_key,
                    }
                    for campaign_key in campaign_keys
                )
            else:
                if window_total is not None:
                    row[measure_alias(0)] = window_total
                rows = (row,)
        truncated = metric in self.truncated_metrics
        return QueryResult(
            rows=rows,
            bytes_scanned=1_024,
            truncated=truncated,
            suppression=Suppression(
                rows_suppressed=suppressed,
                reason="privacy threshold" if suppressed else None,
            ),
        )


@dataclass
class MarketplaceQueries:
    values: dict[tuple[str, date], tuple[tuple[str, str | int | Decimal], ...]]
    currencies: dict[tuple[str, date, str], str] = field(default_factory=lambda: {})
    suppressed: frozenset[tuple[str, date]] = frozenset()
    truncated: frozenset[tuple[str, date]] = frozenset()
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        plan = request.plan.logical
        identity = (plan.metric.name, plan.window.start)
        suppressed = identity in self.suppressed
        selected = () if suppressed else self.values.get(identity, ())
        rows: tuple[QueryRow, ...] = tuple(
            {
                "marketplace_key": marketplace_key,
                **{
                    column: self.currencies.get(
                        (plan.metric.name, plan.window.start, marketplace_key), "USD"
                    )
                    for column in plan.carried_columns
                },
                **{measure_alias(index): value for index in range(len(plan.measures))},
            }
            for marketplace_key, value in selected
        )
        return QueryResult(
            rows=rows,
            bytes_scanned=1_024,
            truncated=identity in self.truncated,
            suppression=Suppression(
                suppressed,
                "privacy threshold" if suppressed else None,
            ),
        )


def _dimension_value(column: str) -> str:
    return {
        "ad_product_key": "canonical-ad-product-secret",
        "amazon_asin": "canonical-product-secret",
        "campaign_key": "canonical-campaign-secret",
        "channel_key": "canonical-channel-secret",
        "date_key": "2026-08-10",
        "marketplace_key": "canonical-marketplace-secret",
        "platform_key": "canonical-platform-secret",
    }.get(column, "descriptor-secret")


@dataclass
class RecordingPages:
    records: list[PageContextRecord] = field(default_factory=list[PageContextRecord])
    artifact_records: list[PageArtifactRecord] = field(default_factory=list[PageArtifactRecord])
    campaign_capability_records: list[CampaignSelectionCapabilityRecord] = field(
        default_factory=list[CampaignSelectionCapabilityRecord]
    )
    campaign_capability_reads: list[CampaignSelectionCapabilityReadRequest] = field(
        default_factory=list[CampaignSelectionCapabilityReadRequest]
    )
    capability_records: list[ChannelRowCapabilityRecord] = field(
        default_factory=list[ChannelRowCapabilityRecord]
    )
    capability_reads: list[ChannelRowCapabilityReadRequest] = field(
        default_factory=list[ChannelRowCapabilityReadRequest]
    )
    selection_records: list[PageContextRecord] = field(default_factory=list[PageContextRecord])
    selection_parent_artifact_counts: list[int] = field(default_factory=list[int])
    selection_reads: list[CampaignSelectionReadRequest | ProductSelectionReadRequest] = field(
        default_factory=list[CampaignSelectionReadRequest | ProductSelectionReadRequest]
    )
    # A Retail health serve mints its page reference and one capability per product row.
    tokens: tuple[str, ...] = (TOKEN, SECOND_SELECTION_TOKEN, THIRD_SELECTION_TOKEN)
    clock: Callable[[], datetime] = field(default=lambda: NOW)
    artifact_failure: AdlyticsError | None = None
    parent_capability_issuance_outcome: ParentCapabilityIssuanceOutcome = (
        ParentCapabilityIssuanceOutcome.ISSUED
    )
    parent_capability_requests: list[ParentCapabilityIssuanceRequest] = field(
        default_factory=list[ParentCapabilityIssuanceRequest]
    )
    writes: list[str] = field(default_factory=list[str])
    activated: list[PageContextRecord] = field(default_factory=list[PageContextRecord])

    def put(self, record: PageContextRecord) -> None:
        self.writes.append("page")
        self.records.append(record)

    def activate(self, record: PageContextRecord) -> None:
        self.activated.append(record)

    def put_artifact(self, record: PageArtifactRecord) -> None:
        self.writes.append("artifact")
        if self.artifact_failure is not None:
            raise self.artifact_failure
        self.artifact_records.append(record)

    def issue_parent_capabilities(
        self, request: ParentCapabilityIssuanceRequest
    ) -> ParentCapabilityIssuanceResult:
        self.parent_capability_requests.append(request)
        if (
            self.parent_capability_issuance_outcome
            is ParentCapabilityIssuanceOutcome.CAPACITY_REFUSED
        ):
            return ParentCapabilityIssuanceResult(self.parent_capability_issuance_outcome)
        if request.parent.parent_reference is not None:
            raise AssertionError("V2 Channel parent must be a root artifact")
        if any(
            not campaign_capability_parent_is_valid(record, request.parent)
            for record in request.capabilities
            if isinstance(record, CampaignSelectionCapabilityRecord)
        ):
            raise AssertionError("Campaign capabilities must share exact parent custody")
        if any(
            not channel_capability_parent_is_valid(record, request.parent)
            for record in request.capabilities
            if isinstance(record, ChannelRowCapabilityRecord)
        ):
            raise AssertionError("Channel capabilities must share exact parent custody")
        self.writes.append("parent-capabilities")
        self.artifact_records.append(request.parent)
        self.campaign_capability_records.extend(
            record
            for record in request.capabilities
            if isinstance(record, CampaignSelectionCapabilityRecord)
        )
        self.capability_records.extend(
            record
            for record in request.capabilities
            if isinstance(record, ChannelRowCapabilityRecord)
        )
        return ParentCapabilityIssuanceResult(ParentCapabilityIssuanceOutcome.ISSUED)

    def read_artifact(self, request: PageArtifactReadRequest) -> PageArtifactValidationResult:
        record = next(
            (item for item in self.artifact_records if item.reference == request.reference),
            None,
        )
        return validate_page_artifact(record, request)

    def put_campaign_capability(self, record: CampaignSelectionCapabilityRecord) -> None:
        parent = next(
            (
                item
                for item in self.artifact_records
                if item.reference == record.parent_artifact_reference
            ),
            None,
        )
        if parent is None or not campaign_capability_parent_is_valid(record, parent):
            raise AssertionError("campaign capability must follow its exact parent artifact")
        self.writes.append("campaign-capability")
        self.campaign_capability_records.append(record)

    def read_campaign_capability(
        self, request: CampaignSelectionCapabilityReadRequest
    ) -> CampaignSelectionCapabilityValidationResult:
        self.campaign_capability_reads.append(request)
        record = next(
            (
                item
                for item in self.campaign_capability_records
                if item.reference == request.reference
            ),
            None,
        )
        parent_record = (
            None
            if record is None
            else next(
                (
                    item
                    for item in self.artifact_records
                    if item.reference == record.parent_artifact_reference
                ),
                None,
            )
        )
        parent = (
            None
            if parent_record is None
            else validate_page_artifact(
                parent_record,
                PageArtifactReadRequest(
                    parent_record.reference,
                    request.verified_owner_id,
                    request.conversation_session_id,
                    request.at,
                ),
            ).artifact
        )
        return validate_campaign_selection_capability(record, request, parent)

    def put_channel_capability(self, record: ChannelRowCapabilityRecord) -> None:
        parent = next(
            (
                item
                for item in self.artifact_records
                if item.reference == record.parent_artifact_reference
            ),
            None,
        )
        if parent is None or not channel_capability_parent_is_valid(record, parent):
            raise AssertionError("channel capability must follow its exact parent artifact")
        self.writes.append("capability")
        self.capability_records.append(record)

    def read_channel_capability(
        self, request: ChannelRowCapabilityReadRequest
    ) -> ChannelRowCapabilityValidationResult:
        self.capability_reads.append(request)
        record = next(
            (item for item in self.capability_records if item.reference == request.reference),
            None,
        )
        parent_record = (
            None
            if record is None
            else next(
                (
                    item
                    for item in self.artifact_records
                    if item.reference == record.parent_artifact_reference
                ),
                None,
            )
        )
        parent = (
            None
            if parent_record is None
            else validate_page_artifact(
                parent_record,
                PageArtifactReadRequest(
                    parent_record.reference,
                    request.verified_owner_id,
                    request.conversation_session_id,
                    request.at,
                ),
            ).artifact
        )
        return validate_channel_row_capability(record, request, parent)

    def put_selection(self, record: PageContextRecord) -> None:
        self.selection_parent_artifact_counts.append(len(self.artifact_records))
        self.selection_records.append(record)

    def read_selection(
        self, request: CampaignSelectionReadRequest
    ) -> CampaignSelectionValidationResult:
        self.selection_reads.append(request)
        record = next(
            (item for item in self.selection_records if item.reference == request.reference),
            None,
        )
        return validate_campaign_selection(record, request)

    def read_product_selection(
        self, request: ProductSelectionReadRequest
    ) -> ProductSelectionValidationResult:
        self.selection_reads.append(request)
        record = next(
            (item for item in self.selection_records if item.reference == request.reference),
            None,
        )
        return validate_product_selection(record, request)


def _runtime(
    module: _PresentationModule,
    catalog: RecordingCatalog,
    pages: RecordingPages,
) -> object:
    minted = iter(pages.tokens)
    return module.runtime_factory(
        catalog=catalog,
        page_context=pages,
        page_artifacts=pages,
        campaign_capabilities=pages,
        scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
        clock=pages.clock,
        token_minter=lambda: next(minted),
        schemas=SCHEMAS,
        dimension_labels=DIMENSION_LABELS,
        campaign_platform_labels=CAMPAIGN_PLATFORM_LABELS,
    )


@dataclass(frozen=True, slots=True)
class _AnalyticalRuntimeRequest:
    catalog: RecordingCatalog
    pages: RecordingPages
    queries: ScenarioQueries | MarketplaceQueries
    publications: StubPublications | None = None
    windows: Any | None = None
    dimension_labels: tuple[PresentationDimensionLabel, ...] = DIMENSION_LABELS
    preset_artifacts: RecordingPresetArtifacts | None = None
    preset_custody: RecordingCustodyStore | None = None
    graph_serving: object | None = None


def _analytical_runtime(
    module: _PresentationModule,
    request: _AnalyticalRuntimeRequest,
) -> object:
    minted = iter(request.pages.tokens)
    publications = request.publications or StubPublications()
    return module.runtime_factory(
        catalog=request.catalog,
        page_context=request.pages,
        page_artifacts=request.pages,
        parent_capability_issuance=request.pages,
        campaign_capabilities=request.pages,
        channel_capabilities=request.pages,
        presentation_evidence=publications,
        scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
        clock=request.pages.clock,
        token_minter=lambda: next(minted),
        schemas=SCHEMAS,
        metric_catalog=METRIC_CATALOG,
        contracts=CONTRACTS,
        measurements=MeasurementTooling(
            queries=request.queries,
            publications=publications,
        ),
        preset_artifact_bodies=request.preset_artifacts,
        preset_artifact_pointers=request.preset_artifacts,
        preset_artifact_custody=_BakedRetailCustody()
        if request.preset_custody is None
        else request.preset_custody,
        graph_serving=request.graph_serving,
        windows=request.windows,
        dimension_labels=request.dimension_labels,
        campaign_platform_labels=CAMPAIGN_PLATFORM_LABELS,
    )


def _event(path: str, body: dict[str, object], subject: str | None = OWNER) -> dict[str, Any]:
    authorizer: dict[str, object] = (
        {}
        if subject is None
        else {
            "lambda": {
                "subject": subject,
                "correlation_id": "12345678-1234-4234-8234-123456789abc",
            }
        }
    )
    return {
        "requestContext": {
            "http": {"method": "POST", "path": path},
            "authorizer": authorizer,
        },
        "body": json.dumps(body),
    }


def _base_body() -> dict[str, object]:
    return {
        "session_id": SESSION,
        "visible_window": {"start": "2026-08-10", "end": "2026-08-16"},
        "comparison_window": None,
    }


SELECTION_RECORD = PageContextRecord(
    reference=PageContextReference(FIRST_SELECTION_TOKEN),
    snapshot=PageContextSnapshot(
        response_identity=(f"campaign-selection:{CAMPAIGN.value}:{FIRST_SELECTION_TOKEN}"),
        visible_window=DateRange(date(2026, 8, 10), date(2026, 8, 16)),
        reporting_window_status=ReportingWindowStatus.PREFERRED_DEFAULT,
        campaign=CAMPAIGN,
        channel=CHANNEL,
        marketplace=MARKETPLACE,
    ),
    verified_owner_id=OWNER,
    conversation_session_id=SESSION,
    expires_at=NOW.replace(hour=13),
)


def _body(response: dict[str, Any]) -> dict[str, object]:
    parsed: object = json.loads(cast("str", response["body"]))
    assert isinstance(parsed, dict)
    return cast("dict[str, object]", parsed)


def test_v2_campaign_parent_serves_a_preset_body_without_measurement_or_catalog_reads(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    pointer, body = _campaign_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body)
    coverage = RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),))
    catalog = RecordingCatalog()
    queries = ScenarioQueries()
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                catalog,
                pages,
                queries,
                preset_artifacts=artifacts,
                windows=coverage,
            ),
        ),
    )
    payload = _body(response)

    assert response["headers"] == {
        "Cache-Control": "no-store",
        "Content-Type": "application/json",
        "Pragma": "no-cache",
    }
    assert payload["kind"] == "page"
    assert artifacts.roster_reads == [
        PresetArtifactPointerRosterRequest(
            PresetArtifactPage("campaigns"),
            (
                PresetArtifactVariant("1d"),
                PresetArtifactVariant("7d"),
                PresetArtifactVariant("14d"),
                PresetArtifactVariant("30d"),
            ),
        )
    ]
    assert artifacts.body_reads == [pointer]
    assert queries.seen == []
    assert catalog.browse_calls == []
    assert coverage.calls == []
    assert len(pages.parent_capability_requests) == 1
    page = cast("dict[str, object]", payload["page"])
    assert page["artifact_revision"] == pages.artifact_records[0].reference.value
    assert cast("dict[str, object]", page["artifact"])["period"] == {
        "start": "2026-08-13",
        "end": "2026-08-19",
    }


def _preset_availability(payload: dict[str, object]) -> dict[str, tuple[str, str | None]]:
    temporal = cast("dict[str, object]", payload["temporal"])
    return {
        cast("str", preset["id"]): (
            cast("str", preset["availability"]),
            cast("str | None", preset["reason_code"]),
        )
        for preset in cast("list[dict[str, object]]", temporal["presets"])
    }


@pytest.mark.covers("ADR-070")
def test_v2_campaign_preset_availability_advertises_every_baked_variant(
    presentation_module: _PresentationModule,
) -> None:
    """A baked 14d pointer must not render disabled because the served 7d span misses it."""
    pages = RecordingPages()
    pointer, body = _campaign_preset_artifact()
    fourteen = replace(pointer, variant=PresetArtifactVariant("14d"))
    artifacts = RecordingPresetArtifacts(pointer, body, extra_pointers=(fourteen,))
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(), pages, ScenarioQueries(), preset_artifacts=artifacts
            ),
        ),
    )
    payload = _body(response)

    assert payload["kind"] == "page"
    availability = _preset_availability(payload)
    assert availability["7d"] == ("available", None)
    assert availability["14d"] == ("available", None)
    assert availability["1d"] == ("unavailable", "preset_unavailable")
    assert availability["30d"] == ("unavailable", "preset_unavailable")


@pytest.mark.covers("ADR-070")
def test_v2_campaign_preset_availability_disables_an_unbaked_variant(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    pointer, body = _campaign_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body)
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(), pages, ScenarioQueries(), preset_artifacts=artifacts
            ),
        ),
    )
    payload = _body(response)

    assert payload["kind"] == "page"
    availability = _preset_availability(payload)
    assert availability["7d"] == ("available", None)
    assert availability["14d"] == ("unavailable", "preset_unavailable")


@pytest.mark.covers("ADR-070")
def test_v2_campaign_preset_refusal_still_advertises_other_baked_variants(
    presentation_module: _PresentationModule,
) -> None:
    """A missing default bake must leave a baked sibling selectable, not dead-end the page."""
    pages = RecordingPages()
    pointer, _ = _campaign_preset_artifact()
    fourteen = replace(pointer, variant=PresetArtifactVariant("14d"))
    artifacts = RecordingPresetArtifacts(None, None, extra_pointers=(fourteen,))
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(), pages, ScenarioQueries(), preset_artifacts=artifacts
            ),
        ),
    )
    payload = _body(response)

    assert payload["kind"] == "window_unavailable"
    assert artifacts.body_reads == []
    availability = _preset_availability(payload)
    assert availability["7d"] == ("unavailable", "preset_unavailable")
    assert availability["14d"] == ("available", None)


@pytest.mark.covers("ADR-070")
def test_v2_retail_health_preset_availability_advertises_every_baked_variant(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    pointer, body = _retail_preset_artifact()
    fourteen = replace(pointer, variant=PresetArtifactVariant("14d"))
    artifacts = RecordingPresetArtifacts(pointer, body, extra_pointers=(fourteen,))
    event = _event(RETAIL_HEALTH_LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(), pages, ScenarioQueries(), preset_artifacts=artifacts
            ),
        ),
    )
    payload = _body(response)

    assert payload["kind"] == "page"
    availability = _preset_availability(payload)
    assert availability["7d"] == ("available", None)
    assert availability["14d"] == ("available", None)
    assert availability["1d"] == ("unavailable", "preset_unavailable")


def test_v2_campaign_parent_returns_window_unavailable_when_preset_pointer_is_missing(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    artifacts = RecordingPresetArtifacts(None, None)
    catalog = RecordingCatalog()
    queries = ScenarioQueries()
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(catalog, pages, queries, preset_artifacts=artifacts),
        ),
    )
    payload = _body(response)

    assert payload["kind"] == "window_unavailable"
    assert artifacts.body_reads == []
    assert queries.seen == []
    assert catalog.browse_calls == []
    assert pages.artifact_records == []


def test_v2_campaign_parent_rejects_a_pointer_body_identity_mismatch(
    presentation_module: _PresentationModule,
) -> None:
    pointer, _ = _campaign_preset_artifact(identity="publication-1")
    _, substituted_body = _campaign_preset_artifact(identity="publication-2")
    artifacts = RecordingPresetArtifacts(pointer, substituted_body)
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    with pytest.raises(InvariantError) as raised:
        presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    RecordingPages(),
                    ScenarioQueries(),
                    preset_artifacts=artifacts,
                ),
            ),
        )

    assert raised.value.code == presentation_module.module.ARTIFACT_MALFORMED


@pytest.mark.covers("ADR-070")
def test_v2_campaign_preset_serve_mints_row_capabilities_from_the_custody_manifest(
    presentation_module: _PresentationModule,
) -> None:
    """A custody-backed bake reissues redeemable child capabilities with no live measurement."""
    pages = RecordingPages()
    pointer, body, custody = _campaign_preset_artifact_with_custody()
    artifacts = RecordingPresetArtifacts(pointer, body)
    custody_store = RecordingCustodyStore(custody)
    catalog = RecordingCatalog()
    queries = ScenarioQueries()
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                catalog,
                pages,
                queries,
                preset_artifacts=artifacts,
                preset_custody=custody_store,
            ),
        ),
    )
    payload = _body(response)

    assert payload["kind"] == "page"
    assert custody_store.reads == [pointer]
    assert queries.seen == []
    assert catalog.browse_calls == []
    assert len(pages.parent_capability_requests) == 1
    assert len(pages.campaign_capability_records) == 1
    record = pages.campaign_capability_records[0]
    parent = pages.artifact_records[0]
    assert record.campaign == CAMPAIGN
    assert record.channel == CHANNEL
    assert record.marketplace == MARKETPLACE
    assert record.parent_artifact_reference == parent.reference
    assert record.parent_artifact_type == "CampaignPortfolioArtifact"
    assert record.expires_at == parent.expires_at
    assert record.reference.value.startswith("v2_")
    page = cast("dict[str, object]", payload["page"])
    rows = cast("list[dict[str, object]]", cast("dict[str, object]", page["artifact"])["rows"])
    capability = cast("dict[str, object]", rows[0]["capability"])
    assert capability["selection_reference"] == record.reference.value
    assert record.safe_signature == campaign_display_signature("Fictional Campaign 001")


def test_v2_campaign_preset_serve_skips_minting_for_a_pre_custody_bake(
    presentation_module: _PresentationModule,
) -> None:
    """A pointer without a custody digest keeps the page serving with no child capabilities."""
    pages = RecordingPages()
    pointer, body, custody = _campaign_preset_artifact_with_custody(with_custody_digest=False)
    artifacts = RecordingPresetArtifacts(pointer, body)
    custody_store = RecordingCustodyStore(custody)
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                pages,
                ScenarioQueries(),
                preset_artifacts=artifacts,
                preset_custody=custody_store,
            ),
        ),
    )
    payload = _body(response)

    assert payload["kind"] == "page"
    assert custody_store.reads == []
    assert len(pages.parent_capability_requests) == 1
    assert pages.parent_capability_requests[0].capabilities == ()
    assert pages.campaign_capability_records == []


def test_v2_campaign_preset_serve_rejects_a_custody_manifest_bound_to_another_body(
    presentation_module: _PresentationModule,
) -> None:
    pointer, body, _ = _campaign_preset_artifact_with_custody(identity="publication-1")
    _, _, substituted_custody = _campaign_preset_artifact_with_custody(identity="publication-2")
    pointer = replace(pointer, custody_digest=substituted_custody.digest)
    artifacts = RecordingPresetArtifacts(pointer, body)
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    with pytest.raises(InvariantError) as raised:
        presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    RecordingPages(),
                    ScenarioQueries(),
                    preset_artifacts=artifacts,
                    preset_custody=RecordingCustodyStore(substituted_custody),
                ),
            ),
        )

    assert raised.value.code == presentation_module.module.ARTIFACT_MALFORMED


def test_v2_channel_parent_retains_canonical_identity_and_safe_row_capabilities(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    coverage = RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),))
    event = _event("/api/presentation/amazon-channel-performance", {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                pages,
                ScenarioQueries(
                    suppressed_metrics=frozenset(),
                    truncated_metrics=frozenset(),
                ),
                windows=coverage,
            ),
        ),
    )
    payload = _body(response)
    page = cast("dict[str, object]", payload["page"])
    artifact = cast("dict[str, object]", page["artifact"])
    current = cast("dict[str, object]", artifact["current"])
    temporal = cast("dict[str, object]", payload["temporal"])

    assert page["page_id"] == "channel-performance"
    assert page["route"] == "/channel-performance/"
    assert page["artifact_id"] == "channel_robustness"
    assert page["actions"] == []
    assert current["period"] == cast("dict[str, object]", temporal["resolved"])["window"]
    assert payload["row_capabilities"] == [
        {
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "safe_channel_label": "Fictional channel",
            "selection_reference": SECOND_SELECTION_TOKEN,
        }
    ]
    assert pages.writes == ["page", "parent-capabilities"]
    assert len(pages.parent_capability_requests) == 1
    issuance = pages.parent_capability_requests[0]
    assert issuance.parent is pages.artifact_records[0]
    assert issuance.parent.temporal_control is not None
    stored_temporal = presentation_module.module._temporal_control_json(
        issuance.parent.temporal_control
    )
    assert stored_temporal == temporal
    assert issuance.capabilities == tuple(pages.capability_records)


def test_v2_channel_capacity_refusal_leaves_no_parent_artifact_or_row_capability(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages(
        parent_capability_issuance_outcome=ParentCapabilityIssuanceOutcome.CAPACITY_REFUSED
    )
    coverage = RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),))
    event = _event("/api/presentation/amazon-channel-performance", {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    with pytest.raises(DomainError, match="too many available row selections"):
        presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    pages,
                    ScenarioQueries(suppressed_metrics=frozenset(), truncated_metrics=frozenset()),
                    windows=coverage,
                ),
            ),
        )

    assert len(pages.records) == 1
    assert len(pages.parent_capability_requests) == 1
    assert pages.artifact_records == []
    assert pages.capability_records == []


def test_v2_wrapper_refuses_undeclared_artifact_metadata(
    presentation_module: _PresentationModule,
) -> None:
    module = cast("Any", presentation_module.module)
    coverage = RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),))
    event = _event("/api/presentation/amazon-channel-performance", {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    malformed_page = {
        "artifact": {
            "outcome": "choices",
            "period": {"end": "2026-08-21", "start": "2026-08-15"},
        },
        "artifact_revision": TOKEN,
        "artifact_type": "UndeclaredArtifact",
        "artifact_version": 1,
        "kind": "campaign_list",
    }

    with (
        patch.object(
            module,
            "_execute_request",
            return_value={"body": json.dumps({"page": malformed_page})},
        ),
        pytest.raises(InvariantError, match="artifact bytes are malformed"),
    ):
        presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    RecordingPages(),
                    ScenarioQueries(),
                    windows=coverage,
                ),
            ),
        )


def test_v2_unavailable_preset_returns_controls_without_executing_campaign_list(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    catalog = RecordingCatalog()
    coverage = RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),))
    artifacts = RecordingPresetArtifacts(None, None)
    event = _event(
        LIST_PATH,
        {
            "session_id": SESSION,
            "temporal_selection": {"kind": "preset", "preset_id": "1d"},
        },
    )
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                catalog, pages, ScenarioQueries(), preset_artifacts=artifacts, windows=coverage
            ),
        ),
    )
    payload = _body(response)

    assert response["headers"]["Cache-Control"] == "no-store"
    assert response["headers"]["Pragma"] == "no-cache"
    assert set(payload) == {
        "envelope_version",
        "kind",
        "reason_code",
        "recovery",
        "session_id",
        "temporal",
    }
    assert payload["kind"] == "window_unavailable"
    assert payload["reason_code"] == "preset_unavailable"
    assert pages.records == []
    assert catalog.browse_calls == []
    assert coverage.calls == []


def test_v2_custom_selection_returns_a_typed_unavailable_result_without_artifact_reads(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    coverage = RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),))
    event = _event(
        LIST_PATH,
        {
            "session_id": SESSION,
            "temporal_selection": {
                "kind": "custom",
                "start": "2026-08-15",
                "end": "2026-08-21",
            },
        },
    )
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(), pages, ScenarioQueries(), windows=coverage
            ),
        ),
    )
    payload = _body(response)
    temporal = cast("dict[str, object]", payload["temporal"])

    assert payload["kind"] == "window_unavailable"
    assert payload["reason_code"] == "custom_disabled"
    assert cast("dict[str, object]", temporal["custom"]) == {
        "availability": "unavailable",
        "bounds": None,
        "reason_code": "custom_disabled",
    }
    assert coverage.calls == []
    assert pages.artifact_records == []


def test_v2_resolved_empty_artifact_is_distinct_from_window_unavailable(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    pointer, body = _campaign_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body)
    coverage = RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),))
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(browse_result=EntityCatalogBrowseResult(())),
                pages,
                ScenarioQueries(),
                preset_artifacts=artifacts,
                windows=coverage,
            ),
        ),
    )
    payload = _body(response)
    page = cast("dict[str, object]", payload["page"])
    artifact = cast("dict[str, object]", page["artifact"])

    assert payload["kind"] == "page"
    assert artifact["outcome"] == "empty"
    assert page["artifact_revision"] == pages.artifact_records[0].reference.value
    assert "reason_code" not in payload


def test_v1_campaign_portfolio_refuses_a_truncated_catalog_population(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    catalog = RecordingCatalog(
        browse_result=EntityCatalogBrowseResult(
            (_alias(CAMPAIGN, "Fictional Campaign"),),
            truncated=True,
            authorized_count=2,
        )
    )
    queries = ScenarioQueries(suppressed_metrics=frozenset(), truncated_metrics=frozenset())
    coverage = RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),))
    event = _event(LIST_PATH, _base_body())
    event["headers"] = {"Accept": "application/json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(catalog, pages, queries, windows=coverage),
        ),
    )

    artifact = cast(
        "dict[str, object]", cast("dict[str, object]", _body(response)["page"])["artifact"]
    )
    assert artifact["outcome"] == "insufficient"
    assert artifact["reason_codes"] == ["truncated"]
    assert artifact["recovery_actions"] == ["change_period"]
    assert artifact["choices"] == []
    assert queries.seen == []


@pytest.mark.parametrize(
    ("headers", "body"),
    (
        (
            {"Accept": "application/vnd.adlytics.presentation-v2+json"},
            {"session_id": SESSION, "visible_window": {"start": "2026-08-15", "end": "2026-08-21"}},
        ),
        (
            {"Accept": "application/json"},
            {"session_id": SESSION, "temporal_selection": {"kind": "preset", "preset_id": "7d"}},
        ),
    ),
)
def test_v1_and_v2_reject_each_others_parent_request_grammar(
    presentation_module: _PresentationModule,
    headers: dict[str, str],
    body: dict[str, object],
) -> None:
    pages = RecordingPages()
    event = _event(LIST_PATH, body)
    event["headers"] = headers

    with pytest.raises(ValidationError, match="fields are invalid"):
        presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(RecordingCatalog(), pages, ScenarioQueries()),
            ),
        )

    assert pages.records == []
    assert pages.artifact_records == []


@pytest.mark.parametrize(
    ("headers", "expected"),
    (
        ({"Accept": "application/json"}, False),
        (
            {"accept": "APPLICATION/VND.ADLYTICS.PRESENTATION-V2+JSON; charset=utf-8"},
            True,
        ),
        (
            {"Accept": ("application/json, application/vnd.adlytics.presentation-v2+json; q=0.5")},
            True,
        ),
        ({"Accept": "application/vnd.adlytics.presentation-v2+json; q=0"}, False),
        ({"Accept": "application/vnd.adlytics.presentation-v2+json; q=invalid"}, False),
    ),
)
def test_v2_accept_negotiation_requires_an_acceptable_explicit_media_type(
    presentation_module: _PresentationModule,
    headers: dict[str, str],
    expected: bool,
) -> None:
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = headers

    assert presentation_module.module._accepts_presentation_v2(event) is expected


def test_non_v2_accept_preserves_the_legacy_campaign_response_shape(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    event = _event(LIST_PATH, _base_body())
    event["headers"] = {"Accept": "application/json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(RecordingCatalog(), pages, ScenarioQueries()),
        ),
    )
    payload = _body(response)

    assert set(payload) == {
        "page",
        "reporting_window_status",
        "session_id",
        "visible_window",
    }
    assert "envelope_version" not in payload
    assert payload["session_id"] == SESSION
    assert pages.artifact_records[0].temporal_control is None


def test_v2_accept_refuses_legacy_only_operations_without_v1_fallback(
    presentation_module: _PresentationModule,
) -> None:
    event = _event("/api/presentation/overview", {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    with pytest.raises(ValidationError, match="versioned presentation operation"):
        presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(RecordingCatalog(), RecordingPages(), ScenarioQueries()),
            ),
        )


def test_v1_campaign_child_executes_the_fixed_delivery_cohort_without_changing_v1(
    presentation_module: _PresentationModule,
) -> None:
    catalog, pages, capability = _campaign_choice_context(presentation_module)
    pages.tokens = (SECOND_SELECTION_TOKEN,)

    response = presentation_module.handle(
        _event(
            DETAIL_PATH,
            {"session_id": SESSION, "selection_reference": capability.reference.value},
        ),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(catalog, pages, ScenarioQueries()),
        ),
    )

    page = cast("dict[str, object]", _body(response)["page"])
    assert response["statusCode"] == 200
    assert page["kind"] == "campaign_detail"
    assert page["artifact_type"] == "CampaignAttribution"
    assert page["parent_revision"] == capability.parent_artifact_reference.value


@pytest.mark.covers("ADR-068")
def test_v1_campaign_child_refuses_divergent_prior_daily_delivery(
    presentation_module: _PresentationModule,
) -> None:
    catalog = RecordingCatalog(
        browse_result=EntityCatalogBrowseResult((_alias(CAMPAIGN, "Fictional Campaign"),)),
        lookup_result=EntityCatalogLookupResult((_alias(CAMPAIGN, "Fictional Campaign"),)),
    )
    pages = RecordingPages(
        tokens=(TOKEN, FIRST_SELECTION_TOKEN, SECOND_SELECTION_TOKEN),
        clock=lambda: NOW,
    )
    queries = ScenarioQueries(
        campaign_row_key=CAMPAIGN.value,
        marketplace_row_key=MARKETPLACE.value,
        suppressed_metrics=frozenset(),
        truncated_metrics=frozenset(),
    )
    publications = StubPublications()
    coverage = RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),))
    parent = _event(LIST_PATH, _base_body())
    parent["headers"] = {"Accept": "application/json"}
    parent_response = presentation_module.handle(
        parent,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                catalog, pages, queries, publications=publications, windows=coverage
            ),
        ),
    )
    parent_page = cast("dict[str, object]", _body(parent_response)["page"])
    parent_artifact = cast("dict[str, object]", parent_page["artifact"])
    parent_period = cast("dict[str, str]", parent_artifact["period"])
    current_period = DateRange(
        date.fromisoformat(parent_period["start"]),
        date.fromisoformat(parent_period["end"]),
    )
    queries.divergent_prior_delivery_window = DateRange(
        current_period.start - timedelta(days=current_period.days),
        current_period.start - timedelta(days=1),
    )
    capability = pages.campaign_capability_records[0]
    pages.tokens = (SECOND_SELECTION_TOKEN,)
    child = _event(
        DETAIL_PATH,
        {"session_id": SESSION, "selection_reference": capability.reference.value},
    )
    child["headers"] = {"Accept": "application/json"}

    response = presentation_module.handle(
        child,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                catalog, pages, queries, publications=publications, windows=coverage
            ),
        ),
    )

    page = cast("dict[str, object]", _body(response)["page"])
    artifact = cast("dict[str, object]", page["artifact"])
    assert response["statusCode"] == 200
    assert page["kind"] == "campaign_detail"
    assert artifact["outcome"] == "selected"
    assert pages.artifact_records[-1].parent_reference == capability.parent_artifact_reference


def test_v2_capability_redemption_returns_closed_parent_refresh_without_reading_custody(
    presentation_module: _PresentationModule,
) -> None:
    catalog, pages, capability = _campaign_choice_context(presentation_module)
    event = _event(
        DETAIL_PATH,
        {"session_id": SESSION, "selection_reference": capability.reference.value},
    )
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    with pytest.raises(DomainError) as raised:
        presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(catalog, pages, ScenarioQueries()),
            ),
        )

    assert str(raised.value.code) == "ADLY-PRESENTATION-0004"
    assert (
        raised.value.user_visible_description == "Refresh the parent page before selecting again."
    )
    assert pages.campaign_capability_reads == []
    assert pages.capability_reads == []
    assert catalog.lookup_calls == []

    response = presentation_module.handler(event, object())

    assert response["statusCode"] == 409
    assert _body(response) == {
        "code": "ADLY-PRESENTATION-0004",
        "error": "Refresh the parent page before selecting again.",
        "error_class": "domain",
    }


def test_v2_capability_redemption_rejects_temporal_fields_before_refresh(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    event = _event(
        DETAIL_PATH,
        {
            "session_id": SESSION,
            "selection_reference": FIRST_SELECTION_TOKEN,
            "temporal_selection": {"kind": "preset", "preset_id": "7d"},
        },
    )
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    with pytest.raises(ValidationError, match="fields are invalid") as raised:
        presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(RecordingCatalog(), pages, ScenarioQueries()),
            ),
        )

    assert raised.value.context == {"outcome": "invalid_fields", "n": 1}
    assert pages.campaign_capability_reads == []
    assert pages.capability_reads == []


def test_product_selection_refuses_wrong_custody_expiry_and_cross_kind_replay() -> None:
    product_record = replace(
        SELECTION_RECORD,
        snapshot=replace(
            SELECTION_RECORD.snapshot,
            campaign=None,
            channel=None,
            product=PRODUCT,
        ),
    )
    cases = (
        ProductSelectionReadRequest(product_record.reference, "other-owner", SESSION, NOW),
        ProductSelectionReadRequest(product_record.reference, OWNER, "other-session", NOW),
        ProductSelectionReadRequest(
            product_record.reference, OWNER, SESSION, product_record.expires_at
        ),
    )
    for request in cases:
        assert (
            validate_product_selection(product_record, request).outcome
            is ResolutionOutcome.INVALID_CONTEXT
        )
    assert (
        validate_product_selection(
            SELECTION_RECORD,
            ProductSelectionReadRequest(SELECTION_RECORD.reference, OWNER, SESSION, NOW),
        ).outcome
        is ResolutionOutcome.INVALID_CONTEXT
    )
    assert (
        validate_campaign_selection(
            product_record,
            CampaignSelectionReadRequest(product_record.reference, OWNER, SESSION, NOW),
        ).outcome
        is ResolutionOutcome.INVALID_CONTEXT
    )


def test_authorizer_subject_owns_parent_bound_campaign_choices_and_body_cannot_supply_owner(
    presentation_module: _PresentationModule,
) -> None:
    aliases = (
        _alias(CAMPAIGN, "Fictional Campaign"),
        _alias(OTHER_CAMPAIGN, "Other Campaign"),
    )
    catalog = RecordingCatalog(browse_result=EntityCatalogBrowseResult(aliases))
    local_zone = timezone(timedelta(hours=-7))
    now = datetime(2026, 8, 19, 5, tzinfo=local_zone)
    pages = RecordingPages(
        tokens=(FIRST_SELECTION_TOKEN, SECOND_SELECTION_TOKEN, TOKEN),
        clock=lambda: now,
    )

    response = presentation_module.handle(
        _event(LIST_PATH, _base_body()),
        _runtime(presentation_module, catalog, pages),
    )
    payload = _body(response)

    assert response["statusCode"] == 200
    page_record = pages.records[0]
    assert page_record.expires_at == now + timedelta(hours=1)
    page = cast("dict[str, object]", payload["page"])
    assert page["kind"] == "campaign_list"
    assert page["artifact_type"] == "CampaignAttribution"
    assert page["artifact_version"] == 1
    assert page["artifact_revision"] == page_record.reference.value
    assert "parent_revision" not in page
    artifact = cast("dict[str, object]", page["artifact"])
    assert artifact["outcome"] == "choices"
    assert artifact["kind"] == "campaign_list"
    choices = cast("list[dict[str, object]]", artifact["choices"])
    assert [
        cast("dict[str, object]", choice["signature"])["effective_name"] for choice in choices
    ] == ["Fictional Campaign", "Other Campaign"]
    assert [
        cast("dict[str, object]", choice["capability"])["selection_reference"] for choice in choices
    ] == [SECOND_SELECTION_TOKEN, TOKEN]
    assert len(pages.artifact_records) == 1
    assert pages.artifact_records[0].reference == page_record.reference
    assert len(pages.campaign_capability_records) == 2
    assert all(
        record.parent_artifact_reference == page_record.reference
        for record in pages.campaign_capability_records
    )
    assert all(record.verified_owner_id == OWNER for record in pages.campaign_capability_records)
    assert all(
        record.conversation_session_id == SESSION for record in pages.campaign_capability_records
    )
    assert pages.writes == [
        "page",
        "artifact",
        "campaign-capability",
        "campaign-capability",
    ]
    assert pages.selection_records == []
    assert CAMPAIGN.value not in json.dumps(payload)
    assert OTHER_CAMPAIGN.value not in json.dumps(payload)
    _assert_selector_safe(payload)

    with pytest.raises(ValidationError, match="fields are invalid"):
        presentation_module.handle(
            _event(LIST_PATH, {**_base_body(), "owner": "body-owner"}),
            _runtime(presentation_module, catalog, RecordingPages()),
        )


def test_complete_safe_signature_collision_persists_no_campaign_capability(
    presentation_module: _PresentationModule,
) -> None:
    aliases = (
        _alias(CAMPAIGN, "Same safe campaign"),
        _alias(OTHER_CAMPAIGN, "Same safe campaign"),
    )
    pages = RecordingPages(
        tokens=(TOKEN, FIRST_SELECTION_TOKEN, SECOND_SELECTION_TOKEN),
        clock=lambda: NOW,
    )

    response = presentation_module.handle(
        _event(LIST_PATH, _base_body()),
        _runtime(
            presentation_module,
            RecordingCatalog(browse_result=EntityCatalogBrowseResult(aliases)),
            pages,
        ),
    )

    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    assert artifact["outcome"] == "ambiguous"
    assert artifact["choices"] == []
    assert artifact["reason_codes"] == ["safe_signature_collision"]
    assert pages.campaign_capability_records == []
    assert pages.writes == ["page", "artifact"]
    assert FIRST_SELECTION_TOKEN not in json.dumps(artifact)
    assert SECOND_SELECTION_TOKEN not in json.dumps(artifact)


@dataclass(frozen=True, slots=True)
class _CampaignListOutcomeCase:
    browse_result: EntityCatalogBrowseResult
    outcome: str
    reason: str
    recovery: list[str]


@pytest.mark.parametrize(
    "case",
    [
        _CampaignListOutcomeCase(
            EntityCatalogBrowseResult(()),
            "empty",
            "empty_covered",
            ["change_period"],
        ),
        _CampaignListOutcomeCase(
            EntityCatalogBrowseResult((_alias(CAMPAIGN, "Fictional campaign"),), True, 2),
            "insufficient",
            "truncated",
            ["change_period"],
        ),
    ],
)
def test_campaign_list_empty_and_truncated_states_remain_typed_without_capabilities(
    presentation_module: _PresentationModule,
    case: _CampaignListOutcomeCase,
) -> None:
    pages = RecordingPages(tokens=(TOKEN,), clock=lambda: NOW)

    response = presentation_module.handle(
        _event(LIST_PATH, _base_body()),
        _runtime(
            presentation_module,
            RecordingCatalog(browse_result=case.browse_result),
            pages,
        ),
    )

    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    assert artifact["outcome"] == case.outcome
    assert artifact["reason_codes"] == [case.reason]
    assert artifact["recovery_actions"] == case.recovery
    assert artifact["choices"] == []
    assert pages.campaign_capability_records == []
    assert pages.writes == ["page", "artifact"]


def test_authorizer_subject_is_required_before_body_or_catalog_read(
    presentation_module: _PresentationModule,
) -> None:
    catalog = RecordingCatalog()
    pages = RecordingPages()

    with pytest.raises(Exception, match="authorizer subject"):
        presentation_module.handle(
            _event(LIST_PATH, {"malformed": True}, subject=None),
            _runtime(presentation_module, catalog, pages),
        )

    assert catalog.browse_calls == []
    assert pages.records == []


def _campaign_choice_context(
    presentation_module: _PresentationModule,
) -> tuple[RecordingCatalog, RecordingPages, CampaignSelectionCapabilityRecord]:
    alias = _alias(CAMPAIGN, "Fictional Campaign")
    catalog = RecordingCatalog(
        browse_result=EntityCatalogBrowseResult((alias,)),
        lookup_result=EntityCatalogLookupResult((alias,)),
    )
    pages = RecordingPages(
        tokens=(TOKEN, FIRST_SELECTION_TOKEN),
        clock=lambda: NOW,
    )
    presentation_module.handle(
        _event(LIST_PATH, _base_body()),
        _runtime(presentation_module, catalog, pages),
    )
    assert len(pages.campaign_capability_records) == 1
    return catalog, pages, pages.campaign_capability_records[0]


def test_detail_redeems_parent_bound_capability_and_builds_exact_child_artifact(
    presentation_module: _PresentationModule,
) -> None:
    catalog, pages, capability = _campaign_choice_context(presentation_module)
    pages.tokens = (SECOND_SELECTION_TOKEN,)
    publications = StubPublications()
    queries = ScenarioQueries(
        suppressed_metrics=frozenset(),
        truncated_metrics=frozenset(),
    )

    response = presentation_module.handle(
        _event(
            DETAIL_PATH,
            {
                "session_id": SESSION,
                "selection_reference": capability.reference.value,
            },
        ),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                catalog,
                pages,
                queries,
                publications=publications,
            ),
        ),
    )
    payload = _body(response)

    assert response["statusCode"] == 200
    page = cast("dict[str, object]", payload["page"])
    artifact = cast("dict[str, object]", page["artifact"])
    assert page["kind"] == "campaign_detail"
    assert page["artifact_type"] == "CampaignAttribution"
    assert page["artifact_version"] == 1
    assert page["artifact_revision"] == SECOND_SELECTION_TOKEN
    assert page["parent_revision"] == capability.parent_artifact_reference.value
    assert artifact["kind"] == "campaign_detail"
    assert artifact["outcome"] == "selected"
    assert artifact["period"] == {"end": "2026-08-16", "start": "2026-08-10"}
    assert artifact["prior_period"] == {
        "end": "2026-08-09",
        "start": "2026-08-03",
    }
    summary = cast("dict[str, object]", artifact["summary"])
    purchases = cast("dict[str, object]", summary["purchases"])
    assert purchases["value"] == 7
    assert purchases["units"] == {"currency_code": None, "kind": "count"}
    assert purchases["grain"] == ["period", "campaign", "marketplace"]
    series = cast("dict[str, object]", artifact["purchase_series"])
    assert len(cast("list[object]", series["points"])) == 7
    assert series["grain"] == ["date", "campaign", "marketplace"]
    assert len(cast("list[object]", artifact["evidence_by_slot"])) == 7
    assert len(publications.inspections) == 1
    assert len(publications.inspections[0].plans) == 7
    assert publications.seen == []
    assert len(queries.seen) == 7
    assert [request.plan.logical.metric.name for request in queries.seen] == [
        "purchases",
        "purchases",
        "roas_attributed_14d",
        "roas_attributed_14d",
        "ad_spend",
        "ad_spend",
        "purchases",
    ]
    child = pages.artifact_records[-1]
    assert child.reference == PageContextReference(SECOND_SELECTION_TOKEN)
    assert child.parent_reference == pages.artifact_records[0].reference
    assert child.expires_at <= pages.artifact_records[0].expires_at
    assert CAMPAIGN.value not in json.dumps(payload)
    _assert_selector_safe(payload)


@dataclass(frozen=True, slots=True)
class _CampaignMissingSlotCase:
    absent_index: int
    slot: str
    condition: str


@pytest.mark.parametrize(
    "case",
    [
        _CampaignMissingSlotCase(0, "purchases", "missing_current_publication"),
        _CampaignMissingSlotCase(
            1,
            "prior_purchases",
            "missing_comparator_publication",
        ),
    ],
)
def test_campaign_missing_current_and_comparator_slots_are_distinct_and_not_queried(
    presentation_module: _PresentationModule,
    case: _CampaignMissingSlotCase,
) -> None:
    catalog, pages, capability = _campaign_choice_context(presentation_module)
    pages.tokens = (SECOND_SELECTION_TOKEN,)
    publications = StubPublications(inspection_absent_indices=frozenset({case.absent_index}))
    queries = ScenarioQueries(
        suppressed_metrics=frozenset(),
        truncated_metrics=frozenset(),
    )

    response = presentation_module.handle(
        _event(
            DETAIL_PATH,
            {"session_id": SESSION, "selection_reference": capability.reference.value},
        ),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                catalog,
                pages,
                queries,
                publications=publications,
            ),
        ),
    )

    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    evidence = {
        cast("str", item["slot"]): item
        for item in cast("list[dict[str, object]]", artifact["evidence_by_slot"])
    }
    assert artifact["outcome"] == "insufficient"
    assert evidence[case.slot]["condition"] == case.condition
    assert evidence[case.slot]["row_count"] == 0
    assert len(publications.inspections) == 1
    assert publications.seen == []
    assert len(queries.seen) == 6


@pytest.mark.parametrize(
    ("signal", "expected"),
    [("empty", "empty_covered"), ("suppressed", "suppressed"), ("truncated", "truncated")],
)
def test_campaign_present_purchase_conditions_remain_typed(
    presentation_module: _PresentationModule,
    signal: str,
    expected: str,
) -> None:
    catalog, pages, capability = _campaign_choice_context(presentation_module)
    pages.tokens = (SECOND_SELECTION_TOKEN,)
    queries = ScenarioQueries(
        empty_metrics=frozenset({"purchases"}) if signal == "empty" else frozenset(),
        suppressed_metrics=(frozenset({"purchases"}) if signal == "suppressed" else frozenset()),
        truncated_metrics=(frozenset({"purchases"}) if signal == "truncated" else frozenset()),
    )
    publications = StubPublications()

    response = presentation_module.handle(
        _event(
            DETAIL_PATH,
            {"session_id": SESSION, "selection_reference": capability.reference.value},
        ),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                catalog,
                pages,
                queries,
                publications=publications,
            ),
        ),
    )

    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    evidence = cast("list[dict[str, object]]", artifact["evidence_by_slot"])
    purchase_evidence = next(item for item in evidence if item["slot"] == "purchases")
    assert artifact["outcome"] == "insufficient"
    assert purchase_evidence["condition"] == expected
    assert publications.seen == []
    assert len(publications.inspections) == 1


def test_campaign_current_coverage_hole_remains_distinct_from_empty_or_missing(
    presentation_module: _PresentationModule,
) -> None:
    catalog, pages, capability = _campaign_choice_context(presentation_module)
    pages.tokens = (SECOND_SELECTION_TOKEN,)
    covered = DateRange(date(2026, 8, 10), date(2026, 8, 12))
    hole = DateRange(date(2026, 8, 13), date(2026, 8, 16))
    coverage = Coverage(
        fact_name="fct_attributed_conversion",
        window=covered,
        freshness=FreshnessStatus.FRESH,
        measurement_basis=MeasurementBasis.AD_ATTRIBUTED,
        valid_grains=("campaign", "marketplace"),
        loaded_at=NOW - timedelta(hours=2),
        intervals=(covered,),
        holes=(hole,),
    )
    publications = StubPublications(inspection_coverage_by_index={0: coverage})

    response = presentation_module.handle(
        _event(
            DETAIL_PATH,
            {"session_id": SESSION, "selection_reference": capability.reference.value},
        ),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                catalog,
                pages,
                ScenarioQueries(
                    suppressed_metrics=frozenset(),
                    truncated_metrics=frozenset(),
                ),
                publications=publications,
            ),
        ),
    )

    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    evidence = cast("list[dict[str, object]]", artifact["evidence_by_slot"])
    purchases = next(item for item in evidence if item["slot"] == "purchases")
    assert purchases["condition"] == "coverage_hole"
    assert purchases["coverage_holes"] == [{"end": "2026-08-16", "start": "2026-08-13"}]
    reasons = cast("list[str]", artifact["insufficiency_reasons"])
    assert "coverage_hole" in reasons
    assert "empty_covered" not in reasons
    assert "missing_current_publication" not in reasons
    assert publications.seen == []


def test_detail_rejects_canonical_keys_and_windows_as_unknown_before_selection_read(
    presentation_module: _PresentationModule,
) -> None:
    catalog = RecordingCatalog()
    pages = RecordingPages(selection_records=[SELECTION_RECORD])
    detail: dict[str, object] = {
        "session_id": SESSION,
        "selection_reference": FIRST_SELECTION_TOKEN,
        "campaign_key": CAMPAIGN.value,
        "visible_window": _base_body()["visible_window"],
    }

    with pytest.raises(ValidationError, match="fields are invalid") as raised:
        presentation_module.handle(
            _event(DETAIL_PATH, detail),
            _runtime(presentation_module, catalog, pages),
        )

    assert raised.value.context == {
        "missing": [],
        "outcome": "invalid_fields",
        "n": 2,
    }
    assert "campaign_key" not in str(raised.value.context)
    assert "visible_window" not in str(raised.value.context)
    assert pages.campaign_capability_reads == []
    assert pages.selection_reads == []
    assert catalog.lookup_calls == []
    assert pages.records == []


def test_missing_and_hidden_parent_bound_detail_are_identical_and_never_query_or_mint(
    presentation_module: _PresentationModule,
) -> None:
    descriptions: list[str] = []
    for lookup_result in (
        EntityCatalogLookupResult(()),
        EntityCatalogLookupResult((), hidden_direct_match=True),
    ):
        catalog, pages, capability = _campaign_choice_context(presentation_module)
        catalog.lookup_result = lookup_result
        pages.tokens = (SECOND_SELECTION_TOKEN,)
        writes_before = tuple(pages.writes)
        queries = ScenarioQueries()
        with pytest.raises(ValidationError) as raised:
            presentation_module.handle(
                _event(
                    DETAIL_PATH,
                    {
                        "session_id": SESSION,
                        "selection_reference": capability.reference.value,
                    },
                ),
                _analytical_runtime(
                    presentation_module,
                    _AnalyticalRuntimeRequest(catalog, pages, queries),
                ),
            )
        descriptions.append(raised.value.user_visible_description)
        assert tuple(pages.writes) == writes_before
        assert queries.seen == []

    assert descriptions == [
        "The requested campaign is not available.",
        "The requested campaign is not available.",
    ]


@pytest.mark.parametrize(
    "case",
    ("invalid", "expired", "wrong_custody", "drifted_signature"),
)
def test_invalid_expired_or_drifted_campaign_capability_is_one_nondisclosing_outcome(
    presentation_module: _PresentationModule,
    case: str,
) -> None:
    catalog, pages, capability = _campaign_choice_context(presentation_module)
    reference = capability.reference
    if case == "invalid":
        reference = PageContextReference(THIRD_SELECTION_TOKEN)
    elif case == "expired":
        pages.campaign_capability_records[0] = replace(capability, expires_at=NOW)
    elif case == "wrong_custody":
        pages.campaign_capability_records[0] = replace(
            capability,
            conversation_session_id="another-session",
        )
    else:
        catalog.lookup_result = EntityCatalogLookupResult(
            (_alias(CAMPAIGN, "Renamed fictional campaign"),)
        )
    pages.tokens = (SECOND_SELECTION_TOKEN,)
    writes_before = tuple(pages.writes)
    queries = ScenarioQueries()

    with pytest.raises(ValidationError) as raised:
        presentation_module.handle(
            _event(
                DETAIL_PATH,
                {
                    "session_id": SESSION,
                    "selection_reference": reference.value,
                },
            ),
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(catalog, pages, queries),
            ),
        )

    assert str(raised.value.code) == "ADLY-PRESENTATION-0001"
    assert raised.value.user_visible_description == "The requested campaign is not available."
    assert tuple(pages.writes) == writes_before
    assert queries.seen == []


def test_malformed_campaign_capability_is_nondisclosing_before_any_read(
    presentation_module: _PresentationModule,
) -> None:
    catalog = RecordingCatalog()
    pages = RecordingPages()
    with pytest.raises(ValidationError) as raised:
        presentation_module.handle(
            _event(
                DETAIL_PATH,
                {"session_id": SESSION, "selection_reference": "not-opaque"},
            ),
            _runtime(presentation_module, catalog, pages),
        )

    assert raised.value.user_visible_description == "The requested campaign is not available."
    assert pages.campaign_capability_reads == []
    assert catalog.lookup_calls == []


def _assert_selector_safe(value: object) -> None:
    forbidden = {
        "ad_product_key",
        "amazon_asin",
        "campaign_key",
        "channel_key",
        "marketplace_key",
        "platform_key",
        "canonical-ad-product-secret",
        "canonical-product-secret",
        "canonical-campaign-secret",
        "canonical-channel-secret",
        "canonical-marketplace-secret",
        "canonical-platform-secret",
    }
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        assert not forbidden.intersection(key for key in mapping if isinstance(key, str))
        for item in mapping.values():
            _assert_selector_safe(item)
    elif isinstance(value, list):
        for item in cast("list[object]", value):
            _assert_selector_safe(item)
    elif isinstance(value, str):
        assert value not in forbidden


@dataclass(frozen=True, slots=True)
class _AnalyticalPageCase:
    path: str
    expected_availability: tuple[str, ...]
    expected_query_grain: frozenset[tuple[str, ...]]
    expected_page_grain: tuple[str, ...]


@pytest.mark.parametrize(
    "case",
    [
        _AnalyticalPageCase(
            "/api/presentation/overview",
            ("available", "empty"),
            frozenset({("marketplace_key",)}),
            ("period", "marketplace"),
        ),
    ],
)
def test_successful_analytical_pages_are_typed_complete_and_selector_safe(
    presentation_module: _PresentationModule,
    case: _AnalyticalPageCase,
) -> None:
    queries = ScenarioQueries()
    pages = RecordingPages()

    response = presentation_module.handle(
        _event(case.path, _base_body()),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(RecordingCatalog(), pages, queries),
        ),
    )
    payload = _body(response)

    assert response["statusCode"] == 200
    page = cast("dict[str, object]", payload["page"])
    measurements = cast("list[dict[str, object]]", page["measurements"])
    assert tuple(item["availability"] for item in measurements) == case.expected_availability
    for measurement in measurements:
        answer = cast("dict[str, object]", measurement["answer"])
        assert set(answer) == {"data", "metadata"}
        data = cast("dict[str, object]", answer["data"])
        metadata = cast("dict[str, object]", answer["metadata"])
        assert tuple(cast("list[str]", data["grain"])) == case.expected_page_grain
        assert {
            "actual_coverage_end",
            "actual_coverage_intervals",
            "actual_coverage_start",
            "computed_at",
            "coverage_end",
            "coverage_holes",
            "coverage_start",
            "freshness_reason",
            "freshness_status",
            "loaded_at",
            "qualified_dq",
            "qualifiers",
            "requested_coverage_intervals",
            "row_count",
            "semantic_qualifiers",
            "source_timezone",
            "suppression",
            "truncated",
        } == set(metadata)
        if measurement["availability"] == "empty":
            assert data["values"] == []
            assert data["units"] is None
            assert data["population_total"] is None
        elif "marketplace" in case.expected_page_grain:
            rows = cast("list[dict[str, object]]", data["values"])
            assert rows
            assert all(row["marketplace"] == "United States" for row in rows)
            assert all("marketplace_key" not in row for row in rows)
        suppression = cast("dict[str, object]", metadata["suppression"])
        assert set(suppression) == {"reason", "rows_suppressed"}
    assert frozenset(request.plan.logical.group_by for request in queries.seen) == (
        case.expected_query_grain
    )
    _assert_selector_safe(payload)
    assert len(pages.records) == 1


@dataclass(frozen=True, slots=True)
class _DataHealthResponseRequest:
    pages: RecordingPages
    queries: ScenarioQueries
    publications: StubPublications | None = None
    body: dict[str, object] | None = None
    v2: bool = False
    windows: Any | None = None
    dimension_labels: tuple[PresentationDimensionLabel, ...] = DIMENSION_LABELS


def _data_health_response(
    presentation_module: _PresentationModule,
    request: _DataHealthResponseRequest,
) -> tuple[dict[str, Any], list[MeasurementToolRequest], StubPublications, ScenarioQueries]:
    module = cast("Any", presentation_module.module)
    original_prepare = module.prepare_batch
    captured: list[MeasurementToolRequest] = []
    publications = request.publications or StubPublications()

    def capture_prepare(
        requests: tuple[MeasurementToolRequest, ...],
    ) -> Any:
        captured.extend(requests)
        return original_prepare(requests)

    with patch.object(module, "prepare_batch", side_effect=capture_prepare):
        event = _event("/api/presentation/data-health", request.body or _base_body())
        if request.v2:
            event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
        response = presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    request.pages,
                    request.queries,
                    publications=publications,
                    windows=request.windows,
                    dimension_labels=request.dimension_labels,
                ),
            ),
        )
    return response, captured, publications, request.queries


def _data_health_coverage(
    *,
    freshness: FreshnessStatus = FreshnessStatus.FRESH,
    intervals: tuple[DateRange, ...] | None = None,
    holes: tuple[DateRange, ...] = (),
) -> Coverage:
    period = DateRange(date(2026, 8, 10), date(2026, 8, 16))
    actual = (period,) if intervals is None else intervals
    return Coverage(
        fact_name="fct_ad_exposure",
        window=actual[0] if len(actual) == 1 else None,
        freshness=freshness,
        measurement_basis=MeasurementBasis.AD_ATTRIBUTED,
        valid_grains=("date", "channel", "campaign", "ad_product", "marketplace"),
        loaded_at=NOW - timedelta(days=14 if freshness is FreshnessStatus.STALE else 1),
        intervals=actual,
        holes=holes,
    )


def test_data_health_healthy_uses_one_inspection_and_emits_exact_safe_artifact(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    response, captured, publications, queries = _data_health_response(
        presentation_module,
        _DataHealthResponseRequest(
            pages,
            ScenarioQueries(
                suppressed_metrics=frozenset(),
                truncated_metrics=frozenset(),
            ),
        ),
    )
    payload = _body(response)
    page = cast("dict[str, object]", payload["page"])
    artifact = cast("dict[str, object]", page["artifact"])
    checks = cast("list[dict[str, object]]", artifact["checks"])
    reach, frequency = checks
    reach_rows = cast("list[dict[str, object]]", reach["evidence_rows"])
    frequency_rows = cast("list[dict[str, object]]", frequency["evidence_rows"])
    reach_row = reach_rows[0]
    frequency_row = frequency_rows[0]
    expected_grain = ["date", "channel", "campaign", "ad_product", "marketplace"]
    expected_dimensions = [
        {"name": "date", "value": "2026-08-10"},
        {"name": "channel", "value": "Fictional channel"},
        {"name": "campaign", "value": "Campaign 0001"},
        {"name": "ad_product", "value": "Ad product 0001"},
        {"name": "marketplace", "value": "United States"},
    ]

    assert response["statusCode"] == 200
    assert page["kind"] == "data_health_detail"
    assert page["artifact_type"] == "DataHealthDetail"
    assert page["artifact_version"] == 1
    assert "measurements" not in page
    assert "publication_status" not in page
    assert artifact["state"] == "healthy"
    assert artifact["serving_provenance"] == {
        "evidence_digest": cast("dict[str, object]", artifact["serving_provenance"])[
            "evidence_digest"
        ],
        "inspection_attested": True,
        "population": "Sponsored-ad exposed users",
        "source": "Amazon Marketing Cloud",
    }
    digest = cast(
        "str",
        cast("dict[str, object]", artifact["serving_provenance"])["evidence_digest"],
    )
    assert digest.startswith("sha256:")
    assert tuple(check["check"] for check in checks) == ("reach_users", "avg_frequency")
    assert all(check["base_condition"] == "available" for check in checks)
    assert reach["grain"] == expected_grain
    assert reach["units"] == {"currency_code": None, "kind": "count"}
    assert reach_row["dimensions"] == expected_dimensions
    assert reach_row["value"] == 10
    assert frequency["grain"] == expected_grain
    assert frequency["units"] == {"currency_code": None, "kind": "ratio"}
    assert frequency_row["dimensions"] == expected_dimensions
    assert frequency_row["value"] == "2.0000"
    assert frequency_row["reach_denominator_ref"] == reach_row["evidence_ref"]
    assert [request.metric_name for request in captured] == [
        "reach_users",
        "avg_frequency",
    ]
    assert all(
        request.grains == ("date", "channel", "campaign", "ad_product", "marketplace")
        for request in captured
    )
    assert len(publications.inspections) == 1
    assert len(publications.inspections[0].plans) == 2
    assert publications.seen == []
    assert [request.plan.logical.metric.name for request in queries.seen] == [
        "reach_users",
        "avg_frequency",
    ]
    assert pages.writes == ["page", "artifact"]
    assert pages.artifact_records[0].artifact.payload == artifact
    assert "unit-primary" not in json.dumps(page)
    _assert_selector_safe(payload)


def test_data_health_stale_and_dq_evidence_is_qualified(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    publications = StubPublications(
        coverage=_data_health_coverage(freshness=FreshnessStatus.STALE),
        qualified_dq=("Fictional source caveat",),
    )
    response, _captured, _inspected, _queries = _data_health_response(
        presentation_module,
        _DataHealthResponseRequest(
            pages,
            ScenarioQueries(
                suppressed_metrics=frozenset(),
                truncated_metrics=frozenset(),
            ),
            publications,
        ),
    )
    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    checks = cast("list[dict[str, object]]", artifact["checks"])

    assert artifact["state"] == "qualified"
    assert all(check["base_condition"] == "available" for check in checks)
    assert all(check["freshness_qualifiers"] == ["stale", "qualified_dq"] for check in checks)
    assert all(check["dq"] == ["Fictional source caveat"] for check in checks)
    assert all(check["grain"] == list(DATA_HEALTH_GRAIN) for check in checks)


@pytest.mark.parametrize(
    ("signal", "expected_condition"),
    [
        ("coverage_hole", "coverage_hole"),
        ("empty", "empty_covered"),
        ("suppressed", "suppressed"),
        ("truncated", "truncated"),
    ],
)
def test_data_health_present_blockers_are_insufficient_closed_conditions(
    presentation_module: _PresentationModule,
    signal: str,
    expected_condition: str,
) -> None:
    queries = ScenarioQueries(
        empty_metrics=(
            frozenset({"retail_revenue_shipped", "reach_users"})
            if signal == "empty"
            else frozenset({"retail_revenue_shipped"})
        ),
        suppressed_metrics=(frozenset({"reach_users"}) if signal == "suppressed" else frozenset()),
        truncated_metrics=(frozenset({"reach_users"}) if signal == "truncated" else frozenset()),
    )
    publications: StubPublications | None = None
    if signal == "coverage_hole":
        covered = DateRange(date(2026, 8, 10), date(2026, 8, 12))
        hole = DateRange(date(2026, 8, 13), date(2026, 8, 16))
        publications = StubPublications(
            coverage=_data_health_coverage(intervals=(covered,), holes=(hole,))
        )
    response, _captured, _inspected, _queries = _data_health_response(
        presentation_module,
        _DataHealthResponseRequest(RecordingPages(), queries, publications),
    )
    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    checks = cast("list[dict[str, object]]", artifact["checks"])
    reach = checks[0]

    assert artifact["state"] == "insufficient"
    assert reach["base_condition"] == expected_condition
    assert reach["usable"] is False
    assert reach["grain"] == list(DATA_HEALTH_GRAIN)
    assert reach["units"] == {"currency_code": None, "kind": "count"}
    if signal == "suppressed":
        assert reach["suppression"] == {
            "reason": "privacy threshold",
            "rows_suppressed": True,
        }
        assert reach["recovery"] == {
            "action_id": None,
            "no_self_service_reason": "source_privacy_threshold",
        }
    elif signal == "truncated":
        assert reach["truncated"] is True
    elif signal == "empty":
        assert reach["evidence_rows"] == []
        assert reach["recovery"] == {
            "action_id": None,
            "no_self_service_reason": "covered_no_rows",
        }
    else:
        assert reach["coverage_holes"] == [{"end": "2026-08-16", "start": "2026-08-13"}]


def test_data_health_absent_slot_is_no_current_publication_and_is_not_queried(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    publications = StubPublications(inspection_absent_indices=frozenset({1}))
    response, captured, inspected, queries = _data_health_response(
        presentation_module,
        _DataHealthResponseRequest(
            pages,
            ScenarioQueries(
                suppressed_metrics=frozenset(),
                truncated_metrics=frozenset(),
            ),
            publications,
        ),
    )
    page = cast("dict[str, object]", _body(response)["page"])
    artifact = cast("dict[str, object]", page["artifact"])
    checks = cast("list[dict[str, object]]", artifact["checks"])
    frequency = checks[1]

    assert page["kind"] == "data_health_detail"
    assert "measurements" not in page
    assert "publication-status" not in json.dumps(page)
    assert artifact["state"] == "no_coverage"
    assert frequency["base_condition"] == "no_current_publication"
    assert frequency["base_condition"] != "never_loaded"
    assert frequency["actual_coverage"] == []
    assert frequency["evidence_rows"] == []
    assert len(captured) == 2
    assert len(inspected.inspections) == 1
    assert inspected.seen == []
    assert [request.plan.logical.metric.name for request in queries.seen] == ["reach_users"]
    assert pages.writes == ["page", "artifact"]


def test_data_health_represented_zero_remains_zero_and_blocks_frequency(
    presentation_module: _PresentationModule,
) -> None:
    response, _captured, _inspected, _queries = _data_health_response(
        presentation_module,
        _DataHealthResponseRequest(
            RecordingPages(),
            ScenarioQueries(
                suppressed_metrics=frozenset(),
                truncated_metrics=frozenset(),
                reach_value=Decimal("0"),
            ),
        ),
    )
    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    reach, frequency = cast("list[dict[str, object]]", artifact["checks"])
    reach_row = cast("list[dict[str, object]]", reach["evidence_rows"])[0]

    assert artifact["state"] == "insufficient"
    assert reach_row["value"] == 0
    assert reach_row["units"] == {"currency_code": None, "kind": "count"}
    assert reach["grain"] == list(DATA_HEALTH_GRAIN)
    assert frequency["base_condition"] == "blocked_by_reach"
    assert frequency["evidence_rows"] == []
    assert frequency["units"] == {"currency_code": None, "kind": "ratio"}


def test_data_health_inspection_failure_remains_a_page_issue_without_queries(
    presentation_module: _PresentationModule,
) -> None:
    failure = DependencyError(
        ErrorCode("PUBLICATION", 1),
        "Fictional publication inspection is unavailable.",
    )
    pages = RecordingPages()
    publications = StubPublications(failure=failure)
    queries = ScenarioQueries(
        suppressed_metrics=frozenset(),
        truncated_metrics=frozenset(),
    )

    with pytest.raises(DependencyError) as raised:
        _data_health_response(
            presentation_module,
            _DataHealthResponseRequest(pages, queries, publications),
        )

    assert raised.value is failure
    assert len(publications.inspections) == 1
    assert publications.seen == []
    assert queries.seen == []
    assert pages.records == []
    assert pages.artifact_records == []


def test_data_health_unmapped_selector_remains_an_invariant_page_issue(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    queries = ScenarioQueries(
        suppressed_metrics=frozenset(),
        truncated_metrics=frozenset(),
    )

    with pytest.raises(InvariantError) as raised:
        _data_health_response(
            presentation_module,
            _DataHealthResponseRequest(
                pages,
                queries,
                dimension_labels=(),
            ),
        )

    assert str(raised.value.code) == "ADLY-PRESENTATION-0003"
    assert len(queries.seen) == 2
    assert pages.records == []
    assert pages.artifact_records == []


@dataclass(frozen=True, slots=True)
class _ChannelResponseRequest:
    pages: RecordingPages
    publications: StubPublications | None = None
    body: dict[str, object] | None = None
    windows: Any | None = None


def _channel_response(
    presentation_module: _PresentationModule,
    request: _ChannelResponseRequest,
) -> tuple[dict[str, Any], list[MeasurementToolRequest], StubPublications, ScenarioQueries]:
    module = cast("Any", presentation_module.module)
    original_prepare = module.prepare_batch
    captured: list[MeasurementToolRequest] = []
    queries = ScenarioQueries(suppressed_metrics=frozenset(), truncated_metrics=frozenset())
    selected_publications = request.publications or StubPublications()

    def capture_prepare(
        requests: tuple[MeasurementToolRequest, ...],
    ) -> Any:
        captured.extend(requests)
        return original_prepare(requests)

    with patch.object(module, "prepare_batch", side_effect=capture_prepare):
        response = presentation_module.handle(
            _event(
                "/api/presentation/amazon-channel-performance",
                request.body or _base_body(),
            ),
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    request.pages,
                    queries,
                    publications=selected_publications,
                    windows=request.windows,
                ),
            ),
        )
    return response, captured, selected_publications, queries


def test_channel_performance_emits_three_exact_variants_in_typed_envelopes(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    response, captured, publications, queries = _channel_response(
        presentation_module, _ChannelResponseRequest(pages)
    )
    payload = _body(response)
    page = cast("dict[str, object]", payload["page"])
    artifact = cast("dict[str, object]", page["artifact"])

    assert page["kind"] == "channel_attribution_robustness"
    assert page["artifact_type"] == "ChannelAttributionRobustness"
    assert page["artifact_version"] == 2
    assert page["artifact_revision"] == pages.artifact_records[0].reference.value
    assert "parent_revision" not in page
    assert set(artifact) == {
        "boundaries",
        "comparison",
        "current",
        "period_relation",
        "selected_channel_daily",
    }
    current = cast("dict[str, object]", artifact["current"])
    comparison = cast("dict[str, object]", artifact["comparison"])
    assert current["period"] == {"end": "2026-08-16", "start": "2026-08-10"}
    assert comparison["period"] == {"end": "2026-08-09", "start": "2026-08-03"}
    assert current["models"] == ["first_touch_v1", "last_touch_v1", "linear_v1"]
    assert comparison["models"] == current["models"]
    assert current["outcome"] == "robust_leader"
    assert comparison["outcome"] == "robust_leader"
    assert artifact["period_relation"] == "leader_persisted"
    assert artifact["selected_channel_daily"] is None
    assert payload["row_capabilities"] == [
        {
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "safe_channel_label": "Fictional channel",
            "selection_reference": SECOND_SELECTION_TOKEN,
        }
    ]
    assert len(pages.artifact_records) == 1
    assert len(pages.capability_records) == 1
    capability = pages.capability_records[0]
    assert capability.parent_artifact_reference == pages.artifact_records[0].reference
    assert capability.parent_artifact_type == "ChannelAttributionRobustness"
    assert capability.parent_artifact_version == 2
    assert capability.verified_owner_id == OWNER
    assert capability.conversation_session_id == SESSION
    assert capability.channel == ChannelKey("canonical-channel-secret")
    assert capability.marketplace == MarketplaceKey("canonical-marketplace-secret")
    assert capability.current_period == DateRange(date(2026, 8, 10), date(2026, 8, 16))
    assert capability.model_ids == ("first_touch_v1", "last_touch_v1", "linear_v1")
    assert capability.reporting_window_status is ReportingWindowStatus.USER_SELECTED
    assert pages.writes == ["page", "artifact", "capability"]
    assert [request.grains for request in captured] == [
        ("channel", "marketplace"),
        ("channel", "marketplace"),
        ("channel", "marketplace"),
        ("channel", "marketplace"),
    ]
    assert [request.filters for request in captured[1:]] == [
        {"attribution_model_key": "first_touch_v1"},
        {"attribution_model_key": "last_touch_v1"},
        {"attribution_model_key": "linear_v1"},
    ]
    assert len(publications.inspections) == 1
    assert publications.seen == []
    assert len(publications.inspections[0].plans) == 8
    assert len(queries.seen) == 8
    _assert_selector_safe(payload)


def test_channel_missing_inspection_slot_is_period_insufficiency_without_second_selection(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    publications = StubPublications(inspection_absent_indices=frozenset({0}))

    response, _captured, inspected, queries = _channel_response(
        presentation_module,
        _ChannelResponseRequest(pages, publications=publications),
    )

    payload = _body(response)
    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", payload["page"])["artifact"],
    )
    current = cast("dict[str, object]", artifact["current"])
    comparison = cast("dict[str, object]", artifact["comparison"])
    assert current["outcome"] == "insufficient"
    assert current["insufficiency_reasons"] == ["missing_publication"]
    assert current["rows"] == []
    assert comparison["outcome"] == "robust_leader"
    assert artifact["period_relation"] == "not_comparable"
    assert payload["row_capabilities"] == []
    assert len(inspected.inspections) == 1
    assert inspected.seen == []
    assert len(queries.seen) == 7
    assert pages.writes == ["page", "artifact"]


def test_artifact_storage_failure_returns_no_success_response(
    presentation_module: _PresentationModule,
) -> None:
    failure = DependencyError(
        ErrorCode("PAGE_ARTIFACT", 1),
        "Fictional artifact storage is unavailable.",
    )
    pages = RecordingPages(artifact_failure=failure)

    with pytest.raises(DependencyError) as raised:
        _channel_response(presentation_module, _ChannelResponseRequest(pages))

    assert raised.value is failure
    assert pages.writes == ["page", "artifact"]
    assert len(pages.records) == 1
    assert pages.artifact_records == []


def test_channel_selection_writes_immutable_child_with_parent_bytes_preserved(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages(tokens=(TOKEN, SECOND_SELECTION_TOKEN, THIRD_SELECTION_TOKEN))
    parent_response, _captured, _parent_publications, _parent_queries = _channel_response(
        presentation_module,
        _ChannelResponseRequest(
            pages,
            body={"session_id": SESSION},
            windows=RecordingWindows(
                completeness=PageDefaultWindowCompleteness.PARTIAL,
            ),
        ),
    )
    parent_payload = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(parent_response)["page"])["artifact"],
    )
    parent_bytes = pages.artifact_records[0].artifact.payload_json
    capability = pages.capability_records[0]
    assert capability.reporting_window_status is ReportingWindowStatus.PARTIAL_DEFAULT
    pages.tokens = (THIRD_SELECTION_TOKEN,)
    publications = StubPublications()
    queries = ScenarioQueries(suppressed_metrics=frozenset(), truncated_metrics=frozenset())

    response = presentation_module.handle(
        _event(
            "/api/presentation/amazon-channel-performance",
            {
                "session_id": SESSION,
                "selection_reference": capability.reference.value,
            },
        ),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                pages,
                queries,
                publications=publications,
            ),
        ),
    )

    payload = _body(response)
    page = cast("dict[str, object]", payload["page"])
    child_payload = cast("dict[str, object]", page["artifact"])
    assert "row_capabilities" not in payload
    assert payload["reporting_window_status"] == "partial_default"
    assert pages.records[-1].snapshot.reporting_window_status is (
        ReportingWindowStatus.PARTIAL_DEFAULT
    )
    assert page["artifact_version"] == 2
    assert page["artifact_revision"] == THIRD_SELECTION_TOKEN
    assert page["parent_revision"] == TOKEN
    assert len(publications.inspections) == 1
    assert publications.seen == []
    assert len(publications.inspections[0].plans) == 4
    assert {plan.plan.group_by for plan in publications.inspections[0].plans} == {
        ("date_key", "channel_key", "marketplace_key")
    }
    assert len(queries.seen) == 4
    assert len(pages.artifact_records) == 2
    child_record = pages.artifact_records[1]
    assert child_record.reference == PageContextReference(THIRD_SELECTION_TOKEN)
    assert child_record.parent_reference == PageContextReference(TOKEN)
    assert child_record.expires_at <= pages.artifact_records[0].expires_at
    assert pages.artifact_records[0].artifact.payload_json == parent_bytes
    for field_name in ("boundaries", "comparison", "current", "period_relation"):
        assert child_payload[field_name] == parent_payload[field_name]
    daily = cast("dict[str, object]", child_payload["selected_channel_daily"])
    assert daily["channel"] == "Fictional channel"
    assert daily["marketplace"] == "United States"
    assert daily["period"] == {"end": "2026-08-16", "start": "2026-08-10"}
    assert daily["models"] == ["first_touch_v1", "last_touch_v1", "linear_v1"]
    assert pages.writes == [
        "page",
        "artifact",
        "capability",
        "page",
        "artifact",
    ]
    _assert_selector_safe(payload)


def test_channel_selection_missing_daily_slot_returns_explicit_insufficiency(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages(tokens=(TOKEN, SECOND_SELECTION_TOKEN))
    _channel_response(presentation_module, _ChannelResponseRequest(pages))
    capability = pages.capability_records[0]
    pages.tokens = (THIRD_SELECTION_TOKEN,)
    publications = StubPublications(inspection_absent_indices=frozenset({0}))
    queries = ScenarioQueries(suppressed_metrics=frozenset(), truncated_metrics=frozenset())

    response = presentation_module.handle(
        _event(
            "/api/presentation/amazon-channel-performance",
            {
                "session_id": SESSION,
                "selection_reference": capability.reference.value,
            },
        ),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                pages,
                queries,
                publications=publications,
            ),
        ),
    )

    daily = cast(
        "dict[str, object]",
        cast(
            "dict[str, object]",
            cast("dict[str, object]", _body(response)["page"])["artifact"],
        )["selected_channel_daily"],
    )
    assert daily["outcome"] == "insufficient"
    assert "missing_publication" in cast("list[str]", daily["insufficiency_reasons"])
    assert len(cast("list[object]", daily["days"])) == 7
    assert len(publications.inspections) == 1
    assert publications.seen == []
    assert len(queries.seen) == 3


@pytest.mark.parametrize("case", ("invalid", "expired", "wrong_custody"))
def test_channel_selection_invalid_capability_is_nondisclosing(
    presentation_module: _PresentationModule,
    case: str,
) -> None:
    pages = RecordingPages(tokens=(TOKEN, SECOND_SELECTION_TOKEN))
    _channel_response(presentation_module, _ChannelResponseRequest(pages))
    reference = pages.capability_records[0].reference
    if case == "invalid":
        reference = PageContextReference(FIRST_SELECTION_TOKEN)
    elif case == "expired":
        pages.capability_records[0] = replace(
            pages.capability_records[0],
            expires_at=NOW,
        )
    else:
        pages.capability_records[0] = replace(
            pages.capability_records[0],
            conversation_session_id="different-session",
        )
    writes_before = tuple(pages.writes)
    artifacts_before = tuple(pages.artifact_records)
    pages.tokens = (THIRD_SELECTION_TOKEN,)
    queries = ScenarioQueries()

    with pytest.raises(ValidationError) as raised:
        presentation_module.handle(
            _event(
                "/api/presentation/amazon-channel-performance",
                {
                    "session_id": SESSION,
                    "selection_reference": reference.value,
                },
            ),
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(RecordingCatalog(), pages, queries),
            ),
        )

    assert str(raised.value.code) == "ADLY-PRESENTATION-0001"
    assert raised.value.user_visible_description == "The requested channel is not available."
    assert tuple(pages.writes) == writes_before
    assert tuple(pages.artifact_records) == artifacts_before
    assert queries.seen == []


@pytest.mark.parametrize(
    ("queries", "expected"),
    [
        (
            ScenarioQueries(
                suppressed_metrics=frozenset({"purchases"}),
                truncated_metrics=frozenset(),
            ),
            "suppressed",
        ),
        (
            ScenarioQueries(
                suppressed_metrics=frozenset(),
                truncated_metrics=frozenset({"purchases"}),
            ),
            "partial",
        ),
    ],
)
def test_analytical_availability_preserves_suppression_and_truncation(
    presentation_module: _PresentationModule,
    queries: ScenarioQueries,
    expected: str,
) -> None:
    response = presentation_module.handle(
        _event("/api/presentation/overview", _base_body()),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(RecordingCatalog(), RecordingPages(), queries),
        ),
    )
    measurements = cast(
        "list[dict[str, object]]",
        cast("dict[str, object]", _body(response)["page"])["measurements"],
    )

    assert measurements[0]["availability"] == expected
    answer = cast("dict[str, object]", measurements[0]["answer"])
    metadata = cast("dict[str, object]", answer["metadata"])
    suppression = cast("dict[str, object]", metadata["suppression"])
    assert suppression["rows_suppressed"] is (expected == "suppressed")
    assert metadata["truncated"] is (expected == "partial")


def test_query_failure_never_mints_page_context(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    queries = ScenarioQueries(fail=True)

    with pytest.raises(RuntimeError, match="query failed"):
        presentation_module.handle(
            _event("/api/presentation/overview", _base_body()),
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(RecordingCatalog(), pages, queries),
            ),
        )

    assert pages.records == []


def test_publication_failure_never_queries_or_mints_page_context(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    queries = ScenarioQueries()
    publications = StubPublications(
        failure=DependencyError(
            ErrorCode("PUBLICATION", 1),
            "Required publication coverage is unavailable.",
        )
    )

    with pytest.raises(DependencyError):
        presentation_module.handle(
            _event("/api/presentation/overview", _base_body()),
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    pages,
                    queries,
                    publications,
                ),
            ),
        )

    assert queries.seen == []
    assert pages.records == []


def test_omitted_window_uses_latest_page_wide_source_coverage_before_querying(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    queries = ScenarioQueries()
    windows = RecordingWindows()

    response = presentation_module.handle(
        _event("/api/presentation/overview", {"session_id": SESSION}),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                pages,
                queries,
                windows=windows,
            ),
        ),
    )
    payload = _body(response)

    assert payload["visible_window"] == {"start": "2026-08-10", "end": "2026-08-16"}
    assert payload["reporting_window_status"] == "preferred_default"
    assert len(windows.calls) == 1
    request = windows.calls[0]
    assert request.preferred_duration_days == 7
    sources = {source for requirement in request.requirements for source in requirement.sources}
    assert sources == {
        ("amc", "amazon_attributed_events_by_conversion_time"),
        ("vendor_central", "GET_VENDOR_SALES_REPORT"),
    }
    assert queries.seen
    assert len(pages.records) == 1


def test_omitted_window_returns_one_exact_qualified_partial_default(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    queries = ScenarioQueries()
    windows = RecordingWindows(
        selected=DateRange(date(2026, 8, 10), date(2026, 8, 12)),
        completeness=PageDefaultWindowCompleteness.PARTIAL,
    )

    response = presentation_module.handle(
        _event("/api/presentation/overview", {"session_id": SESSION}),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                pages,
                queries,
                windows=windows,
            ),
        ),
    )
    payload = _body(response)

    assert payload["visible_window"] == {"start": "2026-08-10", "end": "2026-08-12"}
    assert payload["reporting_window_status"] == "partial_default"
    assert (
        pages.records[-1].snapshot.reporting_window_status is ReportingWindowStatus.PARTIAL_DEFAULT
    )


def test_custom_period_uses_the_exact_user_selected_window_for_every_measurement(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    queries = ScenarioQueries()
    body: dict[str, object] = {
        "session_id": SESSION,
        "visible_window": {"start": "2026-08-10", "end": "2026-08-15"},
    }

    response = presentation_module.handle(
        _event("/api/presentation/overview", body),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(RecordingCatalog(), pages, queries),
        ),
    )
    payload = _body(response)

    assert payload["reporting_window_status"] == "user_selected"
    assert payload["visible_window"] == body["visible_window"]
    assert queries.seen
    assert {seen.plan.logical.window for seen in queries.seen} == {
        DateRange(date(2026, 8, 10), date(2026, 8, 15))
    }
    assert pages.records[-1].snapshot.reporting_window_status is ReportingWindowStatus.USER_SELECTED


@dataclass(frozen=True, slots=True)
class _ErrorEnvelopeCase:
    error: AdlyticsError
    status: int
    safe_error: str
    error_class: str


@pytest.mark.parametrize(
    "case",
    [
        _ErrorEnvelopeCase(
            DomainError(ErrorCode("FICTIONAL", 1), "Fictional domain outcome."),
            409,
            "Fictional domain outcome.",
            "domain",
        ),
        _ErrorEnvelopeCase(
            ValidationError(ErrorCode("FICTIONAL", 2), "Fictional validation outcome."),
            400,
            "Fictional validation outcome.",
            "validation",
        ),
        _ErrorEnvelopeCase(
            DependencyError(ErrorCode("FICTIONAL", 3), "Fictional dependency detail."),
            503,
            "The request could not be completed. The issue has been recorded.",
            "dependency",
        ),
        _ErrorEnvelopeCase(
            InvariantError(ErrorCode("FICTIONAL", 4), "Fictional invariant detail."),
            500,
            "The request could not be completed. The issue has been recorded.",
            "invariant",
        ),
    ],
)
def test_handler_returns_exact_safe_classified_error_envelopes(
    presentation_module: _PresentationModule,
    case: _ErrorEnvelopeCase,
) -> None:
    module = presentation_module.module
    with (
        patch.object(module, "handle", side_effect=case.error),
        patch.object(module, "report_error") as reported,
    ):
        response = presentation_module.handler(
            _event("/api/presentation/overview", _base_body()),
            object(),
        )

    assert response["statusCode"] == case.status
    assert _body(response) == {
        "error": case.safe_error,
        "code": str(case.error.code),
        "error_class": case.error_class,
    }
    assert reported.call_count == 1
    assert reported.call_args.args[0] is case.error


def test_handler_returns_stable_invariant_envelope_for_unexpected_error(
    presentation_module: _PresentationModule,
) -> None:
    error = RuntimeError("caller-controlled secret must not escape")
    module = presentation_module.module
    with (
        patch.object(module, "handle", side_effect=error),
        patch.object(module, "report_error") as reported,
    ):
        response = presentation_module.handler(
            _event("/api/presentation/overview", _base_body()),
            object(),
        )

    assert response["statusCode"] == 500
    assert _body(response) == {
        "error": "The request could not be completed. The issue has been recorded.",
        "code": "ADLY-UNEXPECTED-0000",
        "error_class": "invariant",
    }
    assert reported.call_count == 1
    assert reported.call_args.args[0] is error


@pytest.mark.parametrize(
    ("body", "representation", "result_kind", "counts"),
    (
        (
            {
                "page": {
                    "artifact": {
                        "checks": [{"opaque": "hidden-check"}],
                        "choices": [{"capability": "hidden-choice"}],
                        "rows": [{"marketplace": "hidden-marketplace"}],
                    },
                    "measurements": [{"value": "hidden-measurement"}],
                },
                "row_capabilities": [{"selection_reference": "hidden-row-capability"}],
                "session_id": "hidden-session",
            },
            "v1",
            "page",
            {
                "artifact_row_count": 1,
                "check_count": 1,
                "choice_count": 1,
                "measurement_count": 1,
                "row_capability_count": 1,
            },
        ),
        (
            {
                "envelope_version": 2,
                "kind": "page",
                "page": {"artifact": {"choices": []}, "measurements": []},
                "session_id": "hidden-v2-session",
                "temporal": {"resolved": {"window": "hidden-window"}},
            },
            "v2",
            "page",
            {
                "artifact_row_count": 0,
                "check_count": 0,
                "choice_count": 0,
                "measurement_count": 0,
                "row_capability_count": 0,
            },
        ),
        (
            {
                "envelope_version": 2,
                "kind": "window_unavailable",
                "reason_code": "hidden-reason",
                "recovery": {"action_id": "hidden-action"},
                "session_id": "hidden-unavailable-session",
                "temporal": {"resolved": {"window": "hidden-window"}},
            },
            "v2",
            "window_unavailable",
            {
                "artifact_row_count": 0,
                "check_count": 0,
                "choice_count": 0,
                "measurement_count": 0,
                "row_capability_count": 0,
            },
        ),
    ),
)
def test_handler_emits_safe_typed_terminal_telemetry_for_completed_presentations(  # noqa: PLR0913, PLR0917
    presentation_module: _PresentationModule,
    body: dict[str, object],
    representation: str,
    result_kind: str,
    counts: dict[str, int],
) -> None:
    module = presentation_module.module
    hostile_event = _event(
        LIST_PATH,
        {
            "session_id": "hidden-request-session",
            "selection_reference": "hidden-request-capability",
        },
    )
    response = module._json(200, body)
    traced = MagicMock()
    process_span = MagicMock()
    traced.return_value.__enter__.return_value = process_span

    with (
        patch.object(module, "handle", return_value=response),
        patch.object(module, "report_operation") as reported,
        patch.object(module, "span", traced),
    ):
        returned = presentation_module.handler(hostile_event, object())

    assert {key: value for key, value in returned.items() if key != "headers"} == {
        key: value for key, value in response.items() if key != "headers"
    }
    assert returned["headers"] == {
        **response["headers"],
        "x-correlation-id": "12345678-1234-4234-8234-123456789abc",
    }
    assert vars(module.handler)["__trace_boundaries__"] == (module.PRESENTATION_REQUEST,)
    traced.assert_called_once_with(module.PRESENTATION_REQUEST, operation="presentation.request")
    process_span.record.assert_called_once_with(
        outcome="completed",
        status=200,
        representation=representation,
        result_kind=result_kind,
    )
    assert reported.call_count == 2
    started = reported.call_args_list[0].args[0]
    terminal = reported.call_args_list[1].args[0]
    assert started.phase == "start"
    assert started.outcome == "started"
    assert started.details == {
        "correlation_id": "12345678-1234-4234-8234-123456789abc",
        "duration_ms": started.details["duration_ms"],
    }
    assert terminal.phase == "terminal"
    assert terminal.outcome == "completed"
    assert terminal.details == {
        "correlation_id": "12345678-1234-4234-8234-123456789abc",
        "duration_ms": terminal.details["duration_ms"],
        "representation": representation,
        "result_kind": result_kind,
        "status": 200,
        **counts,
    }
    assert 0 <= started.details["duration_ms"] <= 3_600_000
    assert 0 <= terminal.details["duration_ms"] <= 3_600_000
    logged = repr((started.details, terminal.details))
    for secret in (
        "hidden-action",
        "hidden-check",
        "hidden-choice",
        "hidden-marketplace",
        "hidden-measurement",
        "hidden-request-capability",
        "hidden-request-session",
        "hidden-row-capability",
        "hidden-session",
        "hidden-unavailable-session",
        "hidden-v2-session",
        "hidden-window",
    ):
        assert secret not in logged


@pytest.mark.parametrize(
    ("error", "status", "outcome", "result_kind"),
    (
        (
            ValidationError(ErrorCode("FICTIONAL", 9), "hidden validation secret"),
            400,
            "refused",
            "validation",
        ),
        (
            DependencyError(ErrorCode("FICTIONAL", 10), "hidden dependency secret"),
            503,
            "failed",
            "error",
        ),
        (RuntimeError("hidden runtime secret"), 500, "failed", "error"),
    ),
)
def test_handler_emits_safe_typed_terminal_telemetry_for_errors(  # noqa: PLR0913, PLR0917
    presentation_module: _PresentationModule,
    error: BaseException,
    status: int,
    outcome: str,
    result_kind: str,
) -> None:
    module = presentation_module.module

    with (
        patch.object(module, "handle", side_effect=error),
        patch.object(module, "report_error"),
        patch.object(module, "report_operation") as reported,
    ):
        response = presentation_module.handler(_event(LIST_PATH, _base_body()), object())

    terminal = reported.call_args_list[1].args[0]
    assert response["statusCode"] == status
    assert terminal.phase == "terminal"
    assert terminal.outcome == outcome
    assert terminal.details == {
        "artifact_row_count": 0,
        "check_count": 0,
        "choice_count": 0,
        "correlation_id": "12345678-1234-4234-8234-123456789abc",
        "duration_ms": terminal.details["duration_ms"],
        "measurement_count": 0,
        "representation": "none",
        "result_kind": result_kind,
        "row_capability_count": 0,
        "status": status,
    }
    assert 0 <= terminal.details["duration_ms"] <= 3_600_000
    assert "hidden" not in repr(terminal.details)


def test_paths_and_methods_are_exact(presentation_module: _PresentationModule) -> None:
    runtime = _runtime(presentation_module, RecordingCatalog(), RecordingPages())
    event = _event(f"{LIST_PATH}/extra", _base_body())

    with pytest.raises(ValidationError, match="operation is not available"):
        presentation_module.handle(event, runtime)

    cast("dict[str, object]", cast("dict[str, object]", event["requestContext"])["http"])[
        "method"
    ] = "GET"
    with pytest.raises(ValidationError, match="operation is not available"):
        presentation_module.handle(event, runtime)


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/presentation/overview", _base_body()),
        ("/api/presentation/amazon-channel-performance", _base_body()),
        (
            DETAIL_PATH,
            {"session_id": SESSION, "selection_reference": FIRST_SELECTION_TOKEN},
        ),
        ("/api/presentation/marketplace-comparison", _base_body()),
        ("/api/presentation/data-health", _base_body()),
    ],
)
def test_analytical_requests_refuse_browser_authored_execution_authority(
    presentation_module: _PresentationModule,
    path: str,
    body: dict[str, object],
) -> None:
    forbidden = {
        "metric": "ad_spend",
        "grain": ["campaign"],
        "filter": {"campaign_key": CAMPAIGN.value},
        "selector": {"marketplace_key": MARKETPLACE.value},
        "artifact_revision": TOKEN,
        "sql": "select 1",
        "campaign_key": CAMPAIGN.value,
    }

    with pytest.raises(ValidationError, match="fields are invalid") as raised:
        presentation_module.handle(
            _event(path, {**body, **forbidden}),
            _runtime(presentation_module, RecordingCatalog(), RecordingPages()),
        )

    assert raised.value.context["n"] == len(forbidden)
    assert raised.value.context["missing"] == []


MARKETPLACE_PATH = "/api/presentation/marketplace-comparison"
MARKETPLACE_CURRENT_START = date(2026, 8, 10)
MARKETPLACE_COMPARATOR_START = date(2026, 8, 3)
MARKETPLACE_KEY_ONE = "canonical-marketplace-secret"
MARKETPLACE_KEY_TWO = "canonical-marketplace-two-secret"
MARKETPLACE_DIMENSION_LABELS = (
    *DIMENSION_LABELS,
    PresentationDimensionLabel(
        "marketplace_key",
        MARKETPLACE_KEY_TWO,
        "marketplace",
        "Fictional Canada",
    ),
)


def _marketplace_queries() -> MarketplaceQueries:
    return MarketplaceQueries(
        values={
            ("ad_spend", MARKETPLACE_CURRENT_START): (
                (MARKETPLACE_KEY_TWO, Decimal("300")),
                (MARKETPLACE_KEY_ONE, Decimal("200")),
            ),
            ("ad_spend", MARKETPLACE_COMPARATOR_START): (
                (MARKETPLACE_KEY_ONE, Decimal("100")),
                (MARKETPLACE_KEY_TWO, Decimal("300")),
            ),
            ("retail_revenue_shipped", MARKETPLACE_CURRENT_START): (
                (MARKETPLACE_KEY_ONE, Decimal("400")),
                (MARKETPLACE_KEY_TWO, Decimal("200")),
            ),
            ("retail_revenue_shipped", MARKETPLACE_COMPARATOR_START): (
                (MARKETPLACE_KEY_TWO, Decimal("100")),
                (MARKETPLACE_KEY_ONE, Decimal("200")),
            ),
        },
        currencies={
            (metric, period, MARKETPLACE_KEY_TWO): "CAD"
            for metric in ("ad_spend", "retail_revenue_shipped")
            for period in (MARKETPLACE_CURRENT_START, MARKETPLACE_COMPARATOR_START)
        },
    )


@dataclass(frozen=True, slots=True)
class _MarketplaceResponseRequest:
    queries: MarketplaceQueries | None = None
    publications: StubPublications | None = None
    body: dict[str, object] | None = None
    v2: bool = False
    windows: Any | None = None
    pages: RecordingPages | None = None


def _marketplace_response(
    presentation_module: _PresentationModule,
    request: _MarketplaceResponseRequest | None = None,
) -> tuple[
    dict[str, Any],
    list[tuple[MeasurementToolRequest, ...]],
    StubPublications,
    MarketplaceQueries,
    RecordingPages,
]:
    module = cast("Any", presentation_module.module)
    request = request or _MarketplaceResponseRequest()
    original_prepare = module.prepare_batch
    prepared_requests: list[tuple[MeasurementToolRequest, ...]] = []
    selected_queries = request.queries or _marketplace_queries()
    selected_publications = request.publications or StubPublications()
    selected_pages = request.pages or RecordingPages()

    def capture_prepare(requests: tuple[MeasurementToolRequest, ...]) -> Any:
        prepared_requests.append(requests)
        return original_prepare(requests)

    with patch.object(module, "prepare_batch", side_effect=capture_prepare):
        event = _event(MARKETPLACE_PATH, request.body or _base_body())
        if request.v2:
            event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
        response = presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    selected_pages,
                    selected_queries,
                    publications=selected_publications,
                    windows=request.windows,
                    dimension_labels=MARKETPLACE_DIMENSION_LABELS,
                ),
            ),
        )
    return (
        response,
        prepared_requests,
        selected_publications,
        selected_queries,
        selected_pages,
    )


def test_marketplace_rejects_browser_comparator_and_derives_explicit_period(
    presentation_module: _PresentationModule,
) -> None:
    body = _base_body()
    body["comparison_window"] = {"start": "2026-08-03", "end": "2026-08-09"}
    with pytest.raises(ValidationError, match="derived by the server"):
        presentation_module.handle(
            _event(MARKETPLACE_PATH, body),
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    RecordingPages(),
                    _marketplace_queries(),
                    windows=RecordingWindows(),
                    dimension_labels=MARKETPLACE_DIMENSION_LABELS,
                ),
            ),
        )

    response, _prepared, _publications, _queries, pages = _marketplace_response(presentation_module)
    payload = _body(response)
    assert payload["visible_window"] == {"end": "2026-08-16", "start": "2026-08-10"}
    assert pages.records[0].snapshot.comparison_window == DateRange(
        date(2026, 8, 3), date(2026, 8, 9)
    )


def test_marketplace_default_derives_previous_equal_period(
    presentation_module: _PresentationModule,
) -> None:
    response, _prepared, _publications, _queries, pages = _marketplace_response(
        presentation_module,
        _MarketplaceResponseRequest(
            body={"session_id": SESSION},
            windows=RecordingWindows(),
        ),
    )
    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    assert artifact["current_window"] == {"end": "2026-08-16", "start": "2026-08-10"}
    assert artifact["comparison_window"] == {"end": "2026-08-09", "start": "2026-08-03"}
    assert pages.records[0].snapshot.reporting_window_status is (
        ReportingWindowStatus.PREFERRED_DEFAULT
    )


def test_marketplace_uses_one_ordered_prepare_inspection_execution_and_exact_artifact(
    presentation_module: _PresentationModule,
) -> None:
    module = cast("Any", presentation_module.module)
    execute_query_batch = module.execute_presentation_query_batch
    with patch.object(
        module,
        "execute_presentation_query_batch",
        wraps=execute_query_batch,
    ) as execute:
        response, prepared, publications, queries, pages = _marketplace_response(
            presentation_module
        )
    assert execute.call_count == 1
    payload = _body(response)
    page = cast("dict[str, object]", payload["page"])
    artifact = cast("dict[str, object]", page["artifact"])
    rows = cast("list[dict[str, object]]", artifact["rows"])

    assert page["kind"] == "marketplace_comparison"
    assert page["artifact_type"] == "MarketplaceComparisonArtifact"
    assert page["artifact_version"] == 1
    assert artifact["state"] == "comparable"
    assert [row["marketplace"] for row in rows] == ["United States", "Fictional Canada"]
    assert [
        cast("dict[str, object]", cast("dict[str, object]", row["ad_spend"])["current"])["units"]
        for row in rows
    ] == [
        {"currency_code": "USD", "kind": "currency"},
        {"currency_code": "CAD", "kind": "currency"},
    ]
    assert len(prepared) == 1
    assert [request.metric_name for request in prepared[0]] == [
        "ad_spend",
        "retail_revenue_shipped",
    ]
    assert all(request.tool is MeasurementTool.COMPARE_WINDOWS for request in prepared[0])
    assert all(request.grains == ("marketplace",) for request in prepared[0])
    assert len(publications.inspections) == 1
    assert len(publications.inspections[0].plans) == 4
    assert [request.plan.logical.metric.name for request in queries.seen] == [
        "ad_spend",
        "ad_spend",
        "retail_revenue_shipped",
        "retail_revenue_shipped",
    ]
    assert pages.writes == ["page", "artifact"]
    assert pages.artifact_records[0].artifact.payload == artifact
    assert MARKETPLACE_KEY_ONE not in json.dumps(payload)
    assert MARKETPLACE_KEY_TWO not in json.dumps(payload)
    _assert_selector_safe(payload)


def test_marketplace_refuses_inspection_misalignment_before_any_query(
    presentation_module: _PresentationModule,
) -> None:
    publications = StubPublications()
    original_inspect = publications.inspect

    def misaligned(request: Any) -> Any:
        result = original_inspect(request)
        first, second, *remaining = result.slots
        return replace(
            result,
            slots=(replace(first, role=second.role), second, *remaining),
        )

    queries = _marketplace_queries()
    pages = RecordingPages()
    with (
        patch.object(publications, "inspect", side_effect=misaligned),
        pytest.raises(InvariantError) as raised,
    ):
        _marketplace_response(
            presentation_module,
            _MarketplaceResponseRequest(
                publications=publications,
                queries=queries,
                pages=pages,
            ),
        )

    assert str(raised.value.code) == "ADLY-MEASUREMENT-0003"
    assert queries.seen == []
    assert pages.writes == []


@pytest.mark.parametrize(
    ("absent_index", "expected_reason"),
    [(0, "missing_current_publication"), (1, "missing_comparator_publication")],
)
def test_marketplace_absent_publication_is_typed_and_not_queried(
    presentation_module: _PresentationModule,
    absent_index: int,
    expected_reason: str,
) -> None:
    response, _prepared, publications, queries, _pages = _marketplace_response(
        presentation_module,
        _MarketplaceResponseRequest(
            publications=StubPublications(inspection_absent_indices=frozenset({absent_index}))
        ),
    )
    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    rows = cast("list[dict[str, object]]", artifact["rows"])
    assert all(expected_reason in cast("list[str]", row["reasons"]) for row in rows)
    side = "current" if absent_index == 0 else "comparator"
    assert all(
        cast(
            "dict[str, object]",
            cast("dict[str, object]", row["ad_spend"])[side],
        )["units"]
        is None
        for row in rows
    )
    assert len(publications.inspections) == 1
    assert len(queries.seen) == 3


def test_marketplace_query_decimal_strings_use_exact_money_validation(
    presentation_module: _PresentationModule,
) -> None:
    queries = _marketplace_queries()
    queries.values[("ad_spend", MARKETPLACE_CURRENT_START)] = (
        (MARKETPLACE_KEY_ONE, "200.1250"),
        (MARKETPLACE_KEY_TWO, "300"),
    )
    response, _prepared, _publications, _queries, _pages = _marketplace_response(
        presentation_module,
        _MarketplaceResponseRequest(queries=queries),
    )
    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    rows = cast("list[dict[str, object]]", artifact["rows"])
    united_states = cast("dict[str, object]", rows[0]["ad_spend"])
    current = cast("dict[str, object]", united_states["current"])
    assert current["value"] == "200.1250"
    assert current["units"] == {"currency_code": "USD", "kind": "currency"}
    assert current["grain"] == ["period", "marketplace"]


@pytest.mark.parametrize("invalid_value", ["1.00001", "not-money", "NaN"])
def test_marketplace_query_refuses_invalid_or_overprecision_money_before_storage(
    presentation_module: _PresentationModule,
    invalid_value: str,
) -> None:
    queries = _marketplace_queries()
    queries.values[("ad_spend", MARKETPLACE_CURRENT_START)] = (
        (MARKETPLACE_KEY_ONE, invalid_value),
    )
    pages = RecordingPages()
    with pytest.raises(InvariantError) as raised:
        _marketplace_response(
            presentation_module,
            _MarketplaceResponseRequest(queries=queries, pages=pages),
        )

    assert str(raised.value.code) == "ADLY-PRESENTATION-0003"
    assert pages.writes == []


def test_marketplace_covered_missing_row_differs_from_represented_zero(
    presentation_module: _PresentationModule,
) -> None:
    queries = _marketplace_queries()
    queries.values[("ad_spend", MARKETPLACE_CURRENT_START)] = ((MARKETPLACE_KEY_TWO, Decimal("0")),)
    response, _prepared, _publications, _queries, _pages = _marketplace_response(
        presentation_module,
        _MarketplaceResponseRequest(queries=queries),
    )
    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    rows = {
        cast("str", row["marketplace"]): row
        for row in cast("list[dict[str, object]]", artifact["rows"])
    }
    missing = cast("dict[str, object]", rows["United States"]["ad_spend"])
    represented = cast("dict[str, object]", rows["Fictional Canada"]["ad_spend"])
    missing_current = cast("dict[str, object]", missing["current"])
    assert missing_current["condition"] == "missing"
    assert missing_current["units"] is None
    assert "missing_current" in cast("list[str]", missing["reasons"])
    represented_current = cast("dict[str, object]", represented["current"])
    assert represented_current["condition"] == "available"
    assert represented_current["value"] == "0"
    assert "missing_current" not in cast("list[str]", represented["reasons"])


def test_marketplace_malformed_evidence_is_page_issue_before_storage(
    presentation_module: _PresentationModule,
) -> None:
    module = cast("Any", presentation_module.module)
    pages = RecordingPages()
    with (
        patch.object(
            module,
            "_marketplace_rows",
            side_effect=ValueError("malformed fictional attestation"),
        ),
        pytest.raises(InvariantError) as raised,
    ):
        _marketplace_response(
            presentation_module,
            _MarketplaceResponseRequest(pages=pages),
        )

    assert str(raised.value.code) == "ADLY-PRESENTATION-0003"
    assert pages.writes == []
    assert pages.records == []
    assert pages.artifact_records == []


@pytest.mark.parametrize(
    ("signal", "expected_reason"),
    [("suppressed", "suppressed"), ("truncated", "truncated")],
)
def test_marketplace_suppression_and_truncation_remain_typed(
    presentation_module: _PresentationModule,
    signal: str,
    expected_reason: str,
) -> None:
    queries = _marketplace_queries()
    identity = ("ad_spend", MARKETPLACE_CURRENT_START)
    queries.suppressed = frozenset({identity}) if signal == "suppressed" else frozenset()
    queries.truncated = frozenset({identity}) if signal == "truncated" else frozenset()
    response, _prepared, _publications, _queries, _pages = _marketplace_response(
        presentation_module,
        _MarketplaceResponseRequest(queries=queries),
    )
    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    rows = cast("list[dict[str, object]]", artifact["rows"])
    assert all(expected_reason in cast("list[str]", row["reasons"]) for row in rows)
    ad_spend = cast("dict[str, object]", rows[0]["ad_spend"])
    current = cast("dict[str, object]", ad_spend["current"])
    assert current["condition"] == expected_reason
    assert current["units"] == (
        None if signal == "suppressed" else {"currency_code": "USD", "kind": "currency"}
    )


def test_marketplace_currency_change_invalidates_only_that_marketplace_metric(
    presentation_module: _PresentationModule,
) -> None:
    queries = _marketplace_queries()
    queries.currencies[("ad_spend", MARKETPLACE_CURRENT_START, MARKETPLACE_KEY_TWO)] = "USD"
    response, _prepared, _publications, _queries, _pages = _marketplace_response(
        presentation_module,
        _MarketplaceResponseRequest(queries=queries),
    )
    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    rows = {
        cast("str", row["marketplace"]): row
        for row in cast("list[dict[str, object]]", artifact["rows"])
    }
    united_states = rows["United States"]
    canada = rows["Fictional Canada"]
    assert cast("dict[str, object]", united_states["ad_spend"])["reasons"] == []
    assert united_states["relation"] == "same_direction"
    canada_ad = cast("dict[str, object]", canada["ad_spend"])
    canada_retail = cast("dict[str, object]", canada["retail_revenue_shipped"])
    assert canada_ad["reasons"] == ["currency_changed"]
    assert canada_ad["direction"] is None
    assert canada_retail["reasons"] == []
    assert canada["relation"] is None


@pytest.mark.parametrize(
    ("freshness", "expected_reason"),
    [
        (FreshnessStatus.STALE, "stale"),
        (FreshnessStatus.QUARANTINED, "quarantined"),
    ],
)
def test_marketplace_freshness_blocks_comparison_with_exact_reason(
    presentation_module: _PresentationModule,
    freshness: FreshnessStatus,
    expected_reason: str,
) -> None:
    period = DateRange(date(2026, 8, 10), date(2026, 8, 16))
    coverage = Coverage(
        fact_name="fct_ad_exposure",
        window=period,
        freshness=freshness,
        measurement_basis=MeasurementBasis.AD_ATTRIBUTED,
        valid_grains=("marketplace",),
        loaded_at=NOW - timedelta(days=14),
        intervals=(period,),
    )
    response, _prepared, _publications, _queries, _pages = _marketplace_response(
        presentation_module,
        _MarketplaceResponseRequest(
            publications=StubPublications(inspection_coverage_by_index={0: coverage})
        ),
    )
    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    rows = cast("list[dict[str, object]]", artifact["rows"])
    assert all(expected_reason in cast("list[str]", row["reasons"]) for row in rows)


def test_marketplace_all_absent_publications_produce_empty_no_coverage_artifact(
    presentation_module: _PresentationModule,
) -> None:
    response, prepared, publications, queries, pages = _marketplace_response(
        presentation_module,
        _MarketplaceResponseRequest(
            publications=StubPublications(inspection_absent_indices=frozenset({0, 1, 2, 3}))
        ),
    )
    payload = _body(response)
    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", payload["page"])["artifact"],
    )

    assert artifact["state"] == "no_coverage"
    assert artifact["rows"] == []
    assert artifact["publication_reasons"] == [
        "missing_current_publication",
        "missing_comparator_publication",
    ]
    assert len(prepared) == 1
    assert len(publications.inspections) == 1
    assert queries.seen == []
    assert pages.writes == ["page", "artifact"]


def test_marketplace_qualified_dq_is_page_issue_not_silent_comparability(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    with pytest.raises(InvariantError) as raised:
        _marketplace_response(
            presentation_module,
            _MarketplaceResponseRequest(
                publications=StubPublications(qualified_dq=("Fictional source caveat",)),
                pages=pages,
            ),
        )

    assert str(raised.value.code) == "ADLY-PRESENTATION-0003"
    assert pages.writes == []


def test_marketplace_route_preserves_concurrent_coverage_freshness_and_truncation(
    presentation_module: _PresentationModule,
) -> None:
    period = DateRange(date(2026, 8, 10), date(2026, 8, 16))
    coverage = Coverage(
        fact_name="fct_ad_exposure",
        window=period,
        freshness=FreshnessStatus.STALE,
        measurement_basis=MeasurementBasis.AD_ATTRIBUTED,
        valid_grains=("marketplace",),
        loaded_at=NOW - timedelta(days=14),
        intervals=(period,),
        holes=(period,),
    )
    queries = _marketplace_queries()
    queries.truncated = frozenset({("ad_spend", MARKETPLACE_CURRENT_START)})
    response, _prepared, _publications, _queries, _pages = _marketplace_response(
        presentation_module,
        _MarketplaceResponseRequest(
            queries=queries,
            publications=StubPublications(inspection_coverage_by_index={0: coverage}),
        ),
    )
    artifact = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(response)["page"])["artifact"],
    )
    rows = cast("list[dict[str, object]]", artifact["rows"])
    for row in rows:
        movement = cast("dict[str, object]", row["ad_spend"])
        current = cast("dict[str, object]", movement["current"])
        assert current["condition"] == "coverage_hole"
        assert current["freshness_status"] == "stale"
        assert current["truncated"] is True
        assert movement["reasons"] == ["coverage_hole", "truncated", "stale"]


def test_marketplace_never_loaded_is_page_issue_not_missing_publication(
    presentation_module: _PresentationModule,
) -> None:
    coverage = Coverage(
        fact_name="fct_ad_exposure",
        window=None,
        freshness=FreshnessStatus.NEVER_LOADED,
        measurement_basis=MeasurementBasis.AD_ATTRIBUTED,
        valid_grains=("marketplace",),
    )
    pages = RecordingPages()
    with pytest.raises(InvariantError) as raised:
        _marketplace_response(
            presentation_module,
            _MarketplaceResponseRequest(
                publications=StubPublications(inspection_coverage_by_index={0: coverage}),
                pages=pages,
            ),
        )

    assert str(raised.value.code) == "ADLY-PRESENTATION-0003"
    assert pages.writes == []


def test_marketplace_v2_binds_the_declared_artifact_and_custom_temporal_window(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    coverage = RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),))
    response, _prepared, _publications, _queries, recorded_pages = _marketplace_response(
        presentation_module,
        _MarketplaceResponseRequest(
            body={
                "session_id": SESSION,
                "temporal_selection": {
                    "kind": "custom",
                    "start": "2026-08-10",
                    "end": "2026-08-16",
                },
            },
            pages=pages,
            v2=True,
            windows=coverage,
        ),
    )
    payload = _body(response)
    temporal = cast("dict[str, object]", payload["temporal"])
    resolved = cast("dict[str, object]", temporal["resolved"])
    page = cast("dict[str, object]", payload["page"])
    artifact = cast("dict[str, object]", page["artifact"])

    assert response["headers"] == {
        "Cache-Control": "no-store",
        "Content-Type": "application/json",
        "Pragma": "no-cache",
    }
    assert payload["envelope_version"] == 2
    assert payload["kind"] == "page"
    assert page["page_id"] == "workspace"
    assert page["route"] == "/workspace/"
    assert page["artifact_id"] == "marketplace_comparison"
    assert page["artifact_type"] == "MarketplaceComparisonArtifact"
    assert page["artifact_version"] == 1
    assert page["actions"] == []
    assert page["artifact_revision"] == recorded_pages.artifact_records[0].reference.value
    assert resolved["selection"] == {"kind": "custom", "preset_id": None}
    assert resolved["window"] == {"start": "2026-08-10", "end": "2026-08-16"}
    assert artifact["current_window"] == resolved["window"]
    assert artifact["state"] in {"comparable", "one_marketplace", "insufficient", "no_coverage"}
    assert len(coverage.calls) == 1


def test_marketplace_v2_returns_temporal_refusal_without_persisting_an_artifact(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    coverage = RecordingCoverage((DateRange(date(2026, 8, 10), date(2026, 8, 16)),))
    response, _prepared, _publications, _queries, recorded_pages = _marketplace_response(
        presentation_module,
        _MarketplaceResponseRequest(
            body={
                "session_id": SESSION,
                "temporal_selection": {
                    "kind": "custom",
                    "start": "2026-08-10",
                    "end": "2026-08-10",
                },
            },
            pages=pages,
            v2=True,
            windows=coverage,
        ),
    )
    payload = _body(response)

    assert payload["envelope_version"] == 2
    assert payload["kind"] == "window_unavailable"
    assert "page" not in payload
    assert payload["reason_code"] == "required_comparison_unavailable"
    assert recorded_pages.records == []
    assert recorded_pages.artifact_records == []
    assert len(coverage.calls) == 1


def test_data_health_v2_binds_no_comparison_temporal_control_to_exact_artifact(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    coverage = RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),))
    response, _captured, _publications, _queries = _data_health_response(
        presentation_module,
        _DataHealthResponseRequest(
            pages,
            ScenarioQueries(
                suppressed_metrics=frozenset(),
                truncated_metrics=frozenset(),
            ),
            body={
                "session_id": SESSION,
                "temporal_selection": {
                    "kind": "custom",
                    "start": "2026-08-10",
                    "end": "2026-08-16",
                },
            },
            v2=True,
            windows=coverage,
        ),
    )
    payload = _body(response)
    page = cast("dict[str, object]", payload["page"])
    artifact = cast("dict[str, object]", page["artifact"])
    temporal = cast("dict[str, object]", payload["temporal"])
    resolved = cast("dict[str, object]", temporal["resolved"])
    comparison = cast("dict[str, object]", temporal["comparison"])

    assert payload["envelope_version"] == 2
    assert payload["kind"] == "page"
    assert page["page_id"] == "data-health"
    assert page["route"] == "/data-health/"
    assert page["artifact_id"] == "evidence_health"
    assert page["artifact_type"] == "DataHealthDetail"
    assert page["artifact_version"] == 1
    assert page["actions"] == []
    assert resolved["selection"] == {"kind": "custom", "preset_id": None}
    assert artifact["period"] == resolved["window"]
    assert comparison == {"policy": "none", "reason_code": None, "status": "not_applicable"}
    assert len(coverage.calls) == 1


def test_v2_channel_child_uses_parent_temporal_control_and_preserves_parent_artifact(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages(tokens=(TOKEN, SECOND_SELECTION_TOKEN, THIRD_SELECTION_TOKEN))
    parent_queries = ScenarioQueries(suppressed_metrics=frozenset(), truncated_metrics=frozenset())
    parent_event = _event(
        "/api/presentation/amazon-channel-performance",
        {"session_id": SESSION},
    )
    parent_event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    parent_response = presentation_module.handle(
        parent_event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                pages,
                parent_queries,
                windows=RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),)),
            ),
        ),
    )
    parent_payload = cast(
        "dict[str, object]",
        cast("dict[str, object]", _body(parent_response)["page"])["artifact"],
    )
    parent_record = pages.artifact_records[0]
    capability = pages.capability_records[0]
    pages.tokens = (THIRD_SELECTION_TOKEN,)
    child_queries = ScenarioQueries(suppressed_metrics=frozenset(), truncated_metrics=frozenset())
    child_publications = StubPublications()
    child_event = _event(
        "/api/presentation/amazon-channel-performance",
        {"session_id": SESSION, "selection_reference": capability.reference.value},
    )
    child_event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        child_event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                pages,
                child_queries,
                publications=child_publications,
            ),
        ),
    )

    payload = _body(response)
    page = cast("dict[str, object]", payload["page"])
    child_payload = cast("dict[str, object]", page["artifact"])
    child_record = pages.artifact_records[1]
    assert payload["envelope_version"] == 2
    assert payload["kind"] == "page"
    assert payload["temporal"] == presentation_module.module._temporal_control_json(
        parent_record.temporal_control
    )
    assert "row_capabilities" not in payload
    assert page["artifact_revision"] == child_record.reference.value
    assert page["parent_revision"] == parent_record.reference.value
    assert child_record.parent_reference == parent_record.reference
    assert child_record.temporal_control == parent_record.temporal_control
    for field_name in ("boundaries", "comparison", "current", "period_relation"):
        assert child_payload[field_name] == parent_payload[field_name]
    assert len(child_publications.inspections) == 1
    assert child_publications.seen == []
    assert len(child_queries.seen) == 4


def test_v2_channel_child_refuses_extra_temporal_and_revision_fields_before_custody(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    queries = ScenarioQueries()
    event = _event(
        "/api/presentation/amazon-channel-performance",
        {
            "artifact_revision": TOKEN,
            "selection_reference": SECOND_SELECTION_TOKEN,
            "session_id": SESSION,
            "temporal_selection": {"kind": "preset", "preset_id": "7d"},
        },
    )
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    with pytest.raises(ValidationError, match="fields are invalid") as raised:
        presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(RecordingCatalog(), pages, queries),
            ),
        )

    assert raised.value.context == {"outcome": "invalid_fields", "n": 2}
    assert pages.capability_reads == []
    assert queries.seen == []


def test_v2_channel_child_invalid_capability_does_not_execute_measurements(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages(tokens=(TOKEN, SECOND_SELECTION_TOKEN))
    _channel_response(presentation_module, _ChannelResponseRequest(pages))
    queries = ScenarioQueries()
    publications = StubPublications()
    event = _event(
        "/api/presentation/amazon-channel-performance",
        {"session_id": SESSION, "selection_reference": FIRST_SELECTION_TOKEN},
    )
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    with pytest.raises(ValidationError, match="requested channel is not available"):
        presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    pages,
                    queries,
                    publications=publications,
                ),
            ),
        )

    assert publications.inspections == []
    assert publications.seen == []
    assert queries.seen == []


def test_media_fixed_plan_cohort_is_closed_and_server_selected(
    presentation_module: _PresentationModule,
) -> None:
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(RecordingCatalog(), RecordingPages(), ScenarioQueries()),
    )
    media_scope = MediaScope(
        commerce_scope=SCOPE,
        platform=PLATFORM,
        channel=CHANNEL,
        marketplace=MARKETPLACE,
    )

    requests = presentation_module.module._media_fixed_plan_requests(
        presentation_module.module._MediaFixedPlanRequest(
            runtime=runtime,
            scope=media_scope,
            window=DateRange(date(2026, 8, 10), date(2026, 8, 16)),
            compare_previous=True,
        )
    )

    assert [request.metric_name for request in requests] == [
        "ad_spend",
        "impressions",
        "clicks",
        "measurable_impressions",
        "viewable_impressions",
        "viewable_impressions_estimated",
    ]
    assert all(request.prior_window is not None for request in requests)
    assert all(
        request.filters
        == {
            "platform_key": PLATFORM.value,
            "channel_key": CHANNEL.value,
            "marketplace_key": MARKETPLACE.value,
        }
        for request in requests
    )


def test_v2_campaign_parent_sort_accepts_only_the_declared_server_grammar(
    presentation_module: _PresentationModule,
) -> None:
    module = cast("Any", presentation_module.module)
    request = module._v2_parent_request(
        _event(
            LIST_PATH,
            {"session_id": SESSION, "sort": {"field": "purchases", "direction": "asc"}},
        ),
        LIST_PATH,
    )

    assert request.campaign_sort is not None
    assert request.campaign_sort.field.value == "purchases"
    assert request.campaign_sort.direction == "asc"
    for sort in (
        {"field": "campaign_key", "direction": "asc"},
        {"field": "spend", "direction": "descending"},
        {"field": "spend", "direction": "asc", "client_comparator": "unsafe"},
    ):
        with pytest.raises(ValidationError, match="campaign sort is invalid"):
            module._v2_parent_request(
                _event(LIST_PATH, {"session_id": SESSION, "sort": sort}),
                LIST_PATH,
            )
    named = module._v2_parent_request(
        _event(
            LIST_PATH,
            {"session_id": SESSION, "sort": {"field": "name", "direction": "asc"}, "segment": 2},
        ),
        LIST_PATH,
    )
    assert named.campaign_sort is not None
    assert named.campaign_sort.field.value == "name"
    assert named.campaign_segment == 2
    for segment in (-1, "1", True, 10_001):
        with pytest.raises(ValidationError, match="campaign segment is invalid"):
            module._v2_parent_request(
                _event(LIST_PATH, {"session_id": SESSION, "segment": segment}),
                LIST_PATH,
            )


def test_v1_campaign_portfolio_calculates_then_truncates_an_authorized_population(
    presentation_module: _PresentationModule,
) -> None:
    campaign_keys = tuple(
        CampaignKey(f"campaign:portfolio-{index:03d}", SCOPE, PLATFORM) for index in range(51)
    )
    catalog = RecordingCatalog(
        browse_result=EntityCatalogBrowseResult(
            tuple(
                _alias(campaign, f"Portfolio campaign {index:03d}")
                for index, campaign in enumerate(campaign_keys)
            ),
            authorized_count=len(campaign_keys),
        )
    )
    pages = RecordingPages(
        tokens=tuple(f"{index:043d}" for index in range(len(campaign_keys) + 1)),
        clock=lambda: NOW,
    )
    queries = ScenarioQueries(
        campaign_row_keys=tuple(campaign.value for campaign in campaign_keys),
        marketplace_row_key=MARKETPLACE.value,
        suppressed_metrics=frozenset(),
        truncated_metrics=frozenset(),
    )
    coverage = RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),))
    event = _event(LIST_PATH, _base_body())
    event["headers"] = {"Accept": "application/json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(catalog, pages, queries, windows=coverage),
        ),
    )
    page = cast("dict[str, object]", _body(response)["page"])
    artifact = cast("dict[str, object]", page["artifact"])
    assert catalog.browse_calls[0].max_candidates == 25
    assert artifact["outcome"] == "choices"
    assert len(cast("list[object]", artifact["choices"])) == 51
    assert len(pages.campaign_capability_records) == 51


def test_v1_campaign_portfolio_excludes_non_catalog_campaigns_from_totals_and_trend(
    presentation_module: _PresentationModule,
) -> None:
    outside_catalog = "campaign:not-authorized"
    catalog = RecordingCatalog(
        browse_result=EntityCatalogBrowseResult((_alias(CAMPAIGN, "Fictional Campaign"),))
    )
    pages = RecordingPages(clock=lambda: NOW)
    queries = ScenarioQueries(
        campaign_row_keys=(CAMPAIGN.value, outside_catalog),
        marketplace_row_key=MARKETPLACE.value,
        campaign_metric_values={
            ("ad_spend", outside_catalog): Decimal("10000"),
            ("attributed_revenue_14d", outside_catalog): Decimal("1"),
        },
        suppressed_metrics=frozenset(),
        truncated_metrics=frozenset(),
    )
    coverage = RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),))
    event = _event(LIST_PATH, _base_body())
    event["headers"] = {"Accept": "application/json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(catalog, pages, queries, windows=coverage),
        ),
    )

    artifact = cast(
        "dict[str, object]", cast("dict[str, object]", _body(response)["page"])["artifact"]
    )
    assert artifact["outcome"] == "choices"
    choices = cast("list[dict[str, object]]", artifact["choices"])
    assert len(choices) == 1
    assert all(CAMPAIGN.value not in json.dumps(choice) for choice in choices)
    assert catalog.browse_calls[0].max_candidates == 25


RETAIL_HEALTH_LIST_PATH = "/api/presentation/retail-health"
RETAIL_HEALTH_PERIOD = DateRange(date(2026, 8, 13), date(2026, 8, 19))
RETAIL_HEALTH_PRIOR = DateRange(date(2026, 8, 6), date(2026, 8, 12))
RETAIL_HEALTH_TOTAL_GRAIN = Grain(("period", "marketplace"))
RETAIL_HEALTH_ROW_GRAIN = Grain(("period", "product", "marketplace"))
RETAIL_HEALTH_TREND_GRAIN = Grain(("date", "marketplace"))
RETAIL_HEALTH_SPARK_GRAIN = Grain(("date", "product", "marketplace"))


def _retail_units(metric: str) -> Units:
    kind = RETAIL_HEALTH_METRIC_UNITS[metric]
    return Units(kind, "USD" if kind is UnitKind.CURRENCY else None)


def _retail_comparison(
    metric: str,
    values: tuple[int | Decimal, int | Decimal],
    grain: Grain,
) -> RetailComparison:
    current, prior = values
    units = _retail_units(metric)
    return retail_comparison(
        metric,
        RetailHealthMetricValue(current, units, RETAIL_HEALTH_PERIOD, grain),
        RetailHealthMetricValue(prior, units, RETAIL_HEALTH_PRIOR, grain),
        SEMANTICS,
    )


def _retail_row(child_asin: str, values: dict[str, int | Decimal]) -> RetailHealthProductRow:
    comparisons = tuple(
        _retail_comparison(metric, (values[metric], 1), RETAIL_HEALTH_ROW_GRAIN)
        for metric in RETAIL_HEALTH_ROW_METRICS
    )
    spark = tuple(
        RetailHealthMetricValue(
            1,
            _retail_units("retail_revenue_shipped"),
            DateRange(day, day),
            RETAIL_HEALTH_SPARK_GRAIN,
        )
        for day in (
            RETAIL_HEALTH_PERIOD.start + timedelta(days=offset)
            for offset in range(RETAIL_HEALTH_PERIOD.days)
        )
    )
    return RetailHealthProductRow(
        child_asin,
        f"PARENT-{child_asin}",
        f"Fictional Product {child_asin}",
        comparisons,
        spark,
    )


def _retail_trend() -> tuple[RetailTrendPoint, ...]:
    points: list[RetailTrendPoint] = []
    for offset in range(RETAIL_HEALTH_PERIOD.days):
        day = RETAIL_HEALTH_PERIOD.start + timedelta(days=offset)
        prior_day = RETAIL_HEALTH_PRIOR.start + timedelta(days=offset)
        points.append(
            RetailTrendPoint(
                day,
                tuple(
                    RetailHealthMetricValue(
                        2, _retail_units(metric), DateRange(day, day), RETAIL_HEALTH_TREND_GRAIN
                    )
                    for metric in RETAIL_HEALTH_TREND_METRICS
                ),
                tuple(
                    RetailHealthMetricValue(
                        1,
                        _retail_units(metric),
                        DateRange(prior_day, prior_day),
                        RETAIL_HEALTH_TREND_GRAIN,
                    )
                    for metric in RETAIL_HEALTH_TREND_METRICS
                ),
            )
        )
    return tuple(points)


def _retail_health_artifact(*, scale: int = 1) -> RetailHealthArtifact:
    """`scale` multiplies the amount totals; 200 moves shipped revenue 3000 (ADR-076 §5)."""
    ratio_metrics = {"conversion_rate_retail", "return_rate", "fulfilment_rate"}
    totals = tuple(
        _retail_comparison(
            metric,
            (Decimal("0.02"), Decimal("0.01"))
            if metric in ratio_metrics
            else (30 * scale, 15 * scale),
            RETAIL_HEALTH_TOTAL_GRAIN,
        )
        for metric in RETAIL_HEALTH_TOTAL_METRICS
    )
    rows = (
        _retail_row(
            "FICTA",
            {
                "retail_revenue_shipped": 20,
                "units_shipped": 2,
                "customer_returns": 0,
                "glance_views": 5,
                "return_rate": Decimal("0.0000"),
                "amazon_margin_proxy": 12,
            },
        ),
        _retail_row(
            "FICTB",
            {
                "retail_revenue_shipped": 10,
                "units_shipped": 4,
                "customer_returns": 1,
                "glance_views": 9,
                "return_rate": Decimal("0.2500"),
                "amazon_margin_proxy": 6,
            },
        ),
    )
    return RetailHealthArtifact(
        outcome=RetailHealthOutcome.COMPLETE,
        period=RETAIL_HEALTH_PERIOD,
        prior_period=RETAIL_HEALTH_PRIOR,
        distributor_view=RetailDistributorView.MANUFACTURING,
        totals=totals,
        absences=(),
        insights=unavailable_insight_slots(RETAIL_HEALTH_INSIGHT_IDS),
        trend=_retail_trend(),
        rows=rows,
        total_count=len(rows),
        truncation=None,
        reconciliation=RetailReconciliation("reconciled", RETAIL_HEALTH_PERIOD.end),
        boundary="Fictional retail boundary statement.",
        evidence_status=retail_health_evidence_status(
            RETAIL_HEALTH_PERIOD, tuple(item.metric for item in totals), LABELS
        ),
    )


def _retail_preset_artifact(
    *, identity: str = "publication-1", scale: int = 1, retired: str | None = None
) -> tuple[PresetArtifactPointer, PresetArtifactBody]:
    """`retired` bakes the body in one retired form (`RETAIL_RETIRED_FORMS`).

    A format-8 body with rows is baked with its parent custody, as the publisher writes it
    (ADR-125); `_analytical_runtime` serves that custody when a test gives no store of its own.
    """
    artifact = _retail_health_artifact(scale=scale)
    revision = PresetArtifactRevision.from_publication_identities((identity,))
    body = PresetArtifactBody(
        PresetArtifactPage("retail-health"),
        PresetArtifactVariant("7d"),
        revision,
        canonical_preset_artifact_json(_baked(retail_health_artifact_payload(artifact), retired)),
    )
    custody = _retail_parent_custody(body, artifact)
    _BAKED_RETAIL_CUSTODY[custody.digest] = custody
    pointer = PresetArtifactPointer(
        body.page, body.variant, body.revision, body.digest, 1, None, custody.digest
    )
    return pointer, body


_BAKED_RETAIL_CUSTODY: dict[str, PresetArtifactCustodyManifest] = {}
"""Parent custody each `_retail_preset_artifact` bake wrote, by digest (content-addressed)."""


def _retail_parent_custody(
    body: PresetArtifactBody, artifact: RetailHealthArtifact
) -> PresetArtifactCustodyManifest:
    """Close the child set over the rows; the parent serve reads only this, never a child."""
    products = tuple(
        RetailHealthCustodyProduct(
            product_ref_for(product),
            product,
        )
        for product in (
            ProductKey(f"amazon_asin:{row.child_asin.lower()}", SCOPE, MARKETPLACE)
            for row in artifact.rows
        )
    )
    return _indexed_custody(
        body,
        retail_health_custody_manifest_payload(RetailHealthCustodyManifest(body.digest, products)),
    )


@dataclass
class _BakedRetailCustody:
    """Serves the parent custody a fixture bake wrote; any other read is a test error."""

    reads: list[PresetArtifactPointer] = field(
        default_factory=lambda: cast("list[PresetArtifactPointer]", [])
    )

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactCustodyManifest:
        self.reads.append(pointer)
        if pointer.custody_digest not in _BAKED_RETAIL_CUSTODY:
            raise AssertionError("no fixture bake wrote this custody")
        return _BAKED_RETAIL_CUSTODY[pointer.custody_digest]


def _retail_health_event(body: dict[str, object]) -> dict[str, Any]:
    event = _event(RETAIL_HEALTH_LIST_PATH, body)
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    return event


@pytest.mark.covers("ADR-070")
def test_v2_retail_health_parent_serves_a_preset_body_without_measurement_or_catalog_reads(
    presentation_module: _PresentationModule,
) -> None:
    """The route serves by pointer and body read only; no query, catalog, or coverage call."""
    pages = RecordingPages()
    pointer, body = _retail_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body)
    coverage = RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),))
    catalog = RecordingCatalog()
    queries = ScenarioQueries()

    response = presentation_module.handle(
        _retail_health_event({"session_id": SESSION}),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                catalog,
                pages,
                queries,
                preset_artifacts=artifacts,
                windows=coverage,
            ),
        ),
    )
    payload = _body(response)

    assert payload["kind"] == "page"
    assert artifacts.roster_reads == [
        PresetArtifactPointerRosterRequest(
            PresetArtifactPage("retail-health"),
            (
                PresetArtifactVariant("1d"),
                PresetArtifactVariant("7d"),
                PresetArtifactVariant("14d"),
            ),
        )
    ]
    assert artifacts.body_reads == [pointer]
    assert queries.seen == []
    assert catalog.browse_calls == []
    assert coverage.calls == []
    assert len(pages.parent_capability_requests) == 1
    # One product-detail capability per served row, issued with the parent (ADR-125).
    assert len(pages.parent_capability_requests[0].capabilities) == 2
    page = cast("dict[str, object]", payload["page"])
    assert page["page_id"] == "retail-health"
    assert page["route"] == "/retail-health/"
    assert page["artifact_type"] == "RetailHealthArtifact"
    assert page["artifact_revision"] == pages.artifact_records[0].reference.value
    artifact = cast("dict[str, object]", page["artifact"])
    assert artifact["outcome"] == "complete"
    assert artifact["period"] == {"start": "2026-08-13", "end": "2026-08-19"}
    rows = cast("list[dict[str, object]]", artifact["rows"])
    assert [row["child_asin"] for row in rows] == ["FICTA", "FICTB"]


def _served_comparisons(artifact: dict[str, object]) -> list[dict[str, object]]:
    """Every served total and row comparison of a Campaigns or Retail health artifact."""
    rows = cast("list[dict[str, object]]", artifact["rows"])
    return [
        *cast("list[dict[str, object]]", artifact["totals"]),
        *(item for row in rows for item in cast("list[dict[str, object]]", row["comparisons"])),
    ]


@pytest.mark.covers("ADR-121", "ADR-119")
@pytest.mark.parametrize("retired", RETAIL_RETIRED_FORMS, ids=["format-4", "format-5", "format-7"])
def test_v2_retail_health_refuses_a_retired_body_and_serves_a_current_one(
    presentation_module: _PresentationModule,
    retired: str,
) -> None:
    """The format-4, 5 and 7 reads retired (owner revision, 2026-10-08): no dual path."""

    def serve(body_form: str | None, pages: RecordingPages) -> dict[str, Any]:
        pointer, body = _retail_preset_artifact(retired=body_form)
        return presentation_module.handle(
            _retail_health_event({"session_id": SESSION}),
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    pages,
                    ScenarioQueries(),
                    preset_artifacts=RecordingPresetArtifacts(pointer, body),
                ),
            ),
        )

    # Paired positive control: the current body serves its blocks and its coverage block.
    pages = RecordingPages()
    response = serve(None, pages)
    assert response["statusCode"] == 200
    page = cast("dict[str, object]", _body(response)["page"])
    artifact = cast("dict[str, object]", page["artifact"])
    comparisons = _served_comparisons(artifact)
    assert comparisons
    assert all(isinstance(item["change"], dict) for item in comparisons)
    assert all("percentage_change" not in item for item in comparisons)
    assert isinstance(artifact["evidence_status"], dict)
    stored = json.loads(pages.artifact_records[0].artifact.payload_json)
    decoded = retail_health_artifact_from_payload(stored)
    assert all(item.change is not None for item in decoded.totals)

    refused_pages = RecordingPages()
    with pytest.raises(InvariantError) as refused:
        serve(retired, refused_pages)
    assert refused.value.code == presentation_module.module.ARTIFACT_MALFORMED
    assert refused.value.description == "Persisted durable artifact bytes are malformed."
    assert refused_pages.artifact_records == []


def _sibling_body(page: str, variant: str, payload_json: str) -> PresetArtifactBody:
    return PresetArtifactBody(
        PresetArtifactPage(page),
        PresetArtifactVariant(variant),
        PresetArtifactRevision.from_publication_identities(("publication-1",)),
        payload_json,
    )


def _read_siblings(
    presentation_module: _PresentationModule,
    bodies: tuple[PresetArtifactBody, ...],
    extract: Callable[[dict[str, object]], object],
) -> tuple[object, ...]:
    """Run the deep sibling read over these bodies, each served under its own pointer."""
    by_variant = {body.variant.value: body for body in bodies}
    pointers = tuple(
        PresetArtifactPointer(body.page, body.variant, body.revision, body.digest, 1)
        for body in bodies
    )

    def read(pointer: PresetArtifactPointer) -> PresetArtifactBody:
        return by_variant[pointer.variant.value]

    runtime = SimpleNamespace(preset_artifact_bodies=SimpleNamespace(read=read))
    return cast(
        "tuple[object, ...]",
        presentation_module.module._sibling_entries(
            runtime, SimpleNamespace(pointers=pointers), "7d", extract
        ),
    )


@pytest.mark.covers("ADR-076")
@pytest.mark.parametrize("retired", RETAIL_RETIRED_FORMS, ids=["format-4", "format-5", "format-7"])
def test_a_retired_or_undecodable_sibling_body_is_reported_as_malformed_not_skipped(
    presentation_module: _PresentationModule,
    monkeypatch: pytest.MonkeyPatch,
    retired: str,
) -> None:
    """A stale-format sibling between declarative rebakes is visible, not a silent gap."""
    reported: list[BaseException] = []

    def capture(error: BaseException, report: object) -> None:
        del report
        reported.append(error)

    monkeypatch.setattr(deep_insight_inputs, "report_error", capture)
    payload = retail_health_artifact_payload(_retail_health_artifact())
    stale = _baked(retail_health_artifact_payload(_retail_health_artifact()), retired)
    entries = _read_siblings(
        presentation_module,
        (
            # Paired positive control: the current-format sibling still yields its direction.
            _sibling_body("retail-health", "1d", canonical_preset_artifact_json(payload)),
            _sibling_body("retail-health", "14d", canonical_preset_artifact_json(stale)),
            _sibling_body(
                "retail-health", "30d", canonical_preset_artifact_json({"fixture": True})
            ),
        ),
        presentation_module.module._retail_direction_extract("retail_revenue_shipped"),
    )

    assert [cast("Any", entry).name for entry in entries] == ["Sibling 1D window direction"]
    assert len(reported) == 2
    for error in reported:
        assert isinstance(error, InvariantError)
        assert error.code == presentation_module.module.ARTIFACT_MALFORMED


@pytest.mark.covers("ADR-076")
def test_a_sibling_that_does_not_carry_the_focused_metric_is_typed_absent_and_unreported(
    presentation_module: _PresentationModule,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported: list[BaseException] = []

    def capture(error: BaseException, report: object) -> None:
        del report
        reported.append(error)

    monkeypatch.setattr(deep_insight_inputs, "report_error", capture)
    module = presentation_module.module
    retail = retail_health_artifact_payload(_retail_health_artifact())
    _, portfolio_body = _campaign_preset_artifact()
    portfolio = cast("dict[str, object]", json.loads(portfolio_body.payload_json))

    assert module._retail_direction_extract("fictional_uncarried_metric")(retail) is None
    # The portfolio sibling carries no totals, so its ROAS total is typed absent.
    assert module._portfolio_direction_extract("portfolio_roas")(portfolio) is None
    with pytest.raises(InvariantError) as refused:
        module._portfolio_direction_extract("portfolio_roas")({"fixture": True})
    assert refused.value.code == module.ARTIFACT_MALFORMED
    entries = _read_siblings(
        presentation_module,
        (_sibling_body("retail-health", "1d", canonical_preset_artifact_json(retail)),),
        module._retail_direction_extract("fictional_uncarried_metric"),
    )
    assert entries == ()
    assert reported == []


@pytest.mark.covers("ADR-125")
def test_the_retail_decoder_refuses_a_format_7_body_and_decodes_a_format_8_one() -> None:
    """The format-7 read retired (owner revision, 2026-10-08): no body without fixed statuses."""
    payload = retail_health_artifact_payload(_retail_health_artifact())
    # Paired positive control: the format-8 body carries both fixed statuses and decodes.
    assert payload["return_reasons"] == {"reason": RETURN_REASONS_ABSENCE}
    assert retail_health_artifact_from_payload(payload).totals
    for dropped in ("return_reasons", "amc_retail_purchases"):
        retired = {key: value for key, value in payload.items() if key != dropped}
        with pytest.raises(ValueError) as refused:
            retail_health_artifact_from_payload(retired)
        assert str(refused.value) == "retail health artifact fields are not exact."


@pytest.mark.covers("ADR-070", "ADR-068")
def test_v2_retail_health_parent_overlays_prompted_insights_beside_the_baked_movers_slot(
    presentation_module: _PresentationModule,
) -> None:
    """Serve-time fill replaces the baked prompted placeholders.

    The deterministic movers slot keeps its baked provenance because the overlay
    must never touch it.
    """
    pointer, body = _retail_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body)

    response = presentation_module.handle(
        _retail_health_event({"session_id": SESSION}),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(), RecordingPages(), ScenarioQueries(), preset_artifacts=artifacts
            ),
        ),
    )
    payload = _body(response)

    page = cast("dict[str, object]", payload["page"])
    artifact = cast("dict[str, object]", page["artifact"])
    insights = cast("list[dict[str, object]]", artifact["insights"])
    assert [slot["id"] for slot in insights] == [
        "demand-shape",
        "returns-pressure",
        "ordered-vs-shipped",
        "retail-movers",
    ]
    # The prompted slots were re-derived at serve time from the baked bytes: their provenance
    # is the derivation's revision fingerprint, not the bake placeholder's "unpublished".
    for slot in insights[:3]:
        assert cast("str", slot["artifact_revision"]).startswith("sha256:")
        assert slot["state"] in {"silent", "unavailable"}
    movers = insights[3]
    assert movers["artifact_revision"] == "unpublished"
    assert movers["prompt_version"] == "deterministic-v1"


def test_v2_retail_health_parent_returns_window_unavailable_when_pointer_is_missing(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    artifacts = RecordingPresetArtifacts(None, None)
    catalog = RecordingCatalog()
    queries = ScenarioQueries()

    response = presentation_module.handle(
        _retail_health_event({"session_id": SESSION}),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(catalog, pages, queries, preset_artifacts=artifacts),
        ),
    )
    payload = _body(response)

    assert payload["kind"] == "window_unavailable"
    assert artifacts.body_reads == []
    assert queries.seen == []
    assert catalog.browse_calls == []
    assert pages.artifact_records == []


def test_v2_retail_health_parent_rejects_a_pointer_body_identity_mismatch(
    presentation_module: _PresentationModule,
) -> None:
    pointer, _ = _retail_preset_artifact(identity="publication-1")
    _, substituted_body = _retail_preset_artifact(identity="publication-2")
    artifacts = RecordingPresetArtifacts(pointer, substituted_body)

    with pytest.raises(InvariantError) as raised:
        presentation_module.handle(
            _retail_health_event({"session_id": SESSION}),
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    RecordingPages(),
                    ScenarioQueries(),
                    preset_artifacts=artifacts,
                ),
            ),
        )

    assert raised.value.code == presentation_module.module.ARTIFACT_MALFORMED


def test_v2_retail_health_parent_rejects_a_malformed_persisted_body(
    presentation_module: _PresentationModule,
) -> None:
    """Bytes that attest to the pointer but decode into another shape stay malformed."""
    period = DateRange(date(2026, 8, 13), date(2026, 8, 19))
    foreign = build_campaign_portfolio(
        period=period,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=(),
        totals=(),
        trend=None,
        evidence=(CampaignListEvidence("retained campaign cohort", period, (period,)),),
    )
    revision = PresetArtifactRevision.from_publication_identities(("publication-1",))
    body = PresetArtifactBody(
        PresetArtifactPage("retail-health"),
        PresetArtifactVariant("7d"),
        revision,
        canonical_preset_artifact_json(campaign_portfolio_artifact_payload(foreign)),
    )
    pointer = PresetArtifactPointer(body.page, body.variant, body.revision, body.digest, 1)
    artifacts = RecordingPresetArtifacts(pointer, body)

    with pytest.raises(InvariantError) as raised:
        presentation_module.handle(
            _retail_health_event({"session_id": SESSION}),
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    RecordingPages(),
                    ScenarioQueries(),
                    preset_artifacts=artifacts,
                ),
            ),
        )

    assert raised.value.code == presentation_module.module.ARTIFACT_MALFORMED


@pytest.mark.covers("ADR-070")
def test_v2_retail_health_custom_selection_is_refused_without_artifact_reads(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    pointer, body = _retail_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body)

    response = presentation_module.handle(
        _retail_health_event(
            {
                "session_id": SESSION,
                "temporal_selection": {
                    "kind": "custom",
                    "start": "2026-08-15",
                    "end": "2026-08-21",
                },
            }
        ),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(), pages, ScenarioQueries(), preset_artifacts=artifacts
            ),
        ),
    )
    payload = _body(response)
    temporal = cast("dict[str, object]", payload["temporal"])

    assert payload["kind"] == "window_unavailable"
    assert payload["reason_code"] == "custom_disabled"
    assert cast("dict[str, object]", temporal["custom"])["availability"] == "unavailable"
    assert artifacts.pointer_reads == []
    assert artifacts.roster_reads == []
    assert artifacts.body_reads == []
    assert pages.artifact_records == []


def test_v2_retail_health_rejects_unknown_fields_and_undeclared_sort(
    presentation_module: _PresentationModule,
) -> None:
    module = cast("Any", presentation_module.module)

    request = module._v2_parent_request(
        _retail_health_event(
            {"session_id": SESSION, "sort": {"field": "glance_views", "direction": "desc"}}
        ),
        RETAIL_HEALTH_LIST_PATH,
    )
    assert request.retail_sort is not None
    assert request.retail_sort.field.value == "glance_views"
    assert request.retail_sort.direction == "desc"

    with pytest.raises(ValidationError, match="request fields are invalid"):
        module._v2_parent_request(
            _retail_health_event({"session_id": SESSION, "owner": "attacker"}),
            RETAIL_HEALTH_LIST_PATH,
        )
    for sort in (
        {"field": "revenue_ordered", "direction": "asc"},
        {"field": "glance_views", "direction": "descending"},
        {"field": "glance_views", "direction": "asc", "client_comparator": "unsafe"},
    ):
        with pytest.raises(ValidationError, match="retail sort is invalid"):
            module._v2_parent_request(
                _retail_health_event({"session_id": SESSION, "sort": sort}),
                RETAIL_HEALTH_LIST_PATH,
            )


@pytest.mark.covers("ADR-070")
def test_v2_retail_health_serve_reapplies_the_declared_sort_server_side(
    presentation_module: _PresentationModule,
) -> None:
    """The browser never reorders: a declared sort re-orders the served display rows."""
    pages = RecordingPages()
    pointer, body = _retail_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body)

    response = presentation_module.handle(
        _retail_health_event(
            {"session_id": SESSION, "sort": {"field": "glance_views", "direction": "desc"}}
        ),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(), pages, ScenarioQueries(), preset_artifacts=artifacts
            ),
        ),
    )
    payload = _body(response)

    assert payload["kind"] == "page"
    page = cast("dict[str, object]", payload["page"])
    rows = cast("list[dict[str, object]]", cast("dict[str, object]", page["artifact"])["rows"])
    assert [row["child_asin"] for row in rows] == ["FICTB", "FICTA"]
    persisted = pages.artifact_records[0].artifact.payload
    persisted_rows = cast("list[dict[str, object]]", persisted["rows"])
    assert [row["child_asin"] for row in persisted_rows] == ["FICTB", "FICTA"]


def test_v2_retail_health_requires_the_versioned_representation(
    presentation_module: _PresentationModule,
) -> None:
    """There is no legacy measurement branch behind the Retail Health route."""
    pointer, body = _retail_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body)
    event = _event(RETAIL_HEALTH_LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/json"}

    with pytest.raises(ValidationError) as raised:
        presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    RecordingPages(),
                    ScenarioQueries(),
                    preset_artifacts=artifacts,
                ),
            ),
        )

    assert raised.value.code == presentation_module.module.PRESENTATION_REFUSED
    assert artifacts.pointer_reads == []
    assert artifacts.roster_reads == []


PORTFOLIO_FOCUS_PATH = "/api/presentation/portfolio-metric-focus"
RETAIL_FOCUS_PATH = "/api/presentation/retail-metric-focus"
DEEP_DIVE_PATH = "/api/presentation/insight-deep-dive"


def _complete_campaign_preset_artifact(
    *, material: bool = False
) -> tuple[PresetArtifactPointer, PresetArtifactBody]:
    """One baked 7D campaigns body; `material` sizes spend to clear Gate-1 (ADR-076 §5).

    Material: the campaign's spend moves 1000 (campaign_window tactical row 300) off a
    prior of 1000, and the portfolio's moves 5000 (portfolio_window tactical row 4000).
    """
    period = DateRange(date(2026, 8, 13), date(2026, 8, 19))
    row = portfolio_row(
        campaign_display_signature("Fictional Campaign 001"),
        CampaignSelectionCapability("baked:00000", datetime(2027, 1, 1, tzinfo=UTC)),
        period,
        Decimal("2000") if material else Decimal("10"),
        prior_spend=Decimal("1000") if material else Decimal("5"),
    )
    totals = (
        portfolio_totals(period, spend=(Decimal("30000"), Decimal("25000"))) if material else None
    )
    artifact = complete_portfolio(period, (row,), totals=totals)
    revision = PresetArtifactRevision.from_publication_identities(("publication-1",))
    body = PresetArtifactBody(
        PresetArtifactPage("campaigns"),
        PresetArtifactVariant("7d"),
        revision,
        canonical_preset_artifact_json(campaign_portfolio_artifact_payload(artifact)),
    )
    return PresetArtifactPointer(body.page, body.variant, body.revision, body.digest, 1), body


@pytest.mark.covers("ADR-076")
def test_deep_dive_route_serves_the_deep_analysis_for_the_exact_baked_slot_identity(
    presentation_module: _PresentationModule,
) -> None:
    """A dialog click calls the deep assembler for the same baked artifact.

    It never invokes the card narrator or a live measurement query.
    """

    @dataclass
    class DeepAssembler:
        requests: list[InsightAssemblyRequest] = field(default_factory=list[InsightAssemblyRequest])

        def deep_dive(self, assembly: InsightAssemblyRequest, **kwargs: object) -> InsightDeepServe:
            self.requests.append(assembly)
            assert kwargs["correlation_id"] == ""
            return InsightDeepServe(
                InsightDeepDiveState.FILLED,
                InsightDeepAnalysis(
                    "A fictional movement stands out.",
                    ("Reading one: it may be temporary.",),
                    "The next delivered window would discriminate.",
                    ("100",),
                ),
                None,
                "v9",
                "miss",
                1200,
            )

    pages = RecordingPages()
    pointer, body = _complete_campaign_preset_artifact(material=True)
    artifacts = RecordingPresetArtifacts(pointer, body)
    catalog = RecordingCatalog()
    queries = ScenarioQueries()
    assembler = DeepAssembler()
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(catalog, pages, queries, preset_artifacts=artifacts),
    )
    object.__setattr__(runtime, "insight_assembler", assembler)

    response = presentation_module.handle(
        _event(
            DEEP_DIVE_PATH,
            {"session_id": SESSION, "slot_id": "contribution"},
        ),
        runtime,
    )
    payload = _body(response)

    assert response["statusCode"] == 200
    assert queries.seen == []
    assert len(assembler.requests) == 1
    assert assembler.requests[0].key.slot_id is InsightSlotId.CONTRIBUTION
    # The served provenance is the derivation fingerprint the baked cards carry,
    # never the pointer body's revision: the dialog refuses on a mismatch (the
    # 2026-09-24 live-acceptance defect).
    assert payload["artifact_revision"] == assembler.requests[0].key.artifact_revision
    assert cast("str", payload["artifact_revision"]).startswith("sha256:")
    assert payload["state"] == "filled"
    assert payload["deep_prompt_version"] == "v9"
    assert cast("dict[str, object]", payload["deep"])["what_stands_out"] == (
        "A fictional movement stands out."
    )
    assert payload["period"] == {"start": "2026-08-13", "end": "2026-08-19"}


@pytest.mark.covers("ADR-076")
def test_deep_dive_metric_focus_enriches_only_on_generation_with_full_focus_evidence(
    presentation_module: _PresentationModule,
) -> None:
    """Eager sibling reads would tax every cache-hit serve with Athena-priced work.

    The deep payload must type the complete focus evidence as citable findings, and
    the enrichment (including sibling reads) must run only when the assembler
    actually generates.
    """

    @dataclass
    class EnrichCapturingAssembler:
        enriched: list[InsightAssemblyRequest] = field(default_factory=list[InsightAssemblyRequest])

        def deep_dive(self, assembly: InsightAssemblyRequest, **kwargs: object) -> InsightDeepServe:
            enrich = cast(
                "Callable[[InsightAssemblyRequest], InsightAssemblyRequest] | None",
                kwargs.get("enrich"),
            )
            assert enrich is not None
            self.enriched.append(enrich(assembly))
            return InsightDeepServe(
                InsightDeepDiveState.FILLED,
                InsightDeepAnalysis(
                    "A fictional movement stands out.",
                    ("Reading one: it may be temporary.",),
                    "The next delivered window would discriminate.",
                    ("100",),
                ),
                None,
                "v9",
                "miss",
                1500,
            )

    pages = RecordingPages()
    pointer, body = _complete_campaign_preset_artifact(material=True)
    artifacts = RecordingPresetArtifacts(pointer, body)
    assembler = EnrichCapturingAssembler()
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(
            RecordingCatalog(), pages, ScenarioQueries(), preset_artifacts=artifacts
        ),
    )
    object.__setattr__(runtime, "insight_assembler", assembler)

    response = presentation_module.handle(
        _event(
            DEEP_DIVE_PATH,
            {"session_id": SESSION, "slot_id": "metric-focus", "metric": "spend"},
        ),
        runtime,
    )

    assert response["statusCode"] == 200
    (enriched,) = assembler.enriched
    assert enriched.key.slot_id is InsightSlotId.METRIC_FOCUS
    assert enriched.key.subject == "campaign-portfolio:metric-focus:spend"
    names = {finding.name for finding in enriched.findings}
    # The complete focus evidence (ADR-076 decision 1): window values, the daily
    # series with aligned priors, the computed facts strip, and driver operands.
    assert "Ad spend current window value" in names
    assert "Ad spend aligned prior value" in names
    assert any(name.startswith("Ad spend on 2026-08-") for name in names)
    assert any("fact: Peak day" in name for name in names)
    # Governed labels only: a snake_case fact ID in a model-visible finding name
    # could be echoed by the model into rendered prose.
    assert all("peak_day" not in name for name in names)
    assert any(name.startswith("Ad spend driver ") for name in names)


@pytest.mark.covers("ADR-076")
def test_deep_dive_route_serves_retail_slots_from_the_retail_baked_artifact(
    presentation_module: _PresentationModule,
) -> None:
    @dataclass
    class DeepAssembler:
        requests: list[InsightAssemblyRequest] = field(default_factory=list[InsightAssemblyRequest])

        def deep_dive(self, assembly: InsightAssemblyRequest, **kwargs: object) -> InsightDeepServe:
            del kwargs
            self.requests.append(assembly)
            return InsightDeepServe(
                InsightDeepDiveState.FILLED,
                InsightDeepAnalysis(
                    "A fictional retail movement stands out.",
                    ("Reading one: it may be temporary.",),
                    "The next delivered window would discriminate.",
                    (),
                ),
                None,
                "v3",
                "hit",
                0,
            )

    pages = RecordingPages()
    pointer, body = _retail_preset_artifact(scale=200)
    artifacts = RecordingPresetArtifacts(pointer, body)
    assembler = DeepAssembler()
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(
            RecordingCatalog(), pages, ScenarioQueries(), preset_artifacts=artifacts
        ),
    )
    object.__setattr__(runtime, "insight_assembler", assembler)

    response = presentation_module.handle(
        _event(
            DEEP_DIVE_PATH,
            {"session_id": SESSION, "slot_id": "demand-shape"},
        ),
        runtime,
    )
    payload = _body(response)

    assert response["statusCode"] == 200
    (request,) = assembler.requests
    assert request.key.slot_id is InsightSlotId.DEMAND_SHAPE
    assert request.key.subject == "retail-health"
    assert payload["artifact_revision"] == request.key.artifact_revision
    assert payload["state"] == "filled"
    assert payload["deep_prompt_version"] == "v3"


def test_deep_dive_route_refuses_deterministic_movers_before_any_artifact_read(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    artifacts = RecordingPresetArtifacts(None, None)
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(
            RecordingCatalog(), pages, ScenarioQueries(), preset_artifacts=artifacts
        ),
    )

    with pytest.raises(ValidationError, match="declares no deep analysis"):
        presentation_module.handle(
            _event(DEEP_DIVE_PATH, {"session_id": SESSION, "slot_id": "movers"}),
            runtime,
        )

    assert artifacts.roster_reads == []
    assert artifacts.body_reads == []


@dataclass
class _RecordingDeepAssembler:
    """Record the resolved assemblies (enriched when the route supplies enrichment)."""

    requests: list[InsightAssemblyRequest] = field(default_factory=list[InsightAssemblyRequest])

    def deep_dive(self, assembly: InsightAssemblyRequest, **kwargs: object) -> InsightDeepServe:
        enrich = cast(
            "Callable[[InsightAssemblyRequest], InsightAssemblyRequest] | None",
            kwargs.get("enrich"),
        )
        self.requests.append(assembly if enrich is None else enrich(assembly))
        return InsightDeepServe(
            InsightDeepDiveState.FILLED,
            InsightDeepAnalysis(
                "A fictional detail movement stands out.",
                ("Reading one: it may be temporary.",),
                "The next delivered window would discriminate.",
                (),
            ),
            None,
            "v4",
            "miss",
            900,
        )


def _v2_redeemable_campaign_context(
    presentation_module: _PresentationModule,
    channel: ChannelKey = CHANNEL,
) -> tuple[RecordingPages, ScenarioQueries, CampaignSelectionCapabilityRecord]:
    """Serve the v2 campaigns preset so the minted capability can be redeemed.

    Redemption demands a parent stored WITH temporal control, which only the v2
    preset serve records (the v1 list stores none).
    """
    pages = RecordingPages()
    pointer, body, custody = _campaign_preset_artifact_with_custody(channel=channel)
    artifacts = RecordingPresetArtifacts(pointer, body)
    custody_store = RecordingCustodyStore(custody)
    queries = ScenarioQueries(
        suppressed_metrics=frozenset(),
        truncated_metrics=frozenset(),
    )
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                pages,
                queries,
                preset_artifacts=artifacts,
                preset_custody=custody_store,
            ),
        ),
    )
    assert len(pages.campaign_capability_records) == 1
    return pages, queries, pages.campaign_capability_records[0]


@pytest.mark.covers("ADR-076")
def test_deep_dive_detail_slot_redeems_the_capability_and_serves_the_assembly_revision(
    presentation_module: _PresentationModule,
) -> None:
    """The served provenance must be the picked assembly key's revision.

    The detail derivation fingerprints its own payload, and the frontend refuses on
    a revision mismatch: the wrong choice here would turn every detail deep open
    into a silent refusal.
    """
    pages, queries, capability = _v2_redeemable_campaign_context(presentation_module)
    assembler = _RecordingDeepAssembler()
    catalog = RecordingCatalog(
        lookup_result=EntityCatalogLookupResult((_alias(CAMPAIGN, "Fictional Campaign 001"),)),
    )
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(catalog, pages, queries, publications=StubPublications()),
    )
    object.__setattr__(runtime, "insight_assembler", assembler)

    response = presentation_module.handle(
        _event(
            DEEP_DIVE_PATH,
            {
                "session_id": SESSION,
                "slot_id": "funnel",
                "selection_reference": capability.reference.value,
            },
        ),
        runtime,
    )
    payload = _body(response)

    assert response["statusCode"] == 200
    (request,) = assembler.requests
    assert request.key.slot_id is InsightSlotId.FUNNEL
    assert payload["state"] == "filled"
    assert payload["deep_prompt_version"] == "v4"
    assert payload["artifact_revision"] == request.key.artifact_revision
    assert payload["period"] == {"start": "2026-08-13", "end": "2026-08-19"}


@pytest.mark.covers("ADR-076")
def test_deep_dive_detail_metric_focus_enriches_the_selection_bound_focus_evidence(
    presentation_module: _PresentationModule,
) -> None:
    pages, queries, capability = _v2_redeemable_campaign_context(presentation_module)
    # The stub answers both windows identically by default (zero change, so the
    # focus predicate correctly refuses); give the two spend windows different
    # per-day values — daily series and aggregate stay consistent — so the focus
    # card fills and the deep route can serve. The 7D spend moves 560, above the
    # campaign_window tactical-review row of 300 (ADR-076 §5).
    queries.window_metric_values = {
        ("ad_spend", date(2026, 8, 13)): Decimal("90"),
        ("ad_spend", date(2026, 8, 6)): Decimal("10"),
    }
    assembler = _RecordingDeepAssembler()
    catalog = RecordingCatalog(
        lookup_result=EntityCatalogLookupResult((_alias(CAMPAIGN, "Fictional Campaign 001"),)),
    )
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(catalog, pages, queries, publications=StubPublications()),
    )
    object.__setattr__(runtime, "insight_assembler", assembler)

    response = presentation_module.handle(
        _event(
            DEEP_DIVE_PATH,
            {
                "session_id": SESSION,
                "slot_id": "metric-focus",
                "metric": "spend",
                "selection_reference": capability.reference.value,
            },
        ),
        runtime,
    )
    payload = _body(response)

    assert response["statusCode"] == 200
    (request,) = assembler.requests
    assert request.key.slot_id is InsightSlotId.METRIC_FOCUS
    assert request.key.subject.endswith(":metric-focus:spend")
    names = {finding.name for finding in request.findings}
    assert "Ad spend current window value" in names
    assert "Ad spend aligned prior value" in names
    # The redeemed detail's revision is the focus provenance for this family.
    assert payload["artifact_revision"] == request.key.artifact_revision
    assert payload["state"] == "filled"


def test_deep_dive_detail_slot_refuses_a_reporting_period_beside_the_selection(
    presentation_module: _PresentationModule,
) -> None:
    """A selection-bound deep request is fixed to the redeemed detail's window."""
    pages, queries, capability = _v2_redeemable_campaign_context(presentation_module)
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(RecordingCatalog(), pages, queries),
    )

    with pytest.raises(ValidationError, match="no reporting period"):
        presentation_module.handle(
            _event(
                DEEP_DIVE_PATH,
                {
                    "session_id": SESSION,
                    "slot_id": "trend-shape",
                    "selection_reference": capability.reference.value,
                    "temporal_selection": {"kind": "preset", "preset": "7D"},
                },
            ),
            runtime,
        )


@pytest.mark.covers("ADR-070")
def test_portfolio_metric_focus_serves_from_the_baked_body_without_measurement_reads(
    presentation_module: _PresentationModule,
) -> None:
    """The focus payload is byte-derivable from the baked artifact; no query or catalog."""
    pages = RecordingPages()
    pointer, body = _complete_campaign_preset_artifact(material=True)
    artifacts = RecordingPresetArtifacts(pointer, body)
    catalog = RecordingCatalog()
    queries = ScenarioQueries()

    response = presentation_module.handle(
        _event(PORTFOLIO_FOCUS_PATH, {"session_id": SESSION, "metric": "spend"}),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(catalog, pages, queries, preset_artifacts=artifacts),
        ),
    )
    payload = _body(response)

    assert response["statusCode"] == 200
    assert queries.seen == []
    assert catalog.browse_calls == []
    assert artifacts.body_reads == [pointer]
    assert payload["focus_version"] == 4
    metric = cast("dict[str, object]", payload["metric"])
    assert metric["id"] == "spend"
    assert metric["label"] == "Ad spend"
    assert isinstance(metric["definition"], str) and metric["definition"].strip()
    assert cast("dict[str, object]", metric["unit"])["kind"] == "currency"
    assert metric["basis"] == "ad_attributed"
    current = cast("dict[str, object]", payload["current"])
    assert current["value"] == "30000"
    # The exact value is served beside its two-place USD display (ADR-084).
    assert current["display_value"] == "30000.00"
    assert cast("dict[str, object]", current["units"]) == {
        "kind": "currency",
        "currency_code": "USD",
    }
    assert cast("dict[str, object]", current["period"]) == {
        "start": "2026-08-13",
        "end": "2026-08-19",
    }
    assert current["grain"] == ["period", "marketplace"]
    # Spend rose 20% and has no favourable direction, so its change is neutral (ADR-121).
    assert payload["change"] == {
        "direction": "up",
        "sentiment": "neutral",
        "unit": "percent",
        "display_text": "+20.0%",
        "not_meaningful": None,
    }
    series = cast("dict[str, object]", payload["series"])
    assert len(cast("list[object]", series["points"])) == 7
    facts = cast("list[dict[str, object]]", payload["facts"])
    assert [fact["id"] for fact in facts] == [
        "peak_day",
        "trough_day",
        "largest_day_move",
        "window_total",
    ]
    # A USD window total of 70 reads at two places with its code.
    assert facts[3]["value"] == "70.00 USD"
    drivers = cast("dict[str, object]", payload["drivers"])
    assert drivers["grain"] == "campaign"
    entries = cast("list[dict[str, object]]", drivers["entries"])
    assert entries[0]["name"] == "Fictional Campaign 001"
    assert entries[0]["change"] == "1000"
    # Each entry carries its own units; a USD change of 1000 displays as 1000.00.
    assert entries[0]["units"] == {"kind": "currency", "currency_code": "USD"}
    assert entries[0]["display_change"] == "1000.00"
    assert cast("dict[str, object]", entries[0]["delta"])["display_text"] == "+100.0%"
    assert entries[0]["share_of_move"] == "20%"
    assert payload["neighbors"] == [
        "spend",
        "purchases",
        "impressions",
        "portfolio_roas_attributed_14d",
    ]
    insight = cast("dict[str, object]", payload["insight"])
    assert insight["id"] == "metric-focus"
    assert insight["state"] == "unavailable"
    assert payload["boundary"] is None


def test_portfolio_metric_focus_states_a_designed_absent_decomposition(
    presentation_module: _PresentationModule,
) -> None:
    """Impressions carry no per-campaign rows, so the drivers block states why."""
    pointer, body = _complete_campaign_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body)

    response = presentation_module.handle(
        _event(PORTFOLIO_FOCUS_PATH, {"session_id": SESSION, "metric": "impressions"}),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(), RecordingPages(), ScenarioQueries(), preset_artifacts=artifacts
            ),
        ),
    )
    payload = _body(response)

    drivers = cast("dict[str, object]", payload["drivers"])
    assert set(drivers) == {"absent_reason"}
    assert "do not carry" in cast("str", drivers["absent_reason"])
    # An absent series renders value facts rather than a blank panel.
    assert payload["series"] is None
    facts = cast("list[dict[str, object]]", payload["facts"])
    assert [fact["id"] for fact in facts] == ["window_value", "aligned_prior"]


def test_portfolio_metric_focus_refuses_custom_windows_and_undeclared_metrics(
    presentation_module: _PresentationModule,
) -> None:
    pointer, body = _complete_campaign_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body)
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(
            RecordingCatalog(), RecordingPages(), ScenarioQueries(), preset_artifacts=artifacts
        ),
    )

    with pytest.raises(ValidationError, match="declared presets only"):
        presentation_module.handle(
            _event(
                PORTFOLIO_FOCUS_PATH,
                {
                    "session_id": SESSION,
                    "metric": "spend",
                    "temporal_selection": {
                        "kind": "custom",
                        "start": "2026-08-15",
                        "end": "2026-08-21",
                    },
                },
            ),
            runtime,
        )
    with pytest.raises(ValidationError, match="metric focus is unavailable"):
        presentation_module.handle(
            _event(PORTFOLIO_FOCUS_PATH, {"session_id": SESSION, "metric": "ctr"}),
            runtime,
        )
    assert artifacts.body_reads == []


@pytest.mark.covers("ADR-070")
def test_retail_metric_focus_serves_a_derived_tile_with_ratio_component_drivers(
    presentation_module: _PresentationModule,
) -> None:
    """A derived ratio decomposes into its carried operand movements, measurement-free."""
    pointer, body = _retail_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body)
    catalog = RecordingCatalog()
    queries = ScenarioQueries()

    response = presentation_module.handle(
        _event(RETAIL_FOCUS_PATH, {"session_id": SESSION, "metric": "return_rate"}),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                catalog, RecordingPages(), queries, preset_artifacts=artifacts
            ),
        ),
    )
    payload = _body(response)

    assert response["statusCode"] == 200
    assert queries.seen == []
    assert catalog.browse_calls == []
    assert artifacts.body_reads == [pointer]
    assert payload["focus_version"] == 4
    metric = cast("dict[str, object]", payload["metric"])
    assert metric["id"] == "return_rate"
    assert metric["basis"] == "retail_actual"
    current = cast("dict[str, object]", payload["current"])
    assert current["value"] == "0.02"
    assert cast("dict[str, object]", current["units"])["kind"] == "ratio"
    assert current["display_value"] is None
    drivers = cast("dict[str, object]", payload["drivers"])
    assert drivers["grain"] == "ratio_component"
    entries = cast("list[dict[str, object]]", drivers["entries"])
    assert [entry["name"] for entry in entries] == ["Returned units", "Shipped units"]
    assert entries[0]["change"] == "15"
    # Unit counts carry their own count units and no display change.
    assert [entry["units"] for entry in entries] == [
        {"kind": "count", "currency_code": None},
        {"kind": "count", "currency_code": None},
    ]
    assert [entry["display_change"] for entry in entries] == [None, None]
    assert payload["series"] is None
    assert payload["next_check"] is None
    assert payload["boundary"] == "Fictional retail boundary statement."
    insight = cast("dict[str, object]", payload["insight"])
    assert insight["id"] == "retail-metric-focus"


@pytest.mark.covers("ADR-084")
def test_retail_returns_value_focus_entries_carry_their_own_units(
    presentation_module: _PresentationModule,
) -> None:
    """Returned units stay a raw count; the average selling price is two-place money."""
    pointer, body = _retail_preset_artifact()
    response = presentation_module.handle(
        _event(
            RETAIL_FOCUS_PATH,
            {"session_id": SESSION, "metric": "returns_value_estimate_retail"},
        ),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                RecordingPages(),
                ScenarioQueries(),
                preset_artifacts=RecordingPresetArtifacts(pointer, body),
            ),
        ),
    )
    payload = _body(response)
    assert response["statusCode"] == 200
    assert payload["focus_version"] == 4
    drivers = cast("dict[str, object]", payload["drivers"])
    assert drivers["grain"] == "ratio_component"
    returned, price = cast("list[dict[str, object]]", drivers["entries"])
    # Returns move 15 -> 30 units: a count, with no display change.
    assert returned["name"] == "Returned units"
    assert returned["units"] == {"kind": "count", "currency_code": None}
    assert returned["display_change"] is None
    # Shipped revenue 15 -> 30 USD over 15 -> 30 units: the USD price holds at 1, so the
    # change displays as 0.00 in the shipped-revenue currency.
    assert price["name"] == "Average selling price"
    assert price["units"] == {"kind": "currency", "currency_code": "USD"}
    assert price["display_change"] == "0.00"


def test_retail_metric_focus_trend_metric_carries_series_and_product_drivers(
    presentation_module: _PresentationModule,
) -> None:
    pointer, body = _retail_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body)

    response = presentation_module.handle(
        _event(RETAIL_FOCUS_PATH, {"session_id": SESSION, "metric": "customer_returns"}),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(), RecordingPages(), ScenarioQueries(), preset_artifacts=artifacts
            ),
        ),
    )
    payload = _body(response)

    series = cast("dict[str, object]", payload["series"])
    points = cast("list[dict[str, object]]", series["points"])
    assert len(points) == 7
    assert points[0]["current"] == 2
    assert points[0]["prior"] == 1
    drivers = cast("dict[str, object]", payload["drivers"])
    assert drivers["grain"] == "product"
    entries = cast("list[dict[str, object]]", drivers["entries"])
    assert entries[0]["name"] == "Fictional Product FICTA (FICTA)"
    assert entries[0]["change"] == "-1"
    neighbors = cast("list[str]", payload["neighbors"])
    assert neighbors == list(RETAIL_HEALTH_TOTAL_METRICS)


@pytest.mark.covers("ADR-076")
@pytest.mark.parametrize(
    ("path", "metric"),
    [(PORTFOLIO_FOCUS_PATH, metric) for metric in CAMPAIGN_PORTFOLIO_FOCUS_METRICS]
    + [(RETAIL_FOCUS_PATH, metric) for metric in RETAIL_HEALTH_TOTAL_METRICS],
)
def test_every_focus_metric_builds_findings_its_contract_declares(
    presentation_module: _PresentationModule, path: str, metric: str
) -> None:
    """Every focus branch names a declared (kind, metric, basis) and a declared threshold.

    An undeclared triple raises in `finding_unit` and an undeclared row in `threshold_for`
    (ADR-076 §5); both would fail this route instead of serving the dialog.
    """
    pointer, body = (
        _complete_campaign_preset_artifact(material=True)
        if path == PORTFOLIO_FOCUS_PATH
        else _retail_preset_artifact(scale=200)
    )
    response = presentation_module.handle(
        _event(path, {"session_id": SESSION, "metric": metric}),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                RecordingPages(),
                ScenarioQueries(),
                preset_artifacts=RecordingPresetArtifacts(pointer, body),
            ),
        ),
    )

    assert response["statusCode"] == 200
    insight = cast("dict[str, object]", _body(response)["insight"])
    assert insight["state"] in {"unavailable", "silent"}


@pytest.mark.covers("ADR-071")
def test_a_parent_custody_without_its_sibling_index_is_malformed_never_a_retained_slice(
    presentation_module: _PresentationModule,
) -> None:
    """ADR-145: every sibling is read through the index; a custody without one is refused.

    Format 3's retained-population slice (a parent custody naming no siblings) is no fallback:
    the same parent custody with its index serves the page (the paired control).
    """
    pointer, body, custody = _campaign_preset_artifact_with_custody()
    unindexed_payload = cast("dict[str, object]", json.loads(custody.payload_json))
    unindexed_payload.pop("sibling_index")
    unindexed = PresetArtifactCustodyManifest(
        custody.page,
        custody.variant,
        custody.revision,
        canonical_preset_artifact_json(unindexed_payload),
    )

    def serve(manifest: PresetArtifactCustodyManifest) -> dict[str, Any]:
        event = _event(LIST_PATH, {"session_id": SESSION})
        event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
        return presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    RecordingPages(),
                    ScenarioQueries(),
                    preset_artifacts=RecordingPresetArtifacts(
                        replace(pointer, custody_digest=manifest.digest), body
                    ),
                    preset_custody=RecordingCustodyStore(manifest),
                ),
            ),
        )

    assert serve(custody)["statusCode"] == 200
    with pytest.raises(InvariantError) as raised:
        serve(unindexed)
    assert raised.value.code == presentation_module.module.ARTIFACT_MALFORMED


class _CardOrDeepAssembler:
    """Fill every card request and record each deep request's assembly."""

    def __init__(self) -> None:
        self.deep: list[InsightAssemblyRequest] = []

    def many(
        self, requests: tuple[InsightAssemblyRequest, ...], **kwargs: object
    ) -> tuple[InsightSlot, ...]:
        del kwargs
        return tuple(
            InsightSlot(
                request.key.slot_id,
                request.output_class,
                InsightSlotState.SILENT,
                None,
                None,
                request.key.artifact_revision,
                "v1",
            )
            for request in requests
        )

    def deep_dive(self, assembly: InsightAssemblyRequest, **kwargs: object) -> InsightDeepServe:
        del kwargs
        self.deep.append(assembly)
        return InsightDeepServe(InsightDeepDiveState.FILLED, None, None, "v1", "hit", 0)


def _w11_runtime(
    presentation_module: _PresentationModule, assembler: _CardOrDeepAssembler
) -> object:
    pointer, body, custody, sibling_body, sibling_custody = _segmented_campaign_preset_artifact(
        material=True
    )
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(
            RecordingCatalog(),
            RecordingPages(tokens=(TOKEN, SECOND_SELECTION_TOKEN, "U" * 43, "V" * 43)),
            ScenarioQueries(),
            preset_artifacts=RecordingPresetArtifacts(pointer, body, sibling_body=sibling_body),
            preset_custody=RecordingCustodyStore(custody, sibling_custody),
        ),
    )
    object.__setattr__(runtime, "insight_assembler", assembler)
    return runtime


def _w11_card_revision(
    presentation_module: _PresentationModule, sort: dict[str, str] | None
) -> str:
    """Serve the page under one sort and return its contribution card's revision."""
    assembler = _CardOrDeepAssembler()
    event = _event(LIST_PATH, {"session_id": SESSION, **({} if sort is None else {"sort": sort})})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    payload = _body(presentation_module.handle(event, _w11_runtime(presentation_module, assembler)))
    page = cast("dict[str, object]", payload["page"])
    insights = cast(
        "list[dict[str, object]]", cast("dict[str, object]", page["artifact"])["insights"]
    )
    card = next(item for item in insights if item["id"] == "contribution")
    return cast("str", card["artifact_revision"])


def _w11_deep(
    presentation_module: _PresentationModule, extra: dict[str, object]
) -> tuple[dict[str, object], list[InsightAssemblyRequest]]:
    assembler = _CardOrDeepAssembler()
    response = presentation_module.handle(
        _event(DEEP_DIVE_PATH, {"session_id": SESSION, "slot_id": "contribution", **extra}),
        _w11_runtime(presentation_module, assembler),
    )
    return _body(response), assembler.deep


@pytest.mark.covers("ADR-076")
def test_deep_under_a_page_sort_reads_the_same_segment_as_that_pages_card(
    presentation_module: _PresentationModule,
) -> None:
    """W11: the deep assembly is built from the (sort, segment) artifact the card came from."""
    by_name = {"field": "name", "direction": "asc"}
    card = _w11_card_revision(presentation_module, by_name)
    payload, (assembly,) = _w11_deep(presentation_module, {"sort": by_name, "segment": 0})

    assert assembly.key.artifact_revision == card
    assert payload["artifact_revision"] == card
    # Positive control: without the sort, deep reads the canonical segment, whose revision
    # differs from the name-sorted card's, which is the defect this fixes.
    unsorted, _ = _w11_deep(presentation_module, {})
    assert unsorted["artifact_revision"] != card


@pytest.mark.covers("ADR-076")
def test_deep_on_the_canonical_sort_matches_the_canonical_card(
    presentation_module: _PresentationModule,
) -> None:
    card = _w11_card_revision(presentation_module, None)
    payload, (assembly,) = _w11_deep(
        presentation_module, {"sort": {"field": "spend", "direction": "desc"}}
    )
    assert assembly.key.artifact_revision == card == payload["artifact_revision"]


@pytest.mark.covers("ADR-076")
@pytest.mark.parametrize(
    "body",
    [
        {
            "slot_id": "metric-focus",
            "metric": "spend",
            "sort": {"field": "spend", "direction": "desc"},
        },
        {"slot_id": "demand-shape", "segment": 1},
        {"slot_id": "contribution", "sort": {"field": "impressions", "direction": "desc"}},
        {"slot_id": "contribution", "segment": -1},
    ],
    ids=["sort-on-focus", "segment-on-retail", "invalid-sort-field", "invalid-segment"],
)
def test_deep_refuses_sort_or_segment_outside_the_portfolio_page_grammar(
    presentation_module: _PresentationModule, body: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        presentation_module.handle(
            _event(DEEP_DIVE_PATH, {"session_id": SESSION, **body}),
            _w11_runtime(presentation_module, _CardOrDeepAssembler()),
        )


@pytest.mark.covers("ADR-071")
def test_canonical_first_segment_reads_no_sibling_pair(
    presentation_module: _PresentationModule,
) -> None:
    pointer, body, custody, sibling_body, sibling_custody = _segmented_campaign_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body, sibling_body=sibling_body)
    custody_store = RecordingCustodyStore(custody, sibling_custody)
    pages = RecordingPages(tokens=(TOKEN, SECOND_SELECTION_TOKEN, "U" * 43, "V" * 43))
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                pages,
                ScenarioQueries(),
                preset_artifacts=artifacts,
                preset_custody=custody_store,
            ),
        ),
    )

    assert response["statusCode"] == 200
    assert artifacts.sibling_reads == []
    assert custody_store.sibling_reads == []


@pytest.mark.covers("ADR-071")
def test_noncanonical_segment_reads_exactly_one_attested_pair_and_never_measures(
    presentation_module: _PresentationModule,
) -> None:
    pointer, body, custody, sibling_body, sibling_custody = _segmented_campaign_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body, sibling_body=sibling_body)
    custody_store = RecordingCustodyStore(custody, sibling_custody)
    catalog = RecordingCatalog()
    queries = ScenarioQueries()
    pages = RecordingPages(tokens=(TOKEN, SECOND_SELECTION_TOKEN, "U" * 43, "V" * 43))
    event = _event(
        LIST_PATH,
        {
            "session_id": SESSION,
            "sort": {"field": "name", "direction": "asc"},
            "segment": 0,
        },
    )
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                catalog,
                pages,
                queries,
                preset_artifacts=artifacts,
                preset_custody=custody_store,
            ),
        ),
    )
    payload = _body(response)
    page = cast("dict[str, object]", payload["page"])
    artifact = cast("dict[str, object]", page["artifact"])
    rows = cast("list[dict[str, object]]", artifact["rows"])

    assert response["statusCode"] == 200
    # ADR-145's two-level read: the covering index shard, then the one attested pair.
    assert len(artifacts.sibling_reads) == 1
    shard, pair = custody_store.sibling_reads
    assert shard.descriptor.identity == PresetArtifactIndexShardIdentity(0)
    assert shard.descriptor.body_digest is None
    assert artifacts.sibling_reads[0] == pair
    assert [cast("dict[str, object]", row["signature"])["effective_name"] for row in rows] == [
        "Alpha",
        "Mike",
        "Zulu",
    ]
    assert catalog.browse_calls == []
    assert catalog.lookup_calls == []
    assert queries.seen == []


def _two_level_serve(
    presentation_module: _PresentationModule,
    custody_store: RecordingCustodyStore,
    artifacts: RecordingPresetArtifacts,
) -> dict[str, Any]:
    """Serve the name-ascending segment 0, the one sibling the segmented fixture indexes."""
    event = _event(
        LIST_PATH,
        {"session_id": SESSION, "sort": {"field": "name", "direction": "asc"}, "segment": 0},
    )
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    return presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                RecordingPages(tokens=(TOKEN, SECOND_SELECTION_TOKEN, "U" * 43, "V" * 43)),
                ScenarioQueries(),
                preset_artifacts=artifacts,
                preset_custody=custody_store,
            ),
        ),
    )


@pytest.mark.covers("ADR-145")
@pytest.mark.parametrize("defect", ["not-indexed", "refused-shard", "tampered-shard"])
def test_the_two_level_read_refuses_an_unindexed_identity_and_a_refused_or_tampered_shard(
    presentation_module: _PresentationModule, defect: str
) -> None:
    """Pointer -> parent custody -> covering shard -> descriptor -> body; never a fallback."""
    pointer, body, custody, sibling_body, sibling_custody = _segmented_campaign_preset_artifact()
    (shard,) = _INDEXED[custody.digest]
    # Paired control: the custody and shard as the stage wrote them serve the segment.
    served = _two_level_serve(
        presentation_module,
        RecordingCustodyStore(custody, sibling_custody),
        RecordingPresetArtifacts(pointer, body, sibling_body=sibling_body),
    )
    assert served["statusCode"] == 200
    manifest, shards = custody, (shard,)
    if defect == "not-indexed":
        # The index binds another segment only: the requested one has no descriptor.
        other = replace(sibling_body, identity=PresetArtifactSiblingIdentity("name-asc", 1, "all"))
        other_custody = replace(sibling_custody, identity=other.identity)
        header, _ = split_parent_custody(custody)
        manifest, shards = indexed_parent_custody(body, header, ((other, other_custody),))
    elif defect == "refused-shard":
        # The store refuses the shard read (as S3 does for bytes the entry doesn't bind).
        shards = ()
    else:
        payload = json.loads(shard.payload_json)
        payload["descriptors"][0]["body_digest"] = "0" * 64
        shards = (replace(shard, payload_json=canonical_preset_artifact_json(payload)),)
    store = RecordingCustodyStore(manifest, sibling_custody, shards=shards)
    artifacts = RecordingPresetArtifacts(
        replace(pointer, custody_digest=manifest.digest), body, sibling_body=sibling_body
    )

    with pytest.raises(InvariantError) as raised:
        _two_level_serve(presentation_module, store, artifacts)

    assert raised.value.code == presentation_module.module.ARTIFACT_MALFORMED
    assert artifacts.sibling_reads == []  # no body is read without its attested descriptor


@pytest.mark.covers("ADR-121")
@pytest.mark.parametrize(
    "sort",
    [None, {"field": "name", "direction": "asc"}],
    ids=["canonical-body", "sibling-segment"],
)
@pytest.mark.parametrize("retired", RETIRED_FORMS, ids=["format-6", "format-7"])
def test_v2_campaign_preset_refuses_a_retired_body_and_serves_a_current_one(
    presentation_module: _PresentationModule,
    retired: str,
    sort: dict[str, str] | None,
) -> None:
    """The format-6 and format-7 reads retired (owner revision, 2026-10-08): no dual path."""

    def serve(body_form: str | None, pages: RecordingPages) -> dict[str, Any]:
        pointer, body, custody, sibling_body, sibling_custody = _segmented_campaign_preset_artifact(
            retired=body_form
        )
        request: dict[str, object] = {"session_id": SESSION}
        if sort is not None:
            request |= {"sort": sort, "segment": 0}
        event = _event(LIST_PATH, request)
        event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
        return presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    pages,
                    ScenarioQueries(),
                    preset_artifacts=RecordingPresetArtifacts(
                        pointer, body, sibling_body=sibling_body
                    ),
                    preset_custody=RecordingCustodyStore(custody, sibling_custody),
                ),
            ),
        )

    # Paired positive control: the current body or sibling serves blocks and its coverage.
    pages = RecordingPages(tokens=(TOKEN, SECOND_SELECTION_TOKEN, "U" * 43, "V" * 43))
    response = serve(None, pages)
    assert response["statusCode"] == 200
    page = cast("dict[str, object]", _body(response)["page"])
    comparisons = _served_comparisons(cast("dict[str, object]", page["artifact"]))
    assert len(comparisons) == 4 + 3 * 4
    assert all(isinstance(item["change"], dict) for item in comparisons)
    assert all("percentage_change" not in item for item in comparisons)
    assert isinstance(cast("dict[str, object]", page["artifact"])["evidence_status"], dict)
    stored = json.loads(pages.artifact_records[0].artifact.payload_json)
    decoded = campaign_portfolio_artifact_from_payload(stored)
    assert all(item.change is not None for item in decoded.totals)

    refused_pages = RecordingPages(tokens=(TOKEN, SECOND_SELECTION_TOKEN, "U" * 43, "V" * 43))
    with pytest.raises(InvariantError) as refused:
        serve(retired, refused_pages)
    assert refused.value.code == presentation_module.module.ARTIFACT_MALFORMED
    assert refused.value.description == "Persisted durable artifact bytes are malformed."
    assert refused_pages.artifact_records == []


@pytest.mark.covers("ADR-119", "ADR-118")
@pytest.mark.parametrize(
    "sort",
    [None, {"field": "name", "direction": "asc"}],
    ids=["canonical-body", "sibling-segment"],
)
def test_v2_campaign_preset_serves_its_coverage_block_and_withheld_changes(
    presentation_module: _PresentationModule,
    sort: dict[str, str] | None,
) -> None:
    """A format-8 body serves its coverage block, and a withheld change stays withheld."""
    pointer, body, custody, sibling_body, sibling_custody = _segmented_campaign_preset_artifact(
        missing_spend_day=True
    )
    artifacts = RecordingPresetArtifacts(pointer, body, sibling_body=sibling_body)
    custody_store = RecordingCustodyStore(custody, sibling_custody)
    pages = RecordingPages(tokens=(TOKEN, SECOND_SELECTION_TOKEN, "U" * 43, "V" * 43))
    request: dict[str, object] = {"session_id": SESSION}
    if sort is not None:
        request |= {"sort": sort, "segment": 0}
    event = _event(LIST_PATH, request)
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                pages,
                ScenarioQueries(),
                preset_artifacts=artifacts,
                preset_custody=custody_store,
            ),
        ),
    )
    page = cast("dict[str, object]", _body(response)["page"])
    artifact = cast("dict[str, object]", page["artifact"])
    coverage = cast("dict[str, object]", artifact["evidence_status"])
    entries = cast("list[dict[str, object]]", coverage["entries"])
    comparisons = _served_comparisons(artifact)

    assert response["statusCode"] == 200
    assert entries[0]["slot"] == "spend"
    assert entries[0]["missing_dates"] == ["2026-08-19"]
    assert entries[0]["absence_text"] == "1 day not yet sent."
    assert entries[0]["coverage_text"] == "6 of 7 days"
    spend = [item for item in comparisons if item["metric"] == "spend"]
    assert len(spend) == 1 + 3
    assert all(
        item["change"] is None and item["absolute_change"] is None and item["current"]
        for item in spend
    )
    assert all(
        isinstance(item["change"], dict) for item in comparisons if item["metric"] != "spend"
    )
    stored = json.loads(pages.artifact_records[0].artifact.payload_json)
    decoded = campaign_portfolio_artifact_from_payload(stored)
    assert decoded.evidence_status is not None
    assert all(item.withheld for item in decoded.totals if item.metric.value == "spend")


class _SteppedCardAssembler:
    """Fill each card with a cached campaign step, keyed by the server-only campaign key."""

    def __init__(self, keys: dict[InsightSlotId, str]) -> None:
        self.keys = keys

    def many(
        self, requests: tuple[InsightAssemblyRequest, ...], **kwargs: object
    ) -> tuple[InsightSlot, ...]:
        del kwargs
        return tuple(
            InsightSlot(
                request.key.slot_id,
                request.output_class,
                InsightSlotState.FILLED,
                "A fictional reading.",
                "Read from the values this page already shows.",
                request.key.artifact_revision,
                "v1",
                next_step=InsightNextStep(
                    InsightNextStepKind.OPEN_CAMPAIGN_DETAIL,
                    CampaignKeyTarget("campaigns", self.keys[request.key.slot_id]),
                    "Open Zulu",
                    "Zulu moved the most this window.",
                ),
            )
            for request in requests
            if request.key.slot_id in self.keys
        )


@pytest.mark.covers("ADR-122")
def test_a_served_campaign_step_carries_this_responses_capability_or_is_dropped(
    presentation_module: _PresentationModule,
) -> None:
    """ADR-122 decision 4: a cached key becomes the row's fresh capability; no row, no step."""
    minted = campaign_step_key(CampaignKey("campaign:segment-0", SCOPE, PLATFORM))
    unminted = campaign_step_key(CampaignKey("campaign:not-in-this-segment", SCOPE, PLATFORM))
    assembler = _SteppedCardAssembler(
        {InsightSlotId.CONTRIBUTION: minted, InsightSlotId.EXPOSURE: unminted}
    )
    runtime = _w11_runtime(presentation_module, cast("Any", assembler))
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    response = presentation_module.handle(event, runtime)

    assert response["statusCode"] == 200
    artifact = cast(
        "dict[str, object]", cast("dict[str, object]", _body(response)["page"])["artifact"]
    )
    cards = {
        cast("str", item["id"]): item
        for item in cast("list[dict[str, object]]", artifact["insights"])
    }
    pages = cast("RecordingPages", cast("Any", runtime).parent_capability_issuance)
    (issued,) = pages.parent_capability_requests
    reference = next(
        record.reference.value
        for record in issued.capabilities
        if isinstance(record, CampaignSelectionCapabilityRecord)
        and campaign_step_key(record.campaign) == minted
    )
    assert cards["contribution"]["next_step"] == {
        "kind": "open_campaign_detail",
        "label": "Open Zulu",
        "reason": "Zulu moved the most this window.",
        "target": {"page_id": "campaigns", "campaign_ref": reference},
    }
    assert cards["exposure"]["state"] == "filled"
    assert "next_step" not in cards["exposure"]
    assert minted not in response["body"] and "campaign_key" not in response["body"]


@pytest.mark.covers("ADR-071")
def test_substituted_sibling_identity_fails_closed(
    presentation_module: _PresentationModule,
) -> None:
    pointer, body, custody, sibling_body, sibling_custody = _segmented_campaign_preset_artifact()
    substituted = PresetArtifactSiblingBody(
        sibling_body.page,
        sibling_body.variant,
        sibling_body.revision,
        PresetArtifactSiblingIdentity("spend-asc", 0),
        sibling_body.payload_json,
    )
    event = _event(
        LIST_PATH,
        {
            "session_id": SESSION,
            "sort": {"field": "name", "direction": "asc"},
            "segment": 0,
        },
    )
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}

    with pytest.raises(InvariantError, match="malformed"):
        presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    RecordingPages(),
                    ScenarioQueries(),
                    preset_artifacts=RecordingPresetArtifacts(
                        pointer, body, sibling_body=substituted
                    ),
                    preset_custody=RecordingCustodyStore(custody, sibling_custody),
                ),
            ),
        )


@pytest.mark.covers("ADR-068")
def test_gate_refused_slots_render_silent_and_unanswered_requests_stay_unavailable(
    presentation_module: _PresentationModule,
) -> None:
    """The deterministic matrix mandates Gate-1 refusal is silence, never a delivery failure."""
    module = presentation_module.module
    context = InsightRequestContext(
        "analytical-scope-v1:scope-fictional",
        "campaign-portfolio",
        DateRange(date(2026, 8, 1), date(2026, 8, 7)),
        "sha256:revision-fictional",
    )
    emitted = InsightAssemblyRequest(
        key=InsightCacheKey(
            analytical_scope_id=context.analytical_scope_id,
            subject=context.subject,
            window_start="2026-08-01",
            window_end="2026-08-07",
            artifact_revision=context.artifact_revision,
            prompt_version="active-pointer",
            slot_id=InsightSlotId.CONTRIBUTION,
            input_digest=FICTIONAL_INPUT_DIGEST,
        ),
        output_class=InsightOutputClass.PERSPECTIVE,
        purpose="Explain one server-owned contribution.",
        findings=(DeterministicFinding("Fictional Campaign", Decimal("5")),),
        silence_reason="No fictional contribution exceeds the declared threshold.",
        window_profile=InsightWindowProfile(
            days=7,
            grain=TemporalDisplayGrain.DAY,
            prior_start="2026-07-25",
            prior_end="2026-07-31",
            role=InsightHorizonRole.TACTICAL_REVIEW,
        ),
    )

    slots = module._assembled_insights(
        module._InsightAssemblyInput(
            runtime=cast("Any", SimpleNamespace(insight_assembler=None)),
            context=context,
            slot_ids=(InsightSlotId.CONTRIBUTION, InsightSlotId.DIVERGENCE),
            requests=(emitted,),
            silence_reasons={
                InsightSlotId.DIVERGENCE: (
                    "No campaign direction materially diverges from the portfolio direction."
                )
            },
            missing_evidence_by_slot={},
        )
    )

    contribution, divergence = slots
    # The emitted request went unanswered (no assembler here): a delivery failure.
    assert contribution.id is InsightSlotId.CONTRIBUTION
    assert contribution.state.value == "unavailable"
    assert contribution.absence_class is not None
    assert contribution.absence_class.value == "generation-pending"
    assert contribution.absence_reason is not None
    # No request was emitted for divergence: Gate 1 refused it, so silence is the answer,
    # and the served slot names the contract's silence condition as its business reason.
    assert divergence.id is InsightSlotId.DIVERGENCE
    assert divergence.state.value == "silent"
    assert divergence.prompt_version == "deterministic-gate1"
    assert divergence.artifact_revision == context.artifact_revision
    assert divergence.text is None
    assert divergence.absence_class is not None
    assert divergence.absence_class.value == "evaluated-quiet"
    assert (
        divergence.absence_reason
        == "No campaign direction materially diverges from the portfolio direction."
    )


@pytest.mark.covers("ADR-119")
def test_detail_coverage_sheet_reads_not_sent_for_a_slot_with_no_publication(
    presentation_module: _PresentationModule,
) -> None:
    """A slot refused because no publication arrived names no dates, so it reads "Not sent".

    "None" stays for a complete figure; the prior window's complete rows name that window.
    """
    pages, queries, capability = _v2_redeemable_campaign_context(presentation_module)
    pages.tokens = (THIRD_SELECTION_TOKEN, "W" * 43, "X" * 43)
    catalog = RecordingCatalog(
        lookup_result=EntityCatalogLookupResult((_alias(CAMPAIGN, "Fictional Campaign 001"),)),
    )
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(
            catalog,
            pages,
            queries,
            # Attribution inspects 7 plans; plans past them are delivery's alone.
            publications=StubPublications(inspection_absent_indices=frozenset({10, 11})),
        ),
    )
    event = _event(
        DETAIL_PATH, {"session_id": SESSION, "selection_reference": capability.reference.value}
    )
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    page = cast("dict[str, object]", _body(presentation_module.handle(event, runtime))["page"])
    artifact = cast("dict[str, object]", page["artifact"])
    delivery = cast("dict[str, object]", artifact["delivery"])
    conditions = {
        (cast("str", item["slot"]), cast("str", item["role"])): item["condition"]
        for item in cast("list[dict[str, object]]", delivery["evidence"])
    }
    coverage = cast("dict[str, object]", artifact["evidence_status"])
    entries = cast("list[dict[str, object]]", coverage["entries"])
    unsent = [
        entry
        for entry in entries
        if conditions.get((cast("str", entry["slot"]), cast("str", entry["role"])))
        in {"missing_current_publication", "missing_comparator_publication"}
    ]

    assert unsent, "the fixture serves delivery slots with no publication"
    assert all(entry["state"] == "unavailable" for entry in unsent)
    assert all(entry["missing_dates"] == [] for entry in unsent)
    assert all(entry["missing_text"] == "Not sent" for entry in unsent)
    complete = [entry for entry in entries if entry["state"] == "complete"]
    assert any(entry["role"] == "prior" for entry in complete)
    assert all(entry["missing_text"] == "None" for entry in complete)
    assert all(
        entry["reason"]
        == (
            "Every day of the prior window has arrived."
            if entry["role"] in {"prior", "prior_daily"}
            else "Every day of this window has arrived."
        )
        for entry in complete
    )


@pytest.mark.covers("ADR-076")
def test_detail_deep_serves_the_same_revision_as_the_detail_card_it_opens_from(
    presentation_module: _PresentationModule,
) -> None:
    """F12: the detail page's card and its deep section must name one artifact revision.

    The dialog refuses a deep response whose revision differs from its card's, so a
    mismatch here renders every detail deep section as "belongs to another page revision".
    """
    pages, queries, capability = _v2_redeemable_campaign_context(presentation_module)
    pages.tokens = (THIRD_SELECTION_TOKEN, "W" * 43, "X" * 43)
    catalog = RecordingCatalog(
        lookup_result=EntityCatalogLookupResult((_alias(CAMPAIGN, "Fictional Campaign 001"),)),
    )
    assembler = _CardOrDeepAssembler()
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(catalog, pages, queries, publications=StubPublications()),
    )
    object.__setattr__(runtime, "insight_assembler", assembler)
    event = _event(
        DETAIL_PATH, {"session_id": SESSION, "selection_reference": capability.reference.value}
    )
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    page = cast("dict[str, object]", _body(presentation_module.handle(event, runtime))["page"])
    insights = cast(
        "list[dict[str, object]]", cast("dict[str, object]", page["artifact"])["insights"]
    )
    slot_id = "funnel"
    card = next(item for item in insights if item["id"] == slot_id)

    deep = _body(
        presentation_module.handle(
            _event(
                DEEP_DIVE_PATH,
                {
                    "session_id": SESSION,
                    "slot_id": slot_id,
                    "selection_reference": capability.reference.value,
                },
            ),
            runtime,
        )
    )

    assert assembler.deep, "the detail card must be admitted for its deep section to serve"
    assert deep["artifact_revision"] == card["artifact_revision"]


# --- campaign-detail-500 cause A: a campaign's delivery through the real selector ------------

SPONSORED_PRODUCTS = ChannelKey("amazon_sponsored_products")
_NOT_MEASURED_FOR_CHANNEL = {
    "measurable_impressions": None,
    "outcome": "unavailable",
    "reason_code": "dsp_quality_not_measured_for_channel",
    "viewable_impressions": None,
    "viewable_impressions_estimated": None,
    "viewable_rate": None,
}


def _real_selector_runtime(
    presentation_module: _PresentationModule, channel: ChannelKey
) -> tuple[object, CampaignSelectionCapabilityRecord, _CardOrDeepAssembler]:
    """Return a redeemable campaign on `channel` whose evidence binds through the real selector.

    The stub selector binds every plan, so it can't see a plan no source pack serves; the
    real one runs the deployed `_binds`/`_selected_side` over the committed packs.
    """
    pages, queries, capability = _v2_redeemable_campaign_context(presentation_module, channel)
    pages.tokens = (THIRD_SELECTION_TOKEN, "W" * 43, "X" * 43)
    catalog = RecordingCatalog(
        lookup_result=EntityCatalogLookupResult(
            (_alias(CAMPAIGN, "Fictional Campaign 001", channel),)
        ),
    )
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(
            catalog,
            pages,
            queries,
            dimension_labels=(
                *DIMENSION_LABELS,
                PresentationDimensionLabel(
                    "channel_key", SPONSORED_PRODUCTS.value, "channel", "Fictional channel"
                ),
            ),
        ),
    )
    object.__setattr__(
        runtime,
        "presentation_evidence",
        PublicationSelector(
            RecordingCurrent(), load_source_packs(ROOT), load_commerce_identity_bridge(ROOT)
        ),
    )
    assembler = _CardOrDeepAssembler()
    object.__setattr__(runtime, "insight_assembler", assembler)
    return runtime, capability, assembler


def _served(  # noqa: PLR0913 -- one module, its runtime, a path and a body
    presentation_module: _PresentationModule,
    runtime: object,
    *,
    path: str,
    body: dict[str, object],
) -> dict[str, Any]:
    """Serve through the Lambda entrypoint, so an escaped exception is its real 500."""
    event = _event(path, {"session_id": SESSION, **body})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    with patch.object(presentation_module.module, "RUNTIME", runtime):
        return presentation_module.handler(event, object())


@pytest.mark.parametrize(
    ("channel", "outcome", "quality"),
    [
        (SPONSORED_PRODUCTS, "qualified", _NOT_MEASURED_FOR_CHANNEL),
        (CHANNEL, "complete", None),
    ],
    ids=["sponsored-products", "amazon-dsp"],
)
def test_campaign_detail_serves_a_sponsored_campaign_with_dsp_quality_typed_absent(  # noqa: PLR0913, PLR0917 -- fixture and cases
    presentation_module: _PresentationModule,
    channel: ChannelKey,
    outcome: str,
    quality: dict[str, object] | None,
) -> None:
    """A sponsored campaign's detail is a 200 whose DSP media quality isn't measured.

    Before the fix the delivery batch asked the DSP views pack for a sponsored pin, and the
    real selector raised `no source pack matches ('dsp_views',)`: a 500 ADLY-UNEXPECTED-0000.
    """
    runtime, capability, _ = _real_selector_runtime(presentation_module, channel)

    response = _served(
        presentation_module,
        runtime,
        path=DETAIL_PATH,
        body={"selection_reference": capability.reference.value},
    )

    assert response["statusCode"] == 200, response["body"]
    page = cast("dict[str, object]", _body(response)["page"])
    assert page["kind"] == "campaign_detail"
    delivery = cast("dict[str, Any]", cast("dict[str, object]", page["artifact"])["delivery"])
    assert delivery["outcome"] == outcome
    if quality is None:
        assert delivery["dsp_quality"]["outcome"] == "available"
        assert delivery["dsp_quality"]["reason_code"] is None
    else:
        assert delivery["dsp_quality"] == quality


# --- dsp-cpc-derive: cost per click from the accepted spend and clicks ------------------------

_DETAIL_WINDOW = DateRange(date(2026, 8, 13), date(2026, 8, 19))


@dataclass
class _WriterQueries(ScenarioQueries):
    """Serve the detail window's spend and clicks, and any `cpc` plan as its writers return it.

    `split` returns each `cpc` group as dev's Athena does for an Amazon DSP pin (2026-10-19):
    the DSP clicks writer's row has no currency and no spend, and the DSP impressions writer's
    row has the currency and spend but no clicks. Otherwise one row carries all three, as the
    Sponsored ads traffic writer does.
    """

    spend: Decimal = Decimal(0)
    clicks: tuple[int, ...] = ()
    split: bool = False

    def run(self, request: QueryRequest) -> QueryResult:
        result = super().run(request)
        plan = request.plan.logical
        metric = plan.metric.name
        current = plan.window == _DETAIL_WINDOW
        daily = "date_key" in plan.group_by
        rows: list[QueryRow] = []
        for offset, served in enumerate(result.rows):
            row = dict(served)
            # The operands the base fake serves elsewhere: spend 1, and one click a day.
            spend = self.spend if current and not daily else Decimal(1)
            clicks = (
                (self.clicks[offset] if daily else sum(self.clicks))
                if current
                else 1
                if daily
                else plan.window.days
            )
            if current and metric == "clicks":
                row[measure_alias(0)] = clicks
            elif current and metric == "ad_spend" and not daily:
                row[measure_alias(0)] = self.spend
            elif metric == "cpc":
                row[measure_alias(0)] = spend
                row[measure_alias(1)] = clicks
                if self.split:
                    rows.append({**row, "currency_code": None, measure_alias(0): None})
                    row[measure_alias(1)] = None
            rows.append(row)
        return replace(result, rows=tuple(rows))


def _writer_delivery(
    presentation_module: _PresentationModule, channel: ChannelKey, queries: _WriterQueries
) -> tuple[dict[str, Any], _WriterQueries]:
    """Serve a campaign detail whose delivery evidence comes from `queries`."""
    runtime, capability, _ = _real_selector_runtime(presentation_module, channel)
    tooling = cast("Any", runtime).measurements
    queries.suppressed_metrics = frozenset()
    queries.truncated_metrics = frozenset()
    object.__setattr__(tooling, "queries", queries)

    response = _served(
        presentation_module,
        runtime,
        path=DETAIL_PATH,
        body={"selection_reference": capability.reference.value},
    )

    assert response["statusCode"] == 200, response["body"]
    page = cast("dict[str, object]", _body(response)["page"])
    return cast("dict[str, Any]", cast("dict[str, object]", page["artifact"])["delivery"]), queries


def _slot_value(entries: list[dict[str, Any]], slot: str) -> object:
    (entry,) = (item for item in entries if item["slot"] == slot)
    return entry["value"]["value"]


def test_dsp_campaign_detail_derives_cpc_from_its_split_spend_and_clicks_writers(
    presentation_module: _PresentationModule,
) -> None:
    """DSP Flight 11's real split pair served every delivery figure blank before this fix.

    The pair is Flight 11's prior window on dev (Sep 23-29; its current window, Sep 30-Oct 6, is
    16886.8853 / 19754 = 0.8549), served here as the fixture's detail window. The planned `cpc`
    quotient grouped by currency split into a clicks-only row (14127, no currency) and a
    spend-only row (11771.3696 USD): no units, so `incompatible_scope`, and the all-or-nothing
    reduction then withheld all six delivery figures. CPC now divides the accepted spend and
    clicks: 11771.3696 / 14127 = 0.8333.
    """
    delivery, queries = _writer_delivery(
        presentation_module,
        CHANNEL,
        _WriterQueries(spend=Decimal("11771.3696"), clicks=(2019, *(2018,) * 6), split=True),
    )

    assert delivery["outcome"] == "complete"
    assert _slot_value(delivery["summary"], "ad_spend") == "11771.3696"
    assert _slot_value(delivery["summary"], "clicks") == 14127
    assert _slot_value(delivery["summary"], "cpc") == "0.8333"
    # Prior window: spend 1 over 7 clicks.
    assert _slot_value(delivery["comparison"]["slots"], "cpc") == "0.1429"
    # Each day: spend 1 over 2019 or 2018 clicks.
    assert [row["current"]["cpc"] for row in delivery["series"]["exact_rows"]] == ["0.0005"] * 7
    assert all(item["condition"] == "available" for item in delivery["evidence"])
    assert "cpc" not in {request.plan.logical.metric.name for request in queries.seen}


def test_sponsored_campaign_detail_serves_the_same_cpc_the_planned_quotient_did(
    presentation_module: _PresentationModule,
) -> None:
    """Sponsored Products' single writer: the derived CPC equals the value served before.

    Dev served SP CPC 1.3028 from the planned quotient; 22959.9117 / 17624 = 1.3028.
    """
    delivery, _ = _writer_delivery(
        presentation_module,
        SPONSORED_PRODUCTS,
        _WriterQueries(spend=Decimal("22959.9117"), clicks=(2516, *(2518,) * 6)),
    )

    assert delivery["outcome"] == "qualified"
    assert _slot_value(delivery["summary"], "cpc") == "1.3028"
    assert _slot_value(delivery["comparison"]["slots"], "cpc") == "0.1429"
    assert [row["current"]["cpc"] for row in delivery["series"]["exact_rows"]] == ["0.0004"] * 7


def test_campaign_metric_focus_serves_a_sponsored_campaign(
    presentation_module: _PresentationModule,
) -> None:
    """The detail focus redeems the same delivery, so it shared the detail's 500."""
    runtime, capability, _ = _real_selector_runtime(presentation_module, SPONSORED_PRODUCTS)

    response = _served(
        presentation_module,
        runtime,
        path="/api/presentation/campaign-metric-focus",
        body={"selection_reference": capability.reference.value, "metric": "spend"},
    )

    assert response["statusCode"] == 200, response["body"]
    assert cast("dict[str, object]", _body(response)["metric"])["id"] == "spend"


@pytest.mark.parametrize(
    "body",
    [{"slot_id": "funnel"}, {"slot_id": "metric-focus", "metric": "spend"}],
    ids=["detail-slot", "detail-metric-focus"],
)
def test_detail_scope_insight_deep_dive_serves_a_sponsored_campaign(
    presentation_module: _PresentationModule, body: dict[str, object]
) -> None:
    """The detail-scope deep dive redeems the same delivery, so it shared the detail's 500."""
    runtime, capability, assembler = _real_selector_runtime(presentation_module, SPONSORED_PRODUCTS)
    # As in the DSP metric-focus deep test: a material 7D spend move admits the focus card.
    queries = cast("ScenarioQueries", cast("Any", runtime).measurements.queries)
    queries.window_metric_values = {
        ("ad_spend", date(2026, 8, 13)): Decimal("90"),
        ("ad_spend", date(2026, 8, 6)): Decimal("10"),
    }

    response = _served(
        presentation_module,
        runtime,
        path=DEEP_DIVE_PATH,
        body={"selection_reference": capability.reference.value, **body},
    )

    assert response["statusCode"] == 200, response["body"]
    payload = _body(response)
    assert (payload["slot_id"], payload["state"]) == (body["slot_id"], "filled")
    (request,) = assembler.deep
    assert request.key.slot_id.value == body["slot_id"]
    assert payload["artifact_revision"] == request.key.artifact_revision
    assert payload["period"] == {"start": "2026-08-13", "end": "2026-08-19"}


# --- S6 §2.9: grant-bound pointer cases and the generation label (slice 5 part ii) -------------

GRANT_G1 = "1" * 64
GRANT_G2 = "2" * 64
GRANT_G3 = "3" * 64
G1_WRITTEN_AT = "2026-08-20T06:12:04Z"
G3_WRITTEN_AT = "2026-08-22T06:40:11Z"


@dataclass
class RecordingGraphServing:
    """The presentation graph read port over in-memory `SERVING#` items and an entry instant."""

    entered_at: str | None = None
    bindings: dict[str, ServingBinding] = field(default_factory=dict[str, ServingBinding])
    mode_reads: int = 0
    binding_reads: list[str] = field(default_factory=list[str])

    def graph_entered_at(self) -> str | None:
        self.mode_reads += 1
        return self.entered_at

    def serving_binding(self, grant_id: str) -> ServingBinding | None:
        self.binding_reads.append(grant_id)
        return self.bindings.get(grant_id)


def _binding(grant_id: str, artifact_revision: str | None = None) -> ServingBinding:
    return ServingBinding(
        binding_id="b" * 64,
        grant_id=grant_id,
        conflict_unit="u" * 64,
        served_revision=NOW,
        artifact_revision=serving_generation(grant_id)
        if artifact_revision is None
        else artifact_revision,
        contributor_roots=("root-flow",),
        bound_at=NOW,
    )


def _granted(
    pointer: PresetArtifactPointer, grant_id: str, written_at: str
) -> PresetArtifactPointer:
    return replace(
        pointer,
        grant_id=grant_id,
        serving_generation=serving_generation(grant_id),
        written_at=written_at,
    )


def _preset_event(path: str) -> dict[str, Any]:
    event = _event(path, {"session_id": SESSION})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    return event


@dataclass(frozen=True, slots=True)
class _PresetServe:
    artifacts: RecordingPresetArtifacts
    graph: object | None
    path: str = LIST_PATH


def _serve_preset(
    presentation_module: _PresentationModule, serve: _PresetServe
) -> dict[str, object]:
    response = presentation_module.handle(
        _preset_event(serve.path),
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                RecordingPages(),
                ScenarioQueries(),
                preset_artifacts=serve.artifacts,
                graph_serving=serve.graph,
            ),
        ),
    )
    return _body(response)


def _served_page(payload: dict[str, object]) -> dict[str, object]:
    assert payload["kind"] == "page"
    return cast("dict[str, object]", payload["page"])


def _resolved_window(payload: dict[str, object]) -> object:
    return cast("dict[str, Any]", payload["temporal"])["resolved"]["window"]


def test_case_1_a_grant_less_pointer_is_served_as_legacy_history_with_no_serving_read(
    presentation_module: _PresentationModule,
) -> None:
    pointer, body = _campaign_preset_artifact()
    artifacts = RecordingPresetArtifacts(pointer, body)
    graph = RecordingGraphServing()

    page = _served_page(_serve_preset(presentation_module, _PresetServe(artifacts, graph)))

    assert graph.binding_reads == []
    assert artifacts.body_reads == [pointer]
    assert "generation" not in page


@pytest.mark.parametrize("path", [LIST_PATH, RETAIL_HEALTH_LIST_PATH])
def test_case_3_a_grant_bearing_pointer_named_by_its_binding_is_served_with_built_at(
    presentation_module: _PresentationModule, path: str
) -> None:
    preset = _campaign_preset_artifact() if path == LIST_PATH else _retail_preset_artifact()
    pointer = _granted(preset[0], GRANT_G1, G1_WRITTEN_AT)
    artifacts = RecordingPresetArtifacts(pointer, preset[1])
    graph = RecordingGraphServing(bindings={GRANT_G1: _binding(GRANT_G1)})

    page = _served_page(_serve_preset(presentation_module, _PresetServe(artifacts, graph, path)))

    assert graph.binding_reads == [GRANT_G1]
    assert graph.mode_reads == 0
    assert artifacts.body_reads == [pointer]
    assert page["generation"] == {"built_at": G1_WRITTEN_AT}


def test_case_3_refuses_a_generation_its_binding_does_not_name(
    presentation_module: _PresentationModule,
) -> None:
    """Refusal: not available (`ungranted_generation`), before any body read."""
    preset, body = _campaign_preset_artifact()
    pointer = _granted(preset, GRANT_G1, G1_WRITTEN_AT)
    artifacts = RecordingPresetArtifacts(pointer, body)
    graph = RecordingGraphServing(
        bindings={GRANT_G1: _binding(GRANT_G1, artifact_revision=serving_generation(GRANT_G2))}
    )

    with pytest.raises(DomainError) as refused:
        _serve_preset(presentation_module, _PresetServe(artifacts, graph))

    assert refused.value.code == presentation_module.module.PRESENTATION_UNAVAILABLE
    assert refused.value.context == {"reason": "ungranted_generation", "refusal_age_seconds": 0}
    assert artifacts.body_reads == []
    # Control: a binding that names the generation serves it.
    graph.bindings[GRANT_G1] = _binding(GRANT_G1)
    page = _served_page(_serve_preset(presentation_module, _PresetServe(artifacts, graph)))
    assert page["generation"] == {"built_at": G1_WRITTEN_AT}


@pytest.mark.parametrize(
    "entered_at", [None, "2020-01-01T00:00:00Z", "2026-08-19T12:00:00Z", "2099-12-31T23:59:59Z"]
)
def test_d28_a_grant_less_pointer_is_served_whatever_graph_entered_at_is(
    presentation_module: _PresentationModule, entered_at: str | None
) -> None:
    pointer, body = _campaign_preset_artifact()
    graph = RecordingGraphServing(entered_at=entered_at)

    page = _served_page(
        _serve_preset(
            presentation_module, _PresetServe(RecordingPresetArtifacts(pointer, body), graph)
        )
    )

    assert page["artifact_type"] == "CampaignPortfolioArtifact"
    assert graph.binding_reads == []
    # Control: a grant-bearing pointer whose present binding doesn't name it is refused.
    granted = _granted(pointer, GRANT_G1, G1_WRITTEN_AT)
    graph.bindings[GRANT_G1] = _binding(GRANT_G1, artifact_revision=serving_generation(GRANT_G2))
    with pytest.raises(DomainError) as refused:
        _serve_preset(
            presentation_module, _PresetServe(RecordingPresetArtifacts(granted, body), graph)
        )
    assert refused.value.code == presentation_module.module.PRESENTATION_UNAVAILABLE
    assert refused.value.context == {"reason": "ungranted_generation", "refusal_age_seconds": 0}


@pytest.mark.parametrize("path", [LIST_PATH, RETAIL_HEALTH_LIST_PATH])
def test_d7_q1_an_open_cohort_is_refresh_pending_with_no_revision_read(
    presentation_module: _PresentationModule, path: str
) -> None:
    """Case 4, D7 Q1 "refresh pending": not available until bake binds.

    The existing not-available error carries `serving_refresh_pending`. No `REVISION#` read and
    no body read is made.
    """
    preset = _campaign_preset_artifact() if path == LIST_PATH else _retail_preset_artifact()
    pointer = _granted(preset[0], GRANT_G1, G1_WRITTEN_AT)
    artifacts = RecordingPresetArtifacts(pointer, preset[1])
    graph = RecordingGraphServing()

    with pytest.raises(DomainError) as refused:
        _serve_preset(presentation_module, _PresetServe(artifacts, graph, path))

    assert refused.value.code == presentation_module.module.PRESENTATION_UNAVAILABLE
    assert str(refused.value.code) == "ADLY-PRESENTATION-0002"
    assert refused.value.context == {"reason": "serving_refresh_pending", "refusal_age_seconds": 0}
    # Catalog API-0002 (`docs/ui/page-absence-copy.md`), the server-owned refusal text.
    assert refused.value.description == SERVING_CHECK_TEXT
    assert graph.binding_reads == [GRANT_G1]
    # The pointer store saw only the one roster read: no history (`REVISION#`) and no body.
    assert len(artifacts.roster_reads) == 1
    assert artifacts.pointer_reads == []
    assert artifacts.body_reads == []
    # Control: the same pointer with `SERVING#` present is served.
    graph.bindings[GRANT_G1] = _binding(GRANT_G1)
    page = _served_page(_serve_preset(presentation_module, _PresetServe(artifacts, graph, path)))
    assert page["generation"] == {"built_at": G1_WRITTEN_AT}
    assert artifacts.body_reads == [pointer]


def test_focus_read_applies_the_pointer_cases_and_carries_no_generation(
    presentation_module: _PresentationModule,
) -> None:
    preset, body = _complete_campaign_preset_artifact(material=True)
    pointer = _granted(preset, GRANT_G1, G1_WRITTEN_AT)
    artifacts = RecordingPresetArtifacts(pointer, body)
    graph = RecordingGraphServing(
        bindings={GRANT_G1: _binding(GRANT_G1, artifact_revision=serving_generation(GRANT_G2))}
    )

    def focus() -> dict[str, Any]:
        return presentation_module.handle(
            _event(PORTFOLIO_FOCUS_PATH, {"session_id": SESSION, "metric": "spend"}),
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    RecordingPages(),
                    ScenarioQueries(),
                    preset_artifacts=artifacts,
                    graph_serving=graph,
                ),
            ),
        )

    with pytest.raises(DomainError) as refused:
        focus()
    assert refused.value.code == presentation_module.module.PRESENTATION_UNAVAILABLE
    assert refused.value.context == {"reason": "ungranted_generation", "refusal_age_seconds": 0}
    assert artifacts.body_reads == []
    # Control: the binding names it, so the focus serves, with no generation and no mode read.
    graph.bindings[GRANT_G1] = _binding(GRANT_G1)
    payload = _body(focus())
    assert payload["focus_version"] == 4
    assert "generation" not in payload
    assert graph.mode_reads == 0


_DESIGNED_ABSENCES = (
    ("reach#last_complete_month", "reach_materialization_unavailable"),
    ("retention#latest_with_current_publication", "retention_materialization_unavailable"),
    ("precompute#campaigns#7d#insight", "insight_slot_unavailable"),
)
"""Every D2 cohort's designed absences: sources with no bakeable period and an empty slot."""


def _direct(
    grant_id: str,
    *,
    unavailable: tuple[tuple[str, str], ...] = _DESIGNED_ABSENCES,
    bound_at: datetime = NOW,
) -> ServingBinding:
    """Return a typed DIRECT executed binding, as the V3 finalizer commits it (ADR-139)."""
    return ServingBinding(
        binding_id="d" * 64,
        grant_id=grant_id,
        conflict_unit="u" * 64,
        served_revision=NOW,
        artifact_revision=serving_generation(grant_id),
        contributor_roots=("root-flow",),
        bound_at=bound_at,
        unavailable=unavailable,
        domain="domain-fictional",
        cohort_revision="cohort-fictional",
        serving_input_revision="input-fictional",
        plan_digest="plan-fictional",
        binding_kind=BindingKind.DIRECT,
        winner_run_id="run-fictional",
        execution_attempts=1,
    )


@pytest.mark.covers("ADR-139")
@pytest.mark.parametrize("path", [LIST_PATH, RETAIL_HEALTH_LIST_PATH])
def test_a_designed_absence_binding_serves_its_pointer_with_built_at(
    presentation_module: _PresentationModule, path: str
) -> None:
    preset = _campaign_preset_artifact() if path == LIST_PATH else _retail_preset_artifact()
    pointer = _granted(preset[0], GRANT_G1, G1_WRITTEN_AT)
    artifacts = RecordingPresetArtifacts(pointer, preset[1])
    graph = RecordingGraphServing(bindings={GRANT_G1: _direct(GRANT_G1)})

    page = _served_page(_serve_preset(presentation_module, _PresetServe(artifacts, graph, path)))

    assert page["generation"] == {"built_at": G1_WRITTEN_AT}
    assert artifacts.body_reads == [pointer]
    # Control: one failure entry (a precompute budget exhaustion) refuses the same pointer.
    graph.bindings[GRANT_G1] = _direct(
        GRANT_G1,
        unavailable=(*_DESIGNED_ABSENCES, ("precompute#x", "precompute_budget_exhausted")),
    )
    with pytest.raises(DomainError) as refused:
        _serve_preset(presentation_module, _PresetServe(artifacts, graph, path))
    assert refused.value.context["reason"] == "ungranted_generation"


@pytest.mark.covers("ADR-139")
@pytest.mark.parametrize(
    "parent",
    [
        "_v2_scorecard_parent_response",
        "_v2_new_customers_parent_response",
        "_v2_audiences_parent_response",
        "_v2_reach_parent_response",
        "_v2_retention_parent_response",
    ],
)
def test_every_d2_page_serves_a_designed_absence_generation_through_the_shared_seam(
    presentation_module: _PresentationModule, parent: str
) -> None:
    """The five D2 parents decide their pointer case only through `_served_generation`.

    `test_b4_every_d2_parent_resolves_its_generation_before_the_body_and_emits_it` proves each
    parent calls it once, before the body read, and emits its result; here the seam serves a
    designed-absence binding with `built_at` and refuses the paired failure.
    """
    module = presentation_module.module
    assert "_served_generation(runtime, pointer)" in inspect.getsource(vars(module)[parent])
    preset, _ = _campaign_preset_artifact()
    pointer = _granted(preset, GRANT_G1, G1_WRITTEN_AT)
    graph = RecordingGraphServing(bindings={GRANT_G1: _direct(GRANT_G1)})
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(
            RecordingCatalog(), RecordingPages(), ScenarioQueries(), graph_serving=graph
        ),
    )

    generation = module._served_generation(runtime, pointer)

    assert generation.built_at == G1_WRITTEN_AT
    graph.bindings[GRANT_G1] = _direct(GRANT_G1, unavailable=(("campaigns#7d", "unavailable"),))
    with pytest.raises(DomainError):
        module._served_generation(runtime, pointer)


@pytest.mark.covers("ADR-139")
def test_last_good_serves_the_prior_grant_while_a_withheld_cohort_moves_nothing(
    presentation_module: _PresentationModule,
) -> None:
    """Serve G1, the last good generation, while G2's withheld cohort moves nothing.

    G1 is grantable and CURRENT; G2's binding is stored but CURRENT still names G1, so the page
    serves G1's body, `written_at` and window. Control: G3 grantable moves CURRENT to G3.
    """
    preset, body = _campaign_preset_artifact(identity="publication-g1")
    g1 = _granted(preset, GRANT_G1, G1_WRITTEN_AT)
    withheld = _direct(GRANT_G2, unavailable=(("precompute#x", "precompute_budget_exhausted"),))
    graph = RecordingGraphServing(bindings={GRANT_G1: _direct(GRANT_G1), GRANT_G2: withheld})
    artifacts = RecordingPresetArtifacts(g1, body)

    kept = _serve_preset(presentation_module, _PresetServe(artifacts, graph))

    assert artifacts.body_reads == [g1]
    assert _served_page(kept)["generation"] == {"built_at": G1_WRITTEN_AT}
    assert _resolved_window(kept) == {"start": "2026-08-13", "end": "2026-08-19"}
    assert graph.binding_reads == [GRANT_G1]
    g3_preset, g3_body = _campaign_preset_artifact(identity="publication-g3")
    g3 = replace(
        _granted(g3_preset, GRANT_G3, G3_WRITTEN_AT), forward_token=2, supersedes=g1.revision
    )
    graph.bindings[GRANT_G3] = _direct(GRANT_G3)
    moved = _serve_preset(
        presentation_module, _PresetServe(RecordingPresetArtifacts(g3, g3_body), graph)
    )
    assert _served_page(moved)["generation"] == {"built_at": G3_WRITTEN_AT}


@pytest.mark.covers("ADR-139")
@pytest.mark.parametrize(
    ("bound_seconds_ago", "past_settle"), [(0, False), (269, False), (270, True), (3_600, True)]
)
def test_a_refusal_is_counted_with_its_settle_class_and_logs_its_age(  # noqa: PLR0913, PLR0917 -- fixtures and cases
    presentation_module: _PresentationModule,
    monkeypatch: pytest.MonkeyPatch,
    bound_seconds_ago: int,
    past_settle: bool,
) -> None:
    counted: list[tuple[str, bool]] = []

    def count(reason: str, *, past_settle: bool) -> None:
        counted.append((reason, past_settle))

    monkeypatch.setattr(presentation_module.module, "report_serving_refusal", count)
    preset, body = _campaign_preset_artifact()
    pointer = _granted(preset, GRANT_G1, G1_WRITTEN_AT)
    failed = _direct(
        GRANT_G1,
        unavailable=(("precompute#x", "post_grant_invariant"),),
        bound_at=NOW - timedelta(seconds=bound_seconds_ago),
    )
    graph = RecordingGraphServing(bindings={GRANT_G1: failed})

    with pytest.raises(DomainError) as refused:
        _serve_preset(
            presentation_module, _PresetServe(RecordingPresetArtifacts(pointer, body), graph)
        )

    assert counted == [("ungranted_generation", past_settle)]
    assert refused.value.context == {
        "reason": "ungranted_generation",
        "refusal_age_seconds": bound_seconds_ago,
    }
    assert refused.value.description == SERVING_CHECK_TEXT
    # The caller receives exactly the three error keys; reason and age stay in the log.
    payload = presentation_module.module._error_payload(refused.value)
    assert set(payload) == {"error", "code", "error_class"}
    assert payload["error"] == SERVING_CHECK_TEXT
    # Control: a served pointer counts nothing.
    graph.bindings[GRANT_G1] = _direct(GRANT_G1)
    _serve_preset(presentation_module, _PresetServe(RecordingPresetArtifacts(pointer, body), graph))
    assert len(counted) == 1


@pytest.mark.covers("ADR-139")
@pytest.mark.parametrize(
    ("written_at", "age", "past_settle"),
    [
        ((NOW - timedelta(seconds=269)).strftime("%Y-%m-%dT%H:%M:%SZ"), 269, False),
        ((NOW - timedelta(seconds=270)).strftime("%Y-%m-%dT%H:%M:%SZ"), 270, True),
        ((NOW - timedelta(seconds=3_600)).strftime("%Y-%m-%dT%H:%M:%SZ"), 3_600, True),
    ],
    ids=["within-settle", "at-settle", "past-settle"],
)
def test_a_binding_less_refusal_ages_from_the_pointer_written_at(  # noqa: PLR0913, PLR0917 -- fixtures and cases
    presentation_module: _PresentationModule,
    monkeypatch: pytest.MonkeyPatch,
    written_at: str,
    age: int,
    past_settle: bool,
) -> None:
    """Case 4: with no binding the age anchors on the pointer's `written_at`."""
    counted: list[tuple[str, bool]] = []

    def count(reason: str, *, past_settle: bool) -> None:
        counted.append((reason, past_settle))

    monkeypatch.setattr(presentation_module.module, "report_serving_refusal", count)
    preset, body = _campaign_preset_artifact()
    pointer = replace(_granted(preset, GRANT_G1, G1_WRITTEN_AT), written_at=written_at)
    graph = RecordingGraphServing()

    with pytest.raises(DomainError) as refused:
        _serve_preset(
            presentation_module, _PresetServe(RecordingPresetArtifacts(pointer, body), graph)
        )

    assert counted == [("serving_refresh_pending", past_settle)]
    assert refused.value.context == {
        "reason": "serving_refresh_pending",
        "refusal_age_seconds": age,
    }
    assert refused.value.code == presentation_module.module.PRESENTATION_UNAVAILABLE
    assert refused.value.description == SERVING_CHECK_TEXT


@dataclass(frozen=True)
class _UncheckedPointer:
    """Only the field `refusal_age_seconds` reads, without the pointer's own instant check."""

    written_at: str | None


@pytest.mark.covers("ADR-139")
@pytest.mark.parametrize(
    "written_at",
    ["not-an-instant", "2026-08-19T11:00:00", None],
    ids=["malformed", "naive", "none"],
)
def test_an_unreadable_written_at_ages_a_refusal_as_zero_not_an_error(
    written_at: str | None,
) -> None:
    """An unreadable anchor ages as 0, so the refusal stays a 409 (within settle), never a 500.

    `PresetArtifactPointer` already refuses such a `written_at`; the age is total regardless.
    """
    pointer = cast("PresetArtifactPointer", _UncheckedPointer(written_at))
    assert refusal_age_seconds(pointer, None, NOW) == 0
    # Control: a readable anchor ages.
    readable = cast("PresetArtifactPointer", _UncheckedPointer("2026-08-19T11:00:00Z"))
    assert refusal_age_seconds(readable, None, NOW) == 3_600


@pytest.mark.covers("ADR-139")
def test_the_span_records_only_the_closed_serving_refusal_reason(
    presentation_module: _PresentationModule,
) -> None:
    reason_of = presentation_module.module._serving_refusal_reason
    code = presentation_module.module.PRESENTATION_UNAVAILABLE
    for reason in ("ungranted_generation", "serving_refresh_pending"):
        assert reason_of(DomainError(code, "x", context={"reason": reason})) == reason
    assert reason_of(DomainError(code, "No reporting period is available for this page.")) is None
    assert reason_of(DomainError(code, "x", context={"reason": "other"})) is None


def test_a_kept_g1_pointer_after_a_same_window_g2_exhaustion_shows_g1_window_and_build_time(
    presentation_module: _PresentationModule,
) -> None:
    """D27(a), M1: G2 republished the same window, but its bake exhausted before this page.

    The roster still holds G1's pointer, so the page serves G1's period and G1's `written_at`.
    """
    preset, body = _campaign_preset_artifact(identity="publication-g1")
    g1 = _granted(preset, GRANT_G1, G1_WRITTEN_AT)
    graph = RecordingGraphServing(
        bindings={GRANT_G1: _binding(GRANT_G1), GRANT_G2: _binding(GRANT_G2)}
    )

    kept = _serve_preset(
        presentation_module, _PresetServe(RecordingPresetArtifacts(g1, body), graph)
    )

    g1_window = {"start": "2026-08-13", "end": "2026-08-19"}
    assert _resolved_window(kept) == g1_window
    assert _served_page(kept)["generation"] == {"built_at": G1_WRITTEN_AT}
    # Control: G3 writes a new revision of the same window; built_at moves, the window doesn't.
    g3_preset, g3_body = _campaign_preset_artifact(identity="publication-g3")
    g3 = replace(
        _granted(g3_preset, GRANT_G3, G3_WRITTEN_AT), forward_token=2, supersedes=g1.revision
    )
    assert g3.revision != g1.revision
    graph.bindings[GRANT_G3] = _binding(GRANT_G3)
    moved = _serve_preset(
        presentation_module, _PresetServe(RecordingPresetArtifacts(g3, g3_body), graph)
    )
    assert _resolved_window(moved) == g1_window
    assert _served_page(moved)["generation"] == {"built_at": G3_WRITTEN_AT}


@guard_reserved_words
class _ModeItemClient:
    """A DynamoDB client double that answers `GetItem` for the graph mode item only."""

    def __init__(self, item: dict[str, dict[str, str]] | None) -> None:
        self.item = item
        self.keys: list[object] = []

    def get_item(self, **arguments: object) -> dict[str, object]:
        assert arguments["ConsistentRead"] is True
        self.keys.append(arguments["Key"])
        return {} if self.item is None else {"Item": self.item}


@pytest.mark.parametrize("path", [LIST_PATH, RETAIL_HEALTH_LIST_PATH])
def test_graph_grant_less_pointer_is_built_before_the_rounded_up_graph_entry(
    presentation_module: _PresentationModule, path: str
) -> None:
    preset = _campaign_preset_artifact() if path == LIST_PATH else _retail_preset_artifact()
    artifacts = RecordingPresetArtifacts(*preset)
    client = _ModeItemClient(
        {
            "mode": {"S": "GRAPH"},
            "revision": {"N": "4"},
            "graph_entered_at": {"S": "2026-10-09T21:06:30.123456+00:00"},
        }
    )
    graph = DynamoPresentationGraphReader("adlytics-dev-execution-graph", cast("Any", client))

    page = _served_page(_serve_preset(presentation_module, _PresetServe(artifacts, graph, path)))

    assert page["generation"] == {"built_before": "2026-10-09T21:06:31Z"}
    # Control: outside GRAPH the same pointer's response has no generation.
    client.item = {"mode": {"S": "SHADOW"}, "revision": {"N": "2"}}
    outside = DynamoPresentationGraphReader("adlytics-dev-execution-graph", cast("Any", client))
    assert "generation" not in _served_page(
        _serve_preset(presentation_module, _PresetServe(artifacts, outside, path))
    )


# ADR-120: page context for the assistant --------------------------------------------------------

_V2_ACCEPT = {"Accept": "application/vnd.adlytics.presentation-v2+json"}


def _campaign_preset_artifact_with_rows(
    count: int,
) -> tuple[PresetArtifactPointer, PresetArtifactBody, PresetArtifactCustodyManifest]:
    """Build a custody-backed Campaigns body with `count` rows, each a distinct campaign."""
    period = DateRange(date(2026, 8, 13), date(2026, 8, 19))
    signatures = tuple(
        campaign_display_signature(f"Fictional Campaign {i:03d}") for i in range(count)
    )
    rows = tuple(
        portfolio_row(
            signature,
            CampaignSelectionCapability(f"baked:{i:05d}", datetime(2027, 1, 1, tzinfo=UTC)),
            period,
            Decimal(1000 - i),
        )
        for i, signature in enumerate(signatures)
    )
    artifact = complete_portfolio(period, rows)
    revision = PresetArtifactRevision.from_publication_identities(("publication-50",))
    body = PresetArtifactBody(
        PresetArtifactPage("campaigns"),
        PresetArtifactVariant("7d"),
        revision,
        canonical_preset_artifact_json(campaign_portfolio_artifact_payload(artifact)),
    )
    custody = _indexed_custody(
        body,
        campaign_custody_manifest_payload(
            CampaignCustodyManifest(
                body.digest,
                tuple(
                    CampaignCustodyRow(
                        CampaignKey(f"campaign:fictional-{i:03d}", SCOPE, PLATFORM),
                        CHANNEL,
                        MARKETPLACE,
                        signature,
                    )
                    for i, signature in enumerate(signatures)
                ),
            )
        ),
    )
    pointer = PresetArtifactPointer(
        body.page, body.variant, revision, body.digest, 1, None, custody.digest
    )
    return pointer, body, custody


def _serve_campaigns(
    presentation_module: _PresentationModule, pages: RecordingPages, count: int
) -> dict[str, object]:
    pointer, body, custody = _campaign_preset_artifact_with_rows(count)
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = dict(_V2_ACCEPT)
    response = presentation_module.handle(
        event,
        _analytical_runtime(
            presentation_module,
            _AnalyticalRuntimeRequest(
                RecordingCatalog(),
                pages,
                ScenarioQueries(),
                preset_artifacts=RecordingPresetArtifacts(pointer, body),
                preset_custody=RecordingCustodyStore(custody),
            ),
        ),
    )
    return _body(response)


@pytest.mark.covers("ADR-120")
def test_a_full_50_row_campaigns_segment_writes_its_page_record_before_its_51_item_transaction(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages(tokens=tuple(f"{i:043d}" for i in range(60)))

    payload = _serve_campaigns(presentation_module, pages, 50)

    assert pages.writes == ["page", "parent-capabilities"]
    (request,) = pages.parent_capability_requests
    assert len(request.capabilities) == 50
    # The transaction holds the parent and its 50 rows: 51 items, the cap, unchanged.
    assert 1 + len(request.capabilities) == 51
    (record,) = pages.records
    assert record.reference == request.parent.reference
    assert record.expires_at == request.parent.expires_at
    labels = record.snapshot.labels
    assert labels is not None
    assert (labels.page_id, labels.preset_id) == ("campaigns", "7d")
    # The served body is a one-channel sibling, so the label names its option (ADR-127).
    assert labels.page_context_label == "Campaigns · Sponsored Products · Aug 13\u201319, 2026"
    assert record.snapshot.comparison_window == DateRange(date(2026, 8, 6), date(2026, 8, 12))
    page = cast("dict[str, object]", payload["page"])
    assert page["artifact_revision"] == record.reference.value
    assert page["page_context_label"] == "Campaigns · Sponsored Products · Aug 13\u201319, 2026"
    assert page["page_context_expires_at"] == record.expires_at.isoformat()
    assert page["starter_questions"] == [
        "Which campaign changed spend most between Aug 6\u201312, 2026 and Aug 13\u201319, 2026?",
        "How is spend split across campaigns in Aug 13\u201319, 2026?",
    ]


@dataclass
class _UnwritablePages(RecordingPages):
    def put(self, record: PageContextRecord) -> None:
        del record
        self.writes.append("page-refused")
        raise DependencyError(ErrorCode("PAGE_CONTEXT", 1), "Page context could not be recorded.")


@pytest.mark.covers("ADR-120")
def test_a_refused_page_put_fails_the_serve_before_any_transaction(
    presentation_module: _PresentationModule,
) -> None:
    pages = _UnwritablePages(tokens=tuple(f"{i:043d}" for i in range(10)))

    with pytest.raises(DependencyError, match="could not be recorded"):
        _serve_campaigns(presentation_module, pages, 3)

    assert pages.writes == ["page-refused"]
    assert pages.parent_capability_requests == []


@pytest.mark.covers("ADR-120")
def test_an_empty_campaigns_body_names_its_page_but_offers_no_starter_questions(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    pointer, body = _campaign_preset_artifact()
    event = _event(LIST_PATH, {"session_id": SESSION})
    event["headers"] = dict(_V2_ACCEPT)

    payload = _body(
        presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    pages,
                    ScenarioQueries(),
                    preset_artifacts=RecordingPresetArtifacts(pointer, body),
                ),
            ),
        )
    )

    page = cast("dict[str, object]", payload["page"])
    assert page["page_context_label"] == "Campaigns · Sponsored Products · Aug 13\u201319, 2026"
    assert page["starter_questions"] == []
    assert pages.writes == ["page", "parent-capabilities"]


@pytest.mark.covers("ADR-120")
def test_retail_health_names_its_page_and_writes_its_record_first(
    presentation_module: _PresentationModule,
) -> None:
    pages = RecordingPages()
    pointer, body = _retail_preset_artifact()

    payload = _body(
        presentation_module.handle(
            _retail_health_event({"session_id": SESSION}),
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    pages,
                    ScenarioQueries(),
                    preset_artifacts=RecordingPresetArtifacts(pointer, body),
                    windows=RecordingCoverage((DateRange(date(2026, 8, 1), date(2026, 8, 21)),)),
                ),
            ),
        )
    )

    assert pages.writes == ["page", "parent-capabilities"]
    (record,) = pages.records
    assert record.snapshot.labels is not None
    assert record.snapshot.labels.page_id == "retail-health"
    page = cast("dict[str, object]", payload["page"])
    assert page["page_context_label"] == "Retail health · Aug 13\u201319, 2026"
    assert page["starter_questions"] == [
        "Which products added most shipped revenue from Aug 6\u201312, 2026 "
        "to Aug 13\u201319, 2026?",
        "How did glance views change between Aug 6\u201312, 2026 and Aug 13\u201319, 2026?",
    ]


@pytest.mark.covers("ADR-120")
def test_campaign_detail_names_its_campaign_and_channel_and_offers_detail_starters(
    presentation_module: _PresentationModule,
) -> None:
    pages, queries, capability = _v2_redeemable_campaign_context(presentation_module)
    pages.tokens = (THIRD_SELECTION_TOKEN, "W" * 43, "X" * 43)
    catalog = RecordingCatalog(
        lookup_result=EntityCatalogLookupResult((_alias(CAMPAIGN, "Fictional Campaign 001"),)),
    )
    runtime = _analytical_runtime(
        presentation_module,
        _AnalyticalRuntimeRequest(catalog, pages, queries, publications=StubPublications()),
    )
    event = _event(
        DETAIL_PATH, {"session_id": SESSION, "selection_reference": capability.reference.value}
    )
    event["headers"] = dict(_V2_ACCEPT)

    page = cast("dict[str, object]", _body(presentation_module.handle(event, runtime))["page"])

    detail_record = pages.records[-1]
    labels = detail_record.snapshot.labels
    assert labels is not None
    display = labels.display_labels
    assert display.campaign == "Fictional Campaign 001"
    assert display.channel == capability.safe_signature.channel
    assert page["artifact_revision"] == detail_record.reference.value
    assert page["page_context_label"] == labels.page_context_label
    assert labels.page_context_label == (
        f"Campaigns · {display.channel} · Fictional Campaign 001 · Aug 13\u201319, 2026"
    )
    assert page["page_context_expires_at"] == detail_record.expires_at.isoformat()
    starters = cast("list[str]", page["starter_questions"])
    assert all("Aug 13\u201319, 2026" in question for question in starters)
    assert any("Fictional Campaign 001" in question for question in starters)
