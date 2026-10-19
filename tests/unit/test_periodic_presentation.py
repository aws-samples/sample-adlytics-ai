# SPDX-License-Identifier: MIT-0
"""Page times, calendar presets, `resolve_calendar_preset`, designed texts, chat and transport v2.

ADR-100, design §4.7 and §13, slice S0.2. Proven on the synthetic periodic fact of
`periodic_support` (packs 5 to 7 land 16 days after period end; packs 2 and 16 and daily spend
land 2 days after). Every date is explicit: no test reads a clock.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from periodic_support import AUGUST, LAG_DAYS, SEPTEMBER, WEEK_38, synthetic_catalog

from adapters.jobs.presentation_validation.loader import (
    PRESENTATION_PAGE_SCHEMAS_PATH,
    load_page_schemas,
    load_v2_page_schemas,
)
from adapters.outbound.attribution_models import load_attribution_models
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import Catalog
from domain.conversation.periodic import ChatPeriodRequest, resolve_chat_period
from domain.conversation.temporal import reference_periods
from domain.presentation.calendar_presets import (
    AbsenceSubject,
    DailyOperandCoverage,
    PeriodicContract,
    PresetSection,
    ResolveCalendarPresetRequest,
    SectionPeriodRequest,
    SectionPeriodStatus,
    declared_period_absence_text,
    resolve_calendar_preset,
    section_period_state,
)
from domain.presentation.page_schemas import (
    AnalyticalPageSchema,
    Cardinality,
    MetricRole,
    PageGrain,
    PageMetric,
    PageMetricVariant,
    PageMode,
    PageSchemaError,
    PageSchemaSet,
    PageSection,
    PageTime,
    PageValidationContext,
    PresentationArtifactDeclaration,
    PresentationPageMode,
    PresentationPageSchema,
    PresentationPageSchemaSet,
    PresentationTemporalPolicy,
    PresentationVintagePolicy,
    TemporalComparisonPolicy,
    TemporalPresetId,
    validate_page_schemas,
)
from domain.presentation.temporal import (
    TemporalSelection,
    TemporalSelectionKind,
    temporal_preset_label,
)
from domain.presentation.temporal_v2 import (
    CalendarResolution,
    SelectionRefusalReason,
    TemporalSelectionRefusalError,
    VintageOption,
    calendar_resolution_fields,
    decode_selection_v2,
    no_complete_period_result,
    preset_entries_v2,
    vintage_entries_v2,
)
from domain.semantics.calendar_periods import CalendarPeriod, period_containing
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import DomainError
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-100", "ADR-070")]

ROOT = Path(__file__).resolve().parents[2]
WEEK = TemporalPresetId.LAST_COMPLETE_WEEK
MONTH = TemporalPresetId.LAST_COMPLETE_MONTH
QUARTER = TemporalPresetId.LAST_COMPLETE_QUARTER
SHOPPERS = "synthetic_shoppers_reached"
COST = "synthetic_cost_per_shopper"
PACK_2 = PeriodicContract("amc-new-to-brand-buyers", 2)
PACK_5 = PeriodicContract("amc-exposure-groups", LAG_DAYS)
PACK_6 = PeriodicContract("amc-exposure-frequency", LAG_DAYS)
CONTRACT_16 = PeriodicContract("brand-analytics-repeat-purchase", 2)


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return synthetic_catalog(load_catalog(ROOT))


@pytest.fixture(scope="module")
def context(catalog: Catalog) -> PageValidationContext:
    return PageValidationContext(catalog, load_attribution_models(ROOT))


def _variants() -> tuple[PageMetricVariant, ...]:
    return tuple(PageMetricVariant(grain, (("time_grain", grain),)) for grain in ("week", "month"))


def _slot(name: str, *, declared: bool = True) -> PageMetric:
    return PageMetric(
        name,
        (MetricRole.SUMMARY,),
        variants=_variants() if name != "ad_spend" else (),
        declared_periods=(WEEK, MONTH) if declared else (),
    )


def _v1_scorecard(*metrics: PageMetric) -> AnalyticalPageSchema:
    return AnalyticalPageSchema(
        page_id="scorecard",
        display_name="Scorecard",
        route="/scorecard/",
        purpose="Synthetic ADR-100 page.",
        primary_job="Prove the v1 period checks per page time.",
        mode=PageMode.SINGLE_BASIS,
        boundary_required=False,
        grain=PageGrain(PageTime.PERIOD, ("marketplace",)),
        preferred_window_duration_days=7,
        metrics=metrics,
        sections=(PageSection("network-card", Cardinality.EXACTLY_ONE),),
    )


def _policy(**overrides: object) -> PresentationTemporalPolicy:
    fields: dict[str, object] = {
        "candidate_presets": (
            TemporalPresetId.SEVEN_DAYS,
            TemporalPresetId.THIRTY_DAYS,
            WEEK,
            MONTH,
        ),
        "default_preset": TemporalPresetId.SEVEN_DAYS,
        "custom_enabled": False,
        "custom_min_duration_days": 1,
        "custom_max_duration_days": 90,
        "comparison_policy": TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR,
        "display_grains": (PageTime.PERIOD,),
    }
    fields.update(overrides)
    return PresentationTemporalPolicy(**fields)  # type: ignore[arg-type]


def _v2_page(
    page_id: str,
    policy: PresentationTemporalPolicy | PresentationVintagePolicy,
    sections: tuple[PageSection, ...],
) -> PresentationPageSchema:
    return PresentationPageSchema(
        page_id=page_id,
        display_name=page_id.title(),
        route=f"/{page_id}/",
        purpose="Synthetic ADR-100 page.",
        primary_job="Prove the section rule.",
        mode=PresentationPageMode.SELECTABLE,
        boundary_required=False,
        time_policy=policy,
        artifacts=(PresentationArtifactDeclaration("main", "SyntheticArtifact", 1, ("complete",)),),
        visuals=(),
        sections=sections,
        actions=(),
        compatibility_routes=(),
    )


SCORECARD_SECTIONS = (
    PageSection("network-card", Cardinality.EXACTLY_ONE, metrics=("ad_spend",)),
    PageSection(
        "new-to-brand", Cardinality.EXACTLY_ONE, declared_periods=(WEEK, MONTH), metrics=(COST,)
    ),
    PageSection(
        "reach-and-funnel-summary",
        Cardinality.EXACTLY_ONE,
        declared_periods=(WEEK, MONTH),
        period_anchor=False,
        metrics=(SHOPPERS,),
    ),
)


def _sets(
    v1_pages: tuple[AnalyticalPageSchema, ...], v2_pages: tuple[PresentationPageSchema, ...]
) -> tuple[PageSchemaSet, PresentationPageSchemaSet]:
    v1 = load_page_schemas(ROOT)
    v2 = load_v2_page_schemas(ROOT)
    # A synthetic page replaces the committed entry of the same id (the Scorecard's, since
    # UI slice 9a), so each case checks its own declaration.
    v1_ids = {page.page_id for page in v1_pages}
    v2_ids = {page.page_id for page in v2_pages}
    return (
        replace(v1, pages=(*(page for page in v1.pages if page.page_id not in v1_ids), *v1_pages)),
        replace(
            v2,
            temporal_control_version=2,
            pages=(*(page for page in v2.pages if page.page_id not in v2_ids), *v2_pages),
        ),
    )


# --- v1 period checks per page time (review ARCH-R4-2 (a)) --------------------------------------


def test_the_scorecard_v1_entry_passes_with_declared_period_slots(
    context: PageValidationContext,
) -> None:
    v1, v2 = _sets(
        (_v1_scorecard(_slot("ad_spend", declared=False), _slot(SHOPPERS), _slot(COST)),),
        (_v2_page("scorecard", _policy(), SCORECARD_SECTIONS),),
    )
    assert validate_page_schemas(v1, context, v2) == ()


def test_the_same_shopper_slot_without_declared_periods_fails_check_2(
    context: PageValidationContext,
) -> None:
    v1, v2 = _sets(
        (_v1_scorecard(_slot("ad_spend", declared=False), _slot(SHOPPERS, declared=False)),),
        (_v2_page("scorecard", _policy(), SCORECARD_SECTIONS[:1]),),
    )
    messages = {finding.message for finding in validate_page_schemas(v1, context, v2)}
    assert f"period evidence cannot aggregate {SHOPPERS}." in messages


def test_a_declared_periods_slot_without_the_time_grain_selector_fails(
    context: PageValidationContext,
) -> None:
    v1, v2 = _sets(
        (_v1_scorecard(_slot("ad_spend")),),
        (
            _v2_page(
                "scorecard",
                _policy(),
                (
                    PageSection(
                        "network-card",
                        Cardinality.EXACTLY_ONE,
                        declared_periods=(WEEK, MONTH),
                        metrics=("ad_spend",),
                    ),
                ),
            ),
        ),
    )
    messages = {finding.message for finding in validate_page_schemas(v1, context, v2)}
    assert "ad_spend declares declared_periods without the time_grain exact selector." in messages
    assert "ad_spend declares no period_grains for last_complete_month, last_complete_week." in (
        messages
    )


def test_a_declared_periods_slot_must_be_bound_in_matching_v2_sections(
    context: PageValidationContext,
) -> None:
    unbound = (SCORECARD_SECTIONS[0], replace(SCORECARD_SECTIONS[2], declared_periods=(MONTH,)))
    v1, v2 = _sets(
        (_v1_scorecard(_slot("ad_spend", declared=False), _slot(SHOPPERS)),),
        (_v2_page("scorecard", _policy(), unbound),),
    )
    messages = {finding.message for finding in validate_page_schemas(v1, context, v2)}
    assert (
        f"{SHOPPERS} declares declared_periods, so it must be bound only in v2 sections with the "
        "same declared_periods."
    ) in messages


def test_a_declared_period_page_passes_and_an_as_of_vintage_page_needs_a_vintage_fact(
    context: PageValidationContext,
) -> None:
    repeat = replace(
        _v1_scorecard(_slot(SHOPPERS, declared=False)),
        page_id="repeat-purchase",
        route="/repeat-purchase/",
        grain=PageGrain(PageTime.DECLARED_PERIOD, ("marketplace",)),
    )
    monthly = _policy(
        candidate_presets=(MONTH,),
        default_preset=MONTH,
        comparison_policy=TemporalComparisonPolicy.NONE,
        display_grains=(PageTime.DECLARED_PERIOD,),
    )
    sections = (
        PageSection(
            "repeat-purchase",
            Cardinality.EXACTLY_ONE,
            declared_periods=(MONTH,),
            metrics=(SHOPPERS,),
        ),
    )
    v1, v2 = _sets((repeat,), (_v2_page("repeat-purchase", monthly, sections),))
    assert validate_page_schemas(v1, context, v2) == ()
    retention = replace(
        repeat,
        page_id="retention",
        route="/retention/",
        grain=PageGrain(PageTime.AS_OF_VINTAGE, ("marketplace",)),
    )
    v1, v2 = _sets(
        (retention,),
        (
            _v2_page(
                "retention",
                PresentationVintagePolicy(),
                (PageSection("cohorts", Cardinality.EXACTLY_ONE),),
            ),
        ),
    )
    messages = {finding.message for finding in validate_page_schemas(v1, context, v2)}
    assert messages == {f"{SHOPPERS} reads no as_of_month vintage."}


def test_a_v2_section_binding_a_periodic_metric_declares_its_periods(
    context: PageValidationContext,
) -> None:
    sections = (PageSection("synthetic-periodic", Cardinality.EXACTLY_ONE, metrics=(SHOPPERS,)),)
    v1, v2 = _sets((), (_v2_page("synthetic-periodic", _policy(), sections),))
    findings = validate_page_schemas(v1, context, v2)
    assert [finding.message for finding in findings] == [
        f"synthetic-periodic/{SHOPPERS}: a section binding a periodic metric declares "
        "declared_periods."
    ]
    with pytest.raises(PageSchemaError, match="last_complete_quarter aren't calendar presets"):
        _v2_page(
            "reach",
            _policy(),
            (PageSection("reach", Cardinality.EXACTLY_ONE, declared_periods=(QUARTER,)),),
        )


# --- Calendar presets, comparisons and version gating -------------------------------------------


def test_calendar_preset_rules_hold_in_the_temporal_policy() -> None:
    with pytest.raises(PageSchemaError, match="prior_calendar_period requires every candidate"):
        _policy(comparison_policy=TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD)
    with pytest.raises(PageSchemaError, match="calendar preset can't require an equal prior"):
        _policy(comparison_policy=TemporalComparisonPolicy.REQUIRED_EQUAL_PRIOR)
    with pytest.raises(PageSchemaError, match="declared_period page offers calendar presets only"):
        _policy(display_grains=(PageTime.DECLARED_PERIOD,))
    with pytest.raises(PageSchemaError, match="as_of_vintage is declared by a vintage_policy"):
        _policy(display_grains=(PageTime.AS_OF_VINTAGE,))
    calendar_only = _policy(
        candidate_presets=(WEEK, MONTH, QUARTER),
        default_preset=MONTH,
        comparison_policy=TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD,
        display_grains=(PageTime.DECLARED_PERIOD,),
    )
    assert calendar_only.calendar_presets == (WEEK, MONTH, QUARTER)
    assert temporal_preset_label(MONTH) == "Latest measured month"


def test_v2_grammar_needs_temporal_control_version_2() -> None:
    committed = load_v2_page_schemas(ROOT)
    # The browser's version-2 decoder landed with UI slice 9a, so the header is version 2.
    assert committed.temporal_control_version == 2
    others = tuple(item for item in committed.pages if item.page_id != "scorecard")
    page = _v2_page("scorecard", _policy(), SCORECARD_SECTIONS)
    with pytest.raises(PageSchemaError, match="scorecard: calendar presets, prior_calendar_period"):
        replace(committed, temporal_control_version=1, pages=(*others, page))
    upgraded = replace(committed, temporal_control_version=2, pages=(*others, page))
    assert upgraded.temporal_control_version == 2
    vintage = _v2_page("retention", PresentationVintagePolicy(), SCORECARD_SECTIONS[:1])
    with pytest.raises(PageSchemaError, match="a vintage page has no temporal_policy"):
        _ = vintage.temporal_policy


def test_the_loader_reads_vintage_policy_and_section_fields(tmp_path: Path) -> None:
    text = (ROOT / PRESENTATION_PAGE_SCHEMAS_PATH).read_text()
    path = tmp_path / PRESENTATION_PAGE_SCHEMAS_PATH
    path.parent.mkdir(parents=True)
    path.write_text(text)
    loaded = load_v2_page_schemas(tmp_path)
    retention = next(page for page in loaded.pages if page.page_id == "retention")
    assert retention.vintage_policy == PresentationVintagePolicy()
    path.write_text(
        text.replace("    vintage_policy:\n", "    temporal_policy: {}\n    vintage_policy:\n", 1)
    )
    with pytest.raises(PageSchemaError, match="exactly one of temporal_policy or vintage_policy"):
        load_v2_page_schemas(tmp_path)


def test_version_1_selections_refuse_calendar_presets_and_vintages() -> None:
    with pytest.raises(ValueError, match="calendar preset needs temporal control version 2"):
        TemporalSelection(kind=TemporalSelectionKind.PRESET, preset_id=MONTH)
    with pytest.raises(ValueError, match="vintage selection needs temporal control version 2"):
        TemporalSelection(kind=TemporalSelectionKind.VINTAGE, as_of_month="2026-09")
    assert TemporalSelection(kind=TemporalSelectionKind.PRESET, preset_id=MONTH, version=2)


# --- resolve_calendar_preset (design §13 calendar-preset cases) ---------------------------------


def _weeks_published(contract: PeriodicContract, as_of: date) -> set[tuple[str, CalendarPeriod]]:
    """Every week whose end plus the contract's lag is on or before `as_of`."""
    published: set[tuple[str, CalendarPeriod]] = set()
    week = period_containing(TimeGrain.WEEK, date(2026, 7, 6))
    while week.end + timedelta(days=contract.cadence_lag_days) <= as_of:
        published.add((contract.contract, week))
        week = period_containing(TimeGrain.WEEK, week.end + timedelta(days=1))
    return published


def test_day_5_after_a_week_reach_shows_the_latest_pack_5_week_and_the_scorecard_its_own() -> None:
    as_of = date(2026, 10, 9)  # day 5 after the week of 28 Sep to 4 Oct ends
    publications = frozenset(_weeks_published(PACK_5, as_of) | _weeks_published(PACK_2, as_of))
    reach = resolve_calendar_preset(
        ResolveCalendarPresetRequest(
            WEEK,
            as_of,
            (
                PresetSection("overlap", True, (PACK_5,)),
                PresetSection("funnel", True, (PACK_5,)),
                PresetSection("frequency", False, (PACK_6,)),
            ),
            publications,
        )
    )
    assert reach.period == WEEK_38
    assert reach.label == "Week of 14\u201320 Sep 2026"
    spend = DailyOperandCoverage(
        "amc-dsp-impressions", (DateRange(date(2026, 7, 1), date(2026, 10, 7)),)
    )
    summary = PresetSection("reach-and-funnel-summary", False, (PACK_5,))
    scorecard = resolve_calendar_preset(
        ResolveCalendarPresetRequest(
            WEEK,
            as_of,
            (
                PresetSection("network-card", True),
                PresetSection("channel-table", True),
                PresetSection("new-to-brand", True, (PACK_2,)),
                summary,
            ),
            publications,
            (spend,),
        )
    )
    week = CalendarPeriod(date(2026, 9, 28), date(2026, 10, 4), TimeGrain.WEEK)
    assert scorecard.period == week
    state = section_period_state(SectionPeriodRequest(summary, week, publications, as_of))
    assert state.status is SectionPeriodStatus.PENDING
    assert (
        state.text == "Figures for Week of 28 Sep\u20134 Oct 2026 are expected after 20 Oct 2026."
    )


def test_scorecard_month_resolves_to_september_and_to_august_without_pack_2() -> None:
    """AC-PG.1: anchors list periodic contracts only; daily spend enters through coverage."""
    as_of = date(2026, 10, 10)
    spend = DailyOperandCoverage(
        "amc-dsp-impressions", (DateRange(date(2026, 8, 1), date(2026, 10, 8)),)
    )
    sections = (
        PresetSection("network-card", True),
        PresetSection("channel-table", True),
        PresetSection("new-to-brand", True, (PACK_2,)),
        PresetSection("reach-and-funnel-summary", False, (PACK_5,)),
    )
    both = frozenset({(PACK_2.contract, SEPTEMBER), (PACK_2.contract, AUGUST)})
    request = ResolveCalendarPresetRequest(MONTH, as_of, sections, both, (spend,))
    assert resolve_calendar_preset(request).period == SEPTEMBER
    august_only = replace(request, publications=frozenset({(PACK_2.contract, AUGUST)}))
    assert resolve_calendar_preset(august_only).period == AUGUST
    # Daily coverage decides too: a missing September day holds the page at August.
    gap = DailyOperandCoverage(
        "amc-dsp-impressions",
        (
            DateRange(date(2026, 8, 1), date(2026, 9, 14)),
            DateRange(date(2026, 9, 16), date(2026, 10, 8)),
        ),
    )
    assert resolve_calendar_preset(replace(request, daily_coverage=(gap,))).period == AUGUST


def test_repeat_purchase_month_resolves_to_the_latest_report_or_none_with_its_text() -> None:
    sections = (PresetSection("repeat-purchase", True, (CONTRACT_16,)),)
    as_of = date(2026, 10, 10)
    landed = ResolveCalendarPresetRequest(
        MONTH, as_of, sections, frozenset({(CONTRACT_16.contract, AUGUST)})
    )
    assert resolve_calendar_preset(landed).period == AUGUST
    nothing = resolve_calendar_preset(replace(landed, publications=frozenset()))
    assert nothing.period is None
    assert nothing.none_text == (
        "No complete month has been measured yet. The first is expected after 2 Oct 2026."
    )
    assert no_complete_period_result(nothing) == {
        "kind": "window_unavailable",
        "reason_code": "no_complete_period_measured",
        "expected_after": "2026-10-02",
        "message": nothing.none_text,
    }
    too_old = CalendarPeriod(date(2025, 8, 1), date(2025, 8, 31), TimeGrain.MONTH)
    stale = replace(landed, publications=frozenset({(CONTRACT_16.contract, too_old)}))
    assert resolve_calendar_preset(stale).period is None  # beyond the 13-month look-back


def test_pack_5_published_and_pack_6_not_leaves_frequency_pending_then_missing() -> None:
    sections = (
        PresetSection("overlap", True, (PACK_5,)),
        PresetSection("funnel", True, (PACK_5,)),
        PresetSection("frequency", False, (PACK_6,)),
    )
    publications = frozenset({(PACK_5.contract, SEPTEMBER), (PACK_6.contract, AUGUST)})
    as_of = date(2026, 10, 16)
    resolved = resolve_calendar_preset(
        ResolveCalendarPresetRequest(MONTH, as_of, sections, publications)
    )
    assert resolved.period == SEPTEMBER
    frequency = sections[2]
    pending = section_period_state(SectionPeriodRequest(frequency, SEPTEMBER, publications, as_of))
    assert pending.text == "Figures for September 2026 are expected after 16 Oct 2026."
    late = section_period_state(
        SectionPeriodRequest(frequency, SEPTEMBER, publications, date(2026, 10, 17))
    )
    assert late.status is SectionPeriodStatus.MISSING
    assert late.text == "Figures for September 2026 didn't arrive."
    overlap = section_period_state(
        SectionPeriodRequest(sections[0], SEPTEMBER, publications, as_of)
    )
    assert overlap.status is SectionPeriodStatus.PUBLISHED


# --- Designed absence text ------------------------------------------------------------------------


def test_absence_text_is_built_from_declared_grains_and_server_labels() -> None:
    frequency = declared_period_absence_text(
        AbsenceSubject("Frequency", plural=False),
        (TimeGrain.MONTH, TimeGrain.QUARTER),
        (QUARTER, MONTH),
    )
    assert frequency == (
        "Frequency is measured for complete months and quarters. "
        "Choose Latest measured month to see it."
    )
    shoppers = declared_period_absence_text(
        AbsenceSubject("Shopper counts", plural=True),
        (TimeGrain.WEEK, TimeGrain.MONTH),
        (MONTH, WEEK),
    )
    assert shoppers == (
        "Shopper counts are measured for complete weeks and months. "
        "Choose Latest measured week to see them."
    )


# --- Chat resolution (ADR-100 point 9) ------------------------------------------------------------


def _chat(
    label: str, today: date, publications: frozenset[tuple[str, CalendarPeriod]]
) -> ChatPeriodRequest:
    window = next(period.window for period in reference_periods(today) if period.label == label)
    return ChatPeriodRequest(
        SHOPPERS,
        (TimeGrain.WEEK, TimeGrain.MONTH, TimeGrain.QUARTER),
        window,
        today,
        (PACK_5,),
        publications,
    )


def test_last_month_names_the_month_or_the_latest_measured_one() -> None:
    today = date(2026, 10, 10)
    landed = frozenset({(PACK_5.contract, SEPTEMBER), (PACK_5.contract, AUGUST)})
    answer = resolve_chat_period(_chat("last month", today, landed))
    assert (answer.period, answer.sentence, answer.substituted) == (
        SEPTEMBER,
        "Figures are for September 2026.",
        False,
    )
    pending = resolve_chat_period(
        _chat("last month", today, frozenset({(PACK_5.contract, AUGUST)}))
    )
    assert pending.period == AUGUST
    assert pending.substituted is True
    assert pending.sentence == (
        "September 2026 hasn't been measured yet, so this answer uses the latest measured month, "
        "August 2026."
    )


def test_the_last_7_days_are_refused_with_the_latest_measured_week() -> None:
    today = date(2026, 10, 9)
    publications = frozenset(_weeks_published(PACK_5, today))
    with pytest.raises(DomainError) as refused:
        resolve_chat_period(_chat("the 7 days ending yesterday", today, publications))
    assert refused.value.context["reason"] == "period_not_complete"
    assert refused.value.context["nearest_period_label"] == "Week of 14\u201320 Sep 2026"
    assert refused.value.description.endswith(
        "The latest measured week is Week of 14\u201320 Sep 2026."
    )


# --- Temporal transport v2, server half -----------------------------------------------------------


def test_the_v2_decoder_refuses_what_the_page_does_not_declare() -> None:
    scorecard = _v2_page("scorecard", _policy(), SCORECARD_SECTIONS)
    retention = _v2_page("retention", PresentationVintagePolicy(), SCORECARD_SECTIONS[:1])
    accepted = decode_selection_v2(
        {"kind": "preset", "preset_id": "last_complete_month"}, scorecard
    )
    assert (accepted.preset_id, accepted.version) == (MONTH, 2)
    cases = (
        (
            {"kind": "preset", "preset_id": "last_complete_quarter"},
            scorecard,
            SelectionRefusalReason.UNDECLARED_CALENDAR_PRESET,
        ),
        (
            {"kind": "vintage", "as_of_month": "2026-09"},
            scorecard,
            SelectionRefusalReason.VINTAGE_ON_PRESET_PAGE,
        ),
        (
            {"kind": "preset", "preset_id": "7d"},
            retention,
            SelectionRefusalReason.PRESET_ON_VINTAGE_PAGE,
        ),
        (
            {"kind": "vintage", "as_of_month": "2026-08"},
            retention,
            SelectionRefusalReason.VINTAGE_NOT_OFFERED,
        ),
        ({"kind": "preset", "preset_id": "90d"}, scorecard, SelectionRefusalReason.INVALID),
    )
    for raw, page, reason in cases:
        with pytest.raises(TemporalSelectionRefusalError) as refused:
            decode_selection_v2(raw, page, offered_vintages=("2026-09",))
        assert refused.value.reason is reason
    vintage = decode_selection_v2(
        {"kind": "vintage", "as_of_month": "2026-09"}, retention, offered_vintages=("2026-09",)
    )
    assert vintage.as_of_month == "2026-09"


def test_the_v2_encoder_names_the_period_and_the_calendar_comparison() -> None:
    fields = calendar_resolution_fields(CalendarResolution(MONTH, _policy(), SEPTEMBER))
    assert fields["resolved"] == {
        "window": {"start": "2026-09-01", "end": "2026-09-30"},
        "selection": {"kind": "preset", "preset_id": "last_complete_month"},
        "display_grain": "month",
        "period_label": "September 2026",
        "as_of_month": None,
    }
    assert fields["comparison"] == {
        "policy": "optional_equal_prior",
        "status": "not_applicable",
        "reason_code": "calendar_period_selected",
    }
    calendar = _policy(
        candidate_presets=(MONTH,),
        default_preset=MONTH,
        comparison_policy=TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD,
    )
    prior = calendar_resolution_fields(CalendarResolution(MONTH, calendar, SEPTEMBER, True))
    assert prior["comparison"]["prior_period_label"] == "August 2026"  # type: ignore[index]
    assert [entry["id"] for entry in preset_entries_v2(_policy())] == [
        "7d",
        "30d",
        "last_complete_week",
        "last_complete_month",
    ]
    options = tuple(VintageOption(f"2025-{month:02d}", f"M{month}", True) for month in range(1, 13))
    entries = vintage_entries_v2(
        (*options, VintageOption("2026-01", "Jan", True), VintageOption("2026-02", "Feb", False))
    )
    assert len(entries) == 13
    assert entries[0] == {"as_of_month": "2026-02", "label": "Feb", "availability": "unavailable"}
