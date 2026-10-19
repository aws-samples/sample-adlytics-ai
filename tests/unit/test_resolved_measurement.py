# SPDX-License-Identifier: MIT-0
"""Mapping resolved analytical requests into the existing measurement adapter."""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import cast

import pytest
from publication_support import StubPublications

from adapters.inbound.tool_handlers.gateway import to_json_safe
from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    handle,
)
from adapters.inbound.tool_handlers.resolved_measurement import (
    MeasurementRequestContext,
    SemanticMeasurementContext,
    request_from_semantic_intent,
    to_measurement_tool_request,
)
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import Metric
from domain.data_contracts.contract import DataContract
from domain.measurement.comparison import RankOrder
from domain.measurement.lenses import REPORT_LENSES
from domain.measurement.planning import measure_alias
from domain.semantics.analytical_request import (
    AnalyticalCurrencyMode,
    AnalyticalCurrencyPolicy,
    CandidateAnalyticalRequest,
    DeclarationEvidence,
    EntityIntent,
    EntitySelection,
    FieldResolution,
    PeriodIntent,
    ReportLensReference,
    ResolutionCandidate,
    SelectorReference,
    SourceSelection,
    resolve_analytical_request,
)
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EffectiveLabel,
    EntityAlias,
    EntityContext,
    EntityKind,
    EntityReference,
    FieldOrigin,
    MappingEvidence,
    ResolutionOutcome,
    ResolutionProvenance,
)
from domain.semantics.identity import (
    AdProductKey,
    BrandKey,
    CampaignKey,
    CanonicalKey,
    ChannelGroupKey,
    ChannelKey,
    CommerceScopeKey,
    EcosystemKey,
    MarketplaceKey,
    PlatformKey,
    ProductKey,
    SourceIdentifier,
    SourceSystemKey,
)
from domain.shared_kernel.errors import DomainError, ValidationError
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
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
from ports.outbound.query import QueryRequest, QueryResult

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT)
SCOPE = CommerceScopeKey("amazon_retail_us")
ECOSYSTEM = EcosystemKey("amazon")
PLATFORM = PlatformKey("amazon_ads")
CHANNEL_GROUP = ChannelGroupKey("paid_media")
CHANNEL = ChannelKey("amazon_dsp")
CAMPAIGN = CampaignKey("campaign:fictional", SCOPE, PLATFORM)
AD_PRODUCT = AdProductKey("dsp_display")
MARKETPLACE = MarketplaceKey("amazon_us")
PRODUCT = ProductKey("product:fictional", SCOPE, MARKETPLACE)
CURRENT = DateRange(date(2026, 8, 10), date(2026, 8, 16))
PRIOR = DateRange(date(2026, 8, 3), date(2026, 8, 9))
COMPUTED_AT = datetime(2026, 8, 19, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def contracts() -> tuple[DataContract, ...]:
    return tuple(contract for _path, contract in load_contracts(ROOT))


type SupportedKey = (
    EcosystemKey
    | PlatformKey
    | ChannelGroupKey
    | ChannelKey
    | CampaignKey
    | AdProductKey
    | MarketplaceKey
    | ProductKey
)


@dataclass(frozen=True, slots=True)
class _EntityFixture:
    kind: EntityKind
    key: SupportedKey
    label: str


def _alias(fixture: _EntityFixture) -> EntityAlias:
    return EntityAlias(
        kind=fixture.kind,
        key=fixture.key,
        commerce_scope=SCOPE,
        labels=(EffectiveLabel(fixture.label, date(2026, 1, 1)),),
        source_identifiers=(),
        evidence=MappingEvidence(f"fictional-{fixture.kind.value}-mapping", 1),
        valid_from=date(2026, 1, 1),
        ecosystem=ECOSYSTEM,
        platform=PLATFORM,
        channel_group=CHANNEL_GROUP,
        channel=CHANNEL,
        ad_product=AD_PRODUCT,
        marketplace=MARKETPLACE,
    )


FIXTURES = (
    _EntityFixture(EntityKind.ECOSYSTEM, ECOSYSTEM, "Amazon"),
    _EntityFixture(EntityKind.PLATFORM, PLATFORM, "Amazon Ads"),
    _EntityFixture(EntityKind.CHANNEL_GROUP, CHANNEL_GROUP, "Paid media"),
    _EntityFixture(EntityKind.CHANNEL, CHANNEL, "Amazon DSP"),
    _EntityFixture(EntityKind.CAMPAIGN, CAMPAIGN, "Fictional campaign"),
    _EntityFixture(EntityKind.AD_PRODUCT, AD_PRODUCT, "DSP display"),
    _EntityFixture(EntityKind.MARKETPLACE, MARKETPLACE, "United States"),
    _EntityFixture(EntityKind.PRODUCT, PRODUCT, "Fictional product"),
)
ALIASES = tuple(_alias(fixture) for fixture in FIXTURES)
ENTITIES = tuple(
    EntityIntent(fixture.kind, EntityReference(text=fixture.label)) for fixture in FIXTURES
)


def _candidate() -> CandidateAnalyticalRequest:
    return CandidateAnalyticalRequest(
        catalog=CATALOG,
        today=date(2026, 8, 19),
        scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
        authorized_aliases=ALIASES,
        metric_reference="attributed_revenue_14d",
        operation_reference="breakdown",
        current_period=PeriodIntent(window=CURRENT),
        grains=("channel",),
        entities=ENTITIES,
    )


def _context(contracts: tuple[DataContract, ...]) -> MeasurementRequestContext:
    return MeasurementRequestContext(
        contracts=contracts,
        computed_at=COMPUTED_AT,
        correlation_id="fictional-correlation",
    )


class _EntityCatalog:
    """Pure recording port; no AWS adapter is imported or replaced in these tests."""

    def __init__(
        self,
        results: dict[tuple[EntityKind, str], EntityCatalogLookupResult] | None = None,
    ) -> None:
        self.results = results or {}
        self.calls: list[EntityCatalogLookupRequest] = []

    def browse(self, request: EntityCatalogBrowseRequest) -> EntityCatalogBrowseResult:
        del request
        return EntityCatalogBrowseResult(())

    def lookup(self, request: EntityCatalogLookupRequest) -> EntityCatalogLookupResult:
        self.calls.append(request)
        kind = next(iter(request.kinds))
        reference = request.reference
        value = (
            reference.text
            if reference.text is not None
            else cast("CanonicalKey", reference.canonical_key).value
        )
        return self.results.get((kind, value), EntityCatalogLookupResult(()))

    def public_labels(self, request: PublicEntityLabelRequest) -> PublicEntityLabelResult:
        return PublicEntityLabelResult(
            tuple(
                PublicEntityLabelResolution(PublicEntityLabelOutcome.NOT_FOUND)
                for _ in request.items
            )
        )


class _Queries:
    """Pure query port returning one fictional canonical channel row."""

    def run(self, request: QueryRequest) -> QueryResult:
        del request
        return QueryResult(
            rows=(
                (
                    {
                        "channel_key": "amazon_dsp",
                        measure_alias(0): 12,
                        measure_alias(1): 4,
                    }
                ),
            ),
            bytes_scanned=1_024,
            truncated=False,
            suppression=Suppression(rows_suppressed=False),
        )


def _semantic_context(
    contracts: tuple[DataContract, ...],
    entity_catalog: _EntityCatalog,
) -> SemanticMeasurementContext:
    return SemanticMeasurementContext(
        catalog=CATALOG,
        contracts=contracts,
        entity_catalog=entity_catalog,
        scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
        today=date(2026, 8, 19),
        computed_at=COMPUTED_AT,
        correlation_id="fictional-correlation",
    )


def _semantic_arguments(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "metric_reference": "impressions",
        "grains": ["channel"],
        "period_start_candidate": CURRENT.start.isoformat(),
        "period_end_candidate": CURRENT.end.isoformat(),
    }
    arguments.update(overrides)
    return arguments


def test_mapper_preserves_every_exact_canonical_filter_and_change_ranking(
    contracts: tuple[DataContract, ...],
) -> None:
    resolved = resolve_analytical_request(
        replace(
            _candidate(),
            operation_reference="top increase",
            prior_period=PeriodIntent(window=PRIOR),
            rank_n=7,
        )
    )

    mapped = to_measurement_tool_request(resolved, _context(contracts))

    assert mapped.tool is MeasurementTool.TOP_N
    assert mapped.metric_name == "attributed_revenue_14d"
    assert mapped.grains == ("channel",)
    assert mapped.window == CURRENT
    assert mapped.prior_window == PRIOR
    assert mapped.n == 7
    assert mapped.order_by is RankOrder.CHANGE
    assert mapped.filters == {
        "ecosystem_key": "amazon",
        "platform_key": "amazon_ads",
        "channel_group_key": "paid_media",
        "channel_key": "amazon_dsp",
        "campaign_key": "campaign:fictional",
        "ad_product_key": "dsp_display",
        "marketplace_key": "amazon_us",
        "amazon_asin": "product:fictional",
    }
    assert mapped.defaulted_filters == {}
    expected = (
        ResolutionProvenance(
            "metric", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, "attributed_revenue_14d"
        ),
        ResolutionProvenance("current_period", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, str(CURRENT)),
        ResolutionProvenance("prior_period", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, str(PRIOR)),
        ResolutionProvenance("operation", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, "top_increase (n=7)"),
        ResolutionProvenance("grains", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, "channel"),
        ResolutionProvenance("ad_product", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, AD_PRODUCT.value),
        ResolutionProvenance("campaign", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, CAMPAIGN.value),
        ResolutionProvenance("channel", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, CHANNEL.value),
        ResolutionProvenance(
            "channel_group", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, CHANNEL_GROUP.value
        ),
        ResolutionProvenance("ecosystem", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, ECOSYSTEM.value),
        ResolutionProvenance("marketplace", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, MARKETPLACE.value),
        ResolutionProvenance("platform", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, PLATFORM.value),
        ResolutionProvenance("product", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, PRODUCT.value),
        ResolutionProvenance("currency", FieldOrigin.GOVERNED_DEFAULT, "native"),
    )
    assert mapped.resolution_provenance == expected
    assert resolved.request is not None
    reordered = replace(
        resolved,
        request=replace(resolved.request, entities=tuple(reversed(resolved.request.entities))),
    )
    assert (
        to_measurement_tool_request(reordered, _context(contracts)).resolution_provenance
        == expected
    )


@pytest.mark.covers("ADR-055")
def test_mapper_applies_attribution_selector_once_as_an_explicit_filter(
    contracts: tuple[DataContract, ...],
) -> None:
    model_key = "linear_v1"
    resolved = resolve_analytical_request(
        CandidateAnalyticalRequest(
            catalog=CATALOG,
            today=date(2026, 8, 19),
            scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
            authorized_aliases=(),
            metric_reference="roas_deduplicated_incl_halo_14d",
            operation_reference="breakdown",
            current_period=PeriodIntent(window=CURRENT),
            grains=("channel", "marketplace"),
            selectors=(SelectorReference("attribution_model_key", model_key),),
        )
    )

    mapped = to_measurement_tool_request(resolved, _context(contracts))

    assert mapped.filters == {"attribution_model_key": model_key}
    assert mapped.defaulted_filters == {}
    assert (
        ResolutionProvenance("attribution_model_key", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, model_key)
        in mapped.resolution_provenance
    )


@pytest.mark.covers("ADR-055")
def test_semantic_json_selector_reaches_the_measurement_filter(
    contracts: tuple[DataContract, ...],
) -> None:
    mapped = request_from_semantic_intent(
        MeasurementTool.METRIC_BY_GRAIN,
        _semantic_arguments(
            metric_reference="roas_deduplicated_incl_halo_14d",
            grains=["channel", "marketplace"],
            selectors={"attribution_model_key": "first_touch_v1"},
        ),
        _semantic_context(contracts, _EntityCatalog()),
    )

    assert mapped.filters == {"attribution_model_key": "first_touch_v1"}


@dataclass(frozen=True, slots=True)
class _OperationCase:
    reference: str
    prior: PeriodIntent | None
    n: int | None
    tool: MeasurementTool
    order: RankOrder


@pytest.mark.parametrize(
    "case",
    [
        _OperationCase("breakdown", None, None, MeasurementTool.METRIC_BY_GRAIN, RankOrder.VALUE),
        _OperationCase("trend", None, None, MeasurementTool.METRIC_BY_GRAIN, RankOrder.VALUE),
        _OperationCase(
            "comparison",
            PeriodIntent(window=PRIOR),
            None,
            MeasurementTool.COMPARE_WINDOWS,
            RankOrder.VALUE,
        ),
        _OperationCase("top value", None, 3, MeasurementTool.TOP_N, RankOrder.VALUE),
        _OperationCase(
            "top increase",
            PeriodIntent(window=PRIOR),
            3,
            MeasurementTool.TOP_N,
            RankOrder.CHANGE,
        ),
    ],
)
def test_mapper_preserves_supported_operation_prior_and_rank(
    case: _OperationCase,
    contracts: tuple[DataContract, ...],
) -> None:
    candidate = replace(
        _candidate(),
        operation_reference=case.reference,
        prior_period=case.prior,
        rank_n=case.n,
        grains=("date",) if case.reference == "trend" else ("channel",),
    )
    mapped = to_measurement_tool_request(
        resolve_analytical_request(candidate),
        _context(contracts),
    )

    assert mapped.tool is case.tool
    assert mapped.prior_window == (PRIOR if case.prior is not None else None)
    assert mapped.n == (case.n or 0)
    assert mapped.order_by is case.order


@pytest.mark.covers("ADR-027")
def test_mapper_separates_resolver_applied_lens_defaults_from_explicit_filters(
    contracts: tuple[DataContract, ...],
) -> None:
    retail_entities = tuple(
        intent for intent in ENTITIES if intent.kind in {EntityKind.MARKETPLACE, EntityKind.PRODUCT}
    )
    resolved = resolve_analytical_request(
        CandidateAnalyticalRequest(
            catalog=CATALOG,
            today=date(2026, 8, 19),
            scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
            authorized_aliases=ALIASES,
            metric_reference="retail_revenue_shipped",
            operation_reference="breakdown",
            current_period=PeriodIntent(window=CURRENT),
            grains=("product",),
            entities=retail_entities,
            report_lenses=(ReportLensReference("selling_program", "BUSINESS"),),
        )
    )

    mapped = to_measurement_tool_request(resolved, _context(contracts))

    assert mapped.filters == {
        "marketplace_key": "amazon_us",
        "amazon_asin": "product:fictional",
        "selling_program": "BUSINESS",
    }
    assert mapped.defaulted_filters == {
        "distributor_view": "MANUFACTURING",
        "report_period": "DAY",
    }


def test_unresolved_result_cannot_map_to_measurement(
    contracts: tuple[DataContract, ...],
) -> None:
    unresolved = resolve_analytical_request(
        replace(_candidate(), metric_reference="fictional metric")
    )

    assert unresolved.outcome is ResolutionOutcome.NOT_FOUND
    with pytest.raises(ValueError, match="only a resolved analytical request"):
        to_measurement_tool_request(unresolved, _context(contracts))


def test_semantic_intent_resolves_campaign_name_to_exact_canonical_filter(
    contracts: tuple[DataContract, ...],
) -> None:
    campaign_alias = next(alias for alias in ALIASES if alias.kind is EntityKind.CAMPAIGN)
    entity_catalog = _EntityCatalog(
        {(EntityKind.CAMPAIGN, "Fictional campaign"): EntityCatalogLookupResult((campaign_alias,))}
    )

    mapped = request_from_semantic_intent(
        MeasurementTool.METRIC_BY_GRAIN,
        _semantic_arguments(entity_references={"campaign": "Fictional campaign"}),
        _semantic_context(contracts, entity_catalog),
    )

    assert mapped.filters == {"campaign_key": CAMPAIGN.value}
    assert len(entity_catalog.calls) == 1
    lookup = entity_catalog.calls[0]
    assert lookup.scope == AuthorizedAnalyticalScope(frozenset({SCOPE}))
    assert lookup.kinds == frozenset({EntityKind.CAMPAIGN})
    assert lookup.reference == EntityReference(text="Fictional campaign")
    assert lookup.window == CURRENT
    assert lookup.current_on is None


def test_historical_alias_resolves_in_request_window_without_today_lookup(
    contracts: tuple[DataContract, ...],
) -> None:
    base = next(alias for alias in ALIASES if alias.kind is EntityKind.CAMPAIGN)
    historical = replace(
        base,
        labels=(
            EffectiveLabel(
                "Historical Fictional Campaign",
                date(2026, 1, 1),
                date(2026, 8, 17),
            ),
        ),
        valid_to=date(2026, 8, 17),
    )
    entity_catalog = _EntityCatalog(
        {
            (
                EntityKind.CAMPAIGN,
                "Historical Fictional Campaign",
            ): EntityCatalogLookupResult((historical,))
        }
    )

    mapped = request_from_semantic_intent(
        MeasurementTool.METRIC_BY_GRAIN,
        _semantic_arguments(entity_references={"campaign": "Historical Fictional Campaign"}),
        _semantic_context(contracts, entity_catalog),
    )

    assert mapped.filters == {"campaign_key": CAMPAIGN.value}
    assert len(entity_catalog.calls) == 1
    lookup = entity_catalog.calls[0]
    assert lookup.reference == EntityReference(text="Historical Fictional Campaign")
    assert lookup.window == CURRENT
    assert lookup.current_on is None
    assert lookup.context == EntityContext()


def test_semantic_provenance_reaches_json_without_raw_mapping_or_grounding_exposure(
    contracts: tuple[DataContract, ...],
) -> None:
    source_identifier = SourceIdentifier("private_source_id", "source-id-never-public")
    source_scope = SourceIdentifier("private_scope_id", "source-scope-never-public")
    campaign_alias = next(alias for alias in ALIASES if alias.kind is EntityKind.CAMPAIGN)
    marketplace_alias = next(alias for alias in ALIASES if alias.kind is EntityKind.MARKETPLACE)
    scoped_alias = replace(
        campaign_alias,
        source_identifiers=(source_identifier,),
        source_system=SourceSystemKey("amc"),
        source_scope=source_scope,
        evidence=MappingEvidence("mapping-reference-never-public", 91),
    )
    mapped = request_from_semantic_intent(
        MeasurementTool.METRIC_BY_GRAIN,
        _semantic_arguments(
            entity_references={
                "campaign": "Fictional campaign",
                "marketplace": "United States",
            }
        ),
        _semantic_context(
            contracts,
            _EntityCatalog(
                {
                    (EntityKind.CAMPAIGN, "Fictional campaign"): EntityCatalogLookupResult(
                        (scoped_alias,)
                    ),
                    (EntityKind.MARKETPLACE, "United States"): EntityCatalogLookupResult(
                        (marketplace_alias,)
                    ),
                }
            ),
        ),
    )

    answer = handle(
        mapped,
        MeasurementTooling(queries=_Queries(), publications=StubPublications()),
    )
    without_provenance = handle(
        replace(mapped, resolution_provenance=()),
        MeasurementTooling(queries=_Queries(), publications=StubPublications()),
    )
    encoded = to_json_safe(answer)
    metadata = encoded["metadata"]
    assert metadata["resolution_provenance"] == to_json_safe(mapped.resolution_provenance)
    assert metadata["resolution_provenance"]
    provenance_payload = json.dumps(metadata["resolution_provenance"], sort_keys=True)
    for forbidden in (
        source_identifier.namespace,
        source_identifier.value,
        source_scope.namespace,
        source_scope.value,
        "mapping-reference-never-public",
        SCOPE.value,
        "source_scope",
        "mapping_version",
        "evidence",
        "campaign_key",
    ):
        assert forbidden not in provenance_payload

    grounding = answer.metadata.grounding
    assert grounding is not None
    assert grounding == without_provenance.metadata.grounding
    assert all(
        not path.startswith("metadata.resolution_provenance")
        for rendering in grounding.renderings
        for path in rendering.source_paths
    )


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("ad attributed ROAS", "roas_attributed_14d"),
        ("ROAS", "roas_attributed_14d"),
    ],
)
def test_semantic_intent_maps_explicit_metric_alias_and_governed_default(
    reference: str,
    expected: str,
    contracts: tuple[DataContract, ...],
) -> None:
    mapped = request_from_semantic_intent(
        MeasurementTool.METRIC_BY_GRAIN,
        _semantic_arguments(metric_reference=reference),
        _semantic_context(contracts, _EntityCatalog()),
    )

    assert mapped.metric_name == expected


def test_governed_default_metric_origin_is_mandatory_grounded_context(
    contracts: tuple[DataContract, ...],
) -> None:
    aliases = tuple(
        alias
        for alias in ALIASES
        if alias.kind
        in {
            EntityKind.CAMPAIGN,
            EntityKind.CHANNEL,
            EntityKind.AD_PRODUCT,
            EntityKind.MARKETPLACE,
        }
    )
    names = {
        fixture.kind: fixture.label
        for fixture in FIXTURES
        if fixture.kind in {alias.kind for alias in aliases}
    }
    entity_catalog = _EntityCatalog(
        {(alias.kind, names[alias.kind]): EntityCatalogLookupResult((alias,)) for alias in aliases}
    )
    mapped = request_from_semantic_intent(
        MeasurementTool.METRIC_BY_GRAIN,
        _semantic_arguments(
            metric_reference="ROAS",
            entity_references={kind.value: name for kind, name in names.items()},
        ),
        _semantic_context(contracts, entity_catalog),
    )

    answer = handle(
        mapped,
        MeasurementTooling(queries=_Queries(), publications=StubPublications()),
    )

    metric = next(item for item in mapped.resolution_provenance if item.field == "metric")
    assert metric.origin is FieldOrigin.GOVERNED_DEFAULT
    grounding = answer.metadata.grounding
    assert grounding is not None
    disclosure = next(
        item
        for item in grounding.renderings
        if item.source_paths == ("metadata.resolution_provenance[0].origin",)
    )
    assert disclosure.text == "Metric selection used the governed default."
    assert disclosure.rendering_id in grounding.mandatory_rendering_ids
    serialized = repr(grounding)
    assert metric.value not in serialized


@dataclass(frozen=True, slots=True)
class _SemanticOperationCase:
    tool: MeasurementTool
    overrides: dict[str, object]
    expected_order: RankOrder


@pytest.mark.parametrize(
    "case",
    [
        _SemanticOperationCase(
            MeasurementTool.METRIC_BY_GRAIN,
            {"operation": "BREAKDOWN"},
            RankOrder.VALUE,
        ),
        _SemanticOperationCase(
            MeasurementTool.METRIC_BY_GRAIN,
            {"operation": "TREND", "grains": ["date"]},
            RankOrder.VALUE,
        ),
        _SemanticOperationCase(
            MeasurementTool.COMPARE_WINDOWS,
            {
                "prior_period_start_candidate": PRIOR.start.isoformat(),
                "prior_period_end_candidate": PRIOR.end.isoformat(),
            },
            RankOrder.VALUE,
        ),
        _SemanticOperationCase(
            MeasurementTool.TOP_N,
            {"rank_size": 4, "rank_order": "VALUE"},
            RankOrder.VALUE,
        ),
        _SemanticOperationCase(
            MeasurementTool.TOP_N,
            {
                "rank_size": 4,
                "rank_order": "INCREASE",
                "prior_period_start_candidate": PRIOR.start.isoformat(),
                "prior_period_end_candidate": PRIOR.end.isoformat(),
            },
            RankOrder.CHANGE,
        ),
    ],
)
def test_semantic_intent_maps_each_tool_owned_operation(
    case: _SemanticOperationCase,
    contracts: tuple[DataContract, ...],
) -> None:
    mapped = request_from_semantic_intent(
        case.tool,
        _semantic_arguments(**case.overrides),
        _semantic_context(contracts, _EntityCatalog()),
    )

    assert mapped.tool is case.tool
    assert mapped.order_by is case.expected_order
    assert mapped.n == (4 if case.tool is MeasurementTool.TOP_N else 0)
    assert mapped.prior_window == (
        PRIOR
        if case.tool is MeasurementTool.COMPARE_WINDOWS or case.expected_order is RankOrder.CHANGE
        else None
    )


def test_semantic_intent_maps_an_exact_named_closed_period(
    contracts: tuple[DataContract, ...],
) -> None:
    arguments = _semantic_arguments(period_reference="the 7 days ending yesterday")
    arguments.pop("period_start_candidate")
    arguments.pop("period_end_candidate")

    mapped = request_from_semantic_intent(
        MeasurementTool.METRIC_BY_GRAIN,
        arguments,
        _semantic_context(contracts, _EntityCatalog()),
    )

    assert mapped.window == DateRange(date(2026, 8, 12), date(2026, 8, 18))


@pytest.mark.covers("ADR-027")
def test_semantic_intent_maps_explicit_lens_native_currency_and_default_origins(
    contracts: tuple[DataContract, ...],
) -> None:
    mapped = request_from_semantic_intent(
        MeasurementTool.METRIC_BY_GRAIN,
        _semantic_arguments(
            metric_reference="units ordered",
            grains=["marketplace"],
            report_lenses={"selling_program": "BUSINESS"},
            currency_mode="NATIVE",
        ),
        _semantic_context(contracts, _EntityCatalog()),
    )

    assert mapped.filters == {"selling_program": "BUSINESS"}
    assert mapped.defaulted_filters == {
        "distributor_view": "MANUFACTURING",
        "report_period": "DAY",
    }
    assert mapped.resolution_provenance[-4:] == (
        ResolutionProvenance("distributor_view", FieldOrigin.GOVERNED_DEFAULT, "MANUFACTURING"),
        ResolutionProvenance("report_period", FieldOrigin.GOVERNED_DEFAULT, "DAY"),
        ResolutionProvenance("selling_program", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, "BUSINESS"),
        ResolutionProvenance("currency", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, "native"),
    )


def test_not_found_and_hidden_entity_references_have_the_same_external_refusal(
    contracts: tuple[DataContract, ...],
) -> None:
    arguments = _semantic_arguments(entity_references={"campaign": "Fictional absent campaign"})
    absent_catalog = _EntityCatalog()
    hidden_catalog = _EntityCatalog(
        {
            (
                EntityKind.CAMPAIGN,
                "Fictional absent campaign",
            ): EntityCatalogLookupResult((), hidden_direct_match=True)
        }
    )

    with pytest.raises(ValidationError) as absent:
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            arguments,
            _semantic_context(contracts, absent_catalog),
        )
    with pytest.raises(ValidationError) as hidden:
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            arguments,
            _semantic_context(contracts, hidden_catalog),
        )

    assert absent.value.code == hidden.value.code
    assert absent.value.description == hidden.value.description
    assert len(absent_catalog.calls) == len(hidden_catalog.calls) == 1


def test_ambiguous_entity_refusal_projects_labels_without_resolution_internals(
    contracts: tuple[DataContract, ...],
) -> None:
    first = next(alias for alias in ALIASES if alias.kind is EntityKind.CAMPAIGN)
    second = _alias(
        _EntityFixture(
            EntityKind.CAMPAIGN,
            CampaignKey("campaign:other-fictional", SCOPE, PLATFORM),
            "Fictional campaign",
        )
    )
    entity_catalog = _EntityCatalog(
        {(EntityKind.CAMPAIGN, "Fictional campaign"): EntityCatalogLookupResult((first, second))}
    )

    with pytest.raises(ValidationError) as refused:
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            _semantic_arguments(entity_references={"campaign": "Fictional campaign"}),
            _semantic_context(contracts, entity_catalog),
        )

    visible = refused.value.description
    assert "Fictional campaign" in visible
    assert CAMPAIGN.value not in visible
    assert "campaign:other-fictional" not in visible
    assert SCOPE.value not in visible
    assert "fictional-campaign-mapping" not in visible


def test_truncated_entity_lookup_requires_clarification_before_mapping(
    contracts: tuple[DataContract, ...],
) -> None:
    campaign_alias = next(alias for alias in ALIASES if alias.kind is EntityKind.CAMPAIGN)
    entity_catalog = _EntityCatalog(
        {
            (EntityKind.CAMPAIGN, "Fictional campaign"): EntityCatalogLookupResult(
                (campaign_alias,), truncated=True
            )
        }
    )

    with pytest.raises(ValidationError, match="too many authorized candidates"):
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            _semantic_arguments(entity_references={"campaign": "Fictional campaign"}),
            _semantic_context(contracts, entity_catalog),
        )

    assert len(entity_catalog.calls) == 1


@pytest.mark.parametrize(
    "bad_arguments",
    [
        {"entity_references": {"campaign": {"canonical_key": "campaign:hidden"}}},
        {"entity_references": {"campaign": {"source_identifier": "fictional"}}},
        {"period_start_candidate": "not-a-date"},
        {"source_system_selector": "amc"},
    ],
)
def test_malformed_candidate_intent_performs_no_catalog_lookup(
    bad_arguments: dict[str, object],
    contracts: tuple[DataContract, ...],
) -> None:
    entity_catalog = _EntityCatalog()

    with pytest.raises(ValidationError):
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            _semantic_arguments(**bad_arguments),
            _semantic_context(contracts, entity_catalog),
        )

    assert entity_catalog.calls == []


def test_unknown_metric_stops_before_entity_lookup(
    contracts: tuple[DataContract, ...],
) -> None:
    entity_catalog = _EntityCatalog()

    with pytest.raises(ValidationError, match="could not resolve metric"):
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            _semantic_arguments(
                metric_reference="fictional unknown metric",
                entity_references={"campaign": "Fictional campaign"},
            ),
            _semantic_context(contracts, entity_catalog),
        )

    assert entity_catalog.calls == []


def test_a_metric_whose_sources_published_nothing_refuses_as_no_data_not_a_shape_error(
    contracts: tuple[DataContract, ...],
) -> None:
    """An empty lake: no rephrasing can succeed, so the refusal says nothing is published."""
    arguments = _semantic_arguments(metric_reference="ad_spend", grains=["date"])
    base = _semantic_context(contracts, _EntityCatalog())

    def latest_week(_metric: Metric) -> DateRange | None:
        return DateRange(date(2026, 8, 10), date(2026, 8, 16))

    published = replace(base, default_window=latest_week)
    # Paired: with the metric published, the resolution refusal is unchanged (ADR-004).
    with pytest.raises(ValidationError) as shape:
        request_from_semantic_intent(MeasurementTool.METRIC_BY_GRAIN, arguments, published)
    assert shape.value.context["field"] == "currency"
    assert shape.value.context["outcome"] == "semantically_incompatible"
    assert shape.value.context["candidates"] == ()

    calls: list[str] = []

    def nothing_published(metric: Metric) -> DateRange | None:
        calls.append(metric.name)
        return None

    empty = replace(base, default_window=nothing_published)
    with pytest.raises(DomainError) as refusal:
        request_from_semantic_intent(MeasurementTool.METRIC_BY_GRAIN, arguments, empty)
    assert not isinstance(refusal.value, ValidationError)
    assert str(refusal.value.code) == "ADLY-PUBLICATION_SELECTION-0001"
    assert refusal.value.context == {"field": "publication", "outcome": "nothing_published"}
    label = cast("Metric", CATALOG.metric("ad_spend")).label
    assert refusal.value.description == (
        f"Nothing has been published for {label} yet, so there is no figure to report for any "
        "window. Figures appear once your data producers deliver its source data and it is "
        "published."
    )
    assert calls == ["ad_spend"]

    # An unresolvable metric keeps its own refusal, and reads no coverage.
    calls.clear()
    with pytest.raises(ValidationError, match="could not resolve metric"):
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            _semantic_arguments(metric_reference="fictional unknown metric"),
            empty,
        )
    assert calls == []

    # The happy path never reads coverage.
    def unread(metric: Metric) -> DateRange | None:
        raise AssertionError(metric.name)

    resolved = request_from_semantic_intent(
        MeasurementTool.METRIC_BY_GRAIN,
        _semantic_arguments(),
        replace(base, default_window=unread),
    )
    assert resolved.tool is MeasurementTool.METRIC_BY_GRAIN


def test_entity_unsupported_by_metric_stops_before_pending_page_catalog_lookup(
    contracts: tuple[DataContract, ...],
) -> None:
    entity_catalog = _EntityCatalog()

    with pytest.raises(ValidationError, match="product candidate is not supported"):
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            _semantic_arguments(
                entity_references={"product": "Fictional product"},
            ),
            _semantic_context(contracts, entity_catalog),
        )

    assert entity_catalog.calls == []


@pytest.mark.parametrize("legacy", ["metric_name", "filters", "window_start", "window_end"])
def test_legacy_raw_arguments_are_rejected_before_resolution(
    legacy: str,
    contracts: tuple[DataContract, ...],
) -> None:
    entity_catalog = _EntityCatalog()
    arguments = _semantic_arguments()
    arguments[legacy] = "person@example.com"

    with pytest.raises(ValidationError, match="undeclared arguments") as raised:
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            arguments,
            _semantic_context(contracts, entity_catalog),
        )

    assert raised.value.context == {"outcome": "undeclared_arguments", "n": 1}
    assert legacy not in raised.value.description
    assert "person@example.com" not in str(raised.value)
    assert entity_catalog.calls == []


def test_mapper_refuses_a_forged_explicit_source_selector(
    contracts: tuple[DataContract, ...],
) -> None:
    resolved = resolve_analytical_request(_candidate())
    assert resolved.request is not None
    current = resolved.request.sources
    assert current.value is not None
    selection = SourceSelection(
        required=current.value.required,
        selector=current.value.required[0],
    )
    forged_source = replace(current, value=selection)
    forged = replace(
        resolved,
        request=replace(resolved.request, sources=forged_source),
    )

    with pytest.raises(ValueError, match="source selector"):
        to_measurement_tool_request(forged, _context(contracts))


def test_mapper_refuses_a_resolved_entity_kind_without_an_explicit_mapping(
    contracts: tuple[DataContract, ...],
) -> None:
    resolved = resolve_analytical_request(_candidate())
    assert resolved.request is not None
    selection = EntitySelection(EntityKind.BRAND, BrandKey("brand:fictional", SCOPE))
    evidence = MappingEvidence("fictional-brand-mapping", 1)
    brand = FieldResolution(
        field="brand",
        origin=FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
        outcome=ResolutionOutcome.RESOLVED,
        value=selection,
        candidates=(ResolutionCandidate(selection, "Fictional brand", (evidence,)),),
        evidence=(evidence,),
    )
    forged = replace(
        resolved,
        request=replace(
            resolved.request,
            entities=(*resolved.request.entities, brand),
        ),
    )

    with pytest.raises(ValueError, match="brand has no measurement map"):
        to_measurement_tool_request(forged, _context(contracts))


def test_mapper_refuses_non_native_currency_policy(
    contracts: tuple[DataContract, ...],
) -> None:
    resolved = resolve_analytical_request(_candidate())
    assert resolved.request is not None
    policy = AnalyticalCurrencyPolicy(AnalyticalCurrencyMode.TARGET, "EUR")
    evidence = DeclarationEvidence("currency_policy", "target")
    currency = FieldResolution(
        field="currency",
        origin=FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
        outcome=ResolutionOutcome.RESOLVED,
        value=policy,
        candidates=(ResolutionCandidate(policy, "target currency", (evidence,)),),
        evidence=(evidence,),
    )
    forged = replace(
        resolved,
        request=replace(resolved.request, currency=currency),
    )

    with pytest.raises(ValueError, match="only native currency"):
        to_measurement_tool_request(forged, _context(contracts))


def test_mapper_refuses_a_report_lens_origin_it_cannot_preserve(
    contracts: tuple[DataContract, ...],
) -> None:
    resolved = resolve_analytical_request(
        CandidateAnalyticalRequest(
            catalog=CATALOG,
            today=date(2026, 8, 19),
            scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
            authorized_aliases=ALIASES,
            metric_reference="retail_revenue_shipped",
            operation_reference="breakdown",
            current_period=PeriodIntent(window=CURRENT),
            grains=("product",),
            entities=tuple(
                intent
                for intent in ENTITIES
                if intent.kind in {EntityKind.MARKETPLACE, EntityKind.PRODUCT}
            ),
            report_lenses=(ReportLensReference("selling_program", "BUSINESS"),),
        )
    )
    assert resolved.request is not None
    explicit = next(
        record
        for record in resolved.request.report_lenses
        if record.origin is FieldOrigin.EXPLICIT_CUSTOMER_TEXT
    )
    unexpected = replace(explicit, origin=FieldOrigin.CUSTOMER_CLARIFICATION)
    forged = replace(
        resolved,
        request=replace(
            resolved.request,
            report_lenses=tuple(
                unexpected if record is explicit else record
                for record in resolved.request.report_lenses
            ),
        ),
    )

    with pytest.raises(ValueError, match="customer_clarification"):
        to_measurement_tool_request(forged, _context(contracts))


def test_semantic_boundary_classifies_explicit_source_selection_as_unsupported(
    contracts: tuple[DataContract, ...],
) -> None:
    with pytest.raises(ValidationError, match="sources candidate is not supported") as raised:
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            _semantic_arguments(
                source_system_selector="amc",
                source_dataset_selector="dsp_impressions",
            ),
            _semantic_context(contracts, _EntityCatalog()),
        )

    assert raised.value.context["outcome"] == ResolutionOutcome.UNSUPPORTED_CAPABILITY.value


def test_semantic_boundary_preserves_explicit_advertiser_breakdown_as_grain(
    contracts: tuple[DataContract, ...],
) -> None:
    with pytest.raises(ValidationError, match="could not resolve grains") as raised:
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            _semantic_arguments(grains=["advertiser"]),
            _semantic_context(contracts, _EntityCatalog()),
        )

    assert raised.value.context == {"field": "grains"}


@pytest.mark.parametrize("kind", [EntityKind.BRAND, EntityKind.ADVERTISER])
def test_semantic_boundary_keeps_known_unavailable_entity_separate_from_campaign_breakdown(
    kind: EntityKind,
    contracts: tuple[DataContract, ...],
) -> None:
    entity_catalog = _EntityCatalog()

    with pytest.raises(ValidationError, match=f"{kind.value} candidate is not supported") as raised:
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            _semantic_arguments(entity_references={kind.value: "Fictional entity"}),
            _semantic_context(contracts, entity_catalog),
        )

    assert raised.value.context == {
        "field": kind.value,
        "outcome": ResolutionOutcome.UNSUPPORTED_CAPABILITY.value,
        "candidates": (),
    }
    assert entity_catalog.calls == []


@pytest.mark.parametrize(
    ("tool", "overrides"),
    [
        (MeasurementTool.METRIC_BY_GRAIN, {}),
        (
            MeasurementTool.COMPARE_WINDOWS,
            {
                "prior_period_start_candidate": PRIOR.start.isoformat(),
                "prior_period_end_candidate": PRIOR.end.isoformat(),
            },
        ),
        (
            MeasurementTool.TOP_N,
            {"rank_size": 3, "rank_order": "VALUE"},
        ),
    ],
)
def test_semantic_boundary_classifies_reconciliation_as_unsupported_for_every_tool(
    tool: MeasurementTool,
    overrides: dict[str, object],
    contracts: tuple[DataContract, ...],
) -> None:
    with pytest.raises(ValidationError, match="operation candidate is not supported") as raised:
        request_from_semantic_intent(
            tool,
            _semantic_arguments(operation="reconciliation", **overrides),
            _semantic_context(contracts, _EntityCatalog()),
        )

    assert raised.value.context["outcome"] == ResolutionOutcome.UNSUPPORTED_CAPABILITY.value


def test_unknown_explicit_operation_remains_attributed_to_operation(
    contracts: tuple[DataContract, ...],
) -> None:
    with pytest.raises(ValidationError, match="could not resolve operation") as raised:
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            _semantic_arguments(operation="fictional operation"),
            _semantic_context(contracts, _EntityCatalog()),
        )

    assert raised.value.context["field"] == "operation"


def test_supported_operation_cannot_switch_the_selected_tool_boundary(
    contracts: tuple[DataContract, ...],
) -> None:
    with pytest.raises(ValidationError, match="incompatible with the selected") as raised:
        request_from_semantic_intent(
            MeasurementTool.COMPARE_WINDOWS,
            _semantic_arguments(operation="breakdown"),
            _semantic_context(contracts, _EntityCatalog()),
        )

    assert raised.value.context["field"] == "operation"
    assert raised.value.context["outcome"] == ResolutionOutcome.INVALID_CONTEXT.value


def test_omitted_window_defaults_to_published_coverage_and_is_disclosed_as_governed(
    contracts: tuple[DataContract, ...],
) -> None:
    """A question with no window answers for the latest published coverage, disclosed.

    The user asked for a figure, not for a refusal about calendars; the defaulted window
    must surface as governed-default provenance so the released answer cannot omit it.
    """
    arguments = _semantic_arguments()
    arguments.pop("period_start_candidate")
    arguments.pop("period_end_candidate")
    default = DateRange(date(2026, 8, 10), date(2026, 8, 16))
    requested: list[str] = []

    def default_window(metric: Metric) -> DateRange | None:
        requested.append(metric.name)
        return default

    context = replace(_semantic_context(contracts, _EntityCatalog()), default_window=default_window)
    mapped = request_from_semantic_intent(MeasurementTool.METRIC_BY_GRAIN, arguments, context)

    assert mapped.window == default
    assert requested == ["impressions"]
    period = next(item for item in mapped.resolution_provenance if item.field == "current_period")
    assert period.origin is FieldOrigin.GOVERNED_DEFAULT
    assert period.value == str(default)


def test_omitted_window_without_published_coverage_refuses_with_actionable_reason(
    contracts: tuple[DataContract, ...],
) -> None:
    arguments = _semantic_arguments()
    arguments.pop("period_start_candidate")
    arguments.pop("period_end_candidate")

    def no_coverage(_metric: Metric) -> DateRange | None:
        return None

    context = replace(_semantic_context(contracts, _EntityCatalog()), default_window=no_coverage)

    # Nothing is published for the metric, so naming a window can't help either: the refusal
    # is the governed no-data answer (ADR-062), not a request to specify a window.
    with pytest.raises(DomainError, match="Nothing has been published for") as refusal:
        request_from_semantic_intent(MeasurementTool.METRIC_BY_GRAIN, arguments, context)
    assert refusal.value.context == {"field": "publication", "outcome": "nothing_published"}


def test_an_explicit_window_never_consults_the_coverage_default(
    contracts: tuple[DataContract, ...],
) -> None:
    def default_window(_metric: Metric) -> DateRange | None:
        raise AssertionError("an explicit window must not consult coverage defaulting.")

    context = replace(_semantic_context(contracts, _EntityCatalog()), default_window=default_window)
    mapped = request_from_semantic_intent(
        MeasurementTool.METRIC_BY_GRAIN, _semantic_arguments(), context
    )

    assert mapped.window == CURRENT
    period = next(item for item in mapped.resolution_provenance if item.field == "current_period")
    assert period.origin is FieldOrigin.EXPLICIT_CUSTOMER_TEXT


def test_a_campaign_breakdown_maps_to_measurement_execution(
    contracts: tuple[DataContract, ...],
) -> None:
    """The undisplayable-grain gate is gone: campaign rows are labelled, never refused.

    ADR-067 wires the governed public-label projection into row shaping, so the pre-query
    refusal ADR-066 recorded as a temporary gate must not come back.
    """
    arguments = _semantic_arguments(grains=["campaign"])

    mapped = request_from_semantic_intent(
        MeasurementTool.METRIC_BY_GRAIN,
        arguments,
        _semantic_context(contracts, _EntityCatalog()),
    )

    assert mapped.tool is MeasurementTool.METRIC_BY_GRAIN
    assert mapped.grains == ("campaign",)


def test_an_omitted_single_valued_selector_reaches_the_measurement_filter_as_governed(
    contracts: tuple[DataContract, ...],
) -> None:
    """segment_impressions admits only time_grain=month, so "September" needs no selector."""
    context = replace(_semantic_context(contracts, _EntityCatalog()), today=date(2026, 10, 17))
    mapped = request_from_semantic_intent(
        MeasurementTool.METRIC_BY_GRAIN,
        {
            "metric_reference": "segment_impressions",
            "grains": ["audience_segment", "marketplace"],
            "period_start_candidate": "2026-09-01",
            "period_end_candidate": "2026-09-30",
        },
        context,
    )
    assert mapped.filters["time_grain"] == "month"
    assert (
        ResolutionProvenance("time_grain", FieldOrigin.GOVERNED_DEFAULT, "month")
        in mapped.resolution_provenance
    )
    # Paired control: shoppers_reached holds week and month rows, so it still needs a choice.
    with pytest.raises(ValidationError, match="time_grain"):
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            {
                "metric_reference": "shoppers_reached",
                "grains": ["exposure_group", "identity_space", "marketplace"],
                "period_start_candidate": "2026-09-01",
                "period_end_candidate": "2026-09-30",
            },
            context,
        )


def test_a_selector_value_the_metric_requires_exactly_is_pinned_as_governed(
    contracts: tuple[DataContract, ...],
) -> None:
    """repeat_buyer_rate `requires: cohort_path: standard`, so "the repeat rate" needs no path.

    Live run 3 (`standard_cohort`) got a clarification asking for the path: no cohort_path
    failed the metric's own requirement, and an explicit cohort_path=standard was refused as a
    selector the metric doesn't take. The metric admits one value, so it is pinned with a
    governed-default origin in the provenance. What tells the customer which population was
    read is the metric's own label ("· ad-touched repeat only"), not a separate disclosure.
    """
    vintage = DateRange(date(2026, 4, 1), date(2026, 9, 30))

    def latest_vintage(_metric: Metric) -> DateRange | None:
        return vintage

    context = replace(
        _semantic_context(contracts, _EntityCatalog()),
        today=date(2026, 10, 17),
        default_window=latest_vintage,
    )
    arguments: dict[str, object] = {
        "metric_reference": "repeat_buyer_rate",
        "grains": ["cohort", "acquisition_channel_group", "months_since_acquisition"],
        "selectors": {"time_grain": "custom"},
    }

    mapped = request_from_semantic_intent(MeasurementTool.METRIC_BY_GRAIN, arguments, context)

    assert mapped.filters["cohort_path"] == "standard"
    assert (
        ResolutionProvenance("cohort_path", FieldOrigin.GOVERNED_DEFAULT, "standard")
        in mapped.resolution_provenance
    )
    # The model passing the one value it may pass is the customer's text, not a refusal.
    explicit = request_from_semantic_intent(
        MeasurementTool.METRIC_BY_GRAIN,
        {**arguments, "selectors": {"time_grain": "custom", "cohort_path": "standard"}},
        context,
    )
    assert (
        ResolutionProvenance("cohort_path", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, "standard")
        in explicit.resolution_provenance
    )
    # A contradicting value is refused, naming the one the metric admits; never re-pinned.
    with pytest.raises(ValidationError) as refused:
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            {**arguments, "selectors": {"time_grain": "custom", "cohort_path": "arp"}},
            context,
        )
    assert refused.value.context["field"] == "cohort_path"
    assert refused.value.context["candidates"] == ("cohort_path=standard",)


_REQUIRED_EXACT = tuple(
    (metric.name, key, value)
    for metric in CATALOG.metrics
    for key, value in metric.requires
    if key not in REPORT_LENSES
)
"""Every metric the dictionary defines at one exact non-lens selector value, by name."""


def test_the_exact_requires_pin_covers_exactly_the_cohort_path_metrics() -> None:
    """Spell out who the pin reaches, so a new `requires` entry can't widen it unseen.

    The eight cohort_path=standard metrics are pinned; the five identity_space=account metrics
    are not, because identity_space is one of their own grains.
    """
    assert sorted(_REQUIRED_EXACT) == sorted(
        [
            *(
                (name, "cohort_path", "standard")
                for name in (
                    "cohort_buyers_account_ids",
                    "cohort_buyers_new_to_brand_12mo",
                    "cohort_repeat_buyers_account_ids",
                    "cohort_repeat_buyers_cumulative_account_ids",
                    "cumulative_repeat_rate",
                    "cumulative_repeat_rate_account_ids",
                    "repeat_buyer_rate",
                    "repeat_buyer_rate_account_ids",
                )
            ),
            *(
                (name, "identity_space", "account")
                for name in (
                    "purchaser_rate",
                    "purchaser_rate_by_frequency",
                    "revenue_per_exposed_shopper",
                    "roas_incl_halo_by_frequency",
                    "search_rate",
                )
            ),
        ]
    )


def _vintage_context(contracts: tuple[DataContract, ...]) -> SemanticMeasurementContext:
    vintage = DateRange(date(2026, 4, 1), date(2026, 9, 30))

    def latest_vintage(_metric: Metric) -> DateRange | None:
        return vintage

    return replace(
        _semantic_context(contracts, _EntityCatalog()),
        today=date(2026, 10, 17),
        default_window=latest_vintage,
    )


@pytest.mark.parametrize(
    "metric_name",
    sorted(name for name, key, _value in _REQUIRED_EXACT if key == "cohort_path"),
)
def test_every_cohort_path_metric_resolves_with_the_path_pinned_as_governed(
    contracts: tuple[DataContract, ...], metric_name: str
) -> None:
    mapped = request_from_semantic_intent(
        MeasurementTool.METRIC_BY_GRAIN,
        {
            "metric_reference": metric_name,
            "grains": ["cohort"],
            "selectors": {"time_grain": "custom"},
        },
        _vintage_context(contracts),
    )

    assert mapped.metric_name == metric_name
    assert mapped.filters["cohort_path"] == "standard"
    assert [item for item in mapped.resolution_provenance if item.field == "cohort_path"] == [
        ResolutionProvenance("cohort_path", FieldOrigin.GOVERNED_DEFAULT, "standard")
    ]


_IDENTITY_SPACE_REFUSALS = {
    "search_rate": "Search rate · from the hour after the first ad, within 14 days · Amazon "
    "account IDs (measurement_basis=ad_attributed, unit=ratio)",
    "purchaser_rate": "Purchaser rate · within 14 days of the first ad · Amazon account IDs "
    "(measurement_basis=ad_attributed, unit=ratio)",
    "revenue_per_exposed_shopper": "Sales per shopper reached · within 14 days of the first ad "
    "· includes brand halo · Amazon account IDs (measurement_basis=ad_attributed, unit=currency)",
    "purchaser_rate_by_frequency": "Purchaser rate by how many ads shoppers saw · within 14 days "
    "of the first ad · Amazon account IDs (measurement_basis=ad_attributed, unit=ratio)",
    "roas_incl_halo_by_frequency": "Sales ÷ window spend by how many ads shoppers saw · all ads "
    "· includes brand halo · Amazon account IDs (measurement_basis=ad_attributed, unit=ratio)",
}
"""The one candidate each refusal offers: the metric's label, which names the account-ID
population. The refusal never names identity_space=account itself."""


@pytest.mark.parametrize(
    "metric_name",
    sorted(name for name, key, _value in _REQUIRED_EXACT if key == "identity_space"),
)
@pytest.mark.parametrize(
    "grains",
    [["exposure_group", "marketplace"], ["exposure_group", "identity_space", "marketplace"]],
)
def test_an_identity_space_metric_is_never_pinned_and_refuses_with_its_label(
    contracts: tuple[DataContract, ...], metric_name: str, grains: list[str]
) -> None:
    """identity_space is a grain of these metrics, so a pin could filter a requested breakdown.

    Unchanged from before the cohort_path pin: the metric refuses, unpinned, with or without a
    breakdown by identity space.
    """
    metric = CATALOG.metric(metric_name)
    assert metric is not None
    assert "identity_space" in metric.valid_grains
    (grain,) = (values[0] for key, values in metric.required_selectors if key == "time_grain")
    with pytest.raises(ValidationError) as refused:
        request_from_semantic_intent(
            MeasurementTool.METRIC_BY_GRAIN,
            {
                "metric_reference": metric_name,
                "grains": [name for name in grains if name in metric.valid_grains],
                "selectors": {"time_grain": grain},
                "period_start_candidate": "2026-09-01",
                "period_end_candidate": "2026-09-30",
            },
            replace(_semantic_context(contracts, _EntityCatalog()), today=date(2026, 10, 17)),
        )

    assert refused.value.context["field"] == "metric"
    assert refused.value.context["outcome"] == ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE.value
    assert refused.value.context["candidates"] == (_IDENTITY_SPACE_REFUSALS[metric_name],)
