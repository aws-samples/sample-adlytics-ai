# SPDX-License-Identifier: MIT-0
"""`RetentionCohortArtifact` v1: the triangle, the ARP rate, the curve, the quarterly state.

Every figure is fictional. The domain builds the artifact from planned per-cell values; the page
schema refuses an all-ID rate on the curve; `retention-shift` follows its contract's policy.
"""

from __future__ import annotations

import pathlib
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import cast

import pytest

from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.outbound.metric_dictionary import load_catalog
from domain.data_contracts.contract import CohortGrain
from domain.measurement.periodic import COHORT_SIZES_CHANGED
from domain.presentation.insights import InsightSlotId, InsightSlotState, ShiftPolicy
from domain.presentation.page_schemas import (
    Cardinality,
    PageSection,
    PresentationVintagePolicy,
    _presentation_section_findings,  # pyright: ignore[reportPrivateUsage]
)
from domain.presentation.retention import (
    ABSENT_COHORT_TEXT,
    PAID_CALLOUT,
    RETENTION_PAGE_ID,
    WITHHELD_CELL_LEGEND,
    CohortCellState,
    CohortDelivery,
    PaidPathState,
    RetentionCohortArtifact,
    RetentionGroupGrid,
    RetentionMarket,
    RetentionRow,
    RetentionVintage,
    ShiftSlotRequest,
    arp_rate,
    build_market,
    require_retention_artifact_payload,
    retention_artifact_payload,
    retention_shift_slot,
    retention_shifts,
    scope_band,
    shift_named,
    vintage_cohorts,
)
from domain.semantics.calendar_periods import vintage_ending

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-099", "ADR-101", "ADR-104")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
SEPTEMBER = vintage_ending(date(2026, 9, 1))
GROUPS = ("dsp_only", "sponsored_only", "dsp_and_sponsored")
POLICY = ShiftPolicy(200, Decimal("1.96"))
US = "amazon_us"


def _standard(  # noqa: PLR0913 -- one fictional grid, by keyword
    grain: CohortGrain = CohortGrain.MONTH,
    *,
    skip: tuple[tuple[date, int, str], ...] = (),
    absent: tuple[tuple[date, str], ...] = (),
    size: int = 400,
    repeat: int = 40,
) -> CohortDelivery:
    cells: dict[tuple[date, int, str], dict[str, Decimal | None]] = {}
    for cohort in vintage_cohorts(SEPTEMBER, grain):
        for offset in cohort.offsets:
            for group in GROUPS:
                key = (cohort.start, offset, group)
                if (cohort.start, group) in absent:
                    continue
                if key in skip:
                    cells[key] = dict.fromkeys(
                        (
                            "cohort_buyers_new_to_brand_12mo",
                            "cohort_buyers_account_ids",
                            "cohort_repeat_buyers",
                            "cohort_repeat_buyers_cumulative_account_ids",
                        )
                    )
                    continue
                cells[key] = {
                    "cohort_buyers_new_to_brand_12mo": Decimal(size),
                    "cohort_buyers_account_ids": Decimal(300),
                    "cohort_repeat_buyers": Decimal(repeat),
                    "cohort_repeat_buyers_cumulative_account_ids": Decimal(20 + offset),
                }
    return CohortDelivery("standard", grain, cells)


def _paid(
    check: int = 300, grain: CohortGrain = CohortGrain.MONTH, repeat: int = 60
) -> CohortDelivery:
    cells: dict[tuple[date, int, str], dict[str, Decimal | None]] = {}
    checks: dict[tuple[date, str], Decimal] = {}
    for cohort in vintage_cohorts(SEPTEMBER, grain):
        for group in GROUPS:
            checks[(cohort.start, group)] = Decimal(check)
            for offset in cohort.offsets:
                cells[(cohort.start, offset, group)] = {
                    "cohort_repeat_buyers": Decimal(repeat),
                    "cohort_repeat_buyers_cumulative": Decimal(repeat + offset),
                }
    return CohortDelivery("arp", grain, cells, checks)


def _silent_slot(vintage: RetentionVintage) -> tuple[object, ...]:
    return (retention_shift_slot(ShiftSlotRequest(vintage, (), "sha256:" + "0" * 64, "quiet")),)


def _market(vintage: RetentionVintage):
    return build_market(vintage, _silent_slot(vintage))  # type: ignore[arg-type]


def _row(grids: tuple[RetentionGroupGrid, ...], group: str, cohort: date) -> RetentionRow:
    grid = next(item for item in grids if item.group == group)
    return next(row for row in grid.rows if row.cohort == cohort)


def test_the_standard_grid_binds_the_all_id_rate_with_size_and_the_paid_callout_unobserved() -> (
    None
):
    market = _market(RetentionVintage(SEPTEMBER, US, _standard()))
    row = _row(market.standard, "sponsored_only", date(2025, 9, 1))
    first = row.cells[0]
    assert (first.offset, first.column_label, first.state) == (1, "Month 1", CohortCellState.VALUE)
    assert first.rate is not None and (first.rate.value, first.rate.text) == (
        Decimal("0.1000"),
        "10.0%",
    )
    assert first.cohort_size is not None and first.cohort_size.text == "400 new customers"
    grid = next(item for item in market.standard if item.group == "dsp_only")
    assert grid.label == "Won by DSP only"
    assert grid.grid_label == (
        "Won by DSP only · ad-touched repeat only. Understates true repeat. Month 0 repeats are "
        "excluded."
    )
    # The paid path hasn't delivered: the callout reads, and the standard path renders.
    assert market.paid_state is PaidPathState.NOT_OBSERVED
    assert market.paid == ()
    body = retention_artifact_payload(RetentionCohortArtifact(SEPTEMBER, (market,)))
    (payload,) = body["markets"]  # type: ignore[misc]
    assert payload["path"]["paid"] == {"state": "not_observed", "callout": PAID_CALLOUT}
    assert payload["triangle"]["legend"] == WITHHELD_CELL_LEGEND
    assert body["vintage"] == {  # type: ignore[comparison-overlap]
        "as_of_month": "2026-09",
        "label": "September 2026",
        "start": "2025-09-01",
        "end": "2026-09-30",
    }


def test_a_not_returned_cell_is_not_available_and_a_future_month_reads_not_yet() -> None:
    gap = (date(2026, 6, 1), 2, "dsp_only")
    market = _market(RetentionVintage(SEPTEMBER, US, _standard(skip=(gap,))))
    row = _row(market.standard, "dsp_only", date(2026, 6, 1))
    states = {cell.offset: (cell.state, cell.text) for cell in row.cells}
    assert states[2] == (CohortCellState.NOT_RETURNED, "Not available")
    # Control: its neighbour came back.
    assert states[1][0] is CohortCellState.VALUE
    # June 2026 has three follow-up months by September; months 4 to 12 haven't happened.
    assert states[3][0] is CohortCellState.VALUE
    assert {
        offset for offset, (state, _text) in states.items() if state is CohortCellState.NOT_YET
    } == set(range(4, 13))
    assert states[4][1] == "Not yet"


def test_an_absent_cohort_renders_its_text_never_hatched_gaps() -> None:
    market = _market(
        RetentionVintage(SEPTEMBER, US, _standard(absent=((date(2026, 3, 1), "dsp_only"),)))
    )
    row = _row(market.standard, "dsp_only", date(2026, 3, 1))
    assert row.cells == ()
    assert (
        row.absent_text
        == ABSENT_COHORT_TEXT.format(month="March 2026")
        == ("No shoppers were won this way in March 2026, or too few to show.")
    )
    # Control: another group's March cohort came back.
    assert _row(market.standard, "sponsored_only", date(2026, 3, 1)).absent_text is None


def test_a_sponsored_only_advertiser_gets_an_intact_sponsored_triangle() -> None:
    cohorts = vintage_cohorts(SEPTEMBER, CohortGrain.MONTH)
    absent = tuple(
        (cohort.start, group) for cohort in cohorts for group in ("dsp_only", "dsp_and_sponsored")
    )
    market = _market(RetentionVintage(SEPTEMBER, US, _standard(absent=absent)))
    sponsored = next(grid for grid in market.standard if grid.group == "sponsored_only")
    assert all(row.absent_text is None for row in sponsored.rows)
    assert all(
        cell.state is not CohortCellState.NOT_RETURNED
        for row in sponsored.rows
        for cell in row.cells
    )
    dsp = next(grid for grid in market.standard if grid.group == "dsp_only")
    assert all(row.absent_text is not None for row in dsp.rows)


def test_the_arp_rate_divides_pack_15_by_pack_14s_account_id_cohort_size() -> None:
    market = _market(RetentionVintage(SEPTEMBER, US, _standard(), _paid()))
    cell = _row(market.paid, "dsp_only", date(2025, 9, 1)).cells[0]
    assert cell.state is CohortCellState.VALUE
    assert cell.rate is not None and cell.rate.value == Decimal("0.2000")  # 60 / 300
    assert next(grid for grid in market.paid).grid_label == (
        "All Amazon purchases · Amazon account IDs only"
    )


def test_the_arp_rate_is_withheld_when_cohort_sizes_changed_between_runs() -> None:
    """Design §6.6, review DS-13: pack 15's own count differs from pack 14's."""
    market = _market(RetentionVintage(SEPTEMBER, US, _standard(), _paid(check=301)))
    cell = _row(market.paid, "dsp_only", date(2025, 9, 1)).cells[0]
    assert (cell.state, cell.text, cell.rate) == (
        CohortCellState.WITHHELD,
        "cohort sizes changed between runs",
        None,
    )
    # Numerator above the denominator is withheld the same way.
    above = _market(RetentionVintage(SEPTEMBER, US, _standard(), _paid(repeat=301)))
    assert _row(above.paid, "dsp_only", date(2025, 9, 1)).cells[0].text == COHORT_SIZES_CHANGED
    # Paired control: equal sizes serve the rate.
    assert (
        arp_rate(
            Decimal(60),
            Decimal(300),
            Decimal(300),
            grains=(CohortGrain.MONTH, CohortGrain.MONTH),
            first_quarterly="September 2026",
        )
        == _row(
            _market(RetentionVintage(SEPTEMBER, US, _standard(), _paid())).paid,
            "dsp_only",
            date(2025, 9, 1),
        )
        .cells[0]
        .rate
    )


def test_an_arp_rate_across_cohort_grains_is_withheld_with_the_grain_reason() -> None:
    """The serve-time backstop: never "cohort sizes changed between runs"."""
    assert (
        arp_rate(
            Decimal(60),
            Decimal(300),
            Decimal(300),
            grains=(CohortGrain.QUARTER, CohortGrain.MONTH),
            first_quarterly="August 2026",
        )
        == "Cohort grain changed in August 2026."
    )
    # Controls: same-grain operands serve the rate, monthly and quarterly.
    for grain in (CohortGrain.MONTH, CohortGrain.QUARTER):
        served = arp_rate(
            Decimal(60), Decimal(300), Decimal(300), grains=(grain, grain), first_quarterly="x"
        )
        assert not isinstance(served, str) and served is not None


def test_the_curve_pools_account_id_rates_and_shows_no_partial_sum() -> None:
    market = _market(RetentionVintage(SEPTEMBER, US, _standard()))
    line = next(item for item in market.curve if item.group == "sponsored_only")
    first = line.points[0]
    # Month 1: twelve cohorts of 300 account IDs, each with 21 cumulative repeaters.
    assert first.rate is not None and first.rate.value == Decimal("0.0700")
    assert [point.offset for point in line.points] == list(range(1, 13))
    gap = (date(2025, 12, 1), 3, "sponsored_only")
    holed = _market(RetentionVintage(SEPTEMBER, US, _standard(skip=(gap,))))
    point = next(item for item in holed.curve if item.group == "sponsored_only").points[2]
    assert (point.rate, point.text) == (None, "Not available")
    # Control: another month's point still reads.
    assert next(item for item in holed.curve if item.group == "sponsored_only").points[1].rate


def test_a_quarterly_vintage_labels_quarters_with_no_quarter_4_and_shows_the_grain_note() -> None:
    standard = _standard(CohortGrain.QUARTER)
    market = _market(RetentionVintage(SEPTEMBER, US, standard, first_quarterly=date(2026, 8, 1)))
    grid = next(item for item in market.standard if item.group == "dsp_only")
    assert [row.label for row in grid.rows] == ["Q4 2025", "Q1 2026", "Q2 2026"]
    labels = {cell.column_label for row in grid.rows for cell in row.cells}
    assert labels == {"Quarter 1", "Quarter 2", "Quarter 3"}
    assert grid.grid_label.endswith("Quarter 0 repeats are excluded.")
    body = retention_artifact_payload(RetentionCohortArtifact(SEPTEMBER, (market,)))
    (payload,) = body["markets"]  # type: ignore[misc]
    assert payload["cohort_grain"] == "quarter"
    assert payload["grain_note"] == (
        "Cohorts are grouped by quarter from Q3 2026, because too many monthly cells had too few "
        "shoppers."
    )
    assert payload["scope_band"] == (
        "Cohorts as of September 2026, grouped by quarter: acquired Q4 2025 to Q2 2026, with "
        "repeat quarters through Q3 2026."
    )
    # Paired: a monthly vintage names its grain and carries no note.
    monthly = _market(RetentionVintage(SEPTEMBER, US, _standard()))
    (plain,) = retention_artifact_payload(RetentionCohortArtifact(SEPTEMBER, (monthly,)))["markets"]  # type: ignore[misc]
    assert plain["grain_note"] is None
    assert scope_band(SEPTEMBER, CohortGrain.MONTH) == (
        "Cohorts as of September 2026, grouped by month: acquired September 2025 to August 2026, "
        "with repeat months through September 2026."
    )


# --- retention-shift v1 ---------------------------------------------------------------------


def test_two_40_shopper_cohorts_at_10_and_20_percent_name_nothing() -> None:
    """The contract's calibration: under C_min, whatever the interval."""
    assert not shift_named(((Decimal(4), Decimal(40)), (Decimal(8), Decimal(40))), POLICY)
    # Control: the same rates on 1,000-shopper cohorts are named.
    assert shift_named(((Decimal(100), Decimal(1000)), (Decimal(200), Decimal(1000))), POLICY)
    # And an interval that includes zero names nothing.
    assert not shift_named(((Decimal(100), Decimal(1000)), (Decimal(104), Decimal(1000))), POLICY)


def test_a_shift_compares_one_groups_latest_two_cohorts_and_is_silent_on_quarters() -> None:
    standard = _standard(size=1000, repeat=100)
    cells = dict(standard.cells)
    latest = (date(2026, 8, 1), 1, "dsp_only")
    cells[latest] = {**cells[latest], "cohort_repeat_buyers": Decimal(200)}
    vintage = RetentionVintage(SEPTEMBER, US, replace(standard, cells=cells))
    (finding,) = retention_shifts(vintage, POLICY)
    assert (finding.group, finding.offset, finding.earlier, finding.latest) == (
        "dsp_only",
        1,
        date(2026, 7, 1),
        date(2026, 8, 1),
    )
    assert finding.points == Decimal("10.0")
    slot = retention_shift_slot(ShiftSlotRequest(vintage, (finding,), "sha256:" + "0" * 64, "q"))
    assert slot.state is InsightSlotState.FILLED
    assert slot.text == (
        "For shoppers won by DSP only, the month-1 repeat rate rose 10.0 points from the July "
        "2026 cohort to the August 2026 cohort. Ad-touched repeat only, from this advertiser."
    )
    # Silent beside a not_returned cell, and on a quarterly vintage.
    holed = dict(cells)
    holed[(date(2026, 7, 1), 1, "dsp_only")] = dict.fromkeys(holed[latest])
    assert retention_shifts(replace(vintage, standard=replace(standard, cells=holed)), POLICY) == ()
    assert (
        retention_shifts(
            RetentionVintage(SEPTEMBER, US, _standard(CohortGrain.QUARTER, size=1000)), POLICY
        )
        == ()
    )


def test_the_retention_shift_contract_versions_its_policy() -> None:
    contracts = {item.id: item for _, item in load_insight_contracts(ROOT)}
    contract = contracts[InsightSlotId.RETENTION_SHIFT]
    assert contract.shift_policy == POLICY
    assert {case.kind.value for case in contract.calibration_cases} >= {
        "exploratory",
        "miscomputed-derivation",
    }


# --- the page schema --------------------------------------------------------------------------


def _vintage_page(*sections: PageSection):
    schemas = load_v2_page_schemas(ROOT)
    base = schemas.pages[0]
    return replace(
        base,
        page_id=RETENTION_PAGE_ID,
        route="/retention/",
        time_policy=PresentationVintagePolicy(),
        sections=(*sections, PageSection("boundary", Cardinality.EXACTLY_ONE)),
        boundary_required=True,
    )


def test_the_curve_section_refuses_an_all_id_rate() -> None:
    catalog = load_catalog(ROOT)
    curve = PageSection(
        "retention-curve", Cardinality.EXACTLY_ONE, metrics=("cumulative_repeat_rate",)
    )
    findings = _presentation_section_findings(_vintage_page(curve), catalog)
    assert [finding.message for finding in findings] == [
        "retention-curve/cumulative_repeat_rate: the repeat curve compares acquisition groups, so "
        "it binds Amazon account ID rates only."
    ]
    # Paired control: the account-ID rate passes, and the all-ID rate passes on the grid.
    ok = PageSection(
        "retention-curve",
        Cardinality.EXACTLY_ONE,
        metrics=("cumulative_repeat_rate_account_ids",),
    )
    grid = PageSection("triangle", Cardinality.EXACTLY_ONE, metrics=("repeat_buyer_rate",))
    assert _presentation_section_findings(_vintage_page(ok, grid), catalog) == []


def test_two_markets_name_their_marketplace_and_one_market_reads_as_the_mockups() -> None:
    def labelled(key: str, label: str) -> RetentionMarket:
        vintage = RetentionVintage(SEPTEMBER, key, _standard())
        return build_market(vintage, _silent_slot(vintage), label)  # type: ignore[arg-type]

    two = retention_artifact_payload(
        RetentionCohortArtifact(SEPTEMBER, (labelled(US, "US"), labelled("amazon_uk", "UK")))
    )
    markets = cast("list[dict[str, object]]", two["markets"])
    assert [item["marketplace_text"] for item in markets] == ["US marketplace", "UK marketplace"]
    require_retention_artifact_payload(two)
    # Control: one market carries no marketplace text, even with its label known.
    one = retention_artifact_payload(RetentionCohortArtifact(SEPTEMBER, (labelled(US, "US"),)))
    assert cast("list[dict[str, object]]", one["markets"])[0]["marketplace_text"] is None
    require_retention_artifact_payload(one)
    # Two markets without a text, a lone market with one, or two that share one are refused.
    refused = {
        "two without": [{**markets[0], "marketplace_text": None}, markets[1]],
        "one with": [{**markets[0]}],
        "two alike": [markets[0], {**markets[1], "marketplace_text": "US marketplace"}],
    }
    for value in refused.values():
        with pytest.raises(ValueError, match="marketplace texts"):
            require_retention_artifact_payload({**two, "markets": value})
    with pytest.raises(ValueError, match="names its marketplace"):
        RetentionCohortArtifact(
            SEPTEMBER,
            (_market(RetentionVintage(SEPTEMBER, US, _standard())), labelled("amazon_uk", "UK")),
        )
