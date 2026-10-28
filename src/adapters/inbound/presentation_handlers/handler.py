# SPDX-License-Identifier: MIT-0
"""Authenticated campaign list and detail presentation operations."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import partial
from time import monotonic
from typing import Any, Final, cast
from urllib.parse import urlencode

from aws_lambda_powertools import Logger

from adapters.campaign_insight_requests import (
    DetailInsightRequestDerivationInput,
    FocusInsightDerivationInput,
    artifact_revision,
    detail_insight_request_derivation,
    focus_insight_request_derivation,
    portfolio_insight_request_derivation,
    portfolio_model_slot_ids,
)
from adapters.campaign_measurement import (
    DSP_QUALITY_METRICS,
    DSP_QUALITY_NOT_MEASURED_FOR_CHANNEL,
    CampaignArtifactMeasurementService,
    CampaignDetailMeasurementInput,
    CampaignMeasurementDependencies,
    CampaignMeasurementSelection,
    CampaignPortfolioMeasurementInput,
)
from adapters.config import (
    graph_consumer_settings,
    settings_without_client_secret,
    tracing_settings,
)
from adapters.deep_insight_inputs import (
    DEEP_SIBLING_METRIC,
    SiblingDirectionRequest,
    attribution_timing_entry,
    deep_enrichment,
    detail_deep_inputs,
    portfolio_deep_inputs,
    retail_deep_inputs,
    sibling_direction_entries,
)
from adapters.inbound.auth_handlers import events
from adapters.inbound.correlation import current_correlation_id, returns_correlation
from adapters.inbound.presentation_handlers.insights import (
    InsightAssemblyRequest,
    InsightDeepServe,
    InsightSlotAssembler,
)
from adapters.inbound.tool_handlers import gateway
from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.inbound.tool_handlers.measurement import (
    DEFAULT_MAX_ROWS,
    MeasurementExecutionTooling,
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    PreparedMeasurementBatch,
    PresentationMeasurementAnswerSlot,
    PresentationMeasurementExecutionResult,
    PresentationMeasurementQuerySlot,
    execute_presentation_batch,
    execute_presentation_query_batch,
    handle_batch,
    prepare_batch,
    require_answered,
)
from adapters.insight_finding_units import dictionary_id
from adapters.insight_requests import InsightRequestContext
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.presentation_validation.loader import load_page_schemas, load_v2_page_schemas
from adapters.outbound.athena.query import (
    INTERACTIVE_WAIT,
    AthenaQueryAdapter,
    AthenaQueryTarget,
    ResultTransport,
)
from adapters.outbound.attribution_models import load_attribution_models
from adapters.outbound.bedrock.insights import BedrockInsightGenerator, InsightModelRouteModels
from adapters.outbound.catalog import (
    PageDefaultWindowCompleteness,
    PageDefaultWindowRequest,
    PageWindowCatalog,
    PageWindowRequirement,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.insight_cache import DynamoInsightCache
from adapters.outbound.dynamodb.insight_prompt_registry import DynamoInsightPromptRegistry
from adapters.outbound.dynamodb.page_context import DynamoPageContextStore
from adapters.outbound.dynamodb.presentation_graph import DynamoPresentationGraphReader
from adapters.outbound.dynamodb.preset_artifacts import DynamoPresetArtifactPointerStore
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.entity_catalog import CanonicalEntityCatalog, EntityCatalogTooling
from adapters.outbound.media_scope_catalog import (
    GovernedMediaScopeCatalog,
    MediaScopeCatalogDependencies,
)
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.s3.preset_artifacts import (
    S3PresetArtifactBodyStore,
    S3PresetArtifactCustodyStore,
)
from adapters.outbound.s3.semantic_storage import S3CanonicalAdapter
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.spend_roster import SpendRosterReader, ad_product_labels
from adapters.outbound.sqs.insight_continuation import SqsInsightContinuation
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    METRICS,
    UNEXPECTED_CODE,
    LogSeverity,
    OperationLog,
    Report,
    report_denial,
    report_error,
    report_operation,
    report_serving_refusal,
)
from adapters.outbound.telemetry.tracing import PRESENTATION_REQUEST, span, trace_boundary
from adapters.retail_insight_requests import (
    RetailInsightRequestDerivationInput,
    retail_insight_request_derivation,
)
from domain.catalog.capability import Catalog
from domain.catalog.freshness import Coverage
from domain.data_contracts.contract import DataContract
from domain.identity.session import IdentityError
from domain.measurement.planning import MEASURE_ALIAS, measure_alias
from domain.narrative.shaped_response import ShapedResponse, UnitKind, Units
from domain.operations.execution_graph_release import UngrantedGenerationError
from domain.presentation.absence_copy import (
    SERVING_CHECK_TEXT,
    AbsenceTrigger,
    absence_message,
)
from domain.presentation.analytical_pages import (
    AnalyticalPageResult,
    AvailabilityState,
    PageMeasurementResult,
    PageMeasurementResultRequest,
    PresentationDimensionLabel,
    campaign_platform_labels,
    presentation_dimension_labels,
    project_presentation_answer,
)
from domain.presentation.artifacts import (
    CampaignSelectionCapabilityReadRequest,
    CampaignSelectionCapabilityRecord,
    ChannelRowCapabilityReadRequest,
    ChannelRowCapabilityRecord,
    MediaScopeCapabilityReadRequest,
    MediaScopeCapabilityRecord,
    MediaWindowCapabilityReadRequest,
    MediaWindowCapabilityRecord,
    PageArtifact,
    PageArtifactReadRequest,
    PageArtifactRecord,
    ParentCapabilityIssuanceOutcome,
    ParentCapabilityIssuanceRequest,
    ParentCapabilityIssuanceResult,
    ProductSelectionCapabilityReadRequest,
    ProductSelectionCapabilityRecord,
    ServerValidatedCampaignSelectionCapability,
    ServerValidatedChannelRowCapability,
    ServerValidatedPageArtifact,
    ServerValidatedProductSelectionCapability,
    canonical_artifact_json,
)
from domain.presentation.audiences import (
    AUDIENCES_ARTIFACT_TYPE,
    AUDIENCES_ARTIFACT_VERSION,
    AUDIENCES_PAGE_ID,
    AudiencesPart,
    audiences_artifact_superseded,
    audiences_part_identity,
    require_audiences_artifact_payload,
    require_audiences_part_payload,
    resolve_audiences_payload,
)
from domain.presentation.calendar_presets import ResolveCalendarPresetResult
from domain.presentation.campaign_attribution import (
    CAMPAIGN_ATTRIBUTION_ARTIFACT_TYPE,
    CAMPAIGN_ATTRIBUTION_ARTIFACT_VERSION,
    CAMPAIGN_ATTRIBUTION_DETAIL_ARTIFACT_TYPE,
    CAMPAIGN_ATTRIBUTION_DETAIL_ARTIFACT_VERSION,
    PURCHASE_SERIES_GRAIN,
    SUMMARY_GRAIN,
    CampaignAttributionDetailArtifact,
    CampaignAttributionDetailRequest,
    CampaignAttributionDetailV2,
    CampaignDelivery,
    CampaignDeliveryAnswer,
    CampaignDeliverySlot,
    CampaignDeliveryValue,
    CampaignDetailOutcome,
    CampaignDspQuality,
    CampaignEvidenceCondition,
    CampaignEvidenceSlot,
    CampaignEvidenceStatusCondition,
    CampaignEvidenceStatusRequest,
    CampaignListEvidence,
    CampaignListOutcome,
    CampaignListReason,
    CampaignMetricValue,
    CampaignNumber,
    CampaignPurchasePoint,
    CampaignPurchaseSeriesBuildRequest,
    CampaignRecoveryAction,
    CampaignSelectionCapability,
    CampaignSelectionChoice,
    CampaignSelectionListArtifact,
    CampaignSelectionListRequest,
    CampaignSlotEvidence,
    CampaignSummaryBuildRequest,
    SafeCampaignDisplaySignature,
    build_campaign_attribution_detail,
    build_campaign_prior_comparison,
    build_campaign_purchase_series,
    build_campaign_selection_list,
    build_campaign_summary,
    campaign_attribution_detail_v2_payload,
    campaign_attribution_statuses,
    campaign_delivery_comparison,
    campaign_detail_evidence_status,
    campaign_evidence_status,
    campaign_page_artifact_from_payload,
    campaign_page_artifact_payload,
    derive_campaign_cpc,
    safe_campaign_signature,
    safe_dimension_label,
    uncovered_days,
)
from domain.presentation.campaign_custody import (
    CampaignCustodyManifest,
    CampaignCustodyRow,
    campaign_custody_manifest_from_payload,
    custody_rows_align,
)
from domain.presentation.campaign_portfolio import (
    ALL_CHANNELS_FILTER_ID,
    CAMPAIGN_PORTFOLIO_ARTIFACT_TYPE,
    CAMPAIGN_PORTFOLIO_ARTIFACT_VERSION,
    CAMPAIGN_PORTFOLIO_ROW_GRAIN,
    CAMPAIGN_PORTFOLIO_TOTAL_GRAIN,
    MULTI_CHANNEL_FILTER_IDS,
    CampaignPortfolioArtifact,
    CampaignPortfolioComparison,
    CampaignPortfolioInsufficiencyReason,
    CampaignPortfolioMetric,
    CampaignPortfolioMetricValue,
    CampaignPortfolioOutcome,
    CampaignPortfolioReduction,
    CampaignPortfolioReductionRequest,
    CampaignPortfolioRow,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
    CampaignPortfolioTrend,
    build_campaign_portfolio,
    campaign_portfolio_artifact_from_payload,
    campaign_portfolio_artifact_payload,
    campaign_portfolio_artifact_superseded,
    campaign_portfolio_comparison,
    campaign_portfolio_evidence_status,
    campaign_portfolio_segment_from_payload,
    campaign_roster_shard_from_payload,
    campaign_roster_shard_identity,
    reduce_campaign_portfolio,
)
from domain.presentation.campaigns import (
    CampaignListPage,
    CampaignPresentation,
    CampaignPresentationRequest,
    EffectiveNameStatus,
    present_campaign,
)
from domain.presentation.channel_robustness import (
    MODEL_IDS,
    ChannelAttributionRobustness,
    ChannelDailyDay,
    ChannelDailyMetricValue,
    ChannelDailyModelResult,
    ChannelDailyMovement,
    ChannelDailyMovementRequest,
    ChannelDailyReasonCode,
    ChannelRobustnessOutcome,
    ChannelRobustnessRequest,
    channel_attribution_robustness,
    channel_attribution_robustness_v2,
    channel_attribution_robustness_v2_payload,
    channel_daily_movement_payload,
    decode_channel_v2_parent,
    reduce_channel_daily_movement,
)
from domain.presentation.compared_metrics import presentation_change_semantics, presentation_labels
from domain.presentation.data_health import (
    DATA_HEALTH_POPULATION_LABEL,
    DATA_HEALTH_SOURCE_LABEL,
    DataHealthBaseCondition,
    DataHealthCheck,
    DataHealthCheckRequest,
    DataHealthDetailArtifact,
    DataHealthDetailRequest,
    DataHealthEvidenceRow,
    DataHealthFreshnessQualifier,
    DataHealthServingProvenance,
    build_data_health_detail,
    data_health_detail_payload,
    reduce_data_health_condition,
)
from domain.presentation.graph_serving import (
    SERVING_REFRESH_PENDING_REASON,
    SERVING_REFUSAL_SETTLE,
    UNGRANTED_GENERATION_REASON,
    PageGeneration,
    RefreshPendingError,
    granted_generation,
    legacy_generation,
    refusal_age_seconds,
)
from domain.presentation.insights import (
    NO_SERVED_CAPABILITIES,
    DeterministicMoversSlotRequest,
    InsightAbsenceClass,
    InsightContextEntry,
    InsightDeepDiveState,
    InsightEvidenceInput,
    InsightNextStep,
    InsightNextStepKind,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    NextStepServeContext,
    OperandBasis,
    deterministic_movers_slot,
    gate_refused_insight_slots,
    ineligible_insight_slots,
    next_step_payload,
    served_insight_slots,
    unavailable_insight_slots,
)
from domain.presentation.marketplace_comparison import (
    MARKETPLACE_COMPARISON_ARTIFACT_TYPE,
    MARKETPLACE_COMPARISON_ARTIFACT_VERSION,
    MARKETPLACE_GRAIN,
    MarketplaceComparisonReason,
    MarketplaceComparisonRequest,
    MarketplaceEvidenceCondition,
    MarketplaceMetric,
    MarketplaceMetricEvidence,
    MarketplacePeriodEvidence,
    MarketplacePeriodRole,
    MarketplaceSourceTime,
    build_marketplace_comparison,
    marketplace_comparison_payload,
)
from domain.presentation.media_efficiency import (
    MEDIA_EFFICIENCY_ARTIFACT_TYPE,
    MEDIA_EFFICIENCY_SCOPE_LIST_TYPE,
    MEDIA_EFFICIENCY_VERSION,
    MediaEfficiencyArtifact,
    MediaEfficiencyScopeList,
    MediaMetric,
    MediaScopeChoice,
    MediaWindowCapability,
    insufficient_media_efficiency,
    media_delivery_row,
    media_efficiency_payload,
    media_viewability_row,
    reduce_media_efficiency,
)
from domain.presentation.metric_focus import (
    CAMPAIGN_DETAIL_FOCUS_METRICS,
    CAMPAIGN_PORTFOLIO_FOCUS_METRICS,
    RETAIL_FOCUS_METRICS,
    BuildMetricFocusRequest,
    CampaignDetailFocusRequest,
    MetricFocusIdentity,
    MetricFocusIngredients,
    build_metric_focus,
    campaign_detail_focus_ingredients,
    campaign_portfolio_focus_ingredients,
    metric_focus_deep_findings,
    metric_focus_payload,
    portfolio_focus_metrics,
    retail_health_focus_ingredients,
    retail_product_focus_ingredients,
)
from domain.presentation.new_customers import (
    NEW_CUSTOMERS_ARTIFACT_TYPE,
    NEW_CUSTOMERS_ARTIFACT_VERSION,
    NEW_CUSTOMERS_COVERAGE_METRICS,
    NEW_CUSTOMERS_PAGE_ID,
    new_customers_calendar_period,
    resolve_new_customers_payload,
)
from domain.presentation.next_steps import (
    NextStepTargets,
    PageTarget,
    ProductRefTarget,
    campaign_step_key,
)
from domain.presentation.page_schemas import (
    AnalyticalPageSchema,
    MetricRole,
    PageSchemaSet,
    PageTime,
    PageValidationContext,
    PresentationPageNavigation,
    PresentationTemporalPolicy,
    TemporalComparisonPolicy,
    TemporalPresetId,
    calendar_preset_grain,
    is_calendar_preset,
    validate_page_schemas,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactChildIdentity,
    PresetArtifactCustodyManifest,
    PresetArtifactFilterIdentity,
    PresetArtifactIndexShardIdentity,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerRosterRequest,
    PresetArtifactPointerRosterResult,
    PresetArtifactProductAdsIdentity,
    PresetArtifactProductChildIdentity,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingDescriptor,
    PresetArtifactSiblingIdentity,
    PresetArtifactSiblingIndexEntry,
    PresetArtifactSiblingReadRequest,
    PresetArtifactVariant,
    covering_index_shard,
    index_shard_descriptors,
    split_parent_custody,
)
from domain.presentation.reach import (
    REACH_ARTIFACT_TYPE,
    REACH_ARTIFACT_VERSION,
    REACH_PAGE_ID,
    require_reach_artifact_payload,
)
from domain.presentation.retail_health import (
    RETAIL_HEALTH_ARTIFACT_TYPE,
    RETAIL_HEALTH_ARTIFACT_VERSION,
    RetailHealthArtifact,
    RetailHealthOutcome,
    RetailHealthSort,
    RetailHealthSortField,
    retail_health_artifact_from_payload,
    retail_health_artifact_payload,
)
from domain.presentation.retail_health import (
    RetailProductRow as RetailHealthProductRow,
)
from domain.presentation.retail_health_custody import (
    RetailHealthCustodyManifest,
    product_ref_for,
    retail_health_custody_manifest_from_payload,
    retail_product_ads_custody_from_payload,
    retail_product_child_custody_from_payload,
)
from domain.presentation.retail_product_detail import (
    RETAIL_PRODUCT_DETAIL_ARTIFACT_TYPE,
    RETAIL_PRODUCT_DETAIL_ARTIFACT_VERSION,
    RetailProductDetailArtifact,
    RetailProductDetailOutcome,
    SafeProductDisplaySignature,
    retail_product_ads_from_payload,
    retail_product_detail_from_payload,
    retail_product_detail_payload,
)
from domain.presentation.retention import (
    RETENTION_ARTIFACT_TYPE,
    RETENTION_ARTIFACT_VERSION,
    RETENTION_PAGE_ID,
    require_retention_artifact_payload,
)
from domain.presentation.scorecard import (
    NETWORK_SCORECARD_ARTIFACT_TYPE,
    NETWORK_SCORECARD_ARTIFACT_VERSION,
    range_text,
    scorecard_calendar_period,
)
from domain.presentation.starter_questions import (
    StarterQuestionTemplates,
    page_context_label,
    render_starter_questions,
    window_label,
)
from domain.presentation.temporal import (
    PresentationPageResult,
    PresentationWindowUnavailableResult,
    TemporalAvailability,
    TemporalComparisonStatus,
    TemporalControl,
    TemporalNoSelfServiceReason,
    TemporalPreset,
    TemporalRecovery,
    TemporalResolutionRequest,
    TemporalResolutionUnavailable,
    TemporalResolvedSelectionKind,
    TemporalSelection,
    TemporalSelectionKind,
    TemporalUnavailableReason,
    VintageControl,
    VintageResolved,
    resolve_temporal,
    temporal_preset_label_v2,
)
from domain.presentation.temporal_v2 import (
    BakedControlRequest,
    ServedPeriod,
    ServedPresetWindow,
    TemporalSelectionRefusalError,
    baked_preset_control_v2,
    decode_selection_v2,
)
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EntityAlias,
    EntityContext,
    EntityKind,
    EntityReference,
    ResolutionOutcome,
)
from domain.semantics.calendar_periods import latest_complete_period, period_containing
from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    MarketplaceKey,
)
from domain.semantics.page_context import (
    PageContextDisplayLabels,
    PageContextLabels,
    PageContextRecord,
    PageContextReference,
    PageContextSnapshot,
    ReportingWindowStatus,
)
from domain.shared_kernel.errors import (
    AdlyticsError,
    DomainError,
    ErrorClass,
    ErrorCode,
    InvariantError,
    ValidationError,
)
from domain.shared_kernel.provenance import FreshnessStatus, MeasurementBasis, Suppression
from domain.shared_kernel.values import CURRENCY_PATTERN, DateRange, Grain, Money
from ports.outbound.entity_catalog import (
    MAX_CAMPAIGN_PORTFOLIO_CANDIDATES,
    MAX_ENTITY_CANDIDATES,
    AnalyticalEntityCatalogPort,
    BrowsePurpose,
    EntityCatalogBrowseRequest,
    EntityCatalogLookupRequest,
)
from ports.outbound.media_scope_catalog import (
    MAX_MEDIA_SCOPE_CANDIDATES,
    MediaScopeCatalogCoverageRequest,
    MediaScopeCatalogListRequest,
    MediaScopeCatalogPort,
    MediaScopeCatalogRevalidationOutcome,
    MediaScopeCatalogRevalidationRequest,
)
from ports.outbound.page_artifact import (
    ArtifactCapabilityStorePort,
    ParentCapabilityIssuancePort,
    SelectionCapabilityStorePort,
)
from ports.outbound.page_context import PageContextStorePort
from ports.outbound.presentation_graph import PresentationGraphReadPort
from ports.outbound.preset_artifacts import (
    PresetArtifactBodyStorePort,
    PresetArtifactCustodyStorePort,
    PresetArtifactPointerStorePort,
)
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionPlan,
    PresentationEvidenceInspectionPort,
    PresentationEvidenceInspectionRequest,
    PresentationEvidenceInspectionResult,
    PresentationEvidenceRole,
)
from ports.outbound.source_coverage import SourceCoverageRequest, metric_coverage_requirement

__all__ = ["PresentationRuntime", "handle", "handler"]

CAMPAIGN_LIST_PATH = "/api/presentation/campaigns"
CAMPAIGN_DETAIL_PATH = "/api/presentation/campaign-detail"
CAMPAIGN_METRIC_FOCUS_PATH = "/api/presentation/campaign-metric-focus"
PORTFOLIO_METRIC_FOCUS_PATH = "/api/presentation/portfolio-metric-focus"
RETAIL_METRIC_FOCUS_PATH = "/api/presentation/retail-metric-focus"
INSIGHT_DEEP_DIVE_PATH = "/api/presentation/insight-deep-dive"
OVERVIEW_PATH = "/api/presentation/overview"
CHANNEL_PERFORMANCE_PATH = "/api/presentation/amazon-channel-performance"
MARKETPLACE_COMPARISON_PATH = "/api/presentation/marketplace-comparison"
DATA_HEALTH_PATH = "/api/presentation/data-health"
MEDIA_SCOPE_PATH = "/api/presentation/media-efficiency/scopes"
MEDIA_ARTIFACT_PATH = "/api/presentation/media-efficiency/artifact"
RETAIL_HEALTH_PATH = "/api/presentation/retail-health"
# ADR-125 decision 3: named apart from the retired v1 page id, so its cleanup cannot catch it.
RETAIL_HEALTH_PRODUCT_PATH = "/api/presentation/retail-health-product"
SCORECARD_PATH = "/api/presentation/scorecard"
NEW_CUSTOMERS_PATH = "/api/presentation/new-customers"
REACH_PATH = "/api/presentation/reach"
RETENTION_PATH = "/api/presentation/retention"
AUDIENCES_PATH = "/api/presentation/audiences"
MEDIA_TEMPORAL_POLICY = PresentationTemporalPolicy(
    candidate_presets=(
        TemporalPresetId.ONE_DAY,
        TemporalPresetId.SEVEN_DAYS,
        TemporalPresetId.FOURTEEN_DAYS,
        TemporalPresetId.THIRTY_DAYS,
    ),
    default_preset=TemporalPresetId.SEVEN_DAYS,
    custom_enabled=True,
    custom_min_duration_days=1,
    custom_max_duration_days=90,
    comparison_policy=TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR,
    display_grains=(PageTime.DAY,),
    comparison_intent="compare_previous",
)
ANALYTICAL_PAGES = {
    OVERVIEW_PATH: "decision-overview",
    CHANNEL_PERFORMANCE_PATH: "amazon-channel-performance",
    CAMPAIGN_DETAIL_PATH: "campaign-attribution-detail",
    MARKETPLACE_COMPARISON_PATH: "marketplace-comparison",
    DATA_HEALTH_PATH: "data-health-detail",
}
PRESENTATION_PATHS = frozenset(
    {
        CAMPAIGN_LIST_PATH,
        CAMPAIGN_METRIC_FOCUS_PATH,
        PORTFOLIO_METRIC_FOCUS_PATH,
        RETAIL_METRIC_FOCUS_PATH,
        INSIGHT_DEEP_DIVE_PATH,
        MEDIA_SCOPE_PATH,
        MEDIA_ARTIFACT_PATH,
        RETAIL_HEALTH_PATH,
        RETAIL_HEALTH_PRODUCT_PATH,
        SCORECARD_PATH,
        NEW_CUSTOMERS_PATH,
        REACH_PATH,
        RETENTION_PATH,
        AUDIENCES_PATH,
        *ANALYTICAL_PAGES,
    }
)
PRESENTATION_V2_MEDIA_TYPE = "application/vnd.adlytics.presentation-v2+json"
V2_PARENT_PAGE_IDS = {
    CAMPAIGN_LIST_PATH: "campaigns",
    CHANNEL_PERFORMANCE_PATH: "channel-performance",
    MARKETPLACE_COMPARISON_PATH: "workspace",
    DATA_HEALTH_PATH: "data-health",
    MEDIA_SCOPE_PATH: "media-efficiency",
    MEDIA_ARTIFACT_PATH: "media-efficiency",
    RETAIL_HEALTH_PATH: "retail-health",
    SCORECARD_PATH: "scorecard",
    NEW_CUSTOMERS_PATH: NEW_CUSTOMERS_PAGE_ID,
    REACH_PATH: REACH_PAGE_ID,
    RETENTION_PATH: RETENTION_PAGE_ID,
    AUDIENCES_PATH: AUDIENCES_PAGE_ID,
}
PAGE_TTL = timedelta(hours=1)
MAX_SESSION_ID_LENGTH = 200
PRESENTATION_REFUSED = ErrorCode("PRESENTATION", 1)
PRESENTATION_UNAVAILABLE = ErrorCode("PRESENTATION", 2)
PRESENTATION_REFRESH_REQUIRED = ErrorCode("PRESENTATION", 4)
PRESENTATION_FILTER_NOT_OFFERED = ErrorCode("PRESENTATION", 5)
"""A well-formed channel filter the preset doesn't offer (ADR-127): the browser shows All
channels for it, which no other refusal of the request may trigger."""
ARTIFACT_MALFORMED = ErrorCode("PAGE_CONTEXT", 4)
CHANNEL_ARTIFACT_TYPE = "ChannelAttributionRobustness"
CHANNEL_ARTIFACT_VERSION = 2
DATA_HEALTH_ARTIFACT_TYPE = "DataHealthDetail"
DATA_HEALTH_ARTIFACT_VERSION = 1
DATA_HEALTH_GRAIN = ("date", "channel", "campaign", "ad_product", "marketplace")
DATA_HEALTH_PLAN_GRAIN = tuple(f"{name}_key" for name in DATA_HEALTH_GRAIN)
DATA_HEALTH_EVIDENCE_MALFORMED = ErrorCode("PRESENTATION", 3)
MARKETPLACE_EVIDENCE_MALFORMED = ErrorCode("PRESENTATION", 3)
SAFE_FAILURE_DESCRIPTION = "The request could not be completed. The issue has been recorded."
LOGGER = Logger(service="adlytics-presentation")
_REQUEST_CORRELATION_ID: ContextVar[str] = ContextVar(
    "presentation_request_correlation_id", default=""
)
# The Lambda context's remaining-time reader, threaded per request so a deep serve
# can defer its synchronous generation to the durable continuation when the
# request envelope cannot absorb a full generation timeout. None outside Lambda.
_REQUEST_REMAINING_MS: ContextVar[Callable[[], int] | None] = ContextVar(
    "presentation_request_remaining_ms", default=None
)
INVALID_OPERATION = "presentation.invalid"
TELEMETRY_OPERATIONS = {
    CAMPAIGN_LIST_PATH: "presentation.campaigns",
    CAMPAIGN_DETAIL_PATH: "presentation.campaign_detail",
    CAMPAIGN_METRIC_FOCUS_PATH: "presentation.campaign_metric_focus",
    PORTFOLIO_METRIC_FOCUS_PATH: "presentation.portfolio_metric_focus",
    RETAIL_METRIC_FOCUS_PATH: "presentation.retail_metric_focus",
    INSIGHT_DEEP_DIVE_PATH: "presentation.insight_deep_dive",
    OVERVIEW_PATH: "presentation.overview",
    CHANNEL_PERFORMANCE_PATH: "presentation.channel_performance",
    MARKETPLACE_COMPARISON_PATH: "presentation.marketplace_comparison",
    DATA_HEALTH_PATH: "presentation.data_health",
    MEDIA_SCOPE_PATH: "presentation.media_efficiency.scopes",
    MEDIA_ARTIFACT_PATH: "presentation.media_efficiency.artifact",
    RETAIL_HEALTH_PATH: "presentation.retail_health",
    RETAIL_HEALTH_PRODUCT_PATH: "presentation.retail_health_product",
    SCORECARD_PATH: "presentation.scorecard",
    NEW_CUSTOMERS_PATH: "presentation.new_customers",
    REACH_PATH: "presentation.reach",
    RETENTION_PATH: "presentation.retention",
    AUDIENCES_PATH: "presentation.audiences",
}


@dataclass(frozen=True, slots=True)
class _DefaultPresentationInput:
    session_id: str


@dataclass(frozen=True, slots=True)
class _V2ParentPresentationInput:
    session_id: str
    temporal_selection: TemporalSelection
    campaign_sort: CampaignPortfolioSort | None = None
    retail_sort: RetailHealthSort | None = None
    campaign_segment: int = 0
    filter_id: str = ALL_CHANNELS_FILTER_ID
    """Campaigns' channel filter (ADR-127 decision 6); omitted means All channels."""
    campaign_roster_shard: int | None = None
    """One shard of the filter's baked name roster (ADR-145), instead of a page."""
    retail_product_ads: bool = False
    """Products and ads asks for Retail health's baked part beside the page (render format 12)."""


@dataclass(frozen=True, slots=True)
class _V2ChannelChildPresentationInput:
    session_id: str
    selection_reference: PageContextReference


@dataclass(frozen=True, slots=True)
class _ResolvedV2ParentPresentationInput:
    request: _ListPresentationInput | _AnalyticalPresentationInput
    temporal: TemporalControl


@dataclass(frozen=True, slots=True)
class _MediaFixedPlanRequest:
    """Server-held Media scope and temporal authority for its fixed plan cohort."""

    runtime: PresentationRuntime
    scope: object
    window: DateRange
    compare_previous: bool


@dataclass(frozen=True, slots=True)
class _PresentationExecutionRequest:
    event: dict[str, Any]
    operation: str
    owner: str
    request: _ListPresentationInput | _AnalyticalPresentationInput
    runtime: PresentationRuntime
    temporal_control: TemporalControl | None = None
    validated_channel_selection: _ValidatedChannelSelection | None = None


@dataclass(frozen=True, slots=True)
class _V2ParentResponseRequest:
    event: dict[str, Any]
    operation: str
    owner: str
    runtime: PresentationRuntime


@dataclass(frozen=True, slots=True)
class _ListPresentationInput:
    session_id: str
    visible_window: DateRange
    reporting_window_status: ReportingWindowStatus
    comparison_window: DateRange | None
    campaign_sort: CampaignPortfolioSort | None = None


@dataclass(frozen=True, slots=True)
class _AnalyticalPresentationInput:
    session_id: str
    visible_window: DateRange | None = None
    reporting_window_status: ReportingWindowStatus | None = None
    comparison_window: DateRange | None = None
    selection_reference: PageContextReference | None = None
    compare_previous: bool = False


@dataclass(frozen=True, slots=True)
class _PageMintRequest:
    owner: str
    session_id: str
    snapshot: PageContextSnapshot
    maximum_expiry: datetime | None = None


@dataclass(frozen=True, slots=True)
class _AnalyticalExecution:
    owner: str
    operation: str
    request: _AnalyticalPresentationInput
    runtime: PresentationRuntime
    validated_channel_selection: _ValidatedChannelSelection | None = None


@dataclass(frozen=True, slots=True)
class _AnalyticalPageExecutionResult:
    schema: AnalyticalPageSchema
    page: AnalyticalPageResult | None
    snapshot: PageContextSnapshot
    artifact: PageArtifact | None = None
    channel_current: ChannelAttributionRobustness | None = None
    parent_reference: PageContextReference | None = None
    maximum_expiry: datetime | None = None


@dataclass(frozen=True, slots=True)
class _PendingCampaignCapability:
    reference: PageContextReference
    campaign: CampaignKey
    channel: ChannelKey
    marketplace: MarketplaceKey
    safe_signature: SafeCampaignDisplaySignature


@dataclass(frozen=True, slots=True)
class _CampaignPresetCustodyReadRequest:
    runtime: PresentationRuntime
    pointer: PresetArtifactPointer
    body: PresetArtifactBody
    artifact: CampaignPortfolioArtifact
    sort: CampaignPortfolioSort
    segment_index: int
    filter_parent: _CampaignFilterParent | None = None
    """A channel filter sibling already read through the pointer's custody (ADR-127)."""


@dataclass(frozen=True, slots=True)
class _CampaignPresetResolvedSegment:
    artifact: CampaignPortfolioArtifact
    custody_rows: tuple[CampaignCustodyRow, ...] | None


@dataclass(frozen=True, slots=True)
class _CampaignPresetServeRequest:
    request: _V2ParentPresentationInput
    owner: str
    runtime: PresentationRuntime
    temporal: TemporalControl
    artifact: CampaignPortfolioArtifact
    custody_rows: tuple[CampaignCustodyRow, ...] | None = None

    def __post_init__(self) -> None:
        if self.custody_rows is not None and len(self.custody_rows) != len(self.artifact.rows):
            raise ValueError("campaign preset custody rows must align with the public rows.")


@dataclass(frozen=True, slots=True)
class _RetailHealthPresetServeRequest:
    request: _V2ParentPresentationInput
    owner: str
    runtime: PresentationRuntime
    temporal: TemporalControl
    artifact: RetailHealthArtifact
    pointer: PresetArtifactPointer
    custody: RetailHealthCustodyManifest | None = None
    """The private custody naming each baked row's product; none on a retained body."""


@dataclass(frozen=True, slots=True)
class _ValidatedCampaignCapability:
    capability: ServerValidatedCampaignSelectionCapability
    parent: ServerValidatedPageArtifact
    signature: SafeCampaignDisplaySignature
    period: DateRange
    prior_period: DateRange


@dataclass(frozen=True, slots=True)
class _CampaignMeasurementBatch:
    prepared: PreparedMeasurementBatch
    slots: tuple[CampaignEvidenceSlot, ...]


@dataclass(frozen=True, slots=True)
class _CampaignDeliveryMeasurementBatch:
    """V2-only fixed delivery plans, flattened in inspected execution order."""

    prepared: PreparedMeasurementBatch
    slots: tuple[tuple[CampaignDeliverySlot | None, str, DateRange], ...]


@dataclass(frozen=True, slots=True)
class _CampaignDeliveryAnswerRequest:
    """One immutable delivery answer and the owned selection constraints for it."""

    answer_slot: PresentationMeasurementAnswerSlot
    slot: CampaignDeliverySlot | None
    role: str
    period: DateRange
    capability: ServerValidatedCampaignSelectionCapability


@dataclass(frozen=True, slots=True)
class _CampaignSlotAssemblyRequest:
    slot: CampaignEvidenceSlot
    answer_slot: PresentationMeasurementAnswerSlot
    period: DateRange
    capability: ServerValidatedCampaignSelectionCapability


@dataclass(frozen=True, slots=True)
class _InsightAssemblyInput:
    """Server-owned identity, declared model requests, and per-slot absence classes.

    A declared slot appears in exactly one place: ``requests`` when its predicate
    admitted findings, ``silence_reasons`` when it evaluated quiet, or
    ``missing_evidence_by_slot`` when a required evidence input made it ineligible.
    """

    runtime: PresentationRuntime
    context: InsightRequestContext
    slot_ids: tuple[InsightSlotId, ...]
    requests: tuple[InsightAssemblyRequest, ...]
    silence_reasons: Mapping[InsightSlotId, str]
    missing_evidence_by_slot: Mapping[InsightSlotId, tuple[InsightEvidenceInput, ...]]
    served_steps: NextStepServeContext = NO_SERVED_CAPABILITIES
    """ADR-122 decision 4: what this response offers a cached step's campaign key and filter."""


@dataclass(frozen=True, slots=True)
class _PortfolioInsightInput:
    """Server-built portfolio and presentation runtime for shared insight derivation."""

    artifact: CampaignPortfolioArtifact
    runtime: PresentationRuntime
    campaign_refs: Mapping[str, str] = field(default_factory=dict[str, str])
    """Campaign key to the capability this response minted for that campaign's row."""


@dataclass(frozen=True, slots=True)
class _RetailInsightInput:
    """Baked retail body, its sort-projected served body, and the runtime for insight fill.

    Derivation always reads the baked bytes so the cache revision is one per baked revision,
    stable across every client sort of the same artifact.
    """

    baked: RetailHealthArtifact
    served: RetailHealthArtifact
    runtime: PresentationRuntime


@dataclass(frozen=True, slots=True)
class _DetailInsightInput:
    """Delivered V2 values and custody-derived identity for the detail roster."""

    detail: CampaignAttributionDetailArtifact
    delivery: CampaignDelivery
    assembly: _InsightAssemblyInput


@dataclass(frozen=True, slots=True)
class _V2CampaignDetailProjectionInput:
    """All server-owned inputs required to project a persisted V1 detail into V2."""

    legacy_page: object
    temporal: TemporalControl
    delivery: CampaignDelivery
    runtime: PresentationRuntime
    owner: str
    selected: _ValidatedCampaignCapability
    page_record: PageContextRecord


@dataclass(frozen=True, slots=True)
class _CampaignSlotAssembly:
    evidence: CampaignSlotEvidence
    metric_value: CampaignMetricValue | None = None
    purchase_points: tuple[CampaignPurchasePoint, ...] = ()


@dataclass(frozen=True, slots=True)
class _IssuedChannelCapability:
    record: ChannelRowCapabilityRecord
    safe_channel_label: str


@dataclass(frozen=True, slots=True)
class _ValidatedChannelSelection:
    capability: ServerValidatedChannelRowCapability
    parent: ServerValidatedPageArtifact
    comparison_window: DateRange
    currency: str
    safe_channel_label: str


@dataclass(frozen=True, slots=True)
class _ChannelMeasurementBatch:
    schema: AnalyticalPageSchema
    requests: tuple[MeasurementToolRequest, ...]
    slots: tuple[tuple[str, tuple[MetricRole, ...], str | None], ...]


@dataclass(frozen=True, slots=True)
class _ChannelMeasurementBatchRequest:
    schema: AnalyticalPageSchema
    runtime: PresentationRuntime
    snapshot: PageContextSnapshot
    daily: bool


@dataclass(frozen=True, slots=True)
class _MarketplaceMeasurementBatch:
    prepared: PreparedMeasurementBatch
    slots: tuple[tuple[MarketplaceMetric, MarketplacePeriodRole], ...]


@dataclass(frozen=True, slots=True)
class _MarketplacePeriodEvidenceRequest:
    slot: PresentationMeasurementQuerySlot
    period: DateRange
    marketplace_key: str
    rows: dict[str, Money]


@dataclass(frozen=True, slots=True)
class _DataHealthRawRow:
    source_date: date
    channel_key: str
    campaign_key: str
    ad_product_key: str
    marketplace_key: str
    value: int | Decimal

    @property
    def identity(self) -> tuple[date, str, str, str, str]:
        return (
            self.source_date,
            self.channel_key,
            self.campaign_key,
            self.ad_product_key,
            self.marketplace_key,
        )


@dataclass(frozen=True, slots=True)
class _DataHealthAssemblyRequest:
    prepared: PreparedMeasurementBatch
    inspection: PresentationEvidenceInspectionResult
    executed: PresentationMeasurementExecutionResult
    labels: tuple[PresentationDimensionLabel, ...]
    period: DateRange


@dataclass(frozen=True, slots=True)
class _DataHealthCheckAssembly:
    check: DataHealthCheck
    answer: ShapedResponse | None
    evidence_rows: tuple[DataHealthEvidenceRow, ...]
    period: DateRange
    reach_slice_compatible: bool = True


@dataclass(frozen=True, slots=True)
class _DataHealthDimensionProjection:
    campaign_aliases: dict[str, str]
    ad_product_aliases: dict[str, str]
    reach_references: dict[tuple[date, str, str, str, str], str]
    labels: tuple[PresentationDimensionLabel, ...]


@dataclass(frozen=True, slots=True)
class PresentationRuntime:
    """Authorized catalog, page store, deployment scope, clock, and entropy."""

    catalog: AnalyticalEntityCatalogPort
    page_context: PageContextStorePort
    scope: AuthorizedAnalyticalScope
    clock: Callable[[], datetime]
    token_minter: Callable[[], str]
    page_artifacts: ArtifactCapabilityStorePort | None = None
    parent_capability_issuance: ParentCapabilityIssuancePort | None = None
    campaign_capabilities: SelectionCapabilityStorePort | None = None
    channel_capabilities: SelectionCapabilityStorePort | None = None
    product_capabilities: SelectionCapabilityStorePort | None = None
    media_scope_catalog: MediaScopeCatalogPort | None = None
    presentation_evidence: PresentationEvidenceInspectionPort | None = None
    schemas: PageSchemaSet | None = None
    metric_catalog: Catalog | None = None
    contracts: tuple[DataContract, ...] = ()
    measurements: MeasurementTooling | None = None
    windows: PageWindowCatalog | None = None
    dimension_labels: tuple[PresentationDimensionLabel, ...] = ()
    campaign_platform_labels: tuple[PresentationDimensionLabel, ...] = ()
    insight_assembler: InsightSlotAssembler | None = None
    campaign_measurements: CampaignArtifactMeasurementService | None = None
    preset_artifact_bodies: PresetArtifactBodyStorePort | None = None
    preset_artifact_pointers: PresetArtifactPointerStorePort | None = None
    preset_artifact_custody: PresetArtifactCustodyStorePort | None = None
    graph_serving: PresentationGraphReadPort | None = None
    """S6 §2.9: the graph table's mode item and `SERVING#`; None where it isn't provisioned."""

    def prepare_record(self, request: _PageMintRequest) -> PageContextRecord:
        """Prepare one page record so dependent artifacts can share its exact expiry."""
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("presentation clock must return a timezone-aware instant.")
        expires_at = now + PAGE_TTL
        if request.maximum_expiry is not None:
            if request.maximum_expiry.tzinfo is None or request.maximum_expiry.utcoffset() is None:
                raise ValueError("presentation maximum expiry must be timezone-aware.")
            expires_at = min(expires_at, request.maximum_expiry)
        if expires_at <= now:
            raise _channel_selection_unavailable()
        reference = PageContextReference(self.token_minter())
        stored_snapshot = replace(
            request.snapshot,
            response_identity=f"{request.snapshot.response_identity}:{reference.value}",
        )
        return PageContextRecord(
            reference=reference,
            snapshot=stored_snapshot,
            verified_owner_id=request.owner,
            conversation_session_id=request.session_id,
            expires_at=expires_at,
        )

    def mint(self, request: _PageMintRequest) -> PageContextRecord:
        """Persist one owner/session-bound page context."""
        record = self.prepare_record(request)
        self.page_context.put(record)
        return record

    def mint_selection(
        self,
        owner: str,
        request: _ListPresentationInput,
        campaign: CampaignPresentation,
    ) -> PageContextRecord:
        """Persist and return one opaque campaign-selection capability."""
        record = self.prepare_record(
            _PageMintRequest(
                owner=owner,
                session_id=request.session_id,
                snapshot=PageContextSnapshot(
                    response_identity=f"campaign-selection:{campaign.campaign.value}",
                    visible_window=request.visible_window,
                    reporting_window_status=request.reporting_window_status,
                    comparison_window=request.comparison_window,
                    campaign=campaign.campaign,
                    channel=campaign.channel,
                    marketplace=campaign.marketplace,
                ),
            )
        )
        self.page_context.put_selection(record)
        return record


def handle(  # noqa: PLR0911, PLR0912
    event: dict[str, Any], runtime: PresentationRuntime
) -> dict[str, Any]:
    """Resolve one presentation operation using only authorizer custody."""
    operation = _operation(event)
    owner = events.authorizer_subject(event)
    if operation == CAMPAIGN_METRIC_FOCUS_PATH:
        return _campaign_metric_focus_response(event, owner, runtime)
    if operation == PORTFOLIO_METRIC_FOCUS_PATH:
        return _portfolio_metric_focus_response(event, owner, runtime)
    if operation == RETAIL_METRIC_FOCUS_PATH:
        if "selection_reference" in _body(event):
            return _retail_product_metric_focus_response(event, owner, runtime)
        return _retail_metric_focus_response(event, owner, runtime)
    if operation == RETAIL_HEALTH_PRODUCT_PATH:
        return _retail_health_product_response(event, owner, runtime)
    if operation == INSIGHT_DEEP_DIVE_PATH:
        return _insight_deep_dive_response(event, owner, runtime)
    if operation == MEDIA_SCOPE_PATH:
        return _media_scope_response(event, owner, runtime)
    if operation == MEDIA_ARTIFACT_PATH:
        return _media_artifact_response(event, owner, runtime)
    if _accepts_presentation_v2(event):
        if _is_v2_campaign_child_request(event, operation):
            return _v2_campaign_child_response(
                _V2ParentResponseRequest(event, operation, owner, runtime)
            )
        if _is_v2_channel_child_request(event, operation):
            return _v2_channel_child_response(
                _V2ParentResponseRequest(event, operation, owner, runtime)
            )
        if _v2_capability_refresh_required(event, operation):
            raise DomainError(
                PRESENTATION_REFRESH_REQUIRED,
                "Refresh the parent page before selecting again.",
            )
        return _v2_parent_response(_V2ParentResponseRequest(event, operation, owner, runtime))
    if operation in {RETAIL_HEALTH_PATH, REACH_PATH}:
        # A V2-only page: there is no legacy measurement shape to fall back to.
        raise ValidationError(
            PRESENTATION_REFUSED,
            "The presentation operation requires the versioned representation.",
        )
    request = _resolved_window_request(operation, _request(event, operation), runtime)
    return _execute_request(
        _PresentationExecutionRequest(event, operation, owner, request, runtime)
    )


def _assembled_insights(request: _InsightAssemblyInput) -> tuple[InsightSlot, ...]:
    """Assemble only shared request-derived declared model slots."""
    # Gate 1 already ran during derivation and classified every declared slot: an
    # evaluated-quiet or ineligible slot renders silent by mandate with its named
    # business reason — silence is the designed answer, never a delivery failure.
    # Only a slot whose emitted request went unanswered is unavailable.
    revision = request.context.artifact_revision

    def _absent(slot_id: InsightSlotId) -> InsightSlot:
        missing = request.missing_evidence_by_slot.get(slot_id)
        if missing is not None:
            return ineligible_insight_slots(
                (slot_id,),
                artifact_revision=revision,
                missing_by_slot={slot_id: missing},
            )[0]
        reason = request.silence_reasons.get(slot_id)
        if reason is not None:
            return gate_refused_insight_slots(
                (slot_id,), artifact_revision=revision, reasons={slot_id: reason}
            )[0]
        return unavailable_insight_slots(
            (slot_id,),
            artifact_revision=revision,
            absence_class=InsightAbsenceClass.GENERATION_PENDING,
        )[0]

    if request.runtime.insight_assembler is None or not request.requests:
        return tuple(_absent(slot_id) for slot_id in request.slot_ids)
    # A cached step holds keys; only this response's capabilities and filters serve it.
    by_slot = {
        slot.id: slot
        for slot in served_insight_slots(
            request.runtime.insight_assembler.many(
                request.requests,
                correlation_id=_REQUEST_CORRELATION_ID.get(),
            ),
            request.served_steps,
        )
    }
    return tuple(by_slot.get(slot_id, _absent(slot_id)) for slot_id in request.slot_ids)


# Artifact slot ids whose dictionary declaration lives under a different canonical name.
def _focus_identity(metric_id: str, ingredients: MetricFocusIngredients) -> MetricFocusIdentity:
    """Serve the focused metric's identity head from the dictionary, never from code."""
    declared = METRIC_CATALOG.metric(dictionary_id(metric_id))
    if declared is None or not declared.customer_definition:
        raise RuntimeError("the focusable metric's dictionary identity is undeclared.")
    return MetricFocusIdentity(
        metric_id,
        declared.label,
        declared.customer_definition,
        ingredients.current.units,
        ingredients.current.basis,
        ingredients.current.grain,
    )


def _focus_neighbor_labels(neighbors: tuple[str, ...]) -> dict[str, str]:
    """Label every focusable neighbor from its dictionary entry for the next-check chip."""
    labels: dict[str, str] = {}
    for metric_id in neighbors:
        declared = METRIC_CATALOG.metric(dictionary_id(metric_id))
        if declared is not None:
            labels[metric_id] = declared.label
    return labels


@dataclass(frozen=True, slots=True)
class _FocusSlotRequest:
    runtime: PresentationRuntime
    ingredients: MetricFocusIngredients
    identity: MetricFocusIdentity
    subject: str
    artifact_revision: str
    slot_id: InsightSlotId
    basis: OperandBasis
    filter_id: str = ALL_CHANNELS_FILTER_ID


def _focus_insight_slot(request: _FocusSlotRequest) -> InsightSlot:
    """Assemble the one prompted interpretation slot beside its displayed findings."""
    derivation = focus_insight_request_derivation(
        FocusInsightDerivationInput(
            ingredients=request.ingredients,
            identity=request.identity,
            subject=request.subject,
            scope=request.runtime.scope,
            artifact_revision=request.artifact_revision,
            slot_id=request.slot_id,
            contracts=INSIGHT_CONTRACTS,
            basis=request.basis,
            filter_id=request.filter_id,
        )
    )
    return _assembled_insights(
        _InsightAssemblyInput(
            runtime=request.runtime,
            context=derivation.context,
            slot_ids=(request.slot_id,),
            requests=derivation.requests,
            silence_reasons=derivation.silence_reasons,
            missing_evidence_by_slot=derivation.missing_evidence_by_slot,
        )
    )[0]


def _metric_focus_body(
    event: dict[str, Any], allowlist: tuple[str, ...], *, filtered: bool = False
) -> tuple[str, str, TemporalSelection]:
    """Decode the closed parent-scope focus grammar: presets only, never a custom window.

    `filtered` admits Campaigns' `filter_id` (ADR-127 decision 6), read by its caller.
    """
    payload = _body(event)
    fields = set(payload)
    allowed = {"session_id", "metric", "temporal_selection", *(("filter_id",) if filtered else ())}
    if fields - allowed or {"session_id", "metric"} - fields:
        raise ValidationError(PRESENTATION_REFUSED, "The presentation request fields are invalid.")
    session_id = _text(payload["session_id"], "session_id")
    if len(session_id) > MAX_SESSION_ID_LENGTH:
        raise ValidationError(PRESENTATION_REFUSED, "session_id is too long.")
    metric = _text(payload["metric"], "metric")
    if metric not in allowlist:
        raise ValidationError(PRESENTATION_REFUSED, "The requested metric focus is unavailable.")
    selection = _temporal_selection(payload.get("temporal_selection"))
    if selection.kind is TemporalSelectionKind.CUSTOM:
        raise ValidationError(
            PRESENTATION_REFUSED, "The presentation operation supports declared presets only."
        )
    return session_id, metric, selection


@dataclass(frozen=True, slots=True)
class _PresetFocusRead:
    """One attested baked body and its re-attested temporal control."""

    payload: dict[str, object]
    revision: str
    parent_input: _V2ParentPresentationInput
    roster: PresetArtifactPointerRosterResult
    pointer: PresetArtifactPointer
    body: PresetArtifactBody


@dataclass(frozen=True, slots=True)
class _PresetFocusReadRequest:
    """One parent-scope focus route's addressed baked page."""

    runtime: PresentationRuntime
    operation: str
    page: str
    session_id: str
    selection: TemporalSelection


def _granted_generation(
    runtime: PresentationRuntime, pointer: PresetArtifactPointer
) -> PageGeneration | None:
    """S6 §2.9 cases 3 and 4: serve a grant-bearing pointer only if its binding names it.

    A grant-less pointer (case 1) is legacy history: it is served with no read, and this
    returns None. A refusal is the existing not-available error, with its reason in the context:
    `ungranted_generation` (case 3), or `serving_refresh_pending` while the pointer's cohort is
    still open (case 4, D7 Q1 "refresh pending"). Every refusal is counted
    `PresentationServingRefused` with its settle class (ADR-139): the finalizer moves `CURRENT`
    only under an authorizing binding, so a refusal means the write-side invariant broke.
    """
    if pointer.grant_id is None:
        return None
    graph = runtime.graph_serving
    if graph is None:
        raise RuntimeError("graph serving runtime is unavailable for a grant-bearing pointer.")
    binding = graph.serving_binding(pointer.grant_id)
    try:
        return granted_generation(pointer, binding)
    except UngrantedGenerationError:
        reason = UNGRANTED_GENERATION_REASON
    except RefreshPendingError as pending:
        reason = pending.reason
    age = refusal_age_seconds(pointer, binding, runtime.clock())
    report_serving_refusal(reason, past_settle=age >= SERVING_REFUSAL_SETTLE.total_seconds())
    raise DomainError(
        PRESENTATION_UNAVAILABLE,
        SERVING_CHECK_TEXT,
        context={"reason": reason, "refusal_age_seconds": age},
    )


def _served_generation(
    runtime: PresentationRuntime, pointer: PresetArtifactPointer
) -> PageGeneration | None:
    """S6 §2.9 (D27(a), M1): decide a parent read's pointer case and return its build-time label.

    Decided before any body read. A grant-bearing pointer is labelled `built_at`; a grant-less
    one is labelled `built_before` the `GRAPH` entry instant in `GRAPH`, and is unlabelled
    otherwise.
    """
    if pointer.grant_id is not None:
        return _granted_generation(runtime, pointer)
    graph = runtime.graph_serving
    return legacy_generation(None if graph is None else graph.graph_entered_at())


def _with_generation(
    page: dict[str, object], generation: PageGeneration | None
) -> dict[str, object]:
    """Add the optional closed `generation` field beside `artifact_revision` (S6 §2.9)."""
    if generation is None:
        return page
    if generation.built_at is not None:
        return {**page, "generation": {"built_at": generation.built_at}}
    return {**page, "generation": {"built_before": generation.built_before}}


def _read_preset_focus_body(request: _PresetFocusReadRequest) -> _PresetFocusRead:
    """Read the exact baked body the page was served from; never a live measurement."""
    runtime, selection = request.runtime, request.selection
    schema = _v2_page_schema(request.operation)
    selected = (
        schema.temporal_policy.default_preset
        if selection.kind is TemporalSelectionKind.OMITTED
        else selection.preset_id
    )
    if selected is None:
        raise RuntimeError("preset selection is unavailable for this focus.")
    pointers = runtime.preset_artifact_pointers
    bodies = runtime.preset_artifact_bodies
    if pointers is None or bodies is None:
        raise RuntimeError("preset artifact runtime is unavailable for this focus.")
    roster = pointers.roster(
        PresetArtifactPointerRosterRequest(
            PresetArtifactPage(request.page),
            tuple(
                PresetArtifactVariant(candidate.value)
                for candidate in schema.temporal_policy.candidate_presets
            ),
        )
    )
    pointer = roster.pointer_for(PresetArtifactVariant(selected.value))
    if pointer is None:
        raise DomainError(
            PRESENTATION_UNAVAILABLE,
            "No baked reporting window is available for this focus yet.",
        )
    # The focus read carries no `generation`: the footnote renders from the parent response.
    _granted_generation(runtime, pointer)
    body = bodies.read(pointer)
    _attest_campaign_preset_body(pointer, body)
    try:
        payload = json.loads(body.payload_json)
    except TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None
    return _PresetFocusRead(
        payload=payload,
        revision=pointer.revision.value,
        parent_input=_V2ParentPresentationInput(
            session_id=request.session_id, temporal_selection=selection
        ),
        roster=roster,
        pointer=pointer,
        body=body,
    )


def _portfolio_metric_focus_response(
    event: dict[str, Any],
    owner: str,  # noqa: ARG001
    runtime: PresentationRuntime,
) -> dict[str, Any]:
    """Serve one portfolio focus derived from the exact baked artifact; ADR-070 applies."""
    session_id, metric, selection = _metric_focus_body(
        event, CAMPAIGN_PORTFOLIO_FOCUS_METRICS, filtered=True
    )
    filter_id = _campaign_filter_id(_body(event).get("filter_id"))
    read = _read_preset_focus_body(
        _PresetFocusReadRequest(
            runtime=runtime,
            operation=CAMPAIGN_LIST_PATH,
            page="campaigns",
            session_id=session_id,
            selection=selection,
        )
    )
    if campaign_portfolio_artifact_superseded(read.payload):
        raise _campaign_body_superseded()
    try:
        artifact = campaign_portfolio_artifact_from_payload(read.payload)
    except TypeError, ValueError:
        raise _malformed_artifact() from None
    temporal = _campaign_preset_temporal(read.parent_input, artifact, read.roster)
    # The focus reads the same sibling as its tile (ADR-127 decision 6).
    artifact = _campaign_filter_parent(
        runtime, read.pointer, read.body, artifact, filter_id
    ).artifact
    if artifact.outcome is not CampaignPortfolioOutcome.COMPLETE:
        raise DomainError(
            PRESENTATION_UNAVAILABLE,
            "This reporting window serves no focusable portfolio values.",
        )
    _require_portfolio_focus(artifact, metric)
    ingredients = campaign_portfolio_focus_ingredients(artifact, metric)
    identity = _focus_identity(metric, ingredients)
    slot = _focus_insight_slot(
        _FocusSlotRequest(
            runtime=runtime,
            ingredients=ingredients,
            identity=identity,
            subject=f"campaign-portfolio:metric-focus:{metric}",
            artifact_revision=read.revision,
            slot_id=InsightSlotId.METRIC_FOCUS,
            basis=OperandBasis.PORTFOLIO_WINDOW,
            filter_id=filter_id,
        )
    )
    _ = temporal  # The re-attestation is the check; the payload carries its own periods.
    return _json(
        200,
        metric_focus_payload(
            build_metric_focus(
                BuildMetricFocusRequest(
                    ingredients,
                    identity,
                    slot,
                    _focus_neighbor_labels(ingredients.neighbors),
                    CHANGE_SEMANTICS,
                )
            )
        ),
    )


def _require_portfolio_focus(artifact: CampaignPortfolioArtifact, metric: str) -> None:
    """Refuse a focus this sibling doesn't declare, or one whose total is typed absent.

    Purchases and ROAS aren't declared under a multi-channel option (ADR-127): their series
    would be attributed totals across channels.
    """
    if metric not in portfolio_focus_metrics(artifact):
        raise ValidationError(
            PRESENTATION_REFUSED, "The requested metric focus is unavailable for this view."
        )
    total = next((item for item in artifact.totals if item.metric.value == metric), None)
    if total is None or total.current.value is None or total.prior.value is None:
        raise DomainError(
            PRESENTATION_UNAVAILABLE, "This metric has no value to focus on in this window."
        )


def _retail_metric_focus_response(
    event: dict[str, Any],
    owner: str,  # noqa: ARG001
    runtime: PresentationRuntime,
) -> dict[str, Any]:
    """Serve one retail focus derived from the exact baked artifact; ADR-070 applies."""
    session_id, metric, selection = _metric_focus_body(event, RETAIL_FOCUS_METRICS)
    read = _read_preset_focus_body(
        _PresetFocusReadRequest(
            runtime=runtime,
            operation=RETAIL_HEALTH_PATH,
            page="retail-health",
            session_id=session_id,
            selection=selection,
        )
    )
    try:
        artifact = retail_health_artifact_from_payload(read.payload)
    except TypeError, ValueError:
        raise _malformed_artifact() from None
    temporal = _retail_health_preset_temporal(read.parent_input, artifact, read.roster)
    if artifact.outcome is not RetailHealthOutcome.COMPLETE:
        raise DomainError(
            PRESENTATION_UNAVAILABLE,
            "This reporting window serves no focusable retail values.",
        )
    try:
        ingredients = retail_health_focus_ingredients(artifact, metric)
    except ValueError:
        # A typed-absent tile renders no opener; a request for it is refused, not zeroed.
        raise ValidationError(
            PRESENTATION_REFUSED, "The requested metric focus is unavailable for this view."
        ) from None
    identity = _focus_identity(metric, ingredients)
    slot = _focus_insight_slot(
        _FocusSlotRequest(
            runtime=runtime,
            ingredients=ingredients,
            identity=identity,
            subject=f"retail-health:metric-focus:{metric}",
            artifact_revision=read.revision,
            slot_id=InsightSlotId.RETAIL_METRIC_FOCUS,
            basis=OperandBasis.RETAIL_WINDOW,
        )
    )
    _ = temporal
    return _json(
        200,
        metric_focus_payload(
            build_metric_focus(
                BuildMetricFocusRequest(
                    ingredients,
                    identity,
                    slot,
                    _focus_neighbor_labels(ingredients.neighbors),
                    CHANGE_SEMANTICS,
                )
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class _RedeemedCampaignDetail:
    """One capability-redeemed detail render with its delivered evidence and identity."""

    detail: CampaignAttributionDetailArtifact
    delivery: CampaignDelivery
    revision: str
    campaign_id: str


def _redeemed_campaign_detail(execution: _AnalyticalExecution) -> _RedeemedCampaignDetail:
    """Redeem the parent-bound capability and render the exact detail the page serves."""
    selected = _validated_campaign_capability(execution)
    temporal = selected.parent.temporal_control
    if not isinstance(temporal, TemporalControl) or temporal.resolved.window != selected.period:
        raise DomainError(
            PRESENTATION_REFRESH_REQUIRED,
            "Refresh the parent page before selecting again.",
        )
    rendered = _campaign_detail_page(execution, _campaign_schema(execution.runtime)).artifact
    if rendered is None:
        raise RuntimeError("campaign detail artifact is unavailable.")
    decoded = campaign_page_artifact_from_payload(rendered.payload)
    if not isinstance(decoded, CampaignAttributionDetailArtifact):
        raise _malformed_artifact()
    # F12: the deep section re-derives the detail cards, so it reads the served delivery the
    # cards were fingerprinted over, never the bare delivery read.
    delivery = _served_detail_delivery(decoded, _campaign_delivery(selected, execution))
    return _RedeemedCampaignDetail(
        detail=decoded,
        delivery=delivery,
        revision=artifact_revision(campaign_page_artifact_payload(decoded)),
        campaign_id=selected.capability.campaign.value,
    )


def _campaign_metric_focus_response(
    event: dict[str, Any], owner: str, runtime: PresentationRuntime
) -> dict[str, Any]:
    """Redeem a parent-bound capability and serve one detail focus from delivered values."""
    payload = _body(event)
    if set(payload) != {"session_id", "selection_reference", "metric"}:
        raise ValidationError(PRESENTATION_REFUSED, "The presentation request fields are invalid.")
    session_id = _text(payload["session_id"], "session_id")
    if len(session_id) > MAX_SESSION_ID_LENGTH:
        raise ValidationError(PRESENTATION_REFUSED, "session_id is too long.")
    metric = _text(payload["metric"], "metric")
    if metric not in CAMPAIGN_DETAIL_FOCUS_METRICS:
        raise ValidationError(PRESENTATION_REFUSED, "The requested metric focus is unavailable.")
    selection = _selection_reference(payload["selection_reference"])
    execution = _AnalyticalExecution(
        owner,
        CAMPAIGN_DETAIL_PATH,
        _AnalyticalPresentationInput(session_id=session_id, selection_reference=selection),
        runtime,
    )
    redeemed = _redeemed_campaign_detail(execution)
    try:
        ingredients = campaign_detail_focus_ingredients(
            CampaignDetailFocusRequest(
                detail=redeemed.detail, delivery=redeemed.delivery, metric_id=metric
            )
        )
    except ValueError:
        raise DomainError(
            PRESENTATION_UNAVAILABLE,
            "The focused metric is not served for this selection.",
        ) from None
    identity = _focus_identity(metric, ingredients)
    slot = _focus_insight_slot(
        _FocusSlotRequest(
            runtime=runtime,
            ingredients=ingredients,
            identity=identity,
            subject=f"{redeemed.campaign_id}:metric-focus:{metric}",
            artifact_revision=redeemed.revision,
            slot_id=InsightSlotId.METRIC_FOCUS,
            basis=OperandBasis.CAMPAIGN_WINDOW,
        )
    )
    return _json(
        200,
        metric_focus_payload(
            build_metric_focus(
                BuildMetricFocusRequest(
                    ingredients,
                    identity,
                    slot,
                    _focus_neighbor_labels(ingredients.neighbors),
                    CHANGE_SEMANTICS,
                )
            )
        ),
    )


# --- deep-dive dialog route (ADR-076 slice G; contract §3.1) -----------------------------

_DEEP_FOCUS_SLOTS = frozenset({InsightSlotId.METRIC_FOCUS, InsightSlotId.RETAIL_METRIC_FOCUS})
_DEEP_PORTFOLIO_SLOTS = frozenset(
    {InsightSlotId.CONTRIBUTION, InsightSlotId.DIVERGENCE, InsightSlotId.EXPOSURE}
)
_DEEP_DETAIL_SLOTS = frozenset(
    {
        InsightSlotId.FUNNEL,
        InsightSlotId.EFFICIENCY_DRIFT,
        InsightSlotId.TREND_SHAPE,
        InsightSlotId.NEXT_CHECK,
    }
)
_DEEP_RETAIL_SLOTS = frozenset(
    {
        InsightSlotId.DEMAND_SHAPE,
        InsightSlotId.ORDERED_VS_SHIPPED,
        InsightSlotId.RETURNS_PRESSURE,
    }
)
_DEEP_CARD_NOT_FILLED_REASON = (
    "Deep analysis accompanies a filled insight only; this insight is not filled for this view."
)


@dataclass(frozen=True, slots=True)
class _DeepDiveBody:
    """The closed deep-dive request grammar after validation."""

    session_id: str
    slot_id: InsightSlotId
    metric: str | None
    selection: TemporalSelection
    selection_reference: PageContextReference | None
    sort: CampaignPortfolioSort | None = None
    """The page's served sort (portfolio page slots only, W11)."""
    segment: int = 0
    filter_id: str = ALL_CHANNELS_FILTER_ID
    """The channel filter sibling the card was filled from (ADR-127 decision 6)."""


_DEEP_PORTFOLIO_PAGE_SLOTS = frozenset(
    {InsightSlotId.CONTRIBUTION, InsightSlotId.DIVERGENCE, InsightSlotId.EXPOSURE}
)


@dataclass(frozen=True, slots=True)
class _DeepDiveResolution:
    """One resolved surface: the slot's card assembly plus its deep enrichment."""

    assembly: InsightAssemblyRequest | None
    enrich: Callable[[InsightAssemblyRequest], InsightAssemblyRequest] | None
    revision: str
    window_start: str
    window_end: str
    refusal_reason: str | None = None


def _deep_dive_body(event: dict[str, Any]) -> _DeepDiveBody:  # noqa: PLR0912
    """Decode the closed deep-dive grammar; movers and undeclared shapes are refused."""
    payload = _body(event)
    fields = set(payload)
    allowed = {
        "session_id",
        "slot_id",
        "metric",
        "temporal_selection",
        "selection_reference",
        "sort",
        "segment",
        "filter_id",
    }
    if fields - allowed or {"session_id", "slot_id"} - fields:
        raise ValidationError(PRESENTATION_REFUSED, "The presentation request fields are invalid.")
    session_id = _text(payload["session_id"], "session_id")
    if len(session_id) > MAX_SESSION_ID_LENGTH:
        raise ValidationError(PRESENTATION_REFUSED, "session_id is too long.")
    try:
        slot_id = InsightSlotId(_text(payload["slot_id"], "slot_id"))
    except ValueError:
        raise ValidationError(
            PRESENTATION_REFUSED, "The requested insight is unavailable."
        ) from None
    contract = INSIGHT_CONTRACTS.get(slot_id)
    if contract is None or contract.deep is None:
        raise ValidationError(
            PRESENTATION_REFUSED, "The requested insight declares no deep analysis."
        )
    metric = None
    if slot_id in _DEEP_FOCUS_SLOTS:
        raw_metric = payload.get("metric")
        if raw_metric is None:
            raise ValidationError(PRESENTATION_REFUSED, "The requested focus needs its metric.")
        metric = _text(raw_metric, "metric")
    elif "metric" in payload:
        raise ValidationError(PRESENTATION_REFUSED, "The requested insight takes no metric.")
    selection_reference = None
    detail_family = slot_id in _DEEP_DETAIL_SLOTS
    detail_focus = slot_id is InsightSlotId.METRIC_FOCUS and "selection_reference" in payload
    if detail_family or detail_focus:
        if "selection_reference" not in payload:
            raise ValidationError(
                PRESENTATION_REFUSED, "The requested insight needs its selection reference."
            )
        selection_reference = _selection_reference(payload["selection_reference"])
        if "temporal_selection" in payload:
            raise ValidationError(
                PRESENTATION_REFUSED, "A selection-bound insight takes no reporting period."
            )
    elif "selection_reference" in payload:
        raise ValidationError(
            PRESENTATION_REFUSED, "The requested insight takes no selection reference."
        )
    selection = _temporal_selection(payload.get("temporal_selection"))
    if selection.kind is TemporalSelectionKind.CUSTOM:
        raise ValidationError(
            PRESENTATION_REFUSED, "The presentation operation supports declared presets only."
        )
    allowlist = {
        InsightSlotId.METRIC_FOCUS: (
            CAMPAIGN_DETAIL_FOCUS_METRICS
            if selection_reference is not None
            else CAMPAIGN_PORTFOLIO_FOCUS_METRICS
        ),
        InsightSlotId.RETAIL_METRIC_FOCUS: RETAIL_FOCUS_METRICS,
    }.get(slot_id)
    if metric is not None and (allowlist is None or metric not in allowlist):
        raise ValidationError(PRESENTATION_REFUSED, "The requested metric focus is unavailable.")
    # W11: a portfolio page card is filled per served (sort, segment), so its deep request
    # names both, in the page body's own grammar; every other slot refuses either field.
    paged = {"sort", "segment"} & fields
    if paged and slot_id not in _DEEP_PORTFOLIO_PAGE_SLOTS:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "The presentation request fields are invalid.",
            context={"outcome": "invalid_fields", "n": len(paged)},
        )
    # A channel filter names a Campaigns sibling: a portfolio card or portfolio focus only.
    portfolio_scope = slot_id in _DEEP_PORTFOLIO_PAGE_SLOTS or (
        slot_id is InsightSlotId.METRIC_FOCUS and selection_reference is None
    )
    if "filter_id" in fields and not portfolio_scope:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "The presentation request fields are invalid.",
            context={"outcome": "invalid_fields", "n": 1},
        )
    return _DeepDiveBody(
        session_id,
        slot_id,
        metric,
        selection,
        selection_reference,
        _campaign_portfolio_sort(payload.get("sort")),
        _campaign_portfolio_segment_index(payload.get("segment")),
        _campaign_filter_id(payload.get("filter_id")),
    )


def _sibling_entries(  # noqa: PLR0913, PLR0917 -- one roster read and its extractor
    runtime: PresentationRuntime,
    roster: PresetArtifactPointerRosterResult,
    selected_variant: str,
    extract: Callable[[dict[str, object]], tuple[str, Decimal, Decimal | None] | None],
) -> tuple[InsightContextEntry, ...]:
    """Read sibling bodies from the fetched roster, each attested against its pointer."""
    bodies = runtime.preset_artifact_bodies
    if bodies is None:
        return ()

    def read_payload(pointer: PresetArtifactPointer) -> dict[str, object]:
        body = bodies.read(pointer)
        _attest_campaign_preset_body(pointer, body)
        # PresetArtifactBody admits only a canonical JSON object, so this decode cannot fail.
        return cast("dict[str, object]", json.loads(body.payload_json))

    return sibling_direction_entries(
        SiblingDirectionRequest(roster.pointers, selected_variant, read_payload, extract)
    )


def _portfolio_direction_extract(
    metric: str,
) -> Callable[[dict[str, object]], tuple[str, Decimal, Decimal | None] | None]:
    def extract(payload: dict[str, object]) -> tuple[str, Decimal, Decimal | None] | None:
        if campaign_portfolio_artifact_superseded(payload):
            # A sibling preset baked before render format 14 is unpublished: no direction.
            return None
        try:
            artifact = campaign_portfolio_artifact_from_payload(payload)
        except TypeError, ValueError:
            raise _malformed_artifact() from None
        total = next((item for item in artifact.totals if item.metric.value == metric), None)
        if (
            metric not in portfolio_focus_metrics(artifact)
            or total is None
            or total.current.value is None
            or total.prior.value is None
        ):
            # Typed absent on this sibling, as `_require_portfolio_focus` refuses it on the parent.
            return None
        ingredients = campaign_portfolio_focus_ingredients(artifact, metric)
        identity = _focus_identity(metric, ingredients)
        return identity.label, Decimal(ingredients.current.value), Decimal(ingredients.prior.value)

    return extract


def _retail_direction_extract(
    metric: str,
) -> Callable[[dict[str, object]], tuple[str, Decimal, Decimal | None] | None]:
    def extract(payload: dict[str, object]) -> tuple[str, Decimal, Decimal | None] | None:
        try:
            artifact = retail_health_artifact_from_payload(payload)
        except TypeError, ValueError:
            raise _malformed_artifact() from None
        if all(item.metric != metric for item in artifact.totals):
            # Typed absent on this sibling: the focused retail metric is not carried.
            return None
        ingredients = retail_health_focus_ingredients(artifact, metric)
        identity = _focus_identity(metric, ingredients)
        return identity.label, Decimal(ingredients.current.value), Decimal(ingredients.prior.value)

    return extract


def _deep_slot_assembly(
    derivation_requests: tuple[InsightAssemblyRequest, ...], slot_id: InsightSlotId
) -> InsightAssemblyRequest | None:
    return next((item for item in derivation_requests if item.key.slot_id is slot_id), None)


def _deep_parent_resolution(  # noqa: PLR0915 -- one ordered parent and sibling read
    body: _DeepDiveBody, runtime: PresentationRuntime
) -> _DeepDiveResolution:
    """Resolve a parent-scope deep request from the exact baked artifact the page serves."""
    retail = body.slot_id in _DEEP_RETAIL_SLOTS or body.slot_id is InsightSlotId.RETAIL_METRIC_FOCUS
    operation = RETAIL_HEALTH_PATH if retail else CAMPAIGN_LIST_PATH
    page = "retail-health" if retail else "campaigns"
    read = _read_preset_focus_body(
        _PresetFocusReadRequest(
            runtime=runtime,
            operation=operation,
            page=page,
            session_id=body.session_id,
            selection=body.selection,
        )
    )
    schema = _v2_page_schema(operation)
    selected = (
        schema.temporal_policy.default_preset
        if body.selection.kind is TemporalSelectionKind.OMITTED
        else body.selection.preset_id
    )
    selected_variant = "" if selected is None else selected.value
    filtered: _CampaignFilterParent | None = None
    if not retail and campaign_portfolio_artifact_superseded(read.payload):
        raise _campaign_body_superseded()
    try:
        if retail:
            artifact = retail_health_artifact_from_payload(read.payload)
        else:
            artifact = campaign_portfolio_artifact_from_payload(read.payload)
    except TypeError, ValueError:
        raise _malformed_artifact() from None
    if not retail:
        # The dialog reads the same channel filter sibling as its card (ADR-127 decision 6).
        filtered = _campaign_filter_parent(
            runtime,
            read.pointer,
            read.body,
            cast("CampaignPortfolioArtifact", artifact),
            body.filter_id,
        )
        artifact = filtered.artifact
    # Sibling-preset directions are read from each preset's All-channels body, so a filtered
    # card reads none rather than another scope's direction.
    all_channels = body.filter_id == ALL_CHANNELS_FILTER_ID
    if body.slot_id in _DEEP_FOCUS_SLOTS:
        metric = cast("str", body.metric)
        if not retail:
            _require_portfolio_focus(cast("CampaignPortfolioArtifact", artifact), metric)
        try:
            ingredients = (
                retail_health_focus_ingredients(cast("RetailHealthArtifact", artifact), metric)
                if retail
                else campaign_portfolio_focus_ingredients(
                    cast("CampaignPortfolioArtifact", artifact), metric
                )
            )
        except ValueError:
            raise ValidationError(
                PRESENTATION_REFUSED, "The requested metric focus is unavailable for this view."
            ) from None
        identity = _focus_identity(metric, ingredients)
        subject = (
            f"retail-health:metric-focus:{metric}"
            if retail
            else f"campaign-portfolio:metric-focus:{metric}"
        )
        derivation = focus_insight_request_derivation(
            FocusInsightDerivationInput(
                ingredients=ingredients,
                identity=identity,
                subject=subject,
                scope=runtime.scope,
                artifact_revision=read.revision,
                slot_id=body.slot_id,
                contracts=INSIGHT_CONTRACTS,
                basis=OperandBasis.RETAIL_WINDOW if retail else OperandBasis.PORTFOLIO_WINDOW,
                filter_id=body.filter_id,
            )
        )
        extract = (
            _retail_direction_extract(metric) if retail else _portfolio_direction_extract(metric)
        )
        timing = None if retail else attribution_timing_entry(ingredients.period)
        enrich = deep_enrichment(
            metric_focus_deep_findings(ingredients, identity),
            () if timing is None else (timing,),
            context_supplier=lambda: (
                _sibling_entries(runtime, read.roster, selected_variant, extract)
                if all_channels
                else ()
            ),
        )
        assembly = _deep_slot_assembly(derivation.requests, body.slot_id)
        return _DeepDiveResolution(
            assembly,
            enrich,
            read.revision,
            ingredients.period.start.isoformat(),
            ingredients.period.end.isoformat(),
            refusal_reason=None if assembly is not None else _DEEP_CARD_NOT_FILLED_REASON,
        )
    if retail:
        retail_artifact = cast("RetailHealthArtifact", artifact)
        derivation = retail_insight_request_derivation(
            RetailInsightRequestDerivationInput(
                artifact=retail_artifact,
                scope=runtime.scope,
                contracts=INSIGHT_CONTRACTS,
                catalog=METRIC_CATALOG,
            )
        )
        window = retail_artifact.period
    else:
        portfolio_artifact = cast("CampaignPortfolioArtifact", artifact)
        if portfolio_artifact.outcome is not CampaignPortfolioOutcome.COMPLETE:
            raise DomainError(
                PRESENTATION_UNAVAILABLE,
                "This reporting window serves no focusable portfolio values.",
            )
        # The same served segment the page's card was filled from (W11).
        portfolio_artifact = _campaign_preset_segment(
            _CampaignPresetCustodyReadRequest(
                runtime,
                read.pointer,
                read.body,
                portfolio_artifact,
                body.sort or CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
                body.segment,
                filtered,
            )
        ).artifact
        artifact = portfolio_artifact
        derivation, _ = portfolio_insight_request_derivation(
            artifact=portfolio_artifact,
            scope=runtime.scope,
            contracts=INSIGHT_CONTRACTS,
        )
        window = portfolio_artifact.period
    inputs = (
        retail_deep_inputs(cast("RetailHealthArtifact", artifact), body.slot_id, METRIC_CATALOG)
        if retail
        else portfolio_deep_inputs(
            cast("CampaignPortfolioArtifact", artifact), body.slot_id, METRIC_CATALOG
        )
    )
    sibling_metric = DEEP_SIBLING_METRIC[body.slot_id]
    extract = (
        _retail_direction_extract(sibling_metric)
        if retail
        else _portfolio_direction_extract(sibling_metric)
    )
    enrich = deep_enrichment(
        inputs.findings,
        inputs.context,
        context_supplier=lambda: (
            _sibling_entries(runtime, read.roster, selected_variant, extract)
            if all_channels
            else ()
        ),
    )
    assembly = _deep_slot_assembly(derivation.requests, body.slot_id)
    # The baked cards carry the derivation fingerprint, not the pointer body's
    # revision, and the dialog refuses a deep response whose revision differs from
    # its card's. The picked assembly key carries that same fingerprint (verified
    # live 2026-09-24: re-deriving over the served baked body reproduces the baked
    # slot revision byte for byte on both surfaces).
    revision = read.revision if assembly is None else assembly.key.artifact_revision
    return _DeepDiveResolution(
        assembly,
        enrich,
        revision,
        window.start.isoformat(),
        window.end.isoformat(),
        refusal_reason=None if assembly is not None else _DEEP_CARD_NOT_FILLED_REASON,
    )


def _deep_detail_resolution(
    body: _DeepDiveBody, owner: str, runtime: PresentationRuntime
) -> _DeepDiveResolution:
    """Resolve a capability-bound deep request from the exact detail the page serves."""
    execution = _AnalyticalExecution(
        owner,
        CAMPAIGN_DETAIL_PATH,
        _AnalyticalPresentationInput(
            session_id=body.session_id,
            selection_reference=cast("PageContextReference", body.selection_reference),
        ),
        runtime,
    )
    redeemed = _redeemed_campaign_detail(execution)
    if body.slot_id is InsightSlotId.METRIC_FOCUS:
        metric = cast("str", body.metric)
        try:
            ingredients = campaign_detail_focus_ingredients(
                CampaignDetailFocusRequest(
                    detail=redeemed.detail, delivery=redeemed.delivery, metric_id=metric
                )
            )
        except ValueError:
            raise DomainError(
                PRESENTATION_UNAVAILABLE,
                "The focused metric is not served for this selection.",
            ) from None
        identity = _focus_identity(metric, ingredients)
        derivation = focus_insight_request_derivation(
            FocusInsightDerivationInput(
                ingredients=ingredients,
                identity=identity,
                subject=f"{redeemed.campaign_id}:metric-focus:{metric}",
                scope=runtime.scope,
                artifact_revision=redeemed.revision,
                slot_id=InsightSlotId.METRIC_FOCUS,
                contracts=INSIGHT_CONTRACTS,
                basis=OperandBasis.CAMPAIGN_WINDOW,
            )
        )
        timing = attribution_timing_entry(ingredients.period)
        enrich = deep_enrichment(
            metric_focus_deep_findings(ingredients, identity),
            () if timing is None else (timing,),
        )
        assembly = _deep_slot_assembly(derivation.requests, body.slot_id)
        return _DeepDiveResolution(
            assembly,
            enrich,
            redeemed.revision,
            ingredients.period.start.isoformat(),
            ingredients.period.end.isoformat(),
            refusal_reason=None if assembly is not None else _DEEP_CARD_NOT_FILLED_REASON,
        )
    derivation = detail_insight_request_derivation(
        DetailInsightRequestDerivationInput(
            detail=redeemed.detail,
            delivery=redeemed.delivery,
            subject=redeemed.campaign_id,
            scope=runtime.scope,
            contracts=INSIGHT_CONTRACTS,
        )
    )
    assembly = _deep_slot_assembly(derivation.requests, body.slot_id)
    # The detail derivation fingerprints its own payload; the served provenance must
    # be the derivation's revision, which the picked assembly key carries.
    revision = redeemed.revision if assembly is None else assembly.key.artifact_revision
    inputs = detail_deep_inputs(redeemed.detail, redeemed.delivery, METRIC_CATALOG)
    return _DeepDiveResolution(
        assembly,
        deep_enrichment(inputs.findings, inputs.context),
        revision,
        redeemed.detail.period.start.isoformat(),
        redeemed.detail.period.end.isoformat(),
        refusal_reason=None if assembly is not None else _DEEP_CARD_NOT_FILLED_REASON,
    )


def _deep_dive_payload(
    slot_id: InsightSlotId, serve: InsightDeepServe, resolution: _DeepDiveResolution
) -> dict[str, object]:
    """Render the hand-typed insight-deep-dive.v1 response (plan slice G decision 1)."""
    analysis = serve.analysis
    return {
        "slot_id": slot_id.value,
        "state": serve.state.value,
        "deep": None
        if analysis is None
        else {
            "what_stands_out": analysis.what_stands_out,
            "candidate_readings": list(analysis.candidate_readings),
            "discriminating_evidence": analysis.discriminating_evidence,
            "cited_figures": list(analysis.cited_figures),
        },
        "absence_reason": serve.absence_reason,
        "deep_prompt_version": serve.prompt_version,
        "artifact_revision": resolution.revision,
        "period": {"start": resolution.window_start, "end": resolution.window_end},
    }


def _insight_deep_dive_response(
    event: dict[str, Any], owner: str, runtime: PresentationRuntime
) -> dict[str, Any]:
    """Serve one on-request deep section for a filled insight's dialog (contract §3.1)."""
    body = _deep_dive_body(event)
    resolution = (
        _deep_detail_resolution(body, owner, runtime)
        if body.selection_reference is not None
        else _deep_parent_resolution(body, runtime)
    )
    if resolution.assembly is None:
        serve = InsightDeepServe(
            state=InsightDeepDiveState.UNAVAILABLE,
            analysis=None,
            absence_reason=resolution.refusal_reason or _DEEP_CARD_NOT_FILLED_REASON,
            prompt_version="not-activated",
            cache_outcome="not_read",
            provider_ms=0,
        )
    else:
        assembler = runtime.insight_assembler
        if assembler is None:
            raise RuntimeError("insight assembly runtime is unavailable for this request.")
        serve = assembler.deep_dive(
            resolution.assembly,
            correlation_id=_REQUEST_CORRELATION_ID.get(),
            enrich=resolution.enrich,
            remaining_budget_ms=_REQUEST_REMAINING_MS.get(),
        )
    return _json(200, _deep_dive_payload(body.slot_id, serve, resolution))


def _media_scope_response(
    event: dict[str, Any], owner: str, runtime: PresentationRuntime
) -> dict[str, Any]:
    """Resolve Media-only qualified coverage and atomically mint its parent custody."""
    payload = _body(event)
    if (
        set(payload) - {"session_id", "temporal_selection", "compare_previous"}
        or "session_id" not in payload
    ):
        raise ValidationError(PRESENTATION_REFUSED, "The presentation request fields are invalid.")
    session_id = _text(payload["session_id"], "session_id")
    if len(session_id) > MAX_SESSION_ID_LENGTH:
        raise ValidationError(PRESENTATION_REFUSED, "session_id is too long.")
    compare_previous = payload.get("compare_previous", False)
    if type(compare_previous) is not bool:
        raise ValidationError(PRESENTATION_REFUSED, "compare_previous must be Boolean intent.")
    catalog = runtime.media_scope_catalog
    issuance = runtime.parent_capability_issuance
    if catalog is None or issuance is None:
        raise RuntimeError("Media scope runtime is incomplete.")
    coverage = catalog.coverage(MediaScopeCatalogCoverageRequest(runtime.scope))
    resolution = resolve_temporal(
        TemporalResolutionRequest(
            policy=MEDIA_TEMPORAL_POLICY,
            coverage_intervals=coverage.intervals,
            selection=_temporal_selection(payload.get("temporal_selection")),
            comparison_requested=compare_previous,
        )
    )
    if isinstance(resolution, TemporalResolutionUnavailable):
        if resolution.control is None:
            raise DomainError(
                PRESENTATION_UNAVAILABLE, "No reporting period is available for this page."
            )
        return _json(
            200,
            _presentation_result_json(
                PresentationWindowUnavailableResult(
                    session_id=session_id,
                    temporal=resolution.control,
                    reason_code=resolution.reason_code,
                    recovery=resolution.recovery,
                )
            ),
        )
    temporal = resolution.control
    # aws-pagination: the media-scope catalog port's method, not an AWS client call.
    result = catalog.list_scopes(
        MediaScopeCatalogListRequest(
            runtime.scope, temporal.resolved.window, MAX_MEDIA_SCOPE_CANDIDATES
        )
    )
    snapshot = PageContextSnapshot(
        response_identity="media-efficiency-scope-list",
        visible_window=temporal.resolved.window,
        reporting_window_status=ReportingWindowStatus(
            temporal.resolved.reporting_window_status.value
        ),
        comparison_window=(
            _preceding_period(temporal.resolved.window)
            if temporal.comparison.status is TemporalComparisonStatus.AVAILABLE
            else None
        ),
    )
    context = runtime.prepare_record(_PageMintRequest(owner, session_id, snapshot))
    scope_references = tuple(
        PageContextReference(runtime.token_minter()) for _ in result.candidates
    )
    window_reference = PageContextReference(runtime.token_minter()) if result.candidates else None
    scope_list = MediaEfficiencyScopeList(
        artifact_revision=context.reference.value,
        temporal=_temporal_control_json(temporal),
        outcome="choices" if result.candidates else "empty",
        scopes=tuple(
            MediaScopeChoice(candidate.safe_signature.label, reference.value, context.expires_at)
            for candidate, reference in zip(result.candidates, scope_references, strict=True)
        ),
        window_capability=(
            None
            if window_reference is None
            else MediaWindowCapability(
                window_reference.value, context.expires_at, context.reference.value
            )
        ),
        reason_codes=(() if result.candidates else ("no_qualified_media_scope",)),
        recovery="change_period",
    )
    parent = PageArtifactRecord(
        reference=context.reference,
        artifact=PageArtifact(
            MEDIA_EFFICIENCY_SCOPE_LIST_TYPE,
            MEDIA_EFFICIENCY_VERSION,
            canonical_artifact_json(media_efficiency_payload(scope_list)),
        ),
        verified_owner_id=owner,
        conversation_session_id=session_id,
        expires_at=context.expires_at,
        temporal_control=temporal,
    )
    capabilities = tuple(
        MediaScopeCapabilityRecord(
            reference,
            parent.reference,
            parent.artifact.artifact_type,
            parent.artifact.artifact_version,
            owner,
            session_id,
            parent.expires_at,
            candidate.scope,
            candidate.safe_signature,
            candidate.catalog_identity,
        )
        for candidate, reference in zip(result.candidates, scope_references, strict=True)
    ) + (
        ()
        if window_reference is None
        else (
            MediaWindowCapabilityRecord(
                window_reference,
                parent.reference,
                parent.artifact.artifact_type,
                parent.artifact.artifact_version,
                owner,
                session_id,
                parent.expires_at,
                temporal.resolved,
                compare_previous,
                temporal.comparison,
            ),
        )
    )
    outcome = _issue_parent_after_page_record(
        runtime, context, ParentCapabilityIssuanceRequest(parent, capabilities)
    )
    if outcome.outcome is ParentCapabilityIssuanceOutcome.CAPACITY_REFUSED:
        raise DomainError(
            PRESENTATION_UNAVAILABLE, "The page has too many available row selections."
        )
    if not outcome.issued:
        raise RuntimeError("Media parent capability issuance did not complete.")
    return _json(
        200,
        _presentation_result_json(
            PresentationPageResult(
                session_id=session_id,
                temporal=temporal,
                page=_media_v2_page(
                    parent.artifact.payload,
                    parent.reference.value,
                    None,
                ),
            )
        ),
    )


def _media_artifact_response(
    event: dict[str, Any], owner: str, runtime: PresentationRuntime
) -> dict[str, Any]:
    """Redeem both Media references and revalidate current catalog custody before planning."""
    payload = _body(event)
    if set(payload) != {"session_id", "scope_reference", "window_reference"}:
        raise ValidationError(PRESENTATION_REFUSED, "The presentation request fields are invalid.")
    session_id = _text(payload["session_id"], "session_id")
    if len(session_id) > MAX_SESSION_ID_LENGTH:
        raise ValidationError(PRESENTATION_REFUSED, "session_id is too long.")
    try:
        scope_reference = PageContextReference(_text(payload["scope_reference"], "scope_reference"))
        window_reference = PageContextReference(
            _text(payload["window_reference"], "window_reference")
        )
    except ValueError:
        raise DomainError(
            PRESENTATION_REFRESH_REQUIRED, "Refresh the parent page before selecting again."
        ) from None
    artifacts = runtime.page_artifacts
    catalog = runtime.media_scope_catalog
    if artifacts is None or catalog is None:
        raise RuntimeError("Media artifact runtime is incomplete.")
    now = runtime.clock()
    scope_result = artifacts.read_media_scope_capability(
        MediaScopeCapabilityReadRequest(scope_reference, owner, session_id, now)
    )
    window_result = artifacts.read_media_window_capability(
        MediaWindowCapabilityReadRequest(window_reference, owner, session_id, now)
    )
    scope = scope_result.capability
    window = window_result.capability
    if (
        scope_result.outcome is not ResolutionOutcome.RESOLVED
        or window_result.outcome is not ResolutionOutcome.RESOLVED
        or scope is None
        or window is None
        or (
            scope.parent_artifact_reference != window.parent_artifact_reference
            or scope.parent_artifact_type != MEDIA_EFFICIENCY_SCOPE_LIST_TYPE
            or scope.parent_artifact_version != MEDIA_EFFICIENCY_VERSION
            or window.parent_artifact_type != MEDIA_EFFICIENCY_SCOPE_LIST_TYPE
            or window.parent_artifact_version != MEDIA_EFFICIENCY_VERSION
        )
    ):
        raise DomainError(
            PRESENTATION_REFRESH_REQUIRED, "Refresh the parent page before selecting again."
        )
    parent_result = artifacts.read_artifact(
        PageArtifactReadRequest(scope.parent_artifact_reference, owner, session_id, now)
    )
    parent = parent_result.artifact
    if (
        parent_result.outcome is not ResolutionOutcome.RESOLVED
        or parent is None
        or not isinstance(parent.temporal_control, TemporalControl)
        or (
            parent.artifact.artifact_type != MEDIA_EFFICIENCY_SCOPE_LIST_TYPE
            or parent.artifact.artifact_version != MEDIA_EFFICIENCY_VERSION
            or parent.temporal_control.resolved != window.resolved
            or parent.temporal_control.comparison != window.comparison
        )
    ):
        raise DomainError(
            PRESENTATION_REFRESH_REQUIRED, "Refresh the parent page before selecting again."
        )
    current = catalog.revalidate(
        MediaScopeCatalogRevalidationRequest(
            runtime.scope, scope.scope, scope.catalog_identity, window.resolved.window
        )
    )
    if current.outcome is not MediaScopeCatalogRevalidationOutcome.CURRENT:
        raise DomainError(
            PRESENTATION_REFRESH_REQUIRED, "Refresh the parent page before selecting again."
        )
    # Preparation, inspection, and execution are intentionally unreachable until custody is current.
    fixed = _MediaFixedPlanRequest(
        runtime=runtime,
        scope=scope.scope,
        window=window.resolved.window,
        compare_previous=window.compare_previous,
    )
    requests = _media_fixed_plan_requests(fixed)
    inspector = runtime.presentation_evidence
    tooling = runtime.measurements
    if inspector is None or tooling is None:
        raise RuntimeError("Media artifact evidence runtime is incomplete.")
    prepared = prepare_batch(requests)
    inspection = inspector.inspect(
        PresentationEvidenceInspectionRequest(
            plans=tuple(
                PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                for plan in prepared.plans
            ),
            contracts=prepared.contracts,
            now=prepared.now,
        )
    )
    executed = execute_presentation_batch(
        prepared,
        inspection,
        MeasurementExecutionTooling(
            queries=tooling.queries,
            query_batch_max_concurrency=tooling.query_batch_max_concurrency,
        ),
    )
    child_reference = PageContextReference(runtime.token_minter())
    expires_at = min(parent.expires_at, scope.expires_at, window.expires_at)
    artifact = _media_artifact_from_execution(
        artifact_revision=child_reference.value,
        parent_list_revision=parent.reference.value,
        safe_scope_signature=scope.safe_signature.label,
        temporal=_temporal_control_json(parent.temporal_control),
        expires_at=expires_at,
        period=window.resolved.window,
        executed=executed,
        comparison_requested=window.compare_previous,
    )
    child = PageArtifactRecord(
        reference=child_reference,
        artifact=PageArtifact(
            MEDIA_EFFICIENCY_ARTIFACT_TYPE,
            MEDIA_EFFICIENCY_VERSION,
            canonical_artifact_json(media_efficiency_payload(artifact)),
        ),
        verified_owner_id=owner,
        conversation_session_id=session_id,
        expires_at=expires_at,
        parent_reference=parent.reference,
        temporal_control=parent.temporal_control,
    )
    artifacts.put_artifact(child)
    return _json(
        200,
        _presentation_result_json(
            PresentationPageResult(
                session_id=session_id,
                temporal=parent.temporal_control,
                page=_media_v2_page(
                    child.artifact.payload,
                    child.reference.value,
                    parent.reference.value,
                ),
            )
        ),
    )


def _media_fixed_plan_requests(
    request: _MediaFixedPlanRequest,
) -> tuple[MeasurementToolRequest, ...]:
    """Prepare the closed delivery/viewability plan cohort; selectors stay server-side."""
    runtime = request.runtime
    scope = request.scope
    window = request.window
    compare_previous = request.compare_previous
    if runtime.metric_catalog is None:
        raise RuntimeError("Media metric catalog is unavailable.")
    media = cast("Any", scope)
    filters = {
        "platform_key": media.platform.value,
        "channel_key": media.channel.value,
        "marketplace_key": media.marketplace.value,
    }
    current = runtime.clock()
    requests = tuple(
        MeasurementToolRequest(
            tool=MeasurementTool.COMPARE_WINDOWS
            if compare_previous
            else MeasurementTool.METRIC_BY_GRAIN,
            catalog=runtime.metric_catalog,
            contracts=runtime.contracts,
            metric_name=metric,
            grains=("date", "platform", "channel", "marketplace"),
            window=window,
            prior_window=_preceding_period(window) if compare_previous else None,
            computed_at=current,
            correlation_id="presentation-media-efficiency",
            filters=filters,
            max_rows=DEFAULT_MAX_ROWS,
        )
        for metric in (
            "ad_spend",
            "impressions",
            "clicks",
            "measurable_impressions",
            "viewable_impressions",
            "viewable_impressions_estimated",
        )
    )
    prepared = prepare_batch(requests)
    expected = (
        tuple(
            metric for request in requests for metric in (request.metric_name, request.metric_name)
        )
        if compare_previous
        else tuple(request.metric_name for request in requests)
    )
    if tuple(plan.logical.metric.name for plan in prepared.plans) != expected:
        raise InvariantError(
            ARTIFACT_MALFORMED, "Media plans differ from the governed declaration."
        )
    return requests


def _media_v2_page(
    payload: dict[str, object], artifact_revision: str, parent_revision: str | None
) -> dict[str, object]:
    """Bind either Media artifact kind to its sole generated V2 declaration."""
    artifact_type = payload.get("artifact_type")
    artifact_version = payload.get("artifact_version")
    outcome = payload.get("outcome")
    if (
        artifact_type not in {MEDIA_EFFICIENCY_SCOPE_LIST_TYPE, MEDIA_EFFICIENCY_ARTIFACT_TYPE}
        or artifact_version != MEDIA_EFFICIENCY_VERSION
        or not isinstance(outcome, str)
    ):
        raise _malformed_artifact()
    schema = _v2_page_schema(MEDIA_SCOPE_PATH)
    declaration = next(
        (
            candidate
            for candidate in schema.artifacts
            if candidate.artifact_type == artifact_type
            and candidate.version == artifact_version
            and outcome in candidate.outcomes
        ),
        None,
    )
    if declaration is None:
        raise _malformed_artifact()
    page: dict[str, object] = {
        "actions": [
            {
                "action_id": action.action_id,
                "target_route": _v2_page_route(action.target_page),
            }
            for action in schema.actions
        ],
        "artifact": payload,
        "artifact_id": declaration.artifact_id,
        "artifact_revision": artifact_revision,
        "artifact_type": artifact_type,
        "artifact_version": artifact_version,
        "kind": (
            "media_efficiency_scope_list"
            if artifact_type == MEDIA_EFFICIENCY_SCOPE_LIST_TYPE
            else "media_efficiency"
        ),
        "page_id": schema.page_id,
        "route": schema.route,
    }
    if parent_revision is not None:
        page["parent_revision"] = parent_revision
    return page


def _media_artifact_from_execution(  # noqa: PLR0913
    *,
    artifact_revision: str,
    parent_list_revision: str,
    safe_scope_signature: str,
    temporal: dict[str, object],
    expires_at: datetime,
    period: DateRange,
    executed: PresentationMeasurementExecutionResult,
    comparison_requested: bool,
) -> MediaEfficiencyArtifact:
    """Reduce only complete exact current evidence; absence remains explicitly insufficient."""
    expected = (
        "ad_spend",
        "impressions",
        "clicks",
        "measurable_impressions",
        "viewable_impressions",
        "viewable_impressions_estimated",
    )
    current = tuple(
        slot for slot in executed.slots if slot.role is PresentationEvidenceRole.CURRENT
    )
    required_slots = len(expected) * (2 if comparison_requested else 1)
    if (
        len(executed.slots) != required_slots
        or len(current) != len(expected)
        or tuple(slot.logical.metric.name for slot in current) != expected
        or any(slot.answer is None for slot in executed.slots)
    ):
        return insufficient_media_efficiency(
            artifact_revision=artifact_revision,
            parent_list_revision=parent_list_revision,
            safe_scope_signature=safe_scope_signature,
            temporal=temporal,
            expires_at=expires_at,
            reason_codes=("no_exact_current_media_evidence",),
        )
    try:
        rows_by_metric = {
            slot.logical.metric.name: _media_daily_operands(slot, period) for slot in current
        }
        dates = tuple(period.start + timedelta(days=index) for index in range(period.days))
        delivery_series = tuple(
            media_delivery_row(
                day,
                (
                    rows_by_metric["ad_spend"][day],
                    rows_by_metric["impressions"][day],
                    rows_by_metric["clicks"][day],
                ),
            )
            for day in dates
        )
        viewability_series = tuple(
            media_viewability_row(
                day,
                (
                    rows_by_metric["measurable_impressions"][day],
                    rows_by_metric["viewable_impressions"][day],
                    rows_by_metric["viewable_impressions_estimated"][day],
                ),
            )
            for day in dates
        )
        delivery_operands: tuple[MediaMetric, MediaMetric, MediaMetric] = (
            _media_total(rows_by_metric["ad_spend"].values(), "ad_spend"),
            _media_total(rows_by_metric["impressions"].values(), "impressions"),
            _media_total(rows_by_metric["clicks"].values(), "clicks"),
        )
        viewability_operands: tuple[MediaMetric, MediaMetric, MediaMetric] = (
            _media_total(
                rows_by_metric["measurable_impressions"].values(), "measurable_impressions"
            ),
            _media_total(rows_by_metric["viewable_impressions"].values(), "viewable_impressions"),
            _media_total(
                rows_by_metric["viewable_impressions_estimated"].values(),
                "viewable_impressions_estimated",
            ),
        )
        artifact = reduce_media_efficiency(
            artifact_revision=artifact_revision,
            parent_list_revision=parent_list_revision,
            safe_scope_signature=safe_scope_signature,
            temporal=temporal,
            expires_at=expires_at,
            delivery_operands=delivery_operands,
            delivery_series=delivery_series,
            viewability_operands=viewability_operands,
            viewability_series=viewability_series,
        )
    except KeyError, TypeError, ValueError:
        return insufficient_media_efficiency(
            artifact_revision=artifact_revision,
            parent_list_revision=parent_list_revision,
            safe_scope_signature=safe_scope_signature,
            temporal=temporal,
            expires_at=expires_at,
            reason_codes=("media_evidence_not_exact",),
        )
    return artifact


def _media_daily_operands(
    slot: PresentationMeasurementAnswerSlot, period: DateRange
) -> dict[date, MediaMetric]:
    """Read an exact per-day answer without accepting adapter-derived metrics or totals."""
    answer = slot.answer
    if answer is None or answer.data.units is None:
        raise ValueError("media current evidence is absent")
    metadata = answer.metadata
    basis = metadata.measurement_basis
    if (
        answer.data.grain.dimensions != slot.logical.group_by
        or metadata.requested_coverage_intervals != (period,)
        or metadata.actual_coverage_intervals != (period,)
        or metadata.coverage_holes
        or metadata.truncated
        or metadata.suppression.rows_suppressed
        or metadata.freshness_status is FreshnessStatus.NEVER_LOADED
        or len(basis) != 1
        or len(answer.data.values) != period.days
    ):
        raise ValueError("media evidence is not an exact complete daily source")
    values: dict[date, MediaMetric] = {}
    for raw in answer.data.values:
        day_value = raw.get("date_key")
        numeric = raw.get(MEASURE_ALIAS)
        if (
            not isinstance(day_value, str)
            or isinstance(numeric, bool)
            or not isinstance(numeric, (int, Decimal))
        ):
            raise ValueError("media daily evidence shape is invalid")
        day = date.fromisoformat(day_value)
        if day < period.start or day > period.end or day in values or Decimal(numeric) < 0:
            raise ValueError("media daily evidence does not have exact dates")
        units = answer.data.units
        unit = units.currency_code if units.kind is UnitKind.CURRENCY else units.kind.value
        source_system = slot.logical.metric.source_system
        if unit is None:
            raise ValueError("media currency evidence lacks its source currency")
        values[day] = MediaMetric(
            slot.logical.metric.name,
            Decimal(numeric),
            unit,
            basis[0].value,
            "day",
            source_system,
        )
    if set(values) != {period.start + timedelta(days=index) for index in range(period.days)}:
        raise ValueError("media daily evidence does not cover the resolved window")
    return values


def _media_total(values: Iterable[MediaMetric], slot: str) -> MediaMetric:
    operands = tuple(values)
    if not operands:
        raise ValueError("media exact evidence cannot be empty")
    first = operands[0]
    if any(
        item.slot != slot
        or (item.unit, item.basis, item.grain, item.source)
        != (first.unit, first.basis, first.grain, first.source)
        for item in operands
    ):
        raise ValueError("media exact evidence metadata differs by day")
    return MediaMetric(
        slot,
        sum((item.value for item in operands), Decimal()),
        first.unit,
        first.basis,
        "period",
        first.source,
    )


def _execute_request(execution: _PresentationExecutionRequest) -> dict[str, Any]:
    """Execute the established page reducer after its window has been resolved."""
    request = execution.request
    if isinstance(request, _ListPresentationInput):
        return _campaign_list_response(execution)
    return _json(200, _execute_analytical_request(execution).body)


@dataclass(frozen=True, slots=True)
class _AnalyticalResponse:
    """One analytical page body and the `page#` record it was served under."""

    body: dict[str, object]
    page_record: PageContextRecord


def _execute_analytical_request(execution: _PresentationExecutionRequest) -> _AnalyticalResponse:
    """Write the page record, then the artifact under its reference, then build the body."""
    operation = execution.operation
    owner = execution.owner
    request = execution.request
    runtime = execution.runtime
    if not isinstance(request, _AnalyticalPresentationInput):
        raise RuntimeError("analytical execution request is invalid.")
    analytical = _analytical_page(
        _AnalyticalExecution(
            owner,
            operation,
            request,
            runtime,
            execution.validated_channel_selection,
        )
    )
    snapshot, page_artifact = analytical.snapshot, analytical.artifact
    page_record = runtime.prepare_record(
        _PageMintRequest(
            owner=owner,
            session_id=request.session_id,
            snapshot=snapshot,
            maximum_expiry=analytical.maximum_expiry,
        )
    )
    runtime.page_context.put(page_record)
    issued_capabilities: tuple[_IssuedChannelCapability, ...] = ()
    artifact_record: PageArtifactRecord | None = None
    if page_artifact is not None:
        if runtime.page_artifacts is None:
            raise RuntimeError("page artifact storage is unavailable.")
        artifact_record = PageArtifactRecord(
            reference=page_record.reference,
            artifact=page_artifact,
            verified_owner_id=page_record.verified_owner_id,
            conversation_session_id=page_record.conversation_session_id,
            expires_at=page_record.expires_at,
            parent_reference=analytical.parent_reference,
            temporal_control=execution.temporal_control,
        )
        is_v2_channel_root = (
            execution.temporal_control is not None
            and operation == CHANNEL_PERFORMANCE_PATH
            and analytical.parent_reference is None
        )
        if is_v2_channel_root:
            issued_capabilities = _issue_v2_channel_parent_capabilities(
                runtime, artifact_record, analytical
            )
        else:
            runtime.page_artifacts.put_artifact(artifact_record)
            if analytical.channel_current is not None and analytical.parent_reference is None:
                issued_capabilities = _issue_channel_capabilities(
                    runtime, artifact_record, analytical
                )
    page_json = _analytical_page_json(analytical, runtime.dimension_labels, artifact_record)
    body: dict[str, object] = {
        "page": page_json,
        "session_id": request.session_id,
        "reporting_window_status": snapshot.reporting_window_status.value,
        "visible_window": _window_json(snapshot.visible_window),
    }
    if operation == CHANNEL_PERFORMANCE_PATH and request.selection_reference is None:
        body["row_capabilities"] = [
            _channel_capability_json(capability) for capability in issued_capabilities
        ]
    return _AnalyticalResponse(body, page_record)


def _accepts_presentation_v2(event: dict[str, Any]) -> bool:
    """Return whether the caller explicitly accepts an available V2 representation."""
    headers = event.get("headers", {})
    headers = cast("dict[object, object]", headers)
    accept = next(
        (
            value
            for name, value in headers.items()
            if isinstance(name, str) and name.lower() == "accept"
        ),
        "",
    )
    if not isinstance(accept, str):
        return False
    for item in accept.lower().split(","):
        parts = [part.strip() for part in item.split(";")]
        if not parts or parts[0] != PRESENTATION_V2_MEDIA_TYPE:
            continue
        quality = Decimal(1)
        try:
            for parameter in parts[1:]:
                name, separator, value = parameter.partition("=")
                if name.strip() == "q":
                    if not separator:
                        raise InvalidOperation
                    quality = Decimal(value.strip())
        except InvalidOperation:
            continue
        if quality.is_finite() and Decimal(0) < quality <= Decimal(1):
            return True
    return False


def _is_v2_campaign_child_request(event: dict[str, Any], operation: str) -> bool:
    """Recognize only opaque capabilities minted by the atomic Campaign V2 parent."""
    payload = _body(event)
    reference = payload.get("selection_reference")
    return (
        operation == CAMPAIGN_DETAIL_PATH
        and isinstance(reference, str)
        and reference.startswith("v2_")
    )


def _is_v2_channel_child_request(event: dict[str, Any], operation: str) -> bool:
    """Return whether a V2 Channel request presents a child selection reference."""
    return operation == CHANNEL_PERFORMANCE_PATH and "selection_reference" in _body(event)


def _v2_channel_child_request(event: dict[str, Any]) -> _V2ChannelChildPresentationInput:
    """Decode the sole V2 Channel child grammar before any custody read."""
    payload = _body(event)
    allowed = {"session_id", "selection_reference"}
    fields = set(payload)
    if fields != allowed:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "The presentation request fields are invalid.",
            context={"outcome": "invalid_fields", "n": len(fields - allowed)},
        )
    session_id = _text(payload["session_id"], "session_id")
    if len(session_id) > MAX_SESSION_ID_LENGTH:
        raise ValidationError(PRESENTATION_REFUSED, "session_id is too long.")
    return _V2ChannelChildPresentationInput(
        session_id=session_id,
        selection_reference=_selection_reference(payload["selection_reference"], channel=True),
    )


def _v2_capability_refresh_required(event: dict[str, Any], operation: str) -> bool:
    """Refuse legacy capability redemption under V2 before any custody or query access."""
    if operation not in {
        CAMPAIGN_DETAIL_PATH,
        CHANNEL_PERFORMANCE_PATH,
    }:
        return False
    payload = _body(event)
    if "selection_reference" not in payload:
        return False
    allowed = {"session_id", "selection_reference"}
    fields = set(payload)
    if fields != allowed:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "The presentation request fields are invalid.",
            context={"outcome": "invalid_fields", "n": len(fields - allowed)},
        )
    session_id = _text(payload["session_id"], "session_id")
    if len(session_id) > MAX_SESSION_ID_LENGTH:
        raise ValidationError(PRESENTATION_REFUSED, "session_id is too long.")
    _selection_reference(
        payload["selection_reference"],
        channel=operation == CHANNEL_PERFORMANCE_PATH,
    )
    return True


def _v2_campaign_child_response(input_request: _V2ParentResponseRequest) -> dict[str, Any]:
    """Redeem a V2 Campaign choice only through its temporal-bound durable parent."""
    request = _v2_campaign_child_request(input_request.event)
    analytical_request = _AnalyticalPresentationInput(
        session_id=request.session_id,
        selection_reference=request.selection_reference,
    )
    selected = _validated_campaign_capability(
        _AnalyticalExecution(
            input_request.owner,
            CAMPAIGN_DETAIL_PATH,
            analytical_request,
            input_request.runtime,
        )
    )
    temporal = selected.parent.temporal_control
    if not isinstance(temporal, TemporalControl) or temporal.resolved.window != selected.period:
        raise DomainError(
            PRESENTATION_REFRESH_REQUIRED,
            "Refresh the parent page before selecting again.",
        )
    executed = _execute_analytical_request(
        _PresentationExecutionRequest(
            event=input_request.event,
            operation=CAMPAIGN_DETAIL_PATH,
            owner=input_request.owner,
            request=analytical_request,
            runtime=input_request.runtime,
            temporal_control=temporal,
        )
    )
    legacy_page = _response_body(_json(200, executed.body)).get("page")
    delivery = _campaign_delivery(
        selected,
        _AnalyticalExecution(
            input_request.owner,
            CAMPAIGN_DETAIL_PATH,
            analytical_request,
            input_request.runtime,
        ),
    )
    page = _v2_campaign_detail_page(
        _V2CampaignDetailProjectionInput(
            legacy_page=legacy_page,
            temporal=temporal,
            delivery=delivery,
            runtime=input_request.runtime,
            owner=input_request.owner,
            selected=selected,
            page_record=executed.page_record,
        )
    )
    return _json(
        200,
        _presentation_result_json(
            PresentationPageResult(
                session_id=request.session_id,
                temporal=temporal,
                page=page,
            )
        ),
    )


def _v2_campaign_child_request(event: dict[str, Any]) -> _V2ChannelChildPresentationInput:
    """Decode Campaign's closed V2 child grammar before a custody read."""
    payload = _body(event)
    if set(payload) != {"session_id", "selection_reference"}:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "The presentation request fields are invalid.",
            context={
                "outcome": "invalid_fields",
                "n": len(set(payload) - {"session_id", "selection_reference"}),
            },
        )
    session_id = _text(payload["session_id"], "session_id")
    if len(session_id) > MAX_SESSION_ID_LENGTH:
        raise ValidationError(PRESENTATION_REFUSED, "session_id is too long.")
    return _V2ChannelChildPresentationInput(
        session_id=session_id,
        selection_reference=_selection_reference(payload["selection_reference"]),
    )


def _detail_insight_slots(request: _DetailInsightInput) -> tuple[InsightSlot, ...]:
    """Attach the shared canonical detail request roster to the presentation response."""
    derivation = detail_insight_request_derivation(
        DetailInsightRequestDerivationInput(
            detail=request.detail,
            delivery=request.delivery,
            subject=request.assembly.context.subject,
            scope=request.assembly.runtime.scope,
            contracts=INSIGHT_CONTRACTS,
        )
    )
    return _assembled_insights(
        _InsightAssemblyInput(
            runtime=request.assembly.runtime,
            context=derivation.context,
            slot_ids=request.assembly.slot_ids,
            requests=derivation.requests,
            silence_reasons=derivation.silence_reasons,
            missing_evidence_by_slot=derivation.missing_evidence_by_slot,
        )
    )


def _served_detail_delivery(
    detail: CampaignAttributionDetailArtifact, delivery: CampaignDelivery
) -> CampaignDelivery:
    """Return the delivery exactly as the detail page serves it (one home).

    ADR-072: the served status sheet itemizes attribution and delivery together, so the V1
    per-slot conditions are projected into served words ahead of the delivery roster. The
    detail cards fingerprint this served delivery, so every route that re-derives them (the
    deep section, F12) must read the same one.
    """
    return replace(
        delivery,
        evidence=(*campaign_attribution_statuses(detail), *delivery.evidence),
    )


def _v2_campaign_detail_page(request: _V2CampaignDetailProjectionInput) -> dict[str, object]:
    """Rebind a frozen V1 detail to the closed V2 delivery extension."""
    legacy_page = request.legacy_page
    temporal = request.temporal
    if not isinstance(legacy_page, dict):
        raise _malformed_artifact()
    source = cast("dict[str, object]", legacy_page)
    artifact = source.get("artifact")
    revision = source.get("artifact_revision")
    parent_revision = source.get("parent_revision")
    if not isinstance(artifact, dict) or not isinstance(revision, str) or not revision.strip():
        raise _malformed_artifact()
    if not isinstance(parent_revision, str) or not parent_revision.strip():
        raise _malformed_artifact()
    payload = cast("dict[str, object]", artifact)
    try:
        decoded = campaign_page_artifact_from_payload(payload)
    except TypeError, ValueError:
        raise _malformed_artifact() from None
    if not isinstance(decoded, CampaignAttributionDetailArtifact):
        raise _malformed_artifact()
    if decoded.period != temporal.resolved.window:
        raise _malformed_artifact()
    delivery = _served_detail_delivery(decoded, request.delivery)
    derivation = detail_insight_request_derivation(
        DetailInsightRequestDerivationInput(
            detail=decoded,
            delivery=delivery,
            subject=request.selected.capability.campaign.value,
            scope=request.runtime.scope,
            contracts=INSIGHT_CONTRACTS,
        )
    )
    v2 = CampaignAttributionDetailV2(
        decoded,
        delivery,
        _detail_insight_slots(
            _DetailInsightInput(
                decoded,
                delivery,
                _InsightAssemblyInput(
                    runtime=request.runtime,
                    context=derivation.context,
                    slot_ids=(
                        InsightSlotId.TREND_SHAPE,
                        InsightSlotId.EFFICIENCY_DRIFT,
                        InsightSlotId.FUNNEL,
                        InsightSlotId.NEXT_CHECK,
                    ),
                    requests=derivation.requests,
                    silence_reasons=derivation.silence_reasons,
                    missing_evidence_by_slot=derivation.missing_evidence_by_slot,
                ),
            )
        ),
        campaign_detail_evidence_status(decoded.period, delivery.evidence, PRESENTATION_LABELS),
    )
    schema = _v2_page_schema(CAMPAIGN_LIST_PATH)
    declaration = next(
        (item for item in schema.artifacts if item.artifact_id == "campaign_detail"),
        None,
    )
    visual = next(
        (item for item in schema.visuals if item.visual_id == "campaign_delivery_daily"),
        None,
    )
    if (
        declaration is None
        or declaration.artifact_type != CAMPAIGN_ATTRIBUTION_DETAIL_ARTIFACT_TYPE
        or declaration.version != CAMPAIGN_ATTRIBUTION_DETAIL_ARTIFACT_VERSION
        or visual is None
        or visual.metric_cohort != tuple(slot.value for slot in request.delivery.series_metrics)
        or any(
            grain not in tuple(item.value for item in schema.temporal_policy.display_grains)
            for grain in request.delivery.available_grains
        )
    ):
        raise _malformed_artifact()
    return {
        "actions": [
            {"action_id": action.action_id, "target_route": _v2_page_route(action.target_page)}
            for action in schema.actions
        ],
        "artifact": campaign_attribution_detail_v2_payload(v2),
        "artifact_id": declaration.artifact_id,
        "artifact_revision": revision,
        "artifact_type": declaration.artifact_type,
        "artifact_version": declaration.version,
        "kind": decoded.kind.value,
        "page_id": schema.page_id,
        "parent_revision": parent_revision,
        "route": schema.route,
        # The campaign's channel step targets the Scorecard's channel row (ia §7.2 item 13).
        "scorecard_step": _scorecard_step(
            temporal, request.selected.capability.channel.value, request.runtime
        ),
        **_page_context_fields(
            request.page_record,
            # An insufficient detail is a page-level absence: no starter questions.
            _page_starter_questions(
                "campaigns",
                temporal,
                detail=True,
                campaign=decoded.selection.effective_name,
            )
            if decoded.outcome is CampaignDetailOutcome.SELECTED
            else (),
        ),
    }


def _v2_channel_child_response(input_request: _V2ParentResponseRequest) -> dict[str, Any]:
    """Redeem one V2 Channel row only through its durable parent temporal control."""
    request = _v2_channel_child_request(input_request.event)
    analytical_request = _AnalyticalPresentationInput(
        session_id=request.session_id,
        selection_reference=request.selection_reference,
    )
    selected = _validated_channel_selection(
        _AnalyticalExecution(
            input_request.owner,
            CHANNEL_PERFORMANCE_PATH,
            analytical_request,
            input_request.runtime,
        )
    )
    temporal = selected.parent.temporal_control
    if (
        not isinstance(temporal, TemporalControl)
        or temporal.resolved.window != selected.capability.current_period
    ):
        raise _channel_selection_unavailable()
    legacy_response = _execute_request(
        _PresentationExecutionRequest(
            event=input_request.event,
            operation=CHANNEL_PERFORMANCE_PATH,
            owner=input_request.owner,
            request=analytical_request,
            runtime=input_request.runtime,
            temporal_control=temporal,
            validated_channel_selection=selected,
        )
    )
    page = _v2_page_projection(
        CHANNEL_PERFORMANCE_PATH,
        _response_body(legacy_response).get("page"),
        temporal,
    )
    return _json(
        200,
        _presentation_result_json(
            PresentationPageResult(
                session_id=request.session_id,
                temporal=temporal,
                page=page,
            )
        ),
    )


def _v2_parent_response(  # noqa: PLR0911 -- one explicit branch per declared parent route
    input_request: _V2ParentResponseRequest,
) -> dict[str, Any]:
    """Resolve a V2 parent result and bind its page to the declared V2 contract."""
    if input_request.operation == CAMPAIGN_LIST_PATH:
        return _v2_campaign_preset_parent_response(input_request)
    if input_request.operation == RETAIL_HEALTH_PATH:
        return _v2_retail_health_preset_parent_response(input_request)
    if input_request.operation == SCORECARD_PATH:
        return _v2_scorecard_parent_response(input_request)
    if input_request.operation == NEW_CUSTOMERS_PATH:
        return _v2_new_customers_parent_response(input_request)
    if input_request.operation == REACH_PATH:
        return _v2_reach_parent_response(input_request)
    if input_request.operation == RETENTION_PATH:
        return _v2_retention_parent_response(input_request)
    if input_request.operation == AUDIENCES_PATH:
        return _v2_audiences_parent_response(input_request)
    event = input_request.event
    operation = input_request.operation
    owner = input_request.owner
    runtime = input_request.runtime
    parent_request = _v2_parent_request(event, operation)
    resolved = _resolve_v2_parent_request(operation, parent_request, runtime)
    if isinstance(resolved, PresentationWindowUnavailableResult):
        return _json(200, _presentation_result_json(resolved))
    legacy_response = _execute_request(
        _PresentationExecutionRequest(
            event,
            operation,
            owner,
            resolved.request,
            runtime,
            resolved.temporal,
        ),
    )
    legacy_body = _response_body(legacy_response)
    page = _v2_page_projection(operation, legacy_body.get("page"), resolved.temporal)
    result = PresentationPageResult(
        session_id=parent_request.session_id,
        temporal=resolved.temporal,
        page=page,
    )
    body = _presentation_result_json(result)
    row_capabilities = _v2_channel_row_capabilities(operation, legacy_body)
    if row_capabilities:
        body["row_capabilities"] = row_capabilities
    return _json(200, body)


def _v2_campaign_preset_parent_response(
    input_request: _V2ParentResponseRequest,
) -> dict[str, Any]:
    """Serve a declared Campaign preset body without catalog or measurement fallback."""
    request = _v2_parent_request(input_request.event, CAMPAIGN_LIST_PATH)
    if request.temporal_selection.kind is TemporalSelectionKind.CUSTOM:
        return _campaign_preset_unavailable(request, input_request.runtime)
    schema = _v2_page_schema(CAMPAIGN_LIST_PATH)
    selected = (
        schema.temporal_policy.default_preset
        if request.temporal_selection.kind is TemporalSelectionKind.OMITTED
        else request.temporal_selection.preset_id
    )
    if selected is None:
        raise RuntimeError("campaign preset selection is unavailable.")
    variant = PresetArtifactVariant(selected.value)
    pointers = input_request.runtime.preset_artifact_pointers
    bodies = input_request.runtime.preset_artifact_bodies
    if pointers is None or bodies is None:
        raise RuntimeError("campaign preset artifact runtime is unavailable.")
    page_id = PresetArtifactPage("campaigns")
    roster = pointers.roster(
        PresetArtifactPointerRosterRequest(
            page_id,
            tuple(
                PresetArtifactVariant(candidate.value)
                for candidate in schema.temporal_policy.candidate_presets
            ),
        )
    )
    pointer = roster.pointer_for(variant)
    if pointer is None:
        return _campaign_preset_unavailable(request, input_request.runtime, roster)
    generation = _served_generation(input_request.runtime, pointer)
    body = bodies.read(pointer)
    _attest_campaign_preset_body(pointer, body)
    if request.campaign_roster_shard is not None:
        return _campaign_roster_shard_response(
            request, input_request.runtime, (pointer, body), request.campaign_roster_shard
        )
    try:
        payload = json.loads(body.payload_json)
    except json.JSONDecodeError:
        raise _malformed_artifact() from None
    if campaign_portfolio_artifact_superseded(payload):
        # A body baked before render format 14 is this preset's not-yet-published absence, as a
        # missing pointer is, until the page's next bake replaces it (never decoded, never 500).
        unserved = PresetArtifactPointerRosterResult(
            roster.request,
            tuple(
                None if candidate is not None and candidate.variant == variant else candidate
                for candidate in roster.pointers
            ),
        )
        return _campaign_preset_unavailable(request, input_request.runtime, unserved)
    try:
        artifact = campaign_portfolio_artifact_from_payload(payload)
    except TypeError, ValueError:
        raise _malformed_artifact() from None
    temporal = _campaign_preset_temporal(request, artifact, roster)
    sort = request.campaign_sort or CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc")
    filtered = _campaign_filter_parent(
        input_request.runtime, pointer, body, artifact, request.filter_id
    )
    resolved_segment = _campaign_preset_segment(
        _CampaignPresetCustodyReadRequest(
            input_request.runtime,
            pointer,
            body,
            filtered.artifact,
            sort,
            request.campaign_segment,
            filtered,
        )
    )
    page = _reissue_campaign_preset_page(
        _CampaignPresetServeRequest(
            request=request,
            owner=input_request.owner,
            runtime=input_request.runtime,
            temporal=temporal,
            artifact=resolved_segment.artifact,
            custody_rows=resolved_segment.custody_rows,
        )
    )
    return _json(
        200,
        _presentation_result_json(
            PresentationPageResult(
                session_id=request.session_id,
                temporal=temporal,
                page=_with_generation(page, generation),
            )
        ),
    )


def _campaign_roster_shard_response(  # noqa: PLR0913, PLR0917 -- the request, runtime, parent, shard
    request: _V2ParentPresentationInput,
    runtime: PresentationRuntime,
    parent: tuple[PresetArtifactPointer, PresetArtifactBody],
    shard: int,
) -> dict[str, Any]:
    """Serve one baked shard of a filter's name roster through the parent custody's index.

    Existence is the index's: a shard or filter the bake never wrote is a 400. A listed shard
    whose bytes or fields disagree with its descriptor, the request or this revision is the
    malformed-artifact refusal (ADR-145), never a fallback.
    """
    pointer, body = parent
    custody_store = runtime.preset_artifact_custody
    if custody_store is None or pointer.custody_digest is None:
        raise _malformed_artifact()
    manifest = custody_store.read(pointer)
    _attest_campaign_preset_custody(pointer, manifest)
    header, index = _parent_index(manifest)
    if header.get("body_digest") != body.digest:
        raise _malformed_artifact()
    identity = campaign_roster_shard_identity(request.filter_id, shard)
    descriptor = _indexed_descriptor(runtime, pointer, index, identity)
    if descriptor is None:
        raise ValidationError(PRESENTATION_REFUSED, "The campaign roster shard is invalid.")
    sibling = _read_indexed(runtime, pointer, identity, descriptor)
    try:
        payload = cast("dict[str, object]", json.loads(sibling.body.payload_json))
        campaign_roster_shard_from_payload(payload, shard, pointer.revision.value)
        custody = campaign_custody_manifest_from_payload(json.loads(sibling.custody.payload_json))
    except TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None
    if custody.body_digest != sibling.body.digest or custody.rows:
        raise _malformed_artifact()
    return _json(
        200,
        {
            "kind": "campaign_roster_shard",
            "session_id": request.session_id,
            "parent_revision": pointer.revision.value,
            "shard": payload,
        },
    )


def _campaign_preset_segment(  # noqa: PLR0912 -- one custody chain, each refusal its own
    input_request: _CampaignPresetCustodyReadRequest,
) -> _CampaignPresetResolvedSegment:
    """Resolve one pointer-bound segment without measurement, catalog reads, or reordering.

    A non-canonical segment is read through the parent custody's sibling index (ADR-145).
    """
    custody_store = input_request.runtime.preset_artifact_custody
    pointer = input_request.pointer
    filtered = input_request.filter_parent
    if filtered is not None and filtered.manifest is not None:
        # A filter sibling's custody was read and attested through the pointer's custody.
        if custody_store is None:
            raise _malformed_artifact()
        parent_manifest = filtered.manifest
        index = filtered.index
    else:
        if pointer.custody_digest is None:
            canonical = (
                input_request.sort
                == CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc")
                and input_request.segment_index == 0
            )
            if not canonical:
                raise _malformed_artifact()
            return _CampaignPresetResolvedSegment(input_request.artifact, None)
        if custody_store is None:
            raise _malformed_artifact()
        manifest_object = custody_store.read(pointer)
        _attest_campaign_preset_custody(pointer, manifest_object)
        header, index = _parent_index(manifest_object)
        try:
            parent_manifest = campaign_custody_manifest_from_payload(header)
        except TypeError, ValueError:
            raise _malformed_artifact() from None
        if parent_manifest.body_digest != input_request.body.digest:
            raise _malformed_artifact()

    canonical = (
        input_request.sort == CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc")
        and input_request.segment_index == 0
    )
    if canonical:
        if not custody_rows_align(
            parent_manifest,
            tuple(row.signature for row in input_request.artifact.rows),
        ):
            raise _malformed_artifact()
        return _CampaignPresetResolvedSegment(input_request.artifact, parent_manifest.rows)

    pagination = input_request.artifact.pagination
    if pagination is None or not 0 <= input_request.segment_index < pagination.segment_count:
        raise ValidationError(PRESENTATION_REFUSED, "The campaign segment is out of range.")
    sibling = _indexed_sibling(
        input_request.runtime,
        pointer,
        index,
        PresetArtifactSiblingIdentity(
            f"{input_request.sort.field.value}-{input_request.sort.direction}",
            input_request.segment_index,
            ALL_CHANNELS_FILTER_ID if filtered is None else filtered.filter_id,
        ),
    )
    try:
        segment = campaign_portfolio_segment_from_payload(
            json.loads(sibling.body.payload_json), input_request.artifact
        )
        segment_manifest = campaign_custody_manifest_from_payload(
            json.loads(sibling.custody.payload_json)
        )
    except TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None
    if (
        segment.sort != input_request.sort
        or segment.pagination is None
        or segment.pagination.segment_index != input_request.segment_index
        or segment_manifest.body_digest != sibling.body.digest
        or not custody_rows_align(segment_manifest, tuple(row.signature for row in segment.rows))
    ):
        raise _malformed_artifact()
    return _CampaignPresetResolvedSegment(segment, segment_manifest.rows)


def _parent_index(
    manifest: PresetArtifactCustodyManifest,
) -> tuple[dict[str, object], tuple[PresetArtifactSiblingIndexEntry, ...]]:
    """Split an attested parent custody into its page header and its sibling index."""
    try:
        return split_parent_custody(manifest)
    except TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None


@dataclass(frozen=True, slots=True)
class _IndexedSibling:
    """One sibling read through the index: its descriptor, public body and private custody."""

    descriptor: PresetArtifactSiblingDescriptor
    body: PresetArtifactSiblingBody
    custody: PresetArtifactSiblingCustody


def _indexed_sibling(  # noqa: PLR0913, PLR0917 -- one pointer-bound, index-bound read
    runtime: PresentationRuntime,
    pointer: PresetArtifactPointer,
    index: tuple[PresetArtifactSiblingIndexEntry, ...],
    identity: PresetArtifactChildIdentity,
) -> _IndexedSibling:
    """Read one sibling through the parent custody's index (ADR-145, the two-level read).

    pointer -> parent custody (attested by the caller) -> the index shard whose range covers the
    identity, digest-checked against the parent's entry -> the identity's descriptor -> its body
    and custody, each digest-checked. An identity no shard covers or lists, an unreadable or
    tampered shard, or a sibling whose bytes don't match is the malformed-artifact refusal,
    never a fallback.
    """
    descriptor = _indexed_descriptor(runtime, pointer, index, identity)
    if descriptor is None:
        raise _malformed_artifact()
    return _read_indexed(runtime, pointer, identity, descriptor)


def _indexed_descriptor(  # noqa: PLR0913, PLR0917 -- one pointer-bound, index-bound lookup
    runtime: PresentationRuntime,
    pointer: PresetArtifactPointer,
    index: tuple[PresetArtifactSiblingIndexEntry, ...],
    identity: PresetArtifactChildIdentity,
) -> PresetArtifactSiblingDescriptor | None:
    """Find an identity's descriptor in the covering index shard, or None when it isn't listed.

    An unreadable or tampered shard is the malformed-artifact refusal; an identity no shard
    covers or lists is the caller's to refuse.
    """
    custody_store = runtime.preset_artifact_custody
    position = covering_index_shard(index, identity)
    if custody_store is None:
        raise _malformed_artifact()
    if position is None:
        return None
    entry = index[position]
    try:
        shard = custody_store.read_sibling(
            PresetArtifactSiblingReadRequest(
                pointer.page,
                pointer.variant,
                pointer.revision,
                PresetArtifactSiblingDescriptor(
                    PresetArtifactIndexShardIdentity(position), None, entry.digest
                ),
            )
        )
        descriptors = index_shard_descriptors(shard, entry)
    except InvariantError, TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None
    return next((item for item in descriptors if item.identity == identity), None)


def _read_indexed(  # noqa: PLR0913, PLR0917 -- one pointer-bound read of a listed sibling
    runtime: PresentationRuntime,
    pointer: PresetArtifactPointer,
    identity: PresetArtifactChildIdentity,
    descriptor: PresetArtifactSiblingDescriptor,
) -> _IndexedSibling:
    """Read a listed sibling's body and custody, each checked against its descriptor."""
    bodies = runtime.preset_artifact_bodies
    custody_store = runtime.preset_artifact_custody
    if bodies is None or custody_store is None:
        raise _malformed_artifact()
    request = PresetArtifactSiblingReadRequest(
        pointer.page, pointer.variant, pointer.revision, descriptor
    )
    try:
        public = bodies.read_sibling(request)
        private = custody_store.read_sibling(request)
    except InvariantError:
        raise _malformed_artifact() from None
    expected = (pointer.page, pointer.variant, pointer.revision, identity)
    if (
        (public.page, public.variant, public.revision, public.identity) != expected
        or (private.page, private.variant, private.revision, private.identity) != expected
        or public.digest != descriptor.body_digest
        or private.digest != descriptor.custody_digest
    ):
        raise _malformed_artifact()
    return _IndexedSibling(descriptor, public, private)


def _attest_campaign_preset_custody(
    pointer: PresetArtifactPointer, manifest: PresetArtifactCustodyManifest
) -> None:
    """Reject an adapter response whose identity or immutable bytes differ from the pointer."""
    if (
        manifest.page,
        manifest.variant,
        manifest.revision,
        manifest.digest,
    ) != (
        pointer.page,
        pointer.variant,
        pointer.revision,
        pointer.custody_digest,
    ):
        raise _malformed_artifact()


def _attest_campaign_preset_body(pointer: PresetArtifactPointer, body: PresetArtifactBody) -> None:
    """Reject an adapter response whose identity or immutable bytes differ from the pointer."""
    if (
        body.page,
        body.variant,
        body.revision,
        body.digest,
    ) != (
        pointer.page,
        pointer.variant,
        pointer.revision,
        pointer.body_digest,
    ):
        raise _malformed_artifact()


def _roster_temporal_control(
    control: TemporalControl,
    policy: PresentationTemporalPolicy,
    roster: PresetArtifactPointerRosterResult,
) -> TemporalControl:
    """Advertise exactly the baked declared variants; declaration exceptions still win.

    The deployed defect this replaces: preset availability inferred from only the selected
    artifact's own current-plus-prior span rendered other baked variants falsely disabled.
    """
    candidates = {preset.value for preset in policy.candidate_presets}
    exceptions = {
        exception.preset_id: exception.reason_code for exception in policy.preset_exceptions
    }
    presets: list[TemporalPreset] = []
    for preset in control.presets:
        preset_id = preset.preset_id
        reason: TemporalUnavailableReason | None
        if preset_id.value not in candidates:
            reason = TemporalUnavailableReason.PRESET_UNAVAILABLE
        elif preset_id in exceptions:
            try:
                reason = TemporalUnavailableReason(exceptions[preset_id])
            except ValueError:
                reason = TemporalUnavailableReason.STALE_TEMPORAL_DECLARATION
        elif roster.pointer_for(PresetArtifactVariant(preset_id.value)) is not None:
            reason = None
        else:
            reason = TemporalUnavailableReason.PRESET_UNAVAILABLE
        presets.append(
            TemporalPreset(
                preset_id=preset_id,
                label=preset.label,
                availability=(
                    TemporalAvailability.AVAILABLE
                    if reason is None
                    else TemporalAvailability.UNAVAILABLE
                ),
                reason_code=reason,
            )
        )
    return replace(control, presets=tuple(presets))


_UNPUBLISHED_COVERAGE_DAYS = 60
"""Days of stand-in coverage: the longest declared rolling preset plus its equal prior."""


def _unpublished_coverage(runtime: PresentationRuntime) -> tuple[DateRange, ...]:
    """Clock-anchored stand-in coverage that only shapes an unavailable control.

    No figure is ever read from it: it names the window a rolling preset would resolve to
    today, so a page with nothing published can still serve its designed absence.
    """
    end = runtime.clock().date()
    return (DateRange(end - timedelta(days=_UNPUBLISHED_COVERAGE_DAYS), end),)


def _campaign_preset_unavailable(
    request: _V2ParentPresentationInput,
    runtime: PresentationRuntime,
    roster: PresetArtifactPointerRosterResult | None = None,
) -> dict[str, Any]:
    """Return the existing typed temporal refusal while precompute catches up."""
    policy = _v2_page_schema(CAMPAIGN_LIST_PATH).temporal_policy
    resolution = resolve_temporal(
        TemporalResolutionRequest(
            policy=policy,
            coverage_intervals=_unpublished_coverage(runtime),
            selection=request.temporal_selection,
        )
    )
    if isinstance(resolution, TemporalResolutionUnavailable):
        if resolution.control is None:
            raise DomainError(
                PRESENTATION_UNAVAILABLE, "No reporting period is available for this page."
            )
        control = resolution.control
    else:
        control = resolution.control
    if roster is not None:
        control = _roster_temporal_control(control, policy, roster)
    return _json(
        200,
        _presentation_result_json(
            PresentationWindowUnavailableResult(
                session_id=request.session_id,
                temporal=control,
                reason_code=resolution.reason_code
                if isinstance(resolution, TemporalResolutionUnavailable)
                else TemporalUnavailableReason.PRESET_UNAVAILABLE,
                recovery=TemporalRecovery(
                    no_self_service_reason=TemporalNoSelfServiceReason.WAIT_FOR_SOURCE_COVERAGE
                ),
            )
        ),
    )


def _campaign_preset_temporal(
    request: _V2ParentPresentationInput,
    artifact: CampaignPortfolioArtifact,
    roster: PresetArtifactPointerRosterResult,
) -> TemporalControl:
    """Re-attest a baked period against its declared preset without live coverage reads."""
    policy = _v2_page_schema(CAMPAIGN_LIST_PATH).temporal_policy
    coverage = DateRange(artifact.prior_period.start, artifact.period.end)
    resolution = resolve_temporal(
        TemporalResolutionRequest(
            policy=policy,
            coverage_intervals=(coverage,),
            selection=request.temporal_selection,
        )
    )
    if (
        isinstance(resolution, TemporalResolutionUnavailable)
        or resolution.control.resolved.window != artifact.period
    ):
        raise _malformed_artifact()
    return _roster_temporal_control(resolution.control, policy, roster)


def _v2_retail_health_preset_parent_response(
    input_request: _V2ParentResponseRequest,
) -> dict[str, Any]:
    """Serve a declared Retail Health preset body without catalog or measurement fallback."""
    request = _v2_parent_request(input_request.event, RETAIL_HEALTH_PATH)
    if request.temporal_selection.kind is TemporalSelectionKind.CUSTOM:
        return _retail_health_preset_unavailable(request, input_request.runtime)
    schema = _v2_page_schema(RETAIL_HEALTH_PATH)
    selected = (
        schema.temporal_policy.default_preset
        if request.temporal_selection.kind is TemporalSelectionKind.OMITTED
        else request.temporal_selection.preset_id
    )
    if selected is None:
        raise RuntimeError("retail health preset selection is unavailable.")
    variant = PresetArtifactVariant(selected.value)
    pointers = input_request.runtime.preset_artifact_pointers
    bodies = input_request.runtime.preset_artifact_bodies
    if pointers is None or bodies is None:
        raise RuntimeError("retail health preset artifact runtime is unavailable.")
    roster = pointers.roster(
        PresetArtifactPointerRosterRequest(
            PresetArtifactPage("retail-health"),
            tuple(
                PresetArtifactVariant(candidate.value)
                for candidate in schema.temporal_policy.candidate_presets
            ),
        )
    )
    pointer = roster.pointer_for(variant)
    if pointer is None:
        return _retail_health_preset_unavailable(request, input_request.runtime, roster)
    generation = _served_generation(input_request.runtime, pointer)
    body = bodies.read(pointer)
    _attest_campaign_preset_body(pointer, body)
    try:
        payload = json.loads(body.payload_json)
        artifact = retail_health_artifact_from_payload(payload)
    except TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None
    temporal = _retail_health_preset_temporal(request, artifact, roster)
    custody = _retail_health_custody(input_request.runtime, pointer, body, artifact)
    product_ads: dict[str, object] | None = None
    if request.retail_product_ads and custody is not None:
        product_ads = _retail_product_ads_part(input_request.runtime, pointer, custody[1], artifact)
        if product_ads is None:
            # Baked before render format 12, so no part: Products and ads serves this preset's
            # not-yet-published absence, as a missing pointer is, until the page's next bake
            # (never a fallback to one detail per row, never a 500). Retail health is unchanged.
            unserved = PresetArtifactPointerRosterResult(
                roster.request,
                tuple(
                    None if candidate is not None and candidate.variant == variant else candidate
                    for candidate in roster.pointers
                ),
            )
            return _retail_health_preset_unavailable(request, input_request.runtime, unserved)
    page = _serve_retail_health_preset_page(
        _RetailHealthPresetServeRequest(
            request=request,
            owner=input_request.owner,
            runtime=input_request.runtime,
            temporal=temporal,
            artifact=artifact,
            pointer=pointer,
            custody=None if custody is None else custody[0],
        )
    )
    page = _with_generation(page, generation)
    if request.retail_product_ads:
        # A body with no rows bakes no part; the tab then shows the body's own empty state.
        page = {**page, "product_ads": product_ads}
    return _json(
        200,
        _presentation_result_json(
            PresentationPageResult(session_id=request.session_id, temporal=temporal, page=page)
        ),
    )


def _retail_product_ads_part(  # noqa: PLR0913, PLR0917 -- one pointer- and index-bound read
    runtime: PresentationRuntime,
    pointer: PresetArtifactPointer,
    index: tuple[PresetArtifactSiblingIndexEntry, ...],
    artifact: RetailHealthArtifact,
) -> dict[str, object] | None:
    """Read the Products and ads part a format-12 bake writes beside the children, or None.

    The stage writes it with the children as one closed set checked before any write (ADR-125,
    ADR-145), so an attested index that doesn't list it is a publication baked before render
    format 12. A listed part whose bytes, window or rows disagree with the parent body is the
    malformed-artifact refusal.
    """
    identity = PresetArtifactProductAdsIdentity()
    descriptor = _indexed_descriptor(runtime, pointer, index, identity)
    if descriptor is None:
        return None
    sibling = _read_indexed(runtime, pointer, identity, descriptor)
    try:
        payload = json.loads(sibling.body.payload_json)
        ads = retail_product_ads_from_payload(payload)
        custody = retail_product_ads_custody_from_payload(json.loads(sibling.custody.payload_json))
    except TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None
    if (
        custody.body_digest != sibling.body.digest
        or (ads.period, ads.prior_period) != (artifact.period, artifact.prior_period)
        or tuple(row.child_asin for row in ads.rows)
        != tuple(row.child_asin for row in artifact.rows)
    ):
        raise _malformed_artifact()
    return cast("dict[str, object]", payload)


def _retail_health_preset_unavailable(
    request: _V2ParentPresentationInput,
    runtime: PresentationRuntime,
    roster: PresetArtifactPointerRosterResult | None = None,
) -> dict[str, Any]:
    """Return the existing typed temporal refusal while precompute catches up."""
    policy = _v2_page_schema(RETAIL_HEALTH_PATH).temporal_policy
    resolution = resolve_temporal(
        TemporalResolutionRequest(
            policy=policy,
            coverage_intervals=_unpublished_coverage(runtime),
            selection=request.temporal_selection,
        )
    )
    if isinstance(resolution, TemporalResolutionUnavailable):
        if resolution.control is None:
            raise DomainError(
                PRESENTATION_UNAVAILABLE, "No reporting period is available for this page."
            )
        control = resolution.control
    else:
        control = resolution.control
    if roster is not None:
        control = _roster_temporal_control(control, policy, roster)
    return _json(
        200,
        _presentation_result_json(
            PresentationWindowUnavailableResult(
                session_id=request.session_id,
                temporal=control,
                reason_code=resolution.reason_code
                if isinstance(resolution, TemporalResolutionUnavailable)
                else TemporalUnavailableReason.PRESET_UNAVAILABLE,
                recovery=TemporalRecovery(
                    no_self_service_reason=TemporalNoSelfServiceReason.WAIT_FOR_SOURCE_COVERAGE
                ),
            )
        ),
    )


def _retail_health_preset_temporal(
    request: _V2ParentPresentationInput,
    artifact: RetailHealthArtifact,
    roster: PresetArtifactPointerRosterResult,
) -> TemporalControl:
    """Re-attest a baked period against its declared preset without live coverage reads."""
    policy = _v2_page_schema(RETAIL_HEALTH_PATH).temporal_policy
    coverage = DateRange(artifact.prior_period.start, artifact.period.end)
    resolution = resolve_temporal(
        TemporalResolutionRequest(
            policy=policy,
            coverage_intervals=(coverage,),
            selection=request.temporal_selection,
        )
    )
    if (
        isinstance(resolution, TemporalResolutionUnavailable)
        or resolution.control.resolved.window != artifact.period
    ):
        raise _malformed_artifact()
    return _roster_temporal_control(resolution.control, policy, roster)


_SCORECARD_COVERAGE_METRICS = ("ad_spend", "attributed_revenue_14d")
"""The figures whose coverage gates a Scorecard bake, read again only on a pointer miss."""
_CAMPAIGNS_PAGE = "campaigns"
_RETAIL_HEALTH_PAGE = "retail-health"


def _calendar_label(preset: TemporalPresetId, window: DateRange) -> str | None:
    """Name a calendar preset's window by its server period label, or none if it isn't one."""
    period = period_containing(calendar_preset_grain(preset), window.start)
    return period.label if (period.start, period.end) == (window.start, window.end) else None


def _served_pointer_windows(
    policy: PresentationTemporalPolicy, roster: PresetArtifactPointerRosterResult
) -> tuple[ServedPresetWindow, ...]:
    """Each declared preset's served window, from its pointer (ADR-116 decision 4)."""
    windows: list[ServedPresetWindow] = []
    for preset in policy.candidate_presets:
        pointer = roster.pointer_for(PresetArtifactVariant(preset.value))
        if pointer is None:
            continue
        window = pointer.resolved_window
        label = (
            None
            if window is None or not is_calendar_preset(preset)
            else _calendar_label(preset, window)
        )
        dated = None if label is None and is_calendar_preset(preset) else window
        windows.append(ServedPresetWindow(preset, dated, label))
    return tuple(windows)


def _roster_reads(
    runtime: PresentationRuntime,
    requests: tuple[PresetArtifactPointerRosterRequest, ...],
) -> tuple[PresetArtifactPointerRosterResult | None, ...]:
    """Read independent link-target rosters concurrently; a failed read is None, logged.

    The page never fails for a link (Scorecard LLD "Channel row links"): a target roster
    that can't be read gives its links the "latest {preset}" text instead. The fan-out is
    bounded by the function's configured downstream concurrency and joined by position.
    """
    pointers = runtime.preset_artifact_pointers
    if pointers is None or not requests:
        return tuple(None for _ in requests)
    bound = max(
        1,
        min(
            len(requests),
            SETTINGS.athena_query_batch_max_concurrency,
        ),
    )

    def read(
        request: PresetArtifactPointerRosterRequest,
    ) -> PresetArtifactPointerRosterResult | None:
        try:
            return pointers.roster(request)
        # A link target never fails the page: its links read "latest {preset}" instead.
        # error-propagation: reported once below as a WARNING presentation.link_target failure
        except Exception as error:
            report_operation(
                OperationLog(
                    operation="presentation.link_target",
                    component="presentation_api",
                    phase="pointer_read",
                    outcome="failed",
                    severity=LogSeverity.WARNING,
                    details={"page": request.page.value, "cause": type(error).__name__},
                ),
                LOGGER,
            )
            return None

    with ThreadPoolExecutor(max_workers=bound) as pool:
        return tuple(pool.map(read, requests))


def _target_roster_request(page_id: str) -> PresetArtifactPointerRosterRequest:
    schema = next(item for item in PRESENTATION_PAGE_SCHEMAS.pages if item.page_id == page_id)
    return PresetArtifactPointerRosterRequest(
        PresetArtifactPage(page_id),
        tuple(
            PresetArtifactVariant(candidate.value)
            for candidate in schema.temporal_policy.candidate_presets
        ),
    )


def _target_preset(page_id: str, preset: TemporalPresetId) -> tuple[TemporalPresetId, bool]:
    """Return a link's target preset: the same one when declared, else the target's default.

    The boolean says whether the preset is the target's default, so the URL leaves it out.
    """
    schema = next(item for item in PRESENTATION_PAGE_SCHEMAS.pages if item.page_id == page_id)
    policy = schema.temporal_policy
    chosen = preset if preset in policy.candidate_presets else policy.default_preset
    return chosen, chosen is policy.default_preset


def _link_dates(roster: PresetArtifactPointerRosterResult | None, preset: TemporalPresetId) -> str:
    """Write a link's dates from the target's pointer, or "latest 7 days" without it."""
    pointer = None if roster is None else roster.pointer_for(PresetArtifactVariant(preset.value))
    window = None if pointer is None else pointer.resolved_window
    if window is None:
        report_operation(
            OperationLog(
                operation="presentation.link_target",
                component="presentation_api",
                phase="link_dates",
                outcome="no_window",
                severity=LogSeverity.WARNING,
                details={"preset": preset.value},
            ),
            LOGGER,
        )
        return f"latest {temporal_preset_label_v2(preset)}"
    return range_text(window, with_year=False)


def _href(route: str, preset: TemporalPresetId, default: bool, **params: str) -> str:
    query = {} if default else {"preset": preset.value}
    query.update(params)
    return route if not query else f"{route}?{urlencode(query)}"


def _scorecard_served_payload(  # noqa: PLR0913, PLR0917 -- the body and its two targets
    payload: dict[str, object],
    preset: TemporalPresetId,
    campaigns: PresetArtifactPointerRosterResult | None,
    retail: PresetArtifactPointerRosterResult | None,
) -> dict[str, object]:
    """Add the serve-time link texts to a baked body: one Campaigns and one Retail health read.

    Every channel row links to Campaigns on that channel, the TACoS row to Retail health, and
    every filled channel-mover card on a one-marketplace page gets its served `open_page` step
    (D14). Each text names
    what the target serves now, from its pointer.
    """
    served = json.loads(json.dumps(payload))
    campaign_preset, campaign_default = _target_preset(_CAMPAIGNS_PAGE, preset)
    campaign_dates = _link_dates(campaigns, campaign_preset)
    cards = cast("list[dict[str, Any]]", served["cards"])
    labels: dict[str, str] = {}
    for card in cards:
        for row in cast("list[dict[str, Any]]", card["channel_table"]["rows"]):
            channel = cast("str", row["channel"])
            labels[channel] = cast("str", row["channel_label"])
            row["link"] = {
                "href": _href("/campaigns/", campaign_preset, campaign_default, channel=channel),
                "text": f"Open \u00b7 {campaign_dates}",
                "label": f"Open Campaigns for {row['channel_label']} \u00b7 {campaign_dates}",
            }
    retail_preset, retail_default = _target_preset(_RETAIL_HEALTH_PAGE, preset)
    retail_dates = _link_dates(retail, retail_preset)
    for row in cast("list[dict[str, Any]]", served["tacos"]):
        row["link"] = {
            "href": _href("/retail-health/", retail_preset, retail_default),
            "text": f"Open Retail health \u00b7 {retail_dates}",
            "label": f"Open Retail health \u00b7 {retail_dates}",
        }
    targets = NextStepTargets.from_page_schemas(
        PRESENTATION_PAGE_SCHEMAS,
        channels=CHANNEL_KEYS,
        filters={"campaigns": CAMPAIGN_FILTER_IDS},
    )
    for card in cast("list[dict[str, Any]]", served["insight_cards"]):
        # Campaigns filters by channel, never by marketplace, so with two marketplaces a card's
        # step would open both marketplaces' campaigns: no step rather than an unscoped one
        # (SC9A-GATE-001).
        if card["finding_id"] != "channel-roas-mover" or card["marketplace_text"] is not None:
            continue
        channel = cast("str", card["channel"])
        target = PageTarget(
            _CAMPAIGNS_PAGE,
            preset_id=None if campaign_default else campaign_preset.value,
            filter_id=channel if channel in CAMPAIGN_FILTER_IDS else None,
        )
        targets.check(target)
        card["next_step"] = next_step_payload(
            InsightNextStep(
                InsightNextStepKind.OPEN_PAGE,
                target,
                f"Open Campaigns \u00b7 {labels.get(channel, channel)} \u00b7 {campaign_dates}",
                "The campaigns behind this channel's move are on Campaigns.",
            )
        )
    return served


@dataclass(frozen=True, slots=True)
class _CalendarPresetPage:
    """What a calendar-preset page's miss path reads: its route, gating figures and resolver."""

    path: str
    coverage_metrics: tuple[str, ...]
    calendar_period: Callable[
        [TemporalPresetId, date, tuple[DateRange, ...]], ResolveCalendarPresetResult
    ]
    window_trigger: AbsenceTrigger
    """The catalog row for a rolling preset with no `CURRENT` pointer."""
    period_trigger: AbsenceTrigger
    """The catalog row for a calendar preset whose measured period has no `CURRENT` pointer."""


_SCORECARD_MISS = _CalendarPresetPage(
    SCORECARD_PATH,
    _SCORECARD_COVERAGE_METRICS,
    scorecard_calendar_period,
    AbsenceTrigger.SCORECARD_WINDOW_NOT_PUBLISHED,
    AbsenceTrigger.SCORECARD_PERIOD_NOT_PUBLISHED,
)
_NEW_CUSTOMERS_MISS = _CalendarPresetPage(
    NEW_CUSTOMERS_PATH,
    NEW_CUSTOMERS_COVERAGE_METRICS,
    new_customers_calendar_period,
    AbsenceTrigger.NEW_CUSTOMERS_WINDOW_NOT_PUBLISHED,
    AbsenceTrigger.NEW_CUSTOMERS_PERIOD_NOT_PUBLISHED,
)


def _other_window_ready(pointers: tuple[ServedPresetWindow, ...], preset: TemporalPresetId) -> bool:
    """ABS-ALT: the served control lists a published preset other than the selected one."""
    return any(window.preset != preset for window in pointers)


def _scorecard_window_unavailable(  # noqa: PLR0913, PLR0917 -- the request and its state
    request: _V2ParentPresentationInput,
    runtime: PresentationRuntime,
    preset: TemporalPresetId,
    pointers: tuple[ServedPresetWindow, ...],
    page: _CalendarPresetPage = _SCORECARD_MISS,
) -> dict[str, Any]:
    """Serve a preset with no baked body: no complete period yet, or its bake still pending.

    Only this miss path reads coverage, so the control names the period the preset resolves
    to on the sources now (never a fabricated window, except that a rolling preset with no
    source coverage at all is anchored on `_unpublished_coverage`), and a calendar preset with
    no complete period gets the no-complete-period text and date (ADR-100 point 5). The
    Scorecard and New customers share it, each with its own gating figures and calendar rule.
    """
    policy = _v2_page_schema(page.path).temporal_policy
    windows, catalog = runtime.windows, runtime.metric_catalog
    if windows is None or catalog is None:
        raise RuntimeError("presentation temporal runtime is incomplete.")
    requirements: list[PageWindowRequirement] = []
    for name in page.coverage_metrics:
        metric = catalog.metric(name)
        if metric is None:
            raise RuntimeError(f"presentation metric {name} is unavailable.")
        requirement = metric_coverage_requirement(catalog, metric)
        if requirement not in requirements:
            requirements.append(requirement)
    intervals = windows.source_coverage(SourceCoverageRequest(tuple(requirements))).bounds.intervals
    # Temporal contract rule 3: the page shows the server's text on every unavailable preset,
    # chosen from the absence catalog by its trigger (ADR-139).
    other_ready = _other_window_ready(pointers, preset)
    message: str | None
    expected: date | None = None
    if is_calendar_preset(preset):
        resolved = page.calendar_period(preset, runtime.clock().date(), intervals)
        if resolved.period is None:
            pending = latest_complete_period(calendar_preset_grain(preset), runtime.clock().date())
            served = ServedPeriod(
                preset,
                DateRange(pending.start, pending.end),
                None,
                pending.label,
                available=False,
                unavailable_reason=TemporalUnavailableReason.NO_COMPLETE_PERIOD_MEASURED,
            )
            message, expected = resolved.none_text, resolved.expected_after
        else:
            period = resolved.period
            served = ServedPeriod(
                preset,
                DateRange(period.start, period.end),
                None,
                period.label,
                available=False,
                unavailable_reason=TemporalUnavailableReason.PRESET_UNAVAILABLE,
            )
            message = absence_message(
                page.period_trigger, period_label=period.label, other_window_ready=other_ready
            )
    else:
        selection = TemporalSelection(kind=TemporalSelectionKind.PRESET, preset_id=preset)
        resolution = resolve_temporal(
            TemporalResolutionRequest(
                policy=policy,
                coverage_intervals=intervals,
                selection=selection,
                comparison_requested=True,
            )
        )
        if resolution.control is None:
            # No source coverage at all (a new deployment's empty lake) is anchored as
            # Campaigns' catch-up refusal is, and still serves ABS-SC-1 / ABS-NC-1.
            resolution = resolve_temporal(
                TemporalResolutionRequest(
                    policy=policy,
                    coverage_intervals=_unpublished_coverage(runtime),
                    selection=selection,
                    comparison_requested=True,
                )
            )
        control = resolution.control
        if control is None:
            raise DomainError(
                PRESENTATION_UNAVAILABLE, "No reporting period is available for this page."
            )
        served = ServedPeriod(
            preset,
            control.resolved.window,
            None,
            None,
            available=False,
            unavailable_reason=TemporalUnavailableReason.PRESET_UNAVAILABLE,
        )
        message = absence_message(
            page.window_trigger, period_label=None, other_window_ready=other_ready
        )
    temporal = baked_preset_control_v2(
        BakedControlRequest(policy, request.temporal_selection, served, pointers)
    )
    reason = cast("TemporalUnavailableReason", served.unavailable_reason)
    return _json(
        200,
        _presentation_result_json(
            PresentationWindowUnavailableResult(
                session_id=request.session_id,
                temporal=temporal,
                reason_code=reason,
                recovery=TemporalRecovery(
                    no_self_service_reason=TemporalNoSelfServiceReason.WAIT_FOR_SOURCE_COVERAGE
                ),
                message=message,
                expected_after=expected,
            )
        ),
    )


_AUDIENCES_PART_FIELDS: Final = frozenset({"campaign", "profile", "campaign_index"})
"""The Audiences parts a request may name, at most one (design 01 §5)."""
_AUDIENCES_PART_INVALID: Final = "The Audiences part requested is invalid."
_AUDIENCES_MAX_PAGE: Final = 9_999


@dataclass(frozen=True, slots=True)
class _AudiencesPartRequest:
    """One requested Audiences part: what it is, its sibling identity and its fields."""

    part: AudiencesPart
    identity: PresetArtifactSiblingIdentity
    fields: dict[str, object]
    """The part body's fields that must equal the request (its reference and page)."""


def _audiences_page(value: object, lowest: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(PRESENTATION_REFUSED, _AUDIENCES_PART_INVALID)
    if not lowest <= value <= _AUDIENCES_MAX_PAGE:
        raise ValidationError(PRESENTATION_REFUSED, _AUDIENCES_PART_INVALID)
    return value


def _audiences_identity(
    part: AudiencesPart, page: int, subject: str
) -> PresetArtifactSiblingIdentity:
    """Return the part's sibling identity; a subject the bake never names is the closed 400."""
    try:
        return audiences_part_identity(part, page, subject)
    except ValueError as error:
        raise ValidationError(PRESENTATION_REFUSED, _AUDIENCES_PART_INVALID) from error


def _audiences_part_request(event: dict[str, Any]) -> _AudiencesPartRequest | None:
    """Decode at most one Audiences part selector, closed; None when the parent is asked for."""
    payload = _body(event)
    named = sorted(_AUDIENCES_PART_FIELDS & set(payload))
    if not named:
        return None
    raw = payload[named[0]]
    if len(named) > 1 or not isinstance(raw, dict):
        raise ValidationError(PRESENTATION_REFUSED, _AUDIENCES_PART_INVALID)
    value = cast("dict[str, object]", raw)
    if named[0] == "campaign":
        ref = value.get("ref")
        if set(value) != {"ref", "page"} or not isinstance(ref, str):
            raise ValidationError(PRESENTATION_REFUSED, _AUDIENCES_PART_INVALID)
        page = _audiences_page(value["page"], 0)
        return _AudiencesPartRequest(
            AudiencesPart.CAMPAIGN,
            _audiences_identity(AudiencesPart.CAMPAIGN, page, ref),
            {"campaign_ref": ref, "page": page},
        )
    if named[0] == "profile":
        path = value.get("path")
        if set(value) != {"path", "page"} or not isinstance(path, str):
            raise ValidationError(PRESENTATION_REFUSED, _AUDIENCES_PART_INVALID)
        page = _audiences_page(value["page"], 1)
        return _AudiencesPartRequest(
            AudiencesPart.PROFILE,
            _audiences_identity(AudiencesPart.PROFILE, page, path),
            {"profile_path": path, "page": page},
        )
    if set(value) != {"shard"}:
        raise ValidationError(PRESENTATION_REFUSED, _AUDIENCES_PART_INVALID)
    shard = _audiences_page(value["shard"], 1)
    return _AudiencesPartRequest(
        AudiencesPart.CAMPAIGN_INDEX,
        audiences_part_identity(AudiencesPart.CAMPAIGN_INDEX, shard),
        {"shard": shard},
    )


def _audiences_part_response(  # noqa: PLR0913, PLR0917 -- the request, runtime, parent, part
    request: _V2ParentPresentationInput,
    runtime: PresentationRuntime,
    parent: tuple[PresetArtifactPointer, PresetArtifactBody],
    part: _AudiencesPartRequest,
) -> dict[str, Any]:
    """Serve one baked Audiences part through the parent custody's index, never measuring.

    Existence is the index's: an identity it doesn't list (an unknown reference, a page past a
    campaign's last, an unserved path) is a 400. A listed part whose bytes or fields disagree
    with its descriptor or request is the malformed-artifact refusal (ADR-125 rule 3).
    """
    pointer, body = parent
    custody_store = runtime.preset_artifact_custody
    if custody_store is None or pointer.custody_digest is None:
        raise _malformed_artifact()
    manifest = custody_store.read(pointer)
    _attest_campaign_preset_custody(pointer, manifest)
    header, index = _parent_index(manifest)
    if header.get("body_digest") != body.digest:
        raise _malformed_artifact()
    descriptor = _indexed_descriptor(runtime, pointer, index, part.identity)
    if descriptor is None:
        raise ValidationError(PRESENTATION_REFUSED, _AUDIENCES_PART_INVALID)
    sibling = _read_indexed(runtime, pointer, part.identity, descriptor)
    try:
        payload = require_audiences_part_payload(
            json.loads(sibling.body.payload_json), part.part, pointer.revision.value
        )
        custody = cast("dict[str, object]", json.loads(sibling.custody.payload_json))
    except TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None
    if custody.get("body_digest") != sibling.body.digest or any(
        payload.get(key) != value for key, value in part.fields.items()
    ):
        raise _malformed_artifact()
    return _json(
        200,
        {
            "kind": "audiences_part",
            "session_id": request.session_id,
            "parent_revision": pointer.revision.value,
            "part": payload,
        },
    )


def _audiences_window_unavailable(  # noqa: PLR0913, PLR0917
    request: _V2ParentPresentationInput,
    runtime: PresentationRuntime,
    preset: TemporalPresetId,
    pointers: tuple[ServedPresetWindow, ...],
    *,
    operation: str = AUDIENCES_PATH,
) -> dict[str, Any]:
    """Serve a calendar page's one-preset pending state for a missing body.

    Audiences' own, and Reach's for a preset with no pointer: the pending window is the latest
    complete period of the preset's grain on the request date. The message is the trigger's
    catalog row (ADR-139); only Reach appends ABS-ALT.
    """
    policy = _v2_page_schema(operation).temporal_policy
    pending = latest_complete_period(calendar_preset_grain(preset), runtime.clock().date())
    window, label = DateRange(pending.start, pending.end), pending.label
    served = ServedPeriod(
        preset,
        window,
        None,
        label,
        available=False,
        unavailable_reason=TemporalUnavailableReason.PRESET_UNAVAILABLE,
    )
    temporal = baked_preset_control_v2(
        BakedControlRequest(policy, request.temporal_selection, served, pointers)
    )
    if operation == REACH_PATH:
        trigger = AbsenceTrigger.REACH_PERIOD_NOT_PUBLISHED
    else:
        trigger = AbsenceTrigger.AUDIENCES_MONTH_NOT_PUBLISHED
    message = absence_message(
        trigger,
        period_label=label,
        other_window_ready=operation == REACH_PATH and _other_window_ready(pointers, preset),
    )
    return _json(
        200,
        _presentation_result_json(
            PresentationWindowUnavailableResult(
                session_id=request.session_id,
                temporal=temporal,
                reason_code=TemporalUnavailableReason.PRESET_UNAVAILABLE,
                recovery=TemporalRecovery(
                    no_self_service_reason=TemporalNoSelfServiceReason.WAIT_FOR_SOURCE_COVERAGE
                ),
                message=message,
            )
        ),
    )


def _v2_scorecard_parent_response(input_request: _V2ParentResponseRequest) -> dict[str, Any]:
    """Serve a declared Scorecard preset body, its link texts, and its page context.

    Three independent pointer reads (the Scorecard's own roster, Campaigns' and Retail
    health's) run concurrently; only the Scorecard's can fail the page.
    """
    request = _v2_parent_request(input_request.event, SCORECARD_PATH)
    if request.temporal_selection.kind is TemporalSelectionKind.CUSTOM:
        raise ValidationError(PRESENTATION_REFUSED, "temporal_selection is invalid.")
    schema = _v2_page_schema(SCORECARD_PATH)
    policy = schema.temporal_policy
    preset = (
        policy.default_preset
        if request.temporal_selection.kind is TemporalSelectionKind.OMITTED
        else cast("TemporalPresetId", request.temporal_selection.preset_id)
    )
    if preset not in policy.candidate_presets:
        # Only an omitted selection or a declared preset is accepted (Scorecard LLD §6).
        raise ValidationError(PRESENTATION_REFUSED, "temporal_selection is invalid.")
    runtime = input_request.runtime
    pointers_store, bodies = runtime.preset_artifact_pointers, runtime.preset_artifact_bodies
    if pointers_store is None or bodies is None:
        raise RuntimeError("scorecard preset artifact runtime is unavailable.")
    own_request = _target_roster_request(SCORECARD_PAGE_ID)
    own, campaigns, retail = _roster_reads(
        runtime,
        (
            own_request,
            _target_roster_request(_CAMPAIGNS_PAGE),
            _target_roster_request(_RETAIL_HEALTH_PAGE),
        ),
    )
    if own is None:
        # The page's own pointer read failing is a dependency failure, read once more so the
        # typed error surfaces.
        own = pointers_store.roster(own_request)
    windows = _served_pointer_windows(policy, own)
    pointer = own.pointer_for(PresetArtifactVariant(preset.value))
    if pointer is None:
        return _scorecard_window_unavailable(request, runtime, preset, windows)
    # S6 §2.9 (B4): the pointer case is decided before any body read; an ungranted or
    # refresh-pending generation is refused here, as campaigns and retail-health refuse it.
    generation = _served_generation(runtime, pointer)
    body = bodies.read(pointer)
    _attest_campaign_preset_body(pointer, body)
    try:
        payload = cast("dict[str, object]", json.loads(body.payload_json))
        period = cast("dict[str, str]", payload["period"])
        prior = cast("dict[str, str] | None", payload["prior_period"])
        window = DateRange(date.fromisoformat(period["start"]), date.fromisoformat(period["end"]))
        prior_window = (
            None
            if prior is None
            else DateRange(date.fromisoformat(prior["start"]), date.fromisoformat(prior["end"]))
        )
        period_label = cast("str | None", payload["period_label"])
        if (
            payload["type"] != NETWORK_SCORECARD_ARTIFACT_TYPE
            or payload["version"] != NETWORK_SCORECARD_ARTIFACT_VERSION
            or (pointer.resolved_window is not None and pointer.resolved_window != window)
        ):
            raise ValueError("scorecard body is not the pointer's.")
        served_period = ServedPeriod(preset, window, prior_window, period_label)
    except KeyError, TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None
    temporal = baked_preset_control_v2(
        BakedControlRequest(policy, request.temporal_selection, served_period, windows)
    )
    served = _scorecard_served_payload(payload, preset, campaigns, retail)
    page_record = runtime.prepare_record(
        _PageMintRequest(
            input_request.owner,
            request.session_id,
            PageContextSnapshot(
                response_identity="scorecard-preset",
                visible_window=window,
                reporting_window_status=ReportingWindowStatus(
                    temporal.resolved.reporting_window_status.value
                ),
                comparison_window=prior_window,
                labels=_page_context_labels(SCORECARD_PAGE_ID, temporal),
            ),
        )
    )
    record = PageArtifactRecord(
        reference=page_record.reference,
        artifact=PageArtifact(
            NETWORK_SCORECARD_ARTIFACT_TYPE,
            NETWORK_SCORECARD_ARTIFACT_VERSION,
            canonical_artifact_json(served),
        ),
        verified_owner_id=page_record.verified_owner_id,
        conversation_session_id=page_record.conversation_session_id,
        expires_at=page_record.expires_at,
        temporal_control=temporal,
    )
    outcome = _issue_parent_after_page_record(
        runtime, page_record, ParentCapabilityIssuanceRequest(record, ())
    )
    if not outcome.issued:
        raise RuntimeError("parent capability issuance did not complete.")
    complete = payload["outcome"] == "complete"
    page: dict[str, object] = {
        "actions": [],
        "artifact": served,
        "artifact_id": schema.artifacts[0].artifact_id,
        "artifact_revision": record.reference.value,
        "artifact_type": NETWORK_SCORECARD_ARTIFACT_TYPE,
        "artifact_version": NETWORK_SCORECARD_ARTIFACT_VERSION,
        "kind": "network_scorecard",
        "page_id": SCORECARD_PAGE_ID,
        "route": schema.route,
        # A page-level absence (an empty or insufficient body) offers no starter questions.
        **_page_context_fields(
            page_record,
            _page_starter_questions(SCORECARD_PAGE_ID, temporal) if complete else (),
        ),
    }
    return _json(
        200,
        _presentation_result_json(
            PresentationPageResult(
                session_id=request.session_id,
                temporal=temporal,
                page=_with_generation(page, generation),
            )
        ),
    )


def _v2_new_customers_parent_response(input_request: _V2ParentResponseRequest) -> dict[str, Any]:
    """Serve a declared New customers preset body, re-decided at the serving date.

    One pointer read, the page's own. A baked pending new-buyers section or per-channel table
    turns missing the day after its own expected date with no rebake (ADR-107), and the
    artifact's page context fields are the envelope's, so the two can't disagree (plan D5).
    """
    request = _v2_parent_request(input_request.event, NEW_CUSTOMERS_PATH)
    if request.temporal_selection.kind is TemporalSelectionKind.CUSTOM:
        raise ValidationError(PRESENTATION_REFUSED, "temporal_selection is invalid.")
    schema = _v2_page_schema(NEW_CUSTOMERS_PATH)
    policy = schema.temporal_policy
    preset = (
        policy.default_preset
        if request.temporal_selection.kind is TemporalSelectionKind.OMITTED
        else cast("TemporalPresetId", request.temporal_selection.preset_id)
    )
    if preset not in policy.candidate_presets:
        # Only an omitted selection or a declared preset is accepted (LLD "Time policy").
        raise ValidationError(PRESENTATION_REFUSED, "temporal_selection is invalid.")
    runtime = input_request.runtime
    pointers_store, bodies = runtime.preset_artifact_pointers, runtime.preset_artifact_bodies
    if pointers_store is None or bodies is None:
        raise RuntimeError("new customers preset artifact runtime is unavailable.")
    own_request = _target_roster_request(NEW_CUSTOMERS_PAGE_ID)
    (own,) = _roster_reads(runtime, (own_request,))
    if own is None:
        # Read once more so the pointer store's typed error surfaces.
        own = pointers_store.roster(own_request)
    windows = _served_pointer_windows(policy, own)
    pointer = own.pointer_for(PresetArtifactVariant(preset.value))
    if pointer is None:
        return _scorecard_window_unavailable(request, runtime, preset, windows, _NEW_CUSTOMERS_MISS)
    # S6 §2.9 (B4): the pointer case is decided before any body read; an ungranted or
    # refresh-pending generation is refused here, as campaigns and retail-health refuse it.
    generation = _served_generation(runtime, pointer)
    body = bodies.read(pointer)
    _attest_campaign_preset_body(pointer, body)
    try:
        payload = cast("dict[str, object]", json.loads(body.payload_json))
        period = cast("dict[str, str]", payload["period"])
        prior = cast("dict[str, str] | None", payload["prior_period"])
        window = DateRange(date.fromisoformat(period["start"]), date.fromisoformat(period["end"]))
        prior_window = (
            None
            if prior is None
            else DateRange(date.fromisoformat(prior["start"]), date.fromisoformat(prior["end"]))
        )
        if (
            payload["type"] != NEW_CUSTOMERS_ARTIFACT_TYPE
            or payload["version"] != NEW_CUSTOMERS_ARTIFACT_VERSION
            or payload["preset"] != preset.value
            or (pointer.resolved_window is not None and pointer.resolved_window != window)
        ):
            raise ValueError("new customers body is not the pointer's.")
        # A rolling window reads by its dates; only a calendar period carries a served label.
        period_label = None if prior_window is not None else cast("str", payload["period_label"])
        served_period = ServedPeriod(preset, window, prior_window, period_label)
        served = resolve_new_customers_payload(payload, runtime.clock().date())
    except KeyError, TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None
    temporal = baked_preset_control_v2(
        BakedControlRequest(policy, request.temporal_selection, served_period, windows)
    )
    page_record = runtime.prepare_record(
        _PageMintRequest(
            input_request.owner,
            request.session_id,
            PageContextSnapshot(
                response_identity="new-customers-preset",
                visible_window=window,
                reporting_window_status=ReportingWindowStatus(
                    temporal.resolved.reporting_window_status.value
                ),
                comparison_window=prior_window,
                labels=_page_context_labels(NEW_CUSTOMERS_PAGE_ID, temporal),
            ),
        )
    )
    complete = payload["outcome"] == "complete"
    # A page-level absence (an empty or insufficient body) offers no starter questions.
    context = _page_context_fields(
        page_record,
        _page_starter_questions(NEW_CUSTOMERS_PAGE_ID, temporal) if complete else (),
    )
    served.update(context)
    record = PageArtifactRecord(
        reference=page_record.reference,
        artifact=PageArtifact(
            NEW_CUSTOMERS_ARTIFACT_TYPE,
            NEW_CUSTOMERS_ARTIFACT_VERSION,
            canonical_artifact_json(served),
        ),
        verified_owner_id=page_record.verified_owner_id,
        conversation_session_id=page_record.conversation_session_id,
        expires_at=page_record.expires_at,
        temporal_control=temporal,
    )
    outcome = _issue_parent_after_page_record(
        runtime, page_record, ParentCapabilityIssuanceRequest(record, ())
    )
    if not outcome.issued:
        raise RuntimeError("parent capability issuance did not complete.")
    page: dict[str, object] = {
        "actions": [],
        "artifact": served,
        "artifact_id": schema.artifacts[0].artifact_id,
        "artifact_revision": record.reference.value,
        "artifact_type": NEW_CUSTOMERS_ARTIFACT_TYPE,
        "artifact_version": NEW_CUSTOMERS_ARTIFACT_VERSION,
        "kind": "new_customers",
        "page_id": NEW_CUSTOMERS_PAGE_ID,
        "route": schema.route,
        **context,
    }
    return _json(
        200,
        _presentation_result_json(
            PresentationPageResult(
                session_id=request.session_id,
                temporal=temporal,
                page=_with_generation(page, generation),
            )
        ),
    )


def _v2_audiences_parent_response(  # noqa: PLR0912, PLR0915 -- one strict serve boundary
    input_request: _V2ParentResponseRequest,
) -> dict[str, Any]:
    """Serve the attested Audiences month with request-date paid-period text."""
    request = _v2_parent_request(input_request.event, AUDIENCES_PATH)
    if request.temporal_selection.kind is TemporalSelectionKind.CUSTOM:
        raise ValidationError(PRESENTATION_REFUSED, "temporal_selection is invalid.")
    part = _audiences_part_request(input_request.event)
    schema = _v2_page_schema(AUDIENCES_PATH)
    policy = schema.temporal_policy
    preset = (
        policy.default_preset
        if request.temporal_selection.kind is TemporalSelectionKind.OMITTED
        else cast("TemporalPresetId", request.temporal_selection.preset_id)
    )
    if preset not in policy.candidate_presets:
        raise ValidationError(PRESENTATION_REFUSED, "temporal_selection is invalid.")
    runtime = input_request.runtime
    pointers, bodies = runtime.preset_artifact_pointers, runtime.preset_artifact_bodies
    if pointers is None or bodies is None:
        raise RuntimeError("audiences preset artifact runtime is unavailable.")
    own_request = _target_roster_request(AUDIENCES_PAGE_ID)
    (own,) = _roster_reads(runtime, (own_request,))
    if own is None:
        own = pointers.roster(own_request)
    windows = _served_pointer_windows(policy, own)
    pointer = own.pointer_for(PresetArtifactVariant(preset.value))
    if pointer is None:
        if part is not None:
            raise ValidationError(PRESENTATION_REFUSED, _AUDIENCES_PART_INVALID)
        return _audiences_window_unavailable(request, runtime, preset, windows)
    if pointer.resolved_window is None:
        raise _malformed_artifact()
    # S6 §2.9 (B4): the pointer case is decided before any body read; an ungranted or
    # refresh-pending generation is refused here, as campaigns and retail-health refuse it.
    generation = _served_generation(runtime, pointer)
    body = bodies.read(pointer)
    _attest_campaign_preset_body(pointer, body)
    if part is not None:
        return _audiences_part_response(request, runtime, (pointer, body), part)
    try:
        decoded = cast("object", json.loads(body.payload_json))
    except json.JSONDecodeError:
        raise _malformed_artifact() from None
    if audiences_artifact_superseded(decoded):
        # A body baked before this artifact version is the page's not-yet-published absence,
        # as a missing pointer is, until the first re-bake after the deploy replaces it.
        unserved = tuple(window for window in windows if window.preset is not preset)
        return _audiences_window_unavailable(request, runtime, preset, unserved)
    try:
        payload = require_audiences_artifact_payload(decoded)
        if payload["revision"] != pointer.revision.value:
            raise ValueError("audiences body names another revision than its pointer")
        month = cast("dict[str, str]", payload["month"])
        window = DateRange(date.fromisoformat(month["start"]), date.fromisoformat(month["end"]))
        if pointer.resolved_window != window:
            raise ValueError("audiences body window does not match its pointer")
        served = resolve_audiences_payload(payload, runtime.clock().date())
        served_period = ServedPeriod(preset, window, None, month["label"])
    except KeyError, TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None
    temporal = baked_preset_control_v2(
        BakedControlRequest(policy, request.temporal_selection, served_period, windows)
    )
    page_record = runtime.prepare_record(
        _PageMintRequest(
            input_request.owner,
            request.session_id,
            PageContextSnapshot(
                response_identity="audiences-preset",
                visible_window=window,
                reporting_window_status=ReportingWindowStatus(
                    temporal.resolved.reporting_window_status.value
                ),
                comparison_window=None,
                labels=_page_context_labels(AUDIENCES_PAGE_ID, temporal),
            ),
        )
    )
    for market in cast("list[dict[str, object]]", served["markets"]):
        for view in cast("list[dict[str, object]]", market["views"]):
            for delivery in cast("list[dict[str, object]]", view["deliveries"]):
                for insight in cast("list[dict[str, object]]", delivery["insights"]):
                    insight["artifact_revision"] = page_record.reference.value
    complete = served["outcome"] == "complete"
    context = _page_context_fields(
        page_record,
        _page_starter_questions(AUDIENCES_PAGE_ID, temporal) if complete else (),
    )
    served.update(context)
    try:
        require_audiences_artifact_payload(served, served=True)
    except TypeError, ValueError:
        raise _malformed_artifact() from None
    record = PageArtifactRecord(
        reference=page_record.reference,
        artifact=PageArtifact(
            AUDIENCES_ARTIFACT_TYPE,
            AUDIENCES_ARTIFACT_VERSION,
            canonical_artifact_json(served),
        ),
        verified_owner_id=page_record.verified_owner_id,
        conversation_session_id=page_record.conversation_session_id,
        expires_at=page_record.expires_at,
        temporal_control=temporal,
    )
    outcome = _issue_parent_after_page_record(
        runtime, page_record, ParentCapabilityIssuanceRequest(record, ())
    )
    if not outcome.issued:
        raise RuntimeError("parent capability issuance did not complete.")
    page: dict[str, object] = {
        "actions": [],
        "artifact": served,
        "artifact_id": schema.artifacts[0].artifact_id,
        "artifact_revision": record.reference.value,
        "artifact_type": AUDIENCES_ARTIFACT_TYPE,
        "artifact_version": AUDIENCES_ARTIFACT_VERSION,
        "kind": "audiences",
        "page_id": AUDIENCES_PAGE_ID,
        "route": schema.route,
        **context,
    }
    return _json(
        200,
        _presentation_result_json(
            PresentationPageResult(
                session_id=request.session_id,
                temporal=temporal,
                page=_with_generation(page, generation),
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class _ReachPeriod:
    """One served calendar period: its dates and its server label."""

    window: DateRange
    label: str


def _reach_body(
    pointer: PresetArtifactPointer, body: PresetArtifactBody, preset: TemporalPresetId
) -> tuple[dict[str, object], _ReachPeriod, list[dict[str, object]]]:
    """Decode one attested Reach body: its payload, served period and every insight slot.

    The body must be the pointer's own for the selected preset: its type and version, its
    preset, a period that is exactly one calendar period of the preset's grain under its server
    label, and the pointer's window when the pointer names one. Anything else is malformed.
    """
    try:
        payload = cast("dict[str, object]", json.loads(body.payload_json))
        period = cast("dict[str, str]", payload["period"])
        window = DateRange(date.fromisoformat(period["start"]), date.fromisoformat(period["end"]))
        label = period["label"]
        if (
            payload["type"] != REACH_ARTIFACT_TYPE
            or payload["version"] != REACH_ARTIFACT_VERSION
            or payload["preset"] != preset.value
            or period["grain"] != calendar_preset_grain(preset).value
            or _calendar_label(preset, window) != label
            or (pointer.resolved_window is not None and pointer.resolved_window != window)
        ):
            raise ValueError("reach body is not the pointer's.")
        slots: list[object] = [
            slot
            for market in cast("list[object]", payload["markets"])
            for slot in cast("list[object]", cast("dict[str, object]", market)["insights"])
        ]
        if not all(isinstance(slot, dict) for slot in slots):
            raise TypeError("a reach insight slot is not an object.")
    except KeyError, TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None
    return payload, _ReachPeriod(window, label), cast("list[dict[str, object]]", slots)


def _v2_reach_parent_response(input_request: _V2ParentResponseRequest) -> dict[str, Any]:
    """Serve the attested Reach and funnel body for one declared calendar preset.

    One pointer read, the page's own, then that pointer's body. Reach has no request-date
    text, so the body is served as baked; only the page record's revision and context fields
    are bound in, so every insight slot and the assistant chip name exactly this served page.
    A preset with no pointer serves the one-preset pending state.
    """
    request = _v2_parent_request(input_request.event, REACH_PATH)
    if request.temporal_selection.kind is TemporalSelectionKind.CUSTOM:
        raise ValidationError(PRESENTATION_REFUSED, "temporal_selection is invalid.")
    schema = _v2_page_schema(REACH_PATH)
    policy = schema.temporal_policy
    preset = (
        policy.default_preset
        if request.temporal_selection.kind is TemporalSelectionKind.OMITTED
        else cast("TemporalPresetId", request.temporal_selection.preset_id)
    )
    if preset not in policy.candidate_presets:
        # Only an omitted selection or a declared calendar preset is accepted (LLD §6).
        raise ValidationError(PRESENTATION_REFUSED, "temporal_selection is invalid.")
    runtime = input_request.runtime
    pointers, bodies = runtime.preset_artifact_pointers, runtime.preset_artifact_bodies
    if pointers is None or bodies is None:
        raise RuntimeError("reach preset artifact runtime is unavailable.")
    own_request = _target_roster_request(REACH_PAGE_ID)
    (own,) = _roster_reads(runtime, (own_request,))
    if own is None:
        # Read once more so the pointer store's typed error surfaces.
        own = pointers.roster(own_request)
    windows = _served_pointer_windows(policy, own)
    pointer = own.pointer_for(PresetArtifactVariant(preset.value))
    if pointer is None:
        return _audiences_window_unavailable(
            request, runtime, preset, windows, operation=REACH_PATH
        )
    # S6 §2.9 (B4): the pointer case is decided before any body read; an ungranted or
    # refresh-pending generation is refused here, as campaigns and retail-health refuse it.
    generation = _served_generation(runtime, pointer)
    body = bodies.read(pointer)
    _attest_campaign_preset_body(pointer, body)
    served, period, slots = _reach_body(pointer, body, preset)
    temporal = baked_preset_control_v2(
        BakedControlRequest(
            policy,
            request.temporal_selection,
            ServedPeriod(preset, period.window, None, period.label),
            windows,
        )
    )
    page_record = runtime.prepare_record(
        _PageMintRequest(
            input_request.owner,
            request.session_id,
            PageContextSnapshot(
                response_identity="reach-preset",
                visible_window=period.window,
                reporting_window_status=ReportingWindowStatus(
                    temporal.resolved.reporting_window_status.value
                ),
                comparison_window=None,
                labels=_page_context_labels(REACH_PAGE_ID, temporal),
            ),
        )
    )
    for slot in slots:
        slot["artifact_revision"] = page_record.reference.value
    # Every Reach outcome carries its one starter question: the bake writes it for each, and
    # the browser's strict decoder requires exactly it (`frontend/lib/reach.ts`).
    context = _page_context_fields(page_record, _page_starter_questions(REACH_PAGE_ID, temporal))
    served.update(context)
    # The whole served body must be one the browser's strict decoder accepts before any page
    # record or capability names it: the attested digest proves custody, not the contract.
    try:
        require_reach_artifact_payload(served)
    except TypeError, ValueError:
        raise _malformed_artifact() from None
    record = PageArtifactRecord(
        reference=page_record.reference,
        artifact=PageArtifact(
            REACH_ARTIFACT_TYPE,
            REACH_ARTIFACT_VERSION,
            canonical_artifact_json(served),
        ),
        verified_owner_id=page_record.verified_owner_id,
        conversation_session_id=page_record.conversation_session_id,
        expires_at=page_record.expires_at,
        temporal_control=temporal,
    )
    outcome = _issue_parent_after_page_record(
        runtime, page_record, ParentCapabilityIssuanceRequest(record, ())
    )
    if not outcome.issued:
        raise RuntimeError("parent capability issuance did not complete.")
    page: dict[str, object] = {
        "actions": [],
        "artifact": served,
        "artifact_id": schema.artifacts[0].artifact_id,
        "artifact_revision": record.reference.value,
        "artifact_type": REACH_ARTIFACT_TYPE,
        "artifact_version": REACH_ARTIFACT_VERSION,
        "kind": "reach",
        "page_id": REACH_PAGE_ID,
        "route": schema.route,
        **context,
    }
    return _json(
        200,
        _presentation_result_json(
            PresentationPageResult(
                session_id=request.session_id,
                temporal=temporal,
                page=_with_generation(page, generation),
            )
        ),
    )


def _v2_retention_parent_response(input_request: _V2ParentResponseRequest) -> dict[str, Any]:
    """Serve the attested latest Retention vintage with no request-time fallback.

    Latest only (owner decision 2026-10-14): the page offers no vintage, so any temporal
    selection is refused before a retained-store read.
    """
    request = _v2_parent_request(input_request.event, RETENTION_PATH, ())
    runtime = input_request.runtime
    pointers, bodies = runtime.preset_artifact_pointers, runtime.preset_artifact_bodies
    if pointers is None or bodies is None:
        raise RuntimeError("retention preset artifact runtime is unavailable.")
    variant = PresetArtifactVariant("latest_with_current_publication")
    roster = pointers.roster(
        PresetArtifactPointerRosterRequest(PresetArtifactPage(RETENTION_PAGE_ID), (variant,))
    )
    pointer = roster.pointer_for(variant)
    if pointer is None:
        temporal = VintageControl(resolved=None)
        return _json(
            200,
            _presentation_result_json(
                PresentationWindowUnavailableResult(
                    session_id=request.session_id,
                    temporal=temporal,
                    reason_code=TemporalUnavailableReason.VINTAGE_UNAVAILABLE,
                    recovery=TemporalRecovery(
                        no_self_service_reason=TemporalNoSelfServiceReason.WAIT_FOR_SOURCE_COVERAGE
                    ),
                    message=absence_message(
                        AbsenceTrigger.RETENTION_VINTAGE_NOT_PUBLISHED,
                        period_label=None,
                        other_window_ready=False,
                    ),
                )
            ),
        )
    if pointer.resolved_window is None:
        raise _malformed_artifact()
    offered_month = pointer.resolved_window.end.isoformat()[:7]
    # S6 §2.9 (B4): the pointer case is decided before any body read; an ungranted or
    # refresh-pending generation is refused here, as campaigns and retail-health refuse it.
    generation = _served_generation(runtime, pointer)
    body = bodies.read(pointer)
    _attest_campaign_preset_body(pointer, body)
    try:
        payload = require_retention_artifact_payload(json.loads(body.payload_json))
        vintage = cast("dict[str, object]", payload["vintage"])
        month = cast("str", vintage["as_of_month"])
        label = cast("str", vintage["label"])
        window = DateRange(
            date.fromisoformat(cast("str", vintage["start"])),
            date.fromisoformat(cast("str", vintage["end"])),
        )
        if month != offered_month or window != pointer.resolved_window:
            raise ValueError("retention body window does not match its pointer")
    except KeyError, TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None
    temporal = VintageControl(
        resolved=VintageResolved(as_of_month=month, period_label=label, window=window)
    )
    page_record = runtime.prepare_record(
        _PageMintRequest(
            input_request.owner,
            request.session_id,
            PageContextSnapshot(
                response_identity="retention-vintage",
                visible_window=window,
                reporting_window_status=ReportingWindowStatus("preferred_default"),
                comparison_window=None,
                labels=_page_context_labels(RETENTION_PAGE_ID, temporal),
            ),
        )
    )
    complete = payload["outcome"] == "complete"
    for raw_market in cast("list[dict[str, object]]", payload["markets"]):
        for insight in cast("list[dict[str, object]]", raw_market["insights"]):
            insight["artifact_revision"] = page_record.reference.value
    context = _page_context_fields(
        page_record,
        _page_starter_questions(RETENTION_PAGE_ID, temporal) if complete else (),
    )
    payload.update(context)
    record = PageArtifactRecord(
        reference=page_record.reference,
        artifact=PageArtifact(
            RETENTION_ARTIFACT_TYPE,
            RETENTION_ARTIFACT_VERSION,
            canonical_artifact_json(payload),
        ),
        verified_owner_id=page_record.verified_owner_id,
        conversation_session_id=page_record.conversation_session_id,
        expires_at=page_record.expires_at,
        temporal_control=temporal,
    )
    outcome = _issue_parent_after_page_record(
        runtime, page_record, ParentCapabilityIssuanceRequest(record, ())
    )
    if not outcome.issued:
        raise RuntimeError("parent capability issuance did not complete.")
    schema = _v2_page_schema(RETENTION_PATH)
    page: dict[str, object] = {
        "actions": [],
        "artifact": payload,
        "artifact_id": schema.artifacts[0].artifact_id,
        "artifact_revision": record.reference.value,
        "artifact_type": RETENTION_ARTIFACT_TYPE,
        "artifact_version": RETENTION_ARTIFACT_VERSION,
        "kind": "retention",
        "page_id": RETENTION_PAGE_ID,
        "route": schema.route,
        **context,
    }
    return _json(
        200,
        _presentation_result_json(
            PresentationPageResult(
                session_id=request.session_id,
                temporal=temporal,
                page=_with_generation(page, generation),
            )
        ),
    )


def _retail_health_sorted_rows(
    artifact: RetailHealthArtifact, sort: RetailHealthSort
) -> RetailHealthArtifact:
    """Reapply one declared server sort over the displayed rows; callers never reorder."""

    def _current(row: RetailHealthProductRow) -> Decimal:
        comparison = next(item for item in row.comparisons if item.metric == sort.field.value)
        return Decimal(comparison.current.value)

    ordered = tuple(
        sorted(
            artifact.rows,
            key=lambda row: (
                -_current(row) if sort.direction == "desc" else _current(row),
                row.child_asin,
            ),
        )
    )
    return replace(artifact, rows=ordered)


def _retail_health_custody(  # noqa: PLR0913, PLR0917 -- one pointer-bound read
    runtime: PresentationRuntime,
    pointer: PresetArtifactPointer,
    body: PresetArtifactBody,
    artifact: RetailHealthArtifact,
) -> tuple[RetailHealthCustodyManifest, tuple[PresetArtifactSiblingIndexEntry, ...]] | None:
    """Read the parent custody a format-8 bake writes (ADR-125), with its sibling index.

    A body with no rows has no custody and serves no product-detail capability. A body with
    rows always bakes custody, so one without it is malformed and refuses (ADR-125 decision 3):
    it never falls back to rows that open nothing. The format-7 body without custody retired in
    the integration change (owner revision, 2026-10-08).
    """
    if pointer.custody_digest is None:
        if not artifact.rows:
            return None
        raise _malformed_artifact()
    store = runtime.preset_artifact_custody
    if store is None:
        raise _malformed_artifact()
    manifest_object = store.read(pointer)
    _attest_campaign_preset_custody(pointer, manifest_object)
    header, index = _parent_index(manifest_object)
    try:
        manifest = retail_health_custody_manifest_from_payload(header)
    except TypeError, ValueError:
        raise _malformed_artifact() from None
    if manifest.body_digest != body.digest or len(manifest.products) != len(artifact.rows):
        raise _malformed_artifact()
    return manifest, index


RETAIL_PRODUCT_STEP_REASON = "It changed shipped revenue more than any other product."
"""The served ``open_product_detail`` step's reason; the movers ranking is the server's own."""


def _retail_product_capabilities(
    input_request: _RetailHealthPresetServeRequest,
    served: RetailHealthArtifact,
    record: PageContextRecord,
) -> tuple[tuple[str, ProductSelectionCapabilityRecord], ...]:
    """Mint one product-detail capability per served row, keyed by the row's child ASIN.

    The capability names the canonical product and, in its catalog identity, the exact bake
    the row came from, so detail reads its child through this revision's custody only.
    """
    custody = input_request.custody
    if custody is None:
        return ()
    by_asin = {
        row.child_asin: product
        for row, product in zip(input_request.artifact.rows, custody.products, strict=True)
    }
    pointer = input_request.pointer
    minted: list[tuple[str, ProductSelectionCapabilityRecord]] = []
    for row in served.rows:
        product = by_asin[row.child_asin]
        minted.append(
            (
                row.child_asin,
                ProductSelectionCapabilityRecord(
                    reference=_v2_campaign_capability_reference(
                        input_request.runtime, input_request.temporal
                    ),
                    parent_artifact_reference=record.reference,
                    parent_artifact_type=RETAIL_HEALTH_ARTIFACT_TYPE,
                    parent_artifact_version=RETAIL_HEALTH_ARTIFACT_VERSION,
                    verified_owner_id=record.verified_owner_id,
                    conversation_session_id=record.conversation_session_id,
                    expires_at=record.expires_at,
                    product=product.product,
                    safe_signature=SafeProductDisplaySignature(
                        row.title,
                        EffectiveNameStatus.AVAILABLE,
                        product.product.marketplace.value,
                    ),
                    catalog_identity=_retail_product_catalog_identity(pointer, product.product_ref),
                ),
            )
        )
    return tuple(minted)


def _retail_product_catalog_identity(pointer: PresetArtifactPointer, product_ref: str) -> str:
    return "/".join(
        (pointer.page.value, pointer.variant.value, pointer.revision.value, product_ref)
    )


def _with_movers_product_step(  # noqa: PLR0913, PLR0917 -- served, custody, refs, baked
    served: RetailHealthArtifact,
    custody: RetailHealthCustodyManifest | None,
    references: Mapping[str, str],
    baked: RetailHealthArtifact,
) -> RetailHealthArtifact:
    """Give the filled movers card its ``open_product_detail`` step (ADR-125 decision 5)."""
    if custody is None or custody.movers_product_ref is None:
        return served
    index = next(
        position
        for position, product in enumerate(custody.products)
        if product.product_ref == custody.movers_product_ref
    )
    row = baked.rows[index]
    reference = references.get(row.child_asin)
    if reference is None:
        return served
    step = InsightNextStep(
        InsightNextStepKind.OPEN_PRODUCT_DETAIL,
        ProductRefTarget(reference),
        f"Open {row.title}",
        RETAIL_PRODUCT_STEP_REASON,
    )
    return replace(
        served,
        insights=tuple(
            replace(slot, next_step=step)
            if slot.id is InsightSlotId.RETAIL_MOVERS and slot.state is InsightSlotState.FILLED
            else slot
            for slot in served.insights
        ),
    )


def _serve_retail_health_preset_page(
    input_request: _RetailHealthPresetServeRequest,
) -> dict[str, object]:
    """Persist the served parent and mint one product-detail capability per served row."""
    request = input_request.request
    runtime = input_request.runtime
    temporal = input_request.temporal
    artifact = input_request.artifact
    if runtime.parent_capability_issuance is None:
        raise RuntimeError("parent capability issuance storage is unavailable.")
    served = (
        artifact
        if request.retail_sort is None
        else _retail_health_sorted_rows(artifact, request.retail_sort)
    )
    # ADR-070's serve path fills insight slots beside the baked body: cached reads by the
    # baked artifact's revision, with a bounded first-fill and durable continuation on a miss.
    served = _with_retail_insights(
        _RetailInsightInput(baked=artifact, served=served, runtime=runtime)
    )
    page_record = runtime.prepare_record(
        _PageMintRequest(
            input_request.owner,
            request.session_id,
            PageContextSnapshot(
                response_identity="retail-health-preset",
                visible_window=artifact.period,
                reporting_window_status=ReportingWindowStatus(
                    temporal.resolved.reporting_window_status.value
                ),
                comparison_window=artifact.prior_period,
                labels=_page_context_labels("retail-health", temporal),
            ),
        )
    )
    # Each row capability binds to the parent record this response writes (its reference, type,
    # version, owner, session and expiry), so the rows carry their references in its bytes.
    capabilities = _retail_product_capabilities(input_request, served, page_record)
    references = {asin: record.reference.value for asin, record in capabilities}
    served = replace(
        served,
        rows=tuple(
            replace(row, selection_reference=references.get(row.child_asin)) for row in served.rows
        ),
    )
    served = _with_movers_product_step(served, input_request.custody, references, artifact)
    payload = retail_health_artifact_payload(served)
    page_artifact = PageArtifact(
        RETAIL_HEALTH_ARTIFACT_TYPE,
        RETAIL_HEALTH_ARTIFACT_VERSION,
        canonical_artifact_json(payload),
    )
    record = PageArtifactRecord(
        reference=page_record.reference,
        artifact=page_artifact,
        verified_owner_id=page_record.verified_owner_id,
        conversation_session_id=page_record.conversation_session_id,
        expires_at=page_record.expires_at,
        temporal_control=temporal,
    )
    outcome = _issue_parent_after_page_record(
        runtime,
        page_record,
        ParentCapabilityIssuanceRequest(record, tuple(item for _, item in capabilities)),
    )
    if outcome.outcome is ParentCapabilityIssuanceOutcome.CAPACITY_REFUSED:
        raise DomainError(
            PRESENTATION_UNAVAILABLE, "The page has too many available row selections."
        )
    if not outcome.issued:
        raise RuntimeError("parent capability issuance did not complete.")
    projected: dict[str, object] = {
        "actions": [],
        "artifact": payload,
        "artifact_revision": record.reference.value,
        "artifact_type": RETAIL_HEALTH_ARTIFACT_TYPE,
        "artifact_version": RETAIL_HEALTH_ARTIFACT_VERSION,
        "kind": served.kind,
        "page_id": "retail-health",
    }
    return {
        **_v2_page_projection(RETAIL_HEALTH_PATH, projected, temporal),
        # A page-level absence (an empty or insufficient body) offers no starter questions.
        **_page_context_fields(
            page_record,
            _page_starter_questions("retail-health", temporal)
            if served.outcome is RetailHealthOutcome.COMPLETE
            else (),
        ),
    }


@dataclass(frozen=True, slots=True)
class _RetailProductRequest:
    """The closed product-detail grammar: a row capability and the parent's own preset."""

    session_id: str
    selection_reference: PageContextReference
    temporal_selection: TemporalSelection


@dataclass(frozen=True, slots=True)
class _RedeemedRetailProduct:
    """One redeemed row capability and the baked child its parent's custody attests."""

    request: _RetailProductRequest
    capability: ServerValidatedProductSelectionCapability
    parent: ServerValidatedPageArtifact
    temporal: TemporalControl
    pointer: PresetArtifactPointer
    product_ref: str
    detail: RetailProductDetailArtifact


def _product_selection_unavailable() -> ValidationError:
    return ValidationError(PRESENTATION_REFUSED, "The requested product is not available.")


def _retail_product_request(
    event: dict[str, Any], *, extra: frozenset[str] = frozenset()
) -> tuple[_RetailProductRequest, dict[str, object]]:
    """Decode the product grammar exactly; ``extra`` names a route's own added fields."""
    payload = _body(event)
    required = frozenset({"session_id", "selection_reference", "temporal_selection"}) | extra
    if frozenset(payload) != required:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "The presentation request fields are invalid.",
            context={"outcome": "invalid_fields", "n": len(frozenset(payload) ^ required)},
        )
    session_id = _text(payload["session_id"], "session_id")
    if len(session_id) > MAX_SESSION_ID_LENGTH:
        raise ValidationError(PRESENTATION_REFUSED, "session_id is too long.")
    reference = payload["selection_reference"]
    if not isinstance(reference, str) or not reference.strip():
        raise _product_selection_unavailable()
    try:
        selection_reference = PageContextReference(reference)
    except ValueError:
        raise _product_selection_unavailable() from None
    return (
        _RetailProductRequest(
            session_id, selection_reference, _temporal_selection(payload["temporal_selection"])
        ),
        payload,
    )


def _served_preset(temporal: TemporalControl) -> TemporalPresetId | None:
    """Name the preset a served parent resolved, the declared default included."""
    selection = temporal.resolved.selection
    if selection.preset_id is not None:
        return selection.preset_id
    if selection.kind is TemporalResolvedSelectionKind.DEFAULT:
        return _v2_page_schema(RETAIL_HEALTH_PATH).temporal_policy.default_preset
    return None


def _refresh_required() -> DomainError:
    return DomainError(
        PRESENTATION_REFRESH_REQUIRED, "Refresh the parent page before selecting again."
    )


def _redeem_retail_product(
    request: _RetailProductRequest, owner: str, runtime: PresentationRuntime
) -> _RedeemedRetailProduct:
    """Redeem one row capability and read its child only through the parent's custody.

    ADR-125 decision 3: the request names the parent's own window, or it is refused; the
    child is read through the CURRENT pointer of the revision the row was served from, and a
    missing child or a digest mismatch is the malformed-artifact refusal, never a fallback.
    """
    capabilities = runtime.product_capabilities
    artifacts = runtime.page_artifacts
    pointers = runtime.preset_artifact_pointers
    bodies = runtime.preset_artifact_bodies
    custody_store = runtime.preset_artifact_custody
    if (
        capabilities is None
        or artifacts is None
        or pointers is None
        or bodies is None
        or custody_store is None
    ):
        raise RuntimeError("retail product detail runtime is unavailable.")
    now = runtime.clock()
    result = capabilities.read_product_capability(
        ProductSelectionCapabilityReadRequest(
            request.selection_reference, owner, request.session_id, now
        )
    )
    capability = result.capability
    if result.outcome is not ResolutionOutcome.RESOLVED or capability is None:
        raise _refresh_required()
    parent_result = artifacts.read_artifact(
        PageArtifactReadRequest(
            capability.parent_artifact_reference, owner, request.session_id, now
        )
    )
    parent = parent_result.artifact
    if (
        parent_result.outcome is not ResolutionOutcome.RESOLVED
        or parent is None
        or not isinstance(parent.temporal_control, TemporalControl)
        or parent.artifact.artifact_type != RETAIL_HEALTH_ARTIFACT_TYPE
    ):
        raise _refresh_required()
    temporal = parent.temporal_control
    preset = _served_preset(temporal)
    if (
        request.temporal_selection.kind is not TemporalSelectionKind.PRESET
        or preset is None
        or request.temporal_selection.preset_id is not preset
    ):
        # The product page always shows the parent's window, so its figures add up to the
        # Retail health tiles; a request for any other window is refused.
        raise ValidationError(
            PRESENTATION_REFUSED,
            "Product detail shows the Retail health window. Choose the window on the page.",
        )
    parts = capability.catalog_identity.split("/")
    if (
        len(parts) != 4  # noqa: PLR2004 -- page/variant/revision/product
        or parts[0] != "retail-health"
        or parts[1] != preset.value
        or parts[3] != product_ref_for(capability.product)
    ):
        raise _malformed_artifact()
    page, variant = PresetArtifactPage(parts[0]), PresetArtifactVariant(parts[1])
    pointer = pointers.current(page, variant)
    if pointer is None or pointer.revision.value != parts[2]:
        # The bake advanced since the row was served: the page re-serves its parent.
        raise _refresh_required()
    if pointer.custody_digest is None:
        raise _malformed_artifact()
    manifest_object = custody_store.read(pointer)
    _attest_campaign_preset_custody(pointer, manifest_object)
    header, index = _parent_index(manifest_object)
    try:
        manifest = retail_health_custody_manifest_from_payload(header)
    except TypeError, ValueError:
        raise _malformed_artifact() from None
    product_ref = parts[3]
    if (
        manifest.body_digest != pointer.body_digest
        or manifest.product(product_ref) != capability.product
    ):
        raise _malformed_artifact()
    # The child's one digest home is the index (ADR-145): a ref it doesn't list is malformed.
    child = _indexed_sibling(
        runtime, pointer, index, PresetArtifactProductChildIdentity(product_ref)
    )
    child_body, child_custody = child.body, child.custody
    try:
        private = retail_product_child_custody_from_payload(json.loads(child_custody.payload_json))
        detail = retail_product_detail_from_payload(json.loads(child_body.payload_json))
    except TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None
    if (
        private.product_ref != product_ref
        or private.product != capability.product
        or private.body_digest != child_body.digest
        or detail.period != temporal.resolved.window
        or detail.outcome is not RetailProductDetailOutcome.SELECTED
    ):
        raise _malformed_artifact()
    return _RedeemedRetailProduct(
        request, capability, parent, temporal, pointer, product_ref, detail
    )


def _retail_health_product_response(
    event: dict[str, Any], owner: str, runtime: PresentationRuntime
) -> dict[str, Any]:
    """Serve one baked product-detail child in place on Retail health (ADR-125)."""
    request, _ = _retail_product_request(event)
    redeemed = _redeem_retail_product(request, owner, runtime)
    if runtime.page_artifacts is None:
        raise RuntimeError("page artifact storage is unavailable.")
    detail = redeemed.detail
    temporal = redeemed.temporal
    title = detail.product.title
    page_record = runtime.prepare_record(
        _PageMintRequest(
            owner=owner,
            session_id=request.session_id,
            snapshot=PageContextSnapshot(
                response_identity="retail-health-product",
                visible_window=detail.period,
                reporting_window_status=ReportingWindowStatus(
                    temporal.resolved.reporting_window_status.value
                ),
                comparison_window=detail.prior_period,
                product=redeemed.capability.product,
                marketplace=redeemed.capability.product.marketplace,
                labels=_page_context_labels("retail-health", temporal, product=title),
            ),
            maximum_expiry=redeemed.capability.expires_at,
        )
    )
    runtime.page_context.put(page_record)
    payload = retail_product_detail_payload(detail)
    runtime.page_artifacts.put_artifact(
        PageArtifactRecord(
            reference=page_record.reference,
            artifact=PageArtifact(
                RETAIL_PRODUCT_DETAIL_ARTIFACT_TYPE,
                RETAIL_PRODUCT_DETAIL_ARTIFACT_VERSION,
                canonical_artifact_json(payload),
            ),
            verified_owner_id=page_record.verified_owner_id,
            conversation_session_id=page_record.conversation_session_id,
            expires_at=page_record.expires_at,
            parent_reference=redeemed.parent.reference,
            temporal_control=temporal,
        )
    )
    schema = _v2_page_schema(RETAIL_HEALTH_PATH)
    declaration = next(
        (item for item in schema.artifacts if item.artifact_id == "retail_product_detail"),
        None,
    )
    if (
        declaration is None
        or declaration.artifact_type != RETAIL_PRODUCT_DETAIL_ARTIFACT_TYPE
        or declaration.version != RETAIL_PRODUCT_DETAIL_ARTIFACT_VERSION
    ):
        raise _malformed_artifact()
    page: dict[str, object] = {
        "actions": [],
        "artifact": payload,
        "artifact_id": declaration.artifact_id,
        "artifact_revision": page_record.reference.value,
        "artifact_type": declaration.artifact_type,
        "artifact_version": declaration.version,
        "kind": "retail_product_detail",
        "page_id": schema.page_id,
        "parent_revision": redeemed.parent.reference.value,
        "route": schema.route,
        **_page_context_fields(
            page_record,
            # A title too long for a question falls back to the ASIN the page shows, never cut.
            _page_starter_questions(
                "retail-health",
                temporal,
                detail=True,
                product=title,
                product_identifier=detail.product.child_asin,
            ),
        ),
    }
    return _json(
        200,
        _presentation_result_json(
            PresentationPageResult(session_id=request.session_id, temporal=temporal, page=page)
        ),
    )


def _retail_product_metric_focus_response(
    event: dict[str, Any], owner: str, runtime: PresentationRuntime
) -> dict[str, Any]:
    """Serve one product tile's focus from the same baked child, via the same capability."""
    request, payload = _retail_product_request(event, extra=frozenset({"metric"}))
    metric = _text(payload["metric"], "metric")
    if metric not in RETAIL_FOCUS_METRICS:
        raise ValidationError(PRESENTATION_REFUSED, "The requested metric focus is unavailable.")
    redeemed = _redeem_retail_product(request, owner, runtime)
    try:
        ingredients = retail_product_focus_ingredients(redeemed.detail, metric)
    except ValueError:
        raise ValidationError(
            PRESENTATION_REFUSED, "The requested metric focus is unavailable for this view."
        ) from None
    identity = _focus_identity(metric, ingredients)
    slot = _focus_insight_slot(
        _FocusSlotRequest(
            runtime=runtime,
            ingredients=ingredients,
            identity=identity,
            # The subject names the product by its server-only reference, never its key.
            subject=f"retail-health:product:{redeemed.product_ref}:metric-focus:{metric}",
            artifact_revision=redeemed.pointer.revision.value,
            slot_id=InsightSlotId.RETAIL_METRIC_FOCUS,
            basis=OperandBasis.RETAIL_WINDOW,
        )
    )
    return _json(
        200,
        metric_focus_payload(
            build_metric_focus(
                BuildMetricFocusRequest(
                    ingredients,
                    identity,
                    slot,
                    _focus_neighbor_labels(ingredients.neighbors),
                    CHANGE_SEMANTICS,
                )
            )
        ),
    )


def _reissue_campaign_preset_page(input_request: _CampaignPresetServeRequest) -> dict[str, object]:
    """Mint fresh opaque row references and persist the served parent artifact atomically."""
    request = input_request.request
    owner = input_request.owner
    runtime = input_request.runtime
    temporal = input_request.temporal
    artifact = input_request.artifact
    if runtime.parent_capability_issuance is None:
        raise RuntimeError("parent capability issuance storage is unavailable.")
    page_record = runtime.prepare_record(
        _PageMintRequest(
            owner,
            request.session_id,
            PageContextSnapshot(
                response_identity="campaign-portfolio-preset",
                visible_window=artifact.period,
                reporting_window_status=ReportingWindowStatus(
                    temporal.resolved.reporting_window_status.value
                ),
                comparison_window=artifact.prior_period,
                # The chosen option names the chip and the redeemed context (ADR-120, ADR-127).
                labels=_page_context_labels(
                    "campaigns",
                    temporal,
                    channel=None
                    if artifact.channel_filter is None
                    else artifact.channel_filter.option.label,
                ),
            ),
        )
    )
    sort = request.campaign_sort or CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc")
    pagination = artifact.pagination
    if (
        artifact.sort != sort
        or (
            artifact.outcome is CampaignPortfolioOutcome.COMPLETE
            and (pagination is None or pagination.segment_index != request.campaign_segment)
        )
        or (
            artifact.outcome is not CampaignPortfolioOutcome.COMPLETE
            and request.campaign_segment != 0
        )
    ):
        raise _malformed_artifact()
    segment = artifact
    custody_by_signature: dict[SafeCampaignDisplaySignature, CampaignCustodyRow] = (
        {}
        if input_request.custody_rows is None
        else {custody.safe_signature: custody for custody in input_request.custody_rows}
    )
    rows = tuple(
        replace(
            row,
            capability=CampaignSelectionCapability(
                _v2_campaign_capability_reference(runtime, temporal).value,
                page_record.expires_at,
            ),
        )
        for row in segment.rows
    )
    custody_by_reference: dict[str, CampaignCustodyRow] = (
        {}
        if not custody_by_signature
        else {row.capability.value: custody_by_signature[row.signature] for row in rows}
    )
    served = replace(segment, rows=rows)
    # ADR-070's serve path fills insight slots beside the baked body: cached reads by the
    # artifact's revision, with a bounded first-fill and durable continuation on a miss.
    # A cached campaign step opens only a row this response minted a capability for.
    served = _with_portfolio_insights(
        _PortfolioInsightInput(
            artifact=served,
            runtime=runtime,
            campaign_refs={
                campaign_step_key(custody.campaign): reference
                for reference, custody in custody_by_reference.items()
            },
        )
    )
    page_artifact = PageArtifact(
        CAMPAIGN_PORTFOLIO_ARTIFACT_TYPE,
        CAMPAIGN_PORTFOLIO_ARTIFACT_VERSION,
        canonical_artifact_json(campaign_portfolio_artifact_payload(served)),
    )
    record = PageArtifactRecord(
        reference=page_record.reference,
        artifact=page_artifact,
        verified_owner_id=page_record.verified_owner_id,
        conversation_session_id=page_record.conversation_session_id,
        expires_at=page_record.expires_at,
        temporal_control=temporal,
    )
    # A pre-custody bake carries no canonical selectors, so it mints no child capabilities;
    # a custody-backed bake reissues one redeemable record per served row.
    capabilities = (
        ()
        if not custody_by_reference
        else tuple(
            CampaignSelectionCapabilityRecord(
                reference=PageContextReference(row.capability.value),
                parent_artifact_reference=record.reference,
                parent_artifact_type=record.artifact.artifact_type,
                parent_artifact_version=record.artifact.artifact_version,
                verified_owner_id=record.verified_owner_id,
                conversation_session_id=record.conversation_session_id,
                expires_at=record.expires_at,
                campaign=custody_by_reference[row.capability.value].campaign,
                channel=custody_by_reference[row.capability.value].channel,
                marketplace=custody_by_reference[row.capability.value].marketplace,
                safe_signature=custody_by_reference[row.capability.value].safe_signature,
                reporting_window_status=ReportingWindowStatus(
                    temporal.resolved.reporting_window_status.value
                ),
            )
            for row in served.rows
        )
    )
    outcome = _issue_parent_after_page_record(
        runtime, page_record, ParentCapabilityIssuanceRequest(record, capabilities)
    )
    if outcome.outcome is ParentCapabilityIssuanceOutcome.CAPACITY_REFUSED:
        raise DomainError(
            PRESENTATION_UNAVAILABLE, "The page has too many available row selections."
        )
    if not outcome.issued:
        raise RuntimeError("parent capability issuance did not complete.")
    projected = _analytical_page_json(
        _AnalyticalPageExecutionResult(
            schema=_campaign_schema(runtime),
            page=None,
            snapshot=page_record.snapshot,
            artifact=page_artifact,
        ),
        runtime.dimension_labels,
        record,
    )
    return {
        **_v2_page_projection(CAMPAIGN_LIST_PATH, projected, temporal),
        # Per-channel return has one home, the Scorecard (ia §7.2 item 4).
        "scorecard_step": (
            _scorecard_step(temporal, None, input_request.runtime) if served.multi_channel else None
        ),
        # A page-level absence (an empty or insufficient body) offers no starter questions.
        **_page_context_fields(
            page_record,
            _page_starter_questions("campaigns", temporal)
            if served.outcome is CampaignPortfolioOutcome.COMPLETE
            else (),
        ),
    }


SCORECARD_PAGE_ID = "scorecard"
SCORECARD_CHANNEL_SECTION = "channel-table"
_SCORECARD_STEP_REASON = "Return on ad spend per channel is on the Scorecard."


def _scorecard_step(
    temporal: TemporalControl, channel: str | None, runtime: PresentationRuntime
) -> dict[str, object] | None:
    """Build the `open_page` step to the Scorecard, or none while it isn't a routed tab.

    The Scorecard ships in its own slice; until `presentation/navigation.yaml` routes it, no
    step is served, so no chip opens a page that doesn't exist (ADR-122 decision 5). The step
    keeps the served preset when the Scorecard declares it and highlights the campaign's
    channel row from detail (ia §7.2 item 13).
    """
    targets = NextStepTargets.from_page_schemas(
        PRESENTATION_PAGE_SCHEMAS,
        channels=CHANNEL_KEYS,
        filters={"campaigns": CAMPAIGN_FILTER_IDS},
    )
    page = targets.pages.get(SCORECARD_PAGE_ID)
    if page is None:
        return None
    preset = _served_preset(temporal)
    preset_id = None if preset is None or preset.value not in page.presets else preset.value
    target = PageTarget(
        SCORECARD_PAGE_ID,
        preset_id=preset_id,
        section_id=SCORECARD_CHANNEL_SECTION
        if SCORECARD_CHANNEL_SECTION in page.sections
        else None,
        highlight_channel=channel,
    )
    targets.check(target)
    # The label names what the Scorecard serves now, from its pointer (ADR-116 decision 4).
    scorecard_preset = (
        TemporalPresetId(preset_id)
        if preset_id is not None
        else _v2_page_schema(SCORECARD_PATH).temporal_policy.default_preset
    )
    (roster,) = _roster_reads(runtime, (_target_roster_request(SCORECARD_PAGE_ID),))
    label = f"Open Scorecard \u00b7 {_link_dates(roster, scorecard_preset)}"
    return next_step_payload(
        InsightNextStep(InsightNextStepKind.OPEN_PAGE, target, label, _SCORECARD_STEP_REASON)
    )


def _v2_page_schema(operation: str):
    """Return the sole canonical V2 page declaration for an API operation."""
    page_id = V2_PARENT_PAGE_IDS[operation]
    schema = next(
        (item for item in PRESENTATION_PAGE_SCHEMAS.pages if item.page_id == page_id),
        None,
    )
    if schema is None:
        raise RuntimeError("versioned presentation page schema is unavailable.")
    return schema


def _v2_page_projection(
    operation: str,
    legacy_page: object,
    temporal: TemporalControl,
) -> dict[str, object]:
    """Replace legacy page identity/actions only after exact artifact and period binding."""
    if not isinstance(legacy_page, dict):
        raise _malformed_artifact()
    page = cast("dict[str, object]", legacy_page)
    artifact = page.get("artifact")
    if not isinstance(artifact, dict):
        raise _malformed_artifact()
    payload = cast("dict[str, object]", artifact)
    artifact_type = page.get("artifact_type")
    artifact_version = page.get("artifact_version")
    artifact_revision = page.get("artifact_revision")
    kind = page.get("kind")
    if (
        not isinstance(artifact_type, str)
        or isinstance(artifact_version, bool)
        or not isinstance(artifact_version, int)
        or not isinstance(artifact_revision, str)
        or not artifact_revision.strip()
        or not isinstance(kind, str)
        or not kind.strip()
    ):
        raise _malformed_artifact()
    schema = _v2_page_schema(operation)
    declaration = next(
        (
            item
            for item in schema.artifacts
            if item.artifact_type == artifact_type and item.version == artifact_version
        ),
        None,
    )
    if declaration is None:
        raise _malformed_artifact()
    outcome = _v2_artifact_outcome(artifact_type, payload)
    if outcome not in declaration.outcomes:
        raise _malformed_artifact()
    if _v2_artifact_period(artifact_type, payload) != _window_json(temporal.resolved.window):
        raise _malformed_artifact()
    projected: dict[str, object] = {
        "actions": [
            {
                "action_id": action.action_id,
                "target_route": _v2_page_route(action.target_page),
            }
            for action in schema.actions
        ],
        "artifact": payload,
        "artifact_id": declaration.artifact_id,
        "artifact_revision": artifact_revision,
        "artifact_type": artifact_type,
        "artifact_version": artifact_version,
        "kind": kind,
        "page_id": schema.page_id,
        "route": schema.route,
    }
    parent_revision = page.get("parent_revision")
    if parent_revision is not None:
        if not isinstance(parent_revision, str) or not parent_revision.strip():
            raise _malformed_artifact()
        projected["parent_revision"] = parent_revision
    return projected


def _issue_parent_after_page_record(
    runtime: PresentationRuntime,
    page_record: PageContextRecord,
    request: ParentCapabilityIssuanceRequest,
) -> ParentCapabilityIssuanceResult:
    """ADR-120 decision 1: write the `page#` record, then the unchanged parent transaction.

    The record is a separate conditional put before `TransactWriteItems`, as the v1 path does,
    so the transaction and its 51-item cap don't change. A failed put is the existing
    `PAGE_CONTEXT_UNWRITABLE` error; a transaction that fails after it leaves an orphan
    `page#` item whose reference never reaches the browser and which expires on TTL.
    """
    issuance = runtime.parent_capability_issuance
    if issuance is None:
        raise RuntimeError("parent capability issuance storage is unavailable.")
    runtime.page_context.put(page_record)
    return issuance.issue_parent_capabilities(request)


def _routed_page_schema(page_id: str):
    """Return the routed tab declaration that owns a page's assistant text."""
    schema = next(
        (item for item in PRESENTATION_PAGE_SCHEMAS.pages if item.page_id == page_id),
        None,
    )
    if schema is None or schema.navigation is None or schema.assistant is None:
        raise RuntimeError("the routed page declaration for page context is unavailable.")
    return schema


def _page_context_labels(  # noqa: PLR0913 - keyword-only display parts
    page_id: str,
    temporal: TemporalControl | VintageControl,
    *,
    channel: str | None = None,
    campaign: str | None = None,
    product: str | None = None,
) -> PageContextLabels:
    """Snapshot v2 labels from governed display names (ADR-120 decision 2)."""
    schema = _routed_page_schema(page_id)
    navigation = cast("PresentationPageNavigation", schema.navigation)
    page = navigation.tab_label
    resolved = temporal.resolved
    if resolved is None:
        raise RuntimeError("a served page requires a resolved temporal control.")
    # Calendar periods and cohort vintages both read the server's label, never browser dates.
    window = resolved.period_label or window_label(resolved.window)
    if isinstance(temporal, VintageControl):
        preset = None
    else:
        selection = temporal.resolved.selection
        preset = selection.preset_id or (
            schema.temporal_policy.default_preset
            if selection.kind is TemporalResolvedSelectionKind.DEFAULT
            else None
        )
    return PageContextLabels(
        page_id=page_id,
        page_context_label=page_context_label(
            *(part for part in (page, channel, campaign, product, window) if part is not None)
        ),
        display_labels=PageContextDisplayLabels(
            page=page, window=window, channel=channel, campaign=campaign, product=product
        ),
        preset_id=None if preset is None else preset.value,
    )


CAMPAIGN_NAME_UNAVAILABLE = "Name unavailable"
"""The campaign page's own words for a campaign with no displayable name."""


def _campaign_display_name(signature: SafeCampaignDisplaySignature) -> str:
    return signature.effective_name or CAMPAIGN_NAME_UNAVAILABLE


def _page_starter_questions(  # noqa: PLR0913 - keyword-only display parts
    page_id: str,
    temporal: TemporalControl | VintageControl,
    *,
    detail: bool = False,
    campaign: str | None = None,
    product: str | None = None,
    product_identifier: str | None = None,
) -> tuple[str, ...]:
    """Write a served page's starter questions from its governed templates (decision 3)."""
    assistant = cast("StarterQuestionTemplates", _routed_page_schema(page_id).assistant)
    resolved = temporal.resolved
    if resolved is None:
        raise RuntimeError("served starter questions require a resolved temporal control.")
    window = resolved.window
    prior = (
        window_label(_preceding_period(window))
        if isinstance(temporal, TemporalControl)
        and temporal.comparison.status is TemporalComparisonStatus.AVAILABLE
        else None
    )
    return render_starter_questions(
        assistant.detail if detail else assistant.page,
        # A calendar period reads by its server label and asks no comparison question.
        window=resolved.period_label or window_label(window),
        prior_window=prior,
        campaign=campaign,
        product=product,
        product_identifier=product_identifier,
    )


def _page_context_fields(
    record: PageContextRecord, starter_questions: tuple[str, ...]
) -> dict[str, object]:
    """Return the response fields that name the served page to the assistant (decision 3)."""
    labels = record.snapshot.labels
    if labels is None:
        raise RuntimeError("a served page's context record has no labels.")
    return {
        "page_context_expires_at": record.expires_at.astimezone(UTC).isoformat(),
        "page_context_label": labels.page_context_label,
        "starter_questions": list(starter_questions),
    }


def _v2_page_route(page_id: str) -> str:
    """Resolve a generated V2 action target without accepting handwritten routes."""
    schema = next(
        (item for item in PRESENTATION_PAGE_SCHEMAS.pages if item.page_id == page_id),
        None,
    )
    if schema is None:
        raise RuntimeError("versioned presentation action target is unavailable.")
    return schema.route


def _v2_artifact_outcome(artifact_type: str, payload: dict[str, object]) -> str:
    """Read one declared outcome field from the closed parent artifact shape."""
    if artifact_type == CHANNEL_ARTIFACT_TYPE:
        current = payload.get("current")
        if not isinstance(current, dict):
            raise _malformed_artifact()
        value = cast("dict[str, object]", current).get("outcome")
    elif artifact_type in {MARKETPLACE_COMPARISON_ARTIFACT_TYPE, DATA_HEALTH_ARTIFACT_TYPE}:
        value = payload.get("state")
    else:
        value = payload.get("outcome")
    if not isinstance(value, str) or not value.strip():
        raise _malformed_artifact()
    return value


def _v2_artifact_period(artifact_type: str, payload: dict[str, object]) -> dict[str, str]:
    """Read the current parent period that must exactly equal temporal resolution."""
    if artifact_type == CHANNEL_ARTIFACT_TYPE:
        current = payload.get("current")
        if not isinstance(current, dict):
            raise _malformed_artifact()
        value = cast("dict[str, object]", current).get("period")
    elif artifact_type == MARKETPLACE_COMPARISON_ARTIFACT_TYPE:
        value = payload.get("current_window")
    else:
        value = payload.get("period")
    if not isinstance(value, dict):
        raise _malformed_artifact()
    period = cast("dict[str, object]", value)
    if set(period) != {"start", "end"} or any(
        not isinstance(item, str) for item in period.values()
    ):
        raise _malformed_artifact()
    return cast("dict[str, str]", period)


def _v2_channel_row_capabilities(
    operation: str,
    legacy_body: dict[str, object],
) -> list[dict[str, str]]:
    """Retain only the documented safe Channel parent capabilities in V2."""
    if operation != CHANNEL_PERFORMANCE_PATH:
        return []
    raw_capabilities = legacy_body.get("row_capabilities", [])
    if not isinstance(raw_capabilities, list):
        raise _malformed_artifact()
    raw_capability_list = cast("list[object]", raw_capabilities)
    capabilities: list[dict[str, str]] = []
    for raw in raw_capability_list:
        if not isinstance(raw, dict):
            raise _malformed_artifact()
        capability = cast("dict[str, object]", raw)
        if set(capability) != {"expires_at", "safe_channel_label", "selection_reference"}:
            raise _malformed_artifact()
        if any(not isinstance(value, str) or not value.strip() for value in capability.values()):
            raise _malformed_artifact()
        capabilities.append(cast("dict[str, str]", capability))
    return capabilities


def _v2_parent_request(
    event: dict[str, Any],
    operation: str,
    offered_vintages: tuple[str, ...] | None = (),
) -> _V2ParentPresentationInput:
    """Decode only V2 temporal intent and the closed Campaign Portfolio sort grammar."""
    if operation not in V2_PARENT_PAGE_IDS:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "The versioned presentation operation is not available.",
        )
    payload = _body(event)
    fields = set(payload)
    allowed = {"session_id", "temporal_selection"}
    if operation in {CAMPAIGN_LIST_PATH, RETAIL_HEALTH_PATH}:
        allowed.add("sort")
    if operation == RETAIL_HEALTH_PATH:
        allowed.add("product_ads")
    if operation == CAMPAIGN_LIST_PATH:
        allowed.update({"segment", "filter_id", "roster_shard"})
    if operation == AUDIENCES_PATH:
        allowed.update(_AUDIENCES_PART_FIELDS)
    if fields - allowed or "session_id" not in fields:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "The presentation request fields are invalid.",
            context={"outcome": "invalid_fields", "n": len(fields - allowed)},
        )
    # A roster shard is a part of the served body, never a page: it names no sort or segment.
    if "roster_shard" in fields and fields & {"sort", "segment"}:
        raise ValidationError(PRESENTATION_REFUSED, "The campaign roster shard is invalid.")
    # Products and ads names its part with exactly `true`; any other value is refused, and so is
    # a sort, because the part's rows are in the baked order the tab shows.
    if "product_ads" in fields and (payload["product_ads"] is not True or "sort" in fields):
        raise ValidationError(PRESENTATION_REFUSED, "The products and ads request is invalid.")
    session_id = _text(payload["session_id"], "session_id")
    if len(session_id) > MAX_SESSION_ID_LENGTH:
        raise ValidationError(PRESENTATION_REFUSED, "session_id is too long.")
    return _V2ParentPresentationInput(
        session_id=session_id,
        temporal_selection=_page_temporal_selection(
            V2_PARENT_PAGE_IDS[operation],
            payload.get("temporal_selection"),
            offered_vintages,
        ),
        campaign_sort=(
            _campaign_portfolio_sort(payload.get("sort"))
            if operation == CAMPAIGN_LIST_PATH
            else None
        ),
        campaign_segment=(
            _campaign_portfolio_segment_index(payload.get("segment"))
            if operation == CAMPAIGN_LIST_PATH
            else 0
        ),
        retail_sort=(
            _retail_health_sort(payload.get("sort")) if operation == RETAIL_HEALTH_PATH else None
        ),
        filter_id=_campaign_filter_id(payload.get("filter_id")),
        campaign_roster_shard=(
            _campaign_roster_shard(payload["roster_shard"]) if "roster_shard" in fields else None
        ),
        retail_product_ads="product_ads" in fields,
    )


def _campaign_roster_shard(value: object) -> int:
    """Accept one bounded roster shard index (ADR-145); a bool or negative value is refused."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_CAMPAIGN_SEGMENT_INDEX
    ):
        raise ValidationError(PRESENTATION_REFUSED, "The campaign roster shard is invalid.")
    return value


_GOVERNED_FILTER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _campaign_filter_id(value: object) -> str:
    """Accept one governed channel filter id; omitted means All channels (ADR-127 decision 6).

    Whether the preset offers it is checked against the baked filter set it names.
    """
    if value is None:
        return ALL_CHANNELS_FILTER_ID
    if not isinstance(value, str) or not _GOVERNED_FILTER_ID.fullmatch(value):
        raise ValidationError(PRESENTATION_REFUSED, "The channel filter is invalid.")
    return value


@dataclass(frozen=True, slots=True)
class _CampaignFilterParent:
    """One channel filter's attested parent body and, when baked with one, its custody."""

    artifact: CampaignPortfolioArtifact
    body_digest: str
    manifest: CampaignCustodyManifest | None = None
    index: tuple[PresetArtifactSiblingIndexEntry, ...] = ()
    """The pointer's sibling index, through which this filter's segments are read (ADR-145)."""
    filter_id: str = ALL_CHANNELS_FILTER_ID
    """The filter whose segments these are: each segment's identity names it (ADR-127)."""


def _campaign_filter_parent(  # noqa: PLR0913, PLR0917 -- one pointer-bound sibling read
    runtime: PresentationRuntime,
    pointer: PresetArtifactPointer,
    body: PresetArtifactBody,
    artifact: CampaignPortfolioArtifact,
    filter_id: str,
) -> _CampaignFilterParent:
    """Read the filter sibling the pointer's All-channels custody indexes (ADR-127, ADR-145).

    An id the preset doesn't offer is refused with its own code; a sibling the body offers but the
    index doesn't bind, or whose bytes don't match, is the malformed-artifact refusal, never
    a fallback to All channels.
    """
    if filter_id == ALL_CHANNELS_FILTER_ID:
        return _CampaignFilterParent(artifact, body.digest)
    offered = artifact.channel_filter
    if offered is None or filter_id not in offered.offered:
        raise ValidationError(
            PRESENTATION_FILTER_NOT_OFFERED, "The channel filter isn't offered for this window."
        )
    custody_store = runtime.preset_artifact_custody
    if pointer.custody_digest is None or custody_store is None:
        raise _malformed_artifact()
    manifest_object = custody_store.read(pointer)
    _attest_campaign_preset_custody(pointer, manifest_object)
    header, index = _parent_index(manifest_object)
    try:
        parent_manifest = campaign_custody_manifest_from_payload(header)
    except TypeError, ValueError:
        raise _malformed_artifact() from None
    if parent_manifest.body_digest != body.digest:
        raise _malformed_artifact()
    sibling = _indexed_sibling(runtime, pointer, index, PresetArtifactFilterIdentity(filter_id))
    try:
        filtered = campaign_portfolio_artifact_from_payload(json.loads(sibling.body.payload_json))
        manifest = campaign_custody_manifest_from_payload(json.loads(sibling.custody.payload_json))
    except TypeError, ValueError, json.JSONDecodeError:
        raise _malformed_artifact() from None
    if (
        filtered.channel_filter is None
        or filtered.channel_filter.selected != filter_id
        or filtered.channel_filter.options != offered.options
        or (filtered.period, filtered.prior_period) != (artifact.period, artifact.prior_period)
        or manifest.body_digest != sibling.body.digest
        or not custody_rows_align(manifest, tuple(row.signature for row in filtered.rows))
    ):
        raise _malformed_artifact()
    return _CampaignFilterParent(filtered, sibling.body.digest, manifest, index, filter_id)


def _campaign_portfolio_sort(value: object) -> CampaignPortfolioSort | None:
    """Accept only a declared two-field server sort request; callers cannot supply a comparator."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError(PRESENTATION_REFUSED, "The campaign sort is invalid.")
    raw = cast("dict[str, object]", value)
    if set(raw) != {"field", "direction"}:
        raise ValidationError(PRESENTATION_REFUSED, "The campaign sort is invalid.")
    try:
        return CampaignPortfolioSort(
            CampaignPortfolioSortField(_text(raw["field"], "sort.field")),
            _text(raw["direction"], "sort.direction"),
        )
    except ValueError:
        raise ValidationError(PRESENTATION_REFUSED, "The campaign sort is invalid.") from None


# A transport sanity cap on the segment index; the artifact's own segment count is the
# real bound, applied at selection (ADR-071).
MAX_CAMPAIGN_SEGMENT_INDEX = 10_000


def _campaign_portfolio_segment_index(value: object) -> int:
    """Accept only a bounded non-negative segment index into a baked ordering (ADR-071)."""
    if value is None:
        return 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_CAMPAIGN_SEGMENT_INDEX
    ):
        raise ValidationError(PRESENTATION_REFUSED, "The campaign segment is invalid.")
    return value


def _retail_health_sort(value: object) -> RetailHealthSort | None:
    """Accept only the artifact-declared server sort grammar; callers cannot supply a comparator."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError(PRESENTATION_REFUSED, "The retail sort is invalid.")
    raw = cast("dict[str, object]", value)
    if set(raw) != {"field", "direction"}:
        raise ValidationError(PRESENTATION_REFUSED, "The retail sort is invalid.")
    try:
        return RetailHealthSort(
            RetailHealthSortField(_text(raw["field"], "sort.field")),
            _text(raw["direction"], "sort.direction"),
        )
    except ValueError:
        raise ValidationError(PRESENTATION_REFUSED, "The retail sort is invalid.") from None


def _page_temporal_selection(
    page_id: str,
    value: object,
    offered_vintages: tuple[str, ...] | None = (),
) -> TemporalSelection:
    """Decode a page's selection in the grammar its entry declares (ADR-100 point 8).

    A page whose entry needs version 2 (a calendar preset) decodes `TemporalSelection.v2`,
    refusing a calendar preset it doesn't declare. Vintage pages decode only against the
    pointer-attested roster supplied by their handler. Every other page keeps version 1, which
    refuses any calendar preset, so Campaigns and Retail health decode exactly as before.
    """
    page = next((item for item in PRESENTATION_PAGE_SCHEMAS.pages if item.page_id == page_id), None)
    if page is None or not page.needs_version_2:
        return _temporal_selection(value)
    if value is not None and not isinstance(value, dict):
        raise ValidationError(PRESENTATION_REFUSED, "temporal_selection is invalid.")
    try:
        return decode_selection_v2(cast("dict[str, object] | None", value), page, offered_vintages)
    except TemporalSelectionRefusalError as error:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "temporal_selection is invalid.",
            context={"outcome": error.reason.value},
        ) from None


def _temporal_selection(value: object) -> TemporalSelection:
    """Strictly decode one optional TemporalSelection.v1 without date normalization."""
    if value is None:
        return TemporalSelection.omitted()
    if not isinstance(value, dict):
        raise ValidationError(PRESENTATION_REFUSED, "temporal_selection is invalid.")
    selection = cast("dict[str, object]", value)
    kind = selection.get("kind")
    try:
        if kind == TemporalSelectionKind.PRESET.value and set(selection) == {"kind", "preset_id"}:
            return TemporalSelection(
                kind=TemporalSelectionKind.PRESET,
                preset_id=TemporalPresetId(_text(selection["preset_id"], "preset_id")),
            )
        if kind == TemporalSelectionKind.CUSTOM.value and set(selection) == {
            "kind",
            "start",
            "end",
        }:
            return TemporalSelection(
                kind=TemporalSelectionKind.CUSTOM,
                start=date.fromisoformat(_text(selection["start"], "temporal_selection.start")),
                end=date.fromisoformat(_text(selection["end"], "temporal_selection.end")),
            )
    except ValueError:
        pass
    raise ValidationError(PRESENTATION_REFUSED, "temporal_selection is invalid.")


def _resolve_v2_parent_request(
    operation: str,
    request: _V2ParentPresentationInput,
    runtime: PresentationRuntime,
) -> _ResolvedV2ParentPresentationInput | PresentationWindowUnavailableResult:
    """Use the V2 policy and exact coverage before creating a legacy reducer input."""
    windows = runtime.windows
    catalog = runtime.metric_catalog
    schemas = runtime.schemas
    if windows is None or catalog is None or schemas is None:
        raise RuntimeError("presentation temporal runtime is incomplete.")
    page_id = V2_PARENT_PAGE_IDS[operation]
    v2_schema = next(
        (item for item in PRESENTATION_PAGE_SCHEMAS.pages if item.page_id == page_id),
        None,
    )
    legacy_schema = (
        _campaign_schema(runtime)
        if operation == CAMPAIGN_LIST_PATH
        else _schema_for_operation(runtime, operation)
    )
    if v2_schema is None:
        raise RuntimeError("versioned presentation page schema is unavailable.")
    coverage = windows.source_coverage(
        SourceCoverageRequest(_source_coverage_requirements(legacy_schema, catalog))
    )
    resolution = resolve_temporal(
        TemporalResolutionRequest(
            policy=v2_schema.temporal_policy,
            coverage_intervals=coverage.bounds.intervals,
            selection=request.temporal_selection,
        )
    )
    if isinstance(resolution, TemporalResolutionUnavailable):
        if resolution.control is None:
            raise DomainError(
                PRESENTATION_UNAVAILABLE,
                "No reporting period is available for this page.",
            )
        return PresentationWindowUnavailableResult(
            session_id=request.session_id,
            temporal=resolution.control,
            reason_code=resolution.reason_code,
            recovery=resolution.recovery,
        )
    return _ResolvedV2ParentPresentationInput(
        request=_legacy_request_for_control(
            operation,
            request.session_id,
            resolution.control,
            request.campaign_sort,
        ),
        temporal=resolution.control,
    )


def _source_coverage_requirements(
    schema: AnalyticalPageSchema,
    catalog: Catalog,
) -> tuple[PageWindowRequirement, ...]:
    """Derive exact source requirements from existing governed metric declarations."""
    requirements: list[PageWindowRequirement] = []
    for slot in schema.metrics:
        metric = catalog.metric(slot.name)
        if metric is None:
            raise RuntimeError(f"presentation metric {slot.name} is unavailable.")
        for variant in slot.variants or (None,):
            selectors = () if variant is None else tuple(sorted(variant.selectors))
            requirement = metric_coverage_requirement(catalog, metric, selectors)
            if requirement not in requirements:
                requirements.append(requirement)
    return tuple(requirements)


def _legacy_request_for_control(  # noqa: PLR0913, PLR0917
    operation: str,
    session_id: str,
    control: TemporalControl,
    campaign_sort: CampaignPortfolioSort | None = None,
) -> _ListPresentationInput | _AnalyticalPresentationInput:
    """Adapt the resolved server window to the established reducer-only input shape."""
    comparison = (
        _preceding_period(control.resolved.window)
        if control.comparison.status is TemporalComparisonStatus.AVAILABLE
        else None
    )
    status = ReportingWindowStatus(control.resolved.reporting_window_status.value)
    if operation == CAMPAIGN_LIST_PATH:
        return _ListPresentationInput(
            session_id,
            control.resolved.window,
            status,
            comparison,
            campaign_sort,
        )
    return _AnalyticalPresentationInput(
        session_id=session_id,
        visible_window=control.resolved.window,
        reporting_window_status=status,
        comparison_window=comparison,
    )


def _response_body(response: dict[str, Any]) -> dict[str, object]:
    body = response.get("body")
    if not isinstance(body, str):
        raise RuntimeError("presentation response body is unavailable.")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise RuntimeError("presentation response body is malformed.")
    return cast("dict[str, object]", parsed)


def _presentation_result_json(
    result: PresentationPageResult | PresentationWindowUnavailableResult,
) -> dict[str, object]:
    body: dict[str, object] = {
        "envelope_version": result.envelope_version,
        "kind": result.kind,
        "session_id": result.session_id,
        "temporal": _temporal_control_json(result.temporal),
    }
    if isinstance(result, PresentationPageResult):
        body["page"] = result.page
    else:
        body["reason_code"] = result.reason_code.value
        body["recovery"] = {
            "action_id": result.recovery.action_id,
            "no_self_service_reason": (
                None
                if result.recovery.no_self_service_reason is None
                else result.recovery.no_self_service_reason.value
            ),
        }
        if result.temporal.version == TEMPORAL_V2_VERSION:
            # ADR-100 point 8: the no-complete-period result names when the first is expected.
            body["message"] = result.message
            body["expected_after"] = (
                None if result.expected_after is None else result.expected_after.isoformat()
            )
    return body


TEMPORAL_V2_VERSION = 2


def _temporal_control_json(control: TemporalControl | VintageControl) -> dict[str, object]:
    if isinstance(control, VintageControl):
        resolved = control.resolved
        return {
            "mode": control.mode,
            "resolved": None
            if resolved is None
            else {
                "as_of_month": resolved.as_of_month,
                "label_text": resolved.label_text,
                "period_label": resolved.period_label,
                "window": _window_json(resolved.window),
            },
            "version": control.version,
        }
    body = _temporal_control_v1_json(control)
    if control.version != TEMPORAL_V2_VERSION:
        return body
    # Version 2 (ADR-100 point 8): each preset names the window it serves now, a calendar
    # period carries its server label, and an unavailable preset carries its own text.
    resolved = cast("dict[str, object]", body["resolved"])
    resolved["period_label"] = control.resolved.period_label
    resolved["as_of_month"] = None
    comparison = control.comparison
    if comparison.policy is TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD:
        # A calendar comparison names its prior period and carries no reason code, exactly as
        # `calendar_resolution_fields` encodes it and the browser's v2 decoder reads it.
        body["comparison"] = {
            "policy": comparison.policy.value,
            "status": comparison.status.value,
            "prior_period_label": comparison.prior_period_label,
        }
    body["presets"] = [
        {
            "id": preset.preset_id.value,
            "label": preset.label,
            "availability": preset.availability.value,
            "reason_code": None if preset.reason_code is None else preset.reason_code.value,
            "reason_text": preset.reason_text,
            "window": None if preset.window is None else _window_json(preset.window),
            "period_label": preset.period_label,
        }
        for preset in control.presets
    ]
    return body


def _temporal_control_v1_json(control: TemporalControl) -> dict[str, object]:
    bounds = control.custom.bounds
    return {
        "version": control.version,
        "mode": control.mode.value,
        "resolved": {
            "window": _window_json(control.resolved.window),
            "reporting_window_status": control.resolved.reporting_window_status.value,
            "selection": {
                "kind": control.resolved.selection.kind.value,
                "preset_id": (
                    None
                    if control.resolved.selection.preset_id is None
                    else control.resolved.selection.preset_id.value
                ),
            },
            "display_grain": control.resolved.display_grain.value,
        },
        "comparison": {
            "policy": control.comparison.policy.value,
            "status": control.comparison.status.value,
            "reason_code": (
                None
                if control.comparison.reason_code is None
                else control.comparison.reason_code.value
            ),
        },
        "presets": [
            {
                "id": preset.preset_id.value,
                "label": preset.label,
                "availability": preset.availability.value,
                "reason_code": (None if preset.reason_code is None else preset.reason_code.value),
            }
            for preset in control.presets
        ],
        "custom": {
            "availability": control.custom.availability.value,
            "bounds": (
                None
                if bounds is None
                else {
                    "earliest_start": bounds.earliest_start.isoformat(),
                    "latest_end": bounds.latest_end.isoformat(),
                    "min_duration_days": bounds.min_duration_days,
                    "max_duration_days": bounds.max_duration_days,
                }
            ),
            "reason_code": (
                None if control.custom.reason_code is None else control.custom.reason_code.value
            ),
        },
    }


def _presentation_counts(body: dict[str, object]) -> dict[str, int]:
    """Return safe structural counts without retaining any page data or identifiers."""
    page = body.get("page")
    page_data = cast("dict[str, object]", page) if isinstance(page, dict) else {}
    artifact = page_data.get("artifact")
    artifact_data = cast("dict[str, object]", artifact) if isinstance(artifact, dict) else {}

    def count(value: object) -> int:
        if not isinstance(value, list):
            return 0
        return len(cast("list[object]", value))

    return {
        "artifact_row_count": count(artifact_data.get("rows")),
        "check_count": count(artifact_data.get("checks")),
        "choice_count": count(artifact_data.get("choices")),
        "measurement_count": count(page_data.get("measurements")),
        "row_capability_count": count(body.get("row_capabilities")),
    }


def _presentation_success_details(response: dict[str, Any]) -> dict[str, object]:
    """Classify a completed response using closed V1/V2 transport vocabulary only."""
    try:
        body = _response_body(response)
    except RuntimeError, TypeError, ValueError:
        body = {}
    if body.get("envelope_version") == PRESENTATION_PAGE_SCHEMAS.presentation_result_version:
        result_kind = body.get("kind")
        if result_kind not in {"page", "window_unavailable"}:
            result_kind = "unknown"
        representation = "v2"
    else:
        result_kind = "page" if isinstance(body.get("page"), dict) else "unknown"
        representation = "v1"
    status = response.get("statusCode", 200)
    return {
        **_presentation_counts(body),
        "representation": representation,
        "result_kind": result_kind,
        "status": status if isinstance(status, int) and not isinstance(status, bool) else 200,
    }


def _presentation_error_outcome(error: BaseException) -> tuple[str, str]:
    """Map closed error taxonomy to a lifecycle outcome without copying diagnostics."""
    if isinstance(error, IdentityError):
        return "refused", "authorization"
    if isinstance(error, AdlyticsError) and error.error_class in {
        ErrorClass.DOMAIN,
        ErrorClass.VALIDATION,
    }:
        return "refused", "validation"
    return "failed", "error"


def _serving_refusal_reason(error: AdlyticsError) -> str | None:
    """Return the closed grant-serving refusal reason for the span (ADR-139), else None."""
    reason = error.context.get("reason")
    if reason in {UNGRANTED_GENERATION_REASON, SERVING_REFRESH_PENDING_REASON}:
        return cast("str", reason)
    return None


@dataclass(frozen=True, slots=True)
class _PresentationTelemetry:
    """Request-scoped lifecycle state with no request or response content."""

    report: Report
    started_at: float


@dataclass(frozen=True, slots=True)
class _PresentationLifecycleEvent:
    """One closed safe lifecycle projection."""

    phase: str
    outcome: str
    severity: LogSeverity
    details: dict[str, object]


def _presentation_lifecycle(
    telemetry: _PresentationTelemetry,
    event: _PresentationLifecycleEvent,
) -> None:
    """Emit one duration-bound presentation lifecycle record with allowlisted details."""
    duration_ms = max(0, min(int((monotonic() - telemetry.started_at) * 1_000), 3_600_000))
    report_operation(
        OperationLog(
            operation=telemetry.report.operation,
            component="presentation_api",
            phase=event.phase,
            outcome=event.outcome,
            severity=event.severity,
            details={
                "correlation_id": telemetry.report.correlation_id,
                "duration_ms": duration_ms,
                **event.details,
            },
        ),
        telemetry.report.logger,
    )


def _presentation_start(telemetry: _PresentationTelemetry) -> None:
    """Emit the correlation-bearing start record after the authorizer boundary."""
    _presentation_lifecycle(
        telemetry,
        _PresentationLifecycleEvent(
            phase="start",
            outcome="started",
            severity=LogSeverity.DEBUG,
            details={},
        ),
    )


def _presentation_terminal_success(
    telemetry: _PresentationTelemetry,
    response: dict[str, Any],
) -> None:
    """Emit one safe terminal projection for any completed V1 or V2 response."""
    _presentation_lifecycle(
        telemetry,
        _PresentationLifecycleEvent(
            phase="terminal",
            outcome="completed",
            severity=LogSeverity.INFO,
            details=_presentation_success_details(response),
        ),
    )


def _presentation_terminal_error(
    telemetry: _PresentationTelemetry,
    error: BaseException,
    status: int,
) -> None:
    """Emit one safe terminal projection after shared error reporting completes."""
    outcome, result_kind = _presentation_error_outcome(error)
    _presentation_lifecycle(
        telemetry,
        _PresentationLifecycleEvent(
            phase="terminal",
            outcome=outcome,
            severity=LogSeverity.WARNING if outcome == "refused" else LogSeverity.INFO,
            details={
                **_presentation_counts({}),
                "representation": "none",
                "result_kind": result_kind,
                "status": status,
            },
        ),
    )


def _schema_for_operation(
    runtime: PresentationRuntime,
    operation: str,
) -> AnalyticalPageSchema:
    schemas = runtime.schemas
    if schemas is None:
        raise RuntimeError("presentation page schemas are unavailable.")
    page_id = ANALYTICAL_PAGES[operation]
    schema = next((item for item in schemas.pages if item.page_id == page_id), None)
    if schema is None:
        raise RuntimeError("presentation page schema is unavailable.")
    return schema


def _malformed_artifact() -> InvariantError:
    return InvariantError(
        ARTIFACT_MALFORMED,
        "Persisted durable artifact bytes are malformed.",
        context={"record_kind": "artifact"},
    )


def _telemetry_operation(event: dict[str, Any]) -> str:
    """Map an untrusted request path to one bounded telemetry value."""
    try:
        path = events.path(event)
    except IdentityError:
        return INVALID_OPERATION
    return TELEMETRY_OPERATIONS.get(path, INVALID_OPERATION)


def _error_status(error: AdlyticsError) -> int:
    """Translate the repository taxonomy without calling dependency failures malformed."""
    return {
        ErrorClass.DOMAIN: 409,
        ErrorClass.VALIDATION: 400,
        ErrorClass.DEPENDENCY: 503,
        ErrorClass.INVARIANT: 500,
    }[error.error_class]


def _error_payload(error: AdlyticsError) -> dict[str, object]:
    return {
        "error": error.user_visible_description,
        "code": str(error.code),
        "error_class": error.error_class.value,
    }


def _unexpected_error_payload() -> dict[str, object]:
    return {
        "error": SAFE_FAILURE_DESCRIPTION,
        "code": UNEXPECTED_CODE,
        "error_class": ErrorClass.INVARIANT.value,
    }


@returns_correlation(LOGGER)
@trace_boundary(PRESENTATION_REQUEST)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Return a safe response and report every terminal request outcome."""
    with span(PRESENTATION_REQUEST, operation="presentation.request") as process_span:
        started_at = monotonic()
        operation = _telemetry_operation(event)
        correlation_id = current_correlation_id()
        try:
            events.authorizer_correlation_id(event)
        except IdentityError as error:
            report = Report(operation=operation, correlation_id=correlation_id, logger=LOGGER)
            telemetry = _PresentationTelemetry(report, started_at)
            _presentation_start(telemetry)
            report_denial(str(error), report)
            _presentation_terminal_error(telemetry, error, 401)
            process_span.record(
                outcome="refused",
                status=401,
                representation="none",
                result_kind="authorization",
            )
            return _json(401, {"error": "unauthorized"})
        report = Report(operation=operation, correlation_id=correlation_id, logger=LOGGER)
        telemetry = _PresentationTelemetry(report, started_at)
        _presentation_start(telemetry)
        try:
            correlation_token = _REQUEST_CORRELATION_ID.set(correlation_id)
            # The Lambda context's remaining-time reader lets a deep serve defer its
            # synchronous generation when the request envelope cannot absorb one.
            remaining = getattr(context, "get_remaining_time_in_millis", None)
            remaining_token = _REQUEST_REMAINING_MS.set(
                cast("Callable[[], int]", remaining) if callable(remaining) else None
            )
            try:
                response = handle(event, RUNTIME)
            finally:
                _REQUEST_CORRELATION_ID.reset(correlation_token)
                _REQUEST_REMAINING_MS.reset(remaining_token)
        except IdentityError as error:
            report_denial(str(error), report)
            _presentation_terminal_error(telemetry, error, 401)
            process_span.record(
                outcome="refused",
                status=401,
                representation="none",
                result_kind="authorization",
            )
            return _json(401, {"error": "unauthorized"})
        except AdlyticsError as error:
            status = _error_status(error)
            report_error(error, report)
            _presentation_terminal_error(telemetry, error, status)
            process_span.note_error(error)
            outcome, result_kind = _presentation_error_outcome(error)
            process_span.record(
                outcome=outcome,
                status=status,
                representation="none",
                result_kind=result_kind,
                refusal_reason=_serving_refusal_reason(error),
            )
            return _json(status, _error_payload(error))
        except Exception as error:
            process_span.note_failure(error)
            report_error(error, report)
            _presentation_terminal_error(telemetry, error, 500)
            process_span.record(
                outcome="failed",
                status=500,
                representation="none",
                result_kind="error",
            )
            return _json(500, _unexpected_error_payload())

        details = _presentation_success_details(response)
        process_span.record(
            outcome="completed",
            status=details["status"],
            representation=details["representation"],
            result_kind=details["result_kind"],
        )
        _presentation_terminal_success(telemetry, response)
        return response


def _operation(event: dict[str, Any]) -> str:
    method = events.method(event)
    path = events.path(event)
    if method != "POST" or path not in PRESENTATION_PATHS:
        raise ValidationError(PRESENTATION_REFUSED, "The presentation operation is not available.")
    return path


def _request_shape(operation: str, fields: set[str]) -> tuple[set[str], set[str]]:
    """Return exact required and allowed fields for one Presentation operation shape."""
    default = {"session_id"}
    explicit = {"session_id", "visible_window"}
    explicit_allowed = explicit | {"comparison_window"}
    selection = {"session_id", "selection_reference"}
    if operation == CAMPAIGN_LIST_PATH:
        return (default, default) if fields == default else (explicit, explicit_allowed)
    if operation == CAMPAIGN_DETAIL_PATH:
        return selection, selection
    if operation == CHANNEL_PERFORMANCE_PATH and fields == selection:
        return selection, selection
    return (default, default) if fields == default else (explicit, explicit_allowed)


def _request(
    event: dict[str, Any],
    operation: str,
) -> _DefaultPresentationInput | _ListPresentationInput | _AnalyticalPresentationInput:
    payload = _body(event)
    fields = set(payload)
    required, allowed = _request_shape(operation, fields)
    missing = required - fields
    unknown = fields - allowed
    if missing or unknown:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "The presentation request fields are invalid.",
            context={
                "missing": sorted(missing),
                "outcome": "invalid_fields",
                "n": len(unknown),
            },
        )
    session_id = _text(payload["session_id"], "session_id")
    if len(session_id) > MAX_SESSION_ID_LENGTH:
        raise ValidationError(PRESENTATION_REFUSED, "session_id is too long.")
    if (
        operation
        in {
            CAMPAIGN_DETAIL_PATH,
            CHANNEL_PERFORMANCE_PATH,
        }
        and "selection_reference" in payload
    ):
        reference = _selection_reference(
            payload["selection_reference"],
            channel=operation == CHANNEL_PERFORMANCE_PATH,
        )
        compare_previous = payload.get("compare_previous", False)
        if type(compare_previous) is not bool:
            raise ValidationError(
                PRESENTATION_REFUSED,
                "compare_previous must be a boolean intent.",
            )
        return _AnalyticalPresentationInput(
            session_id=session_id,
            selection_reference=reference,
            compare_previous=compare_previous,
        )
    visible = (
        None
        if "visible_window" not in payload
        else _window(payload["visible_window"], "visible_window")
    )
    comparison_value = payload.get("comparison_window")
    comparison = (
        None if comparison_value is None else _window(comparison_value, "comparison_window")
    )
    if operation == CAMPAIGN_LIST_PATH and comparison is not None:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "The campaign comparison period is derived by the server.",
        )
    if operation == CHANNEL_PERFORMANCE_PATH and comparison is not None:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "The channel comparison period is derived by the server.",
        )
    if operation == MARKETPLACE_COMPARISON_PATH and comparison is not None:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "The marketplace comparison period is derived by the server.",
        )
    if operation == DATA_HEALTH_PATH and comparison is not None:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "Data health does not accept a comparison period.",
        )
    if visible is None:
        return _DefaultPresentationInput(session_id)
    if operation == CAMPAIGN_LIST_PATH:
        return _ListPresentationInput(
            session_id,
            visible,
            ReportingWindowStatus.USER_SELECTED,
            comparison,
        )
    return _AnalyticalPresentationInput(
        session_id=session_id,
        visible_window=visible,
        reporting_window_status=ReportingWindowStatus.USER_SELECTED,
        comparison_window=comparison,
    )


def _resolved_window_request(
    operation: str,
    request: (_DefaultPresentationInput | _ListPresentationInput | _AnalyticalPresentationInput),
    runtime: PresentationRuntime,
) -> _ListPresentationInput | _AnalyticalPresentationInput:
    """Resolve an omitted initial window and derive the channel comparator server-side."""
    if not isinstance(request, _DefaultPresentationInput):
        if (
            operation in {CHANNEL_PERFORMANCE_PATH, MARKETPLACE_COMPARISON_PATH}
            and isinstance(request, _AnalyticalPresentationInput)
            and request.selection_reference is None
        ):
            visible_window = cast("DateRange", request.visible_window)
            return replace(
                request,
                comparison_window=_preceding_period(visible_window),
            )
        return request
    windows = runtime.windows
    catalog = runtime.metric_catalog
    schemas = runtime.schemas
    if windows is None or catalog is None or schemas is None:
        raise RuntimeError("presentation window runtime is incomplete.")
    page_id = (
        "campaign-attribution-detail"
        if operation == CAMPAIGN_LIST_PATH
        else ANALYTICAL_PAGES[operation]
    )
    schema = next((page for page in schemas.pages if page.page_id == page_id), None)
    if schema is None:
        raise RuntimeError("presentation window schema is unavailable.")
    requirements: list[PageWindowRequirement] = []
    for slot in schema.metrics:
        metric = catalog.metric(slot.name)
        if metric is None:
            raise RuntimeError(f"presentation metric {slot.name} is unavailable.")
        for variant in slot.variants or (None,):
            selectors = () if variant is None else tuple(sorted(variant.selectors))
            requirement = metric_coverage_requirement(catalog, metric, selectors)
            if requirement not in requirements:
                requirements.append(requirement)
    resolved = windows.resolve_default(
        PageDefaultWindowRequest(
            requirements=tuple(requirements),
            preferred_duration_days=schema.preferred_window_duration_days,
        )
    )
    if resolved is None:
        raise DomainError(
            PRESENTATION_UNAVAILABLE,
            "No common reporting period is available for this page.",
        )
    status = (
        ReportingWindowStatus.PREFERRED_DEFAULT
        if resolved.completeness is PageDefaultWindowCompleteness.PREFERRED
        else ReportingWindowStatus.PARTIAL_DEFAULT
    )
    if operation == CAMPAIGN_LIST_PATH:
        return _ListPresentationInput(request.session_id, resolved.window, status, None)
    analytical = _AnalyticalPresentationInput(
        session_id=request.session_id,
        visible_window=resolved.window,
        reporting_window_status=status,
    )
    if operation in {CHANNEL_PERFORMANCE_PATH, MARKETPLACE_COMPARISON_PATH}:
        return replace(
            analytical,
            comparison_window=_preceding_period(resolved.window),
        )
    return analytical


def _preceding_period(period: DateRange) -> DateRange:
    """Return the immediately preceding non-overlapping period of equal inclusive length."""
    end = period.start - timedelta(days=1)
    return DateRange(end - timedelta(days=period.days - 1), end)


def _selection_reference(value: object, *, channel: bool = False) -> PageContextReference:
    unavailable = _channel_selection_unavailable if channel else _selection_unavailable
    if not isinstance(value, str) or not value.strip():
        raise unavailable()
    try:
        return PageContextReference(value)
    except ValueError:
        raise unavailable() from None


def _selection_unavailable() -> ValidationError:
    return ValidationError(
        PRESENTATION_REFUSED,
        "The requested campaign is not available.",
    )


def _campaign_body_superseded() -> DomainError:
    """Refuse a dialog over a Campaigns body baked before render format 14: it is unpublished."""
    return DomainError(
        PRESENTATION_UNAVAILABLE, "No campaign results are published for this period."
    )


def _channel_selection_unavailable() -> ValidationError:
    return ValidationError(
        PRESENTATION_REFUSED,
        "The requested channel is not available.",
    )


def _body(event: dict[str, Any]) -> dict[str, object]:
    if event.get("isBase64Encoded") is True:
        raise ValidationError(PRESENTATION_REFUSED, "The presentation body encoding is invalid.")
    body = event.get("body")
    if not isinstance(body, str):
        raise ValidationError(PRESENTATION_REFUSED, "The presentation request needs a JSON body.")
    try:
        raw: object = json.loads(body)
    except ValueError:
        raise ValidationError(
            PRESENTATION_REFUSED,
            "The presentation body is not valid JSON.",
        ) from None
    if not isinstance(raw, dict):
        raise ValidationError(PRESENTATION_REFUSED, "The presentation body must be an object.")
    return cast("dict[str, object]", raw)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(PRESENTATION_REFUSED, f"{label} must be non-empty text.")
    return value


def _window(value: object, label: str) -> DateRange:
    if not isinstance(value, dict):
        raise ValidationError(PRESENTATION_REFUSED, f"{label} must be an exact date range.")
    item = cast("dict[str, object]", value)
    if set(item) != {"start", "end"}:
        raise ValidationError(PRESENTATION_REFUSED, f"{label} must be an exact date range.")
    try:
        start = date.fromisoformat(_text(item["start"], f"{label}.start"))
        end = date.fromisoformat(_text(item["end"], f"{label}.end"))
        return DateRange(start, end)
    except ValueError:
        raise ValidationError(PRESENTATION_REFUSED, f"{label} must be an ISO date range.") from None


def _list_page(
    request: _ListPresentationInput,
    runtime: PresentationRuntime,
    *,
    portfolio: bool = False,
) -> CampaignListPage:
    result = runtime.catalog.browse(
        EntityCatalogBrowseRequest(
            scope=runtime.scope,
            kinds=frozenset({EntityKind.CAMPAIGN}),
            window=request.visible_window,
            current_on=None,
            context=EntityContext(),
            max_candidates=(
                MAX_CAMPAIGN_PORTFOLIO_CANDIDATES if portfolio else MAX_ENTITY_CANDIDATES
            ),
            purpose=BrowsePurpose.INTERACTIVE,
        )
    )
    grouped = _campaign_histories(result.aliases)
    campaigns = tuple(
        present_campaign(CampaignPresentationRequest(aliases, request.visible_window))
        for _, aliases in sorted(
            grouped.items(),
            key=lambda item: (item[0].platform.value, item[0].value),
        )
    )
    return CampaignListPage(
        campaigns,
        request.visible_window,
        request.comparison_window,
        result.truncated,
        result.authorized_count,
    )


def _campaign_schema(runtime: PresentationRuntime) -> AnalyticalPageSchema:
    schema = _schema_for_operation(runtime, CAMPAIGN_DETAIL_PATH)
    declaration = schema.artifact
    if (
        declaration is None
        or declaration.artifact_type != CAMPAIGN_ATTRIBUTION_ARTIFACT_TYPE
        or declaration.version != CAMPAIGN_ATTRIBUTION_ARTIFACT_VERSION
        or declaration.outcomes != ("choices", "empty", "ambiguous", "selected", "insufficient")
        or tuple(metric.name for metric in schema.metrics)
        != ("purchases", "roas_attributed_14d", "ad_spend")
        or any(metric.variants for metric in schema.metrics)
    ):
        raise RuntimeError("campaign attribution artifact declaration is unavailable.")
    return schema


def _v2_campaign_capability_reference(
    runtime: PresentationRuntime,
    temporal_control: TemporalControl | None,
) -> PageContextReference:
    """Mint an opaque V2-only namespace without exposing canonical campaign identity."""
    value = runtime.token_minter()
    if temporal_control is None:
        return PageContextReference(value)
    return PageContextReference(f"v2_{value[3:]}")


def _campaign_portfolio_answer(  # noqa: PLR0913, PLR0917
    answer_slot: PresentationMeasurementAnswerSlot,
    period: DateRange,
    expected_group: tuple[str, ...],
    expected_kind: UnitKind,
) -> tuple[dict[tuple[str, ...], CampaignNumber], Units, tuple[str, ...]] | None:
    """Accept only fully-covered, untruncated portfolio evidence at its declared grain.

    A complete, unsuppressed answer with no rows is a legitimate zero for a non-monetary
    additive metric: a grouped conversion query has no row for a campaign with zero attributed
    purchases. The envelope states no units for such an answer, so the slot's declared unit
    stands in. An empty monetary answer has nothing to denominate and is still refused.
    """
    answer = answer_slot.answer
    if answer is None:
        return None
    metadata = answer.metadata
    units = answer.data.units
    if units is None and not answer.data.values and expected_kind is not UnitKind.CURRENCY:
        units = Units(expected_kind)
    basis = metadata.measurement_basis
    if (
        metadata.requested_coverage_intervals != (period,)
        or metadata.actual_coverage_intervals != (period,)
        or metadata.coverage_holes
        or metadata.suppression.rows_suppressed
        or metadata.truncated
        or metadata.freshness_status is FreshnessStatus.NEVER_LOADED
        or answer.data.grain.dimensions != expected_group
        or basis != (MeasurementBasis.AD_ATTRIBUTED,)
        or units is None
        or units.kind is not expected_kind
        or metadata.row_count != len(answer.data.values)
    ):
        return None
    rows: dict[tuple[str, ...], CampaignNumber] = {}
    expected_fields = {*expected_group, MEASURE_ALIAS}
    for raw in answer.data.values:
        value = raw.get(MEASURE_ALIAS)
        key = tuple(raw.get(item) for item in expected_group)
        if (
            set(raw) != expected_fields
            or any(not isinstance(item, str) or not item.strip() for item in key)
            or isinstance(value, bool)
            or not isinstance(value, (int, Decimal))
            or value < 0
            or key in rows
        ):
            return None
        rows[cast("tuple[str, ...]", key)] = value
    qualifiers = tuple(
        sorted(
            {
                *metadata.qualified_dq,
                *(qualifier.rendering for qualifier in metadata.qualifiers),
                *(
                    ()
                    if metadata.freshness_status is FreshnessStatus.FRESH
                    else (metadata.freshness_status.value,)
                ),
            }
        )
    )
    return rows, units, qualifiers


def _campaign_measurement_service(
    runtime: PresentationRuntime,
) -> CampaignArtifactMeasurementService:
    """Resolve the shared custody-free renderer inputs for a presentation runtime."""
    if runtime.campaign_measurements is not None:
        return runtime.campaign_measurements
    if runtime.metric_catalog is None:
        raise RuntimeError("campaign measurement metric catalog is unavailable.")
    return CampaignArtifactMeasurementService(
        CampaignMeasurementDependencies(
            scope=runtime.scope,
            catalog=runtime.metric_catalog,
            contracts=runtime.contracts,
            clock=runtime.clock,
            rosters=None if runtime.measurements is None else runtime.measurements.rosters,
        )
    )


def _campaign_portfolio_measurement_batch(
    execution: _PresentationExecutionRequest,
    selections: tuple[CampaignMeasurementSelection, ...],
) -> tuple[PreparedMeasurementBatch, tuple[tuple[str, str, str, DateRange], ...]]:
    """Prepare the shared fixed portfolio cohort before issuing V2 row capabilities."""
    runtime = execution.runtime
    request = cast("_ListPresentationInput", execution.request)
    if request.comparison_window is None:
        raise RuntimeError("campaign portfolio comparison period is unavailable.")
    service = _campaign_measurement_service(runtime)
    batch = service.portfolio_batch(
        CampaignPortfolioMeasurementInput(
            selections=selections,
            period=request.visible_window,
            prior_period=request.comparison_window,
            sort=request.campaign_sort
            or CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        )
    )
    return batch.prepared, batch.slots


def _campaign_portfolio_response(  # noqa: PLR0912, PLR0913, PLR0915, PLR0917
    execution: _PresentationExecutionRequest,
    schema: AnalyticalPageSchema,
    page: CampaignListPage,
    signatures: tuple[SafeCampaignDisplaySignature, ...],
) -> dict[str, Any]:
    """Measure, sort, bound, and atomically issue the V2 portfolio parent artifact."""
    runtime = execution.runtime
    request = cast("_ListPresentationInput", execution.request)
    temporal_control = execution.temporal_control
    if (
        temporal_control is None
        or runtime.page_artifacts is None
        or runtime.campaign_capabilities is None
    ):
        raise RuntimeError("campaign portfolio runtime is unavailable.")
    evidence = (
        CampaignListEvidence(
            "Governed campaign catalog", request.visible_window, (request.visible_window,)
        ),
    )
    sort = request.campaign_sort or CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc")
    page_record = runtime.prepare_record(
        _PageMintRequest(
            execution.owner,
            request.session_id,
            PageContextSnapshot(
                response_identity="campaign-portfolio-list",
                visible_window=request.visible_window,
                reporting_window_status=request.reporting_window_status,
            ),
        )
    )
    if not page.campaigns:
        artifact = build_campaign_portfolio(
            period=request.visible_window,
            sort=sort,
            rows=(),
            totals=(),
            trend=None,
            evidence=evidence,
        )
        pending = ()
    elif page.truncated or len(signatures) != len(page.campaigns):
        artifact = build_campaign_portfolio(
            period=request.visible_window,
            sort=sort,
            rows=(),
            totals=(),
            trend=None,
            evidence=evidence,
            reason_codes=(CampaignPortfolioInsufficiencyReason.MISSING_CATALOG,),
        )
        pending = ()
    else:
        pending = tuple(
            _PendingCampaignCapability(
                _v2_campaign_capability_reference(runtime, temporal_control),
                campaign.campaign,
                cast("ChannelKey", campaign.channel),
                cast("MarketplaceKey", campaign.marketplace),
                signature,
            )
            for campaign, signature in zip(page.campaigns, signatures, strict=True)
        )
        inspector = runtime.presentation_evidence
        tooling = runtime.measurements
        if inspector is None or tooling is None:
            raise RuntimeError("campaign portfolio evidence runtime is unavailable.")
        prepared, slots = _campaign_portfolio_measurement_batch(
            execution,
            tuple(
                CampaignMeasurementSelection(
                    candidate.campaign,
                    candidate.channel,
                    candidate.marketplace,
                    candidate.safe_signature,
                )
                for candidate in pending
            ),
        )
        inspection = inspector.inspect(
            PresentationEvidenceInspectionRequest(
                plans=tuple(
                    PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                    for plan in prepared.plans
                ),
                contracts=prepared.contracts,
                now=prepared.now,
            )
        )
        executed = execute_presentation_batch(
            prepared,
            inspection,
            MeasurementExecutionTooling(
                queries=tooling.queries,
                query_batch_max_concurrency=tooling.query_batch_max_concurrency,
            ),
        )
        expected_kinds = {
            "ad_spend": UnitKind.CURRENCY,
            "roas_attributed_14d": UnitKind.RATIO,
            "purchases": UnitKind.COUNT,
            "ctr": UnitKind.RATIO,
            "impressions": UnitKind.COUNT,
            "attributed_revenue_14d": UnitKind.CURRENCY,
        }
        answers: dict[
            tuple[str, str, str],
            tuple[dict[tuple[str, ...], CampaignNumber], Units, tuple[str, ...]],
        ] = {}
        malformed = False
        for answer, (section, metric, role, period) in zip(executed.slots, slots, strict=True):
            group = (
                ("campaign_key", "marketplace_key")
                if section in {"row", "portfolio"}
                else ("date_key", "campaign_key", "marketplace_key")
                if section in {"spark", "trend"}
                else ()
            )
            assembled = _campaign_portfolio_answer(answer, period, group, expected_kinds[metric])
            if assembled is None:
                malformed = True
                break
            answers[(section, metric, role)] = assembled
        if malformed:
            artifact = build_campaign_portfolio(
                period=request.visible_window,
                sort=sort,
                rows=(),
                totals=(),
                trend=None,
                evidence=evidence,
                reason_codes=(CampaignPortfolioInsufficiencyReason.INCOMPATIBLE_SCOPE,),
            )
            pending = ()
        else:
            rows: list[CampaignPortfolioRow] = []
            for candidate in pending:
                identity = (candidate.campaign.value, candidate.marketplace.value)
                comparisons: list[CampaignPortfolioComparison] = []
                freshness: set[str] = set()
                for metric, public_metric in (
                    ("ad_spend", CampaignPortfolioMetric.SPEND),
                    ("roas_attributed_14d", CampaignPortfolioMetric.ATTRIBUTED_ROAS),
                    ("purchases", CampaignPortfolioMetric.PURCHASES),
                    ("ctr", CampaignPortfolioMetric.CTR),
                ):
                    current_rows, current_units, current_freshness = answers[
                        ("row", metric, "current")
                    ]
                    prior_rows, prior_units, prior_freshness = answers[("row", metric, "prior")]
                    # A fully covered grouped answer has no row for a campaign whose additive
                    # operand is zero in that window; absence is zero, not corrupted scope.
                    current_value = current_rows.get(identity, 0)
                    prior_value = prior_rows.get(identity, 0)
                    current = CampaignPortfolioMetricValue(
                        current_value,
                        current_units,
                        MeasurementBasis.AD_ATTRIBUTED,
                        request.visible_window,
                        CAMPAIGN_PORTFOLIO_ROW_GRAIN,
                    )
                    prior = CampaignPortfolioMetricValue(
                        prior_value,
                        prior_units,
                        MeasurementBasis.AD_ATTRIBUTED,
                        cast("DateRange", request.comparison_window),
                        CAMPAIGN_PORTFOLIO_ROW_GRAIN,
                    )
                    comparisons.append(
                        campaign_portfolio_comparison(
                            public_metric, current, prior, CHANGE_SEMANTICS
                        )
                    )
                    freshness.update(current_freshness)
                    freshness.update(prior_freshness)
                # A row carries no spend spark (ADR-127); the spark answer's freshness still
                # qualifies the row it was measured for.
                freshness.update(answers[("spark", "ad_spend", "current")][2])
                rows.append(
                    CampaignPortfolioRow(
                        candidate.safe_signature,
                        CampaignSelectionCapability(
                            candidate.reference.value, page_record.expires_at
                        ),
                        tuple(comparisons),
                        tuple(sorted(freshness)),
                    )
                )
            reduction: CampaignPortfolioReduction | None = None
            portfolio_operands = (
                "ad_spend",
                "attributed_revenue_14d",
                "purchases",
                "impressions",
            )
            if len({candidate.marketplace.value for candidate in pending}) != 1 or any(
                answers[("portfolio", metric, "current")][1]
                != answers[("portfolio", metric, "prior")][1]
                for metric in portfolio_operands
            ):
                malformed = True
            else:
                try:
                    reduction = reduce_campaign_portfolio(
                        CampaignPortfolioReductionRequest(
                            frozenset(
                                (candidate.campaign.value, candidate.marketplace.value)
                                for candidate in pending
                            ),
                            {
                                metric: answers[("portfolio", metric, "current")][0]
                                for metric in portfolio_operands
                            },
                            {
                                metric: answers[("portfolio", metric, "prior")][0]
                                for metric in portfolio_operands
                            },
                            {
                                metric: answers[("trend", metric, "current")][0]
                                for metric in portfolio_operands
                            },
                            request.visible_window,
                        )
                    )
                except ValueError:
                    malformed = True
            total_comparisons: list[CampaignPortfolioComparison] = []
            if not malformed and reduction is not None:
                for public_metric, units in (
                    (
                        CampaignPortfolioMetric.SPEND,
                        answers[("portfolio", "ad_spend", "current")][1],
                    ),
                    (
                        CampaignPortfolioMetric.PURCHASES,
                        answers[("portfolio", "purchases", "current")][1],
                    ),
                    (
                        CampaignPortfolioMetric.IMPRESSIONS,
                        answers[("portfolio", "impressions", "current")][1],
                    ),
                    (CampaignPortfolioMetric.PORTFOLIO_ROAS, Units(UnitKind.RATIO)),
                ):
                    current_value = reduction.current[public_metric]
                    prior_value = reduction.prior[public_metric]
                    current = CampaignPortfolioMetricValue(
                        current_value,
                        units,
                        MeasurementBasis.AD_ATTRIBUTED,
                        request.visible_window,
                        CAMPAIGN_PORTFOLIO_TOTAL_GRAIN,
                    )
                    prior = CampaignPortfolioMetricValue(
                        prior_value,
                        units,
                        MeasurementBasis.AD_ATTRIBUTED,
                        cast("DateRange", request.comparison_window),
                        CAMPAIGN_PORTFOLIO_TOTAL_GRAIN,
                    )
                    total_comparisons.append(
                        campaign_portfolio_comparison(
                            public_metric, current, prior, CHANGE_SEMANTICS
                        )
                    )
            if malformed or reduction is None:
                artifact = build_campaign_portfolio(
                    period=request.visible_window,
                    sort=sort,
                    rows=(),
                    totals=(),
                    trend=None,
                    evidence=evidence,
                    reason_codes=(CampaignPortfolioInsufficiencyReason.INCOMPATIBLE_SCOPE,),
                )
                pending = ()
            else:
                artifact = build_campaign_portfolio(
                    period=request.visible_window,
                    sort=sort,
                    rows=tuple(rows),
                    totals=tuple(total_comparisons),
                    trend=CampaignPortfolioTrend(
                        request.visible_window,
                        reduction.trend_points,
                        answers[("trend", "ad_spend", "current")][1],
                        Units(UnitKind.RATIO),
                        answers[("trend", "purchases", "current")][1],
                        MeasurementBasis.AD_ATTRIBUTED,
                    ),
                    evidence=evidence,
                    authorized_count=page.authorized_count,
                    # Holed answers are refused above, so every figure covers its window.
                    evidence_status=campaign_portfolio_evidence_status(
                        request.visible_window, PRESENTATION_LABELS
                    )
                    if rows
                    else None,
                )
    artifact = _with_portfolio_insights(_PortfolioInsightInput(artifact=artifact, runtime=runtime))
    page_artifact = PageArtifact(
        CAMPAIGN_PORTFOLIO_ARTIFACT_TYPE,
        CAMPAIGN_PORTFOLIO_ARTIFACT_VERSION,
        canonical_artifact_json(campaign_portfolio_artifact_payload(artifact)),
    )
    artifact_record = PageArtifactRecord(
        reference=page_record.reference,
        artifact=page_artifact,
        verified_owner_id=page_record.verified_owner_id,
        conversation_session_id=page_record.conversation_session_id,
        expires_at=page_record.expires_at,
        temporal_control=temporal_control,
    )
    by_reference = {item.reference.value: item for item in pending}
    capabilities = tuple(
        CampaignSelectionCapabilityRecord(
            reference=by_reference[row.capability.value].reference,
            parent_artifact_reference=artifact_record.reference,
            parent_artifact_type=artifact_record.artifact.artifact_type,
            parent_artifact_version=artifact_record.artifact.artifact_version,
            verified_owner_id=artifact_record.verified_owner_id,
            conversation_session_id=artifact_record.conversation_session_id,
            expires_at=artifact_record.expires_at,
            campaign=by_reference[row.capability.value].campaign,
            channel=by_reference[row.capability.value].channel,
            marketplace=by_reference[row.capability.value].marketplace,
            safe_signature=by_reference[row.capability.value].safe_signature,
            reporting_window_status=request.reporting_window_status,
        )
        for row in artifact.rows
    )
    outcome = _issue_parent_after_page_record(
        runtime, page_record, ParentCapabilityIssuanceRequest(artifact_record, capabilities)
    )
    if outcome.outcome is ParentCapabilityIssuanceOutcome.CAPACITY_REFUSED:
        raise DomainError(
            PRESENTATION_UNAVAILABLE, "The page has too many available row selections."
        )
    if not outcome.issued:
        raise RuntimeError("parent capability issuance did not complete.")
    analytical_result = _AnalyticalPageExecutionResult(
        schema=schema,
        page=None,
        snapshot=page_record.snapshot,
        artifact=page_artifact,
    )
    return _json(
        200,
        {
            "page": _analytical_page_json(
                analytical_result, runtime.dimension_labels, artifact_record
            ),
            "session_id": request.session_id,
            "reporting_window_status": request.reporting_window_status.value,
            "visible_window": _window_json(request.visible_window),
        },
    )


def _with_retail_insights(request: _RetailInsightInput) -> RetailHealthArtifact:
    """Attach the three prompted retail slots beside the baked deterministic movers slot."""
    derivation = retail_insight_request_derivation(
        RetailInsightRequestDerivationInput(
            artifact=request.baked,
            scope=request.runtime.scope,
            contracts=INSIGHT_CONTRACTS,
            catalog=METRIC_CATALOG,
        )
    )
    model_slots = _assembled_insights(
        _InsightAssemblyInput(
            runtime=request.runtime,
            context=derivation.context,
            slot_ids=(
                InsightSlotId.DEMAND_SHAPE,
                InsightSlotId.RETURNS_PRESSURE,
                InsightSlotId.ORDERED_VS_SHIPPED,
            ),
            requests=derivation.requests,
            silence_reasons=derivation.silence_reasons,
            missing_evidence_by_slot=derivation.missing_evidence_by_slot,
        )
    )
    # The baked artifact renders retail-movers deterministically at bake time; the declared
    # roster order pins it last and this overlay must preserve that slot untouched.
    return replace(request.served, insights=(*model_slots, request.served.insights[-1]))


def _with_portfolio_insights(request: _PortfolioInsightInput) -> CampaignPortfolioArtifact:
    """Attach shared model slots and the direct deterministic movers slot."""
    derivation, movers = portfolio_insight_request_derivation(
        artifact=request.artifact,
        scope=request.runtime.scope,
        contracts=INSIGHT_CONTRACTS,
    )
    scheduled = portfolio_model_slot_ids(request.artifact)
    assembled = {
        slot.id: slot
        for slot in _assembled_insights(
            _InsightAssemblyInput(
                runtime=request.runtime,
                context=derivation.context,
                slot_ids=scheduled,
                requests=derivation.requests,
                silence_reasons=derivation.silence_reasons,
                missing_evidence_by_slot=derivation.missing_evidence_by_slot,
                served_steps=NextStepServeContext(campaign_refs=request.campaign_refs),
            )
        )
    }
    # A multi-channel sibling has no portfolio ROAS direction, so divergence isn't scheduled
    # and is served as the designed absence its missing input names (ADR-127 decision 5).
    if InsightSlotId.DIVERGENCE not in scheduled:
        assembled[InsightSlotId.DIVERGENCE] = ineligible_insight_slots(
            (InsightSlotId.DIVERGENCE,),
            artifact_revision=derivation.context.artifact_revision,
            missing_by_slot={InsightSlotId.DIVERGENCE: (InsightEvidenceInput.PORTFOLIO_DIRECTION,)},
        )[0]
    model_slots = tuple(
        assembled[slot_id]
        for slot_id in (
            InsightSlotId.CONTRIBUTION,
            InsightSlotId.DIVERGENCE,
            InsightSlotId.EXPOSURE,
        )
    )
    # Movers rank spend changes, so they are written in the spend total's currency. Movers
    # are non-empty only for a COMPLETE portfolio, whose totals always include spend.
    spend_total = next(
        (
            total
            for total in request.artifact.totals
            if total.metric is CampaignPortfolioMetric.SPEND
        ),
        None,
    )
    currency_code = None if spend_total is None else spend_total.current.units.currency_code
    return replace(
        request.artifact,
        insights=(
            *model_slots,
            deterministic_movers_slot(
                DeterministicMoversSlotRequest(
                    artifact_revision=derivation.context.artifact_revision,
                    findings=movers,
                    silence_reason=INSIGHT_CONTRACTS[InsightSlotId.MOVERS].silence_condition,
                    currency_code=currency_code,
                )
            ),
        ),
    )


def _campaign_list_response(execution: _PresentationExecutionRequest) -> dict[str, Any]:  # noqa: PLR0912, PLR0915
    """Build and persist the campaign parent artifact from one resolved execution."""
    owner = execution.owner
    request = execution.request
    runtime = execution.runtime
    if not isinstance(request, _ListPresentationInput):
        raise RuntimeError("campaign list execution request is invalid.")
    temporal_control = execution.temporal_control
    schema = _campaign_schema(runtime)
    artifacts = runtime.page_artifacts
    capabilities = runtime.campaign_capabilities
    if artifacts is None or capabilities is None:
        raise RuntimeError("campaign capability runtime is unavailable.")
    page = _list_page(request, runtime, portfolio=temporal_control is not None)
    blockers: tuple[CampaignListReason, ...] = (
        (CampaignListReason.TRUNCATED,) if page.truncated else ()
    )
    signatures: tuple[SafeCampaignDisplaySignature, ...] = ()
    if not blockers:
        try:
            signatures = tuple(
                safe_campaign_signature(
                    campaign,
                    request.visible_window,
                    (*runtime.dimension_labels, *runtime.campaign_platform_labels),
                )
                for campaign in page.campaigns
            )
        except TypeError, ValueError:
            blockers = (CampaignListReason.MISSING_CATALOG,)
    if temporal_control is not None:
        return _campaign_portfolio_response(execution, schema, page, signatures)
    snapshot = PageContextSnapshot(
        response_identity="campaign-attribution-list",
        visible_window=request.visible_window,
        reporting_window_status=request.reporting_window_status,
    )
    page_record = runtime.prepare_record(_PageMintRequest(owner, request.session_id, snapshot))
    pending: list[_PendingCampaignCapability] = []
    choices: list[CampaignSelectionChoice] = []
    if not blockers:
        for campaign, signature in zip(page.campaigns, signatures, strict=True):
            if campaign.channel is None or campaign.marketplace is None:
                raise RuntimeError("campaign signature lost canonical presentation context.")
            reference = _v2_campaign_capability_reference(runtime, temporal_control)
            pending.append(
                _PendingCampaignCapability(
                    reference,
                    campaign.campaign,
                    campaign.channel,
                    campaign.marketplace,
                    signature,
                )
            )
            choices.append(
                CampaignSelectionChoice(
                    signature,
                    CampaignSelectionCapability(reference.value, page_record.expires_at),
                )
            )
    recovery_actions: tuple[CampaignRecoveryAction, ...] = ()
    if blockers:
        recovery_actions = (
            CampaignRecoveryAction.CHANGE_PERIOD
            if CampaignListReason.TRUNCATED in blockers
            else CampaignRecoveryAction.CONTACT_WORKSPACE_DATA_OWNER,
        )
    elif not choices:
        recovery_actions = (CampaignRecoveryAction.CHANGE_PERIOD,)
    list_artifact = build_campaign_selection_list(
        CampaignSelectionListRequest(
            period=request.visible_window,
            choices=tuple(choices),
            evidence=(
                CampaignListEvidence(
                    source="Governed campaign catalog",
                    requested_period=request.visible_window,
                    actual_coverage=(request.visible_window,),
                ),
            ),
            truncated=page.truncated,
            blockers=blockers,
            recovery_actions=recovery_actions,
        )
    )
    page_artifact = PageArtifact(
        CAMPAIGN_ATTRIBUTION_ARTIFACT_TYPE,
        CAMPAIGN_ATTRIBUTION_ARTIFACT_VERSION,
        canonical_artifact_json(campaign_page_artifact_payload(list_artifact)),
    )
    analytical_result = _AnalyticalPageExecutionResult(
        schema=schema,
        page=None,
        snapshot=snapshot,
        artifact=page_artifact,
    )
    runtime.page_context.put(page_record)
    artifact_record = PageArtifactRecord(
        reference=page_record.reference,
        artifact=page_artifact,
        verified_owner_id=page_record.verified_owner_id,
        conversation_session_id=page_record.conversation_session_id,
        expires_at=page_record.expires_at,
        temporal_control=temporal_control,
    )
    pending_by_reference = {item.reference.value: item for item in pending}
    capability_records = tuple(
        CampaignSelectionCapabilityRecord(
            reference=pending_by_reference[choice.capability.value].reference,
            parent_artifact_reference=artifact_record.reference,
            parent_artifact_type=artifact_record.artifact.artifact_type,
            parent_artifact_version=artifact_record.artifact.artifact_version,
            verified_owner_id=artifact_record.verified_owner_id,
            conversation_session_id=artifact_record.conversation_session_id,
            expires_at=artifact_record.expires_at,
            campaign=pending_by_reference[choice.capability.value].campaign,
            channel=pending_by_reference[choice.capability.value].channel,
            marketplace=pending_by_reference[choice.capability.value].marketplace,
            safe_signature=pending_by_reference[choice.capability.value].safe_signature,
            reporting_window_status=request.reporting_window_status,
        )
        for choice in list_artifact.choices
    )
    if temporal_control is not None:
        issuance = runtime.parent_capability_issuance
        if issuance is None:
            raise RuntimeError("parent capability issuance storage is unavailable.")
        outcome = issuance.issue_parent_capabilities(
            ParentCapabilityIssuanceRequest(artifact_record, capability_records)
        )
        if outcome.outcome is ParentCapabilityIssuanceOutcome.CAPACITY_REFUSED:
            raise DomainError(
                PRESENTATION_UNAVAILABLE,
                "The page has too many available row selections.",
            )
        if not outcome.issued:
            raise RuntimeError("parent capability issuance did not complete.")
    else:
        artifacts.put_artifact(artifact_record)
        for capability in capability_records:
            capabilities.put_campaign_capability(capability)
    return _json(
        200,
        {
            "page": _analytical_page_json(
                analytical_result,
                runtime.dimension_labels,
                artifact_record,
            ),
            "session_id": request.session_id,
            "reporting_window_status": snapshot.reporting_window_status.value,
            "visible_window": _window_json(snapshot.visible_window),
        },
    )


def _analytical_page(
    execution: _AnalyticalExecution,
) -> _AnalyticalPageExecutionResult:
    operation = execution.operation
    runtime = execution.runtime
    schemas = runtime.schemas
    catalog = runtime.metric_catalog
    tooling = runtime.measurements
    if schemas is None or catalog is None or tooling is None:
        raise RuntimeError("analytical presentation runtime is incomplete.")
    page_id = ANALYTICAL_PAGES[operation]
    schema = next((page for page in schemas.pages if page.page_id == page_id), None)
    if schema is None:
        raise RuntimeError("analytical presentation schema is unavailable.")

    if operation == CHANNEL_PERFORMANCE_PATH:
        if execution.request.selection_reference is not None:
            return _selected_channel_page(
                execution,
                schema,
                execution.validated_channel_selection,
            )
        snapshot = _analytical_snapshot(execution)
        return _channel_parent_page(execution, schema, snapshot)
    if operation == CAMPAIGN_DETAIL_PATH:
        return _campaign_detail_page(execution, _campaign_schema(runtime))

    snapshot = _analytical_snapshot(execution)
    if operation == MARKETPLACE_COMPARISON_PATH:
        return _marketplace_comparison_page(execution, schema, snapshot)
    if operation == DATA_HEALTH_PATH:
        return _data_health_page(execution, schema, snapshot)
    filters = _snapshot_filters(snapshot)
    computed_at = runtime.clock()
    requests: list[MeasurementToolRequest] = []
    slots: list[tuple[str, tuple[MetricRole, ...], str | None]] = []
    for metric in schema.metrics:
        for variant in metric.variants or (None,):
            variant_filters = {} if variant is None else dict(variant.selectors)
            requests.append(
                MeasurementToolRequest(
                    tool=(
                        MeasurementTool.COMPARE_WINDOWS
                        if snapshot.comparison_window is not None
                        else MeasurementTool.METRIC_BY_GRAIN
                    ),
                    catalog=catalog,
                    contracts=runtime.contracts,
                    metric_name=metric.name,
                    grains=_page_grains(schema, metric.name),
                    window=snapshot.visible_window,
                    prior_window=snapshot.comparison_window,
                    computed_at=computed_at,
                    correlation_id="presentation-page",
                    filters={**filters, **variant_filters},
                    max_rows=DEFAULT_MAX_ROWS,
                )
            )
            slots.append(
                (
                    metric.name,
                    metric.roles,
                    None if variant is None else variant.variant_id,
                )
            )
    answers = require_answered(handle_batch(tuple(requests), tooling))
    measurements = tuple(
        PageMeasurementResult.from_answer(
            PageMeasurementResultRequest(metric_name, roles, variant_id, answer)
        )
        for (metric_name, roles, variant_id), answer in zip(slots, answers, strict=True)
    )
    page = AnalyticalPageResult(schema, measurements)
    return _AnalyticalPageExecutionResult(schema=schema, page=page, snapshot=snapshot)


def _validated_campaign_capability(
    execution: _AnalyticalExecution,
) -> _ValidatedCampaignCapability:
    runtime = execution.runtime
    capabilities = runtime.campaign_capabilities
    artifacts = runtime.page_artifacts
    reference = execution.request.selection_reference
    if capabilities is None or artifacts is None or reference is None:
        raise RuntimeError("campaign capability runtime is unavailable.")
    result = capabilities.read_campaign_capability(
        CampaignSelectionCapabilityReadRequest(
            reference=reference,
            verified_owner_id=execution.owner,
            conversation_session_id=execution.request.session_id,
            at=runtime.clock(),
        )
    )
    capability = result.capability
    if result.outcome is not ResolutionOutcome.RESOLVED or capability is None:
        raise _selection_unavailable()
    parent_result = artifacts.read_artifact(
        PageArtifactReadRequest(
            reference=capability.parent_artifact_reference,
            verified_owner_id=execution.owner,
            conversation_session_id=execution.request.session_id,
            at=runtime.clock(),
        )
    )
    parent = parent_result.artifact
    if parent_result.outcome is not ResolutionOutcome.RESOLVED or parent is None:
        raise _selection_unavailable()
    if parent.artifact.artifact_type == CAMPAIGN_PORTFOLIO_ARTIFACT_TYPE and (
        campaign_portfolio_artifact_superseded(parent.artifact.payload)
    ):
        # A page served before render format 14 holds no redeemable row after the deploy.
        raise _selection_unavailable()
    try:
        parent_model = (
            campaign_portfolio_artifact_from_payload(parent.artifact.payload)
            if parent.artifact.artifact_type == CAMPAIGN_PORTFOLIO_ARTIFACT_TYPE
            else campaign_page_artifact_from_payload(parent.artifact.payload)
        )
    except TypeError, ValueError:
        raise _malformed_artifact() from None
    matching_choices = (
        tuple(
            choice
            for choice in parent_model.choices
            if choice.capability.value == capability.reference.value
        )
        if isinstance(parent_model, CampaignSelectionListArtifact)
        else tuple(
            row for row in parent_model.rows if row.capability.value == capability.reference.value
        )
        if isinstance(parent_model, CampaignPortfolioArtifact)
        else ()
    )
    parent_type_is_valid = (
        parent.artifact.artifact_type == CAMPAIGN_ATTRIBUTION_ARTIFACT_TYPE
        and parent.artifact.artifact_version == CAMPAIGN_ATTRIBUTION_ARTIFACT_VERSION
        and isinstance(parent_model, CampaignSelectionListArtifact)
        and parent_model.outcome is CampaignListOutcome.CHOICES
    ) or (
        parent.artifact.artifact_type == CAMPAIGN_PORTFOLIO_ARTIFACT_TYPE
        and parent.artifact.artifact_version == CAMPAIGN_PORTFOLIO_ARTIFACT_VERSION
        and isinstance(parent_model, CampaignPortfolioArtifact)
        and parent_model.outcome.value == "complete"
    )
    if (
        parent.parent_reference is not None
        or not parent_type_is_valid
        or len(matching_choices) != 1
        or matching_choices[0].signature != capability.safe_signature
        or matching_choices[0].capability.expires_at != capability.expires_at
    ):
        raise _selection_unavailable()
    result = runtime.catalog.lookup(
        EntityCatalogLookupRequest(
            scope=runtime.scope,
            reference=EntityReference(canonical_key=capability.campaign),
            kinds=frozenset({EntityKind.CAMPAIGN}),
            window=parent_model.period,
            current_on=None,
            context=EntityContext(
                platform=capability.campaign.platform,
                channel=capability.channel,
                marketplace=capability.marketplace,
            ),
            max_candidates=1,
        )
    )
    aliases = tuple(alias for alias in result.aliases if alias.key == capability.campaign)
    if not aliases:
        raise _selection_unavailable()
    try:
        presented = present_campaign(CampaignPresentationRequest(aliases, parent_model.period))
        current_signature = safe_campaign_signature(
            presented,
            parent_model.period,
            (*runtime.dimension_labels, *runtime.campaign_platform_labels),
        )
    except TypeError, ValueError:
        raise _selection_unavailable() from None
    if (
        presented.channel != capability.channel
        or presented.marketplace != capability.marketplace
        or current_signature != capability.safe_signature
    ):
        raise _selection_unavailable()
    return _ValidatedCampaignCapability(
        capability=capability,
        parent=parent,
        signature=current_signature,
        period=parent_model.period,
        prior_period=_preceding_period(parent_model.period),
    )


def _campaign_measurement_batch(
    selected: _ValidatedCampaignCapability,
    execution: _AnalyticalExecution,
) -> _CampaignMeasurementBatch:
    """Build the shared server-owned attribution cohort for an issued selection."""
    service = _campaign_measurement_service(execution.runtime)
    batch = service.detail_batch(
        CampaignDetailMeasurementInput(
            CampaignMeasurementSelection(
                selected.capability.campaign,
                selected.capability.channel,
                selected.capability.marketplace,
                selected.signature,
            ),
            selected.period,
            selected.prior_period,
        )
    )
    return _CampaignMeasurementBatch(
        batch.prepared,
        (
            CampaignEvidenceSlot.PURCHASES,
            CampaignEvidenceSlot.PRIOR_PURCHASES,
            CampaignEvidenceSlot.ATTRIBUTED_ROAS,
            CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS,
            CampaignEvidenceSlot.SPEND,
            CampaignEvidenceSlot.PRIOR_SPEND,
            CampaignEvidenceSlot.PURCHASE_SERIES,
        ),
    )


def _legacy_campaign_measurement_batch(  # pyright: ignore[reportUnusedFunction]
    selected: _ValidatedCampaignCapability,
    execution: _AnalyticalExecution,
) -> _CampaignMeasurementBatch:
    runtime = execution.runtime
    catalog = runtime.metric_catalog
    if catalog is None:
        raise RuntimeError("campaign measurement catalog is unavailable.")
    filters = {
        "campaign_key": selected.capability.campaign.value,
        "channel_key": selected.capability.channel.value,
        "marketplace_key": selected.capability.marketplace.value,
    }
    computed_at = runtime.clock()
    requests = (
        MeasurementToolRequest(
            tool=MeasurementTool.COMPARE_WINDOWS,
            catalog=catalog,
            contracts=runtime.contracts,
            metric_name="purchases",
            grains=("campaign", "marketplace"),
            window=selected.period,
            prior_window=selected.prior_period,
            computed_at=computed_at,
            correlation_id="presentation-page",
            filters=filters,
            max_rows=DEFAULT_MAX_ROWS,
        ),
        MeasurementToolRequest(
            tool=MeasurementTool.COMPARE_WINDOWS,
            catalog=catalog,
            contracts=runtime.contracts,
            metric_name="roas_attributed_14d",
            grains=("campaign", "marketplace"),
            window=selected.period,
            prior_window=selected.prior_period,
            computed_at=computed_at,
            correlation_id="presentation-page",
            filters=filters,
            max_rows=DEFAULT_MAX_ROWS,
        ),
        MeasurementToolRequest(
            tool=MeasurementTool.COMPARE_WINDOWS,
            catalog=catalog,
            contracts=runtime.contracts,
            metric_name="ad_spend",
            grains=("campaign", "marketplace"),
            window=selected.period,
            prior_window=selected.prior_period,
            computed_at=computed_at,
            correlation_id="presentation-page",
            filters=filters,
            max_rows=DEFAULT_MAX_ROWS,
        ),
        MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=catalog,
            contracts=runtime.contracts,
            metric_name="purchases",
            grains=("date", "campaign", "marketplace"),
            window=selected.period,
            computed_at=computed_at,
            correlation_id="presentation-page",
            filters=filters,
            max_rows=DEFAULT_MAX_ROWS,
        ),
    )
    prepared = prepare_batch(requests)
    slots = (
        CampaignEvidenceSlot.PURCHASES,
        CampaignEvidenceSlot.PRIOR_PURCHASES,
        CampaignEvidenceSlot.ATTRIBUTED_ROAS,
        CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS,
        CampaignEvidenceSlot.SPEND,
        CampaignEvidenceSlot.PRIOR_SPEND,
        CampaignEvidenceSlot.PURCHASE_SERIES,
    )
    expected_metrics = {
        CampaignEvidenceSlot.PURCHASES: "purchases",
        CampaignEvidenceSlot.PRIOR_PURCHASES: "purchases",
        CampaignEvidenceSlot.ATTRIBUTED_ROAS: "roas_attributed_14d",
        CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS: "roas_attributed_14d",
        CampaignEvidenceSlot.SPEND: "ad_spend",
        CampaignEvidenceSlot.PRIOR_SPEND: "ad_spend",
        CampaignEvidenceSlot.PURCHASE_SERIES: "purchases",
    }
    for plan, slot in zip(prepared.plans, slots, strict=True):
        prior = slot in {
            CampaignEvidenceSlot.PRIOR_PURCHASES,
            CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS,
            CampaignEvidenceSlot.PRIOR_SPEND,
        }
        expected_role = (
            PresentationEvidenceRole.COMPARATOR if prior else PresentationEvidenceRole.CURRENT
        )
        expected_group = (
            ("date_key", "campaign_key", "marketplace_key")
            if slot is CampaignEvidenceSlot.PURCHASE_SERIES
            else ("campaign_key", "marketplace_key")
        )
        if (
            plan.role is not expected_role
            or plan.logical.metric.name != expected_metrics[slot]
            or plan.logical.group_by != expected_group
        ):
            raise InvariantError(
                ARTIFACT_MALFORMED,
                "Campaign evidence plans differ from the governed slots.",
                context={"record_kind": "campaign-evidence"},
            )
    return _CampaignMeasurementBatch(prepared, slots)


def _campaign_missing_evidence(
    slot: CampaignEvidenceSlot,
    period: DateRange,
) -> _CampaignSlotAssembly:
    condition = (
        CampaignEvidenceCondition.MISSING_COMPARATOR_PUBLICATION
        if slot
        in {
            CampaignEvidenceSlot.PRIOR_PURCHASES,
            CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS,
            CampaignEvidenceSlot.PRIOR_SPEND,
        }
        else CampaignEvidenceCondition.MISSING_CURRENT_PUBLICATION
    )
    grain = PURCHASE_SERIES_GRAIN if slot is CampaignEvidenceSlot.PURCHASE_SERIES else SUMMARY_GRAIN
    return _CampaignSlotAssembly(
        CampaignSlotEvidence(
            slot=slot,
            condition=condition,
            requested_period=period,
            actual_coverage=(),
            coverage_holes=(),
            units=None,
            grain=grain,
            basis=None,
            suppression=Suppression(False),
            row_count=0,
            truncated=False,
        )
    )


def _campaign_slot_expected_kind(slot: CampaignEvidenceSlot) -> UnitKind:
    if slot in {
        CampaignEvidenceSlot.PURCHASES,
        CampaignEvidenceSlot.PRIOR_PURCHASES,
        CampaignEvidenceSlot.PURCHASE_SERIES,
    }:
        return UnitKind.COUNT
    if slot in {
        CampaignEvidenceSlot.ATTRIBUTED_ROAS,
        CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS,
    }:
        return UnitKind.RATIO
    return UnitKind.CURRENCY


def _campaign_slot_rows(
    request: _CampaignSlotAssemblyRequest,
) -> tuple[CampaignMetricValue | None, tuple[CampaignPurchasePoint, ...], bool]:
    answer = request.answer_slot.answer
    if answer is None:  # pragma: no cover - missing slots are handled before extraction
        return None, (), False
    logical = request.answer_slot.logical
    expected_fields = {*logical.group_by, MEASURE_ALIAS}
    points: list[CampaignPurchasePoint] = []
    metric_value: CampaignMetricValue | None = None
    seen_dates: set[date] = set()
    for row in answer.data.values:
        raw_value = row.get(MEASURE_ALIAS)
        if (
            set(row) != expected_fields
            or row.get("campaign_key") != request.capability.campaign.value
            or row.get("marketplace_key") != request.capability.marketplace.value
            or isinstance(raw_value, bool)
            or not isinstance(raw_value, (int, Decimal))
            or raw_value < 0
        ):
            return None, (), False
        if request.slot is CampaignEvidenceSlot.PURCHASE_SERIES:
            raw_date = row.get("date_key")
            try:
                source_date = date.fromisoformat(raw_date) if isinstance(raw_date, str) else None
            except ValueError:
                source_date = None
            if (
                source_date is None
                or not request.period.start <= source_date <= request.period.end
                or source_date in seen_dates
            ):
                return None, (), False
            seen_dates.add(source_date)
            points.append(CampaignPurchasePoint(source_date, raw_value))
    if request.slot is not CampaignEvidenceSlot.PURCHASE_SERIES:
        if len(answer.data.values) != 1 or answer.data.units is None:
            return None, (), False
        raw_value = answer.data.values[0][MEASURE_ALIAS]
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, Decimal)):
            return None, (), False
        metric_value = CampaignMetricValue(
            raw_value,
            answer.data.units,
            MeasurementBasis.AD_ATTRIBUTED,
            request.period,
        )
    return metric_value, tuple(sorted(points, key=lambda item: item.day)), True


def _campaign_slot_assembly(
    request: _CampaignSlotAssemblyRequest,
) -> _CampaignSlotAssembly:
    answer = request.answer_slot.answer
    if answer is None:
        return _campaign_missing_evidence(request.slot, request.period)
    metadata = answer.metadata
    units = answer.data.units
    basis = metadata.measurement_basis[0] if len(metadata.measurement_basis) == 1 else None
    public_grain = (
        PURCHASE_SERIES_GRAIN
        if request.slot is CampaignEvidenceSlot.PURCHASE_SERIES
        else SUMMARY_GRAIN
    )
    expected_kind = _campaign_slot_expected_kind(request.slot)
    shape_compatible = (
        metadata.requested_coverage_intervals == (request.period,)
        and answer.data.grain.dimensions == request.answer_slot.logical.group_by
        and metadata.row_count == len(answer.data.values)
        and basis is MeasurementBasis.AD_ATTRIBUTED
        and (
            (metadata.row_count == 0 and units is None)
            or (metadata.row_count > 0 and units is not None and units.kind is expected_kind)
        )
        and metadata.freshness_status is not FreshnessStatus.NEVER_LOADED
    )
    metric_value: CampaignMetricValue | None = None
    points: tuple[CampaignPurchasePoint, ...] = ()
    rows_valid = True
    if metadata.row_count and shape_compatible:
        metric_value, points, rows_valid = _campaign_slot_rows(request)
    elif metadata.row_count:
        rows_valid = False
    if metadata.coverage_holes:
        condition = CampaignEvidenceCondition.COVERAGE_HOLE
    elif metadata.suppression.rows_suppressed:
        condition = CampaignEvidenceCondition.SUPPRESSED
    elif metadata.truncated:
        condition = CampaignEvidenceCondition.TRUNCATED
    elif metadata.row_count == 0 and metadata.actual_coverage_intervals == (request.period,):
        condition = CampaignEvidenceCondition.EMPTY_COVERED
    elif (
        not shape_compatible
        or not rows_valid
        or metadata.actual_coverage_intervals != (request.period,)
    ):
        condition = CampaignEvidenceCondition.INCOMPATIBLE_SCOPE
    else:
        condition = CampaignEvidenceCondition.AVAILABLE
    if condition is not CampaignEvidenceCondition.AVAILABLE:
        metric_value = None
        points = ()
    qualifiers = {
        *metadata.qualified_dq,
        *(qualifier.rendering for qualifier in metadata.qualifiers),
    }
    if metadata.freshness_status is not FreshnessStatus.FRESH:
        qualifiers.add(metadata.freshness_status.value)
    return _CampaignSlotAssembly(
        evidence=CampaignSlotEvidence(
            slot=request.slot,
            condition=condition,
            requested_period=request.period,
            actual_coverage=metadata.actual_coverage_intervals,
            coverage_holes=metadata.coverage_holes,
            units=units,
            grain=public_grain,
            basis=basis,
            suppression=metadata.suppression,
            row_count=metadata.row_count,
            truncated=metadata.truncated,
            freshness_qualifiers=tuple(sorted(qualifiers)),
        ),
        metric_value=metric_value,
        purchase_points=points,
    )


def _campaign_delivery_measurement_batch(
    selected: _ValidatedCampaignCapability,
    execution: _AnalyticalExecution,
) -> _CampaignDeliveryMeasurementBatch:
    """Build the shared fixed delivery/DSP cohort for an issued selection."""
    service = _campaign_measurement_service(execution.runtime)
    batch = service.delivery_batch(
        CampaignDetailMeasurementInput(
            CampaignMeasurementSelection(
                selected.capability.campaign,
                selected.capability.channel,
                selected.capability.marketplace,
                selected.signature,
            ),
            selected.period,
            selected.prior_period,
        )
    )
    return _CampaignDeliveryMeasurementBatch(batch.prepared, batch.slots)


def _legacy_campaign_delivery_measurement_batch(  # pyright: ignore[reportUnusedFunction]
    selected: _ValidatedCampaignCapability,
    execution: _AnalyticalExecution,
) -> _CampaignDeliveryMeasurementBatch:
    """Prepare the fixed V2 delivery cohort only after a V2 child is redeemed."""
    runtime = execution.runtime
    if runtime.metric_catalog is None:
        raise RuntimeError("campaign delivery metric catalog is unavailable.")
    filters = {
        "campaign_key": selected.capability.campaign.value,
        "channel_key": selected.capability.channel.value,
        "marketplace_key": selected.capability.marketplace.value,
    }
    computed_at = runtime.clock()
    requests: list[MeasurementToolRequest] = []
    slots: list[tuple[CampaignDeliverySlot | None, str, DateRange]] = []
    for slot in CampaignDeliverySlot:
        requests.append(
            MeasurementToolRequest(
                tool=MeasurementTool.COMPARE_WINDOWS,
                catalog=runtime.metric_catalog,
                contracts=runtime.contracts,
                metric_name=slot.value,
                grains=("campaign", "marketplace"),
                window=selected.period,
                prior_window=selected.prior_period,
                computed_at=computed_at,
                correlation_id="presentation-page",
                filters=filters,
                max_rows=DEFAULT_MAX_ROWS,
            )
        )
        slots.extend(((slot, "current", selected.period), (slot, "prior", selected.prior_period)))
    for slot in CampaignDeliverySlot:
        requests.append(
            MeasurementToolRequest(
                tool=MeasurementTool.METRIC_BY_GRAIN,
                catalog=runtime.metric_catalog,
                contracts=runtime.contracts,
                metric_name=slot.value,
                grains=("date", "campaign", "marketplace"),
                window=selected.period,
                computed_at=computed_at,
                correlation_id="presentation-page",
                filters=filters,
                max_rows=DEFAULT_MAX_ROWS,
            )
        )
        slots.append((slot, "daily", selected.period))
    for metric in (
        "measurable_impressions",
        "viewable_impressions",
        "viewable_impressions_estimated",
        "viewable_rate",
    ):
        requests.append(
            MeasurementToolRequest(
                tool=MeasurementTool.METRIC_BY_GRAIN,
                catalog=runtime.metric_catalog,
                contracts=runtime.contracts,
                metric_name=metric,
                grains=("campaign", "marketplace"),
                window=selected.period,
                computed_at=computed_at,
                correlation_id="presentation-page",
                filters=filters,
                max_rows=DEFAULT_MAX_ROWS,
            )
        )
        slots.append((None, metric, selected.period))
    try:
        prepared = prepare_batch(tuple(requests))
    except (KeyError, ValueError) as error:
        raise RuntimeError("A required Campaign V2 delivery metric is not declared.") from error
    for plan, (slot, role, _) in zip(prepared.plans, slots, strict=True):
        expected_metric = role if slot is None else slot.value
        expected_group = (
            ("date_key", "campaign_key", "marketplace_key")
            if role == "daily"
            else ("campaign_key", "marketplace_key")
        )
        expected_role = (
            PresentationEvidenceRole.COMPARATOR
            if role == "prior"
            else PresentationEvidenceRole.CURRENT
        )
        if (
            plan.role is not expected_role
            or plan.logical.metric.name != expected_metric
            or plan.logical.group_by != expected_group
        ):
            raise InvariantError(
                ARTIFACT_MALFORMED,
                "Campaign V2 delivery plans differ from their governed declaration.",
                context={"record_kind": "campaign-delivery-evidence"},
            )
    return _CampaignDeliveryMeasurementBatch(prepared, tuple(slots))


def _campaign_delivery_expected_kind(slot: CampaignDeliverySlot | None, metric: str) -> UnitKind:
    if metric in {
        "measurable_impressions",
        "viewable_impressions",
        "viewable_impressions_estimated",
    }:
        return UnitKind.COUNT
    if metric in {"viewable_rate", CampaignDeliverySlot.CTR.value}:
        return UnitKind.RATIO
    if slot in {CampaignDeliverySlot.AD_SPEND, CampaignDeliverySlot.CPM}:
        return UnitKind.CURRENCY
    return UnitKind.COUNT


def _delivery_missing(role: str) -> CampaignEvidenceStatusCondition:
    return (
        CampaignEvidenceStatusCondition.MISSING_COMPARATOR_PUBLICATION
        if role in {"prior", "daily_prior"}
        else CampaignEvidenceStatusCondition.MISSING_CURRENT_PUBLICATION
    )


def _campaign_delivery_answer(  # noqa: PLR0911, PLR0912
    request: _CampaignDeliveryAnswerRequest,
) -> CampaignDeliveryAnswer:
    """Accept only exact, complete, basis-compatible delivery evidence; name every refusal."""
    answer_slot = request.answer_slot
    slot = request.slot
    role = request.role
    period = request.period
    capability = request.capability
    refused = partial(CampaignDeliveryAnswer, None)
    answer = answer_slot.answer
    if answer is None:
        return refused(_delivery_missing(role))
    metadata = answer.metadata
    units = answer.data.units
    basis = metadata.measurement_basis[0] if len(metadata.measurement_basis) == 1 else None
    is_daily = role in {"daily_current", "daily_prior"}
    expected_group = (
        ("date_key", "campaign_key", "marketplace_key")
        if is_daily
        else ("campaign_key", "marketplace_key")
    )
    expected_kind = _campaign_delivery_expected_kind(slot, role if slot is None else slot.value)
    if metadata.freshness_status is FreshnessStatus.NEVER_LOADED:
        return refused(_delivery_missing(role))
    if (
        metadata.requested_coverage_intervals != (period,)
        or metadata.actual_coverage_intervals != (period,)
        or metadata.coverage_holes
    ):
        return refused(
            CampaignEvidenceStatusCondition.COVERAGE_HOLE,
            missing_dates=uncovered_days(
                period,
                tuple(metadata.actual_coverage_intervals),
                tuple(metadata.coverage_holes),
            ),
        )
    if metadata.suppression.rows_suppressed:
        return refused(CampaignEvidenceStatusCondition.SUPPRESSED)
    if metadata.truncated:
        return refused(CampaignEvidenceStatusCondition.TRUNCATED)
    if (
        answer.data.grain.dimensions != expected_group
        or basis is not MeasurementBasis.AD_ATTRIBUTED
        or units is None
        or units.kind is not expected_kind
        or metadata.row_count != len(answer.data.values)
    ):
        return refused(CampaignEvidenceStatusCondition.INCOMPATIBLE_SCOPE)
    if is_daily and metadata.row_count < period.days:
        returned = {row.get("date_key") for row in answer.data.values}
        return refused(
            CampaignEvidenceStatusCondition.INCOMPLETE_DAILY_SERIES,
            days_present=metadata.row_count,
            days_expected=period.days,
            missing_dates=tuple(
                day
                for offset in range(period.days)
                if (day := period.start + timedelta(days=offset)).isoformat() not in returned
            ),
        )
    if is_daily and metadata.row_count > period.days:
        return refused(CampaignEvidenceStatusCondition.INCOMPATIBLE_SCOPE)
    if not is_daily and metadata.row_count == 0:
        return refused(CampaignEvidenceStatusCondition.EMPTY_COVERED)
    if not is_daily and metadata.row_count != 1:
        return refused(CampaignEvidenceStatusCondition.INCOMPATIBLE_SCOPE)
    rows: list[tuple[date, CampaignNumber]] = []
    numeric_value: CampaignNumber | None = None
    expected_fields = {*expected_group, MEASURE_ALIAS}
    for row in answer.data.values:
        raw_value = row.get(MEASURE_ALIAS)
        if (
            set(row) != expected_fields
            or row.get("campaign_key") != capability.campaign.value
            or row.get("marketplace_key") != capability.marketplace.value
            or isinstance(raw_value, bool)
            or not isinstance(raw_value, (int, Decimal))
            or raw_value < 0
        ):
            return refused(CampaignEvidenceStatusCondition.INCOMPATIBLE_SCOPE)
        numeric_value = raw_value
        if is_daily:
            raw_day = row.get("date_key")
            try:
                day = date.fromisoformat(raw_day) if isinstance(raw_day, str) else None
            except ValueError:
                day = None
            if day is None or not period.start <= day <= period.end:
                return refused(CampaignEvidenceStatusCondition.INCOMPATIBLE_SCOPE)
            rows.append((day, numeric_value))
    if is_daily:
        if tuple(day for day, _ in sorted(rows)) != tuple(
            period.start + timedelta(days=offset) for offset in range(period.days)
        ):
            return refused(CampaignEvidenceStatusCondition.INCOMPATIBLE_SCOPE)
        return CampaignDeliveryAnswer(
            tuple(sorted(rows)), CampaignEvidenceStatusCondition.AVAILABLE
        )
    if numeric_value is None:
        return refused(CampaignEvidenceStatusCondition.INCOMPATIBLE_SCOPE)
    return CampaignDeliveryAnswer(
        CampaignMetricValue(numeric_value, units, MeasurementBasis.AD_ATTRIBUTED, period),
        CampaignEvidenceStatusCondition.AVAILABLE,
    )


def _campaign_delivery(
    selected: _ValidatedCampaignCapability,
    execution: _AnalyticalExecution,
) -> CampaignDelivery:
    """Reduce V2-only governed delivery evidence without mutating the V1 artifact."""
    runtime = execution.runtime
    inspector = runtime.presentation_evidence
    tooling = runtime.measurements
    if inspector is None or tooling is None:
        raise RuntimeError("campaign delivery evidence runtime is unavailable.")
    batch = _campaign_delivery_measurement_batch(selected, execution)
    inspection = inspector.inspect(
        PresentationEvidenceInspectionRequest(
            plans=tuple(
                PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                for plan in batch.prepared.plans
            ),
            contracts=batch.prepared.contracts,
            now=batch.prepared.now,
        )
    )
    executed = execute_presentation_batch(
        batch.prepared,
        inspection,
        MeasurementExecutionTooling(
            queries=tooling.queries,
            query_batch_max_concurrency=tooling.query_batch_max_concurrency,
        ),
    )
    answers: dict[tuple[CampaignDeliverySlot | None, str], CampaignDeliveryAnswer] = {}
    for answer, (slot, role, period) in zip(executed.slots, batch.slots, strict=True):
        answers[(slot, role)] = _campaign_delivery_answer(
            _CampaignDeliveryAnswerRequest(
                answer_slot=answer,
                slot=slot,
                role=role,
                period=period,
                capability=selected.capability,
            )
        )
    for role in ("current", "prior", "daily_current", "daily_prior"):
        answers[(CampaignDeliverySlot.CPC, role)] = derive_campaign_cpc(
            answers[(CampaignDeliverySlot.AD_SPEND, role)],
            answers[(CampaignDeliverySlot.CLICKS, role)],
        )
    values: dict[
        tuple[CampaignDeliverySlot | None, str],
        CampaignMetricValue | tuple[tuple[date, CampaignNumber], ...] | None,
    ] = {
        key: answer.value if answer.condition is CampaignEvidenceStatusCondition.AVAILABLE else None
        for key, answer in answers.items()
    }
    summary = tuple(
        CampaignDeliveryValue(slot, cast("CampaignMetricValue | None", values[(slot, "current")]))
        for slot in CampaignDeliverySlot
    )
    comparison = campaign_delivery_comparison(
        summary,
        tuple(
            cast("CampaignMetricValue | None", values[(slot, "prior")])
            for slot in CampaignDeliverySlot
        ),
        CHANGE_SEMANTICS,
    )
    daily_current = {
        slot: cast(
            "tuple[tuple[date, CampaignNumber], ...] | None",
            values[(slot, "daily_current")],
        )
        for slot in CampaignDeliverySlot
    }
    daily_prior = {
        slot: cast(
            "tuple[tuple[date, CampaignNumber], ...] | None",
            values[(slot, "daily_prior")],
        )
        for slot in CampaignDeliverySlot
    }
    complete_core = (
        all(value.value is not None for value in (*summary, *comparison))
        and all(points is not None for points in daily_current.values())
        and all(points is not None for points in daily_prior.values())
    )
    diverged: set[tuple[CampaignDeliverySlot, str]] = set()
    if complete_core:
        summary_by_slot = {
            value.slot: cast("CampaignMetricValue", value.value) for value in summary
        }
        comparison_by_slot = {
            value.slot: cast("CampaignMetricValue", value.value) for value in comparison
        }
        for daily_role, daily_series, aggregates_by_slot in (
            ("daily_current", daily_current, summary_by_slot),
            ("daily_prior", daily_prior, comparison_by_slot),
        ):
            for slot in (
                CampaignDeliverySlot.IMPRESSIONS,
                CampaignDeliverySlot.CLICKS,
            ):
                daily_total = sum(
                    (
                        Decimal(value)
                        for _, value in cast(
                            "tuple[tuple[date, CampaignNumber], ...]", daily_series[slot]
                        )
                    ),
                    start=Decimal(0),
                )
                if daily_total != Decimal(aggregates_by_slot[slot].value):
                    complete_core = False
                    diverged.add((slot, daily_role))
    rows: tuple[dict[str, object], ...] = ()
    if complete_core:
        rows = tuple(
            {
                "current": {
                    slot.value: next(
                        value
                        for day, value in cast(
                            "tuple[tuple[date, CampaignNumber], ...]", daily_current[slot]
                        )
                        if day == selected.period.start + timedelta(days=offset)
                    )
                    for slot in CampaignDeliverySlot
                },
                "date": (selected.period.start + timedelta(days=offset)).isoformat(),
                "prior": {
                    slot.value: next(
                        value
                        for day, value in cast(
                            "tuple[tuple[date, CampaignNumber], ...]", daily_prior[slot]
                        )
                        if day == selected.prior_period.start + timedelta(days=offset)
                    )
                    for slot in CampaignDeliverySlot
                },
            }
            for offset in range(selected.period.days)
        )
    # DSP media quality is measured only for Amazon DSP delivery: another channel's batch
    # requests none, so its quality is typed absent for the channel, never an evidence gap.
    # Read from the batch itself, so the reduction indexes only slots the batch requested.
    quality_measured = any(slot is None for slot, _, _ in batch.slots)
    quality_values: dict[str, CampaignMetricValue | None] = (
        {
            metric: cast("CampaignMetricValue | None", values[(None, metric)])
            for metric in DSP_QUALITY_METRICS
        }
        if quality_measured
        else dict.fromkeys(DSP_QUALITY_METRICS)
    )
    quality_available = quality_measured and all(
        value is not None for value in quality_values.values()
    )
    quality = CampaignDspQuality(
        outcome="available" if quality_available else "unavailable",
        measurable_impressions=(
            quality_values["measurable_impressions"] if quality_available else None
        ),
        viewable_impressions=(
            quality_values["viewable_impressions"] if quality_available else None
        ),
        viewable_impressions_estimated=(
            quality_values["viewable_impressions_estimated"] if quality_available else None
        ),
        viewable_rate=quality_values["viewable_rate"] if quality_available else None,
        reason_code=(
            None
            if quality_available
            else "dsp_quality_evidence_unavailable"
            if quality_measured
            else DSP_QUALITY_NOT_MEASURED_FOR_CHANNEL
        ),
    )
    unavailable = tuple(CampaignDeliveryValue(slot, None) for slot in CampaignDeliverySlot)
    status_role = {
        "current": "current",
        "prior": "prior",
        "daily_current": "daily",
        "daily_prior": "prior_daily",
    }
    statuses = tuple(
        campaign_evidence_status(
            CampaignEvidenceStatusRequest(
                slot.value,
                status_role[role],
                (
                    CampaignEvidenceStatusCondition.SERIES_TOTAL_DIVERGENCE
                    if (slot, role) in diverged
                    else answers[(slot, role)].condition
                ),
                days_present=answers[(slot, role)].days_present,
                days_expected=answers[(slot, role)].days_expected,
                missing_dates=answers[(slot, role)].missing_dates,
            )
        )
        for slot in CampaignDeliverySlot
        for role in ("current", "prior", "daily_current", "daily_prior")
    )
    outcome = (
        ("complete" if quality_available else "qualified") if complete_core else "insufficient"
    )
    if outcome == "insufficient":
        # ADR-072: one structured event names every refused (slot, role) — enums and safe
        # counts only, never identifiers or row values.
        report_operation(
            OperationLog(
                operation="presentation.campaign_delivery",
                component="presentation_api",
                phase="delivery_reduction",
                outcome="insufficient",
                severity=LogSeverity.INFO,
                details={
                    "refused": [
                        {
                            "slot": status.slot,
                            "role": status.role,
                            "condition": status.condition.value,
                            "days_present": status.days_present,
                            "days_expected": status.days_expected,
                        }
                        for status in statuses
                        if status.condition is not CampaignEvidenceStatusCondition.AVAILABLE
                    ],
                },
            ),
            LOGGER,
        )
    return CampaignDelivery(
        outcome=outcome,
        summary=summary if complete_core else unavailable,
        comparison_period=selected.prior_period,
        comparison=comparison if complete_core else unavailable,
        series_points=rows,
        exact_rows=rows,
        dsp_quality=quality,
        evidence=statuses,
        recovery=CampaignRecoveryAction.CHANGE_PERIOD.value,
    )


def _campaign_detail_page(
    execution: _AnalyticalExecution,
    schema: AnalyticalPageSchema,
) -> _AnalyticalPageExecutionResult:
    runtime = execution.runtime
    inspector = runtime.presentation_evidence
    tooling = runtime.measurements
    if inspector is None or tooling is None:
        raise RuntimeError("campaign presentation evidence runtime is unavailable.")
    selected = _validated_campaign_capability(execution)
    batch = _campaign_measurement_batch(selected, execution)
    inspection = inspector.inspect(
        PresentationEvidenceInspectionRequest(
            plans=tuple(
                PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                for plan in batch.prepared.plans
            ),
            contracts=batch.prepared.contracts,
            now=batch.prepared.now,
        )
    )
    executed = execute_presentation_batch(
        batch.prepared,
        inspection,
        MeasurementExecutionTooling(
            queries=tooling.queries,
            query_batch_max_concurrency=tooling.query_batch_max_concurrency,
        ),
    )
    period_by_slot = {
        slot: (
            selected.prior_period
            if slot
            in {
                CampaignEvidenceSlot.PRIOR_PURCHASES,
                CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS,
                CampaignEvidenceSlot.PRIOR_SPEND,
            }
            else selected.period
        )
        for slot in batch.slots
    }
    assembled = {
        slot: _campaign_slot_assembly(
            _CampaignSlotAssemblyRequest(
                slot,
                answer,
                period_by_slot[slot],
                selected.capability,
            )
        )
        for slot, answer in zip(batch.slots, executed.slots, strict=True)
    }
    summary = build_campaign_summary(
        CampaignSummaryBuildRequest(
            selected.period,
            assembled[CampaignEvidenceSlot.PURCHASES].metric_value,
            assembled[CampaignEvidenceSlot.ATTRIBUTED_ROAS].metric_value,
            assembled[CampaignEvidenceSlot.SPEND].metric_value,
        )
    )
    prior_summary = build_campaign_summary(
        CampaignSummaryBuildRequest(
            selected.prior_period,
            assembled[CampaignEvidenceSlot.PRIOR_PURCHASES].metric_value,
            assembled[CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS].metric_value,
            assembled[CampaignEvidenceSlot.PRIOR_SPEND].metric_value,
        )
    )
    series_slot = assembled[CampaignEvidenceSlot.PURCHASE_SERIES]
    purchase_series = (
        build_campaign_purchase_series(
            CampaignPurchaseSeriesBuildRequest(
                selected.period,
                series_slot.purchase_points,
                cast("Units", series_slot.evidence.units),
                cast("MeasurementBasis", series_slot.evidence.basis),
            )
        )
        if series_slot.evidence.condition is CampaignEvidenceCondition.AVAILABLE
        else None
    )
    try:
        comparison = build_campaign_prior_comparison(summary, prior_summary, CHANGE_SEMANTICS)
    except ValueError:
        comparison = None
    detail = build_campaign_attribution_detail(
        CampaignAttributionDetailRequest(
            selection=selected.signature,
            period=selected.period,
            summary=summary,
            purchase_series=purchase_series,
            evidence_by_slot=tuple(
                assembled[slot].evidence
                for slot in (
                    CampaignEvidenceSlot.PURCHASES,
                    CampaignEvidenceSlot.ATTRIBUTED_ROAS,
                    CampaignEvidenceSlot.SPEND,
                    CampaignEvidenceSlot.PURCHASE_SERIES,
                    CampaignEvidenceSlot.PRIOR_PURCHASES,
                    CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS,
                    CampaignEvidenceSlot.PRIOR_SPEND,
                )
            ),
            prior_period=selected.prior_period,
            comparison=comparison,
        )
    )
    artifact = PageArtifact(
        CAMPAIGN_ATTRIBUTION_ARTIFACT_TYPE,
        CAMPAIGN_ATTRIBUTION_ARTIFACT_VERSION,
        canonical_artifact_json(campaign_page_artifact_payload(detail)),
    )
    parent_temporal = selected.parent.temporal_control
    snapshot = PageContextSnapshot(
        response_identity="campaign-attribution-detail",
        visible_window=selected.period,
        reporting_window_status=selected.capability.reporting_window_status,
        comparison_window=selected.prior_period,
        campaign=selected.capability.campaign,
        channel=selected.capability.channel,
        marketplace=selected.capability.marketplace,
        # Snapshot v2 labels (ADR-120) only for detail served under a v2 Campaigns parent.
        labels=(
            None
            if parent_temporal is None
            else _page_context_labels(
                "campaigns",
                parent_temporal,
                channel=selected.signature.channel,
                campaign=_campaign_display_name(selected.signature),
            )
        ),
    )
    return _AnalyticalPageExecutionResult(
        schema=schema,
        page=None,
        snapshot=snapshot,
        artifact=artifact,
        parent_reference=selected.capability.parent_artifact_reference,
        maximum_expiry=min(selected.capability.expires_at, selected.parent.expires_at),
    )


def _marketplace_evidence_issue() -> InvariantError:
    return InvariantError(
        MARKETPLACE_EVIDENCE_MALFORMED,
        "Marketplace comparison evidence is malformed.",
        context={"record_kind": "marketplace-comparison-evidence"},
    )


def _marketplace_measurement_batch(
    execution: _AnalyticalExecution,
    schema: AnalyticalPageSchema,
    snapshot: PageContextSnapshot,
) -> _MarketplaceMeasurementBatch:
    runtime = execution.runtime
    catalog = runtime.metric_catalog
    comparison = snapshot.comparison_window
    if catalog is None or comparison is None:
        raise RuntimeError("marketplace comparison runtime is unavailable.")
    expected_metrics = (
        MarketplaceMetric.AD_SPEND,
        MarketplaceMetric.RETAIL_REVENUE_SHIPPED,
    )
    if tuple(metric.name for metric in schema.metrics) != tuple(
        metric.value for metric in expected_metrics
    ) or any(metric.variants for metric in schema.metrics):
        raise RuntimeError(
            "marketplace comparison metrics differ from the governed artifact contract."
        )
    requests = tuple(
        MeasurementToolRequest(
            tool=MeasurementTool.COMPARE_WINDOWS,
            catalog=catalog,
            contracts=runtime.contracts,
            metric_name=metric.value,
            grains=("marketplace",),
            window=snapshot.visible_window,
            prior_window=comparison,
            computed_at=runtime.clock(),
            correlation_id="presentation-page",
            filters=_snapshot_filters(snapshot),
            max_rows=DEFAULT_MAX_ROWS,
        )
        for metric in expected_metrics
    )
    prepared = prepare_batch(requests)
    slots = tuple(
        (metric, role)
        for metric in expected_metrics
        for role in (MarketplacePeriodRole.CURRENT, MarketplacePeriodRole.COMPARATOR)
    )
    if len(prepared.plans) != len(slots):
        raise _marketplace_evidence_issue()
    for plan, (metric, role) in zip(prepared.plans, slots, strict=True):
        expected_role = (
            PresentationEvidenceRole.CURRENT
            if role is MarketplacePeriodRole.CURRENT
            else PresentationEvidenceRole.COMPARATOR
        )
        expected_period = (
            snapshot.visible_window if role is MarketplacePeriodRole.CURRENT else comparison
        )
        if (
            plan.role is not expected_role
            or plan.logical.metric.name != metric.value
            or plan.logical.group_by != ("marketplace_key",)
            or plan.logical.window != expected_period
        ):
            raise _marketplace_evidence_issue()
    return _MarketplaceMeasurementBatch(prepared, slots)


def _inspect_marketplace_measurements(
    batch: _MarketplaceMeasurementBatch,
    runtime: PresentationRuntime,
) -> PresentationEvidenceInspectionResult:
    inspector = runtime.presentation_evidence
    if inspector is None:
        raise RuntimeError("marketplace comparison evidence runtime is unavailable.")
    return inspector.inspect(
        PresentationEvidenceInspectionRequest(
            plans=tuple(
                PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                for plan in batch.prepared.plans
            ),
            contracts=batch.prepared.contracts,
            now=batch.prepared.now,
        )
    )


def _marketplace_rows(
    slot: PresentationMeasurementQuerySlot,
) -> dict[str, Money]:
    result = slot.result
    if result is None:
        return {}
    logical = slot.logical
    if (
        logical.group_by != ("marketplace_key",)
        or logical.carried_columns != ("currency_code",)
        or logical.grain.dimensions != ("marketplace_key",)
        or len(logical.measures) != 1
    ):
        raise ValueError("marketplace comparison evidence changed the governed grain.")
    expected_fields = {
        "marketplace_key",
        "currency_code",
        *(measure_alias(index) for index in range(len(logical.measures))),
    }
    rows: dict[str, Money] = {}
    for raw_row in result.rows:
        key = raw_row.get("marketplace_key")
        currency = raw_row.get("currency_code")
        raw_value = raw_row.get(measure_alias(0))
        if (
            set(raw_row) != expected_fields
            or not isinstance(key, str)
            or not key.strip()
            or not isinstance(currency, str)
            or re.fullmatch(CURRENCY_PATTERN, currency) is None
            or isinstance(raw_value, bool)
            or not isinstance(raw_value, (str, int, Decimal))
            or key in rows
        ):
            raise ValueError("marketplace comparison row shape is malformed.")
        try:
            rows[key] = Money(Decimal(str(raw_value)), currency)
        except InvalidOperation, ValueError:
            raise ValueError("marketplace comparison row value or identity is malformed.") from None
    return rows


def _marketplace_actual_coverage(coverage: Coverage) -> tuple[DateRange, ...]:
    return coverage.intervals or (() if coverage.window is None else (coverage.window,))


def _marketplace_condition(
    slot: PresentationMeasurementQuerySlot,
    period: DateRange,
    represented: bool,
) -> MarketplaceEvidenceCondition:
    coverage = slot.coverage
    result = slot.result
    if coverage is None or result is None:
        raise ValueError("present marketplace evidence lacks attested execution.")
    metric = slot.logical.metric
    basis = metric.measurement_basis
    if (
        len(basis) != 1
        or coverage.measurement_basis is not basis[0]
        or "marketplace" not in coverage.valid_grains
        or coverage.freshness is FreshnessStatus.NEVER_LOADED
        or slot.qualified_dq
    ):
        raise ValueError("marketplace comparison publication scope is malformed.")
    actual_coverage = _marketplace_actual_coverage(coverage)
    return (
        MarketplaceEvidenceCondition.COVERAGE_HOLE
        if coverage.holes
        else MarketplaceEvidenceCondition.SUPPRESSED
        if result.suppression.rows_suppressed
        else MarketplaceEvidenceCondition.TRUNCATED
        if result.truncated
        else MarketplaceEvidenceCondition.STALE
        if coverage.freshness is FreshnessStatus.STALE
        else MarketplaceEvidenceCondition.QUARANTINED
        if coverage.freshness is FreshnessStatus.QUARANTINED
        else MarketplaceEvidenceCondition.INCOMPATIBLE_SCOPE
        if actual_coverage != (period,)
        else MarketplaceEvidenceCondition.AVAILABLE
        if represented
        else MarketplaceEvidenceCondition.MISSING
    )


def _marketplace_period_evidence(
    request: _MarketplacePeriodEvidenceRequest,
) -> MarketplacePeriodEvidence:
    slot = request.slot
    period = request.period
    key = request.marketplace_key
    rows = request.rows
    metric = slot.logical.metric
    basis = metric.measurement_basis
    if (
        len(basis) != 1
        or metric.unit_kind is not UnitKind.CURRENCY
        or not slot.logical.source_timezone.strip()
        or not metric.business_calendar.value.strip()
    ):
        raise ValueError("marketplace comparison metric semantics are malformed.")
    source_time = MarketplaceSourceTime(
        source_timezone=slot.logical.source_timezone,
        business_calendar=metric.business_calendar.value,
    )
    if slot.result is None:
        return MarketplacePeriodEvidence(
            condition=MarketplaceEvidenceCondition.MISSING_PUBLICATION,
            freshness_status=None,
            requested_period=period,
            actual_coverage=(),
            coverage_holes=(),
            value=None,
            units=None,
            grain=MARKETPLACE_GRAIN,
            basis=basis[0],
            source=metric.source_system,
            source_time=source_time,
            suppression=Suppression(False),
            row_count=0,
            truncated=False,
        )
    coverage = slot.coverage
    if coverage is None:
        raise ValueError("marketplace comparison execution lost publication coverage.")
    represented = key in rows
    condition = _marketplace_condition(slot, period, represented)
    row = rows.get(key)
    value = None if row is None else row.amount
    units = None if row is None else Units(UnitKind.CURRENCY, row.currency_code)
    if condition in {
        MarketplaceEvidenceCondition.MISSING,
        MarketplaceEvidenceCondition.SUPPRESSED,
    }:
        value = None
        units = None
    return MarketplacePeriodEvidence(
        condition=condition,
        freshness_status=coverage.freshness,
        requested_period=period,
        actual_coverage=_marketplace_actual_coverage(coverage),
        coverage_holes=coverage.holes,
        value=value,
        units=units,
        grain=MARKETPLACE_GRAIN,
        basis=basis[0],
        source=metric.source_system,
        source_time=source_time,
        suppression=slot.result.suppression,
        row_count=1 if represented else 0,
        truncated=slot.result.truncated,
    )


def _marketplace_comparison_page(
    execution: _AnalyticalExecution,
    schema: AnalyticalPageSchema,
    snapshot: PageContextSnapshot,
) -> _AnalyticalPageExecutionResult:
    comparison = snapshot.comparison_window
    if comparison is None:
        raise RuntimeError("marketplace comparison period is unavailable.")
    batch = _marketplace_measurement_batch(execution, schema, snapshot)
    tooling = execution.runtime.measurements
    if tooling is None:
        raise RuntimeError("marketplace comparison evidence runtime is unavailable.")
    inspection = _inspect_marketplace_measurements(batch, execution.runtime)
    executed = execute_presentation_query_batch(
        batch.prepared,
        inspection,
        MeasurementExecutionTooling(
            queries=tooling.queries,
            query_batch_max_concurrency=tooling.query_batch_max_concurrency,
        ),
    )
    try:
        row_sets = tuple(_marketplace_rows(slot) for slot in executed.slots)
        keys = tuple(sorted({key for rows in row_sets for key in rows}))
        publication_reasons = tuple(
            reason
            for reason in (
                MarketplaceComparisonReason.MISSING_CURRENT_PUBLICATION,
                MarketplaceComparisonReason.MISSING_COMPARATOR_PUBLICATION,
            )
            if any(
                slot.result is None
                and slot.role
                is (
                    PresentationEvidenceRole.CURRENT
                    if reason is MarketplaceComparisonReason.MISSING_CURRENT_PUBLICATION
                    else PresentationEvidenceRole.COMPARATOR
                )
                for slot in executed.slots
            )
        )
        evidence: list[MarketplaceMetricEvidence] = []
        for metric_index, metric in enumerate(
            (MarketplaceMetric.AD_SPEND, MarketplaceMetric.RETAIL_REVENUE_SHIPPED)
        ):
            current_index = metric_index * 2
            comparator_index = current_index + 1
            current_slot = executed.slots[current_index]
            comparator_slot = executed.slots[comparator_index]
            for key in keys:
                evidence.append(
                    MarketplaceMetricEvidence(
                        marketplace_key=key,
                        metric=metric,
                        current=_marketplace_period_evidence(
                            _MarketplacePeriodEvidenceRequest(
                                current_slot,
                                snapshot.visible_window,
                                key,
                                row_sets[current_index],
                            )
                        ),
                        comparator=_marketplace_period_evidence(
                            _MarketplacePeriodEvidenceRequest(
                                comparator_slot,
                                comparison,
                                key,
                                row_sets[comparator_index],
                            )
                        ),
                    )
                )
        comparison_artifact = build_marketplace_comparison(
            MarketplaceComparisonRequest(
                current_window=snapshot.visible_window,
                comparison_window=comparison,
                evidence=tuple(evidence),
                publication_reasons=publication_reasons,
            )
        )
        labels = tuple(
            label
            for label in execution.runtime.dimension_labels
            if label.selector_field == "marketplace_key" and label.selector_value in keys
        )
        payload = marketplace_comparison_payload(comparison_artifact, labels)
        artifact = PageArtifact(
            MARKETPLACE_COMPARISON_ARTIFACT_TYPE,
            MARKETPLACE_COMPARISON_ARTIFACT_VERSION,
            canonical_artifact_json(payload),
        )
    except KeyError, TypeError, ValueError:
        raise _marketplace_evidence_issue() from None
    return _AnalyticalPageExecutionResult(
        schema=schema,
        page=None,
        snapshot=snapshot,
        artifact=artifact,
    )


def _data_health_page(
    execution: _AnalyticalExecution,
    schema: AnalyticalPageSchema,
    snapshot: PageContextSnapshot,
) -> _AnalyticalPageExecutionResult:
    runtime = execution.runtime
    inspector = runtime.presentation_evidence
    tooling = runtime.measurements
    catalog = runtime.metric_catalog
    if inspector is None or tooling is None or catalog is None:
        raise RuntimeError("data-health presentation evidence runtime is unavailable.")
    if tuple(metric.name for metric in schema.metrics) != (
        DataHealthCheck.REACH_USERS.value,
        DataHealthCheck.AVG_FREQUENCY.value,
    ) or any(metric.variants for metric in schema.metrics):
        raise RuntimeError("data-health metrics differ from the governed artifact contract.")
    computed_at = runtime.clock()
    filters = _snapshot_filters(snapshot)
    requests = tuple(
        MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=catalog,
            contracts=runtime.contracts,
            metric_name=metric.name,
            grains=_page_grains(schema, metric.name),
            window=snapshot.visible_window,
            computed_at=computed_at,
            correlation_id="presentation-page",
            filters=filters,
            max_rows=DEFAULT_MAX_ROWS,
        )
        for metric in schema.metrics
    )
    prepared = prepare_batch(requests)
    inspection = inspector.inspect(
        PresentationEvidenceInspectionRequest(
            plans=tuple(
                PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                for plan in prepared.plans
            ),
            contracts=prepared.contracts,
            now=prepared.now,
        )
    )
    executed = execute_presentation_batch(
        prepared,
        inspection,
        MeasurementExecutionTooling(
            queries=tooling.queries,
            query_batch_max_concurrency=tooling.query_batch_max_concurrency,
        ),
    )
    try:
        detail = _assemble_data_health_detail(
            _DataHealthAssemblyRequest(
                prepared=prepared,
                inspection=inspection,
                executed=executed,
                labels=runtime.dimension_labels,
                period=snapshot.visible_window,
            )
        )
        artifact = PageArtifact(
            DATA_HEALTH_ARTIFACT_TYPE,
            DATA_HEALTH_ARTIFACT_VERSION,
            canonical_artifact_json(data_health_detail_payload(detail)),
        )
    except KeyError, TypeError, ValueError:
        raise _data_health_evidence_issue() from None
    return _AnalyticalPageExecutionResult(
        schema=schema,
        page=None,
        snapshot=snapshot,
        artifact=artifact,
    )


def _data_health_raw_text(row: dict[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("data-health exact source dimension is unavailable.")
    return value


def _data_health_raw_rows(
    slot: PresentationMeasurementAnswerSlot,
) -> tuple[_DataHealthRawRow, ...]:
    answer = slot.answer
    if answer is None:
        return ()
    if (
        slot.logical.group_by != DATA_HEALTH_PLAN_GRAIN
        or answer.data.grain.dimensions != DATA_HEALTH_PLAN_GRAIN
        or answer.data.population_total is not None
    ):
        raise ValueError("data-health evidence changed the exact source grain.")
    expected_units = Units(slot.logical.units)
    if (answer.data.values and answer.data.units != expected_units) or (
        not answer.data.values and answer.data.units is not None
    ):
        raise ValueError("data-health evidence units contradict the governed metric.")
    rows: list[_DataHealthRawRow] = []
    for source_row in answer.data.values:
        row = cast("dict[str, object]", source_row)
        try:
            source_date = date.fromisoformat(_data_health_raw_text(row, "date_key"))
        except ValueError:
            raise ValueError("data-health source date is invalid.") from None
        value = row.get(MEASURE_ALIAS)
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise ValueError("data-health evidence value is unavailable.")
        rows.append(
            _DataHealthRawRow(
                source_date=source_date,
                channel_key=_data_health_raw_text(row, "channel_key"),
                campaign_key=_data_health_raw_text(row, "campaign_key"),
                ad_product_key=_data_health_raw_text(row, "ad_product_key"),
                marketplace_key=_data_health_raw_text(row, "marketplace_key"),
                value=value,
            )
        )
    ordered = tuple(sorted(rows, key=lambda item: item.identity))
    if len({row.identity for row in ordered}) != len(ordered):
        raise ValueError("data-health evidence repeats an exact source slice.")
    return ordered


def _data_health_public_dimensions(
    row: _DataHealthRawRow,
    projection: _DataHealthDimensionProjection,
) -> tuple[tuple[str, str | date], ...]:
    return (
        ("date", row.source_date),
        (
            "channel",
            safe_dimension_label(projection.labels, "channel_key", row.channel_key),
        ),
        ("campaign", projection.campaign_aliases[row.campaign_key]),
        ("ad_product", projection.ad_product_aliases[row.ad_product_key]),
        (
            "marketplace",
            safe_dimension_label(
                projection.labels,
                "marketplace_key",
                row.marketplace_key,
            ),
        ),
    )


def _data_health_evidence_rows(
    check: DataHealthCheck,
    rows: tuple[_DataHealthRawRow, ...],
    projection: _DataHealthDimensionProjection,
) -> tuple[DataHealthEvidenceRow, ...]:
    units = Units(UnitKind.COUNT if check is DataHealthCheck.REACH_USERS else UnitKind.RATIO)
    evidence: list[DataHealthEvidenceRow] = []
    disclosed_dimensions: set[tuple[tuple[str, str | date], ...]] = set()
    for index, row in enumerate(rows, start=1):
        dimensions = _data_health_public_dimensions(row, projection)
        if dimensions in disclosed_dimensions:
            raise ValueError("data-health public dimensions collapse exact source slices.")
        disclosed_dimensions.add(dimensions)
        evidence.append(
            DataHealthEvidenceRow(
                evidence_ref=(
                    projection.reach_references[row.identity]
                    if check is DataHealthCheck.REACH_USERS
                    else f"frequency-{index:04d}"
                ),
                dimensions=dimensions,
                value=row.value,
                units=units,
                reach_denominator_ref=(
                    None
                    if check is DataHealthCheck.REACH_USERS
                    else projection.reach_references[row.identity]
                ),
            )
        )
    return tuple(evidence)


def _data_health_check_request(
    request: _DataHealthCheckAssembly,
) -> DataHealthCheckRequest:
    units = Units(
        UnitKind.COUNT if request.check is DataHealthCheck.REACH_USERS else UnitKind.RATIO
    )
    answer = request.answer
    if answer is None:
        return DataHealthCheckRequest(
            check=request.check,
            source=DATA_HEALTH_SOURCE_LABEL,
            population=DATA_HEALTH_POPULATION_LABEL,
            grain=Grain(DATA_HEALTH_GRAIN),
            units=units,
            requested_period=request.period,
            condition_candidates=(DataHealthBaseCondition.NO_CURRENT_PUBLICATION,),
            actual_coverage=(),
            coverage_holes=(),
            freshness_qualifiers=(),
            suppression=Suppression(False),
            dq=(),
            row_count=0,
            evidence_rows=(),
            loaded_at=None,
            truncated=False,
            reach_slice_compatible=request.reach_slice_compatible,
        )
    metadata = answer.metadata
    if metadata.requested_coverage_intervals != (request.period,):
        raise ValueError("data-health evidence changed the requested period.")
    if metadata.freshness_status is FreshnessStatus.NEVER_LOADED:
        raise ValueError("present data-health evidence cannot be never loaded.")
    candidates: list[DataHealthBaseCondition] = []
    if metadata.coverage_holes:
        candidates.append(DataHealthBaseCondition.COVERAGE_HOLE)
    if metadata.suppression.rows_suppressed:
        candidates.append(DataHealthBaseCondition.SUPPRESSED)
    if metadata.truncated:
        candidates.append(DataHealthBaseCondition.TRUNCATED)
    if metadata.row_count == 0:
        candidates.append(DataHealthBaseCondition.EMPTY_COVERED)
    if not candidates:
        candidates.append(DataHealthBaseCondition.AVAILABLE)
    condition = reduce_data_health_condition(tuple(candidates))
    freshness: list[DataHealthFreshnessQualifier] = []
    dq: tuple[str, ...] = ()
    if condition is DataHealthBaseCondition.AVAILABLE:
        if metadata.freshness_status is FreshnessStatus.STALE:
            freshness.append(DataHealthFreshnessQualifier.STALE)
        elif metadata.freshness_status is FreshnessStatus.QUARANTINED:
            freshness.append(DataHealthFreshnessQualifier.LATEST_ATTEMPT_REJECTED)
        if metadata.qualified_dq:
            freshness.append(DataHealthFreshnessQualifier.QUALIFIED_DQ)
            dq = metadata.qualified_dq
    return DataHealthCheckRequest(
        check=request.check,
        source=DATA_HEALTH_SOURCE_LABEL,
        population=DATA_HEALTH_POPULATION_LABEL,
        grain=Grain(DATA_HEALTH_GRAIN),
        units=units,
        requested_period=request.period,
        condition_candidates=tuple(candidates),
        actual_coverage=metadata.actual_coverage_intervals,
        coverage_holes=metadata.coverage_holes,
        freshness_qualifiers=tuple(freshness),
        suppression=metadata.suppression,
        dq=dq,
        row_count=len(request.evidence_rows),
        evidence_rows=request.evidence_rows,
        loaded_at=metadata.loaded_at,
        truncated=metadata.truncated,
        reach_slice_compatible=request.reach_slice_compatible,
    )


def _data_health_evidence_digest(
    prepared: PreparedMeasurementBatch,
    inspection: PresentationEvidenceInspectionResult,
) -> str:
    evidence: list[dict[str, object]] = []
    for planned, inspected in zip(prepared.plans, inspection.slots, strict=True):
        publications = () if inspected.bound is None else inspected.bound.all_publications
        evidence.append(
            {
                "metric": planned.logical.metric.name,
                "present": inspected.present,
                "publications": [
                    {
                        "checksum": publication.content_checksum,
                        "partition": publication.partition.identity,
                        "publication": publication.publication_id.value,
                        "published_at": publication.published_at.isoformat(),
                    }
                    for publication in publications
                ],
                "role": planned.role.value,
            }
        )
    encoded = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _assemble_data_health_detail(
    request: _DataHealthAssemblyRequest,
) -> DataHealthDetailArtifact:
    slots = request.executed.slots
    if len(slots) != len(DataHealthCheck) or tuple(slot.logical.metric.name for slot in slots) != (
        DataHealthCheck.REACH_USERS.value,
        DataHealthCheck.AVG_FREQUENCY.value,
    ):
        raise ValueError("data-health evidence slots differ from the governed checks.")
    reach_rows, frequency_rows = tuple(_data_health_raw_rows(slot) for slot in slots)
    all_rows = (*reach_rows, *frequency_rows)
    campaign_aliases = {
        value: f"Campaign {index:04d}"
        for index, value in enumerate(
            sorted({row.campaign_key for row in all_rows}),
            start=1,
        )
    }
    ad_product_aliases = {
        value: f"Ad product {index:04d}"
        for index, value in enumerate(
            sorted({row.ad_product_key for row in all_rows}),
            start=1,
        )
    }
    reach_references = {
        identity: f"reach-{index:04d}"
        for index, identity in enumerate(
            sorted({row.identity for row in all_rows}),
            start=1,
        )
    }
    projection = _DataHealthDimensionProjection(
        campaign_aliases=campaign_aliases,
        ad_product_aliases=ad_product_aliases,
        reach_references=reach_references,
        labels=request.labels,
    )
    reach_evidence = _data_health_evidence_rows(
        DataHealthCheck.REACH_USERS,
        reach_rows,
        projection,
    )
    frequency_evidence = _data_health_evidence_rows(
        DataHealthCheck.AVG_FREQUENCY,
        frequency_rows,
        projection,
    )
    reach_identities = {row.identity for row in reach_rows}
    compatible = all(row.identity in reach_identities for row in frequency_rows)
    reach_request = _data_health_check_request(
        _DataHealthCheckAssembly(
            check=DataHealthCheck.REACH_USERS,
            answer=slots[0].answer,
            evidence_rows=reach_evidence,
            period=request.period,
        )
    )
    frequency_request = _data_health_check_request(
        _DataHealthCheckAssembly(
            check=DataHealthCheck.AVG_FREQUENCY,
            answer=slots[1].answer,
            evidence_rows=frequency_evidence,
            period=request.period,
            reach_slice_compatible=compatible,
        )
    )
    provenance = DataHealthServingProvenance(
        source=DATA_HEALTH_SOURCE_LABEL,
        population=DATA_HEALTH_POPULATION_LABEL,
        evidence_digest=_data_health_evidence_digest(
            request.prepared,
            request.inspection,
        ),
        inspection_attested=True,
    )
    return build_data_health_detail(
        DataHealthDetailRequest(
            period=request.period,
            serving_provenance=provenance,
            reach=reach_request,
            frequency=frequency_request,
        )
    )


def _data_health_evidence_issue() -> InvariantError:
    return InvariantError(
        DATA_HEALTH_EVIDENCE_MALFORMED,
        "Data-health evidence does not align with the governed page.",
        context={"record_kind": "data-health-detail"},
    )


def _channel_measurement_batch(
    request: _ChannelMeasurementBatchRequest,
) -> _ChannelMeasurementBatch:
    schema = request.schema
    runtime = request.runtime
    snapshot = request.snapshot
    catalog = runtime.metric_catalog
    if catalog is None:
        raise RuntimeError("channel measurement catalog is unavailable.")
    requests: list[MeasurementToolRequest] = []
    slots: list[tuple[str, tuple[MetricRole, ...], str | None]] = []
    computed_at = runtime.clock()
    filters = _snapshot_filters(snapshot)
    for metric in schema.metrics:
        for variant in metric.variants or (None,):
            variant_filters = {} if variant is None else dict(variant.selectors)
            requests.append(
                MeasurementToolRequest(
                    tool=(
                        MeasurementTool.METRIC_BY_GRAIN
                        if request.daily
                        else MeasurementTool.COMPARE_WINDOWS
                    ),
                    catalog=catalog,
                    contracts=runtime.contracts,
                    metric_name=metric.name,
                    grains=(
                        ("date", "channel", "marketplace")
                        if request.daily
                        else _page_grains(schema, metric.name)
                    ),
                    window=snapshot.visible_window,
                    prior_window=(None if request.daily else snapshot.comparison_window),
                    computed_at=computed_at,
                    correlation_id="presentation-page",
                    filters={**filters, **variant_filters},
                    max_rows=DEFAULT_MAX_ROWS,
                )
            )
            slots.append(
                (
                    metric.name,
                    metric.roles,
                    None if variant is None else variant.variant_id,
                )
            )
    return _ChannelMeasurementBatch(schema, tuple(requests), tuple(slots))


def _execute_channel_measurements(
    batch: _ChannelMeasurementBatch,
    runtime: PresentationRuntime,
) -> PresentationMeasurementExecutionResult:
    inspector = runtime.presentation_evidence
    tooling = runtime.measurements
    if inspector is None or tooling is None:
        raise RuntimeError("channel presentation evidence runtime is unavailable.")
    prepared = prepare_batch(batch.requests)
    inspection = inspector.inspect(
        PresentationEvidenceInspectionRequest(
            plans=tuple(
                PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                for plan in prepared.plans
            ),
            contracts=prepared.contracts,
            now=prepared.now,
        )
    )
    return execute_presentation_batch(
        prepared,
        inspection,
        MeasurementExecutionTooling(
            queries=tooling.queries,
            query_batch_max_concurrency=tooling.query_batch_max_concurrency,
        ),
    )


def _channel_period_page(
    batch: _ChannelMeasurementBatch,
    executed: PresentationMeasurementExecutionResult,
    role: PresentationEvidenceRole,
) -> AnalyticalPageResult | None:
    schema = batch.schema
    selected = tuple(slot for slot in executed.slots if slot.role is role)
    if len(selected) != len(batch.slots):
        raise InvariantError(
            ARTIFACT_MALFORMED,
            "Channel evidence slots do not align with the governed page.",
            context={"record_kind": "channel-evidence"},
        )
    if any(slot.answer is None for slot in selected):
        return None
    measurements = tuple(
        PageMeasurementResult.from_answer(
            PageMeasurementResultRequest(
                metric_name,
                roles,
                variant_id,
                cast("ShapedResponse", answer_slot.answer),
            )
        )
        for (metric_name, roles, variant_id), answer_slot in zip(
            batch.slots,
            selected,
            strict=True,
        )
    )
    return AnalyticalPageResult(schema, measurements)


def _channel_parent_page(
    execution: _AnalyticalExecution,
    schema: AnalyticalPageSchema,
    snapshot: PageContextSnapshot,
) -> _AnalyticalPageExecutionResult:
    if snapshot.comparison_window is None:
        raise RuntimeError("channel comparison period is unavailable.")
    batch = _channel_measurement_batch(
        _ChannelMeasurementBatchRequest(
            schema=schema,
            runtime=execution.runtime,
            snapshot=snapshot,
            daily=False,
        )
    )
    executed = _execute_channel_measurements(batch, execution.runtime)
    current_page = _channel_period_page(
        batch,
        executed,
        PresentationEvidenceRole.CURRENT,
    )
    comparison_page = _channel_period_page(
        batch,
        executed,
        PresentationEvidenceRole.COMPARATOR,
    )
    current = channel_attribution_robustness(
        ChannelRobustnessRequest(
            page=current_page,
            period=snapshot.visible_window,
            missing_publication=current_page is None,
        )
    )
    comparison = channel_attribution_robustness(
        ChannelRobustnessRequest(
            page=comparison_page,
            period=snapshot.comparison_window,
            missing_publication=comparison_page is None,
        )
    )
    declaration = schema.artifact
    if (
        declaration is None
        or declaration.artifact_type != CHANNEL_ARTIFACT_TYPE
        or declaration.version != CHANNEL_ARTIFACT_VERSION
        or declaration.outcomes != ("robust_leader", "model_sensitive", "insufficient")
    ):
        raise RuntimeError("channel robustness artifact declaration is unavailable.")
    robustness = channel_attribution_robustness_v2(current, comparison)
    artifact = PageArtifact(
        artifact_type=declaration.artifact_type,
        artifact_version=declaration.version,
        payload_json=canonical_artifact_json(
            channel_attribution_robustness_v2_payload(
                robustness,
                execution.runtime.dimension_labels,
            )
        ),
    )
    return _AnalyticalPageExecutionResult(
        schema=schema,
        page=current_page,
        snapshot=snapshot,
        artifact=artifact,
        channel_current=current,
    )


def _validated_channel_selection(
    execution: _AnalyticalExecution,
) -> _ValidatedChannelSelection:
    runtime = execution.runtime
    capabilities = runtime.channel_capabilities
    artifacts = runtime.page_artifacts
    reference = execution.request.selection_reference
    if capabilities is None or artifacts is None or reference is None:
        raise RuntimeError("channel capability runtime is unavailable.")
    result = capabilities.read_channel_capability(
        ChannelRowCapabilityReadRequest(
            reference=reference,
            verified_owner_id=execution.owner,
            conversation_session_id=execution.request.session_id,
            at=runtime.clock(),
        )
    )
    capability = result.capability
    if result.outcome is not ResolutionOutcome.RESOLVED or capability is None:
        raise _channel_selection_unavailable()
    parent_result = artifacts.read_artifact(
        PageArtifactReadRequest(
            reference=capability.parent_artifact_reference,
            verified_owner_id=execution.owner,
            conversation_session_id=execution.request.session_id,
            at=runtime.clock(),
        )
    )
    parent = parent_result.artifact
    if parent_result.outcome is not ResolutionOutcome.RESOLVED or parent is None:
        raise _channel_selection_unavailable()
    try:
        decoded = decode_channel_v2_parent(parent.artifact.payload)
    except TypeError, ValueError:
        raise _channel_selection_unavailable() from None
    safe_channel = safe_dimension_label(
        runtime.dimension_labels,
        "channel_key",
        capability.channel.value,
    )
    safe_marketplace = safe_dimension_label(
        runtime.dimension_labels,
        "marketplace_key",
        capability.marketplace.value,
    )
    valid_rows = sum(row.public_label == safe_channel for row in decoded.current_rows) == 1
    if (
        parent.parent_reference is not None
        or parent.artifact.artifact_type != capability.parent_artifact_type
        or parent.artifact.artifact_version != capability.parent_artifact_version
        or parent.artifact.artifact_type != CHANNEL_ARTIFACT_TYPE
        or parent.artifact.artifact_version != CHANNEL_ARTIFACT_VERSION
        or capability.model_ids != MODEL_IDS
        or decoded.current_period != capability.current_period
        or decoded.models != MODEL_IDS
        or decoded.marketplace != safe_marketplace
        or not valid_rows
    ):
        raise _channel_selection_unavailable()
    return _ValidatedChannelSelection(
        capability=capability,
        parent=parent,
        comparison_window=decoded.comparison_period,
        currency=decoded.currency,
        safe_channel_label=safe_channel,
    )


def _selected_channel_page(
    execution: _AnalyticalExecution,
    schema: AnalyticalPageSchema,
    selected: _ValidatedChannelSelection | None = None,
) -> _AnalyticalPageExecutionResult:
    selected = selected or _validated_channel_selection(execution)
    capability = selected.capability
    snapshot = PageContextSnapshot(
        response_identity="amazon-channel-performance-selection",
        visible_window=capability.current_period,
        reporting_window_status=capability.reporting_window_status,
        comparison_window=selected.comparison_window,
        channel=capability.channel,
        marketplace=capability.marketplace,
    )
    batch = _channel_measurement_batch(
        _ChannelMeasurementBatchRequest(
            schema=schema,
            runtime=execution.runtime,
            snapshot=snapshot,
            daily=True,
        )
    )
    executed = _execute_channel_measurements(batch, execution.runtime)
    daily = _channel_daily_movement(selected, batch, executed)
    parent_payload = selected.parent.artifact.payload
    child_payload = {
        **parent_payload,
        "selected_channel_daily": channel_daily_movement_payload(
            daily,
            execution.runtime.dimension_labels,
        ),
    }
    if any(
        child_payload[key] != parent_payload[key]
        for key in ("boundaries", "comparison", "current", "period_relation")
    ):
        raise InvariantError(
            ARTIFACT_MALFORMED,
            "Channel child artifact changed parent period evidence.",
            context={"record_kind": "channel-child"},
        )
    artifact = PageArtifact(
        artifact_type=selected.parent.artifact.artifact_type,
        artifact_version=selected.parent.artifact.artifact_version,
        payload_json=canonical_artifact_json(child_payload),
    )
    return _AnalyticalPageExecutionResult(
        schema=schema,
        page=None,
        snapshot=snapshot,
        artifact=artifact,
        parent_reference=capability.parent_artifact_reference,
        maximum_expiry=min(capability.expires_at, selected.parent.expires_at),
    )


def _channel_daily_movement(
    selected: _ValidatedChannelSelection,
    batch: _ChannelMeasurementBatch,
    executed: PresentationMeasurementExecutionResult,
) -> ChannelDailyMovement:
    capability = selected.capability
    answers = tuple(slot.answer for slot in executed.slots)
    qualifiers = tuple(
        sorted(
            {
                qualifier.rendering
                for answer in answers
                if answer is not None
                for qualifier in answer.metadata.qualifiers
            }
        )
    )
    if any(answer is None for answer in answers):
        return reduce_channel_daily_movement(
            ChannelDailyMovementRequest(
                channel_key=capability.channel.value,
                period=capability.current_period,
                marketplace_key=capability.marketplace.value,
                currency=selected.currency,
                days=(),
                hard_reasons=(ChannelDailyReasonCode.MISSING_PUBLICATION,),
                qualifiers=qualifiers,
            )
        )

    shaped = tuple(cast("ShapedResponse", answer) for answer in answers)
    hard_reasons: set[ChannelDailyReasonCode] = set()
    value_maps: list[dict[date, Decimal | None]] = []
    states: list[AvailabilityState] = []
    for index, (slot, answer) in enumerate(zip(executed.slots, shaped, strict=True)):
        descriptor = batch.slots[index]
        state = PageMeasurementResult.from_answer(
            PageMeasurementResultRequest(*descriptor, answer)
        ).availability
        states.append(state)
        units = answer.data.units
        expected_kind = UnitKind.CURRENCY if index == 0 else UnitKind.RATIO
        if (
            answer.data.grain.dimensions != slot.logical.group_by
            or answer.metadata.requested_coverage_intervals != (capability.current_period,)
            or not answer.metadata.actual_coverage_intervals
            or answer.metadata.coverage_holes
            or units is None
            or units.kind is not expected_kind
            or (index == 0 and units.currency_code != selected.currency)
        ):
            hard_reasons.add(ChannelDailyReasonCode.INCOMPATIBLE_SCOPE)
        values: dict[date, Decimal | None] = {}
        expected_fields = {*slot.logical.group_by, "value"}
        for row in answer.data.values:
            raw_date = row.get("date_key")
            raw_channel = row.get("channel_key")
            raw_marketplace = row.get("marketplace_key")
            raw_value = row.get("value")
            try:
                source_date = date.fromisoformat(raw_date) if isinstance(raw_date, str) else None
            except ValueError:
                source_date = None
            if (
                set(row) != expected_fields
                or source_date is None
                or not capability.current_period.start
                <= source_date
                <= capability.current_period.end
                or raw_channel != capability.channel.value
                or raw_marketplace != capability.marketplace.value
                or source_date in values
                or isinstance(raw_value, bool)
                or not isinstance(raw_value, (Decimal, int, type(None)))
            ):
                hard_reasons.add(ChannelDailyReasonCode.INCOMPATIBLE_SCOPE)
                continue
            values[source_date] = None if raw_value is None else Decimal(raw_value)
        value_maps.append(values)

    days: list[ChannelDailyDay] = []
    if ChannelDailyReasonCode.INCOMPATIBLE_SCOPE not in hard_reasons:
        source_dates = sorted({source_date for values in value_maps for source_date in values})
        for source_date in source_dates:
            spend_value = _daily_value(value_maps[0], states[0], source_date)
            model_values: list[ChannelDailyModelResult] = []
            for model_id, values, state in zip(
                MODEL_IDS,
                value_maps[1:],
                states[1:],
                strict=True,
            ):
                cell = _daily_value(values, state, source_date)
                model_values.append(ChannelDailyModelResult(model_id, cell.value, cell.evidence))
            days.append(ChannelDailyDay(source_date, spend_value, tuple(model_values)))
    return reduce_channel_daily_movement(
        ChannelDailyMovementRequest(
            channel_key=capability.channel.value,
            period=capability.current_period,
            marketplace_key=capability.marketplace.value,
            currency=selected.currency,
            days=tuple(days),
            hard_reasons=tuple(sorted(hard_reasons, key=str)),
            qualifiers=qualifiers,
        )
    )


def _daily_value(
    values: dict[date, Decimal | None],
    state: AvailabilityState,
    source_date: date,
) -> ChannelDailyMetricValue:
    present = source_date in values
    evidence = (
        state if present or state is AvailabilityState.SUPPRESSED else AvailabilityState.EMPTY
    )
    value = values.get(source_date)
    if evidence in {AvailabilityState.EMPTY, AvailabilityState.SUPPRESSED}:
        value = None
    return ChannelDailyMetricValue(value, evidence)


def _issue_v2_channel_parent_capabilities(
    runtime: PresentationRuntime,
    parent: PageArtifactRecord,
    execution: _AnalyticalPageExecutionResult,
) -> tuple[_IssuedChannelCapability, ...]:
    """Atomically issue one V2 Channel parent root and every row capability."""
    issuance = runtime.parent_capability_issuance
    if issuance is None:
        raise RuntimeError("parent capability issuance storage is unavailable.")
    issued = _prepared_channel_capabilities(runtime, parent, execution)
    result = issuance.issue_parent_capabilities(
        ParentCapabilityIssuanceRequest(parent, tuple(item.record for item in issued))
    )
    if result.outcome is ParentCapabilityIssuanceOutcome.CAPACITY_REFUSED:
        raise DomainError(
            PRESENTATION_UNAVAILABLE,
            "The page has too many available row selections.",
        )
    if not result.issued:
        raise RuntimeError("parent capability issuance did not complete.")
    return issued


def _issue_channel_capabilities(
    runtime: PresentationRuntime,
    parent: PageArtifactRecord,
    execution: _AnalyticalPageExecutionResult,
) -> tuple[_IssuedChannelCapability, ...]:
    """Issue legacy Channel row capabilities after the parent artifact is durable."""
    store = runtime.channel_capabilities
    if store is None:
        raise RuntimeError("channel capability storage is unavailable.")
    issued = _prepared_channel_capabilities(runtime, parent, execution)
    for item in issued:
        store.put_channel_capability(item.record)
    return issued


def _prepared_channel_capabilities(
    runtime: PresentationRuntime,
    parent: PageArtifactRecord,
    execution: _AnalyticalPageExecutionResult,
) -> tuple[_IssuedChannelCapability, ...]:
    """Build the complete Channel capability set before any V2 custody write."""
    current = execution.channel_current
    if current is None:
        raise RuntimeError("channel capability evidence is unavailable.")
    if (
        current.outcome is ChannelRobustnessOutcome.INSUFFICIENT
        or current.marketplace_key is None
        or current.currency is None
        or not current.rows
    ):
        return ()
    labels = tuple(
        safe_dimension_label(runtime.dimension_labels, "channel_key", row.channel_key)
        for row in current.rows
    )
    if len(set(labels)) != len(labels):
        raise ValueError("channel capability labels must identify unique public rows.")
    return tuple(
        _IssuedChannelCapability(
            ChannelRowCapabilityRecord(
                reference=PageContextReference(runtime.token_minter()),
                parent_artifact_reference=parent.reference,
                parent_artifact_type=parent.artifact.artifact_type,
                parent_artifact_version=parent.artifact.artifact_version,
                verified_owner_id=parent.verified_owner_id,
                conversation_session_id=parent.conversation_session_id,
                expires_at=parent.expires_at,
                channel=ChannelKey(row.channel_key),
                marketplace=MarketplaceKey(current.marketplace_key),
                current_period=current.period,
                model_ids=MODEL_IDS,
                reporting_window_status=execution.snapshot.reporting_window_status,
            ),
            safe_label,
        )
        for row, safe_label in zip(current.rows, labels, strict=True)
    )


def _channel_capability_json(capability: _IssuedChannelCapability) -> dict[str, str]:
    return {
        "expires_at": capability.record.expires_at.astimezone(UTC).isoformat(),
        "safe_channel_label": capability.safe_channel_label,
        "selection_reference": capability.record.reference.value,
    }


def _analytical_snapshot(
    execution: _AnalyticalExecution,
) -> PageContextSnapshot:
    operation = execution.operation
    request = execution.request
    if request.reporting_window_status is None:
        raise RuntimeError("analytical reporting window status is unavailable.")
    return PageContextSnapshot(
        response_identity=ANALYTICAL_PAGES[operation],
        visible_window=cast("DateRange", request.visible_window),
        reporting_window_status=request.reporting_window_status,
        comparison_window=request.comparison_window,
    )


def _page_grains(schema: AnalyticalPageSchema, metric_name: str) -> tuple[str, ...]:
    if schema.grain.time is PageTime.EXACT_SOURCE:
        exact_source_grains = {
            "avg_frequency": DATA_HEALTH_GRAIN,
            "reach_users": DATA_HEALTH_GRAIN,
        }
        try:
            return exact_source_grains[metric_name]
        except KeyError:
            raise RuntimeError(
                f"{schema.page_id}: exact-source metric {metric_name} has no declared plan grain."
            ) from None
    return schema.grain.dimensions


def _presentation_grain(schema: AnalyticalPageSchema) -> tuple[str, ...]:
    return (schema.grain.time.value, *schema.grain.dimensions)


def _snapshot_filters(snapshot: PageContextSnapshot) -> dict[str, str]:
    filters: dict[str, str] = {}
    if snapshot.campaign is not None:
        filters["campaign_key"] = snapshot.campaign.value
    if snapshot.product is not None:
        filters["amazon_asin"] = snapshot.product.value
    if snapshot.channel is not None:
        filters["channel_key"] = snapshot.channel.value
    if snapshot.marketplace is not None:
        filters["marketplace_key"] = snapshot.marketplace.value
    return filters


def _analytical_page_json(
    execution: _AnalyticalPageExecutionResult,
    dimension_labels: tuple[PresentationDimensionLabel, ...],
    artifact_record: PageArtifactRecord | None = None,
) -> dict[str, object]:
    schema = execution.schema
    page = execution.page
    artifact = execution.artifact
    actions = [
        {"action_id": action.action_id, "target_page": action.target_page}
        for action in schema.actions
    ]
    if artifact is not None:
        if artifact_record is None or artifact_record.artifact != artifact:
            raise RuntimeError("analytical artifact revision is unavailable.")
        if artifact.artifact_type == CHANNEL_ARTIFACT_TYPE:
            kind = "channel_attribution_robustness"
        elif artifact.artifact_type == MARKETPLACE_COMPARISON_ARTIFACT_TYPE:
            kind = "marketplace_comparison"
        elif artifact.artifact_type == DATA_HEALTH_ARTIFACT_TYPE:
            kind = "data_health_detail"
        elif artifact.artifact_type == CAMPAIGN_PORTFOLIO_ARTIFACT_TYPE:
            kind = campaign_portfolio_artifact_from_payload(artifact.payload).kind
        elif artifact.artifact_type == CAMPAIGN_ATTRIBUTION_ARTIFACT_TYPE:
            campaign = campaign_page_artifact_from_payload(artifact.payload)
            kind = campaign.kind.value
        else:  # pragma: no cover - analytical producers are a closed server-owned set
            raise RuntimeError("analytical artifact type is unavailable.")
        page_json: dict[str, object] = {
            "actions": actions,
            "artifact": artifact.payload,
            "artifact_revision": artifact_record.reference.value,
            "artifact_type": artifact.artifact_type,
            "artifact_version": artifact.artifact_version,
            "kind": kind,
            "page_id": schema.page_id,
        }
        if artifact_record.parent_reference is not None:
            page_json["parent_revision"] = artifact_record.parent_reference.value
        return page_json
    if page is None:
        raise RuntimeError("analytical page measurements are unavailable.")
    return {
        "actions": actions,
        "boundary_required": schema.boundary_required,
        "kind": "analytical_page",
        "measurements": [
            {
                "answer": gateway.to_json_safe(
                    project_presentation_answer(
                        measurement.answer,
                        _presentation_grain(schema),
                        dimension_labels,
                    )
                ),
                "availability": measurement.availability.value,
                "metric_name": measurement.metric_name,
                "roles": [role.value for role in measurement.roles],
                "variant_id": measurement.variant_id,
            }
            for measurement in page.measurements
        ],
        "mode": schema.mode.value,
        "page_id": schema.page_id,
        "sections": [
            {
                "cardinality": section.cardinality.value,
                "section_id": section.section_id,
            }
            for section in schema.sections
        ],
    }


def _campaign_histories(
    aliases: tuple[EntityAlias, ...],
) -> dict[CampaignKey, tuple[EntityAlias, ...]]:
    grouped: dict[CampaignKey, list[EntityAlias]] = {}
    for alias in aliases:
        if alias.kind is not EntityKind.CAMPAIGN or not isinstance(alias.key, CampaignKey):
            continue
        grouped.setdefault(alias.key, []).append(alias)
    return {key: tuple(history) for key, history in grouped.items()}


def _window_json(window: DateRange | None) -> dict[str, str] | None:
    if window is None:
        return None
    return {"end": window.end.isoformat(), "start": window.start.isoformat()}


def _json(status: int, body: dict[str, object]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
            "Pragma": "no-cache",
        },
        "body": json.dumps(body),
    }


def _mint_token() -> str:
    return secrets.token_urlsafe(32)


SETTINGS = settings_without_client_secret()
TRACING = configure_tracing(tracing_settings())
ROOT = contracts_root()
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
INSIGHT_CONTRACTS = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
METRIC_CATALOG = load_catalog(ROOT)
ATTRIBUTION_MODELS = load_attribution_models(ROOT)
PAGE_SCHEMAS = load_page_schemas(ROOT)
PRESENTATION_PAGE_SCHEMAS = load_v2_page_schemas(ROOT)
# With the v2 declaration, as `make validate-page-schemas` runs it: a v1 slot that declares
# periods (New customers' step 2) is valid only beside its v2 section (ADR-100 point 7).
PAGE_SCHEMA_FINDINGS = validate_page_schemas(
    PAGE_SCHEMAS,
    PageValidationContext(METRIC_CATALOG, ATTRIBUTION_MODELS),
    PRESENTATION_PAGE_SCHEMAS,
)
if PAGE_SCHEMA_FINDINGS:
    raise RuntimeError("governed analytical page schemas are invalid.")
# ADR-121: a compared metric without `favourable_direction` refuses the cold start.
CHANGE_SEMANTICS = presentation_change_semantics(METRIC_CATALOG)
PRESENTATION_LABELS = presentation_labels(METRIC_CATALOG)
SOURCE_PACKS = load_source_packs(ROOT)
COMMERCE_BRIDGE = load_commerce_identity_bridge(ROOT)
MEDIA_TAXONOMY = load_media_taxonomy(ROOT)
CAMPAIGN_PLATFORM_LABELS = campaign_platform_labels(MEDIA_TAXONOMY)
CHANNEL_KEYS = frozenset(
    term.key.value for term in MEDIA_TAXONOMY.terms if isinstance(term.key, ChannelKey)
)
CAMPAIGN_FILTER_IDS = frozenset({*MULTI_CHANNEL_FILTER_IDS, *CHANNEL_KEYS})
"""Every channel filter id Campaigns may declare; a workspace offers the ones present."""
DIMENSION_LABELS = presentation_dimension_labels(MEDIA_TAXONOMY, COMMERCE_BRIDGE)
CURRENT_PUBLICATIONS = DynamoCurrentPublications(SETTINGS.semantic_publications_table)
ENTITY_CATALOG = CanonicalEntityCatalog(
    EntityCatalogTooling(
        current=CURRENT_PUBLICATIONS,
        canonical=S3CanonicalAdapter(SETTINGS.semantic_v2_bucket),
        packs=SOURCE_PACKS,
        contracts=CONTRACTS,
        bridge=COMMERCE_BRIDGE,
        taxonomy=MEDIA_TAXONOMY,
    )
)
PAGE_CONTEXT = DynamoPageContextStore(SETTINGS.page_context_table)
PRESET_ARTIFACT_POINTERS = (
    DynamoPresetArtifactPointerStore(SETTINGS.preset_artifact_pointer_table)
    if SETTINGS.preset_artifact_bucket and SETTINGS.preset_artifact_pointer_table
    else None
)
PRESET_ARTIFACT_BODIES = (
    S3PresetArtifactBodyStore(SETTINGS.preset_artifact_bucket)
    if PRESET_ARTIFACT_POINTERS is not None
    else None
)
PRESET_ARTIFACT_CUSTODY = (
    S3PresetArtifactCustodyStore(SETTINGS.preset_artifact_bucket)
    if PRESET_ARTIFACT_POINTERS is not None
    else None
)
_GRAPH_TABLE = graph_consumer_settings().execution_graph_table
GRAPH_SERVING = DynamoPresentationGraphReader(_GRAPH_TABLE) if _GRAPH_TABLE else None
INSIGHT_CACHE = (
    DynamoInsightCache(SETTINGS.insight_cache_table)
    if SETTINGS.insight_cache_table
    and SETTINGS.insight_quick_model_id
    and SETTINGS.insight_deep_model_id
    else None
)
INSIGHT_PROMPTS = (
    DynamoInsightPromptRegistry(SETTINGS.insight_prompt_registry_table)
    if SETTINGS.insight_prompt_registry_table
    and SETTINGS.insight_quick_model_id
    and SETTINGS.insight_deep_model_id
    else None
)
INSIGHT_CONTINUATION = (
    SqsInsightContinuation(SETTINGS.insight_continuation_queue_url)
    if SETTINGS.insight_continuation_queue_url
    else None
)
INSIGHT_ASSEMBLER = (
    InsightSlotAssembler(
        cache=INSIGHT_CACHE,
        prompts=INSIGHT_PROMPTS,
        generator=BedrockInsightGenerator(
            InsightModelRouteModels(
                SETTINGS.insight_quick_model_id,
                SETTINGS.insight_deep_model_id,
            ),
            SETTINGS.insight_generation_timeout_seconds,
        ),
        max_concurrency=SETTINGS.insight_generation_max_concurrency,
        continuation=INSIGHT_CONTINUATION,
        deep_cache=INSIGHT_CACHE,
        generation_timeout_seconds=SETTINGS.insight_generation_timeout_seconds,
    )
    if INSIGHT_CACHE is not None and INSIGHT_PROMPTS is not None
    else None
)
PUBLICATION_SELECTOR = PublicationSelector(
    CURRENT_PUBLICATIONS,
    SOURCE_PACKS,
    COMMERCE_BRIDGE,
)
QUERIES = AthenaQueryAdapter(
    AthenaQueryTarget(
        workgroup=SETTINGS.athena_workgroup,
        database=SETTINGS.semantic_v2_current_database,
        reuse_minutes=SETTINGS.athena_result_reuse_max_age_minutes,
        wait=INTERACTIVE_WAIT,
        results=ResultTransport.PAGED,
    )
)
MEASUREMENTS = MeasurementTooling(
    queries=QUERIES,
    publications=PUBLICATION_SELECTOR,
    query_batch_max_concurrency=SETTINGS.athena_query_batch_max_concurrency,
    pack_coverage=PUBLICATION_SELECTOR,
    rosters=SpendRosterReader(
        catalog=METRIC_CATALOG,
        contracts=CONTRACTS,
        publications=PUBLICATION_SELECTOR,
        queries=QUERIES,
        labels=ad_product_labels(MEDIA_TAXONOMY),
        clock=lambda: datetime.now(UTC),
        # A CAMPAIGN roster is named as chat names it, never refused for want of names.
        names=DimensionLabelResolver(
            ENTITY_CATALOG, AuthorizedAnalyticalScope(frozenset({COMMERCE_BRIDGE.commerce_scope}))
        ),
    ),
)
RUNTIME = PresentationRuntime(
    catalog=ENTITY_CATALOG,
    page_context=PAGE_CONTEXT,
    page_artifacts=PAGE_CONTEXT,
    parent_capability_issuance=PAGE_CONTEXT,
    campaign_capabilities=PAGE_CONTEXT,
    channel_capabilities=PAGE_CONTEXT,
    product_capabilities=PAGE_CONTEXT,
    media_scope_catalog=GovernedMediaScopeCatalog(
        MediaScopeCatalogDependencies(
            commerce_bridge=COMMERCE_BRIDGE,
            taxonomy=MEDIA_TAXONOMY,
            current=CURRENT_PUBLICATIONS,
            packs=SOURCE_PACKS,
            contracts=CONTRACTS,
        )
    ),
    presentation_evidence=PUBLICATION_SELECTOR,
    scope=AuthorizedAnalyticalScope(frozenset({COMMERCE_BRIDGE.commerce_scope})),
    schemas=PAGE_SCHEMAS,
    metric_catalog=METRIC_CATALOG,
    contracts=CONTRACTS,
    measurements=MEASUREMENTS,
    windows=PageWindowCatalog(
        CURRENT_PUBLICATIONS,
        SOURCE_PACKS,
        CONTRACTS,
        COMMERCE_BRIDGE,
    ),
    dimension_labels=DIMENSION_LABELS,
    campaign_platform_labels=CAMPAIGN_PLATFORM_LABELS,
    insight_assembler=INSIGHT_ASSEMBLER,
    campaign_measurements=CampaignArtifactMeasurementService(
        CampaignMeasurementDependencies(
            scope=AuthorizedAnalyticalScope(frozenset({COMMERCE_BRIDGE.commerce_scope})),
            catalog=METRIC_CATALOG,
            contracts=CONTRACTS,
            clock=lambda: datetime.now(UTC),
            rosters=MEASUREMENTS.rosters,
        )
    ),
    preset_artifact_bodies=PRESET_ARTIFACT_BODIES,
    preset_artifact_pointers=PRESET_ARTIFACT_POINTERS,
    preset_artifact_custody=PRESET_ARTIFACT_CUSTODY,
    graph_serving=GRAPH_SERVING,
    clock=lambda: datetime.now(UTC),
    token_minter=_mint_token,
)
