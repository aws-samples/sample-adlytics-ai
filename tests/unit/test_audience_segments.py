# SPDX-License-Identifier: MIT-0
"""Packs 9, 13 and 8: the audience-segment dimension and DSP segment delivery (FEAT-008).

Pack 9 (`amc-dsp-segment-names`) and pack 13 (`amc-segment-metadata`, paid ASI) write
`dim_audience_segment` in two namespaces that stay separate with no cross-namespace key (ADR-103
point 6). Each segment's provider comes from the governed classification, and a segment it doesn't
cover reads "Provider not recorded", never "Amazon". Names and descriptions are the first free-text
columns loaded through the AMC CSV dialect. Pack 8 (`amc-audience-performance`) writes
`fct_segment_delivery`, per DSP campaign, segment and month.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from publication_support import StubPublications, bound_plan
from test_dimension_labels import (
    SCOPE,
    _LabelCatalog,  # pyright: ignore[reportPrivateUsage]
)
from test_semantic_sidecar import (
    MemoryCanonical,
    _case,  # pyright: ignore[reportPrivateUsage]
)

from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    handle,
)
from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.curation.manifests import (
    BuildManifestRequest,
    build_manifest,
    checksum_of,
    declared_columns_of,
)
from adapters.jobs.ingest_delivery.landing import LoadedDelivery
from adapters.jobs.ingest_delivery.semantic_ingestion import (
    SemanticIngestionRequest,
    ingest_semantic_delivery,
)
from adapters.jobs.ingest_delivery.semantic_sidecar import SemanticSidecarOutcome
from adapters.outbound.athena.composition import compose, compose_series
from adapters.outbound.athena.query import (
    ALL_ROWS_WITHHELD,
    INTERACTIVE_WAIT,
    AthenaQueryAdapter,
    AthenaQueryTarget,
    ResultTransport,
)
from adapters.outbound.audience_segment_providers import (
    load_segment_providers,
    parse_segment_providers,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import (
    SELECTION_TOO_LARGE,
    PublicationScopeRequest,
    publication_scopes_for,
)
from adapters.outbound.segment_labels import CanonicalSegmentLabels
from adapters.outbound.segment_roster import RosterReaders, SegmentRosterReader
from adapters.outbound.source_pack import load_source_packs
from adapters.producer.packs import discard_policies, load_query_packs, pack_by_id
from domain.capabilities.paid_features import (
    DerivePaidFeatureStateRequest,
    PaidFeatureDisplay,
    ResolvePeriodStateRequest,
    resolve_period_state,
)
from domain.catalog.capability import (
    GRAIN_NOT_SUPPORTED_CODE,
    NOT_COMPUTABLE_CODE,
    REQUIRES_OPTION_CODE,
)
from domain.curation.landing import GOVERNED_MANIFEST_LAYOUT, LandedObject
from domain.measurement.comparison import RankOrder
from domain.measurement.figures import measure_alias
from domain.measurement.planning import FactShape, PlanRequest, plan
from domain.measurement.publications import BoundQueryPlan
from domain.measurement.rosters import (
    FIRST_MONTH_NOTE,
    ROSTER_REFUSED,
    ROSTER_UNAVAILABLE,
    Roster,
    RosterEntry,
    RosterKind,
    apply_roster,
    coarser_grain_set,
    roster_lines,
)
from domain.narrative.publication import RenderingKind
from domain.narrative.shaped_response import NonAdditivity, ShapedResponse
from domain.operations.bake_refusals import IsolatedRefusal, IsolatedRefusalReason, RefusalScope
from domain.producer.models import AccessTier, SuppressionPolicy
from domain.semantics.audience_segments import (
    ASI_NEEDS_PAID_FEATURE_TEXT,
    PROVIDER_NOT_RECORDED,
    SegmentLabel,
    SegmentNamespace,
    SegmentProvider,
    asi_metadata_state,
    provider_label,
)
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.identity import (
    CanonicalCampaignKeyRequest,
    ChannelKey,
    IdentityMapping,
    MarketplaceKey,
    PlatformKey,
    SourceIdentifier,
    SourceSystemKey,
    canonical_campaign_key,
)
from domain.semantics.publication import PublicationId
from domain.semantics.source_aligned import LandedDeliveryRef
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import DomainError, InvariantError, ValidationError
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from domain.shopper_aggregates.loss import DEGRADED_NOTE, LossGrade
from ports.outbound.canonical import CanonicalDestination
from ports.outbound.publication_selection import PublicationSelection, PublicationSelectionRequest
from ports.outbound.query import (
    MAX_ROWS_CEILING,
    PeriodSeriesRequest,
    QueryRequest,
    QueryResult,
    RowCeiling,
    row_ceiling,
)
from ports.outbound.rosters import RosterRequest
from ports.outbound.segment_labels import SegmentLabelRequest
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-103", "ADR-107")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/generated"
NAMES = "amc-dsp-segment-names"
METADATA = "amc-segment-metadata"
PERFORMANCE = "amc-audience-performance"
DIMENSION = "dim_audience_segment"
FACT = "fct_segment_delivery"
NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
SEPTEMBER = DateRange(date(2026, 9, 1), date(2026, 9, 30))
AUGUST = DateRange(date(2026, 8, 1), date(2026, 8, 31))
TRAILER = "distinct_user_count,suppression_reason,rows_suppressed"
NAMES_HEADER = (
    "campaign_country,behavior_segment_id,segment_name,segment_description,"
    f"time_window_start,time_window_end,{TRAILER}"
)
METADATA_HEADER = (
    "segment_marketplace_id,segment_id,segment_name,segment_description,category_level_1,"
    f"category_level_2,category_path,time_window_start,time_window_end,{TRAILER}"
)
PERFORMANCE_HEADER = (
    "campaign_country,campaign_id_string,behavior_segment_id,impressions,dsp_spend,dsp_currency,"
    "dsp_currency_min,segment_reach,segment_purchasers,segment_purchases,"
    "segment_attributed_revenue,purchase_currency,purchase_currency_min,time_window_start,"
    f"time_window_end,{TRAILER}"
)


def _window(window: DateRange) -> str:
    end = window.end + timedelta(days=1)
    return f"{window.start.isoformat()}T00:00:00.000Z,{end.isoformat()}T00:00:00.000Z"


def _csv(header: str, *lines: str) -> str:
    return "".join(f"{line}\n" for line in (header, *lines))


# --- Packs ----------------------------------------------------------------------------------------


def test_the_three_packs_are_monthly_discard_packs_and_pack_13_is_paid_with_the_pinned_id() -> None:
    names, metadata, performance = (
        pack_by_id(ROOT, item) for item in (NAMES, METADATA, PERFORMANCE)
    )
    for pack in (names, metadata, performance):
        assert [grain.value for grain in pack.period_policy.grains] == ["month"]  # type: ignore[union-attr]
        assert pack.suppression_policy is SuppressionPolicy.DISCARD_SUPPRESSED_ROWS
        assert (pack.expected_bar, pack.effective_cadence_lag_days) == (2, 2)
    assert (names.access_tier, performance.access_tier) == (AccessTier.STANDARD,) * 2
    assert metadata.access_tier is AccessTier.PAID_FEATURE
    assert metadata.cadence_lag_days == 2  # a paid-feature pack must declare it
    assert [(item.name, item.commerce_identity) for item in metadata.input_parameters] == [
        ("segment_marketplace_id", "amc.segment_marketplace_id")
    ]
    assert "segment_marketplace_id = CUSTOM_PARAMETER('segment_marketplace_id')" in metadata.sql
    # Pack 8 is design §5.5's C1-a text: matched segments by the governed campaign country,
    # DSP-attributed outcomes joined on shopper and campaign.
    assert "FROM dsp_impressions_by_matched_segments" in performance.sql
    assert "campaign_country = CUSTOM_PARAMETER('dsp_campaign_country')" in performance.sql
    assert "ad_product_type IS NULL" in performance.sql
    assert (
        "ON o.user_id = s.user_id AND o.campaign_id_string = s.campaign_id_string"
        in performance.sql
    )
    installed = {item.contract_id for item in discard_policies(load_query_packs(ROOT))}
    assert {NAMES, METADATA, PERFORMANCE} <= installed


def test_the_generated_tables_are_new_and_keep_the_namespaces_in_the_key() -> None:
    generated = json.loads((ROOT / "contracts/generated/v2-tables.json").read_text())
    source = {table["name"] for table in generated["sourceAlignedTables"]}
    assert {
        "sa_amc_dsp_segment_names",
        "sa_amc_segment_metadata",
        "sa_amc_audience_performance",
    } <= source
    for layer in ("canonicalCandidateTables", "canonicalCurrentTables"):
        tables = {table["name"]: table for table in generated[layer]}
        dimension = tables[DIMENSION]
        assert dimension["writtenBy"] == [NAMES, METADATA]
        assert dimension["grain"] == [
            "publication_id",
            "platform_key",
            "segment_id",
            "segment_marketplace_key",
            "segment_namespace",
            "valid_from",
        ]
        names = [column["name"] for column in dimension["columns"]]
        # No cross-namespace key or join column: nothing names the other namespace's ID.
        assert not [name for name in names if "asi" in name or "behavior" in name]
        assert f"{DIMENSION}_withheld" not in tables
        fact = tables[FACT]
        assert fact["writtenBy"] == [PERFORMANCE]
        assert {"campaign_key", "segment_namespace", "segment_id"} <= set(fact["grain"])
        assert f"{FACT}_withheld" in tables


# --- Provider classification ----------------------------------------------------------------------


def test_the_provider_classification_names_amazon_third_party_or_nothing() -> None:
    classification = load_segment_providers(ROOT)
    dsp = SegmentNamespace.AMC_DSP_BEHAVIOR
    assert classification.classify(dsp, "Demo - Age 25-34") is SegmentProvider.AMAZON
    assert (
        classification.classify(dsp, "ExperianDemographicsGenderFemale_LiveRamp")
        is SegmentProvider.THIRD_PARTY
    )
    # No namespace rule for DSP behaviour segments: an unlisted one has no provider.
    assert classification.classify(dsp, "Synthetic lifestyle: weekend gardeners") is None
    assert classification.classify(dsp, None) is None
    # ASI segments are Amazon's by the namespace rule, a listed DSP name doesn't leak across.
    assert classification.classify(SegmentNamespace.AMC_ASI, "anything") is SegmentProvider.AMAZON
    assert [provider_label(item) for item in ("amazon", "third_party", None)] == [
        "Amazon",
        "Third party",
        PROVIDER_NOT_RECORDED,
    ]
    assert PROVIDER_NOT_RECORDED == "Provider not recorded"


def test_nothing_labels_every_dsp_segment_as_amazon() -> None:
    classification = load_segment_providers(ROOT)
    rule = next(item for item in classification.rules if item.namespace.value == "amc_dsp_behavior")
    assert rule.provider is None
    assert {entry.provider for entry in classification.entries} == {
        SegmentProvider.AMAZON,
        SegmentProvider.THIRD_PARTY,
    }
    for entry in classification.entries:
        assert entry.evidence == (
            "P-BO-3 first pass, accepted for dev 2026-10-09; human review pending"
        )


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"version": 1, "namespaces": [], "segments": []}, "one rule per namespace"),
        (
            {
                "version": 1,
                "namespaces": [
                    {"namespace": "amc_asi", "provider": "amazon", "evidence": "e"},
                    {"namespace": "amc_dsp_behavior", "provider": None, "evidence": "e"},
                ],
                "segments": [
                    {
                        "namespace": "amc_dsp_behavior",
                        "segment_name": "Demo - Male",
                        "category": "gender",
                        "provider": "amazon",
                        "evidence": "e",
                    }
                ]
                * 2,
            },
            "each segment once",
        ),
        (
            {
                "version": 1,
                "namespaces": [
                    {"namespace": "amc_asi", "provider": "everyone", "evidence": "e"},
                    {"namespace": "amc_dsp_behavior", "provider": None, "evidence": "e"},
                ],
                "segments": [],
            },
            "everyone",
        ),
    ],
)
def test_a_malformed_classification_is_refused(document: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_segment_providers(document)


# --- Paid feature (ADR-107) -----------------------------------------------------------------------


def test_pack_13_unobserved_reads_the_adr_107_text_and_observed_sets_feature_observed() -> None:
    months = (
        DateRange(date(2026, 7, 1), date(2026, 7, 31)),
        AUGUST,
        SEPTEMBER,
    )
    unobserved = asi_metadata_state(
        DerivePaidFeatureStateRequest(SEPTEMBER, months, frozenset(), cadence_lag_days=2)
    )
    assert not unobserved.feature_observed
    resolved = resolve_period_state(ResolvePeriodStateRequest(unobserved, date(2026, 10, 6)))
    assert resolved.display is PaidFeatureDisplay.NEEDS_PAID_FEATURE
    assert ASI_NEEDS_PAID_FEATURE_TEXT == (
        "Needs the Amazon Insights paid feature (Audience Segment Insights). No data has "
        "arrived for this workspace."
    )
    # Positive control: one observed month sets feature_observed, and September is published.
    observed = asi_metadata_state(
        DerivePaidFeatureStateRequest(SEPTEMBER, months, frozenset({SEPTEMBER}), cadence_lag_days=2)
    )
    assert observed.feature_observed and observed.period_published


# --- Load -----------------------------------------------------------------------------------------


def _delivery(contract_id: str, text: str, window: DateRange = SEPTEMBER) -> LoadedDelivery:
    body = text.encode()
    declaration = json.loads((FIXTURES / f"{contract_id}.manifest.json").read_text())
    manifest = build_manifest(
        BuildManifestRequest(
            declaration={
                **declaration,
                "period_start": window.start.isoformat(),
                "period_end": window.end.isoformat(),
                "source_watermark": (window.end + timedelta(days=2)).isoformat(),
            },
            objects=(LandedObject("data-0001.csv", len(body), checksum_of(body)),),
            declared_columns=declared_columns_of(text, source_format="csv"),
        )
    )
    manifest = replace(
        manifest,
        governance=pack_by_id(ROOT, contract_id).governance,
        layout_version=GOVERNED_MANIFEST_LAYOUT,
    )
    return LoadedDelivery(
        manifest=manifest,
        manifest_key=manifest.manifest_key,
        text=text,
        landed_ref=LandedDeliveryRef(
            manifest.manifest_key,
            "manifest-v1",
            "sha256:manifest",
            manifest.object_keys()[0],
            "object-v1",
            manifest.objects[0].checksum,
        ),
    )


@dataclass
class _Loaded:
    outcome: SemanticSidecarOutcome
    rows: tuple[dict[str, object], ...]
    rules: tuple[tuple[str, str, str], ...]
    detail: str = ""


def _load(contract_id: str, delivery: LoadedDelivery, *, providers: bool = True) -> _Loaded:
    _request, tooling, _order, _attempts = _case()
    tooling = replace(
        tooling, segment_providers=load_segment_providers(ROOT) if providers else None
    )
    results = ingest_semantic_delivery(
        SemanticIngestionRequest(
            delivery=delivery,
            contracts=tuple(
                contract
                for _, contract in load_contracts(ROOT)
                if contract.contract_id == contract_id
            ),
            columns=load_registry(ROOT),
            now=NOW,
            operation_id="audience-unit",
            execution_id="audience-execution",
            plan_digest="sha256:audience-plan",
            installed_governance=(pack_by_id(ROOT, contract_id).governance,),
            discard_policies=discard_policies(load_query_packs(ROOT)),
        ),
        tooling,
    )
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    target = DIMENSION if contract_id != PERFORMANCE else FACT
    candidates = tuple(
        item
        for item in canonical.requests
        if item.destination is CanonicalDestination.CANDIDATE
        and item.publication.partition.scope.dataset.value == target
    )
    return _Loaded(
        detail=results[0].detail,
        outcome=results[0].outcome,
        rows=tuple(dict(row) for item in candidates for row in item.rows),
        rules=tuple(
            (result.rule_id, result.outcome.value, result.detail)
            for item in candidates
            for result in item.publication.evidence.results
        ),
    )


W = _window(SEPTEMBER)
AGE = f"US,41001,Demo - Age 25-34,Shoppers aged 25 to 34,{W},2,,false"
EXPERIAN = f"US,41002,ExperianDemographicsGenderFemale_LiveRamp,Third-party gender,{W},2,,false"
QUOTED = f'US,41003,"Synthetic in-market: \\"Trail\\" running shoes",Trail runners,{W},2,,false'
"""A name with an embedded quote, escaped with a backslash as AMC's result CSV does."""
DOUBLED = f'US,41003,"Synthetic in-market: ""Trail"" running shoes",Trail runners,{W},2,,false'
"""The same name escaped RFC 4180's way, which AMC doesn't write: refused, never guessed."""
UNLISTED = f"US,41004,Synthetic lifestyle: weekend gardeners,Gardeners,{W},2,,false"
NAME_WITHHELD = f",,,,{W},1,1 dsp_impressions_by_matched_segments users < 2 required,true"
ASI = (
    f"1,41001,Synthetic ASI in-market: running shoes,Running shoes,In-market,Sports,"
    f"In-market > Sports > Running shoes,{W},2,,false"
)

_SEGMENT = {
    "ecosystem_key": "amazon",
    "platform_key": "amazon_ads",
    "segment_marketplace_key": "amazon_us",
    "is_current": True,
    "valid_from": datetime(2026, 9, 1, tzinfo=UTC),
    "valid_to": datetime(2999, 12, 31, tzinfo=UTC),
}


def _by_id(rows: tuple[dict[str, object], ...]) -> dict[tuple[object, object], dict[str, object]]:
    return {(row["segment_namespace"], row["segment_id"]): row for row in rows}


def test_dsp_segment_names_load_with_their_provider_and_a_withheld_name_is_dropped() -> None:
    loaded = _load(
        NAMES, _delivery(NAMES, _csv(NAMES_HEADER, AGE, EXPERIAN, UNLISTED, NAME_WITHHELD))
    )
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED
    dsp = "amc_dsp_behavior"
    common = {
        **_SEGMENT,
        "segment_namespace": dsp,
        "category_level_1": None,
        "category_level_2": None,
        "category_path": None,
    }
    assert _by_id(loaded.rows) == {
        (dsp, "41001"): {
            **common,
            "segment_id": "41001",
            "segment_name": "Demo - Age 25-34",
            "segment_description": "Shoppers aged 25 to 34",
            "provider": "amazon",
        },
        (dsp, "41002"): {
            **common,
            "segment_id": "41002",
            "segment_name": "ExperianDemographicsGenderFemale_LiveRamp",
            "segment_description": "Third-party gender",
            "provider": "third_party",
        },
        # No rule covers it: NULL, which reads "Provider not recorded", never "Amazon".
        (dsp, "41004"): {
            **common,
            "segment_id": "41004",
            "segment_name": "Synthetic lifestyle: weekend gardeners",
            "segment_description": "Gardeners",
            "provider": None,
        },
    }


def test_a_backslash_escaped_name_stores_exactly_and_an_rfc_doubled_one_is_refused() -> None:
    loaded = _load(NAMES, _delivery(NAMES, _csv(NAMES_HEADER, QUOTED)))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED
    assert [row["segment_name"] for row in loaded.rows] == [
        'Synthetic in-market: "Trail" running shoes'
    ]
    refused = _load(NAMES, _delivery(NAMES, _csv(NAMES_HEADER, DOUBLED)))
    assert (refused.outcome, refused.rows) == (SemanticSidecarOutcome.BLOCKED, ())
    assert refused.detail == (
        "CSV_ESCAPE_AMBIGUOUS at line 2, column segment_name: a quoted field holds a doubled "
        "quote, which is RFC 4180's escape and not AMC's backslash."
    )


def test_colliding_dsp_and_asi_ids_stay_separate_rows_with_their_own_providers() -> None:
    dsp = _load(NAMES, _delivery(NAMES, _csv(NAMES_HEADER, AGE)))
    asi = _load(METADATA, _delivery(METADATA, _csv(METADATA_HEADER, ASI)))
    assert asi.outcome is SemanticSidecarOutcome.PUBLISHED
    rows = _by_id((*dsp.rows, *asi.rows))
    assert set(rows) == {("amc_dsp_behavior", "41001"), ("amc_asi", "41001")}
    assert rows["amc_asi", "41001"] == {
        **_SEGMENT,
        "segment_namespace": "amc_asi",
        "segment_id": "41001",
        "segment_name": "Synthetic ASI in-market: running shoes",
        "segment_description": "Running shoes",
        "category_level_1": "In-market",
        "category_level_2": "Sports",
        "category_path": "In-market > Sports > Running shoes",
        "provider": "amazon",
    }
    assert rows["amc_dsp_behavior", "41001"]["segment_name"] == "Demo - Age 25-34"


def test_a_runtime_without_the_classification_refuses_rather_than_writing_no_provider() -> None:
    with pytest.raises(Exception, match="governed provider classification"):
        _load(NAMES, _delivery(NAMES, _csv(NAMES_HEADER, AGE)), providers=False)


# --- Pack 8 ---------------------------------------------------------------------------------------


def _campaign_key(source_value: str) -> str:
    return canonical_campaign_key(
        CanonicalCampaignKeyRequest(
            commerce_scope=load_commerce_identity_bridge(ROOT).commerce_scope,
            platform=PlatformKey("amazon_ads"),
            channel=ChannelKey("amazon_dsp"),
            source_system=SourceSystemKey("amc"),
            source_scope=SourceIdentifier("amc.entity", "fictional-fixture-amc-entity"),
            identifier_kind="campaign_id_string",
            source_value=source_value,
        )
    ).value


CAMPAIGN = "910000000000000001"
DELIVERED = f"US,{CAMPAIGN},41001,30000,21000000,USD,USD,6000,240,300,12300.00,USD,USD,{W},2,,false"
NO_PURCHASE = f"US,{CAMPAIGN},41002,9000,6300000,USD,USD,1800,0,,,,,{W},2,,false"
PATH_WITHHELD = (
    f",,,19218,130800116,,,3,1,,,,,{W},1,"
    "1 'amazon_attributed_events_by_conversion_time' (path 2 of 2) users < 2 required,true"
)


def test_segment_delivery_publishes_per_campaign_and_segment_and_drops_a_withheld_row() -> None:
    loaded = _load(
        PERFORMANCE,
        _delivery(PERFORMANCE, _csv(PERFORMANCE_HEADER, DELIVERED, NO_PURCHASE, PATH_WITHHELD)),
    )
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED
    common = {
        "basis_key": "ad_attributed",
        "ecosystem_key": "amazon",
        "platform_key": "amazon_ads",
        "marketplace_key": "amazon_us",
        "grain_set": "campaign",
        "time_grain": "month",
        "period_start": SEPTEMBER.start,
        "period_end": SEPTEMBER.end,
        "source_timezone": "UTC",
        "identity_unit": "amazon_user_id",
        "cell_state": "returned",
        "campaign_key": _campaign_key(CAMPAIGN),
        "segment_namespace": "amc_dsp_behavior",
        "ad_product_key": "amazon_dsp",
        "channel_key": "amazon_dsp",
        "channel_group_key": "display",
        "dsp_currency": "USD",
    }
    rows = sorted(loaded.rows, key=lambda row: str(row["segment_id"]))
    assert [{key: row[key] for key in common} for row in rows] == [common, common]
    assert [
        (
            row["segment_id"],
            row["impressions"],
            str(row["dsp_spend"]),
            row["segment_reach"],
            row["segment_purchasers"],
            row["segment_purchases"],
            None
            if row["segment_attributed_revenue"] is None
            else str(row["segment_attributed_revenue"]),
            row["purchase_currency"],
        )
        for row in rows
    ] == [
        ("41001", 30000, "210.0000", 6000, 240, 300, "12300.0000", "USD"),
        ("41002", 9000, "63.0000", 1800, 0, None, None, None),
    ]
    loss = next(detail for rule, _outcome, detail in loaded.rules if rule.endswith(".cell-loss.v1"))
    assert json.loads(loss)["discarded_rows"] == 1


def test_a_withheld_segment_row_keeps_its_spend_out_of_any_total_without_a_currency() -> None:
    """FEAT-002 rule: the live multi-source withheld row's spend arrives with no currency."""
    _request, tooling, _order, _attempts = _case()
    tooling = replace(tooling, segment_providers=load_segment_providers(ROOT))
    ingest_semantic_delivery(
        SemanticIngestionRequest(
            delivery=_delivery(PERFORMANCE, _csv(PERFORMANCE_HEADER, DELIVERED, PATH_WITHHELD)),
            contracts=tuple(
                contract
                for _, contract in load_contracts(ROOT)
                if contract.contract_id == PERFORMANCE
            ),
            columns=load_registry(ROOT),
            now=NOW,
            operation_id="audience-unit",
            execution_id="audience-execution",
            plan_digest="sha256:audience-plan",
            installed_governance=(pack_by_id(ROOT, PERFORMANCE).governance,),
            discard_policies=discard_policies(load_query_packs(ROOT)),
        ),
        tooling,
    )
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    residual = [
        dict(row)
        for item in canonical.requests
        if item.destination is CanonicalDestination.CANDIDATE
        and item.publication.partition.scope.dataset.value == f"{FACT}_withheld"
        for row in item.rows
    ]
    # The withheld row's impressions are kept; its spend came back with no currency, so it is
    # never kept as money anyone could add: no spend, no currency, no sales in the residual.
    assert [
        (
            row["impressions"],
            row.get("dsp_spend"),
            row.get("dsp_currency"),
            row.get("segment_attributed_revenue"),
        )
        for row in residual
    ] == [(19218, None, None, None)]


# --- Planner: month only, never summed across segments or campaigns -------------------------------

US = "ATVPDKIKX0DER"
CAMPAIGN_KEY = "campaign:northstar-dsp"


def _segment_roster(window: DateRange, *ids: str, first: bool = False) -> Roster:
    return Roster(
        RosterKind.SEGMENT,
        window,
        US,
        "amazon_ads",
        tuple(RosterEntry(item, f"Segment label {item}") for item in ids),
        filters=(("campaign_key", CAMPAIGN_KEY),),
        first_period=first,
    )


def _c1_filters() -> dict[str, str]:
    return {
        "time_grain": "month",
        "grain_set": "campaign",
        "marketplace_key": US,
        "campaign_key": CAMPAIGN_KEY,
        "segment_namespace": "amc_dsp_behavior",
    }


def test_c1_plans_per_segment_for_one_campaign_month_and_refuses_a_sum_over_segments() -> None:
    catalog = load_catalog(ROOT)
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    shape = FactShape.for_fact(FACT, contracts)
    roster = _segment_roster(SEPTEMBER, "41001", "41002")
    per_segment = plan(
        catalog,
        PlanRequest(
            "segment_impressions", ("audience_segment",), SEPTEMBER, _c1_filters(), roster=roster
        ),
        shape,
    )
    assert (per_segment.group_by, per_segment.roster) == (("segment_id",), roster)
    for metric in ("segment_impressions", "segment_spend", "segment_reach"):
        with pytest.raises(DomainError) as refused:
            plan(
                catalog,
                PlanRequest(metric, ("campaign",), SEPTEMBER, _c1_filters(), roster=roster),
                shape,
            )
        assert refused.value.code == GRAIN_NOT_SUPPORTED_CODE
        assert "would sum across segment_id" in refused.value.description
    # Spend and sales each name their own currency column (FEAT-002 currency rule).
    currencies = dict(shape.currency_sources)
    assert currencies["dsp_spend"] == "dsp_currency"
    assert currencies["segment_attributed_revenue"] == "purchase_currency"
    # A week isn't measured.
    week = DateRange(date(2026, 9, 21), date(2026, 9, 27))
    with pytest.raises(DomainError) as refused:
        plan(
            catalog,
            PlanRequest(
                "segment_reach",
                ("audience_segment",),
                week,
                {**_c1_filters(), "time_grain": "week"},
                roster=_segment_roster(week, "41001"),
            ),
            shape,
        )
    # Months only: a week isn't a time_grain these rows were measured at.
    assert refused.value.code == REQUIRES_OPTION_CODE
    assert "time_grain=one of (month)" in refused.value.description


def test_a_segment_figure_without_its_roster_is_refused() -> None:
    catalog = load_catalog(ROOT)
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    shape = FactShape.for_fact(FACT, contracts)
    request = PlanRequest("segment_reach", ("audience_segment",), SEPTEMBER, _c1_filters())
    with pytest.raises(InvariantError) as refused:
        plan(catalog, request, shape)
    assert refused.value.code == ROSTER_UNAVAILABLE
    # A campaign roster isn't a segment roster; the segment one plans.
    with pytest.raises(InvariantError):
        plan(
            catalog,
            replace(
                request,
                roster=replace(_segment_roster(SEPTEMBER), kind=RosterKind.CAMPAIGN, filters=()),
            ),
            shape,
        )
    assert plan(catalog, replace(request, roster=_segment_roster(SEPTEMBER)), shape).roster


# --- Chat: which segments did DSP campaign X reach last month -------------------------------------


@dataclass
class _Rows:
    rows: tuple[dict[str, object], ...]
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        return QueryResult(
            rows=self.rows,  # type: ignore[arg-type]
            bytes_scanned=1,
            truncated=False,
            suppression=Suppression(rows_suppressed=False),
        )

    def run_series(self, request: PeriodSeriesRequest) -> QueryResult:
        raise AssertionError(f"no history read expected: {len(request.plans)} month(s)")


_MONTHS = (
    CalendarPeriod(AUGUST.start, AUGUST.end, TimeGrain.MONTH),
    CalendarPeriod(SEPTEMBER.start, SEPTEMBER.end, TimeGrain.MONTH),
)


@dataclass
class _Served:
    months: tuple[CalendarPeriod, ...] = _MONTHS

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        published = {contract.contract_id for contract in request.contracts}
        return ServedPeriods(
            frozenset((item, month) for item in published for month in self.months),
            dict.fromkeys(published, 2),
        )


@dataclass
class _Rosters:
    roster: Roster
    seen: list[RosterRequest] = field(default_factory=list[RosterRequest])

    def __post_init__(self) -> None:
        fixed = self.roster

        def read(request: RosterRequest) -> Roster:
            self.seen.append(request)
            return replace(fixed, period=request.period, filters=request.filters)

        object.__setattr__(self, "roster", read)  # the port method


@dataclass
class _Labels:
    known: dict[str, SegmentLabel]
    seen: list[SegmentLabelRequest] = field(default_factory=list[SegmentLabelRequest])

    def labels(self, request: SegmentLabelRequest) -> dict[str, SegmentLabel]:
        self.seen.append(request)
        return {key: value for key, value in self.known.items() if key in request.segment_ids}


DSP = SegmentNamespace.AMC_DSP_BEHAVIOR
LABELS = {
    "41001": SegmentLabel(DSP, "41001", "Demo - Age 25-34", SegmentProvider.AMAZON),
    "41002": SegmentLabel(
        DSP, "41002", "ExperianDemographicsGenderFemale_LiveRamp", SegmentProvider.THIRD_PARTY
    ),
    "41004": SegmentLabel(DSP, "41004", "Synthetic lifestyle: weekend gardeners", None),
}


def _c1_request(
    metric: str = "segment_reach",
    grains: tuple[str, ...] = ("audience_segment",),
    *,
    campaign: bool = True,
) -> MeasurementToolRequest:
    return MeasurementToolRequest(
        tool=MeasurementTool.METRIC_BY_GRAIN,
        catalog=load_catalog(ROOT),
        contracts=tuple(contract for _, contract in load_contracts(ROOT)),
        metric_name=metric,
        grains=grains,
        window=SEPTEMBER,
        computed_at=NOW,
        correlation_id="corr-c1",
        filters={"marketplace_key": US, **({"campaign_key": CAMPAIGN_KEY} if campaign else {})},
    )


def _c1_chat(  # noqa: PLR0913 — the labels are keyword-only, defaulted to the fixture
    request: MeasurementToolRequest,
    rows: tuple[dict[str, object], ...],
    roster: Roster,
    *,
    known: dict[str, SegmentLabel] = LABELS,
) -> tuple[object, _Rows, _Rosters, _Labels]:
    queries = _Rows(rows)
    rosters = _Rosters(roster)
    labels = _Labels(known)
    response = handle(
        request,
        MeasurementTooling(
            queries=queries,
            publications=StubPublications(),
            periods=_Served(),
            rosters=rosters,  # type: ignore[arg-type]
            labels=DimensionLabelResolver(_LabelCatalog({}), SCOPE, segments=labels),
        ),
    )
    return response, queries, rosters, labels


def _segment_rows(*ids: str) -> tuple[dict[str, object], ...]:
    return tuple(
        {"segment_id": item, measure_alias(0): 1000 + index} for index, item in enumerate(ids)
    )


def test_which_segments_did_campaign_x_reach_names_providers_and_the_roster_gap() -> None:
    roster = _segment_roster(SEPTEMBER, "41001", "41002", "41004")
    roster = replace(
        roster,
        entries=tuple(
            RosterEntry(entry.key, LABELS[entry.key].display) for entry in roster.entries
        ),
    )
    response, queries, rosters, labels = _c1_chat(
        _c1_request(), _segment_rows("41001", "41002", "41003"), roster
    )
    assert dict(queries.seen[0].plan.logical.filters) == {
        "grain_set": "campaign",
        "marketplace_key": US,
        "campaign_key": CAMPAIGN_KEY,
        "segment_namespace": "amc_dsp_behavior",
        "time_grain": "month",
    }
    assert [(item.kind, item.filters) for item in rosters.seen] == [
        (RosterKind.SEGMENT, (("campaign_key", CAMPAIGN_KEY),))
    ]
    # Labelled in the answer's own namespace and marketplace.
    assert (labels.seen[0].namespace, labels.seen[0].marketplace_key) == (DSP, US)
    narrative = _narrative(response)
    assert (
        "Synthetic lifestyle: weekend gardeners · Provider not recorded is not available: too "
        "few shoppers this month, and it was listed last month."
    ) in narrative
    values = cast("tuple[dict[str, object], ...]", response.data.values)  # type: ignore[attr-defined]
    released = " ".join(narrative) + json.dumps(
        [list(row.values()) for row in values], default=str, ensure_ascii=False
    )
    assert "Demo - Age 25-34 · Amazon" in released
    assert "ExperianDemographicsGenderFemale_LiveRamp · Third party" in released
    # A returned segment with no governed name reads by its ID, never a guessed provider.
    assert "Segment 41003 (name not available yet) · Provider not recorded" in released
    assert "Amazon's segments" not in released
    # Control: the whole roster returned names no gap.
    whole, *_ = _c1_chat(_c1_request(), _segment_rows("41001", "41002", "41004"), roster)
    assert not any("is not available" in line for line in _narrative(whole))


def test_a_first_month_roster_is_empty_and_says_so() -> None:
    response, *_ = _c1_chat(
        _c1_request(), _segment_rows("41001"), _segment_roster(SEPTEMBER, first=True)
    )
    assert FIRST_MONTH_NOTE in _narrative(response)
    # Control: a roster read from a previous month has no first-month note.
    other, *_ = _c1_chat(_c1_request(), _segment_rows("41001"), _segment_roster(SEPTEMBER, "41001"))
    assert FIRST_MONTH_NOTE not in _narrative(other)


def test_a_sum_over_segments_is_refused_beside_the_per_segment_answer() -> None:
    roster = _segment_roster(SEPTEMBER, "41001")
    with pytest.raises(DomainError) as refused:
        _c1_chat(_c1_request("segment_impressions", ("campaign",)), (), roster)
    assert refused.value.code == GRAIN_NOT_SUPPORTED_CODE
    assert "would sum across segment_id" in refused.value.description
    # Positive control: per segment, for the same campaign and month, answers.
    response, *_ = _c1_chat(_c1_request("segment_impressions"), _segment_rows("41001"), roster)
    assert len(response.data.values) == 1  # type: ignore[attr-defined]


def test_segment_figures_carry_the_dictionarys_non_additivity_and_no_total() -> None:
    """Run 3 `outside_top_n_segments`: the tool stated a sum the metric forbids, and so did chat.

    `segment_impressions` holds `audience_segment` exact_only (an impression repeats once per
    matched segment), so every measurement tool's result carries that rule beside the figures,
    the evaluator gets it as a rendering, and no breakdown or top-N states a total.
    """
    ids = ("41001", "41002", "41004")
    roster = _segment_roster(SEPTEMBER, *ids)
    request = _c1_request("segment_impressions")
    marker = NonAdditivity(
        across=("segment_id",),
        instruction=(
            "These figures are not additive across audience segment. Don't add them together, "
            "and don't state a total or a share of a total built from them. A total is stated "
            "only when a governed tool returns one."
        ),
    )
    no_total = (
        "No total is stated across these rows: DSP impressions matched to the segment · per "
        "campaign · month isn't additive across audience segment, so these rows' figures are "
        "not their sum."
    )
    asked = {
        MeasurementTool.METRIC_BY_GRAIN: request,
        MeasurementTool.TOP_N: replace(
            request, tool=MeasurementTool.TOP_N, n=2, order_by=RankOrder.VALUE
        ),
        MeasurementTool.COMPARE_WINDOWS: replace(
            request, tool=MeasurementTool.COMPARE_WINDOWS, prior_window=AUGUST
        ),
    }
    totals: dict[MeasurementTool, list[str]] = {}
    for tool, ask in asked.items():
        answered, *_ = _c1_chat(ask, _segment_rows(*ids), roster)
        response = cast("ShapedResponse", answered)
        assert response.data.population_total is None, tool
        assert response.data.additivity == marker, tool
        grounding = response.metadata.grounding
        assert grounding is not None
        assert [
            (item.kind, item.text, item.source_paths)
            for item in grounding.renderings
            if item.rendering_id == f"{grounding.grounding_id}:additivity"
        ] == [(RenderingKind.CONTEXT, marker.instruction, ("data.additivity", "data.grain"))], tool
        totals[tool] = [
            line for line in response.narrative if line.startswith(("Across all", "No total"))
        ]
    # A ranking states neither the total nor the shown rows' share of it; a comparison has none.
    assert totals == {
        MeasurementTool.METRIC_BY_GRAIN: [no_total],
        MeasurementTool.TOP_N: [f"{no_total} 1 of 3 row(s) are not shown."],
        MeasurementTool.COMPARE_WINDOWS: [],
    }
    # Paired controls: one segment's row is its own figure, so there is nothing to add, in a
    # breakdown or in a comparison.
    for ask in (request, asked[MeasurementTool.COMPARE_WINDOWS]):
        one, *_ = _c1_chat(ask, _segment_rows("41001"), roster)
        assert cast("ShapedResponse", one).data.additivity is None, ask.tool
    # Paired control on a distinct count: segment_reach's compared rows carry the same rule.
    compared_reach = replace(asked[MeasurementTool.COMPARE_WINDOWS], metric_name="segment_reach")
    reach, *_ = _c1_chat(compared_reach, _segment_rows(*ids), roster)
    assert cast("ShapedResponse", reach).data.additivity == marker


def test_a_segment_breakdown_over_every_campaign_is_refused_naming_the_campaign() -> None:
    roster = _segment_roster(SEPTEMBER, "41001")
    with pytest.raises(ValidationError) as refused:
        _c1_chat(_c1_request(campaign=False), _segment_rows("41001"), roster)
    assert refused.value.code == ROSTER_REFUSED
    assert refused.value.description == (
        "segment_reach per audience segment is read for one campaign at a time, because each "
        "campaign's segments are listed against its own previous month. Name the campaign."
    )
    # Positive control: with the campaign named, it answers.
    response, *_ = _c1_chat(_c1_request(), _segment_rows("41001"), roster)
    assert len(response.data.values) == 1  # type: ignore[attr-defined]


def _narrative(response: object) -> tuple[str, ...]:
    return cast("tuple[str, ...]", response.narrative)  # type: ignore[attr-defined]


# --- Labels ---------------------------------------------------------------------------------------


def test_segment_labels_come_from_the_rows_namespace_never_the_other_one() -> None:
    labels = _Labels(LABELS)
    resolver = DimensionLabelResolver(_LabelCatalog({}), SCOPE, segments=labels)
    rows: tuple[dict[str, str | int | Decimal | None], ...] = ({"segment_id": "41001"},)
    resolved = resolver.resolve(rows, ("segment_id",), SEPTEMBER, fact=FACT, marketplace=US)
    assert [entry.label for entry in resolved.entries] == ["Demo - Age 25-34 · Amazon"]
    assert labels.seen[-1] == SegmentLabelRequest(DSP, US, ("41001",), SEPTEMBER.end)
    # A fact the namespace map doesn't list (no row namespace) is never labelled by DSP names.
    assert (
        resolver.resolve(rows, ("segment_id",), SEPTEMBER, fact="fct_other", marketplace=US)
    ).entries == ()
    # No marketplace on the row or the answer: the ID is unique only within one, so no name.
    seen = len(labels.seen)
    unscoped = resolver.resolve(rows, ("segment_id",), SEPTEMBER, fact=FACT)
    assert [entry.label for entry in unscoped.entries] == [
        "Segment 41001 (name not available yet) · Provider not recorded"
    ]
    assert len(labels.seen) == seen
    # A row's own marketplace is the one asked for.
    own: tuple[dict[str, str | int | Decimal | None], ...] = (
        {"segment_id": "41001", "marketplace_key": "amazon_us"},
    )
    resolver.resolve(own, ("segment_id",), SEPTEMBER, fact=FACT, marketplace=US)
    assert labels.seen[-1] == SegmentLabelRequest(DSP, "amazon_us", ("41001",), SEPTEMBER.end)
    # Rows naming two marketplaces can't share one label per ID: none is labelled.
    markets: tuple[dict[str, str | int | Decimal | None], ...] = (
        {"segment_id": "41001", "marketplace_key": "amazon_us"},
        {"segment_id": "41001", "marketplace_key": "amazon_ca"},
    )
    assert resolver.resolve(markets, ("segment_id",), SEPTEMBER, fact=FACT).entries == ()
    # Rows naming both namespaces can't share one label per ID: none is labelled.
    mixed: tuple[dict[str, str | int | Decimal | None], ...] = (
        {"segment_id": "41001", "segment_namespace": "amc_dsp_behavior"},
        {"segment_id": "41001", "segment_namespace": "amc_asi"},
    )
    assert (
        resolver.resolve(mixed, ("segment_id",), SEPTEMBER, fact=FACT, marketplace=US)
    ).entries == ()


@dataclass
class _Current:
    publications: dict[str, tuple[object, ...]]
    """Each writer's CURRENT publications, by contract id."""

    def current_for_scope(self, scope: object, horizon: object = None) -> tuple[object, ...]:
        del horizon
        for contract_id, value in self.publications.items():
            pack = next(item for item in load_source_packs(ROOT) if item.contract_id == contract_id)
            contract = next(
                item for _, item in load_contracts(ROOT) if item.contract_id == contract_id
            )
            scopes = publication_scopes_for(
                PublicationScopeRequest(
                    pack=pack,
                    contract=contract,
                    bridge=load_commerce_identity_bridge(ROOT),
                    filters={},
                )
            )
            if scope in scopes:
                return value
        return ()


SEGMENTS_PUBLICATION = PublicationId("publication-segments")


@dataclass
class _Publication:
    rows: tuple[tuple[tuple[str, object], ...], ...]
    partition: object = None
    published_at: datetime = NOW
    publication_id: PublicationId = SEGMENTS_PUBLICATION


def test_the_label_reader_reads_only_its_namespace_writer_and_the_version_on_the_day() -> None:
    def version(
        segment: tuple[str, str, str], start: datetime, end: datetime
    ) -> tuple[tuple[str, object], ...]:
        namespace, segment_id, name = segment
        return tuple(
            sorted(
                {
                    "segment_namespace": namespace,
                    "segment_id": segment_id,
                    "segment_marketplace_key": "amazon_us",
                    "segment_name": name,
                    "provider": None,
                    "valid_from": start,
                    "valid_to": end,
                }.items()
            )
        )

    july, august = datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)
    open_end = datetime(2999, 12, 31, tzinfo=UTC)
    dsp_rows = (
        version(("amc_dsp_behavior", "41001", "Old name"), july, august),
        version(("amc_dsp_behavior", "41001", "Demo - Age 25-34"), august, open_end),
    )
    asi_rows = (version(("amc_asi", "41001", "Synthetic ASI in-market"), july, open_end),)

    class _Canonical:
        def rows(self, publication: _Publication) -> tuple[tuple[tuple[str, object], ...], ...]:
            return publication.rows

    window = DateRange(date(2026, 8, 1), date(2026, 8, 31))
    reader = CanonicalSegmentLabels(
        _Current(
            {
                NAMES: (_Publication(dsp_rows, _Partition(window)),),
                METADATA: (_Publication(asi_rows, _Partition(window)),),
            }
        ),  # type: ignore[arg-type]
        _Canonical(),  # type: ignore[arg-type]
        load_source_packs(ROOT),
        tuple(contract for _, contract in load_contracts(ROOT)),
        load_commerce_identity_bridge(ROOT),
    )
    dsp = reader.labels(SegmentLabelRequest(DSP, US, ("41001",), date(2026, 8, 31)))
    assert {key: (item.namespace, item.name) for key, item in dsp.items()} == {
        "41001": (DSP, "Demo - Age 25-34")
    }
    before = reader.labels(SegmentLabelRequest(DSP, US, ("41001",), date(2026, 7, 31)))
    assert before["41001"].name == "Old name"
    asi = reader.labels(
        SegmentLabelRequest(SegmentNamespace.AMC_ASI, US, ("41001",), date(2026, 8, 31))
    )
    assert asi["41001"].name == "Synthetic ASI in-market"
    assert asi["41001"].display == "Synthetic ASI in-market · Provider not recorded"
    # A marketplace outside the governed scope names nothing.
    assert reader.labels(SegmentLabelRequest(DSP, "amazon_zz", ("41001",), date(2026, 8, 31))) == {}


def test_the_label_reader_reads_only_the_requested_marketplace_and_never_borrows_one() -> None:
    base = load_commerce_identity_bridge(ROOT)
    bridge = replace(
        base,
        marketplace_mappings=(
            *base.marketplace_mappings,
            IdentityMapping(
                SourceIdentifier("amazon.marketplace_id", "A2EUQ1WTGCTBG2"),
                MarketplaceKey("amazon_ca"),
                90,
                date(2000, 1, 1),
            ),
            IdentityMapping(
                SourceIdentifier("amazon.marketplace_id", "A1AM78C64UM0Y8"),
                MarketplaceKey("amazon_mx"),
                91,
                date(2000, 1, 1),
            ),
        ),
    )
    packs = load_source_packs(ROOT)
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    names_pack = next(item for item in packs if item.contract_id == NAMES)
    names_contract = next(item for item in contracts if item.contract_id == NAMES)
    start, end = datetime(2026, 8, 1, tzinfo=UTC), datetime(2999, 12, 31, tzinfo=UTC)

    def row(segment_id: str, market: str, name: str) -> tuple[tuple[str, object], ...]:
        return tuple(
            sorted(
                {
                    "segment_namespace": "amc_dsp_behavior",
                    "segment_id": segment_id,
                    "segment_marketplace_key": market,
                    "segment_name": name,
                    "provider": None,
                    "valid_from": start,
                    "valid_to": end,
                }.items()
            )
        )

    window = DateRange(date(2026, 8, 1), date(2026, 8, 31))
    by_market = {
        # The same namespace and ID in two marketplaces, each with its own name. The US
        # publication also holds a row keyed to Canada, which is never read as the US segment.
        "amazon_us": _Publication(
            (row("41001", "amazon_us", "US name"), row("41002", "amazon_ca", "Stray name")),
            _Partition(window),
        ),
        "amazon_ca": _Publication((row("41001", "amazon_ca", "Canada name"),), _Partition(window)),
    }

    @dataclass
    class _MarketCurrent:
        def current_for_scope(self, scope: object, horizon: object = None) -> tuple[object, ...]:
            del horizon
            for market, publication in by_market.items():
                scopes = publication_scopes_for(
                    PublicationScopeRequest(
                        pack=names_pack,
                        contract=names_contract,
                        bridge=bridge,
                        filters={"marketplace_key": market},
                    )
                )
                if scope in scopes:
                    return (publication,)
            return ()

    class _Canonical:
        def rows(self, publication: _Publication) -> tuple[tuple[tuple[str, object], ...], ...]:
            return publication.rows

    reader = CanonicalSegmentLabels(
        _MarketCurrent(),  # type: ignore[arg-type]
        _Canonical(),  # type: ignore[arg-type]
        packs,
        contracts,
        bridge,
    )
    on = date(2026, 8, 31)

    def names(market: str) -> dict[str, str | None]:
        found = reader.labels(SegmentLabelRequest(DSP, market, ("41001", "41002"), on))
        return {key: item.name for key, item in found.items()}

    assert names("amazon_us") == {"41001": "US name"}
    assert names(US) == {"41001": "US name"}  # a governed alias reads its marketplace
    assert names("amazon_ca") == {"41001": "Canada name"}
    # A marketplace with no names publication borrows no other marketplace's name.
    assert names("amazon_mx") == {}


@dataclass
class _Bucket:
    window: DateRange


@dataclass
class _Partition:
    window: DateRange

    @property
    def bucket(self) -> _Bucket:
        return _Bucket(self.window)


# --- The segment roster reader --------------------------------------------------------------------


def _roster_request() -> RosterRequest:
    return RosterRequest(
        kind=RosterKind.SEGMENT,
        period=SEPTEMBER,
        marketplace_key=US,
        platform_key="amazon_ads",
        filters=(("campaign_key", CAMPAIGN_KEY),),
    )


def test_the_segment_roster_is_the_campaigns_segments_of_the_month_before() -> None:
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    queries = _Rows(_segment_rows("41004", "41001"))
    reader = SegmentRosterReader(
        catalog=load_catalog(ROOT),
        contracts=contracts,
        publications=StubPublications(),
        periods=_Served(),
        queries=queries,
        labels=_Labels(LABELS),
        clock=lambda: NOW,
    )
    roster = reader.roster(_roster_request())
    logical = queries.seen[0].plan.logical
    assert (logical.window, logical.group_by) == (AUGUST, ("segment_id",))
    assert logical.filters["campaign_key"] == CAMPAIGN_KEY
    assert (roster.period, roster.first_period) == (SEPTEMBER, False)
    assert roster.entries == (
        RosterEntry("41001", "Demo - Age 25-34 · Amazon"),
        RosterEntry("41004", "Synthetic lifestyle: weekend gardeners · Provider not recorded"),
    )
    # The previous month's segments are named in the request's marketplace.
    assert cast("_Labels", reader.labels).seen[-1] == SegmentLabelRequest(
        DSP, US, ("41001", "41004"), AUGUST.end
    )


JULY = DateRange(date(2026, 7, 1), date(2026, 7, 31))
_JULY = CalendarPeriod(JULY.start, JULY.end, TimeGrain.MONTH)
OTHER_CAMPAIGN = "campaign:southwind-dsp"


@dataclass
class _History:
    """Segment rows per (month start, campaign): a read returns only its own campaign's months.

    `seen` records each downstream query as the months it read, so one entry is one statement.
    """

    rows: dict[tuple[date, str], tuple[dict[str, object], ...]]
    seen: list[tuple[DateRange, ...]] = field(default_factory=list[tuple[DateRange, ...]])
    series_truncated: bool = False

    def run(self, request: QueryRequest) -> QueryResult:
        logical = request.plan.logical
        self.seen.append((logical.window,))
        return self._result(
            self.rows.get((logical.window.start, logical.filters["campaign_key"]), ())
        )

    def run_series(self, request: PeriodSeriesRequest) -> QueryResult:
        windows = tuple(item.logical.window for item in request.plans)
        self.seen.append(windows)
        # The port's contract: refuse whatever `run` would refuse for any one month.
        if any(
            publication.evidence.withheld_only
            for item in request.plans
            for publication in item.primary
        ):
            raise ValidationError(ALL_ROWS_WITHHELD, "withheld whole")
        campaign = request.plans[0].logical.filters["campaign_key"]
        return self._result(
            tuple(
                {**row, "period_start": window.start.isoformat()}
                for window in windows
                for row in self.rows.get((window.start, campaign), ())
            ),
            truncated=self.series_truncated,
        )

    @staticmethod
    def _result(rows: tuple[dict[str, object], ...], *, truncated: bool = False) -> QueryResult:
        return QueryResult(
            rows=rows,  # type: ignore[arg-type]
            bytes_scanned=1,
            truncated=truncated,
            suppression=Suppression(rows_suppressed=False),
        )


def _history_reader(
    months: tuple[CalendarPeriod, ...],
    rows: dict[tuple[date, str], tuple[dict[str, object], ...]],
    publications: StubPublications | None = None,
) -> SegmentRosterReader:
    return SegmentRosterReader(
        catalog=load_catalog(ROOT),
        contracts=tuple(contract for _, contract in load_contracts(ROOT)),
        publications=publications or StubPublications(),
        periods=_Served(months),
        queries=_History(rows),
        labels=_Labels(LABELS),
        clock=lambda: NOW,
    )


FIRST_MONTH = Roster(
    RosterKind.SEGMENT,
    SEPTEMBER,
    US,
    "amazon_ads",
    (),
    filters=(("campaign_key", CAMPAIGN_KEY),),
    first_period=True,
)


def test_a_first_month_roster_is_empty_but_a_publication_gap_is_refused() -> None:
    # No earlier month is published at all: the campaign's first month, an empty roster.
    reader = _history_reader((_MONTHS[1],), {})
    assert reader.roster(_roster_request()) == FIRST_MONTH
    assert cast("_History", reader.queries).seen == []
    # This campaign was measured in July and August isn't published: August is a gap in its
    # history, not its first month, so September's breakdown is refused.
    gap = _history_reader((_JULY, _MONTHS[1]), {(JULY.start, CAMPAIGN_KEY): _segment_rows("41001")})
    with pytest.raises(DomainError) as refused:
        gap.roster(_roster_request())
    assert refused.value.code == ROSTER_REFUSED
    assert refused.value.description == (
        "Segment delivery from 2026-08-01 to 2026-08-31 isn't published, though this campaign "
        "was measured in an earlier month, so which segments it reached then can't be read and "
        "no segment breakdown is answered against it."
    )
    assert cast("_History", gap.queries).seen == [(JULY,)]
    # Control: with August published too, the roster is read from August alone.
    whole = _history_reader(
        (_JULY, *_MONTHS),
        {
            (JULY.start, CAMPAIGN_KEY): _segment_rows("41002"),
            (AUGUST.start, CAMPAIGN_KEY): _segment_rows("41001"),
        },
    )
    assert whole.roster(_roster_request()) == replace(
        FIRST_MONTH,
        entries=(RosterEntry("41001", "Demo - Age 25-34 · Amazon"),),
        first_period=False,
    )
    assert cast("_History", whole.queries).seen == [(AUGUST,)]


def test_another_campaigns_older_month_never_decides_this_campaigns_first_month() -> None:
    other = {(JULY.start, OTHER_CAMPAIGN): _segment_rows("41001")}
    # August isn't published and only another campaign was measured in July: September is this
    # campaign's first observed month, not a gap in its history.
    reader = _history_reader((_JULY, _MONTHS[1]), other)
    assert reader.roster(_roster_request()) == FIRST_MONTH
    assert cast("_History", reader.queries).seen == [(JULY,)]
    # August is published without this campaign, and July holds only another campaign: still
    # this campaign's first month, so the roster is empty and says so.
    published = _history_reader(
        (_JULY, *_MONTHS), {**other, (AUGUST.start, OTHER_CAMPAIGN): _segment_rows("41002")}
    )
    assert published.roster(_roster_request()) == FIRST_MONTH
    assert cast("_History", published.queries).seen == [(AUGUST,), (JULY,)]
    # Control: this campaign was measured in July and returned nothing in August, so August's
    # empty roster is read as such, not as a first month.
    returning = _history_reader(
        (_JULY, *_MONTHS), {**other, (JULY.start, CAMPAIGN_KEY): _segment_rows("41004")}
    )
    assert returning.roster(_roster_request()) == replace(FIRST_MONTH, first_period=False)
    assert cast("_History", returning.queries).seen == [(AUGUST,), (JULY,)]


def _months_before(month: DateRange, count: int) -> tuple[CalendarPeriod, ...]:
    """Return the `count` calendar months ending the month before `month`, newest first."""
    found: list[CalendarPeriod] = []
    end = month.start - timedelta(days=1)
    for _ in range(count):
        start = end.replace(day=1)
        found.append(CalendarPeriod(start, end, TimeGrain.MONTH))
        end = start - timedelta(days=1)
    return tuple(found)


@pytest.mark.parametrize("retained", [1, 3, 36])
def test_a_campaigns_history_is_one_read_however_many_months_are_retained(retained: int) -> None:
    """The serving work stays fixed as unrelated retained months accumulate (FEAT-008-B4-1)."""
    older = _months_before(AUGUST, retained)
    oldest = older[-1]
    unrelated = {(item.start, OTHER_CAMPAIGN): _segment_rows("41001") for item in older}

    def counted(
        months: tuple[CalendarPeriod, ...],
        rows: dict[tuple[date, str], tuple[dict[str, object], ...]],
    ) -> tuple[SegmentRosterReader, StubPublications, _History]:
        publications = StubPublications()
        reader = _history_reader(months, rows, publications)
        return reader, publications, cast("_History", reader.queries)

    newest_first = tuple(DateRange(item.start, item.end) for item in older)
    # August isn't published and only another campaign ran before it: this campaign's first
    # month, decided by one selection and one statement over every retained month, newest first.
    reader, publications, queries = counted((*older, _MONTHS[1]), unrelated)
    assert reader.roster(_roster_request()) == FIRST_MONTH
    assert queries.seen == [newest_first]
    assert len(publications.seen) == 1
    assert tuple(plan.window for plan in publications.seen[0].plans) == newest_first
    # Paired gap control: the campaign ran only in the oldest retained month, so August is a gap
    # in its history, refused after the same single read.
    own = {**unrelated, (oldest.start, CAMPAIGN_KEY): _segment_rows("41004")}
    reader, publications, queries = counted((*older, _MONTHS[1]), own)
    with pytest.raises(DomainError) as refused:
        reader.roster(_roster_request())
    assert refused.value.code == ROSTER_REFUSED
    assert (queries.seen, len(publications.seen)) == ([newest_first], 1)
    # August published without this campaign: the prior-month roster read plus one history read.
    published = {**unrelated, (AUGUST.start, OTHER_CAMPAIGN): _segment_rows("41002")}
    reader, publications, queries = counted((*older, *_MONTHS), published)
    assert reader.roster(_roster_request()) == FIRST_MONTH
    assert (queries.seen, len(publications.seen)) == ([(AUGUST,), newest_first], 2)
    # Paired first-month control's opposite: having run in the oldest month, August's empty
    # roster is read as such, not as a first month, after the same two reads.
    reader, publications, queries = counted(
        (*older, *_MONTHS), {**published, (oldest.start, CAMPAIGN_KEY): _segment_rows("41004")}
    )
    assert reader.roster(_roster_request()) == replace(FIRST_MONTH, first_period=False)
    assert (queries.seen, len(publications.seen)) == ([(AUGUST,), newest_first], 2)


@dataclass
class _WithheldEvidence:
    """Publishable evidence proving Amazon withheld the month's every attributable row."""

    withheld_only: bool = True
    publishable: bool = True
    readable_historical_success: bool = False
    subject: None = None
    qualified_warnings: tuple[str, ...] = ()


@dataclass
class _WithheldMonth(StubPublications):
    """Bind as the stub does, with one month's publications withheld whole by Amazon."""

    month: DateRange = JULY

    def select(self, request: PublicationSelectionRequest) -> PublicationSelection:
        selection = super().select(request)
        return replace(
            selection,
            plans=tuple(
                replace(
                    item,
                    primary=tuple(
                        replace(publication, evidence=cast("Any", _WithheldEvidence()))
                        for publication in item.primary
                    ),
                )
                if item.logical.window == self.month
                else item
                for item in selection.plans
            ),
        )


def test_a_month_amazon_withheld_whole_is_reached_only_as_a_newest_first_walk_would() -> None:
    """A withheld month can't say whether the campaign ran, so it decides only what it reaches."""
    older = _months_before(AUGUST, 3)  # July, June, May; August isn't published
    june, may = (DateRange(item.start, item.end) for item in older[1:])
    # The campaign ran in July, newer than the withheld June: the read stops at July, so the
    # withheld month is never reached and August is a gap in the campaign's history.
    found = _history_reader(
        (*older, _MONTHS[1]),
        {(JULY.start, CAMPAIGN_KEY): _segment_rows("41001")},
        _WithheldMonth(month=june),
    )
    with pytest.raises(DomainError) as gap:
        found.roster(_roster_request())
    assert gap.value.code == ROSTER_REFUSED
    assert cast("_History", found.queries).seen == [(JULY,)]
    # Control: absent from July, the walk reads on into the withheld June and older in one
    # statement, which refuses before it runs, as the month's own read would. Nothing is guessed.
    absent = _history_reader((*older, _MONTHS[1]), {}, _WithheldMonth(month=june))
    with pytest.raises(ValidationError) as withheld:
        absent.roster(_roster_request())
    assert withheld.value.code == ALL_ROWS_WITHHELD
    assert cast("_History", absent.queries).seen == [(JULY,), (june, may)]
    # Withheld newest: nothing newer is read, and the one read over all three refuses.
    newest = _history_reader(
        (*older, _MONTHS[1]), {(june.start, CAMPAIGN_KEY): _segment_rows("41001")}, _WithheldMonth()
    )
    with pytest.raises(ValidationError):
        newest.roster(_roster_request())
    assert cast("_History", newest.queries).seen == [(JULY, june, may)]


def test_a_truncated_history_read_proves_observation_but_never_absence() -> None:
    """A cut page holding a row proves the campaign ran; an empty cut page proves nothing."""
    older = _months_before(AUGUST, 3)  # July, June, May; August isn't published
    span = "from 2026-05-01 to 2026-07-31"

    def reader(
        rows: dict[tuple[date, str], tuple[dict[str, object], ...]], *, cut: bool
    ) -> SegmentRosterReader:
        built = _history_reader((*older, _MONTHS[1]), rows)
        return replace(built, queries=_History(rows, series_truncated=cut))

    ran = {(JULY.start, CAMPAIGN_KEY): _segment_rows("41001")}
    # Truncated with a row of this campaign: observed, so August is a gap and refused as one.
    with pytest.raises(DomainError) as gap:
        reader(ran, cut=True).roster(_roster_request())
    assert gap.value.code == ROSTER_REFUSED
    assert "isn't published, though this campaign was measured" in gap.value.description
    # Truncated and empty: absence can't be read from a cut page, so the roster is refused as
    # unreadable rather than guessed a first month.
    with pytest.raises(DomainError) as unreadable:
        reader({}, cut=True).roster(_roster_request())
    assert unreadable.value.code == ROSTER_REFUSED
    assert unreadable.value.description == (
        f"Which segments this campaign reached {span} can't be read in full, so no segment "
        "breakdown is answered against it."
    )
    # Control: the same empty read, untruncated, is this campaign's first month.
    assert reader({}, cut=False).roster(_roster_request()) == FIRST_MONTH


def test_a_truncated_bake_roster_read_refuses_its_page_and_chat_keeps_its_answer() -> None:
    """ADR-142: at the bake ceiling the cut read is rows_over_ceiling with its width."""
    older = _months_before(AUGUST, 3)
    built = _history_reader((*older, _MONTHS[1]), {})
    cut = replace(built, queries=_History({}, series_truncated=True))
    bake = replace(_roster_request(), ceiling=RowCeiling.BAKE)
    with pytest.raises(IsolatedRefusal) as refused:
        cut.roster(bake)
    assert (refused.value.reason, refused.value.scope) == (
        IsolatedRefusalReason.ROWS_OVER_CEILING,
        RefusalScope.PAGE,
    )
    assert dict(refused.value.refusal_context) == {"max_rows": row_ceiling(RowCeiling.BAKE)}
    # Paired: chat's read (the narration ceiling) keeps its own ROSTER_REFUSED answer.
    with pytest.raises(DomainError) as chat:
        cut.roster(_roster_request())
    assert chat.value.code == ROSTER_REFUSED and not isinstance(chat.value, IsolatedRefusal)


def test_history_past_the_selection_bound_is_refused_as_the_rosters_own_answer() -> None:
    """The selector's bound is reached over history the user never named, so the roster refuses."""
    older = _months_before(AUGUST, 3)
    too_large = ValidationError(SELECTION_TOO_LARGE, "Narrow the date range or population.")
    reader = _history_reader((*older, _MONTHS[1]), {}, StubPublications(failure=too_large))
    with pytest.raises(DomainError) as refused:
        reader.roster(_roster_request())
    assert refused.value.code == ROSTER_REFUSED
    assert refused.value.description == (
        "Which segments this campaign reached from 2026-05-01 to 2026-07-31 can't be read in "
        "full, so no segment breakdown is answered against it."
    )
    assert refused.value.context == {
        "roster": RosterKind.SEGMENT.value,
        "roster_refusal": "unreadable",
        "reason": str(SELECTION_TOO_LARGE),
    }
    assert refused.value.__cause__ is too_large
    assert cast("_History", reader.queries).seen == []


def _bound_month(month: DateRange, metric: str = "segment_reach") -> BoundQueryPlan:
    logical = plan(
        load_catalog(ROOT),
        PlanRequest(
            metric, ("audience_segment",), month, _c1_filters(), roster=_segment_roster(month)
        ),
        FactShape.for_fact(FACT, tuple(contract for _, contract in load_contracts(ROOT))),
    )
    return bound_plan(logical, NOW)


def test_a_period_series_is_one_statement_fencing_each_month_to_its_own_publications() -> None:
    """Asserted whole, as `compose` is.

    The OR group's own parentheses keep every filter on every month, and only the whole string
    shows a filter bound to the last month alone.
    """
    july, august = _bound_month(JULY), _bound_month(AUGUST)
    composed = compose_series((august, july), max_rows=10)
    assert (len(august.primary_ids), len(july.primary_ids)) == (31, 31)
    # Each month's 31 publication placeholders written as `…`, so a wrong count can't match.
    ids = "(" + ", ".join("?" for _ in range(31)) + ")"
    assert composed.sql.replace(ids, "(…)") == (
        # No fold: pack 8 loads under discard_suppressed_rows and stores no rows_suppressed
        # (ADR-099 point 7); a withheld segment reads through its roster as not available.
        'SELECT "period_start", "period_end", "segment_id", '
        'SUM("segment_reach") AS value_0 '
        'FROM "fct_segment_delivery" '
        'WHERE (("period_start" = CAST(? AS DATE) AND "period_end" = CAST(? AS DATE) '
        'AND "publication_id" IN (…)) '
        'OR ("period_start" = CAST(? AS DATE) AND "period_end" = CAST(? AS DATE) '
        'AND "publication_id" IN (…))) '
        'AND "campaign_key" = ? AND "grain_set" = ? AND "marketplace_key" = ? '
        'AND "segment_namespace" = ? AND "time_grain" = ? '
        'GROUP BY "period_start", "period_end", "segment_id" '
        'ORDER BY "period_start" DESC, "period_end" DESC, "segment_id" '
        "LIMIT 11"
    )
    # Each month's bounds sit beside its own publication ids, newest first, then the filters.
    assert composed.parameters == (
        "2026-08-01",
        "2026-08-31",
        *august.primary_ids,
        "2026-07-01",
        "2026-07-31",
        *july.primary_ids,
        CAMPAIGN_KEY,
        "campaign",
        US,
        "amc_dsp_behavior",
        "month",
    )
    assert composed.sql.count("?") == len(composed.parameters)
    assert CAMPAIGN_KEY not in composed.sql
    # Control: one month's series selects what that month's own read selects, plus its period.
    projection = compose(july, max_rows=10).sql.split(" FROM ")[0].removeprefix("SELECT ")
    assert compose_series((july,), max_rows=10).sql.split(" FROM ")[0] == (
        f'SELECT "period_start", "period_end", {projection}'
    )


def test_a_period_series_repeats_one_read_and_refuses_anything_else() -> None:
    july, august = _bound_month(JULY), _bound_month(AUGUST)
    assert PeriodSeriesRequest((august, july), MAX_ROWS_CEILING).plans == (august, july)
    with pytest.raises(ValueError, match="at least one period"):
        PeriodSeriesRequest((), 10)
    with pytest.raises(ValueError, match="differ beyond period"):
        PeriodSeriesRequest((august, _bound_month(JULY, "segment_impressions")), 10)
    with pytest.raises(ValueError, match="each period once"):
        PeriodSeriesRequest((july, july), 10)
    with pytest.raises(ValueError, match="exceeds"):
        PeriodSeriesRequest((july,), MAX_ROWS_CEILING + 1)


def test_a_period_series_refuses_a_month_withheld_whole_before_athena() -> None:
    class _NoAthena:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"preflight must not call Athena: {name}")

    july = _bound_month(JULY)
    withheld = replace(
        july,
        primary=tuple(
            replace(item, evidence=cast("Any", _WithheldEvidence())) for item in july.primary
        ),
    )
    adapter = AthenaQueryAdapter(
        AthenaQueryTarget(
            "unit-workgroup", "unit-current", 0, INTERACTIVE_WAIT, ResultTransport.PAGED
        ),
        cast("Any", _NoAthena()),
    )
    with pytest.raises(ValidationError) as refused:
        adapter.run_series(PeriodSeriesRequest((_bound_month(AUGUST), withheld), 10))
    assert refused.value.code == ALL_ROWS_WITHHELD


def test_roster_readers_route_segments_and_refuse_without_a_segment_reader() -> None:
    @dataclass
    class _Fixed:
        roster_: Roster

        def roster(self, request: RosterRequest) -> Roster:  # noqa: ARG002
            return self.roster_

    spend = _Fixed(replace(_segment_roster(SEPTEMBER), kind=RosterKind.CAMPAIGN, filters=()))
    segments = _Fixed(_segment_roster(SEPTEMBER, "41001"))
    routed = RosterReaders(spend=spend, segments=segments)
    assert routed.roster(_roster_request()).kind is RosterKind.SEGMENT
    assert (
        routed.roster(replace(_roster_request(), kind=RosterKind.CAMPAIGN, filters=())).kind
        is RosterKind.CAMPAIGN
    )
    with pytest.raises(DomainError) as refused:
        RosterReaders(spend=spend).roster(_roster_request())
    assert refused.value.code == ROSTER_REFUSED


def test_a_segment_roster_is_graded_and_never_withdrawn() -> None:
    roster = _segment_roster(SEPTEMBER, *(str(41000 + index) for index in range(30)))
    outcome = apply_roster(roster, roster.keys[:10], coarser_grain_set(FACT, "campaign"))
    assert (outcome.withdrawn, outcome.loss.grade) == (False, LossGrade.DEGRADED)
    assert roster_lines(outcome)[-1] == DEGRADED_NOTE


def test_a_question_about_one_segment_is_graded_on_the_campaigns_whole_delivery() -> None:
    """A pinned segment reads its delivery's keys; a missing one reads not available."""
    roster = replace(
        _segment_roster(SEPTEMBER, "41001", "41004"),
        entries=(
            RosterEntry("41001", LABELS["41001"].display),
            RosterEntry("41004", LABELS["41004"].display),
        ),
    )
    request = replace(_c1_request(), filters={**_c1_request().filters, "segment_id": "41004"})
    response, queries, *_ = _c1_chat(request, (), roster)
    # The whole delivery is read first (its keys grade the request), then the pinned segment.
    delivery, pinned = (dict(item.plan.logical.filters) for item in queries.seen)
    assert "segment_id" not in delivery
    assert pinned == {**delivery, "segment_id": "41004"}
    assert (
        "Synthetic lifestyle: weekend gardeners · Provider not recorded is not available: too "
        "few shoppers this month, and it was listed last month."
    ) in _narrative(response)
    # Control: the pinned segment that came back answers with its figure and no gap line.
    found, *_ = _c1_chat(
        replace(request, filters={**request.filters, "segment_id": "41001"}),
        _segment_rows("41001"),
        roster,
    )
    assert not any("is not available" in line for line in _narrative(found))


def test_a_segment_ratio_plans_within_one_campaign_and_segment_row() -> None:
    catalog = load_catalog(ROOT)
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    roster = _segment_roster(SEPTEMBER, "41001")
    shape = FactShape.for_fact(FACT, contracts)
    # Sales over spend divides the purchase currency by the DSP one, so no query computes it
    # (FEAT-002 currency rule; FEAT-007's return by frequency is refused the same way).
    with pytest.raises(DomainError) as refused:
        plan(
            catalog,
            PlanRequest(
                "segment_roas_attributed",
                ("audience_segment",),
                SEPTEMBER,
                _c1_filters(),
                roster=roster,
            ),
            shape,
        )
    assert refused.value.code == NOT_COMPUTABLE_CODE
    assert "own currency column" in refused.value.description
    # Positive controls: CPM is labelled from the DSP currency (impressions aren't money), and
    # the purchase rate carries no currency.
    cpm = plan(
        catalog,
        PlanRequest("segment_cpm", ("audience_segment",), SEPTEMBER, _c1_filters(), roster=roster),
        shape,
    )
    assert (cpm.measures, cpm.currency_source) == (("dsp_spend", "impressions"), "dsp_currency")
    for metric in ("segment_cpm", "segment_purchase_rate"):
        ratio = plan(
            catalog,
            PlanRequest(metric, ("audience_segment",), SEPTEMBER, _c1_filters(), roster=roster),
            shape,
        )
        assert ratio.group_by == ("segment_id",)
        with pytest.raises(DomainError) as refused:
            plan(
                catalog,
                PlanRequest(metric, ("campaign",), SEPTEMBER, _c1_filters(), roster=roster),
                FactShape.for_fact(FACT, contracts),
            )
        assert refused.value.code == GRAIN_NOT_SUPPORTED_CODE


def test_a_published_month_with_no_segments_still_asks_the_label_port() -> None:
    """Review #8: the roster's own read pins its label scopes, even with no segment (a pin pass).

    August is published but empty for this campaign, as a bake's empty-answer pass sees it; the
    roster is still this campaign's first month, and the label port is asked first.
    """
    reader = _history_reader(_MONTHS, {})
    assert reader.roster(_roster_request()) == FIRST_MONTH
    assert [request.segment_ids for request in cast("_Labels", reader.labels).seen] == [()]
