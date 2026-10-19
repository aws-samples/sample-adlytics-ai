# SPDX-License-Identifier: MIT-0
"""`ReachAndFunnelArtifact` v1: overlap and funnel from one exposure-groups delivery (pack 5).

Every figure is fictional. Shares, rates and the index are computed inside one delivery (D-3), so a
`not_returned` cell reads "not available" rather than a share of the cells that came back.
"""

from __future__ import annotations

import copy
import json
import pathlib
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any, cast

import pytest

from domain.presentation.insights import (
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    SaturationPolicy,
)
from domain.presentation.page_schemas import TemporalPresetId
from domain.presentation.preset_artifacts import canonical_preset_artifact_json
from domain.presentation.reach import (
    BOUNDARY,
    BUCKET_SCHEMES,
    CAVEAT_BLOCK,
    CURRENCY_MISMATCH_TEXT,
    FREQUENCY_ABSENT_TEXT,
    FREQUENCY_CAVEAT,
    FUNNEL_ABSENT_TEXT,
    NEEDS_CAMPAIGNS_TEXT,
    NEEDS_DSP_TEXT,
    NO_SPEND_TEXT,
    NOT_AVAILABLE_PER_CHANNEL_REASON,
    NOT_AVAILABLE_PER_CHANNEL_TEXT,
    NOT_RETURNED_TEXT,
    OVERLAP_ABSENT_TEXT,
    SCHEME_AXIS_LABELS,
    ExposureCell,
    FrequencyCell,
    FrequencyChannel,
    FrequencyDelivery,
    FrequencySection,
    FrequencySlotRequest,
    ReachAbsence,
    ReachAbsenceReason,
    ReachAndFunnelArtifact,
    ReachDelivery,
    ReachMarket,
    ReachOutcome,
    ReachSections,
    ReachSectionState,
    ReachSlotRequest,
    ReachValue,
    ReachValueUnit,
    SectionsRequest,
    build_sections,
    frequency_saturation_slot,
    full_funnel_association_slot,
    reach_artifact_payload,
    reach_overlap_slot,
    require_reach_artifact_payload,
    return_by_frequency,
    saturated_bucket,
    share_above,
)
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.time import TimeGrain

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-101", "ADR-102")]

WEEK = CalendarPeriod(date(2026, 9, 14), date(2026, 9, 20), TimeGrain.WEEK)
PRIOR = WEEK.previous()
MONTH = CalendarPeriod(date(2026, 9, 1), date(2026, 9, 30), TimeGrain.MONTH)
QUARTER = CalendarPeriod(date(2026, 7, 1), date(2026, 9, 30), TimeGrain.QUARTER)
REVISION = "a" * 64
_GRID = {
    # (group, space): (shoppers, searchers, purchasers, sales)
    ("dsp_only", "account"): (2000, 200, 100, "3000"),
    ("dsp_only", "device_or_browser"): (1000, 50, 10, "250"),
    ("sponsored_only", "account"): (5000, 600, 250, "8000"),
    ("sponsored_only", "device_or_browser"): (2000, 100, 20, "500"),
    ("dsp_and_sponsored", "account"): (3000, 450, 300, "10500"),
    ("dsp_and_sponsored", "device_or_browser"): (500, 40, 5, "100"),
}


def _delivery(
    period: CalendarPeriod = WEEK, missing: tuple[tuple[str, str], ...] = ()
) -> ReachDelivery:
    cells: list[ExposureCell] = []
    for (group, space), (shoppers, searchers, purchasers, sales) in _GRID.items():
        if (group, space) in missing:
            cells.append(ExposureCell(group, space, returned=False))
            continue
        cells.append(
            ExposureCell(
                group,
                space,
                returned=True,
                shoppers_reached=Decimal(shoppers),
                search_shoppers=Decimal(searchers),
                purchasers=Decimal(purchasers),
                revenue_incl_halo=Decimal(sales),
                currency_code="USD",
            )
        )
    return ReachDelivery(period, tuple(cells))


POLICY = SaturationPolicy(1000, 30, Decimal("0.02"), Decimal("1.96"))


def _saturation(delivery: FrequencyDelivery | None, *, material: bool = True) -> InsightSlot:
    return frequency_saturation_slot(
        FrequencySlotRequest(delivery, POLICY, material, REVISION, "quiet")
    )


def _figures(
    delivery: ReachDelivery | None = None, prior: ReachDelivery | None = None
) -> ReachSections:
    return build_sections(
        SectionsRequest(TemporalPresetId.LAST_COMPLETE_WEEK, WEEK, delivery or _delivery(), prior)
    )


def test_the_three_overlap_shares_are_computed_inside_one_delivery() -> None:
    sections = _figures()
    assert sections.outcome is ReachOutcome.COMPLETE
    overlap = sections.overlap
    assert overlap is not None
    tiles = {tile.metric: tile.current for tile in overlap.tiles}
    # Saw both over every group's account shoppers: 3000 / 10000.
    assert tiles["overlap_share"] == ReachValue(Decimal("0.3000"), ReachValueUnit.SHARE)
    # Over both identity spaces: 3500 / 13500.
    assert tiles["overlap_share_all_ids"] == ReachValue(Decimal("0.2593"), ReachValueUnit.SHARE)
    assert tiles["dsp_only_reach_share"] == ReachValue(Decimal("0.2000"), ReachValueUnit.SHARE)
    assert overlap.tiles[0].label.startswith("Saw both within 14 days of their first ad")
    assert overlap.subtitle == (
        "Shoppers reached in Week of 14\u201320 Sep 2026, grouped by the ads they saw within 14 "
        "days of their first ad in that week."
    )
    assert overlap.window_notes[1] == "Ads seen before the week began aren't counted."
    groups = {group.exposure_group: group for group in overlap.groups}
    assert groups["dsp_and_sponsored"].all_ids == ReachValue(Decimal(3500), ReachValueUnit.COUNT)


def test_a_not_returned_cell_makes_every_share_needing_it_not_available() -> None:
    """D-3: a sum with a gap is refused; the account-only shares don't need the gap."""
    sections = _figures(_delivery(missing=(("dsp_only", "device_or_browser"),)))
    assert sections.overlap is not None
    tiles = {tile.metric: tile.current for tile in sections.overlap.tiles}
    gap = ReachAbsence(ReachAbsenceReason.NOT_RETURNED, NOT_RETURNED_TEXT)
    assert tiles["overlap_share_all_ids"] == gap
    # Control: the account-ID shares read cells that came back.
    assert tiles["overlap_share"] == ReachValue(Decimal("0.3000"), ReachValueUnit.SHARE)
    groups = {group.exposure_group: group for group in sections.overlap.groups}
    assert groups["dsp_only"].all_ids == gap
    assert groups["dsp_only"].account == ReachValue(Decimal(2000), ReachValueUnit.COUNT)


def test_funnel_rates_read_account_ids_and_carry_the_caveat_block() -> None:
    funnel = _figures().funnel
    assert funnel is not None
    assert funnel.caveat == CAVEAT_BLOCK
    assert funnel.caveat[0] == (
        "This shows association, not cause. Shoppers were not randomly assigned to groups."
    )
    assert funnel.columns[0] == ("search_rate", "Search rate")
    rows = {row.exposure_group: dict(row.figures) for row in funnel.rows}
    both = rows["dsp_and_sponsored"]
    assert both["search_rate"] == ReachValue(Decimal("0.1500"), ReachValueUnit.SHARE)
    assert both["purchaser_rate"] == ReachValue(Decimal("0.1000"), ReachValueUnit.SHARE)
    assert both["revenue_per_exposed_shopper"] == ReachValue(
        Decimal("3.5000"), ReachValueUnit.CURRENCY, "USD"
    )
    # 0.10 over sponsored only's 0.05.
    assert both["purchaser_rate_index_vs_sponsored_only"] == ReachValue(
        Decimal("2.0000"), ReachValueUnit.MULTIPLIER
    )
    assert funnel.index_note == "may understate the difference between groups"


def test_a_missing_sponsored_only_account_cell_leaves_every_index_not_available() -> None:
    funnel = _figures(_delivery(missing=(("sponsored_only", "account"),))).funnel
    assert funnel is not None
    for row in funnel.rows:
        assert dict(row.figures)["purchaser_rate_index_vs_sponsored_only"] == ReachAbsence(
            ReachAbsenceReason.NOT_RETURNED, NOT_RETURNED_TEXT
        )
    # Control: the other groups' own rates still read.
    both = next(row for row in funnel.rows if row.exposure_group == "dsp_and_sponsored")
    assert isinstance(dict(both.figures)["purchaser_rate"], ReachValue)


def test_a_returned_cell_with_no_sales_reads_no_sales_never_zero_currency() -> None:
    cells = tuple(
        ExposureCell(
            cell.exposure_group,
            cell.identity_space,
            True,
            cell.shoppers_reached,
            cell.search_shoppers,
            Decimal(0),
            None,
            None,
        )
        if cell.exposure_group == "dsp_only" and cell.identity_space == "account"
        else cell
        for cell in _delivery().cells
    )
    funnel = _figures(ReachDelivery(WEEK, cells)).funnel
    assert funnel is not None
    row = next(row for row in funnel.rows if row.exposure_group == "dsp_only")
    figure = dict(row.figures)["revenue_per_exposed_shopper"]
    assert isinstance(figure, ReachAbsence)
    assert figure.reason is ReachAbsenceReason.NO_SALES


def test_the_change_block_reads_points_against_the_prior_week() -> None:
    sections = _figures(prior=_delivery(PRIOR, missing=(("dsp_only", "device_or_browser"),)))
    assert sections.overlap is not None
    tiles = {tile.metric: tile for tile in sections.overlap.tiles}
    assert tiles["overlap_share"].change.text == "0.00 points"  # type: ignore[union-attr]
    # The prior all-ID share isn't available, so no change is shown.
    assert isinstance(tiles["overlap_share_all_ids"].change, ReachAbsence)


def _encoded_no_prior_projection(
    preset: TemporalPresetId, period: CalendarPeriod
) -> dict[str, object]:
    delivery = _delivery(period)
    artifact = ReachAndFunnelArtifact(
        preset,
        period,
        (
            ReachMarket(
                "amazon_us",
                build_sections(SectionsRequest(preset, period, delivery)),
                (
                    reach_overlap_slot(ReachSlotRequest(delivery, True, REVISION, "quiet")),
                    full_funnel_association_slot(
                        ReachSlotRequest(delivery, True, REVISION, "quiet")
                    ),
                    _saturation(None),
                ),
            ),
        ),
    )
    payload = reach_artifact_payload(artifact)
    (market,) = cast("list[dict[str, Any]]", payload["markets"])
    overlap = cast("dict[str, Any]", market["overlap"])
    return {
        "grain": cast("dict[str, str]", payload["period"])["grain"],
        "tile_changes": [tile["change"] for tile in overlap["tiles"]],
        "group_changes": [group["change"] for group in overlap["groups"]],
    }


def test_no_prior_week_and_month_encoder_changes_match_the_frontend_contract_fixture() -> None:
    fixture_path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "frontend/test/fixtures/reach-no-prior-encoder-fixtures.json"
    )
    expected = json.loads(fixture_path.read_text())
    assert expected == {
        "week": _encoded_no_prior_projection(TemporalPresetId.LAST_COMPLETE_WEEK, WEEK),
        "month": _encoded_no_prior_projection(TemporalPresetId.LAST_COMPLETE_MONTH, MONTH),
    }


def test_frequency_is_typed_absent_on_a_week_and_pending_on_a_month() -> None:
    week = _figures().frequency
    assert (week.state, week.text) == (
        ReachSectionState.OUTSIDE_DECLARED_PERIODS,
        FREQUENCY_ABSENT_TEXT,
    )
    month = build_sections(
        SectionsRequest(TemporalPresetId.LAST_COMPLETE_MONTH, MONTH, _delivery(MONTH))
    ).frequency
    assert month.state is ReachSectionState.PENDING
    assert month.expected_after == date(2026, 10, 16)
    assert month.text == "Figures for September 2026 are expected after 16 Oct 2026."


def test_the_quarter_preset_binds_frequency_only() -> None:
    sections = build_sections(
        SectionsRequest(TemporalPresetId.LAST_COMPLETE_QUARTER, QUARTER, None)
    )
    assert sections.overlap is not None and sections.funnel is not None
    assert (sections.overlap.text, sections.funnel.text) == (
        OVERLAP_ABSENT_TEXT,
        FUNNEL_ABSENT_TEXT,
    )
    assert sections.frequency.state is ReachSectionState.PENDING


def test_an_instant_access_instance_reads_needs_dsp_and_keeps_the_period() -> None:
    sections = build_sections(
        SectionsRequest(TemporalPresetId.LAST_COMPLETE_WEEK, WEEK, None, dsp_available=False)
    )
    assert sections.outcome is ReachOutcome.COMPLETE
    assert sections.overlap is not None and sections.funnel is not None
    assert sections.overlap.text == sections.funnel.text == NEEDS_DSP_TEXT
    assert NEEDS_DSP_TEXT == "Needs DSP and sponsored ads on one AMC instance"


def test_no_account_cell_is_insufficient_and_no_delivery_is_empty() -> None:
    every = tuple(key for key in _GRID if key[1] == "account")
    assert _figures(_delivery(missing=every)).outcome is ReachOutcome.INSUFFICIENT
    empty = build_sections(SectionsRequest(TemporalPresetId.LAST_COMPLETE_WEEK, WEEK, None))
    assert (empty.outcome, empty.overlap, empty.funnel) == (ReachOutcome.EMPTY, None, None)


def test_the_overlap_insight_names_the_id_space_and_window_and_never_says_exact() -> None:
    slot = reach_overlap_slot(ReachSlotRequest(_delivery(), True, REVISION, "quiet"))
    assert slot.state is InsightSlotState.FILLED
    assert slot.text == (
        "Among Amazon account IDs reached in Week of 14\u201320 Sep 2026, DSP reached 2000 "
        "shoppers that sponsored ads didn't within 14 days of their first ad in that week, "
        "20.00% of the "
        "total. Overlap on account IDs is a lower bound."
    )
    assert "exact" not in (slot.text or "")
    # Control: a gap in the account cells keeps it silent.
    gap = _delivery(missing=(("sponsored_only", "account"),))
    assert reach_overlap_slot(ReachSlotRequest(gap, True, REVISION, "quiet")).state is (
        InsightSlotState.SILENT
    )


def test_the_funnel_insight_says_association_and_never_cause() -> None:
    slot = full_funnel_association_slot(ReachSlotRequest(_delivery(), True, REVISION, "quiet"))
    assert slot.state is InsightSlotState.FILLED
    text = slot.text or ""
    assert text.endswith("This shows association, not cause.")
    assert not any(word in text for word in ("caused", "drove", "lifted", "incremental"))
    assert "device" not in text
    # Control: an immaterial difference is silent.
    quiet = full_funnel_association_slot(ReachSlotRequest(_delivery(), False, REVISION, "quiet"))
    assert quiet.state is InsightSlotState.SILENT


def test_the_payload_carries_every_section_and_the_boundary() -> None:
    delivery = _delivery()
    market = ReachMarket(
        "amazon_us",
        _figures(delivery),
        (
            reach_overlap_slot(ReachSlotRequest(delivery, True, REVISION, "quiet")),
            full_funnel_association_slot(ReachSlotRequest(delivery, True, REVISION, "quiet")),
            _saturation(None),
        ),
    )
    artifact = ReachAndFunnelArtifact(TemporalPresetId.LAST_COMPLETE_WEEK, WEEK, (market,))
    payload = reach_artifact_payload(artifact)
    assert (payload["type"], payload["version"], payload["outcome"]) == (
        "ReachAndFunnelArtifact",
        1,
        "complete",
    )
    assert payload["boundary"] == list(BOUNDARY)
    assert payload["evidence"] == {
        "pre_exposure_exclusion": "pending_step_b",
        "fence_check": "structural check, not the exact pack text",
    }
    (body,) = cast("list[dict[str, Any]]", payload["markets"])
    assert body["funnel"]["caveat"] == list(CAVEAT_BLOCK)
    assert [slot["id"] for slot in body["insights"]] == [
        InsightSlotId.REACH_OVERLAP.value,
        InsightSlotId.FULL_FUNNEL_ASSOCIATION.value,
        InsightSlotId.FREQUENCY_SATURATION.value,
    ]
    assert body["overlap"]["all_ids_label"] == "All ID types"
    assert body["overlap"]["tiles"][0]["current"]["display_text"] == "30.00%"
    assert "associated with" in payload["starter_questions"][0]  # type: ignore[index]
    # The week preset doesn't bind frequency, so nothing is listed as not shown in full.
    assert payload["evidence_status"] == {"version": 2, "slots": []}
    assert payload["page_context_expires_at"] is None


def test_a_pending_frequency_section_is_listed_in_the_evidence_status() -> None:
    sections = build_sections(SectionsRequest(TemporalPresetId.LAST_COMPLETE_MONTH, MONTH, None))
    artifact = ReachAndFunnelArtifact(
        TemporalPresetId.LAST_COMPLETE_MONTH,
        MONTH,
        (
            ReachMarket(
                "amazon_us",
                sections,
                (
                    reach_overlap_slot(ReachSlotRequest(None, True, REVISION, "quiet")),
                    full_funnel_association_slot(ReachSlotRequest(None, True, REVISION, "quiet")),
                    _saturation(None),
                ),
            ),
        ),
    )
    status = cast("dict[str, Any]", reach_artifact_payload(artifact)["evidence_status"])
    assert status == {
        "version": 2,
        "slots": [
            {
                "marketplace_key": "amazon_us",
                "slot_label": "Frequency",
                "role_label": "This period",
                "missing_dates": [{"start": "2026-09-01", "end": "2026-09-30"}],
                "reason_text": "Figures for September 2026 are expected after 16 Oct 2026.",
                "recovery_text": None,
            }
        ],
    }


# --- frequency (packs 6 and 7) -------------------------------------------------------------------

# A curve that rises to 3 ads and is flat from there, each bucket with 100,000 shoppers.
_RISING_THEN_FLAT = {
    "1": 2000,
    "2": 3000,
    "3": 4000,
    "4": 4000,
    "5": 4000,
    "6": 4000,
    "7": 4000,
    "8": 4000,
    "9": 4000,
    "10_plus": 4000,
}


# At volume: 3%, 4% and 5% of 1,000,000 shoppers, then flat at 5% (design §6.3's worked case).
_SATURATING = dict.fromkeys(_RISING_THEN_FLAT, 50000) | {"1": 30_000, "2": 40_000}


def _frequency(  # noqa: PLR0913 -- one fictional delivery, by keyword
    period: CalendarPeriod = MONTH,
    *,
    scheme: str = "fine",
    purchasers: dict[str, int] | None = None,
    shoppers: int = 100_000,
    missing: tuple[str, ...] = (),
    sponsored_currency: str = "USD",
) -> FrequencyDelivery:
    buckets = BUCKET_SCHEMES[scheme]
    curve = purchasers or {bucket: 4000 for bucket, _label in buckets}
    cells: list[FrequencyCell] = []
    for bucket, _label in buckets:
        for space in ("account", "device_or_browser"):
            if bucket in missing and space == "account":
                cells.append(FrequencyCell(bucket, space, returned=False))
                continue
            cells.append(
                FrequencyCell(
                    bucket,
                    space,
                    returned=True,
                    shoppers=Decimal(shoppers),
                    purchasers=Decimal(curve[bucket]),
                    revenue_incl_halo=Decimal(1000),
                    currency_code="USD",
                    dsp_window_spend=Decimal(300),
                    dsp_currency="USD",
                    sponsored_window_spend=Decimal(100),
                    sponsored_currency=sponsored_currency,
                )
            )
    return FrequencyDelivery(period, scheme, tuple(cells))


CHANNELS = (
    FrequencyChannel("sponsored_products", "Sponsored Products"),
    FrequencyChannel("amazon_dsp", "Amazon DSP"),
)


def _frequency_section(
    delivery: FrequencyDelivery | None, *, campaigns: int | None = 6
) -> FrequencySection:
    return build_sections(
        SectionsRequest(
            TemporalPresetId.LAST_COMPLETE_MONTH,
            MONTH,
            None,
            frequency=delivery,
            channels=CHANNELS,
            active_campaigns=campaigns,
        )
    ).frequency


def test_the_frequency_curve_reads_each_bucket_s_account_purchaser_rate_with_its_scheme() -> None:
    section = _frequency_section(_frequency(purchasers=_RISING_THEN_FLAT, missing=("9",)))
    assert section.state is ReachSectionState.FIGURES
    assert (section.bucket_scheme, section.axis_label) == ("fine", SCHEME_AXIS_LABELS["fine"])
    assert section.caveat == FREQUENCY_CAVEAT
    assert section.caveat[-1] == (
        "Shoppers can see more ads after they buy, so higher buckets may look stronger."
    )
    rates = {item.bucket: item.purchaser_rate for item in section.buckets}
    assert [item.label for item in section.buckets][-1] == "10 or more"
    assert rates["1"] == ReachValue(Decimal("0.0200"), ReachValueUnit.SHARE)
    assert rates["3"].display_text == "4.00%"  # type: ignore[union-attr]
    # A bucket that didn't come back draws a gap: not available, never 0.
    assert rates["9"] == ReachAbsence(ReachAbsenceReason.NOT_RETURNED, NOT_RETURNED_TEXT)


def test_the_coarse_scheme_names_its_six_buckets_in_the_axis_label() -> None:
    section = _frequency_section(_frequency(scheme="coarse"))
    assert [item.bucket for item in section.buckets] == ["1", "2", "3", "4_6", "7_9", "10_plus"]
    assert [item.label for item in section.buckets][3:5] == ["4 to 6", "7 to 9"]
    assert section.axis_label == SCHEME_AXIS_LABELS["coarse"]
    assert "coarse" in (section.axis_label or "")
    # Control: the fine label names no coarse bucket.
    assert "4 to 6" not in SCHEME_AXIS_LABELS["fine"]


def test_every_per_channel_cell_reads_not_available_per_channel_beside_the_platform_figure() -> (
    None
):
    """Plan D12: pack 7 ships without its credit branch, so no per-channel return is shown."""
    section = _frequency_section(_frequency())
    table = section.per_channel
    assert table is not None
    assert [channel.label for channel, _cells in table.rows] == [
        "Sponsored Products",
        "Amazon DSP",
    ]
    cells = [cell for _channel, row in table.rows for _bucket, cell in row]
    assert len(cells) == 20
    assert set(cells) == {
        ReachAbsence(ReachAbsenceReason.NOT_AVAILABLE_PER_CHANNEL, NOT_AVAILABLE_PER_CHANNEL_TEXT)
    }
    assert table.unavailable_reason == NOT_AVAILABLE_PER_CHANNEL_REASON
    assert table.foot.startswith("Attributed sales aren't added across channels")
    assert table.label.endswith("· September 2026")
    # Paired control: the platform figure beside it renders, 1000 / (300 + 100) = 2.50x.
    platform = {item.bucket: item.platform_return for item in section.buckets}
    assert platform["1"] == ReachValue(Decimal("2.5000"), ReachValueUnit.MULTIPLIER)
    assert platform["1"].display_text == "2.50x"  # type: ignore[union-attr]


def test_platform_return_adds_the_two_spends_only_where_their_currencies_agree() -> None:
    """Design §5.5: a currency disagreement refuses a figure that adds the two cost columns."""
    split = _frequency_section(_frequency(sponsored_currency="CAD"))
    assert {item.platform_return for item in split.buckets} == {
        ReachAbsence(ReachAbsenceReason.CURRENCY_MISMATCH, CURRENCY_MISMATCH_TEXT)
    }
    # Controls: no sales in a bucket reads 0.00x; no spend has nothing to divide by.
    no_sales = FrequencyCell(
        "1",
        "account",
        returned=True,
        shoppers=Decimal(10),
        purchasers=Decimal(0),
        sponsored_window_spend=Decimal(50),
        sponsored_currency="USD",
    )
    assert return_by_frequency(no_sales) == ReachValue(Decimal("0.0000"), ReachValueUnit.MULTIPLIER)
    no_spend = FrequencyCell("1", "account", returned=True, shoppers=Decimal(10))
    assert return_by_frequency(no_spend) == ReachAbsence(ReachAbsenceReason.NO_SPEND, NO_SPEND_TEXT)
    # A DSP spend of 0 with no currency (sponsored-only shoppers) doesn't block the sum.
    sponsored_only = replace(no_sales, revenue_incl_halo=Decimal(100), currency_code="USD")
    assert return_by_frequency(sponsored_only) == ReachValue(
        Decimal("2.0000"), ReachValueUnit.MULTIPLIER
    )


def test_frequency_reads_its_readiness_pending_and_absence_texts() -> None:
    assert _frequency_section(_frequency(), campaigns=3) == FrequencySection(
        ReachSectionState.NEEDS_CAMPAIGNS, NEEDS_CAMPAIGNS_TEXT
    )
    assert NEEDS_CAMPAIGNS_TEXT == "Needs at least 4 active campaigns"
    pending = _frequency_section(None)
    assert (pending.state, pending.text) == (
        ReachSectionState.PENDING,
        "Figures for September 2026 are expected after 16 Oct 2026.",
    )
    week = build_sections(SectionsRequest(TemporalPresetId.LAST_COMPLETE_WEEK, WEEK, None))
    assert (week.frequency.state, week.frequency.text) == (
        ReachSectionState.OUTSIDE_DECLARED_PERIODS,
        FREQUENCY_ABSENT_TEXT,
    )
    # Control: four active campaigns are enough.
    assert _frequency_section(_frequency(), campaigns=4).state is ReachSectionState.FIGURES


def test_the_quarter_preset_anchors_frequency_and_shows_its_figures() -> None:
    sections = build_sections(
        SectionsRequest(
            TemporalPresetId.LAST_COMPLETE_QUARTER,
            QUARTER,
            None,
            frequency=_frequency(QUARTER),
            channels=CHANNELS,
            active_campaigns=6,
        )
    )
    assert sections.frequency.state is ReachSectionState.FIGURES
    assert sections.overlap is not None and sections.overlap.text == OVERLAP_ABSENT_TEXT


def test_frequency_saturation_names_the_smallest_bucket_whose_next_two_steps_are_flat() -> None:
    delivery = _frequency(purchasers=_SATURATING, shoppers=1_000_000)
    assert saturated_bucket(delivery, POLICY) == "3"
    slot = _saturation(delivery)
    assert slot.state is InsightSlotState.FILLED
    assert slot.text == (
        "Among Amazon account IDs reached in September 2026, purchaser rate stopped clearly "
        "rising after 3 ads within 14 days of the first ad: each of the next two steps rose by "
        "at most 2% of the rate, even at the upper bound. This shows association, not cause; "
        "shoppers can see more ads after they buy."
    )
    assert not any(word in (slot.text or "") for word in ("caused", "drove", "lifted"))
    assert share_above(delivery, "3") == Decimal("70.00")
    # Control: immaterial is silent.
    assert _saturation(delivery, material=False).state is InsightSlotState.SILENT


def test_a_noisy_30_purchaser_dip_stays_silent() -> None:
    """Design §6.3: at 30 purchasers a rate's relative error is about 18%, far above 2%."""
    noisy = dict.fromkeys(_RISING_THEN_FLAT, 30)
    noisy["4"] = 28  # below P_min: never compared
    noisy["5"] = 30
    delivery = _frequency(purchasers=noisy, shoppers=1000)
    assert saturated_bucket(delivery, POLICY) is None
    assert _saturation(delivery).state is InsightSlotState.SILENT
    # Paired control: the same flat curve at volume names its first bucket.
    flat = _frequency(purchasers=dict.fromkeys(noisy, 30000), shoppers=1_000_000)
    assert saturated_bucket(flat, POLICY) == "1"


def test_saturation_compares_single_count_buckets_only_and_is_silent_beside_a_gap() -> None:
    coarse_flat = _frequency(
        scheme="coarse",
        purchasers={bucket: 40_000 for bucket, _label in BUCKET_SCHEMES["coarse"]},
        shoppers=1_000_000,
    )
    # Only 1 -> 2 -> 3 is compared on the coarse scheme, never a range bucket.
    assert saturated_bucket(coarse_flat, POLICY) == "1"
    rising = {
        "1": 20_000,
        "2": 30_000,
        "3": 40_000,
        "4_6": 40_000,
        "7_9": 40_000,
        "10_plus": 40_000,
    }
    coarse = _frequency(scheme="coarse", purchasers=rising, shoppers=1_000_000)
    assert saturated_bucket(coarse, POLICY) is None
    # Any account bucket not available keeps it silent; control: the whole curve names 3.
    gap = _frequency(purchasers=_SATURATING, shoppers=1_000_000, missing=("10_plus",))
    assert saturated_bucket(gap, POLICY) is None
    whole = _frequency(purchasers=_SATURATING, shoppers=1_000_000)
    assert saturated_bucket(whole, POLICY) == "3"


def test_the_payload_carries_the_frequency_section_for_the_strict_decoder() -> None:
    sections = build_sections(
        SectionsRequest(
            TemporalPresetId.LAST_COMPLETE_MONTH,
            MONTH,
            _delivery(MONTH),
            frequency=_frequency(missing=("2",)),
            channels=CHANNELS[:1],
            active_campaigns=6,
        )
    )
    market = ReachMarket(
        "amazon_us",
        sections,
        (
            reach_overlap_slot(ReachSlotRequest(None, True, REVISION, "quiet")),
            full_funnel_association_slot(ReachSlotRequest(None, True, REVISION, "quiet")),
            _saturation(None),
        ),
    )
    payload = reach_artifact_payload(
        ReachAndFunnelArtifact(TemporalPresetId.LAST_COMPLETE_MONTH, MONTH, (market,))
    )
    (body,) = cast("list[dict[str, Any]]", payload["markets"])
    frequency = body["frequency"]
    assert (frequency["state"], frequency["text"], frequency["bucket_scheme"]) == (
        "figures",
        None,
        "fine",
    )
    assert frequency["caveat"] == list(FREQUENCY_CAVEAT)
    assert frequency["buckets"][0] == {
        "bucket": "1",
        "label": "1 ad",
        "purchaser_rate": {
            "value": "0.0400",
            "unit": "share",
            "currency_code": None,
            "display_text": "4.00%",
        },
        "platform_return": {
            "value": "2.5000",
            "unit": "multiplier",
            "currency_code": None,
            "display_text": "2.50x",
        },
    }
    assert frequency["buckets"][1]["purchaser_rate"] == {
        "absence": {"reason": "not_returned", "text": NOT_RETURNED_TEXT}
    }
    (row,) = frequency["per_channel"]["rows"]
    assert row["cells"]["10_plus"] == {
        "absence": {"reason": "not_available_per_channel", "text": "Not available per channel"}
    }
    # Frequency is shown, so nothing is listed as not shown in full.
    assert payload["evidence_status"] == {"version": 2, "slots": []}


# --- the closed served contract (frontend/lib/reach.ts) ---------------------------------------


def _contract_payload(
    request: SectionsRequest, delivery: ReachDelivery | None = None
) -> dict[str, Any]:
    slot = ReachSlotRequest(delivery, True, REVISION, "quiet")
    market = ReachMarket(
        "amazon_us",
        build_sections(request),
        (reach_overlap_slot(slot), full_funnel_association_slot(slot), _saturation(None)),
    )
    return cast(
        "dict[str, Any]",
        reach_artifact_payload(ReachAndFunnelArtifact(request.preset, request.period, (market,))),
    )


_WEEK_PRESET = TemporalPresetId.LAST_COMPLETE_WEEK
_MONTH_PRESET = TemporalPresetId.LAST_COMPLETE_MONTH
_ACCOUNT_CELLS = tuple(key for key in _GRID if key[1] == "account")


def _designed_states() -> dict[str, dict[str, Any]]:
    """One bake per designed state the page entry declares, each the bake's own output."""
    return {
        "week": _contract_payload(SectionsRequest(_WEEK_PRESET, WEEK, _delivery()), _delivery()),
        "week_with_prior": _contract_payload(
            SectionsRequest(_WEEK_PRESET, WEEK, _delivery(), _delivery(PRIOR)), _delivery()
        ),
        "variant_refused": _contract_payload(
            SectionsRequest(_WEEK_PRESET, WEEK, _delivery(), _delivery(PRIOR), prior_refused=True)
        ),
        "not_returned": _contract_payload(
            SectionsRequest(_WEEK_PRESET, WEEK, _delivery(missing=(("dsp_only", "account"),)))
        ),
        "needs_dsp": _contract_payload(
            SectionsRequest(_WEEK_PRESET, WEEK, None, dsp_available=False)
        ),
        "empty": _contract_payload(SectionsRequest(_WEEK_PRESET, WEEK, None)),
        "insufficient": _contract_payload(
            SectionsRequest(_WEEK_PRESET, WEEK, _delivery(missing=_ACCOUNT_CELLS))
        ),
        "month_pending": _contract_payload(SectionsRequest(_MONTH_PRESET, MONTH, _delivery(MONTH))),
        "month_frequency": _contract_payload(
            SectionsRequest(
                _MONTH_PRESET,
                MONTH,
                _delivery(MONTH),
                frequency=_frequency(),
                channels=CHANNELS,
                active_campaigns=6,
            )
        ),
        "month_needs_campaigns": _contract_payload(
            SectionsRequest(
                _MONTH_PRESET, MONTH, _delivery(MONTH), frequency=_frequency(), active_campaigns=2
            )
        ),
        "month_coarse": _contract_payload(
            SectionsRequest(
                _MONTH_PRESET,
                MONTH,
                _delivery(MONTH),
                frequency=_frequency(scheme="coarse"),
                active_campaigns=6,
            )
        ),
        "month_coarse_per_channel": _contract_payload(
            SectionsRequest(
                _MONTH_PRESET,
                MONTH,
                _delivery(MONTH),
                frequency=_frequency(scheme="coarse"),
                channels=CHANNELS,
                active_campaigns=6,
            )
        ),
        "quarter": _contract_payload(
            SectionsRequest(
                TemporalPresetId.LAST_COMPLETE_QUARTER,
                QUARTER,
                None,
                frequency=_frequency(QUARTER),
                channels=CHANNELS,
                active_campaigns=6,
            )
        ),
    }


@pytest.mark.parametrize("state", sorted(_designed_states()))
def test_the_served_contract_accepts_every_designed_state_the_bake_writes(state: str) -> None:
    payload = _designed_states()[state]
    assert require_reach_artifact_payload(payload) is payload
    # The retained body's canonical key order is what the handler serves; it decodes too.
    wire = json.loads(canonical_preset_artifact_json(payload))
    require_reach_artifact_payload(wire)


# The bake's own sorted-key bytes for a coarse month with per-channel rows. The browser decodes
# the same committed file (`frontend/test/reach.test.ts`), so both decoders face one body.
_COARSE_WIRE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "frontend/test/fixtures/reach-month-coarse-wire.json"
)


def test_the_serializers_sorted_key_coarse_per_channel_body_passes_the_served_contract() -> None:
    text = _COARSE_WIRE.read_text(encoding="utf-8")
    assert text == (
        canonical_preset_artifact_json(_designed_states()["month_coarse_per_channel"]) + "\n"
    )
    wire = json.loads(text)
    frequency = _market_of(wire)["frequency"]
    rows = frequency["per_channel"]["rows"]
    assert frequency["bucket_scheme"] == "coarse"
    assert [row["ad_product_key"] for row in rows] == ["sponsored_products", "amazon_dsp"]
    # Sorted keys put "10_plus" second: the wire order is not the roster order.
    for row in rows:
        assert list(row["cells"]) == ["1", "10_plus", "2", "3", "4_6", "7_9"]
    assert require_reach_artifact_payload(wire) is wire


def _drop_a_bucket(cells: dict[str, Any]) -> None:
    del cells["7_9"]


def _add_a_bucket(cells: dict[str, Any]) -> None:
    cells["extra"] = copy.deepcopy(cells["1"])


def _rename_a_bucket(cells: dict[str, Any]) -> None:
    cells["4_to_6"] = cells.pop("4_6")


@pytest.mark.parametrize(
    "edit",
    [_drop_a_bucket, _add_a_bucket, _rename_a_bucket],
    ids=["missing", "extra", "renamed"],
)
def test_coarse_per_channel_cells_refuse_a_missing_an_extra_or_a_renamed_bucket(
    edit: Callable[[dict[str, Any]], None],
) -> None:
    wire = json.loads(_COARSE_WIRE.read_text(encoding="utf-8"))
    control = copy.deepcopy(wire)
    assert require_reach_artifact_payload(control) is control
    edit(_market_of(wire)["frequency"]["per_channel"]["rows"][0]["cells"])
    with pytest.raises(ValueError) as caught:
        require_reach_artifact_payload(wire)
    assert type(caught.value) is ValueError
    assert str(caught.value) == "the Reach and funnel artifact is invalid."


def _market_of(payload: dict[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", payload["markets"][0])


def _second_market(payload: dict[str, Any]) -> None:
    second = json.loads(json.dumps(_market_of(payload)))
    second["marketplace_key"] = "second_fictional_market"
    payload["markets"].append(second)


def _total_channel(payload: dict[str, Any]) -> None:
    rows = _market_of(payload)["frequency"]["per_channel"]["rows"]
    total = json.loads(json.dumps(rows[0]))
    total.update(ad_product_key="all_channels", label="All channels")
    rows.append(total)


def _prior_without_its_change(payload: dict[str, Any]) -> None:
    # The prior figure is gone, yet the tile still shows a change against it.
    _market_of(payload)["overlap"]["tiles"][0]["prior"] = None


def _filled_beside_no_figures(payload: dict[str, Any]) -> None:
    slot = _market_of(payload)["insights"][2]
    for key in ("absence_class", "absence_reason"):
        del slot[key]
    slot.update(state="filled", text="Fictional saturation.", evidence_line="Fictional rule.")


_REFUSALS: dict[str, tuple[str, Callable[[dict[str, Any]], object]]] = {
    "unknown_outcome": ("week", lambda body: body.update(outcome="unknown")),
    "no_market": ("week", lambda body: body.update(markets=[])),
    "two_markets": ("week", _second_market),
    "extra_key": ("week", lambda body: body.update(total=None)),
    "boolean_version": ("week", lambda body: body.update(version=True)),
    "section_state": ("week", lambda body: _market_of(body)["funnel"].update(state="partial")),
    "pair_disagrees": (
        "needs_dsp",
        lambda body: _market_of(body)["funnel"].update(
            state="outside_declared_periods",
            text=FUNNEL_ABSENT_TEXT,
        ),
    ),
    "change_disagrees": ("week_with_prior", _prior_without_its_change),
    "unit_drift": (
        "week",
        lambda body: _market_of(body)["overlap"]["tiles"][0]["current"].update(unit="percent"),
    ),
    "filled_insight_without_figures": ("week", _filled_beside_no_figures),
    "total_channel": ("month_frequency", _total_channel),
    "pending_text": (
        "month_pending",
        lambda body: _market_of(body)["frequency"].update(
            text="Figures for September 2026 are expected after 17 Oct 2026."
        ),
    ),
    "pending_slot": ("month_pending", lambda body: body["evidence_status"].update(slots=[])),
    "context_label": ("week", lambda body: body.update(page_context_label="Reach and funnel")),
    "context_expiry": (
        "week",
        lambda body: body.update(page_context_expires_at="2026-10-01T00:00:00Z"),
    ),
    "starter": ("week", lambda body: body.update(starter_questions=["Did DSP drive sales?"])),
    "boundary": ("week", lambda body: body.update(boundary=list(BOUNDARY[:2]))),
    "fence_check": ("week", lambda body: body["evidence"].update(fence_check="passed")),
}


@pytest.mark.parametrize("case", sorted(_REFUSALS))
def test_the_served_contract_refuses_what_the_browser_decoder_refuses(case: str) -> None:
    base, edit = _REFUSALS[case]
    payload = _designed_states()[base]
    require_reach_artifact_payload(json.loads(json.dumps(payload)))  # control: the base decodes
    edit(payload)
    with pytest.raises(ValueError, match="Reach and funnel artifact is invalid"):
        require_reach_artifact_payload(payload)
