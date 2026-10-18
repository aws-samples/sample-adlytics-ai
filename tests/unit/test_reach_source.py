# SPDX-License-Identifier: MIT-0
"""The Reach and funnel materializer: planned per-cell answers, fail-closed registration (ADR-075).

Every figure is fictional and synthetic. The renderer runs the real measurement batch, planner and
shaping against an in-memory query port, as the New customers tests do, so the bake is exactly
what the planned answers say.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from publication_support import StubPublications
from test_new_to_brand_buyers import (
    SEPTEMBER,
    SEPTEMBER_PERIOD,
    _chat,  # pyright: ignore[reportPrivateUsage]
)

from adapters.config import PresetArtifactBakeSettings
from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    handle,
)
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher import reach_source
from adapters.jobs.preset_artifact_publisher.bake_results import (
    BakePublication,
    BakeUnavailable,
)
from adapters.jobs.preset_artifact_publisher.baker import (
    CampaignPresetArtifactBaker,
    ReachPresetArtifactBaker,
    RegisteredPresetArtifactBaker,
    UnavailableCampaignPresetArtifactSource,
    UnavailablePresetArtifactBaker,
    UnavailableReachPresetArtifactSource,
)
from adapters.jobs.preset_artifact_publisher.reach_source import (
    ReachArtifactRenderer,
    ReachRenderInput,
    ReachRenderResult,
    RetainedReachPresetArtifactSource,
    reach_policy,
    retained_reach_preset_artifact_source,
)
from adapters.jobs.preset_artifact_publisher.transport import BakeIntent
from adapters.outbound.amc_instance import load_amc_instance
from adapters.outbound.bake_sources import BakeSourceReads
from adapters.outbound.metric_dictionary import load_catalog
from domain.measurement.answers import ASSOCIATION_QUALIFIER
from domain.measurement.planning import measure_alias
from domain.measurement.rosters import Roster, RosterEntry, RosterKind
from domain.presentation.insights import InsightSlotId
from domain.presentation.page_schemas import (
    PageTime,
    PresentationTemporalPolicy,
    TemporalComparisonPolicy,
    TemporalPresetId,
)
from domain.presentation.preset_artifacts import PresetArtifactPage, PresetArtifactVariant
from domain.presentation.reach import REACH_PAGE_ID
from domain.semantics.amc_instance import (
    AMC_INSTANCE_KIND_MISSING,
    AMC_INSTANCE_KIND_UNKNOWN,
    AmcInstanceKind,
    AmcInstanceResolution,
)
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import ValidationError
from domain.shared_kernel.provenance import Suppression
from ports.outbound.publication_selection import PresentationEvidenceInspectionPort
from ports.outbound.query import QueryRequest, QueryResult, QueryRow
from ports.outbound.rosters import RosterRequest
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-100", "ADR-101", "ADR-102")]


def _reads() -> BakeSourceReads:
    """Return a fictional pinnable reader: composition builds over it and never reads."""
    return BakeSourceReads(cast("Any", None), cast("Any", None))


ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT)
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
INSIGHTS = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)
WEEK = CalendarPeriod(date(2026, 9, 21), date(2026, 9, 27), TimeGrain.WEEK)
PRIOR = WEEK.previous()
US = "amazon_us"
EXPOSURE = "amc-exposure-groups"
_COLUMNS = {
    "shoppers_reached": "shoppers_reached",
    "search_shoppers": "search_shoppers",
    "purchasers": "purchasers",
    "revenue_incl_halo": "revenue_incl_halo",
}
_GRID: dict[tuple[str, str], tuple[int, int, int, str]] = {
    ("dsp_only", "account"): (2000, 200, 100, "3000"),
    ("dsp_only", "device_or_browser"): (1000, 50, 10, "250"),
    ("sponsored_only", "account"): (5000, 600, 250, "8000"),
    ("sponsored_only", "device_or_browser"): (2000, 100, 20, "500"),
    ("dsp_and_sponsored", "account"): (3000, 450, 300, "10500"),
    ("dsp_and_sponsored", "device_or_browser"): (500, 40, 5, "100"),
}


@dataclass
class ReachQueries:
    """Grouped rows per governed plan: one per (marketplace, exposure group, identity space)."""

    not_returned: tuple[tuple[str, str], ...] = ()
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        plan = request.plan.logical
        (measure,) = plan.measures
        # A measure outside the four cell fields (impressions) reads the shoppers column.
        position = tuple(_COLUMNS).index(measure) if measure in _COLUMNS else 0
        market = plan.filters["marketplace_key"]
        rows: list[QueryRow] = []
        for (group, space), values in _GRID.items():
            if plan.filters.get("row_kind") == "outcome" and space != "account":
                # The touch-only outcome branch returns account IDs only (design §8).
                continue
            row: dict[str, str | int | Decimal | None] = {
                "marketplace_key": market,
                "exposure_group": group,
                "identity_space": space,
            }
            for column in plan.carried_columns:
                row[column] = "USD"
            value = None if (group, space) in self.not_returned else Decimal(values[position])
            row[measure_alias(0)] = value
            rows.append({key: row[key] for key in (*plan.group_by, *row) if key in row})
        return QueryResult(
            rows=tuple(rows), bytes_scanned=1_024, truncated=False, suppression=Suppression(False)
        )


@dataclass
class _Served:
    periods: frozenset[CalendarPeriod] = frozenset({WEEK, PRIOR})
    series: dict[CalendarPeriod, tuple[str | None, ...]] = field(
        default_factory=dict[CalendarPeriod, tuple[str | None, ...]]
    )
    """The variant series holding each period (plan D9); absent means the base pack's."""

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        del request
        return ServedPeriods(
            frozenset((EXPOSURE, period) for period in self.periods),
            {EXPOSURE: 16},
            {(EXPOSURE, period): held for period, held in self.series.items()},
        )


def _renderer(
    queries: ReachQueries,
    served: _Served | None = None,
    instance: AmcInstanceKind = AmcInstanceKind.STANDARD,
) -> ReachArtifactRenderer:
    stub = StubPublications()
    return ReachArtifactRenderer(
        catalog=CATALOG,
        contracts=CONTRACTS,
        evidence=cast("PresentationEvidenceInspectionPort", stub),
        tooling=MeasurementTooling(queries=queries, publications=stub, periods=served or _Served()),
        marketplaces=(US,),
        insight_contracts=(
            INSIGHTS[InsightSlotId.REACH_OVERLAP],
            INSIGHTS[InsightSlotId.FULL_FUNNEL_ASSOCIATION],
            INSIGHTS[InsightSlotId.FREQUENCY_SATURATION],
        ),
        clock=lambda: NOW,
        instance=instance,
    )


def _baked(queries: ReachQueries | None = None, served: _Served | None = None) -> dict[str, object]:
    result = _renderer(queries or ReachQueries(), served).render_reach(
        ReachRenderInput(TemporalPresetId.LAST_COMPLETE_WEEK, WEEK)
    )
    assert result.artifact is not None
    assert result.publication_identities
    from domain.presentation.reach import reach_artifact_payload  # noqa: PLC0415

    return reach_artifact_payload(result.artifact)


def test_the_bake_equals_the_planned_per_cell_answers() -> None:
    queries = ReachQueries()
    body = _baked(queries)
    (market,) = cast("list[dict[str, object]]", body["markets"])
    overlap = cast("dict[str, list[dict[str, dict[str, str]]]]", market["overlap"])
    tiles = {tile["metric"]: tile["current"] for tile in overlap["tiles"]}  # type: ignore[index]
    # 3000 / 10000 on account IDs, from the planned shoppers_reached answer.
    assert tiles["overlap_share"]["value"] == "0.3000"  # type: ignore[index]
    funnel = cast("dict[str, list[dict[str, object]]]", market["funnel"])
    both = next(row for row in funnel["rows"] if row["exposure_group"] == "dsp_and_sponsored")
    figures = cast("dict[str, dict[str, str]]", both["figures"])
    assert figures["purchaser_rate"]["value"] == "0.1000"
    assert figures["revenue_per_exposed_shopper"]["currency_code"] == "USD"
    # Every plan reads one metric per cell grouped by exposure group and identity space, for
    # the period and its prior week, pinned to the platform grain set and the week grain.
    metrics = sorted({request.plan.logical.metric.name for request in queries.seen})
    assert metrics == [
        "purchasers_in_window",
        "revenue_incl_halo_in_window",
        "search_shoppers_in_window",
        "shoppers_reached",
    ]
    assert len(queries.seen) == 8
    for request in queries.seen:
        plan = request.plan.logical
        assert plan.group_by[-2:] == ("exposure_group", "identity_space")
        assert (plan.filters["time_grain"], plan.filters["grain_set"]) == ("week", "platform")
        # The base pack's series: every figure on its `combined` rows.
        assert plan.filters["row_kind"] == "combined"


def test_a_not_returned_cell_bakes_not_available_never_a_partial_share() -> None:
    body = _baked(ReachQueries(not_returned=(("dsp_only", "device_or_browser"),)))
    (market,) = cast("list[dict[str, object]]", body["markets"])
    overlap = cast("dict[str, list[dict[str, dict[str, object]]]]", market["overlap"])
    tiles = {tile["metric"]: tile["current"] for tile in overlap["tiles"]}  # type: ignore[index]
    assert tiles["overlap_share_all_ids"] == {  # type: ignore[comparison-overlap]
        "absence": {"reason": "not_returned", "text": "too few shoppers, or none"}
    }
    # Control: the account-ID share reads only cells that came back.
    assert tiles["overlap_share"]["value"] == "0.3000"  # type: ignore[index]


type _Series = dict[CalendarPeriod, tuple[str | None, ...]]
TOUCH_ONLY: _Series = {WEEK: ("touch_only",), PRIOR: ("touch_only",)}


def _series(held: _Series) -> _Series:
    return held


def _market(body: dict[str, object]) -> dict[str, object]:
    (market,) = cast("list[dict[str, object]]", body["markets"])
    return market


def _tiles(market: dict[str, object]) -> dict[str, dict[str, object]]:
    overlap = cast("dict[str, list[dict[str, object]]]", market["overlap"])
    return {str(tile["metric"]): tile for tile in overlap["tiles"]}


def test_a_touch_only_period_reads_reach_from_exposure_rows_and_rates_from_outcome_rows() -> None:
    """Plan D9: an activated touch-only series serves; each figure from the rows carrying it."""
    queries = ReachQueries()
    body = _baked(queries, _Served(series=TOUCH_ONLY))
    pins = {
        (request.plan.logical.metric.name, request.plan.logical.filters["row_kind"])
        for request in queries.seen
    }
    assert pins == {
        ("shoppers_reached", "exposure"),
        ("search_shoppers_in_window", "outcome"),
        ("purchasers_in_window", "outcome"),
        ("revenue_incl_halo_in_window", "outcome"),
    }
    market = _market(body)
    assert _tiles(market)["overlap_share"]["current"]["value"] == "0.3000"  # type: ignore[index]
    funnel = cast("dict[str, list[dict[str, object]]]", market["funnel"])
    both = next(row for row in funnel["rows"] if row["exposure_group"] == "dsp_and_sponsored")
    figures = cast("dict[str, dict[str, str]]", both["figures"])
    assert figures["purchaser_rate"]["value"] == "0.1000"
    # Same-variant prior: the change is computed, as on the base pack (control below).
    assert _tiles(market)["overlap_share"]["change"] == {"value": "0.00", "text": "0.00 points"}
    # Isolation control: the base series of the same grid pins only `combined` rows.
    base = ReachQueries()
    assert _baked(base) == body
    assert {request.plan.logical.filters["row_kind"] for request in base.seen} == {"combined"}


def test_a_period_two_series_hold_bakes_nothing() -> None:
    mixed = _Served(series=_series({WEEK: (None, "touch_only")}))
    result = _renderer(ReachQueries(), mixed).render_reach(
        ReachRenderInput(TemporalPresetId.LAST_COMPLETE_WEEK, WEEK)
    )
    assert (result.artifact, result.publication_identities) == (None, ())
    # Control: the same period held by one series bakes.
    assert _baked(served=_Served(series=_series({WEEK: ("touch_only",), PRIOR: ("touch_only",)})))


def test_a_prior_period_of_another_variant_is_refused_its_change() -> None:
    body = _baked(served=_Served(series=_series({WEEK: ("touch_only",), PRIOR: (None,)})))
    tile = _tiles(_market(body))["overlap_share"]
    assert tile["prior"] is None
    assert tile["change"] == {
        "absence": {
            "reason": "pack_variant_mismatch",
            "text": "No change is shown, because a figure isn't available.",
        }
    }
    # A prior two series hold is refused the same way.
    mixed = _baked(
        served=_Served(series=_series({WEEK: ("touch_only",), PRIOR: (None, "touch_only")}))
    )
    change = _tiles(_market(mixed))["overlap_share"]["change"]
    assert change["absence"]["reason"] == "pack_variant_mismatch"  # type: ignore[index]
    # Control: a prior of the same series is compared.
    same = _baked(served=_Served(series=TOUCH_ONLY))
    assert _tiles(_market(same))["overlap_share"]["change"] == {
        "value": "0.00",
        "text": "0.00 points",
    }


def test_an_instant_access_instance_bakes_needs_dsp_with_nothing_planned() -> None:
    queries = ReachQueries()
    result = _renderer(queries, _Served(frozenset()), AmcInstanceKind.INSTANT_ACCESS).render_reach(
        ReachRenderInput(TemporalPresetId.LAST_COMPLETE_WEEK, WEEK)
    )
    assert result.artifact is not None
    assert queries.seen == []
    assert result.publication_identities == (
        "amc-instance.v1:instant_access:week:2026-09-21:2026-09-27",
    )
    from domain.presentation.reach import reach_artifact_payload  # noqa: PLC0415

    market = _market(reach_artifact_payload(result.artifact))
    needs = "Needs DSP and sponsored ads on one AMC instance"
    overlap = cast("dict[str, object]", market["overlap"])
    funnel = cast("dict[str, object]", market["funnel"])
    assert (overlap["state"], overlap["text"]) == ("needs_dsp", needs)
    assert (funnel["state"], funnel["text"]) == ("needs_dsp", needs)
    # Control: a standard instance with no publication bakes nothing.
    standard = _renderer(ReachQueries(), _Served(frozenset())).render_reach(
        ReachRenderInput(TemporalPresetId.LAST_COMPLETE_WEEK, WEEK)
    )
    assert standard.artifact is None


def test_the_instance_kind_is_read_from_its_declaration_and_never_defaulted(
    tmp_path: pathlib.Path,
) -> None:
    # The deployed declaration resolves to the STANDARD instance the live rows name.
    assert load_amc_instance(ROOT) == AmcInstanceResolution(AmcInstanceKind.STANDARD)
    path = tmp_path / "contracts/shared/amc-instance.yaml"
    path.parent.mkdir(parents=True)

    def declared(text: str | None) -> AmcInstanceResolution:
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text)
        return load_amc_instance(tmp_path)

    assert declared("version: 1\nkind: standard\n").kind is AmcInstanceKind.STANDARD
    assert declared("version: 1\nkind: instant_access\n").kind is AmcInstanceKind.INSTANT_ACCESS
    assert declared(None).reason == AMC_INSTANCE_KIND_MISSING
    assert declared("version: 1\n").reason == AMC_INSTANCE_KIND_MISSING
    assert declared("version: 1\nkind: sandbox\n").reason == AMC_INSTANCE_KIND_UNKNOWN
    assert declared("- not a mapping\n").reason == AMC_INSTANCE_KIND_UNKNOWN
    assert declared("kind: [unclosed\n").reason == AMC_INSTANCE_KIND_UNKNOWN


def _resolved(
    resolution: AmcInstanceResolution,
) -> Callable[[pathlib.Path], AmcInstanceResolution]:
    def load(_root: pathlib.Path) -> AmcInstanceResolution:
        return resolution

    return load


def test_an_unknown_instance_kind_keeps_the_source_closed_with_its_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    root = _declared_root(tmp_path)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    def schemas(_root: pathlib.Path) -> object:
        return load_v2_page_schemas(root)

    monkeypatch.setattr(reach_source, "load_v2_page_schemas", schemas)
    for reason in (AMC_INSTANCE_KIND_MISSING, AMC_INSTANCE_KIND_UNKNOWN):
        monkeypatch.setattr(
            reach_source, "load_amc_instance", _resolved(AmcInstanceResolution(None, reason))
        )
        closed = retained_reach_preset_artifact_source(
            _settings(), cast("Any", _Pointers()), cast("Any", None), _reads()
        )
        assert closed is None  # logged once with its closure condition (A5.4)
        result = ReachPresetArtifactBaker(UnavailableReachPresetArtifactSource()).bake(
            BakeIntent(REACH_PAGE_ID, "last_complete_week")
        )
        assert isinstance(result, BakeUnavailable)
        assert result.reason == "reach_materialization_unavailable"  # the generated reason
    # Control: each governed kind opens the source, carrying its kind.
    for kind in AmcInstanceKind:
        monkeypatch.setattr(
            reach_source, "load_amc_instance", _resolved(AmcInstanceResolution(kind))
        )
        opened = retained_reach_preset_artifact_source(
            _settings(), cast("Any", _Pointers()), cast("Any", None), _reads()
        )
        assert isinstance(opened, RetainedReachPresetArtifactSource)
        assert opened.instance is kind


def test_a_period_with_no_publication_bakes_nothing() -> None:
    result = _renderer(ReachQueries(), _Served(frozenset())).render_reach(
        ReachRenderInput(TemporalPresetId.LAST_COMPLETE_WEEK, WEEK)
    )
    assert (result.artifact, result.publication_identities) == (None, ())


POLICY = PresentationTemporalPolicy(
    candidate_presets=(
        TemporalPresetId.LAST_COMPLETE_WEEK,
        TemporalPresetId.LAST_COMPLETE_MONTH,
        TemporalPresetId.LAST_COMPLETE_QUARTER,
    ),
    default_preset=TemporalPresetId.LAST_COMPLETE_MONTH,
    custom_enabled=False,
    custom_min_duration_days=1,
    custom_max_duration_days=90,
    comparison_policy=TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD,
    display_grains=(PageTime.DECLARED_PERIOD,),
)
HANDOFF_ENTRY = """\
  - id: reach
    display_name: Reach and funnel
    route: /reach/
    purpose: Placeholder page purpose; the UI sub-slice writes the page copy.
    primary_job: Placeholder primary job; the UI sub-slice writes the page copy.
    mode: selectable
    boundary_required: true
    compatibility_routes: []
    temporal_policy:
      candidate_presets: [last_complete_week, last_complete_month, last_complete_quarter]
      default_preset: last_complete_month
      custom: {enabled: false, min_duration_days: 1, max_duration_days: 90}
      comparison_policy: prior_calendar_period
      display_grains: [declared_period]
    artifacts:
      - id: reach
        type: ReachAndFunnelArtifact
        version: 1
        outcomes: [complete, empty, insufficient]
    visuals: []
    sections:
      - {id: workspace-scope, cardinality: exactly_one}
      - id: overlap
        cardinality: exactly_one
        declared_periods: [last_complete_week, last_complete_month]
        period_anchor: true
      - id: funnel
        cardinality: exactly_one
        declared_periods: [last_complete_week, last_complete_month]
        period_anchor: true
      - {id: reach-insights, cardinality: exactly_one}
      - id: frequency
        cardinality: exactly_one
        declared_periods: [last_complete_month, last_complete_quarter]
        period_anchor: false
      - {id: evidence-trace, cardinality: exactly_one}
      - {id: boundary, cardinality: exactly_one}
    actions: []
"""
"""The v2 entry as `docs/ui/reach-and-funnel/low-level-design.md` declares it, minus the ADR-115
`navigation` block this branch's loader doesn't read yet."""


def _declared_root(tmp_path: pathlib.Path) -> pathlib.Path:
    committed = (ROOT / "presentation/pages/v2/pages.yaml").read_text()
    target = tmp_path / "presentation/pages/v2/pages.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(committed)
    return tmp_path


@dataclass
class _Pointers:
    def current(self, page: PresetArtifactPage, variant: PresetArtifactVariant) -> None:
        del page, variant


def _settings() -> PresetArtifactBakeSettings:
    return PresetArtifactBakeSettings(
        artifact_bucket="fictional-artifacts",
        pointer_table="fictional-pointers",
        semantic_publications_table="fictional-table",
        semantic_v2_bucket="fictional-bucket",
        athena_workgroup="fictional-workgroup",
        semantic_v2_current_database="fictional-database",
        athena_result_reuse_max_age_minutes=1,
        athena_query_batch_max_concurrency=1,
    )


def test_the_committed_page_entry_opens_the_materializer_policy() -> None:
    schemas = load_v2_page_schemas(ROOT)
    assert REACH_PAGE_ID in {page.page_id for page in schemas.pages}
    assert reach_policy(schemas) == POLICY


def test_only_an_entry_declaring_reach_v1_and_its_sections_opens_the_source(
    tmp_path: pathlib.Path,
) -> None:
    schemas = load_v2_page_schemas(_declared_root(tmp_path))
    page = next(item for item in schemas.pages if item.page_id == REACH_PAGE_ID)
    (artifact,) = page.artifacts

    def policy(**changes: object) -> PresentationTemporalPolicy | None:
        changed = replace(page, **changes)  # type: ignore[arg-type]
        others = tuple(item for item in schemas.pages if item.page_id != REACH_PAGE_ID)
        return reach_policy(replace(schemas, pages=(*others, changed)))

    # Positive control: the hand-off entry opens the source.
    assert policy() == POLICY
    frequency = next(item for item in page.sections if item.section_id == "frequency")
    refused = {
        "wrong type": {"artifacts": (replace(artifact, artifact_type="NewCustomersArtifact"),)},
        "wrong version": {"artifacts": (replace(artifact, version=2),)},
        "an outcome missing": {"artifacts": (replace(artifact, outcomes=("complete", "empty")),)},
        "funnel missing": {
            "sections": tuple(item for item in page.sections if item.section_id != "funnel")
        },
        "frequency anchoring": {
            "sections": tuple(
                replace(item, period_anchor=True) if item is frequency else item
                for item in page.sections
            )
        },
    }
    assert {name: policy(**changes) for name, changes in refused.items()} == dict.fromkeys(refused)


@dataclass
class _Compiler:
    renderer: ReachArtifactRenderer

    def render_reach(self, request: ReachRenderInput) -> ReachRenderResult:
        return self.renderer.render_reach(request)


def test_an_injected_page_entry_bakes_a_publication_equal_to_the_planned_answers() -> None:
    source = RetainedReachPresetArtifactSource(
        temporal_policy=POLICY,
        compiler=_Compiler(_renderer(ReachQueries())),
        served=lambda: frozenset({(EXPOSURE, WEEK), (EXPOSURE, PRIOR)}),
        cadence_lag_days=16,
        pointers=cast("object", _Pointers()),  # type: ignore[arg-type]
        clock=lambda: NOW,
        instance=AmcInstanceKind.STANDARD,
    )
    assert source.resolve(TemporalPresetId.LAST_COMPLETE_WEEK) == ReachRenderInput(
        TemporalPresetId.LAST_COMPLETE_WEEK, WEEK
    )
    # The quarter preset anchors on frequency (pack 6), which has no publication here.
    assert source.resolve(TemporalPresetId.LAST_COMPLETE_QUARTER) is None
    baker = RegisteredPresetArtifactBaker(
        bakers={
            "campaigns": CampaignPresetArtifactBaker(UnavailableCampaignPresetArtifactSource()),
            "reach": ReachPresetArtifactBaker(source),
        },
        fallback=UnavailablePresetArtifactBaker(),
        roster_pages=("campaigns", "reach"),
    )
    result = baker.bake(BakeIntent(REACH_PAGE_ID, "last_complete_week"))
    assert isinstance(result, BakePublication)
    body = json.loads(result.request.body.payload_json)
    assert body == _baked()
    assert (body["type"], body["version"], body["preset"]) == (
        "ReachAndFunnelArtifact",
        1,
        "last_complete_week",
    )
    # The pointer names the served period, so the Reach route's preset buttons carry dates.
    pointer = result.request.pointer_advance.pointer
    assert (pointer.resolved_window, pointer.comparison_window) == (WEEK.window, None)


def test_an_instant_access_source_resolves_the_latest_week_on_the_calendar_alone() -> None:
    source = RetainedReachPresetArtifactSource(
        temporal_policy=POLICY,
        compiler=_Compiler(_renderer(ReachQueries(), instance=AmcInstanceKind.INSTANT_ACCESS)),
        served=frozenset,
        cadence_lag_days=16,
        pointers=cast("object", _Pointers()),  # type: ignore[arg-type]
        clock=lambda: NOW,
        instance=AmcInstanceKind.INSTANT_ACCESS,
    )
    # 2026-10-12 is a Monday: the latest complete ISO week is 5-11 Oct, with nothing published.
    resolved = source.resolve(TemporalPresetId.LAST_COMPLETE_WEEK)
    assert resolved is not None
    assert (resolved.period.window.start, resolved.period.window.end) == (
        date(2026, 10, 5),
        date(2026, 10, 11),
    )
    # The quarter preset still anchors on frequency (pack 6), with nothing published.
    assert source.resolve(TemporalPresetId.LAST_COMPLETE_QUARTER) is None
    # Control: a standard source with nothing published resolves nothing.
    standard = replace(source, instance=AmcInstanceKind.STANDARD)
    assert standard.resolve(TemporalPresetId.LAST_COMPLETE_WEEK) is None


def test_a_chat_answer_on_a_causal_false_metric_carries_association_not_cause() -> None:
    """ADR-102: every pack 5 metric is `causal: false`, so its answer says so, typed and in text."""
    stub = StubPublications()
    response = handle(
        MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=CATALOG,
            contracts=CONTRACTS,
            metric_name="shoppers_reached",
            grains=("marketplace", "exposure_group", "identity_space"),
            window=WEEK.window,
            computed_at=NOW,
            correlation_id="corr-reach-chat",
            filters={"marketplace_key": US},
        ),
        MeasurementTooling(queries=ReachQueries(), publications=stub, periods=_Served()),
    )
    narrative = cast("tuple[str, ...]", response.narrative)  # type: ignore[attr-defined]
    assert ASSOCIATION_QUALIFIER in narrative
    assert "association, not cause" in ASSOCIATION_QUALIFIER
    kinds = {item.kind.value for item in response.metadata.qualifiers}  # type: ignore[attr-defined]
    assert "association_only" in kinds
    # Paired control: a metric with no causal flag (new buyers) carries no such qualifier.
    control, _queries = _chat(
        CONTRACTS,
        SEPTEMBER,
        (SEPTEMBER_PERIOD,),
        ({"marketplace_key": US, measure_alias(0): 36419},),
    )
    lines = cast("tuple[str, ...]", control.narrative)  # type: ignore[attr-defined]
    assert ASSOCIATION_QUALIFIER not in lines
    assert all("association, not cause" not in line for line in lines)


def _chat_reach(  # noqa: PLR0913 -- the metric, its series and two keyword options
    metric: str, served: _Served, *, prior: bool = False, filters: dict[str, str] | None = None
) -> tuple[object, ReachQueries]:
    queries = ReachQueries()
    stub = StubPublications()
    response = handle(
        MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=CATALOG,
            contracts=CONTRACTS,
            metric_name=metric,
            grains=("marketplace", "exposure_group", "identity_space"),
            window=WEEK.window,
            prior_window=PRIOR.window if prior else None,
            computed_at=NOW,
            correlation_id="corr-reach-variant",
            filters={"marketplace_key": US, **(filters or {})},
        ),
        MeasurementTooling(queries=queries, publications=stub, periods=served),
    )
    return response, queries


def _refusal(metric: str, served: _Served, **options: object) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        _chat_reach(metric, served, **options)  # type: ignore[arg-type]
    error = caught.value
    assert str(error.code) == "ADLY-MEASUREMENT-0006"
    assert error.context["finding_code"] == "PACK_VARIANT_MISMATCH"
    return error


def test_chat_reads_a_touch_only_period_from_the_rows_carrying_each_figure() -> None:
    _response, queries = _chat_reach("shoppers_reached", _Served(series=TOUCH_ONLY))
    assert {request.plan.logical.filters["row_kind"] for request in queries.seen} == {"exposure"}
    _response, outcome = _chat_reach("purchasers_in_window", _Served(series=TOUCH_ONLY))
    assert {request.plan.logical.filters["row_kind"] for request in outcome.seen} == {"outcome"}
    # Control: the base series reads its `combined` rows.
    _response, base = _chat_reach("shoppers_reached", _Served())
    assert {request.plan.logical.filters["row_kind"] for request in base.seen} == {"combined"}


def test_chat_refuses_a_mixed_period_a_cross_variant_comparison_and_a_split_rate() -> None:
    mixed = _refusal("shoppers_reached", _Served(series=_series({WEEK: (None, "touch_only")})))
    assert "published under the base pack and variant touch_only" in mixed.description
    across = _refusal(
        "shoppers_reached",
        _Served(series=_series({WEEK: ("touch_only",), PRIOR: (None,)})),
        prior=True,
    )
    assert "this period by variant touch_only" in across.description
    split = _refusal(
        "purchaser_rate", _Served(series=TOUCH_ONLY), filters={"identity_space": "account"}
    )
    assert "two kinds of rows" in split.description
    # Controls: the same comparison inside one series plans, and the rate's numerator alone,
    # on the one branch carrying it, plans on the same touch-only period.
    _response, same = _chat_reach("impressions_in_window", _Served(series=TOUCH_ONLY), prior=True)
    assert {request.plan.logical.filters["row_kind"] for request in same.seen} == {"exposure"}
    _response, part = _chat_reach(
        "purchasers_in_window", _Served(series=TOUCH_ONLY), filters={"identity_space": "account"}
    )
    assert {request.plan.logical.filters["row_kind"] for request in part.seen} == {"outcome"}


# --- frequency (packs 6 and 7) ------------------------------------------------------------

MONTH = CalendarPeriod(date(2026, 9, 1), date(2026, 9, 30), TimeGrain.MONTH)
QUARTER = CalendarPeriod(date(2026, 7, 1), date(2026, 9, 30), TimeGrain.QUARTER)
PLATFORM = "amc-exposure-frequency-platform"
CHANNEL = "amc-exposure-frequency-channel"
_BUCKET_VALUES = {
    # measure: per-bucket value on account IDs; device or browser IDs read half
    "shoppers": 10_000,
    "purchasers": 500,
    "revenue_incl_halo": 2_000,
    "dsp_window_spend": 600,
    "sponsored_window_spend": 200,
}


@dataclass
class FrequencyQueries:
    """Reach's per-cell answers plus pack 6 per bucket and pack 7 per ad product and bucket."""

    reach: ReachQueries = field(default_factory=ReachQueries)
    missing: tuple[str, ...] = ()
    channels: tuple[str, ...] = ("sponsored_products", "sponsored_brands")
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        plan = request.plan.logical
        if plan.fact != "fct_exposure_frequency":
            return self.reach.run(request)
        self.seen.append(request)
        (measure,) = plan.measures
        scheme = plan.filters["bucket_scheme"]
        buckets = ("1", "2", "3", "4_6", "7_9", "10_plus") if scheme == "coarse" else FINE
        products = self.channels if plan.filters["grain_set"] == "channel" else (None,)
        rows: list[QueryRow] = []
        for product in products:
            for bucket in buckets:
                for space in ("account", "device_or_browser"):
                    row: dict[str, str | int | Decimal | None] = {
                        "marketplace_key": plan.filters["marketplace_key"],
                        "ad_product_key": product,
                        "frequency_bucket": bucket,
                        "identity_space": space,
                    }
                    for column in plan.carried_columns:
                        row[column] = "USD"
                    base = _BUCKET_VALUES.get(measure, 1)
                    value = base if space == "account" else base // 2
                    row[measure_alias(0)] = None if bucket in self.missing else Decimal(value)
                    rows.append(
                        {
                            key: row[key]
                            for key in (*plan.group_by, *plan.carried_columns, measure_alias(0))
                        }
                    )
        return QueryResult(
            rows=tuple(rows), bytes_scanned=1_024, truncated=False, suppression=Suppression(False)
        )


FINE = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10_plus")


@dataclass
class _FrequencyServed:
    """Each contract's served periods and, for a variant, the series holding them (plan D9)."""

    held: dict[str, dict[CalendarPeriod, tuple[str | None, ...]]]

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        wanted = {contract.contract_id for contract in request.contracts}
        published = {
            (contract, period)
            for contract, periods in self.held.items()
            if contract in wanted
            for period in periods
        }
        return ServedPeriods(
            frozenset(published),
            dict.fromkeys(wanted, 16),
            {
                (contract, period): series
                for contract, periods in self.held.items()
                if contract in wanted
                for period, series in periods.items()
            },
        )


@dataclass
class _Rosters:
    campaigns: int = 6
    seen: list[RosterRequest] = field(default_factory=list[RosterRequest])

    def roster(self, request: RosterRequest) -> Roster:
        self.seen.append(request)
        if request.kind is RosterKind.CAMPAIGN:
            entries = tuple(
                RosterEntry(f"campaign-{index}", f"Campaign {index}")
                for index in range(self.campaigns)
            )
        else:
            entries = (
                RosterEntry("sponsored_products", "Sponsored Products"),
                RosterEntry("sponsored_display", "Sponsored Display"),
            )
        return Roster(
            request.kind, request.period, request.marketplace_key, request.platform_key, entries
        )


def _frequency_renderer(  # noqa: PLR0913 -- the served periods, queries and two options
    served: _FrequencyServed,
    queries: FrequencyQueries | None = None,
    *,
    rosters: _Rosters | None = None,
    instance: AmcInstanceKind = AmcInstanceKind.STANDARD,
) -> tuple[ReachArtifactRenderer, FrequencyQueries]:
    queries = queries or FrequencyQueries()
    stub = StubPublications()
    renderer = replace(
        _renderer(ReachQueries(), instance=instance),
        tooling=MeasurementTooling(
            queries=queries, publications=stub, periods=served, rosters=rosters or _Rosters()
        ),
        ad_product_labels={"sponsored_brands": "Sponsored Brands"},
    )
    return renderer, queries


def _both(period: CalendarPeriod, series: tuple[str | None, ...] = (None,)) -> _FrequencyServed:
    return _FrequencyServed(
        {
            EXPOSURE: {period: (None,), period.previous(): (None,)},
            PLATFORM: {period: series},
            CHANNEL: {period: series},
        }
    )


def _frequency_body(
    preset: TemporalPresetId, period: CalendarPeriod, renderer: ReachArtifactRenderer
) -> dict[str, object]:
    from domain.presentation.reach import reach_artifact_payload  # noqa: PLC0415

    result = renderer.render_reach(ReachRenderInput(preset, period))
    assert result.artifact is not None
    (market,) = cast("list[dict[str, object]]", reach_artifact_payload(result.artifact)["markets"])
    return cast("dict[str, object]", market["frequency"])


def test_the_month_bake_reads_pack_6_buckets_and_pack_7_channels_as_planned() -> None:
    renderer, queries = _frequency_renderer(_both(MONTH), FrequencyQueries(missing=("9",)))
    frequency = _frequency_body(TemporalPresetId.LAST_COMPLETE_MONTH, MONTH, renderer)
    assert (frequency["state"], frequency["bucket_scheme"]) == ("figures", "fine")
    buckets = cast("list[dict[str, Any]]", frequency["buckets"])
    assert buckets[0]["purchaser_rate"]["display_text"] == "5.00%"  # 500 / 10,000, account IDs
    # 2,000 / (600 + 200), the platform view beside the per-channel table.
    assert buckets[0]["platform_return"]["display_text"] == "2.50x"
    assert buckets[8]["purchaser_rate"] == {
        "absence": {"reason": "not_returned", "text": "too few shoppers, or none"}
    }
    # Every plan reads window rows of the series' own scheme, per grain set.
    pins = {
        (str(request.plan.logical.filters["grain_set"]), request.plan.logical.measures[0])
        for request in queries.seen
    }
    assert pins == {
        ("platform", "shoppers"),
        ("platform", "purchasers"),
        ("platform", "revenue_incl_halo"),
        ("platform", "dsp_window_spend"),
        ("platform", "sponsored_window_spend"),
        ("channel", "shoppers"),
    }
    assert {
        (request.plan.logical.filters["row_kind"], request.plan.logical.filters["bucket_scheme"])
        for request in queries.seen
    } == {("window", "fine")}
    # Rows: the spend roster, then an ad product pack 7 holds off it; every cell unavailable.
    table = cast("dict[str, Any]", frequency["per_channel"])
    assert [row["label"] for row in table["rows"]] == [
        "Sponsored Products",
        "Sponsored Display",
        "Sponsored Brands",
    ]
    assert {
        cell["absence"]["reason"] for row in table["rows"] for cell in row["cells"].values()
    } == {"not_available_per_channel"}


def test_a_coarse_series_is_read_with_its_own_buckets_and_named_in_the_axis() -> None:
    renderer, queries = _frequency_renderer(_both(MONTH, ("coarse",)))
    frequency = _frequency_body(TemporalPresetId.LAST_COMPLETE_MONTH, MONTH, renderer)
    assert frequency["bucket_scheme"] == "coarse"
    assert "coarse" in str(frequency["axis_label"])
    assert [item["bucket"] for item in cast("list[dict[str, Any]]", frequency["buckets"])] == [
        "1",
        "2",
        "3",
        "4_6",
        "7_9",
        "10_plus",
    ]
    assert {request.plan.logical.filters["bucket_scheme"] for request in queries.seen} == {"coarse"}


def test_a_period_fine_and_coarse_both_hold_bakes_nothing() -> None:
    renderer, _queries = _frequency_renderer(_both(MONTH, (None, "coarse")))
    result = renderer.render_reach(ReachRenderInput(TemporalPresetId.LAST_COMPLETE_MONTH, MONTH))
    assert (result.artifact, result.publication_identities) == (None, ())
    # Control: one series bakes.
    one, _ = _frequency_renderer(_both(MONTH, ("coarse",)))
    assert (
        one.render_reach(ReachRenderInput(TemporalPresetId.LAST_COMPLETE_MONTH, MONTH)).artifact
        is not None
    )


def test_frequency_reads_pending_until_pack_6_lands_and_needs_four_campaigns() -> None:
    served = _FrequencyServed({EXPOSURE: {MONTH: (None,), MONTH.previous(): (None,)}})
    renderer, queries = _frequency_renderer(served)
    pending = _frequency_body(TemporalPresetId.LAST_COMPLETE_MONTH, MONTH, renderer)
    assert (pending["state"], pending["expected_after"]) == ("pending", "2026-10-16")
    assert queries.seen == []
    few, _ = _frequency_renderer(_both(MONTH), rosters=_Rosters(campaigns=3))
    needs = _frequency_body(TemporalPresetId.LAST_COMPLETE_MONTH, MONTH, few)
    assert (needs["state"], needs["text"]) == (
        "needs_campaigns",
        "Needs at least 4 active campaigns",
    )


def test_the_quarter_preset_resolves_on_pack_6_and_bakes_frequency_only() -> None:
    renderer, _queries = _frequency_renderer(
        _FrequencyServed({PLATFORM: {QUARTER: (None,)}, CHANNEL: {QUARTER: (None,)}})
    )
    source = RetainedReachPresetArtifactSource(
        temporal_policy=POLICY,
        compiler=_Compiler(renderer),
        served=lambda: frozenset({(PLATFORM, QUARTER)}),
        cadence_lag_days=16,
        pointers=cast("object", _Pointers()),  # type: ignore[arg-type]
        clock=lambda: NOW,
        instance=AmcInstanceKind.STANDARD,
    )
    assert source.resolve(TemporalPresetId.LAST_COMPLETE_QUARTER) == ReachRenderInput(
        TemporalPresetId.LAST_COMPLETE_QUARTER, QUARTER
    )
    frequency = _frequency_body(TemporalPresetId.LAST_COMPLETE_QUARTER, QUARTER, renderer)
    assert frequency["state"] == "figures"
    # Control: the week preset doesn't bind frequency, so pack 6 doesn't anchor it.
    assert source.resolve(TemporalPresetId.LAST_COMPLETE_WEEK) is None


def test_an_instant_access_instance_still_bakes_sponsored_side_frequency() -> None:
    renderer, queries = _frequency_renderer(
        _FrequencyServed({PLATFORM: {MONTH: (None,)}, CHANNEL: {MONTH: (None,)}}),
        instance=AmcInstanceKind.INSTANT_ACCESS,
    )
    result = renderer.render_reach(ReachRenderInput(TemporalPresetId.LAST_COMPLETE_MONTH, MONTH))
    assert result.artifact is not None
    from domain.presentation.reach import reach_artifact_payload  # noqa: PLC0415

    (market,) = cast("list[dict[str, Any]]", reach_artifact_payload(result.artifact)["markets"])
    assert (market["overlap"]["state"], market["frequency"]["state"]) == ("needs_dsp", "figures")
    assert queries.seen
    assert any(
        item.startswith("amc-instance.v1:instant_access:") for item in result.publication_identities
    )
