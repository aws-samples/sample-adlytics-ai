# SPDX-License-Identifier: MIT-0
"""Packs 10-12: the buyer segment profile (C2) and the Audiences server side (FEAT-009).

Design §5.5 "Buyer profile" and §6.5 C2: packs 10 and 11 profile the fenced, Amazon-account
shoppers your ads reached with the month's ASI snapshot (paid), pack 12 the DSP-exposed shoppers
with their DSP behaviour segments. Each delivery is its own profile path and base; shares and the
index are computed inside one delivery, a missing total makes every share of it not available,
and nothing sums shares or compares paths. Every refusal sits beside a positive control.
"""

from __future__ import annotations

import json
import pathlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from publication_support import StubPublications
from test_audience_segments import (
    _delivery,  # pyright: ignore[reportPrivateUsage]
    _Labels,  # pyright: ignore[reportPrivateUsage]
)
from test_dimension_labels import (
    SCOPE,
    _LabelCatalog,  # pyright: ignore[reportPrivateUsage]
)
from test_semantic_sidecar import (
    MemoryCanonical,
    _case,  # pyright: ignore[reportPrivateUsage]
)

from adapters.config import PresetArtifactBakeSettings
from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from adapters.inbound.tool_handlers.measurement import (
    WITHHELD_REASON_FIELD,
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    handle,
)
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.ingest_delivery.semantic_ingestion import (
    SemanticIngestionRequest,
    ingest_semantic_delivery,
)
from adapters.jobs.ingest_delivery.semantic_sidecar import SemanticSidecarOutcome
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher import audiences_source
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    AUDIENCES_BODY_OVER_BOUND,
    AUDIENCES_ENVELOPE_MAX_BYTES,
    AUDIENCES_RENDER_FORMAT,
    AUDIENCES_ROW_MAX_BYTES,
    AUDIENCES_SECTIONS,
    AUDIENCES_TEMPORAL_POLICY,
    AudiencesArtifactRenderer,
    AudiencesRenderInput,
    AudiencesRenderResult,
    RetainedAudiencesPresetArtifactSource,
    audiences_policy_declared,
    governed_campaign_names,
    retained_audiences_preset_artifact_source,
)
from adapters.jobs.preset_artifact_publisher.bake_results import (
    BakePublication,
    BakeUnavailable,
)
from adapters.jobs.preset_artifact_publisher.baker import (
    AudiencesPresetArtifactBaker,
    CampaignPresetArtifactBaker,
    RegisteredPresetArtifactBaker,
    UnavailableCampaignPresetArtifactSource,
    UnavailablePresetArtifactBaker,
)
from adapters.jobs.preset_artifact_publisher.transport import BakeIntent
from adapters.outbound.bake_sources import BakeSourceReads
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.segment_roster import SegmentRosterReader
from adapters.producer.packs import discard_policies, load_query_packs, pack_by_id
from domain.capabilities.paid_features import PaidFeatureState
from domain.catalog.capability import (
    GRAIN_NOT_SUPPORTED_CODE,
    NOT_COMPUTABLE_CODE,
    REQUIRES_OPTION_CODE,
)
from domain.measurement.figures import measure_alias
from domain.measurement.planning import FactShape, PlanRequest, plan
from domain.measurement.rosters import (
    ROSTER_REFUSED,
    Roster,
    RosterEntry,
    RosterKind,
)
from domain.presentation.audiences import (
    BOUNDARY_TEXT,
    C2_BUYER_LABEL,
    DSP_VIEW,
    NO_DELIVERY_TEXT,
    NO_IMPRESSIONS_TEXT,
    NO_SALES_TEXT,
    NO_SPEND_TEXT,
    PAID_VIEW,
    ROAS_CURRENCY_TEXT,
    ROSTER_GAP_TEXT,
    SEGMENT_PARTIAL_TEXT,
    SPEND_CURRENCY_TEXT,
    TOTAL_NOT_AVAILABLE_TEXT,
    VIEW_NAMESPACES,
    AudienceMarket,
    AudienceProfileArtifact,
    AudienceUnavailableArtifact,
    CampaignAudiences,
    CampaignRowRequest,
    DeliveryState,
    OverIndexSlotRequest,
    PaidState,
    ProfileDelivery,
    ProfileSegment,
    ProfileView,
    ViewRequest,
    audiences_artifact_payload,
    bonferroni_z,
    build_view,
    campaign_segment,
    over_index_findings,
    require_audiences_unavailable_artifact_payload,
    segment_figures,
    segment_over_index_slot,
)
from domain.presentation.insights import (
    InsightAbsenceClass,
    InsightCalibrationKind,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    OverIndexPolicy,
)
from domain.presentation.page_schemas import (
    Cardinality,
    PageSection,
    PageTime,
    PresentationPageMode,
    TemporalComparisonPolicy,
    TemporalPresetId,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactPointer,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactVariant,
    SourceUnchanged,
    canonical_preset_artifact_json,
)
from domain.producer.models import AccessTier, PeriodGrain, SuppressionPolicy
from domain.producer.parameters import first_thursday, resolve_period_parameters
from domain.semantics.audience_segments import (
    ASI_NEEDS_PAID_FEATURE_TEXT,
    SEGMENT_IDS_SENTENCE,
    SegmentLabel,
    SegmentNamespace,
    SegmentProvider,
)
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import DomainError, InvariantError, ValidationError
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from ports.outbound.canonical import CanonicalDestination
from ports.outbound.query import QueryRequest, QueryResult, QueryRow
from ports.outbound.rosters import RosterRequest
from ports.outbound.segment_labels import SegmentLabelRequest
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-103", "ADR-107")]


def _reads() -> BakeSourceReads:
    """Return a fictional pinnable reader: composition builds over it and never reads."""
    return BakeSourceReads(cast("Any", None), cast("Any", None))


ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT)
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
INSIGHT = next(
    contract
    for _, contract in load_insight_contracts(ROOT)
    if contract.id is InsightSlotId.SEGMENT_OVER_INDEX
)
_POLICY = INSIGHT.over_index_policy
assert _POLICY is not None
POLICY: OverIndexPolicy = _POLICY
INMARKET = "amc-segment-buyer-profile-inmarket"
LIFESTYLE = "amc-segment-buyer-profile-lifestyle"
DSP_EXPOSED = "amc-segment-buyer-profile-dsp-exposed"
METADATA = "amc-segment-metadata"
PERFORMANCE = "amc-audience-performance"
PACKS = (INMARKET, LIFESTYLE, DSP_EXPOSED)
FACT = "fct_segment_profile"
US = "amazon_us"
NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
SEPTEMBER = DateRange(date(2026, 9, 1), date(2026, 9, 30))
OCTOBER = DateRange(date(2026, 10, 1), date(2026, 10, 31))
AUGUST = DateRange(date(2026, 8, 1), date(2026, 8, 31))
MONTH = CalendarPeriod(SEPTEMBER.start, SEPTEMBER.end, TimeGrain.MONTH)
OCTOBER_MONTH = CalendarPeriod(OCTOBER.start, OCTOBER.end, TimeGrain.MONTH)
ASI = SegmentNamespace.AMC_ASI
DSP = SegmentNamespace.AMC_DSP_BEHAVIOR


# --- packs ---------------------------------------------------------------------------------------


def test_packs_10_to_12_are_monthly_discard_packs_and_10_11_are_paid_on_the_snapshot_range() -> (
    None
):
    packs = {pack_id: pack_by_id(ROOT, pack_id) for pack_id in PACKS}
    for pack in packs.values():
        assert pack.period_policy is not None
        assert pack.period_policy.grains == (PeriodGrain.MONTH,)
        assert pack.suppression_policy is SuppressionPolicy.DISCARD_SUPPRESSED_ROWS
        assert (pack.expected_bar, pack.effective_cadence_lag_days) == (2, 2)
    for pack_id in (INMARKET, LIFESTYLE):
        pack = packs[pack_id]
        assert pack.access_tier is AccessTier.PAID_FEATURE
        assert [
            (item.name, item.commerce_identity, item.period_value) for item in pack.input_parameters
        ] == [
            ("dsp_campaign_country", "amc.campaign_country", None),
            ("segment_marketplace_id", "amc.segment_marketplace_id", None),
            ("snapshot_end", None, "snapshot_end"),
            ("snapshot_start", None, "snapshot_start"),
            ("sponsored_marketplace_name", "amc.marketplace_name", None),
        ]
        assert "segment_marketplace_id = CUSTOM_PARAMETER('segment_marketplace_id')" in pack.sql
        assert "CAST(CUSTOM_PARAMETER('snapshot_start') AS TIMESTAMP)" in pack.sql
        assert "UNION ALL SELECT 'profiled_total' AS row_kind" in pack.sql
    assert "FROM audience_segments_amer_inmarket_snapshot" in packs[INMARKET].sql
    assert "FROM audience_segments_amer_lifestyle_snapshot" in packs[LIFESTYLE].sql
    standard = packs[DSP_EXPOSED]
    assert standard.access_tier is AccessTier.STANDARD
    assert "FROM dsp_impressions_by_user_segments" in standard.sql
    # Pack 12 has no fence and no ID-space restriction: both bases come from its one table.
    assert "exposure-set start" not in standard.sql and "adUserId" not in standard.sql
    installed = {item.contract_id for item in discard_policies(load_query_packs(ROOT))}
    assert set(PACKS) <= installed


def test_packs_10_11_restrict_to_account_ids_after_the_fence_with_an_impression_in_the_period() -> (
    None
):
    for pack_id in (INMARKET, LIFESTYLE):
        text = pack_by_id(ROOT, pack_id).sql
        after = text[text.index("/* exposure-set end */") :]
        exposed = after[after.index("exposed AS (") : after.index("snapshot AS (")]
        assert "WHERE user_id_type = 'adUserId'" in exposed
        assert "AND imps > 0" in exposed
        assert "AND touch_hour < BUILT_IN_PARAMETER('TIME_WINDOW_END')" in exposed
        # The restriction is after the fence, which stays byte-identical (test_exposure_fence).
        assert "adUserId" not in text[: text.index("/* exposure-set end */")]


def test_the_generated_tables_add_three_sa_tables_and_fct_segment_profile_with_no_residual() -> (
    None
):
    generated = json.loads((ROOT / "contracts/generated/v2-tables.json").read_text())
    source = {table["name"] for table in generated["sourceAlignedTables"]}
    assert {
        "sa_amc_segment_buyer_profile_inmarket",
        "sa_amc_segment_buyer_profile_lifestyle",
        "sa_amc_segment_buyer_profile_dsp_exposed",
    } <= source
    for layer in ("canonicalCandidateTables", "canonicalCurrentTables"):
        tables = {table["name"]: table for table in generated[layer]}
        fact = tables[FACT]
        assert fact["writtenBy"] == [DSP_EXPOSED, INMARKET, LIFESTYLE]
        assert fact["grain"] == [
            "publication_id",
            "grain_set",
            "marketplace_key",
            "period_end",
            "period_start",
            "profile_path",
            "row_kind",
            "segment_id",
            "segment_namespace",
            "time_grain",
        ]
        # No additive measure, so no residual (as pack 1).
        assert f"{FACT}_withheld" not in tables


# --- the snapshot range: planned from the period, re-derived by the Finalizer --------------------


@pytest.mark.parametrize(
    ("period", "thursday"),
    [
        ((date(2026, 9, 1), date(2026, 9, 30)), date(2026, 9, 3)),
        # October 2026 starts on a Thursday: the range opens on the last day of September.
        ((date(2026, 10, 1), date(2026, 10, 31)), date(2026, 10, 1)),
        ((date(2026, 2, 1), date(2026, 2, 28)), date(2026, 2, 5)),
    ],
)
def test_the_snapshot_range_spans_the_months_first_thursday(
    period: tuple[date, date], thursday: date
) -> None:
    assert first_thursday(period[1]) == thursday
    derived = resolve_period_parameters(pack_by_id(ROOT, INMARKET), period)
    start = date.fromordinal(thursday.toordinal() - 1)
    end = date.fromordinal(thursday.toordinal() + 1)
    assert derived == {
        "snapshot_start": f"{start.isoformat()} 00:00:00",
        "snapshot_end": f"{end.isoformat()} 00:00:00",
    }
    # Control: the standard path derives nothing from its period.
    assert resolve_period_parameters(pack_by_id(ROOT, DSP_EXPOSED), period) == {}


def test_a_snapshot_parameter_needs_a_monthly_pack() -> None:
    pack = pack_by_id(ROOT, INMARKET)
    with pytest.raises(ValueError, match="needs periodPolicy grains \\[month\\]"):
        replace(pack, period_policy=replace(pack.period_policy, grains=(PeriodGrain.WEEK,)))  # type: ignore[arg-type]


# --- the load ------------------------------------------------------------------------------------

TRAILER = "distinct_user_count,suppression_reason,rows_suppressed"
HEADER = (
    "row_kind,segment_id,segment_exposed_shoppers,segment_buyers,segment_buyers_new_to_brand,"
    f"profiled_exposed_total,profiled_buyers_total,marketplace_name,time_window_start,"
    f"time_window_end,{TRAILER}"
)
W = "2026-09-01T00:00:00.000Z,2026-10-01T00:00:00.000Z"


def _csv(*lines: str, header: str = HEADER) -> str:
    return "".join(f"{line}\n" for line in (header, *lines))


@dataclass
class _Loaded:
    outcome: SemanticSidecarOutcome
    rows: tuple[dict[str, object], ...]
    detail: str


def _load(contract_id: str, text: str) -> _Loaded:
    _request, tooling, _order, _attempts = _case()
    results = ingest_semantic_delivery(
        SemanticIngestionRequest(
            delivery=_delivery(contract_id, text),
            contracts=tuple(item for item in CONTRACTS if item.contract_id == contract_id),
            columns=load_registry(ROOT),
            now=NOW,
            operation_id="profile-unit",
            execution_id="profile-execution",
            plan_digest="sha256:profile-plan",
            installed_governance=(pack_by_id(ROOT, contract_id).governance,),
            discard_policies=discard_policies(load_query_packs(ROOT)),
        ),
        tooling,
    )
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    rows = tuple(
        cast("dict[str, object]", dict(row))
        for item in canonical.requests
        if item.destination is CanonicalDestination.CANDIDATE
        and item.publication.partition.scope.dataset.value == FACT
        for row in item.rows
    )
    return _Loaded(results[0].outcome, rows, results[0].detail)


SEGMENT = f"segment,41001,480,42,16,,,AMAZON.COM,{W},2,,false"
TOTAL = f"profiled_total,,,,,2400,120,AMAZON.COM,{W},2,,false"
TOTAL_WITHHELD = (
    f"profiled_total,,,,,2400,120,,{W},1,1 'conversions' (path 4 of 4) users < 2 required,true"
)
SEGMENT_WITHHELD = f"segment,,3,3,1,,,,{W},1,1 'conversions' (path 4 of 4) users < 2 required,true"
"""Withheld per path with three shoppers and three buyers: never 'under 2 users' (plan D19)."""


def _shape(row: dict[str, object]) -> tuple[object, ...]:
    return (
        row["row_kind"],
        row["segment_id"],
        row["cell_state"],
        row["segment_exposed_shoppers"],
        row["segment_buyers"],
        row["profiled_exposed_total"],
        row["profiled_buyers_total"],
    )


def test_a_delivery_publishes_its_segment_rows_and_its_profiled_total_row() -> None:
    loaded = _load(INMARKET, _csv(SEGMENT, TOTAL))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, loaded.detail
    assert sorted((_shape(row) for row in loaded.rows), key=str) == sorted(
        [
            ("segment", "41001", "returned", 480, 42, None, None),
            ("profiled_total", None, "returned", None, None, 2400, 120),
        ],
        key=str,
    )
    for row in loaded.rows:
        assert (
            row["profile_path"],
            row["segment_namespace"],
            row["grain_set"],
            row["time_grain"],
            row["marketplace_key"],
        ) == ("asi_in_market", "amc_asi", "platform", "month", US)


def test_a_withheld_total_is_written_not_returned_and_a_withheld_segment_is_dropped() -> None:
    loaded = _load(
        DSP_EXPOSED,
        _csv(SEGMENT.replace("41001", "41002"), TOTAL_WITHHELD).replace(
            "row_kind,segment_id,", "row_kind,behavior_segment_id,"
        ),
    )
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, loaded.detail
    shapes = sorted((_shape(row) for row in loaded.rows), key=str)
    assert ("profiled_total", None, "not_returned", None, None, None, None) in shapes
    # Control: the total row that came back is returned with its bases.
    whole = _load(
        DSP_EXPOSED,
        _csv(SEGMENT, TOTAL).replace("row_kind,segment_id,", "row_kind,behavior_segment_id,"),
    )
    assert ("profiled_total", None, "returned", None, None, 2400, 120) in [
        _shape(row) for row in whole.rows
    ]
    # A segment withheld per path with three buyers is dropped, never written as a figure.
    dropped = _load(INMARKET, _csv(SEGMENT, SEGMENT_WITHHELD, TOTAL))
    assert dropped.outcome is SemanticSidecarOutcome.PUBLISHED, dropped.detail
    assert [row["segment_id"] for row in dropped.rows if row["row_kind"] == "segment"] == ["41001"]


# --- planner and catalog -------------------------------------------------------------------------


def _filters(path: str = "dsp_exposed_only", **extra: str) -> dict[str, str]:
    return {
        "marketplace_key": US,
        "grain_set": "platform",
        "time_grain": "month",
        "profile_path": path,
        "segment_namespace": "amc_dsp_behavior" if path == "dsp_exposed_only" else "amc_asi",
        **extra,
    }


def _roster(path: str = "dsp_exposed_only", *ids: str, first: bool = False) -> Roster:
    return Roster(
        RosterKind.SEGMENT,
        SEPTEMBER,
        US,
        "amazon_ads",
        tuple(RosterEntry(item, f"Segment label {item}") for item in ids),
        filters=(("profile_path", path),),
        first_period=first,
    )


def test_segment_figures_plan_per_segment_and_a_sum_over_segments_is_refused() -> None:
    shape = FactShape.for_fact(FACT, CONTRACTS)
    filters = _filters(row_kind="segment")
    per_segment = plan(
        CATALOG,
        PlanRequest("segment_buyers", ("audience_segment",), SEPTEMBER, filters, roster=_roster()),
        shape,
    )
    assert per_segment.group_by == ("segment_id",)
    for metric in ("segment_buyers", "segment_exposed_shoppers", "segment_buyers_new_to_brand"):
        with pytest.raises(DomainError) as refused:
            plan(CATALOG, PlanRequest(metric, ("marketplace",), SEPTEMBER, filters), shape)
        assert refused.value.code == GRAIN_NOT_SUPPORTED_CODE
        assert "would sum across segment_id" in refused.value.description
    # A segment figure is graded against its path's roster, and refused without it.
    with pytest.raises(InvariantError):
        plan(
            CATALOG, PlanRequest("segment_buyers", ("audience_segment",), SEPTEMBER, filters), shape
        )


def test_a_profiled_total_is_read_from_its_total_row_and_never_split_by_segment() -> None:
    shape = FactShape.for_fact(FACT, CONTRACTS)
    total = plan(
        CATALOG,
        PlanRequest(
            "profiled_buyers_total",
            ("marketplace",),
            SEPTEMBER,
            _filters(row_kind="profiled_total"),
        ),
        shape,
    )
    assert (total.group_by, total.roster) == (("marketplace_key",), None)
    # Control: on segment rows the same figure would sum across segments, so it's refused.
    with pytest.raises(DomainError) as refused:
        plan(
            CATALOG,
            PlanRequest(
                "profiled_buyers_total", ("marketplace",), SEPTEMBER, _filters(row_kind="segment")
            ),
            shape,
        )
    assert refused.value.code == GRAIN_NOT_SUPPORTED_CODE


def test_a_c2_figure_without_one_profile_path_is_refused_naming_each_path() -> None:
    shape = FactShape.for_fact(FACT, CONTRACTS)
    unpinned = {
        key: value for key, value in _filters(row_kind="segment").items() if key != "profile_path"
    }
    with pytest.raises(DomainError) as refused:
        plan(
            CATALOG,
            PlanRequest(
                "segment_buyers", ("audience_segment",), SEPTEMBER, unpinned, roster=_roster()
            ),
            shape,
        )
    assert refused.value.code == REQUIRES_OPTION_CODE
    assert "reads one profile path, one of (asi_in_market, asi_lifestyle, dsp_exposed_only)" in (
        refused.value.description
    )
    assert "never compared or added as one series" in refused.value.description


def test_a_share_or_index_is_never_divided_inside_one_query() -> None:
    shape = FactShape.for_fact(FACT, CONTRACTS)
    for metric in ("segment_buyer_share", "segment_exposed_share", "segment_index"):
        with pytest.raises(DomainError) as refused:
            plan(CATALOG, PlanRequest(metric, ("audience_segment",), SEPTEMBER, _filters()), shape)
        assert refused.value.code == NOT_COMPUTABLE_CODE
        assert "the measurement tool pairs them per segment" in refused.value.description
    # The C2 basis label is the governed buyer metric's.
    assert C2_BUYER_LABEL in CATALOG.metric("segment_buyers").label  # type: ignore[union-attr]


# --- the shared rule -----------------------------------------------------------------------------


def _delivery_of(
    *segments: tuple[str, int, int],
    totals: tuple[int, int] | None = (2400, 120),
    path: str = "dsp_exposed_only",
) -> ProfileDelivery:
    return ProfileDelivery(
        path,
        DSP if path == "dsp_exposed_only" else ASI,
        tuple(
            ProfileSegment(key, exposed, buyers, buyers // 3) for key, exposed, buyers in segments
        ),
        None if totals is None else totals[0],
        None if totals is None else totals[1],
    )


def test_shares_and_the_index_stay_inside_one_delivery_and_a_missing_total_is_not_available() -> (
    None
):
    delivery = _delivery_of(("41001", 720, 48), ("41002", 288, 14))
    figures = segment_figures(delivery, "41001")
    assert (figures.buyer_share, figures.exposed_share, figures.index, figures.reason) == (
        Decimal("0.4"),
        Decimal("0.3"),
        Decimal("0.4") / Decimal("0.3"),
        None,
    )
    missing = _delivery_of(("41001", 720, 48), ("41002", 288, 14), totals=None)
    for key in ("41001", "41002"):
        assert segment_figures(missing, key) == (
            segment_figures(missing, key).__class__(None, None, None, TOTAL_NOT_AVAILABLE_TEXT)
        )
    with pytest.raises(ValueError, match="isn't a returned row"):
        segment_figures(delivery, "41003")


# --- chat: one share per segment, the same rule --------------------------------------------------


@dataclass
class _ByMetric:
    """Rows per planned measure: segment rows by segment, a total as one marketplace row."""

    rows: dict[str, tuple[QueryRow, ...]]
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        return QueryResult(
            rows=self.rows[request.plan.logical.measures[0]],
            bytes_scanned=1,
            truncated=False,
            suppression=Suppression(rows_suppressed=False),
        )


def _segment_rows(values: dict[str, int]) -> tuple[QueryRow, ...]:
    return tuple({"segment_id": key, measure_alias(0): value} for key, value in values.items())


def _total_rows(value: int | None) -> tuple[QueryRow, ...]:
    return () if value is None else ({"marketplace_key": US, measure_alias(0): value},)


def _chat_rows(totals: tuple[int, int] | None = (2400, 120)) -> dict[str, tuple[QueryRow, ...]]:
    exposed, buyers = (None, None) if totals is None else totals
    return {
        "segment_exposed_shoppers": _segment_rows({"41001": 720, "41002": 288}),
        "segment_buyers": _segment_rows({"41001": 48, "41002": 14}),
        "profiled_exposed_total": _total_rows(exposed),
        "profiled_buyers_total": _total_rows(buyers),
    }


@dataclass
class _Served:
    months: tuple[CalendarPeriod, ...] = (MONTH,)

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        published = {contract.contract_id for contract in request.contracts}
        return ServedPeriods(
            frozenset((item, month) for item in published for month in self.months),
            dict.fromkeys(published, 2),
        )


@dataclass
class _Rosters:
    by_path: dict[str, Roster] = field(default_factory=dict[str, Roster])
    seen: list[RosterRequest] = field(default_factory=list[RosterRequest])

    def roster(self, request: RosterRequest) -> Roster:
        self.seen.append(request)
        key = dict(request.filters).get("profile_path") or dict(request.filters).get(
            "campaign_key", ""
        )
        found = self.by_path.get(key) or Roster(
            RosterKind.SEGMENT,
            request.period,
            request.marketplace_key,
            request.platform_key,
            (),
            filters=request.filters,
            first_period=True,
        )
        return replace(found, period=request.period, filters=request.filters)


DSP_LABELS = {
    "41001": SegmentLabel(DSP, "41001", "Demo - Age 25-34", SegmentProvider.AMAZON),
    "41002": SegmentLabel(
        DSP, "41002", "ExperianDemographicsGenderFemale_LiveRamp", SegmentProvider.THIRD_PARTY
    ),
}


def _ask(
    metric: str, grains: tuple[str, ...] = ("audience_segment",), **filters: str
) -> MeasurementToolRequest:
    return MeasurementToolRequest(
        tool=MeasurementTool.METRIC_BY_GRAIN,
        catalog=CATALOG,
        contracts=CONTRACTS,
        metric_name=metric,
        grains=grains,
        window=SEPTEMBER,
        computed_at=NOW,
        correlation_id="corr-c2",
        filters={"marketplace_key": US, **filters},
    )


def _chat(
    request: MeasurementToolRequest, rows: dict[str, tuple[QueryRow, ...]]
) -> tuple[object, _ByMetric, _Rosters]:
    queries = _ByMetric(rows)
    rosters = _Rosters({"dsp_exposed_only": _roster("dsp_exposed_only", "41001", "41002")})
    response = handle(
        request,
        MeasurementTooling(
            queries=queries,  # type: ignore[arg-type]
            publications=StubPublications(),
            periods=_Served(),
            rosters=rosters,
            labels=DimensionLabelResolver(_LabelCatalog({}), SCOPE, segments=_Labels(DSP_LABELS)),
        ),
    )
    return response, queries, rosters


def _values(response: object) -> list[dict[str, object]]:
    return list(cast("tuple[dict[str, object], ...]", response.data.values))  # type: ignore[attr-defined]


def test_chat_answers_a_segment_share_and_index_with_the_bakes_rule() -> None:
    response, queries, rosters = _chat(
        _ask("segment_buyer_share", profile_path="dsp_exposed_only"), _chat_rows()
    )
    values = {row["segment_id"]: row["value"] for row in _values(response)}
    # Released under each segment's governed name and provider, never its bare ID.
    assert values == {
        "Demo - Age 25-34 · Amazon": Decimal("0.4"),
        "ExperianDemographicsGenderFemale_LiveRamp · Third party": Decimal(14) / Decimal(120),
    }
    # Four operands, one selection: two segment reads graded on the path's roster, two totals.
    planned = [
        (item.plan.logical.metric.name, item.plan.logical.filters["row_kind"])
        for item in queries.seen
    ]
    assert planned == [
        ("segment_exposed_shoppers", "segment"),
        ("segment_buyers", "segment"),
        ("profiled_exposed_total", "profiled_total"),
        ("profiled_buyers_total", "profiled_total"),
    ]
    assert {item.plan.logical.filters["profile_path"] for item in queries.seen} == {
        "dsp_exposed_only"
    }
    assert [(item.fact, item.filters) for item in rosters.seen] == [
        (FACT, (("profile_path", "dsp_exposed_only"),))
    ] * 2
    index, *_ = _chat(_ask("segment_index", profile_path="dsp_exposed_only"), _chat_rows())
    indexes = {row["segment_id"]: row["value"] for row in _values(index)}
    expected = segment_figures(_delivery_of(("41001", 720, 48), ("41002", 288, 14)), "41001")
    assert indexes["Demo - Age 25-34 · Amazon"] == expected.index


def test_chat_answers_every_share_not_available_when_the_delivery_total_is_missing() -> None:
    response, *_ = _chat(
        _ask("segment_exposed_share", profile_path="dsp_exposed_only"), _chat_rows(None)
    )
    values = _values(response)
    assert {row["segment_id"] for row in values} == {
        "Demo - Age 25-34 · Amazon",
        "ExperianDemographicsGenderFemale_LiveRamp · Third party",
    }
    assert all(row["value"] is None for row in values)
    assert {row[WITHHELD_REASON_FIELD] for row in values} == {TOTAL_NOT_AVAILABLE_TEXT}
    # Control: with the total back, every segment carries its share and no reason.
    whole, *_ = _chat(_ask("segment_exposed_share", profile_path="dsp_exposed_only"), _chat_rows())
    assert {row[WITHHELD_REASON_FIELD] for row in _values(whole)} == {None}


def test_chat_refuses_a_sum_of_shares_and_a_share_without_a_profile_path() -> None:
    with pytest.raises(DomainError) as summed:
        _chat(
            _ask("segment_buyer_share", ("marketplace",), profile_path="dsp_exposed_only"),
            _chat_rows(),
        )
    assert summed.value.code == GRAIN_NOT_SUPPORTED_CODE
    assert "shares never add up" in summed.value.description
    with pytest.raises(DomainError) as unpinned:
        _chat(_ask("segment_buyer_share"), _chat_rows())
    assert unpinned.value.code == REQUIRES_OPTION_CODE
    assert "Pin profile_path." in unpinned.value.description
    # Positive control: per segment on one path, it answers.
    response, *_ = _chat(_ask("segment_buyer_share", profile_path="dsp_exposed_only"), _chat_rows())
    assert len(_values(response)) == 2


def test_chat_segment_buyers_name_providers_and_list_the_paths_roster_gap() -> None:
    queries = _ByMetric({"segment_buyers": _segment_rows({"41001": 48})})
    rosters = _Rosters({"dsp_exposed_only": _roster("dsp_exposed_only", "41001", "41002")})
    response = handle(
        _ask("segment_buyers", profile_path="dsp_exposed_only"),
        MeasurementTooling(
            queries=queries,  # type: ignore[arg-type]
            publications=StubPublications(),
            periods=_Served(),
            rosters=rosters,
            labels=DimensionLabelResolver(_LabelCatalog({}), SCOPE, segments=_Labels(DSP_LABELS)),
        ),
    )
    narrative = " ".join(response.narrative)
    assert "Segment label 41002 is not available" in narrative
    released = narrative + json.dumps(
        [list(row.values()) for row in _values(response)], default=str, ensure_ascii=False
    )
    assert "Demo - Age 25-34 · Amazon" in released
    # A path's segments are listed against its own previous month; a missing path is refused.
    with pytest.raises(ValidationError) as refused:
        handle(
            _ask("segment_buyers"),
            MeasurementTooling(
                queries=queries,  # type: ignore[arg-type]
                publications=StubPublications(),
                periods=_Served(),
                rosters=rosters,
            ),
        )
    assert refused.value.code == ROSTER_REFUSED
    assert refused.value.description.endswith("Name the profile path.")


def test_chat_segment_share_lists_the_paths_roster_gap() -> None:
    rows = {
        **_chat_rows(),
        "segment_exposed_shoppers": _segment_rows({"41001": 720}),
        "segment_buyers": _segment_rows({"41001": 48}),
    }
    response, *_ = _chat(_ask("segment_buyer_share", profile_path="dsp_exposed_only"), rows)
    assert "Segment label 41002 is not available" in " ".join(response.narrative)  # type: ignore[attr-defined]
    assert {row["segment_id"]: row["value"] for row in _values(response)} == {
        "Demo - Age 25-34 · Amazon": Decimal("0.4")
    }
    # Control: with every rostered segment returned, no gap is named.
    whole, *_ = _chat(_ask("segment_buyer_share", profile_path="dsp_exposed_only"), _chat_rows())
    assert "is not available" not in " ".join(whole.narrative)  # type: ignore[attr-defined]


def test_chat_answers_a_segment_one_operand_lacks_not_available_never_a_zero_share() -> None:
    rows = {**_chat_rows(), "segment_buyers": _segment_rows({"41001": 48})}
    response, *_ = _chat(_ask("segment_buyer_share", profile_path="dsp_exposed_only"), rows)
    values = {row["segment_id"]: row for row in _values(response)}
    partial = values["ExperianDemographicsGenderFemale_LiveRamp · Third party"]
    assert (partial["value"], partial[WITHHELD_REASON_FIELD]) == (None, SEGMENT_PARTIAL_TEXT)
    # Control: the segment both operands carry keeps its share.
    whole = values["Demo - Age 25-34 · Amazon"]
    assert (whole["value"], whole[WITHHELD_REASON_FIELD]) == (Decimal("0.4"), None)


# --- the C2 roster reader ------------------------------------------------------------------------


@dataclass
class _History:
    months: tuple[CalendarPeriod, ...]

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        ids = {contract.contract_id for contract in request.contracts}
        return ServedPeriods(
            frozenset((item, month) for item in ids for month in self.months), dict.fromkeys(ids, 2)
        )


def _reader(
    months: tuple[CalendarPeriod, ...], rows: tuple[QueryRow, ...] = ()
) -> tuple[SegmentRosterReader, _ByMetric]:
    queries = _ByMetric({"segment_exposed_shoppers": rows})
    return (
        SegmentRosterReader(
            catalog=CATALOG,
            contracts=CONTRACTS,
            publications=StubPublications(),
            periods=_History(months),
            queries=queries,  # type: ignore[arg-type]
            labels=_Labels(DSP_LABELS),
            clock=lambda: NOW,
        ),
        queries,
    )


def _profile_request() -> RosterRequest:
    return RosterRequest(
        RosterKind.SEGMENT,
        SEPTEMBER,
        US,
        "amazon_ads",
        (("profile_path", "dsp_exposed_only"),),
        fact=FACT,
    )


def test_the_profile_roster_is_the_paths_segments_the_month_before() -> None:
    august = CalendarPeriod(AUGUST.start, AUGUST.end, TimeGrain.MONTH)
    reader, queries = _reader((august, MONTH), _segment_rows({"41001": 700, "41002": 280}))
    roster = reader.roster(_profile_request())
    assert roster.keys == ("41001", "41002")
    assert [entry.label for entry in roster.entries] == [
        "Demo - Age 25-34 · Amazon",
        "ExperianDemographicsGenderFemale_LiveRamp · Third party",
    ]
    (read,) = queries.seen
    assert read.plan.logical.window == AUGUST
    assert dict(read.plan.logical.filters)["profile_path"] == "dsp_exposed_only"
    assert dict(read.plan.logical.filters)["row_kind"] == "segment"
    # First month: no earlier month published, so the roster is empty and says so.
    first, _ = _reader((MONTH,))
    assert first.roster(_profile_request()).first_period
    # A gap: an older month is published but the month before isn't, so it's refused.
    july = CalendarPeriod(date(2026, 7, 1), date(2026, 7, 31), TimeGrain.MONTH)
    gap, _ = _reader((july, MONTH))
    with pytest.raises(DomainError) as refused:
        gap.roster(_profile_request())
    assert refused.value.code == ROSTER_REFUSED


# --- paid states, views and the insight ----------------------------------------------------------


def _slot(
    view: str, state: DeliveryState, labels: dict[str, SegmentLabel] | None = None
) -> InsightSlot:
    findings = () if state.delivery is None else over_index_findings(state.delivery, POLICY)  # type: ignore[arg-type]
    return segment_over_index_slot(
        OverIndexSlotRequest(
            view,
            state.path,
            SEPTEMBER.start,
            state.delivery,
            findings,
            labels or {},
            POLICY,  # type: ignore[arg-type]
            "rev-1",
            "quiet",
        )
    )


def _view(  # noqa: PLR0913, PLR0917 -- one view: its id, state, deliveries and labels
    view: str,
    paid: PaidState,
    deliveries: tuple[DeliveryState, ...],
    labels: dict[str, SegmentLabel] | None = None,
) -> ProfileView:
    insights = tuple(_slot(view, state, labels) for state in deliveries)
    return build_view(ViewRequest(view, paid, deliveries, {}, labels or {}, POLICY, insights))  # type: ignore[arg-type]


def _observed(observed: bool) -> PaidFeatureState:
    return PaidFeatureState(observed, observed, date(2026, 10, 2), SEPTEMBER if observed else None)


def test_paid_profile_unobserved_reads_the_paid_text_and_the_page_defaults_to_dsp() -> None:
    standard = _view(
        DSP_VIEW,
        PaidState.STANDARD,
        (DeliveryState("dsp_exposed_only", _delivery_of(("41001", 720, 48))),),
    )
    unobserved = _view(
        PAID_VIEW,
        PaidState.NOT_OBSERVED,
        (
            DeliveryState("asi_in_market", None, _observed(False)),
            DeliveryState("asi_lifestyle", None, _observed(False)),
        ),
    )
    market = AudienceMarket(US, unobserved, standard, CampaignAudiences(False, ()))
    assert market.default_view == DSP_VIEW
    body = audiences_artifact_payload(AudienceProfileArtifact(MONTH, (market,), POLICY.buyers_min))
    paid, dsp = body["markets"][0]["views"]  # type: ignore[index]
    assert (paid["paid_state"], paid["callout"]) == ("not_observed", ASI_NEEDS_PAID_FEATURE_TEXT)
    assert (dsp["paid_state"], dsp["callout"]) == ("standard", None)
    assert body["markets"][0]["default_view"] == DSP_VIEW  # type: ignore[index]
    # Observed and delivered: the paid view is the default.
    asi = _delivery_of(("41001", 480, 42), path="asi_in_market")
    observed = _view(
        PAID_VIEW,
        PaidState.OBSERVED,
        (
            DeliveryState("asi_in_market", asi, _observed(True)),
            DeliveryState("asi_lifestyle", None, _observed(True)),
        ),
    )
    assert (
        AudienceMarket(US, observed, standard, CampaignAudiences(False, ())).default_view
        == PAID_VIEW
    )


def test_partial_enrolment_names_asi_segments_by_id_and_still_shows_the_index() -> None:
    asi = _delivery_of(("41009", 480, 42), path="asi_in_market")
    view = _view(
        PAID_VIEW,
        PaidState.OBSERVED,
        (
            DeliveryState("asi_in_market", asi, _observed(True)),
            DeliveryState("asi_lifestyle", None, _observed(True)),
        ),
    )
    standard = _view(DSP_VIEW, PaidState.STANDARD, (DeliveryState("dsp_exposed_only", None),))
    body = audiences_artifact_payload(
        AudienceProfileArtifact(
            MONTH,
            (AudienceMarket(US, view, standard, CampaignAudiences(False, ())),),
            POLICY.buyers_min,
        )
    )
    (row,) = body["markets"][0]["views"][0]["rows"]  # type: ignore[index]
    # No governed name and no provider classification: its ID, and no provider label (LLD).
    assert (row["display_name"], row["provider_label"]) == (
        "Segment 41009 (name not available yet)",
        None,
    )
    assert row["segment_index"] == "1.75"  # 42/120 over 480/2400
    assert row["state"] == "value"
    # Control: a named ASI segment reads its name, category and provider.
    named = {
        "41009": SegmentLabel(
            ASI,
            "41009",
            "Synthetic ASI in-market: running shoes",
            SegmentProvider.AMAZON,
            "In-market",
            "In-market > Sports > Running shoes",
        )
    }
    labelled = _view(PAID_VIEW, PaidState.OBSERVED, view.deliveries, named)
    assert labelled.rows[0].label == named["41009"]


def test_an_unclassified_segment_carries_no_provider_label_and_a_classified_one_its_own() -> None:
    labels = {
        "41001": SegmentLabel(DSP, "41001", "Demo - Age 25-34", SegmentProvider.AMAZON),
        "41002": SegmentLabel(DSP, "41002", "Synthetic weekend gardeners", None),
        "41003": SegmentLabel(DSP, "41003", "Synthetic data partner", SegmentProvider.THIRD_PARTY),
    }
    delivery = _delivery_of(("41001", 720, 48), ("41002", 288, 14), ("41003", 240, 12))
    view = _view(
        DSP_VIEW, PaidState.STANDARD, (DeliveryState("dsp_exposed_only", delivery),), labels
    )
    paid = _view(
        PAID_VIEW,
        PaidState.NOT_OBSERVED,
        (DeliveryState("asi_in_market", None), DeliveryState("asi_lifestyle", None)),
    )
    body = audiences_artifact_payload(
        AudienceProfileArtifact(
            MONTH, (AudienceMarket(US, paid, view, CampaignAudiences(False, ())),), 30
        )
    )
    rows = cast("Any", body["markets"])[0]["views"][1]["rows"]
    assert {row["segment_id"]: (row["display_name"], row["provider_label"]) for row in rows} == {
        "41001": ("Demo - Age 25-34 · Amazon", "Amazon"),
        "41002": ("Synthetic weekend gardeners", None),
        "41003": ("Synthetic data partner · Third party", "Third party"),
    }
    assert "Provider not recorded" not in json.dumps(body)


def test_a_view_never_lists_the_other_namespaces_segments() -> None:
    dsp_delivery = _delivery_of(("41001", 720, 48))
    with pytest.raises(ValueError, match="lists amc_asi segments only"):
        _view(
            PAID_VIEW,
            PaidState.OBSERVED,
            (
                DeliveryState("asi_in_market", replace(dsp_delivery, path="asi_in_market")),
                DeliveryState("asi_lifestyle", None),
            ),
        )
    # Control: each path renders on its own.
    assert _view(
        DSP_VIEW, PaidState.STANDARD, (DeliveryState("dsp_exposed_only", dsp_delivery),)
    ).rows


def test_bonferroni_values_match_the_design() -> None:
    assert bonferroni_z(Decimal("0.025"), 100).quantize(Decimal("0.01")) == Decimal("3.48")
    assert bonferroni_z(Decimal("0.025"), 200).quantize(Decimal("0.01")) == Decimal("3.66")
    assert bonferroni_z(Decimal("0.025"), 1).quantize(Decimal("0.01")) == Decimal("1.96")


def test_two_hundred_segments_with_equal_shares_name_none_and_a_clear_over_index_is_named() -> None:
    equal = _delivery_of(
        *((str(50000 + index), 2400, 1200) for index in range(200)), totals=(24000, 12000)
    )
    assert over_index_findings(equal, POLICY) == ()
    clear = _delivery_of(("41001", 4800, 4200), ("41002", 2400, 1200), totals=(24000, 12000))
    (named,) = over_index_findings(clear, POLICY)
    assert named.segment_id == "41001"
    # B_min: a segment under the minimum buyers is never named, however wide its gap.
    few = _delivery_of(("41001", 10, 29), totals=(24000, 100))
    assert over_index_findings(few, POLICY) == ()
    # No total row: nothing is named.
    assert (
        over_index_findings(replace(clear, profiled_exposed=None, profiled_buyers=None), POLICY)
        == ()
    )


def test_the_insight_contract_versions_b_min_and_rejects_summed_shares() -> None:
    assert (POLICY.buyers_min, POLICY.family_alpha) == (30, Decimal("0.025"))
    summed = next(
        case
        for case in INSIGHT.calibration_cases
        if case.kind is InsightCalibrationKind.MISCOMPUTED_DERIVATION
    )
    assert "Reject summing segment shares" in summed.expected_outcome
    asi = _delivery_of(("41001", 4800, 4200), totals=(24000, 12000), path="asi_in_market")
    slot = _slot(PAID_VIEW, DeliveryState("asi_in_market", asi))
    assert slot.state is InsightSlotState.FILLED
    assert slot.text is not None
    assert slot.text.startswith("Amazon audience segments (paid) · In-market · September 2026")
    assert slot.text.endswith("Only segments with at least 30 buyers are named.")
    # A whole delivery naming nothing is evaluated quiet; a missing delivery or total is
    # ineligible with its reason, never "nothing over-indexes".
    quiet = _slot(DSP_VIEW, DeliveryState("dsp_exposed_only", _delivery_of(("41001", 720, 9))))
    absent = _slot(DSP_VIEW, DeliveryState("dsp_exposed_only", None))
    no_total = _slot(
        DSP_VIEW, DeliveryState("dsp_exposed_only", _delivery_of(("41001", 720, 48), totals=None))
    )
    assert [
        (item.state, item.absence_class, item.absence_reason) for item in (quiet, absent, no_total)
    ] == [
        (InsightSlotState.SILENT, InsightAbsenceClass.EVALUATED_QUIET, "quiet"),
        (InsightSlotState.SILENT, InsightAbsenceClass.INELIGIBLE, NO_DELIVERY_TEXT),
        (InsightSlotState.SILENT, InsightAbsenceClass.INELIGIBLE, TOTAL_NOT_AVAILABLE_TEXT),
    ]
    # A card reads its own path only: a finding from another delivery is refused.
    with pytest.raises(ValueError, match="its own path's segments only"):
        OverIndexSlotRequest(
            PAID_VIEW,
            "asi_lifestyle",
            SEPTEMBER.start,
            None,
            over_index_findings(asi, POLICY),  # type: ignore[arg-type]
            {},
            POLICY,  # type: ignore[arg-type]
            "rev-1",
            "quiet",
        )


def test_a_paid_view_with_both_deliveries_ranks_and_names_each_inside_its_own_base() -> None:
    # In-market: three clear over-indexes on a 24,000 / 12,000 base. Lifestyle: one higher
    # index on its own 10,000 / 2,000 base, which a merged top three would have dropped.
    in_market = _delivery_of(
        ("41001", 4800, 4200),
        ("41002", 4000, 3200),
        ("41003", 3600, 2600),
        totals=(24000, 12000),
        path="asi_in_market",
    )
    lifestyle = _delivery_of(("42001", 800, 600), totals=(10000, 2000), path="asi_lifestyle")
    states = (
        DeliveryState("asi_in_market", in_market, _observed(True)),
        DeliveryState("asi_lifestyle", lifestyle, _observed(True)),
    )
    view = _view(PAID_VIEW, PaidState.OBSERVED, states)
    # Rows ranked inside each delivery, the deliveries in the view's order.
    assert [(row.path, row.segment.segment_id) for row in view.rows] == [
        ("asi_in_market", "41001"),
        ("asi_in_market", "41002"),
        ("asi_in_market", "41003"),
        ("asi_lifestyle", "42001"),
    ]
    standard = _view(DSP_VIEW, PaidState.STANDARD, (DeliveryState("dsp_exposed_only", None),))
    body = audiences_artifact_payload(
        AudienceProfileArtifact(
            MONTH, (AudienceMarket(US, view, standard, CampaignAudiences(False, ())),), 30
        )
    )
    paid = cast("Any", body["markets"])[0]["views"][0]
    # Each delivery carries its own base and its own card, which names its path.
    assert [
        (item["profile_path"], item["profiled_exposed_total"], item["profiled_buyers_total"])
        for item in paid["deliveries"]
    ] == [("asi_in_market", 24000, 12000), ("asi_lifestyle", 10000, 2000)]
    texts = [item["insights"][0]["text"] for item in paid["deliveries"]]
    assert texts[0].startswith("Amazon audience segments (paid) · In-market · September 2026")
    assert "42001" not in texts[0]
    assert texts[1].startswith("Amazon audience segments (paid) · Lifestyle · September 2026")
    assert "Segment 42001 (name not available yet): 30.0% of buyers against 8.0%" in texts[1]
    assert "insights" not in paid
    # Each row's shares are inside its own delivery: 600/2000 and 800/10000.
    last = paid["rows"][-1]
    assert (last["segment_buyer_share"]["text"], last["segment_exposed_share"]["text"]) == (
        "30.0%",
        "8.0%",
    )


# --- the materializer ----------------------------------------------------------------------------


def test_the_materializer_is_registered_and_opens_with_the_committed_page_entry() -> None:
    schemas = load_v2_page_schemas(ROOT)
    assert "audiences" in {page.page_id for page in schemas.pages}
    assert audiences_policy_declared(schemas)


def test_only_the_committed_audiences_v1_declaration_opens_the_source() -> None:
    schemas = load_v2_page_schemas(ROOT)
    page = next(item for item in schemas.pages if item.page_id == "audiences")
    assert page.navigation is not None
    assert (
        page.navigation.hub_id,
        page.navigation.tab_label,
        page.navigation.order,
    ) == ("reach", "Audiences", 2)
    assert page.assistant is not None
    assert page.assistant.page == (
        "Which DSP behaviour segments did {window}'s buyers over-index in?",
        "Which DSP campaigns reached which segments in {window}?",
    )
    assert page.assistant.detail == ()
    (artifact,) = page.artifacts
    others = tuple(item for item in schemas.pages if item.page_id != "audiences")

    def opens(**changes: object) -> bool:
        changed = replace(page, **changes)  # type: ignore[arg-type]
        return audiences_policy_declared(replace(schemas, pages=(*others, changed)))

    # Positive control: the hand-off entry opens the source, and it is the LLD's declaration.
    assert opens()
    assert (page.time_policy, page.sections) == (AUDIENCES_TEMPORAL_POLICY, AUDIENCES_SECTIONS)
    policy = page.temporal_policy

    def sections(section_id: str, **changes: object) -> tuple[PageSection, ...]:
        return tuple(
            replace(item, **changes) if item.section_id == section_id else item  # type: ignore[arg-type]
            for item in page.sections
        )

    def without(section_id: str) -> tuple[PageSection, ...]:
        return tuple(item for item in page.sections if item.section_id != section_id)

    refused: dict[str, dict[str, object]] = {
        "wrong type": {"artifacts": (replace(artifact, artifact_type="RetentionCohortArtifact"),)},
        "wrong version": {"artifacts": (replace(artifact, version=2),)},
        "an outcome missing": {"artifacts": (replace(artifact, outcomes=("complete", "empty")),)},
        "two artifacts": {"artifacts": (artifact, replace(artifact, artifact_id="other"))},
        "parent-child mode": {"mode": PresentationPageMode.SELECTABLE_PARENT_CHILD},
        # The schema ties boundary_required to the boundary section, so both go together.
        "no boundary": {"boundary_required": False, "sections": without("boundary")},
        "another candidate preset": {
            "time_policy": replace(
                policy,
                candidate_presets=(
                    TemporalPresetId.LAST_COMPLETE_MONTH,
                    TemporalPresetId.LAST_COMPLETE_QUARTER,
                ),
            )
        },
        # The schema refuses Custom on a declared_period page; on a period page, the gate.
        "custom windows enabled": {
            "time_policy": replace(policy, custom_enabled=True, display_grains=(PageTime.PERIOD,))
        },
        "another comparison": {
            "time_policy": replace(
                policy, comparison_policy=TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR
            )
        },
        "period display grain": {"time_policy": replace(policy, display_grains=(PageTime.PERIOD,))},
        **{
            f"{name} missing": {"sections": without(name)}
            for name in (
                "workspace-scope",
                "profile-path",
                "segment-ranking",
                "audience-insights",
                "campaign-audiences",
                "evidence-trace",
            )
        },
        "ranking not the anchor": {"sections": sections("segment-ranking", period_anchor=False)},
        "campaigns anchoring": {"sections": sections("campaign-audiences", period_anchor=True)},
        "ranking binds no period": {
            "sections": sections("segment-ranking", declared_periods=(), period_anchor=None)
        },
        "insights repeated": {
            "sections": sections("audience-insights", cardinality=Cardinality.ONE_OR_MORE)
        },
        "an extra section": {
            "sections": (*page.sections, PageSection("also-changed", Cardinality.EXACTLY_ONE))
        },
    }
    assert {name: opens(**changes) for name, changes in refused.items()} == dict.fromkeys(
        refused, False
    )


@dataclass
class _Bake:
    """The stores one bake reads: published months, rows per plan, rosters and labels."""

    published: frozenset[tuple[str, DateRange]]
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])
    campaigns: tuple[str, ...] = ("camp-1",)
    """The DSP campaigns pack 8 returned; campaign `camp-{n}` reached n x 30,000 impressions."""
    served_seen: list[ServedPeriodsRequest] = field(default_factory=list[ServedPeriodsRequest])

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        self.served_seen.append(request)
        ids = {contract.contract_id for contract in request.contracts}
        return ServedPeriods(
            frozenset(
                (contract, CalendarPeriod(window.start, window.end, TimeGrain.MONTH))
                for contract, window in self.published
                if contract in ids
            ),
            dict.fromkeys(ids, 2),
        )

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        logical = request.plan.logical
        measure = logical.measures[0]
        path = logical.filters.get("profile_path")
        segment = {
            ("dsp_exposed_only", "segment_exposed_shoppers"): {"41001": 720, "41002": 288},
            ("dsp_exposed_only", "segment_buyers"): {"41001": 48, "41002": 14},
            ("dsp_exposed_only", "segment_buyers_new_to_brand"): {"41001": 19, "41002": 5},
            ("asi_in_market", "segment_exposed_shoppers"): {"41001": 480},
            ("asi_in_market", "segment_buyers"): {"41001": 42},
            ("asi_in_market", "segment_buyers_new_to_brand"): {"41001": 16},
        }
        rows: list[QueryRow] = []
        if logical.group_by == ("marketplace_key",):
            value = {"profiled_exposed_total": 2400, "profiled_buyers_total": 120}[measure]
            rows.append({"marketplace_key": US, measure_alias(0): value})
        elif path is not None:
            rows.extend(
                {"marketplace_key": US, "segment_id": key, measure_alias(0): value}
                for key, value in segment[(path, measure)].items()
            )
        else:
            money = "currency_code" in logical.carried_columns
            value = {
                "segment_reach": 3000,
                "impressions": 30000,
                "dsp_spend": Decimal("210.00"),
                "segment_attributed_revenue": Decimal("123.00"),
            }[measure]
            campaign = logical.filters.get("campaign_key")
            if measure == "impressions" and campaign is not None:
                value *= int(str(campaign).removeprefix("camp-"))
            row: QueryRow = {"marketplace_key": US, "segment_id": "41001", measure_alias(0): value}
            if money:
                row = {**row, "currency_code": "USD"}
            if "campaign_key" in logical.group_by:
                rows.extend({**row, "campaign_key": key} for key in self.campaigns)
            else:
                rows.append(row)
        return QueryResult(
            rows=tuple(rows), bytes_scanned=1, truncated=False, suppression=Suppression(False)
        )


CAMPAIGN_NAMES = {("campaign_key", "camp-1"): "Fall prospecting"}
"""The governed catalog's one named campaign; every other campaign reads chat's placeholder."""


def _renderer(  # noqa: PLR0913 -- published months, rosters, fan-out and the campaign bound
    published: frozenset[tuple[str, DateRange]],
    rosters: _Rosters | None = None,
    bound: int = 1,
    *,
    campaign_bound: int = 13,
) -> tuple[AudiencesArtifactRenderer, _Bake]:
    store = _Bake(published)
    stub = StubPublications()
    if rosters is None:
        rosters = _Rosters({"dsp_exposed_only": _roster("dsp_exposed_only", "41001", "41003")})
    return (
        AudiencesArtifactRenderer(
            catalog=CATALOG,
            contracts=CONTRACTS,
            evidence=stub,
            tooling=MeasurementTooling(
                queries=store,  # type: ignore[arg-type]
                publications=stub,  # type: ignore[arg-type]
                periods=store,  # type: ignore[arg-type]
                query_batch_max_concurrency=bound,
            ),
            rosters=rosters,
            segment_labels=_Labels(DSP_LABELS),
            marketplaces=(US,),
            insight_contract=INSIGHT,
            clock=lambda: NOW,
            campaign_labels=governed_campaign_names(
                DimensionLabelResolver(_LabelCatalog(dict(CAMPAIGN_NAMES)), SCOPE)
            ),
            campaign_bound=campaign_bound,
        ),
        store,
    )


def test_a_renderer_refuses_multiple_markets_before_any_query_port_call() -> None:
    renderer, store = _renderer(frozenset({(DSP_EXPOSED, SEPTEMBER)}))
    with pytest.raises(ValueError, match="exactly one governed marketplace"):
        replace(renderer, marketplaces=(US, "amazon_uk"))
    assert (store.seen, store.served_seen) == ([], [])


def test_multi_market_source_composition_returns_its_named_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = PresetArtifactBakeSettings(
        artifact_bucket="fictional-artifacts",
        pointer_table="fictional-pointers",
        semantic_publications_table="fictional-table",
        semantic_v2_bucket="fictional-bucket",
        athena_workgroup="fictional-workgroup",
        semantic_v2_current_database="fictional-database",
        athena_result_reuse_max_age_minutes=1,
        athena_query_batch_max_concurrency=1,
    )

    def two_marketplaces(_root: pathlib.Path) -> int:
        return 2

    monkeypatch.setattr(audiences_source, "audiences_marketplace_count", two_marketplaces)
    closed = retained_audiences_preset_artifact_source(
        settings, cast("Any", _Pointers()), cast("Any", None), _reads()
    )
    assert closed is None  # logged once: audiences_marketplace_cardinality_unavailable


@dataclass
class _Pointers:
    def current(self, page: object, variant: object) -> None:
        del page, variant


def test_an_injected_page_entry_bakes_a_publication_equal_to_the_planned_answers() -> None:
    published = frozenset({(DSP_EXPOSED, SEPTEMBER), (PERFORMANCE, SEPTEMBER)})
    renderer, store = _renderer(published)
    result = renderer.render_audiences(AudiencesRenderInput(MONTH))
    assert result.artifact is not None
    source = RetainedAudiencesPresetArtifactSource(
        compiler=renderer,
        served=lambda: frozenset({(DSP_EXPOSED, MONTH)}),
        pointers=cast("object", _Pointers()),  # type: ignore[arg-type]
        clock=lambda: NOW,
        row_budget=300,
    )
    baker = RegisteredPresetArtifactBaker(
        bakers={
            "campaigns": CampaignPresetArtifactBaker(UnavailableCampaignPresetArtifactSource()),
            "audiences": AudiencesPresetArtifactBaker(source),
        },
        fallback=UnavailablePresetArtifactBaker(),
        roster_pages=("campaigns", "audiences"),
    )
    baked = baker.bake(BakeIntent("audiences", "last_complete_month"))
    assert isinstance(baked, BakePublication)
    body = json.loads(baked.request.body.payload_json)
    assert baked.request.pointer_advance.pointer.resolved_window == SEPTEMBER
    assert (body["type"], body["version"], body["outcome"]) == (
        "AudienceProfileArtifact",
        1,
        "complete",
    )
    (market,) = body["markets"]
    paid, dsp = market["views"]
    assert market["default_view"] == DSP_VIEW
    assert (paid["paid_state"], paid["callout"]) == ("not_observed", ASI_NEEDS_PAID_FEATURE_TEXT)
    # Each unobserved paid card reads the paid callout, never "no profile this month" (ADR-107).
    assert [
        (item["insights"][0]["absence_class"], item["insights"][0]["absence_reason"])
        for item in paid["deliveries"]
    ] == [("ineligible", ASI_NEEDS_PAID_FEATURE_TEXT)] * 2
    first = dsp["rows"][0]
    # 48 of 120 buyers over 720 of 2400 reached shoppers, the shares the chat rule computes.
    assert (
        first["segment_id"],
        first["segment_buyer_share"],
        first["segment_exposed_share"],
        first["segment_index"],
    ) == (
        "41001",
        {"value": "0.4000", "text": "40.0%"},
        {"value": "0.3000", "text": "30.0%"},
        "1.33",
    )
    assert first["provider_label"] == "Amazon"
    # 41002 has 14 buyers, under B_min 30, so it shows its shares but no index.
    second = dsp["rows"][1]
    assert (second["segment_id"], second["state"], second["segment_index"]) == (
        "41002",
        "below_minimum",
        None,
    )
    # The roster's 41003 didn't come back: listed last as not available, never index 0.
    assert [(item["segment_id"], item["state"]) for item in dsp["not_available"]] == [
        ("41003", "not_available")
    ]
    assert dsp["segment_ids_sentence"] == SEGMENT_IDS_SENTENCE
    assert "segment figures never add up" in dsp["caption"]
    assert body["boundary"] == [BOUNDARY_TEXT, SEGMENT_IDS_SENTENCE]
    assert "Amazon's segments" not in json.dumps(body)
    # Campaign audiences: CPM and ROAS inside one campaign and segment row, currencies agreeing.
    (campaign,) = market["campaign_audiences"]["rows"]
    assert (campaign["segment_cpm"], campaign["segment_roas_attributed"]) == (
        {"value": "7.00", "currency": "USD", "text": "7.00 USD"},
        "0.59",
    )
    # Nothing planned for the paid path, which has no delivery.
    assert {item.plan.logical.filters.get("profile_path") for item in store.seen} <= {
        "dsp_exposed_only",
        None,
    }
    assert source.resolve() == AudiencesRenderInput(MONTH)
    other = baker.bake(BakeIntent("audiences", "last_7_days"))
    assert isinstance(other, BakeUnavailable)


def test_a_missing_total_bakes_every_share_of_its_delivery_not_available() -> None:
    renderer, store = _renderer(frozenset({(DSP_EXPOSED, SEPTEMBER)}))
    original = store.run

    def without_totals(request: QueryRequest) -> QueryResult:
        result = original(request)
        if request.plan.logical.group_by == ("marketplace_key",):
            return replace(result, rows=())
        return result

    object.__setattr__(store, "run", without_totals)
    result = renderer.render_audiences(AudiencesRenderInput(MONTH))
    assert result.artifact is not None
    body = audiences_artifact_payload(result.artifact)
    dsp = body["markets"][0]["views"][1]  # type: ignore[index]
    rows = cast("list[dict[str, object]]", dsp["rows"])  # type: ignore[index]
    assert {row["state"] for row in rows} == {"not_available"}
    assert {row["text"] for row in rows} == {TOTAL_NOT_AVAILABLE_TEXT}
    assert dsp["deliveries"][0]["total_text"] == TOTAL_NOT_AVAILABLE_TEXT
    assert body["outcome"] == "insufficient"


def _bake_with(change: Callable[[QueryRequest, QueryResult], QueryResult]) -> Any:
    """Bake the September DSP and campaign deliveries with one answer changed."""
    renderer, store = _renderer(frozenset({(DSP_EXPOSED, SEPTEMBER), (PERFORMANCE, SEPTEMBER)}))
    original = store.run

    def run(request: QueryRequest) -> QueryResult:
        return change(request, original(request))

    object.__setattr__(store, "run", run)
    result = renderer.render_audiences(AudiencesRenderInput(MONTH))
    assert result.artifact is not None
    return cast("Any", audiences_artifact_payload(result.artifact))


def _measure(request: QueryRequest) -> str:
    return request.plan.logical.measures[0]


def test_a_segment_one_figure_lacks_bakes_not_available_and_never_a_zero() -> None:
    def no_buyers_for_41002(request: QueryRequest, result: QueryResult) -> QueryResult:
        if _measure(request) != "segment_buyers":
            return result
        return replace(
            result, rows=tuple(row for row in result.rows if row["segment_id"] != "41002")
        )

    dsp = _bake_with(no_buyers_for_41002)["markets"][0]["views"][1]  # type: ignore[index]
    assert [row["segment_id"] for row in dsp["rows"]] == ["41001"]
    # Listed with its own reason beside the roster's gap, never as 0 buyers or a 0.0% share.
    assert [(item["segment_id"], item["text"]) for item in dsp["not_available"]] == [
        ("41002", SEGMENT_PARTIAL_TEXT),
        ("41003", ROSTER_GAP_TEXT),
    ]
    # Control: with every figure back, 41002 is a row with its shares.
    whole = _bake_with(lambda _request, result: result)["markets"][0]["views"][1]  # type: ignore[index]
    assert [row["segment_id"] for row in whole["rows"]] == ["41001", "41002"]


def _campaign(
    spend: tuple[Decimal | None, str | None],
    revenue: tuple[Decimal | None, str | None],
    impressions: Decimal | None = Decimal(30000),
) -> tuple[object, ...]:
    row = campaign_segment(
        CampaignRowRequest(("camp-1", "Campaign 1"), "41001", None, impressions, spend, revenue)
    )
    return (row.cpm, row.dsp_currency, row.cpm_reason, row.roas, row.roas_reason)


def test_campaign_roas_needs_agreeing_currencies_and_nothing_divides_by_a_missing_spend() -> None:
    usd, sales = (Decimal("210.00"), "USD"), (Decimal("123.00"), "USD")
    # Positive control: spend and sales agree, so CPM and ROAS are computed.
    assert _campaign(usd, sales) == (Decimal("7.00"), "USD", None, Decimal("0.59"), None)
    # Currencies disagree, or either has none: no quotient, its reason; CPM keeps spend's own.
    assert _campaign(usd, (Decimal("123.00"), "EUR")) == (
        Decimal("7.00"), "USD", None, None, ROAS_CURRENCY_TEXT,
    )  # fmt: skip
    assert _campaign(usd, (Decimal("123.00"), None)) == (
        Decimal("7.00"), "USD", None, None, ROAS_CURRENCY_TEXT,
    )  # fmt: skip
    assert _campaign((Decimal("210.00"), None), sales) == (
        None, None, SPEND_CURRENCY_TEXT, None, ROAS_CURRENCY_TEXT,
    )  # fmt: skip
    # Zero or absent spend: neither CPM nor ROAS, each with its reason, never 0.00.
    for spend in ((Decimal(0), "USD"), (None, None)):
        assert _campaign(spend, sales) == (None, None, NO_SPEND_TEXT, None, NO_SPEND_TEXT)
    # Absent sales or impressions: only the figure that needs them is withheld.
    assert _campaign(usd, (None, None)) == (Decimal("7.00"), "USD", None, None, NO_SALES_TEXT)
    assert _campaign(usd, sales, impressions=None) == (
        None, None, NO_IMPRESSIONS_TEXT, Decimal("0.59"), None,
    )  # fmt: skip


def test_a_campaign_row_with_disagreeing_currencies_bakes_no_roas_quotient() -> None:
    def euro_sales(request: QueryRequest, result: QueryResult) -> QueryResult:
        if _measure(request) != "segment_attributed_revenue":
            return result
        return replace(result, rows=tuple({**row, "currency_code": "EUR"} for row in result.rows))

    (row,) = _bake_with(euro_sales)["markets"][0]["campaign_audiences"]["rows"]  # type: ignore[index]
    assert (row["segment_roas_attributed"], row["roas_text"]) == (None, ROAS_CURRENCY_TEXT)
    assert (row["segment_cpm"], row["cpm_text"]) == (
        {"value": "7.00", "currency": "USD", "text": "7.00 USD"},
        None,
    )
    assert "0.59" not in json.dumps(row)
    # Control: agreeing currencies bake the quotient with no reason.
    (agreeing,) = _bake_with(lambda _request, result: result)["markets"][0]["campaign_audiences"][
        "rows"
    ]  # type: ignore[index]
    assert (agreeing["segment_roas_attributed"], agreeing["roas_text"]) == ("0.59", None)


@dataclass
class _Concurrent(_Rosters):
    """A roster port that only answers once two reads are in flight, and counts them."""

    gate: threading.Barrier = field(default_factory=lambda: threading.Barrier(2, timeout=5))
    lock: threading.Lock = field(default_factory=threading.Lock)
    in_flight: int = 0
    peak: int = 0

    def roster(self, request: RosterRequest) -> Roster:
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            # A serial reader would wait here alone and break the barrier.
            self.gate.wait()
            return _Rosters.roster(self, request)
        finally:
            with self.lock:
                self.in_flight -= 1


def test_campaign_rosters_are_read_together_under_the_bound_and_paired_by_campaign() -> None:
    rosters = _Concurrent({"dsp_exposed_only": _roster("dsp_exposed_only", "41001", "41003")})
    renderer, store = _renderer(
        frozenset({(DSP_EXPOSED, SEPTEMBER), (PERFORMANCE, SEPTEMBER)}), rosters, bound=2
    )
    object.__setattr__(store, "campaigns", ("camp-1", "camp-2", "camp-3"))
    result = renderer.render_audiences(AudiencesRenderInput(MONTH))
    assert result.artifact is not None
    # One served-months read for the marketplace, holding all five contracts.
    (served,) = store.served_seen
    assert {item.contract_id for item in served.contracts} == {
        *PACKS, METADATA, PERFORMANCE,
    }  # fmt: skip
    # One campaign-list query, then one roster per delivery: the path and each campaign.
    listing = [item for item in store.seen if "campaign_key" in item.plan.logical.group_by]
    assert len(listing) == 1
    read = [dict(item.filters).get("campaign_key") for item in rosters.seen]
    assert sorted(read, key=str) == [None, "camp-1", "camp-2", "camp-3"]
    # Two at a time: the configured bound, never more and never one after another.
    assert rosters.peak == 2
    # Five profile figures and three per campaign, nothing per campaign before the batch.
    assert len(store.seen) == 1 + 5 + 3 * 3
    # Each campaign's row reads its own figures: camp-n's 210.00 over n x 30,000 impressions.
    body = cast("Any", audiences_artifact_payload(result.artifact))
    rows = body["markets"][0]["campaign_audiences"]["rows"]
    # Each campaign reads by its governed name or chat's numbered placeholder, never its key,
    # under an opaque per-artifact reference.
    assert [
        (row["campaign_ref"], row["campaign_label"], row["segment_cpm"]["value"]) for row in rows
    ] == [
        ("campaign-1", "Fall prospecting", "7.00"),
        ("campaign-2", "Unnamed campaign 1", "3.50"),
        ("campaign-3", "Unnamed campaign 2", "2.33"),
    ]
    assert "camp-" not in json.dumps(body)


def test_a_campaign_segment_only_spend_returns_bakes_a_row_with_each_reason() -> None:
    def no_impressions(request: QueryRequest, result: QueryResult) -> QueryResult:
        # The campaign's own figures; the list that ranks the campaigns still comes back.
        if _measure(request) != "impressions" or "campaign_key" not in request.plan.logical.filters:
            return result
        return replace(result, rows=())

    (row,) = _bake_with(no_impressions)["markets"][0]["campaign_audiences"]["rows"]  # type: ignore[index]
    assert (row["segment_id"], row["segment_impressions"], row["segment_cpm"]) == (
        "41001",
        None,
        None,
    )
    assert (row["cpm_text"], row["segment_roas_attributed"]) == (NO_IMPRESSIONS_TEXT, "0.59")
    # Control: with impressions back, the row computes its CPM.
    (whole,) = _bake_with(lambda _request, result: result)["markets"][0]["campaign_audiences"][
        "rows"
    ]  # type: ignore[index]
    assert (whole["segment_cpm"], whole["cpm_text"]) == (
        {"value": "7.00", "currency": "USD", "text": "7.00 USD"},
        None,
    )


def test_a_campaign_list_answered_for_another_marketplace_bakes_nothing() -> None:
    def foreign_listing(request: QueryRequest, result: QueryResult) -> QueryResult:
        if "campaign_key" not in request.plan.logical.group_by:
            return result
        return replace(
            result, rows=tuple({**row, "marketplace_key": "amazon_uk"} for row in result.rows)
        )

    with pytest.raises(RuntimeError, match="another marketplace's campaigns"):
        _bake_with(foreign_listing)


@dataclass
class _HeldPointer:
    """The current pointer's two fields `publication_for` reads."""

    revision: PresetArtifactRevision
    forward_token: int = 1


@dataclass
class _CurrentPointers:
    held: _HeldPointer | PresetArtifactPointer

    def current(self, page: object, variant: object) -> _HeldPointer | PresetArtifactPointer:
        del page, variant
        return self.held


def _identities(renderer: AudiencesArtifactRenderer) -> tuple[str, ...]:
    result = renderer.render_audiences(AudiencesRenderInput(MONTH))
    assert result.artifact is not None
    return result.publication_identities


def test_a_matching_generation_one_pointer_is_rebuilt_with_its_resolved_window() -> None:
    renderer, _ = _renderer(frozenset({(DSP_EXPOSED, SEPTEMBER), (PERFORMANCE, SEPTEMBER)}))
    identities = _identities(renderer)

    def source(revision: PresetArtifactRevision) -> RetainedAudiencesPresetArtifactSource:
        return RetainedAudiencesPresetArtifactSource(
            compiler=renderer,
            served=lambda: frozenset({(DSP_EXPOSED, MONTH)}),
            pointers=cast("Any", _CurrentPointers(_HeldPointer(revision))),
            clock=lambda: NOW,
            row_budget=300,
        )

    old = PresetArtifactRevision.from_publication_identities(identities, render_format=1)
    upgraded = source(old).publication_for(PresetArtifactVariant("last_complete_month"))
    assert isinstance(upgraded, PresetArtifactPublicationRequest)
    pointer = upgraded.pointer_advance.pointer
    assert (pointer.resolved_window, pointer.supersedes, pointer.forward_token) == (
        SEPTEMBER,
        old,
        2,
    )
    # Control: a pointer already at this generation's revision is left current.
    current = PresetArtifactRevision.from_publication_identities(
        identities, render_format=AUDIENCES_RENDER_FORMAT
    )
    assert pointer.revision == current
    unchanged = source(current).publication_for(PresetArtifactVariant("last_complete_month"))
    assert unchanged == SourceUnchanged(current)  # D26: revision_current, never unavailable


def test_a_label_or_roster_only_change_advances_the_audiences_revision() -> None:
    published = frozenset({(DSP_EXPOSED, SEPTEMBER), (PERFORMANCE, SEPTEMBER)})
    baseline = _identities(_renderer(published)[0])
    # Control: the same reads derive the same identities.
    assert _identities(_renderer(published)[0]) == baseline
    renamed = replace(
        _renderer(published)[0],
        segment_labels=_Labels(
            {**DSP_LABELS, "41001": replace(DSP_LABELS["41001"], name="Demo - Age 35-44")}
        ),
    )

    def named(keys: tuple[str, ...], _window: DateRange) -> dict[str, str]:
        return {key: f"Named {key}" for key in keys}

    campaign_named = replace(_renderer(published)[0], campaign_labels=named)
    rostered = _renderer(
        published, _Rosters({"dsp_exposed_only": _roster("dsp_exposed_only", "41001", "41004")})
    )[0]
    changed = {
        "segment label": _identities(renamed),
        "campaign label": _identities(campaign_named),
        "roster": _identities(rostered),
    }
    # The measured publications are the same, so only the render-inputs identity differs.
    measured = {item for item in baseline if not item.startswith("audiences-render-inputs.v2:")}
    assert all(measured < set(identities) for identities in changed.values())
    assert {name: identities != baseline for name, identities in changed.items()} == dict.fromkeys(
        changed, True
    )


def _measured_only(identities: tuple[str, ...]) -> set[str]:
    return {item for item in identities if not item.startswith("audiences-render-inputs.v2:")}


def _unrevised(result: AudiencesRenderResult) -> object:
    """Return the body without its revision, which names the identities under test."""
    assert result.artifact is not None
    revision = PresetArtifactRevision.from_publication_identities(
        result.publication_identities, render_format=AUDIENCES_RENDER_FORMAT
    ).value
    return json.loads(json.dumps(audiences_artifact_payload(result.artifact)).replace(revision, ""))


def test_a_paid_state_only_change_advances_the_audiences_revision() -> None:
    published = frozenset({(DSP_EXPOSED, SEPTEMBER), (PERFORMANCE, SEPTEMBER)})
    baseline, _ = _renderer(published)
    # August's in-market publication observes the paid feature without planning September.
    observed, _ = _renderer(published | {(INMARKET, AUGUST)})
    before = baseline.render_audiences(AudiencesRenderInput(MONTH))
    after = observed.render_audiences(AudiencesRenderInput(MONTH))
    assert _unrevised(before) != _unrevised(after)
    assert _measured_only(before.publication_identities) == _measured_only(
        after.publication_identities
    )
    assert before.publication_identities != after.publication_identities
    # Control: the same publication history derives the same identities.
    assert _identities(_renderer(published | {(INMARKET, AUGUST)})[0]) == (
        after.publication_identities
    )


def test_a_campaign_publication_only_change_advances_the_audiences_revision() -> None:
    unpublished, _ = _renderer(frozenset({(DSP_EXPOSED, SEPTEMBER)}))
    empty, store = _renderer(frozenset({(DSP_EXPOSED, SEPTEMBER), (PERFORMANCE, SEPTEMBER)}))
    # Pack 8 published September with no campaign, so it plans no campaign figure.
    store.campaigns = ()
    before = unpublished.render_audiences(AudiencesRenderInput(MONTH))
    after = empty.render_audiences(AudiencesRenderInput(MONTH))
    assert _unrevised(before) != _unrevised(after)
    assert _measured_only(before.publication_identities) == _measured_only(
        after.publication_identities
    )
    assert before.publication_identities != after.publication_identities


def _listing(impressions: dict[str, int]) -> Callable[[QueryRequest, QueryResult], QueryResult]:
    """Give pack 8's campaign list each campaign's matched-segment impressions."""

    def change(request: QueryRequest, result: QueryResult) -> QueryResult:
        if "campaign_key" not in request.plan.logical.group_by:
            return result
        alias = measure_alias(0)
        return replace(
            result,
            rows=tuple(
                {**row, alias: impressions[str(row["campaign_key"])]} for row in result.rows
            ),
        )

    return change


def _bounded_bake(campaigns: tuple[str, ...], campaign_bound: int) -> tuple[Any, _Bake]:
    published = frozenset({(DSP_EXPOSED, SEPTEMBER), (PERFORMANCE, SEPTEMBER)})
    renderer, store = _renderer(published, campaign_bound=campaign_bound)
    store.campaigns = campaigns
    original = store.run
    listing = _listing({"camp-1": 10_000, "camp-2": 30_000, "camp-3": 20_000})

    def run(request: QueryRequest) -> QueryResult:
        return listing(request, original(request))

    object.__setattr__(store, "run", run)
    result = renderer.render_audiences(AudiencesRenderInput(MONTH))
    assert result.artifact is not None
    body = cast("Any", audiences_artifact_payload(result.artifact))
    return body["markets"][0]["campaign_audiences"], store


def _measured_campaigns(store: _Bake) -> set[str]:
    return {
        str(request.plan.logical.filters["campaign_key"])
        for request in store.seen
        if "campaign_key" in request.plan.logical.filters
    }


def test_the_page_shows_the_top_campaigns_by_impressions_with_the_servers_note() -> None:
    campaigns, store = _bounded_bake(("camp-1", "camp-2", "camp-3"), campaign_bound=2)
    # By largest matched-segment impressions, descending; the third is never measured.
    assert [(row["campaign_ref"], row["campaign_label"]) for row in campaigns["rows"]] == [
        ("campaign-1", "Unnamed campaign 1"),
        ("campaign-2", "Unnamed campaign 2"),
    ]
    assert _measured_campaigns(store) == {"camp-2", "camp-3"}
    assert campaigns["shown_text"] == "Showing 2 of 3 campaigns"
    # Control: with every listed campaign within the bound, all are shown and no note serves.
    every, store = _bounded_bake(("camp-1", "camp-2", "camp-3"), campaign_bound=3)
    assert [row["campaign_label"] for row in every["rows"]] == [
        "Unnamed campaign 1",
        "Unnamed campaign 2",
        "Fall prospecting",
    ]
    assert _measured_campaigns(store) == {"camp-1", "camp-2", "camp-3"}
    assert every["shown_text"] is None


def test_campaigns_with_equal_impressions_rank_by_the_order_numbering_their_references() -> None:
    published = frozenset({(DSP_EXPOSED, SEPTEMBER), (PERFORMANCE, SEPTEMBER)})
    renderer, store = _renderer(published, campaign_bound=1)
    store.campaigns = ("camp-2", "camp-1")
    result = renderer.render_audiences(AudiencesRenderInput(MONTH))
    assert result.artifact is not None
    body = cast("Any", audiences_artifact_payload(result.artifact))
    campaigns = body["markets"][0]["campaign_audiences"]
    assert [row["campaign_label"] for row in campaigns["rows"]] == ["Fall prospecting"]
    assert campaigns["shown_text"] == "Showing 1 of 2 campaigns"


def test_an_over_bound_new_month_publishes_a_typed_pointer_that_supersedes_the_prior_month() -> (
    None
):
    renderer, _ = _renderer(frozenset({(DSP_EXPOSED, SEPTEMBER), (PERFORMANCE, SEPTEMBER)}))
    template = renderer.render_audiences(AudiencesRenderInput(MONTH))
    artifact = template.artifact
    assert artifact is not None

    @dataclass(frozen=True)
    class MonthlyCompiler:
        def render_audiences(self, request: AudiencesRenderInput) -> AudiencesRenderResult:
            return AudiencesRenderResult(
                replace(artifact, month=request.period),
                (*template.publication_identities, f"month:{request.period.start.isoformat()}"),
            )

    def source(
        period: CalendarPeriod,
        row_budget: int,
        pointers: _Pointers | _CurrentPointers,
    ) -> AudiencesPresetArtifactBaker:
        return AudiencesPresetArtifactBaker(
            RetainedAudiencesPresetArtifactSource(
                compiler=MonthlyCompiler(),
                served=lambda: frozenset({(DSP_EXPOSED, period)}),
                pointers=cast("Any", pointers),
                clock=lambda: NOW,
                row_budget=row_budget,
            )
        )

    intent = BakeIntent("audiences", "last_complete_month")
    first = source(MONTH, 300, _Pointers()).bake(intent)
    assert isinstance(first, BakePublication)
    first_payload = json.loads(first.request.body.payload_json)
    rows = sum(len(view["rows"]) for view in first_payload["markets"][0]["views"]) + len(
        first_payload["markets"][0]["campaign_audiences"]["rows"]
    )
    assert isinstance(source(MONTH, rows, _Pointers()).bake(intent), BakePublication)

    second = source(
        OCTOBER_MONTH,
        rows - 1,
        _CurrentPointers(first.request.pointer_advance.pointer),
    ).bake(intent)
    assert isinstance(second, BakePublication)
    unavailable = require_audiences_unavailable_artifact_payload(
        json.loads(second.request.body.payload_json)
    )
    assert unavailable == AudienceUnavailableArtifact(
        OCTOBER, "October 2026", AUDIENCES_BODY_OVER_BOUND
    )
    pointer = second.request.pointer_advance.pointer
    assert (pointer.resolved_window, pointer.supersedes, pointer.forward_token) == (
        OCTOBER,
        first.request.body.revision,
        2,
    )
    # Paired pre-fix control: the target-month marker, not September's body, is authoritative.
    assert pointer.body_digest == second.request.body.digest
    assert pointer.body_digest != first.request.body.digest


def test_wide_rows_and_the_envelope_stay_within_their_registered_ceilings() -> None:
    long_name = "N" * 120

    def encoded(segments: int, campaign_segments: int) -> int:
        ids = tuple(str(50_000 + index) for index in range(segments))
        delivered = tuple(str(60_000 + index) for index in range(campaign_segments))
        published = frozenset({(DSP_EXPOSED, SEPTEMBER), (PERFORMANCE, SEPTEMBER)})
        renderer, store = _renderer(
            published, _Rosters({"dsp_exposed_only": _roster("dsp_exposed_only", *ids)})
        )
        renderer = replace(
            renderer,
            segment_labels=_Labels(
                {
                    key: SegmentLabel(DSP, key, long_name, SegmentProvider.THIRD_PARTY)
                    for key in (*ids, *delivered)
                }
            ),
        )
        original = store.run

        def run(request: QueryRequest) -> QueryResult:
            # Eight-digit figures on every segment the plan asks for.
            result = original(request)
            alias = measure_alias(0)
            first = {row.get("campaign_key"): row for row in reversed(result.rows)}
            rows: list[QueryRow] = []
            for row in first.values():
                if "segment_id" not in row:
                    rows.append({**row, alias: cast("int", row[alias]) * 100_000})
                    continue
                pool = ids if "profile_path" in request.plan.logical.filters else delivered
                rows.extend(
                    {**row, "segment_id": key, alias: cast("int", row[alias]) * (100_000 - index)}
                    for index, key in enumerate(pool)
                )
            return replace(result, rows=tuple(rows))

        object.__setattr__(store, "run", run)
        result = renderer.render_audiences(AudiencesRenderInput(MONTH))
        assert result.artifact is not None
        payload = audiences_artifact_payload(result.artifact)
        return len(canonical_preset_artifact_json(payload).encode())

    base = encoded(1, 1)
    # One profile row and one campaign row with the envelope, then 40 more of each.
    assert base <= AUDIENCES_ENVELOPE_MAX_BYTES + 2 * AUDIENCES_ROW_MAX_BYTES
    assert encoded(41, 1) - base <= 40 * AUDIENCES_ROW_MAX_BYTES
    assert encoded(1, 41) - base <= 40 * AUDIENCES_ROW_MAX_BYTES


def test_the_prefetch_walk_reaches_every_campaign_scope_read_and_executes_no_query() -> None:
    """A5.1 prefetch: one synthetic listed campaign per marketplace, every query answered empty.

    Control: the same month's real render lists campaigns only from pack 8's answer, so with no
    answer it reads no campaign-scope roster at all.
    """
    published = frozenset({(DSP_EXPOSED, SEPTEMBER), (PERFORMANCE, SEPTEMBER)})
    renderer, store = _renderer(published, _Rosters())
    rosters = cast("_Rosters", renderer.rosters)

    renderer.prefetch_audiences(AudiencesRenderInput(MONTH))

    assert store.seen == []  # no query reached the port
    campaigns = {dict(item.filters).get("campaign_key") for item in rosters.seen}
    assert "campaign:prefetch-0001" in campaigns
    control, control_store = _renderer(published, _Rosters())
    control_store.seen.clear()
    control.render_audiences(AudiencesRenderInput(MONTH))
    seen = cast("_Rosters", control.rosters).seen
    assert "campaign:prefetch-0001" not in {dict(item.filters).get("campaign_key") for item in seen}


def test_the_prefetch_walk_reaches_the_naming_step() -> None:
    """Review #2/#3: the walk asks every segment-label namespace and the synthetic campaign's name.

    The prefetch answers queries empty; those calls are what pin the label and campaign-name
    scopes, so the test watches the calls themselves, not only that `_named` was entered.
    """
    published = frozenset({(DSP_EXPOSED, SEPTEMBER), (PERFORMANCE, SEPTEMBER)})
    renderer, _store = _renderer(published, _Rosters())
    labelled: list[SegmentLabelRequest] = []
    named: list[tuple[str, ...]] = []

    class _Watched:
        def labels(self, request: SegmentLabelRequest) -> dict[str, Any]:
            labelled.append(request)
            return {}

    def names(keys: tuple[str, ...], _window: object) -> dict[str, str]:
        named.append(keys)
        return {key: key for key in keys}

    walker = replace(renderer, segment_labels=_Watched(), campaign_labels=names)
    walker.prefetch_audiences(AudiencesRenderInput(MONTH))
    assert {request.namespace for request in labelled} == set(VIEW_NAMESPACES.values())
    assert named == [("campaign:prefetch-0001",)]
